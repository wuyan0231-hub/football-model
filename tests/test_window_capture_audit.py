"""Input and evidence binding regressions; no network, fitting or state write."""
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parents[1]
ROOT = Path(os.environ.get('WINDOW_TEST_REPO', '/workspace/scratch/60512f4bf8b4/football-model'))
if (HERE / 'football_model').is_dir():
    ROOT = HERE
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location('window_capture_audit', HERE / 'scripts/window_capture_audit.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class InputTests(unittest.TestCase):
    def test_scheduled_dispatch_has_real_exact_date_and_open_window(self):
        at = lambda text: datetime.fromisoformat(text)
        self.assertTrue(module.dispatch_enabled('schedule', at('2026-10-17T10:00:00+00:00')))
        self.assertTrue(module.dispatch_enabled('schedule', at('2026-10-17T19:29:59+08:00')))
        for text in ('2026-10-17T09:59:59+00:00', '2026-10-17T11:30:00+00:00',
                     '2026-10-18T10:00:00+00:00', '2027-10-17T10:00:00+00:00'):
            with self.subTest(text=text):
                self.assertFalse(module.dispatch_enabled('schedule', at(text)))
        self.assertTrue(module.dispatch_enabled('workflow_dispatch', at('2027-10-17T10:00:00+00:00')))
        with self.assertRaises(ValueError):
            module.dispatch_enabled('schedule', datetime(2026, 10, 17, 10))
        with self.assertRaises(ValueError):
            module.dispatch_enabled('push', at('2026-10-17T10:00:00+00:00'))

    def test_exact_single_league_main_and_positive_target(self):
        self.assertEqual(module.validate_inputs('epl', '1557430', 'refs/heads/main'), 1557430)

    def test_not_all_leagues_not_arbitrary_code(self):
        for league in ('all', 'ucl', 'epl,unl', 'epl;echo x', '../epl', 'epl\n', '', None):
            with self.subTest(league=league), self.assertRaises(ValueError):
                module.validate_inputs(league, '1557430', 'refs/heads/main')

    def test_invalid_target_and_non_main_rejected(self):
        for mid in ('0', '-1', '01', '1.0', '1\n', '', '$(x)', '1;echo x', '1' * 13, None):
            with self.subTest(mid=mid), self.assertRaises(ValueError):
                module.validate_inputs('epl', mid, 'refs/heads/main')
        with self.assertRaises(ValueError):
            module.validate_inputs('epl', '1557430', 'refs/heads/other')

    def test_safe_references_before_fetch(self):
        valid = {'path': 'advanced_snapshots/a/receipts/a.json', 'sha256': 'a' * 64}
        self.assertEqual(module.validate_ref(valid), valid)
        for name in ('../a', 'a/../b', '/a', 'a//b', 'a/./b', 'a\\b', 'a\nb', ''):
            with self.subTest(name=name), self.assertRaises(ValueError):
                module.validate_ref({**valid, 'path': name})
        for sha in ('a' * 63, 'a' * 65, 'A' * 64, 'g' * 64, 'main'):
            with self.subTest(sha=sha), self.assertRaises(ValueError):
                module.validate_ref({**valid, 'sha256': sha})
        with self.assertRaises(ValueError):
            module.validate_ref({**valid, 'extra': 1})


class ReceiptTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name)
        self.runtime = self.repo / 'runtime'
        self.runtime.mkdir()
        self.output = self.repo / 'receipt.json'
        self.ghout = self.repo / 'github-output'
        self.source = 'a' * 40
        self.parent = 'b' * 40
        self.saved_state = 'c' * 40
        (self.repo / 'advanced-models-v1.json').write_bytes((ROOT / 'advanced-models-v1.json').read_bytes())
        from football_model.feature_research import digest
        self.root = self.runtime / 'advanced_snapshots' / digest(json.loads((ROOT / 'advanced-models-v1.json').read_text()))
        self.report_path = self.root / 'reports/epl/latest.json'
        self.report_path.parent.mkdir(parents=True)
        self.capture_ref = {'path': 'advanced_snapshots/p/receipts/' + 'd' * 64 + '.json', 'sha256': 'd' * 64}
        self.first = self.root / 'captures/epl/1557430/LINEUP/first.json'
        self.first.parent.mkdir(parents=True)
        self.first.write_text(json.dumps(self.capture_ref))
        self.report = {'source_commit': self.source, 'state_commit': self.parent, 'league': 'epl',
            'queue': [{'match_id': 1557430, 'horizon': 'LINEUP', 'status': 'captured', 'receipt_ref': self.capture_ref}]}
        self.report_path.write_text(json.dumps(self.report))
        self.save = {'source_sha': self.source, 'github_run_id': '1', 'github_run_attempt': '1', 'child_exit_code': 0,
            'save': {'status': 'saved_and_marker_verified', 'reported_commit': self.saved_state}}
        self.save_path = self.repo / 'window-save-run-receipt.json'
        self.save_path.write_text(json.dumps(self.save))
        self.env = {'GITHUB_RUN_ID': '1', 'GITHUB_RUN_ATTEMPT': '1', 'GITHUB_OUTPUT': str(self.ghout),
            'WINDOW_PARENT_STATE': self.parent, 'RESTORE': 'success', 'FACTS': 'success', 'CAPTURE': 'success',
            'CHECK': 'success', 'PERSIST': 'success'}

    def record(self):
        with patch.dict(os.environ, self.env), patch.object(module.subprocess, 'check_output', return_value=self.source + '\n'), \
                patch.object(module, 'check_implementation', return_value={'sha256': module.EXPECTED_IMPLEMENTATION, 'files': ['x'] * 50}):
            self.assertEqual(module.record(self.repo, self.runtime, 'epl', 1557430, self.output), 0)
        return json.loads(self.output.read_text())

    def test_report_save_and_first_bound_separately(self):
        result = self.record()
        self.assertEqual(result['saved_state_commit'], self.saved_state)
        self.assertEqual(result['capture_ref'], self.capture_ref)
        self.assertTrue(result['is_original_first_output'])
        self.assertEqual(result['remote_durability'], 'not_yet_independently_proven')
        self.assertTrue(result['not_all_league_final_followup'])
        self.assertFalse(result['confirmation_activated_here'])

    def test_save_source_run_and_exit_must_match(self):
        for changed in ({'source_sha': 'f' * 40}, {'github_run_id': '2'}, {'github_run_attempt': '2'}, {'child_exit_code': 1}):
            with self.subTest(changed=changed):
                self.save_path.write_text(json.dumps({**self.save, **changed}))
                result = self.record()
                self.assertIsNone(result['saved_state_commit'])

    def test_capture_wrong_parent_or_source_is_not_current(self):
        for changed in ({'source_commit': 'f' * 40}, {'state_commit': 'f' * 40}, {'league': 'unl'}):
            with self.subTest(changed=changed):
                self.report_path.write_text(json.dumps({**self.report, **changed}))
                result = self.record()
                self.assertIsNone(result['capture_ref'])
                self.assertEqual(result['target_status'], 'not_proven_captured')

    def test_missing_lineup_late_or_not_due_cannot_claim_capture(self):
        for status in ('late', 'ineligible', 'not_due', 'unsupported', 'capacity_deferred'):
            with self.subTest(status=status):
                report = {**self.report, 'queue': [{**self.report['queue'][0], 'status': status}]}
                self.report_path.write_text(json.dumps(report))
                result = self.record()
                self.assertEqual(result['target_status'], status)
                self.assertIsNone(result['capture_ref'])

    def test_skipped_capture_never_reuses_latest(self):
        self.env['CAPTURE'] = 'skipped'
        result = self.record()
        self.assertIsNone(result['capture_ref'])

    def test_previous_first_output_remains_distinct(self):
        self.first.write_text(json.dumps({**self.capture_ref, 'sha256': 'e' * 64}))
        result = self.record()
        self.assertFalse(result['is_original_first_output'])
        self.assertEqual(result['capture_ref'], self.capture_ref)

    def test_missing_first_output_is_not_fabricated(self):
        self.first.unlink()
        result = self.record()
        self.assertIsNone(result['first_ref'])


