import copy
import json
from pathlib import Path
import tempfile
import unittest

from football_model.data import DataError
from football_model.f4_data_quality import (
    audit_saved, inspect_fixture, minute_record, summarize_samples, training_recency,
)

OBSERVED = "2026-10-06T12:00:00Z"


def fixture():
    return {"fixture": {"id": 1, "date": "2026-10-05T12:00:00Z", "status": {"short": "FT"}},
        "league": {"id": 71, "season": 2026}, "teams": {"home": {"id": 10}, "away": {"id": 20}},
        "lineups": [{"team": {"id": 10}, "startXI": [{"player": {"id": p}} for p in range(1, 12)],
                     "substitutes": [{"player": {"id": None}}]}],
        "statistics": [{"team": {"id": 10}, "statistics": [{"type": "expected_goals", "value": 0}]}],
        "players": [{"team": {"id": 10}, "players": [
            {"player": {"id": 1}, "statistics": [{"games": {"minutes": None}}]},
            {"player": {"id": 15}, "statistics": [{"games": {"minutes": 0}}]},
            {"player": {"id": 0}, "statistics": [{"games": {"minutes": 55}}]}]}],
        "events": []}


class MinuteSemanticsTests(unittest.TestCase):
    def test_null_zero_absent_are_distinct(self):
        rows = [minute_record(None, final_status="FT"), minute_record(0, final_status="FT"),
                minute_record(None, minute_present=False, final_status="FT")]
        self.assertEqual([r["minute_state"] for r in rows], ["null", "reported_zero", "absent"])
        self.assertTrue(all(r["reported_minutes_candidate"] is None for r in rows))
        self.assertTrue(all(not r["verified_nonappearance"] for r in rows))

    def test_starter_null_is_appearance_with_unknown_minutes(self):
        row = minute_record(None, started=True, final_status="FT")
        self.assertEqual(row["appearance_status"], "appeared_supported")
        self.assertIsNone(row["reported_minutes_candidate"])

    def test_zero_substitute_not_repaired_or_called_nonparticipant(self):
        row = minute_record(0, substitution_event=True, final_status="FT")
        self.assertEqual(row["appearance_status"], "appeared_supported")
        self.assertEqual(row["raw_minutes"], 0)
        self.assertFalse(row["verified_nonappearance"])

    def test_future_lineup_does_not_prove_participation(self):
        row = minute_record(90, started=True, final_status="NS")
        self.assertEqual(row["appearance_status"], "unknown")
        self.assertIsNone(row["reported_minutes_candidate"])

    def test_aet_and_over_90_values_not_clipped(self):
        for status, minutes in (("AET", 120), ("PEN", 90), ("FT", 97)):
            row = minute_record(minutes, final_status=status)
            self.assertEqual(row["raw_minutes"], minutes)
            self.assertIsNone(row["reported_minutes_candidate"])

    def test_invalid_types_and_identity_do_not_gain_features(self):
        for value in (True, "90", -1, 0.5):
            row = minute_record(value, final_status="FT")
            self.assertEqual(row["minute_state"], "invalid")
            self.assertIsNone(row["reported_minutes_candidate"])
        self.assertIsNone(minute_record(90, final_status="FT", identified=False)["reported_minutes_candidate"])


