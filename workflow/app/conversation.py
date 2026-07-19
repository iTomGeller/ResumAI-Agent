from __future__ import annotations

"""Deterministic, rule-first routing for turns sent during an evaluation.

This module intentionally has no FastAPI, database, or model dependency.  The
Java service remains the source of truth for revisions; this router only
describes what a turn means and which evaluation nodes would be affected.
"""

import json
import re
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Dict, List, Mapping, Optional, Sequence


class TurnIntent(str, Enum):
    SIDE_QUESTION = "SIDE_QUESTION"
    CONTEXT_ADD = "CONTEXT_ADD"
    GOAL_CHANGE = "GOAL_CHANGE"
    EVALUATION_FOCUS = "EVALUATION_FOCUS"
    CLARIFY = "CLARIFY"
    PAUSE = "PAUSE"
    RESUME = "RESUME"
    CANCEL = "CANCEL"


ALL_EVALUATION_NODES: List[str] = [
    "intent",
    "resume_parse",
    "jd_match",
    "knowledge_context",
    "tech_eval",
    "project_eval",
    "risk_eval",
    "evidence_fusion",
    "report",
]

GOAL_CHANGE_NODES: List[str] = [
    "intent",
    "jd_match",
    "knowledge_context",
    "tech_eval",
    "project_eval",
    "risk_eval",
    "evidence_fusion",
    "report",
]

FOCUS_CHANGE_NODES: List[str] = [
    "knowledge_context",
    "tech_eval",
    "project_eval",
    "risk_eval",
    "evidence_fusion",
    "report",
]


@dataclass(frozen=True)
class TurnDecision:
    intent: TurnIntent
    confidence: float
    affects_evaluation: bool
    answer_then_resume: bool
    affected_nodes: List[str] = field(default_factory=list)
    assistant_reply: str = ""
    control_action: Optional[str] = None
    evaluation_patch: Dict[str, Any] = field(default_factory=dict)
    requires_confirmation: bool = False
    reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["intent"] = self.intent.value
        return data


_RESUME_PATTERNS: Sequence[str] = (
    r"(?:继续|恢复|接着)(?:评估|分析|跑|执行)?",
    r"(?:不要|别)(?:停|暂停|中断)",
    r"\bresume\b",
    r"\bcontinue\b",
)
_CANCEL_PATTERNS: Sequence[str] = (
    r"(?:取消|终止)(?:这次|当前|本次)?(?:评估|分析|任务)?",
    r"(?:别跑了|不用跑了|不要评估了|停止评估|结束任务)",
    r"\bcancel\b",
    r"\bstop(?:\s+the)?\s+(?:run|evaluation|task)\b",
)
_PAUSE_PATTERNS: Sequence[str] = (
    r"(?:先|请)?暂停(?:一下)?(?:评估|分析|任务)?",
    r"(?:先)?停一下(?:评估|分析|任务)?",
    r"\bpause\b",
)
_NEGATED_CANCEL_PATTERNS: Sequence[str] = (
    r"(?:不要|别|不用|先不)取消",
    r"(?:don't|do not)\s+cancel",
)
_NEGATED_PAUSE_PATTERNS: Sequence[str] = (
    r"(?:不要|别|不用|先不)(?:暂停|停)",
    r"(?:don't|do not)\s+pause",
)

