from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import ast
from typing import Annotated, Any, Dict, Optional, TypedDict

from langgraph.graph import END, START, StateGraph

from app.agents import (
    FUSION_PROMPT,
    INTENT_PROMPT,
    JD_MATCH_PROMPT,
    PARSE_PROMPT,
    PROJECT_PROMPT,
    REPORT_CORE_PROMPT,
    REPORT_EVAL_PROMPT,
    REPORT_PROMPT,
    RISK_PROMPT,
    TECH_PROMPT,
    emit_tool_event_once,
    generate_report_eval,
    run_llm_node,
)
from app.agent_harness import build_harness_plan, build_harness_reflection
from app.checkpoint import get_checkpointer
from app.events import emit_event, emit_result, make_event_id, now_iso
from app.langfuse_tracing import end_trace, end_span, flush, start_agent_span, start_trace
from app.models import TraceEvent, WorkflowResultPayload, WorkflowState
from app.tools import (
    execute_skill,
    extract_primary_url,
    github_enrichment,
    jd_requirements_extract,
    knowledge_search,
    mcp_current_time,
    mcp_fetch_url,
    memory_search,
    milvus_jd_search,
    milvus_resume_batch_search,
    resume_structure_extract,
    timeline_validator,
)

logger = logging.getLogger(__name__)


def _merge_completed_nodes(left: list | None, right: list | None) -> list:
    merged: list = []
    for item in (left or []) + (right or []):
        if item not in merged:
            merged.append(item)
    return merged


class GraphState(TypedDict, total=False):
    traceId: str
    workflowRunId: str
    resumeText: str
    jobCategory: str
    jobDescription: str
    executionMode: str
    intentResult: str
    parseResult: str
    jdResult: str
    techResult: str
    projectResult: str
    riskResult: str
    fusionResult: str
    finalReport: str
    harnessPlan: dict
    harnessContext: dict
    memoryContext: dict
    knowledgeContext: dict
    overallScore: int
    recommendation: str
    strengths: list
    risks: list
    interviewQuestions: list
    completedNodes: Annotated[list, _merge_completed_nodes]
    failedNode: str
    toolHealth: dict


def _to_workflow_state(state: GraphState) -> WorkflowState:
    return WorkflowState(**{k: state.get(k) for k in WorkflowState.model_fields})


async def _emit_node_start(state: GraphState, node_id: str, agent_name: str, phase: int) -> None:
    event_id = make_event_id(state["traceId"], node_id, 1, "node", 0)
    await emit_event(
        TraceEvent(
            eventId=event_id,
            traceId=state["traceId"],
            nodeId=node_id,
            agentName=agent_name,
            phase=phase,
            attempt=1,
            kind="node",
            roundIndex=0,
            status="RUNNING",
            startedAt=now_iso(),
            callKind="node",
            callName=agent_name,
            roundRole="node_start",
        )
    )


async def _emit_node_end(
    state: GraphState,
    node_id: str,
    agent_name: str,
    phase: int,
    status: str,
    duration_ms: int,
    error: str | None = None,
) -> None:
    event_id = make_event_id(state["traceId"], node_id, 1, "node", 999)
    await emit_event(
        TraceEvent(
            eventId=event_id,
            traceId=state["traceId"],
            nodeId=node_id,
            agentName=agent_name,
            phase=phase,
            attempt=1,
            kind="node",
            roundIndex=0,
            status=status,
            startedAt=now_iso(),
            endedAt=now_iso(),
            durationMs=duration_ms,
            callKind="node",
            callName=agent_name,
            roundRole="node_end",
            outputPreview=error[:500] if error else None,
        )
    )


async def _run_node(
    state: GraphState,
    node_id: str,
    agent_name: str,
    phase: int,
    system_prompt: str,
    user_content: str,
    max_tokens: int = 4096,
) -> str:
    await _emit_node_start(state, node_id, agent_name, phase)
    start_ms = time.time()
    ws = _to_workflow_state(state)
    if state.get("toolHealth"):
        ws.toolHealth = dict(state.get("toolHealth", {}))
    try:
        result = await run_llm_node(node_id, agent_name, phase, system_prompt, user_content, ws, max_tokens=max_tokens)
        if ws.toolHealth:
            merged_health = dict(state.get("toolHealth") or {})
            merged_health.update(ws.toolHealth)
            state["toolHealth"] = merged_health
        duration = int((time.time() - start_ms) * 1000)
        await _emit_node_end(state, node_id, agent_name, phase, "SUCCESS", duration)
        return result
    except Exception as exc:
        duration = int((time.time() - start_ms) * 1000)
        await _emit_node_end(state, node_id, agent_name, phase, "FAILED", duration, str(exc))
        raise


def _safe_queries(raw: str, defaults: list[str]) -> list[str]:
    try:
        data = json.loads(raw)
    except Exception:
        try:
            data = ast.literal_eval(raw)
        except Exception:
            data = None
    try:
        queries = data.get("queries") if isinstance(data, dict) else data
        if isinstance(queries, list):
            cleaned = [str(q).strip() for q in queries if str(q).strip()]
            return cleaned[:4] or defaults[:4]
    except Exception:
        pass
    return defaults[:4]


async def _run_tool_with_span(
    state: GraphState,
    node_id: str,
    agent_name: str,
    phase: int,
    round_index: int,
    tool_name: str,
    tool_input: dict,
    coro: Any,
    agent_span_id: Optional[str],
    parent_event_id: str | None = None,
) -> str:
    start_ms = time.time()
    started_at = now_iso()
    try:
        result_str = await coro
        status = "SUCCESS"
        if tool_result_has_error(result_str):
            status = "FAILED"
    except Exception as exc:
        result_str = json.dumps({"error": str(exc), "tool": tool_name}, ensure_ascii=False)
        status = "FAILED"
    duration_ms = int((time.time() - start_ms) * 1000)
    ended_at = now_iso()
    ws = _to_workflow_state(state)
    await emit_tool_event_once(
        state=ws,
        node_id=node_id,
        agent_name=agent_name,
        phase=phase,
        round_index=round_index,
        tool_name=tool_name,
        tool_input=tool_input,
        result_str=result_str,
        status=status,
        duration_ms=duration_ms,
        agent_span_id=agent_span_id,
        parent_event_id=parent_event_id or make_event_id(state["traceId"], node_id, 1, "generation", round_index),
        started_at=started_at,
        ended_at=ended_at,
    )
    if ws.toolHealth:
        merged_health = dict(state.get("toolHealth") or {})
        merged_health.update(ws.toolHealth)
        state["toolHealth"] = merged_health
    return result_str


def tool_result_has_error(result: str) -> bool:
    try:
        data = json.loads(result)
        return isinstance(data, dict) and bool(data.get("error"))
    except json.JSONDecodeError:
        return False


def _json_obj(raw: str | None) -> Dict[str, Any]:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _fast_lane_resume(resume_text: str) -> bool:
    text = (resume_text or "").strip()
    return len(text) < 600


def _harness_eval_lane(state: GraphState) -> bool:
    return _fast_lane_resume(state.get("resumeText", ""))


def _deterministic_tech_result(state: GraphState) -> str:
    resume_text = state.get("resumeText", "")
    intent = _json_obj(state.get("intentResult", ""))
    parse = _json_obj(state.get("parseResult", ""))
    skills = parse.get("skills") or parse.get("skillKeywords") or intent.get("requiredSkills") or []
    if not isinstance(skills, list):
        skills = [str(skills)]
    projects = parse.get("projects") if isinstance(parse.get("projects"), list) else []
    score = 45 if len(resume_text) < 120 else 62
    score += min(len(skills), 8) * 2
    score += min(len(projects), 5) * 3
    if re.search(r"Agent|RAG|LangGraph|Milvus|K8s|Kafka|高并发|性能|可观测", resume_text, re.I):
        score += 8
    score = max(35, min(score, 88))
    result = {
        "dimensions": [
            {
                "name": "技术栈覆盖",
                "score": score,
                "evidenceSource": "resume_text_only",
                "evidenceQuotes": skills[:5],
            },
            {
                "name": "工程深度",
                "score": max(35, score - (18 if len(projects) < 2 else 8)),
                "evidenceSource": "resume_text_only",
                "evidenceQuotes": [str(p)[:160] for p in projects[:3]] or ["简历缺少可验证项目细节"],
            },
        ],
        "overallTechScore": score,
        "highlights": [f"出现技能关键词：{'、'.join(map(str, skills[:8]))}"] if skills else [],
        "weaknesses": ["需要面试验证技术深度、个人贡献边界、线上指标和排障细节"],
        "evidenceSource": "resume_text_only",
        "toolHealth": {"harnessEvaluator": True, "reason": "context_pack_skip_repeated_llm"},
    }
    return json.dumps(result, ensure_ascii=False)


def _deterministic_project_result(state: GraphState, rag_raw: str = "") -> str:
    resume_text = state.get("resumeText", "")
    parse = _json_obj(state.get("parseResult", ""))
    projects = parse.get("projects") if isinstance(parse.get("projects"), list) else []
    score = 50 + min(len(projects), 6) * 5
    if re.search(r"重构|架构|中台|平台|性能|高并发|上线|监控|Agent|RAG", resume_text, re.I):
        score += 10
    score = max(40, min(score, 88))
    result = {
        "projects": projects[:6],
        "overallProjectScore": score,
        "depthHighlights": [str(p)[:180] for p in projects[:5]] or ["简历未提取到明确项目条目"],
        "concerns": ["项目真实性、个人贡献比例、量化业务结果需要面试继续核验"],
        "evidenceSource": "resume_text_and_rag",
        "toolHealth": {"harnessEvaluator": True, "ragDigest": _compact_text(rag_raw, 500)},
    }
    return json.dumps(result, ensure_ascii=False)


def _deterministic_risk_result(state: GraphState, timeline_raw: str = "", rag_raw: str = "") -> str:
    resume_text = state.get("resumeText", "")
    risks: list[str] = []
    if len(resume_text) < 600:
        risks.append("简历信息较少，项目真实性和职责边界无法充分验证")
    if not re.search(r"\d+%|\d+倍|QPS|TPS|ms|分钟|小时|成本|收入|DAU|转化", resume_text, re.I):
        risks.append("缺少量化结果指标，需面试核验业务价值")
    if re.search(r"熟悉|了解|参与", resume_text) and not re.search(r"负责|主导|Owner|独立", resume_text, re.I):
        risks.append("部分表述可能偏参与型，需要确认个人主导程度")
    result = {
        "riskLevel": "MEDIUM" if risks else "LOW",
        "risks": risks or ["未发现明显硬性风险，但仍需核验项目细节和贡献边界"],
        "overallAssessment": "Harness 基于时间线、量化指标、职责动词和 RAG 证据进行风险初筛。",
        "evidenceSource": "resume_text_and_checks",
        "toolHealth": {"harnessEvaluator": True, "timeline": _compact_text(timeline_raw, 300), "ragDigest": _compact_text(rag_raw, 300)},
    }
    return json.dumps(result, ensure_ascii=False)


