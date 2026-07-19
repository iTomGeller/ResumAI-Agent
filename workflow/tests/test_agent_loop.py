from __future__ import annotations

import asyncio
import importlib
import json
import sys
import types
from pathlib import Path

import pytest


@pytest.fixture
def agent_runtime(monkeypatch: pytest.MonkeyPatch):
    workflow_root = Path(__file__).resolve().parents[1]
    monkeypatch.syspath_prepend(str(workflow_root))

    class Message:
        def __init__(self, content="", **kwargs):
            self.content = content
            for key, value in kwargs.items():
                setattr(self, key, value)

    class SystemMessage(Message):
        pass

    class HumanMessage(Message):
        pass

    class AIMessage(Message):
        def __init__(self, content="", tool_calls=None, response_metadata=None):
            super().__init__(content)
            self.tool_calls = tool_calls or []
            self.response_metadata = response_metadata or {}

    class ToolMessage(Message):
        def __init__(self, content="", tool_call_id="", name=None):
            super().__init__(content)
            self.tool_call_id = tool_call_id
            self.name = name

    class StructuredTool:
        pass

    messages_module = types.ModuleType("langchain_core.messages")
    messages_module.AIMessage = AIMessage
    messages_module.HumanMessage = HumanMessage
    messages_module.SystemMessage = SystemMessage
    messages_module.ToolMessage = ToolMessage
    tools_module = types.ModuleType("langchain_core.tools")
    tools_module.StructuredTool = StructuredTool
    langchain_core = types.ModuleType("langchain_core")
    langchain_openai = types.ModuleType("langchain_openai")
    langchain_openai.ChatOpenAI = object
    monkeypatch.setitem(sys.modules, "langchain_core", langchain_core)
    monkeypatch.setitem(sys.modules, "langchain_core.messages", messages_module)
    monkeypatch.setitem(sys.modules, "langchain_core.tools", tools_module)
    monkeypatch.setitem(sys.modules, "langchain_openai", langchain_openai)

    fake_config = types.ModuleType("app.config")
    fake_config.settings = types.SimpleNamespace(
        deepseek_api_key="",
        deepseek_model="fake-model",
    )
    fake_config.normalized_deepseek_base_url = lambda: "https://example.test/v1"
    monkeypatch.setitem(sys.modules, "app.config", fake_config)

    emitted = []

    class Record:
        def __init__(self, **kwargs):
            for key, value in kwargs.items():
                setattr(self, key, value)

        def model_dump(self):
            return dict(self.__dict__)

    fake_models = types.ModuleType("app.models")
    fake_models.ToolCallRecord = Record
    fake_models.TraceEvent = Record
    fake_models.WorkflowState = Record
    monkeypatch.setitem(sys.modules, "app.models", fake_models)

    fake_events = types.ModuleType("app.events")

    async def emit_event(event):
        emitted.append(event)

    fake_events.emit_event = emit_event
    fake_events.make_event_id = (
        lambda trace, node, attempt, kind, round_index, call_id="0":
        f"{trace}:{node}:{attempt}:{kind}:{round_index}:{call_id}"
    )
    fake_events.messages_preview = lambda messages: json.dumps(messages, ensure_ascii=False)
    fake_events.now_iso = lambda: "2026-07-16T00:00:00+00:00"
    fake_events.preview_text = lambda value, max_len=200: str(value)[:max_len]
    monkeypatch.setitem(sys.modules, "app.events", fake_events)

    spans = []
    fake_tracing = types.ModuleType("app.langfuse_tracing")
    fake_tracing.action_span = lambda **kwargs: "tool-span"

    def end_span(span_id, **kwargs):
        spans.append({"spanId": span_id, **kwargs})

    fake_tracing.end_span = end_span
    fake_tracing.flush = lambda: None
    fake_tracing.record_generation = lambda *args, **kwargs: "generation-span"
    fake_tracing.start_agent_span = lambda *args, **kwargs: "agent-span"
    monkeypatch.setitem(sys.modules, "app.langfuse_tracing", fake_tracing)

    fake_registry = types.ModuleType("app.tool_registry")
    fake_registry.build_tools_for_agent = lambda *args, **kwargs: None
    monkeypatch.setitem(sys.modules, "app.tool_registry", fake_registry)

    sys.modules.pop("app.agents", None)
    module = importlib.import_module("app.agents")
    return types.SimpleNamespace(
        module=module,
        AIMessage=AIMessage,
        Record=Record,
        emitted=emitted,
        spans=spans,
    )


