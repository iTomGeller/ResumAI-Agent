from __future__ import annotations

import asyncio
import sys
from pathlib import Path

WORKFLOW_ROOT = Path(__file__).resolve().parents[1]
if str(WORKFLOW_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKFLOW_ROOT))

from app.agent_harness import (
    AgentToolLedger,
    audit_external_evidence,
    audit_external_subject_binding,
    fence_workflow_result,
    materialize_revision_state,
    plan_revision_execution,
    select_phase4_nodes,
    terminal_status_for_degradation,
    validate_trace_contract,
    validate_timeline_text,
    build_harness_plan,
)
from run_agent_harness import run_gate


def complete_base_state():
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


def test_first_run_executes_every_node() -> None:
    plan = plan_revision_execution([], None)

    assert plan.reused_nodes == []
    assert plan.execute_nodes == [
        "intent",
        "resume_parse",
        "jd_match",
        "knowledge_context",
        "tech_eval",
        "project_eval",
        "risk_eval",
        "evidence_fusion",
        "report",
    ]


def test_llm_call_plan_is_explicitly_a_bound_not_fake_observed_usage() -> None:
    sparse = build_harness_plan("{}", "short resume")
    deep = build_harness_plan(
        '{"requiredSkills":["Java","Kafka","K8s"],"routingHints":["系统设计","性能","稳定性"]}',
        ("Java Spring Kafka K8s 项目 平台 系统 架构 重构 QPS 2025 " * 100),
    )

    assert sparse["route"]["estimatedLlmCallsLowerBound"] == 6
    assert sparse["route"]["fullPipelineLlmCalls"] == 8
    assert "not_observed" in sparse["route"]["llmCallEstimateBasis"]
    assert deep["route"]["estimatedLlmCallsUpperBound"] >= deep["route"]["estimatedLlmCallsLowerBound"]
    assert deep["runtimeBudgets"]["JdMatchAgent"]["scope"] == "adaptive_agent_loop_only"
    assert deep["runtimeBudgets"]["JdMatchAgent"]["preexecutedTools"] == [
        "milvus_jd_search",
        "jd_requirements_extract",
    ]
    assert len(deep["queryPlans"]["tech_eval"]) <= deep["runtimeBudgets"]["TechEvalAgent"]["maxRetrievalQueries"]


def test_timeline_validator_never_returns_fixed_success_for_missing_dates() -> None:
    from datetime import date

    missing = validate_timeline_text("只有技能列表，没有任何日期", reference_date=date(2026, 7, 16))
    checked = validate_timeline_text(
        "甲公司 2023-03 至 2024-06\n乙公司 2024-05 至今",
        reference_date=date(2026, 7, 16),
    )
    invalid = validate_timeline_text(
        "未来经历 2027-01 至 2026-01",
        reference_date=date(2026, 7, 16),
    )

    assert (missing["status"], missing["checked"]) == ("NOT_CHECKED", False)
    assert checked["status"] == "CHECKED"
    assert checked["overlaps"][0]["overlapMonths"] == 2
    assert {item["kind"] for item in invalid["issues"]} == {
        "invalid_range",
        "future_start",
    }


def test_goal_change_reuses_parse_and_invalidates_downstream() -> None:
    plan = plan_revision_execution(["intent", "jd_match"], complete_base_state())

    assert plan.reused_nodes == ["resume_parse"]
    assert plan.execute_nodes[0] == "intent"
    assert plan.execute_nodes[-1] == "report"


def test_revision_route_cannot_prune_invalidated_specialists() -> None:
    harness_plan = {"route": {"selectedAgents": ["risk_eval"]}}
    revision_plan = {"execute_nodes": ["tech_eval", "project_eval", "evidence_fusion", "report"]}

    assert select_phase4_nodes(harness_plan, revision_plan, revision=1) == ["risk_eval"]
    assert select_phase4_nodes(harness_plan, revision_plan, revision=2) == [
        "tech_eval",
        "project_eval",
        "risk_eval",
    ]


