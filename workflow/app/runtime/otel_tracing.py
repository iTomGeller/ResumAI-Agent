"""OpenTelemetry export to Langfuse (OTLP HTTP). Soft-fail when unset/unavailable.

Exporter enables only when endpoint + public key + secret key are all present.
Empty keys with a default endpoint would otherwise send Basic ":" and 401.
"""

from __future__ import annotations

import base64
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

_tracer = None
_initialized = False
_status: Dict[str, Any] = {
    "enabled": False,
    "status": "DISABLED",
    "reason": "not initialized",
    "endpoint": "",
    "publicUrlConfigured": False,
    "publicUrl": "",
    "authConfigured": False,
    "externalLinksEnabled": False,
    "lastSuccessAt": None,
    "lastErrorAt": None,
    "lastError": None,
}


def _trim(value: Optional[str]) -> str:
    return (value or "").strip()


def _redact_endpoint(endpoint: str) -> str:
    if not endpoint:
        return ""
    try:
        uri = urlparse(endpoint)
        if not uri.hostname:
            return endpoint[:48] + ("…" if len(endpoint) > 48 else "")
        netloc = uri.hostname
        if uri.port:
            netloc = f"{netloc}:{uri.port}"
        return f"{uri.scheme}://{netloc}{uri.path or ''}"
    except Exception:  # noqa: BLE001
        return "***"


def _valid_public_url(public_url: str) -> bool:
    lower = public_url.lower()
    return lower.startswith("http://") or lower.startswith("https://")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _set_status(**kwargs: Any) -> None:
    _status.update(kwargs)


def init_otel() -> bool:
    """Configure OTLP exporter toward Langfuse. Safe to call multiple times."""
    global _tracer, _initialized
    if _initialized:
        return _tracer is not None
    _initialized = True

    endpoint = _trim(
        os.getenv("LANGFUSE_OTEL_ENDPOINT") or os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
    )
    public_key = _trim(os.getenv("LANGFUSE_PUBLIC_KEY"))
    secret_key = _trim(os.getenv("LANGFUSE_SECRET_KEY"))
    public_url = _trim(os.getenv("LANGFUSE_PUBLIC_URL"))
    auth_ok = bool(public_key and secret_key)
    public_ok = _valid_public_url(public_url)

    _set_status(
        endpoint=_redact_endpoint(endpoint),
        publicUrlConfigured=public_ok,
        publicUrl=public_url if public_ok else "",
        authConfigured=auth_ok,
        externalLinksEnabled=False,
    )

    if not endpoint and not auth_ok:
        reason = "DISABLED: Langfuse not configured (endpoint + keys required)"
        _set_status(enabled=False, status="DISABLED", reason=reason)
        logger.info("[otel] %s", reason)
        return False

    if endpoint and not auth_ok:
        reason = (
            "AUTH_REQUIRED: LANGFUSE_PUBLIC_KEY/SECRET_KEY missing; "
            "exporter disabled to avoid 401"
        )
        _set_status(enabled=False, status="AUTH_REQUIRED", reason=reason)
        logger.info("[otel] %s", reason)
        return False

    if not endpoint and auth_ok:
        reason = "DISABLED: LANGFUSE_OTEL_ENDPOINT (or OTEL_EXPORTER_OTLP_ENDPOINT) not set"
        _set_status(enabled=False, status="DISABLED", reason=reason)
        logger.info("[otel] %s", reason)
        return False

    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError:
        reason = "DISABLED: OpenTelemetry packages not installed"
        _set_status(enabled=False, status="DISABLED", reason=reason)
        logger.warning("[otel] %s", reason)
        return False

    headers: Dict[str, str] = {
        "x-langfuse-ingestion-version": "4",
        "Authorization": "Basic "
        + base64.b64encode(f"{public_key}:{secret_key}".encode()).decode(),
    }

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

    reason = (
        "Langfuse exporter 已启用"
        if public_ok
        else "Langfuse exporter 已启用，但 LANGFUSE_PUBLIC_URL 未配置，无法生成外链"
    )
    _set_status(
        enabled=True,
        status="READY",
        reason=reason,
        endpoint=_redact_endpoint(otlp_endpoint),
        externalLinksEnabled=public_ok,
        lastSuccessAt=_now_iso(),
    )
    logger.info("[otel] tracing enabled → %s", _redact_endpoint(otlp_endpoint))
    return True


def record_export_success() -> None:
    _set_status(
        lastSuccessAt=_now_iso(),
        lastError=None,
        status="READY" if _status.get("enabled") else _status.get("status"),
    )


def record_export_error(message: str) -> None:
    msg = (message or "export failed")[:300]
    lower = msg.lower()
    status = "AUTH_FAILED" if any(
        token in lower for token in ("401", "403", "unauthorized", "auth")
    ) else "UNREACHABLE"
    _set_status(
        lastErrorAt=_now_iso(),
        lastError=msg,
        status=status,
        externalLinksEnabled=False,
    )


def status_snapshot() -> Dict[str, Any]:
    """Ops / health snapshot (safe to call before init)."""
    if not _initialized:
        # Reflect env without starting exporter side effects beyond init_otel.
        init_otel()
    return dict(_status)


def build_trace_url(trace_id: str) -> str:
    snap = status_snapshot()
    if not snap.get("externalLinksEnabled") or snap.get("status") != "READY":
        return ""
    public_url = _trim(str(snap.get("publicUrl") or ""))
    if not _valid_public_url(public_url) or not _trim(trace_id):
        return ""
    base = public_url.rstrip("/")
    return f"{base}/project/resumai-project/traces/{trace_id}"


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
        record_export_success()
    except Exception as exc:  # noqa: BLE001
        record_export_error(str(exc))


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
