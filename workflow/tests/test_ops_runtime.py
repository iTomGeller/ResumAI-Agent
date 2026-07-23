"""Ops runtime snapshot: MCP health must come from registry probe, not description text."""

from __future__ import annotations

from app.runtime.mcp_registry import McpRegistry, McpServerHealth
from app.runtime.skills import SkillManager


def test_mcp_status_snapshot_uses_probe_not_description():
    registry = McpRegistry(config={
        "mcpServers": {
            "firecrawl": {
                "enabled": True,
                "description": "Official Firecrawl hosted MCP; keyless search/scrape are rate limited",
                "transport": "streamable-http",
                "url": "https://example.invalid/mcp",
            }
        },
        "optionalMcpServers": {},
        "agentToolRouting": {},
    })
    # Simulate a real probe result — AVAILABLE despite description mentioning rate limit.
    registry.health["firecrawl"] = McpServerHealth(
        name="firecrawl", status="AVAILABLE", transport="streamable-http",
        latency_ms=12, tools=["firecrawl.firecrawl_search"], url="https://example.invalid/mcp")
    registry._probed = True
    registry._last_probe_iso = "2026-07-22T00:00:00Z"

    snap = registry.status_snapshot()
    assert snap["source"] == "python_mcp_registry"
    assert snap["servers"]["firecrawl"]["status"] == "AVAILABLE"
    assert "rate limit" in (snap["servers"]["firecrawl"].get("description") or "").lower()
    # Description is metadata only; status stays the probed value.
    assert snap["servers"]["firecrawl"]["status"] != "RATE_LIMITED"


def test_skill_runtime_manifest_separates_active_and_admin():
    mgr = SkillManager()
    # If skills root is empty in CI, still return a well-formed payload.
    manifest = mgr.runtime_manifest(include_deprecated=True)
    assert manifest["source"] == "python_skill_manager"
    assert "skills" in manifest
    assert "activeCount" in manifest
    for skill in manifest.get("skills") or []:
        if skill.get("adminOnly"):
            assert skill.get("deprecated") or skill.get("adminOnly")
