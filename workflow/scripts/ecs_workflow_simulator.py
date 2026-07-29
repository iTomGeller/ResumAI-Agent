"""ECS-local production workflow simulator (no image rebuild required).

Run on the ECS host with the existing workflow image and mount current source:

  docker run --rm \
    --network resumai_resumai-net --env-file /opt/resumai-src/.env \
    -e MCP_TOOLS_ENABLED=1 -e MCP_SKIP_PROBE=0 \
    -e MCP_CONFIG_PATH=/app/config/mcp-servers.json \
    -e JAVA_BACKEND_URL=http://ai-resume-backend:8080 \
    -v /opt/resumai-src/workflow:/workspace -w /workspace \
    -v /opt/resumai-src/config:/app/config \
    -e PYTHONPATH=/workspace --entrypoint python \
    resumai-ai-resume-workflow:latest scripts/ecs_workflow_simulator.py

The simulator executes the real Coordinator, ContextManager, SkillManager,
tool pre-steps and RunExecutor. By default the provider response is
deterministic; ``--live`` uses the ECS production LLM and MCP configuration.
It records the exact model-input messages and tool schemas so routing changes
can be checked on ECS before rebuilding/restarting production services.
"""
from __future__ import annotations

import asyncio
import argparse
import importlib.util
import json
import sys
import time
from datetime import datetime, timezone
from collections import Counter
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.runtime.builtin_tools import BuiltinToolRegistry
from app.runtime.events import NullEmitter
from app.runtime.executor import RunExecutor
from app.runtime.memory import NullMemoryClient
from app.runtime.models import AgentRunRequest, RunBudget
from app.runtime.tools import ToolExecutor


def _load_test_fake_llm():
    path = ROOT / "tests" / "test_runtime_executor.py"
    spec = importlib.util.spec_from_file_location("runtime_test_support", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load test support: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.FakeLlm


FakeLlm = _load_test_fake_llm()


class ContextAuditLlm(FakeLlm):
    def __init__(self) -> None:
        super().__init__()
        self.contexts: list[dict[str, Any]] = []

    async def chat(self, messages, *, agent_id, purpose="", max_tokens=2048,
                   temperature=0.2, json_mode=True, tools=None,
                   tool_choice=None, use_quality=False):
        tool_names = [
            str(item.get("function", {}).get("name") or "")
            for item in (tools or []) if isinstance(item, dict)
        ]
        self.contexts.append({
            "agent": agent_id,
            "purpose": purpose,
            "messageCount": len(messages),
            "contextChars": sum(
                len(str(message.get("content") or "")) for message in messages),
            "toolNames": [name for name in tool_names if name],
            "qualityModel": bool(use_quality),
            "forcedFinal": isinstance(tool_choice, dict),
        })
        return await super().chat(
            messages, agent_id=agent_id, purpose=purpose,
            max_tokens=max_tokens, temperature=temperature,
            json_mode=json_mode, tools=tools, tool_choice=tool_choice,
            use_quality=use_quality)


class LiveContextRecorder:
    """Transparent wrapper around the production LLM client."""

    def __init__(self, delegate: Any, log_path: Path) -> None:
        self.delegate = delegate
        self.log_path = log_path
        self.contexts: list[dict[str, Any]] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self.delegate, name)

    def _record(self, messages, kwargs: dict[str, Any]) -> None:
        tools = kwargs.get("tools") or []
        row = {
            "recordedAt": datetime.now(timezone.utc).isoformat(),
            "agent": kwargs.get("agent_id"),
            "purpose": kwargs.get("purpose"),
            "qualityModel": bool(kwargs.get("use_quality")),
            "toolChoice": kwargs.get("tool_choice"),
            "messages": messages,
            "tools": tools,
            "messageCount": len(messages),
            "contextChars": sum(
                len(str(message.get("content") or "")) for message in messages),
        }
        self.contexts.append({
            "agent": row["agent"],
            "purpose": row["purpose"],
            "messageCount": row["messageCount"],
            "contextChars": row["contextChars"],
            "toolNames": [
                str(item.get("function", {}).get("name") or "")
                for item in tools if isinstance(item, dict)
            ],
            "qualityModel": row["qualityModel"],
            "forcedFinal": isinstance(row["toolChoice"], dict),
        })
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")

    async def chat(self, messages, **kwargs):
        self._record(messages, kwargs)
        return await self.delegate.chat(messages, **kwargs)

    async def chat_turn(self, messages, **kwargs):
        self._record(messages, kwargs)
        return await self.delegate.chat_turn(messages, **kwargs)


