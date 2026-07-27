from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
import hashlib
from typing import Any, Dict, List, Literal, Optional, Tuple

import httpx
from pydantic import BaseModel

from app.config import settings

logger = logging.getLogger(__name__)

# Canonical taxonomy. Legacy names are accepted only at the API boundary and
# are normalized before filtering, prompting or trace emission.
CANONICAL_TYPES = frozenset({"SEMANTIC", "EPISODIC", "PROCEDURAL", "WORKING"})
LEGACY_TYPE_MAP = {
    "CONVERSATION": "WORKING",
    "SHORT_TERM": "WORKING",
    "PREFERENCE": "SEMANTIC",
    "USER_PREFERENCE": "SEMANTIC",
    "HR_FEEDBACK": "SEMANTIC",
    "DOMAIN": "SEMANTIC",
    "FAILURE": "EPISODIC",
}
SPECIALIST_TYPES = frozenset({"SEMANTIC", "EPISODIC", "PROCEDURAL"})
COORDINATOR_TYPES = CANONICAL_TYPES
CONTROL_PLANE_ERROR_CODES = frozenset({
    "ORPHANED_ON_RESTART", "RUNTIME_START_FAILED", "START_STUCK",
})
FAILURE_CONSUMERS = frozenset({
    "CoordinatorAgent", "Coordinator", "PolicyEvolution", "POLICY_EVOLUTION",
    "PolicyLab",
})
REPORT_OR_RISK = frozenset({"ReportAgent", "RiskAgent"})
_BENCHMARK_SOURCE_RE = re.compile(r"^exp\d*_benchmark$", re.IGNORECASE)


class MemoryDecision(BaseModel):
    """Per-hit USED/IGNORED evidence persisted to run_memory_usage."""

    memoryId: str
    consumerAgent: str
    rank: Optional[int] = None
    vectorScore: Optional[float] = None
    lexicalScore: Optional[float] = None
    recencyScore: Optional[float] = None
    finalScore: Optional[float] = None
    decision: Literal["USED", "IGNORED"] = "USED"
    ignoredReason: Optional[str] = None
    memoryType: Optional[str] = None
    taxonomy: Optional[str] = None
    namespace: Optional[str] = None
    reason: Optional[str] = None
    occurredAt: Optional[str] = None


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def canonical_taxonomy(raw_type: Any) -> str:
    value = str(raw_type or "").strip().upper()
    return LEGACY_TYPE_MAP.get(value, value)


def _safe_namespace(hit: Dict[str, Any]) -> str:
    supplied = str(hit.get("namespace") or "").strip()
    if supplied:
        return supplied
    scope = str(hit.get("ownerScope") or hit.get("scope") or "UNKNOWN").upper()
    owner = (hit.get("runId") if scope == "RUN"
             else hit.get("conversationId") if scope == "CONVERSATION"
             else hit.get("userId") if scope == "USER"
             else "global")
    if scope == "GLOBAL":
        return "global"
    if owner:
        digest = hashlib.sha256(str(owner).encode("utf-8")).hexdigest()[:12]
        return f"{scope.lower()}/{digest}"
    return scope.lower()


def allows_failure(consumer_agent: Optional[str]) -> bool:
    if not consumer_agent:
        return False
    if consumer_agent in FAILURE_CONSUMERS:
        return True
    key = consumer_agent.replace("-", "").replace("_", "").lower()
    return key == "coordinatoragent" or key.startswith("policy")


def is_benchmark_source(source: Optional[str]) -> bool:
    if not source:
        return False
    return bool(_BENCHMARK_SOURCE_RE.match(str(source).strip()))


def is_control_plane_memory(hit: Dict[str, Any]) -> bool:
    source = str(hit.get("source") or "")
    if source.lower() == "control_plane":
        return True
    structured = hit.get("structuredContent") or {}
    if isinstance(structured, dict):
        code = str(structured.get("errorCode") or "").upper()
        if code in CONTROL_PLANE_ERROR_CODES:
            return True
        if str(structured.get("category") or "").upper() == "CONTROL_PLANE":
            return True
    content = str(hit.get("content") or "")
    return any(code in content for code in CONTROL_PLANE_ERROR_CODES)


