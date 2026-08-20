"""Matched small-sample A/B for ReportAgent streaming and TTFT.

This runs the current full workflow with identical resume fixtures. The only
switch is ``LLM_STREAM_REPORT_SECTIONS``. Streaming runs record provider TTFT
and the first field-validated report section; both variants retain the same
final merge/quality gate.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
from datetime import datetime
from pathlib import Path
from typing import Any, Optional, Sequence

from ecs_skill_eager_ab import _load_cases
from ecs_workflow_simulator import ROOT, simulate


DEFAULT_CASE_IDS = (
    "senior_backend_003",
    "ai_agent_engineer_013",
    "llm_rag_engineer_024",
    "junior_frontend_033",
    "security_backend_078",
    "sparse_risk_100",
)


def percentile(values: Sequence[float], fraction: float) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(float(value) for value in values)
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    upper = min(len(ordered) - 1, lower + 1)
    weight = position - lower
    return round(
        ordered[lower] + (ordered[upper] - ordered[lower]) * weight, 1)


def read_events(summary: dict[str, Any]) -> list[dict[str, Any]]:
    path = summary.get("eventLog")
    if not path:
        return []
    return json.loads(Path(str(path)).read_text(encoding="utf-8"))


def event_time(event: dict[str, Any]) -> Optional[datetime]:
    raw = str((event.get("payload") or {}).get("occurredAt") or "")
    if not raw:
        return None
    try:
        if raw.endswith("Z"):
            return datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if "." in raw:
            head, fraction = raw.split(".", 1)
            raw = f"{head}.{fraction[:6]}"
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def event_metrics(summary: dict[str, Any]) -> dict[str, Any]:
    events = read_events(summary)
    first_tokens = []
    completed_sections = []
    report_llm = []
    fallback_count = 0
    workflow_started = next(
        (event_time(event) for event in events if event_time(event)), None)
    first_token_times = []
    section_times = []
    for event in events:
        payload = event.get("payload") or {}
        event_type = event.get("eventType")
        if (event_type == "llm.first_token"
                and event.get("agentId") == "ReportAgent"):
            first_tokens.append({
                "purpose": payload.get("purpose"),
                "model": payload.get("model"),
                "ttftMs": int(payload.get("ttftMs") or 0),
                "outputKind": payload.get("outputKind"),
                "providerAttempt": payload.get("providerAttempt"),
            })
            occurred_at = event_time(event)
            if occurred_at is not None:
                first_token_times.append(occurred_at)
        elif event_type == "report.section.completed":
            completed_sections.append({
                "section": payload.get("section"),
                "attempt": payload.get("attempt"),
                "durationMs": int(payload.get("durationMs") or 0),
            })
            occurred_at = event_time(event)
            if occurred_at is not None:
                section_times.append(occurred_at)
        elif (event_type == "llm.completed"
              and event.get("agentId") == "ReportAgent"):
            report_llm.append({
                "purpose": payload.get("purpose"),
                "model": payload.get("model"),
                "durationMs": int(payload.get("durationMs") or 0),
                "ttftMs": payload.get("ttftMs"),
                "streamed": bool(payload.get("streamed")),
                "completionTokens": int(
                    payload.get("completionTokens") or 0),
            })
        elif (event_type == "run.progress"
              and payload.get("stage") == "parallel_report_fallback"):
            fallback_count += 1
    ttfts = [row["ttftMs"] for row in first_tokens if row["ttftMs"] > 0]
    section_ms = [
        row["durationMs"] for row in completed_sections
        if row["durationMs"] > 0]
    return {
        "providerFirstTokens": first_tokens,
        "providerTtftMinMs": min(ttfts) if ttfts else None,
        "providerTtftP50Ms": percentile(ttfts, 0.50),
        "providerTtftP95Ms": percentile(ttfts, 0.95),
        "workflowToFirstTokenMs": round(
            (min(first_token_times) - workflow_started).total_seconds()
            * 1000) if workflow_started and first_token_times else None,
        "completedSections": completed_sections,
        "firstValidatedSectionMs": min(section_ms) if section_ms else None,
        "workflowToFirstValidatedSectionMs": round(
            (min(section_times) - workflow_started).total_seconds()
            * 1000) if workflow_started and section_times else None,
        "reportLlmCalls": report_llm,
        "wholeReportFallbacks": fallback_count,
    }


def quality_guard(
        baseline: dict[str, Any], streaming: dict[str, Any], *,
        sparse: bool,
) -> list[str]:
    base = baseline.get("reportQuality") or {}
    stream = streaming.get("reportQuality") or {}
    failures: list[str] = []
    if baseline.get("status") != "SUCCEEDED":
        failures.append(f"baseline status={baseline.get('status')}")
    if streaming.get("status") != "SUCCEEDED":
        failures.append(f"stream status={streaming.get('status')}")
    floors = {
        "dimensions": 2 if sparse else 4,
        "dimensionEvidenceRefs": 4 if sparse else 6,
        "strengths": 1 if sparse else 2,
        "risks": 2 if sparse else 3,
        "questions": 4,
    }
    tolerances = {
        "dimensions": 0,
        "dimensionEvidenceRefs": 2,
        "strengths": 1,
        "risks": 1,
        "questions": 1,
    }
    for key, floor in floors.items():
        actual = int(stream.get(key) or 0)
        comparison_floor = max(
            floor, int(base.get(key) or 0) - tolerances[key])
        if actual < comparison_floor:
            failures.append(
                f"{key}={actual} below required {comparison_floor}")
    baseline_chars = int(base.get("answerChars") or 0)
    streaming_chars = int(stream.get("answerChars") or 0)
    if baseline_chars and streaming_chars < int(baseline_chars * 0.75):
        failures.append(
            f"answerChars={streaming_chars} below 75% of {baseline_chars}")
    return failures


async def run_ab(*, output: Path, case_ids: Sequence[str]) -> dict[str, Any]:
    rows = []
    cases = _load_cases(case_ids)
    artifact_dir = output.parent / f"{output.stem}-artifacts"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    original = os.environ.get("LLM_STREAM_REPORT_SECTIONS")
    try:
        for index, case in enumerate(cases):
            order = ["baseline", "streaming"]
            if index % 2:
                order.reverse()
            variants: dict[str, dict[str, Any]] = {}
            for variant in order:
                os.environ["LLM_STREAM_REPORT_SECTIONS"] = (
                    "0" if variant == "baseline" else "1")
                label = f"ttft-{case['id']}-{variant}"
                result = await simulate(
                    live=True,
                    context_log=artifact_dir / f"{label}.contexts.jsonl",
                    scenario=case["id"],
                    resume_text=case["resume"],
                    job_description=case["jd"],
                    run_label=label,
                    validate_contract=False,
                )
                result["streamMetrics"] = event_metrics(result)
                variants[variant] = result
            failures = quality_guard(
                variants["baseline"], variants["streaming"],
                sparse=case["id"] == "sparse_risk_100")
            rows.append({
                "caseId": case["id"],
                "role": case["role"],
                "baseline": variants["baseline"],
                "streaming": variants["streaming"],
                "qualityGuard": "PASS" if not failures else "FAIL",
                "qualityFailures": failures,
            })
    finally:
        if original is None:
            os.environ.pop("LLM_STREAM_REPORT_SECTIONS", None)
        else:
            os.environ["LLM_STREAM_REPORT_SECTIONS"] = original

    ttfts = [
        token["ttftMs"]
        for row in rows
        for token in row["streaming"]["streamMetrics"][
            "providerFirstTokens"]
        if token["ttftMs"] > 0
    ]
    first_sections = [
        row["streaming"]["streamMetrics"]["firstValidatedSectionMs"]
        for row in rows
        if row["streaming"]["streamMetrics"][
            "firstValidatedSectionMs"] is not None
    ]
    workflow_first_tokens = [
        row["streaming"]["streamMetrics"]["workflowToFirstTokenMs"]
        for row in rows
        if row["streaming"]["streamMetrics"][
            "workflowToFirstTokenMs"] is not None
    ]
    workflow_first_sections = [
        row["streaming"]["streamMetrics"][
            "workflowToFirstValidatedSectionMs"]
        for row in rows
        if row["streaming"]["streamMetrics"][
            "workflowToFirstValidatedSectionMs"] is not None
    ]
    baseline_elapsed = [row["baseline"]["elapsedMs"] for row in rows]
    streaming_elapsed = [row["streaming"]["elapsedMs"] for row in rows]
    summary = {
        "caseCount": len(rows),
        "reportSectionProviderCalls": len(ttfts),
        "providerTtftP50Ms": percentile(ttfts, 0.50),
        "providerTtftP95Ms": percentile(ttfts, 0.95),
        "providerTtftMaxMs": max(ttfts) if ttfts else None,
        "firstValidatedSectionP50Ms": percentile(first_sections, 0.50),
        "firstValidatedSectionP95Ms": percentile(first_sections, 0.95),
        "workflowToFirstTokenP50Ms": percentile(
            workflow_first_tokens, 0.50),
        "workflowToFirstTokenP95Ms": percentile(
            workflow_first_tokens, 0.95),
        "workflowToFirstSectionP50Ms": percentile(
            workflow_first_sections, 0.50),
        "workflowToFirstSectionP95Ms": percentile(
            workflow_first_sections, 0.95),
        "baselineRuntimeP50Ms": percentile(baseline_elapsed, 0.50),
        "baselineRuntimeP95Ms": percentile(baseline_elapsed, 0.95),
        "streamingRuntimeP50Ms": percentile(streaming_elapsed, 0.50),
        "streamingRuntimeP95Ms": percentile(streaming_elapsed, 0.95),
        "qualityPassCases": sum(
            row["qualityGuard"] == "PASS" for row in rows),
        "allRunsSucceeded": all(
            row[variant].get("status") == "SUCCEEDED"
            for row in rows for variant in ("baseline", "streaming")),
    }
    payload = {
        "experiment": "ReportAgent non-streaming vs OpenAI-compatible SSE streaming",
        "onlyChangedVariable": "LLM_STREAM_REPORT_SECTIONS",
        "summary": summary,
        "cases": rows,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(
        payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    lines = [
        "# ReportAgent Streaming TTFT A/B",
        "",
        f"- Samples: {summary['caseCount']} matched resume pairs",
        f"- Provider TTFT P50 / P95: **{summary['providerTtftP50Ms']} / "
        f"{summary['providerTtftP95Ms']} ms**",
        f"- First validated section P50 / P95: "
        f"**{summary['firstValidatedSectionP50Ms']} / "
        f"{summary['firstValidatedSectionP95Ms']} ms**",
        f"- Workflow start → first provider token P50 / P95: "
        f"**{summary['workflowToFirstTokenP50Ms']} / "
        f"{summary['workflowToFirstTokenP95Ms']} ms**",
        f"- Workflow start → first validated section P50 / P95: "
        f"**{summary['workflowToFirstSectionP50Ms']} / "
        f"{summary['workflowToFirstSectionP95Ms']} ms**",
        f"- Full runtime P50: {summary['baselineRuntimeP50Ms']} → "
        f"{summary['streamingRuntimeP50Ms']} ms",
        f"- Quality guard: {summary['qualityPassCases']}/{len(rows)} PASS",
        "",
        "| Case | TTFT min | First section | Runtime base → stream | Quality |",
        "|---|---:|---:|---:|:---:|",
    ]
    for row in rows:
        metrics = row["streaming"]["streamMetrics"]
        lines.append(
            f"| {row['caseId']} | {metrics['providerTtftMinMs']} ms | "
            f"{metrics['firstValidatedSectionMs']} ms | "
            f"{row['baseline']['elapsedMs']} → {row['streaming']['elapsedMs']} ms | "
            f"{row['qualityGuard']} |")
    output.with_suffix(".md").write_text(
        "\n".join(lines) + "\n", encoding="utf-8")
    return payload


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output", type=Path,
        default=ROOT / ".sim-artifacts" / "streaming-ttft-ab.json")
    parser.add_argument("--case-ids", default=",".join(DEFAULT_CASE_IDS))
    args = parser.parse_args()
    result = asyncio.run(run_ab(
        output=args.output,
        case_ids=tuple(
            value.strip() for value in args.case_ids.split(",")
            if value.strip())))
    print(json.dumps(result["summary"], ensure_ascii=False, indent=2))
