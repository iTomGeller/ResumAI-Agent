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
    "followup": ["ReportAgent"],
    "quick_answer": ["ReportAgent"],
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
    "quick_answer": ["final_report"],
}

TERMINAL_AGENTS = {"ReportAgent", "ResumeOptimizeAgent", "InterviewQuestionAgent"}
REPLAN_TRIGGERS = {
    "missing_required_artifact", "tool_failed", "new_conflict",
    "handoff_requested", "group_failure", "low_confidence",
}

# Soft preference for simple runTypes: still artifact-planned, but skip LLM refine.
SIMPLE_RULE_TYPES = {
    "timeline_check", "risk_check", "evidence_check", "tech_match",
    "project_analysis", "project_rewrite", "resume_optimize",
    "interview_questions", "followup", "quick_answer",
}

# Soft dependency edges used for topo + parallel grouping (artifact edges are
# the source of truth for *selection*; these keep ordering stable).
AGENT_DEPENDENCIES: Dict[str, List[str]] = {
    "ResumeParserAgent": [],
    "JDAnalysisAgent": ["ResumeParserAgent"],
    "TechAgent": ["JDAnalysisAgent"],
    "ProjectAgent": ["ResumeParserAgent"],
    "RiskAgent": ["ResumeParserAgent"],
    "EvidenceAgent": ["TechAgent", "ProjectAgent", "RiskAgent"],
    "ReportAgent": ["EvidenceAgent"],
    "ResumeOptimizeAgent": ["ProjectAgent"],
    "InterviewQuestionAgent": ["RiskAgent"],
}

