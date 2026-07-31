from __future__ import annotations

import json
from pathlib import Path


def _config() -> dict:
    here = Path(__file__).resolve()
    candidates = [
        here.parents[2] / "config" / "mcp-servers.json",
        here.parents[1] / "config" / "mcp-servers.json",
    ]
    path = next((candidate for candidate in candidates if candidate.is_file()), None)
    assert path is not None, f"MCP config not found in {candidates}"
    return json.loads(path.read_text(encoding="utf-8"))


def test_default_mcp_servers_are_real_and_enabled() -> None:
    config = _config()
    servers = config["mcpServers"]

    assert set(servers) == {"exa", "microsoft-learn", "fetch"}
    assert all(server["enabled"] for server in servers.values())
    assert servers["exa"]["url"].startswith("https://mcp.exa.ai/mcp")
    assert (
        servers["microsoft-learn"]["url"]
        == "https://learn.microsoft.com/api/mcp"
    )
    assert servers["fetch"]["transport"] == "stdio"
    assert config["evidencePolicy"]["allowSyntheticFallback"] is False


def test_production_inventory_is_keyless_only() -> None:
    config = _config()

    assert config["optionalMcpServers"] == {}
    serialized = json.dumps(config, ensure_ascii=False).lower()
    for forbidden in (
            "oauth", "required-env", "requiredenv", "api_key", "apikey",
            "authorization", "bearer ${", "githubcopilot"):
        assert forbidden not in serialized


def test_routes_never_reference_removed_synthetic_cn_web_tools() -> None:
    config = _config()
    routed = {
        tool
        for tools in config["agentToolRouting"].values()
        for tool in tools
    }

    assert not any(tool.startswith("cn-web.") for tool in routed)
    assert "mcp_fetch_url" not in routed
    assert "exa.web_search_exa" in routed
    assert "microsoft-learn.microsoft_docs_search" in routed
    assert not any(tool.startswith(("context7.", "deepwiki."))
                   for tool in routed)
    assert not any(tool.endswith(("microsoft_docs_fetch",
                                  "microsoft_code_sample_search"))
                   for tool in routed)
    assert not any(tool.startswith(prefix) for tool in routed for prefix in (
        "tavily.", "firecrawl.", "github.", "brave-search."))


def test_backend_classpath_fallback_matches_the_shared_runtime_config() -> None:
    workflow_root = Path(__file__).resolve().parents[1]
    root = (
        workflow_root.parent
        if (workflow_root.parent / "config" / "mcp-servers.json").is_file()
        else workflow_root
    )
    shared = json.loads(
        (root / "config" / "mcp-servers.json").read_text(encoding="utf-8"))
    classpath = json.loads(
        (root / "backend" / "src" / "main" / "resources"
         / "mcp-servers.json").read_text(encoding="utf-8"))

    assert classpath == shared
