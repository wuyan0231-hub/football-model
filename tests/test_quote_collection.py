"""Quote transport, append-only acquisition and conservative interpretation."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from football_model.collect_quotes import (OddsClient, _candidates, _page, collect_run, parse_records,
                                           adapt_api_reference_row)
from football_model.data import APIError, DataError, RequestBudgetError, RequestDeadlineError
from football_model.decision_quotes import adapt_quote_row
from test_data import FakeTimer, Response, SECRET

NOW = datetime(2026, 10, 7, 8, tzinfo=timezone.utc)
CONFIG = {"api_daily_limit": 5000, "api_max_run_seconds": 3600, "max_requests_per_run": None,
          "competitions": [{"key": "test", "name": "Test", "country": "Test", "mode": "domestic_baseline"}]}


def iso(value=NOW):
    return value.isoformat().replace("+00:00", "Z")


def fixture(mid=10, hours=1):
    kickoff = NOW + timedelta(hours=hours)
    return {"fixture": {"id": mid, "date": iso(kickoff), "timestamp": int(kickoff.timestamp()),
                        "status": {"short": "NS"}},
            "teams": {"home": {"id": 1, "name": "Home"}, "away": {"id": 2, "name": "Away"}},
            "league": {"id": 999, "season": 2026, "name": "Test", "country": "Test"}}


def candidate(mid=10):
    return {"league": "test", "league_id": 999, "fixture": {
        "match_id": mid, "home_id": 1, "away_id": 2, "kickoff": fixture(mid)["fixture"]["date"]}}


def record(mid=10):
    raw = fixture(mid)
    return {"fixture": raw["fixture"], "league": raw["league"], "update": iso(NOW - timedelta(minutes=10)),
            "bookmakers": [{"id": 8, "name": "Bet365", "bets": [
                {"id": 1, "name": "Match Winner", "values": [
                    {"value": "Home", "odd": "1.8"}, {"value": "Draw", "odd": "3.5"}, {"value": "Away", "odd": "4.5"}]},
                {"id": 4, "name": "Asian Handicap", "values": [
                    {"value": "Home -0.25", "odd": "1.85"}, {"value": "Away -0.25", "odd": "2.05"}]},
                {"id": 50, "name": "Goal Line", "values": [
                    {"value": "Over 2.25", "odd": "1.90"}, {"value": "Under 2.25", "odd": "2.00"}]}]}]}


def page(records=None, number=1, total=1, mid=10, observed=NOW):
    records = [record(mid)] if records is None else records
    return {"endpoint": "/odds", "params": {"fixture": mid, "page": number}, "from_cache": False,
            "fetched_at": iso(observed), "payload": {"get": "odds", "parameters": {"fixture": str(mid), "page": str(number)},
            "errors": [], "results": len(records), "paging": {"current": number, "total": total}, "response": records}}


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


class FakeClient:
    def __init__(self, pages=None):
        self.pages = pages or {1: page()}
        self.requests_made = 0
        self.calls = []
        self.fail_page = None
        self.fixture_status = "NS"

    def status(self):
        self.calls.append("status")
        self.requests_made += 1

    def budget_snapshot(self):
        return {"requests_made": self.requests_made, "daily_remaining": 4900}

    def fetch_fixture(self, mid, force=False):
        self.calls.append(("fixture", mid))
        self.requests_made += 1
        raw = fixture(mid)
        raw["fixture"]["status"]["short"] = self.fixture_status
        return {"endpoint": "/fixtures", "params": {"id": mid}, "response": [raw],
                "fetched_at": iso(), "from_cache": False}

    def fetch_odds_page(self, mid, number):
        self.calls.append(("odds", mid, number))
        if number == self.fail_page:
            raise RequestBudgetError("test budget")
        self.requests_made += 1
        return deepcopy(self.pages[number])


class QuoteParsingTests(unittest.TestCase):
    def parse(self, records=None):
        return parse_records(records or [record()], candidate(), observed_at=iso(), source_id="raw1")

    def test_numeric_prices_do_not_certify_open_or_execution(self):
        rows = self.parse()["rows"]
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(r["status"] == "unknown" and r["reference_values_complete"] for r in rows))
        for row in rows:
            result = adapt_quote_row(row, match_id="10")
            self.assertTrue(all(o["decimal_odds"] is None and o["current_open_claim"] is None for o in result["offers"]))

    def test_explicit_reference_bridge_preserves_parent_and_numeric_prices(self):
        rows = self.parse()["rows"]
        for row in rows:
            before = deepcopy(row)
            if row["market"] == "asian_handicap":
                with self.assertRaises(DataError):
                    adapt_api_reference_row(row)
                continue
            adapted = adapt_api_reference_row(row)
            self.assertEqual(row, before)
            self.assertEqual(adapted["parent_api_row"], before)
            self.assertEqual(adapted["original_row"]["parent_source_status"], "unknown")
            self.assertTrue(all(o["status"] == "reference_only" and o["decimal_odds"] > 1
                                and o["availability_status"] == "unknown" for o in adapted["offers"]))

    def test_reference_bridge_refuses_bad_market_closed_incomplete_or_claimed_open(self):
        row = self.parse()["rows"][0]
        for updates in [{"status": "closed"}, {"reference_values_complete": False},
                        {"market_id": 9}, {"open_evidence": {}}, {"odds": {"home": 2}}]:
            with self.assertRaises((DataError, ValueError)):
                adapt_api_reference_row({**row, **updates})

    def test_asian_shared_home_line_preserves_original_and_requires_mapping_review(self):
        ah = next(r for r in self.parse()["rows"] if r["market"] == "asian_handicap")
        self.assertEqual(ah["line"], -.25)
        self.assertEqual(ah["raw_values"]["away"]["value"], "Away -0.25")
        self.assertEqual(ah["line_mapping_status"], "empirical_inference_pending_provider_confirmation")
        adapted = adapt_quote_row(ah, match_id="10")
        self.assertEqual([c["line"] for c in adapted["contracts"]], [-.25, .25])

    def test_opposite_label_signs_are_not_silently_paired(self):
        raw = record()
        raw["bookmakers"][0]["bets"][1]["values"][1]["value"] = "Away +0.25"
        rows = [r for r in self.parse([raw])["rows"] if r["market"] == "asian_handicap"]
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(r["status"] == "incomplete" for r in rows))

    def test_closed_state_overrides_numeric_values(self):
        raw = record()
        raw["bookmakers"][0]["bets"][0]["suspended"] = True
        row = self.parse([raw])["rows"][0]
        self.assertEqual(row["status"], "closed")
        self.assertFalse(row["reference_values_complete"])
        self.assertEqual(row["odds"]["home"], 1.8)

    def test_missing_null_nonfinite_one_and_wrong_decimal_are_not_zero(self):
        for value in [None, "NaN", "Infinity", "1.00", "-2", True, "bad"]:
            raw = record()
            raw["bookmakers"][0]["bets"][0]["values"][0]["odd"] = value
            row = self.parse([raw])["rows"][0]
            self.assertEqual(row["status"], "incomplete", value)
            self.assertIsNone(row["odds"]["home"])

    def test_duplicate_rows_and_conflicting_prices_are_distinct(self):
        raw = record()
        result = self.parse([raw, deepcopy(raw)])
        self.assertEqual(len(result["rows"]), 3)
        self.assertEqual(result["duplicate_rows"], 3)
        other = deepcopy(raw)
        other["bookmakers"][0]["bets"][0]["values"][0]["odd"] = "1.9"
        result = self.parse([raw, other])
        one = [r for r in result["rows"] if r["market"] == "1x2"]
        self.assertEqual(len(one), 2)
        self.assertTrue(all(r["status"] == "incomplete" for r in one))

    def test_unknown_market_and_half_market_do_not_become_full_match(self):
        raw = record()
        raw["bookmakers"][0]["bets"][0]["name"] = "First Half Winner"
        result = self.parse([raw])
        self.assertEqual(len(result["rows"]), 2)
        self.assertEqual(result["issues"][0]["reason"], "market_mapping_unverified")

    def test_source_update_future_invalidates_reference_without_rewriting_time(self):
        raw = record()
        raw["update"] = iso(NOW + timedelta(minutes=1))
        row = self.parse([raw])["rows"][0]
        self.assertEqual(row["source_updated_at"], raw["update"])
        self.assertIsNone(row["quote_time"])
        self.assertEqual(row["status"], "incomplete")

    def test_each_page_observation_is_preserved(self):
        second = record()
        second["bookmakers"][0]["id"] = 9
        earlier = iso(NOW - timedelta(seconds=3))
        result = parse_records([record(), second], candidate(), observed_at=iso(), source_id="s",
                               record_observed_at=[earlier, iso()], record_source_ids=["p1", "p2"])
        self.assertEqual({r["observed_at"] for r in result["rows"]}, {earlier, iso()})
        self.assertEqual({r["source_id"] for r in result["rows"]}, {"p1", "p2"})

    def test_fixture_or_kickoff_conflict_rejected(self):
        for field, value in [("id", 11), ("date", iso())]:
            raw = record()
            raw["fixture"][field] = value
            with self.assertRaises(DataError):
                self.parse([raw])


class QuoteCollectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.runtime = Path(self.temp.name) / "runtime"

    def seed(self, rows=None):
        raw = [fixture()] if rows is None else rows
        # Keep a finished match when the future window is empty, so there is
        # affirmative league identity in the source rather than absent data.
        if not raw:
            raw = [fixture(hours=-2)]
        write(self.runtime / "cache" / "api_cache" / "schedule.json", {
            "endpoint": "/fixtures", "params": {"league": 999, "season": 2026}, "errors": [],
            "fetched_at": iso(NOW - timedelta(minutes=10)), "response": raw,
            "paging": {"current": 1, "total": 1}})

    def run_collect(self, client=None, **kwargs):
        return collect_run(CONFIG, self.runtime, client=client or FakeClient(),
                           clock=kwargs.pop("clock", lambda: NOW), **kwargs)

    def test_collects_without_model_or_forecast_and_saves_raw_identity(self):
        self.seed()
        client = FakeClient()
        code, report = self.run_collect(client)
        self.assertEqual(code, 0, report)
        self.assertEqual(client.calls[0], "status")
        row = report["matches"][0]
        self.assertEqual(row["status"], "collected")
        self.assertTrue(row["pagination_complete"])
        self.assertEqual(row["unknown_rows"], 3)
        snapshot = json.loads((self.runtime / row["snapshot_path"]).read_text())
        self.assertTrue(snapshot["prematch_observation"])
        self.assertEqual(snapshot["candidate"]["candidate_observed_at"], iso(NOW - timedelta(minutes=10)))
        self.assertFalse((self.runtime / "models").exists())

    def test_complete_pages_saved_and_duplicates_counted(self):
        self.seed()
        client = FakeClient({1: page(total=2), 2: page(number=2, total=2)})
        code, report = self.run_collect(client)
        item = report["matches"][0]
        self.assertEqual(code, 0)
        self.assertEqual(item["page_count"], 2)
        self.assertEqual(len(item["raw_pages"]), 2)
        self.assertEqual(item["duplicate_rows"], 3)

    def test_pagination_change_keeps_raw_but_no_snapshot(self):
        self.seed()
        client = FakeClient({1: page(total=2), 2: page(number=2, total=3)})
        code, report = self.run_collect(client)
        item = report["matches"][0]
        self.assertEqual(code, 1)
        self.assertEqual(item["page_count"], 2)
        self.assertFalse(item["pagination_complete"])
        self.assertIsNone(item["snapshot_path"])

    def test_budget_deferred_preserves_partial_page_and_remaining_candidates(self):
        self.seed([fixture(), fixture(mid=11)])
        client = FakeClient({1: page(total=2)})
        client.fail_page = 2
        code, report = self.run_collect(client)
        self.assertEqual(code, 1)
        self.assertEqual(report["counts"]["deferred"], 2)
        self.assertEqual(report["matches"][0]["page_count"], 1)
        self.assertFalse(report["matches"][0]["pagination_complete"])
        self.assertNotIn(("fixture", 11), client.calls)

    def test_status_budget_failure_retains_all_candidate_denominators(self):
        self.seed([fixture(), fixture(mid=11)])
        client = FakeClient()
        client.status = Mock(side_effect=RequestBudgetError("test"))
        code, report = self.run_collect(client)
        self.assertEqual(code, 1)
        self.assertEqual(report["candidate_count"], 2)
        self.assertEqual(len(report["candidate_manifest"]), 2)
        self.assertEqual(report["counts"]["deferred"], 2)
        self.assertEqual(client.calls, [])

    def test_empty_response_is_observed_empty_not_failure(self):
        self.seed()
        code, report = self.run_collect(FakeClient({1: page([])}))
        self.assertEqual(code, 0)
        self.assertEqual(report["counts"]["empty"], 1)
        self.assertTrue(report["matches"][0]["pagination_complete"])

    def test_empty_window_makes_no_network_request(self):
        self.seed([])
        client = FakeClient()
        code, report = self.run_collect(client)
        self.assertEqual(code, 0, report)
        self.assertEqual(report["status"], "no_upcoming")
        self.assertEqual(client.calls, [])

    def test_missing_schedule_is_a_gap_not_empty_success(self):
        client = FakeClient()
        code, report = self.run_collect(client)
        self.assertEqual(code, 1)
        self.assertEqual(client.calls, [])
        self.assertIn("saved_provider_schedule_missing", [i["reason"] for i in report["issues"]])

    def test_current_fixture_closed_or_started_skips_odds(self):
        self.seed()
        client = FakeClient()
        client.fixture_status = "PST"
        code, report = self.run_collect(client)
        self.assertEqual(code, 0)
        self.assertEqual(report["counts"]["skipped"], 1)
        self.assertFalse(any(isinstance(c, tuple) and c[0] == "odds" for c in client.calls))

    def test_run_id_cannot_overwrite_history(self):
        self.seed()
        _, report = self.run_collect()
        path = self.runtime / "quotes" / "reports" / "history" / (report["run_id"] + ".json")
        before = path.read_bytes()
        with self.assertRaises(DataError):
            self.run_collect()
        self.assertEqual(path.read_bytes(), before)

    def test_new_run_appends_snapshot_and_preserves_old(self):
        self.seed()
        _, report = self.run_collect()
        path = self.runtime / report["matches"][0]["snapshot_path"]
        before = path.read_bytes()
        _, second = self.run_collect(clock=lambda: NOW + timedelta(seconds=1))
        self.assertNotEqual(report["matches"][0]["snapshot_path"], second["matches"][0]["snapshot_path"])
        self.assertEqual(path.read_bytes(), before)

    def test_cli_budget_default_respects_smaller_config_without_request_limit(self):
        self.seed()
        cfg = {**CONFIG, "api_max_run_seconds": 120}
        with patch("football_model.collect_quotes.OddsClient", return_value=FakeClient()) as constructor:
            code, report = collect_run(cfg, self.runtime, clock=lambda: NOW)
        self.assertEqual(code, 0)
        self.assertEqual(constructor.call_args.kwargs["max_run_seconds"], 120)
        self.assertIsNone(constructor.call_args.kwargs["max_requests"])

    def test_page_response_identity_and_count_validated(self):
        for mutate in [lambda p: p["params"].update(fixture=11),
                       lambda p: p["payload"]["paging"].update(current=2),
                       lambda p: p["payload"].update(results=2),
                       lambda p: p["payload"]["parameters"].update(fixture="11"),
                       lambda p: p["payload"].update(get="fixtures"),
                       lambda p: p.update(from_cache=True)]:
            value = page()
            mutate(value)
            with self.assertRaises(DataError):
                _page(value, 10, 1, NOW)


class QuoteClientTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.env = patch.dict(os.environ, {"FOOTBALL_API_KEY": SECRET})
        self.env.start()
        self.addCleanup(self.env.stop)

    def client(self, opener, **kwargs):
        return OddsClient(self.temp.name, opener=opener, clock=lambda: NOW, min_interval=0,
                          sleep=lambda _: None, **kwargs)

    def test_pages_pass_transport_without_core_pagination_rejection(self):
        payload = page(total=2)["payload"]
        opener = Mock(return_value=Response(payload))
        result = self.client(opener).fetch_odds_page(10, 1)
        self.assertEqual(result["payload"]["paging"]["total"], 2)
        self.assertEqual(json.loads(result["raw_response_text"]), payload)
        self.assertIn("/odds?fixture=10&page=1", opener.call_args.args[0].full_url)

    def test_budget_unknown_blocks_paid_query_until_status(self):
        opener = Mock()
        client = self.client(opener, daily_limit=5000)
        with self.assertRaises(RequestBudgetError):
            client.fetch_odds_page(10, 1)
        opener.assert_not_called()

    def test_status_calibrates_and_each_odds_page_consumes_real_budget(self):
        status = {"response": {"requests": {"current": 4999, "limit_day": 5000}}, "errors": [], "paging": {"total": 1}}
        opener = Mock(side_effect=[Response(status), Response(page()["payload"])])
        client = self.client(opener, daily_limit=5000)
        client.status()
        client.fetch_odds_page(10, 1)
        with self.assertRaises(RequestBudgetError):
            client.fetch_odds_page(10, 2)
        self.assertEqual(opener.call_count, 2)

    def test_deadline_reserves_transport_time_before_request(self):
        opener = Mock()
        client = self.client(opener, max_run_seconds=20)
        with self.assertRaises(RequestDeadlineError):
            client.fetch_odds_page(10, 1)
        opener.assert_not_called()

    def test_secret_echo_redacted_in_body_and_payload(self):
        payload = page()["payload"]
        payload["echo"] = SECRET
        result = self.client(Mock(return_value=Response(payload))).fetch_odds_page(10, 1)
        self.assertTrue(result["credential_echo_redacted"])
        self.assertNotIn(SECRET, json.dumps(result))

    def test_invalid_input_is_rejected_before_network(self):
        opener = Mock()
        client = self.client(opener)
        for mid, number in [(True, 1), (10, 0), (-1, 1)]:
            with self.assertRaises(DataError):
                client.fetch_odds_page(mid, number)
        opener.assert_not_called()


if __name__ == "__main__":
    unittest.main()
