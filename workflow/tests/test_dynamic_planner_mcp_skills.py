from __future__ import annotations

import sys
from pathlib import Path

WORKFLOW_ROOT = Path(__file__).resolve().parents[1]
if str(WORKFLOW_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKFLOW_ROOT))

from app.runtime.coordinator import GOAL_ARTIFACTS, TASK_PIPELINES, Coordinator
from app.runtime.agents import default_agent_registry
from app.runtime.mcp_registry import load_mcp_config, resolve_mcp_config_path
from app.runtime.models import PolicyBundle
from app.runtime.skills import resolve_skills_root, SkillManager


def test_mcp_config_resolves_shared_file():
    path = resolve_mcp_config_path()
    assert path is not None, "config/mcp-servers.json should resolve"
    cfg = load_mcp_config()
    assert "exa" in (cfg.get("mcpServers") or {})
    assert "context7" in (cfg.get("mcpServers") or {})
    assert "deepwiki" in (cfg.get("mcpServers") or {})
    assert "microsoft-learn" in (cfg.get("mcpServers") or {})
    assert "fetch" in (cfg.get("mcpServers") or {})
    assert (cfg.get("optionalMcpServers") or {}) == {}


def test_skills_load_from_backend_resources():
    root = resolve_skills_root()
    assert root is not None
    manager = SkillManager(root)
    ids = set(manager.list_ids())
    assert "retrieve-public-candidate-evidence" in ids
    assert "handle-knowledge-no-evidence" in ids
    assert "calibrate-evidence-confidence" in ids
    selected = manager.select_for(
        agent_id="ReportAgent", run_type="full_evaluation",
        job_focus=None, overrides={},
        signals={"has_jd": True, "has_projects": True})
    names = {s.skill_id for s in selected}
    assert "handle-knowledge-no-evidence" in names
    assert "calibrate-evidence-confidence" in names


def test_artifact_planner_is_primary_not_task_pipelines():
    policy = PolicyBundle.from_config("balanced", {})
    coordinator = Coordinator(default_agent_registry, policy, None)
    assert "full_evaluation" in GOAL_ARTIFACTS
    assert "full_evaluation" in TASK_PIPELINES

    planned = coordinator.plan_from_artifacts(
        run_type="full_evaluation", needs_parse=True,
        resume_text="项目经历\nResumAI\n工作经历\n2022-2024 后端\nhttps://github.com/demo/x",
        job_description="Java Spring")
    assert planned["reason"].startswith("artifact")
    assert planned["plan"][0] == "ResumeParserAgent"
    assert "ProjectAgent" in planned["plan"]
    assert "RiskAgent" in planned["plan"]
    assert planned["plan"][-1] in {"ReportAgent", "ResumeOptimizeAgent",
                                   "InterviewQuestionAgent"}
    assert planned.get("selectedBecause")
    assert planned.get("budget") or planned.get("budgetPlan")

    no_project = coordinator.plan_from_artifacts(
        run_type="full_evaluation", needs_parse=True,
        resume_text="工作经历\n2022-2024 工程师\n技能 Java Redis",
        job_description="Java")
    assert "ProjectAgent" not in no_project["plan"]
    assert "ProjectAgent" in (no_project.get("skippedBecause") or {})


def test_replan_triggers_include_required_kinds():
    from app.runtime.coordinator import REPLAN_TRIGGERS
    for kind in ("missing_required_artifact", "tool_failed",
                 "new_conflict", "handoff_requested"):
        assert kind in REPLAN_TRIGGERS
