from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional

from app.runtime.models import AgentOutput

# Canonical artifact keys stored exclusively under state.data["artifacts"].
CANONICAL_ARTIFACT_KEYS = [
    "resumeFacts",
    "jdRequirements",
    "technicalFindings",
    "projectFindings",
    "risks",
    "evidence",
    "conflicts",
    "recommendations",
    "parsedResume",
    "effectiveJd",
    "jdMatches",
    "jdCoverage",
    "timelineCheck",
    "mcpEvidence",
    "mcpContext",
    "finalReport",
    "inputPresence",
]

# Legacy blackboard top-level keys (migrated once on restore; new writes go
# only through apply_artifacts / apply_output into artifacts).
BLACKBOARD_KEYS = [
    "resumeFacts", "jdRequirements", "technicalFindings", "projectFindings",
    "risks", "evidence", "conflicts", "recommendations", "agentOutputs",
    "completedTasks", "pendingTasks", "artifacts",
]

_SECTION_READ_MAP: Dict[str, List[str]] = {
    # Each agent reads only what it needs (spec §9.1) — never other agents'
    # hidden reasoning, only structured outputs from canonical artifacts.
    "TechAgent": ["resumeFacts", "jdRequirements", "effectiveJd", "jdCoverage",
                  "inputPresence"],
    "ProjectAgent": ["resumeFacts", "jdRequirements", "effectiveJd", "inputPresence"],
    "RiskAgent": ["resumeFacts", "timelineCheck", "inputPresence"],
    "EvidenceAgent": ["resumeFacts", "jdRequirements", "mcpEvidence",
                      "projectFindings", "technicalFindings", "risks",
                      "inputPresence"],
    "ReportAgent": ["resumeFacts", "jdRequirements", "mcpEvidence",
                    "technicalFindings",                     "projectFindings", "risks", "evidence", "conflicts",
                    "projectFindings", "risks", "evidence", "conflicts",
                    "recommendations", "jdCoverage", "timelineCheck",
                    "effectiveJd", "inputPresence"],
    "CoordinatorAgent": CANONICAL_ARTIFACT_KEYS,
}

# Map claim.section → canonical artifact key.
_CLAIM_SECTION_MAP = {
    "resume_facts": "resumeFacts",
    "jd_requirements": "jdRequirements",
    "technical_findings": "technicalFindings",
    "project_findings": "projectFindings",
    "risks": "risks",
    "evidence": "evidence",
    "recommendations": "recommendations",
}

# AgentOutput.type → default artifact key when typed artifacts dict is empty.
_TYPE_ARTIFACT_MAP = {
    "resume_facts": "resumeFacts",
    "jd_requirements": "jdRequirements",
    "technical_findings": "technicalFindings",
    "project_findings": "projectFindings",
    "risks": "risks",
    "evidence": "evidence",
    "recommendations": "recommendations",
}

# Keys that must remain dict-shaped. Specialist LLM outputs sometimes emit a
# fact-list for resumeFacts/jdRequirements; clobbering the parse dict with a
# list makes inspect_signals crash (`list.get`) and Evidence/Report abort.
_DICT_SHAPED_KEYS = frozenset({
    "resumeFacts",
    "jdRequirements",
    "inputPresence",
    "parsedResume",
    "finalReport",
    "jdCoverage",
    "timelineCheck",
})

# These sections are consumed with list semantics throughout the runtime.
# Models may still wrap them in a presentation container such as
# {"title": "...", "findings": [...]} or {"items": [...]}.  Keep the
# canonical store shape stable and unwrap those containers at the boundary.
_LIST_SHAPED_KEYS = frozenset({
    "technicalFindings",
    "projectFindings",
    "risks",
    "evidence",
    "conflicts",
    "recommendations",
})

_LIST_CONTAINER_FIELDS: Dict[str, tuple[str, ...]] = {
    "technicalFindings": ("findings", "technicalFindings", "items"),
    "projectFindings": ("findings", "projectFindings", "items"),
    "risks": ("risks", "items"),
    "evidence": ("evidence", "items"),
    "conflicts": ("conflicts", "items"),
    "recommendations": ("recommendations", "items"),
}


