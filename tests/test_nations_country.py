"""Country-level admission is isolated, evidence-bound and reproducible."""
import copy
from datetime import timedelta
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from football_model import match_locations
from football_model.data import APIError, DataError
from football_model.nations import prepare_nations, validate_nations_forecast, venue_temporal_audit
from football_model.venue_registry import load_forecast_registry, resolve_venues
from test_venue_registry import NOW, NEW_VENUE, KNOWN_VENUE, VenueClient, fixture, provider_venue


def official(item, association="England"):
    statement = "Synthetic official match notice confirms the bound teams, date and host association."
    return {**match_locations.binding(item), "association": association, "verified_at": NOW.isoformat(),
            "sources": [{"url": "https://example.invalid/official-match", "publisher": "Synthetic FA",
                         "statement": statement, "source_sha256": hashlib.sha256(statement.encode()).hexdigest(),
                         "fetched_at": NOW.isoformat(), "source_published_at": None}]}


class CountryAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.runtime = self.root / "runtime"
        self.seed = self.root / "official.json"
        self.write_seed([])
        mocked = patch("football_model.match_locations.SEED_PATH", self.seed)
        mocked.start()
        self.addCleanup(mocked.stop)

    def write_seed(self, entries):
        self.seed.write_text(json.dumps({"schema_version": 1, "matches": entries}))

    def resolve(self, raw, client=None):
        return resolve_venues(raw, client or VenueClient(), self.runtime, NOW, clock=lambda: NOW)

    def row(self, raw, registry, audit=None):
        row = prepare_nations(raw, NOW, registry=registry)[1][0]
        if audit:
            row["registry_snapshot"] = audit["registry_snapshot"]
        return row

    def test_id_city_spelling_does_not_block_or_claim_precise_stadium(self):
        raw = [fixture(venue=NEW_VENUE)]
        registry, audit = self.resolve(raw, VenueClient([provider_venue(city="Different City Spelling")]))
        row = self.row(raw, registry, audit)
        self.assertFalse(validate_nations_forecast(row, load_forecast_registry(self.runtime, row)))
        evidence = audit["added"][0]["entry"]["verification"]
        self.assertEqual(evidence["schema_version"], 3)
        self.assertEqual(evidence["scope"], "football_association_only")
        self.assertFalse(evidence["city_matches"])
        self.assertNotIn("city_alias_evidence", evidence)
        self.assertNotIn("name_alias", evidence)
        self.assertEqual(row["venue_evidence"]["scope"], "football_association_only")

    def test_unique_exact_name_country_does_not_require_city_match(self):
        raw = [fixture(venue={**NEW_VENUE, "id": 0})]
        registry, audit = self.resolve(raw, VenueClient([provider_venue(city="Provider City")]))
        row = self.row(raw, registry)
        self.assertEqual(row["venue_evidence"]["method"], "api_football_exact_name_country")
        self.assertFalse(row["neutral"])
        self.assertEqual(audit["counts"]["added"], 1)

    def test_missing_city_does_not_block_reliable_name_and_country(self):
        for vid in (NEW_VENUE["id"], 0):
            with self.subTest(vid=vid):
                raw = [fixture(venue={**NEW_VENUE, "id": vid, "city": None})]
                provider = provider_venue(); provider.pop("city")
                registry, audit = self.resolve(raw, VenueClient([provider]))
                row = self.row(raw, registry, audit)
                self.assertFalse(validate_nations_forecast(row, load_forecast_registry(self.runtime, row)))
                self.assertIsNone(row["venue_evidence"]["venue"]["city"])

    def test_country_disagreement_and_ambiguous_name_candidates_still_reject(self):
        raw = [fixture(venue={**KNOWN_VENUE, "city": "London Alternate"})]
        _, audit = self.resolve(raw, VenueClient([provider_venue(KNOWN_VENUE, country="France")]))
        self.assertEqual(audit["pending"][0]["reason_code"], "registered_id_conflict")
        raw = [fixture(venue={**NEW_VENUE, "id": 0})]
        registry, audit = self.resolve(raw, VenueClient([provider_venue(), provider_venue(country="France", id=900002)]))
        self.assertIn("venue_error", self.row(raw, registry))

    def test_obviously_wrong_id_is_not_country_evidence(self):
        observed = {"id": 1971, "name": "Puskás Aréna", "city": "Budapest"}
        response = {"id": 1971, "name": "Windsor Park", "city": "Belfast", "country": "Northern-Ireland"}
        registry, audit = self.resolve([fixture(venue=observed)], VenueClient([response]))
        self.assertEqual(audit["pending"][0]["reason_code"], "provider_identity_unreliable")
        self.assertIn("venue_error", self.row([fixture(venue=observed)], registry))

    def test_independent_known_country_can_corroborate_wrong_id_with_same_country(self):
        observed = {"id": 566, "name": "Wembley Stadium", "city": "London"}
        response = {"id": 566, "name": "The City Ground", "city": "Nottingham", "country": "England"}
        registry, audit = self.resolve([fixture(venue=observed)], VenueClient([response]))
        self.assertEqual(audit["added"][0]["entry"]["verification"]["identity_basis"]["method"],
                         "independent_reviewed_fixture_tuple")
        self.assertFalse(self.row([fixture(venue=observed)], registry)["neutral"])

    def test_conservative_name_normalization_keeps_country_only_claim(self):
        observed = {"id": 900110, "name": "Zimbru", "city": "Chisinau"}
        response = {"id": 900110, "name": "Stadionul Zimbru", "city": "Chişinău", "country": "Moldova"}
        registry, audit = self.resolve([fixture(venue=observed)], VenueClient([response]))
        self.assertEqual(audit["added"][0]["entry"]["association"], "Moldova")
        self.assertTrue(self.row([fixture(venue=observed)], registry)["neutral"])

    def test_official_match_evidence_handles_missing_venue_and_api_failure(self):
        for venue in (None, {"id": 0, "name": "Unknown Ground", "city": None}, NEW_VENUE):
            with self.subTest(venue=venue):
                raw = [fixture(venue=venue)]
                self.write_seed([official(raw[0], "France")])
                registry, audit = self.resolve(raw, VenueClient(error=APIError("synthetic unavailable")))
                row = self.row(raw, registry, audit)
                self.assertTrue(validate_nations_forecast(row, load_forecast_registry(self.runtime, row)))
                self.assertEqual(row["venue_evidence"]["method"], match_locations.METHOD)
                self.assertEqual(row["venue_evidence"]["venue"], venue)

    def test_official_in_away_association_is_not_silently_neutral(self):
        raw = [fixture(venue=None)]
        self.write_seed([official(raw[0], "Wales")])
        registry, _ = self.resolve(raw)
        self.assertIn("反向主场", self.row(raw, registry)["venue_error"])

    def test_official_country_disagrees_with_trusted_provider_and_is_rejected(self):
        raw = [fixture(venue=NEW_VENUE)]
        self.write_seed([official(raw[0], "France")])
        registry, _ = self.resolve(raw)
        self.assertIn("国家冲突", self.row(raw, registry)["venue_error"])

    def test_registered_country_conflict_survives_next_run_api_failure(self):
        venue = {**KNOWN_VENUE, "city": "Alternate London"}
        raw = [fixture(venue=venue)]
        self.write_seed([official(raw[0], "France")])
        registry, _ = self.resolve(raw, VenueClient([provider_venue(KNOWN_VENUE, country="France")]))
        self.assertIn("冲突", self.row(raw, registry)["venue_error"])
        registry, _ = self.resolve(raw, VenueClient(error=APIError("synthetic unavailable")))
        self.assertIn("冲突", self.row(raw, registry)["venue_error"])

    def test_official_binding_rejects_changed_kickoff_teams_venue_and_competition(self):
        raw = [fixture(venue=None)]
        self.write_seed([official(raw[0])])
        registry, _ = self.resolve(raw)
        row = self.row(raw, registry)
        for mutate in (lambda r: r.update(match_id=2),
                       lambda r: r.update(kickoff=(NOW + timedelta(days=2)).isoformat()),
                       lambda r: r.update(home_id=2, home_name="France"),
                       lambda r: r["venue_evidence"].update(venue=NEW_VENUE),
                       lambda r: r["venue_evidence"]["match_binding"].update(season=2024),
                       lambda r: r["venue_evidence"]["match_binding"].update(league_id=39)):
            changed = copy.deepcopy(row); mutate(changed)
            with self.subTest(changed=changed), self.assertRaises(DataError):
                validate_nations_forecast(changed, registry)
        moved = copy.deepcopy(raw); moved[0]["fixture"]["venue"] = NEW_VENUE
        self.assertIn("已变化", self.row(moved, registry)["venue_error"])

    def test_official_source_time_digest_uk_and_competition_validation(self):
        item = fixture(venue=None)
        mutations = (lambda e: e.update(association="United Kingdom"),
                     lambda e: e.update(league_id=39),
                     lambda e: e["sources"][0].update(statement="Tampered original statement"),
                     lambda e: e["sources"][0].update(fetched_at=(NOW + timedelta(seconds=1)).isoformat()))
        for mutate in mutations:
            entry = official(item); mutate(entry); self.write_seed([entry])
            with self.subTest(entry=entry), self.assertRaises(DataError):
                self.resolve([item])

    def test_official_temporal_validity_is_match_specific_and_not_backdated(self):
        raw = [fixture(venue=None)]
        self.write_seed([official(raw[0])])
        registry, _ = self.resolve(raw)
        row = self.row(raw, registry)
        self.assertTrue(venue_temporal_audit(row, registry, cutoff=NOW, strict=True)["eligible"])
        with self.assertRaisesRegex(DataError, "known_after_cutoff"):
            venue_temporal_audit(row, registry, cutoff=NOW - timedelta(seconds=1), strict=True)
        with self.assertRaisesRegex(DataError, "outside_documented_validity"):
            venue_temporal_audit(row, registry, cutoff=NOW, valid_at=NOW, strict=True)

    def test_historical_official_location_uses_same_classifier_with_retrospective_label(self):
        item = fixture(venue=None, status="FT", when=NOW - timedelta(days=3))
        self.write_seed([official(item, "France")])
        registry, _ = self.resolve([item])
        matches, _, audit = prepare_nations([item], NOW, registry=registry)
        self.assertTrue(matches[0]["neutral"])
        self.assertIn("known_after_cutoff", audit["venue_temporal"]["by_match"][0]["reasons"])

    def test_frozen_official_forecast_does_not_change_with_later_seed(self):
        raw = [fixture(venue=None)]
        self.write_seed([official(raw[0])])
        registry, audit = self.resolve(raw)
        row = self.row(raw, registry, audit)
        snapshot = self.runtime / row["registry_snapshot"]
        original = snapshot.read_bytes()
        self.write_seed([official(raw[0], "France")])
        newer, _ = self.resolve(raw)
        self.assertTrue(self.row(raw, newer)["neutral"])
        self.assertEqual(snapshot.read_bytes(), original)
        self.assertFalse(validate_nations_forecast(row, load_forecast_registry(self.runtime, row)))


if __name__ == "__main__":
    unittest.main()
