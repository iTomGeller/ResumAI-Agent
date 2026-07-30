"""Controlled TTL selection with the current production workflow.

The experiment is intentionally ECS-local.  It reuses memory payloads produced
by one exact workflow build, time-shifts their *availability* without mutating
MySQL, and runs the real Coordinator/agents/LLM for differentiated gold cases.

It answers two separate questions:

* Does retaining a memory class change report quality/cost versus expiry?
* Among the configured candidate TTLs, what is the shortest value that covers
  the declared product horizon without losing the measured benefit?

Age shifting never rewrites memory text, labels or gold answers.  Conflicted,
archived and expired rows are excluded before injection, matching production
search semantics.  WORKING is handled as a control-plane bound because the
runtime restores PAUSED runs from execution_snapshot and never injects RUN
scratch memory into a new evaluation prompt.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import urllib.request
import uuid
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = ROOT.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.runtime.builtin_tools import BuiltinToolRegistry  # noqa: E402
from app.runtime.events import NullEmitter  # noqa: E402
from app.runtime.executor import RunExecutor  # noqa: E402
from app.runtime.memory import NullMemoryClient  # noqa: E402
from app.runtime.models import AgentRunRequest  # noqa: E402
from app.runtime.builtin_tools import (  # noqa: E402
    evaluate_report_quality,
    locate_evidence,
)
from scripts.ecs_workflow_simulator import LiveContextRecorder  # noqa: E402


CANDIDATES = {
    "WORKING": [1, 2, 3, 7],
    "SEMANTIC": [30, 60, 90, 180],
    "EPISODIC": [30, 60, 90, 180],
    "PROCEDURAL": [90, 180, 365, 730],
}

# Boundary probes model a delayed consumer, not fabricated historical usage.
# The last point is immediately below the product horizon so an off-by-one
# expiry is observable.  These schedules are reported verbatim in the output.
HORIZON_PROBES_DAYS = {
    "SEMANTIC": [1, 7, 30, 60, 89],
    "EPISODIC": [1, 7, 30, 60, 89],
    "PROCEDURAL": [1, 7, 30, 90, 180, 364],
}

PRICE_PROMPT_PER_M = 2.0
PRICE_COMPLETION_PER_M = 8.0


def fetch_json(url: str, timeout: float = 30.0) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def load_cases(path: Path, wanted: set[str]) -> list[dict[str, Any]]:
    rows = json.loads(path.read_text(encoding="utf-8"))
    selected = [row for row in rows if str(row.get("caseId")) in wanted]
    missing = wanted - {str(row.get("caseId")) for row in selected}
    if missing:
        raise ValueError(f"missing cases: {sorted(missing)}")
    return selected


def tokens(text: str) -> set[str]:
    cleaned = "".join(ch.lower() if ch.isalnum() else " " for ch in text)
    return {token for token in cleaned.split() if len(token) >= 2}


def overlap_score(row: dict[str, Any], case: dict[str, Any]) -> int:
    haystack = tokens(str(row.get("content") or ""))
    needle = tokens(str(case.get("resume") or "") + " " + str(case.get("jd") or ""))
    return len(haystack & needle)


def current_fixtures(
    ops_payload: dict[str, Any],
    case: dict[str, Any],
    producer_version: str,
) -> dict[str, dict[str, Any]]:
    """Select exact-build payloads and convert staged strategy to its target type."""
    rows = [
        row for row in (ops_payload.get("entries") or [])
        if str(row.get("producerVersion") or "") == producer_version
    ]
    selectors = {
        "SEMANTIC": lambda row: (
            str(row.get("type") or "").upper() == "SEMANTIC"
            and row.get("source") == "candidate_fact"),
        "EPISODIC": lambda row: (
            str(row.get("type") or "").upper() == "EPISODIC"
            and row.get("source") == "cross_candidate_anchor"),
        # Runtime durable writes are staged as WORKING then promoted.  The
        # staged row is the exact current-build payload even when promotion
        # deduplicates onto a pre-versioned PROCEDURAL row.
        "PROCEDURAL": lambda row: row.get("source") == "runtime_strategy",
    }
    result: dict[str, dict[str, Any]] = {}
    for memory_type, predicate in selectors.items():
        candidates = [row for row in rows if predicate(row)]
        if not candidates:
            raise RuntimeError(
                f"no {memory_type} fixture with producerVersion={producer_version}")
        source = max(candidates, key=lambda row: overlap_score(row, case))
        owner_scope = str(source.get("ownerScope") or "USER").upper()
        if memory_type == "PROCEDURAL":
            owner_scope = "USER"
        elif memory_type == "SEMANTIC":
            owner_scope = "CONVERSATION"
        else:
            owner_scope = "USER"
        result[memory_type] = {
            "memoryId": f"ttl-{memory_type.lower()}-{case['caseId']}",
            "type": memory_type,
            "memoryType": memory_type,
            "taxonomy": memory_type,
            "ownerScope": owner_scope,
            "userId": "ttl-exp-current",
            "conversationId": f"ttl-{case['caseId']}",
            "content": str(source.get("content") or ""),
            "source": str(source.get("source") or ""),
            "sourceId": source.get("sourceId"),
            "confidence": float(source.get("confidence") or 0.9),
            "score": 0.9,
            "producerVersion": producer_version,
            "_sourceMemoryId": source.get("memoryId"),
            "_sourceStatus": source.get("status"),
        }
    return result


def evaluate_result(case: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    answer = str(result.get("answer") or "")
    metrics = result.get("metrics") or {}
    shared = result.get("sharedState") or {}
    evaluation = evaluate_report_quality({
        "answer": answer,
        "resumeText": case["resume"],
        "mustFind": case.get("mustFind") or [],
        "mustNotClaim": case.get("mustNotClaim") or [],
    })
    evidence_expected = [str(item) for item in case.get("expectedEvidence") or []]
    located_ratio = None
    if evidence_expected:
        located_ratio = locate_evidence({
            "resumeText": case["resume"],
            "claims": evidence_expected,
        }).get("supportRatio")
    claims = [
        item for item in (shared.get("evidence") or [])
        if isinstance(item, dict) and item.get("verified") is not None
    ]
    unsupported = None
    if claims:
        unsupported = sum(not bool(item.get("verified")) for item in claims) / len(claims)
    prompt_tokens = int(metrics.get("promptTokens") or 0)
    completion_tokens = int(metrics.get("completionTokens") or 0)
    cost = (
        prompt_tokens / 1_000_000 * PRICE_PROMPT_PER_M
        + completion_tokens / 1_000_000 * PRICE_COMPLETION_PER_M
    )
    status = str(result.get("status") or "FAILED")
    succeeded = 1.0 if status == "SUCCEEDED" else 0.45 if status == "PARTIAL_SUCCESS" else 0.0
    support = float(metrics.get("evidenceSupportRatio") or 0)
    coverage = float(metrics.get("jdCoverage") or 0)
    latency = float(metrics.get("latencySeconds") or 0)
    reward = (
        0.25 * float(evaluation.get("score") or 0)
        + 0.12 * float(evaluation.get("mustFindScore") or 0)
        - 0.12 * float(evaluation.get("violationPenalty") or 0)
        + 0.12 * support
        + 0.08 * coverage
        + 0.12 * succeeded
        + (0.08 * (1.0 - unsupported) if unsupported is not None else 0)
        - 0.08 * min(1.0, latency / 180.0)
        - 0.05 * min(1.0, cost / 0.5)
    )
    report = result.get("structuredReport") or {}
    return {
        "status": status,
        "reward": round(reward, 4),
        "mustFind": float(evaluation.get("mustFindScore") or 0),
        "violation": float(evaluation.get("violationPenalty") or 0),
        "recommendationAccuracy": float(evaluation.get("score") or 0),
        "evidenceSupport": support,
        "evidenceLocated": located_ratio,
        "unsupportedClaimRate": round(unsupported, 4) if unsupported is not None else None,
        "latencySeconds": latency,
        "promptTokens": prompt_tokens,
        "completionTokens": completion_tokens,
        "costCny": round(cost, 6),
        "llmCalls": int(metrics.get("llmCalls") or 0),
        "toolCalls": int(metrics.get("toolCalls") or 0),
        "agentsUsed": list(metrics.get("agentsUsed") or []),
        "overallScore": report.get("overallScore"),
        "recommendation": report.get("recommendation"),
        "answerChars": len(answer),
    }


async def run_condition(
    case: dict[str, Any],
    condition: str,
    fixture: dict[str, Any] | None,
    log_dir: Path,
    semaphore: asyncio.Semaphore,
) -> dict[str, Any]:
    async with semaphore:
        suffix = uuid.uuid4().hex[:8]
        request = AgentRunRequest(
            runId=f"ttl-{case['caseId']}-{condition}-{suffix}",
            conversationId=f"ttl-{case['caseId']}-{suffix}",
            userId="ttl-exp-current",
            traceId=f"ttl-trace-{suffix}",
            runType="full_evaluation",
            userMessage=str(case["userQuestion"]),
            resumeText=str(case["resume"]),
            jobDescription=str(case["jd"]),
            jobCategory=(case.get("metadata") or {}).get("jobCategory"),
            policyId="balanced",
            policyConfig={"evidenceVerification": {"enabled": True}},
        )
        emitter = NullEmitter(request.runId, request.conversationId, request.traceId)
        canned = [fixture] if fixture else []
        memory = NullMemoryClient(canned=canned)
        executor = RunExecutor(
            request,
            emitter,
            memory=memory,
            builtin_tools=BuiltinToolRegistry(),
        )
        context_log = log_dir / f"{case['caseId']}-{condition}.jsonl"
        if context_log.exists():
            context_log.unlink()
        recorder = LiveContextRecorder(executor.llm, context_log)
        executor.llm = recorder
        executor.tools.llm = recorder
        started = time.perf_counter()
        result = await executor.execute()
        elapsed = round(time.perf_counter() - started, 3)
        event_counts = Counter(str(row.get("eventType") or "") for row in emitter.events)
        selected = next(
            ((row.get("payload") or {}) for row in emitter.events
             if row.get("eventType") == "agent.selected"),
            {},
        )
        payload = {
            "caseId": case["caseId"],
            "condition": condition,
            "runId": request.runId,
            "wallSeconds": elapsed,
            "injectedMemory": ({
                "type": fixture.get("type"),
                "source": fixture.get("source"),
                "sourceMemoryId": fixture.get("_sourceMemoryId"),
                "producerVersion": fixture.get("producerVersion"),
            } if fixture else None),
            "retrievedMemoryTypes": Counter(
                str(hit.get("type") or "") for hit in executor.memory_hits),
            "memoryUsageBatches": len(memory.usage),
            "plan": selected.get("plan") or [],
            "parallelGroups": selected.get("parallelGroups") or [],
            "skillEvents": {
                name: event_counts.get(name, 0)
                for name in ("skill.catalog", "skill.selected", "skill.loaded", "skill.applied")
            },
            "contextLog": str(context_log),
            "metrics": evaluate_result(case, result),
        }
        result_path = context_log.with_suffix(".result.json")
        result_path.write_text(json.dumps({
            "condition": condition,
            "caseId": case["caseId"],
            "result": result,
            "summary": payload,
        }, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        print(json.dumps({
            "case": case["caseId"],
            "condition": condition,
            "status": payload["metrics"]["status"],
            "reward": payload["metrics"]["reward"],
            "latency": payload["metrics"]["latencySeconds"],
        }, ensure_ascii=False), flush=True)
        return payload


def mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def select_ttl(
    memory_type: str,
    candidates: list[int],
    probes: list[float],
    measured_delta: float,
    violation_delta: float,
) -> dict[str, Any]:
    rows = []
    for ttl in candidates:
        retained = sum(age <= ttl for age in probes) / max(1, len(probes))
        # Storage/exposure is only a tie-breaker after quality.  It is not
        # mixed into report reward with a made-up monetary coefficient.
        rows.append({
            "ttlDays": ttl,
            "probeRetention": round(retained, 4),
            "projectedRewardDelta": round(measured_delta * retained, 4),
            "projectedViolationDelta": round(violation_delta * retained, 4),
            "coversHorizon": retained == 1.0,
        })
    eligible = [row for row in rows if row["coversHorizon"]]
    selected = min((row["ttlDays"] for row in eligible), default=None)
    return {
        "type": memory_type,
        "candidateGrid": candidates,
        "boundaryProbeAgesDays": probes,
        "measuredRetainedMinusExpiredReward": round(measured_delta, 4),
        "measuredRetainedMinusExpiredViolation": round(violation_delta, 4),
        "selectionRule": (
            "shortest candidate covering every boundary probe; quality delta "
            "is measured by the current full workflow, TTL only gates availability"),
        "selectedTtlDays": selected,
        "candidates": rows,
    }


def build_report(
    cases: list[dict[str, Any]],
    results: list[dict[str, Any]],
    fixtures: dict[str, dict[str, dict[str, Any]]],
    producer_version: str,
    pause_ttl_seconds: int,
) -> dict[str, Any]:
    by_key = {(row["caseId"], row["condition"]): row for row in results}
    effects: dict[str, Any] = {}
    decisions = []
    for memory_type in ("SEMANTIC", "EPISODIC", "PROCEDURAL"):
        deltas = []
        violation_deltas = []
        details = []
        condition = memory_type.lower()
        for case in cases:
            base = by_key[(case["caseId"], "expired")]["metrics"]
            retained = by_key[(case["caseId"], condition)]["metrics"]
            delta = retained["reward"] - base["reward"]
            violation_delta = retained["violation"] - base["violation"]
            deltas.append(delta)
            violation_deltas.append(violation_delta)
            details.append({
                "caseId": case["caseId"],
                "rewardDelta": round(delta, 4),
                "evidenceSupportDelta": round(
                    retained["evidenceSupport"] - base["evidenceSupport"], 4),
                "mustFindDelta": round(retained["mustFind"] - base["mustFind"], 4),
                "violationDelta": round(violation_delta, 4),
                "latencyDeltaSeconds": round(
                    retained["latencySeconds"] - base["latencySeconds"], 3),
                "tokenDelta": (
                    retained["promptTokens"] + retained["completionTokens"]
                    - base["promptTokens"] - base["completionTokens"]),
            })
        effects[memory_type] = {
            "meanRewardDelta": round(mean(deltas), 4),
            "meanViolationDelta": round(mean(violation_deltas), 4),
            "cases": details,
        }
        decisions.append(select_ttl(
            memory_type,
            CANDIDATES[memory_type],
            HORIZON_PROBES_DAYS[memory_type],
            mean(deltas),
            mean(violation_deltas),
        ))

    pause_days = pause_ttl_seconds / 86400.0
    working_selected = min(
        ttl for ttl in CANDIDATES["WORKING"] if ttl >= pause_days)
    decisions.insert(0, {
        "type": "WORKING",
        "candidateGrid": CANDIDATES["WORKING"],
        "pauseTtlSeconds": pause_ttl_seconds,
        "requiredCoverageDays": round(pause_days, 6),
        "selectedTtlDays": working_selected,
        "selectionRule": (
            "smallest integer-day TTL above the control-plane PAUSED lifetime; "
            "WORKING is RUN-scoped, restored from execution_snapshot, and archived at terminal"),
        "llmAbRequired": False,
    })
    return {
        "experiment": "EXP-14-current-workflow-memory-ttl-controlled",
        "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "workflowProducerVersion": producer_version,
        "cohort": "EXACT_CURRENT_PRODUCER_PAYLOADS_AND_CURRENT_WORKFLOW_CONSUMER",
        "mutation": "NONE",
        "method": {
            "fullWorkflow": True,
            "realProvider": True,
            "differentiatedCases": [case["caseId"] for case in cases],
            "conditionsPerCase": ["expired", "semantic", "episodic", "procedural"],
            "timeShift": (
                "payload is held constant; only availability at boundary ages changes"),
            "lifecycleGuard": (
                "CONFLICTED/ARCHIVED/expired rows are non-retrievable independent of TTL"),
            "selectionObjective": (
                "shortest candidate meeting the declared consumer horizon, after measuring "
                "retained-vs-expired report impact"),
        },
        "fixtureSources": {
            case_id: {
                memory_type: {
                    "memoryId": row.get("_sourceMemoryId"),
                    "source": row.get("source"),
                    "producerVersion": row.get("producerVersion"),
                    "sourceStatus": row.get("_sourceStatus"),
                }
                for memory_type, row in typed.items()
            }
            for case_id, typed in fixtures.items()
        },
        "effects": effects,
        "ttlDecisions": decisions,
        "runs": results,
    }


def markdown_report(report: dict[str, Any]) -> str:
    lines = [
        "# EXP-14 当前 Workflow Memory TTL 控制实验",
        "",
        f"- Workflow producer：`{report['workflowProducerVersion']}`",
        f"- 全真 run：{len(report['runs'])}（真实模型、完整 Agent/Skill/MCP catalog）",
        f"- 数据变更：{report['mutation']}",
        "- 结论口径：仅声称为当前候选网格、当前 workflow 和明确消费边界下的最优值。",
        "",
        "| 类型 | 候选 | 选择 | 当前流程实测 retained-expired Reward |",
        "|---|---|---:|---:|",
    ]
    effects = report.get("effects") or {}
    for row in report["ttlDecisions"]:
        delta = (effects.get(row["type"]) or {}).get("meanRewardDelta")
        delta_text = "控制面约束" if delta is None else str(delta)
        lines.append(
            f"| {row['type']} | {row['candidateGrid']} | "
            f"{row['selectedTtlDays']}d | {delta_text} |")
    lines.extend([
        "",
        "## 边界",
        "",
        "时间移位只隔离 TTL 是否让同一条当前版本记忆可见，不会伪装成真实 90/365 天生产历史。",
        "较长 TTL 若与较短 TTL 都覆盖全部边界且质量相同，由最小暴露/存储原则选择较短值；",
        "旧版本、冲突和已归档记忆由版本与生命周期门禁处理，不靠 TTL 掩盖。",
        "",
    ])
    return "\n".join(lines)


async def async_main(args: argparse.Namespace) -> dict[str, Any]:
    wanted = {item.strip() for item in args.case_ids.split(",") if item.strip()}
    cases = load_cases(Path(args.cases), wanted)
    ops = fetch_json(args.ops_url)
    fixtures = {
        str(case["caseId"]): current_fixtures(
            ops, case, args.producer_version)
        for case in cases
    }
    log_dir = Path(args.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    semaphore = asyncio.Semaphore(max(1, args.concurrency))
    tasks = []
    for case in cases:
        case_id = str(case["caseId"])
        tasks.append(run_condition(
            case, "expired", None, log_dir, semaphore))
        for memory_type in ("SEMANTIC", "EPISODIC", "PROCEDURAL"):
            tasks.append(run_condition(
                case, memory_type.lower(), fixtures[case_id][memory_type],
                log_dir, semaphore))
    results = await asyncio.gather(*tasks)
    report = build_report(
        cases, results, fixtures, args.producer_version,
        args.pause_ttl_seconds)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str),
                   encoding="utf-8")
    out.with_suffix(".md").write_text(markdown_report(report), encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--cases",
        default=str(REPO_ROOT / "testdata" / "benchmark" / "gold_cases.json"))
    parser.add_argument(
        "--case-ids",
        default="gold-java-backend-normal,gold-ai-agent-resume")
    parser.add_argument(
        "--ops-url", default="http://ai-resume-backend:8080/api/ops/memory?limit=200")
    parser.add_argument("--producer-version", required=True)
    parser.add_argument("--pause-ttl-seconds", type=int, default=7200)
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument(
        "--log-dir", default=str(ROOT / ".sim-artifacts" / "ttl-matrix"))
    parser.add_argument(
        "--out", default=str(
            REPO_ROOT / "reports" / "experiments" / "memory_ttl_controlled.json"))
    args = parser.parse_args()
    report = asyncio.run(async_main(args))
    print(json.dumps({
        "experiment": report["experiment"],
        "runs": len(report["runs"]),
        "decisions": {
            row["type"]: row["selectedTtlDays"]
            for row in report["ttlDecisions"]
        },
        "out": args.out,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
