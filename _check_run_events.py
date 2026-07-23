"""Get run events through ops API."""
import requests
import json

BASE = "http://8.138.10.189"
tid = "trace-2b22d983-08a1-4292-82d9-292348763f2d"

# Try ops API
r = requests.get(f"{BASE}/api/ops/runs", params={"traceId": tid}, timeout=10)
print(f"Ops runs: {r.status_code}")
if r.status_code == 200:
    data = r.json()
    runs = data.get("runs") or data.get("content") or (data if isinstance(data, list) else [data])
    if isinstance(runs, dict) and "runId" in runs:
        runs = [runs]
    elif isinstance(runs, dict):
        runs = runs.get("runs") or runs.get("content") or []
    print(f"Found runs: {len(runs) if isinstance(runs, list) else 'dict'}")
    print(f"Data keys: {list(data.keys()) if isinstance(data, dict) else 'list'}")
    if isinstance(runs, list) and runs:
        run = runs[0] if isinstance(runs[0], dict) else {}
        run_id = run.get("runId", "")
        print(f"RunId: {run_id}")
        print(f"Run status: {run.get('status')}")
        print(f"Run keys: {list(run.keys())[:15]}")
        
        if run_id:
            # Get run detail
            r2 = requests.get(f"{BASE}/api/ops/runs/{run_id}", timeout=10)
            print(f"\nRun detail: {r2.status_code}")
            if r2.status_code == 200:
                detail = r2.json()
                events = detail.get("events") or []
                print(f"Events: {len(events)}")
                
                # Analyze events
                agent_starts = {}
                agent_ends = {}
                llm_calls = []
                tool_calls_list = []
                
                for ev in events:
                    et = ev.get("eventType") or ev.get("type") or ""
                    agent_id = ev.get("agentId") or ""
                    payload = ev.get("payload") or {}
                    ts = ev.get("createdAt") or ev.get("timestamp") or ""
                    
                    if et == "agent.started":
                        agent_starts[agent_id] = ts
                    elif et == "agent.completed" or et == "agent.failed":
                        dur = payload.get("durationMs", 0)
                        agent_ends[agent_id] = {"duration": dur, "status": et}
                    elif et == "llm.completed":
                        llm_calls.append({
                            "agent": agent_id,
                            "model": payload.get("model", "?"),
                            "promptTokens": payload.get("promptTokens", 0),
                            "completionTokens": payload.get("completionTokens", 0),
                            "durationMs": payload.get("durationMs", 0),
                        })
                    elif et == "tool.completed" or et == "tool.started":
                        tool_calls_list.append({
                            "agent": agent_id,
                            "tool": ev.get("toolName") or payload.get("toolName", "?"),
                            "type": et,
                            "kind": payload.get("kind", "?"),
                        })
                
                print(f"\n=== AGENT PERFORMANCE ===")
                for aid, info in agent_ends.items():
                    print(f"  {aid}: {info['duration']}ms ({info['status']})")
                
                print(f"\n=== LLM CALLS ({len(llm_calls)}) ===")
                total_prompt = 0
                total_completion = 0
                for lc in llm_calls:
                    total_prompt += lc["promptTokens"]
                    total_completion += lc["completionTokens"]
                    print(f"  {lc['agent']}: model={lc['model']} prompt={lc['promptTokens']} completion={lc['completionTokens']} dur={lc['durationMs']}ms")
                print(f"  TOTAL: prompt={total_prompt} completion={total_completion}")
                
                print(f"\n=== TOOL CALLS ===")
                tools_completed = [t for t in tool_calls_list if t["type"] == "tool.completed"]
                for tc in tools_completed[:10]:
                    print(f"  {tc['agent']}: {tc['tool']} kind={tc['kind']}")
                
                # Categories
                categories = {}
                for ev in events:
                    cat = ev.get("category", "UNKNOWN")
                    categories[cat] = categories.get(cat, 0) + 1
                print(f"\n=== EVENT CATEGORIES ===")
                for cat, count in sorted(categories.items()):
                    print(f"  {cat}: {count}")
    else:
        print(f"Response: {json.dumps(data, indent=2, ensure_ascii=False)[:2000]}")
else:
    print(f"Response: {r.text[:500]}")

# Also try RunController
r3 = requests.get(f"{BASE}/api/runs/{tid.replace('trace-','run-')}", timeout=5)
print(f"\nRunController /api/runs/run-...: {r3.status_code}")
