"""Memory consumer / scope / source isolation for evaluation agents."""
from __future__ import annotations

import asyncio

from app.runtime.memory import (
    NullMemoryClient,
    allowed_types_for,
    canonical_taxonomy,
    decisions_from_hits,
    filter_hits_for_consumer,
    is_benchmark_source,
    is_control_plane_memory,
    memory_trace_entries,
)


def test_benchmark_source_detection():
    assert is_benchmark_source("exp5_benchmark")
    assert is_benchmark_source("exp_benchmark")
    assert not is_benchmark_source("system_rule")


def test_control_plane_memory_detection():
    assert is_control_plane_memory({
        "type": "FAILURE", "source": "control_plane",
        "content": "restart",
    })
    assert is_control_plane_memory({
        "type": "FAILURE", "source": "system_rule",
        "structuredContent": {"errorCode": "ORPHANED_ON_RESTART"},
        "content": "x",
    })
    assert is_control_plane_memory({
        "type": "FAILURE", "content": "错误=RUNTIME_START_FAILED | 详情=boom",
    })
    assert not is_control_plane_memory({
        "type": "FAILURE", "source": "system_rule",
        "structuredContent": {"errorCode": "BUDGET_EXCEEDED"},
        "content": "budget",
    })


def test_report_and_risk_never_see_control_plane_or_failure():
    hits = [
        {"memoryId": "m1", "type": "PREFERENCE", "ownerScope": "USER",
         "source": "user_explicit", "content": "偏好学历次要"},
        {"memoryId": "m2", "type": "FAILURE", "ownerScope": "GLOBAL",
         "source": "control_plane",
         "structuredContent": {"errorCode": "ORPHANED_ON_RESTART"},
         "content": "失败记录: 错误=ORPHANED_ON_RESTART"},
        {"memoryId": "m3", "type": "FAILURE", "ownerScope": "GLOBAL",
         "source": "system_rule",
         "structuredContent": {"errorCode": "BUDGET_EXCEEDED"},
         "content": "预算耗尽"},
        {"memoryId": "m4", "type": "EPISODIC", "ownerScope": "CONVERSATION",
         "source": "system_rule", "content": "上次评估成功"},
        {"memoryId": "m5", "type": "PREFERENCE", "ownerScope": "GLOBAL",
         "source": "exp5_benchmark", "content": "benchmark 污染"},
    ]
    for agent in ("ReportAgent", "RiskAgent", "TechAgent"):
        used, ignored = filter_hits_for_consumer(hits, agent)
        used_ids = {h["memoryId"] for h in used}
        assert used_ids == {"m1", "m4"}
        ignored_ids = {h["memoryId"] for h in ignored}
        assert {"m2", "m3", "m5"} <= ignored_ids
        reasons = {h["memoryId"]: h["ignoredReason"] for h in ignored}
        assert "type_not_allowed" in reasons["m2"] or "control_plane" in reasons["m2"]
        assert "type_not_allowed" in reasons["m3"] or "failure" in reasons["m3"]
        assert "benchmark" in reasons["m5"] or "scope" in reasons["m5"]


def test_coordinator_may_see_failure_including_control_plane():
    hits = [
        {"memoryId": "m1", "type": "FAILURE", "ownerScope": "GLOBAL",
         "source": "control_plane",
         "structuredContent": {"errorCode": "RUNTIME_START_FAILED"},
         "content": "RUNTIME_START_FAILED"},
        {"memoryId": "m2", "type": "PREFERENCE", "ownerScope": "USER",
         "source": "user_explicit", "content": "偏好"},
    ]
    used, ignored = filter_hits_for_consumer(hits, "CoordinatorAgent")
    used_ids = {h["memoryId"] for h in used}
    assert "m1" in used_ids
    assert "m2" in used_ids
    assert ignored == []


def test_memory_trace_records_consumer_and_reason():
    used = [{"memoryId": "a", "type": "PREFERENCE", "ownerScope": "USER",
             "source": "user_explicit", "used": True, "ignoredReason": None}]
    ignored = [{"memoryId": "b", "type": "FAILURE", "ownerScope": "GLOBAL",
                "source": "control_plane", "used": False,
                "ignoredReason": "control_plane_not_injectable"}]
    rows = memory_trace_entries(used, ignored, "ReportAgent")
    assert rows[0]["consumerAgent"] == "ReportAgent"
    assert rows[0]["used"] is True
    assert rows[1]["ignoredReason"] == "control_plane_not_injectable"


def test_null_client_preference_never_writes_global():
    client = NullMemoryClient()

    async def _run():
        mid = await client.write(
            type_="PREFERENCE", owner_scope="GLOBAL", content="偏好 X")
        assert mid
        assert client.writes[0]["ownerScope"] == "USER"

    asyncio.run(_run())


