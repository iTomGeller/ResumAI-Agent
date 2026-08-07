from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import httpx

from app.config import settings
from app.runtime.events import RuntimeEmitter
from app.runtime.models import ContextBudget

logger = logging.getLogger(__name__)

_CJK = re.compile(r"[\u4e00-\u9fff\u3000-\u303f\uff00-\uffef]")

# Calibration state updated from real provider usage (see calibrate()).
_CALIBRATION = {"factor": 1.0, "samples": 0}


def estimate_tokens(text: str) -> int:
    """Mixed zh/en estimate calibrated against provider usage.

    DeepSeek tokenizes CJK at roughly 0.6–0.7 tokens/char and ASCII at
    roughly 0.25–0.3 tokens/char. We estimate both populations separately,
    then apply a safety factor continuously calibrated from real API usage
    (always >= 1.0 so the budget stays conservative).
    """
    if not text:
        return 0
    cjk = len(_CJK.findall(text))
    other = len(text) - cjk
    base = cjk * 0.7 + other / 3.6
    return max(1, int(base * max(1.0, _CALIBRATION["factor"])))


def calibrate(estimated_prompt_tokens: int, actual_prompt_tokens: int) -> None:
    """Feed real usage back into the estimator (exponential moving average)."""
    if estimated_prompt_tokens <= 0 or actual_prompt_tokens <= 0:
        return
    observed = actual_prompt_tokens / estimated_prompt_tokens
    weight = 0.2
    _CALIBRATION["factor"] = (1 - weight) * _CALIBRATION["factor"] + weight * observed
    _CALIBRATION["samples"] += 1


@dataclass
class CompactionRecord:
    summary_version: int
    source_message_start_id: Optional[int]
    source_message_end_id: Optional[int]
    first_kept_message_id: Optional[int]
    before_token_estimate: int
    after_token_estimate: int
    reason: str
    summary: str


_TOOL_CALL_ID = re.compile(r"\[TOOL_CALL \S+ id=(tc-[0-9a-f]+)\]")
_TOOL_RESULT_ID = re.compile(r"\[TOOL_RESULT \S+ id=(tc-[0-9a-f]+)")