_GOAL_CHANGE_PATTERNS: Sequence[str] = (
    r"(?:改成|换成|切到|改按|换个)(?:.+?)(?:岗|岗位|职位|方向)",
    r"(?:目标|应聘|评估)(?:岗位|职位|方向).{0,6}(?:改|换|变)",
    r"(?:按|作为).{1,20}(?:岗|岗位|职位)(?:重新)?(?:评估|看|分析)",
    r"(?:不看|别看).{0,16}(?:改看|换看|看).{1,16}(?:岗|岗位|职位|方向)",
)
_OUTPUT_SIDE_PATTERNS: Sequence[str] = (
    r"(?:只|直接)(?:告诉|给我|说)(?:我)?(?:结论|分数|摘要)",
    r"(?:不要|不用|别)(?:看|展示)(?:\s|这个)*(?:trace|链路|过程|详情|原文)",
    r"(?:一句话|简短|用表格|列表)(?:说明|回答|总结|告诉)",
    r"(?:改写|润色|翻译)(?:这段|报告|结论|项目|简历)",
)
_FOCUS_PATTERNS: Sequence[str] = (
    r"(?:重点|优先|主要|更)(?:看|关注|考察|看重)",
    r"(?:忽略|弱化|不要看|不用看|排除).{0,20}(?:学历|学校|教育|年龄|技术|项目|风险|经验|证书|指标|要求|维度)",
    r"(?:提高|降低|调整).{0,12}(?:权重|标准|门槛)",
    r"(?:严格|宽松)(?:一点|些)?",
    r"只看(?!结论|报告|分数|摘要).{1,30}",
)
_CONTEXT_PREFIX_PATTERNS: Sequence[str] = (
    r"^(?:补充|另外|还有|更正|纠正|说明|备注|事实上|实际(?:上)?)[：:\s,，]",
    r"^(?:补充|另外|还有|更正|纠正|说明|备注)(?:一下|一点|一条)?",
)
_EVALUATION_FACT_TERMS: Sequence[str] = (
    "经验",
    "项目",
    "技术",
    "技能",
    "学历",
    "年限",
    "实习",
    "公司",
    "经历",
    "证书",
    "职责",
    "绩效",
    "贡献",
    "候选人",
    "简历",
    "薪资",
    "职级",
)
_ADMIN_NOTE_TERMS: Sequence[str] = (
    "约面",
    "面试时间",
    "提醒",
    "联系",
    "电话",
    "邮箱",
    "微信",
    "周一",
    "周二",
    "周三",
    "周四",
    "周五",
    "周六",
    "周日",
    "明天",
    "后天",
    "方便时间",
)
_QUESTION_TERMS: Sequence[str] = (
    "什么",
    "为什么",
    "怎么",
    "如何",
    "是否",
    "能否",
    "可以吗",
    "进度",
    "到哪",
    "结果",
    "解释",
    "告诉我",
    "怎么样",
    "多少",
    "改写",
    "润色",
    "岗位比较",
    "对比岗位",
    "模拟面试",
    "出几道题",
)


def _matches_any(text: str, patterns: Sequence[str]) -> bool:
    return any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in patterns)


def _contains_any(text: str, terms: Sequence[str]) -> bool:
    lowered = text.lower()
    return any(term.lower() in lowered for term in terms)


def _progress_reply(context: Mapping[str, Any]) -> str:
    status = str(context.get("runStatus") or context.get("status") or "RUNNING").upper()
    current_node = str(context.get("currentNode") or "").strip()
    completed = context.get("completedNodes") or []
    if not isinstance(completed, list):
        completed = []
    if status in {"SUCCESS", "COMPLETED"}:
        return "本轮评估已经完成，你可以继续追问报告里的结论或证据。"
    if status == "PAUSED":
        suffix = f"，已完成 {len(completed)} 个节点" if completed else ""
        return f"本轮评估目前已暂停{suffix}；你可以继续提问，发送“继续评估”即可恢复。"
    if status in {"CANCELLED", "SUPERSEDED"}:
        return f"本轮评估状态为 {status}，不会再更新当前报告。"
    if current_node:
        return f"当前正在执行 {current_node}；你的问题不会打断评估，任务会继续运行。"
    if completed:
        return f"评估仍在运行，已经完成 {len(completed)} 个节点；你的问题不会打断任务。"
    return "评估仍在运行；你的问题不会打断当前任务。"


