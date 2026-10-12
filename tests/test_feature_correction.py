"""Synthetic contracts check engineering, not real predictive effectiveness."""

import copy
import json
import math
import unittest
from datetime import datetime, timedelta, timezone

import numpy as np

from football_model.feature_correction import (
    CORRECTION_VERSION, apply_correction, fit_correction, predict_correction,
)
from football_model.model import ModelDataError, _distribution_from_rates


def synthetic_rows(n_matches=160, *, constant=False):
    rng = np.random.default_rng(81723)
    start = datetime(2023, 1, 1, 18, tzinfo=timezone.utc)
    rows = []
    for index in range(n_matches):
        kickoff = start + timedelta(days=index)
        cutoff = kickoff - timedelta(hours=10)
        for side in ("home", "away"):
            signal = 0.0 if constant else float(rng.normal())
            baseline = 1.4 if side == "home" else 1.1
            rows.append({
                "match_id": index + 1, "side": side, "kickoff": kickoff.isoformat(),
                "cutoff_at": cutoff.isoformat(),
                "baseline_training_cutoff": (cutoff - timedelta(days=1)).isoformat(),
                "target_available_at": (kickoff + timedelta(hours=4)).isoformat(),
                "lambda0": baseline, "goals": int(rng.poisson(baseline * math.exp(0.4 * signal))),
                "features": {"signal": signal}, "feature_available_at": {"signal": cutoff.isoformat()},
            })
    return rows


def baseline_prediction():
    result = _distribution_from_rates(1.731916720411301, 0.82641991763383)
    result.update({"match_id": 9001, "home_id": 1, "away_id": 2,
                   "kickoff": "2023-07-01T18:00:00Z", "cutoff_at": "2023-07-01T08:00:00Z",
                   "training_cutoff": "2023-06-30T08:00:00Z", "lineup_adjusted": False,
                   "model_version": "independent_poisson_time_decay_l2_v1"})
    return result


class FeatureCorrectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rows = synthetic_rows()
        cls.cutoff = "2023-06-15T00:00:00Z"
        cls.model = fit_correction(cls.rows, ["signal"], alpha=0.1, cutoff=cls.cutoff)

    def test_shared_offset_fit_recovers_direction_and_serializes(self):
        model = self.model
        self.assertGreater(model["coefficients"]["signal"], 0.15)
        self.assertEqual(model["model_version"], CORRECTION_VERSION)
        self.assertEqual(model["n_rows"], 320)
        self.assertEqual(model["n_matches"], 160)
        self.assertTrue(model["shared_home_away_coefficients"])
        self.assertFalse(model["intercept_added"])
        self.assertFalse(model["refit"])
        self.assertEqual(model["L1_wt"], 0)
        self.assertFalse(model["validated_for_production"])
        self.assertFalse(model["automatic_promotion"])
        self.assertEqual(model["time_weighting"]["normalization"], "mean_one")
        self.assertAlmostEqual(model["time_weighting"]["normalized_weight_sum"], 320)
        self.assertLessEqual(model["optimizer"]["gradient_norm"], model["optimizer"]["gradient_norm_tolerance"])
        json.dumps(model, allow_nan=False)
        self.assertEqual(model, fit_correction(list(reversed(self.rows)), ["signal"], alpha=0.1, cutoff=self.cutoff))

    def test_standardization_uses_only_weighted_training_rows_and_is_saved(self):
        fitted = self.model["feature_scaling"]["signal"]
        cutoff = datetime.fromisoformat(self.cutoff.replace("Z", "+00:00"))
        values = np.array([row["features"]["signal"] for row in self.rows])
        ages = np.array([(cutoff - datetime.fromisoformat(row["kickoff"])).total_seconds() / 86400
                         for row in self.rows])
        weights = np.exp(-math.log(2) * ages / 180)
        mean = float(np.average(values, weights=weights))
        scale = math.sqrt(float(np.average((values - mean) ** 2, weights=weights)))
        self.assertAlmostEqual(fitted["mean"], mean)
        self.assertAlmostEqual(fitted["scale"], scale)
        output = apply_correction(json.loads(json.dumps(self.model)), 1.7, {"signal": 2.0})
        contribution = (2.0 - mean) / scale * self.model["coefficients"]["signal"]
        self.assertAlmostEqual(output["log_contributions"]["signal"], contribution)
        self.assertAlmostEqual(output["total_log_correction"], contribution)
        self.assertAlmostEqual(output["lambda"], 1.7 * math.exp(contribution))
        self.assertEqual(self.model["feature_scaling"]["signal"], fitted)

    def test_zero_variation_is_exact_zero_correction_and_original_grid(self):
        model = fit_correction(synthetic_rows(constant=True), ["signal"], alpha=0.1, cutoff=self.cutoff)
        self.assertEqual(model["coefficients"]["signal"], 0.0)
        self.assertTrue(model["feature_scaling"]["signal"]["constant_in_training"])
        rate = 1.731916720411301
        applied = apply_correction(model, rate, {"signal": 0.0})
        self.assertEqual(applied["lambda"], rate)
        # A training-constant covariate has no learnable slope. Novel variation
        # must not invent an effect or divide by a zero standard deviation.
        novel = apply_correction(model, rate, {"signal": 5.0})
        self.assertEqual(novel["lambda"], rate)
        self.assertEqual(novel["log_contributions"]["signal"], 0.0)
        self.assertEqual(novel["standardized_features"]["signal"], 5.0)
        baseline = baseline_prediction()
        result = predict_correction(model, baseline, {"signal": 0.0}, {"signal": 0.0})
        for name in ("expected_goals", "score_matrix", "probabilities", "score_matrix_axes",
                     "tail_mass_before_normalization", "tail_tolerance", "matrix_renormalized"):
            self.assertEqual(result[name], baseline[name])
        self.assertEqual(result["baseline_prediction"], baseline)
        result["baseline_prediction"]["match_id"] = 77
        self.assertEqual(baseline["match_id"], 9001)

    def test_candidate_keeps_full_original_and_uses_existing_distribution(self):
        baseline = baseline_prediction()
        result = predict_correction(self.model, baseline, {"signal": 1.0}, {"signal": -1.0})
        self.assertEqual(result["baseline_prediction"], baseline)
        rates = result["expected_goals"]
        self.assertEqual(result["score_matrix"], _distribution_from_rates(rates["home"], rates["away"])["score_matrix"])
        self.assertGreater(rates["home"], baseline["expected_goals"]["home"])
        self.assertLess(rates["away"], baseline["expected_goals"]["away"])
        self.assertEqual(result["purpose"], "candidate_research_only")
        self.assertFalse(result["lineup_adjusted"])
        self.assertFalse(result["validated_for_production"])
        json.dumps(result, allow_nan=False)

    def test_training_leakage_and_not_yet_available_targets_fail(self):
        cases = (
            ("baseline_training_cutoff", "2023-01-01T09:00:00Z", "offset leakage"),
            ("cutoff_at", "2023-01-01T18:00:00Z", "before kickoff"),
            ("target_available_at", "2023-07-01T00:00:00Z", "not available"),
            ("target_available_at", "2023-01-01T18:00:00Z", "at or before kickoff"),
            ("target_available_at", "2023-01-01T19:00:00Z", "four-hour result delay"),
        )
        for field, value, message in cases:
            with self.subTest(field=field, value=value):
                rows = copy.deepcopy(self.rows)
                rows[0][field] = value
                with self.assertRaisesRegex(ModelDataError, message):
                    fit_correction(rows, ["signal"], alpha=0.1, cutoff=self.cutoff)
        rows = copy.deepcopy(self.rows)
        rows[0]["feature_available_at"]["signal"] = "2023-01-01T09:00:00Z"
        with self.assertRaisesRegex(ModelDataError, "not available at prediction cutoff"):
            fit_correction(rows, ["signal"], alpha=0.1, cutoff=self.cutoff)
        rows = copy.deepcopy(self.rows)
        rows[0]["baseline_training_match_ids"] = [rows[0]["match_id"]]
        with self.assertRaisesRegex(ModelDataError, "contain target"):
            fit_correction(rows, ["signal"], alpha=0.1, cutoff=self.cutoff)

    def test_paired_match_denominator_duplicates_and_times_are_checked(self):
        for rows, message in ((self.rows[1:], "exactly one home"),
                              (self.rows + [self.rows[0]], "duplicate match side")):
            with self.subTest(message=message), self.assertRaisesRegex(ModelDataError, message):
                fit_correction(rows, ["signal"], alpha=0.1, cutoff=self.cutoff)
        rows = copy.deepcopy(self.rows)
        rows[1]["baseline_training_cutoff"] = "2022-12-30T08:00:00Z"
        with self.assertRaisesRegex(ModelDataError, "paired sides disagree"):
            fit_correction(rows, ["signal"], alpha=0.1, cutoff=self.cutoff)

    def test_finite_numbers_exact_features_targets_and_timezone_are_required(self):
        for field, value in (("lambda0", 0), ("lambda0", float("inf")), ("lambda0", True),
                             ("goals", -1), ("goals", 1.5), ("goals", False),
                             ("cutoff_at", "2023-01-01T08:00:00")):
            with self.subTest(field=field, value=value):
                rows = copy.deepcopy(self.rows)
                rows[0][field] = value
                with self.assertRaises(ModelDataError):
                    fit_correction(rows, ["signal"], alpha=0.1, cutoff=self.cutoff)
        for value in (None, True, "0", float("nan"), float("inf")):
            with self.subTest(feature=value):
                rows = copy.deepcopy(self.rows)
                rows[0]["features"]["signal"] = value
                with self.assertRaises(ModelDataError):
                    fit_correction(rows, ["signal"], alpha=0.1, cutoff=self.cutoff)
        for changed in ({"features": {"signal": 1, "extra": 0}}, {"features": {}},
                        {"feature_available_at": {}}, {"feature_available_at": None}):
            rows = copy.deepcopy(self.rows)
            rows[0].update(changed)
            with self.assertRaises(ModelDataError):
                fit_correction(rows, ["signal"], alpha=0.1, cutoff=self.cutoff)

    def test_positive_regularization_sample_gate_and_no_intercept(self):
        for alpha in (0, -1, float("nan"), True):
            with self.subTest(alpha=alpha), self.assertRaises(ModelDataError):
                fit_correction(self.rows, ["signal"], alpha=alpha, cutoff=self.cutoff)
        with self.assertRaisesRegex(ModelDataError, "insufficient"):
            fit_correction(self.rows[:4], ["signal"], alpha=0.1, cutoff=self.cutoff)
        for columns in (["signal", "signal"], ["intercept"], []):
            with self.assertRaises(ModelDataError):
                fit_correction(self.rows, columns, alpha=0.1, cutoff=self.cutoff)
        with self.assertRaises(ModelDataError):
            fit_correction(self.rows, ["signal"], alpha=0.1, cutoff=self.cutoff, half_life_days=0)

    def test_prediction_cannot_use_future_fit_or_corrupt_baseline(self):
        for changed in ({"cutoff_at": "2023-06-01T08:00:00Z"},
                        {"cutoff_at": "2023-07-01T18:00:00Z"},
                        {"training_cutoff": "2023-07-01T09:00:00Z"}):
            baseline = baseline_prediction()
            baseline.update(changed)
            with self.assertRaisesRegex(ModelDataError, "future training|non-prematch"):
                predict_correction(self.model, baseline, {"signal": 1.0}, {"signal": 1.0})
        baseline = baseline_prediction()
        baseline["score_matrix"][0][0] += 0.01
        with self.assertRaisesRegex(ModelDataError, "unchanged independent Poisson"):
            predict_correction(self.model, baseline, {"signal": 1.0}, {"signal": 1.0})
        baseline = baseline_prediction()
        baseline["probabilities"]["draw"] += 0.01
        with self.assertRaisesRegex(ModelDataError, "disagree"):
            predict_correction(self.model, baseline, {"signal": 1.0}, {"signal": 1.0})
        baseline = baseline_prediction()
        baseline["match_id"] = 1
        with self.assertRaisesRegex(ModelDataError, "used to train the correction"):
            predict_correction(self.model, baseline, {"signal": 1.0}, {"signal": 1.0})

    def test_saved_model_tampering_and_exploding_prediction_fail(self):
        model = copy.deepcopy(self.model)
        model["coefficients"]["signal"] += 1
        with self.assertRaisesRegex(ModelDataError, "fingerprint"):
            apply_correction(model, 1.4, {"signal": 0})
        with self.assertRaisesRegex(ModelDataError, "corrected expected goals"):
            apply_correction(self.model, 1.4, {"signal": 1e6})
        for values in ({}, {"signal": None}, {"signal": 1, "extra": 0}):
            with self.assertRaises(ModelDataError):
                apply_correction(self.model, 1.4, values)


if __name__ == "__main__":
    unittest.main()