class WorkflowTests(unittest.TestCase):
    def test_bounded_writer_then_separate_readonly_service_verification(self):
        text = (HERE / '.github/workflows/window-capture.yml').read_text()
        self.assertNotIn('  schedule:', text)
        self.assertNotIn("- cron: '0 10 17 10 *'", text)
        self.assertIn("needs.validate.outputs.proceed == 'true'", text)
        self.assertNotIn('push:', text)
        self.assertIn('group: football-model-state\n      cancel-in-progress: false\n      queue: max', text)
        self.assertNotIn('--league all', text)
        self.assertNotIn('advanced_scoring', text)
        self.assertNotIn('advanced_confirmation', text)
        self.assertNotIn('advanced_research study', text)
        self.assertIn('needs: capture', text)
        self.assertLess(text.index('scripts/cloud_state_save_once.py'), text.index('fixed_readback:'))
        self.assertIn('FOOTBALL_API_KEY: \'\'', text)
        self.assertIn('steps.receipt.outputs.capture_ref == \'null\'', text)
        self.assertIn('--source-commit "$GITHUB_SHA" --state-commit "$WINDOW_PARENT_STATE"', text)
        self.assertIn('--match-id "$WINDOW_MATCH_ID"', text)
        self.assertNotIn('--as-of', text)

    def test_reviewed_live_execution_identity_is_exact(self):
        result = module.check_implementation()
        self.assertEqual(result['sha256'], module.EXPECTED_IMPLEMENTATION)
        self.assertEqual(len(result['files']), 50)

    def test_old_saved_research_identity_is_not_a_live_execution_alias(self):
        from football_model.advanced_research import implementation_fingerprint
        stale = {**implementation_fingerprint(),
                 'sha256': '1e311689b3405f64c6ad9636732d2defb7ad67e26b4bc7e04b01a5b1b2b2d6ac'}
        with patch('football_model.advanced_research.implementation_fingerprint', return_value=stale):
            with self.assertRaisesRegex(ValueError, 'research_implementation_changed'):
                module.check_implementation()