def _side_question_reply(message: str, context: Mapping[str, Any]) -> str:
    if _contains_any(message, ("进度", "到哪", "跑完", "完成了吗", "状态")):
        return _progress_reply(context)

    if _contains_any(message, ("改写", "润色", "优化表述", "改简历")):
        source = str(
            context.get("selectedText")
            or context.get("resumeExcerpt")
            or context.get("resumeText")
            or ""
        ).strip()
        if not source:
            return "可以帮你改写，但需要原句、目标岗位，以及可公开的量化结果；这属于独立请求，不会打断当前评估。"
        compact = re.sub(r"\s+", " ", source)[:220]
        return (
            f"已拿到待改写内容：“{compact}”。建议按“负责的对象 + 采取的技术动作 + 可验证结果”重写；"
            "若要产出不虚构数字的最终版本，还需要你提供该项目的真实指标。当前评估继续运行。"
        )

    if _contains_any(message, ("岗位比较", "比较岗位", "对比岗位", "哪个岗位", "更适合哪个")):
        matches = context.get("topJdMatches") or context.get("jobComparisons") or []
        if isinstance(matches, list) and matches:
            parts: List[str] = []
            for item in matches[:3]:
                if not isinstance(item, Mapping):
                    continue
                title = str(item.get("title") or item.get("name") or "未命名岗位")
                score = item.get("score")
                gaps = item.get("gaps") or []
                score_text = f"{round(float(score) * 100)}%" if isinstance(score, (int, float)) else "未给分"
                gap_text = f"，主要缺口：{'、'.join(map(str, gaps[:2]))}" if isinstance(gaps, list) and gaps else ""
                parts.append(f"{title}（{score_text}{gap_text}）")
            if parts:
                return "根据已提供的岗位匹配数据：" + "；".join(parts) + "。这次比较不会改变当前评估目标。"
        return "可以比较岗位，但需要至少两份 JD，或当前任务的 topJdMatches；在收到这些信息前不会猜测，也不会中断评估。"

    if _contains_any(message, ("模拟面试", "面试我", "开始面试", "出几道题", "追问题")):
        questions = context.get("interviewQuestions") or []
        if isinstance(questions, list) and questions:
            rendered = " ".join(f"{idx}. {question}" for idx, question in enumerate(questions[:4], 1))
            return f"可以，先基于当前已生成的追问开始：{rendered} 当前评估会继续运行。"
        risks = context.get("risks") or []
        if isinstance(risks, list) and risks:
            rendered = " ".join(
                f"{idx}. 请结合具体项目说明：{risk}" for idx, risk in enumerate(risks[:3], 1)
            )
            return f"可以，先按当前风险证据追问：{rendered} 当前评估会继续运行。"
        return "可以模拟面试，但至少需要目标 JD 和简历摘要，或已经生成的 interviewQuestions；当前评估不会因此暂停。"

    definitions = {
        "rag": "RAG 是先检索岗位、知识库或历史证据，再让模型基于检索结果分析；命中低表示可用外部证据较弱，应更多依赖简历原文并降低结论确定性。",
        "checkpoint": "checkpoint 是 LangGraph 在安全节点边界保存的执行状态，用来暂停后从已完成步骤继续，而不是从头重跑。",
        "revision": "revision 是一次不可变的评估意图版本；只有补充内容或目标确实影响结论时才会创建新版本。",
        "mcp": "MCP 是 Agent 调用外部工具或数据服务的标准协议；工具结果必须作为证据返回，不能用提示词伪造。",
        "jd匹配": "JD 匹配度表示简历证据与当前岗位要求的贴合程度，不等同于候选人的整体能力分。",
    }
    lowered = message.lower()
    for term, explanation in definitions.items():
        if term in lowered:
            return explanation + " 这是解释性问题，不会修改或中断当前评估。"

    summary = str(context.get("summary") or "").strip()
    if summary:
        compact = re.sub(r"\s+", " ", summary)
        if len(compact) > 280:
            compact = compact[:280] + "..."
        return f"基于当前已经生成的证据：{compact} 当前评估不会因此中断。"
    return "我会把这当作独立问题处理，不修改评估目标，也不中断正在运行的任务；当前证据不足的部分会明确标注，而不会猜测。"


