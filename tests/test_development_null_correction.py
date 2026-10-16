"""Tests for the independent v2 experiment; existing modules stay unchanged."""
import copy
from datetime import datetime, timedelta, timezone
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from football_model.model import ModelDataError, _distribution_from_rates

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("development_null", ROOT / "scripts/development_null_correction.py")
DEV = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(DEV)


def protocol():
    return {"feature_names": ["feature"], "correction_min_matches": 4,
        "correction_alpha_grid": [0.1, 1.0, 10.0], "inner_validation_fraction": 0.2,
        "baseline_half_life_days": 180, "inner_tie_tolerance": 1e-12}


def rows(n=20, signal=False):
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    result = []
    for i in range(n):
        kickoff = start + timedelta(days=i)
        for side in ("home", "away"):
            result.append({"match_id": i + 1, "side": side, "kickoff": kickoff.isoformat(),
                "cutoff_at": (kickoff - timedelta(hours=10)).isoformat(),
                "baseline_training_cutoff": (start - timedelta(days=1)).isoformat(),
                "target_available_at": (kickoff + timedelta(hours=4)).isoformat(),
                "lambda0": 1.0, "goals": (4 if i % 2 else 0) if signal else 1,
                "features": {"feature": float(i % 2)},
                "feature_available_at": {"feature": (kickoff - timedelta(hours=10)).isoformat()}})
    return result


