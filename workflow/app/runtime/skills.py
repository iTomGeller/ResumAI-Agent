from __future__ import annotations

"""Skill manager: backend/src/main/resources/skills is the single source of truth.

Workflow loads SKILL.md packages at startup (and on refresh). Selection follows
the plan trigger matrix — never dump every skill into the prompt.
"""

import hashlib
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

# Deprecated aliases kept on disk for admin/compat but not injected into the
# candidate-evaluation main runtime path.
DEPRECATED_SKILLS = {
    "intent_routing",
    "webapp-testing",
    "mcp-builder",
    "skill-creator",
}

# Skills that are admin-only / not part of candidate evaluation.
ADMIN_ONLY_SKILLS = {"webapp-testing", "mcp-builder", "skill-creator"}


@dataclass(frozen=True)
class SkillDefinition:
    skill_id: str
    name: str
    version: str
    description: str
    applicable_conditions: tuple
    instructions: str
    positive_examples: tuple = ()
    negative_examples: tuple = ()
    required_tools: tuple = ()
    required_mcp: tuple = ()
    output_requirements: str = ""
    evaluation_metrics: tuple = ()
    status: str = "ACTIVE"
    source_path: str = ""
    deprecated: bool = False

    @property
    def hash(self) -> str:
        return hashlib.sha256(self.instructions.encode("utf-8")).hexdigest()[:12]


def _skills_root_candidates() -> List[Path]:
    configured = (os.getenv("SKILLS_PATH") or "").strip()
    here = Path(__file__).resolve()
    # workflow/app/runtime -> repo root = parents[3]
    repo = here.parents[3]
    candidates: List[Path] = []
    if configured:
        candidates.append(Path(configured))
    candidates.extend([
        Path("/app/skills"),
        repo / "backend" / "src" / "main" / "resources" / "skills",
        Path("backend/src/main/resources/skills"),
        Path("src/main/resources/skills"),
        Path("skills"),
    ])
    return candidates


def resolve_skills_root() -> Optional[Path]:
    for candidate in _skills_root_candidates():
        if not candidate or not str(candidate).strip() or str(candidate) in (".", ""):
            continue
        if candidate.is_dir() and any(candidate.glob("*/SKILL.md")):
            return candidate
        if candidate.is_dir() and candidate.name == "skills":
            # Prefer dirs that look like a skills root even before packages exist.
            return candidate
    # Fallback: first existing directory among candidates.
    for candidate in _skills_root_candidates():
        if candidate and candidate.is_dir() and str(candidate) not in (".", ""):
            return candidate
    return None


def _parse_frontmatter(text: str) -> Tuple[Dict[str, str], str]:
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end < 0:
        return {}, text
    header = text[3:end].strip()
    body = text[end + 4:].lstrip("\n")
    meta: Dict[str, str] = {}
    for line in header.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        meta[key.strip()] = value.strip().strip('"').strip("'")
    return meta, body


def _load_skill_dir(skill_dir: Path) -> Optional[SkillDefinition]:
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.is_file():
        return None
    try:
        raw = skill_md.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("failed reading %s: %s", skill_md, exc)
        return None
    meta, body = _parse_frontmatter(raw)
    skill_id = meta.get("name") or skill_dir.name
    description = meta.get("description") or ""
    allowed = tuple(
        t for t in re.split(r"\s+", meta.get("allowed-tools", "").strip()) if t)
    version = meta.get("version") or "v1"
    deprecated = skill_id in DEPRECATED_SKILLS or skill_id in ADMIN_ONLY_SKILLS \
        or meta.get("status", "").lower() == "deprecated"
    status = "DEPRECATED" if deprecated else "ACTIVE"
    return SkillDefinition(
        skill_id=skill_id,
        name=skill_id,
        version=version,
        description=description[:500],
        applicable_conditions=(),
        instructions=body.strip()[:12000],
        required_tools=allowed,
        output_requirements="",
        status=status,
        source_path=str(skill_md),
        deprecated=deprecated,
    )


