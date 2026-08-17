from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import random
import time
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Callable, Dict, List, Optional

import httpx

from app.config import normalized_deepseek_base_url, settings
from app.runtime.events import RuntimeEmitter
from app.runtime.models import BudgetExceeded, RunBudget

logger = logging.getLogger(__name__)

RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}

# One workflow process serves multiple runs, and every run may fan out to
# several agents.  Bound provider requests across runs so a short burst does
# not turn into 60-second transport retries.  The gate is recreated per event
# loop because the Windows test suite uses multiple asyncio.run() loops.
_provider_gate: Optional[asyncio.Semaphore] = None
_provider_gate_loop: Optional[asyncio.AbstractEventLoop] = None
_provider_gate_limit = 0

class WorkflowRunExecutionController:
    """Reference-count one Java workflow permit across parallel branches."""

    def __init__(self, emitter: RuntimeEmitter) -> None:
        self.emitter = emitter
        self._lock_instance: Optional[asyncio.Lock] = None
        self._permit_held = True
        self._runnable = 0
        self._waiting_llm = 0

    def _lock(self) -> asyncio.Lock:
        if self._lock_instance is None:
            self._lock_instance = asyncio.Lock()
        return self._lock_instance

    async def enter_agent(self) -> "_WorkflowAgentLease":
        lease = _WorkflowAgentLease(self)
        async with self._lock():
            if not self._permit_held:
                await self.emitter.acquire_run_execution_permit()
                self._permit_held = True
            self._runnable += 1
            lease.state = "runnable"
        return lease

    async def suspend_for_llm(self, lease: "_WorkflowAgentLease") -> None:
        async with self._lock():
            if lease.state != "runnable":
                return
            self._runnable = max(0, self._runnable - 1)
            self._waiting_llm += 1
            lease.state = "waiting_llm"
            await self._release_if_fully_suspended()

    async def resume_after_llm(self, lease: "_WorkflowAgentLease") -> int:
        async with self._lock():
            if lease.state != "waiting_llm":
                return 0
            started = time.monotonic()
            if not self._permit_held:
                await self.emitter.acquire_run_execution_permit()
                self._permit_held = True
            wait_ms = int((time.monotonic() - started) * 1000)
            self._waiting_llm = max(0, self._waiting_llm - 1)
            self._runnable += 1
            lease.state = "runnable"
            return wait_ms

    async def leave_agent(self, lease: "_WorkflowAgentLease") -> None:
        async with self._lock():
            if lease.state == "runnable":
                self._runnable = max(0, self._runnable - 1)
            elif lease.state == "waiting_llm":
                self._waiting_llm = max(0, self._waiting_llm - 1)
            lease.state = "closed"
            await self._release_if_fully_suspended()

    async def _release_if_fully_suspended(self) -> None:
        if (self._permit_held and self._runnable == 0
                and self._waiting_llm > 0):
            await self.emitter.release_run_execution_permit()
            self._permit_held = False


class _WorkflowAgentLease:
    def __init__(self, controller: WorkflowRunExecutionController) -> None:
        self.controller = controller
        self.state = "new"

    async def suspend_for_llm(self) -> None:
        await self.controller.suspend_for_llm(self)

    async def resume_after_llm(self) -> int:
        return await self.controller.resume_after_llm(self)

    async def close(self) -> None:
        await self.controller.leave_agent(self)


_current_workflow_agent_lease: ContextVar[Optional[_WorkflowAgentLease]] = (
    ContextVar("workflow_agent_execution_lease", default=None))


@asynccontextmanager
async def workflow_agent_execution(
        controller: WorkflowRunExecutionController) -> AsyncIterator[None]:
    inherited = _current_workflow_agent_lease.get()
    if inherited is not None:
        yield
        return
    lease = await controller.enter_agent()
    token = _current_workflow_agent_lease.set(lease)
    try:
        yield
    finally:
        _current_workflow_agent_lease.reset(token)
        await lease.close()


def _provider_concurrency_gate() -> tuple[asyncio.Semaphore, int]:
    global _provider_gate, _provider_gate_loop, _provider_gate_limit
    try:
        limit = max(1, int(os.getenv("LLM_MAX_CONCURRENT", "48")))
    except ValueError:
        limit = 48
    loop = asyncio.get_running_loop()
    if (_provider_gate is None or _provider_gate_loop is not loop
            or _provider_gate_limit != limit):
        _provider_gate = asyncio.Semaphore(limit)
        _provider_gate_loop = loop
        _provider_gate_limit = limit
    return _provider_gate, limit


@dataclass(frozen=True)
class LlmToolCall:
    """One provider-native function proposal.

    ``raw_arguments`` is preserved for the assistant history sent back to the
    provider. ``arguments`` is parsed but never repaired or synthesized by the
    runtime.
    """

    tool_call_id: str
    name: str
    arguments: Dict[str, Any] = field(default_factory=dict)
    raw_arguments: str = "{}"
    arguments_error: str = ""


@dataclass(frozen=True)
class LlmTurn:
    content: str
    tool_calls: List[LlmToolCall] = field(default_factory=list)
    finish_reason: str = ""


class LlmError(RuntimeError):
    def __init__(self, code: str, message: str, retryable: bool) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.retryable = retryable


