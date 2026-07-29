#!/usr/bin/env python3
"""EXP-13: replay real memory usage to evaluate TTL candidates safely.

This experiment never mutates memory or production policy. For every recorded
memory usage it asks whether the memory would still have existed under each TTL
candidate, then reports USED retention, score-weighted retention, and how many
IGNORED invocations would have been removed. A candidate is only proposed when
sample count, temporal coverage, and candidate differentiation gates all pass.

Runs on ECS: python3 harness/run_memory_ttl_replay.py --base http://127.0.0.1
"""
from __future__ import annotations

import argparse
import json
import math
import subprocess
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]

DEFAULT_TTL_DAYS = {
    "WORKING": 2,
    "SEMANTIC": 90,
    "EPISODIC": 90,
    "PROCEDURAL": 365,
}

CANDIDATE_TTL_DAYS = {
    "WORKING": [1, 2, 3, 7],
    "SEMANTIC": [30, 60, 90, 180],
    "EPISODIC": [30, 60, 90, 180],
    "PROCEDURAL": [90, 180, 365, 730],
}

AGE_BUCKETS_DAYS = [1, 7, 30, 90, 180, 365]

# Keep the replay taxonomy identical to MemoryService. Historical rows remain
# queryable under their storage names even though runtime reads canonicalize
# them before applying TTL policy.
LEGACY_TYPE_REMAP = {
    "CONVERSATION": "WORKING",
    "SHORT_TERM": "WORKING",
    "PREFERENCE": "SEMANTIC",
    "USER_PREFERENCE": "SEMANTIC",
    "HR_FEEDBACK": "SEMANTIC",
    "DOMAIN": "SEMANTIC",
    "FAILURE": "EPISODIC",
}


def fetch_mysql_payload(container: str) -> dict[str, Any]:
    """Read the complete ECS usage history without the Ops API page limit."""
    sql = """
SELECT JSON_OBJECT(
  'type', m.type,
  'decision', u.decision,
  'ageAtUseSeconds', TIMESTAMPDIFF(
      SECOND, m.create_time, DATE_ADD(u.create_time, INTERVAL 8 HOUR)),
  'finalScore', u.final_score,
  'memoryId', u.memory_id,
  'runId', u.run_id
)
FROM run_memory_usage u
JOIN memory_entry m ON m.memory_id = u.memory_id
ORDER BY u.id;
"""
    command = [
        "docker", "exec", "-i", container, "sh", "-lc",
        'mysql -N -B --raw -uroot -p"$MYSQL_ROOT_PASSWORD" "$MYSQL_DATABASE"',
    ]
    completed = subprocess.run(
        command, input=sql, text=True, capture_output=True, check=True)
    rows = [json.loads(line) for line in completed.stdout.splitlines() if line.strip()]
    return {
        "_experimentSource": f"mysql-container:{container}",
        "usage": rows,
        "defaults": {"ttl": {"typeDefaultDays": DEFAULT_TTL_DAYS}},
    }


