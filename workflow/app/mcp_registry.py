from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping

from app.config import settings

logger = logging.getLogger(__name__)

_mcp_tools_cache: List[Any] = []
_mcp_clients_cache: List[Any] = []
_mcp_server_status: Dict[str, Dict[str, Any]] = {}
_mcp_loaded = False
_mcp_load_lock: Any = None
_mcp_load_loop: Any = None

_SUPPORTED_TRANSPORTS = {"stdio", "sse", "streamable_http"}
_DEFAULT_SERVER_DISCOVERY_TIMEOUT_SECONDS = 8.0
_MAX_SERVER_DISCOVERY_TIMEOUT_SECONDS = 30.0


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _missing_required_env(config: Mapping[str, Any]) -> list[str]:
    required = config.get("requiredEnv", [])
    if not isinstance(required, list):
        return ["<invalid requiredEnv>"]
    return [str(name) for name in required if not os.getenv(str(name), "").strip()]


def _requested(config: Mapping[str, Any], missing_env: list[str]) -> tuple[bool, str]:
    enabled = config.get("enabled", True)
    if isinstance(enabled, bool):
        if not enabled:
            return False, "disabled_by_config"
        if missing_env:
            return False, "missing_environment"
        return True, "enabled"
    if str(enabled).strip().lower() == "auto":
        if missing_env:
            return False, "missing_environment"
        return True, "auto_enabled"
    return False, "invalid_enabled_value"


def _header_value(spec: Any) -> tuple[str, str]:
    if isinstance(spec, str):
        return spec, ""
    if isinstance(spec, Mapping):
        return str(spec.get("env", "")), str(spec.get("prefix", ""))
    return "", ""


def _server_discovery_timeout(config: Mapping[str, Any]) -> float:
    raw = config.get("discoveryTimeoutSeconds", _DEFAULT_SERVER_DISCOVERY_TIMEOUT_SECONDS)
    try:
        timeout = float(raw)
    except (TypeError, ValueError):
        timeout = _DEFAULT_SERVER_DISCOVERY_TIMEOUT_SECONDS
    return min(max(timeout, 1.0), _MAX_SERVER_DISCOVERY_TIMEOUT_SECONDS)


def _runtime_config(server_id: str, config: Mapping[str, Any]) -> Dict[str, Any]:
    transport = str(config.get("transport", "")).strip().lower().replace("-", "_")
    if transport == "http":
        transport = "streamable_http"
    if transport not in _SUPPORTED_TRANSPORTS:
        raise ValueError(f"unsupported transport: {transport or '<missing>'}")

    runtime: Dict[str, Any] = {"transport": transport}
    if transport == "stdio":
        command = config.get("command")
        if not isinstance(command, str) or not command.strip():
            raise ValueError("stdio server requires a string command")
        runtime["command"] = command
        args = config.get("args", [])
        if not isinstance(args, list):
            raise ValueError("stdio server args must be a list")
        runtime["args"] = [str(arg) for arg in args]

        environment = config.get("env", {})
        if environment and not isinstance(environment, Mapping):
            raise ValueError("stdio server env must be an object")
        resolved_env = {str(key): str(value) for key, value in dict(environment or {}).items()}
        env_from_host = config.get("envFromHost", [])
        if not isinstance(env_from_host, list):
            raise ValueError("envFromHost must be a list")
        for name in env_from_host:
            value = os.getenv(str(name), "")
            if value:
                resolved_env[str(name)] = value
        if resolved_env:
            runtime["env"] = resolved_env
    else:
        url = config.get("url")
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            raise ValueError("remote server requires an http(s) URL")
        runtime["url"] = url

        headers = config.get("headers", {})
        if headers and not isinstance(headers, Mapping):
            raise ValueError("remote server headers must be an object")
        resolved_headers = {str(key): str(value) for key, value in dict(headers or {}).items()}
        headers_from_env = config.get("headersFromEnv", {})
        if not isinstance(headers_from_env, Mapping):
            raise ValueError("headersFromEnv must be an object")
        for header_name, spec in headers_from_env.items():
            env_name, prefix = _header_value(spec)
            value = os.getenv(env_name, "") if env_name else ""
            if value:
                resolved_headers[str(header_name)] = f"{prefix}{value}"
        if resolved_headers:
            runtime["headers"] = resolved_headers

    logger.debug("Prepared MCP runtime config for %s (transport=%s)", server_id, transport)
    return runtime