class SharedState:
    """Run-level blackboard with a single Canonical Artifact Store.

    All structured findings live under ``data["artifacts"]``. Top-level
    ``resumeFacts`` / ``jdRequirements`` / … are read-through mirrors kept
    only for one compatibility cycle of old checkpoints; new writes never
    dual-write to both places independently.
    """

    def __init__(self) -> None:
        self.data: Dict[str, Any] = {
            "agentOutputs": [],
            "completedTasks": [],
            "pendingTasks": [],
            "artifacts": {
                "resumeFacts": {},
                "jdRequirements": {},
                "technicalFindings": [],
                "projectFindings": [],
                "risks": [],
                "evidence": [],
                "conflicts": [],
                "recommendations": [],
                "inputPresence": {},
            },
        }
        self._sync_legacy_mirrors()

    # ---------- writes ----------

    def apply_artifacts(self, artifacts: Dict[str, Any], *,
                        by_agent: str = "system") -> None:
        """Write/merge into the canonical artifact store only."""
        if not artifacts:
            return
        store = self.data.setdefault("artifacts", {})
        for key, value in artifacts.items():
            if value is None:
                continue
            existing = store.get(key)
            if key in _LIST_SHAPED_KEYS:
                current = _coerce_list_shaped_value(
                    key, existing, by_agent) if existing is not None else []
                current.extend(_coerce_list_shaped_value(
                    key, value, by_agent))
                store[key] = current
            elif isinstance(existing, dict) and isinstance(value, dict):
                merged = dict(existing)
                merged.update(value)
                store[key] = merged
            elif isinstance(existing, list) and isinstance(value, list):
                # A caller may have mutated the canonical list obtained from
                # artifact() and passed that exact object back. Iterating it
                # while appending to itself never terminates; the mutation is
                # already present, so the write is a no-op.
                if existing is value:
                    continue
                for entry in value:
                    normalized = entry if isinstance(entry, dict) else {"text": str(entry)}
                    normalized.setdefault("byAgent", by_agent)
                    existing.append(normalized)
            elif key in _DICT_SHAPED_KEYS and isinstance(existing, dict) \
                    and existing and not isinstance(value, dict):
                # Keep non-empty structured dict; list/scalar clobber breaks .get.
                store.setdefault("conflicts", []).append({
                    "section": key, "key": "_",
                    "existing": existing, "incoming": value,
                    "byAgent": by_agent, "at": time.time(),
                    "reason": "dict_shaped_artifact_type_clash",
                })
            elif key in _DICT_SHAPED_KEYS and isinstance(value, list):
                store[key] = _coerce_dict_shaped_list(value, by_agent)
            else:
                if isinstance(value, list):
                    normalized_list = []
                    for entry in value:
                        if isinstance(entry, dict):
                            entry.setdefault("byAgent", by_agent)
                            normalized_list.append(entry)
                        else:
                            normalized_list.append(
                                {"text": str(entry), "byAgent": by_agent})
                    store[key] = normalized_list
                else:
                    store[key] = value
        self._sync_legacy_mirrors()

    def put_artifact(self, key: str, value: Any) -> None:
        """Compatibility wrapper — always writes to canonical store."""
        self.apply_artifacts({key: value})

    def apply_output(self, output: AgentOutput) -> List[str]:
        """Merge an agent output into canonical artifacts; returns conflicts."""
        conflicts: List[str] = []
        self.data["agentOutputs"].append(output.model_dump())
        store = self.data.setdefault("artifacts", {})

        # Preferred path: typed artifacts dict on AgentOutput.
        typed = getattr(output, "artifacts", None) or {}
        if isinstance(typed, dict) and typed:
            for key, value in typed.items():
                if value is None:
                    continue
                conflicts.extend(self._merge_into(store, key, value, output.agentId))
            for evidence in output.evidence:
                if isinstance(evidence, dict):
                    conflicts.extend(self._merge_into(
                        store, "evidence", evidence, output.agentId))
            self._sync_legacy_mirrors()
            return conflicts

        # Legacy claim.section/value path (still accepted for one cycle).
        for claim in output.claims:
            target = _CLAIM_SECTION_MAP.get(str(claim.get("section") or ""), None)
            value = claim.get("value")
            if target is None or value is None:
                continue
            conflicts.extend(self._merge_into(store, target, value, output.agentId))
        for evidence in output.evidence:
            if isinstance(evidence, dict):
                conflicts.extend(self._merge_into(
                    store, "evidence", evidence, output.agentId))
        self._sync_legacy_mirrors()
        return conflicts

    def _merge_into(self, store: Dict[str, Any], target: str, value: Any,
                    agent_id: str) -> List[str]:
        conflicts: List[str] = []
        existing = store.get(target)
        if target in _LIST_SHAPED_KEYS:
            current = _coerce_list_shaped_value(
                target, existing, agent_id) if existing is not None else []
            current.extend(_coerce_list_shaped_value(
                target, value, agent_id))
            store[target] = current
            return conflicts
        if existing is None or existing == {} or existing == []:
            if target in _DICT_SHAPED_KEYS and isinstance(value, list):
                store[target] = _coerce_dict_shaped_list(value, agent_id)
                return conflicts
            if isinstance(value, dict):
                store[target] = dict(value)
            elif isinstance(value, list):
                entries = []
                for entry in value:
                    normalized = entry if isinstance(entry, dict) else {"text": str(entry)}
                    normalized.setdefault("byAgent", agent_id)
                    entries.append(normalized)
                store[target] = entries
            else:
                store[target] = value
            return conflicts

        if isinstance(existing, dict) and isinstance(value, dict):
            for key, incoming in value.items():
                prior = existing.get(key)
                if prior is not None and _differs(prior, incoming):
                    conflict = {
                        "section": target, "key": key,
                        "existing": prior, "incoming": incoming,
                        "byAgent": agent_id, "at": time.time(),
                    }
                    store.setdefault("conflicts", []).append(conflict)
                    conflicts.append(f"{target}.{key}")
                else:
                    existing[key] = incoming
        elif isinstance(existing, dict) and not isinstance(value, dict):
            # Production bug: ProjectAgent emitted resumeFacts as a fact-list and
            # replaced the parse dict → Evidence/Report inspect_signals blew up
            # with AttributeError: 'list' object has no attribute 'get'.
            conflict = {
                "section": target, "key": "_",
                "existing": existing, "incoming": value,
                "byAgent": agent_id, "at": time.time(),
                "reason": "dict_shaped_artifact_type_clash",
            }
            store.setdefault("conflicts", []).append(conflict)
            conflicts.append(target)
            # Keep the structured dict; never install a list on dict-shaped keys.
        elif isinstance(existing, list):
            if target in _DICT_SHAPED_KEYS and isinstance(value, dict):
                # Heal a previously-corrupted list-shaped dict key.
                store[target] = dict(value)
                return conflicts
            entries = value if isinstance(value, list) else [value]
            for entry in entries:
                normalized = entry if isinstance(entry, dict) else {"text": str(entry)}
                normalized.setdefault("byAgent", agent_id)
                existing.append(normalized)
        else:
            if _differs(existing, value):
                conflict = {
                    "section": target, "key": "_",
                    "existing": existing, "incoming": value,
                    "byAgent": agent_id, "at": time.time(),
                }
                store.setdefault("conflicts", []).append(conflict)
                conflicts.append(target)
            if not (target in _DICT_SHAPED_KEYS and isinstance(value, list)):
                store[target] = value
        return conflicts

    def add_conflict(self, description: Dict[str, Any]) -> None:
        store = self.data.setdefault("artifacts", {})
        store.setdefault("conflicts", []).append(description)
        self._sync_legacy_mirrors()

    def complete_task(self, task: str) -> None:
        if task in self.data["pendingTasks"]:
            self.data["pendingTasks"].remove(task)
        if task not in self.data["completedTasks"]:
            self.data["completedTasks"].append(task)

    def set_pending(self, tasks: List[str]) -> None:
        self.data["pendingTasks"] = [t for t in tasks if t not in self.data["completedTasks"]]

    def set_input_presence(self, *, resume_chars: int = 0, jd_chars: int = 0,
                           has_jd_matches: bool = False) -> None:
        self.apply_artifacts({
            "inputPresence": {
                "resumeChars": resume_chars,
                "jdChars": jd_chars,
                "hasJdMatches": has_jd_matches,
                "resumePresent": resume_chars > 0,
                "jdPresent": jd_chars > 0 or has_jd_matches,
            }
        })

    # ---------- reads ----------

    def artifact(self, key: str, default: Any = None) -> Any:
        return (self.data.get("artifacts") or {}).get(key, default)

    def artifacts(self) -> Dict[str, Any]:
        return self.data.setdefault("artifacts", {})

    def view_for(self, agent_id: str, *, max_chars: int = 9000) -> str:
        """Build agent-visible digest exclusively from canonical artifacts."""
        sections = _SECTION_READ_MAP.get(
            agent_id, ["resumeFacts", "jdRequirements", "inputPresence"])
        store = self.data.get("artifacts") or {}
        view: Dict[str, Any] = {}
        for section in sections:
            value = store.get(section)
            if value is None or value == {} or value == []:
                continue
            if section in {"mcpEvidence", "mcpContext"}:
                value = self._compact_mcp_entries(value)
            view[section] = value
        # Always surface inputPresence so agents cannot claim "原文缺失"
        # when resume/JD text was actually provided upstream.
        presence = store.get("inputPresence") or {}
        if presence:
            view["inputPresence"] = presence
        text = json.dumps(view, ensure_ascii=False, default=str)
        if len(text) > max_chars:
            for section in list(view.keys()):
                if isinstance(view[section], list) and len(view[section]) > 6:
                    view[section] = view[section][-6:]
            text = json.dumps(view, ensure_ascii=False, default=str)[:max_chars]
        return text

    @staticmethod
    def _compact_mcp_entries(value: Any) -> Any:
        """Keep external provenance visible without flooding later prompts.

        MCP bodies can be many kilobytes. Evidence/Report need the transport
        outcome, source URLs and a bounded content preview to arbitrate claims;
        they do not need the complete remote page a second time.
        """
        if not isinstance(value, list):
            return value
        compact: List[Dict[str, Any]] = []
        for raw in value[-6:]:
            if not isinstance(raw, dict):
                continue
            result = raw.get("result") if isinstance(raw.get("result"), dict) else {}
            compact.append({
                "tool": raw.get("tool"),
                "status": raw.get("status"),
                "byAgent": raw.get("byAgent"),
                "sourceUrls": list(raw.get("sourceUrls") or [])[:4],
                "sourceBacked": raw.get("sourceBacked"),
                "candidateFactEligible": raw.get("candidateFactEligible"),
                "resultSuccess": result.get("success"),
                "contentPreview": str(result.get("text") or "")[:1600],
            })
        return compact

    def claims_for_verification(self, limit: int = 30) -> List[Dict[str, Any]]:
        store = self.data.get("artifacts") or {}
        claims: List[Dict[str, Any]] = []
        for section in ("technicalFindings", "projectFindings", "risks", "recommendations"):
            for entry in store.get(section, []) or []:
                if isinstance(entry, dict):
                    text = str(entry.get("text") or entry.get("finding")
                               or entry.get("claim") or entry.get("detail") or "")
                    if text:
                        claims.append({
                            "text": text[:300],
                            "evidence": str(entry.get("evidence") or "")[:300],
                            "section": section,
                            "byAgent": entry.get("byAgent", "unknown"),
                        })
        return claims[:limit]

    def evidence_support_ratio(self) -> Optional[float]:
        store = self.data.get("artifacts") or {}
        verified = [e for e in (store.get("evidence") or [])
                    if isinstance(e, dict) and e.get("verified") is not None]
        if not verified:
            return None
        supported = sum(1 for e in verified if e.get("verified"))
        return round(supported / len(verified), 3)

    def snapshot(self) -> Dict[str, Any]:
        return json.loads(json.dumps(self.data, ensure_ascii=False, default=str))

    def restore(self, data: Dict[str, Any]) -> None:
        """Rehydrate from checkpoint; migrate legacy top-level fields once."""
        if not isinstance(data, dict):
            return
        for key in ("agentOutputs", "completedTasks", "pendingTasks"):
            if key in data:
                self.data[key] = data[key]
        artifacts = data.get("artifacts")
        if isinstance(artifacts, dict):
            self.data["artifacts"] = dict(artifacts)
        else:
            self.data["artifacts"] = {}
        # One-shot migration: lift legacy top-level blackboard sections into
        # canonical artifacts when the checkpoint still has dual storage.
        for key in CANONICAL_ARTIFACT_KEYS:
            top = data.get(key)
            if top is None or top == {} or top == []:
                continue
            existing = self.data["artifacts"].get(key)
            if existing is None or existing == {} or existing == []:
                self.data["artifacts"][key] = top
        # Heal checkpoints created before list-shaped artifact normalization.
        # This also makes resume-from-checkpoint safe after a model emitted a
        # wrapper dict for findings/evidence.
        for key in _LIST_SHAPED_KEYS:
            if key in self.data["artifacts"]:
                self.data["artifacts"][key] = _coerce_list_shaped_value(
                    key, self.data["artifacts"][key], "checkpoint")
        self._sync_legacy_mirrors()

    def merge_parallel(self, outputs: List[AgentOutput]) -> List[str]:
        """Merge outputs produced by parallel agents against read-only
        snapshots. Same-key disagreements become conflicts, never overwrites."""
        conflicts: List[str] = []
        for output in outputs:
            conflicts.extend(self.apply_output(output))
        return conflicts

    def _sync_legacy_mirrors(self) -> None:
        """Expose commonly-read sections at top-level for one compatibility
        cycle so older coordinator/executor call sites keep working while
        they migrate to artifact()/artifacts()."""
        store = self.data.setdefault("artifacts", {})
        for key in ("resumeFacts", "jdRequirements", "technicalFindings",
                    "projectFindings", "risks", "evidence", "conflicts",
                    "recommendations"):
            if key in store:
                self.data[key] = store[key]
            elif key not in self.data:
                self.data[key] = {} if key in ("resumeFacts", "jdRequirements") else []


