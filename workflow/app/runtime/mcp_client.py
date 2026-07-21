from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# Public-web domains the fetch MCP tool may touch. Candidate-declared pages
# (GitHub/Gitee/blogs) are the use case; everything else is refused before
# the request leaves the process.
FETCH_ALLOWED_HOSTS = (
    "github.com", "gist.github.com", "raw.githubusercontent.com",
    "gitee.com", "gitcode.com",
    "juejin.cn", "zhihu.com", "zhuanlan.zhihu.com", "csdn.net", "blog.csdn.net",
    "cnblogs.com", "segmentfault.com", "medium.com", "dev.to",
)


class McpError(RuntimeError):
    pass


class McpStdioClient:
    """Minimal MCP client over stdio JSON-RPC (initialize / tools/list /
    tools/call). One subprocess per server, lazily started, restarted on the
    next call after a crash. Timeouts fail the call, never the run."""

    def __init__(self, name: str, command: list[str],
                 request_timeout: float = 30.0) -> None:
        self.name = name
        self.command = command
        self.request_timeout = request_timeout
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._lock = asyncio.Lock()
        self._next_id = 0
        self._initialized = False

    async def _ensure_started(self) -> None:
        if self._proc is not None and self._proc.returncode is None:
            return
        self._initialized = False
        logger.info("starting MCP server %s: %s", self.name, " ".join(self.command))
        self._proc = await asyncio.create_subprocess_exec(
            *self.command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await self._request("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "resumai-runtime", "version": "1.0"},
        })
        await self._notify("notifications/initialized", {})
        self._initialized = True

    async def _write(self, payload: Dict[str, Any]) -> None:
        assert self._proc is not None and self._proc.stdin is not None
        data = json.dumps(payload, ensure_ascii=False) + "\n"
        self._proc.stdin.write(data.encode("utf-8"))
        await self._proc.stdin.drain()

    async def _request(self, method: str, params: Dict[str, Any]) -> Dict[str, Any]:
        assert self._proc is not None and self._proc.stdout is not None
        self._next_id += 1
        request_id = self._next_id
        await self._write({"jsonrpc": "2.0", "id": request_id,
                           "method": method, "params": params})
        while True:
            line = await asyncio.wait_for(
                self._proc.stdout.readline(), timeout=self.request_timeout)
            if not line:
                raise McpError(f"MCP server {self.name} closed the pipe")
            try:
                message = json.loads(line.decode("utf-8"))
            except json.JSONDecodeError:
                continue  # skip log noise on stdout
            if message.get("id") != request_id:
                continue  # notification or unrelated response
            if "error" in message:
                raise McpError(str(message["error"])[:300])
            return message.get("result") or {}

    async def _notify(self, method: str, params: Dict[str, Any]) -> None:
        await self._write({"jsonrpc": "2.0", "method": method, "params": params})

    async def call_tool(self, tool: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        async with self._lock:
            try:
                await self._ensure_started()
                result = await self._request("tools/call", {
                    "name": tool, "arguments": arguments})
            except (asyncio.TimeoutError, McpError, OSError) as exc:
                # kill the subprocess so the next call restarts fresh
                if self._proc is not None and self._proc.returncode is None:
                    self._proc.kill()
                self._proc = None
                raise McpError(f"MCP {self.name}/{tool} failed: {exc}") from exc
        texts = []
        for item in result.get("content") or []:
            if isinstance(item, dict) and item.get("type") == "text":
                texts.append(str(item.get("text") or ""))
        return {
            "success": not result.get("isError", False),
            "text": "\n".join(texts)[:8000],
        }

    async def close(self) -> None:
        if self._proc is not None and self._proc.returncode is None:
            self._proc.kill()
        self._proc = None


_fetch_client: Optional[McpStdioClient] = None


def fetch_client() -> McpStdioClient:
    """Shared client for the public-web fetch MCP server (mcp-server-fetch)."""
    global _fetch_client
    if _fetch_client is None:
        command = os.getenv("MCP_FETCH_COMMAND", "python -m mcp_server_fetch").split()
        _fetch_client = McpStdioClient("fetch", command)
    return _fetch_client


def host_allowed(url: str) -> bool:
    from urllib.parse import urlparse

    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return False
    return any(host == allowed or host.endswith("." + allowed)
               for allowed in FETCH_ALLOWED_HOSTS)


async def fetch_url(url: str, max_length: int = 6000) -> Dict[str, Any]:
    """Fetch a candidate-declared public page through the MCP fetch server.
    Off-allowlist hosts are refused locally — the request never leaves."""
    if not host_allowed(url):
        return {"success": False,
                "text": f"域名不在公开主页白名单内，已拒绝抓取: {url}"}
    return await fetch_client().call_tool("fetch", {
        "url": url, "max_length": max_length})
