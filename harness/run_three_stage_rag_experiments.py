#!/usr/bin/env python3
"""Run phased, labeled RAG ablations for all three production retrieval stages.

This is a retrieval-only benchmark: it never invokes the full resume workflow.
Each phase changes one decision after loading the previous phase's winner.

Example on ECS::

    python3 harness/run_three_stage_rag_experiments.py \
      --phase all --stages all --out reports/rag_three_stage_20260803
"""
from __future__ import annotations

import argparse
import dataclasses
import hashlib
import itertools
import json
import math
import os
import platform
import random
import statistics
import subprocess
import sys
import time
from collections import defaultdict
from dataclasses import asdict
from pathlib import Path
from typing import Any, Sequence

from rag_three_stage_lib import (
    BM25Index,
    Chunk,
    DeepSeekClient,
    EmbeddingClient,
    KnowledgeCurrentIndex,
    QwenRerankClient,
    ResumeCurrentIndex,
    aggregate_metrics,
    chunk_document,
    chunk_statistics,
    dense_search,
    deterministic_rewrite,
    dump_json,
    expand_kb_terms,
    feature_rerank,
    phrase_tokens,
    percentile_summary,
    rrf_fuse,
    tokenize,
)

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "testdata" / "rag_three_stage"
STAGES = ("jd_recall", "resume_evidence", "knowledge_recall")

DEFAULTS: dict[str, dict[str, Any]] = {
    "jd_recall": {
        "chunk": {"strategy": "recursive", "size": 400, "overlap": 80},
        "embedding": {"model": "text-embedding-v3", "dimension": 1024},
        "tokenizer": "cjk_bigram",
        "retrieval": {"mode": "hybrid", "semanticWeight": 0.7, "rrfK": 60,
                      "scoreThreshold": 0.35, "candidateLimit": 50, "denseMultiplier": 3},
        "rewrite": "none", "rerank": "none",
    },
    "resume_evidence": {
        "chunk": {"strategy": "production_resume", "size": 600, "overlap": 0},
        "embedding": {"model": "none", "dimension": 0},
        "tokenizer": "resume_current_phrase",
        "retrieval": {"mode": "resume_production", "semanticWeight": 0.0, "rrfK": 60,
                      "scoreThreshold": 0.35, "candidateLimit": 5, "denseMultiplier": 1},
        "rewrite": "none", "rerank": "feature",
    },
    "knowledge_recall": {
        "chunk": {"strategy": "production_kb", "size": 320, "overlap": 60},
        "embedding": {"model": "text-embedding-v3", "dimension": 1024},
        "tokenizer": "kb_current_weighted",
        "retrieval": {"mode": "hybrid", "semanticWeight": 0.5, "rrfK": 60,
                      "scoreThreshold": 0.30, "candidateLimit": 20, "denseMultiplier": 1},
        "rewrite": "none", "rerank": "feature",
    },
}

CHUNK_VARIANTS_BY_STAGE: dict[str, list[dict[str, Any]]] = {
    "jd_recall": [
        {"name": "whole_document", "strategy": "whole", "size": 0, "overlap": 0},
        {"name": "fixed_char_320_60", "strategy": "fixed", "size": 320, "overlap": 60},
        {"name": "prod_recursive_400_80", "strategy": "recursive", "size": 400, "overlap": 80},
        {"name": "zh_boundary_256_0", "strategy": "recursive", "size": 256, "overlap": 0},
        {"name": "zh_boundary_320_40", "strategy": "recursive", "size": 320, "overlap": 40},
        {"name": "zh_boundary_512_80", "strategy": "recursive", "size": 512, "overlap": 80},
        {"name": "plain_label_320_0", "strategy": "section", "size": 320, "overlap": 0},
        {"name": "plain_label_title_400_40", "strategy": "section_prefix", "size": 400, "overlap": 40},
        {"name": "semantic_400_p75_s1", "strategy": "semantic", "size": 400, "overlap": 0,
         "minSize": 120, "maxSize": 650, "breakpointPercentile": 75, "overlapSentences": 1,
         "semanticModel": "text-embedding-v3", "semanticDimension": 512},
        {"name": "label_semantic_500_p75_s1", "strategy": "section_semantic", "size": 500, "overlap": 0,
         "minSize": 120, "maxSize": 750, "breakpointPercentile": 75, "overlapSentences": 1,
         "semanticModel": "text-embedding-v3", "semanticDimension": 512},
        {"name": "label_semantic_title_500_p75_s1", "strategy": "section_semantic_prefix", "size": 500, "overlap": 0,
         "minSize": 120, "maxSize": 750, "breakpointPercentile": 75, "overlapSentences": 1,
         "semanticModel": "text-embedding-v3", "semanticDimension": 512},
    ],
    "resume_evidence": [
        {"name": "prod_blankline_else_line_600", "strategy": "production_resume", "size": 600, "overlap": 0},
        {"name": "whole_resume", "strategy": "whole", "size": 0, "overlap": 0},
        {"name": "fixed_char_320_40", "strategy": "fixed", "size": 320, "overlap": 40},
        {"name": "zh_boundary_256_0", "strategy": "recursive", "size": 256, "overlap": 0},
        {"name": "zh_boundary_320_40", "strategy": "recursive", "size": 320, "overlap": 40},
        {"name": "zh_boundary_500_100", "strategy": "recursive", "size": 500, "overlap": 100},
        {"name": "plain_resume_label_320_0", "strategy": "section", "size": 320, "overlap": 0},
        {"name": "plain_resume_label_title_400_40", "strategy": "section_prefix", "size": 400, "overlap": 40},
        {"name": "semantic_320_p75_s1", "strategy": "semantic", "size": 320, "overlap": 0,
         "minSize": 100, "maxSize": 520, "breakpointPercentile": 75, "overlapSentences": 1,
         "semanticModel": "text-embedding-v3", "semanticDimension": 512},
        {"name": "label_semantic_title_400_p75_s0", "strategy": "section_semantic_prefix", "size": 400, "overlap": 0,
         "minSize": 100, "maxSize": 620, "breakpointPercentile": 75, "overlapSentences": 0,
         "semanticModel": "text-embedding-v3", "semanticDimension": 512},
    ],
    "knowledge_recall": [
        {"name": "prod_kb_320_60", "strategy": "production_kb", "size": 320, "overlap": 60},
        {"name": "prod_kb_256_0", "strategy": "production_kb", "size": 256, "overlap": 0},
        {"name": "prod_kb_400_40", "strategy": "production_kb", "size": 400, "overlap": 40},
        {"name": "prod_kb_512_80", "strategy": "production_kb", "size": 512, "overlap": 80},
        {"name": "whole_document", "strategy": "whole", "size": 0, "overlap": 0},
        {"name": "zh_boundary_320_40", "strategy": "recursive", "size": 320, "overlap": 40},
        {"name": "markdown_heading_320_0", "strategy": "section", "size": 320, "overlap": 0},
        {"name": "markdown_heading_title_400_40", "strategy": "section_prefix", "size": 400, "overlap": 40},
        {"name": "heading_semantic_320_p75_s1", "strategy": "section_semantic", "size": 320, "overlap": 0,
         "minSize": 100, "maxSize": 520, "breakpointPercentile": 75, "overlapSentences": 1,
         "semanticModel": "text-embedding-v3", "semanticDimension": 512},
        {"name": "heading_semantic_title_400_p75_s0", "strategy": "section_semantic_prefix", "size": 400, "overlap": 0,
         "minSize": 100, "maxSize": 650, "breakpointPercentile": 75, "overlapSentences": 0,
         "semanticModel": "text-embedding-v3", "semanticDimension": 512},
    ],
}

EMBEDDING_VARIANTS = [
    {"name": "te3_256", "model": "text-embedding-v3", "dimension": 256},
    {"name": "te3_512", "model": "text-embedding-v3", "dimension": 512},
    {"name": "te3_768", "model": "text-embedding-v3", "dimension": 768},
    {"name": "te3_1024_current", "model": "text-embedding-v3", "dimension": 1024},
    {"name": "te4_512", "model": "text-embedding-v4", "dimension": 512},
    {"name": "te4_1024", "model": "text-embedding-v4", "dimension": 1024},
]

EMBEDDING_VARIANTS_BY_STAGE = {
    "jd_recall": EMBEDDING_VARIANTS,
    "knowledge_recall": EMBEDDING_VARIANTS,
    "resume_evidence": [
        {"name": "current_no_dense_NA", "model": "none", "dimension": 0,
         "retrievalMode": "resume_production"},
        *[{**variant, "retrievalMode": "dense"} for variant in EMBEDDING_VARIANTS],
    ],
}

TOKENIZER_VARIANTS_BY_STAGE = {
    "jd_recall": [
        {"name": "current_english_cjk_bigram", "tokenizer": "cjk_bigram"},
        {"name": "unigram_plus_bigram", "tokenizer": "unigram_bigram"},
        {"name": "jieba", "tokenizer": "jieba"},
        {"name": "domain_maxmatch_alias", "tokenizer": "domain"},
    ],
    "resume_evidence": [
        {"name": "current_phrase_contains", "tokenizer": "resume_current_phrase"},
        {"name": "english_cjk_bigram", "tokenizer": "cjk_bigram"},
        {"name": "jieba", "tokenizer": "jieba"},
        {"name": "domain_maxmatch_alias", "tokenizer": "domain"},
    ],
    "knowledge_recall": [
        {"name": "current_weighted_contains_not_bm25", "tokenizer": "kb_current_weighted"},
        {"name": "true_bm25_cjk_bigram", "tokenizer": "cjk_bigram"},
        {"name": "true_bm25_jieba", "tokenizer": "jieba"},
        {"name": "true_bm25_domain_alias", "tokenizer": "domain"},
    ],
}

