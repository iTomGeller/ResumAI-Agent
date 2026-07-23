"""Unit tests for Langfuse OTel enable/disable gate."""

from __future__ import annotations

import importlib


def _reload_otel(monkeypatch, **env):
    for key in (
        "LANGFUSE_OTEL_ENDPOINT",
        "OTEL_EXPORTER_OTLP_ENDPOINT",
        "LANGFUSE_PUBLIC_KEY",
        "LANGFUSE_SECRET_KEY",
        "LANGFUSE_PUBLIC_URL",
    ):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)
    import app.runtime.otel_tracing as otel
    return importlib.reload(otel)


def test_empty_keys_disable_exporter(monkeypatch):
    otel = _reload_otel(
        monkeypatch,
        LANGFUSE_OTEL_ENDPOINT="http://langfuse-web:3000/api/public/otel",
        LANGFUSE_PUBLIC_KEY="",
        LANGFUSE_SECRET_KEY="",
        LANGFUSE_PUBLIC_URL="http://example:3001",
    )
    assert otel.init_otel() is False
    snap = otel.status_snapshot()
    assert snap["enabled"] is False
    assert snap["status"] == "AUTH_REQUIRED"
    assert otel.build_trace_url("t1") == ""


def test_missing_endpoint_disables(monkeypatch):
    otel = _reload_otel(
        monkeypatch,
        LANGFUSE_PUBLIC_KEY="pk",
        LANGFUSE_SECRET_KEY="sk",
        LANGFUSE_PUBLIC_URL="http://example:3001",
    )
    assert otel.init_otel() is False
    snap = otel.status_snapshot()
    assert snap["status"] == "DISABLED"
    assert "ENDPOINT" in snap["reason"]


def test_fully_configured_enables_without_sdk(monkeypatch):
    """When SDKs missing, still reports disabled install; with keys present
    the auth gate must not return AUTH_REQUIRED."""
    otel = _reload_otel(
        monkeypatch,
        LANGFUSE_OTEL_ENDPOINT="http://langfuse-web:3000/api/public/otel",
        LANGFUSE_PUBLIC_KEY="pk",
        LANGFUSE_SECRET_KEY="sk",
        LANGFUSE_PUBLIC_URL="http://8.138.10.189:3001",
    )

    # Force ImportError path for OTel packages if not installed in test env.
    import builtins
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name.startswith("opentelemetry"):
            raise ImportError("forced")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    assert otel.init_otel() is False
    snap = otel.status_snapshot()
    assert snap["status"] == "DISABLED"
    assert "OpenTelemetry" in snap["reason"] or "packages" in snap["reason"]
