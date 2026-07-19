from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException

from app.runtime.events import RuntimeEmitter
from app.runtime.executor import RunExecutor
from app.runtime.models import AgentRunRequest, CancelRequest

logger = logging.getLogger(__name__)

router = APIRouter()


class _AgentRunHandle:
    def __init__(self, request: AgentRunRequest) -> None:
        self.request = request
        self.status = "ACCEPTED"
        self.task: Optional[asyncio.Task] = None
        self.updated_at = time.monotonic()
        self.cancel_reason: Optional[str] = None

    def snapshot(self) -> Dict[str, Any]:
        return {
            "runId": self.request.runId,
            "conversationId": self.request.conversationId,
            "status": self.status,
            "taskActive": self.task is not None and not self.task.done(),
            "cancelReason": self.cancel_reason,
        }


class AgentRunRegistry:
    """Live-run registry for the agent runtime: idempotent start by runId,
    cooperative cancellation, terminal tombstones for cancel-before-start."""

    def __init__(self, max_retained: int = 300) -> None:
        self._runs: Dict[str, _AgentRunHandle] = {}
        self._lock = asyncio.Lock()
        self._max_retained = max_retained

    async def register(self, request: AgentRunRequest) -> Optional[_AgentRunHandle]:
        async with self._lock:
            existing = self._runs.get(request.runId)
            if existing is not None:
                if existing.status in {"SUCCEEDED", "FAILED", "CANCELLED", "TIMED_OUT"}:
                    return None  # idempotent duplicate of a finished/tombstoned run
                if existing.task is not None and not existing.task.done():
                    return None  # already running
                return existing
            self._prune()
            handle = _AgentRunHandle(request)
            self._runs[request.runId] = handle
            return handle

    async def attach(self, run_id: str, task: asyncio.Task) -> None:
        async with self._lock:
            handle = self._runs.get(run_id)
            if handle is not None:
                handle.task = task
                handle.status = "RUNNING"
                handle.updated_at = time.monotonic()

    async def finish(self, run_id: str, status: str) -> None:
        async with self._lock:
            handle = self._runs.get(run_id)
            if handle is not None:
                if handle.status != "CANCELLED":
                    handle.status = status
                handle.updated_at = time.monotonic()

    async def cancel(self, run_id: str, reason: str) -> Dict[str, Any]:
        async with self._lock:
            handle = self._runs.get(run_id)
            if handle is None:
                # tombstone: a cancel racing ahead of the start request
                tombstone = _AgentRunHandle(AgentRunRequest(
                    runId=run_id, conversationId="unknown"))
                tombstone.status = "CANCELLED"
                tombstone.cancel_reason = reason
                self._runs[run_id] = tombstone
                return tombstone.snapshot()
            handle.cancel_reason = reason
            if handle.status not in {"SUCCEEDED", "FAILED", "TIMED_OUT"}:
                handle.status = "CANCELLED"
            handle.updated_at = time.monotonic()
            if handle.task is not None and not handle.task.done():
                handle.task.cancel()
            return handle.snapshot()

    async def get(self, run_id: str) -> Optional[Dict[str, Any]]:
        async with self._lock:
            handle = self._runs.get(run_id)
            return handle.snapshot() if handle else None

    async def active_count(self) -> int:
        async with self._lock:
            return sum(1 for h in self._runs.values()
                       if h.task is not None and not h.task.done())

    async def cancel_all(self) -> None:
        async with self._lock:
            for handle in self._runs.values():
                if handle.task is not None and not handle.task.done():
                    handle.task.cancel()

    def _prune(self) -> None:
        if len(self._runs) <= self._max_retained:
            return
        finished = sorted(
            (h for h in self._runs.values()
             if h.status in {"SUCCEEDED", "FAILED", "CANCELLED", "TIMED_OUT"}),
            key=lambda h: h.updated_at)
        for handle in finished[: len(self._runs) - self._max_retained + 1]:
            self._runs.pop(handle.request.runId, None)


agent_run_registry = AgentRunRegistry()


async def _execute_run(handle: _AgentRunHandle) -> None:
    request = handle.request
    emitter = RuntimeEmitter(request.runId, request.conversationId, request.traceId)
    try:
        executor = RunExecutor(request, emitter)
        result = await executor.execute()
        await agent_run_registry.finish(request.runId, result.get("status", "FAILED"))
        snapshot = await agent_run_registry.get(request.runId)
        if snapshot and snapshot["status"] == "CANCELLED":
            result["status"] = "CANCELLED"
        await emitter.emit_result(result)
    except asyncio.CancelledError:
        logger.info("agent run cancelled run=%s", request.runId)
        await agent_run_registry.finish(request.runId, "CANCELLED")
        try:
            await emitter.emit_result({
                "status": "CANCELLED",
                "answer": "",
                "errorCode": "CANCELLED",
                "errorMessage": handle.cancel_reason or "run cancelled",
            })
        except Exception:  # noqa: BLE001 - cancellation cleanup best effort
            pass
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("agent run crashed run=%s", request.runId)
        await agent_run_registry.finish(request.runId, "FAILED")
        await emitter.emit_result({
            "status": "FAILED",
            "answer": "",
            "errorCode": "RUNTIME_CRASH",
            "errorMessage": str(exc)[:600],
        })


@router.post("/agent/runs")
async def start_agent_run(request: AgentRunRequest) -> Dict[str, Any]:
    if not request.runId or not request.conversationId:
        raise HTTPException(status_code=400, detail="runId and conversationId required")
    handle = await agent_run_registry.register(request)
    if handle is None:
        snapshot = await agent_run_registry.get(request.runId)
        return {"runId": request.runId, "status": snapshot["status"] if snapshot else "DUPLICATE"}
    task = asyncio.create_task(_execute_run(handle))
    await agent_run_registry.attach(request.runId, task)
    return {"runId": request.runId, "status": "ACCEPTED"}


@router.get("/agent/runs/{run_id}")
async def get_agent_run(run_id: str) -> Dict[str, Any]:
    snapshot = await agent_run_registry.get(run_id)
    if snapshot is None:
        raise HTTPException(status_code=404, detail="agent run not found")
    return snapshot


@router.post("/agent/runs/{run_id}/cancel")
async def cancel_agent_run(run_id: str, request: CancelRequest) -> Dict[str, Any]:
    return await agent_run_registry.cancel(run_id, request.reason)
