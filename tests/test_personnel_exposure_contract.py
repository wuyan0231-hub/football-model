"""Safety and provenance tests for the independent PF-02 source audit."""
import copy
import hashlib
import json
import unittest

from football_model.personnel_exposure_contract import audit_fixture_context, PersonnelContractError


def source_fixture():
    teams = []
    lineups = []
    for tid in (1, 2):
        ids = [tid * 100 + i for i in range(1, 13)]
        teams.append({"team": {"id": tid}, "players": [
            {"player": {"id": pid}, "statistics": [{"games": {
                "minutes": 94 if index < 11 else None, "position": "D",
                "substitute": index >= 11}}]} for index, pid in enumerate(ids)]})
        lineups.append({"team": {"id": tid},
            "startXI": [{"player": {"id": pid}} for pid in ids[:11]],
            "substitutes": [{"player": {"id": ids[-1]}}]})
    return {"endpoint": "/fixtures", "params": {"id": 9}, "errors": [],
        "paging": {"current": 1, "total": 1}, "fetched_at": "2026-01-02T18:00:00Z",
        "response": [{"fixture": {"id": 9, "date": "2026-01-02T12:00:00Z", "status": {"short": "FT"}},
            "league": {"id": 39, "season": 2025}, "teams": {"home": {"id": 1}, "away": {"id": 2}},
            "players": teams, "lineups": lineups, "events": []}]}


