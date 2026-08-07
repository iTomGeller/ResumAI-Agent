#!/usr/bin/env python3
"""Export aggregate and representative Context Audit data for one load run.

The durable source remains MySQL ``llm_invocation``. This exporter reads the
redacted backend API, so generated report assets never need database secrets
and never contain the raw phone/email values present in test resumes.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import math
import re
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable


PHONE = re.compile(r"1[3-9]\d{9}")
EMAIL = re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}")
SECTION_MARKERS = (
    "[策略要求]", "[技能指令]", "[输出要求]", "[当前请求]",
    "[当前目标]", "[会话摘要]", "[近期消息]", "[相关记忆]",
    "[共享状态]", "[工具观察]",
)


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def get_json(url: str, timeout: float = 30.0) -> Any:
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def percentile(values: Iterable[float], q: float) -> float | None:
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return None
    position = (len(ordered) - 1) * q
    lo, hi = math.floor(position), math.ceil(position)
    if lo == hi:
        return ordered[lo]
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (position - lo)


def trace_ids(load_dir: Path) -> list[str]:
    traces: list[str] = []
    raw_path = load_dir / "raw_results.json"
    if raw_path.is_file():
        raw = load_json(raw_path)
        rows = raw if isinstance(raw, list) else raw.get("results", [])
        traces.extend(str(row.get("traceId") or "") for row in rows)
    arrivals = load_dir / "arrivals.jsonl"
    if arrivals.is_file():
        for line in arrivals.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            traces.append(str(row.get("traceId") or ""))
    return list(dict.fromkeys(trace for trace in traces if trace))


def parse_json_object(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    try:
        value = json.loads(str(raw or ""))
        return value if isinstance(value, dict) else {}
    except json.JSONDecodeError:
        return {}


def tool_names(request: dict[str, Any]) -> list[str]:
    names: list[str] = []
    for tool in request.get("tools") or []:
        function = tool.get("function") if isinstance(tool, dict) else {}
        if isinstance(function, dict) and function.get("name"):
            names.append(str(function["name"]))
    return names


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1")
    parser.add_argument("--load-dir", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path)
    parser.add_argument("--workers", type=int, default=16)
    args = parser.parse_args()
    base = args.base.rstrip("/")
    load_dir = args.load_dir.resolve()
    out_dir = (args.out_dir or load_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    traces = trace_ids(load_dir)
    if not traces:
        raise SystemExit("no traceIds found in load report")

    def list_trace(trace_id: str) -> tuple[str, list[dict[str, Any]]]:
        query = urllib.parse.urlencode({
            "traceId": trace_id, "page": 1, "pageSize": 100})
        payload = get_json(f"{base}/api/llm-invocations?{query}")
        return trace_id, list(payload.get("items") or [])

    with concurrent.futures.ThreadPoolExecutor(
            max_workers=min(args.workers, len(traces))) as pool:
        listed = dict(pool.map(list_trace, traces))

    indexed: list[tuple[str, dict[str, Any]]] = []
    for trace_id, items in listed.items():
        indexed.extend((trace_id, item) for item in items if item.get("id"))

    def detail(target: tuple[str, dict[str, Any]]) -> dict[str, Any]:
        trace_id, item = target
        full = get_json(
            f"{base}/api/llm-invocations/"
            f"{urllib.parse.quote(str(item['id']))}")
        full["traceId"] = trace_id
        return full

    with concurrent.futures.ThreadPoolExecutor(
            max_workers=args.workers) as pool:
        invocations = list(pool.map(detail, indexed))

    rows: list[dict[str, Any]] = []
    pii_phone = pii_email = 0
    for invocation in invocations:
        prompt_raw = str(invocation.get("promptFull") or "")
        response_raw = str(invocation.get("responseFull") or "")
        pii_phone += len(PHONE.findall(prompt_raw + response_raw))
        pii_email += len(EMAIL.findall(prompt_raw + response_raw))
        prompt = parse_json_object(prompt_raw)
        response = parse_json_object(response_raw)
        request = prompt.get("providerRequest") or {}
        messages = request.get("messages") or []
        joined = "\n".join(
            str(message.get("content") or "")
            for message in messages if isinstance(message, dict))
        usage = response.get("usage") or {}
        rows.append({
            "id": invocation.get("id"),
            "traceId": invocation.get("traceId"),
            "agent": invocation.get("agentRole"),
            "purpose": invocation.get("purpose"),
            "model": invocation.get("model"),
            "durationMs": invocation.get("durationMs") or 0,
            "promptChars": invocation.get("promptChars") or 0,
            "responseChars": invocation.get("responseChars") or 0,
            "inputTokens": invocation.get("inputTokens") or 0,
            "outputTokens": invocation.get("outputTokens") or 0,
            "cacheHitTokens": usage.get("prompt_cache_hit_tokens") or 0,
            "messageCount": len(messages),
            "toolCount": len(request.get("tools") or []),
            "toolNames": tool_names(request),
            "sections": [marker for marker in SECTION_MARKERS if marker in joined],
            "hasLoadedSkillBody": "[已加载技能指令]" in joined,
            "finishReason": invocation.get("finishReason"),
            "errorCode": invocation.get("errorCode"),
            "requestSha256": (
                (prompt.get("inventory") or {}).get("requestSha256")),
        })

    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row.get("agent") or "unknown")].append(row)
    per_agent: dict[str, Any] = {}
    for agent, agent_rows in sorted(grouped.items()):
        prompt_tokens = sum(int(row["inputTokens"]) for row in agent_rows)
        cache_tokens = sum(int(row["cacheHitTokens"]) for row in agent_rows)
        per_agent[agent] = {
            "calls": len(agent_rows),
            "traces": len({row["traceId"] for row in agent_rows}),
            "purposes": dict(Counter(str(row["purpose"]) for row in agent_rows)),
            "promptTokens": prompt_tokens,
            "completionTokens": sum(
                int(row["outputTokens"]) for row in agent_rows),
            "cacheHitTokens": cache_tokens,
            "cacheHitRate": round(cache_tokens / prompt_tokens, 6)
            if prompt_tokens else None,
            "durationMs": {
                "p50": percentile((row["durationMs"] for row in agent_rows), 0.50),
                "p95": percentile((row["durationMs"] for row in agent_rows), 0.95),
                "max": max(int(row["durationMs"]) for row in agent_rows),
            },
            "promptChars": {
                "p50": percentile((row["promptChars"] for row in agent_rows), 0.50),
                "p95": percentile((row["promptChars"] for row in agent_rows), 0.95),
                "max": max(int(row["promptChars"]) for row in agent_rows),
            },
            "messageCount": dict(Counter(
                str(row["messageCount"]) for row in agent_rows)),
            "toolNames": dict(Counter(
                name for row in agent_rows for name in row["toolNames"])),
            "sectionCoverage": {
                marker: sum(marker in row["sections"] for row in agent_rows)
                for marker in SECTION_MARKERS
            },
            "loadedSkillBodyCalls": sum(
                bool(row["hasLoadedSkillBody"]) for row in agent_rows),
            "errors": dict(Counter(
                str(row["errorCode"]) for row in agent_rows
                if row.get("errorCode"))),
        }

    roles_by_trace: dict[str, set[str]] = defaultdict(set)
    for row in rows:
        roles_by_trace[str(row["traceId"])].add(str(row["agent"]))
    target_roles = {
        "TechAgent", "ProjectAgent", "RiskAgent", "EvidenceAgent", "ReportAgent"}
    representative_trace = next(
        (trace for trace in traces
         if target_roles.issubset(roles_by_trace.get(trace, set()))),
        max(traces, key=lambda trace: len(roles_by_trace.get(trace, set()))))
    representative = [
        invocation for invocation in invocations
        if invocation.get("traceId") == representative_trace
    ]
    representative.sort(key=lambda row: (
        str(row.get("requestStartedAt") or ""), str(row.get("id") or "")))

    metrics = {
        "schemaVersion": 1,
        "loadDirectory": str(load_dir),
        "traceCount": len(traces),
        "tracesWithInvocations": sum(bool(items) for items in listed.values()),
        "invocationCount": len(rows),
        "representativeTraceId": representative_trace,
        "piiLeakCheck": {
            "phoneMatches": pii_phone,
            "emailMatches": pii_email,
            "passed": pii_phone == 0 and pii_email == 0,
        },
        "perAgent": per_agent,
        "perPurpose": dict(Counter(str(row["purpose"]) for row in rows)),
        "modelCalls": dict(Counter(str(row["model"]) for row in rows)),
        "errors": dict(Counter(
            str(row["errorCode"]) for row in rows if row.get("errorCode"))),
        "inventory": rows,
    }
    (out_dir / "context_audit_metrics.json").write_text(
        json.dumps(metrics, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")
    (out_dir / "context_audit_representative.json").write_text(
        json.dumps({
            "traceId": representative_trace,
            "note": "Backend-redacted real provider request/response envelopes",
            "invocations": representative,
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "traceCount": len(traces),
        "invocationCount": len(rows),
        "representativeTraceId": representative_trace,
        "piiLeakCheck": metrics["piiLeakCheck"],
        "agents": {agent: data["calls"] for agent, data in per_agent.items()},
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
