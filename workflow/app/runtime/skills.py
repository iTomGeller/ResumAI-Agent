from __future__ import annotations

"""Skill manager: backend/src/main/resources/skills is the single source of truth.

Workflow loads SKILL.md packages at startup (and on refresh). Selection follows
the plan trigger matrix — never dump every skill into the prompt.
"""

import hashlib
import logging
import os
import re
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Reviewed production catalog. Selection remains signal-gated and capped per
# agent; membership here means "eligible", never "always inject/call".
PRODUCTION_SKILLS = frozenset({
    "assess-production-engineering",
    "assess-technical-evidence",
    "audit-claim-consistency",
    "audit-evidence-provenance",
    "calibrate-evidence-confidence",
    "ground-project-claims",
    "retrieve-public-candidate-evidence",
    "risk-pattern-detection",
})

# Skills that are admin-only / not part of candidate evaluation.
ADMIN_ONLY_SKILLS = frozenset({
    "webapp-testing",
    "mcp-builder",
    "skill-creator",
})

# Compatibility/domain packages intentionally kept as resources but hidden
# from the production catalog. Unknown dynamically installed packages are
# likewise non-production until explicitly reviewed into PRODUCTION_SKILLS.
DEPRECATED_SKILLS = frozenset({
    "assess-ats-compatibility",
    "audit-job-relevant-evaluation",
    "calibrate-and-explain-decision",
    "compare-target-roles",
    "evidence_synthesis",
    "evaluate-candidate-evidence",
    "explain-evaluation-decision",
    "generate-interview-probes",
    "handle-knowledge-no-evidence",
    "inspect-github-portfolio",
    "intent_routing",
    "normalize-job-description",
    "plan-evaluation-revision",
    "project_depth_analysis",
    "route-conversation-turn",
    "tech_stack_assessment",
    *ADMIN_ONLY_SKILLS,
})


@dataclass(frozen=True)
class SkillDefinition:
    skill_id: str
    name: str
    version: str
    description: str
    applicable_conditions: tuple = ()
    instructions: str = ""
    positive_examples: tuple = ()
    negative_examples: tuple = ()
    required_tools: tuple = ()
    required_mcp: tuple = ()
    output_requirements: str = ""
    evaluation_metrics: tuple = ()
    status: str = "ACTIVE"
    source_path: str = ""
    deprecated: bool = False
    loaded: bool = False
    content_hash: str = ""
    resource_paths: tuple = ()

    @property
    def hash(self) -> str:
        # The full-file content hash is intentionally unavailable until the
        # skill is activated. Startup discovery reads frontmatter only.
        return self.content_hash or "not-loaded"


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
        here.parents[2] / "skills",  # development fallback only
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


def _read_frontmatter_only(skill_md: Path) -> Dict[str, str]:
    """Read only SKILL.md frontmatter during catalog discovery.

    Agent Skills progressive disclosure requires startup context to contain
    metadata only. Keeping the body out of the in-memory catalog also makes
    accidental eager prompt injection structurally impossible.
    """
    try:
        with skill_md.open("r", encoding="utf-8") as handle:
            if handle.readline().strip() != "---":
                return {}
            lines: List[str] = []
            for line in handle:
                if line.strip() == "---":
                    break
                lines.append(line)
                if len(lines) > 200:
                    raise ValueError("SKILL.md frontmatter exceeds 200 lines")
            else:
                return {}
    except (OSError, UnicodeError, ValueError) as exc:
        logger.warning("failed reading skill metadata %s: %s", skill_md, exc)
        return {}
    meta: Dict[str, str] = {}
    for line in lines:
        if ":" not in line or line[:1].isspace():
            continue
        key, value = line.split(":", 1)
        meta[key.strip()] = value.strip().strip('"').strip("'")
    return meta


