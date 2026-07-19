#!/usr/bin/env python3
"""Compute a deterministic resume-evaluation invalidation plan from JSON input."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


NODE_ORDER = (
    "intent",
    "resume_parse",
    "jd_match",
    "knowledge_context",
    "tech_eval",
    "project_eval",
    "risk_eval",
    "evidence_fusion",
    "report",
)

INVALIDATION_BY_ARTIFACT = {
    "resume": NODE_ORDER,
    "jd": NODE_ORDER[2:],
    "target_role": NODE_ORDER[2:],
    "preferences": NODE_ORDER,
    "evaluation_focus": NODE_ORDER,
    "external_evidence": NODE_ORDER[4:],
    "rubric": NODE_ORDER[4:],
    "conversation_only": (),
}


def _strings(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def plan_revision(payload: dict[str, Any]) -> dict[str, Any]:
    changed = list(dict.fromkeys(_strings(payload.get("changedArtifacts"))))
    completed = set(_strings(payload.get("completedNodes")))
    unknown = [item for item in changed if item not in INVALIDATION_BY_ARTIFACT]

    invalidated_set: set[str] = set()
    reasons: dict[str, list[str]] = {}
    for artifact in changed:
        for node in INVALIDATION_BY_ARTIFACT.get(artifact, ()):
            invalidated_set.add(node)
            reasons.setdefault(node, []).append(artifact)

    invalidated = [node for node in NODE_ORDER if node in invalidated_set]
    reusable = [node for node in NODE_ORDER if node in completed and node not in invalidated_set]
    restart_from = invalidated[0] if invalidated else None
    base_revision = str(payload.get("baseRevision") or "").strip() or None
    new_revision = str(payload.get("newRevision") or "").strip() or None

    return {
        "baseRevision": base_revision,
        "newRevision": new_revision,
        "changedArtifacts": changed,
        "invalidateNodes": invalidated,
        "reuseNodes": reusable,
        "restartFrom": restart_from,
        "supersedesRevision": base_revision if changed and invalidated else None,
        "reasonByNode": {
            node: "depends on changed artifact(s): " + ", ".join(reasons[node])
            for node in invalidated
        },
        "unknownArtifacts": unknown,
        "needsConfirmation": bool(unknown),
    }


def _read_payload(args: argparse.Namespace) -> dict[str, Any]:
    if args.payload:
        raw = args.payload
    elif args.input:
        raw = Path(args.input).read_text(encoding="utf-8")
    else:
        raw = sys.stdin.read()
    value = json.loads(raw or "{}")
    if not isinstance(value, dict):
        raise ValueError("input must be a JSON object")
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--payload", help="JSON object as a string")
    source.add_argument("--input", help="path to a UTF-8 JSON file; stdin is used when omitted")
    args = parser.parse_args()
    try:
        result = plan_revision(_read_payload(args))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
