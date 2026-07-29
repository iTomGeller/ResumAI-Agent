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


def test_project_analysis_with_project_evidence_keeps_full_project_chain():
    """Regression from ECS: `项目：...` plus a project-depth revision was
    misclassified as no project, producing Tech→Evidence→Report."""
    coordinator = _coordinator({
        "evidenceVerification": {"enabled": True},
        "maxAgentCount": 3,
        "maxLlmCalls": 8,
    })
    planned = coordinator.plan_from_artifacts(
        run_type="project_analysis",
        needs_parse=False,
        resume_text=(
            "李明，5年Java后端经验。"
            "项目：支付网关，负责幂等、限流与对账。"
            "项目：Agent评估平台，负责RAG、MCP与Trace。"
        ),
        job_description="高级Java Agent平台后端工程师",
        artifacts={"resumeFacts": {"skills": ["Java", "Kafka"]}},
    )

    plan = planned["plan"]
    assert "project_findings" in planned["goalArtifacts"], planned
    assert "ProjectAgent" in plan, planned
    assert "EvidenceAgent" in plan, planned
    assert "ReportAgent" in plan, planned
    assert plan.index("ProjectAgent") < plan.index("EvidenceAgent")
    assert plan.index("EvidenceAgent") < plan.index("ReportAgent")
    assert "ProjectAgent" not in (planned.get("skippedBecause") or {})
    assert planned["budgetPlan"]["ProjectAgent"]["llmQuota"] >= 1
    assert planned["planClosureOk"] is True


def test_project_analysis_without_project_evidence_may_skip_project_agent():
    coordinator = _coordinator({
        "evidenceVerification": {"enabled": True},
        "maxAgentCount": 3,
    })
    planned = coordinator.plan_from_artifacts(
        run_type="project_analysis",
        needs_parse=False,
        resume_text="技能：Java、Redis。工作经历：2022-2025 后端工程师。",
        job_description="Java后端工程师",
        artifacts={"resumeFacts": {"skills": ["Java", "Redis"]}},
    )

    assert "ProjectAgent" not in planned["plan"]
    assert "project_findings" not in planned["goalArtifacts"]
    assert "project_findings" in planned["optionalArtifacts"]


def test_finalize_repairs_project_agent_dropped_by_external_planner():
    """The final closure is the last fence against an LLM/cache/budget-derived
    plan that omitted the required project_findings producer."""
    coordinator = _coordinator({
        "evidenceVerification": {"enabled": True},
        "maxAgentCount": 3,
    })
    finalized = coordinator._finalize(
        ["EvidenceAgent", "ReportAgent"],
        "external_planner_wrongly_dropped_project",
        goal_artifacts=[
            "resume_facts", "project_findings",
            "evidence_ledger", "final_report",
        ],
        present_artifacts={"resume_facts"},
    )

    assert finalized["plan"] == [
        "ProjectAgent", "EvidenceAgent", "ReportAgent",
    ]
    assert finalized["missingGoalArtifacts"] == []
    assert finalized["planClosureOk"] is True
    assert finalized["budgetPlan"]["ProjectAgent"]["llmQuota"] >= 1


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


def test_sparse_resume_keeps_parallel_evidence_pipeline():
    coordinator = _coordinator({"evidenceVerification": {"enabled": True}})
    planned = coordinator.plan_from_artifacts(
        run_type="full_evaluation",
        needs_parse=True,
        resume_text="a" * 150 + " github.com/user 2022-2024",
        job_description="Java Spring",
    )

    assert planned["plan"] == [
        "ResumeParserAgent", "JDAnalysisAgent", "TechAgent", "ProjectAgent",
        "RiskAgent", "EvidenceAgent", "ReportAgent",
    ]
    assert sum(item["llmQuota"] for item in planned["budgetPlan"].values()) <= 12
    assert planned["budgetPlan"]["TechAgent"]["llmQuota"] == 2
    assert planned["budgetPlan"]["RiskAgent"]["llmQuota"] == 2
    assert planned["budgetPlan"]["ReportAgent"]["llmQuota"] == 3
    assert planned["budgetPlan"]["ProjectAgent"]["actionTurnQuota"] == 2
    assert planned["budgetPlan"]["TechAgent"]["actionTurnQuota"] == 1


def test_sparse_resume_with_project_hint_keeps_project_and_evidence():
    """Short resumes stay multi-agent; unsupported dimensions alone are skipped."""
    coordinator = _coordinator({"evidenceVerification": {"enabled": True}})
    planned = coordinator.plan_from_artifacts(
        run_type="full_evaluation",
        needs_parse=True,
        resume_text=("候选人，目标 Java 后端工程师，拥有两年服务端开发经验。"
                     "技能 Java Spring Boot Redis MySQL，熟悉 Linux、Git 和 REST API。"
                     "项目：订单服务重构，完成接口设计、缓存一致性处理和自动化测试。"
                     "成果：优化慢查询与缓存策略，提升接口稳定性并编写技术文档。"),
        job_description="Java Spring",
    )
    assert planned["plan"] == [
        "ResumeParserAgent", "JDAnalysisAgent", "TechAgent", "ProjectAgent",
        "EvidenceAgent", "ReportAgent",
    ]
    assert "RiskAgent" not in planned["plan"]
    assert "project_findings" in planned["goalArtifacts"]
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
