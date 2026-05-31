"""Patch Grafana dashboard JSON for Chinese variable options and PromQL label_replace."""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DASH_DIR = ROOT / "monitoring" / "grafana" / "provisioning" / "dashboards"

AGENT_MAP = {
    "OrchestratorAgent": "任务编排器",
    "ResumeParserAgent": "简历解析器",
    "TechAgent": "技术评估",
    "ProjectAgent": "项目评估",
    "RiskAgent": "风险识别",
    "JdMatchAgent": "岗位匹配",
    "RagasJudgeAgent": "质量校验",
    "FinalReportAgent": "报告生成",
    "DeepSeekChatModel": "大模型评估",
    "HybridRagStrategy": "混合检索",
    "JdAnalysisAgent": "需求分析",
    "HistoricalRagAgent": "历史匹配",
    "ExternalProfileAgent": "外部检索",
    "DAGEngine": "并发引擎",
}

STEP_MAP = {
    "task_create": "创建任务",
    "upload_parse": "上传解析",
    "resume_parse": "简历解析",
    "jd_match": "岗位匹配",
    "skill_eval": "能力评估",
    "rag_retrieve": "证据融合",
    "llm_complete": "AI 评估",
    "quality_check": "质量校验",
    "report_generate": "报告生成",
    "mcp_call": "外部检索",
    "tool_call": "工具调用",
    "rag_index_verify": "索引校验",
}

LANE_MAP = {"tech": "技术泳道", "project": "项目泳道", "risk": "风险泳道"}

RECOMMENDATION_MAP = {
    "NEED_MANUAL_REVIEW": "需人工复核",
    "RECOMMEND": "推荐",
    "STRONG_RECOMMEND": "强烈推荐",
    "REJECT": "拒绝",
}

TOOL_MAP = {
    "milvus.search": "Milvus 检索",
    "deepseek-chat": "DeepSeek 对话",
    "github": "GitHub MCP",
    "ResumeParserSkill": "简历解析 Skill",
    "TechStackAuditSkill": "技术栈 Skill",
    "ProjectDepthSkill": "项目深度 Skill",
    "RiskDetectionSkill": "风险识别 Skill",
}


def custom_query(mapping: dict[str, str]) -> str:
    return ",".join(f"{text} : {value}" for value, text in mapping.items())


def value_mapping_options(mapping: dict[str, str]) -> dict:
    options = {}
    for idx, (value, text) in enumerate(mapping.items()):
        options[value] = {"index": idx, "text": text}
    return options


def apply_value_mapping(defaults: dict, mapping: dict[str, str]) -> None:
    mappings = defaults.setdefault("mappings", [])
    mappings[:] = [m for m in mappings if m.get("type") != "value"]
    mappings.insert(0, {"type": "value", "options": value_mapping_options(mapping)})


def promql_relabel(expr: str, label: str, mapping: dict[str, str]) -> str:
    if not expr or "label_replace" in expr or label not in expr:
        return expr
    base = expr.strip()
    suffix = ""
    or_match = re.search(r"\s+or\s+vector\s*\(", base)
    if or_match:
        base = base[: or_match.start()].strip()
        suffix = expr[or_match.start() :]
    wrapped = base
    for raw, cn in mapping.items():
        wrapped = f'label_replace({wrapped}, "{label}", "{cn}", "{label}", "{raw}")'
    return wrapped + suffix


def patch_variable(var: dict, name: str, label: str, mapping: dict[str, str]) -> None:
    if var.get("name") != name:
        return
    var["type"] = "custom"
    var["label"] = label
    var["query"] = custom_query(mapping)
    var["includeAll"] = True
    var["allValue"] = ".*"
    var["multi"] = True
    var["current"] = {"selected": True, "text": "全部", "value": "$__all"}
    var["options"] = [{"selected": True, "text": "全部", "value": "$__all"}] + [
        {"selected": False, "text": cn, "value": raw} for raw, cn in mapping.items()
    ]


def sanitize_description(panel: dict) -> None:
    desc = panel.get("description") or ""
    if any(raw in desc for raw in RECOMMENDATION_MAP):
        panel["description"] = "按推荐结论统计：强烈推荐、推荐、需人工复核、拒绝。"
    desc = panel.get("description") or ""
    if "OrchestratorAgent" in desc or "step_kind" in desc:
        panel["description"] = re.sub(r"[A-Z_]{3,}[A-Za-z_]*", "", desc).strip() or "指标说明见面板标题。"


def should_relabel(expr: str, legend: str, label: str) -> bool:
    if label not in expr:
        return False
    if f"by ({label})" in expr or f"by({label})" in expr:
        return True
    if legend == f"{{{{{label}}}}}":
        return True
    if f"{{{{ {label}" in legend or f"{{{{.{label}" in legend:
        return True
    return False


def patch_target_expr(target: dict) -> None:
    expr = (target.get("expr") or "").strip()
    legend = target.get("legendFormat") or ""
    if not expr:
        return
    if should_relabel(expr, legend, "recommendation"):
        target["expr"] = promql_relabel(expr, "recommendation", RECOMMENDATION_MAP)
    elif should_relabel(expr, legend, "step_kind"):
        target["expr"] = promql_relabel(expr, "step_kind", STEP_MAP)
    elif should_relabel(expr, legend, "lane_id"):
        target["expr"] = promql_relabel(expr, "lane_id", LANE_MAP)
    elif should_relabel(expr, legend, "tool_name"):
        target["expr"] = promql_relabel(expr, "tool_name", TOOL_MAP)
    elif should_relabel(expr, legend, "agent"):
        target["expr"] = promql_relabel(expr, "agent", AGENT_MAP)


def patch_dashboard(path: Path) -> None:
    data = json.loads(path.read_text(encoding="utf-8"))
    templating = data.get("templating", {}).get("list", [])
    for var in templating:
        patch_variable(var, "agent", "Agent 角色", AGENT_MAP)
        patch_variable(var, "step_kind", "DAG 步骤", STEP_MAP)
        patch_variable(var, "lane_id", "并行泳道", LANE_MAP)
        patch_variable(var, "tool_name", "工具名", TOOL_MAP)
        if var.get("name") == "job_category":
            var["current"] = {"selected": True, "text": "全部", "value": "$__all"}

    in_debug = False
    for panel in data.get("panels") or []:
        if panel.get("type") == "row":
            in_debug = panel.get("collapsed", False) or "原始指标排障" in (panel.get("title") or "")
            continue
        if in_debug:
            continue
        sanitize_description(panel)
        title = (panel.get("title") or "").lower()
        defaults = panel.setdefault("fieldConfig", {}).setdefault("defaults", {})
        defaults.setdefault("noValue", "暂无样本")
        if "推荐" in panel.get("title", "") or "recommendation" in title:
            apply_value_mapping(defaults, RECOMMENDATION_MAP)
        if "agent" in title or "{{agent}}" in json.dumps(panel.get("targets") or []):
            apply_value_mapping(defaults, AGENT_MAP)
        if "dag" in title or "step_kind" in json.dumps(panel.get("targets") or []):
            apply_value_mapping(defaults, STEP_MAP)
        for target in panel.get("targets") or []:
            patch_target_expr(target)

    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"patched {path.name}")


def main() -> None:
    for path in sorted(DASH_DIR.glob("*.json")):
        patch_dashboard(path)


if __name__ == "__main__":
    main()
