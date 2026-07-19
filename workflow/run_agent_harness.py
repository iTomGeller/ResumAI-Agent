from __future__ import annotations

"""Offline regression gate for the conversational Agent runtime.

Run from the repository root:

    python workflow/run_agent_harness.py

The gate intentionally uses no model, MCP server, database, or Java service.
It exercises the deterministic safety contracts that must hold even when every
external dependency is unavailable.
"""

import asyncio
import argparse
import json
import sys
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List

WORKFLOW_ROOT = Path(__file__).resolve().parent
HARNESS_MANIFEST = WORKFLOW_ROOT / "harness" / "scenarios.json"
if str(WORKFLOW_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKFLOW_ROOT))

from app.agent_harness import (  # noqa: E402
    AgentToolLedger,
    audit_external_evidence,
    audit_external_subject_binding,
    fence_workflow_result,
    guard_tool_proposal_batch,
    plan_revision_execution,
    select_phase4_nodes,
    terminal_status_for_degradation,
    validate_timeline_text,
    validate_trace_contract,
)
from app.conversation import TurnIntent, resolve_turn  # noqa: E402
from app.run_control import RunRegistry, RunStatus, safe_control_boundary  # noqa: E402


Scenario = Callable[[], Awaitable[Dict[str, Any]]]


def _base_checkpoint() -> Dict[str, Any]:
    return {
        "intentResult": "{}",
        "harnessPlan": {},
        "harnessContext": {},
        "memoryContext": {},
        "parseResult": "{}",
        "jdResult": "{}",
        "knowledgeContext": {},
        "techResult": "{}",
        "projectResult": "{}",
        "riskResult": "{}",
        "fusionResult": "{}",
        "finalReport": "report",
        "overallScore": 0,
        "recommendation": "NEED_MANUAL_REVIEW",
        "strengths": [],
        "risks": [],
        "interviewQuestions": [],
        "degradedReasons": [],
    }


async def side_quest_keeps_run_alive() -> Dict[str, Any]:
    decision = resolve_turn(
        "先解释一下 RAG 命中低是什么意思？",
        context={"runStatus": "RUNNING", "currentNode": "tech_eval"},
    )
    assert decision.intent == TurnIntent.SIDE_QUESTION
    assert not decision.affects_evaluation
    assert decision.answer_then_resume
    assert decision.control_action is None
    return {"intent": decision.intent.value, "activeNode": "tech_eval"}


async def goal_change_is_minimal_revision() -> Dict[str, Any]:
    decision = resolve_turn("改成前端岗位重新评估")
    assert decision.intent == TurnIntent.GOAL_CHANGE
    plan = plan_revision_execution(decision.affected_nodes, _base_checkpoint())
    assert "resume_parse" in plan.reused_nodes
    assert "jd_match" in plan.execute_nodes
    assert "report" in plan.execute_nodes
    assert not plan.cache_miss_nodes
    return plan.to_dict()


async def pause_checkpoint_resume_same_run() -> Dict[str, Any]:
    class GraphInterrupt(Exception):
        pass

    registry = RunRegistry()
    state = {
        "workflowRunId": "harness-run-pause",
        "traceId": "harness-trace-pause",
        "conversationId": "harness-conversation",
        "revision": 2,
    }
    await registry.register(
        state["workflowRunId"],
        state["traceId"],
        state["conversationId"],
        state["revision"],
        state,
    )
    await registry.request_pause(state["workflowRunId"])
    checkpoint_payload: Dict[str, Any] = {}

    def interrupt(payload: Dict[str, Any]) -> None:
        checkpoint_payload.update(payload)
        raise GraphInterrupt()

    try:
        await safe_control_boundary(state, registry=registry, interrupter=interrupt)
    except GraphInterrupt:
        pass
    else:
        raise AssertionError("pause did not interrupt at a checkpoint boundary")
    await registry.mark_paused(state["workflowRunId"])
    resume = await registry.request_resume(state["workflowRunId"])
    assert resume.restart_required
    await safe_control_boundary(
        state,
        registry=registry,
        interrupter=lambda payload: {"action": "RESUME", "revision": payload["revision"]},
    )
    snapshot = await registry.require(state["workflowRunId"])
    assert snapshot.status == RunStatus.RUNNING
    assert checkpoint_payload["workflowRunId"] == state["workflowRunId"]
    assert checkpoint_payload["revision"] == state["revision"]
    return {"checkpoint": checkpoint_payload, "status": snapshot.status.value}


