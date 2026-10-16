"""Retain the legitimate revision status that broke the actual 2026-10-16 summary."""
import copy
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest

from football_model.run_summary import _cumulative, _independent_results, _digest


# Exact failing match-status row from saved state
# 53c88a9a80b111b1909a4680cd9e490b229390a0, prospective/latest.json,
# Git blob 380fd1742e358fed00157fba9fe30b5b8aec9601, raw SHA-256
# f3843338c1f4239ea1d2cfc7226f13a16193690efe781abd9323bc6b5d3af608.
# A minimal actual witness, not an assertion that this is the full 420-row original.
ACTUAL_REVISION_ROW = {
    "competition": "primeira", "match_id": 1575513,
    "primary_forecast_id": "269db19d09e776d5dadf25f23fb8fd796f7359601b2f180a62becc2d27b8ce91",
    "protocol_id": "ca504adf06b04a4e4d7d58bb97dbf767357c117fb7e5aeef4e32737ceff8d8eb",
    "result_id": None,
    "schedule_revision_id": "c7f80f3d507978b620da42c3e852abfa84144c3787299fed699a8da0c634890a",
    "status": "unscorable_schedule_revision"}
SOURCE = "9f5ec2a767b0b9873f4078fad981926b71bfc69b"


class ScheduleRevisionSummaryTests(unittest.TestCase):
    def summary(self, rows):
        return {"generated_at": "2026-10-16T01:57:57Z", "forecast_versions": len(rows),
                "unique_primary_matches_by_protocol": len(rows), "result_versions": 0,
                "match_statuses": rows}

    def test_actual_revision_row_is_retained_without_becoming_scored_or_pending(self):
        result = _cumulative(self.summary([copy.deepcopy(ACTUAL_REVISION_ROW)]))
        self.assertEqual(result["status"], "associated_current_cumulative_view")
        self.assertEqual(result["unique_primary_matches_by_protocol"], 1)
        self.assertEqual(result["unscorable_schedule_revision_matches_by_protocol"], 1)
        self.assertEqual(result["unscorable_schedule_revision_unique_matches_across_protocols"], 1)
        self.assertEqual(result["mature_scored_matches_by_protocol"], 0)
        self.assertEqual(result["pending_matches_by_protocol"], 0)

    def test_repeated_protocols_keep_protocol_denominator_and_unique_fixture_distinct(self):
        revision = copy.deepcopy(ACTUAL_REVISION_ROW)
        repeated = {**revision, "protocol_id": "another-protocol"}
        scored = {**revision, "match_id": 1575514, "status": "scored"}
        pending = {**revision, "match_id": 1575515, "status": "pending"}
        result = _cumulative(self.summary([revision, repeated, scored, pending]))
        self.assertEqual(result["unique_primary_matches_by_protocol"], 4)
        self.assertEqual(result["unique_matches_across_protocols"], 3)
        self.assertEqual(result["unscorable_schedule_revision_matches_by_protocol"], 2)
        self.assertEqual(result["unscorable_schedule_revision_unique_matches_across_protocols"], 1)
        self.assertEqual(result["mature_scored_matches_by_protocol"], 1)
        self.assertEqual(result["pending_matches_by_protocol"], 1)

    def test_unknown_status_and_duplicate_identity_are_still_rejected(self):
        with self.assertRaisesRegex(ValueError, "unknown_cumulative_status"):
            _cumulative(self.summary([{**ACTUAL_REVISION_ROW, "status": "invented_success"}]))
        with self.assertRaisesRegex(ValueError, "duplicate_cumulative_identity"):
            _cumulative(self.summary([ACTUAL_REVISION_ROW, ACTUAL_REVISION_ROW]))

    def test_missing_match_statuses_is_unknown_instead_of_zero(self):
        result = _cumulative({"forecast_versions": 1})
        self.assertIsNone(result["unscorable_schedule_revision_matches_by_protocol"])
        self.assertIsNone(result["unscorable_schedule_revision_unique_matches_across_protocols"])

    def independent(self, *, corrupt_hash=False):
        temporary = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1])
        self.addCleanup(temporary.cleanup)
        runtime = Path(temporary.name)
        score = self.summary([copy.deepcopy(ACTUAL_REVISION_ROW)])
        score_id = _digest(score)
        path = runtime / "prospective/result_sync_scores" / (score_id + ".json")
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({**score, "forecast_versions": 99} if corrupt_hash else score))
        report = {"generated_at": "2026-10-16T01:57:58Z", "run_started_at": "2026-10-16T01:51:20Z",
                  "source_commit": SOURCE, "target_matches": 1, "api_requests": 1,
                  "counts": {"unscorable_schedule_revision": 1}, "score_summary_id": score_id}
        latest = runtime / "result_sync/reports/latest.json"
        latest.parent.mkdir(parents=True)
        latest.write_text(json.dumps(report))
        context = {"source_commit": SOURCE, "stage_started_at": {"result_sync": "2026-10-16T01:51:20Z"}}
        return _independent_results(runtime, context, datetime(2026, 10, 16, 2, tzinfo=timezone.utc))

    def test_independent_result_report_can_expose_its_legitimate_cumulative_revision(self):
        result = self.independent()
        self.assertEqual(result["status_counts"]["unscorable_schedule_revision"], 1)
        self.assertEqual(result["cumulative"]["unscorable_schedule_revision_matches_by_protocol"], 1)

    def test_independent_score_hash_gate_is_not_relaxed(self):
        with self.assertRaisesRegex(ValueError, "independent_score_summary_mismatch"):
            self.independent(corrupt_hash=True)


if __name__ == "__main__":
    unittest.main()
