from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any, Dict, List, Optional, Set

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import StructuredTool
from langchain_openai import ChatOpenAI

from app.config import settings, normalized_deepseek_base_url
from app.events import emit_event, make_event_id, messages_preview, now_iso, preview_text
from app.langfuse_tracing import (
    action_span,
    end_span,
    flush,
    record_generation,
    start_agent_span,
)
from app.models import ToolCallRecord, TraceEvent, WorkflowState
from app.tool_registry import build_tools_for_agent
from app.tool_semantics import (
    TOOL_BUDGET_BY_AGENT,
    build_tool_substeps,
    extract_retrieval_metadata,
    get_tool_semantics,
    is_malformed_final_output,
    is_rag_tool,
    observation_kind_for,
    rag_failure_key,
    stable_input_hash,
    tool_signature,
)

logger = logging.getLogger(__name__)

MAX_AGENT_ROUNDS = 4
RAG_FAILURE_THRESHOLD = 2

INTENT_PROMPT = """你是招聘评估路由专家，作用不是贴标签，而是为后续 Agent 制定评估策略。

必须基于简历原文判断：
1. candidateType：TECH|PRODUCT|DESIGN|OPERATION|UNKNOWN
2. experienceLevel：INTERN|JUNIOR|MID|SENIOR|STAFF|UNKNOWN
3. targetRole：候选人最可能投递/适配的岗位方向
4. evaluationStrategy：后续评估的主策略，例如 focus_on_backend_depth / verify_project_authenticity / validate_basic_fit
5. routingHints：下游 Agent 要重点看的方向
6. requiredSkills：从简历与目标岗位推断必须验证的技能
7. evidenceGaps：简历缺失但面试必须补齐的信息
8. ragQueries：给 RAG 用的 3-5 个检索意图，不是工具调用
9. interviewFocus：面试追问要优先验证的事项
10. agentWeights：tech/project/risk/report 的权重，0-1 小数

输出严格 JSON：
{"candidateType":"TECH","experienceLevel":"MID","targetRole":"","evaluationStrategy":"","routingHints":[],"requiredSkills":[],"evidenceGaps":[],"ragQueries":[],"interviewFocus":[],"agentWeights":{"tech":0.35,"project":0.35,"risk":0.2,"report":0.1}}"""

PARSE_PROMPT = """你是简历解析专家。提取结构化简历信息。
输出严格 JSON：{"name":"","summary":"","skills":[],"projects":[],"education":[]}"""

JD_MATCH_PROMPT = """你是岗位匹配专家。基于 JD 检索结果和要求抽取结果，评估岗位匹配度。
必须使用 IntentAgent 输出的 candidateType、experienceLevel、routingHints、requiredSkills 来解释岗位匹配方向。
输出严格 JSON：{"matchedJd":"","matchScore":0.8,"requirements":[],"preferredSkills":[],"gaps":[]}"""

TECH_PROMPT = """你是技术评估专家。必须基于完整简历原文评分，embedding RAG 仅用于定位和补充证据。

硬性规则：
1. resumeText 是主证据；RAG chunk 不能替代完整简历。
2. 若 RAG 返回 fallback/0 命中，基于 resumeText 评估并标记 evidenceSource=resume_text_only。
3. 每个结论必须包含 evidenceSource：resume_text | rag_chunk | external_profile。
4. 必须使用 IntentAgent 的 routingHints/requiredSkills 决定技术评估重点。

输出约束（务必遵守，保证 JSON 完整不被截断）：
- dimensions 最多 4 个；每个 dimension 的 evidenceQuotes 最多 1 条且 <=40 字。
- highlights、weaknesses 各最多 4 条，每条 <=50 字。
- 只输出紧凑 JSON，禁止任何多余解释、换行注释或 markdown。

输出严格 JSON：{"dimensions":[{"name":"","score":0,"evidenceSource":"resume_text","evidenceQuotes":[]}],"overallTechScore":72,"highlights":[],"weaknesses":[],"evidenceSource":"","toolHealth":{}}"""

PROJECT_PROMPT = """你是项目深度分析专家。评估项目复杂度、贡献度和真实性。

硬性规则：
1. resumeText 是主证据；RAG 只用于补充定位，不能因 chunk 少而声称简历信息不足。
2. RAG 失败时基于 resumeText 和 parseResult 继续，标记 evidenceSource=resume_text_only。
3. 每个项目结论包含 evidenceSource 和可验证证据。
4. 必须使用 IntentAgent 的 candidateType/experienceLevel 判断项目深度预期，不允许脱离意图结果打分。

输出约束（务必遵守，保证 JSON 完整不被截断）：
- projects 最多 4 个；每个项目的关键描述 <=60 字。
- depthHighlights、concerns 各最多 4 条，每条 <=50 字。
- 只输出紧凑 JSON，禁止任何多余解释或 markdown。

输出严格 JSON：{"projects":[{"name":"","score":0,"evidenceSource":"resume_text"}],"overallProjectScore":70,"depthHighlights":[],"concerns":[],"evidenceSource":"","toolHealth":{}}"""

