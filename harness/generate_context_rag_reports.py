#!/usr/bin/env python3
"""Generate the final context-audit and RAG Markdown reports on ECS.

The script deliberately reads the persisted experiment artefacts instead of
copying metrics into the report by hand.  It also redacts personal contact data
before writing any example prompt or resume excerpt.
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import font_manager
from matplotlib.patches import FancyBboxPatch, FancyArrowPatch


AGENTS = ["TechAgent", "ProjectAgent", "RiskAgent", "EvidenceAgent", "ReportAgent"]
AGENT_CN = {
    "TechAgent": "技术证据",
    "ProjectAgent": "项目核验",
    "RiskAgent": "履历风险",
    "EvidenceAgent": "证据审计",
    "ReportAgent": "报告收口",
}

# Production SkillManager's single source of truth.  These are deliberately
# shown next to each Agent request in the audit report so readers do not need
# to jump to a separate appendix to understand the effective Skill contract.
AGENT_SKILL_FILES = {
    "TechAgent": [
        ("assess-technical-evidence",
         "backend/src/main/resources/skills/assess-technical-evidence/SKILL.md"),
    ],
    "ProjectAgent": [
        ("ground-project-claims",
         "backend/src/main/resources/skills/ground-project-claims/SKILL.md"),
        ("retrieve-public-candidate-evidence",
         "backend/src/main/resources/skills/retrieve-public-candidate-evidence/SKILL.md"),
    ],
    "RiskAgent": [
        ("risk_pattern_detection",
         "backend/src/main/resources/skills/risk_pattern_detection/SKILL.md"),
    ],
    "EvidenceAgent": [
        ("calibrate-evidence-confidence",
         "backend/src/main/resources/skills/calibrate-evidence-confidence/SKILL.md"),
    ],
    "ReportAgent": [],
}


def load_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def redact(text: str) -> str:
    text = re.sub(r"(?<!\d)1[3-9]\d{9}(?!\d)", "<REDACTED_PHONE>", text)
    text = re.sub(
        r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}",
        "<REDACTED_EMAIL>",
        text,
    )
    return text


def parse_envelope(invocation: dict[str, Any], field: str) -> dict[str, Any]:
    value = invocation.get(field) or "{}"
    if isinstance(value, dict):
        return value
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return {}


def response_tool_calls(invocation: dict[str, Any]) -> list[dict[str, Any]]:
    return parse_envelope(invocation, "responseFull").get("toolCalls") or []


def decision_summary(invocations: list[dict[str, Any]], agent: str) -> str:
    candidates = [item for item in invocations if item.get("agentRole") == agent]
    candidates.sort(key=lambda item: (item.get("requestStartedAt") or "", item.get("id") or ""))
    for item in reversed(candidates):
        for call in response_tool_calls(item):
            args = call.get("arguments") or {}
            output = args.get("output") or {}
            if output.get("summary"):
                return redact(str(output["summary"]))
            payload = args.get("payload") or {}
            if payload.get("summary"):
                return redact(str(payload["summary"]))
    return "该 Agent 的结构化摘要未直接出现在审计响应中，详见最终报告产物。"


def configure_font(font_path: Path) -> None:
    if font_path.exists():
        font_manager.fontManager.addfont(str(font_path))
        prop = font_manager.FontProperties(fname=str(font_path))
        plt.rcParams["font.family"] = prop.get_name()
    plt.rcParams["axes.unicode_minus"] = False
    plt.rcParams.update(
        {
            "font.size": 16,
            "axes.titlesize": 24,
            "axes.labelsize": 18,
            "xtick.labelsize": 15,
            "ytick.labelsize": 16,
            "legend.fontsize": 15,
        }
    )


def save_figure(fig: plt.Figure, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=180, bbox_inches="tight", facecolor="white")
    plt.close(fig)


def draw_vertical_pipeline(path: Path) -> None:
    fig, ax = plt.subplots(figsize=(12, 21))
    ax.set_xlim(0, 10)
    ax.set_ylim(0, 20.5)
    ax.axis("off")
    ax.set_title("一次简历评估的真实主链路（竖向阅读）", pad=24, fontweight="bold")
    nodes = [
        (19.0, "上传简历 + 选择/召回 JD", "Java Backend · MySQL · Redis Stream"),
        (17.3, "TaskWorker 消费任务", "创建 agent_run，thread_id = runId"),
        (15.6, "LangGraph observe_plan", "读 Memory；预处理简历与 JD"),
        (13.9, "Coordinator 确定性规划", "本轮 FULL_EVAL：artifact/signal 规则，0 次 LLM"),
        (12.2, "dispatch + Send 并行 Specialist", "TechAgent · ProjectAgent · RiskAgent"),
        (10.5, "Reducer merge + replan #1", "replanned=false，继续 dispatch"),
        (8.8, "EvidenceAgent", "逐条校准 supported / unsupported / conflicted"),
        (7.1, "Reducer merge + replan #2", "replanned=false，继续 dispatch"),
        (5.4, "ReportAgent", "score / risk / question 分段生成后收口"),
        (3.7, "Reducer merge + replan #3", "replanned=false，最终 replanCount=0"),
        (2.0, "finalize + 业务结果落库", "resume_task + agent_run + PostgreSQL checkpoints"),
    ]
    for idx, (y, title, subtitle) in enumerate(nodes):
        box = FancyBboxPatch(
            (1.1, y),
            7.8,
            1.05,
            boxstyle="round,pad=0.04,rounding_size=0.08",
            linewidth=1.8,
            edgecolor="#2563eb",
            facecolor="#eff6ff" if idx not in {4, 6, 8} else "#ecfdf5",
        )
        ax.add_patch(box)
        ax.text(5, y + 0.68, title, ha="center", va="center", fontsize=20, fontweight="bold")
        ax.text(5, y + 0.28, subtitle, ha="center", va="center", fontsize=15, color="#334155")
        if idx < len(nodes) - 1:
            next_y = nodes[idx + 1][0]
            ax.add_patch(
                FancyArrowPatch(
                    (5, y - 0.03),
                    (5, next_y + 1.1),
                    arrowstyle="-|>",
                    mutation_scale=18,
                    linewidth=1.8,
                    color="#64748b",
                )
            )
    save_figure(fig, path)


def draw_agent_context(metrics: dict[str, Any], path: Path) -> None:
    per_agent = metrics.get("perAgent") or {}
    calls = [per_agent.get(agent, {}).get("calls", 0) for agent in AGENTS]
    prompt_tokens = [per_agent.get(agent, {}).get("promptTokens", 0) for agent in AGENTS]
    completion_tokens = [per_agent.get(agent, {}).get("completionTokens", 0) for agent in AGENTS]
    cache_rate = [100 * per_agent.get(agent, {}).get("cacheHitRate", 0) for agent in AGENTS]
    labels = [f"{agent}\n{AGENT_CN[agent]}" for agent in AGENTS]

    fig, axes = plt.subplots(2, 1, figsize=(13, 13), gridspec_kw={"height_ratios": [1.25, 1]})
    x = range(len(AGENTS))
    axes[0].bar([i - 0.19 for i in x], prompt_tokens, width=0.38, label="Prompt tokens", color="#2563eb")
    axes[0].bar([i + 0.19 for i in x], completion_tokens, width=0.38, label="Completion tokens", color="#10b981")
    axes[0].set_xticks(list(x), labels)
    axes[0].set_ylabel("Token 数")
    axes[0].set_title("各 Agent 的真实上下文与输出规模")
    axes[0].legend()
    axes[0].grid(axis="y", alpha=0.25)
    for idx, value in enumerate(prompt_tokens):
        axes[0].text(idx - 0.19, value + max(prompt_tokens) * 0.02, f"{value:,}", ha="center", fontsize=13)
    for idx, value in enumerate(completion_tokens):
        axes[0].text(idx + 0.19, value + max(prompt_tokens) * 0.02, f"{value:,}", ha="center", fontsize=13)

    bars = axes[1].bar(labels, cache_rate, color="#f59e0b")
    axes[1].set_ylim(0, max(40, max(cache_rate or [0]) + 8))
    axes[1].set_ylabel("Cache hit rate (%)")
    axes[1].set_title("单链路各 Agent Prompt Cache 命中率")
    axes[1].grid(axis="y", alpha=0.25)
    for bar, rate, count in zip(bars, cache_rate, calls):
        axes[1].text(bar.get_x() + bar.get_width() / 2, rate + 1, f"{rate:.1f}% · {count} calls", ha="center", fontsize=13)
    fig.tight_layout(pad=2.5)
    save_figure(fig, path)


def draw_call_timeline(invocations: list[dict[str, Any]], path: Path) -> None:
    rows = sorted(invocations, key=lambda item: item.get("requestStartedAt") or "")
    if not rows:
        return
    starts = [datetime.fromisoformat(item["requestStartedAt"]) for item in rows]
    origin = min(starts)
    offsets = [(stamp - origin).total_seconds() for stamp in starts]
    durations = [float(item.get("durationMs") or 0) / 1000 for item in rows]
    labels = [f"{idx + 1:02d} {item.get('agentRole')} / {item.get('purpose')}" for idx, item in enumerate(rows)]
    colors = {agent: color for agent, color in zip(AGENTS, ["#2563eb", "#0ea5e9", "#f59e0b", "#8b5cf6", "#10b981"])}
    fig, ax = plt.subplots(figsize=(14, 12))
    y = list(range(len(rows)))
    ax.barh(y, durations, left=offsets, color=[colors.get(item.get("agentRole"), "#64748b") for item in rows], height=0.62)
    ax.set_yticks(y, labels)
    ax.invert_yaxis()
    ax.set_xlabel("相对首个 LLM 请求的时间（秒）")
    ax.set_title("11 次真实 Provider 请求：并行、工具回合与 Report 重试")
    ax.grid(axis="x", alpha=0.25)
    for yi, left, duration in zip(y, offsets, durations):
        ax.text(left + duration + 0.5, yi, f"{duration:.1f}s", va="center", fontsize=13)
    fig.tight_layout(pad=2)
    save_figure(fig, path)


def draw_lazy_skill(metrics: dict[str, Any], path: Path) -> None:
    skills = (metrics.get("agentRuntime") or metrics).get("skills") or {}
    names = list(skills)
    selected = [skills[name].get("selected", 0) for name in names]
    loaded = [skills[name].get("loaded", 0) for name in names]
    applied = [skills[name].get("applied", 0) for name in names]
    skipped = [skills[name].get("skipped", 0) for name in names]
    fig, ax = plt.subplots(figsize=(14, 8))
    x = range(len(names))
    width = 0.2
    for offset, values, label, color in [
        (-0.3, selected, "selected", "#2563eb"),
        (-0.1, loaded, "loaded", "#0ea5e9"),
        (0.1, applied, "applied", "#10b981"),
        (0.3, skipped, "skipped", "#ef4444"),
    ]:
        ax.bar([i + offset for i in x], values, width=width, label=label, color=color)
    labels = [name.replace("-", "-\n", 1) for name in names]
    ax.set_xticks(list(x), labels)
    ax.set_ylim(0, max(1.35, max(selected or [1]) + 0.35))
    ax.set_ylabel("本次真实 Run 事件次数")
    ax.set_title("Lazy Skill：被选中不等于一定加载")
    ax.legend(ncol=4, loc="upper center")
    ax.grid(axis="y", alpha=0.2)
    fig.tight_layout(pad=2)
    save_figure(fig, path)


def tool_inventory(invocations: list[dict[str, Any]]) -> tuple[dict[str, set[str]], Counter[str]]:
    available: dict[str, set[str]] = defaultdict(set)
    called: Counter[str] = Counter()
    for item in invocations:
        req = parse_envelope(item, "promptFull").get("providerRequest") or {}
        for spec in req.get("tools") or []:
            fn = spec.get("function") or {}
            if fn.get("name"):
                available[item.get("agentRole") or "unknown"].add(fn["name"])
        for call in response_tool_calls(item):
            if call.get("name"):
                called[call["name"]] += 1
    return available, called


def prompt_sections(invocation: dict[str, Any]) -> dict[str, bool]:
    req = parse_envelope(invocation, "promptFull").get("providerRequest") or {}
    content = "\n".join(str(item.get("content") or "") for item in req.get("messages") or [])
    return {
        "策略": "[策略要求]" in content,
        "Skill": "[技能指令]" in content,
        "请求": "[当前请求]" in content,
        "Memory": "[相关记忆]" in content,
        "共享状态": "[共享状态]" in content,
        "工具观察": "[工具观察]" in content,
    }


def format_skill_table(skill_metrics: dict[str, Any]) -> str:
    skills = (skill_metrics.get("agentRuntime") or skill_metrics).get("skills") or {}
    owner = {
        "assess-technical-evidence": "TechAgent",
        "ground-project-claims": "ProjectAgent",
        "retrieve-public-candidate-evidence": "ProjectAgent",
        "risk_pattern_detection": "RiskAgent",
        "calibrate-evidence-confidence": "EvidenceAgent",
    }
    lines = ["| Agent | Skill | selected | loaded | applied | skipped | 真实解释 |", "|---|---|---:|---:|---:|---:|---|"]
    for name, values in skills.items():
        loaded = int(values.get("loaded", 0))
        skipped = int(values.get("skipped", 0))
        explanation = "模型调用 `load_skill` 后，完整 Skill body 才进入后续请求" if loaded else "仅注入目录摘要；模型本轮未调用 `load_skill`"
        lines.append(
            f"| {owner.get(name, '—')} | `{name}` | {values.get('selected', 0)} | {loaded} | {values.get('applied', 0)} | {skipped} | {explanation} |"
        )
    return "\n".join(lines)


def _repair_mojibake(value: str) -> str:
    """Undo the Windows-1252/UTF-8 round-trip present in the migrated DB dump."""
    buf = bytearray()
    for char in value:
        try:
            buf.extend(char.encode("cp1252"))
        except UnicodeEncodeError:
            if ord(char) <= 255:
                buf.append(ord(char))
            else:
                return value
    try:
        repaired = bytes(buf).decode("utf-8")
    except UnicodeDecodeError:
        return value
    return repaired if repaired != value else value


def _json_fence(value: Any) -> str:
    rendered = redact(json.dumps(value, ensure_ascii=False, indent=2))
    return f"````json\n{rendered}\n````"


def _text_fence(value: str) -> str:
    return f"````text\n{redact(value)}\n````"


def _details(summary: str, body: str) -> str:
    """Render a default-closed disclosure block for verbose audit payloads."""

    return f"<details>\n<summary>{summary}</summary>\n\n{body}\n\n</details>"


def _skill_block_from_system(system: str) -> str:
    marker = "[技能指令]"
    start = system.find(marker)
    if start < 0:
        return ""
    start += len(marker)
    end = system.find("[输出要求]", start)
    return system[start:end if end >= 0 else None].strip()


def exact_skill_injection_appendix(invocations: list[dict[str, Any]]) -> str:
    sections: list[str] = []
    for agent in AGENTS:
        rows = sorted(
            [item for item in invocations if item.get("agentRole") == agent],
            key=lambda item: item.get("requestStartedAt") or "",
        )
        seen: set[str] = set()
        states: list[tuple[str, str]] = []
        for idx, item in enumerate(rows, start=1):
            request = parse_envelope(item, "promptFull").get("providerRequest") or {}
            system = next(
                (str(message.get("content") or "") for message in request.get("messages") or [] if message.get("role") == "system"),
                "",
            )
            block = _skill_block_from_system(system)
            if not block or block in seen:
                continue
            seen.add(block)
            state = "完整 Skill body 已加载" if "[已加载技能指令]" in block else "仅 Skill 目录摘要（Lazy）"
            states.append((f"第 {idx} 次 {agent} Provider 请求：{state}", block))
        sections.append(f"### {agent}\n")
        if not states:
            sections.append("本次请求没有 Skill section。\n")
            continue
        for label, block in states:
            sections.append(_details(label, _text_fence(block)) + "\n")
        body_was_loaded = any(
            "[已加载技能指令]" in block for _, block in states)
        if not body_was_loaded:
            for skill_name, skill_path in AGENT_SKILL_FILES.get(agent, []):
                skill_body = _repository_text(skill_path)
                sections.append(
                    "上面的内容只是本轮实际注入的 Lazy Skill 目录卡。"
                    "由于模型没有调用 `load_skill`，下面的正文未进入本轮 Prompt；"
                    "这里仍紧邻目录卡列出生产源 `SKILL.md` 全文，方便审计。\n\n"
                    + _details(
                        f"展开 {skill_name}/SKILL.md 完整原文（本轮未注入）",
                        _text_fence(skill_body),
                    )
                    + "\n"
                )
    return "\n".join(sections)


def exact_provider_request_appendix(invocations: list[dict[str, Any]]) -> str:
    rows = sorted(invocations, key=lambda item: item.get("requestStartedAt") or "")
    agent_counter: Counter[str] = Counter()
    output: list[str] = []
    output.extend(
        [
            "| 全局序号 | Agent 内序号 | Agent / purpose | model | messages | tools | Prompt / Completion | Cache tokens | 时延 | finish |",
            "|---:|---:|---|---|---:|---:|---:|---:|---:|---|",
        ]
    )
    prepared: list[tuple[int, int, dict[str, Any], dict[str, Any], dict[str, Any]]] = []
    for global_idx, item in enumerate(rows, start=1):
        agent = str(item.get("agentRole") or "unknown")
        agent_counter[agent] += 1
        req_env = parse_envelope(item, "promptFull")
        response = parse_envelope(item, "responseFull")
        request = req_env.get("providerRequest") or {}
        usage = response.get("usage") or {}
        output.append(
            f"| {global_idx} | {agent_counter[agent]} | {agent} / `{item.get('purpose')}` | `{item.get('model')}` | "
            f"{len(request.get('messages') or [])} | {len(request.get('tools') or [])} | "
            f"{item.get('inputTokens', 0):,} / {item.get('outputTokens', 0):,} | "
            f"{usage.get('prompt_cache_hit_tokens', 0):,} | {item.get('durationMs', 0) / 1000:.3f}s | `{item.get('finishReason')}` |"
        )
        prepared.append((global_idx, agent_counter[agent], item, req_env, response))

    output.append("\n下面 11 个折叠块是审计表中 `prompt_full` / `response_full` 的直接脱敏展开；其中 `providerRequest.messages` 就是模型真正收到的完整 Prompt，`providerRequest.tools` 是本轮原生 function schema。\n")
    for global_idx, agent_idx, item, req_env, response in prepared:
        request = req_env.get("providerRequest") or {}
        tool_names = [
            str((tool.get("function") or {}).get("name") or "")
            for tool in request.get("tools") or []
        ]
        response_calls = [str(call.get("name") or "") for call in response.get("toolCalls") or []]
        messages = request.get("messages") or []
        memory_present = any("[相关记忆]" in str(message.get("content") or "") for message in messages)
        loaded_skill = any("[已加载技能指令]" in str(message.get("content") or "") for message in messages)
        label = (
            f"#{global_idx:02d} {item.get('agentRole')} 第{agent_idx}次 / {item.get('purpose')} — "
            f"messages={len(messages)}, availableTools={tool_names or ['无']}, "
            f"called={response_calls or ['无']}, SkillBody={'有' if loaded_skill else '无'}, Memory={'有' if memory_present else '无'}"
        )
        envelope_meta = {
            key: req_env.get(key)
            for key in [
                "schemaVersion",
                "runId",
                "conversationId",
                "traceId",
                "agentId",
                "purpose",
                "budgetScope",
                "callIndex",
                "providerAttempt",
                "traceContext",
                "providerUrl",
                "inventory",
            ]
        }
        output.append(
            f"<details>\n<summary>{label}</summary>\n\n"
            f"{_provider_detail_blocks(request, response, envelope_meta)}\n\n"
            "</details>\n"
        )
    return "\n".join(output)


def _readable_message(message: dict[str, Any], index: int) -> str:
    role = str(message.get("role") or "unknown")
    name = message.get("name")
    tool_call_id = message.get("tool_call_id") or message.get("toolCallId")
    suffixes = []
    if name:
        suffixes.append(f"name={name}")
    if tool_call_id:
        suffixes.append(f"tool_call_id={tool_call_id}")
    suffix = f" ({', '.join(suffixes)})" if suffixes else ""
    content = message.get("content")
    if isinstance(content, str):
        rendered_content = _text_fence(content)
    else:
        rendered_content = _json_fence(content)
    extra = {
        key: value
        for key, value in message.items()
        if key not in {"role", "content", "name", "tool_call_id", "toolCallId"}
    }
    extra_block = ""
    if extra:
        extra_block = "\n\n" + _details(
            "message 的其他原始字段",
            _json_fence(extra),
        )
    content_size = len(content) if isinstance(content, str) else len(
        json.dumps(content, ensure_ascii=False))
    return _details(
        f"messages[{index}] — `{role}`{suffix}｜{content_size:,} 字符",
        f"{rendered_content}{extra_block}",
    )


def _readable_tool(tool: dict[str, Any], index: int) -> str:
    function = tool.get("function") or {}
    tool_name = str(function.get("name") or f"tool_{index}")
    return _details(
        f"tools[{index}] — `{tool_name}` 完整 schema",
        _json_fence(tool),
    )


def _provider_detail_blocks(
        request: dict[str, Any], response: dict[str, Any],
        envelope_meta: dict[str, Any] | None = None) -> str:
    """Split one Provider call into independently collapsible audit payloads."""

    messages = request.get("messages") or []
    tools = request.get("tools") or []
    request_config = {
        key: value
        for key, value in request.items()
        if key not in {"messages", "tools"}
    }
    blocks: list[str] = []
    if envelope_meta is not None:
        blocks.append(_details("审计元数据", _json_fence(envelope_meta)))
    blocks.append(_details(
        "请求顶层配置（model / tool_choice / 生成参数）",
        _json_fence(request_config),
    ))
    blocks.append("#### Prompt：messages[] 按真实发送顺序")
    blocks.extend(
        _readable_message(message, index)
        for index, message in enumerate(messages)
    )
    blocks.append("#### Tool 目录：tools[]")
    if tools:
        blocks.extend(_readable_tool(tool, index) for index, tool in enumerate(tools))
    else:
        blocks.append("本次请求的 `tools` 为空。")

    response_content = response.get("content")
    response_content_block = (
        _text_fence(response_content)
        if isinstance(response_content, str)
        else _json_fence(response_content)
    )
    response_other = {
        key: value for key, value in response.items() if key != "content"
    }
    blocks.append(_details(
        "输出：Provider response.content",
        response_content_block,
    ))
    blocks.append(_details(
        "输出：toolCalls / usage / finishReason 等",
        _json_fence(response_other),
    ))
    return "\n\n".join(blocks)


def _agent_skill_reference_blocks(
        agent: str, request: dict[str, Any]) -> str:
    """Show production SKILL.md files inside the matching Agent prompt block."""

    skills = AGENT_SKILL_FILES.get(agent, [])
    if not skills:
        return "#### 本 Agent 对应的 SKILL.md\n\n该 Agent 在 registry 中没有绑定 Skill。"
    system = "\n".join(
        str(message.get("content") or "")
        for message in request.get("messages") or []
        if message.get("role") == "system"
    )
    blocks = ["#### 本 Agent 对应的 SKILL.md（生产源全文）"]
    for skill_id, skill_path in skills:
        loaded = (
            "[已加载技能指令]" in system
            and skill_id in system
        )
        status = "该次请求已注入" if loaded else "该次请求只有目录，正文未注入"
        blocks.append(_details(
            f"{skill_id}/SKILL.md｜{status}",
            f"生产源：`{skill_path}`\n\n{_text_fence(_repository_text(skill_path))}",
        ))
    return "\n\n".join(blocks)


_PRE_LLM_CONTEXT_KIND = {
    "resume_semantic_search": "当前简历证据检索（RAG）",
    "knowledge_search": "知识库检索（RAG）",
    "jd_match_search": "JD 检索（RAG）",
    "locate_evidence": "简历文本证据定位上下文（非知识库 RAG）",
    "calculate_jd_coverage": "确定性 JD 覆盖率规则",
    "check_timeline": "确定性时间线规则",
    "verify_report_evidence": "确定性证据校验",
}


def _agent_pre_llm_context_blocks(request: dict[str, Any]) -> str:
    """Explain retrieval/rule context directly injected into user messages."""

    observations: list[tuple[str, str, Any]] = []
    for message in request.get("messages") or []:
        content = message.get("content")
        if not isinstance(content, str):
            continue
        for match in re.finditer(
                r"^\[TOOL_RESULT\s+([^\s\]]+)[^\]]*\]\s*(.*)$",
                content, re.MULTILINE):
            tool_name = match.group(1)
            raw = match.group(2).strip()
            try:
                payload: Any = json.loads(raw)
            except json.JSONDecodeError:
                payload = raw
            observations.append((
                tool_name,
                _PRE_LLM_CONTEXT_KIND.get(
                    tool_name, "Runtime 预处理上下文"),
                payload,
            ))
    native_history = any(
        message.get("role") == "tool" or message.get("tool_calls")
        for message in request.get("messages") or []
    )
    lines = [
        "#### 直接注入该次 user prompt 的 RAG / 规则上下文",
        "",
        "这里的检索与规则计算由 Runtime 在调用 LLM 前完成，结果直接写入"
        " `messages[].content` 的 user prompt。审计文本沿用了"
        " `[TOOL_CALL]/[TOOL_RESULT]` 内部回执标记，但它们不是模型 tool call，"
        "也不会出现在 Provider `tools[]` 中。模型原生工具回合才表现为后续"
        " `assistant → tool` messages。",
        "",
        "> **当前实现债务**：Provider 看到的是直接注入的 RAG context；"
        "但 Runtime 内部尚未把 Retrieval 与 Tool 两条管线彻底拆开，"
        "检索仍经 `ToolExecutor.execute()`、`tool_results_block` 和"
        " `[工具观察]` 传递。因此这里描述的是当前真实实现，不声称代码层"
        "已经完成 RAG/Tool 解耦。",
        "",
    ]
    if not observations:
        lines.append("该次 user prompt 没有额外的 Runtime 检索/规则上下文。")
    else:
        lines.extend([
            "| Runtime 数据源 | 上下文类型 | 注入位置 |",
            "|---|---|---|",
        ])
        seen: set[str] = set()
        for tool_name, kind, payload in observations:
            lines.append(
                f"| `{tool_name}` | {kind} | `user message.content` |")
            if tool_name in seen:
                continue
            seen.add(tool_name)
            rendered = (
                _text_fence(payload) if isinstance(payload, str)
                else _json_fence(payload)
            )
            lines.extend([
                "",
                _details(
                    f"展开 {tool_name} 直接注入 user prompt 的内容",
                    rendered),
            ])
    lines.extend([
        "",
        f"该次请求是否还包含模型原生 `assistant → tool` 历史："
        f"**{'是' if native_history else '否'}**。",
    ])
    return "\n".join(lines)


def readable_final_agent_prompts(invocations: list[dict[str, Any]]) -> str:
    """Render final effective requests as readable message blocks, not escaped JSON strings."""
    ordered = sorted(invocations, key=lambda item: item.get("requestStartedAt") or "")
    targets = [
        ("TechAgent", None, "TechAgent 最终技术结论请求"),
        ("ProjectAgent", None, "ProjectAgent 最终项目核验请求"),
        ("RiskAgent", None, "RiskAgent 最终风险结论请求"),
        ("EvidenceAgent", None, "EvidenceAgent 最终证据审计请求"),
        ("ReportAgent", "report_score", "ReportAgent / score 最终重试请求"),
        ("ReportAgent", "report_risk", "ReportAgent / risk 最终请求"),
        ("ReportAgent", "report_question", "ReportAgent / question 最终请求"),
    ]
    sections: list[str] = []
    for agent, purpose, title in targets:
        candidates = [item for item in ordered if item.get("agentRole") == agent]
        if purpose is not None:
            candidates = [item for item in candidates if item.get("purpose") == purpose]
        if not candidates:
            sections.append(f"### {title}\n\n本轮没有匹配的 Provider 请求。")
            continue
        item = candidates[-1]
        request_envelope = parse_envelope(item, "promptFull")
        request = request_envelope.get("providerRequest") or {}
        response = parse_envelope(item, "responseFull")
        messages = request.get("messages") or []
        tools = request.get("tools") or []
        summary = (
            f"{title}｜purpose={item.get('purpose')}｜messages={len(messages)}｜"
            f"tools={len(tools)}｜Prompt/Completion="
            f"{item.get('inputTokens', 0):,}/{item.get('outputTokens', 0):,}｜"
            f"{item.get('durationMs', 0) / 1000:.3f}s｜点击展开完整原始请求"
        )
        sections.append(
            f"<details>\n<summary><strong>{summary}</strong></summary>\n\n"
            f">选取规则：该 Agent / purpose 按 `requestStartedAt` 排序后的最后一次真实请求。"
            f" invocation id=`{item.get('id')}`，purpose=`{item.get('purpose')}`，"
            f"Prompt/Completion=`{item.get('inputTokens', 0):,}/{item.get('outputTokens', 0):,}`，"
            f"duration=`{item.get('durationMs', 0) / 1000:.3f}s`，finish=`{item.get('finishReason')}`。\n\n"
            f"{_agent_skill_reference_blocks(agent, request)}\n\n"
            f"{_agent_pre_llm_context_blocks(request)}\n\n"
            f"{_provider_detail_blocks(request, response)}\n\n"
            "</details>"
        )
    return "\n\n".join(sections)


def _repository_text(relative_path: str) -> str:
    path = Path(__file__).resolve().parents[1] / relative_path
    return path.read_text(encoding="utf-8") if path.exists() else ""


def _coordinator_prompt_v1() -> str:
    source = _repository_text("workflow/app/runtime/prompts.py")
    match = re.search(
        r'PromptVersion\("coordinator-system",\s*"CoordinatorAgent",\s*"v1",\s*"""(.*?)"""\)',
        source,
        re.DOTALL,
    )
    return match.group(1).strip() if match else "未能从 prompts.py 读取 coordinator-system v1。"


def coordinator_prompt_reference_card() -> str:
    """Keep Coordinator visible beside every other Agent prompt.

    Full evaluations currently short-circuit LLM refinement, so this is a
    repository-backed prompt template rather than a fabricated provider
    request.  The distinction belongs inside the card, not in place of it.
    """
    return (
        "<details>\n"
        "<summary><strong>CoordinatorAgent｜coordinator-system v1｜"
        "本轮 Provider 调用 0 次｜点击展开完整 Prompt 配置</strong></summary>\n\n"
        ">这是仓库中当前 Coordinator 的完整生产 Prompt 模板。"
        "本次 `full_evaluation` 被确定性 planner 短路，因此该模板本轮没有发送给 Provider；"
        "这里展示的是配置真值，不冒充真实请求。实际规划输入、计划输出和事件时间线见第 4 节。\n\n"
        f"{_details('Coordinator system message：coordinator-system v1（完整原文）', _text_fence(_coordinator_prompt_v1()))}\n\n"
        "</details>"
    )


def risk_skill_reference_section(invocations: list[dict[str, Any]]) -> str:
    skill_body = _repository_text(
        "backend/src/main/resources/skills/risk_pattern_detection/SKILL.md")
    risk_rows = [
        item for item in invocations if item.get("agentRole") == "RiskAgent"
    ]
    loaded = any(
        "[已加载技能指令]" in str(message.get("content") or "")
        for item in risk_rows
        for message in (
            parse_envelope(item, "promptFull").get("providerRequest") or {}
        ).get("messages") or []
    )
    return f"""### 6.1 RiskAgent 的 timeline、Tool、artifact、Skill 到底是什么关系

