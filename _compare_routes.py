"""Compare routes across multiple candidates."""
import requests
import json

BASE = "http://8.138.10.189"

# Get task list
r = requests.get(f"{BASE}/api/tasks", params={"page": 1, "size": 20}, timeout=10)
data = r.json()
items = data.get("items") or []

print(f"{'traceId':<55} {'status':<17} {'score':<6} {'dur(s)':<8} {'category':<10}")
print("-" * 110)

comparison = []
for item in items[:10]:
    tid = item.get("traceId", "")
    status = item.get("status", "?")
    score = item.get("overallScore")
    dur = (item.get("durationMs") or 0) / 1000
    cat = item.get("jobCategory", "?")
    print(f"{tid:<55} {status:<17} {str(score):<6} {dur:<8.1f} {cat:<10}")
    comparison.append({"traceId": tid, "status": status, "score": score, "duration": dur})

# Get run details for first 6 to compare routes
print(f"\n{'='*80}")
print("ROUTE COMPARISON")
print(f"{'='*80}")

for item in items[:8]:
    tid = item.get("traceId", "")
    if not tid:
        continue
    
    r2 = requests.get(f"{BASE}/api/ops/runs", params={"traceId": tid}, timeout=10)
    if r2.status_code != 200:
        continue
    d2 = r2.json()
    runs = d2.get("items") or []
    if not runs:
        continue
    run = runs[0]
    metrics = run.get("metrics") or {}
    timings = metrics.get("agentTimingsMs") or {}
    agents_used = metrics.get("agentsUsed") or []
    llm_calls = metrics.get("llmCalls", 0)
    tool_calls = metrics.get("toolCalls", 0)
    prompt_tokens = metrics.get("promptTokens", 0)
    
    print(f"\n  Trace: {tid}")
    print(f"  RunType: {run.get('runType')} | Policy: {run.get('policyId')}")
    print(f"  Status: {run.get('status')} | Error: {run.get('errorCode', '-')}")
    print(f"  Agents: {agents_used}")
    print(f"  LLM: {llm_calls} | Tools: {tool_calls} | Tokens: {prompt_tokens}")
    print(f"  Timings: {json.dumps(timings, ensure_ascii=False)}")
    
    # Skills
    skills = run.get("skillVersions") or {}
    print(f"  Skills({len(skills)}): {list(skills.keys())[:5]}")
