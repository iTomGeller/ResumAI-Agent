from __future__ import annotations

"""In-process workflow run registry and LangGraph-safe control boundary.

MySQL remains the durable business-state owner.  This registry owns only live
``asyncio.Task`` objects and cooperative pause flags.  Pause state itself is
made durable by LangGraph's checkpointer when ``interrupt`` is called.
"""

import asyncio
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, Mapping, Optional


class RunStatus(str, Enum):
    ACCEPTED = "ACCEPTED"
    RUNNING = "RUNNING"
    PAUSING = "PAUSING"
    PAUSED = "PAUSED"
    RESUMING = "RESUMING"
    SUCCESS = "SUCCESS"
    PARTIAL_SUCCESS = "PARTIAL_SUCCESS"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


TERMINAL_STATUSES = {
    RunStatus.SUCCESS,
    RunStatus.PARTIAL_SUCCESS,
    RunStatus.FAILED,
    RunStatus.CANCELLED,
}


class RunNotFoundError(KeyError):
    pass


class InvalidRunTransition(RuntimeError):
    pass


class PauseUnavailableError(RuntimeError):
    pass


@dataclass(frozen=True)
class RunSnapshot:
    workflow_run_id: str
    trace_id: str
    conversation_id: str
    revision: int
    status: RunStatus
    pause_requested: bool
    resume_pending: bool
    task_active: bool
    updated_at: float


@dataclass(frozen=True)
class ResumePlan:
    snapshot: RunSnapshot
    restart_required: bool


@dataclass
class _RunHandle:
    workflow_run_id: str
    trace_id: str
    conversation_id: str
    revision: int
    initial_state: Dict[str, Any]
    status: RunStatus = RunStatus.ACCEPTED
    pause_requested: bool = False
    resume_pending: bool = False
    task: Optional[asyncio.Task] = None
    updated_at: float = field(default_factory=time.monotonic)

    def snapshot(self) -> RunSnapshot:
        task_active = self.task is not None and not self.task.done()
        return RunSnapshot(
            workflow_run_id=self.workflow_run_id,
            trace_id=self.trace_id,
            conversation_id=self.conversation_id,
            revision=self.revision,
            status=self.status,
            pause_requested=self.pause_requested,
            resume_pending=self.resume_pending,
            task_active=task_active,
            updated_at=self.updated_at,
        )


