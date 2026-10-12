"""Protocol tests plus separately labelled optional real R integration tests."""

import copy
from datetime import datetime, timedelta, timezone
import json
import math
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

from football_model.goalmodel_adapter import (
    GOALMODEL_SOURCE_COMMIT, GOALMODEL_UPSTREAM_VERSION,
    _prepare_sources, _validate_output, fit_predict_goalmodel)
from football_model.model import ModelDataError, _distribution_from_rates


ROOT = Path(__file__).resolve().parents[1]


def sample_matches():
    rng = np.random.default_rng(1207)
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    result = []
    for i in range(120):
        home = i % 4 + 1
        away = (home + (i // 4) % 3 - 1) % 4 + 1
        if home == away:
            away = home % 4 + 1
        result.append({"match_id": i + 1, "kickoff": (start + timedelta(days=i)).isoformat(),
                       "home_id": home, "away_id": away,
                       "home_name": f"Team {home}", "away_name": f"Team {away}",
                       "home_goals": int(rng.poisson(1.6)), "away_goals": int(rng.poisson(1.1)),
                       "neutral": i % 11 == 0})
    return result


def sample_fixtures():
    return [{"match_id": 999, "home_id": 1, "away_id": 2,
             "kickoff": "2024-06-01T00:00:00Z", "neutral": False}]


def output_for(fixtures, dc=False, rho=0):
    distribution = _distribution_from_rates(1.4, 0.9)
    matrix = np.array(distribution["score_matrix"])
    if dc:
        matrix[:2, :2] *= [[1 - 1.4 * 0.9 * rho, 1 + 1.4 * rho],
                          [1 + 0.9 * rho, 1 - rho]]
        matrix /= matrix.sum()
    probabilities = {"home": float(np.tril(matrix, -1).sum()),
                     "draw": float(np.trace(matrix)), "away": float(np.triu(matrix, 1).sum())}
    forecasts = [dict(fixture, expected_home_goals=1.4, expected_away_goals=0.9, rho=rho,
                      score_matrix=matrix.tolist(), probabilities_1x2=probabilities,
                      score_matrix_axes=distribution["score_matrix_axes"],
                      tail_mass_before_normalization=distribution["tail_mass_before_normalization"])
                 for fixture in fixtures]
    return {"schema_version": 1, "source_commit": GOALMODEL_SOURCE_COMMIT,
            "upstream_version": GOALMODEL_UPSTREAM_VERSION,
            "source_mode": "pinned_official_R_sources", "dc": dc, "status": "ok",
            "fit": {"converged": True}, "forecasts": forecasts, "errors": []}


class GoalmodelProtocolTests(unittest.TestCase):
    def test_unavailable_r_is_failure_and_never_baseline_forecast(self):
        with patch("football_model.goalmodel_adapter.shutil.which", return_value=None):
            output = fit_predict_goalmodel(sample_matches(), sample_fixtures(),
                                           cutoff="2024-05-01T00:00:00Z")
        self.assertEqual(output["status"], "failed")
        self.assertEqual(output["forecasts"], [])
        self.assertEqual(output["errors"][0]["code"], "rscript_unavailable")
        self.assertFalse(output["predictive_gain_verified"])

    def test_temporal_filter_weights_and_feature_order_protocol(self):
        matches = sample_matches()
        recent = dict(matches[-1], match_id=500, kickoff="2024-05-01T01:00:00Z", home_goals=70)
        matches.append(recent)
        features = {m["match_id"]: {"home": {"rest": 3, "load": 2},
                                     "away": {"load": 1, "rest": 4}} for m in matches}
        prediction = {999: {"home": {"rest": 6, "load": 1}, "away": {"rest": 4, "load": 2}}}
        captured = {}

        def fake_r(command, **kwargs):
            request = json.loads(Path(command[-2]).read_text())
            captured.update(request)
            Path(command[-1]).write_text(json.dumps(output_for(request["fixtures"])))
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

        with patch("football_model.goalmodel_adapter.shutil.which", return_value="Rscript"), \
                patch("football_model.goalmodel_adapter.subprocess.run", side_effect=fake_r):
            output = fit_predict_goalmodel(matches, sample_fixtures(), cutoff="2024-05-01T02:00:00Z",
                                           feature_names=["rest", "load"], training_features=features,
                                           prediction_features=prediction)
        self.assertEqual(output["status"], "ok")
        self.assertEqual(output["branch"], "G1")
        self.assertEqual(output["excluded_training_count"], 1)
        self.assertNotIn(500, output["training_match_ids"])
        self.assertEqual(captured["feature_names"], ["rest", "load"])
        age = (datetime.fromisoformat("2024-05-01T02:00:00+00:00") -
               datetime.fromisoformat(matches[0]["kickoff"])).total_seconds()/86400
        self.assertAlmostEqual(captured["training_weights"][0], math.exp(-math.log(2)*age/180))
        self.assertIn("expected_goals", output["forecasts"][0])
        # This only verifies the JSON/process adapter, not real R estimation.

    def test_asymmetric_or_missing_features_rejected_before_r(self):
        features = {m["match_id"]: {"home": {"rest": 3}, "away": {"rest": 4}}
                    for m in sample_matches()}
        prediction = {999: {"home": {"rest": 6}, "away": {"wrong": 4}}}
        with self.assertRaises(ModelDataError), patch("football_model.goalmodel_adapter.subprocess.run") as run:
            fit_predict_goalmodel(sample_matches(), sample_fixtures(), cutoff="2024-05-01T00:00:00Z",
                                  feature_names=["rest"], training_features=features,
                                  prediction_features=prediction)
        run.assert_not_called()

    def test_reserved_features_nonfinite_and_duplicate_id_rejected(self):
        with self.assertRaises(ModelDataError):
            fit_predict_goalmodel(sample_matches(), sample_fixtures(), cutoff="2024-05-01T00:00:00Z",
                                  feature_names=["__home_field"])
        matches = sample_matches()
        matches.append(matches[0])
        with self.assertRaises(ModelDataError):
            fit_predict_goalmodel(matches, sample_fixtures(), cutoff="2024-05-01T00:00:00Z")
        features = {m["match_id"]: {"home": {"rest": 3}, "away": {"rest": 4}}
                    for m in sample_matches()}
        prediction = {999: {"home": {"rest": float("nan")}, "away": {"rest": 4}}}
        with self.assertRaises(ModelDataError):
            fit_predict_goalmodel(sample_matches(), sample_fixtures(), cutoff="2024-05-01T00:00:00Z",
                                  feature_names=["rest"], training_features=features,
                                  prediction_features=prediction)

    def test_r_timeout_and_nonzero_exit_retained(self):
        for fault, code in [(subprocess.TimeoutExpired("Rscript", 180), "r_timeout"),
                            (subprocess.CompletedProcess([], 1, "", "fit did not converge"), "r_execution_failed")]:
            with self.subTest(code=code):
                kwargs = {"side_effect": fault} if isinstance(fault, Exception) else {"return_value": fault}
                with patch("football_model.goalmodel_adapter.shutil.which", return_value="Rscript"), \
                        patch("football_model.goalmodel_adapter.subprocess.run", **kwargs):
                    output = fit_predict_goalmodel(sample_matches(), sample_fixtures(),
                                                   cutoff="2024-05-01T00:00:00Z")
                self.assertEqual(output["errors"][0]["code"], code)
                self.assertEqual(output["forecasts"], [])

    def test_complete_matrix_verified_including_dc(self):
        for dc, rho in [(False, 0), (True, -0.1)]:
            output = output_for(sample_fixtures(), dc, rho)
            self.assertIs(_validate_output(output, sample_fixtures(), dc), output)
            tampered = copy.deepcopy(output)
            tampered["forecasts"][0]["score_matrix"][0][0] += 0.001
            tampered["forecasts"][0]["score_matrix"][0][1] -= 0.001
            with self.assertRaises(ModelDataError):
                _validate_output(tampered, sample_fixtures(), dc)
            tampered = copy.deepcopy(output)
            tampered["forecasts"][0]["probabilities_1x2"]["home"] += 0.01
            with self.assertRaises(ModelDataError):
                _validate_output(tampered, sample_fixtures(), dc)

    def test_native_r_roundoff_preserved_but_canonical_probabilities_match_matrix(self):
        from football_model.feature_research import score_forecast

        for dc, rho in ((False, 0), (True, -0.1)):
            with self.subTest(dc=dc):
                native = output_for(sample_fixtures(), dc, rho)
                native["forecasts"][0]["probabilities_1x2"]["home"] += 5e-12
                native["forecasts"][0]["probabilities_1x2"]["draw"] -= 5e-12
                original = copy.deepcopy(native["forecasts"][0])

                def fake_r(command, **kwargs):
                    Path(command[-1]).write_text(json.dumps(native))
                    return subprocess.CompletedProcess(command, 0, "", "")

                with patch("football_model.goalmodel_adapter.shutil.which", return_value="Rscript"), \
                        patch("football_model.goalmodel_adapter.subprocess.run", side_effect=fake_r):
                    output = fit_predict_goalmodel(sample_matches(), sample_fixtures(),
                                                   cutoff="2024-05-01T00:00:00Z", dc=dc)
                self.assertEqual(output["status"], "ok", output)
                forecast = output["forecasts"][0]
                self.assertEqual(forecast["probabilities_1x2"], original["probabilities_1x2"])
                self.assertEqual(forecast["score_matrix"], original["score_matrix"])
                self.assertEqual(forecast["expected_home_goals"], original["expected_home_goals"])
                self.assertEqual(forecast["expected_away_goals"], original["expected_away_goals"])
                self.assertEqual(forecast["rho"], original["rho"])
                self.assertGreater(forecast["upstream_probability_roundoff_max"], 1e-15)
                self.assertLess(forecast["upstream_probability_roundoff_max"], 1e-10)
                self.assertEqual(output["upstream_probability_roundoff_max"],
                                 forecast["upstream_probability_roundoff_max"])
                self.assertIn("unchanged validated score_matrix", forecast["probability_derivation"])
                score = score_forecast(forecast, {"home_goals": 1, "away_goals": 0})
                self.assertTrue(math.isfinite(score["scoreline_nll"]))

    def test_native_r_large_disagreement_or_nonfinite_probability_still_rejected(self):
        for native_home in (float("nan"), 0.99):
            native = output_for(sample_fixtures())
            native["forecasts"][0]["probabilities_1x2"]["home"] = native_home
            with self.assertRaises(ModelDataError):
                _validate_output(native, sample_fixtures(), False)

    def test_missing_identity_source_or_convergence_not_success(self):
        for mutate in [lambda o: o.update(source_commit="f"*40),
                       lambda o: o["fit"].update(converged=False),
                       lambda o: o.update(forecasts=[]),
                       lambda o: o["forecasts"][0].update(home_id=7)]:
            output = output_for(sample_fixtures())
            mutate(output)
            with self.assertRaises(ModelDataError):
                _validate_output(output, sample_fixtures(), False)

    def test_vendored_exact_sources_verified_and_tampering_rejected(self):
        actual = _prepare_sources(ROOT)
        self.assertEqual(set(actual), {"goalmodel_fit.R", "goalmodel_predict.R"})
        with tempfile.TemporaryDirectory() as temporary:
            p = Path(temporary)/"third_party"/"goalmodel"/"R"
            p.mkdir(parents=True)
            (p/"goalmodel_fit.R").write_text("malicious replacement")
            with self.assertRaises(ModelDataError):
                _prepare_sources(temporary)


@unittest.skipUnless(shutil.which("Rscript"), "real Rscript unavailable; protocol tests are not R validation")
class GoalmodelRealRTests(unittest.TestCase):
    def test_upstream_g0_real_fit_complete_matrix_and_neutral_effect(self):
        fixtures = sample_fixtures()
        fixtures.append(dict(fixtures[0], match_id=1000, neutral=True))
        output = fit_predict_goalmodel(sample_matches(), fixtures, cutoff="2024-05-01T00:00:00Z")
        self.assertEqual(output["status"], "ok", output)
        self.assertEqual(len(output["forecasts"]), 2)
        self.assertTrue(output["fit"]["converged"])
        self.assertNotAlmostEqual(output["forecasts"][0]["expected_home_goals"],
                                  output["forecasts"][1]["expected_home_goals"])

    def test_upstream_g1_real_fit_and_unsupported_team(self):
        matches = sample_matches()
        rng = np.random.default_rng(904)
        features = {m["match_id"]: {"home": {"rest": float(rng.normal())},
                                     "away": {"rest": float(rng.normal())}} for m in matches}
        fixtures = sample_fixtures() + [dict(sample_fixtures()[0], match_id=1001, away_id=9999)]
        prediction = {f["match_id"]: {"home": {"rest": 0.5}, "away": {"rest": -0.5}} for f in fixtures}
        output = fit_predict_goalmodel(matches, fixtures, cutoff="2024-05-01T00:00:00Z",
                                       feature_names=["rest"], training_features=features,
                                       prediction_features=prediction)
        self.assertEqual(output["status"], "ok", output)
        self.assertEqual(len(output["forecasts"]), 1)
        self.assertEqual(output["errors"][0]["code"], "unsupported_team")
        self.assertIn("rest", output["fit"]["parameters"]["beta"])

    def test_upstream_dc_real_fit_preserves_adjusted_matrix(self):
        output = fit_predict_goalmodel(sample_matches(), sample_fixtures(),
                                       cutoff="2024-05-01T00:00:00Z", dc=True)
        self.assertEqual(output["status"], "ok", output)
        forecast = output["forecasts"][0]
        self.assertNotEqual(forecast["rho"], 0)
        independent = _distribution_from_rates(forecast["expected_home_goals"],
                                                forecast["expected_away_goals"])
        self.assertNotAlmostEqual(forecast["score_matrix"][0][0], independent["score_matrix"][0][0])


if __name__ == "__main__":
    unittest.main()
