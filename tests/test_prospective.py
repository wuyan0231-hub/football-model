"""Real wall-clock injection is for controlled tests, not a public --as-of."""
import copy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from football_model.model import _distribution_from_rates, _validated_matches
from football_model.prospective import run, main, _objects, _categorical_metrics

TIME = "observed_wall_clock_v1"


def dt(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class ProspectiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.clock = lambda: dt("2026-10-06T10:05:00Z")
        self.training = [{"match_id": 1, "home_id": 1, "away_id": 2,
            "home_name": "Home", "away_name": "Away", "kickoff": "2026-10-01T16:00:00Z",
            "home_goals": 2, "away_goals": 1}]
        serial = [{k: v for k, v in row.items() if k != "_dt"} for row in _validated_matches(self.training)]
        digest = hashlib.sha256(json.dumps(serial, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        self.row = {"match_id": 22, "home_id": 1, "away_id": 2, "home_name": "Home", "away_name": "Away",
            "kickoff": "2026-10-06T16:00:00Z", "competition": "epl", "model_id": "epl-original",
            "time_semantics": TIME, "run_started_at": "2026-10-06T10:00:00Z",
            "data_observed_at": "2026-10-06T10:01:00Z", "training_cutoff": "2026-10-06T10:00:00Z",
            "model_fitted_at": "2026-10-06T10:02:00Z", "forecast_generated_at": "2026-10-06T10:03:00Z",
            "status": "exploratory_baseline", "prediction": _distribution_from_rates(1.5, 1.2)}
        self.row["prediction"].update(home_id=1, away_id=2, kickoff=self.row["kickoff"],
            training_cutoff=self.row["training_cutoff"], training_data_sha256=digest,
            model_version="independent_poisson_time_decay_l2_v1", neutral=False)
        self.report = {"run_id": "r1", "time_semantics": TIME, "run_started_at": self.row["run_started_at"],
            "generated_at": "2026-10-06T10:04:00Z", "requested_competitions": ["epl"],
            "competitions": [{"key": "epl", "status": "success"}], "source_commit": "a" * 40}
        self.model = {"model_id": "epl-original", "competition": {"key": "epl"},
                      "model": {"training_data_sha256": digest}}
        self.data = {"time_semantics": TIME, "observed_at": self.row["data_observed_at"], "matches": self.training}
        self.config = {"prospective": {"contracts": [
            {"market": "total_goals", "side": "over", "line": 2.25},
            {"market": "asian_handicap", "side": "home", "line": -0.25}]}}
        self.save_inputs()

    def tearDown(self):
        self.temp.cleanup()

    def write(self, filename, value):
        path = self.root / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value))

    def save_inputs(self):
        self.write("reports/latest.json", self.report)
        self.write("predictions/" + self.report["run_id"] + "/epl.json", [self.row])
        self.write("models/epl.json", self.model)
        self.write("data/epl.json", self.data)

    def invoke(self, **kwargs):
        return run(self.root, self.config, clock=self.clock, **kwargs)

    def result(self, home=2, away=1, observed="2026-10-06T21:00:00Z"):
        row = {k: self.row[k] for k in ("match_id", "home_id", "away_id", "home_name", "away_name", "kickoff")}
        row.update(home_goals=home, away_goals=away)
        self.data["matches"] = self.training + [row]
        self.data["observed_at"] = observed
        self.write("data/epl.json", self.data)
        self.clock = lambda: dt(observed.replace("21:00", "21:01").replace("22:00", "22:01"))

    def test_first_capture_frozen_pending_and_reruns_idempotent(self):
        code, first = self.invoke()
        self.assertEqual(code, 0)
        self.assertEqual(first["forecast_versions"], 1)
        self.assertEqual(first["groups"][0]["pending"], 1)
        original = list((self.root / "prospective/forecasts").glob("*.json"))[0].read_bytes()
        self.result()
        code, later = self.invoke()
        self.assertEqual(code, 0)
        self.assertEqual(later["forecast_versions"], 1)
        self.assertEqual(later["groups"][0]["scored"], 1)
        self.assertEqual(later["groups"][0]["common_sample"]["n"], 1)
        self.assertEqual(original, list((self.root / "prospective/forecasts").glob("*.json"))[0].read_bytes())
        self.assertEqual(later["quote_simulation"]["status"], "not_available")
        score = later["groups"][0]["all_model_coverage"]["contracts"]
        over = next(v for v in score if v["contract"]["market"] == "total_goals")
        self.assertEqual(set(over["metrics"]["calibration"]), {"win", "half_loss", "loss"})

    def test_score_corrections_append_and_do_not_overwrite_forecasts_or_prior_result(self):
        self.invoke()
        self.result()
        self.invoke()
        old = {p.name: p.read_bytes() for p in (self.root / "prospective/results").glob("*.json")}
        self.result(home=0, away=3, observed="2026-10-06T22:00:00Z")
        _, summary = self.invoke()
        self.assertEqual(summary["result_versions"], 2)
        for name, value in old.items():
            self.assertEqual((self.root / "prospective/results" / name).read_bytes(), value)
        results = _objects(self.root / "prospective", "results")
        newest = max(results, key=lambda r: r["observed_at"])
        self.assertIn(newest["supersedes"] + ".json", old)
        self.assertEqual(summary["groups"][0]["scored"], 1)

    def test_late_capture_rejected_even_when_forecast_claims_pre_match_time(self):
        self.clock = lambda: dt("2026-10-06T17:00:00Z")
        code, summary = self.invoke()
        self.assertEqual(code, 1)
        self.assertEqual(summary["forecast_versions"], 0)
        self.assertEqual(summary["coverage_status_counts"], {"rejected": 1})

    def test_future_generation_wrong_identity_matrix_and_research_replay_rejected(self):
        mutations = [lambda r: r.update(forecast_generated_at="2026-10-06T11:00:00Z"),
                     lambda r: r["prediction"].update(home_id=99),
                     lambda r: r["prediction"].update(score_matrix=[[.2]]),
                     lambda r: r.update(research_replay=True)]
        for i, mutate in enumerate(mutations):
            with self.subTest(i=i):
                row = copy.deepcopy(self.row)
                mutate(row)
                self.report["run_id"] = "bad" + str(i)
                self.write("reports/latest.json", self.report)
                self.write("predictions/" + self.report["run_id"] + "/epl.json", [row])
                code, summary = self.invoke()
                self.assertEqual(code, 1)
                self.assertEqual(summary["forecast_versions"], 0)

    def test_unsupported_and_missing_league_are_kept_not_replaced_by_old_predictions(self):
        self.row.update(status="unsupported", reason="expired")
        self.report["requested_competitions"].append("laliga")
        self.save_inputs()
        self.write("predictions/old/laliga.json", [self.row])
        code, summary = self.invoke()
        self.assertEqual(code, 0)
        self.assertEqual(summary["coverage_status_counts"], {"unsupported": 1, "no_current_prediction_file": 1})
        self.assertEqual(summary["forecast_versions"], 0)

    def test_future_or_wrong_identity_result_rejected(self):
        self.invoke()
        self.result()
        self.clock = lambda: dt("2026-10-06T20:30:00Z")
        code, summary = self.invoke()
        self.assertEqual(code, 1)
        self.assertEqual(summary["result_versions"], 0)
        self.clock = lambda: dt("2026-10-06T21:01:00Z")
        self.data["matches"][-1]["away_id"] = 77
        self.write("data/epl.json", self.data)
        code, summary = self.invoke()
        self.assertEqual(code, 1)
        self.assertEqual(summary["result_versions"], 0)

    def test_multiple_forecasts_one_independent_primary_match_and_same_protocol(self):
        self.invoke()
        first_id = _objects(self.root / "prospective", "forecasts")[0]["object_id"]
        self.report["run_id"] = "r2"
        self.save_inputs()
        _, summary = self.invoke()
        self.assertEqual(summary["forecast_versions"], 2)
        self.assertEqual(summary["unique_primary_matches_by_protocol"], 1)
        self.assertEqual(summary["match_statuses"][0]["primary_forecast_id"], first_id)
        # Equal clock resolution still retains the first append using capture_sequence.
        self.result()
        _, summary = self.invoke()
        self.assertEqual(summary["groups"][0]["scored"], 1)

    def test_baseline_unavailable_not_fabricated_from_new_data(self):
        self.data["matches"][0]["home_goals"] = 9
        self.save_inputs()
        code, summary = self.invoke()
        self.assertEqual(code, 0)
        self.assertEqual(_objects(self.root / "prospective", "forecasts")[0]["baseline"]["status"], "unavailable")
        self.result()
        _, summary = self.invoke()
        self.assertEqual(summary["groups"][0]["common_sample"]["n"], 0)
        self.assertEqual(summary["groups"][0]["baseline_missing"], 1)

    def quote(self, forecast_id):
        return {"forecast_id": forecast_id, "competition": "epl", "match_id": 22,
            "home_id": 1, "away_id": 2, "kickoff": self.row["kickoff"],
            "contract": {"market": "total_goals", "side": "over", "line": 2.25},
            "observed_at": "2026-10-06T10:05:00Z", "quoted_at": "2026-10-06T10:04:00Z",
            "source": "test_fixture_only", "bookmaker": "synthetic", "price_status": "open",
            "conditions_status": "satisfied", "decimal_odds": 1.9, "cost_per_unit": 0.01}

    def test_quotes_freeze_conditions_fees_and_return_at_actual_line(self):
        self.invoke()
        forecast_id = _objects(self.root / "prospective", "forecasts")[0]["object_id"]
        quote = self.quote(forecast_id)
        code, summary = self.invoke(quotes=[quote])
        self.assertEqual(code, 0)
        self.assertEqual(summary["quote_simulation"]["pending_quotes"], 1)
        self.result(home=1, away=1)
        code, summary = self.invoke()
        self.assertEqual(code, 0)
        self.assertAlmostEqual(summary["quote_simulation"]["settlements"][0]["net_return"], -.51)
        self.assertEqual(summary["quote_simulation"]["unit_stakes"], 1)
        quote["decimal_odds"] = 3
        code, summary = self.invoke(quotes=[quote])
        self.assertEqual(code, 1)
        self.assertEqual(summary["quote_simulation"]["unit_stakes"], 1)

    def test_missing_quote_cost_or_conditions_is_not_available(self):
        self.invoke()
        forecast_id = _objects(self.root / "prospective", "forecasts")[0]["object_id"]
        quote = self.quote(forecast_id)
        del quote["cost_per_unit"]
        code, summary = self.invoke(quotes=[quote])
        self.assertEqual(code, 1)
        self.assertEqual(summary["quote_simulation"]["eligible_quotes"], 0)

    def test_path_traversal_symlinks_and_corruption_are_rejected(self):
        self.report["run_id"] = "../outside"
        self.write("reports/latest.json", self.report)
        with self.assertRaises(ValueError):
            self.invoke()
        self.report["run_id"] = "r1"
        self.save_inputs()
        self.invoke()
        frozen = list((self.root / "prospective/forecasts").glob("*.json"))[0]
        frozen.write_text("{}")
        with self.assertRaises(ValueError):
            self.invoke()



    def test_rescheduled_match_keeps_old_version_and_scores_first_matching_final_identity(self):
        self.invoke()
        first_id = _objects(self.root / "prospective", "forecasts")[0]["object_id"]
        self.report["run_id"] = "r2"
        self.row["kickoff"] = self.row["prediction"]["kickoff"] = "2026-10-06T18:00:00Z"
        self.clock = lambda: dt("2026-10-06T11:00:00Z")
        self.save_inputs()
        self.invoke()
        self.result(observed="2026-10-06T23:00:00Z")
        code, summary = self.invoke()
        self.assertEqual(code, 0)
        self.assertEqual(summary["forecast_versions"], 2)
        self.assertEqual(summary["groups"][0]["scored"], 1)
        self.assertEqual(summary["unique_primary_matches_by_protocol"], 1)
        self.assertNotEqual(summary["match_statuses"][0]["primary_forecast_id"], first_id)
        self.assertEqual(summary["identity_changed_forecasts"][0]["forecast_id"], first_id)

    def test_subsecond_timestamps_choose_true_first_forecast(self):
        self.invoke()
        first_id = _objects(self.root / "prospective", "forecasts")[0]["object_id"]
        self.clock = lambda: dt("2026-10-06T10:05:00.000001Z")
        self.report["run_id"] = "r2"
        self.save_inputs()
        _, summary = self.invoke()
        self.assertEqual(summary["match_statuses"][0]["primary_forecast_id"], first_id)

    def test_subsecond_result_revision_uses_actual_latest_and_never_regresses(self):
        self.invoke()
        self.result()
        self.invoke()
        self.result(home=0, away=2, observed="2026-10-06T21:00:00.000001Z")
        _, summary = self.invoke()
        current_id = summary["match_statuses"][0]["result_id"]
        result = next(r for r in _objects(self.root / "prospective", "results") if r["object_id"] == current_id)
        self.assertEqual(result["home_goals"], 0)
        self.assertEqual(result["away_goals"], 2)
        self.assertEqual(summary["result_versions"], 2)
        _, summary = self.invoke()
        self.assertEqual(summary["result_versions"], 2)

    def test_same_clock_first_quote_wins_and_duplicates_do_not_add_stakes(self):
        self.invoke()
        forecast_id = _objects(self.root / "prospective", "forecasts")[0]["object_id"]
        first = self.quote(forecast_id)
        second = {**first, "decimal_odds": 2.3}
        code, summary = self.invoke(quotes=[first, second, first])
        self.assertEqual(code, 0)
        self.assertEqual(len(_objects(self.root / "prospective", "quotes")), 2)
        self.result()
        _, summary = self.invoke()
        self.assertEqual(summary["quote_simulation"]["settlements"][0]["decimal_odds"], 1.9)
        self.assertEqual(summary["quote_simulation"]["unit_stakes"], 1)

    def test_zero_observed_probability_exposes_infinity_and_numeric_floor(self):
        result = _categorical_metrics([[1., 0.], [.5, .5]], [1, 0], ("yes", "no"))
        self.assertEqual(result["mathematical_log_loss_status"], "positive_infinity")
        self.assertIsNone(result["mathematical_log_loss"])
        self.assertEqual(result["observed_zero_probability_outcomes"], 1)
        self.assertEqual(result["log_loss_probability_floor"], 1e-15)
        self.assertTrue(result["log_loss"] > 10)

    def test_replay_root_and_public_asof_not_allowed(self):
        self.write("RESEARCH_REPLAY.json", {"research_replay": True})
        with self.assertRaises(ValueError):
            self.invoke()
        with self.assertRaises(SystemExit):
            main(["--as-of", "2020-01-01T00:00:00Z"])
