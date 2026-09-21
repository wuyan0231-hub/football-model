import copy
from datetime import datetime, timedelta, timezone
from email.message import Message
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

from football_model.data import APIClient, APIError, DataError, normalize_fixtures, normalize_upcoming


NOW = datetime(2026, 9, 21, 12, tzinfo=timezone.utc)
SECRET = "unit-test-secret-not-a-real-api-key"


def envelope(response, errors=None):
    return {"get": "fixtures", "parameters": {}, "errors": [] if errors is None else errors,
            "paging": {"current": 1, "total": 1}, "response": response}


class Response(io.BytesIO):
    def __init__(self, payload, headers=None):
        super().__init__(json.dumps(payload).encode())
        self.headers = headers or {}


def fixture(mid=1, status="FT", kickoff=None):
    when = kickoff or (NOW - timedelta(days=1))
    return {"fixture": {"id": mid, "date": when.isoformat(), "timestamp": int(when.timestamp()),
                        "status": {"short": status}},
            "teams": {"home": {"id": 11, "name": "Home"}, "away": {"id": 22, "name": "Away"}},
            "goals": {"home": 99, "away": 99},
            "score": {"fulltime": {"home": 2, "away": 1}, "extratime": {"home": 3, "away": 1}}}


class DataTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.env = patch.dict(os.environ, {"FOOTBALL_API_KEY": SECRET})
        self.env.start()
        self.addCleanup(self.env.stop)

    def client(self, opener, **kwargs):
        return APIClient(self.temp.name, opener=opener, sleep=lambda _: None,
                         clock=lambda: NOW, min_interval=0, **kwargs)

    def test_missing_key_fails_without_network(self):
        with patch.dict(os.environ, {"FOOTBALL_API_KEY": ""}):
            with self.assertRaisesRegex(APIError, "FOOTBALL_API_KEY"):
                APIClient(self.temp.name)

    def test_http_200_provider_error_is_failure_and_not_cached(self):
        opener = Mock(return_value=Response(envelope([], {"plan": "Free plan does not support season"})))
        with self.assertRaisesRegex(APIError, "Free plan"):
            self.client(opener).fetch_fixtures(39, 2020)
        self.assertEqual(list(Path(self.temp.name).rglob("*.json")), [])
        self.assertEqual(opener.call_count, 1)

    def test_secret_never_in_exception_or_cache(self):
        opener = Mock(return_value=Response(envelope([], {"bad": SECRET})))
        with self.assertRaises(APIError) as caught:
            self.client(opener).fetch_fixtures(39, 2026)
        self.assertNotIn(SECRET, str(caught.exception))
        opener = Mock(return_value=Response(envelope([{"unexpected_echo": SECRET}])))
        self.client(opener).fetch_fixtures(39, 2025, current=False)
        files = list(Path(self.temp.name).rglob("*.json"))
        self.assertEqual(len(files), 1)
        self.assertNotIn(SECRET, files[0].read_text())

    def test_secret_only_in_header_and_expected_https_host(self):
        opener = Mock(return_value=Response(envelope([])))
        self.client(opener).fetch_fixtures(39, 2026)
        request = opener.call_args.args[0]
        self.assertEqual(request.full_url, "https://v3.football.api-sports.io/fixtures?league=39&season=2026")
        self.assertNotIn(SECRET, request.full_url)
        self.assertEqual(request.get_header("X-apisports-key"), SECRET)

    def test_global_budget_includes_status_and_retries(self):
        opener = Mock(side_effect=[Response(envelope({"subscription": {"active": True}})),
                                   URLError("network problem " + SECRET)])
        client = self.client(opener, max_requests=2)
        client.status()
        with self.assertRaisesRegex(APIError, "请求上限"):
            client.fetch_fixtures(39, 2026)
        self.assertEqual(client.requests_made, 2)
        self.assertEqual(opener.call_count, 2)

    def test_network_retries_are_bounded_and_redacted(self):
        opener = Mock(side_effect=URLError(SECRET))
        client = self.client(opener)
        with self.assertRaises(APIError) as caught:
            client.fetch_fixtures(39, 2026)
        self.assertEqual(client.requests_made, 3)
        self.assertNotIn(SECRET, str(caught.exception))

    def test_daily_quota_header_stops_next_call(self):
        opener = Mock(return_value=Response(envelope([]), {"x-ratelimit-requests-remaining": "0"}))
        client = self.client(opener)
        client.fetch_fixtures(39, 2025)
        with self.assertRaisesRegex(APIError, "剩余额度"):
            client.fetch_fixtures(39, 2026)
        self.assertEqual(opener.call_count, 1)

    def test_minute_limit_header_causes_wait_before_next_call(self):
        opener = Mock(side_effect=[Response(envelope([]), {"X-RateLimit-Remaining": "0"}),
                                   Response(envelope([]), {"X-RateLimit-Remaining": "9"})])
        client = self.client(opener, monotonic=lambda: 0)
        sleeps = []
        client._sleep = sleeps.append
        client.fetch_fixtures(39, 2025)
        client.fetch_fixtures(39, 2026)
        self.assertEqual(sleeps, [60])
        self.assertEqual(client.requests_made, 2)

    def test_status_account_information_not_cached(self):
        payload = envelope({"account": {"email": "example@example.invalid"},
                            "subscription": {"active": True},
                            "requests": {"current": 0, "limit_day": 100}})
        client = self.client(Mock(return_value=Response(payload)))
        status = client.status()
        self.assertTrue(status["subscription"]["active"])
        self.assertFalse(status["from_cache"])
        self.assertEqual(list(Path(self.temp.name).rglob("*.json")), [])

    def test_long_retry_after_is_not_ignored(self):
        headers = Message()
        headers["Retry-After"] = "120"
        opener = Mock(side_effect=HTTPError("https://v3.football.api-sports.io/fixtures", 429,
                                           "rate limited", headers, None))
        with self.assertRaisesRegex(APIError, "超过 60 秒"):
            self.client(opener).fetch_fixtures(39, 2026)
        self.assertEqual(opener.call_count, 1)

    def test_current_refresh_and_historical_original_fetch_time(self):
        opener = Mock(side_effect=lambda *a, **k: Response(envelope([fixture()])))
        client = self.client(opener)
        old = client.fetch_fixtures(39, 2025, current=False)
        later = NOW + timedelta(days=1)
        client._clock = lambda: later
        cached = client.fetch_fixtures(39, 2025, current=False)
        self.assertTrue(cached["from_cache"])
        self.assertEqual(cached["fetched_at"], old["fetched_at"])
        self.assertEqual(opener.call_count, 1)
        client.fetch_fixtures(39, 2026)
        client.fetch_fixtures(39, 2026)
        self.assertEqual(opener.call_count, 3)
        client._clock = lambda: NOW + timedelta(days=8)
        refreshed = client.fetch_fixtures(39, 2025, current=False)
        self.assertFalse(refreshed["from_cache"])
        self.assertEqual(opener.call_count, 4)

    def test_force_overrides_cache(self):
        opener = Mock(side_effect=lambda *a, **k: Response(envelope([])))
        client = self.client(opener)
        client.fetch_fixtures(39, 2025, current=False)
        client.fetch_fixtures(39, 2025, current=False, force=True)
        self.assertEqual(opener.call_count, 2)

    def test_partial_pagination_is_rejected(self):
        payload = envelope([])
        payload["paging"]["total"] = 2
        with self.assertRaisesRegex(APIError, "分页"):
            self.client(Mock(return_value=Response(payload))).fetch_fixtures(39, 2026)

    def test_league_resolution_requires_exact_name_country(self):
        league = {"league": {"id": 39, "name": "Premier League"}, "country": {"name": "England"},
                  "seasons": [{"year": 2026, "current": True, "coverage": {"fixtures": {"lineups": True}}}]}
        wrong = copy.deepcopy(league)
        wrong["country"]["name"] = "Another country"
        opener = Mock(return_value=Response(envelope([wrong, league])))
        result = self.client(opener).resolve_league({"name": "Premier League", "country": "England"})
        self.assertEqual(result["league"]["id"], 39)
        self.assertIn("fetched_at", result)
        self.assertFalse(result["from_cache"])

    def test_ambiguous_league_resolution_fails(self):
        item = {"league": {"id": 1, "name": "X"}, "country": {"name": "Y"}, "seasons": []}
        with self.assertRaisesRegex(APIError, "匹配 2 项"):
            self.client(Mock(return_value=Response(envelope([item, item])))).resolve_league({"name": "X", "country": "Y"})

    def test_only_ft_and_fulltime_goals_are_used(self):
        fixtures = [fixture(), fixture(2, "AET"), fixture(3, "PEN"), fixture(4, "NS"),
                    fixture(5, "FT", NOW + timedelta(days=1)), fixture(6, "FT", NOW - timedelta(hours=3))]
        rows = normalize_fixtures(fixtures, NOW)
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["home_goals"], rows[0]["away_goals"]), (2, 1))

    def test_null_negative_and_string_goals_are_rejected(self):
        for bad in (None, -1, "2", True, 1.5):
            with self.subTest(bad=bad):
                item = fixture()
                item["score"]["fulltime"]["home"] = bad
                with self.assertRaises(DataError):
                    normalize_fixtures([item], NOW)

    def test_duplicate_identical_deduplicated_but_conflict_fails(self):
        one, two = fixture(), fixture()
        self.assertEqual(len(normalize_fixtures([one, two], NOW)), 1)
        two["score"]["fulltime"]["home"] = 3
        with self.assertRaisesRegex(DataError, "冲突"):
            normalize_fixtures([one, two], NOW)

    def test_missing_or_wrong_id_type_and_timezone_rejected(self):
        for bad in (None, "1", True, -1):
            item = fixture()
            item["fixture"]["id"] = bad
            with self.assertRaises(DataError):
                normalize_fixtures([item], NOW)
        with self.assertRaises(DataError):
            normalize_fixtures([fixture()], datetime(2026, 9, 21))

    def test_timestamp_disagreement_rejected(self):
        item = fixture()
        item["fixture"]["timestamp"] += 3600
        with self.assertRaisesRegex(DataError, "不一致"):
            normalize_fixtures([item], NOW)

    def test_upcoming_requires_ns_future_and_horizon(self):
        data = [fixture(1, "NS", NOW + timedelta(days=1)), fixture(2, "NS", NOW - timedelta(days=1)),
                fixture(3, "PST", NOW + timedelta(days=1)), fixture(4, "NS", NOW + timedelta(days=8))]
        rows = normalize_upcoming(data, NOW)
        self.assertEqual([r["match_id"] for r in rows], [1])
        self.assertNotIn("home_goals", rows[0])


if __name__ == "__main__":
    unittest.main()
