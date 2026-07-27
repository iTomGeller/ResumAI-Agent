from __future__ import annotations

import asyncio
from datetime import datetime

from app.runtime.events import NullEmitter


def test_runtime_event_has_source_occurrence_timestamp_without_mutating_payload() -> None:
    emitter = NullEmitter()
    payload = {"toolCallId": "call-1"}

    asyncio.run(emitter.emit(
        "tool.started", tool_name="example.search", payload=payload))

    recorded = emitter.events[0]["payload"]
    assert payload == {"toolCallId": "call-1"}
    assert recorded["toolCallId"] == "call-1"
    assert recorded["occurredAt"].endswith("Z")
    assert datetime.fromisoformat(recorded["occurredAt"].replace("Z", "+00:00")).tzinfo


def test_runtime_event_preserves_explicit_occurrence_timestamp() -> None:
    emitter = NullEmitter()
    occurred_at = "2026-07-27T10:11:12.345Z"

    asyncio.run(emitter.emit(
        "skill.loaded", payload={"occurredAt": occurred_at}))

    assert emitter.events[0]["payload"]["occurredAt"] == occurred_at
