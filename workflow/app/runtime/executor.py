from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any, Dict, List, Optional, Tuple

from app.runtime.agents import AgentDefinition, AgentRegistry, default_agent_registry
from app.runtime.context import ContextManager
from app.runtime.coordinator import Coordinator, TERMINAL_AGENTS
from app.runtime.events import RuntimeEmitter
from app.runtime.llm import LlmError, ResilientLlmClient, extract_json_object
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

# EXP-7: adaptive replan fires when a group's average confidence drops below
# this. Sweepable per deployment without an image rebuild.
import os as _os

REPLAN_CONFIDENCE_THRESHOLD = float(_os.getenv("REPLAN_CONFIDENCE_THRESHOLD", "0.55"))

AGENT_OUTPUT_SCHEMA = """输出 JSON（不要输出其它内容）：
{
  "thought": "简要计划（一两句）",
  "toolCalls": [{"tool": "工具名", "arguments": {...}}]  // 需要工具时给出，不需要为 []
  ,"output": {                                            // 完成本职责时给出，否则为 null
    "summary": "一句话结论",
    "claims": [{"section": "technical_findings|project_findings|risks|evidence|recommendations|resume_facts|jd_requirements",
                 "value": [...] 或 {...}}],
    "evidence": [{"text": "证据描述", "sourceLine": 行号或null, "source": "resume|jd|tool|memory", "verified": true/false/null}],
    "confidence": 0.0-1.0,
    "requestedNextAction": "可选，建议下一步"
  },
  "done": true/false
}"""