def _deterministic_report(state: GraphState, skill_raw: str, coverage_checklist: str, harness_reflection: Dict[str, Any]) -> str:
    resume_text = state.get("resumeText", "")
    tech = _json_obj(state.get("techResult", ""))
    project = _json_obj(state.get("projectResult", ""))
    risk = _json_obj(state.get("riskResult", ""))
    jd = _json_obj(state.get("jdResult", ""))
    harness_plan = state.get("harnessPlan") or {}
    harness_context = state.get("harnessContext") or {}
    tech_score = int(tech.get("overallTechScore") or 45)
    project_score = int(project.get("overallProjectScore") or max(35, tech_score - 5))
    risk_level = str(risk.get("riskLevel") or "MEDIUM")
    score = max(0, min(100, round(tech_score * 0.45 + project_score * 0.25 + 30 * 0.30)))
    if len(resume_text) < 120:
        score = min(score, 45)
    rec = "NOT_RECOMMEND" if score < 60 else "NEED_MANUAL_REVIEW" if score < 75 else "RECOMMEND"
    skills = []
    for key in ("highlights", "weaknesses"):
        value = tech.get(key)
        if isinstance(value, list):
            skills.extend(map(str, value[:3]))
    gaps = jd.get("gaps") if isinstance(jd.get("gaps"), list) else []
    risks = risk.get("risks") if isinstance(risk.get("risks"), list) else []
    risk_lines = list(map(str, risks[:3])) or ["简历内容过短，无法验证项目真实性、职责边界和产出指标。"]
    if gaps:
        risk_lines.append("JD 差距：" + "；".join(map(str, gaps[:2])))
    route = harness_plan.get("route") or {}
    knowledge = (harness_context.get("knowledge") or {}) if isinstance(harness_context, dict) else {}
    projects = project.get("projects") if isinstance(project.get("projects"), list) else []
    project_lines = [str(p)[:220] for p in projects[:5]]
    strengths = skills[:4] or project_lines[:3] or ["简历具备一定岗位相关经验，但需要结合面试进一步确认深度。"]
    questions = [
        "请候选人选择一个最核心项目，说明个人职责、系统架构、技术难点和最终指标。",
        "针对简历中的技能关键词逐项追问实际使用场景、排障案例和边界理解。",
        "核验 GitHub/项目链接是否为本人贡献，并要求现场解释关键代码或设计取舍。",
        "如果目标岗位要求后端/Agent/RAG 能力，追问是否做过线上部署、监控、性能优化和故障恢复。",
        "围绕 JD 缺口追问最近一次真实业务场景下的取舍、失败复盘和量化产出。",
        "请解释一个项目从需求、设计、上线到监控告警的完整闭环，确认是否具备端到端 owner 能力。",
    ]
    return "\n".join([
        f"## 综合评分：{score}/100",
        f"## 推荐决策：{rec}",
        "## 证据来源说明",
        f"- 完整简历原文是主证据，resumeTextLength={len(resume_text)}；embedding RAG、JD 匹配、知识库 HarnessContext 只作为定位和补充，不替代原文。",
        f"- 知识库命中 {knowledge.get('hitCount', 0)} 个 chunk，注入目标：{', '.join(knowledge.get('injectedInto', []) or []) or '无'}；Skill 可用性检查已执行，摘要：{_report_safe_text(skill_raw, 160)}",
        f"- Harness 反思：{_compact_text(json.dumps(harness_reflection, ensure_ascii=False), 180)}",
        "## 简历覆盖核对",
        f"- {coverage_checklist}",
        "## Agent-Harness 执行说明",
        f"- Route：candidateType={route.get('candidateType')}，experienceLevel={route.get('experienceLevel')}，path={route.get('path')}，targetRole={route.get('targetRole')}。",
        f"- ContextPolicy：{_compact_text(json.dumps(harness_plan.get('contextPolicy', {}), ensure_ascii=False), 260)}",
        f"- KnowledgePolicy：{_compact_text(json.dumps(harness_plan.get('knowledgePolicy', {}), ensure_ascii=False), 260)}",
        f"- Guardrails/Eval：{_compact_text(json.dumps({'guardrails': harness_plan.get('guardrails', {}), 'evalPolicy': harness_plan.get('evalPolicy', {})}, ensure_ascii=False), 320)}",
        "## 核心优势",
        *[f"- {item}" for item in strengths],
        *[f"- 项目证据：{item}" for item in project_lines[:3]],
        "## 关键风险",
        *[f"- {item}" for item in risk_lines[:4]],
        "## 面试追问",
        *[f"- {item}" for item in questions],
        "## 综合结论",
        f"- 本次结论由 Harness synthesis 生成：先由 Intent/JD/Knowledge 建立控制面，再由 Tech/Project/Risk evaluator 产出结构化证据，最后合成为 HR 可读报告。推荐结论为 {rec}，分数为 {score}/100；面试应优先核验项目真实性、技术深度、指标结果和 JD 缺口。",
    ])


async def _emit_deterministic_plan_round(
    state: GraphState,
    node_id: str,
    agent_name: str,
    phase: int,
    round_index: int,
    plan_text: str,
    plan_input: str,
    has_tool_calls: bool = True,
) -> str:
    event_id = make_event_id(state["traceId"], node_id, 1, "generation", round_index)
    timestamp = now_iso()
    input_messages = [
        {"role": "system", "content": "确定性证据计划，不调用 LLM。"},
        {"role": "user", "content": plan_input},
    ]
    output_message = {"role": "assistant", "content": plan_text}
    await emit_event(
        TraceEvent(
            eventId=event_id,
            traceId=state["traceId"],
            nodeId=node_id,
            agentName=agent_name,
            phase=phase,
            attempt=1,
            kind="generation",
            roundIndex=round_index,
            status="SUCCESS",
            startedAt=timestamp,
            endedAt=timestamp,
            durationMs=0,
            modelName="deterministic-plan",
            inputMessages=input_messages,
            outputMessage=output_message,
            inputPreview=plan_input[:500],
            outputPreview=plan_text[:500],
            langfuseTraceId=state["traceId"],
            callKind="system_plan",
            callName="deterministic_tool_plan",
            roundRole="decision",
            parentRoundId=f"{node_id}#{round_index}",
            decisionText=plan_text,
            hasToolCalls=has_tool_calls,
            observationKind="system_plan",
        )
    )
    return event_id


async def _emit_deterministic_final_round(
    state: GraphState,
    node_id: str,
    agent_name: str,
    phase: int,
    round_index: int,
    output_text: str,
    input_text: str,
) -> str:
    event_id = make_event_id(state["traceId"], node_id, 1, "generation", round_index)
    timestamp = now_iso()
    await emit_event(
        TraceEvent(
            eventId=event_id,
            traceId=state["traceId"],
            nodeId=node_id,
            agentName=agent_name,
            phase=phase,
            attempt=1,
            kind="generation",
            roundIndex=round_index,
            status="SUCCESS",
            startedAt=timestamp,
            endedAt=timestamp,
            durationMs=0,
            modelName="deterministic-fusion",
            inputMessages=[
                {"role": "system", "content": "确定性证据融合，不调用 LLM。"},
                {"role": "user", "content": input_text},
            ],
            outputMessage={"role": "assistant", "content": output_text},
            inputPreview=input_text[:500],
            outputPreview=output_text[:500],
            langfuseTraceId=state["traceId"],
            callKind="system_fusion",
            callName="deterministic_evidence_fusion",
            roundRole="final",
            parentRoundId=f"{node_id}#{round_index}",
            finalOutput=output_text,
            hasToolCalls=False,
            observationKind="system_fusion",
        )
    )
    return event_id


def _intent_json(state: GraphState) -> dict[str, Any]:
    try:
        parsed = json.loads(state.get("intentResult", "") or "{}")
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _queries_from_intent(state: GraphState, defaults: list[str], focus: str) -> list[str]:
    harness_plan = state.get("harnessPlan") or {}
    query_plans = harness_plan.get("queryPlans") if isinstance(harness_plan, dict) else None
    if isinstance(query_plans, dict):
        route_key = {
            "技术深度": "tech_eval",
            "项目真实性与复杂度": "project_eval",
            "风险验证": "risk_eval",
        }.get(focus)
        planned = query_plans.get(route_key) if route_key else None
        if isinstance(planned, list) and planned:
            return [str(q) for q in planned[:4] if str(q).strip()]
    intent = _intent_json(state)
    raw_queries = intent.get("ragQueries")
    queries: list[str] = []
    if isinstance(raw_queries, list):
        queries.extend(str(q).strip() for q in raw_queries if str(q).strip())
    required_skills = intent.get("requiredSkills")
    if isinstance(required_skills, list) and required_skills:
        queries.append(f"{focus} {' '.join(map(str, required_skills[:5]))}")
    routing_hints = intent.get("routingHints")
    if isinstance(routing_hints, list) and routing_hints:
        queries.append(f"{focus} {' '.join(map(str, routing_hints[:5]))}")
    queries.extend(defaults)
    deduped: list[str] = []
    seen: set[str] = set()
    for query in queries:
        key = query.lower()
        if key and key not in seen:
            seen.add(key)
            deduped.append(query)
    return deduped[:4]


def _json_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _memory_hits(memory_context: dict[str, Any]) -> list[dict[str, Any]]:
    hits: list[dict[str, Any]] = []
    for key in ("episodicHits", "semanticHits", "proceduralHits"):
        for item in _json_list(memory_context.get(key)):
            if isinstance(item, dict):
                hits.append(item)
    return hits


PROJECT_SIGNAL_PATTERNS = {
    "incident": r"排查|故障|定位|恢复|告警|日志|dump|gc|内存|cpu|rss|oom|容器|监控",
    "performance": r"优化|耗时|P99|P95|慢|索引|SQL|吞吐|延迟|QPS|TPS|file sort|执行计划",
    "architecture": r"重构|架构|平台|中台|服务|模板|工作流|编排|配置中心|热替换",
    "ai_agent": r"Agent|RAG|LLM|DAG|Trace|Prompt|Tool|Skill|MCP|Embedding|向量|召回",
    "model_platform": r"模型|Model|Schema|TopK|推理|参数|网关|统一接入|配置",
    "data_observability": r"监控|可观测|对账|FaaS|多机房|日志|指标|索引",
}

LOW_VALUE_PATTERNS = r"自我评价|团队合作|高效检索|熟悉|了解|掌握"


def _extract_parse_projects(parse_result: str) -> list[str]:
    data = _json_obj(parse_result)
    projects = data.get("projects") if isinstance(data.get("projects"), list) else []
    result: list[str] = []
    for item in projects[:6]:
        if isinstance(item, dict):
            result.append(str(item.get("name") or item.get("description") or item)[:120])
        else:
            result.append(str(item)[:120])

    def priority(text: str) -> int:
        score = 0
        for pattern in PROJECT_SIGNAL_PATTERNS.values():
            if re.search(pattern, text, re.I):
                score += 2
        if re.search(r"\d+%|\d+倍|\d+ms|\d+s|P99|P95|QPS|TPS|成本|耗时|降低|提升", text, re.I):
            score += 3
        if re.search(LOW_VALUE_PATTERNS, text, re.I):
            score -= 3
        return -score

    return sorted([item for item in result if item.strip()], key=priority)


