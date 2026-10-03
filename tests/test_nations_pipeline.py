"""National pipeline/access regression tests; fixtures and scores are synthetic.

Provider identities and venue tuples are real registry entries, not real games.
Fitting is exercised with actual optimization and fixed temporal validation.
"""
import copy
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

from football_model import __version__
from football_model import cli
from football_model.cli import atomic_json, input_fingerprint, iso, run_pipeline
from football_model.integration import IntegrationError, build_index, match_packet
from football_model.model import fit_model, score_distribution


NOW = datetime(2026, 9, 1, tzinfo=timezone.utc)
CONFIG = {
    "history_seasons": 1, "max_requests_per_run": 80, "half_life_days": 180,
    "ridge": 3, "min_train": 60, "min_test": 15, "forecast_days": 7,
    "max_model_age_days": 14,
    "competitions": [{"key": "unl", "label": "合成欧国联测试",
                      "name": "UEFA Nations League", "country": "World",
                      "mode": "national_baseline"}],
}
TEAMS = [(10, "England"), (767, "Wales"), (1108, "Scotland"),
         (771, "Northern Ireland"), (2, "France"), (25, "Germany")]
VENUES = {
    "England": {"id": 489, "name": "Wembley Stadium", "city": "London"},
    "Wales": {"id": None, "name": "Cardiff City Stadium", "city": "Cardiff"},
    "Scotland": {"id": 2617, "name": "Hampden Park", "city": "Glasgow"},
    "Northern Ireland": {"id": 1971, "name": "Windsor Park", "city": "Belfast"},
    "France": {"id": 671, "name": "Parc des Princes", "city": "Paris"},
    "Germany": {"id": 700, "name": "Allianz Arena", "city": "München"},
}


def fixture(mid, kickoff, home, away, goals, venue, status="FT"):
    return {"fixture": {"id": mid, "date": iso(kickoff),
                        "timestamp": int(kickoff.timestamp()),
                        "status": {"short": status}, "venue": copy.deepcopy(venue)},
            "league": {"id": 5, "season": 2024, "round": "Synthetic group stage"},
            "teams": {"home": {"id": home[0], "name": home[1]},
                      "away": {"id": away[0], "name": away[1]}},
            "score": {"fulltime": {"home": goals[0], "away": goals[1]}}}


