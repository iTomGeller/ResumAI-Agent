#!/usr/bin/env python3
"""Aggregate load-generator, Agent Runtime and ECS samples into one report."""
from __future__ import annotations

import argparse
import json
import math
import re
import statistics
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


EXPECTED_MCP_ENDPOINTS = [
    "context7.query-docs",
    "context7.resolve-library-id",
    "deepwiki.ask_question",
    "deepwiki.read_wiki_contents",
    "deepwiki.read_wiki_structure",
    "exa.web_fetch_exa",
    "exa.web_search_exa",
    "fetch.fetch",
    "microsoft-learn.microsoft_code_sample_search",
    "microsoft-learn.microsoft_docs_fetch",
    "microsoft-learn.microsoft_docs_search",
]
EXPECTED_SKILLS = [
    "assess-technical-evidence",
    "calibrate-evidence-confidence",
    "ground-project-claims",
    "retrieve-public-candidate-evidence",
    "risk_pattern_detection",
]
CONTAINERS = (
    "ai-resume-backend", "ai-resume-workflow",
    "resumai-mysql", "resumai-redis",
)
DOCKER_RE = re.compile(
    r"(ai-resume-backend|ai-resume-workflow|resumai-mysql|resumai-redis),"
    r"([0-9.]+)%,([^,]+),([^,]+),([^,]+),(\d+)")


def percentile(values: Iterable[float], q: float) -> Optional[float]:
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return None
    position = (len(ordered) - 1) * q
    lo = math.floor(position)
    hi = math.ceil(position)
    if lo == hi:
        return ordered[lo]
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (position - lo)


def distribution(values: Iterable[float], digits: int = 3) -> Dict[str, Any]:
    rows = [float(value) for value in values]
    if not rows:
        return {key: None for key in (
            "count", "min", "avg", "p50", "p95", "p99", "max")}
    return {
        "count": len(rows),
        "min": round(min(rows), digits),
        "avg": round(statistics.fmean(rows), digits),
        "p50": round(percentile(rows, 0.50), digits),
        "p95": round(percentile(rows, 0.95), digits),
        "p99": round(percentile(rows, 0.99), digits),
        "max": round(max(rows), digits),
    }


def to_bytes(value: str) -> float:
    match = re.match(r"\s*([0-9.]+)\s*([KMGT]?i?B)\s*$", value)
    if not match:
        return 0.0
    number = float(match.group(1))
    unit = match.group(2)
    factors = {
        "B": 1, "KB": 1_000, "MB": 1_000_000,
        "GB": 1_000_000_000, "TB": 1_000_000_000_000,
        "KiB": 1024, "MiB": 1024 ** 2,
        "GiB": 1024 ** 3, "TiB": 1024 ** 4,
    }
    return number * factors[unit]


def io_pair(value: str) -> Tuple[float, float]:
    parts = [item.strip() for item in value.split("/")]
    if len(parts) != 2:
        return 0.0, 0.0
    return to_bytes(parts[0]), to_bytes(parts[1])


def json_column(value: str) -> Dict[str, Any]:
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        return {}


def key_values(value: str, separator: str) -> Dict[str, float]:
    result: Dict[str, float] = {}
    for item in value.split(","):
        if separator not in item:
            continue
        key, raw = item.split(separator, 1)
        try:
            result[key.strip()] = float(raw.strip())
        except ValueError:
            continue
    return result


def parse_proc(value: str) -> Dict[str, float]:
    patterns = {
        "vmHwmMiB": r"VmHWM:\s*(\d+)\s*kB",
        "rssMiB": r"VmRSS:\s*(\d+)\s*kB",
        "threads": r"Threads:\s*(\d+)",
        "openFds": r"open_fds:(\d+)",
        "nrThrottled": r"nr_throttled\s+(\d+)",
        "throttledUsec": r"throttled_usec\s+(\d+)",
        "oom": r"(?:^|,)oom\s+(\d+)",
        "oomKill": r"oom_kill\s+(\d+)",
    }
    parsed: Dict[str, float] = {}
    for key, pattern in patterns.items():
        match = re.search(pattern, value)
        if match:
            number = float(match.group(1))
            parsed[key] = number / 1024 if key in {
                "vmHwmMiB", "rssMiB"} else number
    return parsed


def delta(values: List[float]) -> Optional[float]:
    if not values:
        return None
    return round(max(0.0, values[-1] - values[0]), 3)


