"""Sandbox Manager: runs allow-listed resume tools in ephemeral, locked-down
Docker containers. Internal-network only, token protected.

Security posture per invocation:
  network=none, read-only rootfs, non-root user, cap_drop=ALL,
  no-new-privileges, memory/cpu/pids limits, tmpfs workspace with quota,
  stdout cap, wall-clock timeout, TTL labels + orphan reaper.
Agents/users can never choose images, volumes, host paths, capabilities,
entrypoints or network settings — only a tool name and JSON arguments.
"""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

import docker
from docker.errors import APIError, ImageNotFound, NotFound
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("sandbox-manager")

ALLOWED_TOOLS = {
    "parse_resume", "check_timeline", "calculate_jd_coverage", "locate_evidence",
    "verify_report_evidence", "resume_lint", "validate_report_schema",
    "evaluate_policy_output",
}

WORKER_IMAGE = os.getenv("SANDBOX_WORKER_IMAGE", "resumai-sandbox-worker:latest")
MAX_CONCURRENT = int(os.getenv("SANDBOX_MAX_CONCURRENT", "2"))
MEM_LIMIT = os.getenv("SANDBOX_MEM_LIMIT", "384m")
CPU_QUOTA = float(os.getenv("SANDBOX_CPU", "0.5"))
PIDS_LIMIT = int(os.getenv("SANDBOX_PIDS_LIMIT", "64"))
TTL_SECONDS = int(os.getenv("SANDBOX_TTL_SECONDS", "240"))
DEFAULT_TIMEOUT = int(os.getenv("SANDBOX_DEFAULT_TIMEOUT", "90"))
STDOUT_CAP = int(os.getenv("SANDBOX_STDOUT_CAP", str(1024 * 1024)))
INTERNAL_TOKEN = os.getenv("WORKFLOW_INTERNAL_TOKEN", "")
JAVA_BACKEND_URL = os.getenv("JAVA_BACKEND_URL", "http://ai-resume-backend:8080")

app = FastAPI(title="ResumAI Sandbox Manager", version="1.0.0")

_docker: Optional[docker.DockerClient] = None
_semaphore = asyncio.Semaphore(MAX_CONCURRENT)
_active: Dict[str, str] = {}  # sandboxId -> containerId


def client() -> docker.DockerClient:
    global _docker
    if _docker is None:
        _docker = docker.from_env()
    return _docker


@app.middleware("http")
async def require_token(request: Request, call_next):
    if request.url.path in {"/health", "/ready"}:
        return await call_next(request)
    if INTERNAL_TOKEN and INTERNAL_TOKEN != "change-me":
        supplied = request.headers.get("X-Internal-Token", "")
        if not hmac.compare_digest(supplied, INTERNAL_TOKEN):
            return JSONResponse(status_code=401, content={"detail": "invalid internal token"})
    return await call_next(request)


class InvokeRequest(BaseModel):
    sandboxId: str
    runId: str
    conversationId: str = ""
    tool: str
    args: Dict[str, Any] = Field(default_factory=dict)
    timeoutSeconds: int = DEFAULT_TIMEOUT


class CancelBody(BaseModel):
    reason: str = "cancelled"


@app.get("/health")
async def health() -> Dict[str, Any]:
    return {"status": "UP", "active": len(_active),
            "maxConcurrent": MAX_CONCURRENT}


@app.get("/ready")
async def ready() -> Dict[str, Any]:
    try:
        client().ping()
        client().images.get(WORKER_IMAGE)
    except ImageNotFound:
        raise HTTPException(status_code=503, detail=f"worker image missing: {WORKER_IMAGE}")
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"docker unavailable: {exc}")
    return {"status": "READY", "workerImage": WORKER_IMAGE}


