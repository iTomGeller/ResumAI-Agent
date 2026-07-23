from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import time
from typing import Any, Dict, List, Optional

import httpx

from app.config import normalized_deepseek_base_url, settings
from app.runtime.events import RuntimeEmitter
from app.runtime.models import BudgetExceeded, RunBudget

logger = logging.getLogger(__name__)

RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}


class LlmError(RuntimeError):
    def __init__(self, code: str, message: str, retryable: bool) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.retryable = retryable


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


# DeepSeek pricing (CNY per 1M tokens, cache-miss) used for the cost budget axis.
PRICE_PROMPT_CNY_PER_M = 2.0
PRICE_COMPLETION_CNY_PER_M = 8.0


class ResilientLlmClient:
    """DeepSeek chat client with connect/read/total timeouts, bounded retry
    (max 2) with exponential backoff + jitter, cancellation via asyncio,
    error classification, circuit breaker, optional fallback model,
    duration/token/cost accounting and enforced JSON mode.

    Structured output is enforced at the API level with
    ``response_format={"type": "json_object"}`` (all agent prompts include
    the literal word "json" via the output schema), not just by prompting.
    """

    def __init__(self, emitter: RuntimeEmitter, budget: RunBudget,
                 max_llm_calls: int, llm_timeout_seconds: int,
                 breaker: Optional[CircuitBreaker] = None,
                 max_cost_cny: float = 0.0,
                 max_total_tokens: int = 0) -> None:
        self.emitter = emitter
        self.budget = budget
        self.max_llm_calls = max_llm_calls
        self.llm_timeout_seconds = llm_timeout_seconds
        self.max_cost_cny = max_cost_cny
        self.max_total_tokens = max_total_tokens
        self.breaker = breaker or _shared_breaker
        self.base_url = normalized_deepseek_base_url()
        self.model = settings.deepseek_model or "deepseek-chat"
        self.fallback_model = os.getenv("DEEPSEEK_FALLBACK_MODEL", "").strip() or None
        self.api_key = settings.deepseek_api_key

    async def chat(self, messages: List[Dict[str, str]], *, agent_id: str,
                   purpose: str = "", max_tokens: int = 2048,
                   temperature: float = 0.2, json_mode: bool = True,
                   tools: Optional[List[Dict[str, Any]]] = None,
                   tool_choice: Optional[Dict[str, Any]] = None) -> str:
        """When ``tools``/``tool_choice`` force a function call, the returned
        string is the function's arguments JSON — provider-side schema
        enforcement, one layer stronger than json_object mode."""
        if not self.api_key:
            raise LlmError("NO_API_KEY", "DeepSeek API key not configured; fail closed", False)
        if self.budget.llm_calls >= self.max_llm_calls:
            raise BudgetExceeded("maxLlmCalls", f"limit={self.max_llm_calls}")
        if self.max_cost_cny > 0 and self.budget.cost_cny >= self.max_cost_cny:
            raise BudgetExceeded("maxCostCny",
                                 f"spent={self.budget.cost_cny:.4f} limit={self.max_cost_cny}")
        if self.max_total_tokens > 0 and self.budget.total_tokens >= self.max_total_tokens:
            raise BudgetExceeded("maxTotalTokens",
                                 f"used={self.budget.total_tokens} limit={self.max_total_tokens}")
        if not self.breaker.allow():
            raise LlmError("CIRCUIT_OPEN", "LLM circuit breaker open", False)

        self.budget.llm_calls += 1
        await self.emitter.emit("llm.started", agent_id=agent_id, payload={
            "model": self.model, "purpose": purpose, "callIndex": self.budget.llm_calls})

        attempts = 0
        max_retries = 2
        delay = 1.5
        last_error: Optional[Exception] = None
        model = self.model
        effective_max_tokens = max_tokens
        forcing_function = bool(tools and tool_choice)
        while attempts <= max_retries:
            attempts += 1
            started = time.monotonic()
            try:
                content, usage, finish_reason = await self._invoke(
                    messages, model, effective_max_tokens, temperature,
                    json_mode and not forcing_function, tools=tools,
                    tool_choice=tool_choice)
                if forcing_function and not content.strip():
                    # A forced function call must produce arguments.
                    raise LlmError("EMPTY_FUNCTION_ARGS",
                                   "forced tool call returned no arguments", True)
                if json_mode and not forcing_function and not content.strip():
                    # Known DeepSeek JSON-mode failure mode: occasional empty
                    # content. Treat as retryable instead of returning garbage.
                    raise LlmError("EMPTY_JSON_CONTENT",
                                   "JSON mode returned empty content", True)
                if json_mode and finish_reason == "length":
                    # Truncated mid-JSON: the payload can never parse. Retry
                    # once with a doubled output budget before giving up.
                    if effective_max_tokens < 8192:
                        effective_max_tokens = min(effective_max_tokens * 2, 8192)
                        raise LlmError("JSON_TRUNCATED",
                                       f"finish_reason=length at {usage.get('completion_tokens', 0)} tokens",
                                       True)
                    raise LlmError("JSON_TRUNCATED",
                                   "output exceeds model limit even at 8192", False)
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
                await self.emitter.emit("llm.completed", agent_id=agent_id, payload={
                    "model": model,
                    "durationMs": int((time.monotonic() - started) * 1000),
                    "promptTokens": prompt_tokens,
                    "completionTokens": completion_tokens,
                    "promptCacheHitTokens": cache_hit_tokens,
                    "attempts": attempts})
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
                "attempt": attempts, "maxRetries": max_retries,
                "reason": retry_reason,
                "error": str(last_error)[:200]})
            await asyncio.sleep(delay + random.uniform(0, 0.6))
            delay = min(delay * 2, 12.0)
            if self.fallback_model and attempts == max_retries:
                model = self.fallback_model

        await self.emitter.emit("llm.failed", agent_id=agent_id, payload={
            "error": str(last_error)[:300], "attempts": attempts})
        if isinstance(last_error, LlmError):
            raise last_error
        raise LlmError("UNKNOWN", str(last_error), False)

    async def _invoke(self, messages: List[Dict[str, str]], model: str,
                      max_tokens: int, temperature: float,
                      json_mode: bool, *,
                      tools: Optional[List[Dict[str, Any]]] = None,
                      tool_choice: Optional[Dict[str, Any]] = None
                      ) -> tuple[str, Dict[str, int], str]:
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
            # Provider-side schema enforcement: the forced function's
            # parameters ARE the decision schema.
            body["tools"] = tools
            if tool_choice:
                body["tool_choice"] = tool_choice
        elif json_mode:
            # API-enforced valid JSON (prompt already contains the word "json"
            # through the output schema, as DeepSeek requires).
            body["response_format"] = {"type": "json_object"}
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                url,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json=body,
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
            tool_calls = message.get("tool_calls") or []
            if tool_calls:
                # Forced function call: arguments JSON is the payload.
                arguments = (tool_calls[0].get("function") or {}).get("arguments") or ""
                if arguments:
                    content = arguments
        except (KeyError, IndexError) as exc:
            raise LlmError("MALFORMED_RESPONSE", str(exc), False) from exc
        usage = data.get("usage") or {}
        return content, {
            "prompt_tokens": int(usage.get("prompt_tokens") or 0),
            "completion_tokens": int(usage.get("completion_tokens") or 0),
            "prompt_cache_hit_tokens": int(usage.get("prompt_cache_hit_tokens") or 0),
        }, str(choice.get("finish_reason") or "")


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