PARALLELIZABLE = {"TechAgent", "ProjectAgent", "RiskAgent"}

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
        if self.policy.agentOrder:
            allowed = set(self.policy.agentOrder) | {"ResumeParserAgent"} | TERMINAL_AGENTS
            plan = [a for a in plan if a in allowed]
        plan = [a for a in plan if self.registry.known(a)]
        plan = plan[: max(1, self.policy.maxAgentCount)]
        if not any(a in TERMINAL_AGENTS for a in plan):
            plan.append("ReportAgent")
        return plan

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
        resume_facts = shared.get("resumeFacts") or {}
        parsed = artifacts.get("parsedResume") if isinstance(artifacts.get("parsedResume"), dict) \
            else {}
        projects = (resume_facts.get("projects") or parsed.get("projects")
                    or parsed.get("project_names") or [])
        experiences = (resume_facts.get("experiences") or parsed.get("experiences")
                       or parsed.get("timeline") or [])
        text = resume_text or ""
        has_projects = bool(projects) or bool(_PROJECT_HINT.search(text))
        has_timeline = bool(experiences) or bool(_TIMELINE_HINT.search(text))
        has_jd = bool((job_description or "").strip()) \
            or bool(artifacts.get("effectiveJd")) \
            or bool(artifacts.get("jdMatches")) \
            or bool(shared.get("jdRequirements"))
        has_external_urls = bool(_URL_HINT.search(text))
        present = self._present_artifacts(artifacts, shared)
        return {
            "has_projects": has_projects,
            "has_timeline": has_timeline,
            "has_jd": has_jd,
            "has_jd_or_match": has_jd or bool(text.strip()),
            "has_jd_requirements": "jd_requirements" in present or has_jd,
            "has_external_urls": has_external_urls,
            "evidence_enabled": bool(self.policy.evidenceVerification.enabled),
            "needs_parse": "resume_facts" not in present and bool(text.strip()),
        }

    def plan_from_artifacts(self, *, run_type: str, needs_parse: bool,
                            resume_text: str = "", job_description: str = "",
                            artifacts: Optional[Dict[str, Any]] = None,
                            shared: Optional[Dict[str, Any]] = None
                            ) -> Dict[str, Any]:
        """GOAL_ARTIFACTS → backward-chain → topo → parallel groups."""
        goal = list(GOAL_ARTIFACTS.get(run_type, GOAL_ARTIFACTS["full_evaluation"]))
        signals = self.inspect_signals(
            resume_text=resume_text, job_description=job_description,
            artifacts=artifacts, shared=shared)
        if needs_parse or signals["needs_parse"]:
            signals = {**signals, "needs_parse": True}
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
        for definition in self.registry.list_enabled():
            if definition.agent_id == "CoordinatorAgent":
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
                          if p.agent_id not in skipped_because]
            if not candidates:
                continue
            chosen = sorted(candidates, key=lambda d: _cost_rank(d.cost_hint))[0]
            if chosen.agent_id not in selected:
                # Soft requires: only schedule producers whose optional gate passes.
                skip = self._optional_skip_reason(chosen, signals)
                if skip:
                    skipped_because[chosen.agent_id] = skip
                    # If a goal artifact becomes unreachable, drop it rather than
                    # forcing an optional agent (e.g. no projects → no Project).
                    if artifact in goal:
                        goal = [g for g in goal if g != artifact]
                        missing = [m for m in missing if m != artifact]
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

        # Evidence soft-requires: when evidence is off, map final_report deps down.
        if not self.policy.evidenceVerification.enabled:
            selected = [a for a in selected if a != "EvidenceAgent"]
            skipped_because.setdefault(
                "EvidenceAgent", "策略关闭证据核验")

        # Policy agentOrder filter.
        if self.policy.agentOrder:
            allowed = set(self.policy.agentOrder) | {"ResumeParserAgent"} | TERMINAL_AGENTS
            trimmed = []
            for agent in selected:
                if agent in allowed:
                    trimmed.append(agent)
                else:
                    skipped_because.setdefault(agent, "不在策略 agentOrder 内")
            selected = trimmed

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
            goal_artifacts=goal)
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

        cache_key = cache.content_key(
            "plan", run_type, self.policy.policyId, (user_message or "")[:200],
            ",".join(base["plan"]))
        cached = await cache.get_json(cache_key)
        if isinstance(cached, dict) and cached.get("plan"):
            return self._finalize(
                [str(a) for a in cached["plan"]],
                str(cached.get("reason", "cached")) + " (cached)",
                selected_because=cached.get("selectedBecause") or base.get("selectedBecause"),
                skipped_because=cached.get("skippedBecause") or base.get("skippedBecause"),
                artifact_edges=cached.get("artifactEdges") or base.get("artifactEdges"),
                goal_artifacts=base.get("goalArtifacts"))

        refined = await self._refine(
            base["plan"], run_type=run_type, user_message=user_message,
            conversation_summary=conversation_summary, shared_digest=shared_digest,
            failure_notes=failure_notes, memory_notes=memory_notes)
        if not refined["plan"]:
            fallback = self.base_plan(
                run_type, has_resume_facts=False, needs_parse=needs_parse)
            return self._finalize(
                fallback, "safety_fallback(TASK_PIPELINES)",
                selected_because={a: "planner+LLM 均失败" for a in fallback},
                skipped_because=base.get("skippedBecause") or {},
                artifact_edges=base.get("artifactEdges") or [],
                goal_artifacts=base.get("goalArtifacts"))
        if not refined["reason"].startswith("rule_based(llm-error"):
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
            goal_artifacts=base.get("goalArtifacts"))

    def _finalize(self, plan: List[str], reason: str,
                  selected_because: Optional[Dict[str, str]] = None,
                  skipped_because: Optional[Dict[str, str]] = None,
                  artifact_edges: Optional[List[Dict[str, str]]] = None,
                  goal_artifacts: Optional[List[str]] = None) -> Dict[str, Any]:
        ordered = self._order_by_dependencies(plan)
        ordered = self._ensure_unique_terminal(ordered)
        groups = self._parallel_groups(ordered)
        terminal = next((a for a in reversed(ordered) if a in TERMINAL_AGENTS),
                        "ReportAgent")
        budget = self._budget_plan(ordered, terminal)
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
            "goalArtifacts": goal_artifacts or [],
        }

    def _budget_plan(self, ordered: List[str], terminal: str) -> Dict[str, Dict[str, int]]:
        """Plan-time budget allocation: the terminal agent gets a guaranteed
        floor of 2 LLM calls so specialists can never starve the report; the
        rest of the run budget is split evenly across the other agents."""
        terminal_floor = 2
        others = [a for a in ordered if a != terminal]
        remaining_llm = max(1, self.policy.maxLlmCalls - terminal_floor)
        per_agent_llm = max(1, remaining_llm // max(1, len(others)))
        per_agent_tools = max(1, self.policy.toolBudget.maxToolCallsPerRun
                              // max(1, len(ordered)))
        budget: Dict[str, Dict[str, int]] = {}
        for agent in ordered:
            budget[agent] = {
                "llmQuota": terminal_floor if agent == terminal else per_agent_llm,
                "toolQuota": min(per_agent_tools,
                                 self.policy.toolBudget.maxToolCallsPerAgent),
            }
        return budget

    def _order_by_dependencies(self, plan: List[str]) -> List[str]:
        """Stable topological pass: an agent is scheduled only after every
        planned dependency; unknown agents are dropped."""
        plan = [a for a in dict.fromkeys(plan) if self.registry.known(a)]
        placed: List[str] = []
        remaining = list(plan)
        stall_guard = 0
        while remaining and stall_guard <= len(plan) * 2:
            stall_guard += 1
            progressed = False
            for agent in list(remaining):
                deps = [d for d in AGENT_DEPENDENCIES.get(agent, []) if d in plan]
                if all(d in placed for d in deps):
                    placed.append(agent)
                    remaining.remove(agent)
                    progressed = True
            if not progressed:
                placed.extend(remaining)  # cycle fallback: keep original order
                break
        return self._ensure_unique_terminal(placed[: max(1, self.policy.maxAgentCount) + 1])

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
                      memory_notes: List[str]) -> Dict[str, Any]:
        prompt_user = (
            f"任务类别: {run_type}\n用户问题: {user_message[:600]}\n"
            f"会话摘要: {(conversation_summary or '')[:400]}\n"
            f"共享状态摘要: {shared_digest[:800]}\n"
            f"历史失败提示: {'; '.join(failure_notes[:3]) or '无'}\n"
            f"相关记忆: {'; '.join(memory_notes[:3]) or '无'}\n"
            f"可用 Agent 能力目录:\n{self.capability_catalog()}\n"
            f"产物规划基线: {base_plan}\n"
            f"预算: 最多 {self.policy.maxAgentCount} 个 Agent, "
            f"{self.policy.maxLlmCalls} 次 LLM 调用\n"
            "只允许使用目录中的 Agent，必须满足 requires_artifacts / 依赖，"
            "没有项目不要选 ProjectAgent，没有时间线不要选 RiskAgent，"
            "最后一个必须是唯一的 terminal Agent。如基线已合理请原样返回。"
            "输出 JSON {\"plan\": [...], \"reason\": \"...\"}")
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
            return {"plan": plan,
                    "reason": str(parsed.get("reason", "llm_refined"))[:200]}
        except Exception as exc:  # noqa: BLE001 - planning must not kill the run
            logger.info("coordinator refine failed, using artifact plan: %s", exc)
            return {"plan": base_plan,
                    "reason": f"rule_based(llm-error:{type(exc).__name__})"}

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
        mapping = {
            "parsedResume": "parsed_resume",
            "jdCoverage": "technical_findings",
            "timelineCheck": "risks",
            "effectiveJd": "jd_requirements",
            "jdMatches": "jd_requirements",
            "finalReport": "final_report",
        }
        for key, artifact in mapping.items():
            if artifacts.get(key):
                present.add(artifact)
        if shared.get("resumeFacts"):
            present.add("resume_facts")
        if shared.get("jdRequirements"):
            present.add("jd_requirements")
        if shared.get("technicalFindings"):
            present.add("technical_findings")
        if shared.get("projectFindings"):
            present.add("project_findings")
        if shared.get("risks"):
            present.add("risks")
        if shared.get("evidence"):
            present.add("evidence_ledger")
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
