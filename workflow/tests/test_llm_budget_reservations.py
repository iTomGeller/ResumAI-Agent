from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

WORKFLOW_ROOT = Path(__file__).resolve().parents[1]
if str(WORKFLOW_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKFLOW_ROOT))

from app.runtime.agents import default_agent_registry
from app.runtime.coordinator import Coordinator
from app.runtime.events import NullEmitter
from app.runtime.llm import (
    CircuitBreaker,
    LlmError,
    LlmTurn,
    ResilientLlmClient,
)
from app.runtime.models import BudgetExceeded, PolicyBundle, RunBudget


def test_reservations_are_inside_one_global_hard_cap():
    budget = RunBudget()
    budget.configure_llm_budget(
        12,
        {"terminal": 2, "control": 4},
        scope_limits={"control": 4})

    for _ in range(4):
        budget.claim_llm_call(12, "control")
    with pytest.raises(BudgetExceeded):
        budget.claim_llm_call(12, "control")

    # Specialists can consume only the six unreserved calls.
    for _ in range(6):
        budget.claim_llm_call(12, "agent:TechAgent")
    with pytest.raises(BudgetExceeded):
        budget.claim_llm_call(12, "agent:ProjectAgent")

    # The terminal reserve remains intact after all specialist/control work.
    budget.claim_llm_call(12, "terminal")
    budget.claim_llm_call(12, "terminal")
    assert budget.llm_calls == 12
    assert budget.llm_calls_by_scope == {
        "control": 4,
        "agent:TechAgent": 6,
        "terminal": 2,
    }


def test_scope_availability_does_not_misreport_terminal_reserve_as_repair():
    budget = RunBudget()
    budget.configure_llm_budget(
        3, {"terminal": 2, "control": 0},
        scope_limits={"control": 0})
    budget.claim_llm_call(3, "agent:TechAgent")

    # Aggregate planning still sees the terminal pool, but a specialist
    # repair cannot borrow either protected terminal call.
    assert budget.available_agent_llm_calls(3) == 2
    assert budget.available_llm_calls_for_scope(
        3, "agent:TechAgent") == 0
    assert budget.available_llm_calls_for_scope(3, "terminal") == 2


def test_releasing_unused_control_reserve_unblocks_late_evidence_agent():
    """Regression for the 100-resume preflight EvidenceAgent failure.

    Full evaluation planning is deterministic, so its unused control reserve
    must not strand Evidence after earlier specialist/provider retries.
    """
    budget = RunBudget()
    budget.configure_llm_budget(
        17, {"terminal": 3, "control": 4},
        scope_limits={"control": 4})
    for _ in range(5):
        budget.claim_llm_call(17, "agent:TechAgent")
    for _ in range(4):
        budget.claim_llm_call(17, "agent:ProjectAgent")
    budget.claim_llm_call(17, "agent:RiskAgent")

    with pytest.raises(BudgetExceeded) as blocked:
        budget.claim_llm_call(17, "agent:EvidenceAgent")
    assert blocked.value.kind == "llmReservation"

    budget.release_llm_reservation("control")
    budget.claim_llm_call(17, "agent:EvidenceAgent")
    for _ in range(3):
        budget.claim_llm_call(17, "terminal")

    assert budget.llm_calls == 14
    assert budget.llm_reservations == {"terminal": 3, "control": 0}


def test_coordinator_allocates_only_agent_assignable_remaining_calls():
    budget = RunBudget()
    budget.configure_llm_budget(
        12,
        {"terminal": 2, "control": 4},
        scope_limits={"control": 4})
    budget.claim_llm_call(12, "control")  # initial provider planning request

    llm = type("BudgetedLlm", (), {"budget": budget})()
    coordinator = Coordinator(
        default_agent_registry, PolicyBundle.from_config("balanced", {}), llm)
    ordered = [
        "ResumeParserAgent", "JDAnalysisAgent", "TechAgent",
        "ProjectAgent", "RiskAgent", "EvidenceAgent", "ReportAgent",
    ]
    plan = coordinator._budget_plan(ordered, "ReportAgent")
    assert sum(item["llmQuota"] for item in plan.values()) <= 8
    assert plan["ReportAgent"]["llmQuota"] >= 2


