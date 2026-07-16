from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from langfuse import Langfuse

from app.config import settings

logger = logging.getLogger(__name__)

_langfuse: Optional[Langfuse] = None


def get_langfuse() -> Optional[Langfuse]:
    global _langfuse
    if _langfuse is not None:
        return _langfuse
    if not settings.langfuse_public_key or not settings.langfuse_secret_key:
        return None
    try:
        _langfuse = Langfuse(
            public_key=settings.langfuse_public_key,
            secret_key=settings.langfuse_secret_key,
            host=settings.langfuse_host,
        )
        return _langfuse
    except Exception as exc:
        logger.warning("Langfuse init failed: %s", exc)
        return None


def is_langfuse_enabled() -> bool:
    return get_langfuse() is not None


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def start_trace(
    trace_id: str,
    user_id: str = "workflow",
    input_text: str = "",
    metadata: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    lf = get_langfuse()
    if not lf:
        return None
    try:
        trace = lf.trace(
            id=trace_id,
            user_id=user_id,
            input=input_text,
            metadata=metadata or {},
        )
        return trace.id
    except Exception as exc:
        logger.warning("Langfuse trace start failed: %s", exc)
        return None


def end_trace(trace_id: str, output_data: Any = None, metadata: Optional[Dict[str, Any]] = None) -> None:
    lf = get_langfuse()
    if not lf:
        return
    try:
        lf.trace(id=trace_id, output=output_data, metadata=metadata or {})
    except Exception as exc:
        logger.warning("Langfuse trace end failed: %s", exc)


def start_agent_span(
    trace_id: str,
    name: str,
    metadata: Optional[Dict[str, Any]] = None,
    input_data: Any = None,
    started_at: Optional[str] = None,
) -> Optional[str]:
    lf = get_langfuse()
    if not lf:
        return None
    try:
        meta = dict(metadata or {})
        if started_at:
            meta["startedAt"] = started_at
        kwargs: Dict[str, Any] = {
            "trace_id": trace_id,
            "name": name,
            "metadata": meta,
            "input": input_data,
        }
        start_time = _parse_iso(started_at)
        if start_time:
            kwargs["start_time"] = start_time
        span = lf.span(**kwargs)
        return span.id
    except Exception as exc:
        logger.warning("Langfuse agent span failed: %s", exc)
        return None


def end_span(
    observation_id: Optional[str],
    output_data: Any = None,
    ended_at: Optional[str] = None,
    status: str = "SUCCESS",
    metadata: Optional[Dict[str, Any]] = None,
    duration_ms: Optional[int] = None,
) -> None:
    lf = get_langfuse()
    if not lf or not observation_id:
        return
    try:
        meta = dict(metadata or {})
        if ended_at:
            meta["endedAt"] = ended_at
        if duration_ms is not None:
            meta["durationMs"] = duration_ms
        meta["status"] = status
        kwargs: Dict[str, Any] = {
            "id": observation_id,
            "output": output_data,
            "metadata": meta,
        }
        end_time = _parse_iso(ended_at)
        if end_time:
            kwargs["end_time"] = end_time
        lf.span(**kwargs)
    except Exception as exc:
        logger.warning("Langfuse span end failed: %s", exc)


def start_generation(
    trace_id: str,
    name: str,
    model: str,
    input_messages: Any,
    metadata: Optional[Dict[str, Any]] = None,
    parent_observation_id: Optional[str] = None,
    started_at: Optional[str] = None,
) -> Optional[str]:
    lf = get_langfuse()
    if not lf:
        return None
    try:
        meta = dict(metadata or {})
        if started_at:
            meta["startedAt"] = started_at
        kwargs: Dict[str, Any] = {
            "trace_id": trace_id,
            "name": name,
            "model": model,
            "input": input_messages,
            "metadata": meta,
        }
        if parent_observation_id:
            kwargs["parent_observation_id"] = parent_observation_id
        start_time = _parse_iso(started_at)
        if start_time:
            kwargs["start_time"] = start_time
        gen = lf.generation(**kwargs)
        return gen.id
    except Exception as exc:
        logger.warning("Langfuse generation failed: %s", exc)
        return None


def end_generation(
    observation_id: str,
    output: Any,
    usage: Optional[Dict[str, Any]] = None,
    ended_at: Optional[str] = None,
    duration_ms: Optional[int] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> None:
    lf = get_langfuse()
    if not lf or not observation_id:
        return
    try:
        meta = dict(metadata or {})
        if ended_at:
            meta["endedAt"] = ended_at
        if duration_ms is not None:
            meta["durationMs"] = duration_ms
        kwargs: Dict[str, Any] = {
            "id": observation_id,
            "output": output,
            "usage": usage,
            "metadata": meta,
        }
        end_time = _parse_iso(ended_at)
        if end_time:
            kwargs["end_time"] = end_time
        lf.generation(**kwargs)
    except Exception as exc:
        logger.warning("Langfuse generation end failed: %s", exc)


def record_generation(
    trace_id: str,
    name: str,
    model: str,
    input_messages: Any,
    output: Any,
    usage: Optional[Dict[str, Any]] = None,
    metadata: Optional[Dict[str, Any]] = None,
    parent_observation_id: Optional[str] = None,
    started_at: Optional[str] = None,
    ended_at: Optional[str] = None,
    duration_ms: Optional[int] = None,
) -> Optional[str]:
    lf = get_langfuse()
    if not lf:
        return None
    try:
        meta = dict(metadata or {})
        if started_at:
            meta["startedAt"] = started_at
        if ended_at:
            meta["endedAt"] = ended_at
        if duration_ms is not None:
            meta["durationMs"] = duration_ms
        kwargs: Dict[str, Any] = {
            "trace_id": trace_id,
            "name": name,
            "model": model,
            "input": input_messages,
            "output": output,
            "usage": usage,
            "metadata": meta,
        }
        if parent_observation_id:
            kwargs["parent_observation_id"] = parent_observation_id
        start_time = _parse_iso(started_at)
        end_time = _parse_iso(ended_at)
        if start_time:
            kwargs["start_time"] = start_time
        if end_time:
            kwargs["end_time"] = end_time
        gen = lf.generation(**kwargs)
        return gen.id
    except Exception as exc:
        logger.warning("Langfuse generation record failed: %s", exc)
        return None


def action_span(
    trace_id: str,
    tool_name: str,
    input_data: Any,
    output_data: Any,
    metadata: Optional[Dict[str, Any]] = None,
    parent_observation_id: Optional[str] = None,
    started_at: Optional[str] = None,
    ended_at: Optional[str] = None,
    duration_ms: Optional[int] = None,
) -> Optional[str]:
    lf = get_langfuse()
    if not lf:
        return None
    try:
        meta = dict(metadata or {})
        meta["toolName"] = tool_name
        if started_at:
            meta["startedAt"] = started_at
        if ended_at:
            meta["endedAt"] = ended_at
        if duration_ms is not None:
            meta["durationMs"] = duration_ms
        kwargs: Dict[str, Any] = {
            "trace_id": trace_id,
            "name": f"tool.{tool_name}",
            "input": input_data,
            "output": output_data,
            "metadata": meta,
        }
        if parent_observation_id:
            kwargs["parent_observation_id"] = parent_observation_id
        start_time = _parse_iso(started_at)
        end_time = _parse_iso(ended_at)
        if start_time and duration_ms is not None and (not end_time or end_time <= start_time):
            end_time = start_time + timedelta(milliseconds=max(duration_ms, 1))
        if start_time:
            kwargs["start_time"] = start_time
        if end_time:
            kwargs["end_time"] = end_time
        span = lf.span(**kwargs)
        return span.id
    except Exception as exc:
        logger.warning("Langfuse action span failed: %s", exc)
        return None


def tool_span(
    trace_id: str,
    name: str,
    input_data: Any,
    output_data: Any,
    metadata: Optional[Dict[str, Any]] = None,
    parent_observation_id: Optional[str] = None,
    started_at: Optional[str] = None,
    ended_at: Optional[str] = None,
    duration_ms: Optional[int] = None,
) -> Optional[str]:
    return action_span(
        trace_id=trace_id,
        tool_name=name,
        input_data=input_data,
        output_data=output_data,
        metadata=metadata,
        parent_observation_id=parent_observation_id,
        started_at=started_at,
        ended_at=ended_at,
        duration_ms=duration_ms,
    )


def flush() -> None:
    lf = get_langfuse()
    if lf:
        try:
            lf.flush()
        except Exception:
            pass
