"""Get detailed LLM events for the new trace."""
import requests
import json

BASE = "http://8.138.10.189"
run_id = "run-40db4fe2-b160-4420-bf3a-4af12b9096be"

r = requests.get(f"{BASE}/api/ops/runs/{run_id}", params={"eventLimit": 200}, timeout=10)
if r.status_code != 200:
    print(f"Error: {r.status_code}")
    exit(1)

detail = r.json()
events = detail.get("events") or []

print(f"Total events: {len(events)}")
print(f"\n=== LLM EVENTS ===")
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
    purpose = payload.get("purpose", "")
    reason = payload.get("reason", "")
    attempt = payload.get("attempt", "")
    finish = payload.get("finishReason", "")
    
    print(f"  {et}: agent={agent} model={model} purpose={purpose}")
    print(f"    tokens: prompt={pt} completion={ct} dur={dur}ms finish={finish}")
    if reason:
        print(f"    reason: {reason}")
    if attempt:
        print(f"    attempt: {attempt}")
    print()

print(f"\n=== AGENT EVENTS (start/complete/fail) ===")
for ev in events:
    et = ev.get("eventType", "")
    if et not in ("agent.started", "agent.completed", "agent.failed", "agent.selected"):
        continue
    agent = ev.get("agentId", "")
    payload = ev.get("payload") or {}
    dur = payload.get("durationMs", 0)
    plan = payload.get("plan", [])
    groups = payload.get("parallelGroups", [])
    reason = payload.get("reason", "")
    
    if plan:
        print(f"  {et}: plan={plan}")
        print(f"    groups={groups}")
    else:
        extra = ""
        if dur:
            extra = f" dur={dur}ms"
        if reason:
            extra += f" reason={reason[:80]}"
        print(f"  {et}: agent={agent}{extra}")

print(f"\n=== MEMORY/SKILL/CONTEXT EVENTS ===")
for ev in events:
    et = ev.get("eventType", "")
    if "memory" in et or "skill" in et or "context" in et or "compact" in et:
        agent = ev.get("agentId", "")
        payload = ev.get("payload") or {}
        print(f"  {et}: agent={agent} {json.dumps(payload, ensure_ascii=False)[:120]}")
