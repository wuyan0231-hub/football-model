"""Current-stage association and denominator tests for F4 observations."""
import copy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from football_model.run_summary import _canonical, _digest, main, markdown
import test_s2_run_summary as summary_fixtures


class F4SummaryTests(unittest.TestCase):
    def setUp(self):
        self.f = summary_fixtures.RunSummaryTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.f.context['stage_started_at']['shadow_models'] = '2026-10-07T10:03:00Z'
        self.f.outcomes['shadow_models'] = 'success'
        self.protocol_id = self.append('protocols', {'registered_at': '2026-10-07T10:03:02Z'})
        self.arms = []
        for arm in ('baseline180', 'candidate90', 'candidate365'):
            forecast = {'competition': 'epl', 'match_id': 1, 'arm': arm, 'input_id': 'i1',
                'protocol_id': self.protocol_id, 'source_commit': 'a' * 40,
                'captured_at': '2026-10-07T10:03:10Z'}
            fid = self.append('forecasts', forecast)
            self.arms.append({'arm': arm, 'status': 'fitted', 'forecasts': [
                {'match_id': 1, 'status': 'captured', 'forecast_id': fid}]})
        self.run = {'source_commit': 'a' * 40, 'protocol_id': self.protocol_id,
            'purpose': 'prospective_model_experiment', 'started_at': '2026-10-07T10:03:01Z',
            'completed_at': '2026-10-07T10:03:20Z', 'captured_forecasts': 3, 'captured_matches': 1,
            'competitions': [{'competition': 'epl', 'status': 'completed', 'input_id': 'i1',
                              'coverage': [], 'arms': self.arms}], 'errors': []}
        self.save_capture()
        self.score = {'scored_at': '2026-10-07T10:03:30Z', 'forecast_versions': 30,
            'selected_matches': 1, 'status_counts': {'pending': 2}, 'matches': [
                {'protocol_id': self.protocol_id, 'competition': 'epl', 'match_id': 1,
                 'comparison': comparison, 'status': 'pending'} for comparison in
                ('candidate90_vs_baseline180', 'candidate365_vs_baseline180')],
                'errors': [], 'comparisons': []}
        self.save_score()

    def append(self, collection, value):
        oid = _digest(value)
        self.f.write('shadow_models/' + collection + '/' + oid + '.json', value)
        return oid

    def save_capture(self):
        self.rid = self.append('runs', self.run)
        self.latest = {k: v for k, v in self.run.items() if k != 'competitions'}
        self.latest.update(report_id=self.rid, generated_at=self.run['completed_at'],
            run_started_at=self.run['started_at'], competition_count=1,
            primary_pair_captures_this_run=1, eligibility_rejections=0,
            captured_by_arm={a: 1 for a in ('baseline180', 'candidate90', 'candidate365')})
        self.f.write('shadow_models/reports/latest.json', self.latest)

    def save_score(self):
        self.sid = self.append('score_runs', self.score)
        self.score_latest = {k: v for k, v in self.score.items() if k != 'matches'}
        self.score_latest['report_id'] = self.sid
        self.f.write('shadow_models/reports/score-latest.json', self.score_latest)

    def test_capture_and_scores_have_distinct_version_and_match_denominators(self):
        value = self.f.build()
        capture, scoring = value['shadow_models']['capture'], value['shadow_models']['scoring']
        self.assertEqual(capture['captured_forecast_versions'], 3)
        self.assertEqual(capture['primary_pair_captures_this_run'], 1)
        self.assertEqual(scoring['forecast_versions'], 30)
        self.assertEqual(scoring['independent_matches_across_protocols'], 1)
        self.assertEqual([c['paired_matches_by_protocol'] for c in scoring['comparisons']], [1, 1])
        self.assertFalse(scoring['score_source_commit_provided'])
        self.assertIn('配对版本不等于新增独立比赛', markdown(value))

    def test_shadow_survives_missing_model_report(self):
        (self.f.root / 'reports/latest.json').unlink()
        self.f.outcomes['model'] = 'failure'
        self.assertEqual(self.f.build()['shadow_models']['capture']['status'], 'associated_current_capture')

    def test_current_source_commit_required_for_capture(self):
        self.f.context['source_commit'] = 'b' * 40
        value = self.f.build()['shadow_models']
        self.assertEqual(value['capture']['status'], 'unknown')
        # Score has no source-commit field; association is explicitly time/hash only.
        self.assertEqual(value['scoring']['status'], 'associated_current_score_snapshot')

    def test_restored_old_latest_is_unknown_not_zero(self):
        self.f.context['stage_started_at']['shadow_models'] = '2026-10-07T10:04:00Z'
        value = self.f.build()['shadow_models']
        self.assertEqual(value['capture']['status'], 'unknown')
        self.assertNotIn('captured_forecast_versions', value['capture'])
        self.assertEqual(value['scoring']['status'], 'unknown')

    def test_run_and_score_hash_tampering_both_rejected(self):
        self.f.write('shadow_models/runs/' + self.rid + '.json', {'tampered': True})
        self.f.write('shadow_models/score_runs/' + self.sid + '.json', {'tampered': True})
        value = self.f.build()['shadow_models']
        self.assertEqual(value['capture']['status'], 'unknown')
        self.assertEqual(value['scoring']['status'], 'unknown')

    def test_latest_cannot_change_immutable_counts(self):
        self.latest['primary_pair_captures_this_run'] = 2
        self.f.write('shadow_models/reports/latest.json', self.latest)
        self.assertEqual(self.f.build()['shadow_models']['capture']['status'], 'unknown')

    def test_missing_score_count_is_not_zero(self):
        del self.score['forecast_versions']
        self.save_score()
        scoring = self.f.build()['shadow_models']['scoring']
        self.assertEqual(scoring['status'], 'unknown')
        self.assertNotIn('forecast_versions', scoring)

    def test_repeated_score_comparison_does_not_double_count(self):
        self.score['matches'].append(copy.deepcopy(self.score['matches'][0]))
        self.score['status_counts']['pending'] = 3
        self.save_score()
        self.assertEqual(self.f.build()['shadow_models']['scoring']['status'], 'unknown')

    def test_raw_errors_never_exported(self):
        secret = 'private_uncontrolled_error_payload_123456'
        self.run['errors'] = [secret]
        self.score['errors'] = [secret]
        self.save_capture()
        self.save_score()
        value = self.f.build()
        self.assertNotIn(secret, json.dumps(value) + markdown(value))

    def test_shadow_failure_is_required_only_when_stage_was_marked(self):
        self.f.outcomes['shadow_models'] = 'failure'
        self.assertIn('shadow_models', self.f.build()['necessary_steps_not_successful'])
        del self.f.context['stage_started_at']['shadow_models']
        value = self.f.build()
        self.assertNotIn('shadow_models', value['required_steps'])
        self.assertEqual(value['necessary_steps_outcome'], 'success')

    def test_shadow_stage_marker_is_accepted(self):
        context_path = self.f.root / 'workflow-context.json'
        value = copy.deepcopy(self.f.context)
        del value['stage_started_at']['shadow_models']
        context_path.write_bytes(_canonical(value))
        self.assertEqual(main(['mark', '--context', str(context_path), '--stage', 'shadow_models']), 0)
        self.assertIn('shadow_models', json.loads(context_path.read_text())['stage_started_at'])

    def test_score_before_capture_completion_is_unknown(self):
        self.score['scored_at'] = '2026-10-07T10:03:05Z'
        self.save_score()
        self.assertEqual(self.f.build()['shadow_models']['scoring']['status'], 'unknown')

    def test_capacity_deferral_has_immutable_evidence_and_no_zero_capture_counts(self):
        value = {'status': 'capacity_deferred', 'operation': 'capture', 'source_commit': 'a' * 40,
            'run_started_at': '2026-10-07T10:03:01Z', 'generated_at': '2026-10-07T10:03:20Z',
            'runtime_bytes': 400 * 1024 * 1024, 'limit_bytes': 400 * 1024 * 1024,
            'capacity_report_reserve_bytes': 256 * 1024}
        value['capacity_event_id'] = self.append('capacity_events', value)
        self.f.write('shadow_models/reports/latest.json', value)
        self.f.outcomes['shadow_models'] = 'failure'
        result = self.f.build()
        captured = result['shadow_models']['capture']
        self.assertEqual(captured['status'], 'capacity_deferred')
        self.assertIsNone(captured['captured_forecast_versions'])
        self.assertIn('shadow_models', result['necessary_steps_not_successful'])
        self.assertIn('capacity_deferred', markdown(result))

    def capacity_latest(self, operation='capture'):
        value = {'status': 'capacity_deferred', 'operation': operation, 'source_commit': 'a' * 40,
            'run_started_at': '2026-10-07T10:03:01Z', 'generated_at': '2026-10-07T10:03:20Z',
            'scored_at': '2026-10-07T10:03:20Z', 'runtime_bytes': 499713007,
            'estimated_persisted_bytes': 400 * 1024 * 1024,
            'logical_bytes': 500000000, 'logical_limit_bytes': 2 * 1024 * 1024 * 1024,
            'logical_capture_limit_bytes': 2 * 1024 * 1024 * 1024 - 256 * 1024,
            'limit_bytes': 400 * 1024 * 1024, 'capacity_report_reserve_bytes': 256 * 1024,
            'capacity_event_id': None, 'immutable_event_saved': False,
            'diagnostic_evidence': 'bounded_current_latest_only'}
        filename = 'latest.json' if operation == 'capture' else 'score-latest.json'
        self.f.write('shadow_models/reports/' + filename, value)
        return value

    def test_exhausted_report_reserve_is_current_deferral_with_explicit_weaker_evidence(self):
        self.capacity_latest()
        self.capacity_latest('score')
        self.f.outcomes['shadow_models'] = 'failure'
        summary = self.f.build()['shadow_models']
        for operation in ('capture', 'scoring'):
            value = summary[operation]
            self.assertEqual(value['status'], 'capacity_deferred')
            self.assertEqual(value['diagnostic_evidence'], 'bounded_current_latest_only')
            self.assertFalse(value['immutable_event_saved'])
            self.assertIsNone(value['captured_forecast_versions'])
            self.assertEqual(value['runtime_bytes'], 499713007)
            self.assertEqual(value['estimated_persisted_bytes'], 400 * 1024 * 1024)
            self.assertEqual(value['budget_basis'], 'estimated_persisted_storage_and_original_logical_bytes')

    def test_missing_capacity_event_without_explicit_diagnostic_boundary_remains_unknown(self):
        value = self.capacity_latest()
        del value['diagnostic_evidence']
        self.f.write('shadow_models/reports/latest.json', value)
        self.assertEqual(self.f.build()['shadow_models']['capture']['status'], 'unknown')

    def test_unlinked_capacity_report_still_requires_current_stage_and_commit(self):
        value = self.capacity_latest()
        value['source_commit'] = 'b' * 40
        self.f.write('shadow_models/reports/latest.json', value)
        self.assertEqual(self.f.build()['shadow_models']['capture']['status'], 'unknown')
        value['source_commit'] = 'a' * 40
        value['generated_at'] = '2026-10-07T09:00:00Z'
        self.f.write('shadow_models/reports/latest.json', value)
        self.assertEqual(self.f.build()['shadow_models']['capture']['status'], 'unknown')

    def test_current_capture_exposes_separate_immutable_budget_dimensions(self):
        self.run['storage_budget'] = {'runtime_bytes': 499713007, 'estimated_persisted_bytes': 300000000,
            'limit_bytes': 400 * 1024 * 1024, 'logical_bytes': 500000000,
            'logical_limit_bytes': 2 * 1024 * 1024 * 1024,
            'logical_capture_limit_bytes': 2 * 1024 * 1024 * 1024 - 256 * 1024}
        self.save_capture()
        value = self.f.build()['shadow_models']['capture']
        self.assertEqual(value['storage_budget']['runtime_bytes'], 499713007)
        self.assertEqual(value['storage_budget']['estimated_persisted_bytes'], 300000000)
        self.latest['storage_budget']['estimated_persisted_bytes'] -= 1
        self.f.write('shadow_models/reports/latest.json', self.latest)
        self.assertEqual(self.f.build()['shadow_models']['capture']['status'], 'unknown')


if __name__ == '__main__':
    unittest.main()
