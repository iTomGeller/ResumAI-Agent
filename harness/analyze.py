"""Analyze stress raw_results.json -> summary.json + matplotlib charts.

Reads the faithfully-collected raw results from run_stress.py and produces:
  * reports/stress_e2e/summary.json   (all aggregate metrics)
  * reports/stress_e2e/figs/*.png     (charts embedded into the LaTeX report)

All numbers are derived purely from collected data. No fabrication.
"""
from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
OUTDIR = ROOT / "reports" / "stress_e2e"
FIGS = OUTDIR / "figs"
RAW = OUTDIR / "raw_results.json"
CKPT = OUTDIR / "checkpoint.json"
MANIFEST = ROOT / "testdata" / "stress_resumes" / "manifest.json"

plt.rcParams.update({
    "figure.dpi": 130,
    "font.size": 11,
    "axes.grid": True,
    "grid.alpha": 0.3,
    "axes.spines.top": False,
    "axes.spines.right": False,
})

ACCENT = "#2563eb"
ACCENT2 = "#f97316"
GREEN = "#16a34a"
RED = "#dc2626"
PALETTE = ["#2563eb", "#f97316", "#16a34a", "#dc2626", "#9333ea", "#0891b2", "#ca8a04", "#db2777"]

NODE_ORDER = [
    "intent", "resume_parse", "jd_match", "knowledge_context",
    "tech_eval", "project_eval", "risk_eval", "evidence_fusion", "report",
]


def load_records() -> list[dict]:
    """Use whichever source has more records (raw_results.json vs checkpoint.json).

    The full run writes raw_results.json only at the end, so mid-run we prefer the
    larger checkpoint; the final run's raw_results (all 100) then wins.
    """
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    raw_recs: list[dict] = []
    if RAW.is_file():
        try:
            raw_recs = json.loads(RAW.read_text(encoding="utf-8")) or []
        except Exception:  # noqa: BLE001
            raw_recs = []
    ckpt_recs: list[dict] = []
    if CKPT.is_file():
        try:
            state = json.loads(CKPT.read_text(encoding="utf-8"))
            ckpt_recs = [state[r["id"]] for r in manifest if r["id"] in state]
        except Exception:  # noqa: BLE001
            ckpt_recs = []
    return raw_recs if len(raw_recs) >= len(ckpt_recs) else ckpt_recs


def pctl(data: list[float], q: float) -> float:
    return float(np.percentile(data, q)) if data else 0.0


def stats_block(data: list[float]) -> dict:
    if not data:
        return {"count": 0, "mean": 0, "p50": 0, "p90": 0, "p95": 0, "max": 0, "min": 0}
    arr = np.array(data, dtype=float)
    return {
        "count": int(arr.size),
        "mean": round(float(arr.mean()), 1),
        "p50": round(pctl(data, 50), 1),
        "p90": round(pctl(data, 90), 1),
        "p95": round(pctl(data, 95), 1),
        "max": round(float(arr.max()), 1),
        "min": round(float(arr.min()), 1),
    }


def savefig(fig, name: str) -> None:
    FIGS.mkdir(parents=True, exist_ok=True)
    path = FIGS / name
    fig.tight_layout()
    fig.savefig(path, bbox_inches="tight")
    plt.close(fig)
    print(f"[fig] {path}")


