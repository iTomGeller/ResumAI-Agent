from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional

from app.runtime.models import AgentOutput

BLACKBOARD_KEYS = [
    "resumeFacts", "jdRequirements", "technicalFindings", "projectFindings",
    "risks", "evidence", "conflicts", "recommendations", "agentOutputs",
    "completedTasks", "pendingTasks", "artifacts",
]

_SECTION_READ_MAP: Dict[str, List[str]] = {
    # Each agent reads only what it needs (spec §9.1) — never other agents'
    # hidden reasoning, only structured outputs.
    "ResumeParserAgent": ["artifacts"],
    "JDAnalysisAgent": ["resumeFacts"],
    "TechAgent": ["resumeFacts", "jdRequirements"],
    "ProjectAgent": ["resumeFacts", "jdRequirements"],
    "RiskAgent": ["resumeFacts"],
    "EvidenceAgent": ["resumeFacts", "jdRequirements", "technicalFindings",
                      "projectFindings", "risks"],
    "ReportAgent": ["resumeFacts", "jdRequirements", "technicalFindings",
                    "projectFindings", "risks", "evidence", "conflicts",
                    "recommendations"],
    "ResumeOptimizeAgent": ["resumeFacts", "projectFindings", "evidence"],
    "InterviewQuestionAgent": ["technicalFindings", "projectFindings", "risks",
                               "conflicts", "evidence"],
    "CoordinatorAgent": BLACKBOARD_KEYS,
}


class SharedState:
    """Run-level blackboard. Writes are append/merge only — an agent can add
    findings and flag conflicts, but silent overwrites are impossible."""

    def __init__(self) -> None:
        self.data: Dict[str, Any] = {
            "resumeFacts": {},
            "jdRequirements": {},
            "technicalFindings": [],
            "projectFindings": [],
            "risks": [],
            "evidence": [],
            "conflicts": [],
            "recommendations": [],
            "agentOutputs": [],
            "completedTasks": [],
            "pendingTasks": [],
            "artifacts": {},
        }

    # ---------- writes ----------

    def apply_output(self, output: AgentOutput) -> List[str]:
        """Merge an agent output; returns conflict descriptions (if any)."""
        conflicts: List[str] = []
        self.data["agentOutputs"].append(output.model_dump())
        payload_map = {
            "resume_facts": "resumeFacts",
            "jd_requirements": "jdRequirements",
            "technical_findings": "technicalFindings",
            "project_findings": "projectFindings",
            "risks": "risks",
            "evidence": "evidence",
            "recommendations": "recommendations",
        }
        for claim in output.claims:
            target = payload_map.get(str(claim.get("section") or ""), None)
            value = claim.get("value")
            if target is None or value is None:
                continue
            if isinstance(self.data[target], dict):
                if isinstance(value, dict):
                    for key, incoming in value.items():
                        existing = self.data[target].get(key)
                        if existing is not None and _differs(existing, incoming):
                            conflict = {
                                "section": target, "key": key,
                                "existing": existing, "incoming": incoming,
                                "byAgent": output.agentId,
                                "at": time.time(),
                            }
                            self.data["conflicts"].append(conflict)
                            conflicts.append(f"{target}.{key}")
                        else:
                            self.data[target][key] = incoming
            elif isinstance(self.data[target], list):
                entries = value if isinstance(value, list) else [value]
                for entry in entries:
                    normalized = entry if isinstance(entry, dict) else {"text": str(entry)}
                    normalized.setdefault("byAgent", output.agentId)
                    self.data[target].append(normalized)
        for evidence in output.evidence:
            if isinstance(evidence, dict):
                evidence.setdefault("byAgent", output.agentId)
                self.data["evidence"].append(evidence)
        return conflicts

    def add_conflict(self, description: Dict[str, Any]) -> None:
        self.data["conflicts"].append(description)

    def complete_task(self, task: str) -> None:
        if task in self.data["pendingTasks"]:
            self.data["pendingTasks"].remove(task)
        if task not in self.data["completedTasks"]:
            self.data["completedTasks"].append(task)

    def set_pending(self, tasks: List[str]) -> None:
        self.data["pendingTasks"] = [t for t in tasks if t not in self.data["completedTasks"]]

    def put_artifact(self, key: str, value: Any) -> None:
        self.data["artifacts"][key] = value

    # ---------- reads ----------

    def view_for(self, agent_id: str, *, max_chars: int = 9000) -> str:
        sections = _SECTION_READ_MAP.get(agent_id, ["resumeFacts", "jdRequirements"])
        view: Dict[str, Any] = {}
        for section in sections:
            value = self.data.get(section)
            if not value:
                continue
            view[section] = value
        text = json.dumps(view, ensure_ascii=False, default=str)
        if len(text) > max_chars:
            # shrink lists first, keep newest entries
            for section in list(view.keys()):
                if isinstance(view[section], list) and len(view[section]) > 6:
                    view[section] = view[section][-6:]
            text = json.dumps(view, ensure_ascii=False, default=str)[:max_chars]
        return text

    def claims_for_verification(self, limit: int = 30) -> List[Dict[str, Any]]:
        claims: List[Dict[str, Any]] = []
        for section in ("technicalFindings", "projectFindings", "risks", "recommendations"):
            for entry in self.data.get(section, []):
                if isinstance(entry, dict):
                    text = str(entry.get("text") or entry.get("finding")
                               or entry.get("detail") or "")
                    if text:
                        claims.append({
                            "text": text[:300],
                            "evidence": str(entry.get("evidence") or "")[:300],
                            "section": section,
                            "byAgent": entry.get("byAgent", "unknown"),
                        })
        return claims[:limit]

    def evidence_support_ratio(self) -> Optional[float]:
        verified = [e for e in self.data.get("evidence", [])
                    if isinstance(e, dict) and e.get("verified") is not None]
        if not verified:
            return None
        supported = sum(1 for e in verified if e.get("verified"))
        return round(supported / len(verified), 3)

    def snapshot(self) -> Dict[str, Any]:
        return json.loads(json.dumps(self.data, ensure_ascii=False, default=str))

    def restore(self, data: Dict[str, Any]) -> None:
        """Rehydrate the blackboard from a RunExecutionSnapshot."""
        if not isinstance(data, dict):
            return
        for key in BLACKBOARD_KEYS:
            if key in data:
                self.data[key] = data[key]

    def merge_parallel(self, outputs: List[AgentOutput]) -> List[str]:
        """Merge outputs produced by parallel agents against read-only
        snapshots. Same-key disagreements become conflicts, never overwrites."""
        conflicts: List[str] = []
        for output in outputs:
            conflicts.extend(self.apply_output(output))
        return conflicts


def _differs(a: Any, b: Any) -> bool:
    try:
        return json.dumps(a, sort_keys=True, ensure_ascii=False) \
            != json.dumps(b, sort_keys=True, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(a) != str(b)
