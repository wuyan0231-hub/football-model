"""Small synthetic checks for the read-only F5 historical diagnostic entrypoint."""
from __future__ import annotations

import copy
import hashlib
import io
import json
import math
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

from scripts import s2_f5_error_audit as audit


def forecast():
    return {
        "expected_goals": {"home": 1.5, "away": 0.5},
        "probabilities": {"home_win": 0.6, "draw": 0.25, "away_win": 0.15},
        "events": {"over_2_5": 0.4, "home_win_by_2_plus": 0.2},
        "settlement_probabilities": {
            "over_2_25": dict(zip(("win", "half_win", "push", "half_loss", "loss"),
                                  (0.4, 0.1, 0.05, 0.15, 0.3))),
            "over_2_75": dict(zip(("win", "half_win", "push", "half_loss", "loss"),
                                  (0.3, 0.2, 0.1, 0.15, 0.25))),
        },
        "matrix_shape": [10, 10],
        "tail_mass_before_normalization": 1e-8,
    }


def metric_row(home=2, away=0, match_id=1, block="2025-01-01T00:00:00Z"):
    return {"match_id": match_id, "actual_home_goals": home, "actual_away_goals": away,
            "evaluation_block_start": block,
            "forecasts": {name: forecast() for name in audit.NAMES}}


def league_fixture():
    """Exactly two fictional identities, one origin, and all five saved outputs."""
    start = datetime(2025, 1, 1, tzinfo=timezone.utc)
    iso = lambda value: value.isoformat().replace("+00:00", "Z")
    protocol = {"start": iso(start), "end": iso(start + timedelta(days=7)),
                "inner_days": 0, "step_days": 7}
    rows = []
    for i in (1, 2):
        kickoff = start + timedelta(days=i)
        row = metric_row(match_id=i, block=iso(start))
        row.update(home_id=10*i, away_id=10*i+1, kickoff=iso(kickoff),
                   forecast_origin=iso(start), phase="outer",
                   result_assumed_available_at=iso(kickoff + timedelta(hours=4)),
                   training_data_sha256="a"*64, neutral=False,
                   all_half_lives_common=True,
                   selected_candidate_at_origin="half_life_90",
                   effective_policy_candidate="half_life_90", policy_fallback=False)
        rows.append(row)
    coverage = {"outer_total": 2, "all_candidates_common": 2,
                "inner_warmup_count": 0, "by_candidate": {name: 2 for name in audit.NAMES}}
    data = {
        "predictions": rows,
        "windows": [{"origin": iso(start), "end": protocol["end"], "phase": "outer",
                     "training_data_sha256": "a"*64,
                     "last_result_assumed_available_at": iso(start),
                     "test_match_ids": [1, 2], "selection": {"candidate": "half_life_90"}}],
        "report": {"coverage": coverage, "common_sample_metrics": {
            name: {"n": 2, "distribution_diagnostics": {key: 0. for key in audit.SOURCE_METRICS}}
            for name in audit.NAMES}},
    }
    return data, protocol, {"coverage": copy.deepcopy(coverage)}


