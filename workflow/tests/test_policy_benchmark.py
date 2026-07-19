"""Smoke test for the sandbox replay benchmark (repo-root harness).

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

if not (ROOT / "harness" / "run_policy_benchmark.py").exists() or not CASE_DIR.exists():
    pytest.skip("repo-root harness/testdata not present in this environment",
                allow_module_level=True)

sys.path.insert(0, str(ROOT))

from harness.run_policy_benchmark import aggregate, load_cases, run_case  # noqa: E402


def test_load_cases_schema():
    cases = load_cases(CASE_DIR)
    assert len(cases) >= 10
    for case in cases:
        assert case["caseId"]
        assert case["dataset"] in {"GOLD", "SYNTHETIC", "REGRESSION", "SECURITY"}
        assert "mustFind" in case and "mustNotClaim" in case


def test_run_case_and_champion():
    cases = load_cases(CASE_DIR)
    sample = [c for c in cases if c["caseId"] == "gold-java-backend-normal"][0]
    results = [run_case(sample, pid) for pid in ("balanced", "strict_evidence", "low_cost")]
    assert all(r.status == "SUCCEEDED" for r in results)
    summary = aggregate(results)
    assert summary["championPolicy"] in {"balanced", "strict_evidence", "low_cost"}
    assert summary["policies"]["strict_evidence"]["evidenceSupportRatio"] >= 0


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