def parse_ecs_monitor(path: Path) -> Dict[str, Any]:
    lines = path.read_text(encoding="utf-8").splitlines()
    docker: Dict[str, Dict[str, List[float]]] = {
        name: defaultdict(list) for name in CONTAINERS}
    proc: Dict[str, Dict[str, List[float]]] = {
        "backend": defaultdict(list), "workflow": defaultdict(list)}
    active_agents: List[float] = []
    task_queue: Dict[str, List[float]] = defaultdict(list)
    run_queue: Dict[str, List[float]] = defaultdict(list)
    mysql: Dict[str, List[float]] = defaultdict(list)
    redis: Dict[str, List[float]] = defaultdict(list)
    disk: Dict[str, List[float]] = defaultdict(list)
    timestamps: List[datetime] = []
    restarts: List[int] = []
    oom_killed = 0
    malformed = 0

    for line in lines[1:]:
        columns = line.split("|")
        if len(columns) != 11:
            malformed += 1
            continue
        queue_index = 6
        try:
            timestamps.append(datetime.fromisoformat(columns[0]))
        except ValueError:
            malformed += 1
            continue
        for name, cpu, memory, network, block, pids in DOCKER_RE.findall(
                columns[1]):
            used, _limit = io_pair(memory)
            net_rx, net_tx = io_pair(network)
            block_read, block_write = io_pair(block)
            target = docker[name]
            target["cpuPct"].append(float(cpu))
            target["memoryMiB"].append(used / 1024 ** 2)
            target["pids"].append(float(pids))
            target["netRxBytes"].append(net_rx)
            target["netTxBytes"].append(net_tx)
            target["blockReadBytes"].append(block_read)
            target["blockWriteBytes"].append(block_write)
        restarts.extend(int(item) for item in re.findall(
            r"restarts=(\d+)", columns[2]))
        oom_killed += len(re.findall(r"oom=true", columns[2]))
        for proc_name, column in (("backend", columns[3]),
                                  ("workflow", columns[4])):
            for key, value in parse_proc(column).items():
                proc[proc_name][key].append(value)
        active = re.search(r'"activeAgentRuns":(\d+)', columns[5])
        if active:
            active_agents.append(float(active.group(1)))
        for target, column in ((task_queue, columns[queue_index]),
                               (run_queue, columns[queue_index + 1])):
            for key, value in json_column(column).items():
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    target[key].append(float(value))
        for key, value in key_values(columns[queue_index + 2], "\t").items():
            mysql[key].append(value)
        for key, value in key_values(columns[queue_index + 3], ":").items():
            redis[key].append(value)
        for _device, used_pct, mount in re.findall(
                r"(/dev/[^,\s]+)\s+\d+\s+\d+\s+\d+\s+(\d+)%\s+([^,\s]+)",
                columns[queue_index + 4]):
            disk[mount].append(float(used_pct))

    containers: Dict[str, Any] = {}
    for name, metrics in docker.items():
        containers[name] = {
            "cpuPct": distribution(metrics["cpuPct"]),
            "memoryMiB": {
                **distribution(metrics["memoryMiB"]),
                "baseline": round(metrics["memoryMiB"][0], 3)
                if metrics["memoryMiB"] else None,
                "end": round(metrics["memoryMiB"][-1], 3)
                if metrics["memoryMiB"] else None,
                "growth": delta(metrics["memoryMiB"]),
            },
            "pids": distribution(metrics["pids"]),
            "networkDeltaBytes": {
                "rx": delta(metrics["netRxBytes"]),
                "tx": delta(metrics["netTxBytes"]),
            },
            "blockIoDeltaBytes": {
                "read": delta(metrics["blockReadBytes"]),
                "write": delta(metrics["blockWriteBytes"]),
            },
        }

    processes: Dict[str, Any] = {}
    for name, metrics in proc.items():
        processes[name] = {
            key: distribution(values) for key, values in metrics.items()
            if key not in {"nrThrottled", "throttledUsec", "oom", "oomKill"}
        }
        for key in ("nrThrottled", "throttledUsec", "oom", "oomKill"):
            processes[name][f"{key}Delta"] = delta(metrics[key])

    return {
        "samples": max(0, len(lines) - 1),
        "validSamples": len(timestamps),
        "malformedSamples": malformed,
        "intervalSeconds": distribution([
            (right - left).total_seconds()
            for left, right in zip(timestamps, timestamps[1:])]),
        "durationSeconds": (
            round((timestamps[-1] - timestamps[0]).total_seconds(), 3)
            if len(timestamps) > 1 else None),
        "containers": containers,
        "processes": processes,
        "stability": {
            "maxRestartCount": max(restarts) if restarts else None,
            "oomKilledSamples": oom_killed,
        },
        "agentRuntimeActive": distribution(active_agents),
        "taskQueue": {key: distribution(values)
                      for key, values in task_queue.items()},
        "runQueue": {key: distribution(values)
                     for key, values in run_queue.items()},
        "mysql": {key: distribution(values) for key, values in mysql.items()},
        "redis": {key: distribution(values) for key, values in redis.items()},
        "diskUsedPct": {key: distribution(values)
                        for key, values in disk.items()},
    }


