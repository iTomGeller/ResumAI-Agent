from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class AgentRunRequest(BaseModel):
    runId: str
    conversationId: str
    userId: str = "demo-hr"
    traceId: str = ""
    revision: int = 1
    runType: str = "full_evaluation"
    userMessage: str = ""
    resumeText: Optional[str] = None
    jobDescription: Optional[str] = None
    jobCategory: Optional[str] = None
    conversationSummary: Optional[str] = None
    currentGoal: Optional[str] = None
    policyId: str = "balanced"
    policyConfig: Dict[str, Any] = Field(default_factory=dict)
    recentMessages: List[Dict[str, Any]] = Field(default_factory=list)


class CancelRequest(BaseModel):
    reason: str = "user_cancelled"


class ToolBudget(BaseModel):
    maxToolCallsPerRun: int = 20
    maxToolCallsPerAgent: int = 5


class ContextBudget(BaseModel):
    modelWindow: int = 65536
    systemBudget: int = 2200
    policyBudget: int = 320
    skillBudget: int = 1200
    recentMessageBudget: int = 2600
    memoryBudget: int = 1500
    toolResultBudget: int = 3600
    reservedOutputBudget: int = 2048
    compactAtRatio: float = 0.75


class MemoryRetrieval(BaseModel):
    topK: int = 5
    minConfidence: float = 0.35


class EvidenceVerification(BaseModel):
    enabled: bool = True
    strict: bool = False
    minSupportRatio: float = 0.5


class TimeoutPolicy(BaseModel):
    runTimeoutSeconds: int = 900
    llmTimeoutSeconds: int = 120
    toolTimeoutSeconds: int = 30
    sandboxTimeoutSeconds: int = 90


class PolicyBundle(BaseModel):
    """Parsed policy configuration controlling the outer agent loop."""

    policyId: str = "balanced"
    agentOrder: List[str] = Field(default_factory=lambda: [
        "JDAnalysisAgent", "TechAgent", "ProjectAgent",
        "RiskAgent", "EvidenceAgent", "ReportAgent"])
    maxAgentCount: int = 6
    maxLlmCalls: int = 12
    maxIterationsPerAgent: int = 2
    jobFocus: Optional[str] = None
    skillOverrides: Dict[str, str] = Field(default_factory=dict)
    promptVersions: Dict[str, str] = Field(default_factory=dict)
    skillVersions: Dict[str, str] = Field(default_factory=dict)
    toolBudget: ToolBudget = Field(default_factory=ToolBudget)
    contextBudget: ContextBudget = Field(default_factory=ContextBudget)
    memoryRetrieval: MemoryRetrieval = Field(default_factory=MemoryRetrieval)
    evidenceVerification: EvidenceVerification = Field(default_factory=EvidenceVerification)
    rewriteRounds: int = 1
    timeoutPolicy: TimeoutPolicy = Field(default_factory=TimeoutPolicy)

    @classmethod
    def from_config(cls, policy_id: str, config: Dict[str, Any]) -> "PolicyBundle":
        data = dict(config or {})
        data["policyId"] = policy_id
        return cls.model_validate(data)


class AgentOutput(BaseModel):
    """Structured contribution one agent writes to the shared blackboard."""

    agentId: str
    type: str
    claims: List[Dict[str, Any]] = Field(default_factory=list)
    evidence: List[Dict[str, Any]] = Field(default_factory=list)
    confidence: float = 0.5
    source: str = "llm"
    dependencies: List[str] = Field(default_factory=list)
    requestedNextAction: Optional[str] = None
    summary: str = ""
    createdAt: float = Field(default_factory=time.time)


class RunBudget(BaseModel):
    """Mutable consumption counters checked by the executor and loop guard."""

    llm_calls: int = 0
    tool_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    started_at: float = Field(default_factory=time.monotonic)

    def elapsed_seconds(self) -> float:
        return time.monotonic() - self.started_at


class BudgetExceeded(RuntimeError):
    def __init__(self, kind: str, detail: str = "") -> None:
        super().__init__(f"budget exceeded: {kind} {detail}".strip())
        self.kind = kind


class RunCancelled(RuntimeError):
    pass
