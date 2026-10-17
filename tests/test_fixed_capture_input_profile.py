import cProfile
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'scripts/fixed_capture_input_profile.py'
if not SOURCE.exists():
    SOURCE = Path(__file__).with_name('fixed_capture_input_profile.py')
spec = importlib.util.spec_from_file_location('fixed_capture_input_profile', SOURCE)
profile = importlib.util.module_from_spec(spec)
spec.loader.exec_module(profile)
IMPL = '1' * 64


def summary():
    return {'league': 'championship', 'source_commit': '3' * 40,
            'implementation_fingerprint': {'sha256': IMPL, 'files': ['x'] * 50},
            'model_cards': {'G0': {'path': 'advanced_models/championship/h/models/G0/history/a.json',
                                   'sha256': 'a' * 64}}}


class ProfileTests(unittest.TestCase):
    def test_selected_latest_refs(self):
        self.assertEqual(len(profile.select_references(summary(), 'championship', IMPL)), 1)

    def test_implementation_identity(self):
        with self.assertRaises(profile.ProfileError):
            profile.select_references(summary(), 'championship', '2' * 64)

    def test_league_identity(self):
        with self.assertRaises(profile.ProfileError):
            profile.select_references(summary(), 'epl', IMPL)

    def test_bad_reference(self):
        for path in ('/escape', 'advanced_models/championship/../x',
                     'advanced_models/championship/x\nx', 'advanced_models/epl/x'):
            value = summary(); value['model_cards']['G0']['path'] = path
            with self.assertRaises(profile.ProfileError):
                profile.select_references(value, 'championship', IMPL)

    def test_card_limit_and_digest(self):
        for value in ({}, {str(i): {} for i in range(18)}):
            doc = summary(); doc['model_cards'] = value
            with self.assertRaises(profile.ProfileError):
                profile.select_references(doc, 'championship', IMPL)
        doc = summary(); doc['model_cards']['G0']['sha256'] = 'bad'
        with self.assertRaises(profile.ProfileError):
            profile.select_references(doc, 'championship', IMPL)

    def test_new_output_only(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); target = root / 'new.json'
            self.assertEqual(profile.validate_output(target, root), target)
            target.write_text('original')
            with self.assertRaises(profile.ProfileError):
                profile.validate_output(target, root)
            self.assertEqual(target.read_text(), 'original')

    def test_protected_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ('runtime/x.json', 'scripts/x.json', '../outside.json', 'x.py'):
                with self.assertRaises(profile.ProfileError):
                    profile.validate_output(root / name, root)

    def test_symlink_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); link = root / 'link.json'
            link.symlink_to(root / 'not-yet-created')
            with self.assertRaises(profile.ProfileError):
                profile.validate_output(link, root)

    def test_stack_redacted(self):
        sensitive_value = 'DO_NOT_EXPORT_LOCAL_VALUE'
        frames = profile.safe_stack(sys._getframe(), 3)
        self.assertLessEqual(len(frames), 3)
        self.assertNotIn(sensitive_value, json.dumps(frames))
        self.assertTrue(all(set(frame) == {'file', 'function', 'line'} for frame in frames))
        self.assertTrue(all('/' not in frame['file'] for frame in frames))

    def test_profile_rows_safe_and_bounded(self):
        profiler = cProfile.Profile(); profiler.enable()
        profile.safe_stack(sys._getframe())
        profiler.disable()
        rows = profile.profile_rows(profiler, 2)
        self.assertLessEqual(len(rows), 2)
        self.assertTrue(all('/' not in r['file'] and 'locals' not in r for r in rows))

    def run_main(self, *, deadline=False, failed=False):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); output = root / 'profile.json'
            status = {}; closes = []; calls = []
            def sha40(value):
                if len(value) != 40: raise ValueError('bad')
                return value
            def restore(repo, runtime, state, cloud):
                path = runtime / 'advanced_models/championship/p/reports/latest.json'
                path.parent.mkdir(parents=True); path.write_text(json.dumps(summary()))
                return {'test_only': True}
            def freeze(runtime, destination, refs, cutoff, *, capacity_guard):
                calls.append((runtime, destination, refs))
                self.assertEqual(destination.name, 'diagnostic-profile-inputs')
                self.assertFalse((runtime / 'advanced_snapshots').exists())
                if deadline:
                    status['handler'](0, None)
                if failed:
                    raise ValueError('sensitive free exception text')
                return [{'origin_runtime_path': 'saved/input.json', 'sha256': 'c' * 64}]
            def set_signal(_number, handler):
                previous = status.get('handler'); status['handler'] = handler
                return previous
            modules = {
                'fixed_fixture_validation_audit': types.SimpleNamespace(restore_fixed=restore, sha40=sha40),
                'fixed_research_report_audit': types.SimpleNamespace(
                    checkpoint_output=lambda path, obj: path.write_text(json.dumps(obj))),
                'cloud_state': types.SimpleNamespace(_git=lambda *_: types.SimpleNamespace(stdout='4' * 40)),
                'football_model.history_archive': types.SimpleNamespace(
                    read_bytes=lambda path, **_: path.read_bytes(), _close_readers=lambda: closes.append(True)),
                'football_model.advanced_snapshots': types.SimpleNamespace(freeze_inputs=freeze),
                'football_model.advanced_capacity': types.SimpleNamespace(CapacityGuard=lambda runtime: object()),
                'football_model.advanced_research': types.SimpleNamespace(
                    implementation_fingerprint=lambda: {'sha256': IMPL})}
            with patch.dict(sys.modules, modules), patch.object(profile.signal, 'signal', side_effect=set_signal), \
                    patch.object(profile.signal, 'alarm'), contextlib.redirect_stdout(io.StringIO()):
                code = profile.main(['--repo', str(root), '--state-commit', '2' * 40,
                                     '--league', 'championship', '--implementation', IMPL,
                                     '--deadline-seconds', '60', '--output', str(output)])
            result = json.loads(output.read_text())
            self.assertTrue(closes and calls)
            self.assertEqual(result['network_football_requests'], 0)
            self.assertEqual(result['fits'], 0)
            self.assertEqual(result['remote_state_writes'], 0)
            self.assertFalse(result['live_capture_or_first_output_created'])
            self.assertNotIn('sensitive free exception text', json.dumps(result))
            return code, result

    def test_end_to_end_diagnostic_not_capture(self):
        code, result = self.run_main()
        self.assertEqual(code, 0)
        self.assertEqual(result['frozen_originals'], 1)

    def test_deadline_keeps_incomplete_profile(self):
        code, result = self.run_main(deadline=True)
        self.assertEqual(code, 1)
        self.assertEqual(result['status'], 'diagnostic_deadline')
        self.assertEqual(result['failed_stage'], 'native_freeze_saved_card_inputs')

    def test_native_failure_redacted(self):
        code, result = self.run_main(failed=True)
        self.assertEqual(code, 1)
        self.assertEqual(result['status'], 'diagnostic_incomplete')


if __name__ == '__main__':
    unittest.main()