RISK_PROMPT = """你是风险识别专家。检测跳槽、空白期、技能夸大等风险。
当前日期由用户消息提供，勿将未来实习/在职经历误判为风险。

硬性规则：resumeText 是主证据；RAG 仅作补充。
必须使用 IntentAgent 的 experienceLevel/routingHints 判断风险阈值，例如应届、实习、社招的时间线风险标准不同。

输出约束（务必遵守，保证 JSON 完整不被截断）：
- risks 最多 6 条，每条 <=60 字。
- 只输出紧凑 JSON，禁止任何多余解释或 markdown。

输出严格 JSON：{"riskLevel":"LOW","risks":[],"overallAssessment":"","evidenceSource":"","toolHealth":{}}"""

FUSION_PROMPT = """你是证据融合专家。只融合以下来源：
1. 完整简历原文 resumeText
2. embedding RAG 检索结果
3. JD 匹配结果
4. 外部画像（如可用）

禁止引用当前未启用的数据源；只允许使用完整简历原文、embedding RAG、JD 与外部画像。
必须输出合法 JSON，禁止 DSML/tool_calls 格式：
{"evidenceChain":[],"confidence":0.85,"keyFindings":[],"toolHealth":{}}"""

REPORT_PROMPT = """你是 HR 评估报告专家。综合所有评估结果生成 Markdown 报告。

硬性规则：
1. resumeText 是主证据，embedding RAG 只是补充定位证据。
2. 如果 resumeTextLength > 0，禁止写“没有获取完整简历”。
3. 如果 resumeText 很短，只能写“输入简历本身信息较少”，不能写“系统没有拿到完整简历”。
4. 禁止引用当前未启用的数据源；只允许使用完整简历原文、embedding RAG、JD 与外部画像。
5. 报告不是摘要卡片，必须生成可直接用于面试评审的完整 Markdown，不能少于 1400 个中文字符。
6. 必须优先覆盖用户消息中的 resumeCoverageChecklist。该清单里的教育、实习、项目、技术栈、时间线、成果、风险点都要在报告中被引用或评估，不能只挑少数点。
7. 如果 checklist 中有多个项目/经历，必须逐项写到“核心优势”“关键风险”或“面试追问”中，不能只概括成“项目丰富”。
8. 必须使用 IntentAgent 输出解释评估策略：候选人类型、经验级别、routingHints、requiredSkills 如何影响评分、风险和面试追问。
9. 每个章节标题后必须直接输出具体编号条目，禁止写“如下/以下/基于有限信息/围绕风险”这类过渡句。
10. 可以简要说明是否使用知识库 Rubric 或 Memory 策略参考，但禁止把 Harness 内部配置写进 HR 报告。
11. Memory 只能作为历史策略参考，不能作为候选人事实证据；知识库只能作为 Rubric/面试标准，不能作为候选人事实证据。
12. 必须优先使用 SpecificInterviewQuestionCandidates 中的具体项目/技能/风险/JD gap 生成面试追问。

必须包含以下章节（标题格式保持一致）：
## 综合评分：XX/100
## 推荐决策：STRONG_RECOMMEND|RECOMMEND|NEED_MANUAL_REVIEW|NOT_RECOMMEND
## 证据来源说明
- 说明完整简历原文、embedding RAG、JD、外部画像的使用情况及降级状态
## 简历覆盖核对
- 按 resumeCoverageChecklist 逐项说明：已覆盖哪些教育/实习/项目/技能/时间线；哪些无法验证；不能遗漏清单中的重要条目
## 评估标准说明
- 简要说明是否使用知识库 Rubric 或历史策略记忆。
- 必须说明：知识库和 Memory 只作为评估标准/策略参考，不作为候选人事实证据。
- 禁止展开 route、toolBudgets、guardrails、evalPolicy 等内部配置。
## 核心优势
- 不是凑数量；必须覆盖 checklist 中所有可形成优势的经历/项目/技能。每条必须包含证据、项目或技能上下文，禁止泛化空话
## 关键风险
- 不是凑数量；必须覆盖 checklist 中所有需要验证或与 JD 有差距的条目。每条必须说明风险原因和面试验证方式
## 面试追问
- 必须针对 checklist 中的关键项目/实习/技能逐项追问，覆盖技术深度、项目真实性、风险验证、岗位匹配差距
- 每个问题必须点名一个具体简历条目或 JD 缺口，例如具体项目名、实习经历、技能名、时间段、指标；禁止“请详细说明关键项目/技能”这类泛泛句。
- 每个问题必须包含：问题、关联简历条目、考察点、期待回答。
## 综合结论

禁止只输出“技术栈匹配度较好”“项目经历具备追问价值”“关键贡献建议面试验证”等模板句。"""


