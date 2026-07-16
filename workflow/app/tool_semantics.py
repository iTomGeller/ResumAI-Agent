from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Dict, List, Optional

TOOL_SEMANTICS: Dict[str, Dict[str, Any]] = {
    "resume_structure_extract": {
        "origin": "local",
        "family": "tool",
        "operation": "parse_resume",
    },
    "milvus_resume_search": {
        "origin": "rag",
        "family": "retrieval",
        "operation": "resume_search",
        "backend": "milvus",
    },
    "milvus_resume_batch_search": {
        "origin": "rag",
        "family": "retrieval",
        "operation": "resume_batch_search",
        "backend": "milvus",
    },
    "milvus_jd_search": {
        "origin": "rag",
        "family": "retrieval",
        "operation": "jd_search",
        "backend": "milvus",
    },
    "github_enrichment": {
        "origin": "external",
        "family": "external_enrichment",
        "operation": "profile_enrichment",
    },
    "timeline_validator": {
        "origin": "local",
        "family": "tool",
        "operation": "timeline_validate",
    },
    "jd_requirements_extract": {
        "origin": "local",
        "family": "tool",
        "operation": "jd_extract",
    },
    "evidence_merge": {
        "origin": "local",
        "family": "tool",
        "operation": "evidence_merge",
    },
    "list_skills": {
        "origin": "skill",
        "family": "skill",
        "operation": "skill_discovery",
    },
    "execute_skill": {
        "origin": "skill",
        "family": "skill",
        "operation": "skill_execute",
    },
    "mcp_resume_evidence_search": {
        "origin": "mcp",
        "family": "retrieval",
        "operation": "resume_search",
        "server": "resume-tools",
        "protocol": "stdio",
        "backend": "mcp",
        "downstreamApi": "/api/internal/tools/resume-search",
    },
    "mcp_external_profile_search": {
        "origin": "mcp",
        "family": "external_enrichment",
        "operation": "profile_enrichment",
        "server": "resume-tools",
        "protocol": "stdio",
        "downstreamApi": "/api/internal/tools/external-profile",
    },
}

RAG_TOOL_NAMES = frozenset(
    {
        "milvus_resume_search",
        "milvus_resume_batch_search",
        "milvus_jd_search",
        "mcp_resume_evidence_search",
    }
)

TOOL_BUDGET_BY_AGENT: Dict[str, int] = {
    "TechEvalAgent": 4,
    "ProjectEvalAgent": 3,
    "RiskAgent": 3,
    "JdMatchAgent": 3,
    "EvidenceFusionAgent": 2,
}


def get_tool_semantics(tool_name: str) -> Dict[str, Any]:
    if tool_name in TOOL_SEMANTICS:
        return dict(TOOL_SEMANTICS[tool_name])
    if tool_name.startswith("mcp_"):
        return {
            "origin": "mcp",
            "family": "mcp",
            "operation": tool_name,
            "server": "resume-tools",
            "protocol": "stdio",
        }
    if "skill" in tool_name:
        return {"origin": "skill", "family": "skill", "operation": tool_name}
    return {"origin": "local", "family": "tool", "operation": tool_name}


def observation_kind_for(semantics: Dict[str, Any]) -> str:
    return "tool_span"


def langfuse_action_name(tool_name: str, semantics: Dict[str, Any], tool_input: Dict[str, Any]) -> str:
    return tool_name


def stable_input_hash(tool_input: Any) -> str:
    try:
        normalized = json.dumps(tool_input, sort_keys=True, ensure_ascii=False, default=str)
    except TypeError:
        normalized = str(tool_input)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def tool_signature(tool_name: str, tool_input: Any) -> str:
    return f"{tool_name}:{stable_input_hash(tool_input)}"


def is_rag_tool(tool_name: str, semantics: Optional[Dict[str, Any]] = None) -> bool:
    if tool_name in RAG_TOOL_NAMES:
        return True
    if semantics:
        return semantics.get("family") == "retrieval" or semantics.get("origin") == "rag"
    return get_tool_semantics(tool_name).get("family") == "retrieval"