def _load_skill_dir(skill_dir: Path) -> Optional[SkillDefinition]:
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.is_file():
        return None
    meta = _read_frontmatter_only(skill_md)
    skill_id = meta.get("name") or skill_dir.name
    description = meta.get("description") or ""
    allowed = tuple(
        t for t in re.split(r"\s+", meta.get("allowed-tools", "").strip()) if t)
    version = meta.get("version") or "v1"
    deprecated = skill_id not in PRODUCTION_SKILLS \
        or skill_id in DEPRECATED_SKILLS or skill_id in ADMIN_ONLY_SKILLS \
        or meta.get("status", "").lower() == "deprecated"
    status = "DEPRECATED" if deprecated else "ACTIVE"
    # Resource path discovery is metadata-only: list filenames without
    # reading their contents. This lets the runtime hide the generic resource
    # tool for Skills that have no on-demand references at all.
    resources: List[str] = []
    for folder_name in ("references", "scripts", "assets"):
        folder = skill_dir / folder_name
        if not folder.is_dir():
            continue
        try:
            resources.extend(
                child.relative_to(skill_dir).as_posix()
                for child in sorted(folder.iterdir()) if child.is_file())
        except OSError:
            continue
    return SkillDefinition(
        skill_id=skill_id,
        name=skill_id,
        version=version,
        description=description[:500],
        applicable_conditions=(),
        instructions="",
        required_tools=allowed,
        output_requirements="",
        status=status,
        source_path=str(skill_md),
        deprecated=deprecated,
        loaded=False,
        resource_paths=tuple(resources),
    )


