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
from app.runtime.memory import MemoryClient
from app.runtime.models import (
    AgentOutput,
    AgentRunRequest,
    BudgetExceeded,
    PolicyBundle,
    RunBudget,
    RunPaused,
)
from app.runtime.prompts import default_prompt_manager
from app.runtime.sandbox import SandboxClient
from app.runtime.skills import default_skill_manager
from app.runtime.state import SharedState
from app.runtime.tools import ToolExecutor

logger = logging.getLogger(__name__)

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
                 sandbox: Optional[Any] = None,
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
        self.sandbox = sandbox or SandboxClient(
            emitter, request.runId, request.conversationId,
            timeout_seconds=self.policy.timeoutPolicy.sandboxTimeoutSeconds)
        run_context = {
            "resumeText": request.resumeText or "",
            "jobDescription": request.jobDescription or "",
        }
        self.tools = ToolExecutor(
            emitter, self.budget, self.sandbox,
            max_tool_calls_run=self.policy.toolBudget.maxToolCallsPerRun,
            tool_timeout_seconds=self.policy.timeoutPolicy.toolTimeoutSeconds,
            run_context=run_context)
        self.llm = llm or ResilientLlmClient(
            emitter, self.budget, self.policy.maxLlmCalls,
            self.policy.timeoutPolicy.llmTimeoutSeconds)
        self.context = ContextManager(self.policy.contextBudget, emitter,
                                      request.runId, request.conversationId)
        self.guard = LoopGuard()
        self.state = SharedState()
        self.skill_selections: Dict[str, List[Any]] = {}
        self.memory_hits: List[Dict[str, Any]] = []
        self.failure_notes: List[str] = []
        self.final_answer: str = ""
        self.degraded_reasons: List[str] = []
        self.agent_counters: Dict[str, Dict[str, int]] = {}
        self.agent_timings: Dict[str, int] = {}
        self.report_agent_failed = False
        # populated by _restore_snapshot on resume
        self.plan: List[str] = []
        self.parallel_groups: List[List[str]] = []
        self.next_group_index = 0
        self.executed: List[str] = []

    # ------------------------------------------------------------------

    async def execute(self) -> Dict[str, Any]:
        started = time.monotonic()
        try:
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
            memory_types = ["CONVERSATION", "EPISODIC", "USER_PREFERENCE",
                            "HR_FEEDBACK", "DOMAIN", "FAILURE"]
            self.memory_hits = await self.memory.search(
                request.userMessage, types=memory_types,
                top_k=self.policy.memoryRetrieval.topK,
                min_confidence=self.policy.memoryRetrieval.minConfidence)
            self.failure_notes = [
                str(h.get("content", ""))[:160] for h in self.memory_hits
                if h.get("type") == "FAILURE"][:3]

            coordinator = Coordinator(self.registry, self.policy, self.llm)
            needs_parse = bool(request.resumeText) \
                and request.runType in ("full_evaluation", "jd_evaluation",
                                        "backend_eval", "agent_eval",
                                        "resume_optimize", "project_rewrite")
            planned = await coordinator.plan(
                run_type=request.runType, user_message=request.userMessage,
                conversation_summary=request.conversationSummary or "",
                shared_digest="", failure_notes=self.failure_notes,
                memory_notes=[str(h.get("content", ""))[:120]
                              for h in self.memory_hits[:3]],
                needs_parse=needs_parse)
            self.plan = planned["plan"]
            self.parallel_groups = planned["parallelGroups"]
            await self.emitter.emit("agent.selected", agent_id="CoordinatorAgent", payload={
                "plan": self.plan, "reason": planned["reason"],
                "parallelGroups": self.parallel_groups,
                "requiredTerminalAgent": planned["requiredTerminalAgent"],
                "policyId": self.policy.policyId})
            self.state.set_pending(list(self.plan))
        else:
            coordinator = Coordinator(self.registry, self.policy, self.llm)
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

            if len(runnable) == 1:
                ok = await self._run_single(runnable[0], coordinator)
                consecutive_failures = 0 if ok else consecutive_failures + 1
            else:
                ok = await self._run_parallel(runnable)
                consecutive_failures = 0 if ok else consecutive_failures + 1

            if consecutive_failures >= 2:
                self.degraded_reasons.append("consecutive_failures")
                self._ensure_terminal_tail()

        if not self.final_answer:
            self.degraded_reasons.append("no_terminal_answer")
            self.final_answer = self._degraded_answer("报告 Agent 未能完成")
            self.report_agent_failed = True
        summary = self._conversation_summary()
        await self._write_memories(summary)
        status = "PARTIAL_SUCCESS" if (self.report_agent_failed or
                                       self._has_hard_degradation()) else "SUCCEEDED"
        return self._result(status, self.final_answer, conversation_summary=summary)

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
            self.report_agent_failed = True
        self._ensure_terminal_tail()

    def _ensure_terminal_tail(self) -> None:
        remaining = [a for g in self.parallel_groups[self.next_group_index:] for a in g]
        if not any(a in TERMINAL_AGENTS for a in remaining):
            self.parallel_groups.append(["ReportAgent"])
            if "ReportAgent" not in self.plan:
                self.plan.append("ReportAgent")

    def _has_hard_degradation(self) -> bool:
        hard = {"run_timeout", "consecutive_failures"}
        return any(r in hard or r.endswith("_failed") for r in self.degraded_reasons)

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
            "finalAnswer": self.final_answer,
            "degradedReasons": list(self.degraded_reasons),
            "agentTimings": dict(self.agent_timings),
            "failureNotes": list(self.failure_notes)[-5:],
            "createdAt": time.time(),
        }

    def _restore_snapshot(self, snapshot: Optional[Dict[str, Any]]) -> bool:
        if not snapshot:
            return False
        try:
            self.plan = [str(a) for a in snapshot.get("plan", [])]
            self.parallel_groups = [
                [str(a) for a in group]
                for group in snapshot.get("parallelGroups", [[a] for a in self.plan])]
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
            for agent_id in self.executed:
                self.guard.record_completed_agent(agent_id)
            return True
        except Exception as exc:  # noqa: BLE001 - a bad snapshot must not brick the run
            logger.warning("snapshot restore failed, starting fresh: %s", exc)
            return False

    # ------------------------------------------------------------------
    # single agent execution (unchanged core loop, per-agent budget)
    # ------------------------------------------------------------------

    async def _run_agent(self, definition: AgentDefinition) -> AgentOutput:
        request = self.request
        agent_id = definition.agent_id
        prompt = default_prompt_manager.system_for_agent(
            agent_id, self.policy.promptVersions.get(agent_id))
        skills = default_skill_manager.select_for(
            agent_id=agent_id, run_type=request.runType,
            job_focus=self.policy.jobFocus, overrides=self.policy.skillOverrides)
        self.skill_selections[agent_id] = skills

        tool_results_block = ""
        agent_tool_calls = 0
        agent_llm_calls = 0

        for tool, args in self._pre_steps(definition):
            if agent_tool_calls >= min(definition.max_tool_calls,
                                       self.policy.toolBudget.maxToolCallsPerAgent):
                break
            guard = self.guard.check_tool_call(ToolExecutor.signature(tool, args))
            if guard.triggered:
                await self._emit_guard(guard, agent_id)
                continue
            call = await self.tools.execute(agent_id, tool, args)
            agent_tool_calls += 1
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

        output: Optional[AgentOutput] = None
        max_iterations = min(definition.max_iterations, self.policy.maxIterationsPerAgent)
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
                output_schema=AGENT_OUTPUT_SCHEMA)
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

            raw = await self.llm.chat(messages, agent_id=agent_id,
                                      purpose=definition.output_type,
                                      max_tokens=2048)
            agent_llm_calls += 1
            decision = extract_json_object(raw)
            if not decision:
                raw = await self.llm.chat(
                    messages + [{"role": "assistant", "content": raw[:1500]},
                                {"role": "user",
                                 "content": "上面的输出不是合法 JSON。请只输出符合 schema 的 JSON。"}],
                    agent_id=agent_id, purpose="repair", max_tokens=2048)
                agent_llm_calls += 1
                decision = extract_json_object(raw)
                if not decision:
                    raise LlmError("MALFORMED_OUTPUT", "agent 未能给出合法 JSON", False)

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
                    if tool not in definition.tools:
                        observations += f"\n[TOOL_RESULT {tool}] 拒绝：不在该 Agent 白名单"
                        continue
                    if agent_tool_calls >= min(definition.max_tool_calls,
                                               self.policy.toolBudget.maxToolCallsPerAgent):
                        observations += "\n[TOOL_RESULT budget] Agent 工具预算耗尽"
                        break
                    args = tool_call.get("arguments") or {}
                    guard = self.guard.check_tool_call(ToolExecutor.signature(tool, args))
                    if guard.triggered:
                        await self._emit_guard(guard, agent_id)
                        observations += f"\n[TOOL_RESULT {tool}] 跳过：重复调用被 Loop Guard 拦截"
                        continue
                    call = await self.tools.execute(agent_id, tool, args)
                    agent_tool_calls += 1
                    observations += self._format_tool_result(call)
                    if tool == "verify_report_evidence" and call.status == "SUCCEEDED":
                        self._apply_verification(call.result)
                guard = self.guard.check_observation(observations)
                if guard.triggered:
                    await self._emit_guard(guard, agent_id)
                    decision["done"] = True
                tool_results_block += observations

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
            answer = raw_output.get("answer") or raw_output.get("report") \
                or raw_output.get("markdown") or summary
            if isinstance(answer, dict):
                answer = json.dumps(answer, ensure_ascii=False, indent=2)
            if answer:
                self.final_answer = str(answer)
        return output

    def _pre_steps(self, definition: AgentDefinition) -> List[tuple]:
        request = self.request
        resume = request.resumeText or ""
        steps: List[tuple] = []
        parsed_already = "parsedResume" in self.state.data.get("artifacts", {})
        if definition.agent_id == "ResumeParserAgent" and resume and not parsed_already:
            steps.append(("parse_resume", {"resumeText": resume}))
        elif definition.agent_id == "RiskAgent" and resume \
                and "timelineCheck" not in self.state.data.get("artifacts", {}):
            steps.append(("check_timeline", {"resumeText": resume}))
        elif definition.agent_id == "TechAgent" and resume \
                and (request.jobDescription or "").strip() \
                and "jdCoverage" not in self.state.data.get("artifacts", {}):
            steps.append(("calculate_jd_coverage",
                          {"resumeText": resume, "jdText": request.jobDescription}))
        elif definition.agent_id == "EvidenceAgent" and resume:
            claims = self.state.claims_for_verification()
            if claims:
                steps.append(("verify_report_evidence",
                              {"resumeText": resume,
                               "jdText": request.jobDescription or "",
                               "claims": claims}))
        elif definition.agent_id == "ResumeOptimizeAgent" and resume:
            steps.append(("resume_lint", {"resumeText": resume}))
        return steps

    def _apply_verification(self, result: Any) -> None:
        if not isinstance(result, dict):
            return
        for entry in result.get("supported", []):
            self.state.data["evidence"].append({
                "text": entry.get("claim", ""), "verified": True,
                "location": entry.get("location"), "byAgent": "EvidenceAgent"})
        for entry in result.get("unsupported", []):
            self.state.data["evidence"].append({
                "text": entry.get("claim", ""), "verified": False,
                "reason": entry.get("reason"), "byAgent": "EvidenceAgent"})
            self.state.add_conflict({
                "type": "unsupported_claim", "claim": entry.get("claim", ""),
                "reason": entry.get("reason", ""), "byAgent": "EvidenceAgent"})

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
        if definition.memory_policy == "none" or not self.memory_hits:
            return ""
        lines = []
        for hit in self.memory_hits[: self.policy.memoryRetrieval.topK]:
            lines.append(f"- [{hit.get('type')}|置信{hit.get('confidence')}] "
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
        state = self.state.data
        sections: List[str] = [f"> 说明：{reason}，以下为基于已完成分析的降级结果（非完整报告）。\n"]
        if state["technicalFindings"]:
            sections.append("## 技术发现\n" + "\n".join(
                f"- {e.get('text', json.dumps(e, ensure_ascii=False)[:160])}"
                for e in state["technicalFindings"][:8] if isinstance(e, dict)))
        if state["risks"]:
            sections.append("## 风险\n" + "\n".join(
                f"- {e.get('text', json.dumps(e, ensure_ascii=False)[:160])}"
                for e in state["risks"][:8] if isinstance(e, dict)))
        if state["conflicts"]:
            sections.append("## 证据不足/冲突\n" + "\n".join(
                f"- {c.get('claim', c.get('key', ''))}"
                for c in state["conflicts"][:6] if isinstance(c, dict)))
        artifacts = state.get("artifacts", {})
        coverage = artifacts.get("jdCoverage")
        if isinstance(coverage, dict) and coverage.get("coverage") is not None:
            sections.append(f"## JD 覆盖率\n- {coverage.get('coverage')}")
        if len(sections) == 1:
            sections.append("尚未获得足够分析结果，请重试或缩小问题范围。")
        return "\n\n".join(sections)

    def _conversation_summary(self) -> str:
        state = self.state.data
        parts = [f"目标: {(self.request.currentGoal or self.request.userMessage or '')[:150]}"]
        if state["technicalFindings"]:
            parts.append("技术结论: " + "; ".join(
                str(e.get("text", ""))[:80] for e in state["technicalFindings"][:3]
                if isinstance(e, dict)))
        if state["risks"]:
            parts.append("风险: " + "; ".join(
                str(e.get("text", ""))[:80] for e in state["risks"][:3]
                if isinstance(e, dict)))
        if state["conflicts"]:
            parts.append(f"未决冲突 {len(state['conflicts'])} 项")
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
            unsupported = [c for c in self.state.data["conflicts"]
                           if isinstance(c, dict) and c.get("type") == "unsupported_claim"]
            if unsupported:
                await self.memory.write(
                    type_="WORKING", owner_scope="RUN",
                    content="未支持结论: " + "; ".join(
                        str(c.get("claim", ""))[:100] for c in unsupported[:5]),
                    source="system_rule", confidence=0.7, ttl_days=2)
            for preference in self._explicit_preferences():
                await self.memory.write(
                    type_="USER_PREFERENCE", owner_scope="USER",
                    content=f"{preference['kind']}: {preference['text']}",
                    structured=preference,
                    source="user_explicit", confidence=0.9)
        except Exception as exc:  # noqa: BLE001
            logger.info("memory write-back skipped: %s", exc)

    def _result(self, status: str, answer: str, *, error_code: Optional[str] = None,
                error_message: Optional[str] = None,
                conversation_summary: Optional[str] = None,
                snapshot: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        executed_agents = [o.get("agentId") for o in self.state.data["agentOutputs"]]
        support_ratio = self.state.evidence_support_ratio()
        coverage = None
        artifact = self.state.data["artifacts"].get("jdCoverage")
        if isinstance(artifact, dict):
            coverage = artifact.get("coverage")
        metrics = {
            "llmCalls": self.budget.llm_calls,
            "toolCalls": self.budget.tool_calls,
            "promptTokens": self.budget.prompt_tokens,
            "completionTokens": self.budget.completion_tokens,
            "latencySeconds": round(self.budget.elapsed_seconds(), 2),
            "agentsUsed": executed_agents,
            "agentTimingsMs": self.agent_timings,
            "loopGuardTrips": self.guard.summary(),
            "contextCompactions": len(self.context.compactions),
            "degradedReasons": self.degraded_reasons,
            "evidenceSupportRatio": support_ratio,
            "jdCoverage": coverage,
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
        }
        if snapshot is not None:
            result["executionSnapshot"] = snapshot
        return result
