import requests, json, sys

task_id = sys.argv[1] if len(sys.argv) > 1 else "1234"
r = requests.get(f'http://8.138.10.189/api/tasks/{task_id}/detail', timeout=10)
if r.status_code != 200:
    r = requests.get(f'http://8.138.10.189/api/tasks?sortBy=create_time&sortOrder=DESC&page=1&size=1', timeout=10)
    if r.status_code == 200:
        records = r.json().get('records', [])
        if records:
            task_id = records[0].get('id')
            print(f'Using task_id from list: {task_id}')
    r = requests.get(f'http://8.138.10.189/api/tasks/{task_id}', timeout=10)

print(f'status_code={r.status_code}')
if r.status_code == 200:
    d = r.json()
    print(f'evaluationState={d.get("evaluationState")}')
    print(f'queueStatus={d.get("queueStatus")}')
    print(f'overallScore (top level)={d.get("overallScore")}')
    print(f'recommendation (top level)={d.get("recommendation")}')
    
    sr = d.get('structuredReport')
    if sr:
        if isinstance(sr, str):
            try:
                sr = json.loads(sr)
            except:
                print(f'structuredReport parse error, first 200 chars: {sr[:200]}')
                sys.exit()
        if isinstance(sr, dict):
            print(f'\n=== Structured Report ===')
            print(f'overallScore={sr.get("overallScore")}')
            print(f'recommendation={sr.get("recommendation")}')
            print(f'dataQuality={sr.get("dataQuality")}')
            print(f'summary={sr.get("summary", "")[:300]}')
            dims = sr.get('dimensions', [])
            print(f'\nDimensions ({len(dims)}):')
            for dd in dims:
                print(f'  {dd.get("name")}: score={dd.get("score")} status={dd.get("status")}')
                print(f'    rationale: {str(dd.get("rationale", ""))[:120]}')
            strengths = sr.get('strengths', [])
            print(f'\nStrengths ({len(strengths)}):')
            for s in strengths[:5]:
                print(f'  - {str(s)[:100]}')
            risks = sr.get('risks', [])
            print(f'\nRisks ({len(risks)}):')
            for r2 in risks[:5]:
                if isinstance(r2, dict):
                    print(f'  - {r2.get("risk") or r2.get("claim", "")[:80]} [{r2.get("severity")}]')
            probes = sr.get('interviewProbes', [])
            print(f'\nInterview Probes ({len(probes)}):')
            for p in probes[:6]:
                if isinstance(p, dict):
                    print(f'  - Q: {str(p.get("question", ""))[:100]}')
                    print(f'    Why: {str(p.get("whyAsk") or p.get("objective", ""))[:80]}')
            missing = sr.get('missingEvidence', [])
            print(f'\nMissing Evidence ({len(missing)}):')
            for m in missing[:5]:
                print(f'  - {str(m)[:100]}')
    else:
        print('structuredReport=None')