async def cancel_is_immediate_and_terminal() -> Dict[str, Any]:
    registry = RunRegistry()
    await registry.register("harness-run-cancel", "trace-cancel", "conv", 1, {})
    started = asyncio.Event()

    async def worker() -> None:
        started.set()
        await asyncio.Event().wait()

    task = asyncio.ensure_future(worker())
    await registry.attach_task("harness-run-cancel", task)
    await started.wait()
    snapshot = await registry.cancel("harness-run-cancel")
    try:
        await task
    except asyncio.CancelledError:
        pass
    else:
        raise AssertionError("cancel did not terminate live task")
    late = await registry.finish("harness-run-cancel", RunStatus.SUCCESS)
    assert late.status == RunStatus.CANCELLED
    return {"status": late.status.value, "taskCancelled": task.cancelled()}


async def mcp_failure_never_becomes_evidence() -> Dict[str, Any]:
    failure = audit_external_evidence(
        {"status": "FAILED", "error": "public MCP timeout", "results": []}
    )
    false_success_shape = audit_external_evidence(
        {"ok": False, "reason": "timeout", "url": "https://github.com/example/repo"}
    )
    ungrounded = audit_external_evidence({"results": [{"title": "invented profile"}]})
    nested_synthetic = audit_external_evidence(
        {
            "results": [
                {"synthetic": True, "url": "https://github.com/example/repo"}
            ]
        }
    )
    failed_branch_with_url = audit_external_evidence(
        {
            "results": [
                {"status": "RATE_LIMITED", "url": "https://github.com/example/repo"}
            ]
        }
    )
    grounded = audit_external_evidence(
        {"results": [{"title": "repository", "sourceUrl": "https://github.com/example/repo"}]}
    )
    assert not failure.usable and failure.reason == "tool_failed"
    assert not false_success_shape.usable and false_success_shape.reason == "tool_failed"
    assert not ungrounded.usable and ungrounded.reason == "missing_source_url"
    assert not nested_synthetic.usable and nested_synthetic.reason == "synthetic_evidence_forbidden"
    assert not failed_branch_with_url.usable and failed_branch_with_url.reason == "tool_failed"
    assert grounded.usable
    return {
        "failedReason": failure.reason,
        "ungroundedReason": ungrounded.reason,
        "nestedSyntheticReason": nested_synthetic.reason,
        "echoedUrlFailureReason": failed_branch_with_url.reason,
        "acceptedSources": grounded.source_urls,
    }


async def public_lookup_requires_declared_subject() -> Dict[str, Any]:
    metadata = {
        "externalEvidence": {
            "provider": "exa",
            "kind": "public-web",
            "subjectBinding": "unverified",
        }
    }
    missing = audit_external_subject_binding(metadata, {"query": "张三 GitHub"}, "Java backend resume")
    bound = audit_external_subject_binding(
        metadata,
        {"query": "example/repository contributions"},
        "Portfolio: https://github.com/example/repository",
    )
    same_host_wrong_subject = audit_external_subject_binding(
        metadata,
        {"query": "https://github.com/another/repository"},
        "Portfolio: https://github.com/example/repository",
    )
    assert missing == "candidate_identifier_not_declared"
    assert bound is None
    assert same_host_wrong_subject == "tool_input_not_bound_to_declared_identifier"
    return {
        "unbound": missing,
        "sameHostWrongSubject": same_host_wrong_subject,
        "bound": "accepted",
    }


async def tool_loop_enforces_budget_and_dedup() -> Dict[str, Any]:
    ledger = AgentToolLedger("JdMatchAgent")
    first = ledger.inspect("milvus_jd_search", {"query": "Java", "topK": 3}, retrieval_queries=1)
    duplicate = ledger.inspect("milvus_jd_search", {"topK": 3, "query": "Java"}, retrieval_queries=1)
    retrieval_overflow = ledger.inspect(
        "milvus_jd_search", {"query": "Python", "topK": 3}, retrieval_queries=1
    )
    second = ledger.inspect("jd_requirements_extract", {"jdMatchJson": "{}"})
    third = ledger.inspect("execute_skill", {"skillName": "normalize-job-description"})
    budget_overflow = ledger.inspect("jd_requirements_extract", {"jdMatchJson": "{\"next\":1}"})
    assert first.allowed
    assert (duplicate.allowed, duplicate.reason) == (False, "duplicate_tool_call")
    assert (retrieval_overflow.allowed, retrieval_overflow.reason) == (
        False,
        "retrieval_budget_exceeded",
    )
    assert second.allowed
    assert third.allowed
    assert (budget_overflow.allowed, budget_overflow.reason) == (False, "tool_budget_exceeded")
    return {
        "toolCalls": ledger.tool_call_count,
        "retrievalQueries": ledger.retrieval_query_count,
        "blocked": [duplicate.reason, retrieval_overflow.reason, budget_overflow.reason],
    }


