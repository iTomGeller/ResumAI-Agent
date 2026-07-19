from __future__ import annotations

from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class WorkflowRunRequest(BaseModel):
    traceId: str
    resumeText: str
    jobCategory: Optional[str] = None
    jobDescription: Optional[str] = None
    executionMode: Optional[str] = None
    conversationId: Optional[str] = None
    workflowRunId: Optional[str] = None
    revision: int = Field(default=1, ge=1)
    baseTraceId: Optional[str] = None
    baseWorkflowRunId: Optional[str] = None
    evaluationBrief: Dict[str, Any] = Field(default_factory=dict)
    affectedNodes: List[str] = Field(default_factory=list)
    invalidatedNodes: List[str] = Field(default_factory=list)


class WorkflowRunAccepted(BaseModel):
    workflowRunId: str
    traceId: str
    status: str
    conversationId: Optional[str] = None
    revision: int = 1


class RunControlAction(str, Enum):
    PAUSE = "PAUSE"
    RESUME = "RESUME"
    CANCEL = "CANCEL"


class RunControlRequest(BaseModel):
    action: RunControlAction
    traceId: Optional[str] = None
    conversationId: Optional[str] = None
    revision: int = Field(default=1, ge=1)


class RunControlResponse(BaseModel):
    workflowRunId: str
    traceId: str
    conversationId: str
    revision: int
    status: str
    checkpointed: bool = False


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


class WorkflowState(BaseModel):
    traceId: str = ""
    resumeText: str = ""
    jobCategory: Optional[str] = None
    jobDescription: Optional[str] = None
    executionMode: Optional[str] = None
    workflowRunId: str = ""
    conversationId: str = ""
    revision: int = 1
    baseTraceId: Optional[str] = None
    baseWorkflowRunId: Optional[str] = None
    evaluationBrief: Dict[str, Any] = Field(default_factory=dict)
    affectedNodes: List[str] = Field(default_factory=list)
    invalidatedNodes: List[str] = Field(default_factory=list)
    revisionPlan: Dict[str, Any] = Field(default_factory=dict)
    reusedNodes: List[str] = Field(default_factory=list)
    intentResult: Optional[str] = None
    parseResult: Optional[str] = None
    jdResult: Optional[str] = None
    techResult: Optional[str] = None
    projectResult: Optional[str] = None
    riskResult: Optional[str] = None
    fusionResult: Optional[str] = None
    finalReport: Optional[str] = None
    harnessContext: Dict[str, Any] = Field(default_factory=dict)
    harnessPlan: Dict[str, Any] = Field(default_factory=dict)
    overallScore: int = 0
    recommendation: Optional[str] = None
    strengths: List[str] = Field(default_factory=list)
    risks: List[str] = Field(default_factory=list)
    interviewQuestions: List[str] = Field(default_factory=list)
    degradedReasons: List[str] = Field(default_factory=list)
    completedNodes: List[str] = Field(default_factory=list)
    failedNode: Optional[str] = None
    toolHealth: Dict[str, Any] = Field(default_factory=dict)


class ToolCallRecord(BaseModel):
    toolCallId: str
    name: str
    category: str = "tool"
    origin: str = "local"
    family: str = "tool"
    protocol: Optional[str] = None
    server: Optional[str] = None
    operation: Optional[str] = None
    arguments: str = ""
    result: str = ""
    startedAt: Optional[str] = None
    endedAt: Optional[str] = None
    durationMs: int = 0
    status: str = "SUCCESS"
    inputHash: Optional[str] = None
    dedupedCount: int = 0
    substeps: List[Dict[str, Any]] = Field(default_factory=list)
    retrieval: Optional[Dict[str, Any]] = None


class TraceEvent(BaseModel):
    eventId: str
    traceId: str
    nodeId: str
    agentName: str
    phase: int
    attempt: int = 1
    kind: str
    roundIndex: int = 0
    parentEventId: Optional[str] = None
    status: str = "SUCCESS"
    startedAt: Optional[str] = None
    endedAt: Optional[str] = None
    durationMs: int = 0
    modelName: Optional[str] = None
    inputMessages: Optional[List[Dict[str, Any]]] = None
    outputMessage: Optional[Dict[str, Any]] = None
    inputPreview: Optional[str] = None
    outputPreview: Optional[str] = None
    tokenUsage: Optional[Dict[str, Any]] = None
    toolCalls: List[ToolCallRecord] = Field(default_factory=list)
    langfuseTraceId: Optional[str] = None
    langfuseObservationId: Optional[str] = None
    callKind: Optional[str] = None
    callName: Optional[str] = None
    roundRole: Optional[str] = None
    parentRoundId: Optional[str] = None
    decisionText: Optional[str] = None
    hasToolCalls: bool = False
    finalOutput: Optional[str] = None
    observationKind: Optional[str] = None
    toolOrigin: Optional[str] = None
    toolFamily: Optional[str] = None
    substeps: List[Dict[str, Any]] = Field(default_factory=list)
    retrieval: Optional[Dict[str, Any]] = None
    workflowRunId: Optional[str] = None
    conversationId: Optional[str] = None
    revision: Optional[int] = None
    reuseSourceWorkflowRunId: Optional[str] = None
    reuseSourceTraceId: Optional[str] = None


class WorkflowResultPayload(BaseModel):
    traceId: str
    workflowRunId: str
    status: str
    conversationId: Optional[str] = None
    revision: int = 1
    summary: Optional[str] = None
    overallScore: int = 0
    recommendation: Optional[str] = None
    strengths: List[str] = Field(default_factory=list)
    risks: List[str] = Field(default_factory=list)
    interviewQuestions: List[str] = Field(default_factory=list)
    durationMs: int = 0
    tokenCost: int = 0
    failedNode: Optional[str] = None
    errorMessage: Optional[str] = None
