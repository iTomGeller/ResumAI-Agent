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
from app.runtime.builtin_tools import BuiltinToolRegistry
from app.runtime.events import NullEmitter
from app.runtime.models import RunBudget
from app.runtime.skills import PRODUCTION_SKILLS, SkillManager, resolve_skills_root
from app.runtime.tools import ToolDefinition, ToolExecutor


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
    # Resources can retain compatibility/admin packages, but only reviewed
    # production capabilities enter any model-facing catalog.
    active_ids = {skill.skill_id for skill in manager.catalog()}
    assert active_ids == set(PRODUCTION_SKILLS)
    manifest = manager.runtime_manifest(include_deprecated=True)
    assert manifest["activeCount"] == len(PRODUCTION_SKILLS)
    assert {skill["skillId"] for skill in manifest["skills"]
            if not skill["deprecated"]} == set(PRODUCTION_SKILLS)
    assert manifest["advertisedTools"] == [
        "load_skill", "read_skill_resource"]

    selected = manager.select_for(
        agent_id="ReportAgent", run_type="full_evaluation",
        job_focus=None, overrides={},
        signals={"has_jd": True, "has_projects": True})
    assert [skill.skill_id for skill in selected] == [
        "audit-job-relevant-evaluation"]


def test_skill_selection_is_one_or_two_and_revision_aware():
    manager = SkillManager(resolve_skills_root())
    parser = manager.select_for(
        agent_id="ResumeParserAgent", run_type="full_evaluation",
        job_focus=None, overrides={}, signals={}, user_message="评估简历")
    assert parser == [], "deterministic parser must not expose conversation Skills"

    why = manager.select_for(
        agent_id="ReportAgent", run_type="followup",
        job_focus=None, overrides={},
        signals={}, user_message="为什么项目深度只有 60 分？")
    assert [skill.skill_id for skill in why] == [
        "route-conversation-turn", "explain-evaluation-decision"]

    revision = manager.select_for(
        agent_id="ReportAgent", run_type="followup",
        job_focus=None, overrides={},
        signals={}, user_message="把 JD 换成 AI 产品经理并重新评估")
    assert [skill.skill_id for skill in revision] == [
        "route-conversation-turn", "plan-evaluation-revision"]

    for agent_id in (
            "ResumeParserAgent", "JDAnalysisAgent", "TechAgent",
            "ProjectAgent", "RiskAgent", "EvidenceAgent",
            "ReportAgent", "ResumeOptimizeAgent",
            "InterviewQuestionAgent"):
        selected = manager.select_for(
            agent_id=agent_id, run_type="full_evaluation",
            job_focus=None, overrides={},
            signals={
                "has_jd": True,
                "has_jd_requirements": True,
                "has_projects": True,
                "has_timeline": True,
                "has_external_urls": True,
            })
        assert len(selected) <= 2
        assert all(skill.skill_id in PRODUCTION_SKILLS for skill in selected)


def test_skill_selection_varies_with_candidate_and_request_signals():
    manager = SkillManager(resolve_skills_root())
    github = manager.select_for(
        agent_id="ProjectAgent", run_type="full_evaluation",
        job_focus=None, overrides={},
        signals={
            "has_projects": True,
            "has_external_urls": True,
            "has_github": True,
        })
    public_url = manager.select_for(
        agent_id="ProjectAgent", run_type="full_evaluation",
        job_focus=None, overrides={},
        signals={
            "has_projects": True,
            "has_external_urls": True,
            "has_github": False,
        })
    no_url = manager.select_for(
        agent_id="ProjectAgent", run_type="full_evaluation",
        job_focus=None, overrides={},
        signals={"has_projects": True, "has_external_urls": False})
    interview = manager.select_for(
        agent_id="ReportAgent", run_type="full_evaluation",
        job_focus=None, overrides={}, signals={},
        user_message="请给出大厂面试追问")

    assert [skill.skill_id for skill in github] == [
        "inspect-github-portfolio", "ground-project-claims"]
    assert [skill.skill_id for skill in public_url] == [
        "retrieve-public-candidate-evidence", "ground-project-claims"]
    assert [skill.skill_id for skill in no_url] == ["ground-project-claims"]
    assert [skill.skill_id for skill in interview] == [
        "audit-job-relevant-evaluation", "generate-interview-probes"]


class _CatalogRegistry:
    def __init__(self, routes):
        self.routes = routes

    def tools_for_agent(self, agent_id):
        return list(self.routes.get(agent_id) or [])


def _catalog_for(resume_text: str, job_description: str, agent_id: str):
    tools = ToolExecutor(
        NullEmitter(), RunBudget(), BuiltinToolRegistry(),
        max_tool_calls_run=10, tool_timeout_seconds=2,
        run_context={
            "resumeText": resume_text,
            "jobDescription": job_description,
            "userMessage": "",
        })
    routed = {
        "ProjectAgent": [
            "exa.web_search_exa", "exa.web_fetch_exa", "fetch.fetch",
            "deepwiki.ask_question",
        ],
        "TechAgent": [
            "context7.resolve-library-id", "context7.query-docs",
            "microsoft-learn.microsoft_docs_search",
        ],
    }
    for name in {item for values in routed.values() for item in values}:
        server = name.split(".", 1)[0]
        tools.definitions[name] = ToolDefinition(
            name=name,
            description=name,
            input_schema={"type": "object"},
            output_schema={"type": "object"},
            kind="mcp",
            mcp_server=server,
        )
    tools.mcp_registry = _CatalogRegistry(routed)
    return {
        row["name"] for row in tools.catalog_for_agent(agent_id, [])
    }


def test_mcp_catalog_is_signal_gated_not_coverage_rotated():
    assert _catalog_for(
        "Java Spring Boot 项目，无外链", "Java 后端", "ProjectAgent") == set()
    assert _catalog_for(
        "项目 https://gitee.com/acme/demo", "Java 后端", "ProjectAgent") == {
            "exa.web_search_exa", "exa.web_fetch_exa", "fetch.fetch",
        }
    assert _catalog_for(
        "项目 https://github.com/acme/demo", "Java 后端", "ProjectAgent") == {
            "exa.web_search_exa", "exa.web_fetch_exa", "fetch.fetch",
            "deepwiki.ask_question",
        }
    assert _catalog_for(
        "Java Spring Boot Redis", "Java 后端", "TechAgent") == {
            "context7.resolve-library-id", "context7.query-docs",
        }
    assert _catalog_for(
        "C# ASP.NET Core Azure", ".NET 后端", "TechAgent") == {
            "microsoft-learn.microsoft_docs_search",
        }


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
