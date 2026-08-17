#!/usr/bin/env python3
"""Open-model load test for the real resume evaluation ingress and workflow.

Unlike ``run_stress.py`` (a closed-loop correctness batch), this driver issues
uploads on a wall-clock QPS schedule. Uploads and result polling are decoupled,
so 80-second evaluations cannot silently throttle the offered load.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import math
import os
import sys
import threading
import time
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
import run_stress as stress  # noqa: E402

TERMINAL = {"SUCCESS", "PARTIAL_SUCCESS", "FAILED", "CANCELLED", "SUPERSEDED"}
DEFAULT_PHASES = (
    ("warmup", 10, 0.20, 1.00),
    ("steady", 80, 1.00, 1.00),
    ("cooldown", 10, 1.00, 0.20),
)


def log(message: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


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


def server_queue_lifecycle_ms(detail: Dict[str, Any]) -> Optional[int]:
    """Return enqueue-to-finish time from the task service when available.

    The load driver intentionally issues all uploads before draining results.
    Therefore the time at which the client first observes a terminal status is
    not the task completion time.  The backend queue timestamps are the stable
    source for end-to-end latency and work even when polling starts late.
    """
    queue = detail.get("queue")
    if not isinstance(queue, dict):
        return None
    queued_at = queue.get("queuedAt")
    finished_at = queue.get("finishedAt")
    if not queued_at or not finished_at:
        return None
    try:
        elapsed = (
            datetime.fromisoformat(str(finished_at))
            - datetime.fromisoformat(str(queued_at))
        ).total_seconds() * 1000
    except (TypeError, ValueError):
        return None
    return max(0, int(elapsed))


def parse_phase(value: str) -> Tuple[str, int, float, float]:
    try:
        name, count, start_qps, end_qps = value.split(":", 3)
        parsed = (name, int(count), float(start_qps), float(end_qps))
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError(
            "phase must be name:count:start_qps:end_qps") from exc
    if parsed[1] <= 0 or parsed[2] <= 0 or parsed[3] <= 0:
        raise argparse.ArgumentTypeError("phase count and qps must be positive")
    return parsed


def build_schedule(
        phases: List[Tuple[str, int, float, float]], total: int,
) -> List[Dict[str, Any]]:
    schedule: List[Dict[str, Any]] = []
    offset = 0.0
    sequence = 0
    for name, count, start_qps, end_qps in phases:
        for index in range(count):
            if sequence >= total:
                return schedule
            fraction = index / max(1, count - 1)
            qps = start_qps + (end_qps - start_qps) * fraction
            schedule.append({
                "sequence": sequence + 1,
                "phase": name,
                "targetQps": qps,
                "scheduledOffsetS": offset,
            })
            offset += 1.0 / qps
            sequence += 1
    if sequence < total:
        raise ValueError(
            f"phase counts cover {sequence} requests, below requested {total}")
    return schedule


def append_jsonl(path: Path, row: Dict[str, Any], lock: threading.Lock) -> None:
    with lock:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def upload_one(rec: Dict[str, Any], slot: Dict[str, Any], started: float,
               arrivals_path: Path, write_lock: threading.Lock) -> Dict[str, Any]:
    issued = time.monotonic()
    row: Dict[str, Any] = {
        **{key: rec.get(key) for key in (
            "id", "name", "role", "fileType", "hasGithub", "textLength")},
        **slot,
        "issuedOffsetS": issued - started,
        "scheduleLagMs": int(
            max(0.0, issued - started - slot["scheduledOffsetS"]) * 1000),
        "uploadStartedAt": time.time(),
        "traceId": None,
        "uploadStatus": "FAILED",
        "uploadMs": None,
        "uploadError": None,
        "uploadAttempts": 0,
    }
    upload_started = time.monotonic()
    path = (ROOT / rec["path"]).resolve()
    for attempt in range(1, 3):
        row["uploadAttempts"] = attempt
        try:
            response = stress.upload_resume(
                path, rec.get("fileType", "txt"))
            row["traceId"] = response.get("traceId")
            if not row["traceId"]:
                raise RuntimeError("upload response has no traceId")
            row["uploadStatus"] = "SUCCESS"
            row["uploadError"] = None
            break
        except Exception as exc:  # noqa: BLE001 - load result
            row["uploadError"] = str(exc)[:500]
            # A curl timeout is ambiguous: the server may already have
            # created the task but the response body was too slow. Retrying
            # would create a duplicate task and corrupt the offered load.
            if attempt < 2 and "curl rc=28" not in str(exc):
                time.sleep(0.2)
            elif "curl rc=28" in str(exc):
                break
    row["uploadMs"] = int((time.monotonic() - upload_started) * 1000)
    row["uploadFinishedAt"] = time.time()
    append_jsonl(arrivals_path, row, write_lock)
    return row


def sample_queues(base: str, started: float, stop: threading.Event,
                  path: Path, write_lock: threading.Lock,
                  interval_s: float) -> None:
    while not stop.is_set():
        row: Dict[str, Any] = {
            "offsetS": round(time.monotonic() - started, 3),
            "capturedAt": time.time(),
        }
        try:
            row["taskQueue"] = stress.http_json(
                f"{base}/api/task-queue/status", timeout=5)
        except Exception as exc:  # noqa: BLE001
            row["taskQueueError"] = str(exc)[:300]
        try:
            row["runQueue"] = stress.http_json(
                f"{base}/api/runs/queue/status", timeout=5)
        except Exception as exc:  # noqa: BLE001
            row["runQueueError"] = str(exc)[:300]
        append_jsonl(path, row, write_lock)
        stop.wait(interval_s)


def poll_results(base: str, arrivals: List[Dict[str, Any]], started: float,
                 outdir: Path, poll_interval_s: float,
                 drain_timeout_s: float) -> List[Dict[str, Any]]:
    pending = {
        row["traceId"]: dict(row) for row in arrivals if row.get("traceId")}
    completed: Dict[str, Dict[str, Any]] = {}
    deadline = time.monotonic() + drain_timeout_s
    checkpoint = outdir / "checkpoint.json"
    last_log = 0.0
    while pending and time.monotonic() < deadline:
        trace_ids = list(pending)

        def fetch(trace_id: str) -> Tuple[
                str, Optional[Dict[str, Any]], Optional[str]]:
            try:
                return trace_id, stress.http_json(
                    f"{base}/api/tasks/{trace_id}", timeout=15), None
            except Exception as exc:  # noqa: BLE001
                return trace_id, None, str(exc)[:300]

        with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
            fetched = list(pool.map(fetch, trace_ids))
        for trace_id, detail, error in fetched:
            row = pending[trace_id]
            if error:
                row["lastPollError"] = error
                continue
            status = str((detail or {}).get("status") or "RUNNING")
            row["lastStatus"] = status
            if status not in TERMINAL:
                continue
            row["status"] = status
            row["terminalObservedAt"] = time.time()
            server_elapsed_ms = server_queue_lifecycle_ms(detail or {})
            if server_elapsed_ms is not None:
                row["endToEndMs"] = int(row.get("uploadMs") or 0) \
                    + server_elapsed_ms
                row["terminalCompletedAt"] = (
                    float(row["uploadStartedAt"])
                    + row["endToEndMs"] / 1000.0)
                row["completionTimestampSource"] = "server_queue_lifecycle"
            else:
                row["endToEndMs"] = int(
                    (row["terminalObservedAt"]
                     - row["uploadStartedAt"]) * 1000)
                row["terminalCompletedAt"] = row["terminalObservedAt"]
                row["completionTimestampSource"] = "client_observation"
            row["rawTask"] = detail
            completed[trace_id] = row
            pending.pop(trace_id, None)
        checkpoint.write_text(json.dumps({
            "completed": completed,
            "pending": pending,
            "updatedAt": time.time(),
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        now = time.monotonic()
        if now - last_log >= 20:
            counts: Dict[str, int] = {}
            for row in completed.values():
                status = str(row.get("status"))
                counts[status] = counts.get(status, 0) + 1
            log(f"DRAIN completed={len(completed)} pending={len(pending)} "
                f"status={counts}")
            last_log = now
        if pending:
            time.sleep(poll_interval_s)

    for trace_id, row in pending.items():
        row["status"] = "DRAIN_TIMEOUT"
        row["endToEndMs"] = None
        completed[trace_id] = row
    return list(completed.values())


def collect_runtime_metrics(
        base: str, results: List[Dict[str, Any]], outdir: Path,
) -> Dict[str, Any]:
    """Collect lean per-run Agent Runtime telemetry after load has drained."""
    targets = []
    for row in results:
        task = row.get("rawTask") if isinstance(row.get("rawTask"), dict) else {}
        run_id = task.get("workflowRunId")
        if run_id:
            targets.append((row.get("id"), str(run_id)))

    def fetch(target: Tuple[str, str]) -> Dict[str, Any]:
        resume_id, run_id = target
        out: Dict[str, Any] = {"resumeId": resume_id, "runId": run_id}
        try:
            listing = stress.http_json(
                f"{base}/api/ops/runs?runId={run_id}&limit=1", timeout=20)
            items = listing.get("items") or []
            out["run"] = items[0] if items else {}
        except Exception as exc:  # noqa: BLE001
            out["runError"] = str(exc)[:300]
        try:
            timeline = stress.http_json(
                f"{base}/api/dev/runs/{run_id}/timeline?eventLimit=500",
                timeout=30)
            out["events"] = timeline.get("timeline") or []
            out["eventsTruncated"] = bool(timeline.get("truncated"))
        except Exception as exc:  # noqa: BLE001
            out["timelineError"] = str(exc)[:300]
            out["events"] = []
        return out

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        rows = list(pool.map(fetch, targets))

    compact_rows: List[Dict[str, Any]] = []
    agent_durations: Dict[str, List[float]] = defaultdict(list)
    agent_runs: Counter = Counter()
    route_signatures: Counter = Counter()
    llm_durations: List[float] = []
    llm_provider_durations: List[float] = []
    llm_gate_wait_ms: List[float] = []
    permit_reacquire_wait_ms: List[float] = []
    llm_ttft_ms: List[float] = []
    llm_intervals: List[Tuple[float, float]] = []
    llm_model_durations: Dict[str, List[float]] = defaultdict(list)
    llm_model_provider_durations: Dict[str, List[float]] = defaultdict(list)
    llm_calls = 0
    llm_failures = 0
    llm_queued = 0
    streamed_calls = 0
    prompt_tokens = 0
    completion_tokens = 0
    cache_hit_tokens = 0
    model_calls: Counter = Counter()
    mcp_stats: Dict[str, Counter] = defaultdict(Counter)
    mcp_durations: Dict[str, List[float]] = defaultdict(list)
    skill_stats: Dict[str, Counter] = defaultdict(Counter)
    memory_types: Counter = Counter()
    memory_retrieved_types: Counter = Counter()
    memory_write_types: Counter = Counter()
    memory_hits = 0
    memory_reads = 0
    memory_misses = 0
    memory_write_candidates = 0
    retrieval_stats: Counter = Counter()
    retrieval_by_name: Dict[str, Counter] = defaultdict(Counter)
    retrieval_strategies: Dict[str, Counter] = defaultdict(Counter)
    retrieval_fallback_stages: Dict[str, Counter] = defaultdict(Counter)
    retrieval_durations: Dict[str, List[float]] = defaultdict(list)
    report_section_durations: List[float] = []
    orchestration_stages: Counter = Counter()
    repair_stats: Counter = Counter()
    degraded_reasons: Counter = Counter()
    total_cost_cny = 0.0
    evidence_ratios: List[float] = []
    jd_coverages: List[float] = []
    queue_wait_ms: List[float] = []
    runtime_ms: List[float] = []

    for row in rows:
        summary = row.get("run") if isinstance(row.get("run"), dict) else {}
        metrics = summary.get("metrics") if isinstance(summary.get("metrics"), dict) else {}
        if summary.get("queueWaitMs") is not None:
            queue_wait_ms.append(float(summary.get("queueWaitMs") or 0))
        if summary.get("runtimeMs") is not None:
            runtime_ms.append(float(summary.get("runtimeMs") or 0))
        agents = list(metrics.get("agentsUsed") or [])
        if agents:
            route_signatures[" -> ".join(agents)] += 1
            agent_runs.update(agents)
        for reason in metrics.get("degradedReasons") or []:
            degraded_reasons[str(reason)] += 1
        total_cost_cny += float(metrics.get("costCny") or 0.0)
        if metrics.get("evidenceSupportRatio") is not None:
            evidence_ratios.append(float(metrics["evidenceSupportRatio"]))
        if metrics.get("jdCoverage") is not None:
            jd_coverages.append(float(metrics["jdCoverage"]))

        compact_events = []
        for event in row.get("events") or []:
            event_type = str(event.get("eventType") or "")
            agent_id = str(event.get("agentId") or "")
            payload = event.get("payload") if isinstance(
                event.get("payload"), dict) else {}
            if event_type == "llm.completed":
                llm_calls += 1
                duration = float(payload.get("durationMs") or 0)
                reacquire_wait = float(
                    payload.get("agentExecutionReacquireWaitMs") or 0)
                provider_duration = (
                    float(payload.get("providerDurationMs") or 0)
                    if payload.get("providerDurationMs") is not None
                    else max(0.0, duration - reacquire_wait))
                post_provider_duration = max(
                    0.0, duration - provider_duration)
                llm_durations.append(duration)
                llm_provider_durations.append(provider_duration)
                model_name = str(payload.get("model") or "unknown")
                llm_model_durations[model_name].append(duration)
                llm_model_provider_durations[model_name].append(
                    provider_duration)
                occurred_at = payload.get("occurredAt")
                if occurred_at:
                    try:
                        ended_ms = datetime.fromisoformat(
                            str(occurred_at).replace("Z", "+00:00")
                        ).timestamp() * 1000.0
                        provider_ended_ms = ended_ms - post_provider_duration
                        llm_intervals.append((
                            provider_ended_ms - provider_duration,
                            provider_ended_ms,
                        ))
                    except (TypeError, ValueError):
                        pass
                llm_gate_wait_ms.append(float(payload.get("queueWaitMs") or 0))
                permit_reacquire_wait_ms.append(float(
                    payload.get("agentExecutionReacquireWaitMs") or 0))
                if payload.get("ttftMs") is not None:
                    llm_ttft_ms.append(float(payload.get("ttftMs") or 0))
                if payload.get("streamed"):
                    streamed_calls += 1
                prompt_tokens += int(payload.get("promptTokens") or 0)
                completion_tokens += int(payload.get("completionTokens") or 0)
                cache_hit_tokens += int(payload.get("promptCacheHitTokens") or 0)
                model_calls[model_name] += 1
            elif event_type == "llm.queued":
                llm_queued += 1
            elif event_type == "llm.failed":
                llm_failures += 1
            elif event_type == "agent.completed" and agent_id:
                agent_durations[agent_id].append(
                    float(payload.get("durationMs") or 0))
            elif event_type == "agent.failed":
                repair_stats["agent_failed"] += 1
            elif event_type.startswith("skill."):
                skill_id = str(payload.get("skillId") or "unknown")
                skill_stats[skill_id][event_type.split(".", 1)[1]] += 1
            elif event_type == "memory.used":
                memory_types[str(
                    payload.get("memoryType") or payload.get("type")
                    or "UNKNOWN")] += 1
            elif event_type == "memory.read":
                memory_reads += 1
                hit_count = int(payload.get("hitCount") or 0)
                memory_hits += hit_count
                memory_type = str(
                    payload.get("memoryType") or payload.get("type")
                    or "UNKNOWN")
                if hit_count:
                    memory_retrieved_types[memory_type] += hit_count
            elif event_type == "memory.missed":
                memory_misses += 1
            elif event_type == "memory.written":
                memory_write_candidates += 1
                memory_type = str(
                    payload.get("memoryType") or payload.get("type")
                    or "UNKNOWN")
                memory_write_types[memory_type] += 1
            elif event_type == "run.progress" and str(
                    payload.get("stage") or "") == "memory":
                memory_hits += int(payload.get("memoryHits") or 0)
                counts = payload.get("retrievedTypeCounts")
                if isinstance(counts, dict):
                    for memory_type, count in counts.items():
                        memory_retrieved_types[str(memory_type)] += int(count or 0)
            elif event_type == "agent.completed" and agent_id == "MemoryService":
                memory_write_candidates += int(payload.get("written") or 0)
            elif event_type == "report.section.completed":
                if payload.get("durationMs") is not None:
                    report_section_durations.append(float(
                        payload.get("durationMs") or 0))

            source = str(payload.get("source") or "").lower()
            server = str(payload.get("mcpServer") or "")
            tool_name = str(payload.get("toolName") or event.get("toolName") or "")
            if event_type in {"tool.completed", "tool.failed"} and (
                    source == "mcp" or server):
                endpoint = tool_name or f"{server}.unknown"
                raw_outcome = str(payload.get("outcome") or "").lower()
                outcome = (
                    "success" if event_type == "tool.completed"
                    and raw_outcome not in {"unavailable", "rate_limited"}
                    else raw_outcome or "failed")
                mcp_stats[endpoint][outcome] += 1
                if payload.get("durationMs") is not None:
                    mcp_durations[endpoint].append(
                        float(payload.get("durationMs") or 0))
            if event_type in {"tool.completed", "tool.failed"} and (
                    source == "retrieval" or tool_name in {
                        "knowledge_search", "resume_semantic_search"}):
                retrieval_stats[
                    "success" if event_type == "tool.completed" else "failed"] += 1
            stage = str(payload.get("stage") or "")
            if stage.startswith("langgraph."):
                orchestration_stages[stage] += 1
            if event_type in {"retrieval.completed", "retrieval.failed"}:
                name = str(payload.get("retrievalName") or tool_name
                           or "unknown")
                if name.startswith("retrieval."):
                    name = name[len("retrieval."):]
                outcome = "success" if event_type == "retrieval.completed" else "failed"
                retrieval_stats[outcome] += 1
                retrieval_by_name[name][outcome] += 1
                returned_k = int(payload.get("returnedK") or 0)
                if returned_k == 0:
                    retrieval_by_name[name]["zeroHit"] += 1
                if payload.get("fallback") or payload.get("fallbackStage"):
                    retrieval_by_name[name]["fallback"] += 1
                strategy = str(payload.get("strategy") or "")
                if strategy:
                    retrieval_strategies[name][strategy] += 1
                fallback_stage = str(payload.get("fallbackStage") or "")
                if fallback_stage:
                    retrieval_fallback_stages[name][fallback_stage] += 1
                stages = payload.get("stages") if isinstance(
                    payload.get("stages"), dict) else {}
                measured = stages.get("totalMs")
                if measured is None:
                    measured = stages.get("retrievalMs")
                if measured is not None:
                    retrieval_durations[name].append(float(measured or 0))
            if stage in {
                    "budget_reallocated", "parallel_report_retry",
                    "parallel_report_fallback",
                    "parallel_report_question_backfill",
                    "parallel_report_section_salvaged"}:
                repair_stats[stage] += 1
            if event_type in {"llm.failed", "agent.failed", "tool.failed"} \
                    or stage in {
                        "budget_reallocated", "parallel_report_fallback",
                        "parallel_report_question_backfill",
                        "parallel_report_section_salvaged"}:
                compact_events.append({
                    "seq": event.get("seq"), "eventType": event_type,
                    "agentId": agent_id, "stage": stage,
                    "error": str(payload.get("error") or "")[:240],
                })

        compact_rows.append({
            "resumeId": row.get("resumeId"),
            "runId": row.get("runId"),
            "status": summary.get("status"),
            "queueWaitMs": summary.get("queueWaitMs"),
            "runtimeMs": summary.get("runtimeMs"),
            "durationMs": summary.get("durationMs"),
            "metrics": metrics,
            "notableEvents": compact_events,
            "eventsTruncated": row.get("eventsTruncated"),
            "errors": [row.get("runError"), row.get("timelineError")],
        })

    (outdir / "runtime_metrics.json").write_text(
        json.dumps(compact_rows, ensure_ascii=False, indent=2),
        encoding="utf-8")

    def latency_summary(values: List[float]) -> Dict[str, Optional[float]]:
        return {
            "p50": percentile(values, 0.50),
            "p95": percentile(values, 0.95),
            "p99": percentile(values, 0.99),
            "max": max(values) if values else None,
        }

    def max_concurrency(intervals: List[Tuple[float, float]]) -> int:
        points: List[Tuple[float, int]] = []
        for started_at, ended_at in intervals:
            points.append((started_at, 1))
            points.append((ended_at, -1))
        active = 0
        peak = 0
        for _, delta in sorted(points, key=lambda item: (item[0], item[1])):
            active += delta
            peak = max(peak, active)
        return peak

    return {
        "runsCollected": len(rows),
        "runMetricErrors": sum(
            bool(row.get("runError") or row.get("timelineError"))
            for row in rows),
        "routeSignatures": dict(route_signatures),
        "agentUsage": dict(agent_runs),
        "agentLatencyMs": {
            agent: latency_summary(values)
            for agent, values in agent_durations.items()},
        "runLatencyMs": {
            "queueWait": latency_summary(queue_wait_ms),
            "runtime": latency_summary(runtime_ms),
        },
        "llm": {
            "calls": llm_calls,
            "failures": llm_failures,
            "latencyMs": latency_summary(llm_durations),
            "estimatedProviderLatencyMs": latency_summary(
                llm_provider_durations),
            "gateQueueWaitMs": latency_summary(llm_gate_wait_ms),
            "permitReacquireWaitMs": latency_summary(
                permit_reacquire_wait_ms),
            "ttftMs": latency_summary(llm_ttft_ms),
            "queuedCalls": llm_queued,
            "streamedCalls": streamed_calls,
            "observedMaxConcurrent": max_concurrency(llm_intervals),
            "modelCalls": dict(model_calls),
            "modelLatencyMs": {
                model_name: {
                    "total": latency_summary(values),
                    "estimatedProvider": latency_summary(
                        llm_model_provider_durations[model_name]),
                }
                for model_name, values in llm_model_durations.items()
            },
            "promptTokens": prompt_tokens,
            "completionTokens": completion_tokens,
            "cacheHitTokens": cache_hit_tokens,
            "cacheHitRatio": round(
                cache_hit_tokens / prompt_tokens, 4) if prompt_tokens else None,
            "costCny": round(total_cost_cny, 4),
        },
        "mcpEndpoints": {
            endpoint: {
                **dict(counts),
                "latencyMs": latency_summary(mcp_durations[endpoint]),
            }
            for endpoint, counts in mcp_stats.items()},
        "skills": {skill: dict(counts) for skill, counts in skill_stats.items()},
        "memoryUsageByType": dict(memory_types),
        "memory": {
            "reads": memory_reads,
            "misses": memory_misses,
            "hits": memory_hits,
            "retrievedTypeCounts": dict(memory_retrieved_types),
            "writeCandidates": memory_write_candidates,
            "writtenTypeCounts": dict(memory_write_types),
        },
        "retrieval": dict(retrieval_stats),
        "retrievalByName": {
            name: {
                **dict(counts),
                "strategies": dict(retrieval_strategies[name]),
                "fallbackStages": dict(retrieval_fallback_stages[name]),
                "latencyMs": latency_summary(retrieval_durations[name]),
            }
            for name, counts in retrieval_by_name.items()
        },
        "reportSections": {
            "completed": len(report_section_durations),
            "latencyMs": latency_summary(report_section_durations),
        },
        "orchestrationStages": dict(orchestration_stages),
        "repairsAndFailures": dict(repair_stats),
        "degradedReasons": dict(degraded_reasons),
        "evidenceSupportRatio": latency_summary(evidence_ratios),
        "jdCoverage": latency_summary(jd_coverages),
    }


def summarize_queue_samples(path: Path) -> Dict[str, Any]:
    rows = []
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    task_queue = [
        row["taskQueue"] for row in rows
        if isinstance(row.get("taskQueue"), dict)]
    run_queue = [
        row["runQueue"] for row in rows
        if isinstance(row.get("runQueue"), dict)]

    def maximum(source: List[Dict[str, Any]], key: str) -> Optional[float]:
        values = [float(row.get(key) or 0) for row in source]
        return max(values) if values else None

    return {
        "samples": len(rows),
        "sampleErrors": sum(
            bool(row.get("taskQueueError") or row.get("runQueueError"))
            for row in rows),
        "maxTaskQueued": maximum(task_queue, "queued"),
        "maxTaskRunning": maximum(task_queue, "running"),
        "maxPendingMessages": maximum(task_queue, "pendingMessages"),
        "maxActiveWorkers": maximum(task_queue, "activeWorkers"),
        "maxWorkerUtilization": maximum(task_queue, "workerUtilization"),
        "maxRunQueued": maximum(run_queue, "queued"),
        "maxRunActive": maximum(run_queue, "active"),
        "maxOldestWaitSeconds": maximum(task_queue, "oldestWaitSeconds"),
    }


def summarize(arrivals: List[Dict[str, Any]], results: List[Dict[str, Any]],
              schedule: List[Dict[str, Any]], started: float,
              issue_finished: float,
              runtime_metrics: Dict[str, Any],
              queue_metrics: Dict[str, Any]) -> Dict[str, Any]:
    uploads = [row for row in arrivals if row.get("uploadStatus") == "SUCCESS"]
    upload_ms = [row["uploadMs"] for row in arrivals if row.get("uploadMs") is not None]
    e2e_ms = [row["endToEndMs"] for row in results if row.get("endToEndMs")]
    status_counts: Dict[str, int] = {}
    for row in results:
        status = str(row.get("status") or "UNKNOWN")
        status_counts[status] = status_counts.get(status, 0) + 1
    per_phase: Dict[str, Dict[str, Any]] = {}
    for phase in dict.fromkeys(slot["phase"] for slot in schedule):
        rows = [row for row in arrivals if row.get("phase") == phase]
        if not rows:
            continue
        span = max(0.001, max(row["issuedOffsetS"] for row in rows)
                   - min(row["issuedOffsetS"] for row in rows))
        per_phase[phase] = {
            "requests": len(rows),
            "successfulUploads": sum(
                row.get("uploadStatus") == "SUCCESS" for row in rows),
            "achievedQps": round((len(rows) - 1) / span, 4)
            if len(rows) > 1 else None,
            "uploadP95Ms": percentile(
                [row["uploadMs"] for row in rows
                 if row.get("uploadMs") is not None], 0.95),
        }
    issue_span = max(0.001, issue_finished - started)
    upload_start_times = [
        float(row["uploadStartedAt"]) for row in arrivals
        if row.get("uploadStartedAt")]
    upload_finish_times = [
        float(row["uploadFinishedAt"]) for row in arrivals
        if row.get("uploadFinishedAt")]
    terminal_times = [
        float(row.get("terminalCompletedAt") or row["terminalObservedAt"])
        for row in results if row.get("terminalCompletedAt")
        or row.get("terminalObservedAt")]
    first_upload = min(upload_start_times) if upload_start_times else None
    last_upload = max(upload_finish_times) if upload_finish_times else None
    last_terminal = max(terminal_times) if terminal_times else None
    drain_duration = (
        max(0.0, last_terminal - last_upload)
        if last_terminal is not None and last_upload is not None else None)
    observation_span = (
        max(0.001, last_terminal - first_upload)
        if last_terminal is not None and first_upload is not None else None)
    return {
        "profile": schedule,
        "offeredRequests": len(arrivals),
        "successfulUploads": len(uploads),
        "uploadFailures": len(arrivals) - len(uploads),
        "achievedIngressQps": round(len(arrivals) / issue_span, 4),
        "issueDurationS": round(issue_span, 3),
        "drainDurationS": (
            round(drain_duration, 3) if drain_duration is not None else None),
        "completionThroughputPerSecond": (
            round(len(e2e_ms) / observation_span, 4)
            if observation_span is not None else None),
        "phaseMetrics": per_phase,
        "uploadLatencyMs": {
            "p50": percentile(upload_ms, 0.50),
            "p95": percentile(upload_ms, 0.95),
            "p99": percentile(upload_ms, 0.99),
            "max": max(upload_ms) if upload_ms else None,
        },
        "terminalStatus": status_counts,
        "endToEndLatencyMs": {
            "p50": percentile(e2e_ms, 0.50),
            "p95": percentile(e2e_ms, 0.95),
            "p99": percentile(e2e_ms, 0.99),
            "max": max(e2e_ms) if e2e_ms else None,
        },
        "agentRuntime": runtime_metrics,
        "queue": queue_metrics,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--allow-insecure-http", action="store_true")
    parser.add_argument("--outdir", required=True)
    parser.add_argument("--count", type=int, default=100)
    parser.add_argument(
        "--ids", nargs="+", default=None,
        help="run these manifest ids in the supplied order; overrides --count",
    )
    parser.add_argument("--phase", action="append", type=parse_phase)
    parser.add_argument("--upload-workers", type=int, default=8)
    parser.add_argument("--poll-interval", type=float, default=5.0)
    parser.add_argument("--sample-interval", type=float, default=5.0)
    parser.add_argument("--drain-timeout", type=float, default=3600.0)
    parser.add_argument(
        "--hr-id", default=None,
        help="isolated X-HR-Id namespace for benchmark tasks and memory",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if (args.base_url.startswith("http://") and not args.allow_insecure_http
            and "127.0.0.1" not in args.base_url
            and "localhost" not in args.base_url):
        parser.error("plain HTTP target requires --allow-insecure-http")
    manifest_all = json.loads(stress.MANIFEST.read_text(encoding="utf-8"))
    if args.ids:
        by_id = {str(row["id"]): row for row in manifest_all}
        missing = [resume_id for resume_id in args.ids if resume_id not in by_id]
        if missing:
            parser.error("unknown manifest ids: " + ", ".join(missing))
        manifest = [by_id[resume_id] for resume_id in args.ids]
    else:
        manifest = manifest_all[:args.count]
        if len(manifest) != args.count:
            raise RuntimeError(
                f"manifest has {len(manifest)} rows, requested {args.count}")
    request_count = len(manifest)
    phases = list(args.phase or DEFAULT_PHASES)
    schedule = build_schedule(phases, request_count)
    if args.dry_run:
        print(json.dumps({
            "requests": len(schedule),
            "plannedIssueDurationS": round(
                schedule[-1]["scheduledOffsetS"], 3),
            "phases": phases,
        }, ensure_ascii=False, indent=2))
        return 0

    outdir = Path(args.outdir).resolve()
    outdir.mkdir(parents=True, exist_ok=False)
    stress.BASE = args.base_url.rstrip("/")
    stress.AUTH_TOKEN = os.getenv("RESUMAI_AUTH_TOKEN", "")
    stress.HR_ID = args.hr_id or (
        "loadtest-" + time.strftime("%Y%m%d-%H%M%S"))
    benchmark_manifest: Dict[str, Any] = {
        "capturedAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "hrId": stress.HR_ID,
        "mcpEndpoints": [],
    }
    try:
        mcp_snapshot = stress.http_json(
            f"{stress.BASE}/api/ops/mcp?probe=false&recentLimit=1",
            timeout=30)
        benchmark_manifest["mcpEndpoints"] = sorted(
            str(name) for name in (mcp_snapshot.get("availableTools") or []))
        benchmark_manifest["mcpServers"] = sorted(
            str(server.get("name"))
            for server in (mcp_snapshot.get("servers") or [])
            if isinstance(server, dict) and server.get("name"))
    except Exception as exc:  # noqa: BLE001 - benchmark may still proceed
        benchmark_manifest["mcpInventoryError"] = (
            f"{type(exc).__name__}: {exc}")[:300]
    (outdir / "benchmark_manifest.json").write_text(
        json.dumps(benchmark_manifest, ensure_ascii=False, indent=2),
        encoding="utf-8")
    write_lock = threading.Lock()
    arrivals_path = outdir / "arrivals.jsonl"
    queue_path = outdir / "queue_samples.jsonl"
    stop_sampler = threading.Event()
    started = time.monotonic()
    sampler = threading.Thread(
        target=sample_queues,
        args=(stress.BASE, started, stop_sampler, queue_path,
              write_lock, args.sample_interval),
        daemon=True)
    sampler.start()

    log(f"OPEN-LOOP start requests={request_count} "
        f"plannedIssueDuration={schedule[-1]['scheduledOffsetS']:.1f}s")
    futures = []
    with concurrent.futures.ThreadPoolExecutor(
            max_workers=args.upload_workers) as pool:
        for rec, slot in zip(manifest, schedule):
            deadline = started + slot["scheduledOffsetS"]
            remaining = deadline - time.monotonic()
            if remaining > 0:
                time.sleep(remaining)
            futures.append(pool.submit(
                upload_one, rec, slot, started, arrivals_path, write_lock))
        arrivals = [future.result() for future in futures]
    issue_finished = time.monotonic()
    log(f"ISSUE done={len(arrivals)} elapsed={issue_finished-started:.1f}s "
        f"upload_ok={sum(row['uploadStatus']=='SUCCESS' for row in arrivals)}")

    results = poll_results(
        stress.BASE, arrivals, started, outdir,
        args.poll_interval, args.drain_timeout)
    stop_sampler.set()
    sampler.join(timeout=args.sample_interval + 2)
    log("COLLECT Agent Runtime / LLM / MCP / Skill / Memory telemetry")
    runtime_metrics = collect_runtime_metrics(
        stress.BASE, results, outdir)
    queue_metrics = summarize_queue_samples(queue_path)
    summary = summarize(
        arrivals, results, schedule, started, issue_finished,
        runtime_metrics, queue_metrics)
    (outdir / "raw_results.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    (outdir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"DONE terminal={summary['terminalStatus']} "
        f"ingress_qps={summary['achievedIngressQps']}")
    return 0 if not any(
        status in summary["terminalStatus"]
        for status in ("FAILED", "PARTIAL_SUCCESS", "DRAIN_TIMEOUT")) else 2


if __name__ == "__main__":
    raise SystemExit(main())
