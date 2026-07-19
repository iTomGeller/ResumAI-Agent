from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import httpx

from app.config import settings
from app.models import TraceEvent, WorkflowResultPayload

logger = logging.getLogger(__name__)


def _headers() -> Dict[str, str]:
    return {
        "Content-Type": "application/json",
        "X-Internal-Token": settings.workflow_internal_token,
    }


def make_event_id(
    trace_id: str,
    node_id: str,
    attempt: int,
    kind: str,
    round_index: int,
    tool_call_id: str = "0",
) -> str:
    return f"{trace_id}:{node_id}:{attempt}:{kind}:{round_index}:{tool_call_id}"


async def emit_event(event: TraceEvent) -> None:
    url = f"{settings.java_backend_url}/api/internal/workflow/events"
    payload = event.model_dump()
    await _post_with_retry(url, payload, attempts=3, timeout_seconds=15.0, label="emit_event")


async def emit_result(result: WorkflowResultPayload) -> bool:
    url = f"{settings.java_backend_url}/api/internal/workflow/result"
    payload = result.model_dump()
    delivered = await _post_with_retry(
        url,
        payload,
        attempts=8,
        timeout_seconds=15.0,
        label="emit_result",
    )
    if not delivered:
        logger.critical(
            "workflow result callback exhausted retries trace=%s run=%s status=%s",
            result.traceId,
            result.workflowRunId,
            result.status,
        )
    return delivered


async def _post_with_retry(
    url: str,
    payload: Dict[str, Any],
    *,
    attempts: int,
    timeout_seconds: float,
    label: str,
) -> bool:
    """Idempotent callback delivery with bounded exponential retry.

    Event IDs and workflow run IDs make backend ingestion idempotent. Retry
    transport errors, 429, and 5xx; permanent auth/contract failures stop
    immediately so a bad deployment is visible rather than silently looping.
    """

    delay = 1.0
    async with httpx.AsyncClient(timeout=timeout_seconds) as client:
        for attempt in range(1, max(1, attempts) + 1):
            try:
                response = await client.post(url, json=payload, headers=_headers())
                if response.status_code < 400:
                    return True
                retryable = response.status_code == 429 or response.status_code >= 500
                logger.warning(
                    "%s failed attempt=%s/%s status=%s body=%s",
                    label,
                    attempt,
                    attempts,
                    response.status_code,
                    response.text[:300],
                )
                if not retryable:
                    return False
            except Exception as exc:
                logger.warning(
                    "%s transport error attempt=%s/%s: %s",
                    label,
                    attempt,
                    attempts,
                    exc,
                )
            if attempt < attempts:
                await asyncio.sleep(delay)
                delay = min(delay * 2.0, 30.0)
    return False


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def preview_text(value: Any, max_len: int = 200) -> str:
    if value is None:
        return ""
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    if len(text) <= max_len:
        return text
    return text[:max_len] + "..."


def messages_preview(messages: Optional[list]) -> str:
    if not messages:
        return ""
    parts = []
    for msg in messages[:6]:
        role = msg.get("role") or msg.get("type") or "unknown"
        content = msg.get("content") or ""
        if isinstance(content, list):
            content = json.dumps(content, ensure_ascii=False)
        parts.append(f"[{role}] {preview_text(str(content), 80)}")
    return "\n".join(parts)