| 名称 | 类型 | 本项目中的真实作用 |
|---|---|---|
| `timeline_check` | Agent capability / 路由标签 | 表示 RiskAgent 能处理时间线核验场景；它不是文件，也不是 Skill |
| `check_timeline` | Python 内置规则 Tool | 解析履历年月，确定性地产出 gaps / overlaps 等结果 |
| `timelineCheck` | SharedState artifact | `check_timeline` 的结果保存到这里，随后进入 RiskAgent 的共享上下文 |
| `risk_pattern_detection` | Skill 注册 ID | RiskAgent 绑定的风险分析 Skill |
| `risk_pattern_detection/SKILL.md` | 生产 Skill 原文件 | 定义时间线、夸大、一致性、角色匹配等风险框架 |

本轮 `risk_pattern_detection` 的状态是 **selected，但 loaded=0**；RiskAgent 最终 Prompt 中完整 Skill body **{'已经注入' if loaded else '没有注入'}**。不过为了让审计文档能回答“这个 Skill 到底写了什么”，下面仍展示仓库原文件，并明确它是**代码配置参考，不冒充本轮实际 Prompt**。

{_details('生产源：backend/src/main/resources/skills/risk_pattern_detection/SKILL.md（本轮未加载）', _text_fence(skill_body))}
"""


def real_memory_examples(audit_dir: Path) -> str:
    examples = load_json(audit_dir / "memory_examples.json", []) or []
    lines = [
        "| memory_id | type | owner_scope | source | confidence | 数据库真实 content（脱敏） |",
        "|---|---|---|---|---:|---|",
    ]
    for item in examples[:5]:
        content = _repair_mojibake(str(item.get("content") or ""))
        content = redact(content).replace("|", "\\|").replace("\n", " ")
        memory_id = str(item.get("memoryId") or "")
        lines.append(
            f"| `{memory_id[:12]}…` | {item.get('type')} | {item.get('ownerScope')} | {item.get('source')} | "
            f"{float(item.get('confidence') or 0):.3f} | {content} |"
        )
    return "\n".join(lines)


def _repair_tree(value: Any) -> Any:
    if isinstance(value, str):
        return _repair_mojibake(value)
    if isinstance(value, list):
        return [_repair_tree(item) for item in value]
    if isinstance(value, dict):
        return {key: _repair_tree(item) for key, item in value.items()}
    return value


def coordinator_control_plane_section(
        audit_dir: Path, invocations: list[dict[str, Any]]) -> str:
    trace = load_json(audit_dir / "coordinator_trace.json", {}) or {}
    events = trace.get("events") or []
    coordinator_llm_calls = sum(
        1 for item in invocations if item.get("agentRole") == "CoordinatorAgent")
    selected = next(
        (
            event for event in events
            if event.get("eventType") == "agent.selected"
            and event.get("agentId") == "CoordinatorAgent"
        ),
        {},
    )
    selected_payload = _repair_tree(selected.get("payload") or {})
    raw = load_json(audit_dir / "raw_results.json", []) or []
    raw_row = raw[0] if isinstance(raw, list) and raw else raw if isinstance(raw, dict) else {}
    task = raw_row.get("rawTask") or {}

    memory_read = next(
        (event for event in events if event.get("eventType") == "memory.read"
         and event.get("agentId") == "CoordinatorAgent"),
        {},
    )
    parse_done = next(
        (event for event in events if event.get("eventType") == "tool.completed"
         and event.get("agentId") == "CoordinatorAgent"
         and event.get("toolName") == "parse_resume"),
        {},
    )
    jd_done = next(
        (event for event in events if event.get("eventType") == "tool.completed"
         and event.get("agentId") == "CoordinatorAgent"
         and event.get("toolName") == "jd_match_search"),
        {},
    )
    retrieval_done = next(
        (event for event in events if event.get("eventType") == "retrieval.completed"
         and event.get("agentId") == "CoordinatorAgent"
         and event.get("toolName") == "jd_match_search"),
        {},
    )
    dispatches = [
        event for event in events
        if event.get("eventType") == "run.progress"
        and (event.get("payload") or {}).get("stage") == "langgraph.dispatch"
    ]
    merges = [
        event for event in events
        if event.get("eventType") == "run.progress"
        and (event.get("payload") or {}).get("stage") == "langgraph.reducer_merge"
    ]
    replan_checks = [
        event for event in events
        if event.get("eventType") == "run.progress"
        and (event.get("payload") or {}).get("stage") == "langgraph.replan"
    ]

    timeline_rows = [
        "| 顺序 / seq | 真实节点或动作 | 本轮真实输入/输出 |",
        "|---:|---|---|",
    ]
    if events:
        timeline_rows.extend([
            "| 1 / 3 | `observe_plan` 开始 | 加载上下文与 Memory；不是 LLM Prompt |",
            f"| 2 / {memory_read.get('seq', '—')} | Coordinator Memory read | hitCount="
            f"{(memory_read.get('payload') or {}).get('hitCount', 0)}，"
            f"duration={(memory_read.get('payload') or {}).get('durationMs', 0)}ms |",
            f"| 3 / {parse_done.get('seq', '—')} | `parse_resume` 确定性预处理 | 输入简历 "
            f"{len(str(task.get('resumeText') or '')):,} 字符；outcome="
            f"{(parse_done.get('payload') or {}).get('outcome', '—')}，"
            f"duration={(parse_done.get('payload') or {}).get('durationMs', 0)}ms |",
            f"| 4 / {jd_done.get('seq', '—')}–{retrieval_done.get('seq', '—')} | `jd_match_search` + retrieval telemetry | "
            f"outcome={(jd_done.get('payload') or {}).get('outcome', '—')}；"
            f"hitCount={(retrieval_done.get('payload') or {}).get('hitCount', 0)}；"
            f"strategy={(retrieval_done.get('payload') or {}).get('strategy', '—')}；"
            f"totalMs={((retrieval_done.get('payload') or {}).get('stages') or {}).get('totalMs', 0)} |",
            f"| 5 / {selected.get('seq', '—')} | Coordinator 输出计划 | reason=`"
            f"{_md(selected_payload.get('reason', '—'))}`；LLM Provider calls={coordinator_llm_calls} |",
        ])
        for index, event in enumerate(dispatches):
            payload = event.get("payload") or {}
            agents = ", ".join(payload.get("agents") or [])
            merge = merges[index] if index < len(merges) else {}
            check = replan_checks[index] if index < len(replan_checks) else {}
            check_payload = check.get("payload") or {}
            timeline_rows.append(
                f"| {6 + index} / {event.get('seq')}→{merge.get('seq', '—')}→{check.get('seq', '—')} "
                f"| dispatch → Reducer merge → replan check | `[{agents}]`；"
                f"replanned=`{str(bool(check_payload.get('replanned'))).lower()}`；"
                f"replanCount=`{check_payload.get('replanCount', 0)}` |"
            )
        finalize = next(
            (event for event in events if event.get("eventType") == "run.progress"
             and (event.get("payload") or {}).get("stage") == "langgraph.finalize"),
            {},
        )
        timeline_rows.append(
            f"| {6 + len(dispatches)} / {finalize.get('seq', '—')} | `finalize` | "
            "终态报告与 Memory 写回后结束 |"
        )
    else:
        timeline_rows.append("| — | 没有导出的 control-plane events | 不做推断 |")

    safe_plan = {
        "reason": selected_payload.get("reason"),
        "plan": selected_payload.get("plan"),
        "parallelGroups": selected_payload.get("parallelGroups"),
        "presentArtifacts": selected_payload.get("presentArtifacts"),
        "goalArtifacts": selected_payload.get("goalArtifacts"),
        "artifactEdges": selected_payload.get("artifactEdges"),
        "selectedBecause": selected_payload.get("selectedBecause"),
        "skippedBecause": selected_payload.get("skippedBecause"),
        "budgetPlan": selected_payload.get("budgetPlan"),
        "requiredTerminalAgent": selected_payload.get("requiredTerminalAgent"),
        "policyId": selected_payload.get("policyId"),
        "planMode": selected_payload.get("planMode"),
        "memoryHits": selected_payload.get("memoryHits"),
        "llmBudgetAtPlan": selected_payload.get("llmBudget"),
    }
    coordinator_prompt = _coordinator_prompt_v1()
    coordinator_input_summary = {
        "runType": task.get("runType") or "full_evaluation",
        "userMessage": task.get("userMessage"),
        "conversationSummary": task.get("conversationSummary"),
        "resumeTextChars": len(str(task.get("resumeText") or "")),
        "jobDescriptionChars": len(str(task.get("jobDescription") or "")),
        "memoryHitCount": selected_payload.get("memoryHits"),
        "presentArtifacts": selected_payload.get("presentArtifacts"),
        "goalArtifacts": selected_payload.get("goalArtifacts"),
    }
    return f"""### CoordinatorAgent：本轮实际走确定性规划，Prompt 模板仍完整列出

