from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

RUN_EVENT_TYPES = {
    "run.queued", "run.started", "run.progress",
    "agent.selected", "agent.started", "agent.progress", "agent.completed", "agent.failed",
    "llm.context.attached", "llm.queued", "llm.started", "llm.first_token",
    "llm.retrying", "llm.completed", "llm.failed",
    "report.section.completed",
    "tool.started", "tool.progress", "tool.completed", "tool.failed",
    "mcp.catalog.exposed", "mcp.tool.proposed",
    "skill.catalog", "skill.catalog.exposed", "skill.selected", "skill.loaded",
    "skill.applied", "skill.skipped", "skill.failed",
    "retrieval.started", "retrieval.completed", "retrieval.failed",
    "memory.selected", "memory.used", "memory.written", "memory.skipped",
    # Legacy aliases retained for reading historical events only — do not emit for new runs.
    "skill.started", "skill.completed",
    "context.compacted",
    "run.cancelling", "run.cancelled", "run.completed", "run.failed", "run.timed_out",
}


def _utc_now_iso() -> str:
    """Return a stable, sortable source timestamp for the audit timeline."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _event_payload(payload: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    # Copy rather than mutate caller-owned dictionaries.  `occurredAt` is the
    # runtime occurrence time; the Java `create_time` remains ingestion time.
    event_payload = dict(payload or {})
    event_payload.setdefault("occurredAt", _utc_now_iso())
    return event_payload


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
        # A trace emits hundreds of events. Reusing one per-run connection
        # pool avoids creating a fresh TCP client for every event while still
        # keeping lifecycle and cancellation ownership explicit.
        self._client: Optional[httpx.AsyncClient] = None
        # Python 3.8 binds asyncio.Lock at construction time. Emitters are
        # also instantiated by synchronous planners/tests, so create the lock
        # lazily inside the loop that performs the first network operation.
        self._client_lock: Optional[asyncio.Lock] = None

    def _lock(self) -> asyncio.Lock:
        if self._client_lock is None:
            self._client_lock = asyncio.Lock()
        return self._client_lock

    async def _http_client(self) -> httpx.AsyncClient:
        async with self._lock():
            if self._client is None or self._client.is_closed:
                self._client = httpx.AsyncClient(
                    limits=httpx.Limits(
                        max_connections=8,
                        max_keepalive_connections=4,
                        keepalive_expiry=30.0,
                    ))
            return self._client

    async def aclose(self) -> None:
        async with self._lock():
            client = self._client
            self._client = None
        if client is not None and not client.is_closed:
            await client.aclose()

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
            "payload": _event_payload(payload),
        }
        await self._post("/api/internal/agent-runs/events", body, attempts=2, timeout=10.0)

    async def emit_result(self, result: Dict[str, Any]) -> bool:
        body = dict(result)
        body["runId"] = self.run_id
        return await self._post("/api/internal/agent-runs/result", body, attempts=8, timeout=15.0)

    async def save_checkpoint(self, snapshot: Dict[str, Any]) -> bool:
        """Group-boundary checkpoint: persisted on the Java side so a FAILED
        run can be retried from the last completed agent group."""
        return await self._post(
            f"/api/internal/agent-runs/{self.run_id}/checkpoint",
            {"runId": self.run_id, "executionSnapshot": snapshot},
            attempts=2, timeout=10.0)

    async def save_llm_invocation(self, invocation: Dict[str, Any]) -> bool:
        """Persist one exact provider attempt without putting it on SSE.

        Full prompts belong in ``llm_invocation``: they must be durable for
        Context Audit, but replaying them as ``run_event`` payloads would leak
        candidate data to browser subscribers and inflate every trace stream.
        """
        if not settings.context_audit_enabled:
            return False
        body = dict(invocation)
        body.setdefault("runId", self.run_id)
        body.setdefault("traceId", self.trace_id)
        return await self._post(
            "/api/internal/agent-runs/llm-invocations", body,
            attempts=2, timeout=20.0)

    async def _post(self, path: str, body: Dict[str, Any], *, attempts: int, timeout: float) -> bool:
        url = f"{self._base}{path}"
        delay = 1.0
        for attempt in range(1, attempts + 1):
            try:
                client = await self._http_client()
                response = await client.post(
                    url, json=body, headers=_headers(), timeout=timeout)
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
        self.checkpoints: list[Dict[str, Any]] = []
        self.llm_invocations: list[Dict[str, Any]] = []

    async def emit(self, event_type: str, *, agent_id: Optional[str] = None,
                   tool_name: Optional[str] = None,
                   payload: Optional[Dict[str, Any]] = None) -> None:
        self.events.append({
            "eventType": event_type,
            "agentId": agent_id,
            "toolName": tool_name,
            "payload": _event_payload(payload),
        })

    async def emit_result(self, result: Dict[str, Any]) -> bool:
        self.result = result
        return True

    async def save_checkpoint(self, snapshot: Dict[str, Any]) -> bool:
        self.checkpoints.append(snapshot)
        return True

    async def save_llm_invocation(self, invocation: Dict[str, Any]) -> bool:
        body = dict(invocation)
        body.setdefault("runId", self.run_id)
        body.setdefault("traceId", self.trace_id)
        self.llm_invocations.append(body)
        return True
