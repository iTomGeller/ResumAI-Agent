from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from app import tools as legacy_tools
from app.runtime.events import RuntimeEmitter
from app.runtime.models import BudgetExceeded, RunBudget
from app.runtime.sandbox import SANDBOX_TOOLS, SandboxClient, SandboxUnavailable

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    input_schema: Dict[str, Any]
    output_schema: Dict[str, Any]
    timeout_seconds: float = 30.0
    max_retries: int = 1
    idempotent: bool = True
    side_effect_level: str = "read_only"  # read_only / internal_write / external
    network_policy: str = "internal"      # none / internal / gateway
    required_secrets: tuple = ()
    kind: str = "internal"                # internal / sandbox / gateway


@dataclass
class ToolCallResult:
    tool_call_id: str
    tool: str
    status: str
    result: Any
    error: Optional[str] = None
    duration_ms: int = 0
    retries: int = 0


class ToolValidationError(ValueError):
    pass


def _validate(schema: Dict[str, Any], payload: Dict[str, Any], direction: str) -> None:
    """Minimal JSON-schema-ish validation: required keys and primitive types."""
    required = schema.get("required", [])
    for key in required:
        if key not in payload or payload[key] in (None, ""):
            raise ToolValidationError(f"{direction} missing required field: {key}")
    properties = schema.get("properties", {})
    for key, spec in properties.items():
        if key not in payload or payload[key] is None:
            continue
        expected = spec.get("type")
        value = payload[key]
        ok = (
            expected is None
            or (expected == "string" and isinstance(value, str))
            or (expected == "integer" and isinstance(value, int) and not isinstance(value, bool))
            or (expected == "number" and isinstance(value, (int, float)) and not isinstance(value, bool))
            or (expected == "boolean" and isinstance(value, bool))
            or (expected == "array" and isinstance(value, list))
            or (expected == "object" and isinstance(value, dict))
        )
        if not ok:
            raise ToolValidationError(
                f"{direction} field {key} expected {expected}, got {type(value).__name__}")


TEXT_ARGS = {"type": "object", "properties": {
    "resumeText": {"type": "string"}}, "required": ["resumeText"]}
ANY_OBJECT = {"type": "object", "properties": {}}
SUCCESS_SCHEMA = {"type": "object", "properties": {"success": {"type": "boolean"}}}


def build_tool_definitions() -> Dict[str, ToolDefinition]:
    definitions: Dict[str, ToolDefinition] = {}

    def add(defn: ToolDefinition) -> None:
        definitions[defn.name] = defn

    # ---- internal Java/Milvus retrieval tools (reused business tools) ----
    add(ToolDefinition(
        "resume_semantic_search", "在当前简历分块上做混合语义检索，返回证据片段",
        {"type": "object", "properties": {"query": {"type": "string"},
                                          "topK": {"type": "integer"},
                                          "resumeText": {"type": "string"}},
         "required": ["query"]},
        ANY_OBJECT, timeout_seconds=25.0, kind="internal"))
    add(ToolDefinition(
        "jd_match_search", "在 JD 库中检索与简历最匹配的岗位",
        TEXT_ARGS, ANY_OBJECT, timeout_seconds=25.0, kind="internal"))
    add(ToolDefinition(
        "knowledge_search", "检索岗位知识库（评估规则、技能知识）",
        {"type": "object", "properties": {"query": {"type": "string"},
                                          "topK": {"type": "integer"}},
         "required": ["query"]},
        ANY_OBJECT, timeout_seconds=20.0, kind="internal"))
    add(ToolDefinition(
        "timeline_validator", "基于规则的简历时间线快速校验（进程内）",
        TEXT_ARGS, SUCCESS_SCHEMA, timeout_seconds=10.0, kind="internal"))
    add(ToolDefinition(
        "external_profile_lookup", "通过受控 Tool Gateway 查询简历声明的公开主页(GitHub等)",
        TEXT_ARGS, ANY_OBJECT, timeout_seconds=45.0, max_retries=0,
        network_policy="gateway", kind="gateway"))

    # ---- sandbox tools (fixed allowlist, executed in ephemeral docker) ----
    sandbox_schemas: Dict[str, Dict[str, Any]] = {
        "parse_resume": {"type": "object", "properties": {
            "resumeText": {"type": "string"}, "resumeBase64": {"type": "string"},
            "filename": {"type": "string"}}},
        "check_timeline": TEXT_ARGS,
        "calculate_jd_coverage": {"type": "object", "properties": {
            "resumeText": {"type": "string"}, "jdText": {"type": "string"},
            "requirements": {"type": "array"}}, "required": ["resumeText"]},
        "locate_evidence": {"type": "object", "properties": {
            "resumeText": {"type": "string"}, "claims": {"type": "array"}},
            "required": ["resumeText", "claims"]},
        "verify_report_evidence": {"type": "object", "properties": {
            "resumeText": {"type": "string"}, "jdText": {"type": "string"},
            "claims": {"type": "array"}}, "required": ["resumeText", "claims"]},
        "resume_lint": {"type": "object", "properties": {
            "resumeText": {"type": "string"}, "rewrittenText": {"type": "string"}}},
        "validate_report_schema": {"type": "object", "properties": {
            "report": {}}, "required": ["report"]},
        "evaluate_policy_output": {"type": "object", "properties": {
            "answer": {"type": "string"}, "resumeText": {"type": "string"},
            "mustFind": {"type": "array"}, "mustNotClaim": {"type": "array"}},
            "required": ["answer"]},
    }
    for tool_name in SANDBOX_TOOLS:
        add(ToolDefinition(
            tool_name, f"Sandbox 工具 {tool_name}（无网络、只读根文件系统、非 root）",
            sandbox_schemas.get(tool_name, ANY_OBJECT), SUCCESS_SCHEMA,
            timeout_seconds=90.0, max_retries=0, network_policy="none", kind="sandbox"))
    return definitions


