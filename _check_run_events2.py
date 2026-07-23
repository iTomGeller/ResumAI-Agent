"""Get run events - fixed parsing."""
import requests
import json

BASE = "http://8.138.10.189"
tid = "trace-2b22d983-08a1-4292-82d9-292348763f2d"

# Get ops runs data
r = requests.get(f"{BASE}/api/ops/runs", params={"traceId": tid}, timeout=10)
print(f"Status: {r.status_code}")
data = r.json()
print(f"Top keys: {list(data.keys())}")
print(f"Data snippet: {json.dumps(data, ensure_ascii=False)[:2000]}")