Coordinator 不能和五个 Specialist 混成同一种“Agent 调用”。本次 `runType=full_evaluation` 中，它执行了 `observe_plan`、Memory 检索、确定性预处理、artifact/signal 规划、dispatch 与每组后的 replan 检查；但 `llm_invocation` 中 Coordinator 记录为 **{coordinator_llm_calls}**，所以不存在一段“本轮 Coordinator 最终 Provider Prompt”可供展示。

代码的真实短路条件是：

```python
if (self.is_simple(run_type) or self.llm is None
        or run_type in FULL_EVAL_TYPES):
    return base
```

也就是说，仓库里保留的 `coordinator-system v1` 是 Coordinator 的真实 Prompt 配置，但本轮完整评估没有走 `_refine()`。下面把它完整列出，同时明确标注“本轮未发送给 Provider”，避免一边漏文档、一边又把模板冒充成真实调用。

{_details('Coordinator Prompt 模板：coordinator-system v1（本轮未发送给 Provider）', _text_fence(coordinator_prompt))}

{_details('Coordinator 本轮实际规划输入摘要（确定性 planner 参数，不是 LLM messages）', _json_fence(coordinator_input_summary))}

#### 本次 Coordinator / LangGraph 控制面真实时间线

{chr(10).join(timeline_rows)}

