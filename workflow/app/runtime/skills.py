from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass(frozen=True)
class SkillDefinition:
    skill_id: str
    name: str
    version: str
    description: str
    applicable_conditions: tuple
    instructions: str
    positive_examples: tuple = ()
    negative_examples: tuple = ()
    required_tools: tuple = ()
    required_mcp: tuple = ()
    output_requirements: str = ""
    evaluation_metrics: tuple = ()
    status: str = "ACTIVE"

    @property
    def hash(self) -> str:
        return hashlib.sha256(self.instructions.encode("utf-8")).hexdigest()[:12]


_SKILLS: List[SkillDefinition] = [
    SkillDefinition(
        "resume_parsing", "简历结构化解析", "v1",
        "从原始简历文本/PDF 提取结构化事实",
        ("run_start", "resume_available"),
        "优先使用 parse_resume 沙箱工具获得客观结构；只补充工具遗漏的字段；"
        "每个字段标注来源行号；空 PDF/扫描件直接报告解析失败而不是编造内容。",
        positive_examples=("技能'Kafka'来自第12行'基于Kafka实现异步解耦'",),
        negative_examples=("凭空补全教育经历",),
        required_tools=("parse_resume",),
        output_requirements="resumeFacts JSON：skills/projects/experiences/education",
        evaluation_metrics=("parsing_success_rate",)),
    SkillDefinition(
        "jd_requirement_analysis", "JD 需求提取", "v1",
        "提取硬性要求/加分项/关键词/级别",
        ("jd_available", "jd_evaluation"),
        "区分硬性要求与加分项；每条要求归一化为可比对的技能短语；"
        "没有 JD 时使用 jd_match_search 并明确标注来源是检索。",
        required_tools=("jd_match_search",),
        output_requirements="jdRequirements JSON：required/preferred/keywords/level",
        evaluation_metrics=("jd_coverage",)),
    SkillDefinition(
        "java_backend_evaluation", "Java 后端岗位评估", "v1",
        "针对 Java 后端岗位的技术评估侧重",
        ("job_focus:java_backend",),
        "重点核查：并发与 JVM 证据、数据库与缓存实践、消息中间件、分布式一致性、"
        "性能指标真实性；只在技能栏出现而无项目支撑的技能降权并提示面试验证。",
        required_tools=("calculate_jd_coverage", "resume_semantic_search"),
        output_requirements="technicalFindings 按 JD 条目对齐",
        evaluation_metrics=("evidence_support_ratio",)),
    SkillDefinition(
        "ai_agent_job_evaluation", "AI Agent 岗位评估", "v1",
        "针对 LLM/Agent 岗位的技术评估侧重",
        ("job_focus:ai_agent",),
        "重点核查：Agent 编排/工具调用/RAG/评测方法论的实践深度；分辨 Demo 和生产化证据；"
        "关注 prompt 管理、评测集、可观测性等工程化信号。",
        required_tools=("calculate_jd_coverage", "resume_semantic_search"),
        output_requirements="technicalFindings 按 JD 条目对齐",
        evaluation_metrics=("evidence_support_ratio",)),
    SkillDefinition(
        "project_depth_analysis", "项目深度分析", "v1",
        "项目复杂度/贡献/深度评估",
        ("project_analysis", "full_evaluation"),
        "对每个项目回答：解决什么问题、个人负责边界、技术难点与权衡、量化结果是否可信；"
        "指标必须能在原文定位（locate_evidence），否则标记为待核实。",
        required_tools=("locate_evidence",),
        output_requirements="projectFindings 列表，含 blindSpots",
        evaluation_metrics=("unsupported_claim_rate",)),
    SkillDefinition(
        "timeline_risk_analysis", "时间线风险分析", "v1",
        "履历时间线一致性检查",
        ("timeline_check", "risk_check", "full_evaluation"),
        "以 check_timeline 工具输出为准描述重叠/空窗/未来时间；"
        "合理重叠（实习+在校）不判为高风险；高风险必须给出原文行号。",
        required_tools=("check_timeline",),
        output_requirements="risks 列表，含 severity 与 evidence",
        evaluation_metrics=("timeline_precision", "timeline_recall")),
    SkillDefinition(
        "evidence_verification", "证据核验", "v1",
        "对结论逐条核验证据",
        ("evidence_check", "full_evaluation", "strict_evidence"),
        "对共享状态所有 claims 运行 verify_report_evidence；"
        "unsupported 结论写入 conflicts 并附原因（无原文/数字对不上/语义弱匹配）；"
        "不得删除他人结论，只能标记。",
        required_tools=("verify_report_evidence", "locate_evidence"),
        output_requirements="evidence + conflicts 更新",
        evaluation_metrics=("evidence_support_ratio", "unsupported_claim_rate")),
    SkillDefinition(
        "resume_rewrite", "简历改写", "v1",
        "项目描述与整体改写",
        ("project_rewrite", "resume_optimize"),
        "STAR 结构 + 量化优先；保持事实不变（时间/公司/数字不可改动/新增）；"
        "每条改写给出改动理由；改写后跑 resume_lint 并把剩余问题列出。",
        required_tools=("resume_lint",),
        output_requirements="rewrittenSections 前后对照",
        evaluation_metrics=("lint_score",)),
    SkillDefinition(
        "interview_question_generation", "面试追问生成", "v1",
        "基于风险与证据缺口生成追问",
        ("interview_questions",),
        "每题包含：问题、考察点、好答案信号、追问来源（风险/缺口/项目模糊点）；"
        "优先追问 unsupported 结论与高风险时间线。",
        output_requirements="questions 列表",
        evaluation_metrics=("question_specificity",)),
    SkillDefinition(
        "report_generation", "报告生成", "v1",
        "汇总生成最终回答/报告",
        ("always_for_report",),
        "结论分为：确定（有证据）/不确定（证据不足或冲突）；引用来源 Agent 与原文位置；"
        "评分与推荐结论一致；用 validate_report_schema 自检结构。",
        required_tools=("validate_report_schema",),
        output_requirements="Markdown 回答 + 结构化 report JSON",
        evaluation_metrics=("recommendation_accuracy",)),
]


