from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

WORKFLOW_ROOT = Path(__file__).resolve().parents[1]
if str(WORKFLOW_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKFLOW_ROOT))

from app.runtime.agents import default_agent_registry
from app.runtime.builtin_tools import BuiltinToolRegistry
from app.runtime.coordinator import Coordinator
from app.runtime.events import NullEmitter
from app.runtime.executor import RunExecutor
from app.runtime.llm import LlmToolCall, LlmTurn
from app.runtime.mcp_registry import (
    DEGRADED_REPROBE_TTL_S,
    McpError,
    McpRegistry,
    McpServerHealth,
    McpToolInfo,
    PROTOCOL_VERSION,
    StreamableHttpMcpClient,
)
from app.runtime.memory import NullMemoryClient
from app.runtime.models import AgentRunRequest, PolicyBundle, RunBudget
from app.runtime.skills import SkillManager
from app.runtime.tools import ToolExecutor


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def test_skill_progressive_disclosure_and_resource_on_demand(tmp_path):
    root = tmp_path / "skills"
    package = root / "demo-skill"
    references = package / "references"
    references.mkdir(parents=True)
    (package / "SKILL.md").write_text(
        "---\n"
        "name: demo-skill\n"
        "description: Metadata visible at startup.\n"
        "version: v7\n"
        "---\n\n"
        "BODY_SENTINEL: full instructions only after activation.\n"
        "See references/details.md when needed.\n",
        encoding="utf-8")
    (references / "details.md").write_text(
        "RESOURCE_SENTINEL: loaded independently.", encoding="utf-8")

    manager = SkillManager(root)
    metadata = manager.get("demo-skill")
    assert metadata.instructions == ""
    assert metadata.hash == "not-loaded"
    assert "BODY_SENTINEL" not in manager.render([metadata])

    loaded = manager.load("demo-skill")
    assert loaded.loaded is True
    assert "BODY_SENTINEL" in loaded.instructions
    assert loaded.hash != "not-loaded"
    assert "RESOURCE_SENTINEL" not in loaded.instructions
    assert loaded.resource_paths == ("references/details.md",)

    resource = manager.read_resource(
        "demo-skill", "references/details.md")
    assert "RESOURCE_SENTINEL" in resource


class _LiveMcpClient:
    def __init__(self, *, fail: bool = False):
        self.fail = fail
        self.calls = []

    async def call_tool(self, name, arguments):
        self.calls.append((name, dict(arguments)))
        if self.fail:
            return {
                "success": False,
                "isError": True,
                "status": "DOWN",
                "text": "provider unavailable",
            }
        return {
            "success": True,
            "isError": False,
            "text": "source-backed result",
            "structuredContent": {
                "items": [{"url": "https://example.test/evidence"}]},
        }


def _attach_demo_mcp(tools: ToolExecutor, client: _LiveMcpClient) -> McpRegistry:
    registry = McpRegistry(config={
        "mcpServers": {},
        "optionalMcpServers": {},
        "agentToolRouting": {"ProjectAgent": ["demo.search"]},
    })
    registry.health["demo"] = McpServerHealth(
        name="demo", status="AVAILABLE", transport="streamable-http",
        tools=["demo.search"])
    registry.tools["demo.search"] = McpToolInfo(
        server="demo",
        name="remote_search",
        catalog_name="demo.search",
        description="REMOTE_DESCRIPTION_SENTINEL",
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "model query"}},
            "required": ["query"],
        },
        protocol_version="2025-11-25")
    registry._http_clients["demo"] = client
    tools.attach_mcp(registry)
    return registry


def _attach_deepwiki_mcp(tools: ToolExecutor,
                         client: _LiveMcpClient) -> McpRegistry:
    registry = McpRegistry(config={
        "mcpServers": {},
        "optionalMcpServers": {},
        "agentToolRouting": {
            "ProjectAgent": ["deepwiki.ask_question"],
        },
    })
    registry.health["deepwiki"] = McpServerHealth(
        name="deepwiki", status="AVAILABLE",
        transport="streamable-http",
        tools=["deepwiki.ask_question"])
    registry.tools["deepwiki.ask_question"] = McpToolInfo(
        server="deepwiki",
        name="ask_question",
        catalog_name="deepwiki.ask_question",
        description="Ask DeepWiki about one public repository",
        input_schema={
            "type": "object",
            "properties": {
                "repoName": {"type": "string"},
                "question": {"type": "string"},
            },
            "required": ["repoName", "question"],
        },
        # Some remote servers describe their native JSON-RPC result rather
        # than the normalized runtime envelope returned to ToolExecutor.
        output_schema={
            "type": "object", "required": ["result"],
            "properties": {"result": {"type": "object"}},
        },
        protocol_version="2025-06-18")
    registry._http_clients["deepwiki"] = client
    tools.attach_mcp(registry)
    return registry


def test_remote_description_schema_enter_model_catalog_and_id_is_preserved():
    emitter = NullEmitter()
    tools = ToolExecutor(
        emitter, RunBudget(), BuiltinToolRegistry(),
        max_tool_calls_run=5, tool_timeout_seconds=10,
        run_context={})
    client = _LiveMcpClient()
    _attach_demo_mcp(tools, client)

    catalog = tools.catalog_for_agent("ProjectAgent", [])
    model_tools, aliases = tools.openai_tools(catalog)
    assert len(model_tools) == 1
    function = model_tools[0]["function"]
    assert function["description"] == "REMOTE_DESCRIPTION_SENTINEL"
    assert function["parameters"]["required"] == ["query"]
    assert aliases[function["name"]] == "demo.search"
    assert all("cn-web" not in item["function"]["name"]
               for item in model_tools)

    call = run(tools.execute(
        "ProjectAgent", "demo.search",
        {"query": "arguments made by model"},
        tool_call_id="provider-call-42"))
    assert call.status == "SUCCEEDED"
    assert client.calls == [
        ("remote_search", {"query": "arguments made by model"})]
    lifecycle = [
        event for event in emitter.events
        if event["eventType"] in {"tool.started", "tool.completed"}]
    assert [event["payload"]["toolCallId"] for event in lifecycle] == [
        "provider-call-42", "provider-call-42"]
    assert all(event["payload"].get("occurredAt") for event in lifecycle)
    assert lifecycle[0]["payload"].get("startedAt")
    assert lifecycle[1]["payload"].get("endedAt")


def test_deepwiki_catalog_and_call_require_declared_candidate_repository():
    without_repo = ToolExecutor(
        NullEmitter(), RunBudget(), BuiltinToolRegistry(),
        max_tool_calls_run=5, tool_timeout_seconds=10,
        run_context={"resumeText": "项目经历：支付网关"})
    hidden_client = _LiveMcpClient()
    _attach_deepwiki_mcp(without_repo, hidden_client)
    assert without_repo.catalog_for_agent("ProjectAgent", []) == []

    with_repo = ToolExecutor(
        NullEmitter(), RunBudget(), BuiltinToolRegistry(),
        max_tool_calls_run=5, tool_timeout_seconds=10,
        run_context={
            "resumeText": (
                "项目经历：https://github.com/Acme/Payment-Gateway"),
            "userMessage": "",
            "recentMessages": [],
        })
    client = _LiveMcpClient()
    _attach_deepwiki_mcp(with_repo, client)
    catalog = with_repo.catalog_for_agent("ProjectAgent", [])
    assert [item["name"] for item in catalog] == [
        "deepwiki.ask_question"]

    rejected = run(with_repo.execute(
        "ProjectAgent", "deepwiki.ask_question",
        {"repoName": "other/unrelated", "question": "architecture?"},
        tool_call_id="deepwiki-rejected"))
    assert rejected.status == "REJECTED"
    assert client.calls == []

    accepted = run(with_repo.execute(
        "ProjectAgent", "deepwiki.ask_question",
        {"repoName": "acme/payment-gateway", "question": "architecture?"},
        tool_call_id="deepwiki-accepted"))
    assert accepted.status == "SUCCEEDED"
    policy = accepted.result["evidencePolicy"]
    assert policy["evidenceUse"] == "context_only"
    assert policy["candidateFactEligible"] is False
    assert policy["sourceUrl"] == (
        "https://github.com/Acme/Payment-Gateway")
    assert client.calls == [(
        "ask_question",
        {"repoName": "Acme/Payment-Gateway", "question": "architecture?"})]


