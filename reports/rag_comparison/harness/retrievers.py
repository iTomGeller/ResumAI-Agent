"""
五种 RAG 检索形态：
  (a) BM25            —— rank_bm25 + 中英混合分词
  (b) Dense           —— sentence-transformers all-MiniLM-L6-v2（chunk + max-sim）；
                          若不可用则降级 sklearn TF-IDF（报告显式标注）
  (c) Hybrid          —— BM25 + Dense 的 RRF（Reciprocal Rank Fusion）融合
  (d) Agentic RAG     —— LLM 查询改写（扩展） + Hybrid 召回 + LLM 对 Top-N 重排
  (e) GraphRAG        —— LLM 抽取实体建「技能-候选人」知识图(networkx)，查询映射到技能节点后图遍历召回

检索器只接收查询文本（中英拼接），绝不接触 ground truth 的 target_skills。
"""
import json
import os
import re
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor

import numpy as np

import config
from corpus import tokenize

# ============================================================================
# 公共工具
# ============================================================================

def ranks_from_scores(scores):
    """返回 (降序排列的下标数组, 每个下标的 1-based 排名)。稳定排序。"""
    order = np.argsort(-scores, kind="stable")
    rankpos = np.empty(len(scores), dtype=np.int64)
    for r, idx in enumerate(order):
        rankpos[idx] = r + 1  # 1-based
    return order, rankpos


def _norm_skill(name):
    s = name.strip().lower()
    s = re.sub(r"\s+", " ", s)
    alias = {
        "k8s": "kubernetes", "springboot": "spring boot", "spring-boot": "spring boot",
        "node": "node.js", "nodejs": "node.js", "vue": "vue3", "vuejs": "vue3",
        "ts": "typescript", "ml": "machine learning", "pytorch": "pytorch",
        "高并发": "high concurrency", "微服务": "microservices",
    }
    return alias.get(s, s)


# 中文 -> 规范技能（小写）的同义词桥，仅用于 GraphRAG 查询映射兜底
_SYN = {
    "向量": ["vector search", "milvus", "embedding"],
    "向量检索": ["vector search", "milvus"],
    "向量数据库": ["vector search", "milvus"],
    "语义": ["embedding", "vector search"],
    "嵌入": ["embedding"],
    "重排": ["rerank"],
    "知识库": ["knowledge base"],
    "推荐": ["recommendation"],
    "可观测": ["observability"],
    "监控": ["prometheus", "grafana", "observability"],
    "数仓": ["data warehouse"],
    "流式": ["flink", "spark"],
    "实时": ["flink", "spark"],
    "高并发": ["high concurrency"],
    "微服务": ["microservices"],
    "微前端": ["micro-frontend"],
    "组件化": ["componentization"],
    "可视化": ["visualization"],
    "崩溃": ["crash governance"],
    "支付": ["payment sdk"],
    "风控": ["risk control"],
    "漏洞": ["vulnerability"],
    "合规": ["compliance"],
    "加密": ["encryption"],
    "审计": ["audit"],
    "需求分析": ["requirement analysis"],
    "用户研究": ["user research"],
    "商业化": ["monetization"],
    "工具编排": ["tool orchestration"],
    "智能体": ["agent"],
    "数据治理": ["data governance"],
}


# ============================================================================
# 索引：BM25 + Dense（共享，避免重复计算）
# ============================================================================

