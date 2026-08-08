#!/usr/bin/env python3
"""Controlled TTL experiment for business-facing resume memories.

The production database does not yet contain a meaningful temporal cohort: its
rows were produced by load tests in a narrow time window.  This experiment
therefore replays the repository's 100-resume stress corpus as job-scoped
arrival streams.  It varies only the inter-arrival time and evaluates two
storage layers:

* RECENT_CASE: de-identified, evidence-oriented cases for the same job cohort.
* JOB_PROFILE: the consolidated profile for that job cohort.

No model output or production row is fabricated.  Resume cohort, skills,
length, and public-evidence flags come from testdata/stress_resumes/manifest.json.
The simulated dimension is deliberately limited to traffic cadence, with a
fixed seed and reported service-level gates.
"""
from __future__ import annotations

import argparse
import json
import math
import random
import re
import statistics
import time
from collections import defaultdict
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = ROOT / "testdata" / "stress_resumes" / "manifest.json"
DEFAULT_OUT = ROOT / "reports" / "experiments" / "business_memory_ttl_controlled.json"

RECENT_CANDIDATES = (7, 14, 30, 45, 60, 90)
PROFILE_CANDIDATES = (30, 60, 90, 180, 365)

# Per-job arrival rates.  The sparse cohort is intentionally harsh: one
# candidate every four weeks.  Normal is one/week; busy is one/workday.
CADENCES_PER_WEEK = {
    "sparse": 0.25,
    "normal": 1.0,
    "busy": 5.0,
}

SEEDS = tuple(range(40))
SIMULATION_DAYS = 365.0
RECENT_TOP_K = 2
PROFILE_MIN_CASES = 3


def cohort_key(case_id: str) -> str:
    return re.sub(r"_\d+$", "", case_id.strip().lower())


def length_bucket(chars: int) -> str:
    if chars < 1200:
        return "short"
    if chars < 2200:
        return "medium"
    return "long"


def case_features(row: dict[str, Any]) -> frozenset[str]:
    features = {
        f"skill:{str(skill).strip().lower()}"
        for skill in row.get("expectedSkills") or []
        if str(skill).strip()
    }
    features.add(f"length:{length_bucket(int(row.get('textLength') or 0))}")
    features.add(f"public:{bool(row.get('hasGithub'))}")
    return frozenset(features)


def jaccard(left: frozenset[str], right: frozenset[str]) -> float:
    union = left | right
    return len(left & right) / len(union) if union else 1.0


def load_manifest(path: Path) -> dict[str, list[dict[str, Any]]]:
    rows = json.loads(path.read_text(encoding="utf-8"))
    cohorts: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for raw in rows:
        case_id = str(raw.get("id") or "").strip()
        if not case_id:
            continue
        cohorts[cohort_key(case_id)].append({
            "id": case_id,
            "role": str(raw.get("role") or ""),
            "features": case_features(raw),
        })
    if len(rows) < 50 or len(cohorts) < 5:
        raise ValueError(
            f"TTL experiment requires a diverse corpus; rows={len(rows)} cohorts={len(cohorts)}")
    return dict(cohorts)


def generate_events(
    cases: list[dict[str, Any]], rate_per_week: float, seed: int,
) -> list[tuple[float, dict[str, Any]]]:
    rng = random.Random(seed)
    rate_per_day = rate_per_week / 7.0
    now = 0.0
    events: list[tuple[float, dict[str, Any]]] = []
    index = rng.randrange(len(cases))
    while True:
        # Exponential gaps model independent arrivals and expose TTL boundary
        # failures without pretending that load-test timestamps are history.
        gap = -math.log(max(1e-12, 1.0 - rng.random())) / rate_per_day
        now += gap
        if now > SIMULATION_DAYS:
            break
        events.append((now, cases[index % len(cases)]))
        index += 1
    return events