def test_deepwiki_wiki_text_is_context_not_candidate_evidence():
    request = AgentRunRequest(
        runId="r-deepwiki", conversationId="c-deepwiki",
        traceId="t-deepwiki", runType="full_evaluation",
        userMessage="核验 https://github.com/acme/payment-gateway",
        resumeText="项目经历：支付网关",
        jobDescription="Python backend")
    executor = RunExecutor(
        request, NullEmitter(), memory=NullMemoryClient(),
        builtin_tools=BuiltinToolRegistry(), llm=_NativeMcpLlm())
    client = _LiveMcpClient()
    _attach_deepwiki_mcp(executor.tools, client)

    call = run(executor.tools.execute(
        "ProjectAgent", "deepwiki.ask_question",
        {"repoName": "acme/payment-gateway", "question": "architecture?"},
        tool_call_id="deepwiki-context"))
    assert call.status == "SUCCEEDED"
    run(executor._record_tool_success(
        "ProjectAgent", "deepwiki.ask_question",
        {"repoName": "acme/payment-gateway", "question": "architecture?"},
        call.result, tool_call_id=call.tool_call_id))
    run(executor._record_tool_success(
        "ProjectAgent", "deepwiki.ask_question",
        {"repoName": "acme/payment-gateway", "question": "ownership?"},
        call.result, tool_call_id="deepwiki-context-2"))

    assert not executor.state.artifact("mcpEvidence")
    context = executor.state.artifact("mcpContext")
    assert len(context) == 2
    assert [item["toolCallId"] for item in context] == [
        "deepwiki-context", "deepwiki-context-2"]
    assert context[0]["evidenceUse"] == "context_only"
    assert context[0]["candidateFactEligible"] is False
    assert context[0]["sourceUrls"] == [
        "https://github.com/acme/payment-gateway"]


def test_empty_live_tools_list_never_creates_a_hardcoded_fetch_alias():
    emitter = NullEmitter()
    tools = ToolExecutor(
        emitter, RunBudget(), BuiltinToolRegistry(),
        max_tool_calls_run=5, tool_timeout_seconds=10,
        run_context={})
    registry = McpRegistry(config={
        "mcpServers": {},
        "optionalMcpServers": {},
        "agentToolRouting": {"ProjectAgent": ["fetch.fetch"]},
    })
    registry.health["fetch"] = McpServerHealth(
        name="fetch", status="AVAILABLE", transport="stdio", tools=[])
    # A successful initialize with an empty tools/list still has a live
    # client.  It must not cause a locally invented schema to enter the model.
    registry._stdio_clients["fetch"] = object()

    assert tools.attach_mcp(registry) == 0
    assert "mcp_fetch_url" not in tools.definitions
    assert registry.tools_for_agent("ProjectAgent") == []
    assert all(
        definition.kind != "mcp"
        for definition in tools.definitions.values())


class _NativeMcpLlm:
    def __init__(self):
        self.turn = 0
        self.seen_tools = []
        self.tool_choice = None

    async def chat_turn(self, messages, *, agent_id, purpose="",
                        max_tokens=2048, tools=None, tool_choice=None,
                        use_quality=False):
        self.turn += 1
        self.seen_tools = list(tools or [])
        self.tool_choice = tool_choice
        if self.turn == 1:
            remote = next(
                item["function"] for item in self.seen_tools
                if item["function"].get("description")
                == "REMOTE_DESCRIPTION_SENTINEL")
            args = {"query": "model-generated precise project query"}
            return LlmTurn(
                content="",
                tool_calls=[LlmToolCall(
                    tool_call_id="provider-native-7",
                    name=remote["name"],
                    arguments=args,
                    raw_arguments=json.dumps(args))],
                finish_reason="tool_calls")
        decision = {
            "thought": "observed real MCP result",
            "output": {
                "summary": "project evidence checked",
                "claims": [{
                    "section": "project_findings",
                    "value": [{"text": "source-backed result"}],
                }],
                "evidence": [],
                "confidence": 0.7,
            },
            "done": True,
        }
        return LlmTurn(
            content="",
            tool_calls=[LlmToolCall(
                tool_call_id="provider-final-8",
                name="emit_decision",
                arguments=decision,
                raw_arguments=json.dumps(decision))],
            finish_reason="tool_calls")


def test_native_model_proposes_mcp_arguments_and_trace_chain():
    request = AgentRunRequest(
        runId="r-native", conversationId="c-native", traceId="t-native",
        runType="full_evaluation", userMessage="核验这个公开项目",
        resumeText=(
            "项目经历\n公开项目 Example\n"
            "https://example.test/repo\nPython FastAPI"),
        jobDescription="Python backend")
    emitter = NullEmitter(request.runId, request.conversationId, request.traceId)
    llm = _NativeMcpLlm()
    executor = RunExecutor(
        request, emitter, memory=NullMemoryClient(),
        builtin_tools=BuiltinToolRegistry(), llm=llm)
    client = _LiveMcpClient()
    _attach_demo_mcp(executor.tools, client)
    execute_flags = []
    original_execute = executor.tools.execute

    async def audited_execute(*args, **kwargs):
        execute_flags.append(kwargs.get("enable_rewrite"))
        return await original_execute(*args, **kwargs)

    executor.tools.execute = audited_execute
    executor.state.apply_artifacts({
        "resumeFacts": {"projects": [{"name": "Example"}]},
    })

    output = run(executor._run_agent(
        default_agent_registry.get("ProjectAgent")))
    assert output.summary == "project evidence checked"
    assert llm.tool_choice == "auto"
    assert client.calls == [(
        "remote_search",
        {"query": "model-generated precise project query"})]
    assert len(execute_flags) == 3 and all(
        flag is False for flag in execute_flags), (
        "native model-authored MCP parameters must execute verbatim")

    chain = [
        event for event in emitter.events
        if event.get("payload", {}).get("toolCallId") == "provider-native-7"]
    stages = [event.get("payload", {}).get("lifecycleStage")
              for event in chain]
    assert stages.index("CATALOG_EXPOSED") < stages.index("LLM_PROPOSED")
    assert stages.index("LLM_PROPOSED") < stages.index("EXECUTION_STARTED")
    assert stages.index("EXECUTION_STARTED") < stages.index("RESULT")
    round_ids = {
        event.get("payload", {}).get("roundId") for event in chain
        if event.get("payload", {}).get("roundId")
    }
    assert len(round_ids) == 1
    round_id = next(iter(round_ids))
    contexts = [
        event for event in emitter.events
        if event["eventType"] == "llm.context.attached"
        and event.get("payload", {}).get("roundId") == round_id
    ]
    assert len(contexts) == 1
    assert any(
        ref.get("mcpServer") == "demo"
        for ref in contexts[0]["payload"]["toolCatalogRefs"])
    assert contexts[0]["payload"]["contextRole"] == "MODEL_INPUT"
    evidence = executor.state.artifact("mcpEvidence")
    assert evidence and evidence[0]["status"] == "SUCCEEDED"
    assert evidence[0]["toolCallId"] == "provider-native-7"
    run(executor._record_tool_success(
        "ProjectAgent", "demo.search", {"query": "second query"},
        {
            "success": True,
            "structuredContent": {
                "items": [{"url": "https://example.test/evidence-2"}]},
        },
        tool_call_id="provider-native-repeat"))
    evidence = executor.state.artifact("mcpEvidence")
    assert len(evidence) == 2
    assert [item["toolCallId"] for item in evidence] == [
        "provider-native-7", "provider-native-repeat"]


class _OptionalCatalogLlm:
    def __init__(self):
        self.tool_choice = None
        self.tool_names = []

    async def chat_turn(self, messages, *, agent_id, purpose="",
                        max_tokens=2048, tools=None, tool_choice=None,
                        use_quality=False):
        self.tool_choice = tool_choice
        self.tool_names = [
            item["function"]["name"] for item in (tools or [])]
        decision = {
            "thought": "the resume already contains enough evidence",
            "output": {
                "summary": "finished without unnecessary external lookup",
                "claims": [],
                "evidence": [],
                "confidence": 0.7,
            },
            "done": True,
        }
        return LlmTurn(
            content="",
            tool_calls=[LlmToolCall(
                tool_call_id="optional-final",
                name="emit_decision",
                arguments=decision,
                raw_arguments=json.dumps(decision))],
            finish_reason="tool_calls")


