from __future__ import annotations

"""Offline contract gate for the unified agent runtime.

Run from the workflow directory (also executed inside the Docker build):

    python run_agent_harness.py

Uses no model, database, Docker daemon or Java service. It asserts the
deterministic safety contracts of the runtime that must hold even when every
external dependency is unavailable.
"""

import asyncio
import json
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict

WORKFLOW_ROOT = Path(__file__).resolve().parent
if str(WORKFLOW_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKFLOW_ROOT))

from app.conversation import TurnIntent, resolve_turn  # noqa: E402
from app.runtime.context import ContextManager  # noqa: E402
from app.runtime.coordinator import Coordinator, TERMINAL_AGENTS  # noqa: E402
from app.runtime.agents import default_agent_registry  # noqa: E402
from app.runtime.events import NullEmitter  # noqa: E402
from app.runtime.executor import RunExecutor  # noqa: E402
from app.runtime.llm import LlmToolCall, LlmTurn  # noqa: E402
from app.runtime.loop_guard import LoopGuard  # noqa: E402
from app.runtime.mcp_registry import (  # noqa: E402
    McpRegistry,
    McpServerHealth,
    McpToolInfo,
)
from app.runtime.memory import NullMemoryClient  # noqa: E402
from app.runtime.models import (  # noqa: E402
    AgentOutput,
    AgentRunRequest,
    BudgetExceeded,
    ContextBudget,
    PolicyBundle,
    RunBudget,
)
from app.runtime.sandbox_tools_local import run_tool  # noqa: E402
from app.runtime.skills import SkillManager  # noqa: E402
from app.runtime.state import SharedState  # noqa: E402
from app.runtime.builtin_tools import BuiltinToolRegistry  # noqa: E402
from app.runtime.tools import ToolExecutor  # noqa: E402

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {name} {detail}")
    if not condition:
        FAILURES.append(name)


def scenario_coordinator_contracts() -> None:
    policy = PolicyBundle.from_config("balanced", {})
    coordinator = Coordinator(default_agent_registry, policy, llm=None)

    # Primary path: GOAL_ARTIFACTS backward-chaining (not TASK_PIPELINES).
    artifact_plan = coordinator.plan_from_artifacts(
        run_type="full_evaluation", needs_parse=True,
        resume_text="项目经历\n订单中台\n工作经历\n2020-2024 工程师",
        job_description="需要 Java")
    check("artifact_plan_ends_with_terminal",
          artifact_plan["plan"][-1] in TERMINAL_AGENTS,
          detail=str(artifact_plan["plan"]))
    check("artifact_plan_contains_parser",
          "ResumeParserAgent" in artifact_plan["plan"])
    check("artifact_plan_persists_because",
          bool(artifact_plan.get("selectedBecause")))
    no_project = coordinator.plan_from_artifacts(
        run_type="full_evaluation", needs_parse=True,
        resume_text="工作经历\n2020-2024 工程师 技能 Java",
        job_description="Java")
    check("no_project_skips_project",
          "ProjectAgent" not in no_project["plan"],
          detail=str(no_project["plan"]))
    check("no_project_records_skip_reason",
          "ProjectAgent" in (no_project.get("skippedBecause") or {}),
          detail=str(no_project.get("skippedBecause")))

    plan = coordinator.base_plan("full_evaluation", has_resume_facts=False,
                                 needs_parse=True)
    final = coordinator._finalize(plan, "gate")
    check("plan_ends_with_terminal", final["plan"][-1] in TERMINAL_AGENTS)
    check("plan_contains_parser", "ResumeParserAgent" in final["plan"])
    groups = final["parallelGroups"]
    specialists = [g for g in groups if len(g) > 1]
    check("specialists_grouped_parallel", bool(specialists),
          detail=str(groups))
    for group in specialists:
        deps_ok = all(
            not (set(final["dependencies"].get(a, [])) & set(group))
            for a in group)
        check("parallel_group_no_internal_deps", deps_ok, detail=str(group))

    simple = coordinator._finalize(
        coordinator.base_plan("timeline_check", has_resume_facts=False,
                              needs_parse=False), "gate")
    check("simple_route_is_rule_based", coordinator.is_simple("timeline_check"))
    check("simple_route_small", len(simple["plan"]) <= 3, detail=str(simple["plan"]))
    preferred = coordinator._prefer_agent_order(
        ["ProjectAgent", "TechAgent", "ReportAgent"])
    check("agent_order_helper_is_callable",
          preferred == ["TechAgent", "ProjectAgent", "ReportAgent"],
          detail=str(preferred))


