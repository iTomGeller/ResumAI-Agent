from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from typing import Any, Dict, Optional

import httpx

from app.config import settings
from app.runtime.events import RuntimeEmitter

logger = logging.getLogger(__name__)

SANDBOX_TOOLS = {
    "parse_resume", "check_timeline", "calculate_jd_coverage", "locate_evidence",
    "verify_report_evidence", "resume_lint", "validate_report_schema",
    "evaluate_policy_output",
}


class SandboxUnavailable(RuntimeError):
    pass


class SandboxClient:
    """Policy Lab only. Candidate evaluation must never import this class."""

    def __init__(self, emitter: RuntimeEmitter, run_id: str, conversation_id: str,
                 timeout_seconds: int = 90, *,
                 purpose: str = "POLICY_EVOLUTION",
                 experiment_id: str = "",
                 trial_id: str = "") -> None:
        self.emitter = emitter
        self.run_id = run_id
        self.conversation_id = conversation_id
        self.timeout_seconds = timeout_seconds
        self.purpose = purpose
        self.experiment_id = experiment_id
        self.trial_id = trial_id
        self.base_url = os.getenv(
            "SANDBOX_MANAGER_URL", "http://resumai-sandbox-manager:8070").rstrip("/")
        self.enabled = os.getenv("SANDBOX_ENABLED", "true").lower() != "false"

    def _headers(self) -> Dict[str, str]:
        return {
            "Content-Type": "application/json",
            "X-Internal-Token": settings.workflow_internal_token,
        }

    async def invoke(self, tool: str, args: Dict[str, Any]) -> Dict[str, Any]:
        if tool not in SANDBOX_TOOLS:
            raise ValueError(f"tool not in sandbox allowlist: {tool}")
        if not self.enabled:
            raise SandboxUnavailable("sandbox disabled by configuration")
        if self.purpose.upper() in {"CANDIDATE_EVALUATION", "LEGACY_CANDIDATE_EVALUATION"}:
            raise SandboxUnavailable("candidate evaluation is forbidden in Policy Lab sandbox")
        if not self.experiment_id or not self.trial_id:
            raise SandboxUnavailable("experimentId and trialId are required for sandbox invoke")
        sandbox_id = f"sbx-{uuid.uuid4().hex[:16]}"
        await self.emitter.emit("sandbox.started", tool_name=tool, payload={
            "sandboxId": sandbox_id, "tool": tool,
            "purpose": self.purpose, "experimentId": self.experiment_id,
            "trialId": self.trial_id})
        started = time.monotonic()
        try:
            payload = {
                "sandboxId": sandbox_id,
                "runId": self.run_id,
                "conversationId": self.conversation_id,
                "purpose": self.purpose,
                "experimentId": self.experiment_id,
                "trialId": self.trial_id,
                "tool": tool,
                "args": args,
                "timeoutSeconds": self.timeout_seconds,
            }
            timeout = httpx.Timeout(connect=8.0, read=float(self.timeout_seconds + 30),
                                    write=30.0, pool=10.0)
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.post(
                    f"{self.base_url}/sandbox/invoke", json=payload, headers=self._headers())
            if response.status_code >= 400:
                raise SandboxUnavailable(
                    f"sandbox manager HTTP {response.status_code}: {response.text[:200]}")
            data = response.json()
            duration_ms = int((time.monotonic() - started) * 1000)
            status = data.get("status", "FAILED")
            if status == "SUCCEEDED":
                await self.emitter.emit("sandbox.completed", tool_name=tool, payload={
                    "sandboxId": sandbox_id, "durationMs": duration_ms})
                return data.get("result") or {}
            error = data.get("error") or f"sandbox status {status}"
            await self.emitter.emit("sandbox.failed", tool_name=tool, payload={
                "sandboxId": sandbox_id, "status": status,
                "error": str(error)[:300], "durationMs": duration_ms})
            raise SandboxUnavailable(f"{status}: {error}")
        except asyncio.CancelledError:
            await self._cancel_quietly(sandbox_id)
            raise
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            await self.emitter.emit("sandbox.failed", tool_name=tool, payload={
                "sandboxId": sandbox_id, "error": str(exc)[:300]})
            await self._cancel_quietly(sandbox_id)
            raise SandboxUnavailable(f"sandbox transport error: {exc}") from exc

    async def _cancel_quietly(self, sandbox_id: str) -> None:
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                await client.post(
                    f"{self.base_url}/sandbox/{sandbox_id}/cancel",
                    json={"reason": "run_cancelled"}, headers=self._headers())
        except Exception as exc:  # noqa: BLE001 - best effort during teardown
            logger.info("sandbox cancel notify failed %s: %s", sandbox_id, exc)
