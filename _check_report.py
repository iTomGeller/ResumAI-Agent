import requests, json, sys

trace_id = sys.argv[1] if len(sys.argv) > 1 else 'trace-805f9834-79c5-480f-bff9-e1b219984935'
r = requests.get(f'http://8.138.10.189/api/tasks/{trace_id}', timeout=10)
d = r.json()
sr = d.get('structuredReport')
if isinstance(sr, str):
    sr = json.loads(sr)

print(f"overallScore: {sr.get('overallScore')}")
print(f"recommendation: {sr.get('recommendation')}")
print(f"dataQuality: {sr.get('dataQuality')}")
print()

dims = sr.get('dimensions', [])
print(f"Dimensions ({len(dims)}):")
for dd in dims:
    name = dd.get('name', '')
    score = dd.get('score')
    status = dd.get('status', '')
    rat = dd.get('rationale', '')[:200]
    print(f'  {name}: score={score} [{status}]')
    print(f'    {rat}')
print()

summary = sr.get('summary', '')
print(f"Summary: {summary[:300]}")
print()

strengths = sr.get('strengths', [])
print(f"Strengths ({len(strengths)}):")
for s in strengths[:5]:
    print(f"  - {str(s)[:120]}")
print()

risks = sr.get('risks', [])
print(f"Risks ({len(risks)}):")
for r2 in risks[:5]:
    if isinstance(r2, dict):
        claim = r2.get('claim') or r2.get('risk', '')
        sev = r2.get('severity', '')
        print(f"  - [{sev}] {str(claim)[:120]}")
print()

probes = sr.get('interviewProbes', [])
print(f"Interview Probes ({len(probes)}):")
for p in probes[:5]:
    if isinstance(p, dict):
        q = p.get('question', '')
        print(f"  - {str(q)[:120]}")