def scenario_loop_guard() -> None:
    guard = LoopGuard(max_duplicate_tool_calls=2)
    guard.check_tool_call("sig-a")
    guard.check_tool_call("sig-a")
    decision = guard.check_tool_call("sig-a")
    check("duplicate_tool_guard_trips", decision.triggered)

    exported = guard.export_state()
    restored = LoopGuard(max_duplicate_tool_calls=2)
    restored.restore_state(exported)
    decision2 = restored.check_tool_call("sig-a")
    check("guard_state_survives_snapshot", decision2.triggered)


def scenario_llm_budget_reservations() -> None:
    budget = RunBudget()
    budget.configure_llm_budget(
        12, {"terminal": 2, "control": 4},
        scope_limits={"control": 4})
    for _ in range(4):
        budget.claim_llm_call(12, "control")
    control_blocked = False
    try:
        budget.claim_llm_call(12, "control")
    except BudgetExceeded:
        control_blocked = True
    for _ in range(6):
        budget.claim_llm_call(12, "agent:TechAgent")
    specialist_blocked = False
    try:
        budget.claim_llm_call(12, "agent:ProjectAgent")
    except BudgetExceeded:
        specialist_blocked = True
    budget.claim_llm_call(12, "terminal")
    budget.claim_llm_call(12, "terminal")
    check("control_plane_has_hard_provider_call_ceiling", control_blocked)
    check("specialists_cannot_consume_terminal_reserve", specialist_blocked)
    check("all_provider_calls_share_one_global_cap",
          budget.llm_calls == 12
          and sum(budget.llm_calls_by_scope.values()) == 12,
          detail=str(budget.llm_audit(12)))


def scenario_context_compaction() -> None:
    async def run() -> None:
        budget = ContextBudget(modelWindow=1200, reservedOutputBudget=100,
                               compactAtRatio=0.5)
        manager = ContextManager(budget, NullEmitter(), "gate-run", "gate-conv")
        tool_block = ("\n[TOOL_CALL parse_resume id=tc-aaaa1111aaaa1111]"
                      "\n[TOOL_RESULT parse_resume id=tc-aaaa1111aaaa1111 status=SUCCEEDED] {\"ok\":1}"
                      + "x" * 4000)
        messages = manager.assemble(
            system_prompt="系统", policy_instructions="", skill_instructions="",
            user_request="请评估这份简历的技术栈匹配", current_goal="技术栈匹配",
            shared_state_digest="{}" + "y" * 3000,
            recent_messages=[{"id": 10, "role": "USER", "content": "m" * 1200},
                             {"id": 11, "role": "USER", "content": "最新问题"}],
            conversation_summary="", memory_block="",
            tool_results_block=tool_block, output_schema="{}")
        check("compaction_triggers", manager.needs_compaction(messages))
        compacted = await manager.compact(
            messages, reason="gate",
            protected_markers=["[当前请求]", "[当前目标]", "[输出要求]"],
            recent_messages=[{"id": 10, "role": "USER", "content": "m" * 1200},
                             {"id": 11, "role": "USER", "content": "最新问题"}])
        violations = manager.consistency_check(
            compacted, user_request="请评估这份简历的技术栈匹配",
            current_goal="技术栈匹配")
        check("compaction_keeps_goal_and_pairs", not violations,
              detail=str(violations))
        record = manager.compactions[-1]
        check("compaction_records_message_ids",
              record.source_message_start_id == 10
              and record.source_message_end_id == 11)

    asyncio.run(run())


