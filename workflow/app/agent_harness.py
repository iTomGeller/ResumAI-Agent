from __future__ import annotations

import json
import re
from datetime import date
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Tuple
from urllib.parse import urlparse


MAX_PROPOSED_TOOL_CALLS_PER_ROUND = 8


DEFAULT_AGENT_TOOL_BUDGETS: Dict[str, Dict[str, int]] = {
    "IntentAgent": {"maxToolCalls": 2, "maxRetrievalQueries": 0},
    "ResumeParseAgent": {"maxToolCalls": 2, "maxRetrievalQueries": 0},
    "JdMatchAgent": {"maxToolCalls": 3, "maxRetrievalQueries": 1},
    # A batch RAG call may contain four distinct queries. The specialist budget
    # leaves room for one or two source-bound public lookups without allowing
    # an unbounded search loop.
    "TechEvalAgent": {"maxToolCalls": 4, "maxRetrievalQueries": 6},
    "ProjectEvalAgent": {"maxToolCalls": 4, "maxRetrievalQueries": 6},
    "RiskAgent": {"maxToolCalls": 4, "maxRetrievalQueries": 4},
    "EvidenceFusionAgent": {"maxToolCalls": 2, "maxRetrievalQueries": 0},
    "ReportAgent": {"maxToolCalls": 2, "maxRetrievalQueries": 0},
}


_TIMELINE_RANGE_PATTERN = re.compile(
    r"(?P<start_year>(?:19|20)\d{2})"
    r"\s*(?:[./\-年]\s*(?P<start_month>0?[1-9]|1[0-2])\s*月?)?"
    r"(?:"
    r"\s*(?P<present_cn>至今|现在)"
    r"|\s*(?:至|到|[-–—~～])\s*(?:"
    r"(?P<end_year>(?:19|20)\d{2})"
    r"\s*(?:[./\-年]\s*(?P<end_month>0?[1-9]|1[0-2])\s*月?)?"
    r"|(?P<present_en>present|current)"
    r")"
    r")",
    re.I,
)


def validate_timeline_text(
    resume_text: str,
    *,
    reference_date: Optional[date] = None,
) -> Dict[str, Any]:
    """Parse dated intervals without pretending that ambiguous text was verified.

    Overlaps and gaps are observations for the RiskAgent, not automatic fraud
    findings: education, internships, and part-time work can legitimately
    overlap. Invalid or future ranges are explicit issues. If no interval can
    be parsed, the contract returns NOT_CHECKED instead of a fixed success.
    """

    today = reference_date or date.today()
    text = resume_text or ""
    entries: List[Dict[str, Any]] = []
    for line_number, raw_line in enumerate(text.splitlines() or [text], start=1):
        line = raw_line.strip()
        for match in _TIMELINE_RANGE_PATTERN.finditer(line):
            start_year = int(match.group("start_year"))
            start_month = int(match.group("start_month") or 1)
            is_present = bool(match.group("present_cn") or match.group("present_en"))
            end_year = today.year if is_present else int(match.group("end_year"))
            end_month = today.month if is_present else int(match.group("end_month") or 12)
            start_index = start_year * 12 + start_month - 1
            end_index = end_year * 12 + end_month - 1
            entries.append(
                {
                    "lineNumber": line_number,
                    "sourceText": line[:240],
                    "start": f"{start_year:04d}-{start_month:02d}",
                    "end": "present" if is_present else f"{end_year:04d}-{end_month:02d}",
                    "startMonthIndex": start_index,
                    "endMonthIndex": end_index,
                    "durationMonths": end_index - start_index + 1,
                    "isPresent": is_present,
                }
            )

    if not entries:
        return {
            "status": "NOT_CHECKED",
            "checked": False,
            "reason": "no_parseable_date_range",
            "referenceDate": today.isoformat(),
            "timelineEntries": [],
            "issues": [],
            "overlaps": [],
            "gaps": [],
            "riskFlag": False,
            "requiresHumanReview": True,
        }

    entries.sort(key=lambda item: (item["startMonthIndex"], item["endMonthIndex"]))
    issues: List[Dict[str, Any]] = []
    for entry in entries:
        if entry["durationMonths"] <= 0:
            issues.append(
                {
                    "kind": "invalid_range",
                    "start": entry["start"],
                    "end": entry["end"],
                    "lineNumber": entry["lineNumber"],
                }
            )
        if entry["startMonthIndex"] > today.year * 12 + today.month - 1:
            issues.append(
                {
                    "kind": "future_start",
                    "start": entry["start"],
                    "lineNumber": entry["lineNumber"],
                }
            )
        if not entry["isPresent"] and entry["endMonthIndex"] > today.year * 12 + today.month - 1:
            issues.append(
                {
                    "kind": "future_end",
                    "end": entry["end"],
                    "lineNumber": entry["lineNumber"],
                }
            )

    overlaps: List[Dict[str, Any]] = []
    gaps: List[Dict[str, Any]] = []
    for current, following in zip(entries, entries[1:]):
        if following["startMonthIndex"] <= current["endMonthIndex"]:
            overlaps.append(
                {
                    "firstLine": current["lineNumber"],
                    "secondLine": following["lineNumber"],
                    "overlapMonths": current["endMonthIndex"] - following["startMonthIndex"] + 1,
                }
            )
        else:
            gap_months = following["startMonthIndex"] - current["endMonthIndex"] - 1
            if gap_months > 0:
                gaps.append(
                    {
                        "firstLine": current["lineNumber"],
                        "secondLine": following["lineNumber"],
                        "gapMonths": gap_months,
                    }
                )

    return {
        "status": "CHECKED",
        "checked": True,
        "method": "deterministic_interval_parse_v1",
        "referenceDate": today.isoformat(),
        "timelineEntries": entries,
        "issues": issues,
        "overlaps": overlaps,
        "gaps": gaps,
        "riskFlag": bool(issues),
        "requiresHumanReview": bool(overlaps or any(gap["gapMonths"] > 6 for gap in gaps)),
        "limitations": "Overlap/gap observations require role context; they are not automatic candidate risks.",
    }


