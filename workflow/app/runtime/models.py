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
    # Cross-revision reuse is deliberately separate from pause/resume. A new
    # revision imports only reusable artifacts from the previous successful
    # run; it never inherits executed-agent, budget, or tool-call state.
    previousSnapshot: Optional[Dict[str, Any]] = None
    previousArtifacts: Dict[str, Any] = Field(default_factory=dict)
    # Logical artifact names (snake_case) and canonical state keys (camelCase)
    # are both accepted. The executor expands downstream dependencies before
    # planning, so stale derived results cannot survive a changed input/focus.
    invalidatedArtifacts: List[str] = Field(default_factory=list)
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
    maxToolCallsPerAgent: int = 10


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
    topK: int = 8
    minConfidence: float = 0.12


class EvidenceVerification(BaseModel):
    enabled: bool = True
    strict: bool = False
    minSupportRatio: float = 0.5


class TimeoutPolicy(BaseModel):
    runTimeoutSeconds: int = 900
    llmTimeoutSeconds: int = 120
    toolTimeoutSeconds: int = 30


_BALANCED_MIN_LLM_CALLS = 17
_BALANCED_MAX_LLM_CALLS = 18
_BALANCED_TERMINAL_LLM_RESERVE = 3


class PolicyBundle(BaseModel):
    """Parsed policy configuration controlling the outer agent loop."""

    policyId: str = "balanced"
    agentOrder: List[str] = Field(default_factory=lambda: [
        "TechAgent", "ProjectAgent",
        "RiskAgent", "EvidenceAgent", "ReportAgent"])
    # Empty = eligible for any runType. Explicit list is an allowlist
    # (e.g. low_cost must NOT include full_evaluation).
    supportedRunTypes: List[str] = Field(default_factory=list)
    # Empty requiredArtifacts → use GOAL_ARTIFACTS[runType] defaults.
    requiredArtifacts: List[str] = Field(default_factory=list)
    optionalArtifacts: List[str] = Field(default_factory=list)
    maxAgentCount: int = 6
    maxLlmCalls: int = 17
    # Provider-call reservations live inside maxLlmCalls; they are not extra
    # budget. Control-plane calls have a hard ceiling, while the terminal
    # reserve cannot be consumed by specialists.
    controlPlaneLlmReserve: int = 4
    terminalLlmReserve: int = 3
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
        if str(policy_id or "").strip() == "balanced":
            # Existing installations legitimately retain the V6 seed row,
            # whose maxLlmCalls=12 predates the seven-agent runtime and its
            # control/terminal reservations.  Normalize that legacy DB value
            # at the runtime boundary instead of mutating a mounted database.
            #
            # This is a bounded compatibility contract, not budget inflation:
            # balanced may use 17..18 calls (deep_analysis already uses 18),
            # and exactly three are protected for terminal finalization/repair.
            requested = int(data.get("maxLlmCalls", _BALANCED_MIN_LLM_CALLS))
            data["maxLlmCalls"] = min(
                _BALANCED_MAX_LLM_CALLS,
                max(_BALANCED_MIN_LLM_CALLS, requested))
            data["terminalLlmReserve"] = _BALANCED_TERMINAL_LLM_RESERVE
        return cls.model_validate(data)


class ToolCallRequest(BaseModel):
    """One tool invocation the agent asks the harness to execute."""

    tool: str = ""
    arguments: Dict[str, Any] = Field(default_factory=dict)


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
    source: str = "llm"
    dependencies: List[str] = Field(default_factory=list)
    summary: str = ""
    createdAt: float = Field(default_factory=time.time)


