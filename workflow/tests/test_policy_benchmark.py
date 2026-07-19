"""Contract-benchmark smoke tests (repo-root harness).

Inside the workflow Docker image the repo-root ``harness/`` package and
``testdata/`` are not shipped, so this module skips itself there instead of
failing the image build.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CASE_DIR = ROOT / "testdata" / "benchmark"

if not (ROOT / "harness" / "run_policy_contract_benchmark.py").exists() \
        or not CASE_DIR.exists():
    pytest.skip("repo-root harness/testdata not present in this environment",
                allow_module_level=True)

sys.path.insert(0, str(ROOT))

from harness.run_policy_contract_benchmark import (  # noqa: E402
    aggregate,
    load_cases,
    run_case,
)


def test_load_cases_schema():
    cases = load_cases(CASE_DIR)
    assert len(cases) >= 10
    for case in cases:
        assert case["caseId"]
        assert case["dataset"] in {"GOLD", "SYNTHETIC", "REGRESSION", "SECURITY"}
        assert "mustFind" in case and "mustNotClaim" in case


def test_contract_run_and_aggregate_without_champion():
    cases = load_cases(CASE_DIR)
    sample = [c for c in cases if c["caseId"] == "gold-java-backend-normal"][0]
    results = [run_case(sample, pid) for pid in ("balanced", "strict_evidence", "low_cost")]
    assert all(r.status == "SUCCEEDED" for r in results)
    summary = aggregate(results)
    # Contract benchmark must NOT elect a champion — that is E2E-only.
    assert "championPolicy" not in summary
    assert "policies" in summary and "failureInjection" in summary


def test_failure_injection_separated_from_policy_aggregates():
    cases = load_cases(CASE_DIR)
    injected = [c for c in cases
                if (c.get("metadata") or {}).get("injectFabricatedAnswer")]
    assert injected, "gold set keeps at least one failure-injection case"
    results = [run_case(injected[0], "low_cost"), run_case(injected[0], "balanced")]
    summary = aggregate(results)
    injection_cases = summary["failureInjection"]["cases"]
    assert injection_cases >= 1
    # low_cost (verification off) produces the fabricated answer → dataset
    # reclassified; balanced answers from tools → stays in its aggregate.
    assert summary["policies"].get("balanced", {}).get("cases", 0) >= 1


def test_expected_answer_not_leaked_into_answer():
    """mustFind is evaluator-only; build_answer must not echo it blindly."""
    case = {
        "caseId": "t",
        "dataset": "GOLD",
        "resume": "技能：Java\n项目：Demo",
        "jd": "Java",
        "userQuestion": "评估",
        "mustFind": ["SECRET_EXPECTED_TOKEN_XYZ"],
        "mustNotClaim": [],
        "metadata": {},
    }
    for policy_id in ("balanced", "strict_evidence"):
        result = run_case(case, policy_id)
        preview = (result.metrics or {}).get("answerPreview", "")
        assert "SECRET_EXPECTED_TOKEN_XYZ" not in preview, policy_id
