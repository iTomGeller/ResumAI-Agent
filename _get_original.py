import requests, json

trace_id = 'trace-8df751cd-f46b-44cb-832c-8ed709cc1660'
r = requests.get(f'http://8.138.10.189/api/tasks/{trace_id}', timeout=10)
d = r.json()
print(f"fileName: {d.get('fileName')}")
print(f"jobCategory: {d.get('jobCategory')}")
rt = d.get('resumeText', '') or ''
print(f"resumeText chars: {len(rt)}")
print(f"resumeText first 300: {rt[:300]}")
jd = d.get('jobDescription', '') or ''
print(f"jobDescription chars: {len(jd)}")
print(f"jobDescription first 300: {jd[:300]}")
