#!/usr/bin/env python3
"""Production smoke test for routing, Skills, MCP, Memory and RAG diversity.

This harness uploads genuinely different resume files through the public task
API, waits for each evaluation, and then inspects the same trace/Ops surfaces
used by the UI.  It intentionally does not mock the LLM, retrieval services or
MCP servers.

Example:

    python harness/verify_runtime_diversity.py \
      --base http://8.138.10.189 \
      --out reports/verification/runtime-diversity.json
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence
from urllib.parse import urlencode
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RESUMES = (
    ROOT / "testdata" / "resumes" / "ai_agent_backend.pdf",
    ROOT / "testdata" / "resumes" / "career_gap_risk_014.pdf",
    ROOT / "testdata" / "resumes" / "harness_no_project_frontend.txt",
    ROOT / "testdata" / "resumes" / "product_manager_llm.pdf",
)
TASK_TERMINAL = {"SUCCESS", "FAILED", "CANCELLED", "TIMEOUT"}
CANONICAL_MEMORY_TYPES = {"SEMANTIC", "EPISODIC", "PROCEDURAL", "WORKING"}


@dataclass
class Evaluation:
    resume: str
    trace_id: str
    status: str
    run_id: str
    task: Dict[str, Any]
    tree: Dict[str, Any]
    agents: List[str]
    route_mode: str
    skills: List[str]
    memory_types: List[str]
    memory_audit: List[Dict[str, Any]]
    mcp_calls: List[Dict[str, Any]]
    rag_events: List[Dict[str, Any]]


def log(message: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


def http_json(base: str, path: str, *, timeout: int = 45,
              query: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    url = f"{base.rstrip('/')}{path}"
    if query:
        values = {key: value for key, value in query.items() if value is not None}
        url = f"{url}?{urlencode(values)}"
    headers = {"Accept": "application/json"}
    token = (os.getenv("RESUMAI_AUTH_TOKEN") or "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = Request(url, headers=headers, method="GET")
    with urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def upload_resume(base: str, resume: Path) -> Dict[str, Any]:
    curl = shutil.which("curl.exe") or shutil.which("curl")
    if not curl:
        raise RuntimeError("curl is required for multipart resume upload")
    mime = "application/pdf" if resume.suffix.lower() == ".pdf" else "text/plain"
    command = [
        curl, "-fsS", "--max-time", "90",
        "-F", f"file=@{resume};type={mime}",
        "-F", "executionMode=DAG_CONCURRENT",
        f"{base.rstrip('/')}/api/tasks/upload-auto",
    ]
    token = (os.getenv("RESUMAI_AUTH_TOKEN") or "").strip()
    if token:
        command[1:1] = ["-H", f"Authorization: Bearer {token}"]
    completed = subprocess.run(
        command, capture_output=True, text=True, encoding="utf-8",
        errors="replace", check=False)
    if completed.returncode != 0:
        raise RuntimeError(
            f"upload failed rc={completed.returncode}: {completed.stderr[:240]}")
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"upload returned non-JSON: {completed.stdout[:240]}") from exc


def poll_task(base: str, trace_id: str, *, timeout_seconds: int,
              poll_seconds: int) -> Dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    last: Dict[str, Any] = {}
    while time.monotonic() < deadline:
        last = http_json(base, f"/api/tasks/{trace_id}", timeout=45)
        status = str(last.get("status") or "").upper()
        if status in TASK_TERMINAL:
            return last
        time.sleep(poll_seconds)
    raise TimeoutError(
        f"task {trace_id} did not finish in {timeout_seconds}s; "
        f"last status={last.get('status')}")


def rows(payload: Dict[str, Any], *keys: str) -> List[Dict[str, Any]]:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        if isinstance(value, dict) and isinstance(value.get("items"), list):
            return [item for item in value["items"] if isinstance(item, dict)]
    return []


def canonical_memory_type(row: Dict[str, Any]) -> str:
    raw = str(
        row.get("taxonomy") or row.get("memoryType") or row.get("type") or ""
    ).upper()
    aliases = {
        "DOMAIN": "SEMANTIC",
        "PREFERENCE": "SEMANTIC",
        "USER_PREFERENCE": "SEMANTIC",
        "CONVERSATION": "WORKING",
        "FAILURE": "EPISODIC",
    }
    return aliases.get(raw, raw)


def occurrence(row: Dict[str, Any]) -> str:
    return str(
        row.get("occurredAt") or row.get("startedAt")
        or row.get("retrievedAt") or "")


def valid_timestamp(value: str) -> bool:
    if not value:
        return False
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
        return True
    except ValueError:
        return False


def flatten_rounds(tree: Dict[str, Any]) -> Iterable[Dict[str, Any]]:
    for agent in tree.get("executionTree") or []:
        if not isinstance(agent, dict):
            continue
        for round_view in agent.get("rounds") or []:
            if isinstance(round_view, dict):
                yield round_view


def summarize_tree(tree: Dict[str, Any]) -> Dict[str, Any]:
    route = ((tree.get("harnessPlan") or {}).get("route") or {})
    agents = route.get("selectedAgents") or [
        agent.get("name") for agent in tree.get("executionTree") or []
        if isinstance(agent, dict) and agent.get("name")
    ]
    skills = set()
    memory_types = {
        canonical_memory_type(hit)
        for hit in tree.get("memoryTop") or []
        if isinstance(hit, dict)
    }
    for round_view in flatten_rounds(tree):
        for call in round_view.get("toolCalls") or []:
            if not isinstance(call, dict):
                continue
            if call.get("skillId"):
                skills.add(str(call["skillId"]))
    return {
        "agents": sorted(set(str(agent) for agent in agents if agent)),
        "routeMode": str(route.get("routeMode") or ""),
        "skills": sorted(skills),
        "memoryTypes": sorted(value for value in memory_types if value),
    }


def evaluate_one(base: str, resume: Path, *, timeout_seconds: int,
                 poll_seconds: int) -> Evaluation:
    if not resume.is_file():
        raise FileNotFoundError(resume)
    log(f"upload {resume.name}")
    created = upload_resume(base, resume)
    trace_id = str(created.get("traceId") or "")
    if not trace_id:
        raise RuntimeError(f"upload missing traceId: {created}")
    log(f"started {resume.name} trace={trace_id}")
    task = poll_task(
        base, trace_id, timeout_seconds=timeout_seconds,
        poll_seconds=poll_seconds)
    status = str(task.get("status") or "").upper()
    tree = http_json(
        base, f"/api/tasks/{trace_id}/agent-execution", timeout=90)
    summary = summarize_tree(tree)
    run_id = str(tree.get("runId") or "")

    mcp = http_json(
        base, "/api/ops/mcp", timeout=60,
        query={"runId": run_id, "recentLimit": 200})
    memory = http_json(
        base, "/api/ops/memory", timeout=60,
        query={"runId": run_id, "limit": 200})
    rag = http_json(
        base, "/api/ops/rag", timeout=60,
        query={"runId": run_id, "limit": 200})

    memory_usage = rows(memory, "usage")
    memory_entries = rows(memory, "entries")
    memory_audit = memory_usage + memory_entries
    memory_types = set(summary["memoryTypes"])
    for row in memory_audit:
        value = canonical_memory_type(row)
        if value:
            memory_types.add(value)
    rag_events = rows(rag, "events", "retrievals", "items", "details")

    log(
        f"finished {resume.name} status={status} run={run_id} "
        f"agents={summary['agents']} memory={sorted(memory_types)}")
    return Evaluation(
        resume=resume.name,
        trace_id=trace_id,
        status=status,
        run_id=run_id,
        task=task,
        tree=tree,
        agents=summary["agents"],
        route_mode=summary["routeMode"],
        skills=summary["skills"],
        memory_types=sorted(memory_types),
        memory_audit=memory_audit,
        mcp_calls=rows(mcp, "recentCalls", "invocations"),
        rag_events=rag_events,
    )


def check_report(base: str, evaluations: Sequence[Evaluation]) -> Dict[str, Any]:
    failures: List[str] = []
    run_ids = {evaluation.run_id for evaluation in evaluations}
    skill_panel = http_json(
        base, "/api/ops/skills", timeout=90,
        query={"recentLimit": 300})
    skill_events = [
        event for event in rows(skill_panel, "events", "selectedApplied")
        if str(event.get("runId") or "") in run_ids
    ]
    skills_by_run: Dict[str, set] = {run_id: set() for run_id in run_ids}
    for event in skill_events:
        skills_by_run[str(event.get("runId"))].add(
            str(event.get("skillId") or event.get("toolName") or ""))

    for evaluation in evaluations:
        evaluation.skills = sorted(
            set(evaluation.skills) | skills_by_run.get(evaluation.run_id, set()))
        if evaluation.status != "SUCCESS":
            failures.append(
                f"{evaluation.resume}: terminal status={evaluation.status}")
        if not evaluation.run_id:
            failures.append(f"{evaluation.resume}: trace has no runId")
        if not evaluation.agents:
            failures.append(f"{evaluation.resume}: Coordinator route is empty")
        for call in evaluation.mcp_calls:
            tool = str(call.get("tool") or call.get("toolName") or "")
            if tool.startswith("cn-web."):
                failures.append(f"{evaluation.resume}: synthetic tool leaked: {tool}")
            if not valid_timestamp(occurrence(call)):
                failures.append(
                    f"{evaluation.resume}: MCP call {tool} has no source timestamp")
        for event in evaluation.rag_events:
            if not valid_timestamp(occurrence(event)):
                failures.append(
                    f"{evaluation.resume}: RAG event has no source timestamp")
        for event in evaluation.memory_audit:
            if not valid_timestamp(occurrence(event)):
                failures.append(
                    f"{evaluation.resume}: Memory audit row has no source timestamp")

    for event in skill_events:
        if not valid_timestamp(occurrence(event)):
            failures.append(
                f"skill event {event.get('eventType')} "
                f"{event.get('skillId')} has no source timestamp")

    route_variants = {tuple(evaluation.agents) for evaluation in evaluations}
    skill_variants = {tuple(evaluation.skills) for evaluation in evaluations}
    memory_variants = {
        tuple(sorted(
            (
                str(row.get("consumerAgent") or row.get("agentId") or ""),
                canonical_memory_type(row),
                str(row.get("memoryId") or ""),
                str(row.get("decision") or row.get("used") or ""),
            )
            for row in evaluation.memory_audit
        ))
        for evaluation in evaluations
    }
    all_memory_types = {
        memory_type
        for evaluation in evaluations
        for memory_type in evaluation.memory_types
    }
    all_mcp_calls = [
        call for evaluation in evaluations for call in evaluation.mcp_calls
    ]
    lifecycle_events = {str(event.get("eventType") or "") for event in skill_events}

    if len(route_variants) < 2:
        failures.append("Coordinator selected the same agent set for every resume")
    if len(skill_variants) < 2:
        failures.append("Skill selection did not vary across resumes")
    if len(memory_variants) < 2:
        failures.append("Memory routing/selection did not vary across resumes")
    if not (all_memory_types & (CANONICAL_MEMORY_TYPES - {"EPISODIC"})):
        failures.append("all observed Memory usage is EPISODIC")
    unknown_memory = all_memory_types - CANONICAL_MEMORY_TYPES
    if unknown_memory:
        failures.append(f"non-canonical Memory taxonomy leaked: {unknown_memory}")
    if not all_mcp_calls:
        failures.append("no resume produced a real MCP invocation")
    if not ({"skill.loaded", "skill.applied"} & lifecycle_events):
        failures.append("Skills were advertised/selected but never progressively loaded")

    inventory = http_json(
        base, "/api/ops/mcp", timeout=90,
        query={"probe": "true", "recentLimit": 1})
    servers = inventory.get("servers") or {}
    server_names = set(servers) if isinstance(servers, dict) else {
        str(item.get("name")) for item in servers if isinstance(item, dict)
    }
    expected_keyless = {
        "exa", "context7", "deepwiki", "microsoft-learn", "fetch"}
    if server_names != expected_keyless:
        failures.append(
            "live MCP inventory is not the exact keyless set: "
            f"expected={sorted(expected_keyless)} actual={sorted(server_names)}")
    if "cn-web" in server_names:
        failures.append("synthetic cn-web server remains in live inventory")
    available_real = []
    iterable = (
        [{"name": name, **(server if isinstance(server, dict) else {})}
         for name, server in servers.items()]
        if isinstance(servers, dict) else servers
    )
    for server in iterable or []:
        if not isinstance(server, dict):
            continue
        if server.get("status") == "AUTH_REQUIRED":
            failures.append(
                f"credential-gated MCP leaked into production inventory: "
                f"{server.get('name')}")
        if server.get("status") == "AVAILABLE":
            available_real.append(str(server.get("name")))
    if not available_real:
        failures.append("no real MCP server is AVAILABLE after live probe")

    cases = [{
        "resume": evaluation.resume,
        "traceId": evaluation.trace_id,
        "runId": evaluation.run_id,
        "status": evaluation.status,
        "routeMode": evaluation.route_mode,
        "agents": evaluation.agents,
        "skills": evaluation.skills,
        "memoryTypes": evaluation.memory_types,
        "memoryAuditCount": len(evaluation.memory_audit),
        "mcpCallCount": len(evaluation.mcp_calls),
        "ragEventCount": len(evaluation.rag_events),
    } for evaluation in evaluations]
    return {
        "base": base,
        "generatedAt": datetime.now().astimezone().isoformat(),
        "passed": not failures,
        "failures": failures,
        "availableMcpServers": available_real,
        "routeVariantCount": len(route_variants),
        "skillVariantCount": len(skill_variants),
        "memoryVariantCount": len(memory_variants),
        "memoryTaxonomies": sorted(all_memory_types),
        "skillLifecycleEvents": sorted(lifecycle_events),
        "cases": cases,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1")
    parser.add_argument(
        "--resume", action="append", type=Path,
        help="resume path; repeat to override the four default fixtures")
    parser.add_argument("--timeout-seconds", type=int, default=600)
    parser.add_argument("--poll-seconds", type=int, default=5)
    parser.add_argument("--out", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    resumes = tuple(args.resume or DEFAULT_RESUMES)
    if len(resumes) < 3:
        raise SystemExit("at least three diverse resumes are required")
    evaluations = [
        evaluate_one(
            args.base, resume.resolve(),
            timeout_seconds=args.timeout_seconds,
            poll_seconds=args.poll_seconds)
        for resume in resumes
    ]
    report = check_report(args.base, evaluations)
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    print(rendered)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(rendered + "\n", encoding="utf-8")
        log(f"report written to {args.out}")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
    except Exception as exc:  # noqa: BLE001
        print(f"FATAL: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(2)
