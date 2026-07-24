from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Set, Tuple

from app.runtime.agents import AgentDefinition, AgentRegistry
from app.runtime.llm import ResilientLlmClient, extract_json_object
from app.runtime.models import PolicyBundle

logger = logging.getLogger(__name__)

# Safety fallback only — used when artifact planner AND LLM refinement both fail.
TASK_PIPELINES: Dict[str, List[str]] = {
    "full_evaluation": ["JDAnalysisAgent", "TechAgent", "ProjectAgent",
                        "RiskAgent", "EvidenceAgent", "ReportAgent"],
    "jd_evaluation": ["JDAnalysisAgent", "TechAgent", "ProjectAgent",
                      "RiskAgent", "EvidenceAgent", "ReportAgent"],
    "tech_match": ["TechAgent", "EvidenceAgent", "ReportAgent"],
    "project_analysis": ["ProjectAgent", "EvidenceAgent", "ReportAgent"],
    "risk_check": ["RiskAgent", "EvidenceAgent", "ReportAgent"],
    "timeline_check": ["RiskAgent", "EvidenceAgent", "ReportAgent"],
    "evidence_check": ["EvidenceAgent", "ReportAgent"],
    "jd_gap": ["JDAnalysisAgent", "TechAgent", "EvidenceAgent", "ReportAgent"],
    "project_rewrite": ["ProjectAgent", "ResumeOptimizeAgent"],
    "resume_optimize": ["ProjectAgent", "ResumeOptimizeAgent"],
    "interview_questions": ["RiskAgent", "InterviewQuestionAgent"],
    "backend_eval": ["JDAnalysisAgent", "TechAgent", "ProjectAgent",
                     "RiskAgent", "EvidenceAgent", "ReportAgent"],
    "agent_eval": ["JDAnalysisAgent", "TechAgent", "ProjectAgent",
                   "RiskAgent", "EvidenceAgent", "ReportAgent"],
    # followup remains a lightweight evaluation refinement — chat uses
    # /conversation/reply (CopilotAnswer), never this pipeline.
    "followup": ["ReportAgent"],
}

# Goal → required terminal artifacts. Planner backward-chains from these.
GOAL_ARTIFACTS: Dict[str, List[str]] = {
    "full_evaluation": [
        "resume_facts", "jd_requirements", "technical_findings",
        "project_findings", "risks", "evidence_ledger", "final_report"],
    "jd_evaluation": [
        "resume_facts", "jd_requirements", "technical_findings",
        "project_findings", "risks", "evidence_ledger", "final_report"],
    "backend_eval": [
        "resume_facts", "jd_requirements", "technical_findings",
        "project_findings", "risks", "evidence_ledger", "final_report"],
    "agent_eval": [
        "resume_facts", "jd_requirements", "technical_findings",
        "project_findings", "risks", "evidence_ledger", "final_report"],
    "tech_match": [
        "resume_facts", "jd_requirements", "technical_findings",
        "evidence_ledger", "final_report"],
    "project_analysis": [
        "resume_facts", "project_findings", "evidence_ledger", "final_report"],
    "risk_check": ["resume_facts", "risks", "evidence_ledger", "final_report"],
    "timeline_check": ["resume_facts", "risks", "evidence_ledger", "final_report"],
    "evidence_check": ["evidence_ledger", "final_report"],
    "jd_gap": [
        "resume_facts", "jd_requirements", "technical_findings",
        "evidence_ledger", "final_report"],
    "project_rewrite": ["resume_facts", "project_findings", "rewrite"],
    "resume_optimize": ["resume_facts", "project_findings", "rewrite"],
    "interview_questions": ["resume_facts", "risks", "interview_questions"],
    "followup": ["final_report"],
}

TERMINAL_AGENTS = {"ReportAgent", "ResumeOptimizeAgent", "InterviewQuestionAgent"}
FULL_EVAL_TYPES = {
    "full_evaluation", "jd_evaluation", "backend_eval", "agent_eval",
}
REPLAN_TRIGGERS = {
    "missing_required_artifact", "tool_failed", "new_conflict",
    "handoff_requested", "group_failure", "low_confidence",
}

# Soft preference for simple runTypes: still artifact-planned, but skip LLM refine.
# Full evaluations use LLM-based planning to produce dynamic agent selection.
SIMPLE_RULE_TYPES = {
    "timeline_check", "risk_check", "evidence_check", "tech_match",
    "project_analysis", "project_rewrite", "resume_optimize",
    "interview_questions", "followup",
}

# Soft dependency edges used for topo + parallel grouping (artifact edges are
# the source of truth for *selection*; these keep ordering stable).
AGENT_DEPENDENCIES: Dict[str, List[str]] = {
    "ResumeParserAgent": [],
    "JDAnalysisAgent": ["ResumeParserAgent"],
    "TechAgent": ["ResumeParserAgent"],
    "ProjectAgent": ["ResumeParserAgent"],
    "RiskAgent": ["ResumeParserAgent"],
    "EvidenceAgent": ["TechAgent", "ProjectAgent", "RiskAgent"],
    "ReportAgent": ["EvidenceAgent"],
    "ResumeOptimizeAgent": ["ProjectAgent"],
    "InterviewQuestionAgent": ["RiskAgent"],
}

PARALLELIZABLE = {"JDAnalysisAgent", "TechAgent", "ProjectAgent", "RiskAgent"}

_PROJECT_HINT = re.compile(
    r"(项目经历|项目经验|project\s*experience|side\s*project|"
    r"个人项目|开源项目|github\.com/)", re.I)
