from __future__ import annotations

"""MCP registry: Streamable HTTP clients + stdio fetch fallback + health probe.

Only tools that survive initialize + tools/list enter the Agent catalog.
The production catalog contains only services that can initialize without
OAuth or API keys; failed/empty discovery is never treated as availability.
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

from app.runtime.mcp_client import McpError, McpStdioClient, host_allowed

logger = logging.getLogger(__name__)

HealthStatus = str  # AVAILABLE | RATE_LIMITED | AUTH_REQUIRED | DOWN
PROTOCOL_VERSION = "2025-11-25"
PROTOCOL_VERSION_FALLBACKS: Tuple[str, ...] = (
    PROTOCOL_VERSION,
    "2025-06-18",
    "2025-03-26",
    "2024-11-05",
)
DEFAULT_TIMEOUT = 25.0
CIRCUIT_FAILURES = 3
CIRCUIT_COOLDOWN_S = 60.0
try:
    DEGRADED_REPROBE_TTL_S = max(
        CIRCUIT_COOLDOWN_S,
        float(os.getenv("MCP_DEGRADED_REPROBE_TTL_SECONDS", "60") or "60"),
    )
except ValueError:
    DEGRADED_REPROBE_TTL_S = CIRCUIT_COOLDOWN_S

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


def _redact_env_values(value: str, env_names: List[str]) -> str:
    """Keep health/error telemetry useful without exposing URL credentials."""
    redacted = str(value or "")
    for env_name in env_names:
        secret = (os.getenv(str(env_name)) or "").strip()
        if secret:
            redacted = redacted.replace(secret, f"${{{env_name}}}")
    return redacted


def _expand_headers(raw: Dict[str, Any]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for key, value in (raw or {}).items():
        out[str(key)] = _expand_env(str(value))
    return out


def _is_protocol_version_error(exc: Exception) -> bool:
    text = str(exc or "").lower().replace("_", " ")
    return (
        "protocolversion" in text
        or "protocol version" in text
        or ("protocol" in text
            and any(token in text for token in (
                "unsupported", "not supported", "invalid", "unknown",
                "version mismatch")))
    )


@dataclass
class McpToolInfo:
    server: str
    name: str                 # remote tool name (e.g. web_search_exa)
    catalog_name: str         # registered name (e.g. exa.web_search_exa)
    description: str
    input_schema: Dict[str, Any] = field(default_factory=dict)
    output_schema: Dict[str, Any] = field(default_factory=dict)
    protocol_version: str = PROTOCOL_VERSION
    transport: str = ""


@dataclass
class McpServerHealth:
    name: str
    status: HealthStatus
    transport: str
    latency_ms: int = 0
    tools: List[str] = field(default_factory=list)
    error: str = ""
    url: str = ""
    protocol_version: str = ""


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
        # Bind asyncio primitives lazily. Constructing a Lock in a Python 3.8
        # main thread (or reusing it across pytest event loops) is unsafe.
        self._lock: Optional[asyncio.Lock] = None
        self._lock_loop: Any = None
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

    def _base_headers(self, *, include_protocol_version: bool = False) -> Dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            **self.headers,
        }
        if include_protocol_version:
            headers["MCP-Protocol-Version"] = self.protocol_version
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
                    self.url,
                    json=payload,
                    headers=self._base_headers(
                        include_protocol_version=(
                            payload.get("method") != "initialize")),
                )
            session = response.headers.get("mcp-session-id") \
                or response.headers.get("Mcp-Session-Id")
            if session:
                self.session_id = session
            if response.status_code == 401 or response.status_code == 403:
                self._record_failure()
                raise McpError(f"AUTH_REQUIRED status={response.status_code}")
            if response.status_code == 429:
                # Provider quota pressure is not a transport-health failure.
                # Counting 429 toward the circuit breaker made three bursty
                # calls remove an otherwise healthy server from every
                # concurrent run for a full cooldown window.
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

    def _current_lock(self) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        if self._lock is None or self._lock_loop is not loop:
            self._lock = asyncio.Lock()
            self._lock_loop = loop
        return self._lock

    async def initialize(self) -> Dict[str, Any]:
        async with self._current_lock():
            result: Dict[str, Any] = {}
            last_error: Optional[McpError] = None
            for index, version in enumerate(PROTOCOL_VERSION_FALLBACKS):
                try:
                    response = await self._post({
                        "jsonrpc": "2.0",
                        "id": self._next_request_id(),
                        "method": "initialize",
                        "params": {
                            "protocolVersion": version,
                            "capabilities": {},
                            "clientInfo": {
                                "name": "resumai-runtime", "version": "1.0"},
                        },
                    })
                    result = response if isinstance(response, dict) else {}
                    self.protocol_version = str(
                        result.get("protocolVersion") or version)
                    last_error = None
                    break
                except McpError as exc:
                    last_error = exc
                    has_fallback = index + 1 < len(PROTOCOL_VERSION_FALLBACKS)
                    if not has_fallback or not _is_protocol_version_error(exc):
                        raise
                    logger.info(
                        "MCP %s rejected protocol %s; bounded fallback to %s",
                        self.name, version,
                        PROTOCOL_VERSION_FALLBACKS[index + 1])
            if last_error is not None:
                raise last_error
            await self._notify_initialized()
            self._initialized = True
            return result

    async def _notify_initialized(self) -> None:
        """Send the required one-way initialized notification."""
        # Reuse the normal request path so negotiated protocol/session headers,
        # HTTP failures, rate limits and circuit state follow one contract.
        await self._post({
            "jsonrpc": "2.0",
            "method": "notifications/initialized",
            "params": {},
        })

    async def list_tools(self) -> List[Dict[str, Any]]:
        if not self._initialized:
            await self.initialize()
        collected: List[Dict[str, Any]] = []
        cursor: Optional[str] = None
        # MCP tools/list is paginated. Bound the loop so a broken server cannot
        # keep discovery alive forever.
        for _page in range(20):
            params: Dict[str, Any] = {}
            if cursor:
                params["cursor"] = cursor
            result = await self._post({
                "jsonrpc": "2.0",
                "id": self._next_request_id(),
                "method": "tools/list",
                "params": params,
            })
            tools = result.get("tools") if isinstance(result, dict) else None
            if isinstance(tools, list):
                collected.extend(item for item in tools if isinstance(item, dict))
            next_cursor = result.get("nextCursor") if isinstance(result, dict) else None
            if not next_cursor or next_cursor == cursor:
                break
            cursor = str(next_cursor)
        return collected

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
        self._last_probe_at: Optional[float] = None
        self._last_probe_iso: str = ""
        self._probe_lock: Optional[asyncio.Lock] = None
        self._probe_lock_loop: Any = None
        self._call_gate_loop: Any = None
        self._call_semaphores: Dict[str, asyncio.Semaphore] = {}
        self._last_call_started: Dict[str, float] = {}
        self._rate_limited_until: Dict[str, float] = {}

    def _current_probe_lock(self) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        if self._probe_lock is None or self._probe_lock_loop is not loop:
            self._probe_lock = asyncio.Lock()
            self._probe_lock_loop = loop
        return self._probe_lock

    def _server_config(self, server: str) -> Dict[str, Any]:
        value = (
            (self.config.get("mcpServers") or {}).get(server)
            or (self.config.get("optionalMcpServers") or {}).get(server)
            or {})
        return value if isinstance(value, dict) else {}

    def _current_call_semaphore(
            self, server: str, max_concurrent: int) -> asyncio.Semaphore:
        loop = asyncio.get_running_loop()
        if self._call_gate_loop is not loop:
            self._call_gate_loop = loop
            self._call_semaphores = {}
            self._last_call_started = {}
        semaphore = self._call_semaphores.get(server)
        if semaphore is None:
            semaphore = asyncio.Semaphore(max(1, max_concurrent))
            self._call_semaphores[server] = semaphore
        return semaphore

    def _rate_limit_cooldown_seconds(self, server: str) -> float:
        try:
            return max(1.0, float(
                self._server_config(server).get(
                    "rateLimitCooldownSeconds", 120.0)))
        except (TypeError, ValueError):
            return 120.0

    def _rate_limit_blocked(
            self, server: str, *, now: Optional[float] = None) -> bool:
        return self._rate_limited_until.get(server, 0.0) > (
            time.time() if now is None else now)

    def _mark_rate_limited(self, server: str, message: str) -> None:
        now = time.time()
        cooldown = self._rate_limit_cooldown_seconds(server)
        self._rate_limited_until[server] = max(
            self._rate_limited_until.get(server, 0.0), now + cooldown)
        health = self.health.get(server)
        if health is not None:
            health.status = "RATE_LIMITED"
            health.error = str(message or "rate limited")[:300]
        # Start recovery timing at the exhausted call. tools/list may remain
        # healthy while every tools/call is 429, so an immediate re-probe must
        # not re-advertise this provider to the next Run.
        self._last_probe_at = now
        self._last_probe_iso = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
        logger.warning(
            "MCP %s rate-limit cooldown opened for %.1fs", server, cooldown)

    def needs_probe(self, *, now: Optional[float] = None) -> bool:
        """Return whether an initial or degraded-server probe is due.

        Healthy live clients are not periodically torn down. Only DOWN and
        RATE_LIMITED servers become eligible after the circuit-cooldown-sized
        TTL, so constructing an executor on every turn does not cause network
        discovery on every turn.
        """
        if not self._probed:
            return True
        now_value = now if now is not None else time.time()
        eligible = [
            name for name, health in self.health.items()
            if health.status in {"DOWN", "RATE_LIMITED"}
            and not (
                health.status == "RATE_LIMITED"
                and self._rate_limit_blocked(name, now=now_value))
        ]
        if not eligible:
            return False
        checked_at = self._last_probe_at or 0.0
        return now_value - checked_at \
            >= DEGRADED_REPROBE_TTL_S

    async def _drop_server_state(self, server: str) -> None:
        """Remove one degraded server without disrupting healthy live clients."""
        self.tools = {
            name: info for name, info in self.tools.items()
            if info.server != server
        }
        self._http_clients.pop(server, None)
        stdio = self._stdio_clients.pop(server, None)
        if stdio is not None:
            await stdio.close()
        self.health.pop(server, None)

    def _mark_probe_complete(self) -> None:
        self._probed = True
        self._last_probe_at = time.time()
        self._last_probe_iso = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(self._last_probe_at))

    async def probe_all(self, *, force: bool = False) -> Dict[str, McpServerHealth]:
        async with self._current_probe_lock():
            skip_probe = os.getenv("MCP_SKIP_PROBE", "").strip() in (
                "1", "true", "yes")
            servers = dict(self.config.get("mcpServers") or {})
            optional = dict(self.config.get("optionalMcpServers") or {})
            all_servers = {**servers, **optional}

            if self._probed and not force:
                if not self.needs_probe():
                    return self.health
                degraded = [
                    (name, all_servers[name], name in optional)
                    for name, health in self.health.items()
                    if health.status in {"DOWN", "RATE_LIMITED"}
                    and not (
                        health.status == "RATE_LIMITED"
                        and self._rate_limit_blocked(name))
                    and name in all_servers
                    and isinstance(all_servers.get(name), dict)
                ]

                # Re-probe degraded servers in an isolated registry.  The live
                # health/catalog/client maps remain untouched while network I/O
                # is in flight, so caller cancellation or an outer timeout
                # cannot leave a half-replaced degraded server behind.
                staged = McpRegistry(config=self.config)
                try:
                    if skip_probe:
                        for name, cfg, _ in degraded:
                            staged._register_without_probe(name, cfg)
                    else:
                        await asyncio.gather(*(
                            staged._probe_server(
                                name, cfg, optional=is_optional)
                            for name, cfg, is_optional in degraded
                        ))
                except BaseException:
                    await asyncio.gather(*(
                        client.close()
                        for client in staged._stdio_clients.values()
                    ), return_exceptions=True)
                    raise

                degraded_names = {name for name, _, _ in degraded}
                next_health = dict(self.health)
                next_tools = {
                    catalog_name: info
                    for catalog_name, info in self.tools.items()
                    if info.server not in degraded_names
                }
                next_http_clients = dict(self._http_clients)
                next_stdio_clients = dict(self._stdio_clients)
                previous_stdio = []
                for name in degraded_names:
                    next_health.pop(name, None)
                    next_http_clients.pop(name, None)
                    old_stdio = next_stdio_clients.pop(name, None)
                    if old_stdio is not None:
                        previous_stdio.append(old_stdio)
                next_health.update(staged.health)
                next_tools.update(staged.tools)
                next_http_clients.update(staged._http_clients)
                next_stdio_clients.update(staged._stdio_clients)

                # No await occurs during the four-map swap, so other event-loop
                # tasks observe either the complete old state or complete new
                # state, never a partially re-probed catalog.
                self.health = next_health
                self.tools = next_tools
                self._http_clients = next_http_clients
                self._stdio_clients = next_stdio_clients
                for name in degraded_names:
                    if (self.health.get(name)
                            and self.health[name].status == "AVAILABLE"):
                        self._rate_limited_until.pop(name, None)
                self._mark_probe_complete()
                summary = {k: v.status for k, v in self.health.items()}
                logger.info(
                    "MCP degraded re-probe: %s (%d tools)",
                    summary, len(self.tools))
                await asyncio.gather(*(
                    client.close() for client in previous_stdio
                ), return_exceptions=True)
                return self.health

            configured = [
                (name, cfg, name in optional)
                for name, cfg in all_servers.items()
                if isinstance(cfg, dict)
            ]
            # Build a complete replacement registry off to the side.  Live
            # callers continue using the last healthy catalog while a forced
            # probe is in flight; cancellation/timeout cannot expose a
            # half-populated tool set.
            staged = McpRegistry(config=self.config)
            try:
                if skip_probe:
                    for name, cfg, _ in configured:
                        staged._register_without_probe(name, cfg)
                else:
                    # Each remote server has its own bounded request timeout.
                    # Discover them concurrently so one slow public endpoint
                    # cannot turn a five-server probe into a sum-of-timeouts
                    # stall.
                    await asyncio.gather(*(
                        staged._probe_server(
                            name, cfg, optional=is_optional)
                        for name, cfg, is_optional in configured
                    ))
            except BaseException:
                await asyncio.gather(*(
                    client.close()
                    for client in staged._stdio_clients.values()
                ), return_exceptions=True)
                raise

            previous_stdio = list(self._stdio_clients.values())
            self.health = staged.health
            self.tools = staged.tools
            self._http_clients = staged._http_clients
            self._stdio_clients = staged._stdio_clients
            self._rate_limited_until = {}
            await asyncio.gather(*(
                client.close() for client in previous_stdio
            ), return_exceptions=True)

            self._mark_probe_complete()
            summary = {k: v.status for k, v in self.health.items()}
            logger.info("MCP health probe: %s (%d tools)", summary, len(self.tools))
            return self.health

    def _register_without_probe(self, name: str, cfg: Dict[str, Any]) -> None:
        """Record an unprobed server as unavailable.

        Config declarations are not tools/list results and there is no live
        client capable of tools/call. They therefore must never enter the model
        tool catalog.
        """
        transport = str(cfg.get("transport") or "streamable-http")
        url = _expand_env(str(cfg.get("url") or ""))
        enabled = bool(cfg.get("enabled", True))
        required_env = [str(e) for e in (cfg.get("requiredEnv") or [])]
        display_url = _redact_env_values(url, required_env)
        missing = [e for e in required_env if not (os.getenv(e) or "").strip()]
        status = "AUTH_REQUIRED" if missing else "DOWN"
        reason = (f"missing env: {','.join(missing)}" if missing
                  else "disabled in config" if not enabled
                  else "probe skipped; tools/list not verified")
        self.health[name] = McpServerHealth(
            name=name, status=status, transport=transport, url=display_url,
            tools=[], error=reason)
        logger.info("MCP %s not exposed: %s", name, reason)

    async def _probe_server(self, name: str, cfg: Dict[str, Any], *,
                            optional: bool) -> None:
        transport = str(cfg.get("transport") or "streamable-http")
        url = _expand_env(str(cfg.get("url") or ""))
        required_env = [str(e) for e in (cfg.get("requiredEnv") or [])]
        display_url = _redact_env_values(url, required_env)
        missing = [e for e in required_env if not (os.getenv(e) or "").strip()]
        enabled = bool(cfg.get("enabled", True))

        # Generic fail-closed support remains for non-production configs.
        if missing:
            status = str(cfg.get("healthStatusWhenMissingEnv") or "AUTH_REQUIRED")
            self.health[name] = McpServerHealth(
                name=name, status=status, transport=transport, url=display_url,
                error=f"missing env: {','.join(missing)}")
            return

        if optional and not enabled:
            self.health[name] = McpServerHealth(
                name=name, status="AUTH_REQUIRED" if required_env else "DOWN",
                transport=transport, url=display_url,
                error="optional server disabled")
            return

        if not enabled:
            self.health[name] = McpServerHealth(
                name=name, status="DOWN", transport=transport, url=display_url,
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
            if not tools:
                # Transport adapters validate discovery themselves, but keep
                # AVAILABLE fail-closed at this central state transition too.
                raise McpError(
                    f"MCP_DISCOVERY_EMPTY {name}: no tools eligible for catalog")
            latency = int((time.monotonic() - started) * 1000)
            negotiated = (
                self._http_clients[name].protocol_version
                if name in self._http_clients else PROTOCOL_VERSION)
            self.health[name] = McpServerHealth(
                name=name, status="AVAILABLE", transport=transport,
                latency_ms=latency, url=display_url,
                tools=[t.catalog_name for t in tools],
                protocol_version=negotiated)
            for tool in tools:
                self.tools[tool.catalog_name] = tool
        except McpError as exc:
            latency = int((time.monotonic() - started) * 1000)
            msg = _redact_env_values(str(exc), required_env)
            if "AUTH_REQUIRED" in msg:
                status: HealthStatus = "AUTH_REQUIRED"
            elif "RATE_LIMITED" in msg or "429" in msg:
                status = "RATE_LIMITED"
            else:
                status = "DOWN"
            self.health[name] = McpServerHealth(
                name=name, status=status, transport=transport,
                latency_ms=latency, url=display_url, error=msg[:300])
            logger.warning("MCP probe %s -> %s: %s", name, status, msg[:200])
        except Exception as exc:  # noqa: BLE001
            latency = int((time.monotonic() - started) * 1000)
            message = _redact_env_values(str(exc), required_env)
            self.health[name] = McpServerHealth(
                name=name, status="DOWN", transport=transport,
                latency_ms=latency, url=display_url, error=message[:300])
            logger.warning("MCP probe %s failed: %s", name, message)

    async def _probe_http(self, name: str, cfg: Dict[str, Any],
                          allowed: set, prefix: str, url: str
                          ) -> List[McpToolInfo]:
        headers = _expand_headers(cfg.get("headers") or {})
        client = StreamableHttpMcpClient(name, url, headers=headers)
        await client.initialize()
        remote_tools = await client.list_tools()
        tools = self._validated_discovery(
            name, remote_tools, allowed, prefix, client.protocol_version)
        self._http_clients[name] = client
        return tools

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
        try:
            tools = self._validated_discovery(
                name, remote_tools, allowed, prefix, PROTOCOL_VERSION)
        except McpError:
            await client.close()
            raise
        self._stdio_clients[name] = client
        return tools

    @classmethod
    def _validated_discovery(cls, server: str, remote_tools: List[Any],
                             allowed: set, prefix: str,
                             protocol_version: str) -> List[McpToolInfo]:
        """Fail closed when tools/list is empty or a whitelist matches nothing."""
        remote_names = [
            str(item.get("name") or "").strip()
            for item in remote_tools if isinstance(item, dict)
            and str(item.get("name") or "").strip()
        ]
        if not remote_names:
            raise McpError(
                f"MCP_DISCOVERY_EMPTY {server}: tools/list returned no named tools")
        filtered = cls._filter_tools(
            server, remote_tools, allowed, prefix, protocol_version)
        if not filtered:
            configured = ",".join(sorted(str(name) for name in allowed))[:180]
            discovered = ",".join(remote_names)[:180]
            raise McpError(
                f"MCP_CONFIG_MISMATCH {server}: allowedTools matched 0; "
                f"configured=[{configured}] discovered=[{discovered}]")
        return filtered

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
                output_schema=item.get("outputSchema")
                if isinstance(item.get("outputSchema"), dict) else {},
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
            if not self.has_live_client(info.server):
                logger.warning("MCP tool %s omitted: no live tools/call client",
                               catalog_name)
                continue
            server_cfg = self._server_config(info.server)
            routing_guidance = str(
                server_cfg.get("routingGuidance") or "").strip()
            description = info.description
            if routing_guidance:
                description = (
                    f"{description.rstrip()} Runtime routing guidance: "
                    f"{routing_guidance}")
            try:
                timeout_seconds = max(
                    1.0, float(server_cfg.get("timeoutSeconds", 40.0)))
            except (TypeError, ValueError):
                timeout_seconds = 40.0
            tool_executor.definitions[catalog_name] = ToolDefinition(
                name=catalog_name,
                # Keep the exact tools/list description that taught the model
                # when and how to call this function. Server provenance travels
                # in the separate mcp_server field and trace payload.
                description=description,
                input_schema=info.input_schema or {"type": "object"},
                # MCP tools/list describes the server-native result, while the
                # runtime deliberately normalizes every CallToolResult into a
                # stable envelope (success/text/structuredContent/parsed).
                # Validating that envelope against the remote native schema
                # incorrectly rejected successful DeepWiki calls that require
                # a server-side `result` field.
                output_schema={
                    "type": "object",
                    "properties": {
                        "success": {"type": "boolean"},
                        "text": {"type": "string"},
                        "isError": {"type": "boolean"},
                        "structuredContent": {"type": "object"},
                        "parsed": {},
                    },
                    "required": ["success"],
                },
                timeout_seconds=timeout_seconds, max_retries=0,
                network_policy="gateway", kind="mcp",
                side_effect_level="read_only",
                mcp_server=info.server,
                protocol_version=info.protocol_version,
                execution_backend=(
                    "mcp_stdio" if health.transport == "stdio" else "mcp_http"))
            registered += 1
        tool_executor.mcp_registry = self
        return registered

    def has_live_client(self, server: str) -> bool:
        return server in self._http_clients or server in self._stdio_clients

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
            if info is None:
                continue
            health = self.health.get(info.server)
            if (health and health.status == "AVAILABLE"
                    and not self._rate_limit_blocked(info.server)
                    and self.has_live_client(info.server)):
                available.append(name)
        return available

    def status_snapshot(self) -> Dict[str, Any]:
        """Live registry health — never inferred from config description text."""
        servers_cfg = dict(self.config.get("mcpServers") or {})
        optional_cfg = dict(self.config.get("optionalMcpServers") or {})
        servers: Dict[str, Any] = {}
        for name, h in self.health.items():
            cfg = servers_cfg.get(name) or optional_cfg.get(name) or {}
            entry: Dict[str, Any] = {
                "name": name,
                "status": h.status,
                "transport": h.transport,
                "latencyMs": h.latency_ms,
                "tools": list(h.tools),
                "error": h.error,
                "url": h.url,
                "protocolVersion": h.protocol_version or None,
                "optional": name in optional_cfg,
                "enabled": bool(cfg.get("enabled", True)) if isinstance(cfg, dict) else False,
                "default": bool(cfg.get("default", False)) if isinstance(cfg, dict) else False,
                "description": str(cfg.get("description") or "") if isinstance(cfg, dict) else "",
            }
            rate_limit_until = self._rate_limited_until.get(name, 0.0)
            if rate_limit_until > time.time():
                entry["rateLimitRetryAt"] = time.strftime(
                    "%Y-%m-%dT%H:%M:%SZ", time.gmtime(rate_limit_until))
                entry["rateLimitCooldownRemainingSeconds"] = max(
                    0, int(rate_limit_until - time.time()))
            http_client = self._http_clients.get(name)
            if http_client is not None:
                entry["sessionId"] = http_client.session_id or ""
                entry["circuitOpen"] = bool(http_client._circuit_blocked())  # noqa: SLF001
                entry["failCount"] = int(http_client._fail_count)  # noqa: SLF001
                entry["protocolVersion"] = http_client.protocol_version
            servers[name] = entry
        return {
            "source": "python_mcp_registry",
            "probed": self._probed,
            "lastProbeAt": self._last_probe_iso or None,
            "automaticProbeDue": self.needs_probe(),
            "degradedReprobeTtlSeconds": int(DEGRADED_REPROBE_TTL_S),
            "servers": servers,
            "availableTools": sorted(self.tools.keys()),
            "toolCount": len(self.tools),
            "configPath": str(resolve_mcp_config_path() or ""),
            "statusEnum": ["AVAILABLE", "RATE_LIMITED", "AUTH_REQUIRED", "DOWN"],
        }

    async def call(self, catalog_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        info = self.tools.get(catalog_name)
        if info is None:
            return {"success": False, "status": "unavailable",
                    "text": f"MCP tool not in catalog: {catalog_name}"}
        health = self.health.get(info.server)
        if health is None or health.status != "AVAILABLE":
            status = health.status if health else "DOWN"
            return {"success": False, "status": status,
                    "text": f"MCP server {info.server} is {status}"}
        if self._rate_limit_blocked(info.server):
            return {"success": False, "status": "RATE_LIMITED",
                    "text": f"MCP server {info.server} is cooling down"}

        # Whitelist gate for fetch-like / URL scrape tools.
        url = str(arguments.get("url") or arguments.get("urls") or "")
        if url and info.server in ("fetch", "exa"):
            if "://" in url and info.server == "fetch" and not host_allowed(url):
                return {"success": False, "status": "unavailable",
                        "text": f"域名不在白名单: {url}"}

        cfg = self._server_config(info.server)
        required_env = [str(e) for e in (cfg.get("requiredEnv") or [])]
        try:
            max_concurrent = max(1, int(cfg.get("maxConcurrentCalls") or 4))
            min_interval_s = max(
                0.0, float(cfg.get("minIntervalMs") or 0) / 1000.0)
            rate_limit_retries = max(
                0, min(3, int(cfg.get("rateLimitRetries") or 0)))
            backoff_s = max(
                0.1, float(cfg.get("rateLimitBackoffMs") or 1000) / 1000.0)
        except (TypeError, ValueError):
            max_concurrent, min_interval_s = 4, 0.0
            rate_limit_retries, backoff_s = 0, 1.0

        semaphore = self._current_call_semaphore(
            info.server, max_concurrent)
        async with semaphore:
            # A sibling call may have learned that quota is exhausted while
            # this call waited for the per-server gate. Fail queued siblings
            # immediately instead of serially paying the provider timeout.
            if self._rate_limit_blocked(info.server):
                return {"success": False, "status": "RATE_LIMITED",
                        "text": f"MCP server {info.server} is cooling down"}
            for attempt in range(rate_limit_retries + 1):
                elapsed = time.monotonic() - self._last_call_started.get(
                    info.server, 0.0)
                if min_interval_s and elapsed < min_interval_s:
                    await asyncio.sleep(min_interval_s - elapsed)
                self._last_call_started[info.server] = time.monotonic()
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
                    blob = " ".join(str(result.get(key) or "") for key in (
                        "status", "text", "error")).lower()
                    rate_limited = (
                        not result.get("success")
                        and any(token in blob for token in (
                            "rate_limit", "rate limited", "too many requests",
                            "429")))
                    if rate_limited and attempt < rate_limit_retries:
                        await asyncio.sleep(backoff_s * (2 ** attempt))
                        continue
                    if rate_limited:
                        self._mark_rate_limited(
                            info.server,
                            str(result.get("text") or result.get("error")
                                or "RATE_LIMITED"))
                    if not result.get("success"):
                        result.setdefault(
                            "status",
                            "RATE_LIMITED" if rate_limited else "UNAVAILABLE")
                    return result
                except McpError as exc:
                    msg = _redact_env_values(str(exc), required_env)
                    rate_limited = "RATE_LIMITED" in msg or "429" in msg
                    if rate_limited and attempt < rate_limit_retries:
                        await asyncio.sleep(backoff_s * (2 ** attempt))
                        continue
                    status = (
                        "RATE_LIMITED" if rate_limited
                        else "AUTH_REQUIRED" if "AUTH_REQUIRED" in msg
                        else "DOWN")
                    if info.server in self.health:
                        if rate_limited:
                            self._mark_rate_limited(info.server, msg)
                        else:
                            self.health[info.server].status = status
                        self.health[info.server].error = msg[:300]
                    return {
                        "success": False, "status": status,
                        "text": msg[:500], "rateLimitRetries": attempt}

        return {"success": False, "status": "UNAVAILABLE",
                "text": f"MCP call exhausted for {catalog_name}"}


_registry: Optional[McpRegistry] = None


async def get_mcp_registry(*, probe: bool = True) -> McpRegistry:
    global _registry
    # Registry assignment occurs before the first await; probe_all owns the
    # loop-local concurrency lock for the only async state transition.
    if _registry is None:
        _registry = McpRegistry()
    if probe:
        await _registry.probe_all()
    return _registry


def get_mcp_registry_sync() -> Optional[McpRegistry]:
    return _registry
