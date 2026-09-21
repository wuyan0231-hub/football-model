"""Synthetic tests check implementation, not real-world predictive quality."""

import copy
import json
import unittest
from datetime import datetime, timedelta, timezone

import numpy as np

from football_model.model import (
    ModelDataError, UnsupportedTeamError, evaluate_time_holdout,
    fit_model, score_distribution,
)


def synthetic_matches(n=360):
    rng = np.random.default_rng(9102)
    start = datetime(2022, 1, 1, 12, tzinfo=timezone.utc)
    attack = np.array([0.55, 0.3, 0.1, -0.1, -0.3, -0.55])
    defence = np.array([0.3, 0.2, 0.1, -0.1, -0.2, -0.3])
    result = []
    for index in range(n):
        home, away = rng.choice(6, 2, replace=False)
        # Three games per UTC date exercise date grouping during evaluation.
        kickoff = start + timedelta(days=index // 3, hours=index % 3)
        result.append({
            "match_id": index + 1, "kickoff": kickoff.isoformat(),
            "home_id": int(home + 1), "away_id": int(away + 1),
            "home_name": f"Synthetic {home + 1}", "away_name": f"Synthetic {away + 1}",
            "home_goals": int(rng.poisson(np.exp(0.15 + 0.2 + attack[home] - defence[away]))),
            "away_goals": int(rng.poisson(np.exp(0.15 + attack[away] - defence[home]))),
        })
    return result


class ModelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.matches = synthetic_matches()
        cls.cutoff = "2022-05-05T00:00:00Z"
        cls.model = fit_model(cls.matches, cls.cutoff, min_train=100)

    def test_synthetic_fit_recovers_direction_and_constraints(self):
        parameters = self.model["team_parameters"]
        self.assertGreater(parameters["1"]["attack"], parameters["6"]["attack"])
        self.assertGreater(parameters["1"]["defence"], parameters["6"]["defence"])
        self.assertAlmostEqual(sum(p["attack"] for p in parameters.values()), 0, places=10)
        self.assertAlmostEqual(sum(p["defence"] for p in parameters.values()), 0, places=10)
        self.assertEqual(self.model["fit_status"], "converged")
        self.assertFalse(self.model["validated_for_production"])
        json.dumps(self.model, allow_nan=False)

    def test_score_matrix_and_outcome_probabilities_are_consistent(self):
        result = score_distribution(self.model, 1, 6, "2022-05-06T12:00:00Z")
        matrix = np.array(result["score_matrix"])
        self.assertTrue(np.all(np.isfinite(matrix)))
        self.assertTrue(np.all(matrix >= 0))
        self.assertAlmostEqual(float(matrix.sum()), 1, places=12)
        self.assertLessEqual(result["tail_mass_before_normalization"], 1e-8)
        probs = result["probabilities"]
        self.assertAlmostEqual(sum(probs.values()), 1, places=12)
        self.assertAlmostEqual(probs["home_win"], np.tril(matrix, -1).sum(), places=12)
        self.assertEqual(result["model_scope"], "baseline_unadjusted_lineup")
        self.assertFalse(result["lineup_adjusted"])
        self.assertNotIn("ev", result)
        neutral = score_distribution(self.model, 1, 6, "2022-05-06T12:00:00Z", neutral=True)
        self.assertAlmostEqual(neutral["expected_goals"]["home"] * np.exp(self.model["home_advantage"]),
                               result["expected_goals"]["home"])
        json.dumps(result, allow_nan=False)

    def test_unknown_team_and_past_forecast_fail_explicitly(self):
        with self.assertRaises(UnsupportedTeamError):
            score_distribution(self.model, 1, 99, "2022-05-06T12:00:00Z")
        with self.assertRaisesRegex(ModelDataError, "未来信息"):
            score_distribution(self.model, 1, 2, "2022-04-01T12:00:00Z")

    def test_future_and_not_yet_available_results_do_not_enter_fit(self):
        future = copy.deepcopy(self.matches[0])
        future.update({"match_id": 9991, "kickoff": "2022-05-10T12:00:00Z", "home_goals": 80})
        recent = copy.deepcopy(future)
        recent.update({"match_id": 9992, "kickoff": "2022-05-04T22:00:00Z"})
        refitted = fit_model(self.matches + [future, recent], self.cutoff, min_train=100)
        self.assertEqual(refitted["training_data_sha256"], self.model["training_data_sha256"])
        self.assertEqual(refitted["team_parameters"], self.model["team_parameters"])
        self.assertEqual(refitted["excluded_after_cutoff_or_availability_delay"], 2)

    def test_invalid_scores_ids_dates_and_duplicates_rejected(self):
        for field, invalid in (("home_goals", -1), ("away_goals", 1.5), ("home_goals", True),
                               ("kickoff", "2022-01-01T12:00:00"), ("home_id", 0)):
            with self.subTest(field=field, invalid=invalid):
                matches = copy.deepcopy(self.matches)
                matches[0][field] = invalid
                with self.assertRaises(ModelDataError):
                    fit_model(matches, self.cutoff, min_train=100)
        with self.assertRaisesRegex(ModelDataError, "重复"):
            fit_model(self.matches + [self.matches[0]], self.cutoff, min_train=100)

    def test_disconnected_groups_cannot_be_compared(self):
        matches = copy.deepcopy(self.matches[:120])
        second_group = copy.deepcopy(matches)
        for match in second_group:
            match["match_id"] += 10000
            match["home_id"] += 100
            match["away_id"] += 100
        with self.assertRaisesRegex(ModelDataError, "不连通"):
            fit_model(matches + second_group, self.cutoff, min_train=100)

    def test_date_grouped_fixed_holdout_and_metrics(self):
        result = evaluate_time_holdout(self.matches, self.cutoff, min_train=100, min_test=30)
        report = result["report"]
        split = report["split"]
        self.assertLess(split["train_last_kickoff"][:10], split["test_start"][:10])
        self.assertTrue(split["train_cutoff"].endswith("T00:00:00Z"))
        self.assertEqual(split["testing_count"] % 3, 0)
        self.assertFalse(report["is_rolling_backtest"])
        self.assertFalse(report["deployment_approved"])
        self.assertEqual(report["coverage"]["ratio"], 1.0)
        self.assertEqual(report["model_metrics"]["n"], report["baseline_metrics"]["n"])
        self.assertGreaterEqual(report["model_metrics"]["log_loss"], 0)
        self.assertGreaterEqual(report["model_metrics"]["brier_score"], 0)
        self.assertLessEqual(report["model_metrics"]["brier_score"], 2)
        self.assertTrue(all(p["forecast"]["training_cutoff"] == split["train_cutoff"] for p in result["predictions"]))

    def test_unknown_holdout_teams_are_counted_not_silently_dropped(self):
        matches = copy.deepcopy(self.matches)
        # Every last-day fixture contains a never-before-seen team.
        for match in matches[-3:]:
            match["home_id"], match["home_name"] = 999, "Unknown synthetic team"
        result = evaluate_time_holdout(matches, self.cutoff, min_train=100, min_test=30)
        report = result["report"]
        self.assertEqual(report["coverage"]["excluded"], 3)
        self.assertEqual(report["coverage"]["scored"] + 3, report["coverage"]["total"])
        self.assertEqual(sum(p["status"] == "excluded_unknown_team" for p in result["predictions"]), 3)
        self.assertEqual(report["model_metrics"]["n"], report["baseline_metrics"]["n"])

    def test_no_supported_holdout_returns_insufficient_coverage(self):
        matches = copy.deepcopy(self.matches)
        for match in matches[-72:]:
            match["home_id"], match["home_name"] = 999, "Unknown synthetic team"
        result = evaluate_time_holdout(matches, self.cutoff, min_train=100, min_test=30)
        report = result["report"]
        self.assertEqual(report["coverage"]["ratio"], 0)
        self.assertEqual(report["validation_status"], "insufficient_supported_test_matches")
        self.assertIsNone(report["model_metrics"])
        self.assertIsNone(report["comparison"])
        self.assertFalse(report["deployment_approved"])

    def test_holdout_outcomes_cannot_change_fitted_model_or_predictions(self):
        before = evaluate_time_holdout(self.matches, self.cutoff, min_train=100, min_test=30)
        matches = copy.deepcopy(self.matches)
        heldout_ids = {p["match_id"] for p in before["predictions"]}
        for match in matches:
            if match["match_id"] in heldout_ids:
                match["home_goals"], match["away_goals"] = 12, 0
        after = evaluate_time_holdout(matches, self.cutoff, min_train=100, min_test=30)
        self.assertEqual(before["model"], after["model"])
        for first, second in zip(before["predictions"], after["predictions"]):
            self.assertEqual(first["forecast"], second["forecast"])
            self.assertEqual(first["baseline_probabilities"], second["baseline_probabilities"])
        self.assertNotEqual(before["report"]["model_metrics"], after["report"]["model_metrics"])

    def test_too_little_data_does_not_claim_trained_or_validated(self):
        with self.assertRaisesRegex(ModelDataError, "至少需要"):
            fit_model(self.matches[:10], self.cutoff)
        with self.assertRaisesRegex(ModelDataError, "至少需要"):
            evaluate_time_holdout(self.matches[:100], self.cutoff)


if __name__ == "__main__":
    unittest.main()
