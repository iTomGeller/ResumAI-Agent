#!/usr/bin/env python3
"""EXP-10: parallel vs serial specialist groups on the REAL e2e path.

Creates a CANDIDATE policy identical to `balanced` except
parallelSpecialists=false, then runs the real e2e benchmark on the same
cases with both policies and compares latency / reward / tokens.

Usage (ECS):
  python3 harness/run_parallel_ab.py \
      --case-ids gold-java-backend-normal,gold-ai-agent-resume \
      --out reports/experiments
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "harness"))
sys.path.insert(0, str(ROOT / "workflow"))

from run_agent_e2e_benchmark import aggregate, load_cases, run_one  # noqa: E402

SERIAL_POLICY_ID = "exp10-serial-balanced"


def load_env() -> None:
    for candidate in ("/opt/resumai-src/.env",):
        path = Path(candidate)
        if path.is_file():
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip() and not line.startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    os.environ.setdefault(key.strip(), value.strip())


def http(method: str, url: str, body: Optional[dict] = None,
         headers: Optional[dict] = None, timeout: float = 30.0) -> Any:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    all_headers = {"Content-Type": "application/json"}
    all_headers.update(headers or {})
    request = urllib.request.Request(url, data=data, method=method, headers=all_headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def ensure_serial_policy(base: str, token: str) -> None:
    bundles = http("GET", f"{base}/api/internal/policies",
                   headers={"X-Internal-Token": token})
    if any(b.get("policyId") == SERIAL_POLICY_ID for b in bundles):
        print(f"policy {SERIAL_POLICY_ID} already exists")
        return
    balanced = next(b for b in bundles if b.get("policyId") == "balanced")
    config = dict(balanced.get("config") or {})
    config["parallelSpecialists"] = False
    http("POST", f"{base}/api/internal/policies/candidates", {
        "policyId": SERIAL_POLICY_ID,
        "name": "EXP-10 串行对照（balanced 派生）",
        "description": "与 balanced 完全一致，仅 parallelSpecialists=false",
        "config": config,
        "parentPolicyId": "balanced",
        "generation": 0,
        "mutationReason": "EXP-10 parallel-vs-serial ablation control",
    }, headers={"X-Internal-Token": token})
    print(f"created candidate policy {SERIAL_POLICY_ID}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1")
    parser.add_argument("--cases", default=str(ROOT / "testdata" / "benchmark"))
    parser.add_argument("--case-ids",
                        default="gold-java-backend-normal,gold-ai-agent-resume")
    parser.add_argument("--run-timeout", type=int, default=420)
    parser.add_argument("--out", default=str(ROOT / "reports" / "experiments"))
    args = parser.parse_args()
    load_env()
    token = os.environ.get("WORKFLOW_INTERNAL_TOKEN", "")
    if not token:
        print("WORKFLOW_INTERNAL_TOKEN required")
        return 2
    base = args.base.rstrip("/")
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    ensure_serial_policy(base, token)

    wanted = {c.strip() for c in args.case_ids.split(",") if c.strip()}
    cases = [c for c in load_cases(Path(args.cases), "gold") if c["caseId"] in wanted]
    print(f"cases: {[c['caseId'] for c in cases]}")

    results = []
    for policy_id in ("balanced", SERIAL_POLICY_ID):
        for case in cases:
            print(f"[run] {case['caseId']} × {policy_id} ...")
            row = run_one(base, case, policy_id, repeat=1, timeout_s=args.run_timeout)
            print(f"  -> status={row.status} reward={row.total_reward} "
                  f"latency={row.latency_seconds:.1f}s llm={row.llm_calls} "
                  f"err={row.error}")
            results.append(row)

    summary = aggregate(results)
    report = {
        "experiment": "EXP-10 parallel vs serial specialists",
        "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "cases": sorted(wanted),
        "policies": {"parallel": "balanced", "serial": SERIAL_POLICY_ID},
        "results": summary,
    }
    path = out_dir / "parallel_ab.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"report -> {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
