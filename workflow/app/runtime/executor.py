from __future__ import annotations

import asyncio
import copy
import hashlib
import inspect
import json
import logging
import re
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from app.runtime.agents import AgentDefinition, AgentRegistry, default_agent_registry
from app.runtime.context import ContextManager
from app.runtime.coordinator import Coordinator, FULL_EVAL_TYPES, TERMINAL_AGENTS
from app.runtime.events import RuntimeEmitter
from app.runtime.llm import (
    LlmError,
    LlmToolCall,
    LlmTurn,
    ResilientLlmClient,
    WorkflowRunExecutionController,
    extract_json_object,
    workflow_agent_execution,
)
from app.runtime.loop_guard import LoopGuard
from app.runtime.memory import (
    CONTROL_PLANE_ERROR_CODES,
    MemoryClient,
    SPECIALIST_TYPES,
    decisions_from_hits,
    filter_hits_for_consumer,
    memory_trace_entries,
)
from app.runtime.models import (
    AgentDecision,
    AgentOutput,
    AgentRunRequest,
    BudgetExceeded,
    PolicyBundle,
    RunBudget,
    RunPaused,
)
from app.runtime.prompts import default_prompt_manager
from app.runtime.retrieval import BusinessRagRetriever, RetrievalResult
from app.runtime.gateway import java_jd_focus
from app.runtime.builtin_tools import BuiltinToolRegistry
from app.runtime.skills import default_skill_manager
from app.runtime.state import SharedState
from app.runtime.tools import ToolExecutor

logger = logging.getLogger(__name__)

_SOURCE_URL = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z")


def _collect_source_urls(*values: Any, limit: int = 12) -> List[str]:
    """Collect bounded HTTP(S) provenance from nested MCP args/results."""
    found: List[str] = []
    seen = set()

    def visit(value: Any, depth: int = 0) -> None:
        if len(found) >= limit or depth > 6:
            return
        if isinstance(value, dict):
            for nested in value.values():
                visit(nested, depth + 1)
                if len(found) >= limit:
                    break
            return
        if isinstance(value, (list, tuple)):
            for nested in value:
                visit(nested, depth + 1)
                if len(found) >= limit:
                    break
            return
        if not isinstance(value, str):
            return
        for match in _SOURCE_URL.finditer(value[:20000]):
            url = match.group(0).rstrip(".,;:!?)]}")
            if url and url not in seen:
                seen.add(url)
                found.append(url)
                if len(found) >= limit:
                    break

    for item in values:
        visit(item)
        if len(found) >= limit:
            break
    return found


import os as _os


def _configured_eager_skill_ids() -> set[str]:
    """Return the explicitly configured Skill eager-loading experiment set.

    The empty default preserves provider-selected progressive disclosure.  The
    experiment changes disclosure timing only: normal signal-based selection
    still decides whether a Skill belongs to the current agent and resume.
    """
    raw = _os.getenv("SKILL_EAGER_IDS", "")
    return {
        item.strip()
        for item in raw.split(",")
        if item.strip()
    }

AGENT_OUTPUT_SCHEMA = """输出 JSON（不要输出其它内容）：
{
  "thought": "简要计划（一两句）",
  "output": {                                             // 完成本职责时给出，否则为 null
    "summary": "一句话结论",
    "claims": [{"section": "technical_findings|project_findings|risks|evidence|recommendations|resume_facts|jd_requirements",
                 "value": [...] 或 {...}}],
    "evidence": [{"text": "证据描述", "sourceLine": 行号或null, "source": "resume|jd|tool|memory", "verified": true/false/null}],
  },
  "done": true/false
}
工具调用必须使用模型原生 function/tool calls；禁止在 JSON 中嵌套 toolCalls。"""

REPORT_OUTPUT_SCHEMA = """输出 JSON（不要输出其它内容；精简表达）：
{
  "thought": "简要计划",
  "output": {
    "summary": "面试官视角的一句话结论",
    "report": {
      "recommendation": "HIRE|INTERVIEW_RECOMMEND|NEED_MANUAL_REVIEW|NOT_RECOMMEND",
      "dimensions": [{"name":"技术能力|项目深度|JD匹配|履历可信度","score":"0-100整数（依据证据合理评分）","status":"ASSESSED|PARTIAL|UNASSESSED","rationale":"判断理由","evidenceRefs":[{"sourceType":"RESUME","sourceId":"resume","quote":"原文≤30字"}]}],
      "strengths": ["有事实支撑的优势"],
      "risks": [{"id":"r1","category":"CANDIDATE","severity":"HIGH|MEDIUM|LOW","claim":"具体风险","verificationPlan":"面试核实方式"}],
      "interviewProbes": [{"id":"q1","priority":"HIGH|MEDIUM","question":"针对性问题","objective":"目的","triggeredBy":"由哪个项目/风险/JD缺口触发","goodSignals":["好信号"],"redFlags":["警示信号"]}],
      "dataQuality": "SUFFICIENT|PARTIAL|INSUFFICIENT",
      "missingEvidence": ["无法从简历判断的信息"]
    }
  },
  "done": true
}
禁止输出 overallScore（系统加权计算）。无证据维度 status=UNASSESSED score=null。
评分标准：60=基本合格，70=良好匹配，80+=优秀匹配。有证据支撑合理给分，不要全部压低。
risks 仅写候选人侧(category=CANDIDATE)；系统/数据问题放 systemWarnings。
interviewProbes 按去重后的待核验主题动态生成，必须覆盖每个HIGH风险、关键JD缺口和最重要项目；最多8题，超过预算按风险优先级截断，禁止为凑数量重复问题。"""

# Provider-side schema enforcement (JSON guarantee layer 1): the decision loop
# forces this function; DeepSeek then guarantees arguments match the schema.
# Layers 2-4 (json_object mode, extract_json_object, pydantic + repair) stay
# as fallbacks.
EMIT_DECISION_TOOL = {
    "type": "function",
    "function": {
        "name": "emit_decision",
        "description": "提交本轮 agent 决策（json）：思考、需要的工具调用、结构化输出。",
        "parameters": {
            "type": "object",
            "properties": {
                "thought": {"type": "string", "description": "简要计划"},
                "output": {
                    "type": "object",
                    "properties": {
                        "summary": {"type": "string"},
                        "claims": {
                            "type": "array", "maxItems": 12,
                            "items": {"type": "object"}},
                        "evidence": {
                            "type": "array", "maxItems": 12,
                            "items": {"type": "object"}},
                    },
                },
                "done": {"type": "boolean"},
            },
            "required": ["done"],
        },
    },
}

# ReportAgent: strong schema for structured report (Markdown is rendered offline).
_SOURCE_REF_SCHEMA = {
    "type": "object",
    "properties": {
        "sourceType": {
            "type": "string",
            "enum": ["RESUME", "JD", "KNOWLEDGE", "EXTERNAL"],
        },
        "sourceId": {"type": "string"},
        "lineStart": {"type": "integer"},
        "lineEnd": {"type": "integer"},
        "quote": {"type": "string"},
        "uri": {"type": "string"},
    },
    "required": ["sourceType", "sourceId", "quote"],
}
_REPORT_DIM = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "score": {"type": ["integer", "null"], "minimum": 0, "maximum": 100},
        "status": {
            "type": "string",
            "enum": ["ASSESSED", "UNASSESSED", "PARTIAL"],
        },
        "evidenceCoverage": {"type": "number", "minimum": 0, "maximum": 1},
        "rationale": {"type": "string"},
        "evidenceRefs": {
            "type": "array", "minItems": 1,
            "items": _SOURCE_REF_SCHEMA},
    },
    "required": ["name", "status", "rationale", "evidenceRefs"],
}
_CANDIDATE_RISK_SCHEMA = {
    "type": "object",
    "properties": {
        "id": {"type": "string"},
        "category": {"type": "string"},
        "severity": {"type": "string", "enum": ["HIGH", "MEDIUM", "LOW"]},
        "confidence": {"type": "number"},
        "claim": {"type": "string"},
        "impact": {"type": "string"},
        "evidenceRefs": {
            "type": "array", "minItems": 1,
            "items": _SOURCE_REF_SCHEMA},
        "verificationPlan": {"type": "string"},
    },
    "required": [
        "id", "severity", "claim", "evidenceRefs", "verificationPlan"],
}
_INTERVIEW_PROBE_SCHEMA = {
    "type": "object",
    "properties": {
        "id": {"type": "string"},
        "priority": {"type": "string"},
        "question": {"type": "string"},
        "objective": {"type": "string"},
        "triggeredBy": {"type": "string"},
        "evidenceRefs": {
            "type": "array", "minItems": 1,
            "items": _SOURCE_REF_SCHEMA},
        "goodSignals": {"type": "array", "items": {"type": "string"}},
        "redFlags": {"type": "array", "items": {"type": "string"}},
        "followUps": {"type": "array", "items": {"type": "string"}},
        "scoreRubric": {"type": "string"},
    },
    "required": [
        "id", "priority", "question", "objective", "triggeredBy",
        "evidenceRefs"],
}
_SYSTEM_WARNING_SCHEMA = {
    "type": "object",
    "properties": {
        "code": {"type": "string"},
        "stage": {"type": "string"},
        "retryable": {"type": "boolean"},
        "message": {"type": "string"},
    },
    "required": ["code", "message"],
}
EMIT_REPORT_TOOL = {
    "type": "function",
    "function": {
        "name": "emit_decision",
        "description": "提交 ReportAgent 结构化评估（仅 JSON，不含长 Markdown）。",
        "parameters": {
            "type": "object",
            "properties": {
                "thought": {"type": "string"},
                "output": {
                    "type": "object",
                    "properties": {
                        "summary": {"type": "string"},
                        "report": {
                            "type": "object",
                            "properties": {
                                "recommendation": {
                                    "type": "string",
                                    "enum": ["HIRE", "INTERVIEW_RECOMMEND",
                                             "NEED_MANUAL_REVIEW", "NOT_RECOMMEND"],
                                },
                                "dimensions": {"type": "array", "items": _REPORT_DIM},
                                "strengths": {"type": "array", "items": {"type": "string"}},
                                "risks": {
                                    "type": "array", "items": _CANDIDATE_RISK_SCHEMA},
                                "interviewProbes": {
                                    "type": "array", "items": _INTERVIEW_PROBE_SCHEMA},
                                "systemWarnings": {
                                    "type": "array", "items": _SYSTEM_WARNING_SCHEMA},
                                "dataQuality": {
                                    "type": "string",
                                    "enum": ["SUFFICIENT", "PARTIAL", "INSUFFICIENT"],
                                },
                                "missingEvidence": {
                                    "type": "array", "items": {"type": "string"}},
                            },
                            "required": ["recommendation", "dimensions", "strengths",
                                         "risks", "interviewProbes", "dataQuality"],
                        },
                    },
                    "required": ["summary", "report"],
                },
                "done": {"type": "boolean"},
            },
            "required": ["done", "output"],
        },
    },
}


def _parallel_report_section_specs() -> Dict[str, Dict[str, Any]]:
    """Focused report sections used by the latency-safe parallel synthesizer.

    Scoring stays on the quality model. Risk discovery and interview design are
    narrower extraction/generation tasks and use the fast model. The regular
    report validator remains the only authority after deterministic merging.
    """
    score_properties = {
        "summary": {"type": "string"},
        "recommendation": {
            "type": "string",
            "enum": ["HIRE", "INTERVIEW_RECOMMEND",
                     "NEED_MANUAL_REVIEW", "NOT_RECOMMEND"],
        },
        "dataQuality": {
            "type": "string",
            "enum": ["SUFFICIENT", "PARTIAL", "INSUFFICIENT"],
        },
        "dimensions": {
            "type": "array", "items": _REPORT_DIM,
            "minItems": 4, "maxItems": 4,
        },
        "strengths": {
            "type": "array", "items": {"type": "string"},
            "minItems": 2, "maxItems": 5,
        },
    }
    risk_properties = {
        "risks": {
            "type": "array", "items": _CANDIDATE_RISK_SCHEMA,
            "minItems": 4, "maxItems": 6,
        },
        "missingEvidence": {
            "type": "array", "items": {"type": "string"},
            "minItems": 4, "maxItems": 8,
        },
    }
    question_properties = {
        "interviewQuestions": {
            "type": "array", "items": _INTERVIEW_PROBE_SCHEMA,
            # Dynamic count is preserved (4-8); one generic question is not a
            # useful deep-evaluation report and was the only real quality drop
            # observed in the eager/live A/B.
            "minItems": 4, "maxItems": 8,
        },
    }
    return {
        "score": {
            "useQuality": True,
            "maxTokens": 2100,
            "properties": score_properties,
            "required": list(score_properties),
            "instruction": (
                "只生成评分总览小节：技术能力、项目深度、JD匹配、履历可信度"
                "四个维度必须齐全且逐项引用证据；给出150-250字summary、"
                "recommendation、dataQuality和至少2条strengths。"
                "不要生成风险和面试题。只调用一次emit_report_section，"
                "arguments闭合后禁止重复输出第二个JSON对象或解释。"),
        },
        "risk": {
            "useQuality": False,
            "maxTokens": 2200,
            "properties": risk_properties,
            "required": list(risk_properties),
            "instruction": (
                "只生成候选人风险小节：输出4-6条不重复的具体风险，覆盖"
                "履历可信度、项目真实性、JD缺口；每条给影响、核验方式和"
                "证据引用；另列4-8条missingEvidence。不要生成评分和面试题。"
                "只调用一次emit_report_section，arguments闭合后禁止重复输出"
                "第二个JSON对象或解释。"),
        },
        "question": {
            "useQuality": False,
            "maxTokens": 2800,
            "properties": question_properties,
            "required": list(question_properties),
            "instruction": (
                "只生成结构化面试追问：先从HIGH风险、关键JD缺口和最重要项目"
                "形成待核验主题，合并重复主题后每个主题生成一题；必须4-8题，"
                "超过预算按风险优先级截断，禁止为凑数重复问题。每题含目的、"
                "触发依据、好信号、"
                "红旗、1个追问和证据引用；好信号/红旗各1-2条，避免重复。"
                "不要生成评分和风险。只调用一次emit_report_section，"
                "arguments闭合后禁止重复输出第二个JSON对象或解释。"),
        },
    }


def _backfill_interview_questions_from_risks(
        report: Dict[str, Any], minimum: int = 4, *,
        allow_empty: bool = False) -> int:
    """Complete a short model question section from grounded risk outputs.

    Some compatible providers accept the native ``minItems`` schema but still
    return one interview question. Regenerating the whole report turns this
    small provider defect into a 100s+ tail. Preserve the authored question and
    deterministically turn distinct, evidence-bound risk verification plans
    into the missing probes. An empty/malformed question section is not
    backfilled and still follows the normal model retry path.
    """
    raw_questions = report.get("interviewQuestions")
    if not isinstance(raw_questions, list):
        raw_questions = report.get("interviewProbes")
    questions = [
        dict(item) for item in (raw_questions or [])
        if isinstance(item, dict) and str(item.get("question") or "").strip()
    ]
    if (not questions and not allow_empty) or len(questions) >= minimum:
        return 0

    risks = [
        item for item in (report.get("risks") or [])
        if isinstance(item, dict)
    ]
    used_ids = {str(item.get("id") or "") for item in questions}
    used_topics = {
        re.sub(r"\s+", "", str(
            item.get("triggeredBy") or item.get("question") or "")).lower()
        for item in questions
    }
    added = 0
    for risk_index, risk in enumerate(risks, start=1):
        if len(questions) >= minimum:
            break
        claim = str(risk.get("claim") or "").strip()
        verification = str(risk.get("verificationPlan") or "").strip()
        refs = [
            dict(ref) for ref in (risk.get("evidenceRefs") or [])
            if isinstance(ref, dict)
        ]
        topic = re.sub(r"\s+", "", claim).lower()
        if not claim or not refs or topic in used_topics:
            continue
        probe_id = f"risk-probe-{risk.get('id') or risk_index}"
        if probe_id in used_ids:
            continue
        severity = str(risk.get("severity") or "MEDIUM").upper()
        priority = severity if severity in {"HIGH", "MEDIUM", "LOW"} else "MEDIUM"
        focus = verification or claim
        questions.append({
            "id": probe_id[:60],
            "priority": priority,
            "question": (
                f"请结合具体项目、个人贡献和可复现证据说明：{focus}"
            )[:400],
            "objective": f"核验风险结论：{claim}"[:300],
            "triggeredBy": f"风险项：{claim}"[:200],
            "evidenceRefs": refs,
            "goodSignals": [
                "说明技术决策、个人贡献、验证步骤和量化口径",
                "能给出代码、监控、复盘或其他可交叉验证证据",
            ],
            "redFlags": [
                "只复述结论，无法说明数据来源或验证方法",
            ],
            "followUps": [
                "如果重新实施一次，你会如何验证结果并控制风险？",
            ],
            "scoreRubric": "按证据可验证性、个人贡献清晰度和技术取舍评分",
        })
        used_ids.add(probe_id)
        used_topics.add(topic)
        added += 1

    if added:
        report["interviewQuestions"] = questions
        report["interviewProbes"] = questions
    return added


def _salvage_first_complete_report_section(
        raw_arguments: str, required: List[str]) -> Optional[Dict[str, Any]]:
    """Keep a complete first section object before provider-added extra data.

    This recovery is intentionally scoped to report sections. The returned
    object still passes required-field checks and the normal structured report
    quality gate before publication; generic native tools remain strict.
    """
    text = str(raw_arguments or "")
    try:
        candidate, end = json.JSONDecoder().raw_decode(text)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    if (not isinstance(candidate, dict) or not text[end:].strip()
            or any(field not in candidate for field in required)):
        return None
    return dict(candidate)

# Dimension weights for deterministic overallScore (name normalized lowercase).
_DIMENSION_WEIGHTS = {
    "技术能力": 0.35,
    "项目深度": 0.25,
    "jd匹配": 0.25,
    "履历可信度": 0.15,
}
_CORE_DIMENSION_KEYS = {
    n.replace(" ", "").lower() for n in _DIMENSION_WEIGHTS
}
_MIN_EVIDENCE_COVERAGE = 0.2
_PROCESS_DATA_CATEGORIES = frozenset({"PROCESS", "DATA", "CONTROL_PLANE", "SYSTEM"})
MIN_REPORT_ANSWER_CHARS = 80
MIN_RESUME_TEXT_CHARS = 80
_SCORE_CONTRACT_SKIP_TYPES = {
    "followup", "quick_answer", "resume_optimize", "project_rewrite",
    "interview_questions",
}

# Explicit long-term preference statements we are allowed to persist.
_PREFERENCE_PATTERNS = [
    (re.compile(r"(以后|今后|之后|每次)(都)?(请|要|用|使用|输出|给我)(?P<pref>[^。！!？?]{2,40})"), "explicit_instruction"),
    (re.compile(r"我(更)?(偏好|喜欢|习惯|倾向)(?P<pref>[^。！!？?]{2,40})"), "stated_preference"),
    (re.compile(r"(目标|意向)(岗位|职位)(是|为)(?P<pref>[^。！!？?]{2,30})"), "target_job"),
]


