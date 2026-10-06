import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from test_data import NOW, SECRET, Response, envelope
from football_model.data import APIClient, APIError, RequestBudgetError


class FundamentalsClientTests(unittest.TestCase):
    # Reuse the existing synthetic credential/clock fixture, never a live key.
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        env = patch.dict(os.environ, {'FOOTBALL_API_KEY': SECRET})
        env.start()
        self.addCleanup(env.stop)

    def client(self, opener, **kwargs):
        return APIClient(self.temp.name, opener=opener, clock=lambda: NOW,
                         min_interval=0, sleep=lambda _: None, **kwargs)

    def test_fixture_facts_use_supported_endpoint_and_preserve_cache_time(self):
        opener = Mock(return_value=Response(envelope([])))
        client = self.client(opener)
        first = client.fetch_fundamentals('/injuries', 123)
        second = client.fetch_fundamentals('/injuries', 123)
        self.assertEqual(opener.call_count, 1)
        self.assertIn('/injuries?fixture=123', opener.call_args.args[0].full_url)
        self.assertEqual(first['fetched_at'], second['fetched_at'])
        self.assertTrue(second['from_cache'])
        self.assertEqual(second['response'], [])

    def test_invalid_fact_query_never_requests(self):
        opener = Mock()
        client = self.client(opener)
        for endpoint, mid in [('https://invalid.example', 1), ('/fixtures/statistics', True),
                              ('/fixtures/lineups', 0)]:
            with self.assertRaises(ValueError):
                client.fetch_fundamentals(endpoint, mid)
        for ttl in (float('nan'), float('inf'), -1, True):
            with self.assertRaises(ValueError):
                client.fetch_fundamentals('/injuries', 1, max_age_seconds=ttl)
        opener.assert_not_called()

    def test_optional_facts_respect_core_request_reservation(self):
        opener = Mock(return_value=Response(envelope([])))
        client = self.client(opener, max_requests=1)
        with client.reserve_requests(1):
            with self.assertRaises(RequestBudgetError):
                client.fetch_fundamentals('/fixtures/lineups', 1)
        opener.assert_not_called()

    def test_paginated_facts_fail_without_caching_partial_response(self):
        payload = envelope([])
        payload['paging']['total'] = 2
        opener = Mock(return_value=Response(payload))
        with self.assertRaises(APIError):
            self.client(opener).fetch_fundamentals('/injuries', 1)
        self.assertEqual(list(Path(self.temp.name).rglob('*.json')), [])