REPORT_CORE_PROMPT = """你是 HR 评估报告专家。基于用户给出的结构化证据 JSON，生成评估报告的【前半部分】。

硬性规则：
1. resumeExcerpt / resumeDigest 是主证据，embedding RAG/JD 只是补充。
2. 如果有简历内容，禁止写“没有获取完整简历”。简历很短时只能写“输入简历本身信息较少”。
3. 评分必须有依据，要结合候选人类型、经验级别、技术栈与 JD 匹配度解释为什么是这个分数。
4. 简历覆盖核对必须按 resumeDigest 逐项说明：已覆盖哪些教育/实习/项目/技能/时间线，哪些无法验证，不能遗漏重要条目。
5. 知识库和 Memory 只作为评估标准/策略参考，不作为候选人事实证据；禁止展开 route、toolBudgets、guardrails 等内部配置。

只输出以下章节（标题格式严格一致），不要输出核心优势/关键风险/面试追问/综合结论：
## 综合评分：XX/100
## 推荐决策：STRONG_RECOMMEND|RECOMMEND|NEED_MANUAL_REVIEW|NOT_RECOMMEND
## 证据来源说明
- 说明完整简历原文、embedding RAG、JD、外部画像的使用情况及降级状态
## 简历覆盖核对
- 按 resumeDigest 逐项说明
## 评估标准说明
- 简要说明是否使用知识库 Rubric 或历史策略记忆，并强调其只作为标准/策略参考，不作为候选人事实

禁止输出模板套话。"""


REPORT_EVAL_PROMPT = """你是 HR 评估报告专家。基于用户给出的结构化证据 JSON，生成评估报告的【评估部分】。

硬性规则：
1. resumeExcerpt / 各 Agent 结果是主证据，每条优势/风险都要落到具体项目、技能、指标或时间线，禁止泛化空话。
2. 核心优势必须覆盖 resumeDigest 中所有可形成优势的经历/项目/技能；不是凑数量，但不能遗漏重要项目。
3. 关键风险必须覆盖所有需要验证或与 JD 有差距的条目，每条说明风险原因和面试验证方式。
4. 综合结论要给出录用倾向、最需要在面试中确认的 2-3 个点。
5. 知识库和 Memory 只作为标准/策略参考，不作为候选人事实证据。

只输出以下章节（标题格式严格一致），不要输出评分/推荐决策/面试追问：
## 核心优势
## 关键风险
## 综合结论

禁止只输出“技术栈匹配度较好”“项目经历具备追问价值”这类模板句。"""


async def generate_report_eval(user_content: str, max_tokens: int = 1500) -> str:
    """Generate the 优势/风险/结论 part of the report as a parallel sub-call (no separate trace round)."""
    try:
        msg = await _llm(max_tokens=max_tokens).ainvoke([
            SystemMessage(content=REPORT_EVAL_PROMPT),
            HumanMessage(content=user_content),
        ])
        text = msg.content if isinstance(msg, AIMessage) else str(msg)
        if text and text.strip():
            return text
    except Exception:
        pass
    return (
        "## 核心优势\n- 详见技术评估与项目评估结果。\n\n"
        "## 关键风险\n- 详见风险评估结果，需人工复核。\n\n"
        "## 综合结论\n- 评估部分生成异常，建议人工复核证据。"
    )


def _llm(max_tokens: int = 4096) -> ChatOpenAI:
    return ChatOpenAI(
        base_url=normalized_deepseek_base_url(),
        api_key=settings.deepseek_api_key or "sk-placeholder",
        model=settings.deepseek_model,
        temperature=0.2,
        max_tokens=max_tokens,
        streaming=False,
    )


def _msg_to_dict(msg: Any) -> Dict[str, Any]:
    if isinstance(msg, SystemMessage):
        return {"role": "system", "content": msg.content}
    if isinstance(msg, HumanMessage):
        return {"role": "user", "content": msg.content}
    if isinstance(msg, AIMessage):
        d: Dict[str, Any] = {"role": "assistant", "content": msg.content or ""}
        if msg.tool_calls:
            d["tool_calls"] = msg.tool_calls
        return d
    if isinstance(msg, ToolMessage):
        return {
            "role": "tool",
            "content": msg.content,
            "tool_call_id": msg.tool_call_id,
            "name": getattr(msg, "name", None),
        }
    return {"role": "unknown", "content": str(msg)}


