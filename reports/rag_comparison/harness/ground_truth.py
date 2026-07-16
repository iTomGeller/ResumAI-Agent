"""
Ground truth（相关性标签）生成。

判定规则（确定性、可复现）：
    对查询 q 与简历 doc，
        overlap = |set(q.target_skills) ∩ set(doc.expectedSkills)|
    若 overlap >= q.min_overlap（默认 2），则 doc 对 q 相关（relevant=1），否则不相关。

说明：
  - 使用 manifest 的 expectedSkills（英文技能标签）作为权威语义信号，role 隐含其中
    （同一岗位簇的简历共享技能标签）。
  - 二值相关性，适用于 Precision@K / Recall@K / MRR / nDCG@K。
  - 检索器在检索时看不到 target_skills，仅看到查询文本，避免数据泄漏。
"""
import json
import config
import corpus as corpus_mod
import queries as queries_mod


def compute_ground_truth(corpus, query_list):
    gt = {}
    detail = {}
    for q in query_list:
        tset = set(q["target_skills"])
        min_overlap = q.get("min_overlap", config.DEFAULT_MIN_OVERLAP)
        rel = []
        overlaps = {}
        for doc in corpus:
            ov = len(tset & set(doc["expectedSkills"]))
            if ov >= min_overlap:
                rel.append(doc["id"])
                overlaps[doc["id"]] = ov
        gt[q["id"]] = rel
        detail[q["id"]] = {
            "cluster": q["cluster"],
            "min_overlap": min_overlap,
            "n_relevant": len(rel),
            "overlap_by_doc": overlaps,
        }
    return gt, detail


def main():
    corpus = corpus_mod.build_corpus()
    query_list = queries_mod.write_queries()
    gt, detail = compute_ground_truth(corpus, query_list)

    payload = {
        "rule": ("relevant(q, doc) = 1 iff |set(q.target_skills) & "
                 "set(doc.expectedSkills)| >= q.min_overlap (default %d)" % config.DEFAULT_MIN_OVERLAP),
        "n_docs": len(corpus),
        "n_queries": len(query_list),
        "relevant": gt,
        "detail": detail,
    }
    with open(config.GROUND_TRUTH_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    sizes = sorted((len(v), k) for k, v in gt.items())
    print("wrote ground_truth.json")
    print("relevant-set sizes (min/median/max): %d / %d / %d" % (
        sizes[0][0], sizes[len(sizes) // 2][0], sizes[-1][0]))
    print("per-query:")
    for q in query_list:
        print("  %-4s %-12s n_relevant=%d" % (q["id"], q["cluster"], len(gt[q["id"]])))
    empty = [k for k, v in gt.items() if len(v) < 3]
    if empty:
        print("WARNING: queries with <3 relevant:", empty)


if __name__ == "__main__":
    main()
