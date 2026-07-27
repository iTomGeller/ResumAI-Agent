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

    assert not executor.state.artifact("mcpEvidence")
    context = executor.state.artifact("mcpContext")
    assert context and context[0]["evidenceUse"] == "context_only"
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
    assert execute_flags == [False], (
        "native model-authored MCP parameters must execute verbatim")

    chain = [
        event for event in emitter.events
        if event.get("payload", {}).get("toolCallId") == "provider-native-7"]
    stages = [event.get("payload", {}).get("lifecycleStage")
              for event in chain]
    assert stages.index("CATALOG_EXPOSED") < stages.index("LLM_PROPOSED")
    assert stages.index("LLM_PROPOSED") < stages.index("EXECUTION_STARTED")
    assert stages.index("EXECUTION_STARTED") < stages.index("RESULT")
    evidence = executor.state.artifact("mcpEvidence")
    assert evidence and evidence[0]["status"] == "SUCCEEDED"
    assert evidence[0]["toolCallId"] == "provider-native-7"


class _RepeatedActionLlm:
    def __init__(self):
        self.turn = 0

    async def chat_turn(self, messages, *, agent_id, purpose="",
                        max_tokens=2048, tools=None, tool_choice=None,
                        use_quality=False):
        self.turn += 1
        if self.turn <= 2:
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
        traceId="t-action-cap", runType="full_evaluation",
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
        "toolQuota": 2,
    }
    executor.state.apply_artifacts({
        "resumeFacts": {"projects": [{"name": "Example"}]},
    })

    output = run(executor._run_agent(
        default_agent_registry.get("ProjectAgent")))

    assert output.summary == "bounded action loop completed"
    assert llm.turn == 3
    assert client.calls == [
        ("remote_search", {"query": "attempt-1"})]
    counters = executor.agent_counters["ProjectAgent"]
    assert counters["llmCalls"] == 3
    assert counters["actionTurns"] == 1


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
    assert steps == []


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
        "description: Project evidence skill metadata.\n"
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
        traceId="t-skill-turns", runType="full_evaluation",
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

    async def fake_probe(name, cfg, *, optional):
        probed.append(name)
        registry.health[name] = McpServerHealth(
            name=name, status="AVAILABLE", transport="streamable-http")

    registry._probe_server = fake_probe
    monkeypatch.setattr(
        mcp_runtime.time, "time",
        lambda: 100.0 + DEGRADED_REPROBE_TTL_S + 1.0)

    run(registry.probe_all())

    assert probed == ["down"]
    assert registry._http_clients["healthy"] is healthy_client
    assert registry.health["healthy"].status == "AVAILABLE"
    assert registry.health["down"].status == "AVAILABLE"


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


def test_rerank_telemetry_uses_actual_before_after_scores_only():
    from app.runtime import gateway

    tools = ToolExecutor(
        NullEmitter(), RunBudget(), BuiltinToolRegistry(),
        max_tool_calls_run=5, tool_timeout_seconds=10,
        run_context={}, llm=object())

    async def one_query(query):
        return [query]

    async def fake_search(*, query, top_k=5, rerank=False, **kwargs):
        score = 0.7 if rerank else 0.2
        return {
            "chunks": [
                {"chunkId": "a", "score": score, "content": "A"},
                {"chunkId": "b", "score": score - 0.05, "content": "B"},
            ],
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
    assert result["agenticRerank"] is True
    assert result["rerankBeforeTopScore"] == 0.2
    assert result["rerankAfterTopScore"] == 0.7
    assert result["rerankLift"] == 0.5
