from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any, Dict, List, Optional

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

_URL_PATTERN = re.compile(r"https?://[^\s,;）)\"'，；]+", re.I)


def extract_primary_url(text: str) -> str:
    """First external URL in the resume (GitHub/blog/portfolio) for public-MCP enrichment."""
    if not text:
        return ""
    for match in _URL_PATTERN.findall(text):
        url = match.rstrip(".)，。")
        if "github.com" in url.lower() or re.search(r"blog|medium|dev\.to|juejin|cnblogs|segmentfault|gitee|gitlab", url, re.I):
            return url
    first = _URL_PATTERN.search(text)
    return first.group(0).rstrip(".)，。") if first else ""


async def mcp_fetch_url(url: str, max_chars: int = 1200, timeout_s: float = 8.0) -> Dict[str, Any]:
    """Fetch a public web page via the official public MCP server `mcp-server-fetch`.

    Real public MCP integration (Model Context Protocol). Fully guarded: any failure or timeout
    returns an empty/error result so the workflow never blocks on external content.
    """
    if not url:
        return {"ok": False, "reason": "no_url", "server": "mcp-server-fetch"}
    try:
        async def _do() -> Dict[str, Any]:
            from langchain_mcp_adapters.client import MultiServerMCPClient

            client = MultiServerMCPClient({
                "fetch": {"transport": "stdio", "command": "python", "args": ["-m", "mcp_server_fetch"]}
            })
            tools = await client.get_tools()
            fetch_tool = next((t for t in tools if getattr(t, "name", "") == "fetch"), None)
            if fetch_tool is None:
                return {"ok": False, "reason": "fetch_tool_unavailable", "server": "mcp-server-fetch"}
            raw = await fetch_tool.ainvoke({"url": url, "max_length": max_chars})
            content = raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False)
            return {
                "ok": True,
                "server": "mcp-server-fetch",
                "protocol": "MCP/stdio",
                "url": url,
                "contentPreview": content[:max_chars],
            }

        return await asyncio.wait_for(_do(), timeout=timeout_s)
    except asyncio.TimeoutError:
        return {"ok": False, "reason": "timeout", "server": "mcp-server-fetch", "url": url}
    except Exception as exc:  # noqa: BLE001 - external MCP must never break the pipeline
        return {"ok": False, "reason": str(exc)[:160], "server": "mcp-server-fetch", "url": url}


async def mcp_current_time(timezone: str = "Asia/Shanghai", timeout_s: float = 6.0) -> Dict[str, Any]:
    """Authoritative evaluation reference time via the official public MCP server `mcp-server-time`.

    Used to ground timeline-risk reasoning (e.g. future internships are not gaps). Fully guarded.
    """
    try:
        async def _do() -> Dict[str, Any]:
            from langchain_mcp_adapters.client import MultiServerMCPClient

            client = MultiServerMCPClient({
                "time": {"transport": "stdio", "command": "python", "args": ["-m", "mcp_server_time"]}
            })
            tools = await client.get_tools()
            time_tool = next((t for t in tools if getattr(t, "name", "") in ("get_current_time", "current_time")), None)
            if time_tool is None:
                return {"ok": False, "reason": "time_tool_unavailable", "server": "mcp-server-time"}
            raw = await time_tool.ainvoke({"timezone": timezone})
            content = raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False)
            return {"ok": True, "server": "mcp-server-time", "protocol": "MCP/stdio", "timezone": timezone, "result": content[:400]}

        return await asyncio.wait_for(_do(), timeout=timeout_s)
    except asyncio.TimeoutError:
        return {"ok": False, "reason": "timeout", "server": "mcp-server-time"}
    except Exception as exc:  # noqa: BLE001 - external MCP must never break the pipeline
        return {"ok": False, "reason": str(exc)[:160], "server": "mcp-server-time"}


def _headers() -> Dict[str, str]:
    return {
        "Content-Type": "application/json",
        "X-Internal-Token": settings.workflow_internal_token,
    }