def test_no_url_project_receives_live_mcp_and_skill_catalog_but_may_finish():
    request = AgentRunRequest(
        runId="r-optional-catalog", conversationId="c-optional-catalog",
        traceId="t-optional-catalog", runType="full_evaluation",
        resumeText="项目经历\n支付平台\nJava Spring Redis",
        jobDescription="Java backend")
    llm = _OptionalCatalogLlm()
    executor = RunExecutor(
        request, NullEmitter(), memory=NullMemoryClient(),
        builtin_tools=BuiltinToolRegistry(), llm=llm)
    client = _LiveMcpClient()
    _attach_demo_mcp(executor.tools, client)
    executor.state.apply_artifacts({
        "resumeFacts": {"projects": [{"name": "支付平台"}]},
    })

    output = run(executor._run_agent(
        default_agent_registry.get("ProjectAgent")))

    assert output.summary == "finished without unnecessary external lookup"
    assert llm.tool_choice == "auto"
    assert "load_skill" in llm.tool_names
    assert "emit_decision" in llm.tool_names
    assert any(
        name not in {"load_skill", "read_skill_resource", "emit_decision",
                     "locate_evidence", "resume_semantic_search"}
        for name in llm.tool_names), "a live MCP schema must reach the model"
    assert client.calls == [], "catalog exposure must not force execution"


class _SkillThenNativeMcpLlm:
    """Reproduces the production external-URL sequence.

    Deterministic preflight gathers local evidence. The model may then load the
    selected Skill, choose MCP from its live schema, and emit the result.
    """

    def __init__(self):
        self.turn = 0
        self.tool_choices = []

    async def chat_turn(self, messages, *, agent_id, purpose="",
                        max_tokens=2048, tools=None, tool_choice=None,
                        use_quality=False):
        self.turn += 1
        self.tool_choices.append(tool_choice)
        available = list(tools or [])
        if self.turn == 1:
            arguments = {
                "skill_id": "retrieve-public-candidate-evidence"}
            return LlmTurn(
                content="",
                tool_calls=[LlmToolCall(
                    tool_call_id="load-external-evidence-skill",
                    name="load_skill",
                    arguments=arguments,
                    raw_arguments=json.dumps(arguments))],
                finish_reason="tool_calls")
        if self.turn == 2:
            remote = next(
                item["function"] for item in available
                if item["function"].get("description")
                == "REMOTE_DESCRIPTION_SENTINEL")
            arguments = {"query": "candidate-declared repository"}
            return LlmTurn(
                content="",
                tool_calls=[LlmToolCall(
                    tool_call_id="model-selected-mcp-after-skill",
                    name=remote["name"],
                    arguments=arguments,
                    raw_arguments=json.dumps(arguments))],
                finish_reason="tool_calls")
        decision = {
            "thought": "loaded policy then observed the live MCP result",
            "output": {
                "summary": "external evidence checked",
                "claims": [],
                "evidence": [],
                "confidence": 0.7,
            },
            "done": True,
        }
        return LlmTurn(
            content="",
            tool_calls=[LlmToolCall(
                tool_call_id="final-after-mcp",
                name="emit_decision",
                arguments=decision,
                raw_arguments=json.dumps(decision))],
            finish_reason="tool_calls")


def test_external_url_budget_allows_progressive_skill_then_native_mcp():
    request = AgentRunRequest(
        runId="r-skill-mcp", conversationId="c-skill-mcp",
        traceId="t-skill-mcp", runType="project_analysis",
        userMessage="核验简历中声明的公开仓库",
        resumeText=(
            "项目经历\n公开项目 Example\n"
            "https://example.test/repo\nPython FastAPI"),
        jobDescription="Python backend")
    emitter = NullEmitter(request.runId, request.conversationId, request.traceId)
    llm = _SkillThenNativeMcpLlm()
    executor = RunExecutor(
        request, emitter, memory=NullMemoryClient(),
        builtin_tools=BuiltinToolRegistry(), llm=llm)
    client = _LiveMcpClient()
    _attach_demo_mcp(executor.tools, client)
    executor.budget_plan["ProjectAgent"] = {
        "llmQuota": 3,
        "actionTurnQuota": 2,
        "toolQuota": 4,
    }
    executor.state.apply_artifacts({
        "resumeFacts": {"projects": [{"name": "Example"}]},
    })

    output = run(executor._run_agent(
        default_agent_registry.get("ProjectAgent")))

    assert output.summary == "external evidence checked"
    assert llm.turn == 3
    assert llm.tool_choices[:2] == ["auto", "auto"]
    assert client.calls == [(
        "remote_search", {"query": "candidate-declared repository"})]
    counters = executor.agent_counters["ProjectAgent"]
    assert counters["actionTurns"] == 2
    assert counters["toolCalls"] == 4
    mcp_chain = [
        event["payload"]["lifecycleStage"]
        for event in emitter.events
        if event.get("payload", {}).get("toolCallId")
        == "model-selected-mcp-after-skill"
    ]
    assert mcp_chain == [
        "CATALOG_EXPOSED", "LLM_PROPOSED",
        "EXECUTION_STARTED", "RESULT"]


class _RepeatedActionLlm:
    def __init__(self):
        self.turn = 0
        self.tool_choices = []
        self.tool_names = []

    async def chat_turn(self, messages, *, agent_id, purpose="",
                        max_tokens=2048, tools=None, tool_choice=None,
                        use_quality=False):
        self.turn += 1
        self.tool_choices.append(tool_choice)
        self.tool_names.append([
            item["function"]["name"] for item in (tools or [])
        ])
        if tool_choice == "auto":
            remote = next(
                item["function"] for item in (tools or [])
                if item["function"].get("description")
                == "REMOTE_DESCRIPTION_SENTINEL")
            arguments = {"query": f"attempt-{self.turn}"}
            return LlmTurn(
                content="",
                tool_calls=[LlmToolCall(
                    tool_call_id=f"repeated-action-{self.turn}",
                    name=remote["name"],
                    arguments=arguments,
                    raw_arguments=json.dumps(arguments))],
                finish_reason="tool_calls")
        decision = {
            "thought": "respect the tool-turn ceiling",
            "output": {
                "summary": "bounded action loop completed",
                "claims": [],
                "evidence": [],
                "confidence": 0.7,
            },
            "done": True,
        }
        return LlmTurn(
            content="",
            tool_calls=[LlmToolCall(
                tool_call_id="bounded-final-3",
                name="emit_decision",
                arguments=decision,
                raw_arguments=json.dumps(decision))],
            finish_reason="tool_calls")


def test_action_turn_and_total_llm_quota_are_hard_limits():
    request = AgentRunRequest(
        runId="r-action-cap", conversationId="c-action-cap",
        traceId="t-action-cap", runType="project_analysis",
        resumeText="项目经历\nExample\nPython",
        jobDescription="Python backend")
    llm = _RepeatedActionLlm()
    executor = RunExecutor(
        request, NullEmitter(), memory=NullMemoryClient(),
        builtin_tools=BuiltinToolRegistry(), llm=llm)
    client = _LiveMcpClient()
    _attach_demo_mcp(executor.tools, client)
    executor.budget_plan["ProjectAgent"] = {
        "llmQuota": 3,
        "actionTurnQuota": 1,
        "toolQuota": 4,
    }
    executor.state.apply_artifacts({
        "resumeFacts": {"projects": [{"name": "Example"}]},
    })

    output = run(executor._run_agent(
        default_agent_registry.get("ProjectAgent")))

    assert output.summary == "bounded action loop completed"
    assert llm.turn == 2
    assert client.calls == [
        ("remote_search", {"query": "attempt-1"})]
    assert llm.tool_choices == [
        "auto",
        {"type": "function", "function": {"name": "emit_decision"}},
    ]
    assert llm.tool_names[-1] == ["emit_decision"]
    counters = executor.agent_counters["ProjectAgent"]
    assert counters["llmCalls"] == 2
    assert counters["actionTurns"] == 1


class _MalformedNativeFinalThenRepairLlm:
    def __init__(self):
        self.turn = 0
        self.messages = []

    async def chat_turn(self, messages, *, agent_id, purpose="",
                        max_tokens=2048, tools=None, tool_choice=None,
                        use_quality=False):
        self.turn += 1
        self.messages.append(messages)
        if self.turn == 1:
            remote = next(
                item["function"] for item in (tools or [])
                if item["function"].get("description")
                == "REMOTE_DESCRIPTION_SENTINEL")
            arguments = {"query": "candidate repository"}
            return LlmTurn(
                content="",
                tool_calls=[LlmToolCall(
                    tool_call_id="repair-budget-action",
                    name=remote["name"],
                    arguments=arguments,
                    raw_arguments=json.dumps(arguments))],
                finish_reason="tool_calls")
        if self.turn == 2:
            return LlmTurn(
                content="",
                tool_calls=[LlmToolCall(
                    tool_call_id="malformed-native-final",
                    name="emit_decision",
                    arguments={},
                    raw_arguments='{"thought":',
                    arguments_error="unexpected end of JSON input")],
                finish_reason="tool_calls")
        decision = {
            "thought": "repaired the provider-native final arguments",
            "output": {
                "summary": "native final repaired",
                "claims": [],
                "evidence": [],
                "confidence": 0.7,
            },
            "done": True,
        }
        return LlmTurn(
            content="",
            tool_calls=[LlmToolCall(
                tool_call_id="repaired-native-final",
                name="emit_decision",
                arguments=decision,
                raw_arguments=json.dumps(decision))],
            finish_reason="tool_calls")


