from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import pytest

WORKFLOW_ROOT = Path(__file__).resolve().parents[1]
if str(WORKFLOW_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKFLOW_ROOT))

from app.runtime.coordinator import Coordinator, TASK_PIPELINES
from app.runtime.agents import default_agent_registry
from app.runtime.events import NullEmitter
from app.runtime.executor import RunExecutor
from app.runtime.llm import (
    CircuitBreaker,
    LlmToolCall,
    LlmTurn,
    ResilientLlmClient,
)
from app.runtime.memory import NullMemoryClient
from app.runtime.models import (
    AgentOutput,
    AgentRunRequest,
    BudgetExceeded,
    PolicyBundle,
)
from app.runtime.builtin_tools import BuiltinToolRegistry

RESUME = """张三
工作经历
2022.07-2024.06 A公司 Java后端工程师
2024.03-2025.01 B公司 高级工程师
项目经历
项目：订单中台
- 基于Kafka实现异步解耦，峰值处理 5000 QPS
技能
Java Spring Boot MySQL Redis Kafka
"""

JD = """1. 熟悉 Java 与 Spring Boot 开发经验
2. 掌握 Redis 缓存经验
3. 熟悉 Kubernetes 优先
"""


class FakeLlm:
    """Deterministic stand-in that satisfies the ResilientLlmClient contract."""

    def __init__(self, budget=None, delay: float = 0.0, fail_agents=()):
        self.calls = []
        self.delay = delay
        self.fail_agents = set(fail_agents)

    async def chat(self, messages, *, agent_id, purpose="", max_tokens=2048,
                   temperature=0.2, json_mode=True, tools=None, tool_choice=None,
                   use_quality=False):
        self.calls.append({"agent": agent_id, "purpose": purpose,
                           "forcedFunction": bool(tools and tool_choice)})
        if self.delay:
            await asyncio.sleep(self.delay)
        if agent_id in self.fail_agents:
            from app.runtime.llm import LlmError
            raise LlmError("SERVER_ERROR", f"injected failure for {agent_id}", False)
        if agent_id == "CoordinatorAgent":
            return json.dumps({"plan": [], "reason": "keep rule plan"})
        section = {
            "ResumeParserAgent": "resume_facts",
            "JDAnalysisAgent": "jd_requirements",
            "TechAgent": "technical_findings",
            "ProjectAgent": "project_findings",
            "RiskAgent": "risks",
            "EvidenceAgent": "evidence",
            "ReportAgent": "recommendations",
            "ResumeOptimizeAgent": "recommendations",
            "InterviewQuestionAgent": "recommendations",
        }.get(agent_id, "technical_findings")
        claim_value: Any
        if section in {"resume_facts", "jd_requirements"}:
            claim_value = {"summary": f"{agent_id} 完成", "skills": ["Java", "Kafka"]}
        else:
            claim_value = [{"text": f"{agent_id} 结论", "evidence": "第6行"}]
        output = {
            "thought": f"{agent_id} 分析",
            "toolCalls": [],
            "output": {
                "summary": f"{agent_id} 完成",
                "claims": [{"section": section, "value": claim_value}],
                "evidence": [{"text": f"{agent_id} 证据", "sourceLine": 6,
                              "source": "resume", "verified": None}],
                "confidence": 0.8,
            },
            "done": True,
        }
        if agent_id == "ReportAgent":
            ref = {
                "sourceType": "RESUME",
                "sourceId": "resume",
                "lineStart": 6,
                "lineEnd": 6,
                "quote": "基于Kafka实现异步解耦，峰值处理 5000 QPS",
            }
            output["output"]["report"] = {
                "recommendation": "INTERVIEW_RECOMMEND",
                "dimensions": [
                    {"name": "技术能力", "score": 78, "status": "ASSESSED",
                     "evidenceCoverage": 0.8, "rationale": "Kafka 项目证据充分",
                     "evidenceRefs": [ref]},
                    {"name": "项目深度", "score": 72, "status": "ASSESSED",
                     "evidenceCoverage": 0.7, "rationale": "有峰值 QPS 量化",
                     "evidenceRefs": [ref]},
                    {"name": "JD匹配", "score": 70, "status": "ASSESSED",
                     "evidenceCoverage": 0.7, "rationale": "Java/Redis 覆盖，K8s 缺口",
                     "evidenceRefs": [ref]},
                    {"name": "履历可信度", "score": 60, "status": "ASSESSED",
                     "evidenceCoverage": 0.6, "rationale": "时间线存在重叠风险",
                     "evidenceRefs": [ref]},
                ],
                "strengths": ["Kafka 异步解耦经验"],
                "risks": [{
                    "id": "r-timeline",
                    "category": "TIMELINE",
                    "severity": "MEDIUM",
                    "claim": "工作时间线存在重叠",
                    "impact": "需核验真实在职区间",
                    "evidenceRefs": [ref],
                    "verificationPlan": "对照社保/离职证明核对起止月份",
                }],
                "interviewProbes": [{
                    "id": "p1",
                    "priority": "P1",
                    "question": "请说明订单中台峰值如何压测？",
                    "objective": "验证 Kafka 峰值处理真实性",
                    "triggeredBy": "project_claim",
                    "evidenceRefs": [ref],
                    "goodSignals": ["能说出压测工具与瓶颈定位"],
                    "redFlags": ["只能复述简历原句"],
                    "followUps": ["当时的 consumer lag 如何监控？"],
                    "scoreRubric": "能讲清压测方法与结果记满分",
                }],
                "interviewQuestions": ["请说明订单中台峰值如何压测？"],
                "dataQuality": "SUFFICIENT",
                "missingEvidence": [],
                "systemWarnings": [],
            }
        return json.dumps(output, ensure_ascii=False)