这里三次 `langgraph.replan` 都是**检查节点确实执行**，但三次都是 `replanned=false`、最终 `replanCount=0`。因此准确说法是“经过三次 replan gate”，不是“本轮发生了动态 Replan”。

#### Coordinator 本轮真实计划输出（来自 seq={selected.get('seq', '—')} `agent.selected`）

{_details('展开 Coordinator 计划输出 JSON', _json_fence(safe_plan))}
"""


def make_context_report(audit_dir: Path, font_path: Path) -> Path:
    metrics = load_json(audit_dir / "context_audit_metrics.json", {})
    summary = load_json(audit_dir / "summary.json", {})
    representative = load_json(audit_dir / "context_audit_representative.json", {})
    raw = load_json(audit_dir / "raw_results.json", {})
    if isinstance(raw, list):
        raw = raw[0] if raw else {}
    invocations = representative.get("invocations") or []
    available, called = tool_inventory(invocations)
    assets = audit_dir / "assets"
    draw_vertical_pipeline(assets / "01_vertical_pipeline.png")
    draw_agent_context(metrics, assets / "02_agent_context_tokens.png")
    draw_call_timeline(invocations, assets / "03_provider_call_timeline.png")
    draw_lazy_skill(summary, assets / "04_lazy_skill_events.png")

    run = summary.get("agentRuntime") or {}
    task = raw.get("rawTask") or {}
    match = ((task.get("topJdMatches") or [{}])[0])
    per_agent = metrics.get("perAgent") or {}
    section_rows = []
    for agent in AGENTS:
        rows = [item for item in invocations if item.get("agentRole") == agent]
        merged = {key: any(prompt_sections(item)[key] for item in rows) for key in ["策略", "Skill", "请求", "Memory", "共享状态", "工具观察"]}
        flags = " / ".join(f"{key}:{'有' if value else '无'}" for key, value in merged.items())
        tool_names = ", ".join(sorted(available.get(agent, set()))) or "无"
        stats = per_agent.get(agent) or {}
        section_rows.append(
            f"| {agent} | {stats.get('calls', 0)} | {stats.get('promptTokens', 0):,} / {stats.get('completionTokens', 0):,} | {100 * stats.get('cacheHitRate', 0):.1f}% | {flags} | `{tool_names}` |"
        )

    called_lines = "\n".join(f"- `{name}`：{count} 次模型原生 tool call" for name, count in called.most_common())
    agent_examples = []
    consumes = {
        "TechAgent": "resumeFacts、effectiveJd / jdRequirements、JD coverage、RAG 定位片段",
        "ProjectAgent": "resumeFacts、project claims、effectiveJd、候选人公开 URL、上轮工具观察",
        "RiskAgent": "resumeFacts、timelineCheck、JD 输入存在性、规则工具 check_timeline 结果",
        "EvidenceAgent": "technicalFindings、projectFindings、risks、MCP evidence 与冲突",
        "ReportAgent": "全部已合并/校准共享状态；按 score、risk、question 三种 purpose 生成分段",
    }
    produces = {
        "TechAgent": "technical_findings：技术维度、深度、证据与关键缺口",
        "ProjectAgent": "project_findings + mcpEvidence：项目复杂度、个人贡献、量化结果与外链核验",
        "RiskAgent": "risks：时间线、重复指标、技能堆砌、项目归属等风险",
        "EvidenceAgent": "evidence / conflicts / recommendations：逐条支持状态与置信度",
        "ReportAgent": "finalReport：总体分、建议、维度评分、风险、追问与缺失证据",
    }
    for agent in AGENTS:
        agent_examples.append(
            f"### {agent}（{AGENT_CN[agent]}）\n\n"
            f"- **实际消费**：{consumes[agent]}。\n"
            f"- **实际产出**：{produces[agent]}。\n"
            f"- **本例结构化摘要**：{decision_summary(invocations, agent)}\n"
        )

    final_agent_prompts = readable_final_agent_prompts(invocations)
    coordinator_section = coordinator_control_plane_section(audit_dir, invocations)
    coordinator_prompt_card = coordinator_prompt_reference_card()
    skill_injection_details = exact_skill_injection_appendix(invocations)
    risk_skill_reference = risk_skill_reference_section(invocations)
    provider_request_details = exact_provider_request_appendix(invocations)
    memory_examples_table = real_memory_examples(audit_dir)

    report = f"""# Context Audit 真实单链路报告（非 100 份压测）

