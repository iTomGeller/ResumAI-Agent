from __future__ import annotations

import time
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator


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
    # Set by the Java control plane when resuming a PAUSED run: the executor
    # restores shared state / budgets / executed agents from this snapshot and
    # never re-runs completed non-idempotent steps.
    resumeSnapshot: Optional[Dict[str, Any]] = None
    # Present when this run mirrors a legacy resume_task evaluation.
    sourceTaskTraceId: Optional[str] = None
    # Plan-approval mode: pause right after the Coordinator produced the plan
    # so the user can review/edit the agent pipeline before any budget burns.
    planMode: bool = False


class CancelRequest(BaseModel):
    reason: str = "user_cancelled"


class PauseRequest(BaseModel):
    reason: str = "user_paused"


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
    # Empty = eligible for any runType. Explicit list is an allowlist
    # (e.g. low_cost must NOT include full_evaluation).
    supportedRunTypes: List[str] = Field(default_factory=list)
    # Empty requiredArtifacts → use GOAL_ARTIFACTS[runType] defaults.
    requiredArtifacts: List[str] = Field(default_factory=list)
    optionalArtifacts: List[str] = Field(default_factory=list)
    maxAgentCount: int = 6
    maxLlmCalls: int = 12
    # Cost budget axis (CNY, real token pricing); 0 disables the cap.
    maxCostCny: float = 1.0
    # Hard cumulative token ceiling (prompt + completion) enforced before
    # every LLM call; 0 disables the cap.
    maxTotalTokens: int = 120000
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
    parallelSpecialists: bool = True
    timeoutPolicy: TimeoutPolicy = Field(default_factory=TimeoutPolicy)

    def supports_run_type(self, run_type: str) -> bool:
        if not self.supportedRunTypes:
            return True
        return (run_type or "").strip() in self.supportedRunTypes

    @classmethod
    def from_config(cls, policy_id: str, config: Dict[str, Any]) -> "PolicyBundle":
        data = dict(config or {})
        data["policyId"] = policy_id
        return cls.model_validate(data)


class ToolCallRequest(BaseModel):
    """One tool invocation the agent asks the harness to execute."""

    tool: str = ""
    arguments: Dict[str, Any] = Field(default_factory=dict)


class HandoffRequest(BaseModel):
    """First-class agent handoff: transfer a follow-up task to a peer. The
    harness validates dependencies, budget and delegation cycles before the
    target is scheduled."""

    to: str = ""
    reason: str = ""
    task: str = ""


class AgentDecision(BaseModel):
    """Schema of every per-iteration agent reply.

    Validation is the third JSON guarantee layer (after API json_object mode
    and truncation/empty-content checks in the client): a reply that parses
    as JSON but violates this schema is repaired with the exact violation
    fed back to the model.
    """

    thought: str = ""
    toolCalls: List[ToolCallRequest] = Field(default_factory=list)
    output: Optional[Dict[str, Any]] = None
    handoff: Optional[HandoffRequest] = None
    done: bool = False


class SourceRef(BaseModel):
    """Pointer into resume / JD / knowledge / external evidence."""

    sourceType: Literal["RESUME", "JD", "KNOWLEDGE", "EXTERNAL"]
    sourceId: str
    lineStart: Optional[int] = None
    lineEnd: Optional[int] = None
    quote: str
    uri: Optional[str] = None


