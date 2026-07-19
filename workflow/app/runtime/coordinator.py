from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from app.runtime.agents import AgentRegistry
from app.runtime.llm import ResilientLlmClient, extract_json_object
from app.runtime.models import PolicyBundle

logger = logging.getLogger(__name__)

# Deterministic task-type pipelines (spec §8 示例). The coordinator starts
# from these, then the policy bundle and an optional LLM refinement adjust
# them within budget. ReportAgent stays terminal for analysis flows.
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


class Coordinator:
    """Dynamic agent selection: rule-first pipeline from the task type,
    shaped by the policy bundle, optionally refined by one budgeted LLM call
    using conversation context, shared state and failure history."""

    def __init__(self, registry: AgentRegistry, policy: PolicyBundle,
                 llm: Optional[ResilientLlmClient]) -> None:
        self.registry = registry
        self.policy = policy
        self.llm = llm

    def base_plan(self, run_type: str, *, has_resume_facts: bool,
                  needs_parse: bool) -> List[str]:
        plan = list(TASK_PIPELINES.get(run_type, TASK_PIPELINES["full_evaluation"]))
        if needs_parse and "ResumeParserAgent" not in plan:
            plan.insert(0, "ResumeParserAgent")
        if not self.policy.evidenceVerification.enabled:
            plan = [a for a in plan if a != "EvidenceAgent"]
        # policy agentOrder acts as an allow/priority list for analysis flows
        if self.policy.agentOrder:
            allowed = set(self.policy.agentOrder) | {"ResumeParserAgent"} | TERMINAL_AGENTS
            plan = [a for a in plan if a in allowed]
        plan = [a for a in plan if self.registry.known(a)]
        plan = plan[: max(1, self.policy.maxAgentCount)]
        if not any(a in TERMINAL_AGENTS for a in plan):
            plan.append("ReportAgent")
        return plan

    async def refine_plan(self, base_plan: List[str], *, run_type: str,
                          user_message: str, conversation_summary: str,
                          shared_digest: str, failure_notes: List[str],
                          memory_notes: List[str]) -> Dict[str, Any]:
        """One budgeted LLM refinement; falls back to the rule plan."""
        if self.llm is None:
            return {"plan": base_plan, "reason": "rule_based(no-llm)"}
        prompt_user = (
            f"任务类别: {run_type}\n用户问题: {user_message[:600]}\n"
            f"会话摘要: {(conversation_summary or '')[:400]}\n"
            f"共享状态摘要: {shared_digest[:800]}\n"
            f"历史失败提示: {'; '.join(failure_notes[:3]) or '无'}\n"
            f"相关记忆: {'; '.join(memory_notes[:3]) or '无'}\n"
            f"规则基线计划: {base_plan}\n"
            f"预算: 最多 {self.policy.maxAgentCount} 个 Agent, "
            f"{self.policy.maxLlmCalls} 次 LLM 调用\n"
            "如基线已合理请原样返回。输出 JSON {\"plan\": [...], \"reason\": \"...\"}")
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
            plan = plan[: max(1, self.policy.maxAgentCount)]
            if not any(a in TERMINAL_AGENTS for a in plan):
                plan.append("ReportAgent")
            # 终局 Agent 始终收尾
            plan.sort(key=lambda a: 1 if a in TERMINAL_AGENTS else 0)
            return {"plan": plan, "reason": str(parsed.get("reason", "llm_refined"))[:200]}
        except Exception as exc:  # noqa: BLE001 - planning must not kill the run
            logger.info("coordinator refine failed, using rule plan: %s", exc)
            return {"plan": base_plan, "reason": f"rule_based(llm-error:{type(exc).__name__})"}

    def replan_after_failure(self, remaining: List[str], failed_agent: str) -> List[str]:
        """Failure handling: keep partial results, pick an alternative path,
        degrade to report generation with whatever state exists."""
        alternatives = {
            "TechAgent": [],
            "ProjectAgent": [],
            "RiskAgent": [],
            "JDAnalysisAgent": [],
            "EvidenceAgent": [],
            "ResumeParserAgent": [],
        }
        substitute = alternatives.get(failed_agent)
        plan = [a for a in remaining if a != failed_agent]
        if substitute:
            plan = substitute + plan
        if not any(a in TERMINAL_AGENTS for a in plan):
            plan.append("ReportAgent")
        return plan