def resolve_turn(
    message: str,
    *,
    run_status: str = "RUNNING",
    revision: int = 1,
    context: Optional[Mapping[str, Any]] = None,
) -> TurnDecision:
    """Resolve a conversational turn without mutating a workflow.

    ``answer_then_resume`` means the conversational response must not block the
    active evaluation.  The Java caller decides whether to create a revision or
    execute a control command based on the returned contract.
    """

    text = re.sub(r"\s+", " ", (message or "").strip())
    merged_context: Dict[str, Any] = dict(context or {})
    for nested_key in ("currentTask", "task", "evaluation"):
        nested = merged_context.get(nested_key)
        if isinstance(nested, Mapping):
            for key, value in nested.items():
                merged_context.setdefault(str(key), value)
    merged_context.setdefault("runStatus", run_status)
    merged_context.setdefault("revision", revision)

    if not text:
        return TurnDecision(
            intent=TurnIntent.CLARIFY,
            confidence=1.0,
            affects_evaluation=False,
            answer_then_resume=True,
            assistant_reply="请告诉我你想提问、补充候选人信息，还是暂停/继续/取消当前评估。",
            requires_confirmation=True,
            reason="empty_turn",
        )

    # Control commands are deterministic and deliberately precede all semantic
    # routing.  Negative forms such as “别停” are matched as RESUME first.
    cancel_negated = _matches_any(text, _NEGATED_CANCEL_PATTERNS)
    pause_negated = _matches_any(text, _NEGATED_PAUSE_PATTERNS)
    explicit_resume = _contains_any(text, ("继续", "恢复", "接着", "resume", "continue"))
    compound_side_question = (
        "?" in text or "？" in text or _contains_any(text, _QUESTION_TERMS)
    )
    if (
        _matches_any(text, _RESUME_PATTERNS)
        and (not pause_negated or explicit_resume)
        and not compound_side_question
    ):
        return TurnDecision(
            intent=TurnIntent.RESUME,
            confidence=0.99,
            affects_evaluation=False,
            answer_then_resume=True,
            assistant_reply="收到，继续当前 revision 的评估，并从最近的安全 checkpoint 恢复。",
            control_action="RESUME",
            reason="explicit_resume_command",
        )
    if _matches_any(text, _CANCEL_PATTERNS) and not cancel_negated:
        return TurnDecision(
            intent=TurnIntent.CANCEL,
            confidence=0.99,
            affects_evaluation=False,
            answer_then_resume=False,
            assistant_reply="收到，已请求取消当前评估；后续迟到结果不会覆盖当前状态。",
            control_action="CANCEL",
            reason="explicit_cancel_command",
        )
    if _matches_any(text, _PAUSE_PATTERNS) and not pause_negated:
        return TurnDecision(
            intent=TurnIntent.PAUSE,
            confidence=0.99,
            affects_evaluation=False,
            answer_then_resume=False,
            assistant_reply="收到，当前节点结束后会在安全边界写入 checkpoint 并暂停。",
            control_action="PAUSE",
            reason="explicit_pause_command",
        )

    if _matches_any(text, _OUTPUT_SIDE_PATTERNS):
        return TurnDecision(
            intent=TurnIntent.SIDE_QUESTION,
            confidence=0.94,
            affects_evaluation=False,
            answer_then_resume=True,
            assistant_reply=_side_question_reply(text, merged_context),
            reason="output_or_explanation_side_quest",
        )

    if _matches_any(text, _GOAL_CHANGE_PATTERNS):
        return TurnDecision(
            intent=TurnIntent.GOAL_CHANGE,
            confidence=0.94,
            affects_evaluation=True,
            answer_then_resume=False,
            affected_nodes=list(GOAL_CHANGE_NODES),
            assistant_reply="这会改变评估目标，需要创建新的 revision；简历解析结果可以复用，其余相关节点会重新评估。",
            evaluation_patch={"goalInstruction": text},
            reason="explicit_target_role_change",
        )

    if _matches_any(text, _FOCUS_PATTERNS):
        return TurnDecision(
            intent=TurnIntent.EVALUATION_FOCUS,
            confidence=0.93,
            affects_evaluation=True,
            answer_then_resume=False,
            affected_nodes=list(FOCUS_CHANGE_NODES),
            assistant_reply="这会改变评估重点，需要创建新的 revision；岗位与简历解析可复用，受影响的评估和报告节点会重跑。",
            evaluation_patch={"focusInstruction": text},
            reason="explicit_evaluation_focus_change",
        )

    if _matches_any(text, _CONTEXT_PREFIX_PATTERNS):
        has_eval_fact = _contains_any(text, _EVALUATION_FACT_TERMS)
        is_admin_note = _contains_any(text, _ADMIN_NOTE_TERMS) and not has_eval_fact
        if is_admin_note:
            return TurnDecision(
                intent=TurnIntent.CONTEXT_ADD,
                confidence=0.95,
                affects_evaluation=False,
                answer_then_resume=True,
                assistant_reply="已记录为会话备注，不修改评估输入，当前任务继续运行。",
                evaluation_patch={"note": text},
                reason="non_evaluation_note",
            )
        if has_eval_fact:
            return TurnDecision(
                intent=TurnIntent.CONTEXT_ADD,
                confidence=0.92,
                affects_evaluation=True,
                answer_then_resume=False,
                affected_nodes=list(ALL_EVALUATION_NODES),
                assistant_reply="这条补充会影响候选人事实，需要创建新的 revision 并重新评估相关节点。",
                evaluation_patch={"additionalContext": text},
                reason="candidate_evidence_added",
            )
        return TurnDecision(
            intent=TurnIntent.CLARIFY,
            confidence=0.72,
            affects_evaluation=False,
            answer_then_resume=True,
            assistant_reply="这条补充是只作为备注，还是要纳入评估结论？确认前我不会中断当前任务。",
            requires_confirmation=True,
            evaluation_patch={"pendingContext": text},
            reason="ambiguous_context_add",
        )

    # Phrases that hint at a new target but omit a concrete role must not
    # supersede useful work.  Ask once while allowing the active run to proceed.
    if _matches_any(text, (r"(?:换|改)(?:个|一下)?(?:方向|岗位|目标)", r"也看看.+(?:方向|岗位)?")):
        return TurnDecision(
            intent=TurnIntent.CLARIFY,
            confidence=0.78,
            affects_evaluation=False,
            answer_then_resume=True,
            assistant_reply="你想切换到哪个具体岗位，还是只想增加一个评估重点？确认前当前评估会继续。",
            requires_confirmation=True,
            evaluation_patch={"pendingGoal": text},
            reason="ambiguous_goal_change",
        )

    if "?" in text or "？" in text or _contains_any(text, _QUESTION_TERMS):
        return TurnDecision(
            intent=TurnIntent.SIDE_QUESTION,
            confidence=0.9,
            affects_evaluation=False,
            answer_then_resume=True,
            assistant_reply=_side_question_reply(text, merged_context),
            reason="independent_question",
        )

    return TurnDecision(
        intent=TurnIntent.SIDE_QUESTION,
        confidence=0.62,
        affects_evaluation=False,
        answer_then_resume=True,
        assistant_reply=_side_question_reply(text, merged_context),
        reason="safe_non_mutating_fallback",
    )