def extract_project_signals(text: str) -> dict[str, list[str]]:
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    result: dict[str, list[str]] = {
        "incident": [],
        "performance": [],
        "architecture": [],
        "ai_agent": [],
        "model_platform": [],
        "data_observability": [],
        "skills": [],
        "metrics": [],
        "links": [],
    }

    for line in lines:
        clipped = line[:240]
        # Skip tiny fragments (e.g. a wrapped bullet tail) for category signals to avoid junk questions.
        if len(line) >= 12:
            for key, pattern in PROJECT_SIGNAL_PATTERNS.items():
                if re.search(pattern, line, re.I):
                    result[key].append(clipped)
        if re.search(r"Java|Spring|Redis|MySQL|Kafka|K8s|Docker|RAG|LLM|Agent|Milvus|DeepSeek|Vue|React|Python|Go", line, re.I):
            result["skills"].append(clipped)
        if re.search(r"\d+%|\d+倍|\d+ms|\d+s|P99|P95|QPS|TPS|成本|耗时|降低|提升", line, re.I):
            result["metrics"].append(clipped)
        if re.search(r"https?://|github|gitlab|gitee", line, re.I):
            result["links"].append(clipped)

    return {key: dedupe_non_empty(value)[:8] for key, value in result.items()}


def _extract_parse_skills(parse_result: str) -> list[str]:
    data = _json_obj(parse_result)
    value = data.get("skills") or data.get("skillKeywords") or []
    return [str(item) for item in value[:12] if str(item).strip()] if isinstance(value, list) else []


def _extract_jd_gaps(jd_result: str) -> list[str]:
    data = _json_obj(jd_result)
    gaps = data.get("gaps") if isinstance(data.get("gaps"), list) else []
    return [str(item)[:120] for item in gaps[:6] if str(item).strip()]


def build_knowledge_queries(state: GraphState) -> list[str]:
    intent = _json_obj(state.get("intentResult", ""))
    route = ((state.get("harnessPlan") or {}).get("route") or {})
    memory_context = state.get("memoryContext") or {}
    procedural_actions = [
        str(hit.get("recommendedAction") or hit.get("content") or "")
        for hit in _json_list(memory_context.get("proceduralHits"))
        if isinstance(hit, dict)
    ]
    skills = _extract_parse_skills(state.get("parseResult", ""))
    projects = _extract_parse_projects(state.get("parseResult", ""))
    gaps = _extract_jd_gaps(state.get("jdResult", ""))
    required = [str(item) for item in _json_list(intent.get("requiredSkills"))[:6]]
    hints = [str(item) for item in _json_list(intent.get("routingHints"))[:6]]
    target_role = str(route.get("targetRole") or state.get("jobCategory") or "候选人")
    queries = [
        f"面试 Rubric {target_role} {' '.join(skills[:5] or required[:5])}",
        f"项目真实性 贡献边界 {' '.join(projects[:2])}",
        f"风险验证 JD缺口 {' '.join(gaps[:3] or hints[:3])}",
        f"历史策略 {' '.join(procedural_actions[:2])}",
    ]
    cleaned: list[str] = []
    seen: set[str] = set()
    for query in queries:
        normalized = re.sub(r"\s+", " ", query).strip()
        if len(normalized) < 8:
            continue
        key = normalized.lower()
        if key not in seen:
            seen.add(key)
            cleaned.append(normalized)
    return cleaned[:3]


def _compact_text(value: str, limit: int = 2000) -> str:
    text = value or ""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n...[truncated {len(text) - limit} chars]"


def _report_safe_text(value: str, limit: int = 2000) -> str:
    return _compact_text(value, limit).replace("知识图谱", "关系证据").replace("GraphRAG", "关系检索").replace("Neo4j", "关系库")


def _resume_coverage_checklist(resume_text: str, parse_result: str = "") -> str:
    lines = [line.strip() for line in (resume_text or "").splitlines() if line.strip()]
    section_headings: list[str] = []
    timeline_items: list[str] = []
    education_items: list[str] = []
    project_items: list[str] = []
    skill_items: list[str] = []

    date_pattern = re.compile(r"(20\d{2}|19\d{2}|至今|实习|本科|硕士|博士|毕业)", re.I)
    skill_pattern = re.compile(
        r"(Java|Spring|Kafka|K8s|Kubernetes|Redis|MySQL|Docker|Python|Go|Vue|React|LLM|RAG|Agent|Milvus|LangGraph|LangChain|SQL|Flink|RabbitMQ|ElasticSearch)",
        re.I,
    )
    for line in lines:
        clipped = line[:240]
        if line.startswith("#") or line.endswith(":") or line.endswith("："):
            section_headings.append(clipped)
        if date_pattern.search(line):
            timeline_items.append(clipped)
        if any(keyword in line for keyword in ("教育", "大学", "学院", "本科", "硕士", "博士", "专业", "GPA")):
            education_items.append(clipped)
        if any(keyword in line for keyword in ("项目", "系统", "平台", "中台", "重构", "负责", "实习", "工作流", "服务", "接口")):
            project_items.append(clipped)
        if skill_pattern.search(line) or any(keyword in line for keyword in ("技能", "技术栈", "熟悉", "掌握")):
            skill_items.append(clipped)

    payload = {
        "resumeTextLength": len(resume_text or ""),
        "nonEmptyLineCount": len(lines),
        "sectionHeadings": section_headings[:80],
        "educationItems": education_items[:80],
        "timelineItems": timeline_items[:120],
        "projectOrExperienceItems": project_items[:120],
        "skillItems": skill_items[:120],
        "allResumeLines": [line[:240] for line in lines[:180]],
        "parseResult": parse_result,
        "instruction": "ReportAgent 必须覆盖 allResumeLines 中的关键经历，不得只抽样写少数点。",
    }
    return json.dumps(payload, ensure_ascii=False)


async def intent_node(state: GraphState) -> Dict[str, Any]:
    result = await _run_node(state, "intent", "IntentAgent", 1, INTENT_PROMPT, state.get("resumeText", ""), max_tokens=640)
    resume_text = state.get("resumeText", "")
    context_query = " ".join([
        _compact_text(resume_text, 800),
        _compact_text(result, 400),
        str(state.get("jobCategory", "")),
    ])
    memory_raw = await memory_search(context_query, 5)
    try:
        memory_context = json.loads(memory_raw)
        if not isinstance(memory_context, dict):
            memory_context = {"episodicHits": [], "semanticHits": [], "proceduralHits": []}
    except Exception:
        memory_context = {"episodicHits": [], "semanticHits": [], "proceduralHits": []}
    harness_context = {
        "memoryContext": memory_context,
        "memorySummary": [
            _compact_text(str(item.get("content") or item.get("summary", "")), 180)
            for item in _memory_hits(memory_context)[:4]
        ],
    }
    harness_plan = build_harness_plan(result, resume_text, state.get("jobCategory", ""), harness_context)
    await _emit_deterministic_final_round(
        state,
        "harness_context",
        "AgentHarness",
        1,
        1,
        json.dumps({"harnessContext": harness_context, "harnessPlan": harness_plan}, ensure_ascii=False),
        "IntentAgent output -> memory_search + knowledge_search -> dynamic route plan",
    )
    return {
        "intentResult": result,
        "harnessPlan": harness_plan,
        "harnessContext": harness_context,
        "memoryContext": memory_context,
        "completedNodes": ["intent", "harness_context"],
    }


async def resume_parse_node(state: GraphState) -> Dict[str, Any]:
    await _emit_node_start(state, "resume_parse", "ResumeParseAgent", 2)
    start_ms = time.time()
    trace_id = state["traceId"]
    agent_span_id = start_agent_span(
        trace_id,
        "agent.2.resume_parse",
        {"nodeId": "resume_parse", "agentName": "ResumeParseAgent", "workflowRunId": state.get("workflowRunId")},
        input_data={
            "resumeText": state.get("resumeText", ""),
            "resumeTextLength": len(state.get("resumeText", "")),
        },
        started_at=now_iso(),
    )
    try:
        resume_text = state.get("resumeText", "")
        ws = _to_workflow_state(state)
        plan_user = (
            f"完整简历原文：{resume_text}\n"
            "请制定简历结构化抽取计划。输出一句话说明需要先抽取哪些结构化字段。"
        )
        plan_event_id = await _emit_deterministic_plan_round(
            state,
            "resume_parse",
            "ResumeParseAgent",
            2,
            1,
            "系统将先抽取姓名、年限、技能、项目、教育、时间线与原文覆盖清单，再由 LLM 归纳结构化 JSON。",
            plan_user,
        )
        raw = await _run_tool_with_span(
            state,
            "resume_parse",
            "ResumeParseAgent",
            2,
            1,
            "resume_structure_extract",
            {"resumeText": resume_text},
            resume_structure_extract(resume_text),
            agent_span_id,
            plan_event_id,
        )
        user = (
            f"完整简历原文：{resume_text}\n"
            f"结构化抽取结果：{raw}\n"
            "请输出严格 JSON：{\"name\":\"\",\"summary\":\"\",\"skills\":[],\"projects\":[],\"education\":[],\"timelineEntries\":[],\"textLength\":0}"
        )
        result = await run_llm_node(
            "resume_parse", "ResumeParseAgent", 2, PARSE_PROMPT, user, ws,
            agent_span_id=agent_span_id, round_index=2, max_tokens=900,
        )
        end_span(agent_span_id, output_data=result, ended_at=now_iso(), status="SUCCESS",
                 duration_ms=int((time.time() - start_ms) * 1000))
        flush()
        duration = int((time.time() - start_ms) * 1000)
        await _emit_node_end(state, "resume_parse", "ResumeParseAgent", 2, "SUCCESS", duration)
        return {"parseResult": result, "completedNodes": ["resume_parse"]}
    except Exception as exc:
        end_span(agent_span_id, output_data=str(exc), ended_at=now_iso(), status="FAILED",
                 duration_ms=int((time.time() - start_ms) * 1000))
        flush()
        await _emit_node_end(state, "resume_parse", "ResumeParseAgent", 2, "FAILED",
                             int((time.time() - start_ms) * 1000), str(exc))
        raise


