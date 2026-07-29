#!/usr/bin/env python3
"""EXP-13 current-workflow shadow A/B for Episodic TTL.

This is deliberately narrower than the historical replay: it runs the real
workflow twice for each case, with the latest ACTIVE current-version
cross-candidate anchor retained vs temporarily expired.  It never changes the
production TTL policy and restores both expires_at and update_time in a
finally block.

Run on ECS after the current workflow has produced at least one ACTIVE anchor:
  python3 harness/run_memory_ttl_shadow_ab.py \
    --base http://127.0.0.1 --cutover-local '2026-07-29 16:03:16'
"""
from __future__ import annotations

import argparse
import json
import subprocess
import time
from pathlib import Path
from typing import Any

from run_agent_e2e_benchmark import run_one

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CASES = ROOT / "testdata" / "benchmark" / "gold_cases.json"


def mysql(container: str, sql: str) -> str:
    command = [
        "docker", "exec", "-i", container, "sh", "-lc",
        'mysql -N -B -uroot -p"$MYSQL_ROOT_PASSWORD" "$MYSQL_DATABASE"',
    ]
    completed = subprocess.run(
        command, input=sql, text=True, capture_output=True, check=True)
    return completed.stdout


def active_snapshot(container: str, cutover_local: str) -> dict[str, tuple[str, str]]:
    sql = f"""
SELECT memory_id,
       DATE_FORMAT(expires_at, '%Y-%m-%d %H:%i:%s'),
       DATE_FORMAT(update_time, '%Y-%m-%d %H:%i:%s')
FROM memory_entry
WHERE type='EPISODIC' AND source='cross_candidate_anchor'
  AND status='ACTIVE' AND create_time >= '{cutover_local}'
ORDER BY create_time;
"""
    result: dict[str, tuple[str, str]] = {}
    for line in mysql(container, sql).splitlines():
        parts = line.split("\t")
        if len(parts) == 3:
            result[parts[0]] = (parts[1], parts[2])
    return result


def expire(container: str, snapshot: dict[str, tuple[str, str]]) -> None:
    for memory_id, (_, update_time) in snapshot.items():
        mysql(container, f"""
UPDATE memory_entry
SET expires_at=DATE_SUB(NOW(), INTERVAL 1 SECOND),
    update_time='{update_time}'
WHERE memory_id='{memory_id}';
""")


def restore(container: str, snapshot: dict[str, tuple[str, str]]) -> None:
    for memory_id, (expires_at, update_time) in snapshot.items():
        mysql(container, f"""
UPDATE memory_entry
SET expires_at='{expires_at}', update_time='{update_time}'
WHERE memory_id='{memory_id}';
""")


def current_hits(container: str, run_id: str, cutover_local: str) -> list[dict[str, Any]]:
    sql = f"""
SELECT m.type, COUNT(*), COUNT(DISTINCT u.memory_id),
       ROUND(AVG(u.final_score), 4)
FROM run_memory_usage u JOIN memory_entry m ON m.memory_id=u.memory_id
WHERE u.run_id='{run_id}' AND m.create_time >= '{cutover_local}'
GROUP BY m.type ORDER BY m.type;
"""
    rows = []
    for line in mysql(container, sql).splitlines():
        parts = line.split("\t")
        if len(parts) == 4:
            rows.append({
                "type": parts[0],
                "usageRows": int(parts[1]),
                "distinctMemories": int(parts[2]),
                "avgScore": float(parts[3]),
            })
    return rows


def load_case(path: Path, case_id: str) -> dict[str, Any]:
    cases = json.loads(path.read_text(encoding="utf-8"))
    for case in cases:
        if case.get("caseId") == case_id:
            return case
    raise ValueError(f"case not found: {case_id}")