# This is the executable contract behind the product claim "only rerun the
# affected nodes".  Keeping the DAG here makes revision planning independently
# testable; the runtime consumes the returned plan instead of trusting a caller
# supplied list blindly.
EVALUATION_NODE_ORDER: Sequence[str] = (
    "intent",
    "resume_parse",
    "jd_match",
    "knowledge_context",
    "tech_eval",
    "project_eval",
    "risk_eval",
    "evidence_fusion",
    "report",
)


def select_phase4_nodes(
    harness_plan: Mapping[str, Any],
    revision_plan: Optional[Mapping[str, Any]] = None,
    *,
    revision: int = 1,
) -> List[str]:
    """Apply dynamic pruning without pruning mandatory revision work.

    Initial runs follow the content-derived route. For a revision, optional
    evaluators in the invalidation closure are mandatory even if the newly
    calculated route would otherwise omit them; their old outputs were not
    copied and evidence fusion must never observe a missing/mixed revision.
    """

    route = harness_plan.get("route") if isinstance(harness_plan, Mapping) else {}
    route = route if isinstance(route, Mapping) else {}
    enabled = set(route.get("selectedAgents") or route.get("enabledAgents") or [])
    plan = revision_plan if isinstance(revision_plan, Mapping) else {}
    is_revision = bool(
        int(revision or 1) > 1
        or plan.get("baseCheckpointLoaded")
        or plan.get("baseWorkflowRunId")
    )
    if is_revision:
        execute_nodes = plan.get("execute_nodes") or plan.get("executeNodes") or []
        enabled.update(
            node
            for node in execute_nodes
            if node in {"tech_eval", "project_eval", "risk_eval"}
        )
    selected = [
        node
        for node in ("tech_eval", "project_eval", "risk_eval")
        if node in enabled
    ]
    return selected or ["evidence_fusion"]

NODE_DEPENDENCIES: Dict[str, Set[str]] = {
    "intent": set(),
    "resume_parse": set(),
    "jd_match": {"intent", "resume_parse"},
    "knowledge_context": {"jd_match"},
    "tech_eval": {"knowledge_context"},
    "project_eval": {"knowledge_context"},
    "risk_eval": {"knowledge_context"},
    "evidence_fusion": {"tech_eval", "project_eval", "risk_eval"},
    "report": {"evidence_fusion"},
}

NODE_OUTPUT_FIELDS: Dict[str, Sequence[str]] = {
    "intent": ("intentResult", "harnessPlan", "harnessContext", "memoryContext"),
    "resume_parse": ("parseResult",),
    "jd_match": ("jdResult",),
    "knowledge_context": ("harnessPlan", "harnessContext", "knowledgeContext"),
    "tech_eval": ("techResult",),
    "project_eval": ("projectResult",),
    "risk_eval": ("riskResult",),
    "evidence_fusion": ("fusionResult",),
    "report": (
        "finalReport",
        "overallScore",
        "recommendation",
        "strengths",
        "risks",
        "interviewQuestions",
        "degradedReasons",
    ),
}


def terminal_status_for_degradation(degraded_reasons: Sequence[str]) -> str:
    return "PARTIAL_SUCCESS" if any(str(reason).strip() for reason in degraded_reasons) else "SUCCESS"


@dataclass(frozen=True)
class RevisionExecutionPlan:
    requested_invalidations: List[str]
    execute_nodes: List[str]
    reused_nodes: List[str]
    cache_miss_nodes: List[str] = field(default_factory=list)
    unknown_nodes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _downstream_closure(nodes: Set[str]) -> Set[str]:
    invalidated = set(nodes)
    changed = True
    while changed:
        changed = False
        for node, dependencies in NODE_DEPENDENCIES.items():
            if node not in invalidated and dependencies.intersection(invalidated):
                invalidated.add(node)
                changed = True
    return invalidated


def _has_cached_output(base_state: Mapping[str, Any], node: str) -> bool:
    fields = NODE_OUTPUT_FIELDS[node]
    # Presence is intentional: score=0, [] and {} can be valid deterministic
    # outputs.  None/blank strings are incomplete checkpoints and force a rerun.
    for name in fields:
        if name not in base_state:
            return False
        value = base_state.get(name)
        if value is None or (isinstance(value, str) and not value.strip()):
            return False
    return True


def plan_revision_execution(
    affected_nodes: Sequence[str],
    base_state: Optional[Mapping[str, Any]] = None,
) -> RevisionExecutionPlan:
    """Build a fail-closed minimal-rerun plan for an immutable revision.

    A node can only be marked reusable when every output in its contract exists
    in the base checkpoint.  A missing cache entry invalidates that node and its
    transitive dependants; this prevents a superficially "minimal" run from
    producing a report with mixed or absent evidence.
    """

    requested = list(dict.fromkeys(str(node).strip() for node in affected_nodes if str(node).strip()))
    known = set(EVALUATION_NODE_ORDER)
    unknown = [node for node in requested if node not in known]
    requested_known = {node for node in requested if node in known}

    # No base means a first evaluation.  An empty invalidation set on a revision
    # with a base means a no-op revision and all complete outputs are reusable.
    if base_state is None or unknown:
        execute = set(known)
        cache_misses: Set[str] = set()
    else:
        execute = _downstream_closure(requested_known)
        cache_misses = {
            node
            for node in known - execute
            if not _has_cached_output(base_state, node)
        }
        execute.update(_downstream_closure(cache_misses))

    execute_nodes = [node for node in EVALUATION_NODE_ORDER if node in execute]
    reused_nodes = [
        node
        for node in EVALUATION_NODE_ORDER
        if node not in execute and base_state is not None and _has_cached_output(base_state, node)
    ]
    return RevisionExecutionPlan(
        requested_invalidations=requested,
        execute_nodes=execute_nodes,
        reused_nodes=reused_nodes,
        cache_miss_nodes=[node for node in EVALUATION_NODE_ORDER if node in cache_misses],
        unknown_nodes=unknown,
    )