async def jd_match_node(state: GraphState) -> Dict[str, Any]:
    await _emit_node_start(state, "jd_match", "JdMatchAgent", 3)
    start_ms = time.time()
    trace_id = state["traceId"]
    agent_span_id = start_agent_span(
        trace_id,
        "agent.3.jd_match",
        {"nodeId": "jd_match", "agentName": "JdMatchAgent", "workflowRunId": state.get("workflowRunId")},
        input_data={
            "resumeText": state.get("resumeText", ""),
            "parseResult": state.get("parseResult", ""),
        },
        started_at=now_iso(),
    )
    try:
        resume_text = state.get("resumeText", "")
        ws = _to_workflow_state(state)
        plan_user = (
            f"完整简历原文：{resume_text}\n"
            f"意图识别：{state.get('intentResult', '')}\n"
            f"简历解析：{state.get('parseResult', '')}\n"
            "请制定岗位匹配证据收集计划。必须包含：检索岗位库、提取JD要求、再由LLM综合匹配。"
        )
        plan_event_id = await _emit_deterministic_plan_round(
            state,
            "jd_match",
            "JdMatchAgent",
            3,
            1,
            "系统将检索岗位库并抽取 JD 要求，然后由 LLM 结合 IntentAgent 输出形成岗位匹配结论。",
            plan_user,
        )
        jd_raw = await _run_tool_with_span(
            state, "jd_match", "JdMatchAgent", 3, 1,
            "milvus_jd_search", {"resumeText": resume_text, "topK": 3},
            milvus_jd_search(resume_text, 3), agent_span_id, plan_event_id,
        )
        req_raw = await _run_tool_with_span(
            state, "jd_match", "JdMatchAgent", 3, 1,
            "jd_requirements_extract", {"jdMatchJson": jd_raw},
            jd_requirements_extract(jd_raw), agent_span_id, plan_event_id,
        )
        user = (
            f"完整简历原文：{resume_text}\n"
            f"意图识别：{state.get('intentResult', '')}\n"
            f"简历解析：{state.get('parseResult', '')}\n"
            f"JD检索结果：{jd_raw}\n"
            f"JD要求抽取：{req_raw}\n"
            "请输出严格 JSON：{\"matchedJd\":\"\",\"matchScore\":0,\"requirements\":[],\"preferredSkills\":[],\"gaps\":[]}"
        )
        result = await run_llm_node(
            "jd_match", "JdMatchAgent", 3, JD_MATCH_PROMPT, user, ws,
            agent_span_id=agent_span_id, round_index=2, max_tokens=900,
        )
        end_span(agent_span_id, output_data=result, ended_at=now_iso(), status="SUCCESS",
                 duration_ms=int((time.time() - start_ms) * 1000))
        flush()
        duration = int((time.time() - start_ms) * 1000)
        await _emit_node_end(state, "jd_match", "JdMatchAgent", 3, "SUCCESS", duration)
        return {"jdResult": result, "completedNodes": ["jd_match"]}
    except Exception as exc:
        end_span(agent_span_id, output_data=str(exc), ended_at=now_iso(), status="FAILED",
                 duration_ms=int((time.time() - start_ms) * 1000))
        flush()
        await _emit_node_end(state, "jd_match", "JdMatchAgent", 3, "FAILED",
                             int((time.time() - start_ms) * 1000), str(exc))
        raise


async def knowledge_context_node(state: GraphState) -> Dict[str, Any]:
    await _emit_node_start(state, "knowledge_context", "KnowledgeRetrievalAgent", 4)
    start_ms = time.time()
    trace_id = state["traceId"]
    agent_span_id = start_agent_span(
        trace_id,
        "agent.4.knowledge_context",
        {"nodeId": "knowledge_context", "agentName": "KnowledgeRetrievalAgent", "workflowRunId": state.get("workflowRunId")},
        input_data={"harnessPlan": state.get("harnessPlan", {}), "intentResult": state.get("intentResult", "")},
        started_at=now_iso(),
    )
    try:
        harness_plan = state.get("harnessPlan") or {}
        queries = build_knowledge_queries(state)
        plan_text = json.dumps({
            "queries": queries,
            "policy": "self_service_knowledge_is_rubric_not_candidate_fact",
            "injectionPoints": ["TechEvalAgent", "ProjectEvalAgent", "RiskAgent", "EvidenceFusionAgent", "ReportAgent"],
        }, ensure_ascii=False)
        plan_event_id = await _emit_deterministic_plan_round(
            state,
            "knowledge_context",
            "KnowledgeRetrievalAgent",
            4,
            1,
            plan_text,
            f"AgentHarness：{json.dumps(harness_plan, ensure_ascii=False)}\nIntent：{state.get('intentResult', '')}\nJD：{state.get('jdResult', '')}",
        )
        results: list[dict[str, Any]] = []
        for idx, query in enumerate(queries[:3], start=1):
            raw = await _run_tool_with_span(
                state,
                "knowledge_context",
                "KnowledgeRetrievalAgent",
                4,
                1,
                "knowledge_search",
                {"query": query, "topK": 4},
                knowledge_search(query, 4),
                agent_span_id,
                plan_event_id,
            )
            results.append({"query": query, "result": _json_obj(raw)})
        # Dedup chunks across the multiple knowledge queries: the same chunk surfacing for
        # several queries must not be counted/injected multiple times.
        chunks: list[Any] = []
        seen_chunk_ids: set[str] = set()
        for item in results:
            result = item.get("result") if isinstance(item, dict) else {}
            if not (isinstance(result, dict) and isinstance(result.get("chunks"), list)):
                continue
            for chunk in result.get("chunks") or []:
                if isinstance(chunk, dict):
                    key = str(chunk.get("chunkId") or chunk.get("content") or chunk.get("contentPreview") or "")
                else:
                    key = str(chunk)
                if key and key in seen_chunk_ids:
                    continue
                seen_chunk_ids.add(key)
                chunks.append(chunk)

        # Public MCP enrichment: TWO real Model Context Protocol servers run concurrently
        # (mcp-server-time for evaluation-date grounding + mcp-server-fetch for the candidate's
        # external page). Parallel so they add ~0 critical-path latency; both guarded + traced.
        external_web: dict[str, Any] = {}
        evaluation_time: dict[str, Any] = {}
        primary_url = extract_primary_url(state.get("resumeText", ""))

        async def _time_coro() -> str:
            return json.dumps(await mcp_current_time("Asia/Shanghai"), ensure_ascii=False)

        async def _fetch_coro() -> str:
            return json.dumps(await mcp_fetch_url(primary_url), ensure_ascii=False)

        mcp_tasks = [
            _run_tool_with_span(
                state, "knowledge_context", "KnowledgeRetrievalAgent", 4, 1,
                "mcp_time[public:mcp-server-time]", {"timezone": "Asia/Shanghai"},
                _time_coro(), agent_span_id, plan_event_id,
            )
        ]
        if primary_url:
            mcp_tasks.append(
                _run_tool_with_span(
                    state, "knowledge_context", "KnowledgeRetrievalAgent", 4, 1,
                    "mcp_fetch[public:mcp-server-fetch]", {"url": primary_url},
                    _fetch_coro(), agent_span_id, plan_event_id,
                )
            )
        mcp_results = await asyncio.gather(*mcp_tasks)
        evaluation_time = _json_obj(mcp_results[0])
        if primary_url and len(mcp_results) > 1:
            external_web = _json_obj(mcp_results[1])

        existing_context = state.get("harnessContext") or {}
        harness_context = {
            **existing_context,
            "knowledge": {
                "queries": queries,
                "hitCount": len(chunks),
                "chunks": chunks[:8],
                "injectedInto": ["TechEvalAgent", "ProjectEvalAgent", "RiskAgent", "EvidenceFusionAgent", "ReportAgent"],
                "knowledgeAsEvidence": "rubric_only_not_candidate_fact",
            },
            "externalWeb": external_web,
            "evaluationTime": evaluation_time,
            "publicMcpServers": ["mcp-server-time", "mcp-server-fetch"],
        }
        updated_harness_plan = build_harness_plan(
            state.get("intentResult", ""),
            state.get("resumeText", ""),
            state.get("jobCategory", ""),
            harness_context,
        )
        await _emit_deterministic_final_round(
            state,
            "knowledge_context",
            "KnowledgeRetrievalAgent",
            4,
            2,
            json.dumps({"harnessContext": harness_context, "harnessPlan": updated_harness_plan}, ensure_ascii=False),
            plan_text,
        )
        end_span(agent_span_id, output_data=harness_context, ended_at=now_iso(), status="SUCCESS",
                 duration_ms=int((time.time() - start_ms) * 1000))
        flush()
        await _emit_node_end(state, "knowledge_context", "KnowledgeRetrievalAgent", 4, "SUCCESS", int((time.time() - start_ms) * 1000))
        return {"harnessContext": harness_context, "knowledgeContext": harness_context.get("knowledge", {}), "harnessPlan": updated_harness_plan, "completedNodes": ["knowledge_context"]}
    except Exception as exc:
        end_span(agent_span_id, output_data=str(exc), ended_at=now_iso(), status="FAILED",
                 duration_ms=int((time.time() - start_ms) * 1000))
        flush()
        await _emit_node_end(state, "knowledge_context", "KnowledgeRetrievalAgent", 4, "FAILED",
                             int((time.time() - start_ms) * 1000), str(exc))
        return {
            "harnessContext": {
                "knowledge": {"queries": [], "hitCount": 0, "chunks": [], "error": str(exc), "injectedInto": []},
                "contextPolicy": (state.get("harnessPlan") or {}).get("contextPolicy", {}),
            },
            "completedNodes": ["knowledge_context"],
        }


