"""Real failure shapes plus isolated storage/provider faults; no network."""
import copy
import json
import tempfile
import unittest
from unittest.mock import patch

from football_model.data import APIError, DataError
from football_model.fundamentals import (collect_fixture_evidence, failure_diagnostic,
                                        make_snapshot, save_snapshot, statistics_diagnostics)
from tests import test_fundamentals_collection as collection_helpers
from tests.test_fundamentals_collection import (COVERAGE, NOW, FakeClient, fixture, identity, iso)


class S2FundamentalsDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.identity = {**identity(fixture()), "status": "NS"}

    def envelope(self, rows, kind="lineups"):
        return {"endpoint": "/fixtures/" + kind, "params": {"fixture": 10},
                "response": rows, "fetched_at": iso(NOW), "errors": [], "paging": {"total": 1}}

    def lineup(self, tid=1):
        return {"team": {"id": tid}, "startXI": [{"player": {"id": i}} for i in range(1, 12)],
                "substitutes": [{"player": {"id": None, "name": "unidentified"}}]}

    def test_two_live_failures_preserve_complete_xi_and_unknown_bench_identity(self):
        # Exact starter IDs from two blob-verified API responses; failure was
        # player.id=null in substitutes[6], not an incomplete starting eleven.
        cases = [(1492346, 1062, [9978, 515668, 152857, 10122, 197450, 414544,
                  615196, 609544, 280695, 18761, 197513]),
                 (1492363, 147, [425831, 414394, 10378, 9633, 10416, 9924,
                  13310, 315569, 143365, 53966, 10106])]
        for mid, tid, ids in cases:
            with self.subTest(match_id=mid):
                source = self.lineup(tid)
                source["startXI"] = [{"player": {"id": pid}} for pid in ids]
                env = self.envelope([source]); env["params"]["fixture"] = mid
                game = {**self.identity, "match_id": mid, "home_id": tid, "away_id": 9999}
                snapshot = make_snapshot("lineups", env, game)
                team = snapshot["teams"][str(tid)]
                self.assertTrue(team["complete_starting_eleven"])
                self.assertIsNone(team["substitutes"][0]["player_id"])
                self.assertEqual(team["substitutes"][0]["identity_status"], "provider_id_missing")
                self.assertEqual(snapshot["raw_response"], [source])
                with tempfile.TemporaryDirectory() as td:
                    self.assertTrue(save_snapshot(td, snapshot).is_file())

    def test_unknown_substitutes_are_not_deduplicated_or_given_synthetic_ids(self):
        row = self.lineup(); row["substitutes"] *= 2
        team = make_snapshot("lineups", self.envelope([row]), self.identity)["teams"]["1"]
        self.assertEqual([p["player_id"] for p in team["substitutes"]], [None, None])

    def test_missing_starter_id_and_invalid_zero_bench_id_remain_blocked(self):
        for group, value in (("startXI", None), ("substitutes", 0), ("substitutes", True)):
            row = self.lineup(); row[group][0]["player"]["id"] = value
            with self.subTest(group=group, value=value), self.assertRaises(DataError):
                make_snapshot("lineups", self.envelope([row]), self.identity)

    def test_duplicate_known_substitute_still_blocked(self):
        row = self.lineup(); row["substitutes"] = [{"player": {"id": 50}}] * 2
        with self.assertRaises(DataError):
            make_snapshot("lineups", self.envelope([row]), self.identity)

    def test_failure_projection_replays_without_credentials_or_exception_text(self):
        secret = "NEVER_STORE_TOKEN_abcdef012345"
        row = self.lineup(); row["startXI"][0]["player"]["id"] = None
        row["name"] = secret
        env = self.envelope([row]); env["headers"] = {"Authorization": secret}; env["api_key"] = secret
        try:
            make_snapshot("lineups", env, self.identity)
        except DataError as exc:
            diagnostic = failure_diagnostic("lineups", self.identity, stage="normalize", error=exc,
                checked_at=NOW, envelope=env)
        self.assertTrue(diagnostic["replay_verified"])
        self.assertEqual(diagnostic["code"], "invalid_player_id")
        self.assertNotIn(secret, json.dumps(diagnostic))
        self.assertNotIn("Authorization", json.dumps(diagnostic))
        self.assertFalse(diagnostic["source_http_bytes_preserved"])

    def test_provider_exception_is_categorized_but_not_serialized(self):
        client = FakeClient()
        client.fetch_fundamentals = lambda *args, **kwargs: (_ for _ in ()).throw(APIError("secret-token"))
        result = collect_fixture_evidence(client, self.identity, coverage=COVERAGE,
            observed_at=NOW, kinds=("lineups",))
        self.assertEqual(result["missing"][0]["reason"], "provider_error")
        self.assertEqual(result["diagnostics"][0]["stage"], "fetch")
        self.assertNotIn("secret-token", json.dumps(result))

    def test_same_generic_exception_class_does_not_prove_same_failure(self):
        env = self.envelope([self.lineup()]); env["endpoint"] = "/fixtures"
        diagnostic = failure_diagnostic("lineups", self.identity, stage="normalize",
            error=DataError("a different unclassified validation fault"), checked_at=NOW, envelope=env)
        self.assertEqual(diagnostic["code"], "validation_failure")
        self.assertFalse(diagnostic["replay_verified"])

    def test_delayed_response_failure_uses_actual_recording_clock(self):
        from datetime import timedelta
        response_time = NOW + timedelta(seconds=15)
        recorded_time = NOW + timedelta(seconds=16)
        row = self.lineup(); row["startXI"][0]["player"]["id"] = None
        env = self.envelope([row]); env["fetched_at"] = iso(response_time)
        client = FakeClient()
        client.fetch_fundamentals = lambda *args, **kwargs: env
        result = collect_fixture_evidence(client, self.identity, coverage=COVERAGE,
            observed_at=NOW, collection_at=NOW, kinds=("lineups",), clock=lambda: recorded_time)
        diagnostic = result["diagnostics"][0]
        self.assertEqual(diagnostic["collection_started_at"], iso(NOW))
        self.assertEqual(diagnostic["response_observed_at"], iso(response_time))
        self.assertEqual(diagnostic["failure_recorded_at"], iso(recorded_time))
        self.assertEqual(diagnostic["checked_at"], iso(recorded_time))
        self.assertEqual(result["missing"][0]["checked_at"], iso(recorded_time))
        self.assertEqual(diagnostic["payload"]["context_observed_at"], iso(NOW))
        self.assertEqual(diagnostic["payload"]["envelope"]["fetched_at"], iso(response_time))

    def test_normal_unopened_lineup_window_has_no_failure(self):
        from datetime import timedelta
        game = {**self.identity, "kickoff": iso(NOW + timedelta(hours=10))}
        result = collect_fixture_evidence(FakeClient(), game, coverage=COVERAGE,
            observed_at=NOW, kinds=("lineups",))
        self.assertEqual(result["missing"][0]["reason"], "lineup_window_not_open")
        self.assertEqual(result["diagnostics"], [])

    def test_real_s1_shot_shape_flags_contradictions_and_preserves_values(self):
        # S1 009_fixtures_statistics.json, fixture 1493165, team 456.
        def stats(**values): return [{"type": k, "value": v} for k, v in values.items()]
        rows = [{"team": {"id": 456},
            "statistics": stats(**{"Total Shots": 16, "Shots insidebox": 9, "Shots outsidebox": 6}),
            "statistics_1h": stats(**{"Total Shots": 4}),
            "statistics_2h": stats(**{"Total Shots": 11})}]
        before = copy.deepcopy(rows)
        findings = statistics_diagnostics(rows)
        self.assertEqual({x["check"] for x in findings}, {"shots_by_location", "halves_Total Shots"})
        self.assertEqual(rows, before)
        self.assertTrue(all(x["action"] == "quarantine_shot_derived_features" for x in findings))

    def test_missing_or_duplicate_shot_cells_are_not_zero_filled(self):
        row = {"team": {"id": 1}, "statistics": [
            {"type": "Total Shots", "value": 16}, {"type": "Shots insidebox", "value": None},
            {"type": "Shots outsidebox", "value": 6}]}
        self.assertEqual(statistics_diagnostics([row]), [])
        row["statistics"].extend([{"type": "Shots insidebox", "value": 9}])
        self.assertEqual(statistics_diagnostics([row]), [])

    def test_collector_saves_replayable_validation_payload(self):
        helper = collection_helpers.FundamentalsCollectionTests(); helper.setUp()
        self.addCleanup(helper.doCleanups)
        helper.seed(); client = FakeClient()
        client.bad_facts.add(("/fixtures/lineups", 10))
        code, report = helper.run_collect(client)
        item = report["matches"][0]
        self.assertEqual(code, 1)
        diagnostic = next(d for d in item["diagnostics"] if d["stage"] == "normalize")
        saved = json.loads((helper.runtime / diagnostic["diagnostic_path"]).read_text())
        self.assertEqual(saved["code"], "team_identity_mismatch")
        self.assertTrue(saved["replay_verified"])
        self.assertEqual(saved["endpoint"], "/fixtures/lineups")
        self.assertFalse(report["fundamentals_used_in_probabilities"])

    def test_collector_distinguishes_storage_failure(self):
        helper = collection_helpers.FundamentalsCollectionTests(); helper.setUp()
        self.addCleanup(helper.doCleanups)
        helper.seed()
        with patch("football_model.collect_fundamentals.save_snapshot", side_effect=OSError("secret path")):
            code, report = helper.run_collect()
        self.assertEqual(code, 1)
        diagnostics = report["matches"][0]["diagnostics"]
        self.assertTrue(all(x["stage"] == "snapshot_save" and x["category"] == "storage" for x in diagnostics))
        self.assertNotIn("secret path", json.dumps(report))


if __name__ == "__main__":
    unittest.main()
