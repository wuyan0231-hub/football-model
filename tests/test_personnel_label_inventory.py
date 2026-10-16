"""Independent historical label inventory: time and missingness boundaries."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from scripts.personnel_label_inventory import inventory, materialize, run


def data():
    history = [{"match_id": i, "home_id": 1, "away_id": 2,
        "kickoff": f"2026-01-{day:02d}T12:00:00Z", "home_goals": 1, "away_goals": 0}
        for i, day in ((1, 1), (2, 8), (3, 15))]
    sources = {"snapshots": {}, "known_times": {}, "source_issues": []}
    for row in history:
        mid = row["match_id"]
        teams = {str(tid): {"complete_starting_eleven": True,
            "starters": [{"player_id": tid * 100 + n} for n in range(1, 12)],
            "substitutes": [{"player_id": tid * 100 + 12}, {"player_id": None}]}
            for tid in (1, 2)}
        if mid == 3:
            teams["1"]["starters"][-1] = {"player_id": 999}
        snap = {"fixture": {**{k: row[k] for k in ("match_id", "home_id", "away_id", "kickoff")}, "status": "FT"},
            "teams": teams, "known_at": "2026-02-01T00:00:00Z", "snapshot_id": str(mid)}
        ref = {"path": f"lineups/{mid}.json", "sha256": "a" * 64}
        sources["snapshots"][("lineups", mid)] = [(snap, ref)]
        players = copy.deepcopy(snap)
        players["teams"] = {str(tid): {"complete_minutes": False,
            "players": [{"player_id": tid * 100 + 12, "minutes": None, "rating": None},
                        {"player_id": tid * 100 + 13, "minutes": 0, "rating": None},
                        {"player_id": tid * 100 + 14, "minutes": 5, "rating": 6.0}]}
            for tid in (1, 2)}
        players["raw_response"] = [{"team": {"id": tid}, "players": [
            {"player": {"id": p["player_id"]}, "statistics": [{"games": {
                "minutes": p["minutes"], "rating": p["rating"]}}]}
            for p in players["teams"][str(tid)]["players"]]} for tid in (1, 2)]
        sources["snapshots"][("players", mid)] = [(players, {"path": f"players/{mid}.json", "sha256": "b" * 64})]
    return history, sources


class PersonnelLabelInventoryTests(unittest.TestCase):
    def audit(self, history=None, sources=None, **kwargs):
        h, s = data()
        return inventory(h if history is None else history, s if sources is None else sources,
            history_observed_at=kwargs.pop("history_observed_at", "2026-02-01T00:00:00Z"), **kwargs)

    def test_past_only_candidate_pool_preserves_new_starter_outside_and_denominator(self):
        result = self.audit()
        self.assertEqual(result["summary"]["requested_matches"], 3)
        self.assertTrue(result["summary"]["denominator_preserved"])
        row = result["matches"][-1]["teams"]["1"]
        historical = row["historical_development_assumption"]
        self.assertNotIn(999, historical["candidate_ids"])
        self.assertEqual(historical["outside_pool_starting_ids"], [999])
        self.assertEqual(len(historical["positive_ids"]), 10)
        self.assertTrue(set(historical["positive_ids"]).isdisjoint(historical["negative_ids"]))
        self.assertEqual(set(historical["positive_ids"]) | set(historical["negative_ids"]), set(historical["candidate_ids"]))
        self.assertEqual(row["transfer_status"], "unknown_not_inferred_from_absence_or_pool_membership")

    def test_null_and_explicit_zero_minutes_remain_distinct_and_neither_proves_appearance(self):
        result = self.audit()
        row = result["matches"][1]["teams"]["1"]
        stats = row["target_player_statistics"]
        self.assertEqual(stats["null_minutes_count"], 1)
        self.assertEqual(stats["reported_zero_minutes_count"], 1)
        self.assertFalse(stats["complete_reported_minutes"])
        self.assertIsNone(stats["raw_player_rows"][0]["reported_minutes"])
        self.assertEqual(stats["raw_player_rows"][1]["reported_minutes"], 0)
        self.assertTrue(all(p["inferred_minutes"] is None for p in stats["raw_player_rows"]))
        candidates = row["historical_development_assumption"]["candidate_ids"]
        self.assertIn(112, candidates)  # Explicit past bench membership only.
        self.assertNotIn(113, candidates)  # Zero without a lineup/appearance.
        self.assertIn(114, candidates)  # Positive past provider minutes.

    def test_late_downloads_do_not_become_historical_point_in_time_inputs(self):
        result = self.audit()
        self.assertEqual(result["summary"]["matches_both_actual_cutoff_labels_available"], 0)
        self.assertEqual(result["summary"]["matches_both_historical_labels_available"], 2)
        row = result["matches"][-1]["teams"]["1"]
        self.assertTrue(row["target_xi_legal"])
        self.assertFalse(row["actual_observed_by_cutoff"]["history_observed_by_cutoff"])
        self.assertEqual(row["actual_observed_by_cutoff"]["candidate_ids"], [])
        self.assertFalse(row["historical_development_assumption"]["historical_point_in_time_confirmed"])

    def test_actually_early_prior_originals_allow_strict_pool_but_target_label_can_be_late(self):
        h, s = data()
        for kind in ("lineups", "players"):
            for snap, ref in s["snapshots"][(kind, 1)]:
                snap["known_at"] = "2026-01-01T18:00:00Z"
                s["known_times"][ref["path"]] = snap["known_at"]
        result = self.audit(h, s, history_observed_at="2025-12-31T00:00:00Z")
        row = result["matches"][1]["teams"]["1"]
        self.assertTrue(row["actual_observed_by_cutoff"]["label_rows_available"])
        self.assertGreater(row["label_effective_known_at"], result["matches"][1]["prediction_cutoff_at"])
        self.assertTrue(row["label_may_be_postmatch"])

    def test_missing_latest_history_game_is_not_silently_replaced_with_older_game(self):
        h, s = data()
        s["snapshots"].pop(("lineups", 2))
        s["snapshots"].pop(("players", 2))
        result = self.audit(h, s, window_matches=1)
        row = result["matches"][-1]["teams"]["1"]
        self.assertEqual(row["prior_match_ids"], [2])
        self.assertEqual(row["historical_development_assumption"]["candidate_ids"], [])
        self.assertFalse(row["historical_development_assumption"]["label_rows_available"])
        self.assertEqual(result["summary"]["retained_matches"], 3)

    def test_strict_cutoff_selects_saved_older_version_before_future_revision(self):
        h, s = data()
        snap, ref = s["snapshots"][("lineups", 1)][0]
        old = copy.deepcopy(snap)
        old["known_at"] = "2026-01-01T18:00:00Z"
        old_ref = {**ref, "path": "lineups/old-saved.json"}
        s["snapshots"][("lineups", 1)].append((old, old_ref))
        snap["teams"]["1"]["starters"][0]["player_id"] = 777
        result = self.audit(h, s, history_observed_at="2025-12-31T00:00:00Z")
        row = result["matches"][1]["teams"]["1"]
        self.assertIn(777, row["historical_development_assumption"]["candidate_ids"])
        self.assertNotIn(777, row["actual_observed_by_cutoff"]["candidate_ids"])
        self.assertIn(101, row["actual_observed_by_cutoff"]["candidate_ids"])

    def test_cross_team_duplicate_starter_rejects_both_labels(self):
        h, s = data()
        s["snapshots"][("lineups", 3)][0][0]["teams"]["2"]["starters"][0]["player_id"] = 101
        result = self.audit(h, s)
        self.assertFalse(result["matches"][-1]["both_teams_legal_xi"])
        self.assertTrue(all(not r["target_xi_legal"] for r in result["matches"][-1]["teams"].values()))

    def test_past_cross_team_bench_or_player_id_conflict_cannot_enter_candidates(self):
        h, s = data()
        lineup = s["snapshots"][("lineups", 1)][0][0]
        lineup["teams"]["2"]["substitutes"].append({"player_id": 101})
        player = s["snapshots"][("players", 1)][0][0]
        player["teams"]["2"]["players"].append({"player_id": 114, "minutes": None, "rating": None})
        result = self.audit(h, s, window_matches=1)
        row = result["matches"][1]["teams"]["1"]
        candidates = row["historical_development_assumption"]["candidate_ids"]
        self.assertNotIn(101, candidates)
        self.assertNotIn(114, candidates)
        self.assertTrue(any(r["reason"] == "cross_team_player_identity_conflict_excluded" for r in row["missing_prior"]))
        self.assertFalse(result["matches"][0]["both_teams_legal_xi"])

    def test_equal_knowledge_time_conflicting_snapshots_are_not_hash_cherry_picked(self):
        h, s = data()
        snap, ref = s["snapshots"][("lineups", 2)][0]
        other = copy.deepcopy(snap)
        other["teams"]["1"]["starters"][0]["player_id"] = 500
        s["snapshots"][("lineups", 2)].append((other, {**ref, "path": "lineups/conflict.json"}))
        result = self.audit(h, s)
        self.assertFalse(result["matches"][1]["both_teams_legal_xi"])
        self.assertIn("conflicting_observations_at_same_knowledge_time", result["summary"]["missing_observation_reasons"])

    def test_ref_hash_mismatch_and_missing_bytes_are_not_materialized(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source = base / "source"
            source.mkdir()
            (source / "observed.json").write_text('{}\n')
            report = {"source_refs": [
                {"original_path": "data/epl.json", "path": "observed.json", "sha256": "0" * 64},
                {"original_path": "advanced_features/contexts/missing.json", "path": "missing.json", "sha256": "1" * 64}]}
            manifest = materialize(report, source, base / "out")
            self.assertEqual([r["status"] for r in manifest], ["source_hash_mismatch", "missing_local_original"])
            self.assertFalse((base / "out/data/epl.json").exists())
            report["source_refs"][0]["original_path"] = "../escape.json"
            with self.assertRaises(ValueError):
                materialize(report, source, base / "out")

    def test_symlink_routes_cannot_read_or_write_outside_declared_roots(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source = base / "source"
            source.mkdir()
            outside = base / "outside.json"
            outside.write_bytes(b'{}\n')
            sha = hashlib.sha256(outside.read_bytes()).hexdigest()
            (source / "escape.json").symlink_to(outside)
            report = {"source_refs": [{"original_path": "data/epl.json", "path": "escape.json", "sha256": sha}]}
            with self.assertRaisesRegex(ValueError, "source_symlink_escapes"):
                materialize(report, source, base / "out")
            (source / "safe.json").write_bytes(outside.read_bytes())
            output = base / "out"
            output.mkdir()
            (output / "data").symlink_to(source, target_is_directory=True)
            report["source_refs"][0]["path"] = "safe.json"
            with self.assertRaisesRegex(ValueError, "destination_symlink_escapes"):
                materialize(report, source, output)
            with self.assertRaisesRegex(ValueError, "separate"):
                materialize(report, source, base)

    def test_league_traversal_is_rejected_before_reading_or_creating_output(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "untouched"
            for league in ("../escape", "/absolute", "epl/else", "", "EPL"):
                with self.assertRaisesRegex(ValueError, "invalid_league_key"):
                    run("nonexistent-report", "nonexistent-runtime", output, "a" * 40, league=league)
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