async def tech_eval_node(state: GraphState) -> Dict[str, Any]:
    await _emit_node_start(state, "tech_eval", "TechEvalAgent", 4)
    start_ms = time.time()
    trace_id = state["traceId"]
    agent_span_id = start_agent_span(
        trace_id,
        "agent.4.tech_eval",
        {"nodeId": "tech_eval", "agentName": "TechEvalAgent", "workflowRunId": state.get("workflowRunId")},
        input_data={
            "resumeText": state.get("resumeText", ""),
            "parseResult": state.get("parseResult", ""),
            "jdResult": state.get("jdResult", ""),
        },
        started_at=now_iso(),
    )
    try:
        ws = _to_workflow_state(state)
        resume_text = state.get("resumeText", "")
        plan_user = (
            f"resumeText={resume_text}\n"
            f"intentResult={state.get('intentResult', '')}\n"
            f"parseResult={state.get('parseResult', '')}\n"
            f"jdResult={state.get('jdResult', '')}"
        )
        defaults = ["Java Spring Boot Kafka K8s 项目经验", "支付中台 重构 后端 架构", "高并发 稳定性 性能优化"]
        queries = _queries_from_intent(state, defaults, "技术深度")
        fast_lane = _fast_lane_resume(resume_text)
        plan_event_id = await _emit_deterministic_plan_round(
            state,
            "tech_eval",
            "TechEvalAgent",
            4,
            1,
            json.dumps({
                "queries": queries,
                "focus": "技术深度、工程经验、JD requiredSkills",
                "harness": state.get("harnessPlan", {}).get("governance", {}),
                "toolBudget": state.get("harnessPlan", {}).get("toolBudgets", {}).get("TechEvalAgent", {}),
            }, ensure_ascii=False),
            plan_user,
            has_tool_calls=not fast_lane,
        )

        if fast_lane:
            result = _deterministic_tech_result(state)
            await _emit_deterministic_final_round(
                state,
                "tech_eval",
                "TechEvalAgent",
                4,
                2,
                result,
                "Agent-Harness fast-lane: sparse resume, skip expensive TechEval LLM and use resume_text_only evidence.",
            )
            end_span(agent_span_id, output_data=result, ended_at=now_iso(), status="SUCCESS",
                     duration_ms=int((time.time() - start_ms) * 1000))
            flush()
            duration = int((time.time() - start_ms) * 1000)
            await _emit_node_end(state, "tech_eval", "TechEvalAgent", 4, "SUCCESS", duration)
            return {"techResult": result, "completedNodes": ["tech_eval"]}

        rag_raw = await _run_tool_with_span(
            state, "tech_eval", "TechEvalAgent", 4, 1,
            "milvus_resume_batch_search", {"queries": queries, "topK": 3, "resumeText": resume_text},
            milvus_resume_batch_search(queries, 3, resume_text), agent_span_id, plan_event_id,
        )
        if re.search(r"github\.com|gitlab\.com|gitee\.com|https?://", resume_text or "", re.I):
            profile_raw = await _run_tool_with_span(
                state, "tech_eval", "TechEvalAgent", 4, 1,
                "github_enrichment", {"resumeText": resume_text},
                github_enrichment(resume_text), agent_span_id, plan_event_id,
            )
        else:
            profile_raw = '{"skipped": true, "reason": "no external profile url in resume"}'
        if _harness_eval_lane(state):
            result = _deterministic_tech_result(state)
            await _emit_deterministic_final_round(
                state,
                "tech_eval",
                "TechEvalAgent",
                4,
                2,
                result,
                f"Agent-Harness evaluator: context_pack + RAG evidence, skip repeated TechEval LLM.\nRAG={_compact_text(rag_raw, 800)}\nProfile={_compact_text(profile_raw, 400)}",
            )
            end_span(agent_span_id, output_data=result, ended_at=now_iso(), status="SUCCESS",
                     duration_ms=int((time.time() - start_ms) * 1000))
            flush()
            duration = int((time.time() - start_ms) * 1000)
            await _emit_node_end(state, "tech_eval", "TechEvalAgent", 4, "SUCCESS", duration)
            return {"techResult": result, "completedNodes": ["tech_eval"]}
        user = (
            f"简历证据上下文：{_compact_text(resume_text, 4200)}\n"
            f"意图识别：{state.get('intentResult', '')}\n"
            f"解析结果：{state.get('parseResult', '')}\n"
            f"JD结果：{state.get('jdResult', '')}\n"
            f"HarnessContext/知识库注入：{_compact_text(json.dumps(state.get('harnessContext', {}), ensure_ascii=False), 1800)}\n"
            f"RAG证据：{_compact_text(rag_raw, 1800)}\n"
            f"外部画像：{profile_raw}\n"
            f"工具健康：{json.dumps(state.get('toolHealth', {}), ensure_ascii=False)}\n"
            "请基于完整简历为主证据输出技术评估 JSON。"
        )
        result = await run_llm_node(
            "tech_eval", "TechEvalAgent", 4, TECH_PROMPT, user, ws,
            agent_span_id=agent_span_id, round_index=2, max_tokens=900,
        )
        end_span(agent_span_id, output_data=result, ended_at=now_iso(), status="SUCCESS",
                 duration_ms=int((time.time() - start_ms) * 1000))
        flush()
        duration = int((time.time() - start_ms) * 1000)
        await _emit_node_end(state, "tech_eval", "TechEvalAgent", 4, "SUCCESS", duration)
        return {"techResult": result, "completedNodes": ["tech_eval"]}
    except Exception as exc:
        end_span(agent_span_id, output_data=str(exc), ended_at=now_iso(), status="FAILED",
                 duration_ms=int((time.time() - start_ms) * 1000))
        flush()
        await _emit_node_end(state, "tech_eval", "TechEvalAgent", 4, "FAILED",
                             int((time.time() - start_ms) * 1000), str(exc))
        raise


async def project_eval_node(state: GraphState) -> Dict[str, Any]:
    await _emit_node_start(state, "project_eval", "ProjectEvalAgent", 4)
    start_ms = time.time()
    trace_id = state["traceId"]
    agent_span_id = start_agent_span(
        trace_id,
        "agent.4.project_eval",
        {"nodeId": "project_eval", "agentName": "ProjectEvalAgent", "workflowRunId": state.get("workflowRunId")},
        input_data={
            "resumeText": state.get("resumeText", ""),
            "parseResult": state.get("parseResult", ""),
            "jdResult": state.get("jdResult", ""),
        },
        started_at=now_iso(),
    )
    try:
        ws = _to_workflow_state(state)
        resume_text = state.get("resumeText", "")
        plan_user = (
            f"resumeText={resume_text}\n"
            f"intentResult={state.get('intentResult', '')}\n"
            f"parseResult={state.get('parseResult', '')}\n"
            f"jdResult={state.get('jdResult', '')}"
        )
        defaults = ["项目经历 架构 重构 中台", "核心业务 项目 贡献 复杂度", "项目 真实性 验证"]
        queries = _queries_from_intent(state, defaults, "项目真实性与复杂度")
        plan_event_id = await _emit_deterministic_plan_round(
            state,
            "project_eval",
            "ProjectEvalAgent",
            4,
            1,
            json.dumps({
                "queries": queries,
                "focus": "项目复杂度、贡献边界、真实性",
                "harness": state.get("harnessPlan", {}).get("governance", {}),
                "toolBudget": state.get("harnessPlan", {}).get("toolBudgets", {}).get("ProjectEvalAgent", {}),
            }, ensure_ascii=False),
            plan_user,
        )

        rag_raw = await _run_tool_with_span(
            state, "project_eval", "ProjectEvalAgent", 4, 1,
            "milvus_resume_batch_search", {"queries": queries, "topK": 3, "resumeText": resume_text},
            milvus_resume_batch_search(queries, 3, resume_text), agent_span_id, plan_event_id,
        )
        if _harness_eval_lane(state):
            result = _deterministic_project_result(state, rag_raw)
            await _emit_deterministic_final_round(
                state,
                "project_eval",
                "ProjectEvalAgent",
                4,
                2,
                result,
                f"Agent-Harness evaluator: project depth from parseResult + RAG evidence.\nRAG={_compact_text(rag_raw, 800)}",
            )
            end_span(agent_span_id, output_data=result, ended_at=now_iso(), status="SUCCESS",
                     duration_ms=int((time.time() - start_ms) * 1000))
            flush()
            duration = int((time.time() - start_ms) * 1000)
            await _emit_node_end(state, "project_eval", "ProjectEvalAgent", 4, "SUCCESS", duration)
            return {"projectResult": result, "completedNodes": ["project_eval"]}
        user = (
            f"简历证据上下文：{_compact_text(resume_text, 4200)}\n"
            f"意图识别：{state.get('intentResult', '')}\n"
            f"解析结果：{state.get('parseResult', '')}\n"
            f"JD结果：{state.get('jdResult', '')}\n"
            f"HarnessContext/知识库注入：{_compact_text(json.dumps(state.get('harnessContext', {}), ensure_ascii=False), 1800)}\n"
            f"RAG证据：{_compact_text(rag_raw, 1800)}\n"
            f"工具健康：{json.dumps(state.get('toolHealth', {}), ensure_ascii=False)}\n"
            "请基于完整简历为主证据输出项目评估 JSON。"
        )
        result = await run_llm_node(
            "project_eval", "ProjectEvalAgent", 4, PROJECT_PROMPT, user, ws,
            agent_span_id=agent_span_id, round_index=2, max_tokens=900,
        )
        end_span(agent_span_id, output_data=result, ended_at=now_iso(), status="SUCCESS",
                 duration_ms=int((time.time() - start_ms) * 1000))
        flush()
        duration = int((time.time() - start_ms) * 1000)
        await _emit_node_end(state, "project_eval", "ProjectEvalAgent", 4, "SUCCESS", duration)
        return {"projectResult": result, "completedNodes": ["project_eval"]}
    except Exception as exc:
        end_span(agent_span_id, output_data=str(exc), ended_at=now_iso(), status="FAILED",
                 duration_ms=int((time.time() - start_ms) * 1000))
        flush()
        await _emit_node_end(state, "project_eval", "ProjectEvalAgent", 4, "FAILED",
                             int((time.time() - start_ms) * 1000), str(exc))
        raise


async def risk_eval_node(state: GraphState) -> Dict[str, Any]:
    from datetime import date
    await _emit_node_start(state, "risk_eval", "RiskAgent", 4)
    start_ms = time.time()
    trace_id = state["traceId"]
    current_date = date.today().isoformat()
    agent_span_id = start_agent_span(
        trace_id,
        "agent.4.risk_eval",
        {"nodeId": "risk_eval", "agentName": "RiskAgent", "workflowRunId": state.get("workflowRunId")},
        input_data={
            "resumeText": state.get("resumeText", ""),
            "parseResult": state.get("parseResult", ""),
            "jdResult": state.get("jdResult", ""),
        },
        started_at=now_iso(),
    )
    try:
        ws = _to_workflow_state(state)
        resume_text = state.get("resumeText", "")
        plan_user = f"resumeText={resume_text}\nintentResult={state.get('intentResult', '')}\nparseResult={state.get('parseResult', '')}"
        defaults = ["跳槽 空白期 时间线", "技能夸大 简历真实性", "在职 实习 时间冲突"]
        queries = _queries_from_intent(state, defaults, "风险验证")
        plan_event_id = await _emit_deterministic_plan_round(
            state,
            "risk_eval",
            "RiskAgent",
            4,
            1,
            json.dumps({
                "queries": queries,
                "focus": "时间线、真实性、技能夸大、JD 缺口",
                "harness": state.get("harnessPlan", {}).get("governance", {}),
                "toolBudget": state.get("harnessPlan", {}).get("toolBudgets", {}).get("RiskAgent", {}),
            }, ensure_ascii=False),
            plan_user,
        )

        timeline_raw = await _run_tool_with_span(
            state, "risk_eval", "RiskAgent", 4, 1,
            "timeline_validator", {"resumeText": resume_text},
            timeline_validator(resume_text), agent_span_id, plan_event_id,
        )
        rag_raw = await _run_tool_with_span(
            state, "risk_eval", "RiskAgent", 4, 1,
            "milvus_resume_batch_search", {"queries": queries, "topK": 3, "resumeText": resume_text},
            milvus_resume_batch_search(queries, 3, resume_text), agent_span_id, plan_event_id,
        )
        if _harness_eval_lane(state):
            result = _deterministic_risk_result(state, timeline_raw, rag_raw)
            await _emit_deterministic_final_round(
                state,
                "risk_eval",
                "RiskAgent",
                4,
                2,
                result,
                f"Agent-Harness evaluator: risk from timeline + quantification + RAG evidence.\nTimeline={_compact_text(timeline_raw, 400)}\nRAG={_compact_text(rag_raw, 800)}",
            )
            end_span(agent_span_id, output_data=result, ended_at=now_iso(), status="SUCCESS",
                     duration_ms=int((time.time() - start_ms) * 1000))
            flush()
            duration = int((time.time() - start_ms) * 1000)
            await _emit_node_end(state, "risk_eval", "RiskAgent", 4, "SUCCESS", duration)
            return {"riskResult": result, "completedNodes": ["risk_eval"]}
        user = (
            f"当前日期：{current_date}\n"
            f"简历证据上下文：{_compact_text(resume_text, 4200)}\n"
            f"意图识别：{state.get('intentResult', '')}\n"
            f"解析结果：{state.get('parseResult', '')}\n"
            f"JD匹配：{state.get('jdResult', '')}\n"
            f"HarnessContext/知识库注入：{_compact_text(json.dumps(state.get('harnessContext', {}), ensure_ascii=False), 1800)}\n"
            f"时间线校验：{timeline_raw}\n"
            f"RAG证据：{_compact_text(rag_raw, 1800)}\n"
            f"工具健康：{json.dumps(state.get('toolHealth', {}), ensure_ascii=False)}\n"
            "请基于完整简历为主证据输出风险评估 JSON。"
        )
        result = await run_llm_node(
            "risk_eval", "RiskAgent", 4, RISK_PROMPT, user, ws,
            agent_span_id=agent_span_id, round_index=2, max_tokens=800,
        )
        end_span(agent_span_id, output_data=result, ended_at=now_iso(), status="SUCCESS",
                 duration_ms=int((time.time() - start_ms) * 1000))
        flush()
        duration = int((time.time() - start_ms) * 1000)
        await _emit_node_end(state, "risk_eval", "RiskAgent", 4, "SUCCESS", duration)
        return {"riskResult": result, "completedNodes": ["risk_eval"]}
    except Exception as exc:
        end_span(agent_span_id, output_data=str(exc), ended_at=now_iso(), status="FAILED",
                 duration_ms=int((time.time() - start_ms) * 1000))
        flush()
        await _emit_node_end(state, "risk_eval", "RiskAgent", 4, "FAILED",
                             int((time.time() - start_ms) * 1000), str(exc))
        raise