def test_null_client_search_excludes_failure_for_specialists():
    client = NullMemoryClient(canned=[
        {"memoryId": "1", "type": "PREFERENCE", "ownerScope": "USER",
         "source": "user_explicit", "content": "ok"},
        {"memoryId": "2", "type": "FAILURE", "ownerScope": "GLOBAL",
         "source": "control_plane",
         "structuredContent": {"errorCode": "ORPHANED_ON_RESTART"},
         "content": "ORPHANED_ON_RESTART"},
        {"memoryId": "3", "type": "EPISODIC", "ownerScope": "CONVERSATION",
         "source": "exp5_benchmark", "content": "bench"},
    ])

    async def _run():
        hits = await client.search("x", consumer_agent="ReportAgent")
        ids = {h["memoryId"] for h in hits}
        assert ids == {"1"}
        coord = await client.search("x", types=["FAILURE"],
                                    consumer_agent="CoordinatorAgent")
        assert any(h["memoryId"] == "2" for h in coord)

    asyncio.run(_run())


def test_null_client_business_memory_filters_by_job_and_jd():
    client = NullMemoryClient(canned=[
        {"memoryId": "same-job", "type": "JOB_PROFILE",
         "structuredContent": {"jobCategory": "JAVA_BACKEND",
                                "jdFingerprint": "jd-1"},
         "content": "same"},
        {"memoryId": "other-job", "type": "JOB_PROFILE",
         "structuredContent": {"jobCategory": "DATA",
                                "jdFingerprint": "jd-1"},
         "content": "other"},
        {"memoryId": "other-jd", "type": "JOB_PROFILE",
         "structuredContent": {"jobCategory": "JAVA_BACKEND",
                                "jdFingerprint": "jd-2"},
         "content": "other jd"},
    ])

    async def _run():
        hits = await client.search(
            "Java", types=["JOB_PROFILE"],
            job_category="java_backend", jd_fingerprint="jd-1")
        assert [h["memoryId"] for h in hits] == ["same-job"]

    asyncio.run(_run())


def test_canonical_taxonomy_and_agent_routes_are_diverse():
    assert canonical_taxonomy("PREFERENCE") == "SEMANTIC"
    assert canonical_taxonomy("CONVERSATION") == "WORKING"
    assert canonical_taxonomy("FAILURE") == "EPISODIC"

    tech = allowed_types_for("TechAgent")
    report = allowed_types_for("ReportAgent")
    assert tech == frozenset({"SEMANTIC", "EPISODIC", "PROCEDURAL"})
    assert report == tech


def test_usage_decision_has_taxonomy_namespace_reason_and_real_time():
    used = [{
        "memoryId": "semantic-1",
        "type": "SEMANTIC",
        "ownerScope": "CONVERSATION",
        "namespace": "conversation/abc123",
        "selectionReason": "query_intent:SEMANTIC",
        "score": 0.82,
        "occurredAt": "2020-01-01T00:00:00Z",
    }]
    rows = decisions_from_hits(
        used, [], "TechAgent", round_id="run:tech:round:1")
    assert len(rows) == 1
    assert rows[0].taxonomy == "SEMANTIC"
    assert rows[0].memoryType == "SEMANTIC"
    assert rows[0].namespace == "conversation/abc123"
    assert rows[0].reason == "query_intent:SEMANTIC"
    assert rows[0].occurredAt and rows[0].occurredAt.endswith("Z")
    assert rows[0].roundId == "run:tech:round:1"
    assert rows[0].occurredAt != used[0]["occurredAt"]


def test_working_memory_is_not_injected_into_report_agent():
    hits = [
        {"memoryId": "work-1", "type": "WORKING", "ownerScope": "RUN",
         "source": "run_input", "content": "current scratch"},
        {"memoryId": "fact-1", "type": "SEMANTIC", "ownerScope": "CONVERSATION",
         "source": "candidate_fact", "content": "Java 5 years"},
        {"memoryId": "episode-1", "type": "EPISODIC",
         "ownerScope": "CONVERSATION", "source": "evaluation_result",
         "content": "prior successful evaluation"},
    ]
    tech_used, tech_ignored = filter_hits_for_consumer(hits, "TechAgent")
    report_used, report_ignored = filter_hits_for_consumer(hits, "ReportAgent")
    assert {h["memoryId"] for h in tech_used} == {"fact-1", "episode-1"}
    assert any(h["memoryId"] == "work-1" for h in tech_ignored)
    assert {h["memoryId"] for h in report_used} == {"fact-1", "episode-1"}
    assert any(h["memoryId"] == "work-1" for h in report_ignored)