class NationalClient:
    """All requests stay in memory; later calls keep the same historic scores."""
    def __init__(self, now=NOW, include_upcoming=True):
        self.now = now
        self.requests_made = 0
        self.fetch_calls = []
        self.fixtures = []
        rng = np.random.default_rng(726)
        for i in range(180):
            home = TEAMS[i % 6]
            away = TEAMS[(i % 6 + 1 + (i // 6) % 5) % 6]
            neutral = i % 5 == 0
            venue_association = next(name for _, name in TEAMS
                                     if name not in (home[1], away[1])) if neutral else home[1]
            goals = (int(rng.poisson(1.2 if neutral else 1.9)), int(rng.poisson(0.9)))
            self.fixtures.append(fixture(i + 1, NOW - timedelta(days=190-i),
                                         home, away, goals, VENUES[venue_association]))
        if include_upcoming:
            kickoff = now + timedelta(days=2)
            self.fixtures.extend([
                fixture(2000, kickoff, TEAMS[0], TEAMS[1], (None, None), VENUES["England"], "NS"),
                fixture(2001, kickoff, TEAMS[4], TEAMS[5], (None, None), VENUES["England"], "NS"),
                fixture(2002, kickoff, TEAMS[2], TEAMS[3], (None, None), None, "NS"),
            ])

    def status(self):
        self.requests_made += 1
        return {}

    def resolve_league(self, competition):
        self.requests_made += 1
        return {"league": {"id": 5, "name": "UEFA Nations League"},
                "seasons": [{"year": 2024, "start": "2024-09-05"}]}

    def fetch_fixtures(self, league_id, season, **kwargs):
        self.requests_made += 1
        self.fetch_calls.append((league_id, season, kwargs))
        return {"response": copy.deepcopy(self.fixtures), "fetched_at": iso(self.now),
                "from_cache": False}


class NationalPipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base = tempfile.TemporaryDirectory()
        code, cls.report = run_pipeline(CONFIG, cls.base.name, "train", league="unl",
                                        now=NOW, client=NationalClient(), clock=lambda: NOW)
        if code:
            raise AssertionError(cls.report)
        cls.run_id = cls.report["run_id"]

    @classmethod
    def tearDownClass(cls):
        cls.base.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.runtime = Path(self.temp.name) / "runtime"
        shutil.copytree(self.base.name, self.runtime)
        self.model_path = self.runtime / "models" / "unl.json"
        self.prediction_path = self.runtime / "predictions" / self.run_id / "unl.json"

    def tearDown(self):
        self.temp.cleanup()

    def read(self, path):
        return json.loads(path.read_text(encoding="utf-8"))

    def packet(self, mid=2001, asof=None):
        # These tests exercise current forecast use. A strict historical
        # replay requires independently documented venue validity intervals.
        with patch("football_model.integration.datetime", wraps=datetime) as observed_clock:
            observed_clock.now.return_value = asof or NOW + timedelta(hours=1)
            return match_packet(self.runtime, CONFIG, "unl", mid)

    def test_actual_fit_stores_isolated_model_validation_and_audit(self):
        bundle = self.read(self.model_path)
        model = bundle["model"]
        item = self.report["competitions"][0]
        self.assertEqual(item["model_change"], "完成新拟合")
        self.assertEqual((item["eligible_matches"], item["forecast_count"],
                          item["unsupported_forecasts"]), (180, 2, 1))
        self.assertEqual((model["fit_status"], model["n_matches"], model["n_teams"]),
                         ("converged", 180, 6))
        self.assertEqual(set(model["team_parameters"]), {str(team[0]) for team in TEAMS})
        self.assertGreater(abs(model["home_advantage"]), 0.1)
        self.assertFalse(model["validated_for_production"])
        self.assertTrue(bundle["validation"]["meets_minimum_supported_test_matches"])
        self.assertFalse(bundle["validation"]["deployment_approved"])
        audit = self.read(self.runtime / "audits" / "unl" / f"{self.run_id}.json")
        self.assertEqual(audit, bundle["national_audit"])
        self.assertEqual((audit["admitted"], audit["neutral"], audit["excluded"]), (180, 36, 0))
        self.assertEqual(audit["upcoming_unsupported_venue"], 1)
        self.assertEqual(build_index(self.runtime, CONFIG)["competitions"][0]["status"], "available")
        self.assertEqual(list((self.runtime / "models").iterdir()), [self.model_path])

    def test_neutral_saved_prediction_recomputed_with_verified_evidence(self):
        before = self.model_path.read_bytes()
        model = self.read(self.model_path)["model"]
        packet = self.packet()
        prediction = packet["prediction"]
        self.assertTrue(packet["fixture"]["neutral"])
        self.assertEqual(packet["fixture"]["venue_evidence"]["association"], "England")
        self.assertEqual(packet["fixture"]["venue_evidence"]["classification"],
                         "third_association_neutral")
        actual = score_distribution(model, 2, 25, prediction["kickoff"], neutral=True)
        nominal_home = score_distribution(model, 2, 25, prediction["kickoff"], neutral=False)
        self.assertEqual(prediction, actual)
        self.assertNotAlmostEqual(prediction["expected_goals"]["home"],
                                  nominal_home["expected_goals"]["home"], places=3)
        self.assertAlmostEqual(sum(map(sum, prediction["score_matrix"])), 1.0, places=12)
        self.assertTrue(packet["provenance"]["read_and_recomputed"])
        self.assertFalse(packet["provenance"]["fit_performed"])
        self.assertEqual(packet["external_checks"]["actual_venue_and_neutral_status"], "pending")
        self.assertEqual(before, self.model_path.read_bytes())
        self.assertFalse(self.packet(2000)["prediction"]["neutral"])

    def test_unknown_venue_produces_explicit_unsupported_row_without_probability(self):
        row = next(row for row in self.read(self.prediction_path) if row["match_id"] == 2002)
        self.assertEqual(row["status"], "unsupported")
        self.assertIn("场地", row["reason"])
        self.assertNotIn("prediction", row)
        with self.assertRaisesRegex(IntegrationError, "本场不支持"):
            self.packet(2002)

    def test_saved_forecast_without_venue_evidence_is_rejected(self):
        rows = self.read(self.prediction_path)
        next(row for row in rows if row["match_id"] == 2001).pop("venue_evidence")
        atomic_json(self.prediction_path, rows)
        with self.assertRaisesRegex(IntegrationError, "场地"):
            self.packet()

    def test_forged_non_neutral_prediction_cannot_override_registered_location(self):
        rows = self.read(self.prediction_path)
        row = next(row for row in rows if row["match_id"] == 2001)
        row["neutral"] = False
        row["prediction"] = score_distribution(self.read(self.model_path)["model"],
                                                row["home_id"], row["away_id"],
                                                row["kickoff"], neutral=False)
        atomic_json(self.prediction_path, rows)
        with self.assertRaisesRegex(IntegrationError, "场地"):
            self.packet()

    def test_modified_registry_digest_in_saved_evidence_is_rejected(self):
        rows = self.read(self.prediction_path)
        row = next(row for row in rows if row["match_id"] == 2001)
        row["venue_evidence"]["registry_sha256"] = "0" * 64
        atomic_json(self.prediction_path, rows)
        with self.assertRaisesRegex(IntegrationError, "场地"):
            self.packet()

    def test_fresh_same_data_reuses_model_and_validation_without_refitting(self):
        before = self.model_path.read_bytes()
        now = NOW + timedelta(hours=2)
        with patch("football_model.cli.fit_model", side_effect=AssertionError("unexpected refit")), \
                patch("football_model.cli.evaluate_time_holdout", side_effect=AssertionError("unexpected validation")):
            code, report = run_pipeline(CONFIG, self.runtime, "train", league="unl",
                                        now=now, client=NationalClient(now), clock=lambda: now)
        self.assertEqual(code, 0, report)
        self.assertEqual(report["competitions"][0]["model_change"], "数据未变，沿用参数")
        self.assertEqual(before, self.model_path.read_bytes())
        self.assertEqual(len(list((self.runtime / "model_history" / "unl").glob("*.json"))), 1)
        packet = self.packet(asof=now + timedelta(minutes=1))
        self.assertEqual(packet["provenance"]["trained_at"], iso(NOW))
        self.assertEqual(packet["provenance"]["forecast_generated_at"], iso(now))

    def test_stale_same_data_with_upcoming_performs_real_fit_at_current_cutoff(self):
        self.assert_actual_window_refresh(15)

    def test_model_valid_now_but_expired_at_kickoff_is_actually_refitted(self):
        self.assert_actual_window_refresh(13)

    def assert_actual_window_refresh(self, elapsed_days):
        old = self.read(self.model_path)
        now = NOW + timedelta(days=elapsed_days)
        with patch("football_model.cli.fit_model", wraps=fit_model) as actual_fit:
            code, report = run_pipeline(CONFIG, self.runtime, "train", league="unl",
                                        now=now, client=NationalClient(now), clock=lambda: now)
        self.assertEqual(code, 0, report)
        actual_fit.assert_called_once()
        self.assertEqual(actual_fit.call_args.args[1], iso(now))
        refreshed = self.read(self.model_path)
        self.assertEqual(old["data_fingerprint"], refreshed["data_fingerprint"])
        self.assertEqual(old["model"]["training_data_sha256"], refreshed["model"]["training_data_sha256"])
        self.assertEqual(refreshed["trained_at"], iso(now))
        self.assertEqual(refreshed["model"]["training_cutoff"], iso(now))
        self.assertNotEqual(old["model_id"], refreshed["model_id"])
        self.assertNotEqual(old["model"]["team_parameters"], refreshed["model"]["team_parameters"])
        self.assertEqual(len(list((self.runtime / "model_history" / "unl").glob("*.json"))), 2)
        self.assertEqual(report["competitions"][0]["forecast_count"], 2)
        self.assertEqual(self.packet(asof=now + timedelta(hours=1))["provenance"]["trained_at"], iso(now))

    def test_stale_without_upcoming_does_not_refit_or_fake_freshness(self):
        before = self.model_path.read_bytes()
        now = NOW + timedelta(days=15)
        with patch("football_model.cli.fit_model", side_effect=AssertionError("unexpected refit")):
            code, report = run_pipeline(CONFIG, self.runtime, "train", league="unl", now=now,
                                        client=NationalClient(now, include_upcoming=False), clock=lambda: now)
        self.assertEqual(code, 0, report)
        self.assertEqual(report["competitions"][0]["status"], "模型陈旧")
        self.assertEqual(before, self.model_path.read_bytes())
        self.assertFalse((self.runtime / "predictions" / report["run_id"] / "unl.json").exists())

    def test_domestic_fingerprint_is_byte_compatible_with_original_payload(self):
        # This is the original pre-national fingerprint protocol, not the adapter.
        competition = {"key": "epl", "label": "英超", "name": "Premier League",
                       "country": "England", "mode": "domestic_baseline"}
        knobs = {key: CONFIG[key] for key in ("half_life_days", "ridge", "min_train", "min_test")}
        matches = [{"match_id": 1, "kickoff": iso(NOW - timedelta(days=1)),
                    "home_id": 1, "away_id": 2, "home_name": "Domestic A",
                    "away_name": "Domestic B", "home_goals": 2, "away_goals": 1,
                    "neutral": False}]
        payload = {"version": __version__,
                   "source": hashlib.sha256(Path(cli.__file__).with_name("model.py").read_bytes()).hexdigest(),
                   "competition": competition, "settings": knobs, "matches": matches}
        original = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        self.assertEqual(input_fingerprint(matches, knobs, competition), original)


class AutomaticallyRegisteredVenueClient(NationalClient):
    """A new future venue has no matching training fixture; provider verifies it."""
    NEW_GROUND = {"id": 910123, "name": "Synthetic Unseen National Ground", "city": "London"}

    def __init__(self, now=NOW):
        super().__init__(now)
        self.venue_calls = []
        self.fixtures.append(fixture(2003, now + timedelta(days=2), TEAMS[0], TEAMS[1],
                                     (None, None), self.NEW_GROUND, "NS"))

    def fetch_venues(self, *, venue_id=None, name=None, **kwargs):
        self.requests_made += 1
        self.venue_calls.append((venue_id, name))
        return {"response": [{**self.NEW_GROUND, "country": "England"}],
                "fetched_at": iso(self.now), "from_cache": False, "endpoint": "/venues",
                "params": {"id": venue_id} if venue_id is not None else {"name": name}}


class AutomaticallyRegisteredVenuePipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.runtime = Path(self.temp.name) / "runtime"
        self.client = AutomaticallyRegisteredVenueClient()
        code, self.report = run_pipeline(CONFIG, self.runtime, "train", league="unl",
                                         now=NOW, client=self.client, clock=lambda: NOW)
        self.assertEqual(code, 0, self.report)

    def read(self, path):
        return json.loads(path.read_text(encoding="utf-8"))

    def packet(self):
        with patch("football_model.integration.datetime", wraps=datetime) as observed_clock:
            observed_clock.now.return_value = NOW + timedelta(hours=1)
            return match_packet(self.runtime, CONFIG, "unl", 2003)

    def test_new_stadium_absent_from_training_produces_verified_baseline(self):
        item = self.report["competitions"][0]
        bundle = self.read(self.runtime / "models" / "unl.json")
        rows = self.read(self.runtime / "predictions" / self.report["run_id"] / "unl.json")
        row = next(row for row in rows if row["match_id"] == 2003)
        self.assertEqual(item["eligible_matches"], 180)
        self.assertEqual((item["forecast_count"], item["unsupported_forecasts"]), (3, 1))
        self.assertEqual(bundle["model"]["n_matches"], 180)
        self.assertEqual(self.client.venue_calls, [(910123, None)])
        self.assertEqual(row["status"], "exploratory_baseline")
        self.assertEqual(row["venue_evidence"]["venue"], self.client.NEW_GROUND)
        self.assertFalse(row["neutral"])
        self.assertTrue((self.runtime / row["registry_snapshot"]).is_file())
        packet = self.packet()
        actual = score_distribution(bundle["model"], 10, 767, row["kickoff"], neutral=False)
        self.assertEqual(packet["prediction"], actual)
        self.assertTrue(packet["provenance"]["read_and_recomputed"])
        self.assertFalse(packet["provenance"]["fit_performed"])

    def test_saved_forecast_uses_its_snapshot_after_registry_grows(self):
        from football_model.venue_registry import resolve_venues
        before = self.packet()
        other = {"id": 910124, "name": "Synthetic Second Unseen Ground", "city": "London"}

        class AdditionalVenueClient:
            requests_made = 0

            def fetch_venues(self, **kwargs):
                self.requests_made += 1
                return {"response": [{**other, "country": "England"}],
                        "fetched_at": iso(NOW), "from_cache": False, "endpoint": "/venues",
                        "params": {"id": 910124}}

        new_raw = [fixture(2004, NOW + timedelta(days=3), TEAMS[0], TEAMS[1],
                           (None, None), other, "NS")]
        _, audit = resolve_venues(new_raw, AdditionalVenueClient(), self.runtime, NOW, clock=lambda: NOW)
        self.assertEqual(len(audit["added"]), 1)
        after = self.packet()
        self.assertEqual(before["prediction"], after["prediction"])
        self.assertTrue(after["provenance"]["read_and_recomputed"])


if __name__ == "__main__":
    unittest.main()