def _annotate_tools(
    server_id: str,
    config: Mapping[str, Any],
    evidence_policy: Mapping[str, Any],
    tools: list[Any],
) -> None:
    raw_evidence = config.get("evidence")
    is_external_evidence = isinstance(raw_evidence, Mapping)
    evidence = dict(raw_evidence) if is_external_evidence else {}
    if is_external_evidence:
        evidence.setdefault("provider", server_id)
        evidence.setdefault("subjectBinding", "unverified")
        evidence.setdefault("requiresSourceUrl", True)
    guardrail = (
        "External evidence only: candidate facts require a candidate-declared identifier and a "
        "source URL. Empty, rate-limited, or failed results mean unavailable; never invent a fallback."
    )
    for tool in tools:
        try:
            metadata = dict(getattr(tool, "metadata", None) or {})
            metadata["mcpServer"] = server_id
            if is_external_evidence:
                metadata["externalEvidence"] = evidence
                metadata["evidencePolicy"] = dict(evidence_policy)
            tool.metadata = metadata
            description = str(getattr(tool, "description", "") or "")
            if is_external_evidence and guardrail not in description:
                tool.description = f"{description}\n\n{guardrail}".strip()
        except Exception as exc:  # pragma: no cover - third-party tool models vary
            logger.debug("Could not annotate MCP tool %s: %s", getattr(tool, "name", "?"), exc)


def get_mcp_status() -> Dict[str, Dict[str, Any]]:
    """Return credential-safe availability details for diagnostics and tests."""
    return {server_id: dict(status) for server_id, status in _mcp_server_status.items()}


def reset_mcp_registry_cache() -> None:
    """Clear process-local discovery state; primarily useful for deterministic tests."""
    global _mcp_loaded
    _mcp_tools_cache.clear()
    _mcp_clients_cache.clear()
    _mcp_server_status.clear()
    _mcp_loaded = False


def _get_mcp_load_lock() -> asyncio.Lock:
    """Create the lock lazily for Python 3.8 and bind it to the running loop."""
    global _mcp_load_lock, _mcp_load_loop
    loop = asyncio.get_running_loop()
    if _mcp_load_lock is None or _mcp_load_loop is not loop:
        _mcp_load_lock = asyncio.Lock()
        _mcp_load_loop = loop
    return _mcp_load_lock


async def get_mcp_tools() -> List[Any]:
    """Discover MCP tools once, even when parallel graph nodes start together."""
    if _mcp_loaded:
        return list(_mcp_tools_cache)

    async with _get_mcp_load_lock():
        if _mcp_loaded:
            return list(_mcp_tools_cache)
        try:
            return await _discover_mcp_tools()
        except asyncio.CancelledError:
            # A caller-level discovery timeout must not leave a half-populated
            # process cache that the next graph node mistakes for a clean load.
            reset_mcp_registry_cache()
            raise


