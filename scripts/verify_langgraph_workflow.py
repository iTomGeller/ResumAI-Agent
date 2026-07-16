"""Verify LangGraph workflow trace integrity — strict gate before pressure tests."""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE_NODES = {
    "intent",
    "resume_parse",
    "jd_match",
    "evidence_fusion",
    "report",
}
OPTIONAL_NODES = {
    "tech_eval",
    "project_eval",
    "risk_eval",
}
EXPECTED_NODES = BASE_NODES | OPTIONAL_NODES


def http_json(url: str, method: str = "GET", payload: dict | None = None, timeout: int = 60) -> dict:
    data = None
    headers = {"Content-Type": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def load_deploy_env() -> dict[str, str]:
    env: dict[str, str] = {}
    path = Path(__file__).resolve().parents[1] / ".deploy.local.env"
    if not path.exists():
        return env
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip()
    return env


def fetch_ssh_logs(base: str) -> None:
    env = load_deploy_env()
    host = env.get("ALIYUN_HOST")
    if not host:
        print("[diag] ALIYUN_HOST not set, skip ssh logs")
        return
    try:
        import paramiko
    except ImportError:
        print("[diag] paramiko not installed, skip ssh logs")
        return
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        ssh.connect(
            host,
            username=env.get("ALIYUN_USER", "root"),
            password=env.get("ALIYUN_PASSWORD", ""),
            look_for_keys=False,
            allow_agent=False,
            timeout=20,
        )
        for container in ("ai-resume-workflow", "ai-resume-backend"):
            _, stdout, _ = ssh.exec_command(f"docker logs {container} --tail 80 2>&1", timeout=30)
            out = stdout.read().decode("utf-8", errors="replace")
            print(f"\n[diag] === {container} (last 80 lines) ===")
            print(out[-6000:] if len(out) > 6000 else out)
    except Exception as exc:
        print(f"[diag] ssh logs failed: {exc}")
    finally:
        ssh.close()


def print_failure_diagnostics(base: str, trace_id: str, detail: dict | None, tree: dict | None) -> None:
    print("\n[diag] task detail:")
    print(json.dumps(detail or {}, ensure_ascii=False, indent=2)[:4000])
    if tree:
        print("\n[diag] agent-execution summary:")
        for agent in tree.get("executionTree", [])[-3:]:
            node_id = agent.get("nodeId") or agent.get("name")
            rounds = agent.get("rounds", [])
            print(f"  node={node_id} rounds={len(rounds)} status={agent.get('status')}")
            for rnd in rounds[-5:]:
                print(
                    f"    round={rnd.get('roundNum')} event={rnd.get('eventId')} "
                    f"type={rnd.get('type')} tools={len(rnd.get('toolCalls') or [])} "
                    f"orphan={len(rnd.get('orphanToolCalls') or [])}"
                )
    if "--ssh-logs" in sys.argv:
        fetch_ssh_logs(base)


def extract_harness_plan(tree: dict) -> dict:
    direct = tree.get("harnessPlan")
    if isinstance(direct, dict) and direct.get("version"):
        return direct
    for agent in tree.get("executionTree", []):
        for round in agent.get("rounds", []):
            for field in ("input", "output", "decisionText"):
                text = str(round.get(field) or "")
                if "harnessPlan" not in text and "AgentHarness" not in text:
                    continue
                try:
                    parsed = json.loads(text)
                    if isinstance(parsed.get("harnessPlan"), dict):
                        return parsed["harnessPlan"]
                except Exception:
                    pass
                marker = text.find("AgentHarness")
                if marker >= 0:
                    start = text.find("{", marker)
                    if start >= 0:
                        depth = 0
                        for idx in range(start, len(text)):
                            if text[idx] == "{":
                                depth += 1
                            elif text[idx] == "}":
                                depth -= 1
                            if depth == 0:
                                try:
                                    parsed = json.loads(text[start : idx + 1])
                                    if parsed.get("version"):
                                        return parsed
                                except Exception:
                                    pass
                                break
    return {}


def strict_trace_checks(tree: dict, allow_orphan: bool) -> None:
    execution_tree = tree.get("executionTree", [])
    node_ids = {agent.get("nodeId") or agent.get("name") for agent in execution_tree}
    missing_base = BASE_NODES - {n for n in node_ids if n}
    if missing_base:
        raise SystemExit(f"missing base agent nodes: {sorted(missing_base)}")

    harness = extract_harness_plan(tree)
    route = harness.get("route") if isinstance(harness.get("route"), dict) else {}
    selected = set(route.get("selectedAgents") or route.get("enabledAgents") or [])
    if selected:
        unexpected = {n for n in node_ids if n in OPTIONAL_NODES and n not in selected}
        if unexpected:
            raise SystemExit(f"unexpected optional nodes executed: {sorted(unexpected)}")
        missing_selected = {n for n in selected if n in OPTIONAL_NODES and n not in node_ids}
        if missing_selected:
            raise SystemExit(f"selected agents missing from trace: {sorted(missing_selected)}")
        print(f"[verify] routeMode={route.get('routeMode')} selected={sorted(selected)}")
    else:
        optional_present = {n for n in node_ids if n in OPTIONAL_NODES}
        if not optional_present:
            raise SystemExit(f"missing optional eval nodes: expected at least one of {sorted(OPTIONAL_NODES)}")
        print(f"[verify] optional nodes executed: {sorted(optional_present)} (no harnessPlan in trace)")

    duplicate_rounds = 0
    seen_keys: set[str] = set()
    has_messages = False
    has_tool_io = False
    bad_round_types = []

    for agent in execution_tree:
        node_id = agent.get("nodeId") or agent.get("name")
        for round in agent.get("rounds", []):
            if round.get("type") == "trace_integrity_warning":
                continue
            if round.get("type") == "tool_call":
                bad_round_types.append(f"{node_id}:round{round.get('roundNum')}")

            event_id = round.get("eventId") or round.get("id")
            if not event_id and not round.get("orphan"):
                raise SystemExit(
                    f"missing eventId: node={node_id} round={round.get('roundNum')} keys={list(round.keys())}"
                )
            round_num = round.get("roundNum")
            key = f"{node_id}:{round_num}:{event_id}"
            if key in seen_keys:
                duplicate_rounds += 1
            seen_keys.add(key)

            if round.get("hasToolCalls") and not round.get("toolCalls"):
                raise SystemExit(
                    f"hasToolCalls but empty toolCalls: node={node_id} round={round_num} event={event_id}"
                )
            if round.get("final") and not (round.get("finalOutput") or round.get("output")):
                raise SystemExit(
                    f"final round without output: node={node_id} round={round_num} event={event_id}"
                )

            msgs = round.get("inputMessages")
            out_msg = round.get("outputMessage")
            if isinstance(msgs, list) and msgs and out_msg:
                has_messages = True

            for tool in round.get("toolCalls", []):
                if tool.get("name") and tool.get("input") is not None and tool.get("output") is not None:
                    has_tool_io = True
                if not tool.get("status"):
                    raise SystemExit(f"tool missing status: node={node_id} tool={tool.get('name')}")

            orphan = round.get("orphanToolCalls") or []
            if orphan and not allow_orphan:
                raise SystemExit(f"orphan tools detected: node={node_id} count={len(orphan)}")

    if duplicate_rounds > 0:
        raise SystemExit(f"duplicate rounds detected: {duplicate_rounds}")
    if bad_round_types:
        raise SystemExit(f"invalid round types (tool_call): {bad_round_types}")
    if not has_messages:
        raise SystemExit("no rounds with inputMessages + outputMessage")
    if not has_tool_io:
        print("[warn] no tool with input+output found — workflow may have skipped tools")

    print(f"[verify] nodes={len(node_ids)} duplicateRounds={duplicate_rounds}")
    print(f"[verify] hasInputMessages={has_messages} hasToolIO={has_tool_io}")


def count_tools_by_agent(tree: dict) -> dict[str, list[dict]]:
    counts: dict[str, list[dict]] = {}
    for agent in tree.get("executionTree", []):
        name = agent.get("name") or agent.get("nodeId") or "unknown"
        tools: list[dict] = []
        for round in agent.get("rounds", []):
            if round.get("type") == "trace_integrity_warning":
                continue
            for tool in round.get("toolCalls", []) or []:
                tools.append(tool)
        counts[name] = tools
    return counts


def deterministic_tool_budget_checks(tree: dict) -> None:
    by_agent = count_tools_by_agent(tree)
    resume_tools = by_agent.get("ResumeParseAgent", [])
    if len(resume_tools) > 1:
        raise SystemExit(f"ResumeParseAgent tool count {len(resume_tools)} > 1")

    jd_tools = by_agent.get("JdMatchAgent", [])
    if len(jd_tools) > 2:
        raise SystemExit(f"JdMatchAgent tool count {len(jd_tools)} > 2")

    for agent_name in ("TechEvalAgent", "ProjectEvalAgent"):
        batch = [t for t in by_agent.get(agent_name, []) if t.get("name") == "milvus_resume_batch_search"]
        if len(batch) > 1:
            raise SystemExit(f"{agent_name} milvus_resume_batch_search count {len(batch)} > 1")

    fusion_tools = by_agent.get("EvidenceFusionAgent", [])
    if fusion_tools:
        raise SystemExit(f"EvidenceFusionAgent should have 0 tools, got {len(fusion_tools)}")

    for agent_name, tools in by_agent.items():
        for tool in tools:
            name = tool.get("name") or ""
            if "neo4j" in name.lower() or name == "neo4j_graph_query":
                raise SystemExit(f"neo4j tool still present: agent={agent_name} tool={name}")

    report_text = ""
    for agent in tree.get("executionTree", []):
        if agent.get("name") == "ReportAgent":
            report_text = str(agent.get("output") or "")
            for round in agent.get("rounds", []):
                report_text += str(round.get("finalOutput") or round.get("output") or "")
    if "知识图谱" in report_text or "GraphRAG" in report_text or "Neo4j" in report_text:
        raise SystemExit("report still references graph/neo4j")

    print(f"[verify] tool budgets ok: resume={len(resume_tools)} jd={len(jd_tools)} fusion={len(fusion_tools)}")


def main() -> None:
    args = [a for a in sys.argv[1:] if a.startswith("--")]
    positional = [a for a in sys.argv[1:] if not a.startswith("--")]
    base = positional[0] if positional else "http://127.0.0.1"
    strict = "--strict-trace" in args
    allow_orphan = "--allow-orphan-tools" in args

    resume_text = (
        "李四，6年Java后端，Spring Boot、Kafka、K8s。"
        "主导支付中台重构，负责核心模块设计与上线，QPS从1万提升到3万，P99降低40%。"
        "参与Agent/RAG智能体平台，实现DAG编排、Tool调用和Trace可观测。"
        "GitHub: https://github.com/example-dev"
    )
    create_payload = {
        "fileName": "verify-langgraph-e2e.txt",
        "jobCategory": "TECH",
        "executionMode": "AUTO",
        "resumeText": resume_text,
    }
    task = http_json(f"{base}/api/tasks", "POST", create_payload)
    trace_id = task.get("traceId")
    if not trace_id:
        raise SystemExit(f"create task failed: {task}")

    print(f"[verify] created trace={trace_id}")
    deadline = time.time() + 600
    status = "RUNNING"
    detail: dict | None = None
    while time.time() < deadline:
        detail = http_json(f"{base}/api/tasks/{trace_id}")
        status = detail.get("status", "RUNNING")
        qs = (detail.get("queue") or {}).get("queueStatus") or detail.get("queueStatus")
        summary = (detail.get("evaluationSummary") or detail.get("summary") or "")[:120]
        print(f"[verify] status={status} queue={qs} summary={summary}")
        if status in ("SUCCESS", "FAILED"):
            break
        time.sleep(8)

    tree: dict | None = None
    try:
        tree = http_json(f"{base}/api/tasks/{trace_id}/agent-execution")
    except Exception:
        pass

    if status != "SUCCESS":
        print_failure_diagnostics(base, trace_id, detail, tree)
        raise SystemExit(f"task did not succeed: status={status} trace={trace_id}")

    framework = (tree or {}).get("framework", "")
    assert "LangGraph" in framework, f"framework mismatch: {framework}"

    if strict:
        strict_trace_checks(tree or {}, allow_orphan)
        deterministic_tool_budget_checks(tree or {})
    else:
        duplicate_rounds = 0
        seen_keys: set[str] = set()
        for agent in (tree or {}).get("executionTree", []):
            node_id = agent.get("nodeId") or agent.get("name")
            for round in agent.get("rounds", []):
                event_id = round.get("eventId") or round.get("id")
                key = f"{node_id}:{round.get('roundNum')}:{event_id}"
                if key in seen_keys:
                    duplicate_rounds += 1
                seen_keys.add(key)
        if duplicate_rounds > 0:
            raise SystemExit("duplicate rounds detected")

    print(f"[ok] verify_langgraph_workflow passed trace={trace_id}")
    print(f"[ok] deep-link: {base}/#/task/{trace_id}/trace")


if __name__ == "__main__":
    try:
        main()
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise SystemExit(f"HTTP {exc.code}: {body}") from exc
