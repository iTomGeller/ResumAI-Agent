#!/usr/bin/env python3
"""EXP-9: reward-weight sensitivity over REAL persisted policy_reward rows.

Rewards are stored with their per-component breakdown (RewardService), so we
can recompute every FEEDBACK total under perturbed weights (+/-10pp on each
component, renormalized) and check whether the policy ranking (by avg reward)
flips. No LLM calls, no synthetic data — pure offline re-weighting.

Usage (ECS):
  python3 harness/run_reward_sensitivity.py --out reports/experiments
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parents[1]

# Must mirror RewardService.recordFeedbackReward weights.
FEEDBACK_WEIGHTS = {
    "hrAcceptance": 0.22,
    "recommendationAgreement": 0.14,
    "scoreDelta": 0.10,
    "missedEvidence": 0.10,
    "unsupportedClaims": 0.12,
    "riskAccuracy": 0.08,
    "evidenceSupportRatio": 0.10,
    "jdCoverage": 0.06,
    "llmCost": 0.04,
    "latency": 0.04,
}


def load_rows_via_mysql() -> List[Dict[str, Any]]:
    """Read policy_reward through the mysql container (no local driver needed)."""
    env_file = Path("/opt/resumai-src/.env")
    env: Dict[str, str] = {}
    for line in env_file.read_text(encoding="utf-8").splitlines():
        if line.strip() and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            env[key.strip()] = value.strip()
    sql = ("SELECT policy_id, source, total_reward, components FROM policy_reward "
           "WHERE components IS NOT NULL")
    proc = subprocess.run(
        ["docker", "exec", "resumai-mysql", "mysql", "-N", "-B",
         "-uroot", "-p" + env["MYSQL_ROOT_PASSWORD"],
         env.get("MYSQL_DATABASE", "resumai"), "-e", sql],
        capture_output=True, text=True, timeout=60, check=True)
    rows = []
    for line in proc.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) < 4:
            continue
        try:
            components = json.loads(parts[3])
        except json.JSONDecodeError:
            continue
        rows.append({
            "policyId": parts[0],
            "source": parts[1],
            "totalReward": float(parts[2]),
            "components": components,
        })
    return rows


def recompute(components: Dict[str, float], weights: Dict[str, float]) -> float:
    total = sum(weights[k] * float(components.get(k, 0.0)) for k in weights)
    return max(0.0, min(1.0, total))


def ranking(rows: List[Dict[str, Any]], weights: Dict[str, float]) -> List[str]:
    sums: Dict[str, float] = defaultdict(float)
    counts: Dict[str, int] = defaultdict(int)
    for row in rows:
        score = recompute(row["components"], weights)
        sums[row["policyId"]] += score
        counts[row["policyId"]] += 1
    return sorted(sums, key=lambda p: sums[p] / counts[p], reverse=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=str(ROOT / "reports" / "experiments"))
    parser.add_argument("--perturbation", type=float, default=0.10)
    args = parser.parse_args()
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    rows = [r for r in load_rows_via_mysql() if r["source"] == "FEEDBACK"]
    if len(rows) < 4:
        print(f"only {len(rows)} FEEDBACK reward rows — not enough for sensitivity; "
              "report will mark insufficient_data")
    baseline_ranking = ranking(rows, FEEDBACK_WEIGHTS) if rows else []
    champion = baseline_ranking[0] if baseline_ranking else None

    flips = []
    trials = 0
    for key in FEEDBACK_WEIGHTS:
        for direction in (+1, -1):
            perturbed = dict(FEEDBACK_WEIGHTS)
            perturbed[key] = max(0.0, perturbed[key] + direction * args.perturbation)
            norm = sum(perturbed.values())
            perturbed = {k: v / norm for k, v in perturbed.items()}
            new_ranking = ranking(rows, perturbed) if rows else []
            trials += 1
            if new_ranking and new_ranking[0] != champion:
                flips.append({
                    "component": key,
                    "direction": "+10pp" if direction > 0 else "-10pp",
                    "newChampion": new_ranking[0],
                })

    report = {
        "experiment": "EXP-9 reward weight sensitivity",
        "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "feedbackRewardRows": len(rows),
        "policies": sorted({r["policyId"] for r in rows}),
        "baselineRanking": baseline_ranking,
        "champion": champion,
        "perturbation": args.perturbation,
        "trials": trials,
        "championFlips": flips,
        "robust": not flips and len(rows) >= 4,
        "status": "ok" if len(rows) >= 4 else "insufficient_data",
    }
    path = out_dir / "reward_sensitivity.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"report -> {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