def test_malformed_native_final_borrows_one_traced_repair_turn():
    """Regression for run-7d52f4c9…: don't strand a repair at llmQuota."""
    request = AgentRunRequest(
        runId="r-native-repair", conversationId="c-native-repair",
        traceId="t-native-repair", runType="project_analysis",
        resumeText="项目经历\nExample\nPython",
        jobDescription="Python backend")
    emitter = NullEmitter(
        request.runId, request.conversationId, request.traceId)
    llm = _MalformedNativeFinalThenRepairLlm()
    executor = RunExecutor(
        request, emitter, memory=NullMemoryClient(),
        builtin_tools=BuiltinToolRegistry(), llm=llm)
    client = _LiveMcpClient()
    _attach_demo_mcp(executor.tools, client)
    executor.budget_plan["ProjectAgent"] = {
        "llmQuota": 2,
        "actionTurnQuota": 1,
        "toolQuota": 4,
    }
    executor.state.apply_artifacts({
        "resumeFacts": {"projects": [{"name": "Example"}]},
    })

    output = run(executor._run_agent(
        default_agent_registry.get("ProjectAgent")))

    assert output.summary == "native final repaired"
    assert llm.turn == 3
    assert any(
        "json schema" in str(message.get("content") or "")
        for message in llm.messages[-1])
    reallocations = [
        event for event in emitter.events
        if event["eventType"] == "run.progress"
        and event["payload"].get("stage") == "budget_reallocated"
    ]
    assert len(reallocations) == 1
    assert reallocations[0]["payload"]["reason"] == "malformed_native_final"
    counters = executor.agent_counters["ProjectAgent"]
    assert counters["llmCalls"] == 3
    assert counters["borrowedRepairTurns"] == 1


class _ReportFinalizationLlm:
    def __init__(self):
        self.turn = 0
        self.tool_choices = []
        self.tool_names = []
        self.max_tokens = []

    async def chat_turn(self, messages, *, agent_id, purpose="",
                        max_tokens=2048, tools=None, tool_choice=None,
                        use_quality=False):
        self.turn += 1
        names = [item["function"]["name"] for item in (tools or [])]
        self.tool_choices.append(tool_choice)
        self.tool_names.append(names)
        self.max_tokens.append(max_tokens)
        if tool_choice == "auto":
            arguments = {"skill_id": "audit-job-relevant-evaluation"}
            return LlmTurn(
                content="",
                tool_calls=[LlmToolCall(
                    tool_call_id="report-skill-load-1",
                    name="load_skill",
                    arguments=arguments,
                    raw_arguments=json.dumps(arguments))],
                finish_reason="tool_calls")
        decision = {
            "thought": "finalize after observing the loaded audit skill",
            "output": {
                "summary": "report finalized within the terminal reserve",
                "report": {
                    "recommendation": "NEED_MANUAL_REVIEW",
                    "dimensions": [
                        {
                            "name": name,
                            "score": None,
                            "status": "UNASSESSED",
                            "rationale": "test fixture has no evidence",
                            "evidenceRefs": [],
                        }
                        for name in (
                            "技术能力", "项目深度", "JD匹配", "履历可信度")
                    ],
                    "strengths": [],
                    "risks": [],
                    "interviewQuestions": [],
                    "dataQuality": "INSUFFICIENT",
                    "missingEvidence": ["source evidence"],
                },
            },
            "done": True,
        }
        return LlmTurn(
            content="",
            tool_calls=[LlmToolCall(
                tool_call_id="report-final-2",
                name="emit_decision",
                arguments=decision,
                raw_arguments=json.dumps(decision))],
            finish_reason="tool_calls")


def test_report_agent_hides_evaluation_retrieval_and_forces_final_output():
    request = AgentRunRequest(
        runId="r-report-final", conversationId="c-report-final",
        traceId="t-report-final", runType="full_evaluation",
        resumeText="张三\nJava 后端工程师\n项目：支付平台",
        jobDescription="Java 高级后端工程师")
    llm = _ReportFinalizationLlm()
    executor = RunExecutor(
        request, NullEmitter(), memory=NullMemoryClient(),
        builtin_tools=BuiltinToolRegistry(), llm=llm)
    executor.budget_plan["ReportAgent"] = {
        "llmQuota": 3,
        "actionTurnQuota": 1,
        "toolQuota": 2,
    }
    executor.state.apply_artifacts({
        "resumeFacts": {"skills": ["Java"], "projects": [{"name": "支付平台"}]},
        "evidence": [],
    })

    output = run(executor._run_agent(
        default_agent_registry.get("ReportAgent")))

    assert output.summary == "report finalized within the terminal reserve"
    assert "knowledge_search" not in llm.tool_names[0]
    assert "resume_semantic_search" not in llm.tool_names[0]
    assert "validate_report_schema" not in llm.tool_names[0]
    assert llm.tool_names == [
        ["load_skill", "read_skill_resource", "emit_decision"],
        ["emit_decision"],
    ]
    assert llm.tool_choices == [
        "auto",
        {"type": "function", "function": {"name": "emit_decision"}},
    ]
    assert llm.max_tokens == [8192, 8192]


class _ReportRepairLlm:
    def __init__(self):
        self.turn = 0
        self.tool_choices = []
        self.saw_report_error = False

    async def chat_turn(self, messages, *, agent_id, purpose="",
                        max_tokens=2048, tools=None, tool_choice=None,
                        use_quality=False):
        self.turn += 1
        self.tool_choices.append(tool_choice)
        if self.turn == 1:
            decision = {
                "thought": "prematurely claim completion",
                "output": {"summary": "missing structured report"},
                "done": True,
            }
        else:
            message_text = "\n".join(
                str(message.get("content") or "") for message in messages)
            self.saw_report_error = (
                "structured report" in message_text
                and "缺失" in message_text
            )
            decision = {
                "thought": "repair the missing structured report",
                "output": {
                    "summary": "report repaired inside the terminal reserve",
                    "report": {
                        "recommendation": "NEED_MANUAL_REVIEW",
                        "dimensions": [
                            {
                                "name": name,
                                "score": None,
                                "status": "UNASSESSED",
                                "rationale": "test fixture has no evidence",
                                "evidenceRefs": [],
                            }
                            for name in (
                                "技术能力", "项目深度", "JD匹配", "履历可信度")
                        ],
                        "strengths": [],
                        "risks": [],
                        "interviewQuestions": [],
                        "dataQuality": "INSUFFICIENT",
                        "missingEvidence": ["source evidence"],
                    },
                },
                "done": True,
            }
        return LlmTurn(
            content="",
            tool_calls=[LlmToolCall(
                tool_call_id=f"report-repair-{self.turn}",
                name="emit_decision",
                arguments=decision,
                raw_arguments=json.dumps(decision))],
            finish_reason="tool_calls")


def test_report_agent_repairs_done_true_without_structured_report():
    request = AgentRunRequest(
        runId="r-report-repair", conversationId="c-report-repair",
        traceId="t-report-repair", runType="full_evaluation",
        resumeText="张三\nJava 后端工程师\n项目：支付平台",
        jobDescription="Java 高级后端工程师")
    llm = _ReportRepairLlm()
    executor = RunExecutor(
        request, NullEmitter(), memory=NullMemoryClient(),
        builtin_tools=BuiltinToolRegistry(), llm=llm)
    executor.budget_plan["ReportAgent"] = {
        "llmQuota": 2,
        "actionTurnQuota": 0,
        "toolQuota": 0,
    }
    executor.state.apply_artifacts({
        "resumeFacts": {"skills": ["Java"], "projects": [{"name": "支付平台"}]},
        "evidence": [],
    })

    output = run(executor._run_agent(
        default_agent_registry.get("ReportAgent")))

    assert llm.turn == 2
    assert llm.saw_report_error is True
    assert all(
        choice == {
            "type": "function", "function": {"name": "emit_decision"}}
        for choice in llm.tool_choices)
    assert output.summary == "report repaired inside the terminal reserve"
    assert executor.final_answer
    assert executor.state.artifact("finalReport")["recommendation"] \
        == "NEED_MANUAL_REVIEW"


