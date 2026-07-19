from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Set

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from app.mcp_registry import get_mcp_tools
from app.skill_registry import get_skill_tools
from app.tool_semantics import TOOL_SEMANTICS, get_tool_semantics
from app.tools import execute_tool

logger = logging.getLogger(__name__)
MCP_DISCOVERY_TIMEOUT_SECONDS = 20.0


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

SKILL_TOOLS = {"list_skills", "execute_skill"}
RESUME_EVIDENCE_TOOLS = {
    "milvus_resume_search",
    "milvus_resume_batch_search",
    "mcp_resume_evidence_search",
}
PUBLIC_PROFILE_TOOLS = {
    # Official/hosted MCP tool names.  Discovery still has to return the exact
    # tool and attach its source policy before it becomes callable.
    "mcp_external_profile_search",
    "web_search_exa",
    "web_fetch_exa",
    "firecrawl_search",
    "firecrawl_scrape",
    "search_users",
    "search_repositories",
    "get_file_contents",
    "list_commits",
}
MCP_DISCOVERED_TOOL_NAMES = {
    "mcp_resume_evidence_search",
    "mcp_external_profile_search",
    "get_current_time",
    "convert_time",
    *PUBLIC_PROFILE_TOOLS,
}

AGENT_TOOL_WHITELISTS: Dict[str, Set[str]] = {
    "IntentAgent": set(SKILL_TOOLS),
    "ResumeParseAgent": {"resume_structure_extract", *SKILL_TOOLS},
    "JdMatchAgent": {
        "milvus_jd_search",
        "jd_requirements_extract",
        *SKILL_TOOLS,
    },
    "TechEvalAgent": {
        *RESUME_EVIDENCE_TOOLS,
        *PUBLIC_PROFILE_TOOLS,
        *SKILL_TOOLS,
    },
    "ProjectEvalAgent": {
        *RESUME_EVIDENCE_TOOLS,
        *PUBLIC_PROFILE_TOOLS,
        *SKILL_TOOLS,
    },
    "RiskAgent": {
        "timeline_validator",
        "get_current_time",
        *RESUME_EVIDENCE_TOOLS,
        *SKILL_TOOLS,
    },
    "EvidenceFusionAgent": {"evidence_merge", *SKILL_TOOLS},
    "ReportAgent": set(SKILL_TOOLS),
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
        if name in MCP_DISCOVERED_TOOL_NAMES or name in SKILL_TOOLS:
            continue
        add_tool(_make_tool(name, context))

    mcp_tools: List[StructuredTool] = []
    requested_mcp = whitelist & MCP_DISCOVERED_TOOL_NAMES
    if requested_mcp:
        try:
            mcp_tools = await asyncio.wait_for(
                get_mcp_tools(),
                timeout=MCP_DISCOVERY_TIMEOUT_SECONDS,
            )
            for mcp_tool in mcp_tools:
                if mcp_tool.name in requested_mcp:
                    add_tool(mcp_tool)
        except Exception as exc:
            logger.warning("MCP tools unavailable: %s", exc)

    missing_mcp = requested_mcp - {t.name for t in mcp_tools}
    if missing_mcp:
        logger.warning("MCP whitelist tools unavailable via protocol: %s", sorted(missing_mcp))

    for skill_tool in get_skill_tools(agent_name, whitelist):
        add_tool(skill_tool)
    return tools


def semantics_for(tool_name: str) -> Dict[str, Any]:
    return get_tool_semantics(tool_name)
