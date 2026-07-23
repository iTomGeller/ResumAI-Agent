"""Check the new post-fix trace performance."""
import requests
import json

BASE = "http://8.138.10.189"
tid = "trace-55776c9b-3709-432e-ad93-4ebf65d932cd"

r = requests.get(f"{BASE}/api/ops/runs", params={"traceId": tid}, timeout=10)
data = r.json()
run = data["items"][0]

metrics = run.get("metrics") or {}
print(f"RunType: {run.get('runType')}")
print(f"Status: {run.get('status')} | Error: {run.get('errorCode')}")
print(f"LLM Calls: {metrics.get('llmCalls')}")
print(f"Tool Calls: {metrics.get('toolCalls')}")
print(f"Prompt Tokens: {metrics.get('promptTokens')}")
print(f"Completion Tokens: {metrics.get('completionTokens')}")
print(f"Latency: {metrics.get('latencySeconds')}s")
print(f"Degraded: {metrics.get('degradedReasons')}")

timings = metrics.get("agentTimingsMs") or {}
print(f"\nAgent Timings:")
for agent, ms in sorted(timings.items(), key=lambda x: -x[1]):
    print(f"  {agent}: {ms/1000:.1f}s")

# Get detailed LLM events
run_id = run["runId"]
r2 = requests.get(f"{BASE}/api/ops/runs/{run_id}", params={"eventLimit": 200}, timeout=10)
detail = r2.json()
events = detail.get("events") or []

print(f"\nLLM Events:")
for ev in events:
    et = ev.get("eventType", "")
    if "llm" not in et:
        continue
    agent = ev.get("agentId", "")
    payload = ev.get("payload") or {}
    model = payload.get("model", "")
    pt = payload.get("promptTokens", 0)
    ct = payload.get("completionTokens", 0)
    dur = payload.get("durationMs", 0)
    reason = payload.get("reason", "")
    attempt = payload.get("attempt", "")
    
    if et == "llm.completed":
        print(f"  {agent}: prompt={pt} completion={ct} dur={dur/1000:.1f}s model={model}")
    elif et == "llm.retrying":
        print(f"  RETRY {agent}: reason={reason} attempt={attempt}")
