from __future__ import annotations

"""MCP registry: Streamable HTTP clients + stdio fetch fallback + health probe.

Only tools that survive initialize + tools/list enter the Agent catalog.
GitHub Official MCP without GITHUB_TOKEN is reported as AUTH_REQUIRED and is
never silently treated as AVAILABLE.
"""

import asyncio
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx

from app.runtime.mcp_client import FETCH_ALLOWED_HOSTS, McpError, McpStdioClient, host_allowed

logger = logging.getLogger(__name__)

HealthStatus = str  # AVAILABLE | RATE_LIMITED | AUTH_REQUIRED | DOWN
PROTOCOL_VERSION = "2024-11-05"
DEFAULT_TIMEOUT = 25.0
CIRCUIT_FAILURES = 3
CIRCUIT_COOLDOWN_S = 60.0

# Repo-root-relative + container fallbacks. Never evaluate parents[N] at import
# time — Docker WORKDIR paths can be shallower than the monorepo layout.


def _safe_parent_join(levels: int, *parts: str) -> Optional[Path]:
    try:
        base = Path(__file__).resolve()
        for _ in range(levels):
            base = base.parent
        return base.joinpath(*parts)
    except Exception:  # noqa: BLE001
        return None


def _config_candidates() -> List[Path]:
    env_path = os.getenv("MCP_CONFIG_PATH", "").strip()
    out: List[Path] = []
    if env_path:
        out.append(Path(env_path))
    out.append(Path("/app/config/mcp-servers.json"))
    for levels in (3, 4, 2):
        candidate = _safe_parent_join(levels, "config", "mcp-servers.json")
        if candidate is not None:
            out.append(candidate)
    out.extend([
        Path("/app/mcp-servers.json"),
        Path("config/mcp-servers.json"),
        Path("backend/src/main/resources/mcp-servers.json"),
        Path("mcp-servers.json"),
    ])
    return out


def resolve_mcp_config_path() -> Optional[Path]:
    for candidate in _config_candidates():
        if candidate and str(candidate) and candidate.is_file():
            return candidate
    return None


