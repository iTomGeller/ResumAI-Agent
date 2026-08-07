#!/usr/bin/env python3
"""Audit every persisted Top-K chunk from the three-stage RAG experiment."""
from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path
from typing import Any


CHANNELS = ("top", "denseTop", "sparseTop")


def inspect_file(path: Path, top_k: int) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    result = json.loads(path.read_text(encoding="utf-8"))
    stage = str(result.get("stage") or "unknown")
    traces = result.get("traces") or []
    violations: list[dict[str, Any]] = []
    inspected = 0
    primary_inspected = 0
    primary_exact = 0
    primary_evidence_exact = 0
    primary_mixed = 0
    primary_context_chars: list[int] = []
    primary_zero_hits = 0
    empty_primary_rankings = 0

    def fail(case_id: str, channel: str, message: str, chunk: dict[str, Any] | None = None) -> None:
        violations.append({
            "file": path.name,
            "stage": stage,
            "caseId": case_id,
            "channel": channel,
            "chunkId": (chunk or {}).get("chunkId"),
            "message": message,
        })

    for trace in traces:
        case_id = str(trace.get("caseId") or "")
        top_rows = list((trace.get("top") or [])[:top_k])
        if not top_rows:
            # This is a retrieval-quality failure, not a malformed chunk.  Keep
            # it visible in the audit summary without mixing it with structural
            # integrity violations.
            empty_primary_rankings += 1
        if not any(bool(row.get("exact")) for row in top_rows):
            primary_zero_hits += 1
        primary_context_chars.append(sum(int(row.get("chars") or 0) for row in top_rows))

        for channel in CHANNELS:
            rows = list((trace.get(channel) or [])[:top_k])
            seen_chunks: set[str] = set()
            seen_docs: set[str] = set()
            for expected_rank, chunk in enumerate(rows, start=1):
                inspected += 1
                if channel == "top":
                    primary_inspected += 1
                    primary_exact += int(bool(chunk.get("exact")))
                    primary_evidence_exact += int(bool(chunk.get("evidenceExact")))
                    primary_mixed += int(bool(chunk.get("mixedSection")))
                chunk_id = str(chunk.get("chunkId") or "")
                doc_id = str(chunk.get("docId") or "")
                text = str(chunk.get("text") or "")
                if int(chunk.get("rank") or 0) != expected_rank:
                    fail(case_id, channel, "non-contiguous rank", chunk)
                if not chunk_id or chunk_id in seen_chunks:
                    fail(case_id, channel, "missing or duplicate chunkId", chunk)
                seen_chunks.add(chunk_id)
                if not doc_id or not text.strip():
                    fail(case_id, channel, "empty docId or chunk text", chunk)
                if int(chunk.get("chars") or -1) != len(text):
                    fail(case_id, channel, "stored character count differs from text", chunk)
                if int(chunk.get("charEnd") or 0) < int(chunk.get("charStart") or 0):
                    fail(case_id, channel, "invalid character range", chunk)
                if chunk.get("evidenceExact") and (
                        float(chunk.get("evidenceCoverage") or 0) < 0.50
                        or float(chunk.get("evidencePurity") or 0) < 0.35):
                    fail(case_id, channel, "evidenceExact violates frozen span thresholds", chunk)

                if stage == "jd_recall" and channel == "top":
                    if doc_id in seen_docs:
                        fail(case_id, channel, "duplicate JD after document deduplication", chunk)
                    seen_docs.add(doc_id)
                elif stage == "resume_evidence":
                    resume_id = trace.get("resumeId") or trace.get("scope")
                    if resume_id and doc_id != resume_id:
                        fail(case_id, channel, "cross-candidate scope leakage", chunk)

    summary = {
        "file": path.name,
        "stage": stage,
        "status": result.get("status"),
        "queries": len(traces),
        "inspectedAcrossChannels": inspected,
        "primaryTopKInspected": primary_inspected,
        "primaryExactRate": round(primary_exact / max(1, primary_inspected), 6),
        "primaryEvidenceExactRate": round(primary_evidence_exact / max(1, primary_inspected), 6),
        "primaryMixedSectionRate": round(primary_mixed / max(1, primary_inspected), 6),
        "primaryZeroHitQueries": primary_zero_hits,
        "emptyPrimaryRankings": empty_primary_rankings,
        "meanPrimaryContextChars": round(statistics.mean(primary_context_chars), 3)
        if primary_context_chars else 0.0,
        "violations": len(violations),
        "config": result.get("config"),
    }
    return summary, violations


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--raw-dir", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--top-k", type=int, default=5)
    args = parser.parse_args()
    files = sorted(args.raw_dir.rglob("*.json"))
    if not files:
        raise SystemExit(f"no raw JSON files under {args.raw_dir}")

    summaries = []
    violations = []
    for path in files:
        summary, file_violations = inspect_file(path, args.top_k)
        summaries.append(summary)
        violations.extend(file_violations)

    payload = {
        "success": not violations,
        "topK": args.top_k,
        "files": len(files),
        "queries": sum(row["queries"] for row in summaries),
        "chunksInspectedAcrossChannels": sum(row["inspectedAcrossChannels"] for row in summaries),
        "violations": violations,
        "summaries": summaries,
    }
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "chunk_audit.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    lines = [
        "# RAG Top-K Chunk Full Audit",
        "",
        f"- Files: {payload['files']}",
        f"- Query-variant runs: {payload['queries']}",
        f"- Chunks inspected across fused/dense/sparse channels: {payload['chunksInspectedAcrossChannels']}",
        f"- Violations: {len(violations)}",
        "",
        "| Raw result | Stage | Queries | Fused Top-K | Rank exact | Evidence exact | Mixed-section rate | Zero-hit queries | Empty rankings | Mean context chars | Violations |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in summaries:
        lines.append(
            f"| {row['file']} | {row['stage']} | {row['queries']} | "
            f"{row['primaryTopKInspected']} | {row['primaryExactRate']:.4f} | "
            f"{row['primaryEvidenceExactRate']:.4f} | {row['primaryMixedSectionRate']:.4f} | "
            f"{row['primaryZeroHitQueries']} | "
            f"{row['emptyPrimaryRankings']} | {row['meanPrimaryContextChars']:.1f} | "
            f"{row['violations']} |"
        )
    if violations:
        lines.extend(["", "## Violations", ""])
        for row in violations[:200]:
            lines.append(
                f"- `{row['file']}` / `{row['caseId']}` / `{row['channel']}` / "
                f"`{row.get('chunkId')}`: {row['message']}"
            )
    (args.out / "chunk_audit.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps({key: payload[key] for key in (
        "success", "files", "queries", "chunksInspectedAcrossChannels")}, indent=2))
    return 0 if not violations else 1


if __name__ == "__main__":
    raise SystemExit(main())
