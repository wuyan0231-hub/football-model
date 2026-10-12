"""Temporal and denominator tests; synthetic success is not effect evidence."""
import copy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest

from football_model.feature_dataset import (FEATURE_NAMES, build_replay,
                                          coverage_report, feature_values)
from football_model.fundamentals import make_snapshot, save_snapshot
from football_model.model import ModelDataError


def matches(n=24):
    first = datetime(2024, 1, 1, 12, tzinfo=timezone.utc)
    pairs = ((1, 2), (3, 4), (1, 3), (2, 4), (1, 4), (2, 3))
    return [{"match_id": index + 1, "kickoff": (first + timedelta(days=index)).isoformat(),
             "home_id": pairs[index % 6][0], "away_id": pairs[index % 6][1],
             "home_name": f"T{pairs[index % 6][0]}", "away_name": f"T{pairs[index % 6][1]}",
             "home_goals": (index * 7 + 1) % 4, "away_goals": (index * 3) % 3}
            for index in range(n)]


def team_lineup(team):
    return {"team": {"id": team},
            "startXI": [{"player": {"id": team * 100 + i}} for i in range(11)],
            "substitutes": []}


class ReplayTests(unittest.TestCase):
    def test_monthly_fit_and_features_are_strictly_past_only(self):
        sample = matches()
        replay = build_replay(sample, start=sample[8]["kickoff"], end=sample[22]["kickoff"],
                              block_days=7, min_train=6)
        self.assertEqual(replay["coverage"]["requested_matches"], 15)
        self.assertEqual(len(replay["baseline_fits"]), 3)
        self.assertTrue(replay["coverage"]["denominator_preserved"])
        self.assertFalse(replay["prospective_qualified"])
        self.assertEqual(replay["schedule_scope"], "same_competition")
        for forecast in replay["forecasts"]:
            self.assertLessEqual(forecast["baseline_training_cutoff"], forecast["cutoff_at"])
            self.assertLess(forecast["cutoff_at"], forecast["kickoff"])
            self.assertFalse(forecast["baseline_prediction"]["prospective_qualified"])
            self.assertEqual(tuple(forecast["home_features"]), FEATURE_NAMES)
            for evidence in forecast["feature_evidence"].values():
                self.assertLessEqual(evidence["previous_result_assumed_available_at"], forecast["cutoff_at"])
            self.assertEqual(forecast["home_features"]["rest_days_capped"],
                             forecast["away_features"]["opponent_rest_days_capped"])
        for fitted in replay["baseline_fits"]:
            self.assertNotIn(sample[22]["match_id"], fitted["training_match_ids"])
        self.assertEqual(len(replay["correction_rows"]), 30)
        self.assertEqual(replay["rows"], replay["correction_rows"])
        for row in replay["correction_rows"]:
            self.assertEqual(set(row["feature_available_at"]), set(FEATURE_NAMES))
            self.assertTrue(all(t <= row["cutoff_at"] for t in row["feature_available_at"].values()))
            self.assertGreater(row["lambda0"], 0)
        json.dumps(replay, allow_nan=False)

    def test_future_score_lure_cannot_change_existing_predictions(self):
        sample = matches()
        options = dict(start=sample[8]["kickoff"], end=sample[15]["kickoff"], min_train=6)
        original = build_replay(sample, **options)
        lure = copy.deepcopy(sample[-1])
        lure.update(match_id=999, kickoff="2026-12-01T12:00:00Z", home_goals=99, away_goals=98)
        enlarged = build_replay(sample + [lure], **options)
        self.assertEqual(original["forecasts"], enlarged["forecasts"])
        self.assertEqual(original["baseline_fits"], enlarged["baseline_fits"])
        self.assertNotEqual(original["data_sha256"], enlarged["data_sha256"])

    def test_unavailable_result_and_unknown_rest_are_not_zero_imputed(self):
        history = matches(1)
        history[0].update(kickoff="2024-01-20T00:00:00Z")
        target = {"match_id": 99, "home_id": 1, "away_id": 3, "kickoff": "2024-01-20T12:00:00Z"}
        result = feature_values(target, history, "2024-01-20T02:00:00Z")
        self.assertIsNone(result["home"]["rest_days_capped"])
        self.assertIsNone(result["home"]["opponent_rest_days_capped"])
        self.assertEqual(result["home"]["matches_last14d"], 0)
        ready = feature_values(target, history, "2024-01-20T04:00:00Z")
        self.assertAlmostEqual(ready["home"]["rest_days_capped"], 0.5)
        self.assertIsNone(ready["away"]["rest_days_capped"])
        self.assertEqual(ready["home"]["matches_last14d"], 1)

    def test_warmup_and_unseen_teams_remain_in_requested_denominator(self):
        short = build_replay(matches(3))
        self.assertEqual(short["coverage"]["baseline_failures"], 3)
        self.assertEqual(short["coverage"]["failure_codes"]["insufficient_training_history"], 3)
        sample = matches(12)
        sample[-1].update(away_id=99, away_name="New team")
        replay = build_replay(sample, start=sample[-1]["kickoff"], min_train=6)
        self.assertEqual(replay["coverage"]["requested_matches"], 1)
        self.assertEqual(replay["coverage"]["baseline_failures"], 1)
        self.assertEqual(replay["failures"][0]["code"], "unseen_team")
        self.assertEqual(replay["correction_rows"], [])

    def test_workload_with_no_regulation_target_affects_features_only(self):
        sample = matches(12)
        event = {"match_id": 500, "home_id": sample[-1]["home_id"], "away_id": 98,
                 "kickoff": "2024-01-10T12:00:00Z", "status": "AET"}
        options = dict(start=sample[-1]["kickoff"], min_train=6)
        normal = build_replay(sample, **options)
        mixed = build_replay(sample, schedule_events=[event], **options)
        self.assertEqual(normal["baseline_fits"], mixed["baseline_fits"])
        self.assertNotIn(500, mixed["baseline_fits"][0]["training_match_ids"])
        forecast = mixed["forecasts"][0]
        self.assertEqual(forecast["home_features"]["matches_last14d"],
                         normal["forecasts"][0]["home_features"]["matches_last14d"] + 1)
        self.assertEqual(forecast["feature_evidence"][str(event["home_id"])]["previous_match_id"], 500)
        self.assertEqual(mixed["all_competition_completeness"], "unknown")
        with self.assertRaises(ModelDataError):
            build_replay(sample, schedule_events=[{**event, "status": "NS"}], **options)

    def test_conflicting_schedule_identity_is_rejected(self):
        sample = matches(12)
        conflict = copy.deepcopy(sample[0])
        conflict["home_goals"] = 30
        with self.assertRaises(ModelDataError):
            build_replay(sample, schedule_matches=[conflict], min_train=6)
        with self.assertRaises(ModelDataError):
            build_replay(sample, start="2024-01-20T00:00:00")


