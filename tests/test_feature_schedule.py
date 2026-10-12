import copy
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from football_model.data import APIClient, APIError, DataError, RequestBudgetError, _utc
from football_model.feature_schedule import collect_schedule, load_schedule, _metadata, _diagnostic_code


NOW = datetime(2026, 10, 12, 12, tzinfo=timezone.utc)
CONFIG = {"api_daily_limit": 5000, "max_requests_per_run": None, "api_max_run_seconds": 3600,
          "competitions": [{"key": "serie_a", "name": "Serie A", "country": "Italy",
                            "mode": "domestic_baseline"}]}


def fixture(mid, year=2026, lid=135, name="Serie A", *, status="FT", score=True):
    kickoff = NOW - timedelta(days=2 if year == 2026 else 400)
    return {"fixture": {"id": mid, "date": kickoff.isoformat(), "timestamp": int(kickoff.timestamp()),
                        "status": {"short": status, "long": status}},
            "league": {"id": lid, "name": name, "country": "Italy", "season": year},
            "teams": {"home": {"id": 11, "name": "Home"}, "away": {"id": 22, "name": "Away"}},
            "score": {"fulltime": {"home": 2 if score else None, "away": 1 if score else None}}}


def envelope(endpoint, params, rows):
    return {"endpoint": endpoint, "params": params, "response": rows, "errors": [],
            "paging": {"current": 1, "total": 1}, "fetched_at": NOW.isoformat(), "from_cache": False}


class FakeClient:
    def __init__(self):
        self.requests_made = 0
        self.calls = []
        self.rows = {2026: [fixture(1)], 2025: [fixture(2, 2025)]}
        self.team_rows = copy.deepcopy(self.rows)
        self.failures = {}
        self.reserve = None

    def status(self):
        self.requests_made += 1
        self.calls.append(("/status", {}))
        return {"requests": {"current": 4100, "limit_day": 5000}}

    def resolve_league(self, competition, force=False):
        return {"league": {"id": 135, "name": "Serie A", "type": "League"}, "fetched_at": NOW.isoformat()}

    @contextmanager
    def reserve_requests(self, count):
        self.reserve = count
        yield

    def fetch_fixtures(self, lid, year, **options):
        self.calls.append(("league_fixtures", {"league": lid, "season": year}, options))
        return envelope("/fixtures", {"league": lid, "season": year}, self.rows[year])

    def _cached_request(self, endpoint, params, **options):
        self.requests_made += 1
        self.calls.append((endpoint, params, options))
        key = (endpoint, tuple(sorted(params.items())))
        if key in self.failures:
            value = self.failures[key]
            if isinstance(value, Exception):
                raise value
            return value
        if endpoint == "/fixtures":
            return envelope(endpoint, params, self.team_rows[params["season"]])
        if endpoint == "/leagues":
            lid = params["id"]
            name = "Serie A" if lid == 135 else "Coppa Italia"
            return envelope(endpoint, params, [{"league": {"id": lid, "name": name,
                                                          "type": "League" if lid == 135 else "Cup"},
                                               "seasons": [{"year": params["season"]}]}])
        raise AssertionError("unexpected_endpoint")

    def budget_snapshot(self):
        return {"configured_daily_limit": 5000, "provider_daily_used": 4100,
                "daily_remaining": 900, "daily_quota_status": "known",
                "max_requests_per_run": None, "requests_made": self.requests_made}


class ScheduleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.runtime = Path(self.temp.name)
        (self.runtime / "data").mkdir()
        rows = []
        for mid, year in ((1, 2026), (2, 2025)):
            raw = fixture(mid, year)
            rows.append({"match_id": mid, "kickoff": raw["fixture"]["date"],
                         "home_id": 11, "away_id": 22, "home_name": "Home", "away_name": "Away",
                         "home_goals": 2, "away_goals": 1})
        (self.runtime / "data" / "serie_a.json").write_text(json.dumps({
            "matches": rows, "requested_seasons": [2026, 2025], "missing_seasons": [],
            "observed_at": NOW.isoformat()}))

    def collect(self, client, **kwargs):
        return collect_schedule(CONFIG, self.runtime, client=client, clock=lambda: NOW,
                                source_commit="a" * 40, **kwargs)

    def test_collects_all_clubs_all_requested_seasons_preserves_scope_and_sources(self):
        client = FakeClient()
        code, report = self.collect(client)
        self.assertEqual(code, 0)
        coverage = report["coverage"]
        self.assertEqual(coverage["expected_team_season_requests"], 4)
        self.assertEqual(coverage["request_status_counts"], {"collected": 4})
        self.assertEqual(coverage["normalized_target_matches_by_season"], {"2026": 1, "2025": 1})
        self.assertTrue(coverage["provider_queries_complete"])
        self.assertFalse(coverage["all_club_competitions_complete"])
        self.assertFalse(coverage["historical_availability_verified"])
        self.assertFalse(coverage["national_team_player_workload_included"])
        self.assertEqual(len(report["schedule_matches"]), 2)  # duplicate club responses deduplicated
        self.assertEqual(client.reserve, 20)
        calls = [c for c in client.calls if c[0] == "/fixtures"]
        self.assertTrue(all(c[2]["force"] == (c[1]["season"] == 2026) for c in calls))
        self.assertTrue(all(c[2]["max_age_seconds"] == 7 * 86400 for c in calls))
        matches, saved = load_schedule(self.runtime, "serie_a")
        self.assertEqual(matches, report["schedule_matches"])
        self.assertEqual(saved, coverage)
        evidence = coverage["team_season_requests"][0]["evidence"]
        self.assertTrue((self.runtime / evidence["path"]).is_file())
        self.assertEqual(report["api_budget"]["provider_daily_used"], 4100)

    def test_friendlies_excluded_but_cup_workload_without_regulation_score_retained(self):
        client = FakeClient()
        client.team_rows[2026].extend([
            fixture(3, lid=13, name="Coppa Italia", status="PEN", score=False),
            fixture(4, lid=667, name="Friendlies Clubs"),
            fixture(5, lid=13, name="Coppa Italia", status="AET"),
        ])
        _, report = self.collect(client)
        self.assertEqual({x["match_id"] for x in report["schedule_matches"]}, {1, 2, 5})
        self.assertEqual({x["match_id"] for x in report["coverage"]["workload_events"]}, {1, 2, 3, 5})
        self.assertTrue(any(x["reason"] == "regulation_score_unavailable_workload_only"
                            for x in report["coverage"]["issues"]))
        self.assertFalse(any(c[0] == "/leagues" and c[1].get("id") == 667 for c in client.calls))
        self.assertNotIn("home_goals", next(x for x in report["coverage"]["workload_events"] if x["match_id"] == 3))

    def test_invalid_season_status_team_and_type_make_request_partial(self):
        for defect in ("season", "status", "team", "type"):
            with self.subTest(defect=defect):
                client = FakeClient()
                raw = fixture(7)
                if defect == "season":
                    raw["league"]["season"] = 2020
                elif defect == "status":
                    raw["fixture"]["status"]["short"] = "UNKNOWN"
                elif defect == "team":
                    raw["teams"]["home"]["id"] = 33
                    raw["teams"]["away"]["id"] = 44
                else:
                    raw["league"]["type"] = "National"
                client.team_rows[2026].append(raw)
                code, report = self.collect(client)
                self.assertEqual(code, 1)
                self.assertNotIn(7, [x["match_id"] for x in report["schedule_matches"]])
                self.assertEqual(report["coverage"]["request_status_counts"], {"partial": 2, "collected": 2})
                # Separate test runs have immutable file IDs; update clock below
                # avoids duplicate-run paths in the same fixture test directory.
                for path in (self.runtime / "feature_models").rglob("*.json"):
                    path.unlink()

    def test_conflicting_duplicate_excludes_both_versions(self):
        client = FakeClient()
        params = {"team": 22, "season": 2026}
        changed = fixture(1)
        changed["score"]["fulltime"]["home"] = 3
        client.failures[("/fixtures", tuple(sorted(params.items())))] = envelope("/fixtures", params, [changed])
        code, report = self.collect(client)
        self.assertEqual(code, 1)
        self.assertEqual(report["coverage"]["conflict_match_ids"], [1])
        self.assertEqual([x["match_id"] for x in report["schedule_matches"]], [2])
        self.assertEqual([x["match_id"] for x in report["coverage"]["workload_events"]], [2])

    def test_original_training_score_cannot_silently_change_in_season_join(self):
        client = FakeClient()
        client.rows[2026][0]["score"]["fulltime"]["home"] = 3
        client.team_rows = copy.deepcopy(client.rows)
        code, report = self.collect(client)
        self.assertEqual(code, 1)
        self.assertEqual(report["coverage"]["target_match_identity_or_score_mismatches"], [1])
        self.assertFalse(report["coverage"]["provider_queries_complete"])

    def test_nonempty_club_response_missing_known_league_match_fails_closed(self):
        client = FakeClient()
        client.team_rows[2026] = [fixture(3, lid=13, name="Coppa Italia")]
        code, report = self.collect(client)
        self.assertEqual(code, 1)
        self.assertEqual(report["coverage"]["normalized_target_match_ids_missing_from_club_responses"], [1])
        self.assertEqual(report["coverage"]["request_status_counts"], {"collected": 4})
        self.assertFalse(report["coverage"]["provider_queries_complete"])

    def test_request_failure_keeps_all_requested_entries_and_prior_success(self):
        client = FakeClient()
        params = {"team": 11, "season": 2025}
        client.failures[("/fixtures", tuple(sorted(params.items())))] = RequestBudgetError("opaque budget text")
        code, report = self.collect(client)
        self.assertEqual(code, 1)
        self.assertEqual(len(report["coverage"]["team_season_requests"]), 4)
        self.assertEqual(report["coverage"]["request_status_counts"], {"collected": 3, "deferred": 1})
        self.assertEqual(len(report["schedule_matches"]), 2)
        self.assertNotIn("opaque budget text", json.dumps(report))

    def test_initialization_failure_writes_current_failure_not_old_success(self):
        client = FakeClient()
        client.status = lambda: (_ for _ in ()).throw(APIError("private status diagnostic"))
        code, report = self.collect(client)
        self.assertEqual(code, 1)
        self.assertEqual(report["schedule_matches"], [])
        self.assertEqual(report["coverage"]["request_status_counts"], {"deferred": 4})
        self.assertEqual(load_schedule(self.runtime, "serie_a")[1]["completeness_status"], "partial")
        self.assertNotIn("private status diagnostic", json.dumps(report))

    def test_real_metadata_timestamp_after_request_start_uses_completion_clock(self):
        # Production run38281475496: first club response fetched at .669765,
        # then valid Serie A metadata fetched at .910057. The old comparison
        # froze the former time and incorrectly rejected every new response.
        request_started = _utc("2026-10-12T17:23:01.669765Z")
        metadata_fetched = _utc("2026-10-12T17:23:01.910057Z")
        mutable_clock = [request_started]
        env = envelope("/leagues", {"id": 135, "season": 2026}, [{
            "league": {"id": 135, "name": "Serie A", "type": "League"},
            "country": {"name": "Italy", "code": "IT"},
            "seasons": [{"year": 2026, "start": "2026-08-22", "end": "2027-05-30", "current": True}],
        }])
        env["fetched_at"] = metadata_fetched.isoformat()

        class RequestClient:
            def _cached_request(self, *args, **kwargs):
                mutable_clock[0] = metadata_fetched + timedelta(microseconds=1)
                return env

        result = _metadata(RequestClient(), 135, 2026, lambda: mutable_clock[0],
                           self.runtime / "responses_root", self.runtime)
        self.assertGreater(metadata_fetched, request_started)  # the prior implementation would fail
        self.assertEqual(result["type"], "League")
        self.assertEqual(result["evidence"]["fetched_at"], env["fetched_at"])
        self.assertTrue((self.runtime / result["evidence"]["path"]).is_file())

    def test_truly_future_metadata_still_rejected_and_original_saved(self):
        env = envelope("/leagues", {"id": 135, "season": 2026}, [{
            "league": {"id": 135, "name": "Serie A", "type": "League"},
            "seasons": [{"year": 2026}]}])
        env["fetched_at"] = (NOW + timedelta(seconds=1)).isoformat()

        class Client:
            def _cached_request(self, *args, **kwargs):
                return env

        with self.assertRaisesRegex(DataError, "competition_metadata_observation_in_future") as caught:
            _metadata(Client(), 135, 2026, lambda: NOW, self.runtime / "responses_root", self.runtime)
        evidence = caught.exception.schedule_evidence
        self.assertEqual(json.loads((self.runtime / evidence["path"]).read_text()), env)

    def test_metadata_content_failure_has_safe_code_and_preserved_response(self):
        client = FakeClient()
        params = {"id": 135, "season": 2026}
        bad = envelope("/leagues", params, [{"league": {"id": 135, "name": "Serie A", "type": "Unknown"},
                                              "seasons": [{"year": 2026}]}])
        client.failures[("/leagues", tuple(sorted(params.items())))] = bad
        code, report = self.collect(client)
        self.assertEqual(code, 1)
        failure = report["coverage"]["competition_metadata_failures"][0]
        self.assertEqual(failure["diagnostic_code"], "competition_metadata_type_invalid")
        self.assertTrue((self.runtime / failure["evidence"]["path"]).is_file())
        self.assertEqual(_diagnostic_code(DataError("unit_test_secret")), "validation_or_storage_error")

    def test_empty_and_paginated_cached_responses_never_claim_complete(self):
        client = FakeClient()
        params = {"team": 11, "season": 2026}
        paginated = envelope("/fixtures", params, [fixture(1)])
        paginated["paging"]["total"] = 2
        client.failures[("/fixtures", tuple(sorted(params.items())))] = paginated
        client.team_rows[2025] = []
        code, report = self.collect(client)
        self.assertEqual(code, 1)
        counts = report["coverage"]["request_status_counts"]
        self.assertEqual(counts, {"failed": 1, "empty_unverified": 2, "collected": 1})
        self.assertFalse(report["coverage"]["provider_queries_complete"])

    def test_future_cancelled_and_awarded_fixtures_not_workload(self):
        client = FakeClient()
        future = fixture(8, status="NS")
        when = NOW + timedelta(days=1)
        future["fixture"].update(date=when.isoformat(), timestamp=int(when.timestamp()))
        client.team_rows[2026].extend([future, fixture(9, status="CANC"), fixture(10, status="AWD")])
        _, report = self.collect(client)
        self.assertEqual({x["match_id"] for x in report["schedule_events"]}, {1, 2, 8, 9, 10})
        self.assertEqual({x["match_id"] for x in report["coverage"]["workload_events"]}, {1, 2})

    def test_actual_client_reuses_historical_cache_but_protects_daily_reserve(self):
        class Response(io.BytesIO):
            def __init__(self, value):
                super().__init__(json.dumps(value).encode())
                self.headers = {}

        def response(rows):
            return {"response": rows, "errors": [], "paging": {"current": 1, "total": 1}}

        paid = []

        def opener(req, timeout):
            if req.full_url.endswith("/status"):
                return Response(response({"requests": {"current": 4999, "limit_day": 5000}}))
            paid.append(req.full_url)
            return Response(response([fixture(2, 2025)]))

        with patch.dict("os.environ", {"FOOTBALL_API_KEY": "test-only-not-real-key"}):
            client = APIClient(self.runtime / "cache", opener=opener, daily_limit=5000,
                               clock=lambda: NOW, min_interval=0)
            # Install an exact historical cache retaining its earlier fetched_at.
            params = {"team": 11, "season": 2025}
            import hashlib
            digest = hashlib.sha256(json.dumps(["/fixtures", params], sort_keys=True).encode()).hexdigest()
            cached = envelope("/fixtures", params, [fixture(2, 2025)])
            cached["fetched_at"] = (NOW - timedelta(days=1)).isoformat()
            path = self.runtime / "cache" / "api_cache" / (digest + ".json")
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps(cached))
            client.status()
            with client.reserve_requests(1):
                got = client._cached_request("/fixtures", params, force=False, max_age_seconds=7 * 86400)
                self.assertTrue(got["from_cache"])
                self.assertEqual(got["fetched_at"], cached["fetched_at"])
                with self.assertRaises(RequestBudgetError):
                    client._cached_request("/fixtures", {"team": 22, "season": 2026},
                                           force=True, max_age_seconds=7 * 86400)
            self.assertEqual(paid, [])
            self.assertEqual(client.daily_remaining, 1)


if __name__ == "__main__":
    unittest.main()