REPORT_OUTPUT_SCHEMA = """输出 JSON（不要输出其它内容；精简表达）：
{
  "thought": "简要计划",
  "toolCalls": [],
  "output": {
    "summary": "面试官视角的一句话结论",
    "confidence": 0.0-1.0,
    "report": {
      "recommendation": "HIRE|INTERVIEW_RECOMMEND|NEED_MANUAL_REVIEW|NOT_RECOMMEND",
      "dimensions": [{"name":"技术能力|项目深度|JD匹配|履历可信度","score":"0-100整数（依据证据合理评分）","status":"ASSESSED|PARTIAL|UNASSESSED","rationale":"判断理由","evidence":["[RESUME L行号] 原文片段≤30字"]}],
      "strengths": ["有事实支撑的优势"],
      "risks": [{"risk":"具体风险","severity":"HIGH|MEDIUM|LOW","verification":"面试核实方式"}],
      "interviewProbes": [{"question":"针对性问题","whyAsk":"目的","expectedSignals":["好信号"],"redFlags":["警示信号"]}],
      "systemWarnings": [{"code":"...","stage":"...","retryable":false,"message":"..."}],
      "dataQuality": "SUFFICIENT|PARTIAL|INSUFFICIENT",
      "missingEvidence": ["无法从简历判断的信息"]
    }
  },
  "done": true
}
禁止输出 overallScore（系统加权计算）。无证据维度 status=UNASSESSED score=null。
评分标准：60=基本合格，70=良好匹配，80+=优秀匹配。有证据支撑合理给分，不要全部压低。
risks 仅写候选人侧；系统/数据问题放 systemWarnings。"""

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
                "toolCalls": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "tool": {"type": "string"},
                            "arguments": {"type": "object"},
                        },
                        "required": ["tool"],
                    },
                },
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
FORCE_EMIT_DECISION = {"type": "function", "function": {"name": "emit_decision"}}

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
    "required": ["claim", "evidenceRefs"],
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
    "required": ["question", "evidenceRefs", "goodSignals"],
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
                "toolCalls": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "tool": {"type": "string"},
                            "arguments": {"type": "object"},
                        },
                        "required": ["tool"],
                    },
                },
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
            else:
                # Lazy probe will run on first execute; schedule soft attach.
                self._mcp_attach_pending = True
        except Exception as exc:  # noqa: BLE001
            logger.info("MCP attach deferred: %s", exc)
            self._mcp_attach_pending = True

    # ------------------------------------------------------------------

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
            # Specialists/Report: USER/CONVERSATION PREFERENCE/CONVERSATION/EPISODIC
            # only — never FAILURE (control-plane noise must not enter evaluation).
            self.memory_hits = await self.memory.search(
                request.userMessage, types=sorted(SPECIALIST_TYPES),
                top_k=self.policy.memoryRetrieval.topK,
                min_confidence=self.policy.memoryRetrieval.minConfidence,
                consumer_agent="SpecialistAgent")
            # FAILURE is Coordinator / policy-evolution only.
            self.failure_hits = await self.memory.search(
                request.userMessage, types=["FAILURE"],
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
            planned = await coordinator.plan(
                run_type=request.runType, user_message=request.userMessage,
                conversation_summary=request.conversationSummary or "",
                shared_digest=self.state.view_for("CoordinatorAgent", max_chars=2000),
                failure_notes=self.failure_notes,
                memory_notes=[str(h.get("content", ""))[:120]
                              for h in self.memory_hits[:3]],
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
        except BudgetExceeded:
            raise
        except Exception as exc:  # noqa: BLE001 - agent failure boundary
            await self._after_agent_failure(definition, exc, agent_started)
            return False

    async def _run_parallel(self, definitions: List[AgentDefinition]) -> bool:
        """Independent specialists run concurrently against read-only state
        views; outputs are merged sequentially afterwards so no coroutine
        ever mutates the blackboard concurrently."""
        started_at: Dict[str, float] = {}
        for definition in definitions:
            started_at[definition.agent_id] = time.monotonic()
            await self.emitter.emit("agent.started", agent_id=definition.agent_id,
                                    payload={"description": definition.description,
                                             "parallelGroup": [d.agent_id for d in definitions],
                                             "position": len(self.executed) + 1,
                                             "planned": len(self.plan)})

        async def guarded(defn: AgentDefinition) -> Tuple[AgentDefinition, Any]:
            try:
                output = await asyncio.wait_for(
                    self._run_agent(defn), timeout=defn.timeout_seconds)
                return defn, output
            except (asyncio.CancelledError, BudgetExceeded):
                raise
            except Exception as exc:  # noqa: BLE001
                return defn, exc

        results = await asyncio.gather(*(guarded(d) for d in definitions))
        any_success = False
        for definition, outcome in results:
            if isinstance(outcome, AgentOutput):
                conflicts = self.state.apply_output(outcome)
                self._after_agent_success(definition, outcome, conflicts,
                                          started_at[definition.agent_id],
                                          fire_started=False)
                any_success = True
            else:
                await self._after_agent_failure(definition, outcome,
                                                started_at[definition.agent_id])
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
        if agent_id in TERMINAL_AGENTS:
            # A failed terminal agent is never re-queued: retrying it burns the
            # LLM budget on the same failure. Degrade honestly instead.
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
            "replanCount": self.replan_count})

    def _has_hard_degradation(self) -> bool:
        hard = {"run_timeout", "consecutive_failures"}
        return any(r in hard or r.endswith("_failed") for r in self.degraded_reasons)

    def _missing_required_goal_artifacts(self) -> List[str]:
        """Run-end closure: required goal artifacts absent → never silent SUCCEEDED."""
        goals = list((self.plan_meta or {}).get("goalArtifacts") or [])
        if not goals:
            return []
        present = Coordinator._present_artifacts(
            self.state.artifacts() if hasattr(self.state, "artifacts") else
            self.state.data.get("artifacts") or {},
            self.state.data if isinstance(self.state.data, dict) else {})
        # Also treat camelCase finalReport / evidence mirrors as present.
        return [g for g in goals if g not in present]

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
        return int(value) if isinstance(value, (int, float)) and value > 0 else fallback

    async def _run_agent(self, definition: AgentDefinition) -> AgentOutput:
        request = self.request
        agent_id = definition.agent_id
        prompt = default_prompt_manager.system_for_agent(
            agent_id, self.policy.promptVersions.get(agent_id))
        signals = Coordinator(self.registry, self.policy, None).inspect_signals(
            resume_text=request.resumeText or "",
            job_description=request.jobDescription or "",
            artifacts=self.state.data.get("artifacts") or {},
            shared=self.state.data)
        skills = default_skill_manager.select_for(
            agent_id=agent_id, run_type=request.runType,
            job_focus=self.policy.jobFocus, overrides=self.policy.skillOverrides,
            signals=signals, user_message=request.userMessage or "")
        self.skill_selections[agent_id] = skills
        try:
            await default_skill_manager.emit_selection(
                self.emitter, agent_id, skills)
        except Exception as exc:  # noqa: BLE001
            logger.debug("skill event emit skipped: %s", exc)

        # Live tool catalog: static definition tools + AVAILABLE MCP route.
        allowed_tools = set(definition.tools)
        try:
            catalog = self.tools.catalog_for_agent(agent_id, list(definition.tools))
            for entry in catalog:
                allowed_tools.add(str(entry.get("name") or ""))
        except Exception:  # noqa: BLE001
            pass

        tool_results_block = ""
        agent_tool_calls = 0
        agent_llm_calls = 0
        agent_tool_limit = min(
            definition.max_tool_calls,
            self.policy.toolBudget.maxToolCallsPerAgent,
            self._agent_quota(agent_id, "toolQuota",
                              self.policy.toolBudget.maxToolCallsPerAgent))

        for tool, args in self._pre_steps(definition):
            if agent_tool_calls >= agent_tool_limit:
                break
            guard = self.guard.check_tool_call(ToolExecutor.signature(tool, args))
            if guard.triggered:
                await self._emit_guard(guard, agent_id)
                continue
            # Copilot conversational RAG + TechAgent agentic retrieval: rewrite
            # on knowledge/resume search presteps for followup/quick_answer.
            rewrite = (
                tool in ("knowledge_search", "resume_semantic_search")
                and self.request.runType in ("followup", "quick_answer")
            )
            call = await self.tools.execute(agent_id, tool, args,
                                            enable_rewrite=rewrite)
            agent_tool_calls += 1
            if call.status == "FAILED":
                self._tool_failed_this_group = True
            tool_results_block += self._format_tool_result(call)
            if call.status == "SUCCEEDED":
                if tool == "calculate_jd_coverage":
                    self.state.put_artifact("jdCoverage", call.result)
                elif tool == "check_timeline":
                    self.state.put_artifact("timelineCheck", call.result)
                elif tool == "verify_report_evidence":
                    self._apply_verification(call.result)
                elif tool == "parse_resume":
                    self.state.put_artifact("parsedResume", call.result)
                    facts = self._resume_facts_from_parse(call.result)
                    if facts:
                        self.state.put_artifact("resumeFacts", facts)
                elif tool == "jd_match_search":
                    self._store_jd_match_artifacts(call.result)

        # Performance fast-path: high-quality deterministic parse → skip LLM.
        if definition.agent_id == "ResumeParserAgent":
            fast = self._maybe_skip_parser_llm(tool_results_block)
            if fast is not None:
                self.agent_counters[definition.agent_id] = {
                    "iterations": 0, "llmCalls": 0, "toolCalls": agent_tool_calls,
                    "fastPath": 1}
                return fast

        # Performance fast-path: JD short/provided → skip LLM.
        if definition.agent_id == "JDAnalysisAgent":
            fast = self._maybe_skip_jd_llm()
            if fast is not None:
                self.agent_counters[definition.agent_id] = {
                    "iterations": 0, "llmCalls": 0, "toolCalls": agent_tool_calls,
                    "fastPath": 1}
                return fast

        # Performance fast-path: Evidence verify clean → skip arbitration LLM.
        if definition.agent_id == "EvidenceAgent":
            fast = self._maybe_skip_evidence_llm(tool_results_block)
            if fast is not None:
                self.agent_counters[definition.agent_id] = {
                    "iterations": 0, "llmCalls": 0, "toolCalls": agent_tool_calls,
                    "fastPath": 1}
                return fast

        output: Optional[AgentOutput] = None
        max_iterations = min(
            definition.max_iterations, self.policy.maxIterationsPerAgent,
            self._agent_quota(agent_id, "llmQuota", definition.max_iterations))
        iteration = 0
        while iteration < max_iterations and output is None:
            iteration += 1
            await self.emitter.emit("agent.progress", agent_id=agent_id, payload={
                "iteration": iteration, "maxIterations": max_iterations})
            messages = self.context.assemble(
                system_prompt=prompt.content,
                policy_instructions=self._policy_instructions(),
                skill_instructions=default_skill_manager.render(skills),
                user_request=request.userMessage or "（对当前简历执行你的职责）",
                current_goal=request.currentGoal or "",
                shared_state_digest=self.state.view_for(agent_id),
                recent_messages=request.recentMessages,
                conversation_summary=request.conversationSummary or "",
                memory_block=self._memory_block(definition),
                tool_results_block=tool_results_block,
                output_schema=(REPORT_OUTPUT_SCHEMA if agent_id == "ReportAgent"
                               else AGENT_OUTPUT_SCHEMA))
            audit = getattr(self, "_last_memory_audit", None)
            if audit and audit.get("consumerAgent") == agent_id:
                await self.emitter.emit("run.progress", agent_id=agent_id, payload={
                    "stage": "memory_inject",
                    "message": (f"{agent_id} 注入记忆 {audit.get('usedCount', 0)} 条"
                                f"（忽略 {audit.get('ignoredCount', 0)}）"),
                    "memoryTrace": audit.get("memoryTrace") or [],
                    "usedCount": audit.get("usedCount"),
                    "ignoredCount": audit.get("ignoredCount"),
                })
                decisions = audit.get("decisions") or []
                if decisions:
                    try:
                        await self.memory.record_usage(
                            consumer_agent=agent_id, decisions=decisions)
                    except Exception:  # noqa: BLE001
                        pass
                self._last_memory_audit = None
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

            is_terminal = definition.agent_id in TERMINAL_AGENTS
            is_report = definition.agent_id == "ReportAgent"
            # Specialists + ReportAgent: provider-enforced function calling.
            # Other terminal agents (optimize/interview) keep json_object mode —
            # their long markdown answer lives inside the JSON envelope.
            if is_report:
                try:
                    raw = await self.llm.chat(
                        messages, agent_id=agent_id,
                        purpose=definition.output_type, max_tokens=2048,
                        tools=[EMIT_REPORT_TOOL],
                        tool_choice=FORCE_EMIT_DECISION)
                except LlmError as exc:
                    if exc.code in ("PROMPT_OR_SCHEMA_ERROR", "EMPTY_FUNCTION_ARGS"):
                        raw = await self.llm.chat(
                            messages, agent_id=agent_id,
                            purpose=definition.output_type, max_tokens=2048)
                    else:
                        raise
            elif is_terminal:
                raw = await self.llm.chat(messages, agent_id=agent_id,
                                          purpose=definition.output_type,
                                          max_tokens=3600)
            else:
                try:
                    raw = await self.llm.chat(messages, agent_id=agent_id,
                                              purpose=definition.output_type,
                                              max_tokens=1200,
                                              tools=[EMIT_DECISION_TOOL],
                                              tool_choice=FORCE_EMIT_DECISION)
                except LlmError as exc:
                    if exc.code in ("PROMPT_OR_SCHEMA_ERROR", "EMPTY_FUNCTION_ARGS"):
                        raw = await self.llm.chat(messages, agent_id=agent_id,
                                                  purpose=definition.output_type,
                                                  max_tokens=1200)
                    else:
                        raise
            agent_llm_calls += 1
            decision, schema_error = self._parse_decision(raw)
            if decision is None:
                # Repair with the exact violation fed back, not a generic nag.
                # Terminal raw-markdown acceptance was removed: ReportAgent must
                # produce structured JSON or the run fails the report contract.
                raw = await self.llm.chat(
                    messages + [{"role": "assistant", "content": raw[:1500]},
                                {"role": "user",
                                 "content": ("上面的输出未通过 json schema 校验："
                                             f"{schema_error[:400]}。"
                                             "请只输出修正后的 JSON 对象，不要任何其他文本。")}],
                    agent_id=agent_id, purpose="repair", max_tokens=2048)
                agent_llm_calls += 1
                decision, schema_error = self._parse_decision(raw)
                if decision is None:
                    raise LlmError("MALFORMED_OUTPUT",
                                   f"agent 两次输出均未通过 schema 校验: {schema_error[:200]}",
                                   False)

            thought = str(decision.get("thought") or "")
            if thought:
                guard = self.guard.check_plan(f"{agent_id}:{thought}")
                if guard.triggered:
                    await self._emit_guard(guard, agent_id)
                    decision["toolCalls"] = []
                    decision["done"] = True

            tool_calls = decision.get("toolCalls") or []
            if tool_calls and iteration < max_iterations:
                observations = ""
                for tool_call in tool_calls[:3]:
                    tool = str(tool_call.get("tool") or "")
                    if tool not in allowed_tools:
                        observations += f"\n[TOOL_RESULT {tool}] 拒绝：不在该 Agent 白名单"
                        continue
                    if agent_tool_calls >= agent_tool_limit:
                        observations += "\n[TOOL_RESULT budget] Agent 工具预算耗尽"
                        break
                    args = tool_call.get("arguments") or {}
                    guard = self.guard.check_tool_call(ToolExecutor.signature(tool, args))
                    if guard.triggered:
                        await self._emit_guard(guard, agent_id)
                        observations += f"\n[TOOL_RESULT {tool}] 跳过：重复调用被 Loop Guard 拦截"
                        continue
                    # Agentic retrieval: decision-loop calls get query
                    # rewriting + multi-query RRF fusion; pre-steps stay
                    # single-shot to save LLM budget.
                    call = await self.tools.execute(agent_id, tool, args,
                                                    enable_rewrite=True)
                    agent_tool_calls += 1
                    if call.status == "FAILED":
                        self._tool_failed_this_group = True
                    observations += self._format_tool_result(call)
                    if call.status == "SUCCEEDED":
                        if tool == "verify_report_evidence":
                            self._apply_verification(call.result)
                        elif tool == "jd_match_search":
                            self._store_jd_match_artifacts(call.result)
                guard = self.guard.check_observation(observations)
                if guard.triggered:
                    await self._emit_guard(guard, agent_id)
                    decision["done"] = True
                tool_results_block += observations

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
            if raw_output or decision.get("done") or iteration >= max_iterations:
                output = self._build_output(definition, raw_output, tool_results_block)

        if output is None:
            output = self._build_output(definition, None, tool_results_block)
        self.agent_counters[definition.agent_id] = {
            "iterations": iteration,
            "llmCalls": agent_llm_calls,
            "toolCalls": agent_tool_calls,
        }
        return output

    @staticmethod
    def _parse_decision(raw: str) -> Tuple[Optional[Dict[str, Any]], str]:
        """Layered JSON guarantee, application side: extract the object, then
        validate against the AgentDecision schema. Returns (decision, error);
        decision is None when either layer fails, with the exact violation in
        error so the repair call can quote it back to the model."""
        candidate = extract_json_object(raw)
        if not candidate:
            return None, "输出中找不到可解析的 JSON 对象"
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

    def _maybe_skip_evidence_llm(self, tool_results_block: str) -> Optional[AgentOutput]:
        """Skip Evidence LLM when deterministic verify is clean and support is high."""
        support = self.state.evidence_support_ratio()
        conflicts = self.state.artifact("conflicts") or []
        if conflicts:
            return None
        if support is None or support < 0.85:
            return None
        if "verify_report_evidence" not in tool_results_block:
            return None
        summary = f"确定性核验通过：支持率 {support:.2f}，无冲突，跳过 Evidence LLM"
        return AgentOutput(
            agentId="EvidenceAgent",
            type="evidence",
            claims=[],
            evidence=[],
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

        if resume and not jd and not arts.get("jdMatches"):
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
        elif definition.agent_id == "JDAnalysisAgent" and resume \
                and not (request.jobDescription or "").strip() \
                and "jdMatches" not in artifacts:
            # No user JD: deterministic hybrid match (do not rely on LLM tool whim).
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
        # Copilot 对话式 RAG：followup/quick_answer 只有 ReportAgent，回答前
        # 自动检索知识库标准与简历证据，命中不足时报告里如实说明。
        if definition.agent_id == "ReportAgent" \
                and request.runType in ("followup", "quick_answer") \
                and (request.userMessage or "").strip():
            query = request.userMessage.strip()[:200]
            steps.append(("knowledge_search", {"query": query}))
            if resume:
                steps.append(("resume_semantic_search", {"query": query}))
        return steps

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

    def _memory_block(self, definition: AgentDefinition) -> str:
        if definition.memory_policy == "none":
            return ""
        agent_id = definition.agent_id
        # Coordinator may additionally see FAILURE hits for planning hints;
        # Report/Risk and other specialists only see the evaluation-safe pool.
        pool = list(self.memory_hits)
        if agent_id == "CoordinatorAgent":
            pool = pool + list(self.failure_hits)
        if not pool:
            return ""
        used, ignored = filter_hits_for_consumer(pool, agent_id)
        trace = memory_trace_entries(used, ignored, agent_id)
        self.memory_traces.extend(trace)
        decisions = decisions_from_hits(used, ignored, agent_id)
        # Emit per-agent memory audit so Trace can show used vs ignoredReason.
        # Persistence happens in async _run_agent after this sync helper returns.
        try:
            self._last_memory_audit = {
                "consumerAgent": agent_id,
                "usedCount": len(used),
                "ignoredCount": len(ignored),
                "memoryTrace": trace[:20],
                "decisions": decisions,
            }
        except Exception:  # noqa: BLE001
            pass
        lines = []
        for hit in used[: self.policy.memoryRetrieval.topK]:
            scope = hit.get("ownerScope") or hit.get("scope") or "?"
            lines.append(
                f"- [{hit.get('type')}|{scope}|src={hit.get('source') or '?'}|"
                f"置信{hit.get('confidence')}] "
                f"{str(hit.get('content', ''))[:200]}")
        return "\n".join(lines)

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
        if arts.get("technicalFindings"):
            parts.append("技术结论: " + "; ".join(
                str(e.get("text", ""))[:80] for e in (arts.get("technicalFindings") or [])[:3]
                if isinstance(e, dict)))
        if arts.get("risks"):
            parts.append("风险: " + "; ".join(
                str(e.get("text", ""))[:80] for e in (arts.get("risks") or [])[:3]
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

    async def _write_memories(self, summary: str) -> None:
        try:
            await self.memory.write(
                type_="CONVERSATION", owner_scope="CONVERSATION",
                content=f"会话摘要更新: {summary[:600]}",
                structured={"factKey": f"summary:{self.request.conversationId}"},
                source="system_rule", confidence=0.8)
            unsupported = [c for c in (self.state.artifact("conflicts") or [])
                           if isinstance(c, dict) and c.get("type") == "unsupported_claim"]
            # unsupported claims already live in the shared-state snapshot;
            # a separate short-term WORKING memory row duplicated it (removed).
            for preference in self._explicit_preferences():
                await self.memory.write(
                    type_="PREFERENCE", owner_scope="USER",
                    content=f"{preference['kind']}: {preference['text']}",
                    structured=preference,
                    source="user_explicit", confidence=0.9)
        except Exception as exc:  # noqa: BLE001
            logger.info("memory write-back skipped: %s", exc)

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