def make_request(run_type="tech_match", message="这个候选人的技术栈匹配怎么样？",
                 policy_config=None):
    return AgentRunRequest(
        runId="run-t1", conversationId="conv-t1", userId="u1", traceId="tr-1",
        runType=run_type, userMessage=message, resumeText=RESUME,
        jobDescription=JD, policyId="balanced", policyConfig=policy_config or {})


def make_executor(request, llm=None):
    emitter = NullEmitter(request.runId, request.conversationId, request.traceId)
    executor = RunExecutor(
        request, emitter,
        memory=NullMemoryClient(),
        builtin_tools=BuiltinToolRegistry(),
        llm=llm or FakeLlm())
    return executor, emitter


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def test_tech_match_pipeline_produces_grounded_answer():
    request = make_request()
    executor, emitter = make_executor(request)
    result = run(executor.execute())
    assert result["status"] == "SUCCEEDED"
    assert "Kafka" in result["answer"] or "技术能力" in result["answer"]
    assert isinstance(result.get("structuredReport"), dict)
    assert isinstance(result["structuredReport"].get("overallScore"), int)
    agents_used = result["metrics"]["agentsUsed"]
    assert "TechAgent" in agents_used and "ReportAgent" in agents_used
    assert "EvidenceAgent" in agents_used
    event_types = {e["eventType"] for e in emitter.events}
    assert {"agent.selected", "agent.started", "agent.completed"} <= event_types
    assert result["metrics"]["jdCoverage"] is not None, "TechAgent 预置 JD 覆盖率工具"
    assert result["promptVersions"], "prompt 版本必须写入轨迹"
    assert result["skillVersions"], "skill 版本必须写入轨迹"


def test_coordinator_rule_pipelines_cover_business_scenarios():
    for run_type, expected_head in [
        ("full_evaluation", "JDAnalysisAgent"),
        ("timeline_check", "RiskAgent"),
        ("project_rewrite", "ProjectAgent"),
        ("interview_questions", "RiskAgent"),
    ]:
        policy = PolicyBundle.from_config("balanced", {})
        coordinator = Coordinator(default_agent_registry, policy, None)
        plan = coordinator.base_plan(run_type, has_resume_facts=False, needs_parse=False)
        assert plan[0] == expected_head, f"{run_type} -> {plan}"
    rewrite_plan = TASK_PIPELINES["project_rewrite"]
    assert rewrite_plan == ["ProjectAgent", "ResumeOptimizeAgent"]