def evaluate_recent(
    events: list[tuple[float, dict[str, Any]]], ttl_days: int,
) -> dict[str, float]:
    history: list[tuple[float, dict[str, Any]]] = []
    eligible = 0
    covered_one = 0
    covered_two = 0
    similarities: list[float] = []
    attached_counts: list[int] = []
    for at, current in events:
        if history:
            eligible += 1
        available = [
            (seen_at, case) for seen_at, case in history
            if at - seen_at <= ttl_days
        ]
        available.sort(
            key=lambda item: (
                jaccard(current["features"], item[1]["features"]), item[0]),
            reverse=True,
        )
        selected = available[:RECENT_TOP_K]
        if selected:
            covered_one += 1
            similarities.extend(
                jaccard(current["features"], case["features"])
                for _, case in selected
            )
        if len(selected) >= RECENT_TOP_K:
            covered_two += 1
        attached_counts.append(len(selected))
        history.append((at, current))
    denominator = max(1, eligible)
    return {
        "eligibleQueries": float(eligible),
        "coverageAtLeastOne": covered_one / denominator,
        "coverageTop2": covered_two / denominator,
        "meanSelectedSimilarity": statistics.fmean(similarities) if similarities else 0.0,
        "meanAttachedCases": statistics.fmean(attached_counts) if attached_counts else 0.0,
    }


def evaluate_profile(
    events: list[tuple[float, dict[str, Any]]], ttl_days: int,
) -> dict[str, float]:
    prior_count = 0
    last_update: float | None = None
    eligible = 0
    available = 0
    for at, _current in events:
        if prior_count >= PROFILE_MIN_CASES and last_update is not None:
            eligible += 1
            if at - last_update <= ttl_days:
                available += 1
        prior_count += 1
        last_update = at
    return {
        "eligibleQueries": float(eligible),
        "availability": available / max(1, eligible),
    }


def aggregate(rows: list[dict[str, float]]) -> dict[str, float]:
    keys = sorted({key for row in rows for key in row})
    return {
        key: round(statistics.fmean(row[key] for row in rows), 4)
        for key in keys
    }


