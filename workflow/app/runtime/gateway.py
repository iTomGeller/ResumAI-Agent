"""Controlled internal tool gateway used by the agent runtime.

Every network-facing capability goes through the Java control plane (which
owns secrets and allowlists); the runtime itself never issues arbitrary
public HTTP requests.
"""
from __future__ import annotations

import json
from typing import Dict

import httpx

from app.config import settings
from app.runtime.sandbox_tools_local import check_timeline


def _headers() -> Dict[str, str]:
    return {
        "Content-Type": "application/json",
        "X-Internal-Token": settings.workflow_internal_token,
    }


async def java_resume_search(query: str, top_k: int = 5, resume_text: str = "",
                             jd_requirements: str = "",
                             strategy: str = "hybrid") -> str:
    url = f"{settings.java_backend_url}/api/internal/tools/resume-search"
    payload = {"query": query, "topK": top_k, "resumeText": resume_text,
               "jdRequirements": jd_requirements, "strategy": strategy}
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(url, json=payload, headers=_headers())
        resp.raise_for_status()
        return json.dumps(resp.json(), ensure_ascii=False)


async def java_jd_search(resume_text: str, top_k: int = 3) -> str:
    url = f"{settings.java_backend_url}/api/internal/tools/jd-search"
    payload = {"resumeText": resume_text, "topK": top_k}
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(url, json=payload, headers=_headers())
        resp.raise_for_status()
        return json.dumps(resp.json(), ensure_ascii=False)


async def java_knowledge_search(query: str, top_k: int = 5) -> str:
    url = f"{settings.java_backend_url}/api/rag/knowledge-base/search"
    payload = {"query": query, "topK": top_k}
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(url, json=payload)
        resp.raise_for_status()
        return json.dumps(resp.json(), ensure_ascii=False)


async def java_external_profile(resume_text: str) -> str:
    url = f"{settings.java_backend_url}/api/internal/tools/external-profile"
    payload = {"resumeText": resume_text}
    async with httpx.AsyncClient(timeout=45.0) as client:
        resp = await client.post(url, json=payload, headers=_headers())
        resp.raise_for_status()
        data = resp.json()
        return data.get("summary", "") if isinstance(data, dict) else str(data)


async def timeline_validator(resume_text: str) -> str:
    """In-process deterministic timeline check (same logic as the sandbox
    tool, without container overhead) for quick validation paths."""
    return json.dumps(check_timeline({"resumeText": resume_text}),
                      ensure_ascii=False)
