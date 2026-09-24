"""Venue and identity contracts for the isolated Nations League adapter."""
import copy
from datetime import datetime, timedelta, timezone
import unittest

from football_model.data import DataError
from football_model.nations import prepare_nations, validate_nations_forecast


NOW = datetime(2026, 9, 24, 12, tzinfo=timezone.utc)
REGISTRY = {"teams": {"1": "England", "2": "Scotland", "3": "France"},
            "venues": [{"id": 10, "name": "English Ground", "city": "London", "association": "England"},
                       {"id": 20, "name": "Scottish Ground", "city": "Glasgow", "association": "Scotland"},
                       {"id": None, "name": "French Ground", "city": "Paris", "association": "France"}],
            "source_notes": "Synthetic test-only venue registry."}


def fixture(mid=1, venue=0, status="FT", when=None):
    when = when or NOW - timedelta(days=1)
    location = REGISTRY["venues"][venue]
    return {"fixture": {"id": mid, "date": when.isoformat(), "timestamp": int(when.timestamp()),
                        "status": {"short": status},
                        "venue": {key: location[key] for key in ("id", "name", "city")}},
            "league": {"id": 5, "season": 2024, "round": "League A - 1"},
            "teams": {"home": {"id": 1, "name": "England"},
                      "away": {"id": 2, "name": "Scotland"}},
            "score": {"fulltime": {"home": 2, "away": 1},
                      "extratime": {"home": 5, "away": 3},
                      "penalty": {"home": 4, "away": 5}},
            "goals": {"home": 5, "away": 3}}


