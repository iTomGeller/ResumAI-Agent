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


class ResilientLlmClient:
    """DeepSeek chat client with connect/read/total timeouts, bounded retry
    (max 2) with exponential backoff + jitter, cancellation via asyncio,
    error classification, circuit breaker, optional fallback model and
    duration/token accounting."""

    def __init__(self, emitter: RuntimeEmitter, budget: RunBudget,
                 max_llm_calls: int, llm_timeout_seconds: int,
                 breaker: Optional[CircuitBreaker] = None) -> None:
        self.emitter = emitter
        self.budget = budget
        self.max_llm_calls = max_llm_calls
        self.llm_timeout_seconds = llm_timeout_seconds
        self.breaker = breaker or _shared_breaker
        self.base_url = normalized_deepseek_base_url()
        self.model = settings.deepseek_model or "deepseek-chat"
        self.fallback_model = os.getenv("DEEPSEEK_FALLBACK_MODEL", "").strip() or None
        self.api_key = settings.deepseek_api_key

    async def chat(self, messages: List[Dict[str, str]], *, agent_id: str,
                   purpose: str = "", max_tokens: int = 2048,
                   temperature: float = 0.2) -> str:
        if not self.api_key:
            raise LlmError("NO_API_KEY", "DeepSeek API key not configured; fail closed", False)
        if self.budget.llm_calls >= self.max_llm_calls:
            raise BudgetExceeded("maxLlmCalls", f"limit={self.max_llm_calls}")
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
        while attempts <= max_retries:
            attempts += 1
            started = time.monotonic()
            try:
                content, usage = await self._invoke(messages, model, max_tokens, temperature)
                self.breaker.record_success()
                self.budget.prompt_tokens += usage.get("prompt_tokens", 0)
                self.budget.completion_tokens += usage.get("completion_tokens", 0)
                try:
                    from app.runtime.context import calibrate, estimate_tokens
                    estimated = sum(estimate_tokens(m.get("content", ""))
                                    for m in messages)
                    calibrate(estimated, usage.get("prompt_tokens", 0))
                except Exception:  # noqa: BLE001 - calibration is best-effort
                    pass
                await self.emitter.emit("llm.completed", agent_id=agent_id, payload={
                    "model": model,
                    "durationMs": int((time.monotonic() - started) * 1000),
                    "promptTokens": usage.get("prompt_tokens", 0),
                    "completionTokens": usage.get("completion_tokens", 0),
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
            await self.emitter.emit("llm.retrying", agent_id=agent_id, payload={
                "attempt": attempts, "maxRetries": max_retries,
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
                      max_tokens: int, temperature: float) -> tuple[str, Dict[str, int]]:
        url = f"{self.base_url}/chat/completions"
        timeout = httpx.Timeout(
            connect=10.0, read=float(self.llm_timeout_seconds),
            write=30.0, pool=10.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                url,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": model,
                    "messages": messages,
                    "max_tokens": max_tokens,
                    "temperature": temperature,
                    "stream": False,
                },
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
            content = data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError) as exc:
            raise LlmError("MALFORMED_RESPONSE", str(exc), False) from exc
        usage = data.get("usage") or {}
        return content, {
            "prompt_tokens": int(usage.get("prompt_tokens") or 0),
            "completion_tokens": int(usage.get("completion_tokens") or 0),
        }


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
