"""
评测主流程：建索引 -> 跑 5 种方案 -> 算指标 -> 落盘 metrics.json / runs.json。

所有指标为真实运行结果（不可编造）。Agentic/GraphRAG 的 LLM 调用经 DeepSeek，
结果缓存到磁盘以便复现。
"""
import json
import time
from collections import defaultdict
from datetime import datetime

import numpy as np

import config
import corpus as corpus_mod
import queries as queries_mod
import ground_truth as gt_mod
import metrics as metrics_mod
from llm_client import LLMClient
from retrievers import (RetrievalIndex, BM25Retriever, DenseRetriever,
                        HybridRetriever, AgenticRetriever, GraphRAGRetriever)


def _snapshot(llm):
    return (llm.n_calls, llm.prompt_tokens, llm.completion_tokens, llm.api_seconds)


def _delta_stats(before, after):
    calls = after[0] - before[0]
    pin = after[1] - before[1]
    pout = after[2] - before[2]
    secs = after[3] - before[3]
    cost = pin / 1e6 * config.DEEPSEEK_PRICE_IN_PER_M + pout / 1e6 * config.DEEPSEEK_PRICE_OUT_PER_M
    return {"api_calls": calls, "prompt_tokens": pin, "completion_tokens": pout,
            "api_seconds": round(secs, 1), "est_cost_usd": round(cost, 4)}


def run_method(retriever, query_list, gt, ks):
    per_query = {}
    agg = defaultdict(list)
    latencies = []
    for q in query_list:
        qtext = queries_mod.query_text(q)
        t0 = time.perf_counter()
        ranked = retriever.search(qtext, top_k=config.RANK_DEPTH)
        lat_ms = (time.perf_counter() - t0) * 1000.0
        latencies.append(lat_ms)
        rel = set(gt[q["id"]])
        m = metrics_mod.evaluate_run(ranked, rel, ks)
        for k, v in m.items():
            agg[k].append(v)
        per_query[q["id"]] = {"ranked": ranked, "latency_ms": round(lat_ms, 1), "metrics": m}
    summary = {k: float(np.mean(v)) for k, v in agg.items()}
    summary["avg_latency_ms"] = float(np.mean(latencies))
    summary["p50_latency_ms"] = float(np.median(latencies))
    return summary, per_query


def main():
    print("[1/6] 构建语料 ...")
    corpus = corpus_mod.build_corpus()
    query_list = queries_mod.write_queries()
    gt, _ = gt_mod.compute_ground_truth(corpus, query_list)
    # 同步落盘 ground_truth.json
    gt_mod.main()
    ks = config.METRIC_KS

    print("[2/6] 建索引（BM25 + Dense）...")
    index = RetrievalIndex(corpus)
    print("      dense backend = %s (degraded=%s)" % (index.dense_backend, index.dense_degraded))

    llm = LLMClient("deepseek")

    print("[3/6] 构建 GraphRAG 知识图（实体抽取，含缓存）...")
    graph = GraphRAGRetriever(index, llm, BM25Retriever(index))
    gb0 = _snapshot(llm)
    graph.build()
    gb1 = _snapshot(llm)
    graph_llm = _delta_stats(gb0, gb1)
    graph_llm["new_extractions"] = graph.extract_calls
    llm.save()
    print("      graph: %d skills, %d candidates, extract_calls=%d, build=%.1fs"
          % (len(graph.skill_labels), len(corpus), graph.extract_calls, graph.build_seconds))

    bm25 = BM25Retriever(index)
    dense = DenseRetriever(index)
    hybrid = HybridRetriever(index)
    agentic = AgenticRetriever(index, hybrid, llm)

    methods = [bm25, dense, hybrid, agentic, graph]

    print("[4/6] 跑评测 ...")
    results = {}
    runs = {}
    method_llm = {}
    for r in methods:
        b0 = _snapshot(llm)
        summary, per_query = run_method(r, query_list, gt, ks)
        b1 = _snapshot(llm)
        results[r.name] = summary
        runs[r.name] = per_query
        d = _delta_stats(b0, b1)
        if r.name == "GraphRAG":
            # 图查询本身不调用 LLM；LLM 成本在建图阶段
            d = {"api_calls": 0, "prompt_tokens": 0, "completion_tokens": 0,
                 "api_seconds": 0.0, "est_cost_usd": 0.0,
                 "index_build_llm": graph_llm}
        method_llm[r.name] = d
        print("      %-12s P@5=%.3f R@10=%.3f MRR=%.3f nDCG@10=%.3f lat=%.1fms"
              % (r.name, summary["precision_at_5"], summary["recall_at_10"],
                 summary["mrr"], summary["ndcg_at_10"], summary["avg_latency_ms"]))
    llm.save()

    print("[5/6] 写 metrics.json / runs.json ...")
    meta = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "n_docs": len(corpus),
        "n_queries": len(query_list),
        "dense_backend": index.dense_backend,
        "dense_degraded": index.dense_degraded,
        "embedding_model": config.EMBEDDING_MODEL,
        "rrf_k": config.RRF_K,
        "agentic_candidates": config.AGENTIC_CANDIDATES,
        "ranking_depth": config.RANK_DEPTH,
        "graph_hop_decay": config.GRAPH_HOP_DECAY,
        "metric_ks": ks,
        "llm_provider": llm.provider,
        "llm_model": llm.model,
    }
    payload = {
        "meta": meta,
        "index_build_seconds": {k: round(v, 2) for k, v in index.build_seconds.items()},
        "graph_build_seconds": round(graph.build_seconds, 2),
        "methods": results,
        "method_llm": method_llm,
        "llm_total": llm.stats(),
    }
    with open(config.METRICS_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    with open(config.RUNS_PATH, "w", encoding="utf-8") as f:
        json.dump(runs, f, ensure_ascii=False, indent=2)

    print("[6/6] 完成。LLM 总计：%s" % json.dumps(llm.stats(), ensure_ascii=False))
    print("metrics -> %s" % config.METRICS_PATH)


if __name__ == "__main__":
    main()
