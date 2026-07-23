#!/usr/bin/env python3
"""EXP-5: memory retrieval channel ablation (lexical vs semantic vs fused-max).

Seeds an isolated benchmark user scope (userId=exp5-bench, TTL 1 day) with 24
synthetic memories — half lexically-easy (query shares surface terms), half
lexically-hard (paraphrased, only semantically related) — then runs 14 labeled
queries through the internal memory search API on all three channels and
reports recall@5 / MRR per channel.

Runs on ECS: python3 harness/run_memory_ablation.py --base http://127.0.0.1
Reads WORKFLOW_INTERNAL_TOKEN from /opt/resumai-src/.env or environment.
"""
from __future__ import annotations

import argparse
import json
import os
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
USER = "exp5-bench"

# (key, type, content) — key is referenced by query gold lists below.
# Types must be post-V8 canonical: CONVERSATION / EPISODIC / PREFERENCE / FAILURE.
MEMORIES = [
    # lexical-easy: queries reuse the same surface terms
    ("pref_edu", "PREFERENCE", "HR 偏好：学历权重降低，重点看项目经验与工程落地能力"),
    ("pref_salary", "PREFERENCE", "HR 偏好：期望薪资超出预算 20% 以上的候选人直接标记风险"),
    ("fact_bytedance", "EPISODIC", "候选人张三在字节跳动负责推荐系统重排层，任职两年"),
    ("fact_award", "EPISODIC", "候选人李四获得过 2023 年 ACM-ICPC 亚洲区银牌"),
    ("lesson_timeline", "FAILURE", "教训：时间线重叠必须逐段核对社保记录，上次漏检导致误判"),
    ("lesson_project", "FAILURE", "教训：项目真实性追问要先问架构决策再问细节指标"),
    ("rule_english", "PREFERENCE", "规则：英文简历统一按补充规范评估，禁止直接翻译后套用中文标准"),
    ("rule_l5", "PREFERENCE", "规则：L5 级别要求有完整 agent harness 设计经验，包括预算强制与死循环防护"),
    ("pref_remote", "PREFERENCE", "HR 偏好：远程办公经验视为加分项，特别是跨时区协作"),
    ("fact_patent", "EPISODIC", "候选人王五持有两项分布式缓存相关发明专利"),
    ("lesson_ref", "FAILURE", "教训：背调联系人必须是直属上级，同事背书不作数"),
    ("rule_scoring", "PREFERENCE", "规则：评分与推荐结论必须一致，禁止高分低推或低分高推"),
    # lexical-hard: paraphrased — almost no surface term overlap with queries
    ("pref_edu_hard", "PREFERENCE", "看人先看做过什么东西、上过什么生产系统，文凭出身放最后"),
    ("pref_salary_hard", "PREFERENCE", "要价明显超过我们能给的区间就先打个问号再谈"),
    ("fact_bytedance_hard", "EPISODIC", "这位同学之前在某短视频大厂搞过 feed 流的精排与混排"),
    ("fact_award_hard", "EPISODIC", "他大学时拿过国际大学生程序设计竞赛的区域赛奖牌"),
    ("lesson_timeline_hard", "FAILURE", "以前吃过亏：履历日期对不上却没去查缴纳记录，结果看走眼"),
    ("lesson_project_hard", "FAILURE", "追问套路应当从为什么这么设计开始，再往数字上引"),
    ("rule_english_hard", "PREFERENCE", "海外格式的 CV 得走单独的一套评估口径，不能生搬国内模板"),
    ("rule_l5_hard", "PREFERENCE", "高级别的门槛是独立设计过带资源上限控制和防失控保护的智能体框架"),
    ("pref_remote_hard", "PREFERENCE", "在家办公也能高效交付的人可以加印象分，尤其带过海外团队的"),
    ("fact_patent_hard", "EPISODIC", "他名下有几个关于高并发存储加速的授权知识产权"),
    ("lesson_ref_hard", "FAILURE", "打听候选人情况得找他汇报线上的老板，平级说好话没参考价值"),
    ("rule_scoring_hard", "PREFERENCE", "打的分和给不给 offer 的口径要对得上，不许自相矛盾"),
]