def scenario_shared_state_conflicts() -> None:
    state = SharedState()
    first = AgentOutput(agentId="TechAgent", type="technical_findings",
                        claims=[{"section": "resume_facts",
                                 "value": {"years": 3}}])
    second = AgentOutput(agentId="ProjectAgent", type="project_findings",
                         claims=[{"section": "resume_facts",
                                  "value": {"years": 5}}])
    state.apply_output(first)
    conflicts = state.apply_output(second)
    check("conflicting_writes_recorded", bool(conflicts)
          and state.data["conflicts"], detail=str(conflicts))
    check("original_value_not_overwritten",
          state.data["resumeFacts"].get("years") == 3)


def scenario_sandbox_tool_contracts() -> None:
    parsed = run_tool("parse_resume", {"resumeText": "技能：Java\n项目：订单系统"})
    check("parse_resume_contract", parsed.get("success") is True
          and "skills" in parsed)
    timeline = run_tool("check_timeline", {
        "resumeText": "2021.01-2023.12 公司A\n2022.06-2024.06 公司B"})
    check("timeline_detects_overlap", timeline.get("success") is True
          and any(i.get("type") == "overlap" for i in timeline.get("issues", [])))
    unknown = run_tool("no_such_tool", {})
    check("unknown_tool_rejected", unknown.get("success") is False)


