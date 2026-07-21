#!/usr/bin/env python3
"""EXP-8: function calling vs json_object on the REAL decision schema.

Replays the production `emit_decision` tool schema (workflow/app/runtime/
executor.py) against DeepSeek with two decision channels:
  A) forced function calling (tools + tool_choice)   — production main path
  B) response_format json_object + schema-in-prompt  — production fallback

Metrics per arm: one-pass schema validity, repair-needed rate (parseable only
after stripping fences), hard failure rate, latency p50/p95.

Usage (ECS):
  python3 harness/run_json_ab.py --calls 60 --out reports/experiments
"""
from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]

EMIT_DECISION_TOOL = {
    "type": "function",
    "function": {
        "name": "emit_decision",
        "description": "提交本轮 agent 决策（json）：思考、需要的工具调用、结构化输出。",
        "parameters": {
            "type": "object",
            "properties": {
                "thought": {"type": "string", "description": "简要计划"},
                "toolCalls": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "tool": {"type": "string"},
                            "arguments": {"type": "object"},
                        },
                        "required": ["tool"],
                    },
                },
                "output": {
                    "type": "object",
                    "properties": {
                        "summary": {"type": "string"},
                        "claims": {"type": "array", "items": {"type": "object"}},
                        "evidence": {"type": "array", "items": {"type": "object"}},
                        "confidence": {"type": "number"},
                        "requestedNextAction": {"type": "string"},
                    },
                },
                "handoff": {
                    "type": "object",
                    "properties": {
                        "to": {"type": "string"},
                        "reason": {"type": "string"},
                        "task": {"type": "string"},
                    },
                },
                "done": {"type": "boolean"},
            },
        },
    },
}
FORCE = {"type": "function", "function": {"name": "emit_decision"}}

CONTEXTS = [
    ("tech", "你是技术评估 Agent。可用工具：calculate_jd_coverage(resumeText,jdText)、"
             "resume_semantic_search(query)、knowledge_search(query)。\n"
             "简历：5 年 Java 后端，Spring Boot 3 / MySQL / Redis / Kafka；自研 Agent 平台接入 "
             "DeepSeek，实现 Milvus+BM25 混合检索，QPS 800，P95 1.2s。\n"
             "JD：高级 Java / AI Agent 平台工程师，要求 RAG、Trace 可观测、Docker。\n"
             "请决定本轮动作：需要哪些工具调用（首轮通常先算 JD 覆盖率），或直接给出结构化结论。"),
    ("risk", "你是风险审查 Agent。可用工具：check_timeline(resumeText)、timeline_validator(entries)。\n"
             "简历工作经历：2019.03-2021.06 A 公司；2021.03-2023.01 B 公司；2023.05 至今 C 公司。\n"
             "请决定本轮动作：调用哪个工具核查时间线重叠，或直接输出风险结论（含 confidence）。"),
    ("report", "你是报告生成 Agent。可用工具：validate_report_schema(report)、knowledge_search(query)。\n"
               "已知发现：技术匹配 82 分（Java/Spring/RAG 均命中）；项目真实性中等（缺开源佐证）；"
               "时间线无重叠。请决定：先检索评分标准还是直接产出报告摘要（summary+claims+confidence）。"),
]

SCHEMA_PROMPT = (
    "输出必须是一个 json 对象，字段：thought(string)、toolCalls(array of "
    "{tool:string, arguments:object})、output(object: summary/claims/evidence/"
    "confidence/requestedNextAction)、handoff(object|省略)、done(boolean)。"
    "不要输出 json 以外的任何内容。")


def load_env() -> Dict[str, str]:
    env: Dict[str, str] = {}
    for candidate in ("/opt/resumai-src/.env", str(ROOT / ".deploy.local.env")):
        path = Path(candidate)
        if path.is_file():
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip() and not line.startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    env.setdefault(key.strip(), value.strip())
    return env


