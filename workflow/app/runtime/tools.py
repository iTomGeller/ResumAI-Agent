from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from app.runtime import cache, gateway
from app.runtime.builtin_tools import BUILTIN_TOOLS, BuiltinToolRegistry
from app.runtime.events import RuntimeEmitter
from app.runtime.models import BudgetExceeded, RunBudget

logger = logging.getLogger(__name__)

# Deterministic pure-function tools whose results are safe to cache by
# content hash (same input => same output, no side effects).
CACHEABLE_TOOLS = {"parse_resume", "check_timeline", "calculate_jd_coverage",
                   "resume_lint", "jd_requirements_extract"}

MCP_PROTOCOL_VERSION = "2025-11-25"
SOFT_UNAVAILABLE_STATUSES = {
    "UNAVAILABLE", "RATE_LIMITED", "NOT_CHECKED",
}


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
    kind: str = "internal"                # internal / builtin / retrieval / mcp / gateway
    mcp_server: Optional[str] = None
    protocol_version: Optional[str] = None
    skill_id: Optional[str] = None
    skill_version: Optional[str] = None
    execution_backend: str = "in_process"  # in_process / java_http / mcp_http / mcp_stdio


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


_GITHUB_REPOSITORY_URL = re.compile(
    r"https?://(?:www\.)?github\.com/"
    r"(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+)",
    re.IGNORECASE,
)
_REPOSITORY_SLUG = re.compile(
    r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_DEEPWIKI_REPOSITORY_KEYS = {
    "repo", "reponame", "repo_name", "repository", "repositoryname",
    "repository_name",
}
_PUBLIC_URL = re.compile(r"https?://[^\s)\]}>]+", re.IGNORECASE)
_MICROSOFT_STACK = re.compile(
    r"(?:\.NET|ASP\.NET|C#|Azure|PowerShell|Microsoft\s+365|Dynamics\s+365|MSSQL|SQL\s+Server)",
    re.IGNORECASE,
)
_FRAMEWORK_STACK = re.compile(
    r"(?:Spring(?:\s*Boot)?|React|Vue|Angular|Next\.js|Nuxt|Kubernetes|K8s|Kafka|"
    r"LangChain|LangGraph|FastAPI|Django|Flask|PyTorch|TensorFlow|Redis|MyBatis|Hibernate)",
    re.IGNORECASE,
)


def _normalize_repository_slug(value: Any) -> str:
    text = str(value or "").strip().strip("\"'")
    match = _GITHUB_REPOSITORY_URL.search(text)
    if match:
        owner = match.group("owner")
        repo = match.group("repo")
    else:
        candidate = text.strip("/")
        if candidate.lower().endswith(".git"):
            candidate = candidate[:-4]
        if not _REPOSITORY_SLUG.fullmatch(candidate):
            return ""
        owner, repo = candidate.split("/", 1)
    if repo.lower().endswith(".git"):
        repo = repo[:-4]
    repo = repo.rstrip(".")
    if not owner or not repo:
        return ""
    return f"{owner}/{repo}".lower()


def _declared_github_repositories(run_context: Dict[str, Any]) -> Dict[str, str]:
    """Extract only repositories explicitly present in candidate/user text."""
    texts = [
        str(run_context.get("resumeText") or ""),
        str(run_context.get("userMessage") or ""),
    ]
    for message in run_context.get("recentMessages") or []:
        if not isinstance(message, dict):
            continue
        if str(message.get("role") or "").strip().lower() not in {
                "user", "human", "hr"}:
            continue
        texts.append(str(message.get("content") or ""))

    declared: Dict[str, str] = {}
    for text in texts:
        for match in _GITHUB_REPOSITORY_URL.finditer(text):
            owner = match.group("owner")
            repo = match.group("repo")
            if repo.lower().endswith(".git"):
                repo = repo[:-4]
            repo = repo.rstrip(".")
            slug = _normalize_repository_slug(f"{owner}/{repo}")
            if slug:
                declared[slug] = f"https://github.com/{owner}/{repo}"
    return declared


def _tool_context_signals(run_context: Dict[str, Any]) -> Dict[str, bool]:
    text = "\n".join(str(run_context.get(key) or "") for key in (
        "resumeText", "jobDescription", "userMessage"))
    microsoft = bool(_MICROSOFT_STACK.search(text))
    return {
        "has_external_url": bool(_PUBLIC_URL.search(text)),
        "microsoft_stack": microsoft,
        "framework_stack": bool(_FRAMEWORK_STACK.search(text)) and not microsoft,
    }


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

    # ---- builtin tools (deterministic in-process kernels) ----
    builtin_schemas: Dict[str, Dict[str, Any]] = {
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
            "claims": {"type": "array"},
            "externalEvidence": {"type": "array"}},
            "required": ["resumeText", "claims"]},
        "resume_lint": {"type": "object", "properties": {
            "resumeText": {"type": "string"}, "rewrittenText": {"type": "string"}}},
        "validate_report_schema": {"type": "object", "properties": {
            "report": {}}, "required": ["report"]},
        "evaluate_report_quality": {"type": "object", "properties": {
            "answer": {"type": "string"}, "resumeText": {"type": "string"},
            "mustFind": {"type": "array"}, "mustNotClaim": {"type": "array"}},
            "required": ["answer"]},
    }
    for tool_name in BUILTIN_TOOLS:
        add(ToolDefinition(
            tool_name, f"Builtin 工具 {tool_name}（进程内确定性、无网络）",
            builtin_schemas.get(tool_name, ANY_OBJECT), SUCCESS_SCHEMA,
            timeout_seconds=30.0, max_retries=0, network_policy="none",
            kind="builtin", execution_backend="in_process"))

    # ---- progressive skill loading tool ----
    add(ToolDefinition(
        "load_skill",
        "加载技能完整指令。当你在可用技能摘要中看到某个技能适合当前任务时，"
        "调用此工具获取完整的执行指令。",
        {"type": "object", "properties": {
            "skill_id": {"type": "string",
                         "description": "生产 Skill ID，如 evaluate-candidate-evidence"}},
         "required": ["skill_id"]},
        {"type": "object", "properties": {
            "instructions": {"type": "string"},
            "loaded": {"type": "boolean"}}},
        timeout_seconds=2.0, max_retries=0, network_policy="none",
        kind="internal", execution_backend="in_process"))
    add(ToolDefinition(
        "read_skill_resource",
        "读取已加载技能明确列出的一个 references/scripts/assets 资源。"
        "仅在 SKILL.md 指向该资源且当前任务确实需要细节时调用。",
        {"type": "object", "properties": {
            "skill_id": {"type": "string"},
            "path": {
                "type": "string",
                "description": "load_skill 返回的 resources 中的相对路径"},
         }, "required": ["skill_id", "path"]},
        {"type": "object", "properties": {
            "content": {"type": "string"},
            "loaded": {"type": "boolean"}}},
        timeout_seconds=2.0, max_retries=0, network_policy="none",
        kind="internal", execution_backend="in_process"))

    return definitions


