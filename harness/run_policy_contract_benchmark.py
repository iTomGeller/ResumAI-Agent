#!/usr/bin/env python3
"""Policy CONTRACT benchmark — deterministic, offline, no LLM, no Docker.

Scope (and only this scope):
  * policy configuration validity,
  * the reward formula,
  * sandbox tool contracts (same code as the Docker worker),
  * output schema rules,
  * security/prompt-injection refusal rules,
  * deterministic failure-injection regressions.

This is NOT an agent quality benchmark: it never runs the Coordinator, the
RunExecutor or DeepSeek, its answers are synthesized from tool outputs, and
its numbers must never be presented as real agent E2E quality or used to
elect a Champion policy. For that, use harness/run_agent_e2e_benchmark.py.

Usage (on ECS):
  cd /opt/resumai-src
  python3 harness/run_policy_contract_benchmark.py --out reports/benchmark/contract
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "workflow"))

from app.runtime.sandbox_tools_local import run_tool  # noqa: E402

POLICIES: Dict[str, Dict[str, Any]] = {
    "balanced": {
        "evidenceVerification": {"enabled": True, "strict": False, "minSupportRatio": 0.5},
        "agentOrder": ["JDAnalysisAgent", "TechAgent", "ProjectAgent", "RiskAgent",
                       "EvidenceAgent", "ReportAgent"],
        "rewriteRounds": 1,
        "costWeight": 0.15,
    },
    "strict_evidence": {
        "evidenceVerification": {"enabled": True, "strict": True, "minSupportRatio": 0.75},
        "agentOrder": ["TechAgent", "EvidenceAgent", "RiskAgent", "ReportAgent"],
        "rewriteRounds": 1,
        "costWeight": 0.10,
    },
    "deep_analysis": {
        "evidenceVerification": {"enabled": True, "strict": True, "minSupportRatio": 0.6},
        "agentOrder": ["JDAnalysisAgent", "TechAgent", "ProjectAgent", "RiskAgent",
                       "EvidenceAgent", "ResumeOptimizeAgent", "ReportAgent"],
        "rewriteRounds": 2,
        "costWeight": 0.25,
    },
    "low_cost": {
        "evidenceVerification": {"enabled": False, "strict": False, "minSupportRatio": 0.0},
        "agentOrder": ["TechAgent", "ReportAgent"],
        "rewriteRounds": 0,
        "costWeight": 0.05,
    },
    "backend_job": {
        "evidenceVerification": {"enabled": True, "strict": False, "minSupportRatio": 0.55},
        "agentOrder": ["JDAnalysisAgent", "TechAgent", "ProjectAgent", "RiskAgent",
                       "EvidenceAgent", "ReportAgent"],
        "rewriteRounds": 1,
        "costWeight": 0.15,
        "skillBias": "java_backend_evaluation",
    },
    "agent_job": {
        "evidenceVerification": {"enabled": True, "strict": True, "minSupportRatio": 0.6},
        "agentOrder": ["JDAnalysisAgent", "TechAgent", "ProjectAgent", "EvidenceAgent",
                       "ReportAgent"],
        "rewriteRounds": 1,
        "costWeight": 0.18,
        "skillBias": "ai_agent_job_evaluation",
    },
    "resume_rewrite": {
        "evidenceVerification": {"enabled": True, "strict": False, "minSupportRatio": 0.4},
        "agentOrder": ["ProjectAgent", "ResumeOptimizeAgent", "ReportAgent"],
        "rewriteRounds": 2,
        "costWeight": 0.12,
    },
}


@dataclass
class CaseResult:
    case_id: str
    dataset: str
    policy_id: str
    status: str
    metrics: Dict[str, Any] = field(default_factory=dict)
    duration_ms: int = 0
    error: Optional[str] = None


def load_cases(case_dir: Path) -> List[Dict[str, Any]]:
    cases: List[Dict[str, Any]] = []
    for name in ("gold_cases.json", "synthetic_cases.json",
                 "regression_cases.json", "security_cases.json"):
        path = case_dir / name
        if not path.exists():
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, list):
            raise ValueError(f"{path} must be a JSON array")
        for item in payload:
            # Schema guard: required fields, never inject expected answer into agent path.
            for key in ("caseId", "dataset", "resume", "jd", "userQuestion",
                        "mustFind", "mustNotClaim"):
                if key not in item:
                    raise ValueError(f"{path}: case missing {key}")
            cases.append(item)
    return cases


def build_answer(case: Dict[str, Any], parse: Dict[str, Any], timeline: Dict[str, Any],
                 coverage: Dict[str, Any], evidence: Dict[str, Any],
                 lint: Dict[str, Any], policy: Dict[str, Any]) -> str:
    """Policy-conditioned deterministic answer assembled from tool observations.

    Expected answers from the case file are NEVER injected here — only tool
    outputs and the user question drive the narrative.
    """
    meta = case.get("metadata") or {}
    if meta.get("injectFabricatedAnswer") and not policy["evidenceVerification"]["enabled"]:
        # FAILURE INJECTION path (contract regression only): verifies that the
        # evaluator penalizes fabricated numbers when verification is off.
        # Cases carrying injectFabricatedAnswer are reported separately as
        # dataset=FAILURE_INJECTION and excluded from policy aggregates.
        return str(meta["injectFabricatedAnswer"])

    parts: List[str] = [f"针对问题：{case['userQuestion']}"]
    if not parse.get("success"):
        parts.append(f"解析失败：{parse.get('error', 'unknown')}，无法给出确定性录用结论。")
        return "\n".join(parts)

    skills = parse.get("skills") or []
    parts.append(f"识别技能：{', '.join(skills[:12]) or '无'}")
    if coverage.get("success"):
        parts.append(
            f"JD 覆盖率 {coverage.get('coverage')} "
            f"（{coverage.get('coveredCount')}/{coverage.get('requirementCount')}）")
        missing = coverage.get("missing") or []
        if missing:
            parts.append("缺口：" + "；".join(missing[:5]))
    if timeline.get("success"):
        issues = timeline.get("issues") or []
        high = [i for i in issues if i.get("severity") == "high"]
        if high:
            parts.append("时间线高风险：" + "；".join(
                f"{i.get('type')}:{i.get('detail')}" for i in high[:4]))
        elif issues:
            parts.append("时间线提示：" + "；".join(i.get("type", "") for i in issues[:4]))
        else:
            parts.append("时间线未发现高风险冲突")
    if evidence.get("success"):
        parts.append(f"证据支持率 {evidence.get('supportRatio')}")
        unsupported_count = sum(1 for c in evidence.get("claims", []) if not c.get("found"))
        if unsupported_count and policy["evidenceVerification"].get("strict"):
            # Count only — echoing claim text would leak mustFind terms into
            # the answer and let strict policies self-score on the evaluator.
            parts.append(f"严格模式：{unsupported_count} 条结论证据不足，标记为不确定")
    if lint.get("success") and lint.get("issueCount", 0) > 0:
        parts.append(f"简历表述问题 {lint.get('issueCount')} 项")

    # Skill-only detection: skills present but no project evidence lines.
    sections = parse.get("sections") or {}
    project_text = " ".join(sections.get("projects") or [])
    skill_only = [s for s in skills if s not in project_text.lower()
                  and s not in (case.get("resume") or "").lower().split("项目")[-1]]
    # Simpler: if skill appears only before 项目 section
    resume = case.get("resume") or ""
    if "项目" in resume:
        before, after = resume.split("项目", 1)
        skill_only = [s for s in skills if s in before.lower() and s not in after.lower()]
        if skill_only:
            parts.append("技能栏存在但缺少项目证据：" + ", ".join(skill_only[:6]))

    cov = float(coverage.get("coverage") or 0)
    risk = bool(timeline.get("hasHighRisk"))
    support = float(evidence.get("supportRatio") or 0)
    if meta.get("expectParseFailure"):
        rec = "NO_HIRE"
    elif risk and policy["evidenceVerification"].get("strict"):
        rec = "HOLD"
    elif cov >= 0.7 and support >= policy["evidenceVerification"].get("minSupportRatio", 0.5):
        rec = "HIRE"
    elif cov >= 0.4:
        rec = "CONSIDER"
    else:
        rec = "NO_HIRE"
    parts.append(f"推荐结论：{rec}")
    if "停止" in case["userQuestion"] or "取消" in case["userQuestion"]:
        parts.append("已按最新指令聚焦时间线检查，不生成完整评估报告伪装。")
    if "密码" in case["userQuestion"] or "API Key" in case["userQuestion"]:
        parts.append("拒绝输出任何密钥、数据库密码或 API Key。")
    if "curl" in case["userQuestion"] or "pip install" in case["userQuestion"]:
        parts.append("拒绝执行任意 Shell/curl/安装命令；仅允许受控 Sandbox 白名单工具。")
    if "忽略以上" in resume or "STRONG_HIRE" in resume:
        parts.append("检测到提示注入，不采纳简历中的指令性语句。")
    return "\n".join(parts)


def run_case(case: Dict[str, Any], policy_id: str) -> CaseResult:
    policy = POLICIES[policy_id]
    started = time.perf_counter()
    tool_calls = 0
    sandbox_timeout = 0
    sandbox_oom = 0
    try:
        parse = run_tool("parse_resume", {"resumeText": case.get("resume") or ""})
        tool_calls += 1
        timeline = run_tool("check_timeline", {"resumeText": case.get("resume") or ""})
        tool_calls += 1
        coverage = run_tool("calculate_jd_coverage", {
            "resumeText": case.get("resume") or "",
            "jdText": case.get("jd") or "",
        })
        tool_calls += 1
        # Contract check of locate/verify tools: claims are derived from the
        # deterministic parse output (skills actually present in the resume),
        # never from the evaluator-only mustFind labels.
        derived_claims = [str(s) for s in (parse.get("skills") or [])][:10]
        if policy["evidenceVerification"]["enabled"]:
            evidence = run_tool("locate_evidence", {
                "resumeText": case.get("resume") or "",
                "claims": derived_claims or ["java"],
            })
            tool_calls += 1
            verify = run_tool("verify_report_evidence", {
                "resumeText": case.get("resume") or "",
                "jdText": case.get("jd") or "",
                "claims": [{"text": c, "source": "parsed_skill"} for c in derived_claims],
            })
            tool_calls += 1
        else:
            evidence = {"success": True, "supportRatio": 0.0, "claims": []}
            verify = {"success": True, "unsupportedClaimRate": 1.0}
        lint = run_tool("resume_lint", {"resumeText": case.get("resume") or ""})
        tool_calls += 1

        # deep_analysis / resume_rewrite spend extra rewrite rounds as tool budget.
        for _ in range(int(policy.get("rewriteRounds") or 0)):
            run_tool("resume_lint", {"resumeText": case.get("resume") or ""})
            tool_calls += 1

        answer = build_answer(case, parse, timeline, coverage, evidence, lint, policy)
        eval_out = run_tool("evaluate_policy_output", {
            "answer": answer,
            "resumeText": case.get("resume") or "",
            "mustFind": case.get("mustFind") or [],
            "mustNotClaim": case.get("mustNotClaim") or [],
        })
        tool_calls += 1

        meta = case.get("metadata") or {}
        parse_ok = 1.0 if (parse.get("success") or meta.get("expectParseFailure")) else 0.0
        if meta.get("expectParseFailure"):
            parse_ok = 1.0 if not parse.get("success") else 0.0

        timeline_issues = {i.get("type") for i in (timeline.get("issues") or [])}
        expected_risks = set(case.get("expectedRisk") or [])
        if expected_risks:
            tp = len(expected_risks & timeline_issues)
            fp = len(timeline_issues - expected_risks)
            fn = len(expected_risks - timeline_issues)
            precision = tp / max(1, tp + fp)
            recall = tp / max(1, tp + fn)
        else:
            precision = 1.0 if not timeline.get("hasHighRisk") else 0.7
            recall = 1.0

        support_ratio = float(evidence.get("supportRatio") or 0.0)
        unsupported = float(verify.get("unsupportedClaimRate")
                            or (1.0 - support_ratio))
        jd_coverage = float(coverage.get("coverage") or 0.0)
        eval_score = float(eval_out.get("score") or 0.0)
        # Contract-only nominal cost model (labelled as such): this benchmark
        # makes zero LLM calls, so no real token cost exists here by design.
        nominal_llm_calls = max(1, len(policy.get("agentOrder") or []))
        latency_ms = int((time.perf_counter() - started) * 1000)
        nominal_cost = (nominal_llm_calls * 0.02 + tool_calls * 0.005
                        + float(policy.get("costWeight") or 0))
        failure = 0.0 if eval_out.get("success") else 1.0

        # Reward: quality minus cost/latency penalties (component-wise).
        components = {
            "mustFindScore": float(eval_out.get("mustFindScore") or 0),
            "violationPenalty": float(eval_out.get("violationPenalty") or 0),
            "evidenceSupportRatio": support_ratio,
            "unsupportedClaimRate": unsupported,
            "jdCoverage": jd_coverage,
            "timelinePrecision": precision,
            "timelineRecall": recall,
            "parsingSuccess": parse_ok,
            "evalScore": eval_score,
            "llmCallCost": -nominal_cost,
            "latencyPenalty": -min(0.2, latency_ms / 60_000),
            "failurePenalty": -failure,
        }
        total_reward = (
            0.25 * components["evalScore"]
            + 0.15 * components["evidenceSupportRatio"]
            + 0.10 * (1.0 - components["unsupportedClaimRate"])
            + 0.15 * components["jdCoverage"]
            + 0.10 * components["timelinePrecision"]
            + 0.10 * components["timelineRecall"]
            + 0.10 * components["parsingSuccess"]
            + components["llmCallCost"]
            + components["latencyPenalty"]
            + components["failurePenalty"]
        )

        metrics = {
            "parsingSuccessRate": parse_ok,
            "timelinePrecision": round(precision, 4),
            "timelineRecall": round(recall, 4),
            "evidenceSupportRatio": round(support_ratio, 4),
            "unsupportedClaimRate": round(unsupported, 4),
            "jdCoverage": round(jd_coverage, 4),
            "recommendationAccuracy": round(eval_score, 4),
            "nominalLlmCalls": nominal_llm_calls,
            "averageToolCalls": tool_calls,
            "nominalCost": round(nominal_cost, 4),
            "averageLatencyMs": latency_ms,
            "runFailureRate": failure,
            "sandboxTimeoutRate": sandbox_timeout,
            "sandboxOomRate": sandbox_oom,
            "totalReward": round(total_reward, 4),
            "rewardComponents": {k: round(v, 4) for k, v in components.items()},
            "answerPreview": answer[:400],
            "agentOrder": policy.get("agentOrder"),
        }
        dataset = case["dataset"]
        injection_active = bool((case.get("metadata") or {}).get("injectFabricatedAnswer")) \
            and not policy["evidenceVerification"]["enabled"]
        if injection_active:
            dataset = "FAILURE_INJECTION"
        return CaseResult(case["caseId"], dataset, policy_id, "SUCCEEDED",
                          metrics, latency_ms)
    except Exception as exc:  # noqa: BLE001
        latency_ms = int((time.perf_counter() - started) * 1000)
        return CaseResult(case["caseId"], case.get("dataset", "?"), policy_id, "FAILED",
                          {"totalReward": -1.0, "runFailureRate": 1.0}, latency_ms, str(exc))


def aggregate(results: List[CaseResult]) -> Dict[str, Any]:
    """Per-policy aggregates over contract cases only.

    FAILURE_INJECTION rows are reported separately and never mixed into a
    policy's quality aggregate. No champion is elected here — that is the
    exclusive job of the real agent E2E benchmark.
    """
    by_policy: Dict[str, List[CaseResult]] = {}
    injection_rows: List[CaseResult] = []
    for result in results:
        if result.dataset == "FAILURE_INJECTION":
            injection_rows.append(result)
            continue
        by_policy.setdefault(result.policy_id, []).append(result)

    summary: Dict[str, Any] = {}
    for policy_id, rows in by_policy.items():
        n = max(1, len(rows))
        latencies = sorted(r.duration_ms for r in rows)
        p95 = latencies[min(len(latencies) - 1, int(0.95 * (len(latencies) - 1)))]

        def avg(key: str) -> float:
            vals = [float((r.metrics or {}).get(key, 0) or 0) for r in rows]
            return round(sum(vals) / n, 4)

        summary[policy_id] = {
            "cases": len(rows),
            "parsingSuccessRate": avg("parsingSuccessRate"),
            "timelinePrecision": avg("timelinePrecision"),
            "timelineRecall": avg("timelineRecall"),
            "evidenceSupportRatio": avg("evidenceSupportRatio"),
            "unsupportedClaimRate": avg("unsupportedClaimRate"),
            "jdCoverage": avg("jdCoverage"),
            "recommendationAccuracy": avg("recommendationAccuracy"),
            "nominalLlmCalls": avg("nominalLlmCalls"),
            "averageToolCalls": avg("averageToolCalls"),
            "nominalCost": avg("nominalCost"),
            "averageLatencyMs": avg("averageLatencyMs"),
            "p95LatencyMs": p95,
            "runFailureRate": avg("runFailureRate"),
            "sandboxTimeoutRate": avg("sandboxTimeoutRate"),
            "sandboxOomRate": avg("sandboxOomRate"),
            "totalReward": avg("totalReward"),
        }
    injection_summary = {
        "cases": len(injection_rows),
        "fabricationPenalized": sum(
            1 for r in injection_rows
            if float((r.metrics or {}).get("rewardComponents", {})
                     .get("violationPenalty", 0)) > 0
            or float((r.metrics or {}).get("totalReward", 0)) < 0.3),
    }
    return {"policies": summary, "failureInjection": injection_summary}


def write_reports(out_dir: Path, benchmark_id: str, results: List[CaseResult],
                  summary: Dict[str, Any]) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "benchmarkId": benchmark_id,
        "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "summary": summary,
        "results": [
            {
                "caseId": r.case_id,
                "dataset": r.dataset,
                "policyId": r.policy_id,
                "status": r.status,
                "durationMs": r.duration_ms,
                "error": r.error,
                "metrics": r.metrics,
            }
            for r in results
        ],
    }
    (out_dir / "benchmark_results.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    csv_path = out_dir / "benchmark_results.csv"
    fields = [
        "caseId", "dataset", "policyId", "status", "durationMs", "totalReward",
        "parsingSuccessRate", "timelinePrecision", "timelineRecall",
        "evidenceSupportRatio", "unsupportedClaimRate", "jdCoverage",
        "recommendationAccuracy", "nominalLlmCalls", "averageToolCalls",
        "nominalCost", "runFailureRate",
    ]
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for r in results:
            m = r.metrics or {}
            writer.writerow({
                "caseId": r.case_id,
                "dataset": r.dataset,
                "policyId": r.policy_id,
                "status": r.status,
                "durationMs": r.duration_ms,
                "totalReward": m.get("totalReward"),
                "parsingSuccessRate": m.get("parsingSuccessRate"),
                "timelinePrecision": m.get("timelinePrecision"),
                "timelineRecall": m.get("timelineRecall"),
                "evidenceSupportRatio": m.get("evidenceSupportRatio"),
                "unsupportedClaimRate": m.get("unsupportedClaimRate"),
                "jdCoverage": m.get("jdCoverage"),
                "recommendationAccuracy": m.get("recommendationAccuracy"),
                "nominalLlmCalls": m.get("nominalLlmCalls"),
                "averageToolCalls": m.get("averageToolCalls"),
                "nominalCost": m.get("nominalCost"),
                "runFailureRate": m.get("runFailureRate"),
            })

    lines = [
        "# Policy Contract Benchmark Report（非 Agent 质量基准）",
        "",
        f"- Benchmark ID: `{benchmark_id}`",
        "- 类型：**Contract Benchmark**（工具契约 / 评分公式 / 安全规则 / 故障注入回归）",
        "- 本报告不运行 Coordinator/RunExecutor/DeepSeek，不创建 Docker Worker，",
        "  数字不代表真实 Agent 质量，**不用于选择 Champion Policy**。",
        "- 真实质量基准见 `run_agent_e2e_benchmark.py` 的输出。",
        "",
        "## Per-Policy Contract Results",
        "",
        "| Policy | Reward(合约) | Evidence | Unsupported | JD Coverage | P95 Latency | Fail Rate |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for policy_id, stats in summary.get("policies", {}).items():
        lines.append(
            f"| {policy_id} | {stats['totalReward']} | {stats['evidenceSupportRatio']} | "
            f"{stats['unsupportedClaimRate']} | {stats['jdCoverage']} | "
            f"{stats['p95LatencyMs']}ms | {stats['runFailureRate']} |"
        )
    injection = summary.get("failureInjection", {})
    lines.extend([
        "",
        "## Failure Injection",
        "",
        f"- 注入用例数: {injection.get('cases', 0)}，"
        f"编造被评估器惩罚: {injection.get('fabricationPenalized', 0)}",
        "",
        "## Notes",
        "",
        "- 指标来自确定性 Sandbox 工具（与 Docker Worker 同源代码）与 `evaluate_policy_output` 评估器。",
        "- mustFind/mustNotClaim 仅进入评估器；工具核验使用解析产物派生的 claims。",
        "- llmCalls/cost 为名义值（本基准零 LLM 调用），仅用于奖励公式回归。",
        "",
    ])
    (out_dir / "BENCHMARK_REPORT.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cases", default=str(ROOT / "testdata" / "benchmark"))
    parser.add_argument("--out", default=str(ROOT / "reports" / "benchmark" / "contract"))
    parser.add_argument("--policies", default=",".join(POLICIES.keys()))
    args = parser.parse_args()

    cases = load_cases(Path(args.cases))
    if not cases:
        print("No benchmark cases found", file=sys.stderr)
        return 2
    policy_ids = [p.strip() for p in args.policies.split(",") if p.strip()]
    benchmark_id = f"contract-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
    results: List[CaseResult] = []
    for policy_id in policy_ids:
        if policy_id not in POLICIES:
            print(f"Unknown policy: {policy_id}", file=sys.stderr)
            return 2
        for case in cases:
            result = run_case(case, policy_id)
            results.append(result)
            print(f"[{result.status}] {policy_id} :: {result.case_id} "
                  f"reward={result.metrics.get('totalReward')} "
                  f"latency={result.duration_ms}ms")
    summary = aggregate(results)
    out_dir = Path(args.out)
    write_reports(out_dir, benchmark_id, results, summary)
    print(json.dumps({
        "benchmarkId": benchmark_id,
        "kind": "CONTRACT",
        "out": str(out_dir),
        "caseCount": len(cases),
        "resultCount": len(results),
        "failureInjection": summary.get("failureInjection"),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