SHORT_RESUME = """张三，目标岗位 Java 后端工程师，拥有两年服务端开发经验。
技术栈：Java、Spring Boot、Redis、MySQL、Kafka，熟悉 Linux、Git 和 REST API。
项目：负责订单服务重构，完成接口设计、缓存一致性处理和基础自动化测试。
成果：优化慢查询与缓存策略，提升接口稳定性，能独立定位线上问题并编写技术文档。
求职方向：秋招 Java 后端研发，希望参与高并发分布式系统建设。"""

JD = """高级 Java / AI Agent 平台工程师：Java 21、Spring Boot 3、
MySQL、Redis、Docker、RAG、LLM、Trace 可观测性，要求 5 年以上经验。"""

EXTERNAL_RESUME = """王强｜Java 后端工程师
2018-2020 某公司 Java 开发，负责支付接口。
2020-2022 离职，简历没有说明这段时间的产出。
2022-至今 后端工程师，负责订单与用户服务。
技能：Java、Spring Boot、Redis、MySQL、Docker、RAG。
项目：https://github.com/spring-projects/spring-petclinic
项目成果：负责 Agent 编排、向量检索和服务稳定性优化。"""

DOMESTIC_EXTERNAL_RESUME = """陈晨｜Java / AI Agent 平台工程师
2021-至今 企业软件平台，负责 Spring Boot 微服务与 RAG 知识助手。
技术：Java、Spring Boot、Redis、MySQL、Docker、RAG、LLM、Trace。
国内公开仓库：https://gitee.com/mindspore/mindspore
技术文章主页：https://blog.csdn.net/
项目成果：完成混合召回、二次排序和逐调用可观测性改造。"""

STRONG_RESUME = """李明｜高级 AI Agent / Java 平台工程师｜7 年经验
2019-2022 某金融科技公司，高级 Java 工程师：负责交易平台服务治理与性能优化。
2022-至今 某智能软件公司，Agent 平台负责人：带领 5 人团队建设企业知识助手。
技术：Java 21、Spring Boot 3、MySQL、Redis、Kafka、Docker、Kubernetes、Python、RAG、LLM。
项目一：设计多 Agent 编排与状态恢复机制，将复杂任务成功率从 71% 提升到 89%；
通过并行工具调用和上下文压缩把 P95 延迟从 48 秒降低到 19 秒。
项目二：建设混合检索、重排和引用追踪链路，离线 NDCG@10 提升 13%，并落地逐调用 Trace、预算和熔断监控。
职责边界：负责架构设计、关键模块编码、评测集设计和线上事故复盘；业务收益数字由内部看板统计，未附公开链接。"""

SCENARIOS = {
    "short": SHORT_RESUME,
    "strong": STRONG_RESUME,
    "external": EXTERNAL_RESUME,
    "domestic_external": DOMESTIC_EXTERNAL_RESUME,
}