class ToolExecutor:
    """Budgeted, cancellable tool execution with schema validation, timeout,
    retry (idempotent read-only tools only), progress events and duplicate-
    signature accounting for the loop guard."""

    def __init__(self, emitter: RuntimeEmitter, budget: RunBudget,
                 sandbox: SandboxClient, *, max_tool_calls_run: int,
                 tool_timeout_seconds: float, run_context: Dict[str, Any]) -> None:
        self.emitter = emitter
        self.budget = budget
        self.sandbox = sandbox
        self.max_tool_calls_run = max_tool_calls_run
        self.tool_timeout_seconds = tool_timeout_seconds
        self.run_context = run_context
        self.definitions = build_tool_definitions()
        self.signature_counts: Dict[str, int] = {}
        self.call_log: List[ToolCallResult] = []

    def catalog_for(self, tool_names: List[str]) -> List[Dict[str, Any]]:
        catalog = []
        for name in tool_names:
            defn = self.definitions.get(name)
            if defn:
                catalog.append({
                    "name": defn.name,
                    "description": defn.description,
                    "inputSchema": defn.input_schema,
                })
        return catalog

    @staticmethod
    def signature(tool: str, args: Dict[str, Any]) -> str:
        canonical = json.dumps(args or {}, sort_keys=True, ensure_ascii=False)[:2000]
        return f"{tool}:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()[:16]}"

    async def execute(self, agent_id: str, tool: str, args: Dict[str, Any]) -> ToolCallResult:
        defn = self.definitions.get(tool)
        if defn is None:
            return self._reject(agent_id, tool, args, "TOOL_NOT_ALLOWED",
                                f"工具不在白名单中: {tool}")
        if self.budget.tool_calls >= self.max_tool_calls_run:
            raise BudgetExceeded("maxToolCallsPerRun", f"limit={self.max_tool_calls_run}")

        args = dict(args or {})
        # Inject run context the model must not fabricate.
        if "resumeText" in defn.input_schema.get("properties", {}) and not args.get("resumeText"):
            args["resumeText"] = self.run_context.get("resumeText") or ""
        if tool == "calculate_jd_coverage" and not args.get("jdText"):
            args["jdText"] = self.run_context.get("jobDescription") or ""

        try:
            _validate(defn.input_schema, args, "input")
        except ToolValidationError as exc:
            return self._reject(agent_id, tool, args, "INPUT_SCHEMA", str(exc))

        signature = self.signature(tool, args)
        self.signature_counts[signature] = self.signature_counts.get(signature, 0) + 1

        tool_call_id = f"tc-{uuid.uuid4().hex[:16]}"
        self.budget.tool_calls += 1
        await self.emitter.emit("tool.started", agent_id=agent_id, tool_name=tool, payload={
            "toolCallId": tool_call_id,
            "arguments": _preview_args(args),
            "idempotencyKey": signature,
            "sideEffectLevel": defn.side_effect_level,
            "retryCount": 0,
        })
        started = time.monotonic()
        retries = 0
        max_retries = defn.max_retries if (defn.idempotent and defn.side_effect_level == "read_only") else 0
        last_error: Optional[str] = None
        while retries <= max_retries:
            try:
                timeout = min(defn.timeout_seconds, self.tool_timeout_seconds) \
                    if defn.kind != "sandbox" else defn.timeout_seconds
                raw = await asyncio.wait_for(self._dispatch(defn, args), timeout=timeout)
                result = self._normalize_result(raw)
                try:
                    if isinstance(result, dict):
                        _validate(defn.output_schema, result, "output")
                except ToolValidationError as exc:
                    last_error = f"OUTPUT_SCHEMA: {exc}"
                    break
                duration_ms = int((time.monotonic() - started) * 1000)
                call = ToolCallResult(tool_call_id, tool, "SUCCEEDED", result,
                                      duration_ms=duration_ms, retries=retries)
                self.call_log.append(call)
                await self.emitter.emit("tool.completed", agent_id=agent_id, tool_name=tool,
                                        payload={
                                            "toolCallId": tool_call_id,
                                            "durationMs": duration_ms,
                                            "retryCount": retries,
                                            "resultPreview": _preview(result),
                                        })
                return call
            except asyncio.CancelledError:
                await self.emitter.emit("tool.failed", agent_id=agent_id, tool_name=tool,
                                        payload={"toolCallId": tool_call_id,
                                                 "error": "cancelled"})
                raise
            except asyncio.TimeoutError:
                last_error = f"TIMEOUT after {defn.timeout_seconds}s"
            except SandboxUnavailable as exc:
                last_error = f"SANDBOX: {exc}"
                break  # sandbox errors are not retried here
            except Exception as exc:  # noqa: BLE001 - tool boundary
                last_error = f"{type(exc).__name__}: {exc}"
            retries += 1
            if retries <= max_retries:
                await self.emitter.emit("tool.progress", agent_id=agent_id, tool_name=tool,
                                        payload={"toolCallId": tool_call_id,
                                                 "progress": f"retry {retries}",
                                                 "error": (last_error or "")[:200]})
                await asyncio.sleep(0.8 * retries)

        duration_ms = int((time.monotonic() - started) * 1000)
        call = ToolCallResult(tool_call_id, tool, "FAILED", None,
                              error=last_error, duration_ms=duration_ms, retries=retries)
        self.call_log.append(call)
        await self.emitter.emit("tool.failed", agent_id=agent_id, tool_name=tool, payload={
            "toolCallId": tool_call_id, "error": (last_error or "")[:300],
            "durationMs": duration_ms, "retryCount": retries})
        return call

    async def _dispatch(self, defn: ToolDefinition, args: Dict[str, Any]) -> Any:
        if defn.kind == "sandbox":
            return await self.sandbox.invoke(defn.name, args)
        if defn.name == "resume_semantic_search":
            return await legacy_tools.java_resume_search(
                query=str(args.get("query") or ""),
                top_k=int(args.get("topK") or 5),
                resume_text=str(args.get("resumeText") or ""),
                jd_requirements=str(self.run_context.get("jobDescription") or "")[:2000],
                strategy="hybrid")
        if defn.name == "jd_match_search":
            return await legacy_tools.java_jd_search(
                resume_text=str(args.get("resumeText") or ""), top_k=3)
        if defn.name == "knowledge_search":
            return await legacy_tools.java_knowledge_search(
                query=str(args.get("query") or ""), top_k=int(args.get("topK") or 5))
        if defn.name == "timeline_validator":
            return await legacy_tools.timeline_validator(
                resume_text=str(args.get("resumeText") or ""))
        if defn.name == "external_profile_lookup":
            return await legacy_tools.java_external_profile(
                resume_text=str(args.get("resumeText") or ""))
        raise ToolValidationError(f"no dispatcher for tool {defn.name}")

    @staticmethod
    def _normalize_result(raw: Any) -> Any:
        if isinstance(raw, (dict, list)):
            return raw
        if isinstance(raw, str):
            text = raw.strip()
            if text.startswith("{") or text.startswith("["):
                try:
                    return json.loads(text)
                except json.JSONDecodeError:
                    return {"success": True, "text": text[:8000]}
            return {"success": True, "text": text[:8000]}
        return {"success": True, "value": raw}

    def _reject(self, agent_id: str, tool: str, args: Dict[str, Any],
                code: str, message: str) -> ToolCallResult:
        call = ToolCallResult(f"tc-{uuid.uuid4().hex[:16]}", tool, "REJECTED",
                              None, error=f"{code}: {message}")
        self.call_log.append(call)
        logger.info("tool rejected agent=%s tool=%s: %s", agent_id, tool, message)
        return call

    def metrics(self) -> Dict[str, Any]:
        failed = sum(1 for c in self.call_log if c.status == "FAILED")
        return {
            "toolCalls": len(self.call_log),
            "toolFailures": failed,
            "duplicateSignatures": sum(1 for v in self.signature_counts.values() if v > 1),
        }


def _preview(value: Any, limit: int = 500) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value
    except (TypeError, ValueError):
        text = str(value)
    return text[:limit]


def _preview_args(args: Dict[str, Any]) -> Dict[str, Any]:
    out = {}
    for key, value in (args or {}).items():
        if isinstance(value, str) and len(value) > 200:
            out[key] = value[:200] + f"...({len(value)} chars)"
        else:
            out[key] = value
    return out