async def evidence_fusion_node(state: GraphState) -> Dict[str, Any]:
    enabled = set(((state.get("harnessPlan") or {}).get("route") or {}).get("enabledAgents") or [])
    required_results = []
    if "tech_eval" in enabled:
        required_results.append("techResult")
    if "project_eval" in enabled:
        required_results.append("projectResult")
    if "risk_eval" in enabled:
        required_results.append("riskResult")
    missing = [name for name in required_results if not state.get(name)]
    if missing:
        raise RuntimeError(f"evidence_fusion missing upstream results: {','.join(missing)}")
    await _emit_node_start(state, "evidence_fusion", "EvidenceFusionAgent", 5)
    start_ms = time.time()
    try:
        coverage_checklist = _resume_coverage_checklist(state.get("resumeText", ""), state.get("parseResult", ""))
        harness_reflection = build_harness_reflection(
            state.get("harnessPlan", {}),
            state.get("toolHealth", {}),
            coverage_checklist,
            state.get("techResult", ""),
            state.get("projectResult", ""),
            state.get("riskResult", ""),
        )
        user = (
            f"意图识别：{state.get('intentResult', '')}\n"
            f"AgentHarness：{json.dumps(state.get('harnessPlan', {}), ensure_ascii=False)}\n"
            f"HarnessContext：{json.dumps(state.get('harnessContext', {}), ensure_ascii=False)}\n"
            f"JD匹配：{state.get('jdResult', '')}\n"
            f"技术：{_compact_text(state.get('techResult', ''), 1600)}\n"
            f"项目：{_compact_text(state.get('projectResult', ''), 1600)}\n"
            f"风险：{_compact_text(state.get('riskResult', ''), 1600)}\n"
            f"工具健康：{json.dumps(state.get('toolHealth', {}), ensure_ascii=False)}\n"
            f"HarnessReflection：{json.dumps(harness_reflection, ensure_ascii=False)}"
        )
        result = json.dumps(
            {
                "evidenceChain": [
                    {"source": "intent", "claim": state.get("intentResult", ""), "evidenceSource": "llm_intent"},
                    {"source": "jd", "claim": state.get("jdResult", ""), "evidenceSource": "jd_match"},
                    {"source": "tech", "claim": state.get("techResult", ""), "evidenceSource": "resume_text_and_rag"},
                    {"source": "project", "claim": state.get("projectResult", ""), "evidenceSource": "resume_text_and_rag"},
                    {"source": "risk", "claim": state.get("riskResult", ""), "evidenceSource": "resume_text_and_checks"},
                ],
                "confidence": 0.78,
                "keyFindings": [
                    "完整简历原文始终作为主证据。",
                    "RAG/外部画像只用于定位和补充，不替代原文。",
                    "最终报告必须覆盖 resumeCoverageChecklist。",
                ],
                "toolHealth": state.get("toolHealth", {}),
                "harnessReflection": harness_reflection,
            },
            ensure_ascii=False,
        )
        await _emit_deterministic_final_round(
            state,
            "evidence_fusion",
            "EvidenceFusionAgent",
            5,
            1,
            result,
            user,
        )
        await _emit_node_end(state, "evidence_fusion", "EvidenceFusionAgent", 5, "SUCCESS", int((time.time() - start_ms) * 1000))
        return {"fusionResult": result, "completedNodes": ["evidence_fusion"]}
    except Exception as exc:
        await _emit_node_end(state, "evidence_fusion", "EvidenceFusionAgent", 5, "FAILED", int((time.time() - start_ms) * 1000), str(exc))
        raise


GENERIC_PLACEHOLDERS = {
    "技术栈匹配度较好",
    "项目经历具备追问价值",
    "关键贡献建议面试验证",
}


def _normalize_bullet(text: str) -> str:
    cleaned = re.sub(r"\*\*(.+?)\*\*", r"\1", text or "")
    cleaned = re.sub(r"^[-*•\d.)\s]+", "", cleaned).strip()
    return cleaned


def _is_generic_placeholder(text: str) -> bool:
    normalized = _normalize_bullet(text)
    if not normalized:
        return True
    if normalized in GENERIC_PLACEHOLDERS:
        return True
    return len(normalized) < 8


def dedupe_non_empty(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        normalized = _normalize_bullet(item)
        if not normalized or _is_generic_placeholder(normalized):
            continue
        key = normalized.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(normalized)
    return result


def extract_bullets_after_titles(text: str, titles: list[str], limit: int = 8) -> list[str]:
    if not text:
        return []
    lines = text.splitlines()
    bullets: list[str] = []
    capture = False
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            heading = re.sub(r"^#+\s*", "", stripped)
            capture = any(title in heading for title in titles)
            continue
        if not capture:
            continue
        if stripped.startswith(("#", "---", "|")):
            break
        if re.match(r"^[-*•]\s+", stripped) or re.match(r"^\d+[.)]\s+", stripped):
            bullets.append(_normalize_bullet(stripped))
        if len(bullets) >= limit:
            break
    return dedupe_non_empty(bullets)[:limit]


def extract_signal_bullets(text: str, limit: int = 6) -> list[str]:
    if not text:
        return []
    bullets: list[str] = []
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            for key in ("highlights", "depthHighlights", "keyFindings", "strengths", "risks", "concerns", "weaknesses"):
                value = data.get(key)
                if isinstance(value, list):
                    for item in value:
                        if isinstance(item, str):
                            bullets.append(item)
                        elif isinstance(item, dict):
                            label = item.get("name") or item.get("title") or item.get("description")
                            if label:
                                bullets.append(str(label))
    except Exception:
        pass
    if not bullets:
        bullets = extract_bullets_after_titles(text, ["highlights", "concerns", "risks", "keyFindings"], limit)
    return dedupe_non_empty(bullets)[:limit]


def build_questions_from_risks(risks: list[str], strengths: list[str]) -> list[str]:
    questions: list[str] = []
    for risk in risks[:4]:
        questions.append(f"请结合简历说明：{risk} 的真实情况，以及你在其中的具体贡献。")
    for strength in strengths[:3]:
        questions.append(f"请深入展开：{strength} 背后的技术细节和可验证证据。")
    defaults = [
        "请详细说明最近一个项目的架构取舍及你负责的核心模块。",
        "你在团队中承担的是主导、核心开发还是协作角色？请举例说明。",
        "请举例说明一次线上问题定位、修复和复盘过程。",
        "请说明你在高并发/稳定性/性能优化方面的实际落地经验。",
        "请解释简历中关键技术栈的使用深度，而非仅停留在名词层面。",
        "如果入职当前岗位，你会如何在前 30 天验证并补齐关键能力？",
    ]
    for item in defaults:
        if len(questions) >= 10:
            break
        questions.append(item)
    return dedupe_non_empty(questions)[:10]


def extract_report_sections(
    report: str,
    tech_result: str = "",
    project_result: str = "",
    risk_result: str = "",
) -> dict[str, list[str]]:
    strengths = extract_bullets_after_titles(
        report,
        ["核心优势", "关键优势", "候选人亮点", "优势"],
        limit=8,
    )
    risks = extract_bullets_after_titles(
        report,
        ["关键风险", "风险评估", "需验证风险", "主要风险"],
        limit=6,
    )
    questions = extract_bullets_after_titles(
        report,
        ["面试追问", "追问建议", "面试验证", "建议验证点"],
        limit=10,
    )
    if not strengths:
        strengths = extract_signal_bullets(f"{tech_result}\n{project_result}", limit=6)
    if not risks:
        risks = extract_signal_bullets(risk_result, limit=5)
    if not questions:
        questions = build_questions_from_risks(risks, strengths)
    return {
        "strengths": dedupe_non_empty(strengths)[:8],
        "risks": dedupe_non_empty(risks)[:6],
        "interviewQuestions": dedupe_non_empty(questions)[:10],
    }


def _evidence_label(evidence: str) -> str:
    cleaned = re.sub(r"^[·\-\*\d\.\s、)）(（]+", "", (evidence or "").strip())
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned[:24]


def build_specific_interview_questions(state: GraphState) -> list[dict[str, str]]:
    resume_text = state.get("resumeText", "")
    signals = extract_project_signals(resume_text)
    risk = _json_obj(state.get("riskResult", ""))

    questions: list[dict[str, str]] = []

    def add_question(question: str, evidence: str, focus: str, expected: str,
                     prefix_evidence: bool = True, min_len: int = 12) -> None:
        text = (evidence or "").strip()
        if not text or len(text) < min_len:
            return
        if prefix_evidence:
            label = _evidence_label(text)
            stem = f"结合简历「{label}…」：{question}" if label else question
        else:
            stem = question
        questions.append({
            "question": stem,
            "resumeEvidence": text[:240],
            "focus": focus,
            "expectedAnswer": expected,
        })

    for evidence in signals["incident"][:2]:
        add_question(
            "请详细说明这次线上问题的完整排查链路：你先看了哪些监控或日志，如何缩小范围，最终定位到什么根因，怎么验证修复有效？",
            evidence,
            "线上故障排查能力",
            "能讲清楚监控指标、日志/dump/profile 工具、定位路径、修复动作、回归验证和复盘预防。",
        )

    for evidence in signals["performance"][:2]:
        add_question(
            "请说明这个性能优化问题的定位过程：慢在哪里，执行计划或调用链证据是什么，最终索引/缓存/架构方案怎么设计，优化前后指标是多少？",
            evidence,
            "性能优化与数据层能力",
            "能给出 EXPLAIN/调用链/指标对比，解释索引字段顺序、读写权衡和灰度上线方式。",
        )

    for evidence in signals["architecture"][:2]:
        add_question(
            "请说明这个架构或重构项目的设计：原系统痛点是什么，新方案模块如何拆分，如何保证兼容性、线程安全、灰度和回滚？",
            evidence,
            "系统设计和工程落地",
            "能讲出模块边界、数据流、状态管理、并发安全、发布策略和风险控制。",
        )

    for evidence in signals["ai_agent"][:2]:
        add_question(
            "请说明这个 AI/Agent/RAG 项目的核心链路：DAG 节点如何定义，工具调用如何编排，Trace 如何关联，RAG 如何召回和评估？",
            evidence,
            "AI Agent 工程能力",
            "能讲清楚节点依赖、状态流转、Tool/Skill/RAG/LLM 调用、traceId/spanId、fallback 和质量评估。",
        )

    for evidence in signals["model_platform"][:2]:
        add_question(
            "请说明模型平台或多模型接入的抽象设计：不同模型 API 差异如何屏蔽，推理参数如何配置化，JSON Schema 如何保证，线上配置如何生效？",
            evidence,
            "模型平台工程化",
            "能讲清楚参数归一化、配置中心、灰度、生效机制、异常降级和审计字段。",
        )

    for evidence in signals["data_observability"][:2]:
        add_question(
            "请说明这段可观测或数据对账工作的实现：数据源是什么，如何发现缺口，如何补齐索引或链路，如何验证恢复并防止复发？",
            evidence,
            "可观测与数据一致性",
            "能讲清楚监控、日志、索引、对账、告警、恢复验证和长期防护。",
        )

    for evidence in signals["skills"][:4]:
        add_question(
            "请结合一个真实项目说明这里涉及的技术栈使用深度，包括关键参数、边界条件、踩坑和线上问题。",
            evidence,
            "基础技术深度",
            "不是背概念，而是能结合具体项目讲实现细节、故障和优化。",
        )

    for gap in _extract_jd_gaps(state.get("jdResult", ""))[:3]:
        add_question(
            f"JD 缺口「{_evidence_label(gap)}」你准备如何补齐？请结合已有经历说明迁移路径和入职后验证计划。",
            gap,
            "岗位匹配差距",
            "能说明已有相近经验、补齐计划和可验证里程碑。",
            prefix_evidence=False,
            min_len=4,
        )

    risks = risk.get("risks") if isinstance(risk.get("risks"), list) else []
    for item in risks[:3]:
        add_question(
            f"针对风险点「{_evidence_label(str(item))}」，请给出能证明真实性或能力深度的材料和现场解释。",
            str(item),
            "风险验证",
            "能提供时间线、项目材料、代码链接、指标或复盘证据。",
            prefix_evidence=False,
            min_len=4,
        )

    deduped: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in questions:
        key = item["question"] + item["resumeEvidence"]
        if key not in seen:
            seen.add(key)
            deduped.append(item)
        if len(deduped) >= 12:
            break
    return deduped


def build_memory_calibration(state: GraphState) -> dict[str, Any]:
    """Honest episodic-memory calibration prior: only surfaced when >=3 similar past cases exist.

    Computes the score/recommendation distribution of similar historical evaluations so the report
    can calibrate consistency. This is strategy/reference context, never candidate fact.
    """
    mc = state.get("memoryContext") or {}
    episodic = mc.get("episodicHits") if isinstance(mc.get("episodicHits"), list) else []
    scores: list[float] = []
    recs: dict[str, int] = {}
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
        "note": "similar historical candidates calibration reference only, not candidate fact",
    }