def jd_kb_retrieval_variants(default_threshold: float,
                             default_limit: int) -> list[dict[str, Any]]:
    return [
        {"name": "lexical_only", "mode": "lexical", "semanticWeight": 0.0, "rrfK": 60},
        {"name": "dense_only", "mode": "dense", "semanticWeight": 1.0, "rrfK": 60},
        *[
            {"name": f"hybrid_sw{weight}_k{rrf_k}", "mode": "hybrid",
             "semanticWeight": weight, "rrfK": rrf_k}
            for rrf_k in (10, 30, 60, 100)
            for weight in (0.3, 0.5, 0.7)
        ],
        {"name": "hybrid_threshold_0.20", "mode": "hybrid", "semanticWeight": 0.5,
         "rrfK": 60, "scoreThreshold": 0.20},
        {"name": "hybrid_threshold_0.45", "mode": "hybrid", "semanticWeight": 0.5,
         "rrfK": 60, "scoreThreshold": 0.45},
        {"name": "hybrid_candidates_half", "mode": "hybrid", "semanticWeight": 0.5,
         "rrfK": 60, "scoreThreshold": default_threshold,
         "candidateLimit": max(5, default_limit // 2)},
        {"name": "hybrid_candidates_double", "mode": "hybrid", "semanticWeight": 0.5,
         "rrfK": 60, "scoreThreshold": default_threshold,
         "candidateLimit": default_limit * 2},
    ]

RETRIEVAL_VARIANTS_BY_STAGE = {
    "jd_recall": jd_kb_retrieval_variants(0.35, 50),
    "knowledge_recall": jd_kb_retrieval_variants(0.30, 20),
    "resume_evidence": [
        {"name": "current_section_phrase_rrf", "mode": "resume_production", "semanticWeight": 0.0, "rrfK": 60},
        {"name": "lexical_only", "mode": "lexical", "semanticWeight": 0.0, "rrfK": 60},
        {"name": "scoped_dense_only", "mode": "dense", "semanticWeight": 1.0, "rrfK": 60},
        {"name": "scoped_hybrid_sw0.3", "mode": "hybrid", "semanticWeight": 0.3, "rrfK": 60},
        {"name": "scoped_hybrid_sw0.5", "mode": "hybrid", "semanticWeight": 0.5, "rrfK": 60},
        {"name": "scoped_hybrid_sw0.7", "mode": "hybrid", "semanticWeight": 0.7, "rrfK": 60},
        {"name": "scoped_hybrid_threshold_0.20", "mode": "hybrid", "semanticWeight": 0.5,
         "rrfK": 60, "scoreThreshold": 0.20},
        {"name": "scoped_hybrid_threshold_0.45", "mode": "hybrid", "semanticWeight": 0.5,
         "rrfK": 60, "scoreThreshold": 0.45},
        {"name": "scoped_hybrid_candidates_10", "mode": "hybrid", "semanticWeight": 0.5,
         "rrfK": 60, "candidateLimit": 10},
    ],
}

REWRITE_VARIANTS = [
    {"name": "no_rewrite", "rewrite": "none"},
    {"name": "deterministic_alias_rewrite", "rewrite": "deterministic"},
    {"name": "deepseek_rewrite", "rewrite": "deepseek"},
]

RERANK_VARIANTS = [
    {"name": "no_rerank", "rerank": "none"},
    {"name": "feature_rerank_current", "rerank": "feature"},
    {"name": "qwen3_rerank", "rerank": "qwen3"},
    {"name": "deepseek_listwise", "rerank": "deepseek"},
]

RERANK_VARIANTS_BY_STAGE = {
    "jd_recall": [
        {"name": "no_rerank_current", "rerank": "none"},
        {"name": "qwen3_rerank", "rerank": "qwen3"},
        {"name": "deepseek_listwise_current_option", "rerank": "deepseek"},
    ],
    "resume_evidence": RERANK_VARIANTS,
    "knowledge_recall": RERANK_VARIANTS,
}


def load_json(name: str) -> Any:
    return json.loads((DATA / name).read_text(encoding="utf-8"))


def gold_index(stage: str) -> dict[str, dict[str, Any]]:
    manifest = load_json("rag_gold_spans.json")
    return {row["caseId"]: row for row in manifest["cases"][stage]}


def with_gold(stage: str, query: dict[str, Any], index: dict[str, dict[str, Any]]) -> dict[str, Any]:
    gold = index.get(query["caseId"])
    if gold is None:
        raise ValueError(f"missing frozen gold span for {stage}/{query['caseId']}")
    return {
        **query,
        "goldDocIds": list(gold["goldDocIds"]),
        "goldEvidence": list(gold["goldEvidence"]),
        "benchmarkSplit": gold["benchmarkSplit"],
        "goldAnnotationCohort": (
            "weak_duty_fallback" if any(
                span.get("annotationMethod") == "deterministic_duty_lead_fallback_v1"
                for span in gold["goldEvidence"])
            else "primary"
        ),
    }


def git_revision() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, text=True).strip()
    except Exception:
        return "unknown"


def environment_manifest() -> dict[str, Any]:
    return {
        "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "host": platform.node(),
        "platform": platform.platform(),
        "python": sys.version,
        "gitRevision": git_revision(),
        "embeddingBaseUrl": os.getenv("EMBEDDING_BASE_URL", "default-dashscope"),
        "embeddingKeyPresent": bool(os.getenv("DASHSCOPE_API_KEY") or os.getenv("EMBEDDING_API_KEY")),
        "deepseekKeyPresent": bool(os.getenv("DEEPSEEK_API_KEY")),
        "rerankUrl": os.getenv("DASHSCOPE_RERANK_URL", "default-dashscope"),
    }


def render_resume_case(case: dict[str, Any], case_index: int) -> tuple[str, list[tuple[int, int, str]], str]:
    """Render section labels into realistic PDF-extraction layouts."""
    cohort = ("blank_paragraphs", "line_only", "compressed_one_line", "ocr_noisy")[case_index % 4]
    pieces: list[str] = []
    ranges: list[tuple[int, int, str]] = []
    cursor = 0
    for index, section in enumerate(case["sections"]):
        title = str(section["title"])
        content = str(section["content"])
        if cohort == "ocr_noisy":
            # Deterministic OCR-like degradation: spaced headings, ASCII
            # punctuation and hard line wraps. Gold ranges follow the degraded
            # text, so evaluation remains span-accurate.
            noisy_title = " ".join(title)
            content = content.replace("，", ",").replace("。", ".").replace("：", ":")
            content = "\n".join(content[start:start + 72]
                                for start in range(0, len(content), 72))
            prefix, suffix = f"{noisy_title} :\n", "\n"
        elif cohort == "blank_paragraphs":
            prefix, suffix = f"{title}：\n", "\n\n"
        elif cohort == "line_only":
            prefix, suffix = f"{title}：", "\n"
        else:
            prefix, suffix = f"{title}：", "；"
        pieces.append(prefix)
        cursor += len(prefix)
        start = cursor
        pieces.append(content)
        cursor += len(content)
        ranges.append((start, cursor, str(section["sectionId"])))
        if index + 1 < len(case["sections"]):
            pieces.append(suffix)
            cursor += len(suffix)
    return "".join(pieces), ranges, cohort


def attach_resume_sections(chunks: Sequence[Chunk], ranges: Sequence[tuple[int, int, str]]) -> list[Chunk]:
    output = []
    for chunk in chunks:
        best_id = None
        best_overlap = 0
        overlap_chars: dict[str, int] = {}
        for start, end, section_id in ranges:
            overlap = max(0, min(chunk.char_end, end) - max(chunk.char_start, start))
            if overlap > 0:
                overlap_chars[section_id] = overlap_chars.get(section_id, 0) + overlap
            if overlap > best_overlap:
                best_overlap = overlap
                best_id = section_id
        chunk_chars = max(1, chunk.char_end - chunk.char_start)
        overlap_ratios = {
            section_id: round(chars / chunk_chars, 6)
            for section_id, chars in overlap_chars.items()
        }
        output.append(dataclasses.replace(
            chunk, metadata={
                **chunk.metadata,
                "sectionId": best_id or "unknown",
                "sectionOverlapRatios": overlap_ratios,
                "sectionPurity": max(overlap_ratios.values(), default=0.0),
                "mixedSection": len(overlap_ratios) > 1,
            }))
    return output


def attach_kb_parent_sections(chunks: Sequence[Chunk], content: str,
                              tags: Sequence[Any]) -> list[Chunk]:
    import re
    headings = [(match.start(), match.group(1).strip())
                for match in re.finditer(r"(?m)^#{1,6}\s+(.+)$", content)]
    output = []
    for chunk in chunks:
        parent = next((heading for offset, heading in reversed(headings)
                       if offset <= chunk.char_start), "")
        output.append(dataclasses.replace(chunk, metadata={
            **chunk.metadata, "tags": list(tags), "parentSection": parent,
        }))
    return output


def stage_corpus(stage: str, chunk_cfg: dict[str, Any],
                 semantic_embed: Any | None = None
                 ) -> tuple[list[Chunk], list[dict[str, Any]], dict[str, list[str]]]:
    chunks: list[Chunk] = []
    queries: list[dict[str, Any]] = []
    scope: defaultdict[str, list[str]] = defaultdict(list)
    strategy = chunk_cfg["strategy"]
    size = int(chunk_cfg["size"])
    overlap = int(chunk_cfg["overlap"])
    frozen_gold = gold_index(stage)

    if stage == "jd_recall":
        docs = load_json("jd_catalog.json")
        queries = load_json("jd_queries.json")
        for doc in docs:
            # Production always prepends title/category before splitting.
            # Match JdRagService.reindexVectors exactly.
            full_text = f"岗位: {doc['title']}\n类别: {doc['category']}\n{doc['description']}"
            doc_chunks = chunk_document(doc["jdId"], doc["title"], full_text,
                                        strategy, size, overlap,
                                        semantic_options=chunk_cfg,
                                        semantic_embed=semantic_embed)
            chunks.extend(doc_chunks)
            scope["global"].extend(chunk.chunk_id for chunk in doc_chunks)
        queries = [{**with_gold(stage, query, frozen_gold), "scope": "global"}
                   for query in queries]
    elif stage == "resume_evidence":
        cases = load_json("resume_evidence_cases.json")
        for case_index, case in enumerate(cases):
            text, ranges, layout_cohort = render_resume_case(case, case_index)
            doc_chunks = chunk_document(case["resumeId"], case["resumeId"], text,
                                        strategy, size, overlap,
                                        semantic_options=chunk_cfg,
                                        semantic_embed=semantic_embed)
            doc_chunks = attach_resume_sections(doc_chunks, ranges)
            chunks.extend(doc_chunks)
            scope[case["resumeId"]].extend(chunk.chunk_id for chunk in doc_chunks)
            for query in case["queries"]:
                queries.append({**with_gold(stage, query, frozen_gold),
                                "scope": case["resumeId"], "resumeId": case["resumeId"],
                                "formatCohort": layout_cohort})
    elif stage == "knowledge_recall":
        live_path = DATA / "knowledge_documents_live.json"
        docs = json.loads(live_path.read_text(encoding="utf-8")) if live_path.exists() else load_json("knowledge_documents.json")
        queries = load_json("knowledge_queries.json")
        title_to_id = {doc["title"]: doc["docId"] for doc in docs}
        for query in queries:
            live_ids = [title_to_id[title] for title in query.get("goldTitles") or [] if title in title_to_id]
            if live_ids:
                query["goldDocIds"] = live_ids
        for doc in docs:
            doc_chunks = chunk_document(doc["docId"], doc["title"], doc["content"],
                                        strategy, size, overlap,
                                        semantic_options=chunk_cfg,
                                        semantic_embed=semantic_embed)
            tags = doc.get("tags") or []
            doc_chunks = attach_kb_parent_sections(doc_chunks, doc["content"], tags)
            chunks.extend(doc_chunks)
            scope["global"].extend(chunk.chunk_id for chunk in doc_chunks)
        queries = [{**with_gold(stage, query, frozen_gold), "scope": "global"}
                   for query in queries]
    else:
        raise ValueError(stage)
    return chunks, queries, dict(scope)


def rewrite_queries(stage: str, queries: Sequence[dict[str, Any]], mode: str,
                    deepseek: DeepSeekClient) -> tuple[list[str], list[float], list[str]]:
    rewritten: list[str] = []
    latencies: list[float] = []
    errors: list[str] = []
    for query in queries:
        original = query["query"]
        started = time.perf_counter()
        try:
            if mode == "none":
                value = original
            elif mode == "deterministic":
                value = deterministic_rewrite(stage, original)
            elif mode == "deepseek":
                value = deepseek.rewrite(stage, original)
            else:
                raise ValueError(mode)
        except Exception as exc:
            errors.append(f"{query['caseId']}: {exc}")
            value = original
        rewritten.append(value)
        latencies.append((time.perf_counter() - started) * 1000)
    return rewritten, latencies, errors


def exact_and_graded(stage: str, query: dict[str, Any], chunk: Chunk) -> tuple[bool, float, bool]:
    if stage == "jd_recall":
        graded = float(query.get("relevance", {}).get(chunk.doc_id, 0))
        exact = chunk.doc_id == query["goldId"]
        hard_negative = chunk.doc_id in set(query.get("hardNegativeIds") or [])
        return exact, graded, hard_negative
    if stage in {"resume_evidence", "knowledge_recall"}:
        overlap = evidence_overlap(query, chunk)
        doc_match = chunk.doc_id in set(query["goldDocIds"])
        graded = (3.0 if overlap["exact"] else
                  2.0 if overlap["f1"] >= 0.25 else
                  1.0 if doc_match else 0.0)
        return bool(overlap["exact"]), graded, False
    raise ValueError(stage)


def merge_intervals(intervals: Sequence[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[list[int]] = []
    for start, end in sorted(intervals):
        if end <= start:
            continue
        if not merged or start > merged[-1][1]:
            merged.append([start, end])
        else:
            merged[-1][1] = max(merged[-1][1], end)
    return [(start, end) for start, end in merged]


def evidence_overlap(query: dict[str, Any], chunk: Chunk) -> dict[str, float | bool]:
    spans = [span for span in query.get("goldEvidence") or []
             if span["docId"] == chunk.doc_id]
    # Purity is measured against what the retriever actually returns, including
    # any document/section prefix injected into the chunk text.
    chunk_length = max(1, len(chunk.text))
    intersections = []
    best_coverage = 0.0
    for span in spans:
        start = max(chunk.char_start, int(span["start"]))
        end = min(chunk.char_end, int(span["end"]))
        intersection = max(0, end - start)
        if intersection:
            intersections.append((start, end))
        span_length = max(1, int(span["end"]) - int(span["start"]))
        best_coverage = max(best_coverage, intersection / span_length)
    relevant_chars = sum(end - start for start, end in merge_intervals(intersections))
    purity = relevant_chars / chunk_length
    f1 = (2 * best_coverage * purity / (best_coverage + purity)
          if best_coverage + purity else 0.0)
    return {
        "coverage": best_coverage,
        "purity": purity,
        "f1": f1,
        "relevantChars": float(relevant_chars),
        "exact": best_coverage >= 0.50 and purity >= 0.35,
    }


def evidence_set_metrics(query: dict[str, Any], chunks: Sequence[Chunk]) -> dict[str, float]:
    spans = query.get("goldEvidence") or []
    total_gold = sum(max(0, int(span["end"]) - int(span["start"])) for span in spans)
    covered = 0
    for span in spans:
        intervals = []
        for chunk in chunks:
            if chunk.doc_id != span["docId"]:
                continue
            start = max(chunk.char_start, int(span["start"]))
            end = min(chunk.char_end, int(span["end"]))
            if end > start:
                intervals.append((start, end))
        covered += sum(end - start for start, end in merge_intervals(intervals))
    context_chars = sum(len(chunk.text) for chunk in chunks)
    relevant_context = sum(float(evidence_overlap(query, chunk)["relevantChars"])
                           for chunk in chunks)
    recall = covered / max(1, total_gold)
    precision = relevant_context / max(1, context_chars)
    f1 = 2 * recall * precision / (recall + precision) if recall + precision else 0.0
    return {
        "recall": min(1.0, recall),
        "precision": min(1.0, precision),
        "f1": min(1.0, f1),
        "contextChars": float(context_chars),
    }


def matched_gold_span_indexes(query: dict[str, Any], chunk: Chunk) -> set[int]:
    overlap = evidence_overlap(query, chunk)
    if float(overlap["purity"]) < 0.35:
        return set()
    matched = set()
    for index, span in enumerate(query.get("goldEvidence") or []):
        if span["docId"] != chunk.doc_id:
            continue
        intersection = max(
            0,
            min(chunk.char_end, int(span["end"]))
            - max(chunk.char_start, int(span["start"])),
        )
        span_length = max(1, int(span["end"]) - int(span["start"]))
        if intersection / span_length >= 0.50:
            matched.add(index)
    return matched


def dedupe_docs(ranked: Sequence[tuple[Chunk, float]], limit: int) -> list[tuple[Chunk, float]]:
    seen: set[str] = set()
    rows: list[tuple[Chunk, float]] = []
    for chunk, score in ranked:
        if chunk.doc_id in seen:
            continue
        seen.add(chunk.doc_id)
        rows.append((chunk, score))
        if len(rows) >= limit:
            break
    return rows


def rrf_fuse_documents(dense: Sequence[tuple[Chunk, float]],
                       sparse: Sequence[tuple[Chunk, float]],
                       semantic_weight: float, rrf_k: int,
                       limit: int) -> list[tuple[Chunk, float]]:
    """Mirror production JD fusion: dedupe vector chunks to JD before RRF."""
    dense_docs = dedupe_docs(dense, 200)
    sparse_docs = dedupe_docs(sparse, 200)
    scores: defaultdict[str, float] = defaultdict(float)
    representative: dict[str, Chunk] = {}
    for rank, (chunk, _) in enumerate(dense_docs, start=1):
        scores[chunk.doc_id] += semantic_weight / (rrf_k + rank)
        representative.setdefault(chunk.doc_id, chunk)
    for rank, (chunk, _) in enumerate(sparse_docs, start=1):
        scores[chunk.doc_id] += (1.0 - semantic_weight) / (rrf_k + rank)
        # The whole-document lexical row gives a remote reranker complete JD
        # context while the vector rank still reflects the tested chunks.
        representative[chunk.doc_id] = chunk
    rows = [(representative[doc_id], score) for doc_id, score in scores.items()]
    rows.sort(key=lambda item: (-item[1], item[0].doc_id))
    return rows[:limit]


def build_sparse_index(stage: str, chunks: Sequence[Chunk], tokenizer_mode: str) -> Any:
    if stage == "resume_evidence" and tokenizer_mode == "resume_current_phrase":
        return ResumeCurrentIndex(chunks)
    if stage == "knowledge_recall" and tokenizer_mode == "kb_current_weighted":
        return KnowledgeCurrentIndex(chunks)
    return BM25Index(chunks, tokenizer_mode)


def resume_structural_search(query: str, chunks: Sequence[Chunk],
                             tokenizer_mode: str, limit: int) -> list[tuple[Chunk, float]]:
    terms = (phrase_tokens(query) if tokenizer_mode == "resume_current_phrase"
             else list(dict.fromkeys(tokenize(query, tokenizer_mode)))[:30])
    lower_query = (query or "").lower()
    project_intent = "项目" in lower_query or "project" in lower_query
    rows = []
    for chunk in chunks:
        lower = chunk.text.lower()
        score = sum(2.0 for term in terms if term.lower() in lower)
        if project_intent and ("项目" in lower or "project" in lower):
            score += 6.0
        if (("技术" in lower_query or "技能" in lower_query)
                and any(marker in lower for marker in ("技能", "熟练", "掌握", "技术栈"))):
            score += 3.0
        if (("工作" in lower_query or "经验" in lower_query)
                and ("工作经历" in lower or "经验" in lower)):
            score += 2.0
        if score > 0:
            score += min(len(chunk.text), 600) / 1200.0
            rows.append((chunk, score))
    rows.sort(key=lambda item: (-item[1], item[0].chunk_id))
    return rows[:limit]


def resume_controller_rerank(query: str, ranked: Sequence[tuple[Chunk, float]]) -> list[tuple[Chunk, float]]:
    import re
    terms = list(dict.fromkeys(
        term.strip().lower() for term in re.split(r"[\s,，、/|；;:：()（）]+", query or "")
        if len(term.strip()) >= 2))
    project_intent = "项目" in (query or "").lower() or "project" in (query or "").lower()
    rows = []
    for chunk, _ in ranked:
        lower = chunk.text.lower()
        matched = sum(term in lower for term in terms)
        density = matched / max(1, len(terms)) if terms else 0.5
        length_signal = min(len(chunk.text) / 300.0, 1.0)
        section_signal = 0.35 if project_intent and ("项目" in lower or "project" in lower) else 0.0
        score = min(1.0, 0.65 * density + 0.20 * length_signal + section_signal)
        rows.append((chunk, score))
    rows.sort(key=lambda item: (-item[1], item[0].chunk_id))
    return rows


def kb_feature_rerank(query: str, ranked: Sequence[tuple[Chunk, float]],
                      sparse: Sequence[tuple[Chunk, float]],
                      dense: Sequence[tuple[Chunk, float]]) -> list[tuple[Chunk, float]]:
    terms = expand_kb_terms(phrase_tokens(query))
    sparse_by_id = {chunk.chunk_id: score for chunk, score in sparse}
    dense_by_id = {chunk.chunk_id: score for chunk, score in dense}
    raw = [score for _, score in ranked]
    low, high = min(raw, default=0.0), max(raw, default=1.0)
    rows = []
    for chunk, base in ranked:
        searchable = " ".join((chunk.title, chunk.section, chunk.text,
                               " ".join(map(str, chunk.metadata.get("tags") or [])))).lower()
        coverage = sum(term.lower() in searchable for term in terms) / max(1, len(terms))
        retrieval = (base - low) / max(1e-9, high - low)
        dense_score = max(0.0, min(1.0, dense_by_id.get(chunk.chunk_id, 0.0)))
        lexical_raw = sparse_by_id.get(chunk.chunk_id, 0.0)
        lexical = max(0.0, min(1.0, lexical_raw))
        score = max(0.0, min(1.0,
            0.45 * retrieval + 0.30 * coverage + 0.15 * dense_score + 0.10 * lexical))
        rows.append((chunk, score))
    rows.sort(key=lambda item: (-item[1], item[0].chunk_id))
    return rows


def query_metrics(stage: str, query: dict[str, Any], ranked: Sequence[tuple[Chunk, float]],
                  raw_ranked: Sequence[tuple[Chunk, float]]) -> dict[str, Any]:
    exact = []
    graded = []
    hard_negative = []
    scores = []
    seen_gold_spans: set[int] = set()
    for chunk, score in ranked:
        if stage == "jd_recall":
            hit, gain, hard = exact_and_graded(stage, query, chunk)
        else:
            matched = matched_gold_span_indexes(query, chunk)
            newly_matched = matched - seen_gold_spans
            hit = bool(newly_matched)
            gain = 3.0 if newly_matched else 0.0
            hard = False
            seen_gold_spans.update(matched)
        exact.append(1 if hit else 0)
        graded.append(gain)
        hard_negative.append(hard)
        scores.append(float(score))

    row: dict[str, Any] = {}
    if stage == "jd_recall":
        ideal_reference = sorted(
            (float(value) for value in (query.get("relevance") or {}).values()),
            reverse=True)
    else:
        ideal_reference = [3.0] * max(1, len(query.get("goldEvidence") or []))
    for k in (1, 3, 5, 10):
        top = exact[:k]
        row[f"recall@{k}"] = 1.0 if any(top) else 0.0
        row[f"precision@{k}"] = sum(top) / max(1, min(k, len(exact)))
        gains = graded[:k]
        dcg = sum((2 ** gain - 1) / math.log2(rank + 1)
                  for rank, gain in enumerate(gains, start=1))
        ideal = ideal_reference[:k]
        idcg = sum((2 ** gain - 1) / math.log2(rank + 1)
                   for rank, gain in enumerate(ideal, start=1))
        row[f"ndcg@{k}"] = dcg / idcg if idcg else 0.0
    first = next((rank for rank, value in enumerate(exact, start=1) if value), None)
    row["mrr"] = 1.0 / first if first else 0.0
    # JD has one exact target document. Evidence stages may have multiple gold
    # spans, so span recall/F1 below is the authoritative coverage metric.
    row["map"] = row["mrr"] if stage == "jd_recall" else 0.0
    positive_scores = [score for score, hit in zip(scores, exact) if hit]
    negative_scores = [score for score, hit in zip(scores, exact) if not hit]
    row["scoreMargin"] = (max(positive_scores) if positive_scores else 0.0) - (max(negative_scores) if negative_scores else 0.0)
    row["zeroHit"] = 0.0 if any(exact[:10]) else 1.0
    row["hardNegativeFp@5"] = 1.0 if any(hard_negative[:5]) else 0.0

    for k in (1, 3, 5, 10):
        evidence = evidence_set_metrics(query, [chunk for chunk, _ in ranked[:k]])
        row[f"contextChars@{k}"] = evidence["contextChars"]
        row[f"evidenceRecall@{k}"] = evidence["recall"]
        row[f"evidenceContextPrecision@{k}"] = evidence["precision"]
        row[f"evidenceF1@{k}"] = evidence["f1"]

    if stage == "knowledge_recall":
        gold_docs = set(query["goldDocIds"])
        for k in (1, 3, 5, 10):
            returned_gold = {chunk.doc_id for chunk, _ in ranked[:k] if chunk.doc_id in gold_docs}
            row[f"documentRecall@{k}"] = len(returned_gold) / max(1, len(gold_docs))
    if stage == "resume_evidence":
        row["candidateScopeLeakage"] = 1.0 if any(chunk.doc_id != query["resumeId"] for chunk, _ in ranked) else 0.0
    if stage == "jd_recall":
        row["duplicateDocRate@10"] = 1.0 - len({chunk.doc_id for chunk, _ in raw_ranked[:10]}) / max(1, len(raw_ranked[:10]))
    return row


def audit_chunk(stage: str, query: dict[str, Any], chunk: Chunk,
                score: float, rank: int) -> dict[str, Any]:
    exact, graded, hard = exact_and_graded(stage, query, chunk)
    row: dict[str, Any] = {
        "rank": rank,
        "chunkId": chunk.chunk_id,
        "docId": chunk.doc_id,
        "title": chunk.title,
        "section": chunk.section,
        "charStart": chunk.char_start,
        "charEnd": chunk.char_end,
        "chars": len(chunk.text),
        "text": chunk.text,
        "score": round(float(score), 8),
        "exact": exact,
        "gradedRelevance": graded,
        "hardNegative": hard,
    }
    span = evidence_overlap(query, chunk)
    row.update({
        "evidenceCoverage": round(float(span["coverage"]), 6),
        "evidencePurity": round(float(span["purity"]), 6),
        "evidenceF1": round(float(span["f1"]), 6),
        "evidenceExact": bool(span["exact"]),
    })
    if stage == "jd_recall":
        row["goldId"] = query["goldId"]
        row["labeledRelevance"] = query.get("relevance", {}).get(chunk.doc_id, 0)
    elif stage == "resume_evidence":
        ratios = chunk.metadata.get("sectionOverlapRatios") or {}
        row.update({
            "goldSections": query["goldSections"],
            "sectionOverlapRatios": ratios,
            "goldSectionRatio": round(sum(float(ratios.get(section_id, 0.0))
                                           for section_id in query["goldSections"]), 6),
            "sectionPurity": chunk.metadata.get("sectionPurity", 0.0),
            "mixedSection": bool(chunk.metadata.get("mixedSection")),
        })
    elif stage == "knowledge_recall":
        row.update({
            "goldDocIds": query["goldDocIds"],
            "goldSectionHints": query.get("goldSectionHints") or [],
            "parentSection": chunk.metadata.get("parentSection") or "",
            "documentMatch": chunk.doc_id in set(query["goldDocIds"]),
        })
    return row


def evaluate_variant(stage: str, config: dict[str, Any], out_dir: Path,
                     keep_rows: bool = True,
                     split_filter: str | None = None,
                     query_limit: int | None = None) -> dict[str, Any]:
    started_all = time.perf_counter()
    cache_dir = out_dir / "cache"
    embedding_cfg = config["embedding"]
    embedder = EmbeddingClient(
        embedding_cfg["model"], int(embedding_cfg["dimension"]), cache_dir)
    chunk_cfg = config["chunk"]
    semantic_client = None
    semantic_embed = None
    if chunk_cfg.get("strategy") in {"semantic", "section_semantic", "section_semantic_prefix"}:
        if embedding_cfg["model"] != "none" and int(embedding_cfg["dimension"]) > 0:
            semantic_client = embedder
        else:
            semantic_client = EmbeddingClient(
                str(chunk_cfg.get("semanticModel", "text-embedding-v3")),
                int(chunk_cfg.get("semanticDimension", 512)), cache_dir)
        semantic_embed = semantic_client.embed
    chunks, queries, scopes = stage_corpus(stage, chunk_cfg, semantic_embed)
    if split_filter is not None:
        queries = [query for query in queries
                   if query.get("benchmarkSplit") == split_filter]
        if not queries:
            raise RuntimeError(f"no {split_filter} queries for {stage}")
    if query_limit is not None and len(queries) > query_limit:
        queries = sorted(
            queries,
            key=lambda query: hashlib.sha256(
                f"joint-screen:{stage}:{query['caseId']}".encode("utf-8")
            ).hexdigest(),
        )[:query_limit]
    by_id = {chunk.chunk_id: chunk for chunk in chunks}
    scope_chunks = {name: [by_id[cid] for cid in ids] for name, ids in scopes.items()}
    tokenizer = config["tokenizer"]
    retrieval = config["retrieval"]
    rewrite_mode = config["rewrite"]
    rerank_mode = config["rerank"]
    deepseek = DeepSeekClient(cache_dir)
    qwen_rerank = QwenRerankClient(cache_dir)
    rewritten, rewrite_latencies, rewrite_errors = rewrite_queries(stage, queries, rewrite_mode, deepseek)

    # Build each scoped BM25 index once; the resume stage has one isolated index
    # per candidate, which makes cross-candidate leakage structurally impossible.
    bm25_started = time.perf_counter()
    lexical_scope_chunks = scope_chunks
    if stage == "jd_recall":
        whole_cfg = {"strategy": "whole", "size": 0, "overlap": 0}
        lexical_chunks, _, lexical_scopes = stage_corpus(stage, whole_cfg)
        lexical_by_id = {chunk.chunk_id: chunk for chunk in lexical_chunks}
        lexical_scope_chunks = {
            name: [lexical_by_id[cid] for cid in ids]
            for name, ids in lexical_scopes.items()
        }
    bm25 = {name: build_sparse_index(stage, items, tokenizer) for name, items in lexical_scope_chunks.items()}
    bm25_build_ms = (time.perf_counter() - bm25_started) * 1000

    needs_dense = retrieval["mode"] in {"dense", "hybrid"}
    chunk_vectors: dict[str, list[float]] = {}
    query_vectors: list[list[float]] = []
    embedding_index_ms = 0.0
    if needs_dense:
        if embedding_cfg["model"] == "none" or int(embedding_cfg["dimension"]) <= 0:
            raise RuntimeError("dense retrieval requested while embedding is N/A")
        embed_started = time.perf_counter()
        vectors = embedder.embed([chunk.text for chunk in chunks])
        chunk_vectors = {chunk.chunk_id: vector for chunk, vector in zip(chunks, vectors)}
        embedding_index_ms = (time.perf_counter() - embed_started) * 1000
        dense_queries = [query[:2000] if stage == "jd_recall" else query for query in rewritten]
        query_vectors = embedder.embed(dense_queries)

    rows: list[dict[str, Any]] = []
    traces: list[dict[str, Any]] = []
    rerank_errors: list[str] = []
    candidate_limit = int(retrieval.get("candidateLimit", 50))
    dense_limit = candidate_limit * int(retrieval.get("denseMultiplier", 1))
    score_threshold = float(retrieval.get("scoreThreshold", 0.0))
    for query_index, (query, effective_query) in enumerate(zip(queries, rewritten)):
        total_started = time.perf_counter()
        scope_name = query["scope"]
        candidates = scope_chunks[scope_name]
        sparse_query = effective_query[:3000] if stage == "jd_recall" else effective_query

        sparse_started = time.perf_counter()
        sparse = bm25[scope_name].search(sparse_query, candidate_limit)
        sparse_ms = (time.perf_counter() - sparse_started) * 1000

        dense_started = time.perf_counter()
        dense = []
        if needs_dense:
            dense = dense_search(
                candidates,
                [chunk_vectors[chunk.chunk_id] for chunk in candidates],
                query_vectors[query_index],
                dense_limit,
            )
            dense = [(chunk, score) for chunk, score in dense
                     if score >= score_threshold]
        dense_ms = (time.perf_counter() - dense_started) * 1000

        fusion_started = time.perf_counter()
        if retrieval["mode"] == "resume_production":
            structural = resume_structural_search(
                effective_query, candidates, tokenizer, candidate_limit)
            ranked = rrf_fuse(
                [(0.5, structural), (0.5, sparse)],
                int(retrieval["rrfK"]), candidate_limit)
        elif retrieval["mode"] == "lexical":
            ranked = dedupe_docs(sparse, candidate_limit) if stage == "jd_recall" else sparse
        elif retrieval["mode"] == "dense":
            ranked = dedupe_docs(dense, candidate_limit) if stage == "jd_recall" else dense
        else:
            semantic_weight = float(retrieval["semanticWeight"])
            if stage == "jd_recall":
                ranked = rrf_fuse_documents(
                    dense, sparse, semantic_weight,
                    int(retrieval["rrfK"]), candidate_limit)
            else:
                ranked = rrf_fuse(
                    [(semantic_weight, dense), (1.0 - semantic_weight, sparse)],
                    int(retrieval["rrfK"]), candidate_limit,
                )
        fusion_ms = (time.perf_counter() - fusion_started) * 1000
        raw_ranked = list(dense if stage == "jd_recall" and dense else ranked)

        rerank_started = time.perf_counter()
        rerank_error = None
        try:
            if rerank_mode == "feature":
                if stage == "resume_evidence":
                    ranked = resume_controller_rerank(effective_query, ranked[:20])
                elif stage == "knowledge_recall":
                    ranked = kb_feature_rerank(effective_query, ranked[:20], sparse, dense)
                else:
                    ranked = feature_rerank(effective_query, ranked[:20], tokenizer)
            elif rerank_mode == "qwen3":
                ranked = qwen_rerank.rerank(effective_query, ranked, 20)
            elif rerank_mode == "deepseek":
                ranked = deepseek.rerank(stage, effective_query, ranked)
            elif rerank_mode != "none":
                raise ValueError(rerank_mode)
        except Exception as exc:
            rerank_error = str(exc)
            rerank_errors.append(f"{query['caseId']}: {rerank_error}")
        rerank_ms = (time.perf_counter() - rerank_started) * 1000

        evaluated = dedupe_docs(ranked, 50) if stage == "jd_recall" else list(ranked[:50])
        metric = query_metrics(stage, query, evaluated, raw_ranked)
        metric.update({
            "caseId": query["caseId"], "caseType": query.get("caseType"),
            "formatCohort": query.get("formatCohort"),
            "lengthCohort": query.get("lengthCohort"),
            "goldAnnotationCohort": query.get("goldAnnotationCohort"),
            "signalPosition": query.get("signalPosition"),
            "querySource": query.get("querySource", "coordinator_auto" if stage == "jd_recall" else None),
            "benchmarkSplit": query.get("benchmarkSplit", "calibration"),
            "rewriteMs": rewrite_latencies[query_index], "sparseMs": sparse_ms,
            "denseMs": dense_ms, "fusionMs": fusion_ms, "rerankMs": rerank_ms,
            "totalMs": (time.perf_counter() - total_started) * 1000 + rewrite_latencies[query_index],
        })
        rows.append(metric)
        if keep_rows:
            traces.append({
                "caseId": query["caseId"], "query": query["query"],
                "scope": query.get("scope"), "resumeId": query.get("resumeId"),
                "effectiveQuery": effective_query,
                "denseQueryChars": min(len(effective_query), 2000) if stage == "jd_recall" else len(effective_query),
                "sparseQueryChars": min(len(effective_query), 3000) if stage == "jd_recall" else len(effective_query),
                "top": [
                    audit_chunk(stage, query, chunk, score, rank)
                    for rank, (chunk, score) in enumerate(evaluated[:10], start=1)
                ],
                "denseTop": [
                    audit_chunk(stage, query, chunk, score, rank)
                    for rank, (chunk, score) in enumerate(dense[:10], start=1)
                ],
                "sparseTop": [
                    audit_chunk(stage, query, chunk, score, rank)
                    for rank, (chunk, score) in enumerate(sparse[:10], start=1)
                ],
                "rerankError": rerank_error,
            })

    aggregate = aggregate_metrics(rows)
    case_types: dict[str, Any] = {}
    for case_type in sorted({str(row.get("caseType")) for row in rows}):
        case_types[case_type] = aggregate_metrics([row for row in rows if str(row.get("caseType")) == case_type])
    cohorts: dict[str, Any] = {}
    for cohort in sorted({str(row.get("formatCohort")) for row in rows if row.get("formatCohort")}):
        cohorts[cohort] = aggregate_metrics([row for row in rows if str(row.get("formatCohort")) == cohort])
    positions: dict[str, Any] = {}
    for position in sorted({str(row.get("signalPosition")) for row in rows if row.get("signalPosition")}):
        positions[position] = aggregate_metrics([row for row in rows if str(row.get("signalPosition")) == position])
    query_sources: dict[str, Any] = {}
    for source in sorted({str(row.get("querySource")) for row in rows if row.get("querySource")}):
        query_sources[source] = aggregate_metrics([row for row in rows if str(row.get("querySource")) == source])
    length_cohorts: dict[str, Any] = {}
    for cohort in sorted({str(row.get("lengthCohort")) for row in rows if row.get("lengthCohort")}):
        length_cohorts[cohort] = aggregate_metrics(
            [row for row in rows if str(row.get("lengthCohort")) == cohort]
        )
    annotation_cohorts: dict[str, Any] = {}
    for cohort in sorted({str(row.get("goldAnnotationCohort")) for row in rows if row.get("goldAnnotationCohort")}):
        annotation_cohorts[cohort] = aggregate_metrics(
            [row for row in rows if str(row.get("goldAnnotationCohort")) == cohort]
        )
    benchmark_splits: dict[str, Any] = {}
    for split in sorted({str(row.get("benchmarkSplit")) for row in rows if row.get("benchmarkSplit")}):
        benchmark_splits[split] = aggregate_metrics(
            [row for row in rows if str(row.get("benchmarkSplit")) == split]
        )
    stats = chunk_statistics(chunks, len(scopes) if stage == "resume_evidence" else len({c.doc_id for c in chunks}))
    stats["estimatedVectorBytes"] = len(chunks) * int(embedding_cfg["dimension"]) * 4 if needs_dense else 0
    aggregate["experimentWallMs"] = round((time.perf_counter() - started_all) * 1000, 3)
    external_failure = (
        (rewrite_mode == "deepseek" and bool(rewrite_errors))
        or (rerank_mode in {"qwen3", "deepseek"} and bool(rerank_errors))
    )
    result = {
        "status": "unavailable" if external_failure else "ok",
        "stage": stage,
        "config": config,
        "aggregate": aggregate,
        "byCaseType": case_types,
        "byFormatCohort": cohorts,
        "bySignalPosition": positions,
        "byQuerySource": query_sources,
        "byLengthCohort": length_cohorts,
        "byGoldAnnotationCohort": annotation_cohorts,
        "byBenchmarkSplit": benchmark_splits,
        "chunkStats": stats,
        "indexing": {"bm25BuildMs": round(bm25_build_ms, 3), "embeddingIndexMs": round(embedding_index_ms, 3)},
        "remote": {
            "embedding": embedder.stats.summary(),
            "semanticChunkEmbedding": (
                semantic_client.stats.summary() if semantic_client is not None
                and semantic_client is not embedder else
                embedder.stats.summary() if semantic_client is embedder else None
            ),
            "deepseek": deepseek.stats.summary(),
            "qwenRerank": qwen_rerank.stats.summary(),
        },
        "rewriteErrors": rewrite_errors,
        "rerankErrors": rerank_errors,
        "rows": rows if keep_rows else None,
        "traces": traces if keep_rows else None,
    }
    return result


def load_selections(path: Path) -> dict[str, Any]:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {stage: json.loads(json.dumps(DEFAULTS[stage])) for stage in STAGES}


def utility(result: dict[str, Any]) -> float:
    if result.get("status") != "ok":
        return -1e9
    metrics = (result.get("byBenchmarkSplit") or {}).get("calibration") or result["aggregate"]
    value = (
        float(metrics.get("ndcg@10", 0))
        + 0.35 * float(metrics.get("recall@5", 0))
        + 0.15 * float(metrics.get("mrr", 0))
        - 0.20 * float(metrics.get("hardNegativeFp@5", 0))
        - 0.30 * float(metrics.get("zeroHit", 0))
    )
    if result.get("stage") == "jd_recall":
        # JD ranking is document-level, but the dense representative must still
        # overlap the source sentences that justify the synthetic resume query.
        value += 0.15 * float(metrics.get("evidenceRecall@5", 0))
        value += 0.10 * float(metrics.get("evidenceContextPrecision@5", 0))
    if result.get("stage") in {"resume_evidence", "knowledge_recall"}:
        # These endpoints return evidence chunks to an LLM, so finding the
        # correct document is not sufficient. Reward focused evidence and add
        # a small context-budget penalty (capped so quality still dominates).
        value += 0.30 * float(metrics.get("evidenceRecall@5", 0))
        value += 0.20 * float(metrics.get("evidenceContextPrecision@5", 0))
        value -= min(0.15, float(metrics.get("contextChars@5", 0)) / 20000.0)
        value -= 0.20 * float(result.get("chunkStats", {}).get("mixedSectionRate", 0))
    if (result.get("stage") == "resume_evidence"
            and float(metrics.get("candidateScopeLeakage", 0)) > 0):
        return -1e9
    return value


def choose_winner(phase: str, results: dict[str, dict[str, Any]]) -> tuple[str, str]:
    available = [(name, result) for name, result in results.items() if result.get("status") == "ok"]
    if not available:
        raise RuntimeError(f"no successful variants for {phase}")
    scored = sorted(available, key=lambda row: (-utility(row[1]), row[0]))
    best_name, best = scored[0]
    best_utility = utility(best)
    near = [(name, result) for name, result in available if best_utility - utility(result) <= 0.005]
    if phase == "chunking" and len(near) > 1:
        best_name, best = min(near, key=lambda row: (
            row[1]["chunkStats"]["estimatedVectorBytes"],
            row[1]["chunkStats"]["overlapDuplicationRatio"], row[0]))
    elif phase == "embedding" and len(near) > 1:
        best_name, best = min(near, key=lambda row: (
            int(row[1]["config"]["embedding"]["dimension"]), row[0]))
    elif phase in {"rewrite", "rerank"}:
        baseline_name = "no_rewrite" if phase == "rewrite" else next(
            (name for name in results if name.startswith("no_rerank")), "no_rerank")
        baseline = results.get(baseline_name)
        if baseline and utility(best) - utility(baseline) < 0.01:
            best_name, best = baseline_name, baseline
            return best_name, "质量效用提升不足 0.01，按奥卡姆门槛保留无额外模型调用的基线"
    reason = (
        f"calibration utility={utility(best):.5f}; calibration nDCG@10="
        f"{((best.get('byBenchmarkSplit') or {}).get('calibration') or best['aggregate']).get('ndcg@10', 0):.5f}; "
        f"calibration Recall@5={((best.get('byBenchmarkSplit') or {}).get('calibration') or best['aggregate']).get('recall@5', 0):.5f}. "
        "效用差 <=0.005 时优先更小索引/更低复杂度。"
    )
    return best_name, reason


def apply_winner(selection: dict[str, Any], stage: str, phase: str, variant: dict[str, Any]) -> None:
    if phase == "chunking":
        selection["chunk"] = {key: value for key, value in variant.items() if key != "name"}
    elif phase == "embedding":
        selection["embedding"] = {key: variant[key] for key in ("model", "dimension")}
        if variant.get("retrievalMode"):
            selection["retrieval"]["mode"] = variant["retrievalMode"]
    elif phase == "tokenizer":
        selection["tokenizer"] = variant["tokenizer"]
    elif phase == "retrieval":
        selection["retrieval"] = {
            **selection.get("retrieval", {}),
            **{key: value for key, value in variant.items() if key != "name"},
        }
        if (stage == "resume_evidence" and variant["mode"] in {"dense", "hybrid"}
                and selection["embedding"].get("model") == "none"):
            selection["embedding"] = {"model": "text-embedding-v3", "dimension": 1024}
    elif phase == "rewrite":
        selection["rewrite"] = variant["rewrite"]
    elif phase == "rerank":
        selection["rerank"] = variant["rerank"]
    else:
        raise ValueError(phase)


def variants_for_phase(stage: str, phase: str) -> list[dict[str, Any]]:
    return {
        "chunking": CHUNK_VARIANTS_BY_STAGE[stage],
        "embedding": EMBEDDING_VARIANTS_BY_STAGE[stage],
        "tokenizer": TOKENIZER_VARIANTS_BY_STAGE[stage],
        "retrieval": RETRIEVAL_VARIANTS_BY_STAGE[stage],
        "rewrite": REWRITE_VARIANTS,
        "rerank": RERANK_VARIANTS_BY_STAGE[stage],
    }[phase]


def config_for_variant(selection: dict[str, Any], stage: str, phase: str,
                       variant: dict[str, Any]) -> dict[str, Any]:
    config = json.loads(json.dumps(selection))
    apply_winner(config, stage, phase, variant)
    # Phases before rerank should not accidentally inherit a production reranker
    # from DEFAULTS; each decision must be isolated.
    if phase != "rerank":
        config["rerank"] = "none"
    if phase not in {"rewrite", "rerank"}:
        config["rewrite"] = "none"
    return config


def run_phase(stage: str, phase: str, selection: dict[str, Any], out_dir: Path) -> dict[str, Any]:
    phase_results: dict[str, dict[str, Any]] = {}
    for variant in variants_for_phase(stage, phase):
        name = variant["name"]
        print(f"[{stage}/{phase}] {name}", flush=True)
        config = config_for_variant(selection, stage, phase, variant)
        try:
            result = evaluate_variant(
                stage, config, out_dir, keep_rows=True,
                split_filter="calibration")
        except Exception as exc:
            result = {"status": "unavailable", "stage": stage, "config": config,
                      "error": f"{type(exc).__name__}: {exc}"}
            print(f"  unavailable: {result['error']}", file=sys.stderr, flush=True)
        phase_results[name] = result
        dump_json(out_dir / "raw" / f"{stage}_{phase}_{name}.json", result)
        if result.get("status") == "ok":
            a = result["aggregate"]
            print(f"  recall@5={a.get('recall@5', 0):.4f} ndcg@10={a.get('ndcg@10', 0):.4f} "
                  f"mrr={a.get('mrr', 0):.4f} p95={a.get('totalMs', {}).get('p95', 0):.2f}ms", flush=True)
    winner, reason = choose_winner(phase, phase_results)
    ranked_candidates = [
        name for name, result in sorted(
            phase_results.items(), key=lambda item: (-utility(item[1]), item[0]))
        if result.get("status") == "ok"
    ]
    winner_variant = next(v for v in variants_for_phase(stage, phase) if v["name"] == winner)
    apply_winner(selection, stage, phase, winner_variant)
    summary = {
        "stage": stage, "phase": phase, "winner": winner, "reason": reason,
        "selectionData": "calibration_only",
        "rankedCandidates": ranked_candidates,
        "selectionAfterPhase": selection,
        "variants": {
            name: ({"status": result.get("status"), "aggregate": result.get("aggregate"),
                    "byBenchmarkSplit": result.get("byBenchmarkSplit"),
                    "byLengthCohort": result.get("byLengthCohort"),
                    "byGoldAnnotationCohort": result.get("byGoldAnnotationCohort"),
                    "chunkStats": result.get("chunkStats"), "remote": result.get("remote"),
                    "error": result.get("error")})
            for name, result in phase_results.items()
        },
    }
    dump_json(out_dir / f"{stage}_{phase}_summary.json", summary)
    return summary


SHORTLIST_LIMITS = {
    "chunking": 4, "embedding": 3, "tokenizer": 3,
    "retrieval": 5, "rewrite": 2, "rerank": 3,
}


def is_current_variant(stage: str, phase: str, variant: dict[str, Any]) -> bool:
    current = DEFAULTS[stage]
    if phase == "chunking":
        return all(variant.get(key) == current["chunk"].get(key)
                   for key in ("strategy", "size", "overlap"))
    if phase == "embedding":
        return (variant.get("model") == current["embedding"]["model"]
                and int(variant.get("dimension", -1)) == int(current["embedding"]["dimension"]))
    if phase == "tokenizer":
        return variant.get("tokenizer") == current["tokenizer"]
    if phase == "retrieval":
        return all(variant.get(key) == current["retrieval"].get(key)
                   for key in ("mode", "semanticWeight", "rrfK"))
    if phase == "rewrite":
        return variant.get("rewrite") == current["rewrite"]
    if phase == "rerank":
        return variant.get("rerank") == current["rerank"]
    return False


def joint_shortlist(stage: str, phase: str, out_dir: Path) -> list[dict[str, Any]]:
    path = out_dir / f"{stage}_{phase}_summary.json"
    if not path.exists():
        raise RuntimeError(f"joint search requires completed {stage}/{phase}: {path}")
    summary = json.loads(path.read_text(encoding="utf-8"))
    variants = variants_for_phase(stage, phase)
    by_name = {variant["name"]: variant for variant in variants}
    names = list(summary.get("rankedCandidates") or [])[:SHORTLIST_LIMITS[phase]]
    names.extend(variant["name"] for variant in variants
                 if is_current_variant(stage, phase, variant))
    if phase == "chunking":
        # Preserve at least one semantic candidate even when its local score is
        # weaker; it may interact positively with another embedding/retriever.
        names.extend(variant["name"] for variant in variants
                     if "semantic" in str(variant.get("strategy")))
    deduped = list(dict.fromkeys(name for name in names if name in by_name))
    if not deduped:
        raise RuntimeError(f"empty shortlist for {stage}/{phase}")
    return [by_name[name] for name in deduped]


def build_joint_configs(stage: str, out_dir: Path, selection: dict[str, Any],
                        trials: int, seed: int) -> list[dict[str, Any]]:
    phases = ("chunking", "embedding", "tokenizer", "retrieval", "rewrite", "rerank")
    pools = [joint_shortlist(stage, phase, out_dir) for phase in phases]
    candidates: dict[str, dict[str, Any]] = {}

    def add(config: dict[str, Any]) -> None:
        if (stage == "resume_evidence"
                and config["embedding"].get("model") == "none"
                and config["retrieval"].get("mode") in {"dense", "hybrid"}):
            return
        key = json.dumps(config, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        candidates.setdefault(key, config)

    add(json.loads(json.dumps(DEFAULTS[stage])))
    add(json.loads(json.dumps(selection)))
    product_rows = list(itertools.product(*pools))
    random.Random(seed + sum(map(ord, stage))).shuffle(product_rows)
    for values in product_rows:
        config = json.loads(json.dumps(DEFAULTS[stage]))
        for phase, variant in zip(phases, values):
            apply_winner(config, stage, phase, variant)
        add(config)
        if len(candidates) >= max(2, trials):
            break
    return list(candidates.values())[:max(2, trials)]


def paired_bootstrap(winner: dict[str, Any], baseline: dict[str, Any],
                     metrics: Sequence[str], seed: int,
                     samples: int = 2000) -> dict[str, Any]:
    winner_rows = {row["caseId"]: row for row in winner.get("rows") or []}
    baseline_rows = {row["caseId"]: row for row in baseline.get("rows") or []}
    case_ids = sorted(set(winner_rows) & set(baseline_rows))
    if not case_ids:
        return {"cases": 0, "metrics": {}}
    rng = random.Random(seed)
    output = {}
    for metric in metrics:
        observed = statistics.mean(
            float(winner_rows[case_id].get(metric, 0))
            - float(baseline_rows[case_id].get(metric, 0))
            for case_id in case_ids)
        draws = []
        for _ in range(samples):
            sampled = [case_ids[rng.randrange(len(case_ids))] for _ in case_ids]
            draws.append(statistics.mean(
                float(winner_rows[case_id].get(metric, 0))
                - float(baseline_rows[case_id].get(metric, 0))
                for case_id in sampled))
        draws.sort()
        output[metric] = {
            "delta": round(observed, 6),
            "ci95Low": round(percentile_summary(draws)["p50"] if len(draws) == 1
                             else draws[int(0.025 * (len(draws) - 1))], 6),
            "ci95High": round(draws[int(0.975 * (len(draws) - 1))], 6),
        }
    return {"cases": len(case_ids), "samples": samples, "metrics": output}


def paired_bootstrap_by_cohort(winner: dict[str, Any], baseline: dict[str, Any],
                               fields: Sequence[str], metrics: Sequence[str],
                               seed: int) -> dict[str, Any]:
    output: dict[str, Any] = {}
    winner_rows = winner.get("rows") or []
    baseline_rows = baseline.get("rows") or []
    for field_index, field in enumerate(fields):
        values = sorted({str(row.get(field)) for row in winner_rows if row.get(field)})
        output[field] = {}
        for value_index, value in enumerate(values):
            filtered_winner = {"rows": [row for row in winner_rows if str(row.get(field)) == value]}
            filtered_baseline = {"rows": [row for row in baseline_rows if str(row.get(field)) == value]}
            output[field][value] = paired_bootstrap(
                filtered_winner, filtered_baseline, metrics,
                seed + field_index * 1009 + value_index * 37,
                samples=1000)
    return output


def complexity_profile(result: dict[str, Any]) -> dict[str, float]:
    config = result.get("config") or {}
    retrieval = config.get("retrieval") or {}
    embedding = config.get("embedding") or {}
    rewrite = config.get("rewrite")
    rerank = config.get("rerank")
    aggregate = result.get("aggregate") or {}
    total_timing = aggregate.get("totalMs") or {}
    return {
        "externalCallsPerQuery": float(
            (1 if retrieval.get("mode") in {"dense", "hybrid"} else 0)
            + (1 if rewrite == "deepseek" else 0)
            + (1 if rerank in {"qwen3", "deepseek"} else 0)
        ),
        "generationCallsPerQuery": float(
            (1 if rewrite == "deepseek" else 0)
            + (1 if rerank in {"qwen3", "deepseek"} else 0)
        ),
        "estimatedVectorBytes": float(
            (result.get("chunkStats") or {}).get("estimatedVectorBytes") or 0),
        "embeddingDimension": float(embedding.get("dimension") or 0),
        "mixedSectionRate": float(
            (result.get("chunkStats") or {}).get("mixedSectionRate") or 0),
        "observedP95Ms": float(total_timing.get("p95") or 0),
    }


def joint_pareto(results: dict[str, dict[str, Any]]) -> list[str]:
    available = {name: result for name, result in results.items()
                 if result.get("status") == "ok"}
    frontier = []
    for name, result in available.items():
        quality = utility(result)
        cost = complexity_profile(result)
        dominated = False
        for other_name, other in available.items():
            if other_name == name:
                continue
            other_quality = utility(other)
            other_cost = complexity_profile(other)
            no_worse = (
                other_quality >= quality
                and other_cost["generationCallsPerQuery"] <= cost["generationCallsPerQuery"]
                and other_cost["estimatedVectorBytes"] <= cost["estimatedVectorBytes"]
                and other_cost["mixedSectionRate"] <= cost["mixedSectionRate"]
            )
            strictly_better = (
                other_quality > quality + 1e-9
                or other_cost["generationCallsPerQuery"] < cost["generationCallsPerQuery"]
                or other_cost["estimatedVectorBytes"] < cost["estimatedVectorBytes"]
                or other_cost["mixedSectionRate"] < cost["mixedSectionRate"]
            )
            if no_worse and strictly_better:
                dominated = True
                break
        if not dominated:
            frontier.append(name)
    return sorted(frontier, key=lambda name: (-utility(available[name]), name))


def choose_joint_winner(results: dict[str, dict[str, Any]],
                        baseline_name: str | None) -> tuple[str, str, list[str]]:
    frontier = joint_pareto(results)
    if not frontier:
        raise RuntimeError("joint search has no Pareto-eligible tuple")
    best_name = frontier[0]
    best_quality = utility(results[best_name])
    near = [name for name in frontier if best_quality - utility(results[name]) < 0.01]
    best_name = min(near, key=lambda name: (
        complexity_profile(results[name])["generationCallsPerQuery"],
        complexity_profile(results[name])["estimatedVectorBytes"],
        complexity_profile(results[name])["mixedSectionRate"],
        -utility(results[name]), name,
    ))
    if (baseline_name and baseline_name in results
            and results[baseline_name].get("status") == "ok"
            and utility(results[best_name]) - utility(results[baseline_name]) < 0.01):
        best_name = baseline_name
        reason = "联合搜索相对生产基线 calibration utility 提升不足 0.01，保留生产 tuple"
    else:
        reason = (
            f"Pareto frontier calibration utility={utility(results[best_name]):.5f}; "
            f"complexity={complexity_profile(results[best_name])}; "
            "质量差 <0.01 时优先更少生成调用、更小索引和更低混段率"
        )
    return best_name, reason, frontier


def run_joint(stage: str, selection: dict[str, Any], out_dir: Path,
              trials: int, finalists: int, seed: int) -> dict[str, Any]:
    configs = build_joint_configs(stage, out_dir, selection, trials, seed)
    screen_results: dict[str, dict[str, Any]] = {}
    screen_query_limit = 24 if stage == "knowledge_recall" else 32
    for index, config in enumerate(configs, start=1):
        name = f"tuple_{index:03d}_{hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()[:10]}"
        print(f"[{stage}/joint-screen] {index}/{len(configs)} {name}", flush=True)
        try:
            result = evaluate_variant(
                stage, config, out_dir, keep_rows=False,
                split_filter="calibration", query_limit=screen_query_limit)
        except Exception as exc:
            result = {"status": "unavailable", "stage": stage, "config": config,
                      "error": f"{type(exc).__name__}: {exc}"}
        screen_results[name] = result
        dump_json(out_dir / "raw" / "joint_screen" / f"{stage}_{name}.json", result)

    successful = [(name, result) for name, result in screen_results.items()
                  if result.get("status") == "ok"]
    if not successful:
        raise RuntimeError(f"no successful joint-screen tuple for {stage}")
    successful.sort(key=lambda item: (-utility(item[1]), item[0]))
    finalist_rows = successful[:max(1, finalists)]
    baseline_screen = next((row for row in successful
                            if row[1].get("config") == DEFAULTS[stage]), None)
    if baseline_screen and baseline_screen[0] not in {name for name, _ in finalist_rows}:
        finalist_rows.append(baseline_screen)
    full_results: dict[str, dict[str, Any]] = {}
    for index, (name, screened) in enumerate(finalist_rows, start=1):
        print(f"[{stage}/joint-finalist] {index}/{len(finalist_rows)} {name}", flush=True)
        try:
            result = evaluate_variant(
                stage, screened["config"], out_dir, keep_rows=True,
                split_filter="calibration")
        except Exception as exc:
            result = {"status": "unavailable", "stage": stage,
                      "config": screened["config"],
                      "error": f"{type(exc).__name__}: {exc}"}
        full_results[name] = result
        dump_json(out_dir / "raw" / "joint_finalists" / f"{stage}_{name}.json", result)
    baseline_name = next((name for name, result in full_results.items()
                          if result.get("config") == DEFAULTS[stage]), None)
    winner_name, reason, pareto_frontier = choose_joint_winner(
        full_results, baseline_name)
    winner_calibration = full_results[winner_name]
    winner_config = json.loads(json.dumps(winner_calibration["config"]))
    selection.clear()
    selection.update(winner_config)

    winner_heldout = evaluate_variant(
        stage, winner_config, out_dir, keep_rows=True, split_filter="heldout")
    baseline_heldout = evaluate_variant(
        stage, json.loads(json.dumps(DEFAULTS[stage])), out_dir,
        keep_rows=True, split_filter="heldout")
    dump_json(out_dir / "raw" / f"{stage}_joint_winner_heldout.json", winner_heldout)
    dump_json(out_dir / "raw" / f"{stage}_production_baseline_heldout.json", baseline_heldout)
    metric_names = ["recall@5", "ndcg@10", "mrr", "evidenceRecall@5",
                    "evidenceContextPrecision@5", "evidenceF1@5", "totalMs"]
    bootstrap = paired_bootstrap(winner_heldout, baseline_heldout, metric_names, seed)
    cohort_fields = (
        ["lengthCohort", "signalPosition", "goldAnnotationCohort"]
        if stage == "jd_recall" else
        ["formatCohort", "querySource"] if stage == "resume_evidence" else
        ["querySource", "caseType"]
    )
    cohort_bootstrap = paired_bootstrap_by_cohort(
        winner_heldout, baseline_heldout, cohort_fields, metric_names, seed + 31)

    operational = None
    if stage == "knowledge_recall":
        winner_operational = evaluate_variant(
            stage, winner_config, out_dir, keep_rows=True,
            split_filter="operational")
        baseline_operational = evaluate_variant(
            stage, json.loads(json.dumps(DEFAULTS[stage])), out_dir,
            keep_rows=True, split_filter="operational")
        dump_json(out_dir / "raw" / f"{stage}_joint_winner_operational.json", winner_operational)
        dump_json(out_dir / "raw" / f"{stage}_production_baseline_operational.json", baseline_operational)
        operational = {
            "winner": winner_operational.get("aggregate"),
            "productionBaseline": baseline_operational.get("aggregate"),
            "pairedBootstrap": paired_bootstrap(
                winner_operational, baseline_operational, metric_names, seed + 17),
        }

    summary = {
        "stage": stage,
        "phase": "joint",
        "selectionData": "calibration_only",
        "screenTrials": len(configs),
        "screenQueryLimit": screen_query_limit,
        "finalists": len(full_results),
        "winner": winner_name,
        "reason": reason,
        "paretoFrontier": [
            {"name": name, "utility": utility(full_results[name]),
             "complexity": complexity_profile(full_results[name])}
            for name in pareto_frontier
        ],
        "winnerConfig": winner_config,
        "calibration": winner_calibration.get("aggregate"),
        "calibrationByLengthCohort": winner_calibration.get("byLengthCohort"),
        "heldout": {
            "winner": winner_heldout.get("aggregate"),
            "productionBaseline": baseline_heldout.get("aggregate"),
            "winnerByLengthCohort": winner_heldout.get("byLengthCohort"),
            "productionBaselineByLengthCohort": baseline_heldout.get("byLengthCohort"),
            "winnerByGoldAnnotationCohort": winner_heldout.get("byGoldAnnotationCohort"),
            "productionBaselineByGoldAnnotationCohort": baseline_heldout.get("byGoldAnnotationCohort"),
            "pairedBootstrap": bootstrap,
            "pairedBootstrapByCohort": cohort_bootstrap,
        },
        "operational": operational,
        "screenRanking": [name for name, _ in successful],
    }
    dump_json(out_dir / f"{stage}_joint_summary.json", summary)
    return summary


def validate_only(out_dir: Path) -> int:
    result: dict[str, Any] = {"environment": environment_manifest(), "stages": {}}
    for stage in STAGES:
        stage_rows = {}
        for variant in CHUNK_VARIANTS_BY_STAGE[stage]:
            if variant["strategy"] in {"semantic", "section_semantic", "section_semantic_prefix"}:
                stage_rows[variant["name"]] = {
                    "requiresExternalEmbedding": True,
                    "semanticModel": variant.get("semanticModel"),
                    "semanticDimension": variant.get("semanticDimension"),
                }
                continue
            chunks, queries, scopes = stage_corpus(stage, variant)
            stage_rows[variant["name"]] = {
                "queries": len(queries),
                "scopes": len(scopes),
                "chunkStats": chunk_statistics(chunks, len(scopes) if stage == "resume_evidence" else len({c.doc_id for c in chunks})),
            }
        result["stages"][stage] = stage_rows
    dump_json(out_dir / "validation.json", result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    global DATA
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", choices=["validate", "chunking", "embedding", "tokenizer", "retrieval", "rewrite", "rerank", "joint", "all"], default="all")
    parser.add_argument("--stages", default="all", help="all or comma-separated stage names")
    parser.add_argument("--data", type=Path, default=DATA)
    parser.add_argument("--out", type=Path, default=ROOT / "reports" / "rag_three_stage")
    parser.add_argument("--joint-trials", type=int, default=48)
    parser.add_argument("--joint-finalists", type=int, default=10)
    parser.add_argument("--seed", type=int, default=20260804)
    args = parser.parse_args()
    DATA = args.data
    args.out.mkdir(parents=True, exist_ok=True)
    dump_json(args.out / "environment.json", environment_manifest())
    if args.phase == "validate":
        return validate_only(args.out)

    stages = list(STAGES) if args.stages == "all" else [item.strip() for item in args.stages.split(",") if item.strip()]
    unknown = set(stages) - set(STAGES)
    if unknown:
        raise SystemExit(f"unknown stages: {sorted(unknown)}")
    selection_path = args.out / "selection.json"
    selections = load_selections(selection_path)
    phases = ["chunking", "embedding", "tokenizer", "retrieval", "rewrite", "rerank", "joint"] if args.phase == "all" else [args.phase]
    all_summaries = []
    for stage in stages:
        if args.phase == "all":
            print(f"[{stage}/production_baseline] exact current-code configuration", flush=True)
            try:
                baseline = evaluate_variant(
                    stage, json.loads(json.dumps(DEFAULTS[stage])), args.out,
                    keep_rows=True, split_filter="calibration")
            except Exception as exc:
                baseline = {"status": "unavailable", "stage": stage,
                            "config": DEFAULTS[stage],
                            "error": f"{type(exc).__name__}: {exc}"}
            dump_json(args.out / f"{stage}_production_baseline.json", baseline)
        for phase in phases:
            if phase == "joint":
                summary = run_joint(
                    stage, selections[stage], args.out,
                    args.joint_trials, args.joint_finalists, args.seed)
            else:
                summary = run_phase(stage, phase, selections[stage], args.out)
            all_summaries.append(summary)
            dump_json(selection_path, selections)
    dump_json(args.out / "experiment_summary.json", {
        "environment": environment_manifest(),
        "selections": selections,
        "phases": all_summaries,
    })
    print(f"final selections -> {selection_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
