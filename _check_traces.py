"""Check the 3 focus traces plus recent new trace."""
import requests
import json

BASE = "http://8.138.10.189"

traces = [
    "trace-8df751cd-f46b-44cb-832c-8ed709cc1660",
    "trace-773a04c7-d67a-4b69-b5c5-cb0cc92f2644",
    "trace-ea7241a9-3041-4db3-bbab-85bed0aa9e98",
    "trace-2b22d983-08a1-4292-82d9-292348763f2d",  # new trace
]

all_results = []

for tid in traces:
    print(f"\n{'='*60}")
    print(f"TRACE: {tid}")
    print(f"{'='*60}")
    
    r = requests.get(f"{BASE}/api/tasks/{tid}", timeout=10)
    if r.status_code != 200:
        print(f"  HTTP {r.status_code}")
        continue
    
    d = r.json()
    status = d.get("status")
    score = d.get("overallScore")
    rec = d.get("recommendation")
    dur = d.get("durationMs", 0)
    
    print(f"  Status: {status}")
    print(f"  Score: {score}")
    print(f"  Recommendation: {rec}")
    print(f"  Duration: {dur/1000:.1f}s")
    
    sr = d.get("structuredReport") or {}
    dims = sr.get("dimensions") or []
    probes = sr.get("interviewProbes") or sr.get("interviewQuestions") or []
    risks = sr.get("risks") or []
    strengths = sr.get("strengths") or []
    summary = sr.get("summary", "")
    must_have = sr.get("mustHaveCoverage") or []
    missing = sr.get("missingEvidence") or []
    data_quality = sr.get("dataQuality")
    
    print(f"  DataQuality: {data_quality}")
    print(f"  Dimensions: {len(dims)}")
    for dim in dims:
        did = dim.get("id", dim.get("name", "?"))
        print(f"    - {did}: score={dim.get('score')} status={dim.get('status')}")
    print(f"  Strengths: {len(strengths)}")
    print(f"  Risks: {len(risks)}")
    for rsk in risks[:3]:
        if isinstance(rsk, dict):
            print(f"    - [{rsk.get('severity','?')}] {str(rsk.get('risk',''))[:80]}")
        else:
            print(f"    - {str(rsk)[:80]}")
    print(f"  InterviewProbes: {len(probes)}")
    print(f"  MustHaveCoverage: {len(must_have)}")
    print(f"  MissingEvidence: {len(missing)}")
    print(f"  Summary: {summary[:150]}")
    
    # Check for known bad patterns
    bad_patterns = ["共享状态为空", "没有简历", "SharedState", "无法生成"]
    for bp in bad_patterns:
        if bp in summary:
            print(f"  *** BAD PATTERN: '{bp}' found in summary ***")
    
    # Get runs
    r2 = requests.get(f"{BASE}/api/runs/by-trace/{tid}", timeout=10)
    if r2.status_code == 200:
        runs_data = r2.json()
        runs = runs_data if isinstance(runs_data, list) else [runs_data]
        for run in runs[:1]:
            run_id = run.get("runId", "")
            print(f"\n  Run: {run_id}")
            print(f"  RunType: {run.get('runType')}")
            print(f"  RunStatus: {run.get('status')}")
            
            r3 = requests.get(f"{BASE}/api/runs/{run_id}/events", timeout=10)
            if r3.status_code == 200:
                events = r3.json() if isinstance(r3.json(), list) else []
                cats = {}
                agent_events = []
                llm_events = []
                tool_events = []
                skill_events = []
                memory_events = []
                retry_events = []
                
                for ev in events:
                    cat = ev.get("category", "UNKNOWN")
                    cats[cat] = cats.get(cat, 0) + 1
                    evt = ev.get("type", "")
                    
                    if evt.startswith("agent."):
                        agent_events.append(ev)
                    elif "llm" in evt:
                        llm_events.append(ev)
                    elif "tool" in evt:
                        tool_events.append(ev)
                    elif "skill" in evt:
                        skill_events.append(ev)
                    elif "memory" in evt:
                        memory_events.append(ev)
                    elif "retry" in evt:
                        retry_events.append(ev)
                
                print(f"  Total Events: {len(events)}")
                print(f"  Categories: {json.dumps(cats, indent=4)}")
                print(f"  Agent events: {len(agent_events)}")
                for ae in agent_events[:10]:
                    aid = ae.get("agentId", "?")
                    atype = ae.get("type", "?")
                    adur = ae.get("durationMs", 0)
                    payload = ae.get("payload") or {}
                    print(f"    {atype}: {aid} dur={adur}ms payload_keys={list(payload.keys())[:5]}")
                
                print(f"  LLM events: {len(llm_events)}")
                for le in llm_events[:5]:
                    payload = le.get("payload") or {}
                    print(f"    {le.get('type')}: model={payload.get('model','?')} tokens={payload.get('promptTokens','?')}/{payload.get('completionTokens','?')}")
                
                print(f"  Tool events: {len(tool_events)}")
                for te in tool_events[:5]:
                    payload = te.get("payload") or {}
                    print(f"    {te.get('type')}: {payload.get('toolName','?')} kind={payload.get('kind','?')}")
                
                print(f"  Skill events: {len(skill_events)}")
                print(f"  Memory events: {len(memory_events)}")
                print(f"  Retry events: {len(retry_events)}")
            else:
                print(f"  Events: HTTP {r3.status_code}")
    else:
        print(f"  Runs: HTTP {r2.status_code}")
    
    all_results.append({
        "traceId": tid,
        "status": status,
        "score": score,
        "rec": rec,
        "duration": dur,
        "dims": len(dims),
        "probes": len(probes),
        "risks": len(risks),
        "strengths": len(strengths),
    })

print(f"\n\n{'='*60}")
print("COMPARISON MATRIX")
print(f"{'='*60}")
print(f"{'traceId':<50} {'status':<18} {'score':<6} {'dims':<5} {'probes':<7} {'dur(s)'}")
for r in all_results:
    print(f"{r['traceId']:<50} {r['status']:<18} {str(r['score']):<6} {r['dims']:<5} {r['probes']:<7} {r['duration']/1000:.1f}")