def run_pair(base: str, container: str, cutover_local: str,
             case: dict[str, Any], repeat_offset: int) -> dict[str, Any]:
    before = active_snapshot(container, cutover_local)
    if not before:
        raise RuntimeError(
            "no ACTIVE current-version cross_candidate_anchor; run a clean cohort first")
    saved = dict(before)
    results: list[dict[str, Any]] = []
    try:
        retained = run_one(base, case, "balanced", repeat_offset, 300)
        retained_view = dict(retained.__dict__)
        retained_view["condition"] = "CURRENT_EPISODIC_RETAINED"
        retained_view["currentProducerMemoryHits"] = current_hits(
            container, retained.run_id, cutover_local)
        results.append(retained_view)

        # The successful retained run may supersede the old anchor. Expire all
        # currently ACTIVE anchors, including the newly produced one.
        after = active_snapshot(container, cutover_local)
        saved.update(after)
        expire(container, after)
        expired = run_one(base, case, "balanced", repeat_offset + 1, 300)
        expired_view = dict(expired.__dict__)
        expired_view["condition"] = "CURRENT_EPISODIC_EXPIRED"
        expired_view["currentProducerMemoryHits"] = current_hits(
            container, expired.run_id, cutover_local)
        results.append(expired_view)
    finally:
        restore(container, saved)

    retained = results[0]
    expired = results[1]
    return {
        "caseId": case["caseId"],
        "results": results,
        "comparison": {
            "rewardDeltaRetainedMinusExpired": round(
                retained["total_reward"] - expired["total_reward"], 4),
            "evidenceSupportDelta": round(
                (retained.get("evidence_support_ratio") or 0)
                - (expired.get("evidence_support_ratio") or 0), 4),
            "latencyDeltaSeconds": round(
                retained["latency_seconds"] - expired["latency_seconds"], 2),
            "mustFindDelta": round(
                retained["must_find_score"] - expired["must_find_score"], 4),
            "violationDelta": round(
                retained["violation_penalty"] - expired["violation_penalty"], 4),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1")
    parser.add_argument("--mysql-container", default="resumai-mysql")
    parser.add_argument("--cutover-local", required=True,
                        help="Asia/Shanghai local deployment cutover")
    parser.add_argument("--cases", default=str(DEFAULT_CASES))
    parser.add_argument("--java-case", default="gold-java-backend-normal")
    parser.add_argument("--agent-case", default="gold-ai-agent-resume")
    parser.add_argument("--out", default=str(
        ROOT / "reports" / "experiments" / "memory_ttl_shadow_ab.json"))
    args = parser.parse_args()

    pairs = []
    for offset, case_id in enumerate((args.java_case, args.agent_case), 1):
        case = load_case(Path(args.cases), case_id)
        pair = run_pair(args.base, args.mysql_container, args.cutover_local,
                        case, offset * 2 - 1)
        pairs.append(pair)
        print(json.dumps(pair["comparison"], ensure_ascii=False), flush=True)

    valid_results = [row for pair in pairs for row in pair["results"]
                     if row["status"] in {"SUCCEEDED", "PARTIAL_SUCCESS"}]
    comparisons = [pair["comparison"] for pair in pairs]
    report = {
        "experiment": "EXP-13-current-workflow-episodic-shadow-ab",
        "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "workflowCutoverLocal": args.cutover_local,
        "cohort": "CURRENT_VERSION_CONSUMER_AND_PRODUCER",
        "pairs": pairs,
        "aggregate": {
            "successfulOrPartialRuns": len(valid_results),
            "meanRewardDelta": round(sum(x["rewardDeltaRetainedMinusExpired"]
                                         for x in comparisons) / len(comparisons), 4),
            "meanEvidenceSupportDelta": round(sum(x["evidenceSupportDelta"]
                                                   for x in comparisons) / len(comparisons), 4),
            "meanLatencyDeltaSeconds": round(sum(x["latencyDeltaSeconds"]
                                                  for x in comparisons) / len(comparisons), 2),
        },
        "mutationSafety": "expires_at and update_time restored in finally",
        "decision": "KEEP_EPISODIC_INCUMBENT_NOT_HORIZON_OPTIMAL",
    }
    output = Path(args.out)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                      encoding="utf-8")
    print(f"report -> {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
