#!/usr/bin/env python3
"""Synchronize the curated production knowledge-base corpus.

Only documents whose titles belong to this repository's managed corpus are
changed. User-uploaded documents and other titles are never deleted.

Usage:
  python3 scripts/seed_knowledge_base.py --base http://127.0.0.1
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from typing import Any, Dict, List, Optional

from knowledge_base_corpus import DOCS, LEGACY_MANAGED_TITLES


def http(
    method: str,
    url: str,
    body: Optional[Dict[str, Any]] = None,
    timeout: float = 120.0,
) -> Dict[str, Any]:
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    request = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = response.read().decode("utf-8")
        return json.loads(payload) if payload else {}


def document_url(base: str, doc_id: str) -> str:
    return f"{base}/api/rag/knowledge-base/documents/{urllib.parse.quote(doc_id, safe='')}"


def same_managed_document(base: str, row: Dict[str, Any], expected: Dict[str, str]) -> bool:
    doc_id = str(row.get("docId") or row.get("documentId") or "").strip()
    if not doc_id:
        return False
    detail = http("GET", document_url(base, doc_id))
    return (
        str(detail.get("title") or "") == expected["title"]
        and str(detail.get("content") or "").strip() == expected["content"].strip()
        and str(detail.get("docType") or "") == expected["docType"]
    )


def delete_document(base: str, row: Dict[str, Any]) -> bool:
    doc_id = str(row.get("docId") or row.get("documentId") or "").strip()
    if not doc_id:
        raise ValueError(f"managed document has no docId: {row.get('title')}")
    result = http("DELETE", document_url(base, doc_id))
    return bool(result.get("removed"))


def create_document(base: str, doc: Dict[str, str]) -> Dict[str, Any]:
    result = http(
        "POST",
        f"{base}/api/rag/knowledge-base/documents",
        {
            "title": doc["title"],
            "content": doc["content"],
            "docType": doc["docType"],
            "tags": doc["tags"],
        },
    )
    document = result.get("document", result)
    return document if isinstance(document, dict) else {}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1")
    parser.add_argument(
        "--force",
        action="store_true",
        help="replace every repository-managed document even when content is unchanged",
    )
    args = parser.parse_args()
    base = args.base.rstrip("/")

    try:
        listing = http("GET", f"{base}/api/rag/knowledge-base/documents")
    except Exception as exc:
        print(f"FAIL  cannot list knowledge-base documents: {exc}", file=sys.stderr)
        return 1

    listed = listing.get("documents", []) if isinstance(listing, dict) else []
    if not isinstance(listed, list):
        print("FAIL  invalid knowledge-base listing response", file=sys.stderr)
        return 1

    by_title: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in listed:
        if isinstance(row, dict):
            by_title[str(row.get("title") or "")].append(row)

    new_by_title = {doc["title"]: doc for doc in DOCS}
    deleted = created = updated = skipped = failed = 0

    # Remove only the legacy seed corpus. An unrelated user-uploaded title is
    # outside LEGACY_MANAGED_TITLES and therefore cannot be touched here.
    for title in sorted(LEGACY_MANAGED_TITLES - set(new_by_title)):
        for row in by_title.get(title, []):
            try:
                if not delete_document(base, row):
                    raise RuntimeError("API reported removed=false")
                print(f"DELETE legacy  {title} -> {row.get('docId')}")
                deleted += 1
            except Exception as exc:
                print(f"FAIL   delete legacy {title}: {exc}", file=sys.stderr)
                failed += 1

    for title, expected in new_by_title.items():
        rows = by_title.get(title, [])
        keep_existing = False
        if not args.force and len(rows) == 1:
            try:
                keep_existing = same_managed_document(base, rows[0], expected)
            except Exception as exc:
                print(f"WARN   cannot compare {title}; replacing it: {exc}", file=sys.stderr)

        if keep_existing:
            print(f"SKIP   current {title}")
            skipped += 1
            continue

        replacing = bool(rows)
        delete_ok = True
        for row in rows:
            try:
                if not delete_document(base, row):
                    raise RuntimeError("API reported removed=false")
                print(f"DELETE stale   {title} -> {row.get('docId')}")
                deleted += 1
            except Exception as exc:
                print(f"FAIL   delete stale {title}: {exc}", file=sys.stderr)
                failed += 1
                delete_ok = False

        if not delete_ok:
            continue

        try:
            document = create_document(base, expected)
            action = "UPDATE" if replacing else "CREATE"
            print(
                f"{action:<6} {title} -> {document.get('docId')} "
                f"({document.get('chunkCount')} chunks)"
            )
            created += 1
            if replacing:
                updated += 1
        except (urllib.error.URLError, urllib.error.HTTPError, ValueError) as exc:
            print(f"FAIL   create {title}: {exc}", file=sys.stderr)
            failed += 1

    print(
        "\nmanaged corpus sync: "
        f"deleted={deleted} created={created} updated={updated} "
        f"skipped={skipped} failed={failed} defined={len(DOCS)}"
    )
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