async def _discover_mcp_tools() -> List[Any]:
    global _mcp_loaded
    config_path = Path(settings.mcp_config_path)
    if not config_path.exists():
        _mcp_server_status["_config"] = {
            "status": "unavailable",
            "reason": "config_not_found",
            "path": str(config_path),
            "checkedAt": _now(),
        }
        logger.warning("MCP unavailable: config not found at %s", config_path)
        _mcp_loaded = True
        return []

    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        _mcp_server_status["_config"] = {
            "status": "unavailable",
            "reason": "invalid_config",
            "detail": str(exc),
            "checkedAt": _now(),
        }
        logger.warning("MCP unavailable: invalid config %s: %s", config_path, exc)
        _mcp_loaded = True
        return []

    servers = raw.get("mcpServers", {})
    evidence_policy = raw.get("evidencePolicy", {})
    if not isinstance(servers, Mapping) or not servers:
        _mcp_server_status["_config"] = {
            "status": "disabled",
            "reason": "no_servers_configured",
            "checkedAt": _now(),
        }
        logger.warning("MCP disabled: %s has no mcpServers", config_path)
        _mcp_loaded = True
        return []
    if not isinstance(evidence_policy, Mapping):
        evidence_policy = {}

    try:
        from langchain_mcp_adapters.client import MultiServerMCPClient
    except ImportError as exc:
        for server_id, server_config in servers.items():
            if not isinstance(server_config, Mapping):
                continue
            missing = _missing_required_env(server_config)
            requested, reason = _requested(server_config, missing)
            _mcp_server_status[str(server_id)] = {
                "status": "unavailable" if requested else ("unavailable" if reason == "missing_environment" else "disabled"),
                "reason": "adapter_not_installed" if requested else reason,
                "missingEnv": missing,
                "checkedAt": _now(),
            }
        logger.warning("MCP unavailable: langchain-mcp-adapters is not installed: %s", exc)
        _mcp_loaded = True
        return []

    pending_discovery: list[tuple[str, Mapping[str, Any], Dict[str, Any], str]] = []
    for raw_server_id, server_config in servers.items():
        server_id = str(raw_server_id)
        if not isinstance(server_config, Mapping):
            _mcp_server_status[server_id] = {
                "status": "unavailable",
                "reason": "invalid_server_config",
                "checkedAt": _now(),
            }
            logger.warning("MCP server %s unavailable: config must be an object", server_id)
            continue

        missing_env = _missing_required_env(server_config)
        requested, request_reason = _requested(server_config, missing_env)
        if not requested:
            status = "unavailable" if request_reason in {"missing_environment", "invalid_enabled_value"} else "disabled"
            _mcp_server_status[server_id] = {
                "status": status,
                "reason": request_reason,
                "missingEnv": missing_env,
                "default": bool(server_config.get("default", False)),
                "checkedAt": _now(),
            }
            if status == "unavailable":
                logger.warning("MCP server %s unavailable: %s (%s)", server_id, request_reason, missing_env)
            else:
                logger.info("MCP server %s disabled by config", server_id)
            continue

        try:
            runtime_config = _runtime_config(server_id, server_config)
        except ValueError as exc:
            _mcp_server_status[server_id] = {
                "status": "unavailable",
                "reason": "invalid_server_config",
                "detail": str(exc),
                "checkedAt": _now(),
            }
            logger.warning("MCP server %s unavailable: %s", server_id, exc)
            continue

        pending_discovery.append((server_id, server_config, runtime_config, request_reason))

    async def discover_server(
        server_id: str,
        server_config: Mapping[str, Any],
        runtime_config: Dict[str, Any],
        request_reason: str,
    ) -> tuple[str, Dict[str, Any], Any, list[Any]]:
        timeout = _server_discovery_timeout(server_config)
        try:
            client = MultiServerMCPClient({server_id: runtime_config})
            server_tools = await asyncio.wait_for(client.get_tools(), timeout=timeout)
        except asyncio.TimeoutError:
            status = {
                "status": "unavailable",
                "reason": "discovery_timeout",
                "detail": f"tool discovery exceeded {timeout:g}s",
                "transport": runtime_config["transport"],
                "checkedAt": _now(),
            }
            logger.warning("MCP server %s unavailable: discovery exceeded %.1fs", server_id, timeout)
            return server_id, status, None, []
        except Exception as exc:  # noqa: BLE001 - isolate optional public providers
            status = {
                "status": "unavailable",
                "reason": "connection_or_discovery_failed",
                "detail": str(exc)[:240],
                "transport": runtime_config["transport"],
                "checkedAt": _now(),
            }
            logger.warning("MCP server %s unavailable: %s", server_id, exc)
            return server_id, status, None, []

        if not server_tools:
            status = {
                "status": "unavailable",
                "reason": "no_tools_discovered",
                "transport": runtime_config["transport"],
                "checkedAt": _now(),
            }
            logger.warning("MCP server %s unavailable: no tools discovered", server_id)
            return server_id, status, None, []

        _annotate_tools(server_id, server_config, evidence_policy, server_tools)
        tool_names = [str(getattr(tool, "name", "<unnamed>")) for tool in server_tools]
        status = {
            "status": "available",
            "reason": request_reason,
            "transport": runtime_config["transport"],
            "default": bool(server_config.get("default", False)),
            "tools": tool_names,
            "checkedAt": _now(),
        }
        logger.info("Loaded MCP server %s tools: %s", server_id, tool_names)
        return server_id, status, client, list(server_tools)

    # Phase-4 graph nodes start concurrently. Discovering providers concurrently
    # prevents one slow optional public endpoint from starving internal/time tools.
    discovery_results = await asyncio.gather(
        *(discover_server(*entry) for entry in pending_discovery)
    )
    loaded_tools: list[Any] = []
    loaded_clients: list[Any] = []
    for server_id, status, client, server_tools in discovery_results:
        _mcp_server_status[server_id] = status
        if client is not None:
            loaded_clients.append(client)
        loaded_tools.extend(server_tools)

    _mcp_clients_cache.extend(loaded_clients)
    _mcp_tools_cache.extend(loaded_tools)
    _mcp_loaded = True
    logger.info("Loaded MCP tools: %s", [getattr(tool, "name", "<unnamed>") for tool in loaded_tools])
    return list(_mcp_tools_cache)
