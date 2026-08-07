#!/usr/bin/env python3
"""Shared retrieval primitives for the three-stage RAG benchmark.

The implementation intentionally has no dependency on the application code so
the same frozen corpus can compare algorithms that are not deployed yet.  All
remote model calls are real HTTP calls and are cached by model, dimension and
text hash.  A failed model is reported as unavailable; it is never silently
replaced by another model.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import random
import re
import statistics
import time
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence


@dataclass(frozen=True)
class Chunk:
    chunk_id: str
    doc_id: str
    title: str
    section: str
    text: str
    char_start: int
    char_end: int
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class Timings:
    rewrite_ms: float = 0.0
    sparse_ms: float = 0.0
    dense_ms: float = 0.0
    fusion_ms: float = 0.0
    rerank_ms: float = 0.0
    total_ms: float = 0.0


@dataclass
class RemoteStats:
    calls: int = 0
    failures: int = 0
    cache_hits: int = 0
    input_chars: int = 0
    usage_tokens: int = 0
    latency_ms: list[float] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        return {
            "calls": self.calls,
            "failures": self.failures,
            "cacheHits": self.cache_hits,
            "inputChars": self.input_chars,
            "usageTokens": self.usage_tokens,
            "latencyMs": percentile_summary(self.latency_ms),
        }


def stable_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def percentile(values: Sequence[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    pos = (len(ordered) - 1) * q
    lo, hi = int(math.floor(pos)), int(math.ceil(pos))
    if lo == hi:
        return float(ordered[lo])
    return float(ordered[lo] * (hi - pos) + ordered[hi] * (pos - lo))


def percentile_summary(values: Sequence[float]) -> dict[str, float]:
    if not values:
        return {"count": 0, "mean": 0.0, "p50": 0.0, "p95": 0.0, "p99": 0.0, "max": 0.0}
    return {
        "count": len(values),
        "mean": round(statistics.mean(values), 3),
        "p50": round(percentile(values, 0.50), 3),
        "p95": round(percentile(values, 0.95), 3),
        "p99": round(percentile(values, 0.99), 3),
        "max": round(max(values), 3),
    }


def normalize_text(text: str) -> str:
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[\t\u00a0]+", " ", text)
    text = re.sub(r"[ ]{2,}", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def split_markdown_sections(text: str, default_title: str) -> list[tuple[str, str, int, int]]:
    """Detect Markdown headings or common plain-Chinese JD labels.

    The plain-label branch is essential for product-entered JDs such as
    ``岗位职责：`` / ``任职要求：``; it does not require Markdown syntax.
    Documents without either signal stay as one section and are handled by the
    recursive candidates rather than receiving invented structure.
    """
    normalized = normalize_text(text)
    lines = normalized.splitlines(keepends=True)
    headings: list[tuple[int, int, str]] = []
    offset = 0
    for line in lines:
        clean = line.strip()
        match = re.match(r"^(#{1,6})\s+(.+)$", clean)
        if match:
            headings.append((offset, len(match.group(1)), match.group(2).strip()))
        offset += len(line)

    # PDF extractors frequently collapse a Chinese resume/JD into one physical
    # line.  Labels are still reliable boundaries even when they are not at a
    # line start, so scan the normalized text as well.  The left boundary avoids
    # splitting ordinary prose such as "负责岗位职责梳理".
    plain_pattern = re.compile(
        r"(?<![A-Za-z0-9_\u3400-\u9fff])"
        r"(岗位职责|工作职责|职位职责|工作内容|岗位描述|任职要求|职位要求|"
        r"必须技能|必要技能|技能要求|加分项|经验要求|生产场景(?:与考核题)?|"
        r"个人摘要|专业技能|技能|工作经历|项目经历|核心项目|故障与复盘|"
        r"教育背景|其他说明)\s*[：:]"
    )
    headings.extend((match.start(1), 2, match.group(1))
                    for match in plain_pattern.finditer(normalized))
    # Prefer a Markdown heading when two detectors point at the same offset.
    by_start: dict[int, tuple[int, int, str]] = {}
    for item in sorted(headings, key=lambda value: (value[0], value[1])):
        current = by_start.get(item[0])
        if current is None or item[1] < current[1]:
            by_start[item[0]] = item
    headings = sorted(by_start.values(), key=lambda value: value[0])
    if not headings:
        return [(default_title, normalized, 0, len(normalized))]

    sections: list[tuple[str, str, int, int]] = []
    title_stack: dict[int, str] = {0: default_title}
    first = headings[0][0]
    if first > 0 and normalized[:first].strip() and len(normalized[:first].strip()) >= 80:
        sections.append((default_title, normalized[:first].strip(), 0, first))
    for index, (start, level, heading) in enumerate(headings):
        end = headings[index + 1][0] if index + 1 < len(headings) else len(normalized)
        title_stack = {k: v for k, v in title_stack.items() if k < level}
        title_stack[level] = heading
        path = " > ".join(title_stack[k] for k in sorted(title_stack))
        content = normalized[start:end].strip()
        content_lines = content.splitlines()
        inline_body = ""
        if content_lines and re.match(r"^[^：:]{1,30}[：:]", content_lines[0]):
            inline_body = re.split(r"[：:]", content_lines[0], maxsplit=1)[1].strip()
        body_without_heading = (inline_body + "\n" + "\n".join(content_lines[1:])).strip()
        # A document H1 immediately followed by H2 carries context but no
        # standalone evidence.  Keep it in the section path, not as a tiny chunk.
        if content and body_without_heading:
            sections.append((path, content, start, end))
    return sections


def smart_windows(text: str, size: int, overlap: int) -> list[tuple[str, int, int]]:
    if size <= 0 or len(text) <= size:
        return [(text.strip(), 0, len(text))] if text.strip() else []
    overlap = max(0, min(overlap, size - 1))
    result: list[tuple[str, int, int]] = []
    start = 0
    while start < len(text):
        target = min(len(text), start + size)
        end = target
        if target < len(text):
            floor = start + max(80, int(size * 0.55))
            boundaries = [text.rfind(mark, floor, target) for mark in ("\n\n", "\n", "。", "；", ";", "，", ",", " ")]
            best = max(boundaries)
            if best > floor:
                end = best + (2 if text[best:best + 2] == "\n\n" else 1)
        piece = text[start:end].strip()
        if piece:
            left_trim = len(text[start:end]) - len(text[start:end].lstrip())
            right_trimmed = len(text[start:end].rstrip())
            result.append((piece, start + left_trim, start + right_trimmed))
        if end >= len(text):
            break
        next_start = max(start + 1, end - overlap)
        if next_start <= start:
            next_start = end
        start = next_start
    return result


def fixed_windows(text: str, size: int, overlap: int) -> list[tuple[str, int, int]]:
    """Character-window control: no language-aware boundary selection."""
    if size <= 0 or len(text) <= size:
        return [(text.strip(), 0, len(text))] if text.strip() else []
    overlap = max(0, min(overlap, size - 1))
    rows = []
    start = 0
    while start < len(text):
        end = min(len(text), start + size)
        raw = text[start:end]
        piece = raw.strip()
        if piece:
            left = len(raw) - len(raw.lstrip())
            rows.append((piece, start + left, start + len(raw.rstrip())))
        if end >= len(text):
            break
        start = max(start + 1, end - overlap)
    return rows


def production_resume_blocks(text: str) -> list[tuple[str, int, int]]:
    """Mirror ResumeRagService.splitResumeBlocks, including line fallback."""
    normalized = normalize_text(text)
    raw_blocks = [block.strip() for block in re.split(r"\n{2,}", normalized) if block.strip()]
    if len(raw_blocks) <= 1:
        raw_blocks = [line.strip() for line in normalized.splitlines() if line.strip()]
    if not raw_blocks and normalized:
        raw_blocks = [normalized]
    rows = []
    cursor = 0
    for block in raw_blocks:
        idx = normalized.find(block, cursor)
        if idx < 0:
            idx = cursor
        # Production retrieval truncates each scored block to 600 chars.
        piece = block[:600]
        rows.append((piece, idx, idx + len(piece)))
        cursor = idx + len(block)
    return rows


def production_kb_blocks(text: str, size: int, overlap: int) -> list[tuple[str, int, int]]:
    """Mirror KnowledgeBaseDocumentService.splitIntoBlocks exactly enough for A/B."""
    normalized = normalize_text(text)
    segments: list[str] = []
    current: list[str] = []
    boundary_re = re.compile(r"^(#{1,6}\s+.*|\d+[.)、]\s*.*|[一二三四五六七八九十]+[、.].*)$")
    for line in normalized.splitlines():
        trimmed = line.strip()
        if boundary_re.fullmatch(trimmed) and current:
            segments.append("\n".join(current).strip())
            current = []
        if trimmed:
            current.append(trimmed)
    if current:
        segments.append("\n".join(current).strip())
    if not segments:
        segments = [normalized]
    texts: list[str] = []
    for segment in segments:
        if len(segment) <= size:
            if texts and len(segment) < 30:
                texts[-1] = texts[-1] + "\n" + segment
            else:
                texts.append(segment)
        else:
            texts.extend(piece for piece, _, _ in fixed_windows(segment, size, overlap))
    rows = []
    cursor = 0
    for piece in texts:
        idx = normalized.find(piece, cursor)
        if idx < 0:
            idx = normalized.find(piece)
        if idx < 0:
            idx = cursor
        rows.append((piece, idx, idx + len(piece)))
        cursor = idx + len(piece)
    return rows


def semantic_sentence_spans(text: str) -> list[tuple[str, int, int]]:
    spans: list[tuple[str, int, int]] = []
    start = 0
    for match in re.finditer(r"[。！？!?；;\n]+", text):
        end = match.end()
        raw = text[start:end]
        piece = raw.strip()
        if piece:
            left = start + len(raw) - len(raw.lstrip())
            spans.append((piece, left, start + len(raw.rstrip())))
        start = end
    raw = text[start:]
    if raw.strip():
        left = start + len(raw) - len(raw.lstrip())
        spans.append((raw.strip(), left, start + len(raw.rstrip())))
    return spans


def cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    numerator = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    return numerator / max(1e-12, left_norm * right_norm)


def semantic_windows(text: str, target_size: int, max_size: int,
                     min_size: int, breakpoint_percentile: float,
                     overlap_sentences: int,
                     embed: Callable[[Sequence[str]], list[list[float]]] | None
                     ) -> list[tuple[str, int, int]]:
    """Group adjacent sentences and cut at embedding topic-change points."""
    sentences = semantic_sentence_spans(text)
    if not sentences:
        return []
    if len(sentences) == 1:
        return smart_windows(text, max_size or target_size, 0)
    if embed is None:
        raise RuntimeError("semantic chunking requires an embedding client")
    vectors = embed([sentence[0] for sentence in sentences])
    distances = [1.0 - cosine_similarity(vectors[index], vectors[index + 1])
                 for index in range(len(vectors) - 1)]
    threshold = percentile(distances, max(0.0, min(1.0, breakpoint_percentile / 100.0)))
    boundaries = {index + 1 for index, distance in enumerate(distances)
                  if distance >= threshold}
    target_size = max(80, target_size)
    max_size = max(target_size, max_size)
    min_size = max(40, min(min_size, target_size))
    overlap_sentences = max(0, overlap_sentences)

    rows: list[tuple[str, int, int]] = []
    group_start = 0
    cursor = 1
    while cursor <= len(sentences):
        start = sentences[group_start][1]
        end = sentences[cursor - 1][2]
        current_length = end - start
        at_end = cursor == len(sentences)
        next_end = sentences[cursor][2] if not at_end else end
        hard_break = not at_end and next_end - start > max_size
        semantic_break = (
            not at_end and cursor in boundaries and current_length >= min_size
            and current_length >= int(target_size * 0.55)
        )
        soft_break = (
            not at_end and current_length >= target_size
            and current_length >= int(max_size * 0.85)
        )
        if at_end or hard_break or semantic_break or soft_break:
            piece = text[start:end].strip()
            if piece:
                rows.append((piece, start, end))
            if at_end:
                break
            next_start = max(group_start + 1, cursor - overlap_sentences)
            group_start = next_start
            cursor = max(cursor, group_start + 1)
            continue
        cursor += 1
    return rows


def chunk_document(doc_id: str, title: str, text: str, strategy: str,
                   size: int, overlap: int,
                   explicit_sections: list[dict[str, Any]] | None = None,
                   semantic_options: dict[str, Any] | None = None,
                   semantic_embed: Callable[[Sequence[str]], list[list[float]]] | None = None) -> list[Chunk]:
    normalized = normalize_text(text)
    raw_sections: list[tuple[str, str, int, int, dict[str, Any]]] = []
    if explicit_sections:
        cursor = 0
        for section in explicit_sections:
            content = normalize_text(section.get("content", ""))
            section_title = section.get("title") or section.get("sectionId") or title
            raw_sections.append((section_title, content, cursor, cursor + len(content), {
                "sectionId": section.get("sectionId", section_title),
            }))
            cursor += len(content) + 2
    else:
        raw_sections = [(s, c, start, end, {}) for s, c, start, end in split_markdown_sections(normalized, title)]

    pieces: list[tuple[str, str, int, int, dict[str, Any]]] = []
    if strategy == "whole":
        pieces = [(title, normalized, 0, len(normalized), {})]
    elif strategy == "fixed":
        pieces = [(title, piece, start, end, {}) for piece, start, end in fixed_windows(normalized, size, overlap)]
    elif strategy == "recursive":
        pieces = [(title, piece, start, end, {}) for piece, start, end in smart_windows(normalized, size, overlap)]
    elif strategy == "production_resume":
        pieces = [(title, piece, start, end, {}) for piece, start, end in production_resume_blocks(normalized)]
    elif strategy == "production_kb":
        pieces = [(title, piece, start, end, {}) for piece, start, end in production_kb_blocks(normalized, size, overlap)]
    elif strategy == "semantic":
        options = semantic_options or {}
        pieces = [(title, piece, start, end, {}) for piece, start, end in semantic_windows(
            normalized, size, int(options.get("maxSize", max(size, 650))),
            int(options.get("minSize", 120)),
            float(options.get("breakpointPercentile", 75)),
            int(options.get("overlapSentences", 1)), semantic_embed)]
    elif strategy in {"section", "section_prefix", "section_semantic", "section_semantic_prefix"}:
        for section, content, base_start, _, metadata in raw_sections:
            # Overlap is allowed only inside an oversized section, never across headings.
            prefixed = strategy in {"section_prefix", "section_semantic_prefix"}
            prefix = f"文档：{title}\n章节：{section}\n" if prefixed else ""
            window_size = max(80, size - len(prefix)) if size > 0 else size
            window_overlap = min(overlap, max(0, window_size - 1))
            if strategy in {"section_semantic", "section_semantic_prefix"}:
                options = semantic_options or {}
                section_rows = semantic_windows(
                    content, window_size,
                    int(options.get("maxSize", max(window_size, 650))),
                    int(options.get("minSize", 120)),
                    float(options.get("breakpointPercentile", 75)),
                    int(options.get("overlapSentences", 1)), semantic_embed)
            else:
                section_rows = smart_windows(content, window_size, window_overlap)
            for piece, rel_start, rel_end in section_rows:
                body = piece
                if prefixed:
                    body = prefix + piece
                pieces.append((section, body, base_start + rel_start, base_start + rel_end, metadata))
    else:
        raise ValueError(f"unknown chunk strategy: {strategy}")

    chunks: list[Chunk] = []
    for index, (section, content, start, end, metadata) in enumerate(pieces):
        chunks.append(Chunk(
            chunk_id=f"{doc_id}#{index:03d}", doc_id=doc_id, title=title,
            section=section, text=content, char_start=start, char_end=end,
            metadata=metadata,
        ))
    return chunks


ALIASES = {
    "springboot": "spring boot", "spring-boot": "spring boot",
    "js": "javascript", "ts": "typescript", "k8s": "kubernetes",
    "大模型": "llm", "大型语言模型": "llm", "智能体": "agent",
    "检索增强生成": "rag", "向量数据库": "milvus", "消息队列": "mq",
    "关系型数据库": "sql", "持续集成": "ci", "持续交付": "cd",
    "站点可靠性": "sre", "基础设施即代码": "terraform",
}

DOMAIN_TERMS = sorted({
    "大模型", "智能体", "检索增强生成", "向量检索", "混合召回", "重排序", "知识库",
    "前端", "后端", "服务端", "数据库", "消息队列", "分库分表", "慢查询", "线程池",
    "机器学习", "深度学习", "自然语言", "目标检测", "图像分割", "数据工程", "数据分析",
    "实时数仓", "维度建模", "数据倾斜", "云原生", "容器安全", "代码审计", "自动化测试",
    "产品经理", "用户研究", "交互设计", "解决方案", "性能优化", "故障治理", "容量规划",
    "可观测性", "高可用", "灰度发布", "量化指标", "用户分层", "因果推断", "空窗期",
}, key=len, reverse=True)


def alias_normalize(text: str) -> str:
    lower = (text or "").lower()
    for source, target in ALIASES.items():
        lower = re.sub(rf"(?<![a-z0-9]){re.escape(source)}(?![a-z0-9])", f" {target} ", lower)
    return lower


def _latin_tokens(text: str) -> list[str]:
    return [token.strip("-_.") for token in re.findall(r"[a-z][a-z0-9+#_.-]{0,31}|\d+(?:\.\d+)?", text.lower()) if token.strip("-_.")]


def _chinese_runs(text: str) -> list[str]:
    return re.findall(r"[\u3400-\u9fff]+", text)


def tokenize(text: str, mode: str) -> list[str]:
    if mode in {"resume_current_phrase", "kb_current_weighted"}:
        return phrase_tokens(text)
    normalized = alias_normalize(text) if mode == "domain" else (text or "").lower()
    latin = _latin_tokens(normalized)
    result = list(latin)
    if mode == "jieba":
        try:
            import jieba  # type: ignore
        except ImportError as exc:
            raise RuntimeError("tokenizer=jieba requires jieba==0.42.1") from exc
        result.extend(tok.strip().lower() for tok in jieba.lcut(normalized)
                      if tok.strip() and not tok.isspace())
    elif mode == "domain":
        for run in _chinese_runs(normalized):
            cursor = 0
            while cursor < len(run):
                match = next((term for term in DOMAIN_TERMS if run.startswith(term, cursor)), None)
                if match:
                    result.append(match)
                    cursor += len(match)
                else:
                    if cursor + 1 < len(run):
                        result.append(run[cursor:cursor + 2])
                    else:
                        result.append(run[cursor])
                    cursor += 1
    else:
        for run in _chinese_runs(normalized):
            if mode == "unigram_bigram":
                result.extend(run)
            if len(run) == 1:
                result.append(run)
            else:
                result.extend(run[i:i + 2] for i in range(len(run) - 1))
    return [token for token in result if token and len(token) <= 40]


class BM25Index:
    def __init__(self, chunks: Sequence[Chunk], tokenizer: str, k1: float = 1.2, b: float = 0.75):
        self.chunks = list(chunks)
        self.tokenizer = tokenizer
        self.k1 = k1
        self.b = b
        self.tfs: list[Counter[str]] = []
        self.df: Counter[str] = Counter()
        self.lengths: list[int] = []
        for chunk in self.chunks:
            tf = Counter(tokenize(chunk.text, tokenizer))
            self.tfs.append(tf)
            self.df.update(tf.keys())
            self.lengths.append(sum(tf.values()))
        self.avgdl = statistics.mean(self.lengths) if self.lengths else 1.0

    def search(self, query: str, limit: int) -> list[tuple[Chunk, float]]:
        query_terms = list(dict.fromkeys(tokenize(query, self.tokenizer)))
        n = max(1, len(self.chunks))
        ranked: list[tuple[Chunk, float]] = []
        for chunk, tf, dl in zip(self.chunks, self.tfs, self.lengths):
            score = 0.0
            for term in query_terms:
                freq = tf.get(term, 0)
                if not freq:
                    continue
                df = self.df.get(term, 0)
                idf = math.log(1.0 + (n - df + 0.5) / (df + 0.5))
                denominator = freq + self.k1 * (1 - self.b + self.b * dl / max(self.avgdl, 1.0))
                score += idf * freq * (self.k1 + 1) / denominator
            if score > 0:
                ranked.append((chunk, score))
        ranked.sort(key=lambda row: (-row[1], row[0].chunk_id))
        return ranked[:limit]


def phrase_tokens(text: str) -> list[str]:
    return [token.strip() for token in re.split(r"[\s,，、/|；;:：()（）\[\]{}]+", text or "")
            if 2 <= len(token.strip()) <= 32 and not token.strip().isdigit()][:30]


class ResumeCurrentIndex:
    """Mirror ResumeRagService.lexicalRetrieve (BM25-like, not standard BM25)."""
    def __init__(self, chunks: Sequence[Chunk]):
        self.chunks = list(chunks)

    def search(self, query: str, limit: int) -> list[tuple[Chunk, float]]:
        terms = list(dict.fromkeys(phrase_tokens(query)))
        total = max(1, len(self.chunks))
        rows = []
        for chunk in self.chunks:
            lower = chunk.text.lower()
            score = 0.0
            for term in terms:
                needle = term.lower()
                tf = lower.count(needle)
                if not tf:
                    continue
                df = sum(needle in other.text.lower() for other in self.chunks)
                idf = math.log(1 + (total - df + 0.5) / (df + 0.5))
                score += (tf * 2.2 / (tf + 1.2)) * idf
            if score > 0:
                rows.append((chunk, score))
        rows.sort(key=lambda row: (-row[1], row[0].chunk_id))
        return rows[:limit]


KB_SYNONYMS = {
    "agent": ["智能体", "harness", "workflow", "dag"],
    "rag": ["检索", "召回", "向量", "embedding", "rerank"],
    "java": ["spring", "后端", "jvm"],
    "项目": ["系统", "平台", "中台", "贡献", "真实性"],
    "风险": ["核验", "验证", "缺口", "边界"],
}


def expand_kb_terms(terms: Sequence[str]) -> list[str]:
    expanded = list(terms)
    for term in terms:
        expanded.extend(KB_SYNONYMS.get(term.lower(), []))
    return list(dict.fromkeys(expanded))


class KnowledgeCurrentIndex:
    """Mirror the production weighted containment scorer called bm25_like."""
    def __init__(self, chunks: Sequence[Chunk]):
        self.chunks = list(chunks)

    @staticmethod
    def _coverage(text: str, terms: Sequence[str]) -> float:
        lower = (text or "").lower()
        return sum(term.lower() in lower for term in terms) / max(1, len(terms))

    def search(self, query: str, limit: int) -> list[tuple[Chunk, float]]:
        terms = phrase_tokens(query)
        expanded = expand_kb_terms(terms)
        rows = []
        for chunk in self.chunks:
            tags = " ".join(map(str, chunk.metadata.get("tags") or []))
            score = (
                0.25 * self._coverage(chunk.title, terms)
                + 0.20 * self._coverage(chunk.section, terms)
                + 0.42 * self._coverage(chunk.text, expanded)
                + 0.10 * self._coverage(tags, terms)
            )
            if score >= 0.12:
                rows.append((chunk, min(1.0, score)))
        rows.sort(key=lambda row: (-row[1], row[0].chunk_id))
        return rows[:limit]


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def dense_search(chunks: Sequence[Chunk], vectors: Sequence[Sequence[float]],
                 query_vector: Sequence[float], limit: int) -> list[tuple[Chunk, float]]:
    ranked = [(chunk, cosine(vector, query_vector)) for chunk, vector in zip(chunks, vectors)]
    ranked.sort(key=lambda row: (-row[1], row[0].chunk_id))
    return ranked[:limit]


def rrf_fuse(channels: Sequence[tuple[float, Sequence[tuple[Chunk, float]]]],
             rrf_k: int, limit: int) -> list[tuple[Chunk, float]]:
    scores: defaultdict[str, float] = defaultdict(float)
    chunks: dict[str, Chunk] = {}
    for weight, ranked in channels:
        for rank, (chunk, _) in enumerate(ranked, start=1):
            scores[chunk.chunk_id] += weight / (rrf_k + rank)
            chunks[chunk.chunk_id] = chunk
    rows = [(chunks[cid], score) for cid, score in scores.items()]
    rows.sort(key=lambda row: (-row[1], row[0].chunk_id))
    return rows[:limit]


def feature_rerank(query: str, ranked: Sequence[tuple[Chunk, float]],
                   tokenizer_mode: str) -> list[tuple[Chunk, float]]:
    terms = set(tokenize(query, tokenizer_mode))
    base_scores = [score for _, score in ranked]
    min_score = min(base_scores, default=0.0)
    max_score = max(base_scores, default=1.0)
    result = []
    for chunk, base in ranked:
        chunk_terms = set(tokenize(chunk.text, tokenizer_mode))
        coverage = len(terms & chunk_terms) / max(1, len(terms))
        base_norm = (base - min_score) / max(1e-9, max_score - min_score)
        section_bonus = 0.10 if any(term in chunk.section.lower() for term in re.findall(r"[a-z]+|[\u3400-\u9fff]{2,}", query.lower())) else 0.0
        length_signal = min(len(chunk.text) / 400.0, 1.0)
        score = 0.48 * coverage + 0.32 * base_norm + 0.10 * length_signal + section_bonus
        result.append((chunk, min(1.0, score)))
    result.sort(key=lambda row: (-row[1], row[0].chunk_id))
    return result


def _post_json(url: str, payload: dict[str, Any], headers: dict[str, str],
               timeout: float = 90.0, attempts: int = 3) -> tuple[dict[str, Any], float]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    last: Exception | None = None
    for attempt in range(attempts):
        request = urllib.request.Request(url, data=data, method="POST", headers={
            "Content-Type": "application/json", **headers,
        })
        started = time.perf_counter()
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = json.loads(response.read().decode("utf-8"))
            return body, (time.perf_counter() - started) * 1000
        except urllib.error.HTTPError as exc:
            try:
                detail = exc.read().decode("utf-8", errors="replace")[:1000]
            except Exception:
                detail = ""
            last = RuntimeError(f"HTTP {exc.code}: {detail}")
            if attempt + 1 < attempts:
                time.sleep(0.5 * (2 ** attempt) + random.random() * 0.1)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last = exc
            if attempt + 1 < attempts:
                time.sleep(0.5 * (2 ** attempt) + random.random() * 0.1)
    raise RuntimeError(f"POST {url} failed after {attempts} attempts: {last}")


class EmbeddingClient:
    def __init__(self, model: str, dimension: int, cache_dir: Path,
                 api_key: str | None = None, base_url: str | None = None):
        self.model = model
        self.dimension = dimension
        self.api_key = api_key or os.getenv("DASHSCOPE_API_KEY") or os.getenv("EMBEDDING_API_KEY") or ""
        self.base_url = (base_url or os.getenv("EMBEDDING_BASE_URL") or
                         "https://dashscope.aliyuncs.com/compatible-mode/v1").rstrip("/")
        self.cache_path = cache_dir / f"embedding_{re.sub(r'[^a-zA-Z0-9_.-]', '_', model)}_{dimension}.json"
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.cache: dict[str, list[float]] = {}
        if self.cache_path.exists():
            self.cache = json.loads(self.cache_path.read_text(encoding="utf-8"))
        self.stats = RemoteStats()

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if not self.api_key:
            raise RuntimeError("missing DASHSCOPE_API_KEY/EMBEDDING_API_KEY")
        result: list[list[float] | None] = [None] * len(texts)
        missing: list[tuple[int, str, str]] = []
        for index, text in enumerate(texts):
            key = stable_hash(text)
            cached = self.cache.get(key)
            if cached is not None:
                result[index] = cached
                self.stats.cache_hits += 1
            else:
                missing.append((index, key, text))

        # The OpenAI-compatible realtime endpoint accepts at most 10 texts per
        # request for both v3 and v4.  Larger offline throughput belongs to the
        # separate batch API, not this endpoint.
        batch_size = 10
        dirty = False
        for start in range(0, len(missing), batch_size):
            batch = missing[start:start + batch_size]
            payload = {"model": self.model, "input": [item[2] for item in batch], "dimensions": self.dimension}
            self.stats.calls += 1
            self.stats.input_chars += sum(len(item[2]) for item in batch)
            try:
                body, latency = _post_json(f"{self.base_url}/embeddings", payload,
                                           {"Authorization": f"Bearer {self.api_key}"})
                self.stats.latency_ms.append(latency)
                usage = body.get("usage") or {}
                self.stats.usage_tokens += int(usage.get("total_tokens") or usage.get("prompt_tokens") or 0)
                rows = sorted(body.get("data") or [], key=lambda row: int(row.get("index", 0)))
                if len(rows) != len(batch):
                    raise RuntimeError(f"embedding response count {len(rows)} != request {len(batch)}: {body}")
                for request_row, response_row in zip(batch, rows):
                    vector = response_row.get("embedding")
                    if not isinstance(vector, list) or len(vector) != self.dimension:
                        raise RuntimeError(f"{self.model} returned dimension {len(vector) if isinstance(vector, list) else 'invalid'}")
                    index, key, _ = request_row
                    self.cache[key] = vector
                    result[index] = vector
                    dirty = True
            except Exception:
                self.stats.failures += 1
                raise
        if dirty:
            temp = self.cache_path.with_suffix(".tmp")
            temp.write_text(json.dumps(self.cache, separators=(",", ":")), encoding="utf-8")
            temp.replace(self.cache_path)
        return [row for row in result if row is not None]


class DeepSeekClient:
    def __init__(self, cache_dir: Path):
        self.api_key = os.getenv("DEEPSEEK_API_KEY", "")
        self.base_url = os.getenv("DEEPSEEK_API_URL", "https://api.deepseek.com/v1").rstrip("/")
        if self.base_url.endswith("/chat/completions"):
            self.url = self.base_url
        else:
            self.url = f"{self.base_url}/chat/completions"
        self.model = os.getenv("DEEPSEEK_QUALITY_MODEL") or os.getenv("DEEPSEEK_MODEL") or "deepseek-chat"
        self.cache_path = cache_dir / "deepseek_rag_ops.json"
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.cache = json.loads(self.cache_path.read_text(encoding="utf-8")) if self.cache_path.exists() else {}
        self.stats = RemoteStats()

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    def _json_call(self, operation: str, system: str, user: str, max_tokens: int) -> dict[str, Any]:
        key = stable_hash(json.dumps([operation, self.model, system, user], ensure_ascii=False))
        if key in self.cache:
            self.stats.cache_hits += 1
            return self.cache[key]
        if not self.api_key:
            raise RuntimeError("missing DEEPSEEK_API_KEY")
        payload = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "response_format": {"type": "json_object"},
            "temperature": 0,
            "max_tokens": max_tokens,
        }
        self.stats.calls += 1
        self.stats.input_chars += len(system) + len(user)
        try:
            body, latency = _post_json(self.url, payload, {"Authorization": f"Bearer {self.api_key}"}, timeout=120)
            self.stats.latency_ms.append(latency)
            usage = body.get("usage") or {}
            self.stats.usage_tokens += int(usage.get("total_tokens") or 0)
            content = body["choices"][0]["message"]["content"]
            parsed = json.loads(content)
            self.cache[key] = parsed
            temp = self.cache_path.with_suffix(".tmp")
            temp.write_text(json.dumps(self.cache, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
            temp.replace(self.cache_path)
            return parsed
        except Exception:
            self.stats.failures += 1
            raise

    def rewrite(self, stage: str, query: str) -> str:
        system = ("你是企业招聘 RAG 查询改写器。输出 JSON，格式 {\"query\":\"...\"}。"
                  "保留原问题的约束、数字、否定词和岗位级别；只补充同义词，不得编造候选人事实。")
        user = f"stage={stage}\n原查询：{query}\n请生成一条适合混合检索的短查询。"
        return str(self._json_call("rewrite", system, user, 180).get("query") or query)

    def rerank(self, stage: str, query: str,
               ranked: Sequence[tuple[Chunk, float]]) -> list[tuple[Chunk, float]]:
        candidates = list(ranked[:20])
        system = ("你是 RAG 候选重排器。只根据查询与候选文本的证据相关性排序。"
                  "输出 JSON：{\"rankedIds\":[\"chunkId\"]}，不得加入不存在的 ID。")
        user = f"stage={stage}\nquery={query}\n" + "\n".join(
            f"ID={chunk.chunk_id}\n{chunk.text[:900]}" for chunk, _ in candidates)
        ids = self._json_call("rerank", system, user, 500).get("rankedIds") or []
        by_id = {chunk.chunk_id: (chunk, score) for chunk, score in candidates}
        ordered: list[tuple[Chunk, float]] = []
        for rank, cid in enumerate(ids):
            row = by_id.pop(str(cid), None)
            if row:
                ordered.append((row[0], 1.0 / (rank + 1)))
        offset = len(ordered)
        ordered.extend((chunk, 1.0 / (offset + rank + 1)) for rank, (chunk, _) in enumerate(by_id.values()))
        return ordered


class QwenRerankClient:
    def __init__(self, cache_dir: Path):
        self.api_key = os.getenv("DASHSCOPE_API_KEY") or os.getenv("EMBEDDING_API_KEY") or ""
        self.url = os.getenv(
            "DASHSCOPE_RERANK_URL",
            "https://dashscope.aliyuncs.com/api/v1/services/rerank/text-rerank/text-rerank",
        )
        self.model = os.getenv("DASHSCOPE_RERANK_MODEL", "qwen3-rerank")
        self.cache_path = cache_dir / f"rerank_{self.model}.json"
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.cache = json.loads(self.cache_path.read_text(encoding="utf-8")) if self.cache_path.exists() else {}
        self.stats = RemoteStats()

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    def rerank(self, query: str, ranked: Sequence[tuple[Chunk, float]], limit: int = 20) -> list[tuple[Chunk, float]]:
        candidates = list(ranked[:limit])
        texts = [chunk.text[:3000] for chunk, _ in candidates]
        key = stable_hash(json.dumps([self.model, query, texts], ensure_ascii=False))
        if key in self.cache:
            self.stats.cache_hits += 1
            results = self.cache[key]
        else:
            if not self.api_key:
                raise RuntimeError("missing DASHSCOPE_API_KEY")
            payload = {"model": self.model, "query": query, "documents": texts, "top_n": len(texts)}
            self.stats.calls += 1
            self.stats.input_chars += len(query) + sum(map(len, texts))
            try:
                body, latency = _post_json(self.url, payload, {"Authorization": f"Bearer {self.api_key}"}, timeout=120)
                self.stats.latency_ms.append(latency)
                usage = body.get("usage") or {}
                self.stats.usage_tokens += int(usage.get("total_tokens") or 0)
                results = body.get("results") or (body.get("output") or {}).get("results") or []
                if not results:
                    raise RuntimeError(f"empty qwen rerank response: {body}")
                self.cache[key] = results
                temp = self.cache_path.with_suffix(".tmp")
                temp.write_text(json.dumps(self.cache, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
                temp.replace(self.cache_path)
            except Exception:
                self.stats.failures += 1
                raise
        output = []
        for row in results:
            index = int(row["index"])
            if 0 <= index < len(candidates):
                output.append((candidates[index][0], float(row.get("relevance_score", row.get("score", 0.0)))))
        output.sort(key=lambda item: (-item[1], item[0].chunk_id))
        return output


def deterministic_rewrite(stage: str, query: str) -> str:
    expanded = alias_normalize(query)
    if stage == "jd_recall":
        sentences = [part.strip() for part in re.split(r"(?<=[。；;！？!?])|\n+", expanded) if part.strip()]
        tech_pattern = re.compile(
            r"java|python|golang|go\b|c\+\+|spring|redis|kafka|mysql|docker|"
            r"kubernetes|rag|llm|agent|pytorch|spark|flink|react|vue|ios|android|"
            r"安全|数据|算法|产品|前端|后端|运维|测试", re.I)
        ranked = []
        for index, sentence in enumerate(sentences):
            score = 0
            score += 5 * bool(tech_pattern.search(sentence))
            score += 4 * any(marker in sentence for marker in ("最近", "核心", "主责", "目标岗位"))
            score += 2 * any(marker in sentence for marker in ("项目", "技能", "经验", "负责", "成果"))
            score += 2 * bool(re.search(r"\d+\s*年", sentence))
            if score:
                ranked.append((score, index, sentence))
        selected = sorted(sorted(ranked, key=lambda row: (-row[0], row[1]))[:8], key=lambda row: row[1])
        concise = " ".join(sentence for _, _, sentence in selected)[:1000]
        return normalize_text(concise + " 岗位技能 经验 生产项目")
    stage_terms = {
        "resume_evidence": " 简历原文 证据 项目 结果",
        "knowledge_recall": " 评估规则 判定标准 处理要求",
    }
    return normalize_text(expanded + stage_terms.get(stage, ""))


def evaluate_ranking(relevances: Sequence[float], scores: Sequence[float],
                     hard_negative_flags: Sequence[bool] | None = None,
                     ks: Sequence[int] = (1, 3, 5, 10)) -> dict[str, float]:
    metrics: dict[str, float] = {}
    binary = [1 if value > 0 else 0 for value in relevances]
    total_relevant = max(1, sum(binary))
    for k in ks:
        top = binary[:k]
        metrics[f"recall@{k}"] = min(1.0, sum(top) / total_relevant)
        metrics[f"precision@{k}"] = sum(top) / max(1, min(k, len(binary)))
        gains = relevances[:k]
        dcg = sum((2 ** gain - 1) / math.log2(rank + 1) for rank, gain in enumerate(gains, start=1))
        ideal = sorted(relevances, reverse=True)[:k]
        idcg = sum((2 ** gain - 1) / math.log2(rank + 1) for rank, gain in enumerate(ideal, start=1))
        metrics[f"ndcg@{k}"] = dcg / idcg if idcg else 0.0
    rr = next((1.0 / rank for rank, value in enumerate(binary, start=1) if value), 0.0)
    metrics["mrr"] = rr
    hits = 0
    ap_sum = 0.0
    for rank, value in enumerate(binary, start=1):
        if value:
            hits += 1
            ap_sum += hits / rank
    metrics["map"] = ap_sum / total_relevant
    positive_scores = [score for score, rel in zip(scores, binary) if rel]
    negative_scores = [score for score, rel in zip(scores, binary) if not rel]
    metrics["scoreMargin"] = (max(positive_scores) if positive_scores else 0.0) - (max(negative_scores) if negative_scores else 0.0)
    metrics["zeroHit"] = 0.0 if any(binary[:10]) else 1.0
    if hard_negative_flags is not None:
        metrics["hardNegativeFp@5"] = 1.0 if any(hard_negative_flags[:5]) else 0.0
    return metrics


def aggregate_metrics(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {"queries": 0}
    numeric_keys = sorted({key for row in rows for key, value in row.items()
                           if isinstance(value, (int, float)) and not isinstance(value, bool)})
    result: dict[str, Any] = {"queries": len(rows)}
    for key in numeric_keys:
        values = [float(row[key]) for row in rows if isinstance(row.get(key), (int, float))]
        result[key] = round(statistics.mean(values), 5) if values else 0.0
    for timing in ("rewriteMs", "sparseMs", "denseMs", "fusionMs", "rerankMs", "totalMs"):
        values = [float(row[timing]) for row in rows if timing in row]
        if values:
            result[timing] = percentile_summary(values)
    return result


def chunk_statistics(chunks: Sequence[Chunk], document_count: int) -> dict[str, Any]:
    lengths = [len(chunk.text) for chunk in chunks]
    by_doc: defaultdict[str, list[Chunk]] = defaultdict(list)
    for chunk in chunks:
        by_doc[chunk.doc_id].append(chunk)
    duplicate_chars = 0
    total_chars = sum(lengths)
    for doc_chunks in by_doc.values():
        ordered = sorted(doc_chunks, key=lambda c: (c.char_start, c.char_end))
        for previous, current in zip(ordered, ordered[1:]):
            duplicate_chars += max(0, previous.char_end - current.char_start)
    heading_pattern = re.compile(
        r"(?m)^(?:#{1,6}\s+|岗位职责\s*[：:]|工作职责\s*[：:]|职位职责\s*[：:]|"
        r"任职要求\s*[：:]|职位要求\s*[：:]|必须技能\s*[：:]|必要技能\s*[：:]|"
        r"技能要求\s*[：:]|加分项\s*[：:]|经验要求\s*[：:]|生产场景(?:与考核题)?\s*[：:])")
    mixed_section = sum(
        1 for chunk in chunks
        if bool(chunk.metadata.get("mixedSection"))
        or len(heading_pattern.findall(chunk.text)) > 1
    )
    return {
        "documents": document_count,
        "chunks": len(chunks),
        "chunksPerDocument": round(len(chunks) / max(1, document_count), 3),
        "chars": percentile_summary(lengths),
        "tooSmallRate": round(sum(length_ < 80 for length_ in lengths) / max(1, len(lengths)), 5),
        "overlapDuplicationRatio": round(duplicate_chars / max(1, total_chars), 5),
        "mixedSectionRate": round(mixed_section / max(1, len(chunks)), 5),
        "textTitlePrefixRate": round(sum(chunk.text.startswith("文档：") for chunk in chunks) / max(1, len(chunks)), 5),
        "sectionSignalCoverageRate": round(sum(chunk.section != chunk.title for chunk in chunks) / max(1, len(chunks)), 5),
        "estimatedVectorBytes": None,
    }


def dump_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)


def chunk_to_dict(chunk: Chunk) -> dict[str, Any]:
    return asdict(chunk)
