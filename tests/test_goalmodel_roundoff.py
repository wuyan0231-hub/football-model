"""Regression for the actual fixed-input UNL G1 aggregation failure."""
import copy
import json
import math
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

import numpy as np
from scipy.stats import poisson

from football_model.advanced_research import predict_saved_goalmodel
from football_model.feature_research import score_forecast
from football_model.goalmodel_adapter import fit_predict_goalmodel, goalmodel_probabilities
from football_model.model import ModelDataError, TAIL_TOLERANCE
from tests.test_goalmodel_adapter import output_for, sample_fixtures, sample_matches


FIXTURE = Path(__file__).parent / "fixtures/unl-goalmodel-roundoff-1545612.json"


def real_case():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def real_grid():
    rates = real_case()["original_numeric"]["expected_goals"]
    limit = max(2, *(int(poisson.ppf(1 - TAIL_TOLERANCE / 4, rate))
                     for rate in rates.values()))
    raw = np.outer(poisson.pmf(np.arange(limit + 1), rates["home"]),
                   poisson.pmf(np.arange(limit + 1), rates["away"]))
    return raw / raw.sum(), raw


class GoalmodelRoundoffTests(unittest.TestCase):
    def test_one_ulp_mass_residual_is_bounded_without_mutating_matrix(self):
        for matrix in (np.array([[0.0, math.nextafter(1.0, math.inf)], [0.0, 0.0]]),
                       np.array([[0.0, 0.0], [math.nextafter(1.0, math.inf), 0.0]])):
            with self.subTest(matrix=matrix.tolist()):
                before = matrix.tobytes()
                result = goalmodel_probabilities(matrix)
                self.assertEqual(matrix.tobytes(), before)
                self.assertEqual(math.fsum(result.values()), 1.0)
                self.assertTrue(all(0 <= value <= 1 for value in result.values()))
                self.assertEqual(max(result.values()), 1.0)

    def test_real_saved_fit_preserves_rates_and_full_grid_and_scores(self):
        case = real_case()
        self.assertEqual(case["diagnostic_run_id"], 38414005169)
        self.assertGreater(float(case["original_numeric"]["probability_repr"]["away_win"]), 1)
        prediction = predict_saved_goalmodel(case["card"], case["fixture"],
                                             case["features"], case["as_of"])
        self.assertEqual(prediction["expected_goals"], case["original_numeric"]["expected_goals"])
        matrix, _ = real_grid()
        np.testing.assert_array_equal(prediction["score_matrix"], matrix)
        self.assertEqual(matrix.shape, (110, 110))
        self.assertEqual(prediction["probabilities"]["away_win"], 1.0)
        self.assertGreater(prediction["probabilities"]["draw"], 0)
        self.assertGreater(prediction["probabilities"]["home_win"], 0)
        result = score_forecast(prediction, case["observed"], extended_diagnostics=True)
        self.assertTrue(math.isfinite(result["scoreline_nll"]))
        self.assertTrue(math.isfinite(result["one_x_two_nll"]))
        self.assertFalse(prediction["validated_for_production"])
        self.assertFalse(prediction["automatic_promotion"])

    def test_early_adapter_uses_same_bounded_view_preserving_native_r_fields(self):
        native = output_for(sample_fixtures())
        matrix, raw = real_grid()
        rates = real_case()["original_numeric"]["expected_goals"]
        forecast = native["forecasts"][0]
        forecast.update(expected_home_goals=rates["home"], expected_away_goals=rates["away"],
                        score_matrix=matrix.tolist(), tail_mass_before_normalization=1 - float(raw.sum()),
                        probabilities_1x2={"home": float(np.tril(matrix, -1).sum()),
                                           "draw": float(np.trace(matrix)),
                                           "away": float(np.triu(matrix, 1).sum())})
        original = copy.deepcopy(forecast)

        def fake_r(command, **kwargs):
            Path(command[-1]).write_text(json.dumps(native), encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, "", "")

        with patch("football_model.goalmodel_adapter.shutil.which", return_value="Rscript"), \
                patch("football_model.goalmodel_adapter.subprocess.run", side_effect=fake_r):
            result = fit_predict_goalmodel(sample_matches(), sample_fixtures(),
                                           cutoff="2024-05-01T00:00:00Z")
        self.assertEqual(result["status"], "ok")
        forecast = result["forecasts"][0]
        self.assertEqual(forecast["probabilities_1x2"], original["probabilities_1x2"])
        self.assertEqual(forecast["score_matrix"], original["score_matrix"])
        self.assertEqual(forecast["expected_goals"], rates)
        self.assertEqual(forecast["probabilities"], goalmodel_probabilities(matrix))
        score_forecast(forecast, {"home_goals": 1, "away_goals": 0})

    def test_invalid_cells_shapes_and_material_mass_error_are_not_repaired(self):
        for matrix in ([[0, 1.00000000000001], [0, 0]], [[0, 0.999999], [0, 0]],
                       [[0, 1], [-1e-30, 0]], [[0, float("nan")], [0, 0]],
                       [[0, float("inf")], [0, 0]], [[1e308, 1e308], [0, 0]],
                       [[0, 0], [0, 0]], [[1]], [], [1, 0], [[0, 1], [0]],
                       np.zeros((129, 2))):
            with self.subTest(matrix=str(matrix)[:80]), self.assertRaises(ModelDataError):
                goalmodel_probabilities(matrix)

    def test_observed_legal_native_mass_residual_does_not_reject_entire_batch(self):
        # Engineering reproduction of the measured 1.5543122344752192e-15
        # residual, not a claim these perturbed cells are original R bytes.
        fixtures = sample_fixtures() + [{**sample_fixtures()[0], "match_id": 1000}]
        native = output_for(fixtures)
        first = native["forecasts"][0]
        matrix = np.asarray(first["score_matrix"])
        matrix[0, 0] += 7 * np.finfo(float).eps
        self.assertGreater(abs(math.fsum(matrix.ravel()) - 1), 1e-15)
        self.assertLess(abs(float(matrix.sum()) - 1), 1e-10)
        first["score_matrix"] = matrix.tolist()
        first["probabilities_1x2"] = {"home": float(np.tril(matrix, -1).sum()),
                                      "draw": float(np.trace(matrix)),
                                      "away": float(np.triu(matrix, 1).sum())}
        real_native = json.loads((FIXTURE.parent / "unl-native-r-target-1545612.json").read_text())
        # Reuse actual native matrix/rates/aggregates; mock fixture identity is
        # explicit because this is an adapter batch protocol test, not R fitting.
        native["forecasts"][1] = {**real_native["native_forecast"], **fixtures[1]}
        original = copy.deepcopy(native)

        def fake_r(command, **kwargs):
            Path(command[-1]).write_text(json.dumps(native), encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, "", "")

        with patch("football_model.goalmodel_adapter.shutil.which", return_value="Rscript"), \
                patch("football_model.goalmodel_adapter.subprocess.run", side_effect=fake_r):
            result = fit_predict_goalmodel(sample_matches(), fixtures, cutoff="2024-05-01T00:00:00Z")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(len(result["forecasts"]), 2)
        for forecast, untouched in zip(result["forecasts"], original["forecasts"]):
            self.assertEqual(forecast["score_matrix"], untouched["score_matrix"])
            self.assertEqual(forecast["probabilities_1x2"], untouched["probabilities_1x2"])
            expected = {key: value for key, value in zip(("home_win", "draw", "away_win"),
                (np.tril(np.asarray(untouched["score_matrix"]), -1).sum(),
                 np.trace(np.asarray(untouched["score_matrix"])),
                 np.triu(np.asarray(untouched["score_matrix"]), 1).sum()))}
            self.assertEqual(forecast["probabilities"], expected)
            score_forecast(forecast, {"home_goals": 0, "away_goals": 2})

    def test_existing_scoring_probability_range_and_consistency_remain_strict(self):
        case = real_case()
        prediction = predict_saved_goalmodel(case["card"], case["fixture"],
                                             case["features"], case["as_of"])
        for value in (math.nextafter(1.0, math.inf), -1e-30, float("nan"), 0.99):
            corrupt = copy.deepcopy(prediction)
            corrupt["probabilities"]["away_win"] = value
            with self.subTest(value=value), self.assertRaises(ModelDataError):
                score_forecast(corrupt, case["observed"])

    def test_existing_saved_fit_grid_and_temporal_gates_remain_strict(self):
        case = real_case()
        card = copy.deepcopy(case["card"])
        card["goalmodel_fit"]["parameters"]["beta"]["__home_field"] = 10
        with self.assertRaisesRegex(ModelDataError, "unbounded saved goalmodel matrix"):
            predict_saved_goalmodel(card, case["fixture"], case["features"], case["as_of"])
        with self.assertRaisesRegex(ModelDataError, "temporal mismatch"):
            predict_saved_goalmodel(case["card"], case["fixture"], case["features"],
                                   case["fixture"]["kickoff"])


if __name__ == "__main__":
    unittest.main()
