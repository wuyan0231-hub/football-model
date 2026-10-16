"""Synthetic leakage/calibration tests; fixtures are not real experiment protocols."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

from scripts.personnel_label_inventory import inventory
from scripts.restricted_start_development import (prepare, execute, fit_preprocessor, design,
    fit_logistic, predict, fit_calibration, calibrate, score, run)
from tests.test_personnel_label_inventory import data


def prepared_fixture():
    history, sources = data()
    value = inventory(history, sources, history_observed_at="2026-02-01T00:00:00Z")
    # Keep private experiment identities out of public unit-test inputs.  These
    # synthetic dates/three opportunities are deliberately not the real study.
    protocol = json.loads((Path(__file__).resolve().parent / "fixtures" /
        "restricted-start-synthetic-protocol-v1.json").read_text())
    assert protocol["purpose"] == "synthetic_unit_test_only_not_actual_experiment_protocol"
    assert protocol["inventory_sha256"] == "0" * 64
    return value, protocol


def fit_rows():
    return [{"match_id": i // 10, "team_id": (i % 2) + 1, "player_id": i,
        "x": [.9 if i % 2 else .1, None if i % 3 else 0., 2., 3., 5.],
        "y": i % 2} for i in range(200)]


class RestrictedStartDevelopmentTests(unittest.TestCase):
    def test_target_xi_changes_only_labels_not_current_input_features(self):
        value, protocol = prepared_fixture()
        rows, _, _ = prepare(value, protocol)
        altered = copy.deepcopy(value)
        team = altered["matches"][-1]["teams"]["1"]
        team["target_xi_ids"][-1] = 111
        pool = team["historical_development_assumption"]
        candidates, actual = set(pool["candidate_ids"]), set(team["target_xi_ids"])
        pool.update(positive_ids=sorted(candidates & actual), negative_ids=sorted(candidates - actual),
                    outside_pool_starting_ids=sorted(actual - candidates))
        other, _, _ = prepare(altered, protocol)
        a = {(r["team_id"], r["player_id"]): r for r in rows if r["match_id"] == 3}
        b = {(r["team_id"], r["player_id"]): r for r in other if r["match_id"] == 3}
        self.assertEqual({key: r["x"] for key, r in a.items()}, {key: r["x"] for key, r in b.items()})
        self.assertNotEqual(a[(1, 111)]["y"], b[(1, 111)]["y"])

    def test_candidates_never_include_target_pool_outside_starter(self):
        value, protocol = prepared_fixture()
        rows, opportunities, _ = prepare(value, protocol)
        self.assertFalse(any(r["player_id"] == 999 for r in rows))
        self.assertEqual(opportunities[-1]["outside_pool_starters"], 1)
        self.assertEqual(len(opportunities), 3)
        self.assertFalse(opportunities[0]["eligible_paired"])

    def test_too_recent_past_record_is_rejected_instead_of_using_its_label(self):
        value, protocol = prepared_fixture()
        value["matches"][-1]["prior_match_ids"] = [3]
        value["matches"][-1]["teams"]["1"]["prior_match_ids"] = [3]
        with self.assertRaisesRegex(ValueError, "past_input_not_before_cutoff"):
            prepare(value, protocol)

    def test_full_opportunity_denominator_mismatch_is_rejected(self):
        value, protocol = prepared_fixture()
        protocol["expected_total_opportunities"] = 4
        with self.assertRaisesRegex(ValueError, "denominator_mismatch"):
            prepare(value, protocol)

    def test_sample_gates_block_all_fitting_without_lowering_minimum(self):
        value, protocol = prepared_fixture()
        rows, opportunities, gates = prepare(value, protocol)
        with patch("scripts.restricted_start_development.fit_logistic") as fitting:
            report, models, predictions = execute(rows, opportunities, gates, protocol)
        fitting.assert_not_called()
        self.assertEqual(report["fits_performed"], 0)
        self.assertEqual(gates["train"]["minimum"], 100)
        self.assertEqual(models, {})
        self.assertEqual(predictions, [])

    def test_preprocessing_learns_only_training_values_and_keeps_raw_null(self):
        training = fit_rows()
        original = copy.deepcopy(training)
        pre = fit_preprocessor(training)
        frozen = copy.deepcopy(pre)
        extreme = copy.deepcopy(training[:2])
        extreme[0]["x"] = [10000., 9999., 8888., 7777., 6666.]
        transformed = design(extreme, pre)
        self.assertTrue(np.all(np.isfinite(transformed)))
        self.assertEqual(pre, frozen)
        self.assertEqual(training, original)
        self.assertIsNone(training[1]["x"][1])

    def test_real_ridge_fit_is_finite_and_marginals_are_not_forced_to_eleven(self):
        rows = fit_rows()
        settings = {"maxiter": 1000, "gtol": 1e-8, "ftol": 1e-12}
        model = fit_logistic(rows, .1, settings)
        p = predict(rows[:2], model)
        self.assertTrue(model["solver_success"])
        self.assertLess(p[0], p[1])
        self.assertTrue(np.all((p > 0) & (p < 1)))
        self.assertNotAlmostEqual(float(p.sum()), 11.)
        self.assertEqual(model["fit_fixture_ids"], list(range(20)))

    def test_calibration_uses_supplied_partition_and_does_not_claim_joint_distribution(self):
        rows = fit_rows()
        cal = fit_calibration([.2 if r["y"] == 0 else .8 for r in rows], rows, 1e-6)
        self.assertGreaterEqual(cal["slope"], 0)
        self.assertEqual(cal["fit_fixture_ids"], list(range(20)))
        p = calibrate([.2, .8], cal, 1e-6)
        self.assertLess(p[0], p[1])
        self.assertNotAlmostEqual(float(p.sum()), 11.)

    def test_score_preserves_fixture_weighting_and_reliability_denominators(self):
        rows = [{"match_id": 1, "team_id": 1, "y": y} for y in [0, 1, 0]]
        rows.append({"match_id": 2, "team_id": 2, "y": 1})
        p = np.array([.1, .9, .2, .8]); metrics = score(rows, p, 1e-6)
        self.assertAlmostEqual(metrics["player_micro_brier"], .025)
        expected = (np.mean([.01, .01, .04]) + .04) / 2
        self.assertAlmostEqual(metrics["equal_fixture_macro_brier"], expected)
        self.assertEqual(sum(b["count"] for b in metrics["reliability"]), 4)
        self.assertFalse(metrics["per_team_probability_sum"]["normalized_to_eleven"])

    def test_before_fit_identity_failure_creates_no_output(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            (base / "inventory.json").write_text('{}')
            (base / "protocol.json").write_text(json.dumps({"inventory_sha256": "wrong"}))
            (base / "freeze.json").write_text('{}')
            with self.assertRaisesRegex(ValueError, "before_fit_freeze_invalid"):
                run(base / "inventory.json", base / "protocol.json", base / "freeze.json", base / "output")
            self.assertFalse((base / "output").exists())


if __name__ == "__main__":
    unittest.main()