class F5ErrorAuditTests(unittest.TestCase):
    def test_hand_calculated_match_metrics(self):
        values = audit.metrics([metric_row()], "half_life_180")
        expected = {
            "one_x_two_log_loss": -math.log(0.6),
            "one_x_two_brier": (1-0.6)**2 + 0.25**2 + 0.15**2,
            "score_log_loss": 2 - 2*math.log(1.5) + math.log(2),
            "total_log_loss": 2 - math.log(2),
            "total_absolute_error": 0., "margin_absolute_error": 1.,
            "over_2_5_log_loss": -math.log(0.6), "over_2_5_brier": 0.16,
            "home_win_by_2_plus_log_loss": -math.log(0.2), "one_x_two_correct": 1.,
        }
        for key, value in expected.items():
            with self.subTest(metric=key):
                self.assertEqual(values[key].shape, (1,))
                self.assertAlmostEqual(values[key][0], value, places=14)

    def test_quarter_total_settlements_at_two_three_four_goals(self):
        rows = [metric_row(2, away) for away in (0, 1, 2)]
        values = audit.metrics(rows, "half_life_180")
        # O2.25: half loss, win, win. O2.75: loss, half win, win.
        for market, probabilities in (("over_2_25", [0.15, 0.4, 0.4]),
                                      ("over_2_75", [0.25, 0.2, 0.3])):
            with self.subTest(market=market):
                np.testing.assert_allclose(values[market+"_settlement_log_loss"],
                                           [-math.log(p) for p in probabilities], rtol=0, atol=1e-14)

    def test_calibration_endpoints_and_empty_bins(self):
        value = audit.calibration([0., 1.], [0, 1])
        self.assertEqual(value["n"], 2)
        self.assertEqual(value["bins"][0]["n"], 1)
        self.assertEqual(value["bins"][-1]["n"], 1)
        self.assertEqual(sum(b["n"] for b in value["bins"]), 2)
        self.assertEqual(value["binary_brier"], 0)
        self.assertEqual(value["binary_log_loss"], 0)
        empty = audit.calibration([], [])
        self.assertEqual(empty["n"], 0)
        for key in ("predicted_mean", "observed_rate", "binary_brier", "binary_log_loss",
                    "observed_minus_predicted", "fixed_10_bin_ECE_descriptive"):
            self.assertIsNone(empty[key])
        self.assertTrue(all(b["n"] == 0 and b["predicted_mean"] is None for b in empty["bins"]))

    def test_calibration_rejects_invalid_inputs(self):
        for probs, actual in (([0.5], []), ([[0.5]], [[1]]), ([math.nan], [1]),
                              ([1.1], [1]), ([-0.1], [0]), ([0.5], [2])):
            with self.subTest(probs=probs, actual=actual), self.assertRaises(ValueError):
                audit.calibration(probs, actual)

    def test_paired_variance_counts_matches_once_across_candidates(self):
        rows = [metric_row(match_id=i, block="A" if i < 2 else "B") for i in range(4)]
        result = audit.paired_summary(rows, np.array([-0.2, 0.1, 0.2, -0.1]))
        self.assertEqual(result["n"], 4)  # 5 candidate outputs never become 20 samples.
        self.assertEqual(result["calendar_28_day_blocks"], 2)
        self.assertEqual([block["n"] for block in result["blocks"]], [2, 2])
        self.assertAlmostEqual(result["mean_candidate_minus_baseline"], 0., places=14)
        self.assertAlmostEqual(result["paired_sd"], math.sqrt(0.1/3), places=14)
        self.assertAlmostEqual(result["iid_standard_error"], math.sqrt(0.1/12), places=14)
        self.assertAlmostEqual(result["calendar_cluster_standard_error_descriptive"], 0.05, places=14)
        sensitivity = result["sample_size_sensitivity_not_validation_gate"]
        self.assertGreaterEqual(sensitivity["normal_approximation_n_no_cluster_discount"],
                                sensitivity["normal_approximation_n_iid"])

    def test_paired_summary_rejects_missing_single_or_nonfinite_pairs(self):
        for rows, delta in (([], []), ([metric_row()], [0.1]),
                            ([metric_row(), metric_row()], [0.1]),
                            ([metric_row(), metric_row()], [0.1, math.inf])):
            with self.subTest(n=len(rows), delta=delta), self.assertRaises(ValueError):
                audit.paired_summary(rows, np.asarray(delta))

    def test_single_calendar_block_preserves_iid_and_unknown_cluster_variance(self):
        rows = [metric_row(match_id=1), metric_row(match_id=2)]
        value = audit.paired_summary(rows, np.array([-0.1, 0.1]))
        self.assertEqual(value["n"], 2)
        self.assertEqual(value["calendar_28_day_blocks"], 1)
        self.assertAlmostEqual(value["iid_standard_error"], 0.1)
        self.assertIsNone(value["calendar_cluster_standard_error_descriptive"])
        self.assertIsNone(value["variance_inflation_vs_iid_descriptive"])
        sensitivity = value["sample_size_sensitivity_not_validation_gate"]
        self.assertIsNone(sensitivity["normal_approximation_n_empirical_calendar_cluster"])
        self.assertEqual(sensitivity["normal_approximation_n_no_cluster_discount"],
                         sensitivity["normal_approximation_n_iid"])

    def test_forecast_rejects_nonfinite_range_sum_and_boolean_values(self):
        audit.validate_forecast(forecast())
        cases = (
            (("expected_goals", "home"), math.nan), (("expected_goals", "away"), 0),
            (("expected_goals", "home"), True), (("probabilities", "home_win"), 1.1),
            (("probabilities", "draw"), -0.1), (("probabilities", "draw"), 0.3),
            (("probabilities", "away_win"), False), (("events", "over_2_5"), math.inf),
            (("tail_mass_before_normalization",), -1e-9), (("matrix_shape",), [True, 10]),
        )
        for path, invalid in cases:
            value = forecast()
            target = value
            for part in path[:-1]:
                target = target[part]
            target[path[-1]] = invalid
            with self.subTest(path=path, invalid=invalid), self.assertRaises(ValueError):
                audit.validate_forecast(value)

    def test_strict_json_validates_raw_hash_and_rejects_nonstandard_numbers(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/"input.json"
            raw = b'{"number": 0.25, "items": [0, 1]}'
            path.write_bytes(raw)
            parsed, digest = audit.strict_json(path)
            self.assertEqual(parsed, {"number": 0.25, "items": [0, 1]})
            self.assertEqual(digest, hashlib.sha256(raw).hexdigest())
            for invalid in ('{"x":1,"x":2}', '{"nested":{"x":1,"x":2}}',
                            '{"x":NaN}', '{"x":Infinity}', '{"x":[1e999]}', '{'):
                path.write_text(invalid)
                with self.subTest(raw=invalid), self.assertRaises(ValueError):
                    audit.strict_json(path)

    def test_paths_reject_existing_missing_nested_and_symlink_locations(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, plan, output = root/"source", root/"plan.json", root/"output"
            source.mkdir()
            plan.write_text("{}")
            self.assertEqual(audit.validate_paths(source, plan, output), (source, plan, output))
            output.mkdir()
            with self.assertRaisesRegex(ValueError, "output_already_exists"):
                audit.validate_paths(source, plan, output)
            output.rmdir()
            for inp, p, out in ((root/"missing", plan, output), (source, root/"missing.json", output),
                                 (source, plan, source/"nested"), (source, plan, root/"missing"/"out")):
                with self.subTest(inp=inp, plan=p, out=out), self.assertRaises(ValueError):
                    audit.validate_paths(inp, p, out)
            source_link, plan_link, out_link = root/"source-link", root/"plan-link", root/"out-link"
            source_link.symlink_to(source, target_is_directory=True)
            plan_link.symlink_to(plan)
            out_link.symlink_to(root/"absent-target", target_is_directory=True)
            for inp, p, out in ((source_link, plan, output), (source, plan_link, output),
                                 (source, plan, out_link)):
                with self.subTest(inp=inp, plan=p, out=out), self.assertRaises(ValueError):
                    audit.validate_paths(inp, p, out)
            with self.assertRaises(ValueError):
                audit.strict_json(plan_link)

    def test_valid_two_match_league_records_each_identity_once(self):
        data, protocol, run = league_fixture()
        identities = set()
        audit.validate_league(data, protocol, run, identities)
        self.assertEqual(identities, {1, 2})

    def test_league_rejects_duplicate_global_id_and_team_kickoff(self):
        data, protocol, run = league_fixture()
        with self.assertRaisesRegex(ValueError, "duplicate_match_id"):
            audit.validate_league(data, protocol, run, {1})
        data["predictions"][1]["match_id"] = 1
        with self.assertRaisesRegex(ValueError, "duplicate_match_id"):
            audit.validate_league(data, protocol, run, set())
        data, protocol, run = league_fixture()
        for key in ("home_id", "away_id", "kickoff"):
            data["predictions"][1][key] = data["predictions"][0][key]
        with self.assertRaisesRegex(ValueError, "duplicate_match_identity"):
            audit.validate_league(data, protocol, run, set())

    def test_league_rejects_origin_block_training_hash_and_bad_goals(self):
        for key, invalid, reason in (
            ("forecast_origin", "2024-12-31T00:00:00Z", "match_outside_forecast_window"),
            ("kickoff", "2025-01-08T00:00:00Z", "match_outside_forecast_window"),
            ("result_assumed_available_at", "2025-01-02T03:00:00Z", "result_availability_mismatch"),
            ("evaluation_block_start", "2025-01-02T00:00:00Z", "evaluation_block_mismatch"),
            ("training_data_sha256", "b"*64, "training_input_origin_mismatch"),
            ("actual_home_goals", -1, "invalid_integer"),
            ("actual_away_goals", True, "invalid_integer"),
            ("actual_home_goals", 1.5, "invalid_integer"),
        ):
            data, protocol, run = league_fixture()
            data["predictions"][0][key] = invalid
            with self.subTest(key=key, invalid=invalid), self.assertRaisesRegex(ValueError, reason):
                audit.validate_league(data, protocol, run, set())

    def test_league_rejects_window_and_coverage_disagreement(self):
        cases = (
            ("window_end", "forecast_window_calendar_mismatch"),
            ("training_after_origin", "training_result_after_origin"),
            ("window_ids", "window_match_identity_mismatch"),
            ("coverage", "manifest_coverage_mismatch"),
            ("metric_count", "metric_sample_size_mismatch"),
            ("policy", "selected_policy_not_original_candidate"),
        )
        for case, reason in cases:
            data, protocol, run = league_fixture()
            if case == "window_end":
                data["windows"][0]["end"] = "2025-01-09T00:00:00Z"
            elif case == "training_after_origin":
                data["windows"][0]["last_result_assumed_available_at"] = "2025-01-02T00:00:00Z"
            elif case == "window_ids":
                data["windows"][0]["test_match_ids"] = [1, 3]
            elif case == "coverage":
                data["report"]["coverage"]["outer_total"] = 3
            elif case == "metric_count":
                data["report"]["common_sample_metrics"]["half_life_90"]["n"] = 3
            else:
                data["predictions"][0]["forecasts"]["selected_policy"]["expected_goals"]["home"] = 2.
            with self.subTest(case=case), self.assertRaisesRegex(ValueError, reason):
                audit.validate_league(data, protocol, run, set())

    def test_empty_common_group_is_rejected_without_computing_metrics(self):
        data, protocol, run = league_fixture()
        for row in data["predictions"]:
            row["forecasts"] = {}
            row["all_half_lives_common"] = False
            row["effective_policy_candidate"] = None
        with self.assertRaisesRegex(ValueError, "requires_at_least_two_common_matches"):
            audit.validate_league(data, protocol, run, set())
        with self.assertRaisesRegex(ValueError, "metrics_require_nonempty_matches"):
            audit.metrics([], "half_life_180")

    def test_cli_rejects_existing_output_before_reading_malformed_inputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, plan, output = root/"source", root/"plan.json", root/"output"
            source.mkdir()
            plan.write_text("not-json")
            output.mkdir()
            prior = output/"prior-evidence.json"
            prior.write_bytes(b'{"original":true}\n')
            stderr, stdout = io.StringIO(), io.StringIO()
            with redirect_stderr(stderr), redirect_stdout(stdout):
                code = audit.main(["--input-dir", str(source), "--plan", str(plan), "--output-dir", str(output)])
            self.assertEqual(code, 1)
            self.assertEqual(json.loads(stderr.getvalue())["error"], "output_already_exists")
            self.assertEqual(stdout.getvalue(), "")
            self.assertEqual(prior.read_bytes(), b'{"original":true}\n')
            self.assertEqual(list(output.iterdir()), [prior])

    def test_cli_missing_or_malformed_inputs_never_create_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, plan = root/"source", root/"plan.json"
            source.mkdir()
            for content in ("{", "{}"):
                plan.write_text(content)
                output = root/"output"
                stderr, stdout = io.StringIO(), io.StringIO()
                with self.subTest(plan=content), redirect_stderr(stderr), redirect_stdout(stdout):
                    code = audit.main(["--input-dir", str(source), "--plan", str(plan), "--output-dir", str(output)])
                self.assertEqual(code, 1)
                self.assertFalse(json.loads(stderr.getvalue())["completed"])
                self.assertEqual(stdout.getvalue(), "")
                self.assertFalse(output.exists())
                self.assertEqual(plan.read_text(), content)


if __name__ == "__main__":
    unittest.main()