def test_degraded_report_cannot_be_terminal_success() -> None:
    assert terminal_status_for_degradation([]) == "SUCCESS"
    assert terminal_status_for_degradation(["report_eval_generation_failed"]) == "PARTIAL_SUCCESS"


def test_materialized_revision_copies_only_reused_node_outputs() -> None:
    base = complete_base_state()
    current = {
        "traceId": "trace-rev-2",
        "workflowRunId": "run-rev-2",
        "revision": 2,
    }

    state, plan = materialize_revision_state(
        current,
        [
            "intent",
            "jd_match",
            "knowledge_context",
            "tech_eval",
            "project_eval",
            "risk_eval",
            "evidence_fusion",
            "report",
        ],
        base,
    )

    assert plan.reused_nodes == ["resume_parse"]
    assert state["parseResult"] == base["parseResult"]
    assert "jdResult" not in state
    assert "finalReport" not in state
    assert state["revision"] == 2


def test_missing_cached_parse_forces_parse_and_dependants() -> None:
    base = complete_base_state()
    del base["parseResult"]

    plan = plan_revision_execution(["tech_eval"], base)

    assert "resume_parse" in plan.cache_miss_nodes
    assert "resume_parse" in plan.execute_nodes
    assert "jd_match" in plan.execute_nodes
    assert "report" in plan.execute_nodes
    assert "intent" in plan.reused_nodes


def test_unknown_invalidation_is_visible_and_fails_closed_to_full_run() -> None:
    plan = plan_revision_execution(["made_up_node"], complete_base_state())

    assert plan.unknown_nodes == ["made_up_node"]
    assert len(plan.execute_nodes) == 9
    assert plan.reused_nodes == []


def test_tool_ledger_dedup_is_order_insensitive_and_does_not_spend_budget() -> None:
    ledger = AgentToolLedger("TechEvalAgent")
    allowed = ledger.inspect("search", {"query": "java", "topK": 3}, retrieval_queries=1)
    duplicate = ledger.inspect("search", {"topK": 3, "query": "java"}, retrieval_queries=1)

    assert allowed.allowed
    assert not duplicate.allowed
    assert duplicate.reason == "duplicate_tool_call"
    assert ledger.tool_call_count == 1
    assert ledger.retrieval_query_count == 1


def test_tool_ledger_enforces_retrieval_budget_separately() -> None:
    ledger = AgentToolLedger("JdMatchAgent")
    assert ledger.inspect("search", {"query": "java"}, retrieval_queries=1).allowed

    blocked = ledger.inspect("search", {"query": "python"}, retrieval_queries=1)
    local = ledger.inspect("extract", {"input": "{}"})

    assert (blocked.allowed, blocked.reason) == (False, "retrieval_budget_exceeded")
    assert local.allowed