def main() -> None:
    records = load_records()
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    total = len(records)
    ok = [r for r in records if r.get("status") == "SUCCESS"]
    failed = [r for r in records if r.get("status") != "SUCCESS"]
    n_ok = len(ok)

    def M(r):  # metrics accessor
        return r.get("metrics", {}) or {}

    # ---------------- latency ----------------
    server_durations = [M(r).get("serverDurationMs") for r in ok if isinstance(M(r).get("serverDurationMs"), (int, float))]
    wall_durations = [r.get("clientWallMs") for r in ok if isinstance(r.get("clientWallMs"), (int, float))]
    lat = stats_block([d / 1000 for d in server_durations])  # seconds
    wall = stats_block([d / 1000 for d in wall_durations])

    # ---------------- node durations ----------------
    node_acc: dict[str, list[float]] = defaultdict(list)
    for r in ok:
        for nid, d in (M(r).get("nodeDurations") or {}).items():
            if isinstance(d, (int, float)) and d > 0:
                node_acc[nid].append(float(d))
    node_means = {nid: round(float(np.mean(v)), 1) for nid, v in node_acc.items()}
    node_counts = {nid: len(v) for nid, v in node_acc.items()}

    # ---------------- routeMode ----------------
    route_counts = Counter(M(r).get("routeMode") or "UNKNOWN" for r in ok)

    # ---------------- planned generation lower bound vs observed calls ----------------
    saved_vals = [M(r).get("llmCallsSavedVsFull") for r in ok if isinstance(M(r).get("llmCallsSavedVsFull"), (int, float))]
    est_vals = [M(r).get("estimatedLlmCalls") for r in ok if isinstance(M(r).get("estimatedLlmCalls"), (int, float))]
    full_vals = [M(r).get("fullPipelineLlmCalls") or 8 for r in ok]
    observed_vals = [M(r).get("observedLlmCalls") for r in ok if isinstance(M(r).get("observedLlmCalls"), (int, float))]
    observed_input_tokens = [M(r).get("observedInputTokens") for r in ok if isinstance(M(r).get("observedInputTokens"), (int, float))]
    observed_output_tokens = [M(r).get("observedOutputTokens") for r in ok if isinstance(M(r).get("observedOutputTokens"), (int, float))]
    total_saved = int(sum(saved_vals))
    total_estimated = int(sum(est_vals))
    total_full = int(sum(full_vals))
    saved_dist = Counter(int(v) for v in saved_vals)

    # ---------------- MCP / github ----------------
    mcp_tasks = [r for r in ok if (M(r).get("mcpFetchCount") or 0) > 0]
    gh_tasks = [r for r in ok if (M(r).get("githubEnrichmentCount") or 0) > 0]
    mcp_durations = [d for r in ok for d in (M(r).get("mcpFetchDurations") or [])]
    gh_durations = [d for r in ok for d in (M(r).get("githubEnrichmentDurations") or [])]
    mcp_statuses = Counter(s for r in ok for s in (M(r).get("mcpFetchStatuses") or []))
    has_github_manifest = sum(1 for m in manifest if m.get("hasGithub"))

    # ---------------- score / recommendation ----------------
    scores = [M(r).get("overallScore") for r in ok if isinstance(M(r).get("overallScore"), (int, float))]
    rec_counts = Counter(M(r).get("recommendation") or "UNKNOWN" for r in ok)

    # ---------------- report length / interview questions ----------------
    report_lengths = [M(r).get("reportLength") or 0 for r in ok]
    iq_counts = [M(r).get("interviewQuestionsCount") or 0 for r in ok]
    strengths_counts = [M(r).get("strengthsCount") or 0 for r in ok]
    risks_counts = [M(r).get("risksCount") or 0 for r in ok]
    iq_ge8 = sum(1 for c in iq_counts if c >= 8)

    # ---------------- failure reasons ----------------
    fail_reasons = Counter(r.get("failReason") or "unknown" for r in failed)

    summary = {
        "generatedAt": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
        "base": (
            next(iter({str(r.get("baseUrl")) for r in records if r.get("baseUrl")}))
            if len({str(r.get("baseUrl")) for r in records if r.get("baseUrl")}) == 1
            else "MIXED_OR_UNKNOWN"
        ),
        "totals": {
            "manifest": len(manifest),
            "collected": total,
            "success": n_ok,
            "failed": len(failed),
            "successRate": round(n_ok / total * 100, 1) if total else 0,
            "failureRate": round(len(failed) / total * 100, 1) if total else 0,
        },
        "latencySecServer": lat,
        "latencySecWall": wall,
        "nodeDurationMeanMs": node_means,
        "nodeDurationSampleCount": node_counts,
        "routeModeCounts": dict(route_counts),
        "llm": {
            "estimateBasis": "plan lower bound; not observed provider usage",
            "totalSaved": total_saved,
            "totalEstimated": total_estimated,
            "totalFullPipeline": total_full,
            "savingPct": round(total_saved / total_full * 100, 1) if total_full else 0,
            "avgEstimatedPerTask": round(total_estimated / n_ok, 2) if n_ok else 0,
            "savedDistribution": {str(k): v for k, v in sorted(saved_dist.items())},
            "totalObservedCalls": int(sum(observed_vals)),
            "observedCallsPerTask": stats_block(observed_vals),
            "observedInputTokens": int(sum(observed_input_tokens)),
            "observedOutputTokens": int(sum(observed_output_tokens)),
        },
        "mcp": {
            "triggerTasks": len(mcp_tasks),
            "triggerRatePct": round(len(mcp_tasks) / n_ok * 100, 1) if n_ok else 0,
            "totalCalls": len(mcp_durations),
            "avgLatencyMs": round(float(np.mean(mcp_durations)), 1) if mcp_durations else 0,
            "p90LatencyMs": round(pctl(mcp_durations, 90), 1) if mcp_durations else 0,
            "maxLatencyMs": round(float(np.max(mcp_durations)), 1) if mcp_durations else 0,
            "statuses": dict(mcp_statuses),
        },
        "githubEnrichment": {
            "triggerTasks": len(gh_tasks),
            "triggerRatePct": round(len(gh_tasks) / n_ok * 100, 1) if n_ok else 0,
            "totalCalls": len(gh_durations),
            "avgLatencyMs": round(float(np.mean(gh_durations)), 1) if gh_durations else 0,
        },
        "manifestHasGithub": has_github_manifest,
        "manifestHasGithubPct": round(has_github_manifest / len(manifest) * 100, 1),
        "score": stats_block(scores),
        "recommendationCounts": dict(rec_counts),
        "reportLength": stats_block(report_lengths),
        "interviewQuestions": stats_block(iq_counts),
        "interviewQuestionsGE8": iq_ge8,
        "interviewQuestionsGE8Pct": round(iq_ge8 / n_ok * 100, 1) if n_ok else 0,
        "strengths": stats_block(strengths_counts),
        "risks": stats_block(risks_counts),
        "failureReasons": dict(fail_reasons),
    }

    OUTDIR.mkdir(parents=True, exist_ok=True)
    (OUTDIR / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[summary] success={n_ok}/{total} p50={lat['p50']}s p90={lat['p90']}s saved={total_saved} mcp={summary['mcp']['triggerRatePct']}%")

    # ============================ CHARTS ============================
    # 1. latency histogram (server) + percentile lines
    if server_durations:
        sd = [d / 1000 for d in server_durations]
        fig, ax = plt.subplots(figsize=(7, 4))
        ax.hist(sd, bins=20, color=ACCENT, alpha=0.85, edgecolor="white")
        for q, c, lbl in [(50, GREEN, "p50"), (90, ACCENT2, "p90"), (95, RED, "p95")]:
            v = pctl(sd, q)
            ax.axvline(v, color=c, linestyle="--", linewidth=1.6, label=f"{lbl}={v:.1f}s")
        ax.set_xlabel("End-to-end latency (seconds, server durationMs)")
        ax.set_ylabel("Number of resumes")
        ax.set_title(f"End-to-End Latency Distribution (n={len(sd)})")
        ax.legend()
        savefig(fig, "latency_hist.png")

    # 2. node duration bar (mean)
    present = [n for n in NODE_ORDER if n in node_means] + [n for n in node_means if n not in NODE_ORDER]
    if present:
        vals = [node_means[n] / 1000 for n in present]
        fig, ax = plt.subplots(figsize=(8, 4.2))
        colors = [RED if node_means[n] == max(node_means.values()) else ACCENT for n in present]
        bars = ax.bar(range(len(present)), vals, color=colors, alpha=0.9)
        ax.set_ylabel("Mean duration (seconds)")
        ax.set_title("Per-Agent-Node Mean Latency (bottleneck highlighted)")
        ax.set_xticks(range(len(present)))
        ax.set_xticklabels(present, rotation=35, ha="right")
        for b, v in zip(bars, vals):
            ax.text(b.get_x() + b.get_width() / 2, v, f"{v:.1f}", ha="center", va="bottom", fontsize=9)
        savefig(fig, "node_duration_bar.png")

    # 3. routeMode pie + bar
    if route_counts:
        labels = list(route_counts.keys())
        sizes = list(route_counts.values())
        fig, (a1, a2) = plt.subplots(1, 2, figsize=(11, 4.4))
        a1.pie(sizes, labels=labels, autopct=lambda p: f"{p:.0f}%\n({int(round(p*sum(sizes)/100))})",
               colors=PALETTE[:len(labels)], startangle=90, textprops={"fontsize": 9})
        a1.set_title("routeMode Distribution (dynamic routing)")
        order = sorted(range(len(sizes)), key=lambda i: -sizes[i])
        a2.bar(range(len(order)), [sizes[i] for i in order], color=[PALETTE[i % len(PALETTE)] for i in order])
        a2.set_ylabel("Number of resumes")
        a2.set_title("routeMode Counts")
        a2.set_xticks(range(len(order)))
        a2.set_xticklabels([labels[i] for i in order], rotation=30, ha="right", fontsize=9)
        savefig(fig, "routemode_dist.png")

    # 4. score histogram
    if scores:
        fig, ax = plt.subplots(figsize=(7, 4))
        ax.hist(scores, bins=range(0, 105, 5), color=ACCENT2, alpha=0.85, edgecolor="white")
        ax.axvline(float(np.mean(scores)), color=RED, linestyle="--", label=f"mean={np.mean(scores):.1f}")
        ax.set_xlabel("overallScore")
        ax.set_ylabel("Number of resumes")
        ax.set_title(f"Overall Score Distribution (n={len(scores)})")
        ax.legend()
        savefig(fig, "score_hist.png")

    # 5. recommendation distribution
    if rec_counts:
        rec_order = ["STRONG_RECOMMEND", "RECOMMEND", "NEED_MANUAL_REVIEW", "NOT_RECOMMEND"]
        labels = [x for x in rec_order if x in rec_counts] + [x for x in rec_counts if x not in rec_order]
        vals = [rec_counts[x] for x in labels]
        cmap = {"STRONG_RECOMMEND": GREEN, "RECOMMEND": "#65a30d", "NEED_MANUAL_REVIEW": ACCENT2, "NOT_RECOMMEND": RED}
        fig, ax = plt.subplots(figsize=(7.5, 4))
        bars = ax.bar(range(len(labels)), vals, color=[cmap.get(x, ACCENT) for x in labels])
        for b, v in zip(bars, vals):
            ax.text(b.get_x() + b.get_width() / 2, v, str(v), ha="center", va="bottom")
        ax.set_ylabel("Number of resumes")
        ax.set_title("Recommendation Distribution")
        ax.set_xticks(range(len(labels)))
        ax.set_xticklabels(labels, rotation=20, ha="right", fontsize=9)
        savefig(fig, "recommendation_bar.png")

    # 6. planned generation lower-bound reduction distribution
    if saved_vals:
        keys = sorted(saved_dist.keys())
        vals = [saved_dist[k] for k in keys]
        fig, ax = plt.subplots(figsize=(7, 4))
        bars = ax.bar([str(k) for k in keys], vals, color=GREEN, alpha=0.9)
        for b, v in zip(bars, vals):
            ax.text(b.get_x() + b.get_width() / 2, v, str(v), ha="center", va="bottom")
        ax.set_xlabel("Planned generation lower-bound reduction vs full route (per resume)")
        ax.set_ylabel("Number of resumes")
        ax.set_title(
            "Route-plan Lower-Bound Reduction "
            f"(total reduction={total_saved} of {total_full} planned generations)"
        )
        savefig(fig, "llm_saved_bar.png")

    # 7. public MCP latency (identified by trace provenance)
    if mcp_durations:
        fig, ax = plt.subplots(figsize=(7, 4))
        ax.hist([d / 1000 for d in mcp_durations], bins=18, color="#9333ea", alpha=0.85, edgecolor="white")
        ax.axvline(float(np.mean(mcp_durations)) / 1000, color=RED, linestyle="--",
                   label=f"mean={np.mean(mcp_durations)/1000:.2f}s")
        ax.set_xlabel("Public MCP tool latency (seconds)")
        ax.set_ylabel("Number of calls")
        ax.set_title(f"Public MCP Tool Latency (n={len(mcp_durations)})")
        ax.legend()
        savefig(fig, "mcp_latency.png")

    # 8. interview questions distribution
    if iq_counts:
        fig, ax = plt.subplots(figsize=(7, 4))
        c = Counter(iq_counts)
        keys = sorted(c.keys())
        bars = ax.bar([str(k) for k in keys], [c[k] for k in keys], color=ACCENT)
        ax.set_xlabel("Interview follow-up questions per resume")
        ax.set_ylabel("Number of resumes")
        ax.set_title(f"Interview Question Count ( >=8: {iq_ge8}/{n_ok} = {summary['interviewQuestionsGE8Pct']}% )")
        for b, k in zip(bars, keys):
            ax.text(b.get_x() + b.get_width() / 2, c[k], str(c[k]), ha="center", va="bottom")
        savefig(fig, "interview_hist.png")

    # 9. report length distribution
    if report_lengths and max(report_lengths) > 0:
        fig, ax = plt.subplots(figsize=(7, 4))
        ax.hist(report_lengths, bins=20, color="#0891b2", alpha=0.85, edgecolor="white")
        ax.set_xlabel("Report body length (chars, summary field)")
        ax.set_ylabel("Number of resumes")
        ax.set_title(f"Report Body Length Distribution (n={len(report_lengths)})")
        savefig(fig, "report_length_hist.png")

    print("[done] analyze complete")


if __name__ == "__main__":
    main()