class RunExecutor:
    """Executes one conversational run: Observe → Plan → Select Agents →
    Execute (parallel groups) → Tools → Update Shared State → Finish, with
    budgets, loop guard, layered memory, compaction and pause/resume."""

    def __init__(self, request: AgentRunRequest, emitter: RuntimeEmitter, *,
                 registry: Optional[AgentRegistry] = None,
                 memory: Optional[MemoryClient] = None,
                 builtin_tools: Optional[BuiltinToolRegistry] = None,
                 llm: Optional[ResilientLlmClient] = None,
                 pause_event: Optional[asyncio.Event] = None) -> None:
        self.request = request
        self.emitter = emitter
        self.registry = registry or default_agent_registry
        self.retriever = BusinessRagRetriever()
        self.policy = PolicyBundle.from_config(request.policyId, request.policyConfig)
        self.budget = RunBudget()
        global_llm_limit = max(0, int(self.policy.maxLlmCalls))
        terminal_reserve = min(
            max(0, int(self.policy.terminalLlmReserve)),
            max(0, global_llm_limit - 1))
        control_reserve = min(
            max(0, int(self.policy.controlPlaneLlmReserve)),
            max(0, global_llm_limit - terminal_reserve - 1))
        self.budget.configure_llm_budget(
            global_llm_limit,
            {"terminal": terminal_reserve, "control": control_reserve},
            scope_limits={"control": control_reserve})
        # Optional: only runs launched through the service carry a live pause
        # event (kept optional so sync test construction needs no event loop).
        self.pause_event = pause_event
        self.memory = memory or MemoryClient(request.runId, request.conversationId,
                                             request.userId)
        # Candidate evaluation uses in-process deterministic builtin tools.
        self.builtin_tools = builtin_tools or BuiltinToolRegistry()
        run_context = {
            "resumeText": request.resumeText or "",
            "jobDescription": request.jobDescription or "",
            "userMessage": request.userMessage or "",
            "recentMessages": list(request.recentMessages or []),
        }
        self.llm = llm or ResilientLlmClient(
            emitter, self.budget, self.policy.maxLlmCalls,
            self.policy.timeoutPolicy.llmTimeoutSeconds,
            max_cost_cny=self.policy.maxCostCny,
            max_total_tokens=self.policy.maxTotalTokens)
        self.workflow_execution = WorkflowRunExecutionController(emitter)
        # The tool executor holds an llm reference only for query rewriting
        # (agentic retrieval); it never drives its own decision loop.
        self.tools = ToolExecutor(
            emitter, self.budget, self.builtin_tools,
            max_tool_calls_run=self.policy.toolBudget.maxToolCallsPerRun,
            tool_timeout_seconds=self.policy.timeoutPolicy.toolTimeoutSeconds,
            run_context=run_context, llm=self.llm)
        self.context = ContextManager(self.policy.contextBudget, emitter,
                                      request.runId, request.conversationId)
        self.guard = LoopGuard()
        self.state = SharedState()
        self.skill_selections: Dict[str, List[Any]] = {}
        self.memory_hits: List[Dict[str, Any]] = []
        self.failure_hits: List[Dict[str, Any]] = []
        self.failure_notes: List[str] = []
        self.memory_traces: List[Dict[str, Any]] = []
        self.pending_memory_writes: List[Dict[str, Any]] = []
        self.final_answer: str = ""
        self.degraded_reasons: List[str] = []
        self.agent_counters: Dict[str, Dict[str, int]] = {}
        self.agent_timings: Dict[str, int] = {}
        self.report_agent_failed = False
        # plan-time budget allocation (agent -> {llmQuota, toolQuota});
        # empty means "no per-agent quota", only global limits apply.
        self.budget_plan: Dict[str, Dict[str, int]] = {}
        # set by _restore_snapshot when an approved plan needs regrouping
        self._regroup_needed = False
        self.plan_meta: Dict[str, Any] = {}
        self.revision_reuse: Dict[str, Any] = {}
        # conflict arbitration runs exactly once (ruling is final)
        self._arbitrated = False
        # populated by _restore_snapshot on resume
        self.plan: List[str] = []
        self.parallel_groups: List[List[str]] = []
        self.next_group_index = 0
        self.executed: List[str] = []
        # Attach probed MCP catalog (best-effort; empty catalog if probe fails).
        try:
            from app.runtime.mcp_registry import get_mcp_registry_sync, get_mcp_registry
            registry = get_mcp_registry_sync()
            if registry is not None:
                self.tools.attach_mcp(registry)
                self._mcp_attach_pending = bool(registry.needs_probe())
            else:
                # Lazy probe will run on first execute; schedule soft attach.
                self._mcp_attach_pending = True
        except Exception as exc:  # noqa: BLE001
            logger.info("MCP attach deferred: %s", exc)
            self._mcp_attach_pending = True

    # ------------------------------------------------------------------

    @staticmethod
    def _memory_retrieval_query(
            request: AgentRunRequest) -> Tuple[str, List[str]]:
        """Build a de-identified query for same-job business memories."""
        resume = request.resumeText or ""
        lines = [line.strip() for line in resume.splitlines() if line.strip()]
        cue_lines: List[str] = []
        cue_pattern = re.compile(
            r"(技能|技术|项目|经历|教育|公司|岗位|工程师|负责|实现|优化|"
            r"skill|project|experience|education|engineer|develop|build)",
            re.IGNORECASE)
        for line in lines:
            if not cue_lines or cue_pattern.search(line):
                value = line[:140]
                if value not in cue_lines:
                    cue_lines.append(value)
            if len(cue_lines) >= 4:
                break
        technical_tokens: List[str] = []
        for token in re.findall(
                r"[A-Za-z][A-Za-z0-9.+#_-]{1,30}", resume):
            normalized = token.lower()
            if normalized not in {value.lower() for value in technical_tokens}:
                technical_tokens.append(token)
            if len(technical_tokens) >= 18:
                break
        resume_cues = " | ".join(cue_lines)
        if technical_tokens:
            resume_cues = (
                f"{resume_cues} | 技术词={','.join(technical_tokens)}"
                if resume_cues else f"技术词={','.join(technical_tokens)}")
        parts = [
            ("run_type", request.runType or ""),
            ("job_category", (request.jobCategory or "")[:100]),
            ("job_description", (request.jobDescription or "")[:220]),
            ("resume_cues", resume_cues[:480]),
        ]
        query = "\n".join(
            text.strip() for _, text in parts if text and text.strip()
        ) or request.runType
        basis = [name for name, text in parts if text and text.strip()]
        return query, basis

    @staticmethod
    def _business_memory_matches_request(
            hit: Dict[str, Any], request: AgentRunRequest) -> bool:
        """Defense in depth: never inject a memory from another job/JD."""
        structured = hit.get("structuredContent") or hit.get("structured") or {}
        if not isinstance(structured, dict):
            return False
        current_category = (request.jobCategory or "").strip().upper()
        memory_category = str(structured.get("jobCategory") or "").strip().upper()
        if current_category and memory_category != current_category:
            return False
        normalized_jd = re.sub(
            r"\s+", " ", (request.jobDescription or "").strip()).lower()
        if normalized_jd and str(hit.get("type") or "") == "JOB_PROFILE":
            fingerprint = hashlib.sha256(
                normalized_jd.encode("utf-8")).hexdigest()[:20]
            if str(structured.get("jdFingerprint") or "") != fingerprint:
                return False
        return bool(current_category or normalized_jd)

    @staticmethod

    @staticmethod
    def _merge_memory_hits(
            recent_cases: List[Dict[str, Any]],
            job_profiles: List[Dict[str, Any]],
            legacy_unused: Optional[List[Dict[str, Any]]] = None,
            *,
            limit: int,
    ) -> List[Dict[str, Any]]:
        """Keep one job profile and at most two de-identified recent cases."""
        buckets = [list(job_profiles[:1]), list(recent_cases[:2])]
        merged: List[Dict[str, Any]] = []
        seen = set()

        def add(hit: Dict[str, Any]) -> bool:
            memory_id = str(hit.get("memoryId") or "")
            identity = memory_id or (
                str(hit.get("type") or ""),
                str(hit.get("source") or ""),
                str(hit.get("content") or ""),
            )
            if identity in seen:
                return False
            seen.add(identity)
            merged.append(hit)
            return True

        capacity = max(1, int(limit))
        # First pass: protect diversity whenever those classes really exist.
        for bucket in buckets:
            if bucket:
                add(bucket.pop(0))
            if len(merged) >= capacity:
                return merged

        # Second pass: quality-ranked fill across the remaining real hits.
        remaining = [hit for bucket in buckets for hit in bucket]
        remaining.sort(
            key=lambda hit: float(hit.get("score") or hit.get("finalScore") or 0.0),
            reverse=True)
        for hit in remaining:
            add(hit)
            if len(merged) >= capacity:
                break
        return merged

    async def execute(self) -> Dict[str, Any]:
        started = time.monotonic()
        try:
            if self._requires_score_contract():
                resume = (self.request.resumeText or "").strip()
                if len(resume) < MIN_RESUME_TEXT_CHARS:
                    return self._result(
                        "FAILED", "",
                        error_code="RESUME_TEXT_INSUFFICIENT",
                        error_message=(
                            f"简历文本过短（{len(resume)} 字），无法生成可靠评分与报告"))
            result = await asyncio.wait_for(
                self._execute_inner(),
                timeout=self.policy.timeoutPolicy.runTimeoutSeconds)
            return result
        except RunPaused as paused:
            return self._result("PAUSED", "", snapshot=paused.snapshot)
        except asyncio.TimeoutError:
            self.degraded_reasons.append("run_timeout")
            answer = self._degraded_answer("运行超时")
            return self._result("TIMED_OUT", answer, error_code="RUN_TIMEOUT",
                                error_message="运行超出策略时限")
        except asyncio.CancelledError:
            raise
        except BudgetExceeded as exc:
            self.degraded_reasons.append(f"budget:{exc.kind}")
            answer = self._degraded_answer(f"预算耗尽（{exc.kind}）")
            return self._result("PARTIAL_SUCCESS" if answer else "FAILED", answer,
                                error_code="BUDGET_EXCEEDED", error_message=str(exc))
        except LlmError as exc:
            return self._result("FAILED", "", error_code=exc.code,
                                error_message=str(exc))
        except Exception as exc:  # noqa: BLE001 - top-level run boundary
            logger.exception("run executor crashed run=%s", self.request.runId)
            return self._result("FAILED", "", error_code="RUNTIME_ERROR",
                                error_message=str(exc)[:800])
        finally:
            logger.info("run %s finished in %.1fs llm=%d tools=%d",
                        self.request.runId, time.monotonic() - started,
                        self.budget.llm_calls, self.budget.tool_calls)

    async def _execute_inner(self) -> Dict[str, Any]:
        request = self.request
        resumed = self._restore_snapshot(request.resumeSnapshot)

        if not resumed:
            await self.emitter.emit("run.progress", payload={
                "stage": "observe", "message": "加载记忆与上下文"})
            self.revision_reuse = self._reuse_previous_revision_artifacts()
            if self.revision_reuse:
                await self.emitter.emit("run.progress", payload={
                    "stage": "revision_reuse",
                    "message": (
                        f"revision #{request.revision} 复用 "
                        f"{len(self.revision_reuse['reusedArtifacts'])} 个旧产物，"
                        f"失效 {len(self.revision_reuse['invalidatedArtifacts'])} 个"),
                    **self.revision_reuse,
                })
            # Business memory is job-scoped, de-identified, and split into a
            # small recent-case layer plus one stable job profile.
            memory_query, memory_query_basis = self._memory_retrieval_query(
                request)
            recall_limit = self.policy.memoryRetrieval.topK
            recent_case_hits, job_profile_hits = (
                await asyncio.gather(
                    self.memory.search(
                        memory_query, types=["RECENT_CASE"],
                        top_k=min(2, recall_limit),
                        min_confidence=self.policy.memoryRetrieval.minConfidence,
                        consumer_agent="SpecialistAgent"),
                    self.memory.search(
                        memory_query, types=["JOB_PROFILE"], top_k=1,
                        min_confidence=self.policy.memoryRetrieval.minConfidence,
                        consumer_agent="SpecialistAgent"),
                ))
            recent_case_hits = [
                hit for hit in recent_case_hits
                if self._business_memory_matches_request(hit, request)]
            job_profile_hits = [
                hit for hit in job_profile_hits
                if self._business_memory_matches_request(hit, request)]
            self.memory_hits = self._merge_memory_hits(
                recent_case_hits, job_profile_hits,
                limit=self.policy.memoryRetrieval.topK)
            self.failure_hits = []
            self.failure_notes = []
            # Memory retrieval must be observable in the trace, not a black box.
            type_counts: Dict[str, int] = {}
            for hit in self.memory_hits:
                hit_type = str(hit.get("type") or "UNKNOWN")
                type_counts[hit_type] = type_counts.get(hit_type, 0) + 1
            observe_trace = memory_trace_entries(
                [{"used": True, "ignoredReason": None, **h} for h in self.memory_hits],
                [],
                "SpecialistAgent",
            )
            self.memory_traces.extend(observe_trace)
            await self.emitter.emit("run.progress", payload={
                "stage": "memory",
                "message": f"岗位业务记忆命中 {len(self.memory_hits)} 条",
                "memoryHits": len(self.memory_hits),
                "failureHits": 0,
                "queryBasis": memory_query_basis + ["same_job_business_memory"],
                "retrievedTypeCounts": {
                    "RECENT_CASE": len(recent_case_hits),
                    "JOB_PROFILE": len(job_profile_hits),
                },
                "memoryTypeCounts": type_counts,
                "memoryTrace": observe_trace[:12],
                "memoryTop": [
                    {"memoryId": h.get("memoryId"),
                     "type": str(h.get("type") or ""),
                     "scope": h.get("ownerScope") or h.get("scope"),
                     "source": h.get("source"),
                     "consumerAgent": "SpecialistAgent",
                     "used": True,
                     "ignoredReason": None,
                     "confidence": h.get("confidence"),
                     "content": str(h.get("content") or "")[:120]}
                    for h in self.memory_hits[:3]
                ]})

            if getattr(self, "_mcp_attach_pending", False):
                try:
                    from app.runtime.mcp_registry import get_mcp_registry
                    registry = await get_mcp_registry(probe=True)
                    self.tools.attach_mcp(registry)
                except Exception as exc:  # noqa: BLE001
                    logger.info("MCP probe skipped: %s", exc)
                self._mcp_attach_pending = False

            # Deterministic preflight: parse resume / load JD into the
            # canonical artifact store BEFORE artifact backward-chaining.
            await self._prepare_context()
            coordinator = Coordinator(self.registry, self.policy, self.llm)
            arts = self.state.artifacts()
            needs_parse = bool(request.resumeText) and not arts.get("resumeFacts") \
                and request.runType in ("full_evaluation", "jd_evaluation",
                                        "backend_eval", "agent_eval",
                                        "resume_optimize", "project_rewrite")
            business_memories = [
                h for h in self.memory_hits
                if isinstance(h, dict)
                and str(h.get("type") or "") in {"RECENT_CASE", "JOB_PROFILE"}
            ]
            async with workflow_agent_execution(self.workflow_execution):
                planned = await coordinator.plan(
                    run_type=request.runType, user_message=request.userMessage,
                    conversation_summary=request.conversationSummary or "",
                    shared_digest=self.state.view_for(
                        "CoordinatorAgent", max_chars=2000),
                    failure_notes=self.failure_notes,
                    memory_notes=business_memories or [
                        str(h.get("content", ""))[:120]
                        for h in self.memory_hits[:3]],
                    needs_parse=needs_parse,
                    resume_text=request.resumeText or "",
                    job_description=request.jobDescription or "",
                    artifacts=arts,
                    shared=self.state.data)
            self.plan = planned["plan"]
            self.parallel_groups = planned["parallelGroups"]
            self.budget_plan = planned.get("budgetPlan") or planned.get("budget") or {}
            self.plan_meta = {
                "selectedBecause": planned.get("selectedBecause") or {},
                "skippedBecause": planned.get("skippedBecause") or {},
                "artifactEdges": planned.get("artifactEdges") or [],
                "goalArtifacts": planned.get("goalArtifacts") or [],
                "optionalArtifacts": planned.get("optionalArtifacts") or [],
                "presentArtifacts": planned.get("presentArtifacts") or [],
                "revisionReuse": dict(self.revision_reuse),
                "budget": self.budget_plan,
            }
            if request.runType in FULL_EVAL_TYPES:
                # Full evaluations use the deterministic, signal-driven
                # artifact planner above.  Keeping four unused control-plane
                # calls reserved stranded late specialists behind capacity
                # that this run type can never consume.
                self.budget.release_llm_reservation("control")
            await self.emitter.emit("agent.selected", agent_id="CoordinatorAgent", payload={
                "plan": self.plan, "reason": planned["reason"],
                "parallelGroups": self.parallel_groups,
                "requiredTerminalAgent": planned["requiredTerminalAgent"],
                "policyId": self.policy.policyId,
                "budgetPlan": self.budget_plan,
                "budget": self.budget_plan,
                "selectedBecause": self.plan_meta["selectedBecause"],
                "skippedBecause": self.plan_meta["skippedBecause"],
                "artifactEdges": self.plan_meta["artifactEdges"],
                "goalArtifacts": self.plan_meta["goalArtifacts"],
                "presentArtifacts": self.plan_meta["presentArtifacts"],
                "revisionReuse": dict(self.revision_reuse),
                "llmBudget": self.budget.llm_audit(
                    self.policy.maxLlmCalls),
                "planMode": request.planMode,
                "memoryHits": len(self.memory_hits),
                "memoryNotes": [str(h.get("content", ""))[:120]
                                for h in self.memory_hits[:3]]})
            self.state.set_pending(list(self.plan))
            if request.planMode:
                # Plan-approval gate: pause before any specialist burns budget.
                # RESUME carries the (possibly user-edited) plan back in.
                snapshot = self.export_snapshot()
                snapshot["pauseReason"] = "AWAITING_PLAN_APPROVAL"
                raise RunPaused(snapshot)
        else:
            coordinator = Coordinator(self.registry, self.policy, self.llm)
            if self._regroup_needed:
                # Plan approval edited the pipeline: recompute dependency
                # ordering, parallel groups and budget from the approved plan.
                refreshed = coordinator._finalize(self.plan, "user_approved_plan")
                self.plan = refreshed["plan"]
                self.parallel_groups = refreshed["parallelGroups"]
                self.budget_plan = refreshed.get("budgetPlan") or {}
                self.next_group_index = 0
                await self.emitter.emit("agent.selected", agent_id="CoordinatorAgent", payload={
                    "plan": self.plan, "reason": "用户确认/编辑后的计划",
                    "parallelGroups": self.parallel_groups,
                    "requiredTerminalAgent": refreshed["requiredTerminalAgent"],
                    "policyId": self.policy.policyId,
                    "budgetPlan": self.budget_plan,
                    "approved": True})
                self.state.set_pending(list(self.plan))
            await self.emitter.emit("run.progress", payload={
                "stage": "resume",
                "message": f"从快照恢复：已完成 {len(self.executed)} 个 Agent",
                "executedAgents": self.executed})

        if any(agent in self.plan for agent in ("TechAgent", "ProjectAgent")):
            await self._prepare_jd_focus()

        consecutive_failures = 0
        while self.next_group_index < len(self.parallel_groups):
            self._pause_boundary()
            group = [a for a in self.parallel_groups[self.next_group_index]
                     if a not in self.executed]
            self.next_group_index += 1
            if not group:
                continue
            if len(self.executed) >= self.policy.maxAgentCount \
                    and not any(a in TERMINAL_AGENTS for a in group):
                self.degraded_reasons.append("max_agent_count")
                continue

            runnable: List[AgentDefinition] = []
            for agent_id in group:
                guard = self.guard.check_agent_start(agent_id)
                if guard.triggered:
                    await self._emit_guard(guard, agent_id)
                    continue
                try:
                    runnable.append(self.registry.get(agent_id))
                except KeyError:
                    continue
            if not runnable:
                continue

            if len(runnable) == 1:
                ok = await self._run_single(runnable[0], coordinator)
                consecutive_failures = 0 if ok else consecutive_failures + 1
            else:
                ok = await self._run_parallel(runnable)
                consecutive_failures = 0 if ok else consecutive_failures + 1

            if consecutive_failures >= 2:
                self.degraded_reasons.append("consecutive_failures")
                self._ensure_terminal_tail()

            # Debate-style conflict arbitration (single round, final): after
            # EvidenceAgent flagged conflicts, one LLM call adjudicates each
            # claim as keep/reject/uncertain — no ping-pong re-litigation.
            if any(d.agent_id == "EvidenceAgent" for d in runnable):
                await self._arbitrate_conflicts()

            # Group-boundary checkpoint: persist before advancing so the
            # per-run emitter connection can be closed without racing a
            # detached HTTP task, and a later retry never loses this boundary.
            await self.emitter.save_checkpoint(self.export_snapshot())

        if not self.final_answer:
            self.degraded_reasons.append("no_terminal_answer")
            self.final_answer = self._degraded_answer("报告 Agent 未能完成")
            self.report_agent_failed = True
        summary = self._conversation_summary()
        await self._write_memories(summary)
        missing_goals = self._missing_required_goal_artifacts()
        status = "PARTIAL_SUCCESS" if (self.report_agent_failed or
                                       self._has_hard_degradation() or
                                       missing_goals) else "SUCCEEDED"
        error_code = None
        error_message = None
        if missing_goals:
            self.degraded_reasons.append("missing_goal_artifacts")
            error_code = "MISSING_GOAL_ARTIFACTS"
            error_message = "缺少必选 goal artifacts: " + ", ".join(missing_goals)
        if status == "SUCCEEDED" and self._requires_score_contract():
            contract_error = self._report_contract_violation()
            if contract_error:
                status = "PARTIAL_SUCCESS"
                error_code = "REPORT_CONTRACT_FAILED"
                error_message = contract_error
                self.degraded_reasons.append("report_contract_failed")
        return self._result(status, self.final_answer,
                            error_code=error_code, error_message=error_message,
                            conversation_summary=summary,
                            missing_goal_artifacts=missing_goals)

    # ------------------------------------------------------------------
    # group execution
    # ------------------------------------------------------------------

    async def _run_single(self, definition: AgentDefinition,
                          coordinator: Coordinator) -> bool:
        agent_id = definition.agent_id
        await self.emitter.emit("agent.started", agent_id=agent_id, payload={
            "description": definition.description,
            "position": len(self.executed) + 1, "planned": len(self.plan)})
        agent_started = time.monotonic()
        try:
            output = await asyncio.wait_for(
                self._run_agent(definition), timeout=definition.timeout_seconds)
            conflicts = self.state.apply_output(output)
            self._after_agent_success(definition, output, conflicts, agent_started)
            return True
        except asyncio.CancelledError:
            raise
        except BudgetExceeded as exc:
            if (definition.agent_id not in TERMINAL_AGENTS
                    and exc.kind in {"llmReservation", "llmScopeLimit"}):
                await self._after_agent_failure(
                    definition, exc, agent_started)
                return False
            raise
        except Exception as exc:  # noqa: BLE001 - agent failure boundary
            await self._after_agent_failure(definition, exc, agent_started)
            return False

    async def _run_parallel(self, definitions: List[AgentDefinition]) -> bool:
        """Run independent specialists concurrently, then merge in plan order.

        The Coordinator only puts dependency-free agents in the same group.
        Keeping the merge ordered makes state/conflict resolution deterministic
        while avoiding the previous false parallelism where the trace advertised
        a group but every provider call waited for the preceding specialist.
        """
        async def invoke(
                definition: AgentDefinition,
        ) -> Tuple[AgentDefinition, float, Optional[AgentOutput], Optional[Exception]]:
            agent_start = time.monotonic()
            try:
                output = await asyncio.wait_for(
                    self._run_agent(definition),
                    timeout=definition.timeout_seconds)
                return definition, agent_start, output, None
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - merged below in plan order
                return definition, agent_start, None, exc

        base_position = len(self.executed)
        for offset, definition in enumerate(definitions):
            await self.emitter.emit("agent.started", agent_id=definition.agent_id,
                                    payload={"description": definition.description,
                                             "parallelGroup": [d.agent_id for d in definitions],
                                             "position": base_position + offset + 1,
                                             "planned": len(self.plan)})

        results = await asyncio.gather(
            *(invoke(definition) for definition in definitions))

        any_success = False
        for definition, agent_start, output, exc in results:
            if exc is None and isinstance(output, AgentOutput):
                conflicts = self.state.apply_output(output)
                self._after_agent_success(definition, output, conflicts,
                                          agent_start, fire_started=False)
                any_success = True
                continue
            if isinstance(exc, BudgetExceeded):
                if (definition.agent_id not in TERMINAL_AGENTS
                        and exc.kind in {
                            "llmReservation", "llmScopeLimit"}):
                    await self._after_agent_failure(
                        definition, exc, agent_start)
                    continue
                raise exc
            if isinstance(exc, Exception):
                await self._after_agent_failure(definition, exc, agent_start)
        return any_success

    def _after_agent_success(self, definition: AgentDefinition, output: AgentOutput,
                             conflicts: List[str], agent_started: float,
                             fire_started: bool = True) -> None:
        agent_id = definition.agent_id
        self.state.complete_task(agent_id)
        self.guard.record_completed_agent(agent_id)
        self.executed.append(agent_id)
        duration_ms = int((time.monotonic() - agent_started) * 1000)
        self.agent_timings[agent_id] = self.agent_timings.get(agent_id, 0) + duration_ms
        counters = self.agent_counters.get(agent_id, {})
        asyncio.ensure_future(self.emitter.emit(
            "agent.completed", agent_id=agent_id, payload={
                "iterations": counters.get("iterations", 1),
                "llmCalls": counters.get("llmCalls", 0),
                "toolCalls": counters.get("toolCalls", 0),
                "summary": output.summary[:300],
                "conflicts": conflicts,
                "durationMs": duration_ms,
                "output": {"type": output.type, "claims": len(output.claims),
                           "evidence": len(output.evidence)}}))
    async def _after_agent_failure(self, definition: AgentDefinition, exc: Exception,
                                   agent_started: float) -> None:
        agent_id = definition.agent_id
        error_text = f"{type(exc).__name__}: {exc}"
        self.failure_notes.append(f"{agent_id} 失败 {error_text[:120]}")
        duration_ms = int((time.monotonic() - agent_started) * 1000)
        self.agent_timings[agent_id] = self.agent_timings.get(agent_id, 0) + duration_ms
        await self.emitter.emit("agent.failed", agent_id=agent_id, payload={
            "error": error_text[:300], "durationMs": duration_ms})
        self.guard.check_error(error_text)
        self.degraded_reasons.append(f"{agent_id}_failed")
        self.executed.append(agent_id)
        if agent_id in TERMINAL_AGENTS:
            self.report_agent_failed = True
            return
        self._ensure_terminal_tail()

    def _ensure_terminal_tail(self) -> None:
        if self.report_agent_failed:
            return
        remaining = [a for g in self.parallel_groups[self.next_group_index:] for a in g]
        if not any(a in TERMINAL_AGENTS for a in remaining):
            self.parallel_groups.append(["ReportAgent"])
            if "ReportAgent" not in self.plan:
                self.plan.append("ReportAgent")

    async def _arbitrate_conflicts(self) -> None:
        """One-round conflict adjudication: unresolved conflicts get a
        keep / reject / uncertain ruling with a reason. The ruling is final
        (written back onto the conflict), the ReportAgent cites it."""
        conflicts = [c for c in (self.state.artifact("conflicts") or [])
                     if isinstance(c, dict) and not c.get("resolution")]
        if not conflicts or self._arbitrated:
            return
        self._arbitrated = True
        if self.budget.available_llm_calls_for_scope(
                self.policy.maxLlmCalls, "control") <= 0:
            # Full evaluation releases the unused control-plane reservation
            # after deterministic planning. Do not emit a guaranteed red
            # llm.failed node for best-effort arbitration; conservatively mark
            # unresolved conflicts as uncertain for ReportAgent disclosure.
            for conflict in conflicts:
                conflict["resolution"] = "uncertain"
                conflict["resolutionReason"] = (
                    "证据不足，保留为面试核验项")
            await self.emitter.emit("run.progress", payload={
                "stage": "arbitration",
                "mode": "deterministic_no_control_budget",
                "message": f"冲突保守标记：{len(conflicts)} 条待面试核验",
                "resolved": len(conflicts),
                "total": len(conflicts),
            })
            return
        items = [{"claim": str(c.get("claim", c.get("key", "")))[:200],
                  "reason": str(c.get("reason", c.get("type", "")))[:200]}
                 for c in conflicts[:6]]
        prompt_user = (
            "以下是评估过程中被证据核验标记的冲突结论。请逐条裁决，输出 json：\n"
            "{\"rulings\": [{\"claim\": \"...\", \"verdict\": \"keep|reject|uncertain\","
            " \"reason\": \"一句依据\"}]}\n"
            "裁决标准：简历原文/工具结果能支撑=keep；明确矛盾或无来源=reject；"
            "证据不足以定夺=uncertain（报告中如实标注）。\n"
            f"冲突列表: {json.dumps(items, ensure_ascii=False)}")
        try:
            async with workflow_agent_execution(self.workflow_execution):
                raw = await self.llm.chat(
                    [{"role": "system",
                      "content": "你是评估冲突仲裁者，只依据给定材料裁决，不新增事实。"},
                     {"role": "user", "content": prompt_user}],
                    agent_id="EvidenceAgent", purpose="arbitration",
                    max_tokens=600)
            parsed = extract_json_object(raw)
            rulings = {str(r.get("claim", ""))[:200]: r
                       for r in parsed.get("rulings", []) if isinstance(r, dict)}
            resolved = 0
            for conflict in conflicts:
                key = str(conflict.get("claim", conflict.get("key", "")))[:200]
                ruling = rulings.get(key)
                if ruling and ruling.get("verdict") in ("keep", "reject", "uncertain"):
                    conflict["resolution"] = ruling["verdict"]
                    conflict["resolutionReason"] = str(ruling.get("reason", ""))[:200]
                    resolved += 1
            await self.emitter.emit("run.progress", payload={
                "stage": "arbitration",
                "message": f"冲突仲裁完成：{resolved}/{len(conflicts)} 条已裁决",
                "resolved": resolved, "total": len(conflicts)})
        except Exception as exc:  # noqa: BLE001 - arbitration is best-effort
            logger.info("conflict arbitration skipped: %s", exc)

    def _has_hard_degradation(self) -> bool:
        hard = {"run_timeout", "consecutive_failures", "no_terminal_answer",
                "report_contract_failed"}
        terminal_fail = {f"{a}_failed" for a in TERMINAL_AGENTS}
        for r in self.degraded_reasons:
            if r in hard or r in terminal_fail or r.endswith("_failed"):
                return True
        return False

    def _missing_required_goal_artifacts(self) -> List[str]:
        """Run-end closure: required goal artifacts absent → never silent SUCCEEDED.
        
        An artifact is NOT considered missing if its designated producer Agent
        was executed (even if output was empty) — this avoids false PARTIAL_SUCCESS
        when e.g. EvidenceAgent fast-path finds no conflicts."""
        goals = list((self.plan_meta or {}).get("goalArtifacts") or [])
        if not goals:
            return []
        present = Coordinator._present_artifacts(
            self.state.artifacts() if hasattr(self.state, "artifacts") else
            self.state.data.get("artifacts") or {},
            self.state.data if isinstance(self.state.data, dict) else {})
        # Agents that ran count their artifacts as "attempted" even if empty.
        _AGENT_PRODUCES = {
            "ProjectAgent": {"project_findings"},
            "EvidenceAgent": {"evidence_ledger"},
            "TechAgent": {"technical_findings"},
            "RiskAgent": {"risks"},
            "ReportAgent": {"final_report"},
        }
        attempted = set()
        for agent_id in self.executed:
            attempted.update(_AGENT_PRODUCES.get(agent_id, set()))
        return [g for g in goals if g not in present and g not in attempted]

    def _requires_score_contract(self) -> bool:
        return self.request.runType not in _SCORE_CONTRACT_SKIP_TYPES
    def _report_contract_violation(self) -> Optional[str]:
        """SUCCEEDED requires non-empty structuredReport with meaningful content
        and a rendered answer that meets the minimum length contract."""
        report = self.state.data.get("artifacts", {}).get("finalReport")
        if not isinstance(report, dict) or not report:
            return "structuredReport 为空"
        quality = str(report.get("dataQuality") or "").upper()
        # If overallScore is missing but we have dimensions with scores,
        # compute a fallback average so the report isn't rejected.
        if not isinstance(report.get("overallScore"), int):
            if quality == "INSUFFICIENT":
                report.pop("overallScore", None)
                self.final_answer = self.render_report(report)
            else:
                dims = report.get("dimensions") or []
                scored = [d for d in dims if isinstance(d, dict)
                          and isinstance(d.get("score"), int)]
                if scored:
                    avg = int(round(
                        sum(d["score"] for d in scored) / len(scored)))
                    report["overallScore"] = avg
                    self.final_answer = self.render_report(report)
                else:
                    return "overallScore 为空且无可用维度分数"
        if len(self.final_answer or "") < MIN_REPORT_ANSWER_CHARS:
            return f"报告正文过短（<{MIN_REPORT_ANSWER_CHARS} 字）"
        return None

    # ------------------------------------------------------------------
    # pause / resume
    # ------------------------------------------------------------------

    def _reuse_previous_revision_artifacts(self) -> Dict[str, Any]:
        """Import only non-invalidated artifacts into a fresh revision.

        This is intentionally not checkpoint resume: budgets, executed agents,
        tool ledgers, and loop-guard state always start clean.
        """
        request = self.request
        snapshot = request.previousSnapshot or {}
        shared = snapshot.get("sharedState") if isinstance(snapshot, dict) else {}
        if not isinstance(shared, dict):
            shared = {}
        previous = shared.get("artifacts")
        if not isinstance(previous, dict) and isinstance(snapshot, dict):
            previous = snapshot.get("artifacts")
        previous = dict(previous) if isinstance(previous, dict) else {}
        if request.previousArtifacts:
            previous.update(request.previousArtifacts)
        if not previous:
            return {}

        reusable, expanded = Coordinator.reusable_artifacts(
            copy.deepcopy(previous), request.invalidatedArtifacts)
        if reusable:
            self.state.apply_artifacts(reusable, by_agent="previous_revision")
        return {
            "sourceRevision": max(1, int(request.revision or 1) - 1),
            "targetRevision": int(request.revision or 1),
            "requestedInvalidations": list(request.invalidatedArtifacts),
            "invalidatedArtifacts": sorted(expanded),
            "reusedArtifacts": sorted(reusable.keys()),
        }

    def _pause_boundary(self) -> None:
        """Safe pause point between agent groups: everything committed so far
        is durable in the snapshot; nothing mid-flight is frozen."""
        if self.pause_event is not None and self.pause_event.is_set():
            raise RunPaused(self.export_snapshot())

    def export_snapshot(self) -> Dict[str, Any]:
        return {
            "runId": self.request.runId,
            "plan": list(self.plan),
            "parallelGroups": [list(g) for g in self.parallel_groups],
            "budgetPlan": dict(self.budget_plan),
            "nextPlanIndex": self.next_group_index,
            "executedAgents": list(self.executed),
            "sharedState": self.state.snapshot(),
            "budget": self.budget.snapshot(),
            "loopGuardState": self.guard.export_state(),
            "contextSummary": self.request.conversationSummary or "",
            "recentMessages": self.request.recentMessages[-8:],
            "toolCallLedger": self.tools.ledger(),
            "promptVersions": default_prompt_manager.versions_used(
                list(dict.fromkeys(self.executed + ["CoordinatorAgent"])),
                self.policy.promptVersions),
            "skillVersions": default_skill_manager.versions_used(self.skill_selections),
            "policyId": self.policy.policyId,
            "planMeta": dict(self.plan_meta or {}),
            "revisionReuse": dict(self.revision_reuse),
            "finalAnswer": self.final_answer,
            "degradedReasons": list(self.degraded_reasons),
            "agentTimings": dict(self.agent_timings),
            "failureNotes": list(self.failure_notes)[-5:],
            "memoryHits": list(self.memory_hits)[:20],
            "failureHits": list(self.failure_hits)[:10],
            "memoryTraces": list(self.memory_traces)[-40:],
            "arbitrated": self._arbitrated,
            "reportAgentFailed": self.report_agent_failed,
            "agentCounters": dict(self.agent_counters),
            "createdAt": time.time(),
        }

    def _restore_snapshot(self, snapshot: Optional[Dict[str, Any]]) -> bool:
        if not snapshot:
            return False
        try:
            self.plan = [str(a) for a in snapshot.get("plan", [])]
            # A missing parallelGroups means the plan was edited during
            # approval: groups/budget must be recomputed from the new plan.
            self._regroup_needed = "parallelGroups" not in snapshot
            self.parallel_groups = [
                [str(a) for a in group]
                for group in snapshot.get("parallelGroups", [[a] for a in self.plan])]
            budget_plan = snapshot.get("budgetPlan")
            if isinstance(budget_plan, dict):
                self.budget_plan = {str(k): dict(v) for k, v in budget_plan.items()
                                    if isinstance(v, dict)}
            self.next_group_index = int(snapshot.get("nextPlanIndex", 0))
            self.executed = [str(a) for a in snapshot.get("executedAgents", [])]
            self.state.restore(snapshot.get("sharedState") or {})
            self.budget.restore(snapshot.get("budget") or {})
            self.guard.restore_state(snapshot.get("loopGuardState") or {})
            self.tools.restore_ledger(snapshot.get("toolCallLedger") or [])
            self.final_answer = str(snapshot.get("finalAnswer") or "")
            self.degraded_reasons = list(snapshot.get("degradedReasons") or [])
            self.agent_timings = dict(snapshot.get("agentTimings") or {})
            self.failure_notes = list(snapshot.get("failureNotes") or [])
            self.memory_hits = list(snapshot.get("memoryHits") or [])
            self.failure_hits = list(snapshot.get("failureHits") or [])
            self.memory_traces = list(snapshot.get("memoryTraces") or [])
            self._arbitrated = bool(snapshot.get("arbitrated", False))
            self.report_agent_failed = bool(
                snapshot.get("reportAgentFailed", False))
            counters = snapshot.get("agentCounters")
            if isinstance(counters, dict):
                self.agent_counters = {
                    str(k): dict(v) for k, v in counters.items()
                    if isinstance(v, dict)
                }
            plan_meta = snapshot.get("planMeta")
            if isinstance(plan_meta, dict):
                self.plan_meta = dict(plan_meta)
            self.revision_reuse = dict(snapshot.get("revisionReuse") or {})
            for agent_id in self.executed:
                self.guard.record_completed_agent(agent_id)
            return True
        except Exception as exc:  # noqa: BLE001 - a bad snapshot must not brick the run
            logger.warning("snapshot restore failed, starting fresh: %s", exc)
            return False

    # ------------------------------------------------------------------
    # single agent execution (unchanged core loop, per-agent budget)
    # ------------------------------------------------------------------

    def _agent_quota(self, agent_id: str, key: str, fallback: int) -> int:
        """Plan-time quota for one agent (llmQuota/toolQuota); falls back to
        the static policy limit when the coordinator did not allocate one."""
        quota = self.budget_plan.get(agent_id) or {}
        value = quota.get(key)
        return int(value) if isinstance(value, (int, float)) and value >= 0 else fallback

    async def _run_agent(self, definition: AgentDefinition) -> AgentOutput:
        # Parallel Agent branches share one workflow permit. It is detached
        # only while every live branch is suspended in provider I/O.
        async with workflow_agent_execution(self.workflow_execution):
            return await self._run_agent_in_slot(definition)

    async def _run_agent_in_slot(
            self, definition: AgentDefinition) -> AgentOutput:
        request = self.request
        agent_id = definition.agent_id
        if agent_id in TERMINAL_AGENTS:
            # No control-plane work is legal after the terminal stage starts.
            # Release unused planning/arbitration capacity to the terminal.
            self.budget.release_llm_reservation("control")
        prompt = default_prompt_manager.system_for_agent(
            agent_id, self.policy.promptVersions.get(agent_id))
        signals = Coordinator(self.registry, self.policy, None).inspect_signals(
            resume_text=request.resumeText or "",
            job_description=request.jobDescription or "",
            artifacts=self.state.data.get("artifacts") or {},
            shared=self.state.data)
        single_pass_evaluation = request.runType in (
            "full_evaluation", "jd_evaluation", "backend_eval", "agent_eval")
        skills = default_skill_manager.select_for(
            agent_id=agent_id, run_type=request.runType,
            job_focus=self.policy.jobFocus, overrides=self.policy.skillOverrides,
            signals=signals, user_message=request.userMessage or "")
        eager_skill_ids = _configured_eager_skill_ids()
        eager_loaded_skills: Dict[str, Any] = {}
        eager_skill_call_ids: Dict[str, str] = {}
        for metadata in skills:
            if metadata.skill_id not in eager_skill_ids:
                continue
            try:
                loaded = default_skill_manager.load(metadata.skill_id)
            except Exception as exc:  # noqa: BLE001 - fall back to progressive
                logger.warning(
                    "eager Skill load failed for %s; keeping progressive flow: %s",
                    metadata.skill_id, exc)
                continue
            eager_loaded_skills[metadata.skill_id] = loaded
            eager_skill_call_ids[metadata.skill_id] = (
                f"skill-eager-{uuid.uuid4().hex[:16]}")
        progressive_skills = [
            skill for skill in skills
            if skill.skill_id not in eager_loaded_skills
        ]
        # Build the requested surface now, but do not expose Skill/MCP metadata
        # until we know this agent will actually execute an LLM turn. This
        # prevents deterministic fast paths from producing phantom standalone
        # Skill/MCP rows in the trace.
        requested_tools = list(definition.tools)
        if progressive_skills:
            requested_tools.append("load_skill")
        # Metadata/body already advertises the exact optional resource paths.
        # Hiding the generic reader when none exist prevents models from
        # inventing SKILL.md/hash paths after the body was already loaded.
        if any(skill.resource_paths for skill in skills):
            requested_tools.append("read_skill_resource")

        rag_context_block, retrieval_refs = await self._retrieve_rag_context(
            definition)
        tool_results_block = ""
        pre_llm_tool_call_ids: List[str] = []
        pre_llm_succeeded_tools: set[str] = set()
        agent_tool_calls = 0
        agent_llm_calls = 0
        agent_tool_limit = min(
            definition.max_tool_calls,
            self.policy.toolBudget.maxToolCallsPerAgent,
            self._agent_quota(agent_id, "toolQuota",
                              self.policy.toolBudget.maxToolCallsPerAgent))

        # Deterministic parsing / control-plane retrieval may run as harness
        # preflight. Public MCP never appears here: the model alone proposes
        # MCP name and arguments from the live schemas above.
        for tool, args in self._pre_steps(definition):
            if agent_tool_calls >= agent_tool_limit:
                break
            defn = self.tools.definitions.get(tool)
            if defn is not None and defn.kind == "mcp":
                logger.error("ignored invalid hard-coded MCP prestep %s", tool)
                continue
            guard = self.guard.check_tool_call(ToolExecutor.signature(tool, args))
            if guard.triggered:
                await self._emit_guard(guard, agent_id)
                continue
            rewrite = (
                tool in ("knowledge_search", "resume_semantic_search")
                and self.request.runType in ("followup", "quick_answer")
            )
            try:
                call = await self.tools.execute(agent_id, tool, args,
                                                enable_rewrite=rewrite)
            except Exception as _tool_exc:  # noqa: BLE001
                from .tools import ToolCallResult
                call = ToolCallResult(
                    f"tc-err-{agent_tool_calls}", tool, "FAILED", None,
                    error=f"{type(_tool_exc).__name__}: {_tool_exc}"[:200])
            agent_tool_calls += 1
            pre_llm_tool_call_ids.append(call.tool_call_id)
            tool_results_block += self._format_tool_result(call)
            if call.status == "SUCCEEDED":
                pre_llm_succeeded_tools.add(tool)
                await self._record_tool_success(
                    agent_id, tool, args, call.result,
                    tool_call_id=call.tool_call_id,
                    duration_ms=call.duration_ms)

        # This block is immutable input to the provider. Native action results
        # are carried by assistant/tool messages in ``native_history``. Once
        # that history exists, rebuilding the original user message with the
        # same observations duplicated at its tail destroys DeepSeek's exact
        # prefix reuse and wastes prompt tokens on every Project action turn.
        initial_tool_results_block = tool_results_block

        # Performance fast-path: Evidence verify clean → skip arbitration LLM.
        if definition.agent_id == "EvidenceAgent":
            fast = self._maybe_skip_evidence_llm(tool_results_block)
            if fast is not None:
                self.skill_selections[agent_id] = []
                self.agent_counters[definition.agent_id] = {
                    "iterations": 0, "llmCalls": 0, "toolCalls": agent_tool_calls,
                    "fastPath": 1}
                return fast

        # From this point onward the model really receives the progressive
        # Skill metadata and live MCP tools/list schemas.
        self.skill_selections[agent_id] = skills
        try:
            await default_skill_manager.emit_catalog(
                self.emitter, agent_id, skills)
            await default_skill_manager.emit_selection(
                self.emitter, agent_id, skills,
                trigger_reason="agent_capability_and_input_signals")
            for skill_id, loaded in eager_loaded_skills.items():
                await default_skill_manager.emit_loaded(
                    self.emitter, agent_id, loaded,
                    tool_call_id=eager_skill_call_ids[skill_id],
                    reason="configured_eager_experiment")
        except Exception as exc:  # noqa: BLE001
            logger.debug("skill catalog/selection emit skipped: %s", exc)

        catalog: List[Dict[str, Any]] = []
        catalog_exposure_ids: Dict[str, str] = {}
        try:
            model_requested_tools = [
                tool for tool in requested_tools
                if not (single_pass_evaluation
                        and tool in pre_llm_succeeded_tools)
            ]
            # Successful deterministic pre-steps are already attached as
            # observations. Re-exposing the same tools made the model repeat
            # them and consume the action turn needed for Skill/MCP selection.
            # Per-agent Skill enums must not mutate the shared ToolDefinition
            # schemas held by this run's other agents.
            catalog = copy.deepcopy(self.tools.catalog_for_agent(
                agent_id, model_requested_tools))
            progressive_skill_ids = [
                skill.skill_id for skill in progressive_skills]
            selected_skill_ids = [skill.skill_id for skill in skills]
            for entry in catalog:
                entry_name = str(entry.get("name") or "")
                if entry_name in {"load_skill", "read_skill_resource"}:
                    schema = entry.get("inputSchema")
                    properties = schema.get("properties") \
                        if isinstance(schema, dict) else None
                    skill_id_schema = properties.get("skill_id") \
                        if isinstance(properties, dict) else None
                    if isinstance(skill_id_schema, dict):
                        allowed_ids = (
                            progressive_skill_ids
                            if entry_name == "load_skill"
                            else selected_skill_ids)
                        skill_id_schema["enum"] = allowed_ids
                        skill_id_schema["description"] = (
                            "必须使用枚举中的规范 Skill ID；不要附加版本或哈希")
                if entry.get("kind") == "mcp" or entry.get("mcpServer"):
                    exposure_id = f"catalog-{uuid.uuid4().hex[:16]}"
                    catalog_exposure_ids[str(entry.get("name") or "")] = exposure_id
                    await self.emitter.emit(
                        "tool.progress", agent_id=agent_id,
                        tool_name=str(entry.get("name") or ""),
                        payload={
                            "toolCallId": exposure_id,
                            "lifecycleStage": "CATALOG_EXPOSED",
                            "catalogScope": "AGENT_MODEL_INPUT",
                            "source": "mcp",
                            "mcpServer": entry.get("mcpServer"),
                            "toolName": entry.get("name"),
                            "modelName": entry.get("modelName"),
                            "description": entry.get("description"),
                            "inputSchema": entry.get("inputSchema"),
                            "occurredAt": _utc_now(),
                        })
        except Exception as exc:  # noqa: BLE001
            logger.debug("live tool catalog unavailable: %s", exc)
            catalog = []
        model_tools, model_tool_aliases = ToolExecutor.openai_tools(catalog)
        allowed_tools = set(model_tool_aliases.values())
        final_tool = (EMIT_REPORT_TOOL if agent_id == "ReportAgent"
                      else EMIT_DECISION_TOOL)
        model_tools.append(final_tool)

        output: Optional[AgentOutput] = None
        # A reasoning iteration and a provider-native action/observation turn
        # are different budget axes.  If they share one counter, a normal
        # progressive flow such as load_skill -> read_resource -> final answer
        # can never finish for agents whose decision budget is one or two.
        # Reserve at most two action follow-up turns normally. An external-URL
        # Project may need three (local evidence -> Skill -> MCP) before its
        # final decision; every call still counts against the run-wide hard
        # LLM/token/cost limits enforced by the client.
        max_decision_iterations = min(
            definition.max_iterations, self.policy.maxIterationsPerAgent)
        if single_pass_evaluation:
            # Normal path is still one decision. Keep two conditional repair
            # slots for malformed provider JSON. Under sustained load we have
            # observed both the native function arguments and the first JSON
            # repair arrive truncated; the third decision is never consumed
            # on a valid path and remains governed by the run-wide ledger.
            max_decision_iterations = 3
        action_turn_ceiling = (
            3 if agent_id == "ProjectAgent"
            and signals.get("has_external_urls") else 2)
        if single_pass_evaluation:
            research_turn_ceiling = (
                2 if agent_id == "ProjectAgent"
                and signals.get("has_external_urls")
                else 3 if agent_id == "TechAgent"
                and (signals.get("has_framework_stack")
                     or signals.get("has_microsoft_stack"))
                else 0)
            # Skill metadata is present in the model context, but the body is
            # disclosed only if the model calls load_skill. Reserving a turn
            # makes that choice real without requiring any Skill invocation.
            skill_action_available = any(
                tool in requested_tools
                for tool in {"load_skill", "read_skill_resource"}
            )
            action_turn_ceiling = max(
                research_turn_ceiling, 1 if skill_action_available else 0)
        available_tool_turns = min(
            action_turn_ceiling,
            max(0, agent_tool_limit - agent_tool_calls))
        fallback_turn_limit = max_decision_iterations + available_tool_turns
        agent_llm_quota = self._agent_quota(
            agent_id, "llmQuota", fallback_turn_limit)
        final_turn_reserve = min(
            2 if agent_id == "ReportAgent" else 1,
            max(1, agent_llm_quota))
        max_action_turns = min(
            available_tool_turns,
            self._agent_quota(
                agent_id, "actionTurnQuota", available_tool_turns),
            max(0, agent_llm_quota - final_turn_reserve))
        max_turns = min(
            fallback_turn_limit, max(0, agent_llm_quota))
        iteration = 0
        decision_iterations = 0
        action_turns = 0
        borrowed_repair_turns = 0
        json_repair_pending = False
        loaded_skills: Dict[str, Any] = dict(eager_loaded_skills)
        loaded_skill_call_ids: Dict[str, str] = dict(eager_skill_call_ids)
        applied_skill_ids: set = set()
        native_history: List[Dict[str, Any]] = []
        # Memory selection is immutable for this agent execution. Keeping the
        # audit local avoids cross-agent races when specialists run in parallel;
        # the same block is intentionally attached to each provider prompt.
        memory_block, memory_audit = self._memory_context(definition)
        memory_usage_recorded = False
        while (iteration < max_turns
               and decision_iterations < max_decision_iterations
               and output is None):
            iteration += 1
            round_id = f"{self.request.runId}:{agent_id}:round:{iteration}"
            await self.emitter.emit("agent.progress", agent_id=agent_id, payload={
                "roundId": round_id,
                "iteration": iteration,
                "maxIterations": max_turns,
                "decisionIterations": decision_iterations,
                "decisionIterationLimit": max_decision_iterations,
                "actionTurns": action_turns,
                "actionTurnAllowance": max_action_turns,
            })
            effective_tool_results = (
                initial_tool_results_block
                if native_history else tool_results_block)
            skill_text = default_skill_manager.render_progressive(
                skills, list(loaded_skills.values()))
            messages = self.context.assemble(
                system_prompt=prompt.content,
                policy_instructions=self._policy_instructions(),
                skill_instructions=skill_text,
                user_request=request.userMessage or "（对当前简历执行你的职责）",
                agent_task=definition.task_prompt or definition.description,
                current_goal=request.currentGoal or "",
                shared_state_digest=self.state.view_for(agent_id),
                recent_messages=request.recentMessages,
                conversation_summary=request.conversationSummary or "",
                memory_block=memory_block,
                rag_context_block=rag_context_block,
                tool_results_block=effective_tool_results,
                output_schema=(REPORT_OUTPUT_SCHEMA if agent_id == "ReportAgent"
                               else AGENT_OUTPUT_SCHEMA))
            if native_history:
                messages.extend(native_history)
            if self.context.needs_compaction(messages):
                messages = await self.context.compact(
                    messages, reason="context_over_threshold",
                    protected_markers=["[原始请求]", "[本Agent任务]",
                                       "[当前目标]", "[输出要求]"],
                    recent_messages=request.recentMessages)
                violations = self.context.consistency_check(
                    messages, user_request=(request.userMessage or "")[:80],
                    current_goal=(request.currentGoal or "")[:60],
                    agent_task=(definition.task_prompt
                                or definition.description)[:80])
                if violations:
                    logger.warning("compaction consistency violations: %s", violations)

            is_report = definition.agent_id == "ReportAgent"
            # A loaded skill is "applied" only when its instructions actually
            # enter a subsequent model turn.
            for skill_id, loaded in loaded_skills.items():
                if skill_id in applied_skill_ids:
                    continue
                if loaded.instructions and loaded.instructions[:80] in "\n".join(
                        str(m.get("content") or "") for m in messages):
                    await default_skill_manager.emit_applied(
                        self.emitter, agent_id, loaded,
                        tool_call_id=loaded_skill_call_ids[skill_id],
                        round_id=round_id)
                    applied_skill_ids.add(skill_id)

            # Once the model has consumed its native action turns, remove every
            # action tool and force the provider-native terminal function.
            # Rejecting another proposed action would consume the final LLM
            # turn and strand ReportAgent without a report.
            force_final = (
                action_turns >= max_action_turns
                or iteration >= max_turns
            )
            turn_tools = (
                [] if json_repair_pending
                else [final_tool] if force_final
                else model_tools
            )
            turn_messages = list(messages)
            tool_choice: Any = "auto"
            if json_repair_pending:
                tool_choice = None
                turn_messages.append({
                    "role": "user",
                    "content": (
                        "上一次原生函数参数不是合法 JSON。"
                        "本轮不要调用任何工具，直接输出一个符合输出 schema "
                        "的 JSON 对象，不要使用 markdown 或 DSML 包装。"
                    ),
                })
            elif force_final:
                terminal_name = str(final_tool["function"]["name"])
                tool_choice = {
                    "type": "function",
                    "function": {"name": terminal_name},
                }
                turn_messages.append({
                    "role": "user",
                    "content": (
                        "工具观察阶段已结束。现在必须仅调用 "
                        f"{terminal_name} 提交最终结构化结果；"
                        "不要再请求任何检索、Skill 或校验工具。"
                    ),
                })
            # Persist memory usage once, at the first prompt that consumes it.
            # Every later round still declares it as an input attachment below,
            # without duplicating durable usage rows or sibling trace nodes.
            if memory_audit and not memory_usage_recorded:
                decisions = memory_audit.get("decisions") or []
                for decision in decisions:
                    decision.roundId = round_id
                    decision.occurredAt = _utc_now()
                if decisions:
                    try:
                        await self.memory.record_usage(
                            consumer_agent=agent_id, decisions=decisions)
                    except Exception:  # noqa: BLE001
                        pass
                memory_usage_recorded = True

            turn_model_names = {
                str(item.get("function", {}).get("name") or "")
                for item in turn_tools if isinstance(item, dict)
            }
            tool_catalog_refs = [
                {
                    "toolName": entry.get("name"),
                    "modelName": entry.get("modelName"),
                    "source": ("mcp" if entry.get("mcpServer") else
                               entry.get("kind") or "builtin"),
                    "mcpServer": entry.get("mcpServer"),
                    "description": entry.get("description"),
                    "inputSchema": entry.get("inputSchema"),
                }
                for entry in catalog
                if str(entry.get("modelName") or "") in turn_model_names
            ]
            terminal_name = str(final_tool.get("function", {}).get("name") or "")
            if terminal_name in turn_model_names:
                tool_catalog_refs.append({
                    "toolName": terminal_name,
                    "modelName": terminal_name,
                    "source": "runtime_terminal",
                    "mcpServer": None,
                    "description": final_tool.get("function", {}).get("description"),
                    "inputSchema": final_tool.get("function", {}).get("parameters"),
                })
            memory_refs = [
                row for row in (memory_audit.get("memoryTrace") or [])
                if row.get("used")
            ] if memory_audit else []
            skill_refs = [{
                "skillId": skill.skill_id,
                "skillVersion": (
                    loaded_skills.get(skill.skill_id, skill).version),
                "disclosureState": (
                    "INSTRUCTIONS" if skill.skill_id in loaded_skills
                    else "METADATA"),
                "selected": True,
                "loaded": skill.skill_id in loaded_skills,
                "applied": skill.skill_id in applied_skill_ids,
                "instructionsAttached": skill.skill_id in applied_skill_ids,
                "loadToolCallId": loaded_skill_call_ids.get(skill.skill_id),
            } for skill in skills]
            observed_tool_call_ids = list(dict.fromkeys(
                pre_llm_tool_call_ids + [
                    str(message.get("tool_call_id") or "")
                    for message in native_history
                    if message.get("role") == "tool"
                    and message.get("tool_call_id")
                ]))
            trace_context = {
                "roundId": round_id,
                "parentAgentId": agent_id,
                "contextRole": "MODEL_INPUT",
            }
            await self.emitter.emit(
                "llm.context.attached", agent_id=agent_id, payload={
                    **trace_context,
                    "memoryRefs": memory_refs,
                    "skillRefs": skill_refs,
                    "toolCatalogRefs": tool_catalog_refs,
                    "retrievalRefs": retrieval_refs,
                    "observedToolCallIds": observed_tool_call_ids,
                    "memoryCount": len(memory_refs),
                    "skillCount": len(skill_refs),
                    "memoryAttachedCount": len(memory_refs),
                    "skillMetadataCount": len(skills),
                    "skillInstructionCount": sum(
                        1 for ref in skill_refs
                        if ref["instructionsAttached"]),
                    "toolCatalogCount": len(tool_catalog_refs),
                    "retrievalCount": len(retrieval_refs),
                    "messageCount": len(turn_messages),
                    "toolChoice": tool_choice,
                    "occurredAt": _utc_now(),
                })
            turn_max_tokens = (
                8192 if is_report
                else 6144 if definition.agent_id == "EvidenceAgent"
                and single_pass_evaluation
                else 3600 if definition.agent_id in TERMINAL_AGENTS
                else 4096)
            was_json_repair_turn = json_repair_pending
            try:
                if json_repair_pending:
                    repair_max_tokens = (
                        min(turn_max_tokens, 2400)
                        if agent_id == "RiskAgent" else turn_max_tokens)
                    turn = await self._chat_json_repair_turn(
                        turn_messages, agent_id=agent_id,
                        purpose=definition.output_type,
                        max_tokens=repair_max_tokens,
                        use_quality=is_report,
                        fallback_tool=final_tool,
                        trace_context=trace_context)
                    json_repair_pending = False
                else:
                    turn = await self._chat_native_turn(
                        turn_messages, agent_id=agent_id,
                        purpose=definition.output_type,
                        # Full reports routinely exceed 4k tokens because every
                        # score, risk and interview probe is evidence-bound.
                        max_tokens=turn_max_tokens,
                        tools=turn_tools,
                        tool_choice=tool_choice,
                        use_quality=is_report,
                        trace_context=trace_context)
            except LlmError as exc:
                if (was_json_repair_turn
                        and agent_id == "RiskAgent"
                        and exc.code in {"JSON_TRUNCATED", "MALFORMED_OUTPUT"}):
                    note = (
                        "RiskAgent JSON repair did not produce a bounded "
                        "schema-valid result; "
                        "kept deterministic timeline checks and delegated "
                        "grounded risk synthesis to ReportAgent")
                    self.failure_notes.append(note)
                    await self.emitter.emit(
                        "run.progress", agent_id=agent_id, payload={
                            "stage": "specialist_repair_compacted",
                            "reason": (
                                "risk_json_repair_truncated"
                                if exc.code == "JSON_TRUNCATED" else
                                "risk_json_repair_malformed"),
                            "error": str(exc)[:240],
                            "occurredAt": _utc_now(),
                        })
                    output = self._build_output(
                        definition, None, tool_results_block)
                    break
                raise
            agent_llm_calls += 1
            raw = turn.content
            final_calls: List[Any] = []

            if turn.tool_calls:
                native_history.append({
                    "role": "assistant",
                    "content": turn.content or None,
                    "tool_calls": [
                        {
                            "id": call.tool_call_id,
                            "type": "function",
                            "function": {
                                "name": call.name,
                                "arguments": call.raw_arguments,
                            },
                        }
                        for call in turn.tool_calls
                    ],
                })
                final_calls = [
                    call for call in turn.tool_calls
                    if call.name == "emit_decision"]
                action_calls = [
                    call for call in turn.tool_calls
                    if call.name != "emit_decision"]
                unexpected_final = bool(
                    final_calls and "emit_decision" not in turn_model_names)

                # When actions and a final emission appear together, execute
                # the actions and explicitly defer the stale final proposal.
                if action_calls or unexpected_final:
                    action_turn_allowed = action_turns < max_action_turns
                    exposed_action_calls = [
                        call for call in action_calls
                        if call.name in turn_model_names
                    ]
                    if action_turn_allowed and exposed_action_calls:
                        action_turns += 1
                    observations = ""
                    tool_messages: List[Dict[str, Any]] = []
                    for proposed in turn.tool_calls:
                        if proposed.name == "emit_decision":
                            result_payload = {
                                "success": False,
                                "deferred": True,
                                "reason": "tool results must be observed before final output",
                            }
                            tool_messages.append({
                                "role": "tool",
                                "tool_call_id": proposed.tool_call_id,
                                "name": proposed.name,
                                "content": json.dumps(
                                    result_payload, ensure_ascii=False),
                            })
                            continue

                        tool = model_tool_aliases.get(proposed.name, "")
                        if (not tool
                                and proposed.name in pre_llm_succeeded_tools):
                            tool = proposed.name
                        entry = next(
                            (item for item in catalog
                             if item.get("name") == tool), {})
                        defn = self.tools.definitions.get(tool)
                        source = (
                            "mcp" if defn and defn.kind == "mcp"
                            else "skill" if tool in {
                                "load_skill", "read_skill_resource"}
                            else defn.kind if defn else "unknown")
                        if source == "mcp":
                            # Bind the earlier catalog exposure to the provider
                            # call id so catalog → proposal → execution → result
                            # can be queried as one trace chain.
                            await self.emitter.emit(
                                "tool.progress", agent_id=agent_id,
                                tool_name=tool or proposed.name, payload={
                                    **trace_context,
                                    "toolCallId": proposed.tool_call_id,
                                    "lifecycleStage": "CATALOG_EXPOSED",
                                    "source": "mcp",
                                    "mcpServer": entry.get("mcpServer"),
                                    "toolName": tool or proposed.name,
                                    "modelName": proposed.name,
                                    "description": entry.get("description"),
                                    "inputSchema": entry.get("inputSchema"),
                                    "originalExposureId": (
                                        catalog_exposure_ids.get(tool)),
                                    "occurredAt": _utc_now(),
                                })
                        await self.emitter.emit(
                            "tool.progress", agent_id=agent_id,
                            tool_name=tool or proposed.name, payload={
                                **trace_context,
                                "toolCallId": proposed.tool_call_id,
                                "lifecycleStage": "LLM_PROPOSED",
                                "source": source,
                                "mcpServer": (
                                    entry.get("mcpServer")
                                    or (defn.mcp_server if defn else None)),
                                "toolName": tool or proposed.name,
                                "modelName": proposed.name,
                                "inputSchema": (
                                    entry.get("inputSchema")
                                    or (defn.input_schema if defn else None)),
                                "arguments": proposed.arguments,
                                "argumentsParseError": (
                                    proposed.arguments_error or None),
                                "occurredAt": _utc_now(),
                            })

                        if not action_turn_allowed:
                            result_payload = await self._reject_native_proposal(
                                agent_id, tool or proposed.name, proposed,
                                "native action-turn budget exhausted; emit a final decision",
                                source=source,
                                mcp_server=entry.get("mcpServer"),
                                trace_context=trace_context)
                        elif (proposed.name not in turn_model_names
                              and tool in pre_llm_succeeded_tools):
                            result_payload = (
                                await self._ack_duplicate_native_proposal(
                                    agent_id, tool, proposed,
                                    trace_context=trace_context))
                        elif proposed.name not in turn_model_names:
                            result_payload = await self._reject_native_proposal(
                                agent_id, tool or proposed.name, proposed,
                                "tool was not exposed in this model turn",
                                source=source,
                                mcp_server=entry.get("mcpServer"),
                                trace_context=trace_context)
                        elif proposed.arguments_error:
                            result_payload = await self._reject_native_proposal(
                                agent_id, tool or proposed.name, proposed,
                                f"arguments are not a JSON object: "
                                f"{proposed.arguments_error}", source=source,
                                mcp_server=entry.get("mcpServer"),
                                trace_context=trace_context)
                        elif not tool or tool not in allowed_tools:
                            result_payload = await self._reject_native_proposal(
                                agent_id, tool or proposed.name, proposed,
                                "tool was not present in this agent's exposed catalog",
                                source=source,
                                mcp_server=entry.get("mcpServer"),
                                trace_context=trace_context)
                        elif agent_tool_calls >= agent_tool_limit:
                            result_payload = await self._reject_native_proposal(
                                agent_id, tool, proposed,
                                "agent tool budget exhausted", source=source,
                                mcp_server=entry.get("mcpServer"),
                                trace_context=trace_context)
                        elif tool in {"load_skill", "read_skill_resource"}:
                            result_payload = await self._execute_skill_proposal(
                                agent_id=agent_id,
                                tool=tool,
                                proposed=proposed,
                                selected_skills=skills,
                                loaded_skills=loaded_skills,
                                loaded_skill_call_ids=loaded_skill_call_ids,
                                trace_context=trace_context)
                            agent_tool_calls += 1
                        else:
                            args = proposed.arguments
                            guard = self.guard.check_tool_call(
                                ToolExecutor.signature(tool, args))
                            if guard.triggered:
                                await self._emit_guard(guard, agent_id)
                                result_payload = await self._reject_native_proposal(
                                    agent_id, tool, proposed,
                                    "duplicate call blocked by loop guard",
                                    source=source,
                                    mcp_server=entry.get("mcpServer"),
                                    trace_context=trace_context)
                            else:
                                call = await self.tools.execute(
                                    agent_id, tool, args,
                                    # The model already authored these native
                                    # tool arguments. Never spend a hidden
                                    # provider call rewriting them.
                                    enable_rewrite=False,
                                    tool_call_id=proposed.tool_call_id,
                                    trace_context=trace_context)
                                agent_tool_calls += 1
                                observations += self._format_tool_result(call)
                                if call.status == "SUCCEEDED":
                                    await self._record_tool_success(
                                        agent_id, tool, args, call.result,
                                        tool_call_id=call.tool_call_id,
                                        duration_ms=call.duration_ms)
                                result_payload = {
                                    "success": call.status == "SUCCEEDED",
                                    "status": call.status,
                                    "result": call.result if (
                                        call.status == "SUCCEEDED") else None,
                                    "error": call.error if (
                                        call.status != "SUCCEEDED") else None,
                                }
                        observations += (
                            f"\n[MODEL_TOOL_RESULT {tool or proposed.name} "
                            f"id={proposed.tool_call_id}] "
                            f"{json.dumps(result_payload, ensure_ascii=False)[:1500]}")
                        tool_messages.append({
                            "role": "tool",
                            "tool_call_id": proposed.tool_call_id,
                            "name": proposed.name,
                            "content": json.dumps(
                                result_payload, ensure_ascii=False)[
                                    :(6000 if source == "mcp" else 8000)],
                        })
                    native_history.extend(tool_messages)
                    guard = self.guard.check_observation(observations)
                    if guard.triggered:
                        await self._emit_guard(guard, agent_id)
                    tool_results_block += observations
                    continue

                # No actions: the final structured function arguments are the
                # decision payload. A plain content JSON remains a compatibility
                # fallback for providers without function calling.
                if final_calls:
                    raw = final_calls[0].raw_arguments

            decision_iterations += 1
            decision, schema_error = self._parse_decision(raw)
            if (decision is None and final_calls
                    and final_calls[0].arguments_error):
                schema_error = (
                    "provider function arguments are not valid JSON: "
                    f"{final_calls[0].arguments_error[:300]}")
            if (decision is not None and final_calls
                    and not decision.get("done")
                    and not decision.get("output")):
                # Calling the terminal function is a final-emission contract.
                # Treating an empty done=false emission as a normal reasoning
                # turn left its tool_call_id unanswered and guaranteed the
                # next provider request would be rejected.
                decision = None
                schema_error = (
                    "emit_decision returned done=false with no output; "
                    "submit a complete final decision")
            if (decision is not None
                    and agent_id == "ReportAgent"
                    and self._requires_score_contract()):
                report_error = self._report_decision_schema_error(decision)
                if report_error:
                    decision = None
                    schema_error = report_error
            if decision is None:
                repair_allowed = (
                    decision_iterations < max_decision_iterations)
                # A native action followed by a malformed terminal function
                # can consume the agent's final planned turn.  When the
                # run-wide ledger still has assignable capacity, borrow one
                # explicit repair turn instead of stranding a valid run.
                # The provider client remains the hard budget authority.
                runtime_budget = getattr(self.llm, "budget", self.budget)
                repair_scope = (
                    "control" if agent_id == "CoordinatorAgent"
                    else "terminal" if agent_id in TERMINAL_AGENTS
                    else f"agent:{agent_id}")
                can_borrow_repair = (
                    repair_allowed
                    and (bool(final_calls) or was_json_repair_turn)
                    and iteration >= max_turns
                    and borrowed_repair_turns < 1
                    and runtime_budget.available_llm_calls_for_scope(
                        self.policy.maxLlmCalls, repair_scope) > 0
                )
                if can_borrow_repair:
                    borrowed_repair_turns += 1
                    max_turns += 1
                    await self.emitter.emit(
                        "run.progress", agent_id=agent_id, payload={
                            "stage": "budget_reallocated",
                            "reason": "malformed_native_final",
                            "borrowedRepairTurns": borrowed_repair_turns,
                            "plannedLlmQuota": agent_llm_quota,
                            "effectiveTurnLimit": max_turns,
                            "schemaError": schema_error[:200],
                            "occurredAt": _utc_now(),
                        })
                if repair_allowed and iteration < max_turns:
                    # Once schema validation fails, stay on the provider's
                    # JSON-only repair channel. Returning to native function
                    # arguments after a malformed JSON repair merely repeats
                    # the truncation mode observed under sustained load.
                    json_repair_pending = True
                    repair_message = (
                        "上面的输出未通过 json schema 校验："
                        f"{schema_error[:400]}。"
                        "请使用 emit_decision/emit_report 提交修正后的结构化结果。")
                    if final_calls:
                        # The assistant declared every call id in this turn.
                        # OpenAI-compatible providers require one tool response
                        # for *each* id before any user/assistant repair message.
                        for final_call in final_calls:
                            native_history.append({
                                "role": "tool",
                                "tool_call_id": final_call.tool_call_id,
                                "name": final_call.name,
                                "content": json.dumps({
                                    "success": False,
                                    "error": schema_error[:400],
                                    "retryable": True,
                                }, ensure_ascii=False),
                            })
                    elif raw:
                        native_history.append({
                            "role": "assistant", "content": raw[:1500]})
                    native_history.append({
                        "role": "user", "content": repair_message})
                    continue
                if was_json_repair_turn and agent_id == "RiskAgent":
                    note = (
                        "RiskAgent JSON repair remained schema-invalid; "
                        "kept deterministic timeline checks and delegated "
                        "grounded risk synthesis to ReportAgent")
                    self.failure_notes.append(note)
                    await self.emitter.emit(
                        "run.progress", agent_id=agent_id, payload={
                            "stage": "specialist_repair_compacted",
                            "reason": "risk_json_repair_malformed",
                            "schemaError": schema_error[:240],
                            "occurredAt": _utc_now(),
                        })
                    output = self._build_output(
                        definition, None, tool_results_block)
                    break
                raise LlmError(
                    "MALFORMED_OUTPUT",
                    f"agent output failed schema validation within budget: "
                    f"{schema_error[:200]}",
                    False)

            thought = str(decision.get("thought") or "")
            if thought:
                guard = self.guard.check_plan(f"{agent_id}:{thought}")
                if guard.triggered:
                    await self._emit_guard(guard, agent_id)
                    decision["toolCalls"] = []
                    decision["done"] = True

            nested_calls = decision.get("toolCalls") or []
            if nested_calls:
                await self.emitter.emit("run.progress", agent_id=agent_id, payload={
                    "stage": "nested_tool_calls_rejected",
                    "reason": "tools must use provider-native function calls",
                    "count": len(nested_calls),
                    "occurredAt": _utc_now(),
                })
                decision["toolCalls"] = []
                if (not decision.get("output")
                        and decision_iterations < max_decision_iterations
                        and iteration < max_turns):
                    tool_results_block += (
                        "\n[NATIVE_TOOL_REQUIRED] JSON 内嵌 toolCalls 已拒绝；"
                        "请使用已提供的原生 function tools。")
                    continue

            raw_output = decision.get("output")
            if (raw_output or decision.get("done")
                    or decision_iterations >= max_decision_iterations
                    or iteration >= max_turns):
                output = self._build_output(definition, raw_output, tool_results_block)

        if output is None:
            output = self._build_output(definition, None, tool_results_block)
        self.skill_selections[agent_id] = [
            loaded_skills.get(skill.skill_id, skill) for skill in skills]
        await self._emit_unapplied_skills(
            agent_id, skills, loaded_skills, applied_skill_ids,
            reason="model_did_not_load_or_apply")
        self.agent_counters[definition.agent_id] = {
            "iterations": iteration,
            "decisionIterations": decision_iterations,
            "actionTurns": action_turns,
            "llmCalls": agent_llm_calls,
            "toolCalls": agent_tool_calls,
            "borrowedRepairTurns": borrowed_repair_turns,
        }
        return output

    async def _chat_native_turn(self, messages: List[Dict[str, Any]], *,
                                agent_id: str, purpose: str,
                                max_tokens: int,
                                tools: List[Dict[str, Any]],
                                use_quality: bool,
                                tool_choice: Any = "auto",
                                trace_context: Optional[Dict[str, Any]] = None
                                ) -> LlmTurn:
        """Use native tool calling when supported; preserve test adapters."""
        chat_turn = getattr(self.llm, "chat_turn", None)
        if callable(chat_turn):
            kwargs: Dict[str, Any] = {
                "agent_id": agent_id,
                "purpose": purpose,
                "max_tokens": max_tokens,
                "tools": tools,
                "tool_choice": tool_choice,
                "use_quality": use_quality,
            }
            # Compatibility adapters often accept **kwargs only to forward
            # them to an older strict signature. Pass trace metadata solely
            # when the adapter explicitly declares support for it.
            if "trace_context" in inspect.signature(chat_turn).parameters:
                kwargs["trace_context"] = trace_context
            return await chat_turn(messages, **kwargs)
        chat = self.llm.chat
        kwargs = {
            "agent_id": agent_id,
            "purpose": purpose,
            "max_tokens": max_tokens,
            "tools": tools,
            "tool_choice": tool_choice,
            "use_quality": use_quality,
        }
        if "trace_context" in inspect.signature(chat).parameters:
            kwargs["trace_context"] = trace_context
        raw = await chat(messages, **kwargs)
        return LlmTurn(content=str(raw or ""), tool_calls=[],
                       finish_reason="legacy_adapter")

    async def _chat_json_repair_turn(
            self, messages: List[Dict[str, Any]], *, agent_id: str,
            purpose: str, max_tokens: int, use_quality: bool,
            fallback_tool: Dict[str, Any],
            trace_context: Optional[Dict[str, Any]] = None) -> LlmTurn:
        """Repair malformed terminal function arguments through JSON mode.

        Repeating the same forced-function request caused DeepSeek-compatible
        providers to repeat malformed DSML/function arguments.  Production's
        resilient client supports a separate ``json_object`` channel; test and
        legacy adapters without that channel retain the native repair path.
        """
        if isinstance(self.llm, ResilientLlmClient):
            kwargs: Dict[str, Any] = {
                "agent_id": agent_id,
                "purpose": purpose,
                "max_tokens": max_tokens,
                "json_mode": True,
                "tools": None,
                "tool_choice": None,
                "use_quality": use_quality,
            }
            if "trace_context" in inspect.signature(
                    self.llm.chat).parameters:
                kwargs["trace_context"] = trace_context
            if (agent_id == "RiskAgent"
                    and "max_output_tokens_hard" in inspect.signature(
                        self.llm.chat).parameters):
                kwargs["max_output_tokens_hard"] = max_tokens
            raw = await self.llm.chat(
                self._json_only_messages(messages), **kwargs)
            return LlmTurn(
                content=str(raw or ""), tool_calls=[],
                finish_reason="json_repair")
        legacy_turn = await self._chat_native_turn(
            messages, agent_id=agent_id, purpose=purpose,
            max_tokens=max_tokens, tools=[fallback_tool],
            tool_choice={
                "type": "function",
                "function": {
                    "name": fallback_tool["function"]["name"]},
            },
            use_quality=use_quality, trace_context=trace_context)
        if legacy_turn.tool_calls:
            return LlmTurn(
                content=legacy_turn.tool_calls[0].raw_arguments,
                tool_calls=[], finish_reason="legacy_native_repair")
        return legacy_turn

    @staticmethod
    def _json_only_messages(
            messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Flatten provider-native history before switching to JSON mode.

        Some OpenAI-compatible gateways reject historical ``tool_calls`` when
        the current request uses ``response_format=json_object``, even when
        every call id has a tool response. Tool observations are already in
        the assembled context, so omit protocol frames but retain ordinary
        system/user/assistant content.
        """
        clean: List[Dict[str, Any]] = []
        for message in messages:
            role = str(message.get("role") or "")
            if role == "tool" or message.get("tool_calls"):
                continue
            content = message.get("content")
            if role not in {"system", "user", "assistant"} or not content:
                continue
            clean.append({"role": role, "content": str(content)})
        return clean

    def _parallel_report_sections_enabled(self) -> bool:
        configured = _os.getenv(
            "REPORT_PARALLEL_SECTIONS", "auto").strip().lower()
        return (
            configured not in {"0", "false", "no", "off"}
            and bool(getattr(
                self.llm, "supports_parallel_report_sections", False))
        )

    @staticmethod
    def _try_acquire_parallel_report_slot() -> bool:
        global _ACTIVE_PARALLEL_REPORTS
        try:
            limit = max(0, int(_os.getenv(
                "REPORT_PARALLEL_MAX_INFLIGHT", "12")))
        except ValueError:
            limit = 1
        if _ACTIVE_PARALLEL_REPORTS >= limit:
            return False
        _ACTIVE_PARALLEL_REPORTS += 1
        return True

    @staticmethod
    def _release_parallel_report_slot() -> None:
        global _ACTIVE_PARALLEL_REPORTS
        _ACTIVE_PARALLEL_REPORTS = max(0, _ACTIVE_PARALLEL_REPORTS - 1)

    def _should_parallel_report_sections(
            self, signals: Dict[str, Any]) -> bool:
        """Use section fan-out for every full report by default.

        The current-provider live A/B on strong, sparse and domestic-external
        resumes reduced end-to-end latency by 37%-61% while preserving the
        structured quality contract.  ``REPORT_PARALLEL_SECTIONS=0`` remains
        the explicit rollback switch.
        """
        if not self._parallel_report_sections_enabled():
            return False
        mode = _os.getenv(
            "REPORT_PARALLEL_SECTIONS", "auto").strip().lower()
        return mode not in {"0", "false", "no", "off"}

    async def _run_parallel_report_sections(
            self, messages: List[Dict[str, Any]], *, round_id: str,
            memory_refs: List[Dict[str, Any]],
            skill_refs: List[Dict[str, Any]],
            observed_tool_call_ids: List[str],
            is_sparse_resume: bool,
            max_calls: int = 4,
    ) -> Tuple[Optional[Dict[str, Any]], int]:
        """Generate score, risk and interview sections concurrently.

        A failed/weak merge returns ``None`` so the existing monolithic Pro
        path remains the automatic fallback. No partial section is published.
        """
        specs = _parallel_report_section_specs()

        async def generate(
                section: str, spec: Dict[str, Any], attempt: int = 1,
        ) -> Tuple[str, Dict[str, Any]]:
            section_started = time.monotonic()
            section_round = (
                f"{round_id}:section:{section}:attempt:{attempt}")
            tool = {
                "type": "function",
                "function": {
                    "name": "emit_report_section",
                    "description": f"提交 ReportAgent {section} 结构化小节",
                    "parameters": {
                        "type": "object",
                        "properties": spec["properties"],
                        "required": spec["required"],
                    },
                },
            }
            # Native terminal functions enforce the section JSON schema at
            # the provider boundary.  The previous ResilientLlmClient branch
            # used unconstrained json_mode, repeatedly produced too few
            # evidenceRefs, retried two sections and finally regenerated the
            # whole report (198s in the production trace).
            uses_json_mode = False
            section_messages = list(messages) + [{
                "role": "user",
                "content": (
                    "[并行报告小节任务]\n"
                    f"{spec['instruction']}\n"
                    + (
                        "硬性数量要求：interviewQuestions 必须输出4至8题，"
                        "不得只输出1题；至少分别覆盖HIGH风险核验、JD核心缺口、"
                        "项目技术深度、量化成果或履历可信度。\n"
                        if section == "question" else "")
                    + ("这是质量闸门后的定向重试，必须严格满足数量、"
                       "证据引用和结构要求；上次结果不合格，本次少于4题"
                       "将被拒绝。\n" if attempt > 1 else "")
                    + (
                        "直接输出一个完整 JSON 对象，不要使用 markdown。"
                        if uses_json_mode else
                        "必须调用 emit_report_section，一次提交完整结果。"
                    )),
            }]
            trace_context = {
                "roundId": section_round,
                "parentRoundId": round_id,
                "parentAgentId": "ReportAgent",
                "contextRole": "MODEL_INPUT",
                "reportSection": section,
            }
            await self.emitter.emit(
                "llm.context.attached", agent_id="ReportAgent", payload={
                    **trace_context,
                    "memoryRefs": memory_refs,
                    "skillRefs": skill_refs,
                    "observedToolCallIds": observed_tool_call_ids,
                    "memoryCount": len(memory_refs),
                    "skillCount": len(skill_refs),
                    "memoryAttachedCount": len(memory_refs),
                    "skillMetadataCount": len(skill_refs),
                    "skillInstructionCount": sum(
                        1 for ref in skill_refs
                        if ref.get("instructionsAttached")),
                    "toolCatalogRefs": [] if uses_json_mode else [{
                        "toolName": "emit_report_section",
                        "modelName": "emit_report_section",
                        "source": "runtime_terminal",
                        "mcpServer": None,
                        "description": tool["function"]["description"],
                        "inputSchema": tool["function"]["parameters"],
                    }],
                    "toolCatalogCount": 0 if uses_json_mode else 1,
                    "messageCount": len(section_messages),
                    "toolChoice": "json_object" if uses_json_mode else {
                        "type": "function",
                        "function": {"name": "emit_report_section"},
                    },
                    "sectionSchemaMode": "native_tool",
                    "occurredAt": _utc_now(),
                })
            if uses_json_mode:
                raw = await self.llm.chat(
                    self._json_only_messages(section_messages),
                    agent_id="ReportAgent",
                    purpose=f"report_{section}",
                    max_tokens=int(spec["maxTokens"]),
                    json_mode=True,
                    tools=None,
                    tool_choice=None,
                    use_quality=bool(spec["useQuality"]),
                    trace_context=trace_context)
                turn = LlmTurn(
                    content=str(raw or ""), tool_calls=[],
                    finish_reason="json_section")
            else:
                timeout_seconds = _report_section_timeout_seconds()
                try:
                    turn = await asyncio.wait_for(
                        self._chat_native_turn(
                            section_messages,
                            agent_id="ReportAgent",
                            purpose=f"report_{section}",
                            max_tokens=int(spec["maxTokens"]),
                            tools=[tool],
                            tool_choice={
                                "type": "function",
                                "function": {"name": "emit_report_section"},
                            },
                            use_quality=bool(spec["useQuality"]),
                            trace_context=trace_context),
                        timeout=timeout_seconds)
                except asyncio.TimeoutError as exc:
                    raise LlmError(
                        "TIMEOUT",
                        f"ReportAgent {section} exceeded "
                        f"{timeout_seconds:.2f}s wall-clock ceiling",
                        True) from exc
            call = next(
                (candidate for candidate in turn.tool_calls
                 if candidate.name == "emit_report_section"), None)
            if call is not None:
                if call.arguments_error:
                    salvaged = (
                        _salvage_first_complete_report_section(
                            call.raw_arguments, spec["required"])
                        if "Extra data" in call.arguments_error else None)
                    if salvaged is None:
                        raise LlmError(
                            "MALFORMED_OUTPUT", call.arguments_error, False)
                    payload = salvaged
                    await self.emitter.emit(
                        "run.progress", agent_id="ReportAgent", payload={
                            "stage": "parallel_report_section_salvaged",
                            "reason": "complete_first_object_before_extra_data",
                            "section": section,
                            "attempt": attempt,
                            "occurredAt": _utc_now(),
                        })
                else:
                    payload = dict(call.arguments)
            else:
                payload = extract_json_object(turn.content)
            if not isinstance(payload, dict) or not payload:
                raise LlmError(
                    "MALFORMED_OUTPUT",
                    f"ReportAgent {section} section is empty", False)

            # Compatibility adapters may still return the legacy full
            # AgentDecision shape. Extract only this section's fields.
            nested_output = payload.get("output")
            if isinstance(nested_output, dict):
                nested_report = nested_output.get("report")
                if isinstance(nested_report, dict):
                    payload = dict(nested_report)
                    if nested_output.get("summary"):
                        payload.setdefault(
                            "summary", nested_output["summary"])
            if section == "question" and not payload.get(
                    "interviewQuestions"):
                payload["interviewQuestions"] = list(
                    payload.get("interviewProbes") or [])
            missing = [
                field for field in spec["required"]
                if field not in payload
            ]
            if missing:
                raise LlmError(
                    "MALFORMED_OUTPUT",
                    f"ReportAgent {section} missing: {', '.join(missing)}",
                    False)
            await self.emitter.emit(
                "report.section.completed", agent_id="ReportAgent", payload={
                    **trace_context,
                    "section": section,
                    "attempt": attempt,
                    "durationMs": int(
                        (time.monotonic() - section_started) * 1000),
                    "validated": True,
                    "data": payload,
                    "occurredAt": _utc_now(),
                })
            return section, payload

        minimums = (
            {"dimensions": 2, "strengths": 1, "risks": 2,
             "questions": 4, "refs": 4}
            if is_sparse_resume else
            # Three evidence-bound core risks are more useful than forcing a
            # fourth filler item. Production traces showed the old 4/8 floor
            # discarded a complete 4-dimension/8-question report with three
            # risks and seven refs, then spent ~140s on a worse fallback.
            {"dimensions": 4, "strengths": 2, "risks": 3,
             "questions": 4, "refs": 6}
        )
        sections: Dict[str, Dict[str, Any]] = {}
        call_count = 0

        async def run_sections(
                names: List[str], attempt: int,
        ) -> Dict[str, str]:
            nonlocal call_count
            if not names:
                return {}
            remaining_call_budget = max(0, int(max_calls) - call_count)
            names = names[:remaining_call_budget]
            if not names:
                return {}
            call_count += len(names)
            generated = await asyncio.gather(
                *(generate(name, specs[name], attempt) for name in names),
                return_exceptions=True)
            errors: Dict[str, str] = {}
            for name, result in zip(names, generated):
                if isinstance(result, BaseException):
                    errors[name] = str(result)[:240]
                    continue
                section, payload = result
                sections[section] = payload
            return errors

        async def backfill_short_question_section(
                *, allow_empty: bool = False) -> int:
            question_section = sections.get("question")
            risk_section = sections.get("risk")
            if not isinstance(risk_section, dict):
                return 0
            if not isinstance(question_section, dict):
                if not allow_empty:
                    return 0
                question_section = {"interviewQuestions": []}
                sections["question"] = question_section
            original_count = len(
                question_section.get("interviewQuestions") or
                question_section.get("interviewProbes") or [])
            combined = {**risk_section, **question_section}
            added = _backfill_interview_questions_from_risks(
                combined, minimum=minimums["questions"],
                allow_empty=allow_empty)
            if not added:
                return 0
            question_section["interviewQuestions"] = list(
                combined["interviewQuestions"])
            question_section["interviewProbes"] = list(
                combined["interviewProbes"])
            await self.emitter.emit(
                "run.progress", agent_id="ReportAgent", payload={
                    "stage": "parallel_report_question_backfill",
                    "reason": (
                        "question_generation_failed_twice" if allow_empty
                        else "provider_schema_min_items_ignored"),
                    "modelQuestionCount": original_count,
                    "backfilledQuestionCount": added,
                    "finalQuestionCount": len(
                        question_section["interviewQuestions"]),
                    "occurredAt": _utc_now(),
                })
            return added

        def assess() -> Tuple[
                Optional[Dict[str, Any]], Dict[str, int],
                Dict[str, Dict[str, int]], set]:
            merged = {
                **sections.get("score", {}),
                **sections.get("risk", {}),
                **sections.get("question", {}),
            }
            merged["interviewProbes"] = list(
                merged.get("interviewQuestions") or [])
            validated = self._validate_structured_report(merged)
            if not validated:
                return None, {}, {}, set(specs)
            groups = {
                "score": list(validated.get("dimensions") or []),
                "risk": list(validated.get("risks") or []),
                "question": list(
                    validated.get("interviewQuestions") or []),
            }
            refs_by_section = {
                name: sum(
                    len(item.get("evidenceRefs") or [])
                    for item in items if isinstance(item, dict))
                for name, items in groups.items()
            }
            refs = sum(refs_by_section.values())
            observed = {
                "dimensions": len(groups["score"]),
                "strengths": len(validated.get("strengths") or []),
                "risks": len(groups["risk"]),
                "questions": len(groups["question"]),
                "refs": refs,
            }
            high_risk_topics = sum(
                str(item.get("severity") or "").upper() == "HIGH"
                for item in groups["risk"] if isinstance(item, dict))
            high_priority_questions = sum(
                str(item.get("priority") or "").upper() == "HIGH"
                for item in groups["question"] if isinstance(item, dict))
            observed["highRiskTopics"] = high_risk_topics
            observed["highPriorityQuestions"] = high_priority_questions
            observed["questionBudgetCap"] = 8
            # Priority labels are an observability proxy, not semantic proof
            # that every risk topic is covered.  Do not discard an otherwise
            # grounded report or trigger a full fallback from this count.
            observed["highRiskPriorityProxySatisfied"] = int(
                high_priority_questions >= min(high_risk_topics, 8))
            weak = {
                key: {"actual": observed[key], "minimum": minimum}
                for key, minimum in minimums.items()
                if observed[key] < minimum
            }
            retry = set()
            if "dimensions" in weak or "strengths" in weak:
                retry.add("score")
            if "risks" in weak:
                retry.add("risk")
            if "questions" in weak:
                retry.add("question")
            if "refs" in weak:
                per_section_floor = 1 if is_sparse_resume else 2
                retry.update(
                    name for name, count in refs_by_section.items()
                    if count < per_section_floor)
                if not retry:
                    retry.update(specs)
            return validated, observed, weak, retry

        errors = await run_sections(list(specs), attempt=1)
        await backfill_short_question_section()
        if errors:
            # A missing section makes whole-report validation fail, but the
            # other concurrently generated sections may already be valid.
            # Retry only malformed/failed sections; retrying all three caused
            # a 3-call storm without improving the successful outputs.
            validated, observed, weak = None, {}, {}
            retry_sections = set(errors)
        else:
            validated, observed, weak, retry_sections = assess()
        if retry_sections:
            await self.emitter.emit(
                "run.progress", agent_id="ReportAgent", payload={
                    "stage": "parallel_report_retry",
                    "reason": (
                        "section_generation_failed" if errors
                        else "section_quality_floor_failed"),
                    "retrySections": sorted(retry_sections),
                    "errors": errors,
                    "qualityFloor": weak,
                    "occurredAt": _utc_now(),
                })
            retry_errors = await run_sections(
                sorted(retry_sections), attempt=2)
            recovered_questions = await backfill_short_question_section(
                allow_empty="question" in retry_errors)
            if recovered_questions and len(
                    (sections.get("question") or {}).get(
                        "interviewQuestions") or []) >= minimums["questions"]:
                retry_errors.pop("question", None)
            validated, observed, weak, _ = assess()
            errors = retry_errors
        if errors or weak or not validated:
            await self.emitter.emit(
                "run.progress", agent_id="ReportAgent", payload={
                    "stage": "parallel_report_fallback",
                    "reason": (
                        "section_generation_failed" if errors
                        else "section_quality_floor_failed"),
                    "errors": errors,
                    "qualityFloor": weak,
                    "occurredAt": _utc_now(),
                })
            return None, call_count
        await self.emitter.emit(
            "run.progress", agent_id="ReportAgent", payload={
                "stage": "parallel_report_merged",
                "reportSections": list(specs),
                "quality": observed,
                "occurredAt": _utc_now(),
            })
        return {
            "summary": str(validated.get("summary") or ""),
            "report": validated,
        }, call_count

    async def _reject_native_proposal(self, agent_id: str, tool: str,
                                      proposed: LlmToolCall, reason: str, *,
                                      source: str,
                                      mcp_server: Optional[str] = None,
                                      trace_context: Optional[Dict[str, Any]] = None
                                      ) -> Dict[str, Any]:
        now = _utc_now()
        await self.emitter.emit("tool.failed", agent_id=agent_id,
                                tool_name=tool, payload={
                                    **(trace_context or {}),
                                    "toolCallId": proposed.tool_call_id,
                                    "lifecycleStage": "ERROR",
                                    "source": source,
                                    "mcpServer": mcp_server,
                                    "toolName": tool,
                                    "modelName": proposed.name,
                                    "arguments": proposed.arguments,
                                    "error": reason,
                                    "outcome": "REJECTED",
                                    "occurredAt": now,
                                    "startedAt": now,
                                    "endedAt": now,
                                })
        return {
            "success": False,
            "status": "REJECTED",
            "error": reason,
        }

    async def _ack_duplicate_native_proposal(
            self, agent_id: str, tool: str, proposed: LlmToolCall, *,
            trace_context: Optional[Dict[str, Any]] = None
            ) -> Dict[str, Any]:
        """Acknowledge a deterministic pre-step the model asked to repeat.

        The observation is already present in MODEL_INPUT, so executing again
        adds latency while emitting tool.failed falsely lowers RAG success.
        Keep a traceable suppression event and instruct the next turn to use
        the attached result.
        """
        now = _utc_now()
        await self.emitter.emit(
            "tool.progress", agent_id=agent_id, tool_name=tool, payload={
                **(trace_context or {}),
                "toolCallId": proposed.tool_call_id,
                "lifecycleStage": "DUPLICATE_SUPPRESSED",
                "source": "builtin",
                "toolName": tool,
                "modelName": proposed.name,
                "arguments": proposed.arguments,
                "outcome": "SKIPPED_DUPLICATE",
                "occurredAt": now,
            })
        return {
            "success": True,
            "status": "SKIPPED_DUPLICATE",
            "result": (
                "该检索已在本 Agent 的确定性预处理阶段成功执行；"
                "请直接使用当前上下文中的既有工具观察生成结论。"),
        }

    async def _execute_skill_proposal(
            self, *, agent_id: str, tool: str, proposed: LlmToolCall,
            selected_skills: List[Any],
            loaded_skills: Dict[str, Any],
            loaded_skill_call_ids: Dict[str, str],
            trace_context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        if self.budget.tool_calls >= self.tools.max_tool_calls_run:
            return await self._reject_native_proposal(
                agent_id, tool, proposed, "run tool budget exhausted",
                source="skill", trace_context=trace_context)
        started_at = _utc_now()
        self.budget.tool_calls += 1
        await self.emitter.emit("tool.started", agent_id=agent_id,
                                tool_name=tool, payload={
                                    **(trace_context or {}),
                                    "toolCallId": proposed.tool_call_id,
                                    "lifecycleStage": "EXECUTION_STARTED",
                                    "source": "skill",
                                    "kind": "internal",
                                    "toolName": tool,
                                    "modelName": proposed.name,
                                    "arguments": proposed.arguments,
                                    "occurredAt": started_at,
                                    "startedAt": started_at,
                                })
        requested_skill_ref = str(
            proposed.arguments.get("skill_id") or "").strip()
        skill_id = requested_skill_ref
        try:
            selected_ids = {skill.skill_id for skill in selected_skills}
            # The rendered loaded header contains ``id@version#hash`` for
            # provenance. Providers occasionally echo that full reference as
            # the tool argument even though the schema asks for the canonical
            # ID. Accept only a reference that resolves to an already-selected
            # Skill, then normalize the trace/result to the canonical ID.
            if skill_id not in selected_ids:
                skill_id = next((
                    candidate for candidate in selected_ids
                    if requested_skill_ref.startswith(f"{candidate}@")
                ), requested_skill_ref)
            if not skill_id or skill_id not in selected_ids:
                raise KeyError(
                    "skill not selected for this agent: "
                    f"{requested_skill_ref or '<empty>'}")
            if tool == "load_skill":
                loaded = loaded_skills.get(skill_id)
                already_loaded = loaded is not None
                if loaded is None:
                    loaded = default_skill_manager.load(skill_id)
                    loaded_skills[skill_id] = loaded
                    loaded_skill_call_ids[skill_id] = proposed.tool_call_id
                    await default_skill_manager.emit_loaded(
                        self.emitter, agent_id, loaded,
                        tool_call_id=proposed.tool_call_id,
                        reason="llm_native_tool_call",
                        round_id=(trace_context or {}).get("roundId"))
                result: Dict[str, Any] = {
                    "success": True,
                    "loaded": True,
                    "alreadyLoaded": already_loaded,
                    "skillId": loaded.skill_id,
                    "skillVersion": loaded.version,
                    "resources": list(loaded.resource_paths),
                    "instructionsInjectedNextTurn": True,
                }
            else:
                loaded = loaded_skills.get(skill_id)
                if loaded is None:
                    raise KeyError(
                        f"load_skill must run before read_skill_resource: {skill_id}")
                path = str(proposed.arguments.get("path") or "").strip()
                content = default_skill_manager.read_resource(skill_id, path)
                result = {
                    "success": True,
                    "loaded": True,
                    "skillId": loaded.skill_id,
                    "skillVersion": loaded.version,
                    "path": path,
                    "content": content,
                }
                await self.emitter.emit(
                    "skill.loaded", agent_id=agent_id,
                    tool_name=skill_id, payload={
                        **(trace_context or {}),
                        "toolCallId": proposed.tool_call_id,
                        "skillId": loaded.skill_id,
                        "skillVersion": loaded.version,
                        "skillHash": loaded.hash,
                        "agentId": agent_id,
                        "lifecycleStage": "RESOURCE_LOADED",
                        "reason": "llm_requested_reference",
                        "resourcePath": path,
                        "disclosureState": "RESOURCE",
                        "occurredAt": _utc_now(),
                    })
            ended_at = _utc_now()
            await self.emitter.emit("tool.completed", agent_id=agent_id,
                                    tool_name=tool, payload={
                                        **(trace_context or {}),
                                        "toolCallId": proposed.tool_call_id,
                                        "lifecycleStage": "RESULT",
                                        "source": "skill",
                                        "kind": "internal",
                                        "toolName": tool,
                                        "modelName": proposed.name,
                                        "arguments": proposed.arguments,
                                        "resultPreview": {
                                            k: v for k, v in result.items()
                                            if k != "content"},
                                        "occurredAt": ended_at,
                                        "startedAt": started_at,
                                        "endedAt": ended_at,
                                    })
            return result
        except Exception as exc:  # noqa: BLE001
            ended_at = _utc_now()
            error = f"{type(exc).__name__}: {exc}"[:500]
            await self.emitter.emit("tool.failed", agent_id=agent_id,
                                    tool_name=tool, payload={
                                        **(trace_context or {}),
                                        "toolCallId": proposed.tool_call_id,
                                        "lifecycleStage": "ERROR",
                                        "source": "skill",
                                        "kind": "internal",
                                        "toolName": tool,
                                        "modelName": proposed.name,
                                        "arguments": proposed.arguments,
                                        "error": error,
                                        "occurredAt": ended_at,
                                        "startedAt": started_at,
                                        "endedAt": ended_at,
                                    })
            await self.emitter.emit("skill.failed", agent_id=agent_id,
                                    tool_name=skill_id or tool, payload={
                                        **(trace_context or {}),
                                        "toolCallId": proposed.tool_call_id,
                                        "skillId": skill_id or None,
                                        "skillVersion": None,
                                        "agentId": agent_id,
                                        "lifecycleStage": "ERROR",
                                        "reason": error,
                                        "occurredAt": ended_at,
                                    })
            return {"success": False, "status": "FAILED", "error": error}

    async def _emit_unapplied_skills(
            self, agent_id: str, selected: List[Any],
            loaded: Dict[str, Any], applied_ids: set, *,
            reason: str) -> None:
        for metadata in selected:
            if metadata.skill_id in applied_ids:
                continue
            skill = loaded.get(metadata.skill_id, metadata)
            detail = (
                "loaded_without_subsequent_model_turn"
                if metadata.skill_id in loaded
                else reason)
            try:
                await default_skill_manager.emit_skipped(
                    self.emitter, agent_id, skill, reason=detail)
            except Exception as exc:  # noqa: BLE001
                logger.debug("skill skipped event emit failed: %s", exc)

    async def _record_tool_success(self, agent_id: str, tool: str,
                                   args: Dict[str, Any], result: Any, *,
                                   tool_call_id: str,
                                   duration_ms: Optional[int] = None) -> None:
        """Persist only successful tool material; failures never become evidence."""
        if isinstance(result, dict) and result.get("success") is False:
            return
        if tool == "calculate_jd_coverage":
            self.state.put_artifact("jdCoverage", result)
        elif tool == "check_timeline":
            self.state.put_artifact("timelineCheck", result)
        elif tool == "verify_report_evidence":
            self._apply_verification(result)
        elif tool == "parse_resume":
            self.state.put_artifact("parsedResume", result)
            facts = self._resume_facts_from_parse(result)
            if facts:
                self.state.put_artifact("resumeFacts", facts)
        defn = self.tools.definitions.get(tool)
        if defn is not None and defn.kind == "mcp":
            server = str(defn.mcp_server or "")
            result_source_urls = _collect_source_urls(result)
            request_source_urls = _collect_source_urls(
                args.get("url"), args.get("urls"))
            source_urls = list(result_source_urls)
            if server == "fetch":
                # For fetch calls the URL is the actual requested document.
                # Search-query text, however, is not evidence provenance.
                source_urls = list(dict.fromkeys(
                    request_source_urls + result_source_urls))
            base_entry = {
                "toolCallId": tool_call_id,
                "tool": tool,
                "mcpServer": server,
                "status": "SUCCEEDED",
                "query": str(args.get("query") or "")[:500],
                "url": str(args.get("url") or "")[:1000],
                "repository": str(
                    args.get("repoName") or args.get("repository")
                    or args.get("repo") or "")[:300],
                "sourceUrls": source_urls,
                "result": result,
                "collectedAt": _utc_now(),
            }

            # Documentation MCPs inform reasoning but are never candidate
            # facts. DeepWiki additionally requires the locally injected,
            # candidate-declared repository binding from ToolExecutor.
            if server in {"deepwiki", "context7"}:
                if server == "deepwiki":
                    policy = (
                        result.get("evidencePolicy")
                        if isinstance(result, dict) else None)
                    binding_url = str(
                        policy.get("sourceUrl") or ""
                    ) if isinstance(policy, dict) else ""
                    if (
                            not isinstance(policy, dict)
                            or policy.get("evidenceUse") != "context_only"
                            or policy.get("candidateFactEligible") is not False
                            or not re.match(
                                r"^https://github\.com/[^/]+/[^/]+/?$",
                                binding_url, re.IGNORECASE)):
                        logger.warning(
                            "DeepWiki result omitted from state: invalid "
                            "candidate repository binding")
                        return
                    source_urls = [binding_url]
                    base_entry["sourceUrls"] = source_urls
                    base_entry["repository"] = str(
                        policy.get("repository") or "")[:300]
                base_entry.update({
                    "evidenceUse": "context_only",
                    "candidateFactEligible": False,
                    "sourceBacked": bool(source_urls),
                })
                # Append one fresh item through the state boundary. Reusing
                # the canonical list and then writing that same list back
                # makes apply_artifacts iterate and append to one object,
                # which can grow forever on the second successful MCP call.
                self.state.apply_artifacts({"mcpContext": [base_entry]},
                                           by_agent=agent_id)
                return

            # Public-web output without an HTTP(S) source may still be shown to
            # the calling model, but it cannot enter the evidence ledger.
            if not source_urls:
                logger.info(
                    "MCP result omitted from mcpEvidence: %s returned no source URL",
                    tool)
                return

            base_entry.update({
                "evidenceUse": "raw_source_for_calibration",
                "candidateFactEligible": False,
                "requiresCalibration": True,
                "sourceBacked": True,
            })
            self.state.apply_artifacts({"mcpEvidence": [base_entry]},
                                       by_agent=agent_id)

    @staticmethod
    def _with_measured_retrieval_latency(
            result: Any, duration_ms: Optional[int]) -> Any:
        """Fill only missing RAG timing from the measured tool boundary."""
        if not isinstance(result, dict) or duration_ms is None:
            return result
        latency = result.get("_latency")
        latency = dict(latency) if isinstance(latency, dict) else {}
        latency.setdefault("retrieval_ms", float(duration_ms))
        latency.setdefault("total_ms", float(duration_ms))
        return {**result, "_latency": latency}

    @staticmethod
    def _parse_decision(raw: str) -> Tuple[Optional[Dict[str, Any]], str]:
        """Layered JSON guarantee, application side: extract the object, then
        validate against the AgentDecision schema. Returns (decision, error);
        decision is None when either layer fails, with the exact violation in
        error so the repair call can quote it back to the model."""
        candidate = extract_json_object(raw)
        if not candidate:
            return None, "输出中找不到可解析的 JSON 对象"
        # Some OpenAI-compatible providers occasionally double-encode a
        # function argument field and return ``output`` as a JSON string.
        # Decode only a complete JSON object through the same bounded extractor
        # used for the outer response; arbitrary prose remains invalid.
        raw_output = candidate.get("output")
        if isinstance(raw_output, str):
            parsed_output = extract_json_object(raw_output)
            if not parsed_output:
                return None, "output 是字符串，但其中找不到合法 JSON 对象"
            candidate = dict(candidate)
            candidate["output"] = parsed_output
        try:
            validated = AgentDecision.model_validate(candidate)
        except Exception as exc:  # pydantic.ValidationError
            return None, str(exc)
        decision = validated.model_dump()
        # Downstream expects plain dicts for tool calls.
        decision["toolCalls"] = [
            {"tool": c["tool"], "arguments": c["arguments"]}
            for c in decision.get("toolCalls", [])]
        return decision, ""

    def _report_decision_schema_error(
            self, decision: Dict[str, Any]) -> str:
        """Validate the score-contract payload before accepting ``done=true``.

        ``AgentDecision`` intentionally permits an empty output for specialist
        agents. ReportAgent cannot use that looser contract: accepting an empty
        or malformed report here would end the loop and waste the reserved
        terminal repair turn, eventually producing ``no_terminal_answer``.
        """
        output = decision.get("output")
        if not isinstance(output, dict):
            return (
                "ReportAgent structured report 缺失，"
                "必须提交结构化 report")
        report = output.get("report")
        if not isinstance(report, dict):
            return "ReportAgent structured report 缺失或不是 JSON 对象"

        required = (
            "recommendation",
            "dimensions",
            "strengths",
            "risks",
            "interviewProbes",
            "dataQuality",
        )
        missing = [field for field in required if field not in report]
        if missing:
            return (
                "ReportAgent structured report 缺少必填字段: "
                + ", ".join(missing)
            )
        for field in (
                "dimensions", "strengths", "risks", "interviewProbes"):
            if not isinstance(report.get(field), list):
                return (
                    f"ReportAgent structured report 字段 {field} 必须是数组")

        validated = self._validate_structured_report(report)
        if not validated:
            return "ReportAgent structured report 未通过运行时语义校验"
        if report.get("dimensions") and not validated.get("dimensions"):
            return "ReportAgent structured report 的 dimensions 全部无效"
        return ""

    def _build_output(self, definition: AgentDefinition,
                      raw_output: Optional[Dict[str, Any]],
                      tool_results_block: str) -> AgentOutput:
        raw_output = raw_output if isinstance(raw_output, dict) else {}
        summary = str(raw_output.get("summary") or "")
        claims = [c for c in (raw_output.get("claims") or []) if isinstance(c, dict)]
        evidence = [e for e in (raw_output.get("evidence") or []) if isinstance(e, dict)]
        output = AgentOutput(
            agentId=definition.agent_id,
            type=definition.output_type,
            claims=claims,
            evidence=evidence,
            source="llm+tools" if tool_results_block else "llm",
            dependencies=[],
            summary=summary[:500])
        if definition.agent_id in TERMINAL_AGENTS:
            if definition.agent_id == "ReportAgent":
                report = raw_output.get("report")
                if isinstance(report, dict):
                    if summary and not report.get("summary"):
                        report = {**report, "summary": summary}
                    validated = self._apply_evidence_gate(
                        self._validate_structured_report(report))
                    if validated:
                        self.state.put_artifact("finalReport", validated)
                        self.final_answer = self.render_report(validated)
                        return output
                # followup/quick_answer 可降级为短答；评估类必须走结构化契约。
                if not self._requires_score_contract():
                    answer = raw_output.get("answer") or summary
                    if answer:
                        self.final_answer = str(answer)
                return output
            report = raw_output.get("report")
            if isinstance(report, dict):
                validated = self._apply_evidence_gate(
                    self._validate_structured_report(report))
                if validated:
                    self.state.put_artifact("finalReport", validated)
            answer = raw_output.get("answer") or raw_output.get("markdown") or summary
            if not answer and isinstance(report, dict):
                answer = json.dumps(report, ensure_ascii=False, indent=2)
            if isinstance(answer, dict):
                answer = json.dumps(answer, ensure_ascii=False, indent=2)
            if answer:
                self.final_answer = str(answer)
        return output

    async def _emit_rag_metrics(
            self, agent_id: str, result: Any, query: str = "", *,
            retrieval_name: str, retrieval_id: str,
            requested_k: Any = None) -> None:
        """Emit measured multi-stage retrieval telemetry.

        Missing provider counters remain ``None``/``NOT_COLLECTED``; the
        runtime never invents recall, latency stages, cache hits or scores.
        """
        if not isinstance(result, dict):
            return
        chunks: List[Any] = []
        # Prefer structured ranked items when the provider also keeps legacy
        # string chunks for model compatibility.  This preserves per-result
        # scores and current-candidate provenance in Ops.
        for key in ("items", "results", "hits", "chunks"):
            if isinstance(result.get(key), list):
                chunks = result[key]
                break
        returned_k = len(chunks)
        queries_used = result.get("queriesUsed") or ([query] if query else [])
        score_values: List[float] = []
        doc_ids = set()
        normalized_chunks = []
        for chunk in chunks[:10]:
            if isinstance(chunk, dict):
                raw_score = next((
                    chunk.get(key) for key in (
                        "finalScore", "rerankScore", "retrievalScore",
                        "relevanceScore", "similarity", "vectorScore",
                        "bm25Score", "rrfScore", "score")
                    if isinstance(chunk.get(key), (int, float))), None)
                score = float(raw_score) if raw_score is not None else None
                if score is not None:
                    score_values.append(score)
                doc_id = (chunk.get("docId") or chunk.get("documentId")
                          or chunk.get("jdId") or chunk.get("id") or "")
                if doc_id:
                    doc_ids.add(doc_id)
                content = chunk.get("content") or chunk.get("text") or chunk.get("pageContent") or ""
                normalized_chunks.append({
                    "chunkId": (chunk.get("chunkId") or chunk.get("jdId")
                                or chunk.get("id")),
                    "docId": doc_id or None,
                    "title": chunk.get("title"),
                    "source": chunk.get("source") or chunk.get("sourceType"),
                    "uri": chunk.get("uri") or chunk.get("url"),
                    "finalScore": round(score, 4) if score is not None else None,
                    "retrievalScore": chunk.get("retrievalScore"),
                    "rerankScore": chunk.get("rerankScore"),
                    "rrfScore": chunk.get("rrfScore"),
                    "content": str(content)[:800] if content else None,
                    "provenance": chunk.get("provenance") or {
                        "indexName": chunk.get("index")
                        or chunk.get("collection"),
                        "sourceId": chunk.get("sourceId"),
                    },
                })
            elif isinstance(chunk, str) and chunk.strip():
                # ResumeRagService returns text chunks plus topScore /
                # rerankScores at the response root. Preserve the snippets so
                # Ops does not show hitCount>0 together with an empty result.
                normalized_chunks.append({
                    "chunkId": None,
                    "docId": "current_resume",
                    "title": "当前简历片段",
                    "source": "resume_text",
                    "uri": None,
                    "finalScore": None,
                    "retrievalScore": None,
                    "rerankScore": None,
                    "rrfScore": None,
                    "content": chunk[:800],
                    "provenance": {
                        "indexName": "current_resume",
                        "sourceId": self.request.runId,
                    },
                })
                doc_ids.add("current_resume")
        score_metric = "chunk_final_score"
        if not score_values:
            rerank_scores = result.get("rerankScores")
            if isinstance(rerank_scores, list):
                score_values.extend(
                    float(value) for value in rerank_scores
                    if isinstance(value, (int, float)))
                if score_values:
                    score_metric = "resume_rerank_usefulness"
            if not score_values:
                root_score = (
                    result.get("usefulnessScore")
                    if isinstance(result.get("usefulnessScore"), (int, float))
                    else result.get("topScore"))
                if isinstance(root_score, (int, float)):
                    score_values.append(float(root_score))
                    score_metric = (
                        "resume_rerank_usefulness"
                        if result.get("usefulnessScore") is not None
                        else "retrieval_top_score")
        latency = result.get("_latency") or {}
        counters = result.get("counters") if isinstance(
            result.get("counters"), dict) else {}
        candidate_count = (
            result.get("candidateCount")
            if isinstance(result.get("candidateCount"), int)
            else counters.get("candidateCount")
            if isinstance(counters.get("candidateCount"), int)
            else result.get("hitCount")
            if isinstance(result.get("hitCount"), int)
            else None)
        ended_at = str(result.get("retrievedAt") or _utc_now())
        rerank_applied = (
            result.get("rerankApplied")
            if isinstance(result.get("rerankApplied"), bool)
            else result.get("agenticRerank")
            if isinstance(result.get("agenticRerank"), bool)
            else None)
        payload = {
            "retrievalId": retrieval_id,
            "retrievalName": retrieval_name,
            "query": query[:200] if query else "",
            "requestedK": int(requested_k) if isinstance(
                requested_k, (int, float)) else None,
            "returnedK": returned_k,
            "candidateCount": candidate_count,
            "lexicalHits": result.get("lexicalHits")
            if isinstance(result.get("lexicalHits"), int) else None,
            "vectorHits": result.get("vectorHits")
            if isinstance(result.get("vectorHits"), int) else None,
            "filteredCount": result.get("filteredCount")
            if isinstance(result.get("filteredCount"), int) else None,
            "droppedCount": result.get("droppedCount")
            if isinstance(result.get("droppedCount"), int) else None,
            "deduplicatedCount": result.get("deduplicatedCount")
            if isinstance(result.get("deduplicatedCount"), int) else None,
            "hitCount": returned_k,
            "queriesUsed": queries_used[:3],
            "queryRewriteMode": result.get("queryRewriteMode"),
            "scores": {
                "top": round(max(score_values), 4) if score_values else None,
                "min": round(min(score_values), 4) if score_values else None,
                "mean": round(sum(score_values) / len(score_values), 4)
                if score_values else None,
                "collectedCount": len(score_values),
                "metric": score_metric if score_values else None,
            },
            "uniqueDocuments": len(doc_ids),
            "docIds": list(doc_ids)[:5],
            "strategy": result.get("strategy"),
            "fusionStrategy": result.get("fusion"),
            "indexName": result.get("indexName"),
            "source": result.get("source") or result.get("backend"),
            "chunks": normalized_chunks,
            "stages": {
                "queryRewriteMs": latency.get("rewrite_ms"),
                "embeddingMs": latency.get("embedding_ms"),
                "retrievalMs": (
                    latency.get("retrieval_ms")
                    if latency.get("retrieval_ms") is not None
                    else result.get("latencyMs")),
                "embeddingRetrievalMs": latency.get("embedding_search_ms"),
                "fusionMs": latency.get("fusion_ms"),
                "rerankMs": latency.get("rerank_ms"),
                "totalMs": latency.get("total_ms"),
            },
            "counters": counters or None,
            "rerankApplied": rerank_applied,
            "rerankProvider": result.get("rerankProvider"),
            "rerankBeforeTopScore": result.get("rerankBeforeTopScore"),
            "rerankAfterTopScore": result.get("rerankAfterTopScore"),
            "rerankLift": result.get("rerankLift"),
            "rerankBeforeTopChunkId": result.get(
                "rerankBeforeTopChunkId"),
            "rerankAfterTopChunkId": result.get(
                "rerankAfterTopChunkId"),
            "rerankMovedCount": result.get("rerankMovedCount"),
            "cacheHit": result.get("cacheHit")
            if isinstance(result.get("cacheHit"), bool) else None,
            "fallback": result.get("fallback")
            if isinstance(result.get("fallback"), bool) else None,
            "fallbackStage": result.get("fallbackStage"),
            "fallbackChain": result.get("fallbackChain")
            if isinstance(result.get("fallbackChain"), list) else None,
            "degraded": result.get("degraded")
            if isinstance(result.get("degraded"), bool) else None,
            "reason": result.get("reason"),
            "error": result.get("error"),
            "retrievedAt": result.get("retrievedAt"),
            "occurredAt": ended_at,
            "startedAt": result.get("_startedAt"),
            "endedAt": ended_at,
        }
        await self.emitter.emit(
            "retrieval.completed", agent_id=agent_id,
            tool_name=f"retrieval.{retrieval_name}", payload=payload)

    def _store_jd_match_artifacts(self, result: Any) -> None:
        """Persist hybrid JD matches + effectiveJd for Tech coverage / sync."""
        if not isinstance(result, dict):
            return
        items = result.get("items") if isinstance(result.get("items"), list) else None
        if items is None and isinstance(result.get("jdMatches"), list):
            items = result["jdMatches"]
        if items is not None:
            self.state.put_artifact("jdMatches", items)
        effective = result.get("effectiveJd")
        if isinstance(effective, str) and effective.strip():
            self.state.put_artifact("effectiveJd", effective.strip())
        elif items:
            top = items[0] if isinstance(items[0], dict) else {}
            title = str(top.get("title") or "").strip()
            reasons = top.get("matchReasons") or []
            lines = [f"岗位：{title}"] if title else []
            for reason in reasons[:6]:
                if reason:
                    lines.append(f"- {reason}")
            if lines:
                self.state.put_artifact("effectiveJd", "\n".join(lines))

    def _resume_facts_from_parse(self, parsed: Any) -> Optional[Dict[str, Any]]:
        """Map deterministic parse_resume output into resumeFacts artifact.

        Always returns facts when parse succeeded — downstream agents MUST
        have material to work with even for short/unstructured resumes.
        Always includes rawExcerpt so agents can read the original text.
        """
        if not isinstance(parsed, dict) or not parsed.get("success"):
            return None
        sections = parsed.get("sections") if isinstance(parsed.get("sections"), dict) else {}
        skills = list(parsed.get("skills") or [])
        projects = list(parsed.get("projectNames") or [])
        experiences = list(sections.get("experience") or [])[:20]
        education = list(sections.get("education") or [])[:12]
        completeness = 0
        if skills:
            completeness += 1
        if projects or sections.get("projects"):
            completeness += 1
        if experiences:
            completeness += 1
        if education:
            completeness += 1
        if parsed.get("timelinePeriods"):
            completeness += 1
        resume_text = (self.request.resumeText or "").strip()
        return {
            "rawExcerpt": resume_text[:3000],
            "skills": skills[:40],
            "projects": [{"name": p} for p in projects[:12]],
            "experiences": [{"raw": e} for e in experiences],
            "education": [{"raw": e} for e in education],
            "contact": parsed.get("contact") or {},
            "timelinePeriods": parsed.get("timelinePeriods") or [],
            "source": "parse_resume_fast_path",
            "completeness": completeness,
            "confidence": float(parsed.get("confidence") or 0.8),
        }

    def _extract_candidate_urls(self, resume_text: str) -> List[str]:
        """Extract verifiable candidate URLs from resume (GitHub, LinkedIn, blog, portfolio)."""
        import re as _re
        url_pattern = _re.compile(
            r'https?://(?:github\.com|linkedin\.com|gitee\.com|gitcode\.com|'
            r'(?:blog\.)?csdn\.net|juejin\.cn|(?:zhuanlan\.)?zhihu\.com|'
            r'(?:www\.)?cnblogs\.com|segmentfault\.com|'
            r'blog\.\w+|[\w-]+\.github\.io|portfolio|[\w-]+\.vercel\.app)'
            r'[^\s\)\]<>\"\']*', _re.IGNORECASE)
        urls = url_pattern.findall(resume_text or "")
        seen = set()
        unique = []
        for u in urls:
            u_clean = u.rstrip(".,;:)")
            if u_clean not in seen:
                seen.add(u_clean)
                unique.append(u_clean)
        return unique

    async def _build_evidence_search_query_llm(self, resume: str, artifacts: Dict[str, Any],
                                                agent_id: str) -> str:
        """Compatibility wrapper with no hidden provider call.

        Native tool arguments are model-authored in the visible agent turn;
        legacy callers receive the deterministic fallback.
        """
        return self._build_evidence_search_query_fallback(resume, artifacts)

    def _build_evidence_search_query_fallback(self, resume: str, artifacts: Dict[str, Any]) -> str:
        """Regex fallback when LLM unavailable."""
        import re as _re
        facts = artifacts.get("resumeFacts") or {}
        parts = []
        if isinstance(facts, dict):
            experiences = facts.get("experiences") or []
            if experiences and isinstance(experiences, list):
                exp = experiences[0] if isinstance(experiences[0], dict) else {}
                company = exp.get("company") or ""
                role = exp.get("role") or exp.get("title") or ""
                if company and len(company) >= 2:
                    parts.append(company)
                if role:
                    parts.append(role[:20])
            skills = facts.get("skills") or []
            if isinstance(skills, list) and skills:
                top_skills = [s for s in skills[:5]
                              if isinstance(s, str) and len(s) >= 2]
                parts.extend(top_skills[:3])
        if not parts:
            companies = _re.findall(
                r"(字节跳动|阿里巴巴|腾讯|美团|百度|京东|华为|快手|小红书|"
                r"拼多多|网易|滴滴|蚂蚁|[\u4e00-\u9fa5]{2,6}(?:科技|网络|公司))",
                resume)
            if companies:
                parts.append(companies[0])
            tech = _re.findall(
                r"\b(Spring\s*Boot|Redis|Kafka|Go|Kubernetes|Docker|"
                r"MySQL|微服务|分布式|RAG|LLM|Agent)\b", resume, _re.IGNORECASE)
            parts.extend(tech[:3])
        if not parts:
            return ""
        return " ".join(parts[:4])

    def _build_evidence_search_query(self, resume: str, artifacts: Dict[str, Any]) -> str:
        """Sync wrapper - used by plan builder. Returns fallback only."""
        return self._build_evidence_search_query_fallback(resume, artifacts)

    def _fallback_search_query(self, resume: str) -> str:
        """Produce a claim-based search query — NEVER use candidate name."""
        import re as _re
        lines = (resume or "").strip().split("\n")[:30]
        companies = []
        techs = []
        metrics = []
        for line in lines:
            clean = line.strip()
            if not clean:
                continue
            company_match = _re.search(
                r"(字节跳动|阿里巴巴|腾讯|美团|百度|京东|华为|快手|"
                r"[\u4e00-\u9fa5]{2,6}(?:科技|网络|信息|技术|集团|公司|互联网))",
                clean)
            if company_match:
                companies.append(company_match.group(1))
            tech_match = _re.findall(
                r"\b(Spring\s*Boot|Redis|Kafka|Go|Kubernetes|Docker|"
                r"MySQL|微服务|分布式|RAG|LLM|Flink|Spark|Vue|React)\b",
                clean, _re.IGNORECASE)
            techs.extend(tech_match[:2])
            metric_match = _re.search(
                r"(QPS|延迟|性能|吞吐|并发).{0,10}(提升|降低|优化).{0,8}\d+",
                clean)
            if metric_match:
                metrics.append(clean[:40])
        if companies and techs:
            return f"{companies[0]} {' '.join(techs[:2])} 技术实践"
        if techs:
            return f"{' '.join(techs[:3])} 最佳实践 架构"
        if companies:
            return f"{companies[0]} 后端开发 技术栈"
        return ""

    def _fallback_project_query(self, resume: str, artifacts: Dict[str, Any]) -> str:
        """Always produce a project search query from resume content."""
        import re as _re
        lines = (resume or "").strip().split("\n")
        for line in lines:
            proj_match = _re.search(
                r"(?:项目[名称]*[:：]\s*|(?:独立|个人|开源)项目\s*[:：]?\s*)(.{4,30})",
                line)
            if proj_match:
                return proj_match.group(1).strip()
        tech_keywords = _re.findall(
            r"\b(Spring\s*Boot|Spring\s*Cloud|Vue|React|Next\.?js|Django|"
            r"FastAPI|Kubernetes|Flink|Spark|TensorFlow|PyTorch|LangChain)\b",
            resume, _re.IGNORECASE)
        if tech_keywords:
            return f"{tech_keywords[0]} 项目 架构"
        companies = _re.findall(
            r"([\u4e00-\u9fa5]{2,8}(?:科技|网络|信息|技术|集团|公司|互联网))", resume)
        if companies:
            return f"{companies[0]} 技术项目 开发"
        return "软件开发项目 技术架构 实践"

    def _build_project_search_query(self, resume: str, artifacts: Dict[str, Any]) -> str:
        """Short semantic query from JD passages, never a technology whitelist."""
        focus = self._jd_focus_text(artifacts, "projectSegments")
        if not focus:
            focus = self._jd_focus_fallback(
                artifacts.get("effectiveJd") or self.request.jobDescription or "")
        return self._bounded_rag_query([
            "项目证据",
            f"岗位要求：{focus}" if focus else "",
            "关注：项目目标、个人职责、技术决策、架构难点、量化结果",
        ])

    def _maybe_skip_evidence_llm(self, tool_results_block: str) -> Optional[AgentOutput]:
        """Skip Evidence LLM when deterministic verify is clean and support is high.
        Never skip when MCP tools produced results (external verification happened)."""
        support = self.state.evidence_support_ratio()
        conflicts = self.state.artifact("conflicts") or []
        if conflicts:
            return None
        if support is None or support < 0.85:
            return None
        if "verify_report_evidence" not in tool_results_block:
            return None
        if "fetch.fetch" in tool_results_block:
            return None
        if "bing_cn.web_search" in tool_results_block:
            return None
        summary = f"确定性核验通过：支持率 {support:.2f}，无冲突，跳过 Evidence LLM"
        return AgentOutput(
            agentId="EvidenceAgent",
            type="evidence",
            claims=[],
            artifacts={"evidence": [{"text": summary, "verified": True,
                                     "byAgent": "EvidenceAgent", "fastPath": True}]},
            evidence=[{"text": summary, "verified": True,
                       "byAgent": "EvidenceAgent"}],
            source="tools",
            dependencies=[],
            summary=summary[:500])

    def _prepare_jd_requirements(self) -> None:
        """Deterministically normalize the one effective JD during preflight."""
        jd = (self.request.jobDescription or "").strip()
        effective = self.state.artifact("effectiveJd")
        if isinstance(effective, str) and effective.strip():
            jd = effective.strip()
        if not jd:
            return
        requirements = {"rawJd": jd, "source": "deterministic_preflight"}
        lines = [l.strip() for l in jd.replace("；", "\n").replace("、", "\n").split("\n") if l.strip()]
        must_have = [l for l in lines if any(k in l for k in ("要求", "必须", "精通", "熟悉", "年以上", "经验"))]
        nice_to_have = [l for l in lines if l not in must_have and len(l) > 4]
        requirements["mustHave"] = must_have[:10]
        requirements["niceToHave"] = nice_to_have[:8]
        requirements["title"] = lines[0] if lines else ""
        self.state.put_artifact("jdRequirements", requirements)

    async def _prepare_jd_focus(self) -> None:
        """Select Agent-specific JD passages once, before specialist RAG."""
        if self.state.artifact("jdFocus"):
            return
        # An explicitly supplied JD is authoritative. Only use a catalog match
        # when this Run did not contain one.
        jd = (self.request.jobDescription or "").strip()
        if not jd:
            effective = self.state.artifact("effectiveJd")
            jd = effective.strip() if isinstance(effective, str) else ""
        if not jd:
            return
        requirements = self.state.artifact("jdRequirements")
        if (self.request.jobDescription or "").strip():
            title = next((line.strip() for line in jd.splitlines()
                          if line.strip()), "")[:80]
        else:
            title = str(requirements.get("title") or "") \
                if isinstance(requirements, dict) else ""
        started = time.monotonic()
        try:
            focus = await java_jd_focus(
                jd, title, (self.request.jobCategory or "")[:40])
            if not focus:
                return
            self.state.put_artifact("jdFocus", focus)
            await self.emitter.emit("run.progress", payload={
                "stage": "jd_focus",
                "message": "JD语义选段完成",
                "strategy": focus.get("strategy"),
                "segmentCount": focus.get("segmentCount", 0),
                "techSegmentCount": len(focus.get("techSegments") or []),
                "projectSegmentCount": len(focus.get("projectSegments") or []),
                "cacheHit": bool(focus.get("cacheHit")),
                "fallbackUsed": bool(focus.get("fallbackUsed")),
                "durationMs": round((time.monotonic() - started) * 1000),
            })
        except Exception as exc:  # retrieval degrades to bounded raw-JD fallback
            logger.info("JD semantic focus unavailable: %s", exc)
            await self.emitter.emit("retrieval.failed", agent_id="CoordinatorAgent",
                                    tool_name="retrieval.jd_focus", payload={
                "error": str(exc)[:300],
                "durationMs": round((time.monotonic() - started) * 1000),
                "occurredAt": _utc_now(),
            })

    @staticmethod
    def render_report(report: Dict[str, Any]) -> str:
        """Deterministic Markdown from a validated structured report."""
        score = report.get("overallScore")
        recommendation = report.get("recommendation") or "NEED_MANUAL_REVIEW"
        quality = report.get("dataQuality") or "SUFFICIENT"
        sections: List[str] = []
        title_bits = []
        if isinstance(score, int):
            title_bits.append(f"综合评分 {score}")
        title_bits.append(f"建议 {recommendation}")
        sections.append("# 简历评估报告\n\n" + " · ".join(title_bits))
        if report.get("summary"):
            sections.append(f"## 结论\n\n{report['summary']}")
        dimensions = report.get("dimensions") or []
        if dimensions:
            lines = ["## 维度评分\n"]
            for dim in dimensions:
                if not isinstance(dim, dict):
                    continue
                name = dim.get("name", "")
                status = str(dim.get("status") or "").upper()
                dim_score = dim.get("score")
                rationale = dim.get("rationale") or ""
                if status == "UNASSESSED" or dim_score is None:
                    score_part = "未评估"
                else:
                    score_part = f"{dim_score} 分"
                    if status == "PARTIAL":
                        score_part += "（部分证据）"
                lines.append(f"- **{name}**：{score_part}"
                             + (f" — {rationale}" if rationale else ""))
                refs = dim.get("evidenceRefs") or []
                for ref in refs[:3]:
                    if isinstance(ref, dict) and ref.get("quote"):
                        loc = ""
                        if ref.get("lineStart") is not None:
                            end = ref.get("lineEnd") or ref.get("lineStart")
                            loc = f" L{ref['lineStart']}-{end}"
                        lines.append(
                            f"  - 证据[{ref.get('sourceType', '?')}{loc}]："
                            f"{str(ref['quote'])[:120]}")
            sections.append("\n".join(lines))
        strengths = [str(v) for v in (report.get("strengths") or []) if str(v).strip()]
        if strengths:
            sections.append("## 优势\n\n" + "\n".join(f"- {v}" for v in strengths))
        risks = report.get("risks") or []
        if risks:
            lines = ["## 候选人风险\n"]
            for risk in risks:
                if isinstance(risk, str):
                    lines.append(f"- {risk}")
                    continue
                if not isinstance(risk, dict):
                    continue
                sev = risk.get("severity") or "MEDIUM"
                claim = risk.get("claim") or ""
                impact = risk.get("impact") or ""
                lines.append(f"- **[{sev}]** {claim}"
                             + (f"（影响：{impact}）" if impact else ""))
                plan = risk.get("verificationPlan") or ""
                if plan:
                    lines.append(f"  - 核实：{plan}")
                for ref in (risk.get("evidenceRefs") or [])[:2]:
                    if isinstance(ref, dict) and ref.get("quote"):
                        lines.append(f"  - 证据：{str(ref['quote'])[:120]}")
            sections.append("\n".join(lines))
        probes = report.get("interviewProbes") or report.get("interviewQuestions") or []
        if probes:
            lines = ["## 面试追问\n"]
            for probe in probes:
                if isinstance(probe, str):
                    lines.append(f"- {probe}")
                    continue
                if not isinstance(probe, dict):
                    continue
                q = probe.get("question") or ""
                lines.append(f"- {q}")
                if probe.get("objective"):
                    lines.append(f"  - 考察点：{probe['objective']}")
                goods = [str(s) for s in (probe.get("goodSignals") or []) if str(s).strip()]
                if goods:
                    lines.append(f"  - 好信号：{'；'.join(goods[:3])}")
                flags = [str(s) for s in (probe.get("redFlags") or []) if str(s).strip()]
                if flags:
                    lines.append(f"  - 红旗：{'；'.join(flags[:3])}")
            sections.append("\n".join(lines))
        missing = [str(v) for v in (report.get("missingEvidence") or []) if str(v).strip()]
        if missing:
            sections.append("## 证据缺口\n\n" + "\n".join(f"- {v}" for v in missing))
        warnings = report.get("systemWarnings") or []
        if warnings:
            lines = ["## 系统告警\n"]
            for warn in warnings:
                if not isinstance(warn, dict):
                    continue
                code = warn.get("code") or "WARNING"
                msg = warn.get("message") or ""
                stage = warn.get("stage") or ""
                retry = "可重试" if warn.get("retryable") else "不可重试"
                lines.append(f"- **{code}**"
                             + (f"@{stage}" if stage else "")
                             + f"（{retry}）：{msg}")
            sections.append("\n".join(lines))
        if quality and quality != "SUFFICIENT":
            sections.append(f"## 数据质量\n\n- {quality}")
        return "\n\n".join(sections).strip()

    @staticmethod
    def _parse_source_refs(raw: Any) -> List[Dict[str, Any]]:
        refs: List[Dict[str, Any]] = []
        if not isinstance(raw, list):
            return refs
        allowed = {"RESUME", "JD", "KNOWLEDGE", "EXTERNAL"}
        _LINE_RE = re.compile(
            r"\[?(RESUME|JD|KNOWLEDGE|EXTERNAL)\s*L?(\d+)(?:-L?(\d+))?\]?\s*(.*)",
            re.I)
        for item in raw:
            if isinstance(item, str):
                text = item.strip()
                if not text:
                    continue
                m = _LINE_RE.match(text)
                if m:
                    refs.append({
                        "sourceType": m.group(1).upper(),
                        "sourceId": m.group(1).lower(),
                        "quote": (m.group(4) or text)[:400],
                        "lineStart": int(m.group(2)),
                        "lineEnd": int(m.group(3)) if m.group(3) else int(m.group(2)),
                    })
                else:
                    source = "RESUME" if "resume" in text.lower() or "简历" in text else "JD"
                    refs.append({
                        "sourceType": source,
                        "sourceId": source.lower(),
                        "quote": text[:400],
                    })
                continue
            if not isinstance(item, dict):
                continue
            source_type = str(item.get("sourceType") or "").strip().upper()
            quote = str(item.get("quote") or "").strip()
            source_id = str(item.get("sourceId") or "").strip()
            if source_type not in allowed or not quote or not source_id:
                continue
            entry: Dict[str, Any] = {
                "sourceType": source_type,
                "sourceId": source_id[:120],
                "quote": quote[:400],
            }
            for key in ("lineStart", "lineEnd"):
                val = item.get(key)
                if isinstance(val, (int, float)) and int(val) >= 0:
                    entry[key] = int(val)
            if item.get("uri"):
                entry["uri"] = str(item["uri"])[:300]
            refs.append(entry)
        return refs[:8]

    @staticmethod
    def _claim_has_control_plane_noise(claim: str) -> bool:
        text = claim or ""
        upper = text.upper()
        if any(code in upper for code in CONTROL_PLANE_ERROR_CODES):
            return True
        markers = (
            "CONTROL_PLANE", "ORPHANED_ON_RESTART", "RUNTIME_START_FAILED",
            "START_STUCK", "WORKER", "QUEUE_STUCK", "控制面",
        )
        return any(m in upper or m in text for m in markers)

    def _apply_evidence_gate(
            self, report: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """Enforce EvidenceAgent results on the terminal report.

        Missing source evidence is a reporting constraint, not a reason to
        rebuild the already executed plan.  Keep the model's report shape, but
        deterministically cap data quality and surface unsupported claims.
        """
        if not report or "EvidenceAgent" not in self.executed:
            return report

        out = dict(report)
        artifacts = self.state.artifacts()
        checked = [
            item for item in (artifacts.get("evidence") or [])
            if isinstance(item, dict) and item.get("verified") is not None
        ]
        unsupported = [item for item in checked if item.get("verified") is False]
        conflicts = [
            item for item in (artifacts.get("conflicts") or [])
            if isinstance(item, dict)
            and str(item.get("resolution") or "").lower() != "keep"
        ]
        support_ratio = self.state.evidence_support_ratio()

        ceiling: Optional[str] = None
        if not checked:
            ceiling = "INSUFFICIENT"
        elif unsupported or conflicts or support_ratio is None or support_ratio < 0.85:
            ceiling = "PARTIAL"

        if ceiling:
            rank = {"INSUFFICIENT": 0, "PARTIAL": 1, "SUFFICIENT": 2}
            current = str(out.get("dataQuality") or "SUFFICIENT").upper()
            if current not in rank or rank[current] > rank[ceiling]:
                out["dataQuality"] = ceiling
            if out.get("dataQuality") == "INSUFFICIENT":
                out.pop("overallScore", None)

        missing = [
            str(item)[:300] for item in (out.get("missingEvidence") or [])
            if str(item).strip()
        ]
        for item in unsupported + conflicts:
            text = str(
                item.get("text") or item.get("claim") or item.get("finding")
                or item.get("detail") or ""
            ).strip()
            if text and text not in missing:
                missing.append(text[:300])
        if missing:
            out["missingEvidence"] = missing[:12]
        return out

    @staticmethod
    def _validate_structured_report(report: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Validate structured report; overallScore is computed only when
        enough core dimensions are evidence-assessed (never from the model,
        never by inventing 0 for UNASSESSED)."""
        allowed_recommendations = {
            "HIRE", "INTERVIEW_RECOMMEND", "NEED_MANUAL_REVIEW", "NOT_RECOMMEND"}
        legacy_recommendations = {
            "STRONG_RECOMMEND": "HIRE",
            "RECOMMEND": "INTERVIEW_RECOMMEND",
            "MANUAL_REVIEW": "NEED_MANUAL_REVIEW",
            "REVIEW": "NEED_MANUAL_REVIEW",
        }
        out: Dict[str, Any] = {}
        recommendation = str(report.get("recommendation") or "").strip().upper()
        recommendation = legacy_recommendations.get(recommendation, recommendation)
        if recommendation in allowed_recommendations:
            out["recommendation"] = recommendation
        quality = str(report.get("dataQuality") or "SUFFICIENT").strip().upper()
        if quality not in {"SUFFICIENT", "PARTIAL", "INSUFFICIENT"}:
            quality = "SUFFICIENT"
        out["dataQuality"] = quality

        system_warnings: List[Dict[str, Any]] = []
        for warn in report.get("systemWarnings") or []:
            if not isinstance(warn, dict):
                continue
            code = str(warn.get("code") or "").strip()
            message = str(warn.get("message") or "").strip()
            if not code or not message:
                continue
            system_warnings.append({
                "code": code[:80],
                "stage": str(warn.get("stage") or "")[:80],
                "retryable": bool(warn.get("retryable", False)),
                "message": message[:400],
            })

        dimensions: List[Dict[str, Any]] = []
        assessed_core: List[Tuple[str, int]] = []
        for dim in report.get("dimensions") or []:
            if not isinstance(dim, dict) or not dim.get("name"):
                continue
            name = str(dim["name"])[:60]
            status = str(dim.get("status") or "").strip().upper()
            dim_score = dim.get("score")
            score_val: Optional[int] = None
            if isinstance(dim_score, (int, float)) and 0 <= float(dim_score) <= 100:
                score_val = int(round(float(dim_score)))

            if status not in {"ASSESSED", "UNASSESSED", "PARTIAL"}:
                status = "UNASSESSED" if score_val is None else "ASSESSED"

            # UNASSESSED must keep score=null — never coerce missing evidence to 0.
            if status == "UNASSESSED":
                score_val = None

            refs = RunExecutor._parse_source_refs(
                dim.get("evidenceRefs") or dim.get("evidence"))
            try:
                coverage = float(dim.get("evidenceCoverage", 0.0) or 0.0)
            except (TypeError, ValueError):
                coverage = 0.0
            if refs and coverage <= 0:
                coverage = min(1.0, 0.35 * len(refs))
            coverage = max(0.0, min(1.0, coverage))

            # Downgrade status when no evidence at all and no score.
            if status == "ASSESSED" and not refs and score_val is None:
                status = "UNASSESSED"
            elif status == "ASSESSED" and not refs:
                status = "PARTIAL"

            entry: Dict[str, Any] = {
                "name": name,
                "status": status,
                "evidenceCoverage": round(coverage, 3),
                "score": score_val,
            }
            if dim.get("rationale"):
                entry["rationale"] = str(dim["rationale"])[:300]
            if refs:
                entry["evidenceRefs"] = refs
            dimensions.append(entry)

            key = name.replace(" ", "").lower()
            # Allow dimensions with score + rationale to count toward
            # overallScore even without strict SourceRef format.
            has_rationale = bool(dim.get("rationale"))
            scorable = (status in {"ASSESSED", "PARTIAL"}
                        and score_val is not None
                        and (bool(refs) or has_rationale)
                        and key in _CORE_DIMENSION_KEYS)
            if scorable:
                assessed_core.append((name, score_val))

        if dimensions:
            out["dimensions"] = dimensions[:10]

        # overallScore when ≥2 core dimensions have scores (rationale or refs).
        if len(assessed_core) >= 2:
            weight_sum = 0.0
            weighted = 0.0
            for name, score in assessed_core:
                key = name.replace(" ", "").lower()
                weight = next(
                    (w for n, w in _DIMENSION_WEIGHTS.items()
                     if n.replace(" ", "").lower() == key),
                    None)
                if weight is None:
                    continue
                weighted += score * weight
                weight_sum += weight
            if weight_sum > 0:
                out["overallScore"] = int(round(weighted / weight_sum))
            else:
                out["overallScore"] = int(
                    round(sum(s for _, s in assessed_core) / len(assessed_core)))
        else:
            out.pop("overallScore", None)

        strengths = [str(v)[:300] for v in (report.get("strengths") or [])
                     if isinstance(v, (str, int, float)) and str(v).strip()]
        if strengths:
            out["strengths"] = strengths[:12]

        risks_out: List[Dict[str, Any]] = []
        for idx, risk in enumerate(report.get("risks") or []):
            # Legacy string risks lack evidence → reject (do not promote).
            if isinstance(risk, str):
                text = risk.strip()
                if not text:
                    continue
                if RunExecutor._claim_has_control_plane_noise(text):
                    system_warnings.append({
                        "code": "CONTROL_PLANE_IN_RISK_CLAIM",
                        "stage": "report_validation",
                        "retryable": False,
                        "message": f"控制面噪声已从候选人风险剔除：{text[:200]}",
                    })
                continue
            if not isinstance(risk, dict):
                continue
            claim = str(risk.get("claim") or risk.get("risk") or "").strip()
            if not claim:
                continue
            category = str(risk.get("category") or "CANDIDATE").strip().upper()
            if category in _PROCESS_DATA_CATEGORIES:
                system_warnings.append({
                    "code": str(risk.get("id") or category)[:80],
                    "stage": "report",
                    "retryable": bool(risk.get("retryable", False)),
                    "message": claim[:400],
                })
                continue
            if RunExecutor._claim_has_control_plane_noise(claim):
                system_warnings.append({
                    "code": "CONTROL_PLANE_IN_RISK_CLAIM",
                    "stage": "report_validation",
                    "retryable": False,
                    "message": f"控制面错误码不得进入候选人风险：{claim[:200]}",
                })
                continue
            refs = RunExecutor._parse_source_refs(risk.get("evidenceRefs"))
            severity = str(risk.get("severity") or "MEDIUM").strip().upper()
            if severity not in {"HIGH", "MEDIUM", "LOW"}:
                severity = "MEDIUM"
            entry = {
                "id": str(risk.get("id") or f"risk-{idx + 1}")[:60],
                "category": "CANDIDATE",
                "severity": severity,
                "claim": claim[:400],
                "impact": str(risk.get("impact") or "")[:300],
                "verificationPlan": str(risk.get("verificationPlan") or risk.get("verification") or "")[:300],
            }
            if refs:
                entry["evidenceRefs"] = refs
            conf = risk.get("confidence")
            if isinstance(conf, (int, float)):
                entry["confidence"] = max(0.0, min(1.0, float(conf)))
            risks_out.append(entry)
        if risks_out:
            out["risks"] = risks_out[:12]

        probes_raw = report.get("interviewProbes")
        if not isinstance(probes_raw, list) or not probes_raw:
            probes_raw = report.get("interviewQuestions") or []
        probes_out: List[Dict[str, Any]] = []
        for idx, probe in enumerate(probes_raw):
            if isinstance(probe, str):
                # Legacy bare questions lack evidence/signals → reject
                continue
            if not isinstance(probe, dict):
                continue
            question = str(probe.get("question") or "").strip()
            if not question:
                continue
            refs = RunExecutor._parse_source_refs(probe.get("evidenceRefs"))
            good = [str(s)[:200] for s in (probe.get("goodSignals") or probe.get("expectedSignals") or [])
                    if isinstance(s, (str, int, float)) and str(s).strip()]
            entry = {
                "id": str(probe.get("id") or f"probe-{idx + 1}")[:60],
                "priority": (
                    p if (p := str(probe.get("priority") or "MEDIUM").strip().upper())
                    in {"HIGH", "MEDIUM", "LOW"} else "MEDIUM"
                ),
                "question": question[:400],
                "objective": str(probe.get("objective") or probe.get("whyAsk") or "")[:300],
                "triggeredBy": str(probe.get("triggeredBy") or "")[:200],
                "evidenceRefs": refs,
                "goodSignals": good[:8],
                "redFlags": [str(s)[:200] for s in (probe.get("redFlags") or [])
                             if isinstance(s, (str, int, float)) and str(s).strip()][:8],
                "followUps": [str(s)[:200] for s in (probe.get("followUps") or [])
                              if isinstance(s, (str, int, float)) and str(s).strip()][:6],
                "scoreRubric": str(probe.get("scoreRubric") or "")[:300],
            }
            probes_out.append(entry)
        if probes_out:
            out["interviewQuestions"] = probes_out[:12]
            out["interviewProbes"] = probes_out[:12]

        missing = [str(v)[:300] for v in (report.get("missingEvidence") or [])
                   if isinstance(v, (str, int, float)) and str(v).strip()]
        if missing:
            out["missingEvidence"] = missing[:12]
        if system_warnings:
            out["systemWarnings"] = system_warnings[:20]
        if report.get("summary"):
            out["summary"] = str(report["summary"])[:500]
        # Require the core contract fields for a non-empty artifact.
        if "recommendation" not in out and "dimensions" not in out:
            return None
        return out or None

    async def _prepare_context(self) -> None:
        """Deterministic preflight before Coordinator planning.

        Ensures resumeFacts / parsedResume / effectiveJd / jdMatches live in
        the canonical artifact store so subsequent agents never claim
        "原文缺失" when the raw inputs were actually provided.
        """
        request = self.request
        resume = (request.resumeText or "").strip()
        jd = (request.jobDescription or "").strip()
        arts = self.state.artifacts()

        self.state.set_input_presence(
            resume_chars=len(resume),
            jd_chars=len(jd),
            has_jd_matches=bool(arts.get("jdMatches")))

        await self.emitter.emit("run.progress", payload={
            "stage": "preflight",
            "message": "确定性上下文预热",
            "resumeChars": len(resume),
            "jdChars": len(jd)})

        if resume and not arts.get("parsedResume"):
            call = await self.tools.execute(
                "CoordinatorAgent", "parse_resume", {"resumeText": resume})
            if call.status == "SUCCEEDED" and isinstance(call.result, dict):
                self.state.apply_artifacts({"parsedResume": call.result})
                facts = self._resume_facts_from_parse(call.result)
                if facts:
                    self.state.apply_artifacts({"resumeFacts": facts})

        # Guarantee resumeFacts exists when resume text is available — even a
        # minimal stub so specialists always have material to work with.
        arts = self.state.artifacts()
        if resume and not arts.get("resumeFacts"):
            self.state.apply_artifacts({"resumeFacts": {
                "rawExcerpt": resume[:3000],
                "skills": [],
                "projects": [],
                "experiences": [],
                "education": [],
                "contact": {},
                "timelinePeriods": [],
                "source": "raw_text_fallback",
                "completeness": 0,
                "confidence": 0.3,
            }})

        if jd and not arts.get("effectiveJd"):
            self.state.apply_artifacts({"effectiveJd": jd})

        if resume and not arts.get("jdMatches"):
            retrieval = await self.retriever.retrieve(
                "jd", resume_text=resume, top_k=3)
            await self._record_retrieval(
                "CoordinatorAgent", retrieval, requested_k=3)

        # JD normalization is preflight data preparation, not an Agent turn.
        self._prepare_jd_requirements()

        # Refresh presence after JD match may have landed.
        arts = self.state.artifacts()
        self.state.set_input_presence(
            resume_chars=len(resume),
            jd_chars=len(jd) or len(str(arts.get("effectiveJd") or "")),
            has_jd_matches=bool(arts.get("jdMatches")))

    def _pre_steps(self, definition: AgentDefinition) -> List[tuple]:
        request = self.request
        resume = request.resumeText or ""
        artifacts = self.state.artifacts()
        steps: List[tuple] = []
        if definition.agent_id == "ProjectAgent" and resume:
            # Gather cheap internal project evidence before the model turn.
            # Otherwise the model predictably spends its first action turn on
            # these two builtins and cannot progress from Skill activation to
            # optional public MCP research within the bounded latency budget.
            project_claims: List[str] = []
            facts = artifacts.get("resumeFacts") or {}
            if isinstance(facts, dict):
                for project in list(facts.get("projects") or [])[:6]:
                    if isinstance(project, dict):
                        for key in ("name", "title", "summary", "role"):
                            value = str(project.get(key) or "").strip()
                            if value and value not in project_claims:
                                project_claims.append(value[:240])
                    elif str(project).strip():
                        project_claims.append(str(project).strip()[:240])
            if not project_claims:
                project_claims = [
                    line.strip()[:240]
                    for line in resume.splitlines()
                    if any(marker in line for marker in (
                        "项目", "负责", "成果", "贡献"))
                    and line.strip()
                ][:8]
            if project_claims:
                steps.append(("locate_evidence", {
                    "resumeText": resume,
                    "claims": project_claims,
                }))
        elif definition.agent_id == "RiskAgent" and resume \
                and "timelineCheck" not in artifacts:
            steps.append(("check_timeline", {"resumeText": resume}))
        elif definition.agent_id == "TechAgent" and resume:
            effective_jd = ""
            artifact_jd = artifacts.get("effectiveJd")
            if isinstance(artifact_jd, str) and artifact_jd.strip():
                effective_jd = artifact_jd.strip()
            elif (request.jobDescription or "").strip():
                effective_jd = request.jobDescription.strip()
            if effective_jd and "jdCoverage" not in artifacts:
                steps.append(("calculate_jd_coverage",
                              {"resumeText": resume, "jdText": effective_jd}))
        elif definition.agent_id == "EvidenceAgent" and resume:
            claims = self.state.claims_for_verification()
            if claims:
                steps.append(("verify_report_evidence",
                              {"resumeText": resume,
                               "jdText": (artifacts.get("effectiveJd")
                                          or request.jobDescription or ""),
                               "claims": claims,
                               "externalEvidence": list(
                                   artifacts.get("mcpEvidence") or [])}))
        return steps

    def _rag_steps(self, definition: AgentDefinition) -> List[Dict[str, Any]]:
        """Build deterministic retrieval work before the first model turn."""
        request = self.request
        resume = request.resumeText or ""
        artifacts = self.state.artifacts()
        effective_jd = str(
            artifacts.get("effectiveJd") or request.jobDescription or "")
        steps: List[Dict[str, Any]] = []
        if definition.agent_id == "ProjectAgent" and resume:
            query = self._build_project_search_query(resume, artifacts)
            if query:
                steps.append({
                    "source": "resume", "query": query, "top_k": 5,
                    "resume_text": resume, "job_description": effective_jd})
        if definition.agent_id == "TechAgent" and resume:
            query = self._build_tech_search_query(
                resume, effective_jd, artifacts)
            if query:
                steps.append({
                    "source": "resume", "query": query, "top_k": 5,
                    "resume_text": resume, "job_description": effective_jd})
            kb_query = self._build_knowledge_query(
                definition.agent_id, resume, artifacts, request)
            if kb_query:
                steps.append({
                    "source": "knowledge", "query": kb_query, "top_k": 5})
        if definition.agent_id == "ReportAgent":
            if request.runType in ("followup", "quick_answer") \
                    and (request.userMessage or "").strip():
                query = request.userMessage.strip()[:200]
            else:
                query = self._build_knowledge_query(
                    definition.agent_id, resume, artifacts, request)
            if query:
                steps.append({
                    "source": "knowledge", "query": query, "top_k": 5})
            if resume and request.runType in ("followup", "quick_answer"):
                steps.append({
                    "source": "resume", "query": query, "top_k": 5,
                    "resume_text": resume, "job_description": effective_jd})
        return steps

    async def _retrieve_rag_context(
            self, definition: AgentDefinition,
    ) -> Tuple[str, List[Dict[str, Any]]]:
        blocks: List[str] = []
        refs: List[Dict[str, Any]] = []
        for step in self._rag_steps(definition):
            source = str(step.pop("source"))
            retrieval = await self.retriever.retrieve(source, **step)
            await self._record_retrieval(
                definition.agent_id, retrieval,
                requested_k=step.get("top_k"))
            refs.append({
                "retrievalId": retrieval.retrieval_id,
                "source": retrieval.source,
                "query": retrieval.query,
                "status": "SUCCEEDED" if retrieval.succeeded else "FAILED",
                "durationMs": retrieval.duration_ms,
            })
            if retrieval.succeeded:
                payload = json.dumps(
                    retrieval.result, ensure_ascii=False, separators=(",", ":"))
                blocks.append(
                    f"[来源={retrieval.source} "
                    f"retrievalId={retrieval.retrieval_id}]\n{payload[:6000]}")
        return "\n\n".join(blocks), refs

    async def _record_retrieval(
            self, agent_id: str, retrieval: RetrievalResult, *,
            requested_k: Any = None) -> None:
        if retrieval.succeeded:
            if retrieval.source == "jd":
                self._store_jd_match_artifacts(retrieval.result)
            await self._emit_rag_metrics(
                agent_id, retrieval.result, retrieval.query,
                retrieval_name=retrieval.source,
                retrieval_id=retrieval.retrieval_id,
                requested_k=requested_k)
            return
        await self.emitter.emit(
            "retrieval.failed", agent_id=agent_id,
            tool_name=f"retrieval.{retrieval.source}", payload={
                "retrievalId": retrieval.retrieval_id,
                "retrievalName": retrieval.source,
                "query": retrieval.query[:200],
                "error": retrieval.error,
                "durationMs": retrieval.duration_ms,
                "occurredAt": _utc_now(),
            })

    def _build_knowledge_query(self, agent_id: str, resume: str,
                               artifacts: Dict[str, Any],
                               request: Any) -> str:
        """Build a bounded KB query from semantic JD focus or verified gaps."""
        if agent_id == "TechAgent":
            focus = self._jd_focus_text(artifacts, "techSegments")
            if not focus:
                focus = self._jd_focus_fallback(
                    getattr(request, "jobDescription", "")
                    or artifacts.get("effectiveJd") or "")
            title = self._jd_focus_title(artifacts)
            return self._bounded_rag_query([
                f"{title}技术评估标准" if title else "岗位技术评估标准",
                f"岗位要求：{focus}" if focus else "",
                "维度：技术深度、生产工程、架构、性能、故障处理",
            ])

        # ReportAgent runs after specialist/evidence merge, so its retrieval is
        # driven by this Run's actual risks and unsupported evidence instead of
        # a fixed generic phrase.
        title = self._jd_focus_title(artifacts)
        risks = self._artifact_query_texts(
            artifacts.get("risks"),
            ("type", "category", "claim", "text", "detail"), limit=2)
        gaps = self._artifact_query_texts(
            [item for item in (artifacts.get("evidence") or [])
             if isinstance(item, dict) and item.get("verified") is False],
            ("claim", "text", "reason"), limit=2)
        for value in self._artifact_query_texts(
                artifacts.get("conflicts"),
                ("claim", "key", "reason"), limit=2):
            if value not in gaps:
                gaps.append(value)
            if len(gaps) >= 2:
                break
        return self._bounded_rag_query([
            f"{title}候选人评估规则" if title else "候选人评估规则",
            f"风险：{'；'.join(risks)}" if risks else "",
            f"证据缺口：{'；'.join(gaps)}" if gaps else "",
            "规则：评分、证据不足处理、风险定级、录用建议",
        ])

    def _build_tech_search_query(self, resume: str, jd: str,
                                 artifacts: Dict[str, Any]) -> str:
        """Short semantic query from JD passages, never a technology whitelist."""
        focus = self._jd_focus_text(artifacts, "techSegments")
        if not focus:
            focus = self._jd_focus_fallback(jd)
        return self._bounded_rag_query([
            "技术实践证据",
            f"岗位要求：{focus}" if focus else "",
            "关注：个人职责、技术方案、生产实践、性能、故障、量化结果",
        ])

    @staticmethod
    def _bounded_rag_query(parts: List[str], limit: int = 420) -> str:
        normalized = [re.sub(r"\s+", " ", str(part)).strip()
                      for part in parts if str(part or "").strip()]
        query = "｜".join(normalized)
        return query if len(query) <= limit else query[:limit].rstrip("；｜,， ")

    @staticmethod
    def _jd_focus_text(artifacts: Dict[str, Any], key: str,
                       limit: int = 300) -> str:
        focus = artifacts.get("jdFocus") or {}
        values = focus.get(key) if isinstance(focus, dict) else []
        selected: List[str] = []
        used = 0
        for raw in values or []:
            value = re.sub(r"\s+", " ", str(raw)).strip()
            if not value or value in selected:
                continue
            remaining = limit - used - (1 if selected else 0)
            if remaining <= 0:
                break
            selected.append(value[:remaining])
            used += len(selected[-1]) + (1 if len(selected) > 1 else 0)
        return "；".join(selected)

    @staticmethod
    def _jd_focus_fallback(jd: Any, limit: int = 300) -> str:
        """Only used when semantic selection is unavailable, never primary."""
        value = re.sub(r"\s+", " ", str(jd or "")).strip()
        if len(value) <= limit:
            return value
        window = limit // 3
        middle = max(0, len(value) // 2 - window // 2)
        return "…".join((
            value[:window],
            value[middle:middle + window],
            value[-window:],
        ))[:limit]

    @staticmethod
    def _jd_focus_title(artifacts: Dict[str, Any]) -> str:
        focus = artifacts.get("jdFocus") or {}
        if isinstance(focus, dict) and str(focus.get("jobTitle") or "").strip():
            return re.sub(r"\s+", " ", str(focus["jobTitle"])).strip()[:60]
        requirements = artifacts.get("jdRequirements") or {}
        if isinstance(requirements, dict):
            return re.sub(r"\s+", " ", str(requirements.get("title") or "")).strip()[:60]
        return ""

    @staticmethod
    def _artifact_query_texts(raw: Any, keys: Tuple[str, ...],
                              *, limit: int) -> List[str]:
        values = raw if isinstance(raw, list) else []
        out: List[str] = []
        for item in values:
            if isinstance(item, dict):
                text = next((str(item.get(key) or "").strip()
                             for key in keys if str(item.get(key) or "").strip()), "")
            else:
                text = str(item or "").strip()
            text = re.sub(r"\s+", " ", text)[:80]
            if text and text not in out:
                out.append(text)
            if len(out) >= limit:
                break
        return out

    def _apply_verification(self, result: Any) -> None:
        if not isinstance(result, dict):
            return
        supported = []
        unsupported = []
        for entry in result.get("supported", []) or []:
            if not isinstance(entry, dict):
                continue
            supported.append({
                "text": entry.get("claim", ""), "verified": True,
                "location": entry.get("location"), "byAgent": "EvidenceAgent"})
        for entry in result.get("unsupported", []) or []:
            if not isinstance(entry, dict):
                continue
            unsupported.append({
                "text": entry.get("claim", ""), "verified": False,
                "reason": entry.get("reason"), "byAgent": "EvidenceAgent"})
            self.state.add_conflict({
                "type": "unsupported_claim", "claim": entry.get("claim", ""),
                "reason": entry.get("reason", ""), "byAgent": "EvidenceAgent"})
        if supported or unsupported:
            self.state.apply_artifacts({"evidence": supported + unsupported})

    def _policy_instructions(self) -> str:
        ev = self.policy.evidenceVerification
        lines = [
            f"当前策略: {self.policy.policyId}",
            f"证据核验: {'严格' if ev.strict else '启用' if ev.enabled else '关闭'}"
            f"（最低支持率 {ev.minSupportRatio}）",
            f"预算: LLM≤{self.policy.maxLlmCalls} 次, 工具≤{self.policy.toolBudget.maxToolCallsPerRun} 次",
        ]
        if self.policy.jobFocus:
            lines.append(f"岗位侧重: {self.policy.jobFocus}")
        return "\n".join(lines)

    def _memory_context(
            self, definition: AgentDefinition
            ) -> Tuple[str, Optional[Dict[str, Any]]]:
        if definition.memory_policy == "none":
            return "", None
        agent_id = definition.agent_id
        pool = list(self.memory_hits)
        if not pool:
            return "", None
        used, ignored = filter_hits_for_consumer(pool, agent_id)
        trace = memory_trace_entries(used, ignored, agent_id)
        self.memory_traces.extend(trace)
        decisions = decisions_from_hits(used, ignored, agent_id)
        audit = {
            "consumerAgent": agent_id,
            "usedCount": len(used),
            "ignoredCount": len(ignored),
            "memoryTrace": trace[:20],
            "decisions": decisions,
        }
        selected = used[: self.policy.memoryRetrieval.topK]
        profiles = [hit for hit in selected if hit.get("type") == "JOB_PROFILE"]
        recent_cases = [hit for hit in selected if hit.get("type") == "RECENT_CASE"]

        def bounded_list(
                structured: Dict[str, Any], key: str, *, limit: int,
                item_chars: int) -> List[str]:
            values = structured.get(key)
            if not isinstance(values, list):
                return []
            return [
                str(value).strip()[:item_chars]
                for value in values[:limit]
                if str(value).strip()
            ]

        def prompt_view(hit: Dict[str, Any]) -> Dict[str, Any]:
            structured = hit.get("structuredContent") \
                or hit.get("structured") or {}
            if not isinstance(structured, dict) or not structured:
                # Compatibility for legacy rows written before structuredContent.
                return {"summary": str(hit.get("content") or "")[:400]}
            if hit.get("type") == "JOB_PROFILE":
                sample_count = structured.get("sampleCount")
                return {
                    "jobKey": structured.get("jobKey"),
                    "jobCategory": structured.get("jobCategory"),
                    "sampleCount": sample_count
                    if isinstance(sample_count, int) else None,
                    "stableRequirements": bounded_list(
                        structured, "stableRequirements", limit=5,
                        item_chars=120),
                    "commonGaps": bounded_list(
                        structured, "commonGaps", limit=4, item_chars=120),
                    "commonRiskPatterns": bounded_list(
                        structured, "commonRiskPatterns", limit=4,
                        item_chars=100),
                    "unsupportedClaimPatterns": bounded_list(
                        structured, "unsupportedClaimPatterns", limit=4,
                        item_chars=120),
                }

            features = structured.get("resumeFeatures")
            features = features if isinstance(features, dict) else {}
            project_count = features.get("projectCount")
            support_ratio = structured.get("evidenceSupportRatio")
            return {
                "jobKey": structured.get("jobKey"),
                "jobCategory": structured.get("jobCategory"),
                "runType": structured.get("runType"),
                "resumeFeatures": {
                    "skills": bounded_list(
                        features, "skills", limit=8, item_chars=60),
                    "projectCount": project_count
                    if isinstance(project_count, int) else None,
                    "hasPublicUrl": bool(features.get("hasPublicUrl")),
                },
                "verifiedMatches": bounded_list(
                    structured, "verifiedMatches", limit=3, item_chars=120),
                "jdGaps": bounded_list(
                    structured, "jdGaps", limit=3, item_chars=120),
                "unsupportedClaims": bounded_list(
                    structured, "unsupportedClaims", limit=3,
                    item_chars=120),
                "riskPatterns": bounded_list(
                    structured, "riskPatterns", limit=3, item_chars=100),
                "evidenceSupportRatio": support_ratio
                if isinstance(support_ratio, (int, float)) else None,
            }

        lines = [
            "以下仅用于校准证据检查，不是当前候选人的事实或结论；必须以当前简历/JD/工具证据为准。",
        ]
        if profiles:
            lines.extend([
                "[长期岗位画像|JSON]",
                json.dumps(prompt_view(profiles[0]), ensure_ascii=False,
                           separators=(",", ":")),
            ])
        if recent_cases:
            lines.extend([
                "[近期同岗位案例|JSON]",
                json.dumps([prompt_view(hit) for hit in recent_cases],
                           ensure_ascii=False, separators=(",", ":")),
            ])
        block = "\n".join(lines) if used else ""
        return block, audit

    @staticmethod
    def _format_tool_result(call: Any) -> str:
        preview = json.dumps(call.result, ensure_ascii=False)[:1500] \
            if call.result is not None else (call.error or "")[:400]
        return (f"\n[TOOL_CALL {call.tool} id={call.tool_call_id}]"
                f"\n[TOOL_RESULT {call.tool} id={call.tool_call_id} "
                f"status={call.status}] {preview}")

    async def _emit_guard(self, guard: Any, agent_id: str) -> None:
        await self.emitter.emit("run.progress", agent_id=agent_id, payload={
            "stage": "loop_guard", "kind": guard.kind,
            "detail": guard.detail, "action": guard.action})

    def _degraded_answer(self, reason: str) -> str:
        """Best-effort answer from whatever the blackboard already holds.
        Always labelled — degraded output is never disguised as a report."""
        arts = self.state.artifacts()
        sections: List[str] = [f"> 说明：{reason}，以下为基于已完成分析的降级结果（非完整报告）。\n"]
        if arts.get("technicalFindings"):
            sections.append("## 技术发现\n" + "\n".join(
                f"- {e.get('text', json.dumps(e, ensure_ascii=False)[:160])}"
                for e in (arts.get("technicalFindings") or [])[:8] if isinstance(e, dict)))
        if arts.get("risks"):
            sections.append("## 风险\n" + "\n".join(
                f"- {e.get('text', json.dumps(e, ensure_ascii=False)[:160])}"
                for e in (arts.get("risks") or [])[:8] if isinstance(e, dict)))
        if arts.get("conflicts"):
            sections.append("## 证据不足/冲突\n" + "\n".join(
                f"- {c.get('claim', c.get('key', ''))}"
                for c in (arts.get("conflicts") or [])[:6] if isinstance(c, dict)))
        coverage = arts.get("jdCoverage")
        if isinstance(coverage, dict) and coverage.get("coverage") is not None:
            sections.append(f"## JD 覆盖率\n- {coverage.get('coverage')}")
        if len(sections) == 1:
            sections.append("尚未获得足够分析结果，请重试或缩小问题范围。")
        return "\n\n".join(sections)

    def _conversation_summary(self) -> str:
        arts = self.state.artifacts()
        parts = [f"目标: {(self.request.currentGoal or self.request.userMessage or '')[:150]}"]
        tf = arts.get("technicalFindings")
        if tf and isinstance(tf, list):
            parts.append("技术结论: " + "; ".join(
                str(e.get("text", ""))[:80] for e in tf[:3]
                if isinstance(e, dict)))
        rf = arts.get("risks")
        if rf and isinstance(rf, list):
            parts.append("风险: " + "; ".join(
                str(e.get("text", ""))[:80] for e in rf[:3]
                if isinstance(e, dict)))
        if arts.get("conflicts"):
            parts.append(f"未决冲突 {len(arts.get('conflicts') or [])} 项")
        return "\n".join(parts)[:1800]

    def _explicit_preferences(self) -> List[Dict[str, str]]:
        """Only preferences the user literally stated are persisted; nothing
        is inferred by the model."""
        message = self.request.userMessage or ""
        found = []
        for pattern, kind in _PREFERENCE_PATTERNS:
            match = pattern.search(message)
            if match:
                found.append({"kind": kind,
                              "text": match.group("pref").strip()[:120]})
        return found[:2]

    @staticmethod
    def _runtime_strategy_class(selected_agents: List[str]) -> Tuple[str, str]:
        selected = set(selected_agents)
        if {"ProjectAgent", "EvidenceAgent"} <= selected:
            return (
                "PROJECT_EVIDENCE",
                "项目或外部链接场景保留 ProjectAgent 与 EvidenceAgent，并为证据工具调用预留 action turn。",
            )
        if "RiskAgent" in selected:
            return (
                "RISK_TIMELINE",
                "履历风险场景保留 RiskAgent，并将时间线结论交给 EvidenceAgent 或 ReportAgent 复核。",
            )
        if "TechAgent" in selected:
            return (
                "JD_TECH",
                "JD 由确定性 preflight 归一化，再由 TechAgent 逐项核对证据。",
            )
        return (
            "BASELINE",
            "轻量简历评估仅保留满足目标产物所需的最短路由，并由 ReportAgent 收口。",
        )

    async def _write_memories(self, summary: str) -> None:
        """Write only de-identified, same-job business memories.

        RECENT_CASE is one accepted workflow case (30d). JOB_PROFILE is an
        upsertable job/JD profile (180d); Java consolidates repeated writes by
        its stable factKey. Neither record contains identity fields, raw resume
        text, report prose, recommendation, interview questions, or user chat.
        """
        await self.emitter.emit(
            "agent.started", agent_id="MemoryService",
            payload={"description": "同岗位业务记忆持久化"})
        try:
            arts = self.state.artifacts()
            job_description = (self.request.jobDescription or "").strip()
            job_category = (self.request.jobCategory or "").strip().upper()
            if not job_description and not job_category:
                await self.emitter.emit(
                    "agent.completed", agent_id="MemoryService",
                    payload={"durationMs": 0, "llmCalls": 0, "toolCalls": 0,
                             "written": 0, "reason": "job_scope_missing"})
                return

            normalized_jd = re.sub(r"\s+", " ", job_description).lower()
            jd_fingerprint = hashlib.sha256(
                normalized_jd.encode("utf-8")).hexdigest()[:20]
            job_key = job_category or f"JD:{jd_fingerprint}"

            parsed = arts.get("resumeFacts") \
                if isinstance(arts.get("resumeFacts"), dict) else {}
            skills = [
                str(value).strip()[:60]
                for value in (parsed.get("skills") or [])
                if str(value).strip()
            ][:12]
            projects = parsed.get("projects") or []
            resume_features = {
                "skills": skills,
                "projectCount": len(projects) if isinstance(projects, list) else 0,
                "hasPublicUrl": bool(re.search(
                    r"https?://|github\.com|gitee\.com",
                    self.request.resumeText or "", re.IGNORECASE)),
            }

            verified_matches: List[str] = []
            unsupported_claims: List[str] = []
            for item in (arts.get("evidence") or []):
                if not isinstance(item, dict):
                    continue
                text = str(item.get("text") or item.get("claim") or "").strip()
                if not text:
                    continue
                target = verified_matches if item.get("verified") is True \
                    else unsupported_claims if item.get("verified") is False else None
                if target is not None and text[:180] not in target:
                    target.append(text[:180])
            for item in (arts.get("conflicts") or []):
                if not isinstance(item, dict):
                    continue
                text = str(item.get("claim") or item.get("key") or "").strip()
                if text and text[:180] not in unsupported_claims:
                    unsupported_claims.append(text[:180])

            coverage = arts.get("jdCoverage") \
                if isinstance(arts.get("jdCoverage"), dict) else {}
            gaps: List[str] = []
            for item in (coverage.get("gaps") or []):
                value = (item.get("requirement") or item.get("text") or "") \
                    if isinstance(item, dict) else item
                if str(value).strip():
                    gaps.append(str(value).strip()[:160])

            risk_patterns: List[str] = []
            for item in (arts.get("risks") or []):
                if not isinstance(item, dict):
                    continue
                value = item.get("type") or item.get("category") \
                    or item.get("text") or item.get("claim")
                if str(value or "").strip():
                    risk_patterns.append(str(value).strip()[:120])

            recent_structured = {
                "memoryKind": "recent_job_case",
                "jobKey": job_key,
                "jobCategory": job_category or None,
                "jdFingerprint": jd_fingerprint,
                "runType": self.request.runType,
                "resumeFeatures": resume_features,
                "verifiedMatches": verified_matches[:6],
                "jdGaps": gaps[:6],
                "unsupportedClaims": unsupported_claims[:6],
                "riskPatterns": risk_patterns[:6],
                "evidenceSupportRatio": self.state.evidence_support_ratio(),
                "piiExcluded": True,
                "rawResumeExcluded": True,
                "derivedFromRunId": self.request.runId,
            }
            recent_content = (
                f"岗位={job_key}; 技能特征={', '.join(skills[:8]) or '无'}; "
                f"已核验匹配={'; '.join(verified_matches[:3]) or '无'}; "
                f"JD缺口={'; '.join(gaps[:3]) or '无'}; "
                f"待核验={'; '.join(unsupported_claims[:3]) or '无'}; "
                f"风险类型={'; '.join(risk_patterns[:3]) or '无'}")[:1200]
            await self._queue_memory_write(
                type_="RECENT_CASE", owner_scope="USER",
                content=recent_content, structured=recent_structured,
                source="recent_job_case",
                source_id=f"recent_case:{job_key}:{self.request.runId}",
                confidence=0.85, ttl_days=30)

            requirements = arts.get("jdRequirements") \
                if isinstance(arts.get("jdRequirements"), dict) else {}
            stable_requirements = [
                str(value).strip()[:160]
                for value in (requirements.get("mustHave")
                              or requirements.get("requirements") or [])
                if str(value).strip()
            ][:10]
            profile_fact_key = f"job_profile:{job_key}:{jd_fingerprint}"
            profile_structured = {
                "factKey": profile_fact_key,
                "memoryKind": "job_profile",
                "jobKey": job_key,
                "jobCategory": job_category or None,
                "jdFingerprint": jd_fingerprint,
                "sampleCount": 1,
                "stableRequirements": stable_requirements,
                "commonGaps": gaps[:8],
                "commonRiskPatterns": risk_patterns[:8],
                "unsupportedClaimPatterns": unsupported_claims[:8],
                "piiExcluded": True,
                "rawResumeExcluded": True,
                "derivedFromRunIds": [self.request.runId],
            }
            profile_content = (
                f"岗位画像={job_key}; 稳定要求={'; '.join(stable_requirements[:5]) or '无'}; "
                f"常见证据缺口={'; '.join(gaps[:4]) or '待积累'}; "
                f"常见风险={'; '.join(risk_patterns[:4]) or '待积累'}")[:1200]
            await self._queue_memory_write(
                type_="JOB_PROFILE", owner_scope="USER",
                content=profile_content, structured=profile_structured,
                source="job_profile", source_id=profile_fact_key,
                confidence=0.8, ttl_days=180)

            self.state.data["memoryWriteCandidates"] = list(
                self.pending_memory_writes)
            await self.emitter.emit(
                "agent.completed", agent_id="MemoryService",
                payload={"durationMs": 0, "llmCalls": 0, "toolCalls": 0,
                         "written": len(self.pending_memory_writes),
                         "types": ["RECENT_CASE", "JOB_PROFILE"]})
        except Exception as exc:
            logger.info("business memory write-back skipped: %s", exc)
            await self.emitter.emit(
                "agent.completed", agent_id="MemoryService",
                payload={"durationMs": 0, "llmCalls": 0, "toolCalls": 0,
                         "written": 0, "error": str(exc)[:200]})


    async def _queue_memory_write(
            self, *, type_: str, owner_scope: str, content: str,
            structured: Optional[Dict[str, Any]] = None,
            source: str = "model_generated",
            source_id: Optional[str] = None, confidence: float = 0.5,
            ttl_days: Optional[int] = None) -> Optional[str]:
        """Queue a business memory for Java to persist after terminal accept."""
        taxonomy = str(type_ or "").strip().upper()
        if taxonomy not in {"RECENT_CASE", "JOB_PROFILE"}:
            return None
        candidate_id = f"pending-{uuid.uuid4().hex[:16]}"
        self.pending_memory_writes.append({
            "candidateId": candidate_id,
            "type": taxonomy,
            "ownerScope": owner_scope,
            "content": content,
            "structuredContent": dict(structured or {}),
            "source": source,
            "sourceId": source_id,
            "confidence": confidence,
            "ttlDays": ttl_days,
        })
        return candidate_id

    def _result(self, status: str, answer: str, *, error_code: Optional[str] = None,
                error_message: Optional[str] = None,
                conversation_summary: Optional[str] = None,
                snapshot: Optional[Dict[str, Any]] = None,
                missing_goal_artifacts: Optional[List[str]] = None) -> Dict[str, Any]:
        executed_agents = [o.get("agentId") for o in self.state.data["agentOutputs"]]
        support_ratio = self.state.evidence_support_ratio()
        coverage = None
        artifact = self.state.data["artifacts"].get("jdCoverage")
        if isinstance(artifact, dict):
            coverage = artifact.get("coverage")
        missing_goals = list(missing_goal_artifacts or [])
        metrics = {
            "llmCalls": self.budget.llm_calls,
            "llmBudget": self.budget.llm_audit(
                self.policy.maxLlmCalls),
            "logicalAgentLlmTurns": sum(
                int(counter.get("llmCalls", 0))
                for counter in self.agent_counters.values()
                if isinstance(counter, dict)),
            "toolCalls": self.budget.tool_calls,
            "promptTokens": self.budget.prompt_tokens,
            "completionTokens": self.budget.completion_tokens,
            "promptCacheHitTokens": self.budget.prompt_cache_hit_tokens,
            "costCny": round(self.budget.cost_cny, 4),
            "latencySeconds": round(self.budget.elapsed_seconds(), 2),
            "agentsUsed": executed_agents,
            "agentTimingsMs": self.agent_timings,
            "loopGuardTrips": self.guard.summary(),
            "contextCompactions": len(self.context.compactions),
            "degradedReasons": self.degraded_reasons,
            "evidenceSupportRatio": support_ratio,
            "jdCoverage": coverage,
            "missingGoalArtifacts": missing_goals,
            **self.tools.metrics(),
        }
        prompt_versions = default_prompt_manager.versions_used(
            list(dict.fromkeys(executed_agents + ["CoordinatorAgent"])),
            self.policy.promptVersions)
        skill_versions = default_skill_manager.versions_used(self.skill_selections)
        shared = self.state.snapshot()
        shared["agentOutputs"] = shared["agentOutputs"][-12:]
        result: Dict[str, Any] = {
            "status": status,
            "answer": answer,
            "errorCode": error_code,
            "errorMessage": error_message,
            "sharedState": shared,
            "metrics": metrics,
            "promptVersions": prompt_versions,
            "skillVersions": skill_versions,
            "conversationSummary": conversation_summary or "",
            "currentGoal": (self.request.currentGoal or self.request.userMessage or "")[:500],
            "missingGoalArtifacts": missing_goals,
        }
        final_report = self.state.data["artifacts"].get("finalReport")
        if isinstance(final_report, dict) and final_report:
            result["structuredReport"] = final_report
        if snapshot is not None:
            result["executionSnapshot"] = snapshot
        return result