def test_followup_report_agent_runs_conversational_rag_presteps():
    """Copilot 追问（followup）必须先检索知识库与简历证据再回答。"""
    request = make_request(run_type="followup", message="这个候选人的评分依据是什么？")
    executor, emitter = make_executor(request)
    result = run(executor.execute())
    assert result["status"] in ("SUCCEEDED", "PARTIAL_SUCCESS")
    started_tools = [e.get("toolName") for e in emitter.events
                     if e["eventType"] == "tool.started"]
    assert "knowledge_search" in started_tools, "追问必须先查知识库标准"
    assert "resume_semantic_search" in started_tools, "追问必须带简历证据检索"


def test_query_rewrite_degrades_to_single_query_without_llm():
    """查询改写降级：无 llm 时 _retrieve_with_rewrite 用原 query 正常返回。"""
    from app.runtime.events import NullEmitter as _NullEmitter
    from app.runtime.models import RunBudget
    from app.runtime.tools import ToolExecutor

    executor = ToolExecutor(
        _NullEmitter(), RunBudget(), BuiltinToolRegistry(),
        max_tool_calls_run=5, tool_timeout_seconds=10,
        run_context={"resumeText": RESUME, "jobDescription": JD}, llm=None)

    async def scenario():
        queries = await executor._rewrite_queries("候选人的 Kafka 经验")
        assert queries == ["候选人的 Kafka 经验"], "无 llm 必须只用原 query"

    run(scenario())


def test_rewrite_stage_never_spends_a_hidden_provider_call():
    """RAG 保留 rewrite 阶段指标，但模型生成的 query 必须原样执行。"""
    from app.runtime.events import NullEmitter as _NullEmitter
    from app.runtime.models import RunBudget
    from app.runtime.tools import ToolExecutor

    class AuditLlm(FakeLlm):
        async def chat(self, messages, *, agent_id, purpose="", max_tokens=2048,
                       temperature=0.2, json_mode=True, tools=None, tool_choice=None,
                       use_quality=False):
            self.calls.append({"agent": agent_id, "purpose": purpose})
            return await super().chat(
                messages, agent_id=agent_id, purpose=purpose,
                max_tokens=max_tokens, temperature=temperature,
                json_mode=json_mode, tools=tools, tool_choice=tool_choice)

    llm = AuditLlm()
    tools = ToolExecutor(
        _NullEmitter("r", "c", "t"), RunBudget(), BuiltinToolRegistry(),
        max_tool_calls_run=10, tool_timeout_seconds=10,
        run_context={"resumeText": RESUME, "jobDescription": JD}, llm=llm)

    async def scenario():
        # Monkeypatch gateway so we don't hit Java.
        from app.runtime import gateway
        async def fake_kb(query, top_k=5, rerank=False, **kwargs):
            return json.dumps({
                "chunks": [{"chunkId": f"c-{query[:8]}", "content": query,
                            "title": "rubric"}],
                "strategy": "hybrid_bm25_embedding",
            })
        original = gateway.java_knowledge_search
        gateway.java_knowledge_search = fake_kb
        try:
            call = await tools.execute(
                "ReportAgent", "knowledge_search",
                {"query": "评分依据"}, enable_rewrite=True)
            assert call.status == "SUCCEEDED"
            assert not any(c["purpose"] == "query_rewrite" for c in llm.calls)
            result = call.result
            assert isinstance(result, dict)
            assert result.get("queriesUsed") == ["评分依据"]
            assert result.get("queryRewriteMode") == "deterministic_passthrough"
            assert isinstance(result.get("_latency"), dict)
            assert "rewrite_ms" in result["_latency"]
        finally:
            gateway.java_knowledge_search = original

    run(scenario())


def test_followup_presteps_enable_rewrite_flag_in_events():
    """Copilot followup 的 knowledge_search prestep 必须带 rewriteEnabled。"""
    request = make_request(run_type="followup", message="评分依据是什么？")
    llm = FakeLlm()
    executor, emitter = make_executor(request, llm=llm)
    result = run(executor.execute())
    assert result["status"] in ("SUCCEEDED", "PARTIAL_SUCCESS")
    kb_starts = [e for e in emitter.events
                 if e["eventType"] == "tool.started"
                 and e.get("toolName") == "knowledge_search"]
    assert kb_starts, "followup 必须启动 knowledge_search"
    assert any(e.get("payload", {}).get("rewriteEnabled") for e in kb_starts), (
        "followup knowledge_search 必须 enable_rewrite")