class RetrievalIndex:
    def __init__(self, corpus):
        self.corpus = corpus
        self.ids = [d["id"] for d in corpus]
        self.id2idx = {d["id"]: i for i, d in enumerate(corpus)}
        self.texts = [d["text"] for d in corpus]
        self.tokens = [d["tokens"] for d in corpus]

        self.build_seconds = {}

        # ---- BM25 ----
        from rank_bm25 import BM25Okapi
        t0 = time.time()
        self.bm25 = BM25Okapi(self.tokens)
        self.build_seconds["bm25"] = time.time() - t0

        # ---- Dense ----
        self.dense_backend = None  # "minilm" / "tfidf"
        self.dense_degraded = False
        self._model = None
        self._tfidf = None
        self._chunk_emb = None
        self._chunk_doc = None
        self._build_dense()

    # ------------------------------------------------------------------
    def _chunk(self, text, size=350, overlap=70, max_chunks=20):
        if len(text) <= size:
            return [text]
        out = []
        i = 0
        while i < len(text) and len(out) < max_chunks:
            out.append(text[i:i + size])
            i += size - overlap
        return out

    def _build_dense(self):
        t0 = time.time()
        try:
            from sentence_transformers import SentenceTransformer
            self._model = SentenceTransformer(config.EMBEDDING_MODEL)
            self.dense_backend = "minilm"

            chunk_texts, chunk_doc = [], []
            for di, text in enumerate(self.texts):
                for ch in self._chunk(text):
                    chunk_texts.append(ch)
                    chunk_doc.append(di)

            sig = self._corpus_sig() + "|minilm|chunk350"
            cache_npz = os.path.join(config.CACHE_DIR, "dense_chunks.npz")
            emb = None
            if os.path.exists(cache_npz):
                z = np.load(cache_npz, allow_pickle=True)
                if str(z.get("sig")) == sig:
                    emb = z["emb"]
                    chunk_doc = list(z["chunk_doc"])
            if emb is None:
                emb = self._model.encode(chunk_texts, batch_size=64,
                                         normalize_embeddings=True,
                                         show_progress_bar=False)
                emb = np.asarray(emb, dtype=np.float32)
                np.savez(cache_npz, emb=emb, chunk_doc=np.array(chunk_doc), sig=sig)
            self._chunk_emb = emb
            self._chunk_doc = np.array(chunk_doc)
        except Exception as e:  # noqa  —— 降级 TF-IDF
            self.dense_backend = "tfidf"
            self.dense_degraded = True
            self._dense_error = str(e)[:200]
            from sklearn.feature_extraction.text import TfidfVectorizer
            self._tfidf = TfidfVectorizer(tokenizer=tokenize, lowercase=False,
                                          token_pattern=None)
            self._tfidf_mat = self._tfidf.fit_transform(self.texts)
        self.build_seconds["dense"] = time.time() - t0

    def _corpus_sig(self):
        import hashlib
        h = hashlib.sha256()
        for d in self.corpus:
            h.update((d["id"] + str(d["charLen"])).encode("utf-8"))
        return h.hexdigest()[:16]

    # ------------------------------------------------------------------
    def bm25_scores(self, query_text):
        return np.asarray(self.bm25.get_scores(tokenize(query_text)), dtype=np.float64)

    def dense_scores(self, query_text):
        if self.dense_backend == "minilm":
            qv = self._model.encode([query_text], normalize_embeddings=True)[0]
            sims = self._chunk_emb @ qv  # (n_chunks,)
            doc = np.full(len(self.ids), -1.0, dtype=np.float64)
            for ci, di in enumerate(self._chunk_doc):
                if sims[ci] > doc[di]:
                    doc[di] = sims[ci]
            return doc
        else:
            qv = self._tfidf.transform([query_text])
            return np.asarray((self._tfidf_mat @ qv.T).toarray()).ravel()


# ============================================================================
# (a) BM25
# ============================================================================

class BM25Retriever:
    name = "BM25"

    def __init__(self, index):
        self.index = index

    def search(self, query_text, top_k=config.RANK_DEPTH):
        scores = self.index.bm25_scores(query_text)
        order, _ = ranks_from_scores(scores)
        return [self.index.ids[i] for i in order[:top_k]]


# ============================================================================
# (b) Dense Embedding
# ============================================================================

class DenseRetriever:
    name = "Dense"

    def __init__(self, index):
        self.index = index

    def search(self, query_text, top_k=config.RANK_DEPTH):
        scores = self.index.dense_scores(query_text)
        order, _ = ranks_from_scores(scores)
        return [self.index.ids[i] for i in order[:top_k]]


# ============================================================================
# (c) Hybrid（RRF）
#   RRF(d) = 1/(K + rank_bm25(d)) + 1/(K + rank_dense(d))，K=60，rank 为 1-based
# ============================================================================

