"""Re-evaluate the original fault case with the fixed code."""
import requests, time, json

BASE = 'http://8.138.10.189'

# Get the original resume text
trace_id = 'trace-8df751cd-f46b-44cb-832c-8ed709cc1660'
r = requests.get(f'{BASE}/api/tasks/{trace_id}', timeout=10)
d = r.json()
resume_text = d.get('resumeText', '')
print(f"Original resume ({len(resume_text)} chars): {resume_text[:100]}...")

# Re-upload with same parameters
files = {'file': ('ai_agent_backend_reeval.txt', resume_text.encode('utf-8'), 'text/plain')}
data = {'jobDescription': '', 'jobCategory': 'TECH', 'executionMode': 'DAG_CONCURRENT'}

t0 = time.time()
r = requests.post(f'{BASE}/api/tasks/upload', files=files, data=data, timeout=30)
print(f'Upload: {r.status_code}')
task = r.json()
new_trace = task.get('traceId', '')
print(f'New traceId: {new_trace}')

for i in range(60):
    time.sleep(3)
    try:
        r2 = requests.get(f'{BASE}/api/tasks/{new_trace}', timeout=10)
    except:
        continue
    if r2.status_code != 200:
        continue
    d2 = r2.json()
    st = d2.get('evaluationState') or d2.get('status')
    if st in ('SUCCESS', 'PARTIAL_SUCCESS', 'COMPLETED', 'FAILED', 'SYSTEM_FAILED'):
        elapsed = time.time() - t0
        print(f'\nCompleted in {elapsed:.1f}s  status={st}')
        sr = d2.get('structuredReport') or {}
        if isinstance(sr, str):
            try: sr = json.loads(sr)
            except: sr = {}
        if isinstance(sr, dict):
            print(f"overallScore: {sr.get('overallScore')}")
            print(f"recommendation: {sr.get('recommendation')}")
            print(f"dataQuality: {sr.get('dataQuality')}")
            print(f"summary: {sr.get('summary', '')[:200]}")
            dims = sr.get('dimensions', [])
            print(f"\nDimensions ({len(dims)}):")
            for dd in dims:
                print(f"  {dd.get('name')}: score={dd.get('score')} [{dd.get('status')}]")
            print(f"\nstrengths: {len(sr.get('strengths', []))}")
            print(f"risks: {len(sr.get('risks', []))}")
            print(f"probes: {len(sr.get('interviewProbes', []))}")
            print(f"missingEvidence: {len(sr.get('missingEvidence', []))}")
            
            # Check it doesn't claim empty state
            summary = sr.get('summary', '')
            has_empty_claim = '共享状态为空' in summary or '没有简历' in summary
            print(f"\nClaims empty state: {has_empty_claim}")
        break
else:
    print(f'\nTIMEOUT after {time.time()-t0:.0f}s')
