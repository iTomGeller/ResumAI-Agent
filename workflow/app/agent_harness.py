from __future__ import annotations

import json
import re
from typing import Any, Dict, List


DEFAULT_AGENT_TOOL_BUDGETS: Dict[str, Dict[str, int]] = {
    "ResumeParseAgent": {"maxToolCalls": 1, "maxRetrievalQueries": 0},
    "JdMatchAgent": {"maxToolCalls": 2, "maxRetrievalQueries": 1},
    "TechEvalAgent": {"maxToolCalls": 2, "maxRetrievalQueries": 3},
    "ProjectEvalAgent": {"maxToolCalls": 1, "maxRetrievalQueries": 3},
    "RiskAgent": {"maxToolCalls": 2, "maxRetrievalQueries": 3},
    "EvidenceFusionAgent": {"maxToolCalls": 0, "maxRetrievalQueries": 0},
    "ReportAgent": {"maxToolCalls": 1, "maxRetrievalQueries": 0},
}


def parse_json_object(raw: str | None) -> Dict[str, Any]:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def build_harness_plan(
    intent_result: str,
    resume_text: str,
    job_category: str = "",
    harness_context: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    intent = parse_json_object(intent_result)
    context = harness_context or {}
    required_skills = _as_str_list(intent.get("requiredSkills"))
    routing_hints = _as_str_list(intent.get("routingHints"))
    evidence_gaps = _as_str_list(intent.get("evidenceGaps"))
    interview_focus = _as_str_list(intent.get("interviewFocus"))
    candidate_type = str(intent.get("candidateType") or job_category or "UNKNOWN")
    experience_level = str(intent.get("experienceLevel") or "UNKNOWN")
    resume_len = len(resume_text or "")
    complexity = classify_complexity(resume_text, required_skills, routing_hints)
    memory_context = normalize_memory_context(context)
    knowledge_hits = normalize_knowledge_hits(context)
    memory_hits = (
        _as_list(memory_context.get("episodicHits"))
        + _as_list(memory_context.get("semanticHits"))
        + _as_list(memory_context.get("proceduralHits"))
    )
    has_tech_signal = bool(re.search(r"Java|Spring|Kafka|K8s|Kubernetes|Redis|MySQL|Docker|Milvus|RAG|LLM|后端|系统|平台", resume_text or "", re.I))
    has_project_signal = bool(re.search(r"项目|平台|系统|中台|架构|重构|负责|上线|指标|性能|高并发", resume_text or "", re.I))
    has_metric_signal = bool(re.search(r"\d+%|\d+倍|QPS|TPS|P99|RT|ms|秒|分钟|成本|收入|DAU|PV|UV", resume_text or "", re.I))
    has_timeline_signal = bool(re.search(r"20\d{2}|19\d{2}|至今|实习|毕业|入职|离职", resume_text or "", re.I))
    risk_memory_hits = [
        hit for hit in _as_list(memory_context.get("proceduralHits")) + _as_list(memory_context.get("semanticHits"))
        if isinstance(hit, dict)
        and float(hit.get("matchScore") or 0) >= 0.45
        and re.search(r"risk_eval|风险|真实性|时间线|证据|gap|缺口", str(hit.get("appliesTo", "")), re.I)
    ]
    meaningful_gaps = [
        gap for gap in evidence_gaps
        if not re.search(r"unknown|未明确|待定|一般|通用", gap, re.I)
    ]
    should_run_risk = bool(
        meaningful_gaps
        or resume_len < 900
        or risk_memory_hits
        or "risk" in " ".join(routing_hints).lower()
        or (not has_metric_signal and not has_timeline_signal)
    )

    base_agents = ["resume_parse", "jd_match", "evidence_fusion", "report"]
    all_optional = {"tech_eval", "project_eval", "risk_eval"}

    if complexity == "sparse":
        route_mode = "FAST_SCREEN"
        selected_agents = base_agents + ["risk_eval"]
    elif has_tech_signal and has_project_signal and not should_run_risk:
        route_mode = "TECH_DEEP_DIVE"
        selected_agents = base_agents + ["tech_eval", "project_eval"]
    elif has_tech_signal and not has_project_signal:
        route_mode = "TECH_SCREEN"
        selected_agents = base_agents + ["tech_eval", "risk_eval"]
    elif has_project_signal and should_run_risk:
        route_mode = "PROJECT_AUTHENTICITY_REVIEW"
        selected_agents = base_agents + ["project_eval", "risk_eval"]
    elif should_run_risk:
        route_mode = "RISK_REVIEW"
        selected_agents = base_agents + ["risk_eval"]
    else:
        route_mode = "FULL_REVIEW"
        selected_agents = base_agents + ["tech_eval", "project_eval", "risk_eval"]

    def reason_for_skip(agent: str) -> str:
        if agent == "tech_eval":
            return "未检测到技术岗位/技术栈信号"
        if agent == "project_eval":
            return "简历缺少项目/平台/职责边界信号"
        if agent == "risk_eval":
            return "证据、时间线和量化指标较完整，未命中风险型 Memory"
        return "当前路由模式不需要该 Agent"

    skipped_agents = {
        agent: reason_for_skip(agent)
        for agent in all_optional
        if agent not in selected_agents
    }
    enabled_agents = dedupe_keep_order(selected_agents)

    why_selected: List[str] = []
    if "tech_eval" in enabled_agents:
        why_selected.append("检测到技术栈/岗位信号，启用 TechEvalAgent")
    if "project_eval" in enabled_agents:
        why_selected.append("检测到项目/平台/职责信号，启用 ProjectEvalAgent")
    if "risk_eval" in enabled_agents:
        why_selected.append("存在 JD gap、时间线/指标缺口或风险 Memory，启用 RiskAgent")
    if route_mode == "FAST_SCREEN":
        why_selected.append("短简历走 FAST_SCREEN，仅保留核心解析与风险验证")

    why_skipped = [f"{agent}: {reason}" for agent, reason in skipped_agents.items()]

    if not skipped_agents:
        signals: List[str] = []
        if has_tech_signal:
            signals.append("技术栈信号")
        if has_project_signal:
            signals.append("项目信号")
        if meaningful_gaps:
            signals.append(f"JD gap {len(meaningful_gaps)} 项")
        if should_run_risk:
            signals.append("风险信号")
        if has_metric_signal:
            signals.append("量化指标")
        no_pruning_reason = f"保留完整评估 DAG：{('、'.join(signals) if signals else '多信号均命中')}"
    else:
        no_pruning_reason = ""
    memory_influence = derive_memory_influence(memory_context)
    knowledge_influence = derive_knowledge_influence(knowledge_hits)
    query_plans = {
        "tech_eval": build_queries(intent, resume_text, "技术深度", [
            "Java Spring Boot Kafka K8s 项目经验",
            "高并发 稳定性 性能优化",
            "后端工程实践 可观测性 排障",
        ]),
        "project_eval": build_queries(intent, resume_text, "项目真实性与复杂度", [
            "项目经历 架构 重构 中台",
            "核心业务 项目 贡献 复杂度",
            "项目 真实性 验证",
        ]),
        "risk_eval": build_queries(intent, resume_text, "风险验证", [
            "跳槽 空白期 时间线",
            "技能夸大 简历真实性",
            "在职 实习 时间冲突",
        ]),
    }

    route = {
        "routeMode": route_mode,
        "executionProfile": {
            "FAST_SCREEN": "短/信息不足简历：跳过技术与项目深评，仅核心解析+风险核验，最省 LLM 调用",
            "TECH_SCREEN": "技术信号强、项目薄：技术深评+风险核验，跳过项目深评",
            "TECH_DEEP_DIVE": "技术与项目都充分：技术+项目双深评（并行），证据完整故跳过风险",
            "PROJECT_AUTHENTICITY_REVIEW": "项目多但贡献边界不清：项目真实性深评+风险核验",
            "RISK_REVIEW": "时间线/证据/JD gap 风险高：聚焦风险核验",
            "FULL_REVIEW": "多信号且复杂：技术+项目+风险全开",
        }.get(route_mode, route_mode),
        "candidateType": candidate_type,
        "experienceLevel": experience_level,
        "targetRole": intent.get("targetRole") or infer_target_role(resume_text, candidate_type),
        "selectedAgents": enabled_agents,
        "enabledAgents": enabled_agents,
        "skippedAgents": skipped_agents,
        "whySelected": why_selected,
        "whySkipped": why_skipped,
        "noPruningReason": no_pruning_reason,
        "requiredSkills": required_skills,
        "routingHints": routing_hints,
        "evidenceGaps": evidence_gaps,
        "interviewFocus": interview_focus,
        "complexity": complexity,
        "path": "deep_pdf" if complexity in {"medium", "deep"} else "sparse_fast_lane",
        "routingRationale": build_routing_rationale(
            candidate_type,
            experience_level,
            resume_len,
            required_skills,
            routing_hints,
            memory_hits,
            knowledge_hits,
        ),
        "memoryHitCount": len(memory_hits),
        "knowledgeHitCount": len(knowledge_hits),
    }
    # LLM-call accounting: intent + resume_parse + jd_match + report always run (4);
    # tech/project/risk are optional. evidence_fusion is deterministic (no LLM).
    optional_selected = [a for a in enabled_agents if a in {"tech_eval", "project_eval", "risk_eval"}]
    full_pipeline_calls = 7
    estimated_calls = 4 + len(optional_selected)
    route["estimatedLlmCalls"] = estimated_calls
    route["fullPipelineLlmCalls"] = full_pipeline_calls
    route["llmCallsSavedVsFull"] = max(0, full_pipeline_calls - estimated_calls)
    return {
        "version": "agent-harness-v1",
        "route": route,
        "dynamicQueries": {key: value for key, value in query_plans.items() if key in enabled_agents},
        "memoryInfluence": memory_influence,
        "knowledgeInfluence": knowledge_influence,
        "contextManagement": derive_context_management(complexity, enabled_agents, knowledge_hits),
        "runtimeBudgets": derive_runtime_budgets(enabled_agents, complexity, memory_context, knowledge_hits),
        "reportMode": "deterministic_sparse" if resume_len < 600 else "llm_detailed",
        "queryPlans": query_plans,
        "governance": {
            "maxDuplicateToolArgs": 0,
            "requireEvidenceSource": True,
            "requireCoverageChecklist": True,
            "fallbackPolicy": "explicitly_surface_fallback_do_not_hide",
            "budgetPolicy": "long_pdf_uses_context_pack_not_full_text_repeatedly",
        },
    }


def classify_complexity(resume_text: str, required_skills: List[str], routing_hints: List[str]) -> str:
    text = resume_text or ""
    project_count = len(re.findall(r"项目|平台|系统|中台|重构|架构|Agent|RAG", text, re.I))
    if len(text) < 600:
        return "sparse"
    if len(text) > 4500 or project_count >= 8 or len(required_skills) + len(routing_hints) >= 6:
        return "deep"
    return "medium"


def normalize_memory_context(context: Dict[str, Any]) -> Dict[str, Any]:
    raw = context.get("memoryContext") if isinstance(context.get("memoryContext"), dict) else context
    if any(key in raw for key in ("episodicHits", "semanticHits", "proceduralHits")):
        return {
            "episodicHits": _as_list(raw.get("episodicHits")),
            "semanticHits": _as_list(raw.get("semanticHits")),
            "proceduralHits": _as_list(raw.get("proceduralHits")),
        }
    legacy = _as_list(raw.get("memoryHits"))
    return {"episodicHits": legacy, "semanticHits": [], "proceduralHits": []}


def normalize_knowledge_hits(context: Dict[str, Any]) -> List[Any]:
    if isinstance(context.get("knowledgeContext"), dict):
        knowledge = context["knowledgeContext"].get("knowledge") if isinstance(context["knowledgeContext"].get("knowledge"), dict) else {}
        return _as_list(knowledge.get("chunks"))
    if isinstance(context.get("knowledge"), dict):
        return _as_list(context["knowledge"].get("chunks"))
    return _as_list(context.get("knowledgeHits"))


def derive_memory_influence(memory_context: Dict[str, Any]) -> Dict[str, Any]:
    episodic = _as_list(memory_context.get("episodicHits"))
    semantic = _as_list(memory_context.get("semanticHits"))
    procedural = _as_list(memory_context.get("proceduralHits"))
    influences: List[Dict[str, Any]] = []
    for layer, hits in (("episodic", episodic), ("semantic", semantic), ("procedural", procedural)):
        for hit in hits[:3]:
            if not isinstance(hit, dict):
                continue
            influences.append({
                "type": layer,
                "memoryId": hit.get("memoryId"),
                "traceId": hit.get("traceId"),
                "appliesTo": hit.get("appliesTo", "routing"),
                "recommendedAction": hit.get("recommendedAction", ""),
                "matchReason": hit.get("matchReason", ""),
                "content": hit.get("content") or hit.get("summary", ""),
            })
    return {
        "hitCount": len(episodic) + len(semantic) + len(procedural),
        "episodicCount": len(episodic),
        "semanticCount": len(semantic),
        "proceduralCount": len(procedural),
        "appliedTo": dedupe_keep_order([str(item.get("appliesTo")) for item in influences if item.get("appliesTo")]),
        "influences": influences,
        "calibration": derive_memory_calibration(episodic),
        "poisoningControl": "Memory influences strategy only; it is never candidate factual evidence.",
    }


def derive_memory_calibration(episodic: List[Any]) -> Dict[str, Any]:
    """Aggregate similar past evaluations into a scoring-calibration anchor (only when >=3 samples).

    This is the concrete, felt use of episodic memory: retrieval over our own evaluation history
    keeps scoring consistent across similar candidates. Calibration reference only, never a fact.
    """
    scores: List[float] = []
    recs: Dict[str, int] = {}
    for hit in episodic:
        if not isinstance(hit, dict):
            continue
        evidence = hit.get("evidence") if isinstance(hit.get("evidence"), dict) else {}
        try:
            score = float(evidence.get("score") or 0)
        except (TypeError, ValueError):
            score = 0.0
        if score > 0:
            scores.append(score)
        rec = str(evidence.get("recommendation") or "").strip()
        if rec:
            recs[rec] = recs.get(rec, 0) + 1
    if len(scores) < 3:
        return {"available": False, "sampleSize": len(scores)}
    avg = round(sum(scores) / len(scores), 1)
    return {
        "available": True,
        "sampleSize": len(scores),
        "avgScore": avg,
        "scoreRange": [int(min(scores)), int(max(scores))],
        "recommendationDistribution": recs,
        "note": "相似历史候选人评分校准参考，非候选人事实",
    }


def derive_knowledge_influence(knowledge_hits: List[Any]) -> Dict[str, Any]:
    chunks: List[Dict[str, Any]] = [hit for hit in knowledge_hits if isinstance(hit, dict)]
    return {
        "hitCount": len(chunks),
        "injectedInto": ["TechEvalAgent", "ProjectEvalAgent", "RiskAgent", "EvidenceFusionAgent", "ReportAgent"] if chunks else [],
        "chunks": [
            {
                "chunkId": chunk.get("chunkId"),
                "title": chunk.get("title"),
                "docType": chunk.get("docType"),
                "sectionPath": chunk.get("sectionPath"),
                "score": chunk.get("score"),
                "rerankReason": chunk.get("rerankReason"),
                "contentPreview": chunk.get("contentPreview") or str(chunk.get("content", ""))[:220],
            }
            for chunk in chunks[:5]
        ],
        "evidenceBoundary": "Knowledge base is rubric/interview standard only, not candidate fact evidence.",
    }


def derive_context_management(complexity: str, enabled_agents: List[str], knowledge_hits: List[Any]) -> Dict[str, Any]:
    """Context engineering budget (write / select / compress / isolate).

    Treats the context window as a budget with per-segment line items, mirroring the
    Anthropic/LangChain "context engineering" practice. The actual select/compress is enforced
    in build_report_context_pack (top-k reranked knowledge, compacted agent JSON, bounded resume excerpt).
    """
    window = 12000 if complexity == "deep" else (5000 if complexity == "sparse" else 8000)
    segments = [
        {"segment": "system + tool catalog", "budgetTokens": 1200, "policy": "static_cached（write）"},
        {"segment": "task + routing decision", "budgetTokens": 700, "policy": "always_resident"},
        {"segment": "retrieved knowledge", "budgetTokens": 1500 if knowledge_hits else 0,
         "policy": "select：top-k reranked, 不喂裸 top-N"},
        {"segment": "agent evidence (tech/project/risk)", "budgetTokens": 3000, "policy": "compress：compact JSON"},
        {"segment": "resume excerpt", "budgetTokens": 2600, "policy": "compress：超阈值截断"},
        {"segment": "reserved for model reply", "budgetTokens": 2500, "policy": "reserved（不挪用）"},
    ]
    return {
        "strategy": ["write", "select", "compress", "isolate"],
        "windowBudgetTokens": window,
        "segments": segments,
        "compactionTrigger": "compact_when_segment_exceeds_budget",
        "isolation": "每个 eval Agent 独立上下文窗口，仅向 ReportAgent 回传压缩后的结构化证据",
        "note": "上下文按预算分段管理，避免 context pollution；ReportAgent 只吃压缩证据而非全量原文重复",
    }


def derive_runtime_budgets(
    enabled_agents: List[str],
    complexity: str,
    memory_context: Dict[str, Any],
    knowledge_hits: List[Any],
) -> Dict[str, Dict[str, Any]]:
    budgets: Dict[str, Dict[str, Any]] = {}
    agent_map = {
        "resume_parse": "ResumeParseAgent",
        "jd_match": "JdMatchAgent",
        "tech_eval": "TechEvalAgent",
        "project_eval": "ProjectEvalAgent",
        "risk_eval": "RiskAgent",
        "evidence_fusion": "EvidenceFusionAgent",
        "report": "ReportAgent",
    }
    for route_id in enabled_agents:
        agent = agent_map.get(route_id)
        if not agent:
            continue
        budget = dict(DEFAULT_AGENT_TOOL_BUDGETS.get(agent, {"maxToolCalls": 0, "maxRetrievalQueries": 0}))
        if complexity == "deep" and route_id in {"project_eval", "risk_eval"}:
            budget["maxRetrievalQueries"] = max(int(budget.get("maxRetrievalQueries", 0)), 4)
        if route_id == "report":
            budget["contextSources"] = ["resume_text", "jd_result", "agent_results"]
            if knowledge_hits:
                budget["contextSources"].append("knowledge_hits")
            if memory_context.get("proceduralHits"):
                budget["contextSources"].append("procedural_memory")
        budgets[agent] = budget
    return budgets


def build_routing_rationale(
    candidate_type: str,
    experience_level: str,
    resume_len: int,
    required_skills: List[str],
    routing_hints: List[str],
    memory_hits: List[Any] | None = None,
    knowledge_hits: List[Any] | None = None,
) -> List[str]:
    rationale = [
        f"candidateType={candidate_type}",
        f"experienceLevel={experience_level}",
        f"resumeTextLength={resume_len}",
    ]
    if required_skills:
        rationale.append("requiredSkills=" + ",".join(required_skills[:6]))
    if routing_hints:
        rationale.append("routingHints=" + ",".join(routing_hints[:6]))
    if resume_len > 4500:
        rationale.append("long_pdf_context_pack_required")
    if resume_len < 600:
        rationale.append("sparse_resume_fast_lane")
    if memory_hits:
        rationale.append(f"agent_memory_hits={len(memory_hits)}")
    if knowledge_hits:
        rationale.append(f"self_service_knowledge_hits={len(knowledge_hits)}")
    return rationale


def build_queries(intent: Dict[str, Any], resume_text: str, focus: str, defaults: List[str]) -> List[str]:
    queries: List[str] = []
    queries.extend(_as_str_list(intent.get("ragQueries")))
    required_skills = _as_str_list(intent.get("requiredSkills"))
    routing_hints = _as_str_list(intent.get("routingHints"))
    if required_skills:
        queries.append(f"{focus} {' '.join(required_skills[:6])}")
    if routing_hints:
        queries.append(f"{focus} {' '.join(routing_hints[:6])}")
    for keyword in extract_resume_keywords(resume_text)[:4]:
        queries.append(f"{focus} {keyword}")
    queries.extend(defaults)
    return dedupe_keep_order(queries)[:4]


def build_harness_reflection(
    harness_plan: Dict[str, Any],
    tool_health: Dict[str, Any] | None,
    coverage_checklist: str,
    tech_result: str,
    project_result: str,
    risk_result: str,
) -> Dict[str, Any]:
    health = tool_health or {}
    fallback_tools = [
        name for name, entry in health.items()
        if isinstance(entry, dict) and entry.get("fallbackUsed")
    ]
    missing_contracts: List[str] = []
    for label, value in {
        "techResult": tech_result,
        "projectResult": project_result,
        "riskResult": risk_result,
    }.items():
        if "evidenceSource" not in (value or ""):
            missing_contracts.append(f"{label}.evidenceSource")
    return {
        "harnessVersion": harness_plan.get("version"),
        "fallbackTools": fallback_tools,
        "missingContracts": missing_contracts,
        "coverageChecklistLength": len(coverage_checklist or ""),
        "nextRunImprovements": [
            "If fallbackTools is non-empty, surface RAG fallback in report and Grafana.",
            "If missingContracts is non-empty, tighten the corresponding agent prompt/schema.",
            "Keep deterministic planning; reserve LLM calls for synthesis and final report.",
        ],
    }


def infer_target_role(resume_text: str, candidate_type: str) -> str:
    text = resume_text or ""
    if re.search(r"Java|Spring|Kafka|K8s|后端", text, re.I):
        return "Java 后端 / 平台工程师"
    if "产品" in text:
        return "产品经理"
    return candidate_type


def extract_resume_keywords(resume_text: str) -> List[str]:
    pattern = re.compile(r"(Java|Spring Boot|Kafka|K8s|Kubernetes|Redis|MySQL|Docker|LLM|RAG|Agent|支付中台|重构|实习|本科|项目)")
    return dedupe_keep_order(pattern.findall(resume_text or ""))


def dedupe_keep_order(items: List[str]) -> List[str]:
    seen: set[str] = set()
    result: List[str] = []
    for item in items:
        text = str(item).strip()
        key = text.lower()
        if text and key not in seen:
            seen.add(key)
            result.append(text)
    return result


def _as_str_list(value: Any) -> List[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def _as_list(value: Any) -> List[Any]:
    return value if isinstance(value, list) else []
