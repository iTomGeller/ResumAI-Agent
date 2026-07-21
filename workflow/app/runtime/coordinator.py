from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from app.runtime.agents import AgentRegistry
from app.runtime.llm import ResilientLlmClient, extract_json_object
from app.runtime.models import PolicyBundle

logger = logging.getLogger(__name__)

# Deterministic task-type pipelines: the safety fallback and the fast path for
# simple, unambiguous requests. Complex/ambiguous requests additionally get
# one budgeted LLM refinement over the agent capability catalog.
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

TERMINAL_AGENTS = {"ReportAgent", "ResumeOptimizeAgent", "InterviewQuestionAgent"}

# Rule-routed types never spend a Coordinator LLM call: the pipeline is a
# direct, unambiguous function of the request type.
SIMPLE_RULE_TYPES = {
    "timeline_check", "risk_check", "evidence_check", "tech_match",
    "project_analysis", "project_rewrite", "resume_optimize",
    "interview_questions", "followup", "quick_answer",
}

# Data dependencies between agents. parallel groups may only contain agents
# whose dependencies are all satisfied by earlier groups.
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

# Specialists that read disjoint blackboard sections and may run concurrently.
PARALLELIZABLE = {"TechAgent", "ProjectAgent", "RiskAgent"}


class Coordinator:
    """Dynamic agent planning.

    1. Rule-first: simple runTypes map directly to a pipeline (no LLM cost).
    2. Complex runTypes start from the rule baseline and get one budgeted LLM
       refinement over the capability catalog, shared state, memory and
       failure history.
    3. The final plan is grouped into parallelGroups respecting
       AGENT_DEPENDENCIES; a terminal agent always closes the plan.
    """

    def __init__(self, registry: AgentRegistry, policy: PolicyBundle,
                 llm: Optional[ResilientLlmClient]) -> None:
        self.registry = registry
        self.policy = policy
        self.llm = llm

    # ------------------------------------------------------------------

    def base_plan(self, run_type: str, *, has_resume_facts: bool,
                  needs_parse: bool) -> List[str]:
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
            deps = AGENT_DEPENDENCIES.get(definition.agent_id, [])
            lines.append(
                f"- {definition.agent_id}: {definition.description}"
                f" | capabilities={','.join(definition.capabilities)}"
                f" | requires={','.join(deps) or '无'}"
                f" | terminal={'是' if definition.agent_id in TERMINAL_AGENTS else '否'}")
        return "\n".join(lines)

    async def plan(self, *, run_type: str, user_message: str,
                   conversation_summary: str, shared_digest: str,
                   failure_notes: List[str], memory_notes: List[str],
                   needs_parse: bool) -> Dict[str, Any]:
        """Full planning entry: returns plan, reason, parallelGroups,
        requiredTerminalAgent, dependencies and budgetPlan."""
        base = self.base_plan(run_type, has_resume_facts=False, needs_parse=needs_parse)
        if self.is_simple(run_type) or self.llm is None:
            return self._finalize(base, f"rule_based({run_type})")

        # Same runType + policy + question shape => same refined plan; one
        # cached LLM refinement covers e.g. batch-screening against one JD.
        from app.runtime import cache

        cache_key = cache.content_key(
            "plan", run_type, self.policy.policyId, (user_message or "")[:200],
            ",".join(base))
        cached = await cache.get_json(cache_key)
        if isinstance(cached, dict) and cached.get("plan"):
            return self._finalize([str(a) for a in cached["plan"]],
                                  str(cached.get("reason", "cached")) + " (cached)")

        refined = await self._refine(base, run_type=run_type,
                                     user_message=user_message,
                                     conversation_summary=conversation_summary,
                                     shared_digest=shared_digest,
                                     failure_notes=failure_notes,
                                     memory_notes=memory_notes)
        if refined["plan"] and not refined["reason"].startswith("rule_based(llm-error"):
            await cache.set_json(cache_key, refined, cache.TTL_PLAN)
        return self._finalize(refined["plan"], refined["reason"])

    def _finalize(self, plan: List[str], reason: str) -> Dict[str, Any]:
        ordered = self._order_by_dependencies(plan)
        groups = self._parallel_groups(ordered)
        terminal = next((a for a in reversed(ordered) if a in TERMINAL_AGENTS),
                        "ReportAgent")
        return {
            "plan": ordered,
            "reason": reason,
            "parallelGroups": groups,
            "requiredTerminalAgent": terminal,
            "dependencies": {a: AGENT_DEPENDENCIES.get(a, []) for a in ordered},
            "budgetPlan": self._budget_plan(ordered, terminal),
        }

    def _budget_plan(self, ordered: List[str], terminal: str) -> Dict[str, Dict[str, int]]:
        """Plan-time budget allocation: the terminal agent gets a guaranteed
        floor of 2 LLM calls so specialists can never starve the report; the
        rest of the run budget is split evenly across the other agents.
        Runtime enforcement caps each agent at its quota."""
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
        if not any(a in TERMINAL_AGENTS for a in placed):
            placed.append("ReportAgent")
        else:
            placed.sort(key=lambda a: 1 if a in TERMINAL_AGENTS else 0)
        return placed[: max(1, self.policy.maxAgentCount) + 1]

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
            f"规则基线计划: {base_plan}\n"
            f"预算: 最多 {self.policy.maxAgentCount} 个 Agent, "
            f"{self.policy.maxLlmCalls} 次 LLM 调用\n"
            "只允许使用目录中的 Agent，必须满足 requires 依赖，"
            "最后一个必须是 terminal Agent。如基线已合理请原样返回。"
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
            logger.info("coordinator refine failed, using rule plan: %s", exc)
            return {"plan": base_plan,
                    "reason": f"rule_based(llm-error:{type(exc).__name__})"}

    def replan_after_failure(self, remaining: List[str], failed_agent: str) -> List[str]:
        """Failure handling: keep partial results, drop the failed step,
        guarantee a terminal agent still closes the run."""
        plan = [a for a in remaining if a != failed_agent]
        if not any(a in TERMINAL_AGENTS for a in plan):
            plan.append("ReportAgent")
        return plan

    async def adaptive_replan(self, *, remaining: List[str], executed: List[str],
                              shared_digest: str, trigger: str,
                              failure_notes: List[str]) -> Optional[Dict[str, Any]]:
        """Mid-run replanning (bounded dynamism): when a group ends with
        failures / new conflicts / low confidence, one budgeted LLM call may
        adjust the remaining pipeline. Returns None to keep the current plan."""
        if self.llm is None or not remaining:
            return None
        prompt_user = (
            f"运行中触发重规划，原因: {trigger}\n"
            f"已完成 Agent: {executed}\n"
            f"剩余计划: {remaining}\n"
            f"共享状态摘要: {shared_digest[:800]}\n"
            f"失败记录: {'; '.join(failure_notes[-3:]) or '无'}\n"
            f"可用 Agent 能力目录:\n{self.capability_catalog()}\n"
            "只允许调整剩余部分（不得包含已完成 Agent），必须满足 requires 依赖，"
            "最后一个必须是 terminal Agent。若当前剩余计划已合理，原样返回。"
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
            if not plan or plan == remaining:
                return None
            finalized = self._finalize(plan, f"replan({trigger})")
            finalized["reason"] = str(parsed.get("reason", finalized["reason"]))[:200]
            return finalized
        except Exception as exc:  # noqa: BLE001 - replanning must not kill the run
            logger.info("adaptive replan skipped (%s): %s", trigger, exc)
            return None
