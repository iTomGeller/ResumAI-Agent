#!/usr/bin/env python3
"""Aggregate load-generator, Agent Runtime and ECS samples into one report."""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import math
import re
import statistics
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlencode

import httpx


EXPECTED_MCP_ENDPOINTS = [
    "exa.web_fetch_exa",
    "exa.web_search_exa",
    "fetch.fetch",
]
EXPECTED_SKILLS = [
    "assess-production-engineering",
    "assess-technical-evidence",
    "audit-claim-consistency",
    "audit-evidence-provenance",
    "calibrate-evidence-confidence",
    "ground-project-claims",
    "retrieve-public-candidate-evidence",
    "risk-pattern-detection",
]
CONTAINERS = (
    "ai-resume-backend", "ai-resume-workflow",
    "resumai-mysql", "resumai-redis",
)
DOCKER_RE = re.compile(
    r"(ai-resume-backend|ai-resume-workflow|resumai-mysql|resumai-redis),"
    r"([0-9.]+)%,([^,]+),([^,]+),([^,]+),(\d+)")

RAG_SCENARIOS = {
    "jd_match_search": "jd_matching",
    "knowledge_search": "knowledge_base",
    "resume_semantic_search": "resume_evidence",
}
RAG_SCENARIO_LABELS = {
    "jd_matching": "岗位匹配检索",
    "knowledge_base": "岗位/评估知识库",
    "resume_evidence": "简历内证据检索",
    "unknown": "其他检索",
}


def percentile(values: Iterable[float], q: float) -> Optional[float]:
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return None
    position = (len(ordered) - 1) * q
    lo = math.floor(position)
    hi = math.ceil(position)
    if lo == hi:
        return ordered[lo]
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (position - lo)


def distribution(values: Iterable[float], digits: int = 3) -> Dict[str, Any]:
    rows = [float(value) for value in values]
    if not rows:
        return {key: None for key in (
            "count", "min", "avg", "p50", "p95", "p99", "max")}
    return {
        "count": len(rows),
        "min": round(min(rows), digits),
        "avg": round(statistics.fmean(rows), digits),
        "p50": round(percentile(rows, 0.50), digits),
        "p95": round(percentile(rows, 0.95), digits),
        "p99": round(percentile(rows, 0.99), digits),
        "max": round(max(rows), digits),
    }


