from __future__ import annotations

"""ConversationalAgent responder: CopilotAnswer only — no StructuredReport."""

import json
import logging
import re
from typing import Any, Dict, List, Optional

from app.conversation.models import (
    ConversationReplyRequest,
    CopilotAction,
    CopilotAnswer,
    SourceRef,
)
from app.conversation.tools import collect_session_citations, evidence_hints

logger = logging.getLogger(__name__)

_ARITH = re.compile(r"^\s*(\d+)\s*([+\-*/x×])\s*(\d+)\s*$")
_SAFE_COPILOT_ACTIONS = {"OPEN_REPORT", "OPEN_TRACE", "VIEW_EVIDENCE"}
_SNAPSHOT_TEXT_LIMITS = {
    "activeGoal": 600,
    "summary": 1200,
    "jobDescription": 1600,
    "jdText": 1600,
    "effectiveJd": 1600,
    "resumeText": 1800,
    "resumeExcerpt": 1800,
    "selectedText": 900,
}

_SYSTEM = """你是 ResumAI 招聘决策 Copilot。只输出 JSON（不要 markdown）：
{"answer":"针对当前问题的短答","citations":[],"actions":[],"suggestions":["可选下一步"]}
规则：
1. 不要复述完整评估报告；完整报告只在决策报告页。
2. 不要编造候选人事实、分数或风险。
3. 证据不足时明确说明缺什么，而不是猜。
4. 算术/闲聊直接短答。
5. citations 仅在有真实依据时填写。
6. 即使还没有完整评估报告，也必须直接回答用户当前问题；只能使用消息和给定的
   JD、简历片段、会话摘要，候选人结论不足时明确标成“待核验”。
7. 回答控制在 300 个中文字符左右；普通问答不得建议或触发完整评估运行。
"""


async def generate_copilot_answer(request: ConversationReplyRequest) -> CopilotAnswer:
    content = (request.content or "").strip()
    snapshot = dict(request.contextSnapshot or {})

    local = _local_answer(content, request, snapshot)
    if local is not None:
        return local

    model_answer = await _model_answer(content, request, snapshot)
    if model_answer is not None:
        return model_answer

    return CopilotAnswer(
        turnId=request.turnId,
        answer=_bounded_fallback_answer(content, snapshot),
        suggestions=["补充具体证据", "查看当前进度", "说明想核验的维度"],
    )


def _local_answer(
    content: str,
    request: ConversationReplyRequest,
    snapshot: Dict[str, Any],
) -> Optional[CopilotAnswer]:
    arith = _ARITH.match(content)
    if arith:
        return CopilotAnswer(
            turnId=request.turnId,
            answer=_eval_arith(arith),
            suggestions=["继续问评估依据", "补充候选人事实", "改投岗位后重新评估"],
        )

    if request.allowTools or (request.disposition or "").upper() == "BACKGROUND_QUERY":
        citations = [SourceRef(**c) for c in collect_session_citations(snapshot)]
        return CopilotAnswer(
            turnId=request.turnId,
            answer=evidence_hints(content, snapshot),
            citations=citations,
            actions=[CopilotAction(type="OPEN_REPORT", label="打开决策报告")],
            suggestions=["核对 must-have 缺口", "查看风险证据", "出面试追问"],
        )

    lowered = content.lower()
    is_overall_query = any(k in lowered for k in ("结论", "分数", "推荐", "进度", "到哪"))
    if not is_overall_query and "怎么样" in lowered:
        is_overall_query = len(content) <= 12 or re.search(
            r"(候选人|这个人|这份简历|这个候选|他|她).{0,4}怎么样$", content) is not None
    if is_overall_query:
        report = snapshot.get("structuredReport") or snapshot.get("report") or {}
        if isinstance(report, dict) and report.get("recommendation"):
            rec = report["recommendation"]
            dims = report.get("dimensions") or []
            risks = report.get("risks") or []
            strengths = report.get("strengths") or []
            rec_label = {"HIRE": "推荐录用", "INTERVIEW_RECOMMEND": "建议面试",
                         "NEED_MANUAL_REVIEW": "需人工复审",
                         "NOT_RECOMMEND": "不推荐"}.get(rec, rec)
            dim_text = "、".join(
                f"{d.get('name', '?')}{d.get('score', '?')}分" for d in dims[:4]
                if isinstance(d, dict))
            strength_text = "；".join(s[:30] for s in strengths[:2]) if strengths else "暂无"
            risk_text = "；".join(
                r.get("claim", "")[:30] for r in risks[:2]
                if isinstance(r, dict)) if risks else "暂无明显风险"
            answer = (
                f"综合评估结论：**{rec_label}**。\n"
                f"维度评分：{dim_text or '评估中'}。\n"
                f"核心优势：{strength_text}。\n"
                f"主要风险：{risk_text}。\n"
                f"详细证据和面试追问见决策报告页。")
            return CopilotAnswer(
                turnId=request.turnId,
                answer=answer,
                citations=[SourceRef(sourceType="SESSION", sourceId="structured",
                                     quote=f"recommendation={rec}")],
                actions=[CopilotAction(type="OPEN_REPORT", label="查看完整报告")],
                suggestions=["为什么给这个分数？", "主要风险详情", "面试该问什么？"],
            )
        summary = str(snapshot.get("summary") or "").strip()
        if summary:
            first_sentence = next(
                (part.strip() for part in re.split(r"(?<=[。！？!?])", summary)
                 if part.strip()),
                summary[:140],
            )[:160]
            return CopilotAnswer(
                turnId=request.turnId,
                answer=f"当前评估摘要：{first_sentence}\n详细证据和面试追问见决策报告页。",
                citations=[SourceRef(
                    sourceType="SESSION", sourceId="summary",
                    quote=first_sentence[:120])],
                actions=[CopilotAction(type="OPEN_REPORT", label="查看完整报告")],
                suggestions=["为什么给这个分数？", "主要风险详情", "面试该问什么？"],
            )
        # No report is not permission to start a run. Let the bounded
        # conversational model answer from the available resume/JD/session
        # context; its candidate claims remain evidence-gated by _SYSTEM.
        return None

    if any(k in lowered for k in ("你好", "谢谢", "在吗")):
        return CopilotAnswer(
            turnId=request.turnId,
            answer="你好，我是招聘决策 Copilot。可以直接问评估依据、风险或面试追问；普通闲聊不会触发评估任务。",
            suggestions=["这份简历怎么样？", "主要风险是什么？"],
        )
    return None


