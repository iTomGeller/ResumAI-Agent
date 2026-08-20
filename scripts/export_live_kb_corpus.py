#!/usr/bin/env python3
"""Freeze the live knowledge-base corpus for a reproducible RAG experiment."""
from __future__ import annotations

import argparse
import json
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


def get_json(url: str, timeout: float = 60.0) -> Any:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    base = args.base.rstrip("/")
    listing = get_json(f"{base}/api/rag/knowledge-base/documents")
    documents = []
    for row in listing.get("documents") or []:
        doc_id = str(row["docId"])
        detail = get_json(f"{base}/api/rag/knowledge-base/documents/{urllib.parse.quote(doc_id)}")
        documents.append({
            "docId": doc_id,
            "title": detail.get("title") or row.get("title") or "",
            "docType": detail.get("docType") or row.get("docType") or "knowledge",
            "tags": detail.get("tags") or row.get("tags") or [],
            "content": detail.get("content") or "",
            "liveVersion": detail.get("version"),
            "liveIndexVersion": detail.get("indexVersion"),
            "liveChunkCount": detail.get("chunkCount"),
            "liveIndexStatus": detail.get("indexStatus"),
        })
    documents.sort(key=lambda item: item["title"])
    if any(not doc["content"] for doc in documents):
        raise SystemExit("one or more live documents had no content")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(documents, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "documents": len(documents),
        "characters": sum(len(doc["content"]) for doc in documents),
        "liveChunks": sum(int(doc.get("liveChunkCount") or 0) for doc in documents),
        "ready": sum(doc.get("liveIndexStatus") == "ready" for doc in documents),
        "output": str(args.out),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
