"""Full run analysis."""
import requests
import json

BASE = "http://8.138.10.189"
tid = "trace-2b22d983-08a1-4292-82d9-292348763f2d"

r = requests.get(f"{BASE}/api/ops/runs", params={"traceId": tid}, timeout=10)
data = r.json()
run = data["items"][0]

print("=== RUN METADATA ===")
print(f"RunId: {run['runId']}")
print(f"RunType: {run['runType']}")
print(f"Status: {run['status']}")
print(f"ErrorCode: {run.get('errorCode')}")
print(f"ErrorMessage: {run.get('errorMessage')}")
print(f"PolicyId: {run.get('policyId')}")
print(f"CurrentAgent: {run.get('currentAgent')}")

metrics = run.get("metrics") or {}
print(f"\n=== METRICS ===")
print(f"LLM Calls: {metrics.get('llmCalls')}")
print(f"Tool Calls: {metrics.get('toolCalls')}")
print(f"Tool Failures: {metrics.get('toolFailures')}")
print(f"Prompt Tokens: {metrics.get('promptTokens')}")
print(f"Completion Tokens: {metrics.get('completionTokens')}")
print(f"Cost CNY: {metrics.get('costCny')}")
print(f"Latency: {metrics.get('latencySeconds')}s")
print(f"JD Coverage: {metrics.get('jdCoverage')}")
print(f"Agents Used: {metrics.get('agentsUsed')}")

timings = metrics.get("agentTimingsMs") or {}
print(f"\n=== AGENT TIMINGS ===")
for agent, ms in sorted(timings.items(), key=lambda x: -x[1]):
    print(f"  {agent}: {ms}ms ({ms/1000:.1f}s)")

print(f"\nDegraded Reasons: {metrics.get('degradedReasons')}")
print(f"Loop Guard Trips: {metrics.get('loopGuardTrips')}")

# Skills
print(f"\n=== SKILL VERSIONS ===")
for sk, ver in (run.get("skillVersions") or {}).items():
    print(f"  {sk}: {ver}")

# Get run detail with events
run_id = run["runId"]
r2 = requests.get(f"{BASE}/api/ops/runs/{run_id}", timeout=10)
print(f"\n=== RUN DETAIL ===")
print(f"Detail status: {r2.status_code}")
if r2.status_code == 200:
    detail = r2.json()
    events = detail.get("events") or []
    print(f"Events: {len(events)}")
    
    # Show key events
    for ev in events[:40]:
        et = ev.get("eventType", "")
        agent = ev.get("agentId", "")
        tool = ev.get("toolName", "")
        payload = ev.get("payload") or {}
        cat = ev.get("category", "")
        
        if "llm" in et:
            model = payload.get("model", "")
            pt = payload.get("promptTokens", 0)
            ct = payload.get("completionTokens", 0)
            dur = payload.get("durationMs", 0)
            reason = payload.get("reason", "")
            print(f"  [{cat}] {et} agent={agent} model={model} pt={pt} ct={ct} dur={dur}ms {reason}")
        elif "agent.selected" in et or "agent.started" in et or "agent.completed" in et or "agent.failed" in et:
            dur = payload.get("durationMs", 0)
            reason = payload.get("reason", "")[:80]
            plan = payload.get("plan", [])
            groups = payload.get("parallelGroups", [])
            if plan:
                print(f"  [{cat}] {et} plan={plan} groups={groups}")
            else:
                print(f"  [{cat}] {et} agent={agent} dur={dur}ms {reason}")
        elif "tool" in et:
            kind = payload.get("kind", "?")
            dur = payload.get("durationMs", 0)
            print(f"  [{cat}] {et} agent={agent} tool={tool} kind={kind} dur={dur}ms")
        elif "skill" in et or "memory" in et or "context" in et:
            print(f"  [{cat}] {et} agent={agent} {json.dumps(payload, ensure_ascii=False)[:100]}")
else:
    print(f"  {r2.text[:500]}")
