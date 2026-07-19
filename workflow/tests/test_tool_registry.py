from __future__ import annotations

import asyncio
import importlib
import sys
import types
from pathlib import Path

import pytest


@pytest.fixture
def tool_registry(monkeypatch: pytest.MonkeyPatch):
    workflow_root = Path(__file__).resolve().parents[1]
    monkeypatch.syspath_prepend(str(workflow_root))

    class StructuredTool:
        def __init__(self, name, description="", coroutine=None, metadata=None):
            self.name = name
            self.description = description
            self.coroutine = coroutine
            self.metadata = metadata

        @classmethod
        def from_function(cls, *, coroutine, name, description, args_schema=None):
            return cls(name=name, description=description, coroutine=coroutine)

    tools_module = types.ModuleType("langchain_core.tools")
    tools_module.StructuredTool = StructuredTool
    langchain_core = types.ModuleType("langchain_core")
    monkeypatch.setitem(sys.modules, "langchain_core", langchain_core)
    monkeypatch.setitem(sys.modules, "langchain_core.tools", tools_module)

    fake_pydantic = types.ModuleType("pydantic")

    class BaseModel:
        pass

    def Field(default=None, default_factory=None, **kwargs):
        return default_factory() if default_factory is not None else default

    fake_pydantic.BaseModel = BaseModel
    fake_pydantic.Field = Field
    monkeypatch.setitem(sys.modules, "pydantic", fake_pydantic)

    mcp_tools = []
    fake_mcp = types.ModuleType("app.mcp_registry")

    async def get_mcp_tools():
        return list(mcp_tools)

    fake_mcp.get_mcp_tools = get_mcp_tools
    monkeypatch.setitem(sys.modules, "app.mcp_registry", fake_mcp)

    fake_skills = types.ModuleType("app.skill_registry")
    fake_skills.get_skill_tools = lambda agent_name, whitelist: [
        StructuredTool("execute_skill", "real skill runtime")
    ] if "execute_skill" in whitelist else []
    monkeypatch.setitem(sys.modules, "app.skill_registry", fake_skills)

    fake_tools = types.ModuleType("app.tools")

    async def execute_tool(name, args):
        return {"tool": name, "args": args}

    fake_tools.execute_tool = execute_tool
    monkeypatch.setitem(sys.modules, "app.tools", fake_tools)

    fake_semantics = types.ModuleType("app.tool_semantics")
    fake_semantics.TOOL_SEMANTICS = {}
    fake_semantics.get_tool_semantics = lambda name: {"operation": name}
    monkeypatch.setitem(sys.modules, "app.tool_semantics", fake_semantics)

    sys.modules.pop("app.tool_registry", None)
    module = importlib.import_module("app.tool_registry")
    return types.SimpleNamespace(module=module, StructuredTool=StructuredTool, mcp_tools=mcp_tools)


def test_public_mcp_tool_is_discovered_not_shadowed_by_fake_local_wrapper(tool_registry) -> None:
    real_exa = tool_registry.StructuredTool(
        "web_search_exa",
        "source-backed public search",
        metadata={"mcpServer": "exa", "externalEvidence": {"requiresSourceUrl": True}},
    )
    tool_registry.mcp_tools.append(real_exa)

    tools = asyncio.run(tool_registry.module.build_tools_for_agent("TechEvalAgent", {}))
    by_name = {tool.name: tool for tool in tools}

    assert by_name["web_search_exa"] is real_exa
    assert "milvus_resume_batch_search" in by_name
    assert "execute_skill" in by_name


def test_unavailable_public_mcp_tool_is_not_synthesized_locally(tool_registry) -> None:
    tools = asyncio.run(tool_registry.module.build_tools_for_agent("TechEvalAgent", {}))
    names = {tool.name for tool in tools}

    assert "web_search_exa" not in names
    assert "firecrawl_search" not in names
    assert "search_repositories" not in names


def test_time_mcp_is_not_shadowed_by_unknown_local_execute_tool(tool_registry) -> None:
    real_time = tool_registry.StructuredTool(
        "get_current_time",
        "official time MCP",
        metadata={"mcpServer": "time", "externalEvidence": {"requiresSourceUrl": False}},
    )
    tool_registry.mcp_tools.append(real_time)

    tools = asyncio.run(tool_registry.module.build_tools_for_agent("RiskAgent", {}))

    assert {tool.name: tool for tool in tools}["get_current_time"] is real_time


def test_specialists_have_real_domain_tools_beyond_skill_loading(tool_registry) -> None:
    whitelists = tool_registry.module.AGENT_TOOL_WHITELISTS

    assert "resume_structure_extract" in whitelists["ResumeParseAgent"]
    assert {"milvus_jd_search", "jd_requirements_extract"} <= whitelists["JdMatchAgent"]
    assert "milvus_resume_batch_search" in whitelists["TechEvalAgent"]
    assert "timeline_validator" in whitelists["RiskAgent"]
    assert "evidence_merge" in whitelists["EvidenceFusionAgent"]
