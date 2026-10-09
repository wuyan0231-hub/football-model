"""Engineering counterexamples; synthetic fixtures never prove model improvement."""
import copy
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

from football_model import total_shrink_shadow as ts
from football_model.model import fit_model, score_distribution
from football_model.prospective import (_append, _digest, _objects, capture_report,
                                        _json_bytes)
from tests.test_model import synthetic_matches

NOW = datetime(2022, 5, 5, tzinfo=timezone.utc)


def iso(value):
    return value.isoformat().replace("+00:00", "Z")


class TotalShrinkShadowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.runtime = Path(self.temp.name) / "runtime"
        self.root = self.runtime / ts.ROOT
        self.now = NOW
        self.clock = lambda: self.now
        self.config = {"half_life_days": 180, "ridge": 3., "min_train": 180,
            "forecast_days": 7, "max_model_age_days": 14,
            "competitions": [{"key": "epl", "mode": "domestic_baseline"}]}
        self.training = synthetic_matches()
        self.model = fit_model(self.training, iso(NOW - timedelta(hours=1)), min_train=180)
        self.bundle = {"model_id": "original-model", "competition": {"key": "epl"}, "model": self.model}
        self.publish([1001], NOW + timedelta(days=1))

    def tearDown(self):
        self.temp.cleanup()

    def write(self, name, value):
        path = self.runtime / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value))

    def publish(self, ids, kickoff, run_id="r1", *, capture_production=True):
        observed = iso(self.now - timedelta(minutes=5))
        start = iso(self.now - timedelta(minutes=10))
        self.rows = []
        for mid in ids:
            self.rows.append({"match_id": mid, "home_id": 1, "away_id": 2,
                "home_name": "Synthetic 1", "away_name": "Synthetic 2", "competition": "epl",
                "kickoff": iso(kickoff), "status": "exploratory_baseline", "model_id": self.bundle["model_id"],
                "time_semantics": "observed_wall_clock_v1", "run_started_at": start,
                "data_observed_at": observed, "training_cutoff": self.model["training_cutoff"],
                "model_fitted_at": iso(NOW - timedelta(minutes=30)),
                "forecast_generated_at": iso(self.now - timedelta(minutes=3)),
                "prediction": score_distribution(self.model, 1, 2, iso(kickoff))})
        self.data = {"matches": self.training, "time_semantics": "observed_wall_clock_v1",
                     "observed_at": observed, "missing_seasons": []}
        self.report = {"run_id": run_id, "mode": "train", "source_commit": "a" * 40,
            "time_semantics": "observed_wall_clock_v1", "run_started_at": start,
            "generated_at": iso(self.now - timedelta(minutes=2)), "requested_competitions": ["epl"],
            "competitions": [{"key": "epl", "status": "探索性基线", "data_observed_at": observed,
                "provider_league_id": 39, "missing_seasons": [],
                "seasons": [{"fetched_at": observed, "received_at": observed}]}]}
        self.index = {"status": "ready", "competitions": [{"key": "epl", "status": "available",
            "latest_successful_forecast": {"run_id": run_id, "forecast_count": len(ids), "unsupported_forecasts": 0}}]}
        self.write("integration/index.json", self.index)
        self.write("data/epl.json", self.data)
        self.write("models/epl.json", self.bundle)
        self.write("predictions/" + run_id + "/epl.json", self.rows)
        self.write("reports/history/" + run_id + ".json", self.report)
        if capture_production:
            capture_report(self.runtime, self.config, self.report,
                           clock=lambda: self.now - timedelta(minutes=1))

    def capture(self):
        return ts.capture(self.runtime, self.config, source_commit="b" * 40, clock=self.clock)

    def result(self, row=None, home=1, away=0, observed=None, captured=None, league_id=39):
        row = row or self.rows[0]
        observed = observed or self.now
        captured = captured or observed
        mid = row["match_id"]
        envelope = {"endpoint": "/fixtures", "params": {"id": mid}, "errors": [], "results": 1,
            "fetched_at": iso(observed), "response": [{"fixture": {"id": mid, "date": row["kickoff"],
                "status": {"short": "FT"}}, "league": {"id": league_id},
                "teams": {"home": {"id": row["home_id"], "name": row["home_name"]},
                          "away": {"id": row["away_id"], "name": row["away_name"]}},
                "score": {"fulltime": {"home": home, "away": away}}}]}
        production = self.runtime / "prospective"
        sid = _append(production, "sources", {"kind": "independent_fixture_result_response",
            "content": envelope, "request": {"fixture_id": mid}, "client_response_sha256": _digest(envelope)})
        value = {k: row[k] for k in ("match_id", "home_id", "away_id", "kickoff", "competition")}
        value.update(home_goals=home, away_goals=away, observed_at=iso(observed), captured_at=iso(captured),
                     source_id=sid, collection_method="independent_fixture_sync")
        return _append(production, "results", value)

    def test_reuses_native_prediction_exactly_and_never_fits_or_mutates_production(self):
        before = {p.relative_to(self.runtime): p.read_bytes() for p in self.runtime.rglob("*.json")}
        with patch("football_model.model.fit_model", side_effect=AssertionError("no fit")), \
             patch("football_model.shadow_models.fit_model", side_effect=AssertionError("no fit")):
            code, report = self.capture()
        self.assertEqual(code, 0, report)
        self.assertEqual(report["captured_forecasts"], 1)
        forecast = _objects(self.root, "forecasts")[0]
        self.assertEqual(forecast["predictions"]["alpha_0"], self.rows[0]["prediction"])
        self.assertEqual(forecast["prediction"], self.rows[0]["prediction"])
        self.assertFalse(forecast["confirmation_eligible"])
        self.assertEqual(forecast["selection"]["n"], 0)
        self.assertEqual(set(forecast["predictions"]), {"alpha_0", "alpha_0.25", "alpha_0.5", "alpha_0.75", "alpha_1"})
        for name, raw in before.items():
            self.assertEqual((self.runtime / name).read_bytes(), raw)
        ts._saved_forecast_valid(self.root, forecast, self.now)

    def test_mean_is_original_unweighted_smoothed_training_mean_not_model_intercept(self):
        self.capture()
        forecast = _objects(self.root, "forecasts")[0]
        n = len(self.training)
        expected = {side: (sum(r[side + "_goals"] for r in self.training) + .5) / (n + .5)
                    for side in ("home", "away")}
        self.assertEqual(forecast["league_mean_rates"], expected)
        self.assertAlmostEqual(sum(forecast["predictions"]["alpha_1"]["expected_goals"].values()), sum(expected.values()))

    def test_grid_preserves_home_away_ratio_and_zero_returns_deep_copy(self):
        original = self.rows[0]["prediction"]
        for alpha in ts.ALPHAS:
            prediction = ts.shrink_prediction(original, {"home": 1., "away": 1.}, alpha)
            r = prediction["expected_goals"]
            base = original["expected_goals"]
            self.assertAlmostEqual(r["home"] / r["away"], base["home"] / base["away"])
        zero = ts.shrink_prediction(original, {"home": 1., "away": 1.}, 0)
        zero["expected_goals"]["home"] = 100
        self.assertNotEqual(zero, original)
        with self.assertRaises(ValueError):
            ts.shrink_prediction(original, {"home": 1., "away": 1.}, True)

    def test_repeat_capture_has_no_duplicate_prediction(self):
        self.capture()
        code, report = self.capture()
        self.assertEqual(code, 0)
        self.assertEqual(report["coverage_status_counts"], {"already_captured": 1})
        self.assertEqual(len(_objects(self.root, "forecasts")), 1)
        _, scored = ts.score(self.runtime, clock=self.clock)
        self.assertEqual(scored["selected_matches"], 1)

    def test_latest_failure_blocks_old_success(self):
        self.index["competitions"][0]["status"] = "blocked_latest_failure"
        self.write("integration/index.json", self.index)
        code, report = self.capture()
        self.assertEqual(code, 1)
        self.assertEqual(report["captured_forecasts"], 0)
        self.assertIn("production_source_blocked", report["errors"][0])

    def test_missing_native_record_cannot_be_backfilled(self):
        for path in (self.runtime / "prospective/forecasts").glob("*.json"):
            path.unlink()
        _, report = self.capture()
        self.assertEqual(report["captured_forecasts"], 0)
        self.assertIn("no_native_recorded", report["competitions"][0]["coverage"][0]["reason"])

    def test_invalid_production_matrix_is_rejected_by_native_recompute(self):
        # Change a production input and re-record under a genuinely distinct run.
        self.rows[0]["prediction"]["expected_goals"]["home"] += .2
        self.write("predictions/r1/epl.json", self.rows)
        for collection in ("forecasts", "coverage"):
            for path in (self.runtime / "prospective" / collection).glob("*.json"):
                path.unlink()
        capture_report(self.runtime, self.config, self.report, clock=lambda: self.now-timedelta(minutes=1))
        _, report = self.capture()
        self.assertEqual(report["captured_forecasts"], 0)
        self.assertIn("native_production_prediction_does_not_recompute", report["competitions"][0]["coverage"][0]["reason"])

    def test_source_content_corruption_is_not_silently_consumed(self):
        self.capture()
        forecast = _objects(self.root, "forecasts")[0]
        path = self.root / "production_parents" / (forecast["production_parent_id"] + ".json")
        parent = json.loads(path.read_text())
        parent["forecast"]["captured_at"] = iso(NOW-timedelta(hours=1))
        path.write_text(json.dumps(parent))
        code, report = ts.score(self.runtime, clock=self.clock)
        self.assertEqual(code, 1)
        self.assertIn("hash_mismatch", report["errors"][0])

    def test_replay_marker_and_public_asof_rejected(self):
        self.write("RESEARCH_REPLAY.json", {"research_replay": True})
        with self.assertRaisesRegex(ValueError, "historical_replay"):
            self.capture()
        with self.assertRaises(SystemExit):
            ts.main(["capture", "--source-commit", "a" * 40, "--as-of", iso(NOW)])

    def test_actual_capture_after_kickoff_cannot_become_future(self):
        self.now += timedelta(days=2)
        _, report = self.capture()
        self.assertEqual(report["captured_forecasts"], 0)
        self.assertEqual(report["coverage_rejection_reasons"], {"not_future_at_real_capture": 1})

    def test_storage_completion_crossing_kickoff_preserves_payload_without_receipt(self):
        real_append = ts.sm._append
        def delayed(root, collection, value):
            result = real_append(root, collection, value)
            if collection == "forecast_payloads":
                self.now += timedelta(days=2)
            return result
        with patch.object(ts.sm, "_append", side_effect=delayed):
            _, report = self.capture()
        self.assertEqual(report["captured_forecasts"], 0)
        self.assertEqual(len(_objects(self.root, "forecast_payloads")), 1)
        self.assertEqual(len(_objects(self.root, "forecasts")), 0)
        self.assertIn("storage_completed_at_or_after_kickoff", report["competitions"][0]["coverage"][0]["reason"])

    def test_clock_regression_does_not_create_receipt(self):
        from itertools import chain, repeat
        values = chain(repeat(NOW, 4), repeat(NOW-timedelta(seconds=1)))
        with self.assertRaisesRegex(ValueError, "wall_clock_regressed"):
            ts.capture(self.runtime, self.config, source_commit="a"*40, clock=lambda: next(values))

    def test_pending_then_score_revision_keeps_original_and_one_independent_match(self):
        self.capture()
        _, pending = ts.score(self.runtime, clock=self.clock)
        self.assertEqual(pending["status_counts"], {"pending": 1})
        originals = {p.name: p.read_bytes() for p in (self.root / "forecasts").glob("*.json")}
        self.now += timedelta(days=1, hours=5)
        self.result()
        code, first = ts.score(self.runtime, clock=self.clock)
        self.assertEqual(code, 0, first)
        self.assertEqual(first["n_scored_matches"], 1)
        self.assertEqual(first["mean_candidate_minus_baseline"]["total_goals_log_loss"], 0.)
        count = len(_objects(self.root, "scores"))
        self.now += timedelta(hours=1)
        self.result(home=0, away=3)
        _, revised = ts.score(self.runtime, clock=self.clock)
        self.assertEqual(revised["selected_matches"], 1)
        self.assertEqual(len(_objects(self.root, "scores")), count+1)
        for name, raw in originals.items():
            self.assertEqual((self.root / "forecasts" / name).read_bytes(), raw)

    def test_wrong_result_league_is_rejected(self):
        self.capture()
        self.now += timedelta(days=1, hours=5)
        self.result(league_id=99)
        code, report = ts.score(self.runtime, clock=self.clock)
        self.assertEqual(code, 1)
        self.assertIn("result_competition_mismatch", report["errors"][0])

    def test_48h_is_from_real_result_receipt_not_old_kickoff(self):
        self.capture()
        self.now += timedelta(days=4)
        self.result(observed=self.now-timedelta(hours=1))
        old_rows = copy.deepcopy(self.rows)
        self.publish([2001], self.now+timedelta(days=1), run_id="r2")
        _, report = self.capture()
        selection = report["competitions"][0]["selection"]
        self.assertEqual(selection["n"], 0)
        frozen = ts._load_object(self.root, "selection_inputs", selection["selection_input_id"])
        self.assertIn("true_first_recorded_result_not_48h_mature", frozen["history_coverage"][0]["reason"])
        self.now += timedelta(hours=48)
        # Update observation/run, leaving legitimate saved earlier predictions intact.
        self.publish([2002], self.now+timedelta(days=1), run_id="r3")
        _, report = self.capture()
        self.assertEqual(report["competitions"][0]["selection"]["n"], 1)

    def test_result_observed_earlier_but_recorded_later_obeys_receipt_embargo(self):
        self.capture()
        self.now += timedelta(days=5)
        self.result(observed=NOW+timedelta(days=1, hours=5), captured=self.now-timedelta(hours=1))
        self.publish([2001], self.now+timedelta(days=1), run_id="r2")
        _, report = self.capture()
        self.assertEqual(report["competitions"][0]["selection"]["n"], 0)

    def test_40_independent_pairs_select_grid_with_latest_pre_cutoff_revision(self):
        self.publish(list(range(1001, 1041)), NOW+timedelta(days=1), run_id="r40")
        _, captured = self.capture()
        self.assertEqual(captured["captured_forecasts"], 40)
        history = copy.deepcopy(self.rows)
        observed = NOW+timedelta(days=1, hours=5)
        for row in history:
            self.result(row=row, home=2, away=1, observed=observed)
        self.now = observed+timedelta(hours=49)
        self.publish([2001], self.now+timedelta(days=1), run_id="r41")
        _, report = self.capture()
        selected = report["competitions"][0]["selection"]
        self.assertEqual(selected["n"], 40)
        self.assertEqual(selected["reason"], "past_minimum_total_log_loss")
        forecasts = _objects(self.root, "forecasts")
        new = next(f for f in forecasts if f["match_id"] == 2001)
        ts._saved_forecast_valid(self.root, new, self.now)
        original_selection = copy.deepcopy(selected)
        # A future revision cannot retroactively alter the frozen choice.
        self.result(row=history[0], home=9, away=1, observed=self.now+timedelta(hours=1))
        self.now += timedelta(hours=2)
        ts._saved_forecast_valid(self.root, new, self.now)
        self.assertEqual(new["selection"], original_selection)

    def test_numeric_tolerance_is_derived_only_alpha_zero_remains_exact(self):
        self.capture()
        original = _objects(self.root, "forecasts")[0]
        forecast = {k:v for k,v in original.items() if k != "object_id"}
        p = forecast["predictions"]["alpha_0.5"]["score_matrix"]
        p[0][0] += 1e-17
        payload = {k: v for k, v in forecast.items() if k not in ("payload_id", "storage_completed_at", "captured_at", "live_before_kickoff")}
        forecast["payload_id"] = _append(self.root, "forecast_payloads", payload)
        ts._saved_forecast_valid(self.root, forecast, self.now)
        forecast["predictions"]["alpha_0"]["expected_goals"]["home"] += 1e-17
        # Force a representable perturbation.
        forecast["predictions"]["alpha_0"]["expected_goals"]["home"] = float(np.nextafter(forecast["predictions"]["alpha_0"]["expected_goals"]["home"], np.inf))
        payload = {k: v for k, v in forecast.items() if k not in ("payload_id", "storage_completed_at", "captured_at", "live_before_kickoff")}
        forecast["payload_id"] = _append(self.root, "forecast_payloads", payload)
        with self.assertRaisesRegex(ValueError, "alpha_zero"):
            ts._saved_forecast_valid(self.root, forecast, self.now)

    def test_capacity_refusal_leaves_diagnostic_and_no_forecasts(self):
        estimate = ts.sm.estimate_runtime(self.runtime)
        with patch.object(ts.sm, "STORAGE_LIMIT_BYTES", estimate["persisted_bytes"] - 1):
            code, report = self.capture()
        self.assertEqual(code, 1)
        self.assertEqual(report["status"], "capacity_deferred")
        self.assertEqual(len(_objects(self.root, "forecasts")), 0)
        latest = json.loads((self.root/"reports/latest.json").read_text())
        self.assertEqual(latest, report)
        self.assertEqual(latest["source_commit"], "b"*40)

    def test_capacity_diagnostic_cannot_bypass_actual_logical_limit(self):
        self.capture()
        budget = {"estimated_persisted_bytes": 0, "logical_bytes": ts.sm.MAX_LOGICAL_BYTES}
        error = ts.sm.ShadowCapacityError(budget)
        error.run_context = (NOW, "b"*40)
        actual = ts.sm.estimate_runtime(self.runtime)
        actual["logical_bytes"] = ts.sm.MAX_LOGICAL_BYTES
        with patch.object(ts.sm, "estimate_runtime", return_value=actual):
            with self.assertRaisesRegex(ValueError, "exceeds_shared_limits"):
                ts._capacity(self.runtime, "score", error, self.clock)

    def test_above_shadow_and_reserve_keeps_bounded_failure_under_hard_transport_limit(self):
        actual = ts.sm.estimate_runtime(self.runtime)
        actual["persisted_bytes"] = ts.sm.STORAGE_LIMIT_BYTES + ts.sm.CAPACITY_REPORT_RESERVE_BYTES + 1
        stale = ts.sm.ShadowCapacityError({"estimated_persisted_bytes": 0, "logical_bytes": 0})
        with patch.object(ts.sm, "estimate_runtime", return_value=actual):
            code, report = ts._capacity(self.runtime, "capture", stale, self.clock)
        self.assertEqual(code, 1)
        self.assertEqual(report["status"], "capacity_deferred")
        self.assertFalse(report["immutable_event_saved"])
        self.assertEqual(json.loads((self.root / "reports/latest.json").read_text()), report)
        actual["persisted_bytes"] = ts.sm.MAX_PHYSICAL_BYTES
        with patch.object(ts.sm, "estimate_runtime", return_value=actual):
            with self.assertRaisesRegex(ValueError, "exceeds_shared_limits"):
                ts._capacity(self.runtime, "capture", stale, self.clock)

    def test_capacity_diagnostics_remeasure_stale_budget_under_writer_lock(self):
        self.capture()
        actual = ts.sm.estimate_runtime(self.runtime)
        stale = ts.sm.ShadowCapacityError({"estimated_persisted_bytes": 0, "logical_bytes": 0})
        stale.run_context = (NOW, "b"*40)
        with patch.object(ts.sm, "estimate_runtime", return_value=actual) as estimate:
            _, report = ts._capacity(self.runtime, "capture", stale, self.clock)
        estimate.assert_called_once()
        self.assertEqual(report["logical_bytes"], actual["logical_bytes"])
        self.assertEqual(report["estimated_persisted_bytes"], actual["persisted_bytes"])
        with ts.sm._writer(self.runtime):
            with self.assertRaisesRegex(ValueError, "already_active"):
                ts._capacity(self.runtime, "capture", stale, self.clock)

    def test_shared_sources_are_content_addressed_once_per_production_run(self):
        self.publish([1001,1002,1003], NOW+timedelta(days=1), run_id="r3")
        _, report = self.capture()
        self.assertEqual(report["captured_forecasts"], 3)
        self.assertEqual(len(_objects(self.root,"production_sources")), 4)
        self.assertEqual(len(_objects(self.root,"production_parents")), 3)

    def test_checkpoint_and_final_all_competition_reports_are_legally_distinct(self):
        # The production ledger really freezes its one-league checkpoint; the
        # integration index later points at a final multi-league report.
        self.report["requested_competitions"] += ["mls"]
        self.report["competitions"] += [{"key": "mls", "status": "no_upcoming"}]
        self.report["generated_at"] = iso(self.now-timedelta(seconds=30))
        self.write("reports/history/r1.json", self.report)
        code, report = self.capture()
        self.assertEqual(code, 0)
        self.assertEqual(report["captured_forecasts"], 1, report)
        code, scored = ts.score(self.runtime, clock=self.clock)
        self.assertEqual(code, 0, scored)
        self.assertEqual(scored["n_scored_matches"], 0)

    def test_checkpoint_cannot_bind_different_final_competition_item(self):
        self.report["competitions"][0]["detail"] = "changed after checkpoint"
        self.write("reports/history/r1.json", self.report)
        _, report = self.capture()
        self.assertEqual(report["captured_forecasts"], 0)
        self.assertIn("report_competition_item_mismatch", report["competitions"][0]["coverage"][0]["reason"])

    def test_different_registered_production_fit_config_never_mixes_history(self):
        self.capture()
        first_protocol = _objects(self.root, "forecasts")[0]["protocol_id"]
        self.config["ridge"] = 4.
        _, report = self.capture()
        self.assertNotEqual(report["protocol_id"], first_protocol)
        self.assertEqual(report["captured_forecasts"], 0)
        self.assertEqual(report["competitions"][0]["selection"]["n"], 0)
        self.assertIn("registered_production_fit_parameters_mismatch", report["competitions"][0]["coverage"][0]["reason"])
        self.assertEqual(len(_objects(self.root, "forecasts")), 1)

    def test_all_frozen_data_and_schedule_parent_files_are_required(self):
        self.capture()
        forecast = _objects(self.root, "forecasts")[0]
        for field in ("data_source", "schedule_source"):
            path = self.root/"sources"/(forecast["parents"][field]+".json")
            raw = path.read_bytes()
            path.unlink()
            with self.assertRaises(OSError):
                ts._saved_forecast_valid(self.root,forecast,self.now)
            path.write_bytes(raw)

    def test_known_quote_fixture_reschedule_fences_old_prediction(self):
        observed = self.now-timedelta(minutes=1)
        envelope = {"endpoint": "/fixtures", "params": {"id": 1001}, "errors": [],
            "fetched_at": iso(observed), "response": [{"fixture": {"id": 1001,
                "date": iso(NOW+timedelta(days=2)), "status": {"short": "NS"}},
                "league": {"id": 39}, "teams": {"home": {"id": 1}, "away": {"id": 2}}}]}
        raw = _json_bytes(envelope)
        self.write("quotes/fixtures/1001/"+hashlib.sha256(raw).hexdigest()+".json", envelope)
        # Use exact bytes matching the fixture reader's raw SHA assertion.
        path = next((self.runtime/"quotes/fixtures/1001").glob("*.json"))
        path.write_bytes(raw)
        _, report = self.capture()
        self.assertEqual(report["captured_forecasts"], 0)
        self.assertIn("known_newer_fixture_identity_conflict", report["competitions"][0]["coverage"][0]["reason"])


if __name__ == "__main__":
    unittest.main()