def _parent_round_id(node_id: str, round_index: int) -> str:
    return f"{node_id}#{round_index}"


def normalize_tool_args(raw: Any) -> Dict[str, Any]:
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return {}
        try:
            parsed = json.loads(text)
            return parsed if isinstance(parsed, dict) else {"input": parsed}
        except json.JSONDecodeError:
            return {"input": text}
    return {"input": raw}


def tool_call_name(tool_call: Dict[str, Any]) -> str:
    return tool_call.get("name") or (tool_call.get("function") or {}).get("name") or "tool"


def tool_call_args(tool_call: Dict[str, Any]) -> Dict[str, Any]:
    raw = tool_call.get("args")
    if raw is None:
        raw = (tool_call.get("function") or {}).get("arguments")
    return normalize_tool_args(raw)


def tool_result_has_error(result: str) -> bool:
    try:
        data = json.loads(result)
        return isinstance(data, dict) and bool(data.get("error"))
    except json.JSONDecodeError:
        return False


def _record_tool_health(state: WorkflowState, tool_name: str, semantics: Dict[str, Any], result_str: str, status: str) -> None:
    health = dict(state.toolHealth or {})
    entry = {
        "tool": tool_name,
        "family": semantics.get("family"),
        "origin": semantics.get("origin"),
        "status": status,
    }
    if is_rag_tool(tool_name, semantics):
        retrieval = extract_retrieval_metadata(tool_name, result_str, semantics, {})
        if retrieval:
            entry.update(
                {
                    "hitCount": retrieval.get("hitCount"),
                    "fallbackUsed": retrieval.get("fallbackUsed"),
                    "fallbackReason": retrieval.get("fallbackReason"),
                    "backend": retrieval.get("backend"),
                }
            )
    health[tool_name] = entry
    state.toolHealth = health


async def _emit_generation_event(
    *,
    trace_id: str,
    node_id: str,
    agent_name: str,
    phase: int,
    attempt: int,
    round_index: int,
    input_messages: List[Any],
    ai_msg: AIMessage,
    has_tool_calls: bool,
    is_final: bool,
    agent_span_id: Optional[str],
    started_at: str,
    ended_at: str,
    duration_ms: int,
) -> tuple[str, Optional[str]]:
    msgs_dict = [_msg_to_dict(m) for m in input_messages]
    output_dict = _msg_to_dict(ai_msg)
    content = str(ai_msg.content or "")
    round_role = "final" if is_final else "decision"
    parent_round_id = _parent_round_id(node_id, round_index)

    event_id = make_event_id(trace_id, node_id, attempt, "generation", round_index)
    usage = None
    if ai_msg.response_metadata:
        usage = ai_msg.response_metadata.get("token_usage")
    gen_id = record_generation(
        trace_id,
        f"llm.{agent_name}.round.{round_index}",
        settings.deepseek_model,
        msgs_dict,
        output_dict,
        usage,
        {
            "eventId": event_id,
            "nodeId": node_id,
            "agentName": agent_name,
            "roundIndex": round_index,
            "durationMs": duration_ms,
        },
        parent_observation_id=agent_span_id,
        started_at=started_at,
        ended_at=ended_at,
        duration_ms=duration_ms,
    )

    await emit_event(
        TraceEvent(
            eventId=event_id,
            traceId=trace_id,
            nodeId=node_id,
            agentName=agent_name,
            phase=phase,
            attempt=attempt,
            kind="generation",
            roundIndex=round_index,
            status="SUCCESS",
            startedAt=started_at,
            endedAt=ended_at,
            durationMs=duration_ms,
            modelName=settings.deepseek_model,
            inputMessages=msgs_dict,
            outputMessage=output_dict,
            inputPreview=messages_preview(msgs_dict),
            outputPreview=preview_text(content if is_final else content, 200),
            tokenUsage=usage,
            langfuseTraceId=trace_id,
            langfuseObservationId=gen_id,
            callKind="llm",
            callName=settings.deepseek_model,
            roundRole=round_role,
            parentRoundId=parent_round_id,
            decisionText=content if has_tool_calls else None,
            hasToolCalls=has_tool_calls,
            finalOutput=content if is_final else None,
            observationKind="llm_generation",
        )
    )
    return event_id, gen_id


