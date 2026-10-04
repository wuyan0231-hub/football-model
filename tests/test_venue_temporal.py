"""Reviewed spelling and point-in-time venue evidence regression contracts.

All documents, timestamps, fixtures and API responses here are synthetic.
"""
import copy
from datetime import timedelta
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from football_model.data import DataError
from football_model.nations import prepare_nations, validate_nations_forecast, venue_temporal_audit
from football_model.venue_registry import load_forecast_registry, resolve_venues
from test_venue_registry import NOW, NEW_VENUE, VenueClient, fixture, provider_venue


class VenueTemporalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.runtime = Path(self.temp.name) / "runtime"
        self.observed = {"id": 980780, "name": "Synthetic Toumba Ground", "city": "Thessaloniki"}
        self.provider = {"id": 980780, "name": "Synthetic Stadio Toumbas", "city": "Thessaloníki", "country": "Greece"}
        statement = "Synthetic reviewed same-stadium city-spelling evidence."
        self.rule = {"rule_id": "synthetic-toumba-only", "provider_id": 980780,
                     "association": "Greece", "fixture_venue": self.observed,
                     "provider_venue": {k: self.provider[k] for k in ("id", "name", "city")},
                     "reviewed_at": NOW.isoformat(), "sources": [{
                         "url": "https://example.invalid/synthetic-official-stadium",
                         "statement": statement, "source_sha256": hashlib.sha256(statement.encode()).hexdigest(),
                         "fetched_at": NOW.isoformat()}]}
        self.alias_path = Path(self.temp.name) / "reviewed_aliases.json"
        self.alias_path.write_text(json.dumps({"schema_version": 1, "aliases": [self.rule]}))
        mocked = patch("football_model.venue_registry.CITY_ALIASES_PATH", self.alias_path)
        mocked.start()
        self.addCleanup(mocked.stop)

    def resolve(self, venue=NEW_VENUE, response=None, **kwargs):
        return resolve_venues([fixture(venue=venue)], VenueClient(response), self.runtime,
                              NOW, clock=lambda: NOW, **kwargs)

    def row(self, registry, venue=NEW_VENUE):
        return prepare_nations([fixture(venue=venue)], NOW, registry=registry)[1][0]

    def test_reviewed_exact_stadium_alias_is_frozen_without_mutating_raw_tuple(self):
        original = copy.deepcopy(self.observed)
        registry, audit = self.resolve(self.observed, [self.provider])
        self.assertEqual(audit["counts"]["added"], 1)
        entry = audit["added"][0]["entry"]
        self.assertEqual(entry["verification"]["identity_basis"]["evidence"], self.rule)
        self.assertEqual(entry["verification"]["original_venue"], original)
        self.assertEqual(entry["verification"]["provider_record"], self.provider)
        row = self.row(registry, self.observed)
        row["registry_snapshot"] = audit["registry_snapshot"]
        self.assertTrue(validate_nations_forecast(row, load_forecast_registry(self.runtime, row)))
        self.assertEqual(self.observed, original)

    def test_reviewed_alias_does_not_generalize_to_other_id_name_city_or_country(self):
        cases = [({**self.observed, "id": 980781}, {**self.provider, "id": 980781}),
                 ({**self.observed, "name": "Another Stadium"}, self.provider),
                 ({**self.observed, "city": "Athens"}, self.provider),
                 (self.observed, {**self.provider, "city": "Athens"}),
                 (self.observed, {**self.provider, "country": "Türkiye"}),
                 ({**self.observed, "id": None}, self.provider)]
        for observed, provider in cases:
            with self.subTest(observed=observed, provider=provider):
                _, audit = self.resolve(observed, [provider])
                self.assertEqual(audit["added"], [])
                self.assertEqual(len(audit["pending"]), 1)

    def test_matching_provider_tuple_and_reviewed_fixture_tuple_can_coexist(self):
        provider_tuple = {k: self.provider[k] for k in ("id", "name", "city")}
        self.resolve(provider_tuple, [self.provider])
        registry, audit = self.resolve(self.observed, [self.provider])
        self.assertEqual(audit["counts"]["added"], 1)
        self.assertNotIn("venue_error", self.row(registry, self.observed))

    def test_future_source_or_future_alias_review_is_not_registered(self):
        client = VenueClient(fetched_at=(NOW + timedelta(minutes=1)).isoformat())
        _, audit = resolve_venues([fixture(venue=NEW_VENUE)], client, self.runtime, NOW, clock=lambda: NOW)
        self.assertEqual(audit["pending"][0]["reason_code"], "source_time_in_future")
        self.rule["reviewed_at"] = (NOW + timedelta(minutes=1)).isoformat()
        self.alias_path.write_text(json.dumps({"schema_version": 1, "aliases": [self.rule]}))
        _, audit = self.resolve(self.observed, [self.provider])
        self.assertEqual(audit["pending"][0]["reason_code"], "source_time_in_future")

    def test_current_lookup_records_unknown_historical_validity_and_publication(self):
        registry, _ = self.resolve()
        row = self.row(registry)
        temporal = venue_temporal_audit(row, registry, cutoff=NOW + timedelta(hours=1))
        self.assertFalse(temporal["eligible"])
        self.assertEqual(temporal["known_at"], NOW.isoformat())
        self.assertIsNone(temporal["valid_from"])
        self.assertIsNone(temporal["valid_to"])
        self.assertIsNone(temporal["source_published_at"])
        self.assertEqual(temporal["reasons"], ["historical_validity_unknown"])
        self.assertFalse(validate_nations_forecast(row, registry))
        with self.assertRaisesRegex(DataError, "historical_validity_unknown"):
            venue_temporal_audit(row, registry, cutoff=NOW + timedelta(hours=1), strict=True)

    def test_late_evidence_cannot_pass_an_earlier_cutoff(self):
        registry, _ = self.resolve()
        row = self.row(registry)
        with self.assertRaisesRegex(DataError, "known_after_cutoff.*evidence_after_cutoff"):
            venue_temporal_audit(row, registry, cutoff=NOW - timedelta(seconds=1), strict=True)

    def test_historical_training_is_labelled_retrospective_with_original_match_cutoff(self):
        registry, _ = self.resolve()
        raw = [fixture(venue=NEW_VENUE, status="FT", when=NOW - timedelta(days=10))]
        matches, _, audit = prepare_nations(raw, NOW, registry=registry)
        self.assertEqual(len(matches), 1)
        temporal = audit["venue_temporal"]["by_match"][0]
        self.assertEqual(temporal["usage"], "current_retrospective")
        self.assertIn("known_after_cutoff", temporal["reasons"])
        self.assertEqual(temporal["cutoff"], matches[0]["kickoff"])
        self.assertIsNotNone(audit["venue_temporal"]["max_evidence_available_at"])

    def test_documented_interval_can_pass_and_is_checked_at_both_bounds(self):
        registry, _ = self.resolve()
        entry = next(v for v in registry["venues"] if v["id"] == NEW_VENUE["id"])
        temporal = entry["verification"]["temporal"]
        temporal.update(valid_from=(NOW - timedelta(days=30)).isoformat(),
                        valid_to=(NOW + timedelta(days=2)).isoformat(),
                        validity_status="documented_interval", basis="reviewed_historical_source",
                        validity_source={"url": "https://example.invalid/synthetic-dated-source",
                            "statement": "Synthetic statement documenting this exact interval.",
                            "source_sha256": "a" * 64, "fetched_at": NOW.isoformat(),
                            "reviewed_at": NOW.isoformat()})
        row = self.row(registry)
        self.assertTrue(venue_temporal_audit(row, registry, cutoff=NOW, strict=True)["eligible"])
        for valid_at in (NOW - timedelta(days=31), NOW + timedelta(days=2)):
            with self.assertRaisesRegex(DataError, "outside_documented_validity"):
                venue_temporal_audit(row, registry, cutoff=NOW, valid_at=valid_at, strict=True)

    def test_new_temporal_schema_rejects_fabricated_or_inconsistent_fields(self):
        registry, _ = self.resolve()
        mutations = [lambda t: t.update(valid_from=NOW.isoformat()),
                     lambda t: t.update(known_at=(NOW - timedelta(seconds=1)).isoformat()),
                     lambda t: t.update(known_at=(NOW + timedelta(seconds=1)).isoformat()),
                     lambda t: t.update(source_published_at=NOW.isoformat()),
                     lambda t: t.update(validity_status="documented_interval", basis="reviewed_historical_source",
                                        valid_from=NOW.isoformat()),
                     lambda t: t.pop("valid_to")]
        for mutate in mutations:
            altered = copy.deepcopy(registry)
            entry = next(v for v in altered["venues"] if v["id"] == NEW_VENUE["id"])
            mutate(entry["verification"]["temporal"])
            with self.subTest(mutate=mutate), self.assertRaises(DataError):
                self.row(altered)

    def test_legacy_entry_remains_byte_unchanged_and_strict_history_stays_unknown(self):
        registry, _ = self.resolve()
        entry = next(v for v in registry["venues"] if v["id"] == NEW_VENUE["id"])
        entry["verification"]["schema_version"] = 1
        entry["verification"].pop("temporal")
        entry["verification"]["method"] = "api_football_venue_id_city_country"
        entry["verification"]["name_alias"] = False
        for key in ("scope", "identity_basis", "name_matches", "city_matches"):
            entry["verification"].pop(key)
        current = self.runtime / "registries/nations_venues.json"
        current.write_text(json.dumps(registry, ensure_ascii=False, indent=2) + "\n")
        registry, audit = self.resolve()
        row = self.row(registry)
        row["registry_snapshot"] = audit["registry_snapshot"]
        snapshot = self.runtime / audit["registry_snapshot"]
        original_bytes = snapshot.read_bytes()
        self.assertNotIn("temporal", row["venue_evidence"])
        loaded = load_forecast_registry(self.runtime, row)
        self.assertFalse(validate_nations_forecast(row, loaded))
        with self.assertRaisesRegex(DataError, "historical_validity_unknown"):
            venue_temporal_audit(row, loaded, cutoff=NOW, strict=True)
        self.resolve()
        self.assertEqual(snapshot.read_bytes(), original_bytes)


if __name__ == "__main__":
    unittest.main()