class RunBudget(BaseModel):
    """Mutable consumption counters checked by the executor and loop guard.

    Budget axes: steps (agents/iterations), tool calls, tokens, wall clock
    and real cost (CNY) — checked before every LLM/tool step, never only at
    the end of the run.
    """

    llm_calls: int = 0
    llm_limit: int = 0
    llm_calls_by_scope: Dict[str, int] = Field(default_factory=dict)
    llm_reservations: Dict[str, int] = Field(default_factory=dict)
    llm_scope_limits: Dict[str, int] = Field(default_factory=dict)
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

    def configure_llm_budget(
            self, limit: int, reservations: Dict[str, int],
            scope_limits: Optional[Dict[str, int]] = None) -> None:
        """Configure reservations *within* one global provider-call limit."""
        self.llm_limit = max(0, int(limit))
        remaining = self.llm_limit
        normalized: Dict[str, int] = {}
        for scope, requested in (reservations or {}).items():
            amount = min(max(0, int(requested)), remaining)
            normalized[str(scope)] = amount
            remaining -= amount
        self.llm_reservations = normalized
        self.llm_scope_limits = {
            str(scope): min(
                max(0, int(value)),
                normalized.get(str(scope), max(0, int(value))))
            for scope, value in (scope_limits or {}).items()
        }

    def release_llm_reservation(self, scope: str) -> None:
        """Release only the unused portion; historical usage stays auditable."""
        used = int(self.llm_calls_by_scope.get(scope, 0))
        if scope in self.llm_reservations:
            self.llm_reservations[scope] = min(
                self.llm_reservations[scope], used)
        if scope in self.llm_scope_limits:
            self.llm_scope_limits[scope] = min(
                self.llm_scope_limits[scope], used)

    def claim_llm_call(self, max_calls: int, scope: str) -> int:
        """Atomically account one actual provider request.

        A scope may use its own reservation or unreserved capacity, but never
        another scope's outstanding reservation. Hard-limited control-plane
        scopes cannot spill into the specialist/terminal pool.
        """
        scope = str(scope or "unclassified")
        limit = self.llm_limit if self.llm_limit > 0 else max(0, int(max_calls))
        used_by_scope = int(self.llm_calls_by_scope.get(scope, 0))
        scope_limit = self.llm_scope_limits.get(scope)
        if scope_limit is not None and used_by_scope >= scope_limit:
            raise BudgetExceeded(
                "llmScopeLimit", f"scope={scope} limit={scope_limit}")
        if self.llm_calls >= limit:
            raise BudgetExceeded("maxLlmCalls", f"limit={limit}")

        own_outstanding = max(
            0, int(self.llm_reservations.get(scope, 0)) - used_by_scope)
        other_outstanding = sum(
            max(0, int(reserved)
                - int(self.llm_calls_by_scope.get(other_scope, 0)))
            for other_scope, reserved in self.llm_reservations.items()
            if other_scope != scope
        )
        remaining_global = limit - self.llm_calls
        if own_outstanding <= 0 and remaining_global <= other_outstanding:
            raise BudgetExceeded(
                "llmReservation",
                f"scope={scope} protectedForOthers={other_outstanding}")

        self.llm_calls += 1
        self.llm_calls_by_scope[scope] = used_by_scope + 1
        return self.llm_calls

    def available_agent_llm_calls(self, max_calls: int) -> int:
        """Calls assignable to planned agents after control reservations."""
        limit = self.llm_limit if self.llm_limit > 0 else max(0, int(max_calls))
        remaining = max(0, limit - self.llm_calls)
        non_agent_outstanding = sum(
            max(0, int(reserved)
                - int(self.llm_calls_by_scope.get(scope, 0)))
            for scope, reserved in self.llm_reservations.items()
            if scope != "terminal" and not scope.startswith("agent:")
        )
        return max(0, remaining - non_agent_outstanding)

    def available_llm_calls_for_scope(self, max_calls: int,
                                      scope: str) -> int:
        """Return calls this exact scope can claim without stealing reserves.

        Aggregate agent capacity intentionally includes the terminal pool for
        planning. Runtime repair, however, must know whether *this* specialist
        can make another provider request before it emits a reallocation event.
        """
        scope = str(scope or "unclassified")
        limit = self.llm_limit if self.llm_limit > 0 else max(0, int(max_calls))
        remaining_global = max(0, limit - self.llm_calls)
        used_by_scope = int(self.llm_calls_by_scope.get(scope, 0))
        other_outstanding = sum(
            max(0, int(reserved)
                - int(self.llm_calls_by_scope.get(other_scope, 0)))
            for other_scope, reserved in self.llm_reservations.items()
            if other_scope != scope
        )
        available = max(0, remaining_global - other_outstanding)
        scope_limit = self.llm_scope_limits.get(scope)
        if scope_limit is not None:
            available = min(
                available, max(0, int(scope_limit) - used_by_scope))
        return available

    def llm_audit(self, max_calls: Optional[int] = None) -> Dict[str, Any]:
        limit = self.llm_limit if self.llm_limit > 0 else max(
            0, int(max_calls or 0))
        return {
            "limit": limit,
            "used": self.llm_calls,
            "remaining": max(0, limit - self.llm_calls),
            "callsByScope": dict(self.llm_calls_by_scope),
            "reservations": dict(self.llm_reservations),
            "scopeLimits": dict(self.llm_scope_limits),
            "agentAssignableRemaining": self.available_agent_llm_calls(limit),
        }

    def restore(self, counters: Dict[str, Any]) -> None:
        self.llm_calls = int(counters.get("llmCalls", 0))
        if "llmLimit" in counters:
            self.llm_limit = int(counters.get("llmLimit", 0))
        scopes = counters.get("llmCallsByScope")
        if isinstance(scopes, dict):
            self.llm_calls_by_scope = {
                str(k): int(v) for k, v in scopes.items()}
        else:
            self.llm_calls_by_scope = {"legacy": self.llm_calls}
            # Old checkpoints predate control accounting; planning already
            # happened, so do not strand capacity behind that reservation.
            self.release_llm_reservation("control")
        reservations = counters.get("llmReservations")
        if isinstance(reservations, dict):
            self.llm_reservations = {
                str(k): int(v) for k, v in reservations.items()}
        limits = counters.get("llmScopeLimits")
        if isinstance(limits, dict):
            self.llm_scope_limits = {
                str(k): int(v) for k, v in limits.items()}
        self.tool_calls = int(counters.get("toolCalls", 0))
        self.prompt_tokens = int(counters.get("promptTokens", 0))
        self.completion_tokens = int(counters.get("completionTokens", 0))
        self.prompt_cache_hit_tokens = int(
            counters.get("promptCacheHitTokens", 0))
        self.cost_cny = float(counters.get("costCny", 0.0))

    def snapshot(self) -> Dict[str, Any]:
        return {
            "llmCalls": self.llm_calls,
            "llmLimit": self.llm_limit,
            "llmCallsByScope": dict(self.llm_calls_by_scope),
            "llmReservations": dict(self.llm_reservations),
            "llmScopeLimits": dict(self.llm_scope_limits),
            "toolCalls": self.tool_calls,
            "promptTokens": self.prompt_tokens,
            "completionTokens": self.completion_tokens,
            "promptCacheHitTokens": self.prompt_cache_hit_tokens,
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