class FakeTool:
    def __init__(self, name, result=None, error=None, metadata=None):
        self.name = name
        self.result = result
        self.error = error
        self.metadata = metadata
        self.calls = []

    async def ainvoke(self, args):
        self.calls.append(args)
        if self.error:
            raise self.error
        return self.result


class FakeLlm:
    def __init__(self, responses):
        self.responses = list(responses)

    def bind_tools(self, tools):
        return self

    async def ainvoke(self, messages):
        if not self.responses:
            raise AssertionError("LLM called more times than the harness expected")
        return self.responses.pop(0)


def make_state(runtime):
    return runtime.Record(
        traceId="trace-1",
        workflowRunId="run-1",
        conversationId="conv-1",
        revision=3,
        resumeText="Java backend resume",
        jdResult="{}",
        techResult="",
        projectResult="",
        riskResult="",
        harnessPlan={},
        toolHealth={},
    )


def test_real_agent_loop_enforces_dedup_retrieval_and_tool_budgets(agent_runtime, monkeypatch):
    module = agent_runtime.module
    search = FakeTool("milvus_jd_search", result={"chunks": [], "sourceUrl": "https://example.test/jd"})
    extract = FakeTool("jd_requirements_extract", result={"requirements": []})

    async def tools_for_agent(agent_name, context):
        return [search, extract]

    responses = [
        agent_runtime.AIMessage(
            tool_calls=[
                {"id": "call-1", "name": "milvus_jd_search", "args": {"query": "Java", "topK": 3}},
                {"id": "call-dup", "name": "milvus_jd_search", "args": {"topK": 3, "query": "Java"}},
            ]
        ),
        agent_runtime.AIMessage(
            tool_calls=[
                {"id": "call-over-retrieval", "name": "milvus_jd_search", "args": {"query": "Python"}}
            ]
        ),
        agent_runtime.AIMessage(
            tool_calls=[
                {"id": "call-2", "name": "jd_requirements_extract", "args": {"jdMatchJson": "{}"}}
            ]
        ),
        agent_runtime.AIMessage(
            content='{"matchedJd":"Java backend","matchScore":0.8,"requirements":[],"gaps":[]}'
        ),
    ]
    fake_llm = FakeLlm(responses)
    monkeypatch.setattr(module, "build_tools_for_agent", tools_for_agent)
    monkeypatch.setattr(module, "_llm", lambda *args, **kwargs: fake_llm)

    result = asyncio.run(
        module.run_agent_node("jd_match", "JdMatchAgent", 3, "system", "user", make_state(agent_runtime))
    )

    assert json.loads(result)["matchScore"] == 0.8
    assert len(search.calls) == 1
    assert len(extract.calls) == 1
    tool_events = [event for event in agent_runtime.emitted if event.kind == "tool"]
    reasons = [json.loads(event.toolCalls[0].result).get("reason") for event in tool_events]
    assert "duplicate_tool_call" in reasons
    assert "retrieval_budget_exceeded" in reasons
    assert all(event.workflowRunId == "run-1" for event in agent_runtime.emitted)
    assert all(event.conversationId == "conv-1" for event in agent_runtime.emitted)
    assert all(event.revision == 3 for event in agent_runtime.emitted)
    event_ids = {event.eventId for event in agent_runtime.emitted}
    assert all(
        not getattr(event, "parentEventId", None) or event.parentEventId in event_ids
        for event in agent_runtime.emitted
    )
    assert agent_runtime.spans[-1]["status"] == "SUCCESS"


