from __future__ import annotations

import asyncio
import copy
import inspect
import json
import logging
import re
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from app.runtime.agents import AgentDefinition, AgentRegistry, default_agent_registry
from app.runtime.context import ContextManager
from app.runtime.coordinator import Coordinator, TERMINAL_AGENTS
from app.runtime.events import RuntimeEmitter
from app.runtime.llm import (
    LlmError,
    LlmToolCall,
    LlmTurn,
    ResilientLlmClient,
    extract_json_object,
)
from app.runtime.loop_guard import LoopGuard
from app.runtime.memory import (
    CONTROL_PLANE_ERROR_CODES,
    MemoryClient,
    SPECIALIST_TYPES,
    decisions_from_hits,
    filter_hits_for_consumer,
    memory_trace_entries,
)
from app.runtime.models import (
    AgentDecision,
    AgentOutput,
    AgentRunRequest,
    BudgetExceeded,
    PolicyBundle,
    RunBudget,
    RunPaused,
)
from app.runtime.prompts import default_prompt_manager
from app.runtime.builtin_tools import BuiltinToolRegistry
from app.runtime.skills import default_skill_manager
from app.runtime.state import SharedState
from app.runtime.tools import ToolExecutor

logger = logging.getLogger(__name__)

_SOURCE_URL = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z")


def _collect_source_urls(*values: Any, limit: int = 12) -> List[str]:
    """Collect bounded HTTP(S) provenance from nested MCP args/results."""
    found: List[str] = []
    seen = set()

    def visit(value: Any, depth: int = 0) -> None:
        if len(found) >= limit or depth > 6:
            return
        if isinstance(value, dict):
            for nested in value.values():
                visit(nested, depth + 1)
                if len(found) >= limit:
                    break
            return
        if isinstance(value, (list, tuple)):
            for nested in value:
                visit(nested, depth + 1)
                if len(found) >= limit:
                    break
            return
        if not isinstance(value, str):
            return
        for match in _SOURCE_URL.finditer(value[:20000]):
            url = match.group(0).rstrip(".,;:!?)]}")
            if url and url not in seen:
                seen.add(url)
                found.append(url)
                if len(found) >= limit:
                    break

    for item in values:
        visit(item)
        if len(found) >= limit:
            break
    return found


# EXP-7: adaptive replan fires when a group's average confidence drops below
# this. Sweepable per deployment without an image rebuild.
import os as _os

REPLAN_CONFIDENCE_THRESHOLD = float(_os.getenv("REPLAN_CONFIDENCE_THRESHOLD", "0.55"))

AGENT_OUTPUT_SCHEMA = """输出 JSON（不要输出其它内容）：
{
  "thought": "简要计划（一两句）",
  "output": {                                             // 完成本职责时给出，否则为 null
    "summary": "一句话结论",
    "claims": [{"section": "technical_findings|project_findings|risks|evidence|recommendations|resume_facts|jd_requirements",
                 "value": [...] 或 {...}}],
    "evidence": [{"text": "证据描述", "sourceLine": 行号或null, "source": "resume|jd|tool|memory", "verified": true/false/null}],
    "confidence": 0.0-1.0,
    "requestedNextAction": "可选，建议下一步"
  },
  "done": true/false
}
工具调用必须使用模型原生 function/tool calls；禁止在 JSON 中嵌套 toolCalls。"""

REPORT_OUTPUT_SCHEMA = """输出 JSON（不要输出其它内容；精简表达）：
{
  "thought": "简要计划",
  "output": {
    "summary": "面试官视角的一句话结论",
    "confidence": 0.0-1.0,
    "report": {
      "recommendation": "HIRE|INTERVIEW_RECOMMEND|NEED_MANUAL_REVIEW|NOT_RECOMMEND",
      "dimensions": [{"name":"技术能力|项目深度|JD匹配|履历可信度","score":"0-100整数（依据证据合理评分）","status":"ASSESSED|PARTIAL|UNASSESSED","rationale":"判断理由","evidenceRefs":[{"sourceType":"RESUME","sourceId":"resume","quote":"原文≤30字"}]}],
      "strengths": ["有事实支撑的优势"],
      "risks": [{"id":"r1","category":"CANDIDATE","severity":"HIGH|MEDIUM|LOW","claim":"具体风险","verificationPlan":"面试核实方式"}],
      "interviewProbes": [{"id":"q1","priority":"HIGH|MEDIUM","question":"针对性问题","objective":"目的","triggeredBy":"由哪个项目/风险/JD缺口触发","goodSignals":["好信号"],"redFlags":["警示信号"]}],
      "dataQuality": "SUFFICIENT|PARTIAL|INSUFFICIENT",
      "missingEvidence": ["无法从简历判断的信息"]
    }
  },
  "done": true
}
禁止输出 overallScore（系统加权计算）。无证据维度 status=UNASSESSED score=null。
评分标准：60=基本合格，70=良好匹配，80+=优秀匹配。有证据支撑合理给分，不要全部压低。
risks 仅写候选人侧(category=CANDIDATE)；系统/数据问题放 systemWarnings。
interviewProbes 数量≥6（丰富简历）或≥4（信息不足），必须覆盖：每个HIGH风险、TOP3 JD缺口、最重要项目的深挖。"""

# Provider-side schema enforcement (JSON guarantee layer 1): the decision loop
# forces this function; DeepSeek then guarantees arguments match the schema.
# Layers 2-4 (json_object mode, extract_json_object, pydantic + repair) stay
# as fallbacks.
EMIT_DECISION_TOOL = {
    "type": "function",
    "function": {
        "name": "emit_decision",
        "description": "提交本轮 agent 决策（json）：思考、需要的工具调用、结构化输出。",
        "parameters": {
            "type": "object",
            "properties": {
                "thought": {"type": "string", "description": "简要计划"},
                "output": {
                    "type": "object",
                    "properties": {
                        "summary": {"type": "string"},
                        "claims": {"type": "array", "items": {"type": "object"}},
                        "evidence": {"type": "array", "items": {"type": "object"}},
                        "confidence": {"type": "number"},
                        "requestedNextAction": {"type": "string"},
                    },
                },
                "handoff": {
                    "type": "object",
                    "description": "需要移交任务给其它 Agent 时填写",
                    "properties": {
                        "to": {"type": "string"},
                        "reason": {"type": "string"},
                        "task": {"type": "string"},
                    },
                },
                "done": {"type": "boolean"},
            },
            "required": ["done"],
        },
    },
}

# ReportAgent: strong schema for structured report (Markdown is rendered offline).
_SOURCE_REF_SCHEMA = {
    "type": "object",
    "properties": {
        "sourceType": {
            "type": "string",
            "enum": ["RESUME", "JD", "KNOWLEDGE", "EXTERNAL"],
        },
        "sourceId": {"type": "string"},
        "lineStart": {"type": "integer"},
        "lineEnd": {"type": "integer"},
        "quote": {"type": "string"},
        "uri": {"type": "string"},
    },
    "required": ["sourceType", "sourceId", "quote"],
}
_REPORT_DIM = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "score": {"type": ["integer", "null"], "minimum": 0, "maximum": 100},
        "status": {
            "type": "string",
            "enum": ["ASSESSED", "UNASSESSED", "PARTIAL"],
        },
        "evidenceCoverage": {"type": "number", "minimum": 0, "maximum": 1},
        "rationale": {"type": "string"},
        "evidenceRefs": {"type": "array", "items": _SOURCE_REF_SCHEMA},
    },
    "required": ["name", "status", "rationale"],
}
_CANDIDATE_RISK_SCHEMA = {
    "type": "object",
    "properties": {
        "id": {"type": "string"},
        "category": {"type": "string"},
        "severity": {"type": "string", "enum": ["HIGH", "MEDIUM", "LOW"]},
        "confidence": {"type": "number"},
        "claim": {"type": "string"},
        "impact": {"type": "string"},
        "evidenceRefs": {"type": "array", "items": _SOURCE_REF_SCHEMA},
        "verificationPlan": {"type": "string"},
    },
    "required": ["claim"],
}
_INTERVIEW_PROBE_SCHEMA = {
    "type": "object",
    "properties": {
        "id": {"type": "string"},
        "priority": {"type": "string"},
        "question": {"type": "string"},
        "objective": {"type": "string"},
        "triggeredBy": {"type": "string"},
        "evidenceRefs": {"type": "array", "items": _SOURCE_REF_SCHEMA},
        "goodSignals": {"type": "array", "items": {"type": "string"}},
        "redFlags": {"type": "array", "items": {"type": "string"}},
        "followUps": {"type": "array", "items": {"type": "string"}},
        "scoreRubric": {"type": "string"},
    },
    "required": ["question"],
}
_SYSTEM_WARNING_SCHEMA = {
    "type": "object",
    "properties": {
        "code": {"type": "string"},
        "stage": {"type": "string"},
        "retryable": {"type": "boolean"},
        "message": {"type": "string"},
    },
    "required": ["code", "message"],
}
EMIT_REPORT_TOOL = {
    "type": "function",
    "function": {
        "name": "emit_decision",
        "description": "提交 ReportAgent 结构化评估（仅 JSON，不含长 Markdown）。",
        "parameters": {
            "type": "object",
            "properties": {
                "thought": {"type": "string"},
                "output": {
                    "type": "object",
                    "properties": {
                        "summary": {"type": "string"},
                        "confidence": {"type": "number"},
                        "report": {
                            "type": "object",
                            "properties": {
                                "recommendation": {
                                    "type": "string",
                                    "enum": ["HIRE", "INTERVIEW_RECOMMEND",
                                             "NEED_MANUAL_REVIEW", "NOT_RECOMMEND"],
                                },
                                "dimensions": {"type": "array", "items": _REPORT_DIM},
                                "strengths": {"type": "array", "items": {"type": "string"}},
                                "risks": {
                                    "type": "array", "items": _CANDIDATE_RISK_SCHEMA},
                                "interviewQuestions": {
                                    "type": "array", "items": _INTERVIEW_PROBE_SCHEMA},
                                "interviewProbes": {
                                    "type": "array", "items": _INTERVIEW_PROBE_SCHEMA},
                                "systemWarnings": {
                                    "type": "array", "items": _SYSTEM_WARNING_SCHEMA},
                                "dataQuality": {
                                    "type": "string",
                                    "enum": ["SUFFICIENT", "PARTIAL", "INSUFFICIENT"],
                                },
                                "missingEvidence": {
                                    "type": "array", "items": {"type": "string"}},
                            },
                            "required": ["recommendation", "dimensions", "strengths",
                                         "risks", "interviewQuestions", "dataQuality"],
                        },
                    },
                    "required": ["summary", "report"],
                },
                "done": {"type": "boolean"},
            },
            "required": ["done", "output"],
        },
    },
}

# Dimension weights for deterministic overallScore (name normalized lowercase).
_DIMENSION_WEIGHTS = {
    "技术能力": 0.35,
    "项目深度": 0.25,
    "jd匹配": 0.25,
    "履历可信度": 0.15,
}
_CORE_DIMENSION_KEYS = {
    n.replace(" ", "").lower() for n in _DIMENSION_WEIGHTS
}
_MIN_EVIDENCE_COVERAGE = 0.2
_PROCESS_DATA_CATEGORIES = frozenset({"PROCESS", "DATA", "CONTROL_PLANE", "SYSTEM"})
MIN_REPORT_ANSWER_CHARS = 80
MIN_RESUME_TEXT_CHARS = 80
_SCORE_CONTRACT_SKIP_TYPES = {
    "followup", "quick_answer", "resume_optimize", "project_rewrite",
    "interview_questions",
}

# Explicit long-term preference statements we are allowed to persist.
_PREFERENCE_PATTERNS = [
    (re.compile(r"(以后|今后|之后|每次)(都)?(请|要|用|使用|输出|给我)(?P<pref>[^。！!？?]{2,40})"), "explicit_instruction"),
    (re.compile(r"我(更)?(偏好|喜欢|习惯|倾向)(?P<pref>[^。！!？?]{2,40})"), "stated_preference"),
    (re.compile(r"(目标|意向)(岗位|职位)(是|为)(?P<pref>[^。！!？?]{2,30})"), "target_job"),
]