async def tool_proposal_flood_is_bounded() -> Dict[str, Any]:
    normal = guard_tool_proposal_batch(3)
    boundary = guard_tool_proposal_batch(8)
    flood = guard_tool_proposal_batch(200)
    assert normal.allowed
    assert boundary.allowed
    assert not flood.allowed
    assert flood.reason == "tool_proposal_batch_exceeded"
    agents_source = (WORKFLOW_ROOT / "app" / "agents.py").read_text(encoding="utf-8")
    assert "guard_tool_proposal_batch(len(tool_calls))" in agents_source
    return {
        "normal": normal.proposed_count,
        "limit": boundary.limit,
        "flood": flood.proposed_count,
        "blockedReason": flood.reason,
    }


async def late_result_is_fenced() -> Dict[str, Any]:
    late = fence_workflow_result(
        active_conversation_id="conv-1",
        active_workflow_run_id="run-rev-2",
        active_revision=2,
        incoming_conversation_id="conv-1",
        incoming_workflow_run_id="run-rev-1",
        incoming_revision=1,
    )
    exact = fence_workflow_result(
        active_conversation_id="conv-1",
        active_workflow_run_id="run-rev-2",
        active_revision=2,
        incoming_conversation_id="conv-1",
        incoming_workflow_run_id="run-rev-2",
        incoming_revision=2,
    )
    assert not late.accepted and late.reason == "revision_mismatch"
    assert exact.accepted
    return {"late": late.reason, "current": exact.reason}


async def trace_identity_is_complete() -> Dict[str, Any]:
    event = {
        "eventId": "trace-1:tech_eval:1:tool:2:call-1",
        "traceId": "trace-1",
        "workflowRunId": "run-1",
        "conversationId": "conv-1",
        "revision": 3,
        "kind": "tool",
        "toolCalls": [
            {
                "toolCallId": "call-1",
                "name": "milvus_resume_search",
                "status": "SUCCESS",
                "inputHash": "deadbeef",
            }
        ],
    }
    violations = validate_trace_contract(
        [event], workflow_run_id="run-1", conversation_id="conv-1", revision=3
    )
    assert violations == []
    broken = dict(event)
    broken["revision"] = 2
    assert "event[0].revision mismatch" in validate_trace_contract(
        [broken], workflow_run_id="run-1", conversation_id="conv-1", revision=3
    )
    return {"events": 1, "violations": violations}


async def production_graph_has_no_heuristic_scoring() -> Dict[str, Any]:
    graph_source = (WORKFLOW_ROOT / "app" / "graph.py").read_text(encoding="utf-8")
    agents_source = (WORKFLOW_ROOT / "app" / "agents.py").read_text(encoding="utf-8")
    harness_source = (WORKFLOW_ROOT / "app" / "agent_harness.py").read_text(encoding="utf-8")
    tools_source = (WORKFLOW_ROOT / "app" / "tools.py").read_text(encoding="utf-8")
    forbidden = [
        "_deterministic_tech_result",
        "_deterministic_project_result",
        "_deterministic_risk_result",
        "_deterministic_report",
        "_fast_lane_resume",
        "_harness_eval_lane",
    ]
    present = [name for name in forbidden if name in graph_source]
    assert not present, f"production heuristic scoring paths present: {present}"
    assert '"confidence": 0.78' not in graph_source
    assert '"confidenceStatus": "NOT_CALIBRATED"' in graph_source
    assert "confidence_must_be_null_without_calibration" in agents_source
    assert "ALLOWED_EVIDENCE_SOURCES" in agents_source
    assert '"reportMode": "llm_detailed"' in harness_source
    assert "deterministic_sparse" not in harness_source
    assert "timeline validation delegated" not in tools_source
    assert '"merged": True' not in tools_source
    assert validate_timeline_text("skills only")["status"] == "NOT_CHECKED"
    return {
        "forbiddenPathsPresent": present,
        "reportMode": "llm_detailed",
        "undatedTimelineStatus": "NOT_CHECKED",
        "fusionConfidence": "NOT_CALIBRATED",
    }


async def revision_route_cannot_prune_invalidated_nodes() -> Dict[str, Any]:
    route = {"route": {"selectedAgents": ["risk_eval"]}}
    revision_plan = {
        "execute_nodes": ["tech_eval", "project_eval", "evidence_fusion", "report"]
    }
    initial = select_phase4_nodes(route, revision_plan, revision=1)
    revised = select_phase4_nodes(route, revision_plan, revision=2)
    assert initial == ["risk_eval"]
    assert revised == ["tech_eval", "project_eval", "risk_eval"]
    return {"initialRoute": initial, "revisionRoute": revised}