def _strip_json_whitespace_outside_strings(value: str) -> str:
    """Return JSON text without insignificant whitespace."""
    compact: List[str] = []
    in_string = False
    escaped = False
    for char in value:
        if in_string:
            compact.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        else:
            if char == '"':
                in_string = True
                compact.append(char)
            elif not char.isspace():
                compact.append(char)
    return "".join(compact)


def _parse_native_tool_arguments(
        raw_arguments: Any, *, allow_exact_duplicate: bool = False,
) -> tuple[Dict[str, Any], str, str]:
    """Parse provider-native arguments without generic JSON repair.

    DeepSeek occasionally repeats the *same* complete function arguments
    object twice, which ``json.loads`` reports as ``Extra data``.  For report
    sections only, accepting an exact semantic duplicate is lossless.  Any
    distinct second object or arbitrary trailing text remains malformed.
    """
    if isinstance(raw_arguments, dict):
        text = json.dumps(
            raw_arguments, ensure_ascii=False, separators=(",", ":"))
        return raw_arguments, text, ""

    text = str(raw_arguments or "")
    try:
        candidate = json.loads(text or "{}")
        if not isinstance(candidate, dict):
            raise ValueError("tool arguments must be a JSON object")
        return candidate, text, ""
    except (json.JSONDecodeError, ValueError) as exc:
        if (allow_exact_duplicate and isinstance(exc, json.JSONDecodeError)
                and exc.msg == "Extra data"):
            decoder = json.JSONDecoder()
            try:
                first, first_end = decoder.raw_decode(text)
                trailing = text[first_end:].strip()
                second, second_end = decoder.raw_decode(trailing)
                remainder = trailing[second_end:].strip()
                if (isinstance(first, dict) and first == second
                        and not remainder):
                    normalized = json.dumps(
                        first, ensure_ascii=False, separators=(",", ":"))
                    return first, normalized, ""
                detail = (
                    f"{exc}; exactDuplicate=false; "
                    f"trailingChars={len(trailing)}; "
                    f"trailingRemainderChars={len(remainder)}")
                return {}, text, detail
            except (json.JSONDecodeError, ValueError, TypeError) as tail_exc:
                # Some compatible streaming providers emit one complete
                # arguments object and then start repeating that same object
                # before the stream ends. Keep the complete object only when
                # the malformed tail is an exact textual prefix of it.
                try:
                    first, first_end = json.JSONDecoder().raw_decode(text)
                    first_raw = text[:first_end].strip()
                    trailing = text[first_end:].strip()
                    compact_first = _strip_json_whitespace_outside_strings(
                        first_raw)
                    compact_trailing = _strip_json_whitespace_outside_strings(
                        trailing)
                    if (isinstance(first, dict) and compact_trailing
                            and compact_first.startswith(compact_trailing)):
                        normalized = json.dumps(
                            first, ensure_ascii=False, separators=(",", ":"))
                        return first, normalized, ""
                except (json.JSONDecodeError, ValueError, TypeError):
                    pass
                return {}, text, (
                    f"{exc}; exactDuplicate=false; "
                    f"trailingParse={type(tail_exc).__name__}")
        return {}, text, str(exc)


def _provider_request_body(
        messages: List[Dict[str, Any]], model: str, max_tokens: int,
        temperature: float, json_mode: bool, *,
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: Optional[Any] = None,
        stream: bool = False,
) -> Dict[str, Any]:
    """Build the exact non-secret JSON body sent to the LLM provider."""
    body: Dict[str, Any] = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "stream": stream,
    }
    if stream:
        body["stream_options"] = {"include_usage": True}
    if tools:
        body["tools"] = tools
        body["thinking"] = {"type": "disabled"}
        if tool_choice is not None:
            body["tool_choice"] = tool_choice
    elif json_mode:
        body["response_format"] = {"type": "json_object"}
        body["thinking"] = {"type": "disabled"}
    elif max_tokens <= 200:
        body["thinking"] = {"type": "disabled"}
    return body


class CircuitBreaker:
    """Simple failure-rate breaker: N failures within a window opens the
    circuit for a cooldown; a half-open probe closes it again on success."""

    def __init__(self, threshold: int = 5, window_seconds: float = 90.0,
                 cooldown_seconds: float = 45.0) -> None:
        self.threshold = threshold
        self.window_seconds = window_seconds
        self.cooldown_seconds = cooldown_seconds
        self._failures: List[float] = []
        self._opened_at: Optional[float] = None

    def allow(self) -> bool:
        if self._opened_at is None:
            return True
        if time.monotonic() - self._opened_at >= self.cooldown_seconds:
            return True  # half-open probe
        return False

    def record_success(self) -> None:
        self._failures.clear()
        self._opened_at = None

    def record_failure(self) -> None:
        now = time.monotonic()
        self._failures = [t for t in self._failures if now - t <= self.window_seconds]
        self._failures.append(now)
        if len(self._failures) >= self.threshold:
            self._opened_at = now

    @property
    def open(self) -> bool:
        return self._opened_at is not None and not self.allow()


_shared_breaker = CircuitBreaker()


