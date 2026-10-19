"""Engineering tests use synthetic matches, never evidence of prediction gains."""
import copy
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

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

    def test_retired_national_config_cannot_start_a_shadow_capture(self):
        self.config['competitions'][0]['mode'] = 'national_baseline'
        with self.assertRaisesRegex(ValueError, 'unknown_or_audit_only_competition'):
            self.run_capture()

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
        from football_model.state_storage import estimate_runtime
        threshold = estimate_runtime(self.runtime)['persisted_bytes'] - 1
        with patch('football_model.shadow_models.STORAGE_LIMIT_BYTES', threshold), patch('football_model.shadow_models.fit_model') as fit:
            code, report = self.run_capture()
        fit.assert_not_called()
        self.assertEqual(code, 1)
        self.assertEqual(report['status'], 'capacity_deferred')
        self.assertIsNone(report['counts_for_partial_capture'])
        self.assertEqual(len(_objects(self.root, 'forecasts')), 0)
        self.assertIsNotNone(report['capacity_event_id'])

    def test_capacity_diagnostic_remeasures_under_writer_lock(self):
        from football_model import shadow_models as sm
        actual = sm.estimate_runtime(self.runtime)
        stale = sm.ShadowCapacityError({'estimated_persisted_bytes': 0, 'logical_bytes': 0})
        with patch.object(sm, 'estimate_runtime', return_value=actual) as measured:
            code, report = sm._capacity_report(self.runtime, 'capture', stale, self.clock(), self.clock)
        measured.assert_called_once()
        self.assertEqual(code, 1)
        self.assertEqual(report['estimated_persisted_bytes'], actual['persisted_bytes'])
        with sm._writer(self.runtime):
            with self.assertRaisesRegex(ValueError, 'already_active'):
                sm._capacity_report(self.runtime, 'capture', stale, self.clock(), self.clock)

    def test_incremental_capacity_blocks_crossing_and_duplicate_objects_are_free(self):
        from football_model.shadow_models import _writer, _append as guarded_append, ShadowCapacityError
        from football_model.state_storage import estimate_runtime, storage_cost
        from football_model.prospective import _json_bytes
        first = {'already_saved': True}
        first_path = 'shadow_models/sources/' + _digest(first) + '.json'
        start = estimate_runtime(self.runtime)['persisted_bytes']
        with patch('football_model.shadow_models.STORAGE_LIMIT_BYTES', start + storage_cost(first_path, _json_bytes(first))), \
                patch('football_model.shadow_models.estimate_runtime', wraps=estimate_runtime) as scans:
            with _writer(self.runtime) as root:
                oid = guarded_append(root, 'sources', first)
                self.assertEqual(guarded_append(root, 'sources', first), oid)
                with self.assertRaises(ShadowCapacityError):
                    guarded_append(root, 'sources', {'new_object': True})
        self.assertEqual(scans.call_count, 1)
        self.assertEqual(len(_objects(self.root, 'sources')), 1)

    def test_capacity_mid_capture_preserves_objects_and_current_failure_report(self):
        from football_model.state_storage import estimate_runtime
        start = estimate_runtime(self.runtime)['persisted_bytes']
        with patch('football_model.shadow_models.STORAGE_LIMIT_BYTES', start + 5000):
            code, report = self.run_capture()
        self.assertEqual(code, 1)
        self.assertEqual(report['status'], 'capacity_deferred')
        self.assertTrue(report['partial_objects_preserved'])
        self.assertGreater(len(_objects(self.root, 'sources')), 0)
        self.assertEqual(json.loads((self.root / 'reports/latest.json').read_text())['status'], 'capacity_deferred')

    def test_score_attempts_existing_forecasts_after_capture_capacity_deferral(self):
        from football_model.state_storage import estimate_runtime
        self.run_capture()
        before = {p.name: p.read_bytes() for p in (self.root / 'forecasts').glob('*.json')}
        with patch('football_model.shadow_models.STORAGE_LIMIT_BYTES', estimate_runtime(self.runtime)['persisted_bytes'] - 1):
            with patch('football_model.shadow_models._saved_forecast_valid', wraps=__import__('football_model.shadow_models', fromlist=['_saved_forecast_valid'])._saved_forecast_valid) as checked:
                code, report = score(self.runtime, clock=self.clock)
            self.assertEqual(checked.call_count, 3)
        self.assertEqual(code, 1)
        self.assertEqual(report['status'], 'capacity_deferred')
        self.assertEqual(before, {p.name: p.read_bytes() for p in (self.root / 'forecasts').glob('*.json')})

    def test_compressible_history_above_raw_threshold_allows_real_capture(self):
        from football_model.state_storage import estimate_runtime
        from football_model.prospective import _json_bytes
        value = {'cold_evidence': 'a' * (3 * 1024 * 1024)}
        path = self.runtime / 'quotes/raw/1001' / (_digest(value) + '.json')
        path.parent.mkdir(parents=True)
        original = _json_bytes(value)
        path.write_bytes(original)
        estimate = estimate_runtime(self.runtime)
        threshold = estimate['persisted_bytes'] + 1024 * 1024
        self.assertGreater(estimate['raw_bytes'], threshold)
        with patch('football_model.shadow_models.STORAGE_LIMIT_BYTES', threshold):
            code, report = self.run_capture()
        self.assertEqual(code, 0)
        self.assertEqual(report['captured_forecasts'], 3)
        self.assertGreater(report['storage_budget']['runtime_bytes'], threshold)
        self.assertLess(report['storage_budget']['estimated_persisted_bytes'], threshold)
        self.assertEqual(path.read_bytes(), original)

    def test_logical_guard_stops_highly_compressible_capture(self):
        from football_model.state_storage import estimate_runtime
        from football_model.shadow_models import CAPACITY_REPORT_RESERVE_BYTES
        estimate = estimate_runtime(self.runtime)
        logical_limit = estimate['raw_bytes'] + CAPACITY_REPORT_RESERVE_BYTES - 1
        with patch('football_model.shadow_models.MAX_LOGICAL_BYTES', logical_limit), \
                patch('football_model.shadow_models.fit_model') as fit:
            code, report = self.run_capture()
        fit.assert_not_called()
        self.assertEqual(code, 1)
        self.assertEqual(report['status'], 'capacity_deferred')
        self.assertLess(report['estimated_persisted_bytes'], report['limit_bytes'])
        self.assertEqual(report['logical_limit_bytes'], logical_limit)
        self.assertEqual(report['logical_capture_limit_bytes'], logical_limit - CAPACITY_REPORT_RESERVE_BYTES)

    def test_replay_expansion_is_in_logical_guard_but_not_runtime_bytes(self):
        import gzip
        from football_model.state_storage import estimate_runtime
        from football_model.shadow_models import CAPACITY_REPORT_RESERVE_BYTES
        from football_model.prospective import _json_bytes
        value = {'cold_replay': 'a' * (1024 * 1024)}
        path = self.runtime / 'replay/bundles' / (_digest(value) + '.json.gz')
        path.parent.mkdir(parents=True)
        path.write_bytes(gzip.compress(_json_bytes(value), mtime=0))
        estimate = estimate_runtime(self.runtime)
        logical_limit = estimate['logical_bytes'] + CAPACITY_REPORT_RESERVE_BYTES - 1
        with patch('football_model.shadow_models.MAX_LOGICAL_BYTES', logical_limit), \
                patch('football_model.shadow_models.fit_model') as fit:
            code, report = self.run_capture()
        fit.assert_not_called()
        self.assertEqual(code, 1)
        self.assertEqual(report['runtime_bytes'], estimate['raw_bytes'])
        self.assertEqual(report['logical_bytes'], estimate['logical_bytes'])
        self.assertGreater(report['logical_bytes'], report['runtime_bytes'])

    def test_failed_append_does_not_charge_raw_or_persisted_budget(self):
        from football_model.shadow_models import _writer, _BUDGET, _append as guarded_append
        with _writer(self.runtime) as root:
            before = {k: _BUDGET.get()[k] for k in ('runtime_bytes', 'estimated_persisted_bytes')}
            with patch('football_model.shadow_models._ledger_append', side_effect=OSError('disk rejected')):
                with self.assertRaises(OSError):
                    guarded_append(root, 'sources', {'new': True})
            self.assertEqual(before, {k: _BUDGET.get()[k] for k in before})

    def test_single_file_guard_prevents_compressible_object_unsavable_by_cloud(self):
        from football_model.shadow_models import _writer, _append as guarded_append, ShadowCapacityError
        with _writer(self.runtime) as root, patch('football_model.shadow_models.MAX_FILE_BYTES', 100):
            with self.assertRaises(ShadowCapacityError) as raised:
                guarded_append(root, 'sources', {'large_but_compressible': 'a' * 1000})
        self.assertEqual(raised.exception.budget['capacity_constraint'], 'file_limit')
        self.assertEqual(len(_objects(self.root, 'sources')), 0)

    def test_inline_latest_charges_actual_serialized_bytes_without_gzip_discount(self):
        from football_model.shadow_models import _writer, _BUDGET, _latest
        value = {'z': 'x' * 3000, 'a': 1}
        with _writer(self.runtime) as root:
            raw_before, persisted_before = (_BUDGET.get()[k] for k in ('runtime_bytes', 'estimated_persisted_bytes'))
            _latest(root, 'latest.json', value)
            written = (root / 'reports/latest.json').read_bytes()
            self.assertEqual(_BUDGET.get()['runtime_bytes'] - raw_before, len(written))
            self.assertEqual(_BUDGET.get()['estimated_persisted_bytes'] - persisted_before, len(written))

    def test_exhausted_diagnostic_reserve_still_publishes_explicit_bounded_deferral(self):
        from football_model.state_storage import estimate_runtime
        from football_model.shadow_models import CAPACITY_REPORT_RESERVE_BYTES
        threshold = estimate_runtime(self.runtime)['persisted_bytes'] - CAPACITY_REPORT_RESERVE_BYTES - 1
        with patch('football_model.shadow_models.STORAGE_LIMIT_BYTES', threshold):
            code, report = self.run_capture()
        self.assertEqual(code, 1)
        self.assertFalse(report['immutable_event_saved'])
        self.assertIsNone(report['capacity_event_id'])
        self.assertEqual(report['diagnostic_evidence'], 'bounded_current_latest_only')
        self.assertEqual(len(_objects(self.root, 'capacity_events')), 0)
        self.assertEqual(json.loads((self.root / 'reports/latest.json').read_text()), report)

    def test_matrix_nextafter_roundoff_scores_legacy_forecasts_without_rewriting(self):
        from football_model.model import score_distribution
        self.run_capture()
        before = {p.name:p.read_bytes() for p in (self.root / 'forecasts').glob('*.json')}
        def nearby(*args, **kwargs):
            value = score_distribution(*args, **kwargs)
            value['score_matrix'][0][0] = float(np.nextafter(value['score_matrix'][0][0], np.inf))
            return value
        with patch('football_model.shadow_models.score_distribution', side_effect=nearby):
            code, report = score(self.runtime, clock=self.clock)
        self.assertEqual(code, 0)
        self.assertEqual(report['status_counts'], {'pending':2})
        self.assertEqual(report['numeric_replay_matrix_abs_tol'], 1e-15)
        self.assertEqual(before, {p.name:p.read_bytes() for p in (self.root / 'forecasts').glob('*.json')})

    def test_matrix_actual_probability_drift_is_rejected(self):
        from football_model.model import score_distribution
        self.run_capture()
        def drift(*args, **kwargs):
            value = score_distribution(*args, **kwargs)
            value['score_matrix'][0][0] += 1e-12
            value['score_matrix'][0][1] -= 1e-12
            return value
        with patch('football_model.shadow_models.score_distribution', side_effect=drift):
            with self.assertRaisesRegex(ValueError, 'prediction_does_not_recompute'):
                score(self.runtime, clock=self.clock)

    def test_nonmatrix_nextafter_remains_exactly_rejected(self):
        from football_model.model import score_distribution
        self.run_capture()
        def drift(*args, **kwargs):
            value = score_distribution(*args, **kwargs)
            value['expected_goals']['home'] = float(np.nextafter(value['expected_goals']['home'], np.inf))
            return value
        with patch('football_model.shadow_models.score_distribution', side_effect=drift):
            with self.assertRaisesRegex(ValueError, 'prediction_does_not_recompute'):
                score(self.runtime, clock=self.clock)

    def test_1x2_and_tail_nextafter_scores_legacy_forecasts_without_rewriting(self):
        from football_model.model import score_distribution
        self.run_capture()
        before = {p.name:p.read_bytes() for p in (self.root / 'forecasts').glob('*.json')}
        protocol_before = {p.name:p.read_bytes() for p in (self.root / 'protocols').glob('*.json')}
        def nearby(*args, **kwargs):
            value = score_distribution(*args, **kwargs)
            for key in ('home_win', 'draw', 'away_win'):
                value['probabilities'][key] = float(np.nextafter(value['probabilities'][key], np.inf))
            key = 'tail_mass_before_normalization'
            value[key] = float(np.nextafter(value[key], np.inf))
            return value
        with patch('football_model.shadow_models.score_distribution', side_effect=nearby):
            code, report = score(self.runtime, clock=self.clock)
        self.assertEqual(code, 0)
        self.assertEqual(report['status_counts'], {'pending':2})
        self.assertEqual(report['numeric_replay_probability_abs_tol'], 1e-15)
        self.assertEqual(before, {p.name:p.read_bytes() for p in (self.root / 'forecasts').glob('*.json')})
        self.assertEqual(protocol_before, {p.name:p.read_bytes() for p in (self.root / 'protocols').glob('*.json')})

    def test_1x2_and_tail_actual_drift_is_rejected(self):
        from football_model.model import score_distribution
        from football_model.shadow_models import _numeric_replay_equivalent
        self.run_capture()
        saved = _objects(self.root, 'forecasts')[0]['prediction']
        for key in ('home_win', 'draw', 'away_win', 'tail_mass_before_normalization'):
            with self.subTest(field=key):
                drift = copy.deepcopy(saved)
                target = drift if key == 'tail_mass_before_normalization' else drift['probabilities']
                target[key] += 1e-12
                self.assertFalse(_numeric_replay_equivalent(drift, saved))
                self.assertFalse(_numeric_replay_equivalent(saved, drift))

    def test_1x2_and_tail_malformed_values_or_keys_are_rejected(self):
        from football_model.shadow_models import _numeric_replay_equivalent
        self.run_capture()
        saved = _objects(self.root, 'forecasts')[0]['prediction']
        for key in ('home_win', 'draw', 'away_win', 'tail_mass_before_normalization'):
            for invalid in (True, False, '0.2', None, float('nan'), float('inf'), -1e-17, 1 + 1e-10, [], {}):
                with self.subTest(field=key, invalid_type=type(invalid).__name__):
                    bad = copy.deepcopy(saved)
                    target = bad if key == 'tail_mass_before_normalization' else bad['probabilities']
                    target[key] = invalid
                    self.assertFalse(_numeric_replay_equivalent(bad, saved))
                    self.assertFalse(_numeric_replay_equivalent(saved, bad))
        invalid = []
        for key in ('probabilities', 'tail_mass_before_normalization'):
            bad = copy.deepcopy(saved)
            del bad[key]
            invalid.append(bad)
        for key in ('home_win', 'draw', 'away_win'):
            bad = copy.deepcopy(saved)
            del bad['probabilities'][key]
            invalid.append(bad)
        bad = copy.deepcopy(saved)
        bad['probabilities']['extra'] = 0.0
        invalid.append(bad)
        bad = copy.deepcopy(saved)
        bad['probabilities'] = [0.25, 0.5, 0.25]
        invalid.append(bad)
        for bad in invalid:
            self.assertFalse(_numeric_replay_equivalent(bad, saved))
            self.assertFalse(_numeric_replay_equivalent(saved, bad))

    def test_probability_roundoff_keeps_axes_and_all_other_metadata_exact(self):
        from football_model.shadow_models import _numeric_replay_equivalent
        self.run_capture()
        saved = _objects(self.root, 'forecasts')[0]['prediction']
        changes = [('score_matrix_axes', {'rows':'away_goals_from_0', 'columns':'home_goals_from_0'}),
                   ('model_age_days', float(np.nextafter(saved['model_age_days'], np.inf))),
                   ('tail_tolerance', float(np.nextafter(saved['tail_tolerance'], np.inf))),
                   ('model_version', 'changed'), ('home_name', 'changed'), ('lineup_adjusted', True)]
        for field, changed in changes:
            with self.subTest(field=field):
                bad = copy.deepcopy(saved)
                bad['probabilities']['home_win'] = float(np.nextafter(bad['probabilities']['home_win'], np.inf))
                bad[field] = changed
                self.assertFalse(_numeric_replay_equivalent(bad, saved))
                self.assertFalse(_numeric_replay_equivalent(saved, bad))

    def test_numeric_replay_rejects_shape_nan_probability_and_nonnumeric_cells(self):
        from football_model.shadow_models import _numeric_replay_equivalent
        saved = {'score_matrix':[[0.25,0.25],[0.25,0.25]], 'other':{'exact':1},
                 'probabilities':{'home_win':0.25, 'draw':0.5, 'away_win':0.25},
                 'tail_mass_before_normalization':0.0}
        self.assertTrue(_numeric_replay_equivalent(copy.deepcopy(saved), saved))
        invalid = [ [[1.0]], [[0.25,0.25,0.0],[0.25,0.25,0.0]],
                    [[float('nan'),0.25],[0.25,0.25]], [[-1e-17,0.5],[0.25,0.25]],
                    [['0.25',0.25],[0.25,0.25]], [[True,0.0],[0.0,0.0]],
                    [[0.5],[0.25,0.25]], [[0.25,0.25],[0.25,0.3]],
                    np.asarray(saved['score_matrix']) ]
        for matrix in invalid:
            with self.subTest(matrix_type=type(matrix).__name__):
                malformed = {**saved, 'score_matrix':matrix}
                self.assertFalse(_numeric_replay_equivalent(malformed, saved))
                self.assertFalse(_numeric_replay_equivalent(saved, malformed))
        self.assertFalse(_numeric_replay_equivalent({'other':1}, {'other':1}))
        self.assertFalse(_numeric_replay_equivalent({'score_matrix':[[1+1e-10]]}, {'score_matrix':[[1.0]]}))

    def test_matrix_roundoff_does_not_relax_parent_hash(self):
        from football_model.shadow_models import _saved_forecast_valid
        self.run_capture()
        forecast = _objects(self.root, 'forecasts')[0]
        parent = self.root / 'models' / (forecast['model_id'] + '.json')
        value = json.loads(parent.read_text())
        value['input_id'] = '0' * 64
        parent.write_text(json.dumps(value))
        with self.assertRaisesRegex(ValueError, 'parent_hash_mismatch'):
            _saved_forecast_valid(self.root, forecast, NOW)

    def test_numeric_replay_keeps_actual_clock_order_gate(self):
        from football_model.shadow_models import _saved_forecast_valid
        self.run_capture()
        forecast = _objects(self.root, 'forecasts')[0]
        with self.assertRaisesRegex(ValueError, 'actual_time_order_invalid'):
            _saved_forecast_valid(self.root, forecast, NOW - timedelta(seconds=1))

    def test_numerical_environment_is_report_metadata_and_protocol_is_unchanged(self):
        import platform
        import scipy
        _, capture_report = self.run_capture()
        _, score_report = score(self.runtime, clock=self.clock)
        expected = {'python_version':platform.python_version(), 'numpy_version':np.__version__,
                    'scipy_version':scipy.__version__, 'platform_machine':platform.machine()}
        self.assertEqual(capture_report['numerical_environment'], expected)
        self.assertEqual(score_report['numerical_environment'], expected)
        protocol = _objects(self.root, 'protocols')[0]
        self.assertNotIn('numerical_environment', protocol['spec'])
        self.assertNotIn('numeric_replay_matrix_abs_tol', protocol['spec'])
        self.assertNotIn('numeric_replay_probability_abs_tol', protocol['spec'])
        self.assertTrue(all('numerical_environment' not in f for f in _objects(self.root, 'forecasts')))
        before = {p.name:p.read_bytes() for p in (self.root / 'protocols').glob('*.json')}
        self.clock = lambda: NOW + timedelta(hours=1)
        self.run_capture()
        self.assertEqual(before, {p.name:p.read_bytes() for p in (self.root / 'protocols').glob('*.json')})


if __name__ == '__main__':
    unittest.main()
