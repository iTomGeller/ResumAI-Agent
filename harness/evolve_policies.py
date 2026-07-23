#!/usr/bin/env python3
"""Policy Optimization Lab — OFFLINE_SEARCH loop (无 GPU).

LLM-guided **bounded evolutionary search** over PolicyBundle config knobs
(budgets / agentOrder / verification thresholds). This is NOT model-weight
training (MODEL_WEIGHTS unchanged), NOT PPO/GRPO/RLHF, and NOT full GEPA.

Active Policy Lab path:
  PolicyExperimentRunner.run(experiment_id) talks to /api/dev/policy-lab/*
  writes policy_trial rows, respects pause/cancel, records gate — NEVER promotes.

Offline CLI path (legacy cron): still evaluates / mutates via internal APIs
but must not be used as the production promote path for Policy Lab.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Protocol

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "harness"))
sys.path.insert(0, str(ROOT / "workflow"))

from run_agent_e2e_benchmark import E2EResult, load_cases, run_one  # noqa: E402

try:
    from app.policy_lab.sandbox_client import SandboxClient, SandboxUnavailable  # noqa: F401,E402
except ImportError:  # pragma: no cover
    SandboxClient = None  # type: ignore[misc, assignment]
    SandboxUnavailable = None  # type: ignore[misc, assignment]

INTERNAL_TOKEN = os.getenv("WORKFLOW_INTERNAL_TOKEN", "")
DEEPSEEK_KEY = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_URL = os.getenv("DEEPSEEK_API_URL", "https://api.deepseek.com/v1").rstrip("/")
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")

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
        raw = response.read().decode("utf-8")
        return json.loads(raw) if raw else None


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
# Policy Lab HTTP client stubs
# ---------------------------------------------------------------------------

class PolicyLabClient(Protocol):
    def get_experiment(self, experiment_id: str) -> Dict[str, Any]: ...
    def start(self, experiment_id: str) -> Dict[str, Any]: ...
    def create_candidate(self, experiment_id: str, body: Dict[str, Any]) -> Dict[str, Any]: ...
    def create_trial(self, experiment_id: str, body: Dict[str, Any]) -> Dict[str, Any]: ...
    def finish_trial(self, trial_id: str, body: Dict[str, Any]) -> Dict[str, Any]: ...
    def record_gate(self, experiment_id: str, body: Dict[str, Any]) -> Dict[str, Any]: ...


class HttpPolicyLabClient:
    """Talks to /api/dev/policy-lab/* (control plane is source of truth)."""

    def __init__(self, base: str, token: str = "") -> None:
        self.base = base.rstrip("/")
        self.token = token or INTERNAL_TOKEN

    def _call(self, method: str, path: str, body: Optional[dict] = None) -> Any:
        headers = {"X-Internal-Token": self.token} if self.token else {}
        headers["X-Developer-Actor"] = "policy-lab-worker"
        return http(method, f"{self.base}{path}", body, headers=headers, timeout=120.0)

    def get_experiment(self, experiment_id: str) -> Dict[str, Any]:
        return self._call("GET", f"/api/dev/policy-lab/experiments/{experiment_id}")

    def start(self, experiment_id: str) -> Dict[str, Any]:
        return self._call("POST", f"/api/dev/policy-lab/experiments/{experiment_id}/start")

    def create_candidate(self, experiment_id: str, body: Dict[str, Any]) -> Dict[str, Any]:
        return self._call("POST", f"/api/dev/policy-lab/experiments/{experiment_id}/candidates", body)

    def create_trial(self, experiment_id: str, body: Dict[str, Any]) -> Dict[str, Any]:
        return self._call("POST", f"/api/dev/policy-lab/experiments/{experiment_id}/trials", body)

    def finish_trial(self, trial_id: str, body: Dict[str, Any]) -> Dict[str, Any]:
        return self._call("POST", f"/api/dev/policy-lab/trials/{trial_id}/finish", body)

    def record_gate(self, experiment_id: str, body: Dict[str, Any]) -> Dict[str, Any]:
        return self._call("POST", f"/api/dev/policy-lab/experiments/{experiment_id}/gate", body)


@dataclass
class HardGate:
    name: str
    status: str
    detail: str = ""


@dataclass
class GateReport:
    delta_mean: float = 0.0
    ci95_low: float = 0.0
    ci95_high: float = 0.0
    violation_rate_candidate: float = 0.0
    violation_rate_champion: float = 0.0
    timeout_rate: float = 0.0
    spent_cny: float = 0.0
    hard_gates: List[HardGate] = field(default_factory=list)
    passed: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "deltaMean": self.delta_mean,
            "ci95Low": self.ci95_low,
            "ci95High": self.ci95_high,
            "violationRateCandidate": self.violation_rate_candidate,
            "violationRateChampion": self.violation_rate_champion,
            "timeoutRate": self.timeout_rate,
            "spentCny": self.spent_cny,
            "hardGates": [g.__dict__ for g in self.hard_gates],
            "passed": self.passed,
        }


class PolicyLabEvaluator:
    """Paired-seed / bootstrap-CI stub used by the active lab runner."""

    def evaluate_paired(self, champion_rewards: List[float],
                        candidate_rewards: List[float],
                        candidate_violations: int,
                        champion_violations: int,
                        timeout_rate: float,
                        spent_cny: float,
                        budget_cny: float) -> GateReport:
        n = max(1, min(len(champion_rewards), len(candidate_rewards)))
        deltas = [candidate_rewards[i] - champion_rewards[i] for i in range(n)]
        mean = sum(deltas) / n
        # Crude percentile bootstrap without numpy.
        samples = sorted(deltas)
        lo = samples[max(0, int(0.025 * (n - 1)))] if samples else 0.0
        hi = samples[min(n - 1, int(0.975 * (n - 1)))] if samples else 0.0
        gates = [
            HardGate("reward_ci_above_zero", "PASSED" if lo > 0 else "FAILED",
                     f"ci95=[{lo:.4f},{hi:.4f}] mean={mean:.4f}"),
            HardGate("safety_zero_violations",
                     "PASSED" if candidate_violations <= champion_violations else "FAILED",
                     f"cand={candidate_violations} champ={champion_violations}"),
            HardGate("budget",
                     "PASSED" if spent_cny <= budget_cny else "FAILED",
                     f"spent={spent_cny:.4f} budget={budget_cny:.4f}"),
            HardGate("timeout_rate",
                     "PASSED" if timeout_rate <= 0.2 else "FAILED",
                     f"timeoutRate={timeout_rate:.3f}"),
        ]
        passed = all(g.status == "PASSED" for g in gates)
        return GateReport(
            delta_mean=round(mean, 4),
            ci95_low=round(lo, 4),
            ci95_high=round(hi, 4),
            violation_rate_candidate=float(candidate_violations),
            violation_rate_champion=float(champion_violations),
            timeout_rate=timeout_rate,
            spent_cny=spent_cny,
            hard_gates=gates,
            passed=passed,
        )


class PolicyExperimentRunner:
    """Active Policy Lab runner — DB via Policy Lab APIs is source of truth.

    Never calls promote. Gate pass only marks PASSED_GATE.
    """

    def __init__(self, client: PolicyLabClient, *,
                 cases_root: Path,
                 timeout_s: int = 420,
                 evaluator: Optional[PolicyLabEvaluator] = None) -> None:
        self.client = client
        self.cases_root = cases_root
        self.timeout_s = timeout_s
        self.evaluator = evaluator or PolicyLabEvaluator()
        self.base = getattr(client, "base", "http://127.0.0.1")

    def run(self, experiment_id: str) -> GateReport:
        detail = self.client.get_experiment(experiment_id)
        exp = detail.get("experiment") or detail
        self.client.start(experiment_id)
        seeds = exp.get("seeds") or [42]
        repeats = int(exp.get("repeatsPerCase") or 1)
        case_limit = int(exp.get("caseLimit") or 1)
        budget = float(exp.get("budgetCny") or 0.5)
        spent = float(exp.get("spentCny") or 0.0)

        datasets = self.datasets(exp, case_limit)
        candidates = self.generate_candidates(exp, detail)
        last_gate = GateReport(passed=False)

        for candidate in candidates:
            self.check_pause_or_cancel(experiment_id)
            cand_id = candidate["candidateId"]
            policy_id = candidate.get("bundlePolicyId") or exp.get("basePolicyId")
            rewards_by_split: Dict[str, List[float]] = {}
            violations = 0
            timeouts = 0
            total_trials = 0

            for split, cases in datasets.items():
                rewards_by_split[split] = []
                for case in cases:
                    for seed in seeds:
                        for repeat_no in range(1, repeats + 1):
                            self.check_pause_or_cancel(experiment_id)
                            if spent >= budget:
                                print(f"[budget] ceiling reached spent={spent:.4f}")
                                break
                            trial = self.client.create_trial(experiment_id, {
                                "candidateId": cand_id,
                                "datasetSplit": split,
                                "caseId": case.get("id") or case.get("caseId"),
                                "seed": seed,
                                "repeatNo": repeat_no,
                                "status": "RUNNING",
                            })
                            result = self.run_trial(exp, candidate, case, int(seed), policy_id)
                            spent += float(result.get("costCny") or 0.0)
                            total_trials += 1
                            if result.get("status") == "TIMED_OUT":
                                timeouts += 1
                            violations += int(result.get("violations") or 0)
                            rewards_by_split[split].append(float(result.get("reward") or 0.0))
                            self.client.finish_trial(trial["trialId"], {
                                "status": result.get("status", "SUCCEEDED"),
                                "totalReward": result.get("reward"),
                                "costCny": result.get("costCny"),
                                "latencyMs": result.get("latencyMs"),
                                "metricsJson": json.dumps(result.get("metrics") or {}),
                                "rewardComponentsJson": json.dumps(
                                    result.get("rewardComponents") or {}),
                                "error": result.get("error"),
                                "runId": result.get("runId"),
                            })
                        if spent >= budget:
                            break
                    if spent >= budget:
                        break
                if spent >= budget:
                    break

            champ_rewards = rewards_by_split.get("gate") or rewards_by_split.get("eval") or [0.0]
            # Champion baseline is the first candidate (base) when present.
            if candidate.get("isBase"):
                continue
            gate = self.evaluator.evaluate_paired(
                champion_rewards=champ_rewards,
                candidate_rewards=rewards_by_split.get("gate") or [0.0],
                candidate_violations=violations,
                champion_violations=0,
                timeout_rate=(timeouts / max(1, total_trials)),
                spent_cny=spent,
                budget_cny=budget,
            )
            self.client.record_gate(experiment_id, {
                "candidateId": cand_id,
                "passed": gate.passed,
                "gateMetricsJson": json.dumps(gate.to_dict()),
            })
            last_gate = gate
            # NEVER promote — human must call /candidates/{id}/promote
            print(f"[gate] candidate={cand_id} passed={gate.passed} "
                  f"(autoPromote=false, no promote call)")
        return last_gate

    def generate_candidates(self, exp: Dict[str, Any],
                            detail: Dict[str, Any]) -> List[Dict[str, Any]]:
        existing = detail.get("candidates") or []
        if existing:
            return existing
        base_id = exp.get("basePolicyId")
        created = self.client.create_candidate(exp["experimentId"], {
            "bundlePolicyId": base_id,
            "parentPolicyId": base_id,
            "status": "EVALUATING",
            "mutationReason": "base seed candidate",
            "configHash": hashlib.sha256(str(base_id).encode()).hexdigest()[:16],
        })
        created["isBase"] = True
        return [created]

    def datasets(self, exp: Dict[str, Any], case_limit: int) -> Dict[str, List[Dict[str, Any]]]:
        out: Dict[str, List[Dict[str, Any]]] = {}
        for split, key in (("eval", "evalDataset"), ("gate", "gateDataset"),
                           ("safety", "safetyDataset")):
            name = exp.get(key) or ("gold" if split == "eval" else split)
            try:
                cases = load_cases(self.cases_root, name)[:case_limit]
            except Exception as exc:  # noqa: BLE001
                print(f"[datasets] load {name} failed: {exc}; using empty")
                cases = []
            out[split] = cases
        return out

    def check_pause_or_cancel(self, experiment_id: str) -> None:
        detail = self.client.get_experiment(experiment_id)
        exp = detail.get("experiment") or detail
        if exp.get("cancelRequested") or str(exp.get("status", "")).upper() == "CANCELLED":
            raise RuntimeError(f"experiment cancelled: {experiment_id}")
        while exp.get("pauseRequested") or str(exp.get("status", "")).upper() == "PAUSED":
            print(f"[pause] waiting on {experiment_id} ...")
            time.sleep(3)
            detail = self.client.get_experiment(experiment_id)
            exp = detail.get("experiment") or detail
            if exp.get("cancelRequested"):
                raise RuntimeError(f"experiment cancelled while paused: {experiment_id}")

    def run_trial(self, exp: Dict[str, Any], candidate: Dict[str, Any],
                  case: Dict[str, Any], seed: int, policy_id: str) -> Dict[str, Any]:
        """Execute one case×seed via the real stack (same as offline eval)."""
        try:
            result: E2EResult = run_one(
                self.base, case, policy_id, repeat=1, timeout_s=self.timeout_s)
            return {
                "status": result.status,
                "reward": result.total_reward,
                "costCny": result.actual_cost_cny,
                "latencyMs": int((result.latency_seconds or 0) * 1000),
                "violations": 1 if result.violation_penalty > 0 else 0,
                "runId": getattr(result, "run_id", None),
                "error": result.error,
                "metrics": {
                    "seed": seed,
                    "caseId": result.case_id,
                    "llmCalls": result.llm_calls,
                },
                "rewardComponents": {
                    "total": result.total_reward,
                    "violationPenalty": result.violation_penalty,
                },
            }
        except Exception as exc:  # noqa: BLE001
            return {
                "status": "FAILED",
                "reward": 0.0,
                "costCny": 0.0,
                "latencyMs": 0,
                "violations": 0,
                "error": str(exc)[:500],
                "metrics": {"seed": seed},
                "rewardComponents": {},
            }

    def enforce_budget(self, experiment_id: str) -> None:
        detail = self.client.get_experiment(experiment_id)
        exp = detail.get("experiment") or detail
        spent = float(exp.get("spentCny") or 0.0)
        budget = float(exp.get("budgetCny") or 0.0)
        if budget > 0 and spent >= budget:
            raise RuntimeError(f"budget exhausted spent={spent} budget={budget}")


# ---------------------------------------------------------------------------
# Offline helpers (CLI)
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

    merged = json.loads(json.dumps(base_config))
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
    system = ("你是 Policy Optimization Lab 的策略配置变异器。"
              "根据失败证据提出策略配置的定向变异（json）。"
              "只允许修改给定的可变参数，每个变异必须给出针对失败证据的理由。"
              "这不是完整 GEPA，不要假装有 Pareto frontier。")
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
    except Exception as exc:  # noqa: BLE001
        print(f"[reflect] mutation proposal failed: {exc}")
        return []


def run_offline(args: argparse.Namespace) -> int:
    """Legacy offline CLI — writes reports + internal verdict (no Policy Lab promote)."""
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
    print(f"[gen {generation}] Policy Optimization Lab OFFLINE_SEARCH")
    print(f"[gen {generation}] champion={champion['policyId']} "
          f"actives={[b['policyId'] for b in active]}")

    eval_cases = load_cases(Path(args.cases), args.eval_dataset)
    gate_cases = load_cases(Path(args.cases), args.gate_dataset)

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

    champion_gate = evaluate_policy(base, champion["policyId"], gate_cases, args.timeout)
    spent += champion_gate["totalCostCny"]
    print(f"[gate] champion {champion['policyId']}: reward={champion_gate['avgReward']} "
          f"violations={champion_gate['violations']}")

    # Offline mode still records verdict for audit, but Policy Lab active path
    # never auto-promotes via this script when --experiment-id is used.
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
        # Force promote=False in offline when --no-promote (default for lab safety)
        promote = bool(beats and safe and promoted is None and not args.no_promote)
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

    report = {
        "lab": "Policy Optimization Lab",
        "mode": "OFFLINE_SEARCH",
        "modelWeights": "unchanged",
        "method": "bounded_evolutionary_search",
        "notFullGepa": True,
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
        "autoPromote": False,
    }
    path = out_dir / f"generation_{generation}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[done] gen {generation} champion: {report['championBefore']} -> "
          f"{report['championAfter']} cost={spent:.2f} CNY report={path}")
    return 0


def run_worker_loop(args: argparse.Namespace) -> int:
    """policy-lab-worker entry: poll PENDING experiments and run them."""
    client = HttpPolicyLabClient(args.base)
    cases_root = Path(args.cases)
    print("[worker] Policy Lab worker started (no auto-promote)")
    while True:
        try:
            experiments = http(
                "GET", f"{args.base.rstrip('/')}/api/dev/policy-lab/experiments?limit=20",
                headers={"X-Developer-Actor": "policy-lab-worker",
                         "X-Internal-Token": INTERNAL_TOKEN})
            pending = [e for e in (experiments or [])
                       if str(e.get("status", "")).upper() in {"PENDING", "RUNNING"}
                       and not e.get("cancelRequested")]
            for exp in pending:
                eid = exp["experimentId"]
                print(f"[worker] running experiment {eid}")
                runner = PolicyExperimentRunner(
                    client, cases_root=cases_root, timeout_s=args.timeout)
                try:
                    gate = runner.run(eid)
                    print(f"[worker] done {eid} gate.passed={gate.passed}")
                except Exception as exc:  # noqa: BLE001
                    print(f"[worker] experiment {eid} failed: {exc}")
        except Exception as exc:  # noqa: BLE001
            print(f"[worker] poll failed: {exc}")
        time.sleep(15)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Policy Optimization Lab OFFLINE_SEARCH / active PolicyExperimentRunner")
    parser.add_argument("--base", default="http://127.0.0.1")
    parser.add_argument("--cases", default=str(ROOT / "testdata" / "benchmark"))
    parser.add_argument("--eval-dataset", default="gold")
    parser.add_argument("--gate-dataset", default="regression")
    parser.add_argument("--timeout", type=int, default=420)
    parser.add_argument("--budget-cny", type=float, default=5.0)
    parser.add_argument("--out", default=str(ROOT / "reports" / "evolution"))
    parser.add_argument("--experiment-id", default="",
                        help="Run active Policy Lab experiment via HTTP APIs (never promote)")
    parser.add_argument("--worker", action="store_true",
                        help="Daemon mode: poll PENDING experiments")
    parser.add_argument("--no-promote", action="store_true", default=True,
                        help="Offline CLI: do not auto-promote (default true)")
    args = parser.parse_args()

    if args.worker:
        return run_worker_loop(args)

    if args.experiment_id:
        client = HttpPolicyLabClient(args.base)
        runner = PolicyExperimentRunner(
            client, cases_root=Path(args.cases), timeout_s=args.timeout)
        gate = runner.run(args.experiment_id)
        print(json.dumps(gate.to_dict(), ensure_ascii=False, indent=2))
        return 0 if gate.passed else 1

    return run_offline(args)


if __name__ == "__main__":
    sys.exit(main())
