from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

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


def _provider_concurrency_gate() -> tuple[asyncio.Semaphore, int]:
    global _provider_gate, _provider_gate_loop, _provider_gate_limit
    try:
        limit = max(1, int(os.getenv("LLM_MAX_CONCURRENT", "8")))
    except ValueError:
        limit = 8
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
                return {}, text, (
                    f"{exc}; exactDuplicate=false; "
                    f"trailingParse={type(tail_exc).__name__}")
        return {}, text, str(exc)


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
    supports_parallel_report_sections = True

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
            cls._shared_client = httpx.AsyncClient(
                timeout=httpx.Timeout(connect=10.0, read=timeout_seconds,
                                      write=30.0, pool=10.0),
                limits=httpx.Limits(max_connections=20, max_keepalive_connections=8),
            )
        return cls._shared_client

    @staticmethod
    def _budget_scope(agent_id: str, purpose: str) -> str:
        purpose_key = str(purpose or "").strip().lower()
        if agent_id == "CoordinatorAgent" or purpose_key in {
                "plan", "replan", "arbitration"}:
            return "control"
        if agent_id in {
                "ReportAgent", "ResumeOptimizeAgent",
                "InterviewQuestionAgent"}:
            return "terminal"
        return f"agent:{agent_id or 'unknown'}"

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

        # --- CONTEXT AUDIT (temporary) ---
        _audit = {"agent": agent_id, "budgetScope": scope}
        _full_text = "\n".join(m.get("content", "") for m in messages if m.get("content"))
        _audit["total_chars"] = len(_full_text)
        _audit["has_memory"] = "记忆" in _full_text or "memory" in _full_text.lower() or "[历史评估]" in _full_text
        _audit["has_skill"] = "[SKILL " in _full_text or "load_skill" in _full_text
        _native_tool_blob = json.dumps(tools or [], ensure_ascii=False).lower()
        _audit["has_mcp"] = (
            "model context protocol" in _native_tool_blob
            or '"mcpserver"' in _native_tool_blob
            or any(bool(message.get("tool_calls"))
                   for message in messages if isinstance(message, dict))
        )
        _audit["has_knowledge"] = "知识库" in _full_text or "knowledge" in _full_text.lower() or "[KB:" in _full_text
        _audit["has_tools"] = bool(tools)
        _audit["tool_count"] = len(tools) if tools else 0
        _audit["msg_count"] = len(messages)
        _audit["sys_chars"] = len(messages[0].get("content", "")) if messages else 0
        import time as _time_mod
        # --- END AUDIT ---

        attempts = 0
        max_retries = 2
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
            _audit["call#"] = call_index
            _audit["providerAttempt"] = attempts
            print(f"LLM_CONTEXT_AUDIT | {_audit}", flush=True)
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
            try:
                try:
                    turn, usage, finish_reason = await self._invoke(
                        messages, model, effective_max_tokens, temperature,
                        json_mode and not tools, tools=tools,
                        tool_choice=tool_choice)
                finally:
                    gate.release()
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
                    "callIndex": call_index,
                    "budgetScope": scope,
                    "durationMs": int((time.monotonic() - started) * 1000),
                    "queueWaitMs": queue_wait_ms,
                    "concurrencyLimit": concurrency_limit,
                    "promptTokens": prompt_tokens,
                    "completionTokens": completion_tokens,
                    "promptCacheHitTokens": cache_hit_tokens,
                    "attempts": attempts,
                    "finishReason": finish_reason,
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
            except asyncio.CancelledError:
                raise
            except LlmError as exc:
                last_error = exc
                self.breaker.record_failure()
                if not exc.retryable or attempts > max_retries:
                    break
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = LlmError("TRANSPORT", str(exc), True)
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

    async def _invoke(self, messages: List[Dict[str, str]], model: str,
                      max_tokens: int, temperature: float,
                      json_mode: bool, *,
                      tools: Optional[List[Dict[str, Any]]] = None,
                      tool_choice: Optional[Any] = None
                      ) -> tuple[LlmTurn, Dict[str, int], str]:
        url = f"{self.base_url}/chat/completions"
        timeout = httpx.Timeout(
            connect=10.0, read=float(self.llm_timeout_seconds),
            write=30.0, pool=10.0)
        body: Dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": False,
        }
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
        client = self._get_client(float(self.llm_timeout_seconds))
        response = await client.post(
            url,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            json=body,
            timeout=timeout,
        )
        if response.status_code >= 400:
            retryable = response.status_code in RETRYABLE_STATUS
            code = "RATE_LIMITED" if response.status_code == 429 else (
                "SERVER_ERROR" if response.status_code >= 500 else "REQUEST_ERROR")
            if response.status_code in (400, 413):
                # prompt too long / bad schema: never retry blindly
                retryable = False
                code = "PROMPT_OR_SCHEMA_ERROR"
            if response.status_code in (401, 403):
                retryable = False
                code = "AUTH_ERROR"
            raise LlmError(code, response.text[:300], retryable)
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
