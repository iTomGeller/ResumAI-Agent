from __future__ import annotations

import asyncio
import sys
from pathlib import Path

WORKFLOW_ROOT = Path(__file__).resolve().parents[1]
if str(WORKFLOW_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKFLOW_ROOT))

from app.conversation import TurnIntent, resolve_turn, resolve_turn_with_model


def test_side_question_answers_progress_without_interrupting() -> None:
    decision = resolve_turn(
        "现在评估到哪了？",
        context={"runStatus": "RUNNING", "currentNode": "risk_eval", "completedNodes": ["intent"]},
    )

    assert decision.intent == TurnIntent.SIDE_QUESTION
    assert decision.affects_evaluation is False
    assert decision.answer_then_resume is True
    assert decision.affected_nodes == []
    assert "risk_eval" in decision.assistant_reply


def test_explanation_side_quest_uses_known_definition() -> None:
    decision = resolve_turn("RAG 命中低是什么意思？")

    assert decision.intent == TurnIntent.SIDE_QUESTION
    assert "外部证据" in decision.assistant_reply
    assert decision.control_action is None


def test_rewrite_side_quest_states_missing_information() -> None:
    decision = resolve_turn("帮我改写这段项目经历")

    assert decision.intent == TurnIntent.SIDE_QUESTION
    assert decision.answer_then_resume is True
    assert "需要原句" in decision.assistant_reply


def test_output_preference_does_not_become_evaluation_focus_change() -> None:
    decision = resolve_turn(
        "不要看 Trace，直接告诉我结论",
        context={"summary": "综合评分 78，建议进入人工复核。"},
    )

    assert decision.intent == TurnIntent.SIDE_QUESTION
    assert decision.affects_evaluation is False
    assert decision.affected_nodes == []
    assert "综合评分 78" in decision.assistant_reply


def test_job_comparison_uses_provided_context_only() -> None:
    decision = resolve_turn(
        "比较岗位，哪个岗位更适合？",
        context={
            "topJdMatches": [
                {"title": "Java 后端", "score": 0.82, "gaps": ["高并发指标"]},
                {"title": "AI Agent", "score": 0.71, "gaps": ["生产部署"]},
            ]
        },
    )

    assert decision.intent == TurnIntent.SIDE_QUESTION
    assert "Java 后端" in decision.assistant_reply
    assert "82%" in decision.assistant_reply
    assert decision.affects_evaluation is False


def test_mock_interview_uses_existing_questions() -> None:
    decision = resolve_turn(
        "现在帮我模拟面试",
        context={"interviewQuestions": ["为什么选择该架构？", "如何定位线上故障？"]},
    )

    assert decision.intent == TurnIntent.SIDE_QUESTION
    assert "为什么选择该架构" in decision.assistant_reply
    assert decision.answer_then_resume is True


def test_side_quest_accepts_nested_current_task_context() -> None:
    decision = resolve_turn(
        "解释一下当前结论？",
        context={"currentTask": {"summary": "项目证据较强，但高并发指标仍需面试验证。"}},
    )

    assert decision.intent == TurnIntent.SIDE_QUESTION
    assert "高并发指标" in decision.assistant_reply
    assert decision.answer_then_resume is True


def test_non_evaluation_note_does_not_create_revision() -> None:
    decision = resolve_turn("备注：周五下午约面")

    assert decision.intent == TurnIntent.CONTEXT_ADD
    assert decision.affects_evaluation is False
    assert decision.affected_nodes == []
    assert decision.answer_then_resume is True


def test_candidate_fact_invalidates_all_evaluation_nodes() -> None:
    decision = resolve_turn("补充：候选人还有一段 Go 项目经验")

    assert decision.intent == TurnIntent.CONTEXT_ADD
    assert decision.affects_evaluation is True
    assert "resume_parse" in decision.affected_nodes
    assert "report" in decision.affected_nodes
    assert decision.answer_then_resume is False


def test_goal_change_reuses_resume_parse_but_invalidates_downstream() -> None:
    decision = resolve_turn("改成前端岗位重新评估")

    assert decision.intent == TurnIntent.GOAL_CHANGE
    assert decision.affects_evaluation is True
    assert "resume_parse" not in decision.affected_nodes
    assert "jd_match" in decision.affected_nodes


def test_evaluation_focus_is_a_revision_change() -> None:
    decision = resolve_turn("重点看高并发，忽略学历")

    assert decision.intent == TurnIntent.EVALUATION_FOCUS
    assert decision.affects_evaluation is True
    assert "resume_parse" not in decision.affected_nodes
    assert "intent" not in decision.affected_nodes
    assert "jd_match" not in decision.affected_nodes
    assert "knowledge_context" in decision.affected_nodes
    assert "tech_eval" in decision.affected_nodes
    assert decision.evaluation_patch["focusInstruction"] == "重点看高并发，忽略学历"


def test_ambiguous_goal_asks_for_clarification_while_run_continues() -> None:
    decision = resolve_turn("换个方向")

    assert decision.intent == TurnIntent.CLARIFY
    assert decision.requires_confirmation is True
    assert decision.affects_evaluation is False
    assert decision.answer_then_resume is True


def test_control_commands_are_rule_first() -> None:
    pause = resolve_turn("先暂停评估")
    cancel = resolve_turn("别跑了")
    resume = resolve_turn("别停，继续评估")

    assert (pause.intent, pause.control_action) == (TurnIntent.PAUSE, "PAUSE")
    assert (cancel.intent, cancel.control_action) == (TurnIntent.CANCEL, "CANCEL")
    assert (resume.intent, resume.control_action) == (TurnIntent.RESUME, "RESUME")


def test_negated_control_with_a_side_question_does_not_interrupt() -> None:
    cancel = resolve_turn("不要取消，我只是想比较另一个岗位？")
    pause = resolve_turn("别暂停，我先问个进度问题？")

    assert cancel.control_action is None
    assert pause.control_action is None
    assert pause.intent == TurnIntent.SIDE_QUESTION
    assert pause.answer_then_resume is True


def test_model_fallback_is_safe_when_runtime_or_credentials_are_unavailable() -> None:
    decision = asyncio.run(resolve_turn_with_model("我突然想到另一个职业方向怎么办"))

    assert decision.intent == TurnIntent.SIDE_QUESTION
    assert decision.affects_evaluation is False
    assert decision.answer_then_resume is True
