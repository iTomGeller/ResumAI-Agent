from __future__ import annotations

"""Short Copilot answer protocol — never a StructuredReport / ReportAgent payload."""

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field


class SourceRef(BaseModel):
    sourceType: Literal["RESUME", "JD", "KNOWLEDGE", "EXTERNAL", "SESSION"] = "SESSION"
    sourceId: str = ""
    lineStart: Optional[int] = None
    lineEnd: Optional[int] = None
    quote: str = ""
    uri: Optional[str] = None


class CopilotAction(BaseModel):
    type: str
    label: str
    payload: Dict[str, Any] = Field(default_factory=dict)


class CopilotAnswer(BaseModel):
    answer: str
    citations: List[SourceRef] = Field(default_factory=list)
    actions: List[CopilotAction] = Field(default_factory=list)
    suggestions: List[str] = Field(default_factory=list)
    turnId: Optional[str] = None


class ContextRef(BaseModel):
    type: str
    id: str
    revision: Optional[int] = None
    version: Optional[int] = None


class ConversationReplyRequest(BaseModel):
    turnId: str
    content: str
    conversationId: Optional[str] = None
    disposition: Optional[str] = None
    intent: Optional[str] = None
    allowTools: bool = False
    contextRefs: List[ContextRef] = Field(default_factory=list)
    contextSnapshot: Dict[str, Any] = Field(default_factory=dict)