class PersonnelExposureContractTests(unittest.TestCase):
    def setUp(self):
        self.value = source_fixture()

    def audit(self, value=None, **overrides):
        raw = json.dumps(self.value if value is None else value, separators=(",", ":")).encode()
        return self.audit_bytes(raw, **overrides)

    def audit_bytes(self, raw, **overrides):
        kwargs = {"expected_sha256": hashlib.sha256(raw).hexdigest(), "source_state_sha": "a" * 40,
            "source_path": "advanced_features/contexts/original.json", "expected_fixture_id": 9,
            "cutoff_at": "2026-01-03T12:00:00Z"}
        kwargs.update(overrides)
        return audit_fixture_context(raw, **kwargs)

    def test_null_bench_minutes_stay_unknown_and_all_rows_remain_in_denominator(self):
        result = self.audit()
        self.assertEqual(result["denominator"]["original_player_rows"], 24)
        self.assertEqual(result["denominator"]["minutes_status_counts"]["null"], 2)
        nulls = [p for p in result["player_rows"] if p["minutes_status"] == "null"]
        self.assertTrue(all(p["listed_bench"] is True for p in nulls))
        self.assertTrue(all(p["reported_minutes"] is None and p["inferred_minutes"] is None for p in nulls))
        self.assertIsNone(result["denominator"]["eligible_roster_population"])
        self.assertFalse(result["denominator"]["complete_event_population_proven"])

    def test_native_stoppage_minutes_and_source_pointer_are_preserved(self):
        result = self.audit()
        row = result["player_rows"][0]
        self.assertEqual(row["reported_minutes"], 94)
        self.assertEqual(row["source_pointer"], "/response/0/players/0/players/0")
        self.assertEqual(row["raw_row"], self.value["response"][0]["players"][0]["players"][0])
        self.assertFalse(result["limits"]["team_minutes_normalized_to_990"])
        self.assertEqual(result["source"]["fixed_state_sha"], "a" * 40)
        self.assertFalse(result["source"]["git_tree_membership_independently_verified_here"])

    def test_zero_null_absent_invalid_and_unextractable_are_distinct(self):
        people = self.value["response"][0]["players"][0]["players"]
        people[0]["statistics"][0]["games"]["minutes"] = 0
        del people[1]["statistics"][0]["games"]["minutes"]
        people[2]["statistics"][0]["games"]["minutes"] = True
        people[3]["statistics"] = []
        result = self.audit()
        counts = result["denominator"]["minutes_status_counts"]
        for key in ("zero", "absent", "invalid", "unextractable"):
            self.assertEqual(counts[key], 1)
        self.assertEqual(sum(counts.values()), 24)
        self.assertEqual(counts["null"], 2)

    def test_out_of_range_large_integer_is_retained_as_invalid_row(self):
        huge = 10 ** 1000
        self.value["response"][0]["players"][0]["players"][0]["statistics"][0]["games"]["minutes"] = huge
        result = self.audit()
        self.assertEqual(len(result["player_rows"]), 24)
        self.assertEqual(result["player_rows"][0]["reported_minutes"], huge)
        self.assertEqual(result["player_rows"][0]["minutes_status"], "invalid")

    def test_unidentified_duplicate_and_cross_team_player_rows_are_retained(self):
        teams = self.value["response"][0]["players"]
        teams[0]["players"][0]["player"]["id"] = None
        teams[0]["players"].append(copy.deepcopy(teams[0]["players"][1]))
        teams[1]["players"][0]["player"]["id"] = teams[0]["players"][1]["player"]["id"]
        result = self.audit()
        self.assertEqual(len(result["player_rows"]), 25)
        for code in ("player_identity_unknown_or_invalid", "duplicate_player_minutes_rows", "player_minutes_reported_for_both_teams"):
            self.assertIn(code, result["issue_counts"])

    def test_duplicate_team_original_is_not_selected_or_deduplicated(self):
        teams = self.value["response"][0]["players"]
        teams.append(copy.deepcopy(teams[0]))
        result = self.audit()
        self.assertEqual(result["denominator"]["original_player_team_rows"], 3)
        self.assertEqual(result["denominator"]["retained_player_rows"], 36)
        self.assertIn("player_team_row_missing_or_duplicated", result["issue_counts"])

    def test_substitution_observation_does_not_establish_duration_or_fill_null(self):
        self.value["response"][0]["events"] = [{"type": "subst", "team": {"id": 1},
            "player": {"id": 101}, "assist": {"id": 112}, "time": {"elapsed": 46, "extra": None}}]
        result = self.audit()
        bench = next(p for p in result["player_rows"] if p["player_id"] == 112)
        self.assertEqual(bench["substitution_in_event_indices"], [0])
        self.assertIsNone(bench["reported_minutes"])
        self.assertIsNone(bench["inferred_minutes"])
        self.assertIsNone(result["event_rows"][0]["derived_duration"])
        self.assertIn("unknown_minutes_with_substitution_in_observation", result["issue_counts"])

    def test_unknown_outgoing_id_and_red_card_time_are_explicit_and_preserved(self):
        self.value["response"][0]["events"] = [
            {"type": "subst", "team": {"id": 1}, "player": {"id": None}, "assist": {"id": 112}, "time": {"elapsed": 70}},
            {"type": "Card", "detail": "Red Card", "team": {"id": 2}, "player": {"id": 201}, "time": {"elapsed": None}}]
        result = self.audit()
        self.assertEqual(len(result["event_rows"]), 2)
        self.assertIn("substitution_identity_not_closed", result["issue_counts"])
        self.assertIn("red_card_identity_or_time_not_closed", result["issue_counts"])
        self.assertTrue(result["event_rows"][1]["reported_red_card"])

    def test_opponent_self_and_duplicate_substitutions_cannot_support_matching(self):
        examples = [
            ({"id": 201}, {"id": 212}, "event_player_team_conflict"),
            ({"id": 112}, {"id": 112}, "substitution_same_incoming_and_outgoing_player"),
        ]
        for outgoing, incoming, expected in examples:
            value = copy.deepcopy(self.value)
            value["response"][0]["events"] = [{"type": "subst", "team": {"id": 1},
                "player": outgoing, "assist": incoming, "time": {"elapsed": 70}}]
            result = self.audit(value)
            self.assertIn(expected, result["issue_counts"])
            self.assertFalse(result["event_rows"][0]["eligible_for_substitution_consistency_matching"])
        event = {"type": "subst", "team": {"id": 1}, "player": {"id": 101},
                 "assist": {"id": 112}, "time": {"elapsed": 70}}
        self.value["response"][0]["events"] = [event, copy.deepcopy(event)]
        result = self.audit()
        self.assertEqual(result["issue_counts"]["duplicate_substitution_observation"], 2)
        self.assertFalse(any(e["eligible_for_substitution_consistency_matching"] for e in result["event_rows"]))

    def test_own_goal_beneficiary_team_is_not_mistaken_for_exposure_identity_conflict(self):
        self.value["response"][0]["events"] = [{"type": "Goal", "detail": "Own Goal", "team": {"id": 2},
            "player": {"id": 101}, "assist": {"id": None}, "time": {"elapsed": 89}}]
        result = self.audit()
        self.assertNotIn("event_player_team_conflict", result["issue_counts"])
        self.assertFalse(result["event_rows"][0]["identity_team_consistency_assessed"])

    def test_known_past_fixture_input_is_time_admissible_without_trial_qualification(self):
        result = self.audit(mode="prospective_input")
        self.assertTrue(result["time_contract"]["prospective_input_time_admissible"])
        self.assertFalse(result["time_contract"]["formal_trial_qualification_assessed"])

    def test_partial_substitution_cannot_hide_positive_minutes_without_consistent_match(self):
        self.value["response"][0]["players"][0]["players"][-1]["statistics"][0]["games"]["minutes"] = 15
        self.value["response"][0]["events"] = [{"type": "subst", "team": {"id": 1},
            "player": {"id": None}, "assist": {"id": 112}, "time": {"elapsed": None}}]
        result = self.audit()
        row = next(p for p in result["player_rows"] if p["player_id"] == 112)
        self.assertEqual(row["substitution_in_event_indices"], [0])
        self.assertEqual(row["consistent_substitution_in_event_indices"], [])
        self.assertIn("positive_minutes_without_matching_xi_or_substitution_in", result["issue_counts"])

    def test_display_name_or_detail_change_cannot_hide_duplicate_substitution_identity(self):
        event = {"type": "subst", "detail": "Substitution 1", "team": {"id": 1, "name": "A"},
                 "player": {"id": 101, "name": "old"}, "assist": {"id": 112}, "time": {"elapsed": 70}}
        changed = copy.deepcopy(event)
        changed["player"]["name"] = "new"
        changed["team"]["name"] = "alias"
        changed["detail"] = "Substitution 2"
        self.value["response"][0]["events"] = [event, changed]
        result = self.audit()
        self.assertEqual(result["issue_counts"]["duplicate_substitution_observation"], 2)
        self.assertFalse(any(e["eligible_for_substitution_consistency_matching"] for e in result["event_rows"]))
        self.assertEqual(result["event_rows"][1]["raw_row"], changed)

    def test_multiple_incoming_observations_are_ambiguous_without_reentry_contract(self):
        self.value["response"][0]["events"] = [
            {"type": "subst", "team": {"id": 1}, "player": {"id": 101}, "assist": {"id": 112}, "time": {"elapsed": 70}},
            {"type": "subst", "team": {"id": 1}, "player": {"id": 102}, "assist": {"id": 112}, "time": {"elapsed": 75}}]
        result = self.audit()
        self.assertEqual(result["issue_counts"]["ambiguous_multiple_substitution_in_observations"], 2)
        row = next(p for p in result["player_rows"] if p["player_id"] == 112)
        self.assertEqual(row["substitution_in_event_indices"], [0, 1])
        self.assertEqual(row["consistent_substitution_in_event_indices"], [])

    def test_late_download_can_only_be_explicit_development_not_prospective_input(self):
        result = self.audit(cutoff_at="2026-01-02T16:00:00Z", mode="prospective_input")
        self.assertFalse(result["time_contract"]["prospective_input_time_admissible"])
        self.assertIn("original_observed_after_cutoff", result["time_contract"]["past_fixture_input_time_failure_reasons"])
        historical = self.audit(cutoff_at="2026-01-02T16:00:00Z")
        self.assertEqual(historical["time_contract"]["mode"], "historical_development")
        self.assertFalse(historical["time_contract"]["clean_historical_prematch_claim"])

    def test_future_or_unfinished_fixture_never_passes_past_input_time_checks(self):
        result = self.audit(cutoff_at="2026-01-02T10:00:00Z", mode="prospective_input")
        self.assertIn("fixture_not_past_at_cutoff", result["time_contract"]["past_fixture_input_time_failure_reasons"])
        self.value["response"][0]["fixture"]["status"]["short"] = "2H"
        result = self.audit(mode="prospective_input")
        self.assertIn("fixture_not_final", result["time_contract"]["past_fixture_input_time_failure_reasons"])

    def test_extra_time_scope_is_kept_separate_from_regulation(self):
        self.value["response"][0]["fixture"]["status"]["short"] = "AET"
        result = self.audit(mode="outcome_label")
        self.assertEqual(result["minutes_scope"], "provider_reported_may_include_extra_time")
        self.assertFalse(result["time_contract"]["formal_trial_qualification_assessed"])

    def test_corrupt_original_hash_and_unsafe_provenance_are_rejected(self):
        for kwargs in ({"expected_sha256": "b" * 64}, {"source_state_sha": "main"},
                       {"source_path": "../escape"}, {"source_path": "/absolute"},
                       {"source_path": "a/./b"}, {"source_path": "a\\b"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(PersonnelContractError):
                self.audit(**kwargs)

    def test_duplicate_json_keys_and_nonfinite_numbers_are_rejected(self):
        for raw in (b'{"endpoint":"/fixtures","endpoint":"wrong"}', b'{"x":NaN}', b'{"x":1e309}'):
            with self.subTest(raw=raw), self.assertRaises(PersonnelContractError):
                self.audit_bytes(raw)

    def test_wrong_fixture_pagination_and_request_are_rejected(self):
        for mutation in (lambda x: x["paging"].update(total=2),
                         lambda x: x["params"].update(id=10),
                         lambda x: x["response"][0]["teams"]["away"].update(id=1)):
            value = copy.deepcopy(self.value)
            mutation(value)
            with self.assertRaises(PersonnelContractError):
                self.audit(value)
        with self.assertRaises(PersonnelContractError):
            self.audit(expected_fixture_id=True)

    def test_naive_or_invented_knowledge_time_is_rejected(self):
        for field, bad in (("fetched_at", "2026-01-02T18:00:00"), ("fetched_at", None)):
            self.value[field] = bad
            with self.assertRaises(PersonnelContractError):
                self.audit()
        self.value = source_fixture()
        with self.assertRaises(PersonnelContractError):
            self.audit(cutoff_at="2026-01-03")

    def test_lineup_conflicts_prevent_definite_join_without_dropping_player_rows(self):
        lineups = self.value["response"][0]["lineups"]
        lineups[1]["startXI"][0]["player"]["id"] = 101
        result = self.audit()
        self.assertIn("player_listed_for_both_teams", result["issue_counts"])
        self.assertEqual(len(result["player_rows"]), 24)
        self.assertTrue(all(p["listed_starting_eleven"] is None for p in result["player_rows"]))

    def test_unknown_bench_identity_does_not_invalidate_known_xi_or_prove_player_absence(self):
        self.value["response"][0]["lineups"][0]["substitutes"][0]["player"]["id"] = None
        result = self.audit()
        row = next(p for p in result["player_rows"] if p["player_id"] == 112)
        self.assertFalse(row["listed_bench"])
        self.assertIsNone(row["complete_roster_membership"])
        self.assertIn("false_is_not_verified_absence", result["membership_semantics"])
        self.assertTrue(next(p for p in result["player_rows"] if p["player_id"] == 101)["listed_starting_eleven"])
        self.assertIn("lineup_player_identity_unknown", result["issue_counts"])


if __name__ == "__main__":
    unittest.main()