def _first_text(args: Dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = args.get(key)
        if value is not None and str(value).strip():
            return str(value)
    return ""


def _int_arg(args: Dict[str, Any], default: int, *keys: str) -> int:
    for key in keys:
        if args.get(key) is not None:
            return int(args.get(key))
    return default


async def java_resume_search(query: str, top_k: int = 5, resume_text: str = "", jd_requirements: str = "", strategy: str = "hybrid") -> str:
    url = f"{settings.java_backend_url}/api/internal/tools/resume-search"
    payload = {"query": query, "topK": top_k, "resumeText": resume_text, "jdRequirements": jd_requirements, "strategy": strategy}
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


async def java_external_profile(resume_text: str) -> str:
    url = f"{settings.java_backend_url}/api/internal/tools/external-profile"
    payload = {"resumeText": resume_text}
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(url, json=payload, headers=_headers())
        resp.raise_for_status()
        data = resp.json()
        return data.get("summary", "") if isinstance(data, dict) else str(data)


async def java_list_skills() -> str:
    url = f"{settings.java_backend_url}/api/internal/tools/skills/list"
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(url, json={}, headers=_headers())
        resp.raise_for_status()
        return json.dumps(resp.json(), ensure_ascii=False)


async def java_execute_skill(skill_name: str, task: str) -> str:
    url = f"{settings.java_backend_url}/api/internal/tools/skills/execute"
    payload = {"skillName": skill_name, "task": task}
    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.post(url, json=payload, headers=_headers())
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, dict):
            return json.dumps(data, ensure_ascii=False)
        return str(data)


async def java_knowledge_search(query: str, top_k: int = 5) -> str:
    url = f"{settings.java_backend_url}/api/rag/knowledge-base/search"
    payload = {"query": query, "topK": top_k}
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(url, json=payload)
        resp.raise_for_status()
        return json.dumps(resp.json(), ensure_ascii=False)


async def java_memory_search(query: str, top_k: int = 5) -> str:
    url = f"{settings.java_backend_url}/api/internal/tools/memory/search"
    payload = {"query": query, "topK": top_k}
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(url, json=payload, headers=_headers())
        resp.raise_for_status()
        return json.dumps(resp.json(), ensure_ascii=False)


async def resume_structure_extract(resume_text: str) -> str:
    text = resume_text or ""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    sections: list[str] = []
    skills: list[str] = []
    projects: list[dict[str, str]] = []
    timeline_entries: list[str] = []

    skill_pattern = re.compile(
        r"(Java|Spring Boot|Kafka|K8s|Kubernetes|Redis|MySQL|Docker|Milvus|RAG|LLM)",
        re.I,
    )
    date_pattern = re.compile(r"(20\d{2}[./年-]?\d{0,2}|19\d{2}[./年-]?\d{0,2})")

    for line in lines:
        if line.startswith("#") or line.endswith(":") or line.endswith("："):
            sections.append(line)
        for match in skill_pattern.findall(line):
            if match not in skills:
                skills.append(match)
        if any(keyword in line for keyword in ("项目", "重构", "平台", "系统", "中台")):
            projects.append({"name": line[:60], "description": line[:300]})
        if date_pattern.search(line):
            timeline_entries.append(line[:200])

    return json.dumps(
        {
            "textLength": len(text),
            "sections": sections[:30],
            "skills": skills,
            "projects": projects[:20],
            "timelineEntries": timeline_entries[:20],
            "rawTextDigest": text[:2000],
            "preview": text[:500],
            "coverageWarnings": [] if len(text) >= 300 else ["input_resume_text_is_short"],
        },
        ensure_ascii=False,
    )


async def jd_requirements_extract(jd_match_json: str) -> str:
    try:
        data = json.loads(jd_match_json)
        items = data.get("items") if isinstance(data, dict) else data
        if isinstance(items, list) and items:
            top = items[0]
            return json.dumps({
                "title": top.get("title"),
                "requirements": top.get("matchReasons", []),
                "gaps": top.get("gaps", []),
            }, ensure_ascii=False)
        if isinstance(data, list) and data:
            top = data[0]
            return json.dumps({
                "title": top.get("title"),
                "requirements": top.get("matchReasons", []),
                "gaps": top.get("gaps", []),
            }, ensure_ascii=False)
        return jd_match_json
    except json.JSONDecodeError:
        return jd_match_json


async def timeline_validator(resume_text: str) -> str:
    return json.dumps({"status": "OK", "note": "timeline validation delegated to Java RAG pipeline"}, ensure_ascii=False)


async def evidence_merge(tech: str, project: str, risk: str) -> str:
    return json.dumps({
        "techSummary": tech[:300] if tech else "",
        "projectSummary": project[:300] if project else "",
        "riskSummary": risk[:300] if risk else "",
        "merged": True,
    }, ensure_ascii=False)


async def github_enrichment(resume_text: str) -> str:
    return await java_external_profile(resume_text)


async def milvus_resume_search(query: str, top_k: int = 5, resume_text: str = "", strategy: str = "hybrid") -> str:
    return await java_resume_search(query, top_k, resume_text, strategy=strategy)


