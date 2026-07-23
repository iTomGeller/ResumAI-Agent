from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.conversation.models import ConversationReplyRequest, CopilotAnswer
from app.conversation.responder import generate_copilot_answer
from app.conversation.routing import resolve_turn_with_model
from app.models import ConversationTurnRequest, ConversationTurnResponse

router = APIRouter(tags=["conversation"])


@router.post("/conversation/reply", response_model=CopilotAnswer)
async def conversation_reply(request: ConversationReplyRequest) -> CopilotAnswer:
    """Short CopilotAnswer path — never StructuredReport / ReportAgent."""
    content = (request.content or "").strip()
    if not content:
        raise HTTPException(status_code=400, detail="content required")
    if not (request.turnId or "").strip():
        raise HTTPException(status_code=400, detail="turnId required")
    return await generate_copilot_answer(request)


@router.post("/conversation/turns/resolve", response_model=ConversationTurnResponse)
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