def test_policy_low_cost_disables_evidence_agent():
    config = {"agentOrder": ["TechAgent", "ReportAgent"],
              "evidenceVerification": {"enabled": False},
              "maxAgentCount": 4}
    request = make_request(policy_config=config)
    executor, _ = make_executor(request)
    result = run(executor.execute())
    assert result["status"] == "SUCCEEDED"
    assert "EvidenceAgent" not in result["metrics"]["agentsUsed"]


def test_run_cancellation_propagates():
    request = make_request(run_type="full_evaluation")
    executor, _ = make_executor(request, llm=FakeLlm(delay=0.4))

    async def scenario():
        task = asyncio.create_task(executor.execute())
        await asyncio.sleep(0.15)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    run(scenario())


def test_run_timeout_returns_timed_out_with_degraded_answer():
    config = {"timeoutPolicy": {"runTimeoutSeconds": 1}}
    request = make_request(run_type="full_evaluation", policy_config=config)
    executor, _ = make_executor(request, llm=FakeLlm(delay=0.8))
    result = run(executor.execute())
    assert result["status"] == "TIMED_OUT"
    assert result["errorCode"] == "RUN_TIMEOUT"
    assert "降级" in result["answer"]


def test_agent_failure_degrades_not_hangs():
    request = make_request(run_type="tech_match")
    executor, emitter = make_executor(
        request, llm=FakeLlm(fail_agents={"TechAgent"}))
    result = run(executor.execute())
    # TechAgent 失败后重规划并降级：产生答案，但结果必须如实标记为
    # PARTIAL_SUCCESS，不能把降级伪装成完整成功。
    assert result["status"] == "PARTIAL_SUCCESS", \
        "one agent failure must degrade honestly, not sink or fake the run"
    assert result["answer"], "degraded run still answers from remaining agents"
    assert any(e["eventType"] == "agent.failed" for e in emitter.events)
    assert any("TechAgent" in r for r in result["metrics"]["degradedReasons"])


def test_specialist_budget_rejection_preserves_terminal_execution():
    class ReservedTerminalLlm(FakeLlm):
        async def chat(self, messages, *, agent_id, purpose="", max_tokens=2048,
                       temperature=0.2, **kwargs):
            if agent_id == "TechAgent":
                raise BudgetExceeded(
                    "llmReservation", "protectedForOthers=2")
            return await super().chat(
                messages, agent_id=agent_id, purpose=purpose,
                max_tokens=max_tokens, temperature=temperature, **kwargs)

    request = make_request(run_type="tech_match")
    llm = ReservedTerminalLlm()
    executor, _ = make_executor(request, llm=llm)
    result = run(executor.execute())
    assert result["status"] == "PARTIAL_SUCCESS"
    assert any(call["agent"] == "ReportAgent" for call in llm.calls)
    assert "TechAgent_failed" in result["metrics"]["degradedReasons"]


def test_tool_whitelist_enforced_per_agent():
    # followup keeps ReportAgent-only pipeline; chat no longer uses quick_answer.
    request = make_request(run_type="followup", message="随便聊聊")

    class NaughtyLlm(FakeLlm):
        async def chat(self, messages, *, agent_id, purpose="", max_tokens=2048,
                       temperature=0.2, **kwargs):
            if agent_id == "ReportAgent" and not self.calls:
                self.calls.append({"agent": agent_id})
                return json.dumps({
                    "thought": "尝试越权",
                    "toolCalls": [{"tool": "external_profile_lookup", "arguments": {}}],
                    "output": None, "done": False})
            return await super().chat(messages, agent_id=agent_id, purpose=purpose)

    executor, emitter = make_executor(request, llm=NaughtyLlm())
    result = run(executor.execute())
    # ReportAgent 只有一次迭代且把它浪费在越权调用上：白名单拒绝生效，
    # 运行以明确标注的降级结果收尾而不是伪装成功。
    assert result["status"] in ("SUCCEEDED", "PARTIAL_SUCCESS")
    rejected = [e for e in emitter.events
                if e["eventType"] == "tool.started"
                and e.get("toolName") == "external_profile_lookup"]
    assert not rejected, "ReportAgent 无权调用外网工具，必须被拒绝"