def run_experiment(cohorts: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    recent: dict[str, list[dict[str, Any]]] = {}
    profiles: dict[str, list[dict[str, Any]]] = {}
    for cadence, rate in CADENCES_PER_WEEK.items():
        recent_rows: dict[int, list[dict[str, float]]] = defaultdict(list)
        profile_rows: dict[int, list[dict[str, float]]] = defaultdict(list)
        for cohort_index, cases in enumerate(cohorts.values()):
            for seed in SEEDS:
                events = generate_events(
                    cases, rate, seed=seed * 10_007 + cohort_index * 97)
                for ttl in RECENT_CANDIDATES:
                    recent_rows[ttl].append(evaluate_recent(events, ttl))
                for ttl in PROFILE_CANDIDATES:
                    profile_rows[ttl].append(evaluate_profile(events, ttl))
        recent[cadence] = [
            {"ttlDays": ttl, **aggregate(recent_rows[ttl])}
            for ttl in RECENT_CANDIDATES
        ]
        profiles[cadence] = [
            {"ttlDays": ttl, **aggregate(profile_rows[ttl])}
            for ttl in PROFILE_CANDIDATES
        ]

    def recent_metric(cadence: str, ttl: int, key: str) -> float:
        row = next(item for item in recent[cadence] if item["ttlDays"] == ttl)
        return float(row[key])

    def profile_metric(cadence: str, ttl: int) -> float:
        row = next(item for item in profiles[cadence] if item["ttlDays"] == ttl)
        return float(row["availability"])

    recent_selected = next((
        ttl for ttl in RECENT_CANDIDATES
        if recent_metric("normal", ttl, "coverageTop2") >= 0.90
        and recent_metric("busy", ttl, "coverageTop2") >= 0.95
        and recent_metric("normal", ttl, "meanSelectedSimilarity") >= 0.90
    ), None)
    profile_selected = next((
        ttl for ttl in PROFILE_CANDIDATES
        if min(profile_metric(cadence, ttl) for cadence in CADENCES_PER_WEEK) >= 0.99
    ), None)

    return {
        "experiment": "business_memory_ttl_controlled_replay",
        "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source": str(DEFAULT_MANIFEST.relative_to(ROOT)),
        "corpus": {
            "cases": sum(len(rows) for rows in cohorts.values()),
            "jobCohorts": len(cohorts),
            "cohortSizes": {key: len(value) for key, value in sorted(cohorts.items())},
        },
        "method": {
            "simulationDays": SIMULATION_DAYS,
            "seeds": len(SEEDS),
            "cadencesPerJobPerWeek": CADENCES_PER_WEEK,
            "recentTopK": RECENT_TOP_K,
            "jobProfileMinCases": PROFILE_MIN_CASES,
            "identityDependency": "NONE; memories are job-scoped and de-identified",
            "productionMutation": "NONE",
        },
        "selectionGates": {
            "RECENT_CASE": {
                "normalCoverageTop2": 0.90,
                "busyCoverageTop2": 0.95,
                "normalMeanSimilarity": 0.90,
            },
            "JOB_PROFILE": {
                "minimumAvailabilityAcrossCadences": 0.99,
            },
        },
        "results": {
            "RECENT_CASE": recent,
            "JOB_PROFILE": profiles,
        },
        "decision": {
            "RECENT_CASE": recent_selected,
            "JOB_PROFILE": profile_selected,
        },
    }


def markdown(report: dict[str, Any]) -> str:
    lines = [
        "# 业务 Memory TTL 受控回放实验",
        "",
        f"- 语料：{report['corpus']['cases']} 份简历，"
        f"{report['corpus']['jobCohorts']} 个岗位 cohort",
        "- 时间：365 天受控 Poisson 到达流，稀疏/正常/繁忙三档",
        "- 候选人识别：不需要；案例按岗位隔离且脱敏",
        "- 线上数据变更：无",
        "",
        "## 决策",
        "",
        f"- `RECENT_CASE`: **{report['decision']['RECENT_CASE']} 天**",
        f"- `JOB_PROFILE`: **{report['decision']['JOB_PROFILE']} 天**",
        "",
        "## RECENT_CASE",
        "",
        "| 流量 | TTL | Top-2覆盖 | 至少1条 | 平均相似度 |",
        "|---|---:|---:|---:|---:|",
    ]
    for cadence, rows in report["results"]["RECENT_CASE"].items():
        for row in rows:
            lines.append(
                f"| {cadence} | {row['ttlDays']}d | {row['coverageTop2']:.4f} | "
                f"{row['coverageAtLeastOne']:.4f} | {row['meanSelectedSimilarity']:.4f} |")
    lines.extend([
        "",
        "## JOB_PROFILE",
        "",
        "| 流量 | TTL | 建立后可用率 |",
        "|---|---:|---:|",
    ])
    for cadence, rows in report["results"]["JOB_PROFILE"].items():
        for row in rows:
            lines.append(
                f"| {cadence} | {row['ttlDays']}d | {row['availability']:.4f} |")
    lines.extend([
        "",
        "## 适用边界",
        "",
        "该实验选择的是当前候选网格和显式业务SLO下的最短TTL。"
        "JD内容变更必须通过JD版本/fingerprint使旧画像失效，不能只依赖TTL。",
        "",
    ])
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", default=str(DEFAULT_MANIFEST))
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    args = parser.parse_args()
    cohorts = load_manifest(Path(args.manifest))
    report = run_experiment(cohorts)
    if report["decision"]["RECENT_CASE"] is None:
        raise RuntimeError("no RECENT_CASE TTL candidate met the declared gates")
    if report["decision"]["JOB_PROFILE"] is None:
        raise RuntimeError("no JOB_PROFILE TTL candidate met the declared gates")
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    out.with_suffix(".md").write_text(markdown(report), encoding="utf-8")
    print(markdown(report))
    print(f"json -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
