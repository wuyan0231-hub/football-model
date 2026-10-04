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

from football_model.data import APIClient, APIError, DataError, RequestBudgetError, RequestDeadlineError, normalize_fixtures, normalize_upcoming


NOW = datetime(2026, 9, 21, 12, tzinfo=timezone.utc)
SECRET = "unit-test-secret-not-a-real-api-key"


def envelope(response, errors=None):
    return {"get": "fixtures", "parameters": {}, "errors": [] if errors is None else errors,
            "paging": {"current": 1, "total": 1}, "response": response}


class Response(io.BytesIO):
    def __init__(self, payload, headers=None):
        super().__init__(json.dumps(payload).encode())
        self.headers = headers or {}


class FakeTimer:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


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
        kwargs.setdefault("min_interval", 0)
        kwargs.setdefault("sleep", lambda _: None)
        return APIClient(self.temp.name, opener=opener, clock=lambda: NOW, **kwargs)

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

    def test_default_has_no_80_attempt_cap_and_unknown_daily_is_not_reset(self):
        opener = Mock(side_effect=lambda *a, **k: Response(envelope([])))
        client = self.client(opener)
        for _ in range(85):
            client.fetch_fixtures(39, 2026)
        self.assertEqual(client.requests_made, 85)
        self.assertIsNone(client.max_requests)
        self.assertIsNone(client.daily_remaining)
        self.assertEqual(client.budget_snapshot()["quota_source"], "unknown")

    def test_configured_5000_uses_status_and_allows_more_than_80_actual_requests(self):
        opener = Mock(side_effect=lambda request, **k: Response(envelope(
            {"requests": {"current": 4000, "limit_day": 5000}}
            if request.full_url.endswith("/status") else [])))
        client = self.client(opener, daily_limit=5000)
        client.status()
        for _ in range(85):
            client.fetch_fixtures(39, 2026)
        self.assertEqual(client.requests_made, 86)
        self.assertEqual(client.daily_remaining, 915)

    def test_configured_daily_limit_blocks_unknown_and_partial_evidence_until_status(self):
        opener = Mock(side_effect=[Response(envelope({}), {"X-RateLimit-Requests-Remaining": "6000"}),
                                   Response(envelope({"requests": {"current": 4999, "limit_day": 7500}})),
                                   Response(envelope([]))])
        client = self.client(opener, daily_limit=5000)
        with self.assertRaises(RequestBudgetError):
            client.fetch_fixtures(39, 2026)
        opener.assert_not_called()
        client.status()
        self.assertIsNone(client.daily_remaining)
        self.assertEqual(client.daily_remaining_reported, 6000)
        self.assertEqual(client.budget_snapshot()["daily_quota_status"], "incomplete")
        with self.assertRaises(RequestBudgetError):
            client.fetch_fixtures(39, 2026)
        self.assertEqual(opener.call_count, 1)
        client.status()
        self.assertEqual(client.daily_remaining, 1)
        client.fetch_fixtures(39, 2026)
        self.assertEqual(client.daily_remaining, 0)

    def test_configured_daily_limit_cannot_be_bypassed_by_fully_missing_status_quota(self):
        opener = Mock(return_value=Response(envelope({"subscription": {"active": True}})))
        client = self.client(opener, daily_limit=5000)
        client.status()
        self.assertIsNone(client.daily_remaining)
        with self.assertRaises(RequestBudgetError):
            client.fetch_fixtures(39, 2026)
        self.assertEqual(opener.call_count, 1)

    def test_conflicting_status_body_cannot_relax_same_response_zero_balance_header(self):
        opener = Mock(return_value=Response(envelope({"requests": {"current": 4900, "limit_day": 5000}}),
                                             {"X-RateLimit-Requests-Remaining": "0"}))
        client = self.client(opener, daily_limit=5000)
        client.status()
        self.assertEqual(client.daily_remaining, 0)
        self.assertEqual(client.daily_remaining_reported, 0)
        self.assertEqual(client.budget_snapshot()["quota_source"], "status_and_response_headers")
        with self.assertRaises(RequestBudgetError):
            client.fetch_fixtures(39, 2026)
        self.assertEqual(opener.call_count, 1)

    def test_conflicting_large_plan_status_keeps_stricter_configured_allowance_from_headers(self):
        opener = Mock(return_value=Response(envelope({"requests": {"current": 4900, "limit_day": 10000}}),
            {"X-RateLimit-Requests-Limit": "10000", "X-RateLimit-Requests-Remaining": "5000"}))
        client = self.client(opener, daily_limit=5000)
        client.status()
        self.assertEqual(client.daily_remaining, 0)
        self.assertEqual(client.provider_daily_used, 5000)
        with self.assertRaises(RequestBudgetError):
            client.fetch_fixtures(39, 2026)
        self.assertEqual(opener.call_count, 1)

    def test_limit_only_header_cannot_relax_conservative_status_balance(self):
        opener = Mock(side_effect=[Response(envelope({"requests": {"current": 4900, "limit_day": 5000}}),
            {"X-RateLimit-Requests-Limit": "5000", "X-RateLimit-Requests-Remaining": "5"}),
            Response(envelope([]), {"X-RateLimit-Requests-Limit": "5000"})])
        client = self.client(opener, daily_limit=5000)
        client.status()
        self.assertEqual(client.daily_remaining, 5)
        client.fetch_fixtures(39, 2026)
        self.assertEqual(client.daily_remaining, 4)
        self.assertEqual(client.provider_daily_used, 4995)

    def test_status_uses_already_spent_daily_allowance_and_protects_reserve(self):
        opener = Mock(side_effect=[Response(envelope({"requests": {"current": 4998, "limit_day": 5000}})),
                                   Response(envelope([])), Response(envelope([]))])
        client = self.client(opener, daily_limit=5000)
        client.status()
        self.assertEqual(client.daily_remaining, 2)
        self.assertEqual(client.requests_made, 1)
        with client.reserve_requests(1):
            client.fetch_fixtures(39, 2026)
            with self.assertRaises(RequestBudgetError):
                client.fetch_fixtures(39, 2026)
        client.fetch_fixtures(39, 2026)
        with self.assertRaises(RequestBudgetError):
            client.fetch_fixtures(39, 2026)
        self.assertEqual(client.requests_made, 3)
        self.assertEqual(client.daily_remaining, 0)

    def test_status_respects_smaller_provider_plan_and_larger_plan_configuration(self):
        for provider_limit, current, expected in ((100, 97, 3), (7500, 4998, 2), (7500, 6000, 0)):
            with self.subTest(provider_limit=provider_limit, current=current):
                client = self.client(Mock(return_value=Response(envelope(
                    {"requests": {"current": current, "limit_day": provider_limit}}))), daily_limit=5000)
                client.status()
                self.assertEqual(client.daily_remaining, expected)
                snapshot = client.budget_snapshot()
                self.assertEqual(snapshot["configured_daily_limit"], 5000)
                self.assertEqual(snapshot["provider_daily_limit"], provider_limit)
                self.assertEqual(snapshot["provider_daily_used"], current)
                self.assertEqual(snapshot["quota_source"], "status")
                self.assertEqual(snapshot["quota_observed_at"], "2026-09-21T12:00:00Z")

    def test_status_can_refresh_exhausted_balance_without_charging_daily_allowance(self):
        opener = Mock(side_effect=[URLError("status timeout"), Response(envelope(
            {"requests": {"current": 4999, "limit_day": 5000}}))])
        client = self.client(opener, daily_limit=5000)
        client.daily_remaining = 0
        with client.reserve_requests(1):
            client.status()
        self.assertEqual(client.requests_made, 2)
        self.assertEqual(client.daily_remaining, 1)

    def test_malformed_status_does_not_invent_balance_or_return_account(self):
        for requests in ({}, {"current": True, "limit_day": 5000},
                         {"current": "4999", "limit_day": 5000}, {"current": -1, "limit_day": 5000}):
            with self.subTest(requests=requests):
                client = self.client(Mock(return_value=Response(envelope(
                    {"account": {"email": "private@example.invalid"}, "requests": requests}))),
                    daily_limit=5000)
                status = client.status()
                self.assertNotIn("account", status)
                self.assertEqual(status["requests"], {})
                self.assertIsNone(client.daily_remaining)
                self.assertNotIn("private@example.invalid", json.dumps(client.budget_snapshot()))

    def test_daily_limit_and_remaining_headers_correct_usage_under_configured_limit(self):
        opener = Mock(side_effect=[Response(envelope({"requests": {"current": 4996, "limit_day": 7500}})),
                                   Response(envelope([]), {"X-RateLimit-Requests-Limit": "7500",
                                                           "X-RateLimit-Requests-Remaining": "2502"}),
                                   Response(envelope([]))])
        client = self.client(opener, daily_limit=5000)
        client.status()
        self.assertEqual(client.daily_remaining, 4)
        client.fetch_fixtures(39, 2026)
        self.assertEqual(client.daily_remaining, 2)
        self.assertEqual(client.daily_remaining_reported, 2502)
        self.assertEqual(client.provider_daily_used, 4998)
        client.fetch_fixtures(39, 2026)
        self.assertEqual(client.daily_remaining, 1)
        self.assertEqual(client.budget_snapshot()["quota_source"], "response_headers")

    def test_invalid_budget_timing_inputs_fail_before_network(self):
        cases = [(name, bad) for name in ("max_requests", "daily_limit")
                 for bad in (0, -1, True, 1.5, "5000")]
        cases += [(name, bad) for name in ("timeout", "max_run_seconds")
                  for bad in (0, -1, True, float("inf"), float("nan"), "25")]
        cases += [("min_interval", bad) for bad in (-1, 61, True, float("inf"), float("nan"), "0")]
        opener = Mock()
        for name, bad in cases:
            with self.subTest(name=name, value=bad), self.assertRaises(ValueError):
                self.client(opener, **{name: bad})
        opener.assert_not_called()

    def test_adaptive_interval_uses_provider_limit_and_explicit_zero_cannot_bypass_it(self):
        for configured in (None, 0):
            with self.subTest(min_interval=configured):
                timer = FakeTimer()
                opener = Mock(side_effect=lambda *a, **k: Response(envelope([]),
                    {"X-RateLimit-Limit": "300", "X-RateLimit-Remaining": "299"}))
                client = self.client(opener, min_interval=configured, monotonic=timer, sleep=timer.sleep)
                self.assertEqual(client.budget_snapshot()["min_interval_seconds"], 6.5 if configured is None else 0)
                client.fetch_fixtures(39, 2026)
                client.fetch_fixtures(39, 2026)
                self.assertAlmostEqual(timer.sleeps[0], 0.21)
                self.assertAlmostEqual(client.budget_snapshot()["min_interval_seconds"], 0.21)

    def test_unknown_minute_limit_uses_adaptive_fallback(self):
        timer = FakeTimer()
        client = self.client(Mock(side_effect=lambda *a, **k: Response(envelope([]))),
                             min_interval=None, monotonic=timer, sleep=timer.sleep)
        client.fetch_fixtures(39, 2026)
        client.fetch_fixtures(39, 2026)
        self.assertEqual(timer.sleeps, [6.5])

    def test_one_request_per_minute_is_supported_without_wait_exceeding_60(self):
        timer = FakeTimer()
        client = self.client(Mock(side_effect=lambda *a, **k: Response(envelope([]),
            {"X-RateLimit-Limit": "1", "X-RateLimit-Remaining": "0"})),
            min_interval=None, monotonic=timer, sleep=timer.sleep)
        client.fetch_fixtures(39, 2026)
        client.fetch_fixtures(39, 2026)
        self.assertEqual(timer.sleeps, [60])
        self.assertEqual(client.budget_snapshot()["min_interval_seconds"], 60)

    def test_headerless_failure_conservatively_consumes_minute_balance(self):
        timer = FakeTimer()
        call_times = []
        responses = [Response(envelope([]), {"X-RateLimit-Limit": "120", "X-RateLimit-Remaining": "1"}),
                     URLError("response lost"), Response(envelope([]))]
        def open_response(*args, **kwargs):
            call_times.append(timer.now)
            result = responses.pop(0)
            if isinstance(result, Exception):
                raise result
            return result
        client = self.client(open_response, monotonic=timer, sleep=timer.sleep)
        client.fetch_fixtures(39, 2026)
        client.fetch_fixtures(39, 2026)
        self.assertEqual(len(call_times), 3)
        self.assertGreaterEqual(call_times[2], 60)
        self.assertIsNone(client.minute_remaining)
        self.assertTrue(all(wait <= 60 for wait in timer.sleeps))

    def test_expired_minute_window_is_unknown_and_429_waits_before_retry(self):
        timer = FakeTimer()
        headers = Message()
        headers["X-RateLimit-Remaining"] = "0"
        headers["X-RateLimit-Limit"] = "120"
        opener = Mock(side_effect=[HTTPError("https://v3.football.api-sports.io/fixtures",
                                           429, "too many requests", headers, None), Response(envelope([]))])
        client = self.client(opener, monotonic=timer, sleep=timer.sleep)
        client.fetch_fixtures(39, 2026)
        self.assertEqual(timer.sleeps, [60])
        self.assertIsNone(client.budget_snapshot()["minute_remaining"])
        self.assertEqual(client.requests_made, 2)

    def test_soft_deadline_checks_wait_and_timeout_before_network(self):
        for elapsed, timeout, remaining_header in ((8, 5, None), (1, 1, "0")):
            with self.subTest(elapsed=elapsed):
                timer = FakeTimer()
                opener = Mock()
                client = self.client(opener, max_run_seconds=10, timeout=timeout,
                                     monotonic=timer, sleep=timer.sleep)
                timer.now = elapsed
                if remaining_header is not None:
                    client._rate_headers({"X-RateLimit-Remaining": remaining_header})
                with self.assertRaises(RequestDeadlineError):
                    client.fetch_fixtures(39, 2026)
                opener.assert_not_called()
                self.assertEqual(timer.sleeps, [])
                self.assertEqual(client.requests_made, 0)

    def test_soft_deadline_stops_retry_and_keeps_cached_evidence_usable(self):
        timer = FakeTimer()
        headers = Message()
        opener = Mock(side_effect=[Response(envelope([])),
            HTTPError("https://v3.football.api-sports.io/fixtures", 429, "too many", headers, None)])
        client = self.client(opener, max_run_seconds=30, timeout=5, monotonic=timer, sleep=timer.sleep)
        original = client.fetch_venues(venue_id=780)
        with self.assertRaises(RequestDeadlineError):
            client.fetch_fixtures(39, 2026)
        timer.now = 31
        cached = client.fetch_venues(venue_id=780)
        self.assertTrue(cached["from_cache"])
        self.assertEqual(cached["fetched_at"], original["fetched_at"])
        self.assertEqual(opener.call_count, 2)
        self.assertEqual(timer.sleeps, [])

    def test_daily_quota_header_stops_next_call(self):
        opener = Mock(return_value=Response(envelope([]), {"x-ratelimit-requests-remaining": "0"}))
        client = self.client(opener)
        client.fetch_fixtures(39, 2025)
        with self.assertRaisesRegex(APIError, "剩余额度"):
            client.fetch_fixtures(39, 2026)
        self.assertEqual(opener.call_count, 1)

    def test_optional_scope_preserves_run_and_known_daily_allowance(self):
        for budget in ("run", "daily"):
            with self.subTest(budget=budget):
                opener = Mock(side_effect=lambda *a, **k: Response(envelope([])))
                client = self.client(opener, max_requests=80)
                client.requests_made = 79 if budget == "run" else 3
                client.daily_remaining = None if budget == "run" else 1
                original_used = client.requests_made
                with client.reserve_requests(1):
                    with self.assertRaisesRegex(RequestBudgetError, "预留"):
                        client.fetch_venues(venue_id=780)
                opener.assert_not_called()
                self.assertEqual(client.requests_made, original_used)
                # The scope must restore the original limits even on failure.
                client.fetch_fixtures(39, 2026)
                self.assertEqual(opener.call_count, 1)
                self.assertEqual(client.requests_made, original_used + 1)
                self.assertEqual(client.max_requests, 80)

    def test_optional_scope_cache_hit_works_at_reserved_limits(self):
        record = {"id": 780, "name": "Toumba Stadium", "city": "Thessaloniki", "country": "Greece"}
        opener = Mock(side_effect=lambda *a, **k: Response(envelope([record])))
        client = self.client(opener, max_requests=2)
        original = client.fetch_venues(venue_id=780)
        client.daily_remaining = 1
        with client.reserve_requests(1):
            cached = client.fetch_venues(venue_id=780)
            self.assertTrue(cached["from_cache"])
            self.assertEqual(cached["fetched_at"], original["fetched_at"])
            with self.assertRaises(RequestBudgetError):
                client.fetch_venues(venue_id=781)
        self.assertEqual(client.requests_made, 1)
        self.assertEqual(client.daily_remaining, 1)
        self.assertEqual(opener.call_count, 1)
        client.fetch_fixtures(39, 2026)
        self.assertEqual(client.requests_made, 2)
        self.assertEqual(client.daily_remaining, 0)
        # Even an exhausted network budget must not hide valid cached evidence.
        with client.reserve_requests(1):
            self.assertTrue(client.fetch_venues(venue_id=780)["from_cache"])
        self.assertEqual(opener.call_count, 2)

    def test_known_daily_allowance_counts_headerless_failed_attempts(self):
        opener = Mock(side_effect=[URLError("synthetic timeout"), Response(envelope([]))])
        client = self.client(opener)
        client.daily_remaining = 2
        with client.reserve_requests(1):
            with self.assertRaisesRegex(RequestBudgetError, "今日已知.*预留"):
                client.fetch_venues(venue_id=780)
        self.assertEqual(opener.call_count, 1)
        self.assertEqual(client.requests_made, 1)
        self.assertEqual(client.daily_remaining, 1)
        self.assertIsNone(client.daily_remaining_reported)
        client.fetch_fixtures(39, 2026)
        self.assertEqual(opener.call_count, 2)
        self.assertEqual(client.daily_remaining, 0)

    def test_unknown_daily_allowance_is_protected_after_retry_header(self):
        headers = Message()
        headers["x-ratelimit-requests-remaining"] = "1"
        opener = Mock(side_effect=[HTTPError("https://v3.football.api-sports.io/venues", 500,
                                           "synthetic failure", headers, None),
                                   Response(envelope([]), {"x-ratelimit-requests-remaining": "0"})])
        client = self.client(opener)
        self.assertIsNone(client.daily_remaining)
        with client.reserve_requests(1):
            with self.assertRaisesRegex(RequestBudgetError, "今日已知.*预留"):
                client.fetch_venues(venue_id=780)
        self.assertEqual(opener.call_count, 1)
        self.assertEqual(client.requests_made, 1)
        self.assertEqual(client.daily_remaining, 1)
        self.assertEqual(client.daily_remaining_reported, 1)
        client.fetch_fixtures(39, 2026)
        self.assertEqual(opener.call_count, 2)
        self.assertEqual(client.daily_remaining, 0)
        self.assertEqual(client.daily_remaining_reported, 0)

    def test_unknown_daily_allowance_stays_unknown_without_headers(self):
        opener = Mock(side_effect=URLError("synthetic timeout"))
        client = self.client(opener, max_requests=2)
        with client.reserve_requests(1):
            with self.assertRaisesRegex(RequestBudgetError, "本次运行.*预留"):
                client.fetch_venues(venue_id=780)
        self.assertEqual(opener.call_count, 1)
        self.assertIsNone(client.daily_remaining)
        self.assertIsNone(client.daily_remaining_reported)

    def test_daily_header_updates_conservative_attempt_estimate(self):
        opener = Mock(side_effect=[Response(envelope([]), {"x-ratelimit-requests-remaining": "4"}),
                                   Response(envelope([]))])
        client = self.client(opener)
        client.daily_remaining = 2
        with client.reserve_requests(1):
            client.fetch_venues(venue_id=780)
            # A genuine provider header is authoritative, not minus one twice.
            self.assertEqual(client.daily_remaining, 4)
            self.assertEqual(client.daily_remaining_reported, 4)
            client.fetch_venues(venue_id=781)
            self.assertEqual(client.daily_remaining, 3)
            self.assertEqual(client.daily_remaining_reported, 4)
        self.assertEqual(opener.call_count, 2)

    def test_nested_reservation_cannot_release_outer_allowance(self):
        opener = Mock(side_effect=lambda *a, **k: Response(envelope([])))
        client = self.client(opener, max_requests=4)
        client.requests_made = 2
        with client.reserve_requests(2):
            with client.reserve_requests(0):
                with self.assertRaises(RequestBudgetError):
                    client.fetch_venues(venue_id=780)
            with client.reserve_requests(3):
                with self.assertRaises(RequestBudgetError):
                    client.fetch_venues(venue_id=780)
            with self.assertRaises(RequestBudgetError):
                client.fetch_venues(venue_id=780)
        opener.assert_not_called()
        client.fetch_fixtures(39, 2026)
        self.assertEqual(opener.call_count, 1)

    def test_invalid_reservation_never_changes_request_budget(self):
        opener = Mock()
        client = self.client(opener)
        for reserve in (-1, True, 1.5, None):
            with self.subTest(reserve=reserve), self.assertRaises(ValueError):
                with client.reserve_requests(reserve):
                    client.fetch_venues(venue_id=780)
        opener.assert_not_called()
        self.assertEqual(client.requests_made, 0)

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

    def test_venue_queries_use_shared_budget_cache_and_exact_parameters(self):
        record = {"id": 780, "name": "Toumba Stadium", "city": "Thessaloniki", "country": "Greece"}
        opener = Mock(side_effect=lambda *a, **k: Response(envelope([record])))
        client = self.client(opener, max_requests=2)
        first = client.fetch_venues(venue_id=780)
        self.assertEqual(opener.call_args.args[0].full_url,
                         "https://v3.football.api-sports.io/venues?id=780")
        self.assertFalse(first["from_cache"])
        cached = client.fetch_venues(venue_id=780)
        self.assertTrue(cached["from_cache"])
        self.assertEqual(cached["fetched_at"], first["fetched_at"])
        client.fetch_venues(name="Toumba Stadium")
        self.assertEqual(opener.call_args.args[0].full_url,
                         "https://v3.football.api-sports.io/venues?name=Toumba+Stadium")
        with self.assertRaisesRegex(APIError, "请求上限"):
            client.fetch_venues(venue_id=781)
        self.assertEqual(opener.call_count, 2)

    def test_venue_invalid_queries_do_not_send_requests(self):
        opener = Mock()
        client = self.client(opener)
        for kwargs in ({}, {"venue_id": 0}, {"venue_id": True}, {"name": " "},
                       {"venue_id": 780, "name": "Toumba Stadium"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                client.fetch_venues(**kwargs)
        opener.assert_not_called()

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

    def test_finished_statuses_use_only_fulltime_and_keep_completion_buffer(self):
        fixtures = [fixture(), fixture(2, "AET"), fixture(3, "PEN"), fixture(4, "NS"),
                    fixture(5, "FT", NOW + timedelta(days=1)), fixture(6, "FT", NOW - timedelta(hours=3))]
        rows = normalize_fixtures(fixtures, NOW)
        self.assertEqual([row["match_id"] for row in rows], [1, 2, 3])
        self.assertTrue(all((row["home_goals"], row["away_goals"]) == (2, 1) for row in rows))

    def test_direct_shootout_elapsed_120_keeps_regulation_draw(self):
        item = fixture(8, "PEN")
        item["fixture"]["status"]["elapsed"] = 120
        item["score"]["fulltime"] = {"home": 1, "away": 1}
        item["score"]["penalty"] = {"home": 1, "away": 4}
        item["teams"]["away"]["winner"] = True
        row = normalize_fixtures([item], NOW)[0]
        self.assertEqual((row["home_goals"], row["away_goals"]), (1, 1))

    def test_extra_time_goals_never_enter_regulation_score(self):
        item = fixture(9, "AET")
        item["score"]["fulltime"] = {"home": 0, "away": 0}
        item["score"]["extratime"] = {"home": 2, "away": 1}
        item["goals"] = {"home": 2, "away": 1}
        row = normalize_fixtures([item], NOW)[0]
        self.assertEqual((row["home_goals"], row["away_goals"]), (0, 0))

    def test_two_leg_deciders_need_not_be_drawn_in_regulation(self):
        for status in ("AET", "PEN"):
            with self.subTest(status=status):
                item = fixture(10, status)
                item["fixture"]["status"]["elapsed"] = None
                row = normalize_fixtures([item], NOW)[0]
                self.assertEqual((row["home_goals"], row["away_goals"]), (2, 1))

    def test_missing_regulation_score_never_falls_back(self):
        for status in ("FT", "AET", "PEN"):
            for bad_score in ({}, {"fulltime": None}, {"fulltime": []}):
                with self.subTest(status=status, bad_score=bad_score):
                    item = fixture(12, status)
                    item["score"] = {**bad_score, "extratime": {"home": 2, "away": 1},
                                     "penalty": {"home": 4, "away": 3}}
                    with self.assertRaisesRegex(DataError, f"match_id=12 status={status}.*fulltime"):
                        normalize_fixtures([item], NOW)

    def test_incomplete_statuses_are_excluded_even_with_scores(self):
        statuses = ("NS", "1H", "HT", "2H", "ET", "P", "BT", "LIVE", "SUSP", "INT",
                    "PST", "CANC", "ABD", "AWD", "WO")
        items = [fixture(i + 1, status) for i, status in enumerate(statuses)]
        self.assertEqual(normalize_fixtures(items, NOW), [])

    def test_all_finished_statuses_observe_four_hour_cutoff(self):
        for status in ("FT", "AET", "PEN"):
            with self.subTest(status=status):
                recent = fixture(14, status, NOW - timedelta(hours=4) + timedelta(seconds=1))
                boundary = fixture(15, status, NOW - timedelta(hours=4))
                self.assertEqual(normalize_fixtures([recent], NOW), [])
                self.assertEqual([r["match_id"] for r in normalize_fixtures([boundary], NOW)], [15])

    def test_halftime_cannot_exceed_regulation_score(self):
        for status in ("FT", "AET", "PEN"):
            with self.subTest(status=status):
                item = fixture(16, status)
                item["score"]["halftime"] = {"home": 3, "away": 0}
                with self.assertRaisesRegex(DataError, f"match_id=16 status={status}.*halftime.home"):
                    normalize_fixtures([item], NOW)

    def test_missing_halftime_does_not_block_explicit_regulation_score(self):
        item = fixture(17, "PEN")
        item["score"]["halftime"] = {"home": None, "away": None}
        self.assertEqual(len(normalize_fixtures([item], NOW)), 1)

    def test_null_negative_and_string_goals_are_rejected(self):
        for status in ("FT", "AET", "PEN"):
            for side in ("home", "away"):
                for bad in (None, -1, "2", True, 1.5):
                    with self.subTest(status=status, side=side, bad=bad):
                        item = fixture(18, status)
                        item["score"]["fulltime"][side] = bad
                        with self.assertRaisesRegex(DataError, f"match_id=18 status={status}.*fulltime.{side}"):
                            normalize_fixtures([item], NOW)

    def test_duplicate_identical_deduplicated_but_conflict_fails(self):
        for status in ("FT", "AET", "PEN"):
            with self.subTest(status=status):
                one, two = fixture(19, status), fixture(19, status)
                self.assertEqual(len(normalize_fixtures([one, two], NOW)), 1)
                two["score"]["fulltime"]["home"] = 3
                with self.assertRaisesRegex(DataError, "match_id=19.*冲突"):
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
