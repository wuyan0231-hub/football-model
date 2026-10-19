import copy
import math
import unittest

from football_model.coverage_audit import TARGET_SCOPE, compare_validation_coverage


SOURCE = {"competition": "epl", "target_scope": TARGET_SCOPE}
CUTOFF = "2026-01-01T00:00:00Z"


def scored(match_id, probabilities=(0.5, 0.3, 0.2), goals=(1, 0)):
    return {"match_id": match_id, "home_id": 10, "away_id": 11,
            "kickoff": "2026-01-03T12:00:00Z", "status": "scored",
            "actual_home_goals": goals[0], "actual_away_goals": goals[1],
            "forecast": {"training_cutoff": CUTOFF, "model_version": "test-v1",
                         "model_scope": "baseline_unadjusted_lineup",
                         "probabilities": dict(zip(("home_win", "draw", "away_win"), probabilities))},
            "baseline_probabilities": {"home_win": 0.4, "draw": 0.3, "away_win": 0.3}}


def excluded(match_id, reason="unknown team"):
    row = scored(match_id)
    row.pop("forecast")
    row.update(status="excluded_unknown_team", reason=reason)
    return row


def validation(rows):
    return {"report": {"evaluation_data_cutoff": "2026-01-10T00:00:00Z",
                       "is_rolling_backtest": False, "minimum_test_matches": 1,
                       "split": {"method": "chronological_fixed_model_holdout", "grouped_by": "kickoff_utc_calendar_date",
                                 "train_cutoff": CUTOFF, "test_start": "2026-01-03T12:00:00Z",
                                 "test_end": "2026-01-03T12:00:00Z", "result_availability_delay_hours": 4},
                       "coverage": {"total": len(rows), "scored": sum(r["status"] == "scored" for r in rows)}},
            "predictions": rows,
            "model": {"training_cutoff": CUTOFF, "model_version": "test-v1", "model_scope": "baseline_unadjusted_lineup",
                      "half_life_days": 180, "ridge": 3}}


def compare(old, new, **kwargs):
    return compare_validation_coverage(old, new, old_source=SOURCE, new_source=SOURCE, **kwargs)