class RunRegistry:
    def __init__(self, max_retained: int = 500) -> None:
        self._runs: Dict[str, _RunHandle] = {}
        self._lock = asyncio.Lock()
        self._max_retained = max(20, max_retained)

    async def register(
        self,
        workflow_run_id: str,
        trace_id: str,
        conversation_id: str,
        revision: int,
        initial_state: Mapping[str, Any],
    ) -> RunSnapshot:
        async with self._lock:
            existing = self._runs.get(workflow_run_id)
            if existing is not None:
                same_identity = (
                    existing.trace_id == trace_id
                    and existing.conversation_id == conversation_id
                    and existing.revision == max(1, int(revision))
                )
                if not same_identity:
                    raise InvalidRunTransition(
                        f"workflow run id is already bound to another identity: {workflow_run_id}"
                    )
                if existing.status in TERMINAL_STATUSES:
                    # Run identifiers are immutable.  In particular, a
                    # CANCELLED handle may be a pre-start tombstone created by
                    # a cancellation that beat the start request.
                    return existing.snapshot()
                raise InvalidRunTransition(f"workflow run already active: {workflow_run_id}")
            duplicate_trace = next(
                (
                    handle
                    for handle in self._runs.values()
                    if handle.trace_id == trace_id and handle.status not in TERMINAL_STATUSES
                ),
                None,
            )
            if duplicate_trace is not None:
                raise InvalidRunTransition(
                    f"trace already has active workflow run: {duplicate_trace.workflow_run_id}"
                )
            self._prune_locked()
            handle = _RunHandle(
                workflow_run_id=workflow_run_id,
                trace_id=trace_id,
                conversation_id=conversation_id,
                revision=max(1, int(revision)),
                initial_state=dict(initial_state),
            )
            self._runs[workflow_run_id] = handle
            return handle.snapshot()

    async def cancel_or_tombstone(
        self,
        workflow_run_id: str,
        trace_id: str,
        conversation_id: str,
        revision: int,
    ) -> RunSnapshot:
        """Cancel a live run or fence a start request that has not arrived yet.

        Java assigns ``workflow_run_id`` before enqueueing.  A revision change
        or user cancellation can therefore legitimately reach this process
        before ``POST /workflow/runs``.  Recording a terminal handle closes
        that race: a later start with the same immutable identity is accepted
        idempotently but never scheduled.
        """

        async with self._lock:
            existing = self._runs.get(workflow_run_id)
            normalized_revision = max(1, int(revision))
            if existing is None:
                self._prune_locked()
                handle = _RunHandle(
                    workflow_run_id=workflow_run_id,
                    trace_id=trace_id,
                    conversation_id=conversation_id,
                    revision=normalized_revision,
                    initial_state={
                        "workflowRunId": workflow_run_id,
                        "traceId": trace_id,
                        "conversationId": conversation_id,
                        "revision": normalized_revision,
                    },
                    status=RunStatus.CANCELLED,
                )
                self._runs[workflow_run_id] = handle
                return handle.snapshot()

            same_identity = (
                existing.trace_id == trace_id
                and existing.conversation_id == conversation_id
                and existing.revision == normalized_revision
            )
            if not same_identity:
                raise InvalidRunTransition(
                    f"workflow run id is already bound to another identity: {workflow_run_id}"
                )
            if existing.status in {
                RunStatus.SUCCESS,
                RunStatus.PARTIAL_SUCCESS,
                RunStatus.FAILED,
            }:
                raise InvalidRunTransition(f"cannot cancel terminal run in {existing.status.value}")
            existing.status = RunStatus.CANCELLED
            existing.pause_requested = False
            existing.resume_pending = False
            existing.updated_at = time.monotonic()
            task = existing.task
            if task is not None and not task.done():
                task.cancel()
            return existing.snapshot()

    async def get(self, workflow_run_id: str) -> Optional[RunSnapshot]:
        async with self._lock:
            handle = self._runs.get(workflow_run_id)
            return handle.snapshot() if handle is not None else None

    async def restore_paused(
        self,
        workflow_run_id: str,
        trace_id: str,
        conversation_id: str,
        revision: int,
        initial_state: Mapping[str, Any],
    ) -> RunSnapshot:
        """Rehydrate registry metadata after a process restart.

        The graph state itself remains in the durable LangGraph checkpointer;
        only the identifiers required to address that checkpoint are restored.
        """

        async with self._lock:
            existing = self._runs.get(workflow_run_id)
            if existing is not None:
                return existing.snapshot()
            self._prune_locked()
            handle = _RunHandle(
                workflow_run_id=workflow_run_id,
                trace_id=trace_id,
                conversation_id=conversation_id,
                revision=max(1, int(revision)),
                initial_state=dict(initial_state),
                status=RunStatus.PAUSED,
                pause_requested=True,
            )
            self._runs[workflow_run_id] = handle
            return handle.snapshot()

    async def require(self, workflow_run_id: str) -> RunSnapshot:
        snapshot = await self.get(workflow_run_id)
        if snapshot is None:
            raise RunNotFoundError(workflow_run_id)
        return snapshot

    async def initial_state(self, workflow_run_id: str) -> Dict[str, Any]:
        async with self._lock:
            handle = self._require_locked(workflow_run_id)
            return dict(handle.initial_state)

    async def find_active_by_trace(self, trace_id: str) -> Optional[RunSnapshot]:
        async with self._lock:
            for handle in self._runs.values():
                if handle.trace_id == trace_id and handle.status not in TERMINAL_STATUSES:
                    return handle.snapshot()
            return None

    async def attach_task(self, workflow_run_id: str, task: asyncio.Task) -> RunSnapshot:
        async with self._lock:
            handle = self._require_locked(workflow_run_id)
            if handle.task is not None and not handle.task.done() and handle.task is not task:
                raise InvalidRunTransition(f"workflow run already has a live task: {workflow_run_id}")
            handle.task = task
            if handle.status == RunStatus.ACCEPTED:
                handle.status = RunStatus.RUNNING
            handle.updated_at = time.monotonic()
            return handle.snapshot()

    async def detach_task(self, workflow_run_id: str, task: asyncio.Task) -> Optional[RunSnapshot]:
        async with self._lock:
            handle = self._runs.get(workflow_run_id)
            if handle is None:
                return None
            if handle.task is task:
                handle.task = None
                handle.updated_at = time.monotonic()
            return handle.snapshot()

    async def request_pause(self, workflow_run_id: str) -> RunSnapshot:
        async with self._lock:
            handle = self._require_locked(workflow_run_id)
            if handle.status in TERMINAL_STATUSES:
                raise InvalidRunTransition(f"cannot pause terminal run in {handle.status.value}")
            if handle.status == RunStatus.PAUSED:
                return handle.snapshot()
            handle.pause_requested = True
            handle.resume_pending = False
            handle.status = RunStatus.PAUSING
            handle.updated_at = time.monotonic()
            return handle.snapshot()

    async def mark_paused(self, workflow_run_id: str) -> RunSnapshot:
        async with self._lock:
            handle = self._require_locked(workflow_run_id)
            if handle.status == RunStatus.CANCELLED:
                return handle.snapshot()
            handle.pause_requested = True
            handle.status = RunStatus.PAUSED
            handle.updated_at = time.monotonic()
            return handle.snapshot()

    async def request_resume(self, workflow_run_id: str) -> ResumePlan:
        async with self._lock:
            handle = self._require_locked(workflow_run_id)
            task_active = handle.task is not None and not handle.task.done()
            if handle.status == RunStatus.PAUSING and task_active:
                # The safe boundary has not been reached yet; withdrawing the
                # pause request requires no checkpoint replay.
                handle.pause_requested = False
                handle.resume_pending = False
                handle.status = RunStatus.RUNNING
                handle.updated_at = time.monotonic()
                return ResumePlan(handle.snapshot(), restart_required=False)
            if handle.status == RunStatus.RUNNING:
                return ResumePlan(handle.snapshot(), restart_required=False)
            if handle.status != RunStatus.PAUSED:
                raise InvalidRunTransition(f"cannot resume run in {handle.status.value}")
            handle.status = RunStatus.RESUMING
            # Keep this true until the re-executed interrupt call consumes the
            # Command(resume=...) value from LangGraph.
            handle.pause_requested = True
            handle.resume_pending = True
            handle.updated_at = time.monotonic()
            return ResumePlan(handle.snapshot(), restart_required=True)

    async def mark_boundary_resumed(self, workflow_run_id: str) -> RunSnapshot:
        async with self._lock:
            handle = self._require_locked(workflow_run_id)
            if handle.status == RunStatus.CANCELLED:
                return handle.snapshot()
            handle.pause_requested = False
            handle.resume_pending = False
            handle.status = RunStatus.RUNNING
            handle.updated_at = time.monotonic()
            return handle.snapshot()

    async def cancel(self, workflow_run_id: str) -> RunSnapshot:
        async with self._lock:
            handle = self._require_locked(workflow_run_id)
            if handle.status in {
                RunStatus.SUCCESS,
                RunStatus.PARTIAL_SUCCESS,
                RunStatus.FAILED,
            }:
                raise InvalidRunTransition(f"cannot cancel terminal run in {handle.status.value}")
            handle.status = RunStatus.CANCELLED
            handle.pause_requested = False
            handle.resume_pending = False
            handle.updated_at = time.monotonic()
            task = handle.task
            if task is not None and not task.done():
                task.cancel()
            return handle.snapshot()

    async def finish(self, workflow_run_id: str, status: RunStatus) -> RunSnapshot:
        if status not in TERMINAL_STATUSES:
            raise ValueError(f"finish requires a terminal status, got {status.value}")
        async with self._lock:
            handle = self._require_locked(workflow_run_id)
            # Cancellation wins over a late success/failure from an upstream
            # provider that did not stop immediately.
            if handle.status != RunStatus.CANCELLED:
                handle.status = status
            handle.pause_requested = False
            handle.resume_pending = False
            handle.updated_at = time.monotonic()
            return handle.snapshot()

    async def list_snapshots(self) -> Dict[str, RunSnapshot]:
        async with self._lock:
            return {key: handle.snapshot() for key, handle in self._runs.items()}

    def _require_locked(self, workflow_run_id: str) -> _RunHandle:
        handle = self._runs.get(workflow_run_id)
        if handle is None:
            raise RunNotFoundError(workflow_run_id)
        return handle

    def _prune_locked(self) -> None:
        overflow = len(self._runs) - self._max_retained + 1
        if overflow <= 0:
            return
        terminal = sorted(
            (handle for handle in self._runs.values() if handle.status in TERMINAL_STATUSES),
            key=lambda handle: handle.updated_at,
        )
        for handle in terminal[:overflow]:
            self._runs.pop(handle.workflow_run_id, None)


