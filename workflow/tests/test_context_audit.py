from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

WORKFLOW_ROOT = Path(__file__).resolve().parents[1]
if str(WORKFLOW_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKFLOW_ROOT))

from app.config import settings
from app.runtime.events import NullEmitter
from app.runtime.llm import LlmToolCall, LlmTurn, ResilientLlmClient
from app.runtime.models import RunBudget


def test_context_audit_persists_exact_messages_tools_and_native_response(
        monkeypatch):
    monkeypatch.setattr(settings, "context_audit_enabled", True)
    emitter = NullEmitter("run-audit", "conv-audit", "trace-audit")
    client = ResilientLlmClient(
        emitter, RunBudget(), max_llm_calls=3, llm_timeout_seconds=5)
    client.api_key = "test-only"
    messages = [
        {"role": "system", "content": "system + policy + skill"},
        {"role": "user", "content": "resume + JD + memory"},
    ]
    tools = [{
        "type": "function",
        "function": {
            "name": "load_skill",
            "description": "lazy load one skill",
            "parameters": {
                "type": "object",
                "properties": {"skillId": {"type": "string"}},
                "required": ["skillId"],
            },
        },
    }]

    async def fake_invoke(*_args, **_kwargs):
        return (
            LlmTurn(
                content="",
                tool_calls=[LlmToolCall(
                    tool_call_id="call-1", name="load_skill",
                    arguments={"skillId": "assess-technical-evidence"},
                    raw_arguments=(
                        '{"skillId":"assess-technical-evidence"}'))],
                finish_reason="tool_calls"),
            {
                "prompt_tokens": 123,
                "completion_tokens": 17,
                "prompt_cache_hit_tokens": 80,
            },
            "tool_calls",
        )

    client._invoke = fake_invoke
    turn = asyncio.run(client.chat_turn(
        messages,
        agent_id="TechAgent",
        purpose="technical_findings",
        tools=tools,
        tool_choice="auto",
        trace_context={"roundId": "round-1", "contextRole": "MODEL_INPUT"}))

    assert turn.tool_calls[0].name == "load_skill"
    assert len(emitter.llm_invocations) == 1
    row = emitter.llm_invocations[0]
    prompt = json.loads(row["prompt"])
    response = json.loads(row["response"])
    request = prompt["providerRequest"]
    assert prompt["runId"] == "run-audit"
    assert prompt["traceContext"]["roundId"] == "round-1"
    assert request["messages"] == messages
    assert request["tools"] == tools
    assert request["tool_choice"] == "auto"
    assert request["thinking"] == {"type": "disabled"}
    assert response["toolCalls"][0]["rawArguments"] == (
        '{"skillId":"assess-technical-evidence"}')
    assert response["usage"]["prompt_cache_hit_tokens"] == 80
    assert row["inputTokens"] == 123
    assert row["outputTokens"] == 17


def test_context_audit_is_off_by_default(monkeypatch):
    monkeypatch.setattr(settings, "context_audit_enabled", False)
    emitter = NullEmitter("run-off", "conv-off", "trace-off")
    client = ResilientLlmClient(
        emitter, RunBudget(), max_llm_calls=2, llm_timeout_seconds=5)
    client.api_key = "test-only"

    async def fake_invoke(*_args, **_kwargs):
        return (
            LlmTurn(content="ok", finish_reason="stop"),
            {"prompt_tokens": 2, "completion_tokens": 1},
            "stop",
        )

    client._invoke = fake_invoke
    answer = asyncio.run(client.chat(
        [{"role": "user", "content": "hello"}],
        agent_id="TechAgent", json_mode=False))
    assert answer == "ok"
    assert emitter.llm_invocations == []