class HybridRetriever:
    name = "Hybrid(RRF)"

    def __init__(self, index, k=config.RRF_K):
        self.index = index
        self.k = k

    def rrf_scores(self, query_text):
        bm = self.index.bm25_scores(query_text)
        dn = self.index.dense_scores(query_text)
        _, rb = ranks_from_scores(bm)
        _, rd = ranks_from_scores(dn)
        return 1.0 / (self.k + rb) + 1.0 / (self.k + rd)

    def search(self, query_text, top_k=config.RANK_DEPTH):
        rrf = self.rrf_scores(query_text)
        order, _ = ranks_from_scores(rrf)
        return [self.index.ids[i] for i in order[:top_k]]


# ============================================================================
# (d) Agentic RAG：查询改写 + Hybrid 召回 + LLM 重排
# ============================================================================

class AgenticRetriever:
    name = "Agentic"

    def __init__(self, index, hybrid, llm):
        self.index = index
        self.hybrid = hybrid
        self.llm = llm

    def _rewrite(self, query_text):
        sys = ("你是检索查询改写器。给定一条招聘检索查询，扩展同义词、相关技术栈与中英文别名，"
               "用于在中英文混合的简历库中提升召回。只输出 JSON。")
        user = ('查询："%s"\n'
                '请输出 JSON：{"expanded": "一行扩展关键词，中英文混合，空格分隔"}。'
                "不要解释。" % query_text)
        try:
            raw = self.llm.chat(user, system=sys, temperature=0.0, max_tokens=200, json_mode=True)
            obj = json.loads(raw)
            exp = obj.get("expanded", "")
            return (query_text + " " + exp).strip()
        except Exception:
            return query_text

    def _rerank(self, query_text, cand_ids):
        docs = []
        for cid in cand_ids:
            d = self.index.corpus[self.index.id2idx[cid]]
            snippet = re.sub(r"\s+", " ", d["text"])[:350]
            docs.append({"id": cid, "snippet": snippet})
        sys = ("你是检索重排器。根据查询与候选简历摘要，按相关性从高到低排序。只输出 JSON。")
        user = ("查询：%s\n候选（id + 摘要）：\n%s\n\n"
                '输出 JSON：{"ranking": ["id1","id2",...]}，包含全部候选 id，按相关性降序。'
                % (query_text, json.dumps(docs, ensure_ascii=False)))
        try:
            raw = self.llm.chat(user, system=sys, temperature=0.0, max_tokens=600, json_mode=True)
            obj = json.loads(raw)
            order = [x for x in obj.get("ranking", []) if x in set(cand_ids)]
            for cid in cand_ids:  # 补齐遗漏
                if cid not in order:
                    order.append(cid)
            return order
        except Exception:
            return cand_ids

    def search(self, query_text, top_k=config.RANK_DEPTH):
        aug = self._rewrite(query_text)
        rrf = self.hybrid.rrf_scores(aug)
        order, _ = ranks_from_scores(rrf)
        base = [self.index.ids[i] for i in order]
        cands = base[:config.AGENTIC_CANDIDATES]
        reranked = self._rerank(query_text, cands)
        rest = [x for x in base if x not in set(reranked)]
        return (reranked + rest)[:top_k]


# ============================================================================
# (e) GraphRAG：实体抽取 -> 技能图 -> 查询映射 -> 图遍历
# ============================================================================

