#!/usr/bin/env python3
"""EXP-7: one labeled e2e batch under the currently deployed replan threshold.

Submits N full-evaluation conversations, waits for terminal states, then joins
run metrics with replan events from run_event (via docker exec mysql on the
host). The sweep driver (scripts/_exp7_replan_sweep.sh) redeploys the workflow
container per threshold and calls this once per label.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

CASES = [
    {
        "title": "exp7-strong",
        "resume": "8年Java后端，主导过日活千万的订单系统架构，Spring Cloud 微服务全家桶，"
                  "MySQL 分库分表与 Redis 多级缓存实战，带 6 人团队，有完整线上故障复盘经验。"
                  "项目：支付网关重构（TPS 3k→12k），风控规则引擎（规则热更新）。",
        "jd": "资深 Java 后端工程师：8年以上经验，微服务架构设计，高并发系统调优，团队管理经验。",
    },
    {
        "title": "exp7-borderline",
        "resume": "3年开发经验，先做了一年半测试，后转 Java 开发。会 Spring Boot 和 MyBatis，"
                  "参与过公司 CRM 系统维护，最近自学了 Python 和大模型应用，做过一个 RAG demo。"
                  "2022.03-2022.09 与 2022.06-2023.01 两段经历时间有重叠。",
        "jd": "Java 后端工程师：3年以上经验，微服务，分布式缓存，消息队列，有 AI 应用经验加分。",
    },
    {
        "title": "exp7-weak-mismatch",
        "resume": "应届生，专业市场营销，实习经历为新媒体运营与短视频剪辑，"
                  "熟练使用剪映、PS，了解 Excel 函数，无编程项目经历。",
        "jd": "高级算法工程师：硕士以上，扎实的机器学习基础，精通深度学习框架，有大规模推荐系统经验。",
    },
]


def http(method: str, url: str, body: dict | None = None, timeout: float = 60.0):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(url, data=data, method=method, headers={
        "Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def mysql(sql: str) -> str:
    cmd = ("source /opt/resumai-src/.env 2>/dev/null; "
           "docker exec resumai-mysql mysql -N -uroot -p\"$MYSQL_ROOT_PASSWORD\" "
           "\"$MYSQL_DATABASE\" -e " + json.dumps(sql))
    out = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True, timeout=30)
    return out.stdout.strip()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1")
    parser.add_argument("--label", required=True, help="e.g. t040 / t055 / t070")
    parser.add_argument("--threshold", required=True)
    parser.add_argument("--out", default=str(ROOT / "reports" / "experiments"))
    args = parser.parse_args()
    base = args.base.rstrip("/")

    rows = []
    for case in CASES:
        conv = http("POST", f"{base}/api/conversations", {
            "title": case["title"], "resumeText": case["resume"],
            "jobDescription": case["jd"], "jobCategory": "TECH"})
        cid = conv["conversationId"]
        turn = http("POST", f"{base}/api/conversations/{cid}/messages", {
            "clientMessageId": f"exp7-{args.label}-{int(time.time()*1000)}",
            "content": "请对这份简历做完整评估", "queueMode": "collect"})
        run_id = turn.get("runId")
        status = ""
        started = time.monotonic()
        while time.monotonic() - started < 420:
            time.sleep(6)
            run = http("GET", f"{base}/api/runs/{run_id}")
            status = str(run.get("status") or "")
            if status in ("SUCCEEDED", "FAILED", "CANCELLED", "TIMED_OUT"):
                break
        run = http("GET", f"{base}/api/runs/{run_id}")
        replans = mysql(
            "SELECT COUNT(*) FROM run_event WHERE run_id='" + run_id +
            "' AND payload_json LIKE '%\"replanned\"%'") or "0"
        llm_calls = mysql(
            "SELECT COUNT(*) FROM run_event WHERE run_id='" + run_id +
            "' AND event_type LIKE 'llm.%started%'") or "?"
        rows.append({
            "case": case["title"], "runId": run_id, "status": status,
            "durationMs": run.get("durationMs"), "tokenCost": run.get("tokenCost"),
            "replanEvents": int(replans.splitlines()[-1] or 0),
            "llmStartEvents": llm_calls.splitlines()[-1],
        })
        print(json.dumps(rows[-1], ensure_ascii=False))

    report = {
        "experiment": "replan_threshold_sweep",
        "label": args.label,
        "threshold": args.threshold,
        "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "rows": rows,
        "summary": {
            "succeeded": sum(1 for r in rows if r["status"] == "SUCCEEDED"),
            "totalReplans": sum(r["replanEvents"] for r in rows),
            "avgDurationMs": round(sum(r["durationMs"] or 0 for r in rows) / max(1, len(rows))),
        },
    }
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"replan_sweep_{args.label}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report["summary"], ensure_ascii=False))
    print(f"report -> {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