def test_oversized_tool_proposal_batch_is_rejected_before_context_amplification(
    agent_runtime,
    monkeypatch,
):
    module = agent_runtime.module
    search = FakeTool("milvus_resume_search", result={"chunks": []})

    async def tools_for_agent(agent_name, context):
        return [search]

    oversized = [
        {
            "id": f"call-{index}",
            "name": "milvus_resume_search",
            "args": {"query": f"query-{index}"},
        }
        for index in range(module.MAX_PROPOSED_TOOL_CALLS_PER_ROUND + 1)
    ]
    fake_llm = FakeLlm(
        [
            agent_runtime.AIMessage(tool_calls=oversized),
            agent_runtime.AIMessage(
                content='{"riskLevel":"MEDIUM","risks":[],"evidenceSource":"resume_text_only"}'
            ),
        ]
    )
    monkeypatch.setattr(module, "build_tools_for_agent", tools_for_agent)
    monkeypatch.setattr(module, "_llm", lambda *args, **kwargs: fake_llm)

    result = asyncio.run(
        module.run_agent_node(
            "risk_eval",
            "RiskAgent",
            4,
            "system",
            "user",
            make_state(agent_runtime),
        )
    )

    assert json.loads(result)["riskLevel"] == "MEDIUM"
    assert search.calls == []
    assert not [event for event in agent_runtime.emitted if event.kind == "tool"]


def test_public_mcp_failure_is_traced_and_does_not_fabricate_evidence(agent_runtime, monkeypatch):
    module = agent_runtime.module
    public_mcp = FakeTool("mcp_public_search", error=RuntimeError("public MCP timeout"))

    async def tools_for_agent(agent_name, context):
        return [public_mcp]

    fake_llm = FakeLlm(
        [
            agent_runtime.AIMessage(
                tool_calls=[{
                    "id": "mcp-1",
                    "name": "mcp_public_search",
                    "args": {"query": "https://github.com/example/repository"},
                }]
            ),
            agent_runtime.AIMessage(
                content='{"riskLevel":"MEDIUM","risks":[],"evidenceSource":"resume_text_only"}'
            ),
        ]
    )
    monkeypatch.setattr(module, "build_tools_for_agent", tools_for_agent)
    monkeypatch.setattr(module, "_llm", lambda *args, **kwargs: fake_llm)
    state = make_state(agent_runtime)
    state.resumeText = "Portfolio: https://github.com/example/repository"

    result = asyncio.run(module.run_agent_node("risk_eval", "RiskAgent", 4, "system", "user", state))

    assert json.loads(result)["evidenceSource"] == "resume_text_only"
    failed = [event for event in agent_runtime.emitted if event.kind == "tool" and event.status == "FAILED"]
    assert len(failed) == 1
    assert "public MCP timeout" in failed[0].toolCalls[0].result
    assert state.toolHealth["mcp_public_search"]["status"] == "FAILED"
    assert agent_runtime.spans[-1]["status"] == "SUCCESS"


def test_ungrounded_external_mcp_success_is_redacted_before_model_sees_it(agent_runtime, monkeypatch):
    module = agent_runtime.module
    public_mcp = FakeTool(
        "mcp_public_search",
        result={"results": [{"title": "plausible but source-less claim"}]},
        metadata={
            "externalEvidence": {"provider": "public-search", "requiresSourceUrl": True},
            "evidencePolicy": {"allowSyntheticFallback": False},
        },
    )

    async def tools_for_agent(agent_name, context):
        return [public_mcp]

    class InspectingLlm(FakeLlm):
        async def ainvoke(self, messages):
            if len(self.responses) == 1:
                tool_messages = [message for message in messages if hasattr(message, "tool_call_id")]
                assert tool_messages
                assert "external_evidence_rejected" in tool_messages[-1].content
                assert "plausible but source-less claim" not in tool_messages[-1].content
            return await super().ainvoke(messages)

    fake_llm = InspectingLlm(
        [
            agent_runtime.AIMessage(
                tool_calls=[{
                    "id": "mcp-1",
                    "name": "mcp_public_search",
                    "args": {"query": "example/repository"},
                }]
            ),
            agent_runtime.AIMessage(
                content='{"riskLevel":"MEDIUM","risks":[],"evidenceSource":"resume_text_only"}'
            ),
        ]
    )
    monkeypatch.setattr(module, "build_tools_for_agent", tools_for_agent)
    monkeypatch.setattr(module, "_llm", lambda *args, **kwargs: fake_llm)
    state = make_state(agent_runtime)
    state.resumeText = "Portfolio: https://github.com/example/repository"

    result = asyncio.run(module.run_agent_node("risk_eval", "RiskAgent", 4, "system", "user", state))

    assert json.loads(result)["evidenceSource"] == "resume_text_only"
    failed = [event for event in agent_runtime.emitted if event.kind == "tool" and event.status == "FAILED"]
    assert len(failed) == 1
    assert "missing_source_url" in failed[0].toolCalls[0].result
    assert "plausible but source-less claim" not in failed[0].toolCalls[0].result


