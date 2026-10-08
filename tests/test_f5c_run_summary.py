"""F5-C stage outcomes remain separate from production training."""
import copy
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from football_model.run_summary import build_summary, _canonical, _digest, main, markdown


class F5CSummaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.now = datetime(2026, 10, 8, 10, 5, tzinfo=timezone.utc)
        self.context = {'schema_version': 'layered-workflow-summary-v4',
            'started_at': '2026-10-08T10:00:00Z', 'source_commit': 'a' * 40,
            'mode': 'shadow_only', 'league': 'all',
            'stage_started_at': {'total_shrink_shadow': '2026-10-08T10:03:00Z'}}
        self.outcomes = {key: 'success' for key in ('restore', 'total_shrink_shadow', 'check', 'persist')}

    def build(self):
        return build_summary(self.root, self.context, self.outcomes, now=self.now)

    def test_dedicated_mode_does_not_require_uninvoked_model_or_api_stages(self):
        value = self.build()
        self.assertEqual(value['required_steps'], ['restore', 'total_shrink_shadow', 'check', 'persist'])
        self.assertEqual(value['necessary_steps_outcome'], 'success')
        self.assertEqual(value['model']['status'], 'unknown')
        self.assertNotIn('model', value['required_steps'])
        self.assertNotIn('quotes', value['required_steps'])

    def test_shadow_failure_does_not_become_success_after_successful_save(self):
        self.outcomes['total_shrink_shadow'] = 'failure'
        value = self.build()
        self.assertEqual(value['necessary_steps_not_successful'], ['total_shrink_shadow'])
        self.assertEqual(value['state_save_outcome'], 'success')
        self.assertEqual(value['necessary_steps_outcome'], 'failure')

    def test_missing_f5_report_is_unknown_without_fabricated_zero(self):
        value = self.build()['total_shrink_shadow']
        self.assertEqual(value['capture']['status'], 'unknown')
        self.assertEqual(value['scoring']['status'], 'unknown')
        self.assertNotIn('captured_pairs', value['capture'])

    def test_stage_is_required_when_wired_into_daily_context(self):
        self.context['mode'] = 'train'
        for key in ('model', 'replay', 'prospective'):
            self.outcomes[key] = 'success'
        self.outcomes['total_shrink_shadow'] = 'failure'
        value = self.build()
        self.assertIn('total_shrink_shadow', value['required_steps'])
        self.assertEqual(value['necessary_steps_not_successful'], ['total_shrink_shadow'])
        del self.context['stage_started_at']['total_shrink_shadow']
        self.assertNotIn('total_shrink_shadow', self.build()['required_steps'])

    def test_dedicated_mode_and_stage_marker_accepted_by_cli(self):
        path = self.root / 'context.json'
        env = {'GITHUB_SHA': 'a' * 40, 'MODE': 'shadow_only', 'LEAGUE': 'all'}
        with patch.dict('os.environ', env, clear=True):
            self.assertEqual(main(['begin', '--context', str(path)]), 0)
        self.assertEqual(main(['mark', '--context', str(path), '--stage', 'total_shrink_shadow']), 0)
        value = json.loads(path.read_text())
        self.assertEqual(value['mode'], 'shadow_only')
        self.assertIn('total_shrink_shadow', value['stage_started_at'])


