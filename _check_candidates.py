"""Check multiple candidates for route comparison."""
import requests
import json

BASE = "http://8.138.10.189"

# Get candidate list
r = requests.get(f"{BASE}/api/tasks", params={"page": 1, "size": 20}, timeout=10)
if r.status_code != 200:
    # Try other endpoints
    r = requests.get(f"{BASE}/api/candidates", params={"page": 1, "size": 20}, timeout=10)
    if r.status_code != 200:
        r = requests.get(f"{BASE}/api/tasks/list", timeout=10)

print(f"List status: {r.status_code}")
if r.status_code == 200:
    data = r.json()
    if isinstance(data, dict):
        items = data.get("items") or data.get("content") or data.get("records") or data.get("tasks") or []
        print(f"Found items: {len(items)}")
        print(f"Keys: {list(data.keys())}")
    elif isinstance(data, list):
        items = data
        print(f"Found items (list): {len(items)}")
    else:
        items = []
    
    # Get first few to check structure
    for item in items[:3]:
        if isinstance(item, dict):
            print(f"\n  Item keys: {list(item.keys())[:10]}")
            print(f"  traceId: {item.get('traceId', item.get('trace_id', '?'))}")
            print(f"  status: {item.get('status', '?')}")
            print(f"  category: {item.get('jobCategory', item.get('category', '?'))}")
            break
else:
    print(f"Body: {r.text[:500]}")

# Try alternative endpoint
print(f"\n=== Trying /api/candidates ===")
r2 = requests.get(f"{BASE}/api/candidates", params={"page": 1, "size": 10}, timeout=10)
print(f"Status: {r2.status_code}")
if r2.status_code == 200:
    d2 = r2.json()
    print(f"Keys: {list(d2.keys()) if isinstance(d2, dict) else 'list'}")
    if isinstance(d2, dict):
        items2 = d2.get("items") or d2.get("records") or d2.get("content") or []
        print(f"Items: {len(items2)}")
        for item in items2[:2]:
            print(f"  {json.dumps(item, ensure_ascii=False)[:200]}")