async def milvus_resume_batch_search(queries: list, top_k: int = 5, resume_text: str = "", strategy: str = "hybrid") -> str:
    results_by_query: Dict[str, Any] = {}
    total_hits = 0
    max_score = 0.0
    any_fallback = False
    fallback_reasons: list[str] = []
    query_list = queries if isinstance(queries, list) else [queries]
    for q in query_list[:4]:
        if not str(q).strip():
            continue
        raw = await java_resume_search(str(q), top_k, resume_text, strategy=strategy)
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            data = {"error": raw, "query": q}
        results_by_query[str(q)] = data
        if isinstance(data, dict):
            total_hits += int(data.get("hitCount", 0) or 0)
            max_score = max(max_score, float(data.get("topScore", 0) or 0))
            if data.get("fallbackUsed"):
                any_fallback = True
                reason = data.get("fallbackReason")
                if reason:
                    fallback_reasons.append(str(reason))
    return json.dumps(
        {
            "resultsByQuery": results_by_query,
            "queryCount": len(results_by_query),
            "hitCount": total_hits,
            "topScore": max_score,
            "fallbackUsed": any_fallback,
            "fallbackReason": ",".join(fallback_reasons) if fallback_reasons else None,
            "backend": "milvus",
            "strategy": f"batch_{strategy}",
        },
        ensure_ascii=False,
    )


async def milvus_jd_search(resume_text: str, top_k: int = 3) -> str:
    return await java_jd_search(resume_text, top_k)


async def list_skills() -> str:
    return await java_list_skills()


async def execute_skill(skill_name: str, task: str = "") -> str:
    return await java_execute_skill(skill_name, task)


async def knowledge_search(query: str, top_k: int = 5) -> str:
    return await java_knowledge_search(query, top_k)


async def memory_search(query: str, top_k: int = 5) -> str:
    return await java_memory_search(query, top_k)


TOOL_HANDLERS = {
    "resume_structure_extract": lambda args: resume_structure_extract(
        _first_text(args, "resumeText", "resume_text", "query", "input")
    ),
    "milvus_jd_search": lambda args: milvus_jd_search(
        _first_text(args, "resumeText", "resume_text", "query", "input"),
        _int_arg(args, 3, "topK", "top_k"),
    ),
    "jd_requirements_extract": lambda args: jd_requirements_extract(
        _first_text(args, "jdMatchJson", "jd_match_json", "input")
    ),
    "milvus_resume_search": lambda args: milvus_resume_search(
        _first_text(args, "query", "input", "resumeText", "resume_text"),
        _int_arg(args, 5, "topK", "top_k"),
        _first_text(args, "resumeText", "resume_text"),
        _first_text(args, "strategy"),
    ),
    "milvus_resume_batch_search": lambda args: milvus_resume_batch_search(
        args.get("queries") or args.get("query") or [],
        _int_arg(args, 5, "topK", "top_k"),
        _first_text(args, "resumeText", "resume_text"),
        _first_text(args, "strategy") or "hybrid",
    ),
    "github_enrichment": lambda args: github_enrichment(
        _first_text(args, "resumeText", "resume_text", "query", "input")
    ),
    "timeline_validator": lambda args: timeline_validator(
        _first_text(args, "resumeText", "resume_text", "query", "input")
    ),
    "evidence_merge": lambda args: evidence_merge(
        _first_text(args, "techResult", "tech_result"),
        _first_text(args, "projectResult", "project_result"),
        _first_text(args, "riskResult", "risk_result"),
    ),
    "list_skills": lambda args: list_skills(),
    "execute_skill": lambda args: execute_skill(
        _first_text(args, "skillName", "skill_name"),
        _first_text(args, "task"),
    ),
    "knowledge_search": lambda args: knowledge_search(
        _first_text(args, "query", "input"),
        _int_arg(args, 5, "topK", "top_k"),
    ),
    "memory_search": lambda args: memory_search(
        _first_text(args, "query", "resumeText", "resume_text", "input"),
        _int_arg(args, 5, "topK", "top_k"),
    ),
}


async def execute_tool(name: str, arguments: Dict[str, Any]) -> str:
    handler = TOOL_HANDLERS.get(name)
    if not handler:
        return json.dumps({"error": f"unknown tool: {name}", "tool": name}, ensure_ascii=False)
    try:
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError:
                arguments = {"input": arguments}
        result = await handler(arguments or {})
        return result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
    except Exception as exc:
        logger.warning("tool %s failed: %s", name, exc)
        return json.dumps({"error": str(exc), "tool": name}, ensure_ascii=False)
