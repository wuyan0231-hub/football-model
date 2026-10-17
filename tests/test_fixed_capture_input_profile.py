import cProfile
import contextlib
import hashlib
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
    def test_saved_identity_is_separate_and_never_relabelled(self):
        self.assertEqual(profile.research_implementation(IMPL, '2'*64), '2'*64)
        self.assertEqual(profile.research_implementation(IMPL), IMPL)

    def test_both_implementation_hashes_must_be_exact(self):
        for execution, saved in ((None, IMPL), ('main', IMPL), (IMPL, 'main'),
                                 (IMPL, 'A'*64), (IMPL, '2'*63)):
            with self.subTest(execution=execution, saved=saved), self.assertRaises(profile.ProfileError):
                profile.research_implementation(execution, saved)

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

    def run_main(self, *, deadline=False, failed=False, execution_implementation=IMPL, saved_implementation=None, expect_freeze=True):
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
                    implementation_fingerprint=lambda: {'sha256': execution_implementation})}
            with patch.dict(sys.modules, modules), patch.object(profile.signal, 'signal', side_effect=set_signal), \
                    patch.object(profile.signal, 'alarm'), contextlib.redirect_stdout(io.StringIO()):
                argv = ['--repo', str(root), '--state-commit', '2' * 40,
                        '--league', 'championship', '--implementation', execution_implementation,
                        '--deadline-seconds', '60', '--output', str(output)]
                if saved_implementation is not None:
                    argv += ['--research-implementation', saved_implementation]
                code = profile.main(argv)
            result = json.loads(output.read_text())
            self.assertTrue(closes)
            self.assertEqual(bool(calls), expect_freeze)
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

    def test_new_execution_reads_old_saved_identity_without_rewriting_it(self):
        code, result = self.run_main(execution_implementation='2'*64, saved_implementation=IMPL)
        self.assertEqual(code, 0)
        self.assertEqual(result['research_implementation'], IMPL)
        self.assertEqual(result['execution_implementation_fingerprint']['sha256'], '2'*64)
        self.assertFalse(result['live_capture_or_first_output_created'])

    def test_new_execution_cannot_claim_old_cards_are_new(self):
        code, result = self.run_main(execution_implementation='2'*64, expect_freeze=False)
        self.assertEqual(code, 1)
        self.assertEqual(result['error_code'], 'implementation_mismatch')

    def test_deadline_keeps_incomplete_profile(self):
        code, result = self.run_main(deadline=True)
        self.assertEqual(code, 1)
        self.assertEqual(result['status'], 'diagnostic_deadline')
        self.assertEqual(result['failed_stage'], 'native_freeze_saved_card_inputs')

    def test_native_failure_redacted(self):
        code, result = self.run_main(failed=True)
        self.assertEqual(code, 1)
        self.assertEqual(result['status'], 'diagnostic_incomplete')


