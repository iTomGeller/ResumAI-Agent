"""Analyze new trace performance."""
import requests
import json

BASE = "http://8.138.10.189"
tid = "trace-29e41391-ecdd-47ba-b81a-0270ae480c47"

r = requests.get(f"{BASE}/api/ops/runs", params={"traceId": tid}, timeout=10)
data = r.json()
run = data["items"][0]

print("=== RUN METADATA ===")
print(f"RunId: {run['runId']}")
print(f"Status: {run['status']}")
print(f"PolicyId: {run.get('policyId')}")

metrics = run.get("metrics") or {}
print(f"\n=== METRICS ===")
print(f"LLM Calls: {metrics.get('llmCalls')}")
print(f"Tool Calls: {metrics.get('toolCalls')}")
print(f"Prompt Tokens: {metrics.get('promptTokens')}")
print(f"Completion Tokens: {metrics.get('completionTokens')}")
print(f"Cost CNY: {metrics.get('costCny')}")
print(f"Latency: {metrics.get('latencySeconds')}s")
print(f"Agents Used: {metrics.get('agentsUsed')}")

timings = metrics.get("agentTimingsMs") or {}
print(f"\n=== AGENT TIMINGS (sorted) ===")
total_agent = 0
for agent, ms in sorted(timings.items(), key=lambda x: -x[1]):
    print(f"  {agent}: {ms}ms ({ms/1000:.1f}s)")
    total_agent += ms
print(f"  [Total agent time]: {total_agent}ms ({total_agent/1000:.1f}s)")

print(f"\nDegraded Reasons: {metrics.get('degradedReasons')}")

# Get events
run_id = run["runId"]
r2 = requests.get(f"{BASE}/api/ops/runs/{run_id}", timeout=10)
if r2.status_code == 200:
    detail = r2.json()
    events = detail.get("events") or []
    print(f"\n=== EVENTS ({len(events)} total) ===")
    
    llm_events = []
    retry_events = []
    agent_events = []
    
    for ev in events:
        et = ev.get("eventType", "")
        payload = ev.get("payload") or {}
        agent = ev.get("agentId", "")
        
        if "llm.completed" in et or "llm.success" in et:
            llm_events.append({
                "agent": agent,
                "model": payload.get("model", "?"),
                "pt": payload.get("promptTokens", 0),
                "ct": payload.get("completionTokens", 0),
                "dur": payload.get("durationMs", 0),
                "finish": payload.get("finishReason", "?"),
                "thinking": payload.get("thinkingTokens", 0),
            })
        elif "llm.retrying" in et or "retry" in et.lower():
            retry_events.append({
                "agent": agent,
                "reason": payload.get("reason", "?"),
                "attempt": payload.get("attempt", "?"),
                "error": payload.get("error", "")[:100],
            })
        elif "agent.started" in et or "agent.completed" in et:
            agent_events.append({
                "type": et,
                "agent": agent,
                "dur": payload.get("durationMs", 0),
            })
    
    print(f"\n--- LLM Calls ({len(llm_events)}) ---")
    for e in llm_events:
        print(f"  {e['agent']}: model={e['model']} pt={e['pt']} ct={e['ct']} dur={e['dur']}ms finish={e['finish']} thinking={e['thinking']}")
    
    print(f"\n--- Retries ({len(retry_events)}) ---")
    for e in retry_events:
        print(f"  {e['agent']}: reason={e['reason']} attempt={e['attempt']} error={e['error']}")
    
    print(f"\n--- Agent Lifecycle ---")
    for e in agent_events:
        if e['dur']:
            print(f"  {e['type']}: {e['agent']} dur={e['dur']}ms ({e['dur']/1000:.1f}s)")
        else:
            print(f"  {e['type']}: {e['agent']}")
