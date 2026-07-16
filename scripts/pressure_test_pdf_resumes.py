"""Upload generated PDF resumes and validate parsing/evaluation coverage."""
from __future__ import annotations

import json
import mimetypes
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "testdata" / "resumes"


def ensure_dataset() -> list[dict]:
    meta = DATA_DIR / "metadata.json"
    if not meta.exists():
        subprocess.run([sys.executable, str(ROOT / "scripts" / "generate_resume_dataset.py")], check=True)
    return json.loads(meta.read_text(encoding="utf-8"))


def multipart_upload(url: str, fields: dict[str, str], file_field: str, file_path: Path) -> dict:
    boundary = "----ResumAIBoundary"
    chunks: list[bytes] = []
    for name, value in fields.items():
        chunks.append(f"--{boundary}\r\n".encode())
        chunks.append(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
        chunks.append(str(value).encode("utf-8"))
        chunks.append(b"\r\n")
    mime = mimetypes.guess_type(file_path.name)[0] or "application/pdf"
    chunks.append(f"--{boundary}\r\n".encode())
    chunks.append(
        f'Content-Disposition: form-data; name="{file_field}"; filename="{file_path.name}"\r\n'
        f"Content-Type: {mime}\r\n\r\n".encode()
    )
    chunks.append(file_path.read_bytes())
    chunks.append(b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode())
    body = b"".join(chunks)
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read().decode("utf-8"))


def http_json(url: str, timeout: int = 60) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def wait_task(base: str, trace_id: str, deadline_s: int = 420) -> dict:
    deadline = time.time() + deadline_s
    detail = {}
    while time.time() < deadline:
        detail = http_json(f"{base}/api/tasks/{trace_id}", timeout=30)
        status = detail.get("status")
        print(f"[pdf-test] {trace_id} status={status} summary={(detail.get('summary') or '')[:90]}")
        if status in ("SUCCESS", "FAILED"):
            return detail
        time.sleep(8)
    raise TimeoutError(f"task timeout: {trace_id}")


def main() -> None:
    base = sys.argv[1].rstrip("/") if len(sys.argv) > 1 else "http://127.0.0.1"
    sample_size = 12
    if "--all" in sys.argv:
        sample_size = 10_000
    for arg in sys.argv[2:]:
        if arg.startswith("--sample="):
            sample_size = int(arg.split("=", 1)[1])
    items = ensure_dataset()
    selected = items[:sample_size]
    print(f"[pdf-test] datasetSize={len(items)} selected={len(selected)} use --all to run every PDF")
    results = []
    for item in selected:
        pdf_path = ROOT / item["pdf"]
        start = time.perf_counter()
        created = multipart_upload(
            f"{base}/api/tasks/upload-auto",
            {"executionMode": "DAG_CONCURRENT"},
            "file",
            pdf_path,
        )
        trace_id = created["traceId"]
        detail = wait_task(base, trace_id)
        elapsed_ms = int((time.perf_counter() - start) * 1000)
        resume_text = detail.get("resumeText") or ""
        expected = item["expected"]
        covered = [kw for kw in expected if kw.lower() in resume_text.lower() or kw.lower() in json.dumps(detail, ensure_ascii=False).lower()]
        result = {
            "id": item["id"],
            "traceId": trace_id,
            "status": detail.get("status"),
            "score": detail.get("overallScore"),
            "elapsedMs": elapsed_ms,
            "expectedCount": len(expected),
            "coveredCount": len(covered),
            "coverageRate": round(len(covered) / max(len(expected), 1), 3),
            "resumeTextLength": len(resume_text),
            "recommendation": detail.get("recommendation"),
        }
        print(f"[pdf-test] RESULT {result}")
        results.append(result)
    print("\n=== PDF Resume Dataset Summary ===")
    avg_coverage = sum(r["coverageRate"] for r in results) / len(results)
    avg_latency = sum(r["elapsedMs"] for r in results) / len(results)
    print(json.dumps({"count": len(results), "avgCoverage": avg_coverage, "avgLatencyMs": avg_latency, "results": results}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
