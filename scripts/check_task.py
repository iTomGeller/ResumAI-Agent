import urllib.request, json

url = "http://8.166.136.122/api/tasks/trace-058067d0-1cb4-472a-83eb-b4405cc11f99"
try:
    with urllib.request.urlopen(url, timeout=20) as resp:
        raw = resp.read()
        # Try to parse as JSON - the response might have encoding issues
        try:
            data = json.loads(raw)
            print(f"Status: {data.get('status')}")
            print(f"Score: {data.get('overallScore')}")
            print(f"Duration: {data.get('durationMs')}ms")
            print(f"Recommendation: {data.get('recommendation')}")
            summary = data.get('summary', '')
            print(f"Summary (first 500 chars): {summary[:500]}")
        except json.JSONDecodeError as e:
            # Show raw around the error position
            pos = e.pos if hasattr(e, 'pos') else 0
            print(f"JSON parse error at pos {pos}: {e.msg}")
            text = raw.decode('utf-8', errors='replace')
            # Extract status manually
            import re
            status_m = re.search(r'"status":"([^"]+)"', text)
            score_m = re.search(r'"overallScore":(\d+)', text)
            dur_m = re.search(r'"durationMs":(\d+)', text)
            rec_m = re.search(r'"recommendation":"([^"]*)"', text)
            print(f"Status: {status_m.group(1) if status_m else 'N/A'}")
            print(f"Score: {score_m.group(1) if score_m else 'N/A'}")
            print(f"Duration: {dur_m.group(1) if dur_m else 'N/A'}ms")
            print(f"Rec: {rec_m.group(1) if rec_m else 'N/A'}")
except Exception as e:
    print(f"Request failed: {e}")
