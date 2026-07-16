"""Small-scale LangGraph workflow pressure test with strict trace gate."""
from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def http_json(url: str, method: str = "GET", payload: dict | None = None, timeout: int = 120) -> dict:
    data = None
    headers = {"Content-Type": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def wait_task(base: str, trace_id: str, timeout_sec: int = 600) -> tuple[str, int, dict]:
    start = time.time()
    detail: dict = {}
    while time.time() - start < timeout_sec:
        detail = http_json(f"{base}/api/tasks/{trace_id}")
        status = detail.get("status", "RUNNING")
        if status in ("SUCCESS", "FAILED"):
            duration = int((time.time() - start) * 1000)
            return status, duration, detail
        time.sleep(6)
    return "TIMEOUT", int((time.time() - start) * 1000), detail


def run_one(base: str, idx: int) -> dict:
    resume_text = (
        f"候选人{idx}，{5 + idx % 4}年Java后端，Spring Boot、Redis、Kafka。"
        "参与电商订单系统重构，GitHub: https://github.com/example-dev"
    )
    task = http_json(
        f"{base}/api/tasks",
        "POST",
        {
            "fileName": f"pressure-{idx}.txt",
            "jobCategory": "TECH",
            "executionMode": "AUTO",
            "resumeText": resume_text,
        },
    )
    trace_id = task.get("traceId", "")
    status, duration_ms, detail = wait_task(base, trace_id)
    failed_node = detail.get("failedNode") or ""
    error = detail.get("errorMessage") or detail.get("summary") or ""
    return {
        "traceId": trace_id,
        "status": status,
        "durationMs": duration_ms,
        "failedNode": failed_node,
        "error": error[:200],
    }


def query_prometheus(base: str) -> dict:
    prom_base = base.replace(":80", "").rstrip("/")
    if ":8080" not in prom_base and ":80" not in prom_base:
        prom_base = prom_base.rstrip("/")
    queries = {
        "workflow_tool_count": 'sum(increase(resumai_workflow_tool_count_total[15m]))',
        "workflow_node_count": 'sum(increase(resumai_workflow_node_count_total[15m]))',
        "funnel_completed": 'sum(increase(resumai_funnel_evaluation_completed_total[15m]))',
    }
    snapshot: dict = {}
    for name, query in queries.items():
        try:
            url = f"http://127.0.0.1:9090/api/v1/query?query={urllib.request.quote(query)}"
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                snapshot[name] = data.get("data", {}).get("result", [])
        except Exception as exc:
            snapshot[name] = {"error": str(exc)}
    return snapshot


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("base", nargs="?", default="http://127.0.0.1")
    parser.add_argument("--count", type=int, default=30)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--strict-trace", action="store_true")
    args = parser.parse_args()

    if args.strict_trace:
        print("[pressure] running strict gate first...")
        cmd = [sys.executable, "scripts/verify_langgraph_workflow.py", args.base, "--strict-trace"]
        rc = subprocess.call(cmd)
        if rc != 0:
            raise SystemExit(f"strict gate failed with code {rc}")

    results: list[dict] = []
    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futures = [pool.submit(run_one, args.base, i + 1) for i in range(args.count)]
        for fut in as_completed(futures):
            results.append(fut.result())
            done = len(results)
            ok = sum(1 for r in results if r["status"] == "SUCCESS")
            print(f"[pressure] progress {done}/{args.count} success={ok}")

    durations = [r["durationMs"] for r in results if r["status"] == "SUCCESS"]
    success = sum(1 for r in results if r["status"] == "SUCCESS")
    failed = [r for r in results if r["status"] != "SUCCESS"]
    failure_by_node = Counter(r.get("failedNode") or "unknown" for r in failed)

    summary = {
        "count": args.count,
        "concurrency": args.concurrency,
        "successRate": round(success / args.count, 4),
        "avgMs": int(statistics.mean(durations)) if durations else 0,
        "p95Ms": int(statistics.quantiles(durations, n=20)[-1]) if len(durations) >= 2 else (durations[0] if durations else 0),
        "failureByNode": dict(failure_by_node),
        "traceIds": [r["traceId"] for r in results if r["status"] == "SUCCESS"][:5],
        "failedSamples": failed[:5],
    }
    print("\n[pressure] summary:")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print("\n[pressure] prometheus snapshot (local):")
    print(json.dumps(query_prometheus(args.base), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        main()
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise SystemExit(f"HTTP {exc.code}: {body}") from exc
