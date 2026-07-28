from __future__ import annotations

import asyncio
import sys
from pathlib import Path

WORKFLOW_ROOT = Path(__file__).resolve().parents[1]
if str(WORKFLOW_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKFLOW_ROOT))

from app.runtime.context import ContextManager, estimate_tokens
from app.runtime.events import NullEmitter
from app.runtime.loop_guard import LoopGuard
from app.runtime.models import AgentOutput, ContextBudget, PolicyBundle
from app.runtime.state import SharedState


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ---------------- policy bundle ----------------

def test_policy_bundle_parses_db_config():
    config = {
        "agentOrder": ["TechAgent", "ReportAgent"],
        "maxAgentCount": 4,
        "maxLlmCalls": 6,
        "supportedRunTypes": ["quick_answer", "followup", "tech_match"],
        "requiredArtifacts": ["final_report"],
        "optionalArtifacts": ["technical_findings"],
        "toolBudget": {"maxToolCallsPerRun": 8, "maxToolCallsPerAgent": 3},
        "evidenceVerification": {"enabled": False, "strict": False, "minSupportRatio": 0.3},
        "timeoutPolicy": {"runTimeoutSeconds": 480},
    }
    bundle = PolicyBundle.from_config("low_cost", config)
    assert bundle.policyId == "low_cost"
    assert bundle.maxLlmCalls == 6
    assert bundle.toolBudget.maxToolCallsPerAgent == 3
    assert bundle.evidenceVerification.enabled is False
    assert bundle.timeoutPolicy.runTimeoutSeconds == 480
    assert bundle.timeoutPolicy.llmTimeoutSeconds == 120  # default preserved
    assert not bundle.supports_run_type("full_evaluation")
    assert bundle.supports_run_type("quick_answer")
    assert bundle.requiredArtifacts == ["final_report"]


# ---------------- loop guard ----------------

def test_loop_guard_duplicate_tool_calls():
    guard = LoopGuard(max_duplicate_tool_calls=2)
    assert guard.check_tool_call("sig-a").triggered is False
    assert guard.check_tool_call("sig-a").triggered is False
    decision = guard.check_tool_call("sig-a")
    assert decision.triggered and decision.kind == "duplicate_tool_call"
    assert decision.action == "skip_step"


def test_loop_guard_repeated_plans_switch_agent():
    guard = LoopGuard(max_repeated_plans=2)
    plan = "TechAgent: 检索 Kafka 证据 123"
    assert guard.check_plan(plan).triggered is False
    # 数字与空白差异不影响语义指纹
    assert guard.check_plan("TechAgent: 检索Kafka证据 456").triggered is False
    decision = guard.check_plan(plan)
    assert decision.triggered and decision.action == "switch_agent"


def test_loop_guard_no_new_information():
    guard = LoopGuard(max_no_new_info=2)
    assert guard.check_observation("结果A").triggered is False
    assert guard.check_observation("结果A").triggered is False
    decision = guard.check_observation("结果A")
    assert decision.triggered and decision.kind == "no_new_information"


def test_loop_guard_completed_agent_and_delegation_cycle():
    guard = LoopGuard(max_agent_visits=1)
    guard.check_agent_start("TechAgent")
    guard.record_completed_agent("TechAgent")
    decision = guard.check_agent_start("TechAgent")
    assert decision.triggered and decision.kind == "repeated_completed_agent"

    guard2 = LoopGuard()
    assert guard2.check_delegation("A", "B").triggered is False
    assert guard2.check_delegation("B", "A").triggered is False
    assert guard2.check_delegation("A", "B").triggered is True


def test_loop_guard_repeated_error_degrades():
    guard = LoopGuard(max_repeated_errors=3)
    guard.check_error("TimeoutError: tool x")
    guard.check_error("TimeoutError: tool x")
    decision = guard.check_error("TimeoutError: tool x")
    assert decision.triggered and decision.action == "degrade"


# ---------------- shared state ----------------

def _output(agent_id: str, section: str, value) -> AgentOutput:
    return AgentOutput(agentId=agent_id, type="findings",
                       claims=[{"section": section, "value": value}])


def test_shared_state_appends_and_flags_conflicts():
    state = SharedState()
    state.apply_output(_output("TechAgent", "technical_findings",
                               [{"text": "Kafka 有项目证据"}]))
    conflicts = state.apply_output(_output(
        "ResumeParserAgent", "resume_facts", {"name": "张三"}))
    assert conflicts == []
    conflicts = state.apply_output(_output(
        "JDAnalysisAgent", "resume_facts", {"name": "李四"}))
    assert conflicts == ["resumeFacts.name"]
    assert state.data["conflicts"], "conflicting fact must be recorded, not overwritten"
    assert state.data["resumeFacts"]["name"] == "张三", "original value preserved"