def test_parse_decision_decodes_provider_stringified_output_object():
    encoded_output = json.dumps({
        "summary": "provider double encoded this object",
        "claims": [],
        "evidence": [],
        "confidence": 0.8,
    })
    raw = json.dumps({
        "thought": "decode safely",
        "output": encoded_output,
        "done": True,
    })

    decision, error = RunExecutor._parse_decision(raw)

    assert error == ""
    assert decision is not None
    assert decision["output"]["summary"] \
        == "provider double encoded this object"
    assert decision["output"]["confidence"] == 0.8


def test_report_contract_normalizes_interview_probes_alias():
    request = AgentRunRequest(
        runId="r-report-probes", conversationId="c-report-probes",
        traceId="t-report-probes", runType="full_evaluation",
        resumeText="张三\nJava 后端工程师\n项目：支付平台",
        jobDescription="Java 高级后端工程师")
    executor = RunExecutor(
        request, NullEmitter(), memory=NullMemoryClient(),
        builtin_tools=BuiltinToolRegistry(), llm=_ReportFinalizationLlm())
    probes = [{
        "id": "probe-1",
        "priority": "HIGH",
        "question": "支付平台峰值流量和故障恢复如何验证？",
        "objective": "核验项目深度",
        "triggeredBy": "支付平台项目",
        "evidenceRefs": [],
        "goodSignals": ["说明压测数据和恢复时间"],
        "redFlags": ["只有笼统描述"],
    }]
    decision = {
        "thought": "use the established probes alias",
        "output": {
            "summary": "alias normalized",
            "report": {
                "recommendation": "NEED_MANUAL_REVIEW",
                "dimensions": [{
                    "name": "项目深度",
                    "score": 60,
                    "status": "PARTIAL",
                    "rationale": "需要面试核验",
                    "evidenceRefs": [],
                }],
                "strengths": [],
                "risks": [],
                "interviewProbes": probes,
                "dataQuality": "PARTIAL",
            },
        },
        "done": True,
    }

    error = executor._report_decision_schema_error(decision)
    output = executor._build_output(
        default_agent_registry.get("ReportAgent"),
        decision["output"], "")

    assert error == ""
    assert decision["output"]["report"]["interviewQuestions"] == probes
    assert output.summary == "alias normalized"
    final_report = executor.state.artifact("finalReport")
    assert final_report["interviewQuestions"][0]["id"] == "probe-1"
    assert final_report["interviewProbes"][0]["question"] \
        == "支付平台峰值流量和故障恢复如何验证？"


def test_failed_mcp_result_is_error_and_never_evidence():
    request = AgentRunRequest(
        runId="r-fail", conversationId="c-fail", traceId="t-fail",
        runType="full_evaluation", resumeText="项目经历\nExample\nPython",
        jobDescription="Python")
    executor = RunExecutor(
        request, NullEmitter(), memory=NullMemoryClient(),
        builtin_tools=BuiltinToolRegistry(), llm=_NativeMcpLlm())
    client = _LiveMcpClient(fail=True)
    _attach_demo_mcp(executor.tools, client)

    call = run(executor.tools.execute(
        "ProjectAgent", "demo.search", {"query": "q"},
        tool_call_id="provider-failed-1"))
    assert call.status == "FAILED"
    run(executor._record_tool_success(
        "ProjectAgent", "demo.search", {"query": "q"}, call.result,
        tool_call_id=call.tool_call_id))
    assert not executor.state.artifact("mcpEvidence")
    failed = [
        event for event in executor.emitter.events
        if event["eventType"] == "tool.failed"]
    assert failed and failed[-1]["payload"]["lifecycleStage"] == "ERROR"


def test_expected_mcp_unavailability_is_observation_not_red_failure():
    class UnavailableClient:
        async def call_tool(self, name, arguments):
            return {
                "success": False,
                "isError": True,
                "status": "UNAVAILABLE",
                "text": "robots.txt temporarily unreachable",
            }

    request = AgentRunRequest(
        runId="r-unavailable", conversationId="c-unavailable",
        traceId="t-unavailable", runType="full_evaluation",
        resumeText="https://blog.csdn.net/example", jobDescription="Java")
    executor = RunExecutor(
        request, NullEmitter(), memory=NullMemoryClient(),
        builtin_tools=BuiltinToolRegistry(), llm=_NativeMcpLlm())
    _attach_demo_mcp(executor.tools, UnavailableClient())

    call = run(executor.tools.execute(
        "ProjectAgent", "demo.search", {"query": "q"},
        tool_call_id="provider-unavailable-1"))

    assert call.status == "UNAVAILABLE"
    assert call.result["status"] == "UNAVAILABLE"
    assert not [event for event in executor.emitter.events
                if event["eventType"] == "tool.failed"]
    completed = [event for event in executor.emitter.events
                 if event["eventType"] == "tool.completed"]
    assert completed[-1]["payload"]["outcome"] == "UNAVAILABLE"
    assert completed[-1]["payload"]["lifecycleStage"] == "RESULT"


def test_search_query_url_does_not_fake_result_provenance():
    class NoSourceClient:
        async def call_tool(self, name, arguments):
            return {
                "success": True,
                "isError": False,
                "text": "answer without a cited source",
            }

    request = AgentRunRequest(
        runId="r-no-source", conversationId="c-no-source",
        traceId="t-no-source", runType="full_evaluation",
        resumeText="项目经历\nExample\nPython",
        jobDescription="Python")
    executor = RunExecutor(
        request, NullEmitter(), memory=NullMemoryClient(),
        builtin_tools=BuiltinToolRegistry(), llm=_NativeMcpLlm())
    _attach_demo_mcp(executor.tools, NoSourceClient())

    args = {"query": "verify https://model-invented.example/profile"}
    call = run(executor.tools.execute(
        "ProjectAgent", "demo.search", args,
        tool_call_id="provider-no-source-1"))
    assert call.status == "SUCCEEDED"
    run(executor._record_tool_success(
        "ProjectAgent", "demo.search", args, call.result,
        tool_call_id=call.tool_call_id))

    assert not executor.state.artifact("mcpEvidence")


def test_project_presteps_never_force_mcp_call():
    request = AgentRunRequest(
        runId="r-no-force", conversationId="c-no-force",
        runType="full_evaluation",
        resumeText="项目经历\nExample\nhttps://example.test/repo",
        jobDescription="Python")
    executor = RunExecutor(
        request, NullEmitter(), memory=NullMemoryClient(),
        builtin_tools=BuiltinToolRegistry(), llm=_NativeMcpLlm())
    steps = executor._pre_steps(default_agent_registry.get("ProjectAgent"))
    assert all(
        not (executor.tools.definitions.get(name)
             and executor.tools.definitions[name].kind == "mcp")
        for name, _arguments in steps)
    assert "locate_evidence" in {name for name, _arguments in steps}


class _ProgressiveSkillLlm:
    def __init__(self):
        self.turn = 0
        self.saw_instructions = False
        self.saw_resource = False

    async def chat_turn(self, messages, *, agent_id, purpose="",
                        max_tokens=2048, tools=None, tool_choice=None,
                        use_quality=False):
        self.turn += 1
        content = "\n".join(
            str(message.get("content") or "") for message in messages)
        self.saw_instructions |= "BODY_SENTINEL" in content
        self.saw_resource |= "RESOURCE_SENTINEL" in content
        if self.turn == 1:
            arguments = {"skill_id": "ground-project-claims"}
            return LlmTurn(
                content="",
                tool_calls=[LlmToolCall(
                    tool_call_id="skill-load-1",
                    name="load_skill",
                    arguments=arguments,
                    raw_arguments=json.dumps(arguments))],
                finish_reason="tool_calls")
        if self.turn == 2:
            arguments = {
                "skill_id": "ground-project-claims",
                "path": "references/details.md",
            }
            return LlmTurn(
                content="",
                tool_calls=[LlmToolCall(
                    tool_call_id="skill-resource-2",
                    name="read_skill_resource",
                    arguments=arguments,
                    raw_arguments=json.dumps(arguments))],
                finish_reason="tool_calls")
        decision = {
            "thought": "used progressively disclosed instructions and resource",
            "output": {
                "summary": "progressive skill completed",
                "claims": [],
                "evidence": [],
                "confidence": 0.8,
            },
            "done": True,
        }
        return LlmTurn(
            content="",
            tool_calls=[LlmToolCall(
                tool_call_id="skill-final-3",
                name="emit_decision",
                arguments=decision,
                raw_arguments=json.dumps(decision))],
            finish_reason="tool_calls")


