"""Unit tests for evidence-bound structured report validation."""
from __future__ import annotations

import sys
from pathlib import Path

WORKFLOW_ROOT = Path(__file__).resolve().parents[1]
if str(WORKFLOW_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKFLOW_ROOT))

from app.runtime.executor import RunExecutor


def _ref(quote: str = "Kafka 峰值 5000 QPS", source_type: str = "RESUME") -> dict:
    return {
        "sourceType": source_type,
        "sourceId": "resume" if source_type == "RESUME" else "jd",
        "quote": quote,
        "lineStart": 1,
        "lineEnd": 1,
    }


def _assessed_dims(n: int = 4) -> list:
    names = ["技术能力", "项目深度", "JD匹配", "履历可信度"]
    return [
        {
            "name": names[i],
            "score": 70 + i,
            "status": "ASSESSED",
            "evidenceCoverage": 0.8,
            "rationale": f"{names[i]}有证据",
            "evidenceRefs": [_ref(f"{names[i]}证据")],
        }
        for i in range(n)
    ]


def test_unassessed_keeps_null_score_not_zero():
    report = {
        "recommendation": "NEED_MANUAL_REVIEW",
        "dataQuality": "INSUFFICIENT",
        "dimensions": [
            {
                "name": "技术能力",
                "score": 0,
                "status": "UNASSESSED",
                "rationale": "原文不足",
                "evidenceRefs": [],
            }
        ],
        "strengths": [],
        "risks": [],
        "interviewQuestions": [],
    }
    out = RunExecutor._validate_structured_report(report)
    assert out is not None
    dim = out["dimensions"][0]
    assert dim["status"] == "UNASSESSED"
    assert dim["score"] is None
    assert "overallScore" not in out


def test_insufficient_not_promoted_to_partial_by_low_scores():
    report = {
        "recommendation": "NOT_RECOMMEND",
        "dataQuality": "INSUFFICIENT",
        "dimensions": [
            {
                "name": "技术能力",
                "score": 20,
                "status": "ASSESSED",
                "evidenceCoverage": 0.9,
                "rationale": "低分但仍有证据",
                "evidenceRefs": [_ref()],
            },
            {
                "name": "项目深度",
                "score": 15,
                "status": "ASSESSED",
                "evidenceCoverage": 0.9,
                "rationale": "低分",
                "evidenceRefs": [_ref("项目")],
            },
        ],
        "strengths": [],
        "risks": [],
        "interviewQuestions": [],
    }
    out = RunExecutor._validate_structured_report(report)
    assert out is not None
    assert out["dataQuality"] == "INSUFFICIENT"
    # 2 core dims now suffice for overallScore computation
    assert isinstance(out.get("overallScore"), int)


def test_overall_score_requires_three_core_dims_with_evidence():
    base = {
        "recommendation": "INTERVIEW_RECOMMEND",
        "dataQuality": "SUFFICIENT",
        "strengths": ["扎实"],
        "risks": [],
        "interviewQuestions": [],
    }
    two = RunExecutor._validate_structured_report({**base, "dimensions": _assessed_dims(2)})
    assert two is not None
    assert isinstance(two.get("overallScore"), int)

    three = RunExecutor._validate_structured_report({**base, "dimensions": _assessed_dims(3)})
    assert three is not None
    assert isinstance(three.get("overallScore"), int)


def test_risks_without_evidence_rejected():
    report = {
        "recommendation": "NEED_MANUAL_REVIEW",
        "dataQuality": "PARTIAL",
        "dimensions": _assessed_dims(3),
        "strengths": [],
        "risks": [
            "裸字符串风险应被拒绝",
            {
                "id": "r1",
                "category": "CANDIDATE",
                "severity": "HIGH",
                "claim": "无证据风险",
                "evidenceRefs": [],
            },
            {
                "id": "r2",
                "category": "CANDIDATE",
                "severity": "MEDIUM",
                "claim": "时间线重叠",
                "impact": "需核实",
                "verificationPlan": "追问任职安排",
                "evidenceRefs": [_ref("2022.07-2024.06 与 2024.03 重叠")],
            },
        ],
        "interviewQuestions": [],
    }
    out = RunExecutor._validate_structured_report(report)
    assert out is not None
    risks = out.get("risks") or []
    # r1 (no evidence) is now kept (relaxed policy); bare string still rejected
    assert len(risks) == 2
    assert risks[0]["id"] == "r1"
    assert risks[1]["id"] == "r2"
    assert risks[1].get("evidenceRefs")


def test_control_plane_and_process_go_to_system_warnings():
    report = {
        "recommendation": "NEED_MANUAL_REVIEW",
        "dataQuality": "PARTIAL",
        "dimensions": _assessed_dims(3),
        "strengths": [],
        "risks": [
            {
                "id": "bad",
                "category": "CANDIDATE",
                "severity": "HIGH",
                "claim": "ORPHANED_ON_RESTART caused incomplete analysis",
                "evidenceRefs": [_ref("假证据也不行")],
            },
            {
                "id": "proc",
                "category": "PROCESS",
                "severity": "LOW",
                "claim": "工具超时导致覆盖不全",
                "evidenceRefs": [_ref()],
            },
            {
                "id": "ok",
                "category": "CANDIDATE",
                "severity": "MEDIUM",
                "claim": "量化指标缺少压测细节",
                "evidenceRefs": [_ref()],
            },
        ],
        "interviewQuestions": [],
        "systemWarnings": [
            {
                "code": "TOOL_TIMEOUT",
                "stage": "TechAgent",
                "retryable": True,
                "message": "calculate_jd_coverage timed out",
            }
        ],
    }
    out = RunExecutor._validate_structured_report(report)
    assert out is not None
    risks = out.get("risks") or []
    assert len(risks) == 1
    assert risks[0]["id"] == "ok"
    warnings = out.get("systemWarnings") or []
    codes = {w["code"] for w in warnings}
    assert "CONTROL_PLANE_IN_RISK_CLAIM" in codes
    assert "proc" in codes or "PROCESS" in codes
    assert "TOOL_TIMEOUT" in codes


def test_interview_probes_require_evidence_and_good_signals():
    report = {
        "recommendation": "INTERVIEW_RECOMMEND",
        "dataQuality": "SUFFICIENT",
        "dimensions": _assessed_dims(3),
        "strengths": [],
        "risks": [],
        "interviewQuestions": [
            "请介绍一下 RAG",
            {
                "id": "q-bad",
                "question": "缺信号的题",
                "evidenceRefs": [_ref()],
                "goodSignals": [],
            },
            {
                "id": "q-ok",
                "priority": "HIGH",
                "question": "订单中台峰值如何压测？",
                "objective": "验证 QPS",
                "triggeredBy": "5000 QPS",
                "evidenceRefs": [_ref()],
                "goodSignals": ["能说明压测工具"],
                "redFlags": ["只会背数字"],
                "followUps": ["分区策略？"],
                "scoreRubric": "方法清晰记高分",
            },
        ],
    }
    out = RunExecutor._validate_structured_report(report)
    assert out is not None
    probes = out.get("interviewProbes") or out.get("interviewQuestions") or []
    # Relaxed: probes with question are kept even without goodSignals;
    # bare strings still rejected
    assert len(probes) == 2
    assert probes[0]["id"] == "q-bad"
    assert probes[1]["id"] == "q-ok"
    assert probes[1]["goodSignals"]