def test_discovered_public_mcp_uses_retrieval_budget_and_trace_semantics(agent_runtime, monkeypatch):
    module = agent_runtime.module
    exa = FakeTool(
        "web_search_exa",
        result={
            "results": [
                {
                    "title": "candidate-declared repository",
                    "url": "https://github.com/example/repository",
                }
            ]
        },
        metadata={
            "mcpServer": "exa",
            "externalEvidence": {
                "provider": "exa",
                "kind": "public-web",
                "requiresSourceUrl": True,
            },
        },
    )

    async def tools_for_agent(agent_name, context):
        return [exa]

    fake_llm = FakeLlm(
        [
            agent_runtime.AIMessage(
                tool_calls=[{
                    "id": "exa-1",
                    "name": "web_search_exa",
                    "args": {"query": "example/repository"},
                }]
            ),
            agent_runtime.AIMessage(
                tool_calls=[{
                    "id": "exa-2",
                    "name": "web_search_exa",
                    "args": {"query": "example/repository contributions"},
                }]
            ),
            agent_runtime.AIMessage(
                content='{"dimensions":[],"overallTechScore":72,"evidenceSource":"external_profile"}'
            ),
        ]
    )
    monkeypatch.setattr(module, "build_tools_for_agent", tools_for_agent)
    monkeypatch.setattr(module, "_llm", lambda *args, **kwargs: fake_llm)
    state = make_state(agent_runtime)
    state.resumeText = "Portfolio: https://github.com/example/repository"
    state.harnessPlan = {
        "runtimeBudgets": {
            "TechEvalAgent": {"maxToolCalls": 2, "maxRetrievalQueries": 1}
        }
    }

    asyncio.run(module.run_agent_node("tech_eval", "TechEvalAgent", 4, "system", "user", state))

    tool_events = [event for event in agent_runtime.emitted if event.kind == "tool"]
    assert tool_events[0].toolOrigin == "mcp"
    assert tool_events[0].toolFamily == "retrieval"
    assert tool_events[0].toolCalls[0].server == "exa"
    assert tool_events[0].toolCalls[0].inputHash
    assert json.loads(tool_events[1].toolCalls[0].result)["reason"] == "retrieval_budget_exceeded"
    assert len(exa.calls) == 1


def test_public_profile_lookup_without_declared_identifier_is_blocked(agent_runtime, monkeypatch):
    module = agent_runtime.module
    exa = FakeTool(
        "web_search_exa",
        result={"results": [{"url": "https://example.test/wrong-person"}]},
        metadata={
            "mcpServer": "exa",
            "externalEvidence": {
                "provider": "exa",
                "kind": "public-web",
                "subjectBinding": "unverified",
                "requiresSourceUrl": True,
            },
        },
    )

    async def tools_for_agent(agent_name, context):
        return [exa]

    fake_llm = FakeLlm(
        [
            agent_runtime.AIMessage(
                tool_calls=[{"id": "exa-1", "name": "web_search_exa", "args": {"query": "张三 GitHub"}}]
            ),
            agent_runtime.AIMessage(
                content='{"dimensions":[],"overallTechScore":60,"evidenceSource":"resume_text_only"}'
            ),
        ]
    )
    monkeypatch.setattr(module, "build_tools_for_agent", tools_for_agent)
    monkeypatch.setattr(module, "_llm", lambda *args, **kwargs: fake_llm)
    state = make_state(agent_runtime)

    asyncio.run(module.run_agent_node("tech_eval", "TechEvalAgent", 4, "system", "user", state))

    assert exa.calls == []
    skipped = [event for event in agent_runtime.emitted if event.kind == "tool"]
    assert json.loads(skipped[0].toolCalls[0].result)["reason"] == "candidate_identifier_not_declared"