class GraphRAGRetriever:
    name = "GraphRAG"

    def __init__(self, index, llm, bm25):
        self.index = index
        self.llm = llm
        self.bm25 = bm25
        self.graph = None
        self.skill_labels = set()
        self.doc_skills = {}
        self.extract_calls = 0
        self.build_seconds = 0.0

    # ---- 实体抽取（每份简历 1 次，缓存到 graph_cache.json） ----
    def _extract_one(self, doc):
        text = re.sub(r"\s+", " ", doc["text"])[:2500]
        sys = ("你是简历信息抽取器。抽取候选人的技术技能/工具/框架/领域，"
               "用规范英文名称（如 Java, Spring Boot, Kafka, Kubernetes, RAG, Vue3）。只输出 JSON。")
        user = ('简历正文：\n"""%s"""\n\n'
                '输出 JSON：{"skills": ["规范英文技能", ...]}，10-18 个，去重。' % text)
        raw = self.llm.chat(user, system=sys, temperature=0.0, max_tokens=400, json_mode=True)
        obj = json.loads(raw)
        skills = obj.get("skills", [])
        return [_norm_skill(s) for s in skills if isinstance(s, str) and s.strip()]

    def build(self):
        t0 = time.time()
        cache = {}
        if os.path.exists(config.GRAPH_CACHE):
            try:
                cache = json.load(open(config.GRAPH_CACHE, "r", encoding="utf-8"))
            except Exception:
                cache = {}

        todo = [d for d in self.index.corpus if d["id"] not in cache]

        def work(d):
            try:
                return d["id"], self._extract_one(d)
            except Exception:
                # 兜底：从正文用已知技能词表做正则抽取（避免单点失败）
                return d["id"], self._fallback_extract(d)

        if todo:
            with ThreadPoolExecutor(max_workers=config.LLM_WORKERS) as ex:
                for did, skills in ex.map(work, todo):
                    cache[did] = skills
            json.dump(cache, open(config.GRAPH_CACHE, "w", encoding="utf-8"),
                      ensure_ascii=False, indent=2)

        self.extract_calls = len(todo)
        self.doc_skills = cache

        import networkx as nx
        G = nx.Graph()
        for did, skills in cache.items():
            G.add_node(did, kind="candidate")
            for s in set(skills):
                snode = "skill::" + s
                G.add_node(snode, kind="skill")
                G.add_edge(did, snode, weight=1.0)
                self.skill_labels.add(s)
        self.graph = G
        self.build_seconds = time.time() - t0
        return self

    def _fallback_extract(self, doc):
        """LLM 失败时的兜底：从该简历正文里正则匹配其自身 expectedSkills 词形
        （只读该简历自己的标签，不读 ground truth）。"""
        low = doc["text"].lower()
        found = [_norm_skill(s) for s in doc.get("expectedSkills", []) if s.lower() in low]
        return found or [_norm_skill(s) for s in doc.get("expectedSkills", [])[:6]]

    # ---- 查询 -> 技能节点（仅用查询文本，确定性） ----
    def map_query_to_skills(self, query_text):
        low = query_text.lower()
        matched = set()
        for s in self.skill_labels:
            if s in low:
                matched.add(s)
                continue
            parts = re.findall(r"[a-z0-9]+", s)
            if parts and all(p in low for p in parts):
                matched.add(s)
        for zh, sks in _SYN.items():
            if zh in low:
                for s in sks:
                    if s in self.skill_labels:
                        matched.add(s)
        return matched

    def search(self, query_text, top_k=config.RANK_DEPTH):
        G = self.graph
        matched = self.map_query_to_skills(query_text)
        score = defaultdict(float)
        hit = defaultdict(int)

        matched_nodes = ["skill::" + s for s in matched if ("skill::" + s) in G]
        for snode in matched_nodes:
            for cand in G.neighbors(snode):
                score[cand] += G[snode][cand]["weight"]
                hit[cand] += 1

        # 一跳扩展：与命中技能共现的相关技能（图结构增益）
        related = Counter()
        mset = set(matched_nodes)
        for snode in matched_nodes:
            for cand in G.neighbors(snode):
                for s2 in G.neighbors(cand):
                    if s2 not in mset:
                        related[s2] += 1
        for s2, _ in related.most_common(6):
            for cand in G.neighbors(s2):
                score[cand] += G[s2][cand]["weight"] * config.GRAPH_HOP_DECAY

        # BM25 作为同分内部的细排信号 + 不足时补齐
        bm = self.index.bm25_scores(query_text)
        bm_by_id = {self.index.ids[i]: bm[i] for i in range(len(self.index.ids))}

        ranked = sorted(score.keys(),
                        key=lambda c: (score[c], hit[c], bm_by_id.get(c, 0.0)),
                        reverse=True)
        if len(ranked) < top_k:
            for cid in [self.index.ids[i] for i in ranks_from_scores(bm)[0]]:
                if cid not in score:
                    ranked.append(cid)
                if len(ranked) >= top_k:
                    break
        return ranked[:top_k]
