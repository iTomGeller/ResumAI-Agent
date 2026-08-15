"""Controlled internal tool gateway used by the agent runtime.

Every network-facing capability goes through the Java control plane (which
owns secrets and allowlists); the runtime itself never issues arbitrary
public HTTP requests.
"""
from __future__ import annotations

import json
import time
from typing import Any, Dict

import httpx

from app.config import settings
from app.runtime.builtin_tools import check_timeline


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
    started = time.perf_counter()
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(url, json=payload, headers=_headers())
        resp.raise_for_status()
        data = resp.json()
    elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
    if isinstance(data, dict):
        data["_latency"] = {
            "retrieval_ms": elapsed_ms,
            "total_ms": elapsed_ms,
        }
    return json.dumps(data, ensure_ascii=False)


async def java_jd_focus(jd_text: str, job_title: str = "",
                        job_category: str = "") -> Dict[str, Any]:
    """Rank existing JD-RAG chunks for Tech/Project scoped queries."""
    url = f"{settings.java_backend_url}/api/internal/tools/jd-focus"
    payload = {"jdText": jd_text, "jobTitle": job_title,
               "jobCategory": job_category}
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(url, json=payload, headers=_headers())
        resp.raise_for_status()
        data = resp.json()
    return data if isinstance(data, dict) else {}


async def java_knowledge_search(query: str, top_k: int = 5,
                                rerank: bool = False) -> str:
    """KB hybrid search with an optional in-request second-stage reranker."""
    url = f"{settings.java_backend_url}/api/rag/knowledge-base/search"
    payload = {"query": query, "topK": top_k, "rerank": bool(rerank)}
    async with httpx.AsyncClient(timeout=60.0 if rerank else 30.0) as client:
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
    """In-process deterministic timeline check for quick validation paths."""
    return json.dumps(check_timeline({"resumeText": resume_text}),
                      ensure_ascii=False)