# DeepSeek V4 pricing (CNY per 1M tokens, cache-miss).
# V4-Flash: $0.14/$0.28 per 1M → ~1.0/2.0 CNY; V4-Pro: $0.435/$0.87 → ~3.1/6.3 CNY
PRICE_PROMPT_CNY_PER_M = 1.0
PRICE_COMPLETION_CNY_PER_M = 2.0


class ResilientLlmClient:
    """DeepSeek chat client with connect/read/total timeouts, bounded retry
    (max 2) with exponential backoff + jitter, cancellation via asyncio,
    error classification, circuit breaker, optional fallback model,
    duration/token/cost accounting and enforced JSON mode.

    Structured output is enforced at the API level with
    ``response_format={"type": "json_object"}`` (all agent prompts include
    the literal word "json" via the output schema), not just by prompting.
    """

    # Runtime capability flag: test/legacy adapters stay on the monolithic
    # report path unless they explicitly implement concurrent native turns.

    _shared_client: Optional[httpx.AsyncClient] = None

    def __init__(self, emitter: RuntimeEmitter, budget: RunBudget,
                 max_llm_calls: int, llm_timeout_seconds: int,
                 breaker: Optional[CircuitBreaker] = None,
                 max_cost_cny: float = 0.0,
                 max_total_tokens: int = 0) -> None:
        self.emitter = emitter
        self.budget = budget
        self.max_llm_calls = max_llm_calls
        self.llm_timeout_seconds = min(llm_timeout_seconds, 60)
        self.max_cost_cny = max_cost_cny
        self.max_total_tokens = max_total_tokens
        self.breaker = breaker or _shared_breaker
        self.base_url = normalized_deepseek_base_url()
        self.model = settings.deepseek_model or "deepseek-v4-flash"
        self.quality_model = settings.deepseek_quality_model or "deepseek-v4-pro"
        self.fallback_model = os.getenv("DEEPSEEK_FALLBACK_MODEL", "").strip() or self.model
        self.api_key = settings.deepseek_api_key

    @classmethod
    def _get_client(cls, timeout_seconds: float) -> httpx.AsyncClient:
        if cls._shared_client is None or cls._shared_client.is_closed:
            try:
                provider_limit = max(
                    1, int(os.getenv("LLM_MAX_CONCURRENT", "48")))
            except ValueError:
                provider_limit = 48
            try:
                max_connections = max(
                    provider_limit,
                    int(os.getenv(
                        "LLM_HTTP_MAX_CONNECTIONS",
                        str(max(20, provider_limit)))),
                )
            except ValueError:
                max_connections = max(20, provider_limit)
            try:
                max_keepalive = max(
                    1, int(os.getenv(
                        "LLM_HTTP_MAX_KEEPALIVE_CONNECTIONS",
                        str(min(max_connections, max(8, provider_limit // 2))))),
                )
            except ValueError:
                max_keepalive = min(max_connections, max(8, provider_limit // 2))
            max_keepalive = min(max_connections, max_keepalive)
            cls._shared_client = httpx.AsyncClient(
                timeout=httpx.Timeout(connect=10.0, read=timeout_seconds,
                                      write=30.0, pool=10.0),
                limits=httpx.Limits(
                    max_connections=max_connections,
                    max_keepalive_connections=max_keepalive),
            )
        return cls._shared_client

    @staticmethod
    def _budget_scope(agent_id: str, purpose: str) -> str:
        purpose_key = str(purpose or "").strip().lower()
        if agent_id == "CoordinatorAgent" or purpose_key in {
                "plan", "arbitration"}:
            return "control"
        if agent_id == "ReportAgent":
            return "terminal"
        return f"agent:{agent_id or 'unknown'}"

    async def _save_context_audit(
            self, *, messages: List[Dict[str, Any]], model: str,
            max_tokens: int, temperature: float, json_mode: bool,
            tools: Optional[List[Dict[str, Any]]], tool_choice: Optional[Any],
            stream: bool, agent_id: str, purpose: str, budget_scope: str,
            call_index: int, provider_attempt: int,
            trace_context: Dict[str, Any], duration_ms: int,
            turn: Optional[LlmTurn] = None,
            usage: Optional[Dict[str, int]] = None,
            finish_reason: str = "", error: Optional[BaseException] = None,
    ) -> bool:
        """Persist the real provider envelope for one attempt.

        The request body is generated by the same helper used by ``_invoke``;
        this prevents documentation/audit drift from the bytes represented by
        the runtime call. Authorization headers are never included.
        """
        if not settings.context_audit_enabled:
            return False
        provider_request = _provider_request_body(
            messages, model, max_tokens, temperature,
            json_mode and not tools, tools=tools, tool_choice=tool_choice,
            stream=stream)
        canonical_request = json.dumps(
            provider_request, ensure_ascii=False, separators=(",", ":"),
            sort_keys=True)
        trace_blob = json.dumps(
            trace_context, ensure_ascii=False, separators=(",", ":"),
            sort_keys=True)
        span_hash = hashlib.sha256(trace_blob.encode("utf-8")).hexdigest()[:12]
        span_id = f"ctx-{span_hash}-c{call_index}-a{provider_attempt}"[:64]
        role_chars: Dict[str, int] = {}
        for message in messages:
            role = str(message.get("role") or "unknown")
            content = message.get("content")
            role_chars[role] = role_chars.get(role, 0) + len(
                content if isinstance(content, str)
                else json.dumps(content, ensure_ascii=False))
        prompt_envelope = {
            "schemaVersion": 1,
            "auditType": "agent_provider_request",
            "runId": self.emitter.run_id,
            "conversationId": self.emitter.conversation_id,
            "traceId": self.emitter.trace_id,
            "agentId": agent_id,
            "purpose": purpose,
            "budgetScope": budget_scope,
            "callIndex": call_index,
            "providerAttempt": provider_attempt,
            "traceContext": trace_context,
            "providerUrl": f"{self.base_url}/chat/completions",
            "providerRequest": provider_request,
            "inventory": {
                "messageCount": len(messages),
                "messageRoleChars": role_chars,
                "toolCount": len(tools or []),
                "requestSha256": hashlib.sha256(
                    canonical_request.encode("utf-8")).hexdigest(),
            },
        }
        usage = dict(usage or {})
        response_envelope: Optional[Dict[str, Any]] = None
        if turn is not None:
            response_envelope = {
                "schemaVersion": 1,
                "auditType": "provider_agent_response",
                "content": turn.content,
                "toolCalls": [{
                    "id": call.tool_call_id,
                    "name": call.name,
                    "arguments": call.arguments,
                    "rawArguments": call.raw_arguments,
                    "argumentsError": call.arguments_error,
                } for call in turn.tool_calls],
                "finishReason": finish_reason or turn.finish_reason,
                "usage": usage,
            }
        error_code = ""
        error_body = ""
        if error is not None:
            error_code = (
                error.code if isinstance(error, LlmError)
                else type(error).__name__)
            error_body = str(error)
        saved = await self.emitter.save_llm_invocation({
            "traceId": self.emitter.trace_id,
            "spanId": span_id,
            "modelName": model,
            "agentRole": agent_id,
            "purpose": purpose,
            "durationMs": max(0, duration_ms),
            "prompt": json.dumps(
                prompt_envelope, ensure_ascii=False, separators=(",", ":")),
            "response": (
                json.dumps(response_envelope, ensure_ascii=False,
                           separators=(",", ":"))
                if response_envelope is not None else ""),
            "inputTokens": int(usage.get("prompt_tokens") or 0),
            "outputTokens": int(usage.get("completion_tokens") or 0),
            "finishReason": (
                finish_reason or ("error" if error is not None else "")),
            "errorCode": error_code,
            "errorBody": error_body,
        })
        logger.info(
            "context audit persisted=%s run=%s agent=%s call=%s attempt=%s "
            "messages=%s tools=%s",
            saved, self.emitter.run_id, agent_id, call_index,
            provider_attempt, len(messages), len(tools or []))
        return saved

    async def chat(self, messages: List[Dict[str, str]], *, agent_id: str,
                   purpose: str = "", max_tokens: int = 2048,
                   temperature: float = 0.2, json_mode: bool = True,
                   tools: Optional[List[Dict[str, Any]]] = None,
                   tool_choice: Optional[Dict[str, Any]] = None,
                   use_quality: bool = False,
                   _return_turn: bool = False,
                   budget_scope: str = "",
                   trace_context: Optional[Dict[str, Any]] = None,
                   max_output_tokens_hard: Optional[int] = None) -> Any:
        """LLM chat completion.

        The legacy string return remains the default. Native agent loops call
        :meth:`chat_turn` so every provider tool call (name, id and model-made
        arguments) is preserved instead of flattening the first call into a
        JSON string.
        """
        if not self.api_key:
            raise LlmError("NO_API_KEY", "DeepSeek API key not configured; fail closed", False)
        if self.max_cost_cny > 0 and self.budget.cost_cny >= self.max_cost_cny:
            raise BudgetExceeded("maxCostCny",
                                 f"spent={self.budget.cost_cny:.4f} limit={self.max_cost_cny}")
        if self.max_total_tokens > 0 and self.budget.total_tokens >= self.max_total_tokens:
            raise BudgetExceeded("maxTotalTokens",
                                 f"used={self.budget.total_tokens} limit={self.max_total_tokens}")
        if not self.breaker.allow():
            raise LlmError("CIRCUIT_OPEN", "LLM circuit breaker open", False)

        scope = budget_scope or self._budget_scope(agent_id, purpose)
        model = self.quality_model if use_quality else self.model
        trace_payload = {
            str(key): value for key, value in (trace_context or {}).items()
            if value is not None
        }

        import time as _time_mod

        attempts = 0
        # A monolithic quality report can consume the entire tail budget when
        # the provider stalls for two consecutive 60-second attempts.  Keep
        # the first Pro attempt, then fail over to Flash immediately after a
        # retryable transport/provider error.  Other agent calls retain the
        # regular two-retry policy.
        is_terminal_quality_report = (
            agent_id == "ReportAgent"
            and str(purpose or "").strip().lower() == "report"
            and use_quality)
        max_retries = 1 if is_terminal_quality_report else 2
        delay = 1.5
        last_error: Optional[Exception] = None
        effective_max_tokens = max_tokens
        output_token_ceiling = max(
            1, int(max_output_tokens_hard or 8192))
        while attempts <= max_retries:
            attempts += 1
            try:
                call_index = self.budget.claim_llm_call(
                    self.max_llm_calls, scope)
            except BudgetExceeded as exc:
                await self.emitter.emit(
                    "llm.failed", agent_id=agent_id, payload={
                        **trace_payload,
                        "error": str(exc),
                        "attempts": attempts - 1,
                        "budgetScope": scope,
                        "budgetRejected": True,
                        "budget": self.budget.llm_audit(
                            self.max_llm_calls)})
                raise
            _call_start = _time_mod.perf_counter()
            execution_lease = _current_workflow_agent_lease.get()
            execution_reacquire_wait_ms = 0
            if execution_lease is not None:
                await execution_lease.suspend_for_llm()
            gate, concurrency_limit = _provider_concurrency_gate()
            queue_started = time.monotonic()
            was_queued = gate.locked()
            if was_queued:
                await self.emitter.emit(
                    "llm.queued", agent_id=agent_id, payload={
                        **trace_payload,
                        "model": model,
                        "purpose": purpose,
                        "callIndex": call_index,
                        "providerAttempt": attempts,
                        "budgetScope": scope,
                        "concurrencyLimit": concurrency_limit,
                    })
            await gate.acquire()
            queue_wait_ms = int(
                (time.monotonic() - queue_started) * 1000)
            started = time.monotonic()
            await self.emitter.emit("llm.started", agent_id=agent_id, payload={
                **trace_payload,
                "model": model,
                "purpose": purpose,
                "callIndex": call_index,
                "providerAttempt": attempts,
                "budgetScope": scope,
                "budget": self.budget.llm_audit(self.max_llm_calls),
                "useQuality": use_quality,
                "queueWaitMs": queue_wait_ms,
                "concurrencyLimit": concurrency_limit,
            })
            first_token: Dict[str, Any] = {}
            first_token_emit_task: Optional[asyncio.Task[Any]] = None

            def on_first_token(ttft_ms: int, output_kind: str) -> None:
                """Publish TTFT without blocking consumption of the SSE body."""
                nonlocal first_token_emit_task
                if first_token:
                    return
                first_token.update({
                    "ttftMs": ttft_ms,
                    "outputKind": output_kind,
                })
                first_token_emit_task = asyncio.create_task(
                    self.emitter.emit(
                        "llm.first_token", agent_id=agent_id, payload={
                            **trace_payload,
                            "model": model,
                            "purpose": purpose,
                            "callIndex": call_index,
                            "providerAttempt": attempts,
                            "budgetScope": scope,
                            "ttftMs": ttft_ms,
                            "outputKind": output_kind,
                            "queueWaitMs": queue_wait_ms,
                        }))

            stream_response = (
                str(os.getenv(
                    "LLM_STREAM_REPORT_SECTIONS", "1")).strip().lower()
                not in {"0", "false", "no", "off"}
                and agent_id == "ReportAgent"
                and (str(purpose or "") == "report"
                     or str(purpose or "").startswith("report_")))
            audit_finalized = False
            provider_cancelled = False
            provider_duration_ms = 0
            try:
                try:
                    if stream_response:
                        turn, usage, finish_reason = await self._invoke(
                            messages, model, effective_max_tokens, temperature,
                            json_mode and not tools, tools=tools,
                            tool_choice=tool_choice,
                            stream=True,
                            on_first_token=on_first_token)
                    else:
                        # Preserve the long-standing call shape for benchmark
                        # adapters and tests that replace _invoke directly.
                        turn, usage, finish_reason = await self._invoke(
                            messages, model, effective_max_tokens, temperature,
                            json_mode and not tools, tools=tools,
                            tool_choice=tool_choice)
                    provider_duration_ms = int(
                        (time.monotonic() - started) * 1000)
                except asyncio.CancelledError:
                    provider_cancelled = True
                    raise
                finally:
                    gate.release()
                    if execution_lease is not None and not provider_cancelled:
                        execution_reacquire_wait_ms = (
                            await execution_lease.resume_after_llm())
                if first_token_emit_task is not None:
                    # The stream has already been consumed, so this wait does
                    # not add provider backpressure. It only guarantees event
                    # ordering before llm.completed is persisted.
                    await first_token_emit_task
                if settings.context_audit_enabled:
                    await self._save_context_audit(
                        messages=messages,
                        model=model,
                        max_tokens=effective_max_tokens,
                        temperature=temperature,
                        json_mode=json_mode,
                        tools=tools,
                        tool_choice=tool_choice,
                        stream=stream_response,
                        agent_id=agent_id,
                        purpose=purpose,
                        budget_scope=scope,
                        call_index=call_index,
                        provider_attempt=attempts,
                        trace_context=trace_payload,
                        duration_ms=int(
                            (time.monotonic() - started) * 1000),
                        turn=turn,
                        usage=usage,
                        finish_reason=finish_reason)
                    audit_finalized = True
                content = turn.content
                if json_mode and not tools and not content.strip():
                    raise LlmError("EMPTY_JSON_CONTENT",
                                   "JSON mode returned empty content", True)
                if tools and not turn.tool_calls and not content.strip():
                    raise LlmError("EMPTY_TOOL_TURN",
                                   "model returned neither content nor tool calls", True)
                if json_mode and not tools and finish_reason == "length":
                    # Truncated mid-JSON: the payload can never parse. Retry
                    # once with a doubled output budget before giving up.
                    if effective_max_tokens < output_token_ceiling:
                        effective_max_tokens = min(
                            effective_max_tokens * 2,
                            output_token_ceiling)
                        raise LlmError("JSON_TRUNCATED",
                                       f"finish_reason=length at {usage.get('completion_tokens', 0)} tokens",
                                       True)
                    raise LlmError("JSON_TRUNCATED",
                                   "output exceeds configured repair limit "
                                   f"{output_token_ceiling}", False)
                self.breaker.record_success()
                prompt_tokens = usage.get("prompt_tokens", 0)
                completion_tokens = usage.get("completion_tokens", 0)
                cache_hit_tokens = usage.get("prompt_cache_hit_tokens", 0)
                self.budget.prompt_tokens += prompt_tokens
                self.budget.completion_tokens += completion_tokens
                self.budget.prompt_cache_hit_tokens += cache_hit_tokens
                # DeepSeek bills prefix-cache hits at ~1/10 of a miss; account
                # for it so the cost budget reflects reality.
                cache_miss_tokens = max(0, prompt_tokens - cache_hit_tokens)
                self.budget.cost_cny += (
                    cache_miss_tokens / 1e6 * PRICE_PROMPT_CNY_PER_M
                    + cache_hit_tokens / 1e6 * PRICE_PROMPT_CNY_PER_M * 0.1
                    + completion_tokens / 1e6 * PRICE_COMPLETION_CNY_PER_M)
                try:
                    from app.runtime.context import calibrate, estimate_tokens
                    estimated = sum(estimate_tokens(m.get("content", ""))
                                    for m in messages)
                    calibrate(estimated, prompt_tokens)
                except Exception:  # noqa: BLE001 - calibration is best-effort
                    pass
                _call_elapsed = int((_time_mod.perf_counter() - _call_start) * 1000)
                print(f"LLM_LATENCY | agent={agent_id} call#={call_index} "
                      f"elapsed_ms={_call_elapsed} prompt_tokens={prompt_tokens} "
                      f"completion_tokens={completion_tokens} model={model}", flush=True)
                await self.emitter.emit("llm.completed", agent_id=agent_id, payload={
                    **trace_payload,
                    "model": model,
                    "purpose": purpose,
                    "callIndex": call_index,
                    "budgetScope": scope,
                    "durationMs": int((time.monotonic() - started) * 1000),
                    "providerDurationMs": provider_duration_ms,
                    "queueWaitMs": queue_wait_ms,
                    "concurrencyLimit": concurrency_limit,
                    "agentExecutionReacquireWaitMs": (
                        execution_reacquire_wait_ms),
                    "promptTokens": prompt_tokens,
                    "completionTokens": completion_tokens,
                    "promptCacheHitTokens": cache_hit_tokens,
                    "attempts": attempts,
                    "finishReason": finish_reason,
                    "streamed": stream_response,
                    "ttftMs": first_token.get("ttftMs"),
                    "firstOutputKind": first_token.get("outputKind"),
                    "toolCallCount": len(turn.tool_calls),
                    "toolNames": [call.name for call in turn.tool_calls],
                })
                if _return_turn:
                    return turn
                if turn.tool_calls:
                    # Compatibility for older forced-function callers.
                    raw_arguments = turn.tool_calls[0].raw_arguments
                    if not raw_arguments.strip():
                        raise LlmError("EMPTY_FUNCTION_ARGS",
                                       "function call returned empty arguments", True)
                    return raw_arguments
                return content
            except asyncio.CancelledError as exc:
                if settings.context_audit_enabled and not audit_finalized:
                    await self._save_context_audit(
                        messages=messages, model=model,
                        max_tokens=effective_max_tokens,
                        temperature=temperature, json_mode=json_mode,
                        tools=tools, tool_choice=tool_choice,
                        stream=stream_response, agent_id=agent_id,
                        purpose=purpose, budget_scope=scope,
                        call_index=call_index, provider_attempt=attempts,
                        trace_context=trace_payload,
                        duration_ms=int(
                            (time.monotonic() - started) * 1000),
                        error=exc)
                raise
            except LlmError as exc:
                if settings.context_audit_enabled and not audit_finalized:
                    await self._save_context_audit(
                        messages=messages, model=model,
                        max_tokens=effective_max_tokens,
                        temperature=temperature, json_mode=json_mode,
                        tools=tools, tool_choice=tool_choice,
                        stream=stream_response, agent_id=agent_id,
                        purpose=purpose, budget_scope=scope,
                        call_index=call_index, provider_attempt=attempts,
                        trace_context=trace_payload,
                        duration_ms=int(
                            (time.monotonic() - started) * 1000),
                        error=exc)
                last_error = exc
                self.breaker.record_failure()
                if not exc.retryable or attempts > max_retries:
                    break
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = LlmError("TRANSPORT", str(exc), True)
                if settings.context_audit_enabled and not audit_finalized:
                    await self._save_context_audit(
                        messages=messages, model=model,
                        max_tokens=effective_max_tokens,
                        temperature=temperature, json_mode=json_mode,
                        tools=tools, tool_choice=tool_choice,
                        stream=stream_response, agent_id=agent_id,
                        purpose=purpose, budget_scope=scope,
                        call_index=call_index, provider_attempt=attempts,
                        trace_context=trace_payload,
                        duration_ms=int(
                            (time.monotonic() - started) * 1000),
                        error=last_error)
                self.breaker.record_failure()
                if attempts > max_retries:
                    break
            retry_reason = "UNKNOWN"
            if isinstance(last_error, LlmError):
                retry_reason = last_error.code
            elif isinstance(last_error, httpx.TimeoutException):
                retry_reason = "READ_TIMEOUT"
            elif isinstance(last_error, httpx.TransportError):
                retry_reason = "CONNECT_TIMEOUT"
            await self.emitter.emit("llm.retrying", agent_id=agent_id, payload={
                **trace_payload,
                "attempt": attempts, "maxRetries": max_retries,
                "callIndex": call_index, "budgetScope": scope,
                "reason": retry_reason,
                "error": str(last_error)[:200]})
            await asyncio.sleep(delay + random.uniform(0, 0.6))
            delay = min(delay * 2, 12.0)
            if self.fallback_model and attempts == max_retries:
                model = self.fallback_model

        await self.emitter.emit("llm.failed", agent_id=agent_id, payload={
            **trace_payload,
            "error": str(last_error)[:300], "attempts": attempts,
            "budgetScope": scope,
            "budget": self.budget.llm_audit(self.max_llm_calls)})
        if isinstance(last_error, LlmError):
            raise last_error
        raise LlmError("UNKNOWN", str(last_error), False)

    async def chat_turn(self, messages: List[Dict[str, Any]], *, agent_id: str,
                        purpose: str = "", max_tokens: int = 2048,
                        temperature: float = 0.2,
                        tools: Optional[List[Dict[str, Any]]] = None,
                        tool_choice: Optional[Any] = None,
                        use_quality: bool = False,
                        budget_scope: str = "",
                        trace_context: Optional[Dict[str, Any]] = None) -> LlmTurn:
        """Return one provider-native assistant turn including all tool calls."""
        turn = await self.chat(
            messages, agent_id=agent_id, purpose=purpose,
            max_tokens=max_tokens, temperature=temperature,
            json_mode=False, tools=tools, tool_choice=tool_choice,
            use_quality=use_quality, _return_turn=True,
            budget_scope=budget_scope, trace_context=trace_context)
        if not isinstance(turn, LlmTurn):
            raise LlmError("MALFORMED_RESPONSE",
                           "native tool turn was not preserved", False)
        return turn

    async def _invoke(self, messages: List[Dict[str, Any]], model: str,
                      max_tokens: int, temperature: float,
                      json_mode: bool, *,
                      tools: Optional[List[Dict[str, Any]]] = None,
                      tool_choice: Optional[Any] = None,
                      stream: bool = False,
                      on_first_token: Optional[
                          Callable[[int, str], None]] = None,
                      ) -> tuple[LlmTurn, Dict[str, int], str]:
        url = f"{self.base_url}/chat/completions"
        timeout = httpx.Timeout(
            connect=10.0, read=float(self.llm_timeout_seconds),
            write=30.0, pool=10.0)
        body = _provider_request_body(
            messages, model, max_tokens, temperature, json_mode,
            tools=tools, tool_choice=tool_choice, stream=stream)
        client = self._get_client(float(self.llm_timeout_seconds))
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        if stream:
            headers["Accept"] = "text/event-stream"
            return await self._invoke_stream(
                client, url, headers, body, timeout,
                on_first_token=on_first_token)

        response = await client.post(
            url, headers=headers, json=body, timeout=timeout)
        self._raise_for_provider_status(response.status_code, response.text)
        data = response.json()
        try:
            choice = data["choices"][0]
            message = choice["message"]
            content = message.get("content") or ""
            parsed_tool_calls: List[LlmToolCall] = []
            for index, raw_call in enumerate(message.get("tool_calls") or []):
                if not isinstance(raw_call, dict):
                    continue
                function = raw_call.get("function") or {}
                if not isinstance(function, dict):
                    continue
                name = str(function.get("name") or "").strip()
                arguments, raw_arguments_text, arguments_error = (
                    _parse_native_tool_arguments(
                        function.get("arguments"),
                        allow_exact_duplicate=(
                            name == "emit_report_section")))
                parsed_tool_calls.append(LlmToolCall(
                    tool_call_id=str(raw_call.get("id") or f"call-{index + 1}"),
                    name=name,
                    arguments=arguments,
                    raw_arguments=raw_arguments_text,
                    arguments_error=arguments_error,
                ))
        except (KeyError, IndexError) as exc:
            raise LlmError("MALFORMED_RESPONSE", str(exc), False) from exc
        usage = data.get("usage") or {}
        finish_reason = str(choice.get("finish_reason") or "")
        return LlmTurn(
            content=content,
            tool_calls=parsed_tool_calls,
            finish_reason=finish_reason,
        ), {
            "prompt_tokens": int(usage.get("prompt_tokens") or 0),
            "completion_tokens": int(usage.get("completion_tokens") or 0),
            "prompt_cache_hit_tokens": int(usage.get("prompt_cache_hit_tokens") or 0),
        }, finish_reason

    @staticmethod
    def _raise_for_provider_status(status_code: int, body: str) -> None:
        if status_code < 400:
            return
        retryable = status_code in RETRYABLE_STATUS
        code = "RATE_LIMITED" if status_code == 429 else (
            "SERVER_ERROR" if status_code >= 500 else "REQUEST_ERROR")
        if status_code in (400, 413):
            retryable = False
            code = "PROMPT_OR_SCHEMA_ERROR"
        if status_code in (401, 403):
            retryable = False
            code = "AUTH_ERROR"
        raise LlmError(code, body[:300], retryable)

    async def _invoke_stream(
            self, client: httpx.AsyncClient, url: str,
            headers: Dict[str, str], body: Dict[str, Any],
            timeout: httpx.Timeout, *,
            on_first_token: Optional[Callable[[int, str], None]] = None,
    ) -> tuple[LlmTurn, Dict[str, int], str]:
        """Consume an OpenAI-compatible SSE response into the existing turn.

        Tool argument fragments are never parsed or exposed as a report until
        the provider finishes the function call. This preserves the same
        structured-output validation contract as the non-streaming path.
        """
        request_started = time.monotonic()
        content_parts: List[str] = []
        tool_parts: Dict[int, Dict[str, Any]] = {}
        usage: Dict[str, Any] = {}
        finish_reason = ""
        first_output_seen = False

        async with client.stream(
                "POST", url, headers=headers, json=body,
                timeout=timeout) as response:
            if response.status_code >= 400:
                raw_error = (await response.aread()).decode(
                    "utf-8", errors="replace")
                self._raise_for_provider_status(
                    response.status_code, raw_error)

            async for line in response.aiter_lines():
                line = line.strip()
                if not line or line.startswith(":"):
                    continue
                if not line.startswith("data:"):
                    continue
                raw_data = line[5:].strip()
                if raw_data == "[DONE]":
                    break
                try:
                    chunk = json.loads(raw_data)
                except json.JSONDecodeError as exc:
                    raise LlmError(
                        "MALFORMED_STREAM", str(exc), True) from exc

                if isinstance(chunk.get("usage"), dict):
                    usage = chunk["usage"]
                choices = chunk.get("choices") or []
                if not choices:
                    continue
                choice = choices[0] if isinstance(choices[0], dict) else {}
                if choice.get("finish_reason") is not None:
                    finish_reason = str(choice.get("finish_reason") or "")
                delta = choice.get("delta") or {}
                if not isinstance(delta, dict):
                    continue
                content_delta = delta.get("content")
                if content_delta:
                    content_parts.append(str(content_delta))
                    if not first_output_seen:
                        first_output_seen = True
                        if on_first_token is not None:
                            on_first_token(int(
                                (time.monotonic() - request_started) * 1000),
                                "content")
                for raw_call in delta.get("tool_calls") or []:
                    if not isinstance(raw_call, dict):
                        continue
                    try:
                        index = int(raw_call.get("index", 0))
                    except (TypeError, ValueError):
                        index = 0
                    item = tool_parts.setdefault(index, {
                        "id": "", "name": "", "arguments": []})
                    if raw_call.get("id"):
                        item["id"] = str(raw_call["id"])
                    function = raw_call.get("function") or {}
                    if isinstance(function, dict):
                        if function.get("name"):
                            item["name"] += str(function["name"])
                        arguments_delta = function.get("arguments")
                        if arguments_delta:
                            item["arguments"].append(
                                str(arguments_delta))
                    if (not first_output_seen
                            and (item["name"] or item["arguments"])):
                        first_output_seen = True
                        if on_first_token is not None:
                            on_first_token(int(
                                (time.monotonic() - request_started) * 1000),
                                "tool_call")

        parsed_tool_calls: List[LlmToolCall] = []
        for index, item in sorted(tool_parts.items()):
            raw_arguments = "".join(item["arguments"])
            arguments, raw_arguments_text, arguments_error = (
                _parse_native_tool_arguments(
                    raw_arguments,
                    allow_exact_duplicate=(
                        item["name"] == "emit_report_section")))
            parsed_tool_calls.append(LlmToolCall(
                tool_call_id=item["id"] or f"call-{index + 1}",
                name=item["name"],
                arguments=arguments,
                raw_arguments=raw_arguments_text,
                arguments_error=arguments_error,
            ))
        return LlmTurn(
            content="".join(content_parts),
            tool_calls=parsed_tool_calls,
            finish_reason=finish_reason,
        ), {
            "prompt_tokens": int(usage.get("prompt_tokens") or 0),
            "completion_tokens": int(usage.get("completion_tokens") or 0),
            "prompt_cache_hit_tokens": int(
                usage.get("prompt_cache_hit_tokens") or 0),
        }, finish_reason


def extract_json_object(raw: str) -> Dict[str, Any]:
    """Best-effort extraction of the first JSON object from an LLM reply."""
    if not raw:
        return {}
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
    start = text.find("{")
    if start < 0:
        return {}
    depth = 0
    in_string = False
    escaped = False
    for idx in range(start, len(text)):
        ch = text[idx]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                candidate = text[start:idx + 1]
                try:
                    parsed = json.loads(candidate)
                    return parsed if isinstance(parsed, dict) else {}
                except json.JSONDecodeError:
                    return {}
    return {}