def test_resume_facts_list_does_not_clobber_dict():
    """Regression: ProjectAgent fact-list must not replace parse dict.

    Production run run-24c69602… crashed Evidence/Report with
    AttributeError: 'list' object has no attribute 'get' because
    inspect_signals assumed resumeFacts was always a dict.
    """
    state = SharedState()
    state.apply_artifacts({
        "resumeFacts": {
            "skills": ["Java"],
            "projects": [{"name": "ResumAI"}],
            "experiences": [{"raw": "2025-2026"}],
            "source": "parse_resume_fast_path",
        }
    }, by_agent="ResumeParserAgent")
    conflicts = state.apply_output(_output(
        "ProjectAgent", "resume_facts",
        [{"fact": "会 Java", "detail": "简历原文", "source": "技能栏"}]))
    assert "resumeFacts" in conflicts
    facts = state.artifact("resumeFacts")
    assert isinstance(facts, dict), f"expected dict, got {type(facts)}"
    assert facts.get("source") == "parse_resume_fast_path"
    assert facts.get("projects")
    clash = [c for c in (state.artifact("conflicts") or [])
             if isinstance(c, dict) and c.get("section") == "resumeFacts"]
    assert clash and clash[-1].get("reason") == "dict_shaped_artifact_type_clash"


def test_list_artifact_wrappers_are_normalized_before_evidence_append():
    """Regression for run-9cf010f9…: model wrapper dicts stay list-shaped."""
    state = SharedState()
    state.apply_output(AgentOutput(
        agentId="TechAgent",
        type="technical_findings",
        artifacts={
            "technicalFindings": {
                "title": "技术能力",
                "findings": [
                    {"text": "Java 项目证据充分"},
                    "Kafka 仍需追问",
                ],
            },
            "evidence": {
                "title": "技术证据",
                "items": [{"text": "简历技能栏"}],
            },
        },
        evidence=[{"text": "TechAgent 直接证据"}],
    ))
    state.apply_output(AgentOutput(
        agentId="ProjectAgent",
        type="project_findings",
        claims=[
            {
                "section": "project_findings",
                "value": {
                    "title": "项目经历",
                    "findings": [{"text": "存在公开项目链接"}],
                },
            },
            {
                "section": "risks",
                "value": {
                    "title": "风险",
                    "risks": [{"text": "个人贡献边界待确认"}],
                },
            },
        ],
        evidence=[{"text": "ProjectAgent 直接证据"}],
    ))

    for key in (
            "technicalFindings", "projectFindings", "risks", "evidence"):
        assert isinstance(state.artifact(key), list), key
    assert [item["text"] for item in state.artifact("technicalFindings")] == [
        "Java 项目证据充分", "Kafka 仍需追问"]
    assert len(state.artifact("evidence")) == 3
    assert state.artifact("evidence")[-1]["byAgent"] == "ProjectAgent"


def test_put_artifact_same_canonical_list_is_idempotent():
    state = SharedState()
    state.apply_artifacts({"mcpEvidence": [{"toolCallId": "mcp-1"}]})
    canonical = state.artifact("mcpEvidence")
    canonical.append({"toolCallId": "mcp-2"})

    state.put_artifact("mcpEvidence", canonical)

    assert [item["toolCallId"] for item in state.artifact("mcpEvidence")] == [
        "mcp-1", "mcp-2"]


def test_inspect_signals_tolerates_list_resume_facts():
    from app.runtime.agents import default_agent_registry
    from app.runtime.coordinator import Coordinator
    from app.runtime.models import PolicyBundle

    policy = PolicyBundle.from_config("balanced", {
        "agentOrder": ["TechAgent", "EvidenceAgent", "ReportAgent"],
        "maxAgentCount": 8,
        "maxLlmCalls": 12,
        "evidenceVerification": {"enabled": True},
    })
    coordinator = Coordinator(default_agent_registry, policy, None)
    signals = coordinator.inspect_signals(
        resume_text="项目经历：ResumAI Agent",
        job_description="Java",
        artifacts={
            "resumeFacts": [
                {"fact": "Java", "detail": "技能"},
                {"fact": "项目", "detail": "ResumAI"},
            ],
            "parsedResume": {"success": True, "projectNames": ["ResumAI"]},
        },
        shared={})
    assert signals["has_projects"] is True
    assert isinstance(signals, dict)


def test_shared_state_scoped_views():
    state = SharedState()
    state.apply_output(_output("TechAgent", "technical_findings", [{"text": "T1"}]))
    state.apply_output(_output("RiskAgent", "risks", [{"text": "R1"}]))
    tech_view = state.view_for("TechAgent")
    assert "risks" not in tech_view, "TechAgent 不应读取风险区"
    report_view = state.view_for("ReportAgent")
    assert "technicalFindings" in report_view and "risks" in report_view