class DevelopmentNullCorrectionTests(unittest.TestCase):
    cutoff = "2025-01-01T00:00:00Z"

    def test_exact_null_is_selectable_and_not_unpenalized_fit(self):
        result = DEV.select_with_null(rows(), protocol(), self.cutoff)
        self.assertEqual(result["mode"], "null")
        self.assertNotIn("alpha", result)
        self.assertTrue(all(t.get("alpha", 1) > 0 for t in result["trials"]))

    def test_real_signal_can_choose_nonzero_using_past_validation(self):
        result = DEV.select_with_null(rows(40, signal=True), protocol(), self.cutoff)
        self.assertEqual(result["mode"], "correction")
        self.assertIn(result["alpha"], protocol()["correction_alpha_grid"])
        self.assertTrue(set(result["inner_training_match_ids"]).isdisjoint(result["inner_validation_match_ids"]))

    def test_future_outcome_and_late_feature_rejected(self):
        source = rows()
        source[-1]["target_available_at"] = "2026-01-01T00:00:00Z"
        with self.assertRaises(ModelDataError):
            DEV.select_with_null(source, protocol(), self.cutoff)
        source = rows()
        source[-1]["feature_available_at"]["feature"] = source[-1]["kickoff"]
        with self.assertRaises(ModelDataError):
            DEV.select_with_null(source, protocol(), self.cutoff)

    def test_insufficient_inner_history_returns_null_without_fit(self):
        with patch.object(DEV, "fit_correction", side_effect=AssertionError("must not fit")):
            result = DEV.select_with_null(rows(4), protocol(), self.cutoff)
        self.assertEqual(result["mode"], "null")
        self.assertEqual(result["reason"], "insufficient_inner_training_history")

    def test_failed_nonzero_candidates_preserve_legal_null(self):
        with patch.object(DEV, "fit_correction", side_effect=ModelDataError("fit failed")):
            result = DEV.select_with_null(rows(), protocol(), self.cutoff)
        self.assertEqual(result["mode"], "null")
        self.assertEqual(sum(t["status"] == "failed" for t in result["trials"]), 3)

    def test_outer_refit_failure_preserves_successful_inner_trials(self):
        selection = {"mode": "correction", "alpha": 1.0,
            "trials": [{"mode": "null", "status": "scored", "score": 1.1},
                       {"mode": "correction", "alpha": 1.0, "status": "scored", "score": 1.0}]}
        with patch.object(DEV, "select_with_null", return_value=selection), patch.object(DEV,
                "fit_correction", side_effect=ModelDataError("outer optimizer failed")):
            result = DEV.fit_arm(rows(), protocol(), self.cutoff, "v2_null_allowed")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["failed_stage"], "outer_refit")
        self.assertEqual(result["selection"], selection)

    def test_outer_target_outcome_cannot_change_current_block_selection(self):
        past = rows()
        fixture = {"match_id": 999, "kickoff": "2024-03-02T00:00:00Z",
            "cutoff_at": "2024-03-01T14:00:00Z", "baseline_training_cutoff": "2024-03-01T00:00:00Z",
            "home_goals": 1, "away_goals": 1, "neutral": False}
        fixture["baseline_prediction"] = {**_distribution_from_rates(1, 1),
            "match_id": 999, "training_cutoff": fixture["baseline_training_cutoff"], "kickoff": fixture["kickoff"]}
        outcomes, first = DEV.evaluate(past, [fixture], protocol())
        changed = copy.deepcopy(fixture)
        changed.update(home_goals=9, away_goals=0)
        _, second = DEV.evaluate(past, [changed], protocol())
        self.assertEqual(first, second)
        self.assertEqual(outcomes[0]["status"]["v2_null_allowed"], "null_unchanged_M0")
        self.assertEqual(outcomes[0]["predictions"]["v2_null_allowed"]["score_matrix"], fixture["baseline_prediction"]["score_matrix"])

    def test_all_requested_failures_retained_and_not_zero_scored(self):
        fixture = {"match_id": 998, "kickoff": "2024-03-02T00:00:00Z", "cutoff_at": "2024-03-01T14:00:00Z",
            "baseline_training_cutoff": "2024-03-01T00:00:00Z", "code": "unseen_team", "reason": "unknown"}
        result, _ = DEV.evaluate(rows(), [fixture], protocol())
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["scores"], {})
        self.assertEqual(result[0]["status"]["v2_null_allowed"], "unforecast_no_M0")

    def test_local_artifacts_reject_bad_hash_and_escape_without_network(self):
        with tempfile.TemporaryDirectory() as temporary:
            reader = DEV.OfflineObjects(temporary)
            path = Path(temporary) / "input.json"
            path.write_text("{}")
            with self.assertRaises(ValueError):
                reader.raw({"path": "input.json", "sha256": "0" * 64})
            with self.assertRaises(ValueError):
                reader.raw({"path": "../outside.json", "sha256": "0" * 64})

    def test_match_admission_requires_both_finite_feature_sides(self):
        names = ["rest_days_capped", "matches_last14d", "opponent_rest_days_capped", "opponent_matches_last14d"]
        good = {"match_id": 1, "block": 1, "kickoff": "2024-02-02T00:00:00Z",
            "cutoff_at": "2024-02-01T14:00:00Z", "target_available_at": "2024-02-02T04:00:00Z",
            "baseline_training_cutoff": "2024-02-01T00:00:00Z", "home_goals": 1, "away_goals": 1,
            "home_features": dict.fromkeys(names, 1.0), "away_features": dict.fromkeys(names, 1.0),
            "baseline_prediction": {"expected_goals": {"home": 1.0, "away": 1.0}}}
        for invalid in (None, float("nan"), True):
            bad = copy.deepcopy(good)
            bad["match_id"] = 2
            bad["away_features"][names[0]] = invalid
            replay = {"forecasts": [good, bad], "failures": [], "baseline_fits": [{"block": 1, "training_match_ids": [99]}]}
            class Reader:
                def root(self, ref):
                    return {"EARLY": {"$advanced_object_ref": {"path": "replay"}}} if ref["path"] == "replays" else replay
                def unpack(self, value):
                    return value
            source = {"replays": {"$advanced_object_ref": {"path": "replays"}},
                "protocol": {"candidates": {"M_schedule": {"groups": ["schedule"]}}}}
            generated, requested = DEV.prepare_from_saved_report(Reader(), source)
            self.assertEqual(len(generated), 2)
            self.assertEqual({r["match_id"] for r in generated}, {1})
            self.assertEqual(len(requested), 2)

    def test_output_identity_is_exclusive_and_preserves_partial_run(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = DEV.reserve_output(temporary, "protocol", "run", b"entrypoint", b"protocol", {"hash": "x"})
            with self.assertRaises(FileExistsError):
                DEV.reserve_output(temporary, "protocol", "run", b"changed", b"changed", {"hash": "y"})
            self.assertEqual((path / "entrypoint.py").read_bytes(), b"entrypoint")
            self.assertEqual((path / "protocol.json").read_bytes(), b"protocol")


if __name__ == "__main__":
    unittest.main()