def test_subject_binding_rejections_still_consume_tool_budget(agent_runtime, monkeypatch):
    module = agent_runtime.module
    exa = FakeTool(
        "web_search_exa",
        result={"results": [{"url": "https://github.com/example/repository"}]},
        metadata={
            "mcpServer": "exa",
            "externalEvidence": {
                "provider": "exa",
                "kind": "public-web",
                "requiresSourceUrl": True,
            },
        },
    )

    async def tools_for_agent(agent_name, context):
        return [exa]

    invalid_calls = [
        {
            "id": f"invalid-{index}",
            "name": "web_search_exa",
            "args": {"query": f"unrelated-person-{index}"},
        }
        for index in range(4)
    ]
    fake_llm = FakeLlm(
        [
            agent_runtime.AIMessage(tool_calls=invalid_calls),
            agent_runtime.AIMessage(
                tool_calls=[{
                    "id": "valid-but-over-budget",
                    "name": "web_search_exa",
                    "args": {"query": "example/repository"},
                }]
            ),
            agent_runtime.AIMessage(
                content='{"dimensions":[],"overallTechScore":60,"evidenceSource":"resume_text_only"}'
            ),
        ]
    )
    monkeypatch.setattr(module, "build_tools_for_agent", tools_for_agent)
    monkeypatch.setattr(module, "_llm", lambda *args, **kwargs: fake_llm)
    state = make_state(agent_runtime)
    state.resumeText = "Portfolio: https://github.com/example/repository"

    asyncio.run(
        module.run_agent_node("tech_eval", "TechEvalAgent", 4, "system", "user", state)
    )

    assert exa.calls == []
    skipped = [event for event in agent_runtime.emitted if event.kind == "tool"]
    assert [json.loads(event.toolCalls[0].result)["reason"] for event in skipped[:4]] == [
        "tool_input_not_bound_to_declared_identifier"
    ] * 4
    assert json.loads(skipped[-1].toolCalls[0].result)["reason"] == "tool_budget_exceeded"


def test_local_tool_failure_fails_node_and_marks_span_failed(agent_runtime, monkeypatch):
    module = agent_runtime.module
    local_tool = FakeTool("timeline_validator", error=RuntimeError("parser invariant broken"))

    async def tools_for_agent(agent_name, context):
        return [local_tool]

    fake_llm = FakeLlm(
        [
            agent_runtime.AIMessage(
                tool_calls=[{"id": "local-1", "name": "timeline_validator", "args": {"resumeText": "x"}}]
            )
        ]
    )
    monkeypatch.setattr(module, "build_tools_for_agent", tools_for_agent)
    monkeypatch.setattr(module, "_llm", lambda *args, **kwargs: fake_llm)

    with pytest.raises(RuntimeError, match="parser invariant broken"):
        asyncio.run(module.run_agent_node("risk_eval", "RiskAgent", 4, "system", "user", make_state(agent_runtime)))

    assert agent_runtime.spans[-1]["status"] == "FAILED"
    assert "parser invariant broken" in agent_runtime.spans[-1]["output_data"]


def test_malformed_final_output_fails_closed_after_max_rounds(agent_runtime, monkeypatch):
    module = agent_runtime.module
    fake_llm = FakeLlm([agent_runtime.AIMessage(content="{}") for _ in range(module.MAX_AGENT_ROUNDS)])

    async def no_tools(agent_name, context):
        return []

    monkeypatch.setattr(module, "build_tools_for_agent", no_tools)
    monkeypatch.setattr(module, "_llm", lambda *args, **kwargs: fake_llm)

    with pytest.raises(RuntimeError, match="invalid_final_output_after_max_rounds"):
        asyncio.run(module.run_agent_node("risk_eval", "RiskAgent", 4, "system", "user", make_state(agent_runtime)))

    assert agent_runtime.spans[-1]["status"] == "FAILED"


