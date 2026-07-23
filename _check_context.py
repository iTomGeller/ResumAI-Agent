"""Verify LLM Context completeness via run artifacts and events"""
import requests, json

BASE = "http://8.138.10.189/api"
tid = "trace-29e41391-ecdd-47ba-b81a-0270ae480c47"

# Get run detail
run_resp = requests.get(f"{BASE}/ops/runs", params={"traceId": tid})
runs = run_resp.json().get("items", [])
run_id = runs[0]["runId"]
detail = requests.get(f"{BASE}/ops/runs/{run_id}").json()

# Check artifacts
artifacts = detail.get("artifacts", {})
print("=== ARTIFACTS (data chain verification) ===")
for key, val in artifacts.items():
    if isinstance(val, str):
        print(f"  {key}: {len(val)} chars")
    elif isinstance(val, list):
        print(f"  {key}: list[{len(val)}]")
    elif isinstance(val, dict):
        print(f"  {key}: dict with keys {list(val.keys())[:5]}")
    else:
        print(f"  {key}: {type(val).__name__}")

# Check plan goal artifacts vs actual
plan = detail.get("plan", {})
goal_artifacts = plan.get("goalArtifacts", [])
print(f"\n=== GOAL ARTIFACTS ===")
print(f"  Required: {goal_artifacts}")
print(f"  Present: {list(artifacts.keys())}")
missing = [g for g in goal_artifacts if g not in artifacts]
print(f"  Missing: {missing}")

# Check metrics for missing goal artifacts
metrics = detail.get("metrics", {})
print(f"\n  missingGoalArtifacts (from metrics): {metrics.get('missingGoalArtifacts', [])}")

# Check skills
print(f"\n=== SKILLS SELECTED ===")
skills = detail.get("skillsSelected", [])
for s in skills[:10]:
    print(f"  {s.get('skillId', '?')} -> agent={s.get('agentId', '?')}")
if len(skills) > 10:
    print(f"  ... and {len(skills)-10} more")

# Check memory
memory = detail.get("memory", {})
print(f"\n=== MEMORY ===")
print(f"  {json.dumps(memory, ensure_ascii=False, default=str)[:300]}")

# Check budget
budget = detail.get("budget", {})
print(f"\n=== BUDGET ===")
print(f"  {json.dumps(budget, ensure_ascii=False, default=str)[:300]}")

# Check LLM events for context size
events = detail.get("events", [])
llm_events = [e for e in events if e.get("eventType") == "llm.completed" or e.get("eventType") == "llm.started"]
print(f"\n=== LLM CALL CONTEXT SIZES ===")
for e in events:
    if e.get("eventType") == "llm.completed":
        p = e.get("payload", {})
        agent = e.get("agentId", "?")
        pt = p.get("promptTokens", 0)
        ct = p.get("completionTokens", 0)
        model = p.get("model", "?")
        dur = p.get("durationMs", 0)
        print(f"  {agent:20s} | model={model} | pt={pt} ct={ct} | dur={dur}ms")

# Check observability
obs = detail.get("observability", {})
print(f"\n=== OBSERVABILITY ===")
if obs:
    print(f"  keys: {list(obs.keys())}")
    for k, v in obs.items():
        if isinstance(v, (str, int, float, bool)):
            print(f"    {k}: {v}")
        elif isinstance(v, dict):
            print(f"    {k}: {json.dumps(v, ensure_ascii=False, default=str)[:150]}")
