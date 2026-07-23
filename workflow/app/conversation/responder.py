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

_SYSTEM = """你是 ResumAI 招聘决策 Copilot。只输出 JSON（不要 markdown）：
{"answer":"针对当前问题的短答","citations":[],"actions":[],"suggestions":["可选下一步"]}
规则：
1. 不要复述完整评估报告；完整报告只在决策报告页。
2. 不要编造候选人事实、分数或风险。
3. 证据不足时明确说明缺什么，而不是猜。
4. 算术/闲聊直接短答。
5. citations 仅在有真实依据时填写。
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
        answer=(
            "已收到。这是对话回复，不会启动完整评估流水线。"
            "若要重新评估，请明确说明岗位或候选人事实变更；停止任务请说“停止”。"
        ),
        suggestions=["为什么给这个分数？", "主要风险是什么？", "改为目标岗位后重新评估"],
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
    if any(k in lowered for k in ("结论", "分数", "推荐", "怎么样", "进度", "到哪")):
        summary = str(snapshot.get("summary") or "").strip()
        if summary:
            compact = re.sub(r"\s+", " ", summary)
            # Collapse pathological repeated summaries into one sentence.
            first_sentence = re.split(r"[。！？.!?]", compact, maxsplit=1)[0].strip()
            if first_sentence:
                compact = first_sentence + "。"
            if len(compact) > 160:
                compact = compact[:160] + "…"
            return CopilotAnswer(
                turnId=request.turnId,
                answer=f"基于当前会话摘要：{compact} 完整决策报告请在报告页查看，聊天不会复述整份报告。",
                citations=[SourceRef(sourceType="SESSION", sourceId="summary", quote=compact[:200])],
                actions=[CopilotAction(type="OPEN_REPORT", label="打开决策报告")],
                suggestions=["为什么给这个分数？", "主要风险是什么？"],
            )
        return CopilotAnswer(
            turnId=request.turnId,
            answer="当前还没有可用的评估摘要。发起完整评估后，可在决策报告页查看证据化结论。",
            actions=[CopilotAction(type="START_EVALUATION", label="发起完整评估")],
            suggestions=["完整评估这份简历", "先核对 JD 缺口"],
        )

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

        compact = {
            key: snapshot.get(key)
            for key in ("activeGoal", "summary", "jobCategory", "revision",
                        "hasResume", "hasJobDescription")
            if snapshot.get(key) not in (None, "", [], {})
        }
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
                    "max_tokens": 600,
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
            if isinstance(item, dict) and item.get("type") and item.get("label"):
                actions.append(CopilotAction(
                    type=str(item["type"]),
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