def build_report_runtime_summary(state: GraphState) -> dict[str, Any]:
    plan = state.get("harnessPlan") or {}
    knowledge = plan.get("knowledgeInfluence") or {}
    memory = plan.get("memoryInfluence") or {}
    route = plan.get("route") or {}
    return {
        "reportMode": plan.get("reportMode"),
        "selectedAgents": route.get("enabledAgents", []),
        "skippedAgents": route.get("skippedAgents", {}),
        "estimatedLlmCalls": route.get("estimatedLlmCalls"),
        "llmCallsSavedVsFull": route.get("llmCallsSavedVsFull"),
        "memoryCalibration": build_memory_calibration(state),
        "knowledgeHitCount": knowledge.get("hitCount", 0),
        "knowledgeSnippets": [
            {
                "title": chunk.get("title"),
                "docType": chunk.get("docType"),
                "contentPreview": chunk.get("contentPreview"),
                "score": chunk.get("score"),
            }
            for chunk in (knowledge.get("chunks") or [])[:3]
            if isinstance(chunk, dict)
        ],
        "memoryStrategyHints": [
            {
                "type": item.get("type"),
                "appliesTo": item.get("appliesTo"),
                "recommendedAction": item.get("recommendedAction"),
            }
            for item in (memory.get("influences") or [])
            if isinstance(item, dict) and item.get("type") in ("semantic", "procedural")
        ][:3],
        "boundary": "knowledge and memory are evaluation standards only, not candidate facts",
    }


def build_history_calibration(state: GraphState) -> dict[str, Any]:
    """Aggregate similar past evaluations (episodic memory) into a scoring calibration anchor.

    This is the concrete, explainable use of memory: retrieval over our own evaluation history to
    keep scoring consistent across similar candidates. It is a calibration reference only, never a
    candidate fact.
    """
    mc = state.get("memoryContext") or {}
    episodic = [h for h in _json_list(mc.get("episodicHits")) if isinstance(h, dict)]
    scores: list[int] = []
    recs: list[str] = []
    cases: list[dict[str, Any]] = []
    for hit in episodic[:8]:
        evidence = hit.get("evidence") if isinstance(hit.get("evidence"), dict) else {}
        try:
            score = int(evidence.get("score") or hit.get("score") or 0)
        except (TypeError, ValueError):
            score = 0
        rec = str(evidence.get("recommendation") or hit.get("recommendation") or "")
        if score > 0:
            scores.append(score)
        if rec:
            recs.append(rec)
        cases.append({
            "traceId": hit.get("traceId"),
            "score": score,
            "recommendation": rec,
            "matchReason": hit.get("matchReason"),
        })
    if not cases:
        return {}
    avg_score = round(sum(scores) / len(scores), 1) if scores else None
    positive = sum(1 for r in recs if r in ("RECOMMEND", "STRONG_RECOMMEND"))
    recommend_rate = round(100 * positive / len(recs)) if recs else None
    return {
        "sampleSize": len(cases),
        "avgScore": avg_score,
        "recommendRatePct": recommend_rate,
        "similarCases": cases[:5],
        "usage": "scoring calibration reference only, not candidate fact",
    }


def build_report_context_pack(state: GraphState, coverage_checklist: str, skill_result: dict[str, Any]) -> dict[str, Any]:
    # Input tokens are cheap latency-wise; only output tokens drive ReportAgent time.
    # So we keep generous evidence context (restore detail) while output stays parallel + bounded.
    return {
        "resumeDigest": coverage_checklist,
        "resumeExcerpt": _compact_text(state.get("resumeText", ""), 3200),
        "intent": _json_obj(state.get("intentResult", "")),
        "jd": _compact_text(state.get("jdResult", ""), 1200),
        "tech": _compact_text(state.get("techResult", ""), 1600),
        "project": _compact_text(state.get("projectResult", ""), 1600),
        "risk": _compact_text(state.get("riskResult", ""), 1300),
        "fusion": _compact_text(state.get("fusionResult", ""), 900),
        "specificQuestions": build_specific_interview_questions(state),
        "runtimeSummary": build_report_runtime_summary(state),
        "skillResult": {
            "evidenceWeights": skill_result.get("evidenceWeights"),
            "conflicts": skill_result.get("conflicts"),
            "missingEvidence": skill_result.get("missingEvidence"),
            "reportHints": skill_result.get("reportHints"),
        },
    }


def render_specific_questions_md(questions: list[dict[str, Any]]) -> str:
    """Render deterministic, specific interview questions as markdown under 面试追问.

    Questions are already extracted from resume signals (no LLM), guaranteeing specificity
    and removing the slowest part of the report generation.
    """
    if not questions:
        return ""
    lines = ["## 面试追问"]
    for idx, item in enumerate(questions[:12], 1):
        if not isinstance(item, dict):
            continue
        focus = str(item.get("focus") or "").strip()
        question = str(item.get("question") or "").strip()
        evidence = str(item.get("resumeEvidence") or "").strip()
        expected = str(item.get("expectedAnswer") or "").strip()
        head = f"{idx}. （{focus}）{question}" if focus else f"{idx}. {question}"
        if evidence:
            head += f" ｜简历依据：{evidence[:90]}"
        lines.append(head)
        if expected:
            lines.append(f"   期待回答：{expected}")
    return "\n".join(lines)


def _assemble_report(core: str, evaluation: str, questions_md: str) -> str:
    parts = [core.strip(), evaluation.strip(), questions_md.strip()]
    return "\n\n".join(part for part in parts if part)


def build_report_harness_context(state: GraphState) -> dict[str, Any]:
    plan = state.get("harnessPlan") or {}
    memory = plan.get("memoryInfluence") if isinstance(plan.get("memoryInfluence"), dict) else {}
    knowledge = plan.get("knowledgeInfluence") if isinstance(plan.get("knowledgeInfluence"), dict) else {}
    return {
        "route": plan.get("route", {}),
        "reportMode": plan.get("reportMode"),
        "memoryInfluence": {
            "hitCount": memory.get("hitCount", 0),
            "appliedTo": memory.get("appliedTo", []),
            "influences": [
                {
                    "type": item.get("type"),
                    "appliesTo": item.get("appliesTo"),
                    "recommendedAction": item.get("recommendedAction"),
                    "matchReason": item.get("matchReason"),
                }
                for item in _json_list(memory.get("influences"))[:4]
                if isinstance(item, dict)
            ],
            "poisoningControl": memory.get("poisoningControl"),
        },
        "knowledgeInfluence": {
            "hitCount": knowledge.get("hitCount", 0),
            "injectedInto": knowledge.get("injectedInto", []),
            "chunks": [
                {
                    "title": chunk.get("title"),
                    "docType": chunk.get("docType"),
                    "sectionPath": chunk.get("sectionPath"),
                    "score": chunk.get("score"),
                    "contentPreview": chunk.get("contentPreview"),
                    "rerankReason": chunk.get("rerankReason"),
                }
                for chunk in _json_list(knowledge.get("chunks"))[:4]
                if isinstance(chunk, dict)
            ],
            "evidenceBoundary": knowledge.get("evidenceBoundary"),
        },
    }


def _recommendation_from_score(score: int) -> str:
    """Deterministic mapping from overall score to recommendation.

    Single source of truth so the report can NEVER show an inconsistent pair
    (e.g. 25 分 + RECOMMEND). The score bands are the authoritative decision.
    """
    if score >= 85:
        return "STRONG_RECOMMEND"
    if score >= 70:
        return "RECOMMEND"
    if score >= 55:
        return "NEED_MANUAL_REVIEW"
    return "NOT_RECOMMEND"