def is_failure_episode(hit: Dict[str, Any]) -> bool:
    raw_type = str(hit.get("storedType") or hit.get("type") or "").upper()
    source = str(hit.get("source") or "").lower()
    source_id = str(hit.get("sourceId") or "")
    structured = hit.get("structuredContent") or {}
    return (
        raw_type == "FAILURE"
        or source in {"control_plane", "failed_run"}
        or source_id.startswith("failure:")
        or (isinstance(structured, dict)
            and (str(structured.get("outcome") or "").upper() == "FAILURE"
                 or str(structured.get("category") or "").upper() == "CONTROL_PLANE"))
    )


def allowed_types_for(consumer_agent: Optional[str]) -> frozenset[str]:
    key = str(consumer_agent or "Specialist").replace("-", "").replace("_", "").lower()
    if key.startswith("policy"):
        return frozenset({"PROCEDURAL", "EPISODIC"})
    if key in {"coordinator", "coordinatoragent"}:
        return COORDINATOR_TYPES
    if "resumeparser" in key or "conversation" in key:
        return frozenset({"SEMANTIC", "WORKING"})
    if "jdanalysis" in key or key == "jdagent":
        return frozenset({"SEMANTIC", "PROCEDURAL", "WORKING"})
    if "report" in key or "risk" in key:
        return frozenset({"EPISODIC", "SEMANTIC", "PROCEDURAL"})
    return SPECIALIST_TYPES


def _score_fields(hit: Dict[str, Any]) -> Dict[str, Optional[float]]:
    relevance = hit.get("relevance")
    vector = lexical = recency = final = None
    if isinstance(relevance, dict):
        vector = _as_float(relevance.get("semantic") or relevance.get("vector"))
        lexical = _as_float(relevance.get("lexical"))
        recency = _as_float(relevance.get("recency"))
        final = _as_float(relevance.get("fused") or relevance.get("final"))
    if final is None:
        final = _as_float(hit.get("score") or hit.get("finalScore") or hit.get("relevance"))
    if vector is None:
        vector = _as_float(hit.get("vectorScore"))
    if lexical is None:
        lexical = _as_float(hit.get("lexicalScore"))
    if recency is None:
        recency = _as_float(hit.get("recencyScore"))
    return {
        "vectorScore": vector,
        "lexicalScore": lexical,
        "recencyScore": recency,
        "finalScore": final,
    }


def _as_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def decisions_from_hits(
    used: List[Dict[str, Any]],
    ignored: List[Dict[str, Any]],
    consumer_agent: str,
) -> List[MemoryDecision]:
    """Build MemoryDecision rows with score breakdown for persistence."""
    rows: List[MemoryDecision] = []
    rank = 0
    for hit in used:
        mid = str(hit.get("memoryId") or "").strip()
        if not mid:
            continue
        rank += 1
        scores = _score_fields(hit)
        taxonomy = canonical_taxonomy(
            hit.get("taxonomy") or hit.get("memoryType") or hit.get("type"))
        rows.append(MemoryDecision(
            memoryId=mid,
            consumerAgent=consumer_agent,
            rank=rank,
            vectorScore=scores["vectorScore"],
            lexicalScore=scores["lexicalScore"],
            recencyScore=scores["recencyScore"],
            finalScore=scores["finalScore"],
            decision="USED",
            ignoredReason=None,
            memoryType=taxonomy,
            taxonomy=taxonomy,
            namespace=_safe_namespace(hit),
            reason=str(hit.get("reason") or hit.get("selectionReason")
                       or "selected_for_agent_context"),
            occurredAt=str(hit.get("occurredAt") or _utc_now_iso()),
        ))
    for hit in ignored:
        mid = str(hit.get("memoryId") or "").strip()
        if not mid:
            continue
        rank += 1
        scores = _score_fields(hit)
        taxonomy = canonical_taxonomy(
            hit.get("taxonomy") or hit.get("memoryType") or hit.get("type"))
        rows.append(MemoryDecision(
            memoryId=mid,
            consumerAgent=consumer_agent,
            rank=rank,
            vectorScore=scores["vectorScore"],
            lexicalScore=scores["lexicalScore"],
            recencyScore=scores["recencyScore"],
            finalScore=scores["finalScore"],
            decision="IGNORED",
            ignoredReason=str(hit.get("ignoredReason") or "") or None,
            memoryType=taxonomy,
            taxonomy=taxonomy,
            namespace=_safe_namespace(hit),
            reason=str(hit.get("ignoredReason") or "excluded_by_consumer_policy"),
            occurredAt=str(hit.get("occurredAt") or _utc_now_iso()),
        ))
    return rows


