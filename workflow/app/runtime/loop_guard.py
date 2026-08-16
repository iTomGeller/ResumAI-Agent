from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set


@dataclass
class GuardDecision:
    triggered: bool
    kind: str = ""
    detail: str = ""
    action: str = "continue"  # continue / switch_agent / skip_step / degrade


class LoopGuard:
    """Detects unproductive loops across the whole run.

    Watches: duplicate tool signatures, semantically-repeated plans,
    observations with no new information, re-execution of completed agents,
    delegation cycles, repeated conclusions and repeated identical errors.
    Escalation: switch/skip -> degrade (never infinite looping).
    """

    def __init__(self, *, max_duplicate_tool_calls: int = 2,
                 max_repeated_plans: int = 2,
                 max_no_new_info: int = 2,
                 max_repeated_errors: int = 3,
                 max_agent_visits: int = 2) -> None:
        self.max_duplicate_tool_calls = max_duplicate_tool_calls
        self.max_repeated_plans = max_repeated_plans
        self.max_no_new_info = max_no_new_info
        self.max_repeated_errors = max_repeated_errors
        self.max_agent_visits = max_agent_visits

        self._tool_signatures: Dict[str, int] = {}
        self._plan_signatures: Dict[str, int] = {}
        self._observation_hashes: List[str] = []
        self._no_new_info_streak = 0
        self._error_signatures: Dict[str, int] = {}
        self._agent_visits: Dict[str, int] = {}
        self._completed_agents: Set[str] = set()
        self._delegation_edges: Set[tuple] = set()
        self._conclusion_hashes: Dict[str, int] = {}
        self.trips: List[GuardDecision] = []

    # ---------- recording ----------

    def record_completed_agent(self, agent_id: str) -> None:
        self._completed_agents.add(agent_id)

    def check_agent_start(self, agent_id: str) -> GuardDecision:
        self._agent_visits[agent_id] = self._agent_visits.get(agent_id, 0) + 1
        if agent_id in self._completed_agents and self._agent_visits[agent_id] > self.max_agent_visits:
            return self._trip("repeated_completed_agent",
                              f"{agent_id} 已完成却被再次调度 {self._agent_visits[agent_id]} 次",
                              "skip_step")
        return GuardDecision(False)

    def check_delegation(self, from_agent: str, to_agent: str) -> GuardDecision:
        edge = (from_agent, to_agent)
        reverse = (to_agent, from_agent)
        if reverse in self._delegation_edges and edge in self._delegation_edges:
            return self._trip("delegation_cycle",
                              f"{from_agent} <-> {to_agent} 互相委派", "skip_step")
        self._delegation_edges.add(edge)
        return GuardDecision(False)

    def check_tool_call(self, signature: str) -> GuardDecision:
        count = self._tool_signatures.get(signature, 0) + 1
        self._tool_signatures[signature] = count
        if count > self.max_duplicate_tool_calls:
            return self._trip("duplicate_tool_call",
                              f"相同工具+参数第 {count} 次调用: {signature}", "skip_step")
        return GuardDecision(False)

    def check_plan(self, plan_text: str) -> GuardDecision:
        signature = self._normalize_hash(plan_text)
        count = self._plan_signatures.get(signature, 0) + 1
        self._plan_signatures[signature] = count
        if count > self.max_repeated_plans:
            return self._trip("repeated_plan",
                              f"语义近似的计划重复 {count} 次", "switch_agent")
        return GuardDecision(False)

    def check_observation(self, observation_text: str) -> GuardDecision:
        digest = self._normalize_hash(observation_text)
        if digest in self._observation_hashes:
            self._no_new_info_streak += 1
        else:
            self._no_new_info_streak = 0
            self._observation_hashes.append(digest)
            if len(self._observation_hashes) > 64:
                self._observation_hashes.pop(0)
        if self._no_new_info_streak >= self.max_no_new_info:
            return self._trip("no_new_information",
                              f"连续 {self._no_new_info_streak} 次观察无新增信息", "skip_step")
        return GuardDecision(False)

    def check_conclusion(self, conclusion_text: str) -> GuardDecision:
        digest = self._normalize_hash(conclusion_text)
        count = self._conclusion_hashes.get(digest, 0) + 1
        self._conclusion_hashes[digest] = count
        if count > 2:
            return self._trip("repeated_conclusion",
                              "同一结论被反复生成", "degrade")
        return GuardDecision(False)

    def check_error(self, error_text: str) -> GuardDecision:
        digest = self._normalize_hash(error_text)
        count = self._error_signatures.get(digest, 0) + 1
        self._error_signatures[digest] = count
        if count >= self.max_repeated_errors:
            return self._trip("repeated_error",
                              f"同一错误连续出现 {count} 次", "degrade")
        return GuardDecision(False)

    # ---------- helpers ----------

    def _trip(self, kind: str, detail: str, action: str) -> GuardDecision:
        decision = GuardDecision(True, kind, detail, action)
        self.trips.append(decision)
        return decision

    @staticmethod
    def _normalize_hash(text: str) -> str:
        normalized = re.sub(r"[\s\d\.,;:，。；：]+", "", (text or "").lower())[:1500]
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]

    def summary(self) -> Dict[str, int]:
        kinds: Dict[str, int] = {}
        for trip in self.trips:
            kinds[trip.kind] = kinds.get(trip.kind, 0) + 1
        return kinds

    # ---------- pause/resume snapshot ----------

    def export_state(self) -> Dict[str, object]:
        return {
            "toolSignatures": dict(self._tool_signatures),
            "planSignatures": dict(self._plan_signatures),
            "errorSignatures": dict(self._error_signatures),
            "agentVisits": dict(self._agent_visits),
            "completedAgents": sorted(self._completed_agents),
            "conclusionHashes": dict(self._conclusion_hashes),
        }

    def restore_state(self, data: Dict[str, object]) -> None:
        if not isinstance(data, dict):
            return
        self._tool_signatures = dict(data.get("toolSignatures") or {})
        self._plan_signatures = dict(data.get("planSignatures") or {})
        self._error_signatures = dict(data.get("errorSignatures") or {})
        self._agent_visits = dict(data.get("agentVisits") or {})
        self._completed_agents = set(data.get("completedAgents") or [])
        self._conclusion_hashes = dict(data.get("conclusionHashes") or {})
