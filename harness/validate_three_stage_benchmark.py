#!/usr/bin/env python3
"""Fail-fast data qualification gate for all three RAG benchmark stages."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "harness"))
import run_three_stage_rag_experiments as experiment  # noqa: E402
from rag_three_stage_lib import normalize_text  # noqa: E402


def load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", type=Path, default=ROOT / "testdata" / "rag_three_stage")
    parser.add_argument("--out", type=Path, default=ROOT / "reports" / "rag_three_stage" / "data_gate.json")
    args = parser.parse_args()
    failures: list[str] = []
    warnings: list[str] = []

    jds = load(args.data / "jd_catalog.json")
    jd_queries = load(args.data / "jd_queries.json")
    resumes = load(args.data / "resume_evidence_cases.json")
    kb_docs = load(args.data / "knowledge_documents_live.json")
    kb_queries = load(args.data / "knowledge_queries.json")
    gold = load(args.data / "rag_gold_spans.json")
    manifest = load(args.data / "manifest.json")

    manifest_counts = {
        "jdDocuments": len(jds),
        "jdQueries": len(jd_queries),
        "resumeDocuments": len(resumes),
        "resumeQueries": sum(len(row.get("queries", [])) for row in resumes),
        "knowledgeDocuments": len(kb_docs),
        "knowledgeQueries": len(kb_queries),
    }
    for key, actual in manifest_counts.items():
        if manifest.get(key) != actual:
            failures.append(
                f"manifest {key}={manifest.get(key)!r}, but corpus contains {actual}"
            )
    if manifest.get("totalQueries") != (
            len(jd_queries) + manifest_counts["resumeQueries"] + len(kb_queries)):
        failures.append("manifest totalQueries does not match the three frozen query sets")

    jd_lengths = Counter(row.get("lengthCohort") for row in jds)
    jd_splits = Counter(row.get("benchmarkSplit") for row in jd_queries)
    if len(jds) != 120 or len({row["jdId"] for row in jds}) != 120:
        failures.append("JD corpus must contain 120 unique documents")
    if jd_lengths != Counter({"short": 40, "medium": 40, "long": 40}):
        failures.append(f"JD length cohorts must be 40/40/40, got {dict(jd_lengths)}")
    if len({row.get("source", {}).get("rowId") for row in jds}) != len(jds):
        failures.append("JD source row IDs are not unique")
    if any(row.get("source", {}).get("license") != "apache-2.0" for row in jds):
        failures.append("JD source/license provenance is incomplete")
    if any(row.get("formatCohort") == "markdown" for row in jds):
        failures.append("JD corpus contains Markdown despite textarea production shape")
    if jd_splits != Counter({"calibration": 80, "heldout": 40}):
        failures.append(f"JD split must be 80/40, got {dict(jd_splits)}")
    if any(query["goldId"] not in {row["jdId"] for row in jds} for query in jd_queries):
        failures.append("JD query references a missing gold document")

    resume_queries = [query for row in resumes for query in row["queries"]]
    resume_splits = Counter(row.get("benchmarkSplit") for row in resume_queries)
    layout_counts = Counter(
        experiment.render_resume_case(row, index)[2] for index, row in enumerate(resumes)
    )
    if len(resumes) != 30 or len(resume_queries) != 120:
        failures.append("resume evidence corpus must contain 30 resumes and 120 queries")
    if resume_splits != Counter({"calibration": 80, "heldout": 40}):
        failures.append(f"resume split must be 80/40, got {dict(resume_splits)}")
    if set(layout_counts) != {"blank_paragraphs", "line_only", "compressed_one_line", "ocr_noisy"}:
        failures.append(f"resume layout cohorts incomplete: {dict(layout_counts)}")
    for resume in resumes:
        section_ids = {section["sectionId"] for section in resume["sections"]}
        for query in resume["queries"]:
            if not set(query["goldSections"]) <= section_ids:
                failures.append(f"{query['caseId']} references a missing resume section")

    kb_splits = Counter(row.get("benchmarkSplit") for row in kb_queries)
    kb_sources = Counter(row.get("querySource") for row in kb_queries)
    live_titles = {row["title"] for row in kb_docs}
    if len(kb_docs) != 19 or any(row.get("liveIndexStatus") != "ready" for row in kb_docs):
        failures.append("KB snapshot must contain 19 ready ECS documents")
    if len(kb_queries) != 67 or kb_splits != Counter({
            "calibration": 40, "heldout": 20, "operational": 7}):
        failures.append(f"KB query count/split invalid: n={len(kb_queries)}, split={dict(kb_splits)}")
    if kb_sources != Counter({"copilot_question": 60, "workflow_template": 7}):
        failures.append(f"KB query-source cohorts invalid: {dict(kb_sources)}")
    for query in kb_queries:
        if not set(query.get("goldTitles") or []) <= live_titles:
            failures.append(f"{query['caseId']} references a non-live KB title")
        if query.get("querySource") == "copilot_question" and not query.get("goldSectionHints"):
            failures.append(f"{query['caseId']} lacks section-level gold")

    semantic_title_leaks = sum(
        1 for query in jd_queries
        if query.get("caseType") != "lexical"
        and any(row["title"].lower() in query["query"].lower()
                for row in jds if row["jdId"] == query["goldId"])
    )
    if semantic_title_leaks:
        warnings.append(
            f"{semantic_title_leaks} non-lexical JD queries contain the exact gold title; report separately"
        )

    documents: dict[str, dict[str, str]] = {
        "jd_recall": {}, "resume_evidence": {}, "knowledge_recall": {},
    }
    for row in jds:
        documents["jd_recall"][row["jdId"]] = normalize_text(
            f"岗位: {row['title']}\n类别: {row['category']}\n{row['description']}")
    for index, row in enumerate(resumes):
        rendered, _, _ = experiment.render_resume_case(row, index)
        documents["resume_evidence"][row["resumeId"]] = normalize_text(rendered)
    for row in kb_docs:
        documents["knowledge_recall"][row["docId"]] = normalize_text(row["content"])

    expected_counts = {"jd_recall": 120, "resume_evidence": 120, "knowledge_recall": 67}
    for stage, expected in expected_counts.items():
        gold_cases = gold.get("cases", {}).get(stage) or []
        if len(gold_cases) != expected or len({row["caseId"] for row in gold_cases}) != expected:
            failures.append(f"{stage} frozen gold count must be {expected}")
        for case in gold_cases:
            if not case.get("goldEvidence"):
                failures.append(f"{stage}/{case['caseId']} has no frozen evidence span")
            for doc_id, expected_hash in (case.get("documentHashes") or {}).items():
                text = documents[stage].get(doc_id)
                if text is None:
                    failures.append(f"{stage}/{case['caseId']} gold document missing: {doc_id}")
                    continue
                actual_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
                if actual_hash != expected_hash:
                    failures.append(f"{stage}/{case['caseId']} document hash drift: {doc_id}")
            for span in case.get("goldEvidence") or []:
                text = documents[stage].get(span["docId"])
                if text is None:
                    continue
                start, end = int(span["start"]), int(span["end"])
                if not (0 <= start < end <= len(text)):
                    failures.append(f"{stage}/{case['caseId']} span out of bounds: {start}:{end}")
                    continue
                span_hash = hashlib.sha256(text[start:end].encode("utf-8")).hexdigest()
                if span_hash != span.get("textSha256"):
                    failures.append(f"{stage}/{case['caseId']} evidence span hash drift")

    fallback_count = len(gold.get("jdFallbackCases") or [])
    if fallback_count:
        warnings.append(f"{fallback_count} JD cases use duty-lead fallback spans; report as weak annotation")

    # Queries from one resume/KB document must not be split across calibration
    # and held-out. Workflow templates are a separate operational cohort.
    resume_case_splits = {
        row["resumeId"]: {query["benchmarkSplit"] for query in row["queries"]}
        for row in resumes
    }
    if any(len(splits) != 1 for splits in resume_case_splits.values()):
        failures.append("queries from one resume cross calibration/held-out")
    kb_doc_splits: dict[str, set[str]] = {}
    gold_kb = {row["caseId"]: row for row in gold["cases"]["knowledge_recall"]}
    for query in kb_queries:
        if query["benchmarkSplit"] == "operational":
            continue
        for doc_id in gold_kb[query["caseId"]]["goldDocIds"]:
            kb_doc_splits.setdefault(doc_id, set()).add(query["benchmarkSplit"])
    if any(len(splits) != 1 for splits in kb_doc_splits.values()):
        failures.append("queries from one KB document cross calibration/held-out")

    report = {
        "success": not failures,
        "failures": failures,
        "warnings": warnings,
        "jd": {"documents": len(jds), "queries": len(jd_queries),
               "lengthCohorts": jd_lengths, "splits": jd_splits},
        "resumeEvidence": {"documents": len(resumes), "queries": len(resume_queries),
                           "layouts": layout_counts, "splits": resume_splits},
        "knowledge": {"documents": len(kb_docs), "queries": len(kb_queries),
                      "querySources": kb_sources, "splits": kb_splits},
        "goldSpans": {stage: sum(len(row["goldEvidence"])
                                  for row in gold["cases"][stage])
                      for stage in expected_counts},
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=dict) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2, default=dict))
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