# queries -> gold memory keys (easy+hard variants both count as relevant)
QUERIES = [
    ("学历和项目经验哪个更重要", ["pref_edu", "pref_edu_hard"]),
    ("候选人薪资要求太高怎么处理", ["pref_salary", "pref_salary_hard"]),
    ("张三在字节跳动做什么", ["fact_bytedance", "fact_bytedance_hard"]),
    ("候选人有什么竞赛获奖经历", ["fact_award", "fact_award_hard"]),
    ("时间线重叠应该怎么核实", ["lesson_timeline", "lesson_timeline_hard"]),
    ("项目真实性追问的正确顺序", ["lesson_project", "lesson_project_hard"]),
    ("英文简历怎么评估", ["rule_english", "rule_english_hard"]),
    ("L5 工程师的能力门槛是什么", ["rule_l5", "rule_l5_hard"]),
    ("远程工作经验加分吗", ["pref_remote", "pref_remote_hard"]),
    ("候选人有专利吗", ["fact_patent", "fact_patent_hard"]),
    ("背调应该找谁核实", ["lesson_ref", "lesson_ref_hard"]),
    ("评分和推荐结论矛盾怎么办", ["rule_scoring", "rule_scoring_hard"]),
    ("履历上的日期对不上要查什么", ["lesson_timeline", "lesson_timeline_hard"]),
    ("怎么判断智能体框架设计经验是否达标", ["rule_l5", "rule_l5_hard"]),
]


def load_token() -> str:
    token = os.environ.get("WORKFLOW_INTERNAL_TOKEN", "")
    if token:
        return token
    for env_path in ("/opt/resumai-src/.env", str(ROOT / ".env")):
        if os.path.isfile(env_path):
            with open(env_path, encoding="utf-8") as fh:
                for line in fh:
                    if line.startswith("WORKFLOW_INTERNAL_TOKEN="):
                        return line.split("=", 1)[1].strip()
    raise SystemExit("WORKFLOW_INTERNAL_TOKEN not found")


def http(method: str, url: str, body: dict | None, token: str, timeout: float = 60.0):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(url, data=data, method=method, headers={
        "Content-Type": "application/json", "X-Internal-Token": token})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def metrics(ranked: list[str], gold: set[str], k: int = 5) -> dict:
    top = ranked[:k]
    recall = sum(1 for g in gold if g in top) / max(1, len(gold))
    mrr = 0.0
    for rank, item in enumerate(ranked, start=1):
        if item in gold:
            mrr = 1.0 / rank
            break
    return {"recall": recall, "mrr": mrr}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1")
    parser.add_argument("--out", default=str(ROOT / "reports" / "experiments"))
    args = parser.parse_args()
    base = args.base.rstrip("/")
    token = load_token()
    api = f"{base}/api/internal/agent-runs"

    # 1. seed isolated memories, capture memoryId per key
    key_to_id: dict[str, str] = {}
    for key, mtype, content in MEMORIES:
        result = http("POST", f"{api}/memory/write", {
            "type": mtype, "ownerScope": "USER", "userId": USER,
            "content": content, "source": "exp5_benchmark", "sourceId": key,
            "confidence": 0.9, "sensitivityLevel": "NONE", "ttlDays": 1,
        }, token)
        key_to_id[key] = str(result.get("memoryId"))
    print(f"seeded {len(key_to_id)} memories under userId={USER}")
    # give the async vector indexer a moment
    time.sleep(20)

    # 2. run all queries on each channel
    id_to_key = {v: k for k, v in key_to_id.items()}
    channels = ("lexical", "semantic", "fused")
    results: dict[str, list[dict]] = {c: [] for c in channels}
    for query, gold_keys in QUERIES:
        gold_ids = {key_to_id[k] for k in gold_keys}
        for channel in channels:
            started = time.monotonic()
            response = http("POST", f"{api}/memory/search", {
                "query": query, "userId": USER, "topK": 5, "channel": channel,
                "consumerAgent": "PolicyEvolution",
                "includeBenchmarkSources": True,
            }, token)
            latency = (time.monotonic() - started) * 1000
            ranked = [str(h.get("memoryId")) for h in response.get("hits", [])]
            row = metrics(ranked, gold_ids)
            row["latencyMs"] = latency
            row["query"] = query
            row["hits"] = [id_to_key.get(r, r[:8]) for r in ranked]
            results[channel].append(row)

    report = {
        "experiment": "memory_channel_ablation",
        "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "queries": len(QUERIES),
        "memories": len(MEMORIES),
        "results": {},
        "perQuery": {c: results[c] for c in channels},
    }
    for channel in channels:
        rows = results[channel]
        report["results"][channel] = {
            "recall@5": round(sum(r["recall"] for r in rows) / len(rows), 4),
            "mrr": round(sum(r["mrr"] for r in rows) / len(rows), 4),
            "avgLatencyMs": round(sum(r["latencyMs"] for r in rows) / len(rows), 1),
        }
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "memory_ablation.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report["results"], ensure_ascii=False, indent=2))
    print(f"report -> {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