def test_external_tool_error_or_missing_source_is_not_evidence() -> None:
    failed = audit_external_evidence({"error": "timeout"})
    false_ok_with_url = audit_external_evidence(
        {"ok": False, "reason": "timeout", "url": "https://example.test/profile"}
    )
    skipped_with_url = audit_external_evidence(
        {"skipped": True, "url": "https://example.test/profile"}
    )
    empty = audit_external_evidence({}, require_source_url=False)
    fallback = audit_external_evidence(
        {"fallbackUsed": True, "results": [{"url": "https://example.test/fallback"}]}
    )
    synthetic_fallback = audit_external_evidence(
        {"syntheticFallback": True, "sourceUrl": "https://example.test/fallback"}
    )
    ungrounded = audit_external_evidence({"results": [{"title": "claim"}]})
    grounded = audit_external_evidence(
        {"results": [{"title": "claim", "url": "https://example.test/source"}]}
    )
    nested_synthetic = audit_external_evidence(
        {
            "results": [
                {"url": "https://example.test/source", "synthetic": True}
            ]
        }
    )
    failed_branch_with_echoed_url = audit_external_evidence(
        {
            "results": [
                {"status": "RATE_LIMITED", "url": "https://example.test/requested-profile"}
            ]
        }
    )
    mixed_results = audit_external_evidence(
        {
            "results": [
                {"error": "timeout", "url": "https://example.test/unavailable"},
                {"title": "grounded", "url": "https://example.test/real-source"},
            ]
        }
    )

    assert (failed.usable, failed.reason) == (False, "tool_failed")
    assert (false_ok_with_url.usable, false_ok_with_url.reason) == (False, "tool_failed")
    assert (skipped_with_url.usable, skipped_with_url.reason) == (False, "tool_failed")
    assert (empty.usable, empty.reason) == (False, "empty_external_result")
    assert (fallback.usable, fallback.reason) == (False, "fallback_not_external_evidence")
    assert (synthetic_fallback.usable, synthetic_fallback.reason) == (
        False,
        "synthetic_evidence_forbidden",
    )
    assert (ungrounded.usable, ungrounded.reason) == (False, "missing_source_url")
    assert grounded.usable
    assert (nested_synthetic.usable, nested_synthetic.reason) == (
        False,
        "synthetic_evidence_forbidden",
    )
    assert (failed_branch_with_echoed_url.usable, failed_branch_with_echoed_url.reason) == (
        False,
        "tool_failed",
    )
    assert mixed_results.usable
    assert mixed_results.source_urls == ["https://example.test/real-source"]


def test_public_lookup_must_bind_to_resume_declared_identifier() -> None:
    metadata = {
        "externalEvidence": {
            "provider": "exa",
            "kind": "public-web",
            "subjectBinding": "unverified",
        }
    }

    assert audit_external_subject_binding(
        metadata, {"query": "张三 GitHub"}, "Java backend resume"
    ) == "candidate_identifier_not_declared"
    assert audit_external_subject_binding(
        metadata,
        {"query": "example/repository"},
        "Portfolio https://github.com/example/repository",
    ) is None
    assert audit_external_subject_binding(
        metadata,
        {"query": "https://github.com/another/repository"},
        "Portfolio https://github.com/example/repository",
    ) == "tool_input_not_bound_to_declared_identifier"


def test_late_result_fence_requires_full_identity_match() -> None:
    decision = fence_workflow_result(
        active_conversation_id="conv",
        active_workflow_run_id="run-2",
        active_revision=2,
        incoming_conversation_id="conv",
        incoming_workflow_run_id="run-1",
        incoming_revision=1,
    )

    assert not decision.accepted
    assert decision.reason == "revision_mismatch"


def test_cancelled_run_result_is_never_writable() -> None:
    decision = fence_workflow_result(
        active_conversation_id="conv",
        active_workflow_run_id="run",
        active_revision=1,
        incoming_conversation_id="conv",
        incoming_workflow_run_id="run",
        incoming_revision=1,
        active_status="CANCELLED",
    )

    assert (decision.accepted, decision.reason) == (False, "active_run_not_writable")


def test_trace_contract_rejects_lost_revision_and_missing_tool_hash() -> None:
    violations = validate_trace_contract(
        [
            {
                "eventId": "event-1",
                "workflowRunId": "run",
                "conversationId": "conv",
                "revision": 1,
                "kind": "tool",
                "parentEventId": "missing-generation-event",
                "toolCalls": [{"toolCallId": "call", "name": "search", "status": "SUCCESS"}],
            }
        ],
        workflow_run_id="run",
        conversation_id="conv",
        revision=2,
    )

    assert "event[0].revision mismatch" in violations
    assert "event[0].toolCalls[0].inputHash missing" in violations
    assert "event[0].parentEventId missing target" in violations


def test_offline_agent_harness_gate_passes_all_scenarios() -> None:
    report = asyncio.run(run_gate())

    assert report["status"] == "PASS"
    assert report["failed"] == 0
    assert report["passed"] >= 13