class SkillManager:
    """Versioned skill registry with condition-based dynamic loading. Only
    skills applicable to the current task/policy are injected into context;
    versions are recorded into the trajectory for benchmark binding."""

    def __init__(self) -> None:
        self._by_id: Dict[str, Dict[str, SkillDefinition]] = {}
        for skill in _SKILLS:
            self._by_id.setdefault(skill.skill_id, {})[skill.version] = skill

    def get(self, skill_id: str, version: Optional[str] = None) -> SkillDefinition:
        versions = self._by_id.get(skill_id)
        if not versions:
            raise KeyError(f"unknown skill: {skill_id}")
        if version and version in versions:
            return versions[version]
        active = [s for s in versions.values() if s.status == "ACTIVE"]
        return sorted(active or list(versions.values()), key=lambda s: s.version)[-1]

    def list_ids(self) -> List[str]:
        return list(self._by_id.keys())

    def select_for(self, *, agent_id: str, run_type: str, job_focus: Optional[str],
                   overrides: Dict[str, str]) -> List[SkillDefinition]:
        base_map: Dict[str, List[str]] = {
            "ResumeParserAgent": ["resume_parsing"],
            "JDAnalysisAgent": ["jd_requirement_analysis"],
            "TechAgent": ["java_backend_evaluation" if job_focus == "java_backend"
                          else "ai_agent_job_evaluation" if job_focus == "ai_agent"
                          else "jd_requirement_analysis"],
            "ProjectAgent": ["project_depth_analysis"],
            "RiskAgent": ["timeline_risk_analysis"],
            "EvidenceAgent": ["evidence_verification"],
            "ReportAgent": ["report_generation"],
            "ResumeOptimizeAgent": ["resume_rewrite"],
            "InterviewQuestionAgent": ["interview_question_generation"],
            "CoordinatorAgent": [],
        }
        skill_ids = list(base_map.get(agent_id, []))
        override = overrides.get(agent_id)
        if override and override in self._by_id and override not in skill_ids:
            skill_ids.insert(0, override)
        if run_type in ("timeline_check", "risk_check") and agent_id == "RiskAgent" \
                and "timeline_risk_analysis" not in skill_ids:
            skill_ids.append("timeline_risk_analysis")
        skills = []
        for skill_id in skill_ids[:2]:  # never flood context with every skill
            try:
                skills.append(self.get(skill_id))
            except KeyError:
                continue
        return skills

    @staticmethod
    def render(skills: List[SkillDefinition]) -> str:
        blocks = []
        for skill in skills:
            blocks.append(
                f"技能 {skill.name}（{skill.skill_id}@{skill.version}）：\n"
                f"{skill.instructions}\n输出要求：{skill.output_requirements}")
        return "\n\n".join(blocks)

    def versions_used(self, selections: Dict[str, List[SkillDefinition]]) -> Dict[str, str]:
        used = {}
        for agent_id, skills in selections.items():
            for skill in skills:
                used[skill.skill_id] = f"{skill.version}#{skill.hash}"
        return used


default_skill_manager = SkillManager()
