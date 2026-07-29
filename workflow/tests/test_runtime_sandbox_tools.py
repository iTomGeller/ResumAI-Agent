import sys
from pathlib import Path

WORKFLOW_ROOT = Path(__file__).resolve().parents[1]
if str(WORKFLOW_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKFLOW_ROOT))

from app.runtime import sandbox_tools_local as tools

RESUME = """张三
教育经历
2018.09-2022.06 某大学 计算机科学 本科
工作经历
2022.07-2024.06 A公司 Java后端工程师
2024.03-2025.01 B公司 高级工程师
项目经历
项目：订单中台
- 基于Kafka实现异步解耦，峰值处理 5000 QPS
- 使用Redis缓存热点数据，接口耗时从 300ms 降至 80ms
技能
Java Spring Boot MySQL Redis Kafka Docker
"""

JD = """岗位要求：
1. 熟悉 Java 与 Spring Boot 开发经验
2. 掌握 MySQL 与 Redis 优化经验
3. 熟悉 Kafka 或 RocketMQ 消息中间件
4. 了解 Kubernetes 容器编排优先
"""


def test_parse_resume_extracts_skills_and_projects():
    result = tools.parse_resume({"resumeText": RESUME})
    assert result["success"] is True
    assert "java" in result["skills"]
    assert "kafka" in result["skills"]
    assert any("订单中台" in p for p in result["projectNames"])
    assert result["timelinePeriods"]


def test_parse_resume_fails_closed_on_empty():
    result = tools.parse_resume({"resumeText": "   "})
    assert result["success"] is False
    assert result["error"] == "empty_text"


def test_check_timeline_detects_overlap():
    result = tools.check_timeline({"resumeText": RESUME})
    assert result["success"] is True
    assert result["overlaps"], "2024.03-2025.01 overlaps 2022.07-2024.06"
    assert any(i["type"] == "overlap" for i in result["issues"])


def test_check_timeline_flags_future_dates():
    text = "2030.01-2031.02 未来公司 工程师"
    result = tools.check_timeline({"resumeText": text})
    assert any(i["type"] == "future_start" for i in result["issues"])
    assert result["hasHighRisk"] is True


def test_jd_coverage_scores_reasonably():
    result = tools.calculate_jd_coverage({"resumeText": RESUME, "jdText": JD})
    assert result["success"] is True
    assert result["requirementCount"] >= 3
    assert 0.4 <= result["coverage"] <= 1.0
    missing = " ".join(result["missing"])
    assert "Kubernetes" in missing or "容器" in missing


def test_locate_evidence_finds_and_rejects():
    result = tools.locate_evidence({
        "resumeText": RESUME,
        "claims": ["基于Kafka实现异步解耦", "精通 Rust 系统编程"],
    })
    assert result["success"] is True
    kafka, rust = result["claims"]
    assert kafka["found"] is True and kafka["line"] is not None
    assert rust["found"] is False


def test_verify_report_evidence_rejects_fabricated_numbers():
    result = tools.verify_report_evidence({
        "resumeText": RESUME,
        "claims": [
            {"text": "峰值处理 5000 QPS", "evidence": "基于Kafka实现异步解耦，峰值处理 5000 QPS"},
            {"text": "系统可用性达到 99.999%", "evidence": ""},
        ],
    })
    assert result["success"] is True
    assert result["unsupportedCount"] >= 1
    unsupported_text = " ".join(u["claim"] for u in result["unsupported"])
    assert "99.999" in unsupported_text


def test_verify_report_evidence_rejects_stale_fetch_failure_claim():
    url = "https://blog.csdn.net/example/article/details/123"
    result = tools.verify_report_evidence({
        "resumeText": f"技术文章：{url}",
        "claims": [{"text": f"CSDN 链接 {url} 无法抓取验证"}],
        "externalEvidence": [{
            "status": "SUCCEEDED",
            "sourceUrls": [url],
            "result": {"success": True, "text": "文章正文"},
        }],
    })
    assert result["success"] is True
    assert result["unsupportedCount"] == 1
    assert result["unsupported"][0]["reason"] \
        == "contradicted_by_successful_external_fetch"


def test_verify_report_evidence_does_not_infer_ownership_from_fetch():
    url = "https://gitee.com/example/project"
    result = tools.verify_report_evidence({
        "resumeText": f"项目链接：{url}",
        "claims": [{"text": "已确认候选人是该 Gitee 仓库维护者"}],
        "externalEvidence": [{
            "status": "SUCCEEDED",
            "sourceUrls": [url],
            "result": {"success": True, "text": "仓库 README"},
        }],
    })
    assert result["success"] is True
    assert result["unsupportedCount"] == 1


def test_resume_lint_flags_vague_wording():
    text = "- 熟悉各种分布式系统\n- 使用Redis缓存热点数据，接口耗时从 300ms 降至 80ms"
    result = tools.resume_lint({"resumeText": text})
    assert result["success"] is True
    assert any(i.get("type") == "vague_wording" for i in result["issues"])


def test_validate_report_schema():
    good = {"overallScore": 78, "recommendation": "CONSIDER",
            "strengths": ["扎实"], "risks": ["时间线重叠"], "summary": "ok"}
    assert tools.validate_report_schema({"report": good})["valid"] is True
    bad = {"overallScore": 300, "recommendation": "MAYBE"}
    result = tools.validate_report_schema({"report": bad})
    assert result["valid"] is False
    assert any("out of range" in e for e in result["errors"])


def test_evaluate_policy_output_scoring():
    answer = "候选人 Kafka 经验真实，存在时间线重叠风险。"
    result = tools.evaluate_policy_output({
        "answer": answer,
        "resumeText": RESUME,
        "mustFind": ["时间线重叠", "Kafka"],
        "mustNotClaim": ["Kubernetes 生产经验"],
    })
    assert result["success"] is True
    assert result["mustFindScore"] == 1.0
    assert result["violations"] == []
    assert result["score"] > 0.8


def test_run_tool_unknown():
    assert tools.run_tool("rm_rf", {})["success"] is False
