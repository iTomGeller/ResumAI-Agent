from __future__ import annotations

import asyncio
import hmac
import logging
import uuid

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from app.checkpoint import checkpointer_available, close_checkpointer
from app.config import settings
from app.conversation import resolve_turn_with_model
from app.events import emit_result
from app.graph import run_workflow
from app.models import (
    ConversationTurnRequest,
    ConversationTurnResponse,
    RunControlAction,
    RunControlRequest,
    RunControlResponse,
    WorkflowResultPayload,
    WorkflowRunAccepted,
    WorkflowRunRequest,
    WorkflowState,
)
from app.run_control import (
    InvalidRunTransition,
    RunNotFoundError,
    RunSnapshot,
    RunStatus,
    TERMINAL_STATUSES,
    default_run_registry,
)
from app.runtime.service import agent_run_registry, router as agent_runtime_router

logger = logging.getLogger(__name__)

app = FastAPI(title="ResumAI LangGraph Workflow", version="1.2.0")
app.include_router(agent_runtime_router)


@app.middleware("http")
async def require_internal_token(request: Request, call_next):
    """Protect the Docker-internal control plane with the configured shared token."""

    if request.url.path in {"/health", "/ready"}:
        return await call_next(request)
    expected = (settings.workflow_internal_token or "").strip()
    # Local tests remain dependency-light.  Production compose refuses to start
    # without an explicit token, so the default is never accepted on ECS.
    if expected and expected != "change-me":
        supplied = request.headers.get("X-Internal-Token", "")
        if not hmac.compare_digest(supplied, expected):
            return JSONResponse(status_code=401, content={"detail": "invalid internal token"})
    return await call_next(request)


@app.get("/health")
async def health() -> dict:
    snapshots = await default_run_registry.list_snapshots()
    active = sum(1 for item in snapshots.values() if item.status not in TERMINAL_STATUSES)
    return {
        "status": "UP",
        "service": "ai-resume-workflow",
        "activeRuns": active,
        "activeAgentRuns": await agent_run_registry.active_count(),
    }


@app.get("/ready")
async def ready() -> dict:
    """Production readiness: pause/resume is not advertised without a saver."""

    checkpoint_ready = await checkpointer_available()
    if not checkpoint_ready:
        raise HTTPException(
            status_code=503,
            detail={
                "status": "NOT_READY",
                "service": "ai-resume-workflow",
                "checkpoint": "unavailable",
            },
        )
    return {
        "status": "READY",
        "service": "ai-resume-workflow",
        "checkpoint": "available",
    }


@app.post("/conversation/turns/resolve", response_model=ConversationTurnResponse)
async def resolve_conversation_turn(request: ConversationTurnRequest) -> ConversationTurnResponse:
    message = (request.message or request.content or "").strip()
    if not message:
        raise HTTPException(status_code=400, detail="message or content required")
    context = dict(request.context)
    context.setdefault("runStatus", request.runStatus)
    decision = await resolve_turn_with_model(
        message,
        run_status=request.runStatus,
        revision=request.revision,
        context=context,
    )
    return ConversationTurnResponse(
        intent=decision.intent.value,
        confidence=decision.confidence,
        affectsEvaluation=decision.affects_evaluation,
        answerThenResume=decision.answer_then_resume,
        affectedNodes=decision.affected_nodes,
        assistantReply=decision.assistant_reply,
        assistantMessage=decision.assistant_reply,
        controlAction=decision.control_action,
        evaluationPatch=decision.evaluation_patch,
        requiresConfirmation=decision.requires_confirmation,
        reason=decision.reason,
        conversationId=request.conversationId,
        workflowRunId=request.workflowRunId,
        traceId=request.traceId,
        revision=request.revision,
    )


@app.post("/workflow/runs", response_model=WorkflowRunAccepted)
async def start_workflow(request: WorkflowRunRequest) -> WorkflowRunAccepted:
    return await _start_workflow(request)


@app.post("/execute", response_model=WorkflowRunAccepted)
async def execute_compat(request: WorkflowRunRequest) -> WorkflowRunAccepted:
    """Backward-compatible alias for callers that still use ``/execute``."""

    return await _start_workflow(request)


