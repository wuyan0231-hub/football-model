"""Automatic venue registration contracts; all provider responses are synthetic.

These tests check venue evidence and saved-forecast reproducibility, without
making network requests or fitting a different football model.
"""
import copy
from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.error import URLError

from football_model.data import APIClient, APIError, DataError
from football_model.nations import prepare_nations, validate_nations_forecast
from football_model.venue_registry import load_forecast_registry, resolve_venues


NOW = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)
KNOWN_VENUE = {"id": 489, "name": "Wembley Stadium", "city": "London"}
NEW_VENUE = {"id": 900001, "name": "Synthetic New Ground", "city": "London"}


def fixture(mid=1, venue=None, *, status="NS", when=None):
    if when is None:
        when = NOW + timedelta(days=1) if status == "NS" else NOW - timedelta(days=1)
    return {
        "fixture": {"id": mid, "date": when.isoformat(), "timestamp": int(when.timestamp()),
                    "status": {"short": status}, "venue": copy.deepcopy(venue)},
        "league": {"id": 5, "season": 2026, "round": "Synthetic League A"},
        "teams": {"home": {"id": 10, "name": "England"},
                  "away": {"id": 767, "name": "Wales"}},
        "score": {"fulltime": {"home": None if status == "NS" else 2,
                                "away": None if status == "NS" else 1}},
    }


def provider_venue(venue=NEW_VENUE, country="England", **changes):
    return {**copy.deepcopy(venue), "country": country, **changes}


class VenueClient:
    def __init__(self, response=None, *, error=None, max_requests=100,
                 fetched_at=None, from_cache=False):
        self.response = [provider_venue()] if response is None else response
        self.error = error
        self.calls = []
        self.requests_made = 0
        self.max_requests = max_requests
        self.daily_remaining = None
        self.fetched_at = fetched_at or NOW.isoformat()
        self.from_cache = from_cache

    def fetch_venues(self, *, venue_id=None, name=None, **kwargs):
        self.calls.append({"venue_id": venue_id, "name": name, **kwargs})
        self.requests_made += 1
        if self.error is not None:
            raise self.error
        response = self.response(venue_id=venue_id, name=name) if callable(self.response) else self.response
        return {"response": copy.deepcopy(response), "fetched_at": self.fetched_at,
                "from_cache": self.from_cache, "endpoint": "/venues",
                "params": {"id": venue_id} if venue_id is not None else {"name": name}}


class SyntheticHTTPResponse(io.BytesIO):
    def __init__(self, response):
        super().__init__(json.dumps({"response": response, "errors": [],
                                    "paging": {"current": 1, "total": 1}}).encode())
        self.headers = {}


class VenueRegistryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.runtime = Path(self.temp.name) / "runtime"

    def resolve(self, raw, client=None, **kwargs):
        return resolve_venues(raw, client or VenueClient(), self.runtime, NOW, **kwargs)

    def forecasts(self, raw, registry, audit):
        _, upcoming, _ = prepare_nations(raw, NOW, registry=registry)
        for row in upcoming:
            row["registry_snapshot"] = audit["registry_snapshot"]
        return upcoming

    def assert_unregistered(self, venue, client):
        raw = [fixture(venue=venue)]
        registry, audit = self.resolve(raw, client)
        self.assertEqual(audit["added"], [])
        self.assertEqual(len(audit["pending"]), 1)
        row = self.forecasts(raw, registry, audit)[0]
        self.assertIn("venue_error", row)
        self.assertNotIn("neutral", row)
        return audit

    def test_unseen_positive_id_is_registered_and_saved_forecast_validates(self):
        raw = [fixture(venue=NEW_VENUE)]
        before = copy.deepcopy(raw)
        client = VenueClient()
        registry, audit = self.resolve(raw, client)
        row = self.forecasts(raw, registry, audit)[0]
        self.assertEqual(raw, before)
        self.assertEqual(audit["counts"]["queried"], 1)
        self.assertEqual(client.calls[0]["venue_id"], NEW_VENUE["id"])
        self.assertEqual(len(audit["added"]), 1)
        self.assertEqual(audit["pending"], [])
        self.assertFalse(row["neutral"])
        self.assertEqual(row["venue_evidence"]["association"], "England")
        self.assertEqual(row["venue_evidence"]["venue"], NEW_VENUE)
        self.assertTrue((self.runtime / "registries" / "nations_venues.json").is_file())
        self.assertTrue((self.runtime / audit["registry_snapshot"]).is_file())
        loaded = load_forecast_registry(self.runtime, row)
        self.assertFalse(validate_nations_forecast(row, loaded))
        self.assertEqual(row["venue_evidence"]["registry_sha256"], audit["registry_sha256"])

    def test_registered_venue_is_reused_without_new_lookup(self):
        self.resolve([fixture(venue=NEW_VENUE)])
        client = VenueClient(error=APIError("unexpected request"))
        registry, audit = self.resolve([fixture(2, NEW_VENUE)], client)
        self.assertEqual(client.calls, [])
        self.assertEqual(audit["counts"]["queried"], 0)
        self.assertEqual(audit["added"], [])
        self.assertNotIn("venue_error", self.forecasts([fixture(2, NEW_VENUE)], registry, audit)[0])

    def test_cached_lookup_preserves_original_evidence_time_and_provider_record(self):
        fetched_at = (NOW - timedelta(days=2)).isoformat()
        registry, audit = self.resolve([fixture(venue=NEW_VENUE)],
                                       VenueClient(fetched_at=fetched_at, from_cache=True))
        entry = next(entry for entry in registry["venues"]
                     if all(entry.get(key) == NEW_VENUE[key] for key in ("id", "name", "city")))
        verification = entry["verification"]
        self.assertEqual(verification["fetched_at"], fetched_at)
        self.assertTrue(verification["from_cache"])
        self.assertEqual(verification["original_venue"], NEW_VENUE)
        self.assertEqual(verification["provider_record"], provider_venue())
        self.assertEqual(verification["endpoint"], "/venues")
        self.assertEqual(verification["params"], {"id": NEW_VENUE["id"]})
        self.assertEqual(audit["queries"][0]["fetched_at"], fetched_at)

    def test_country_location_drives_neutrality_not_nominal_home(self):
        raw = [fixture(venue=NEW_VENUE)]
        registry, audit = self.resolve(raw, VenueClient([provider_venue(country="France")]))
        row = self.forecasts(raw, registry, audit)[0]
        self.assertTrue(row["neutral"])
        self.assertEqual(row["venue_evidence"]["association"], "France")
        self.assertTrue(validate_nations_forecast(row, load_forecast_registry(self.runtime, row)))

    def test_venue_in_nominal_away_association_keeps_reverse_home_guard(self):
        raw = [fixture(venue=NEW_VENUE)]
        registry, audit = self.resolve(raw, VenueClient([provider_venue(country="Wales")]))
        self.assertEqual(len(audit["added"]), 1)
        row = self.forecasts(raw, registry, audit)[0]
        self.assertIn("反向主场", row["venue_error"])
        self.assertNotIn("neutral", row)

    def test_null_and_zero_provider_ids_preserve_observed_tuple(self):
        for sentinel in (None, 0):
            with self.subTest(sentinel=sentinel), tempfile.TemporaryDirectory() as temporary:
                venue = {**NEW_VENUE, "id": sentinel}
                raw = [fixture(venue=venue)]
                client = VenueClient()
                registry, audit = resolve_venues(raw, client, Path(temporary), NOW)
                self.assertIsNone(client.calls[0]["venue_id"])
                self.assertEqual(client.calls[0]["name"], NEW_VENUE["name"])
                row = self.forecasts(raw, registry, audit)[0]
                self.assertEqual(row["venue_evidence"]["venue"]["id"], sentinel)
                self.assertEqual(raw[0]["fixture"]["venue"]["id"], sentinel)
                self.assertFalse(validate_nations_forecast(row, registry))

    def test_name_search_requires_unique_exact_name(self):
        venue = {**NEW_VENUE, "id": None}
        for response in (
            [provider_venue(), provider_venue(id=900002)],
            [provider_venue(name="Synthetic Different Ground")],
        ):
            with self.subTest(response=response):
                self.assert_unregistered(venue, VenueClient(response))

    def test_positive_id_verified_alias_is_kept_without_mutating_fixture(self):
        raw = [fixture(venue=NEW_VENUE)]
        canonical = "Synthetic New Ground Stadium"
        registry, audit = self.resolve(raw, VenueClient([provider_venue(name=canonical)]))
        row = self.forecasts(raw, registry, audit)[0]
        self.assertNotIn("venue_error", row)
        self.assertEqual(row["venue_evidence"]["venue"]["name"], NEW_VENUE["name"])
        self.assertEqual(raw[0]["fixture"]["venue"]["name"], NEW_VENUE["name"])
        self.assertIn(canonical, json.dumps(registry, ensure_ascii=False))

    def test_wrong_positive_id_cannot_register(self):
        for response in ([provider_venue(id=900002)],):
            with self.subTest(response=response):
                self.assert_unregistered(NEW_VENUE, VenueClient(response))

    def test_id_query_with_extra_record_is_not_treated_as_single_stadium_evidence(self):
        self.assert_unregistered(NEW_VENUE, VenueClient(
            [provider_venue(), provider_venue(id=900002, name="Synthetic Extra Ground")]))

    def test_missing_unknown_or_ambiguous_country_never_infers_home_association(self):
        for country in (None, "", "United Kingdom", "World", "Unmapped Country"):
            with self.subTest(country=country):
                self.assert_unregistered(NEW_VENUE, VenueClient([provider_venue(country=country)]))

    def test_existing_id_conflicting_association_cannot_be_extended(self):
        for venue, candidate in (
            ({**KNOWN_VENUE, "name": "Synthetic Alias of Wembley"},
             provider_venue(KNOWN_VENUE, country="Scotland", name="Synthetic Alias of Wembley")),
        ):
            with self.subTest(venue=venue):
                self.assert_unregistered(venue, VenueClient([candidate]))

    def test_one_failed_lookup_preserves_supported_known_fixture(self):
        raw = [fixture(1, NEW_VENUE), fixture(2, KNOWN_VENUE)]
        registry, audit = self.resolve(raw, VenueClient(error=APIError("synthetic API failure")))
        rows = self.forecasts(raw, registry, audit)
        self.assertEqual(len(audit["pending"]), 1)
        self.assertIn("venue_error", rows[0])
        self.assertNotIn("venue_error", rows[1])
        self.assertFalse(rows[1]["neutral"])

    def test_same_venue_multiple_fixtures_uses_one_lookup(self):
        raw = [fixture(1, NEW_VENUE), fixture(2, NEW_VENUE, when=NOW + timedelta(days=2))]
        client = VenueClient()
        registry, audit = self.resolve(raw, client)
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(audit["counts"]["queried"], 1)
        self.assertTrue(all("venue_error" not in row for row in self.forecasts(raw, registry, audit)))

    def test_default_queries_more_than_24_unique_venues_and_deduplicates_aliases(self):
        venues = [{"id": 987001 + i, "name": f"Synthetic Uncapped Ground {i}", "city": "London"}
                  for i in range(30)]
        raw = [fixture(530001 + i, venue) for i, venue in enumerate(venues)]
        raw.extend([fixture(530101, venues[0]),
                    fixture(530102, {**venues[0], "name": "Synthetic Uncapped Ground 0 Stadium"})])
        by_id = {venue["id"]: venue for venue in venues}
        client = VenueClient(response=lambda venue_id=None, name=None: [provider_venue(by_id[venue_id])])

        registry, audit = self.resolve(raw, client)

        self.assertEqual(len(client.calls), 30)
        self.assertEqual(audit["counts"]["queried"], 30)
        self.assertEqual(audit["counts"]["added"], 31)
        self.assertEqual(audit["counts"]["deferred"], 0)
        self.assertEqual(audit["pending"], [])
        self.assertIsNone(audit["max_lookups"])
        self.assertIsNone(json.loads(json.dumps(audit))["max_lookups"])
        self.assertTrue(all("venue_error" not in row for row in self.forecasts(raw, registry, audit)))

    def test_explicit_zero_lookup_cap_defers_all_unknown_venues(self):
        client = VenueClient()
        _, audit = self.resolve([fixture(venue=NEW_VENUE)], client, max_lookups=0)
        self.assertEqual(client.calls, [])
        self.assertEqual(audit["max_lookups"], 0)
        self.assertEqual(audit["counts"]["queried"], 0)
        self.assertEqual(audit["pending"][0]["reason_code"], "lookup_budget_deferred")

    def test_invalid_lookup_caps_fail_before_requests_or_mutable_state(self):
        for invalid in (True, False, -1, 1.5, "24", [], {}):
            with self.subTest(max_lookups=invalid):
                client = VenueClient()
                with self.assertRaises(DataError):
                    self.resolve([fixture(venue=NEW_VENUE)], client, max_lookups=invalid)
                self.assertEqual(client.calls, [])
                self.assertFalse(self.runtime.exists())

    def test_default_still_protects_real_network_and_known_daily_budgets_after_24(self):
        venues = [{"id": 988001 + i, "name": f"Synthetic Budget Ground {i}", "city": "London"}
                  for i in range(31)]
        raw = [fixture(540001 + i, venue) for i, venue in enumerate(venues)]
        for budget in ("run", "daily"):
            with self.subTest(budget=budget), tempfile.TemporaryDirectory() as temporary:
                opener = Mock(side_effect=[SyntheticHTTPResponse([provider_venue(venue)])
                                           for venue in venues])
                with patch.dict(os.environ, {"FOOTBALL_API_KEY": "synthetic-unit-test-secret"}):
                    client = APIClient(Path(temporary) / "data", max_requests=80, opener=opener,
                                       clock=lambda: NOW, sleep=lambda _: None, min_interval=0)
                    client.requests_made = 50 if budget == "run" else 0
                    client.daily_remaining = None if budget == "run" else 30
                    original_used = client.requests_made
                    _, audit = resolve_venues(raw, client, Path(temporary) / "runtime", NOW,
                                               reserve_requests=1)
                    self.assertEqual(opener.call_count, 29)
                    self.assertEqual(client.requests_made - original_used, 29)
                    self.assertEqual(audit["counts"]["added"], 29)
                    self.assertEqual(audit["counts"]["queried"], 29)
                    self.assertEqual(audit["counts"]["deferred"], 2)
                    self.assertTrue(all(item["reason_code"] == "request_budget_deferred"
                                        for item in audit["pending"]))
                    self.assertIsNone(audit["max_lookups"])
                    self.assertEqual(client.max_requests, 80)
                    if budget == "daily":
                        self.assertEqual(client.daily_remaining, 1)
                    else:
                        self.assertIsNone(client.daily_remaining)

    def test_default_uses_cached_venue_when_real_network_and_daily_budgets_are_exhausted(self):
        later = {**NEW_VENUE, "id": 989001, "name": "Synthetic Uncached Ground"}
        opener = Mock(return_value=SyntheticHTTPResponse([provider_venue()]))
        with patch.dict(os.environ, {"FOOTBALL_API_KEY": "synthetic-unit-test-secret"}):
            client = APIClient(self.runtime / "data", max_requests=1, opener=opener,
                               clock=lambda: NOW, sleep=lambda _: None, min_interval=0)
            original = client.fetch_venues(venue_id=NEW_VENUE["id"])
            client.daily_remaining = 0
            _, audit = self.resolve([fixture(1, NEW_VENUE), fixture(2, later)], client,
                                     reserve_requests=1)
            self.assertEqual(opener.call_count, 1)
            self.assertEqual(client.requests_made, 1)
            self.assertEqual(client.daily_remaining, 0)
            self.assertEqual(audit["counts"]["queried"], 1)
            self.assertEqual(audit["counts"]["added"], 1)
            self.assertTrue(audit["queries"][0]["from_cache"])
            self.assertEqual(audit["queries"][0]["fetched_at"], original["fetched_at"])
            self.assertEqual(audit["pending"][0]["venue"], later)
            self.assertEqual(audit["pending"][0]["reason_code"], "request_budget_deferred")

    def test_future_window_is_prioritized_before_historical_unknowns(self):
        historic = {**NEW_VENUE, "id": 900002, "name": "Synthetic Historical Ground"}
        raw = [fixture(1, historic, status="FT"), fixture(2, NEW_VENUE)]
        client = VenueClient()
        registry, audit = self.resolve(raw, client, max_lookups=1)
        self.assertEqual(client.calls[0]["venue_id"], NEW_VENUE["id"])
        self.assertEqual(audit["counts"]["queried"], 1)
        self.assertNotIn("venue_error", self.forecasts(raw, registry, audit)[0])

    def test_future_lookup_order_is_kickoff_and_outside_window_is_not_requested(self):
        later = {**NEW_VENUE, "id": 900002, "name": "Synthetic Later Ground"}
        outside = {**NEW_VENUE, "id": 900003, "name": "Synthetic Outside Window"}
        raw = [fixture(1, later, when=NOW + timedelta(days=3)),
               fixture(2, outside, when=NOW + timedelta(days=8)), fixture(3, NEW_VENUE)]
        client = VenueClient()
        _, audit = self.resolve(raw, client, max_lookups=1)
        self.assertEqual([call["venue_id"] for call in client.calls], [NEW_VENUE["id"]])
        self.assertEqual(audit["counts"]["queried"], 1)

    def test_request_reserve_prevents_venue_lookup_from_using_later_league_budget(self):
        later = {**NEW_VENUE, "id": 900002, "name": "Synthetic Later Ground"}
        client = VenueClient(max_requests=2)
        _, audit = self.resolve([fixture(1, NEW_VENUE), fixture(2, later)], client,
                               max_lookups=24, reserve_requests=1)
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(client.requests_made, 1)
        self.assertEqual(audit["counts"]["queried"], 1)
        self.assertEqual(len(audit["pending"]), 1)

    def test_real_client_retries_cannot_consume_reserved_requests(self):
        opener = Mock(side_effect=[URLError("synthetic connection failure"),
                                   SyntheticHTTPResponse([])])
        with patch.dict(os.environ, {"FOOTBALL_API_KEY": "synthetic-unit-test-secret"}):
            client = APIClient(self.runtime / "data", max_requests=2, max_retries=2,
                               opener=opener, sleep=lambda _: None, clock=lambda: NOW,
                               min_interval=0)
            registry, audit = self.resolve([fixture(venue=NEW_VENUE)], client,
                                           reserve_requests=1)
            self.assertEqual(client.requests_made, 1)
            retry = json.loads((self.runtime / audit["retry_state_path"]).read_text())
            self.assertEqual(retry["queries"][json.dumps({"id": NEW_VENUE["id"]})]["attempts"], 1)
            self.assertEqual(client.max_requests, 2)
            self.assertEqual(len(audit["pending"]), 1)
            self.assertNotIn("synthetic-unit-test-secret", json.dumps(audit))
            self.assertIn("venue_error", self.forecasts([fixture(venue=NEW_VENUE)], registry, audit)[0])
            # The request held for a later competition remains usable after the
            # optional venue operation failed and restored the original budget.
            client.fetch_fixtures(39, 2026)
            self.assertEqual(client.requests_made, 2)
            self.assertEqual(opener.call_count, 2)

    def test_deferred_history_is_queried_next_run_after_unresolvable_first_batch(self):
        venues = [{"id": 980001 + i, "name": f"Unresolved Ground {i}", "city": "London"}
                  for i in range(25)]
        raw = [fixture(500001 + i, venue, status="FT", when=NOW - timedelta(days=i + 1))
               for i, venue in enumerate(venues)]

        def response(venue_id=None, name=None):
            i = venue_id - 980001
            return [provider_venue(venues[i], country="England" if i == 24 else "United Kingdom")]

        first = VenueClient(response=response)
        _, first_audit = self.resolve(raw, first, max_lookups=24)
        self.assertEqual([call["venue_id"] for call in first.calls], list(range(980001, 980025)))
        self.assertEqual(first_audit["added"], [])
        self.assertEqual(first_audit["pending"][-1]["reason_code"], "lookup_budget_deferred")
        # A fresh client reads the saved scheduler rather than retaining any
        # in-process cursor. The formerly deferred stadium now gets evidence.
        second = VenueClient(response=response)
        registry, second_audit = self.resolve(raw, second, max_lookups=24)
        self.assertEqual(second.calls[0]["venue_id"], venues[-1]["id"])
        self.assertEqual(second_audit["counts"]["added"], 1)
        entry = second_audit["added"][0]["entry"]
        self.assertEqual(entry["verification"]["provider_record"]["country"], "England")
        self.assertIn(entry, registry["venues"])

    def test_future_overflow_cannot_permanently_displace_untouched_history(self):
        future = [{"id": 981001 + i, "name": f"Unknown Future {i}", "city": "London"}
                  for i in range(25)]
        historic = {"id": 982001, "name": "Verifiable Historic Ground", "city": "London"}
        raw = [fixture(510001 + i, venue, when=NOW + timedelta(hours=1, minutes=i))
               for i, venue in enumerate(future)]
        raw.append(fixture(510100, historic, status="FT"))
        by_id = {venue["id"]: venue for venue in future + [historic]}

        def response(venue_id=None, name=None):
            return [provider_venue(by_id[venue_id], country="England" if venue_id == historic["id"]
                                   else "United Kingdom")]

        first = VenueClient(response=response)
        _, initial = self.resolve(raw, first, max_lookups=24)
        self.assertEqual([call["venue_id"] for call in first.calls], [v["id"] for v in future[:24]])
        self.assertEqual(initial["counts"]["added"], 0)
        second = VenueClient(response=response)
        _, advanced = self.resolve(raw, second, max_lookups=24)
        self.assertEqual([call["venue_id"] for call in second.calls[:2]],
                         [future[-1]["id"], historic["id"]])
        self.assertEqual(advanced["added"][0]["entry"]["id"], historic["id"])

    def test_retry_progress_changes_neither_registry_digest_nor_frozen_evidence(self):
        venues = [{"id": 983001 + i, "name": f"Pending Ground {i}", "city": "London"}
                  for i in range(26)]
        raw = [fixture(520001 + i, venue, status="FT", when=NOW - timedelta(days=i + 1))
               for i, venue in enumerate(venues)]
        by_id = {venue["id"]: venue for venue in venues}
        response = lambda venue_id=None, name=None: [provider_venue(by_id[venue_id], country="United Kingdom")]
        _, first = self.resolve(raw, VenueClient(response=response), max_lookups=24)
        snapshot = self.runtime / first["registry_snapshot"]
        original_bytes = snapshot.read_bytes()
        initial_queue = (self.runtime / first["retry_state_path"]).read_bytes()
        second_client = VenueClient(response=response)
        _, second = self.resolve(raw, second_client, max_lookups=24)
        self.assertEqual([call["venue_id"] for call in second_client.calls[:2]],
                         [venues[24]["id"], venues[25]["id"]])
        self.assertNotEqual((self.runtime / second["retry_state_path"]).read_bytes(), initial_queue)
        self.assertEqual(second["registry_sha256"], first["registry_sha256"])
        self.assertEqual(second["registry_snapshot"], first["registry_snapshot"])
        self.assertEqual(snapshot.read_bytes(), original_bytes)

    def test_budget_deferred_query_has_no_attempt_and_remains_first_next_run(self):
        later = {**NEW_VENUE, "id": 984001, "name": "Later Pending Ground"}
        raw = [fixture(1, NEW_VENUE), fixture(2, later, when=NOW + timedelta(days=2))]
        blocked = VenueClient(max_requests=0)
        _, audit = self.resolve(raw, blocked)
        state = json.loads((self.runtime / audit["retry_state_path"]).read_text())
        self.assertEqual(blocked.calls, [])
        self.assertEqual(audit["counts"]["queried"], 0)
        self.assertTrue(all(q["attempts"] == q["last_attempt_run"] == 0 for q in state["queries"].values()))
        allowed = VenueClient()
        _, next_audit = self.resolve(raw, allowed, max_lookups=1)
        self.assertEqual(allowed.calls[0]["venue_id"], NEW_VENUE["id"])
        self.assertEqual(next_audit["counts"]["added"], 1)

    def test_same_query_aliases_share_one_persisted_attempt_and_rotate_together(self):
        alias = {**NEW_VENUE, "name": "Alternate Fixture Name"}
        later = {**NEW_VENUE, "id": 985001, "name": "Deferred Other Venue"}
        raw = [fixture(1, NEW_VENUE), fixture(2, alias), fixture(3, later)]
        by_id = {NEW_VENUE["id"]: NEW_VENUE, later["id"]: later}
        response = lambda venue_id=None, name=None: [provider_venue(by_id[venue_id], country="United Kingdom")]
        first_client = VenueClient(response=response)
        _, audit = self.resolve(raw, first_client, max_lookups=1)
        state = json.loads((self.runtime / audit["retry_state_path"]).read_text())
        self.assertEqual(len(first_client.calls), 1)
        self.assertEqual(len(state["queries"]), 2)
        self.assertEqual(state["queries"][json.dumps({"id": NEW_VENUE["id"]})]["attempts"], 1)
        second_client = VenueClient(response=response)
        self.resolve(raw, second_client, max_lookups=1)
        self.assertEqual(second_client.calls[0]["venue_id"], later["id"])

    def test_newly_arriving_future_does_not_reset_older_deferred_batch(self):
        old = {**NEW_VENUE, "id": 986001, "name": "Previously Deferred History"}
        raw = [fixture(1, NEW_VENUE), fixture(2, old, status="FT")]
        by_id = {NEW_VENUE["id"]: NEW_VENUE, old["id"]: old}
        response = lambda venue_id=None, name=None: [provider_venue(by_id[venue_id], country="United Kingdom")]
        self.resolve(raw, VenueClient(response=response), max_lookups=1)
        newest = {**NEW_VENUE, "id": 986002, "name": "Newly Arriving Future"}
        by_id[newest["id"]] = newest
        second = VenueClient(response=response)
        self.resolve([fixture(3, newest)] + raw, second, max_lookups=1)
        self.assertEqual(second.calls[0]["venue_id"], old["id"])

    def test_invalid_retry_queue_is_not_silently_reset(self):
        raw = [fixture(venue=NEW_VENUE)]
        self.resolve(raw, VenueClient(error=APIError("synthetic failure")))
        path = self.runtime / "registries/nations_venue_retry.json"
        saved = json.loads(path.read_text())
        saved["queries"][json.dumps({"id": NEW_VENUE["id"]})]["attempts"] = -1
        path.write_text(json.dumps(saved))
        client = VenueClient()
        with self.assertRaises(DataError):
            self.resolve(raw, client)
        self.assertEqual(client.calls, [])
        self.assertEqual(json.loads(path.read_text()), saved)

    def test_conflicting_fixture_identity_fails_without_registration(self):
        original = fixture(1, NEW_VENUE)
        conflict = copy.deepcopy(original)
        conflict["teams"]["home"]["name"] = "Wales"
        client = VenueClient()
        with self.assertRaises(DataError):
            self.resolve([original, conflict], client)
        self.assertEqual(client.calls, [])

    def test_old_snapshot_still_valid_after_registry_growth(self):
        first_raw = [fixture(1, NEW_VENUE)]
        registry, audit = self.resolve(first_raw)
        row = self.forecasts(first_raw, registry, audit)[0]
        frozen_path = self.runtime / row["registry_snapshot"]
        before = frozen_path.read_bytes()
        later = {**NEW_VENUE, "id": 900002, "name": "Synthetic Later Ground"}
        _, newer_audit = self.resolve([fixture(2, later)], VenueClient([provider_venue(later)]))
        self.assertNotEqual(audit["registry_sha256"], newer_audit["registry_sha256"])
        self.assertEqual(frozen_path.read_bytes(), before)
        self.assertFalse(validate_nations_forecast(row, load_forecast_registry(self.runtime, row)))

    def test_missing_or_tampered_snapshot_never_falls_back_to_latest_registry(self):
        raw = [fixture(venue=NEW_VENUE)]
        registry, audit = self.resolve(raw)
        row = self.forecasts(raw, registry, audit)[0]
        snapshot = self.runtime / row["registry_snapshot"]
        saved = snapshot.read_bytes()
        changed = json.loads(saved)
        changed["source_notes"] = "Tampered after forecasting"
        snapshot.write_text(json.dumps(changed), encoding="utf-8")
        with self.assertRaises(DataError):
            load_forecast_registry(self.runtime, row)
        snapshot.unlink()
        with self.assertRaises(DataError):
            load_forecast_registry(self.runtime, row)

    def test_snapshot_digest_and_path_are_bound_to_forecast_evidence(self):
        raw = [fixture(venue=NEW_VENUE)]
        registry, audit = self.resolve(raw)
        row = self.forecasts(raw, registry, audit)[0]
        changed = copy.deepcopy(row)
        changed["venue_evidence"]["registry_sha256"] = "0" * 64
        with self.assertRaises(DataError):
            load_forecast_registry(self.runtime, changed)
        for path in ("../registries/nations_venues.json", str(self.runtime / audit["registry_snapshot"]),
                     "registries/nations_venues/not-a-digest.json"):
            with self.subTest(path=path):
                changed = copy.deepcopy(row)
                changed["registry_snapshot"] = path
                with self.assertRaises(DataError):
                    load_forecast_registry(self.runtime, changed)

    def test_legacy_forecast_uses_explicit_legacy_validation_path(self):
        self.assertIsNone(load_forecast_registry(self.runtime, {}))


if __name__ == "__main__":
    unittest.main()
