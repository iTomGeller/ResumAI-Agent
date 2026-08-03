from __future__ import annotations

import asyncio
import operator
import sys
from pathlib import Path
from typing import Annotated, TypedDict

WORKFLOW_ROOT = Path(__file__).resolve().parents[1]
if str(WORKFLOW_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKFLOW_ROOT))

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, Send, interrupt

from app.runtime.langgraph_executor import (
    LangGraphRunExecutor,
    _reduce_agent_results,
)


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def test_agent_result_reducer_preserves_parallel_outputs():
    assert _reduce_agent_results(
        [{"agentId": "TechAgent"}],
        [{"agentId": "RiskAgent"}],
    ) == [
        {"agentId": "TechAgent"},
        {"agentId": "RiskAgent"},
    ]


def test_runtime_graph_wires_send_reducer_command_and_custom_stream():
    async def scenario():
        executor = object.__new__(LangGraphRunExecutor)

        async def observe_plan(state):
            get_stream_writer()({"stage": "test.plan"})
            return {"needs_plan_approval": False}

        async def approval_gate(state):
            raise AssertionError("approval gate should be skipped")

        async def dispatch(state):
            return {
                "done": False,
                "group_token": 1,
                "dispatch_agents": ["TechAgent", "RiskAgent"],
                "execution_snapshot": {"runId": "run-lg-test"},
            }

        async def agent(task):
            await asyncio.sleep(0)
            return {"agent_results": [{
                "groupToken": task["group_token"],
                "agentId": task["agent_id"],
            }]}

        async def merge(state):
            assert {result["agentId"] for result in state["agent_results"]} == {
                "TechAgent", "RiskAgent"}
            return Command(goto="replan", update={"group_ok": True})

        async def replan(state):
            return Command(goto="finalize", update={"replanned": True})

        async def pause_gate(state):
            raise AssertionError("pause gate should be skipped")

        async def finalize(state):
            return {"done": True, "result": {"status": "SUCCEEDED"}}

        executor._observe_plan_node = observe_plan
        executor._approval_gate_node = approval_gate
        executor._dispatch_node = dispatch
        executor._agent_node = agent
        executor._merge_node = merge
        executor._replan_node = replan
        executor._pause_gate_node = pause_gate
        executor._finalize_node = finalize

        saver = InMemorySaver()
        graph = executor.build_graph(saver)
        config = {"configurable": {"thread_id": "run-lg-test"}}
        custom = []
        async for mode, chunk in graph.astream(
                {"run_id": "run-lg-test", "agent_results": []},
                config,
                stream_mode=["updates", "custom"],
                durability="sync"):
            if mode == "custom":
                custom.append(chunk)

        snapshot = await graph.aget_state(config)
        assert snapshot.config["configurable"]["thread_id"] == "run-lg-test"
        assert snapshot.values["result"]["status"] == "SUCCEEDED"
        assert snapshot.values["replanned"] is True
        assert {item["agentId"] for item in snapshot.values["agent_results"]} == {
            "TechAgent", "RiskAgent"}
        assert custom == [{"stage": "test.plan"}]

    run(scenario())


def test_postgres_checkpoint_resume_contract_uses_same_thread_id():
    class PauseState(TypedDict, total=False):
        resumed: bool
        values: Annotated[list[str], operator.add]

    async def gate(state):
        payload = interrupt({"pauseReason": "test"})
        return Command(
            goto=END,
            update={"resumed": bool(payload), "values": ["after-resume"]},
        )

    async def scenario():
        saver = InMemorySaver()
        builder = StateGraph(PauseState)
        builder.add_node("gate", gate)
        builder.add_edge(START, "gate")
        graph = builder.compile(checkpointer=saver)
        config = {"configurable": {"thread_id": "run-checkpoint-test"}}

        await graph.ainvoke({"values": ["before-pause"]}, config)
        paused = await graph.aget_state(config)
        assert paused.next == ("gate",)
        assert any(task.interrupts for task in paused.tasks)

        await graph.ainvoke(Command(resume={"approved": True}), config)
        resumed = await graph.aget_state(config)
        assert resumed.next == ()
        assert resumed.values["resumed"] is True
        assert resumed.values["values"] == ["before-pause", "after-resume"]
        assert resumed.config["configurable"]["thread_id"] == (
            "run-checkpoint-test")

    run(scenario())
