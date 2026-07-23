"""Deep check of the new trace to understand PARTIAL_SUCCESS and question count."""
import requests
import json

BASE = "http://8.138.10.189"
tid = "trace-2b22d983-08a1-4292-82d9-292348763f2d"

# Get full task data
r = requests.get(f"{BASE}/api/tasks/{tid}", timeout=10)
d = r.json()

# Print key fields
print("=== TASK DATA ===")
print(f"status: {d.get('status')}")
print(f"overallScore: {d.get('overallScore')}")
print(f"recommendation: {d.get('recommendation')}")
print(f"dataQuality: {d.get('dataQuality')}")
print(f"durationMs: {d.get('durationMs')}")

sr = d.get("structuredReport") or {}
print(f"\n=== STRUCTURED REPORT KEYS ===")
print(f"Keys: {list(sr.keys())}")

# Check systemWarnings
warnings = sr.get("systemWarnings") or []
print(f"\n=== SYSTEM WARNINGS ({len(warnings)}) ===")
for w in warnings:
    print(f"  [{w.get('code')}] {w.get('message','')[:150]}")

# Check dimensions
dims = sr.get("dimensions") or []
print(f"\n=== DIMENSIONS ({len(dims)}) ===")
for dim in dims:
    print(f"  {dim.get('name')}: score={dim.get('score')} status={dim.get('status')} refs={len(dim.get('evidenceRefs') or [])}")
    if dim.get("rationale"):
        print(f"    rationale: {dim['rationale'][:100]}")

# Check interview probes  
probes = sr.get("interviewProbes") or sr.get("interviewQuestions") or []
print(f"\n=== INTERVIEW PROBES ({len(probes)}) ===")
for p in probes:
    if isinstance(p, dict):
        print(f"  Q: {p.get('question','')[:100]}")
        print(f"    objective: {p.get('objective','')[:80]}")
        print(f"    triggeredBy: {p.get('triggeredBy','')[:80]}")
        print(f"    goodSignals: {len(p.get('goodSignals') or [])}")
        print(f"    redFlags: {len(p.get('redFlags') or [])}")
        print(f"    evidenceRefs: {len(p.get('evidenceRefs') or [])}")
        print()

# Check risks
risks = sr.get("risks") or []
print(f"\n=== RISKS ({len(risks)}) ===")
for rsk in risks:
    if isinstance(rsk, dict):
        print(f"  [{rsk.get('severity')}] {rsk.get('claim','')[:100]}")

# Check strengths
strengths = sr.get("strengths") or []
print(f"\n=== STRENGTHS ({len(strengths)}) ===")
for s in strengths[:6]:
    print(f"  - {str(s)[:100]}")

# Check missingEvidence
missing = sr.get("missingEvidence") or []
print(f"\n=== MISSING EVIDENCE ({len(missing)}) ===")
for m in missing:
    print(f"  - {str(m)[:100]}")

# Check summary
print(f"\n=== SUMMARY ===")
print(sr.get("summary", "")[:300])

# Try to get run events
print(f"\n=== CHECKING RUN EVENTS ===")
# Try different endpoint patterns
endpoints = [
    f"{BASE}/api/runs/by-trace/{tid}",
    f"{BASE}/api/agent-runs?traceId={tid}",
    f"{BASE}/api/internal/runs?traceId={tid}",
]
for ep in endpoints:
    try:
        r2 = requests.get(ep, timeout=5)
        print(f"  {ep}: {r2.status_code}")
        if r2.status_code == 200:
            data = r2.json()
            if isinstance(data, list):
                print(f"    Found {len(data)} runs")
                for run in data[:2]:
                    print(f"    runId={run.get('runId')} status={run.get('status')}")
            elif isinstance(data, dict):
                print(f"    runId={data.get('runId')} status={data.get('status')}")
    except Exception as e:
        print(f"  {ep}: error {e}")

# Check the raw response for PARTIAL_SUCCESS clue
print(f"\n=== WHY PARTIAL_SUCCESS? ===")
# Possible reasons: systemWarnings, missing mustHaveCoverage, etc.
if not sr.get("mustHaveCoverage"):
    print("  - mustHaveCoverage is empty (may contribute to PARTIAL)")
if warnings:
    print(f"  - Has {len(warnings)} systemWarnings")
if d.get("status") == "PARTIAL_SUCCESS" and d.get("overallScore"):
    print("  - Has score but marked PARTIAL - check Java side logic")
