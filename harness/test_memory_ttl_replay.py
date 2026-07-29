import unittest

from harness.run_memory_ttl_replay import build_report, evaluate_type, normalize_usage


class MemoryTtlReplayTest(unittest.TestCase):

    def test_normalization_excludes_missing_and_negative_ages(self):
        rows, diagnostics = normalize_usage([
            {"type": "EPISODIC", "decision": "USED", "ageAtUseSeconds": 86400},
            {"type": "EPISODIC", "decision": "USED", "ageAtUseSeconds": -1},
            {"type": "EPISODIC", "decision": "USED"},
        ])
        self.assertEqual(1, len(rows))
        self.assertEqual(1, diagnostics["negativeAge"])
        self.assertEqual(1, diagnostics["missingAge"])

    def test_replay_proposes_shortest_candidate_only_after_gates_pass(self):
        samples = [
            {"decision": "USED", "ageDays": 1.0, "weight": 1.0},
            {"decision": "USED", "ageDays": 20.0, "weight": 0.8},
            {"decision": "USED", "ageDays": 50.0, "weight": 0.9},
            {"decision": "IGNORED", "ageDays": 110.0, "weight": 0.2},
        ]
        result = evaluate_type(
            "EPISODIC", samples, default_days=90,
            candidates=[30, 60, 90, 180], min_used_samples=3,
            retention_floor=0.99, coverage_ratio=0.8)
        self.assertTrue(result["conclusive"])
        self.assertEqual(60, result["proposedTtlDays"])
        by_ttl = {row["ttlDays"]: row for row in result["candidates"]}
        self.assertLess(by_ttl[30]["usedRetention"], 0.99)
        self.assertEqual(1.0, by_ttl[60]["usedRetention"])

    def test_short_history_never_changes_defaults(self):
        payload = {
            "usage": [
                {"type": "PROCEDURAL", "decision": "USED",
                 "ageAtUseSeconds": 60, "finalScore": 0.8}
                for _ in range(40)
            ]
        }
        report = build_report(payload, min_used_samples=30)
        procedural = next(
            row for row in report["typeResults"] if row["type"] == "PROCEDURAL")
        self.assertFalse(procedural["conclusive"])
        self.assertIsNone(procedural["proposedTtlDays"])
        self.assertEqual("KEEP_CURRENT_DEFAULTS_INSUFFICIENT_DATA",
                         report["overallDecision"])


if __name__ == "__main__":
    unittest.main()