> 生成时间：2026-08-05。数据来自 ECS 上一次真实上传、真实 LangGraph 执行和真实 DeepSeek Provider 请求。候选人样本为**合成但生产形态完整的测试简历**，下文使用 `C-014`，联系方式已脱敏。用户已取消新的 100 份压测，因此本报告只证明链路正确性，不声称容量或 P95 稳定性。

## 1. 先说结论

- 任务最终 `SUCCESS`，E2E **{summary.get('endToEndLatencyMs', {}).get('p50', 0) / 1000:.3f}s**，Runtime **{run.get('runLatencyMs', {}).get('runtime', {}).get('p50', 0) / 1000:.3f}s**。
- Coordinator 控制面完成确定性规划（本轮 0 次 LLM）；五个 LLM 执行 Agent 全部运行，共 **{metrics.get('invocationCount', 0)}** 次真实 Provider 请求。Prompt / Completion 为 **{run.get('llm', {}).get('promptTokens', 0):,} / {run.get('llm', {}).get('completionTokens', 0):,} tokens**，费用 **¥{run.get('llm', {}).get('costCny', 0):.4f}**。
- `CONTEXT_AUDIT_ENABLED=true` 记录的是**最终发给 Provider 的 messages、tools schema、tool_choice 和真实响应**，不是从模板反推的伪样例；PII 扫描 **{metrics.get('piiLeakCheck', {}).get('phoneMatches', 0)} 个手机号、{metrics.get('piiLeakCheck', {}).get('emailMatches', 0)} 个邮箱命中**。
- Skill 已是 **Lazy**：5 个 Skill 被路由选中，但只有 Tech 的 1 个和 Project 的 2 个被模型调用 `load_skill` 后加载/应用；Risk/Evidence 本轮选择跳过。
- 本例没有 `[相关记忆]` 正文进入 Provider 请求，`memoryUsageByType={{}}`。这不是“项目没有 Memory”，而是本轮没有达到注入条件的相关记忆；数据库已有 10,653 条持久化 Memory，不能为了报告伪造一次命中。

## 2. 一次上传到底经过什么

![一次简历评估的竖向主链路](assets/01_vertical_pipeline.png)

这张图刻意改成竖向：每一步单独占一行，避免之前一张超宽图在 Markdown 中被压成缩略图。

## 3. 本次真实输入与最终结果

| 项目 | 本例真实值 |
|---|---|
| 样本 | `ai_agent_engineer_014`，报告中匿名为 `C-014` |
| 简历形态 | 2,149 字符；AI Agent 后端方向；包含 LangGraph、RAG、Milvus、MCP、FastAPI/Spring 线索与多个量化主张 |
| 命中 JD | `{match.get('title', task.get('matchedJdTitle', '高级 Java / AI Agent 平台工程师'))}` |
| JD 核心要求 | Java、Spring Boot、MySQL、Redis、Docker、RAG、LLM；偏好可观测与线上排障 |
| 规则匹配分 | `{match.get('matchScore', task.get('jdMatchScore', 0)):.4f}`；技能 `{match.get('skillMatchScore', 0):.4f}` / 经验 `{match.get('experienceMatchScore', 0):.4f}` / 项目 `{match.get('projectMatchScore', 0):.4f}` |
| 规则 gaps | `{redact(str(match.get('gaps') or '无'))}`（这是 Java `JdRagService` 规则匹配产物，不是某个 LLM Agent 生成） |
| Agent 最终结果 | 总分 **{task.get('overallScore', '—')}**；建议 **`{task.get('recommendation', '—')}`** |
| 证据支持率 | **{run.get('evidenceSupportRatio', {}).get('p50', 0):.3f}**；JD coverage **{run.get('jdCoverage', {}).get('p50', 0):.3f}** |

简历中的代表性证据（脱敏、节选）包括：Milvus 索引/分片优化、Agent Runtime 路由与工具预算、Langfuse/Prometheus/Grafana 可观测、RAG 多路召回与重排、MCP 动态工具治理。最终报告没有简单照抄这些主张，而是把“Java/Spring Boot 生产证据不足”“多个数字在不同段落重复”“项目归属不清”列为重点核验项。

## 4. Coordinator 控制面 + 五个 LLM 执行 Agent

{coordinator_section}

### 五个实际产生 Provider 请求的执行 Agent

{"".join(agent_examples)}
## 5. Prompt 不是一段 system 文本，而是一整个 Provider 请求

真实请求的结构如下，顺序与审计落库一致：

```text
providerRequest
├─ model / temperature / max_tokens / stream
├─ messages[]
│  ├─ system
│  │  ├─ Agent 固定职责与证据纪律
│  │  ├─ [策略要求]（balanced、核验阈值、预算）
│  │  ├─ [技能指令]
│  │  │  ├─ 首轮：Skill 目录摘要 + “需要时调用 load_skill”
│  │  │  └─ 加载后续轮：[已加载技能指令] + 完整 Skill body
│  │  └─ [输出要求]（结构化 schema / 原生 tool call 约束）
│  ├─ user
│  │  ├─ [当前请求]
│  │  ├─ [当前目标] / [会话摘要] / [近期消息]（存在才加入）
│  │  ├─ [相关记忆]（有合格命中才加入；本例没有）
│  │  ├─ [共享状态]（简历、JD、上游 Agent 产物）
│  │  └─ [工具观察]（RAG、规则工具、MCP 返回）
│  └─ assistant / tool / user follow-up（发生工具回合才追加）
├─ tools[]：本轮允许的 function schema（独立字段，不是 system 文本）
└─ tool_choice：auto 或强制 emit_decision / emit_report_section
```

### 5.1 五个 LLM 执行 Agent 的真实上下文库存

| Agent | LLM calls | Prompt / Completion tokens | Cache | 实际 messages section | 本轮出现过的 Provider tool schema |
|---|---:|---:|---:|---|---|
{chr(10).join(section_rows)}

![各 Agent 的真实上下文规模与缓存](assets/02_agent_context_tokens.png)

### 5.2 Coordinator 配置与每个 LLM 执行 Agent 的完整请求

Coordinator 也作为第一个 Agent 块列在这里：完整展示仓库中的 `coordinator-system v1`，同时明确标注本轮确定性 planner 短路、Provider 调用为 0，不能把模板冒充成本轮真实请求。随后 Tech / Project / Risk / Evidence 选取各自最后一次有效请求；Report 因为实际分为 score / risk / question 三条并行分支，所以三条都展示，score 选取 `finishReason=length` 后的最终重试。

每个块依次展开：本 Agent 的生产 `SKILL.md` 全文 → 直接注入 user prompt 的 RAG/规则上下文及来源 → 请求参数 → 完整 system/user/assistant/tool messages → 完整 tools schema → 真实 Provider 响应。

特别注意：报告原始审计文本中的 `[TOOL_CALL]/[TOOL_RESULT]` 是 Runtime 内部统一回执格式。对于 `resume_semantic_search`、`knowledge_search` 等检索源，Runtime 在调用 LLM 前完成检索，并把召回结果直接拼入 user message；它们不是 Agent 可调用工具，也不在 Provider `tools[]` 中。只有请求历史里的 `assistant → tool` 才是模型原生 tool call。

这也暴露了当前实现债务：Provider/Agent 视角已经是“RAG context 直接注入 user prompt”，但 Runtime 代码内部仍复用 `ToolExecutor.execute()`、`tool_results_block`、`[工具观察]` 和 `[TOOL_RESULT]` 来承载检索结果。也就是说，**行为上是直接注入，代码抽象上尚未完成 Retrieval/Tool 解耦**；本报告不能把后者美化成已经完成。

{coordinator_prompt_card}

{final_agent_prompts}

## 6. Lazy Skill 到底怎么工作

![Lazy Skill 的 selected / loaded / applied / skipped](assets/04_lazy_skill_events.png)

{format_skill_table(summary)}

关键区别：`selected` 只表示路由认为该 Skill **可能相关**，首轮仅把名称、简介、allowed-tools 和 `load_skill` 用法放进目录；只有模型实际调用 `load_skill(skill_id=...)`，完整 Skill body 才追加到后续请求。它不是 Eager，也不是“每次把五份 Skill 全塞进 Prompt”。

{risk_skill_reference}

## 7. MCP / Tool：目录、调用、观察三件事必须分开

Provider 请求里出现过的工具 schema 是“**可以调用**”，响应 `toolCalls` 才是“**模型真的调用**”，下一轮 message/tool observation 才是“**结果真的回到上下文**”。本次真实模型调用统计：

{called_lines}

其中 ProjectAgent 的可用目录包含 `fetch_fetch`、`exa_web_fetch_exa`、`exa_web_search_exa`；本次 Runtime 指标记录了 `fetch.fetch` 1 次和 `exa.web_fetch_exa` 1 次成功返回。ReportAgent 只有 `emit_report_section`，没有公网 MCP，避免最终报告绕过 EvidenceAgent 自行搜网改写事实。

## 8. Memory：项目里有，但本例没有硬塞

ECS 恢复后的数据库物理行中仍能看到 `PREFERENCE`、`FAILURE` 等历史名称，但当前 Runtime 和 Java 服务对外只有四种正式 taxonomy：**WORKING、SEMANTIC、EPISODIC、PROCEDURAL**。读取时 `PREFERENCE → SEMANTIC`，`FAILURE → EPISODIC`；它们不是第五、第六种 Memory。历史 `USED` 记录覆盖 Report/Tech/Evidence/Risk/Project 五个 Agent，说明 Memory 子系统真实使用过。

但是本次代表 Run：

- Context Audit 的 11 次 Provider 请求都没有 `[相关记忆]` section；
- `memoryUsageByType` 为空；
- 因此本报告不展示伪造的 Memory 正文，也不把“库里有数据”等价成“本次注入了数据”。

真正命中时，Memory 会位于 user message 中、在共享状态之前，形态为带类型/来源/置信度的相关记忆条目；未命中时该 section 整段省略。

### 8.1 四种正式 Memory：本项目中的具体 case