def test_agent_output_schema_rejects_present_but_invalid_fields(agent_runtime):
    module = agent_runtime.module

    assert module.agent_final_output_error(
        "JdMatchAgent",
        '{"matchedJd":"x","matchScore":8,"requirements":[],"gaps":[]}',
    ) == "matchScore_out_of_range"
    assert module.agent_final_output_error(
        "TechEvalAgent",
        '{"dimensions":"not-an-array","overallTechScore":80,"evidenceSource":"resume_text"}',
    ) == "dimensions_must_be_array"
    assert module.agent_final_output_error(
        "TechEvalAgent",
        '{"dimensions":[],"overallTechScore":true,"evidenceSource":"resume_text"}',
    ) == "overallTechScore_must_be_numeric"
    assert module.agent_final_output_error(
        "TechEvalAgent",
        '{"dimensions":[{"score":999,"evidenceSource":"resume_text"}],'
        '"overallTechScore":80,"evidenceSource":"resume_text"}',
    ) == "dimensions[0].score_invalid"
    assert module.agent_final_output_error(
        "TechEvalAgent",
        '{"dimensions":[],"overallTechScore":80,"evidenceSource":"github"}',
        evidence_availability={"external": True, "rag": False},
    ) == "evidenceSource_invalid:github"
    external_claim = (
        '{"dimensions":[{"name":"code","score":80,"evidenceSource":"external_profile"}],'
        '"overallTechScore":80,"evidenceSource":"external_profile"}'
    )
    assert module.agent_final_output_error(
        "TechEvalAgent",
        external_claim,
        evidence_availability={"external": False, "rag": False},
    ) == "external_evidence_source_not_available"
    assert module.agent_final_output_error(
        "TechEvalAgent",
        external_claim,
        evidence_availability={"external": True, "rag": False},
    ) is None
    assert module.tool_result_has_error(
        '{"ok":false,"reason":"timeout","url":"https://example.test"}'
    )
    assert module.tool_result_has_error('{"status":"unavailable"}')
    assert module.agent_final_output_error(
        "EvidenceFusionAgent",
        '{"evidenceChain":[],"confidence":null,"confidenceStatus":"NOT_CALIBRATED",'
        '"keyFindings":[],"toolHealth":{}}',
    ) is None
    assert module.agent_final_output_error(
        "EvidenceFusionAgent",
        '{"evidenceChain":[],"confidence":0.78,"confidenceStatus":"CALIBRATED",'
        '"keyFindings":[],"toolHealth":{}}',
    ) == "confidence_must_be_null_without_calibration"


def test_preexecuted_tool_is_not_reoffered_to_adaptive_loop(agent_runtime, monkeypatch):
    module = agent_runtime.module
    timeline = FakeTool("timeline_validator", result={"status": "ok"})

    async def tools_for_agent(agent_name, context):
        return [timeline]

    fake_llm = FakeLlm([
        agent_runtime.AIMessage(
            content='{"riskLevel":"LOW","risks":[],"evidenceSource":"resume_text_only"}'
        )
    ])
    monkeypatch.setattr(module, "build_tools_for_agent", tools_for_agent)
    monkeypatch.setattr(module, "_llm", lambda *args, **kwargs: fake_llm)

    asyncio.run(
        module.run_agent_node(
            "risk_eval",
            "RiskAgent",
            4,
            "system",
            "user",
            make_state(agent_runtime),
            preexecuted_tool_names={"timeline_validator"},
        )
    )

    assert timeline.calls == []


def test_tool_health_preserves_earlier_success_when_later_call_fails(agent_runtime):
    module = agent_runtime.module
    state = make_state(agent_runtime)
    semantics = {
        "origin": "mcp",
        "family": "external_enrichment",
        "server": "exa",
        "operation": "search",
    }

    module._record_tool_health(state, "web_search_exa", semantics, "{}", "SUCCESS")
    module._record_tool_health(state, "web_search_exa", semantics, '{"error":"timeout"}', "FAILED")

    assert state.toolHealth["web_search_exa"]["status"] == "SUCCESS"
    assert state.toolHealth["web_search_exa"]["lastStatus"] == "FAILED"
    assert state.toolHealth["web_search_exa"]["successCount"] == 1
    assert state.toolHealth["web_search_exa"]["failureCount"] == 1


def test_report_parallel_llm_failure_emits_explicit_degraded_trace(agent_runtime, monkeypatch):
    module = agent_runtime.module

    class FailingLlm:
        async def ainvoke(self, messages):
            raise RuntimeError("provider timeout")

    monkeypatch.setattr(module, "_llm", lambda *args, **kwargs: FailingLlm())
    state = make_state(agent_runtime)

    fallback = asyncio.run(
        module.generate_report_eval(
            "evidence",
            state=state,
            agent_span_id="report-parent-span",
        )
    )

    assert "人工复核" in fallback
    assert module.REPORT_EVAL_DEGRADED_MARKER in fallback
    degraded = [event for event in agent_runtime.emitted if event.status == "DEGRADED"]
    assert len(degraded) == 1
    assert degraded[0].workflowRunId == "run-1"
    assert degraded[0].revision == 3
    assert "provider timeout" in degraded[0].outputPreview