class _GateMcpClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Dict[str, Any]]] = []

    async def call_tool(self, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        self.calls.append((name, dict(arguments)))
        return {
            "success": True,
            "isError": False,
            "text": "source-backed result",
            "structuredContent": {
                "items": [{"url": "https://example.test/source"}]},
        }


class _GateNativeLlm:
    def __init__(self) -> None:
        self.turn = 0
        self.tool_choice: Any = None
        self.seen_tools: list[Dict[str, Any]] = []

    async def chat_turn(self, messages: list[Dict[str, Any]], *,
                        agent_id: str, purpose: str = "",
                        max_tokens: int = 2048,
                        tools: Any = None, tool_choice: Any = None,
                        use_quality: bool = False) -> LlmTurn:
        self.turn += 1
        self.tool_choice = tool_choice
        self.seen_tools = list(tools or [])
        if self.turn == 1:
            remote = next(
                item["function"] for item in self.seen_tools
                if item["function"].get("description") == "REMOTE_GATE_DESCRIPTION")
            arguments = {"query": "model-created project evidence query"}
            return LlmTurn(
                content="",
                tool_calls=[LlmToolCall(
                    tool_call_id="gate-provider-call-1",
                    name=remote["name"],
                    arguments=arguments,
                    raw_arguments=json.dumps(arguments))],
                finish_reason="tool_calls")
        decision = {
            "thought": "used observed tool result",
            "output": {
                "summary": "project evidence checked",
                "claims": [{
                    "section": "project_findings",
                    "value": [{"text": "source-backed result"}],
                }],
                "evidence": [],
                "confidence": 0.8,
            },
            "done": True,
        }
        return LlmTurn(
            content="",
            tool_calls=[LlmToolCall(
                tool_call_id="gate-final-call-2",
                name="emit_decision",
                arguments=decision,
                raw_arguments=json.dumps(decision))],
            finish_reason="tool_calls")


def scenario_native_mcp_and_progressive_skills() -> None:
    # agentskills.io disclosure contract: metadata first, body after load,
    # referenced content only after an explicit resource read.
    with tempfile.TemporaryDirectory(prefix="resumai-skill-gate-") as temp:
        package = Path(temp) / "gate-skill"
        references = package / "references"
        references.mkdir(parents=True)
        (package / "SKILL.md").write_text(
            "---\nname: gate-skill\n"
            "description: Startup metadata only.\nversion: v1\n---\n\n"
            "BODY_GATE_SENTINEL\n",
            encoding="utf-8")
        (references / "details.md").write_text(
            "RESOURCE_GATE_SENTINEL", encoding="utf-8")
        manager = SkillManager(Path(temp))
        metadata = manager.get("gate-skill")
        check("skill_catalog_is_metadata_only",
              metadata.instructions == ""
              and metadata.hash == "not-loaded"
              and "BODY_GATE_SENTINEL" not in manager.render([metadata]))
        loaded = manager.load("gate-skill")
        check("skill_body_loads_after_activation",
              loaded.loaded and "BODY_GATE_SENTINEL" in loaded.instructions
              and loaded.hash != "not-loaded")
        check("skill_resource_is_on_demand",
              "RESOURCE_GATE_SENTINEL" not in loaded.instructions
              and "RESOURCE_GATE_SENTINEL" in manager.read_resource(
                  "gate-skill", "references/details.md"))

    async def run_native() -> None:
        request = AgentRunRequest(
            runId="gate-native", conversationId="gate-conversation",
            traceId="gate-trace", runType="full_evaluation",
            userMessage="核验公开项目",
            resumeText=(
                "项目经历\nDemo\nhttps://example.test/repo\nPython FastAPI"),
            jobDescription="Python backend")
        emitter = NullEmitter(
            request.runId, request.conversationId, request.traceId)
        llm = _GateNativeLlm()
        executor = RunExecutor(
            request, emitter, memory=NullMemoryClient(),
            builtin_tools=BuiltinToolRegistry(), llm=llm)
        client = _GateMcpClient()
        registry = McpRegistry(config={
            "mcpServers": {},
            "optionalMcpServers": {},
            "agentToolRouting": {"ProjectAgent": ["gate.search"]},
        })
        registry.health["gate"] = McpServerHealth(
            name="gate", status="AVAILABLE",
            transport="streamable-http", tools=["gate.search"])
        registry.tools["gate.search"] = McpToolInfo(
            server="gate", name="remote_search",
            catalog_name="gate.search",
            description="REMOTE_GATE_DESCRIPTION",
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "model query"}},
                "required": ["query"],
            })
        registry._http_clients["gate"] = client
        executor.tools.attach_mcp(registry)
        executor.state.apply_artifacts({
            "resumeFacts": {"projects": [{"name": "Demo"}]}})

        catalog = executor.tools.catalog_for_agent("ProjectAgent", [])
        provider_tools, aliases = ToolExecutor.openai_tools(catalog)
        remote = next(
            item["function"] for item in provider_tools
            if item["function"]["description"] == "REMOTE_GATE_DESCRIPTION")
        check("mcp_tools_list_schema_reaches_model",
              remote["parameters"].get("required") == ["query"]
              and aliases.get(remote["name"]) == "gate.search")
        presteps = executor._pre_steps(
            default_agent_registry.get("ProjectAgent"))
        check("mcp_is_not_forced_by_agent",
              all(not (
                  executor.tools.definitions.get(name)
                  and executor.tools.definitions[name].kind == "mcp")
                  for name, _arguments in presteps))

        output = await executor._run_agent(
            default_agent_registry.get("ProjectAgent"))
        check("native_tool_choice_is_auto", llm.tool_choice == "auto")
        check("model_generated_mcp_arguments",
              client.calls == [(
                  "remote_search",
                  {"query": "model-created project evidence query"})])
        check("native_mcp_observation_reaches_agent",
              output.summary == "project evidence checked")
        chain = [
            event for event in emitter.events
            if event.get("payload", {}).get("toolCallId")
            == "gate-provider-call-1"]
        stages = [
            event.get("payload", {}).get("lifecycleStage") for event in chain]
        expected = [
            "CATALOG_EXPOSED", "LLM_PROPOSED",
            "EXECUTION_STARTED", "RESULT",
        ]
        check("mcp_trace_keeps_provider_tool_call_id",
              all(stage in stages for stage in expected)
              and [stages.index(stage) for stage in expected]
              == sorted(stages.index(stage) for stage in expected),
              detail=str(stages))

    asyncio.run(run_native())