def test_claims_for_verification_and_ratio():
    state = SharedState()
    state.apply_output(_output("TechAgent", "technical_findings",
                               [{"text": "掌握Kafka", "evidence": "第12行"}]))
    claims = state.claims_for_verification()
    assert claims and claims[0]["byAgent"] == "TechAgent"
    state.data["evidence"].append({"text": "a", "verified": True})
    state.data["evidence"].append({"text": "b", "verified": False})
    assert state.evidence_support_ratio() == 0.5


# ---------------- context manager ----------------

def _manager(window=2000, ratio=0.5) -> ContextManager:
    budget = ContextBudget(modelWindow=window, compactAtRatio=ratio,
                           reservedOutputBudget=200, recentMessageBudget=300)
    return ContextManager(budget, NullEmitter(), "run-1", "conv-1")


def test_context_assembly_and_budget_cap():
    manager = _manager()
    messages = manager.assemble(
        system_prompt="系统" * 6000,
        policy_instructions="策略",
        skill_instructions="技能",
        user_request="用户问题：技术栈匹配怎么样？",
        current_goal="技术栈匹配",
        shared_state_digest="{}",
        recent_messages=[{"role": "USER", "content": "第一条"},
                         {"role": "ASSISTANT", "content": "第二条"}],
        conversation_summary="",
        memory_block="",
        tool_results_block="",
        output_schema="JSON")
    assert len(messages) == 2
    assert "超出预算已截断" in messages[0]["content"]
    assert "用户问题" in messages[1]["content"]


def test_recent_message_overflow_becomes_summary():
    manager = _manager()
    history = [{"role": "USER", "content": f"历史消息{i}" * 40} for i in range(20)]
    prepared = manager.prepare_messages(history, "")
    assert prepared["overflowCount"] > 0
    assert "自动压缩" in prepared["summary"]


def test_compaction_preserves_protected_sections():
    manager = _manager(window=900, ratio=0.1)
    tool_block = "\n".join(
        [f"[TOOL_CALL t{i} id=tc-{i:016x}]\n"
         f"[TOOL_RESULT t{i} id=tc-{i:016x} status=SUCCEEDED] " + "长结果" * 120
         for i in range(4)])
    messages = manager.assemble(
        system_prompt="系统提示",
        policy_instructions="",
        skill_instructions="",
        user_request="请评估这份简历的技术栈匹配",
        current_goal="技术栈匹配",
        shared_state_digest="{}" * 200,
        recent_messages=[],
        conversation_summary="旧摘要" * 100,
        memory_block="记忆" * 200,
        tool_results_block=tool_block,
        output_schema="JSON SCHEMA")
    assert manager.needs_compaction(messages)
    compacted = run(manager.compact(
        messages, reason="test",
        protected_markers=["[当前请求]", "[当前目标]", "[输出要求]"]))
    joined = "\n".join(m["content"] for m in compacted)
    assert "请评估这份简历的技术栈匹配" in joined, "最新请求不可丢失"
    assert "技术栈匹配" in joined
    violations = manager.consistency_check(
        compacted, user_request="请评估这份简历的技术栈匹配", current_goal="技术栈匹配")
    assert violations == []
    assert manager.estimate(compacted) < manager.estimate(messages)
    assert manager.compactions and manager.compactions[0].summary_version == 1


def test_consistency_check_detects_broken_tool_pair():
    manager = _manager()
    messages = [{"role": "user",
                 "content": "[TOOL_CALL a id=tc-00000000000000aa]\n没有结果 用户请求X"}]
    violations = manager.consistency_check(messages, user_request="用户请求X",
                                           current_goal="")
    assert any(v.startswith("tool_call_without_result") for v in violations)


def test_consistency_check_pairs_by_id_not_count():
    """One orphan call plus one orphan result would pass a count check but
    must fail the per-id pairing check."""
    manager = _manager()
    messages = [{"role": "user", "content": (
        "[TOOL_CALL a id=tc-00000000000000aa]\n"
        "[TOOL_RESULT b id=tc-00000000000000bb status=SUCCEEDED] x\n用户请求X")}]
    violations = manager.consistency_check(messages, user_request="用户请求X",
                                           current_goal="")
    assert any(v.startswith("tool_call_without_result") for v in violations)
    assert any(v.startswith("tool_result_without_call") for v in violations)


def test_token_estimate_positive():
    # ASCII ≈ chars/3.6; CJK ≈ 0.7/char — both must stay positive and sane.
    ascii_estimate = estimate_tokens("abc" * 100)
    assert 60 <= ascii_estimate <= 140
    cjk_estimate = estimate_tokens("简历评估" * 50)
    assert 120 <= cjk_estimate <= 220
