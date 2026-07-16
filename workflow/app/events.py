from __future__ import annotations

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
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(url, json=payload, headers=_headers())
            if resp.status_code >= 400:
                logger.warning("emit_event failed %s: %s", resp.status_code, resp.text[:300])
    except Exception as exc:
        logger.warning("emit_event error: %s", exc)


async def emit_result(result: WorkflowResultPayload) -> None:
    url = f"{settings.java_backend_url}/api/internal/workflow/result"
    payload = result.model_dump()
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(url, json=payload, headers=_headers())
            if resp.status_code >= 400:
                logger.warning("emit_result failed %s: %s", resp.status_code, resp.text[:300])
    except Exception as exc:
        logger.warning("emit_result error: %s", exc)


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