def scenario_revision_artifact_reuse() -> None:
    request = AgentRunRequest(
        runId="gate-revision-2", conversationId="gate-conversation",
        revision=2, runType="full_evaluation",
        resumeText="项目经历\nDemo\nPython",
        jobDescription="new Python JD",
        previousArtifacts={
            "resumeFacts": {"projects": [{"name": "Demo"}]},
            "parsedResume": {"skills": ["Python"]},
            "jdRequirements": {"must": ["old"]},
            "technicalFindings": [{"text": "old"}],
            "projectFindings": [{"text": "old"}],
            "risks": [{"claim": "timeline"}],
            "evidence": [{"text": "old"}],
            "finalReport": {"recommendation": "HIRE"},
        },
        invalidatedArtifacts=["jdRequirements"])
    executor = RunExecutor(
        request, NullEmitter(), memory=NullMemoryClient(),
        builtin_tools=BuiltinToolRegistry(), llm=_GateNativeLlm())
    reuse = executor._reuse_previous_revision_artifacts()
    artifacts = executor.state.artifacts()
    check("revision_reuses_unaffected_artifacts",
          bool(artifacts.get("resumeFacts")) and bool(artifacts.get("risks")))
    check("revision_discards_downstream_artifacts",
          all(not artifacts.get(key) for key in (
              "jdRequirements", "technicalFindings", "projectFindings",
              "evidence", "finalReport")),
          detail=str(reuse.get("invalidatedArtifacts")))
    planned = Coordinator(
        default_agent_registry,
        PolicyBundle.from_config("balanced", {}), None).plan_from_artifacts(
            run_type="full_evaluation", needs_parse=False,
            resume_text=request.resumeText or "",
            job_description=request.jobDescription or "",
            artifacts=artifacts)
    check("revision_skips_unaffected_agents",
          "ResumeParserAgent" not in planned["plan"]
          and "RiskAgent" not in planned["plan"],
          detail=str(planned["plan"]))


def scenario_turn_routing() -> None:
    cancel = resolve_turn("停止评估", run_status="RUNNING", revision=1, context={})
    check("explicit_cancel_is_deterministic",
          cancel.intent == TurnIntent.CANCEL and cancel.control_action == "CANCEL")
    pause = resolve_turn("先暂停一下", run_status="RUNNING", revision=1, context={})
    check("explicit_pause_writes_checkpoint_contract",
          pause.intent == TurnIntent.PAUSE and pause.control_action == "PAUSE")
    resume = resolve_turn("继续评估", run_status="PAUSED", revision=1, context={})
    check("explicit_resume_uses_checkpoint_contract",
          resume.intent == TurnIntent.RESUME and resume.control_action == "RESUME")
    negation = resolve_turn("不要取消，继续", run_status="RUNNING", revision=1,
                            context={})
    check("negated_cancel_not_triggered", negation.control_action != "CANCEL",
          detail=str(negation.intent))
    goal = resolve_turn(
        "目标岗位改成 Python Agent 平台工程师",
        run_status="RUNNING", revision=1, context={})
    check("goal_change_creates_selective_revision",
          goal.intent == TurnIntent.GOAL_CHANGE
          and goal.affects_evaluation
          and "resume_parse" not in goal.affected_nodes
          and "report" in goal.affected_nodes,
          detail=str(goal.affected_nodes))
    side = resolve_turn(
        "顺便告诉我这个分数怎么算？",
        run_status="RUNNING", revision=1,
        context={"structuredReport": {"recommendation": "HIRE"}})
    check("side_question_does_not_interrupt_run",
          side.intent == TurnIntent.SIDE_QUESTION
          and not side.affects_evaluation
          and side.answer_then_resume)


def main() -> int:
    print("== unified agent runtime contract gate ==")
    scenario_coordinator_contracts()
    scenario_loop_guard()
    scenario_llm_budget_reservations()
    scenario_context_compaction()
    scenario_shared_state_conflicts()
    scenario_sandbox_tool_contracts()
    scenario_native_mcp_and_progressive_skills()
    scenario_revision_artifact_reuse()
    scenario_turn_routing()
    print(json.dumps({"failures": FAILURES}, ensure_ascii=False))
    return 1 if FAILURES else 0


if __name__ == "__main__":
    raise SystemExit(main())