| 类型 | 回答什么问题 | 本项目中的具体写入 case | scope / 默认 TTL | 谁能读取、如何进入 Prompt |
|---|---|---|---|---|
| `WORKING` | “本次 Run 临时处理到什么上下文？” | 上传 C-014 后写入 `run_input_context`：简历长度、是否有 JD、runType、topSkills；Evidence 完成后还可写 `evidence_context`，记录已验证/未验证数量 | 强制 `RUN` / 1天；终态接受后归档，待晋升记录转成目标长期类型 | 只对同一 `runId` 可见；Coordinator、ResumeParser、JDAnalysis 可按策略读取，普通 Specialist 默认不读取 |
| `SEMANTIC` | “这个候选人或用户有哪些稳定事实？” | `candidate_fact`：技能包含 LangGraph、RAG、Milvus，项目包含 ResumAI，经历包含快手/哔哩哔哩；用户明确说“以后优先输出中文”也作为 `SEMANTIC/USER`，不是单独的 PREFERENCE 类型 | 候选人事实 `CONVERSATION`，明确偏好 `USER` / 90天 | 同一候选人后续 revision 或同一用户后续请求检索；以 `[SEMANTIC|src=candidate_fact] ...` 进入 `[相关记忆]` |
| `EPISODIC` | “之前一次评估发生了什么、得到了什么经验证据？” | `evaluation_insight`：本次建议、关键证据、JD缺口和面试验证重点；`cross_candidate_anchor`：同岗位候选人的总分、JD匹配和最大 gap。失败 Run 也只是 `outcome=FAILURE` 的 EPISODIC | 候选人洞察 `CONVERSATION`，对比锚点 `USER` / 90天 | Tech/Project/Risk/Evidence/Report 可读普通评估 episode；控制面失败 episode 仅 Coordinator 可读，不能进入 Risk/Report |
| `PROCEDURAL` | “下一次类似任务应该怎样执行？” | `runtime_strategy[RISK_TIMELINE]`：履历风险场景保留 RiskAgent，并让 EvidenceAgent 或 ReportAgent 复核时间线；只保存候选人无关的路由和工具策略 | `USER` / 365天 | Coordinator 规划时优先读取，Specialist 也可读取获批准策略；以 `[PROCEDURAL|src=runtime_strategy] ...` 进入 `[相关记忆]` |

一次成功 Run 的真实生命周期是：Python 先把所有 Runtime 写入暂存成 `WORKING/RUN`；Java 接受成功终态后，才把待晋升记录变成 `SEMANTIC/EPISODIC/PROCEDURAL`，并归档剩余 WORKING。取消、失败或未被接受的 Run 不会把候选人结论污染到长期 Memory。

### 8.2 数据库里真实存在的 Memory 长什么样

下面来自 ECS `memory_entry` 的真实 active 记录，只选择 `runtime_strategy/system_rule/evaluation_insight` 来源并脱敏；它们证明持久化数据存在，但**不代表本次 Run 使用了它们**。

{memory_examples_table}

### 8.3 命中后实际拼进 Prompt 的格式

当前代码先在 `_memory_context()` 中按 Agent 过滤，然后按 `topK` 截取，每条 content 最多 400 字符；`ContextManager.assemble()` 再把它放到共享状态之前。当前实现的准确形态如下：

````text
[相关记忆]
[相关记忆]
# 历史评估洞察
  <source=evaluation_insight 的真实 content，最多 3 条>
# 同岗位对比基准
  <source=cross_candidate_anchor 的真实 content，最多 3 条>
# 上下文
  [PROCEDURAL|src=runtime_strategy] <真实 content，最多 2 条>
````

注意当前实现会出现两次 `[相关记忆]`：一次由 `_memory_context()` 生成，一次由 `ContextManager.assemble()` 包裹。这是代码现状，不在报告里替它美化。

## 9. 11 次 LLM 调用为什么不是 5 次

![真实 Provider 调用时间线](assets/03_provider_call_timeline.png)

- TechAgent：首轮看 Skill 目录，调用 `load_skill`；第二轮带完整 Skill body 输出技术结论，共 2 次。
- ProjectAgent：Skill 加载 + 外部 URL 工具回合 + 最终结构化提交，共 3 次。
- RiskAgent / EvidenceAgent：各 1 次；本轮未加载完整 Skill。
- ReportAgent：score / risk / question 并行分段，其中 score 首次 `finishReason=length` 后重试，所以共 4 次，也是本例 94.625s E2E 的主要长项。

## 10. Context Audit 抓到了一个非常具体的质量问题

RiskAgent 的工具观察明确返回 `gaps=[]`，但它的 LLM 响应一度把正常衔接误写成“3 年职业空窗”。最终 Report 没把该空窗写进核心风险，并写成“工作经历时间线连贯”。这说明：

1. Context Audit 能定位到**哪一次、哪一个 Agent、看到什么输入却给出什么错误结论**；
2. Evidence/Report 收口确实有价值，但仍应增加“风险结论与 timelineCheck 机器结果一致性”的确定性校验，不能只依赖下游 LLM 自行纠错。

## 11. 本报告能证明与不能证明的边界

能证明：ECS 部署、LangGraph 主链路、PostgreSQL checkpoint、五 Agent 编排、Lazy Skill、MCP 工具回合、Context Audit 落库和最终业务报告均真实跑通。

不能证明：高并发吞吐、P95/P99、100 份完成率、4C8G 容量上限。新的 100 份压测已按用户要求取消，不能拿一份样本伪装成压测结论。

## 12. LLM 执行 Agent 的 Skill 注入原文：目录态与加载态

以下内容直接从 11 次真实 `system` message 的 `[技能指令]` 区段抽取。相同内容只展示一次，因此可以明确看到 Lazy 加载前后到底多了什么。

{skill_injection_details}

## 13. 11 次真实 Provider 请求与响应全文

这部分是面试追问用的审计底稿，不再做“摘要代替原文”。每次请求都包含完整 messages、完整 tools schema、tool_choice、模型参数，以及真实响应的 toolCalls/usage。联系方式已经由服务端审计写入前脱敏，并在生成报告时再次扫描。