@app.post("/sandbox/invoke")
async def invoke(request: InvokeRequest) -> Dict[str, Any]:
    if request.tool not in ALLOWED_TOOLS:
        raise HTTPException(status_code=400, detail=f"tool not allowed: {request.tool}")
    timeout = min(max(request.timeoutSeconds, 10), 300)
    payload = json.dumps({"tool": request.tool, "args": request.args},
                         ensure_ascii=False)
    if len(payload) > 6 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="args payload too large")

    async with _semaphore:
        return await asyncio.get_event_loop().run_in_executor(
            None, _run_container, request, payload, timeout)


def _run_container(request: InvokeRequest, payload: str, timeout: int) -> Dict[str, Any]:
    started = time.monotonic()
    expire_at = datetime.now(timezone.utc) + timedelta(seconds=TTL_SECONDS + timeout)
    container = None
    status = "FAILED"
    exit_code: Optional[int] = None
    stdout_tail = ""
    stderr_tail = ""
    error: Optional[str] = None
    result: Optional[Dict[str, Any]] = None
    try:
        container = client().containers.create(
            WORKER_IMAGE,
            command=["python", "/opt/sandbox/run_tool.py"],
            environment={"SANDBOX_TOOL": request.tool},
            network_mode="none",
            read_only=True,
            user="65534:65534",
            cap_drop=["ALL"],
            security_opt=["no-new-privileges"],
            mem_limit=MEM_LIMIT,
            memswap_limit=MEM_LIMIT,
            nano_cpus=int(CPU_QUOTA * 1e9),
            pids_limit=PIDS_LIMIT,
            tmpfs={"/workspace": "rw,size=64m,mode=1777",
                   "/tmp": "rw,size=16m,mode=1777"},
            working_dir="/workspace",
            labels={
                "project": "resumai",
                "sandbox": "true",
                "runId": request.runId,
                "conversationId": request.conversationId,
                "sandboxId": request.sandboxId,
                "expireAt": expire_at.isoformat(),
            },
            stdin_open=True,
            detach=True,
        )
        _active[request.sandboxId] = container.id
        _report(request, "RUNNING", container.id, None, None, None, None, expire_at)
        container.start()
        socket = container.attach_socket(params={"stdin": 1, "stream": 1})
        socket._sock.sendall(payload.encode("utf-8"))
        socket._sock.shutdown(1)  # close stdin so the tool reads EOF
        socket.close()

        wait_result = container.wait(timeout=timeout)
        exit_code = int(wait_result.get("StatusCode", -1))
        stdout_tail = container.logs(stdout=True, stderr=False, tail=2000)[:STDOUT_CAP] \
            .decode("utf-8", errors="replace")
        stderr_tail = container.logs(stdout=False, stderr=True, tail=400)[:65536] \
            .decode("utf-8", errors="replace")
        inspect = client().api.inspect_container(container.id)
        oom_killed = bool(inspect.get("State", {}).get("OOMKilled"))
        if oom_killed:
            status = "OOM_KILLED"
            error = "sandbox container was OOM killed"
        elif exit_code == 0:
            try:
                result = json.loads(stdout_tail.strip().splitlines()[-1])
                status = "SUCCEEDED"
            except (json.JSONDecodeError, IndexError) as exc:
                status = "FAILED"
                error = f"tool produced no valid JSON: {exc}"
        else:
            status = "FAILED"
            error = f"exit code {exit_code}: {stderr_tail[:300]}"
    except docker.errors.ContainerError as exc:
        error = f"container error: {exc}"
    except (APIError, NotFound) as exc:
        error = f"docker api error: {exc}"
    except Exception as exc:  # noqa: BLE001
        if "timed out" in str(exc).lower() or "timeout" in str(exc).lower():
            status = "TIMED_OUT"
            error = f"sandbox tool exceeded {timeout}s"
        else:
            error = f"{type(exc).__name__}: {exc}"
    finally:
        _active.pop(request.sandboxId, None)
        if container is not None:
            try:
                container.remove(force=True)
            except Exception:  # noqa: BLE001 - double destroy is fine
                pass
    duration_ms = int((time.monotonic() - started) * 1000)
    _report(request, status, container.id if container else None, exit_code,
            duration_ms, stdout_tail[-3800:], error, expire_at)
    return {
        "sandboxId": request.sandboxId,
        "status": status,
        "exitCode": exit_code,
        "durationMs": duration_ms,
        "result": result,
        "error": error,
    }