def test_evidence_agent_marks_unsupported_claims():
    request = make_request(run_type="full_evaluation")

    class OverclaimLlm(FakeLlm):
        async def chat(self, messages, *, agent_id, purpose="", max_tokens=2048,
                       temperature=0.2, **kwargs):
            if agent_id == "TechAgent":
                return json.dumps({
                    "thought": "评估",
                    "toolCalls": [],
                    "output": {
                        "summary": "夸大结论",
                        "claims": [{"section": "technical_findings",
                                    "value": [{"text": "可用性达到 99.999%",
                                               "evidence": ""}]}],
                        "evidence": [], "confidence": 0.9},
                    "done": True}, ensure_ascii=False)
            return await super().chat(messages, agent_id=agent_id, purpose=purpose)

    executor, _ = make_executor(request, llm=OverclaimLlm())
    result = run(executor.execute())
    conflicts = result["sharedState"]["conflicts"]
    assert any(c.get("type") == "unsupported_claim" and "99.999" in str(c.get("claim"))
               for c in conflicts), "编造数字必须被证据核验拦下"
    assert result["metrics"]["evidenceSupportRatio"] is not None


def test_llm_budget_exceeded_degrades():
    config = {"maxLlmCalls": 1}
    request = make_request(run_type="full_evaluation", policy_config=config)

    class CountingLlm(FakeLlm):
        def __init__(self):
            super().__init__()
            self.count = 0

        async def chat(self, messages, *, agent_id, purpose="", max_tokens=2048,
                       temperature=0.2, **kwargs):
            self.count += 1
            if self.count > 1:
                raise BudgetExceeded("maxLlmCalls", "limit=1")
            return await super().chat(messages, agent_id=agent_id, purpose=purpose)

    executor, _ = make_executor(request, llm=CountingLlm())
    result = run(executor.execute())
    assert result["errorCode"] == "BUDGET_EXCEEDED"
    assert result["status"] == "PARTIAL_SUCCESS"
    assert result["answer"], "预算耗尽也要基于已有结果降级回答"


def test_parallel_specialists_grouped_and_merged():
    request = make_request(run_type="full_evaluation",
                           message="请完整评估这份简历")
    executor, emitter = make_executor(request)
    result = run(executor.execute())
    assert result["status"] == "SUCCEEDED"
    selected = [e for e in emitter.events if e["eventType"] == "agent.selected"]
    groups = selected[0]["payload"]["parallelGroups"]
    assert any(len(g) > 1 for g in groups), f"specialists must parallelize: {groups}"
    agents_used = result["metrics"]["agentsUsed"]
    for agent in ("TechAgent", "ProjectAgent", "RiskAgent", "ReportAgent"):
        assert agent in agents_used
    assert result["metrics"]["agentTimingsMs"], "per-agent profiling recorded"


def test_parallel_runner_overlaps_independent_agent_tasks():
    async def scenario():
        request = make_request(run_type="full_evaluation")
        executor, _ = make_executor(request)
        definitions = [
            executor.registry.get(agent_id)
            for agent_id in ("TechAgent", "ProjectAgent", "RiskAgent")
        ]
        starts = {}

        async def delayed_run(definition):
            starts[definition.agent_id] = asyncio.get_running_loop().time()
            await asyncio.sleep(0.20)
            return AgentOutput(
                agentId=definition.agent_id,
                type=definition.output_type,
                claims=[], evidence=[], confidence=0.8,
                source="test", dependencies=[], summary="done")

        executor._run_agent = delayed_run  # type: ignore[method-assign]
        started = asyncio.get_running_loop().time()
        assert await executor._run_parallel(definitions)
        elapsed = asyncio.get_running_loop().time() - started
        assert max(starts.values()) - min(starts.values()) < 0.02
        assert elapsed < 0.50, f"parallel group took {elapsed:.3f}s"

    run(scenario())