async def resolve_turn_with_model(
    message: str,
    *,
    run_status: str = "RUNNING",
    revision: int = 1,
    context: Optional[Mapping[str, Any]] = None,
) -> TurnDecision:
    """Use a constrained LLM only for genuinely ambiguous/new user ideas.

    Explicit control and high-confidence mutation rules never reach the model.
    The model cannot emit a control action and cannot invent graph nodes.  If
    credentials, parsing, or the provider fail, the deterministic safe decision
    is returned unchanged so an active evaluation is never interrupted by an
    intent-classification outage.
    """

    baseline = resolve_turn(
        message,
        run_status=run_status,
        revision=revision,
        context=context,
    )
    if baseline.control_action or baseline.confidence >= 0.8:
        return baseline

    try:
        import httpx

        from app.config import normalized_deepseek_base_url, settings

        if not settings.deepseek_api_key:
            return baseline

        safe_context = dict(context or {})
        compact_context = {
            key: safe_context.get(key)
            for key in (
                "runStatus",
                "currentNode",
                "currentSummary",
                "selectedText",
                "topJdMatches",
                "interviewQuestions",
            )
            if safe_context.get(key) not in (None, "", [], {})
        }
        prompt = """你是持续简历评估会话的意图路由器。只输出 JSON，不要 markdown。
允许 intent：SIDE_QUESTION、CONTEXT_ADD、GOAL_CHANGE、EVALUATION_FOCUS、CLARIFY。
规则：临时提问、解释、改写、岗位比较、模拟面试均为 SIDE_QUESTION，不能打断主评估；
只有明确改变候选人事实、JD、目标岗位或评分重点才 affectsEvaluation=true；含糊变更必须 CLARIFY；
不得输出 PAUSE/RESUME/CANCEL，控制命令只由确定性规则处理；不得创造候选人事实。
输出：{"intent":"SIDE_QUESTION","affectsEvaluation":false,"requiresConfirmation":false,
"assistantReply":"基于给定上下文的简短回答；证据不足则说明需要什么","reason":"简短理由"}。"""
        user_payload = json.dumps(
            {
                "message": message,
                "runStatus": run_status,
                "revision": revision,
                "context": compact_context,
            },
            ensure_ascii=False,
            default=str,
        )
        async with httpx.AsyncClient(timeout=25.0) as client:
            response = await client.post(
                f"{normalized_deepseek_base_url()}/chat/completions",
                headers={"Authorization": f"Bearer {settings.deepseek_api_key}",
                         "Content-Type": "application/json"},
                json={
                    "model": settings.deepseek_model,
                    "messages": [
                        {"role": "system", "content": prompt},
                        {"role": "user", "content": user_payload},
                    ],
                    "temperature": 0,
                    "max_tokens": 500,
                    "stream": False,
                })
            response.raise_for_status()
            data = response.json()
        raw = str(data["choices"][0]["message"]["content"] or "")
        start, end = raw.find("{"), raw.rfind("}")
        if start < 0 or end <= start:
            return baseline
        payload = json.loads(raw[start : end + 1])
        intent = TurnIntent(str(payload.get("intent", "SIDE_QUESTION")))
        if intent in {TurnIntent.PAUSE, TurnIntent.RESUME, TurnIntent.CANCEL}:
            return baseline
        affects = bool(payload.get("affectsEvaluation", False))
        needs_confirmation = bool(payload.get("requiresConfirmation", False))
        if intent == TurnIntent.GOAL_CHANGE:
            affects, affected_nodes = True, list(GOAL_CHANGE_NODES)
        elif intent == TurnIntent.EVALUATION_FOCUS:
            affects, affected_nodes = True, list(FOCUS_CHANGE_NODES)
        elif intent == TurnIntent.CONTEXT_ADD and affects:
            affected_nodes = list(ALL_EVALUATION_NODES)
        else:
            affects, affected_nodes = False, []
        reply = str(payload.get("assistantReply") or baseline.assistant_reply).strip()
        return TurnDecision(
            intent=intent,
            confidence=0.82,
            affects_evaluation=affects,
            answer_then_resume=not affects,
            affected_nodes=affected_nodes,
            assistant_reply=reply,
            evaluation_patch={"modelRoutedInstruction": message} if affects else {},
            requires_confirmation=needs_confirmation,
            reason="model_fallback:" + str(payload.get("reason") or "ambiguous_turn"),
        )
    except Exception:
        return baseline
