#!/usr/bin/env python3
"""Policy self-evolution loop — evolutionary search over policy text/params
(GEPA-style reflective mutation), no GPU, fully automatic.

One generation:
  1. SEED      current champion + ACTIVE bundles.
  2. EVALUATE  each policy runs the gold cases through the REAL stack
               (isolatedSandbox: forced policies execute deterministic tools
               in Docker workers) — reuses run_agent_e2e_benchmark.run_one.
  3. REFLECT   the worst policy's failure evidence (low-reward cases, errors,
               violations) is fed to DeepSeek which proposes 1-2 mutated
               candidate configs (budgets / agentOrder / verification
               thresholds only — a bounded, auditable mutation space).
  4. GATE      candidates run the held-out regression cases; a candidate is
               promoted to champion only if it beats the current champion on
               held-out reward with zero safety regressions. Losers retire.
  5. AUDIT     every step lands in policy_evolution_log (lineage replayable).

Reward-hacking guards: mutation never sees gold labels (only aggregate
failure descriptions), the gate set is disjoint from the evaluation set, and
safety cases must stay at zero violations.

Usage (ECS cron, nightly):
  cd /opt/resumai-src && python3 harness/evolve_policies.py \
      --base http://127.0.0.1 --budget-cny 5 --out reports/evolution
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "harness"))
sys.path.insert(0, str(ROOT / "workflow"))

from run_agent_e2e_benchmark import E2EResult, load_cases, run_one  # noqa: E402

INTERNAL_TOKEN = os.getenv("WORKFLOW_INTERNAL_TOKEN", "")
DEEPSEEK_KEY = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_URL = os.getenv("DEEPSEEK_API_URL", "https://api.deepseek.com/v1").rstrip("/")
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")

# Mutation space: ONLY these keys may be changed, inside these bounds. This
# keeps evolution auditable and rules out prompt-injection style mutations.
MUTABLE_BOUNDS = {
    "maxLlmCalls": (4, 16),
    "maxAgentCount": (3, 7),
    "maxIterationsPerAgent": (1, 3),
    "maxCostCny": (0.2, 2.0),
    "maxTotalTokens": (30000, 200000),
    "rewriteRounds": (0, 2),
}
MUTABLE_NESTED = {
    "toolBudget.maxToolCallsPerRun": (6, 30),
    "toolBudget.maxToolCallsPerAgent": (2, 8),
    "memoryRetrieval.topK": (2, 8),
    "memoryRetrieval.minConfidence": (0.2, 0.6),
    "evidenceVerification.minSupportRatio": (0.3, 0.8),
}
MUTABLE_FLAGS = {"parallelSpecialists", "evidenceVerification.enabled",
                 "evidenceVerification.strict"}


def http(method: str, url: str, body: Optional[dict] = None, *,
         headers: Optional[dict] = None, timeout: float = 60.0) -> Any:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    all_headers = {"Content-Type": "application/json"}
    all_headers.update(headers or {})
    request = urllib.request.Request(url, data=data, method=method, headers=all_headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def internal(method: str, base: str, path: str, body: Optional[dict] = None) -> Any:
    return http(method, f"{base}{path}", body,
                headers={"X-Internal-Token": INTERNAL_TOKEN})


def deepseek_json(system: str, user: str, max_tokens: int = 900) -> Dict[str, Any]:
    body = {
        "model": DEEPSEEK_MODEL,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "temperature": 0.4,
        "max_tokens": max_tokens,
        "response_format": {"type": "json_object"},
    }
    response = http("POST", f"{DEEPSEEK_URL}/chat/completions", body, headers={
        "Authorization": f"Bearer {DEEPSEEK_KEY}"}, timeout=120.0)
    content = response["choices"][0]["message"]["content"] or "{}"
    return json.loads(content)


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------

def evaluate_policy(base: str, policy_id: str, cases: List[Dict[str, Any]],
                    timeout_s: int) -> Dict[str, Any]:
    results: List[E2EResult] = []
    for case in cases:
        results.append(run_one(base, case, policy_id, repeat=1, timeout_s=timeout_s))
    rewards = [r.total_reward for r in results]
    violations = sum(1 for r in results if r.violation_penalty > 0)
    cost = sum(r.actual_cost_cny for r in results)
    return {
        "policyId": policy_id,
        "avgReward": round(sum(rewards) / max(1, len(rewards)), 4),
        "violations": violations,
        "totalCostCny": round(cost, 4),
        "failures": [
            {"caseId": r.case_id, "status": r.status,
             "reward": r.total_reward, "error": r.error,
             "violationPenalty": r.violation_penalty,
             "llmCalls": r.llm_calls,
             "latencySeconds": r.latency_seconds}
            for r in sorted(results, key=lambda x: x.total_reward)[:3]
        ],
        "results": results,
    }


def clamp_config(config: Dict[str, Any], base_config: Dict[str, Any]) -> Dict[str, Any]:
    """Enforce the bounded mutation space: unknown keys revert to base, known
    keys clamp into range. The mutated config can never leave the sandbox of
    allowed knobs."""
    def read_nested(cfg: Dict[str, Any], dotted: str) -> Any:
        node: Any = cfg
        for part in dotted.split("."):
            if not isinstance(node, dict):
                return None
            node = node.get(part)
        return node

    def write_nested(cfg: Dict[str, Any], dotted: str, value: Any) -> None:
        parts = dotted.split(".")
        node = cfg
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value

    merged = json.loads(json.dumps(base_config))  # deep copy
    for key, (low, high) in MUTABLE_BOUNDS.items():
        value = config.get(key)
        if isinstance(value, (int, float)):
            clamped = max(low, min(high, value))
            merged[key] = int(clamped) if isinstance(low, int) else round(float(clamped), 4)
    for dotted, (low, high) in MUTABLE_NESTED.items():
        value = read_nested(config, dotted)
        if isinstance(value, (int, float)):
            clamped = max(low, min(high, value))
            write_nested(merged, dotted,
                         int(clamped) if isinstance(low, int) else round(float(clamped), 4))
    for dotted in MUTABLE_FLAGS:
        value = read_nested(config, dotted) if "." in dotted else config.get(dotted)
        if isinstance(value, bool):
            if "." in dotted:
                write_nested(merged, dotted, value)
            else:
                merged[dotted] = value
    return merged


def reflect_and_mutate(worst: Dict[str, Any], worst_config: Dict[str, Any],
                       generation: int) -> List[Dict[str, Any]]:
    """GEPA-style reflection: the LLM reads WHY the worst policy failed
    (statuses, penalties, latency — never gold labels) and proposes bounded
    config mutations with a reason."""
    system = ("你是 Agent 策略进化器。根据失败证据提出策略配置的定向变异（json）。"
              "只允许修改给定的可变参数，每个变异必须给出针对失败证据的理由。")
    user = json.dumps({
        "失败证据": {
            "policyId": worst["policyId"],
            "avgReward": worst["avgReward"],
            "violations": worst["violations"],
            "worstCases": worst["failures"],
        },
        "当前配置": worst_config,
        "可变参数与边界": {**MUTABLE_BOUNDS, **MUTABLE_NESTED,
                          "flags": sorted(MUTABLE_FLAGS)},
        "输出格式": {"mutations": [{"config": "完整配置对象（在当前配置上修改）",
                                    "reason": "针对失败证据的一句理由"}]},
        "要求": "提出 1-2 个本质不同的变异方向（如：预算换质量 / 收紧证据验证）",
    }, ensure_ascii=False)
    try:
        parsed = deepseek_json(system, user)
        mutations = parsed.get("mutations") or []
        out = []
        for mutation in mutations[:2]:
            config = mutation.get("config")
            if isinstance(config, dict):
                out.append({
                    "config": clamp_config(config, worst_config),
                    "reason": str(mutation.get("reason", ""))[:400],
                })
        return out
    except Exception as exc:  # noqa: BLE001 - reflection is best-effort
        print(f"[reflect] mutation proposal failed: {exc}")
        return []


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1")
    parser.add_argument("--cases", default=str(ROOT / "testdata" / "benchmark"))
    parser.add_argument("--eval-dataset", default="gold")
    parser.add_argument("--gate-dataset", default="regression")
    parser.add_argument("--timeout", type=int, default=420)
    parser.add_argument("--budget-cny", type=float, default=5.0,
                        help="hard cost ceiling for the whole generation")
    parser.add_argument("--out", default=str(ROOT / "reports" / "evolution"))
    args = parser.parse_args()
    base = args.base.rstrip("/")
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    if not INTERNAL_TOKEN:
        print("WORKFLOW_INTERNAL_TOKEN required")
        return 2

    bundles = internal("GET", base, "/api/internal/policies")
    active = [b for b in bundles if b.get("status") == "ACTIVE"]
    if not active:
        print("no ACTIVE policies")
        return 2
    champion = next((b for b in active if b.get("isChampion") == 1), active[0])
    generation = max(int(b.get("generation") or 0) for b in bundles) + 1
    print(f"[gen {generation}] champion={champion['policyId']} "
          f"actives={[b['policyId'] for b in active]}")

    eval_cases = load_cases(Path(args.cases), args.eval_dataset)
    gate_cases = load_cases(Path(args.cases), args.gate_dataset)

    # ---- 2. EVALUATE actives on the eval set --------------------------------
    spent = 0.0
    evaluations: Dict[str, Dict[str, Any]] = {}
    for bundle in active:
        evaluation = evaluate_policy(base, bundle["policyId"], eval_cases, args.timeout)
        evaluations[bundle["policyId"]] = evaluation
        spent += evaluation["totalCostCny"]
        print(f"[eval] {bundle['policyId']}: reward={evaluation['avgReward']} "
              f"violations={evaluation['violations']} cost={evaluation['totalCostCny']}")
        if spent >= args.budget_cny:
            print(f"[budget] generation cost ceiling reached ({spent:.2f} CNY)")
            break

    if not evaluations:
        return 2
    worst_id = min(evaluations, key=lambda k: evaluations[k]["avgReward"])
    champion_eval = evaluations.get(champion["policyId"])

    # ---- 3. REFLECT + mutate the worst policy -------------------------------
    worst_bundle = next(b for b in active if b["policyId"] == worst_id)
    mutations = [] if spent >= args.budget_cny else reflect_and_mutate(
        evaluations[worst_id], worst_bundle.get("config") or {}, generation)

    candidates: List[Dict[str, Any]] = []
    for index, mutation in enumerate(mutations):
        candidate_id = f"evo-g{generation}-{uuid.uuid4().hex[:6]}"
        internal("POST", base, "/api/internal/policies/candidates", {
            "policyId": candidate_id,
            "name": f"进化候选 g{generation}#{index + 1}（源 {worst_id}）",
            "description": mutation["reason"],
            "config": mutation["config"],
            "parentPolicyId": worst_id,
            "generation": generation,
            "mutationReason": mutation["reason"],
        })
        candidates.append({"policyId": candidate_id, **mutation})
        print(f"[mutate] candidate {candidate_id}: {mutation['reason'][:80]}")

    # ---- 4. GATE: held-out set, champion as the bar -------------------------
    champion_gate = evaluate_policy(base, champion["policyId"], gate_cases, args.timeout)
    spent += champion_gate["totalCostCny"]
    print(f"[gate] champion {champion['policyId']}: reward={champion_gate['avgReward']} "
          f"violations={champion_gate['violations']}")

    promoted = None
    for candidate in candidates:
        if spent >= args.budget_cny:
            internal("POST", base, f"/api/internal/policies/{candidate['policyId']}/verdict", {
                "generation": generation, "promote": False,
                "reason": "generation budget exhausted before gate"})
            continue
        gate = evaluate_policy(base, candidate["policyId"], gate_cases, args.timeout)
        spent += gate["totalCostCny"]
        beats = gate["avgReward"] > champion_gate["avgReward"]
        safe = gate["violations"] <= champion_gate["violations"]
        promote = bool(beats and safe and promoted is None)
        internal("POST", base, f"/api/internal/policies/{candidate['policyId']}/verdict", {
            "generation": generation,
            "benchmarkScore": gate["avgReward"],
            "championScore": champion_gate["avgReward"],
            "promote": promote,
            "reason": (f"held-out reward {gate['avgReward']} vs champion "
                       f"{champion_gate['avgReward']}, violations {gate['violations']} "
                       f"vs {champion_gate['violations']}"),
        })
        print(f"[gate] {candidate['policyId']}: reward={gate['avgReward']} "
              f"violations={gate['violations']} -> "
              f"{'PROMOTED' if promote else 'REJECTED'}")
        if promote:
            promoted = candidate["policyId"]

    # ---- 5. AUDIT report -----------------------------------------------------
    report = {
        "generation": generation,
        "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "championBefore": champion["policyId"],
        "championAfter": promoted or champion["policyId"],
        "evaluations": {k: {kk: vv for kk, vv in v.items() if kk != "results"}
                        for k, v in evaluations.items()},
        "championGate": {k: v for k, v in champion_gate.items() if k != "results"},
        "candidates": [{"policyId": c["policyId"], "reason": c["reason"]}
                       for c in candidates],
        "totalCostCny": round(spent, 4),
        "budgetCny": args.budget_cny,
    }
    path = out_dir / f"generation_{generation}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[done] gen {generation} champion: {report['championBefore']} -> "
          f"{report['championAfter']} cost={spent:.2f} CNY report={path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