async def _emit_final_event(
    *,
    trace_id: str,
    node_id: str,
    agent_name: str,
    phase: int,
    attempt: int,
    round_index: int,
    final_output: str,
    parent_event_id: str,
) -> None:
    event_id = make_event_id(trace_id, node_id, attempt, "final", round_index)
    await emit_event(
        TraceEvent(
            eventId=event_id,
            traceId=trace_id,
            nodeId=node_id,
            agentName=agent_name,
            phase=phase,
            attempt=attempt,
            kind="final",
            roundIndex=round_index,
            parentEventId=parent_event_id,
            status="SUCCESS",
            startedAt=now_iso(),
            endedAt=now_iso(),
            outputPreview=preview_text(final_output, 500),
            callKind="final",
            callName=agent_name,
            roundRole="final",
            parentRoundId=_parent_round_id(node_id, round_index),
            finalOutput=final_output,
        )
    )


async def _emit_tool_event(
    *,
    trace_id: str,
    node_id: str,
    agent_name: str,
    phase: int,
    attempt: int,
    round_index: int,
    tool_name: str,
    tool_call_id: str,
    tool_input: Any,
    result_str: str,
    status: str,
    duration_ms: int,
    started_at: str,
    ended_at: str,
    parent_event_id: str,
    agent_span_id: Optional[str],
    deduped_count: int = 0,
) -> str:
    semantics = get_tool_semantics(tool_name)
    observation_kind = observation_kind_for(semantics)
    substeps = build_tool_substeps(
        tool_name,
        normalize_tool_args(tool_input),
        result_str,
        started_at,
        ended_at,
        duration_ms,
        status,
        semantics,
    )
    retrieval = extract_retrieval_metadata(tool_name, result_str, semantics, normalize_tool_args(tool_input))
    lf_meta = {
        "nodeId": node_id,
        "roundIndex": round_index,
        "toolCallId": tool_call_id,
        "parentEventId": parent_event_id,
        "origin": semantics.get("origin"),
        "family": semantics.get("family"),
        "operation": semantics.get("operation"),
        "protocol": semantics.get("protocol"),
        "server": semantics.get("server"),
        "backend": semantics.get("backend"),
        "observationKind": observation_kind,
    }
    if retrieval:
        lf_meta["retrieval"] = retrieval
        lf_meta["originalToolName"] = tool_name
        if isinstance(retrieval.get("selectedChunks"), list):
            lf_meta["selectedChunks"] = retrieval.get("selectedChunks")
        if retrieval.get("usefulnessScore") is not None:
            lf_meta["usefulnessScore"] = retrieval.get("usefulnessScore")
    lf_action_name = tool_name
    if retrieval:
        backend = retrieval.get("backend") or semantics.get("backend") or "rag"
        strategy = retrieval.get("strategy") or semantics.get("operation") or "retrieval"
        lf_action_name = f"rag.{backend}.{strategy}"
    lf_tool_id = action_span(
        trace_id=trace_id,
        tool_name=lf_action_name,
        input_data=tool_input,
        output_data=result_str,
        metadata=lf_meta,
        parent_observation_id=agent_span_id,
        started_at=started_at,
        ended_at=ended_at,
        duration_ms=duration_ms,
    )
    tool_event_id = make_event_id(trace_id, node_id, attempt, "tool", round_index, tool_call_id)
    input_hash = stable_input_hash(tool_input)
    tool_record = ToolCallRecord(
        toolCallId=tool_call_id,
        name=tool_name,
        category=semantics.get("family", "tool"),
        origin=semantics.get("origin", "local"),
        family=semantics.get("family", "tool"),
        protocol=semantics.get("protocol"),
        server=semantics.get("server"),
        operation=semantics.get("operation"),
        arguments=json.dumps(tool_input, ensure_ascii=False, default=str),
        result=preview_text(result_str, 2000),
        startedAt=started_at,
        endedAt=ended_at,
        durationMs=duration_ms,
        status=status,
        inputHash=input_hash,
        dedupedCount=deduped_count,
        substeps=substeps,
        retrieval=retrieval,
    )
    await emit_event(
        TraceEvent(
            eventId=tool_event_id,
            traceId=trace_id,
            nodeId=node_id,
            agentName=agent_name,
            phase=phase,
            attempt=attempt,
            kind="tool",
            roundIndex=round_index,
            parentEventId=parent_event_id,
            status=status,
            startedAt=started_at,
            endedAt=ended_at,
            durationMs=duration_ms,
            toolCalls=[tool_record],
            inputPreview=preview_text(tool_input),
            outputPreview=preview_text(result_str),
            langfuseTraceId=trace_id,
            langfuseObservationId=lf_tool_id,
            callKind=semantics.get("family", "tool"),
            callName=tool_name,
            roundRole="tool_result",
            parentRoundId=_parent_round_id(node_id, round_index),
            observationKind=observation_kind,
            toolOrigin=semantics.get("origin"),
            toolFamily=semantics.get("family"),
            substeps=substeps,
            retrieval=retrieval,
        )
    )
    return tool_event_id


