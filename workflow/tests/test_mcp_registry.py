from __future__ import annotations

import asyncio
import importlib
import json
import sys
import types
from pathlib import Path

import pytest


@pytest.fixture
def registry(monkeypatch: pytest.MonkeyPatch):
    workflow_root = Path(__file__).resolve().parents[1]
    monkeypatch.syspath_prepend(str(workflow_root))

    fake_config = types.ModuleType("app.config")
    fake_config.settings = types.SimpleNamespace(mcp_config_path="mcp-servers.json")
    monkeypatch.setitem(sys.modules, "app.config", fake_config)
    sys.modules.pop("app.mcp_registry", None)
    module = importlib.import_module("app.mcp_registry")
    module.reset_mcp_registry_cache()
    return module


def test_runtime_config_filters_metadata_and_resolves_headers(registry, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    config = {
        "enabled": "auto",
        "default": False,
        "requiredEnv": ["GITHUB_TOKEN"],
        "transport": "streamable-http",
        "url": "https://api.githubcopilot.com/mcp/",
        "headers": {"X-MCP-Readonly": "true"},
        "headersFromEnv": {
            "Authorization": {"env": "GITHUB_TOKEN", "prefix": "Bearer "}
        },
        "evidence": {"provider": "github"},
    }

    runtime = registry._runtime_config("github", config)

    assert runtime == {
        "transport": "streamable_http",
        "url": "https://api.githubcopilot.com/mcp/",
        "headers": {
            "X-MCP-Readonly": "true",
            "Authorization": "Bearer test-token",
        },
    }
    assert "enabled" not in runtime
    assert "requiredEnv" not in runtime
    assert "evidence" not in runtime


def test_discovery_isolates_optional_servers_and_attaches_evidence(
    registry,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    config_path = tmp_path / "mcp-servers.json"
    config_path.write_text(
        json.dumps(
            {
                "evidencePolicy": {"allowSyntheticFallback": False},
                "mcpServers": {
                    "ready": {
                        "enabled": True,
                        "default": True,
                        "transport": "streamable_http",
                        "url": "https://example.test/mcp",
                        "evidence": {"provider": "real-provider", "requiresSourceUrl": True},
                    },
                    "missing-credential": {
                        "enabled": "auto",
                        "requiredEnv": ["MISSING_MCP_TOKEN"],
                        "transport": "streamable_http",
                        "url": "https://example.test/private-mcp",
                    },
                    "optional-off": {
                        "enabled": False,
                        "transport": "stdio",
                        "command": "does-not-run",
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    registry.settings.mcp_config_path = str(config_path)
    monkeypatch.delenv("MISSING_MCP_TOKEN", raising=False)

    class FakeTool:
        name = "real_search"
        description = "Returns source-backed search results"
        metadata = None

    class FakeClient:
        def __init__(self, servers):
            assert list(servers) == ["ready"]

        async def get_tools(self):
            return [FakeTool()]

    adapter_package = types.ModuleType("langchain_mcp_adapters")
    adapter_client = types.ModuleType("langchain_mcp_adapters.client")
    adapter_client.MultiServerMCPClient = FakeClient
    monkeypatch.setitem(sys.modules, "langchain_mcp_adapters", adapter_package)
    monkeypatch.setitem(sys.modules, "langchain_mcp_adapters.client", adapter_client)

    tools = asyncio.run(registry.get_mcp_tools())
    status = registry.get_mcp_status()

    assert [tool.name for tool in tools] == ["real_search"]
    assert tools[0].metadata["externalEvidence"]["provider"] == "real-provider"
    assert tools[0].metadata["evidencePolicy"]["allowSyntheticFallback"] is False
    assert "never invent a fallback" in tools[0].description
    assert status["ready"]["status"] == "available"
    assert status["missing-credential"]["status"] == "unavailable"
    assert status["missing-credential"]["reason"] == "missing_environment"
    assert status["optional-off"]["status"] == "disabled"


def test_concurrent_discovery_is_single_flight(
    registry,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    config_path = tmp_path / "mcp-servers.json"
    config_path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "ready": {
                        "enabled": True,
                        "transport": "streamable_http",
                        "url": "https://example.test/mcp",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    registry.settings.mcp_config_path = str(config_path)
    calls = 0

    class FakeTool:
        name = "real_search"
        description = "Returns source-backed search results"
        metadata = None

    class FakeClient:
        def __init__(self, servers):
            assert list(servers) == ["ready"]

        async def get_tools(self):
            nonlocal calls
            calls += 1
            await asyncio.sleep(0.01)
            return [FakeTool()]

    adapter_package = types.ModuleType("langchain_mcp_adapters")
    adapter_client = types.ModuleType("langchain_mcp_adapters.client")
    adapter_client.MultiServerMCPClient = FakeClient
    monkeypatch.setitem(sys.modules, "langchain_mcp_adapters", adapter_package)
    monkeypatch.setitem(sys.modules, "langchain_mcp_adapters.client", adapter_client)

    async def discover_from_parallel_nodes():
        return await asyncio.gather(*(registry.get_mcp_tools() for _ in range(6)))

    results = asyncio.run(discover_from_parallel_nodes())

    assert calls == 1
    assert [[tool.name for tool in tools] for tools in results] == [["real_search"]] * 6


def test_enabled_servers_discover_in_parallel_and_fail_independently(
    registry,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    config_path = tmp_path / "mcp-servers.json"
    config_path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    name: {
                        "enabled": True,
                        "transport": "streamable_http",
                        "url": f"https://{name}.example.test/mcp",
                    }
                    for name in ("first", "second", "broken")
                }
            }
        ),
        encoding="utf-8",
    )
    registry.settings.mcp_config_path = str(config_path)

    class FakeTool:
        description = "source-backed result"
        metadata = None

        def __init__(self, name: str):
            self.name = name

    entered: set[str] = set()
    both_ready = None

    class FakeClient:
        def __init__(self, servers):
            self.server_id = next(iter(servers))

        async def get_tools(self):
            nonlocal both_ready
            if both_ready is None:
                both_ready = asyncio.Event()
            if self.server_id == "broken":
                raise RuntimeError("provider offline")
            entered.add(self.server_id)
            if entered == {"first", "second"}:
                both_ready.set()
            await asyncio.wait_for(both_ready.wait(), timeout=0.2)
            return [FakeTool(f"{self.server_id}_search")]

    adapter_package = types.ModuleType("langchain_mcp_adapters")
    adapter_client = types.ModuleType("langchain_mcp_adapters.client")
    adapter_client.MultiServerMCPClient = FakeClient
    monkeypatch.setitem(sys.modules, "langchain_mcp_adapters", adapter_package)
    monkeypatch.setitem(sys.modules, "langchain_mcp_adapters.client", adapter_client)

    tools = asyncio.run(registry.get_mcp_tools())
    status = registry.get_mcp_status()

    assert {tool.name for tool in tools} == {"first_search", "second_search"}
    assert status["first"]["status"] == "available"
    assert status["second"]["status"] == "available"
    assert status["broken"]["status"] == "unavailable"
    assert status["broken"]["reason"] == "connection_or_discovery_failed"


def test_internal_tools_are_not_mislabeled_as_external_evidence(registry):
    class InternalTool:
        name = "resume_parse"
        description = "Parses the submitted resume"
        metadata = None

    tool = InternalTool()
    registry._annotate_tools(
        "resume-tools",
        {"enabled": True, "transport": "stdio"},
        {"allowSyntheticFallback": False},
        [tool],
    )

    assert tool.metadata == {"mcpServer": "resume-tools"}
    assert tool.description == "Parses the submitted resume"


def test_bundled_config_uses_real_public_endpoints():
    config_path = Path(__file__).resolve().parents[1] / "mcp-servers.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))

    assert config["evidencePolicy"]["allowSyntheticFallback"] is False
    assert config["mcpServers"]["exa"]["url"].startswith("https://mcp.exa.ai/mcp")
    assert config["mcpServers"]["firecrawl"]["url"] == "https://mcp.firecrawl.dev/v2/mcp"
    assert config["mcpServers"]["github"]["url"] == "https://api.githubcopilot.com/mcp/"
    assert config["mcpServers"]["github"]["requiredEnv"] == ["GITHUB_TOKEN"]
    assert config["mcpServers"]["fetch"]["enabled"] is False