class SavedSampleQualityTests(unittest.TestCase):
    def test_bad_statistic_identity_preserves_starters_xg_and_unknown_bench(self):
        raw = fixture()
        original = copy.deepcopy(raw)
        report = inspect_fixture(raw, OBSERVED, {})
        self.assertEqual(raw, original)
        team = report["teams"][0]
        self.assertTrue(team["fields"]["starting_eleven"]["candidate_usable"])
        self.assertEqual(team["unknown_substitute_count"], 1)
        self.assertTrue(team["fields"]["xg"]["candidate_usable"])
        unknown = report["players"][-1]
        self.assertEqual(unknown["raw_player_id"], 0)
        self.assertIsNone(unknown["player_id"])
        self.assertIsNone(unknown["reported_minutes_candidate"])

    def test_only_null_player_shell_never_counts_as_usable_minutes(self):
        raw = fixture()
        raw["players"][0]["players"] = [{"player": {"id": None}, "statistics": [{"games": {"minutes": None}}]}]
        report = inspect_fixture(raw, OBSERVED, {})
        fields = report["teams"][0]["fields"]
        self.assertTrue(fields["player_statistics"]["present"])
        self.assertFalse(fields["player_statistics"]["candidate_usable"])
        self.assertFalse(fields["player_minutes"]["candidate_usable"])
        self.assertFalse(fields["player_identity"]["present"])

    def test_invalid_player_module_keeps_fixture_denominator_and_other_fields(self):
        for blocks in (None, [{"team": {"id": 999}, "players": []}],
                       [{"team": {"id": 10}, "players": None}],
                       [{"team": {"id": 10}, "players": [None, {}]}]):
            raw = fixture()
            raw["players"] = blocks
            with self.subTest(blocks=blocks):
                report = inspect_fixture(raw, OBSERVED, {})
                self.assertTrue(report["teams"][0]["fields"]["xg"]["candidate_usable"])
                self.assertTrue(report["teams"][0]["fields"]["starting_eleven"]["candidate_usable"])
                self.assertEqual(summarize_samples([report], as_of=OBSERVED)["selected_fixtures"], 1)

    def test_duplicate_team_player_blocks_quarantined_without_arbitrary_selection(self):
        raw = fixture()
        raw["players"] += copy.deepcopy(raw["players"])
        report = inspect_fixture(raw, OBSERVED, {})
        self.assertEqual(report["players"], [])
        self.assertTrue(report["teams"][0]["fields"]["starting_eleven"]["candidate_usable"])

    def test_malformed_events_do_not_block_xg_lineups_or_minutes(self):
        for events in (None, [None, {}], [{"type": "subst", "team": {"id": 10}, "player": None, "assist": {"id": []}}]):
            raw = fixture()
            raw["events"] = events
            report = inspect_fixture(raw, OBSERVED, {})
            self.assertEqual(len(report["players"]), 3)
            self.assertTrue(report["teams"][0]["fields"]["xg"]["candidate_usable"])

    def test_duplicates_quarantine_both_player_values(self):
        raw = fixture()
        raw["players"][0]["players"] = [{"player": {"id": 1}, "statistics": [{"games": {"minutes": 45}}]}]*2
        report = inspect_fixture(raw, OBSERVED, {})
        self.assertTrue(all(p["minute_feature_exclusion"] == "duplicate_player_statistic_identity" for p in report["players"]))

    def test_invalid_xg_isolates_statistics_only(self):
        raw = fixture()
        raw["statistics"][0]["statistics"][0]["value"] = -1
        report = inspect_fixture(raw, OBSERVED, {})
        self.assertTrue(report["teams"][0]["fields"]["starting_eleven"]["present"])
        self.assertFalse(report["teams"][0]["fields"]["xg"]["candidate_usable"])

    def test_existing_shot_diagnostic_is_reused(self):
        raw = fixture()
        raw["statistics"][0]["statistics"] += [{"type": n, "value": v} for n, v in
            (("Total Shots", 16), ("Shots insidebox", 9), ("Shots outsidebox", 6))]
        report = inspect_fixture(raw, OBSERVED, {})
        self.assertTrue(any(q["code"] == "shot_statistics_inconsistent" for q in report["quality_findings"]))
        self.assertEqual(report["teams"][0]["xg_value"], 0)

    def test_duplicate_versions_not_independent_fixtures(self):
        first = inspect_fixture(fixture(), OBSERVED, {"path": "one"})
        second = inspect_fixture(fixture(), "2026-10-07T12:00:00Z", {"path": "two"})
        summary = summarize_samples([first, second], as_of="2026-10-08T12:00:00Z")
        self.assertEqual(summary["selected_fixtures"], 1)
        self.assertEqual(summary["field_coverage"][0]["fields"]["xg"]["observed_team_fixtures"], 2)
        self.assertFalse(summary["continuous_league_coverage_verified"])

    def test_late_fetch_excluded_from_old_cutoff(self):
        record = inspect_fixture(fixture(), OBSERVED, {})
        result = summarize_samples([record], as_of="2026-10-06T10:00:00Z")
        self.assertEqual(result["selected_fixtures"], 0)

    def test_same_time_conflict_rejected(self):
        a = inspect_fixture(fixture(), OBSERVED, {})
        b = copy.deepcopy(a)
        b["teams"][0]["xg_value"] = 1.0
        with self.assertRaises(DataError):
            summarize_samples([a, b], as_of=OBSERVED)

    def test_body_address_integrity_checked(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/"fixtures"/"1"/("a"*64+".json")
            path.parent.mkdir(parents=True)
            path.write_text("{}")
            with self.assertRaises(DataError):
                audit_saved(as_of=OBSERVED, fundamentals_dir=tmp)

    def test_future_finished_state_is_invalid(self):
        with self.assertRaises(DataError):
            inspect_fixture(fixture(), "2026-10-01T00:00:00Z", {})


class TrainingRecencyTests(unittest.TestCase):
    def test_fresh_league_result_does_not_mask_old_team(self):
        matches = [{"match_id": i, "kickoff": date, "home_id": h, "away_id": a,
                    "home_name": "home", "away_name": "away", "home_goals": 1, "away_goals": 0}
                   for i, date, h, a in ((1, "2026-01-01T12:00:00Z", 1, 2),
                                         (2, "2026-10-05T12:00:00Z", 2, 3),
                                         (3, "2026-10-06T11:00:00Z", 2, 3))]
        result = training_recency({"matches": matches, "observed_at": "2026-10-07T00:00:00Z"}, as_of=OBSERVED, source={})
        self.assertEqual(result["eligible_matches"], 2)
        self.assertEqual(result["teams_without_match_90_days"], 1)
        self.assertTrue(result["data_observed_after_as_of"])
        self.assertFalse(result["feed_freshness_verified"])
        self.assertFalse(result["point_in_time_sources_verified"])

    def test_process_coverage_uses_only_matched_training_identity(self):
        sample = inspect_fixture(fixture(), OBSERVED, {})
        match = {"match_id": 1, "kickoff": sample["kickoff"], "home_id": 10, "away_id": 20,
                 "home_name": "H", "away_name": "A", "home_goals": 1, "away_goals": 0}
        report = training_recency({"matches": [match]}, as_of=OBSERVED, source={}, samples=[sample])
        self.assertEqual(report["process_sample_overlap"]["sampled_training_matches"], 1)
        self.assertEqual(report["process_sample_overlap"]["training_matches_with_both_xg"], 0)
        match["home_id"] = 11
        with self.assertRaises(DataError):
            training_recency({"matches": [match]}, as_of=OBSERVED, source={}, samples=[sample])


if __name__ == "__main__":
    unittest.main()
