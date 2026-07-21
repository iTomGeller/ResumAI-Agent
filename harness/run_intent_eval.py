#!/usr/bin/env python3
"""EXP-6: intent rule-layer accuracy + LLM fallback rate on a labeled set.

The rule layer must (a) never misroute control/mutation intents and (b) hand
open-ended messages to the LLM second pass (gold=UNCLASSIFIED) instead of
guessing. Reports per-class accuracy and the fallback (LLM-call) rate.

Usage (ECS): python3 harness/run_intent_eval.py --base http://127.0.0.1
"""
from __future__ import annotations

import argparse
import json
import time
import urllib.request
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def http(method: str, url: str, body: dict | None = None, timeout: float = 30.0):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(url, data=data, method=method, headers={
        "Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1")
    parser.add_argument("--cases", default=str(
        ROOT / "testdata" / "benchmark" / "intent_cases.json"))
    parser.add_argument("--out", default=str(ROOT / "reports" / "experiments"))
    args = parser.parse_args()
    base = args.base.rstrip("/")
    cases = json.loads(Path(args.cases).read_text(encoding="utf-8"))

    per_class = defaultdict(lambda: {"total": 0, "correct": 0})
    misroutes = []
    fallback = 0
    correct = 0
    for case in cases:
        result = http("POST", f"{base}/api/conversations/intent-preview",
                      {"content": case["content"]})
        predicted = result.get("intent")
        gold = case["gold"]
        per_class[gold]["total"] += 1
        if result.get("llmSecondPass"):
            fallback += 1
        ok = predicted == gold
        # action-level check for control commands
        if ok and case.get("goldAction"):
            ok = result.get("action") == case["goldAction"]
        if ok:
            correct += 1
            per_class[gold]["correct"] += 1
        else:
            misroutes.append({
                "content": case["content"], "gold": gold,
                "goldAction": case.get("goldAction"),
                "predicted": predicted, "action": result.get("action"),
                "note": case.get("note"),
            })

    report = {
        "experiment": "intent_rule_layer",
        "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "cases": len(cases),
        "accuracy": round(correct / max(1, len(cases)), 4),
        "llmFallbackRate": round(fallback / max(1, len(cases)), 4),
        "perClass": {k: {
            "total": v["total"],
            "accuracy": round(v["correct"] / max(1, v["total"]), 4),
        } for k, v in sorted(per_class.items())},
        "misroutes": misroutes,
    }
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "intent_eval.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: report[k] for k in
                      ("cases", "accuracy", "llmFallbackRate", "perClass")},
                     ensure_ascii=False, indent=2))
    print(f"misroutes={len(misroutes)}")
    for m in misroutes:
        print("  -", m["content"], "gold=", m["gold"], "pred=", m["predicted"])
    print(f"report -> {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
