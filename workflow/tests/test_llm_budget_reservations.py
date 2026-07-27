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
