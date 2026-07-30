#!/usr/bin/env python3
"""Strict production evidence gate for MCP endpoints and ACTIVE Skills.

The gate accepts explicit run IDs so old traces from unstable workflow builds
cannot make a new build look covered.  MCP coverage requires a consolidated
SUCCESS invocation; Skill coverage requires both loaded and applied events in
one of the selected runs.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Dict, Iterable, List, Set
from urllib.parse import urlencode
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "config" / "mcp-servers.json"


def get_json(base: str, path: str, query: Dict[str, Any]) -> Dict[str, Any]:
    url = f"{base.rstrip('/')}{path}?{urlencode(query)}"
    with urlopen(Request(url, headers={"Accept": "application/json"}), timeout=90) as response:
        return json.loads(response.read().decode("utf-8"))


def list_rows(payload: Dict[str, Any], *keys: str) -> List[Dict[str, Any]]:
    result: List[Dict[str, Any]] = []
    seen: Set[str] = set()
    for key in keys:
        value = payload.get(key)
        if not isinstance(value, list):
            continue
        for row in value:
            if not isinstance(row, dict):
                continue
            marker = json.dumps(row, ensure_ascii=False, sort_keys=True, default=str)
            if marker not in seen:
                seen.add(marker)
                result.append(row)
    return result


def expected_endpoints(config_path: Path) -> Set[str]:
    config = json.loads(config_path.read_text(encoding="utf-8"))
    endpoints: Set[str] = set()
    for server, spec in (config.get("mcpServers") or {}).items():
        if not spec.get("enabled", True):
            continue
        prefix = str(spec.get("toolPrefix") or server)
        endpoints.update(f"{prefix}.{name}" for name in spec.get("allowedTools") or [])
    return endpoints


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://8.138.10.189")
    parser.add_argument("--run-id", action="append", required=True)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()

    run_ids = list(dict.fromkeys(args.run_id))
    expected_mcp = expected_endpoints(args.config)
    successful_mcp: Dict[str, Set[str]] = {}
    skill_events: Dict[str, Dict[str, Set[str]]] = {}
    active_skills: Set[str] = set()

    for run_id in run_ids:
        mcp = get_json(args.base, "/api/ops/mcp", {
            "runId": run_id, "recentLimit": 500,
        })
        for row in list_rows(mcp, "invocations", "recentCalls"):
            if str(row.get("runId") or "") != run_id:
                continue
            endpoint = str(row.get("tool") or row.get("toolName") or "")
            if str(row.get("outcome") or "").upper() == "SUCCESS":
                successful_mcp.setdefault(endpoint, set()).add(run_id)

        skills = get_json(args.base, "/api/ops/skills", {
            "runId": run_id, "recentLimit": 500,
        })
        active_skills.update(
            str(row.get("skillId") or row.get("name") or "")
            for row in skills.get("skills") or []
            if isinstance(row, dict)
            and str(row.get("status") or "").upper() == "ACTIVE"
            and not row.get("deprecated")
        )
        for row in list_rows(skills, "selectedApplied"):
            if str(row.get("runId") or "") != run_id:
                continue
            skill_id = str(row.get("skillId") or "")
            event_type = str(row.get("eventType") or "").lower()
            if skill_id and event_type in {"skill.loaded", "skill.applied"}:
                skill_events.setdefault(skill_id, {}).setdefault(event_type, set()).add(run_id)

    covered_skills = {
        skill_id for skill_id, events in skill_events.items()
        if events.get("skill.loaded") and events.get("skill.applied")
    }
    missing_mcp = sorted(expected_mcp - set(successful_mcp))
    missing_skills = sorted(active_skills - covered_skills)
    report = {
        "gate": "PASS" if not missing_mcp and not missing_skills else "FAIL",
        "runIds": run_ids,
        "mcp": {
            "expectedCount": len(expected_mcp),
            "coveredCount": len(expected_mcp & set(successful_mcp)),
            "expected": sorted(expected_mcp),
            "coveredByRun": {
                endpoint: sorted(successful_mcp.get(endpoint, set()))
                for endpoint in sorted(expected_mcp)
            },
            "missing": missing_mcp,
        },
        "skills": {
            "activeCount": len(active_skills),
            "coveredCount": len(active_skills & covered_skills),
            "active": sorted(active_skills),
            "coveredByRun": {
                skill_id: {
                    event: sorted(run_set)
                    for event, run_set in sorted(skill_events.get(skill_id, {}).items())
                }
                for skill_id in sorted(active_skills)
            },
            "missing": missing_skills,
        },
    }
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    print(rendered)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(rendered + "\n", encoding="utf-8")
    return 0 if report["gate"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
