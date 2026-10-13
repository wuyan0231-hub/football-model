"""Exercise the domestic schedule batch against real cache and quota mechanics."""
import copy
from datetime import timedelta
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from football_model.data import APIClient, RequestBudgetError, _iso
from football_model.advanced_capacity import CapacityDeferred
from football_model.feature_schedule import collect_all_schedules, collect_schedule
from test_feature_schedule import CONFIG, NOW, FakeClient, fixture


class CachedClient(APIClient):
    """Use APIClient's cache/deadline/reservation without external requests."""
    def __init__(self, root, time, seconds, *, remaining=4000):
        with patch.dict(os.environ, {'FOOTBALL_API_KEY': 'schedule-test-placeholder'}):
            super().__init__(root, min_interval=0, max_run_seconds=600, clock=lambda: time[0],
                             monotonic=lambda: seconds[0], daily_limit=5000)
        self.time, self.seconds, self.remaining = time, seconds, remaining
        self.calls, self.status_calls = [], 0

    def status(self):
        self.status_calls += 1
        self.requests_made += 1
        self._observe_daily(5000, 5000 - self.remaining, self.remaining, 'test_status')
        return {'requests': {'current': 5000 - self.remaining, 'limit_day': 5000}}

    def _request(self, endpoint, params):
        self._before_request()
        self.calls.append((endpoint, copy.deepcopy(params)))
        if endpoint == '/fixtures':
            rows = [fixture(1 if params['season'] == 2026 else 2, params['season'])]
        else:
            years = [params['season']] if 'season' in params else [2026, 2025]
            rows = [{'league': {'id': 135, 'name': 'Serie A', 'type': 'League'},
                     'country': {'name': 'Italy'},
                     'seasons': [{'year': y, 'coverage': {}} for y in years]}]
        return {'response': rows, 'errors': [], 'paging': {'current': 1, 'total': 1},
                'fetched_at': _iso(self.time[0]), 'from_cache': False}


class ScheduleBatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'data').mkdir()
        self.time, self.seconds = [NOW], [0]
        self.config = copy.deepcopy(CONFIG)
        self.config['competitions'].extend([
            {**self.config['competitions'][0], 'key': 'epl'},
            {'key': 'unl', 'mode': 'nations_league_baseline'},
            {'key': 'ucl', 'mode': 'audit_only'}])
        rows = []
        for mid, year in ((1, 2026), (2, 2025)):
            raw = fixture(mid, year)
            rows.append({'match_id': mid, 'kickoff': raw['fixture']['date'], 'home_id': 11,
                'away_id': 22, 'home_name': 'Home', 'away_name': 'Away', 'home_goals': 2, 'away_goals': 1})
        for league in ('serie_a', 'epl'):
            (self.root / 'data' / (league + '.json')).write_text(json.dumps({
                'matches': rows, 'requested_seasons': [2026, 2025], 'observed_at': NOW.isoformat()}))

    def run_batch(self, client=None):
        return collect_all_schedules(self.config, self.root, source_commit='a' * 40,
            client=client or FakeClient(), clock=lambda: self.time[0])

    def test_one_account_observation_domestic_only_and_original_evidence(self):
        client = FakeClient()
        code, report = self.run_batch(client)
        self.assertEqual(code, 0)
        self.assertTrue(report['full_collection_completed'])
        self.assertEqual(report['configured_domestic_leagues'], ['serie_a', 'epl'])
        self.assertEqual(report['excluded_non_domestic_leagues'], ['unl', 'ucl'])
        self.assertEqual(sum(call[0] == '/status' for call in client.calls), 1)
        self.assertEqual(client.reserve, 1500)
        self.assertEqual(report['global_network_max_seconds'], 600)
        self.assertEqual(sum(r['api_requests'] for r in report['results']) + 1, report['api_requests'])
        for row in report['results']:
            path = self.root / row['report_ref']['path']
            detail = json.loads(path.read_text())
            self.assertEqual(detail['league'], row['league'])
            self.assertFalse(detail['coverage']['historical_availability_verified'])
            for request in detail['coverage']['team_season_requests']:
                evidence = request['evidence']
                raw = json.loads((self.root / evidence['path']).read_text())
                self.assertEqual(raw['fetched_at'], evidence['fetched_at'])

    def test_real_cache_reuse_preserves_observation_and_expiry(self):
        client = CachedClient(self.root / 'cache', self.time, self.seconds)
        code, first = self.run_batch(client)
        self.assertEqual(code, 0)
        before = len(client.calls)
        self.time[0] += timedelta(hours=5)
        second_client = CachedClient(self.root / 'cache', self.time, self.seconds)
        code, second = self.run_batch(second_client)
        self.assertEqual(code, 0)
        self.assertEqual(second_client.calls, [])
        self.assertTrue(all(r['cache_reused_responses'] == 6 for r in second['results']))
        detail = json.loads((self.root / second['results'][0]['report_ref']['path']).read_text())
        self.assertEqual(detail['coverage']['team_season_requests'][0]['evidence']['fetched_at'], _iso(NOW))
        self.time[0] += timedelta(hours=2)
        third_client = CachedClient(self.root / 'cache', self.time, self.seconds)
        code, third = self.run_batch(third_client)
        self.assertEqual(code, 0)
        self.assertGreater(before, 0)
        refreshed = [params for endpoint, params in third_client.calls if endpoint == '/fixtures']
        self.assertEqual(len(refreshed), 3)  # one league audit, two club rows
        self.assertTrue(all(params['season'] == 2026 for params in refreshed))

    def test_daily_reservation_blocks_network_without_resetting_budget(self):
        client = CachedClient(self.root / 'cache', self.time, self.seconds, remaining=1500)
        code, report = self.run_batch(client)
        self.assertEqual(code, 0)
        self.assertFalse(report['full_collection_completed'])
        self.assertEqual(report['deferred_leagues'], 2)
        self.assertEqual(client.calls, [])
        self.assertEqual(client.daily_remaining, 1500)
        self.assertEqual(client.status_calls, 1)
        self.assertIsNone(report['last_attempted_league'])

    def test_global_deadline_remains_shared_across_leagues(self):
        client = CachedClient(self.root / 'cache', self.time, self.seconds)
        original = client._request
        def consume_deadline(endpoint, params):
            response = original(endpoint, params)
            self.seconds[0] = 600
            return response
        client._request = consume_deadline
        code, report = self.run_batch(client)
        self.assertEqual(code, 0)
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(report['deferred_leagues'], 2)
        self.assertEqual(report['last_attempted_league'], 'serie_a')
        self.assertFalse(report['full_collection_completed'])

    def test_rotation_resumes_after_last_network_league(self):
        client = CachedClient(self.root / 'cache', self.time, self.seconds)
        original = client._request
        def stop(endpoint, params):
            response = original(endpoint, params)
            self.seconds[0] = 600
            return response
        client._request = stop
        self.run_batch(client)
        self.time[0] += timedelta(minutes=1)
        _, report = self.run_batch(FakeClient())
        self.assertEqual(report['processing_order'], ['epl', 'serie_a'])

    def test_incomplete_league_has_priority_without_dropping_others(self):
        _, report = self.run_batch()
        pointer = self.root / 'feature_models/schedules/collection/latest.json'
        report['last_attempted_league'] = 'epl'
        report['results'][1]['status'] = 'deferred'
        pointer.write_text(json.dumps(report))
        self.time[0] += timedelta(minutes=1)
        _, resumed = self.run_batch()
        self.assertEqual(resumed['processing_order'], ['epl', 'serie_a'])
        self.assertEqual(len(resumed['results']), 2)

    def test_unfinished_club_season_is_attempted_first(self):
        client = FakeClient()
        collect_schedule(CONFIG, self.root, client=client, clock=lambda: self.time[0])
        pointer = self.root / 'feature_models/schedules/serie_a/latest.json'
        report = json.loads(pointer.read_text())
        report['coverage']['team_season_requests'][-1]['status'] = 'deferred'
        pointer.write_text(json.dumps(report))
        self.time[0] += timedelta(seconds=1)
        new_client = FakeClient()
        collect_schedule(CONFIG, self.root, client=new_client, clock=lambda: self.time[0],
                         pending_first=True, current_max_age_seconds=21600)
        calls = [c for c in new_client.calls if c[0] == '/fixtures']
        self.assertEqual(calls[0][1], {'team': 22, 'season': 2025})

    def test_missing_league_input_is_visible_and_other_league_continues(self):
        (self.root / 'data/serie_a.json').unlink()
        code, report = self.run_batch()
        self.assertEqual(code, 1)
        self.assertEqual(report['failed_leagues'], 1)
        self.assertEqual(report['completed_leagues'], 1)
        self.assertFalse(report['full_collection_completed'])
        self.assertEqual(report['results'][0]['status'], 'failed')

    def test_identity_conflicts_are_not_converted_to_budget_deferral(self):
        client = FakeClient()
        client.team_rows[2026][0]['teams']['home']['id'] = 99
        code, report = self.run_batch(client)
        self.assertEqual(code, 1)
        self.assertEqual(report['failed_leagues'], 2)
        self.assertEqual(report['deferred_leagues'], 0)

    def test_future_dated_provider_evidence_stays_rejected(self):
        client = FakeClient()
        original = client.fetch_fixtures
        def future(*args, **kwargs):
            envelope = original(*args, **kwargs)
            envelope['fetched_at'] = _iso(NOW + timedelta(hours=1))
            return envelope
        client.fetch_fixtures = future
        code, report = self.run_batch(client)
        self.assertEqual(code, 1)
        self.assertEqual(report['failed_leagues'], 2)
        self.assertFalse(report['full_collection_completed'])

    def test_capacity_deferral_keeps_its_distinct_diagnostic(self):
        client = FakeClient()
        for year in (2026, 2025):
            client.failures[('/leagues', (('id', 135), ('season', year)))] = CapacityDeferred({'test': True})
        code, report = self.run_batch(client)
        self.assertEqual(code, 0)
        self.assertEqual(report['deferred_leagues'], 2)
        for league in report['results']:
            detail = json.loads((self.root / league['report_ref']['path']).read_text())
            diagnostics = detail['coverage']['competition_metadata_failures']
            self.assertTrue(diagnostics)
            self.assertTrue(all(x['diagnostic_code'] == 'storage_capacity_deferred' for x in diagnostics))


if __name__ == '__main__':
    unittest.main()
