"""ECS-local production workflow simulator (no image rebuild, no live LLM).

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
tool pre-steps and RunExecutor. Only the provider response is deterministic.
It records the exact model-input message sizes and tool schemas that the
production executor would send, so Python routing changes can be checked on
ECS before rebuilding/restarting Docker services.
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


async def simulate(*, live: bool = False,
                   context_log: Optional[Path] = None,
                   external: bool = False) -> dict[str, Any]:
    request = AgentRunRequest(
        runId="ecs-sim-run", conversationId="ecs-sim-conversation",
        userId="ecs-sim", traceId="ecs-sim-trace",
        runType="full_evaluation", userMessage="评估这份简历",
        resumeText=(EXTERNAL_RESUME if external else SHORT_RESUME),
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
        "liveProvider": live,
        "externalScenario": external,
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

    agents = set(summary["plan"])
    if result.get("status") != "SUCCEEDED":
        raise SystemExit(f"simulation failed: {result.get('status')}")
    if not {"TechAgent", "ProjectAgent", "EvidenceAgent", "ReportAgent"} <= agents:
        raise SystemExit(f"multi-agent plan regressed: {summary['plan']}")
    min_calls, max_calls = ((7, 9) if external else (4, 6))
    if not (min_calls <= summary["llmCalls"] <= max_calls):
        raise SystemExit(f"unexpected LLM call count: {summary['llmCalls']}")
    if summary["skillEvents"]["skill.loaded"] < 2 \
            or summary["skillEvents"]["skill.applied"] < 2:
        raise SystemExit(f"Skill injection regressed: {summary['skillEvents']}")
    report_context = next(
        (row for row in llm.contexts if row["agent"] == "ReportAgent"), None)
    if not report_context or not report_context["qualityModel"]:
        raise SystemExit("ReportAgent is not using the quality model")
    if external and summary["mcpCatalogExposures"] < 1:
        raise SystemExit("external scenario did not expose live MCP tools")
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
        help="use a valid public GitHub repository to exercise MCP research")
    parser.add_argument(
        "--mcp-smoke", action="store_true",
        help="probe and call DeepWiki once without invoking an LLM")
    args = parser.parse_args()
    if args.mcp_smoke:
        payload = asyncio.run(mcp_smoke())
    else:
        payload = asyncio.run(simulate(
            live=args.live, context_log=args.context_log,
            external=args.external))
    print(json.dumps(payload, ensure_ascii=False, indent=2))