def test_progressive_skill_action_turns_are_reserved(monkeypatch, tmp_path):
    from app.runtime import executor as executor_module

    package = tmp_path / "skills" / "ground-project-claims"
    references = package / "references"
    references.mkdir(parents=True)
    (package / "SKILL.md").write_text(
        "---\n"
        "name: ground-project-claims\n"
        "description: Unified candidate evidence skill metadata.\n"
        "version: v-test\n"
        "---\n\n"
        "BODY_SENTINEL: apply project evidence rules.\n"
        "Read references/details.md for the scoring detail.\n",
        encoding="utf-8")
    (references / "details.md").write_text(
        "RESOURCE_SENTINEL: score only source-backed claims.",
        encoding="utf-8")
    monkeypatch.setattr(
        executor_module, "default_skill_manager",
        SkillManager(tmp_path / "skills"))

    request = AgentRunRequest(
        runId="r-skill-turns", conversationId="c-skill-turns",
        traceId="t-skill-turns", runType="project_analysis",
        resumeText="项目经历\n支付平台，负责缓存与消息队列",
        jobDescription="Java backend")
    llm = _ProgressiveSkillLlm()
    executor = RunExecutor(
        request, NullEmitter(), memory=NullMemoryClient(),
        builtin_tools=BuiltinToolRegistry(), llm=llm)
    executor.state.apply_artifacts({
        "resumeFacts": {"projects": [{"name": "支付平台"}]},
    })

    output = run(executor._run_agent(
        default_agent_registry.get("ProjectAgent")))

    assert output.summary == "progressive skill completed"
    assert llm.turn == 3
    assert llm.saw_instructions is True
    assert llm.saw_resource is True
    counters = executor.agent_counters["ProjectAgent"]
    assert counters["decisionIterations"] == 1
    assert counters["actionTurns"] == 2
    assert counters["iterations"] == 3
    assert any(
        event["eventType"] == "skill.applied"
        for event in executor.emitter.events)


def test_coordinator_order_helper_and_revision_reuse_contract():
    coordinator = Coordinator(
        default_agent_registry, PolicyBundle.from_config("balanced", {}), None)
    ordered = coordinator._prefer_agent_order([
        "ProjectAgent", "TechAgent", "ReportAgent"])
    assert ordered == ["TechAgent", "ProjectAgent", "ReportAgent"]
    full_plan = [
        "ResumeParserAgent", "JDAnalysisAgent", "TechAgent",
        "ProjectAgent", "RiskAgent", "EvidenceAgent", "ReportAgent"]
    rich_budget = coordinator._budget_plan(
        full_plan, "ReportAgent", signals={
            "is_rich_resume": True,
            "has_projects": True,
            "has_external_urls": True,
            "has_jd": True,
        })
    assert sum(item["llmQuota"] for item in rich_budget.values()) \
        <= coordinator.policy.maxLlmCalls
    assert rich_budget["ProjectAgent"]["llmQuota"] >= 3
    assert all(
        item["actionTurnQuota"] <= max(0, item["llmQuota"] - 1)
        for item in rich_budget.values())

    runtime_budget = RunBudget()
    runtime_budget.configure_llm_budget(
        17, {"terminal": 3, "control": 4},
        scope_limits={"control": 4})
    runtime_budget.claim_llm_call(17, "control")
    live_coordinator = Coordinator(
        default_agent_registry,
        PolicyBundle.from_config("balanced", {}),
        type("BudgetedLlm", (), {"budget": runtime_budget})())
    live_budget = live_coordinator._budget_plan(
        full_plan, "ReportAgent", signals={
            "is_rich_resume": True,
            "has_projects": True,
            "has_external_urls": True,
            "has_jd": True,
        })
    assert live_budget["ReportAgent"]["llmQuota"] >= 3
    assert live_budget["ProjectAgent"]["actionTurnQuota"] >= 3
    assert live_budget["EvidenceAgent"]["actionTurnQuota"] >= 1

    previous = {
        "resumeFacts": {"skills": ["Python"], "projects": [{"name": "Demo"}]},
        "parsedResume": {"skills": ["Python"]},
        "jdRequirements": {"must": ["Java"]},
        "effectiveJd": "old Java JD",
        "technicalFindings": [{"text": "old tech"}],
        "projectFindings": [{"text": "old relevance"}],
        "risks": [{"claim": "timeline"}],
        "evidence": [{"text": "old evidence"}],
        "finalReport": {"recommendation": "HIRE"},
    }
    request = AgentRunRequest(
        runId="r-revision", conversationId="c-revision", revision=2,
        runType="full_evaluation", resumeText="项目经历\nDemo\nPython",
        jobDescription="new Python JD",
        previousArtifacts=previous,
        invalidatedArtifacts=["jdRequirements"])
    executor = RunExecutor(
        request, NullEmitter(), memory=NullMemoryClient(),
        builtin_tools=BuiltinToolRegistry(), llm=_NativeMcpLlm())
    reuse = executor._reuse_previous_revision_artifacts()
    artifacts = executor.state.artifacts()
    assert reuse["sourceRevision"] == 1
    assert artifacts["resumeFacts"]["skills"] == ["Python"]
    assert artifacts["risks"], "unaffected risk result should be reused"
    for stale in (
            "jdRequirements", "effectiveJd", "technicalFindings",
            "projectFindings", "evidence", "finalReport"):
        assert not artifacts.get(stale), f"{stale} must be invalidated"

    planned = coordinator.plan_from_artifacts(
        run_type="full_evaluation", needs_parse=False,
        resume_text=request.resumeText or "",
        job_description=request.jobDescription or "",
        artifacts=artifacts)
    assert "ResumeParserAgent" not in planned["plan"]
    assert "RiskAgent" not in planned["plan"]
    for affected_agent in (
            "JDAnalysisAgent", "TechAgent", "ProjectAgent",
            "EvidenceAgent", "ReportAgent"):
        assert affected_agent in planned["plan"]


def test_memory_writeback_persists_real_candidate_facts_as_semantic():
    request = AgentRunRequest(
        runId="r-memory-types", conversationId="c-memory-types",
        runType="full_evaluation",
        resumeText="技能\nJava Spring Boot Kafka\n项目经历\n支付网关",
        jobDescription="Java 后端工程师")
    memory = NullMemoryClient()
    executor = RunExecutor(
        request, NullEmitter(), memory=memory,
        builtin_tools=BuiltinToolRegistry(), llm=_NativeMcpLlm())
    executor.state.apply_artifacts({
        "parsedResume": {
            "success": True,
            "skills": ["java", "spring boot", "kafka"],
            "projectNames": ["支付网关"],
            "confidence": 0.91,
        },
        "resumeFacts": {
            "skills": ["java", "spring boot", "kafka"],
            "projects": [{"name": "支付网关"}],
            "experiences": [{"raw": "负责支付链路稳定性"}],
            "education": [{"raw": "计算机本科"}],
            "confidence": 0.91,
        },
    })

    run(executor._write_memories("done"))

    semantic = [
        row for row in memory.writes
        if row["type"] == "SEMANTIC"
        and row["source"] == "candidate_fact"]
    assert semantic
    assert semantic[0]["structured"]["skills"] == [
        "java", "spring boot", "kafka"]
    assert semantic[0]["structured"]["projects"] == ["支付网关"]
    assert any(row["type"] == "WORKING" for row in memory.writes)


def test_memory_writeback_learns_candidate_free_procedure_from_actual_run():
    request = AgentRunRequest(
        runId="r-runtime-strategy", conversationId="c-runtime-strategy",
        runType="full_evaluation",
        resumeText="候选人 Alice；Java 支付项目",
        jobDescription="Java 后端工程师")
    memory = NullMemoryClient()
    executor = RunExecutor(
        request, NullEmitter(), memory=memory,
        builtin_tools=BuiltinToolRegistry(), llm=_NativeMcpLlm())
    executor.executed = [
        "JDAnalysisAgent", "TechAgent", "ProjectAgent",
        "EvidenceAgent", "ReportAgent"]
    executor.agent_timings = {
        "JDAnalysisAgent": 100, "TechAgent": 200, "ProjectAgent": 300,
        "EvidenceAgent": 100, "ReportAgent": 200}
    executor.agent_counters = {
        "TechAgent": {"llmCalls": 2, "toolCalls": 1},
        "ProjectAgent": {"llmCalls": 2, "toolCalls": 1},
        "EvidenceAgent": {"llmCalls": 1, "toolCalls": 1},
        "ReportAgent": {"llmCalls": 2, "toolCalls": 0},
    }
    executor.state.apply_artifacts({
        "finalReport": {
            "recommendation": "INTERVIEW_RECOMMEND",
            "overallScore": 80,
        },
    })

    run(executor._write_memories("done"))

    learned = [
        row for row in memory.writes
        if row["type"] == "PROCEDURAL"
        and row["source"] == "runtime_strategy"]
    assert len(learned) == 1
    procedure = learned[0]
    assert procedure["ownerScope"] == "USER"
    assert procedure["structured"]["derivedFromRunId"] == "r-runtime-strategy"
    assert procedure["structured"]["actualExecution"] is True
    assert procedure["structured"]["candidateDataExcluded"] is True
    assert procedure["structured"]["strategyClass"] == "PROJECT_EVIDENCE"
    assert "Alice" not in procedure["content"]
    assert "Java 后端工程师" not in procedure["content"]