async def simulate(*, live: bool = False,
                   context_log: Optional[Path] = None,
                   scenario: str = "short") -> dict[str, Any]:
    if scenario not in SCENARIOS:
        raise ValueError(f"unknown scenario: {scenario}")
    request = AgentRunRequest(
        runId=f"ecs-sim-{scenario}-run",
        conversationId=f"ecs-sim-{scenario}-conversation",
        userId="ecs-sim", traceId=f"ecs-sim-{scenario}-trace",
        runType="full_evaluation", userMessage="评估这份简历",
        resumeText=SCENARIOS[scenario],
        jobDescription=JD,
        policyId="balanced",
        policyConfig={"evidenceVerification": {"enabled": True}},
    )
    emitter = NullEmitter(
        request.runId, request.conversationId, request.traceId)
    if live:
        executor = RunExecutor(
            request, emitter, memory=NullMemoryClient(),
            builtin_tools=BuiltinToolRegistry())
        log_path = context_log or (
            ROOT / ".sim-artifacts" / "llm-contexts.jsonl")
        if log_path.exists():
            log_path.unlink()
        llm = LiveContextRecorder(executor.llm, log_path)
        executor.llm = llm
        executor.tools.llm = llm
    else:
        llm = ContextAuditLlm()
        executor = RunExecutor(
            request, emitter, memory=NullMemoryClient(),
            builtin_tools=BuiltinToolRegistry(), llm=llm)
    started = time.perf_counter()
    result = await executor.execute()
    elapsed_ms = round((time.perf_counter() - started) * 1000)

    selected = next(
        (event.get("payload") or {} for event in emitter.events
         if event.get("eventType") == "agent.selected"), {})
    event_counts = Counter(
        str(event.get("eventType") or "") for event in emitter.events)
    mcp_calls = [
        call for call in executor.tools.call_log
        if (
            executor.tools.definitions.get(call.tool) is not None
            and executor.tools.definitions[call.tool].kind == "mcp"
        )
    ]
    summary = {
        "status": result.get("status"),
        "elapsedMs": elapsed_ms,
        "plan": selected.get("plan") or [],
        "parallelGroups": selected.get("parallelGroups") or [],
        "budgetPlan": selected.get("budgetPlan") or {},
        "llmCalls": len(llm.contexts),
        "contexts": llm.contexts,
        "skillEvents": {
            key: event_counts.get(key, 0)
            for key in ("skill.catalog", "skill.selected", "skill.loaded",
                        "skill.applied")
        },
        "mcpCatalogExposures": sum(
            1 for event in emitter.events
            if event.get("eventType") == "tool.progress"
            and (event.get("payload") or {}).get("lifecycleStage")
            == "CATALOG_EXPOSED"),
        "mcpExecutions": {
            outcome: sum(1 for call in mcp_calls if call.status == outcome)
            for outcome in ("SUCCEEDED", "FAILED", "REJECTED")
        },
        "mcpCalls": [{
            "tool": call.tool,
            "status": call.status,
            "durationMs": call.duration_ms,
            "error": call.error,
        } for call in mcp_calls],
        "liveProvider": live,
        "scenario": scenario,
        "contextLog": str(context_log or (
            ROOT / ".sim-artifacts" / "llm-contexts.jsonl")) if live else None,
        "reportQuality": {
            "score": (result.get("structuredReport") or {}).get("overallScore"),
            "recommendation": (
                result.get("structuredReport") or {}).get("recommendation"),
            "strengths": len(
                (result.get("structuredReport") or {}).get("strengths") or []),
            "risks": len(
                (result.get("structuredReport") or {}).get("risks") or []),
            "questions": len(
                (result.get("structuredReport") or {}).get(
                    "interviewQuestions") or []),
            "answerChars": len(str(result.get("answer") or "")),
        },
    }
    if live:
        result_path = (context_log or (
            ROOT / ".sim-artifacts" / "llm-contexts.jsonl")).with_suffix(
                ".result.json")
        result_path.write_text(json.dumps({
            "elapsedMs": elapsed_ms,
            "status": result.get("status"),
            "answer": result.get("answer"),
            "structuredReport": result.get("structuredReport"),
            "metrics": result.get("metrics"),
        }, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        summary["resultLog"] = str(result_path)
        event_path = result_path.with_name(
            result_path.stem.replace(".result", "") + ".events.json")
        event_path.write_text(json.dumps(
            emitter.events, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8")
        summary["eventLog"] = str(event_path)

    agents = set(summary["plan"])
    if result.get("status") != "SUCCEEDED":
        raise SystemExit(f"simulation failed: {result.get('status')}")
    if not {"TechAgent", "ProjectAgent", "EvidenceAgent", "ReportAgent"} <= agents:
        raise SystemExit(f"multi-agent plan regressed: {summary['plan']}")
    # The deterministic fake completes ProjectAgent without proposing extra
    # MCP action turns; live external research is expected to use them.
    min_calls, max_calls = (
        (6, 15) if live and scenario == "external"
        else (6, 14) if live
        else (5, 7)
    )
    if not (min_calls <= summary["llmCalls"] <= max_calls):
        raise SystemExit(f"unexpected LLM call count: {summary['llmCalls']}")
    skill_events = summary["skillEvents"]
    if skill_events["skill.catalog"] < 1 \
            or skill_events["skill.selected"] < 1:
        raise SystemExit(f"Skill metadata exposure regressed: {skill_events}")
    if skill_events["skill.applied"] > skill_events["skill.loaded"]:
        raise SystemExit(f"Skill lifecycle is inconsistent: {skill_events}")
    report_context = next(
        (row for row in llm.contexts if row["agent"] == "ReportAgent"), None)
    if not report_context or not report_context["qualityModel"]:
        raise SystemExit("ReportAgent is not using the quality model")
    if scenario == "external" and summary["mcpCatalogExposures"] < 1:
        raise SystemExit("external scenario did not expose live MCP tools")
    if live and scenario == "external" \
            and summary["mcpExecutions"]["SUCCEEDED"] < 1:
        raise SystemExit(
            "external scenario did not autonomously complete MCP research")
    return summary


async def mcp_smoke() -> dict[str, Any]:
    from app.runtime.mcp_registry import get_mcp_registry

    registry = await get_mcp_registry(probe=True)
    tools = ToolExecutor(
        NullEmitter("ecs-mcp-smoke", "ecs-mcp-smoke", "ecs-mcp-smoke"),
        RunBudget(), BuiltinToolRegistry(), max_tool_calls_run=3,
        tool_timeout_seconds=45,
        run_context={
            "resumeText": (
                "项目：https://github.com/spring-projects/spring-petclinic"),
        })
    tools.attach_mcp(registry)
    call = await tools.execute(
        "ProjectAgent", "deepwiki.read_wiki_structure",
        {"repoName": "spring-projects/spring-petclinic"},
        tool_call_id="ecs-deepwiki-smoke")
    result = {
        "status": call.status,
        "durationMs": call.duration_ms,
        "error": call.error,
        "resultKeys": sorted((call.result or {}).keys())
        if isinstance(call.result, dict) else [],
    }
    if call.status != "SUCCEEDED":
        raise SystemExit(json.dumps(result, ensure_ascii=False))
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--live", action="store_true",
        help="call the production LLM/MCP clients using the mounted ECS env")
    parser.add_argument(
        "--context-log", type=Path,
        help="JSONL path for the exact per-round messages and tool schemas")
    parser.add_argument(
        "--external", action="store_true",
        help="compatibility alias for --scenario external")
    parser.add_argument(
        "--scenario", choices=sorted(SCENARIOS), default="short",
        help="differentiated resume fixture")
    parser.add_argument(
        "--mcp-smoke", action="store_true",
        help="probe and call DeepWiki once without invoking an LLM")
    args = parser.parse_args()
    if args.mcp_smoke:
        payload = asyncio.run(mcp_smoke())
    else:
        payload = asyncio.run(simulate(
            live=args.live, context_log=args.context_log,
            scenario=("external" if args.external else args.scenario)))
    print(json.dumps(payload, ensure_ascii=False, indent=2))
