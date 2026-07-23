from __future__ import annotations

"""Read-only helpers for BACKGROUND_QUERY turns. No ReportAgent imports."""

from typing import Any, Dict, List, Mapping


def collect_session_citations(snapshot: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """Build soft session citations from the Java-provided snapshot."""
    citations: List[Dict[str, Any]] = []
    summary = str(snapshot.get("summary") or "").strip()
    if summary:
        citations.append({
            "sourceType": "SESSION",
            "sourceId": "summary",
            "quote": summary[:200],
        })
    goal = str(snapshot.get("activeGoal") or "").strip()
    if goal:
        citations.append({
            "sourceType": "SESSION",
            "sourceId": "activeGoal",
            "quote": goal[:200],
        })
    return citations


def evidence_hints(content: str, snapshot: Mapping[str, Any]) -> str:
    """Deterministic evidence-oriented hint when tools are unavailable."""
    bits: List[str] = []
    if snapshot.get("hasResume"):
        bits.append("会话已挂载简历原文，可在决策报告中查看带行号的引用。")
    else:
        bits.append("当前会话尚未挂载简历，无法给出原文行号证据。")
    if snapshot.get("hasJobDescription"):
        bits.append("已挂载 JD，可对照 must-have 条款核对缺口。")
    if snapshot.get("summary"):
        bits.append("已有评估摘要可用，完整证据链请打开决策报告。")
    prefix = " ".join(bits) if bits else "证据不足时不会猜测。"
    return f"{prefix} 针对「{content[:80]}」，请在决策报告页查看带 SourceRef 的评分与风险。"
