"""Check skills usage in latest trace."""
import requests
import json

BASE = "http://8.138.10.189"
tid = "trace-f46c230d-925c-4b0b-b7c9-96e010a6d2aa"

r = requests.get(f"{BASE}/api/ops/runs", params={"traceId": tid}, timeout=10)
run = r.json()["items"][0]
run_id = run["runId"]

# Check skill versions
sv = run.get("skillVersions") or {}
print(f"=== SKILL VERSIONS USED ===")
for k, v in sv.items():
    print(f"  {k}: {v}")

# Get events and filter skill events
r2 = requests.get(f"{BASE}/api/ops/runs/{run_id}", timeout=10)
detail = r2.json()
events = detail.get("events") or []

print(f"\n=== SKILL EVENTS ===")
skill_events = [e for e in events if "skill" in e.get("eventType", "").lower()]
for e in skill_events:
    et = e.get("eventType")
    agent = e.get("agentId", "")
    tool = e.get("toolName", "")
    payload = e.get("payload") or {}
    print(f"  {et} agent={agent} skill={tool}")
    for k, v in payload.items():
        if k not in ("agentId",):
            print(f"    {k}: {v}")

print(f"\n=== MEMORY EVENTS ===")
memory_events = [e for e in events if "memory" in e.get("eventType", "").lower()]
for e in memory_events:
    et = e.get("eventType")
    agent = e.get("agentId", "")
    payload = e.get("payload") or {}
    print(f"  {et} agent={agent}")
    for k, v in list(payload.items())[:5]:
        print(f"    {k}: {str(v)[:100]}")

print(f"\n=== CONTEXT EVENTS ===")
context_events = [e for e in events if "context" in e.get("eventType", "").lower()]
for e in context_events:
    et = e.get("eventType")
    agent = e.get("agentId", "")
    payload = e.get("payload") or {}
    print(f"  {et} agent={agent}")
    for k, v in list(payload.items())[:5]:
        print(f"    {k}: {str(v)[:80]}")
