from __future__ import annotations

import asyncio
import sys
from pathlib import Path

WORKFLOW_ROOT = Path(__file__).resolve().parents[1]
if str(WORKFLOW_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKFLOW_ROOT))

from app.run_control import (
    InvalidRunTransition,
    RunRegistry,
    RunStatus,
    safe_control_boundary,
)


def test_cancel_immediately_cancels_live_asyncio_task() -> None:
    async def scenario() -> None:
        registry = RunRegistry()
        await registry.register("wr-1", "trace-1", "conv-1", 1, {"workflowRunId": "wr-1"})
        started = asyncio.Event()

        async def worker() -> None:
            started.set()
            await asyncio.Event().wait()

        task = asyncio.create_task(worker())
        await registry.attach_task("wr-1", task)
        await started.wait()
        snapshot = await registry.cancel("wr-1")

        assert snapshot.status == RunStatus.CANCELLED
        try:
            await task
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("live workflow task was not cancelled")

    asyncio.run(scenario())


def test_resume_before_safe_boundary_withdraws_pause_without_restart() -> None:
    async def scenario() -> None:
        registry = RunRegistry()
        await registry.register("wr-2", "trace-2", "conv-2", 1, {"workflowRunId": "wr-2"})
        task = asyncio.create_task(asyncio.Event().wait())
        await registry.attach_task("wr-2", task)

        pausing = await registry.request_pause("wr-2")
        plan = await registry.request_resume("wr-2")

        assert pausing.status == RunStatus.PAUSING
        assert plan.restart_required is False
        assert plan.snapshot.status == RunStatus.RUNNING
        assert plan.snapshot.pause_requested is False
        await registry.cancel("wr-2")
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(scenario())


def test_safe_boundary_interrupt_and_same_run_resume() -> None:
    class FakeInterrupt(Exception):
        pass

    async def scenario() -> None:
        registry = RunRegistry()
        state = {
            "workflowRunId": "wr-3",
            "traceId": "trace-3",
            "conversationId": "conv-3",
            "revision": 4,
        }
        await registry.register("wr-3", "trace-3", "conv-3", 4, state)
        await registry.request_pause("wr-3")
        captured = {}

        def first_interrupt(payload):
            captured.update(payload)
            raise FakeInterrupt()

        try:
            await safe_control_boundary(state, registry=registry, interrupter=first_interrupt)
        except FakeInterrupt:
            pass
        else:
            raise AssertionError("first pause boundary must interrupt graph execution")

        assert captured["workflowRunId"] == "wr-3"
        await registry.mark_paused("wr-3")
        plan = await registry.request_resume("wr-3")
        assert plan.restart_required is True
        assert plan.snapshot.status == RunStatus.RESUMING

        resumed = await safe_control_boundary(
            state,
            registry=registry,
            interrupter=lambda payload: {"action": "RESUME", "revision": payload["revision"]},
        )
        snapshot = await registry.require("wr-3")
        assert resumed["action"] == "RESUME"
        assert snapshot.status == RunStatus.RUNNING
        assert snapshot.pause_requested is False

    asyncio.run(scenario())


def test_cancelled_status_wins_over_late_success() -> None:
    async def scenario() -> None:
        registry = RunRegistry()
        await registry.register("wr-4", "trace-4", "conv-4", 1, {"workflowRunId": "wr-4"})
        await registry.cancel("wr-4")
        snapshot = await registry.finish("wr-4", RunStatus.SUCCESS)

        assert snapshot.status == RunStatus.CANCELLED

    asyncio.run(scenario())


def test_paused_registry_metadata_can_be_rehydrated_after_restart() -> None:
    async def scenario() -> None:
        registry = RunRegistry()
        restored = await registry.restore_paused(
            "wr-restored",
            "trace-restored",
            "conv-restored",
            3,
            {"workflowRunId": "wr-restored", "traceId": "trace-restored", "revision": 3},
        )
        plan = await registry.request_resume("wr-restored")

        assert restored.status == RunStatus.PAUSED
        assert restored.pause_requested is True
        assert plan.restart_required is True
        assert plan.snapshot.status == RunStatus.RESUMING

    asyncio.run(scenario())


def test_paused_invocation_can_detach_before_immediate_resume() -> None:
    async def scenario() -> None:
        registry = RunRegistry()
        await registry.register("wr-race", "trace-race", "conv-race", 1, {"workflowRunId": "wr-race"})
        old_task = asyncio.create_task(asyncio.sleep(0))
        await registry.attach_task("wr-race", old_task)
        await registry.mark_paused("wr-race")
        await registry.detach_task("wr-race", old_task)

        plan = await registry.request_resume("wr-race")
        new_task = asyncio.create_task(asyncio.sleep(0))
        attached = await registry.attach_task("wr-race", new_task)

        assert plan.restart_required is True
        assert attached.status == RunStatus.RESUMING
        await old_task
        await new_task

    asyncio.run(scenario())


def test_terminal_success_cannot_be_cancelled() -> None:
    async def scenario() -> None:
        registry = RunRegistry()
        await registry.register("wr-5", "trace-5", "conv-5", 1, {"workflowRunId": "wr-5"})
        await registry.finish("wr-5", RunStatus.SUCCESS)
        try:
            await registry.cancel("wr-5")
        except InvalidRunTransition:
            return
        raise AssertionError("terminal success should reject cancellation")

    asyncio.run(scenario())


def test_partial_success_is_terminal_and_not_reported_as_failure() -> None:
    async def scenario() -> None:
        registry = RunRegistry()
        await registry.register(
            "wr-partial",
            "trace-partial",
            "conv-partial",
            1,
            {"workflowRunId": "wr-partial"},
        )
        snapshot = await registry.finish("wr-partial", RunStatus.PARTIAL_SUCCESS)

        assert snapshot.status == RunStatus.PARTIAL_SUCCESS
        try:
            await registry.cancel("wr-partial")
        except InvalidRunTransition:
            return
        raise AssertionError("partial success should be terminal")

    asyncio.run(scenario())


def test_cancel_before_start_creates_terminal_tombstone() -> None:
    async def scenario() -> None:
        registry = RunRegistry()
        cancelled = await registry.cancel_or_tombstone(
            "wr-before-start", "trace-before-start", "conv-before-start", 2
        )
        registered = await registry.register(
            "wr-before-start",
            "trace-before-start",
            "conv-before-start",
            2,
            {"workflowRunId": "wr-before-start"},
        )

        assert cancelled.status == RunStatus.CANCELLED
        assert registered.status == RunStatus.CANCELLED
        assert registered.task_active is False

    asyncio.run(scenario())


def test_terminal_run_id_cannot_be_rebound_to_another_identity() -> None:
    async def scenario() -> None:
        registry = RunRegistry()
        await registry.cancel_or_tombstone("wr-immutable", "trace-a", "conv-a", 1)
        try:
            await registry.register(
                "wr-immutable",
                "trace-b",
                "conv-b",
                1,
                {"workflowRunId": "wr-immutable"},
            )
        except InvalidRunTransition:
            return
        raise AssertionError("terminal workflow run id must remain identity-bound")

    asyncio.run(scenario())
