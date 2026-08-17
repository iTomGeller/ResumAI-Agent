"""End-to-end stress harness for the ResumAI Agent system deployed on Aliyun ECS.

Uploads every resume in testdata/stress_resumes/manifest.json to a caller-selected backend,
polls each task to completion, then pulls the
agent-execution trace and parses all metrics required by the stress report.

Design goals:
  * Reproducible + resumable: completed (SUCCESS) records are checkpointed to disk;
    a restart will not re-run successful tasks.
  * Bounded concurrency: at most --concurrency tasks in-flight at any moment.
  * Faithful: stores the FULL raw task detail + agent-execution JSON for every
    record so analysis never has to guess. No fabricated numbers.

Usage:
    python harness/run_stress.py --base-url http://127.0.0.1:8080
    python harness/run_stress.py --limit 2       # probe first 2 (validation)
    python harness/run_stress.py --ids id1 id2   # run specific manifest ids
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import httpx

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "testdata" / "stress_resumes" / "manifest.json"
DEFAULT_OUTDIR = ROOT / "reports" / "stress_e2e"
OUTDIR = DEFAULT_OUTDIR
CHECKPOINT = OUTDIR / "checkpoint.json"
RAW_RESULTS = OUTDIR / "raw_results.json"

BASE = ""
AUTH_TOKEN = ""
HR_ID = ""
POLL_INTERVAL_S = 5
TASK_TIMEOUT_S = 180
UPLOAD_RETRIES = 3
TERMINAL = {"SUCCESS", "PARTIAL_SUCCESS", "FAILED", "CANCELLED", "SUPERSEDED"}

_print_lock = threading.Lock()
_ckpt_lock = threading.Lock()


def log(msg: str) -> None:
    with _print_lock:
        ts = time.strftime("%H:%M:%S")
        print(f"[{ts}] {msg}", flush=True)


def http_json(url: str, timeout: int = 60) -> dict:
    headers = {"Accept": "application/json"}
    if AUTH_TOKEN:
        headers["Authorization"] = f"Bearer {AUTH_TOKEN}"
    if HR_ID:
        headers["X-HR-Id"] = HR_ID
    response = httpx.get(url, headers=headers, timeout=timeout)
    response.raise_for_status()
    return response.json()


def mime_for(file_type: str) -> str:
    return "application/pdf" if file_type.lower() == "pdf" else "text/plain"


def upload_resume(abs_path: Path, file_type: str) -> dict:
    """Upload one resume via platform curl (subprocess, no shell)."""
    curl = shutil.which("curl.exe") or shutil.which("curl")
    if not curl:
        raise RuntimeError("curl executable not found")
    form_file = f"file=@{abs_path};type={mime_for(file_type)}"
    cmd = [
        curl, "-s", "-S", "--max-time", "60",
        "-F", form_file,
        "-F", "executionMode=DAG_CONCURRENT",
        f"{BASE}/api/tasks/upload-auto",
    ]
    if AUTH_TOKEN:
        cmd[1:1] = ["-H", f"Authorization: Bearer {AUTH_TOKEN}"]
    if HR_ID:
        cmd[1:1] = ["-H", f"X-HR-Id: {HR_ID}"]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        raise RuntimeError(f"curl rc={proc.returncode} stderr={proc.stderr.strip()[:200]}")
    out = (proc.stdout or "").strip()
    if not out:
        raise RuntimeError("empty upload response")
    try:
        return json.loads(out)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"non-json upload response: {out[:200]}") from exc


def extract_harness_plan(tree: dict) -> dict:
    """Harness plan is embedded in the knowledge_context node round output; scan for it."""
    direct = tree.get("harnessPlan")
    if isinstance(direct, dict) and direct.get("version"):
        return direct
    for agent in tree.get("executionTree", []) or []:
        for rnd in agent.get("rounds", []) or []:
            for field in ("output", "finalOutput", "input", "decisionText"):
                text = str(rnd.get(field) or "")
                if "harnessPlan" not in text:
                    continue
                # try whole-string json first
                try:
                    parsed = json.loads(text)
                    if isinstance(parsed, dict) and isinstance(parsed.get("harnessPlan"), dict):
                        return parsed["harnessPlan"]
                except Exception:
                    pass
                # brace-scan for an embedded {"harnessPlan": {...}} or {"version": "agent-harness..."}
                marker = text.find("harnessPlan")
                start = text.rfind("{", 0, marker)
                while start >= 0:
                    depth = 0
                    for idx in range(start, len(text)):
                        ch = text[idx]
                        if ch == "{":
                            depth += 1
                        elif ch == "}":
                            depth -= 1
                            if depth == 0:
                                try:
                                    parsed = json.loads(text[start:idx + 1])
                                    if isinstance(parsed, dict):
                                        if isinstance(parsed.get("harnessPlan"), dict):
                                            return parsed["harnessPlan"]
                                        if parsed.get("version") and isinstance(parsed.get("route"), dict):
                                            return parsed
                                except Exception:
                                    pass
                                break
                    start = text.rfind("{", 0, start)
    return {}


def parse_metrics(detail: dict, tree: dict) -> dict:
    """Parse the report-relevant metrics from task detail + agent-execution tree."""
    # ---- node durations keyed by nodeId ----
    node_durations: dict[str, int] = {}
    node_names: dict[str, str] = {}
    tool_calls: list[dict] = []
    observed_llm_calls = 0
    observed_input_tokens = 0
    observed_output_tokens = 0
    for agent in tree.get("executionTree", []) or []:
        node_id = agent.get("nodeId") or agent.get("name") or "unknown"
        try:
            dur = int(agent.get("durationMs") or 0)
        except (TypeError, ValueError):
            dur = 0
        # keep the max if a node appears twice
        node_durations[node_id] = max(node_durations.get(node_id, 0), dur)
        node_names[node_id] = agent.get("name") or node_id
        for rnd in agent.get("rounds", []) or []:
            model_name = str(rnd.get("modelName") or "")
            if rnd.get("callKind") == "llm" or (model_name and not model_name.startswith("deterministic")):
                observed_llm_calls += 1
                usage = rnd.get("tokenUsage") if isinstance(rnd.get("tokenUsage"), dict) else {}
                observed_input_tokens += int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
                observed_output_tokens += int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
            for tc in rnd.get("toolCalls", []) or []:
                name = tc.get("name")
                if not name:
                    continue
                try:
                    tdur = int(tc.get("durationMs") or 0)
                except (TypeError, ValueError):
                    tdur = 0
                tool_calls.append({
                    "name": name,
                    "durationMs": tdur,
                    "status": tc.get("status"),
                    "nodeId": node_id,
                    "origin": tc.get("origin") or tc.get("toolOrigin"),
                    "family": tc.get("family") or tc.get("toolFamily") or tc.get("category"),
                    "server": tc.get("server"),
                })

    # Prefer the runtime's tool provenance over brittle tool-name matching. The
    # name fallback keeps older traces analyzable, but new public providers can
    # be added without teaching this harness each provider-specific name.
    mcp_calls = [
        t for t in tool_calls
        if t.get("origin") == "mcp" or "[public:" in str(t.get("name") or "")
    ]
    gh_calls = [t for t in tool_calls if t["name"] == "github_enrichment"]

    # ---- harness plan / dynamic routing ----
    harness = extract_harness_plan(tree)
    route = harness.get("route") if isinstance(harness.get("route"), dict) else {}
    selected = route.get("selectedAgents") or route.get("enabledAgents") or []

    # ---- task-level fields ----
    def _len(x) -> int:
        return len(x) if isinstance(x, str) else 0

    def _count(x) -> int:
        return len(x) if isinstance(x, list) else 0

    return {
        "nodeDurations": node_durations,
        "nodeNames": node_names,
        "toolCalls": tool_calls,
        "toolCallCount": len(tool_calls),
        "mcpFetchCount": len(mcp_calls),
        "mcpFetchDurations": [t["durationMs"] for t in mcp_calls],
        "mcpFetchStatuses": [t["status"] for t in mcp_calls],
        "githubEnrichmentCount": len(gh_calls),
        "githubEnrichmentDurations": [t["durationMs"] for t in gh_calls],
        "githubEnrichmentStatuses": [t["status"] for t in gh_calls],
        "framework": tree.get("framework"),
        "routeMode": route.get("routeMode"),
        "selectedAgents": selected,
        "complexity": route.get("complexity"),
        "candidateType": route.get("candidateType"),
        "experienceLevel": route.get("experienceLevel"),
        "estimatedLlmCalls": route.get("estimatedLlmCalls"),
        "fullPipelineLlmCalls": route.get("fullPipelineLlmCalls"),
        "llmCallsSavedVsFull": route.get("llmCallsSavedVsFull"),
        "observedLlmCalls": observed_llm_calls,
        "observedInputTokens": observed_input_tokens,
        "observedOutputTokens": observed_output_tokens,
        "memoryHitCount": route.get("memoryHitCount"),
        "knowledgeHitCount": route.get("knowledgeHitCount"),
        # task-level
        "serverDurationMs": detail.get("durationMs"),
        "overallScore": detail.get("overallScore"),
        "recommendation": detail.get("recommendation"),
        "tokenCost": detail.get("tokenCost"),
        "reportLength": _len(detail.get("summary")),
        "aiRecommendationLength": _len(detail.get("aiRecommendation")),
        "decisionRationaleLength": _len(detail.get("decisionRationale")),
        "riskSummaryLength": _len(detail.get("riskSummary")),
        "strengthsCount": _count(detail.get("strengths")),
        "risksCount": _count(detail.get("risks")),
        "interviewQuestionsCount": _count(detail.get("interviewQuestions")),
        "matchedJdTitle": detail.get("matchedJdTitle"),
        "jdMatchScore": detail.get("jdMatchScore"),
    }


def run_one(rec: dict) -> dict:
    rid = rec["id"]
    abs_path = (ROOT / rec["path"]).resolve()
    result: dict = {
        "id": rid,
        "name": rec.get("name"),
        "role": rec.get("role"),
        "fileType": rec.get("fileType"),
        "hasGithub": rec.get("hasGithub"),
        "textLength": rec.get("textLength"),
        "expectedSkills": rec.get("expectedSkills"),
        "traceId": None,
        "baseUrl": BASE,
        "status": "FAILED",
        "failReason": None,
        "uploadMs": None,
        "clientWallMs": None,
    }
    if not abs_path.is_file():
        result["failReason"] = f"file_not_found: {abs_path}"
        log(f"FAIL  {rid}: file not found")
        return result

    # ---- upload (with retries) ----
    t0 = time.time()
    trace_id = None
    last_err = None
    for attempt in range(1, UPLOAD_RETRIES + 1):
        try:
            up = upload_resume(abs_path, rec.get("fileType", "txt"))
            trace_id = up.get("traceId")
            if trace_id:
                break
            last_err = f"no traceId in response: {json.dumps(up, ensure_ascii=False)[:160]}"
        except Exception as exc:  # noqa: BLE001
            last_err = str(exc)
        time.sleep(2 * attempt)
    result["uploadMs"] = int((time.time() - t0) * 1000)
    if not trace_id:
        result["failReason"] = f"upload_failed: {last_err}"
        log(f"FAIL  {rid}: upload failed -> {last_err}")
        return result
    result["traceId"] = trace_id
    log(f"START {rid}: trace={trace_id}")

    # ---- poll to terminal ----
    deadline = time.time() + TASK_TIMEOUT_S
    status = "RUNNING"
    detail: dict = {}
    while time.time() < deadline:
        try:
            detail = http_json(f"{BASE}/api/tasks/{trace_id}", timeout=30)
            status = detail.get("status", "RUNNING")
        except Exception as exc:  # noqa: BLE001
            status = "RUNNING"
            detail = detail or {}
            last_err = str(exc)
        if status in TERMINAL:
            break
        time.sleep(POLL_INTERVAL_S)

    result["clientWallMs"] = int((time.time() - t0) * 1000)
    result["status"] = status
    result["rawTask"] = detail

    if status != "SUCCESS":
        if status not in TERMINAL:
            result["status"] = "FAILED"
            result["failReason"] = f"client_timeout_{TASK_TIMEOUT_S}s (last_status={status})"
        else:
            result["failReason"] = f"task_status={status}"
        log(f"FAIL  {rid}: status={result['status']} ({result.get('failReason')})")
        return result

    # ---- agent execution tree ----
    tree: dict = {}
    for attempt in range(1, 4):
        try:
            tree = http_json(f"{BASE}/api/tasks/{trace_id}/agent-execution", timeout=45)
            break
        except Exception as exc:  # noqa: BLE001
            last_err = str(exc)
            time.sleep(2 * attempt)
    result["rawExecution"] = tree

    try:
        result["metrics"] = parse_metrics(detail, tree)
    except Exception as exc:  # noqa: BLE001
        result["metrics"] = {}
        result["failReason"] = f"parse_error: {exc}"

    m = result.get("metrics", {})
    log(
        f"OK    {rid}: dur={m.get('serverDurationMs')}ms route={m.get('routeMode')} "
        f"score={m.get('overallScore')} rec={m.get('recommendation')} "
        f"mcp={m.get('mcpFetchCount')} gh={m.get('githubEnrichmentCount')} "
        f"saved={m.get('llmCallsSavedVsFull')} iq={m.get('interviewQuestionsCount')}"
    )
    return result


def load_checkpoint() -> dict:
    if CHECKPOINT.is_file():
        try:
            return json.loads(CHECKPOINT.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return {}
    return {}


def _atomic_write_json(path: Path, obj, retries: int = 10) -> None:
    """Atomic-ish write that survives transient Windows file locks (AV/indexer/IDE).

    os.replace can raise PermissionError [WinError 5] when another process briefly
    holds the target open. Retry the replace; as a last resort write in place.
    """
    text = json.dumps(obj, ensure_ascii=False, indent=2)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    for i in range(retries):
        try:
            tmp.replace(path)
            return
        except PermissionError:
            time.sleep(0.4 * (i + 1))
    # last resort: non-atomic direct write
    try:
        path.write_text(text, encoding="utf-8")
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def save_checkpoint(state: dict) -> None:
    _atomic_write_json(CHECKPOINT, state)


def main() -> None:
    global BASE, AUTH_TOKEN, HR_ID, OUTDIR, CHECKPOINT, RAW_RESULTS, TASK_TIMEOUT_S
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--base-url",
        default=os.environ.get("RESUMAI_BASE_URL", ""),
        help="Backend origin; or set RESUMAI_BASE_URL",
    )
    ap.add_argument(
        "--allow-insecure-http",
        action="store_true",
        help="Allow plain HTTP for a non-loopback target",
    )
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument(
        "--outdir", type=Path, default=DEFAULT_OUTDIR,
        help="isolated checkpoint/result directory; use a fresh directory for each build",
    )
    ap.add_argument("--task-timeout", type=int, default=TASK_TIMEOUT_S)
    ap.add_argument("--limit", type=int, default=0, help="run only first N manifest entries")
    ap.add_argument("--ids", nargs="*", default=None, help="run only these manifest ids")
    ap.add_argument("--retry-failed", action="store_true", help="re-run non-SUCCESS checkpoint entries")
    args = ap.parse_args()

    BASE = str(args.base_url or "").rstrip("/")
    if not BASE:
        ap.error("--base-url or RESUMAI_BASE_URL is required")
    parsed_base = urlparse(BASE)
    if parsed_base.scheme not in {"http", "https"} or not parsed_base.hostname:
        ap.error("base URL must be an absolute http(s) origin")
    is_loopback = parsed_base.hostname in {"localhost", "127.0.0.1", "::1"}
    if parsed_base.scheme == "http" and not is_loopback and not args.allow_insecure_http:
        ap.error("refusing non-loopback plain HTTP; use HTTPS or explicitly pass --allow-insecure-http")
    AUTH_TOKEN = os.environ.get("RESUMAI_API_TOKEN", "").strip()
    HR_ID = os.environ.get("RESUMAI_HR_ID", "").strip()
    OUTDIR = args.outdir.resolve()
    CHECKPOINT = OUTDIR / "checkpoint.json"
    RAW_RESULTS = OUTDIR / "raw_results.json"
    TASK_TIMEOUT_S = max(60, args.task_timeout)

    OUTDIR.mkdir(parents=True, exist_ok=True)
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    if args.ids:
        manifest = [r for r in manifest if r["id"] in set(args.ids)]
    elif args.limit:
        manifest = manifest[: args.limit]

    state = load_checkpoint()  # id -> record
    todo = []
    for rec in manifest:
        prev = state.get(rec["id"])
        if prev and prev.get("status") == "SUCCESS" and prev.get("baseUrl") == BASE:
            continue
        if prev and prev.get("baseUrl") == BASE and not args.retry_failed and prev.get("status") == "FAILED" \
                and not str(prev.get("failReason", "")).startswith("client_timeout"):
            # keep definitive backend failures unless explicitly retrying
            continue
        todo.append(rec)

    done_ok = sum(
        1 for v in state.values()
        if v.get("status") == "SUCCESS" and v.get("baseUrl") == BASE
    )
    log(f"manifest={len(manifest)} already_ok={done_ok} todo={len(todo)} concurrency={args.concurrency}")

    completed = 0
    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futures = {pool.submit(run_one, rec): rec for rec in todo}
        for fut in as_completed(futures):
            rec = futures[fut]
            try:
                res = fut.result()
            except Exception as exc:  # noqa: BLE001
                res = {"id": rec["id"], "status": "FAILED", "failReason": f"worker_crash: {exc}"}
            with _ckpt_lock:
                state[res["id"]] = res
                save_checkpoint(state)
            completed += 1
            ok = sum(
                1 for v in state.values()
                if v.get("status") == "SUCCESS" and v.get("baseUrl") == BASE
            )
            fail = sum(
                1 for v in state.values()
                if v.get("status") == "FAILED" and v.get("baseUrl") == BASE
            )
            log(f"PROGRESS {completed}/{len(todo)} (cumulative ok={ok} fail={fail})")

    # final raw dump (ordered by manifest)
    ordered = [state[r["id"]] for r in manifest if r["id"] in state]
    _atomic_write_json(RAW_RESULTS, ordered)
    ok = sum(1 for v in ordered if v.get("status") == "SUCCESS")
    fail = len(ordered) - ok
    log(f"DONE wrote {RAW_RESULTS} records={len(ordered)} ok={ok} fail={fail}")


if __name__ == "__main__":
    main()