def _coerce_dict_shaped_list(value: List[Any], agent_id: str) -> Dict[str, Any]:
    """Fold a fact-list into a dict so .get readers never see a bare list."""
    items: List[Dict[str, Any]] = []
    for entry in value:
        if isinstance(entry, dict):
            normalized = dict(entry)
            normalized.setdefault("byAgent", agent_id)
            items.append(normalized)
        else:
            items.append({"text": str(entry), "byAgent": agent_id})
    return {"items": items, "source": "coerced_list", "byAgent": agent_id}


def _coerce_list_shaped_value(target: str, value: Any,
                              agent_id: str) -> List[Dict[str, Any]]:
    """Normalize a list artifact without discarding model wrapper contents."""
    if value is None:
        return []
    wrapper_agent = agent_id
    if isinstance(value, list):
        entries = value
    elif isinstance(value, dict):
        wrapper_agent = str(value.get("byAgent") or agent_id)
        entries = None
        for field in _LIST_CONTAINER_FIELDS.get(target, ("items",)):
            candidate = value.get(field)
            if isinstance(candidate, list):
                entries = candidate
                break
        if entries is None:
            entries = [value]
    else:
        entries = [value]

    normalized_entries: List[Dict[str, Any]] = []
    for entry in entries:
        if isinstance(entry, dict):
            normalized = dict(entry)
            normalized.setdefault("byAgent", wrapper_agent)
        else:
            normalized = {"text": str(entry), "byAgent": wrapper_agent}
        normalized_entries.append(normalized)
    return normalized_entries


def _differs(a: Any, b: Any) -> bool:
    try:
        return json.dumps(a, sort_keys=True, ensure_ascii=False) \
            != json.dumps(b, sort_keys=True, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(a) != str(b)