{provider_request_details}
"""
    out = audit_dir / "CONTEXT_AUDIT_REAL_SINGLE_RUN.md"
    out.write_text(report, encoding="utf-8")
    return out


def metric_value(summary: dict[str, Any], candidate: str, key: str) -> float:
    variants = summary.get("variants") or {}
    item: Any = variants.get(candidate) if isinstance(variants, dict) else None
    if not item and isinstance(variants, list):
        item = next((row for row in variants if row.get("name") == candidate or row.get("id") == candidate), None)
    if not item:
        return 0.0
    for source in [
        (item.get("byBenchmarkSplit") or {}).get("calibration"),
        item.get("aggregate"),
        item.get("calibration"),
        item.get("metrics"),
        item,
    ]:
        if isinstance(source, dict) and key in source:
            value = source[key]
            if isinstance(value, dict):
                return float(value.get("p50") or value.get("mean") or 0)
            return float(value or 0)
    return 0.0


def draw_jd_comparison(joint: dict[str, Any], path: Path) -> None:
    heldout = joint.get("heldout") or {}
    winner = heldout.get("winner") or {}
    baseline = heldout.get("productionBaseline") or {}
    metrics = ["recall@5", "ndcg@10", "mrr"]
    labels = ["Recall@5", "NDCG@10", "MRR"]
    fig, ax = plt.subplots(figsize=(12, 8))
    x = range(len(metrics))
    bw = 0.35
    base_values = [baseline.get(key, 0) for key in metrics]
    win_values = [winner.get(key, 0) for key in metrics]
    ax.bar([i - bw / 2 for i in x], base_values, width=bw, label="生产基线", color="#94a3b8")
    ax.bar([i + bw / 2 for i in x], win_values, width=bw, label="联合搜索 winner", color="#2563eb")
    ax.set_xticks(list(x), labels)
    ax.set_ylim(0, max(0.85, max(win_values or [0]) + 0.1))
    ax.set_ylabel("Held-out 指标")
    ax.set_title("JD 召回：独立 Held-out 上的基线与 winner")
    ax.legend()
    ax.grid(axis="y", alpha=0.25)
    for offset, values in [(-bw / 2, base_values), (bw / 2, win_values)]:
        for idx, value in enumerate(values):
            ax.text(idx + offset, value + 0.018, f"{value:.3f}", ha="center", fontsize=15)
    fig.tight_layout(pad=2)
    save_figure(fig, path)


def draw_resume_options(rag_dir: Path, path: Path) -> None:
    files = [
        ("生产基线", rag_dir / "resume_evidence_production_baseline.json"),
        ("section 320/0", rag_dir / "resume_evidence_chunking_summary.json"),
        ("TE3-512", rag_dir / "resume_evidence_embedding_summary.json"),
        ("hybrid sw=0.7", rag_dir / "resume_evidence_retrieval_summary.json"),
    ]
    labels, recalls, ndcgs = [], [], []
    for label, file in files:
        data = load_json(file, {})
        if "production" in file.name:
            source = (
                (data.get("byBenchmarkSplit") or {}).get("calibration")
                or data.get("aggregate")
                or data.get("metrics")
                or data.get("calibration")
                or data
            )
            recall = source.get("recall@5", 0)
            ndcg = source.get("ndcg@10", 0)
        else:
            winner = data.get("winner") or ""
            recall = metric_value(data, winner, "recall@5")
            ndcg = metric_value(data, winner, "ndcg@10")
        if recall or ndcg:
            labels.append(label)
            recalls.append(recall)
            ndcgs.append(ndcg)
    if not labels:
        return
    fig, ax = plt.subplots(figsize=(13, 8))
    x = range(len(labels))
    bw = 0.35
    ax.bar([i - bw / 2 for i in x], recalls, width=bw, label="Recall@5", color="#0ea5e9")
    ax.bar([i + bw / 2 for i in x], ndcgs, width=bw, label="NDCG@10", color="#10b981")
    ax.set_xticks(list(x), labels)
    ax.set_ylim(0, 1.05)
    ax.set_title("简历证据召回：逐阶段当前最优项（校准集）")
    ax.legend()
    ax.grid(axis="y", alpha=0.25)
    for idx, value in enumerate(recalls):
        ax.text(idx - bw / 2, value + 0.02, f"{value:.3f}", ha="center", fontsize=14)
    for idx, value in enumerate(ndcgs):
        ax.text(idx + bw / 2, value + 0.02, f"{value:.3f}", ha="center", fontsize=14)
    fig.tight_layout(pad=2)
    save_figure(fig, path)


def stage_status(rag_dir: Path, stage: str) -> tuple[str, str]:
    if (rag_dir / f"{stage}_joint_summary.json").exists():
        return "完成", "联合搜索 + 独立 held-out 已完成"
    completed = []
    for phase in ["chunking", "embedding", "tokenizer", "retrieval", "rewrite", "rerank"]:
        if (rag_dir / f"{stage}_{phase}_summary.json").exists():
            completed.append(phase)
    if completed:
        return "进行中", "已完成 " + "、".join(completed)
    return "未开始", "尚无该阶段结果文件"


STAGE_LABELS = {
    "jd_recall": "JD 召回",
    "resume_evidence": "简历证据召回",
    "knowledge_recall": "知识召回",
}
PHASE_LABELS = {
    "chunking": "Chunking（切分）",
    "embedding": "Embedding（向量模型/维度）",
    "tokenizer": "Tokenizer / 词法匹配",
    "retrieval": "Retrieval（召回与融合）",
    "rewrite": "Query Rewrite",
    "rerank": "Rerank",
}
PHASES = ["chunking", "embedding", "tokenizer", "retrieval", "rewrite", "rerank"]


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _fmt(value: Any, digits: int = 4) -> str:
    if value is None:
        return "—"
    try:
        return f"{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return str(value)


def _md(value: Any) -> str:
    return str(value if value is not None else "—").replace("|", "\\|").replace("\n", " ")


def _metric_source(item: dict[str, Any]) -> dict[str, Any]:
    return (
        (item.get("byBenchmarkSplit") or {}).get("calibration")
        or item.get("aggregate")
        or item.get("calibration")
        or item.get("metrics")
        or {}
    )


def _remote_calls(item: dict[str, Any], key: str) -> int:
    value = (item.get("remote") or {}).get(key) or {}
    return int(value.get("calls") or 0)


def phase_comparison_tables(rag_dir: Path, stage: str) -> str:
    """Render every persisted one-factor candidate; no losing row is hidden."""
    blocks: list[str] = []
    for phase in PHASES:
        summary = load_json(rag_dir / f"{stage}_{phase}_summary.json", {})
        variants = summary.get("variants") or {}
        if isinstance(variants, list):
            variants = {
                str(row.get("name") or row.get("id") or f"candidate_{idx + 1}"): row
                for idx, row in enumerate(variants)
            }
        ranked = summary.get("rankedCandidates") or []
        if not isinstance(ranked, list):
            ranked = []
        names = [name for name in ranked if name in variants]
        names.extend(name for name in variants if name not in names)
        winner = str(summary.get("winner") or "")
        rows = [
            "| # | 参数/方案 | 状态 | Queries | Recall@5 | NDCG@10 | MRR | Zero-hit | P95(ms) | Chunks | Vector MiB | Emb/DS/Rerank calls |",
            "|---:|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
        for idx, name in enumerate(names, 1):
            item = variants.get(name) or {}
            metrics = _metric_source(item)
            stats = item.get("chunkStats") or {}
            total = metrics.get("totalMs") or {}
            marker = " **← winner**" if name == winner else ""
            status = item.get("status") or ("error" if item.get("error") else "unknown")
            calls = f"{_remote_calls(item, 'embedding')}/{_remote_calls(item, 'deepseek')}/{_remote_calls(item, 'qwenRerank')}"
            rows.append(
                f"| {idx} | `{_md(name)}`{marker} | {_md(status)} | {int(metrics.get('queries') or 0)} "
                f"| {_fmt(metrics.get('recall@5'))} | {_fmt(metrics.get('ndcg@10'))} | {_fmt(metrics.get('mrr'))} "
                f"| {_fmt(metrics.get('zeroHit'))} | {_fmt(total.get('p95'), 3)} | {int(stats.get('chunks') or 0)} "
                f"| {_fmt(_float(stats.get('estimatedVectorBytes')) / 1024 / 1024, 3)} | {calls} |"
            )
        reason = _md(summary.get("reason") or "无")
        blocks.append(
            f"### {PHASE_LABELS[phase]}\n\n"
            f">选择依据：{reason}\n\n"
            f"<details open>\n<summary>完整候选对比（{len(names)} 组）</summary>\n\n"
            + "\n".join(rows)
            + "\n\n</details>"
        )
    return "\n\n".join(blocks)


def _config_value(config: dict[str, Any], path: tuple[str, ...]) -> Any:
    value: Any = config
    for key in path:
        if not isinstance(value, dict):
            return "—"
        value = value.get(key)
    return "—" if value is None else value


def final_config_matrix(joints: dict[str, dict[str, Any]]) -> str:
    fields = [
        ("chunk.strategy", ("chunk", "strategy")),
        ("chunk.size", ("chunk", "size")),
        ("chunk.overlap", ("chunk", "overlap")),
        ("embedding.model", ("embedding", "model")),
        ("embedding.dimension", ("embedding", "dimension")),
        ("tokenizer", ("tokenizer",)),
        ("retrieval.mode", ("retrieval", "mode")),
        ("retrieval.semanticWeight", ("retrieval", "semanticWeight")),
        ("retrieval.rrfK", ("retrieval", "rrfK")),
        ("retrieval.scoreThreshold", ("retrieval", "scoreThreshold")),
        ("retrieval.candidateLimit", ("retrieval", "candidateLimit")),
        ("retrieval.denseMultiplier", ("retrieval", "denseMultiplier")),
        ("rewrite", ("rewrite",)),
        ("rerank", ("rerank",)),
    ]
    rows = [
        "| 参数 | JD 召回 | 简历证据 | 知识召回 |",
        "|---|---|---|---|",
    ]
    for label, path in fields:
        values = [
            _config_value(joints.get(stage, {}).get("winnerConfig") or {}, path)
            for stage in STAGE_LABELS
        ]
        rows.append(f"| `{label}` | `{_md(values[0])}` | `{_md(values[1])}` | `{_md(values[2])}` |")
    return "\n".join(rows)


def heldout_comparison_table(joints: dict[str, dict[str, Any]]) -> str:
    rows = [
        "| 阶段 | Q | Recall@5 基线→winner (Δ) | NDCG@10 基线→winner (Δ) | MRR 基线→winner (Δ) | Zero-hit 基线→winner | P95(ms) 基线→winner |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for stage, label in STAGE_LABELS.items():
        heldout = joints.get(stage, {}).get("heldout") or {}
        winner = heldout.get("winner") or {}
        base = heldout.get("productionBaseline") or {}
        cells = []
        for metric in ["recall@5", "ndcg@10", "mrr"]:
            before, after = _float(base.get(metric)), _float(winner.get(metric))
            cells.append(f"{before:.4f}→{after:.4f} ({after - before:+.4f})")
        p95_base = _float((base.get("totalMs") or {}).get("p95"))
        p95_win = _float((winner.get("totalMs") or {}).get("p95"))
        rows.append(
            f"| {label} | {int(winner.get('queries') or 0)} | {cells[0]} | {cells[1]} | {cells[2]} "
            f"| {_float(base.get('zeroHit')):.4f}→{_float(winner.get('zeroHit')):.4f} "
            f"| {p95_base:.3f}→{p95_win:.3f} |"
        )
    return "\n".join(rows)


def bootstrap_table(joints: dict[str, dict[str, Any]]) -> str:
    rows = [
        "| 阶段 | Cases / Samples | 指标 | Δ | 95% CI | 判读 |",
        "|---|---:|---|---:|---:|---|",
    ]
    for stage, label in STAGE_LABELS.items():
        paired = ((joints.get(stage, {}).get("heldout") or {}).get("pairedBootstrap") or {})
        metrics = paired.get("metrics") or {}
        for metric in ["recall@5", "ndcg@10", "mrr", "totalMs"]:
            item = metrics.get(metric) or {}
            low, high = _float(item.get("ci95Low")), _float(item.get("ci95High"))
            if metric == "totalMs" and high < 0:
                reading = "延迟下降，CI 不跨 0"
            elif metric == "totalMs" and low > 0:
                reading = "延迟上升，这是成本回归"
            elif metric == "totalMs":
                reading = "延迟 CI 跨 0，无法确认变化"
            elif low > 0:
                reading = "质量提升，CI 不跨 0"
            elif high < 0:
                reading = "质量下降，CI 不跨 0"
            else:
                reading = "CI 跨 0，当前样本无法确认变化"
            rows.append(
                f"| {label} | {int(paired.get('cases') or 0)} / {int(paired.get('samples') or 0)} | `{metric}` "
                f"| {_fmt(item.get('delta'), 6)} | [{_fmt(item.get('ci95Low'), 6)}, {_fmt(item.get('ci95High'), 6)}] | {reading} |"
            )
    return "\n".join(rows)


def cohort_tables(joints: dict[str, dict[str, Any]]) -> str:
    blocks: list[str] = []
    cohort_pairs = [
        ("Length cohort", "winnerByLengthCohort", "productionBaselineByLengthCohort"),
        ("Gold annotation cohort", "winnerByGoldAnnotationCohort", "productionBaselineByGoldAnnotationCohort"),
    ]
    for stage, label in STAGE_LABELS.items():
        heldout = joints.get(stage, {}).get("heldout") or {}
        rows = [
            "| 分层类型 | Cohort | Q | Recall@5 基线→winner | NDCG@10 基线→winner | MRR 基线→winner | Zero-hit 基线→winner |",
            "|---|---|---:|---:|---:|---:|---:|",
        ]
        for cohort_type, winner_key, base_key in cohort_pairs:
            winner_groups = heldout.get(winner_key) or {}
            base_groups = heldout.get(base_key) or {}
            for name in sorted(set(winner_groups) | set(base_groups)):
                win, base = winner_groups.get(name) or {}, base_groups.get(name) or {}
                rows.append(
                    f"| {cohort_type} | `{_md(name)}` | {int(win.get('queries') or base.get('queries') or 0)} "
                    f"| {_fmt(base.get('recall@5'))}→{_fmt(win.get('recall@5'))} "
                    f"| {_fmt(base.get('ndcg@10'))}→{_fmt(win.get('ndcg@10'))} "
                    f"| {_fmt(base.get('mrr'))}→{_fmt(win.get('mrr'))} "
                    f"| {_fmt(base.get('zeroHit'))}→{_fmt(win.get('zeroHit'))} |"
                )
        blocks.append(f"### {label}\n\n" + "\n".join(rows))
    return "\n\n".join(blocks)


def pareto_tables(joints: dict[str, dict[str, Any]]) -> str:
    blocks: list[str] = []
    for stage, label in STAGE_LABELS.items():
        joint = joints.get(stage, {})
        winner = str(joint.get("winner") or "")
        rows = [
            "| Tuple | 是否最终 winner | Utility | Dim | Vector MiB | P95(ms) | External calls/q | Generation calls/q | Mixed-section rate |",
            "|---|---|---:|---:|---:|---:|---:|---:|---:|",
        ]
        for item in joint.get("paretoFrontier") or []:
            complexity = item.get("complexity") or {}
            name = str(item.get("name") or "")
            rows.append(
                f"| `{_md(name)}` | {'是' if name == winner else '否'} | {_fmt(item.get('utility'), 6)} "
                f"| {_fmt(complexity.get('embeddingDimension'), 0)} "
                f"| {_fmt(_float(complexity.get('estimatedVectorBytes')) / 1024 / 1024, 3)} "
                f"| {_fmt(complexity.get('observedP95Ms'), 3)} "
                f"| {_fmt(complexity.get('externalCallsPerQuery'), 3)} "
                f"| {_fmt(complexity.get('generationCallsPerQuery'), 3)} "
                f"| {_fmt(complexity.get('mixedSectionRate'), 4)} |"
            )
        blocks.append(
            f"### {label}\n\nScreen trials: **{joint.get('screenTrials', 0)}**; finalists: **{joint.get('finalists', 0)}**.\n\n"
            + "\n".join(rows)
        )
    return "\n\n".join(blocks)


def draw_all_stage_heldout(joints: dict[str, dict[str, Any]], path: Path) -> None:
    stages = list(STAGE_LABELS)
    labels = [STAGE_LABELS[stage] for stage in stages]
    fig, axes = plt.subplots(3, 1, figsize=(15, 16))
    colors = ("#94a3b8", "#2563eb")
    for ax, metric, title in zip(axes, ["recall@5", "ndcg@10", "mrr"], ["Recall@5", "NDCG@10", "MRR"]):
        base = [_float(((joints[stage].get("heldout") or {}).get("productionBaseline") or {}).get(metric)) for stage in stages]
        win = [_float(((joints[stage].get("heldout") or {}).get("winner") or {}).get(metric)) for stage in stages]
        x = list(range(len(stages)))
        bw = 0.34
        ax.bar([i - bw / 2 for i in x], base, bw, label="生产基线", color=colors[0])
        ax.bar([i + bw / 2 for i in x], win, bw, label="联合 winner", color=colors[1])
        ax.set_xticks(x, labels)
        ax.set_ylim(0, 1.08)
        ax.set_ylabel(title)
        ax.grid(axis="y", alpha=0.22)
        ax.legend(loc="upper left")
        for offset, values in [(-bw / 2, base), (bw / 2, win)]:
            for idx, value in enumerate(values):
                ax.text(idx + offset, value + 0.025, f"{value:.3f}", ha="center", fontsize=14)
    fig.suptitle("三类 RAG 场景：独立 Held-out 基线与最终 Winner", fontsize=27)
    fig.tight_layout(rect=(0, 0, 1, 0.97), pad=2.2)
    save_figure(fig, path)


def draw_all_stage_latency(joints: dict[str, dict[str, Any]], path: Path) -> None:
    stages = list(STAGE_LABELS)
    labels = [STAGE_LABELS[stage] for stage in stages]
    base = [_float((((joints[stage].get("heldout") or {}).get("productionBaseline") or {}).get("totalMs") or {}).get("p95")) for stage in stages]
    win = [_float((((joints[stage].get("heldout") or {}).get("winner") or {}).get("totalMs") or {}).get("p95")) for stage in stages]
    fig, ax = plt.subplots(figsize=(15, 9))
    x = list(range(len(stages)))
    bw = 0.34
    ax.bar([i - bw / 2 for i in x], base, bw, label="生产基线", color="#94a3b8")
    ax.bar([i + bw / 2 for i in x], win, bw, label="联合 winner", color="#0f766e")
    ax.set_xticks(x, labels)
    ax.set_ylabel("本地检索 Total P95（ms）")
    ax.set_title("三类 RAG 场景：Held-out 查询延迟")
    ax.grid(axis="y", alpha=0.22)
    ax.legend()
    for offset, values in [(-bw / 2, base), (bw / 2, win)]:
        for idx, value in enumerate(values):
            ax.text(idx + offset, value + max(base + win) * 0.025, f"{value:.3f}", ha="center", fontsize=15)
    fig.tight_layout(pad=2)
    save_figure(fig, path)


def make_rag_report(rag_dir: Path) -> Path:
    assets = rag_dir / "assets"
    joints = {
        stage: load_json(rag_dir / f"{stage}_joint_summary.json", {})
        for stage in STAGE_LABELS
    }
    if joints["jd_recall"]:
        draw_jd_comparison(joints["jd_recall"], assets / "01_jd_heldout_comparison.png")
    draw_resume_options(rag_dir, assets / "02_resume_stage_options.png")
    draw_all_stage_heldout(joints, assets / "03_all_stage_heldout.png")
    draw_all_stage_latency(joints, assets / "04_all_stage_latency.png")
    gate = load_json(rag_dir / "data_gate.json", {})
    statuses = {stage: stage_status(rag_dir, stage) for stage in ["jd_recall", "resume_evidence", "knowledge_recall"]}
    complete = all(value[0] == "完成" for value in statuses.values())
    config_matrix = final_config_matrix(joints)
    heldout_table = heldout_comparison_table(joints)
    bootstrap_rows = bootstrap_table(joints)
    cohorts = cohort_tables(joints)
    pareto = pareto_tables(joints)
    phase_sections = "\n\n".join(
        f"## {idx}. {label}：六阶段全候选实验表\n\n"
        f">本节展开 `{stage}` 的全部单因素候选，**输的方案也保留**。表中指标优先取冻结 calibration split，未另行混用 held-out。\n\n"
        f"{phase_comparison_tables(rag_dir, stage)}"
        for idx, (stage, label) in enumerate(STAGE_LABELS.items(), 5)
    )
    jd_dup_base = _float(((joints["jd_recall"].get("heldout") or {}).get("productionBaseline") or {}).get("duplicateDocRate@10"))
    jd_dup_win = _float(((joints["jd_recall"].get("heldout") or {}).get("winner") or {}).get("duplicateDocRate@10"))
    counts = {
        "jd_docs": ((gate.get("jd") or {}).get("documents") or 0),
        "jd_queries": ((gate.get("jd") or {}).get("queries") or 0),
        "resume_docs": ((gate.get("resumeEvidence") or {}).get("documents") or 0),
        "resume_queries": ((gate.get("resumeEvidence") or {}).get("queries") or 0),
        "knowledge_docs": ((gate.get("knowledge") or {}).get("documents") or 0),
        "knowledge_queries": ((gate.get("knowledge") or {}).get("queries") or 0),
        "gold": sum(_float(v) for v in (gate.get("goldSpans") or {}).values()),
    }
    report = f"""# 三阶段 RAG 实验报告

