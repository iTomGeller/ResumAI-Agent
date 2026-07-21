from __future__ import annotations

from dataclasses import dataclass, field
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
    timeout_seconds: int = 240
    failure_policy: str = "degrade"          # degrade / skip / abort
    enabled: bool = True
    output_type: str = "findings"


AGENT_DEFINITIONS: Dict[str, AgentDefinition] = {
    "CoordinatorAgent": AgentDefinition(
        "CoordinatorAgent", "协调者", "分析用户目标并动态选择 Agent 流水线",
        ("planning", "routing", "replanning"),
        tools=(), max_iterations=1, max_tool_calls=0, timeout_seconds=60,
        failure_policy="degrade", output_type="plan"),
    "ResumeParserAgent": AgentDefinition(
        "ResumeParserAgent", "简历解析", "简历结构化与事实提取",
        ("resume_parsing",),
        skills=("resume_parsing",),
        tools=("parse_resume",),
        max_iterations=1, max_tool_calls=2, timeout_seconds=150,
        output_type="resume_facts"),
    "JDAnalysisAgent": AgentDefinition(
        "JDAnalysisAgent", "JD 分析", "岗位要求提取与归一化",
        ("jd_analysis",),
        skills=("jd_requirement_analysis",),
        tools=("jd_match_search", "knowledge_search"),
        max_iterations=1, max_tool_calls=3, timeout_seconds=150,
        output_type="jd_requirements"),
    "TechAgent": AgentDefinition(
        "TechAgent", "技术评估", "技能与 JD 匹配、深度信号评估",
        ("tech_evaluation",),
        skills=("java_backend_evaluation", "ai_agent_job_evaluation"),
        tools=("calculate_jd_coverage", "resume_semantic_search", "knowledge_search",
               "mcp_fetch_url"),
        mcp_servers=("fetch",),
        max_iterations=2, max_tool_calls=5, timeout_seconds=240,
        output_type="technical_findings"),
    "ProjectAgent": AgentDefinition(
        "ProjectAgent", "项目分析", "项目复杂度、贡献与真实性",
        ("project_analysis",),
        skills=("project_depth_analysis",),
        tools=("locate_evidence", "resume_semantic_search", "mcp_fetch_url"),
        mcp_servers=("fetch",),
        max_iterations=2, max_tool_calls=5, timeout_seconds=240,
        output_type="project_findings"),
    "RiskAgent": AgentDefinition(
        "RiskAgent", "风险审查", "时间线与履历风险检测",
        ("risk_detection", "timeline_check"),
        skills=("timeline_risk_analysis",),
        tools=("check_timeline", "timeline_validator"),
        max_iterations=1, max_tool_calls=3, timeout_seconds=180,
        output_type="risks"),
    "EvidenceAgent": AgentDefinition(
        "EvidenceAgent", "证据核验", "对结论逐条核验并记录冲突",
        ("evidence_verification",),
        skills=("evidence_verification",),
        tools=("verify_report_evidence", "locate_evidence"),
        max_iterations=1, max_tool_calls=4, timeout_seconds=210,
        output_type="evidence"),
    "ReportAgent": AgentDefinition(
        "ReportAgent", "报告生成", "汇总证据生成最终回答",
        ("report_generation",),
        skills=("report_generation",),
        # knowledge_search / resume_semantic_search: Copilot 追问（followup/
        # quick_answer 只有 ReportAgent）需要对话式 RAG——先查评估标准与简历
        # 证据再回答。
        tools=("validate_report_schema", "knowledge_search",
               "resume_semantic_search"),
        max_iterations=1, max_tool_calls=4, timeout_seconds=240,
        failure_policy="abort", output_type="report"),
    "ResumeOptimizeAgent": AgentDefinition(
        "ResumeOptimizeAgent", "简历优化", "事实不变前提下的改写",
        ("resume_rewrite",),
        skills=("resume_rewrite",),
        tools=("resume_lint", "locate_evidence"),
        max_iterations=2, max_tool_calls=4, timeout_seconds=240,
        output_type="rewrite"),
    "InterviewQuestionAgent": AgentDefinition(
        "InterviewQuestionAgent", "面试追问", "针对风险与缺口生成追问",
        ("interview_questions",),
        skills=("interview_question_generation",),
        tools=(),
        max_iterations=1, max_tool_calls=1, timeout_seconds=150,
        output_type="questions"),
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


default_agent_registry = AgentRegistry()