class F5CAssociationTests(unittest.TestCase):
    def build(self):
        return build_summary(self.root, self.context, self.outcomes, now=self.now)

    def setUp(self):
        F5CSummaryTests.setUp(self)
        self.protocol_id = self.append('protocols', {'registered_at': '2026-10-08T10:03:01Z'})
        self.forecast = {'protocol_id': self.protocol_id, 'competition': 'epl', 'match_id': 1,
            'source_commit': 'a' * 40, 'input_id': 'input-one', 'captured_at': '2026-10-08T10:03:10Z'}
        self.forecast_id = self.append('forecasts', self.forecast)
        self.run = {'source_commit': 'a' * 40, 'protocol_id': self.protocol_id,
            'started_at': '2026-10-08T10:03:01Z', 'completed_at': '2026-10-08T10:03:20Z',
            'confirmation_eligible': False, 'production_models_changed': False,
            'new_fits': 0, 'new_api_calls': 0, 'captured_forecasts': 1, 'captured_matches': 1,
            'coverage_status_counts': {'captured': 1}, 'errors': [],
            'competitions': [{'competition': 'epl', 'status': 'completed', 'input_id': 'input-one',
                'coverage': [{'competition': 'epl', 'match_id': 1, 'status': 'captured', 'forecast_id': self.forecast_id}]}]}
        self.save_capture()
        self.score = {'source_commit': 'a' * 40, 'run_started_at': '2026-10-08T10:03:25Z',
            'scored_at': '2026-10-08T10:03:25Z', 'generated_at': '2026-10-08T10:03:30Z',
            'confirmation_eligible': False, 'production_models_changed': False, 'new_fits': 0, 'new_api_calls': 0,
            'forecast_versions': 3, 'selected_matches': 1, 'n_scored_matches': 0, 'status_counts': {'pending': 1},
            'matches': [{'protocol_id': self.protocol_id, 'competition': 'epl', 'match_id': 1,
                         'forecast_id': self.forecast_id, 'status': 'pending'}], 'errors': []}
        self.save_score()

    def write(self, name, value):
        path = self.root / 'total_shrink_shadow' / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(_canonical(value))

    def append(self, collection, value):
        object_id = _digest(value)
        self.write(collection + '/' + object_id + '.json', value)
        return object_id

    def save_capture(self):
        self.run_id = self.append('runs', self.run)
        self.latest = {k: v for k, v in self.run.items() if k != 'competitions'}
        self.latest['report_id'] = self.run_id
        self.write('reports/latest.json', self.latest)

    def save_score(self):
        self.score_id = self.append('score_runs', self.score)
        self.score_latest = {k: v for k, v in self.score.items() if k != 'matches'}
        self.score_latest['report_id'] = self.score_id
        self.write('reports/score-latest.json', self.score_latest)

    def test_missing_f5_report_is_unknown_without_fabricated_zero(self):
        (self.root / 'total_shrink_shadow/reports/latest.json').unlink()
        (self.root / 'total_shrink_shadow/reports/score-latest.json').unlink()
        F5CSummaryTests.test_missing_f5_report_is_unknown_without_fabricated_zero(self)

    def test_current_receipts_keep_version_and_match_counts_separate(self):
        value = self.build()['total_shrink_shadow']
        self.assertEqual(value['capture']['status'], 'associated_current_capture')
        self.assertEqual(value['capture']['captured_pair_versions'], 1)
        self.assertEqual(value['scoring']['forecast_versions'], 3)
        self.assertEqual(value['scoring']['independent_matches_across_protocols'], 1)
        self.assertEqual(value['scoring']['pending_matches_by_protocol'], 1)
        self.assertFalse(value['scoring']['confirmation_eligible'])
        self.assertIn('一份对照回执', markdown(self.build()))

    def test_old_or_wrong_commit_pointers_never_count_as_current(self):
        for alter in ('stage', 'source'):
            with self.subTest(alter=alter):
                context = copy.deepcopy(self.context)
                if alter == 'stage':
                    context['stage_started_at']['total_shrink_shadow'] = '2026-10-08T10:04:00Z'
                else:
                    context['source_commit'] = 'b' * 40
                value = build_summary(self.root, context, self.outcomes, now=self.now)['total_shrink_shadow']
                self.assertEqual(value['capture']['status'], 'unknown')
                self.assertEqual(value['scoring']['status'], 'unknown')
                self.assertNotIn('captured_pair_versions', value['capture'])

    def test_immutable_capture_or_forecast_tamper_is_rejected(self):
        self.write('forecasts/' + self.forecast_id + '.json', {'tampered': True})
        value = self.build()['total_shrink_shadow']
        self.assertEqual(value['capture']['status'], 'unknown')
        self.assertEqual(value['scoring']['status'], 'unknown')

    def test_latest_count_changes_do_not_override_immutable_report(self):
        self.latest['captured_matches'] = 2
        self.write('reports/latest.json', self.latest)
        self.score_latest['selected_matches'] = 2
        self.write('reports/score-latest.json', self.score_latest)
        value = self.build()['total_shrink_shadow']
        self.assertEqual(value['capture']['status'], 'unknown')
        self.assertEqual(value['scoring']['status'], 'unknown')

    def test_duplicate_score_identity_cannot_count_as_new_match(self):
        self.score['matches'].append(copy.deepcopy(self.score['matches'][0]))
        self.score.update(selected_matches=2, status_counts={'pending': 2})
        self.save_score()
        self.assertEqual(self.build()['total_shrink_shadow']['scoring']['status'], 'unknown')

    def test_missing_forecast_version_count_is_unknown_not_zero(self):
        del self.score['forecast_versions']
        self.save_score()
        self.assertEqual(self.build()['total_shrink_shadow']['scoring']['status'], 'unknown')

    def test_raw_error_text_never_exported(self):
        secret = 'uncontrolled-field-that-must-not-appear-123'
        self.run['errors'] = [secret]
        self.score['errors'] = [secret]
        self.save_capture()
        self.save_score()
        value = self.build()
        self.assertNotIn(secret, json.dumps(value) + markdown(value))
        self.assertEqual(value['total_shrink_shadow']['capture']['error_count'], 1)

    def test_boolean_zero_is_not_accepted_as_no_new_fit(self):
        self.run['new_fits'] = False
        self.save_capture()
        self.assertEqual(self.build()['total_shrink_shadow']['capture']['status'], 'unknown')

    def test_current_score_cannot_predate_current_capture_completion(self):
        self.score.update(run_started_at='2026-10-08T10:03:05Z', scored_at='2026-10-08T10:03:05Z')
        self.save_score()
        self.assertEqual(self.build()['total_shrink_shadow']['scoring']['status'], 'unknown')

    def test_no_native_parent_reason_and_zero_saved_pairs_are_explicit(self):
        self.run.update(captured_forecasts=0, captured_matches=0, coverage_status_counts={'rejected': 1})
        row = self.run['competitions'][0]['coverage'][0]
        row.update(status='rejected', reason='no_native_recorded_production_forecast')
        self.save_capture()
        value = self.build()
        capture = value['total_shrink_shadow']['capture']
        self.assertEqual(capture['captured_pair_versions'], 0)
        self.assertEqual(capture['coverage_rejection_reasons'], {'no_native_recorded_production_forecast': 1})
        self.assertFalse(capture['forecast_admission_is_quality_acceptance'])
        self.assertIn('no_native_recorded_production_forecast=1', markdown(value))

    def test_scored_row_requires_bound_original_score(self):
        row = {**self.score['matches'][0], 'status': 'scored'}
        row['score_id'] = self.append('scores', row)
        self.score.update(matches=[row], status_counts={'scored': 1}, n_scored_matches=1)
        self.save_score()
        self.assertEqual(self.build()['total_shrink_shadow']['scoring']['scored_matches_by_protocol'], 1)
        self.write('scores/' + row['score_id'] + '.json', {'tampered': True})
        self.assertEqual(self.build()['total_shrink_shadow']['scoring']['status'], 'unknown')

    def test_capacity_report_remains_visible_after_successful_persist(self):
        for operation, filename in (('capture', 'latest.json'), ('score', 'score-latest.json')):
            value = {'status': 'capacity_deferred', 'operation': operation, 'source_commit': 'a' * 40,
                'generated_at': '2026-10-08T10:03:40Z', 'estimated_persisted_bytes': 450000000,
                'logical_bytes': 650000000, 'immutable_event_saved': True}
            value['capacity_event_id'] = self.append('capacity_events', value)
            self.write('reports/' + filename, value)
        self.outcomes['total_shrink_shadow'] = 'failure'
        value = self.build()
        self.assertEqual(value['state_save_outcome'], 'success')
        self.assertEqual(value['total_shrink_shadow']['capture']['status'], 'capacity_deferred')
        self.assertIsNone(value['total_shrink_shadow']['capture']['captured_pair_versions'])
        self.assertEqual(value['necessary_steps_outcome'], 'failure')


if __name__ == '__main__':
    unittest.main()