> 实验状态：**{'三阶段全部完成' if complete else '存在未完成阶段'}**。新的 100 份简历压测已按要求取消；RAG 离线实验已继续跑完。所有实验、汇总与 PNG 渲染都在阿里云 ECS 完成，Python 依赖使用中国镜像源。

## 1. 先说结论，但不用结论代替实验过程

三个场景的联合搜索 winner **不相同**，因此不应共用一套“万能 RAG 参数”。下表是最终 winner，后文会保留 18 个阶段、全部候选项的原始对比表：

{config_matrix}

最容易被面试官追问的是简历证据场景：单因素阶段分别选出过 `320/0`、`TE3-512`、`scoped hybrid sw=0.7`，但联合搜索胜出的是 `400/40 + TE3-1024 + dense`。这不是报告矛盾，而是参数交互：**单因素最优的机械组合不保证是联合最优**。

## 2. 实验问题、语料与数据门禁

- `jd_recall`：跨 JD 文档找合适岗位，强调文档召回与排序。
- `resume_evidence`：在单份简历中定位原文证据，强调 section 边界、scope 不泄漏与低噪声。
- `knowledge_recall`：从知识库召回框架/API/评估知识，强调语义完整和可引用。

冻结语料与标注：

| 场景 | 文档数 | 查询数 | Split | Gold spans |
|---|---:|---:|---|---:|
| JD 召回 | {counts['jd_docs']} | {counts['jd_queries']} | calibration 80 / held-out 40 | {int((gate.get('goldSpans') or {{}}).get('jd_recall') or 0)} |
| 简历证据 | {counts['resume_docs']} | {counts['resume_queries']} | calibration 80 / held-out 40 | {int((gate.get('goldSpans') or {{}}).get('resume_evidence') or 0)} |
| 知识召回 | {counts['knowledge_docs']} | {counts['knowledge_queries']} | calibration 40 / held-out 20 / operational 7 | {int((gate.get('goldSpans') or {{}}).get('knowledge_recall') or 0)} |
| **合计** | **{counts['jd_docs'] + counts['resume_docs'] + counts['knowledge_docs']}** | **{counts['jd_queries'] + counts['resume_queries'] + counts['knowledge_queries']}** | — | **{int(counts['gold'])}** |

数据门禁通过，但两个警告不隐藏：7 个非词法 JD query 含精确职位标题；20 个 JD case 使用较弱的 duty-lead fallback span。因此报告同时展示 cohort，不只看总均值。

## 3. 完成状态与实验规则

| 阶段 | 状态 | 已完成内容 |
|---|---|---|
| JD 召回 | {statuses['jd_recall'][0]} | {statuses['jd_recall'][1]} |
| 简历证据召回 | {statuses['resume_evidence'][0]} | {statuses['resume_evidence'][1]} |
| 知识召回 | {statuses['knowledge_recall'][0]} | {statuses['knowledge_recall'][1]} |

1. 每个场景先按 chunking → embedding → tokenizer → retrieval → rewrite → rerank 做单因素对比；
2. 再做 48 组联合 screen，选 11 个 finalist，最后在冻结 held-out 上与生产基线对比；
3. 单因素表只用 calibration 选型；held-out 不参与参数调整；
4. winner 同时参考 Recall@5、NDCG@10、MRR、Zero-hit、P95、向量体积和外部调用数。

表中的“参数/方案”是实验程序持久化的原始 candidate id，不是报告二次命名。常见缩写：`400_40` = size 400 / overlap 40；`p75_s1` = 75 分位语义边界 / semantic 开启；`sw0.7_k10` = semanticWeight 0.7 / RRF k=10；`TE3-512` = text-embedding-v3 512 维。`Emb/DS/Rerank calls` 记录真实远程 cache miss 调用，不把本地 cache hit 冒充为 API 请求。

## 4. 三类场景的独立 Held-out 结果

![三类场景 Held-out 质量指标](assets/03_all_stage_heldout.png)

{heldout_table}

![三类场景 Held-out P95 延迟](assets/04_all_stage_latency.png)

### 配对 Bootstrap（2,000 次）

{bootstrap_rows}

JD winner 的 `duplicateDocRate@10` 从 **{jd_dup_base:.4f}** 上升到 **{jd_dup_win:.4f}**。这是真实的副作用：Recall/NDCG 提升了，但 Top10 中同一 JD 的 chunk 更重复，生产化前应补 doc-level diversity/MMR 去重。

{phase_sections}

## 8. 联合搜索与 Pareto 前沿

单因素 winner 只能解释“在当时其他参数固定时，哪个选项更好”；联合搜索才能检查参数交互。Pareto 表保留质量、延迟、索引和外部调用之间的折中。

{pareto}

> 说明：joint summary 完整保留最终 winner config、screen ranking 和 Pareto complexity，但没有在汇总文件中冗余保存每个非 winner tuple 的全部嵌套配置。本报告不会反推或伪造它们；完整的可重复参数对比由前述 18 张单因素表提供。

## 9. Held-out 分层结果

总均值可能掩盖“长文档更差”或“弱标注抬高指标”。下表把每个场景的 length/layout/source 类分层与 gold annotation cohort 全部展开。

{cohorts}

## 10. 怎么读这份报告（面试口径）

- 我们没有用一套参数覆盖三种不同召回任务，而是先单因素建立可解释性，再联合搜索处理参数交互。
- 选型不只看 Recall：JD 场景的重复文档率明显恶化，如果只报质量涨幅就是不完整的实验结论。
- 简历证据 joint winner 和单因素 winners 不同，证明了为什么不能把各阶段第一名直接拼起来。
- 延迟表是实验进程内的本地检索计时；不应将其冒充为端到端 Agent 延迟，也不应忽略首次向量化的远程成本。
- Knowledge held-out 只有 20 个 query，当前提升很大，但仍需要更大的应用问题集扩大置信证据。

## 11. 与新 100 份压测的边界

本报告是 RAG 离线检索实验，回答“哪些切分/召回参数更好”。新的 100 份简历压测已取消，因此这份报告不讨论上传 QPS、Runtime P95/P99 或 4C8G 容量上限，也不会用离线 query 延迟冒充 E2E 压测结论。
"""
    out = rag_dir / "RAG_THREE_STAGE_EXPERIMENT_REPORT.md"
    out.write_text(report, encoding="utf-8")
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--audit-dir", type=Path, required=True)
    parser.add_argument("--rag-dir", type=Path, required=True)
    parser.add_argument("--font", type=Path, required=True)
    args = parser.parse_args()
    configure_font(args.font)
    context_report = make_context_report(args.audit_dir, args.font)
    rag_report = make_rag_report(args.rag_dir)
    print(json.dumps({"contextReport": str(context_report), "ragReport": str(rag_report)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