def _eval_arith(match: re.Match[str]) -> str:
    a = float(match.group(1))
    b = float(match.group(3))
    op = match.group(2)
    try:
        if op == "+":
            result = a + b
        elif op == "-":
            result = a - b
        elif op in {"*", "x", "×"}:
            result = a * b
        elif op == "/":
            if b == 0:
                return "无法计算该表达式。"
            result = a / b
        else:
            return "无法计算该表达式。"
        if result == int(result):
            return str(int(result))
        return str(result)
    except Exception:
        return "无法计算该表达式。"


async def _model_answer(
    content: str,
    request: ConversationReplyRequest,
    snapshot: Dict[str, Any],
) -> Optional[CopilotAnswer]:
    try:
        import httpx

        from app.config import normalized_deepseek_base_url, settings

        if not settings.deepseek_api_key:
            return None

        # A conversational answer must not depend on a completed ReportAgent
        # result. Before the report exists, the current message plus bounded
        # resume/JD/session context is still enough for a useful short answer.
        # Candidate-specific conclusions remain evidence-gated by _SYSTEM.
        compact = _bounded_context_snapshot(snapshot)
        user_payload = json.dumps(
            {
                "content": content,
                "disposition": request.disposition,
                "contextRefs": [r.model_dump() for r in request.contextRefs],
                "contextSnapshot": compact,
            },
            ensure_ascii=False,
            default=str,
        )
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"{normalized_deepseek_base_url()}/chat/completions",
                headers={
                    "Authorization": f"Bearer {settings.deepseek_api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": settings.deepseek_model,
                    "messages": [
                        {"role": "system", "content": _SYSTEM},
                        {"role": "user", "content": user_payload},
                    ],
                    "temperature": 0.2,
                    "max_tokens": 500,
                    "response_format": {"type": "json_object"},
                    "stream": False,
                },
            )
            response.raise_for_status()
            data = response.json()
        raw = str(data["choices"][0]["message"]["content"] or "")
        start, end = raw.find("{"), raw.rfind("}")
        if start < 0 or end <= start:
            return None
        payload = json.loads(raw[start : end + 1])
        answer = str(payload.get("answer") or "").strip()
        if not answer:
            return None
        citations: List[SourceRef] = []
        for item in payload.get("citations") or []:
            if isinstance(item, dict):
                try:
                    citations.append(SourceRef(**item))
                except Exception:
                    continue
        actions: List[CopilotAction] = []
        for item in payload.get("actions") or []:
            action_type = str(item.get("type") or "").strip().upper() \
                if isinstance(item, dict) else ""
            if (isinstance(item, dict)
                    and action_type in _SAFE_COPILOT_ACTIONS
                    and item.get("label")):
                actions.append(CopilotAction(
                    type=action_type,
                    label=str(item["label"]),
                    payload=dict(item.get("payload") or {}),
                ))
        suggestions = [
            str(s) for s in (payload.get("suggestions") or []) if str(s).strip()
        ]
        return CopilotAnswer(
            turnId=request.turnId,
            answer=answer,
            citations=citations,
            actions=actions,
            suggestions=suggestions,
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("copilot model reply skipped: %s", exc)
        return None


def _bounded_context_snapshot(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    """Keep the short-answer prompt useful without leaking an unbounded session.

    Java currently sends flags, goal and summary; richer callers may also send
    JD/resume excerpts. All text is clipped here before it reaches the model.
    A completed report is reduced to decision-bearing fields instead of copying
    the full report into an ordinary chat turn.
    """
    compact: Dict[str, Any] = {}
    for key in ("jobCategory", "revision", "hasResume", "hasJobDescription"):
        value = snapshot.get(key)
        if value not in (None, "", [], {}):
            compact[key] = value
    for key, limit in _SNAPSHOT_TEXT_LIMITS.items():
        value = snapshot.get(key)
        if value not in (None, "", [], {}):
            compact[key] = _clip_text(value, limit)

    report = snapshot.get("structuredReport") or snapshot.get("report")
    if isinstance(report, dict) and report:
        compact_report: Dict[str, Any] = {}
        for key in ("recommendation", "overallScore", "dataQuality"):
            if report.get(key) not in (None, "", [], {}):
                compact_report[key] = report.get(key)
        for key, limit in (
            ("dimensions", 6),
            ("strengths", 3),
            ("risks", 3),
            ("interviewProbes", 3),
        ):
            value = report.get(key)
            if isinstance(value, list) and value:
                compact_report[key] = value[:limit]
        if compact_report:
            compact["structuredReport"] = compact_report
    return compact


def _clip_text(value: Any, limit: int) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text if len(text) <= limit else text[:limit] + "…"


def _bounded_fallback_answer(content: str, snapshot: Dict[str, Any]) -> str:
    """Useful deterministic fallback when the short-answer model is unavailable.

    This path deliberately answers common system/interview concepts rather than
    returning a routing receipt. Candidate-specific claims stay conservative.
    """
    lowered = (content or "").lower()
    if "rag" in lowered or "检索" in lowered or "命中率" in lowered:
        return (
            "RAG 命中率低表示当前查询没有召回足够相关、可引用的证据。"
            "应先检查查询改写、分块、召回与重排，再回退到简历/JD 原文；"
            "在证据补齐前降低结论置信度，并明确标注“待核验”，不能让模型猜。"
        )
    if "checkpoint" in lowered or "断点" in lowered:
        return (
            "checkpoint 是安全边界保存的执行状态。暂停后应保留已完成 Agent、"
            "工具结果和共享产物；继续时从该快照恢复，而不是新建一次运行从头重跑。"
        )
    if "revision" in lowered or "版本" in lowered:
        return (
            "revision 是不可变的评估意图版本。JD、候选人事实或评估重点变化时"
            "创建新版本，废弃旧结论，只复用未受影响产物并重跑依赖链。"
        )
    if "mcp" in lowered:
        return (
            "MCP 把真实工具的描述和参数 schema 提供给模型，由模型按当前任务"
            "决定是否调用并生成参数；工具失败必须显式降级，不能伪造成功结果。"
        )

    context = _bounded_context_snapshot(snapshot)
    basis = context.get("summary") or context.get("activeGoal") \
        or context.get("selectedText")
    if basis:
        return (
            f"基于当前可用上下文：{_clip_text(basis, 180)} "
            "这还不是完整评估结论；涉及候选人能力或风险的判断需要对应的"
            "简历/JD证据，缺失部分应标为待核验。"
        )
    has_resume = bool(context.get("hasResume") or context.get("resumeText")
                      or context.get("resumeExcerpt"))
    has_jd = bool(context.get("hasJobDescription") or context.get("jobDescription")
                  or context.get("jdText") or context.get("effectiveJd"))
    available = "简历和JD" if has_resume and has_jd \
        else "简历" if has_resume else "JD" if has_jd else "当前消息"
    return (
        f"我可以先按{available}回答这个问题，但目前没有足够证据形成候选人结论。"
        "请指出要核验的具体技能、项目或风险；我会区分已知事实与待核验项，不会猜测。"
    )
