"""Shared DTOs for the workflow service.

Only the conversation-turn resolution contract lives here; run execution
models belong to app.runtime.models. All legacy graph-runtime DTOs were
removed together with that runtime.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class ConversationTurnRequest(BaseModel):
    conversationId: Optional[str] = None
    workflowRunId: Optional[str] = None
    traceId: Optional[str] = None
    revision: int = Field(default=1, ge=1)
    message: Optional[str] = None
    content: Optional[str] = None
    runStatus: str = "RUNNING"
    context: Dict[str, Any] = Field(default_factory=dict)


class ConversationTurnResponse(BaseModel):
    intent: str
    confidence: float
    affectsEvaluation: bool
    answerThenResume: bool
    affectedNodes: List[str] = Field(default_factory=list)
    assistantReply: str
    assistantMessage: Optional[str] = None
    controlAction: Optional[str] = None
    evaluationPatch: Dict[str, Any] = Field(default_factory=dict)
    requiresConfirmation: bool = False
    reason: str = ""
    conversationId: Optional[str] = None
    workflowRunId: Optional[str] = None
    traceId: Optional[str] = None
    revision: int = 1