def materialize_revision_state(
    current_state: Mapping[str, Any],
    affected_nodes: Sequence[str],
    base_state: Optional[Mapping[str, Any]],
) -> Tuple[Dict[str, Any], RevisionExecutionPlan]:
    """Copy only planner-approved outputs into a new immutable revision state."""

    materialized = dict(current_state)
    plan = plan_revision_execution(affected_nodes, base_state)
    if base_state is not None:
        for node_id in plan.reused_nodes:
            for field_name in NODE_OUTPUT_FIELDS[node_id]:
                materialized[field_name] = base_state[field_name]
    materialized["revisionPlan"] = plan.to_dict()
    materialized["reusedNodes"] = list(plan.reused_nodes)
    return materialized, plan


@dataclass(frozen=True)
class ToolLoopDecision:
    allowed: bool
    reason: str
    signature: str
    tool_call_count: int
    retrieval_query_count: int


@dataclass(frozen=True)
class ToolProposalBatchDecision:
    allowed: bool
    reason: str
    proposed_count: int
    limit: int


def guard_tool_proposal_batch(
    proposed_count: int,
    *,
    limit: int = MAX_PROPOSED_TOOL_CALLS_PER_ROUND,
) -> ToolProposalBatchDecision:
    """Bound model-proposed calls before they amplify trace and context size."""

    normalized_count = max(0, int(proposed_count))
    normalized_limit = max(0, int(limit))
    allowed = normalized_count <= normalized_limit
    return ToolProposalBatchDecision(
        allowed=allowed,
        reason="allowed" if allowed else "tool_proposal_batch_exceeded",
        proposed_count=normalized_count,
        limit=normalized_limit,
    )


class AgentToolLedger:
    """Deterministic budget and de-duplication guard for an Agent tool loop."""

    def __init__(
        self,
        agent_name: str,
        budgets: Optional[Mapping[str, Mapping[str, int]]] = None,
    ) -> None:
        configured = dict((budgets or DEFAULT_AGENT_TOOL_BUDGETS).get(agent_name, {}))
        self.max_tool_calls = max(0, int(configured.get("maxToolCalls", 0)))
        self.max_retrieval_queries = max(0, int(configured.get("maxRetrievalQueries", 0)))
        self.tool_call_count = 0
        self.retrieval_query_count = 0
        self._seen: Set[str] = set()

    @staticmethod
    def signature(tool_name: str, tool_args: Mapping[str, Any]) -> str:
        normalized = json.dumps(tool_args, ensure_ascii=False, sort_keys=True, default=str)
        # Full normalized arguments are kept out of traces and logs.  The hash
        # remains stable across dict ordering and is sufficient for de-dup.
        import hashlib

        digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]
        return f"{tool_name}:{digest}"

    def inspect(
        self,
        tool_name: str,
        tool_args: Mapping[str, Any],
        *,
        retrieval_queries: int = 0,
    ) -> ToolLoopDecision:
        signature = self.signature(tool_name, tool_args)
        if signature in self._seen:
            return self._decision(False, "duplicate_tool_call", signature)
        if self.tool_call_count >= self.max_tool_calls:
            return self._decision(False, "tool_budget_exceeded", signature)
        requested_queries = max(0, int(retrieval_queries))
        if requested_queries and (
            self.retrieval_query_count + requested_queries > self.max_retrieval_queries
        ):
            return self._decision(False, "retrieval_budget_exceeded", signature)

        self._seen.add(signature)
        self.tool_call_count += 1
        self.retrieval_query_count += requested_queries
        return self._decision(True, "allowed", signature)

    def _decision(self, allowed: bool, reason: str, signature: str) -> ToolLoopDecision:
        return ToolLoopDecision(
            allowed=allowed,
            reason=reason,
            signature=signature,
            tool_call_count=self.tool_call_count,
            retrieval_query_count=self.retrieval_query_count,
        )


@dataclass(frozen=True)
class ResultFenceDecision:
    accepted: bool
    reason: str


def fence_workflow_result(
    *,
    active_conversation_id: str,
    active_workflow_run_id: str,
    active_revision: int,
    incoming_conversation_id: str,
    incoming_workflow_run_id: str,
    incoming_revision: int,
    active_status: str = "RUNNING",
) -> ResultFenceDecision:
    """Reject late/superseded callbacks before they can mutate visible state."""

    if str(active_status).upper() in {"CANCELLED", "SUPERSEDED"}:
        return ResultFenceDecision(False, "active_run_not_writable")
    if incoming_conversation_id != active_conversation_id:
        return ResultFenceDecision(False, "conversation_mismatch")
    if int(incoming_revision) != int(active_revision):
        return ResultFenceDecision(False, "revision_mismatch")
    if incoming_workflow_run_id != active_workflow_run_id:
        return ResultFenceDecision(False, "workflow_run_mismatch")
    return ResultFenceDecision(True, "identity_match")


@dataclass(frozen=True)
class ExternalEvidenceAudit:
    usable: bool
    reason: str
    source_urls: List[str] = field(default_factory=list)


