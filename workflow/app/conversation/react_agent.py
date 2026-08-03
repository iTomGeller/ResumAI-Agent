"""
ReAct Agent for Copilot conversations.
Implements Thought -> Action -> Observation loop with:
- Budget caps (max_steps, total timeout)
- Loop detection (same tool+args signature = break)
- Observation trimming (prevent context explosion)
- Error recovery (parse failures with retry + nudge)
- Structured trace output (for frontend display)
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import httpx

from app.config import normalized_deepseek_base_url, settings
from app.conversation.copilot_mcp import COPILOT_MCP_REGISTRY, call_mcp_tool
from app.conversation.models import CopilotAction, CopilotAnswer, SourceRef

logger = logging.getLogger(__name__)

MAX_STEPS = 5
MAX_OBSERVATION_CHARS = 800
LLM_TIMEOUT = 15.0
TOTAL_TIMEOUT = 25.0


@dataclass
class ReactStep:
    """One Thought-Action-Observation triple."""
    thought: str
    action: Optional[str] = None
    action_args: Optional[Dict] = None
    observation: Optional[str] = None
    error: Optional[str] = None
    duration_ms: int = 0


@dataclass
class ReactRunState:
    """Mutable state for one ReAct execution."""
    steps: List[ReactStep] = field(default_factory=list)
    tool_signatures: set = field(default_factory=set)
    total_tokens: int = 0
    started_at: float = field(default_factory=time.monotonic)
    parse_failures: int = 0

    @property
    def elapsed_s(self) -> float:
        return time.monotonic() - self.started_at

    def is_loop(self, tool: str, args: Dict) -> bool:
        sig = hashlib.md5(f"{tool}:{json.dumps(args, sort_keys=True)}".encode()).hexdigest()
        if sig in self.tool_signatures:
            return True
        self.tool_signatures.add(sig)
        return False


REACT_SYSTEM = """你是 ResumAI 招聘决策 Copilot，使用 ReAct 推理模式回答HR和面试官的问题。

## 可用工具（MCP 协议）
{tool_descriptions}

## 输出格式（严格遵循）

每一步输出：
Thought: <你的推理：分析问题、决定下一步行动、评估已有信息是否足够>
Action: <tool_name>({{"param": "value"}})

当信息足够时：
Thought: <总结推理过程，说明结论依据>
Final Answer: <给HR/面试官的回答，简洁有用，带证据引用>

## 规则
1. 绝不编造候选人事实——证据不足就说「当前评估数据中未包含此信息」
2. 每步只调用一个工具
3. 如果工具返回空或错误，换个角度或直接基于已有信息回答
4. 最多 {max_steps} 步必须给出 Final Answer
5. Final Answer 面向非技术HR时用通俗语言，面向面试官时可用专业术语
6. 回答中引用证据时标注来源（如「报告技术维度评分75」）"""


async def react_copilot(
    message: str,
    intent: str,
    context_snapshot: Dict[str, Any],
    *,
    max_steps: int = MAX_STEPS,
) -> Optional[CopilotAnswer]:
    """Execute ReAct loop. Returns None if ReAct unavailable or fails."""

    available_tools = _select_tools(intent)
    tool_desc = "\n".join(
        f"- {name}: {t['description']} | 参数: {json.dumps(t['parameters'], ensure_ascii=False)}"
        for name, t in available_tools.items()
    )

    system = REACT_SYSTEM.format(tool_descriptions=tool_desc, max_steps=max_steps)

    context_brief = _build_context_brief(context_snapshot, intent)
    user_msg = f"""## 当前候选人评估上下文
{context_brief}

## 用户问题
{message}