async def _start_workflow(request: WorkflowRunRequest) -> WorkflowRunAccepted:
    if not request.traceId or not request.resumeText:
        raise HTTPException(status_code=400, detail="traceId and resumeText required")
    workflow_run_id = request.workflowRunId or f"wr-{uuid.uuid4()}"
    conversation_id = request.conversationId or request.traceId
    affected_nodes = list(dict.fromkeys(request.affectedNodes or request.invalidatedNodes))
    initial = WorkflowState(
        traceId=request.traceId,
        workflowRunId=workflow_run_id,
        conversationId=conversation_id,
        revision=request.revision,
        baseTraceId=request.baseTraceId,
        baseWorkflowRunId=request.baseWorkflowRunId,
        resumeText=request.resumeText,
        jobCategory=request.jobCategory,
        jobDescription=request.jobDescription,
        executionMode=request.executionMode,
        evaluationBrief=request.evaluationBrief,
        affectedNodes=affected_nodes,
        invalidatedNodes=request.invalidatedNodes or affected_nodes,
    )
    try:
        registered = await default_run_registry.register(
            workflow_run_id,
            request.traceId,
            conversation_id,
            request.revision,
            initial.model_dump(),
        )
    except InvalidRunTransition as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    if registered.status in {
        RunStatus.SUCCESS,
        RunStatus.PARTIAL_SUCCESS,
        RunStatus.FAILED,
        RunStatus.CANCELLED,
    }:
        # Idempotent duplicate start, including a cancellation tombstone that
        # arrived before this request.  Never resurrect a terminal run id.
        return WorkflowRunAccepted(
            workflowRunId=workflow_run_id,
            traceId=request.traceId,
            conversationId=conversation_id,
            revision=request.revision,
            status=registered.status.value,
        )

    task = asyncio.create_task(_execute_background(initial, resume=False))
    try:
        await default_run_registry.attach_task(workflow_run_id, task)
    except Exception:
        task.cancel()
        raise
    return WorkflowRunAccepted(
        workflowRunId=workflow_run_id,
        traceId=request.traceId,
        conversationId=conversation_id,
        revision=request.revision,
        status="ACCEPTED",
    )


@app.get("/workflow/runs/{workflow_run_id}", response_model=RunControlResponse)
async def workflow_run_status(workflow_run_id: str) -> RunControlResponse:
    snapshot = await default_run_registry.get(workflow_run_id)
    if snapshot is None:
        raise HTTPException(status_code=404, detail="workflow run not found")
    return _control_response(snapshot, checkpointed=snapshot.status == RunStatus.PAUSED)


@app.post("/workflow/runs/{workflow_run_id}/control", response_model=RunControlResponse)
async def control_workflow_run(
    workflow_run_id: str,
    request: RunControlRequest,
) -> RunControlResponse:
    snapshot = await default_run_registry.get(workflow_run_id)
    if snapshot is None:
        if request.action == RunControlAction.CANCEL and request.traceId:
            conversation_id = request.conversationId or request.traceId
            try:
                cancelled = await default_run_registry.cancel_or_tombstone(
                    workflow_run_id,
                    request.traceId,
                    conversation_id,
                    request.revision,
                )
            except InvalidRunTransition as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            await emit_result(_status_result(cancelled, "CANCELLED", "Workflow cancelled before start"))
            return _control_response(cancelled)
        if request.action != RunControlAction.RESUME or not request.traceId:
            raise HTTPException(status_code=404, detail="workflow run not found")
        if not await checkpointer_available():
            raise HTTPException(
                status_code=409,
                detail="durable LangGraph checkpoint unavailable; cannot restore paused run",
            )
        conversation_id = request.conversationId or request.traceId
        recovered = WorkflowState(
            traceId=request.traceId,
            workflowRunId=workflow_run_id,
            conversationId=conversation_id,
            revision=request.revision,
        )
        snapshot = await default_run_registry.restore_paused(
            workflow_run_id,
            request.traceId,
            conversation_id,
            request.revision,
            recovered.model_dump(),
        )
    try:
        if request.action == RunControlAction.CANCEL:
            cancelled = await default_run_registry.cancel(workflow_run_id)
            if not cancelled.task_active:
                await emit_result(_status_result(cancelled, "CANCELLED", "Workflow cancelled by user"))
            return _control_response(cancelled)

        if request.action == RunControlAction.PAUSE:
            if snapshot.status not in {RunStatus.PAUSING, RunStatus.PAUSED}:
                if not await checkpointer_available():
                    raise HTTPException(
                        status_code=409,
                        detail="durable LangGraph checkpoint unavailable; refusing unsafe pause",
                    )
            pausing = await default_run_registry.request_pause(workflow_run_id)
            return _control_response(pausing, checkpointed=pausing.status == RunStatus.PAUSED)

        if snapshot.status == RunStatus.PAUSED and not await checkpointer_available():
            raise HTTPException(
                status_code=409,
                detail="durable LangGraph checkpoint unavailable; cannot resume safely",
            )
        plan = await default_run_registry.request_resume(workflow_run_id)
        if plan.restart_required:
            state_data = await default_run_registry.initial_state(workflow_run_id)
            initial = WorkflowState(**state_data)
            task = asyncio.create_task(_execute_background(initial, resume=True))
            await default_run_registry.attach_task(workflow_run_id, task)
        return _control_response(plan.snapshot, checkpointed=plan.restart_required)
    except RunNotFoundError as exc:
        raise HTTPException(status_code=404, detail="workflow run not found") from exc
    except InvalidRunTransition as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