def test_parallel_report_sections_merge_and_overlap():
    ref = {
        "sourceType": "RESUME", "sourceId": "resume",
        "quote": "负责订单服务和缓存一致性优化",
    }

    class SectionLlm:
        supports_parallel_report_sections = True

        def __init__(self):
            self.started = {}
            self.quality = {}

        async def chat_turn(self, messages, *, agent_id, purpose="",
                            max_tokens=2048, tools=None, tool_choice=None,
                            use_quality=False, trace_context=None):
            self.started[purpose] = asyncio.get_running_loop().time()
            self.quality[purpose] = use_quality
            await asyncio.sleep(0.08)
            if purpose == "report_score":
                payload = {
                    "summary": "候选人基础栈匹配，但项目深度需要面试核验。",
                    "recommendation": "NEED_MANUAL_REVIEW",
                    "dataQuality": "PARTIAL",
                    "dimensions": [
                        {"name": name, "score": score, "status": "ASSESSED",
                         "rationale": "有简历证据", "evidenceRefs": [ref]}
                        for name, score in (
                            ("技术能力", 72), ("项目深度", 66),
                            ("JD匹配", 70), ("履历可信度", 62))],
                    "strengths": ["Java 后端经验", "具备缓存优化经验"],
                }
            elif purpose == "report_risk":
                payload = {
                    "risks": [
                        {"id": f"r{i}", "category": "CANDIDATE",
                         "severity": "HIGH" if i == 1 else "MEDIUM",
                         "claim": f"风险 {i}", "impact": "影响岗位判断",
                         "verificationPlan": "面试中核验",
                         "evidenceRefs": [ref]}
                        for i in range(1, 5)],
                    "missingEvidence": [f"缺失证据 {i}" for i in range(1, 9)],
                }
            else:
                payload = {
                    "interviewQuestions": [
                        {"id": f"q{i}", "priority": "HIGH",
                         "question": f"请说明项目问题 {i}",
                         "objective": "核验项目深度", "triggeredBy": "项目风险",
                         "goodSignals": ["给出数据和取舍"],
                         "redFlags": ["只能复述简历"],
                         "evidenceRefs": [ref]}
                        for i in range(1, 9)],
                }
            raw = json.dumps(payload, ensure_ascii=False)
            return LlmTurn(
                content="",
                tool_calls=[LlmToolCall(
                    tool_call_id=f"call-{purpose}",
                    name="emit_report_section", arguments=payload,
                    raw_arguments=raw)],
                finish_reason="tool_calls")

    async def scenario():
        request = make_request(run_type="full_evaluation")
        llm = SectionLlm()
        executor, _ = make_executor(request, llm=llm)
        started = asyncio.get_running_loop().time()
        output, calls = await executor._run_parallel_report_sections(
            [{"role": "system", "content": "生成结构化评估"},
             {"role": "user", "content": "评估这份简历"}],
            round_id="r:ReportAgent:round:1",
            memory_refs=[], skill_refs=[], observed_tool_call_ids=[],
            is_sparse_resume=False)
        elapsed = asyncio.get_running_loop().time() - started
        assert calls == 3 and output is not None
        report = output["report"]
        assert len(report["risks"]) == 4
        assert len(report["interviewQuestions"]) == 8
        assert isinstance(report.get("overallScore"), int)
        assert max(llm.started.values()) - min(llm.started.values()) < 0.02
        assert elapsed < 0.18
        assert llm.quality == {
            "report_score": True,
            "report_risk": False,
            "report_question": False,
        }

    run(scenario())