def test_each_retry_is_an_accounted_provider_call(monkeypatch):
    budget = RunBudget()
    budget.configure_llm_budget(
        5, {"terminal": 1, "control": 1},
        scope_limits={"control": 1})
    client = ResilientLlmClient(
        NullEmitter(), budget, max_llm_calls=5, llm_timeout_seconds=5,
        breaker=CircuitBreaker(threshold=5))
    client.api_key = "test-only"
    attempts = 0

    async def fake_invoke(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise LlmError("TRANSIENT", "retry me", True)
        return (
            LlmTurn(content="ok", finish_reason="stop"),
            {"prompt_tokens": 1, "completion_tokens": 1},
            "stop",
        )

    async def no_sleep(_seconds):
        return None

    client._invoke = fake_invoke
    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    answer = asyncio.run(client.chat(
        [{"role": "user", "content": "hello"}],
        agent_id="TechAgent", purpose="technical_findings",
        json_mode=False))
    assert answer == "ok"
    assert attempts == 2
    assert budget.llm_calls == 2
    assert budget.llm_calls_by_scope["agent:TechAgent"] == 2


def test_provider_gate_bounds_concurrent_requests(monkeypatch):
    monkeypatch.setenv("LLM_MAX_CONCURRENT", "2")
    budget = RunBudget()
    client = ResilientLlmClient(
        NullEmitter(), budget, max_llm_calls=8, llm_timeout_seconds=5,
        breaker=CircuitBreaker(threshold=20))
    client.api_key = "test-only"
    active = 0
    max_active = 0

    async def fake_invoke(*args, **kwargs):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        try:
            await asyncio.sleep(0.02)
            return (
                LlmTurn(content="ok", finish_reason="stop"),
                {"prompt_tokens": 1, "completion_tokens": 1},
                "stop",
            )
        finally:
            active -= 1

    client._invoke = fake_invoke

    async def exercise():
        return await asyncio.gather(*[
            client.chat(
                [{"role": "user", "content": f"request {idx}"}],
                agent_id="TechAgent", purpose="technical_findings",
                json_mode=False)
            for idx in range(4)
        ])

    assert asyncio.run(exercise()) == ["ok"] * 4
    assert max_active == 2
    assert budget.llm_calls == 4


def test_budget_snapshot_preserves_scope_accounting():
    budget = RunBudget()
    budget.configure_llm_budget(
        9, {"terminal": 2, "control": 3},
        scope_limits={"control": 3})
    budget.claim_llm_call(9, "control")
    budget.claim_llm_call(9, "agent:RiskAgent")

    restored = RunBudget()
    restored.restore(budget.snapshot())
    assert restored.llm_limit == 9
    assert restored.llm_calls_by_scope == {
        "control": 1, "agent:RiskAgent": 1}
    assert restored.llm_reservations == {
        "terminal": 2, "control": 3}
    assert restored.llm_scope_limits == {"control": 3}


def test_legacy_balanced_db_budget_is_bounded_and_keeps_tools_actionable():
    legacy_policy = PolicyBundle.from_config("balanced", {
        "maxLlmCalls": 12,
        "terminalLlmReserve": 1,
        "maxIterationsPerAgent": 2,
        "toolBudget": {
            "maxToolCallsPerRun": 20,
            "maxToolCallsPerAgent": 5,
        },
    })
    assert legacy_policy.maxLlmCalls == 17
    assert legacy_policy.terminalLlmReserve == 3
    assert PolicyBundle.from_config(
        "balanced", {"maxLlmCalls": 999}).maxLlmCalls == 18

    budget = RunBudget()
    budget.configure_llm_budget(
        legacy_policy.maxLlmCalls,
        {
            "terminal": legacy_policy.terminalLlmReserve,
            "control": legacy_policy.controlPlaneLlmReserve,
        },
        scope_limits={"control": legacy_policy.controlPlaneLlmReserve})
    budget.claim_llm_call(legacy_policy.maxLlmCalls, "control")
    coordinator = Coordinator(
        default_agent_registry,
        legacy_policy,
        type("BudgetedLlm", (), {"budget": budget})())
    ordered = [
        "ResumeParserAgent", "JDAnalysisAgent", "TechAgent",
        "ProjectAgent", "RiskAgent", "EvidenceAgent", "ReportAgent",
    ]

    plan = coordinator._budget_plan(
        ordered,
        "ReportAgent",
        signals={"has_projects": True, "has_jd": True})

    assignable = budget.available_agent_llm_calls(
        legacy_policy.maxLlmCalls)
    assert sum(row["llmQuota"] for row in plan.values()) <= assignable
    assert plan["ReportAgent"]["llmQuota"] >= 3
    assert plan["TechAgent"]["actionTurnQuota"] >= 1
    assert plan["ProjectAgent"]["actionTurnQuota"] >= 1
    assert plan["EvidenceAgent"]["actionTurnQuota"] >= 1


def test_external_url_budget_keeps_skill_mcp_and_final_turns_inside_cap():
    policy = PolicyBundle.from_config("balanced", {
        "maxLlmCalls": 12,
        "terminalLlmReserve": 1,
        "maxIterationsPerAgent": 2,
        "toolBudget": {
            "maxToolCallsPerRun": 20,
            "maxToolCallsPerAgent": 6,
        },
    })
    budget = RunBudget()
    budget.configure_llm_budget(
        policy.maxLlmCalls,
        {
            "terminal": policy.terminalLlmReserve,
            "control": policy.controlPlaneLlmReserve,
        },
        scope_limits={"control": policy.controlPlaneLlmReserve})
    budget.claim_llm_call(policy.maxLlmCalls, "control")
    coordinator = Coordinator(
        default_agent_registry, policy,
        type("BudgetedLlm", (), {"budget": budget})())
    ordered = [
        "ResumeParserAgent", "JDAnalysisAgent", "TechAgent",
        "ProjectAgent", "RiskAgent", "EvidenceAgent", "ReportAgent",
    ]

    plan = coordinator._budget_plan(
        ordered, "ReportAgent",
        signals={
            "has_projects": True,
            "has_external_urls": True,
            "has_jd": True,
        })

    assert sum(row["llmQuota"] for row in plan.values()) <= (
        budget.available_agent_llm_calls(policy.maxLlmCalls))
    assert plan["ProjectAgent"]["llmQuota"] >= 4
    assert plan["ProjectAgent"]["actionTurnQuota"] >= 3
    assert plan["ProjectAgent"]["toolQuota"] == 6
    assert plan["EvidenceAgent"]["actionTurnQuota"] >= 1