class CoverageAuditTests(unittest.TestCase):
    def test_common_new_removed_scores_and_reasons_keep_denominators(self):
        old = validation([scored(1), excluded(2), scored(3), excluded(4, "venue missing")])
        new = validation([scored(1, (0.6, 0.2, 0.2)), scored(2, goals=(0, 0)), excluded(3, "venue conflict"), excluded(4)])
        result = compare(old, new)
        self.assertEqual(result["status"], "matched_conditions_diagnostic")
        self.assertEqual(result["counts"], {"old_total": 4, "new_total": 4, "old_scored": 2, "new_scored": 2,
                                           "common_scored": 1, "paired_common_scored": 1, "new_only_scored": 1, "removed_scored": 1,
                                           "unpaired_common_scored": 0, "old_excluded": 2, "new_excluded": 2})
        common = result["groups"]["common"]
        self.assertAlmostEqual(common["old"]["model_metrics"]["log_loss"], -math.log(0.5))
        self.assertAlmostEqual(common["new"]["model_metrics"]["brier_score"], 0.24)
        self.assertAlmostEqual(common["paired_diagnostic_delta"]["new_minus_old_log_loss"], -math.log(0.6) + math.log(0.5))
        self.assertEqual(result["groups"]["new_only"]["transitions"][0]["other_reason"], "unknown team")
        self.assertEqual(result["groups"]["removed"]["transitions"][0]["other_reason"], "venue conflict")
        self.assertEqual(result["groups"]["new_only"]["model_metrics"]["n"], 1)
        self.assertFalse(result["prospective_increment_validated"])
        self.assertFalse(result["betting_return_validated"])

    def test_window_and_split_changes_cannot_be_pure_repair_comparison(self):
        old = validation([scored(1)])
        new = copy.deepcopy(old)
        new["report"]["evaluation_data_cutoff"] = "2026-01-11T00:00:00Z"
        new["report"]["split"]["test_end"] = "2026-01-04T12:00:00Z"
        result = compare(old, new)
        self.assertEqual(result["status"], "noncomparable_diagnostic")
        self.assertEqual(result["counts"]["common_scored"], 1)
        self.assertFalse(result["groups"]["common"]["controlled_comparison_eligible"])
        self.assertIn("different_or_unknown_evaluation_data_cutoff", result["comparison_eligibility"]["reasons"])

    def test_changed_result_identity_or_training_cutoff_is_not_paired(self):
        for change in ("actual_home_goals", "home_id", "training_cutoff"):
            with self.subTest(change=change):
                old = validation([scored(1)])
                new = copy.deepcopy(old)
                if change == "training_cutoff":
                    new["predictions"][0]["forecast"][change] = "2026-01-02T00:00:00Z"
                    new["report"]["split"]["train_cutoff"] = "2026-01-02T00:00:00Z"
                    new["model"][change] = "2026-01-02T00:00:00Z"
                else:
                    new["predictions"][0][change] += 1
                result = compare(old, new)
                self.assertEqual(result["groups"]["unpaired_common"]["n"], 1)
                self.assertEqual(result["groups"]["common"]["n"], 0)
                self.assertIsNone(result["groups"]["common"]["paired_diagnostic_delta"])
                self.assertEqual(result["counts"]["new_only_scored"], 0)

    def test_missing_old_validation_does_not_invent_new_coverage(self):
        result = compare(None, validation([scored(1), excluded(2)]))
        self.assertEqual(result["status"], "missing_previous_validation")
        self.assertIsNone(result["counts"]["old_total"])
        self.assertIsNone(result["counts"]["new_only_scored"])
        self.assertEqual(result["groups"]["current_all"]["model_metrics"]["n"], 1)
        self.assertEqual(result["groups"]["new_only"]["n"], 0)

    def test_missing_or_different_target_remains_unverified(self):
        v = validation([scored(1)])
        result = compare_validation_coverage(v, v)
        self.assertFalse(result["comparison_eligibility"]["same_evaluation_conditions"])
        self.assertIn("missing_target_scope", result["comparison_eligibility"]["reasons"])
        result = compare_validation_coverage(v, v, old_source=SOURCE, new_source={**SOURCE, "target_scope": "including_extra_time"})
        self.assertIn("different_target_scope", result["comparison_eligibility"]["reasons"])

    def test_duplicate_invalid_probability_and_incomplete_baseline_do_not_silently_score(self):
        old = validation([scored(1), scored(2)])
        new = validation([scored(1), scored(1), scored(2, (0.5, 0.5, 0.5))])
        result = compare(old, new)
        self.assertEqual(result["counts"]["new_total"], 3)
        self.assertEqual(result["counts"]["new_scored"], 0)
        self.assertIn("duplicate_match_id", [e["reason"] for e in result["data_errors"]["new"]])
        self.assertIn("invalid_1x2_probabilities", [e["reason"] for e in result["data_errors"]["new"]])
        new = validation([scored(1), scored(2)])
        new["predictions"][1].pop("baseline_probabilities")
        result = compare(old, new)
        self.assertIsNone(result["groups"]["common"]["new"]["baseline_metrics"])
        self.assertEqual(result["groups"]["common"]["new"]["model_metrics"]["n"], 2)

    def test_unknown_temporal_fields_remain_unknown_and_present_fields_do_not_approve_pit(self):
        new = validation([scored(1), scored(2)])
        temporal = {"mode": "current_retrospective", "by_match": [
            {"match_id": 1, "eligible": True, "known_at": "2025-12-01T00:00:00Z", "reasons": []},
            {"match_id": 2, "eligible": False, "reasons": ["valid_from_unknown"]}]}
        result = compare(None, new, new_temporal_audit=temporal)
        summary = result["temporal_evidence"]["new"]
        self.assertEqual(summary["provided_eligibility_counts"], {"eligible": 1, "ineligible": 1, "unknown": 0})
        self.assertEqual(summary["missing_or_invalid_time_counts"]["known_at"], 1)
        self.assertEqual(summary["missing_or_invalid_time_counts"]["valid_from"], 2)
        self.assertFalse(summary["historical_point_in_time_validated"])
        self.assertFalse(result["prospective_increment_validated"])

    def test_inputs_are_immutable_and_missing_row_reason_is_explicit(self):
        old, new = validation([scored(1)]), validation([scored(1), scored(2)])
        before = copy.deepcopy((old, new))
        result = compare(old, new)
        self.assertEqual((old, new), before)
        transition = result["groups"]["new_only"]["transitions"][0]
        self.assertEqual(transition["other_status"], "absent_from_validation")
        self.assertIn("不能猜", transition["other_reason"])

    def test_scored_rows_outside_window_or_availability_are_rejected(self):
        cases = [
            ("2025-12-20T00:00:00Z", "kickoff_before_or_unknown_training_cutoff"),
            ("2026-01-11T00:00:00Z", "kickoff_outside_or_unknown_evaluation_window"),
        ]
        for kickoff, reason in cases:
            with self.subTest(kickoff=kickoff):
                v = validation([scored(1)])
                v["predictions"][0]["kickoff"] = kickoff
                result = compare(v, v)
                self.assertEqual(result["status"], "noncomparable_diagnostic")
                self.assertEqual(result["counts"]["new_scored"], 0)
                self.assertIn(reason, [r["reason"] for r in result["data_errors"]["new"]])
        v = validation([scored(1)])
        v["report"]["evaluation_data_cutoff"] = "2026-01-03T13:00:00Z"
        result = compare(v, v)
        self.assertIn("result_unavailable_at_evaluation_cutoff", [r["reason"] for r in result["data_errors"]["new"]])

    def test_forecast_must_belong_to_validation_model_and_match(self):
        for field, wrong in (("model_version", "wrong-model"), ("model_scope", "wrong-scope"), ("match_id", 99)):
            with self.subTest(field=field):
                v = validation([scored(1)])
                v["predictions"][0]["forecast"][field] = wrong
                result = compare(v, v)
                self.assertEqual(result["status"], "noncomparable_diagnostic")
                self.assertEqual(result["counts"]["paired_common_scored"], 0)
                self.assertTrue(result["data_errors"]["new"])


if __name__ == "__main__":
    unittest.main()
