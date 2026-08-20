"""Run a 12-resume, same-input Skill disclosure A/B on the ECS host.

Baseline keeps provider-selected progressive ``load_skill`` calls.  Treatment
eagerly injects only Skills named by ``--eager-ids`` *after* normal signal
selection.  The script alternates variant order to reduce time/provider bias and
writes both machine-readable results and a compact Markdown decision table.

Use the same docker command documented in ``ecs_workflow_simulator.py`` and add:

  scripts/ecs_skill_eager_ab.py --live --output /workspace/.sim-artifacts/skill-eager-ab.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
from pathlib import Path
from typing import Any, Sequence

from ecs_workflow_simulator import ROOT, simulate


DEFAULT_CASE_IDS = (
    "senior_backend_003",
    "ai_agent_engineer_013",
    "llm_rag_engineer_024",
    "algorithm_ml_067",
    "data_platform_054",
    "devops_sre_060",
    "junior_frontend_033",
    "senior_frontend_038",
    "mobile_engineer_084",
    "new_grad_088",
    "security_backend_078",
    "sparse_risk_100",
)


def _load_cases(case_ids: Sequence[str] = DEFAULT_CASE_IDS) -> list[dict[str, Any]]:
    manifest_path = ROOT.parent / "testdata" / "stress_resumes" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    by_id = {str(item.get("id")): item for item in manifest}
    cases = []
    for case_id in case_ids:
        item = by_id.get(case_id)
        if not item:
            raise RuntimeError(f"missing benchmark case: {case_id}")
        resume_path = ROOT.parent / str(item["path"])
        if resume_path.suffix.lower() != ".txt":
            raise RuntimeError(f"A/B requires text fixture: {resume_path}")
        expected = ", ".join(str(v) for v in item.get("expectedSkills") or [])
        cases.append({
            "id": case_id,
            "role": str(item.get("role") or "目标岗位"),
            "resume": resume_path.read_text(encoding="utf-8"),
            "jd": (
                f"招聘{item.get('role') or '研发工程师'}；重点评估岗位相关技术深度、"
                f"项目 ownership、可验证结果和风险。参考技能：{expected}。"
            ),
        })
    return cases


def _percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, round((len(ordered) - 1) * fraction)))
    return float(ordered[index])


def _quality_guard(baseline: dict[str, Any], treatment: dict[str, Any]) -> list[str]:
    base = baseline.get("reportQuality") or {}
    eager = treatment.get("reportQuality") or {}
    failures: list[str] = []
    if baseline.get("status") != "SUCCEEDED":
        failures.append(f"baseline status={baseline.get('status')}")
    if treatment.get("status") != "SUCCEEDED":
        failures.append(f"eager status={treatment.get('status')}")
    if not int(base.get("dimensions") or 0):
        failures.append("baseline structured report has no dimensions")
    if not int(eager.get("dimensions") or 0):
        failures.append("eager structured report has no dimensions")
    minimums = {
        "dimensions": 0,
        "dimensionEvidenceRefs": 1,
        "strengths": 1,
        "risks": 1,
        "questions": 1,
    }
    for key, tolerance in minimums.items():
        if int(eager.get(key) or 0) < max(0, int(base.get(key) or 0) - tolerance):
            failures.append(
                f"{key} {eager.get(key, 0)} < baseline {base.get(key, 0)}-{tolerance}")
    base_chars = int(base.get("answerChars") or 0)
    if base_chars and int(eager.get("answerChars") or 0) < int(base_chars * 0.75):
        failures.append(
            f"answerChars {eager.get('answerChars', 0)} < 75% of {base_chars}")
    return failures


async def run_ab(*, live: bool, eager_ids: str, output: Path,
                 case_ids: Sequence[str] = DEFAULT_CASE_IDS) -> dict[str, Any]:
    cases = _load_cases(case_ids)
    rows: list[dict[str, Any]] = []
    artifact_dir = output.parent / (output.stem + "-artifacts")
    artifact_dir.mkdir(parents=True, exist_ok=True)
    original = os.environ.get("SKILL_EAGER_IDS")
    try:
        for index, case in enumerate(cases):
            variants = ["baseline", "eager"]
            if index % 2:
                variants.reverse()
            results: dict[str, dict[str, Any]] = {}
            for variant in variants:
                os.environ["SKILL_EAGER_IDS"] = "" if variant == "baseline" else eager_ids
                label = f"skill-ab-{case['id']}-{variant}"
                results[variant] = await simulate(
                    live=live,
                    context_log=(artifact_dir / f"{label}.contexts.jsonl"),
                    scenario=case["id"],
                    resume_text=case["resume"],
                    job_description=case["jd"],
                    run_label=label,
                    validate_contract=False,
                )
            baseline = results["baseline"]
            eager = results["eager"]
            failures = _quality_guard(baseline, eager)
            rows.append({
                "caseId": case["id"],
                "role": case["role"],
                "baseline": baseline,
                "eager": eager,
                "llmCallDelta": eager["llmCalls"] - baseline["llmCalls"],
                "elapsedDeltaMs": eager["elapsedMs"] - baseline["elapsedMs"],
                "qualityGuard": "PASS" if not failures else "FAIL",
                "qualityFailures": failures,
            })
    finally:
        if original is None:
            os.environ.pop("SKILL_EAGER_IDS", None)
        else:
            os.environ["SKILL_EAGER_IDS"] = original

    baseline_ms = [float(row["baseline"]["elapsedMs"]) for row in rows]
    eager_ms = [float(row["eager"]["elapsedMs"]) for row in rows]
    baseline_calls = sum(int(row["baseline"]["llmCalls"]) for row in rows)
    eager_calls = sum(int(row["eager"]["llmCalls"]) for row in rows)
    payload = {
        "experiment": "signal-selected Skill progressive vs configured eager disclosure",
        "liveProvider": live,
        "eagerIds": [item.strip() for item in eager_ids.split(",") if item.strip()],
        "caseCount": len(rows),
        "summary": {
            "baselineLlmCalls": baseline_calls,
            "eagerLlmCalls": eager_calls,
            "llmCallReductionPct": round(
                (baseline_calls - eager_calls) * 100 / baseline_calls, 2)
                if baseline_calls else 0.0,
            "baselineLatencyP50Ms": round(statistics.median(baseline_ms)),
            "eagerLatencyP50Ms": round(statistics.median(eager_ms)),
            "baselineLatencyP95Ms": round(_percentile(baseline_ms, 0.95)),
            "eagerLatencyP95Ms": round(_percentile(eager_ms, 0.95)),
            "qualityPassCases": sum(
                1 for row in rows if row["qualityGuard"] == "PASS"),
            "allRunsSucceeded": all(
                row[variant].get("status") == "SUCCEEDED"
                for row in rows for variant in ("baseline", "eager")),
            "decision": "PROMOTE" if all(
                row["qualityGuard"] == "PASS" for row in rows)
                and eager_calls < baseline_calls
                and statistics.median(eager_ms) < statistics.median(baseline_ms)
                else "KEEP_PROGRESSIVE",
        },
        "cases": rows,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8")
    md_path = output.with_suffix(".md")
    summary = payload["summary"]
    lines = [
        "# Skill Disclosure A/B",
        "",
        f"- Decision: **{summary['decision']}**",
        f"- LLM calls: {summary['baselineLlmCalls']} → {summary['eagerLlmCalls']} "
        f"({summary['llmCallReductionPct']}% reduction)",
        f"- Runtime P50: {summary['baselineLatencyP50Ms']} ms → "
        f"{summary['eagerLatencyP50Ms']} ms",
        f"- Runtime P95: {summary['baselineLatencyP95Ms']} ms → "
        f"{summary['eagerLatencyP95Ms']} ms",
        f"- Structural quality guard: {summary['qualityPassCases']}/{len(rows)} PASS",
        "",
        "| Case | Role | LLM Δ | Runtime Δ(ms) | Quality |",
        "|---|---|---:|---:|:---:|",
    ]
    lines.extend(
        f"| {row['caseId']} | {row['role']} | {row['llmCallDelta']} | "
        f"{row['elapsedDeltaMs']} | {row['qualityGuard']} |"
        for row in rows
    )
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return payload


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true")
    parser.add_argument(
        "--eager-ids",
        default="assess-technical-evidence,ground-project-claims")
    parser.add_argument(
        "--output", type=Path,
        default=ROOT / ".sim-artifacts" / "skill-eager-ab.json")
    parser.add_argument(
        "--case-ids", default=",".join(DEFAULT_CASE_IDS),
        help="comma-separated subset; useful for parallel ECS shards")
    args = parser.parse_args()
    result = asyncio.run(run_ab(
        live=args.live, eager_ids=args.eager_ids, output=args.output,
        case_ids=tuple(
            item.strip() for item in args.case_ids.split(",")
            if item.strip())))
    print(json.dumps(result["summary"], ensure_ascii=False, indent=2))
