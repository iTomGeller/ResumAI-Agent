"""OpenTelemetry export to Langfuse (OTLP HTTP). Soft-fail when unset/unavailable."""

from __future__ import annotations

import base64
import logging
import os
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_tracer = None
_initialized = False


def init_otel() -> bool:
    """Configure OTLP exporter toward Langfuse. Safe to call multiple times."""
    global _tracer, _initialized
    if _initialized:
        return _tracer is not None
    _initialized = True

    endpoint = (
        os.getenv("LANGFUSE_OTEL_ENDPOINT")
        or os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
        or ""
    ).strip()
    if not endpoint:
        logger.info("[otel] LANGFUSE_OTEL_ENDPOINT / OTEL_EXPORTER_OTLP_ENDPOINT unset")
        return False

    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError:
        logger.warning("[otel] OpenTelemetry packages not installed; tracing disabled")
        return False

    public_key = os.getenv("LANGFUSE_PUBLIC_KEY", "")
    secret_key = os.getenv("LANGFUSE_SECRET_KEY", "")
    headers: Dict[str, str] = {
        "x-langfuse-ingestion-version": "4",
    }
    if public_key and secret_key:
        token = base64.b64encode(f"{public_key}:{secret_key}".encode()).decode()
        headers["Authorization"] = f"Basic {token}"

    # Langfuse expects /api/public/otel or /api/public/otel/v1/traces
    otlp_endpoint = endpoint.rstrip("/")
    if not otlp_endpoint.endswith("/v1/traces"):
        if otlp_endpoint.endswith("/otel"):
            otlp_endpoint = otlp_endpoint + "/v1/traces"
        elif "/otel" not in otlp_endpoint:
            otlp_endpoint = otlp_endpoint + "/api/public/otel/v1/traces"

    resource = Resource.create({"service.name": "resumai-workflow"})
    provider = TracerProvider(resource=resource)
    exporter = OTLPSpanExporter(endpoint=otlp_endpoint, headers=headers)
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    _tracer = trace.get_tracer("resumai.workflow", "2.0.0")
    logger.info("[otel] tracing enabled → %s", otlp_endpoint)
    return True


def start_span(name: str, attributes: Optional[Dict[str, Any]] = None) -> Any:
    init_otel()
    if _tracer is None:
        return None
    span = _tracer.start_span(name)
    for key, value in (attributes or {}).items():
        if value is None:
            continue
        try:
            span.set_attribute(key, value)
        except Exception:  # noqa: BLE001
            pass
    return span


def end_span(span: Any, *, status: str = "OK", duration_ms: int = 0,
             error: Optional[str] = None) -> None:
    if span is None:
        return
    try:
        from opentelemetry.trace import Status, StatusCode
        span.set_attribute("tool.status", status)
        if duration_ms:
            span.set_attribute("duration_ms", duration_ms)
        if error:
            span.set_attribute("error.message", error[:500])
            span.set_status(Status(StatusCode.ERROR, error[:200]))
        elif status in ("FAILED", "CANCELLED"):
            span.set_status(Status(StatusCode.ERROR, status))
        else:
            span.set_status(Status(StatusCode.OK))
        span.end()
    except Exception:  # noqa: BLE001
        pass


def current_trace_id_hex() -> Optional[str]:
    """Return the active OTel trace id (32-hex) if any."""
    try:
        from opentelemetry import trace
        span = trace.get_current_span()
        ctx = span.get_span_context() if span else None
        if ctx and ctx.is_valid:
            return format(ctx.trace_id, "032x")
    except Exception:  # noqa: BLE001
        return None
    return None