class SkillManager:
    """Disk-backed skill registry with the plan trigger matrix."""

    def __init__(self, root: Optional[Path] = None) -> None:
        self.root = root or resolve_skills_root()
        self._by_id: Dict[str, Dict[str, SkillDefinition]] = {}
        self.reload()

    def reload(self) -> int:
        self._by_id.clear()
        root = self.root or resolve_skills_root()
        self.root = root
        if root is None:
            logger.warning("skills root not found; skill catalog empty")
            return 0
        count = 0
        try:
            for child in sorted(root.iterdir()):
                if not child.is_dir():
                    continue
                skill = _load_skill_dir(child)
                if skill is None:
                    continue
                self._by_id.setdefault(skill.skill_id, {})[skill.version] = skill
                count += 1
        except OSError as exc:
            logger.warning("skills scan failed: %s", exc)
            return 0
        logger.info("loaded %d skills from %s", count, root)
        return count

    def get(self, skill_id: str, version: Optional[str] = None) -> SkillDefinition:
        versions = self._by_id.get(skill_id)
        if not versions:
            raise KeyError(f"unknown skill: {skill_id}")
        if version and version in versions:
            return versions[version]
        active = [s for s in versions.values() if s.status == "ACTIVE"]
        return sorted(active or list(versions.values()), key=lambda s: s.version)[-1]

    def list_ids(self) -> List[str]:
        return list(self._by_id.keys())

    def select_for(self, *, agent_id: str, run_type: str, job_focus: Optional[str],
                   overrides: Dict[str, str],
                   signals: Optional[Dict[str, bool]] = None,
                   user_message: str = "") -> List[SkillDefinition]:
        """Precise trigger matrix from the agent-runtime plan §5.2."""
        signals = signals or {}
        selected_ids: List[str] = []

        def add(skill_id: str) -> None:
            if skill_id and skill_id not in selected_ids and skill_id in self._by_id:
                if skill_id in ADMIN_ONLY_SKILLS:
                    return
                selected_ids.append(skill_id)

        # Every turn: conversation routing (Coordinator / first specialist).
        if agent_id in ("CoordinatorAgent", "ResumeParserAgent", "ReportAgent"):
            add("route-conversation-turn")

        if agent_id == "ResumeParserAgent":
            add("assess-ats-compatibility")

        if agent_id == "JDAnalysisAgent":
            if signals.get("has_jd") or run_type in (
                    "full_evaluation", "jd_evaluation", "jd_gap",
                    "backend_eval", "agent_eval"):
                add("normalize-job-description")

        if agent_id == "TechAgent":
            if signals.get("has_jd_requirements") or signals.get("has_jd") \
                    or run_type in ("tech_match", "jd_gap", "full_evaluation",
                                    "jd_evaluation", "backend_eval", "agent_eval"):
                add("assess-technical-evidence")

        if agent_id == "ProjectAgent":
            if signals.get("has_projects", True):
                add("ground-project-claims")
            if signals.get("has_external_urls"):
                add("retrieve-public-candidate-evidence")
                add("inspect-github-portfolio")

        if agent_id == "RiskAgent":
            if signals.get("has_timeline", True) or run_type in (
                    "risk_check", "timeline_check", "interview_questions"):
                add("risk_pattern_detection")

        if agent_id == "EvidenceAgent":
            add("calibrate-evidence-confidence")

        if agent_id == "ReportAgent":
            add("calibrate-evidence-confidence")
            add("audit-job-relevant-evaluation")
            add("handle-knowledge-no-evidence")
            if run_type not in ("followup", "quick_answer"):
                add("generate-interview-probes")
            if run_type in ("followup", "quick_answer") or "为什么" in (user_message or "") \
                    or "依据" in (user_message or ""):
                add("explain-evaluation-decision")
            if signals.get("compare_roles"):
                add("compare-target-roles")

        if agent_id == "InterviewQuestionAgent":
            add("generate-interview-probes")

        if agent_id == "ResumeOptimizeAgent":
            add("ground-project-claims")

        if run_type in ("followup", "quick_answer") and agent_id == "ReportAgent":
            add("plan-evaluation-revision")

        # Policy / focus overrides (still capped).
        override = overrides.get(agent_id)
        if override:
            add(override)

        # Hard cap: never flood the prompt.
        skills: List[SkillDefinition] = []
        for skill_id in selected_ids[:4]:
            try:
                skill = self.get(skill_id)
            except KeyError:
                continue
            if skill.deprecated and skill_id not in (override or "",):
                continue
            skills.append(skill)
        return skills

    @staticmethod
    def render(skills: List[SkillDefinition]) -> str:
        blocks = []
        for skill in skills:
            tools = (", ".join(skill.required_tools) if skill.required_tools
                     else "（未声明）")
            blocks.append(
                f"技能 {skill.name}（{skill.skill_id}@{skill.version}"
                f"#{skill.hash}）：\n{skill.description}\n"
                f"{skill.instructions}\nallowedTools: {tools}")
        return "\n\n".join(blocks)

    def versions_used(self, selections: Dict[str, List[SkillDefinition]]) -> Dict[str, str]:
        used = {}
        for _agent_id, skills in selections.items():
            for skill in skills:
                used[skill.skill_id] = f"{skill.version}#{skill.hash}"
        return used

    def runtime_manifest(self, *, include_deprecated: bool = False) -> Dict[str, Any]:
        """Python SkillManager snapshot for Ops — not the Java install catalog alone."""
        active: List[Dict[str, Any]] = []
        deprecated: List[Dict[str, Any]] = []
        for skill_id in sorted(self.list_ids()):
            try:
                skill = self.get(skill_id)
            except KeyError:
                continue
            item = {
                "skillId": skill.skill_id,
                "name": skill.name,
                "version": skill.version,
                "hash": skill.hash,
                "status": skill.status,
                "deprecated": skill.deprecated,
                "description": skill.description,
                "requiredTools": list(skill.required_tools),
                "requiredMcp": list(skill.required_mcp),
                "sourcePath": skill.source_path,
                "adminOnly": skill.skill_id in ADMIN_ONLY_SKILLS,
            }
            if skill.deprecated or skill.skill_id in ADMIN_ONLY_SKILLS:
                deprecated.append(item)
            else:
                active.append(item)
        skills = list(active)
        if include_deprecated:
            skills.extend(deprecated)
        return {
            "source": "python_skill_manager",
            "root": str(self.root or ""),
            "count": len(skills),
            "activeCount": len(active),
            "deprecatedCount": len(deprecated),
            "skills": skills,
            "deprecatedSkills": deprecated if include_deprecated else [],
            "advertisedTools": ["load_skill", "execute_skill", "read_skill_resource"],
        }

    async def emit_selection(self, emitter: Any, agent_id: str,
                             skills: List[SkillDefinition],
                             *, trigger_reason: str = "policy_match") -> None:
        """Emit skill lifecycle for Trace — Skill is not a tool execution.

        Events: skill.selected → skill.applied (no fake started/completed tool
        lifecycle). RunTraceBridge must consume these as Agent annotations.
        """
        for skill in skills:
            base = {
                "skillId": skill.skill_id,
                "skillVersion": skill.version,
                "skillHash": skill.hash,
                "agentId": agent_id,
                "triggerReason": trigger_reason,
            }
            await emitter.emit("skill.selected", agent_id=agent_id,
                               tool_name=skill.skill_id, payload=base)
            await emitter.emit("skill.applied", agent_id=agent_id,
                               tool_name=skill.skill_id, payload={
                                   **base, "injected": True})


default_skill_manager = SkillManager()
