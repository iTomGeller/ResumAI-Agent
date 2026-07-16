"""
检索质量指标（二值相关性）。

ranked: 排序后的 doc_id 列表（下标 0 为最相关）
relevant: 相关 doc_id 集合（来自 ground truth）
"""
import math


def precision_at_k(ranked, relevant, k):
    if k <= 0:
        return 0.0
    top = ranked[:k]
    hit = sum(1 for d in top if d in relevant)
    return hit / float(k)


def recall_at_k(ranked, relevant, k):
    if not relevant:
        return 0.0
    top = ranked[:k]
    hit = sum(1 for d in top if d in relevant)
    return hit / float(len(relevant))


def mrr(ranked, relevant):
    for i, d in enumerate(ranked):
        if d in relevant:
            return 1.0 / (i + 1)
    return 0.0


def ndcg_at_k(ranked, relevant, k):
    if not relevant:
        return 0.0
    dcg = 0.0
    for i, d in enumerate(ranked[:k]):
        if d in relevant:
            dcg += 1.0 / math.log2(i + 2)  # i 从 0 开始 -> log2(i+2)
    ideal_hits = min(len(relevant), k)
    idcg = sum(1.0 / math.log2(i + 2) for i in range(ideal_hits))
    return dcg / idcg if idcg > 0 else 0.0


def evaluate_run(ranked, relevant, ks):
    """对单条查询计算全部指标。ks = {'precision':5,'recall':10,'ndcg':10}"""
    return {
        "precision_at_%d" % ks["precision"]: precision_at_k(ranked, relevant, ks["precision"]),
        "recall_at_%d" % ks["recall"]: recall_at_k(ranked, relevant, ks["recall"]),
        "mrr": mrr(ranked, relevant),
        "ndcg_at_%d" % ks["ndcg"]: ndcg_at_k(ranked, relevant, ks["ndcg"]),
    }