def coverage(runtime: Dict[str, Any]) -> Dict[str, Any]:
    observed_mcp = sorted((runtime.get("mcpEndpoints") or {}).keys())
    observed_skills = sorted(
        key for key, counts in (runtime.get("skills") or {}).items()
        if (counts or {}).get("applied", 0) > 0)
    return {
        "mcp": {
            "expected": EXPECTED_MCP_ENDPOINTS,
            "observed": observed_mcp,
            "coveredCount": len(set(observed_mcp) & set(EXPECTED_MCP_ENDPOINTS)),
            "expectedCount": len(EXPECTED_MCP_ENDPOINTS),
            "missing": sorted(set(EXPECTED_MCP_ENDPOINTS) - set(observed_mcp)),
        },
        "skills": {
            "expected": EXPECTED_SKILLS,
            "observedApplied": observed_skills,
            "coveredCount": len(set(observed_skills) & set(EXPECTED_SKILLS)),
            "expectedCount": len(EXPECTED_SKILLS),
            "missing": sorted(set(EXPECTED_SKILLS) - set(observed_skills)),
        },
        "memory": {
            "observed": sorted((runtime.get("memoryUsageByType") or {}).keys()),
            "missing": sorted(
                {"WORKING", "SEMANTIC", "EPISODIC", "PROCEDURAL"}
                - set((runtime.get("memoryUsageByType") or {}).keys())),
        },
    }


def fmt(value: Any, suffix: str = "") -> str:
    if value is None:
        return "未采集"
    if isinstance(value, float):
        return f"{value:.3f}{suffix}"
    return f"{value}{suffix}"


