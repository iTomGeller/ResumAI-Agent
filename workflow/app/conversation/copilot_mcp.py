"""
Copilot MCP Server: internal MCP tools for the ReAct agent.
Registered as server 'copilot-qa' in the MCP registry.
Tools read from contextSnapshot/database, never call external APIs.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

COPILOT_MCP_REGISTRY: Dict[str, Dict[str, Any]] = {
    "search_report": {
        "description": "搜索当前评估报告中的特定信息。支持查询：分数、风险、优势、建议、某维度详情。",
        "parameters": {
            "query": {"type": "string", "description": "搜索关键词，如'技术风险'/'项目深度'/'推荐理由'"},
        },
        "mcpServer": "copilot-qa",
        "kind": "mcp",
    },
    "get_dimension_detail": {
        "description": "获取某个评估维度的详细评分、证据和改进建议。",
        "parameters": {
            "dimension": {"type": "string", "description": "维度名称: 技术能力/项目深度/JD匹配/综合潜力"},
        },
        "mcpServer": "copilot-qa",
        "kind": "mcp",
    },
    "compare_candidates": {
        "description": "将当前候选人与历史评估候选人横向对比。返回同岗位候选人平均分和排名。",
        "parameters": {
            "aspect": {"type": "string", "description": "对比维度: overall/tech/risk/jd_match"},
        },
        "mcpServer": "copilot-qa",
        "kind": "mcp",
    },
    "generate_interview_question": {
        "description": "基于评估报告中的风险或能力gap，生成有针对性的面试追问。",
        "parameters": {
            "focus": {"type": "string", "description": "追问方向: 项目贡献/技术深度/风险验证/文化匹配"},
            "count": {"type": "integer", "description": "生成问题数量(1-5)", "default": 3},
        },
        "mcpServer": "copilot-qa",
        "kind": "mcp",
    },
    "fetch_jd_gaps": {
        "description": "获取候选人与目标JD之间的能力缺口详情，包括缺失技能和验证建议。",
        "parameters": {
            "jd_title": {"type": "string", "description": "JD标题(可选，空则取最佳匹配)", "default": ""},
        },
        "mcpServer": "copilot-qa",
        "kind": "mcp",
    },
}


async def call_mcp_tool(
    tool_name: str,
    args: Dict[str, Any],
    context_snapshot: Dict[str, Any],
) -> Any:
    """Execute a copilot MCP tool against the context snapshot."""
    handler = _HANDLERS.get(tool_name)
    if not handler:
        return {"error": f"Unknown tool: {tool_name}"}
    try:
        return await handler(args, context_snapshot)
    except Exception as exc:
        logger.warning("copilot_mcp tool %s failed: %s", tool_name, exc)
        return {"error": str(exc)[:200]}


async def _search_report(args: Dict, ctx: Dict) -> Dict:
    """Search structured report for matching content."""
    query = (args.get("query") or "").lower()
    report = ctx.get("structuredReport") or {}
    if not isinstance(report, dict):
        return {"found": False, "message": "当前无可用评估报告"}

    results: Dict[str, Any] = {}

    for dim in (report.get("dimensions") or []):
        if isinstance(dim, dict) and query in (dim.get("name", "") + dim.get("evidence", "")).lower():
            results[dim.get("name", "?")] = {
                "score": dim.get("score"),
                "evidence": dim.get("evidence", "")[:200],
                "suggestions": dim.get("suggestions", [])[:2],
            }

    if "风险" in query or "risk" in query:
        results["risks"] = [
            {"claim": r.get("claim", "")[:80], "severity": r.get("severity"),
             "evidence": r.get("evidence", "")[:100]}
            for r in (report.get("risks") or [])[:5] if isinstance(r, dict)
        ]

    if "优势" in query or "强" in query or "strength" in query:
        results["strengths"] = (report.get("strengths") or [])[:5]

    if "推荐" in query or "结论" in query or "总" in query or "overall" in query:
        results["recommendation"] = report.get("recommendation")
        results["overallScore"] = report.get("overallScore")
        results["rationale"] = report.get("decisionRationale", "")[:200]

    if not results:
        results["summary"] = {
            "recommendation": report.get("recommendation"),
            "overallScore": report.get("overallScore"),
            "dimensionCount": len(report.get("dimensions") or []),
            "riskCount": len(report.get("risks") or []),
        }
    return {"found": True, "results": results}


async def _get_dimension(args: Dict, ctx: Dict) -> Dict:
    """Get detailed dimension info."""
    target = (args.get("dimension") or "").lower()
    report = ctx.get("structuredReport") or {}
    for dim in (report.get("dimensions") or []):
        if isinstance(dim, dict) and target in dim.get("name", "").lower():
            return {
                "name": dim.get("name"),
                "score": dim.get("score"),
                "weight": dim.get("weight"),
                "evidence": dim.get("evidence", "")[:300],
                "suggestions": dim.get("suggestions", [])[:3],
                "subDimensions": dim.get("subDimensions", [])[:4],
            }
    available = [d.get("name") for d in (report.get("dimensions") or []) if isinstance(d, dict)]
    return {"error": f"维度'{target}'未找到", "available": available}


async def _compare_candidates(args: Dict, ctx: Dict) -> Dict:
    """Compare with historical candidates using memory anchors."""
    aspect = args.get("aspect", "overall")
    report = ctx.get("structuredReport") or {}
    current_score = report.get("overallScore") or 0
    current_rec = report.get("recommendation") or "?"

    anchors = ctx.get("memoryAnchors") or []
    if anchors:
        same_job = [a for a in anchors if isinstance(a, dict)]
        if same_job:
            avg_score = sum(a.get("overallScore", 0) for a in same_job) / len(same_job)
            return {
                "current": {"score": current_score, "recommendation": current_rec},
                "historicalAvg": round(avg_score, 1),
                "historicalCount": len(same_job),
                "ranking": f"当前候选人在{len(same_job)}位历史候选人中排第"
                           f"{'1' if current_score >= avg_score else '2+'}位",
            }

    return {
        "current": {"score": current_score, "recommendation": current_rec},
        "comparison": "暂无同岗位历史候选人对比数据",
        "note": "后续评估完成后将自动积累对比基准",
    }


async def _gen_interview_question(args: Dict, ctx: Dict) -> Dict:
    """Generate interview questions based on risks/gaps."""
    focus = (args.get("focus") or "").lower()
    count = min(int(args.get("count", 3)), 5)
    report = ctx.get("structuredReport") or {}

    probes = report.get("interviewProbes") or []
    relevant = [p for p in probes if isinstance(p, dict)
                and focus in (p.get("dimension", "") + p.get("question", "")).lower()]
    if not relevant:
        relevant = probes[:count]

    questions = [p.get("question", "") for p in relevant[:count] if isinstance(p, dict)]

    if len(questions) < count:
        risks = report.get("risks") or []
        for r in risks:
            if isinstance(r, dict) and len(questions) < count:
                claim = r.get("claim", "?")[:30]
                questions.append(
                    f"关于「{claim}」：请具体描述当时的场景、你的角色、和最终可量化的结果。")

    return {"questions": questions[:count], "focus": focus or "综合", "source": "report_probes+risks"}


async def _fetch_jd_gaps(args: Dict, ctx: Dict) -> Dict:
    """Fetch JD gap details from topJdMatches."""
    jd_title = (args.get("jd_title") or "").strip()
    matches = ctx.get("topJdMatches") or []

    if not matches:
        return {"error": "当前无JD匹配数据", "suggestion": "请先上传JD或等待评估完成"}

    target = None
    for m in matches:
        if not isinstance(m, dict):
            continue
        if jd_title and jd_title.lower() in (m.get("title") or "").lower():
            target = m
            break
    if not target:
        target = matches[0] if matches else {}

    return {
        "jdTitle": target.get("title"),
        "matchScore": target.get("matchScore"),
        "gaps": target.get("gaps", [])[:5],
        "interviewChecks": target.get("interviewChecks", [])[:3],
        "skillMatch": target.get("skillMatchScore"),
        "experienceMatch": target.get("experienceMatchScore"),
    }


_HANDLERS = {
    "search_report": _search_report,
    "get_dimension_detail": _get_dimension,
    "compare_candidates": _compare_candidates,
    "generate_interview_question": _gen_interview_question,
    "fetch_jd_gaps": _fetch_jd_gaps,
}
