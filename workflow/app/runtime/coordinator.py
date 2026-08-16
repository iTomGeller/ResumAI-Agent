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
    "full_evaluation": ["TechAgent", "ProjectAgent", "RiskAgent", "ReportAgent"],
    "jd_evaluation": ["TechAgent", "ProjectAgent", "RiskAgent", "ReportAgent"],
    "tech_match": ["TechAgent", "ReportAgent"],
    "project_analysis": ["ProjectAgent", "ReportAgent"],
    "risk_check": ["RiskAgent", "ReportAgent"],
    "timeline_check": ["RiskAgent", "ReportAgent"],
    "evidence_check": ["ReportAgent"],
    "jd_gap": ["TechAgent", "ReportAgent"],
    "project_rewrite": ["ProjectAgent", "ReportAgent"],
    "resume_optimize": ["ProjectAgent", "ReportAgent"],
    "interview_questions": ["RiskAgent", "ReportAgent"],
    "backend_eval": ["TechAgent", "ProjectAgent", "RiskAgent", "ReportAgent"],
    "agent_eval": ["TechAgent", "ProjectAgent", "RiskAgent", "ReportAgent"],
    # followup remains a lightweight evaluation refinement — chat uses
    # /conversation/reply (CopilotAnswer), never this pipeline.
    "followup": ["ReportAgent"],
}

# Goal → required terminal artifacts. Planner backward-chains from these.
GOAL_ARTIFACTS: Dict[str, List[str]] = {
    "full_evaluation": [
        "resume_facts", "jd_requirements", "technical_findings",
        "project_findings", "risks", "final_report"],
    "jd_evaluation": [
        "resume_facts", "jd_requirements", "technical_findings",
        "project_findings", "risks", "final_report"],
    "backend_eval": [
        "resume_facts", "jd_requirements", "technical_findings",
        "project_findings", "risks", "final_report"],
    "agent_eval": [
        "resume_facts", "jd_requirements", "technical_findings",
        "project_findings", "risks", "final_report"],
    "tech_match": [
        "resume_facts", "jd_requirements", "technical_findings",
        "final_report"],
    "project_analysis": [
        "resume_facts", "project_findings", "final_report"],
    "risk_check": ["resume_facts", "risks", "final_report"],
    "timeline_check": ["resume_facts", "risks", "final_report"],
    "evidence_check": ["final_report"],
    "jd_gap": [
        "resume_facts", "jd_requirements", "technical_findings",
        "final_report"],
    "project_rewrite": ["resume_facts", "project_findings", "final_report"],
    "resume_optimize": ["resume_facts", "project_findings", "final_report"],
    "interview_questions": ["resume_facts", "risks", "final_report"],
    "followup": ["final_report"],
}

TERMINAL_AGENTS = {"ReportAgent"}
FULL_EVAL_TYPES = {
    "full_evaluation", "jd_evaluation", "backend_eval", "agent_eval",
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
    "TechAgent": [],
    "ProjectAgent": [],
    "RiskAgent": [],
    "ReportAgent": ["TechAgent", "ProjectAgent", "RiskAgent"],
}

PARALLELIZABLE = {"TechAgent", "ProjectAgent", "RiskAgent"}

# Logical planner artifact -> canonical SharedState keys.  The reverse aliases
# let the Java control plane name invalidations in either representation.
ARTIFACT_STATE_KEYS: Dict[str, Tuple[str, ...]] = {
    "parsed_resume": ("parsedResume",),
    "resume_facts": ("resumeFacts",),
    "jd_requirements": ("jdRequirements", "effectiveJd", "jdMatches", "jdFocus"),
    "technical_findings": ("technicalFindings", "jdCoverage"),
    "project_findings": ("projectFindings",),
    "risks": ("risks", "timelineCheck"),
    "final_report": ("finalReport",),
    "rewrite": ("rewrite",),
    "interview_questions": ("interviewQuestions",),
}
_ARTIFACT_ALIASES = {
    alias: logical
    for logical, aliases in ARTIFACT_STATE_KEYS.items()
    for alias in (logical, *aliases)
}
# Transitive data dependencies used when a new conversation revision changes
# an input or evaluation focus. ReportAgent intentionally has soft runtime
# dependencies, so this graph makes the otherwise implicit dependencies
# explicit for invalidation.
ARTIFACT_INVALIDATION_GRAPH: Dict[str, Tuple[str, ...]] = {
    "parsed_resume": ("resume_facts",),
    "resume_facts": (
        "jd_requirements", "technical_findings", "project_findings", "risks",
        "final_report", "rewrite", "interview_questions",
    ),
    "jd_requirements": (
        "technical_findings", "project_findings", "final_report",
    ),
    "technical_findings": ("final_report",),
    "project_findings": ("final_report", "rewrite"),
    "risks": ("final_report", "interview_questions"),
}