def fetch_json(url: str, timeout: float = 30.0) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def finite_number(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def normalize_usage(rows: Iterable[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, int]]:
    samples: list[dict[str, Any]] = []
    diagnostics = {
        "inputRows": 0,
        "missingType": 0,
        "legacyTypeRemapped": 0,
        "legacyTypeCounts": {},
        "missingAge": 0,
        "negativeAge": 0,
        "unsupportedDecision": 0,
    }
    for row in rows:
        diagnostics["inputRows"] += 1
        raw_type = str(row.get("type") or "").strip().upper()
        memory_type = LEGACY_TYPE_REMAP.get(raw_type, raw_type)
        if memory_type != raw_type:
            diagnostics["legacyTypeRemapped"] += 1
            counts = diagnostics["legacyTypeCounts"]
            counts[raw_type] = counts.get(raw_type, 0) + 1
        if memory_type not in CANDIDATE_TTL_DAYS:
            diagnostics["missingType"] += 1
            continue
        age_seconds = finite_number(row.get("ageAtUseSeconds"))
        if age_seconds is None:
            diagnostics["missingAge"] += 1
            continue
        if age_seconds < 0:
            diagnostics["negativeAge"] += 1
            continue
        decision = str(row.get("decision") or "").strip().upper()
        if decision not in {"USED", "IGNORED"}:
            diagnostics["unsupportedDecision"] += 1
            continue
        score = finite_number(row.get("finalScore"))
        samples.append({
            "type": memory_type,
            "decision": decision,
            "ageDays": age_seconds / 86400.0,
            "weight": min(1.0, max(0.05, score if score is not None else 1.0)),
            "memoryId": row.get("memoryId"),
            "runId": row.get("runId"),
        })
    diagnostics["validRows"] = len(samples)
    return samples, diagnostics


def age_histogram(samples: list[dict[str, Any]]) -> dict[str, int]:
    histogram: dict[str, int] = {}
    lower = 0
    for upper in AGE_BUCKETS_DAYS:
        label = f"{lower}-{upper}d"
        histogram[label] = sum(lower <= row["ageDays"] < upper for row in samples)
        lower = upper
    histogram[f">={AGE_BUCKETS_DAYS[-1]}d"] = sum(
        row["ageDays"] >= AGE_BUCKETS_DAYS[-1] for row in samples)
    return histogram


def ratio(numerator: float, denominator: float) -> float | None:
    if denominator <= 0:
        return None
    return round(numerator / denominator, 4)


def evaluate_type(
    memory_type: str,
    samples: list[dict[str, Any]],
    default_days: int,
    candidates: list[int],
    min_used_samples: int,
    retention_floor: float,
    coverage_ratio: float,
) -> dict[str, Any]:
    used = [row for row in samples if row["decision"] == "USED"]
    ignored = [row for row in samples if row["decision"] == "IGNORED"]
    total_used_weight = sum(row["weight"] for row in used)
    max_age = max((row["ageDays"] for row in samples), default=0.0)
    observed_days = len({int(row["ageDays"]) for row in samples})
    required_coverage_days = round(default_days * coverage_ratio, 1)

    rows: list[dict[str, Any]] = []
    for ttl_days in sorted(set(candidates + [default_days])):
        kept_used = [row for row in used if row["ageDays"] <= ttl_days]
        kept_ignored = [row for row in ignored if row["ageDays"] <= ttl_days]
        kept_all = [row for row in samples if row["ageDays"] <= ttl_days]
        used_retention = ratio(len(kept_used), len(used))
        weighted_retention = ratio(
            sum(row["weight"] for row in kept_used), total_used_weight)
        ignored_expired = ratio(len(ignored) - len(kept_ignored), len(ignored))
        all_retention = ratio(len(kept_all), len(samples))
        passes_quality = (
            used_retention is not None
            and weighted_retention is not None
            and used_retention >= retention_floor
            and weighted_retention >= retention_floor
        )
        rows.append({
            "ttlDays": ttl_days,
            "isCurrentDefault": ttl_days == default_days,
            "usedRetention": used_retention,
            "weightedUsedRetention": weighted_retention,
            "ignoredExpiredFraction": ignored_expired,
            "allInvocationRetention": all_retention,
            "passesQualityGate": passes_quality,
        })

    signatures = {
        (row["usedRetention"], row["weightedUsedRetention"],
         row["ignoredExpiredFraction"], row["allInvocationRetention"])
        for row in rows
    }
    reasons: list[str] = []
    if len(used) < min_used_samples:
        reasons.append(f"USED 样本 {len(used)} < {min_used_samples}")
    if max_age < required_coverage_days:
        reasons.append(
            f"最大观测年龄 {max_age:.1f}d < 当前 TTL 的 {coverage_ratio:.0%} "
            f"覆盖门槛 {required_coverage_days:.1f}d")
    if observed_days < 3:
        reasons.append(f"仅覆盖 {observed_days} 个不同记忆年龄日")
    if len(signatures) < 2:
        reasons.append("候选 TTL 在当前样本上没有产生可区分结果")

    conclusive = not reasons
    passing = [row for row in rows if row["passesQualityGate"]]
    proposed = min((row["ttlDays"] for row in passing), default=None) if conclusive else None
    decision = "PROPOSE_CANDIDATE" if proposed is not None else "KEEP_DEFAULT_INSUFFICIENT_DATA"
    if conclusive and proposed is None:
        decision = "KEEP_DEFAULT_QUALITY_GATE_FAILED"

    return {
        "type": memory_type,
        "currentDefaultDays": default_days,
        "sampleCount": len(samples),
        "usedSamples": len(used),
        "ignoredSamples": len(ignored),
        "maxObservedAgeDays": round(max_age, 3),
        "distinctObservedAgeDays": observed_days,
        "requiredCoverageDays": required_coverage_days,
        "ageHistogram": age_histogram(samples),
        "conclusive": conclusive,
        "insufficientReasons": reasons,
        "decision": decision,
        "proposedTtlDays": proposed,
        "candidates": rows,
    }


def build_report(
    payload: dict[str, Any],
    min_used_samples: int = 30,
    retention_floor: float = 0.99,
    coverage_ratio: float = 0.8,
) -> dict[str, Any]:
    samples, diagnostics = normalize_usage(payload.get("usage") or [])
    defaults = (
        payload.get("defaults", {}).get("ttl", {}).get("typeDefaultDays")
        or DEFAULT_TTL_DAYS
    )
    results = []
    for memory_type, candidates in CANDIDATE_TTL_DAYS.items():
        type_samples = [row for row in samples if row["type"] == memory_type]
        default_days = int(defaults.get(memory_type, DEFAULT_TTL_DAYS[memory_type]))
        results.append(evaluate_type(
            memory_type, type_samples, default_days, candidates,
            min_used_samples, retention_floor, coverage_ratio))

    proposals = [
        row for row in results
        if row["conclusive"] and row["proposedTtlDays"] is not None
    ]
    return {
        "experiment": "memory_ttl_temporal_replay",
        "version": 2,
        "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source": payload.get("_experimentSource", "ops-memory-api"),
        "mutation": "NONE",
        "guardrail": "Never change production TTL from an inconclusive replay",
        "thresholds": {
            "minUsedSamplesPerType": min_used_samples,
            "usedRetentionFloor": retention_floor,
            "weightedUsedRetentionFloor": retention_floor,
            "minimumHistoryCoverageRatio": coverage_ratio,
        },
        "diagnostics": diagnostics,
        "overallDecision": (
            "REVIEW_PROPOSALS" if proposals
            else "KEEP_CURRENT_DEFAULTS_INSUFFICIENT_DATA"
        ),
        "typeResults": results,
    }


def markdown_summary(report: dict[str, Any]) -> str:
    lines = [
        "# EXP-13 Memory TTL 时间回放",
        "",
        f"- 生成时间：{report['generatedAt']}",
        f"- 总体决策：**{report['overallDecision']}**",
        f"- 有效 usage：{report['diagnostics']['validRows']} / "
        f"{report['diagnostics']['inputRows']}",
        f"- 数据源：{report['source']}",
        f"- 历史类型归一：{report['diagnostics']['legacyTypeRemapped']} 条",
        "- 安全边界：样本不足时只保留默认值，不自动修改生产 TTL。",
        "",
        "| 类型 | 当前 TTL | USED 样本 | 最大年龄 | 是否可定论 | 建议 |",
        "|---|---:|---:|---:|---|---|",
    ]
    for row in report["typeResults"]:
        proposal = (f"{row['proposedTtlDays']}d" if row["proposedTtlDays"] is not None
                    else "保持默认")
        lines.append(
            f"| {row['type']} | {row['currentDefaultDays']}d | {row['usedSamples']} | "
            f"{row['maxObservedAgeDays']}d | {'是' if row['conclusive'] else '否'} | {proposal} |")
        if row["insufficientReasons"]:
            lines.append(f"|  |  |  |  | 原因 | {'；'.join(row['insufficientReasons'])} |")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1")
    parser.add_argument("--input", help="Offline Ops memory JSON fixture")
    parser.add_argument(
        "--mysql-container",
        help="Read complete history from a local ECS MySQL container (for example resumai-mysql)")
    parser.add_argument("--limit", type=int, default=200)
    parser.add_argument("--min-used", type=int, default=30)
    parser.add_argument("--retention-floor", type=float, default=0.99)
    parser.add_argument("--coverage-ratio", type=float, default=0.8)
    parser.add_argument("--out", default=str(ROOT / "reports" / "experiments"))
    parser.add_argument("--require-conclusive", action="store_true")
    args = parser.parse_args()

    if args.mysql_container:
        payload = fetch_mysql_payload(args.mysql_container)
    elif args.input:
        payload = json.loads(Path(args.input).read_text(encoding="utf-8"))
    else:
        query = urllib.parse.urlencode({"limit": min(max(1, args.limit), 200)})
        payload = fetch_json(f"{args.base.rstrip('/')}/api/ops/memory?{query}")

    report = build_report(
        payload, min_used_samples=max(1, args.min_used),
        retention_floor=args.retention_floor,
        coverage_ratio=args.coverage_ratio)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "memory_ttl_replay.json"
    md_path = out_dir / "memory_ttl_replay.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(markdown_summary(report), encoding="utf-8")
    print(markdown_summary(report))
    print(f"json -> {json_path}")
    print(f"markdown -> {md_path}")
    if args.require_conclusive and not any(row["conclusive"] for row in report["typeResults"]):
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
