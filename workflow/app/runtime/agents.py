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
    task_prompt: str = ""
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
        task_prompt=("根据原始评估请求、简历、JD和当前共享状态选择必要的专家Agent，"
                     "分配受预算约束的执行计划；只负责控制流，不代替专家产出业务结论。"),
        skills=(),
        tools=(), max_iterations=1, max_tool_calls=0, timeout_seconds=60,
        failure_policy="degrade", output_type="plan",
        requires_artifacts=(), produces_artifacts=("execution_plan",),
        cost_hint="low"),
    "TechAgent": AgentDefinition(
        "TechAgent", "技术评估", "技能与 JD 匹配、深度信号评估",
        ("tech_evaluation",),
        task_prompt=("根据当前简历、目标JD和技术知识库，判断技术主张是否有可定位证据；"
                     "重点评估技术深度、生产工程经验和JD技术缺口，不以关键词数量代替能力判断。"),
        skills=("assess-technical-evidence", "assess-production-engineering"),
        tools=("calculate_jd_coverage",),
        # Four deterministic pre-steps may precede Skill + documentation MCP
        # calls; five made valid provider actions fail as budget exhausted.
        max_iterations=2, max_tool_calls=10, timeout_seconds=240,
        output_type="technical_findings",
        # resume/JD parsing is deterministic preflight, not a separate Agent.
        # Tech can still assess resume evidence when no effective JD exists.
        requires_artifacts=("resume_facts",),
        produces_artifacts=("technical_findings",),
        cost_hint="medium", optional_when="has_jd_requirements"),
    "ProjectAgent": AgentDefinition(
        "ProjectAgent", "项目分析", "项目复杂度、贡献与真实性",
        ("project_analysis",),
        task_prompt=("评估项目复杂度、候选人的个人职责、技术决策和量化结果；"
                     "区分团队成果与个人贡献，并列出需要面试确认的信息。"),
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
        task_prompt=("检查任职、教育和项目时间线以及职责主张的一致性；"
                     "区分明确冲突、口径差异和信息缺失，不得把无法核验的信息写成造假。"),
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
        task_prompt=("逐条核验Tech、Project和Risk产出的核心主张，确认其是否能定位到"
                     "简历、JD、RAG上下文或真实工具证据，并记录冲突和未支持项。"),
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
        task_prompt=("综合已经核验的技术、项目、风险和证据结果，生成唯一的最终候选人评估报告；"
                     "不得引入新的未经核验事实。"),
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