def extract_retrieval_metadata(
    tool_name: str,
    result_str: str,
    semantics: Dict[str, Any],
    tool_input: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    if not is_rag_tool(tool_name, semantics):
        return None
    data: Dict[str, Any] = {}
    try:
        parsed = json.loads(result_str)
        if isinstance(parsed, dict):
            data = parsed
    except json.JSONDecodeError:
        pass
    query = (
        tool_input.get("query")
        or tool_input.get("input")
        or data.get("query")
        or ""
    )
    top_k = tool_input.get("topK") or tool_input.get("top_k") or data.get("topK") or 5
    chunks = data.get("chunks")
    if isinstance(chunks, list):
        hit_count = len(chunks)
    else:
        hit_count = data.get("hitCount", 0)
    return {
        "backend": semantics.get("backend") or data.get("backend") or ("mcp" if semantics.get("origin") == "mcp" else "milvus"),
        "query": str(query),
        "topK": int(top_k) if top_k else 5,
        "hitCount": int(hit_count) if hit_count else 0,
        "topScore": float(data.get("topScore", 0) or 0),
        "fallbackUsed": bool(data.get("fallbackUsed", False)),
        "fallbackReason": data.get("fallbackReason") or data.get("error"),
        "strategy": data.get("strategy"),
        "errorType": data.get("errorType") or (data.get("error") if data.get("error") else None),
        "usedResumeTextFallback": bool(data.get("usedResumeTextFallback", False)),
        "chunks": chunks if isinstance(chunks, list) else [],
        "selectedChunks": data.get("selectedChunks") if isinstance(data.get("selectedChunks"), list) else [],
        "usefulnessScore": float(data.get("usefulnessScore", 0) or 0),
        "rerankStrategy": data.get("rerankStrategy"),
    }


def build_tool_substeps(
    tool_name: str,
    tool_input: Dict[str, Any],
    result_str: str,
    started_at: str,
    ended_at: str,
    duration_ms: int,
    status: str,
    semantics: Dict[str, Any],
) -> List[Dict[str, Any]]:
    substeps: List[Dict[str, Any]] = [
        {
            "name": "agent_tool_call",
            "kind": "agent_request",
            "status": status,
            "startedAt": started_at,
            "endedAt": ended_at,
            "durationMs": duration_ms,
            "summary": f"Agent 请求 {tool_name}",
        }
    ]
    origin = semantics.get("origin", "local")
    if origin == "mcp":
        substeps.append(
            {
                "name": "mcp_protocol",
                "kind": "mcp_protocol",
                "status": status,
                "summary": f"MCP stdio / {semantics.get('server', 'resume-tools')}",
                "metadata": {
                    "protocol": semantics.get("protocol", "stdio"),
                    "server": semantics.get("server"),
                    "downstreamApi": semantics.get("downstreamApi"),
                },
            }
        )
    if origin == "skill":
        skill_name = tool_input.get("skillName") or tool_input.get("skill_name") or tool_name
        phase = "skill_discovery" if tool_name == "list_skills" else "skill_execute"
        substeps.append(
            {
                "name": phase,
                "kind": "skill_load" if tool_name == "list_skills" else "skill_execute",
                "status": status,
                "summary": f"Skill {skill_name}: {semantics.get('operation', 'execute')}",
                "metadata": {"skillName": skill_name, "task": tool_input.get("task", "")},
            }
        )
        if tool_name == "execute_skill":
            substeps.append(
                {
                    "name": "skill_apply_next_round",
                    "kind": "skill_execute",
                    "status": "PENDING",
                    "summary": "指令将在下一轮 LLM round 中应用",
                }
            )
    if is_rag_tool(tool_name, semantics):
        retrieval = extract_retrieval_metadata(tool_name, result_str, semantics, tool_input)
        substeps.append(
            {
                "name": "retrieval",
                "kind": "retrieval",
                "status": status,
                "summary": _retrieval_substep_summary(retrieval),
                "metadata": retrieval or {},
            }
        )
        if retrieval and retrieval.get("fallbackUsed"):
            substeps.append(
                {
                    "name": "fallback",
                    "kind": "fallback",
                    "status": "WARNING",
                    "summary": f"降级: {retrieval.get('fallbackReason') or 'unknown'}",
                }
            )
    return substeps


def _retrieval_substep_summary(retrieval: Optional[Dict[str, Any]]) -> str:
    if not retrieval:
        return "检索完成"
    return (
        f"backend={retrieval.get('backend')} "
        f"hits={retrieval.get('hitCount')}/{retrieval.get('topK')} "
        f"topScore={retrieval.get('topScore')}"
    )


def rag_failure_key(tool_name: str, semantics: Dict[str, Any]) -> str:
    backend = semantics.get("backend") or semantics.get("origin") or "rag"
    return f"{backend}:{semantics.get('family', 'retrieval')}"


def is_malformed_final_output(text: str) -> bool:
    if not text or not text.strip():
        return True
    if "DSML" in text and "tool_calls" in text:
        return True
    if text.strip() in ("{}", "[]", "null"):
        return True
    return False


def resume_text_fallback_chunks(resume_text: str, query: str, top_k: int = 5) -> List[str]:
    if not resume_text or not query:
        return []
    keywords = [w for w in re.split(r"[\s,，、/|；;]+", query) if len(w) >= 2]
    if not keywords:
        keywords = [query[:20]]
    paragraphs = [p.strip() for p in re.split(r"\n{2,}", resume_text) if p.strip()]
    if not paragraphs:
        paragraphs = [line.strip() for line in resume_text.splitlines() if line.strip()]
    scored: List[tuple[int, str]] = []
    for para in paragraphs:
        score = sum(1 for kw in keywords if kw.lower() in para.lower())
        if score > 0:
            scored.append((score, para[:500]))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [p for _, p in scored[:top_k]]
