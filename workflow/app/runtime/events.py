from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, Optional

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

RUN_EVENT_TYPES = {
    "run.queued", "run.started", "run.progress",
    "agent.selected", "agent.started", "agent.progress", "agent.completed", "agent.failed",
    "llm.started", "llm.retrying", "llm.completed", "llm.failed",
    "tool.started", "tool.progress", "tool.completed", "tool.failed",
    "sandbox.started", "sandbox.progress", "sandbox.completed", "sandbox.failed",
    "context.compacted",
    "run.cancelling", "run.cancelled", "run.completed", "run.failed", "run.timed_out",
}


def _headers() -> Dict[str, str]:
    return {
        "Content-Type": "application/json",
        "X-Internal-Token": settings.workflow_internal_token,
    }


class RuntimeEmitter:
    """Delivers run events / final result to the Java control plane.

    Events are best-effort with limited retry (they can be replayed from the
    runtime trajectory); the final result retries hard because losing it would
    orphan the run until the watchdog closes it.
    """

    def __init__(self, run_id: str, conversation_id: str, trace_id: str) -> None:
        self.run_id = run_id
        self.conversation_id = conversation_id
        self.trace_id = trace_id
        self._base = settings.java_backend_url.rstrip("/")

    async def emit(self, event_type: str, *, agent_id: Optional[str] = None,
                   tool_name: Optional[str] = None,
                   payload: Optional[Dict[str, Any]] = None) -> None:
        if event_type not in RUN_EVENT_TYPES:
            logger.warning("unknown event type %s", event_type)
        body = {
            "runId": self.run_id,
            "eventType": event_type,
            "agentId": agent_id,
            "toolName": tool_name,
            "payload": payload or {},
        }
        await self._post("/api/internal/agent-runs/events", body, attempts=2, timeout=10.0)

    async def emit_result(self, result: Dict[str, Any]) -> bool:
        body = dict(result)
        body["runId"] = self.run_id
        return await self._post("/api/internal/agent-runs/result", body, attempts=8, timeout=15.0)

    async def _post(self, path: str, body: Dict[str, Any], *, attempts: int, timeout: float) -> bool:
        url = f"{self._base}{path}"
        delay = 1.0
        for attempt in range(1, attempts + 1):
            try:
                async with httpx.AsyncClient(timeout=timeout) as client:
                    response = await client.post(url, json=body, headers=_headers())
                if response.status_code < 400:
                    return True
                retryable = response.status_code == 429 or response.status_code >= 500
                logger.warning("%s failed status=%s body=%s", path,
                               response.status_code, response.text[:200])
                if not retryable:
                    return False
            except asyncio.CancelledError:
                # During cancellation we still try to flush exactly once more.
                raise
            except Exception as exc:
                logger.warning("%s transport error attempt=%s: %s", path, attempt, exc)
            if attempt < attempts:
                await asyncio.sleep(delay)
                delay = min(delay * 2, 20.0)
        return False


class NullEmitter(RuntimeEmitter):
    """Test/benchmark emitter that records events in memory."""

    def __init__(self, run_id: str = "test-run", conversation_id: str = "test-conv",
                 trace_id: str = "test-trace") -> None:
        super().__init__(run_id, conversation_id, trace_id)
        self.events: list[Dict[str, Any]] = []
        self.result: Optional[Dict[str, Any]] = None

    async def emit(self, event_type: str, *, agent_id: Optional[str] = None,
                   tool_name: Optional[str] = None,
                   payload: Optional[Dict[str, Any]] = None) -> None:
        self.events.append({
            "eventType": event_type,
            "agentId": agent_id,
            "toolName": tool_name,
            "payload": payload or {},
        })

    async def emit_result(self, result: Dict[str, Any]) -> bool:
        self.result = result
        return True