async def _execute_background(initial: WorkflowState, *, resume: bool) -> None:
    current_task = asyncio.current_task()
    run_id = initial.workflowRunId
    try:
        result = await run_workflow(initial, resume=resume)
        live_snapshot = await default_run_registry.get(run_id)
        if live_snapshot is not None and live_snapshot.status == RunStatus.CANCELLED:
            # A user cancellation wins even if the provider completed in the
            # same event-loop tick and produced a late success/pause payload.
            result = _status_result(live_snapshot, "CANCELLED", "Workflow cancelled by user")
        if result.status == "PAUSED":
            await default_run_registry.mark_paused(run_id)
            # Release the completed invocation before publishing PAUSED.  The
            # Java callback can trigger RESUME immediately, so leaving this
            # task attached until after the network call creates a race where
            # the resumed task cannot be attached to the same run.
            if current_task is not None:
                await default_run_registry.detach_task(run_id, current_task)
                current_task = None
        elif result.status == "CANCELLED":
            await default_run_registry.finish(run_id, RunStatus.CANCELLED)
        elif result.status in {"SUCCESS", "PARTIAL_SUCCESS"}:
            await default_run_registry.finish(run_id, RunStatus(result.status))
        else:
            await default_run_registry.finish(run_id, RunStatus.FAILED)
        await emit_result(result)
    except asyncio.CancelledError:
        logger.info("workflow cancelled run=%s trace=%s", run_id, initial.traceId)
        try:
            await default_run_registry.finish(run_id, RunStatus.CANCELLED)
        except RunNotFoundError:
            pass
        await emit_result(
            WorkflowResultPayload(
                traceId=initial.traceId,
                workflowRunId=run_id,
                conversationId=initial.conversationId,
                revision=initial.revision,
                status="CANCELLED",
                summary="Workflow cancelled by user",
            )
        )
    except Exception as exc:
        logger.exception("background workflow error run=%s: %s", run_id, exc)
        try:
            snapshot = await default_run_registry.get(run_id)
            if snapshot is None or snapshot.status != RunStatus.CANCELLED:
                await default_run_registry.finish(run_id, RunStatus.FAILED)
        except RunNotFoundError:
            pass
        await emit_result(
            WorkflowResultPayload(
                traceId=initial.traceId,
                workflowRunId=run_id,
                conversationId=initial.conversationId,
                revision=initial.revision,
                status="FAILED",
                summary=str(exc),
                errorMessage=str(exc),
            )
        )
    finally:
        if current_task is not None:
            await default_run_registry.detach_task(run_id, current_task)


def _control_response(snapshot: RunSnapshot, checkpointed: bool = False) -> RunControlResponse:
    return RunControlResponse(
        workflowRunId=snapshot.workflow_run_id,
        traceId=snapshot.trace_id,
        conversationId=snapshot.conversation_id,
        revision=snapshot.revision,
        status=snapshot.status.value,
        checkpointed=checkpointed,
    )


def _status_result(snapshot: RunSnapshot, status: str, summary: str) -> WorkflowResultPayload:
    return WorkflowResultPayload(
        traceId=snapshot.trace_id,
        workflowRunId=snapshot.workflow_run_id,
        conversationId=snapshot.conversation_id,
        revision=snapshot.revision,
        status=status,
        summary=summary,
    )


@app.on_event("shutdown")
async def shutdown() -> None:
    snapshots = await default_run_registry.list_snapshots()
    for run_id, snapshot in snapshots.items():
        if snapshot.task_active:
            try:
                await default_run_registry.cancel(run_id)
            except (RunNotFoundError, InvalidRunTransition):
                pass
    await agent_run_registry.cancel_all()
    await close_checkpointer()