async def emit_tool_event_once(
    *,
    state: WorkflowState,
    node_id: str,
    agent_name: str,
    phase: int,
    round_index: int,
    tool_name: str,
    tool_input: Any,
    result_str: str,
    status: str = "SUCCESS",
    duration_ms: int = 0,
    agent_span_id: Optional[str] = None,
    parent_event_id: str = "",
    started_at: Optional[str] = None,
    ended_at: Optional[str] = None,
) -> str:
    started_at = started_at or now_iso()
    ended_at = ended_at or now_iso()
    tool_call_id = str(uuid.uuid4())
    semantics = get_tool_semantics(tool_name)
    if status == "SUCCESS":
        _record_tool_health(state, tool_name, semantics, result_str, status)
    return await _emit_tool_event(
        trace_id=state.traceId,
        node_id=node_id,
        agent_name=agent_name,
        phase=phase,
        attempt=1,
        round_index=round_index,
        tool_name=tool_name,
        tool_call_id=tool_call_id,
        tool_input=tool_input,
        result_str=result_str,
        status=status,
        duration_ms=duration_ms,
        started_at=started_at,
        ended_at=ended_at,
        parent_event_id=parent_event_id or make_event_id(state.traceId, node_id, 1, "tool", round_index),
        agent_span_id=agent_span_id,
    )


async def run_llm_node(
    node_id: str,
    agent_name: str,
    phase: int,
    system_prompt: str,
    user_content: str,
    state: WorkflowState,
    attempt: int = 1,
    agent_span_id: Optional[str] = None,
    round_index: int = 1,
    is_final_output: bool = True,
    max_tokens: int = 4096,
) -> str:
    trace_id = state.traceId
    agent_started_at = now_iso()
    agent_start_ms = time.time()
    owns_span = agent_span_id is None
    if owns_span:
        agent_span_id = start_agent_span(
            trace_id,
            f"agent.{phase}.{node_id}",
            {
                "nodeId": node_id,
                "agentName": agent_name,
                "workflowRunId": state.workflowRunId,
                "inputLength": len(user_content or ""),
            },
            input_data={
                "systemPrompt": system_prompt,
                "userContent": user_content,
                "userContentLength": len(user_content or ""),
            },
            started_at=agent_started_at,
        )

    input_messages: List[Any] = [
        SystemMessage(content=system_prompt),
        HumanMessage(content=user_content),
    ]
    started_at = now_iso()
    start_ms = time.time()
    ai_msg = await _llm(max_tokens=max_tokens).ainvoke(input_messages)
    ended_at = now_iso()
    duration_ms = int((time.time() - start_ms) * 1000)
    if not isinstance(ai_msg, AIMessage):
        raise TypeError(f"expected AIMessage, got {type(ai_msg).__name__}")

    event_id, _ = await _emit_generation_event(
        trace_id=trace_id,
        node_id=node_id,
        agent_name=agent_name,
        phase=phase,
        attempt=attempt,
        round_index=round_index,
        input_messages=input_messages,
        ai_msg=ai_msg,
        has_tool_calls=not is_final_output,
        is_final=is_final_output,
        agent_span_id=agent_span_id,
        started_at=started_at,
        ended_at=ended_at,
        duration_ms=duration_ms,
    )
    final_output = str(ai_msg.content or "")
    if is_final_output:
        await _emit_final_event(
            trace_id=trace_id,
            node_id=node_id,
            agent_name=agent_name,
            phase=phase,
            attempt=attempt,
            round_index=round_index,
            final_output=final_output,
            parent_event_id=event_id,
        )
    if owns_span:
        end_span(
            agent_span_id,
            output_data=final_output,
            ended_at=now_iso(),
            status="SUCCESS",
            duration_ms=int((time.time() - agent_start_ms) * 1000),
        )
        flush()
    return final_output


async def _invoke_tool(tool: StructuredTool, tool_args: Any) -> str:
    normalized_args = normalize_tool_args(tool_args)
    result = await tool.ainvoke(normalized_args)
    if isinstance(result, str):
        return result
    return json.dumps(result, ensure_ascii=False, default=str)


def _should_skip_rag(tool_name: str, semantics: Dict[str, Any], rag_circuit_open: Set[str]) -> bool:
    if not is_rag_tool(tool_name, semantics):
        return False
    return rag_failure_key(tool_name, semantics) in rag_circuit_open