default_run_registry = RunRegistry()


async def safe_control_boundary(
    state: Mapping[str, Any],
    *,
    registry: RunRegistry = default_run_registry,
    interrupter: Optional[Callable[[Dict[str, Any]], Any]] = None,
) -> Optional[Any]:
    """Apply cancellation/pause at a LangGraph node boundary.

    On the first call, LangGraph's ``interrupt`` persists the current graph
    state and exits the invocation.  On replay with ``Command(resume=...)`` the
    same call returns, after which the registry transitions back to RUNNING.
    An injectable interrupter keeps the state machine testable without
    LangGraph or Postgres.
    """

    workflow_run_id = str(state.get("workflowRunId") or "").strip()
    if not workflow_run_id:
        return None
    snapshot = await registry.get(workflow_run_id)
    if snapshot is None:
        # Direct harness calls that bypass FastAPI remain backward compatible.
        return None
    if snapshot.status == RunStatus.CANCELLED:
        raise asyncio.CancelledError()
    if not snapshot.pause_requested and snapshot.status not in {
        RunStatus.PAUSING,
        RunStatus.PAUSED,
        RunStatus.RESUMING,
    }:
        return None

    payload = {
        "kind": "USER_PAUSE",
        "workflowRunId": workflow_run_id,
        "traceId": snapshot.trace_id,
        "conversationId": snapshot.conversation_id,
        "revision": snapshot.revision,
    }
    if interrupter is None:
        try:
            from langgraph.types import interrupt as langgraph_interrupt
        except Exception as exc:  # pragma: no cover - depends on deployment deps
            raise PauseUnavailableError("LangGraph interrupt is unavailable") from exc
        interrupter = langgraph_interrupt

    resume_value = interrupter(payload)
    await registry.mark_boundary_resumed(workflow_run_id)
    return resume_value