class RunExecutor:
    """Executes one conversational run: Observe → Plan → Select Agents →
    Execute (parallel groups) → Tools → Update Shared State → Finish, with
    budgets, loop guard, layered memory, compaction and pause/resume."""

    def __init__(self, request: AgentRunRequest, emitter: RuntimeEmitter, *,
                 registry: Optional[AgentRegistry] = None,
                 memory: Optional[MemoryClient] = None,
                 builtin_tools: Optional[BuiltinToolRegistry] = None,
                 llm: Optional[ResilientLlmClient] = None,
                 pause_event: Optional[asyncio.Event] = None) -> None:
        self.request = request
        self.emitter = emitter
        self.registry = registry or default_agent_registry
        self.policy = PolicyBundle.from_config(request.policyId, request.policyConfig)
        self.budget = RunBudget()
        global_llm_limit = max(0, int(self.policy.maxLlmCalls))
        terminal_reserve = min(
            max(0, int(self.policy.terminalLlmReserve)),
            max(0, global_llm_limit - 1))
        control_reserve = min(
            max(0, int(self.policy.controlPlaneLlmReserve)),
            max(0, global_llm_limit - terminal_reserve - 1))
        self.budget.configure_llm_budget(
            global_llm_limit,
            {"terminal": terminal_reserve, "control": control_reserve},
            scope_limits={"control": control_reserve})
        # Optional: only runs launched through the service carry a live pause
        # event (kept optional so sync test construction needs no event loop).
        self.pause_event = pause_event
        self.memory = memory or MemoryClient(request.runId, request.conversationId,
                                             request.userId)
        # Production candidate evaluation always uses in-process builtin tools.
        # Docker Sandbox is reserved for Policy Lab / benchmark / replay and
        # must not appear in this dependency graph.
        self.builtin_tools = builtin_tools or BuiltinToolRegistry()
        run_context = {
            "resumeText": request.resumeText or "",
            "jobDescription": request.jobDescription or "",
            "userMessage": request.userMessage or "",
            "recentMessages": list(request.recentMessages or []),
        }
        self.llm = llm or ResilientLlmClient(
            emitter, self.budget, self.policy.maxLlmCalls,
            self.policy.timeoutPolicy.llmTimeoutSeconds,
            max_cost_cny=self.policy.maxCostCny,
            max_total_tokens=self.policy.maxTotalTokens)
        # The tool executor holds an llm reference only for query rewriting
        # (agentic retrieval); it never drives its own decision loop.
        self.tools = ToolExecutor(
            emitter, self.budget, self.builtin_tools,
            max_tool_calls_run=self.policy.toolBudget.maxToolCallsPerRun,
            tool_timeout_seconds=self.policy.timeoutPolicy.toolTimeoutSeconds,
            run_context=run_context, llm=self.llm)
        self.context = ContextManager(self.policy.contextBudget, emitter,
                                      request.runId, request.conversationId)
        self.guard = LoopGuard()
        self.state = SharedState()
        self.skill_selections: Dict[str, List[Any]] = {}
        self.memory_hits: List[Dict[str, Any]] = []
        self.failure_hits: List[Dict[str, Any]] = []
        self.failure_notes: List[str] = []
        self.memory_traces: List[Dict[str, Any]] = []
        self.final_answer: str = ""
        self.degraded_reasons: List[str] = []
        self.agent_counters: Dict[str, Dict[str, int]] = {}
        self.agent_timings: Dict[str, int] = {}
        self.report_agent_failed = False
        # plan-time budget allocation (agent -> {llmQuota, toolQuota});
        # empty means "no per-agent quota", only global limits apply.
        self.budget_plan: Dict[str, Dict[str, int]] = {}
        # set by _restore_snapshot when an approved plan needs regrouping
        self._regroup_needed = False
        # adaptive replan budget: at most 2 mid-run plan adjustments
        self.replan_count = 0
        # pending mid-run replan hints set by agent loops
        self._pending_handoff: Optional[str] = None
        self._tool_failed_this_group = False
        self._missing_artifacts: List[str] = []
        self.plan_meta: Dict[str, Any] = {}
        self.revision_reuse: Dict[str, Any] = {}
        # conflict arbitration runs exactly once (ruling is final)
        self._arbitrated = False
        # populated by _restore_snapshot on resume
        self.plan: List[str] = []
        self.parallel_groups: List[List[str]] = []
        self.next_group_index = 0
        self.executed: List[str] = []
        # Attach probed MCP catalog (best-effort; empty catalog if probe fails).
        try:
            from app.runtime.mcp_registry import get_mcp_registry_sync, get_mcp_registry
            registry = get_mcp_registry_sync()
            if registry is not None:
                self.tools.attach_mcp(registry)
                self._mcp_attach_pending = bool(registry.needs_probe())
            else:
                # Lazy probe will run on first execute; schedule soft attach.
                self._mcp_attach_pending = True
        except Exception as exc:  # noqa: BLE001
            logger.info("MCP attach deferred: %s", exc)
            self._mcp_attach_pending = True

    # ------------------------------------------------------------------

    @staticmethod
    def _memory_retrieval_query(
            request: AgentRunRequest) -> Tuple[str, List[str]]:
        """Build a bounded, evidence-bearing recall query.

        Upload endpoints often provide a generic user message.  Querying only
        that text makes relevant semantic/procedural memory effectively
        unreachable, so include bounded JD/resume evidence without persisting
        or logging the raw query.
        """
        parts = [
            ("user_message", request.userMessage or ""),
            ("current_goal", request.currentGoal or ""),
            ("job_description", (request.jobDescription or "")[:1200]),
            ("resume", (request.resumeText or "")[:1800]),
        ]
        query = "\n".join(
            text.strip() for _, text in parts if text and text.strip()
        ) or request.runType
        basis = [name for name, text in parts if text and text.strip()]
        return query, basis

    @staticmethod
    def _procedural_memory_query(request: AgentRunRequest) -> str:
        """Small candidate-free query for reusable, observed run strategies."""
        text = " ".join((
            request.runType or "",
            request.userMessage or "",
            request.currentGoal or "",
            request.resumeText or "",
        )).lower()
        cues = ["简历评估", "执行策略", request.runType or "full_evaluation"]
        if any(token in text for token in (
                "项目", "github", "开源", "project", "作品集")):
            cues.extend(["项目", "证据核验"])
        if any(token in text for token in (
                "空档", "风险", "离职", "gap", "时间线")):
            cues.extend(["风险", "时间线"])
        if request.jobDescription:
            cues.extend(["JD", "技术匹配"])
        return " ".join(dict.fromkeys(cues))

    @staticmethod
    def _merge_memory_hits(
            procedures: List[Dict[str, Any]],
            candidate_hits: List[Dict[str, Any]],
            *,
            limit: int,
    ) -> List[Dict[str, Any]]:
        """Keep a real procedure hit visible without duplicating memory rows."""
        merged: List[Dict[str, Any]] = []
        seen = set()
        for hit in list(procedures) + list(candidate_hits):
            memory_id = str(hit.get("memoryId") or "")
            identity = memory_id or (
                str(hit.get("type") or ""),
                str(hit.get("source") or ""),
                str(hit.get("content") or ""),
            )
            if identity in seen:
                continue
            seen.add(identity)
            merged.append(hit)
            if len(merged) >= max(1, int(limit)):
                break
        return merged

    async def execute(self) -> Dict[str, Any]:
        started = time.monotonic()
        try:
            if self._requires_score_contract():
                resume = (self.request.resumeText or "").strip()
                if len(resume) < MIN_RESUME_TEXT_CHARS:
                    return self._result(
                        "FAILED", "",
                        error_code="RESUME_TEXT_INSUFFICIENT",
                        error_message=(
                            f"简历文本过短（{len(resume)} 字），无法生成可靠评分与报告"))
            result = await asyncio.wait_for(
                self._execute_inner(),
                timeout=self.policy.timeoutPolicy.runTimeoutSeconds)
            return result
        except RunPaused as paused:
            return self._result("PAUSED", "", snapshot=paused.snapshot)
        except asyncio.TimeoutError:
            self.degraded_reasons.append("run_timeout")
            answer = self._degraded_answer("运行超时")
            return self._result("TIMED_OUT", answer, error_code="RUN_TIMEOUT",
                                error_message="运行超出策略时限")
        except asyncio.CancelledError:
            raise
        except BudgetExceeded as exc:
            self.degraded_reasons.append(f"budget:{exc.kind}")
            answer = self._degraded_answer(f"预算耗尽（{exc.kind}）")
            return self._result("PARTIAL_SUCCESS" if answer else "FAILED", answer,
                                error_code="BUDGET_EXCEEDED", error_message=str(exc))
        except LlmError as exc:
            return self._result("FAILED", "", error_code=exc.code,
                                error_message=str(exc))
        except Exception as exc:  # noqa: BLE001 - top-level run boundary
            logger.exception("run executor crashed run=%s", self.request.runId)
            return self._result("FAILED", "", error_code="RUNTIME_ERROR",
                                error_message=str(exc)[:800])
        finally:
            logger.info("run %s finished in %.1fs llm=%d tools=%d",
                        self.request.runId, time.monotonic() - started,
                        self.budget.llm_calls, self.budget.tool_calls)

    async def _execute_inner(self) -> Dict[str, Any]:
        request = self.request
        resumed = self._restore_snapshot(request.resumeSnapshot)

        if not resumed:
            await self.emitter.emit("run.progress", payload={
                "stage": "observe", "message": "加载记忆与上下文"})
            self.revision_reuse = self._reuse_previous_revision_artifacts()
            if self.revision_reuse:
                await self.emitter.emit("run.progress", payload={
                    "stage": "revision_reuse",
                    "message": (
                        f"revision #{request.revision} 复用 "
                        f"{len(self.revision_reuse['reusedArtifacts'])} 个旧产物，"
                        f"失效 {len(self.revision_reuse['invalidatedArtifacts'])} 个"),
                    **self.revision_reuse,
                })
            # Candidate facts/episodes and reusable execution procedures use
            # separate queries.  A focused procedural query prevents a long raw
            # resume from diluting the lexical signal, while the merge remains
            # bounded and contains only records returned by the durable store.
            memory_query, memory_query_basis = self._memory_retrieval_query(
                request)
            procedure_hits = await self.memory.search(
                self._procedural_memory_query(request),
                types=["PROCEDURAL"],
                top_k=min(2, self.policy.memoryRetrieval.topK),
                min_confidence=self.policy.memoryRetrieval.minConfidence,
                consumer_agent="SpecialistAgent")
            candidate_hits = await self.memory.search(
                memory_query, types=["SEMANTIC", "EPISODIC"],
                top_k=self.policy.memoryRetrieval.topK,
                min_confidence=self.policy.memoryRetrieval.minConfidence,
                consumer_agent="SpecialistAgent")
            self.memory_hits = self._merge_memory_hits(
                procedure_hits, candidate_hits,
                limit=self.policy.memoryRetrieval.topK)
            # FAILURE is Coordinator / policy-evolution only.
            self.failure_hits = await self.memory.search(
                memory_query, types=["FAILURE"],
                top_k=3,
                min_confidence=self.policy.memoryRetrieval.minConfidence,
                consumer_agent="CoordinatorAgent")
            self.failure_notes = [
                str(h.get("content", ""))[:160] for h in self.failure_hits][:3]
            # Memory retrieval must be observable in the trace, not a black box.
            type_counts: Dict[str, int] = {}
            for hit in self.memory_hits + self.failure_hits:
                hit_type = str(hit.get("type") or "UNKNOWN")
                type_counts[hit_type] = type_counts.get(hit_type, 0) + 1
            observe_trace = memory_trace_entries(
                [{"used": True, "ignoredReason": None, **h} for h in self.memory_hits],
                [],
                "SpecialistAgent",
            ) + memory_trace_entries(
                [{"used": True, "ignoredReason": None, **h} for h in self.failure_hits],
                [],
                "CoordinatorAgent",
            )
            self.memory_traces.extend(observe_trace)
            await self.emitter.emit("run.progress", payload={
                "stage": "memory",
                "message": (f"记忆命中 {len(self.memory_hits)} 条"
                            f"（FAILURE 仅 Coordinator {len(self.failure_hits)} 条）"),
                "memoryHits": len(self.memory_hits),
                "failureHits": len(self.failure_hits),
                "queryBasis": memory_query_basis + ["runtime_strategy"],
                "memoryTypeCounts": type_counts,
                "memoryTrace": observe_trace[:12],
                "memoryTop": [
                    {"memoryId": h.get("memoryId"),
                     "type": str(h.get("type") or ""),
                     "scope": h.get("ownerScope") or h.get("scope"),
                     "source": h.get("source"),
                     "consumerAgent": "SpecialistAgent",
                     "used": True,
                     "ignoredReason": None,
                     "confidence": h.get("confidence"),
                     "content": str(h.get("content") or "")[:120]}
                    for h in self.memory_hits[:3]
                ]})

            if getattr(self, "_mcp_attach_pending", False):
                try:
                    from app.runtime.mcp_registry import get_mcp_registry
                    registry = await get_mcp_registry(probe=True)
                    self.tools.attach_mcp(registry)
                except Exception as exc:  # noqa: BLE001
                    logger.info("MCP probe skipped: %s", exc)
                self._mcp_attach_pending = False

            # Deterministic preflight: parse resume / load JD into the
            # canonical artifact store BEFORE artifact backward-chaining.
            await self._prepare_context()
            coordinator = Coordinator(self.registry, self.policy, self.llm)
            arts = self.state.artifacts()
            needs_parse = bool(request.resumeText) and not arts.get("resumeFacts") \
                and request.runType in ("full_evaluation", "jd_evaluation",
                                        "backend_eval", "agent_eval",
                                        "resume_optimize", "project_rewrite")
            execution_profiles = [
                h for h in self.memory_hits
                if isinstance(h, dict)
                and h.get("source") in {"runtime_strategy", "execution_profile"}
            ]
            planned = await coordinator.plan(
                run_type=request.runType, user_message=request.userMessage,
                conversation_summary=request.conversationSummary or "",
                shared_digest=self.state.view_for("CoordinatorAgent", max_chars=2000),
                failure_notes=self.failure_notes,
                memory_notes=execution_profiles or [
                    str(h.get("content", ""))[:120] for h in self.memory_hits[:3]],
                needs_parse=needs_parse,
                resume_text=request.resumeText or "",
                job_description=request.jobDescription or "",
                artifacts=arts,
                shared=self.state.data)
            self.plan = planned["plan"]
            self.parallel_groups = planned["parallelGroups"]
            self.budget_plan = planned.get("budgetPlan") or planned.get("budget") or {}
            self.plan_meta = {
                "selectedBecause": planned.get("selectedBecause") or {},
                "skippedBecause": planned.get("skippedBecause") or {},
                "artifactEdges": planned.get("artifactEdges") or [],
                "goalArtifacts": planned.get("goalArtifacts") or [],
                "optionalArtifacts": planned.get("optionalArtifacts") or [],
                "presentArtifacts": planned.get("presentArtifacts") or [],
                "revisionReuse": dict(self.revision_reuse),
                "budget": self.budget_plan,
            }
            await self.emitter.emit("agent.selected", agent_id="CoordinatorAgent", payload={
                "plan": self.plan, "reason": planned["reason"],
                "parallelGroups": self.parallel_groups,
                "requiredTerminalAgent": planned["requiredTerminalAgent"],
                "policyId": self.policy.policyId,
                "budgetPlan": self.budget_plan,
                "budget": self.budget_plan,
                "selectedBecause": self.plan_meta["selectedBecause"],
                "skippedBecause": self.plan_meta["skippedBecause"],
                "artifactEdges": self.plan_meta["artifactEdges"],
                "goalArtifacts": self.plan_meta["goalArtifacts"],
                "presentArtifacts": self.plan_meta["presentArtifacts"],
                "revisionReuse": dict(self.revision_reuse),
                "llmBudget": self.budget.llm_audit(
                    self.policy.maxLlmCalls),
                "planMode": request.planMode,
                "memoryHits": len(self.memory_hits),
                "memoryNotes": [str(h.get("content", ""))[:120]
                                for h in self.memory_hits[:3]]})
            self.state.set_pending(list(self.plan))
            if request.planMode:
                # Plan-approval gate: pause before any specialist burns budget.
                # RESUME carries the (possibly user-edited) plan back in.
                snapshot = self.export_snapshot()
                snapshot["pauseReason"] = "AWAITING_PLAN_APPROVAL"
                raise RunPaused(snapshot)
        else:
            coordinator = Coordinator(self.registry, self.policy, self.llm)
            if self._regroup_needed:
                # Plan approval edited the pipeline: recompute dependency
                # ordering, parallel groups and budget from the approved plan.
                refreshed = coordinator._finalize(self.plan, "user_approved_plan")
                self.plan = refreshed["plan"]
                self.parallel_groups = refreshed["parallelGroups"]
                self.budget_plan = refreshed.get("budgetPlan") or {}
                self.next_group_index = 0
                await self.emitter.emit("agent.selected", agent_id="CoordinatorAgent", payload={
                    "plan": self.plan, "reason": "用户确认/编辑后的计划",
                    "parallelGroups": self.parallel_groups,
                    "requiredTerminalAgent": refreshed["requiredTerminalAgent"],
                    "policyId": self.policy.policyId,
                    "budgetPlan": self.budget_plan,
                    "approved": True})
                self.state.set_pending(list(self.plan))
            await self.emitter.emit("run.progress", payload={
                "stage": "resume",
                "message": f"从快照恢复：已完成 {len(self.executed)} 个 Agent",
                "executedAgents": self.executed})

        consecutive_failures = 0
        while self.next_group_index < len(self.parallel_groups):
            self._pause_boundary()
            group = [a for a in self.parallel_groups[self.next_group_index]
                     if a not in self.executed]
            self.next_group_index += 1
            if not group:
                continue
            if len(self.executed) >= self.policy.maxAgentCount \
                    and not any(a in TERMINAL_AGENTS for a in group):
                self.degraded_reasons.append("max_agent_count")
                continue

            runnable: List[AgentDefinition] = []
            for agent_id in group:
                guard = self.guard.check_agent_start(agent_id)
                if guard.triggered:
                    await self._emit_guard(guard, agent_id)
                    continue
                try:
                    runnable.append(self.registry.get(agent_id))
                except KeyError:
                    continue
            if not runnable:
                continue

            conflicts_before = len(self.state.artifact("conflicts") or [])
            self._tool_failed_this_group = False
            self._pending_handoff = None
            self._missing_artifacts = []
            if len(runnable) == 1:
                ok = await self._run_single(runnable[0], coordinator)
                consecutive_failures = 0 if ok else consecutive_failures + 1
            else:
                ok = await self._run_parallel(runnable)
                consecutive_failures = 0 if ok else consecutive_failures + 1

            if consecutive_failures >= 2:
                self.degraded_reasons.append("consecutive_failures")
                self._ensure_terminal_tail()

            # Debate-style conflict arbitration (single round, final): after
            # EvidenceAgent flagged conflicts, one LLM call adjudicates each
            # claim as keep/reject/uncertain — no ping-pong re-litigation.
            if any(d.agent_id == "EvidenceAgent" for d in runnable):
                await self._arbitrate_conflicts()

            # Bounded mid-run dynamism: missing artifact / tool failure /
            # new conflict / handoff / group failure / low confidence.
            await self._maybe_replan(coordinator, ok, conflicts_before)

            # Group-boundary checkpoint (fire-and-forget): a later FAILED /
            # TIMED_OUT run can be retried from here, skipping finished work.
            asyncio.ensure_future(
                self.emitter.save_checkpoint(self.export_snapshot()))

        if not self.final_answer:
            self.degraded_reasons.append("no_terminal_answer")
            self.final_answer = self._degraded_answer("报告 Agent 未能完成")
            self.report_agent_failed = True
        summary = self._conversation_summary()
        await self._write_memories(summary)
        missing_goals = self._missing_required_goal_artifacts()
        status = "PARTIAL_SUCCESS" if (self.report_agent_failed or
                                       self._has_hard_degradation() or
                                       missing_goals) else "SUCCEEDED"
        error_code = None
        error_message = None
        if missing_goals:
            self.degraded_reasons.append("missing_goal_artifacts")
            error_code = "MISSING_GOAL_ARTIFACTS"
            error_message = "缺少必选 goal artifacts: " + ", ".join(missing_goals)
        if status == "SUCCEEDED" and self._requires_score_contract():
            contract_error = self._report_contract_violation()
            if contract_error:
                status = "PARTIAL_SUCCESS"
                error_code = "REPORT_CONTRACT_FAILED"
                error_message = contract_error
                self.degraded_reasons.append("report_contract_failed")
        return self._result(status, self.final_answer,
                            error_code=error_code, error_message=error_message,
                            conversation_summary=summary,
                            missing_goal_artifacts=missing_goals)

    # ------------------------------------------------------------------
    # group execution
    # ------------------------------------------------------------------

    async def _run_single(self, definition: AgentDefinition,
                          coordinator: Coordinator) -> bool:
        agent_id = definition.agent_id
        await self.emitter.emit("agent.started", agent_id=agent_id, payload={
            "description": definition.description,
            "position": len(self.executed) + 1, "planned": len(self.plan)})
        agent_started = time.monotonic()
        try:
            output = await asyncio.wait_for(
                self._run_agent(definition), timeout=definition.timeout_seconds)
            conflicts = self.state.apply_output(output)
            self._after_agent_success(definition, output, conflicts, agent_started)
            return True
        except asyncio.CancelledError:
            raise
        except BudgetExceeded as exc:
            if (definition.agent_id not in TERMINAL_AGENTS
                    and exc.kind in {"llmReservation", "llmScopeLimit"}):
                await self._after_agent_failure(
                    definition, exc, agent_started)
                return False
            raise
        except Exception as exc:  # noqa: BLE001 - agent failure boundary
            await self._after_agent_failure(definition, exc, agent_started)
            return False

    async def _run_parallel(self, definitions: List[AgentDefinition]) -> bool:
        """Specialists run sequentially to avoid resource contention on
        constrained infrastructure. Outputs are merged after each agent."""
        any_success = False
        for definition in definitions:
            await self.emitter.emit("agent.started", agent_id=definition.agent_id,
                                    payload={"description": definition.description,
                                             "parallelGroup": [d.agent_id for d in definitions],
                                             "position": len(self.executed) + 1,
                                             "planned": len(self.plan)})
            agent_start = time.monotonic()
            try:
                output = await asyncio.wait_for(
                    self._run_agent(definition), timeout=definition.timeout_seconds)
                if isinstance(output, AgentOutput):
                    conflicts = self.state.apply_output(output)
                    self._after_agent_success(definition, output, conflicts,
                                              agent_start, fire_started=False)
                    any_success = True
            except asyncio.CancelledError:
                raise
            except BudgetExceeded as exc:
                if (definition.agent_id not in TERMINAL_AGENTS
                        and exc.kind in {
                            "llmReservation", "llmScopeLimit"}):
                    await self._after_agent_failure(
                        definition, exc, agent_start)
                    continue
                raise
            except Exception as exc:  # noqa: BLE001
                await self._after_agent_failure(definition, exc, agent_start)
        return any_success

    def _after_agent_success(self, definition: AgentDefinition, output: AgentOutput,
                             conflicts: List[str], agent_started: float,
                             fire_started: bool = True) -> None:
        agent_id = definition.agent_id
        self.state.complete_task(agent_id)
        self.guard.record_completed_agent(agent_id)
        self.executed.append(agent_id)
        duration_ms = int((time.monotonic() - agent_started) * 1000)
        self.agent_timings[agent_id] = self.agent_timings.get(agent_id, 0) + duration_ms
        counters = self.agent_counters.get(agent_id, {})
        asyncio.ensure_future(self.emitter.emit(
            "agent.completed", agent_id=agent_id, payload={
                "iterations": counters.get("iterations", 1),
                "llmCalls": counters.get("llmCalls", 0),
                "toolCalls": counters.get("toolCalls", 0),
                "confidence": output.confidence,
                "summary": output.summary[:300],
                "conflicts": conflicts,
                "durationMs": duration_ms,
                "output": {"type": output.type, "claims": len(output.claims),
                           "evidence": len(output.evidence)}}))
        if output.requestedNextAction:
            requested = output.requestedNextAction
            delegation = self.guard.check_delegation(agent_id, requested)
            flat_remaining = [a for g in self.parallel_groups[self.next_group_index:]
                              for a in g]
            if not delegation.triggered and self.registry.known(requested) \
                    and requested not in flat_remaining \
                    and requested not in self.executed \
                    and len(self.executed) + len(flat_remaining) < self.policy.maxAgentCount + 2:
                self.parallel_groups.insert(self.next_group_index, [requested])
                self.plan.append(requested)

    async def _after_agent_failure(self, definition: AgentDefinition, exc: Exception,
                                   agent_started: float) -> None:
        agent_id = definition.agent_id
        error_text = f"{type(exc).__name__}: {exc}"
        self.failure_notes.append(f"{agent_id} 失败 {error_text[:120]}")
        duration_ms = int((time.monotonic() - agent_started) * 1000)
        self.agent_timings[agent_id] = self.agent_timings.get(agent_id, 0) + duration_ms
        await self.emitter.emit("agent.failed", agent_id=agent_id, payload={
            "error": error_text[:300], "durationMs": duration_ms})
        self.guard.check_error(error_text)
        self.degraded_reasons.append(f"{agent_id}_failed")
        self.executed.append(agent_id)
        if agent_id in TERMINAL_AGENTS:
            self.report_agent_failed = True
            return
        self._ensure_terminal_tail()

    def _ensure_terminal_tail(self) -> None:
        if self.report_agent_failed:
            return
        remaining = [a for g in self.parallel_groups[self.next_group_index:] for a in g]
        if not any(a in TERMINAL_AGENTS for a in remaining):
            self.parallel_groups.append(["ReportAgent"])
            if "ReportAgent" not in self.plan:
                self.plan.append("ReportAgent")

    async def _arbitrate_conflicts(self) -> None:
        """One-round conflict adjudication: unresolved conflicts get a
        keep / reject / uncertain ruling with a reason. The ruling is final
        (written back onto the conflict), the ReportAgent cites it."""
        conflicts = [c for c in (self.state.artifact("conflicts") or [])
                     if isinstance(c, dict) and not c.get("resolution")]
        if not conflicts or self._arbitrated:
            return
        self._arbitrated = True
        items = [{"claim": str(c.get("claim", c.get("key", "")))[:200],
                  "reason": str(c.get("reason", c.get("type", "")))[:200]}
                 for c in conflicts[:6]]
        prompt_user = (
            "以下是评估过程中被证据核验标记的冲突结论。请逐条裁决，输出 json：\n"
            "{\"rulings\": [{\"claim\": \"...\", \"verdict\": \"keep|reject|uncertain\","
            " \"reason\": \"一句依据\"}]}\n"
            "裁决标准：简历原文/工具结果能支撑=keep；明确矛盾或无来源=reject；"
            "证据不足以定夺=uncertain（报告中如实标注）。\n"
            f"冲突列表: {json.dumps(items, ensure_ascii=False)}")
        try:
            raw = await self.llm.chat(
                [{"role": "system",
                  "content": "你是评估冲突仲裁者，只依据给定材料裁决，不新增事实。"},
                 {"role": "user", "content": prompt_user}],
                agent_id="EvidenceAgent", purpose="arbitration", max_tokens=600)
            parsed = extract_json_object(raw)
            rulings = {str(r.get("claim", ""))[:200]: r
                       for r in parsed.get("rulings", []) if isinstance(r, dict)}
            resolved = 0
            for conflict in conflicts:
                key = str(conflict.get("claim", conflict.get("key", "")))[:200]
                ruling = rulings.get(key)
                if ruling and ruling.get("verdict") in ("keep", "reject", "uncertain"):
                    conflict["resolution"] = ruling["verdict"]
                    conflict["resolutionReason"] = str(ruling.get("reason", ""))[:200]
                    resolved += 1
            await self.emitter.emit("run.progress", payload={
                "stage": "arbitration",
                "message": f"冲突仲裁完成：{resolved}/{len(conflicts)} 条已裁决",
                "resolved": resolved, "total": len(conflicts)})
        except Exception as exc:  # noqa: BLE001 - arbitration is best-effort
            logger.info("conflict arbitration skipped: %s", exc)

    async def _maybe_replan(self, coordinator: Coordinator, group_ok: bool,
                            conflicts_before: int) -> None:
        """Adaptive replan trigger after each group. Bounded: <=2 per run.
        Triggers: missing_required_artifact / tool_failed / new_conflict /
        handoff_requested / group_failure / low_confidence."""
        if coordinator.is_simple(self.request.runType):
            return
        remaining = [a for g in self.parallel_groups[self.next_group_index:] for a in g]
        non_terminal_remaining = [a for a in remaining if a not in TERMINAL_AGENTS]
        if self.replan_count >= 2 or not non_terminal_remaining:
            return
        new_conflicts = len(self.state.artifact("conflicts") or []) - conflicts_before
        recent_outputs = self.state.data["agentOutputs"][-3:]
        confidences = [float(o.get("confidence", 1.0)) for o in recent_outputs
                       if isinstance(o, dict)]
        avg_confidence = sum(confidences) / len(confidences) if confidences else 1.0

        # Detect missing required artifacts for remaining agents.
        missing: List[str] = list(self._missing_artifacts)
        present = set()
        arts = self.state.artifacts()
        if arts.get("parsedResume") or arts.get("resumeFacts"):
            present.add("resume_facts")
        if arts.get("effectiveJd") or arts.get("jdRequirements"):
            present.add("jd_requirements")
        if arts.get("technicalFindings"):
            present.add("technical_findings")
        if arts.get("projectFindings"):
            present.add("project_findings")
        if arts.get("risks") or arts.get("timelineCheck"):
            present.add("risks")
        if arts.get("evidence"):
            present.add("evidence_ledger")
        for agent_id in remaining:
            if not self.registry.known(agent_id):
                continue
            try:
                definition = self.registry.get(agent_id)
            except KeyError:
                continue
            for req in definition.requires_artifacts:
                if req not in present and req not in missing:
                    missing.append(req)

        trigger = None
        handoff_to = self._pending_handoff
        if handoff_to:
            # Handoff 去环：目标已执行则忽略。
            if handoff_to in self.executed:
                handoff_to = None
            else:
                trigger = f"handoff_requested:{handoff_to}"
        elif missing:
            trigger = "missing_required_artifact"
        elif self._tool_failed_this_group:
            trigger = "tool_failed"
        elif not group_ok:
            trigger = "group_failure"
        elif new_conflicts > 0:
            trigger = f"new_conflict:{new_conflicts}"
        elif avg_confidence < REPLAN_CONFIDENCE_THRESHOLD:
            trigger = f"low_confidence:{avg_confidence:.2f}"
        if trigger is None:
            return
        arts = self.state.artifacts()
        shared_digest = json.dumps({
            "technicalFindings": len(arts.get("technicalFindings") or []),
            "projectFindings": len(arts.get("projectFindings") or []),
            "risks": len(arts.get("risks") or []),
            "conflicts": [str(c.get("claim", c.get("key", "")))[:80]
                          for c in (arts.get("conflicts") or [])[-3:]
                          if isinstance(c, dict)],
            "missingArtifacts": missing[:6],
            "inputPresence": arts.get("inputPresence") or {},
        }, ensure_ascii=False)
        adjusted = await coordinator.adaptive_replan(
            remaining=remaining, executed=self.executed,
            shared_digest=shared_digest, trigger=trigger,
            failure_notes=self.failure_notes,
            missing_artifacts=missing,
            handoff_to=handoff_to)
        if adjusted is None:
            return
        self.replan_count += 1
        self._pending_handoff = None
        self._tool_failed_this_group = False
        self._missing_artifacts = []
        self.parallel_groups = self.parallel_groups[: self.next_group_index] \
            + adjusted["parallelGroups"]
        self.plan = self.executed + adjusted["plan"]
        for agent_id, quota in (adjusted.get("budgetPlan") or {}).items():
            self.budget_plan[agent_id] = quota
        self.plan_meta = {
            "selectedBecause": adjusted.get("selectedBecause") or {},
            "skippedBecause": adjusted.get("skippedBecause") or {},
            "artifactEdges": adjusted.get("artifactEdges") or [],
            "goalArtifacts": adjusted.get("goalArtifacts") or missing,
            "budget": self.budget_plan,
        }
        await self.emitter.emit("agent.selected", agent_id="CoordinatorAgent", payload={
            "plan": self.plan, "reason": adjusted["reason"],
            "parallelGroups": self.parallel_groups,
            "requiredTerminalAgent": adjusted["requiredTerminalAgent"],
            "policyId": self.policy.policyId,
            "budgetPlan": self.budget_plan,
            "budget": self.budget_plan,
            "selectedBecause": self.plan_meta["selectedBecause"],
            "skippedBecause": self.plan_meta["skippedBecause"],
            "artifactEdges": self.plan_meta["artifactEdges"],
            "replanned": True, "trigger": trigger,
            "replanCount": self.replan_count,
            "llmBudget": self.budget.llm_audit(
                self.policy.maxLlmCalls)})

    def _has_hard_degradation(self) -> bool:
        hard = {"run_timeout", "consecutive_failures", "no_terminal_answer",
                "report_contract_failed"}
        terminal_fail = {f"{a}_failed" for a in TERMINAL_AGENTS}
        for r in self.degraded_reasons:
            if r in hard or r in terminal_fail or r.endswith("_failed"):
                return True
        return False

    def _missing_required_goal_artifacts(self) -> List[str]:
        """Run-end closure: required goal artifacts absent → never silent SUCCEEDED.
        
        An artifact is NOT considered missing if its designated producer Agent
        was executed (even if output was empty) — this avoids false PARTIAL_SUCCESS
        when e.g. EvidenceAgent fast-path finds no conflicts."""
        goals = list((self.plan_meta or {}).get("goalArtifacts") or [])
        if not goals:
            return []
        present = Coordinator._present_artifacts(
            self.state.artifacts() if hasattr(self.state, "artifacts") else
            self.state.data.get("artifacts") or {},
            self.state.data if isinstance(self.state.data, dict) else {})
        # Agents that ran count their artifacts as "attempted" even if empty.
        _AGENT_PRODUCES = {
            "ProjectAgent": {"project_findings"},
            "EvidenceAgent": {"evidence_ledger"},
            "TechAgent": {"technical_findings"},
            "RiskAgent": {"risks"},
            "JDAnalysisAgent": {"jd_requirements"},
            "ResumeParserAgent": {"resume_facts", "parsed_resume"},
            "ReportAgent": {"final_report"},
        }
        attempted = set()
        for agent_id in self.executed:
            attempted.update(_AGENT_PRODUCES.get(agent_id, set()))
        return [g for g in goals if g not in present and g not in attempted]

    def _requires_score_contract(self) -> bool:
        return self.request.runType not in _SCORE_CONTRACT_SKIP_TYPES
    def _report_contract_violation(self) -> Optional[str]:
        """SUCCEEDED requires non-empty structuredReport with meaningful content
        and a rendered answer that meets the minimum length contract."""
        report = self.state.data.get("artifacts", {}).get("finalReport")
        if not isinstance(report, dict) or not report:
            return "structuredReport 为空"
        # If overallScore is missing but we have dimensions with scores,
        # compute a fallback average so the report isn't rejected.
        if not isinstance(report.get("overallScore"), int):
            dims = report.get("dimensions") or []
            scored = [d for d in dims if isinstance(d, dict)
                      and isinstance(d.get("score"), int)]
            if scored:
                avg = int(round(sum(d["score"] for d in scored) / len(scored)))
                report["overallScore"] = avg
                self.final_answer = self.render_report(report)
            else:
                quality = str(report.get("dataQuality") or "").upper()
                if quality == "INSUFFICIENT":
                    return "简历证据不足，无法给出数值分"
                return "overallScore 为空且无可用维度分数"
        if len(self.final_answer or "") < MIN_REPORT_ANSWER_CHARS:
            return f"报告正文过短（<{MIN_REPORT_ANSWER_CHARS} 字）"
        return None

    # ------------------------------------------------------------------
    # pause / resume
    # ------------------------------------------------------------------

    def _reuse_previous_revision_artifacts(self) -> Dict[str, Any]:
        """Import only non-invalidated artifacts into a fresh revision.

        This is intentionally not checkpoint resume: budgets, executed agents,
        tool ledgers, and loop-guard state always start clean.
        """
        request = self.request
        snapshot = request.previousSnapshot or {}
        shared = snapshot.get("sharedState") if isinstance(snapshot, dict) else {}
        if not isinstance(shared, dict):
            shared = {}
        previous = shared.get("artifacts")
        if not isinstance(previous, dict) and isinstance(snapshot, dict):
            previous = snapshot.get("artifacts")
        previous = dict(previous) if isinstance(previous, dict) else {}
        if request.previousArtifacts:
            previous.update(request.previousArtifacts)
        if not previous:
            return {}

        reusable, expanded = Coordinator.reusable_artifacts(
            copy.deepcopy(previous), request.invalidatedArtifacts)
        if reusable:
            self.state.apply_artifacts(reusable, by_agent="previous_revision")
        return {
            "sourceRevision": max(1, int(request.revision or 1) - 1),
            "targetRevision": int(request.revision or 1),
            "requestedInvalidations": list(request.invalidatedArtifacts),
            "invalidatedArtifacts": sorted(expanded),
            "reusedArtifacts": sorted(reusable.keys()),
        }

    def _pause_boundary(self) -> None:
        """Safe pause point between agent groups: everything committed so far
        is durable in the snapshot; nothing mid-flight is frozen."""
        if self.pause_event is not None and self.pause_event.is_set():
            raise RunPaused(self.export_snapshot())

    def export_snapshot(self) -> Dict[str, Any]:
        return {
            "runId": self.request.runId,
            "plan": list(self.plan),
            "parallelGroups": [list(g) for g in self.parallel_groups],
            "budgetPlan": dict(self.budget_plan),
            "nextPlanIndex": self.next_group_index,
            "executedAgents": list(self.executed),
            "sharedState": self.state.snapshot(),
            "budget": self.budget.snapshot(),
            "loopGuardState": self.guard.export_state(),
            "contextSummary": self.request.conversationSummary or "",
            "recentMessages": self.request.recentMessages[-8:],
            "toolCallLedger": self.tools.ledger(),
            "promptVersions": default_prompt_manager.versions_used(
                list(dict.fromkeys(self.executed + ["CoordinatorAgent"])),
                self.policy.promptVersions),
            "skillVersions": default_skill_manager.versions_used(self.skill_selections),
            "policyId": self.policy.policyId,
            "planMeta": dict(self.plan_meta or {}),
            "revisionReuse": dict(self.revision_reuse),
            "finalAnswer": self.final_answer,
            "degradedReasons": list(self.degraded_reasons),
            "agentTimings": dict(self.agent_timings),
            "failureNotes": list(self.failure_notes)[-5:],
            "memoryHits": list(self.memory_hits)[:20],
            "failureHits": list(self.failure_hits)[:10],
            "memoryTraces": list(self.memory_traces)[-40:],
            "createdAt": time.time(),
        }

    def _restore_snapshot(self, snapshot: Optional[Dict[str, Any]]) -> bool:
        if not snapshot:
            return False
        try:
            self.plan = [str(a) for a in snapshot.get("plan", [])]
            # A missing parallelGroups means the plan was edited during
            # approval: groups/budget must be recomputed from the new plan.
            self._regroup_needed = "parallelGroups" not in snapshot
            self.parallel_groups = [
                [str(a) for a in group]
                for group in snapshot.get("parallelGroups", [[a] for a in self.plan])]
            budget_plan = snapshot.get("budgetPlan")
            if isinstance(budget_plan, dict):
                self.budget_plan = {str(k): dict(v) for k, v in budget_plan.items()
                                    if isinstance(v, dict)}
            self.next_group_index = int(snapshot.get("nextPlanIndex", 0))
            self.executed = [str(a) for a in snapshot.get("executedAgents", [])]
            self.state.restore(snapshot.get("sharedState") or {})
            self.budget.restore(snapshot.get("budget") or {})
            self.guard.restore_state(snapshot.get("loopGuardState") or {})
            self.tools.restore_ledger(snapshot.get("toolCallLedger") or [])
            self.final_answer = str(snapshot.get("finalAnswer") or "")
            self.degraded_reasons = list(snapshot.get("degradedReasons") or [])
            self.agent_timings = dict(snapshot.get("agentTimings") or {})
            self.failure_notes = list(snapshot.get("failureNotes") or [])
            self.memory_hits = list(snapshot.get("memoryHits") or [])
            self.failure_hits = list(snapshot.get("failureHits") or [])
            self.memory_traces = list(snapshot.get("memoryTraces") or [])
            plan_meta = snapshot.get("planMeta")
            if isinstance(plan_meta, dict):
                self.plan_meta = dict(plan_meta)
            self.revision_reuse = dict(snapshot.get("revisionReuse") or {})
            for agent_id in self.executed:
                self.guard.record_completed_agent(agent_id)
            return True
        except Exception as exc:  # noqa: BLE001 - a bad snapshot must not brick the run
            logger.warning("snapshot restore failed, starting fresh: %s", exc)
            return False

    # ------------------------------------------------------------------
    # single agent execution (unchanged core loop, per-agent budget)
    # ------------------------------------------------------------------

    def _agent_quota(self, agent_id: str, key: str, fallback: int) -> int:
        """Plan-time quota for one agent (llmQuota/toolQuota); falls back to
        the static policy limit when the coordinator did not allocate one."""
        quota = self.budget_plan.get(agent_id) or {}
        value = quota.get(key)
        return int(value) if isinstance(value, (int, float)) and value >= 0 else fallback

    async def _run_agent(self, definition: AgentDefinition) -> AgentOutput:
        request = self.request
        agent_id = definition.agent_id
        if agent_id in TERMINAL_AGENTS:
            # No control-plane work is legal after the terminal stage starts.
            # Release unused plan/replan/arbitration capacity to the terminal.
            self.budget.release_llm_reservation("control")
        prompt = default_prompt_manager.system_for_agent(
            agent_id, self.policy.promptVersions.get(agent_id))
        signals = Coordinator(self.registry, self.policy, None).inspect_signals(
            resume_text=request.resumeText or "",
            job_description=request.jobDescription or "",
            artifacts=self.state.data.get("artifacts") or {},
            shared=self.state.data)
        sparse_fast_path = bool(
            signals.get("is_sparse_resume")
            and request.runType in ("full_evaluation", "jd_evaluation",
                                    "backend_eval", "agent_eval"))
        skills = default_skill_manager.select_for(
            agent_id=agent_id, run_type=request.runType,
            job_focus=self.policy.jobFocus, overrides=self.policy.skillOverrides,
            signals=signals, user_message=request.userMessage or "")
        if sparse_fast_path:
            # A thin resume has no evidence surface for Skills/MCP exploration.
            # Keep one direct model decision per selected specialist/terminal;
            # this avoids a skill-loading action turn plus a slow repair turn.
            skills = []
        # Build the requested surface now, but do not expose Skill/MCP metadata
        # until we know this agent will actually execute an LLM turn. This
        # prevents deterministic fast paths from producing phantom standalone
        # Skill/MCP rows in the trace.
        requested_tools = list(definition.tools)
        if agent_id == "ReportAgent" and self.request.runType not in (
                "followup", "quick_answer"):
            requested_tools = [
                tool for tool in requested_tools
                if tool not in {"knowledge_search", "resume_semantic_search"}
            ]
        if sparse_fast_path:
            requested_tools = []
        if skills:
            requested_tools.extend(["load_skill", "read_skill_resource"])

        tool_results_block = ""
        pre_llm_tool_call_ids: List[str] = []
        agent_tool_calls = 0
        agent_llm_calls = 0
        agent_tool_limit = min(
            definition.max_tool_calls,
            self.policy.toolBudget.maxToolCallsPerAgent,
            self._agent_quota(agent_id, "toolQuota",
                              self.policy.toolBudget.maxToolCallsPerAgent))

        # Deterministic parsing / control-plane retrieval may run as harness
        # preflight. Public MCP never appears here: the model alone proposes
        # MCP name and arguments from the live schemas above.
        for tool, args in self._pre_steps(definition):
            if agent_tool_calls >= agent_tool_limit:
                break
            defn = self.tools.definitions.get(tool)
            if defn is not None and defn.kind == "mcp":
                logger.error("ignored invalid hard-coded MCP prestep %s", tool)
                continue
            guard = self.guard.check_tool_call(ToolExecutor.signature(tool, args))
            if guard.triggered:
                await self._emit_guard(guard, agent_id)
                continue
            rewrite = (
                tool in ("knowledge_search", "resume_semantic_search")
                and self.request.runType in ("followup", "quick_answer")
            )
            try:
                call = await self.tools.execute(agent_id, tool, args,
                                                enable_rewrite=rewrite)
            except Exception as _tool_exc:  # noqa: BLE001
                from .tools import ToolCallResult
                call = ToolCallResult(
                    f"tc-err-{agent_tool_calls}", tool, "FAILED", None,
                    error=f"{type(_tool_exc).__name__}: {_tool_exc}"[:200])
            agent_tool_calls += 1
            pre_llm_tool_call_ids.append(call.tool_call_id)
            if call.status == "FAILED":
                self._tool_failed_this_group = True
            tool_results_block += self._format_tool_result(call)
            if call.status == "SUCCEEDED":
                await self._record_tool_success(
                    agent_id, tool, args, call.result,
                    tool_call_id=call.tool_call_id)

        # Performance fast-path: high-quality deterministic parse → skip LLM.
        if definition.agent_id == "ResumeParserAgent":
            fast = self._maybe_skip_parser_llm(tool_results_block)
            if fast is not None:
                self.skill_selections[agent_id] = []
                self.agent_counters[definition.agent_id] = {
                    "iterations": 0, "llmCalls": 0, "toolCalls": agent_tool_calls,
                    "fastPath": 1}
                return fast

        # Performance fast-path: JD short/provided → skip LLM.
        if definition.agent_id == "JDAnalysisAgent":
            fast = self._maybe_skip_jd_llm()
            if fast is not None:
                self.skill_selections[agent_id] = []
                self.agent_counters[definition.agent_id] = {
                    "iterations": 0, "llmCalls": 0, "toolCalls": agent_tool_calls,
                    "fastPath": 1}
                return fast

        # Performance fast-path: Evidence verify clean → skip arbitration LLM.
        if definition.agent_id == "EvidenceAgent":
            fast = self._maybe_skip_evidence_llm(tool_results_block)
            if fast is not None:
                self.skill_selections[agent_id] = []
                self.agent_counters[definition.agent_id] = {
                    "iterations": 0, "llmCalls": 0, "toolCalls": agent_tool_calls,
                    "fastPath": 1}
                return fast

        # From this point onward the model really receives the progressive
        # Skill metadata and live MCP tools/list schemas.
        self.skill_selections[agent_id] = skills
        try:
            await default_skill_manager.emit_catalog(
                self.emitter, agent_id, skills)
            await default_skill_manager.emit_selection(
                self.emitter, agent_id, skills,
                trigger_reason="agent_capability_and_input_signals")
        except Exception as exc:  # noqa: BLE001
            logger.debug("skill catalog/selection emit skipped: %s", exc)

        catalog: List[Dict[str, Any]] = []
        catalog_exposure_ids: Dict[str, str] = {}
        try:
            catalog = self.tools.catalog_for_agent(agent_id, requested_tools)
            for entry in catalog:
                if entry.get("kind") == "mcp" or entry.get("mcpServer"):
                    exposure_id = f"catalog-{uuid.uuid4().hex[:16]}"
                    catalog_exposure_ids[str(entry.get("name") or "")] = exposure_id
                    await self.emitter.emit(
                        "tool.progress", agent_id=agent_id,
                        tool_name=str(entry.get("name") or ""),
                        payload={
                            "toolCallId": exposure_id,
                            "lifecycleStage": "CATALOG_EXPOSED",
                            "catalogScope": "AGENT_MODEL_INPUT",
                            "source": "mcp",
                            "mcpServer": entry.get("mcpServer"),
                            "toolName": entry.get("name"),
                            "modelName": entry.get("modelName"),
                            "description": entry.get("description"),
                            "inputSchema": entry.get("inputSchema"),
                            "occurredAt": _utc_now(),
                        })
        except Exception as exc:  # noqa: BLE001
            logger.debug("live tool catalog unavailable: %s", exc)
            catalog = []
        model_tools, model_tool_aliases = ToolExecutor.openai_tools(catalog)
        allowed_tools = set(model_tool_aliases.values())
        final_tool = (EMIT_REPORT_TOOL if agent_id == "ReportAgent"
                      else EMIT_DECISION_TOOL)
        model_tools.append(final_tool)

        output: Optional[AgentOutput] = None
        # A reasoning iteration and a provider-native action/observation turn
        # are different budget axes.  If they share one counter, a normal
        # progressive flow such as load_skill -> read_resource -> final answer
        # can never finish for agents whose decision budget is one or two.
        # Reserve at most two action follow-up turns normally. An external-URL
        # Project may need three (local evidence -> Skill -> MCP) before its
        # final decision; every call still counts against the run-wide hard
        # LLM/token/cost limits enforced by the client.
        max_decision_iterations = min(
            definition.max_iterations, self.policy.maxIterationsPerAgent)
        if sparse_fast_path:
            max_decision_iterations = 1
        action_turn_ceiling = (
            3 if agent_id == "ProjectAgent"
            and signals.get("has_external_urls") else 2)
        if sparse_fast_path:
            action_turn_ceiling = 0
        available_tool_turns = min(
            action_turn_ceiling,
            max(0, agent_tool_limit - agent_tool_calls))
        fallback_turn_limit = max_decision_iterations + available_tool_turns
        agent_llm_quota = self._agent_quota(
            agent_id, "llmQuota", fallback_turn_limit)
        final_turn_reserve = min(
            2 if agent_id == "ReportAgent" else 1,
            max(1, agent_llm_quota))
        max_action_turns = min(
            available_tool_turns,
            self._agent_quota(
                agent_id, "actionTurnQuota", available_tool_turns),
            max(0, agent_llm_quota - final_turn_reserve))
        max_turns = min(
            fallback_turn_limit, max(0, agent_llm_quota))
        iteration = 0
        decision_iterations = 0
        action_turns = 0
        borrowed_repair_turns = 0
        loaded_skills: Dict[str, Any] = {}
        loaded_skill_call_ids: Dict[str, str] = {}
        applied_skill_ids: set = set()
        native_history: List[Dict[str, Any]] = []
        # Memory selection is immutable for this agent execution. Keeping the
        # audit local avoids cross-agent races when specialists run in parallel;
        # the same block is intentionally attached to each provider prompt.
        memory_block, memory_audit = self._memory_context(definition)
        memory_usage_recorded = False
        while (iteration < max_turns
               and decision_iterations < max_decision_iterations
               and output is None):
            iteration += 1
            round_id = f"{self.request.runId}:{agent_id}:round:{iteration}"
            await self.emitter.emit("agent.progress", agent_id=agent_id, payload={
                "roundId": round_id,
                "iteration": iteration,
                "maxIterations": max_turns,
                "decisionIterations": decision_iterations,
                "decisionIterationLimit": max_decision_iterations,
                "actionTurns": action_turns,
                "actionTurnAllowance": max_action_turns,
            })
            effective_tool_results = tool_results_block
            skill_text = default_skill_manager.render_progressive(
                skills, list(loaded_skills.values()))
            messages = self.context.assemble(
                system_prompt=prompt.content,
                policy_instructions=self._policy_instructions(),
                skill_instructions=skill_text,
                user_request=request.userMessage or "（对当前简历执行你的职责）",
                current_goal=request.currentGoal or "",
                shared_state_digest=self.state.view_for(agent_id),
                recent_messages=request.recentMessages,
                conversation_summary=request.conversationSummary or "",
                memory_block=memory_block,
                tool_results_block=effective_tool_results,
                output_schema=(REPORT_OUTPUT_SCHEMA if agent_id == "ReportAgent"
                               else AGENT_OUTPUT_SCHEMA))
            if native_history:
                messages.extend(native_history)
            if self.context.needs_compaction(messages):
                messages = await self.context.compact(
                    messages, reason="context_over_threshold",
                    protected_markers=["[当前请求]", "[当前目标]", "[输出要求]"],
                    recent_messages=request.recentMessages)
                violations = self.context.consistency_check(
                    messages, user_request=(request.userMessage or "")[:80],
                    current_goal=(request.currentGoal or "")[:60])
                if violations:
                    logger.warning("compaction consistency violations: %s", violations)

            is_report = definition.agent_id == "ReportAgent"
            # A loaded skill is "applied" only when its instructions actually
            # enter a subsequent model turn.
            for skill_id, loaded in loaded_skills.items():
                if skill_id in applied_skill_ids:
                    continue
                if loaded.instructions and loaded.instructions[:80] in "\n".join(
                        str(m.get("content") or "") for m in messages):
                    await default_skill_manager.emit_applied(
                        self.emitter, agent_id, loaded,
                        tool_call_id=loaded_skill_call_ids[skill_id],
                        round_id=round_id)
                    applied_skill_ids.add(skill_id)

            # Once the model has consumed its native action turns, remove every
            # action tool and force the provider-native terminal function.
            # Rejecting another proposed action would consume the final LLM
            # turn and strand ReportAgent without a report.
            force_final = (
                action_turns >= max_action_turns
                or iteration >= max_turns
            )
            turn_tools = [final_tool] if force_final else model_tools
            turn_messages = list(messages)
            tool_choice: Any = "auto"
            if force_final:
                terminal_name = str(final_tool["function"]["name"])
                tool_choice = {
                    "type": "function",
                    "function": {"name": terminal_name},
                }
                turn_messages.append({
                    "role": "user",
                    "content": (
                        "工具观察阶段已结束。现在必须仅调用 "
                        f"{terminal_name} 提交最终结构化结果；"
                        "不要再请求任何检索、Skill 或校验工具。"
                    ),
                })

            # Persist memory usage once, at the first prompt that consumes it.
            # Every later round still declares it as an input attachment below,
            # without duplicating durable usage rows or sibling trace nodes.
            if memory_audit and not memory_usage_recorded:
                decisions = memory_audit.get("decisions") or []
                for decision in decisions:
                    decision.roundId = round_id
                    decision.occurredAt = _utc_now()
                if decisions:
                    try:
                        await self.memory.record_usage(
                            consumer_agent=agent_id, decisions=decisions)
                    except Exception:  # noqa: BLE001
                        pass
                memory_usage_recorded = True

            turn_model_names = {
                str(item.get("function", {}).get("name") or "")
                for item in turn_tools if isinstance(item, dict)
            }
            tool_catalog_refs = [
                {
                    "toolName": entry.get("name"),
                    "modelName": entry.get("modelName"),
                    "source": ("mcp" if entry.get("mcpServer") else
                               entry.get("kind") or "builtin"),
                    "mcpServer": entry.get("mcpServer"),
                    "description": entry.get("description"),
                    "inputSchema": entry.get("inputSchema"),
                }
                for entry in catalog
                if str(entry.get("modelName") or "") in turn_model_names
            ]
            terminal_name = str(final_tool.get("function", {}).get("name") or "")
            if terminal_name in turn_model_names:
                tool_catalog_refs.append({
                    "toolName": terminal_name,
                    "modelName": terminal_name,
                    "source": "runtime_terminal",
                    "mcpServer": None,
                    "description": final_tool.get("function", {}).get("description"),
                    "inputSchema": final_tool.get("function", {}).get("parameters"),
                })
            memory_refs = [
                row for row in (memory_audit.get("memoryTrace") or [])
                if row.get("used")
            ] if memory_audit else []
            skill_refs = [{
                "skillId": skill.skill_id,
                "skillVersion": (
                    loaded_skills.get(skill.skill_id, skill).version),
                "disclosureState": (
                    "INSTRUCTIONS" if skill.skill_id in loaded_skills
                    else "METADATA"),
                "selected": True,
                "loaded": skill.skill_id in loaded_skills,
                "applied": skill.skill_id in applied_skill_ids,
                "instructionsAttached": skill.skill_id in applied_skill_ids,
                "loadToolCallId": loaded_skill_call_ids.get(skill.skill_id),
            } for skill in skills]
            observed_tool_call_ids = list(dict.fromkeys(
                pre_llm_tool_call_ids + [
                    str(message.get("tool_call_id") or "")
                    for message in native_history
                    if message.get("role") == "tool"
                    and message.get("tool_call_id")
                ]))
            trace_context = {
                "roundId": round_id,
                "parentAgentId": agent_id,
                "contextRole": "MODEL_INPUT",
            }
            await self.emitter.emit(
                "llm.context.attached", agent_id=agent_id, payload={
                    **trace_context,
                    "memoryRefs": memory_refs,
                    "skillRefs": skill_refs,
                    "toolCatalogRefs": tool_catalog_refs,
                    "observedToolCallIds": observed_tool_call_ids,
                    "memoryCount": len(memory_refs),
                    "skillCount": len(skill_refs),
                    "memoryAttachedCount": len(memory_refs),
                    "skillMetadataCount": len(skills),
                    "skillInstructionCount": sum(
                        1 for ref in skill_refs
                        if ref["instructionsAttached"]),
                    "toolCatalogCount": len(tool_catalog_refs),
                    "messageCount": len(turn_messages),
                    "toolChoice": tool_choice,
                    "occurredAt": _utc_now(),
                })
            turn = await self._chat_native_turn(
                turn_messages, agent_id=agent_id,
                purpose=definition.output_type,
                # Full reports routinely exceed 4k tokens because every score,
                # risk and interview probe is evidence-bound. A 4096 ceiling
                # truncated provider-native emit_decision arguments twice in
                # production, leaving no JSON object for schema repair.
                max_tokens=(8192 if is_report else 3600 if (
                    definition.agent_id in TERMINAL_AGENTS) else 4096),
                tools=turn_tools,
                tool_choice=tool_choice,
                use_quality=is_report,
                trace_context=trace_context)
            agent_llm_calls += 1
            raw = turn.content
            final_calls: List[Any] = []

            if turn.tool_calls:
                native_history.append({
                    "role": "assistant",
                    "content": turn.content or None,
                    "tool_calls": [
                        {
                            "id": call.tool_call_id,
                            "type": "function",
                            "function": {
                                "name": call.name,
                                "arguments": call.raw_arguments,
                            },
                        }
                        for call in turn.tool_calls
                    ],
                })
                final_calls = [
                    call for call in turn.tool_calls
                    if call.name == "emit_decision"]
                action_calls = [
                    call for call in turn.tool_calls
                    if call.name != "emit_decision"]

                # When actions and a final emission appear together, execute
                # the actions and explicitly defer the stale final proposal.
                if action_calls:
                    action_turn_allowed = action_turns < max_action_turns
                    if action_turn_allowed:
                        action_turns += 1
                    observations = ""
                    tool_messages: List[Dict[str, Any]] = []
                    for proposed in turn.tool_calls:
                        if proposed.name == "emit_decision":
                            result_payload = {
                                "success": False,
                                "deferred": True,
                                "reason": "tool results must be observed before final output",
                            }
                            tool_messages.append({
                                "role": "tool",
                                "tool_call_id": proposed.tool_call_id,
                                "name": proposed.name,
                                "content": json.dumps(
                                    result_payload, ensure_ascii=False),
                            })
                            continue

                        tool = model_tool_aliases.get(proposed.name, "")
                        entry = next(
                            (item for item in catalog
                             if item.get("name") == tool), {})
                        defn = self.tools.definitions.get(tool)
                        source = (
                            "mcp" if defn and defn.kind == "mcp"
                            else "skill" if tool in {
                                "load_skill", "read_skill_resource"}
                            else defn.kind if defn else "unknown")
                        if source == "mcp":
                            # Bind the earlier catalog exposure to the provider
                            # call id so catalog → proposal → execution → result
                            # can be queried as one trace chain.
                            await self.emitter.emit(
                                "tool.progress", agent_id=agent_id,
                                tool_name=tool or proposed.name, payload={
                                    **trace_context,
                                    "toolCallId": proposed.tool_call_id,
                                    "lifecycleStage": "CATALOG_EXPOSED",
                                    "source": "mcp",
                                    "mcpServer": entry.get("mcpServer"),
                                    "toolName": tool or proposed.name,
                                    "modelName": proposed.name,
                                    "description": entry.get("description"),
                                    "inputSchema": entry.get("inputSchema"),
                                    "originalExposureId": (
                                        catalog_exposure_ids.get(tool)),
                                    "occurredAt": _utc_now(),
                                })
                        await self.emitter.emit(
                            "tool.progress", agent_id=agent_id,
                            tool_name=tool or proposed.name, payload={
                                **trace_context,
                                "toolCallId": proposed.tool_call_id,
                                "lifecycleStage": "LLM_PROPOSED",
                                "source": source,
                                "mcpServer": (
                                    entry.get("mcpServer")
                                    or (defn.mcp_server if defn else None)),
                                "toolName": tool or proposed.name,
                                "modelName": proposed.name,
                                "inputSchema": (
                                    entry.get("inputSchema")
                                    or (defn.input_schema if defn else None)),
                                "arguments": proposed.arguments,
                                "argumentsParseError": (
                                    proposed.arguments_error or None),
                                "occurredAt": _utc_now(),
                            })

                        if not action_turn_allowed:
                            result_payload = await self._reject_native_proposal(
                                agent_id, tool or proposed.name, proposed,
                                "native action-turn budget exhausted; emit a final decision",
                                source=source,
                                mcp_server=entry.get("mcpServer"),
                                trace_context=trace_context)
                        elif proposed.arguments_error:
                            result_payload = await self._reject_native_proposal(
                                agent_id, tool or proposed.name, proposed,
                                f"arguments are not a JSON object: "
                                f"{proposed.arguments_error}", source=source,
                                mcp_server=entry.get("mcpServer"),
                                trace_context=trace_context)
                        elif not tool or tool not in allowed_tools:
                            result_payload = await self._reject_native_proposal(
                                agent_id, tool or proposed.name, proposed,
                                "tool was not present in this agent's exposed catalog",
                                source=source,
                                mcp_server=entry.get("mcpServer"),
                                trace_context=trace_context)
                        elif agent_tool_calls >= agent_tool_limit:
                            result_payload = await self._reject_native_proposal(
                                agent_id, tool, proposed,
                                "agent tool budget exhausted", source=source,
                                mcp_server=entry.get("mcpServer"),
                                trace_context=trace_context)
                        elif tool in {"load_skill", "read_skill_resource"}:
                            result_payload = await self._execute_skill_proposal(
                                agent_id=agent_id,
                                tool=tool,
                                proposed=proposed,
                                selected_skills=skills,
                                loaded_skills=loaded_skills,
                                loaded_skill_call_ids=loaded_skill_call_ids,
                                trace_context=trace_context)
                            agent_tool_calls += 1
                        else:
                            args = proposed.arguments
                            guard = self.guard.check_tool_call(
                                ToolExecutor.signature(tool, args))
                            if guard.triggered:
                                await self._emit_guard(guard, agent_id)
                                result_payload = await self._reject_native_proposal(
                                    agent_id, tool, proposed,
                                    "duplicate call blocked by loop guard",
                                    source=source,
                                    mcp_server=entry.get("mcpServer"),
                                    trace_context=trace_context)
                            else:
                                call = await self.tools.execute(
                                    agent_id, tool, args,
                                    # The model already authored these native
                                    # tool arguments. Never spend a hidden
                                    # provider call rewriting them.
                                    enable_rewrite=False,
                                    tool_call_id=proposed.tool_call_id,
                                    trace_context=trace_context)
                                agent_tool_calls += 1
                                if call.status != "SUCCEEDED":
                                    self._tool_failed_this_group = True
                                observations += self._format_tool_result(call)
                                if call.status == "SUCCEEDED":
                                    await self._record_tool_success(
                                        agent_id, tool, args, call.result,
                                        tool_call_id=call.tool_call_id)
                                result_payload = {
                                    "success": call.status == "SUCCEEDED",
                                    "status": call.status,
                                    "result": call.result if (
                                        call.status == "SUCCEEDED") else None,
                                    "error": call.error if (
                                        call.status != "SUCCEEDED") else None,
                                }
                        observations += (
                            f"\n[MODEL_TOOL_RESULT {tool or proposed.name} "
                            f"id={proposed.tool_call_id}] "
                            f"{json.dumps(result_payload, ensure_ascii=False)[:1500]}")
                        tool_messages.append({
                            "role": "tool",
                            "tool_call_id": proposed.tool_call_id,
                            "name": proposed.name,
                            "content": json.dumps(
                                result_payload, ensure_ascii=False)[:12000],
                        })
                    native_history.extend(tool_messages)
                    guard = self.guard.check_observation(observations)
                    if guard.triggered:
                        await self._emit_guard(guard, agent_id)
                    tool_results_block += observations
                    continue

                # No actions: the final structured function arguments are the
                # decision payload. A plain content JSON remains a compatibility
                # fallback for providers without function calling.
                if final_calls:
                    raw = final_calls[0].raw_arguments

            decision_iterations += 1
            decision, schema_error = self._parse_decision(raw)
            if (decision is not None
                    and agent_id == "ReportAgent"
                    and self._requires_score_contract()):
                report_error = self._report_decision_schema_error(decision)
                if report_error:
                    decision = None
                    schema_error = report_error
            if decision is None:
                repair_allowed = (
                    decision_iterations < max_decision_iterations)
                # A native action followed by a malformed terminal function
                # can consume the agent's final planned turn.  When the
                # run-wide ledger still has assignable capacity, borrow one
                # explicit repair turn instead of stranding a valid run.
                # The provider client remains the hard budget authority.
                runtime_budget = getattr(self.llm, "budget", self.budget)
                repair_scope = (
                    "control" if agent_id == "CoordinatorAgent"
                    else "terminal" if agent_id in TERMINAL_AGENTS
                    else f"agent:{agent_id}")
                can_borrow_repair = (
                    repair_allowed
                    and bool(final_calls)
                    and iteration >= max_turns
                    and borrowed_repair_turns < 1
                    and runtime_budget.available_llm_calls_for_scope(
                        self.policy.maxLlmCalls, repair_scope) > 0
                )
                if can_borrow_repair:
                    borrowed_repair_turns += 1
                    max_turns += 1
                    await self.emitter.emit(
                        "run.progress", agent_id=agent_id, payload={
                            "stage": "budget_reallocated",
                            "reason": "malformed_native_final",
                            "borrowedRepairTurns": borrowed_repair_turns,
                            "plannedLlmQuota": agent_llm_quota,
                            "effectiveTurnLimit": max_turns,
                            "schemaError": schema_error[:200],
                            "occurredAt": _utc_now(),
                        })
                if repair_allowed and iteration < max_turns:
                    repair_message = (
                        "上面的输出未通过 json schema 校验："
                        f"{schema_error[:400]}。"
                        "请使用 emit_decision/emit_report 提交修正后的结构化结果。")
                    if final_calls:
                        native_history.append({
                            "role": "tool",
                            "tool_call_id": final_calls[0].tool_call_id,
                            "name": final_calls[0].name,
                            "content": json.dumps({
                                "success": False,
                                "error": schema_error[:400],
                                "retryable": True,
                            }, ensure_ascii=False),
                        })
                    elif raw:
                        native_history.append({
                            "role": "assistant", "content": raw[:1500]})
                    native_history.append({
                        "role": "user", "content": repair_message})
                    continue
                raise LlmError(
                    "MALFORMED_OUTPUT",
                    f"agent output failed schema validation within budget: "
                    f"{schema_error[:200]}",
                    False)

            thought = str(decision.get("thought") or "")
            if thought:
                guard = self.guard.check_plan(f"{agent_id}:{thought}")
                if guard.triggered:
                    await self._emit_guard(guard, agent_id)
                    decision["toolCalls"] = []
                    decision["done"] = True

            nested_calls = decision.get("toolCalls") or []
            if nested_calls:
                await self.emitter.emit("run.progress", agent_id=agent_id, payload={
                    "stage": "nested_tool_calls_rejected",
                    "reason": "tools must use provider-native function calls",
                    "count": len(nested_calls),
                    "occurredAt": _utc_now(),
                })
                decision["toolCalls"] = []
                if (not decision.get("output")
                        and decision_iterations < max_decision_iterations
                        and iteration < max_turns):
                    tool_results_block += (
                        "\n[NATIVE_TOOL_REQUIRED] JSON 内嵌 toolCalls 已拒绝；"
                        "请使用已提供的原生 function tools。")
                    continue

            # First-class handoff: reuse the requestedNextAction insertion
            # path (dependency + delegation-cycle + budget checks live there),
            # but emit an explicit edge for the trace view.
            handoff = decision.get("handoff") or {}
            if isinstance(handoff, dict) and handoff.get("to"):
                target = str(handoff["to"])
                # Handoff 去环：拒绝已完成目标；LoopGuard 另检双向委派。
                if target not in self.executed:
                    delegation = self.guard.check_delegation(agent_id, target)
                    if delegation.triggered:
                        await self._emit_guard(delegation, agent_id)
                    else:
                        self._pending_handoff = target
                        raw_candidate = decision.get("output")
                        if not isinstance(raw_candidate, dict):
                            decision["output"] = raw_candidate = {}
                        raw_candidate.setdefault("requestedNextAction", target)
                        await self.emitter.emit("agent.progress", agent_id=agent_id, payload={
                            "handoff": {"to": target,
                                        "reason": str(handoff.get("reason", ""))[:200],
                                        "task": str(handoff.get("task", ""))[:200]}})
                else:
                    await self.emitter.emit("agent.progress", agent_id=agent_id, payload={
                        "handoffRejected": True, "to": target,
                        "reason": "handoff 去环：目标 Agent 已执行"})

            raw_output = decision.get("output")
            if (raw_output or decision.get("done")
                    or decision_iterations >= max_decision_iterations
                    or iteration >= max_turns):
                output = self._build_output(definition, raw_output, tool_results_block)

        if output is None:
            output = self._build_output(definition, None, tool_results_block)
        self.skill_selections[agent_id] = [
            loaded_skills.get(skill.skill_id, skill) for skill in skills]
        await self._emit_unapplied_skills(
            agent_id, skills, loaded_skills, applied_skill_ids,
            reason="model_did_not_load_or_apply")
        self.agent_counters[definition.agent_id] = {
            "iterations": iteration,
            "decisionIterations": decision_iterations,
            "actionTurns": action_turns,
            "llmCalls": agent_llm_calls,
            "toolCalls": agent_tool_calls,
            "borrowedRepairTurns": borrowed_repair_turns,
        }
        return output

    async def _chat_native_turn(self, messages: List[Dict[str, Any]], *,
                                agent_id: str, purpose: str,
                                max_tokens: int,
                                tools: List[Dict[str, Any]],
                                use_quality: bool,
                                tool_choice: Any = "auto",
                                trace_context: Optional[Dict[str, Any]] = None
                                ) -> LlmTurn:
        """Use native tool calling when supported; preserve test adapters."""
        chat_turn = getattr(self.llm, "chat_turn", None)
        if callable(chat_turn):
            kwargs: Dict[str, Any] = {
                "agent_id": agent_id,
                "purpose": purpose,
                "max_tokens": max_tokens,
                "tools": tools,
                "tool_choice": tool_choice,
                "use_quality": use_quality,
            }
            # Compatibility adapters often accept **kwargs only to forward
            # them to an older strict signature. Pass trace metadata solely
            # when the adapter explicitly declares support for it.
            if "trace_context" in inspect.signature(chat_turn).parameters:
                kwargs["trace_context"] = trace_context
            return await chat_turn(messages, **kwargs)
        chat = self.llm.chat
        kwargs = {
            "agent_id": agent_id,
            "purpose": purpose,
            "max_tokens": max_tokens,
            "tools": tools,
            "tool_choice": tool_choice,
            "use_quality": use_quality,
        }
        if "trace_context" in inspect.signature(chat).parameters:
            kwargs["trace_context"] = trace_context
        raw = await chat(messages, **kwargs)
        return LlmTurn(content=str(raw or ""), tool_calls=[],
                       finish_reason="legacy_adapter")

    async def _reject_native_proposal(self, agent_id: str, tool: str,
                                      proposed: LlmToolCall, reason: str, *,
                                      source: str,
                                      mcp_server: Optional[str] = None,
                                      trace_context: Optional[Dict[str, Any]] = None
                                      ) -> Dict[str, Any]:
        now = _utc_now()
        await self.emitter.emit("tool.failed", agent_id=agent_id,
                                tool_name=tool, payload={
                                    **(trace_context or {}),
                                    "toolCallId": proposed.tool_call_id,
                                    "lifecycleStage": "ERROR",
                                    "source": source,
                                    "mcpServer": mcp_server,
                                    "toolName": tool,
                                    "modelName": proposed.name,
                                    "arguments": proposed.arguments,
                                    "error": reason,
                                    "outcome": "REJECTED",
                                    "occurredAt": now,
                                    "startedAt": now,
                                    "endedAt": now,
                                })
        return {
            "success": False,
            "status": "REJECTED",
            "error": reason,
        }

    async def _execute_skill_proposal(
            self, *, agent_id: str, tool: str, proposed: LlmToolCall,
            selected_skills: List[Any],
            loaded_skills: Dict[str, Any],
            loaded_skill_call_ids: Dict[str, str],
            trace_context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        if self.budget.tool_calls >= self.tools.max_tool_calls_run:
            return await self._reject_native_proposal(
                agent_id, tool, proposed, "run tool budget exhausted",
                source="skill", trace_context=trace_context)
        started_at = _utc_now()
        self.budget.tool_calls += 1
        await self.emitter.emit("tool.started", agent_id=agent_id,
                                tool_name=tool, payload={
                                    **(trace_context or {}),
                                    "toolCallId": proposed.tool_call_id,
                                    "lifecycleStage": "EXECUTION_STARTED",
                                    "source": "skill",
                                    "kind": "internal",
                                    "toolName": tool,
                                    "modelName": proposed.name,
                                    "arguments": proposed.arguments,
                                    "occurredAt": started_at,
                                    "startedAt": started_at,
                                })
        skill_id = str(proposed.arguments.get("skill_id") or "").strip()
        try:
            selected_ids = {skill.skill_id for skill in selected_skills}
            if not skill_id or skill_id not in selected_ids:
                raise KeyError(
                    f"skill not selected for this agent: {skill_id or '<empty>'}")
            if tool == "load_skill":
                loaded = default_skill_manager.load(skill_id)
                loaded_skills[skill_id] = loaded
                loaded_skill_call_ids[skill_id] = proposed.tool_call_id
                await default_skill_manager.emit_loaded(
                    self.emitter, agent_id, loaded,
                    tool_call_id=proposed.tool_call_id,
                    reason="llm_native_tool_call",
                    round_id=(trace_context or {}).get("roundId"))
                result: Dict[str, Any] = {
                    "success": True,
                    "loaded": True,
                    "skillId": loaded.skill_id,
                    "skillVersion": loaded.version,
                    "resources": list(loaded.resource_paths),
                    "instructionsInjectedNextTurn": True,
                }
            else:
                loaded = loaded_skills.get(skill_id)
                if loaded is None:
                    raise KeyError(
                        f"load_skill must run before read_skill_resource: {skill_id}")
                path = str(proposed.arguments.get("path") or "").strip()
                content = default_skill_manager.read_resource(skill_id, path)
                result = {
                    "success": True,
                    "loaded": True,
                    "skillId": loaded.skill_id,
                    "skillVersion": loaded.version,
                    "path": path,
                    "content": content,
                }
                await self.emitter.emit(
                    "skill.loaded", agent_id=agent_id,
                    tool_name=skill_id, payload={
                        **(trace_context or {}),
                        "toolCallId": proposed.tool_call_id,
                        "skillId": loaded.skill_id,
                        "skillVersion": loaded.version,
                        "skillHash": loaded.hash,
                        "agentId": agent_id,
                        "lifecycleStage": "RESOURCE_LOADED",
                        "reason": "llm_requested_reference",
                        "resourcePath": path,
                        "disclosureState": "RESOURCE",
                        "occurredAt": _utc_now(),
                    })
            ended_at = _utc_now()
            await self.emitter.emit("tool.completed", agent_id=agent_id,
                                    tool_name=tool, payload={
                                        **(trace_context or {}),
                                        "toolCallId": proposed.tool_call_id,
                                        "lifecycleStage": "RESULT",
                                        "source": "skill",
                                        "kind": "internal",
                                        "toolName": tool,
                                        "modelName": proposed.name,
                                        "arguments": proposed.arguments,
                                        "resultPreview": {
                                            k: v for k, v in result.items()
                                            if k != "content"},
                                        "occurredAt": ended_at,
                                        "startedAt": started_at,
                                        "endedAt": ended_at,
                                    })
            return result
        except Exception as exc:  # noqa: BLE001
            ended_at = _utc_now()
            error = f"{type(exc).__name__}: {exc}"[:500]
            await self.emitter.emit("tool.failed", agent_id=agent_id,
                                    tool_name=tool, payload={
                                        **(trace_context or {}),
                                        "toolCallId": proposed.tool_call_id,
                                        "lifecycleStage": "ERROR",
                                        "source": "skill",
                                        "kind": "internal",
                                        "toolName": tool,
                                        "modelName": proposed.name,
                                        "arguments": proposed.arguments,
                                        "error": error,
                                        "occurredAt": ended_at,
                                        "startedAt": started_at,
                                        "endedAt": ended_at,
                                    })
            await self.emitter.emit("skill.failed", agent_id=agent_id,
                                    tool_name=skill_id or tool, payload={
                                        **(trace_context or {}),
                                        "toolCallId": proposed.tool_call_id,
                                        "skillId": skill_id or None,
                                        "skillVersion": None,
                                        "agentId": agent_id,
                                        "lifecycleStage": "ERROR",
                                        "reason": error,
                                        "occurredAt": ended_at,
                                    })
            return {"success": False, "status": "FAILED", "error": error}

    async def _emit_unapplied_skills(
            self, agent_id: str, selected: List[Any],
            loaded: Dict[str, Any], applied_ids: set, *,
            reason: str) -> None:
        for metadata in selected:
            if metadata.skill_id in applied_ids:
                continue
            skill = loaded.get(metadata.skill_id, metadata)
            detail = (
                "loaded_without_subsequent_model_turn"
                if metadata.skill_id in loaded
                else reason)
            try:
                await default_skill_manager.emit_skipped(
                    self.emitter, agent_id, skill, reason=detail)
            except Exception as exc:  # noqa: BLE001
                logger.debug("skill skipped event emit failed: %s", exc)

    async def _record_tool_success(self, agent_id: str, tool: str,
                                   args: Dict[str, Any], result: Any, *,
                                   tool_call_id: str) -> None:
        """Persist only successful tool material; failures never become evidence."""
        if isinstance(result, dict) and result.get("success") is False:
            return
        if tool == "calculate_jd_coverage":
            self.state.put_artifact("jdCoverage", result)
        elif tool == "check_timeline":
            self.state.put_artifact("timelineCheck", result)
        elif tool == "verify_report_evidence":
            self._apply_verification(result)
        elif tool == "parse_resume":
            self.state.put_artifact("parsedResume", result)
            facts = self._resume_facts_from_parse(result)
            if facts:
                self.state.put_artifact("resumeFacts", facts)
        elif tool in ("knowledge_search", "resume_semantic_search"):
            await self._emit_rag_metrics(
                agent_id, result, str(args.get("query") or ""),
                tool_name=tool, tool_call_id=tool_call_id,
                requested_k=int(args.get("topK") or 5))
        elif tool == "jd_match_search":
            self._store_jd_match_artifacts(result)

        defn = self.tools.definitions.get(tool)
        if defn is not None and defn.kind == "mcp":
            server = str(defn.mcp_server or "")
            result_source_urls = _collect_source_urls(result)
            request_source_urls = _collect_source_urls(
                args.get("url"), args.get("urls"))
            source_urls = list(result_source_urls)
            if server == "fetch" or tool == "exa.web_fetch_exa":
                # For fetch calls the URL is the actual requested document.
                # Search-query text, however, is not evidence provenance.
                source_urls = list(dict.fromkeys(
                    request_source_urls + result_source_urls))
            base_entry = {
                "toolCallId": tool_call_id,
                "tool": tool,
                "mcpServer": server,
                "status": "SUCCEEDED",
                "query": str(args.get("query") or "")[:500],
                "url": str(args.get("url") or "")[:1000],
                "repository": str(
                    args.get("repoName") or args.get("repository")
                    or args.get("repo") or "")[:300],
                "sourceUrls": source_urls,
                "result": result,
                "collectedAt": _utc_now(),
            }

            # Documentation MCPs inform reasoning but are never candidate
            # facts. DeepWiki additionally requires the locally injected,
            # candidate-declared repository binding from ToolExecutor.
            if server in {"deepwiki", "context7", "microsoft-learn"}:
                if server == "deepwiki":
                    policy = (
                        result.get("evidencePolicy")
                        if isinstance(result, dict) else None)
                    binding_url = str(
                        policy.get("sourceUrl") or ""
                    ) if isinstance(policy, dict) else ""
                    if (
                            not isinstance(policy, dict)
                            or policy.get("evidenceUse") != "context_only"
                            or policy.get("candidateFactEligible") is not False
                            or not re.match(
                                r"^https://github\.com/[^/]+/[^/]+/?$",
                                binding_url, re.IGNORECASE)):
                        logger.warning(
                            "DeepWiki result omitted from state: invalid "
                            "candidate repository binding")
                        return
                    source_urls = [binding_url]
                    base_entry["sourceUrls"] = source_urls
                    base_entry["repository"] = str(
                        policy.get("repository") or "")[:300]
                base_entry.update({
                    "evidenceUse": "context_only",
                    "candidateFactEligible": False,
                    "sourceBacked": bool(source_urls),
                })
                # Append one fresh item through the state boundary. Reusing
                # the canonical list and then writing that same list back
                # makes apply_artifacts iterate and append to one object,
                # which can grow forever on the second successful MCP call.
                self.state.apply_artifacts({"mcpContext": [base_entry]},
                                           by_agent=agent_id)
                return

            # Public-web output without an HTTP(S) source may still be shown to
            # the calling model, but it cannot enter the evidence ledger.
            if not source_urls:
                logger.info(
                    "MCP result omitted from mcpEvidence: %s returned no source URL",
                    tool)
                return

            base_entry.update({
                "evidenceUse": "raw_source_for_calibration",
                "candidateFactEligible": False,
                "requiresCalibration": True,
                "sourceBacked": True,
            })
            self.state.apply_artifacts({"mcpEvidence": [base_entry]},
                                       by_agent=agent_id)

    @staticmethod
    def _parse_decision(raw: str) -> Tuple[Optional[Dict[str, Any]], str]:
        """Layered JSON guarantee, application side: extract the object, then
        validate against the AgentDecision schema. Returns (decision, error);
        decision is None when either layer fails, with the exact violation in
        error so the repair call can quote it back to the model."""
        candidate = extract_json_object(raw)
        if not candidate:
            return None, "输出中找不到可解析的 JSON 对象"
        # Some OpenAI-compatible providers occasionally double-encode a
        # function argument field and return ``output`` as a JSON string.
        # Decode only a complete JSON object through the same bounded extractor
        # used for the outer response; arbitrary prose remains invalid.
        raw_output = candidate.get("output")
        if isinstance(raw_output, str):
            parsed_output = extract_json_object(raw_output)
            if not parsed_output:
                return None, "output 是字符串，但其中找不到合法 JSON 对象"
            candidate = dict(candidate)
            candidate["output"] = parsed_output
        try:
            validated = AgentDecision.model_validate(candidate)
        except Exception as exc:  # pydantic.ValidationError
            return None, str(exc)
        decision = validated.model_dump()
        # Downstream expects plain dicts for tool calls.
        decision["toolCalls"] = [
            {"tool": c["tool"], "arguments": c["arguments"]}
            for c in decision.get("toolCalls", [])]
        if decision.get("handoff") is not None and not decision["handoff"].get("to"):
            decision["handoff"] = None
        return decision, ""

    def _report_decision_schema_error(
            self, decision: Dict[str, Any]) -> str:
        """Validate the score-contract payload before accepting ``done=true``.

        ``AgentDecision`` intentionally permits an empty output for specialist
        agents. ReportAgent cannot use that looser contract: accepting an empty
        or malformed report here would end the loop and waste the reserved
        terminal repair turn, eventually producing ``no_terminal_answer``.
        """
        output = decision.get("output")
        if not isinstance(output, dict):
            return (
                "ReportAgent structured report 缺失，"
                "必须提交结构化 report")
        report = output.get("report")
        if not isinstance(report, dict):
            return "ReportAgent structured report 缺失或不是 JSON 对象"

        # ``interviewProbes`` is the established richer runtime field. Accept
        # it as the compatibility alias of the provider-schema field and
        # normalize the in-flight decision so persisted output is canonical.
        probes = report.get("interviewProbes")
        questions = report.get("interviewQuestions")
        if isinstance(probes, list) and (
                not isinstance(questions, list) or not questions):
            report["interviewQuestions"] = list(probes)

        required = (
            "recommendation",
            "dimensions",
            "strengths",
            "risks",
            "interviewQuestions",
            "dataQuality",
        )
        missing = [field for field in required if field not in report]
        if missing:
            return (
                "ReportAgent structured report 缺少必填字段: "
                + ", ".join(missing)
            )
        for field in (
                "dimensions", "strengths", "risks", "interviewQuestions"):
            if not isinstance(report.get(field), list):
                return (
                    f"ReportAgent structured report 字段 {field} 必须是数组")

        validated = self._validate_structured_report(report)
        if not validated:
            return "ReportAgent structured report 未通过运行时语义校验"
        if report.get("dimensions") and not validated.get("dimensions"):
            return "ReportAgent structured report 的 dimensions 全部无效"
        return ""

    def _build_output(self, definition: AgentDefinition,
                      raw_output: Optional[Dict[str, Any]],
                      tool_results_block: str) -> AgentOutput:
        raw_output = raw_output if isinstance(raw_output, dict) else {}
        summary = str(raw_output.get("summary") or "")
        guard = self.guard.check_conclusion(f"{definition.agent_id}:{summary}")
        claims = [c for c in (raw_output.get("claims") or []) if isinstance(c, dict)]
        evidence = [e for e in (raw_output.get("evidence") or []) if isinstance(e, dict)]
        try:
            confidence = float(raw_output.get("confidence", 0.5))
        except (TypeError, ValueError):
            confidence = 0.5
        requested = raw_output.get("requestedNextAction")
        output = AgentOutput(
            agentId=definition.agent_id,
            type=definition.output_type,
            claims=claims,
            evidence=evidence,
            confidence=max(0.0, min(1.0, confidence)),
            source="llm+tools" if tool_results_block else "llm",
            dependencies=[],
            requestedNextAction=str(requested) if requested and not guard.triggered else None,
            summary=summary[:500])
        if definition.agent_id in TERMINAL_AGENTS:
            if definition.agent_id == "ReportAgent":
                report = raw_output.get("report")
                if isinstance(report, dict):
                    if summary and not report.get("summary"):
                        report = {**report, "summary": summary}
                    validated = self._validate_structured_report(report)
                    if validated:
                        self.state.put_artifact("finalReport", validated)
                        self.final_answer = self.render_report(validated)
                        return output
                # followup/quick_answer 可降级为短答；评估类必须走结构化契约。
                if not self._requires_score_contract():
                    answer = raw_output.get("answer") or summary
                    if answer:
                        self.final_answer = str(answer)
                return output
            report = raw_output.get("report")
            if isinstance(report, dict):
                validated = self._validate_structured_report(report)
                if validated:
                    self.state.put_artifact("finalReport", validated)
            answer = raw_output.get("answer") or raw_output.get("markdown") or summary
            if not answer and isinstance(report, dict):
                answer = json.dumps(report, ensure_ascii=False, indent=2)
            if isinstance(answer, dict):
                answer = json.dumps(answer, ensure_ascii=False, indent=2)
            if answer:
                self.final_answer = str(answer)
        return output

    async def _emit_rag_metrics(
            self, agent_id: str, result: Any, query: str = "", *,
            tool_name: str, tool_call_id: str,
            requested_k: Any = None) -> None:
        """Emit measured multi-stage retrieval telemetry.

        Missing provider counters remain ``None``/``NOT_COLLECTED``; the
        runtime never invents recall, latency stages, cache hits or scores.
        """
        if not isinstance(result, dict):
            return
        chunks: List[Any] = []
        for key in ("chunks", "results", "hits", "items"):
            if isinstance(result.get(key), list):
                chunks = result[key]
                break
        returned_k = len(chunks)
        queries_used = result.get("queriesUsed") or ([query] if query else [])
        score_values: List[float] = []
        doc_ids = set()
        normalized_chunks = []
        for chunk in chunks[:10]:
            if isinstance(chunk, dict):
                raw_score = next((
                    chunk.get(key) for key in (
                        "score", "relevanceScore", "similarity",
                        "vectorScore", "bm25Score", "rrfScore")
                    if isinstance(chunk.get(key), (int, float))), None)
                score = float(raw_score) if raw_score is not None else None
                if score is not None:
                    score_values.append(score)
                doc_id = chunk.get("docId") or chunk.get("documentId") or chunk.get("id") or ""
                if doc_id:
                    doc_ids.add(doc_id)
                content = chunk.get("content") or chunk.get("text") or chunk.get("pageContent") or ""
                normalized_chunks.append({
                    "chunkId": chunk.get("chunkId") or chunk.get("id"),
                    "docId": doc_id or None,
                    "title": chunk.get("title"),
                    "source": chunk.get("source") or chunk.get("sourceType"),
                    "uri": chunk.get("uri") or chunk.get("url"),
                    "score": round(score, 4) if score is not None else None,
                    "content": str(content)[:800] if content else None,
                    "provenance": chunk.get("provenance") or {
                        "indexName": chunk.get("index")
                        or chunk.get("collection"),
                        "sourceId": chunk.get("sourceId"),
                    },
                })
        latency = result.get("_latency") or {}
        counters = result.get("counters") if isinstance(
            result.get("counters"), dict) else {}
        candidate_count = (
            result.get("candidateCount")
            if isinstance(result.get("candidateCount"), int)
            else counters.get("candidateCount")
            if isinstance(counters.get("candidateCount"), int)
            else None)
        ended_at = str(result.get("retrievedAt") or _utc_now())
        rerank_applied = (
            result.get("rerankApplied")
            if isinstance(result.get("rerankApplied"), bool)
            else result.get("agenticRerank")
            if isinstance(result.get("agenticRerank"), bool)
            else None)
        payload = {
            "toolCallId": tool_call_id,
            "toolName": tool_name,
            "query": query[:200] if query else "",
            "requestedK": int(requested_k) if isinstance(
                requested_k, (int, float)) else None,
            "returnedK": returned_k,
            "candidateCount": candidate_count,
            "lexicalHits": result.get("lexicalHits")
            if isinstance(result.get("lexicalHits"), int) else None,
            "vectorHits": result.get("vectorHits")
            if isinstance(result.get("vectorHits"), int) else None,
            "filteredCount": result.get("filteredCount")
            if isinstance(result.get("filteredCount"), int) else None,
            "droppedCount": result.get("droppedCount")
            if isinstance(result.get("droppedCount"), int) else None,
            "deduplicatedCount": result.get("deduplicatedCount")
            if isinstance(result.get("deduplicatedCount"), int) else None,
            "hitCount": returned_k,
            "queriesUsed": queries_used[:3],
            "queryRewriteMode": result.get("queryRewriteMode"),
            "scores": {
                "top": round(max(score_values), 4) if score_values else None,
                "min": round(min(score_values), 4) if score_values else None,
                "mean": round(sum(score_values) / len(score_values), 4)
                if score_values else None,
                "collectedCount": len(score_values),
            },
            "uniqueDocuments": len(doc_ids),
            "docIds": list(doc_ids)[:5],
            "strategy": result.get("strategy"),
            "fusionStrategy": result.get("fusion"),
            "indexName": result.get("indexName"),
            "source": result.get("source"),
            "chunks": normalized_chunks,
            "stages": {
                "queryRewriteMs": latency.get("rewrite_ms"),
                "embeddingMs": latency.get("embedding_ms"),
                "retrievalMs": (
                    latency.get("retrieval_ms")
                    if latency.get("retrieval_ms") is not None
                    else result.get("latencyMs")),
                "embeddingRetrievalMs": latency.get("embedding_search_ms"),
                "fusionMs": latency.get("fusion_ms"),
                "rerankMs": latency.get("rerank_ms"),
                "totalMs": latency.get("total_ms"),
            },
            "counters": counters or None,
            "rerankApplied": rerank_applied,
            "rerankProvider": result.get("rerankProvider"),
            "rerankBeforeTopScore": result.get("rerankBeforeTopScore"),
            "rerankAfterTopScore": result.get("rerankAfterTopScore"),
            "rerankLift": result.get("rerankLift"),
            "cacheHit": result.get("cacheHit")
            if isinstance(result.get("cacheHit"), bool) else None,
            "fallback": result.get("fallback")
            if isinstance(result.get("fallback"), bool) else None,
            "fallbackStage": result.get("fallbackStage"),
            "fallbackChain": result.get("fallbackChain")
            if isinstance(result.get("fallbackChain"), list) else None,
            "degraded": result.get("degraded")
            if isinstance(result.get("degraded"), bool) else None,
            "reason": result.get("reason"),
            "error": result.get("error"),
            "retrievedAt": result.get("retrievedAt"),
            "occurredAt": ended_at,
            "startedAt": result.get("_startedAt"),
            "endedAt": ended_at,
        }
        await self.emitter.emit(
            "retrieval.completed", agent_id=agent_id,
            tool_name=tool_name, payload=payload)

    def _store_jd_match_artifacts(self, result: Any) -> None:
        """Persist hybrid JD matches + effectiveJd for Tech coverage / sync."""
        if not isinstance(result, dict):
            return
        items = result.get("items") if isinstance(result.get("items"), list) else None
        if items is None and isinstance(result.get("jdMatches"), list):
            items = result["jdMatches"]
        if items is not None:
            self.state.put_artifact("jdMatches", items)
        effective = result.get("effectiveJd")
        if isinstance(effective, str) and effective.strip():
            self.state.put_artifact("effectiveJd", effective.strip())
        elif items:
            top = items[0] if isinstance(items[0], dict) else {}
            title = str(top.get("title") or "").strip()
            reasons = top.get("matchReasons") or []
            lines = [f"岗位：{title}"] if title else []
            for reason in reasons[:6]:
                if reason:
                    lines.append(f"- {reason}")
            if lines:
                self.state.put_artifact("effectiveJd", "\n".join(lines))

    def _resume_facts_from_parse(self, parsed: Any) -> Optional[Dict[str, Any]]:
        """Map deterministic parse_resume output into resumeFacts artifact.

        Always returns facts when parse succeeded — downstream agents MUST
        have material to work with even for short/unstructured resumes.
        Always includes rawExcerpt so agents can read the original text.
        """
        if not isinstance(parsed, dict) or not parsed.get("success"):
            return None
        sections = parsed.get("sections") if isinstance(parsed.get("sections"), dict) else {}
        skills = list(parsed.get("skills") or [])
        projects = list(parsed.get("projectNames") or [])
        experiences = list(sections.get("experience") or [])[:20]
        education = list(sections.get("education") or [])[:12]
        completeness = 0
        if skills:
            completeness += 1
        if projects or sections.get("projects"):
            completeness += 1
        if experiences:
            completeness += 1
        if education:
            completeness += 1
        if parsed.get("timelinePeriods"):
            completeness += 1
        resume_text = (self.request.resumeText or "").strip()
        return {
            "rawExcerpt": resume_text[:3000],
            "skills": skills[:40],
            "projects": [{"name": p} for p in projects[:12]],
            "experiences": [{"raw": e} for e in experiences],
            "education": [{"raw": e} for e in education],
            "contact": parsed.get("contact") or {},
            "timelinePeriods": parsed.get("timelinePeriods") or [],
            "source": "parse_resume_fast_path",
            "completeness": completeness,
            "confidence": float(parsed.get("confidence") or 0.8),
        }

    def _maybe_skip_parser_llm(self, tool_results_block: str) -> Optional[AgentOutput]:
        facts = self.state.data.get("artifacts", {}).get("resumeFacts")
        if not isinstance(facts, dict):
            return None
        if facts.get("source") not in ("parse_resume_fast_path", "raw_text_fallback"):
            return None
        summary = (f"确定性解析完成：技能 {len(facts.get('skills') or [])}、"
                   f"项目 {len(facts.get('projects') or [])}、"
                   f"经历 {len(facts.get('experiences') or [])}")
        return AgentOutput(
            agentId="ResumeParserAgent",
            type="resume_facts",
            claims=[{"text": summary, "confidence": facts.get("confidence", 0.8)}],
            artifacts={"resumeFacts": facts},
            evidence=[],
            confidence=float(facts.get("confidence") or 0.8),
            source="tools",
            dependencies=[],
            requestedNextAction=None,
            summary=summary[:500])

    def _extract_candidate_urls(self, resume_text: str) -> List[str]:
        """Extract verifiable candidate URLs from resume (GitHub, LinkedIn, blog, portfolio)."""
        import re as _re
        url_pattern = _re.compile(
            r'https?://(?:github\.com|linkedin\.com|gitee\.com|'
            r'blog\.\w+|[\w-]+\.github\.io|portfolio|[\w-]+\.vercel\.app)'
            r'[^\s\)\]<>\"\']*', _re.IGNORECASE)
        urls = url_pattern.findall(resume_text or "")
        seen = set()
        unique = []
        for u in urls:
            u_clean = u.rstrip(".,;:)")
            if u_clean not in seen:
                seen.add(u_clean)
                unique.append(u_clean)
        return unique

    async def _build_evidence_search_query_llm(self, resume: str, artifacts: Dict[str, Any],
                                                agent_id: str) -> str:
        """Compatibility wrapper with no hidden provider call.

        Native tool arguments are model-authored in the visible agent turn;
        legacy callers receive the deterministic fallback.
        """
        return self._build_evidence_search_query_fallback(resume, artifacts)

    def _build_evidence_search_query_fallback(self, resume: str, artifacts: Dict[str, Any]) -> str:
        """Regex fallback when LLM unavailable."""
        import re as _re
        facts = artifacts.get("resumeFacts") or {}
        parts = []
        if isinstance(facts, dict):
            experiences = facts.get("experiences") or []
            if experiences and isinstance(experiences, list):
                exp = experiences[0] if isinstance(experiences[0], dict) else {}
                company = exp.get("company") or ""
                role = exp.get("role") or exp.get("title") or ""
                if company and len(company) >= 2:
                    parts.append(company)
                if role:
                    parts.append(role[:20])
            skills = facts.get("skills") or []
            if isinstance(skills, list) and skills:
                top_skills = [s for s in skills[:5]
                              if isinstance(s, str) and len(s) >= 2]
                parts.extend(top_skills[:3])
        if not parts:
            companies = _re.findall(
                r"(字节跳动|阿里巴巴|腾讯|美团|百度|京东|华为|快手|小红书|"
                r"拼多多|网易|滴滴|蚂蚁|[\u4e00-\u9fa5]{2,6}(?:科技|网络|公司))",
                resume)
            if companies:
                parts.append(companies[0])
            tech = _re.findall(
                r"\b(Spring\s*Boot|Redis|Kafka|Go|Kubernetes|Docker|"
                r"MySQL|微服务|分布式|RAG|LLM|Agent)\b", resume, _re.IGNORECASE)
            parts.extend(tech[:3])
        if not parts:
            return ""
        return " ".join(parts[:4])

    def _build_evidence_search_query(self, resume: str, artifacts: Dict[str, Any]) -> str:
        """Sync wrapper - used by plan builder. Returns fallback only."""
        return self._build_evidence_search_query_fallback(resume, artifacts)

    def _fallback_search_query(self, resume: str) -> str:
        """Produce a claim-based search query — NEVER use candidate name."""
        import re as _re
        lines = (resume or "").strip().split("\n")[:30]
        companies = []
        techs = []
        metrics = []
        for line in lines:
            clean = line.strip()
            if not clean:
                continue
            company_match = _re.search(
                r"(字节跳动|阿里巴巴|腾讯|美团|百度|京东|华为|快手|"
                r"[\u4e00-\u9fa5]{2,6}(?:科技|网络|信息|技术|集团|公司|互联网))",
                clean)
            if company_match:
                companies.append(company_match.group(1))
            tech_match = _re.findall(
                r"\b(Spring\s*Boot|Redis|Kafka|Go|Kubernetes|Docker|"
                r"MySQL|微服务|分布式|RAG|LLM|Flink|Spark|Vue|React)\b",
                clean, _re.IGNORECASE)
            techs.extend(tech_match[:2])
            metric_match = _re.search(
                r"(QPS|延迟|性能|吞吐|并发).{0,10}(提升|降低|优化).{0,8}\d+",
                clean)
            if metric_match:
                metrics.append(clean[:40])
        if companies and techs:
            return f"{companies[0]} {' '.join(techs[:2])} 技术实践"
        if techs:
            return f"{' '.join(techs[:3])} 最佳实践 架构"
        if companies:
            return f"{companies[0]} 后端开发 技术栈"
        return ""

    def _fallback_project_query(self, resume: str, artifacts: Dict[str, Any]) -> str:
        """Always produce a project search query from resume content."""
        import re as _re
        lines = (resume or "").strip().split("\n")
        for line in lines:
            proj_match = _re.search(
                r"(?:项目[名称]*[:：]\s*|(?:独立|个人|开源)项目\s*[:：]?\s*)(.{4,30})",
                line)
            if proj_match:
                return proj_match.group(1).strip()
        tech_keywords = _re.findall(
            r"\b(Spring\s*Boot|Spring\s*Cloud|Vue|React|Next\.?js|Django|"
            r"FastAPI|Kubernetes|Flink|Spark|TensorFlow|PyTorch|LangChain)\b",
            resume, _re.IGNORECASE)
        if tech_keywords:
            return f"{tech_keywords[0]} 项目 架构"
        companies = _re.findall(
            r"([\u4e00-\u9fa5]{2,8}(?:科技|网络|信息|技术|集团|公司|互联网))", resume)
        if companies:
            return f"{companies[0]} 技术项目 开发"
        return "软件开发项目 技术架构 实践"

    def _build_project_search_query(self, resume: str, artifacts: Dict[str, Any]) -> str:
        """Build a search query for project verification using specific claims."""
        import re as _re
        facts = artifacts.get("resumeFacts") or {}
        if isinstance(facts, dict):
            projects = facts.get("projects") or []
            if projects and isinstance(projects, list):
                proj = projects[0] if isinstance(projects[0], dict) else {}
                proj_name = proj.get("name") or proj.get("title") or ""
                tech = proj.get("techStack") or proj.get("tech") or ""
                if proj_name and len(proj_name) > 3:
                    query = proj_name
                    if isinstance(tech, list):
                        query += " " + " ".join(tech[:2])
                    elif isinstance(tech, str):
                        query += " " + tech[:30]
                    return query.strip()
        proj_lines = _re.findall(
            r"(?:项目[名称]*[:：]\s*|(?:\d+)[.、]\s*)(.{4,30}?)(?:\s*[\(（]|$)",
            resume[:1500])
        if proj_lines:
            proj_name = proj_lines[0].strip()
            techs = _re.findall(
                r"\b(Go|Java|Python|Redis|Kafka|Flink|Spring|Vue|React|"
                r"Kubernetes|Docker|ClickHouse|gRPC)\b",
                resume[:800], _re.IGNORECASE)
            if techs:
                return f"{proj_name} {' '.join(list(dict.fromkeys(techs))[:2])} 架构"
            return f"{proj_name} 技术架构 实现"
        techs = _re.findall(
            r"\b(Spring\s*Boot|Redis|Kafka|Go|Kubernetes|Docker|"
            r"Flink|ClickHouse|gRPC|微服务)\b", resume[:1000], _re.IGNORECASE)
        if techs:
            return f"{' '.join(list(dict.fromkeys(techs))[:3])} 高并发 架构设计"
        return ""

    def _maybe_skip_evidence_llm(self, tool_results_block: str) -> Optional[AgentOutput]:
        """Skip Evidence LLM when deterministic verify is clean and support is high.
        Never skip when MCP tools produced results (external verification happened)."""
        support = self.state.evidence_support_ratio()
        conflicts = self.state.artifact("conflicts") or []
        if conflicts:
            return None
        if support is None or support < 0.85:
            return None
        if "verify_report_evidence" not in tool_results_block:
            return None
        if "fetch.fetch" in tool_results_block:
            return None
        if "exa.web_search_exa" in tool_results_block:
            return None
        summary = f"确定性核验通过：支持率 {support:.2f}，无冲突，跳过 Evidence LLM"
        return AgentOutput(
            agentId="EvidenceAgent",
            type="evidence",
            claims=[],
            artifacts={"evidence": [{"text": summary, "verified": True,
                                     "byAgent": "EvidenceAgent", "fastPath": True}]},
            evidence=[{"text": summary, "verified": True,
                       "byAgent": "EvidenceAgent"}],
            confidence=max(0.7, float(support)),
            source="tools",
            dependencies=[],
            requestedNextAction=None,
            summary=summary[:500])

    def _maybe_skip_jd_llm(self) -> Optional[AgentOutput]:
        """Skip JDAnalysis LLM when JD is short text provided directly."""
        jd = (self.request.jobDescription or "").strip()
        effective = self.state.artifact("effectiveJd")
        if isinstance(effective, str) and effective.strip():
            jd = effective.strip()
        if not jd or len(jd) > 800:
            return None
        requirements = {"rawJd": jd, "source": "direct_text_fast_path"}
        lines = [l.strip() for l in jd.replace("；", "\n").replace("、", "\n").split("\n") if l.strip()]
        must_have = [l for l in lines if any(k in l for k in ("要求", "必须", "精通", "熟悉", "年以上", "经验"))]
        nice_to_have = [l for l in lines if l not in must_have and len(l) > 4]
        requirements["mustHave"] = must_have[:10]
        requirements["niceToHave"] = nice_to_have[:8]
        requirements["title"] = lines[0] if lines else ""
        self.state.put_artifact("jdRequirements", requirements)
        summary = f"JD 确定性提取完成：{len(must_have)} 必需 + {len(nice_to_have)} 优选"
        return AgentOutput(
            agentId="JDAnalysisAgent",
            type="jd_requirements",
            claims=[{"text": summary, "confidence": 0.8}],
            artifacts={"jdRequirements": requirements},
            evidence=[],
            confidence=0.8,
            source="tools",
            dependencies=[],
            requestedNextAction=None,
            summary=summary)

    @staticmethod
    def render_report(report: Dict[str, Any]) -> str:
        """Deterministic Markdown from a validated structured report."""
        score = report.get("overallScore")
        recommendation = report.get("recommendation") or "NEED_MANUAL_REVIEW"
        quality = report.get("dataQuality") or "SUFFICIENT"
        sections: List[str] = []
        title_bits = []
        if isinstance(score, int):
            title_bits.append(f"综合评分 {score}")
        title_bits.append(f"建议 {recommendation}")
        sections.append("# 简历评估报告\n\n" + " · ".join(title_bits))
        if report.get("summary"):
            sections.append(f"## 结论\n\n{report['summary']}")
        dimensions = report.get("dimensions") or []
        if dimensions:
            lines = ["## 维度评分\n"]
            for dim in dimensions:
                if not isinstance(dim, dict):
                    continue
                name = dim.get("name", "")
                status = str(dim.get("status") or "").upper()
                dim_score = dim.get("score")
                rationale = dim.get("rationale") or ""
                if status == "UNASSESSED" or dim_score is None:
                    score_part = "未评估"
                else:
                    score_part = f"{dim_score} 分"
                    if status == "PARTIAL":
                        score_part += "（部分证据）"
                lines.append(f"- **{name}**：{score_part}"
                             + (f" — {rationale}" if rationale else ""))
                refs = dim.get("evidenceRefs") or []
                for ref in refs[:3]:
                    if isinstance(ref, dict) and ref.get("quote"):
                        loc = ""
                        if ref.get("lineStart") is not None:
                            end = ref.get("lineEnd") or ref.get("lineStart")
                            loc = f" L{ref['lineStart']}-{end}"
                        lines.append(
                            f"  - 证据[{ref.get('sourceType', '?')}{loc}]："
                            f"{str(ref['quote'])[:120]}")
            sections.append("\n".join(lines))
        strengths = [str(v) for v in (report.get("strengths") or []) if str(v).strip()]
        if strengths:
            sections.append("## 优势\n\n" + "\n".join(f"- {v}" for v in strengths))
        risks = report.get("risks") or []
        if risks:
            lines = ["## 候选人风险\n"]
            for risk in risks:
                if isinstance(risk, str):
                    lines.append(f"- {risk}")
                    continue
                if not isinstance(risk, dict):
                    continue
                sev = risk.get("severity") or "MEDIUM"
                claim = risk.get("claim") or ""
                impact = risk.get("impact") or ""
                lines.append(f"- **[{sev}]** {claim}"
                             + (f"（影响：{impact}）" if impact else ""))
                plan = risk.get("verificationPlan") or ""
                if plan:
                    lines.append(f"  - 核实：{plan}")
                for ref in (risk.get("evidenceRefs") or [])[:2]:
                    if isinstance(ref, dict) and ref.get("quote"):
                        lines.append(f"  - 证据：{str(ref['quote'])[:120]}")
            sections.append("\n".join(lines))
        probes = report.get("interviewProbes") or report.get("interviewQuestions") or []
        if probes:
            lines = ["## 面试追问\n"]
            for probe in probes:
                if isinstance(probe, str):
                    lines.append(f"- {probe}")
                    continue
                if not isinstance(probe, dict):
                    continue
                q = probe.get("question") or ""
                lines.append(f"- {q}")
                if probe.get("objective"):
                    lines.append(f"  - 考察点：{probe['objective']}")
                goods = [str(s) for s in (probe.get("goodSignals") or []) if str(s).strip()]
                if goods:
                    lines.append(f"  - 好信号：{'；'.join(goods[:3])}")
                flags = [str(s) for s in (probe.get("redFlags") or []) if str(s).strip()]
                if flags:
                    lines.append(f"  - 红旗：{'；'.join(flags[:3])}")
            sections.append("\n".join(lines))
        missing = [str(v) for v in (report.get("missingEvidence") or []) if str(v).strip()]
        if missing:
            sections.append("## 证据缺口\n\n" + "\n".join(f"- {v}" for v in missing))
        warnings = report.get("systemWarnings") or []
        if warnings:
            lines = ["## 系统告警\n"]
            for warn in warnings:
                if not isinstance(warn, dict):
                    continue
                code = warn.get("code") or "WARNING"
                msg = warn.get("message") or ""
                stage = warn.get("stage") or ""
                retry = "可重试" if warn.get("retryable") else "不可重试"
                lines.append(f"- **{code}**"
                             + (f"@{stage}" if stage else "")
                             + f"（{retry}）：{msg}")
            sections.append("\n".join(lines))
        if quality and quality != "SUFFICIENT":
            sections.append(f"## 数据质量\n\n- {quality}")
        return "\n\n".join(sections).strip()

    @staticmethod
    def _parse_source_refs(raw: Any) -> List[Dict[str, Any]]:
        refs: List[Dict[str, Any]] = []
        if not isinstance(raw, list):
            return refs
        allowed = {"RESUME", "JD", "KNOWLEDGE", "EXTERNAL"}
        _LINE_RE = re.compile(
            r"\[?(RESUME|JD|KNOWLEDGE|EXTERNAL)\s*L?(\d+)(?:-L?(\d+))?\]?\s*(.*)",
            re.I)
        for item in raw:
            if isinstance(item, str):
                text = item.strip()
                if not text:
                    continue
                m = _LINE_RE.match(text)
                if m:
                    refs.append({
                        "sourceType": m.group(1).upper(),
                        "sourceId": m.group(1).lower(),
                        "quote": (m.group(4) or text)[:400],
                        "lineStart": int(m.group(2)),
                        "lineEnd": int(m.group(3)) if m.group(3) else int(m.group(2)),
                    })
                else:
                    source = "RESUME" if "resume" in text.lower() or "简历" in text else "JD"
                    refs.append({
                        "sourceType": source,
                        "sourceId": source.lower(),
                        "quote": text[:400],
                    })
                continue
            if not isinstance(item, dict):
                continue
            source_type = str(item.get("sourceType") or "").strip().upper()
            quote = str(item.get("quote") or "").strip()
            source_id = str(item.get("sourceId") or "").strip()
            if source_type not in allowed or not quote or not source_id:
                continue
            entry: Dict[str, Any] = {
                "sourceType": source_type,
                "sourceId": source_id[:120],
                "quote": quote[:400],
            }
            for key in ("lineStart", "lineEnd"):
                val = item.get(key)
                if isinstance(val, (int, float)) and int(val) >= 0:
                    entry[key] = int(val)
            if item.get("uri"):
                entry["uri"] = str(item["uri"])[:300]
            refs.append(entry)
        return refs[:8]

    @staticmethod
    def _claim_has_control_plane_noise(claim: str) -> bool:
        text = claim or ""
        upper = text.upper()
        if any(code in upper for code in CONTROL_PLANE_ERROR_CODES):
            return True
        markers = (
            "CONTROL_PLANE", "ORPHANED_ON_RESTART", "RUNTIME_START_FAILED",
            "START_STUCK", "WORKER", "QUEUE_STUCK", "控制面",
        )
        return any(m in upper or m in text for m in markers)

    @staticmethod
    def _validate_structured_report(report: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Validate structured report; overallScore is computed only when
        enough core dimensions are evidence-assessed (never from the model,
        never by inventing 0 for UNASSESSED)."""
        allowed_recommendations = {
            "HIRE", "INTERVIEW_RECOMMEND", "NEED_MANUAL_REVIEW", "NOT_RECOMMEND"}
        legacy_recommendations = {
            "STRONG_RECOMMEND": "HIRE",
            "RECOMMEND": "INTERVIEW_RECOMMEND",
            "MANUAL_REVIEW": "NEED_MANUAL_REVIEW",
            "REVIEW": "NEED_MANUAL_REVIEW",
        }
        out: Dict[str, Any] = {}
        recommendation = str(report.get("recommendation") or "").strip().upper()
        recommendation = legacy_recommendations.get(recommendation, recommendation)
        if recommendation in allowed_recommendations:
            out["recommendation"] = recommendation
        quality = str(report.get("dataQuality") or "SUFFICIENT").strip().upper()
        if quality not in {"SUFFICIENT", "PARTIAL", "INSUFFICIENT"}:
            quality = "SUFFICIENT"
        out["dataQuality"] = quality

        system_warnings: List[Dict[str, Any]] = []
        for warn in report.get("systemWarnings") or []:
            if not isinstance(warn, dict):
                continue
            code = str(warn.get("code") or "").strip()
            message = str(warn.get("message") or "").strip()
            if not code or not message:
                continue
            system_warnings.append({
                "code": code[:80],
                "stage": str(warn.get("stage") or "")[:80],
                "retryable": bool(warn.get("retryable", False)),
                "message": message[:400],
            })

        dimensions: List[Dict[str, Any]] = []
        assessed_core: List[Tuple[str, int]] = []
        for dim in report.get("dimensions") or []:
            if not isinstance(dim, dict) or not dim.get("name"):
                continue
            name = str(dim["name"])[:60]
            status = str(dim.get("status") or "").strip().upper()
            dim_score = dim.get("score")
            score_val: Optional[int] = None
            if isinstance(dim_score, (int, float)) and 0 <= float(dim_score) <= 100:
                score_val = int(round(float(dim_score)))

            if status not in {"ASSESSED", "UNASSESSED", "PARTIAL"}:
                status = "UNASSESSED" if score_val is None else "ASSESSED"

            # UNASSESSED must keep score=null — never coerce missing evidence to 0.
            if status == "UNASSESSED":
                score_val = None

            refs = RunExecutor._parse_source_refs(
                dim.get("evidenceRefs") or dim.get("evidence"))
            try:
                coverage = float(dim.get("evidenceCoverage", 0.0) or 0.0)
            except (TypeError, ValueError):
                coverage = 0.0
            if refs and coverage <= 0:
                coverage = min(1.0, 0.35 * len(refs))
            coverage = max(0.0, min(1.0, coverage))

            # Downgrade status when no evidence at all and no score.
            if status == "ASSESSED" and not refs and score_val is None:
                status = "UNASSESSED"
            elif status == "ASSESSED" and not refs:
                status = "PARTIAL"

            entry: Dict[str, Any] = {
                "name": name,
                "status": status,
                "evidenceCoverage": round(coverage, 3),
                "score": score_val,
            }
            if dim.get("rationale"):
                entry["rationale"] = str(dim["rationale"])[:300]
            if refs:
                entry["evidenceRefs"] = refs
            dimensions.append(entry)

            key = name.replace(" ", "").lower()
            # Allow dimensions with score + rationale to count toward
            # overallScore even without strict SourceRef format.
            has_rationale = bool(dim.get("rationale"))
            scorable = (status in {"ASSESSED", "PARTIAL"}
                        and score_val is not None
                        and (bool(refs) or has_rationale)
                        and key in _CORE_DIMENSION_KEYS)
            if scorable:
                assessed_core.append((name, score_val))

        if dimensions:
            out["dimensions"] = dimensions[:10]

        # overallScore when ≥2 core dimensions have scores (rationale or refs).
        if len(assessed_core) >= 2:
            weight_sum = 0.0
            weighted = 0.0
            for name, score in assessed_core:
                key = name.replace(" ", "").lower()
                weight = next(
                    (w for n, w in _DIMENSION_WEIGHTS.items()
                     if n.replace(" ", "").lower() == key),
                    None)
                if weight is None:
                    continue
                weighted += score * weight
                weight_sum += weight
            if weight_sum > 0:
                out["overallScore"] = int(round(weighted / weight_sum))
            else:
                out["overallScore"] = int(
                    round(sum(s for _, s in assessed_core) / len(assessed_core)))
        else:
            out.pop("overallScore", None)

        strengths = [str(v)[:300] for v in (report.get("strengths") or [])
                     if isinstance(v, (str, int, float)) and str(v).strip()]
        if strengths:
            out["strengths"] = strengths[:12]

        risks_out: List[Dict[str, Any]] = []
        for idx, risk in enumerate(report.get("risks") or []):
            # Legacy string risks lack evidence → reject (do not promote).
            if isinstance(risk, str):
                text = risk.strip()
                if not text:
                    continue
                if RunExecutor._claim_has_control_plane_noise(text):
                    system_warnings.append({
                        "code": "CONTROL_PLANE_IN_RISK_CLAIM",
                        "stage": "report_validation",
                        "retryable": False,
                        "message": f"控制面噪声已从候选人风险剔除：{text[:200]}",
                    })
                continue
            if not isinstance(risk, dict):
                continue
            claim = str(risk.get("claim") or risk.get("risk") or "").strip()
            if not claim:
                continue
            category = str(risk.get("category") or "CANDIDATE").strip().upper()
            if category in _PROCESS_DATA_CATEGORIES:
                system_warnings.append({
                    "code": str(risk.get("id") or category)[:80],
                    "stage": "report",
                    "retryable": bool(risk.get("retryable", False)),
                    "message": claim[:400],
                })
                continue
            if RunExecutor._claim_has_control_plane_noise(claim):
                system_warnings.append({
                    "code": "CONTROL_PLANE_IN_RISK_CLAIM",
                    "stage": "report_validation",
                    "retryable": False,
                    "message": f"控制面错误码不得进入候选人风险：{claim[:200]}",
                })
                continue
            refs = RunExecutor._parse_source_refs(risk.get("evidenceRefs"))
            severity = str(risk.get("severity") or "MEDIUM").strip().upper()
            if severity not in {"HIGH", "MEDIUM", "LOW"}:
                severity = "MEDIUM"
            entry = {
                "id": str(risk.get("id") or f"risk-{idx + 1}")[:60],
                "category": "CANDIDATE",
                "severity": severity,
                "claim": claim[:400],
                "impact": str(risk.get("impact") or "")[:300],
                "verificationPlan": str(risk.get("verificationPlan") or risk.get("verification") or "")[:300],
            }
            if refs:
                entry["evidenceRefs"] = refs
            conf = risk.get("confidence")
            if isinstance(conf, (int, float)):
                entry["confidence"] = max(0.0, min(1.0, float(conf)))
            risks_out.append(entry)
        if risks_out:
            out["risks"] = risks_out[:12]

        probes_raw = report.get("interviewProbes")
        if not isinstance(probes_raw, list) or not probes_raw:
            probes_raw = report.get("interviewQuestions") or []
        probes_out: List[Dict[str, Any]] = []
        for idx, probe in enumerate(probes_raw):
            if isinstance(probe, str):
                # Legacy bare questions lack evidence/signals → reject
                continue
            if not isinstance(probe, dict):
                continue
            question = str(probe.get("question") or "").strip()
            if not question:
                continue
            refs = RunExecutor._parse_source_refs(probe.get("evidenceRefs"))
            good = [str(s)[:200] for s in (probe.get("goodSignals") or probe.get("expectedSignals") or [])
                    if isinstance(s, (str, int, float)) and str(s).strip()]
            entry = {
                "id": str(probe.get("id") or f"probe-{idx + 1}")[:60],
                "priority": (
                    p if (p := str(probe.get("priority") or "MEDIUM").strip().upper())
                    in {"HIGH", "MEDIUM", "LOW"} else "MEDIUM"
                ),
                "question": question[:400],
                "objective": str(probe.get("objective") or probe.get("whyAsk") or "")[:300],
                "triggeredBy": str(probe.get("triggeredBy") or "")[:200],
                "evidenceRefs": refs,
                "goodSignals": good[:8],
                "redFlags": [str(s)[:200] for s in (probe.get("redFlags") or [])
                             if isinstance(s, (str, int, float)) and str(s).strip()][:8],
                "followUps": [str(s)[:200] for s in (probe.get("followUps") or [])
                              if isinstance(s, (str, int, float)) and str(s).strip()][:6],
                "scoreRubric": str(probe.get("scoreRubric") or "")[:300],
            }
            probes_out.append(entry)
        if probes_out:
            out["interviewQuestions"] = probes_out[:12]
            out["interviewProbes"] = probes_out[:12]

        missing = [str(v)[:300] for v in (report.get("missingEvidence") or [])
                   if isinstance(v, (str, int, float)) and str(v).strip()]
        if missing:
            out["missingEvidence"] = missing[:12]
        if system_warnings:
            out["systemWarnings"] = system_warnings[:20]
        if report.get("summary"):
            out["summary"] = str(report["summary"])[:500]
        # Require the core contract fields for a non-empty artifact.
        if "recommendation" not in out and "dimensions" not in out:
            return None
        return out or None

    async def _prepare_context(self) -> None:
        """Deterministic preflight before Coordinator planning.

        Ensures resumeFacts / parsedResume / effectiveJd / jdMatches live in
        the canonical artifact store so subsequent agents never claim
        "原文缺失" when the raw inputs were actually provided.
        """
        request = self.request
        resume = (request.resumeText or "").strip()
        jd = (request.jobDescription or "").strip()
        arts = self.state.artifacts()

        self.state.set_input_presence(
            resume_chars=len(resume),
            jd_chars=len(jd),
            has_jd_matches=bool(arts.get("jdMatches")))

        await self.emitter.emit("run.progress", payload={
            "stage": "preflight",
            "message": "确定性上下文预热",
            "resumeChars": len(resume),
            "jdChars": len(jd)})

        if resume and not arts.get("parsedResume"):
            call = await self.tools.execute(
                "CoordinatorAgent", "parse_resume", {"resumeText": resume})
            if call.status == "SUCCEEDED" and isinstance(call.result, dict):
                self.state.apply_artifacts({"parsedResume": call.result})
                facts = self._resume_facts_from_parse(call.result)
                if facts:
                    self.state.apply_artifacts({"resumeFacts": facts})

        # Guarantee resumeFacts exists when resume text is available — even a
        # minimal stub so specialists always have material to work with.
        arts = self.state.artifacts()
        if resume and not arts.get("resumeFacts"):
            self.state.apply_artifacts({"resumeFacts": {
                "rawExcerpt": resume[:3000],
                "skills": [],
                "projects": [],
                "experiences": [],
                "education": [],
                "contact": {},
                "timelinePeriods": [],
                "source": "raw_text_fallback",
                "completeness": 0,
                "confidence": 0.3,
            }})

        if jd and not arts.get("effectiveJd"):
            self.state.apply_artifacts({"effectiveJd": jd})

        if resume and not arts.get("jdMatches"):
            call = await self.tools.execute(
                "CoordinatorAgent", "jd_match_search", {"resumeText": resume})
            if call.status == "SUCCEEDED":
                self._store_jd_match_artifacts(call.result)

        # Refresh presence after JD match may have landed.
        arts = self.state.artifacts()
        self.state.set_input_presence(
            resume_chars=len(resume),
            jd_chars=len(jd) or len(str(arts.get("effectiveJd") or "")),
            has_jd_matches=bool(arts.get("jdMatches")))

    def _pre_steps(self, definition: AgentDefinition) -> List[tuple]:
        request = self.request
        resume = request.resumeText or ""
        artifacts = self.state.artifacts()
        steps: List[tuple] = []
        parsed_already = "parsedResume" in artifacts and bool(artifacts.get("parsedResume"))
        # ResumeParserAgent: skip re-parse when preflight already populated facts.
        if definition.agent_id == "ResumeParserAgent" and resume and not parsed_already:
            steps.append(("parse_resume", {"resumeText": resume}))
        elif definition.agent_id == "JDAnalysisAgent" and resume:
            if "jdMatches" not in artifacts:
                steps.append(("jd_match_search", {"resumeText": resume}))
        elif definition.agent_id == "RiskAgent" and resume \
                and "timelineCheck" not in artifacts:
            steps.append(("check_timeline", {"resumeText": resume}))
        elif definition.agent_id == "TechAgent" and resume \
                and "jdCoverage" not in artifacts:
            effective_jd = ""
            artifact_jd = artifacts.get("effectiveJd")
            if isinstance(artifact_jd, str) and artifact_jd.strip():
                effective_jd = artifact_jd.strip()
            elif (request.jobDescription or "").strip():
                effective_jd = request.jobDescription.strip()
            if effective_jd:
                steps.append(("calculate_jd_coverage",
                              {"resumeText": resume, "jdText": effective_jd}))
        elif definition.agent_id == "EvidenceAgent" and resume:
            claims = self.state.claims_for_verification()
            if claims:
                steps.append(("verify_report_evidence",
                              {"resumeText": resume,
                               "jdText": (artifacts.get("effectiveJd")
                                          or request.jobDescription or ""),
                               "claims": claims}))
        elif definition.agent_id == "ResumeOptimizeAgent" and resume:
            steps.append(("resume_lint", {"resumeText": resume}))
        # Knowledge RAG: inject relevant evaluation guidelines from KB
        if definition.agent_id in ("ReportAgent", "TechAgent") and resume:
            kb_query = self._build_knowledge_query(definition.agent_id, resume, artifacts, request)
            if kb_query:
                steps.append(("knowledge_search", {"query": kb_query}))
        # Copilot 对话式 RAG
        if definition.agent_id == "ReportAgent" \
                and request.runType in ("followup", "quick_answer") \
                and (request.userMessage or "").strip():
            query = request.userMessage.strip()[:200]
            steps.append(("knowledge_search", {"query": query}))
            if resume:
                steps.append(("resume_semantic_search", {"query": query}))
        return steps

    def _build_knowledge_query(self, agent_id: str, resume: str,
                               artifacts: Dict[str, Any],
                               request: Any) -> str:
        """Build a query to retrieve relevant KB evaluation guidelines."""
        import re as _re
        jd = (artifacts.get("effectiveJd") or
              getattr(request, "jobDescription", "") or "")
        if agent_id == "TechAgent":
            techs = _re.findall(
                r"\b(Java|Python|Go|Spring|Redis|Kafka|Docker|K8s|"
                r"RAG|LLM|微服务|分布式|前端|后端|AI)\b",
                jd + " " + resume[:500], _re.IGNORECASE)
            if techs:
                return f"技术评估 {' '.join(list(dict.fromkeys(techs))[:3])} 标准"
            return "技术能力评估标准 评分规范"
        # ReportAgent: retrieve overall evaluation policy
        return "简历评估 评分标准 录用建议 风险判断"

    def _apply_verification(self, result: Any) -> None:
        if not isinstance(result, dict):
            return
        supported = []
        unsupported = []
        for entry in result.get("supported", []) or []:
            if not isinstance(entry, dict):
                continue
            supported.append({
                "text": entry.get("claim", ""), "verified": True,
                "location": entry.get("location"), "byAgent": "EvidenceAgent"})
        for entry in result.get("unsupported", []) or []:
            if not isinstance(entry, dict):
                continue
            unsupported.append({
                "text": entry.get("claim", ""), "verified": False,
                "reason": entry.get("reason"), "byAgent": "EvidenceAgent"})
            self.state.add_conflict({
                "type": "unsupported_claim", "claim": entry.get("claim", ""),
                "reason": entry.get("reason", ""), "byAgent": "EvidenceAgent"})
        if supported or unsupported:
            self.state.apply_artifacts({"evidence": supported + unsupported})

    def _policy_instructions(self) -> str:
        ev = self.policy.evidenceVerification
        lines = [
            f"当前策略: {self.policy.policyId}",
            f"证据核验: {'严格' if ev.strict else '启用' if ev.enabled else '关闭'}"
            f"（最低支持率 {ev.minSupportRatio}）",
            f"预算: LLM≤{self.policy.maxLlmCalls} 次, 工具≤{self.policy.toolBudget.maxToolCallsPerRun} 次",
        ]
        if self.policy.jobFocus:
            lines.append(f"岗位侧重: {self.policy.jobFocus}")
        return "\n".join(lines)

    def _memory_context(
            self, definition: AgentDefinition
            ) -> Tuple[str, Optional[Dict[str, Any]]]:
        if definition.memory_policy == "none":
            return "", None
        agent_id = definition.agent_id
        # Coordinator may additionally see FAILURE hits for planning hints;
        # Report/Risk and other specialists only see the evaluation-safe pool.
        pool = list(self.memory_hits)
        if agent_id == "CoordinatorAgent":
            pool = pool + list(self.failure_hits)
        if not pool:
            return "", None
        used, ignored = filter_hits_for_consumer(pool, agent_id)
        trace = memory_trace_entries(used, ignored, agent_id)
        self.memory_traces.extend(trace)
        decisions = decisions_from_hits(used, ignored, agent_id)
        audit = {
            "consumerAgent": agent_id,
            "usedCount": len(used),
            "ignoredCount": len(ignored),
            "memoryTrace": trace[:20],
            "decisions": decisions,
        }
        lines = ["[相关记忆]"]
        insights = []
        anchors = []
        context = []
        for hit in used[: self.policy.memoryRetrieval.topK]:
            content = str(hit.get("content", ""))[:400]
            source = hit.get("source") or "?"
            if "对比锚点" in content or source == "cross_candidate_anchor":
                anchors.append(f"  {content}")
            elif "评估洞察" in content or "证据" in content or source == "evaluation_insight":
                insights.append(f"  {content}")
            else:
                context.append(f"  [{hit.get('type')}|src={source}] {content}")
        if insights:
            lines.append("# 历史评估洞察")
            lines.extend(insights[:3])
        if anchors:
            lines.append("# 同岗位对比基准")
            lines.extend(anchors[:3])
        if context:
            lines.append("# 上下文")
            lines.extend(context[:2])
        block = "\n".join(lines) if len(lines) > 1 else ""
        return block, audit

    @staticmethod
    def _format_tool_result(call: Any) -> str:
        preview = json.dumps(call.result, ensure_ascii=False)[:1500] \
            if call.result is not None else (call.error or "")[:400]
        return (f"\n[TOOL_CALL {call.tool} id={call.tool_call_id}]"
                f"\n[TOOL_RESULT {call.tool} id={call.tool_call_id} "
                f"status={call.status}] {preview}")

    async def _emit_guard(self, guard: Any, agent_id: str) -> None:
        await self.emitter.emit("run.progress", agent_id=agent_id, payload={
            "stage": "loop_guard", "kind": guard.kind,
            "detail": guard.detail, "action": guard.action})

    def _degraded_answer(self, reason: str) -> str:
        """Best-effort answer from whatever the blackboard already holds.
        Always labelled — degraded output is never disguised as a report."""
        arts = self.state.artifacts()
        sections: List[str] = [f"> 说明：{reason}，以下为基于已完成分析的降级结果（非完整报告）。\n"]
        if arts.get("technicalFindings"):
            sections.append("## 技术发现\n" + "\n".join(
                f"- {e.get('text', json.dumps(e, ensure_ascii=False)[:160])}"
                for e in (arts.get("technicalFindings") or [])[:8] if isinstance(e, dict)))
        if arts.get("risks"):
            sections.append("## 风险\n" + "\n".join(
                f"- {e.get('text', json.dumps(e, ensure_ascii=False)[:160])}"
                for e in (arts.get("risks") or [])[:8] if isinstance(e, dict)))
        if arts.get("conflicts"):
            sections.append("## 证据不足/冲突\n" + "\n".join(
                f"- {c.get('claim', c.get('key', ''))}"
                for c in (arts.get("conflicts") or [])[:6] if isinstance(c, dict)))
        coverage = arts.get("jdCoverage")
        if isinstance(coverage, dict) and coverage.get("coverage") is not None:
            sections.append(f"## JD 覆盖率\n- {coverage.get('coverage')}")
        if len(sections) == 1:
            sections.append("尚未获得足够分析结果，请重试或缩小问题范围。")
        return "\n\n".join(sections)

    def _conversation_summary(self) -> str:
        arts = self.state.artifacts()
        parts = [f"目标: {(self.request.currentGoal or self.request.userMessage or '')[:150]}"]
        tf = arts.get("technicalFindings")
        if tf and isinstance(tf, list):
            parts.append("技术结论: " + "; ".join(
                str(e.get("text", ""))[:80] for e in tf[:3]
                if isinstance(e, dict)))
        rf = arts.get("risks")
        if rf and isinstance(rf, list):
            parts.append("风险: " + "; ".join(
                str(e.get("text", ""))[:80] for e in rf[:3]
                if isinstance(e, dict)))
        if arts.get("conflicts"):
            parts.append(f"未决冲突 {len(arts.get('conflicts') or [])} 项")
        return "\n".join(parts)[:1800]

    def _explicit_preferences(self) -> List[Dict[str, str]]:
        """Only preferences the user literally stated are persisted; nothing
        is inferred by the model."""
        message = self.request.userMessage or ""
        found = []
        for pattern, kind in _PREFERENCE_PATTERNS:
            match = pattern.search(message)
            if match:
                found.append({"kind": kind,
                              "text": match.group("pref").strip()[:120]})
        return found[:2]

    @staticmethod
    def _runtime_strategy_class(selected_agents: List[str]) -> Tuple[str, str]:
        selected = set(selected_agents)
        if {"ProjectAgent", "EvidenceAgent"} <= selected:
            return (
                "PROJECT_EVIDENCE",
                "项目或外部链接场景保留 ProjectAgent 与 EvidenceAgent，并为证据工具调用预留 action turn。",
            )
        if "RiskAgent" in selected:
            return (
                "RISK_TIMELINE",
                "履历风险场景保留 RiskAgent，并将时间线结论交给 EvidenceAgent 或 ReportAgent 复核。",
            )
        if {"JDAnalysisAgent", "TechAgent"} <= selected:
            return (
                "JD_TECH",
                "JD 技术匹配场景先结构化岗位要求，再由 TechAgent 逐项核对证据。",
            )
        return (
            "BASELINE",
            "轻量简历评估仅保留满足目标产物所需的最短路由，并由 ReportAgent 收口。",
        )

    async def _write_memories(self, summary: str) -> None:
        await self.emitter.emit("agent.started", agent_id="MemoryService",
                                payload={"description": "评估记忆持久化"})
        try:
            arts = self.state.artifacts()
            final_report = arts.get("finalReport") or {}

            # 0a) WORKING: raw input context for this run only.
            resume_text = self.request.resumeText or ""
            parse_output = arts.get("parsedResume") \
                if isinstance(arts.get("parsedResume"), dict) else {}
            resume_facts = arts.get("resumeFacts") \
                if isinstance(arts.get("resumeFacts"), dict) else {}
            parsed = {**parse_output, **resume_facts}
            if resume_text:
                name = ""
                if isinstance(parsed, dict):
                    name = parsed.get("name") or parsed.get("candidateName") or ""
                input_snapshot = (
                    f"候选人: {name or '未知'}. "
                    f"简历长度: {len(resume_text)}字. "
                    f"JD: {(self.request.jobDescription or '')[:100] or '未提供'}. "
                    f"运行类型: {self.request.runType}. "
                    f"关键词: {resume_text[:200]}"
                )
                await self.memory.write(
                    type_="WORKING", owner_scope="RUN",
                    content=input_snapshot[:500],
                    structured={
                        "factKey": "run_input_context",
                        "memoryKind": "working",
                        "candidateName": name,
                        "resumeLength": len(resume_text),
                        "runType": self.request.runType,
                        "hasJd": bool((self.request.jobDescription or "").strip()),
                        "topSkills": (parsed.get("skills") or [])[:8]
                                     if isinstance(parsed, dict) else [],
                    },
                    source="run_input", confidence=1.0)

            # 0b) SEMANTIC: durable candidate facts extracted from the actual
            # resume. This is deliberately not an evaluation conclusion.
            semantic_written = False
            if isinstance(parsed, dict):
                candidate_skills = [
                    str(item).strip() for item in (parsed.get("skills") or [])
                    if str(item).strip()
                ][:16]
                raw_projects = parsed.get("projects") or parsed.get("projectNames") or []
                project_names = []
                for project in raw_projects[:8] if isinstance(raw_projects, list) else []:
                    value = project.get("name") if isinstance(project, dict) else project
                    if str(value or "").strip():
                        project_names.append(str(value).strip()[:100])
                raw_experiences = parsed.get("experiences") or []
                experiences = []
                for experience in (
                        raw_experiences[:6] if isinstance(raw_experiences, list) else []):
                    value = experience.get("raw") \
                        if isinstance(experience, dict) else experience
                    if str(value or "").strip():
                        experiences.append(str(value).strip()[:140])
                raw_education = parsed.get("education") or []
                education = []
                for item in raw_education[:4] if isinstance(raw_education, list) else []:
                    value = item.get("raw") if isinstance(item, dict) else item
                    if str(value or "").strip():
                        education.append(str(value).strip()[:140])
                candidate_name = str(
                    parsed.get("name") or parsed.get("candidateName") or "").strip()
                if candidate_name or candidate_skills or project_names \
                        or experiences or education:
                    fact_parts = []
                    if candidate_name:
                        fact_parts.append(f"候选人={candidate_name[:80]}")
                    if candidate_skills:
                        fact_parts.append(f"技能={', '.join(candidate_skills[:10])}")
                    if project_names:
                        fact_parts.append(f"项目={'; '.join(project_names[:4])}")
                    if experiences:
                        fact_parts.append(f"经历={'; '.join(experiences[:3])}")
                    if education:
                        fact_parts.append(f"教育={'; '.join(education[:2])}")
                    await self.memory.write(
                        type_="SEMANTIC", owner_scope="CONVERSATION",
                        content=("候选人事实: " + " | ".join(fact_parts))[:900],
                        structured={
                            "factKey": "candidate_profile",
                            "memoryKind": "candidate_fact",
                            "candidateName": candidate_name,
                            "skills": candidate_skills,
                            "projects": project_names,
                            "experiences": experiences,
                            "education": education,
                            "sourceArtifact": (
                                "resumeFacts" if resume_facts else "parsedResume"),
                        },
                        source="candidate_fact",
                        source_id=(
                            f"candidate_profile:{self.request.conversationId}"),
                        confidence=float(parsed.get("confidence") or 0.85),
                        ttl_days=180)
                    semantic_written = True

            # 0c) WORKING: evidence & verification context.
            evidence_ledger = arts.get("evidence_ledger") or arts.get("evidenceLedger") or {}
            if isinstance(evidence_ledger, dict) and evidence_ledger:
                verified = [k for k, v in evidence_ledger.items()
                            if isinstance(v, dict) and v.get("verified")]
                unverified = [k for k, v in evidence_ledger.items()
                              if isinstance(v, dict) and not v.get("verified")]
                await self.memory.write(
                    type_="WORKING", owner_scope="RUN",
                    content=(f"证据核验: {len(verified)}项已验证, {len(unverified)}项未验证. "
                             f"已验证: {'; '.join(verified[:4])}"),
                    structured={
                        "factKey": "evidence_context",
                        "memoryKind": "working",
                        "verifiedCount": len(verified),
                        "unverifiedCount": len(unverified),
                        "verified": verified[:5],
                        "unverified": unverified[:5],
                    },
                    source="evidence_agent", confidence=0.9)

            # 1) EPISODIC: evidence-chain insight (NOT conclusion reiteration)
            if isinstance(final_report, dict) and final_report.get("recommendation"):
                rec = final_report["recommendation"]
                dims = final_report.get("dimensions") or []
                strengths = final_report.get("strengths") or []
                risks = final_report.get("risks") or []
                candidate_id = self.request.conversationId or "unknown"
                name = ""
                if isinstance(parsed, dict):
                    name = parsed.get("name") or parsed.get("candidateName") or ""

                # Extract key evidence from agent outputs
                agent_outputs = arts.get("agentOutputs") or []
                key_evidence = []
                for ao in (agent_outputs[-8:] if isinstance(agent_outputs, list) else []):
                    if isinstance(ao, dict) and ao.get("findings"):
                        for f in (ao["findings"][:2] if isinstance(ao["findings"], list) else []):
                            if isinstance(f, dict) and f.get("evidence"):
                                key_evidence.append(
                                    f"[{ao.get('agentId','?')}] {f.get('claim','')[:40]} "
                                    f"\u2190 证据: {f['evidence'][:60]}")

                # JD gap specifics
                jd_reqs = arts.get("jdRequirements") or arts.get("effectiveJd") or {}
                must_haves = (jd_reqs.get("mustHave") or jd_reqs.get("requirements") or []) \
                    if isinstance(jd_reqs, dict) else []
                candidate_skills = (parsed.get("skills") or []) if isinstance(parsed, dict) else []
                missing = [req for req in must_haves[:5]
                           if isinstance(req, str) and not any(
                               req.lower() in s.lower() for s in candidate_skills)]

                evidence_text = "\n".join(key_evidence[:4]) if key_evidence else "无具体证据链"
                gap_text = f"JD核心缺口: {', '.join(missing[:3])}" if missing else "JD要求基本覆盖"

                probes = final_report.get("interviewProbes") or []
                probe_summary = "; ".join(
                    p.get("question", "")[:40] for p in probes[:3]
                    if isinstance(p, dict) and p.get("question"))

                await self.memory.write(
                    type_="EPISODIC", owner_scope="CONVERSATION",
                    content=(f"[评估洞察] {name or '候选人'} \u2192 {rec}\n"
                             f"关键证据:\n{evidence_text}\n"
                             f"{gap_text}\n"
                             f"面试验证重点: {probe_summary[:80] if probe_summary else '待定'}"),
                    structured={
                        "factKey": f"evaluation_insight:{candidate_id}",
                        "recommendation": rec,
                        "keyEvidence": key_evidence[:4],
                        "jdGaps": missing[:3],
                        "riskCount": len(risks),
                    },
                    source="evaluation_insight", confidence=0.9)

                # 1b) EPISODIC: cross-candidate comparison anchor
                job_focus = self.request.jobDescription or ""
                if job_focus and final_report.get("overallScore"):
                    jd_score = next(
                        (d.get("score") for d in dims
                         if isinstance(d, dict) and "JD" in (d.get("name") or "").upper()),
                        None)
                    await self.memory.write(
                        type_="EPISODIC", owner_scope="USER",
                        content=(f"[对比锚点] 岗位={job_focus[:30]} | "
                                 f"候选人={name or '?'} | "
                                 f"总分={final_report.get('overallScore')} | "
                                 f"JD匹配={jd_score or '?'} | "
                                 f"推荐={rec} | "
                                 f"最大gap={missing[0] if missing else '无'}"),
                        structured={
                            "factKey": "comparison_anchor",
                            "jobFocus": job_focus[:50],
                            "candidateName": name,
                            "overallScore": final_report.get("overallScore"),
                            "recommendation": rec,
                            "topGap": missing[0] if missing else None,
                        },
                        source="cross_candidate_anchor", confidence=0.9)

            # 2) EPISODIC: key technical findings (for cross-candidate comparison)
            tech_findings = arts.get("technicalFindings") or []
            if tech_findings and isinstance(tech_findings, list):
                tech_claims = [f.get("text", "")[:60] for f in tech_findings[:5]
                               if isinstance(f, dict) and f.get("text")]
                if tech_claims:
                    await self.memory.write(
                        type_="EPISODIC", owner_scope="CONVERSATION",
                        content=f"技术发现: {'; '.join(tech_claims)}",
                        structured={"factKey": "tech_findings",
                                    "claims": tech_claims},
                        source="evaluation_result", confidence=0.85)

            # 3) EPISODIC: verified risks (for re-evaluation awareness)
            risks_found = arts.get("risks") or []
            if risks_found and isinstance(risks_found, list):
                verified_risks = [r for r in risks_found
                                  if isinstance(r, dict) and r.get("severity") in ("HIGH", "MEDIUM")]
                if verified_risks:
                    await self.memory.write(
                        type_="EPISODIC", owner_scope="CONVERSATION",
                        content=(f"已识别风险({len(verified_risks)}项): " +
                                 "; ".join(r.get("claim", "")[:50] for r in verified_risks[:4])),
                        structured={"factKey": "identified_risks",
                                    "risks": [{"claim": r.get("claim"),
                                               "severity": r.get("severity")}
                                              for r in verified_risks[:4]]},
                        source="evaluation_result", confidence=0.85)

            # 4) EPISODIC: interview probes for future reference
            probes = (final_report.get("interviewProbes") or []) if isinstance(final_report, dict) else []
            if probes and isinstance(probes, list) and len(probes) >= 2:
                probe_summary = "; ".join(
                    p.get("question", "")[:50] for p in probes[:5]
                    if isinstance(p, dict) and p.get("question"))
                if probe_summary:
                    await self.memory.write(
                        type_="EPISODIC", owner_scope="CONVERSATION",
                        content=f"面试追问要点({len(probes)}条): {probe_summary}",
                        structured={"factKey": "interview_probes",
                                    "probeCount": len(probes),
                                    "topProbes": [p.get("question", "") for p in probes[:5]
                                                  if isinstance(p, dict)]},
                        source="evaluation_result", confidence=0.85)

            # 5) PROCEDURAL: a candidate-free strategy learned from this actual
            # execution. Java stages it until terminal acceptance and validates
            # its provenance before USER-scoped promotion.
            agent_timings = getattr(self, "agent_timings", {})
            agent_counters = getattr(self, "agent_counters", {})
            selected_agents = list(dict.fromkeys(
                self.executed or list(agent_timings.keys())))
            strategy_written = False
            if ((agent_timings or agent_counters)
                    and selected_agents
                    and isinstance(final_report, dict)
                    and final_report.get("recommendation")
                    and not self.report_agent_failed):
                agent_llm_calls = {a: c.get("llmCalls", 0)
                                   for a, c in agent_counters.items() if isinstance(c, dict)}
                agent_tool_calls = {a: c.get("toolCalls", 0)
                                    for a, c in agent_counters.items() if isinstance(c, dict)}
                tool_agents = sorted(
                    agent for agent, calls in agent_tool_calls.items()
                    if int(calls or 0) > 0)
                strategy_class, strategy_hint = self._runtime_strategy_class(
                    selected_agents)
                strategy_key = (
                    f"execution_strategy:{self.request.runType}:{strategy_class}")
                await self.memory.write(
                    type_="PROCEDURAL", owner_scope="USER",
                    content=(
                        f"简历评估执行策略[{strategy_class}]: {strategy_hint} "
                        f"已验证路由={' -> '.join(selected_agents)}; "
                        f"工具参与={','.join(tool_agents) or '无'}"),
                    structured={
                        "factKey": strategy_key,
                        "memoryKind": "execution_strategy",
                        "strategyClass": strategy_class,
                        "derivedFromRunId": self.request.runId,
                        "actualExecution": True,
                        "candidateDataExcluded": True,
                        "selectedAgents": selected_agents,
                        "toolAgents": tool_agents,
                        "agentTimings": agent_timings,
                        "agentLlmCalls": agent_llm_calls,
                        "agentToolCalls": agent_tool_calls,
                        "totalLlmCalls": self.budget.llm_calls,
                        "providerLlmCallsByScope": dict(
                            self.budget.llm_calls_by_scope),
                        "totalToolCalls": self.budget.tool_calls,
                        "totalTokens": getattr(self.budget, "total_tokens", 0),
                        "elapsedSeconds": self.budget.elapsed_seconds(),
                        "runType": self.request.runType,
                    },
                    source="runtime_strategy",
                    source_id=strategy_key,
                    confidence=0.95,
                    ttl_days=365)
                strategy_written = True

            # Emit memory write event
            write_types = ["run_input_context"]
            if semantic_written:
                write_types.append("candidate_profile")
            if evidence_ledger:
                write_types.append("evidence_context")
            if isinstance(final_report, dict) and final_report.get("recommendation"):
                write_types.extend(["evaluation_result", "interview_probes"])
            if tech_findings:
                write_types.append("tech_findings")
            if risks_found:
                write_types.append("risks")
            if strategy_written:
                write_types.append("runtime_strategy")
            if write_types:
                await self.emitter.emit(
                    "run.progress", agent_id="MemoryService",
                    tool_name="memory_write",
                    payload={
                        "stage": "memory_write",
                        "writes": write_types,
                        "count": len(write_types),
                        "message": f"写入 {len(write_types)} 条记忆: {'+'.join(write_types[:3])}",
                    })

            # 6) User preferences (unchanged)
            for preference in self._explicit_preferences():
                await self.memory.write(
                    type_="SEMANTIC", owner_scope="USER",
                    content=f"{preference['kind']}: {preference['text']}",
                    structured=preference,
                    source="user_explicit", confidence=0.9)

            await self.emitter.emit(
                "agent.completed", agent_id="MemoryService",
                payload={"durationMs": 0, "llmCalls": 0, "toolCalls": 0,
                         "summary": f"记忆写入完成",
                         "confidence": 1.0})
        except Exception as exc:  # noqa: BLE001
            logger.info("memory write-back skipped: %s", exc)
            await self.emitter.emit(
                "agent.completed", agent_id="MemoryService",
                payload={"durationMs": 0, "llmCalls": 0, "toolCalls": 0,
                         "summary": f"记忆写入跳过: {exc}",
                         "confidence": 0.0})

    def _result(self, status: str, answer: str, *, error_code: Optional[str] = None,
                error_message: Optional[str] = None,
                conversation_summary: Optional[str] = None,
                snapshot: Optional[Dict[str, Any]] = None,
                missing_goal_artifacts: Optional[List[str]] = None) -> Dict[str, Any]:
        executed_agents = [o.get("agentId") for o in self.state.data["agentOutputs"]]
        support_ratio = self.state.evidence_support_ratio()
        coverage = None
        artifact = self.state.data["artifacts"].get("jdCoverage")
        if isinstance(artifact, dict):
            coverage = artifact.get("coverage")
        missing_goals = list(missing_goal_artifacts or [])
        metrics = {
            "llmCalls": self.budget.llm_calls,
            "llmBudget": self.budget.llm_audit(
                self.policy.maxLlmCalls),
            "logicalAgentLlmTurns": sum(
                int(counter.get("llmCalls", 0))
                for counter in self.agent_counters.values()
                if isinstance(counter, dict)),
            "toolCalls": self.budget.tool_calls,
            "promptTokens": self.budget.prompt_tokens,
            "completionTokens": self.budget.completion_tokens,
            "promptCacheHitTokens": self.budget.prompt_cache_hit_tokens,
            "costCny": round(self.budget.cost_cny, 4),
            "latencySeconds": round(self.budget.elapsed_seconds(), 2),
            "agentsUsed": executed_agents,
            "agentTimingsMs": self.agent_timings,
            "loopGuardTrips": self.guard.summary(),
            "contextCompactions": len(self.context.compactions),
            "degradedReasons": self.degraded_reasons,
            "evidenceSupportRatio": support_ratio,
            "jdCoverage": coverage,
            "missingGoalArtifacts": missing_goals,
            **self.tools.metrics(),
        }
        prompt_versions = default_prompt_manager.versions_used(
            list(dict.fromkeys(executed_agents + ["CoordinatorAgent"])),
            self.policy.promptVersions)
        skill_versions = default_skill_manager.versions_used(self.skill_selections)
        shared = self.state.snapshot()
        shared["agentOutputs"] = shared["agentOutputs"][-12:]
        result: Dict[str, Any] = {
            "status": status,
            "answer": answer,
            "errorCode": error_code,
            "errorMessage": error_message,
            "sharedState": shared,
            "metrics": metrics,
            "promptVersions": prompt_versions,
            "skillVersions": skill_versions,
            "conversationSummary": conversation_summary or "",
            "currentGoal": (self.request.currentGoal or self.request.userMessage or "")[:500],
            "missingGoalArtifacts": missing_goals,
        }
        final_report = self.state.data["artifacts"].get("finalReport")
        if isinstance(final_report, dict) and final_report:
            result["structuredReport"] = final_report
        if snapshot is not None:
            result["executionSnapshot"] = snapshot
        return result
