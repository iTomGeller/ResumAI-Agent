from __future__ import annotations

import sys
from pathlib import Path

WORKFLOW_ROOT = Path(__file__).resolve().parents[1]
if str(WORKFLOW_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKFLOW_ROOT))

from app.runtime.agents import default_agent_registry
from app.runtime.coordinator import Coordinator, FULL_EVAL_TYPES
from app.runtime.models import PolicyBundle


def _coordinator(config: dict | None = None, policy_id: str = "balanced") -> Coordinator:
    return Coordinator(
        default_agent_registry,
        PolicyBundle.from_config(policy_id, config or {}),
        None,
    )


def test_policy_bundle_supports_run_types():
    low = PolicyBundle.from_config("low_cost", {
        "supportedRunTypes": ["quick_answer", "followup", "tech_match"],
        "requiredArtifacts": ["final_report"],
        "optionalArtifacts": ["technical_findings"],
        "agentOrder": ["TechAgent", "ReportAgent"],
        "evidenceVerification": {"enabled": False},
    })
    assert low.supports_run_type("quick_answer")
    assert not low.supports_run_type("full_evaluation")
    assert low.requiredArtifacts == ["final_report"]
    assert "technical_findings" in low.optionalArtifacts

    open_policy = PolicyBundle.from_config("balanced", {})
    assert open_policy.supports_run_type("full_evaluation")  # empty = all


def test_agent_order_is_not_allowlist_for_required_producers():
    """low_cost-style agentOrder must not strip Project/Evidence when required."""
    policy = PolicyBundle.from_config("balanced", {
        "agentOrder": ["TechAgent", "ReportAgent"],
        "maxAgentCount": 8,
        "evidenceVerification": {"enabled": True},
    })
    coordinator = Coordinator(default_agent_registry, policy, None)
    planned = coordinator.plan_from_artifacts(
        run_type="full_evaluation", needs_parse=True,
        resume_text="项目经历\nResumAI Agent\n工作经历\n2022-2024 后端\n",
        job_description="Java Spring")
    assert "ProjectAgent" in planned["plan"], planned
    assert "EvidenceAgent" in planned["plan"], planned
    assert "ReportAgent" in planned["plan"]
    # agentOrder still influences relative preference (Tech before Project when both ready)
    assert "不在策略 agentOrder 内" not in (planned.get("skippedBecause") or {}).values()


def test_full_eval_with_projects_forces_project_agent():
    coordinator = _coordinator({
        "evidenceVerification": {"enabled": True},
        "maxAgentCount": 8,
    })
    planned = coordinator.plan_from_artifacts(
        run_type="full_evaluation", needs_parse=True,
        resume_text="项目经历\n个人项目 Foo\n工作经历\n2021-2023 工程师",
        job_description="Backend")
    assert "ProjectAgent" in planned["plan"]
    assert "project_findings" in planned["goalArtifacts"]


def test_evidence_enabled_forces_evidence_agent():
    coordinator = _coordinator({
        "evidenceVerification": {"enabled": True},
        "maxAgentCount": 8,
    })
    planned = coordinator.plan_from_artifacts(
        run_type="full_evaluation", needs_parse=True,
        resume_text="工作经历\n2022-2024 工程师\n技能 Java",
        job_description="Java")
    assert "EvidenceAgent" in planned["plan"]
    assert "evidence_ledger" in planned["goalArtifacts"]


def test_evidence_disabled_skips_evidence_agent():
    coordinator = _coordinator({
        "evidenceVerification": {"enabled": False},
        "maxAgentCount": 8,
    })
    planned = coordinator.plan_from_artifacts(
        run_type="full_evaluation", needs_parse=True,
        resume_text="工作经历\n2022-2024 工程师\n技能 Java",
        job_description="Java")
    assert "EvidenceAgent" not in planned["plan"]
    assert "evidence_ledger" not in planned["goalArtifacts"]


def test_refine_cannot_drop_sole_goal_producer():
    coordinator = _coordinator({"evidenceVerification": {"enabled": True}})
    base = ["ResumeParserAgent", "ProjectAgent", "EvidenceAgent", "ReportAgent"]
    goals = ["project_findings", "evidence_ledger", "final_report"]
    # LLM wrongly drops ProjectAgent — sole producer of project_findings.
    refined = ["ResumeParserAgent", "EvidenceAgent", "ReportAgent"]
    protected = coordinator._protect_required_producers(refined, base, goals)
    assert "ProjectAgent" in protected
    assert protected[-1] == "ReportAgent"


def test_finalize_reports_missing_goal_artifacts_when_unproducible():
    coordinator = _coordinator()
    # Plan without ReportAgent producer for final_report — closure should repair.
    finalized = coordinator._finalize(
        ["ResumeParserAgent"], "test",
        goal_artifacts=["resume_facts", "final_report"])
    assert "ReportAgent" in finalized["plan"] or finalized.get("missingGoalArtifacts")
    # After repair, final_report should be producible.
    assert "final_report" not in (finalized.get("missingGoalArtifacts") or [])


def test_full_eval_types_constant():
    assert "full_evaluation" in FULL_EVAL_TYPES
    assert "backend_eval" in FULL_EVAL_TYPES