def test_memory_recall_fusion_reserves_real_hits_across_memory_classes():
    request = AgentRunRequest(
        runId="r-procedure-query", conversationId="c-procedure-query",
        runType="full_evaluation",
        userMessage="评估一下",
        resumeText="项目经历 GitHub 开源仓库",
        jobDescription="Java 后端")
    query = RunExecutor._procedural_memory_query(request)
    assert "执行策略" in query
    assert "项目" in query
    assert "证据核验" in query

    merged = RunExecutor._merge_memory_hits(
        [
            {"memoryId": "proc-1", "type": "PROCEDURAL", "score": 0.95},
            {"memoryId": "proc-2", "type": "PROCEDURAL", "score": 0.94},
        ],
        [
            {"memoryId": "semantic-1", "type": "SEMANTIC", "score": 0.52,
             "ownerScope": "CONVERSATION", "source": "candidate_fact"},
            {"memoryId": "unsafe-user-semantic", "type": "SEMANTIC",
             "score": 0.99, "ownerScope": "USER",
             "source": "evaluation_result"},
        ],
        [
            {"memoryId": "episode-1", "type": "EPISODIC", "score": 0.61,
             "ownerScope": "CONVERSATION", "source": "evaluation_insight"},
            {"memoryId": "anchor-1", "type": "EPISODIC", "score": 0.48,
             "ownerScope": "USER", "source": "cross_candidate_anchor"},
            # A USER-scoped candidate episode is not a safe comparison anchor.
            {"memoryId": "unsafe-user-episode", "type": "EPISODIC", "score": 0.99,
             "ownerScope": "USER", "source": "evaluation_result"},
        ],
        limit=4)
    assert [row["memoryId"] for row in merged] == [
        "semantic-1", "episode-1", "anchor-1", "proc-1"]
    assert "unsafe-user-episode" not in {row["memoryId"] for row in merged}
    assert "unsafe-user-semantic" not in {row["memoryId"] for row in merged}


def test_memory_recall_query_uses_resume_and_jd_not_only_generic_message():
    request = AgentRunRequest(
        runId="r-memory-query", conversationId="c-memory-query",
        runType="full_evaluation",
        userMessage="请评估这份简历",
        currentGoal="判断 Java 平台岗位匹配度",
        resumeText="五年 Java、Kafka、Kubernetes 支付平台经验",
        jobDescription="需要 Java 21、Spring Boot、Kafka")

    query, basis = RunExecutor._memory_retrieval_query(request)

    assert "请评估这份简历" in query
    assert "Java 21" in query
    assert "Kubernetes" in query
    assert basis == [
        "user_message", "current_goal", "job_description", "resume_cues"]
    assert len(query) < 1000

    episodic_query = RunExecutor._episodic_memory_query(request)
    assert "历史评估" in episodic_query
    assert "Java 21" in episodic_query
    assert len(episodic_query) < 700


def test_mcp_registry_is_main_thread_safe_and_expands_url_env():
    # Regression: Python 3.8 raised "There is no current event loop" merely
    # constructing a registry after another loop had been closed.
    registry = McpRegistry(config={
        "mcpServers": {}, "optionalMcpServers": {}, "agentToolRouting": {}})
    os.environ["DEMO_MCP_TOKEN"] = "token-123"
    captured = {}

    async def fake_probe(name, cfg, allowed, prefix, url):
        captured["url"] = url
        return []

    registry._probe_http = fake_probe
    try:
        run(registry._probe_server(
            "demo",
            {
                "enabled": True,
                "transport": "streamable-http",
                "url": "https://example.test/${DEMO_MCP_TOKEN}/mcp",
            },
            optional=False))
    finally:
        os.environ.pop("DEMO_MCP_TOKEN", None)
    assert captured["url"] == "https://example.test/token-123/mcp"
    assert registry.health["demo"].status == "DOWN"
    assert "MCP_DISCOVERY_EMPTY" in registry.health["demo"].error


def test_mcp_protocol_negotiation_is_bounded_and_final_version_propagates():
    client = StreamableHttpMcpClient(
        "demo", "https://example.test/mcp")
    attempted = []

    async def fake_post(payload):
        version = payload["params"]["protocolVersion"]
        attempted.append(version)
        if len(attempted) < 3:
            raise McpError(f"unsupported protocol version {version}")
        return {"protocolVersion": "2025-03-26"}

    async def no_notify():
        return None

    client._post = fake_post
    client._notify_initialized = no_notify
    result = run(client.initialize())
    assert PROTOCOL_VERSION == "2025-11-25"
    assert attempted == ["2025-11-25", "2025-06-18", "2025-03-26"]
    assert result["protocolVersion"] == "2025-03-26"
    assert client.protocol_version == "2025-03-26"


def test_streamable_http_sends_negotiated_protocol_header_after_initialize(
        monkeypatch):
    from app.runtime import mcp_registry as mcp_runtime

    captured = []

    class Response:
        status_code = 200
        headers = {}
        text = '{"jsonrpc":"2.0","id":1,"result":{}}'

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, *, json, headers):
            captured.append((json["method"], dict(headers)))
            return Response()

    monkeypatch.setattr(mcp_runtime.httpx, "AsyncClient", Client)
    client = StreamableHttpMcpClient(
        "demo", "https://example.test/mcp")
    client.protocol_version = "2025-06-18"

    run(client._post({
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {}}))
    run(client._post({
        "jsonrpc": "2.0", "id": 2, "method": "tools/list",
        "params": {}}))
    run(client._post({
        "jsonrpc": "2.0", "id": 3, "method": "tools/call",
        "params": {"name": "search", "arguments": {}}}))
    run(client._notify_initialized())

    by_method = {method: headers for method, headers in captured}
    assert "MCP-Protocol-Version" not in by_method["initialize"]
    assert by_method["tools/list"]["MCP-Protocol-Version"] == "2025-06-18"
    assert by_method["tools/call"]["MCP-Protocol-Version"] == "2025-06-18"
    assert (
        by_method["notifications/initialized"]["MCP-Protocol-Version"]
        == "2025-06-18"
    )


def test_empty_or_zero_whitelist_discovery_fails_closed():
    with pytest.raises(McpError, match="MCP_DISCOVERY_EMPTY"):
        McpRegistry._validated_discovery(
            "empty", [], {"expected"}, "empty", PROTOCOL_VERSION)

    with pytest.raises(McpError, match="MCP_CONFIG_MISMATCH"):
        McpRegistry._validated_discovery(
            "renamed",
            [{"name": "remote_new_name", "inputSchema": {"type": "object"}}],
            {"configured_old_name"}, "renamed", PROTOCOL_VERSION)


def test_degraded_probe_ttl_retries_only_degraded_server(monkeypatch):
    from app.runtime import mcp_registry as mcp_runtime

    registry = McpRegistry(config={
        "mcpServers": {
            "healthy": {"enabled": True},
            "down": {"enabled": True},
        },
        "optionalMcpServers": {},
        "agentToolRouting": {},
    })
    registry._probed = True
    registry._last_probe_at = 100.0
    registry.health["healthy"] = McpServerHealth(
        name="healthy", status="AVAILABLE", transport="streamable-http")
    registry.health["down"] = McpServerHealth(
        name="down", status="DOWN", transport="streamable-http")
    healthy_client = object()
    registry._http_clients["healthy"] = healthy_client
    probed = []

    async def fake_probe(self, name, cfg, *, optional):
        probed.append(name)
        self.health[name] = McpServerHealth(
            name=name, status="AVAILABLE", transport="streamable-http")

    monkeypatch.setattr(McpRegistry, "_probe_server", fake_probe)
    monkeypatch.setattr(
        mcp_runtime.time, "time",
        lambda: 100.0 + DEGRADED_REPROBE_TTL_S + 1.0)

    run(registry.probe_all())

    assert probed == ["down"]
    assert registry._http_clients["healthy"] is healthy_client
    assert registry.health["healthy"].status == "AVAILABLE"
    assert registry.health["down"].status == "AVAILABLE"


