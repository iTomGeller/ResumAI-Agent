from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import List

from app.config import settings

logger = logging.getLogger(__name__)

_mcp_tools_cache: List = []


async def get_mcp_tools() -> List:
    global _mcp_tools_cache
    if _mcp_tools_cache:
        return _mcp_tools_cache
    config_path = Path(settings.mcp_config_path)
    if not config_path.exists():
        return []
    try:
        from langchain_mcp_adapters.client import MultiServerMCPClient

        raw = json.loads(config_path.read_text(encoding="utf-8"))
        servers = raw.get("mcpServers", {})
        if not servers:
            logger.warning("MCP disabled: workflow/mcp-servers.json has no mcpServers")
            return []
        client = MultiServerMCPClient(servers)
        _mcp_tools_cache = await client.get_tools()
        logger.info("Loaded MCP tools: %s", [tool.name for tool in _mcp_tools_cache])
        return _mcp_tools_cache
    except ImportError:
        logger.info("langchain-mcp-adapters not available, skipping MCP tools")
        return []
    except Exception as exc:
        logger.warning("Failed to load MCP tools: %s", exc)
        return []
