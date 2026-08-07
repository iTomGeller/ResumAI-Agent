#!/usr/bin/env python3
"""Freeze chunker-independent evidence spans for the three RAG benchmarks."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "harness"))
from rag_three_stage_lib import normalize_text  # noqa: E402
from run_three_stage_rag_experiments import render_resume_case  # noqa: E402

DEFAULT_DATA = ROOT / "testdata" / "rag_three_stage"


def load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sentence_spans(text: str) -> list[tuple[int, int]]:
    """Return deterministic Chinese sentence/clause spans with source offsets."""
    spans: list[tuple[int, int]] = []
    start = 0
    for match in re.finditer(r"[。！？!?；;\n]+", text):
        end = match.end()
        if text[start:end].strip():
            left = start + len(text[start:end]) - len(text[start:end].lstrip())
            right = start + len(text[start:end].rstrip())
            spans.append((left, right))
        start = end
    if text[start:].strip():
        left = start + len(text[start:]) - len(text[start:].lstrip())
        spans.append((left, len(text.rstrip())))
    return spans


def compact_term_windows(text: str, base_start: int, terms: Iterable[str]) -> list[tuple[int, int, int]]:
    """Create focused, <=500-char evidence windows around exact source terms."""
    lowered = text.lower()
    rows: list[tuple[int, int, int]] = []
    for start, end in sentence_spans(text):
        sentence = lowered[start:end]
        matched = [term for term in terms if term and term.lower() in sentence]
        if not matched:
            continue
        if end - start <= 500:
            rows.append((base_start + start, base_start + end, len(set(matched))))
            continue
        positions = [sentence.find(term.lower()) for term in matched]
        centre = start + min(position for position in positions if position >= 0)
        window_start = max(start, centre - 180)
        window_end = min(end, window_start + 500)
        if window_end - window_start < 500:
            window_start = max(start, window_end - 500)
        rows.append((base_start + window_start, base_start + window_end, len(set(matched))))
    rows.sort(key=lambda row: (-row[2], row[0], row[1]))
    selected: list[tuple[int, int, int]] = []
    for row in rows:
        if any(max(row[0], old[0]) < min(row[1], old[1]) for old in selected):
            continue
        selected.append(row)
        if len(selected) >= 4:
            break
    return sorted(selected)


def heading_sections(text: str) -> list[dict[str, Any]]:
    matches = list(re.finditer(r"(?m)^(#{1,6})\s+(.+?)\s*$", text))
    sections = []
    for index, match in enumerate(matches):
        level = len(match.group(1))
        end = len(text)
        for later in matches[index + 1:]:
            if len(later.group(1)) <= level:
                end = later.start()
                break
        sections.append({
            "heading": match.group(2).strip(),
            "level": level,
            "start": match.start(),
            "end": end,
        })
    return sections


def evidence_row(doc_id: str, start: int, end: int, text: str,
                 section: str, annotation: str) -> dict[str, Any]:
    return {
        "docId": doc_id,
        "start": start,
        "end": end,
        "relevance": 3,
        "section": section,
        "textSha256": digest(text[start:end]),
        "annotationMethod": annotation,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", type=Path, default=DEFAULT_DATA)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    out = args.out or args.data / "rag_gold_spans.json"

    jds = load(args.data / "jd_catalog.json")
    jd_queries = load(args.data / "jd_queries.json")
    resumes = load(args.data / "resume_evidence_cases.json")
    kb_docs = load(args.data / "knowledge_documents_live.json")
    kb_queries = load(args.data / "knowledge_queries.json")
    jd_by_id = {row["jdId"]: row for row in jds}
    kb_by_title = {row["title"]: row for row in kb_docs}

    cases: dict[str, list[dict[str, Any]]] = {
        "jd_recall": [], "resume_evidence": [], "knowledge_recall": [],
    }
    missing: list[str] = []
    jd_fallback_cases: list[str] = []

    for query in jd_queries:
        doc = jd_by_id[query["goldId"]]
        prefix = f"岗位: {doc['title']}\n类别: {doc['category']}\n"
        indexed = normalize_text(prefix + doc["description"])
        description = normalize_text(doc["description"])
        description_start = indexed.find(description)
        windows = compact_term_windows(
            description, description_start, query.get("goldEvidenceTerms") or [])
        if not windows:
            fallback = [span for span in sentence_spans(description)
                        if span[1] - span[0] >= 30][:2]
            windows = [
                (description_start + start, description_start + min(end, start + 500), 0)
                for start, end in fallback
            ]
            jd_fallback_cases.append(query["caseId"])
        if not windows:
            missing.append(f"{query['caseId']}: no JD source evidence span")
        annotation = ("deterministic_duty_lead_fallback_v1"
                      if query["caseId"] in jd_fallback_cases
                      else "deterministic_term_sentence_v1")
        evidence = [
            evidence_row(doc["jdId"], start, end, indexed, "source_term_sentence",
                         annotation)
            for start, end, _ in windows
        ]
        cases["jd_recall"].append({
            "caseId": query["caseId"],
            "benchmarkSplit": query["benchmarkSplit"],
            "goldDocIds": [doc["jdId"]],
            "documentHashes": {doc["jdId"]: digest(indexed)},
            "goldEvidence": evidence,
        })

    for case_index, case in enumerate(resumes):
        rendered, ranges, cohort = render_resume_case(case, case_index)
        rendered = normalize_text(rendered)
        # render_resume_case already returns offsets in its output. Normalizing
        # the deterministic layouts must not change their length/offsets.
        rerendered, reranges, _ = render_resume_case(case, case_index)
        if normalize_text(rerendered) != rendered or rerendered != rendered:
            raise RuntimeError(f"unstable resume render: {case['resumeId']}")
        for query in case["queries"]:
            evidence = []
            for start, end, section_id in reranges:
                if section_id in set(query["goldSections"]):
                    evidence.append(evidence_row(
                        case["resumeId"], start, end, rendered, section_id,
                        "frozen_synthetic_section_span_v1"))
            if not evidence:
                missing.append(f"{query['caseId']}: no resume evidence span")
            cases["resume_evidence"].append({
                "caseId": query["caseId"],
                "benchmarkSplit": query["benchmarkSplit"],
                "goldDocIds": [case["resumeId"]],
                "layoutCohort": cohort,
                "documentHashes": {case["resumeId"]: digest(rendered)},
                "goldEvidence": evidence,
            })

    for query in kb_queries:
        evidence = []
        hashes = {}
        sections_by_title = query.get("goldSectionsByTitle") or {}
        for title in query.get("goldTitles") or []:
            doc = kb_by_title.get(title)
            if doc is None:
                missing.append(f"{query['caseId']}: live KB title missing: {title}")
                continue
            content = normalize_text(doc["content"])
            hashes[doc["docId"]] = digest(content)
            hints = sections_by_title.get(title) or query.get("goldSectionHints") or []
            for hint in hints:
                matches = [section for section in heading_sections(content)
                           if str(hint) in section["heading"]]
                if not matches:
                    missing.append(f"{query['caseId']}: KB heading missing: {title}/{hint}")
                    continue
                for section in matches:
                    evidence.append(evidence_row(
                        doc["docId"], section["start"], section["end"], content,
                        section["heading"], "curated_heading_span_v1"))
        if not evidence:
            missing.append(f"{query['caseId']}: no KB evidence span")
        cases["knowledge_recall"].append({
            "caseId": query["caseId"],
            "benchmarkSplit": query["benchmarkSplit"],
            "goldDocIds": sorted(hashes),
            "documentHashes": hashes,
            "goldEvidence": evidence,
        })

    if missing:
        raise SystemExit("gold span build failed:\n- " + "\n- ".join(missing))
    manifest = {
        "schemaVersion": 1,
        "coordinateSpace": "normalized_indexed_text",
        "thresholds": {"minGoldCoverage": 0.50, "minChunkPurity": 0.35},
        "cases": cases,
        "counts": {stage: len(rows) for stage, rows in cases.items()},
        "jdFallbackCases": jd_fallback_cases,
        "limitations": [
            "JD spans are deterministic term-bearing source sentences, not human double annotation.",
            "Resume spans are privacy-safe synthetic section annotations.",
            "KB spans are curated query-to-heading mappings over the frozen ECS snapshot.",
        ],
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"out": str(out), "counts": manifest["counts"]}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
