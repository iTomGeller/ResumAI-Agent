#!/usr/bin/env python3
"""Real agent E2E quality benchmark.

Each (gold case × policy × repetition) drives the REAL production path:

  Java /api/conversations → /messages → run queue → policy-forced dispatch →
  Python /agent/runs → Coordinator → RunExecutor → DeepSeek → Sandbox Manager
  → ephemeral Docker workers → result callback → /api/runs/{id}

Metrics come exclusively from the run's persisted runtime metrics (real LLM
calls, real token usage, real tool/sandbox calls, real latency). The
evaluator (mustFind / mustNotClaim / expectedRisk / expectedEvidence) runs
strictly outside the agent: those labels never enter the conversation, the
prompt, tool arguments, memory or shared state.

Only this benchmark may elect the Champion policy.

Usage (on ECS):
  cd /opt/resumai-src
  python3 harness/run_agent_e2e_benchmark.py \
      --base http://127.0.0.1 --repeats 3 \
      --policies balanced,strict_evidence,low_cost \
      --out reports/benchmark/e2e
"""
from __future__ import annotations

import argparse
import csv
import json
import statistics
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "workflow"))

from app.runtime.sandbox_tools_local import (  # noqa: E402 (evaluator only)
    evaluate_policy_output,
    locate_evidence,
)

TERMINAL = {"SUCCEEDED", "PARTIAL_SUCCESS", "FAILED", "CANCELLED", "TIMED_OUT"}
# DeepSeek-chat pricing (CNY per 1M tokens, cache miss), used for real cost.
PRICE_PROMPT_PER_M = 2.0
PRICE_COMPLETION_PER_M = 8.0


