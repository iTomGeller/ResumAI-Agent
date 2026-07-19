from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Dict, List, Optional

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


class RunExecutor:
    """Executes one conversational run: Observe → Plan → Select Agent →
    Execute → Tool → Observation → Update Shared State → Continue/Finish →
    Respond, with budgets, loop guard, layered memory and compaction."""

    def __init__(self, request: AgentRunRequest, emitter: RuntimeEmitter, *,
                 registry: Optional[AgentRegistry] = None,
                 memory: Optional[MemoryClient] = None,
                 sandbox: Optional[Any] = None,
                 llm: Optional[ResilientLlmClient] = None) -> None:
        self.request = request
        self.emitter = emitter
        self.registry = registry or default_agent_registry
        self.policy = PolicyBundle.from_config(request.policyId, request.policyConfig)
        self.budget = RunBudget()
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

    # ------------------------------------------------------------------

    async def execute(self) -> Dict[str, Any]:
        started = time.monotonic()
        try:
            result = await asyncio.wait_for(
                self._execute_inner(),
                timeout=self.policy.timeoutPolicy.runTimeoutSeconds)
            return result
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
            return self._result("SUCCEEDED" if answer else "FAILED", answer,
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
        await self.emitter.emit("run.progress", payload={
            "stage": "observe", "message": "加载记忆与上下文"})

        # Observe: task-relevant memory only (never a full dump).
        memory_types = ["CONVERSATION", "EPISODIC", "USER_PREFERENCE",
                        "HR_FEEDBACK", "DOMAIN", "FAILURE"]
        self.memory_hits = await self.memory.search(
            request.userMessage, types=memory_types,
            top_k=self.policy.memoryRetrieval.topK,
            min_confidence=self.policy.memoryRetrieval.minConfidence)
        self.failure_notes = [
            str(h.get("content", ""))[:160] for h in self.memory_hits
            if h.get("type") == "FAILURE"][:3]

        # Plan: rule-first pipeline refined by one budgeted LLM call.
        coordinator = Coordinator(self.registry, self.policy, self.llm)
        needs_parse = bool(request.resumeText) and len(request.resumeText or "") > 0 \
            and request.runType in ("full_evaluation", "jd_evaluation", "backend_eval",
                                    "agent_eval", "resume_optimize", "project_rewrite")
        base_plan = coordinator.base_plan(
            request.runType, has_resume_facts=False, needs_parse=needs_parse)
        refined = await coordinator.refine_plan(
            base_plan, run_type=request.runType, user_message=request.userMessage,
            conversation_summary=request.conversationSummary or "",
            shared_digest="", failure_notes=self.failure_notes,
            memory_notes=[str(h.get("content", ""))[:120] for h in self.memory_hits[:3]])
        plan: List[str] = refined["plan"]
        await self.emitter.emit("agent.selected", agent_id="CoordinatorAgent", payload={
            "plan": plan, "reason": refined["reason"],
            "policyId": self.policy.policyId})
        self.state.set_pending(list(plan))

        # Execute the pipeline with failure replanning and loop guarding.
        executed: List[str] = []
        consecutive_failures = 0
        index = 0
        while index < len(plan):
            agent_id = plan[index]
            index += 1
            if len(executed) >= self.policy.maxAgentCount:
                self.degraded_reasons.append("max_agent_count")
                break
            guard = self.guard.check_agent_start(agent_id)
            if guard.triggered:
                await self._emit_guard(guard, agent_id)
                continue
            try:
                definition = self.registry.get(agent_id)
            except KeyError:
                continue
            await self.emitter.emit("agent.started", agent_id=agent_id, payload={
                "description": definition.description, "position": len(executed) + 1,
                "planned": len(plan)})
            agent_started = time.monotonic()
            try:
                output = await asyncio.wait_for(
                    self._run_agent(definition),
                    timeout=definition.timeout_seconds)
                conflicts = self.state.apply_output(output)
                self.state.complete_task(agent_id)
                self.guard.record_completed_agent(agent_id)
                executed.append(agent_id)
                consecutive_failures = 0
                counters = self.agent_counters.get(agent_id, {})
                await self.emitter.emit("agent.completed", agent_id=agent_id, payload={
                    "iterations": counters.get("iterations", 1),
                    "llmCalls": counters.get("llmCalls", 0),
                    "toolCalls": counters.get("toolCalls", 0),
                    "confidence": output.confidence,
                    "summary": output.summary[:300],
                    "conflicts": conflicts,
                    "durationMs": int((time.monotonic() - agent_started) * 1000),
                    "output": {"type": output.type, "claims": len(output.claims),
                               "evidence": len(output.evidence)}})
                if output.requestedNextAction:
                    delegation = self.guard.check_delegation(
                        agent_id, output.requestedNextAction)
                    if not delegation.triggered \
                            and self.registry.known(output.requestedNextAction) \
                            and output.requestedNextAction not in plan[index:] \
                            and output.requestedNextAction not in executed \
                            and len(plan) < self.policy.maxAgentCount + 2:
                        plan.insert(index, output.requestedNextAction)
            except asyncio.CancelledError:
                raise
            except (asyncio.TimeoutError, LlmError, BudgetExceeded, Exception) as exc:  # noqa: BLE001
                if isinstance(exc, BudgetExceeded):
                    raise
                consecutive_failures += 1
                error_text = f"{type(exc).__name__}: {exc}"
                self.failure_notes.append(f"{agent_id} 失败 {error_text[:120]}")
                await self.emitter.emit("agent.failed", agent_id=agent_id, payload={
                    "error": error_text[:300],
                    "durationMs": int((time.monotonic() - agent_started) * 1000)})
                guard = self.guard.check_error(error_text)
                if guard.triggered or consecutive_failures >= 2:
                    self.degraded_reasons.append(f"{agent_id}_failed")
                    if not any(a in TERMINAL_AGENTS for a in plan[index:]):
                        plan.append("ReportAgent")
                    continue
                remaining = coordinator.replan_after_failure(plan[index:], agent_id)
                plan = plan[:index] + remaining
                self.degraded_reasons.append(f"{agent_id}_replaced")

        # Finish: ensure there is a user-facing answer.
        if not self.final_answer:
            self.degraded_reasons.append("no_terminal_answer")
            self.final_answer = self._degraded_answer("报告 Agent 未能完成")
        summary = self._conversation_summary()
        await self._write_memories(summary)
        return self._result("SUCCEEDED", self.final_answer,
                            conversation_summary=summary)

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

        # Deterministic grounding pre-steps guarantee objective tool evidence.
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
            if tool == "calculate_jd_coverage" and call.status == "SUCCEEDED":
                self.state.put_artifact("jdCoverage", call.result)
            if tool == "check_timeline" and call.status == "SUCCEEDED":
                self.state.put_artifact("timelineCheck", call.result)
            if tool == "verify_report_evidence" and call.status == "SUCCEEDED":
                self._apply_verification(call.result)
            if tool == "parse_resume" and call.status == "SUCCEEDED":
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
                    protected_markers=["[当前请求]", "[当前目标]", "[输出要求]"])
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
                # one repair attempt: ask for pure JSON
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
        if definition.agent_id == "ResumeParserAgent" and resume:
            steps.append(("parse_resume", {"resumeText": resume}))
        elif definition.agent_id == "RiskAgent" and resume:
            steps.append(("check_timeline", {"resumeText": resume}))
        elif definition.agent_id == "TechAgent" and resume \
                and (request.jobDescription or "").strip():
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
                f"\n[TOOL_RESULT {call.tool} status={call.status}] {preview}")

    async def _emit_guard(self, guard: Any, agent_id: str) -> None:
        await self.emitter.emit("run.progress", agent_id=agent_id, payload={
            "stage": "loop_guard", "kind": guard.kind,
            "detail": guard.detail, "action": guard.action})

    def _degraded_answer(self, reason: str) -> str:
        """Best-effort answer from whatever the blackboard already holds."""
        state = self.state.data
        sections: List[str] = [f"> 说明：{reason}，以下为基于已完成分析的降级结果。\n"]
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
        except Exception as exc:  # noqa: BLE001
            logger.info("memory write-back skipped: %s", exc)

    def _result(self, status: str, answer: str, *, error_code: Optional[str] = None,
                error_message: Optional[str] = None,
                conversation_summary: Optional[str] = None) -> Dict[str, Any]:
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
        # keep payload bounded
        shared["agentOutputs"] = shared["agentOutputs"][-12:]
        return {
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
