import copy
from datetime import datetime
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from football_model.run_summary import build_summary, markdown, main, _canonical, _digest


class RunSummaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.now = datetime.fromisoformat("2026-10-07T10:05:00+00:00")
        self.run_id = "20261007T100001000000Z"
        self.context = {"source_commit": "a" * 40, "mode": "train", "league": "epl",
            "stage_started_at": {"model": "2026-10-07T10:00:00Z",
                "prospective": "2026-10-07T10:01:01Z", "fundamentals": "2026-10-07T10:02:01Z"}}
        self.outcomes = dict.fromkeys(("restore", "model", "replay", "prospective", "fundamentals", "check", "persist"), "success")
        self.report = {"run_id": self.run_id, "mode": "train", "source_commit": "a" * 40,
            "run_started_at": "2026-10-07T10:00:01Z", "generated_at": "2026-10-07T10:01:00Z",
            "requested_competitions": ["epl"], "competitions": [{"key": "epl", "forecast_availability": {
                "status": "available", "upcoming_count": 2, "supported_count": 2, "unsupported_count": 0}}]}
        self.write("reports/latest.json", self.report)
        raw = (self.root / "reports/latest.json").read_bytes()
        source = _digest({"kind": "run_report", "original_bytes_sha256": hashlib.sha256(raw).hexdigest(), "content": self.report})
        base = {"run_id": self.run_id, "competition": "epl", "report_source": source,
                "captured_at": "2026-10-07T10:01:02Z"}
        forecast = {**base, "match_id": 1}
        fid = self.append("forecasts", forecast)
        self.accepted = {**base, "match_id": 1, "status": "prospective", "forecast_id": fid}
        self.rejected = {**base, "match_id": 2, "status": "rejected",
            "reason": "model_fitted_at 必须是包含时区的 ISO 时间字符串。"}
        self.append("coverage", self.accepted)
        self.append("coverage", self.rejected)
        # Old cumulative objects must not enter this run's counts.
        self.append("coverage", {**base, "run_id": "old", "match_id": 9, "status": "prospective"})
        self.prospective = {"generated_at": "2026-10-07T10:02:00Z", "current_run_id": self.run_id,
            "forecast_versions": 7, "unique_primary_matches_by_protocol": 5, "result_versions": 3,
            "match_statuses": [{"competition": "epl", "match_id": i, "protocol_id": "p1",
                "status": "scored" if i <= 2 else "pending"} for i in range(1, 6)]}
        self.write("prospective/latest.json", self.prospective)
        base_fact = {"forecast_run_id": self.run_id, "status": "features_saved", "missing": []}
        self.facts = {"run_started_at": "2026-10-07T10:02:02Z", "generated_at": "2026-10-07T10:03:00Z",
            "status": "partial", "candidate_count": 3, "matches": [copy.deepcopy(base_fact),
                {**base_fact, "missing": [{"reason": "lineup_window_not_open"}]},
                {**base_fact, "missing": [{"reason": "snapshot_save_failed"}]}]}
        self.write("fundamentals/reports/latest.json", self.facts)

    def write(self, name, value):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(_canonical(value))

    def append(self, collection, value):
        object_id = _digest(value)
        self.write(f"prospective/{collection}/{object_id}.json", value)
        return object_id

    def build(self):
        return build_summary(self.root, self.context, self.outcomes, now=self.now)

    def test_model_success_capture_failure_saved_state_remain_separate(self):
        self.outcomes["prospective"] = "failure"
        self.outcomes["fundamentals"] = "failure"
        result = self.build()
        self.assertEqual(result["necessary_steps_outcome"], "failure")
        self.assertEqual(result["necessary_steps_not_successful"], ["prospective"])
        self.assertEqual(result["state_save_outcome"], "success")
        self.assertEqual(result["model"]["supported_predictions"], 2)
        self.assertEqual(result["prospective_current"]["observed_accepted_predictions"], 1)
        self.assertEqual(result["prospective_current"]["observed_rejected_predictions"], 1)
        self.assertEqual(result["prospective_current"]["reason_counts"], {"model_fitted_at_missing_or_invalid": 1})
        self.assertFalse(result["summary_saved_in_same_model_state_commit"])

    def test_current_comes_from_ledger_not_cumulative_subtraction(self):
        self.prospective["forecast_versions"] = 900
        self.write("prospective/latest.json", self.prospective)
        result = self.build()
        self.assertEqual(result["prospective_current"]["observed_accepted_predictions"], 1)
        self.assertEqual(result["prospective_cumulative"]["forecast_versions"], 900)
        self.assertEqual(result["prospective_cumulative"]["mature_scored_matches_by_protocol"], 2)

    def test_missing_current_coverage_is_unknown_even_with_cumulative(self):
        for path in (self.root / "prospective/coverage").glob("*.json"):
            path.unlink()
        result = self.build()
        self.assertEqual(result["prospective_current"]["status"], "unknown")
        self.assertNotIn("observed_accepted_predictions", result["prospective_current"])
        self.assertEqual(result["prospective_cumulative"]["forecast_versions"], 7)

    def test_stale_latest_model_is_never_current(self):
        self.context["stage_started_at"]["model"] = "2026-10-07T10:04:00Z"
        result = self.build()
        self.assertEqual(result["model"]["status"], "unknown")
        self.assertEqual(result["prospective_cumulative"]["status"], "unknown")

    def test_wrong_source_commit_is_unknown(self):
        self.context["source_commit"] = "b" * 40
        self.assertEqual(self.build()["model"]["status"], "unknown")

    def test_stale_or_different_prospective_latest_not_accepted(self):
        for change in ({"current_run_id": "old"}, {"generated_at": "2026-10-06T10:02:00Z"}):
            with self.subTest(change=change):
                self.write("prospective/latest.json", {**self.prospective, **change})
                result = self.build()
                self.assertEqual(result["prospective_cumulative"]["status"], "unknown")
                self.assertEqual(result["prospective_current"]["observed_accepted_predictions"], 1)

    def test_normal_missing_partial_and_complete_have_distinct_counts(self):
        self.outcomes["fundamentals"] = "failure"
        result = self.build()
        self.assertEqual(result["necessary_steps_outcome"], "success")
        facts = result["fundamentals"]
        self.assertEqual([facts[key] for key in ("complete_collection_matches", "partial_collection_matches", "normal_missing_matches")], [1, 1, 1])
        self.assertEqual(facts["features_saved_matches"], 3)
        self.assertFalse(facts["is_model_probability_gate"])

    def test_fundamentals_forecast_link_and_stage_time_required(self):
        for mismatch in ("forecast", "time"):
            facts = copy.deepcopy(self.facts)
            if mismatch == "forecast":
                facts["matches"][0]["forecast_run_id"] = "previous"
            else:
                facts["run_started_at"] = "2026-10-07T10:02:00Z"
            self.write("fundamentals/reports/latest.json", facts)
            self.assertEqual(self.build()["fundamentals"]["status"], "unknown")

    def test_missing_count_not_silently_zeroed(self):
        self.context["league"] = "all"
        self.report["requested_competitions"].append("laliga")
        self.write("reports/latest.json", self.report)
        model = self.build()["model"]
        self.assertEqual(model["known_supported_predictions"], 2)
        self.assertIsNone(model["supported_predictions"])
        self.assertEqual(model["unknown_prediction_coverage_competitions"], 1)

    def test_arbitrary_errors_never_copied_to_json_or_markdown(self):
        secret = "some_secret_value_abc123"
        old = self.root / "prospective/coverage" / (_digest(self.rejected) + ".json")
        old.unlink()
        self.append("coverage", {**self.rejected, "reason": secret})
        self.facts["matches"][0]["missing"] = [{"reason": secret}]
        self.write("fundamentals/reports/latest.json", self.facts)
        self.outcomes["check"] = secret
        result = self.build()
        self.assertNotIn(secret, json.dumps(result) + markdown(result))
        self.assertEqual(result["prospective_current"]["reason_counts"], {"unclassified_reason": 1})
        self.assertEqual(result["output_check_outcome"], "unknown")

    def test_integrity_failure_does_not_fabricate_accepted_zero(self):
        self.write("prospective/forecasts/" + self.accepted["forecast_id"] + ".json", {"changed": True})
        self.assertEqual(self.build()["prospective_current"]["status"], "unknown")

    def test_duplicate_run_match_coverage_does_not_double_count(self):
        self.append("coverage", {**self.rejected, "captured_at": "2026-10-07T10:01:03Z"})
        self.assertEqual(self.build()["prospective_current"]["status"], "unknown")

    def test_cross_protocol_deduplication_separate_from_protocol_sample_count(self):
        self.prospective["match_statuses"].append({"competition": "epl", "match_id": 1, "protocol_id": "p2", "status": "scored"})
        self.prospective["unique_primary_matches_by_protocol"] = 6
        self.write("prospective/latest.json", self.prospective)
        cumulative = self.build()["prospective_cumulative"]
        self.assertEqual(cumulative["unique_matches_across_protocols"], 5)
        self.assertEqual(cumulative["mature_scored_matches_by_protocol"], 3)
        self.assertEqual(cumulative["mature_unique_matches_across_protocols"], 2)

    def test_audit_skips_prospective_without_becoming_failed(self):
        self.context["mode"] = self.report["mode"] = "audit"
        self.outcomes["prospective"] = self.outcomes["fundamentals"] = "skipped"
        self.write("reports/latest.json", self.report)
        result = self.build()
        self.assertEqual(result["necessary_steps_outcome"], "success")
        self.assertEqual(result["prospective_current"]["status"], "unknown")

    def test_workflow_keeps_true_outcomes_and_original_failure_gate(self):
        text = (Path(__file__).parents[1] / ".github/workflows/football.yml").read_text()
        for clause in ("steps.model.outcome != 'success'", "steps.persist.outcome != 'success'",
                       "steps.prospective.outcome == 'failure'", "steps.replay_check.outcome == 'failure'"):
            self.assertIn(clause, text)
        self.assertGreaterEqual(text.count("continue-on-error: true"), 6)
        self.assertNotIn(".conclusion", text)
        self.assertLess(text.index("id: persist"), text.index("id: summary"))

    def test_render_without_context_or_with_invalid_context_is_unknown_and_safe(self):
        context = self.root / "missing-context.json"
        output, rendered = self.root / "summary.json", self.root / "summary.md"
        for bad_context in (None, "malformed secret_do_not_echo"):
            if bad_context is not None:
                context.write_text(bad_context)
            env = {key.upper() + "_OUTCOME": val for key, val in self.outcomes.items()}
            env["MODE"] = "train"
            with patch.dict("os.environ", env, clear=True):
                self.assertEqual(main(["render", "--context", str(context), "--runtime", str(self.root),
                    "--output", str(output), "--summary", str(rendered)]), 0)
            result = json.loads(output.read_text())
            self.assertEqual(result["model"]["status"], "unknown")
            self.assertNotIn("secret_do_not_echo", output.read_text() + rendered.read_text())


if __name__ == "__main__":
    unittest.main()
