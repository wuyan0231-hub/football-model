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

    def test_name_search_requires_unique_exact_name_and_city(self):
        venue = {**NEW_VENUE, "id": None}
        for response in (
            [provider_venue(), provider_venue(id=900002)],
            [provider_venue(name="Synthetic Different Ground")],
            [provider_venue(city="Manchester")],
        ):
            with self.subTest(response=response):
                self.assert_unregistered(venue, VenueClient(response))

    def test_positive_id_verified_alias_is_kept_without_mutating_fixture(self):
        raw = [fixture(venue=NEW_VENUE)]
        canonical = "Synthetic Provider Canonical Ground"
        registry, audit = self.resolve(raw, VenueClient([provider_venue(name=canonical)]))
        row = self.forecasts(raw, registry, audit)[0]
        self.assertNotIn("venue_error", row)
        self.assertEqual(row["venue_evidence"]["venue"]["name"], NEW_VENUE["name"])
        self.assertEqual(raw[0]["fixture"]["venue"]["name"], NEW_VENUE["name"])
        self.assertIn(canonical, json.dumps(registry, ensure_ascii=False))

    def test_positive_id_and_city_mismatch_cannot_register(self):
        for response in ([provider_venue(id=900002)], [provider_venue(city="Manchester")]):
            with self.subTest(response=response):
                self.assert_unregistered(NEW_VENUE, VenueClient(response))

    def test_id_query_with_extra_record_is_not_treated_as_single_stadium_evidence(self):
        self.assert_unregistered(NEW_VENUE, VenueClient(
            [provider_venue(), provider_venue(id=900002, name="Synthetic Extra Ground")]))

    def test_missing_unknown_or_ambiguous_country_never_infers_home_association(self):
        for country in (None, "", "United Kingdom", "World", "Unmapped Country"):
            with self.subTest(country=country):
                self.assert_unregistered(NEW_VENUE, VenueClient([provider_venue(country=country)]))

    def test_existing_id_conflicting_city_or_association_cannot_be_extended(self):
        for venue, candidate in (
            ({**KNOWN_VENUE, "city": "Manchester"},
             provider_venue(KNOWN_VENUE, city="Manchester")),
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
            self.assertEqual(client.max_requests, 2)
            self.assertEqual(len(audit["pending"]), 1)
            self.assertNotIn("synthetic-unit-test-secret", json.dumps(audit))
            self.assertIn("venue_error", self.forecasts([fixture(venue=NEW_VENUE)], registry, audit)[0])
            # The request held for a later competition remains usable after the
            # optional venue operation failed and restored the original budget.
            client.fetch_fixtures(39, 2026)
            self.assertEqual(client.requests_made, 2)
            self.assertEqual(opener.call_count, 2)

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
