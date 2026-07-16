from __future__ import annotations

import logging
from typing import Any, Dict, List, Set

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from app.mcp_registry import get_mcp_tools
from app.skill_registry import get_skill_tools
from app.tool_semantics import TOOL_SEMANTICS, get_tool_semantics
from app.tools import execute_tool

logger = logging.getLogger(__name__)


class ResumeSearchArgs(BaseModel):
    query: str = Field("", description="检索关键词或问题")
    topK: int = Field(5, description="返回条数")


class BatchResumeSearchArgs(BaseModel):
    queries: list[str] = Field(default_factory=list, description="批量检索问题，最多 4 个")
    topK: int = Field(5, description="每个 query 返回条数")


class JdSearchArgs(BaseModel):
    query: str = Field("", description="候选人核心技能或简历摘要")
    topK: int = Field(3, description="返回岗位数量")


class JdExtractArgs(BaseModel):
    jdMatchJson: str = Field("", description="milvus_jd_search 返回的 JSON")


class TextOnlyArgs(BaseModel):
    query: str = Field("", description="输入文本")


class ResumeTextArgs(BaseModel):
    resumeText: str = Field("", description="简历全文")


class EvidenceMergeArgs(BaseModel):
    techResult: str = Field("", description="技术评估结果 JSON")
    projectResult: str = Field("", description="项目评估结果 JSON")
    riskResult: str = Field("", description="风险评估结果 JSON")


class SkillExecuteArgs(BaseModel):
    skillName: str = Field("", description="Skill 名称")
    task: str = Field("", description="任务描述")


TOOL_ARG_SCHEMAS: Dict[str, type[BaseModel]] = {
    "milvus_resume_search": ResumeSearchArgs,
    "milvus_resume_batch_search": BatchResumeSearchArgs,
    "milvus_jd_search": JdSearchArgs,
    "jd_requirements_extract": JdExtractArgs,
    "github_enrichment": ResumeTextArgs,
    "timeline_validator": ResumeTextArgs,
    "resume_structure_extract": ResumeTextArgs,
    "evidence_merge": EvidenceMergeArgs,
    "execute_skill": SkillExecuteArgs,
}

TOOL_DESCRIPTIONS: Dict[str, str] = {
    "milvus_resume_search": "Milvus 简历 chunk 单 query 检索，返回 hitCount/topScore/chunks",
    "milvus_resume_batch_search": "Milvus 简历批量检索，一次传入最多 4 个 query，返回 resultsByQuery",
    "milvus_jd_search": "Milvus JD 岗位匹配检索，query 可为候选人技能摘要，resumeText 从上下文注入",
    "jd_requirements_extract": "从 milvus_jd_search 结果 JSON 提取岗位要求与缺口",
    "github_enrichment": "外部 profile enrichment（GitHub 等）",
    "timeline_validator": "简历时间线一致性校验",
    "resume_structure_extract": "简历结构化分段提取",
    "evidence_merge": "融合技术/项目/风险三方面证据",
    "execute_skill": "加载 Skill 指令，由当前 Agent 在后续 LLM round 中应用",
}

AGENT_TOOL_WHITELISTS: Dict[str, Set[str]] = {
    "IntentAgent": set(),
    "ResumeParseAgent": set(),
    "JdMatchAgent": set(),
    "TechEvalAgent": set(),
    "ProjectEvalAgent": set(),
    "RiskAgent": set(),
    "EvidenceFusionAgent": set(),
    "ReportAgent": set(),
}


def _make_tool(tool_name: str, context: Dict[str, Any]) -> StructuredTool:
    async def _run(**kwargs: Any) -> str:
        merged = dict(context)
        merged.update(kwargs)
        return await execute_tool(tool_name, merged)

    return StructuredTool.from_function(
        coroutine=_run,
        name=tool_name,
        description=TOOL_DESCRIPTIONS.get(tool_name, f"Tool {tool_name}"),
        args_schema=TOOL_ARG_SCHEMAS.get(tool_name),
    )


async def build_tools_for_agent(agent_name: str, context: Dict[str, Any]) -> List[StructuredTool]:
    whitelist = AGENT_TOOL_WHITELISTS.get(agent_name, set())
    tools: List[StructuredTool] = []
    seen: set[str] = set()

    def add_tool(tool: StructuredTool) -> None:
        if tool.name in seen:
            return
        seen.add(tool.name)
        tools.append(tool)

    for name in sorted(whitelist):
        if name.startswith("mcp_") or name in ("list_skills", "execute_skill"):
            continue
        add_tool(_make_tool(name, context))

    mcp_tools: List[StructuredTool] = []
    try:
        mcp_tools = await get_mcp_tools()
        for mcp_tool in mcp_tools:
            if mcp_tool.name in whitelist:
                add_tool(mcp_tool)
    except Exception as exc:
        logger.warning("MCP tools unavailable: %s", exc)

    missing_mcp = {name for name in whitelist if name.startswith("mcp_")} - {t.name for t in mcp_tools}
    if missing_mcp:
        logger.warning("MCP whitelist tools unavailable via protocol: %s", sorted(missing_mcp))

    for skill_tool in get_skill_tools(whitelist):
        add_tool(skill_tool)
    return tools


def semantics_for(tool_name: str) -> Dict[str, Any]:
    return get_tool_semantics(tool_name)