class SkillManager:
    """Disk-backed skill registry with the plan trigger matrix."""

    def __init__(self, root: Optional[Path] = None) -> None:
        self.root = root or resolve_skills_root()
        self._by_id: Dict[str, Dict[str, SkillDefinition]] = {}
        self._loaded: Dict[Tuple[str, str], SkillDefinition] = {}
        self.reload()

    def reload(self) -> int:
        self._by_id.clear()
        self._loaded.clear()
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

    def catalog(self, *, include_deprecated: bool = False) -> List[SkillDefinition]:
        """Return metadata-only entries in stable order."""
        result: List[SkillDefinition] = []
        for skill_id in sorted(self.list_ids()):
            try:
                skill = self.get(skill_id)
            except KeyError:
                continue
            if not include_deprecated and (skill.deprecated or skill.status != "ACTIVE"):
                continue
            result.append(skill)
        return result

    def load(self, skill_id: str, version: Optional[str] = None) -> SkillDefinition:
        """Activate one skill by loading only its SKILL.md instructions.

        Referenced resources are indexed by path but are not read here.
        """
        metadata = self.get(skill_id, version)
        key = (metadata.skill_id, metadata.version)
        cached = self._loaded.get(key)
        if cached is not None:
            return cached
        skill_md = Path(metadata.source_path)
        try:
            raw = skill_md.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise KeyError(f"skill unreadable: {skill_id}: {exc}") from exc
        _meta, body = _parse_frontmatter(raw)
        resources: List[str] = []
        skill_root = skill_md.parent
        for folder_name in ("references", "scripts", "assets"):
            folder = skill_root / folder_name
            if not folder.is_dir():
                continue
            try:
                for child in sorted(folder.iterdir()):
                    if child.is_file():
                        resources.append(child.relative_to(skill_root).as_posix())
            except OSError:
                continue
        loaded = replace(
            metadata,
            instructions=body.strip()[:20000],
            loaded=True,
            content_hash=hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12],
            resource_paths=tuple(resources),
        )
        self._loaded[key] = loaded
        return loaded

    def read_resource(self, skill_id: str, relative_path: str, *,
                      version: Optional[str] = None,
                      max_chars: int = 12000) -> str:
        """Read one explicitly requested, one-level skill resource.

        The resource must have been advertised by ``load`` and remain inside
        the skill package. Deep reference chains and traversal are rejected.
        """
        loaded = self.load(skill_id, version)
        normalized = str(relative_path or "").replace("\\", "/").strip("/")
        if normalized not in set(loaded.resource_paths):
            raise KeyError(f"resource not advertised for {skill_id}: {normalized}")
        parts = Path(normalized).parts
        if len(parts) != 2 or parts[0] not in {"references", "scripts", "assets"}:
            raise KeyError(f"resource path must be one level deep: {normalized}")
        skill_root = Path(loaded.source_path).parent.resolve()
        target = (skill_root / normalized).resolve()
        try:
            target.relative_to(skill_root)
        except ValueError as exc:
            raise KeyError(f"resource escapes skill root: {normalized}") from exc
        if not target.is_file():
            raise KeyError(f"resource not found: {normalized}")
        try:
            return target.read_text(encoding="utf-8")[:max_chars]
        except (OSError, UnicodeError) as exc:
            raise KeyError(f"resource unreadable: {normalized}: {exc}") from exc

    def select_for(self, *, agent_id: str, run_type: str, job_focus: Optional[str],
                   overrides: Dict[str, str],
                   signals: Optional[Dict[str, bool]] = None,
                   user_message: str = "") -> List[SkillDefinition]:
        """Precise trigger matrix from the agent-runtime plan §5.2."""
        signals = signals or {}
        selected_ids: List[str] = []

        def add(skill_id: str) -> None:
            if not skill_id or skill_id in selected_ids \
                    or skill_id not in PRODUCTION_SKILLS \
                    or skill_id not in self._by_id:
                return
            try:
                skill = self.get(skill_id)
            except KeyError:
                return
            if skill.deprecated or skill.status != "ACTIVE":
                return
            selected_ids.append(skill_id)

        if agent_id == "TechAgent":
            if signals.get("has_jd_requirements") or signals.get("has_jd") \
                    or run_type in ("tech_match", "jd_gap", "full_evaluation",
                                    "jd_evaluation", "backend_eval", "agent_eval"):
                add("assess-technical-evidence")
                add("assess-production-engineering")

        if agent_id == "ProjectAgent":
            if signals.get("has_external_urls"):
                add("retrieve-public-candidate-evidence")
            if signals.get("has_projects", True):
                add("ground-project-claims")

        if agent_id == "RiskAgent":
            if signals.get("has_timeline", True) or run_type in (
                    "risk_check", "timeline_check", "interview_questions"):
                add("risk-pattern-detection")
                add("audit-claim-consistency")

        # Legacy DB overrides cannot resurrect hidden aliases.
        override = overrides.get(agent_id)
        if override:
            add(override)

        # Hard cap: expose one or two precise candidates, never a skill dump.
        skills: List[SkillDefinition] = []
        for skill_id in selected_ids[:2]:
            try:
                skill = self.get(skill_id)
            except KeyError:
                continue
            if skill.deprecated and skill_id not in (override or "",):
                continue
            skills.append(skill)
        return skills

    @staticmethod
    def render(skills: List[SkillDefinition], *, summary_only: bool = True) -> str:
        """Render metadata unless an individual definition is already loaded."""
        blocks = []
        for skill in skills:
            tools = (", ".join(skill.required_tools) if skill.required_tools
                     else "（未声明）")
            if summary_only or not skill.loaded:
                blocks.append(
                    f"[可用技能] {skill.name}（{skill.skill_id}@{skill.version}）："
                    f" {skill.description or '无描述'}"
                    f"\n  allowedTools: {tools}"
                    f"\n  → 需要时调用 load_skill(skill_id=\"{skill.skill_id}\")")
            else:
                blocks.append(
                    f"技能 {skill.name}（{skill.skill_id}@{skill.version}"
                    f"#{skill.hash}）：\n{skill.description}\n"
                    f"{skill.instructions}\nallowedTools: {tools}")
        return "\n\n".join(blocks)

    @staticmethod
    def render_progressive(catalog: List[SkillDefinition],
                           loaded: List[SkillDefinition]) -> str:
        loaded_ids = {s.skill_id for s in loaded}
        metadata = [s for s in catalog if s.skill_id not in loaded_ids]
        blocks: List[str] = []
        if metadata:
            blocks.append(SkillManager.render(metadata, summary_only=True))
        if loaded:
            blocks.append("[已加载技能指令]\n" +
                          SkillManager.render(loaded, summary_only=False))
        return "\n\n".join(b for b in blocks if b)

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
                "disclosureState": "METADATA",
                "instructionsLoaded": False,
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
            # Python production runtime supports progressive load/read only.
            # The legacy Java execute_skill compatibility endpoint is not a
            # model tool in this runtime and must not be advertised as one.
            "advertisedTools": ["load_skill", "read_skill_resource"],
            "disclosure": {
                "startup": "name+description",
                "activation": "SKILL.md",
                "resources": "on-demand",
            },
        }

    async def emit_catalog(self, emitter: Any, agent_id: str,
                           skills: List[SkillDefinition]) -> None:
        for skill in skills:
            event_id = f"skill-catalog-{uuid.uuid4().hex[:16]}"
            await emitter.emit("skill.catalog", agent_id=agent_id,
                               tool_name=skill.skill_id, payload={
                                   **self._event_base(skill, agent_id, event_id),
                                   "lifecycleStage": "CATALOG_EXPOSED",
                                   "reason": "metadata_available",
                                   "description": skill.description,
                                   "disclosureState": "METADATA",
                               })

    async def emit_selection(self, emitter: Any, agent_id: str,
                             skills: List[SkillDefinition],
                             *, trigger_reason: str = "agent_input_match") -> None:
        """Emit skill.selected only — marks skills as AVAILABLE for this agent.

        skill.applied is emitted later by executor when LLM actually calls
        load_skill, implementing true progressive loading.
        """
        for skill in skills:
            event_id = f"skill-select-{uuid.uuid4().hex[:16]}"
            base = self._event_base(skill, agent_id, event_id)
            await emitter.emit("skill.selected", agent_id=agent_id,
                               tool_name=skill.skill_id, payload={
                                   **base,
                                   "lifecycleStage": "SELECTED",
                                   "triggerReason": trigger_reason,
                                   "disclosureState": "METADATA",
                               })

    async def emit_loaded(self, emitter: Any, agent_id: str,
                          skill: SkillDefinition, *,
                          tool_call_id: str,
                          reason: str = "llm_requested",
                          round_id: Optional[str] = None) -> None:
        await emitter.emit("skill.loaded", agent_id=agent_id,
                           tool_name=skill.skill_id, payload={
                               **self._event_base(skill, agent_id, tool_call_id),
                               "roundId": round_id,
                               "parentRoundId": round_id,
                               "lifecycleStage": "LOADED",
                               "reason": reason,
                               "disclosureState": "INSTRUCTIONS",
                               "resourcesAdvertised": list(skill.resource_paths),
                           })

    async def emit_applied(self, emitter: Any, agent_id: str,
                           skill: SkillDefinition, *,
                           tool_call_id: str,
                           reason: str = "instructions_in_model_context",
                           round_id: Optional[str] = None) -> None:
        """Emit only after loaded instructions were included in a model turn."""
        await emitter.emit("skill.applied", agent_id=agent_id,
                           tool_name=skill.skill_id, payload={
                               **self._event_base(skill, agent_id, tool_call_id),
                               "roundId": round_id,
                               "applicationRoundId": round_id,
                               "lifecycleStage": "APPLIED",
                               "triggerReason": reason,
                               "injected": True})

    async def emit_skipped(self, emitter: Any, agent_id: str,
                           skill: SkillDefinition, *, reason: str) -> None:
        event_id = f"skill-skip-{uuid.uuid4().hex[:16]}"
        await emitter.emit("skill.skipped", agent_id=agent_id,
                           tool_name=skill.skill_id, payload={
                               **self._event_base(skill, agent_id, event_id),
                               "lifecycleStage": "SKIPPED",
                               "reason": reason,
                               "disclosureState": (
                                   "INSTRUCTIONS" if skill.loaded else "METADATA"),
                           })

    @staticmethod
    def _event_base(skill: SkillDefinition, agent_id: str,
                    tool_call_id: str) -> Dict[str, Any]:
        return {
            "toolCallId": tool_call_id,
            "skillId": skill.skill_id,
            "skillVersion": skill.version,
            "skillHash": skill.hash,
            "agentId": agent_id,
            "occurredAt": _utc_now(),
        }


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z")


default_skill_manager = SkillManager()
