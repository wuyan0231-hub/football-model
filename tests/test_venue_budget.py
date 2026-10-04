"""Optional venue budget regressions with the real client and mocked HTTP only."""

from email.message import Message
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

from football_model.data import APIClient
from football_model.nations import prepare_nations
from football_model.venue_registry import resolve_venues
from tests.test_data import Response, envelope
from tests.test_venue_registry import KNOWN_VENUE, NEW_VENUE, NOW, fixture, provider_venue


class VenueBudgetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.runtime = Path(self.temp.name) / "runtime"
        self.env = patch.dict(os.environ, {"FOOTBALL_API_KEY": "synthetic-venue-budget-placeholder"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def client(self, opener, **kwargs):
        return APIClient(self.runtime / "cache", opener=opener, sleep=lambda _: None,
                         clock=lambda: NOW, min_interval=0, **kwargs)

    def resolve(self, client):
        raw = [fixture(1, NEW_VENUE), fixture(2, KNOWN_VENUE)]
        registry, audit = resolve_venues(raw, client, self.runtime, NOW, reserve_requests=1)
        _, upcoming, _ = prepare_nations(raw, NOW, registry=registry)
        known = next(row for row in upcoming if row["match_id"] == 2)
        self.assertNotIn("venue_error", known)
        self.assertFalse(known["neutral"])
        return registry, audit, upcoming

    def assert_deferred(self, audit, upcoming):
        self.assertEqual(audit["added"], [])
        self.assertEqual(len(audit["pending"]), 1)
        self.assertEqual(audit["pending"][0]["reason_code"], "request_budget_deferred")
        unknown = next(row for row in upcoming if row["match_id"] == 1)
        self.assertIn("venue_error", unknown)
        self.assertNotIn("neutral", unknown)

    def test_original_low_daily_balance_preserves_later_required_fixture(self):
        opener = Mock(side_effect=lambda *a, **k: Response(
            envelope([]), {"x-ratelimit-requests-remaining": "0"}))
        client = self.client(opener, max_requests=80)
        client.requests_made = 3
        client.daily_remaining = 1
        _, audit, upcoming = self.resolve(client)
        self.assert_deferred(audit, upcoming)
        opener.assert_not_called()
        self.assertEqual(client.requests_made, 3)
        self.assertEqual(client.daily_remaining, 1)
        self.assertEqual(client.max_requests, 80)
        client.fetch_fixtures(39, 2026)
        self.assertEqual(opener.call_count, 1)
        self.assertIn("/fixtures?", opener.call_args.args[0].full_url)
        self.assertEqual(client.requests_made, 4)
        self.assertEqual(client.daily_remaining, 0)

    def test_cached_venue_evidence_is_usable_when_both_optional_budgets_are_zero(self):
        def response(request, **kwargs):
            if "/venues?" in request.full_url:
                payload = envelope([provider_venue()])
                payload["get"] = "venues"
                payload["parameters"] = {"id": NEW_VENUE["id"]}
                return Response(payload)
            return Response(envelope([]), {"x-ratelimit-requests-remaining": "0"})

        opener = Mock(side_effect=response)
        client = self.client(opener, max_requests=2)
        original = client.fetch_venues(venue_id=NEW_VENUE["id"])
        client.daily_remaining = 1
        _, audit, upcoming = self.resolve(client)
        self.assertEqual(len(audit["added"]), 1)
        self.assertEqual(audit["pending"], [])
        self.assertEqual(opener.call_count, 1)
        self.assertEqual(client.requests_made, 1)
        self.assertEqual(client.daily_remaining, 1)
        verification = audit["added"][0]["entry"]["verification"]
        self.assertTrue(verification["from_cache"])
        self.assertEqual(verification["fetched_at"], original["fetched_at"])
        self.assertNotIn("venue_error", next(row for row in upcoming if row["match_id"] == 1))
        client.fetch_fixtures(39, 2026)
        self.assertEqual(opener.call_count, 2)
        self.assertEqual(client.requests_made, 2)

    def test_runtime_deadline_defers_network_but_keeps_cached_evidence_and_queue(self):
        payload = envelope([provider_venue()])
        payload["get"] = "venues"
        payload["parameters"] = {"id": NEW_VENUE["id"]}
        opener = Mock(return_value=Response(payload))
        elapsed = [0.0]
        client = self.client(opener, max_run_seconds=3600, monotonic=lambda: elapsed[0])
        original = client.fetch_venues(venue_id=NEW_VENUE["id"])
        elapsed[0] = 3600.0
        uncached = {**NEW_VENUE, "id": NEW_VENUE["id"] + 1, "name": "Synthetic Uncached Ground"}
        raw = [fixture(1, uncached), fixture(2, NEW_VENUE), fixture(3, KNOWN_VENUE)]

        registry, audit = resolve_venues(raw, client, self.runtime, NOW, reserve_requests=1)

        self.assertEqual(opener.call_count, 1)
        self.assertEqual(client.requests_made, 1)
        self.assertEqual(audit["counts"]["deferred"], 1)
        self.assertEqual(audit["counts"]["queried"], 1)
        self.assertEqual(audit["pending"], [{"venue": uncached, "match_ids": [1],
            "reason_code": "runtime_budget_deferred",
            "reason": "本轮请求运行时间已到软截止，场馆查询延后"}])
        verification = audit["added"][0]["entry"]["verification"]
        self.assertTrue(verification["from_cache"])
        self.assertEqual(verification["fetched_at"], original["fetched_at"])
        self.assertTrue((self.runtime / audit["registry_snapshot"]).is_file())
        retry_state = json.loads((self.runtime / audit["retry_state_path"]).read_text(encoding="utf-8"))
        by_id = {json.loads(key)["id"]: value for key, value in retry_state["queries"].items()}
        self.assertEqual(by_id[uncached["id"]]["attempts"], 0)
        self.assertEqual(by_id[uncached["id"]]["last_attempt_run"], 0)
        self.assertEqual(by_id[NEW_VENUE["id"]]["attempts"], 1)
        _, upcoming, _ = prepare_nations(raw, NOW, registry=registry)
        self.assertIn("venue_error", next(row for row in upcoming if row["match_id"] == 1))
        for mid in (2, 3):
            self.assertNotIn("venue_error", next(row for row in upcoming if row["match_id"] == mid))

    def test_first_error_header_makes_unknown_daily_balance_protect_retry(self):
        headers = Message()
        headers["x-ratelimit-requests-remaining"] = "1"
        opener = Mock(side_effect=[HTTPError("https://v3.football.api-sports.io/venues", 500,
                                           "synthetic failure", headers, None),
                                   Response(envelope([]), {"x-ratelimit-requests-remaining": "0"})])
        client = self.client(opener)
        self.assertIsNone(client.daily_remaining)
        _, audit, upcoming = self.resolve(client)
        self.assert_deferred(audit, upcoming)
        self.assertEqual(opener.call_count, 1)
        self.assertEqual(client.requests_made, 1)
        self.assertEqual(client.daily_remaining, 1)
        self.assertEqual(client.daily_remaining_reported, 1)
        client.fetch_fixtures(39, 2026)
        self.assertEqual(opener.call_count, 2)
        self.assertIn("/fixtures?", opener.call_args.args[0].full_url)

    def test_known_daily_balance_protects_headerless_failed_retry(self):
        opener = Mock(side_effect=[URLError("synthetic lost response"), Response(envelope([]))])
        client = self.client(opener)
        client.daily_remaining = 2
        _, audit, upcoming = self.resolve(client)
        self.assert_deferred(audit, upcoming)
        self.assertEqual(opener.call_count, 1)
        self.assertEqual(client.requests_made, 1)
        self.assertEqual(client.daily_remaining, 1)
        self.assertIsNone(client.daily_remaining_reported)
        client.fetch_fixtures(39, 2026)
        self.assertEqual(opener.call_count, 2)
        self.assertEqual(client.daily_remaining, 0)

    def test_run_reservation_protects_retries_without_claiming_unknown_daily_balance(self):
        opener = Mock(side_effect=[URLError("synthetic lost response"), Response(envelope([]))])
        client = self.client(opener, max_requests=2)
        _, audit, upcoming = self.resolve(client)
        self.assert_deferred(audit, upcoming)
        self.assertEqual(opener.call_count, 1)
        self.assertEqual(client.requests_made, 1)
        self.assertEqual(client.max_requests, 2)
        self.assertIsNone(client.daily_remaining)
        self.assertIsNone(client.daily_remaining_reported)
        client.fetch_fixtures(39, 2026)
        self.assertEqual(opener.call_count, 2)
        self.assertEqual(client.requests_made, 2)
        self.assertIsNone(client.daily_remaining)


if __name__ == "__main__":
    unittest.main()
