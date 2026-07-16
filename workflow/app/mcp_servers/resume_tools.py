"""Real MCP stdio server exposing resume evidence search via Java internal API."""
from __future__ import annotations

import json
import os

import httpx
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("resume-tools")

JAVA_BACKEND_URL = os.environ.get("JAVA_BACKEND_URL", "http://ai-resume-backend:8080")
INTERNAL_TOKEN = os.environ.get("WORKFLOW_INTERNAL_TOKEN", "")

if not INTERNAL_TOKEN:
    print("WARNING: WORKFLOW_INTERNAL_TOKEN is missing; MCP tools will return MCP_TOKEN_MISSING")


def _headers() -> dict[str, str]:
    return {"Content-Type": "application/json", "X-Internal-Token": INTERNAL_TOKEN}


def _token_error() -> str:
    return json.dumps(
        {
            "error": "MCP_TOKEN_MISSING",
            "errorType": "MCP_TOKEN_MISSING",
            "fallbackUsed": True,
            "fallbackReason": "MCP_TOKEN_MISSING",
            "hitCount": 0,
            "topScore": 0,
        },
        ensure_ascii=False,
    )


@mcp.tool()
async def mcp_resume_evidence_search(
    query: str,
    top_k: int = 5,
    resume_text: str = "",
) -> str:
    """Search resume chunks via Milvus RAG (real MCP protocol tool)."""
    if not INTERNAL_TOKEN:
        return _token_error()
    url = f"{JAVA_BACKEND_URL}/api/internal/tools/resume-search"
    payload = {"query": query, "topK": top_k, "resumeText": resume_text, "jdRequirements": ""}
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(url, json=payload, headers=_headers())
        if resp.status_code == 401:
            return json.dumps(
                {
                    "error": "Unauthorized",
                    "errorType": "HTTP_401",
                    "fallbackUsed": True,
                    "fallbackReason": "HTTP_401",
                    "hitCount": 0,
                    "topScore": 0,
                },
                ensure_ascii=False,
            )
        resp.raise_for_status()
        return json.dumps(resp.json(), ensure_ascii=False)


@mcp.tool()
async def mcp_external_profile_search(resume_text: str) -> str:
    """Fetch external profile enrichment summary for a resume."""
    if not INTERNAL_TOKEN:
        return _token_error()
    url = f"{JAVA_BACKEND_URL}/api/internal/tools/external-profile"
    payload = {"resumeText": resume_text}
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(url, json=payload, headers=_headers())
        if resp.status_code == 401:
            return json.dumps(
                {
                    "error": "Unauthorized",
                    "errorType": "HTTP_401",
                    "fallbackUsed": True,
                    "fallbackReason": "HTTP_401",
                },
                ensure_ascii=False,
            )
        resp.raise_for_status()
        return json.dumps(resp.json(), ensure_ascii=False)


if __name__ == "__main__":
    mcp.run(transport="stdio")