def audit_external_evidence(raw_result: Any, *, require_source_url: bool = True) -> ExternalEvidenceAudit:
    """Fail closed when a public MCP/tool fails or returns ungrounded content."""

    parsed: Any = raw_result
    if isinstance(raw_result, str):
        try:
            parsed = json.loads(raw_result)
        except json.JSONDecodeError:
            return ExternalEvidenceAudit(False, "non_json_external_result")
    if not isinstance(parsed, (dict, list)):
        return ExternalEvidenceAudit(False, "invalid_external_result")
    if not parsed:
        return ExternalEvidenceAudit(False, "empty_external_result")
    urls: List[str] = []
    failure_seen = False
    synthetic_seen = False
    fallback_seen = False

    def visit(value: Any) -> None:
        nonlocal failure_seen, synthetic_seen, fallback_seen
        if isinstance(value, Mapping):
            status = str(value.get("status") or "").strip().upper()
            failed_branch = bool(
                value.get("error")
                or status
                in {
                    "FAILED",
                    "ERROR",
                    "UNAVAILABLE",
                    "TIMEOUT",
                    "CANCELLED",
                    "SKIPPED",
                    "RATE_LIMITED",
                    "RATE-LIMITED",
                    "NOT_FOUND",
                }
                or value.get("ok") is False
                or value.get("success") is False
                or value.get("available") is False
                or value.get("skipped") is True
                or value.get("rateLimited") is True
                or value.get("timedOut") is True
            )
            synthetic_seen = synthetic_seen or bool(
                value.get("synthetic")
                or value.get("fabricated")
                or value.get("syntheticFallback") is True
                or value.get("synthetic_fallback") is True
            )
            fallback_seen = fallback_seen or bool(
                value.get("fallbackUsed")
                or value.get("usedResumeTextFallback")
                or value.get("fallback") is True
            )
            if failed_branch:
                # A failed branch cannot ground itself by echoing a requested
                # URL. Other independent result branches may still be usable.
                failure_seen = True
                return
            for key, item in value.items():
                if str(key).lower() in {"url", "sourceurl", "source_url", "html_url"}:
                    if isinstance(item, str) and re.match(r"^https?://", item.strip(), re.I):
                        urls.append(item.strip())
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    visit(parsed)
    if synthetic_seen:
        return ExternalEvidenceAudit(False, "synthetic_evidence_forbidden")
    if fallback_seen:
        return ExternalEvidenceAudit(False, "fallback_not_external_evidence")
    urls = dedupe_keep_order(urls)
    if failure_seen and not urls:
        return ExternalEvidenceAudit(False, "tool_failed")
    if require_source_url and not urls:
        return ExternalEvidenceAudit(False, "missing_source_url")
    return ExternalEvidenceAudit(True, "source_grounded", urls)


def audit_external_subject_binding(
    tool_metadata: Mapping[str, Any],
    tool_args: Mapping[str, Any],
    resume_text: str,
) -> Optional[str]:
    """Return a reason when candidate lookup is not bound to a declared identity."""

    evidence = tool_metadata.get("externalEvidence")
    if not isinstance(evidence, Mapping):
        return None
    if evidence.get("subjectBinding") == "not-applicable" or evidence.get("kind") == "deterministic-time":
        return None
    declared_urls = re.findall(r"https?://[^\s<>()\]\[\"']+", resume_text or "", re.I)
    declared_urls_normalized: Set[str] = set()
    declared_identifiers: Set[str] = set()
    generic_hosts = {
        "github.com",
        "www.github.com",
        "gitlab.com",
        "www.gitlab.com",
        "gitee.com",
        "www.gitee.com",
        "medium.com",
        "www.medium.com",
        "dev.to",
        "juejin.cn",
        "www.cnblogs.com",
        "segmentfault.com",
    }
    for url in declared_urls:
        lowered = url.rstrip("/.,，。；;").lower()
        declared_urls_normalized.add(lowered)
        parsed_url = urlparse(lowered)
        host = (parsed_url.hostname or "").lower()
        path_parts = [part for part in parsed_url.path.split("/") if part]
        # A personal/custom domain is itself a declared identity. Shared code
        # hosts are not: github.com/alice must never authorize github.com/bob.
        if host and host not in generic_hosts:
            declared_identifiers.add(host)
        if path_parts:
            declared_identifiers.add(path_parts[0])
        if len(path_parts) >= 2:
            declared_identifiers.add("/".join(path_parts[:2]))
    declared_identifiers.update(
        match.lower()
        for match in re.findall(
            r"(?<!\w)@([A-Za-z0-9](?:[A-Za-z0-9-]{1,38}))",
            resume_text or "",
        )
    )
    if not declared_urls_normalized and not declared_identifiers:
        return "candidate_identifier_not_declared"
    serialized_args = json.dumps(
        tool_args, ensure_ascii=False, sort_keys=True, default=str
    ).lower()
    full_url_match = any(url in serialized_args for url in declared_urls_normalized)
    identifier_match = any(
        re.search(
            rf"(?<![a-z0-9-]){re.escape(identifier)}(?![a-z0-9-])",
            serialized_args,
        )
        for identifier in declared_identifiers
        if len(identifier) >= 3
    )
    if not full_url_match and not identifier_match:
        return "tool_input_not_bound_to_declared_identifier"
    return None


def validate_trace_contract(
    events: Sequence[Mapping[str, Any]],
    *,
    workflow_run_id: str,
    conversation_id: str,
    revision: int,
) -> List[str]:
    """Return trace contract violations; an empty list is a passing trace."""

    violations: List[str] = []
    seen_event_ids: Set[str] = set()
    for index, event in enumerate(events):
        prefix = f"event[{index}]"
        event_id = str(event.get("eventId") or "")
        if not event_id:
            violations.append(f"{prefix}.eventId missing")
        elif event_id in seen_event_ids:
            violations.append(f"{prefix}.eventId duplicate")
        else:
            seen_event_ids.add(event_id)
        expected_identity = {
            "workflowRunId": workflow_run_id,
            "conversationId": conversation_id,
            "revision": revision,
        }
        for key, expected in expected_identity.items():
            if event.get(key) != expected:
                violations.append(f"{prefix}.{key} mismatch")
        if event.get("kind") == "tool":
            calls = event.get("toolCalls")
            if not isinstance(calls, list) or not calls:
                violations.append(f"{prefix}.toolCalls missing")
                continue
            for call_index, call in enumerate(calls):
                if not isinstance(call, Mapping):
                    violations.append(f"{prefix}.toolCalls[{call_index}] invalid")
                    continue
                for key in ("toolCallId", "name", "status", "inputHash"):
                    if not call.get(key):
                        violations.append(f"{prefix}.toolCalls[{call_index}].{key} missing")
    for index, event in enumerate(events):
        parent_id = str(event.get("parentEventId") or "")
        if parent_id and parent_id not in seen_event_ids:
            violations.append(f"event[{index}].parentEventId missing target")
    return violations