class NationsTests(unittest.TestCase):
    def prepare(self, raw, **kwargs):
        return prepare_nations(raw, NOW, registry=REGISTRY, **kwargs)

    def test_home_and_neutral_location_exactly_identified(self):
        matches, upcoming, audit = self.prepare([fixture(), fixture(2, venue=2)])
        self.assertEqual([row["neutral"] for row in matches], [False, True])
        self.assertEqual(matches[1]["venue_evidence"]["association"], "France")
        self.assertIsNone(matches[1]["venue_evidence"]["venue"]["id"])
        self.assertEqual((audit["admitted"], audit["neutral"]), (2, 1))
        self.assertTrue(audit["training_graph"]["connected"])
        self.assertEqual(upcoming, [])

    def test_away_location_excluded_not_mislabelled_neutral(self):
        matches, _, audit = self.prepare([fixture(venue=1)])
        self.assertEqual(matches, [])
        self.assertEqual(audit["excluded"], 1)
        self.assertIn("反向主场", audit["exclusions"][0]["reason"])

    def test_missing_unknown_changed_name_city_or_id_excluded(self):
        values = [None, {}, {"id": 10, "name": "English Ground", "city": "Wrong"},
                  {"id": 10, "name": "Renamed", "city": "London"},
                  {"id": 999, "name": "English Ground", "city": "London"},
                  {"id": True, "name": "English Ground", "city": "London"}]
        for venue in values:
            item = fixture(); item["fixture"]["venue"] = venue
            with self.subTest(venue=venue):
                matches, _, audit = self.prepare([item])
                self.assertEqual(matches, [])
                self.assertEqual(audit["eligible_regulation"], 1)
                self.assertEqual(audit["exclusions"][0]["match_id"], 1)

    def test_future_unknown_and_away_locations_remain_explicitly_unsupported(self):
        rows = [fixture(1, 0, "NS", NOW + timedelta(days=1)),
                fixture(2, 2, "NS", NOW + timedelta(days=2)),
                fixture(3, 1, "NS", NOW + timedelta(days=3)),
                fixture(4, 0, "NS", NOW + timedelta(days=4))]
        rows[3]["fixture"]["venue"] = None
        matches, upcoming, audit = self.prepare(rows)
        self.assertEqual(matches, [])
        self.assertEqual([row.get("neutral") for row in upcoming], [False, True, None, None])
        self.assertTrue(all("venue_error" in row for row in upcoming[2:]))
        self.assertEqual(audit["upcoming_supported_venue"], 2)
        self.assertEqual(audit["upcoming_unsupported_venue"], 2)

    def test_duplicate_venue_conflict_rejected_before_normalizer(self):
        with self.assertRaisesRegex(DataError, "场地.*冲突"):
            self.prepare([fixture(), fixture(venue=2)])
        matches, _, audit = self.prepare([fixture(), fixture()])
        self.assertEqual(len(matches), 1)
        self.assertEqual((audit["raw_records"], audit["unique_raw_fixtures"], audit["duplicate_records"]), (2, 1, 1))

    def test_duplicate_score_conflict_still_rejected(self):
        changed = fixture(); changed["score"]["fulltime"]["home"] = 3
        with self.assertRaisesRegex(DataError, "冲突"):
            self.prepare([fixture(), changed])

    def test_team_identity_and_wrong_competition_rejected(self):
        for mutate in (lambda x: x["teams"]["home"].update(name="Scotland"),
                       lambda x: x["teams"]["home"].update(id=99),
                       lambda x: x["league"].update(id=39),
                       lambda x: x["league"].update(season=None)):
            item = fixture(); mutate(item)
            with self.assertRaises(DataError):
                self.prepare([item])

    def test_aet_pen_and_four_hour_delay_reuse_regulation_contract(self):
        rows = [fixture(1, status="AET"), fixture(2, status="PEN"),
                fixture(3, when=NOW - timedelta(hours=4)),
                fixture(4, when=NOW - timedelta(hours=4) + timedelta(seconds=1)),
                fixture(5, status="AWD"), fixture(6, status="CANC")]
        matches, _, audit = self.prepare(rows)
        self.assertEqual([r["match_id"] for r in matches], [1, 2, 3])
        self.assertTrue(all((r["home_goals"], r["away_goals"]) == (2, 1) for r in matches))
        self.assertEqual(audit["eligible_regulation"], 3)
        rows[0]["score"]["fulltime"]["home"] = None
        with self.assertRaisesRegex(DataError, "常规时间"):
            self.prepare(rows)

    def test_horizon_and_started_match_exclusion_unchanged(self):
        rows = [fixture(1, status="NS", when=NOW),
                fixture(2, status="NS", when=NOW + timedelta(days=7)),
                fixture(3, status="NS", when=NOW + timedelta(days=7, seconds=1))]
        _, upcoming, _ = self.prepare(rows)
        self.assertEqual([r["match_id"] for r in upcoming], [2])

    def test_audit_counts_close_by_edition_and_stage(self):
        rows = [fixture(1), fixture(2, 1), fixture(3, 2)]
        rows[2]["league"].update(season=2022, round="Final")
        _, _, audit = self.prepare(rows)
        self.assertEqual(audit["eligible_regulation"], audit["admitted"] + audit["excluded"])
        self.assertEqual(audit["by_season"]["2024"], {"eligible_regulation": 2, "admitted": 1, "excluded": 1, "neutral": 0})
        self.assertEqual(audit["by_stage"]["Final"]["neutral"], 1)

    def test_registry_rejects_duplicate_tuple_and_bad_team_ids(self):
        registry = copy.deepcopy(REGISTRY)
        registry["venues"].append(copy.deepcopy(registry["venues"][0]))
        with self.assertRaisesRegex(DataError, "重复场地"):
            prepare_nations([fixture()], NOW, registry=registry)
        registry = copy.deepcopy(REGISTRY); registry["teams"]["01"] = "Another"
        with self.assertRaisesRegex(DataError, "国家队ID"):
            prepare_nations([fixture()], NOW, registry=registry)
        registry = copy.deepcopy(REGISTRY); registry["venues"][0]["association"] = "United Kingdom"
        with self.assertRaisesRegex(DataError, "所属协会"):
            prepare_nations([fixture()], NOW, registry=registry)

    def test_adapter_does_not_mutate_raw_or_registry(self):
        raw, registry = [fixture()], copy.deepcopy(REGISTRY)
        original = copy.deepcopy(raw)
        prepare_nations(raw, NOW, registry=registry)
        self.assertEqual(raw, original)
        self.assertEqual(registry, REGISTRY)

    def test_saved_venue_validation_detects_tampering_or_changed_registry(self):
        _, upcoming, _ = self.prepare([fixture(1, 2, "NS", NOW + timedelta(days=1))])
        row = upcoming[0]
        self.assertTrue(validate_nations_forecast(row, REGISTRY))
        for mutate in (lambda x: x.update(neutral=False),
                       lambda x: x.update(neutral=1),
                       lambda x: x["venue_evidence"].update(association="England"),
                       lambda x: x["venue_evidence"].update(registry_sha256="0" * 64),
                       lambda x: x.update(home_name="Scotland")):
            changed = copy.deepcopy(row); mutate(changed)
            with self.assertRaises(DataError):
                validate_nations_forecast(changed, REGISTRY)
        registry = copy.deepcopy(REGISTRY); registry["source_notes"] = "Changed registry version"
        with self.assertRaises(DataError):
            validate_nations_forecast(row, registry)


if __name__ == "__main__":
    unittest.main()