def call_deepseek(env: Dict[str, str], messages: List[Dict[str, str]], *,
                  use_fc: bool) -> Tuple[str, float]:
    body: Dict[str, Any] = {
        "model": env.get("DEEPSEEK_MODEL", "deepseek-chat"),
        "messages": messages,
        "max_tokens": 1024,
        "temperature": 0.2,
    }
    if use_fc:
        body["tools"] = [EMIT_DECISION_TOOL]
        body["tool_choice"] = FORCE
    else:
        body["response_format"] = {"type": "json_object"}
    request = urllib.request.Request(
        (env.get("DEEPSEEK_API_URL", "https://api.deepseek.com/v1").rstrip("/")
         + "/chat/completions"),
        data=json.dumps(body).encode("utf-8"),
        headers={"Authorization": "Bearer " + env["DEEPSEEK_API_KEY"],
                 "Content-Type": "application/json"},
        method="POST")
    started = time.monotonic()
    with urllib.request.urlopen(request, timeout=120) as response:
        payload = json.loads(response.read().decode("utf-8"))
    latency_ms = (time.monotonic() - started) * 1000
    message = payload["choices"][0]["message"]
    if use_fc:
        tool_calls = message.get("tool_calls") or []
        content = (tool_calls[0].get("function") or {}).get("arguments", "") if tool_calls else ""
    else:
        content = message.get("content") or ""
    return content, latency_ms


def validate(raw: str) -> str:
    """Returns: 'pass' (one-shot), 'repaired' (needed fence strip), 'fail'."""
    def check(text: str) -> bool:
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return False
        if not isinstance(data, dict):
            return False
        if "thought" in data and not isinstance(data["thought"], str):
            return False
        calls = data.get("toolCalls")
        if calls is not None:
            if not isinstance(calls, list):
                return False
            for item in calls:
                if not isinstance(item, dict) or not isinstance(item.get("tool"), str):
                    return False
        if "done" in data and not isinstance(data["done"], bool):
            return False
        # A decision must carry at least one actionable field.
        return bool(data.get("toolCalls") or data.get("output")
                    or data.get("handoff") or "done" in data)

    if check(raw):
        return "pass"
    stripped = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip())
    match = re.search(r"\{.*\}", stripped, re.DOTALL)
    if match and check(match.group(0)):
        return "repaired"
    return "fail"


def run_arm(env: Dict[str, str], *, use_fc: bool, calls: int) -> Dict[str, Any]:
    outcomes = {"pass": 0, "repaired": 0, "fail": 0}
    latencies: List[float] = []
    errors = 0
    for index in range(calls):
        name, context = CONTEXTS[index % len(CONTEXTS)]
        system = context if use_fc else context + "\n" + SCHEMA_PROMPT
        messages = [{"role": "system", "content": system},
                    {"role": "user", "content": f"第 {index // len(CONTEXTS) + 1} 轮：请给出决策。"}]
        try:
            raw, latency_ms = call_deepseek(env, messages, use_fc=use_fc)
            latencies.append(latency_ms)
            outcomes[validate(raw)] += 1
        except Exception as exc:  # noqa: BLE001 — network/API failures count as errors
            errors += 1
            print(f"  [{'fc' if use_fc else 'json'}#{index}] error: {exc}")
        if (index + 1) % 10 == 0:
            print(f"  [{'fc' if use_fc else 'json'}] {index + 1}/{calls} done")
    total = max(1, sum(outcomes.values()))
    latencies.sort()
    p95 = latencies[min(len(latencies) - 1, int(0.95 * (len(latencies) - 1)))] if latencies else 0
    return {
        "calls": calls,
        "onePassRate": round(outcomes["pass"] / total, 4),
        "repairedRate": round(outcomes["repaired"] / total, 4),
        "failRate": round(outcomes["fail"] / total, 4),
        "apiErrors": errors,
        "avgLatencyMs": round(statistics.mean(latencies), 1) if latencies else None,
        "p95LatencyMs": round(p95, 1) if latencies else None,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--calls", type=int, default=60)
    parser.add_argument("--out", default=str(ROOT / "reports" / "experiments"))
    args = parser.parse_args()
    env = load_env()
    if not env.get("DEEPSEEK_API_KEY"):
        print("DEEPSEEK_API_KEY missing")
        return 2
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"=== arm A: function calling ({args.calls} calls) ===")
    fc = run_arm(env, use_fc=True, calls=args.calls)
    print(json.dumps(fc, ensure_ascii=False))
    print(f"=== arm B: json_object ({args.calls} calls) ===")
    jo = run_arm(env, use_fc=False, calls=args.calls)
    print(json.dumps(jo, ensure_ascii=False))

    report = {
        "experiment": "EXP-8 function calling vs json_object",
        "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "model": env.get("DEEPSEEK_MODEL", "deepseek-chat"),
        "contexts": [c[0] for c in CONTEXTS],
        "results": {"function_calling": fc, "json_object": jo},
    }
    path = out_dir / "json_ab.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"report -> {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