_TIMELINE_HINT = re.compile(
    r"(工作经历|实习经历|education|工作经验|"
    r"\d{4}\s*[./年-]\s*\d{1,2}|\d{4}\s*[-–—]\s*\d{4})", re.I)
_URL_HINT = re.compile(r"https?://|github\.com/|gitee\.com/", re.I)


class Coordinator:
    """Artifact-driven dynamic planner.

    Primary path:
      GOAL_ARTIFACTS[runType] → inspect present artifacts → backward-chain
      producers from the capability catalog → topological order → parallel groups.

    TASK_PIPELINES is only the safety fallback when both the artifact planner
    and the (optional) LLM refine produce an empty / invalid plan.

    Contracts: at most one terminal agent; max 2 replans (enforced by executor);
    handoff cycles are rejected by LoopGuard + replan trigger.
    """

    def __init__(self, registry: AgentRegistry, policy: PolicyBundle,
                 llm: Optional[ResilientLlmClient]) -> None:
        self.registry = registry
        self.policy = policy
        self.llm = llm

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def base_plan(self, run_type: str, *, has_resume_facts: bool,
                  needs_parse: bool) -> List[str]:
        """Safety-fallback pipeline (TASK_PIPELINES). Prefer plan_from_artifacts."""
        plan = list(TASK_PIPELINES.get(run_type, TASK_PIPELINES["full_evaluation"]))
        if needs_parse and "ResumeParserAgent" not in plan:
            plan.insert(0, "ResumeParserAgent")
        if not self.policy.evidenceVerification.enabled:
            plan = [a for a in plan if a != "EvidenceAgent"]
        # agentOrder only affects ordering preference — never strips required producers.
        plan = [a for a in plan if self.registry.known(a)]
        plan = plan[: max(1, self.policy.maxAgentCount)]
        if not any(a in TERMINAL_AGENTS for a in plan):
            plan.append("ReportAgent")
        return plan

    def resolve_goal_artifacts(self, run_type: str, *,
                               signals: Optional[Dict[str, bool]] = None
                               ) -> Tuple[List[str], List[str]]:
        """Return (required, optional) goal artifacts for this run/policy."""
        signals = signals or {}
        defaults = list(GOAL_ARTIFACTS.get(run_type, GOAL_ARTIFACTS["full_evaluation"]))
        required = list(self.policy.requiredArtifacts) if self.policy.requiredArtifacts \
            else list(defaults)
        optional = list(self.policy.optionalArtifacts or [])
        # Soften signal-gated artifacts into optional unless forced below.
        if not signals.get("has_projects") and "project_findings" in required:
            required = [a for a in required if a != "project_findings"]
            if "project_findings" not in optional:
                optional.append("project_findings")
        if not signals.get("has_timeline") and "risks" in required:
            required = [a for a in required if a != "risks"]
            if "risks" not in optional:
                optional.append("risks")
        if not self.policy.evidenceVerification.enabled:
            if "evidence_ledger" in required:
                required = [a for a in required if a != "evidence_ledger"]
            if "evidence_ledger" not in optional:
                optional.append("evidence_ledger")
        # Force required when gates demand producers.
        if run_type in FULL_EVAL_TYPES and signals.get("has_projects"):
            if "project_findings" not in required:
                required.append("project_findings")
            optional = [a for a in optional if a != "project_findings"]
        if self.policy.evidenceVerification.enabled and "evidence_ledger" in defaults:
            if "evidence_ledger" not in required:
                required.append("evidence_ledger")
            optional = [a for a in optional if a != "evidence_ledger"]
        # De-dupe while preserving order.
        required = list(dict.fromkeys(required))
        optional = [a for a in dict.fromkeys(optional) if a not in required]
        return required, optional
    def is_simple(self, run_type: str) -> bool:
        return run_type in SIMPLE_RULE_TYPES

    def capability_catalog(self) -> str:
        lines = []
        for definition in self.registry.list_enabled():
            if definition.agent_id == "CoordinatorAgent":
                continue
            lines.append(
                f"- {definition.agent_id}: {definition.description}"
                f" | capabilities={','.join(definition.capabilities)}"
                f" | requires_artifacts={','.join(definition.requires_artifacts) or '无'}"
                f" | produces={','.join(definition.produces_artifacts) or '无'}"
                f" | cost={definition.cost_hint}"
                f" | optional_when={definition.optional_when or 'always'}"
                f" | terminal={'是' if definition.agent_id in TERMINAL_AGENTS else '否'}")
        return "\n".join(lines)

    def inspect_signals(self, *, resume_text: str = "", job_description: str = "",
                        artifacts: Optional[Dict[str, Any]] = None,
                        shared: Optional[Dict[str, Any]] = None) -> Dict[str, bool]:
        """Deterministic feature gates used by optional_when + skip reasons."""
        artifacts = artifacts or {}
        shared = shared or {}
        # Canonical store first; legacy top-level as fallback.
        # Defensive: specialists may have emitted resumeFacts as a fact-list;
        # never call .get on a non-dict (Evidence/Report production crash).
        resume_raw = artifacts.get("resumeFacts") or shared.get("resumeFacts") or {}
        resume_facts = resume_raw if isinstance(resume_raw, dict) else {}
        parsed = artifacts.get("parsedResume") if isinstance(artifacts.get("parsedResume"), dict) \
            else {}
        projects = (resume_facts.get("projects") or parsed.get("projects")
                    or parsed.get("project_names") or parsed.get("projectNames") or [])
        experiences = (resume_facts.get("experiences") or parsed.get("experiences")
                       or parsed.get("timeline") or [])
        if not projects and isinstance(resume_raw, list):
            projects = resume_raw
        if not experiences and isinstance(resume_raw, list):
            experiences = resume_raw
        text = resume_text or ""
        has_projects = bool(projects) or bool(_PROJECT_HINT.search(text))
        has_timeline = bool(experiences) or bool(_TIMELINE_HINT.search(text))
        has_jd = bool((job_description or "").strip()) \
            or bool(artifacts.get("effectiveJd")) \
            or bool(artifacts.get("jdMatches")) \
            or bool(artifacts.get("jdRequirements") or shared.get("jdRequirements"))
        has_external_urls = bool(_URL_HINT.search(text))
        present = self._present_artifacts(artifacts, shared)
        is_rich_resume = len(text) > 2000 and has_projects and has_timeline
        has_github = bool(re.search(r"github\.com/\w+", text, re.I))
        has_publications = bool(re.search(
            r"(论文|paper|publish|arxiv|conference|journal)", text, re.I))
        is_senior = bool(re.search(
            r"(高级|资深|senior|lead|architect|principal|staff|\d{2,}\s*年)", text, re.I))
        has_management_exp = bool(re.search(
            r"(管理|带.*团队|leader|manager|director|VP|CTO|负责.*人|下属)", text, re.I))
        is_career_changer = bool(re.search(
            r"(转行|转型|跨.*领域|career\s*change|从.*转.*到|非科班)", text, re.I))
        has_certifications = bool(re.search(
            r"(PMP|AWS|CKA|CKAD|CFA|FRM|注册|认证|certified|certification)", text, re.I))
        is_fresh_grad = bool(re.search(
            r"(应届|在读|实习|intern|fresh\s*grad|202[4-7].*毕业)", text, re.I))
        resume_lang = "en" if len(re.findall(r"[a-zA-Z]+", text)) > len(text) * 0.4 else "zh"
        return {
            "has_projects": has_projects,
            "has_timeline": has_timeline,
            "has_jd": has_jd,
            "has_jd_or_match": has_jd or bool(text.strip()),
            "has_jd_requirements": "jd_requirements" in present or has_jd,
            "has_external_urls": has_external_urls,
            "has_github": has_github,
            "has_publications": has_publications,
            "is_rich_resume": is_rich_resume,
            "is_senior": is_senior,
            "has_management_exp": has_management_exp,
            "is_career_changer": is_career_changer,
            "has_certifications": has_certifications,
            "is_fresh_grad": is_fresh_grad,
            "resume_language": resume_lang,
            "evidence_enabled": bool(self.policy.evidenceVerification.enabled),
            "needs_parse": "resume_facts" not in present and bool(text.strip()),
        }

    def plan_from_artifacts(self, *, run_type: str, needs_parse: bool,
                            resume_text: str = "", job_description: str = "",
                            artifacts: Optional[Dict[str, Any]] = None,
                            shared: Optional[Dict[str, Any]] = None
                            ) -> Dict[str, Any]:
        """GOAL_ARTIFACTS → backward-chain → topo → parallel groups."""
        signals = self.inspect_signals(
            resume_text=resume_text, job_description=job_description,
            artifacts=artifacts, shared=shared)
        if needs_parse or signals["needs_parse"]:
            signals = {**signals, "needs_parse": True}
        goal, optional_goal = self.resolve_goal_artifacts(run_type, signals=signals)
        present = self._present_artifacts(artifacts or {}, shared or {})
        selected: List[str] = []
        selected_because: Dict[str, str] = {}
        skipped_because: Dict[str, str] = {}
        artifact_edges: List[Dict[str, str]] = []
        missing = [a for a in goal if a not in present]
        producers_cache: Dict[str, List[AgentDefinition]] = {}

        # Upload path: deterministic parse (+ JD when applicable) before specialists.
        if signals.get("needs_parse") and "ResumeParserAgent" not in selected:
            selected.append("ResumeParserAgent")
            selected_because["ResumeParserAgent"] = "上传后确定性解析简历"
            for art in ("resume_facts", "parsed_resume"):
                artifact_edges.append({
                    "from": "ResumeParserAgent", "artifact": art, "to": "*"})
            present = set(present) | {"resume_facts", "parsed_resume"}
            missing = [a for a in goal if a not in present]

        # Soft-skip optional agents before chaining so we don't pull them in.
        forced_agents = self._forced_agents(run_type, signals, goal)
        for definition in self.registry.list_enabled():
            if definition.agent_id == "CoordinatorAgent":
                continue
            if definition.agent_id in forced_agents:
                continue
            skip = self._optional_skip_reason(definition, signals)
            if skip:
                skipped_because[definition.agent_id] = skip

        stall = 0
        while missing and stall < 24:
            stall += 1
            artifact = missing.pop(0)
            if artifact in present:
                continue
            producers = producers_cache.setdefault(
                artifact, self.registry.producers_of(artifact))
            if not producers:
                skipped_because[f"artifact:{artifact}"] = "无生产者 Agent"
                continue
            # Prefer lowest cost among producers not already skipped.
            candidates = [p for p in producers
                          if p.agent_id not in skipped_because
                          or p.agent_id in forced_agents]
            if not candidates:
                # Required goal with only skipped producers: force cheapest.
                if artifact in goal and producers:
                    chosen = sorted(producers, key=lambda d: _cost_rank(d.cost_hint))[0]
                    skipped_because.pop(chosen.agent_id, None)
                    candidates = [chosen]
                else:
                    continue
            chosen = sorted(candidates, key=lambda d: _cost_rank(d.cost_hint))[0]
            if chosen.agent_id not in selected:
                # Soft requires: only schedule producers whose optional gate passes
                # unless the agent is forced for this run.
                if chosen.agent_id not in forced_agents:
                    skip = self._optional_skip_reason(chosen, signals)
                    if skip:
                        skipped_because[chosen.agent_id] = skip
                        # Optional goal artifacts may be dropped; required stay.
                        if artifact in optional_goal and artifact not in goal:
                            continue
                        if artifact in goal:
                            # Keep chasing via force path below rather than drop.
                            skipped_because.pop(chosen.agent_id, None)
                        else:
                            continue
                selected.append(chosen.agent_id)
                selected_because[chosen.agent_id] = f"产出缺失产物 {artifact}"
                for produced in chosen.produces_artifacts:
                    artifact_edges.append({
                        "from": chosen.agent_id, "artifact": produced, "to": artifact})
                    present = set(present) | {produced}
                for req in chosen.requires_artifacts:
                    if req not in present and req not in missing:
                        missing.append(req)
                        artifact_edges.append({
                            "from": "*", "artifact": req, "to": chosen.agent_id})

        # Evidence soft-requires: when evidence is off, drop EvidenceAgent.
        if not self.policy.evidenceVerification.enabled:
            selected = [a for a in selected if a != "EvidenceAgent"]
            skipped_because.setdefault(
                "EvidenceAgent", "策略关闭证据核验")
        else:
            # Evidence enabled → force EvidenceAgent for goals that need ledger.
            if "evidence_ledger" in goal and "EvidenceAgent" not in selected:
                selected.append("EvidenceAgent")
                selected_because["EvidenceAgent"] = "证据核验启用，强制 EvidenceAgent"
                skipped_because.pop("EvidenceAgent", None)

        # Full evaluation with projects → force ProjectAgent.
        if "ProjectAgent" in forced_agents and "ProjectAgent" not in selected:
            selected.append("ProjectAgent")
            selected_because["ProjectAgent"] = "full evaluation 存在项目，强制 ProjectAgent"
            skipped_because.pop("ProjectAgent", None)

        # agentOrder is ordering preference only — never an allowlist that
        # strips required producers (e.g. low_cost agentOrder must not remove
        # ProjectAgent / EvidenceAgent when they are required for the goal).
        selected = [a for a in selected if self.registry.known(a)]
        if not selected:
            # Absolute last resort.
            selected = self.base_plan(
                run_type, has_resume_facts="resume_facts" in present,
                needs_parse=bool(signals.get("needs_parse")))
            for agent in selected:
                selected_because.setdefault(agent, "artifact planner 空计划 → TASK_PIPELINES fallback")

        finalized = self._finalize(
            selected, "artifact_backward_chain",
            selected_because=selected_because,
            skipped_because=skipped_because,
            artifact_edges=artifact_edges,
            goal_artifacts=goal,
            optional_artifacts=optional_goal)
        return finalized
    async def plan(self, *, run_type: str, user_message: str,
                   conversation_summary: str, shared_digest: str,
                   failure_notes: List[str], memory_notes: List[str],
                   needs_parse: bool,
                   resume_text: str = "", job_description: str = "",
                   artifacts: Optional[Dict[str, Any]] = None,
                   shared: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Full planning entry: artifact chain first, optional LLM refine,
        TASK_PIPELINES only if both fail."""
        base = self.plan_from_artifacts(
            run_type=run_type, needs_parse=needs_parse,
            resume_text=resume_text, job_description=job_description,
            artifacts=artifacts, shared=shared)
        if self.is_simple(run_type) or self.llm is None:
            return base

        from app.runtime import cache

        signals = self.inspect_signals(
            resume_text=resume_text, job_description=job_description,
            artifacts=artifacts, shared=shared)
        self._last_signals = signals
        self._execution_history = [
            n for n in (memory_notes if isinstance(memory_notes, list) else [])
            if isinstance(n, dict) and (n.get("source") == "execution_profile"
                                        or "execution_profile" in str(n.get("structured", {}).get("factKey", "")))]

        # Full evaluations: NEVER cache plans — each candidate deserves a
        # fresh, signal-driven plan. Only cache simple/lightweight run types.
        use_cache = run_type not in FULL_EVAL_TYPES
        cache_key = None
        if use_cache:
            cache_key = cache.content_key(
                "plan", run_type, self.policy.policyId, (user_message or "")[:200],
                ",".join(base["plan"]),
                str(sorted((k, v) for k, v in signals.items() if v)))
            cached = await cache.get_json(cache_key)
            if isinstance(cached, dict) and cached.get("plan"):
                return self._finalize(
                    [str(a) for a in cached["plan"]],
                    str(cached.get("reason", "cached")) + " (cached)",
                    selected_because=cached.get("selectedBecause") or base.get("selectedBecause"),
                    skipped_because=cached.get("skippedBecause") or base.get("skippedBecause"),
                    artifact_edges=cached.get("artifactEdges") or base.get("artifactEdges"),
                    goal_artifacts=base.get("goalArtifacts"),
                    optional_artifacts=base.get("optionalArtifacts"))

        refined = await self._refine(
            base["plan"], run_type=run_type, user_message=user_message,
            conversation_summary=conversation_summary, shared_digest=shared_digest,
            failure_notes=failure_notes, memory_notes=memory_notes,
            goal_artifacts=list(base.get("goalArtifacts") or []),
            signals=signals)
        if not refined["plan"]:
            fallback = self.base_plan(
                run_type, has_resume_facts=False, needs_parse=needs_parse)
            return self._finalize(
                fallback, "safety_fallback(TASK_PIPELINES)",
                selected_because={a: "planner+LLM 均失败" for a in fallback},
                skipped_because=base.get("skippedBecause") or {},
                artifact_edges=base.get("artifactEdges") or [],
                goal_artifacts=base.get("goalArtifacts"),
                optional_artifacts=base.get("optionalArtifacts"))
        if use_cache and cache_key and not refined["reason"].startswith("rule_based(llm-error"):
            await cache.set_json(cache_key, {
                "plan": refined["plan"], "reason": refined["reason"],
                "selectedBecause": base.get("selectedBecause"),
                "skippedBecause": base.get("skippedBecause"),
                "artifactEdges": base.get("artifactEdges"),
            }, cache.TTL_PLAN)
        return self._finalize(
            refined["plan"], refined["reason"],
            selected_because=base.get("selectedBecause"),
            skipped_because=base.get("skippedBecause"),
            artifact_edges=base.get("artifactEdges"),
            goal_artifacts=base.get("goalArtifacts"),
            optional_artifacts=base.get("optionalArtifacts"))

    def _finalize(self, plan: List[str], reason: str,
                  selected_because: Optional[Dict[str, str]] = None,
                  skipped_because: Optional[Dict[str, str]] = None,
                  artifact_edges: Optional[List[Dict[str, str]]] = None,
                  goal_artifacts: Optional[List[str]] = None,
                  optional_artifacts: Optional[List[str]] = None) -> Dict[str, Any]:
        ordered = self._order_by_dependencies(plan)
        ordered = self._ensure_unique_terminal(ordered)
        # Plan-time closure: ensure every required goal artifact has a producer
        # still in the plan (or mark it missing — never silently drop).
        goal = list(goal_artifacts or [])
        missing_producers = self._missing_goal_producers(ordered, goal)
        if missing_producers:
            repaired = list(ordered)
            for artifact, producer_id in missing_producers.items():
                if producer_id and producer_id not in repaired:
                    if repaired and repaired[-1] in TERMINAL_AGENTS:
                        repaired.insert(-1, producer_id)
                    else:
                        repaired.append(producer_id)
                    if selected_because is not None:
                        selected_because[producer_id] = (
                            f"closure 补齐 goal artifact {artifact} 的唯一生产者")
            ordered = self._ensure_unique_terminal(self._order_by_dependencies(repaired))
        still_missing = [
            a for a in goal
            if a not in self._producible_artifacts(ordered)
        ]
        groups = self._parallel_groups(ordered)
        terminal = next((a for a in reversed(ordered) if a in TERMINAL_AGENTS),
                        "ReportAgent")
        budget = self._budget_plan(
            ordered, terminal,
            execution_history=getattr(self, "_execution_history", None),
            signals=getattr(self, "_last_signals", None))
        return {
            "plan": ordered,
            "reason": reason,
            "parallelGroups": groups,
            "requiredTerminalAgent": terminal,
            "dependencies": {a: AGENT_DEPENDENCIES.get(a, []) for a in ordered},
            "budgetPlan": budget,
            "budget": budget,
            "selectedBecause": selected_because or {
                a: reason for a in ordered},
            "skippedBecause": skipped_because or {},
            "artifactEdges": artifact_edges or [],
            "goalArtifacts": goal,
            "optionalArtifacts": list(optional_artifacts or []),
            "missingGoalArtifacts": still_missing,
            "planClosureOk": not still_missing,
        }
    def _budget_plan(self, ordered: List[str], terminal: str,
                     execution_history: Optional[List[Dict[str, Any]]] = None,
                     signals: Optional[Dict[str, Any]] = None) -> Dict[str, Dict[str, int]]:
        """Adaptive budget allocation driven by historical execution profiles.
        Falls back to even split when no history is available."""
        sig = signals or {}
        base_llm = self.policy.maxLlmCalls
        if sig.get("is_rich_resume"):
            base_llm = min(base_llm + 3, 18)
        elif sig.get("is_fresh_grad") or not sig.get("has_projects"):
            base_llm = max(8, base_llm - 2)

        terminal_floor = 2
        others = [a for a in ordered if a != terminal]

        hist_ratios = self._extract_budget_ratios(execution_history, others)
        remaining_llm = max(1, base_llm - terminal_floor)

        budget: Dict[str, Dict[str, int]] = {}
        if hist_ratios:
            for agent in others:
                ratio = hist_ratios.get(agent, 1.0 / max(1, len(others)))
                budget[agent] = {
                    "llmQuota": max(1, round(remaining_llm * ratio)),
                    "toolQuota": min(
                        max(1, self.policy.toolBudget.maxToolCallsPerRun
                            // max(1, len(ordered))),
                        self.policy.toolBudget.maxToolCallsPerAgent),
                }
        else:
            per_agent_llm = max(1, remaining_llm // max(1, len(others)))
            per_agent_tools = max(1, self.policy.toolBudget.maxToolCallsPerRun
                                  // max(1, len(ordered)))
            for agent in others:
                budget[agent] = {
                    "llmQuota": per_agent_llm,
                    "toolQuota": min(per_agent_tools,
                                     self.policy.toolBudget.maxToolCallsPerAgent),
                }
        budget[terminal] = {
            "llmQuota": terminal_floor,
            "toolQuota": min(
                max(1, self.policy.toolBudget.maxToolCallsPerRun // max(1, len(ordered))),
                self.policy.toolBudget.maxToolCallsPerAgent),
        }
        return budget

    @staticmethod
    def _extract_budget_ratios(execution_history: Optional[List[Dict[str, Any]]],
                               agents: List[str]) -> Optional[Dict[str, float]]:
        """Derive per-agent budget ratio from historical execution profiles."""
        if not execution_history:
            return None
        agent_totals: Dict[str, int] = {}
        count = 0
        for profile in execution_history[-5:]:
            structured = profile.get("structured") or profile
            agent_llm = structured.get("agentLlmCalls") or {}
            if not agent_llm:
                continue
            count += 1
            for agent, calls in agent_llm.items():
                agent_totals[agent] = agent_totals.get(agent, 0) + int(calls or 0)
        if count == 0 or not agent_totals:
            return None
        total = sum(agent_totals.values()) or 1
        return {a: agent_totals.get(a, 1) / total for a in agents}

    def _order_by_dependencies(self, plan: List[str]) -> List[str]:
        """Stable topological pass: an agent is scheduled only after every
        planned dependency; unknown agents are dropped.

        ``policy.agentOrder`` is a soft preference among ready agents — never
        an allowlist that removes required producers from the plan.
        """
        plan = [a for a in dict.fromkeys(plan) if self.registry.known(a)]
        order_rank = {name: i for i, name in enumerate(self.policy.agentOrder or [])}
        placed: List[str] = []
        remaining = list(plan)
        stall_guard = 0
        while remaining and stall_guard <= len(plan) * 2:
            stall_guard += 1
            ready = []
            for agent in remaining:
                deps = [d for d in AGENT_DEPENDENCIES.get(agent, []) if d in plan]
                if all(d in placed for d in deps):
                    ready.append(agent)
            if not ready:
                placed.extend(remaining)  # cycle fallback: keep original order
                break
            ready.sort(key=lambda a: (order_rank.get(a, 10_000), remaining.index(a)))
            chosen = ready[0]
            placed.append(chosen)
            remaining.remove(chosen)
        return self._ensure_unique_terminal(placed[: max(1, self.policy.maxAgentCount) + 1])

    def _prefer_agent_order(self, plan: List[str]) -> List[str]:
        """Documentation helper / tests: soft preference without dropping agents.
        Runtime ordering uses agentOrder ranks inside ``_order_by_dependencies``.
        """
        if not self.policy.agentOrder:
            return plan
        body = [a for a in plan if a not in TERMINAL_AGENTS]
        terminals = [a for a in plan if a in TERMINAL_AGENTS]
        preferred = [a for a in self.policy.agentOrder if a in body]
        rest = [a for a in body if a not in preferred]
        return preferred + rest + terminals    @staticmethod
    @staticmethod
    def _ensure_unique_terminal(plan: List[str]) -> List[str]:
        """Exactly one of Report/Optimize/Interview closes the plan."""
        body = [a for a in plan if a not in TERMINAL_AGENTS]
        terminals = [a for a in plan if a in TERMINAL_AGENTS]
        if not terminals:
            body.append("ReportAgent")
        else:
            # Prefer the last requested terminal; drop earlier duplicates.
            body.append(terminals[-1])
        return body

    def _parallel_groups(self, ordered: List[str]) -> List[List[str]]:
        groups: List[List[str]] = []
        for agent in ordered:
            deps = set(AGENT_DEPENDENCIES.get(agent, [])) & set(ordered)
            can_join = (
                groups
                and agent in PARALLELIZABLE
                and all(a in PARALLELIZABLE for a in groups[-1])
                and self.policy.parallelSpecialists
                and not (deps & set(groups[-1]))
            )
            if can_join:
                groups[-1].append(agent)
            else:
                groups.append([agent])
        return groups

    async def _refine(self, base_plan: List[str], *, run_type: str,
                      user_message: str, conversation_summary: str,
                      shared_digest: str, failure_notes: List[str],
                      memory_notes: List[str],
                      goal_artifacts: Optional[List[str]] = None,
                      signals: Optional[Dict[str, bool]] = None) -> Dict[str, Any]:
        sig = signals or {}
        signal_lines = []
        if sig.get("is_rich_resume"):
            signal_lines.append("简历丰富（>2000字，有项目+时间线），建议完整Agent覆盖+深度验证")
        elif sig.get("is_fresh_grad"):
            signal_lines.append("应届生/实习生，精简路径：跳过深度验证，重点考察潜力和学习能力")
        if sig.get("has_github"):
            signal_lines.append("有 GitHub 链接，EvidenceAgent 必须使用 MCP fetch 验证代码贡献")
        if sig.get("has_publications"):
            signal_lines.append("有论文/出版物，TechAgent 应深入评估学术能力和研究方向")
        if sig.get("is_senior"):
            signal_lines.append("资深候选人（10年+），RiskAgent 重点关注架构决策和团队管理证据")
        if sig.get("has_management_exp"):
            signal_lines.append("有管理经验，评估应包含团队规模、管理方法论和跨部门协作能力")
        if sig.get("is_career_changer"):
            signal_lines.append("跨领域转型候选人，JDAnalysisAgent 需额外评估能力迁移可行性")
        if sig.get("has_certifications"):
            signal_lines.append("有行业认证，TechAgent 需验证认证有效性并作为加分项")
        if not sig.get("has_projects"):
            signal_lines.append("无明显项目经历，可跳过 ProjectAgent，预算分配给其他Agent")
        if not sig.get("has_timeline"):
            signal_lines.append("无清晰时间线，RiskAgent 时间线核查优先级降低")
        if sig.get("has_external_urls"):
            signal_lines.append("有外部链接，EvidenceAgent 应使用 MCP 验证可公开验证的声明")
        if sig.get("resume_language") == "en":
            signal_lines.append("英文简历，评估标准切换为国际化口径")

        prompt_user = (
            f"任务类别: {run_type}\n用户问题: {user_message[:600]}\n"
            f"会话摘要: {(conversation_summary or '')[:400]}\n"
            f"共享状态摘要: {shared_digest[:800]}\n"
            f"候选人信号:\n" + "\n".join(f"  - {s}" for s in signal_lines) + "\n"
            f"历史失败提示: {'; '.join(failure_notes[:3]) or '无'}\n"
            f"相关记忆: {'; '.join(memory_notes[:3]) or '无'}\n"
            f"可用 Agent 能力目录:\n{self.capability_catalog()}\n"
            f"产物规划基线: {base_plan}\n"
            f"必选 goal artifacts: {goal_artifacts or []}\n"
            f"预算: 最多 {self.policy.maxAgentCount} 个 Agent, "
            f"{self.policy.maxLlmCalls} 次 LLM 调用\n\n"
            "你是动态规划 Coordinator。根据候选人特征做出差异化决策：\n"
            "1. 丰富简历（>2000字+项目+多年经验）：完整流水线+深度验证\n"
            "2. 薄简历（<800字/应届/缺少项目）：精简路径，跳过ProjectAgent\n"
            "3. 有GitHub/外部URL：EvidenceAgent必含+MCP工具优先\n"
            "4. 有论文/出版物：TechAgent需深入学术评估\n"
            "5. 资深候选人（10年+/总监级）：RiskAgent重点关注管理&架构证据\n"
            "6. 跨领域转型：增加JDAnalysisAgent权重,评估能力迁移\n\n"
            "关键约束：\n"
            "- 不得删除goal artifact的唯一生产者\n"
            "- 最后一个必须是唯一terminal Agent\n"
            "- 你的计划必须体现此候选人的独特性,不是固定模板\n"
            "输出 JSON {\"plan\": [...], \"reason\": \"基于[具体信号]选择了[具体策略]\"}")
        try:
            from app.runtime.prompts import default_prompt_manager

            system = default_prompt_manager.system_for_agent("CoordinatorAgent").content
            raw = await self.llm.chat(
                [{"role": "system", "content": system},
                 {"role": "user", "content": prompt_user}],
                agent_id="CoordinatorAgent", purpose="plan", max_tokens=400)
            parsed = extract_json_object(raw)
            plan = [str(a) for a in parsed.get("plan", []) if self.registry.known(str(a))]
            if not plan:
                return {"plan": base_plan, "reason": "rule_based(llm-empty)"}
            plan = self._protect_required_producers(
                plan, base_plan, goal_artifacts or [])
            return {"plan": plan,
                    "reason": str(parsed.get("reason", "llm_refined"))[:200]}
        except Exception as exc:  # noqa: BLE001 - planning must not kill the run
            code = getattr(exc, "code", type(exc).__name__)
            logger.warning("coordinator refine failed (%s), using artifact plan: %s", code, exc)
            return {"plan": base_plan,
                    "reason": f"artifact_planned(refine_skipped:{code})"}

    def _protect_required_producers(self, refined: List[str], base: List[str],
                                    goal_artifacts: List[str]) -> List[str]:
        """LLM refine may add/reorder optional agents, but must not remove the
        sole producer of any required goal artifact that was in the base plan."""
        result = [a for a in dict.fromkeys(refined) if self.registry.known(a)]
        for artifact in goal_artifacts:
            producers = {p.agent_id for p in self.registry.producers_of(artifact)}
            if not producers:
                continue
            base_hits = [a for a in base if a in producers]
            refined_hits = [a for a in result if a in producers]
            if base_hits and not refined_hits:
                # Restore the sole (or first) producer from the base plan.
                restore = base_hits[0]
                if result and result[-1] in TERMINAL_AGENTS:
                    result.insert(-1, restore)
                else:
                    result.append(restore)
                logger.info(
                    "refine restored sole producer %s for goal artifact %s",
                    restore, artifact)
        return result

    @staticmethod
    def _forced_agents(run_type: str, signals: Dict[str, bool],
                       goal: List[str]) -> Set[str]:
        forced: Set[str] = set()
        if run_type in FULL_EVAL_TYPES and signals.get("has_projects") \
                and "project_findings" in goal:
            forced.add("ProjectAgent")
        if signals.get("evidence_enabled") and "evidence_ledger" in goal:
            forced.add("EvidenceAgent")
        return forced

    def _producible_artifacts(self, plan: List[str]) -> Set[str]:
        present: Set[str] = set()
        for agent_id in plan:
            if not self.registry.known(agent_id):
                continue
            for art in self.registry.get(agent_id).produces_artifacts:
                present.add(art)
        return present

    def _missing_goal_producers(self, plan: List[str],
                                goal_artifacts: List[str]) -> Dict[str, str]:
        """Map missing required artifacts → a producer id to insert (if any)."""
        producible = self._producible_artifacts(plan)
        missing: Dict[str, str] = {}
        for artifact in goal_artifacts:
            if artifact in producible:
                continue
            producers = self.registry.producers_of(artifact)
            missing[artifact] = producers[0].agent_id if producers else ""
        return missing

    def replan_after_failure(self, remaining: List[str], failed_agent: str) -> List[str]:
        """Failure handling: keep partial results, drop the failed step,
        guarantee a terminal agent still closes the run."""
        plan = [a for a in remaining if a != failed_agent]
        return self._ensure_unique_terminal(plan)

    async def adaptive_replan(self, *, remaining: List[str], executed: List[str],
                              shared_digest: str, trigger: str,
                              failure_notes: List[str],
                              missing_artifacts: Optional[List[str]] = None,
                              handoff_to: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Mid-run replanning. Triggers include missing_required_artifact /
        tool_failed / new_conflict / handoff_requested (not only confidence)."""
        trigger_kind = trigger.split(":", 1)[0]
        if trigger_kind not in REPLAN_TRIGGERS and not trigger.startswith("new_conflict"):
            # Accept namespaced forms like new_conflicts:2 / low_confidence:0.4
            if not any(trigger.startswith(t) for t in REPLAN_TRIGGERS):
                logger.info("adaptive replan ignored unknown trigger=%s", trigger)
                return None
        if self.llm is None or not remaining:
            # Deterministic handoff insertion without LLM.
            if handoff_to and handoff_to not in executed and self.registry.known(handoff_to):
                if handoff_to in executed:
                    return None  # handoff 去环：已执行过的目标拒绝
                new_plan = [a for a in remaining if a != handoff_to]
                # Insert handoff before terminal.
                terminals = [a for a in new_plan if a in TERMINAL_AGENTS]
                body = [a for a in new_plan if a not in TERMINAL_AGENTS]
                body.append(handoff_to)
                body.extend(terminals)
                return self._finalize(
                    body, f"replan({trigger})",
                    selected_because={handoff_to: f"handoff_requested:{trigger}"},
                    skipped_because={},
                    artifact_edges=[],
                    goal_artifacts=missing_artifacts or [])
            if trigger_kind == "missing_required_artifact" and missing_artifacts:
                patched = list(remaining)
                for artifact in missing_artifacts:
                    for producer in self.registry.producers_of(artifact):
                        if producer.agent_id not in executed and producer.agent_id not in patched:
                            # Insert before terminal
                            if patched and patched[-1] in TERMINAL_AGENTS:
                                patched.insert(-1, producer.agent_id)
                            else:
                                patched.append(producer.agent_id)
                            break
                if patched != remaining:
                    return self._finalize(
                        patched, f"replan({trigger})",
                        selected_because={a: f"补齐产物 {missing_artifacts}"
                                          for a in patched if a not in remaining},
                        skipped_because={}, artifact_edges=[],
                        goal_artifacts=missing_artifacts)
            return None

        prompt_user = (
            f"运行中触发重规划，原因: {trigger}\n"
            f"已完成 Agent: {executed}\n"
            f"剩余计划: {remaining}\n"
            f"缺失产物: {missing_artifacts or []}\n"
            f"handoff 目标: {handoff_to or '无'}\n"
            f"共享状态摘要: {shared_digest[:800]}\n"
            f"失败记录: {'; '.join(failure_notes[-3:]) or '无'}\n"
            f"可用 Agent 能力目录:\n{self.capability_catalog()}\n"
            "只允许调整剩余部分（不得包含已完成 Agent，handoff 不得形成环），"
            "必须满足 requires_artifacts，最后一个必须是唯一 terminal Agent。"
            "若当前剩余计划已合理，原样返回。"
            "输出 json {\"plan\": [...], \"reason\": \"...\"}")
        try:
            from app.runtime.prompts import default_prompt_manager

            system = default_prompt_manager.system_for_agent("CoordinatorAgent").content
            raw = await self.llm.chat(
                [{"role": "system", "content": system},
                 {"role": "user", "content": prompt_user}],
                agent_id="CoordinatorAgent", purpose="replan", max_tokens=300)
            parsed = extract_json_object(raw)
            plan = [str(a) for a in parsed.get("plan", [])
                    if self.registry.known(str(a)) and str(a) not in executed]
            # Handoff 去环：拒绝把已执行 Agent 再插回。
            if handoff_to and handoff_to in executed:
                plan = [a for a in plan if a != handoff_to]
            if not plan or plan == remaining:
                return None
            finalized = self._finalize(plan, f"replan({trigger})")
            finalized["reason"] = str(parsed.get("reason", finalized["reason"]))[:200]
            return finalized
        except Exception as exc:  # noqa: BLE001 - replanning must not kill the run
            logger.info("adaptive replan skipped (%s): %s", trigger, exc)
            return None

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _present_artifacts(artifacts: Dict[str, Any],
                           shared: Dict[str, Any]) -> Set[str]:
        present: Set[str] = set()
        # Prefer canonical artifacts; fall back to legacy top-level mirrors.
        store = artifacts if isinstance(artifacts, dict) else {}
        legacy = shared if isinstance(shared, dict) else {}
        mapping = {
            "parsedResume": "parsed_resume",
            "resumeFacts": "resume_facts",
            "jdCoverage": "technical_findings",
            "timelineCheck": "risks",
            "effectiveJd": "jd_requirements",
            "jdMatches": "jd_requirements",
            "jdRequirements": "jd_requirements",
            "technicalFindings": "technical_findings",
            "projectFindings": "project_findings",
            "risks": "risks",
            "evidence": "evidence_ledger",
            "finalReport": "final_report",
        }
        for key, artifact in mapping.items():
            value = store.get(key)
            if value is None or value == {} or value == []:
                value = legacy.get(key)
            if value:
                present.add(artifact)
        return present

    @staticmethod
    def _optional_skip_reason(definition: AgentDefinition,
                              signals: Dict[str, bool]) -> str:
        gate = definition.optional_when or ""
        if not gate:
            return ""
        if gate == "has_projects" and not signals.get("has_projects"):
            return "简历无项目经历，跳过 ProjectAgent"
        if gate == "has_timeline" and not signals.get("has_timeline"):
            return "简历无时间线/工作经历，跳过 RiskAgent"
        if gate == "has_jd" and not signals.get("has_jd"):
            return "无 JD 文本/URL，跳过"
        if gate == "has_jd_or_match" and not signals.get("has_jd_or_match"):
            return "无简历可匹配，跳过 JDAnalysis"
        if gate == "has_jd_requirements" and not signals.get("has_jd_requirements"):
            return "无岗位要求，跳过 TechAgent"
        if gate == "evidence_enabled" and not signals.get("evidence_enabled"):
            return "策略关闭证据核验"
        return ""


def _cost_rank(hint: str) -> int:
    return {"low": 0, "medium": 1, "high": 2}.get((hint or "medium").lower(), 1)
