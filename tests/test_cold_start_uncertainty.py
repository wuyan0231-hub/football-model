"""Numerical engineering checks; no observed match outcomes or calibration."""
import copy
import importlib.util
import hashlib
import json
import math
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
from numpy.polynomial.hermite import hermgauss
from scipy.stats import poisson

from football_model.cold_start import score_cold_start
from football_model.model import MODEL_VERSION

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("cold_uncertainty", ROOT / "scripts/cold_start_uncertainty.py")
CALC = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CALC)


class ColdStartUncertaintyTests(unittest.TestCase):
    def setUp(self):
        self.model = dict(model_version=MODEL_VERSION, fit_status="converged", graph_connected=True,
            training_cutoff="2026-01-01T00:00:00Z", training_data_sha256="a" * 64,
            intercept=0.1, home_advantage=0.2,
            team_parameters={"1": dict(name="Known", attack=.3, defence=.2, training_matches=50)},
            warnings=[], low_sample_team_threshold=5)
        self.fixture = dict(match_id=8, home_id=1, away_id=2, home_name="Known", away_name="New",
            kickoff="2026-01-02T00:00:00Z", neutral=False)
        self.params = dict(tau=1.0, attack_variance=.04, defence_variance=.09,
            attack_defence_correlation=0.0, cross_team_correlation=0.0, known_parameter_uncertainty=False)

    def test_tau_zero_exactly_returns_existing_point_prediction(self):
        result = CALC.predict_uncertainty(self.model, self.fixture, {**self.params, "tau": 0})
        self.assertEqual(result["prediction"], score_cold_start(self.model, self.fixture))
        self.assertTrue(result["exact_point_prior_branch"])

    def test_single_unknown_variance_assignment_and_known_parameters_preserved(self):
        before = copy.deepcopy(self.model)
        result = CALC.predict_uncertainty(self.model, self.fixture, self.params)
        point = score_cold_start(self.model, self.fixture)
        self.assertEqual(self.model, before)
        self.assertEqual(result["prediction"]["expected_goals"], point["expected_goals"])
        self.assertAlmostEqual(result["diagnostics"]["home_marginal"]["log_rate_variance"], .09)
        self.assertAlmostEqual(result["diagnostics"]["away_marginal"]["log_rate_variance"], .04)
        self.assertEqual(result["prediction"]["parent_model_sha256"], point["parent_model_sha256"])

    def test_double_unknown_adds_disjoint_attack_and_opponent_defence_variances(self):
        fixture = {**self.fixture, "home_id": 2, "away_id": 3, "home_name": "NewA", "away_name": "NewB"}
        result = CALC.predict_uncertainty(self.model, fixture, self.params)
        self.assertEqual(result["prediction"]["unknown_team_ids"], [2, 3])
        for side in ("home", "away"):
            self.assertAlmostEqual(result["diagnostics"][side + "_marginal"]["log_rate_variance"], .13)

    def test_analytic_mean_preserved_and_matrix_mean_difference_disclosed(self):
        result = CALC.predict_uncertainty(self.model, self.fixture, self.params)
        for side in ("home", "away"):
            marginal = result["diagnostics"][side + "_marginal"]
            target = result["prediction"]["expected_goals"][side]
            self.assertAlmostEqual(marginal["orders"][-1]["quadrature_mean_rate"], target, places=10)
            self.assertLess(abs(result["diagnostics"]["normalized_matrix_mean_rates"][side] - target), 1e-7)

    def test_mixture_is_not_poisson_at_the_mean_lambda(self):
        result = CALC.predict_uncertainty(self.model, self.fixture, self.params)
        home = result["prediction"]["expected_goals"]["home"]
        probability_zero = result["diagnostics"]["raw_home_pmf"][0]
        self.assertGreater(probability_zero, math.exp(-home) + 1e-4)

    def test_actual_tail_bound_and_full_normal_nodes_checked_before_normalizing(self):
        result = CALC.predict_uncertainty(self.model, self.fixture, self.params)
        diag = result["diagnostics"]
        self.assertLessEqual(diag["actual_joint_grid_tail_upper_bound"], CALC.DEFAULT_SETTINGS["tail_tolerance"] / 2 + 1e-14)
        self.assertAlmostEqual(sum(map(sum, result["prediction"]["score_matrix"])), 1.0, places=12)
        for side in ("home", "away"):
            m = diag[side + "_marginal"]
            self.assertFalse(m["prior_or_node_rates_clipped"])
            self.assertGreater(m["orders"][-1]["largest_node_rate"], m["rate_cap_for_bound_only"])
            self.assertGreaterEqual(len(m["orders"]), 3)

    def test_diagonal_outer_product_matches_independent_four_variable_integration(self):
        fixture = {**self.fixture, "home_id": 2, "away_id": 3, "home_name": "NewA", "away_name": "NewB"}
        params = {**self.params, "attack_variance": .02, "defence_variance": .03}
        result = CALC.predict_uncertainty(self.model, fixture, params)
        matrix = np.asarray(result["prediction"]["score_matrix"])
        # Each side below integrates its own two latent variables. Their joint
        # tensor product is the independent four-variable integral, using a
        # different quadrature representation from the collapsed 1D algorithm.
        nodes, weights = hermgauss(12)
        z = math.sqrt(2) * nodes
        weights = weights / math.sqrt(math.pi)
        pairweights = np.outer(weights, weights).ravel()
        multiplier = np.exp(-(.02 + .03) / 2 + math.sqrt(.02) * z[:, None] - math.sqrt(.03) * z[None, :]).ravel()
        point = score_cold_start(self.model, fixture)
        hp = poisson.pmf(np.arange(matrix.shape[0])[None, :], point["expected_goals"]["home"] * multiplier[:, None])
        ap = poisson.pmf(np.arange(matrix.shape[1])[None, :], point["expected_goals"]["away"] * multiplier[:, None])
        reference = np.einsum("i,j,ik,jl->kl", pairweights, pairweights, hp, ap, optimize=True)
        reference /= reference.sum()
        self.assertLess(float(np.abs(reference - matrix).sum()), 3e-10)

    def test_nonzero_correlations_and_known_parameter_noise_rejected(self):
        for changes in ({"attack_defence_correlation": .1}, {"cross_team_correlation": -.1}, {"known_parameter_uncertainty": True}):
            with self.assertRaises(CALC.UncertaintyError):
                CALC.predict_uncertainty(self.model, self.fixture, {**self.params, **changes})

    def test_illegal_parameters_rejected(self):
        for changes in ({"tau": True}, {"tau": -1}, {"attack_variance": float("nan")}, {"defence_variance": -0.1}):
            with self.assertRaises(CALC.UncertaintyError):
                CALC.predict_uncertainty(self.model, self.fixture, {**self.params, **changes})
        with self.assertRaisesRegex(CALC.UncertaintyError, "scaling overflow"):
            CALC.predict_uncertainty(self.model, self.fixture, {**self.params, "tau": 1e308})

    def test_mixture_metadata_describes_distribution_and_actual_tolerance(self):
        settings = {**CALC.DEFAULT_SETTINGS, "tail_tolerance": 1e-7}
        result = CALC.predict_uncertainty(self.model, self.fixture, self.params, settings)
        prediction = result["prediction"]
        self.assertNotEqual(prediction["unknown_team_coefficients"], "zero_league_centred_prior")
        self.assertEqual(prediction["tail_tolerance"], settings["tail_tolerance"])
        self.assertAlmostEqual(prediction["unknown_team_coefficient_prior"]["attack_mean"], -.04 / 2)
        self.assertAlmostEqual(prediction["unknown_team_coefficient_prior"]["defence_mean"], .09 / 2)

    def test_cli_parent_hash_mismatch_is_partial_and_nonzero_exit(self):
        from football_model.advanced_research import implementation_fingerprint
        with tempfile.TemporaryDirectory(dir=ROOT) as temporary:
            root = Path(temporary)
            source = {"report_implementation_fingerprint": implementation_fingerprint(),
                "records": [{"horizon": "EARLY", "parent_fit_key": "parent", "fixture": self.fixture,
                    "saved_prediction": {"parent_model_sha256": "wrong-parent"}}],
                "parent_fits": {"parent": {"model": self.model}}}
            raw = json.dumps(source).encode()
            (root / "input.json").write_bytes(raw)
            document = {"source_input_sha256": hashlib.sha256(raw).hexdigest(), "horizon": "EARLY",
                "engineering_demo_tau_values": [0],
                "prior_parameters": {k: v for k, v in self.params.items() if k != "tau"},
                "numerical_settings": CALC.DEFAULT_SETTINGS}
            (root / "parameters.json").write_text(json.dumps(document))
            process = subprocess.run([sys.executable, str(ROOT / "scripts/cold_start_uncertainty.py"),
                "--inputs", str(root / "input.json"), "--parameters", str(root / "parameters.json"),
                "--output", str(root / "output")], capture_output=True, text=True)
            self.assertEqual(process.returncode, 2, process.stderr)
            receipt = json.loads(process.stdout)["receipt"]
            self.assertEqual(receipt["execution_status"], "partial")
            self.assertEqual(receipt["successful_records"], 0)
            self.assertIn("parent model differs", receipt["failures"][0]["reason"])

    def test_tiny_negative_tail_roundoff_is_not_reported_as_probability(self):
        pmf = np.array([.5, .5 + 2e-16])
        diagnostics = {"actual_distribution_grid_tail_upper_bound": 1e-10,
            "quadrature_l1_last_difference": 0.0}
        with patch.object(CALC, "marginal", return_value=(pmf, diagnostics)):
            result = CALC.predict_uncertainty(self.model, self.fixture, self.params)
        self.assertEqual(result["prediction"]["tail_mass_before_normalization"], 0)
        self.assertLess(result["diagnostics"]["signed_numerical_missing_mass"], 0)
        self.assertTrue(result["prediction"]["negative_tail_roundoff_clamped"])

    def test_cli_self_signed_manifest_with_unapproved_path_is_rejected(self):
        from football_model.advanced_research import implementation_fingerprint
        with tempfile.TemporaryDirectory(dir=ROOT) as temporary:
            root = Path(temporary)
            manifest = implementation_fingerprint()
            manifest["files"].pop(next(iter(manifest["files"])))
            manifest["files"]["../unapproved-path"] = "a" * 64
            manifest["sha256"] = CALC.digest(manifest["files"])
            raw = json.dumps({"report_implementation_fingerprint": manifest}).encode()
            (root / "input.json").write_bytes(raw)
            (root / "parameters.json").write_text(json.dumps({"source_input_sha256": hashlib.sha256(raw).hexdigest()}))
            process = subprocess.run([sys.executable, str(ROOT / "scripts/cold_start_uncertainty.py"),
                "--inputs", str(root / "input.json"), "--parameters", str(root / "parameters.json"),
                "--output", str(root / "output")], capture_output=True, text=True)
            self.assertNotEqual(process.returncode, 0)
            self.assertIn("current approved50-file", process.stderr)

    def test_overwide_or_insufficient_grid_explicitly_fails(self):
        with self.assertRaisesRegex(CALC.UncertaintyError, "variance outside"):
            CALC.predict_uncertainty(self.model, self.fixture, {**self.params, "tau": 10})
        with self.assertRaisesRegex(CALC.UncertaintyError, "exceeds max_goal"):
            CALC.predict_uncertainty(self.model, self.fixture, self.params, {**CALC.DEFAULT_SETTINGS, "max_goal": 3})

    def test_unconverged_quadrature_does_not_emit_normalized_prediction(self):
        with self.assertRaisesRegex(CALC.UncertaintyError, "did not converge"):
            CALC.predict_uncertainty(self.model, self.fixture, {**self.params, "attack_variance": .3, "defence_variance": .3},
                {**CALC.DEFAULT_SETTINGS, "node_orders": [4, 6, 8]})


if __name__ == "__main__":
    unittest.main()