class ToolExecutor:
    """Budgeted, cancellable tool execution with schema validation, timeout,
    retry (idempotent read-only tools only), progress events and duplicate-
    signature accounting for the loop guard."""

    def __init__(self, emitter: RuntimeEmitter, budget: RunBudget,
                 builtin_tools: BuiltinToolRegistry, *, max_tool_calls_run: int,
                 tool_timeout_seconds: float, run_context: Dict[str, Any],
                 llm: Any = None) -> None:
        self.emitter = emitter
        self.budget = budget
        self.builtin_tools = builtin_tools
        self.max_tool_calls_run = max_tool_calls_run
        self.tool_timeout_seconds = tool_timeout_seconds
        self.run_context = run_context
        # Retained for API compatibility only. Retrieval never performs a
        # hidden provider call: native tool arguments are executed verbatim.
        self.llm = llm
        self.definitions = build_tool_definitions()
        self.signature_counts: Dict[str, int] = {}
        self.call_log: List[ToolCallResult] = []
        self.mcp_registry: Any = None

    def attach_mcp(self, registry: Any) -> int:
        """Register AVAILABLE MCP tools from a probed McpRegistry."""
        if registry is None:
            return 0
        return int(registry.register_into(self) or 0)

    def _tool_event_meta(self, defn: ToolDefinition) -> Dict[str, Any]:
        """Contract fields for Trace (kind, origin, MCP, skill)."""
        meta: Dict[str, Any] = {
            "kind": defn.kind,
            "origin": defn.kind,
            "source": defn.kind,
            "executionBackend": defn.execution_backend,
            "toolName": defn.name,
        }
        if defn.mcp_server:
            meta["mcpServer"] = defn.mcp_server
        if defn.protocol_version or defn.kind == "mcp":
            meta["protocolVersion"] = defn.protocol_version or MCP_PROTOCOL_VERSION
        skill_id = defn.skill_id or self.run_context.get("skillId")
        skill_version = defn.skill_version or self.run_context.get("skillVersion")
        if skill_id:
            meta["skillId"] = skill_id
        if skill_version:
            meta["skillVersion"] = skill_version
        return meta

    def catalog_for(self, tool_names: List[str]) -> List[Dict[str, Any]]:
        catalog = []
        for name in tool_names:
            defn = self.definitions.get(name)
            if defn:
                entry = {
                    "name": defn.name,
                    "description": defn.description,
                    "inputSchema": defn.input_schema,
                    "kind": defn.kind,
                    "modelName": self.model_name(defn.name),
                }
                if defn.mcp_server:
                    entry["mcpServer"] = defn.mcp_server
                if defn.protocol_version:
                    entry["protocolVersion"] = defn.protocol_version
                catalog.append(entry)
        return catalog

    def catalog_for_agent(self, agent_id: str, tool_names: List[str]) -> List[Dict[str, Any]]:
        """Merge static agent tools with live MCP route (ReportAgent: no public MCP)."""
        names = list(tool_names or [])
        if agent_id == "ReportAgent":
            names = [n for n in names
                     if not (self.definitions.get(n)
                             and self.definitions[n].kind == "mcp")]
            return self.catalog_for(names)
        routed: set = set()
        if self.mcp_registry is not None:
            # Preserve the configured route order. Iterating a set randomized
            # native tool schema order between Python processes, invalidating
            # DeepSeek's exact-prefix cache after every deployment.
            routed_names = self.mcp_registry.tools_for_agent(agent_id)
            routed = set(routed_names)
            for extra in routed_names:
                if extra not in names:
                    names.append(extra)
        declared_repositories = _declared_github_repositories(self.run_context)
        context_signals = _tool_context_signals(self.run_context)
        filtered = []
        for name in names:
            defn = self.definitions.get(name)
            if defn is None:
                continue
            # A configured name or optimistic health flag is insufficient.
            # MCP entries reach the model only after live tools/list discovery
            # and only when the agent route explicitly includes them.
            if defn.kind == "mcp" and name not in routed:
                continue
            # DeepWiki is meaningful only for an explicitly declared public
            # repository. Hiding it here avoids teaching the model a tool that
            # runtime policy would necessarily reject for this candidate.
            if (defn.mcp_server == "deepwiki"
                    and not declared_repositories):
                continue
            # URL fetchers are candidate-bound evidence tools. Do not expose
            # them when no URL was declared in the resume/JD/request.
            if (name in {"fetch.fetch", "exa.web_fetch_exa", "exa.web_search_exa"}
                    and not context_signals["has_external_url"]):
                continue
            if (defn.mcp_server == "context7"
                    and not context_signals["framework_stack"]):
                continue
            filtered.append(name)
        names = filtered
        return self.catalog_for(names)

    @staticmethod
    def model_name(catalog_name: str) -> str:
        """Map an MCP catalog name to a provider-safe function identifier."""
        safe = re.sub(r"[^A-Za-z0-9_-]", "_", str(catalog_name or "tool"))
        if not safe:
            safe = "tool"
        if len(safe) > 55:
            suffix = hashlib.sha256(
                catalog_name.encode("utf-8")).hexdigest()[:8]
            safe = f"{safe[:46]}_{suffix}"
        return safe

    @staticmethod
    def openai_tools(catalog: List[Dict[str, Any]]) -> tuple[
            List[Dict[str, Any]], Dict[str, str]]:
        """Convert the live catalog to model-native function definitions."""
        tools: List[Dict[str, Any]] = []
        aliases: Dict[str, str] = {}
        for entry in catalog:
            catalog_name = str(entry.get("name") or "").strip()
            if not catalog_name:
                continue
            model_name = str(entry.get("modelName")
                             or ToolExecutor.model_name(catalog_name))
            # Deterministic collision handling without altering the MCP name
            # stored in trace/provenance.
            if model_name in aliases and aliases[model_name] != catalog_name:
                suffix = hashlib.sha256(
                    catalog_name.encode("utf-8")).hexdigest()[:8]
                model_name = f"{model_name[:46]}_{suffix}"
            aliases[model_name] = catalog_name
            schema = entry.get("inputSchema")
            if not isinstance(schema, dict):
                schema = {"type": "object"}
            tools.append({
                "type": "function",
                "function": {
                    "name": model_name,
                    "description": str(entry.get("description") or "")[:1024],
                    "parameters": schema,
                },
            })
            entry["modelName"] = model_name
        return tools, aliases

    @staticmethod
    def signature(tool: str, args: Dict[str, Any]) -> str:
        canonical = json.dumps(args or {}, sort_keys=True, ensure_ascii=False)[:2000]
        return f"{tool}:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()[:16]}"

    async def execute(self, agent_id: str, tool: str, args: Dict[str, Any],
                      enable_rewrite: bool = False,
                      tool_call_id: Optional[str] = None,
                      trace_context: Optional[Dict[str, Any]] = None
                      ) -> ToolCallResult:
        proposed_id = str(tool_call_id or f"tc-{uuid.uuid4().hex[:16]}")
        trace = dict(trace_context or {})
        defn = self.definitions.get(tool)
        if defn is None:
            call = self._reject(agent_id, tool, args, "TOOL_NOT_ALLOWED",
                                f"工具不在白名单中: {tool}",
                                tool_call_id=proposed_id)
            await self._emit_rejected(
                agent_id, tool, args, call, trace_context=trace)
            return call
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
            call = self._reject(agent_id, tool, args, "INPUT_SCHEMA", str(exc),
                                tool_call_id=proposed_id)
            await self._emit_rejected(
                agent_id, tool, args, call, defn=defn,
                trace_context=trace)
            return call
        if defn.kind == "mcp" and defn.mcp_server == "deepwiki":
            try:
                # Validate and canonicalize before trace/signature generation
                # so the recorded arguments equal the repository actually sent.
                self._deepwiki_context_policy(args)
            except ToolValidationError as exc:
                call = self._reject(
                    agent_id, tool, args, "SUBJECT_BINDING", str(exc),
                    tool_call_id=proposed_id)
                await self._emit_rejected(
                    agent_id, tool, args, call, defn=defn,
                    trace_context=trace)
                return call

        # Wire enable_rewrite AFTER schema validation (internal flag, not in tool schema).
        if enable_rewrite and tool in ("resume_semantic_search", "knowledge_search"):
            args["_rewrite"] = True

        signature = self.signature(tool, {k: v for k, v in args.items()
                                          if k != "_rewrite"})
        self.signature_counts[signature] = self.signature_counts.get(signature, 0) + 1

        tool_call_id = proposed_id
        self.budget.tool_calls += 1
        public_args = {k: v for k, v in args.items() if k != "_rewrite"}
        meta = self._tool_event_meta(defn)
        started_at = _utc_now()
        started_payload = {
            **trace,
            "toolCallId": tool_call_id,
            "arguments": _preview_args(public_args),
            "idempotencyKey": signature,
            "sideEffectLevel": defn.side_effect_level,
            "retryCount": 0,
            "rewriteEnabled": bool(args.get("_rewrite")),
            "lifecycleStage": "EXECUTION_STARTED",
            "occurredAt": started_at,
            "startedAt": started_at,
            **meta,
        }
        await self.emitter.emit("tool.started", agent_id=agent_id, tool_name=tool,
                                payload=started_payload)
        started = time.monotonic()

        # Deterministic tools: content-hash cache (30d) — repeat evaluations
        # of the same resume/JD skip builtin re-execution entirely.
        cache_key = None
        if tool in CACHEABLE_TOOLS:
            cache_key = cache.content_key("tool", tool, json.dumps(
                public_args, sort_keys=True, ensure_ascii=False)[:20000])
            cached = await cache.get_json(cache_key)
            if cached is not None:
                duration_ms = int((time.monotonic() - started) * 1000)
                call = ToolCallResult(tool_call_id, tool, "SUCCEEDED", cached,
                                      duration_ms=duration_ms)
                self.call_log.append(call)
                await self.emitter.emit("tool.completed", agent_id=agent_id,
                                        tool_name=tool, payload={
                                            **trace,
                                            "toolCallId": tool_call_id,
                                            "durationMs": duration_ms,
                                            "cacheHit": True,
                                            "lifecycleStage": "RESULT",
                                            "occurredAt": _utc_now(),
                                            "startedAt": started_at,
                                            "endedAt": _utc_now(),
                                            "arguments": _preview_args(public_args),
                                            "resultPreview": _preview(cached),
                                            **meta,
                                        })
                return call
        retries = 0
        max_retries = defn.max_retries if (defn.idempotent and defn.side_effect_level == "read_only") else 0
        last_error: Optional[str] = None
        while retries <= max_retries:
            try:
                timeout = min(defn.timeout_seconds, self.tool_timeout_seconds)
                raw = await asyncio.wait_for(
                    self._dispatch(defn, args, agent_id=agent_id), timeout=timeout)
                result = self._normalize_result(raw)
                try:
                    if isinstance(result, dict):
                        _validate(defn.output_schema, result, "output")
                except ToolValidationError as exc:
                    last_error = f"OUTPUT_SCHEMA: {exc}"
                    break
                duration_ms = int((time.monotonic() - started) * 1000)
                outcome = "SUCCEEDED"
                if isinstance(result, dict) and result.get("success") is False:
                    provider_status = str(
                        result.get("status") or "").strip().upper()
                    outcome = (
                        "UNAVAILABLE"
                        if provider_status in SOFT_UNAVAILABLE_STATUSES
                        else "FAILED")
                call = ToolCallResult(tool_call_id, tool, outcome, result,
                                      duration_ms=duration_ms, retries=retries)
                self.call_log.append(call)
                if cache_key is not None and outcome == "SUCCEEDED":
                    await cache.set_json(cache_key, result, cache.TTL_PARSE_RESUME)
                ended_at = _utc_now()
                event_type = (
                    "tool.failed" if outcome == "FAILED"
                    else "tool.completed")
                await self.emitter.emit(event_type, agent_id=agent_id, tool_name=tool,
                                        payload={
                                            **trace,
                                            "toolCallId": tool_call_id,
                                            "outcome": outcome,
                                            "lifecycleStage": (
                                                "RESULT" if outcome != "FAILED"
                                                else "ERROR"),
                                            "occurredAt": ended_at,
                                            "startedAt": started_at,
                                            "endedAt": ended_at,
                                            "durationMs": duration_ms,
                                            "retryCount": retries,
                                            "arguments": _preview_args(args),
                                            "resultPreview": _preview(result),
                                            **meta,
                                        })
                return call
            except asyncio.CancelledError:
                await self.emitter.emit("tool.failed", agent_id=agent_id, tool_name=tool,
                                        payload={"toolCallId": tool_call_id,
                                                 **trace,
                                                 "error": "cancelled",
                                                 "lifecycleStage": "ERROR",
                                                 "occurredAt": _utc_now(),
                                                 "startedAt": started_at,
                                                 "endedAt": _utc_now(),
                                                 **meta})
                raise
            except asyncio.TimeoutError:
                last_error = f"TIMEOUT after {defn.timeout_seconds}s"
            except Exception as exc:  # noqa: BLE001 - tool boundary
                last_error = f"{type(exc).__name__}: {exc}"
            retries += 1
            if retries <= max_retries:
                await self.emitter.emit("tool.progress", agent_id=agent_id, tool_name=tool,
                                        payload={"toolCallId": tool_call_id,
                                                 **trace,
                                                 "progress": f"retry {retries}",
                                                 "error": (last_error or "")[:200],
                                                 **meta})
                await asyncio.sleep(0.8 * retries)

        duration_ms = int((time.monotonic() - started) * 1000)
        call = ToolCallResult(tool_call_id, tool, "FAILED", None,
                              error=last_error, duration_ms=duration_ms, retries=retries)
        self.call_log.append(call)
        await self.emitter.emit("tool.failed", agent_id=agent_id, tool_name=tool, payload={
            **trace,
            "toolCallId": tool_call_id, "error": (last_error or "")[:300],
            "lifecycleStage": "ERROR",
            "occurredAt": _utc_now(),
            "startedAt": started_at,
            "endedAt": _utc_now(),
            "arguments": _preview_args(args),
            "durationMs": duration_ms, "retryCount": retries, **meta})
        return call

    def _deepwiki_context_policy(self, args: Dict[str, Any]) -> Dict[str, Any]:
        declared = _declared_github_repositories(self.run_context)
        requested = ""
        requested_key = ""
        for key, value in args.items():
            if str(key).strip().lower() in _DEEPWIKI_REPOSITORY_KEYS:
                requested_key = str(key)
                requested = _normalize_repository_slug(value)
                break
        if not requested:
            raise ToolValidationError(
                "DeepWiki requires a repository argument in owner/repo form")
        source_url = declared.get(requested)
        if not source_url:
            allowed = ", ".join(sorted(declared)) or "none"
            raise ToolValidationError(
                "DeepWiki repository was not explicitly declared by the "
                f"candidate/user: {requested}; declared=[{allowed}]")
        canonical_repository = source_url.split(
            "https://github.com/", 1)[-1].strip("/")
        # DeepWiki's repoName contract is owner/repo and preserves casing.
        # Bind the outbound argument to the candidate-declared canonical path,
        # rather than trusting the model-authored spelling or URL form.
        args[requested_key] = canonical_repository
        return {
            "evidenceUse": "context_only",
            "candidateFactEligible": False,
            "subjectBinding": "candidate_declared_repository",
            "repository": canonical_repository,
            "sourceUrl": source_url,
            "sourceUrls": [source_url],
            "contentNature": "ai_generated_wiki_context",
        }

    async def _dispatch(self, defn: ToolDefinition, args: Dict[str, Any],
                        agent_id: str = "") -> Any:
        rewrite = bool(args.pop("_rewrite", False))
        if defn.kind == "builtin":
            return await self.builtin_tools.invoke(defn.name, args)
        if defn.kind == "mcp":
            if agent_id == "ReportAgent":
                raise ToolValidationError(
                    "ReportAgent 不得直接调用公网 MCP，只消费已校准 evidence")
            if self.mcp_registry is not None:
                deepwiki_policy = (
                    self._deepwiki_context_policy(args)
                    if defn.mcp_server == "deepwiki" else None)
                result = await self.mcp_registry.call(defn.name, args)
                if deepwiki_policy is not None and isinstance(result, dict):
                    # Remote wiki prose is model-generated context, never a
                    # candidate fact. Overwrite any untrusted remote claim
                    # about provenance with the locally enforced binding.
                    result["evidencePolicy"] = deepwiki_policy
                return result
            raise ToolValidationError(f"no MCP dispatcher for tool {defn.name}")
        if defn.name in ("resume_semantic_search", "knowledge_search"):
            return await self._retrieve_with_rewrite(defn, args, rewrite)
        if defn.name == "jd_match_search":
            return await gateway.java_jd_search(
                resume_text=str(args.get("resumeText") or ""), top_k=3)
        if defn.name == "timeline_validator":
            return await gateway.timeline_validator(
                resume_text=str(args.get("resumeText") or ""))
        if defn.name == "external_profile_lookup":
            return await gateway.java_external_profile(
                resume_text=str(args.get("resumeText") or ""))
        raise ToolValidationError(f"no dispatcher for tool {defn.name}")

    # ------------------------------------------------------------------
    # Multi-stage retrieval: visible query -> recall -> fusion -> rerank.
    # ------------------------------------------------------------------

    async def _rewrite_queries(self, query: str) -> List[str]:
        """Budget-safe rewrite stage.

        The provider-native agent already selected the tool and authored the
        query. Returning it verbatim avoids an invisible second LLM call while
        keeping queryRewriteMs and later retrieval/fusion/rerank stages
        observable.
        """
        return [str(query or "").strip()]

    async def _retrieve_with_rewrite(self, defn: ToolDefinition,
                                     args: Dict[str, Any],
                                     rewrite: bool) -> Any:
        import time as _time
        query = str(args.get("query") or "")
        top_k = int(args.get("topK") or 5)
        _t0 = _time.perf_counter()
        queries = await self._rewrite_queries(query) if rewrite else [query]
        _rewrite_ms = (_time.perf_counter() - _t0) * 1000

        async def one(q: str, *, use_rerank: bool = False) -> Any:
            if defn.name == "resume_semantic_search":
                return await gateway.java_resume_search(
                    query=q, top_k=top_k,
                    resume_text=str(args.get("resumeText") or ""),
                    jd_requirements=str(self.run_context.get("jobDescription") or "")[:2000],
                    strategy="hybrid")
            return await gateway.java_knowledge_search(
                query=q, top_k=top_k, rerank=use_rerank)

        if len(queries) == 1:
            _t1 = _time.perf_counter()
            # Full evaluations use the backend's measured, millisecond-scale
            # second-stage reranker in the same request.  Previously rerank
            # only ran for Copilot rewrites and repeated the whole retrieval.
            result = await one(
                queries[0], use_rerank=(defn.name == "knowledge_search"))
            _embed_ms = (_time.perf_counter() - _t1) * 1000
            result = self._normalize_result(result)
            if isinstance(result, dict):
                result.setdefault("queriesUsed", queries)
                result["queryRewriteMode"] = (
                    "deterministic_passthrough" if rewrite
                    else "not_requested")
                backend_total = result.get("latencyMs")
                result["_latency"] = {
                    "rewrite_ms": round(_rewrite_ms, 1),
                    "retrieval_ms": result.get("retrievalMs")
                    if result.get("retrievalMs") is not None
                    else round(_embed_ms, 1),
                    "fusion_ms": result.get("fusionMs"),
                    "rerank_ms": result.get("rerankMs"),
                    "total_ms": round(
                        _rewrite_ms + (
                            float(backend_total)
                            if isinstance(backend_total, (int, float))
                            else _embed_ms), 1),
                }
                rerank_strategy = str(
                    result.get("rerankStrategy") or "").strip()
                if (not isinstance(result.get("rerankApplied"), bool)
                        and rerank_strategy):
                    result["rerankApplied"] = True
                    result.setdefault(
                        "rerankProvider", "overlap_density_v1")
                if (rewrite and defn.name == "knowledge_search"
                        and not result.get("rerankApplied")
                        and self._retrieval_low_confidence(result)):
                    _t2 = _time.perf_counter()
                    reranked = await one(queries[0], use_rerank=True)
                    _rerank_ms = (_time.perf_counter() - _t2) * 1000
                    reranked = self._normalize_result(reranked)
                    if isinstance(reranked, dict):
                        before_top = self._top_retrieval_score(result)
                        after_top = self._top_retrieval_score(reranked)
                        reranked.setdefault("queriesUsed", queries)
                        reranked["queryRewriteMode"] = (
                            "deterministic_passthrough")
                        reranked["agenticRerank"] = True
                        reranked["rerankProvider"] = "retrieval_backend"
                        if before_top is not None and after_top is not None:
                            reranked["rerankBeforeTopScore"] = before_top
                            reranked["rerankAfterTopScore"] = after_top
                            reranked["rerankLift"] = round(
                                after_top - before_top, 6)
                        reranked["_latency"] = {
                            "rewrite_ms": round(_rewrite_ms, 1),
                            "retrieval_ms": round(_embed_ms, 1),
                            "rerank_ms": round(_rerank_ms, 1),
                            "total_ms": round(_rewrite_ms + _embed_ms + _rerank_ms, 1),
                        }
                        return reranked
            return result

        _t1 = _time.perf_counter()
        raws = await asyncio.gather(*(one(q, use_rerank=False) for q in queries),
                                    return_exceptions=True)
        _embed_ms = (_time.perf_counter() - _t1) * 1000
        # RRF fusion across query variants, dedup by item identity.
        _t_fuse = _time.perf_counter()
        fused: Dict[str, Dict[str, Any]] = {}
        scores: Dict[str, float] = {}
        raw_candidate_count = 0
        key_fields = ("chunkId", "id", "docId", "jdId", "title", "content")
        for raw in raws:
            if isinstance(raw, Exception):
                continue
            parsed = self._normalize_result(raw)
            items = []
            if isinstance(parsed, dict):
                for field_name in ("chunks", "hits", "results", "items"):
                    if isinstance(parsed.get(field_name), list):
                        items = parsed[field_name]
                        break
            elif isinstance(parsed, list):
                items = parsed
            raw_candidate_count += len(items)
            for rank, item in enumerate(items):
                if not isinstance(item, dict):
                    continue
                identity = next((str(item[f]) for f in key_fields
                                 if item.get(f)), None)
                if identity is None:
                    identity = json.dumps(item, sort_keys=True,
                                          ensure_ascii=False)[:120]
                fused.setdefault(identity, item)
                scores[identity] = scores.get(identity, 0.0) + 1.0 / (60 + rank + 1)
        ranked = sorted(fused.items(), key=lambda kv: scores[kv[0]],
                        reverse=True)[:top_k]
        _fusion_ms = (_time.perf_counter() - _t_fuse) * 1000
        fused_result = {
            "success": True,
            "chunks": [
                {**item, "rrfScore": round(scores[identity], 8)}
                for identity, item in ranked
            ],
            "queriesUsed": queries,
            "queryRewriteMode": "deterministic_passthrough",
            "strategy": "multi_query_retrieval",
            "fusion": "rrf_multi_query",
            "candidateCount": raw_candidate_count,
            "deduplicatedCount": max(0, raw_candidate_count - len(fused)),
            "_latency": {
                "rewrite_ms": round(_rewrite_ms, 1),
                "retrieval_ms": round(_embed_ms, 1),
                "fusion_ms": round(_fusion_ms, 1),
                "rerank_ms": 0,
                "total_ms": round(_rewrite_ms + _embed_ms + _fusion_ms, 1),
            },
        }
        # After multi-query fusion, still-low confidence asks the retrieval
        # backend for its configured rerank stage; this runtime does not issue
        # another provider chat completion.
        if (rewrite and defn.name == "knowledge_search"
                and self._retrieval_low_confidence(fused_result)):
            _t2 = _time.perf_counter()
            reranked = await one(query, use_rerank=True)
            _rerank_ms = (_time.perf_counter() - _t2) * 1000
            reranked = self._normalize_result(reranked)
            if isinstance(reranked, dict):
                before_top = self._top_retrieval_score(fused_result)
                after_top = self._top_retrieval_score(reranked)
                reranked.setdefault("queriesUsed", queries)
                reranked["queryRewriteMode"] = "deterministic_passthrough"
                reranked["agenticRerank"] = True
                reranked["rerankProvider"] = "retrieval_backend"
                reranked["fusion"] = "rrf_multi_query+backend_rerank"
                if before_top is not None and after_top is not None:
                    reranked["rerankBeforeTopScore"] = before_top
                    reranked["rerankAfterTopScore"] = after_top
                    reranked["rerankLift"] = round(after_top - before_top, 6)
                reranked["_latency"] = {
                    "rewrite_ms": round(_rewrite_ms, 1),
                    "retrieval_ms": round(_embed_ms, 1),
                    "fusion_ms": round(_fusion_ms, 1),
                    "rerank_ms": round(_rerank_ms, 1),
                    "total_ms": round(_rewrite_ms + _embed_ms + _fusion_ms + _rerank_ms, 1),
                }
                return reranked
        return fused_result

    @staticmethod
    def _top_retrieval_score(result: Dict[str, Any]) -> Optional[float]:
        """Return the provider/fusion score of the first scored top result."""
        items: List[Any] = []
        for field_name in ("chunks", "hits", "results", "items"):
            if isinstance(result.get(field_name), list):
                items = result[field_name]
                break
        for item in items:
            if not isinstance(item, dict):
                continue
            for key in (
                    "finalScore", "rerankScore", "retrievalScore",
                    "vectorScore", "bm25Score", "similarity", "rrfScore",
                    "matchScore", "score"):
                value = item.get(key)
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    return float(value)
        return None

    @staticmethod
    def _retrieval_low_confidence(result: Dict[str, Any]) -> bool:
        """Heuristic for EXP-4 agentic second-round rerank trigger."""
        items = []
        for field_name in ("chunks", "hits", "results", "items"):
            if isinstance(result.get(field_name), list):
                items = result[field_name]
                break
        if len(items) < 2:
            return True
        top_score = None
        for item in items[:3]:
            if not isinstance(item, dict):
                continue
            for key in ("vectorScore", "bm25Score", "retrievalScore", "similarity", "rrfScore", "matchScore", "score"):
                if isinstance(item.get(key), (int, float)):
                    top_score = float(item[key])
                    break
            if top_score is not None:
                break
        if top_score is None:
            return len(items) < 3
        # Scores from hybrid/RRF are typically 0-1; treat weak tops as low confidence.
        return top_score < 0.35

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
                code: str, message: str, *,
                tool_call_id: Optional[str] = None) -> ToolCallResult:
        call = ToolCallResult(tool_call_id or f"tc-{uuid.uuid4().hex[:16]}",
                              tool, "REJECTED",
                              None, error=f"{code}: {message}")
        self.call_log.append(call)
        logger.info("tool rejected agent=%s tool=%s: %s", agent_id, tool, message)
        return call

    async def _emit_rejected(self, agent_id: str, tool: str,
                             args: Dict[str, Any], call: ToolCallResult,
                             *, defn: Optional[ToolDefinition] = None,
                             trace_context: Optional[Dict[str, Any]] = None
                             ) -> None:
        now = _utc_now()
        meta = self._tool_event_meta(defn) if defn is not None else {
            "kind": "unknown", "origin": "unknown", "source": "unknown",
            "toolName": tool,
        }
        await self.emitter.emit("tool.failed", agent_id=agent_id,
                                tool_name=tool, payload={
                                    **(trace_context or {}),
                                    "toolCallId": call.tool_call_id,
                                    "error": call.error,
                                    "outcome": "REJECTED",
                                    "lifecycleStage": "ERROR",
                                    "arguments": _preview_args(args),
                                    "occurredAt": now,
                                    "startedAt": now,
                                    "endedAt": now,
                                    **meta,
                                })

    def metrics(self) -> Dict[str, Any]:
        failed = sum(1 for c in self.call_log if c.status == "FAILED")
        return {
            "toolCalls": len(self.call_log),
            "toolFailures": failed,
            "duplicateSignatures": sum(1 for v in self.signature_counts.values() if v > 1),
        }

    # ---------- pause/resume snapshot ----------

    def ledger(self) -> List[Dict[str, Any]]:
        """Completed tool calls by id — the resume path uses this to never
        re-execute an already finished (potentially non-idempotent) call."""
        return [
            {
                "toolCallId": call.tool_call_id,
                "tool": call.tool,
                "status": call.status,
                "durationMs": call.duration_ms,
                "retries": call.retries,
            }
            for call in self.call_log
        ]

    def restore_ledger(self, entries: List[Dict[str, Any]]) -> None:
        for entry in entries or []:
            self.call_log.append(ToolCallResult(
                tool_call_id=str(entry.get("toolCallId") or ""),
                tool=str(entry.get("tool") or ""),
                status=str(entry.get("status") or "SUCCEEDED"),
                result=None,
                duration_ms=int(entry.get("durationMs") or 0),
                retries=int(entry.get("retries") or 0)))


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


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z")