def load_mcp_config() -> Dict[str, Any]:
    path = resolve_mcp_config_path()
    if path is None:
        logger.warning("mcp-servers.json not found; MCP catalog will be empty")
        return {"mcpServers": {}, "optionalMcpServers": {}, "agentToolRouting": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        logger.info("loaded MCP config from %s", path)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("failed to load MCP config %s: %s", path, exc)
        return {"mcpServers": {}, "optionalMcpServers": {}, "agentToolRouting": {}}


def _expand_env(value: str) -> str:
    def repl(match: re.Match[str]) -> str:
        return os.getenv(match.group(1), "")
    return re.sub(r"\$\{([A-Z0-9_]+)\}", repl, value)


def _expand_headers(raw: Dict[str, Any]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for key, value in (raw or {}).items():
        out[str(key)] = _expand_env(str(value))
    return out


@dataclass
class McpToolInfo:
    server: str
    name: str                 # remote tool name (e.g. web_search_exa)
    catalog_name: str         # registered name (e.g. exa.web_search_exa)
    description: str
    input_schema: Dict[str, Any] = field(default_factory=dict)
    protocol_version: str = PROTOCOL_VERSION


@dataclass
class McpServerHealth:
    name: str
    status: HealthStatus
    transport: str
    latency_ms: int = 0
    tools: List[str] = field(default_factory=list)
    error: str = ""
    url: str = ""


class StreamableHttpMcpClient:
    """Official Streamable HTTP MCP: initialize → notifications/initialized →
    tools/list | tools/call. Handles JSON and SSE responses, session header,
    timeouts and a simple circuit breaker."""

    def __init__(self, name: str, url: str, *, headers: Optional[Dict[str, str]] = None,
                 request_timeout: float = DEFAULT_TIMEOUT) -> None:
        self.name = name
        self.url = url
        self.headers = dict(headers or {})
        self.request_timeout = request_timeout
        self.session_id: Optional[str] = None
        self.protocol_version = PROTOCOL_VERSION
        self._next_id = 0
        self._initialized = False
        self._lock = asyncio.Lock()
        self._fail_count = 0
        self._circuit_open_until = 0.0

    def _circuit_blocked(self) -> bool:
        return time.monotonic() < self._circuit_open_until

    def _record_failure(self) -> None:
        self._fail_count += 1
        if self._fail_count >= CIRCUIT_FAILURES:
            self._circuit_open_until = time.monotonic() + CIRCUIT_COOLDOWN_S
            logger.warning("MCP %s circuit open for %ss", self.name, CIRCUIT_COOLDOWN_S)

    def _record_success(self) -> None:
        self._fail_count = 0
        self._circuit_open_until = 0.0

    def _next_request_id(self) -> int:
        self._next_id += 1
        return self._next_id

    def _base_headers(self) -> Dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            **self.headers,
        }
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        return headers

    @staticmethod
    def _parse_body(response: httpx.Response) -> Dict[str, Any]:
        ctype = (response.headers.get("content-type") or "").lower()
        text = response.text or ""
        if "text/event-stream" in ctype or text.lstrip().startswith("event:"):
            return StreamableHttpMcpClient._parse_sse(text)
        if not text.strip():
            return {}
        try:
            parsed = json.loads(text)
            return parsed if isinstance(parsed, dict) else {"result": parsed}
        except json.JSONDecodeError:
            return StreamableHttpMcpClient._parse_sse(text)

    @staticmethod
    def _parse_sse(text: str) -> Dict[str, Any]:
        """Extract the last JSON-RPC payload from an SSE stream."""
        data_lines: List[str] = []
        last_payload: Dict[str, Any] = {}
        for line in text.splitlines():
            if line.startswith("data:"):
                data_lines.append(line[5:].lstrip())
            elif not line.strip() and data_lines:
                blob = "\n".join(data_lines)
                data_lines = []
                try:
                    payload = json.loads(blob)
                    if isinstance(payload, dict):
                        last_payload = payload
                except json.JSONDecodeError:
                    continue
        if data_lines:
            try:
                payload = json.loads("\n".join(data_lines))
                if isinstance(payload, dict):
                    last_payload = payload
            except json.JSONDecodeError:
                pass
        return last_payload

    async def _post(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        if self._circuit_blocked():
            raise McpError(f"MCP {self.name} circuit open")
        try:
            async with httpx.AsyncClient(timeout=self.request_timeout) as client:
                response = await client.post(
                    self.url, json=payload, headers=self._base_headers())
            session = response.headers.get("mcp-session-id") \
                or response.headers.get("Mcp-Session-Id")
            if session:
                self.session_id = session
            if response.status_code == 401 or response.status_code == 403:
                self._record_failure()
                raise McpError(f"AUTH_REQUIRED status={response.status_code}")
            if response.status_code == 429:
                self._record_failure()
                raise McpError(f"RATE_LIMITED status=429 body={response.text[:200]}")
            if response.status_code >= 400:
                self._record_failure()
                raise McpError(
                    f"HTTP {response.status_code}: {response.text[:300]}")
            message = self._parse_body(response)
            if "error" in message:
                err = message["error"]
                self._record_failure()
                raise McpError(str(err)[:300])
            self._record_success()
            return message.get("result") if "result" in message else message
        except McpError:
            raise
        except Exception as exc:  # noqa: BLE001
            self._record_failure()
            raise McpError(f"MCP {self.name} transport: {exc}") from exc

    async def initialize(self) -> Dict[str, Any]:
        async with self._lock:
            result = await self._post({
                "jsonrpc": "2.0",
                "id": self._next_request_id(),
                "method": "initialize",
                "params": {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "resumai-runtime", "version": "1.0"},
                },
            })
            if isinstance(result, dict) and result.get("protocolVersion"):
                self.protocol_version = str(result["protocolVersion"])
            # notifications/initialized (no id)
            try:
                async with httpx.AsyncClient(timeout=self.request_timeout) as client:
                    await client.post(
                        self.url,
                        json={"jsonrpc": "2.0",
                              "method": "notifications/initialized",
                              "params": {}},
                        headers=self._base_headers())
            except Exception as exc:  # noqa: BLE001 - notification best-effort
                logger.debug("MCP %s initialized notify skipped: %s", self.name, exc)
            self._initialized = True
            return result if isinstance(result, dict) else {}

    async def list_tools(self) -> List[Dict[str, Any]]:
        if not self._initialized:
            await self.initialize()
        result = await self._post({
            "jsonrpc": "2.0",
            "id": self._next_request_id(),
            "method": "tools/list",
            "params": {},
        })
        tools = result.get("tools") if isinstance(result, dict) else None
        return tools if isinstance(tools, list) else []

    async def call_tool(self, tool: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        if not self._initialized:
            await self.initialize()
        result = await self._post({
            "jsonrpc": "2.0",
            "id": self._next_request_id(),
            "method": "tools/call",
            "params": {"name": tool, "arguments": arguments or {}},
        })
        return self._normalize_tool_result(result if isinstance(result, dict) else {})

    @staticmethod
    def _normalize_tool_result(result: Dict[str, Any]) -> Dict[str, Any]:
        texts: List[str] = []
        for item in result.get("content") or []:
            if isinstance(item, dict) and item.get("type") == "text":
                texts.append(str(item.get("text") or ""))
        text = "\n".join(texts)[:12000]
        structured = result.get("structuredContent")
        out: Dict[str, Any] = {
            "success": not result.get("isError", False),
            "text": text,
            "isError": bool(result.get("isError", False)),
        }
        if isinstance(structured, dict):
            out["structuredContent"] = structured
        # Prefer parsing JSON text for search results that include URLs.
        if text.strip().startswith("{") or text.strip().startswith("["):
            try:
                out["parsed"] = json.loads(text)
            except json.JSONDecodeError:
                pass
        return out


class McpRegistry:
    """Loads config, probes servers, and exposes only healthy tools."""

    def __init__(self, config: Optional[Dict[str, Any]] = None) -> None:
        self.config = config or load_mcp_config()
        self.health: Dict[str, McpServerHealth] = {}
        self.tools: Dict[str, McpToolInfo] = {}
        self._http_clients: Dict[str, StreamableHttpMcpClient] = {}
        self._stdio_clients: Dict[str, McpStdioClient] = {}
        self.agent_routing: Dict[str, List[str]] = {
            str(k): list(v) for k, v in
            (self.config.get("agentToolRouting") or {}).items()
            if isinstance(v, list)
        }
        self._probed = False

    async def probe_all(self, *, force: bool = False) -> Dict[str, McpServerHealth]:
        if self._probed and not force:
            return self.health
        self.health.clear()
        self.tools.clear()

        servers = dict(self.config.get("mcpServers") or {})
        optional = dict(self.config.get("optionalMcpServers") or {})

        # Optional servers: still probe for status visibility, but only register
        # tools when enabled AND env requirements are met.
        for name, cfg in {**servers, **optional}.items():
            if not isinstance(cfg, dict):
                continue
            is_optional = name in optional
            await self._probe_server(name, cfg, optional=is_optional)

        self._probed = True
        summary = {k: v.status for k, v in self.health.items()}
        logger.info("MCP health probe: %s (%d tools)", summary, len(self.tools))
        return self.health

    async def _probe_server(self, name: str, cfg: Dict[str, Any], *,
                            optional: bool) -> None:
        transport = str(cfg.get("transport") or "streamable-http")
        url = str(cfg.get("url") or "")
        required_env = [str(e) for e in (cfg.get("requiredEnv") or [])]
        missing = [e for e in required_env if not (os.getenv(e) or "").strip()]
        enabled = bool(cfg.get("enabled", True))

        # GitHub / AUTH_REQUIRED: never pretend AVAILABLE without token.
        if missing:
            status = str(cfg.get("healthStatusWhenMissingEnv") or "AUTH_REQUIRED")
            self.health[name] = McpServerHealth(
                name=name, status=status, transport=transport, url=url,
                error=f"missing env: {','.join(missing)}")
            return

        if optional and not enabled:
            self.health[name] = McpServerHealth(
                name=name, status="AUTH_REQUIRED" if required_env else "DOWN",
                transport=transport, url=url,
                error="optional server disabled")
            return

        if not enabled:
            self.health[name] = McpServerHealth(
                name=name, status="DOWN", transport=transport, url=url,
                error="disabled in config")
            return

        allowed = set(cfg.get("allowedTools") or [])
        prefix = str(cfg.get("toolPrefix") or name)
        started = time.monotonic()
        try:
            if transport == "stdio":
                tools = await self._probe_stdio(name, cfg, allowed, prefix)
            else:
                tools = await self._probe_http(name, cfg, allowed, prefix, url)
            latency = int((time.monotonic() - started) * 1000)
            self.health[name] = McpServerHealth(
                name=name, status="AVAILABLE", transport=transport,
                latency_ms=latency, url=url,
                tools=[t.catalog_name for t in tools])
            for tool in tools:
                self.tools[tool.catalog_name] = tool
        except McpError as exc:
            latency = int((time.monotonic() - started) * 1000)
            msg = str(exc)
            if "AUTH_REQUIRED" in msg:
                status: HealthStatus = "AUTH_REQUIRED"
            elif "RATE_LIMITED" in msg or "429" in msg:
                status = "RATE_LIMITED"
            else:
                status = "DOWN"
            self.health[name] = McpServerHealth(
                name=name, status=status, transport=transport,
                latency_ms=latency, url=url, error=msg[:300])
            logger.warning("MCP probe %s -> %s: %s", name, status, msg[:200])
        except Exception as exc:  # noqa: BLE001
            latency = int((time.monotonic() - started) * 1000)
            self.health[name] = McpServerHealth(
                name=name, status="DOWN", transport=transport,
                latency_ms=latency, url=url, error=str(exc)[:300])
            logger.warning("MCP probe %s failed: %s", name, exc)

    async def _probe_http(self, name: str, cfg: Dict[str, Any],
                          allowed: set, prefix: str, url: str
                          ) -> List[McpToolInfo]:
        headers = _expand_headers(cfg.get("headers") or {})
        client = StreamableHttpMcpClient(name, url, headers=headers)
        await client.initialize()
        remote_tools = await client.list_tools()
        self._http_clients[name] = client
        return self._filter_tools(name, remote_tools, allowed, prefix,
                                  client.protocol_version)

    async def _probe_stdio(self, name: str, cfg: Dict[str, Any],
                           allowed: set, prefix: str) -> List[McpToolInfo]:
        command = str(cfg.get("command") or "python")
        args = [str(a) for a in (cfg.get("args") or [])]
        client = McpStdioClient(name, [command, *args])
        # list tools via initialize path already in call_tool ensure; do a
        # lightweight initialize + tools/list through private API.
        await client._ensure_started()  # noqa: SLF001 - shared stdio probe
        result = await client._request("tools/list", {})  # noqa: SLF001
        remote_tools = result.get("tools") if isinstance(result, dict) else []
        if not isinstance(remote_tools, list):
            remote_tools = []
        self._stdio_clients[name] = client
        # Whitelist-only fetch: advertise mcp_fetch_url + fetch.fetch if fetch exists.
        return self._filter_tools(name, remote_tools, allowed, prefix, PROTOCOL_VERSION)

    @staticmethod
    def _filter_tools(server: str, remote_tools: List[Any], allowed: set,
                      prefix: str, protocol_version: str) -> List[McpToolInfo]:
        out: List[McpToolInfo] = []
        for item in remote_tools:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            if not name:
                continue
            if allowed and name not in allowed:
                continue
            catalog = f"{prefix}.{name}" if prefix else name
            out.append(McpToolInfo(
                server=server, name=name, catalog_name=catalog,
                description=str(item.get("description") or f"MCP {server}/{name}"),
                input_schema=item.get("inputSchema")
                if isinstance(item.get("inputSchema"), dict) else {"type": "object"},
                protocol_version=protocol_version))
        return out

    def register_into(self, tool_executor: Any) -> int:
        """Dynamically register AVAILABLE MCP tools onto a ToolExecutor."""
        from app.runtime.tools import ToolDefinition

        registered = 0
        for catalog_name, info in self.tools.items():
            health = self.health.get(info.server)
            if health is None or health.status != "AVAILABLE":
                continue
            tool_executor.definitions[catalog_name] = ToolDefinition(
                name=catalog_name,
                description=f"[MCP:{info.server}] {info.description}",
                input_schema=info.input_schema or {"type": "object"},
                output_schema={"type": "object", "properties": {
                    "success": {"type": "boolean"}}},
                timeout_seconds=40.0, max_retries=0,
                network_policy="gateway", kind="mcp",
                side_effect_level="read_only",
                mcp_server=info.server,
                protocol_version=info.protocol_version)
            registered += 1
        # Keep legacy mcp_fetch_url alias pointing at stdio fetch whitelist path.
        if "fetch" in self.health and self.health["fetch"].status == "AVAILABLE":
            existing = tool_executor.definitions.get("mcp_fetch_url")
            if existing is None or existing.kind != "mcp":
                tool_executor.definitions["mcp_fetch_url"] = ToolDefinition(
                    "mcp_fetch_url",
                    "通过 MCP fetch server 抓取候选人声明的公开主页（白名单域名）",
                    {"type": "object", "properties": {
                        "url": {"type": "string"},
                        "maxLength": {"type": "integer"}}, "required": ["url"]},
                    {"type": "object", "properties": {"success": {"type": "boolean"}}},
                    timeout_seconds=40.0, max_retries=0,
                    network_policy="gateway", kind="mcp",
                    mcp_server="fetch", protocol_version=PROTOCOL_VERSION)
                registered += 1
        tool_executor.mcp_registry = self
        return registered

    def tools_for_agent(self, agent_id: str) -> List[str]:
        """Agent routing: only return tools that are both routed and AVAILABLE."""
        # ReportAgent must never call public-web MCP directly.
        if agent_id == "ReportAgent":
            return []
        routed = self.agent_routing.get(agent_id)
        if routed is None:
            # Default: any tool from servers listed on the agent definition
            # is filtered by AVAILABLE status below when routed is empty list
            # vs missing — empty list means explicitly none.
            return []
        available = []
        for name in routed:
            info = self.tools.get(name)
            if name == "mcp_fetch_url":
                health = self.health.get("fetch")
                if health and health.status == "AVAILABLE":
                    available.append(name)
                continue
            if info is None:
                continue
            health = self.health.get(info.server)
            if health and health.status == "AVAILABLE":
                available.append(name)
        return available

    def status_snapshot(self) -> Dict[str, Any]:
        return {
            "servers": {
                name: {
                    "status": h.status,
                    "transport": h.transport,
                    "latencyMs": h.latency_ms,
                    "tools": h.tools,
                    "error": h.error,
                    "url": h.url,
                }
                for name, h in self.health.items()
            },
            "availableTools": sorted(self.tools.keys()),
            "configPath": str(resolve_mcp_config_path() or ""),
        }

    async def call(self, catalog_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        if catalog_name == "mcp_fetch_url":
            from app.runtime import mcp_client
            url = str(arguments.get("url") or "")
            if not host_allowed(url):
                return {"success": False, "status": "unavailable",
                        "text": f"域名不在公开主页白名单内: {url}"}
            return await mcp_client.fetch_url(
                url, max_length=int(arguments.get("maxLength") or 6000))

        info = self.tools.get(catalog_name)
        if info is None:
            return {"success": False, "status": "unavailable",
                    "text": f"MCP tool not in catalog: {catalog_name}"}
        health = self.health.get(info.server)
        if health is None or health.status != "AVAILABLE":
            status = health.status if health else "DOWN"
            return {"success": False, "status": status,
                    "text": f"MCP server {info.server} is {status}"}

        # Whitelist gate for fetch-like / URL scrape tools.
        url = str(arguments.get("url") or arguments.get("urls") or "")
        if url and info.server in ("fetch", "firecrawl", "exa"):
            # Firecrawl/Exa may search without URL; only gate explicit URL args.
            if "://" in url and info.server == "fetch" and not host_allowed(url):
                return {"success": False, "status": "unavailable",
                        "text": f"域名不在白名单: {url}"}

        try:
            if info.server in self._http_clients:
                result = await self._http_clients[info.server].call_tool(
                    info.name, arguments)
            elif info.server in self._stdio_clients:
                result = await self._stdio_clients[info.server].call_tool(
                    info.name, arguments)
            else:
                return {"success": False, "status": "DOWN",
                        "text": f"no live client for {info.server}"}
            result.setdefault("mcpServer", info.server)
            result.setdefault("protocolVersion", info.protocol_version)
            result.setdefault("tool", catalog_name)
            if not result.get("success"):
                result.setdefault("status", "unavailable")
            return result
        except McpError as exc:
            msg = str(exc)
            status = "RATE_LIMITED" if "RATE_LIMITED" in msg or "429" in msg \
                else "AUTH_REQUIRED" if "AUTH_REQUIRED" in msg \
                else "unavailable"
            if info.server in self.health and status in (
                    "RATE_LIMITED", "AUTH_REQUIRED"):
                self.health[info.server].status = status
                self.health[info.server].error = msg[:300]
            return {"success": False, "status": status, "text": msg[:500]}


_registry: Optional[McpRegistry] = None
_registry_lock = asyncio.Lock()


async def get_mcp_registry(*, probe: bool = True) -> McpRegistry:
    global _registry
    async with _registry_lock:
        if _registry is None:
            _registry = McpRegistry()
        if probe:
            await _registry.probe_all()
        return _registry


def get_mcp_registry_sync() -> Optional[McpRegistry]:
    return _registry
