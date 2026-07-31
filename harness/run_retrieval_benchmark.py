#!/usr/bin/env python3
"""Retrieval quality benchmark (EXP-1/2/3/4).

Runs labeled retrieval cases against the live stack and reports recall@k,
precision@k, MRR, latency and (for remote embeddings) cost per 1k queries.

Case format (testdata/benchmark/retrieval_cases.json):
  {"kind": "jd_match",  "query": "<resume text>",  "goldIds": ["jd-001"]}
  {"kind": "knowledge", "query": "<hr question>",  "goldDocTitles": ["时间线风险判定标准"]}

Experiments:
  --exp strategy   vector-only / lexical-only / hybrid weight grid (EXP-3)
  --exp rerank     hybrid vs hybrid+rerank (EXP-4)
  --exp embedding  compare under different EMBEDDING_MODEL deployments (EXP-1;
                   run once per deployed provider, results merged offline)
  --exp chunking   knowledge retrieval under different chunk configs (EXP-2;
                   run once per deployed chunk config)

Usage (ECS):
  python3 harness/run_retrieval_benchmark.py --base http://127.0.0.1 \
      --exp strategy --out reports/experiments
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]


def http(method: str, url: str, body: Optional[dict] = None,
         timeout: float = 60.0) -> Any:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(url, data=data, method=method, headers={
        "Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def metrics_at_k(ranked_ids: List[str], gold: List[str], k: int = 5) -> Dict[str, float]:
    top = ranked_ids[:k]
    gold_set = set(gold)
    hits = [1 if item in gold_set else 0 for item in top]
    recall = sum(1 for g in gold_set if g in top) / max(1, len(gold_set))
    precision = sum(hits) / max(1, len(top))
    mrr = 0.0
    for rank, item in enumerate(ranked_ids, start=1):
        if item in gold_set:
            mrr = 1.0 / rank
            break
    dcg = sum(hit / math.log2(rank + 1)
              for rank, hit in enumerate(hits, start=1))
    ideal_hits = min(len(gold_set), k)
    ideal_dcg = sum(1.0 / math.log2(rank + 1)
                    for rank in range(1, ideal_hits + 1))
    ndcg = dcg / ideal_dcg if ideal_dcg else 0.0
    return {
        "recall": recall,
        "precision": precision,
        "mrr": mrr,
        "ndcg": ndcg,
    }


def run_jd_case(base: str, case: Dict[str, Any],
                options: Dict[str, Any]) -> Dict[str, Any]:
    started = time.monotonic()
    response = http("POST", f"{base}/api/rag/preview", {
        "resumeText": case["query"],
        "options": options,
    })
    latency_ms = (time.monotonic() - started) * 1000
    candidates = response.get("candidates") or []
    ranked = [str(c.get("jdId")) for c in candidates]
    out = metrics_at_k(ranked, [str(g) for g in case.get("goldIds") or []])
    out["latencyMs"] = latency_ms
    return out


def run_knowledge_case(base: str, case: Dict[str, Any]) -> Dict[str, Any]:
    started = time.monotonic()
    response = http("POST", f"{base}/api/rag/knowledge-base/search", {
        "query": case["query"], "topK": 5})
    latency_ms = (time.monotonic() - started) * 1000
    chunks = response.get("chunks") or []
    ranked_titles = [str(c.get("title")) for c in chunks]
    out = metrics_at_k(ranked_titles, [str(g) for g in case.get("goldDocTitles") or []])
    out["latencyMs"] = latency_ms
    return out


def summarize(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not rows:
        return {}
    latencies = sorted(r["latencyMs"] for r in rows)
    p95 = latencies[min(len(latencies) - 1, int(0.95 * (len(latencies) - 1)))]
    return {
        "cases": len(rows),
        "recall@5": round(sum(r["recall"] for r in rows) / len(rows), 4),
        "precision@5": round(sum(r["precision"] for r in rows) / len(rows), 4),
        "mrr": round(sum(r["mrr"] for r in rows) / len(rows), 4),
        "nDCG@5": round(sum(r["ndcg"] for r in rows) / len(rows), 4),
        "avgLatencyMs": round(statistics.mean(latencies), 1),
        "p95LatencyMs": round(p95, 1),
    }


def strategy_variants() -> Dict[str, Dict[str, Any]]:
    base = {"topK": 5, "rrfK": 60, "rerankerEnabled": False}
    variants: Dict[str, Dict[str, Any]] = {
        "vector_only": {**base, "strategy": "vector"},
        "lexical_bm25_only": {**base, "strategy": "lexical"},
    }
    for weight in (0.5, 0.6, 0.7, 0.8):
        variants[f"hybrid_w{weight}"] = {
            **base, "strategy": "hybrid",
            "semanticWeight": weight, "keywordWeight": round(1 - weight, 2)}
    return variants


def rerank_variants() -> Dict[str, Dict[str, Any]]:
    base = {"topK": 5, "rrfK": 60, "strategy": "hybrid",
            "semanticWeight": 0.7, "keywordWeight": 0.3}
    return {
        "hybrid": {**base, "rerankerEnabled": False},
        "hybrid_rerank": {**base, "rerankerEnabled": True},
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1")
    parser.add_argument("--cases", default=str(
        ROOT / "testdata" / "benchmark" / "retrieval_cases.json"))
    parser.add_argument("--exp", choices=["strategy", "rerank", "embedding", "chunking"],
                        default="strategy")
    parser.add_argument("--label", default="",
                        help="deployment label for embedding/chunking runs "
                             "(e.g. minilm-384 / te3small-1536 / chunkC-512-15)")
    parser.add_argument("--out", default=str(ROOT / "reports" / "experiments"))
    args = parser.parse_args()
    base = args.base.rstrip("/")
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    cases = json.loads(Path(args.cases).read_text(encoding="utf-8"))
    jd_cases = [c for c in cases if c.get("kind") == "jd_match"]
    kb_cases = [c for c in cases if c.get("kind") == "knowledge"]
    print(f"loaded {len(jd_cases)} jd cases, {len(kb_cases)} knowledge cases")

    report: Dict[str, Any] = {
        "experiment": args.exp,
        "label": args.label,
        "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "results": {},
    }

    if args.exp in ("strategy", "rerank"):
        variants = strategy_variants() if args.exp == "strategy" else rerank_variants()
        for name, options in variants.items():
            rows = []
            for case in jd_cases:
                try:
                    rows.append(run_jd_case(base, case, options))
                except (urllib.error.URLError, urllib.error.HTTPError) as exc:
                    print(f"  case failed ({name}): {exc}")
            report["results"][name] = summarize(rows)
            print(f"[{name}] {report['results'][name]}")
    else:
        # embedding / chunking: the deployment IS the variable — one labeled
        # run per deployed config, merged offline across labels.
        jd_rows = []
        for case in jd_cases:
            try:
                jd_rows.append(run_jd_case(base, case, {
                    "topK": 5, "strategy": "hybrid",
                    "semanticWeight": 0.7, "keywordWeight": 0.3, "rrfK": 60,
                    "rerankerEnabled": False}))
            except (urllib.error.URLError, urllib.error.HTTPError) as exc:
                print(f"  jd case failed: {exc}")
        kb_rows = []
        for case in kb_cases:
            try:
                kb_rows.append(run_knowledge_case(base, case))
            except (urllib.error.URLError, urllib.error.HTTPError) as exc:
                print(f"  kb case failed: {exc}")
        report["results"]["jd_match"] = summarize(jd_rows)
        report["results"]["knowledge"] = summarize(kb_rows)
        print(f"[jd_match] {report['results']['jd_match']}")
        print(f"[knowledge] {report['results']['knowledge']}")

    suffix = f"_{args.label}" if args.label else ""
    path = out_dir / f"retrieval_{args.exp}{suffix}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"report -> {path}")

    # CI regression gate (EXP-3): compare against the frozen baseline.
    baseline_path = ROOT / "testdata" / "benchmark" / "retrieval_baseline.json"
    if args.exp == "strategy" and baseline_path.exists():
        baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
        best = max(report["results"].items(),
                   key=lambda kv: kv[1].get("recall@5", 0))
        floor = float(baseline.get("recall@5", 0)) - 0.02
        if best[1].get("recall@5", 0) < floor:
            print(f"REGRESSION: best recall@5 {best[1]['recall@5']} < baseline floor {floor}")
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
