"""Protected runtime Ops snapshot: real MCP probe + SkillManager manifest."""

from __future__ import annotations

import logging
from typing import Any, Dict

from fastapi import APIRouter, Query

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/internal/ops", tags=["ops"])


@router.get("/runtime")
async def runtime_ops_snapshot(
    probe: bool = Query(default=False, description="Force MCP re-probe (slow)"),
    include_deprecated_skills: bool = Query(default=False),
) -> Dict[str, Any]:
    """Live Python runtime state for Java `/api/ops` — not config inference."""
    mcp_body: Dict[str, Any]
    try:
        from app.runtime.mcp_registry import get_mcp_registry, get_mcp_registry_sync

        registry = get_mcp_registry_sync()
        if registry is None:
            registry = await get_mcp_registry(probe=True)
        elif probe:
            await registry.probe_all(force=True)
        else:
            # Cheap when healthy/not due; automatically retries degraded
            # servers after the bounded registry TTL.
            await registry.probe_all()
        mcp_body = registry.status_snapshot()
    except Exception as exc:  # noqa: BLE001
        logger.warning("ops MCP snapshot failed: %s", exc)
        mcp_body = {
            "source": "python_mcp_registry",
            "probed": False,
            "error": str(exc)[:300],
            "servers": {},
            "availableTools": [],
            "statusEnum": ["AVAILABLE", "RATE_LIMITED", "AUTH_REQUIRED", "DOWN"],
        }

    skills_body: Dict[str, Any]
    try:
        from app.runtime.skills import default_skill_manager

        if not default_skill_manager.list_ids():
            default_skill_manager.reload()
        skills_body = default_skill_manager.runtime_manifest(
            include_deprecated=include_deprecated_skills)
    except Exception as exc:  # noqa: BLE001
        logger.warning("ops skills snapshot failed: %s", exc)
        skills_body = {
            "source": "python_skill_manager",
            "error": str(exc)[:300],
            "skills": [],
            "count": 0,
        }

    return {
        "service": "ai-resume-workflow",
        "mcp": mcp_body,
        "skills": skills_body,
    }
