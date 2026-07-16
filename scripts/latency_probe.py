"""Local latency probe for DeepSeek (LLM-only, no ECS/docker).

Quantifies generation throughput and tests whether a tighter eval prompt reduces
ReportAgent/eval latency without losing structure. Run: python scripts/latency_probe.py
"""
from __future__ import annotations

import json
import time
import urllib.request
from pathlib import Path

API = "https://api.deepseek.com/v1/chat/completions"
MODEL = "deepseek-chat"


def _load_key() -> str:
    env_path = Path(__file__).resolve().parents[1] / ".deploy.local.env"
    for line in env_path.read_text(encoding="utf-8").splitlines():
        if line.startswith("DEEPSEEK_API_KEY="):
            return line.split("=", 1)[1].strip()
    raise SystemExit("DEEPSEEK_API_KEY not found in .deploy.local.env")


KEY = _load_key()

SAMPLE_RESUME = (
    "黄义健，6年Java后端。字节跳动 AgentOps 平台：排查线上实例 mem rss 高占用（argos监控+gc日志+heap dump 定位到大json日志），"
    "排查美国OCI机房日志查询故障（补齐索引+FaaS多机房对账），分析 searchagent 接口 P99 耗时（file sort 慢SQL，gh-ost 加联合索引），"
    "重构 PE 管理服务（动态模板热替换），ModelHub/方舟模型平台统一接入与参数归一化。ResumAI Agent：DAG 编排、RAG、Milvus、DeepSeek 封装、双视图、Prometheus/Grafana。"
    "技能：Java并发、Spring/MyBatis、MySQL索引事务锁MVCC、Redis、LLM、RAG、AI Agent、Docker。"
)

TECH_PROMPT_BASE = """你是技术评估专家。必须基于完整简历原文评分，embedding RAG 仅用于定位和补充证据。
硬性规则：1.resumeText 是主证据 2.每个结论含 evidenceSource 3.维度给 evidenceQuotes 4.用 routingHints 决定重点。
输出严格 JSON：{"dimensions":[],"overallTechScore":72,"highlights":[],"weaknesses":[],"evidenceSource":"","toolHealth":{}}"""

TECH_PROMPT_TIGHT = """你是技术评估专家。基于简历原文评分。只输出紧凑 JSON，最多 3 个 dimensions，每个 dimension 的 evidenceQuotes 最多 1 条且 <=40 字，highlights/weaknesses 各最多 3 条且每条 <=40 字。不要任何多余解释。
输出严格 JSON：{"dimensions":[{"name":"","score":0,"evidenceSource":"resume_text","evidenceQuotes":[]}],"overallTechScore":72,"highlights":[],"weaknesses":[],"evidenceSource":"resume_text_only","toolHealth":{}}"""

USER = f"resumeText：{SAMPLE_RESUME}\nroutingHints：[Java并发,性能优化,AI Agent工程]\nrequiredSkills：[Java,Spring,MySQL,Redis,RAG]"


def call(system: str, max_tokens: int) -> tuple[float, int, str]:
    body = json.dumps({
        "model": MODEL,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": USER}],
        "temperature": 0.2,
        "max_tokens": max_tokens,
    }).encode()
    req = urllib.request.Request(API, data=body, headers={
        "Content-Type": "application/json", "Authorization": f"Bearer {KEY}"})
    t0 = time.time()
    resp = json.loads(urllib.request.urlopen(req, timeout=120).read())
    dt = time.time() - t0
    usage = resp.get("usage", {})
    out_tokens = usage.get("completion_tokens", 0)
    content = resp["choices"][0]["message"]["content"]
    return dt, out_tokens, content


def main() -> None:
    print(f"{'variant':22s} {'max_tok':>7s} {'latency':>8s} {'out_tok':>7s} {'tok/s':>7s} {'valid_json':>10s}")
    cases = [
        ("tech_base", TECH_PROMPT_BASE, 900),
        ("tech_base", TECH_PROMPT_BASE, 700),
        ("tech_tight", TECH_PROMPT_TIGHT, 700),
        ("tech_tight", TECH_PROMPT_TIGHT, 500),
    ]
    for name, sysp, mt in cases:
        try:
            dt, ot, content = call(sysp, mt)
            try:
                json.loads(content[content.find("{"):content.rfind("}") + 1])
                valid = "yes"
            except Exception:
                valid = "no"
            tps = ot / dt if dt else 0
            print(f"{name:22s} {mt:7d} {dt:7.1f}s {ot:7d} {tps:7.1f} {valid:>10s}")
        except Exception as e:
            print(f"{name:22s} {mt:7d}  ERROR {repr(e)[:80]}")


if __name__ == "__main__":
    main()
