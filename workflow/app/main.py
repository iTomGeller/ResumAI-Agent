from __future__ import annotations

import hmac
import logging

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from app.config import settings
from app.conversation import resolve_turn_with_model
from app.models import ConversationTurnRequest, ConversationTurnResponse
from app.runtime.service import agent_run_registry, router as agent_runtime_router

logger = logging.getLogger(__name__)

app = FastAPI(title="ResumAI Agent Runtime", version="2.0.0")
app.include_router(agent_runtime_router)


@app.on_event("startup")
async def startup() -> None:
    try:
        from app.runtime.otel_tracing import init_otel
        init_otel()
    except Exception as exc:  # noqa: BLE001
        logger.info("otel init skipped: %s", exc)


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
    return {
        "status": "UP",
        "service": "ai-resume-workflow",
        "activeAgentRuns": await agent_run_registry.active_count(),
    }


@app.get("/ready")
async def ready() -> dict:
    """Java owns durable state; this process is ready once it can execute.
    Pause/resume snapshots travel through the Java control plane, so no
    local checkpoint store is required (or advertised)."""
    return {
        "status": "READY",
        "service": "ai-resume-workflow",
        "runtime": "agent-runs",
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


@app.on_event("startup")
async def startup() -> None:
    """Warm skills + MCP health probe so the first run sees a real catalog."""
    try:
        from app.runtime.skills import default_skill_manager
        count = default_skill_manager.reload()
        logger.info("skills ready: %d from %s", count, default_skill_manager.root)
    except Exception as exc:  # noqa: BLE001
        logger.warning("skills reload failed: %s", exc)
    try:
        from app.runtime.mcp_registry import get_mcp_registry
        registry = await get_mcp_registry(probe=True)
        snap = registry.status_snapshot()
        logger.info("MCP health: %s tools=%s",
                    {k: v.get("status") for k, v in (snap.get("servers") or {}).items()},
                    len(snap.get("availableTools") or []))
    except Exception as exc:  # noqa: BLE001
        logger.warning("MCP probe at startup failed (catalog empty until retry): %s", exc)


@app.on_event("shutdown")
async def shutdown() -> None:
    await agent_run_registry.cancel_all()
