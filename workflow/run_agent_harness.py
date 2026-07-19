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
from app.runtime.loop_guard import LoopGuard  # noqa: E402
from app.runtime.models import ContextBudget, PolicyBundle  # noqa: E402
from app.runtime.sandbox_tools_local import run_tool  # noqa: E402
from app.runtime.state import SharedState  # noqa: E402
from app.runtime.models import AgentOutput  # noqa: E402

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {name} {detail}")
    if not condition:
        FAILURES.append(name)


def scenario_coordinator_contracts() -> None:
    policy = PolicyBundle.from_config("balanced", {})
    coordinator = Coordinator(default_agent_registry, policy, llm=None)

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


def scenario_turn_routing() -> None:
    cancel = resolve_turn("停止评估", run_status="RUNNING", revision=1, context={})
    check("explicit_cancel_is_deterministic",
          cancel.intent == TurnIntent.CANCEL and cancel.control_action == "CANCEL")
    negation = resolve_turn("不要取消，继续", run_status="RUNNING", revision=1,
                            context={})
    check("negated_cancel_not_triggered", negation.control_action != "CANCEL",
          detail=str(negation.intent))


def main() -> int:
    print("== unified agent runtime contract gate ==")
    scenario_coordinator_contracts()
    scenario_loop_guard()
    scenario_context_compaction()
    scenario_shared_state_conflicts()
    scenario_sandbox_tool_contracts()
    scenario_turn_routing()
    print(json.dumps({"failures": FAILURES}, ensure_ascii=False))
    return 1 if FAILURES else 0


if __name__ == "__main__":
    raise SystemExit(main())