def http(method: str, url: str, body: Optional[dict] = None,
         timeout: float = 30.0) -> dict:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(url, data=data, method=method, headers={
        "Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


@dataclass
class E2EResult:
    case_id: str
    policy_id: str
    repeat: int
    run_id: str = ""
    trace_id: str = ""
    status: str = "FAILED"
    agents_used: List[str] = field(default_factory=list)
    llm_calls: int = 0
    tool_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    actual_cost_cny: float = 0.0
    latency_seconds: float = 0.0
    sandbox_timeouts: int = 0
    sandbox_ooms: int = 0
    evidence_support_ratio: Optional[float] = None
    jd_coverage: Optional[float] = None
    must_find_score: float = 0.0
    violation_penalty: float = 0.0
    unsupported_claim_rate: Optional[float] = None
    recommendation_accuracy: float = 0.0
    timeline_hit: Optional[bool] = None
    evidence_located_ratio: Optional[float] = None
    total_reward: float = 0.0
    error: Optional[str] = None


def load_cases(case_dir: Path, dataset: str) -> List[Dict[str, Any]]:
    path = case_dir / f"{dataset}_cases.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    for item in payload:
        for key in ("caseId", "resume", "jd", "userQuestion", "mustFind", "mustNotClaim"):
            if key not in item:
                raise ValueError(f"{path}: case missing {key}")
    return payload


def run_one(base: str, case: Dict[str, Any], policy_id: str, repeat: int,
            timeout_s: int) -> E2EResult:
    result = E2EResult(case["caseId"], policy_id, repeat)
    started = time.monotonic()
    try:
        conv = http("POST", f"{base}/api/conversations", {
            "title": f"e2e-{case['caseId']}-{policy_id}-r{repeat}",
            "resumeText": case["resume"],
            "jobDescription": case["jd"],
            "jobCategory": (case.get("metadata") or {}).get("jobCategory"),
        })
        conversation_id = conv["conversationId"]
        turn = http("POST", f"{base}/api/conversations/{conversation_id}/messages", {
            "clientMessageId": f"e2e-{uuid.uuid4().hex[:12]}",
            # Agent input: user question only. Labels never leave the evaluator.
            "content": case["userQuestion"],
            "queueMode": "collect",
            "forcedPolicyId": policy_id,
        })
        result.run_id = turn.get("runId") or ""
        if not result.run_id:
            result.error = "no runId returned"
            return result

        run_view: Dict[str, Any] = {}
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            run_view = http("GET", f"{base}/api/runs/{result.run_id}")
            if run_view.get("status") in TERMINAL:
                break
            time.sleep(3)
        result.status = str(run_view.get("status") or "TIMED_OUT_POLL")
        result.trace_id = str(run_view.get("traceId") or "")
        answer = str(run_view.get("answer") or "")
        metrics = run_view.get("metrics") or {}
        result.agents_used = list(metrics.get("agentsUsed") or [])
        result.llm_calls = int(metrics.get("llmCalls") or 0)
        result.tool_calls = int(metrics.get("toolCalls") or 0)
        result.prompt_tokens = int(metrics.get("promptTokens") or 0)
        result.completion_tokens = int(metrics.get("completionTokens") or 0)
        result.actual_cost_cny = round(
            result.prompt_tokens / 1e6 * PRICE_PROMPT_PER_M
            + result.completion_tokens / 1e6 * PRICE_COMPLETION_PER_M, 6)
        result.latency_seconds = float(metrics.get("latencySeconds")
                                       or (time.monotonic() - started))
        result.evidence_support_ratio = metrics.get("evidenceSupportRatio")
        result.jd_coverage = metrics.get("jdCoverage")

        shared = run_view.get("sharedState") or {}
        sandbox_stats = _sandbox_stats(base, result.run_id)
        result.sandbox_timeouts = sandbox_stats["timeouts"]
        result.sandbox_ooms = sandbox_stats["ooms"]

        # ---------------- evaluator (outside the agent) ----------------
        evaluation = evaluate_policy_output({
            "answer": answer,
            "resumeText": case["resume"],
            "mustFind": case.get("mustFind") or [],
            "mustNotClaim": case.get("mustNotClaim") or [],
        })
        result.must_find_score = float(evaluation.get("mustFindScore") or 0)
        result.violation_penalty = float(evaluation.get("violationPenalty") or 0)
        result.recommendation_accuracy = float(evaluation.get("score") or 0)

        expected_risks = set(case.get("expectedRisk") or [])
        if expected_risks:
            risk_text = json.dumps(shared.get("risks") or [], ensure_ascii=False) + answer
            hits = sum(1 for risk in expected_risks
                       if risk.replace("_", "") in risk_text.replace("_", "")
                       or risk in risk_text)
            result.timeline_hit = hits > 0

        expected_evidence = [str(e) for e in (case.get("expectedEvidence") or [])]
        if expected_evidence:
            located = locate_evidence({
                "resumeText": case["resume"], "claims": expected_evidence})
            result.evidence_located_ratio = located.get("supportRatio")

        claims = [c for c in (shared.get("evidence") or [])
                  if isinstance(c, dict) and c.get("verified") is not None]
        if claims:
            unsupported = sum(1 for c in claims if not c.get("verified"))
            result.unsupported_claim_rate = round(unsupported / len(claims), 4)

        # PARTIAL_SUCCESS must not share SUCCEEDED's full success credit.
        if result.status == "SUCCEEDED":
            succeeded = 1.0
        elif result.status == "PARTIAL_SUCCESS":
            succeeded = 0.45
        else:
            succeeded = 0.0
        # Missing evidence → 0 contribution (never treat undefined support as 1).
        support = (float(result.evidence_support_ratio)
                   if result.evidence_support_ratio is not None else 0.0)
        coverage = float(result.jd_coverage) if result.jd_coverage is not None else 0.0
        # Include timeline_hit / expectedRisk and unsupportedClaimRate when available.
        timeline_term = 0.0
        timeline_w = 0.0
        if result.timeline_hit is not None:
            timeline_term = 1.0 if result.timeline_hit else 0.0
            timeline_w = 0.08
        unsupported_term = 0.0
        unsupported_w = 0.0
        if result.unsupported_claim_rate is not None:
            unsupported_term = 1.0 - float(result.unsupported_claim_rate)
            unsupported_w = 0.08
        result.total_reward = round(
            0.25 * result.recommendation_accuracy
            + 0.12 * result.must_find_score
            - 0.12 * result.violation_penalty
            + 0.12 * support
            + 0.08 * coverage
            + 0.12 * succeeded
            + timeline_w * timeline_term
            + unsupported_w * unsupported_term
            - 0.08 * min(1.0, result.latency_seconds / 180.0)
            - 0.05 * min(1.0, result.actual_cost_cny / 0.5), 4)
        return result
    except (urllib.error.URLError, urllib.error.HTTPError, OSError) as exc:
        result.error = f"transport: {exc}"
        return result
    except Exception as exc:  # noqa: BLE001 - benchmark boundary
        result.error = f"{type(exc).__name__}: {exc}"
        return result


def _sandbox_stats(base: str, run_id: str) -> Dict[str, int]:
    """Real sandbox outcome counts recorded by the manager for this run."""
    try:
        view = http("GET", f"{base}/api/runs/{run_id}/sandbox-executions")
        rows = view if isinstance(view, list) else view.get("rows", [])
        return {
            "timeouts": sum(1 for r in rows if r.get("status") == "TIMED_OUT"),
            "ooms": sum(1 for r in rows if r.get("status") == "OOM_KILLED"),
        }
    except Exception:  # noqa: BLE001 - endpoint optional
        return {"timeouts": 0, "ooms": 0}


def aggregate(results: List[E2EResult]) -> Dict[str, Any]:
    by_policy: Dict[str, List[E2EResult]] = {}
    for row in results:
        by_policy.setdefault(row.policy_id, []).append(row)
    summary: Dict[str, Any] = {}
    for policy_id, rows in by_policy.items():
        n = max(1, len(rows))
        latencies = sorted(r.latency_seconds for r in rows)
        p95 = latencies[min(len(latencies) - 1, int(0.95 * (len(latencies) - 1)))]
        finished = [r for r in rows if r.status in ("SUCCEEDED", "PARTIAL_SUCCESS")]
        summary[policy_id] = {
            "runs": len(rows),
            "successRate": round(len(finished) / n, 4),
            "avgReward": round(sum(r.total_reward for r in rows) / n, 4),
            "rewardStdev": round(statistics.pstdev(
                [r.total_reward for r in rows]) if len(rows) > 1 else 0.0, 4),
            "avgLlmCalls": round(sum(r.llm_calls for r in rows) / n, 2),
            "avgToolCalls": round(sum(r.tool_calls for r in rows) / n, 2),
            "avgPromptTokens": int(sum(r.prompt_tokens for r in rows) / n),
            "avgCompletionTokens": int(sum(r.completion_tokens for r in rows) / n),
            "avgCostCny": round(sum(r.actual_cost_cny for r in rows) / n, 6),
            "avgLatencySeconds": round(sum(r.latency_seconds for r in rows) / n, 2),
            "p95LatencySeconds": round(p95, 2),
            "avgMustFindScore": round(sum(r.must_find_score for r in rows) / n, 4),
            "avgViolationPenalty": round(sum(r.violation_penalty for r in rows) / n, 4),
            "avgEvidenceSupport": round(sum(
                float(r.evidence_support_ratio or 0) for r in rows) / n, 4),
            "avgJdCoverage": round(sum(float(r.jd_coverage or 0) for r in rows) / n, 4),
            "sandboxTimeoutRate": round(sum(r.sandbox_timeouts for r in rows) / n, 4),
            "sandboxOomRate": round(sum(r.sandbox_ooms for r in rows) / n, 4),
            "failureRate": round(1 - len(finished) / n, 4),
        }
    champion = max(summary.items(), key=lambda kv: kv[1]["avgReward"])[0] \
        if summary else None
    return {"policies": summary, "championPolicy": champion}


def write_reports(out_dir: Path, benchmark_id: str, results: List[E2EResult],
                  summary: Dict[str, Any], meta: Dict[str, Any]) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "benchmarkId": benchmark_id,
        "kind": "REAL_AGENT_E2E",
        "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "meta": meta,
        "summary": summary,
        "results": [r.__dict__ for r in results],
    }
    (out_dir / "e2e_results.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    fields = ["caseId", "policyId", "repeat", "runId", "traceId", "status",
              "llmCalls", "toolCalls", "promptTokens", "completionTokens",
              "actualCostCny", "latencySeconds", "mustFindScore",
              "violationPenalty", "evidenceSupportRatio", "jdCoverage",
              "totalReward", "error"]
    with (out_dir / "e2e_results.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(fields)
        for r in results:
            writer.writerow([
                r.case_id, r.policy_id, r.repeat, r.run_id, r.trace_id, r.status,
                r.llm_calls, r.tool_calls, r.prompt_tokens, r.completion_tokens,
                r.actual_cost_cny, r.latency_seconds, r.must_find_score,
                r.violation_penalty, r.evidence_support_ratio, r.jd_coverage,
                r.total_reward, r.error or ""])

    lines = [
        "# Real Agent E2E Quality Benchmark",
        "",
        f"- Benchmark ID: `{benchmark_id}`",
        f"- Champion Policy: **{summary.get('championPolicy')}**（真实 E2E 平均 Reward 最高）",
        f"- 模型: {meta.get('model')}  重复次数/用例: {meta.get('repeats')}",
        "- 每一行都对应一次真实 /agent/runs 执行：真实 Coordinator、真实 DeepSeek、",
        "  真实 Sandbox Docker Worker；LLM 次数与 Token 来自 runtime metrics，",
        "  成本按 DeepSeek 官方单价由真实 Token 计算。",
        "- mustFind/mustNotClaim/expectedRisk 只进入评估器，从未进入 Agent 输入。",
        "",
        "## Policy Summary",
        "",
        "| Policy | Reward | Success | LLM Calls | Tokens(P/C) | Cost(CNY) | Avg Latency | P95 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for policy_id, stats in summary.get("policies", {}).items():
        mark = " ← champion" if policy_id == summary.get("championPolicy") else ""
        lines.append(
            f"| {policy_id}{mark} | {stats['avgReward']} | {stats['successRate']} | "
            f"{stats['avgLlmCalls']} | {stats['avgPromptTokens']}/{stats['avgCompletionTokens']} | "
            f"{stats['avgCostCny']} | {stats['avgLatencySeconds']}s | "
            f"{stats['p95LatencySeconds']}s |")
    (out_dir / "E2E_BENCHMARK_REPORT.md").write_text(
        "\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1")
    parser.add_argument("--cases", default=str(ROOT / "testdata" / "benchmark"))
    parser.add_argument("--dataset", default="gold")
    parser.add_argument("--case-ids", default="",
                        help="comma separated subset of caseIds")
    parser.add_argument("--policies", default="balanced,strict_evidence,low_cost")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--run-timeout", type=int, default=420)
    parser.add_argument("--out", default=str(ROOT / "reports" / "benchmark" / "e2e"))
    parser.add_argument("--model", default="deepseek-chat")
    args = parser.parse_args()

    cases = load_cases(Path(args.cases), args.dataset)
    if args.case_ids:
        wanted = {c.strip() for c in args.case_ids.split(",") if c.strip()}
        cases = [c for c in cases if c["caseId"] in wanted]
    policies = [p.strip() for p in args.policies.split(",") if p.strip()]
    benchmark_id = f"e2e-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"

    results: List[E2EResult] = []
    total = len(cases) * len(policies) * args.repeats
    index = 0
    for case in cases:
        for policy_id in policies:
            for repeat in range(1, args.repeats + 1):
                index += 1
                print(f"[{index}/{total}] {case['caseId']} × {policy_id} × r{repeat} ...",
                      flush=True)
                row = run_one(args.base, case, policy_id, repeat, args.run_timeout)
                results.append(row)
                print(f"    -> {row.status} reward={row.total_reward} "
                      f"llm={row.llm_calls} tokens={row.prompt_tokens}+{row.completion_tokens} "
                      f"cost={row.actual_cost_cny} latency={row.latency_seconds:.1f}s "
                      f"{('ERR ' + row.error) if row.error else ''}", flush=True)

    summary = aggregate(results)
    write_reports(Path(args.out), benchmark_id, results, summary, {
        "model": args.model,
        "repeats": args.repeats,
        "policies": policies,
        "dataset": args.dataset,
        "caseCount": len(cases),
    })
    print(json.dumps({
        "benchmarkId": benchmark_id,
        "championPolicy": summary.get("championPolicy"),
        "out": args.out,
        "totalRuns": len(results),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