class PrewarmTests(unittest.TestCase):
    def run_warm(self, root, documents, refs, **kwargs):
        reads, tickets = [], []
        def read(path, **_):
            name = path.relative_to(root).as_posix(); reads.append(name)
            return documents[name]
        def refs_in(value):
            if isinstance(value, dict):
                if 'path' in value and 'sha256' in value: yield value
                for item in value.values(): yield from refs_in(item)
            elif isinstance(value, list):
                for item in value: yield from refs_in(item)
        def commit(ticket):
            self.assertEqual(ticket[0].read_bytes(), ticket[1]); tickets.append(ticket)
        guard = types.SimpleNamespace(check_write=lambda p, b: (p, b), commit_write=commit)
        value = profile.prewarm_saved_originals(root, refs, read_bytes=read, refs_in=refs_in,
            select_cold_path=lambda n: bool(profile.re.fullmatch(r'advanced_models/(objects|inputs)/[0-9a-f]{64}\.json', n)),
            guard=guard, progress=lambda *_, **__: None, **kwargs)
        return value, reads, tickets

    def ref(self, raw, name=None):
        digest = hashlib.sha256(raw).hexdigest()
        return {'path': name or 'advanced_models/objects/' + digest + '.json', 'sha256': digest}

    def test_exact_once_with_repeated_edges(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); b = b'{"value": 7}'; leaf = self.ref(b)
            a = json.dumps([leaf, leaf, leaf]).encode(); parent = self.ref(a, 'advanced_models/championship/card.json')
            result, reads, tickets = self.run_warm(root, {parent['path']: a, leaf['path']: b}, [parent, parent])
            self.assertEqual(reads, [parent['path'], leaf['path']])
            self.assertEqual(result['edges'], 5)
            self.assertEqual(result['warmed_originals'], 1)
            self.assertEqual(len(tickets), 1)
            self.assertEqual((root / leaf['path']).read_bytes(), b)
            self.assertFalse((root / parent['path']).exists())
            self.assertTrue(result['native_freeze_still_required'])

    def test_existing_original_not_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); b = b'{}'; ref = self.ref(b); path = root / ref['path']
            path.parent.mkdir(parents=True); path.write_bytes(b)
            result, _, tickets = self.run_warm(root, {ref['path']: b}, [ref])
            self.assertEqual(result['warmed_originals'], 0); self.assertEqual(tickets, [])
            self.assertEqual(path.read_bytes(), b)

    def test_repeated_frontier_is_deduplicated_before_enqueue(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); b = b'{}'; leaf = self.ref(b)
            a = json.dumps([leaf] * 10000).encode(); parent = self.ref(a, 'card.json')
            result, reads, _ = self.run_warm(root, {parent['path']: a, leaf['path']: b}, [parent], max_edges=2)
            self.assertEqual(result['edges'], 10001)
            self.assertEqual(result['scheduled_bindings'], 2)
            self.assertEqual(result['deduplicated_occurrences'], 9999)
            self.assertEqual(result['peak_pending'], 1)
            self.assertEqual(reads, [parent['path'], leaf['path']])

    def test_pending_conflict_is_checked_even_before_read(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); b = b'{}'; leaf = self.ref(b)
            a = json.dumps([leaf, {**leaf, 'sha256': '1' * 64}]).encode()
            parent = self.ref(a, 'card.json')
            with self.assertRaisesRegex(profile.ProfileError, 'version_conflict'):
                self.run_warm(root, {parent['path']: a, leaf['path']: b}, [parent])
            self.assertFalse((root / leaf['path']).exists())

    def test_occurrences_remain_finitely_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); b = b'{}'; ref = self.ref(b)
            with self.assertRaisesRegex(profile.ProfileError, 'occurrence_limit'):
                self.run_warm(root, {ref['path']: b}, [ref, ref], max_occurrences=1)

    def test_nonimmutable_paths_read_but_never_written(self):
        for name in ('cache/api_cache/a.json', 'advanced_models/inputs/latest.json',
                     'advanced_snapshots/first.json', 'advanced_models/championship/reports/latest.json'):
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory); b = b'{}'; ref = self.ref(b, name)
                result, _, tickets = self.run_warm(root, {name: b}, [ref])
                self.assertEqual(result['warmed_originals'], 0); self.assertEqual(tickets, [])
                self.assertFalse((root / name).exists())

    def test_hash_mismatch_never_written(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); ref = self.ref(b'{}')
            with self.assertRaisesRegex(profile.ProfileError, 'hash_mismatch'):
                self.run_warm(root, {ref['path']: b'{"other":1}'}, [ref])
            self.assertFalse((root / ref['path']).exists())

    def test_conflicting_repeated_reference_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); b = b'{}'; ref = self.ref(b)
            other = {**ref, 'sha256': '1' * 64}
            with self.assertRaisesRegex(profile.ProfileError, 'version_conflict'):
                self.run_warm(root, {ref['path']: b}, [other, ref])

    def test_unsafe_paths_rejected(self):
        for name in ('/absolute', '../escape', './x', 'a//b', 'a/../b', 'a\\b', 'a\nx', 'a\x7fx'):
            with tempfile.TemporaryDirectory() as directory:
                with self.assertRaisesRegex(profile.ProfileError, 'invalid_reference_path'):
                    self.run_warm(Path(directory), {}, [self.ref(b'{}', name)])

    def test_symlink_parent_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root / 'advanced_models').symlink_to(root)
            ref = self.ref(b'{}')
            with self.assertRaisesRegex(profile.ProfileError, 'symlink'):
                self.run_warm(root, {ref['path']: b'{}'}, [ref])

    def test_byte_and_node_limits(self):
        for kwargs, error in (({'max_warm_bytes': 1}, 'byte_limit'), ({'max_nodes': 0}, 'node_limit'),
                              ({'max_edges': 0}, 'edge_limit')):
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory); ref = self.ref(b'{}')
                with self.assertRaisesRegex(profile.ProfileError, error):
                    self.run_warm(root, {ref['path']: b'{}'}, [ref], **kwargs)
                self.assertFalse((root / ref['path']).exists())

    def test_read_budget_applies_to_hot_inputs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); b = b'{"large": "1234567890"}'; ref = self.ref(b, 'hot.json')
            with self.assertRaisesRegex(profile.ProfileError, 'read_byte_limit'):
                self.run_warm(root, {ref['path']: b}, [ref], max_warm_bytes=1)

    def test_nonfinite_and_duplicate_json_rejected(self):
        for b, error in ((b'{"x":NaN}', 'nonfinite_json'), (b'{"x":1,"x":2}', 'duplicate_json_key')):
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory); ref = self.ref(b, 'hot.json')
                with self.assertRaisesRegex(profile.ProfileError, error):
                    self.run_warm(root, {ref['path']: b}, [ref])

    def test_bad_digest_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            ref = {'path': 'hot.json', 'sha256': 'bad'}
            with self.assertRaisesRegex(profile.ProfileError, 'invalid_digest'):
                self.run_warm(Path(directory), {}, [ref])


if __name__ == '__main__':
    unittest.main()