class CoverageTests(unittest.TestCase):
    def test_local_coverage_distinguishes_zero_null_and_historical_backfill(self):
        sample = matches(2)
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory)
            (runtime / "data").mkdir()
            (runtime / "data" / "serie_a.json").write_text(json.dumps({"matches": sample,
                "requested_seasons": [2024], "missing_seasons": [], "observed_at": "2024-02-01T00:00:00Z"}))
            def save(kind, match, rows, at):
                fixture = {**match, "status": "FT"}
                endpoint = {"statistics": "/fixtures/statistics", "lineups": "/fixtures/lineups", "injuries": "/injuries"}[kind]
                envelope = {"endpoint": endpoint, "params": {"fixture": match["match_id"]},
                            "response": rows, "errors": [], "fetched_at": at}
                save_snapshot(runtime / "fundamentals" / "snapshots", make_snapshot(kind, envelope, fixture))
            save("statistics", sample[0], [{"team": {"id": tid},
                "statistics": [{"type": "expected_goals", "value": "0.00"}]} for tid in (1, 2)], "2024-02-01T00:00:00Z")
            save("statistics", sample[1], [{"team": {"id": tid},
                "statistics": [{"type": "expected_goals", "value": None}]} for tid in (3, 4)], "2024-02-01T00:00:00Z")
            save("lineups", sample[0], [team_lineup(1), team_lineup(2)], "2024-01-01T01:00:00Z")
            save("lineups", sample[1], [team_lineup(3), team_lineup(4)], "2024-02-01T00:00:00Z")
            save("injuries", sample[0], [], "2024-02-01T00:00:00Z")
            result = coverage_report(runtime, "serie_a")
            self.assertEqual(result["history"]["matches"], 2)
            self.assertFalse(result["supplier_full_coverage_certified"])
            self.assertIsNone(result["official_season_denominator"])
            self.assertEqual(result["fixture_cache"]["season_unknown_matches"], 2)
            self.assertEqual(result["fundamentals"]["statistics"]["both_sides_qualified"], 1)
            self.assertEqual(result["fundamentals"]["statistics"]["qualified_observed_by_T_minus_10h"], 0)
            self.assertEqual(result["fundamentals"]["lineups"]["both_sides_qualified"], 2)
            self.assertEqual(result["fundamentals"]["lineups"]["qualified_observed_by_T_minus_10h"], 1)
            self.assertEqual(result["fundamentals"]["injuries"]["complete_roster_verified"], 0)
            self.assertFalse(result["fundamentals"]["injuries"]["empty_means_healthy"])
            self.assertEqual(result["issues"], [])

    def test_source_conflicts_are_visible_and_never_shrink_score_denominator(self):
        sample = matches(1)
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory)
            (runtime / "data").mkdir()
            (runtime / "data" / "serie_a.json").write_text(json.dumps({"matches": sample}))
            cache = runtime / "cache" / "api_cache"
            cache.mkdir(parents=True)
            raw = {"fixture": {"id": 1, "date": sample[0]["kickoff"], "status": {"short": "FT"}},
                   "teams": {"home": {"id": 1}, "away": {"id": 9}}, "league": {"season": 2024}}
            (cache / "wrong.json").write_text(json.dumps({"endpoint": "/fixtures", "params": {"id": 1},
                "response": [raw], "fetched_at": "2024-02-01T00:00:00Z"}))
            result = coverage_report(runtime, "serie_a")
            self.assertEqual(result["history"]["matches"], 1)
            self.assertEqual(result["fixture_cache"]["matching_match_ids"], 0)
            self.assertEqual(result["issues"][0]["reason"], "invalid_cache_evidence")
            self.assertEqual(result["fundamentals"]["statistics"]["local_denominator"], 1)


if __name__ == "__main__":
    unittest.main()
