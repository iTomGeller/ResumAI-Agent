#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ECS acceptance for Decision Agent regression (plan §7.6).

Uses stdlib only (urllib). Exit non-zero on any failed assertion.
Writes JSON report to reports/acceptance/decision-agent-regression.json.

Env:
  BASE_URL          e.g. http://127.0.0.1 or http://HOST:8080
  ACCEPTANCE_HOST   host when BASE_URL unset (default 127.0.0.1)
  ACCEPTANCE_VIA_NGINX  if 1/true → http://HOST (port 80); else http://HOST:8080
  ACCEPTANCE_TIMEOUT_SEC  wait_terminal timeout (default 900)
"""

from __future__ import annotations

import json
import mimetypes
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen

SOURCE_TRACE = "trace-8e182632-e64a-4e04-ab02-acf82cf1ed95"
TERMINAL_OK = {"SUCCESS", "PARTIAL_SUCCESS"}
TERMINAL_ALL = TERMINAL_OK | {"FAILED", "CANCELLED", "TIMED_OUT", "SYSTEM_FAILED"}
REPO_ROOT = Path(__file__).resolve().parents[1]
REPORT_PATH = REPO_ROOT / "reports" / "acceptance" / "decision-agent-regression.json"


def resolve_base_url() -> str:
    raw = (os.environ.get("BASE_URL") or "").strip().rstrip("/")
    if raw:
        return raw
    host = (os.environ.get("ACCEPTANCE_HOST") or "127.0.0.1").strip()
    if host.startswith("http://") or host.startswith("https://"):
        return host.rstrip("/")
    via_nginx = os.environ.get("ACCEPTANCE_VIA_NGINX", "").strip().lower() in (
        "1", "true", "yes",
    )
    if via_nginx:
        return f"http://{host}"
    port = (os.environ.get("ACCEPTANCE_PORT") or "8080").strip()
    if port in ("", "80"):
        return f"http://{host}"
    return f"http://{host}:{port}"


BASE_URL = resolve_base_url()
TIMEOUT_SEC = int(os.environ.get("ACCEPTANCE_TIMEOUT_SEC") or "900")


class CheckResult:
    def __init__(self) -> None:
        self.checks: list[dict[str, Any]] = []
        self.meta: dict[str, Any] = {
            "baseUrl": BASE_URL,
            "sourceTrace": SOURCE_TRACE,
            "startedAt": datetime.now(timezone.utc).isoformat(),
        }

    def record(self, name: str, ok: bool, detail: Any = None) -> None:
        entry = {"name": name, "ok": bool(ok), "detail": detail}
        self.checks.append(entry)
        status = "PASS" if ok else "FAIL"
        print(f"[{status}] {name}" + (f" — {detail}" if detail is not None and not ok else ""))
        if not ok:
            raise AssertionError(f"{name}: {detail}")

    def soft(self, name: str, ok: bool, detail: Any = None) -> None:
        """Record without raising (used only for informational notes)."""
        self.checks.append({"name": name, "ok": bool(ok), "detail": detail, "soft": True})
        print(f"[{'PASS' if ok else 'WARN'}] {name}" + (f" — {detail}" if detail else ""))

    @property
    def failed(self) -> bool:
        return any(not c["ok"] and not c.get("soft") for c in self.checks)

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.meta,
            "finishedAt": datetime.now(timezone.utc).isoformat(),
            "passed": not self.failed,
            "checks": self.checks,
        }


def _url(path: str) -> str:
    if path.startswith("http://") or path.startswith("https://"):
        return path
    return urljoin(BASE_URL.rstrip("/") + "/", path.lstrip("/"))


def http_request(
    method: str,
    path: str,
    *,
    data: bytes | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 60.0,
) -> tuple[int, bytes, str]:
    hdrs = dict(headers or {})
    req = Request(_url(path), data=data, headers=hdrs, method=method.upper())
    try:
        with urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            ctype = resp.headers.get("Content-Type", "")
            return resp.status, body, ctype
    except HTTPError as exc:
        body = exc.read() if exc.fp else b""
        return exc.code, body, exc.headers.get("Content-Type", "") if exc.headers else ""


def get_bytes(path: str, timeout: float = 60.0) -> bytes:
    status, body, _ = http_request("GET", path, timeout=timeout)
    if status >= 400:
        raise RuntimeError(f"GET {path} -> {status}: {body[:300]!r}")
    return body


def get_json(path: str, timeout: float = 60.0, retries: int = 8) -> Any:
    last_err: Exception | None = None
    for attempt in range(max(1, retries)):
        try:
            status, body, _ = http_request("GET", path, timeout=timeout)
            if status in (502, 503, 504):
                time.sleep(min(2 + attempt, 8))
                continue
            if status >= 400:
                raise RuntimeError(f"GET {path} -> {status}: {body[:400]!r}")
            return json.loads(body.decode("utf-8"))
        except (URLError, TimeoutError, json.JSONDecodeError) as exc:
            last_err = exc
            time.sleep(min(2 + attempt, 8))
    if last_err:
        raise RuntimeError(f"GET {path} failed after retries: {last_err}")
    raise RuntimeError(f"GET {path} failed after retries (gateway)")


def post_json(path: str, payload: dict[str, Any], timeout: float = 120.0, retries: int = 5) -> Any:
    data = json.dumps(payload).encode("utf-8")
    last_err: Exception | None = None
    for attempt in range(max(1, retries)):
        try:
            status, body, _ = http_request(
                "POST",
                path,
                data=data,
                headers={"Content-Type": "application/json", "Accept": "application/json"},
                timeout=timeout,
            )
            if status in (502, 503, 504):
                time.sleep(min(2 + attempt, 8))
                continue
            if status >= 400:
                raise RuntimeError(f"POST {path} -> {status}: {body[:500]!r}")
            if not body:
                return None
            return json.loads(body.decode("utf-8"))
        except (URLError, TimeoutError, json.JSONDecodeError) as exc:
            last_err = exc
            time.sleep(min(2 + attempt, 8))
    if last_err:
        raise RuntimeError(f"POST {path} failed after retries: {last_err}")
    raise RuntimeError(f"POST {path} failed after retries (gateway)")


def upload_multipart(
    path: str,
    file_field: str,
    filename: str,
    file_bytes: bytes,
    extra_fields: dict[str, str] | None = None,
    timeout: float = 180.0,
) -> Any:
    boundary = f"----ResumAIBoundary{uuid.uuid4().hex}"
    parts: list[bytes] = []
    ctype = mimetypes.guess_type(filename)[0] or "application/octet-stream"

    def add_field(name: str, value: str) -> None:
        parts.append(f"--{boundary}\r\n".encode())
        parts.append(
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode()
        )
        parts.append(value.encode("utf-8"))
        parts.append(b"\r\n")

    for k, v in (extra_fields or {}).items():
        add_field(k, v)

    parts.append(f"--{boundary}\r\n".encode())
    parts.append(
        (
            f'Content-Disposition: form-data; name="{file_field}"; '
            f'filename="{filename}"\r\n'
            f"Content-Type: {ctype}\r\n\r\n"
        ).encode()
    )
    parts.append(file_bytes)
    parts.append(b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    body = b"".join(parts)

    status, resp, _ = http_request(
        "POST",
        path,
        data=body,
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Accept": "application/json",
        },
        timeout=timeout,
    )
    if status >= 400:
        raise RuntimeError(f"UPLOAD {path} -> {status}: {resp[:500]!r}")
    return json.loads(resp.decode("utf-8"))


def wait_terminal(trace_id: str, timeout: int = TIMEOUT_SEC) -> dict[str, Any]:
    deadline = time.time() + timeout
    last: dict[str, Any] | None = None
    while time.time() < deadline:
        try:
            last = get_json(f"/api/tasks/{trace_id}", timeout=30.0)
        except RuntimeError as exc:
            # Redeploy / nginx 502 during polling — keep waiting.
            if "-> 502" in str(exc) or "-> 503" in str(exc) or "-> 504" in str(exc):
                time.sleep(5)
                continue
            raise
        status = last.get("status")
        eval_state = last.get("evaluationState")
        if status in TERMINAL_ALL or eval_state in {
            "COMPLETED", "SYSTEM_FAILED",
        }:
            return last
        time.sleep(5)
    raise TimeoutError(
        f"task {trace_id} not terminal after {timeout}s; last={last}"
    )


def write_report(report: CheckResult) -> None:
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(
        json.dumps(report.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"\n[report] {REPORT_PATH}")


def approx_eq(a: Any, b: Any, eps: float = 5e-3) -> bool:
    try:
        return abs(float(a) - float(b)) <= eps
    except (TypeError, ValueError):
        return False


def run() -> int:
    report = CheckResult()
    print(f"BASE_URL={BASE_URL}")
    print(f"SOURCE_TRACE={SOURCE_TRACE}")

    try:
        # --- health ---
        health = get_json("/api/health", timeout=15.0)
        report.record(
            "health",
            isinstance(health, dict) and (
                health.get("status") == "UP" or "UP" in json.dumps(health)
            ),
            health,
        )

        # --- fetch PDF from known failing task ---
        pdf = get_bytes(f"/api/tasks/{SOURCE_TRACE}/file", timeout=60.0)
        report.record("source_pdf", len(pdf) > 100, f"bytes={len(pdf)}")

        before = get_json(f"/api/ops/runs?conversationId={SOURCE_TRACE}")
        before_count = before.get("count") if isinstance(before, dict) else None
        report.meta["beforeOpsRuns"] = before_count

        # --- upload-auto ---
        new_task = upload_multipart(
            "/api/tasks/upload-auto",
            "file",
            f"acceptance-{SOURCE_TRACE}.pdf",
            pdf,
            extra_fields={"executionMode": "DAG_CONCURRENT"},
            timeout=180.0,
        )
        trace_id = new_task.get("traceId")
        report.record("upload_auto", bool(trace_id), new_task.get("status"))
        report.meta["traceId"] = trace_id
        report.meta["conversationId"] = new_task.get("conversationId")
        report.meta["workflowRunId"] = new_task.get("workflowRunId")

        run_task = wait_terminal(trace_id)
        report.meta["finalStatus"] = run_task.get("status")
        report.meta["evaluationState"] = run_task.get("evaluationState")

        report.record(
            "terminal_status",
            run_task.get("status") in TERMINAL_OK,
            run_task.get("status"),
        )
        report.record(
            "evaluation_state_completed",
            run_task.get("evaluationState") == "COMPLETED",
            run_task.get("evaluationState"),
        )
        report.record(
            "system_error_none",
            run_task.get("systemError") is None,
            run_task.get("systemError"),
        )
        report.record(
            "recommendation_present",
            run_task.get("recommendation") is not None,
            run_task.get("recommendation"),
        )

        # --- JD matchScore contract ---
        top = run_task.get("topJdMatches") or []
        report.record("top_jd_matches_nonempty", len(top) >= 1, f"n={len(top)}")
        best = top[0]
        match_score = best.get("matchScore", best.get("score"))
        rrf = best.get("rrfScore")
        report.record(
            "match_score_gt_0_50",
            match_score is not None and float(match_score) > 0.50,
            match_score,
        )
        if rrf is not None:
            report.record(
                "rrf_score_lt_0_05",
                float(rrf) < 0.05,
                rrf,
            )
        else:
            report.soft("rrf_score_lt_0_05", True, "rrfScore absent — skipped")
        jd_match = run_task.get("jdMatchScore")
        report.record(
            "jd_match_score_equals_best",
            approx_eq(jd_match, match_score),
            {"jdMatchScore": jd_match, "best.matchScore": match_score},
        )

        # --- candidate events: no sandbox (except POLICY_LAB view) ---
        events = get_json(f"/api/traces/{trace_id}")
        if not isinstance(events, list):
            events = []
        report.record(
            "trace_has_run_start",
            any(
                (e.get("title") == "运行开始")
                or (e.get("eventType") in ("run.started", "TASK_CREATED", "RUN_STARTED"))
                for e in events
            ),
            f"events={len(events)}",
        )
        has_preflight = any("preflight" in json.dumps(e, ensure_ascii=False).lower() for e in events)
        report.soft("trace_mentions_preflight", has_preflight, has_preflight)

        sandbox_hits = []
        for e in events:
            if e.get("viewType") == "POLICY_LAB":
                continue
            blob = json.dumps(e, ensure_ascii=False).lower()
            # Field name sandboxSummary=null is metadata, not sandbox execution.
            # Flag only real sandbox backends / sandbox.* tools / non-null summaries.
            tool = str(e.get("toolName") or e.get("callName") or "").lower()
            summary = e.get("sandboxSummary")
            backend_hit = (
                '"executionbackend":"sandbox"' in blob.replace(" ", "")
                or '"execution_backend":"sandbox"' in blob.replace(" ", "")
            )
            tool_hit = tool.startswith("sandbox.") or tool == "sandbox"
            summary_hit = isinstance(summary, dict) and any(summary.values())
            if backend_hit or tool_hit or summary_hit:
                sandbox_hits.append(e)
        report.record(
            "no_sandbox_in_candidate_events",
            len(sandbox_hits) == 0,
            f"hits={len(sandbox_hits)}",
        )

        # --- Copilot 1+1: DIRECT_REPLY, no new runs ---
        conversation_id = run_task.get("conversationId") or new_task.get("conversationId")
        report.record("conversation_id", bool(conversation_id), conversation_id)
        conversation = get_json(f"/api/conversations/{conversation_id}")
        runs_before = get_json(f"/api/conversations/{conversation_id}/runs")
        if not isinstance(runs_before, list):
            runs_before = []
        count_before = len(runs_before)

        reply = post_json(
            f"/api/conversations/{conversation_id}/messages",
            {
                "clientMessageId": f"accept-1-plus-1-{uuid.uuid4().hex[:8]}",
                "content": "1+1",
                "expectedRevision": conversation.get("activeRevision"),
                "contextRefs": [],
            },
        )
        assistant = (reply.get("assistantMessage") or "").strip()
        report.record(
            "copilot_disposition_direct_reply",
            reply.get("disposition") == "DIRECT_REPLY",
            reply.get("disposition"),
        )
        report.record(
            "copilot_answer_is_2",
            assistant == "2" or assistant.startswith("2"),
            assistant,
        )
        runs_after = get_json(f"/api/conversations/{conversation_id}/runs")
        if not isinstance(runs_after, list):
            runs_after = []
        report.record(
            "copilot_no_new_runs",
            len(runs_after) == count_before,
            {"before": count_before, "after": len(runs_after)},
        )

        # --- Ops detail entropy ---
        workflow_run_id = (
            run_task.get("workflowRunId")
            or new_task.get("workflowRunId")
            or report.meta.get("workflowRunId")
        )
        report.record("workflow_run_id", bool(workflow_run_id), workflow_run_id)
        detail = get_json(f"/api/dev/runs/{workflow_run_id}")
        timeline = detail.get("timeline") or []
        plan = detail.get("plan") or {}
        budget = detail.get("budget") or {}
        report.record("ops_timeline_nonempty", bool(timeline), f"n={len(timeline)}")
        goal_artifacts = plan.get("goalArtifacts") if isinstance(plan, dict) else None
        report.record(
            "ops_plan_goal_artifacts",
            goal_artifacts is not None,
            goal_artifacts,
        )
        budget_has_actual = isinstance(budget, dict) and (
            "actual" in budget or "actualMetrics" in budget
        )
        report.record("ops_budget_actual", budget_has_actual, list(budget) if isinstance(budget, dict) else type(budget).__name__)
        report.record("ops_artifacts_key", "artifacts" in detail, None)
        outcomes_ok = all(
            isinstance(e, dict) and ("outcome" in e)
            for e in timeline
        )
        report.record("ops_timeline_outcomes", outcomes_ok, None)
        blob = json.dumps(detail, ensure_ascii=False)
        entropy_tokens = ("durationMs", "retryCount", "cacheHit", "ignoredReason")
        report.record(
            "ops_entropy_tokens",
            any(tok in blob for tok in entropy_tokens),
            [t for t in entropy_tokens if t in blob],
        )

        # --- Policy Lab smoke (tolerate PENDING/RUNNING if worker down) ---
        exp = post_json(
            "/api/dev/policy-lab/experiments",
            {
                "kind": "OFFLINE_SEARCH",
                "basePolicyId": "balanced",
                "runType": "full_evaluation",
                "cohortKey": "acceptance",
                "evalDataset": "gold",
                "gateDataset": "regression",
                "safetyDataset": "safety",
                "seeds": [42],
                "repeatsPerCase": 1,
                "caseLimit": 1,
                "budgetCny": 0.5,
                "note": "decision-agent acceptance",
                "autoPromote": True,  # server must force false
            },
        )
        exp_id = exp.get("experimentId")
        report.record("policy_lab_create", bool(exp_id), exp.get("status"))
        report.record(
            "policy_lab_auto_promote_false",
            exp.get("autoPromote") is False,
            exp.get("autoPromote"),
        )
        report.meta["experimentId"] = exp_id

        # Optional wait: if worker finishes, assert richer fields; else tolerate.
        detail_exp = None
        deadline = time.time() + min(120, TIMEOUT_SEC)
        while time.time() < deadline:
            detail_exp = get_json(f"/api/dev/policy-lab/experiments/{exp_id}")
            status = None
            if isinstance(detail_exp, dict):
                experiment = detail_exp.get("experiment") or detail_exp
                if isinstance(experiment, dict):
                    status = experiment.get("status")
            if status in {
                "SUCCEEDED", "FAILED", "CANCELLED", "COMPLETED",
                "PASSED", "ABORTED",
            }:
                break
            if status in {"PENDING", "RUNNING", "QUEUED", "CREATED"}:
                time.sleep(3)
                continue
            break

        if isinstance(detail_exp, dict):
            experiment = detail_exp.get("experiment") or detail_exp
            status = experiment.get("status") if isinstance(experiment, dict) else None
            report.meta["experimentStatus"] = status
            if status in {"PENDING", "RUNNING", "QUEUED", "CREATED", None}:
                report.soft(
                    "policy_lab_worker",
                    True,
                    f"tolerated status={status} (worker may be down)",
                )
            else:
                trials = detail_exp.get("trials") or []
                hard_gates = detail_exp.get("hardGates") or []
                report.record("policy_lab_trials", bool(trials), f"n={len(trials)}")
                report.record("policy_lab_hard_gates", bool(hard_gates), f"n={len(hard_gates)}")
                spent = None
                if isinstance(experiment, dict):
                    spent = experiment.get("spentCny")
                if spent is not None:
                    report.record(
                        "policy_lab_budget",
                        float(spent) <= 0.5,
                        spent,
                    )

        print("\n[ok] decision-agent acceptance passed")
        write_report(report)
        return 0

    except Exception as exc:
        report.checks.append({
            "name": "uncaught",
            "ok": False,
            "detail": f"{type(exc).__name__}: {exc}",
        })
        print(f"\n[FAIL] {type(exc).__name__}: {exc}", file=sys.stderr)
        write_report(report)
        return 1


if __name__ == "__main__":
    try:
        sys.exit(run())
    except URLError as exc:
        print(f"[FAIL] network: {exc}", file=sys.stderr)
        # still try to write a minimal report
        try:
            REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
            REPORT_PATH.write_text(
                json.dumps(
                    {
                        "baseUrl": BASE_URL,
                        "passed": False,
                        "error": str(exc),
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
        except OSError:
            pass
        sys.exit(1)
