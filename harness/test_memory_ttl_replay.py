import unittest

from harness.run_memory_ttl_replay import (
    build_report, evaluate_type, normalize_usage, parse_utc_cutover)


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

    def test_normalization_maps_legacy_storage_types_to_ttl_taxonomy(self):
        rows, diagnostics = normalize_usage([
            {"type": "CONVERSATION", "decision": "USED", "ageAtUseSeconds": 60},
            {"type": "PREFERENCE", "decision": "USED", "ageAtUseSeconds": 120},
            {"type": "FAILURE", "decision": "USED", "ageAtUseSeconds": 180},
        ])
        self.assertEqual(["WORKING", "SEMANTIC", "EPISODIC"],
                         [row["type"] for row in rows])
        self.assertEqual(3, diagnostics["legacyTypeRemapped"])
        self.assertEqual({"CONVERSATION": 1, "PREFERENCE": 1, "FAILURE": 1},
                         diagnostics["legacyTypeCounts"])

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

    def test_mixed_legacy_history_is_baseline_only(self):
        report = build_report({
            "_cohort": {"compatibility": "MIXED_LEGACY", "sinceUtc": None},
            "usage": [{
                "type": "EPISODIC", "decision": "USED",
                "ageAtUseSeconds": 60, "finalScore": 0.8,
            }],
        })
        self.assertEqual("BASELINE_ONLY_MIXED_VERSION", report["overallDecision"])

    def test_empty_current_version_cohort_is_not_called_an_optimum(self):
        report = build_report({
            "_cohort": {
                "compatibility": "CURRENT_VERSION",
                "sinceUtc": "2026-07-29 08:03:16",
            },
            "usage": [],
        })
        self.assertEqual("INSUFFICIENT_CURRENT_VERSION_DATA",
                         report["overallDecision"])

    def test_current_cohort_records_producer_compatibility(self):
        report = build_report({
            "_cohort": {
                "compatibility": "CURRENT_VERSION",
                "sinceUtc": "2026-07-29 08:03:16",
                "producerCompatibility": "CURRENT_VERSION",
            },
            "usage": [],
        })
        self.assertEqual("CURRENT_VERSION",
                         report["cohort"]["producerCompatibility"])

    def test_cutover_is_normalized_to_utc(self):
        self.assertEqual("2026-07-29 08:03:16",
                         parse_utc_cutover("2026-07-29T16:03:16+08:00"))


if __name__ == "__main__":
    unittest.main()