async def run_agent_node(
    node_id: str,
    agent_name: str,
    phase: int,
    system_prompt: str,
    user_content: str,
    state: WorkflowState,
    attempt: int = 1,
) -> str:
    trace_id = state.traceId
    agent_started_at = now_iso()
    agent_start_ms = time.time()
    agent_span_id = start_agent_span(
        trace_id,
        f"agent.{phase}.{node_id}",
        {"nodeId": node_id, "agentName": agent_name, "workflowRunId": state.workflowRunId},
        input_data={
            "systemPrompt": system_prompt,
            "userContent": user_content,
            "userContentLength": len(user_content or ""),
        },
        started_at=agent_started_at,
    )

    context = {
        "resumeText": state.resumeText,
        "resume_text": state.resumeText,
        "jdMatchResult": state.jdResult or "",
        "jd_match_json": state.jdResult or "",
        "techResult": state.techResult or "",
        "projectResult": state.projectResult or "",
        "riskResult": state.riskResult or "",
    }
    tools = await build_tools_for_agent(agent_name, context)
    tool_map = {tool.name: tool for tool in tools}
    llm_with_tools = _llm().bind_tools(tools) if tools else _llm()

    input_messages: List[Any] = [
        SystemMessage(content=system_prompt),
        HumanMessage(content=user_content),
    ]
    round_index = 0
    final_output = ""
    last_tool_name = ""
    seen_tool_signatures: Set[str] = set()
    rag_failure_counts: Dict[str, int] = {}
    rag_circuit_open: Set[str] = set()
    tool_call_count = 0
    tool_budget = TOOL_BUDGET_BY_AGENT.get(agent_name, 6)

    try:
        for _ in range(MAX_AGENT_ROUNDS):
            round_index += 1
            llm_for_round = _llm() if tools and round_index == MAX_AGENT_ROUNDS else llm_with_tools
            gen_started_at = now_iso()
            gen_start_ms = time.time()
            ai_msg = await llm_for_round.ainvoke(input_messages)
            gen_duration_ms = int((time.time() - gen_start_ms) * 1000)
            gen_ended_at = now_iso()
            if not isinstance(ai_msg, AIMessage):
                raise TypeError(f"expected AIMessage, got {type(ai_msg).__name__}")

            tool_calls = ai_msg.tool_calls or []
            has_tool_calls = bool(tool_calls)
            is_final = not has_tool_calls

            gen_event_id, gen_observation_id = await _emit_generation_event(
                trace_id=trace_id,
                node_id=node_id,
                agent_name=agent_name,
                phase=phase,
                attempt=attempt,
                round_index=round_index,
                input_messages=input_messages,
                ai_msg=ai_msg,
                has_tool_calls=has_tool_calls,
                is_final=is_final,
                agent_span_id=agent_span_id,
                started_at=gen_started_at,
                ended_at=gen_ended_at,
                duration_ms=gen_duration_ms,
            )
            input_messages.append(ai_msg)

            if is_final:
                final_output = str(ai_msg.content or "")
                if is_malformed_final_output(final_output) and round_index < MAX_AGENT_ROUNDS:
                    input_messages.append(
                        HumanMessage(
                            content="上次输出无效。请直接输出合法 JSON 或 Markdown 最终结论，禁止 tool_calls/DSML 格式。"
                        )
                    )
                    continue
                await _emit_final_event(
                    trace_id=trace_id,
                    node_id=node_id,
                    agent_name=agent_name,
                    phase=phase,
                    attempt=attempt,
                    round_index=round_index,
                    final_output=final_output,
                    parent_event_id=gen_event_id,
                )
                break

            for tool_call in tool_calls:
                tool_name = tool_call_name(tool_call)
                tool_args = tool_call_args(tool_call)
                tool_call_id = tool_call.get("id") or str(uuid.uuid4())
                last_tool_name = tool_name
                semantics = get_tool_semantics(tool_name)
                signature = tool_signature(tool_name, tool_args)
                deduped_count = 0

                if signature in seen_tool_signatures:
                    deduped_count = 1
                    skip_result = json.dumps(
                        {"skipped": True, "reason": "duplicate_tool_call", "tool": tool_name},
                        ensure_ascii=False,
                    )
                    started_at = now_iso()
                    ended_at = started_at
                    await _emit_tool_event(
                        trace_id=trace_id,
                        node_id=node_id,
                        agent_name=agent_name,
                        phase=phase,
                        attempt=attempt,
                        round_index=round_index,
                        tool_name=tool_name,
                        tool_call_id=tool_call_id,
                        tool_input=tool_args,
                        result_str=skip_result,
                        status="SKIPPED",
                        duration_ms=0,
                        started_at=started_at,
                        ended_at=ended_at,
                        parent_event_id=gen_event_id,
                        agent_span_id=agent_span_id,
                        deduped_count=deduped_count,
                    )
                    input_messages.append(
                        ToolMessage(content=skip_result, tool_call_id=tool_call_id, name=tool_name)
                    )
                    continue

                if tool_call_count >= tool_budget:
                    skip_result = json.dumps(
                        {"skipped": True, "reason": "tool_budget_exceeded", "tool": tool_name},
                        ensure_ascii=False,
                    )
                    started_at = now_iso()
                    ended_at = started_at
                    await _emit_tool_event(
                        trace_id=trace_id,
                        node_id=node_id,
                        agent_name=agent_name,
                        phase=phase,
                        attempt=attempt,
                        round_index=round_index,
                        tool_name=tool_name,
                        tool_call_id=tool_call_id,
                        tool_input=tool_args,
                        result_str=skip_result,
                        status="SKIPPED",
                        duration_ms=0,
                        started_at=started_at,
                        ended_at=ended_at,
                        parent_event_id=gen_event_id,
                        agent_span_id=agent_span_id,
                    )
                    input_messages.append(
                        ToolMessage(content=skip_result, tool_call_id=tool_call_id, name=tool_name)
                    )
                    continue

                if _should_skip_rag(tool_name, semantics, rag_circuit_open):
                    skip_result = json.dumps(
                        {
                            "skipped": True,
                            "reason": "rag_circuit_open",
                            "tool": tool_name,
                            "message": "RAG 后端连续失败，已熔断，请基于 resume_text 输出",
                        },
                        ensure_ascii=False,
                    )
                    started_at = now_iso()
                    ended_at = started_at
                    await _emit_tool_event(
                        trace_id=trace_id,
                        node_id=node_id,
                        agent_name=agent_name,
                        phase=phase,
                        attempt=attempt,
                        round_index=round_index,
                        tool_name=tool_name,
                        tool_call_id=tool_call_id,
                        tool_input=tool_args,
                        result_str=skip_result,
                        status="SKIPPED",
                        duration_ms=0,
                        started_at=started_at,
                        ended_at=ended_at,
                        parent_event_id=gen_event_id,
                        agent_span_id=agent_span_id,
                    )
                    input_messages.append(
                        ToolMessage(content=skip_result, tool_call_id=tool_call_id, name=tool_name)
                    )
                    continue

                seen_tool_signatures.add(signature)
                tool_call_count += 1
                tool = tool_map.get(tool_name)
                started_at = now_iso()
                start_ms = time.time()
                if tool is None:
                    result_str = json.dumps({"error": f"tool not found: {tool_name}", "tool": tool_name}, ensure_ascii=False)
                    status = "FAILED"
                    logger.warning("tool not found: %s for agent %s", tool_name, agent_name)
                else:
                    try:
                        result_str = await _invoke_tool(tool, tool_args)
                        status = "FAILED" if tool_result_has_error(result_str) else "SUCCESS"
                    except Exception as exc:
                        result_str = json.dumps({"error": str(exc), "tool": tool_name}, ensure_ascii=False)
                        status = "FAILED"
                        logger.warning("tool %s failed: %s", tool_name, exc)

                duration_ms = int((time.time() - start_ms) * 1000)
                ended_at = now_iso()
                _record_tool_health(state, tool_name, semantics, result_str, status)

                if is_rag_tool(tool_name, semantics) and status == "FAILED":
                    key = rag_failure_key(tool_name, semantics)
                    rag_failure_counts[key] = rag_failure_counts.get(key, 0) + 1
                    if rag_failure_counts[key] >= RAG_FAILURE_THRESHOLD:
                        rag_circuit_open.add(key)

                await _emit_tool_event(
                    trace_id=trace_id,
                    node_id=node_id,
                    agent_name=agent_name,
                    phase=phase,
                    attempt=attempt,
                    round_index=round_index,
                    tool_name=tool_name,
                    tool_call_id=tool_call_id,
                    tool_input=tool_args,
                    result_str=result_str,
                    status=status,
                    duration_ms=duration_ms,
                    started_at=started_at,
                    ended_at=ended_at,
                    parent_event_id=gen_event_id,
                    agent_span_id=agent_span_id,
                )
                input_messages.append(
                    ToolMessage(content=result_str, tool_call_id=tool_call_id, name=tool_name)
                )
                if status == "FAILED" and not is_rag_tool(tool_name, semantics):
                    raise RuntimeError(f"tool {tool_name} failed: {result_str}")
        else:
            raise RuntimeError(
                f"workflow node {node_id} exceeded max rounds after tool {last_tool_name or 'unknown'}"
            )
    except Exception as exc:
        raise RuntimeError(
            f"workflow node {node_id} failed: {type(exc).__name__}: {exc}"
        ) from exc
    finally:
        agent_duration_ms = int((time.time() - agent_start_ms) * 1000)
        end_span(
            agent_span_id,
            output_data=final_output,
            ended_at=now_iso(),
            status="SUCCESS",
            duration_ms=agent_duration_ms,
        )
        flush()

    return final_output