def test_parallel_report_retries_only_the_failed_section():
    ref = {
        "sourceType": "RESUME", "sourceId": "resume",
        "quote": "candidate evidence",
    }

    class OneMalformedSectionLlm:
        supports_parallel_report_sections = True

        def __init__(self):
            self.calls = []

        async def chat_turn(self, messages, *, agent_id, purpose="",
                            max_tokens=2048, tools=None, tool_choice=None,
                            use_quality=False, trace_context=None):
            self.calls.append(purpose)
            if purpose == "report_score":
                payload = {
                    "summary": "summary", "recommendation": "NEED_MANUAL_REVIEW",
                    "dataQuality": "PARTIAL",
                    "dimensions": [
                        {"name": name, "score": 60, "status": "ASSESSED",
                         "rationale": "evidence", "evidenceRefs": [ref]}
                        for name in ("技术能力", "项目深度", "JD匹配", "履历可信度")],
                    "strengths": ["one", "two"],
                }
            elif purpose == "report_risk":
                payload = {
                    "risks": [
                        {"claim": f"risk {i}", "evidenceRefs": [ref]}
                        for i in range(3)],
                    "missingEvidence": [],
                }
            elif self.calls.count("report_question") == 1:
                return LlmTurn(
                    content="", tool_calls=[LlmToolCall(
                        tool_call_id="bad-question", name="emit_report_section",
                        arguments={}, raw_arguments="{bad",
                        arguments_error="malformed json")],
                    finish_reason="tool_calls")
            else:
                payload = {
                    "interviewQuestions": [
                        {"question": f"question {i}", "evidenceRefs": [ref]}
                        for i in range(8)]}
            raw = json.dumps(payload, ensure_ascii=False)
            return LlmTurn(
                content="", tool_calls=[LlmToolCall(
                    tool_call_id=f"call-{purpose}",
                    name="emit_report_section", arguments=payload,
                    raw_arguments=raw)], finish_reason="tool_calls")

    async def scenario():
        request = make_request(run_type="full_evaluation")
        llm = OneMalformedSectionLlm()
        executor, _ = make_executor(request, llm=llm)
        output, calls = await executor._run_parallel_report_sections(
            [{"role": "system", "content": "json report"}],
            round_id="r:ReportAgent:round:1", memory_refs=[],
            skill_refs=[], observed_tool_call_ids=[], is_sparse_resume=False)
        assert output is not None
        assert calls == 4
        assert llm.calls.count("report_score") == 1
        assert llm.calls.count("report_risk") == 1
        assert llm.calls.count("report_question") == 2

    run(scenario())


def test_parallel_report_production_client_uses_json_mode_and_call_cap():
    ref = {
        "sourceType": "RESUME", "sourceId": "resume",
        "quote": "candidate evidence",
    }

    async def scenario():
        request = make_request(run_type="full_evaluation")
        executor, emitter = make_executor(request, llm=FakeLlm())
        client = ResilientLlmClient(
            emitter, executor.budget,
            max_llm_calls=executor.policy.maxLlmCalls,
            llm_timeout_seconds=5,
            breaker=CircuitBreaker(threshold=5))
        calls = []

        async def fake_chat(messages, *, purpose="", json_mode=True,
                            tools=None, tool_choice=None, **kwargs):
            calls.append({
                "purpose": purpose, "jsonMode": json_mode,
                "tools": tools, "toolChoice": tool_choice,
            })
            if purpose == "report_score":
                payload = {
                    "summary": "summary",
                    "recommendation": "NEED_MANUAL_REVIEW",
                    "dataQuality": "PARTIAL",
                    "dimensions": [
                        {"name": name, "score": 60, "status": "ASSESSED",
                         "rationale": "evidence", "evidenceRefs": [ref]}
                        for name in (
                            "技术能力", "项目深度", "JD匹配", "履历可信度")],
                    "strengths": ["one", "two"],
                }
            elif purpose == "report_risk":
                payload = {
                    "risks": [
                        {"claim": f"risk {i}", "evidenceRefs": [ref]}
                        for i in range(4)],
                    "missingEvidence": [],
                }
            else:
                payload = {
                    "interviewQuestions": [
                        {"question": f"question {i}",
                         "evidenceRefs": [ref]}
                        for i in range(8)]}
            return json.dumps(payload, ensure_ascii=False)

        client.chat = fake_chat
        executor.llm = client
        executor.tools.llm = client
        output, call_count = await executor._run_parallel_report_sections(
            [{"role": "system", "content": "json report"}],
            round_id="r:ReportAgent:round:1", memory_refs=[],
            skill_refs=[], observed_tool_call_ids=[], is_sparse_resume=False,
            max_calls=3)

        assert output is not None
        assert call_count == 3
        assert len(calls) == 3
        assert all(call["jsonMode"] is True for call in calls)
        assert all(call["tools"] is None for call in calls)

    run(scenario())


