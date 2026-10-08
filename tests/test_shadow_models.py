"""Engineering tests use synthetic matches, never evidence of prediction gains."""
import copy
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from football_model.shadow_models import capture, score, main
from football_model.prospective import _append, _digest, _objects
from tests.test_model import synthetic_matches


NOW = datetime(2022, 5, 5, tzinfo=timezone.utc)
def iso(t):
    return t.isoformat().replace('+00:00', 'Z')


class ShadowModelTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.runtime = Path(self.tmp.name) / 'runtime'
        self.root = self.runtime / 'shadow_models'
        self.clock = lambda: NOW
        self.config = {'half_life_days': 180, 'ridge': 3.0, 'min_train': 180,
            'forecast_days': 7, 'max_model_age_days': 14,
            'competitions': [{'key': 'epl', 'mode': 'domestic_baseline'}]}
        self.observed = iso(NOW - timedelta(hours=1))
        self.rows = [{'match_id': 1001, 'home_id': 1, 'away_id': 2, 'home_name': 'Synthetic 1',
            'away_name': 'Synthetic 2', 'kickoff': iso(NOW + timedelta(days=1)),
            'competition': 'epl', 'status': 'exploratory_baseline', 'run_started_at': self.observed,
            'data_observed_at': self.observed, 'forecast_generated_at': self.observed}]
        self.data = {'matches': synthetic_matches(), 'time_semantics': 'observed_wall_clock_v1',
                     'observed_at': self.observed, 'missing_seasons': []}
        self.report = {'run_id': 'r1', 'mode': 'train', 'time_semantics': 'observed_wall_clock_v1',
            'run_started_at': self.observed, 'generated_at': self.observed,
            'competitions': [{'key': 'epl', 'status': '探索性基线', 'data_observed_at': self.observed,
                              'provider_league_id': 39, 'missing_seasons': [],
                              'seasons': [{'fetched_at': self.observed, 'received_at': self.observed}]}]}
        self.index = {'status': 'ready', 'competitions': [{'key': 'epl', 'status': 'available',
            'latest_successful_forecast': {'run_id': 'r1', 'forecast_count': 1, 'unsupported_forecasts': 0}}]}
        self.inputs()

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, name, value):
        p = self.runtime / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(value))

    def inputs(self):
        self.write('integration/index.json', self.index)
        self.write('data/epl.json', self.data)
        self.write('predictions/r1/epl.json', self.rows)
        self.write('reports/history/r1.json', self.report)

    def run_capture(self, **kwargs):
        return capture(self.runtime, self.config, source_commit='a' * 40, clock=self.clock, **kwargs)

    def test_real_three_fits_share_cutoff_and_data_without_production_writes(self):
        before = {p.relative_to(self.runtime): p.read_bytes() for p in self.runtime.rglob('*.json')}
        code, report = self.run_capture()
        self.assertEqual(code, 0)
        self.assertEqual(report['captured_forecasts'], 3)
        models = _objects(self.root, 'models')
        self.assertEqual({m['model']['half_life_days'] for m in models}, {90., 180., 365.})
        self.assertEqual(len({m['input_cutoff'] for m in models}), 1)
        self.assertEqual(len({m['model']['training_data_sha256'] for m in models}), 1)
        self.assertEqual(len({m['model']['team_parameters']['1']['attack'] for m in models}), 3)
        for p, raw in before.items():
            self.assertEqual((self.runtime / p).read_bytes(), raw)
        self.assertFalse((self.runtime / 'prospective').exists())
        for f in _objects(self.root, 'forecasts'):
            self.assertTrue(f['research_only'])
            self.assertFalse(f['production_ledger_eligible'])
            self.assertLess(f['captured_at'], f['kickoff'])

    def test_unknown_team_keeps_all_three_rejections(self):
        self.rows[0]['away_id'] = 999
        self.inputs()
        _, report = self.run_capture()
        self.assertEqual(report['captured_forecasts'], 0)
        coverage = _objects(self.root, 'coverage')
        self.assertEqual(len(coverage), 3)
        self.assertTrue(all('未知球队' in c['reason'] for c in coverage))

    def test_past_match_has_no_fit_and_no_fake_prospective(self):
        self.rows[0]['kickoff'] = iso(NOW)
        self.inputs()
        with patch('football_model.shadow_models.fit_model') as fit:
            _, report = self.run_capture()
        fit.assert_not_called()
        self.assertEqual(report['competitions'][0]['coverage'][0]['reason'], 'not_future_at_real_capture')

    def test_latest_failure_blocks_old_success(self):
        self.index['competitions'][0]['status'] = 'blocked_latest_failure'
        self.inputs()
        code, report = self.run_capture()
        self.assertEqual(code, 1)
        self.assertEqual(report['captured_forecasts'], 0)
        self.assertIn('production_source_blocked', report['errors'][0])

    def test_future_observation_and_research_sources_rejected(self):
        self.data['observed_at'] = iso(NOW + timedelta(hours=1))
        self.inputs()
        code, _ = self.run_capture()
        self.assertEqual(code, 1)
        self.data['observed_at'] = self.observed
        self.report['research_replay'] = True
        self.inputs()
        code, _ = self.run_capture()
        self.assertEqual(code, 1)

    def test_replay_marker_and_public_asof_rejected(self):
        self.write('RESEARCH_REPLAY.json', {'research': True})
        with self.assertRaisesRegex(ValueError, 'historical_replay'):
            self.run_capture()
        with self.assertRaises(SystemExit):
            main(['capture', '--as-of', iso(NOW), '--source-commit', 'a' * 40])

    def test_new_config_half_life_requires_new_protocol(self):
        self.config['half_life_days'] = 90
        with self.assertRaisesRegex(ValueError, 'baseline'):
            self.run_capture()

    def test_fit_failure_retained_and_other_arms_continue(self):
        from football_model.model import fit_model
        def conditional(*args, **kwargs):
            if kwargs['half_life_days'] == 90:
                raise ValueError('test_fit_failure')
            return fit_model(*args, **kwargs)
        with patch('football_model.shadow_models.fit_model', side_effect=conditional):
            code, report = self.run_capture()
        self.assertEqual(code, 1)
        self.assertEqual(report['captured_forecasts'], 2)
        _, scored = score(self.runtime, clock=self.clock)
        self.assertEqual(scored['selected_matches'], 1)
        self.assertEqual(len(scored['incomplete_groups']), 1)

    def test_clock_crosses_kickoff_during_fit_rejects(self):
        calls = [0]
        def clock():
            calls[0] += 1
            return NOW if calls[0] <= 5 else NOW + timedelta(days=2)
        self.clock = clock
        _, report = self.run_capture()
        self.assertEqual(report['captured_forecasts'], 0)

    def result(self, home=1, away=0, observed=None, league_id=39):
        observed = observed or NOW + timedelta(days=1, hours=5)
        row = self.rows[0]
        envelope = {'endpoint': '/fixtures', 'params': {'id': 1001}, 'errors': [], 'results': 1,
            'fetched_at': iso(observed), 'response': [{'fixture': {'id': 1001, 'date': row['kickoff'],
                'status': {'short': 'FT'}}, 'league': {'id': league_id},
                'teams': {'home': {'id': row['home_id'], 'name': row['home_name']},
                          'away': {'id': row['away_id'], 'name': row['away_name']}},
                'score': {'fulltime': {'home': home, 'away': away}}}]}
        production = self.runtime / 'prospective'
        sid = _append(production, 'sources', {'kind': 'independent_fixture_result_response',
            'content': envelope, 'request': {'fixture_id': 1001}, 'client_response_sha256': _digest(envelope)})
        value = {k: row[k] for k in ('match_id', 'home_id', 'away_id', 'kickoff', 'competition')}
        value.update(home_goals=home, away_goals=away, observed_at=iso(observed), captured_at=iso(observed),
                     source_id=sid, collection_method='independent_fixture_sync')
        _append(production, 'results', value)
        self.clock = lambda: observed + timedelta(minutes=1)

    def test_separate_result_scoring_keeps_revisions_and_one_match(self):
        self.run_capture()
        _, pending = score(self.runtime, clock=self.clock)
        self.assertEqual(pending['status_counts'], {'pending': 2})
        self.result()
        code, first = score(self.runtime, clock=self.clock)
        self.assertEqual(code, 0)
        self.assertEqual(first['status_counts'], {'scored': 2})
        self.assertEqual(len(first['matches'][0]['arms']), 2)
        first_scores = len(_objects(self.root, 'scores'))
        self.result(home=0, away=1, observed=NOW + timedelta(days=1, hours=6))
        _, changed = score(self.runtime, clock=self.clock)
        self.assertEqual(changed['selected_matches'], 1)
        self.assertEqual(len(_objects(self.root, 'scores')), first_scores + 2)

    def test_result_wrong_league_or_missing_source_rejected(self):
        self.run_capture()
        self.result(league_id=999)
        code, report = score(self.runtime, clock=self.clock)
        self.assertEqual(code, 1)
        self.assertIn('result_competition_mismatch', report['errors'][0])

    def test_existing_snapshot_tampering_is_not_silently_used(self):
        self.run_capture()
        p = next((self.root / 'forecasts').glob('*.json'))
        v = json.loads(p.read_text())
        v['match_id'] = 500
        p.write_text(json.dumps(v))
        with self.assertRaisesRegex(ValueError, 'hash mismatch'):
            score(self.runtime, clock=self.clock)

    def test_national_without_registry_is_rejected(self):
        self.config['competitions'][0]['mode'] = 'national_baseline'
        _, report = self.run_capture()
        self.assertEqual(report['captured_forecasts'], 0)
        self.assertIn('national_competition', report['competitions'][0]['coverage'][0]['reason'])

    def test_newer_known_reschedule_is_retained_and_rejected(self):
        row = self.rows[0]
        envelope = {'endpoint': '/fixtures', 'params': {'id': row['match_id']}, 'errors': [],
            'fetched_at': iso(NOW - timedelta(minutes=30)), 'response': [{'fixture': {
                'id': row['match_id'], 'date': iso(NOW + timedelta(days=2)), 'status': {'short': 'NS'}},
                'league': {'id': 39}, 'teams': {'home': {'id': 1}, 'away': {'id': 2}}}]}
        raw = json.dumps(envelope).encode()
        digest = hashlib.sha256(raw).hexdigest()
        target = self.runtime / 'quotes/fixtures/1001' / (digest + '.json')
        target.parent.mkdir(parents=True)
        target.write_bytes(raw)
        _, report = self.run_capture()
        self.assertEqual(report['captured_forecasts'], 0)
        self.assertIn('known_newer_fixture_identity_conflict', report['competitions'][0]['coverage'][0]['reason'])
        self.assertTrue(any(s['kind'] == 'quote_fixture_response' for s in _objects(self.root, 'sources')))

    def test_source_received_after_claimed_observation_is_rejected(self):
        self.report['competitions'][0]['seasons'][0]['received_at'] = iso(NOW)
        self.inputs()
        code, report = self.run_capture()
        self.assertEqual(code, 1)
        self.assertEqual(report['captured_forecasts'], 0)

    def test_secondary_fit_failure_does_not_remove_primary_pair(self):
        from football_model.model import fit_model
        def conditional(*args, **kwargs):
            if kwargs['half_life_days'] == 365:
                raise ValueError('test_secondary_failure')
            return fit_model(*args, **kwargs)
        with patch('football_model.shadow_models.fit_model', side_effect=conditional):
            _, report = self.run_capture()
        self.assertEqual(report['captured_forecasts'], 2)
        self.result()
        _, scored = score(self.runtime, clock=self.clock)
        self.assertEqual(scored['comparisons'][0]['n_scored_matches'], 1)
        self.assertEqual(scored['comparisons'][1]['n_scored_matches'], 0)

    def test_rescheduled_identity_selects_first_matching_pair_not_old_pair(self):
        self.run_capture()
        original_kickoff = self.rows[0]['kickoff']
        self.rows[0]['kickoff'] = iso(NOW + timedelta(days=2))
        self.clock = lambda: NOW + timedelta(hours=1)
        self.inputs()
        self.run_capture()
        self.result(observed=NOW + timedelta(days=2, hours=5))
        _, scored = score(self.runtime, clock=self.clock)
        self.assertEqual(scored['selected_matches'], 1)
        self.assertEqual(scored['comparisons'][0]['n_scored_matches'], 1)
        self.assertEqual(len(scored['matches'][0]['identity_excluded_forecast_ids']), 3)
        self.assertEqual(sum(f['kickoff'] == original_kickoff for f in _objects(self.root, 'forecasts')), 3)

    def test_durable_payload_write_crossing_kickoff_cannot_enter_live_forecasts(self):
        current = [NOW]
        self.clock = lambda: current[0]
        def slow_append(root, collection, value):
            oid = _append(root, collection, value)
            if collection == 'forecast_payloads':
                current[0] = NOW + timedelta(days=2)
            return oid
        with patch('football_model.shadow_models._append', side_effect=slow_append):
            _, report = self.run_capture()
        self.assertEqual(report['captured_forecasts'], 0)
        self.assertEqual(len(_objects(self.root, 'forecasts')), 0)
        self.assertEqual(len(_objects(self.root, 'forecast_payloads')), 1)
        self.assertTrue(any('storage_completed_at_or_after' in c.get('reason', '') for c in _objects(self.root, 'coverage')))
        _, scored = score(self.runtime, clock=self.clock)
        self.assertEqual(scored['selected_matches'], 0)

    def test_scoring_requires_frozen_training_parent(self):
        self.run_capture()
        f = _objects(self.root, 'forecasts')[0]
        (self.root / 'sources' / (f['parents']['data_source'] + '.json')).unlink()
        with self.assertRaises(FileNotFoundError):
            score(self.runtime, clock=self.clock)

    def test_protocol_registration_not_rewritten_on_later_capture(self):
        self.run_capture()
        initial = {p.name: p.read_bytes() for p in (self.root / 'protocols').glob('*.json')}
        self.clock = lambda: NOW + timedelta(hours=1)
        self.run_capture()
        self.assertEqual(initial, {p.name: p.read_bytes() for p in (self.root / 'protocols').glob('*.json')})
        _, report = score(self.runtime, clock=self.clock)
        self.assertEqual(report['selected_matches'], 1)
        self.assertEqual(report['forecast_versions'], 6)

    def test_capacity_preflight_stops_fit_and_reports_deferral_without_zero_counts(self):
        from football_model.shadow_models import _runtime_bytes
        threshold = _runtime_bytes(self.runtime) - 1
        with patch('football_model.shadow_models.STORAGE_LIMIT_BYTES', threshold), patch('football_model.shadow_models.fit_model') as fit:
            code, report = self.run_capture()
        fit.assert_not_called()
        self.assertEqual(code, 1)
        self.assertEqual(report['status'], 'capacity_deferred')
        self.assertIsNone(report['counts_for_partial_capture'])
        self.assertEqual(len(_objects(self.root, 'forecasts')), 0)
        self.assertIsNotNone(report['capacity_event_id'])

    def test_incremental_capacity_blocks_crossing_and_duplicate_objects_are_free(self):
        from football_model.shadow_models import _runtime_bytes, _writer, _append as guarded_append, ShadowCapacityError
        from football_model.prospective import _json_bytes
        first = {'already_saved': True}
        start = _runtime_bytes(self.runtime)
        with patch('football_model.shadow_models.STORAGE_LIMIT_BYTES', start + len(_json_bytes(first))), \
                patch('football_model.shadow_models._runtime_bytes', wraps=_runtime_bytes) as scans:
            with _writer(self.runtime) as root:
                oid = guarded_append(root, 'sources', first)
                self.assertEqual(guarded_append(root, 'sources', first), oid)
                with self.assertRaises(ShadowCapacityError):
                    guarded_append(root, 'sources', {'new_object': True})
        self.assertEqual(scans.call_count, 1)
        self.assertEqual(len(_objects(self.root, 'sources')), 1)

    def test_capacity_mid_capture_preserves_objects_and_current_failure_report(self):
        from football_model.shadow_models import _runtime_bytes
        start = _runtime_bytes(self.runtime)
        with patch('football_model.shadow_models.STORAGE_LIMIT_BYTES', start + 5000):
            code, report = self.run_capture()
        self.assertEqual(code, 1)
        self.assertEqual(report['status'], 'capacity_deferred')
        self.assertTrue(report['partial_objects_preserved'])
        self.assertGreater(len(_objects(self.root, 'sources')), 0)
        self.assertEqual(json.loads((self.root / 'reports/latest.json').read_text())['status'], 'capacity_deferred')

    def test_score_attempts_existing_forecasts_after_capture_capacity_deferral(self):
        from football_model.shadow_models import _runtime_bytes
        self.run_capture()
        before = {p.name: p.read_bytes() for p in (self.root / 'forecasts').glob('*.json')}
        with patch('football_model.shadow_models.STORAGE_LIMIT_BYTES', _runtime_bytes(self.runtime) - 1):
            with patch('football_model.shadow_models._saved_forecast_valid', wraps=__import__('football_model.shadow_models', fromlist=['_saved_forecast_valid'])._saved_forecast_valid) as checked:
                code, report = score(self.runtime, clock=self.clock)
            self.assertEqual(checked.call_count, 3)
        self.assertEqual(code, 1)
        self.assertEqual(report['status'], 'capacity_deferred')
        self.assertEqual(before, {p.name: p.read_bytes() for p in (self.root / 'forecasts').glob('*.json')})


if __name__ == '__main__':
    unittest.main()
