from __future__ import annotations

import asyncio
import sys
from pathlib import Path

WORKFLOW_ROOT = Path(__file__).resolve().parents[1]
if str(WORKFLOW_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKFLOW_ROOT))

from app.conversation.models import ConversationReplyRequest, CopilotAnswer
from app.conversation.responder import generate_copilot_answer
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
