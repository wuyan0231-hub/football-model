from contextlib import redirect_stderr
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

from football_model.data import APIClient, DataError
from football_model.advanced_capacity import CapacityDeferred, CapacityGuard
from football_model.fixture_recovery import ROOT, TARGET_IDS, _bytes, _candidates, collect_run, main

NOW = datetime(2026, 10, 14, 0, 10, tzinfo=timezone.utc)
SHA = "a" * 40


class Response:
    headers = {}
    def __init__(self, raw):
        self.raw = raw
    def __enter__(self):
        return self
    def __exit__(self, *args):
        pass
    def read(self):
        return self.raw


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.runtime = Path(self.directory.name)
        self.config = {"api_daily_limit": 7460, "max_requests_per_run": None,
            "api_max_run_seconds": 3600, "competitions": [
                {"key": "championship", "name": "Championship", "country": "England"}]}
        self.predictions = [{"match_id": mid, "home_id": 100 + index * 2,
            "away_id": 101 + index * 2, "kickoff": "2026-10-13T18:45:00Z",
            "competition": "championship"} for index, mid in enumerate(TARGET_IDS)]
        self.request = {"schema_version": "fixture-recovery-request-v1", "league": "championship",
            "league_id": 40, "target_ids": list(TARGET_IDS), "original_state_commit": SHA,
            "original_forecast": self.parent("predictions/old/championship.json", self.predictions),
            "failed_runs": [self.parent("fundamentals/reports/history/round" + str(index) + ".json",
                {"status": "partial", "matches": [{"match_id": mid, "league": "championship",
                    "status": "deferred"} for mid in TARGET_IDS]}) for index in (20, 21)]}
        self.wires, self.calls = {}, []
        self.used = 5000
        self.mutator = lambda value: value

    def parent(self, relative, value):
        raw = _bytes(value)
        path = self.runtime / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        return {"path": relative, "sha256": hashlib.sha256(raw).hexdigest()}

    def fixture(self, mid):
        expected = next(row for row in self.predictions if row["match_id"] == mid)
        return {"fixture": {"id": mid, "date": expected["kickoff"],
            "timestamp": int(datetime.fromisoformat(expected["kickoff"].replace("Z", "+00:00")).timestamp()),
            "status": {"short": "FT"}}, "league": {"id": 40, "season": 2026},
            "teams": {"home": {"id": expected["home_id"], "name": "Home"},
                      "away": {"id": expected["away_id"], "name": "Away"}},
            "score": {"fulltime": {"home": 2, "away": 1}, "halftime": {"home": 1, "away": 0}},
            "statistics": [], "lineups": [], "players": []}

    def opener(self, request, **kwargs):
        # Only public endpoint/parameters are inspected, never request headers.
        url = urlsplit(request.full_url)
        self.calls.append(url.path)
        if url.path == "/status":
            return Response(_bytes({"errors": [], "response": {
                "requests": {"current": self.used, "limit_day": 7500}}}))
        mid = int(parse_qs(url.query)["id"][0])
        value = self.mutator({"errors": [], "paging": {"current": 1, "total": 1},
                              "response": [self.fixture(mid)]})
        raw = json.dumps(value, indent=3).encode() + b"\n\n"
        self.wires[mid] = raw
        return Response(raw)

    def run_collection(self, clock=lambda: NOW):
        with patch.dict("os.environ", {"FOOTBALL_API_KEY": "fake-recovery-test-value"}):
            client = APIClient(self.runtime / ROOT / "cache", daily_limit=7460,
                max_run_seconds=300, max_retries=0, min_interval=0,
                opener=self.opener, clock=clock)
            return collect_run(self.config, self.runtime, self.request,
                source_commit=SHA, state_commit=SHA, clock=clock, client=client)

    def test_eight_sources_have_exact_wire_bytes_and_historical_scope(self):
        old = {ref["path"]: (self.runtime / ref["path"]).read_bytes()
               for ref in [self.request["original_forecast"], *self.request["failed_runs"]]}
        code, report, ref = self.run_collection()
        self.assertEqual(code, 0)
        self.assertTrue(report["full_collection_completed"])
        self.assertEqual(self.calls.count("/fixtures"), 8)
        self.assertEqual(report["api_budget"]["configured_daily_limit"], 7460)
        self.assertEqual(report["scope"], "retrospective_historical_fixture_details")
        self.assertFalse(report["prematch_eligible"])
        self.assertFalse(report["confirmation_eligible"])
        self.assertEqual(report["started_at"], "2026-10-14T00:10:00Z")
        for item in report["matches"]:
            raw_ref = item["http_attempts"][0]["raw_body"]
            self.assertEqual((self.runtime / raw_ref["path"]).read_bytes(), self.wires[item["match_id"]])
            self.assertEqual(item["regulation_result"]["home_goals"], 2)
            self.assertEqual(item["embedded_fields"]["players"]["value"], None)
        for path, raw in old.items():
            self.assertEqual((self.runtime / path).read_bytes(), raw)
        self.assertEqual(hashlib.sha256((self.runtime / ref["path"]).read_bytes()).hexdigest(), ref["sha256"])
        self.assertFalse((self.runtime / "integration/index.json").exists())
        self.assertFalse((self.runtime / "fundamentals/reports/latest.json").exists())

    def test_wrong_fixture_or_team_identity_is_rejected_but_wire_retained(self):
        def wrong(value):
            value["response"][0]["teams"]["home"]["id"] += 999
            return value
        self.mutator = wrong
        code, report, _ = self.run_collection()
        self.assertEqual(code, 1)
        self.assertEqual(len(report["matches"]), 8)
        for item in report["matches"]:
            self.assertEqual(item["status"], "failed")
            self.assertIn("raw_body", item["http_attempts"][0])
            self.assertNotIn("regulation_result", item)

    def test_duplicate_response_is_rejected(self):
        def duplicate(value):
            value["response"] *= 2
            return value
        self.mutator = duplicate
        code, report, _ = self.run_collection()
        self.assertEqual(code, 1)
        self.assertTrue(all(item["status"] == "failed" for item in report["matches"]))

    def test_duplicate_json_fixture_id_cannot_be_validated(self):
        opener = self.opener
        def ambiguous(request, **kwargs):
            response = opener(request, **kwargs)
            if urlsplit(request.full_url).path == "/fixtures":
                response.raw = response.raw.replace(b'"id": ', b'"id": 9999999, "id": ', 1)
            return response
        self.opener = ambiguous
        code, report, _ = self.run_collection()
        self.assertEqual(code, 1)
        for item in report["matches"]:
            self.assertEqual(item["status"], "failed")
            attempt = item["http_attempts"][0]
            self.assertEqual(attempt["body_validation_status"], "invalid_or_ambiguous_json")
            self.assertIn("raw_body", attempt)

    def test_duplicate_parent_identity_is_rejected_before_network(self):
        request = deepcopy(self.request)
        request["original_forecast"] = self.parent("predictions/bad/championship.json", self.predictions * 2)
        with self.assertRaises(DataError):
            _candidates(self.runtime, request, self.config)
        self.assertEqual(self.calls, [])

    def test_budget_reservation_defers_every_unfinished_request(self):
        self.used = 6960
        code, report, _ = self.run_collection()
        self.assertEqual(code, 1)
        self.assertEqual(self.calls, ["/status"])
        self.assertEqual(len(report["matches"]), 8)
        self.assertTrue(all(item["status"] == "deferred" for item in report["matches"]))
        self.assertFalse(report["full_collection_completed"])

    def test_real_clock_before_kickoff_cannot_create_history(self):
        before = datetime(2026, 10, 13, 15, tzinfo=timezone.utc)
        code, report, _ = self.run_collection(clock=lambda: before)
        self.assertEqual(code, 1)
        self.assertEqual(self.calls, [])
        self.assertFalse(report["prematch_eligible"])
        self.assertEqual(report["started_at"], "2026-10-13T15:00:00Z")

    def test_cli_has_no_as_of_or_time_override(self):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
            main(["--source-commit", SHA, "--state-commit", SHA, "--as-of", "2026-10-13T15:00:00Z"])
        self.assertEqual(caught.exception.code, 2)

    def test_clock_rollback_stops_requests_and_saves_partial_report(self):
        readings = iter([NOW, NOW, NOW - timedelta(seconds=1)])
        def clock():
            return next(readings, NOW - timedelta(seconds=1))
        code, report, ref = self.run_collection(clock=clock)
        self.assertEqual(code, 1)
        self.assertEqual(report["clock_integrity"], "failed")
        self.assertIsNone(report["finished_at"])
        self.assertEqual(len(report["matches"]), 8)
        self.assertTrue((self.runtime / ref["path"]).is_file())
        self.assertEqual(self.calls, ["/status"])

    def test_credential_echo_never_enters_a_saved_file(self):
        def echo(value):
            value["provider_echo"] = "fake-recovery-test-value"
            return value
        self.mutator = echo
        code, report, _ = self.run_collection()
        self.assertEqual(code, 1)
        for path in self.runtime.rglob("*"):
            if path.is_file():
                self.assertNotIn(b"fake-recovery-test-value", path.read_bytes())
        first = report["matches"][0]["http_attempts"][0]
        self.assertEqual(first["raw_body_preservation_status"], "credential_or_storage_admission_refused")
        self.assertNotIn("raw_body", first)
        self.assertTrue(all(item["status"] == "deferred" for item in report["matches"][1:]))

    def test_http_error_wire_is_saved_without_changing_provider_status(self):
        opener = self.opener
        def failed(request, **kwargs):
            if urlsplit(request.full_url).path == "/status":
                return opener(request, **kwargs)
            self.calls.append("/fixtures")
            raise HTTPError(request.full_url, 403, "ignored", {}, io.BytesIO(b"{\"error\":\"denied\"}\n"))
        self.opener = failed
        code, report, _ = self.run_collection()
        self.assertEqual(code, 1)
        for item in report["matches"]:
            attempt = item["http_attempts"][0]
            self.assertEqual(attempt["http_status"], 403)
            self.assertEqual((self.runtime / attempt["raw_body"]["path"]).read_bytes(), b"{\"error\":\"denied\"}\n")

    def test_capacity_deferral_preserves_all_requested_matches(self):
        guard = CapacityGuard(self.runtime)
        def defer(**kwargs):
            raise CapacityDeferred(guard.summary())
        guard.before_request = defer
        code, report, _ = collect_run(self.config, self.runtime, self.request,
            source_commit=SHA, state_commit=SHA, clock=lambda: NOW, capacity_guard=guard,
            client=SimpleNamespace(budget_snapshot=lambda: {}))
        self.assertEqual(code, 1)
        self.assertEqual(len(report["matches"]), 8)
        self.assertTrue(all(item["reason"] == "storage_capacity_deferred" for item in report["matches"]))

    def test_bad_embedded_player_identity_is_quarantined(self):
        def bad_players(value):
            value["response"][0]["players"] = [{"team": {"id": 99999}, "players": []}]
            return value
        self.mutator = bad_players
        code, report, _ = self.run_collection()
        self.assertEqual(code, 1)
        for item in report["matches"]:
            self.assertEqual(item["embedded_fields"]["players"]["status"], "quarantined")
            self.assertIn("regulation_result", item)
            self.assertIn("raw_body", item["http_attempts"][0])


if __name__ == "__main__":
    unittest.main()