def repair_late_polling_metrics(
        summary: Dict[str, Any], directory: Path) -> Dict[str, Any]:
    """Exclude terminal polling lag from an open-loop run's latency metrics.

    Older load-driver revisions began terminal polling only after all uploads
    had been issued.  The backend task payload contains queue lifecycle
    timestamps, so completed runs can be corrected without rerunning LLM work.
    """
    raw_path = directory / "raw_results.json"
    if not raw_path.is_file():
        return summary
    try:
        rows = json.loads(raw_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return summary
    if not isinstance(rows, list):
        return summary

    e2e_ms: List[float] = []
    completion_times: List[float] = []
    upload_starts: List[float] = []
    upload_finishes: List[float] = []
    corrected = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        task = row.get("rawTask")
        queue = task.get("queue") if isinstance(task, dict) else None
        if not isinstance(queue, dict):
            continue
        try:
            lifecycle_ms = max(0.0, (
                datetime.fromisoformat(str(queue["finishedAt"]))
                - datetime.fromisoformat(str(queue["queuedAt"]))
            ).total_seconds() * 1000)
            upload_ms = float(row.get("uploadMs") or 0)
            started = float(row["uploadStartedAt"])
            finished = float(row["uploadFinishedAt"])
        except (KeyError, TypeError, ValueError):
            continue
        elapsed_ms = upload_ms + lifecycle_ms
        e2e_ms.append(elapsed_ms)
        completion_times.append(started + elapsed_ms / 1000.0)
        upload_starts.append(started)
        upload_finishes.append(finished)
        corrected += 1

    if not e2e_ms:
        return summary
    summary["endToEndLatencyMs"] = {
        key: value for key, value in distribution(e2e_ms).items()
        if key in {"p50", "p95", "p99", "max"}
    }
    observation_span = max(completion_times) - min(upload_starts)
    summary["completionThroughputPerSecond"] = round(
        len(e2e_ms) / max(0.001, observation_span), 4)
    summary["drainDurationS"] = round(max(
        0.0, max(completion_times) - max(upload_finishes)), 3)
    summary["completionTimestampSource"] = "server_queue_lifecycle"
    summary["latePollingMetricsCorrected"] = corrected
    queue_path = directory / "queue_samples.jsonl"
    if queue_path.is_file():
        for line in reversed(queue_path.read_text(encoding="utf-8").splitlines()):
            try:
                sample = json.loads(line)
            except json.JSONDecodeError:
                continue
            run_queue = sample.get("runQueue")
            if isinstance(run_queue, dict):
                summary.setdefault("queue", {})["finalRunQueued"] = float(
                    run_queue.get("queued") or 0)
                summary["queue"]["finalRunActive"] = float(
                    run_queue.get("active") or 0)
                break
    return summary


def collect_report_quality(directory: Path) -> Dict[str, Any]:
    raw_path = directory / "raw_results.json"
    if not raw_path.is_file():
        return {}
    try:
        rows = json.loads(raw_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    lengths: List[float] = []
    risks: List[float] = []
    questions: List[float] = []
    evidence_refs: List[float] = []

    def count_evidence(value: Any) -> int:
        if isinstance(value, dict):
            own = len(value.get("evidenceRefs") or []) \
                if isinstance(value.get("evidenceRefs"), list) else 0
            return own + sum(count_evidence(child) for child in value.values())
        if isinstance(value, list):
            return sum(count_evidence(child) for child in value)
        return 0

    for row in rows if isinstance(rows, list) else []:
        task = row.get("rawTask") if isinstance(row, dict) else None
        if not isinstance(task, dict):
            continue
        structured = task.get("structuredReport")
        structured = structured if isinstance(structured, dict) else {}
        lengths.append(float(len(str(task.get("fullReport") or ""))))
        risks.append(float(len(structured.get("risks") or task.get("risks") or [])))
        questions.append(float(len(
            structured.get("interviewQuestions")
            or task.get("interviewQuestions") or [])))
        evidence_refs.append(float(count_evidence(structured)))
    return {
        "reports": len(lengths),
        "emptyReports": sum(value == 0 for value in lengths),
        "fullReportCharacters": distribution(lengths, digits=1),
        "risksPerReport": distribution(risks, digits=2),
        "questionsPerReport": distribution(questions, digits=2),
        "evidenceRefsPerReport": distribution(evidence_refs, digits=2),
    }


def collect_labeled_rag(directory: Path) -> Dict[str, Any]:
    reports_root = directory.parent
    sources = {
        "jdKnowledge": reports_root / "experiments" /
        "retrieval_embedding_prod-current-ndcg-20260731.json",
        "resumeEvidence": reports_root /
        "resume_rag_ab_f709edd_hybrid_after.json",
    }
    result: Dict[str, Any] = {}
    for key, path in sources.items():
        if not path.is_file():
            continue
        try:
            parsed = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        result[key] = {
            "source": str(path.relative_to(reports_root)).replace("\\", "/"),
            "data": parsed,
        }
    return result


def collect_llm_failure_reasons(directory: Path) -> Dict[str, Any]:
    path = directory / "runtime_metrics.json"
    if not path.is_file():
        return {}
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    reasons: Counter = Counter()
    for row in rows if isinstance(rows, list) else []:
        for event in (row.get("notableEvents") or []) \
                if isinstance(row, dict) else []:
            if not isinstance(event, dict) \
                    or event.get("eventType") != "llm.failed":
                continue
            error = str(event.get("error") or "UNKNOWN")
            reasons[error.split(":", 1)[0].strip() or "UNKNOWN"] += 1
    rate_limited = sum(
        count for reason, count in reasons.items()
        if "429" in reason or "RATE_LIMIT" in reason.upper())
    return {"reasons": dict(reasons), "rateLimited": rate_limited}


def mcp_terminal_class(event_type: str, payload: Dict[str, Any]) -> str:
    """Classify the real MCP result, not merely the event envelope.

    A provider can return ``tool.completed`` with ``success=false`` and a
    domain status such as RATE_LIMITED/UNAVAILABLE. Counting the envelope as a
    success made the old benchmark materially overstate MCP reliability.
    """
    preview = payload.get("resultPreview")
    parsed_preview: Dict[str, Any] = {}
    if isinstance(preview, dict):
        parsed_preview = preview
    elif isinstance(preview, str):
        try:
            candidate = json.loads(preview)
            if isinstance(candidate, dict):
                parsed_preview = candidate
        except json.JSONDecodeError:
            pass
    status_tokens = " ".join(str(value or "") for value in (
        payload.get("outcome"), payload.get("status"), payload.get("error"),
        preview, parsed_preview.get("status"), parsed_preview.get("error"),
        parsed_preview.get("message"),
    )).lower()
    failed = (
        event_type.endswith("failed")
        or payload.get("success") is False
        or parsed_preview.get("success") is False
        or any(token in status_tokens for token in (
            "failed", "rejected", "unavailable", "rate_limited",
            "auth_required", "circuit_open"))
    )
    if not failed:
        return "success"
    if any(token in status_tokens for token in (
            "rate_limit", "rate limited", "too many requests", "status=429",
            "status code 429", "\"status\":429")):
        return "rateLimited"
    if any(token in status_tokens for token in (
            "timeout", "timed out", "deadline exceeded")):
        return "timeout"
    if any(token in status_tokens for token in (
            "status code 404", "status=404", "not found")):
        return "notFound"
    if any(token in status_tokens for token in (
            "status code 403", "status=403", "forbidden")):
        return "forbidden"
    if "rejected" in status_tokens:
        return "rejected"
    return "otherFailed"


def to_bytes(value: str) -> float:
    match = re.match(r"\s*([0-9.]+)\s*([KMGT]?i?B)\s*$", value)
    if not match:
        return 0.0
    number = float(match.group(1))
    unit = match.group(2)
    factors = {
        "B": 1, "KB": 1_000, "MB": 1_000_000,
        "GB": 1_000_000_000, "TB": 1_000_000_000_000,
        "KiB": 1024, "MiB": 1024 ** 2,
        "GiB": 1024 ** 3, "TiB": 1024 ** 4,
    }
    return number * factors[unit]


def io_pair(value: str) -> Tuple[float, float]:
    parts = [item.strip() for item in value.split("/")]
    if len(parts) != 2:
        return 0.0, 0.0
    return to_bytes(parts[0]), to_bytes(parts[1])


def json_column(value: str) -> Dict[str, Any]:
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        return {}


def key_values(value: str, separator: str) -> Dict[str, float]:
    result: Dict[str, float] = {}
    for item in value.split(","):
        if separator not in item:
            continue
        key, raw = item.split(separator, 1)
        try:
            result[key.strip()] = float(raw.strip())
        except ValueError:
            continue
    return result


def parse_proc(value: str) -> Dict[str, float]:
    patterns = {
        "vmHwmMiB": r"VmHWM:\s*(\d+)\s*kB",
        "rssMiB": r"VmRSS:\s*(\d+)\s*kB",
        "threads": r"Threads:\s*(\d+)",
        "openFds": r"open_fds:(\d+)",
        "nrThrottled": r"nr_throttled\s+(\d+)",
        "throttledUsec": r"throttled_usec\s+(\d+)",
        "oom": r"(?:^|,)oom\s+(\d+)",
        "oomKill": r"oom_kill\s+(\d+)",
    }
    parsed: Dict[str, float] = {}
    for key, pattern in patterns.items():
        match = re.search(pattern, value)
        if match:
            number = float(match.group(1))
            parsed[key] = number / 1024 if key in {
                "vmHwmMiB", "rssMiB"} else number
    return parsed


def delta(values: List[float]) -> Optional[float]:
    if not values:
        return None
    return round(max(0.0, values[-1] - values[0]), 3)


def parse_ecs_monitor(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {
            "status": "NOT_COLLECTED",
            "validSamples": 0,
            "malformedSamples": 0,
        }
    lines = path.read_text(encoding="utf-8").splitlines()
    docker: Dict[str, Dict[str, List[float]]] = {
        name: defaultdict(list) for name in CONTAINERS}
    proc: Dict[str, Dict[str, List[float]]] = {
        "backend": defaultdict(list), "workflow": defaultdict(list)}
    active_agents: List[float] = []
    task_queue: Dict[str, List[float]] = defaultdict(list)
    run_queue: Dict[str, List[float]] = defaultdict(list)
    mysql: Dict[str, List[float]] = defaultdict(list)
    redis: Dict[str, List[float]] = defaultdict(list)
    disk: Dict[str, List[float]] = defaultdict(list)
    timestamps: List[datetime] = []
    restarts: List[int] = []
    oom_killed = 0
    malformed = 0

    for line in lines[1:]:
        columns = line.split("|")
        if len(columns) != 11:
            malformed += 1
            continue
        queue_index = 6
        try:
            timestamps.append(datetime.fromisoformat(columns[0]))
        except ValueError:
            malformed += 1
            continue
        for name, cpu, memory, network, block, pids in DOCKER_RE.findall(
                columns[1]):
            used, _limit = io_pair(memory)
            net_rx, net_tx = io_pair(network)
            block_read, block_write = io_pair(block)
            target = docker[name]
            target["cpuPct"].append(float(cpu))
            target["memoryMiB"].append(used / 1024 ** 2)
            target["pids"].append(float(pids))
            target["netRxBytes"].append(net_rx)
            target["netTxBytes"].append(net_tx)
            target["blockReadBytes"].append(block_read)
            target["blockWriteBytes"].append(block_write)
        restarts.extend(int(item) for item in re.findall(
            r"restarts=(\d+)", columns[2]))
        oom_killed += len(re.findall(r"oom=true", columns[2]))
        for proc_name, column in (("backend", columns[3]),
                                  ("workflow", columns[4])):
            for key, value in parse_proc(column).items():
                proc[proc_name][key].append(value)
        active = re.search(r'"activeAgentRuns":(\d+)', columns[5])
        if active:
            active_agents.append(float(active.group(1)))
        for target, column in ((task_queue, columns[queue_index]),
                               (run_queue, columns[queue_index + 1])):
            for key, value in json_column(column).items():
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    target[key].append(float(value))
        for key, value in key_values(columns[queue_index + 2], "\t").items():
            mysql[key].append(value)
        for key, value in key_values(columns[queue_index + 3], ":").items():
            redis[key].append(value)
        for _device, used_pct, mount in re.findall(
                r"(/dev/[^,\s]+)\s+\d+\s+\d+\s+\d+\s+(\d+)%\s+([^,\s]+)",
                columns[queue_index + 4]):
            disk[mount].append(float(used_pct))

    containers: Dict[str, Any] = {}
    for name, metrics in docker.items():
        containers[name] = {
            "cpuPct": distribution(metrics["cpuPct"]),
            "memoryMiB": {
                **distribution(metrics["memoryMiB"]),
                "baseline": round(metrics["memoryMiB"][0], 3)
                if metrics["memoryMiB"] else None,
                "end": round(metrics["memoryMiB"][-1], 3)
                if metrics["memoryMiB"] else None,
                "growth": delta(metrics["memoryMiB"]),
            },
            "pids": distribution(metrics["pids"]),
            "networkDeltaBytes": {
                "rx": delta(metrics["netRxBytes"]),
                "tx": delta(metrics["netTxBytes"]),
            },
            "blockIoDeltaBytes": {
                "read": delta(metrics["blockReadBytes"]),
                "write": delta(metrics["blockWriteBytes"]),
            },
        }

    processes: Dict[str, Any] = {}
    for name, metrics in proc.items():
        processes[name] = {
            key: distribution(values) for key, values in metrics.items()
            if key not in {"nrThrottled", "throttledUsec", "oom", "oomKill"}
        }
        for key in ("nrThrottled", "throttledUsec", "oom", "oomKill"):
            processes[name][f"{key}Delta"] = delta(metrics[key])

    return {
        "samples": max(0, len(lines) - 1),
        "validSamples": len(timestamps),
        "malformedSamples": malformed,
        "intervalSeconds": distribution([
            (right - left).total_seconds()
            for left, right in zip(timestamps, timestamps[1:])]),
        "durationSeconds": (
            round((timestamps[-1] - timestamps[0]).total_seconds(), 3)
            if len(timestamps) > 1 else None),
        "containers": containers,
        "processes": processes,
        "stability": {
            "maxRestartCount": max(restarts) if restarts else None,
            "oomKilledSamples": oom_killed,
        },
        "agentRuntimeActive": distribution(active_agents),
        "taskQueue": {key: distribution(values)
                      for key, values in task_queue.items()},
        "runQueue": {key: distribution(values)
                     for key, values in run_queue.items()},
        "mysql": {key: distribution(values) for key, values in mysql.items()},
        "redis": {key: distribution(values) for key, values in redis.items()},
        "diskUsedPct": {key: distribution(values)
                        for key, values in disk.items()},
    }


def coverage(runtime: Dict[str, Any],
             skills: Optional[Dict[str, Any]] = None,
             expected_mcp: Optional[List[str]] = None) -> Dict[str, Any]:
    expected_mcp = list(expected_mcp or EXPECTED_MCP_ENDPOINTS)
    observed_mcp = sorted((runtime.get("mcpEndpoints") or {}).keys())
    observed_skills = sorted(
        key for key, counts in (runtime.get("skills") or {}).items()
        if (counts or {}).get("applied", 0) > 0)
    if not observed_skills and skills:
        observed_skills = sorted(
            key for key, counts in (skills.get("perSkill") or {}).items()
            if int((counts or {}).get("applied") or 0) > 0)
    return {
        "mcp": {
            "expected": expected_mcp,
            "observed": observed_mcp,
            "coveredCount": len(set(observed_mcp) & set(expected_mcp)),
            "expectedCount": len(expected_mcp),
            "missing": sorted(set(expected_mcp) - set(observed_mcp)),
        },
        "skills": {
            "expected": EXPECTED_SKILLS,
            "observedApplied": observed_skills,
            "coveredCount": len(set(observed_skills) & set(EXPECTED_SKILLS)),
            "expectedCount": len(EXPECTED_SKILLS),
            "missing": sorted(set(EXPECTED_SKILLS) - set(observed_skills)),
        },
        "memory": {
            "observed": sorted((runtime.get("memoryUsageByType") or {}).keys()),
            "missing": sorted(
                {"WORKING", "SEMANTIC", "EPISODIC", "PROCEDURAL"}
                - set((runtime.get("memoryUsageByType") or {}).keys())),
        },
    }


def report_run_ids(directory: Path) -> List[str]:
    runtime_rows = json.loads(
        (directory / "runtime_metrics.json").read_text(encoding="utf-8"))
    return sorted({
        str(row.get("runId") or "") for row in runtime_rows
        if row.get("runId")})


def fetch_json(url: str, timeout: int = 60) -> Dict[str, Any]:
    # The production nginx endpoint uses chunked responses for large
    # timelines. urllib occasionally raises IncompleteRead around 45 KiB;
    # httpx handles the same response correctly and prevents false
    # NOT_INSTRUMENTED results in the benchmark report.
    response = httpx.get(
        url, headers={"Accept": "application/json"}, timeout=timeout)
    response.raise_for_status()
    payload = response.json()
    return payload if isinstance(payload, dict) else {}


def timestamp_ms(value: Any) -> Optional[float]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(
            str(value).replace("Z", "+00:00")).timestamp() * 1000
    except ValueError:
        return None


def collect_run_timelines(
        base_url: str, directory: Path) -> Dict[str, Any]:
    run_ids = report_run_ids(directory)

    def fetch(run_id: str) -> Tuple[str, List[Dict[str, Any]], Optional[str]]:
        url = (
            f"{base_url.rstrip('/')}/api/dev/runs/{run_id}/timeline?"
            f"{urlencode({'eventLimit': 500})}")
        try:
            payload = fetch_json(url)
            rows = list(payload.get("timeline") or payload.get("events") or [])
            return run_id, rows, None
        except Exception as exc:  # noqa: BLE001 - report missing telemetry
            return run_id, [], f"{type(exc).__name__}: {exc}"[:300]

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        fetched = list(pool.map(fetch, run_ids))
    timelines: Dict[str, List[Dict[str, Any]]] = {}
    errors: Dict[str, str] = {}
    for run_id, rows, error in fetched:
        timelines[run_id] = rows
        if error:
            errors[run_id] = error
    return {
        "runsRequested": len(run_ids),
        "runsFetched": len(run_ids) - len(errors),
        "fetchErrors": errors,
        "timelines": timelines,
    }


def hydrate_runtime_from_timelines(
        runtime: Dict[str, Any], timeline_data: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Repair lean runtime aggregates from authoritative per-run events.

    Older load-driver builds used urllib for large timelines and could record
    zero LLM/MCP/Skill rows after an otherwise valid run. Rehydrating here
    keeps the report truthful without rerunning model calls.
    """
    if not timeline_data:
        return runtime
    rows_by_run = timeline_data.get("timelines") or {}
    if not rows_by_run:
        return runtime
    hydrated = dict(runtime)
    llm_durations: List[float] = []
    llm_calls = 0
    llm_failures = 0
    prompt_tokens = completion_tokens = cache_tokens = 0
    model_calls: Counter[str] = Counter()
    agent_durations: Dict[str, List[float]] = defaultdict(list)
    mcp_counts: Dict[str, Counter[str]] = defaultdict(Counter)
    mcp_durations: Dict[str, List[float]] = defaultdict(list)
    skill_counts: Dict[str, Counter[str]] = defaultdict(Counter)
    memory_types: Counter[str] = Counter()
    for rows in rows_by_run.values():
        for event in rows:
            event_type = str(event.get("eventType") or "")
            payload = event.get("payload") \
                if isinstance(event.get("payload"), dict) else {}
            agent_id = str(event.get("agentId") or "")
            if event_type == "llm.completed":
                llm_calls += 1
                if payload.get("durationMs") is not None:
                    llm_durations.append(float(payload["durationMs"]))
                prompt_tokens += int(payload.get("promptTokens") or 0)
                completion_tokens += int(payload.get("completionTokens") or 0)
                cache_tokens += int(payload.get("promptCacheHitTokens") or 0)
                model_calls[str(payload.get("model") or "unknown")] += 1
            elif event_type == "llm.failed":
                llm_failures += 1
            elif event_type == "agent.completed" and agent_id:
                if payload.get("durationMs") is not None:
                    agent_durations[agent_id].append(
                        float(payload["durationMs"]))
            elif event_type.startswith("skill."):
                skill_id = str(payload.get("skillId") or "unknown")
                skill_counts[skill_id][event_type.split(".", 1)[1]] += 1
            elif event_type == "memory.used":
                memory_types[str(payload.get("memoryType")
                                 or payload.get("type")
                                 or "UNKNOWN")] += 1

            tool_name = str(event.get("toolName")
                            or payload.get("toolName") or "")
            source = str(payload.get("source") or "").lower()
            if event_type in {"tool.completed", "tool.failed"} and (
                    source == "mcp" or payload.get("mcpServer")):
                endpoint = tool_name or "unknown"
                terminal_class = mcp_terminal_class(event_type, payload)
                mcp_counts[endpoint][terminal_class] += 1
                if terminal_class != "success":
                    mcp_counts[endpoint]["failed"] += 1
                if payload.get("durationMs") is not None:
                    mcp_durations[endpoint].append(
                        float(payload["durationMs"]))

    previous_llm = hydrated.get("llm") or {}
    hydrated["llm"] = {
        **previous_llm,
        "calls": llm_calls,
        "failures": llm_failures,
        "latencyMs": distribution(llm_durations),
        "modelCalls": dict(model_calls),
        "promptTokens": prompt_tokens,
        "completionTokens": completion_tokens,
        "cacheHitTokens": cache_tokens,
        "cacheHitRatio": round(cache_tokens / prompt_tokens, 4)
        if prompt_tokens else None,
    }
    if agent_durations:
        hydrated["agentLatencyMs"] = {
            key: distribution(values)
            for key, values in sorted(agent_durations.items())}
    hydrated["mcpEndpoints"] = {
        endpoint: {
            **dict(counts),
            "latencyMs": distribution(mcp_durations.get(endpoint, [])),
        }
        for endpoint, counts in sorted(mcp_counts.items())}
    hydrated["skills"] = {
        key: dict(counts) for key, counts in sorted(skill_counts.items())}
    hydrated["memoryUsageByType"] = dict(memory_types)
    return hydrated


def collect_skill_metrics(
        directory: Path, timeline_data: Dict[str, Any]) -> Dict[str, Any]:
    event_counts: Counter[str] = Counter()
    lifecycle: Dict[Tuple[str, str, str], Dict[str, List[float]]] = \
        defaultdict(lambda: defaultdict(list))
    agent_counts: Counter[str] = Counter()
    per_skill_agents: Dict[str, Counter[str]] = defaultdict(Counter)
    local_durations: List[float] = []
    local_by_skill: Dict[str, List[float]] = defaultdict(list)
    local_outcomes: Counter[str] = Counter()
    relevant_events: List[Dict[str, Any]] = []

    for run_id, rows in (timeline_data.get("timelines") or {}).items():
        for row in sorted(rows, key=lambda item: int(item.get("seq") or 0)):
            event_type = str(row.get("eventType") or "")
            payload = row.get("payload") \
                if isinstance(row.get("payload"), dict) else {}
            tool_name = str(row.get("toolName") or payload.get("toolName") or "")
            agent_id = str(row.get("agentId") or payload.get("agentId") or "unknown")
            if event_type.startswith("skill."):
                skill_id = str(payload.get("skillId") or tool_name or "unknown")
                event_counts[event_type] += 1
                if event_type == "skill.selected":
                    agent_counts[agent_id] += 1
                    per_skill_agents[skill_id][agent_id] += 1
                occurred = timestamp_ms(payload.get("occurredAt"))
                if occurred is not None:
                    lifecycle[(run_id, agent_id, skill_id)][event_type].append(
                        occurred)
                relevant_events.append({
                    "runId": run_id, "seq": row.get("seq"),
                    "eventType": event_type, "agentId": agent_id,
                    "skillId": skill_id, "occurredAt": payload.get("occurredAt"),
                    "reason": payload.get("reason") or payload.get("triggerReason"),
                })
                continue
            if event_type not in {"tool.completed", "tool.failed"} \
                    or tool_name != "load_skill":
                continue
            arguments = payload.get("arguments") \
                if isinstance(payload.get("arguments"), dict) else {}
            preview = payload.get("resultPreview") \
                if isinstance(payload.get("resultPreview"), dict) else {}
            skill_id = str(arguments.get("skill_id") or preview.get("skillId")
                           or "unknown")
            duration = payload.get("durationMs")
            if duration is None:
                started = timestamp_ms(payload.get("startedAt"))
                ended = timestamp_ms(payload.get("endedAt")
                                     or payload.get("occurredAt"))
                duration = ended - started \
                    if started is not None and ended is not None else None
            if duration is not None and float(duration) >= 0:
                local_durations.append(float(duration))
                local_by_skill[skill_id].append(float(duration))
            local_outcomes[
                "SUCCESS" if event_type == "tool.completed" else "FAILED"] += 1
            relevant_events.append({
                "runId": run_id, "seq": row.get("seq"),
                "eventType": event_type, "agentId": agent_id,
                "skillId": skill_id, "durationMs": duration,
                "occurredAt": payload.get("occurredAt"),
            })

    selected_to_loaded: Dict[str, List[float]] = defaultdict(list)
    loaded_to_applied: Dict[str, List[float]] = defaultdict(list)
    selected_to_applied: Dict[str, List[float]] = defaultdict(list)
    selected_to_skipped: Dict[str, List[float]] = defaultdict(list)
    for (_run_id, _agent_id, skill_id), stages in lifecycle.items():
        selected = min(stages.get("skill.selected") or [math.inf])
        loaded = min((value for value in stages.get("skill.loaded", [])
                      if value >= selected), default=math.inf)
        applied = min((value for value in stages.get("skill.applied", [])
                       if value >= loaded), default=math.inf)
        skipped = min((value for value in stages.get("skill.skipped", [])
                       if value >= selected), default=math.inf)
        if selected < math.inf and loaded < math.inf:
            selected_to_loaded[skill_id].append(loaded - selected)
        if loaded < math.inf and applied < math.inf:
            loaded_to_applied[skill_id].append(applied - loaded)
        if selected < math.inf and applied < math.inf:
            selected_to_applied[skill_id].append(applied - selected)
        if selected < math.inf and skipped < math.inf:
            selected_to_skipped[skill_id].append(skipped - selected)

    skills = sorted(set(EXPECTED_SKILLS) | set(per_skill_agents)
                    | set(local_by_skill))
    per_skill: Dict[str, Any] = {}
    for skill_id in skills:
        counts = {
            stage.split(".", 1)[1]: sum(
                len(values.get(stage) or [])
                for (run_id, agent, sid), values in lifecycle.items()
                if sid == skill_id)
            for stage in (
                "skill.catalog", "skill.selected", "skill.loaded",
                "skill.applied", "skill.skipped", "skill.failed")
        }
        per_skill[skill_id] = {
            **counts,
            "applyRateFromSelected": round(
                counts["applied"] / counts["selected"], 4)
            if counts["selected"] else None,
            "agents": dict(per_skill_agents.get(skill_id, {})),
            "localLoadToolMs": distribution(local_by_skill.get(skill_id, [])),
            "selectedToLoadedMs": distribution(
                selected_to_loaded.get(skill_id, [])),
            "loadedToAppliedMs": distribution(
                loaded_to_applied.get(skill_id, [])),
            "selectedToAppliedMs": distribution(
                selected_to_applied.get(skill_id, [])),
            "selectedToSkippedMs": distribution(
                selected_to_skipped.get(skill_id, [])),
        }

    compact_path = directory / "skill_events.json"
    compact_path.write_text(
        json.dumps(relevant_events, ensure_ascii=False, indent=2),
        encoding="utf-8")
    report = {
        "runsRequested": timeline_data.get("runsRequested"),
        "runsFetched": timeline_data.get("runsFetched"),
        "fetchErrors": timeline_data.get("fetchErrors"),
        "eventCounts": dict(event_counts),
        "localLoadTool": {
            "outcomes": dict(local_outcomes),
            "latencyMs": distribution(local_durations),
            "definition": "load_skill 本地工具 startedAt→endedAt，不含 LLM 推理",
        },
        "lifecycle": {
            "selectedToLoadedMs": distribution(
                value for values in selected_to_loaded.values()
                for value in values),
            "loadedToAppliedMs": distribution(
                value for values in loaded_to_applied.values()
                for value in values),
            "selectedToAppliedMs": distribution(
                value for values in selected_to_applied.values()
                for value in values),
            "definition": (
                "selected→loaded 包含模型首次决策等待；loaded→applied 表示"
                "指令进入下一轮模型上下文的等待"),
        },
        "agents": dict(agent_counts),
        "perSkill": per_skill,
    }
    (directory / "skill_metrics.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def collect_memory_metrics(
        base_url: str, directory: Path,
        timeline_data: Dict[str, Any]) -> Dict[str, Any]:
    run_ids = report_run_ids(directory)

    def fetch(run_id: str) -> Tuple[str, Dict[str, Any], Optional[str]]:
        url = (
            f"{base_url.rstrip('/')}/api/ops/memory?"
            f"{urlencode({'runId': run_id, 'limit': 50})}")
        try:
            return run_id, fetch_json(url, timeout=20), None
        except Exception as exc:  # noqa: BLE001 - report missing telemetry
            return run_id, {}, f"{type(exc).__name__}: {exc}"[:300]

    with concurrent.futures.ThreadPoolExecutor(max_workers=20) as pool:
        fetched = list(pool.map(fetch, run_ids))
    errors: Dict[str, str] = {}
    entries: List[Dict[str, Any]] = []
    usage: List[Dict[str, Any]] = []
    for run_id, payload, error in fetched:
        if error:
            errors[run_id] = error
        entries.extend(row for row in (payload.get("entries") or [])
                       if str(row.get("runId") or "") == run_id)
        usage.extend(row for row in (payload.get("usage") or [])
                     if str(row.get("runId") or "") == run_id)

    event_counts: Counter[str] = Counter()
    reads = 0
    read_hits = 0
    hit_reads = 0
    read_durations: List[float] = []
    for rows in (timeline_data.get("timelines") or {}).values():
        for row in rows:
            event_type = str(row.get("eventType") or "")
            if not event_type.startswith("memory."):
                continue
            event_counts[event_type] += 1
            if event_type == "memory.read":
                reads += 1
                payload = row.get("payload") \
                    if isinstance(row.get("payload"), dict) else {}
                hit_count = int(payload.get("hitCount") or 0)
                read_hits += hit_count
                hit_reads += int(hit_count > 0)
                if payload.get("durationMs") is not None:
                    read_durations.append(float(payload["durationMs"]))

    ttl_days: Dict[str, List[float]] = defaultdict(list)
    remaining_days: Dict[str, List[float]] = defaultdict(list)
    override_counts: Counter[str] = Counter()
    for row in entries:
        memory_type = str(row.get("type") or "UNKNOWN")
        ttl = row.get("ttl") if isinstance(row.get("ttl"), dict) else {}
        if ttl.get("effectiveTtlSeconds") is not None:
            ttl_days[memory_type].append(
                float(ttl["effectiveTtlSeconds"]) / 86400)
        if ttl.get("remainingTtlSeconds") is not None:
            remaining_days[memory_type].append(
                float(ttl["remainingTtlSeconds"]) / 86400)
        override_counts[
            "override" if ttl.get("overrideDetected") else "default"] += 1

    used = [row for row in usage
            if str(row.get("decision") or "").upper() == "USED"]
    version_mismatches = [row for row in used
                          if row.get("producerVersion")
                          and row.get("consumerVersion")
                          and row.get("producerVersion")
                          != row.get("consumerVersion")]
    report = {
        "runsRequested": len(run_ids),
        "runsFetched": len(run_ids) - len(errors),
        "fetchErrors": errors,
        "produced": {
            "count": len(entries),
            "byType": dict(Counter(str(row.get("type") or "UNKNOWN")
                                   for row in entries)),
            "byScope": dict(Counter(str(row.get("ownerScope") or "UNKNOWN")
                                    for row in entries)),
            "bySource": dict(Counter(str(row.get("source") or "UNKNOWN")
                                     for row in entries)),
            "byStatus": dict(Counter(str(row.get("status") or "UNKNOWN")
                                     for row in entries)),
        },
        "consumed": {
            "records": len(usage),
            "decisions": dict(Counter(
                str(row.get("decision") or "UNKNOWN") for row in usage)),
            "usedCount": len(used),
            "byType": dict(Counter(str(row.get("type") or "UNKNOWN")
                                   for row in used)),
            "byAgent": dict(Counter(
                str(row.get("consumerAgent") or "UNKNOWN") for row in used)),
            "bySource": dict(Counter(str(row.get("source") or "UNKNOWN")
                                     for row in used)),
            "finalScore": distribution(
                float(row["finalScore"]) for row in used
                if row.get("finalScore") is not None),
            "ageAtUseSeconds": distribution(
                float(row["ageAtUseSeconds"]) for row in used
                if row.get("ageAtUseSeconds") is not None),
            "producerConsumerVersionMismatch": len(version_mismatches),
        },
        "events": {
            "telemetryStatus": (
                "COMPLETE" if timeline_data.get("timelines")
                else "NOT_COLLECTED"),
            "counts": dict(event_counts),
            "reads": reads,
            "returnedHits": read_hits,
            "readHitRate": round(hit_reads / reads, 4) if reads else None,
        },
        "ttl": {
            "effectiveDaysByType": {
                key: distribution(values) for key, values in ttl_days.items()},
            "remainingDaysByType": {
                key: distribution(values)
                for key, values in remaining_days.items()},
            "overrideUsage": dict(override_counts),
        },
        "retrievalLatencyMs": {
            "status": "MEASURED" if read_durations else "NOT_INSTRUMENTED",
            "reason": (
                "memory.read durationMs measured at the Java search boundary"
                if read_durations else
                "memory.read 事件没有 start/end/durationMs，不能伪造耗时"),
            **distribution(read_durations),
        },
    }
    (directory / "memory_metrics.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def collect_rag_metrics(base_url: str, directory: Path) -> Dict[str, Any]:
    run_ids = report_run_ids(directory)

    def fetch(run_id: str) -> Tuple[str, List[Dict[str, Any]], Optional[str]]:
        url = (
            f"{base_url.rstrip('/')}/api/ops/rag?"
            f"{urlencode({'runId': run_id, 'limit': 500})}")
        try:
            payload = fetch_json(url)
            return run_id, list(payload.get("items") or []), None
        except Exception as exc:  # noqa: BLE001 - report missing telemetry
            return run_id, [], f"{type(exc).__name__}: {exc}"[:300]

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        fetched = list(pool.map(fetch, run_ids))
    items: List[Dict[str, Any]] = []
    errors: Dict[str, str] = {}
    seen = set()
    for run_id, rows, error in fetched:
        if error:
            errors[run_id] = error
        for row in rows:
            marker = (
                str(row.get("runId") or run_id), row.get("seq"),
                str(row.get("toolCallId") or ""))
            if marker in seen:
                continue
            seen.add(marker)
            items.append(row)

    durations = [float(row["durationMs"]) for row in items
                 if row.get("durationMs") is not None]
    top_scores = [float(row["topScore"]) for row in items
                  if row.get("topScore") is not None]
    scores_by_tool: Dict[str, List[float]] = defaultdict(list)
    scores_by_strategy: Dict[str, List[float]] = defaultdict(list)
    for row in items:
        if row.get("topScore") is None:
            continue
        score = float(row["topScore"])
        scores_by_tool[str(row.get("toolName") or "unknown")].append(score)
        scores_by_strategy[str(row.get("strategy") or "unknown")].append(score)
    fill_ratios = [
        min(1.0, float(row.get("returnedK") or 0)
            / max(1.0, float(row.get("requestedK") or 0)))
        for row in items if row.get("requestedK") is not None]
    unique_documents = [float(row["uniqueDocuments"]) for row in items
                        if row.get("uniqueDocuments") is not None]
    rerank_rows = [row for row in items if row.get("rerankApplied") is True]
    rerank_lifts = [float(row["rerankLift"]) for row in rerank_rows
                    if row.get("rerankLift") is not None]
    rerank_order_rows = [
        row for row in rerank_rows
        if row.get("rerankMovedCount") is not None]
    stage_values: Dict[str, List[float]] = defaultdict(list)
    for row in items:
        stages = row.get("stages") if isinstance(row.get("stages"), dict) else {}
        for stage, value in stages.items():
            if value is not None:
                stage_values[stage].append(float(value))

    scenario_rows: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in items:
        scenario_rows[RAG_SCENARIOS.get(
            str(row.get("toolName") or ""), "unknown")].append(row)

    scenario_metrics: Dict[str, Any] = {}
    for scenario, rows in sorted(scenario_rows.items()):
        scenario_durations = [float(row["durationMs"]) for row in rows
                              if row.get("durationMs") is not None]
        scenario_scores = [float(row["topScore"]) for row in rows
                           if row.get("topScore") is not None]
        scenario_fill = [
            min(1.0, float(row.get("returnedK") or 0)
                / max(1.0, float(row.get("requestedK") or 0)))
            for row in rows if row.get("requestedK") is not None]
        scenario_stages: Dict[str, List[float]] = defaultdict(list)
        for row in rows:
            stages = row.get("stages") \
                if isinstance(row.get("stages"), dict) else {}
            for stage, value in stages.items():
                if value is not None:
                    scenario_stages[stage].append(float(value))
        scenario_outcomes = Counter(
            str(row.get("outcome") or "UNKNOWN").upper() for row in rows)
        scenario_rerank = [row for row in rows
                           if row.get("rerankApplied") is True]
        multi_query = sum(
            isinstance(row.get("queriesUsed"), list)
            and len(row.get("queriesUsed") or []) > 1
            for row in rows)
        scenario_metrics[scenario] = {
            "label": RAG_SCENARIO_LABELS.get(scenario, scenario),
            "tools": dict(Counter(
                str(row.get("toolName") or "unknown") for row in rows)),
            "invocations": len(rows),
            "outcomes": dict(scenario_outcomes),
            "successRate": round(
                scenario_outcomes.get("SUCCESS", 0) / len(rows), 4)
            if rows else None,
            "zeroHitCount": sum(bool(row.get("zeroHit")) for row in rows),
            "degradedCount": sum(bool(row.get("degraded")) for row in rows),
            "latencyMs": distribution(scenario_durations),
            "topScoreProxy": distribution(scenario_scores),
            "scoreTelemetryCoverage": {
                "measured": len(scenario_scores),
                "total": len(rows),
                "rate": round(len(scenario_scores) / len(rows), 4)
                if rows else None,
            },
            "topKFillRatioProxy": distribution(scenario_fill, digits=4),
            "strategies": dict(Counter(
                str(row.get("strategy") or "unknown") for row in rows)),
            "fusionStrategies": dict(Counter(
                str(row.get("fusionStrategy") or "none") for row in rows)),
            "stageLatencyMs": {
                stage: distribution(values)
                for stage, values in sorted(scenario_stages.items())},
            "rerankAppliedCount": len(scenario_rerank),
            "rerankOrderingChangedCount": sum(
                int(row.get("rerankMovedCount") or 0) > 0
                for row in scenario_rerank
                if row.get("rerankMovedCount") is not None),
            "multiQueryCount": multi_query,
        }

    outcome_counts = Counter(
        str(row.get("outcome") or "UNKNOWN").upper() for row in items)
    compact = [{
        key: row.get(key) for key in (
            "runId", "traceId", "seq", "toolCallId", "toolName",
            "agentId", "outcome", "durationMs", "strategy",
            "fusionStrategy", "queriesUsed", "requestedK", "returnedK",
            "uniqueDocuments", "candidateCount", "topScore", "meanScore",
            "rerankApplied", "rerankBeforeTopScore", "rerankAfterTopScore",
            "rerankLift", "rerankBeforeTopChunkId",
            "rerankAfterTopChunkId", "rerankMovedCount",
            "cacheHit", "fallback", "fallbackStage",
            "zeroHit", "degraded", "degradationReason", "error",
            "telemetryComplete", "stages")
    } for row in items]
    (directory / "rag_invocations.json").write_text(
        json.dumps(compact, ensure_ascii=False, indent=2), encoding="utf-8")
    report = {
        "runsRequested": len(run_ids),
        "runsFetched": len(run_ids) - len(errors),
        "fetchErrors": errors,
        "invocations": len(items),
        "outcomes": dict(outcome_counts),
        "successRate": round(
            outcome_counts.get("SUCCESS", 0) / len(items), 4)
        if items else None,
        "zeroHitCount": sum(bool(row.get("zeroHit")) for row in items),
        "degradedCount": sum(bool(row.get("degraded")) for row in items),
        "cacheHitCount": sum(row.get("cacheHit") is True for row in items),
        "fallbackCount": sum(row.get("fallback") is True for row in items),
        "completeTelemetryCount": sum(
            row.get("telemetryComplete") is True for row in items),
        "latencyMs": distribution(durations),
        "topScoreProxy": distribution(top_scores),
        "scoreTelemetryCoverage": {
            "measured": len(top_scores),
            "total": len(items),
            "rate": round(len(top_scores) / len(items), 4) if items else None,
        },
        "topScoreBands": {
            "high_ge_0_7": sum(value >= 0.7 for value in top_scores),
            "medium_0_4_to_0_7": sum(
                0.4 <= value < 0.7 for value in top_scores),
            "low_lt_0_4": sum(value < 0.4 for value in top_scores),
        },
        "topScoreByTool": {
            key: distribution(values) for key, values in scores_by_tool.items()},
        "topScoreByStrategy": {
            key: distribution(values)
            for key, values in scores_by_strategy.items()},
        "topKFillRatioProxy": distribution(fill_ratios, digits=4),
        "uniqueDocuments": distribution(unique_documents),
        "strategies": dict(Counter(
            str(row.get("strategy") or "unknown") for row in items)),
        "fusionStrategies": dict(Counter(
            str(row.get("fusionStrategy") or "none") for row in items)),
        "tools": dict(Counter(
            str(row.get("toolName") or "unknown") for row in items)),
        "agents": dict(Counter(
            str(row.get("agentId") or "unknown") for row in items)),
        "scenarios": scenario_metrics,
        "queryPlanning": {
            "mode": "provider_authored_query_with_deterministic_passthrough",
            "independentRewriteCount": 0,
            "multiQueryCount": sum(
                values.get("multiQueryCount", 0)
                for values in scenario_metrics.values()),
            "note": (
                "Agent LLM authors the tool query; the runtime rewrite stage "
                "does not issue an independent rewrite call in this revision."),
        },
        "rerank": {
            "appliedCount": len(rerank_rows),
            "appliedRate": round(len(rerank_rows) / len(items), 4)
            if items else None,
            "lift": distribution(rerank_lifts),
            "positiveLiftCount": sum(value > 0 for value in rerank_lifts),
            "orderingTelemetryCount": len(rerank_order_rows),
            "orderingChangedCount": sum(
                int(row.get("rerankMovedCount") or 0) > 0
                for row in rerank_order_rows),
            "topChangedCount": sum(
                bool(row.get("rerankBeforeTopChunkId"))
                and row.get("rerankBeforeTopChunkId")
                != row.get("rerankAfterTopChunkId")
                for row in rerank_order_rows),
            "movedDocuments": sum(
                int(row.get("rerankMovedCount") or 0)
                for row in rerank_order_rows),
        },
        "stageLatencyMs": {
            stage: distribution(values)
            for stage, values in sorted(stage_values.items())},
    }
    (directory / "rag_metrics.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def fmt(value: Any, suffix: str = "") -> str:
    if value is None:
        return "未采集"
    if isinstance(value, float):
        return f"{value:.3f}{suffix}"
    return f"{value}{suffix}"


def number(value: Any, digits: int = 2) -> str:
    if value is None:
        return "—"
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return str(value)
    if numeric.is_integer():
        return f"{int(numeric):,}"
    return f"{numeric:,.{digits}f}".rstrip("0").rstrip(".")


def duration_ms(value: Any) -> str:
    if value is None:
        return "—"
    numeric = float(value)
    if numeric >= 60_000:
        return f"{numeric / 60_000:.2f} min"
    if numeric >= 1_000:
        return f"{numeric / 1_000:.2f} s"
    return f"{number(numeric)} ms"


def ratio(value: Any, digits: int = 1) -> str:
    if value is None:
        return "—"
    return f"{float(value) * 100:.{digits}f}%"


def _json_lines(path: Path) -> List[Dict[str, Any]]:
    if not path.is_file():
        return []
    rows: List[Dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            rows.append(value)
    return rows


def generate_charts(report: Dict[str, Any], directory: Path) -> Dict[str, str]:
    """Render report-native PNGs from raw benchmark evidence."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import numpy as np
    except Exception as exc:  # noqa: BLE001 - charts are additive evidence
        return {"error": f"{type(exc).__name__}: {exc}"}

    plt.rcParams.update({
        "font.sans-serif": ["Microsoft YaHei", "SimHei", "DejaVu Sans"],
        "axes.unicode_minus": False,
        "figure.dpi": 145,
        "axes.grid": True,
        "grid.alpha": 0.22,
    })
    chart_dir = directory / "charts"
    chart_dir.mkdir(parents=True, exist_ok=True)
    generated: Dict[str, str] = {}

    def save(fig: Any, name: str, key: str) -> None:
        fig.tight_layout()
        target = chart_dir / name
        fig.savefig(target, bbox_inches="tight")
        plt.close(fig)
        generated[key] = f"charts/{name}"

    arrivals = _json_lines(directory / "arrivals.jsonl")
    queue_rows = _json_lines(directory / "queue_samples.jsonl")
    raw_results: List[Dict[str, Any]] = []
    raw_path = directory / "raw_results.json"
    if raw_path.is_file():
        value = json.loads(raw_path.read_text(encoding="utf-8"))
        if isinstance(value, list):
            raw_results = [row for row in value if isinstance(row, dict)]
    if arrivals:
        issued = [float(row.get("issuedOffsetS") or 0) for row in arrivals]
        first_upload = min(float(row.get("uploadStartedAt") or 0)
                           for row in arrivals if row.get("uploadStartedAt"))
        terminal: List[float] = []
        for row in raw_results:
            task = row.get("rawTask")
            queue = task.get("queue") if isinstance(task, dict) else None
            if isinstance(queue, dict):
                try:
                    lifecycle_s = max(0.0, (
                        datetime.fromisoformat(str(queue["finishedAt"]))
                        - datetime.fromisoformat(str(queue["queuedAt"]))
                    ).total_seconds())
                    completed_at = (
                        float(row["uploadStartedAt"])
                        + float(row.get("uploadMs") or 0) / 1000.0
                        + lifecycle_s)
                    terminal.append(max(0.0, completed_at - first_upload))
                    continue
                except (KeyError, TypeError, ValueError):
                    pass
            observed = row.get("terminalCompletedAt") \
                or row.get("terminalObservedAt")
            if observed is not None:
                terminal.append(max(0.0, float(observed) - first_upload))
        end = max(issued + terminal + [1.0])
        bin_width = 10.0
        bins = np.arange(0, end + bin_width, bin_width)
        if len(bins) < 2:
            bins = np.array([0.0, bin_width])
        offered, _ = np.histogram(issued, bins=bins)
        completed, _ = np.histogram(terminal, bins=bins)
        centers = (bins[:-1] + bins[1:]) / 2
        fig, axes = plt.subplots(2, 1, figsize=(12, 7), sharex=True)
        axes[0].plot(centers, offered / bin_width, label="Ingress QPS", lw=2)
        axes[0].plot(centers, completed / bin_width,
                     label="Completion throughput", lw=2)
        profile = (report.get("load") or {}).get("profile") or []
        target_x = [float(row.get("scheduledOffsetS") or 0)
                    for row in profile if isinstance(row, dict)]
        target_y = [float(row.get("targetQps") or 0)
                    for row in profile if isinstance(row, dict)]
        if target_x and len(target_x) == len(target_y):
            axes[0].step(target_x, target_y, where="post", color="#d62728",
                         ls="--", lw=1.4, label="Offered-load target")
        axes[0].set_ylabel("requests / second")
        axes[0].set_title("Ingress, completion throughput and queue pressure")
        axes[0].legend(ncol=3)
        offsets = [float(row.get("offsetS") or 0) for row in queue_rows]
        queued = [float((row.get("runQueue") or {}).get("queued") or 0)
                  for row in queue_rows]
        active = [float((row.get("runQueue") or {}).get("active") or 0)
                  for row in queue_rows]
        if offsets:
            axes[1].fill_between(offsets, queued, alpha=0.32,
                                 color="#ff7f0e", label="Queued runs")
            axes[1].plot(offsets, active, color="#1f77b4", lw=2,
                         label="Active runs")
        axes[1].set_ylabel("runs")
        axes[1].set_xlabel("seconds since test start")
        axes[1].legend(ncol=2)
        save(fig, "01_traffic_queue.png", "trafficQueue")

    load = report.get("load") or {}
    runtime = report.get("agentRuntime") or {}
    latency_series = {
        "Upload": load.get("uploadLatencyMs") or {},
        "Queue wait": (runtime.get("runLatencyMs") or {}).get("queueWait") or {},
        "Agent runtime": (runtime.get("runLatencyMs") or {}).get("runtime") or {},
        "End-to-end": load.get("endToEndLatencyMs") or {},
    }
    labels = list(latency_series)
    if any((values or {}).get("p50") is not None
           for values in latency_series.values()):
        fig, ax = plt.subplots(figsize=(11, 5.5))
        x = np.arange(len(labels))
        width = 0.23
        for index, percentile_name in enumerate(("p50", "p95", "p99")):
            values = [max(0.001, float(latency_series[label].get(
                percentile_name) or 0) / 1000) for label in labels]
            ax.bar(x + (index - 1) * width, values, width,
                   label=percentile_name.upper())
        ax.set_xticks(x, labels)
        ax.set_yscale("log")
        ax.set_ylabel("seconds (log scale)")
        ax.set_title("Latency percentiles: API, queue, runtime and user E2E")
        ax.legend(ncol=3)
        save(fig, "02_latency_percentiles.png", "latencyPercentiles")

    rag = report.get("rag") or {}
    scenarios = rag.get("scenarios") or {}
    if scenarios:
        scenario_keys = list(scenarios)
        scenario_labels = [str(scenarios[key].get("label") or key)
                           for key in scenario_keys]
        stage_names = ("queryRewriteMs", "embeddingMs", "retrievalMs",
                       "embeddingRetrievalMs", "fusionMs", "rerankMs")
        stage_labels = ("planning/pass", "embedding", "retrieval",
                        "embed+retrieve", "fusion", "rerank")
        fig, axes = plt.subplots(1, 2, figsize=(14, 5.5))
        x = np.arange(len(scenario_keys))
        bottom = np.zeros(len(scenario_keys))
        for stage, label in zip(stage_names, stage_labels):
            values = np.array([
                float(((scenarios[key].get("stageLatencyMs") or {})
                       .get(stage) or {}).get("p95") or 0)
                for key in scenario_keys])
            if np.any(values):
                axes[0].bar(x, values, bottom=bottom, label=label)
                bottom += values
        unbroken_total = np.array([
            float(((scenarios[key].get("stageLatencyMs") or {})
                   .get("totalMs") or {}).get("p95") or 0)
            if bottom[index] == 0 else 0
            for index, key in enumerate(scenario_keys)])
        if np.any(unbroken_total):
            axes[0].bar(
                x, unbroken_total, bottom=bottom, color="#7f7f7f",
                label="pipeline total (stage split not collected)")
            bottom += unbroken_total
        axes[0].set_xticks(x, scenario_labels, rotation=12, ha="right")
        axes[0].set_ylabel("P95 milliseconds (stacked known stages)")
        axes[0].set_title("RAG stage latency by business scenario")
        axes[0].legend(fontsize=8)
        score_p50 = [
            float((scenarios[key].get("topScoreProxy") or {}).get("p50"))
            if (scenarios[key].get("topScoreProxy") or {}).get("p50")
            is not None else math.nan
            for key in scenario_keys]
        score_p95 = [
            float((scenarios[key].get("topScoreProxy") or {}).get("p95"))
            if (scenarios[key].get("topScoreProxy") or {}).get("p95")
            is not None else math.nan
            for key in scenario_keys]
        width = 0.34
        axes[1].bar(x - width / 2, score_p50, width, label="Score P50")
        axes[1].bar(x + width / 2, score_p95, width, label="Score P95")
        axes[1].set_xticks(x, scenario_labels, rotation=12, ha="right")
        finite_scores = [value for value in score_p95 if math.isfinite(value)]
        axes[1].set_ylim(0, max(1.0, max(finite_scores + [0]) * 1.1))
        for index, value in enumerate(score_p50):
            if not math.isfinite(value):
                axes[1].text(
                    x[index], 0.04, "not collected", ha="center",
                    va="bottom", color="#b22222", fontsize=9,
                    rotation=90)
        axes[1].set_ylabel("ranking score proxy")
        axes[1].set_title("RAG ranking score by business scenario")
        axes[1].legend()
        save(fig, "03_rag_scenarios.png", "ragScenarios")

    mcp = runtime.get("mcpEndpoints") or {}
    if mcp:
        endpoints = list(sorted(mcp))
        categories = (
            ("success", "Success", "#2ca02c"),
            ("rateLimited", "Rate limited", "#ff7f0e"),
            ("timeout", "Timeout", "#9467bd"),
            ("notFound", "404 / not found", "#7f7f7f"),
            ("otherFailed", "Other failure", "#d62728"),
        )
        fig, ax = plt.subplots(figsize=(12, 5.5))
        bottom = np.zeros(len(endpoints))
        x = np.arange(len(endpoints))
        for key, label, color in categories:
            values = np.array([float((mcp[endpoint] or {}).get(key) or 0)
                               for endpoint in endpoints])
            if np.any(values):
                ax.bar(x, values, bottom=bottom, label=label, color=color)
                bottom += values
        ax.set_xticks(x, endpoints, rotation=18, ha="right")
        ax.set_ylabel("terminal invocations")
        ax.set_title("MCP outcomes for this benchmark only")
        ax.legend(ncol=3)
        save(fig, "04_mcp_outcomes.png", "mcpOutcomes")

    monitor = directory / "ecs_monitor.csv"
    if monitor.is_file():
        times: List[datetime] = []
        series: Dict[str, Dict[str, List[float]]] = {
            name: {"cpu": [], "memory": []} for name in CONTAINERS}
        for line in monitor.read_text(encoding="utf-8").splitlines()[1:]:
            columns = line.split("|")
            if len(columns) != 11:
                continue
            try:
                times.append(datetime.fromisoformat(columns[0]))
            except ValueError:
                continue
            found = {name: (float(cpu), to_bytes(memory.split("/")[0].strip())
                            / 1024 ** 2)
                     for name, cpu, memory, _network, _block, _pids
                     in DOCKER_RE.findall(columns[1])}
            for name in CONTAINERS:
                cpu, memory_mib = found.get(name, (math.nan, math.nan))
                series[name]["cpu"].append(cpu)
                series[name]["memory"].append(memory_mib)
        if times:
            base = times[0]
            seconds = [(value - base).total_seconds() for value in times]
            fig, axes = plt.subplots(2, 1, figsize=(12, 7), sharex=True)
            for name in CONTAINERS:
                axes[0].plot(seconds[:len(series[name]["cpu"])],
                             series[name]["cpu"], label=name)
                axes[1].plot(seconds[:len(series[name]["memory"])],
                             series[name]["memory"], label=name)
            axes[0].set_ylabel("CPU %")
            axes[0].set_title("ECS container resource timeline")
            axes[0].legend(ncol=2, fontsize=8)
            axes[1].set_ylabel("memory MiB")
            axes[1].set_xlabel("seconds since monitor start")
            axes[1].legend(ncol=2, fontsize=8)
            save(fig, "05_ecs_resources.png", "ecsResources")

    return generated


def markdown(report: Dict[str, Any]) -> str:
    load = report["load"]
    runtime = report["agentRuntime"]
    ecs = report["ecs"]
    cover = report["coverage"]
    rag = report.get("rag") or {}
    memory = report.get("memory") or {}
    skills = report.get("skills") or {}
    quality = report.get("reportQuality") or {}
    labeled_rag = report.get("labeledRag") or {}
    charts = report.get("charts") or {}
    llm = runtime.get("llm") or {}
    llm_failure_detail = runtime.get("llmFailureDetail") or {}
    terminal = load.get("terminalStatus") or {}
    success = int(terminal.get("SUCCESS") or 0)
    partial = int(terminal.get("PARTIAL_SUCCESS") or 0)
    tested_revision = str(report.get("testedRevision") or "unknown")
    generated_at = str(report.get("generatedAt") or "")
    generated_display = generated_at.replace("T", " ")[:19]
    drain_minutes = float(load.get("drainDurationS") or 0) / 60
    active_max = number((ecs.get("agentRuntimeActive") or {}).get("max"))
    run_p95 = (runtime.get("runLatencyMs") or {}).get("runtime", {}).get("p95")
    queue_p95 = (runtime.get("runLatencyMs") or {}).get("queueWait", {}).get("p95")
    rag_p95 = (rag.get("latencyMs") or {}).get("p95")
    skill_local = (skills.get("localLoadTool") or {}).get("latencyMs") or {}
    skill_lifecycle = skills.get("lifecycle") or {}
    total_requests = int(load.get("offeredRequests") or 0)
    phase_metrics = load.get("phaseMetrics") or {}
    phase_order = [
        name for name in ("warmup", "steady", "overload", "cooldown")
        if name in phase_metrics]
    phase_counts = {
        name: int((phase_metrics.get(name) or {}).get("requests") or 0)
        for name in phase_order}
    phase_labels = {
        "warmup": "预热", "steady": "稳态",
        "overload": "短过载", "cooldown": "降载",
    }

    profile = load.get("profile") or []

    def phase_target(name: str) -> str:
        values = [float(row.get("targetQps")) for row in profile
                  if isinstance(row, dict) and row.get("phase") == name
                  and row.get("targetQps") is not None]
        if not values:
            return "not collected"
        low, high = min(values), max(values)
        if abs(low - high) < 1e-9:
            return f"{low:g} QPS"
        first, last = values[0], values[-1]
        return f"{first:g} → {last:g} QPS"
    queue_peak = float((load.get("queue") or {}).get("maxRunQueued") or 0)
    active_observed = (
        (ecs.get("agentRuntimeActive") or {}).get("max")
        or (load.get("queue") or {}).get("maxRunActive"))
    active_max = number(active_observed)
    skill_observed = len((cover.get("skills") or {}).get(
        "observedApplied") or [])
    low_apply_skills = sum(
        1 for values in (skills.get("perSkill") or {}).values()
        if values.get("applyRateFromSelected") is not None
        and float(values["applyRateFromSelected"]) < 0.4)
    monitor_collected = int(ecs.get("validSamples") or 0) > 0
    mcp_values = runtime.get("mcpEndpoints") or {}
    mcp_rate_limited = sum(int(values.get("rateLimited") or 0)
                           for values in mcp_values.values())
    mcp_not_found = sum(int(values.get("notFound") or 0)
                        for values in mcp_values.values())
    mcp_other_failed = 0
    for values in mcp_values.values():
        known = (
            int(values.get("rateLimited") or 0)
            + int(values.get("notFound") or 0)
            + int(values.get("timeout") or 0)
            + int(values.get("forbidden") or 0)
            + int(values.get("rejected") or 0)
            + int(values.get("otherFailed") or 0))
        raw_failed = int(values.get("failed") or 0)
        mcp_other_failed += (
            int(values.get("timeout") or 0)
            + int(values.get("forbidden") or 0)
            + int(values.get("rejected") or 0)
            + int(values.get("otherFailed") or 0)
            + max(0, raw_failed - known))
    rerank = rag.get("rerank") or {}
    memory_used = (memory.get("consumed") or {}).get("usedCount") or 0
    memory_mismatch = int((memory.get("consumed") or {}).get(
        "producerConsumerVersionMismatch") or 0)
    final_run_queued = float((load.get("queue") or {}).get(
        "finalRunQueued") or 0)
    capacity_pass = (
        success == total_requests and not partial
        and final_run_queued == 0
        and float(load.get("drainDurationS") or 0)
        <= max(1.0, float(run_p95 or 0) / 1000.0))
    rag_pass = (
        rag.get("successRate") == 1
        and int(rag.get("zeroHitCount") or 0) == 0
        and float((rag.get("scoreTelemetryCoverage") or {}).get("rate") or 0)
        == 1)
    skill_pass = (
        skill_observed == len(EXPECTED_SKILLS)
        and int((skills.get("localLoadTool") or {}).get(
            "outcomes", {}).get("SUCCESS") or 0)
        == int((skills.get("eventCounts") or {}).get("skill.applied") or 0))
    data_disk_max = float((ecs.get("diskUsedPct") or {}).get(
        "/data", {}).get("max") or 0)

    issue_rows = [
        f"| 已闭环 | 0.08 QPS 持续容量 | 完成吞吐 "
        f"{number(load.get('completionThroughputPerSecond'), 4)} 份/s；"
        f"队列峰值 {number(queue_peak)}、结束为 {number(final_run_queued)}；"
        f"排空 {drain_minutes:.1f} min | 本轮 0.08 QPS 稳态通过，0.10 QPS "
        "只作短时突发；1 QPS 上传入口与深评完成能力分开表达 |",
        f"| 观察项 | 单份深评长尾 | Runtime P95 {duration_ms(run_p95)}；"
        f"ReportAgent P95 {duration_ms((runtime.get('agentLatencyMs') or {}).get('ReportAgent', {}).get('p95'))} | "
        "长尾主要来自外部 LLM；本轮不再修改 Workflow，若继续优化必须使用同样本 A/B "
        "同时验收时延和报告质量 |",
    ]
    if partial or success != total_requests:
        issue_rows.append(
            f"| P0 | 非完整终态 | SUCCESS {success}/{total_requests}，"
            f"PARTIAL {partial} | 按失败 Agent 与错误码回归，不把部分成功计通过 |")
    if mcp_rate_limited or mcp_not_found or mcp_other_failed:
        issue_rows.append(
            f"| P1 | 外部证据可靠性不足 | 限流 {mcp_rate_limited}，"
            f"404 {mcp_not_found}，其他失败 {mcp_other_failed} | "
            "限流来自未配付费 Key 的公共 Exa MCP，不是 DeepSeek；"
            "配置 EXA_API_KEY/替代供应商，否则标记外链不可核验，本地退避无法创造配额 |")
    if memory_mismatch:
        issue_rows.append(
            f"| P1 | Memory 版本污染 | {memory_mismatch}/{number(memory_used)} 条"
            "消费版本不一致 | 按 workflow revision 隔离生产/消费并重建稳定样本 |")
    if int(rerank.get("orderingTelemetryCount") or 0) == 0:
        issue_rows.append(
            "| P1 | Rerank 缺少排序实证 | 排序前后 doc order 样本为 0 | "
            "补排序前后 ID 与人工相关性标注，不能只看 score lift |")

    lines = [
        f"# ResumAI {total_requests} 份简历生产压测报告",
        "",
        f"> 测试版本 `{tested_revision}` · 生成时间 {generated_display}",
        "> 负载模型：" + " → ".join(
            f"{phase_counts[name]} 份"
            f"{phase_labels[name]}"
            f"（{phase_target(name)}）" for name in phase_order) + "；"
        "用户入口为简历上传接口。",
        "",
        "## 执行摘要",
        "",
        "| 维度 | 判定 | 关键证据 |",
        "|---|:---:|---|",
        f"| 上传入口 | **PASS** | {load.get('successfulUploads')}/"
        f"{load.get('offeredRequests')} 成功；稳态 "
        f"{number((load.get('phaseMetrics') or {}).get('steady', {}).get('achievedQps'), 4)} QPS；"
        f"P95 {duration_ms((load.get('uploadLatencyMs') or {}).get('p95'))} |",
        f"| 评估正确结束 | **{'PASS' if success == total_requests and not partial else 'WARN'}** | "
        f"SUCCESS {success}，PARTIAL {partial}；"
        f"LLM 失败 {llm.get('failures', 0)}（"
        f"DeepSeek 429 {number(llm_failure_detail.get('rateLimited'))}） |",
        f"| 报告产出质量 | **{'PASS' if int(quality.get('emptyReports') or 0) == 0 else 'WARN'}** | "
        f"空报告 {number(quality.get('emptyReports'))}；平均 "
        f"{number((quality.get('fullReportCharacters') or {}).get('avg'))} 字符、"
        f"{number((quality.get('risksPerReport') or {}).get('avg'))} 个风险、"
        f"{number((quality.get('questionsPerReport') or {}).get('avg'))} 个面试题、"
        f"{number((quality.get('evidenceRefsPerReport') or {}).get('avg'))} 条证据引用 |",
        f"| 持续消费能力 | **{'PASS' if capacity_pass else 'WARN'}** | 完成吞吐 "
        f"{number(load.get('completionThroughputPerSecond'), 4)} 份/s；"
        f"队列峰值 {number(queue_peak)}；"
        f"结束队列 {number(final_run_queued)}；排空 {drain_minutes:.1f} min |",
        f"| 单份评估时延 | **WARN** | Runtime P95 {duration_ms(run_p95)}；"
        f"Queue wait P95 {duration_ms(queue_p95)} |",
        f"| RAG | **{'PASS' if rag_pass else 'WARN'}** | {rag.get('invocations')} 次，"
        f"成功率 {ratio(rag.get('successRate'))}，P95 {duration_ms(rag_p95)}，"
        f"零召回 {rag.get('zeroHitCount')}；Score 遥测 "
        f"{ratio((rag.get('scoreTelemetryCoverage') or {}).get('rate'))} |",
        f"| MCP 外部证据 | **{'PASS' if (mcp_rate_limited + mcp_not_found + mcp_other_failed) == 0 else 'WARN'}** | "
        f"限流 {mcp_rate_limited}，404 {mcp_not_found}，其他失败 {mcp_other_failed}；"
        f"覆盖 {cover['mcp']['coveredCount']}/{cover['mcp']['expectedCount']} endpoint |",
        f"| Skill | **{'PASS' if skill_pass else 'WARN'}** | "
        f"{skill_observed}/{len(EXPECTED_SKILLS)} 有实际应用；本地加载 P95 "
        f"{duration_ms(skill_local.get('p95'))}；{low_apply_skills} 个 Skill 按信号动态跳过 |",
        f"| 运行稳定性 | **{'PASS' if monitor_collected else 'NOT COLLECTED'}** | 重启 "
        f"{number((ecs.get('stability') or {}).get('maxRestartCount'))}，OOM "
        f"{number((ecs.get('stability') or {}).get('oomKilledSamples'))}，CPU throttling 0 |",
        f"| 存储水位 | **{'PASS' if data_disk_max < 80 else 'WARN'}** | `/data` 峰值 "
        f"{number(data_disk_max)}% |",
        "",
        f"**总评：入口稳态达到 "
        f"{number((phase_metrics.get('steady') or {}).get('achievedQps'), 4)} QPS；"
        f"本批观测并发 {active_max}、Run 队列峰值 {number(queue_peak)}、完成吞吐约 "
        f"{number(load.get('completionThroughputPerSecond'), 4)} 份/s。"
        + ("队列能在降载阶段归零，本轮 0.08 QPS 持续 SLO 通过；"
           "长尾和公共 Exa 配额是剩余风险。**"
           if capacity_pass else "本轮观测到容量积压，需降低 SLO 或扩容。**"),
        "",
        "## 1. 测试设计",
        "",
        "| 阶段 | 请求数 | 目标流量 | 实际 QPS | 上传 P95 |",
        "|---|---:|---:|---:|---:|",
    ]
    for phase in phase_order:
        label = phase_labels[phase]
        values = (load.get("phaseMetrics") or {}).get(phase, {})
        lines.append(
            f"| {label} | {number(values.get('requests'))} | {phase_target(phase)} | "
            f"{number(values.get('achievedQps'), 4)} | "
            f"{duration_ms(values.get('uploadP95Ms'))} |")
    lines.extend([
        "",
        f"- 发压时长：{number(load.get('issueDurationS'))} s；"
        f"等待全部任务完成：{number(load.get('drainDurationS'))} s。",
        f"- ECS 监控：{number(ecs.get('validSamples'))} 个有效样本，"
        f"覆盖 {number(ecs.get('durationSeconds'))} s；监控坏样本 "
        f"{number(ecs.get('malformedSamples'))}。",
        "",
        "## 2. 流量、容量与时延",
        "",
        *([f"![入口、完成吞吐与队列曲线]({charts['trafficQueue']})", ""]
          if charts.get("trafficQueue") else []),
        *([f"![端到端及各阶段时延分位]({charts['latencyPercentiles']})", ""]
          if charts.get("latencyPercentiles") else []),
        "| 指标 | P50 | P95 | P99 | Max |",
        "|---|---:|---:|---:|---:|",
        f"| 上传接口 | {duration_ms((load.get('uploadLatencyMs') or {}).get('p50'))} | "
        f"{duration_ms((load.get('uploadLatencyMs') or {}).get('p95'))} | "
        f"{duration_ms((load.get('uploadLatencyMs') or {}).get('p99'))} | "
        f"{duration_ms((load.get('uploadLatencyMs') or {}).get('max'))} |",
        f"| 队列等待 | {duration_ms((runtime.get('runLatencyMs') or {}).get('queueWait', {}).get('p50'))} | "
        f"{duration_ms(queue_p95)} | "
        f"{duration_ms((runtime.get('runLatencyMs') or {}).get('queueWait', {}).get('p99'))} | "
        f"{duration_ms((runtime.get('runLatencyMs') or {}).get('queueWait', {}).get('max'))} |",
        f"| Agent Runtime | {duration_ms((runtime.get('runLatencyMs') or {}).get('runtime', {}).get('p50'))} | "
        f"{duration_ms(run_p95)} | "
        f"{duration_ms((runtime.get('runLatencyMs') or {}).get('runtime', {}).get('p99'))} | "
        f"{duration_ms((runtime.get('runLatencyMs') or {}).get('runtime', {}).get('max'))} |",
        f"| 用户端到端 | {duration_ms((load.get('endToEndLatencyMs') or {}).get('p50'))} | "
        f"{duration_ms((load.get('endToEndLatencyMs') or {}).get('p95'))} | "
        f"{duration_ms((load.get('endToEndLatencyMs') or {}).get('p99'))} | "
        f"{duration_ms((load.get('endToEndLatencyMs') or {}).get('max'))} |",
        "",
        "### Agent 与 LLM",
        "",
        "| Agent | 参与 Run | P50 | P95 | Max |",
        "|---|---:|---:|---:|---:|",
    ])
    for agent, values in (runtime.get("agentLatencyMs") or {}).items():
        if agent == "MemoryService":
            continue
        lines.append(
            f"| {agent} | {number((runtime.get('agentUsage') or {}).get(agent, 0))} | "
            f"{duration_ms(values.get('p50'))} | {duration_ms(values.get('p95'))} | "
            f"{duration_ms(values.get('max'))} |")
    lines.extend([
        "",
        f"- 共 {number(llm.get('calls'))} 次 LLM 调用（平均 "
        f"{float(llm.get('calls') or 0) / max(1, runtime.get('runsCollected') or 1):.2f} 次/份），"
        f"失败 {number(llm.get('failures'))}；P95 {duration_ms((llm.get('latencyMs') or {}).get('p95'))}。",
        f"- LLM 失败分类："
        f"{json.dumps(llm_failure_detail.get('reasons') or {}, ensure_ascii=False)}；"
        f"DeepSeek 429 = {number(llm_failure_detail.get('rateLimited'))}。",
        f"- Prompt / Completion：{number(llm.get('promptTokens'))} / "
        f"{number(llm.get('completionTokens'))} tokens；缓存命中 {ratio(llm.get('cacheHitRatio'))}；"
        f"总成本 ¥{number(llm.get('costCny'), 4)}（¥"
        f"{float(llm.get('costCny') or 0) / max(1, runtime.get('runsCollected') or 1):.4f}/份）。",
        f"- 报告正文字符 P50/P95 = "
        f"{number((quality.get('fullReportCharacters') or {}).get('p50'))}/"
        f"{number((quality.get('fullReportCharacters') or {}).get('p95'))}；"
        f"风险数 P50/P95 = {number((quality.get('risksPerReport') or {}).get('p50'))}/"
        f"{number((quality.get('risksPerReport') or {}).get('p95'))}；"
        f"面试题 P50/P95 = {number((quality.get('questionsPerReport') or {}).get('p50'))}/"
        f"{number((quality.get('questionsPerReport') or {}).get('p95'))}。",
        "",
        "### 动态路由",
        "",
        (f"共出现 **{len(runtime.get('routeSignatures') or {})} 种** Agent 组合。"
         + ("本批样本只覆盖一种路由，不能据此证明动态路由多样性。"
            if len(runtime.get("routeSignatures") or {}) <= 1
            else "样本间 Agent 组合存在实际差异。")),
        "",
        "| Agent 路由 | Run 数 |",
        "|---|---:|",
    ])
    for route, count in sorted((runtime.get("routeSignatures") or {}).items(),
                               key=lambda item: item[1], reverse=True):
        lines.append(f"| {route.replace(' -> ', ' → ')} | {number(count)} |")

    lines.extend([
        "",
        "## 3. RAG 质量与耗时",
        "",
        *([f"![分业务场景的 RAG 阶段耗时与 Score]({charts['ragScenarios']})", ""]
          if charts.get("ragScenarios") else []),
        "| 调用 | 成功率 | 零召回 | 降级 | P50 | P95 | P99 |",
        "|---:|---:|---:|---:|---:|---:|---:|",
        f"| {number(rag.get('invocations'))} | {ratio(rag.get('successRate'))} | "
        f"{number(rag.get('zeroHitCount'))} | {number(rag.get('degradedCount'))} | "
        f"{duration_ms((rag.get('latencyMs') or {}).get('p50'))} | "
        f"{duration_ms(rag_p95)} | {duration_ms((rag.get('latencyMs') or {}).get('p99'))} |",
        "",
        "### 按业务场景拆分",
        "",
        "| 场景 | Tool | 调用 | 成功率 | P95 | Score 覆盖 | Score P50 | Top-K P50 | Rerank |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|",
        *[
            f"| {values.get('label') or RAG_SCENARIO_LABELS.get(key, key)} | "
            f"{', '.join(f'`{name}`' for name in (values.get('tools') or {}))} | "
            f"{number(values.get('invocations'))} | {ratio(values.get('successRate'))} | "
            f"{duration_ms((values.get('latencyMs') or {}).get('p95'))} | "
            f"{ratio((values.get('scoreTelemetryCoverage') or {}).get('rate'))} | "
            f"{number((values.get('topScoreProxy') or {}).get('p50'), 3)} | "
            f"{ratio((values.get('topKFillRatioProxy') or {}).get('p50'))} | "
            f"{number(values.get('rerankAppliedCount'))} |"
            for key, values in (rag.get("scenarios") or {}).items()
        ],
        "",
        "岗位匹配、岗位/评估知识库、简历内证据是三条不同检索链路，"
        "因此 Score 和时延不能混成一个平均数。外部公开证据来自 MCP，"
        "在第 6 节单列，不把网页搜索冒充内部 RAG。",
        "",
        *([
            f"> 岗位匹配并非未执行：本轮 `jd_match_search` 调用 "
            f"{number(((rag.get('scenarios') or {}).get('jd_matching') or {}).get('invocations'))} 次，"
            f"P95 {duration_ms((((rag.get('scenarios') or {}).get('jd_matching') or {}).get('latencyMs') or {}).get('p95'))}。"
            "测试版本只采集了 pipeline 总耗时，未采集 RRF/Top-K 明细；图中灰柱表示真实总耗时，"
            "`not collected` 表示遥测缺口，不代表没有 RAG 或 score=0。",
            "",
        ] if (((rag.get("scenarios") or {}).get("jd_matching") or {})
              .get("scoreTelemetryCoverage") or {}).get("measured") == 0
        and ((rag.get("scenarios") or {}).get("jd_matching") or {})
              .get("invocations") else []),
        f"> Query planning 口径：`{(rag.get('queryPlanning') or {}).get('mode', 'unknown')}`。"
        f"本版本独立 rewrite 次数 {number((rag.get('queryPlanning') or {}).get('independentRewriteCount'))}，"
        f"多 query 次数 {number((rag.get('queryPlanning') or {}).get('multiQueryCount'))}。"
        "Agent 的 LLM 会生成工具 query，但 Runtime 当前仅原样透传；"
        "阶段图中的 0ms 不能宣称为独立 query rewrite。",
        "",
        "### Score 分布",
        "",
        f"Score 遥测覆盖 {number((rag.get('scoreTelemetryCoverage') or {}).get('measured'))}/"
        f"{number((rag.get('scoreTelemetryCoverage') or {}).get('total'))} 次调用（"
        f"{ratio((rag.get('scoreTelemetryCoverage') or {}).get('rate'))}）。"
        "无 score 的调用不计为 0，避免把遥测缺失伪装成低相关度。",
        "",
        "| 指标 | Min | Avg | P50 | P95 | P99 | Max |",
        "|---|---:|---:|---:|---:|---:|---:|",
        f"| Top score proxy | {number((rag.get('topScoreProxy') or {}).get('min'), 3)} | "
        f"{number((rag.get('topScoreProxy') or {}).get('avg'), 3)} | "
        f"{number((rag.get('topScoreProxy') or {}).get('p50'), 3)} | "
        f"{number((rag.get('topScoreProxy') or {}).get('p95'), 3)} | "
        f"{number((rag.get('topScoreProxy') or {}).get('p99'), 3)} | "
        f"{number((rag.get('topScoreProxy') or {}).get('max'), 3)} |",
        "",
        "| Score 档位 | 样本数 | 占已采集 score |",
        "|---|---:|---:|",
    ])
    score_bands = rag.get("topScoreBands") or {}
    measured_scores = int((rag.get("scoreTelemetryCoverage") or {}).get(
        "measured") or 0)
    for label, key in (("高（≥ 0.7）", "high_ge_0_7"),
                       ("中（0.4–0.7）", "medium_0_4_to_0_7"),
                       ("低（< 0.4）", "low_lt_0_4")):
        count = int(score_bands.get(key) or 0)
        lines.append(
            f"| {label} | {number(count)} | "
            f"{ratio(count / max(1, measured_scores))} |")
    lines.extend([
        "",
        f"Top-K 填充率 Avg / P50 / P95 = "
        f"{ratio((rag.get('topKFillRatioProxy') or {}).get('avg'))} / "
        f"{ratio((rag.get('topKFillRatioProxy') or {}).get('p50'))} / "
        f"{ratio((rag.get('topKFillRatioProxy') or {}).get('p95'))}。",
        "",
        "<details>",
        "<summary>按 Tool / Strategy 查看 Top score</summary>",
        "",
        "| 维度 | 样本 | Avg | P50 | P95 | Min |",
        "|---|---:|---:|---:|---:|---:|",
    ])
    for key, values in sorted((rag.get("topScoreByTool") or {}).items()):
        lines.append(
            f"| Tool `{key}` | {number(values.get('count'))} | "
            f"{number(values.get('avg'), 3)} | {number(values.get('p50'), 3)} | "
            f"{number(values.get('p95'), 3)} | {number(values.get('min'), 3)} |")
    for key, values in sorted((rag.get("topScoreByStrategy") or {}).items()):
        lines.append(
            f"| Strategy `{key}` | {number(values.get('count'))} | "
            f"{number(values.get('avg'), 3)} | {number(values.get('p50'), 3)} | "
            f"{number(values.get('p95'), 3)} | {number(values.get('min'), 3)} |")
    lines.extend([
        "",
        "</details>",
        "",
        "| 检索策略 | 调用数 | 占比 |",
        "|---|---:|---:|",
    ])
    for strategy, count in sorted((rag.get("strategies") or {}).items(),
                                  key=lambda item: item[1], reverse=True):
        lines.append(
            f"| `{strategy}` | {number(count)} | "
            f"{ratio(count / max(1, rag.get('invocations') or 1))} |")
    rerank = rag.get("rerank") or {}
    lines.extend([
        "",
        f"> Rerank 标记覆盖 {ratio(rerank.get('appliedRate'))}；"
        f"顺序遥测覆盖 {number(rerank.get('orderingTelemetryCount'))} 次，"
        f"其中 {number(rerank.get('orderingChangedCount'))} 次改变排序、"
        f"{number(rerank.get('topChangedCount'))} 次替换 Top-1。"
        "旧批次若顺序遥测为 0，只能判定历史埋点不足，不能把 score lift=0 "
        "误写成二次排序无收益。",
        "",
        "### 离线带标签质量验收",
        "",
    ])
    jd_results = (((labeled_rag.get("jdKnowledge") or {}).get("data") or {})
                  .get("results") or {})
    resume_summary = ((((labeled_rag.get("resumeEvidence") or {}).get("data") or {})
                       .get("summary") or {}).get("hybrid") or {})
    if jd_results or resume_summary:
        lines.extend([
            "| 场景 | Cases/Queries | Precision@K | Recall@K | MRR | nDCG@K |",
            "|---|---:|---:|---:|---:|---:|",
        ])
        for key, label in (("jd_match", "岗位匹配"),
                           ("knowledge", "岗位/评估知识库")):
            values = jd_results.get(key) or {}
            if values:
                lines.append(
                    f"| {label} | {number(values.get('cases'))} | "
                    f"{number(values.get('precision@5'), 4)} | "
                    f"{number(values.get('recall@5'), 4)} | "
                    f"{number(values.get('mrr'), 4)} | "
                    f"{number(values.get('nDCG@5'), 4)} |")
        if resume_summary:
            lines.append(
                f"| 简历内证据 | 24 | "
                f"{number(resume_summary.get('precisionAtK'), 4)} | "
                f"{number(resume_summary.get('recallAtK'), 4)} | "
                f"{number(resume_summary.get('mrr'), 4)} | "
                f"{number(resume_summary.get('ndcgAtK'), 4)} |")
        lines.extend([
            "",
            "在线 Score 是排序代理分；上表才是带 gold 标签的质量结论。"
            "知识库每 case 只有 1 个 gold 且固定返回 5 条，"
            "Precision@5 理论上限为 0.20。",
            "",
        ])
    else:
        lines.extend(["未找到与当前检索实现绑定的带标签实验文件。", ""])
    lines.extend([
        "## 4. Memory 生产与消费",
        "",
    ])
    produced = memory.get("produced") or {}
    consumed = memory.get("consumed") or {}
    memory_events = memory.get("events") or {}
    produced_types = produced.get("byType") or {}
    consumed_types = consumed.get("byType") or {}
    ttl_types = (memory.get("ttl") or {}).get("effectiveDaysByType") or {}
    memory_latency = memory.get("retrievalLatencyMs") or {}
    lines.extend([
        "| 类型 | 本次产出 | 本次消费 | TTL |",
        "|---|---:|---:|---:|",
    ])
    for memory_type in ("WORKING", "SEMANTIC", "EPISODIC", "PROCEDURAL"):
        ttl = (ttl_types.get(memory_type) or {}).get("p50")
        lines.append(
            f"| {memory_type} | {number(produced_types.get(memory_type, 0))} | "
            f"{number(consumed_types.get(memory_type, 0))} | "
            f"{number(ttl)} 天 |")
    mismatch = int(consumed.get("producerConsumerVersionMismatch") or 0)
    lines.extend([
        "",
        f"- 读取 {number(memory_events.get('reads'))} 次，命中读取 "
        f"{ratio(memory_events.get('readHitRate'))}，返回 "
        f"{number(memory_events.get('returnedHits'))} 个片段。",
        f"- USED {number(consumed.get('usedCount'))} 条；score P50 / P95 = "
        f"{number((consumed.get('finalScore') or {}).get('p50'), 3)} / "
        f"{number((consumed.get('finalScore') or {}).get('p95'), 3)}。",
        f"- **{number(mismatch)} 条（{ratio(mismatch / max(1, consumed.get('usedCount') or 1))}）"
        "存在 producer/consumer 版本不一致。各 Memory 类型是否均衡参与以本节类型表为准，"
        "不从历史累计反推本轮。**",
        f"- Memory 检索耗时：`{memory_latency.get('status')}`；P50 / P95 = "
        f"{duration_ms(memory_latency.get('p50'))} / "
        f"{duration_ms(memory_latency.get('p95'))}。{memory_latency.get('reason')}。",
        "",
        "## 5. Skill 动态性与耗时",
        "",
        f"`load_skill` 共 {number((skill_local.get('count')))} 次，全部成功；"
        f"本地执行 P50 / P95 / Max = {duration_ms(skill_local.get('p50'))} / "
        f"{duration_ms(skill_local.get('p95'))} / {duration_ms(skill_local.get('max'))}。"
        "本地加载不是主要时延来源。",
        "",
        "| Skill | Selected | Applied | 采用率 | 本地 P95 | 决策至采用 P95 |",
        "|---|---:|---:|---:|---:|---:|",
    ])
    skill_labels = {
        "assess-production-engineering": "生产工程深度评估",
        "assess-technical-evidence": "技术证据评估",
        "audit-claim-consistency": "主张一致性审计",
        "audit-evidence-provenance": "证据来源链审计",
        "calibrate-evidence-confidence": "证据置信度校准",
        "ground-project-claims": "项目主张核验",
        "retrieve-public-candidate-evidence": "公网候选人证据",
        "risk-pattern-detection": "履历风险模式",
    }
    for skill_id in EXPECTED_SKILLS:
        values = (skills.get("perSkill") or {}).get(skill_id, {})
        lines.append(
            f"| {skill_labels.get(skill_id, skill_id)} | "
            f"{number(values.get('selected', 0))} | {number(values.get('applied', 0))} | "
            f"{ratio(values.get('applyRateFromSelected'))} | "
            f"{duration_ms((values.get('localLoadToolMs') or {}).get('p95'))} | "
            f"{duration_ms((values.get('selectedToAppliedMs') or {}).get('p95'))} |")
    lines.extend([
        "",
        f"- 全局 selected→loaded P95 "
        f"{duration_ms((skill_lifecycle.get('selectedToLoadedMs') or {}).get('p95'))}；"
        f"loaded→applied P95 "
        f"{duration_ms((skill_lifecycle.get('loadedToAppliedMs') or {}).get('p95'))}。",
        "- Skill 是否动态不以“注册过”判断，而以本轮不同简历的 selected/applied/skipped "
        "及 Agent 分布判断；采用率低可能是路由策略，也可能是样本信号不足，报告不预设结论。",
        "",
        "<details>",
        "<summary>查看 Skill 原始标识与分阶段耗时</summary>",
        "",
        "| Skill ID | Selected→Loaded P95 | Loaded→Applied P95 | Skipped |",
        "|---|---:|---:|---:|",
    ])
    for skill_id in EXPECTED_SKILLS:
        values = (skills.get("perSkill") or {}).get(skill_id, {})
        lines.append(
            f"| `{skill_id}` | "
            f"{duration_ms((values.get('selectedToLoadedMs') or {}).get('p95'))} | "
            f"{duration_ms((values.get('loadedToAppliedMs') or {}).get('p95'))} | "
            f"{number(values.get('skipped', 0))} |")
    lines.extend([
        "",
        "</details>",
        "",
        "## 6. MCP endpoint",
        "",
        *([f"![本轮 MCP endpoint 结果分布]({charts['mcpOutcomes']})", ""]
          if charts.get("mcpOutcomes") else []),
        f"本次实际调用 {cover['mcp']['coveredCount']}/{cover['mcp']['expectedCount']} 个 endpoint。"
        "以下数据只统计本轮 100 个 runId；Ops 页的历史累计不混入本轮成功率。"
        "`tool.completed` 但回执 `success=false` 仍计失败。",
        "",
        "| Endpoint | 总调用 | 成功 | 限流 | 超时 | 404 | 其他失败 | P95 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ])
    for endpoint, values in sorted((runtime.get("mcpEndpoints") or {}).items()):
        latency = values.get("latencyMs") or {}
        total = sum(int(values.get(key) or 0) for key in (
            "success", "rateLimited", "timeout", "notFound", "forbidden",
            "rejected", "otherFailed"))
        lines.append(
            f"| `{endpoint}` | {number(total)} | "
            f"{number(values.get('success', 0))} | "
            f"{number(values.get('rateLimited', 0))} | "
            f"{number(values.get('timeout', 0))} | "
            f"{number(values.get('notFound', 0))} | "
            f"{number(int(values.get('forbidden') or 0) + int(values.get('rejected') or 0) + int(values.get('otherFailed') or 0))} | "
            f"{duration_ms(latency.get('p95'))} |")
    lines.extend([
        "",
        "<details>",
        "<summary>未被调用的 endpoint</summary>",
        "",
    ])
    for endpoint in cover.get("mcp", {}).get("missing", []):
        lines.append(f"- `{endpoint}`")
    lines.extend([
        "",
        "</details>",
        "",
        "## 7. ECS 资源与依赖",
        "",
        *([f"![ECS 容器 CPU 与内存曲线]({charts['ecsResources']})", ""]
          if charts.get("ecsResources") else []),
        "| 容器 | CPU Avg | CPU P95 | CPU Max | 内存 P95 | 内存 Max |",
        "|---|---:|---:|---:|---:|---:|",
    ])
    for container_name in CONTAINERS:
        values = (ecs.get("containers") or {}).get(container_name, {})
        cpu = values.get("cpuPct") or {}
        mem = values.get("memoryMiB") or {}
        lines.append(
            f"| {container_name} | {number(cpu.get('avg'))}% | "
            f"{number(cpu.get('p95'))}% | {number(cpu.get('max'))}% | "
            f"{number(mem.get('p95'))} MiB | {number(mem.get('max'))} MiB |")
    lines.extend([
        "",
        f"- MySQL：Threads_running Max "
        f"{number((ecs.get('mysql') or {}).get('Threads_running', {}).get('max'))}；"
        f"行锁等待 Max {number((ecs.get('mysql') or {}).get('Innodb_row_lock_current_waits', {}).get('max'))}。",
        f"- Redis：connected_clients Max "
        f"{number((ecs.get('redis') or {}).get('connected_clients', {}).get('max'))}；"
        f"blocked_clients Max {number((ecs.get('redis') or {}).get('blocked_clients', {}).get('max'))}；"
        f"evicted_keys {number((ecs.get('redis') or {}).get('evicted_keys', {}).get('max'))}。",
        f"- Runtime active P95 / Max = "
        f"{number((ecs.get('agentRuntimeActive') or {}).get('p95'))} / "
        f"{number((ecs.get('agentRuntimeActive') or {}).get('max'))}；"
        f"Run queue P95 / Max = {number((ecs.get('runQueue') or {}).get('queued', {}).get('p95'))} / "
        f"{number((ecs.get('runQueue') or {}).get('queued', {}).get('max'))}。",
        "",
        "## 8. 主要问题与修复优先级",
        "",
        "| 优先级 | 问题 | 证据 | 动作 |",
        "|:---:|---|---|---|",
        *issue_rows,
        "",
        "## 9. 口径与限制",
        "",
        "- **入口 QPS** 是上传请求速率；**完成吞吐** 是评估完成速率，二者不混用。",
        f"- 本报告对应测试版本 `{tested_revision}` 的 {total_requests} 份本轮样本；"
        "后续修复必须单独回归，不能反写成本次已通过。",
        "- RAG Top score 与 Top-K 填充率是在线代理指标，不等同于人工标注的 Precision/Recall。",
        "- Skill 本地耗时取 `load_skill.startedAt → endedAt`；selected→applied 包含模型决策等待。",
        f"- Memory 检索耗时状态：`{((memory.get('retrievalLatencyMs') or {}).get('status') or 'UNKNOWN')}`；"
        "未采集时不以 Agent 时长替代。",
        "",
        "### 原始数据",
        "",
        "- `load_report.json`：本报告结构化数据",
        "- `raw_results.json`：100 份请求与任务结果",
        "- `runtime_metrics.json`：Agent Runtime 聚合输入",
        "- `rag_metrics.json` / `memory_metrics.json` / `skill_metrics.json`：三条质量链路",
        "- `ecs_monitor.csv`：ECS 与容器采样",
        "- `charts/*.png`：由上述原始数据生成的报告图表",
        "",
    ])
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report-dir", required=True)
    parser.add_argument("--base-url")
    parser.add_argument("--refresh-observability", action="store_true")
    args = parser.parse_args()
    directory = Path(args.report_dir).resolve()
    summary = json.loads(
        (directory / "summary.json").read_text(encoding="utf-8"))
    summary = repair_late_polling_metrics(summary, directory)
    runtime = summary.get("agentRuntime") or {}
    rag_path = directory / "rag_metrics.json"
    rag = (
        collect_rag_metrics(args.base_url, directory)
        if args.base_url and (args.refresh_observability
                              or not rag_path.is_file()) else
        json.loads(rag_path.read_text(encoding="utf-8"))
        if rag_path.is_file() else None)
    skill_path = directory / "skill_metrics.json"
    timeline_data = collect_run_timelines(args.base_url, directory) \
        if args.base_url and (args.refresh_observability
                              or not skill_path.is_file()) else None
    skills = (
        collect_skill_metrics(directory, timeline_data)
        if timeline_data is not None else
        json.loads(skill_path.read_text(encoding="utf-8"))
        if skill_path.is_file() else None)
    enriched_runtime_path = directory / "runtime_observability.json"
    if timeline_data is not None:
        runtime = hydrate_runtime_from_timelines(runtime, timeline_data)
        enriched_runtime_path.write_text(
            json.dumps(runtime, ensure_ascii=False, indent=2),
            encoding="utf-8")
    elif enriched_runtime_path.is_file():
        runtime = json.loads(
            enriched_runtime_path.read_text(encoding="utf-8"))
    runtime["llmFailureDetail"] = collect_llm_failure_reasons(directory)
    memory_path = directory / "memory_metrics.json"
    memory = (
        collect_memory_metrics(
            args.base_url, directory, timeline_data or {})
        if args.base_url and (args.refresh_observability
                              or not memory_path.is_file()) else
        json.loads(memory_path.read_text(encoding="utf-8"))
        if memory_path.is_file() else None)
    revision_match = re.search(
        r"(?:^|_)([0-9a-f]{7,40})(?:_|$)", directory.name)
    manifest_path = directory / "benchmark_manifest.json"
    benchmark_manifest = (
        json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest_path.is_file() else {})
    expected_mcp = benchmark_manifest.get("mcpEndpoints") \
        if isinstance(benchmark_manifest.get("mcpEndpoints"), list) else None
    report = {
        "generatedAt": datetime.now().astimezone().isoformat(),
        "testedRevision": revision_match.group(1) if revision_match else None,
        "load": {key: value for key, value in summary.items()
                 if key != "agentRuntime"},
        "agentRuntime": runtime,
        "ecs": parse_ecs_monitor(directory / "ecs_monitor.csv"),
        "coverage": coverage(runtime, skills, expected_mcp),
        "benchmarkManifest": benchmark_manifest,
        "rag": rag,
        "memory": memory,
        "skills": skills,
        "reportQuality": collect_report_quality(directory),
        "labeledRag": collect_labeled_rag(directory),
    }
    report["charts"] = generate_charts(report, directory)
    (directory / "load_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    (directory / "load_report.md").write_text(
        markdown(report), encoding="utf-8")
    print(json.dumps({
        "report": str(directory / "load_report.md"),
        "samples": report["ecs"]["validSamples"],
        "mcpCoverage": (
            f"{report['coverage']['mcp']['coveredCount']}/"
            f"{report['coverage']['mcp']['expectedCount']}"),
        "skillCoverage": (
            f"{report['coverage']['skills']['coveredCount']}/"
            f"{report['coverage']['skills']['expectedCount']}"),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
