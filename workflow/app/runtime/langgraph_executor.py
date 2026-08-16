from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Annotated, Any, Dict, List, Optional, TypedDict

from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, Send, interrupt

from app.runtime.checkpoint import get_checkpointer
from app.runtime.coordinator import Coordinator, FULL_EVAL_TYPES, TERMINAL_AGENTS
from app.runtime.executor import RunExecutor
from app.runtime.memory import memory_trace_entries
from app.runtime.models import AgentOutput, BudgetExceeded

logger = logging.getLogger(__name__)


def _reduce_agent_results(
        left: Optional[List[Dict[str, Any]]],
        right: Optional[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    """LangGraph reducer for parallel ``Send`` specialist results."""

    return list(left or []) + list(right or [])


class RuntimeGraphState(TypedDict, total=False):
    run_id: str
    execution_snapshot: Dict[str, Any]
    needs_plan_approval: bool
    pause_requested: bool
    pause_reason: str
    done: bool
    group_token: int
    dispatch_agents: List[str]
    consecutive_failures: int
    agent_results: Annotated[List[Dict[str, Any]], _reduce_agent_results]
    result: Dict[str, Any]


class LangGraphRunExecutor(RunExecutor):
    """LangGraph orchestration over the existing production Agent runtime.

    The existing Agent loop, tools, MCP, Skills, memory, budgets and report
    validation remain the execution primitives. LangGraph owns the durable
    orchestration boundaries: initial planning, ``Send`` fan-out, reducer
    merge, checkpointing and finalization.
    """

    def _hydrate(self, state: RuntimeGraphState) -> bool:
        """Restore the mutable runtime once when a checkpoint resumes."""

        if getattr(self, "_langgraph_hydrated", False):
            return False
        snapshot = state.get("execution_snapshot") or self.request.resumeSnapshot
        restored = bool(snapshot and self._restore_snapshot(snapshot))
        self._langgraph_hydrated = True
        return restored

    @staticmethod
    def _write_custom(stage: str, **payload: Any) -> None:
        """Publish a LangGraph custom-stream event when inside a graph task."""

        try:
            writer = get_stream_writer()
            writer({"stage": stage, **payload})
        except Exception:
            # Unit-level direct node calls do not have a graph stream writer.
            pass

    def build_graph(self, checkpointer: Any) -> Any:
        builder = StateGraph(RuntimeGraphState)
        builder.add_node("observe_plan", self._observe_plan_node)
        builder.add_node("approval_gate", self._approval_gate_node)
        builder.add_node("dispatch", self._dispatch_node)
        builder.add_node("agent", self._agent_node)
        builder.add_node("merge", self._merge_node)
        builder.add_node("pause_gate", self._pause_gate_node)
        builder.add_node("finalize", self._finalize_node)

        builder.add_edge(START, "observe_plan")
        builder.add_conditional_edges(
            "observe_plan",
            self._route_after_plan,
            {"approval_gate": "approval_gate", "dispatch": "dispatch"},
        )
        builder.add_conditional_edges("dispatch", self._route_dispatch)
        builder.add_edge("agent", "merge")
        builder.add_edge("finalize", END)
        return builder.compile(
            checkpointer=checkpointer,
            name="resumai-agent-runtime",
        )

    async def _execute_inner(self) -> Dict[str, Any]:
        checkpointer = await get_checkpointer()
        graph = self.build_graph(checkpointer)
        config = {
            "configurable": {"thread_id": self.request.runId},
            "recursion_limit": 100,
        }

        saved = await graph.aget_state(config)
        graph_input: Any
        if saved.values:
            completed = saved.values.get("result")
            if completed and not saved.next:
                return dict(completed)
            if self._state_is_interrupted(saved):
                if not self.request.resumeSnapshot:
                    return self._paused_result(saved.values)
                graph_input = Command(resume={
                    "resumeSnapshot": self.request.resumeSnapshot,
                })
            else:
                # Resume the last durable super-step after process failure.
                graph_input = None
        else:
            graph_input = {
                "run_id": self.request.runId,
                "agent_results": [],
                "consecutive_failures": 0,
                "pause_requested": False,
                "done": False,
            }

        async for mode, chunk in graph.astream(
                graph_input,
                config,
                stream_mode=["updates", "custom"],
                durability="sync"):
            if mode != "custom" or not isinstance(chunk, dict):
                continue
            await self.emitter.emit("run.progress", payload={
                "runtime": "langgraph",
                **chunk,
            })

        saved = await graph.aget_state(config)
        if self._state_is_interrupted(saved):
            return self._paused_result(saved.values)
        result = saved.values.get("result") if saved.values else None
        if isinstance(result, dict):
            return result
        raise RuntimeError("LangGraph finished without a terminal result")

    @staticmethod
    def _state_is_interrupted(snapshot: Any) -> bool:
        return any(getattr(task, "interrupts", ()) for task in snapshot.tasks)

    def _paused_result(self, values: Dict[str, Any]) -> Dict[str, Any]:
        snapshot = dict(values.get("execution_snapshot") or self.export_snapshot())
        snapshot["pauseReason"] = values.get("pause_reason") or "USER_PAUSED"
        return self._result("PAUSED", "", snapshot=snapshot)

    async def _observe_plan_node(
            self, state: RuntimeGraphState) -> Dict[str, Any]:
        request = self.request
        resumed = self._hydrate(state)
        coordinator = Coordinator(self.registry, self.policy, self.llm)

        if resumed:
            if self._regroup_needed:
                refreshed = coordinator._finalize(
                    self.plan, "user_approved_plan")
                self.plan = refreshed["plan"]
                self.parallel_groups = refreshed["parallelGroups"]
                self.budget_plan = refreshed.get("budgetPlan") or {}
                self.next_group_index = 0
                self.state.set_pending(list(self.plan))
                await self.emitter.emit(
                    "agent.selected", agent_id="CoordinatorAgent", payload={
                        "plan": self.plan,
                        "reason": "用户确认/编辑后的计划",
                        "parallelGroups": self.parallel_groups,
                        "requiredTerminalAgent": refreshed[
                            "requiredTerminalAgent"],
                        "policyId": self.policy.policyId,
                        "budgetPlan": self.budget_plan,
                        "approved": True,
                    })
            await self.emitter.emit("run.progress", payload={
                "stage": "resume",
                "message": f"从快照恢复：已完成 {len(self.executed)} 个 Agent",
                "executedAgents": self.executed,
                "runtime": "langgraph",
            })
            self._write_custom(
                "langgraph.resume", executedAgents=list(self.executed))
            return {
                "execution_snapshot": self.export_snapshot(),
                "needs_plan_approval": False,
            }

        await self.emitter.emit("run.progress", payload={
            "stage": "observe", "message": "加载记忆与上下文",
            "runtime": "langgraph",
        })
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

        memory_query, memory_query_basis = self._memory_retrieval_query(request)
        recall_limit = self.policy.memoryRetrieval.topK
        recent_case_hits, job_profile_hits = (
            await asyncio.gather(
                self.memory.search(
                    memory_query, types=["RECENT_CASE"],
                    top_k=min(2, recall_limit),
                    min_confidence=self.policy.memoryRetrieval.minConfidence,
                    consumer_agent="SpecialistAgent"),
                self.memory.search(
                    memory_query, types=["JOB_PROFILE"], top_k=1,
                    min_confidence=self.policy.memoryRetrieval.minConfidence,
                    consumer_agent="SpecialistAgent"),
            ))
        recent_case_hits = [
            hit for hit in recent_case_hits
            if self._business_memory_matches_request(hit, request)]
        job_profile_hits = [
            hit for hit in job_profile_hits
            if self._business_memory_matches_request(hit, request)]
        self.memory_hits = self._merge_memory_hits(
            recent_case_hits, job_profile_hits,
            limit=self.policy.memoryRetrieval.topK)
        self.failure_hits = []
        self.failure_notes = []

        type_counts: Dict[str, int] = {}
        for hit in self.memory_hits:
            hit_type = str(hit.get("type") or "UNKNOWN")
            type_counts[hit_type] = type_counts.get(hit_type, 0) + 1
        observe_trace = memory_trace_entries(
            [{"used": True, "ignoredReason": None, **hit}
             for hit in self.memory_hits],
            [], "SpecialistAgent",
        )
        self.memory_traces.extend(observe_trace)
        await self.emitter.emit("run.progress", payload={
            "stage": "memory",
            "message": f"岗位业务记忆命中 {len(self.memory_hits)} 条",
            "memoryHits": len(self.memory_hits),
            "failureHits": 0,
            "queryBasis": memory_query_basis + ["same_job_business_memory"],
            "retrievedTypeCounts": {
                "RECENT_CASE": len(recent_case_hits),
                "JOB_PROFILE": len(job_profile_hits),
            },
            "memoryTypeCounts": type_counts,
            "memoryTrace": observe_trace[:12],
        })

        if getattr(self, "_mcp_attach_pending", False):
            try:
                from app.runtime.mcp_registry import get_mcp_registry

                registry = await get_mcp_registry(probe=True)
                self.tools.attach_mcp(registry)
            except Exception as exc:
                logger.info("MCP probe skipped: %s", exc)
            self._mcp_attach_pending = False

        await self._prepare_context()
        artifacts = self.state.artifacts()
        needs_parse = (
            bool(request.resumeText)
            and not artifacts.get("resumeFacts")
            and request.runType in (
                "full_evaluation", "jd_evaluation", "backend_eval",
                "agent_eval", "resume_optimize", "project_rewrite")
        )
        business_memories = [
            hit for hit in self.memory_hits
            if isinstance(hit, dict)
            and str(hit.get("type") or "") in {
                "RECENT_CASE", "JOB_PROFILE"}
        ]
        planned = await coordinator.plan(
            run_type=request.runType,
            user_message=request.userMessage,
            conversation_summary=request.conversationSummary or "",
            shared_digest=self.state.view_for(
                "CoordinatorAgent", max_chars=2000),
            failure_notes=self.failure_notes,
            memory_notes=business_memories or [
                str(hit.get("content", ""))[:120]
                for hit in self.memory_hits[:3]],
            needs_parse=needs_parse,
            resume_text=request.resumeText or "",
            job_description=request.jobDescription or "",
            artifacts=artifacts,
            shared=self.state.data,
        )
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
        if request.runType in FULL_EVAL_TYPES:
            self.budget.release_llm_reservation("control")
        await self.emitter.emit(
            "agent.selected", agent_id="CoordinatorAgent", payload={
                "plan": self.plan,
                "reason": planned["reason"],
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
                "memoryNotes": [
                    str(hit.get("content", ""))[:120]
                    for hit in self.memory_hits[:3]],
                "runtime": "langgraph",
            })
        self.state.set_pending(list(self.plan))
        snapshot = self.export_snapshot()
        if request.planMode:
            snapshot["pauseReason"] = "AWAITING_PLAN_APPROVAL"
        self._write_custom(
            "langgraph.plan",
            plan=list(self.plan),
            parallelGroups=[list(group) for group in self.parallel_groups],
        )
        return {
            "execution_snapshot": snapshot,
            "needs_plan_approval": bool(request.planMode),
            "pause_reason": (
                "AWAITING_PLAN_APPROVAL" if request.planMode else ""),
        }

    @staticmethod
    def _route_after_plan(state: RuntimeGraphState) -> str:
        return "approval_gate" if state.get(
            "needs_plan_approval") else "dispatch"

    async def _approval_gate_node(
            self, state: RuntimeGraphState) -> Command:
        self._hydrate(state)
        resumed = interrupt({
            "runId": self.request.runId,
            "pauseReason": "AWAITING_PLAN_APPROVAL",
            "executionSnapshot": state.get("execution_snapshot") or {},
        })
        approved = resumed.get("resumeSnapshot") \
            if isinstance(resumed, dict) else None
        if isinstance(approved, dict) and approved:
            self._restore_snapshot(approved)
        if self._regroup_needed:
            coordinator = Coordinator(self.registry, self.policy, self.llm)
            refreshed = coordinator._finalize(
                self.plan, "user_approved_plan")
            self.plan = refreshed["plan"]
            self.parallel_groups = refreshed["parallelGroups"]
            self.budget_plan = refreshed.get("budgetPlan") or {}
            self.next_group_index = 0
            self.state.set_pending(list(self.plan))
        await self.emitter.emit("run.progress", payload={
            "stage": "plan_approved",
            "message": "已从 LangGraph checkpoint 恢复并执行确认后的计划",
            "runtime": "langgraph",
        })
        self._write_custom("langgraph.plan_approved")
        return Command(
            goto="dispatch",
            update={
                "needs_plan_approval": False,
                "pause_reason": "",
                "execution_snapshot": self.export_snapshot(),
            },
        )

    async def _dispatch_node(
            self, state: RuntimeGraphState) -> Dict[str, Any]:
        self._hydrate(state)
        if self.pause_event is not None and self.pause_event.is_set():
            return {
                "pause_requested": True,
                "pause_reason": "USER_PAUSED",
                "execution_snapshot": self.export_snapshot(),
            }

        while self.next_group_index < len(self.parallel_groups):
            group = [
                agent_id
                for agent_id in self.parallel_groups[self.next_group_index]
                if agent_id not in self.executed
            ]
            self.next_group_index += 1
            if not group:
                continue
            if (len(self.executed) >= self.policy.maxAgentCount
                    and not any(agent_id in TERMINAL_AGENTS
                                for agent_id in group)):
                self.degraded_reasons.append("max_agent_count")
                continue

            runnable = []
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

            base_position = len(self.executed)
            agent_ids = [definition.agent_id for definition in runnable]
            for offset, definition in enumerate(runnable):
                await self.emitter.emit(
                    "agent.started", agent_id=definition.agent_id, payload={
                        "description": definition.description,
                        "parallelGroup": agent_ids if len(agent_ids) > 1 else [],
                        "position": base_position + offset + 1,
                        "planned": len(self.plan),
                        "runtime": "langgraph",
                    })
            token = self.next_group_index
            self._write_custom(
                "langgraph.dispatch",
                groupToken=token,
                agents=agent_ids,
                parallel=len(agent_ids) > 1,
            )
            return {
                "done": False,
                "pause_requested": False,
                "group_token": token,
                "dispatch_agents": agent_ids,
                "execution_snapshot": self.export_snapshot(),
            }

        return {
            "done": True,
            "dispatch_agents": [],
            "execution_snapshot": self.export_snapshot(),
        }

    @staticmethod
    def _route_dispatch(
            state: RuntimeGraphState) -> str | List[Send]:
        if state.get("pause_requested"):
            return "pause_gate"
        if state.get("done"):
            return "finalize"
        token = int(state.get("group_token") or 0)
        snapshot = state.get("execution_snapshot") or {}
        return [
            Send("agent", {
                "agent_id": agent_id,
                "group_token": token,
                "execution_snapshot": snapshot,
            })
            for agent_id in state.get("dispatch_agents") or []
        ]

    async def _agent_node(self, task: Dict[str, Any]) -> Dict[str, Any]:
        self._hydrate(task)  # type: ignore[arg-type]
        agent_id = str(task["agent_id"])
        definition = self.registry.get(agent_id)
        started = time.monotonic()
        output: Optional[AgentOutput] = None
        error_type: Optional[str] = None
        error_message: Optional[str] = None
        budget_kind: Optional[str] = None
        try:
            output = await asyncio.wait_for(
                self._run_agent(definition),
                timeout=definition.timeout_seconds,
            )
        except asyncio.CancelledError:
            raise
        except BudgetExceeded as exc:
            error_type = type(exc).__name__
            error_message = str(exc)[:800]
            budget_kind = exc.kind
        except Exception as exc:
            error_type = type(exc).__name__
            error_message = str(exc)[:800]

        duration_ms = int((time.monotonic() - started) * 1000)
        result = {
            "groupToken": int(task.get("group_token") or 0),
            "agentId": agent_id,
            "output": output.model_dump() if output is not None else None,
            "errorType": error_type,
            "errorMessage": error_message,
            "budgetKind": budget_kind,
            "durationMs": duration_ms,
            # A completed Send task is durably written by LangGraph. This
            # snapshot lets a replacement process restore counters/tool ledger
            # before the reducer merge without repeating completed siblings.
            "runtimeSnapshot": self.export_snapshot(),
        }
        self._write_custom(
            "langgraph.agent_result",
            groupToken=result["groupToken"],
            agentId=agent_id,
            ok=output is not None,
            durationMs=duration_ms,
        )
        return {"agent_results": [result]}

    def _restore_worker_snapshot(
            self, results: List[Dict[str, Any]]) -> None:
        candidates = [
            result.get("runtimeSnapshot")
            for result in results
            if isinstance(result.get("runtimeSnapshot"), dict)
        ]
        if not candidates:
            return
        richest = max(
            candidates,
            key=lambda snap: (
                int((snap.get("budget") or {}).get("llmCalls", 0)),
                int((snap.get("budget") or {}).get("toolCalls", 0)),
                float(snap.get("createdAt", 0.0)),
            ),
        )
        self._restore_snapshot(richest)

    async def _merge_node(self, state: RuntimeGraphState) -> Command:
        restored = self._hydrate(state)
        token = int(state.get("group_token") or 0)
        current = [
            result for result in state.get("agent_results") or []
            if int(result.get("groupToken") or 0) == token
        ]
        by_agent = {str(result.get("agentId")): result for result in current}
        ordered = [
            by_agent[agent_id]
            for agent_id in state.get("dispatch_agents") or []
            if agent_id in by_agent
        ]
        if restored:
            self._restore_worker_snapshot(ordered)

        any_success = False
        evidence_ran = False
        for result in ordered:
            agent_id = str(result.get("agentId"))
            definition = self.registry.get(agent_id)
            duration_ms = max(0, int(result.get("durationMs") or 0))
            synthetic_started = time.monotonic() - duration_ms / 1000.0
            raw_output = result.get("output")
            if isinstance(raw_output, dict):
                output = AgentOutput.model_validate(raw_output)
                conflicts = self.state.apply_output(output)
                self._after_agent_success(
                    definition, output, conflicts, synthetic_started,
                    fire_started=False)
                any_success = True
                evidence_ran = evidence_ran or agent_id == "EvidenceAgent"
                continue

            message = str(result.get("errorMessage") or "agent failed")
            budget_kind = result.get("budgetKind")
            if budget_kind:
                exc = BudgetExceeded(str(budget_kind), message)
                if (agent_id not in TERMINAL_AGENTS
                        and budget_kind in {
                            "llmReservation", "llmScopeLimit"}):
                    await self._after_agent_failure(
                        definition, exc, synthetic_started)
                    continue
                raise exc
            await self._after_agent_failure(
                definition,
                RuntimeError(
                    f"{result.get('errorType') or 'AgentError'}: {message}"),
                synthetic_started,
            )

        consecutive = int(state.get("consecutive_failures") or 0)
        consecutive = 0 if any_success else consecutive + 1
        if consecutive >= 2:
            self.degraded_reasons.append("consecutive_failures")
            self._ensure_terminal_tail()
        if evidence_ran:
            await self._arbitrate_conflicts()

        self._write_custom(
            "langgraph.reducer_merge",
            groupToken=token,
            agents=[result.get("agentId") for result in ordered],
            groupOk=any_success,
        )
        snapshot = self.export_snapshot()
        # MySQL remains the Java control-plane snapshot/audit copy. PostgreSQL
        # is the actual graph checkpointer and is committed by LangGraph with
        # durability="sync" at this super-step boundary.
        await self.emitter.save_checkpoint(snapshot)
        self._write_custom(
            "langgraph.group_checkpoint",
            groupToken=token,
            nextGroupIndex=self.next_group_index,
        )
        if self.pause_event is not None and self.pause_event.is_set():
            return Command(
                goto="pause_gate",
                update={
                    "consecutive_failures": consecutive,
                    "pause_requested": True,
                    "pause_reason": "USER_PAUSED",
                    "execution_snapshot": snapshot,
                },
            )
        return Command(
            goto="dispatch",
            update={
                "consecutive_failures": consecutive,
                "pause_requested": False,
                "execution_snapshot": snapshot,
            },
        )

    async def _pause_gate_node(
            self, state: RuntimeGraphState) -> Command:
        self._hydrate(state)
        resumed = interrupt({
            "runId": self.request.runId,
            "pauseReason": state.get("pause_reason") or "USER_PAUSED",
            "executionSnapshot": state.get("execution_snapshot") or {},
        })
        supplied = resumed.get("resumeSnapshot") \
            if isinstance(resumed, dict) else None
        if isinstance(supplied, dict) and supplied:
            self._restore_snapshot(supplied)
        if self.pause_event is not None:
            self.pause_event.clear()
        self._write_custom("langgraph.resumed")
        return Command(
            goto="dispatch",
            update={
                "pause_requested": False,
                "pause_reason": "",
                "execution_snapshot": self.export_snapshot(),
            },
        )

    async def _finalize_node(
            self, state: RuntimeGraphState) -> Dict[str, Any]:
        self._hydrate(state)
        if not self.final_answer:
            self.degraded_reasons.append("no_terminal_answer")
            self.final_answer = self._degraded_answer("报告 Agent 未能完成")
            self.report_agent_failed = True
        summary = self._conversation_summary()
        await self._write_memories(summary)
        missing_goals = self._missing_required_goal_artifacts()
        status = "PARTIAL_SUCCESS" if (
            self.report_agent_failed
            or self._has_hard_degradation()
            or missing_goals
        ) else "SUCCEEDED"
        error_code = None
        error_message = None
        if missing_goals:
            self.degraded_reasons.append("missing_goal_artifacts")
            error_code = "MISSING_GOAL_ARTIFACTS"
            error_message = (
                "缺少必选 goal artifacts: " + ", ".join(missing_goals))
        if status == "SUCCEEDED" and self._requires_score_contract():
            contract_error = self._report_contract_violation()
            if contract_error:
                status = "PARTIAL_SUCCESS"
                error_code = "REPORT_CONTRACT_FAILED"
                error_message = contract_error
                self.degraded_reasons.append("report_contract_failed")
        result = self._result(
            status,
            self.final_answer,
            error_code=error_code,
            error_message=error_message,
            conversation_summary=summary,
            missing_goal_artifacts=missing_goals,
        )
        self._write_custom(
            "langgraph.finalize", status=status,
            executedAgents=list(self.executed))
        return {
            "done": True,
            "result": result,
            "execution_snapshot": self.export_snapshot(),
        }
