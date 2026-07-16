"""RAG A/B pressure test for embedding vs lexical vs hybrid retrieval.

Usage:
  python scripts/pressure_test_rag_ab.py http://8.138.10.189
"""
from __future__ import annotations

import json
import statistics
import sys
import time
import urllib.request
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


QUERIES = [
    "Java Spring Boot Kafka K8s 项目经验",
    "支付中台 重构 后端 架构",
    "高并发 稳定性 性能优化",
    "项目真实性 贡献边界 复杂度",
    "MySQL Redis Docker AI Agent 缺口",
]

RESUME_TEXT = """李四，6年Java后端，Spring Boot、Kafka、K8s。
主导支付中台重构，负责交易链路异步化、消息可靠性、服务容器化部署。
熟悉后端工程实践、线上排障、SQL 优化、日志与监控。GitHub: https://github.com/example-dev
"""


def load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    path = Path(__file__).resolve().parents[1] / ".deploy.local.env"
    if not path.exists():
        return env
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key.strip()] = value.strip()
    return env


def load_remote_token(env: dict[str, str]) -> str:
    host = env.get("ALIYUN_HOST")
    password = env.get("ALIYUN_PASSWORD")
    if not host or not password:
        return ""
    try:
        import paramiko
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect(
            host,
            username=env.get("ALIYUN_USER", "root"),
            password=password,
            look_for_keys=False,
            allow_agent=False,
            timeout=30,
        )
        _, stdout, _ = ssh.exec_command(
            "grep -E '^WORKFLOW_INTERNAL_TOKEN=' /opt/ai-resume-agent-platform/.env | tail -1",
            timeout=20,
        )
        line = stdout.read().decode("utf-8", errors="replace").strip()
        ssh.close()
        if "=" in line:
            return line.split("=", 1)[1].strip()
    except Exception as exc:
        print(f"[warn] fetch remote token failed: {exc}")
    return ""


def post_json(url: str, token: str, payload: dict) -> tuple[dict, float]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json", "X-Internal-Token": token},
        method="POST",
    )
    start = time.perf_counter()
    with urllib.request.urlopen(req, timeout=60) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    elapsed_ms = (time.perf_counter() - start) * 1000
    return body, elapsed_ms


def main() -> None:
    base = sys.argv[1].rstrip("/") if len(sys.argv) > 1 else "http://127.0.0.1"
    env = load_env()
    token = env.get("WORKFLOW_INTERNAL_TOKEN", "") or load_remote_token(env)
    if not token:
        raise SystemExit("WORKFLOW_INTERNAL_TOKEN missing in .deploy.local.env")

    url = f"{base}/api/internal/tools/resume-search"
    strategies = ["embedding", "lexical", "hybrid"]
    summary: dict[str, list[dict]] = {strategy: [] for strategy in strategies}

    for strategy in strategies:
        for query in QUERIES:
            body, elapsed_ms = post_json(
                url,
                token,
                {
                    "query": query,
                    "topK": 5,
                    "resumeText": RESUME_TEXT,
                    "strategy": strategy,
                },
            )
            row = {
                "query": query,
                "elapsedMs": round(elapsed_ms, 1),
                "hitCount": body.get("hitCount", 0),
                "topScore": body.get("topScore", 0),
                "fallbackUsed": body.get("fallbackUsed", False),
                "backend": body.get("backend"),
                "strategy": body.get("strategy"),
                "errorType": body.get("errorType"),
            }
            summary[strategy].append(row)
            print(f"[rag-ab] {strategy} {query} -> {row}")

    print("\n=== RAG A/B Summary ===")
    for strategy, rows in summary.items():
        latencies = [float(row["elapsedMs"]) for row in rows]
        hits = [int(row["hitCount"] or 0) for row in rows]
        fallbacks = sum(1 for row in rows if row["fallbackUsed"])
        avg_top = statistics.mean(float(row["topScore"] or 0) for row in rows)
        print(
            f"{strategy}: avgLatencyMs={statistics.mean(latencies):.1f} "
            f"p95LatencyMs={sorted(latencies)[int(len(latencies) * 0.95) - 1]:.1f} "
            f"avgHits={statistics.mean(hits):.2f} avgTopScore={avg_top:.3f} "
            f"fallbacks={fallbacks}/{len(rows)}"
        )


if __name__ == "__main__":
    main()