def test_full_evaluation_arbitration_does_not_call_released_control_scope():
    class NoControlLlm(FakeLlm):
        async def chat(self, *args, **kwargs):
            raise AssertionError("released control scope must not call LLM")

    request = make_request(run_type="full_evaluation")
    executor, emitter = make_executor(request, llm=NoControlLlm())
    executor.budget.release_llm_reservation("control")
    executor.state.apply_artifacts({
        "conflicts": [{
            "claim": "项目吞吐指标缺少压测口径",
            "reason": "缺少外部证据",
        }],
    })

    run(executor._arbitrate_conflicts())

    conflict = executor.state.artifact("conflicts")[0]
    assert conflict["resolution"] == "uncertain"
    assert any(
        event["eventType"] == "run.progress"
        and event["payload"].get("mode")
        == "deterministic_no_control_budget"
        for event in emitter.events)


def test_parallel_report_multiple_failures_never_exceed_four_section_calls():
    class MalformedSectionsLlm:
        supports_parallel_report_sections = True

        def __init__(self):
            self.calls = []

        async def chat_turn(self, messages, *, agent_id, purpose="",
                            max_tokens=2048, tools=None, tool_choice=None,
                            use_quality=False, trace_context=None):
            self.calls.append(purpose)
            if purpose == "report_risk":
                payload = {"risks": [], "missingEvidence": []}
                return LlmTurn(
                    content="", tool_calls=[LlmToolCall(
                        tool_call_id="risk-ok", name="emit_report_section",
                        arguments=payload,
                        raw_arguments=json.dumps(payload))],
                    finish_reason="tool_calls")
            return LlmTurn(
                content="", tool_calls=[LlmToolCall(
                    tool_call_id=f"bad-{len(self.calls)}",
                    name="emit_report_section", arguments={},
                    raw_arguments="{bad", arguments_error="malformed")],
                finish_reason="tool_calls")

    async def scenario():
        request = make_request(run_type="full_evaluation")
        llm = MalformedSectionsLlm()
        executor, _ = make_executor(request, llm=llm)
        output, call_count = await executor._run_parallel_report_sections(
            [{"role": "system", "content": "json report"}],
            round_id="r:ReportAgent:round:1", memory_refs=[],
            skill_refs=[], observed_tool_call_ids=[], is_sparse_resume=False,
            max_calls=4)
        assert output is None
        assert call_count == 4
        assert len(llm.calls) == 4

    run(scenario())


def test_pause_snapshot_and_resume_skips_completed_agents():
    async def scenario():
        request = make_request(run_type="full_evaluation")
        pause_event = asyncio.Event()
        emitter = NullEmitter(request.runId, request.conversationId, request.traceId)
        executor = RunExecutor(request, emitter, memory=NullMemoryClient(),
                               builtin_tools=BuiltinToolRegistry(),
                               llm=FakeLlm(delay=0.05),
                               pause_event=pause_event)
        task = asyncio.create_task(executor.execute())
        # Wait for a real checkpoint boundary instead of relying on wall-clock
        # timing: planning/probing work legitimately changes across runtimes.
        for _ in range(100):
            if any(event["eventType"] == "agent.completed"
                   for event in emitter.events):
                break
            await asyncio.sleep(0.02)
        pause_event.set()
        result = await task
        assert result["status"] == "PAUSED"
        snapshot = result["executionSnapshot"]
        assert snapshot["executedAgents"], "pause after at least one agent"
        assert snapshot["plan"] and snapshot["nextPlanIndex"] >= 1
        assert snapshot["toolCallLedger"] is not None
        executed_before = list(snapshot["executedAgents"])

        resume_request = make_request(run_type="full_evaluation")
        resume_request = resume_request.model_copy(
            update={"resumeSnapshot": snapshot})
        resumed_llm = FakeLlm()
        executor2 = RunExecutor(resume_request,
                                NullEmitter(request.runId, request.conversationId,
                                            request.traceId),
                                memory=NullMemoryClient(),
                                builtin_tools=BuiltinToolRegistry(), llm=resumed_llm)
        result2 = await executor2.execute()
        assert result2["status"] in ("SUCCEEDED", "PARTIAL_SUCCESS")
        rerun = [c["agent"] for c in resumed_llm.calls
                 if c["agent"] in executed_before]
        assert not rerun, f"completed agents must not re-run: {rerun}"

    run(scenario())