def filter_hits_for_consumer(
    hits: List[Dict[str, Any]],
    consumer_agent: str,
    *,
    include_benchmark: bool = False,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Return (used, ignored) with ignoredReason on each ignored row.

    Defense-in-depth for Report/Risk: control-plane FAILURE codes are always
    dropped even if a buggy search returned them.
    """
    allowed_types = allowed_types_for(consumer_agent)
    evaluation_safe = not allows_failure(consumer_agent)
    used: List[Dict[str, Any]] = []
    ignored: List[Dict[str, Any]] = []

    for hit in hits:
        reason: Optional[str] = None
        hit_type = canonical_taxonomy(
            hit.get("taxonomy") or hit.get("memoryType") or hit.get("type"))
        scope = str(hit.get("ownerScope") or hit.get("scope") or "").upper()
        source = str(hit.get("source") or "")

        if hit_type and hit_type not in allowed_types:
            reason = f"type_not_allowed_for_{consumer_agent}"
        elif is_control_plane_memory(hit) and not allows_failure(consumer_agent):
            reason = "control_plane_not_injectable"
        elif is_failure_episode(hit) and not allows_failure(consumer_agent):
            reason = "failure_reserved_for_coordinator"
        elif scope == "GLOBAL" and hit_type != "PROCEDURAL" and not (
                allows_failure(consumer_agent) and is_failure_episode(hit)):
            reason = f"scope_{scope}_excluded_for_{consumer_agent}"
        elif scope == "RUN" and hit_type != "WORKING":
            reason = "run_scope_requires_working_memory"
        elif not include_benchmark and is_benchmark_source(source):
            reason = "benchmark_source_excluded"
        elif is_control_plane_memory(hit) and (
                evaluation_safe or consumer_agent in REPORT_OR_RISK):
            reason = "control_plane_not_injectable"
        elif consumer_agent in REPORT_OR_RISK and is_control_plane_memory(hit):
            reason = "control_plane_blocked_for_report_risk"

        if reason:
            ignored.append({
                **hit,
                "type": hit_type,
                "memoryType": hit_type,
                "taxonomy": hit_type,
                "namespace": _safe_namespace(hit),
                "occurredAt": hit.get("occurredAt") or _utc_now_iso(),
                "consumerAgent": consumer_agent,
                "used": False,
                "ignoredReason": reason,
            })
        else:
            used.append({
                **hit,
                "type": hit_type,
                "memoryType": hit_type,
                "taxonomy": hit_type,
                "namespace": _safe_namespace(hit),
                "occurredAt": hit.get("occurredAt") or _utc_now_iso(),
                "consumerAgent": consumer_agent,
                "used": True,
                "ignoredReason": None,
            })
    return used, ignored


def memory_trace_entries(
    used: List[Dict[str, Any]],
    ignored: List[Dict[str, Any]],
    consumer_agent: str,
) -> List[Dict[str, Any]]:
    """Compact trace rows: memoryId/type/scope/source/consumer/used|reason."""
    rows: List[Dict[str, Any]] = []
    for hit in used + ignored:
        taxonomy = canonical_taxonomy(
            hit.get("taxonomy") or hit.get("memoryType") or hit.get("type"))
        rows.append({
            "memoryId": hit.get("memoryId"),
            "type": taxonomy,
            "memoryType": taxonomy,
            "taxonomy": taxonomy,
            "scope": hit.get("ownerScope") or hit.get("scope"),
            "namespace": _safe_namespace(hit),
            "source": hit.get("source"),
            "consumerAgent": consumer_agent,
            "used": bool(hit.get("used")),
            "ignoredReason": hit.get("ignoredReason"),
            "reason": (hit.get("ignoredReason")
                       or hit.get("reason")
                       or hit.get("selectionReason")
                       or ("selected_for_agent_context"
                           if hit.get("used") else "not_selected")),
            "confidence": hit.get("confidence"),
            "relevance": hit.get("relevance") or hit.get("score"),
            "occurredAt": hit.get("occurredAt") or _utc_now_iso(),
        })
    return rows


class MemoryClient:
    """Layered-memory access via the Java control plane (the durable owner).

    The runtime only ever sees memory the Java side scopes to this
    run/conversation/user; failures degrade to empty results, never to
    fabricated context. Callers should pass consumer_agent so the backend
    applies type/scope/source isolation.
    """

    def __init__(self, run_id: str, conversation_id: str, user_id: str) -> None:
        self.run_id = run_id
        self.conversation_id = conversation_id
        self.user_id = user_id
        self._base = settings.java_backend_url.rstrip("/")

    def _headers(self) -> Dict[str, str]:
        return {
            "Content-Type": "application/json",
            "X-Internal-Token": settings.workflow_internal_token,
        }

    async def search(self, query: str, *, types: Optional[List[str]] = None,
                     top_k: int = 5, min_confidence: float = 0.35,
                     consumer_agent: Optional[str] = None,
                     include_benchmark_sources: bool = False) -> List[Dict[str, Any]]:
        # Default specialist-safe types when caller omits them.
        effective_types = types
        if effective_types is None and not allows_failure(consumer_agent):
            effective_types = sorted(SPECIALIST_TYPES)
        body = {
            "query": query,
            "types": effective_types,
            "userId": self.user_id,
            "conversationId": self.conversation_id,
            "runId": self.run_id,
            "topK": top_k,
            "minConfidence": min_confidence,
            "consumerAgent": consumer_agent,
            "includeBenchmarkSources": include_benchmark_sources,
        }
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(
                    f"{self._base}/api/internal/agent-runs/memory/search",
                    json=body, headers=self._headers())
            if response.status_code >= 400:
                logger.info("memory search failed status=%s", response.status_code)
                return []
            hits = list(response.json().get("hits") or [])
            # Client-side consumer filter as defense in depth.
            if consumer_agent:
                used, _ignored = filter_hits_for_consumer(
                    hits, consumer_agent,
                    include_benchmark=include_benchmark_sources)
                return used
            return [
                h for h in hits
                if not is_benchmark_source(h.get("source"))
                and not is_failure_episode(h)
            ]
        except Exception as exc:  # noqa: BLE001
            logger.info("memory search unavailable: %s", exc)
            return []

    async def record_usage(
        self,
        *,
        consumer_agent: str,
        decisions: List[MemoryDecision],
    ) -> int:
        """Persist USED/IGNORED decisions to Java run_memory_usage."""
        if not decisions:
            return 0
        body = {
            "consumerAgent": consumer_agent,
            "decisions": [
                {
                    "memoryId": d.memoryId,
                    "consumerAgent": d.consumerAgent or consumer_agent,
                    "rankNo": d.rank,
                    "vectorScore": d.vectorScore,
                    "lexicalScore": d.lexicalScore,
                    "recencyScore": d.recencyScore,
                    "finalScore": d.finalScore,
                    "decision": d.decision,
                    "ignoredReason": d.ignoredReason,
                    "memoryType": d.memoryType,
                    "taxonomy": d.taxonomy,
                    "namespace": d.namespace,
                    "reason": d.reason,
                    "occurredAt": d.occurredAt or _utc_now_iso(),
                }
                for d in decisions
            ],
        }
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                response = await client.post(
                    f"{self._base}/api/internal/runs/{self.run_id}/memory-usage",
                    json=body, headers=self._headers())
            if response.status_code >= 400:
                logger.info("memory usage persist failed status=%s", response.status_code)
                return 0
            return int(response.json().get("written") or 0)
        except Exception as exc:  # noqa: BLE001
            logger.info("memory usage persist unavailable: %s", exc)
            return 0

    async def write(self, *, type_: str, owner_scope: str, content: str,
                    structured: Optional[Dict[str, Any]] = None, source: str = "model_generated",
                    source_id: Optional[str] = None, confidence: float = 0.5,
                    ttl_days: Optional[int] = None) -> Optional[str]:
        type_ = canonical_taxonomy(type_)
        if type_ == "WORKING":
            owner_scope = "RUN"
        # Candidate/user semantic facts never write into a global namespace.
        if type_ == "SEMANTIC" and owner_scope.upper() == "GLOBAL":
            owner_scope = "USER"
        body = {
            "type": type_,
            "ownerScope": owner_scope,
            "userId": self.user_id,
            "conversationId": self.conversation_id,
            "runId": self.run_id,
            "content": content,
            "structuredContent": structured or {},
            "source": source,
            "sourceId": source_id,
            "confidence": confidence,
            "ttlDays": ttl_days,
        }
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(
                    f"{self._base}/api/internal/agent-runs/memory/write",
                    json=body, headers=self._headers())
            if response.status_code >= 400:
                logger.info("memory write failed status=%s body=%s",
                            response.status_code, response.text[:150])
                return None
            return response.json().get("memoryId")
        except Exception as exc:  # noqa: BLE001
            logger.info("memory write unavailable: %s", exc)
            return None


class NullMemoryClient(MemoryClient):
    """Offline stand-in for tests/benchmarks with injectable canned memory."""

    def __init__(self, canned: Optional[List[Dict[str, Any]]] = None) -> None:
        super().__init__("test-run", "test-conv", "test-user")
        self.canned = canned or []
        self.writes: List[Dict[str, Any]] = []
        self.usage: List[Dict[str, Any]] = []

    async def search(self, query: str, *, types: Optional[List[str]] = None,
                     top_k: int = 5, min_confidence: float = 0.35,
                     consumer_agent: Optional[str] = None,
                     include_benchmark_sources: bool = False) -> List[Dict[str, Any]]:
        hits = self.canned
        if types:
            failure_only = any(str(t).upper() == "FAILURE" for t in types)
            requested = {canonical_taxonomy(t) for t in types}
            hits = [
                h for h in hits
                if canonical_taxonomy(
                    h.get("taxonomy") or h.get("memoryType") or h.get("type"))
                in requested
                and (not failure_only or is_failure_episode(h))
            ]
        if consumer_agent:
            used, _ = filter_hits_for_consumer(
                hits, consumer_agent,
                include_benchmark=include_benchmark_sources)
            hits = used
        else:
            hits = [
                h for h in hits
                if (include_benchmark_sources or not is_benchmark_source(h.get("source")))
                and not is_failure_episode(h)
            ]
        return hits[:top_k]

    async def record_usage(
        self,
        *,
        consumer_agent: str,
        decisions: List[MemoryDecision],
    ) -> int:
        payload = {
            "consumerAgent": consumer_agent,
            "decisions": [d.model_dump() for d in decisions],
        }
        self.usage.append(payload)
        return len(decisions)

    async def write(self, *, type_: str, owner_scope: str, content: str,
                    structured: Optional[Dict[str, Any]] = None, source: str = "model_generated",
                    source_id: Optional[str] = None, confidence: float = 0.5,
                    ttl_days: Optional[int] = None) -> Optional[str]:
        type_ = canonical_taxonomy(type_)
        if type_ == "WORKING":
            owner_scope = "RUN"
        if type_ == "SEMANTIC" and owner_scope.upper() == "GLOBAL":
            owner_scope = "USER"
        self.writes.append({
            "type": type_, "ownerScope": owner_scope, "content": content,
            "structured": structured or {}, "source": source,
            "sourceId": source_id, "confidence": confidence,
        })
        return f"mem-test-{len(self.writes)}"
