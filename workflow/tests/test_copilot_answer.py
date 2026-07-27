from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

WORKFLOW_ROOT = Path(__file__).resolve().parents[1]
if str(WORKFLOW_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKFLOW_ROOT))

from app.conversation.models import ConversationReplyRequest, CopilotAnswer
from app.conversation.responder import (
    _bounded_context_snapshot,
    generate_copilot_answer,
)
from app.runtime.coordinator import TASK_PIPELINES


def test_copilot_does_not_repeat_full_report_by_default() -> None:
    request = ConversationReplyRequest(
        turnId="turn-1",
        content="这份简历怎么样？",
        contextSnapshot={
            "summary": ("综合评分 78，建议进入人工复核。" * 20),
        },
    )
    answer = asyncio.run(generate_copilot_answer(request))
    assert isinstance(answer, CopilotAnswer)
    assert "综合评分 78" in answer.answer
    # Must not dump the repeated full summary blob into chat.
    assert answer.answer.count("综合评分 78") == 1
    assert len(answer.answer) < 220
    assert any(a.type == "OPEN_REPORT" for a in answer.actions)


def test_arithmetic_is_direct_copilot_answer() -> None:
    answer = asyncio.run(generate_copilot_answer(ConversationReplyRequest(
        turnId="turn-2",
        content="1+1",
    )))
    assert answer.answer == "2"
    assert answer.citations == []


def test_quick_answer_pipeline_removed() -> None:
    assert "quick_answer" not in TASK_PIPELINES


def test_background_query_returns_copilot_answer_not_structured_report() -> None:
    answer = asyncio.run(generate_copilot_answer(ConversationReplyRequest(
        turnId="turn-3",
        content="查一下证据出处",
        allowTools=True,
        disposition="BACKGROUND_QUERY",
        contextSnapshot={"hasResume": True, "summary": "技术匹配中等"},
    )))
    assert isinstance(answer, CopilotAnswer)
    assert "决策报告" in answer.answer
    assert not hasattr(answer, "scores")
    assert not hasattr(answer, "overallScore")


def test_no_report_side_question_uses_real_bounded_model(
    monkeypatch,
) -> None:
    """A report is optional for chat; no-report turns must still call the
    bounded short-answer model instead of returning a routing receipt."""
    import httpx
    from app.config import settings

    captured = {}

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self):
            return {
                "choices": [{
                    "message": {
                        "content": json.dumps({
                            "answer": (
                                "RAG 命中率低说明当前证据召回不足，应检查查询改写、"
                                "分块和重排，并在补证前降低结论置信度。"
                            ),
                            "citations": [],
                            # A model cannot turn ordinary chat into a full run.
                            "actions": [{
                                "type": "START_EVALUATION",
                                "label": "错误的模型动作",
                            }],
                            "suggestions": ["查看召回与重排指标"],
                        }, ensure_ascii=False),
                    },
                }],
            }

    class FakeClient:
        def __init__(self, *args, **kwargs) -> None:
            captured["timeout"] = kwargs.get("timeout")

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        async def post(self, url, **kwargs):
            captured["url"] = url
            captured.update(kwargs)
            return FakeResponse()

    monkeypatch.setattr(settings, "deepseek_api_key", "test-key")
    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)

    answer = asyncio.run(generate_copilot_answer(ConversationReplyRequest(
        turnId="turn-no-report",
        content="RAG 命中率低代表什么？",
        disposition="DIRECT_REPLY",
        contextSnapshot={
            "hasResume": True,
            "hasJobDescription": True,
            "resumeText": "Java Kafka 支付平台" * 500,
            "jobDescription": "高级 Java 后端，要求 Kafka 与 Kubernetes" * 200,
            # Deliberately no structuredReport.
        },
    )))

    assert "证据召回不足" in answer.answer
    assert "这是对话回复" not in answer.answer
    assert answer.actions == []
    assert captured["json"]["max_tokens"] <= 500
    prompt = json.loads(captured["json"]["messages"][1]["content"])
    assert "structuredReport" not in prompt["contextSnapshot"]
    assert len(prompt["contextSnapshot"]["resumeText"]) <= 1801
    assert len(prompt["contextSnapshot"]["jobDescription"]) <= 1601


def test_no_report_side_question_has_useful_offline_fallback(
    monkeypatch,
) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "deepseek_api_key", "")
    answer = asyncio.run(generate_copilot_answer(ConversationReplyRequest(
        turnId="turn-no-key",
        content="RAG 命中率低代表什么？",
        disposition="DIRECT_REPLY",
        contextSnapshot={"hasResume": True, "hasJobDescription": True},
    )))

    assert "召回" in answer.answer
    assert "简历/JD 原文" in answer.answer
    assert "这是对话回复" not in answer.answer
    assert answer.actions == []


def test_no_report_overall_question_never_starts_evaluation(
    monkeypatch,
) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "deepseek_api_key", "")
    answer = asyncio.run(generate_copilot_answer(ConversationReplyRequest(
        turnId="turn-no-report-overall",
        content="这个候选人推荐吗？",
        disposition="DIRECT_REPLY",
        contextSnapshot={
            "hasResume": True,
            "hasJobDescription": True,
            "resumeText": "Java 后端，负责支付平台幂等与 Kafka 消息链路。",
            "jobDescription": "高级 Java 后端，要求 Kubernetes 与 RAG。",
        },
    )))

    assert answer.actions == []
    assert "发起完整评估" not in answer.answer
    assert "没有足够证据" in answer.answer


def test_short_answer_context_is_bounded_without_report() -> None:
    compact = _bounded_context_snapshot({
        "resumeText": "R" * 5000,
        "jobDescription": "J" * 5000,
        "summary": "S" * 5000,
        "hasResume": True,
        "hasJobDescription": True,
    })
    assert len(compact["resumeText"]) == 1801
    assert len(compact["jobDescription"]) == 1601
    assert len(compact["summary"]) == 1201
    assert "structuredReport" not in compact
