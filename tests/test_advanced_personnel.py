import copy
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from football_model.advanced_collection import _save
from football_model.advanced_features import (build_feature_rows, feature_values,
                                              load_sources, make_players_snapshot)
from football_model.advanced_personnel import load_personnel_sources, personnel_features
from football_model.data import DataError
from football_model.fundamentals import make_snapshot, save_snapshot


def fixture(mid, day, status="FT"):
    return {"match_id": mid, "kickoff": f"2026-01-{day:02d}T12:00:00Z", "home_id": 1, "away_id": 2, "status": status}


class AdvancedPersonnelTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.history = [fixture(i, i * 2) for i in range(1, 5)]
        self.target = fixture(9, 12, "NS")
        self.cutoff = "2026-01-12T02:00:00Z"
        (self.root / "data").mkdir()
        (self.root / "data" / "x.json").write_text(json.dumps({"matches": self.history, "observed_at": "2026-01-09T00:00:00Z"}))
        for row in self.history:
            for kind in ("players", "lineups"):
                response = []
                for tid in (1, 2):
                    people = [tid * 100 + p for p in range(1, 12)]
                    if kind == "players":
                        data = {"players": [{"player": {"id": pid}, "statistics": [{"games": {"minutes": 90, "rating": "7.0"}}]} for pid in people]}
                    else:
                        data = {"startXI": [{"player": {"id": pid}} for pid in people], "substitutes": []}
                    response.append({"team": {"id": tid}, **data})
                env = {"endpoint": "/fixtures/" + kind, "params": {"fixture": row["match_id"]}, "response": response,
                       "fetched_at": row["kickoff"].replace("12:00", "18:00")}
                if kind == "players":
                    _save(self.root, Path("advanced_features/snapshots/players") / str(row["match_id"]), make_players_snapshot(env, row))
                else:
                    save_snapshot(self.root / "fundamentals/snapshots", make_snapshot(kind, env, row))
        self.injuries()

    def injuries(self, *, rows=None, observed="2026-01-12T01:00:00Z", coverage=True, status="NS"):
        if rows is None:
            rows = [{"team": {"id": tid}, "fixture": {"id": 9},
                     "player": {"id": tid * 100 + 1, "type": "Missing Fixture", "reason": "Knee Injury"}} for tid in (1, 2)]
        target = {**self.target, "status": status}
        snap = make_snapshot("injuries", {"endpoint": "/injuries", "params": {"fixture": 9}, "response": rows, "fetched_at": observed}, target)
        path = save_snapshot(self.root / "fundamentals/snapshots", snap)
        raw = {"fixture": {"id": 9, "date": target["kickoff"], "status": {"short": status}},
               "teams": {"home": {"id": 1}, "away": {"id": 2}}, "league": {"id": 39, "season": 2025}}
        env = {"endpoint": "/fixtures", "params": {"id": 9}, "response": [raw], "fetched_at": observed}
        _save(self.root, Path("fundamentals/fixtures/9"), {"fixture_response": env, "coverage": {"injuries": coverage},
              "coverage_observed_at": observed, "league_id": 39, "season": 2025})
        return path

    def clear_injuries(self):
        for directory in ("fundamentals/snapshots/injuries", "fundamentals/fixtures"):
            for path in (self.root / directory).glob("*/*.json"):
                path.unlink()

    def values(self, **kwargs):
        return personnel_features(self.root, "x", self.target, self.cutoff, **kwargs)

    def test_real_early_absence_uses_past_usage_and_retains_source_closure(self):
        value = self.values()
        self.assertTrue(value["groups"]["personnel_early"])
        self.assertAlmostEqual(value["home"]["reported_absence_recent_minutes_share"], 1 / 11)
        self.assertAlmostEqual(value["home"]["reported_absence_previous_xi_share"], 1 / 11)
        self.assertEqual(value["evidence"]["1"]["reported_absent_recent_rating"]["101"], 7)
        self.assertFalse(value["evidence"]["1"]["complete_roster_status"])
        self.assertTrue(any("fundamentals/fixtures/9/" in ref["path"] for ref in value["source_refs"]))
        self.assertTrue(any("snapshots/injuries/9/" in ref["path"] for ref in value["source_refs"]))
        self.assertEqual(value["probability_adjustment"], "none_here_requires_past_fitted_offset_coefficients")

    def test_later_historical_injury_download_never_enters_training(self):
        self.clear_injuries()
        self.injuries(observed="2026-01-13T01:00:00Z", status="FT")
        for mode in ("historical", "prospective"):
            value = self.values(mode=mode)
            self.assertFalse(value["groups"]["personnel_early"])
            self.assertEqual(value["missing"][0]["reason"], "no_actual_prematch_injury_snapshot")
            self.assertFalse(value["historical_current_injuries_assumed_available"])

    def test_empty_response_is_not_a_healthy_roster_or_zero_feature(self):
        self.clear_injuries()
        self.injuries(rows=[])
        value = self.values()
        self.assertFalse(value["groups"]["personnel_early"])
        self.assertIsNone(value["home"]["reported_absence_recent_minutes_share"])
        self.assertEqual(value["missing"][0]["reason"], "empty_injury_report_not_verified_healthy")

    def test_unknown_coverage_blocks_and_stale_snapshot_does_not_refresh_by_reading(self):
        self.clear_injuries()
        self.injuries(coverage=None)
        self.assertEqual(self.values()["missing"][0]["reason"], "injury_coverage_not_confirmed")
        self.clear_injuries()
        self.injuries(observed="2026-01-11T12:00:00Z")
        self.assertEqual(self.values()["missing"][0]["reason"], "injury_snapshot_stale")

    def test_questionable_is_not_confirmed_absent(self):
        self.clear_injuries()
        self.injuries(rows=[{"team": {"id": 1}, "fixture": {"id": 9},
                            "player": {"id": 101, "type": "Questionable", "reason": "Knock"}}])
        value = self.values()
        self.assertIsNone(value["home"]["reported_absence_previous_xi_share"])
        self.assertEqual(value["evidence"]["1"]["unresolved_player_ids"], [101])

    def test_long_absence_has_observed_zero_recent_usage_without_manual_penalty(self):
        self.clear_injuries()
        self.injuries(rows=[{"team": {"id": tid}, "fixture": {"id": 9},
                            "player": {"id": tid * 100 + 99, "type": "Missing Fixture", "reason": "Injury"}} for tid in (1, 2)])
        value = self.values()
        self.assertTrue(value["groups"]["personnel_early"])
        self.assertEqual(value["home"]["reported_absence_recent_minutes_share"], 0)
        self.assertEqual(value["home"]["reported_absence_previous_xi_share"], 0)

    def test_missing_latest_minutes_never_skips_game_to_make_absence_zero(self):
        for path in (self.root / "advanced_features/snapshots/players/4").glob("*.json"):
            path.unlink()
        value = self.values()
        self.assertFalse(value["groups"]["personnel_early"])
        self.assertEqual(value["evidence"]["1"]["incomplete_minutes_match_ids"], [4])
        self.assertEqual(value["missing"][0]["reason"], "recent_player_minutes_incomplete")

    def test_tampered_latest_injury_does_not_silently_use_old_snapshot(self):
        bad = self.injuries(observed="2026-01-12T01:30:00Z")
        value = json.loads(bad.read_text())
        value["teams"]["1"]["records"][0]["player_id"] = 999
        bad.write_text(json.dumps(value))
        self.assertEqual(self.values()["missing"][0]["reason"], "personnel_source_invalid")

    def test_preloaded_sources_do_not_rescan_and_lineup_horizon_avoids_double_counting(self):
        sources = load_sources(self.root, "x")
        sources["personnel_sources"] = load_personnel_sources(self.root)
        with patch("football_model.advanced_personnel.load_personnel_sources", side_effect=AssertionError("rescanned")):
            self.assertTrue(self.values(sources=sources)["groups"]["personnel_early"])
        value = self.values(horizon="LINEUP", sources=sources)
        self.assertFalse(value["groups"]["personnel_early"])
        self.assertEqual(value["missing"][0]["reason"], "not_applicable_confirmed_lineup_uses_actual_replacements")

    def test_future_history_index_and_postkickoff_cutoff_rejected(self):
        with self.assertRaisesRegex(DataError, "not_pre_match"):
            personnel_features(self.root, "x", self.target, self.target["kickoff"])
        path = self.root / "data/x.json"
        value = json.loads(path.read_text())
        value["observed_at"] = "2026-01-13T00:00:00Z"
        path.write_text(json.dumps(value))
        with self.assertRaisesRegex(DataError, "history_not_observed"):
            self.values()

    def test_same_time_conflicting_reports_rejected_with_both_originals(self):
        self.injuries(rows=[{"team": {"id": tid}, "fixture": {"id": 9},
                            "player": {"id": tid * 100 + 2, "type": "Missing Fixture", "reason": "Injury"}} for tid in (1, 2)])
        value = self.values()
        self.assertEqual(value["missing"][0]["reason"], "ambiguous_current_injury_snapshot")
        self.assertEqual(sum("snapshots/injuries/9/" in ref["path"] for ref in value["source_refs"]), 2)

    def test_live_feature_pipeline_merges_personnel_and_rebuilds_frozen_closure(self):
        value = feature_values(self.root, "x", self.target, self.cutoff)
        self.assertTrue(value["groups"]["personnel_early"])
        self.assertAlmostEqual(value["home"]["reported_absence_recent_minutes_share"], 1 / 11)
        with tempfile.TemporaryDirectory() as directory:
            frozen = Path(directory)
            for ref in value["source_refs"]:
                target = frozen / ref["path"]
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(self.root / ref["path"], target)
            rebuilt = feature_values(frozen, "x", self.target, self.cutoff)
            for name in ("home", "away", "groups", "missing"):
                self.assertEqual(rebuilt[name], value[name])

    def test_historical_pipeline_counts_only_real_archived_prematch_injuries(self):
        saved = self.history + [{**self.target, "status": "FT"}]
        path = self.root / "data/x.json"
        path.write_text(json.dumps({"matches": saved, "observed_at": "2026-01-13T00:00:00Z"}))
        result = build_feature_rows(self.root, "x", matches=saved, horizon_hours=10)
        target = next(row for row in result["rows"] if row["match_id"] == 9)
        self.assertTrue(target["groups"]["personnel_early"])
        self.assertEqual(result["coverage"]["complete_groups"]["personnel_early"], 1)
        self.assertFalse(target["prospective_qualified"])
        self.clear_injuries()
        self.injuries(observed="2026-01-13T01:00:00Z", status="FT")
        again = build_feature_rows(self.root, "x", matches=saved, horizon_hours=10)
        self.assertEqual(again["coverage"]["complete_groups"].get("personnel_early", 0), 0)
        self.assertEqual(again["coverage"]["requested"], len(saved))

    def test_invalid_current_personnel_original_remains_invalid_after_freeze(self):
        bad = self.injuries(observed="2026-01-12T01:30:00Z")
        bad.write_text('{}')
        value = feature_values(self.root, "x", self.target, self.cutoff)
        with tempfile.TemporaryDirectory() as directory:
            frozen = Path(directory)
            for ref in value["source_refs"]:
                target = frozen / ref["path"]
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(self.root / ref["path"], target)
            rebuilt = feature_values(frozen, "x", self.target, self.cutoff)
            self.assertEqual(rebuilt["missing"], value["missing"])
            self.assertFalse(rebuilt["groups"]["personnel_early"])
