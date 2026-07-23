"""Compare multiple candidates for route diversity."""
import requests
import json

BASE = "http://8.138.10.189"

# Get candidate list
r = requests.get(f"{BASE}/api/candidates", params={"page": 1, "size": 20}, timeout=10)
data = r.json()
items = data.get("items") or data.get("content") or []
print(f"Total candidates: {data.get('total', len(items))}")

# For each candidate with a completed evaluation, get their trace details
results = []
for item in items[:15]:
    trace_id = item.get("traceId") or item.get("latestTraceId") or ""
    name = item.get("name") or item.get("candidateName") or "?"
    score = item.get("overallScore") or item.get("score")
    status = item.get("status") or item.get("evaluationStatus") or "?"
    rec = item.get("recommendation") or ""
    
    if not trace_id:
        continue
    
    # Get run details
    try:
        r2 = requests.get(f"{BASE}/api/ops/runs", params={"traceId": trace_id}, timeout=5)
        if r2.status_code != 200:
            continue
        runs = r2.json().get("items") or []
        if not runs:
            continue
        run = runs[0]
        metrics = run.get("metrics") or {}
        timings = metrics.get("agentTimingsMs") or {}
        
        results.append({
            "name": name[:12],
            "traceId": trace_id[-8:],
            "status": run.get("status", "?"),
            "score": score,
            "rec": rec[:15],
            "agents": metrics.get("agentsUsed", []),
            "llmCalls": metrics.get("llmCalls", 0),
            "toolCalls": metrics.get("toolCalls", 0),
            "retries": sum(1 for e in (requests.get(f"{BASE}/api/ops/runs/{run['runId']}", timeout=5).json().get("events") or []) if "retry" in e.get("eventType", "").lower()) if run.get("runId") else 0,
            "latency": metrics.get("latencySeconds", 0),
            "promptTokens": metrics.get("promptTokens", 0),
            "completionTokens": metrics.get("completionTokens", 0),
        })
    except Exception as e:
        continue

print(f"\nAnalyzed {len(results)} candidates with run data\n")

# Print comparison table
print(f"{'Name':<12} {'Trace':<10} {'Status':<10} {'Score':<6} {'Rec':<16} {'LLM':<5} {'Tool':<5} {'Retry':<6} {'Lat(s)':<8} {'Agents'}")
print("-" * 140)
for r in results:
    agents_str = ",".join(a.replace("Agent", "") for a in r["agents"])[:40]
    print(f"{r['name']:<12} {r['traceId']:<10} {r['status']:<10} {str(r['score']):<6} {r['rec']:<16} {r['llmCalls']:<5} {r['toolCalls']:<5} {r['retries']:<6} {r['latency']:<8} {agents_str}")

# Check route diversity
all_agent_sets = [frozenset(r["agents"]) for r in results if r["agents"]]
unique_sets = set(all_agent_sets)
print(f"\n=== ROUTE DIVERSITY ===")
print(f"Total runs with agents: {len(all_agent_sets)}")
print(f"Unique agent combinations: {len(unique_sets)}")
for s in unique_sets:
    count = all_agent_sets.count(s)
    agents_short = ",".join(a.replace("Agent", "") for a in sorted(s))
    print(f"  [{count}x] {agents_short}")