def _parse_score_and_recommendation(report: str) -> tuple[int, str]:
    score = 0
    m = re.search(r"综合评分[：:]\s*(\d+)", report)
    if m:
        score = max(0, min(100, int(m.group(1))))
    # Recommendation is derived from the score band, NOT parsed independently, so the
    # displayed score and recommendation are always consistent.
    return score, _recommendation_from_score(score)


def _sync_report_recommendation(report: str, rec: str) -> str:
    """Overwrite the report's 推荐决策 line so the visible markdown matches the derived rec."""
    if not report:
        return report
    return re.sub(r"(##\s*推荐决策[：:]\s*)[A-Za-z_|]+", r"\g<1>" + rec, report, count=1)


async def report_node(state: GraphState) -> Dict[str, Any]:
    resume_text = state.get("resumeText", "")
    await _emit_node_start(state, "report", "ReportAgent", 6)
    start_ms = time.time()
    trace_id = state["traceId"]
    agent_span_id = start_agent_span(
        trace_id,
        "agent.6.report",
        {"nodeId": "report", "agentName": "ReportAgent", "workflowRunId": state.get("workflowRunId")},
        input_data={"resumeTextLength": len(resume_text), "harnessPlan": state.get("harnessPlan", {})},
        started_at=now_iso(),
    )
    coverage_checklist = _resume_coverage_checklist(resume_text, state.get("parseResult", ""))
    harness_reflection = build_harness_reflection(
        state.get("harnessPlan", {}),
        state.get("toolHealth", {}),
        coverage_checklist,
        state.get("techResult", ""),
        state.get("projectResult", ""),
        state.get("riskResult", ""),
    )
    try:
        skill_raw = await _run_tool_with_span(
            state,
            "report",
            "ReportAgent",
            6,
            1,
            "execute_skill",
            {"skillName": "evidence_synthesis", "task": "Use this skill to structure the final HR evidence report."},
            execute_skill("evidence_synthesis", "Use this skill to structure the final HR evidence report."),
            agent_span_id,
            make_event_id(trace_id, "report", 1, "generation", 1),
        )
        if _fast_lane_resume(resume_text):
            result = _deterministic_report(state, skill_raw, coverage_checklist, harness_reflection)
            await _emit_deterministic_final_round(
                state,
                "report",
                "ReportAgent",
                6,
                2,
                result,
                "Agent-Harness synthesis: build complete HR report from structured evidence, HarnessContext and evidence_synthesis skill without repeated long-context report LLM.",
            )
            end_span(agent_span_id, output_data=result, ended_at=now_iso(), status="SUCCESS",
                     duration_ms=int((time.time() - start_ms) * 1000))
            flush()
            await _emit_node_end(state, "report", "ReportAgent", 6, "SUCCESS", int((time.time() - start_ms) * 1000))
            score, rec = _parse_score_and_recommendation(result)
            result = _sync_report_recommendation(result, rec)
            sections = extract_report_sections(
                report=result,
                tech_result=state.get("techResult", ""),
                project_result=state.get("projectResult", ""),
                risk_result=state.get("riskResult", ""),
            )
            return {
                "finalReport": result,
                "overallScore": score,
                "recommendation": rec,
                "strengths": sections["strengths"],
                "risks": sections["risks"],
                "interviewQuestions": sections["interviewQuestions"],
                "completedNodes": ["report"],
            }
        skill_result = _json_obj(skill_raw)
        report_pack = build_report_context_pack(state, coverage_checklist, skill_result)
        specific_questions = report_pack.get("specificQuestions") or []
        core_keys = ("resumeDigest", "resumeExcerpt", "intent", "jd", "tech", "project", "risk", "runtimeSummary", "skillResult")
        eval_keys = ("resumeDigest", "resumeExcerpt", "tech", "project", "risk", "fusion", "jd", "skillResult")
        core_user = json.dumps({k: report_pack[k] for k in core_keys if k in report_pack}, ensure_ascii=False)
        eval_user = json.dumps({k: report_pack[k] for k in eval_keys if k in report_pack}, ensure_ascii=False)

        # Latency: output tokens dominate, so generate the two narrative halves concurrently and
        # render 面试追问 deterministically (no LLM). 1 long 37s call -> 2 parallel ~16s calls.
        core_task = run_llm_node(
            "report",
            "ReportAgent",
            6,
            REPORT_CORE_PROMPT,
            core_user,
            _to_workflow_state(state),
            agent_span_id=agent_span_id,
            round_index=2,
            max_tokens=1300,
        )
        eval_task = generate_report_eval(eval_user, max_tokens=1600)
        core_result, eval_result = await asyncio.gather(core_task, eval_task)
        questions_md = render_specific_questions_md(specific_questions)
        result = _assemble_report(core_result, eval_result, questions_md)
        end_span(agent_span_id, output_data=result, ended_at=now_iso(), status="SUCCESS",
                 duration_ms=int((time.time() - start_ms) * 1000))
        flush()
        await _emit_node_end(state, "report", "ReportAgent", 6, "SUCCESS", int((time.time() - start_ms) * 1000))
        score, rec = _parse_score_and_recommendation(result)
        result = _sync_report_recommendation(result, rec)
        sections = extract_report_sections(
            report=result,
            tech_result=state.get("techResult", ""),
            project_result=state.get("projectResult", ""),
            risk_result=state.get("riskResult", ""),
        )
        return {
            "finalReport": result,
            "overallScore": score,
            "recommendation": rec,
            "strengths": sections["strengths"],
            "risks": sections["risks"],
            "interviewQuestions": sections["interviewQuestions"],
            "completedNodes": ["report"],
        }
    except Exception as exc:
        end_span(agent_span_id, output_data=str(exc), ended_at=now_iso(), status="FAILED",
                 duration_ms=int((time.time() - start_ms) * 1000))
        flush()
        await _emit_node_end(state, "report", "ReportAgent", 6, "FAILED", int((time.time() - start_ms) * 1000), str(exc))
        raise


def route_phase4(state: GraphState) -> list[str]:
    route = ((state.get("harnessPlan") or {}).get("route") or {})
    enabled = set(route.get("selectedAgents") or route.get("enabledAgents") or [])
    routes: list[str] = []
    if "tech_eval" in enabled:
        routes.append("tech_eval")
    if "project_eval" in enabled:
        routes.append("project_eval")
    if "risk_eval" in enabled:
        routes.append("risk_eval")
    return routes or ["evidence_fusion"]


def build_graph() -> Any:
    builder = StateGraph(GraphState)
    builder.add_node("intent", intent_node)
    builder.add_node("resume_parse", resume_parse_node)
    builder.add_node("jd_match", jd_match_node)
    builder.add_node("knowledge_context", knowledge_context_node)
    builder.add_node("tech_eval", tech_eval_node)
    builder.add_node("project_eval", project_eval_node)
    builder.add_node("risk_eval", risk_eval_node)
    builder.add_node("evidence_fusion", evidence_fusion_node)
    builder.add_node("report", report_node)

    # intent (memory + harness plan) and resume_parse only depend on resumeText, so run them in
    # parallel; jd_match waits for BOTH (it consumes intentResult + parseResult). Saves ~4-5s.
    builder.add_edge(START, "intent")
    builder.add_edge(START, "resume_parse")
    builder.add_edge("intent", "jd_match")
    builder.add_edge("resume_parse", "jd_match")
    builder.add_edge("jd_match", "knowledge_context")
    builder.add_conditional_edges("knowledge_context", route_phase4)
    builder.add_edge("tech_eval", "evidence_fusion")
    builder.add_edge("project_eval", "evidence_fusion")
    builder.add_edge("risk_eval", "evidence_fusion")
    builder.add_edge("evidence_fusion", "report")
    builder.add_edge("report", END)
    return builder


async def compile_graph() -> Any:
    builder = build_graph()
    checkpointer = await get_checkpointer()
    if checkpointer:
        return builder.compile(checkpointer=checkpointer)
    return builder.compile()


async def run_workflow(initial: WorkflowState) -> WorkflowResultPayload:
    start_ms = time.time()
    start_trace(
        initial.traceId,
        input_text=initial.resumeText,
        metadata={"workflowRunId": initial.workflowRunId, "resumeTextLength": len(initial.resumeText or "")},
    )
    graph = await compile_graph()
    state: GraphState = initial.model_dump()
    config = {
        "configurable": {
            "thread_id": f"{initial.traceId}:{initial.workflowRunId}",
            "checkpoint_ns": initial.workflowRunId,
        }
    }
    try:
        final_state = await graph.ainvoke(state, config=config)
        duration = int((time.time() - start_ms) * 1000)
        final_report = (final_state.get("finalReport") or "").strip()
        if not final_report:
            duration = int((time.time() - start_ms) * 1000)
            end_trace(
                initial.traceId,
                output_data="ReportAgent 未生成最终报告",
                metadata={
                    "workflowRunId": initial.workflowRunId,
                    "durationMs": duration,
                    "status": "FAILED",
                    "failedNode": "report",
                },
            )
            return WorkflowResultPayload(
                traceId=initial.traceId,
                workflowRunId=initial.workflowRunId,
                status="FAILED",
                summary="ReportAgent 未生成最终报告",
                durationMs=duration,
                failedNode="report",
                errorMessage="empty finalReport from ReportAgent",
            )
        duration = int((time.time() - start_ms) * 1000)
        end_trace(
            initial.traceId,
            output_data=final_report,
            metadata={
                "workflowRunId": initial.workflowRunId,
                "durationMs": duration,
                "status": "SUCCESS",
            },
        )
        return WorkflowResultPayload(
            traceId=initial.traceId,
            workflowRunId=initial.workflowRunId,
            status="SUCCESS",
            summary=final_report,
            overallScore=final_state.get("overallScore", 0),
            recommendation=final_state.get("recommendation", "NEED_MANUAL_REVIEW"),
            strengths=final_state.get("strengths", []),
            risks=final_state.get("risks", []),
            interviewQuestions=final_state.get("interviewQuestions", []),
            durationMs=duration,
            tokenCost=max(duration // 100, 1),
        )
    except Exception as exc:
        logger.exception("workflow failed trace=%s", initial.traceId)
        duration = int((time.time() - start_ms) * 1000)
        error_text = str(exc)
        failed_node = "workflow"
        if error_text.startswith("workflow node ") and " failed:" in error_text:
            failed_node = error_text.split("workflow node ", 1)[1].split(" failed:", 1)[0]
        summary = error_text if error_text.startswith("workflow node ") else f"workflow failed: {error_text}"
        end_trace(
            initial.traceId,
            output_data=summary,
            metadata={
                "workflowRunId": initial.workflowRunId,
                "durationMs": duration,
                "status": "FAILED",
                "failedNode": failed_node,
            },
        )
        return WorkflowResultPayload(
            traceId=initial.traceId,
            workflowRunId=initial.workflowRunId,
            status="FAILED",
            summary=summary,
            durationMs=duration,
            failedNode=failed_node,
            errorMessage=error_text,
        )
