from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional


@dataclass(frozen=True)
class AgentDefinition:
    agent_id: str
    name: str
    description: str
    capabilities: tuple
    system_prompt_version: str = "v1"
    skills: tuple = ()
    tools: tuple = ()
    mcp_servers: tuple = ()
    memory_policy: str = "task_relevant"     # none / task_relevant / broad
    context_policy: str = "focused"          # focused / wide
    max_iterations: int = 2
    max_tool_calls: int = 5
    timeout_seconds: int = 120
    failure_policy: str = "degrade"          # degrade / skip / abort
    enabled: bool = True
    output_type: str = "findings"
    # Dynamic planner: artifact contract + cost + optional gates
    requires_artifacts: tuple = ()
    produces_artifacts: tuple = ()
    cost_hint: str = "medium"                # low / medium / high
    optional_when: str = ""                  # e.g. has_projects / has_timeline / has_jd


AGENT_DEFINITIONS: Dict[str, AgentDefinition] = {
    "CoordinatorAgent": AgentDefinition(
        "CoordinatorAgent", "协调者", "分析用户目标并动态选择 Agent 流水线",
        ("planning", "routing", "replanning"),
        skills=(),
        tools=(), max_iterations=1, max_tool_calls=0, timeout_seconds=60,
        failure_policy="degrade", output_type="plan",
        requires_artifacts=(), produces_artifacts=("execution_plan",),
        cost_hint="low"),
    "ResumeParserAgent": AgentDefinition(
        "ResumeParserAgent", "简历解析", "简历结构化与事实提取",
        ("resume_parsing",),
        # Conversation routing is not a parser capability. Keeping it here
        # produced a fake "Skill skipped" child on the deterministic no-LLM
        # parser fast path.
        skills=(),
        tools=("parse_resume",),
        max_iterations=1, max_tool_calls=2, timeout_seconds=150,
        output_type="resume_facts",
        requires_artifacts=(), produces_artifacts=("resume_facts", "parsed_resume"),
        cost_hint="low"),
    "JDAnalysisAgent": AgentDefinition(
        "JDAnalysisAgent", "JD 分析", "岗位要求提取与归一化",
        ("jd_analysis",),
        skills=(),
        tools=(),
        max_iterations=2, max_tool_calls=4, timeout_seconds=150,
        output_type="jd_requirements",
        requires_artifacts=("resume_facts",),
        produces_artifacts=("jd_requirements",),
        cost_hint="medium", optional_when="has_jd_or_match"),
    "TechAgent": AgentDefinition(
        "TechAgent", "技术评估", "技能与 JD 匹配、深度信号评估",
        ("tech_evaluation",),
        skills=("assess-technical-evidence", "assess-production-engineering"),
        tools=("calculate_jd_coverage",),
        # Four deterministic pre-steps may precede Skill + documentation MCP
        # calls; five made valid provider actions fail as budget exhausted.
        max_iterations=2, max_tool_calls=10, timeout_seconds=240,
        output_type="technical_findings",
        requires_artifacts=("resume_facts", "jd_requirements"),
        produces_artifacts=("technical_findings",),
        cost_hint="medium", optional_when="has_jd_requirements"),
    "ProjectAgent": AgentDefinition(
        "ProjectAgent", "项目分析", "项目复杂度、贡献与真实性",
        ("project_analysis",),
        skills=("ground-project-claims", "retrieve-public-candidate-evidence"),
        tools=("locate_evidence",),
        mcp_servers=("exa", "fetch"),
        # External evidence may require preflight + two Skills + several live
        # provider calls in one model-authored action batch.
        max_iterations=2, max_tool_calls=10, timeout_seconds=240,
        output_type="project_findings",
        requires_artifacts=("resume_facts",),
        produces_artifacts=("project_findings",),
        cost_hint="high", optional_when="has_projects"),
    "RiskAgent": AgentDefinition(
        "RiskAgent", "风险审查", "时间线与履历风险检测",
        ("risk_detection", "timeline_check"),
        skills=("risk-pattern-detection", "audit-claim-consistency"),
        tools=("check_timeline", "timeline_validator"),
        mcp_servers=("exa", "fetch"),
        max_iterations=2, max_tool_calls=4, timeout_seconds=180,
        output_type="risks",
        requires_artifacts=("resume_facts",),
        produces_artifacts=("risks",),
        cost_hint="low", optional_when="has_timeline"),
    "EvidenceAgent": AgentDefinition(
        "EvidenceAgent", "证据核验", "对结论逐条核验并记录冲突",
        ("evidence_verification",),
        skills=("calibrate-evidence-confidence", "audit-evidence-provenance"),
        tools=("verify_report_evidence", "locate_evidence"),
        mcp_servers=("exa", "fetch"),
        max_iterations=2, max_tool_calls=6, timeout_seconds=210,
        output_type="evidence",
        # Soft deps via AGENT_DEPENDENCIES: only wait for specialists that are
        # actually in the plan (skipped Project/Risk must not block Evidence).
        requires_artifacts=("resume_facts",),
        produces_artifacts=("evidence_ledger",),
        cost_hint="medium", optional_when="evidence_enabled"),
    "ReportAgent": AgentDefinition(
        "ReportAgent", "报告生成", "汇总证据生成最终回答",
        ("report_generation",),
        skills=(),
        # Full evaluation and Copilot follow-up both use deterministic
        # pre-generation RAG injected into the user prompt. ReportAgent has no
        # model-callable retrieval or public-network tools.
        tools=(),
        max_iterations=2, max_tool_calls=4, timeout_seconds=240,
        failure_policy="abort", output_type="report",
        # evidence_ledger is preferred when present; followup/quick_answer may
        # produce final_report directly without a full evidence pass.
        requires_artifacts=(),
        produces_artifacts=("final_report",),
        cost_hint="medium"),
    "ResumeOptimizeAgent": AgentDefinition(
        "ResumeOptimizeAgent", "简历优化", "事实不变前提下的改写",
        ("resume_rewrite",),
        skills=("ground-project-claims",),
        tools=("resume_lint", "locate_evidence"),
        max_iterations=2, max_tool_calls=4, timeout_seconds=240,
        output_type="rewrite",
        requires_artifacts=("project_findings",),
        produces_artifacts=("rewrite",),
        cost_hint="medium"),
    "InterviewQuestionAgent": AgentDefinition(
        "InterviewQuestionAgent", "面试追问", "针对风险与缺口生成追问",
        ("interview_questions",),
        skills=(),
        tools=(),
        max_iterations=2, max_tool_calls=2, timeout_seconds=150,
        output_type="questions",
        requires_artifacts=("risks",),
        produces_artifacts=("interview_questions",),
        cost_hint="low"),
}


class AgentRegistry:
    def __init__(self, definitions: Optional[Dict[str, AgentDefinition]] = None) -> None:
        self._definitions = dict(definitions or AGENT_DEFINITIONS)

    def get(self, agent_id: str) -> AgentDefinition:
        definition = self._definitions.get(agent_id)
        if definition is None or not definition.enabled:
            raise KeyError(f"agent not available: {agent_id}")
        return definition

    def known(self, agent_id: str) -> bool:
        return agent_id in self._definitions

    def list_enabled(self) -> List[AgentDefinition]:
        return [d for d in self._definitions.values() if d.enabled]

    def producers_of(self, artifact: str) -> List[AgentDefinition]:
        return [d for d in self.list_enabled()
                if artifact in d.produces_artifacts and d.agent_id != "CoordinatorAgent"]


default_agent_registry = AgentRegistry()
