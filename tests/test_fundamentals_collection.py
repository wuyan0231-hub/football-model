"""Collector contracts with synthetic fixtures only; no authenticated network."""
import copy
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from football_model.collect_fundamentals import collect_run
from football_model.data import APIClient, RequestBudgetError
from football_model.integration import build_index

NOW = datetime(2026, 10, 6, 10, tzinfo=timezone.utc)
CONFIG = {"api_daily_limit": 100, "api_max_run_seconds": 3600,
          "competitions": [{"key": "test", "label": "Test League", "name": "Test League",
                            "country": "Test", "mode": "domestic_baseline"}]}
COVERAGE = {"injuries": True, "fixtures": {"lineups": True, "statistics_fixtures": True}}


def iso(when):
    return when.isoformat().replace("+00:00", "Z")


def fixture(mid=10, when=None, home=1, away=2, status="NS"):
    when = when or NOW + timedelta(hours=1)
    return {"fixture": {"id": mid, "date": iso(when), "status": {"short": status}},
            "teams": {"home": {"id": home}, "away": {"id": away}},
            "league": {"id": 999, "season": 2026}}


def identity(raw):
    return {"match_id": raw["fixture"]["id"], "kickoff": raw["fixture"]["date"],
            "home_id": raw["teams"]["home"]["id"], "away_id": raw["teams"]["away"]["id"]}


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


class FakeClient:
    def __init__(self, fixtures=None):
        self.fixtures = {f["fixture"]["id"]: f for f in fixtures or [fixture()]}
        self.calls = []
        self.requests_made = 0
        self.coverage = copy.deepcopy(COVERAGE)
        self.observed = NOW
        self.fail_at = None
        self.bad_facts = set()

    def call(self, kind, mid=None):
        if self.fail_at is not None and self.requests_made >= self.fail_at:
            raise RequestBudgetError("synthetic quota exhausted")
        self.calls.append((kind, mid))
        self.requests_made += 1

    def status(self):
        self.call("status")
        return {}

    def resolve_league(self, config, force=False):
        self.call("league")
        return {"league": {"id": 999}, "seasons": [{"year": 2026, "coverage": self.coverage}],
                "fetched_at": iso(self.observed), "from_cache": False}

    def fetch_fixture(self, mid, force=False):
        self.call("fixture", mid)
        return {"endpoint": "/fixtures", "params": {"id": mid},
                "response": [copy.deepcopy(self.fixtures[mid])], "fetched_at": iso(self.observed),
                "from_cache": False}

    def reserve_requests(self, requests):
        return nullcontext()

    def fetch_fundamentals(self, endpoint, mid, **kwargs):
        self.call(endpoint, mid)
        raw = self.fixtures[mid]
        if (endpoint, mid) in self.bad_facts:
            rows = [{"team": {"id": 99999}}]
        elif endpoint == "/injuries":
            rows = []
        elif endpoint == "/fixtures/statistics":
            rows = [{"team": {"id": raw["teams"][side]["id"]},
                     "statistics": [{"type": "expected_goals", "value": 1.5}]}
                    for side in ("home", "away")]
        else:
            rows = [{"team": {"id": raw["teams"][side]["id"]}, "formation": "4-3-3",
                     "startXI": [{"player": {"id": raw["teams"][side]["id"] * 100 + n}} for n in range(11)],
                     "substitutes": []} for side in ("home", "away")]
        return {"endpoint": endpoint, "params": {"fixture": mid}, "response": rows,
                "fetched_at": iso(self.observed), "from_cache": False}


class FundamentalsCollectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.runtime = Path(self.temp.name) / "runtime"

    def seed(self, targets=None, history=None):
        targets = [fixture()] if targets is None else targets
        run_id = "source"
        rows = [{**identity(f), "status": "unsupported", "generated_at": iso(NOW - timedelta(minutes=5))}
                for f in targets]
        report = {"run_id": run_id, "mode": "train", "generated_at": iso(NOW - timedelta(minutes=5)),
                  "competitions": [{"key": "test", "status": "探索性基线", "model_id": "m1",
                                    "forecast_count": 0, "unsupported_forecasts": len(rows)}]}
        write(self.runtime / "models" / "test.json", {"model_id": "m1"})
        write(self.runtime / "reports" / "history" / "source.json", report)
        write(self.runtime / "predictions" / run_id / "test.json", rows)
        write(self.runtime / "data" / "test.json", {"observed_at": iso(NOW - timedelta(minutes=5)),
                                                    "matches": [identity(f) for f in history or []]})
        build_index(self.runtime, CONFIG)

    def run_collect(self, client=None, **kwargs):
        return collect_run(CONFIG, self.runtime, client=client or FakeClient(), clock=kwargs.pop("clock", lambda: NOW), **kwargs)

    def test_empty_window_does_not_create_client_or_probe_status(self):
        self.seed([])
        with patch("football_model.collect_fundamentals.APIClient") as client:
            code, report = self.run_collect()
        self.assertEqual(code, 0)
        self.assertEqual(report["status"], "no_upcoming")
        client.assert_not_called()

    def test_unsupported_forecast_identity_is_collectable_without_changing_prediction(self):
        self.seed()
        before_model = (self.runtime / "models" / "test.json").read_bytes()
        before_prediction = (self.runtime / "predictions" / "source" / "test.json").read_bytes()
        code, report = self.run_collect()
        self.assertEqual(code, 0, report)
        result = report["matches"][0]
        self.assertEqual(result["status"], "features_saved")
        feature = json.loads((self.runtime / result["feature_path"]).read_text())
        self.assertFalse(feature["fundamentals_used_in_probabilities"])
        self.assertFalse(feature["collection_provenance"]["all_competitions_complete"])
        self.assertEqual(before_model, (self.runtime / "models" / "test.json").read_bytes())
        self.assertEqual(before_prediction, (self.runtime / "predictions" / "source" / "test.json").read_bytes())

    def test_historical_aet_lineups_saved_but_totals_never_used_as_90_minutes(self):
        prior = fixture(9, NOW - timedelta(days=4), away=3, status="AET")
        self.seed(history=[prior])
        client = FakeClient([fixture(), prior])
        code, report = self.run_collect(client)
        self.assertEqual(code, 0, report)
        self.assertIn(("/fixtures/lineups", 9), client.calls)
        self.assertNotIn(("/fixtures/statistics", 9), client.calls)
        feature = json.loads((self.runtime / report["matches"][0]["feature_path"]).read_text())
        self.assertEqual(feature["teams"]["1"]["rest_days"]["value"], 4 + 1/24)
        self.assertEqual(feature["teams"]["1"]["prior_xg"]["paired_sample_count"], 0)
        self.assertEqual(feature["teams"]["1"]["lineup_continuity"]["prior_lineup_count"], 1)

    def test_recent_ft_xg_uses_exact_finished_source_response(self):
        prior = fixture(9, NOW - timedelta(days=4), away=3, status="FT")
        self.seed(history=[prior])
        client = FakeClient([fixture(), prior])
        code, report = self.run_collect(client)
        self.assertEqual(code, 0, report)
        self.assertIn(("fixture", 9), client.calls)
        feature = json.loads((self.runtime / report["matches"][0]["feature_path"]).read_text())
        self.assertEqual(feature["teams"]["1"]["prior_xg"]["for_mean"], 1.5)
        self.assertEqual(feature["teams"]["1"]["prior_xg"]["paired_sample_count"], 1)

    def test_changed_source_identity_blocks_only_that_match(self):
        second = fixture(20, home=3, away=4)
        self.seed([fixture(), second])
        changed = fixture(home=99)
        client = FakeClient([changed, second])
        code, report = self.run_collect(client)
        self.assertEqual(code, 1)
        self.assertEqual([m["status"] for m in report["matches"]], ["failed", "features_saved"])
        self.assertNotIn(("/injuries", 10), client.calls)

    def test_malformed_status_does_not_abort_later_matches_or_report(self):
        second = fixture(20, home=3, away=4)
        self.seed([fixture(), second])
        malformed = fixture()
        malformed["fixture"]["status"] = 123
        client = FakeClient([malformed, second])
        code, report = self.run_collect(client)
        self.assertEqual(code, 1)
        self.assertEqual([m["status"] for m in report["matches"]], ["failed", "features_saved"])
        self.assertTrue((self.runtime / "fundamentals" / "reports" / "latest.json").is_file())

    def test_explicit_missing_match_is_reported_without_network(self):
        self.seed()
        client = FakeClient()
        code, report = self.run_collect(client, match_id=999)
        self.assertEqual(code, 1)
        self.assertIn("requested_match_unavailable", {i["reason"] for i in report["issues"]})
        self.assertFalse(client.calls)

    def test_unfinished_prior_score_does_not_become_ft(self):
        prior = fixture(9, NOW - timedelta(days=4), away=3, status="PST")
        self.seed(history=[prior])
        client = FakeClient([fixture(), prior])
        code, report = self.run_collect(client)
        self.assertNotIn(("/fixtures/statistics", 9), client.calls)
        feature = json.loads((self.runtime / report["matches"][0]["feature_path"]).read_text())
        self.assertIsNone(feature["teams"]["1"]["rest_days"]["value"])

    def test_budget_exhaustion_saves_earlier_evidence_and_defers_remaining_matches(self):
        second = fixture(20, home=3, away=4)
        self.seed([fixture(), second])
        client = FakeClient([fixture(), second])
        client.fail_at = 4  # status, metadata, fixture, injuries; lineups is deferred.
        code, report = self.run_collect(client)
        self.assertEqual(code, 1)
        self.assertEqual(client.requests_made, 4)
        self.assertEqual(report["matches"][0]["status"], "features_saved")
        self.assertTrue(report["matches"][0]["snapshot_paths"])
        self.assertEqual(report["matches"][1]["status"], "deferred")

    def test_bad_later_payload_cannot_discard_saved_first_kind(self):
        self.seed()
        client = FakeClient()
        client.bad_facts.add(("/fixtures/lineups", 10))
        code, report = self.run_collect(client)
        self.assertEqual(code, 1)
        self.assertEqual(len(report["matches"][0]["snapshot_paths"]), 1)
        self.assertIn("/injuries/", report["matches"][0]["snapshot_paths"][0])

    def test_no_features_if_collection_finishes_after_kickoff(self):
        self.seed()
        client = FakeClient()
        def clock():
            return NOW + timedelta(hours=2) if ("/fixtures/lineups", 10) in client.calls else NOW
        code, report = self.run_collect(client, clock=clock)
        self.assertEqual(code, 1)
        self.assertEqual(report["matches"][0]["status"], "evidence_only")
        self.assertFalse(list((self.runtime / "fundamentals" / "features").rglob("*.json")))
        self.assertEqual(len(report["matches"][0]["snapshot_paths"]), 2)

    def test_provider_coverage_false_is_missing_not_zero_and_avoids_requests(self):
        self.seed()
        client = FakeClient()
        client.coverage = {"injuries": False, "fixtures": {"lineups": False}}
        code, report = self.run_collect(client)
        self.assertEqual(code, 0, report)
        self.assertFalse(any(kind.startswith("/fixtures/") or kind == "/injuries" for kind, _ in client.calls))
        self.assertEqual({m["reason"] for m in report["matches"][0]["missing"]}, {"coverage_false"})

    def test_cache_observation_time_is_not_relabelled(self):
        self.seed()
        client = FakeClient()
        client.observed = NOW - timedelta(minutes=10)
        _, report = self.run_collect(client)
        for relative in report["matches"][0]["snapshot_paths"]:
            snapshot = json.loads((self.runtime / relative).read_text())
            self.assertEqual(snapshot["observed_at"], iso(client.observed))
        self.assertEqual(report["forecast_cutoff"], iso(NOW))

    def test_stale_index_is_rejected_without_api_requests(self):
        self.seed()
        write(self.runtime / "reports" / "history" / "later.json",
              {"run_id": "later", "mode": "train", "generated_at": iso(NOW),
               "competitions": [{"key": "test", "status": "未完成"}]})
        client = FakeClient()
        code, report = self.run_collect(client)
        self.assertEqual(code, 1)
        self.assertFalse(client.calls)

    def test_exact_fixture_wrapper_uses_existing_cache_and_validation(self):
        client = object.__new__(APIClient)
        client._cached_request = Mock(return_value={"fetched_at": iso(NOW)})
        client.fetch_fixture(10)
        client._cached_request.assert_called_once_with("/fixtures", {"id": 10}, force=True, max_age_seconds=0)
        with self.assertRaises(ValueError):
            client.fetch_fixture(True)


if __name__ == "__main__":
    unittest.main()