_PROJECT_HINT = re.compile(
    r"(项目经历|项目经验|project\s*experience|side\s*project|"
    r"个人项目|开源项目|项目名称|项目(?:[一二三四五六七八九十0-9]+)?\s*[:：]|"
    r"github\.com/)", re.I)
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

    Contract: the initial plan contains at most one terminal agent and remains
    immutable for the run. Runtime failures are handled by bounded LLM/tool
    retries and evidence-aware report degradation, not mid-run replanning.
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
        if not signals.get("has_jd") and "jd_requirements" in required:
            required = [a for a in required if a != "jd_requirements"]
            if "jd_requirements" not in optional:
                optional.append("jd_requirements")
        # Force required when gates demand producers.
        if (run_type in FULL_EVAL_TYPES
                and signals.get("has_projects")
                and not signals.get("is_sparse_resume")):
            if "project_findings" not in required:
                required.append("project_findings")
            optional = [a for a in optional if a != "project_findings"]
        if signals.get("is_sparse_resume") and run_type in FULL_EVAL_TYPES:
            # Keep the multi-agent review surface even for a short resume, but
            # only activate dimensions supported by input signals. Latency is
            # controlled by parallel single-pass execution, not by deleting
            # Skills/agents from the evaluation.
            core = {
                "resume_facts", "jd_requirements", "technical_findings",
                "final_report",
            }
            if signals.get("has_projects"):
                core.add("project_findings")
            if signals.get("has_timeline"):
                core.add("risks")
            dropped = [artifact for artifact in required if artifact not in core]
            required = [artifact for artifact in required if artifact in core]
            optional.extend(dropped)
        # De-dupe while preserving order.
        required = list(dict.fromkeys(required))
        optional = [a for a in dict.fromkeys(optional) if a not in required]
        return required, optional

    @staticmethod
    def expand_invalidated_artifacts(names: List[str]) -> Set[str]:
        """Normalize and transitively expand revision invalidations."""
        pending = [
            _ARTIFACT_ALIASES.get(str(name or "").strip(),
                                  str(name or "").strip())
            for name in (names or [])
            if str(name or "").strip()
        ]
        expanded: Set[str] = set()
        while pending:
            artifact = pending.pop(0)
            if artifact in expanded:
                continue
            expanded.add(artifact)
            pending.extend(ARTIFACT_INVALIDATION_GRAPH.get(artifact, ()))
        return expanded

    @staticmethod
    def reusable_artifacts(artifacts: Dict[str, Any],
                           invalidated: List[str]) -> Tuple[Dict[str, Any], Set[str]]:
        """Return a copy containing only artifacts safe for a new revision."""
        expanded = Coordinator.expand_invalidated_artifacts(invalidated)
        reusable: Dict[str, Any] = {}
        for key, value in (artifacts or {}).items():
            logical = _ARTIFACT_ALIASES.get(str(key), str(key))
            if logical not in expanded:
                reusable[str(key)] = value
        return reusable, expanded

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
        # A short resume cannot support separate project/risk/public-evidence
        # investigations. Treat it as a fast, evidence-limited assessment;
        # callers can still request those deep modes explicitly.
        # Keep explicit contract tests and structured multi-section resumes out
        # of this gate; the fast path targets a real uploaded fragment, not an
        # empty placeholder.  120–800 chars with fewer than seven populated
        # lines is the practical “too thin to fan out” range.
        populated_lines = sum(1 for line in text.splitlines() if line.strip())
        is_sparse_resume = (
            120 <= len(text.strip()) < 800
            and not (populated_lines >= 7 and has_projects and has_timeline))
        present = self._present_artifacts(artifacts, shared)
        is_rich_resume = len(text) > 2000 and has_projects and has_timeline
        has_github = bool(re.search(r"github\.com/\w+", text, re.I))
        stack_text = f"{text}\n{job_description or ''}"
        has_microsoft_stack = bool(re.search(
            r"(?:\.NET|ASP\.NET|C#|Azure|PowerShell|Microsoft\s+365|Dynamics\s+365|MSSQL|SQL\s+Server)",
            stack_text, re.I))
        has_framework_stack = bool(re.search(
            r"(?:Spring(?:\s*Boot)?|React|Vue|Angular|Next\.js|Nuxt|Kubernetes|K8s|Kafka|"
            r"LangChain|LangGraph|FastAPI|Django|Flask|PyTorch|TensorFlow|Redis|MyBatis|Hibernate)",
            stack_text, re.I)) and not has_microsoft_stack
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
            "is_sparse_resume": is_sparse_resume,
            "has_github": has_github,
            "has_framework_stack": has_framework_stack,
            "has_microsoft_stack": has_microsoft_stack,
            "has_publications": has_publications,
            "is_rich_resume": is_rich_resume,
            "is_senior": is_senior,
            "has_management_exp": has_management_exp,
            "is_career_changer": is_career_changer,
            "has_certifications": has_certifications,
            "is_fresh_grad": is_fresh_grad,
            "resume_language": resume_lang,
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
        signals["single_pass_evaluation"] = run_type in FULL_EVAL_TYPES
        if needs_parse or signals["needs_parse"]:
            signals = {**signals, "needs_parse": True}
        goal, optional_goal = self.resolve_goal_artifacts(run_type, signals=signals)
        self._last_signals = signals
        present = self._present_artifacts(artifacts or {}, shared or {})
        initially_present = set(present)
        selected: List[str] = []
        selected_because: Dict[str, str] = {}
        skipped_because: Dict[str, str] = {}
        artifact_edges: List[Dict[str, str]] = []
        missing = [a for a in goal if a not in present]
        producers_cache: Dict[str, List[AgentDefinition]] = {}

        # Upload parsing is deterministic preflight, not an LLM Agent.
        if signals.get("needs_parse"):
            for art in ("resume_facts", "parsed_resume"):
                artifact_edges.append({
                    "from": "preflight.parse_resume", "artifact": art, "to": "*"})
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

        # Project-bearing full evaluations and project-focused runs must keep
        # ProjectAgent. This is a hard goal constraint, not an LLM/budget hint:
        # a user who explicitly asks for project depth must not get a plan that
        # silently substitutes TechAgent for the project_findings producer.
        if ("ProjectAgent" in forced_agents
                and "project_findings" not in initially_present
                and "ProjectAgent" not in selected):
            selected.append("ProjectAgent")
            selected_because["ProjectAgent"] = (
                f"{run_type} 检测到项目证据，强制产出 project_findings")
            skipped_because.pop("ProjectAgent", None)

        # agentOrder is ordering preference only — never an allowlist that
        # strips required producers (e.g. low_cost agentOrder must not remove
        # ProjectAgent when it is required for the goal).
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
            optional_artifacts=optional_goal,
            present_artifacts=initially_present)
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
        signals = self.inspect_signals(
            resume_text=resume_text, job_description=job_description,
            artifacts=artifacts, shared=shared)
        signals["single_pass_evaluation"] = run_type in FULL_EVAL_TYPES
        self._last_signals = signals
        self._business_memory = [
            note for note in (memory_notes if isinstance(memory_notes, list) else [])
            if isinstance(note, dict)
            and str(note.get("type") or note.get("memoryType") or "")
            in {"RECENT_CASE", "JOB_PROFILE"}
        ]
        # Runtime counters belong to telemetry, not business Memory. Business
        # memories influence priority through evidence/project/risk signals.
        self._execution_history = []
        for note in self._business_memory:
            structured = note.get("structuredContent") or note.get("structured") or {}
            if not isinstance(structured, dict):
                continue
            if structured.get("observedJdGaps") \
                    or structured.get("commonGaps") \
                    or structured.get("jdGaps"):
                signals["memory_project_gaps"] = True
            if structured.get("riskPatterns") \
                    or structured.get("observedRiskPatterns") \
                    or structured.get("commonRiskPatterns"):
                signals["memory_risk_patterns"] = True

        if self._business_memory:
            base = self._finalize(
                list(base.get("plan") or []),
                str(base.get("reason") or "artifact_backward_chain"),
                selected_because=base.get("selectedBecause"),
                skipped_because=base.get("skippedBecause"),
                artifact_edges=base.get("artifactEdges"),
                goal_artifacts=base.get("goalArtifacts"),
                optional_artifacts=base.get("optionalArtifacts"),
                present_artifacts=set(base.get("presentArtifacts") or []),
            )

        # Evaluation routing is deterministic and signal-driven. Spending a
        # coordinator provider call to restate the artifact plan adds latency
        # without improving the evidence contract.
        if (self.is_simple(run_type) or self.llm is None
                or run_type in FULL_EVAL_TYPES):
            return base

        from app.runtime import cache

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
                    optional_artifacts=base.get("optionalArtifacts"),
                    present_artifacts=set(base.get("presentArtifacts") or []))

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
                optional_artifacts=base.get("optionalArtifacts"),
                present_artifacts=set(base.get("presentArtifacts") or []))
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
            optional_artifacts=base.get("optionalArtifacts"),
            present_artifacts=set(base.get("presentArtifacts") or []))

    def _finalize(self, plan: List[str], reason: str,
                  selected_because: Optional[Dict[str, str]] = None,
                  skipped_because: Optional[Dict[str, str]] = None,
                  artifact_edges: Optional[List[Dict[str, str]]] = None,
                  goal_artifacts: Optional[List[str]] = None,
                  optional_artifacts: Optional[List[str]] = None,
                  present_artifacts: Optional[Set[str]] = None) -> Dict[str, Any]:
        ordered = self._order_by_dependencies(plan)
        ordered = self._ensure_unique_terminal(ordered)
        # Plan-time closure: ensure every required goal artifact has a producer
        # still in the plan (or mark it missing — never silently drop).
        goal = list(goal_artifacts or [])
        present = set(present_artifacts or set())
        closure_goal = [artifact for artifact in goal if artifact not in present]
        missing_producers = self._missing_goal_producers(ordered, closure_goal)
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
            a for a in closure_goal
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
            "presentArtifacts": sorted(present),
            "missingGoalArtifacts": still_missing,
            "planClosureOk": not still_missing,
        }
    def _budget_plan(self, ordered: List[str], terminal: str,
                     execution_history: Optional[List[Dict[str, Any]]] = None,
                     signals: Optional[Dict[str, Any]] = None) -> Dict[str, Dict[str, int]]:
        """Allocate hard per-agent ceilings whose sum never exceeds the run cap.

        ``llmQuota`` bounds logical agent turns; retry attempts are separately
        counted by the run-wide provider-call ledger. ``actionTurnQuota`` is
        the subset that may contain native tool calls; at least one turn is
        kept for the final structured decision. Input signals and successful
        history influence priority, but can never mint calls beyond
        ``policy.maxLlmCalls``. Runtime control-plane reservations and provider
        attempts already consumed are subtracted before agent allocation.
        """
        if not ordered:
            return {}
        sig = signals or {}
        others = [a for a in ordered if a != terminal]
        hist_ratios = self._extract_budget_ratios(execution_history, others)
        hard_cap = max(0, int(self.policy.maxLlmCalls))
        if sig.get("single_pass_evaluation"):
            # One bounded decision flow per specialist and one complete
            # ReportAgent report. Specialists may use an optional progressive
            # Skill/tool turn; Project may use two for external research.
            hard_cap = min(hard_cap, 12)
        runtime_budget = getattr(self.llm, "budget", None)
        if runtime_budget is not None and hasattr(
                runtime_budget, "available_agent_llm_calls"):
            hard_cap = min(
                hard_cap,
                runtime_budget.available_agent_llm_calls(hard_cap))
        if sig.get("single_pass_evaluation"):
            quotas = {agent: 0 for agent in ordered}
            remaining = hard_cap
            if terminal in quotas:
                terminal_turns = min(
                    3 if terminal == "ReportAgent" else 1, remaining)
                quotas[terminal] = terminal_turns
                remaining -= terminal_turns
            priority = [
                "TechAgent", "ProjectAgent", "RiskAgent",
            ]
            for agent in priority:
                if remaining <= 0:
                    break
                if agent in quotas and quotas[agent] == 0:
                    quotas[agent] = 1
                    remaining -= 1
            # Documentation MCPs are useful only for recognized stacks. Three
            # action turns allow Skill load + Context7 resolve -> query before
            # the Tech final (Microsoft Learn normally finishes in fewer).
            if ((sig.get("has_framework_stack") or sig.get("has_microsoft_stack"))
                    and "TechAgent" in quotas):
                while remaining > 0 and quotas["TechAgent"] < 4:
                    quotas["TechAgent"] += 1
                    remaining -= 1
            # Public project evidence gets extra turns only when the candidate
            # actually declared an external URL/repository.
            if (sig.get("has_external_urls") and "ProjectAgent" in quotas):
                while remaining > 0 and quotas["ProjectAgent"] < 3:
                    quotas["ProjectAgent"] += 1
                    remaining -= 1
            # Give the other reasoning specialists room to activate one Skill
            # from metadata. They keep the turn when no activation is useful.
            memory_priority = []
            if sig.get("memory_project_gaps"):
                memory_priority.append("ProjectAgent")
            if sig.get("memory_risk_patterns"):
                memory_priority.append("RiskAgent")
            memory_priority.extend(
                ["TechAgent", "ProjectAgent", "RiskAgent"])
            for agent in dict.fromkeys(memory_priority):
                if remaining <= 0:
                    break
                if agent in quotas and quotas[agent] == 1:
                    quotas[agent] += 1
                    remaining -= 1
            per_agent_tools = min(
                max(1, self.policy.toolBudget.maxToolCallsPerRun
                    // max(1, len(ordered))),
                self.policy.toolBudget.maxToolCallsPerAgent)
            budget: Dict[str, Dict[str, int]] = {}
            for agent in ordered:
                tool_quota = per_agent_tools
                if agent == "TechAgent" and (
                        sig.get("has_framework_stack")
                        or sig.get("has_microsoft_stack")):
                    # Deterministic JD/RAG pre-steps share this counter with
                    # progressive Skill and documentation MCP actions. Keep
                    # enough capacity for preflight + load + docs lookup.
                    tool_quota = min(
                        10, self.policy.toolBudget.maxToolCallsPerAgent)
                if agent == "ProjectAgent" and sig.get("has_external_urls"):
                    tool_quota = min(
                        10, self.policy.toolBudget.maxToolCallsPerAgent)
                budget[agent] = {
                    "llmQuota": quotas[agent],
                    "actionTurnQuota": min(
                        (3 if agent == "TechAgent" and (
                            sig.get("has_framework_stack")
                            or sig.get("has_microsoft_stack"))
                         else 2 if agent == "ProjectAgent"
                         and sig.get("has_external_urls") else 1),
                        max(0, quotas[agent] - 1)),
                    "toolQuota": tool_quota,
                }
            return budget
        quotas = {agent: 0 for agent in ordered}
        remaining = hard_cap

        # Protect the terminal floor before assigning any specialist turn.
        # With constrained policies it is better to skip a specialist LLM and
        # consume its deterministic artifacts than to strand ReportAgent
        # without a finalization/repair turn.
        if terminal in quotas and remaining > 0:
            terminal_floor = min(
                remaining,
                max(1, int(self.policy.terminalLlmReserve)))
            quotas[terminal] = terminal_floor
            remaining -= terminal_floor
        for agent in others:
            if remaining <= 0:
                break
            quotas[agent] += 1
            remaining -= 1

        base_weights = {
            "TechAgent": 2.5,
            "ProjectAgent": 3.0,
            "RiskAgent": 2.0,
            "ReportAgent": 4.0,
        }
        weights = {
            agent: base_weights.get(agent, 1.5) for agent in ordered}
        if sig.get("has_projects"):
            weights["ProjectAgent"] = weights.get("ProjectAgent", 3.0) + 3.0
        if sig.get("has_external_urls"):
            weights["ProjectAgent"] = weights.get("ProjectAgent", 3.0) + 5.0
        if sig.get("has_jd") or sig.get("has_jd_requirements"):
            weights["TechAgent"] = weights.get("TechAgent", 2.5) + 2.0
        if sig.get("has_timeline"):
            weights["RiskAgent"] = weights.get("RiskAgent", 2.0) + 1.0
        if hist_ratios:
            for agent, ratio in hist_ratios.items():
                weights[agent] = weights.get(agent, 1.5) + 4.0 * ratio

        action_caps: Dict[str, int] = {}
        total_caps: Dict[str, int] = {}
        for agent in ordered:
            try:
                definition = self.registry.get(agent)
                decision_cap = max(1, min(
                    definition.max_iterations,
                    self.policy.maxIterationsPerAgent))
                max_native_action_turns = (
                    3 if agent == "ProjectAgent"
                    and sig.get("has_external_urls") else 2)
                action_cap = min(
                    max_native_action_turns, definition.max_tool_calls,
                    self.policy.toolBudget.maxToolCallsPerAgent)
            except KeyError:
                decision_cap, action_cap = 1, 1
            action_caps[agent] = max(0, action_cap)
            total_caps[agent] = decision_cap + max(0, action_cap)

        # Project/Evidence are the two source-acquisition specialists in a
        # complete evaluation.  A one-turn quota can only produce their final
        # decision and makes the advertised MCP/Skill catalog unreachable.
        #
        # An explicit candidate URL needs one additional Project turn:
        # metadata -> load the selected Skill -> model-selected MCP call ->
        # final decision.  This remains native ``tool_choice=auto``; only the
        # already-computed run budget is routed toward the evidenced need.
        # Without this floor production traces stopped after Skill loading and
        # incorrectly reported the URL as "not checked" despite a live catalog.
        action_floors = {
            # local evidence -> Skill -> MCP -> final is four LLM turns.
            "ProjectAgent": 4 if sig.get("has_external_urls") else 2,
        }
        for tool_agent, floor in action_floors.items():
            while (
                    remaining > 0
                    and tool_agent in quotas
                    and quotas[tool_agent] > 0
                    and action_caps[tool_agent] > 0
                    and quotas[tool_agent] < floor
                    and quotas[tool_agent] < total_caps[tool_agent]):
                quotas[tool_agent] += 1
                remaining -= 1

        # Weighted marginal allocation avoids the old rich-resume bug where
        # per-agent quotas summed to more than the client-wide hard cap.
        while remaining > 0:
            eligible = [
                agent for agent in ordered
                if quotas[agent] < total_caps[agent]]
            if not eligible:
                break
            # Distribute action-capable second turns before giving a third or
            # fourth turn to one high-weight specialist.  The previous pure
            # weight ratio left Project/Evidence at llmQuota=1, which means
            # actionTurnQuota=0 and made their advertised MCP tools impossible
            # to call in production.
            nonterminal = [
                agent for agent in eligible if agent != terminal]
            pool = nonterminal or eligible
            lowest_quota = min(quotas[agent] for agent in pool)
            layer = [
                agent for agent in pool
                if quotas[agent] == lowest_quota]
            agent = max(
                layer,
                key=lambda item: (
                    weights.get(item, 1.0) / (quotas[item] + 0.5),
                    -ordered.index(item)))
            quotas[agent] += 1
            remaining -= 1

        per_agent_tools = min(
            max(1, self.policy.toolBudget.maxToolCallsPerRun
                // max(1, len(ordered))),
            self.policy.toolBudget.maxToolCallsPerAgent)
        budget: Dict[str, Dict[str, int]] = {}
        for agent in ordered:
            llm_quota = quotas[agent]
            action_quota = min(
                action_caps[agent], max(0, llm_quota - 1))
            tool_quota = per_agent_tools
            if agent == "ProjectAgent" and sig.get("has_external_urls"):
                # Real external-evidence turns commonly need one Skill load,
                # two local evidence presteps and up to three model-selected
                # MCP calls (search plus two candidate-declared URLs). Keep
                # that branch usable without raising the run-wide hard cap.
                tool_quota = min(
                    6, self.policy.toolBudget.maxToolCallsPerAgent)
            budget[agent] = {
                "llmQuota": llm_quota,
                "actionTurnQuota": action_quota,
                "toolQuota": tool_quota,
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
        return preferred + rest + terminals

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
            signal_lines.append(
                "有公开代码仓库链接，应把外部核验列为高价值选项；"
                "由执行 Agent 根据实时工具目录与证据缺口自主决定是否调用")
        if sig.get("has_publications"):
            signal_lines.append("有论文/出版物，TechAgent 应深入评估学术能力和研究方向")
        if sig.get("is_senior"):
            signal_lines.append("资深候选人（10年+），RiskAgent 重点关注架构决策和团队管理证据")
        if sig.get("has_management_exp"):
            signal_lines.append("有管理经验，评估应包含团队规模、管理方法论和跨部门协作能力")
        if sig.get("is_career_changer"):
            signal_lines.append("跨领域转型候选人，TechAgent 需额外评估能力迁移可行性")
        if sig.get("has_certifications"):
            signal_lines.append("有行业认证，TechAgent 需验证认证有效性并作为加分项")
        if not sig.get("has_projects"):
            signal_lines.append("无明显项目经历，可跳过 ProjectAgent，预算分配给其他Agent")
        if not sig.get("has_timeline"):
            signal_lines.append("无清晰时间线，RiskAgent 时间线核查优先级降低")
        if sig.get("has_external_urls"):
            signal_lines.append(
                "有外部链接，可优先安排公开证据核验；具体工具由执行 Agent 自主选择")
        if sig.get("resume_language") == "en":
            signal_lines.append("英文简历，评估标准切换为国际化口径")

        prompt_user = (
            f"任务类别: {run_type}\n用户问题: {user_message[:600]}\n"
            f"会话摘要: {(conversation_summary or '')[:400]}\n"
            f"共享状态摘要: {shared_digest[:800]}\n"
            f"候选人信号:\n" + "\n".join(f"  - {s}" for s in signal_lines) + "\n"
            f"历史失败提示: {'; '.join(failure_notes[:3]) or '无'}\n"
            f"相关记忆: {'; '.join(str(m.get('content','') if isinstance(m, dict) else m)[:80] for m in memory_notes[:3]) or '无'}\n"
            f"可用 Agent 能力目录:\n{self.capability_catalog()}\n"
            f"产物规划基线: {base_plan}\n"
            f"必选 goal artifacts: {goal_artifacts or []}\n"
            f"预算: 最多 {self.policy.maxAgentCount} 个 Agent, "
            f"{self.policy.maxLlmCalls} 次 LLM 调用\n\n"
            "你是动态规划 Coordinator。根据候选人特征做出差异化决策：\n"
            "1. 丰富简历（>2000字+项目+多年经验）：完整流水线+深度验证\n"
            "2. 薄简历（<800字/应届/缺少项目）：精简路径，跳过ProjectAgent\n"
            "3. 有GitHub/外部URL：保留公开证据核验能力，具体工具由执行 Agent 自主选择\n"
            "4. 有论文/出版物：TechAgent需深入学术评估\n"
            "5. 资深候选人（10年+/总监级）：RiskAgent重点关注管理&架构证据\n"
            "6. 跨领域转型：由TechAgent评估能力迁移可行性\n\n"
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
        project_depth_run = run_type == "project_analysis"
        if (run_type in FULL_EVAL_TYPES or project_depth_run) \
                and signals.get("has_projects") \
                and not signals.get("is_sparse_resume") \
                and "project_findings" in goal:
            forced.add("ProjectAgent")
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
        return ""


def _cost_rank(hint: str) -> int:
    return {"low": 0, "medium": 1, "high": 2}.get((hint or "medium").lower(), 1)
