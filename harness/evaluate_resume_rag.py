#!/usr/bin/env python3
"""Label-based A/B for the current-resume retrieval endpoint.

The generated stress resumes provide deterministic section/skill labels.  The
benchmark reports source isolation plus Precision/Recall/MRR/nDCG instead of
claiming that an uncalibrated online score proves relevance.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import statistics
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "testdata" / "stress_resumes" / "manifest.json"
DEFAULT_RAW_RESULTS = (
    ROOT / "reports" / "load_100_ingress1qps_8bca961_20260731"
    / "raw_results.json")


def load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    path = ROOT / ".deploy.local.env"
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip() and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                env[key.strip()] = value.strip()
    return env


def remote_token(env: dict[str, str]) -> str:
    if env.get("WORKFLOW_INTERNAL_TOKEN"):
        return env["WORKFLOW_INTERNAL_TOKEN"]
    import paramiko
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(
        env["ALIYUN_HOST"], username=env.get("ALIYUN_USER", "root"),
        password=env["ALIYUN_PASSWORD"], look_for_keys=False,
        allow_agent=False, timeout=30)
    try:
        _, stdout, _ = ssh.exec_command(
            "grep -E '^WORKFLOW_INTERNAL_TOKEN=' /opt/resumai-src/.env | tail -1",
            timeout=20)
        line = stdout.read().decode("utf-8", errors="replace").strip()
        return line.split("=", 1)[1].strip() if "=" in line else ""
    finally:
        ssh.close()


def norm(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip().lower()


def blocks(text: str) -> list[str]:
    rows = [row.strip() for row in re.split(r"\n\s*\n", text) if row.strip()]
    if len(rows) <= 1:
        rows = [row.strip() for row in text.splitlines() if row.strip()]
    return rows or [text.strip()]


def relevant(block: str, case: dict[str, Any]) -> bool:
    value = norm(block)
    if case["kind"] == "project":
        return any(marker in value for marker in (
            "项目经历", "项目经验", "project experience"))
    return any(norm(term) in value for term in case["terms"] if norm(term))


def post(url: str, token: str, payload: dict[str, Any]) -> tuple[dict, float]:
    last_error: Optional[Exception] = None
    for attempt in range(3):
        request = urllib.request.Request(
            url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "X-Internal-Token": token}, method="POST")
        started = time.perf_counter()
        try:
            with urllib.request.urlopen(request, timeout=90) as response:
                body = json.loads(response.read().decode("utf-8"))
            return body, (time.perf_counter() - started) * 1000
        except (urllib.error.URLError, TimeoutError) as exc:
            last_error = exc
            if attempt < 2:
                time.sleep(1.0 * (attempt + 1))
    raise RuntimeError(f"RAG endpoint failed after retries: {last_error}")


def evaluate(resume: str, returned: list[str], case: dict[str, Any]) -> dict[str, Any]:
    source = norm(resume)
    truth = blocks(resume)
    truth_ids = {i for i, block in enumerate(truth) if relevant(block, case)}
    gains: list[int] = []
    matched_truth: set[int] = set()
    source_hits = 0
    for chunk in returned:
        chunk_norm = norm(chunk)
        if chunk_norm and chunk_norm in source:
            source_hits += 1
        matches = {
            i for i, block in enumerate(truth)
            if chunk_norm and (chunk_norm in norm(block) or norm(block) in chunk_norm)
        }
        rel_matches = matches & truth_ids
        if not rel_matches and relevant(chunk, case):
            rel_matches = {-1}
        gains.append(1 if rel_matches else 0)
        matched_truth.update(i for i in rel_matches if i >= 0)
    precision = sum(gains) / len(gains) if gains else 0.0
    recall = len(matched_truth) / len(truth_ids) if truth_ids else 1.0
    first = next((i for i, gain in enumerate(gains, 1) if gain), None)
    mrr = 1.0 / first if first else 0.0
    dcg = sum(gain / math.log2(rank + 1) for rank, gain in enumerate(gains, 1))
    ideal = sum(1.0 / math.log2(rank + 1)
                for rank in range(1, min(len(truth_ids), len(gains)) + 1))
    return {
        "precisionAtK": precision,
        "recallAtK": recall,
        "mrr": mrr,
        "ndcgAtK": dcg / ideal if ideal else 1.0,
        "sourcePrecision": source_hits / len(returned) if returned else 0.0,
        "returnedK": len(returned),
        "truthCount": len(truth_ids),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://8.138.10.189")
    parser.add_argument("--count", type=int, default=30)
    parser.add_argument("--strategies", nargs="+",
                        default=["lexical", "embedding", "hybrid"])
    parser.add_argument("--raw-results", type=Path,
                        default=DEFAULT_RAW_RESULTS,
                        help="completed load results containing parsed resumeText")
    parser.add_argument("--out", type=Path,
                        default=ROOT / "reports" / "resume_rag_ab.json")
    args = parser.parse_args()
    token = remote_token(load_env())
    if not token:
        raise SystemExit("WORKFLOW_INTERNAL_TOKEN unavailable")
    all_rows = json.loads(MANIFEST.read_text(encoding="utf-8"))
    if args.count >= len(all_rows):
        rows = all_rows
    elif args.count <= 1:
        rows = all_rows[:1]
    else:
        rows = [all_rows[round(i * (len(all_rows) - 1) / (args.count - 1))]
                for i in range(args.count)]
    parsed_text: dict[str, str] = {}
    if args.raw_results.exists():
        for row in json.loads(args.raw_results.read_text(encoding="utf-8")):
            task = row.get("rawTask") if isinstance(row, dict) else None
            text = task.get("resumeText") if isinstance(task, dict) else None
            if row.get("id") and text:
                parsed_text[str(row["id"])] = str(text)
    results: list[dict[str, Any]] = []
    endpoint = args.base_url.rstrip("/") + "/api/internal/tools/resume-search"
    for record in rows:
        resume = parsed_text.get(str(record["id"]), "")
        if not resume:
            path = ROOT / record["path"]
            if str(record.get("fileType") or "").lower() == "pdf":
                print(f"skip {record['id']}: no parsed PDF text", flush=True)
                continue
            resume = path.read_text(encoding="utf-8")
        terms = list(record.get("expectedSkills") or [])[:6]
        cases = [
            {"kind": "project", "query": "项目经历 项目复杂度 技术方案 个人贡献"},
            {"kind": "tech", "query": " ".join(terms) + " 项目实践 量化成果",
             "terms": terms},
        ]
        for strategy in args.strategies:
            for case in cases:
                body, latency = post(endpoint, token, {
                    "query": case["query"], "topK": 5,
                    "resumeText": resume, "strategy": strategy})
                raw_returned = body.get("selectedChunks") or body.get("chunks") or []
                returned = [str(item) for item in raw_returned]
                results.append({
                    "id": record["id"], "strategy": strategy,
                    "kind": case["kind"], "latencyMs": round(latency, 1),
                    "onlineTopScore": body.get("topScore"),
                    "fallbackUsed": body.get("fallbackUsed"),
                    **evaluate(resume, returned, case),
                })
                print(f"{record['id']} {strategy} {case['kind']} "
                      f"P={results[-1]['precisionAtK']:.2f} "
                      f"R={results[-1]['recallAtK']:.2f} "
                      f"source={results[-1]['sourcePrecision']:.2f}", flush=True)
                args.out.parent.mkdir(parents=True, exist_ok=True)
                args.out.with_suffix(".checkpoint.json").write_text(
                    json.dumps(results, ensure_ascii=False, indent=2),
                    encoding="utf-8")
    summary: dict[str, Any] = {}
    for strategy in args.strategies:
        subset = [row for row in results if row["strategy"] == strategy]
        summary[strategy] = {
            key: round(statistics.mean(float(row[key]) for row in subset), 4)
            for key in ("precisionAtK", "recallAtK", "mrr", "ndcgAtK",
                        "sourcePrecision", "returnedK", "latencyMs")
        }
        summary[strategy]["fallbackRate"] = round(
            sum(bool(row["fallbackUsed"]) for row in subset) / len(subset), 4)
    output = {"samples": len(rows), "queries": len(results),
              "summary": summary, "rows": results}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(output, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
