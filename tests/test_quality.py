"""Protocol tests are implementation checks, not predictive-quality evidence."""

import copy
import json
import unittest
from unittest.mock import patch
from datetime import datetime, timedelta, timezone

import numpy as np

from football_model.model import ModelDataError
from football_model.quality import (
    CANDIDATES, _binary_metrics, _over_state, _select,
    evaluate_walk_forward, mix_matrices, summarize_distribution,
)


def matches(n=100):
    rng = np.random.default_rng(127)
    start = datetime(2024, 1, 1, 12, tzinfo=timezone.utc)
    result = []
    for i in range(n):
        h, a = rng.choice(6, 2, replace=False) + 1
        result.append({"match_id": i + 1, "kickoff": (start + timedelta(days=i // 2, hours=i % 2)).isoformat(),
                       "home_id": int(h), "away_id": int(a), "home_name": f"Team {h}", "away_name": f"Team {a}",
                       "home_goals": int(rng.poisson(1.5)), "away_goals": int(rng.poisson(1.1))})
    return result


class QualityTests(unittest.TestCase):
    def replay(self, source):
        return evaluate_walk_forward(source, "2024-02-10T00:00:00Z", "2024-02-20T00:00:00Z",
                                     step_days=3, min_train=50, minimum_selection_matches=4)

    def test_mixture_uses_probabilities_and_handles_different_shapes(self):
        first = np.array([[0.25, 0.25], [0.25, 0.25]])
        second = np.array([[1.0, 0.0, 0.0]])
        result = mix_matrices(first, second)
        self.assertEqual(result.shape, (2, 3))
        self.assertAlmostEqual(result.sum(), 1.0)
        self.assertAlmostEqual(result[0, 0], 0.625)
        for invalid in ([[-1, 2]], [[0.1, 0.1]], [1.0], [[float("nan")]]):
            with self.assertRaises(ModelDataError):
                mix_matrices(invalid, first)

    def test_fixed_events_and_quarter_settlements(self):
        matrix = np.zeros((4, 4))
        matrix[2, 0], matrix[2, 1], matrix[0, 0] = 0.2, 0.3, 0.5
        summary = summarize_distribution(matrix, {"home": 1.0, "away": 0.3})
        self.assertAlmostEqual(summary["events"]["over_2_5"], 0.3)
        self.assertAlmostEqual(summary["events"]["home_win_by_2_plus"], 0.2)
        self.assertAlmostEqual(summary["settlement_probabilities"]["over_2_25"]["half_loss"], 0.2)
        self.assertAlmostEqual(summary["settlement_probabilities"]["over_2_75"]["half_win"], 0.3)
        self.assertEqual(_over_state(2, 9), "half_loss")
        self.assertEqual(_over_state(3, 11), "half_win")

    def test_future_outcomes_do_not_change_prior_forecasts_parameters_or_selection(self):
        source = matches()
        before = self.replay(source)
        modified = copy.deepcopy(source)
        # Changed games remain unavailable at every origin <= February 16.
        for row in modified:
            if row["kickoff"] >= "2024-02-16":
                row["home_goals"], row["away_goals"] = 10, 0
        after = self.replay(modified)
        for left, right in zip(before["windows"], after["windows"]):
            if left["origin"] <= "2024-02-16T00:00:00Z":
                for key in ("training_data_sha256", "team_model_parameters", "selection"):
                    self.assertEqual(left[key], right[key])
        for left, right in zip(before["predictions"], after["predictions"]):
            if left["forecast_origin"] <= "2024-02-16T00:00:00Z":
                self.assertEqual(left["forecasts"], right["forecasts"])
                self.assertEqual(left["effective_policy_candidate"], right["effective_policy_candidate"])

    def test_four_hour_embargo_applies_to_training_and_sequential_selection(self):
        source = matches()
        recent = copy.deepcopy(source[0])
        recent.update({"match_id": 1001, "kickoff": "2024-02-09T22:00:00Z", "home_goals": 40})
        before, after = self.replay(source), self.replay(source + [recent])
        self.assertEqual(before["windows"][0]["training_data_sha256"], after["windows"][0]["training_data_sha256"])
        self.assertNotEqual(before["windows"][1]["training_data_sha256"], after["windows"][1]["training_data_sha256"])
        previous = [{"common_coverage": True, "result_assumed_available_at": "2024-02-10T02:00:00Z",
                     "actual_home_goals": 1, "actual_away_goals": 0,
                     "forecasts": {name: {"probabilities": {"home_win": 0.8 if name == "team_poisson" else 0.2}}
                                   for name in CANDIDATES}}]
        origin = datetime(2024, 2, 10, tzinfo=timezone.utc)
        self.assertEqual(_select(previous, origin, 1)["candidate"], "league_mean")
        self.assertEqual(_select(previous, origin + timedelta(hours=2), 1)["candidate"], "team_poisson")

    def test_unknown_teams_reduce_common_coverage_without_hiding_baseline_rows(self):
        source = matches()
        for row in source:
            if row["kickoff"][:10] == "2024-02-10":
                row["home_id"], row["home_name"] = 1001, "Previously unseen"
        result = self.replay(source)
        coverage = result["report"]["coverage"]
        self.assertEqual(coverage["by_candidate"]["league_mean"], coverage["total"])
        self.assertEqual(coverage["common"], coverage["total"] - 2)
        counts = [value["n"] for value in result["report"]["common_sample_metrics"].values()]
        self.assertEqual(len(set(counts)), 1)
        first = result["windows"][0]
        self.assertEqual(first["selection"]["candidate"], "league_mean")

    def test_calibration_and_losses_have_reconstructible_definitions(self):
        value = _binary_metrics([0, 0.5, 1.0], [False, True, True])
        self.assertAlmostEqual(value["log_loss"], np.log(2) / 3)
        self.assertAlmostEqual(value["brier_score"], 0.25 / 3)
        self.assertEqual(sum(row["n"] for row in value["calibration"]), 3)
        self.assertEqual(value["calibration"][-1]["n"], 1)

    def test_replay_never_claims_live_validation_or_automatic_deployment(self):
        result = self.replay(matches())
        report = result["report"]
        self.assertEqual(report["validation_status"], "historical_development_replay")
        self.assertFalse(report["deployment_approved"])
        self.assertFalse(report["point_in_time_sources_verified"])
        self.assertFalse(report["prospective_validation"])
        json.dumps(result, allow_nan=False)
        for window in result["windows"]:
            self.assertLessEqual(window["last_result_assumed_available_at"], window["origin"])
            self.assertEqual(window["training_data_sha256"], window["team_model_parameters"]["training_data_sha256"])

    def test_invalid_protocol_rejected_before_fit(self):
        with self.assertRaises(ModelDataError):
            evaluate_walk_forward(matches(), "2024-02-10T12:00:00Z", "2024-02-20T00:00:00Z")
        with self.assertRaises(ModelDataError):
            evaluate_walk_forward(matches(), "2024-02-10T00:00:00Z", "2024-02-20T00:00:00Z", step_days=0)

    def test_at_bound_fit_excluded_consistently_with_production(self):
        with patch("football_model.quality.fit_model", return_value={"optimizer": {"parameter_at_bound": True}}):
            result = self.replay(matches())
        coverage = result["report"]["coverage"]
        self.assertEqual(coverage["common"], 0)
        self.assertEqual(coverage["by_candidate"]["team_poisson"], 0)
        self.assertEqual(coverage["by_candidate"]["poisson_mix_50"], 0)
        self.assertEqual(coverage["by_candidate"]["league_mean"], coverage["total"])
        self.assertIsNone(result["report"]["common_sample_metrics"]["team_poisson"]["one_x_two"])
        self.assertIn("触及优化边界", result["windows"][0]["team_model_fit_error"])
        self.assertIsNotNone(result["windows"][0]["rejected_team_model_parameters"])


if __name__ == "__main__":
    unittest.main()