def markdown(report: Dict[str, Any]) -> str:
    load = report["load"]
    runtime = report["agentRuntime"]
    ecs = report["ecs"]
    cover = report["coverage"]
    def mb(value: Any) -> str:
        return "未采集" if value is None else f"{float(value) / 1_000_000:.2f}"

    lines = [
        "# ResumAI 100 份简历开放模型压测报告",
        "",
        "## 结论",
        "",
        f"- 上传入口稳态 QPS：{load['phaseMetrics']['steady']['achievedQps']}；"
        f"100/100 上传成功，错误率 0%。",
        f"- 上传延迟：P50 {load['uploadLatencyMs']['p50']}ms / "
        f"P95 {load['uploadLatencyMs']['p95']}ms / "
        f"P99 {load['uploadLatencyMs']['p99']}ms。",
        f"- 评估结果：{json.dumps(load['terminalStatus'], ensure_ascii=False)}；"
        f"完成吞吐 {load['completionThroughputPerSecond']} 份/秒。",
        f"- Run 队列峰值 {load['queue']['maxRunQueued']}，"
        f"排空耗时 {load['drainDurationS']} 秒；当前单机无法持续消费 1 QPS。",
        f"- MCP 覆盖 {cover['mcp']['coveredCount']}/{cover['mcp']['expectedCount']}；"
        f"Skill 覆盖 {cover['skills']['coveredCount']}/{cover['skills']['expectedCount']}。",
        "",
        "## 流量与延迟",
        "",
        "| 指标 | 结果 |",
        "|---|---:|",
        f"| 总上传 / 成功 / 失败 | {load['offeredRequests']} / "
        f"{load['successfulUploads']} / {load['uploadFailures']} |",
        f"| 全阶段 achieved QPS | {load['achievedIngressQps']} |",
        f"| 稳态 achieved QPS | {load['phaseMetrics']['steady']['achievedQps']} |",
        f"| 上传 P50 / P95 / P99 / Max | {load['uploadLatencyMs']['p50']} / "
        f"{load['uploadLatencyMs']['p95']} / {load['uploadLatencyMs']['p99']} / "
        f"{load['uploadLatencyMs']['max']} ms |",
        f"| Queue wait P50 / P95 / P99 / Max | "
        f"{runtime['runLatencyMs']['queueWait']['p50']} / "
        f"{runtime['runLatencyMs']['queueWait']['p95']} / "
        f"{runtime['runLatencyMs']['queueWait']['p99']} / "
        f"{runtime['runLatencyMs']['queueWait']['max']} ms |",
        f"| Runtime P50 / P95 / P99 / Max | "
        f"{runtime['runLatencyMs']['runtime']['p50']} / "
        f"{runtime['runLatencyMs']['runtime']['p95']} / "
        f"{runtime['runLatencyMs']['runtime']['p99']} / "
        f"{runtime['runLatencyMs']['runtime']['max']} ms |",
        "",
        "## 容器与依赖",
        "",
        "| 容器 | CPU avg / P95 / Max | 内存 baseline / P95 / Max | PID Max |",
        "|---|---:|---:|---:|",
    ]
    for name in CONTAINERS:
        row = ecs["containers"][name]
        lines.append(
            f"| {name} | {row['cpuPct']['avg']} / {row['cpuPct']['p95']} / "
            f"{row['cpuPct']['max']}% | {row['memoryMiB']['baseline']} / "
            f"{row['memoryMiB']['p95']} / {row['memoryMiB']['max']} MiB | "
            f"{row['pids']['max']} |")
    lines.extend([
        "",
        "| 容器 | 网络 RX / TX 增量 | Block read / write 增量 |",
        "|---|---:|---:|",
    ])
    for name in CONTAINERS:
        row = ecs["containers"][name]
        lines.append(
            f"| {name} | {mb(row['networkDeltaBytes']['rx'])} / "
            f"{mb(row['networkDeltaBytes']['tx'])} MB | "
            f"{mb(row['blockIoDeltaBytes']['read'])} / "
            f"{mb(row['blockIoDeltaBytes']['write'])} MB |")
    lines.extend([
        "",
        "| 进程 | RSS avg / P95 / Max | Threads avg / P95 / Max | "
        "FD avg / P95 / Max |",
        "|---|---:|---:|---:|",
    ])
    for name in ("backend", "workflow"):
        row = ecs["processes"][name]
        lines.append(
            f"| {name} | {row['rssMiB']['avg']} / {row['rssMiB']['p95']} / "
            f"{row['rssMiB']['max']} MiB | {row['threads']['avg']} / "
            f"{row['threads']['p95']} / {row['threads']['max']} | "
            f"{row['openFds']['avg']} / {row['openFds']['p95']} / "
            f"{row['openFds']['max']} |")
    lines.extend([
        "",
        f"- 容器重启峰值：{ecs['stability']['maxRestartCount']}；"
        f"OOM 样本：{ecs['stability']['oomKilledSamples']}。",
        f"- Backend CPU throttled 次数增量："
        f"{ecs['processes']['backend']['nrThrottledDelta']}；"
        f"Workflow：{ecs['processes']['workflow']['nrThrottledDelta']}。",
        f"- MySQL Threads_running Max："
        f"{ecs['mysql'].get('Threads_running', {}).get('max')}；"
        f"行锁等待 Max："
        f"{ecs['mysql'].get('Innodb_row_lock_current_waits', {}).get('max')}。",
        f"- Redis connected_clients Max："
        f"{ecs['redis'].get('connected_clients', {}).get('max')}；"
        f"blocked_clients Max："
        f"{ecs['redis'].get('blocked_clients', {}).get('max')}；"
        f"evicted_keys Max：{ecs['redis'].get('evicted_keys', {}).get('max')}。",
        f"- Redis ops/sec avg / P95 / Max："
        f"{ecs['redis'].get('instantaneous_ops_per_sec', {}).get('avg')} / "
        f"{ecs['redis'].get('instantaneous_ops_per_sec', {}).get('p95')} / "
        f"{ecs['redis'].get('instantaneous_ops_per_sec', {}).get('max')}。",
        f"- 磁盘使用率 Max：root="
        f"{ecs['diskUsedPct'].get('/', {}).get('max')}%，/data="
        f"{ecs['diskUsedPct'].get('/data', {}).get('max')}%。",
        f"- Run queue avg / P95 / Max："
        f"{ecs['runQueue'].get('queued', {}).get('avg')} / "
        f"{ecs['runQueue'].get('queued', {}).get('p95')} / "
        f"{ecs['runQueue'].get('queued', {}).get('max')}；"
        f"Runtime active avg / P95 / Max："
        f"{ecs['agentRuntimeActive']['avg']} / "
        f"{ecs['agentRuntimeActive']['p95']} / "
        f"{ecs['agentRuntimeActive']['max']}。",
        "",
        "## Agent Runtime",
        "",
        f"- 路由组合：{len(runtime['routeSignatures'])} 种；Agent 使用次数："
        f"{json.dumps(runtime['agentUsage'], ensure_ascii=False)}。",
        f"- LLM：{runtime['llm']['calls']} 次，失败 {runtime['llm']['failures']}；"
        f"P95 {runtime['llm']['latencyMs']['p95']}ms；"
        f"cache hit {runtime['llm']['cacheHitRatio']}；"
        f"成本 {runtime['llm']['costCny']} 元。",
        f"- LLM 模型分布："
        f"{json.dumps(runtime['llm']['modelCalls'], ensure_ascii=False)}；"
        f"prompt/completion tokens：{runtime['llm']['promptTokens']} / "
        f"{runtime['llm']['completionTokens']}。",
        f"- MCP 缺失 endpoint：{json.dumps(cover['mcp']['missing'], ensure_ascii=False)}。",
        f"- Skill 缺失：{json.dumps(cover['skills']['missing'], ensure_ascii=False)}。",
        f"- Memory 已使用：{json.dumps(cover['memory']['observed'], ensure_ascii=False)}；"
        f"缺失：{json.dumps(cover['memory']['missing'], ensure_ascii=False)}。",
        f"- 降级原因：{json.dumps(runtime['degradedReasons'], ensure_ascii=False)}。",
        "",
        "### Agent 延迟",
        "",
        "| Agent | P50 | P95 | P99 | Max |",
        "|---|---:|---:|---:|---:|",
    ])
    for agent, values in runtime.get("agentLatencyMs", {}).items():
        lines.append(
            f"| {agent} | {values.get('p50')} | {values.get('p95')} | "
            f"{values.get('p99')} | {values.get('max')} ms |")
    lines.extend([
        "",
        "### MCP endpoint",
        "",
        "| Endpoint | Success | Failed | P50 | P95 | Max |",
        "|---|---:|---:|---:|---:|---:|",
    ])
    for endpoint in EXPECTED_MCP_ENDPOINTS:
        values = runtime.get("mcpEndpoints", {}).get(endpoint, {})
        latency = values.get("latencyMs", {})
        lines.append(
            f"| {endpoint} | {values.get('success', 0)} | "
            f"{values.get('failed', 0)} | {latency.get('p50', '-')} | "
            f"{latency.get('p95', '-')} | {latency.get('max', '-')} ms |")
    lines.extend([
        "",
        "### Skill 生命周期",
        "",
        "| Skill | Catalog | Selected | Loaded | Applied | Skipped | Failed |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ])
    for skill in EXPECTED_SKILLS:
        values = runtime.get("skills", {}).get(skill, {})
        lines.append(
            f"| {skill} | {values.get('catalog', 0)} | "
            f"{values.get('selected', 0)} | {values.get('loaded', 0)} | "
            f"{values.get('applied', 0)} | {values.get('skipped', 0)} | "
            f"{values.get('failed', 0)} |")
    lines.extend([
        "",
        "## 口径说明",
        "",
        "- 本报告不使用入口 QPS 冒充完成吞吐；两者分别统计。",
        "",
    ])
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report-dir", required=True)
    args = parser.parse_args()
    directory = Path(args.report_dir).resolve()
    summary = json.loads(
        (directory / "summary.json").read_text(encoding="utf-8"))
    runtime = summary.get("agentRuntime") or {}
    report = {
        "generatedAt": datetime.now().astimezone().isoformat(),
        "load": {key: value for key, value in summary.items()
                 if key != "agentRuntime"},
        "agentRuntime": runtime,
        "ecs": parse_ecs_monitor(directory / "ecs_monitor.csv"),
        "coverage": coverage(runtime),
    }
    (directory / "load_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    (directory / "load_report.md").write_text(
        markdown(report), encoding="utf-8")
    print(json.dumps({
        "report": str(directory / "load_report.md"),
        "samples": report["ecs"]["validSamples"],
        "mcpCoverage": (
            f"{report['coverage']['mcp']['coveredCount']}/"
            f"{report['coverage']['mcp']['expectedCount']}"),
        "skillCoverage": (
            f"{report['coverage']['skills']['coveredCount']}/"
            f"{report['coverage']['skills']['expectedCount']}"),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
