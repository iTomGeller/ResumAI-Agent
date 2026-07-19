from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import List, Set

from langchain_core.tools import StructuredTool

from app.config import settings
from app.tools import execute_skill, list_skills

logger = logging.getLogger(__name__)


# Keep the mapping explicit: an Agent can discover and execute only Skills that
# are relevant to its responsibility. Legacy underscore names remain during the
# graph migration so existing calls do not break.
AGENT_SKILL_ALLOWLIST: dict[str, frozenset[str]] = {
    "IntentAgent": frozenset({
        "route-conversation-turn",
        "plan-evaluation-revision",
        "explain-evaluation-decision",
        "intent_routing",
    }),
    "ResumeParseAgent": frozenset({
        "assess-ats-compatibility",
    }),
    "JdMatchAgent": frozenset({
        "normalize-job-description",
        "assess-ats-compatibility",
        "compare-target-roles",
    }),
    "TechEvalAgent": frozenset({
        "assess-technical-evidence",
        "inspect-github-portfolio",
        "calibrate-evidence-confidence",
        "tech_stack_assessment",
    }),
    "ProjectEvalAgent": frozenset({
        "ground-project-claims",
        "inspect-github-portfolio",
        "generate-interview-probes",
        "project_depth_analysis",
    }),
    "RiskAgent": frozenset({
        "calibrate-evidence-confidence",
        "audit-job-relevant-evaluation",
        "risk_pattern_detection",
    }),
    "EvidenceFusionAgent": frozenset({
        "calibrate-evidence-confidence",
        "audit-job-relevant-evaluation",
        "evidence_synthesis",
    }),
    "ReportAgent": frozenset({
        "assess-ats-compatibility",
        "compare-target-roles",
        "generate-interview-probes",
        "calibrate-evidence-confidence",
        "audit-job-relevant-evaluation",
        "explain-evaluation-decision",
        "evidence_synthesis",
    }),
}


def allowed_skills_for_agent(agent_name: str) -> frozenset[str]:
    return AGENT_SKILL_ALLOWLIST.get(agent_name, frozenset())


def _filter_skill_listing(raw: str, allowed: Set[str]) -> str:
    """Filter the Java skill listing without inventing metadata on failures."""
    try:
        data = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return raw
    if not isinstance(data, list):
        return raw
    filtered = [
        item for item in data
        if isinstance(item, dict) and str(item.get("name", "")) in allowed
    ]
    return json.dumps(filtered, ensure_ascii=False)


def get_skill_tools(agent_name: str, whitelist: Set[str]) -> List[StructuredTool]:
    tools: List[StructuredTool] = []
    allowed = allowed_skills_for_agent(agent_name)
    allowed_text = ", ".join(sorted(allowed)) or "none"

    if "list_skills" in whitelist:
        async def _list() -> str:
            return _filter_skill_listing(await list_skills(), set(allowed))

        tools.append(StructuredTool.from_function(
            coroutine=_list,
            name="list_skills",
            description=f"List Skills allowed for {agent_name}: {allowed_text}",
        ))

    if "execute_skill" in whitelist:
        async def _execute(skillName: str, task: str = "") -> str:
            requested = str(skillName or "").strip()
            if requested not in allowed:
                return json.dumps({
                    "error": "skill_not_allowed_for_agent",
                    "agentName": agent_name,
                    "skillName": requested,
                    "allowedSkills": sorted(allowed),
                }, ensure_ascii=False)
            return await execute_skill(requested, task)

        tools.append(StructuredTool.from_function(
            coroutine=_execute,
            name="execute_skill",
            description=f"Load and execute one Skill allowed for {agent_name}: {allowed_text}",
        ))
    return tools


def _frontmatter_value(skill_file: Path, key: str) -> str:
    try:
        for line in skill_file.read_text(encoding="utf-8").splitlines()[1:20]:
            if line.strip() == "---":
                break
            field, separator, value = line.partition(":")
            if separator and field.strip() == key:
                return value.strip().strip('"\'')
    except OSError as exc:
        logger.warning("Unable to read skill metadata from %s: %s", skill_file, exc)
    return ""


def scan_skills_metadata() -> List[dict]:
    root = Path(settings.skills_path)
    if not root.is_dir():
        return []
    items = []
    for skill_dir in sorted(root.iterdir(), key=lambda item: item.name):
        skill_file = skill_dir / "SKILL.md"
        if skill_file.is_file():
            items.append({
                "name": _frontmatter_value(skill_file, "name") or skill_dir.name,
                "description": _frontmatter_value(skill_file, "description"),
                "path": str(skill_file),
            })
    return items
