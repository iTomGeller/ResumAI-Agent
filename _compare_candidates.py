"""Compare 10 candidates routing - full matrix"""
import requests, json

BASE = "http://8.138.10.189/api"

# Get diverse tasks
resp = requests.get(f"{BASE}/tasks", params={"page": 1, "pageSize": 30})
tasks = resp.json().get("items", [])

seen_files = set()
diverse_tasks = []
for t in tasks:
    fname = t.get("fileName", "") or ""
    if fname and fname not in seen_files and t.get("status") != "FAILED":
        seen_files.add(fname)
        diverse_tasks.append(t)
    if len(diverse_tasks) >= 10:
        break

print(f"{'#':<3} {'File':<30} {'Score':>5} {'Rec':<22} {'Status':<16}")
print("-" * 90)
for i, t in enumerate(diverse_tasks):
    print(f"{i+1:<3} {(t.get('fileName') or '?'):<30} {str(t.get('overallScore') or '?'):>5} {(t.get('recommendation') or '?'):<22} {(t.get('status') or '?'):<16}")

print("\n" + "=" * 140)
print("ROUTE COMPARISON")
print("=" * 140)

results = []
for t in diverse_tasks:
    tid = t.get("traceId", "")
    fname = (t.get("fileName") or "?")[:28]
    
    run_resp = requests.get(f"{BASE}/ops/runs", params={"traceId": tid})
    if not run_resp.ok:
        results.append({"file": fname, "agents": [], "reason": "api_error"})
        continue
    
    runs = run_resp.json().get("items", [])
    if not runs:
        results.append({"file": fname, "agents": [], "reason": "no_run"})
        continue
    
    run_id = runs[0].get("runId", "")
    detail_resp = requests.get(f"{BASE}/ops/runs/{run_id}")
    if not detail_resp.ok:
        results.append({"file": fname, "agents": [], "reason": "detail_error"})
        continue
    
    rd = detail_resp.json()
    metrics = rd.get("metrics", {})
    plan = rd.get("plan", {})
    skills_selected = rd.get("skillsSelected", [])
    memory = rd.get("memory", {})
    
    agents_used = metrics.get("agentsUsed", [])
    selected_because = plan.get("selectedBecause", {}) if plan else {}
    skipped_because = plan.get("skippedBecause", {}) if plan else {}
    parallel_groups = plan.get("parallelGroups", []) if plan else []
    
    # Count skill and memory from events
    events = rd.get("events", [])
    skill_events = [e for e in events if "skill" in e.get("eventType", "")]
    memory_events = [e for e in events if "memory" in e.get("eventType", "")]
    retry_events = [e for e in events if "retry" in (e.get("eventType", "") or "")]
    tool_events = [e for e in events if e.get("eventType") == "tool.completed"]
    
    # Get tool kinds
    tool_kinds = {}
    for e in tool_events:
        p = e.get("payload", {})
        kind = p.get("kind", "?")
        tool_kinds[kind] = tool_kinds.get(kind, 0) + 1
    
    results.append({
        "file": fname,
        "agents": agents_used,
        "llm": metrics.get("llmCalls", 0),
        "tools": metrics.get("toolCalls", 0),
        "retries": len(retry_events),
        "skills": len(skill_events),
        "memory_hits": memory.get("hits", 0) if isinstance(memory, dict) else 0,
        "compact": metrics.get("contextCompactions", 0),
        "duration": metrics.get("latencySeconds", 0),
        "parallel": parallel_groups,
        "selected": selected_because,
        "skipped": skipped_because,
        "tool_kinds": tool_kinds,
    })

# Print matrix
print(f"\n{'File':<28} {'Agents Used':<55} {'LLM':>4} {'Tool':>5} {'Ret':>4} {'Skl':>4} {'Mem':>4} {'Cmp':>4} {'Dur':>6}")
print("-" * 140)
for r in results:
    agents_str = ",".join([a.replace("Agent","") for a in r.get("agents", [])])[:54]
    print(f"{r['file']:<28} {agents_str:<55} {r.get('llm',0):>4} {r.get('tools',0):>5} {r.get('retries',0):>4} {r.get('skills',0):>4} {r.get('memory_hits',0):>4} {r.get('compact',0):>4} {r.get('duration',0):>6.1f}")

# Analyze routing differences
print("\n" + "=" * 80)
print("ROUTING ANALYSIS")
print("=" * 80)
agent_sets = {}
for r in results:
    key = tuple(sorted(r.get("agents", [])))
    if key not in agent_sets:
        agent_sets[key] = []
    agent_sets[key].append(r["file"])

print(f"\nUnique agent combinations: {len(agent_sets)}")
for agents, files in agent_sets.items():
    print(f"\n  Agents: {', '.join(a.replace('Agent','') for a in agents)}")
    print(f"  Used by: {', '.join(files)}")

# Check if routing varies by input
print("\n\nSELECTED/SKIPPED REASONS:")
for r in results:
    if r.get("skipped"):
        print(f"\n  {r['file']}:")
        print(f"    Skipped: {json.dumps(r['skipped'], ensure_ascii=False)[:200]}")