class NativeServiceBindingTests(unittest.TestCase):
    def service(self, *, capture_status='completed', marker=None):
        from football_model import advanced_durability as native
        state, source = 'a' * 40, 'b' * 40
        meta = {'github_run_id': '123', 'github_run_attempt': '1', 'source_sha': source}
        def api(url, token):
            if '/jobs?' in url:
                return {'total_count': 3, 'jobs': [
                    {'id': 100, 'run_id': 123, 'run_attempt': 1, 'name': 'validate',
                     'status': 'completed', 'completed_at': '2026-10-17T10:00:10Z'},
                    {'id': 101, 'run_id': 123, 'run_attempt': 1, 'name': 'capture',
                     'status': capture_status, 'completed_at': '2026-10-17T10:35:00Z' if capture_status == 'completed' else None},
                    {'id': 102, 'run_id': 123, 'run_attempt': 1, 'name': 'fixed_readback',
                     'status': 'in_progress', 'completed_at': None}]}, 'c' * 64
            self.assertIn('/attempts/1', url)
            return {'id': 123, 'run_attempt': 1, 'head_sha': source, 'status': 'in_progress'}, 'd' * 64
        def log(url, token):
            if '/101/' in url:
                return ("2026-10-17T10:34:00Z ##[group]Run python scripts/cloud_state_save_once.py\n"
                    '2026-10-17T10:34:55Z MODEL_STATE_SAVED_COMMIT=' + (marker or state) + '\n').encode()
            return b'2026-10-17T10:00:10Z Single-league live-window dispatch gate: enabled\n'
        with patch.dict(os.environ, {'GITHUB_REPOSITORY': 'owner/repo', 'GITHUB_TOKEN': 'synthetic-service-test-only'}), \
                patch.object(native, '_api_json', side_effect=api), patch.object(native, '_job_log', side_effect=log):
            return native._remote_completion(meta, state)

    def test_completed_capture_job_exact_save_once_marker_is_accepted_during_same_run_readback(self):
        result = self.service()
        self.assertEqual(result['job_id'], '101')
        self.assertEqual(result['job_name'], 'capture')
        self.assertEqual(result['observed_completion_upper_bound'], '2026-10-17T10:35:00Z')
        self.assertEqual(result['saved_commit_marker'], 'MODEL_STATE_SAVED_COMMIT=' + 'a' * 40)

    def test_running_capture_or_different_commit_marker_never_qualifies(self):
        self.assertIsNone(self.service(capture_status='in_progress'))
        self.assertIsNone(self.service(marker='e' * 40))


if __name__ == '__main__':
    unittest.main()