class ContextManager:
    """Token-budgeted context assembly with structured compaction.

    Assembly order keeps the reusable prompt prefix first: system → policy →
    skills → output contract → current request → goal → conversation summary
    → recent messages → memory → shared state → tool results. DeepSeek's
    automatic cache matches an exact prefix, so stable instructions must not
    sit behind resume-specific state. Compaction triggers when the estimate crosses
    compactAtRatio of the model window and never drops the newest user
    request, the goal, cancellation constraints or any tool call whose
    result would be separated from it (pairing is checked per toolCallId).
    """

    def __init__(self, budget: ContextBudget, emitter: RuntimeEmitter,
                 run_id: str, conversation_id: str) -> None:
        self.budget = budget
        self.emitter = emitter
        self.run_id = run_id
        self.conversation_id = conversation_id
        self.summary_version = 0
        self.compactions: List[CompactionRecord] = []

    # ---------------- message history ----------------

    def prepare_messages(self, recent_messages: List[Dict[str, Any]],
                         conversation_summary: str) -> Dict[str, Any]:
        """Fit recent messages into recentMessageBudget; overflow becomes a
        structured summary block merged with the stored conversation summary."""
        kept: List[Dict[str, Any]] = []
        used = 0
        overflow: List[Dict[str, Any]] = []
        for message in reversed(recent_messages):
            cost = estimate_tokens(str(message.get("content") or ""))
            if used + cost <= self.budget.recentMessageBudget or not kept:
                kept.append(message)
                used += cost
            else:
                overflow.append(message)
        kept.reverse()
        overflow.reverse()
        summary = conversation_summary or ""
        if overflow:
            summary_lines = [summary] if summary else []
            summary_lines.append("此前对话要点（自动压缩）：")
            for message in overflow[-12:]:
                role = "用户" if str(message.get("role", "")).upper() == "USER" else "助手"
                content = str(message.get("content") or "").replace("\n", " ")
                summary_lines.append(f"- {role}: {content[:120]}")
            summary = "\n".join(summary_lines)
        return {"kept": kept, "summary": summary, "overflowCount": len(overflow),
                "overflow": overflow}

    # ---------------- assembly ----------------

    def assemble(self, *, system_prompt: str, policy_instructions: str,
                 skill_instructions: str, user_request: str, current_goal: str,
                 shared_state_digest: str, recent_messages: List[Dict[str, Any]],
                 conversation_summary: str, memory_block: str,
                 rag_context_block: str, tool_results_block: str,
                 output_schema: str) -> List[Dict[str, str]]:
        prepared = self.prepare_messages(recent_messages, conversation_summary)

        system_block = self._cap(system_prompt, self.budget.systemBudget)
        if policy_instructions:
            system_block += "\n\n[策略要求]\n" + self._cap(
                policy_instructions, self.budget.policyBudget)
        if skill_instructions:
            system_block += "\n\n[技能指令]\n" + self._cap(
                skill_instructions, self.budget.skillBudget)
        # The output contract is agent/version specific but candidate
        # independent. Keeping it in the system prefix lets DeepSeek reuse it
        # across runs instead of placing it after resume/tool content where an
        # exact-prefix cache can never reach it.
        if output_schema:
            system_block += "\n\n[输出要求]\n" + output_schema

        # Put the usually stable evaluation intent before candidate-specific
        # context. Section labels preserve semantics; the ordering only
        # increases the reusable exact prefix.
        user_block_sections = ["[当前请求]\n" + user_request]
        if current_goal:
            user_block_sections.append("[当前目标]\n" + current_goal)
        if prepared["summary"]:
            user_block_sections.append("[会话摘要]\n" + self._cap(prepared["summary"], 1500))
        if prepared["kept"]:
            history = "\n".join(
                f"{'用户' if str(m.get('role','')).upper() == 'USER' else '助手'}: "
                f"{str(m.get('content') or '')[:400]}"
                for m in prepared["kept"][-8:])
            user_block_sections.append("[近期消息]\n" + history)
        if rag_context_block:
            user_block_sections.append("[RAG上下文]\n" + self._cap(
                rag_context_block, self.budget.toolResultBudget))
        if memory_block:
            user_block_sections.append("[相关记忆]\n" + self._cap(
                memory_block, self.budget.memoryBudget))
        if shared_state_digest:
            user_block_sections.append("[共享状态]\n" + self._cap(
                shared_state_digest, self.budget.toolResultBudget))
        if tool_results_block:
            user_block_sections.append("[工具观察]\n" + self._cap_tools(
                tool_results_block, self.budget.toolResultBudget))

        return [
            {"role": "system", "content": system_block},
            {"role": "user", "content": "\n\n".join(user_block_sections)},
        ]

    def needs_compaction(self, messages: List[Dict[str, str]]) -> bool:
        total = sum(estimate_tokens(m["content"]) for m in messages)
        window = self.budget.modelWindow - self.budget.reservedOutputBudget
        return total >= window * self.budget.compactAtRatio

    def estimate(self, messages: List[Dict[str, str]]) -> int:
        return sum(estimate_tokens(m["content"]) for m in messages)

    async def compact(self, messages: List[Dict[str, str]], *, reason: str,
                      protected_markers: List[str],
                      recent_messages: Optional[List[Dict[str, Any]]] = None
                      ) -> List[Dict[str, str]]:
        """Shrink the user block while preserving protected sections whole and
        keeping every tool call/result pair together (per toolCallId)."""
        before = self.estimate(messages)
        compacted: List[Dict[str, str]] = []
        for message in messages:
            content = message["content"]
            if message["role"] == "system":
                compacted.append(message)
                continue
            sections = content.split("\n\n")
            kept_sections: List[str] = []
            for section in sections:
                header = section.split("\n", 1)[0]
                if any(marker in header for marker in protected_markers):
                    kept_sections.append(section)
                elif header.startswith("[工具观察]"):
                    kept_sections.append(self._summarize_tools(section))
                elif header.startswith("[RAG上下文]"):
                    kept_sections.append(section[:1600])
                elif header.startswith("[近期消息]"):
                    lines = section.splitlines()
                    kept_sections.append("\n".join(lines[:1] + lines[-4:]))
                elif header.startswith("[相关记忆]") or header.startswith("[会话摘要]"):
                    kept_sections.append(section[:800])
                else:
                    kept_sections.append(section[:600])
            compacted.append({"role": message["role"], "content": "\n\n".join(kept_sections)})
        after = self.estimate(compacted)
        self.summary_version += 1

        start_id = end_id = first_kept_id = None
        if recent_messages:
            ids = [m.get("id") for m in recent_messages
                   if isinstance(m.get("id"), int)]
            if ids:
                start_id, end_id = min(ids), max(ids)
                prepared = self.prepare_messages(recent_messages, "")
                kept_ids = [m.get("id") for m in prepared["kept"]
                            if isinstance(m.get("id"), int)]
                first_kept_id = min(kept_ids) if kept_ids else end_id

        record = CompactionRecord(
            summary_version=self.summary_version,
            source_message_start_id=start_id,
            source_message_end_id=end_id,
            first_kept_message_id=first_kept_id,
            before_token_estimate=before,
            after_token_estimate=after,
            reason=reason,
            summary=compacted[-1]["content"][:4000] if compacted else "")
        self.compactions.append(record)
        await self.emitter.emit("context.compacted", payload={
            "summaryVersion": record.summary_version,
            "beforeTokens": before, "afterTokens": after, "reason": reason,
            "sourceMessageStartId": start_id, "sourceMessageEndId": end_id,
            "firstKeptMessageId": first_kept_id})
        await self._persist(record)
        return compacted

    @staticmethod
    def consistency_check(messages: List[Dict[str, str]], *, user_request: str,
                          current_goal: str) -> List[str]:
        """Post-compaction invariants. Tool calls and results are matched
        per toolCallId, not by count."""
        violations = []
        joined = "\n".join(m["content"] for m in messages)
        if user_request and user_request[:80] not in joined:
            violations.append("latest_user_request_lost")
        if current_goal and current_goal[:60] and current_goal[:60] not in joined:
            violations.append("current_goal_lost")
        call_ids = set(_TOOL_CALL_ID.findall(joined))
        result_ids = set(_TOOL_RESULT_ID.findall(joined))
        if call_ids - result_ids:
            violations.append(
                f"tool_call_without_result:{sorted(call_ids - result_ids)[:3]}")
        if result_ids - call_ids:
            violations.append(
                f"tool_result_without_call:{sorted(result_ids - call_ids)[:3]}")
        return violations

    @staticmethod
    def _summarize_tools(section: str) -> str:
        """Compress tool observations while keeping every call/result pair.
        Pairs are units: either both lines survive (result truncated) or the
        pair is dropped as a whole and mentioned in the summary line."""
        lines = section.splitlines()
        header = lines[0]
        pairs: List[List[str]] = []
        current: List[str] = []
        for line in lines[1:]:
            if line.startswith("[TOOL_CALL"):
                if current:
                    pairs.append(current)
                current = [line]
            elif current:
                current.append(line)
        if current:
            pairs.append(current)
        kept_pairs = pairs[-6:]
        dropped = len(pairs) - len(kept_pairs)
        out = [header + f"（已压缩，保留最近 {len(kept_pairs)} 组工具调用"
               + (f"，省略 {dropped} 组" if dropped > 0 else "") + "）"]
        for pair in kept_pairs:
            for line in pair[:2]:
                out.append(line[:300])
        return "\n".join(out)

    def _cap(self, text: str, budget_tokens: int) -> str:
        if not text:
            return ""
        if estimate_tokens(text) <= budget_tokens:
            return text
        max_chars = int(budget_tokens * 2.5)
        return text[:max_chars] + "\n...[超出预算已截断]"

    def _cap_tools(self, text: str, budget_tokens: int) -> str:
        """Cap the tool block without splitting a call/result pair."""
        if estimate_tokens(text) <= budget_tokens:
            return text
        return self._summarize_tools("[工具观察]\n" + text).split("\n", 1)[-1]

    async def _persist(self, record: CompactionRecord) -> None:
        body = {
            "runId": self.run_id,
            "conversationId": self.conversation_id,
            "summaryVersion": record.summary_version,
            "sourceMessageStartId": record.source_message_start_id,
            "sourceMessageEndId": record.source_message_end_id,
            "firstKeptMessageId": record.first_kept_message_id,
            "beforeTokenEstimate": record.before_token_estimate,
            "afterTokenEstimate": record.after_token_estimate,
            "reason": record.reason,
            "summary": record.summary,
        }
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                await client.post(
                    f"{settings.java_backend_url.rstrip('/')}/api/internal/agent-runs/context-snapshots",
                    json=body,
                    headers={"X-Internal-Token": settings.workflow_internal_token})
        except Exception as exc:  # noqa: BLE001
            logger.info("context snapshot persist skipped: %s", exc)