@app.post("/sandbox/{sandbox_id}/cancel")
async def cancel(sandbox_id: str, body: CancelBody) -> Dict[str, Any]:
    container_id = _active.pop(sandbox_id, None)
    if container_id is None:
        return {"sandboxId": sandbox_id, "status": "NOT_ACTIVE"}
    try:
        container = client().containers.get(container_id)
        container.remove(force=True)
        return {"sandboxId": sandbox_id, "status": "CANCELLED"}
    except NotFound:
        return {"sandboxId": sandbox_id, "status": "ALREADY_GONE"}
    except Exception as exc:  # noqa: BLE001
        return {"sandboxId": sandbox_id, "status": "CANCEL_FAILED", "error": str(exc)[:200]}


@app.get("/sandbox/{sandbox_id}")
async def status(sandbox_id: str) -> Dict[str, Any]:
    container_id = _active.get(sandbox_id)
    return {"sandboxId": sandbox_id,
            "status": "RUNNING" if container_id else "NOT_ACTIVE"}


def _report(request: InvokeRequest, status: str, container_id: Optional[str],
            exit_code: Optional[int], duration_ms: Optional[int],
            stdout_tail: Optional[str], error: Optional[str],
            expire_at: datetime) -> None:
    """Persist the sandbox execution record via the Java control plane."""
    try:
        import urllib.request

        body = json.dumps({
            "sandboxId": request.sandboxId,
            "runId": request.runId,
            "conversationId": request.conversationId,
            "toolName": request.tool,
            "containerId": container_id,
            "status": status,
            "exitCode": exit_code,
            "durationMs": duration_ms,
            "stdoutTail": stdout_tail,
            "error": error,
            "expireAt": expire_at.strftime("%Y-%m-%dT%H:%M:%S"),
        }).encode("utf-8")
        http_request = urllib.request.Request(
            f"{JAVA_BACKEND_URL}/api/internal/agent-runs/sandbox-executions",
            data=body, method="POST",
            headers={"Content-Type": "application/json",
                     "X-Internal-Token": INTERNAL_TOKEN})
        urllib.request.urlopen(http_request, timeout=6)
    except Exception as exc:  # noqa: BLE001 - reporting must not break execution
        logger.info("sandbox execution report skipped: %s", exc)


async def _reaper_loop() -> None:
    """Destroy expired/orphaned sandbox containers (TTL, manager restarts)."""
    while True:
        try:
            containers = client().containers.list(
                all=True, filters={"label": ["project=resumai", "sandbox=true"]})
            now = datetime.now(timezone.utc)
            for container in containers:
                expire_raw = container.labels.get("expireAt", "")
                expired = True
                if expire_raw:
                    try:
                        expired = datetime.fromisoformat(expire_raw) < now
                    except ValueError:
                        expired = True
                known = container.id in _active.values()
                if expired and not known:
                    logger.info("reaping expired sandbox %s (%s)",
                                container.name, container.labels.get("sandboxId"))
                    try:
                        container.remove(force=True)
                    except Exception:  # noqa: BLE001
                        pass
        except Exception as exc:  # noqa: BLE001
            logger.info("reaper cycle failed: %s", exc)
        await asyncio.sleep(45)


@app.on_event("startup")
async def startup() -> None:
    asyncio.get_event_loop().create_task(_reaper_loop())
    logger.info("sandbox manager started image=%s maxConcurrent=%d",
                WORKER_IMAGE, MAX_CONCURRENT)