def test_initial_mcp_discovery_probes_servers_concurrently(monkeypatch):
    registry = McpRegistry(config={
        "mcpServers": {
            "one": {"enabled": True},
            "two": {"enabled": True},
            "three": {"enabled": True},
        },
        "optionalMcpServers": {},
        "agentToolRouting": {},
    })
    started = []
    all_started = asyncio.Event()

    async def fake_probe(self, name, cfg, *, optional):
        started.append(name)
        if len(started) == 3:
            all_started.set()
        await all_started.wait()
        self.health[name] = McpServerHealth(
            name=name, status="AVAILABLE", transport="streamable-http")

    monkeypatch.setattr(McpRegistry, "_probe_server", fake_probe)
    monkeypatch.delenv("MCP_SKIP_PROBE", raising=False)

    async def scenario():
        await asyncio.wait_for(registry.probe_all(), timeout=0.5)

    run(scenario())

    assert set(started) == {"one", "two", "three"}
    assert registry._probed is True


def test_cancelled_forced_probe_keeps_last_healthy_catalog(monkeypatch):
    registry = McpRegistry(config={
        "mcpServers": {
            "one": {"enabled": True},
            "two": {"enabled": True},
        },
        "optionalMcpServers": {},
        "agentToolRouting": {"ProjectAgent": ["stable.search"]},
    })
    registry._probed = True
    registry.health["stable"] = McpServerHealth(
        name="stable", status="AVAILABLE", transport="streamable-http")
    registry.tools["stable.search"] = McpToolInfo(
        server="stable", name="search", catalog_name="stable.search",
        description="last known healthy tool", input_schema={"type": "object"})

    async def hanging_probe(self, name, cfg, *, optional):
        self.health[name] = McpServerHealth(
            name=name, status="AVAILABLE", transport="streamable-http")
        await asyncio.Event().wait()

    monkeypatch.setattr(McpRegistry, "_probe_server", hanging_probe)
    monkeypatch.delenv("MCP_SKIP_PROBE", raising=False)

    async def scenario():
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(
                registry.probe_all(force=True), timeout=0.05)

    run(scenario())

    assert set(registry.health) == {"stable"}
    assert set(registry.tools) == {"stable.search"}


def test_runtime_transport_failure_transitions_to_down_for_ttl_reprobe():
    class DownClient:
        async def call_tool(self, name, arguments):
            raise McpError("MCP demo transport: connection refused")

    registry = McpRegistry(config={
        "mcpServers": {"demo": {"enabled": True}},
        "optionalMcpServers": {},
        "agentToolRouting": {"ProjectAgent": ["demo.search"]},
    })
    registry._probed = True
    registry._last_probe_at = 100.0
    registry.health["demo"] = McpServerHealth(
        name="demo", status="AVAILABLE", transport="streamable-http")
    registry.tools["demo.search"] = McpToolInfo(
        server="demo", name="search", catalog_name="demo.search",
        description="search", input_schema={"type": "object"})
    registry._http_clients["demo"] = DownClient()

    result = run(registry.call("demo.search", {}))

    assert result["success"] is False
    assert result["status"] == "DOWN"
    assert registry.health["demo"].status == "DOWN"
    assert registry.needs_probe(
        now=100.0 + DEGRADED_REPROBE_TTL_S - 1.0) is False
    assert registry.needs_probe(
        now=100.0 + DEGRADED_REPROBE_TTL_S + 1.0) is True


def test_knowledge_search_uses_single_in_request_rerank_with_real_timings():
    from app.runtime import gateway

    tools = ToolExecutor(
        NullEmitter(), RunBudget(), BuiltinToolRegistry(),
        max_tool_calls_run=5, tool_timeout_seconds=10,
        run_context={}, llm=object())

    async def one_query(query):
        return [query]

    calls = []

    async def fake_search(*, query, top_k=5, rerank=False, **kwargs):
        calls.append(rerank)
        return {
            "chunks": [
                {"chunkId": "a", "finalScore": 0.7, "content": "A"},
                {"chunkId": "b", "finalScore": 0.65, "content": "B"},
            ],
            "rerankApplied": True,
            "rerankProvider": "feature_rerank_v1",
            "rerankBeforeTopScore": 0.55,
            "rerankAfterTopScore": 0.7,
            "retrievalMs": 12,
            "fusionMs": 1,
            "rerankMs": 2,
            "latencyMs": 15,
        }

    tools._rewrite_queries = one_query
    original = gateway.java_knowledge_search
    gateway.java_knowledge_search = fake_search
    try:
        result = run(tools._retrieve_with_rewrite(
            tools.definitions["knowledge_search"],
            {"query": "q", "topK": 2},
            True))
    finally:
        gateway.java_knowledge_search = original
    assert calls == [True]
    assert result["rerankApplied"] is True
    assert result["rerankProvider"] == "feature_rerank_v1"
    assert result["rerankBeforeTopScore"] == 0.55
    assert result["rerankAfterTopScore"] == 0.7
    assert result["_latency"]["retrieval_ms"] == 12
    assert result["_latency"]["fusion_ms"] == 1
    assert result["_latency"]["rerank_ms"] == 2


def test_tech_presteps_use_one_semantic_recall_and_leave_no_duplicate_catalog():
    request = AgentRunRequest(
        runId="r-tech-pre", conversationId="c-tech-pre",
        runType="full_evaluation",
        resumeText=("Java Spring Boot Redis RAG 项目：性能优化与故障排查"),
        jobDescription="Java Spring Boot Docker RAG Agent")
    executor = RunExecutor(
        request, NullEmitter(), memory=NullMemoryClient(),
        builtin_tools=BuiltinToolRegistry(), llm=_NativeMcpLlm())
    steps = executor._pre_steps(default_agent_registry.get("TechAgent"))
    names = [name for name, _arguments in steps]
    assert names.count("resume_semantic_search") == 1
    assert names.count("knowledge_search") == 1
    assert names.count("calculate_jd_coverage") == 1
    semantic_args = next(
        arguments for name, arguments in steps
        if name == "resume_semantic_search")
    assert "性能优化" in semantic_args["query"]
    assert "resumeText" in semantic_args


def test_cancelled_degraded_reprobe_preserves_old_health_catalog_and_clients(
        monkeypatch):
    from app.runtime import mcp_registry as mcp_runtime

    registry = McpRegistry(config={
        "mcpServers": {
            "healthy": {"enabled": True},
            "down": {"enabled": True},
        },
        "optionalMcpServers": {},
        "agentToolRouting": {},
    })
    registry._probed = True
    registry._last_probe_at = 100.0
    registry._last_probe_iso = "old-probe"
    healthy_health = McpServerHealth(
        name="healthy", status="AVAILABLE", transport="streamable-http",
        tools=["healthy.search"])
    degraded_health = McpServerHealth(
        name="down", status="DOWN", transport="streamable-http",
        tools=["down.search"], error="last known failure")
    healthy_tool = McpToolInfo(
        server="healthy", name="search", catalog_name="healthy.search",
        description="healthy", input_schema={"type": "object"})
    degraded_tool = McpToolInfo(
        server="down", name="search", catalog_name="down.search",
        description="last known degraded catalog",
        input_schema={"type": "object"})
    healthy_client = object()
    degraded_client = object()
    registry.health.update({
        "healthy": healthy_health,
        "down": degraded_health,
    })
    registry.tools.update({
        "healthy.search": healthy_tool,
        "down.search": degraded_tool,
    })
    registry._http_clients.update({
        "healthy": healthy_client,
        "down": degraded_client,
    })
    probe_started = asyncio.Event()

    async def hanging_probe(self, name, cfg, *, optional):
        self.health[name] = McpServerHealth(
            name=name, status="AVAILABLE", transport="streamable-http",
            tools=[f"{name}.replacement"])
        self.tools[f"{name}.replacement"] = McpToolInfo(
            server=name, name="replacement",
            catalog_name=f"{name}.replacement",
            description="staged replacement", input_schema={"type": "object"})
        self._http_clients[name] = object()
        probe_started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(McpRegistry, "_probe_server", hanging_probe)
    monkeypatch.setattr(
        mcp_runtime.time, "time",
        lambda: 100.0 + DEGRADED_REPROBE_TTL_S + 1.0)
    monkeypatch.delenv("MCP_SKIP_PROBE", raising=False)

    async def scenario():
        task = asyncio.create_task(registry.probe_all())
        await asyncio.wait_for(probe_started.wait(), timeout=0.2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    run(scenario())

    assert registry.health["healthy"] is healthy_health
    assert registry.health["down"] is degraded_health
    assert registry.tools["healthy.search"] is healthy_tool
    assert registry.tools["down.search"] is degraded_tool
    assert set(registry.tools) == {"healthy.search", "down.search"}
    assert registry._http_clients["healthy"] is healthy_client
    assert registry._http_clients["down"] is degraded_client
    assert registry._last_probe_at == 100.0
    assert registry._last_probe_iso == "old-probe"