class ScoreDimension(BaseModel):
    """Evidence-bound scoring dimension (replaces ReportDimension)."""

    name: str
    score: Optional[int] = None
    status: Literal["ASSESSED", "UNASSESSED", "PARTIAL"] = "ASSESSED"
    evidenceCoverage: float = 0.0
    rationale: str = ""
    evidenceRefs: List[SourceRef] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _normalize_legacy_status(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        status = str(data.get("status") or "").strip().upper()
        score = data.get("score")
        if status not in {"ASSESSED", "UNASSESSED", "PARTIAL"}:
            data = dict(data)
            data["status"] = "UNASSESSED" if score is None else "ASSESSED"
        return data

    @model_validator(mode="after")
    def _unassessed_keeps_null_score(self) -> "ScoreDimension":
        # UNASSESSED must keep score=null — never coerce missing evidence to 0.
        if self.status == "UNASSESSED":
            self.score = None
        return self


# Backward-compatible alias used by older imports / schemas.
ReportDimension = ScoreDimension


class CandidateRisk(BaseModel):
    """Candidate-side risk only — never control-plane / PROCESS / DATA noise.

    ``claim`` must describe the candidate (timeline, exaggeration, skill gap),
    never control-plane error codes. PROCESS/DATA issues belong in
    ``StructuredReport.systemWarnings``. Executor rejects risks lacking
    ``evidenceRefs`` or carrying control-plane noise in ``claim``.
    """

    id: str = ""
    # Keep as str so PROCESS/DATA payloads parse here and are redirected to
    # systemWarnings by executor validation (never promoted as candidate risks).
    category: str = "CANDIDATE"
    severity: Literal["HIGH", "MEDIUM", "LOW"] = "MEDIUM"
    confidence: Optional[float] = None
    claim: str
    impact: str = ""
    evidenceRefs: List[SourceRef] = Field(default_factory=list)
    verificationPlan: str = ""


class InterviewProbe(BaseModel):
    """Actionable interview question bound to evidence and answer signals."""

    id: str = ""
    priority: Literal["HIGH", "MEDIUM", "LOW"] = "MEDIUM"
    question: str
    objective: str = ""
    triggeredBy: str = ""
    evidenceRefs: List[SourceRef] = Field(default_factory=list)
    goodSignals: List[str] = Field(default_factory=list)
    redFlags: List[str] = Field(default_factory=list)
    followUps: List[str] = Field(default_factory=list)
    scoreRubric: str = ""

    @field_validator("priority", mode="before")
    @classmethod
    def _normalize_priority(cls, value: Any) -> Any:
        text = str(value or "MEDIUM").strip().upper()
        return text if text in {"HIGH", "MEDIUM", "LOW"} else "MEDIUM"


class SystemWarning(BaseModel):
    """Process/data/control-plane issues — never mixed into candidate risks."""

    code: str
    stage: str = ""
    retryable: bool = False
    message: str = ""


class StructuredReport(BaseModel):
    """Contract for ReportAgent structured output (Markdown is rendered offline)."""

    recommendation: Literal[
        "HIRE", "INTERVIEW_RECOMMEND", "NEED_MANUAL_REVIEW", "NOT_RECOMMEND"
    ]
    dimensions: List[ScoreDimension] = Field(default_factory=list)
    strengths: List[str] = Field(default_factory=list)
    risks: List[CandidateRisk] = Field(default_factory=list)
    interviewQuestions: List[InterviewProbe] = Field(default_factory=list)
    interviewProbes: List[InterviewProbe] = Field(default_factory=list)
    systemWarnings: List[SystemWarning] = Field(default_factory=list)
    dataQuality: Literal["SUFFICIENT", "PARTIAL", "INSUFFICIENT"] = "SUFFICIENT"
    missingEvidence: List[str] = Field(default_factory=list)
    overallScore: Optional[int] = None
    summary: str = ""

    @field_validator("recommendation", mode="before")
    @classmethod
    def _normalize_recommendation(cls, value: Any) -> Any:
        text = str(value or "").strip().upper()
        legacy = {
            "STRONG_RECOMMEND": "HIRE",
            "RECOMMEND": "INTERVIEW_RECOMMEND",
            "MANUAL_REVIEW": "NEED_MANUAL_REVIEW",
            "REVIEW": "NEED_MANUAL_REVIEW",
        }
        return legacy.get(text, text)

    @field_validator("dataQuality", mode="before")
    @classmethod
    def _normalize_data_quality(cls, value: Any) -> Any:
        text = str(value or "SUFFICIENT").strip().upper()
        return text if text in {"SUFFICIENT", "PARTIAL", "INSUFFICIENT"} else "SUFFICIENT"

    @field_validator("risks", mode="before")
    @classmethod
    def _coerce_legacy_risks(cls, value: Any) -> Any:
        if not isinstance(value, list):
            return value
        coerced: List[Any] = []
        for i, item in enumerate(value):
            if isinstance(item, str):
                text = item.strip()
                if text:
                    coerced.append({
                        "id": f"legacy-risk-{i + 1}",
                        "category": "CANDIDATE",
                        "severity": "MEDIUM",
                        "claim": text,
                        "evidenceRefs": [],
                    })
            else:
                coerced.append(item)
        return coerced

    @field_validator("interviewQuestions", "interviewProbes", mode="before")
    @classmethod
    def _coerce_legacy_probes(cls, value: Any) -> Any:
        if not isinstance(value, list):
            return value
        coerced: List[Any] = []
        for i, item in enumerate(value):
            if isinstance(item, str):
                text = item.strip()
                if text:
                    coerced.append({
                        "id": f"legacy-probe-{i + 1}",
                        "priority": "MEDIUM",
                        "question": text,
                        "evidenceRefs": [],
                        "goodSignals": [],
                    })
            else:
                coerced.append(item)
        return coerced

    @model_validator(mode="after")
    def _merge_probe_aliases(self) -> "StructuredReport":
        # Prefer explicit interviewProbes; mirror into interviewQuestions for
        # consumers that still read the legacy field name.
        if self.interviewProbes and not self.interviewQuestions:
            self.interviewQuestions = list(self.interviewProbes)
        elif self.interviewQuestions and not self.interviewProbes:
            self.interviewProbes = list(self.interviewQuestions)
        return self


class AgentOutput(BaseModel):
    """Structured contribution one agent writes to the shared blackboard.

    Prefer ``artifacts`` for structured writes. ``claims`` carry conclusions /
    rationales only — do not smuggle artifact payloads via claim.section/value.
    """

    agentId: str
    type: str
    claims: List[Dict[str, Any]] = Field(default_factory=list)
    artifacts: Dict[str, Any] = Field(default_factory=dict)
    evidence: List[Dict[str, Any]] = Field(default_factory=list)
    confidence: float = 0.5
    source: str = "llm"
    dependencies: List[str] = Field(default_factory=list)
    requestedNextAction: Optional[str] = None
    summary: str = ""
    createdAt: float = Field(default_factory=time.time)


class RunBudget(BaseModel):
    """Mutable consumption counters checked by the executor and loop guard.

    Budget axes: steps (agents/iterations), tool calls, tokens, wall clock
    and real cost (CNY) — checked before every LLM/tool step, never only at
    the end of the run.
    """

    llm_calls: int = 0
    tool_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    prompt_cache_hit_tokens: int = 0
    cost_cny: float = 0.0
    started_at: float = Field(default_factory=time.monotonic)

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def elapsed_seconds(self) -> float:
        return time.monotonic() - self.started_at

    def restore(self, counters: Dict[str, Any]) -> None:
        self.llm_calls = int(counters.get("llmCalls", 0))
        self.tool_calls = int(counters.get("toolCalls", 0))
        self.prompt_tokens = int(counters.get("promptTokens", 0))
        self.completion_tokens = int(counters.get("completionTokens", 0))
        self.cost_cny = float(counters.get("costCny", 0.0))

    def snapshot(self) -> Dict[str, Any]:
        return {
            "llmCalls": self.llm_calls,
            "toolCalls": self.tool_calls,
            "promptTokens": self.prompt_tokens,
            "completionTokens": self.completion_tokens,
            "costCny": round(self.cost_cny, 6),
        }


class BudgetExceeded(RuntimeError):
    def __init__(self, kind: str, detail: str = "") -> None:
        super().__init__(f"budget exceeded: {kind} {detail}".strip())
        self.kind = kind


class RunCancelled(RuntimeError):
    pass


class RunPaused(RuntimeError):
    """Raised inside the executor at a safe boundary to unwind into PAUSED."""

    def __init__(self, snapshot: Dict[str, Any]) -> None:
        super().__init__("run paused at safe boundary")
        self.snapshot = snapshot
