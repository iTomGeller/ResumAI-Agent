"""
根据 metrics.json / runs.json 生成对比图（PNG，嵌入 LaTeX）。

图：
  fig_quality_bars.png   各方案 P@5 / Recall@10 / MRR / nDCG@10 分组柱状图
  fig_latency_bars.png   各方案查询中位时延（对数轴）
  fig_tradeoff.png       nDCG@10 vs 中位时延（质量-时延权衡）
  fig_heatmap.png        每查询 × 每方案 nDCG@10 热力图
图中标签用英文，避免 matplotlib 中文字体依赖；正文（LaTeX）用中文。
"""
import json
import os

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

import config

METHOD_ORDER = ["BM25", "Dense", "Hybrid(RRF)", "Agentic", "GraphRAG"]
COLORS = {
    "BM25": "#4C72B0", "Dense": "#DD8452", "Hybrid(RRF)": "#55A868",
    "Agentic": "#C44E52", "GraphRAG": "#8172B3",
}


def load():
    with open(config.METRICS_PATH, "r", encoding="utf-8") as f:
        metrics = json.load(f)
    with open(config.RUNS_PATH, "r", encoding="utf-8") as f:
        runs = json.load(f)
    return metrics, runs


def fig_quality_bars(metrics):
    ks = metrics["meta"]["metric_ks"]
    metric_keys = ["precision_at_%d" % ks["precision"], "recall_at_%d" % ks["recall"],
                   "mrr", "ndcg_at_%d" % ks["ndcg"]]
    metric_labels = ["P@%d" % ks["precision"], "Recall@%d" % ks["recall"],
                     "MRR", "nDCG@%d" % ks["ndcg"]]
    methods = [m for m in METHOD_ORDER if m in metrics["methods"]]
    x = np.arange(len(metric_labels))
    width = 0.16
    fig, ax = plt.subplots(figsize=(9, 5))
    for i, m in enumerate(methods):
        vals = [metrics["methods"][m][k] for k in metric_keys]
        bars = ax.bar(x + (i - (len(methods) - 1) / 2) * width, vals, width,
                      label=m, color=COLORS[m])
        for b, v in zip(bars, vals):
            ax.text(b.get_x() + b.get_width() / 2, v + 0.005, "%.2f" % v,
                    ha="center", va="bottom", fontsize=6.5)
    ax.set_xticks(x)
    ax.set_xticklabels(metric_labels)
    ax.set_ylim(0, 1.08)
    ax.set_ylabel("Score (higher is better)")
    ax.set_title("Retrieval Quality by RAG Method (100 resumes, 28 queries)")
    ax.legend(ncol=5, fontsize=8, loc="upper center", bbox_to_anchor=(0.5, -0.08))
    ax.grid(axis="y", alpha=0.3)
    fig.tight_layout()
    p = os.path.join(config.FIG_DIR, "fig_quality_bars.png")
    fig.savefig(p, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return p


def fig_latency_bars(metrics):
    methods = [m for m in METHOD_ORDER if m in metrics["methods"]]
    vals = [metrics["methods"][m]["p50_latency_ms"] for m in methods]
    fig, ax = plt.subplots(figsize=(8, 4.5))
    bars = ax.bar(methods, vals, color=[COLORS[m] for m in methods])
    ax.set_yscale("log")
    ax.set_ylabel("Median query latency (ms, log scale)")
    ax.set_title("Per-query Latency by RAG Method (median)")
    for b, v in zip(bars, vals):
        ax.text(b.get_x() + b.get_width() / 2, v * 1.1,
                ("%.1f ms" % v) if v < 1000 else ("%.0f ms" % v),
                ha="center", va="bottom", fontsize=8)
    ax.grid(axis="y", alpha=0.3, which="both")
    fig.tight_layout()
    p = os.path.join(config.FIG_DIR, "fig_latency_bars.png")
    fig.savefig(p, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return p


def fig_tradeoff(metrics):
    ks = metrics["meta"]["metric_ks"]
    nd = "ndcg_at_%d" % ks["ndcg"]
    methods = [m for m in METHOD_ORDER if m in metrics["methods"]]
    fig, ax = plt.subplots(figsize=(8, 5))
    for m in methods:
        x = metrics["methods"][m]["p50_latency_ms"]
        y = metrics["methods"][m][nd]
        ax.scatter(x, y, s=160, color=COLORS[m], edgecolors="black", zorder=3, label=m)
        ax.annotate(m, (x, y), textcoords="offset points", xytext=(8, 6), fontsize=9)
    ax.set_xscale("log")
    ax.set_xlabel("Median query latency (ms, log scale)  ->  slower")
    ax.set_ylabel("nDCG@%d  ->  better" % ks["ndcg"])
    ax.set_title("Quality vs Latency Trade-off")
    ax.grid(alpha=0.3, which="both")
    fig.tight_layout()
    p = os.path.join(config.FIG_DIR, "fig_tradeoff.png")
    fig.savefig(p, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return p


def fig_heatmap(metrics, runs):
    ks = metrics["meta"]["metric_ks"]
    nd = "ndcg_at_%d" % ks["ndcg"]
    methods = [m for m in METHOD_ORDER if m in runs]
    qids = list(runs[methods[0]].keys())
    mat = np.array([[runs[m][q]["metrics"][nd] for q in qids] for m in methods])
    fig, ax = plt.subplots(figsize=(12, 3.6))
    im = ax.imshow(mat, aspect="auto", cmap="RdYlGn", vmin=0, vmax=1)
    ax.set_yticks(range(len(methods)))
    ax.set_yticklabels(methods)
    ax.set_xticks(range(len(qids)))
    ax.set_xticklabels(qids, rotation=90, fontsize=6)
    ax.set_title("Per-query nDCG@%d Heatmap (green=good, red=poor)" % ks["ndcg"])
    fig.colorbar(im, ax=ax, fraction=0.025, pad=0.01)
    fig.tight_layout()
    p = os.path.join(config.FIG_DIR, "fig_heatmap.png")
    fig.savefig(p, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return p


def main():
    metrics, runs = load()
    paths = [
        fig_quality_bars(metrics),
        fig_latency_bars(metrics),
        fig_tradeoff(metrics),
        fig_heatmap(metrics, runs),
    ]
    for p in paths:
        print("wrote", p)


if __name__ == "__main__":
    main()
