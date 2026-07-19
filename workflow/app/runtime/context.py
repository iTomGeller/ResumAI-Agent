from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import httpx

from app.config import settings
from app.runtime.events import RuntimeEmitter
from app.runtime.models import ContextBudget

logger = logging.getLogger(__name__)


def estimate_tokens(text: str) -> int:
    """Conservative mixed zh/en estimate: ~1 token per 3 chars."""
    if not text:
        return 0
    return max(1, len(text) // 3)


@dataclass
class ContextPart:
    name: str
    content: str
    budget_tokens: int
    keep_always: bool = False

    @property
    def tokens(self) -> int:
        return estimate_tokens(self.content)


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


class ContextManager:
    """Token-budgeted context assembly with structured compaction.

    Assembly order (spec): system → policy → skills → current request →
    goal → shared state → recent messages → conversation summary → memory →
    tool results → output schema. Compaction triggers when the estimate
    crosses compactAtRatio of the model window and never drops the newest
    user request, the goal, cancellation constraints or unfinished tool
    call/result pairs (tool results are only ever summarized as one unit).
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
        return {"kept": kept, "summary": summary, "overflowCount": len(overflow)}

    # ---------------- assembly ----------------

    def assemble(self, *, system_prompt: str, policy_instructions: str,
                 skill_instructions: str, user_request: str, current_goal: str,
                 shared_state_digest: str, recent_messages: List[Dict[str, Any]],
                 conversation_summary: str, memory_block: str,
                 tool_results_block: str, output_schema: str) -> List[Dict[str, str]]:
        parts = [
            ContextPart("system", self._cap(system_prompt, self.budget.systemBudget), self.budget.systemBudget, True),
            ContextPart("policy", self._cap(policy_instructions, self.budget.policyBudget), self.budget.policyBudget),
            ContextPart("skills", self._cap(skill_instructions, self.budget.skillBudget), self.budget.skillBudget),
            ContextPart("request", user_request, 10_000, True),
            ContextPart("goal", current_goal or "", 600, True),
            ContextPart("shared_state", shared_state_digest, self.budget.toolResultBudget),
            ContextPart("memory", self._cap(memory_block, self.budget.memoryBudget), self.budget.memoryBudget),
            ContextPart("tool_results", self._cap(tool_results_block, self.budget.toolResultBudget), self.budget.toolResultBudget),
            ContextPart("output_schema", output_schema, 1200, True),
        ]
        prepared = self.prepare_messages(recent_messages, conversation_summary)

        system_block = parts[0].content
        if parts[1].content:
            system_block += "\n\n[策略要求]\n" + parts[1].content
        if parts[2].content:
            system_block += "\n\n[技能指令]\n" + parts[2].content

        user_block_sections = []
        if prepared["summary"]:
            user_block_sections.append("[会话摘要]\n" + self._cap(prepared["summary"], 1500))
        if prepared["kept"]:
            history = "\n".join(
                f"{'用户' if str(m.get('role','')).upper() == 'USER' else '助手'}: "
                f"{str(m.get('content') or '')[:400]}"
                for m in prepared["kept"][-8:])
            user_block_sections.append("[近期消息]\n" + history)
        if parts[6].content:
            user_block_sections.append("[相关记忆]\n" + parts[6].content)
        if parts[5].content:
            user_block_sections.append("[共享状态]\n" + parts[5].content)
        if parts[7].content:
            user_block_sections.append("[工具观察]\n" + parts[7].content)
        if parts[4].content:
            user_block_sections.append("[当前目标]\n" + parts[4].content)
        user_block_sections.append("[当前请求]\n" + parts[3].content)
        user_block_sections.append("[输出要求]\n" + parts[8].content)

        messages = [
            {"role": "system", "content": system_block},
            {"role": "user", "content": "\n\n".join(user_block_sections)},
        ]
        return messages

    def needs_compaction(self, messages: List[Dict[str, str]]) -> bool:
        total = sum(estimate_tokens(m["content"]) for m in messages)
        window = self.budget.modelWindow - self.budget.reservedOutputBudget
        return total >= window * self.budget.compactAtRatio

    def estimate(self, messages: List[Dict[str, str]]) -> int:
        return sum(estimate_tokens(m["content"]) for m in messages)

    async def compact(self, messages: List[Dict[str, str]], *, reason: str,
                      protected_markers: List[str]) -> List[Dict[str, str]]:
        """Shrink the user block while preserving protected sections whole."""
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
        record = CompactionRecord(
            summary_version=self.summary_version,
            source_message_start_id=None,
            source_message_end_id=None,
            first_kept_message_id=None,
            before_token_estimate=before,
            after_token_estimate=after,
            reason=reason,
            summary=compacted[-1]["content"][:4000] if compacted else "")
        self.compactions.append(record)
        await self.emitter.emit("context.compacted", payload={
            "summaryVersion": record.summary_version,
            "beforeTokens": before, "afterTokens": after, "reason": reason})
        await self._persist(record)
        return compacted

    @staticmethod
    def consistency_check(messages: List[Dict[str, str]], *, user_request: str,
                          current_goal: str) -> List[str]:
        """Post-compaction invariants (spec §14). Returns violation list."""
        violations = []
        joined = "\n".join(m["content"] for m in messages)
        if user_request and user_request[:80] not in joined:
            violations.append("latest_user_request_lost")
        if current_goal and current_goal[:60] and current_goal[:60] not in joined:
            violations.append("current_goal_lost")
        open_calls = joined.count("[TOOL_CALL ")
        closed_calls = joined.count("[TOOL_RESULT ")
        if open_calls != closed_calls:
            violations.append("tool_call_result_pair_broken")
        return violations

    @staticmethod
    def _summarize_tools(section: str) -> str:
        lines = section.splitlines()
        header, body = lines[0], lines[1:]
        summarized: List[str] = [header + "（已压缩，保留关键字段）"]
        for line in body:
            if line.startswith("[TOOL_CALL") or line.startswith("[TOOL_RESULT"):
                summarized.append(line[:300])
            elif len(summarized) < 24:
                summarized.append(line[:160])
        return "\n".join(summarized)

    def _cap(self, text: str, budget_tokens: int) -> str:
        if not text:
            return ""
        max_chars = budget_tokens * 3
        if len(text) <= max_chars:
            return text
        return text[:max_chars] + "\n...[超出预算已截断]"

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