async def explicit_jd_is_primary_revision_input() -> Dict[str, Any]:
    graph_source = (WORKFLOW_ROOT / "app" / "graph.py").read_text(encoding="utf-8")
    assert 'state.get("jobDescription")' in graph_source
    assert "user_supplied_job_description" in graph_source
    plan = plan_revision_execution(["jd_match"], _base_checkpoint())
    assert "jd_match" in plan.execute_nodes
    assert "knowledge_context" in plan.execute_nodes
    assert "report" in plan.execute_nodes
    assert "resume_parse" in plan.reused_nodes
    return {
        "explicitJdSource": "user_supplied_job_description",
        "executeNodes": plan.execute_nodes,
        "reusedNodes": plan.reused_nodes,
    }


async def degraded_report_is_partial_success() -> Dict[str, Any]:
    healthy = terminal_status_for_degradation([])
    degraded = terminal_status_for_degradation(["report_eval_generation_failed"])
    assert healthy == "SUCCESS"
    assert degraded == "PARTIAL_SUCCESS"
    registry = RunRegistry()
    await registry.register("run-partial", "trace-partial", "conv-partial", 1, {})
    snapshot = await registry.finish("run-partial", RunStatus.PARTIAL_SUCCESS)
    assert snapshot.status == RunStatus.PARTIAL_SUCCESS
    return {
        "healthyStatus": healthy,
        "degradedStatus": degraded,
        "registryStatus": snapshot.status.value,
    }


SCENARIOS: List[tuple] = [
    ("side_quest_keeps_run_alive", side_quest_keeps_run_alive),
    ("goal_change_is_minimal_revision", goal_change_is_minimal_revision),
    ("pause_checkpoint_resume_same_run", pause_checkpoint_resume_same_run),
    ("cancel_is_immediate_and_terminal", cancel_is_immediate_and_terminal),
    ("mcp_failure_never_becomes_evidence", mcp_failure_never_becomes_evidence),
    ("public_lookup_requires_declared_subject", public_lookup_requires_declared_subject),
    ("tool_loop_enforces_budget_and_dedup", tool_loop_enforces_budget_and_dedup),
    ("tool_proposal_flood_is_bounded", tool_proposal_flood_is_bounded),
    ("late_result_is_fenced", late_result_is_fenced),
    ("trace_identity_is_complete", trace_identity_is_complete),
    ("production_graph_has_no_heuristic_scoring", production_graph_has_no_heuristic_scoring),
    ("revision_route_cannot_prune_invalidated_nodes", revision_route_cannot_prune_invalidated_nodes),
    ("explicit_jd_is_primary_revision_input", explicit_jd_is_primary_revision_input),
    ("degraded_report_is_partial_success", degraded_report_is_partial_success),
]


async def run_gate() -> Dict[str, Any]:
    results: List[Dict[str, Any]] = []
    failed = 0
    try:
        manifest_items = json.loads(HARNESS_MANIFEST.read_text(encoding="utf-8"))
        manifest = {str(item["id"]): item for item in manifest_items}
    except Exception as exc:
        return {
            "harness": "resumai-agent-runtime-gate-v1",
            "status": "FAIL",
            "passed": 0,
            "failed": 1,
            "scenarios": [{"name": "manifest", "status": "FAIL", "error": str(exc)}],
        }
    code_scenarios = {name for name, _ in SCENARIOS}
    manifest_scenarios = set(manifest)
    if code_scenarios != manifest_scenarios:
        failed += 1
        results.append(
            {
                "name": "manifest_coverage",
                "status": "FAIL",
                "error": {
                    "missingInManifest": sorted(code_scenarios - manifest_scenarios),
                    "missingInCode": sorted(manifest_scenarios - code_scenarios),
                },
            }
        )
    for name, scenario in SCENARIOS:
        metadata = manifest.get(name, {})
        try:
            evidence = await scenario()
            results.append(
                {
                    "name": name,
                    "severity": metadata.get("severity"),
                    "invariant": metadata.get("invariant"),
                    "status": "PASS",
                    "evidence": evidence,
                }
            )
        except Exception as exc:
            failed += 1
            results.append(
                {
                    "name": name,
                    "severity": metadata.get("severity"),
                    "invariant": metadata.get("invariant"),
                    "status": "FAIL",
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
    return {
        "harness": "resumai-agent-runtime-gate-v1",
        "status": "PASS" if failed == 0 else "FAIL",
        "passed": sum(1 for item in results if item["status"] == "PASS"),
        "failed": failed,
        "scenarios": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run deterministic ResumAI Agent runtime gates")
    parser.add_argument("--output", help="Optional JSON report path")
    args = parser.parse_args()
    report = asyncio.run(run_gate())
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    print(rendered)
    if args.output:
        output = Path(args.output).resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered + "\n", encoding="utf-8")
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