请开始 ReAct 推理。"""

    messages: List[Dict[str, str]] = [
        {"role": "system", "content": system},
        {"role": "user", "content": user_msg},
    ]

    state = ReactRunState()

    for step_idx in range(max_steps):
        if state.elapsed_s > TOTAL_TIMEOUT:
            logger.info("ReAct timeout after %.1fs", state.elapsed_s)
            break

        t0 = time.monotonic()
        try:
            raw = await _llm_call(messages)
        except Exception as e:
            logger.warning("ReAct LLM call failed: %s", e)
            break
        duration = int((time.monotonic() - t0) * 1000)

        thought, action, final_answer = _parse_response(raw)

        if final_answer:
            step = ReactStep(thought=thought or "", duration_ms=duration)
            state.steps.append(step)
            return _build_answer(final_answer, state, context_snapshot, message)

        if not action:
            state.parse_failures += 1
            if state.parse_failures >= 2:
                break
            messages.append({"role": "assistant", "content": raw})
            messages.append({"role": "user", "content":
                "请严格按格式输出。如果已有足够信息，输出 'Final Answer: ...'。"
                "如果需要更多数据，输出 'Action: tool_name({...})'。"})
            continue

        tool_name, tool_args = action
        step = ReactStep(thought=thought or "", action=tool_name,
                         action_args=tool_args, duration_ms=duration)

        if state.is_loop(tool_name, tool_args):
            step.error = "loop_detected"
            state.steps.append(step)
            logger.info("ReAct loop detected: %s(%s)", tool_name, tool_args)
            break

        if tool_name not in available_tools:
            step.observation = f"工具 {tool_name} 不可用。可用: {list(available_tools.keys())}"
            state.steps.append(step)
            messages.append({"role": "assistant", "content": raw})
            messages.append({"role": "user", "content": f"Observation: {step.observation}"})
            continue

        try:
            result = await call_mcp_tool(tool_name, tool_args, context_snapshot)
            observation = _trim_observation(result, MAX_OBSERVATION_CHARS)
        except Exception as e:
            observation = f"工具执行失败: {str(e)[:200]}"
            step.error = str(e)[:100]

        step.observation = observation
        state.steps.append(step)

        messages.append({"role": "assistant", "content": raw})
        messages.append({"role": "user", "content": f"Observation: {observation}"})

    return await _force_final(messages, state, message)


def _parse_response(raw: str) -> Tuple[Optional[str], Optional[Tuple[str, Dict]], Optional[str]]:
    """Parse LLM output into (thought, action_tuple, final_answer)."""
    thought = None
    thought_match = re.search(r"Thought:\s*(.+?)(?=\nAction:|\nFinal Answer:|\Z)", raw, re.DOTALL)
    if thought_match:
        thought = thought_match.group(1).strip()

    final_match = re.search(r"Final Answer:\s*(.+)", raw, re.DOTALL)
    if final_match:
        return thought, None, final_match.group(1).strip()

    action_match = re.search(r"Action:\s*(\w+)\((.+?)\)\s*$", raw, re.MULTILINE | re.DOTALL)
    if action_match:
        tool_name = action_match.group(1)
        raw_args = action_match.group(2).strip()
        try:
            args = json.loads(raw_args)
        except json.JSONDecodeError:
            args = {"query": raw_args.strip('"').strip("'")}
        return thought, (tool_name, args), None

    return thought, None, None


def _trim_observation(result: Any, max_chars: int) -> str:
    """Trim tool output to prevent context explosion."""
    if isinstance(result, dict):
        text = json.dumps(result, ensure_ascii=False, indent=None)
    elif isinstance(result, list):
        text = json.dumps(result[:5], ensure_ascii=False)
    else:
        text = str(result)
    if len(text) > max_chars:
        return text[:max_chars] + f"...(截断，原长{len(text)}字符)"
    return text


def _select_tools(intent: str) -> Dict[str, Any]:
    """Intent-based tool selection."""
    all_tools = COPILOT_MCP_REGISTRY
    intent_map = {
        "report_qa": ("search_report", "get_dimension_detail"),
        "compare": ("search_report", "compare_candidates"),
        "interview_prep": ("generate_interview_question", "search_report"),
        "jd_gap": ("fetch_jd_gaps", "search_report"),
        "suggestion": ("search_report", "fetch_jd_gaps", "compare_candidates"),
    }
    tool_names = intent_map.get(intent)
    if tool_names:
        return {k: v for k, v in all_tools.items() if k in tool_names}
    return all_tools


def _build_context_brief(snapshot: Dict, intent: str) -> str:
    """Build minimal context for ReAct system prompt."""
    parts = []
    report = snapshot.get("structuredReport") or {}
    if isinstance(report, dict):
        if report.get("recommendation"):
            parts.append(f"推荐结论: {report['recommendation']}")
        dims = report.get("dimensions") or []
        if dims:
            parts.append("评分: " + ", ".join(
                f"{d.get('name', '?')}={d.get('score', '?')}"
                for d in dims[:4] if isinstance(d, dict)))
        risks = report.get("risks") or []
        if risks:
            parts.append(f"风险数: {len(risks)}")
        strengths = report.get("strengths") or []
        if strengths:
            parts.append(f"优势数: {len(strengths)}")
    if snapshot.get("hasJobDescription"):
        parts.append("已挂载JD")
    if snapshot.get("hasResume"):
        parts.append("已挂载简历")
    return "\n".join(parts) if parts else "暂无评估数据"


async def _llm_call(messages: List[Dict]) -> str:
    """Single LLM call to DeepSeek for ReAct reasoning."""
    async with httpx.AsyncClient(timeout=LLM_TIMEOUT) as client:
        resp = await client.post(
            f"{normalized_deepseek_base_url()}/chat/completions",
            headers={"Authorization": f"Bearer {settings.deepseek_api_key}",
                     "Content-Type": "application/json"},
            json={"model": settings.deepseek_model,
                  "messages": messages,
                  "temperature": 0.1,
                  "max_tokens": 500,
                  "stream": False})
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]


async def _force_final(
    messages: List[Dict], state: ReactRunState, original_question: str
) -> Optional[CopilotAnswer]:
    """Force LLM to produce final answer from accumulated observations."""
    observations = [s.observation for s in state.steps if s.observation]
    if not observations:
        return None
    messages.append({"role": "user", "content":
        "你已经收集了以下信息:\n" +
        "\n".join(f"- {obs[:200]}" for obs in observations[:4]) +
        f"\n\n现在必须给出 Final Answer 回答用户问题：「{original_question}」"})
    try:
        raw = await _llm_call(messages)
        final_match = re.search(r"Final Answer:\s*(.+)", raw, re.DOTALL)
        answer = final_match.group(1).strip() if final_match else raw.strip()
        return _build_answer(answer, state, {}, original_question)
    except Exception:
        return None


def _build_answer(
    answer: str, state: ReactRunState, snapshot: Dict, question: str
) -> CopilotAnswer:
    """Build structured CopilotAnswer with trace metadata."""
    citations = []
    if any(s.action == "search_report" for s in state.steps):
        citations.append(SourceRef(sourceType="SESSION", sourceId="report",
                                   quote="基于评估报告"))
    if any(s.action == "fetch_jd_gaps" for s in state.steps):
        citations.append(SourceRef(sourceType="SESSION", sourceId="jd_match",
                                   quote="基于JD匹配分析"))

    suggestions = _generate_suggestions(state)

    return CopilotAnswer(
        answer=answer,
        citations=citations,
        actions=[],
        suggestions=suggestions,
    )


def _generate_suggestions(state: ReactRunState) -> List[str]:
    """Generate follow-up suggestions based on what tools were used."""
    used_tools = {s.action for s in state.steps if s.action}
    suggestions = []
    if "search_report" not in used_tools:
        suggestions.append("查看详细评分")
    if "generate_interview_question" not in used_tools:
        suggestions.append("生成面试追问")
    if "fetch_jd_gaps" not in used_tools:
        suggestions.append("查看JD缺口")
    if "compare_candidates" not in used_tools:
        suggestions.append("与历史候选人对比")
    return suggestions[:3]