def parse_json_object(raw: Optional[str]) -> Dict[str, Any]:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def build_harness_plan(
    intent_result: str,
    resume_text: str,
    job_category: str = "",
    harness_context: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    intent = parse_json_object(intent_result)
    context = harness_context or {}
    required_skills = _as_str_list(intent.get("requiredSkills"))
    routing_hints = _as_str_list(intent.get("routingHints"))
    evidence_gaps = _as_str_list(intent.get("evidenceGaps"))
    interview_focus = _as_str_list(intent.get("interviewFocus"))
    candidate_type = str(intent.get("candidateType") or job_category or "UNKNOWN")
    experience_level = str(intent.get("experienceLevel") or "UNKNOWN")
    resume_len = len(resume_text or "")
    complexity = classify_complexity(resume_text, required_skills, routing_hints)
    memory_context = normalize_memory_context(context)
    knowledge_hits = normalize_knowledge_hits(context)
    memory_hits = (
        _as_list(memory_context.get("episodicHits"))
        + _as_list(memory_context.get("semanticHits"))
        + _as_list(memory_context.get("proceduralHits"))
    )
    has_tech_signal = bool(re.search(r"Java|Spring|Kafka|K8s|Kubernetes|Redis|MySQL|Docker|Milvus|RAG|LLM|后端|系统|平台", resume_text or "", re.I))
    has_project_signal = bool(re.search(r"项目|平台|系统|中台|架构|重构|负责|上线|指标|性能|高并发", resume_text or "", re.I))
    has_metric_signal = bool(re.search(r"\d+%|\d+倍|QPS|TPS|P99|RT|ms|秒|分钟|成本|收入|DAU|PV|UV", resume_text or "", re.I))
    has_timeline_signal = bool(re.search(r"20\d{2}|19\d{2}|至今|实习|毕业|入职|离职", resume_text or "", re.I))
    risk_memory_hits = [
        hit for hit in _as_list(memory_context.get("proceduralHits")) + _as_list(memory_context.get("semanticHits"))
        if isinstance(hit, dict)
        and float(hit.get("matchScore") or 0) >= 0.45
        and re.search(r"risk_eval|风险|真实性|时间线|证据|gap|缺口", str(hit.get("appliesTo", "")), re.I)
    ]
    meaningful_gaps = [
        gap for gap in evidence_gaps
        if not re.search(r"unknown|未明确|待定|一般|通用", gap, re.I)
    ]
    should_run_risk = bool(
        meaningful_gaps
        or resume_len < 900
        or risk_memory_hits
        or "risk" in " ".join(routing_hints).lower()
        or (not has_metric_signal and not has_timeline_signal)
    )

    base_agents = ["resume_parse", "jd_match", "evidence_fusion", "report"]
    all_optional = {"tech_eval", "project_eval", "risk_eval"}

    if complexity == "sparse":
        route_mode = "FAST_SCREEN"
        selected_agents = base_agents + ["risk_eval"]
    elif has_tech_signal and has_project_signal and not should_run_risk:
        route_mode = "TECH_DEEP_DIVE"
        selected_agents = base_agents + ["tech_eval", "project_eval"]
    elif has_tech_signal and not has_project_signal:
        route_mode = "TECH_SCREEN"
        selected_agents = base_agents + ["tech_eval", "risk_eval"]
    elif has_project_signal and should_run_risk:
        route_mode = "PROJECT_AUTHENTICITY_REVIEW"
        selected_agents = base_agents + ["project_eval", "risk_eval"]
    elif should_run_risk:
        route_mode = "RISK_REVIEW"
        selected_agents = base_agents + ["risk_eval"]
    else:
        route_mode = "FULL_REVIEW"
        selected_agents = base_agents + ["tech_eval", "project_eval", "risk_eval"]

    def reason_for_skip(agent: str) -> str:
        if agent == "tech_eval":
            return "未检测到技术岗位/技术栈信号"
        if agent == "project_eval":
            return "简历缺少项目/平台/职责边界信号"
        if agent == "risk_eval":
            return "证据、时间线和量化指标较完整，未命中风险型 Memory"
        return "当前路由模式不需要该 Agent"

    skipped_agents = {
        agent: reason_for_skip(agent)
        for agent in all_optional
        if agent not in selected_agents
    }
    enabled_agents = dedupe_keep_order(selected_agents)

    why_selected: List[str] = []
    if "tech_eval" in enabled_agents:
        why_selected.append("检测到技术栈/岗位信号，启用 TechEvalAgent")
    if "project_eval" in enabled_agents:
        why_selected.append("检测到项目/平台/职责信号，启用 ProjectEvalAgent")
    if "risk_eval" in enabled_agents:
        why_selected.append("存在 JD gap、时间线/指标缺口或风险 Memory，启用 RiskAgent")
    if route_mode == "FAST_SCREEN":
        why_selected.append("短简历走 FAST_SCREEN，仅保留核心解析与风险验证")

    why_skipped = [f"{agent}: {reason}" for agent, reason in skipped_agents.items()]

    if not skipped_agents:
        signals: List[str] = []
        if has_tech_signal:
            signals.append("技术栈信号")
        if has_project_signal:
            signals.append("项目信号")
        if meaningful_gaps:
            signals.append(f"JD gap {len(meaningful_gaps)} 项")
        if should_run_risk:
            signals.append("风险信号")
        if has_metric_signal:
            signals.append("量化指标")
        no_pruning_reason = f"保留完整评估 DAG：{('、'.join(signals) if signals else '多信号均命中')}"
    else:
        no_pruning_reason = ""
    memory_influence = derive_memory_influence(memory_context)
    knowledge_influence = derive_knowledge_influence(knowledge_hits)
    query_plans = {
        "tech_eval": build_queries(intent, resume_text, "技术深度", [
            "Java Spring Boot Kafka K8s 项目经验",
            "高并发 稳定性 性能优化",
            "后端工程实践 可观测性 排障",
        ]),
        "project_eval": build_queries(intent, resume_text, "项目真实性与复杂度", [
            "项目经历 架构 重构 中台",
            "核心业务 项目 贡献 复杂度",
            "项目 真实性 验证",
        ]),
        "risk_eval": build_queries(intent, resume_text, "风险验证", [
            "跳槽 空白期 时间线",
            "技能夸大 简历真实性",
            "在职 实习 时间冲突",
        ]),
    }

    route = {
        "routeMode": route_mode,
        "executionProfile": {
            "FAST_SCREEN": "短/信息不足简历：跳过技术与项目深评，仅核心解析+风险核验，最省 LLM 调用",
            "TECH_SCREEN": "技术信号强、项目薄：技术深评+风险核验，跳过项目深评",
            "TECH_DEEP_DIVE": "技术与项目都充分：技术+项目双深评（并行），证据完整故跳过风险",
            "PROJECT_AUTHENTICITY_REVIEW": "项目多但贡献边界不清：项目真实性深评+风险核验",
            "RISK_REVIEW": "时间线/证据/JD gap 风险高：聚焦风险核验",
            "FULL_REVIEW": "多信号且复杂：技术+项目+风险全开",
        }.get(route_mode, route_mode),
        "candidateType": candidate_type,
        "experienceLevel": experience_level,
        "targetRole": intent.get("targetRole") or infer_target_role(resume_text, candidate_type),
        "selectedAgents": enabled_agents,
        "enabledAgents": enabled_agents,
        "skippedAgents": skipped_agents,
        "whySelected": why_selected,
        "whySkipped": why_skipped,
        "noPruningReason": no_pruning_reason,
        "requiredSkills": required_skills,
        "routingHints": routing_hints,
        "evidenceGaps": evidence_gaps,
        "interviewFocus": interview_focus,
        "complexity": complexity,
        "path": "deep_pdf" if complexity in {"medium", "deep"} else "sparse_focused_route",
        "routingRationale": build_routing_rationale(
            candidate_type,
            experience_level,
            resume_len,
            required_skills,
            routing_hints,
            memory_hits,
            knowledge_hits,
        ),
        "memoryHitCount": len(memory_hits),
        "knowledgeHitCount": len(knowledge_hits),
    }
    # This is a plan-time lower bound, never an observed cost metric.  A normal
    # run has intent + parse + JD (3), one generation per selected evaluator,
    # and two parallel report generations. Tool loops may add rounds up to the
    # explicit upper bound; actual calls/tokens come only from generation trace
    # events. Sparse resumes still use real model evaluation; routing may skip
    # irrelevant specialist nodes but never substitutes heuristic scores.
    optional_selected = [a for a in enabled_agents if a in {"tech_eval", "project_eval", "risk_eval"}]
    full_pipeline_calls = 8
    estimated_calls = 5 + len(optional_selected)
    estimated_upper_bound = (3 + len(optional_selected)) * 4 + 2
    route["estimatedLlmCalls"] = estimated_calls
    route["estimatedLlmCallsLowerBound"] = estimated_calls
    route["estimatedLlmCallsUpperBound"] = estimated_upper_bound
    route["fullPipelineLlmCalls"] = full_pipeline_calls
    route["llmCallsSavedVsFull"] = max(0, full_pipeline_calls - estimated_calls)
    route["llmCallEstimateBasis"] = "plan_lower_bound_not_observed; use generation trace events for actual calls"
    return {
        "version": "agent-harness-v1",
        "route": route,
        "dynamicQueries": {key: value for key, value in query_plans.items() if key in enabled_agents},
        "memoryInfluence": memory_influence,
        "knowledgeInfluence": knowledge_influence,
        "contextManagement": derive_context_management(complexity, enabled_agents, knowledge_hits),
        "runtimeBudgets": derive_runtime_budgets(enabled_agents, complexity, memory_context, knowledge_hits),
        "reportMode": "llm_detailed",
        "queryPlans": query_plans,
        "governance": {
            "maxDuplicateToolArgs": 0,
            "requireEvidenceSource": True,
            "requireCoverageChecklist": True,
            "fallbackPolicy": "explicitly_surface_fallback_do_not_hide",
            "budgetPolicy": "long_pdf_uses_context_pack_not_full_text_repeatedly",
        },
    }


def classify_complexity(resume_text: str, required_skills: List[str], routing_hints: List[str]) -> str:
    text = resume_text or ""
    project_count = len(re.findall(r"项目|平台|系统|中台|重构|架构|Agent|RAG", text, re.I))
    if len(text) < 600:
        return "sparse"
    if len(text) > 4500 or project_count >= 8 or len(required_skills) + len(routing_hints) >= 6:
        return "deep"
    return "medium"


def normalize_memory_context(context: Dict[str, Any]) -> Dict[str, Any]:
    raw = context.get("memoryContext") if isinstance(context.get("memoryContext"), dict) else context
    if any(key in raw for key in ("episodicHits", "semanticHits", "proceduralHits")):
        return {
            "episodicHits": _as_list(raw.get("episodicHits")),
            "semanticHits": _as_list(raw.get("semanticHits")),
            "proceduralHits": _as_list(raw.get("proceduralHits")),
        }
    legacy = _as_list(raw.get("memoryHits"))
    return {"episodicHits": legacy, "semanticHits": [], "proceduralHits": []}


def normalize_knowledge_hits(context: Dict[str, Any]) -> List[Any]:
    if isinstance(context.get("knowledgeContext"), dict):
        knowledge = context["knowledgeContext"].get("knowledge") if isinstance(context["knowledgeContext"].get("knowledge"), dict) else {}
        return _as_list(knowledge.get("chunks"))
    if isinstance(context.get("knowledge"), dict):
        return _as_list(context["knowledge"].get("chunks"))
    return _as_list(context.get("knowledgeHits"))


def derive_memory_influence(memory_context: Dict[str, Any]) -> Dict[str, Any]:
    episodic = _as_list(memory_context.get("episodicHits"))
    semantic = _as_list(memory_context.get("semanticHits"))
    procedural = _as_list(memory_context.get("proceduralHits"))
    influences: List[Dict[str, Any]] = []
    for layer, hits in (("episodic", episodic), ("semantic", semantic), ("procedural", procedural)):
        for hit in hits[:3]:
            if not isinstance(hit, dict):
                continue
            influences.append({
                "type": layer,
                "memoryId": hit.get("memoryId"),
                "traceId": hit.get("traceId"),
                "appliesTo": hit.get("appliesTo", "routing"),
                "recommendedAction": hit.get("recommendedAction", ""),
                "matchReason": hit.get("matchReason", ""),
                "content": hit.get("content") or hit.get("summary", ""),
            })
    return {
        "hitCount": len(episodic) + len(semantic) + len(procedural),
        "episodicCount": len(episodic),
        "semanticCount": len(semantic),
        "proceduralCount": len(procedural),
        "appliedTo": dedupe_keep_order([str(item.get("appliesTo")) for item in influences if item.get("appliesTo")]),
        "influences": influences,
        "calibration": derive_memory_calibration(episodic),
        "poisoningControl": "Memory influences strategy only; it is never candidate factual evidence.",
    }


def derive_memory_calibration(episodic: List[Any]) -> Dict[str, Any]:
    """Aggregate similar past evaluations into a scoring-calibration anchor (only when >=3 samples).

    This is the concrete, felt use of episodic memory: retrieval over our own evaluation history
    keeps scoring consistent across similar candidates. Calibration reference only, never a fact.
    """
    scores: List[float] = []
    recs: Dict[str, int] = {}
    for hit in episodic:
        if not isinstance(hit, dict):
            continue
        evidence = hit.get("evidence") if isinstance(hit.get("evidence"), dict) else {}
        try:
            score = float(evidence.get("score") or 0)
        except (TypeError, ValueError):
            score = 0.0
        if score > 0:
            scores.append(score)
        rec = str(evidence.get("recommendation") or "").strip()
        if rec:
            recs[rec] = recs.get(rec, 0) + 1
    if len(scores) < 3:
        return {"available": False, "sampleSize": len(scores)}
    avg = round(sum(scores) / len(scores), 1)
    return {
        "available": True,
        "sampleSize": len(scores),
        "avgScore": avg,
        "scoreRange": [int(min(scores)), int(max(scores))],
        "recommendationDistribution": recs,
        "note": "相似历史候选人评分校准参考，非候选人事实",
    }


def derive_knowledge_influence(knowledge_hits: List[Any]) -> Dict[str, Any]:
    chunks: List[Dict[str, Any]] = [hit for hit in knowledge_hits if isinstance(hit, dict)]
    return {
        "hitCount": len(chunks),
        "injectedInto": ["TechEvalAgent", "ProjectEvalAgent", "RiskAgent", "EvidenceFusionAgent", "ReportAgent"] if chunks else [],
        "chunks": [
            {
                "chunkId": chunk.get("chunkId"),
                "title": chunk.get("title"),
                "docType": chunk.get("docType"),
                "sectionPath": chunk.get("sectionPath"),
                "score": chunk.get("score"),
                "rerankReason": chunk.get("rerankReason"),
                "contentPreview": chunk.get("contentPreview") or str(chunk.get("content", ""))[:220],
            }
            for chunk in chunks[:5]
        ],
        "evidenceBoundary": "Knowledge base is rubric/interview standard only, not candidate fact evidence.",
    }


def derive_context_management(complexity: str, enabled_agents: List[str], knowledge_hits: List[Any]) -> Dict[str, Any]:
    """Context engineering budget (write / select / compress / isolate).

    Treats the context window as a budget with per-segment line items, mirroring the
    Anthropic/LangChain "context engineering" practice. The actual select/compress is enforced
    in build_report_context_pack (top-k reranked knowledge, compacted agent JSON, bounded resume excerpt).
    """
    window = 12000 if complexity == "deep" else (5000 if complexity == "sparse" else 8000)
    segments = [
        {"segment": "system + tool catalog", "budgetTokens": 1200, "policy": "static_cached（write）"},
        {"segment": "task + routing decision", "budgetTokens": 700, "policy": "always_resident"},
        {"segment": "retrieved knowledge", "budgetTokens": 1500 if knowledge_hits else 0,
         "policy": "select：top-k reranked, 不喂裸 top-N"},
        {"segment": "agent evidence (tech/project/risk)", "budgetTokens": 3000, "policy": "compress：compact JSON"},
        {"segment": "resume excerpt", "budgetTokens": 2600, "policy": "compress：超阈值截断"},
        {"segment": "reserved for model reply", "budgetTokens": 2500, "policy": "reserved（不挪用）"},
    ]
    return {
        "strategy": ["write", "select", "compress", "isolate"],
        "windowBudgetTokens": window,
        "segments": segments,
        "compactionTrigger": "compact_when_segment_exceeds_budget",
        "isolation": "每个 eval Agent 独立上下文窗口，仅向 ReportAgent 回传压缩后的结构化证据",
        "note": "上下文按预算分段管理，避免 context pollution；ReportAgent 只吃压缩证据而非全量原文重复",
    }


def derive_runtime_budgets(
    enabled_agents: List[str],
    complexity: str,
    memory_context: Dict[str, Any],
    knowledge_hits: List[Any],
) -> Dict[str, Dict[str, Any]]:
    budgets: Dict[str, Dict[str, Any]] = {}
    agent_map = {
        "resume_parse": "ResumeParseAgent",
        "jd_match": "JdMatchAgent",
        "tech_eval": "TechEvalAgent",
        "project_eval": "ProjectEvalAgent",
        "risk_eval": "RiskAgent",
        "evidence_fusion": "EvidenceFusionAgent",
        "report": "ReportAgent",
    }
    fixed_tools_by_agent: Dict[str, List[str]] = {
        "ResumeParseAgent": ["resume_structure_extract"],
        "JdMatchAgent": ["milvus_jd_search", "jd_requirements_extract"],
        "RiskAgent": ["timeline_validator"],
        "ReportAgent": ["execute_skill"],
    }
    for route_id in enabled_agents:
        agent = agent_map.get(route_id)
        if not agent:
            continue
        budget = dict(DEFAULT_AGENT_TOOL_BUDGETS.get(agent, {"maxToolCalls": 0, "maxRetrievalQueries": 0}))
        if complexity == "deep" and route_id in {"project_eval", "risk_eval"}:
            budget["maxRetrievalQueries"] = max(int(budget.get("maxRetrievalQueries", 0)), 4)
        fixed_tools = fixed_tools_by_agent.get(agent, [])
        budget["scope"] = "adaptive_agent_loop_only"
        budget["preexecutedTools"] = list(fixed_tools)
        budget["maxTotalToolCalls"] = int(budget.get("maxToolCalls", 0)) + len(fixed_tools)
        if route_id == "report":
            budget["contextSources"] = ["resume_text", "jd_result", "agent_results"]
            if knowledge_hits:
                budget["contextSources"].append("knowledge_hits")
            if memory_context.get("proceduralHits"):
                budget["contextSources"].append("procedural_memory")
        budgets[agent] = budget
    return budgets


def build_routing_rationale(
    candidate_type: str,
    experience_level: str,
    resume_len: int,
    required_skills: List[str],
    routing_hints: List[str],
    memory_hits: Optional[List[Any]] = None,
    knowledge_hits: Optional[List[Any]] = None,
) -> List[str]:
    rationale = [
        f"candidateType={candidate_type}",
        f"experienceLevel={experience_level}",
        f"resumeTextLength={resume_len}",
    ]
    if required_skills:
        rationale.append("requiredSkills=" + ",".join(required_skills[:6]))
    if routing_hints:
        rationale.append("routingHints=" + ",".join(routing_hints[:6]))
    if resume_len > 4500:
        rationale.append("long_pdf_context_pack_required")
    if resume_len < 600:
        rationale.append("sparse_resume_focused_route_no_heuristic_scoring")
    if memory_hits:
        rationale.append(f"agent_memory_hits={len(memory_hits)}")
    if knowledge_hits:
        rationale.append(f"self_service_knowledge_hits={len(knowledge_hits)}")
    return rationale


def build_queries(intent: Dict[str, Any], resume_text: str, focus: str, defaults: List[str]) -> List[str]:
    queries: List[str] = []
    queries.extend(_as_str_list(intent.get("ragQueries")))
    required_skills = _as_str_list(intent.get("requiredSkills"))
    routing_hints = _as_str_list(intent.get("routingHints"))
    if required_skills:
        queries.append(f"{focus} {' '.join(required_skills[:6])}")
    if routing_hints:
        queries.append(f"{focus} {' '.join(routing_hints[:6])}")
    for keyword in extract_resume_keywords(resume_text)[:4]:
        queries.append(f"{focus} {keyword}")
    queries.extend(defaults)
    return dedupe_keep_order(queries)[:4]


def build_harness_reflection(
    harness_plan: Dict[str, Any],
    tool_health: Optional[Dict[str, Any]],
    coverage_checklist: str,
    tech_result: str,
    project_result: str,
    risk_result: str,
) -> Dict[str, Any]:
    health = tool_health or {}
    fallback_tools = [
        name for name, entry in health.items()
        if isinstance(entry, dict) and entry.get("fallbackUsed")
    ]
    missing_contracts: List[str] = []
    for label, value in {
        "techResult": tech_result,
        "projectResult": project_result,
        "riskResult": risk_result,
    }.items():
        if "evidenceSource" not in (value or ""):
            missing_contracts.append(f"{label}.evidenceSource")
    return {
        "harnessVersion": harness_plan.get("version"),
        "fallbackTools": fallback_tools,
        "missingContracts": missing_contracts,
        "coverageChecklistLength": len(coverage_checklist or ""),
        "nextRunImprovements": [
            "If fallbackTools is non-empty, surface RAG fallback in report and Grafana.",
            "If missingContracts is non-empty, tighten the corresponding agent prompt/schema.",
            "Keep deterministic planning; reserve LLM calls for synthesis and final report.",
        ],
    }


def infer_target_role(resume_text: str, candidate_type: str) -> str:
    text = resume_text or ""
    if re.search(r"Java|Spring|Kafka|K8s|后端", text, re.I):
        return "Java 后端 / 平台工程师"
    if "产品" in text:
        return "产品经理"
    return candidate_type


def extract_resume_keywords(resume_text: str) -> List[str]:
    pattern = re.compile(r"(Java|Spring Boot|Kafka|K8s|Kubernetes|Redis|MySQL|Docker|LLM|RAG|Agent|支付中台|重构|实习|本科|项目)")
    return dedupe_keep_order(pattern.findall(resume_text or ""))


def dedupe_keep_order(items: List[str]) -> List[str]:
    seen: set[str] = set()
    result: List[str] = []
    for item in items:
        text = str(item).strip()
        key = text.lower()
        if text and key not in seen:
            seen.add(key)
            result.append(text)
    return result


def _as_str_list(value: Any) -> List[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def _as_list(value: Any) -> List[Any]:
    return value if isinstance(value, list) else []
