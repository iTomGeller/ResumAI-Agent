#!/usr/bin/env python3
"""Idempotently add the frozen real 120-JD RAG corpus to the live application.

Only IDs beginning with the explicit ``--owned-prefix`` are owned by this
script. Existing product JDs are never updated or deleted. By default owned
rows are skipped too; ``--update-owned`` performs an optimistic-lock update
when the frozen content changed.
"""
from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CATALOG = ROOT / "testdata" / "rag_three_stage" / "jd_catalog.json"


def request_json(method: str, url: str, body: dict[str, Any] | None = None,
                 timeout: float = 120.0, attempts: int = 3) -> Any:
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    last: Exception | None = None
    for attempt in range(attempts):
        request = urllib.request.Request(url, data=data, method=method, headers={
            "Content-Type": "application/json",
        })
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError) as exc:
            last = exc
            if attempt + 1 < attempts:
                time.sleep(1.0 * (2 ** attempt))
    raise RuntimeError(f"{method} {url} failed: {last}")


def list_all(base: str) -> dict[str, dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    page = 1
    while True:
        response = request_json(
            "GET", f"{base}/api/jds?page={page}&pageSize=100&category=ALL"
        )
        items = response.get("items") or []
        rows.update({str(row["jdId"]): row for row in items})
        total = int(response.get("total") or len(rows))
        if not items or len(rows) >= total:
            break
        page += 1
    return rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1")
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    parser.add_argument("--owned-prefix", default="exp-real-jd-")
    parser.add_argument("--update-owned", action="store_true")
    parser.add_argument("--max", type=int, default=0, help="seed only N rows for a smoke test")
    parser.add_argument("--receipt", type=Path)
    args = parser.parse_args()
    base = args.base.rstrip("/")
    catalog = json.loads(args.catalog.read_text(encoding="utf-8"))
    if args.max > 0:
        catalog = catalog[:args.max]
    if not args.owned_prefix or len(args.owned_prefix) < 8:
        raise SystemExit("owned prefix is too broad; refusing to mutate")
    if any(not str(row.get("jdId", "")).startswith(args.owned_prefix) for row in catalog):
        raise SystemExit("catalog contains a non-owned JD id; refusing to mutate")

    before = list_all(base)
    receipt: dict[str, Any] = {
        "startedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "base": base,
        "beforeTotal": len(before),
        "requested": len(catalog),
        "created": [], "updated": [], "skipped": [], "failed": [],
    }
    for index, row in enumerate(catalog, start=1):
        jd_id = row["jdId"]
        payload = {key: row[key] for key in ("jdId", "title", "category", "description")}
        try:
            if jd_id not in before:
                result = request_json("POST", f"{base}/api/jds", payload)
                receipt["created"].append(jd_id)
                action = "created"
            elif args.update_owned:
                current = request_json("GET", f"{base}/api/jds/{urllib.parse.quote(jd_id)}")
                if (current.get("title") == row["title"] and
                        current.get("category") == row["category"] and
                        current.get("description") == row["description"]):
                    receipt["skipped"].append(jd_id)
                    action = "unchanged"
                else:
                    payload["version"] = current["version"]
                    result = request_json("PUT", f"{base}/api/jds/{urllib.parse.quote(jd_id)}", payload)
                    receipt["updated"].append(jd_id)
                    action = f"updated-v{result.get('version')}"
            else:
                receipt["skipped"].append(jd_id)
                action = "exists"
            print(f"[{index:03d}/{len(catalog):03d}] {action}: {jd_id}", flush=True)
        except Exception as exc:
            receipt["failed"].append({"jdId": jd_id, "error": str(exc)})
            print(f"[{index:03d}/{len(catalog):03d}] FAILED: {jd_id}: {exc}", flush=True)

    after = list_all(base)
    owned_after = {jd_id for jd_id in after if jd_id.startswith(args.owned_prefix)}
    missing = [row["jdId"] for row in catalog if row["jdId"] not in owned_after]
    receipt.update({
        "finishedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "afterTotal": len(after),
        "ownedAfter": len(owned_after),
        "missingAfter": missing,
        "success": not receipt["failed"] and not missing,
    })
    receipt_path = args.receipt or (ROOT / "reports" / "rag_three_stage" / "jd_seed_receipt.json")
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    receipt_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: value for key, value in receipt.items() if key not in {"created", "updated", "skipped", "failed"}}, ensure_ascii=False, indent=2))
    print(f"receipt -> {receipt_path}")
    return 0 if receipt["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
