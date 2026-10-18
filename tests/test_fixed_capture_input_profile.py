import cProfile
from collections import OrderedDict
import contextlib
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import subprocess
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
checkpoint_spec = importlib.util.spec_from_file_location('profile_output_audit', SOURCE.with_name('fixed_research_report_audit.py'))
checkpoint_audit = importlib.util.module_from_spec(checkpoint_spec)
checkpoint_spec.loader.exec_module(checkpoint_audit)
IMPL = '1' * 64


class ReaderObservationTests(unittest.TestCase):
    def reader_type(self):
        class Reader:
            def __init__(self):
                self.cache_bytes = 12
                self._cache = OrderedDict()
                self._cached_bytes = 0
                self._process = object()
                self.calls = 0
                self.failure = ValueError('same-native-exception')
            def _blob(self, commit, relative, *, missing=False):
                self.calls += 1
                if relative == 'raises': raise self.failure
                key = (commit, relative)
                if key in self._cache:
                    self._cache.move_to_end(key); return self._cache[key]
                if relative == 'absent': return None
                raw = b'MANIFEST' if relative == 'state_storage.json' else b'raw'
                while self._cache and self._cached_bytes + len(raw) > self.cache_bytes:
                    _, previous = self._cache.popitem(last=False); self._cached_bytes -= len(previous)
                self._cache[key] = raw; self._cached_bytes += len(raw)
                return raw
            def close(self):
                self._cache.clear(); self._cached_bytes = 0; self._process = None
        return Reader

    def test_native_return_and_exception_identity_preserved_once_per_call(self):
        kind = self.reader_type(); reader = kind()
        original_blob, original_close = kind._blob, kind.close
        observation = profile.ReaderCacheObservation(kind); observation.start()
        try:
            value = reader._blob('c', 'state_archives/value')
            self.assertIs(reader._blob('c', 'state_archives/value'), value)
            self.assertIsNone(reader._blob('c', 'absent', missing=True))
            with self.assertRaises(ValueError) as raised: reader._blob('c', 'raises')
            self.assertIs(raised.exception, reader.failure)
            self.assertEqual(reader.calls, 4)
            result = observation.snapshot(trace=True)
            self.assertEqual(result['calls'], 4)
            self.assertEqual(result['exceptions'], 1)
            self.assertEqual(result['missing_returns'], 1)
            self.assertEqual(result['reader_summaries'][0]['actual_vs_original_hit_mismatches'], 0)
            self.assertNotIn('state_archives/value', json.dumps(result))
            self.assertNotIn('MANIFEST', json.dumps(result))
        finally: observation.stop()
        self.assertIs(kind._blob, original_blob); self.assertIs(kind.close, original_close)

    def test_seeded_lru_and_reset_match_native_and_alternative_is_simulation(self):
        kind = self.reader_type(); reader = kind()
        reader._blob('seed', 'state_archives/a')
        observation = profile.ReaderCacheObservation(kind); observation.start()
        try:
            reader._blob('seed', 'state_archives/a')
            for commit in ('one', 'two', 'three', 'four'):
                reader._blob(commit, 'state_storage.json')
                for key in ('a', 'b', 'c'): reader._blob('seed', 'state_archives/' + key)
            reader.close()
            reader._blob('seed', 'state_archives/a')
            result = observation.snapshot(trace=True)
            summary = result['reader_summaries'][0]
            self.assertEqual(summary['actual_vs_original_hit_mismatches'], 0)
            self.assertGreater(summary['skip_manifest_transport_hits'], summary['original_transport_hits'])
            self.assertEqual(len(result['initial_cache_key_indices']), 1)
            self.assertEqual(summary['close_calls'], 1)
            self.assertEqual(len(result['resets']), 1)
            self.assertIn('simulation only', result['replay_scope'])
        finally: observation.stop()

    def test_trace_limit_never_changes_native_execution_or_budget(self):
        kind = self.reader_type(); reader = kind()
        observation = profile.ReaderCacheObservation(kind)
        observation.MAX_KEYS = 2; observation.MAX_EVENTS = 3
        observation.start()
        try:
            for index in range(8): self.assertEqual(reader._blob('c', 'state_archives/' + str(index)), b'raw')
            result = observation.snapshot(trace=True)
            self.assertFalse(result['trace_complete'])
            self.assertEqual(result['trace_truncation'], 'key_limit')
            self.assertLessEqual(result['traced_keys'], 2)
            self.assertLessEqual(result['traced_events'] + result['traced_resets'], 3)
            self.assertEqual(result['calls'], 8)
            self.assertEqual(reader.calls, 8)
            self.assertEqual(reader.cache_bytes, 12)
            self.assertLessEqual(reader._cached_bytes, 12)
        finally: observation.stop()

    def test_budget_transitions_evict_both_replays_to_the_native_capacity(self):
        kind = self.reader_type(); reader = kind()
        reader.total_cache_bytes = 12
        reader.member_cache_budget_bytes = 0
        reader.cache_budget_generation = 0
        for name in 'abcd': reader._blob('c', 'state_archives/' + name)
        observation = profile.ReaderCacheObservation(kind, native_policy='original'); observation.start()
        def reserve(blob_budget):
            while reader._cache and reader._cached_bytes > blob_budget:
                _, raw = reader._cache.popitem(last=False); reader._cached_bytes -= len(raw)
            reader.cache_bytes = blob_budget
            reader.member_cache_budget_bytes = reader.total_cache_bytes - blob_budget
            reader.cache_budget_generation += 1
        try:
            reader._blob('c', 'state_archives/a')
            reserve(6)
            self.assertEqual(reader._blob('c', 'state_archives/a'), b'raw')
            self.assertEqual(reader._blob('c', 'state_archives/b'), b'raw')
            reserve(12)
            self.assertEqual(reader._blob('c', 'state_archives/b'), b'raw')
            result = observation.snapshot(trace=True)
            summary = result['reader_summaries'][0]
            self.assertEqual(summary['budget_changes'], 2)
            self.assertEqual(summary['actual_vs_original_hit_mismatches'], 0)
            self.assertEqual(summary['actual_vs_skip_manifest_hit_mismatches'], 0)
            self.assertEqual(result['budget_events'], [[1, 0, 1, 6, 12, 6], [3, 0, 2, 12, 12, 0]])
            self.assertEqual(reader.calls, 8)
            self.assertEqual(reader.cache_bytes, 12)
        finally: observation.stop()

    def test_budget_events_share_the_original_trace_bound(self):
        kind = self.reader_type(); reader = kind()
        reader.total_cache_bytes = 12; reader.member_cache_budget_bytes = 0; reader.cache_budget_generation = 0
        observation = profile.ReaderCacheObservation(kind, native_policy='original')
        observation.MAX_EVENTS = 2; observation.start()
        try:
            reader._blob('c', 'state_archives/a')
            for generation in range(1, 20):
                reader.cache_budget_generation = generation
                self.assertEqual(reader._blob('c', 'state_archives/a'), b'raw')
            result = observation.snapshot(trace=True)
            self.assertFalse(result['trace_complete'])
            self.assertEqual(result['trace_truncation'], 'event_limit')
            self.assertLessEqual(result['traced_events'] + result['traced_resets'] + result['traced_budget_changes'], 2)
            self.assertEqual(result['reader_summaries'][0]['budget_changes'], 19)
            self.assertEqual(reader.calls, 20)
        finally: observation.stop()

    def test_native_metrics_are_read_outside_observation_lock_and_only_numeric_fields(self):
        kind = self.reader_type(); reader = kind()
        observation = profile.ReaderCacheObservation(kind)
        def metrics():
            self.assertFalse(observation.lock._is_owned())
            return {'current_bytes': 12, 'peak_bytes': 24, 'hits': 9, 'budget_bytes': 36,
                    'total_cache_bytes': 64, 'secret_provider_data': b'not-exported'}
        reader.member_cache_metrics = metrics
        observation.start()
        try:
            reader._blob('c', 'state_archives/a')
            snapshot = observation.snapshot(trace=True)
            self.assertEqual(snapshot['reader_summaries'][0]['member_cache']['peak_bytes'], 24)
            self.assertNotIn('secret_provider_data', json.dumps(snapshot))
            self.assertTrue(any('profile_member_cache' in line for line in observation.minute_lines(snapshot)))
            reader.close()
            snapshot = observation.snapshot()
            self.assertEqual(snapshot['reader_summaries'][0]['member_cache_before_last_close']['hits'], 9)
        finally: observation.stop()

    def test_candidate_real_git_native_matches_skip_policy_not_prior_baseline(self):
        from football_model import history_archive as history
        from football_model.state_storage import pack_runtime
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); repo = root / 'repo'; repo.mkdir()
            def git(*args):
                return subprocess.check_output(['git', '-C', str(repo), *args], stderr=subprocess.DEVNULL)
            git('init', '-q'); git('config', 'user.email', 'test@example.invalid'); git('config', 'user.name', 'Test')
            source = root / 'source'
            path = source / ('advanced_models/inputs/' + 'a' * 64 + '.json')
            path.parent.mkdir(parents=True); path.write_bytes(b'{"immutable":1}\n')
            pack_runtime(source, repo); git('add', '.'); git('commit', '-qm', 'fixed transport')
            commit = git('rev-parse', 'HEAD').decode().strip()
            manifest = (repo / 'state_storage.json').read_bytes()
            transport = 'state_archives/' + json.loads(manifest)['entries'][0]['blob_sha256'] + '.json.gz'
            with history.FixedStateReader(repo, commit, cache_bytes=len(manifest)) as reader:
                observation = profile.ReaderCacheObservation(history.FixedStateReader, native_policy='skip_manifest')
                observation.start()
                try:
                    first = reader._blob(commit, transport)
                    for _ in range(2):
                        self.assertEqual(reader._blob(commit, 'state_storage.json'), manifest)
                        self.assertIs(reader._blob(commit, transport), first)
                    result = observation.snapshot(trace=True)
                    summary = result['reader_summaries'][0]
                    self.assertEqual(summary['actual_vs_skip_manifest_hit_mismatches'], 0)
                    self.assertGreater(summary['actual_vs_original_hit_mismatches'], 0)
                    self.assertEqual(result['native_replay_match_counter'], 'actual_vs_skip_manifest_hit_mismatches')
                    self.assertEqual(result['policy_labels']['original'], 'prior_cache_all_baseline')
                finally: observation.stop()

    def test_minute_log_rows_are_short_and_preserve_full_checkpoint_snapshot(self):
        kind = self.reader_type(); reader = kind()
        observation = profile.ReaderCacheObservation(kind, native_policy='original'); observation.start()
        try:
            reader._blob('c', 'state_storage.json'); reader._blob('c', 'state_archives/private')
            snapshot = observation.snapshot(trace=True)
            lines = observation.minute_lines(snapshot)
            self.assertEqual(len(lines), 5)
            self.assertLess(len(lines[0]), 400)
            for line in lines[1:4]: self.assertLess(len(line), 350)
            self.assertLess(len(lines[4]), 800)
            self.assertIn('prior_all_baseline', lines[4])
            self.assertIn('skip_manifest', lines[4])
            self.assertIn('keys', snapshot)
            self.assertNotIn('state_archives/private', '\n'.join(lines))
            observation.categories['manifest']['returned_bytes'] = 10 ** 900
            bounded = observation.minute_lines()
            self.assertTrue(all(len(line) < 800 for line in bounded))
            self.assertTrue(any('profile_cache_log_bound' in line for line in bounded))
            self.assertEqual(observation.snapshot()['categories']['manifest']['returned_bytes'], 10 ** 900)
        finally: observation.stop()


class OutputBudgetTests(unittest.TestCase):
    def payload(self, observation):
        frame = {'file': 'bounded_code_location_' + 'x' * 80 + '.py',
                 'function': 'bounded_function_' + 'x' * 80, 'line': 99999}
        return {'status': 'diagnostic_deadline', 'exception_class': 'StorageError',
            'error_code': 'native_diagnostic_failure', 'failed_stage': 'native_freeze_saved_card_inputs',
            'frozen_originals': 5401, 'frozen_inventory_sha256': 'a' * 64,
            'state_commit': 'b' * 40, 'diagnostic_source_commit': 'c' * 40,
            'summary_sha256': 'd' * 64, 'research_implementation': 'e' * 64,
            'execution_implementation_fingerprint': {'sha256': IMPL,
                'files': {'football_model/' + str(i) + '_' + 'x' * 100 + '.py': 'f' * 64 for i in range(50)}},
            'stack_samples': [{'elapsed_seconds': 1500.0, 'stage': 'native_freeze_saved_card_inputs',
                'frames': [dict(frame) for _ in range(12)]} for _ in range(30)],
            'profile_functions': [{**frame, 'calls': 999999999, 'recursive_calls': 999999999,
                'own_seconds': 1500.0, 'cumulative_seconds': 1500.0} for _ in range(40)],
            'reader_cache_observation': observation.snapshot(trace=True),
            'ended_at': '2026-10-18T00:00:00+00:00', 'elapsed_seconds': 1511.532}

    def assert_real_checkpoint(self, out):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'final.json'
            checkpoint_audit.checkpoint_output(path, out)
            self.assertEqual(path.stat().st_size, out['output_budget']['serialized_bytes'])
            self.assertLessEqual(path.stat().st_size, 512 * 1024)
            self.assertEqual(json.loads(path.read_text()), out)

    def test_saturated_observer_and_full_results_fit_original_pretty_json_gate(self):
        kind = ReaderObservationTests().reader_type()
        readers = [kind() for _ in range(profile.ReaderCacheObservation.MAX_READERS)]
        observation = profile.ReaderCacheObservation(kind, native_policy='skip_manifest')
        observation.start()
        try:
            for i in range(observation.MAX_EVENTS + 1):
                reader = readers[i % len(readers)]
                reader._blob('c', 'state_archives/' + str(i % observation.MAX_KEYS))
            out = self.payload(observation)
            required = {key: value for key, value in out.items() if key != 'reader_cache_observation'}
            aggregate = observation.snapshot()
            self.assertEqual(aggregate['traced_keys'], observation.MAX_KEYS)
            self.assertEqual(aggregate['traced_events'], observation.MAX_EVENTS)
            self.assertEqual(aggregate['calls'], observation.MAX_EVENTS + 1)
            self.assertFalse(aggregate['trace_complete'])
            profile.budget_final_output(out)
            self.assertEqual({key: out[key] for key in required}, required)
            snapshot = out['reader_cache_observation']
            self.assertEqual({key: snapshot[key] for key in aggregate}, aggregate)
            self.assertEqual(snapshot['detailed_trace_export'], 'detailed_trace_not_exported_within_original_bound')
            self.assertNotIn('keys', snapshot); self.assertNotIn('events', snapshot)
            self.assertEqual(len(snapshot['reader_summaries']), 8)
            self.assert_real_checkpoint(out)
        finally: observation.stop()

    def test_old_large_trace_is_optional_but_failure_and_counters_are_preserved(self):
        observation = profile.ReaderCacheObservation(None, native_policy='skip_manifest')
        out = self.payload(observation)
        snapshot = out['reader_cache_observation']
        snapshot.update(keys=[['a' * 64, 0, 'transport', 99999] for _ in range(1796)],
                        events=[[i % 1796, 0] for i in range(18586)], resets=[[18586, 0]])
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(checkpoint_audit.AuditError, 'bounded_output_limit'):
                checkpoint_audit.checkpoint_output(Path(directory) / 'oversized.json', out)
        profile.budget_final_output(out)
        self.assertEqual(out['status'], 'diagnostic_deadline')
        self.assertEqual(out['error_code'], 'native_diagnostic_failure')
        self.assertEqual(snapshot['native_replay_match_counter'], 'actual_vs_skip_manifest_hit_mismatches')
        self.assertEqual(set(out['output_budget']['optional_reader_trace_fields_removed']),
            {'key_columns', 'event_columns', 'keys', 'events', 'resets', 'initial_cache_key_indices',
             'budget_event_columns', 'budget_events'})
        self.assert_real_checkpoint(out)

    def test_required_results_remain_subject_to_unchanged_hard_limit(self):
        out = self.payload(profile.ReaderCacheObservation(None))
        out['required_result'] = 'x' * (512 * 1024)
        profile.budget_final_output(out)
        self.assertEqual(len(out['required_result']), 512 * 1024)
        self.assertGreater(out['output_budget']['serialized_bytes'], 512 * 1024)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'final.json'; path.write_text('previous checkpoint')
            with self.assertRaisesRegex(checkpoint_audit.AuditError, 'bounded_output_limit'):
                checkpoint_audit.checkpoint_output(path, out)
            self.assertEqual(path.read_text(), 'previous checkpoint')


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

    def run_main(self, *, deadline=False, swallow_deadline=False, failed=False,
                 freeze_elapsed=0.0, cleanup_elapsed=None, execution_implementation=IMPL,
                 saved_implementation=None, expect_freeze=True, function_profiler=True, prewarm=False):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); output = root / 'profile.json'
            status = {}; closes = []; calls = []
            guard_instance = object()
            clock = {'elapsed': 0.0}
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
                self.assertEqual(refs, list(summary()['model_cards'].values()))
                self.assertIsNotNone(cutoff.utcoffset())
                self.assertIs(capacity_guard, guard_instance)
                if deadline:
                    if swallow_deadline:
                        try: status['handler'](0, None)
                        except BaseException: pass
                    else:
                        status['handler'](0, None)
                if failed:
                    raise ValueError('sensitive free exception text')
                clock['elapsed'] = freeze_elapsed
                return [{'origin_runtime_path': 'saved/input.json', 'sha256': 'c' * 64}]
            def close_readers():
                closes.append(True)
                if cleanup_elapsed is not None: clock['elapsed'] = cleanup_elapsed
            def set_signal(_number, handler):
                previous = status.get('handler'); status['handler'] = handler
                return previous
            modules = {
                'fixed_fixture_validation_audit': types.SimpleNamespace(restore_fixed=restore, sha40=sha40),
                'fixed_research_report_audit': types.SimpleNamespace(
                    checkpoint_output=checkpoint_audit.checkpoint_output),
                'cloud_state': types.SimpleNamespace(_git=lambda *_: types.SimpleNamespace(stdout='4' * 40)),
                'football_model.history_archive': types.SimpleNamespace(
                    read_bytes=lambda path, **_: path.read_bytes(), _close_readers=close_readers),
                'football_model.advanced_snapshots': types.SimpleNamespace(freeze_inputs=freeze),
                'football_model.advanced_capacity': types.SimpleNamespace(CapacityGuard=lambda runtime: guard_instance),
                'football_model.advanced_research': types.SimpleNamespace(
                    implementation_fingerprint=lambda: {'sha256': execution_implementation})}
            factory_patch = (patch.object(profile.cProfile, 'Profile', wraps=cProfile.Profile) if function_profiler
                else patch.object(profile.cProfile, 'Profile', side_effect=AssertionError('native timing constructed cProfile')))
            rows_patch = (contextlib.nullcontext() if function_profiler
                else patch.object(profile, 'profile_rows', side_effect=AssertionError('native timing read function stats')))
            with factory_patch as factory, rows_patch, patch.dict(sys.modules, modules), \
                    patch.object(profile.signal, 'signal', side_effect=set_signal), \
                    patch.object(profile.signal, 'alarm'), \
                    patch.object(profile.time, 'monotonic', side_effect=lambda: 100.0 + clock['elapsed']), \
                    contextlib.redirect_stdout(io.StringIO()):
                argv = ['--repo', str(root), '--state-commit', '2' * 40,
                        '--league', 'championship', '--implementation', execution_implementation,
                        '--deadline-seconds', '60', '--output', str(output)]
                if saved_implementation is not None:
                    argv += ['--research-implementation', saved_implementation]
                if not function_profiler:
                    argv += ['--no-function-profiler']
                if prewarm:
                    argv += ['--prewarm']
                code = profile.main(argv)
                self.assertEqual(factory.call_count, int(function_profiler))
            result = json.loads(output.read_text())
            self.assertTrue(closes)
            self.assertEqual(len(calls), 1 if expect_freeze else 0)
            self.assertEqual(result['function_profiler_enabled'], function_profiler)
            self.assertEqual(result['measurement_kind'], 'full_function_diagnostic' if function_profiler
                else 'native_production_timing')
            self.assertEqual(result['schema_version'], 'fixed-capture-input-profile-v1' if function_profiler
                else 'fixed-capture-native-timing-v1')
            if not function_profiler:
                self.assertIsNone(result['profile_functions'])
                self.assertFalse(result['prewarm_enabled'])
            self.assertEqual(result['network_football_requests'], 0)
            self.assertEqual(result['fits'], 0)
            self.assertEqual(result['remote_state_writes'], 0)
            self.assertFalse(result['live_capture_or_first_output_created'])
            self.assertNotIn('sensitive free exception text', json.dumps(result))
            self.assertEqual(output.stat().st_size, result['output_budget']['serialized_bytes'])
            self.assertEqual(result['reader_cache_observation']['detailed_trace_export'],
                'detailed_trace_not_exported_within_original_bound')
            self.assertNotIn('events', result['reader_cache_observation'])
            return code, result

    def test_end_to_end_diagnostic_not_capture(self):
        code, result = self.run_main()
        self.assertEqual(code, 0)
        self.assertEqual(result['frozen_originals'], 1)
        self.assertTrue(result['function_profiler_enabled'])
        self.assertIsInstance(result['profile_functions'], list)
        self.assertGreater(len(result['profile_functions']), 0)

    def test_native_timing_skips_profiler_and_keeps_the_same_single_freeze_call(self):
        code, result = self.run_main(function_profiler=False)
        self.assertEqual(code, 0)
        expected = [('saved/input.json', 'c' * 64)]
        self.assertEqual(result['frozen_inventory_sha256'],
            hashlib.sha256(json.dumps(expected, separators=(',', ':')).encode()).hexdigest())

    def test_native_timing_forbids_prewarm_before_loading_or_profiler_creation(self):
        with self.assertRaisesRegex(profile.ProfileError, 'native_timing_prewarm_forbidden'):
            self.run_main(function_profiler=False, prewarm=True)

    def test_native_timing_direct_deadline_is_preserved(self):
        code, result = self.run_main(function_profiler=False, deadline=True)
        self.assertEqual(code, 1)
        self.assertEqual(result['status'], 'diagnostic_deadline')
        self.assertTrue(result['deadline_observation']['expired_flag'])

    def test_native_timing_swallowed_deadline_still_fails(self):
        code, result = self.run_main(function_profiler=False, deadline=True, swallow_deadline=True)
        self.assertEqual(code, 1)
        self.assertEqual(result['status'], 'diagnostic_deadline')
        self.assertTrue(result['deadline_observation']['expired_flag'])

    def test_native_timing_late_signal_keeps_unrounded_gate(self):
        code, result = self.run_main(function_profiler=False, freeze_elapsed=60.0004)
        self.assertEqual(code, 1)
        self.assertEqual(result['elapsed_seconds'], 60.0)
        self.assertTrue(result['deadline_observation']['unrounded_elapsed_exceeded_limit'])

    def test_native_timing_cleanup_overshoot_fails(self):
        code, result = self.run_main(function_profiler=False, freeze_elapsed=59.0, cleanup_elapsed=60.0004)
        self.assertEqual(code, 1)
        self.assertEqual(result['status'], 'diagnostic_deadline')
        self.assertEqual(result['failed_stage'], 'final_cleanup')
        self.assertTrue(result['deadline_observation']['cleanup_included_in_elapsed_gate'])

    def test_native_timing_keeps_native_failure_when_cleanup_exceeds_limit(self):
        code, result = self.run_main(function_profiler=False, failed=True, cleanup_elapsed=61.0)
        self.assertEqual(code, 1)
        self.assertEqual(result['status'], 'diagnostic_deadline')
        self.assertEqual(result['prior_native_failure']['exception_class'], 'ValueError')
        self.assertEqual(result['prior_native_failure']['status'], 'diagnostic_incomplete')

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

    def test_deadline_survives_real_path_resolve_oserror_fallback(self):
        class OldDeadline(OSError): pass
        path = Path('/__engineering_deadline_probe_missing__/child')
        with patch('os.lstat', side_effect=OldDeadline('deadline')):
            self.assertEqual(path.resolve(strict=False), path)
        with patch('os.lstat', side_effect=profile.ProfileDeadline('deadline')):
            with self.assertRaises(profile.ProfileDeadline): path.resolve(strict=False)

    def test_expired_flag_cannot_be_erased_by_a_lower_layer_catching_baseexception(self):
        code, result = self.run_main(deadline=True, swallow_deadline=True)
        self.assertEqual(code, 1)
        self.assertEqual(result['status'], 'diagnostic_deadline')
        self.assertTrue(result['deadline_observation']['expired_flag'])
        self.assertEqual(result['error_code'], 'bounded_diagnostic_deadline')

    def test_completion_uses_unrounded_elapsed_when_the_signal_is_delayed(self):
        code, result = self.run_main(freeze_elapsed=60.0004)
        self.assertEqual(result['elapsed_seconds'], 60.0)
        self.assertEqual(code, 1)
        self.assertEqual(result['status'], 'diagnostic_deadline')
        self.assertTrue(result['deadline_observation']['unrounded_elapsed_exceeded_limit'])

    def test_cleanup_is_inside_the_same_elapsed_success_gate(self):
        code, result = self.run_main(freeze_elapsed=59.0, cleanup_elapsed=60.0004)
        self.assertEqual(code, 1)
        self.assertEqual(result['frozen_originals'], 1)
        self.assertEqual(result['status'], 'diagnostic_deadline')
        self.assertEqual(result['failed_stage'], 'final_cleanup')

    def test_cleanup_deadline_preserves_an_earlier_native_failure(self):
        code, result = self.run_main(failed=True, cleanup_elapsed=61.0)
        self.assertEqual(code, 1)
        self.assertEqual(result['status'], 'diagnostic_deadline')
        self.assertEqual(result['prior_native_failure']['exception_class'], 'ValueError')
        self.assertEqual(result['prior_native_failure']['error_code'], 'native_diagnostic_failure')
        self.assertEqual(result['prior_native_failure']['status'], 'diagnostic_incomplete')

    def test_native_failure_redacted(self):
        code, result = self.run_main(failed=True)
        self.assertEqual(code, 1)
        self.assertEqual(result['status'], 'diagnostic_incomplete')


class NativeTimingIntegrationTests(unittest.TestCase):
    def test_disabled_mode_runs_real_native_recursive_freeze_without_constructing_profiler(self):
        from football_model import advanced_snapshots as native_snapshots
        from football_model.advanced_capacity import CapacityGuard
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory); output = repo / 'native.json'
            original = {}
            def restore(_repo, runtime, _state, _cloud):
                child = 'advanced_models/inputs/' + 'c' * 64 + '.json'
                original[child] = b'{"synthetic":true}\n'
                child_ref = {'path': child, 'sha256': hashlib.sha256(original[child]).hexdigest()}
                card = summary()['model_cards']['G0']['path']
                original[card] = json.dumps({'training_source': child_ref, 'duplicate': child_ref}).encode()
                document = summary()
                document['model_cards']['G0']['sha256'] = hashlib.sha256(original[card]).hexdigest()
                for name, raw in original.items():
                    path = runtime / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(raw)
                latest = runtime / 'advanced_models/championship/p/reports/latest.json'
                latest.parent.mkdir(parents=True); latest.write_text(json.dumps(document))
                return {'test_only': True}
            helpers = {
                'fixed_fixture_validation_audit': types.SimpleNamespace(restore_fixed=restore, sha40=lambda value: value),
                'fixed_research_report_audit': types.SimpleNamespace(checkpoint_output=checkpoint_audit.checkpoint_output),
                'cloud_state': types.SimpleNamespace(_git=lambda *_: types.SimpleNamespace(stdout='4' * 40)),
                'football_model.advanced_research': types.SimpleNamespace(implementation_fingerprint=lambda: {'sha256': IMPL})}
            with patch.dict(sys.modules, helpers), \
                    patch.object(profile.cProfile, 'Profile', side_effect=AssertionError('native timing constructed cProfile')), \
                    patch.object(profile, 'profile_rows', side_effect=AssertionError('native timing sampled functions')), \
                    patch.object(native_snapshots, 'freeze_inputs', wraps=native_snapshots.freeze_inputs) as native_freeze, \
                    contextlib.redirect_stdout(io.StringIO()):
                code = profile.main(['--repo', str(repo), '--state-commit', '2' * 40,
                    '--league', 'championship', '--implementation', IMPL, '--deadline-seconds', '60',
                    '--no-function-profiler', '--output', str(output)])
            result = json.loads(output.read_text())
            self.assertEqual(code, 0, result)
            self.assertEqual(native_freeze.call_count, 1)
            runtime = repo / 'fixed-capture-profile-runtime'
            args, kwargs = native_freeze.call_args
            self.assertEqual(args[0], runtime)
            self.assertEqual(args[1], runtime / 'diagnostic-profile-inputs')
            self.assertEqual(len(args[2]), 1)
            self.assertIsNotNone(args[3].utcoffset())
            self.assertIsInstance(kwargs['capacity_guard'], CapacityGuard)
            self.assertEqual(result['frozen_originals'], 2)
            inventory = sorted((name, hashlib.sha256(raw).hexdigest()) for name, raw in original.items())
            self.assertEqual(result['frozen_inventory_sha256'],
                hashlib.sha256(json.dumps(inventory, separators=(',', ':')).encode()).hexdigest())
            for name, raw in original.items():
                self.assertEqual((runtime / name).read_bytes(), raw)
                self.assertEqual((args[1] / 'sources' / (hashlib.sha256(raw).hexdigest() + '.json')).read_bytes(), raw)
            self.assertFalse((runtime / 'advanced_snapshots').exists())
            self.assertFalse(result['prewarm_enabled'])
            self.assertFalse(result['function_profiler_enabled'])
            self.assertIsNone(result['profile_functions'])
            self.assertEqual(result['measurement_kind'], 'native_production_timing')
            self.assertEqual(result['schema_version'], 'fixed-capture-native-timing-v1')
            self.assertEqual(result['deadline_observation']['limit_seconds'], 60)
            self.assertTrue(result['deadline_observation']['cleanup_included_in_elapsed_gate'])
            self.assertLessEqual(output.stat().st_size, 512 * 1024)
            for key in ('network_football_requests', 'fits', 'remote_state_writes'):
                self.assertEqual(result[key], 0)

    def test_workflow_uses_a_distinct_fresh_native_measurement_and_original_bounds(self):
        text = (ROOT / '.github/workflows/fixed-capture-input-profile.yml').read_text()
        self.assertIn('name: 固定捕获输入：原生无预热性能验收', text)
        self.assertEqual(text.count('python scripts/fixed_capture_input_profile.py'), 1)
        self.assertIn('--no-function-profiler', text)
        self.assertNotIn('--prewarm', text)
        self.assertIn('--deadline-seconds 1500', text)
        self.assertIn('--state-commit e55c1dad479990567169341624345db239ed8f4e', text)
        self.assertIn('--implementation f4b3d8aafaf0cea8b8c0d6b557cf9efa5d6da124296c57a561de7d5c8f4e526f', text)
        self.assertIn('--research-implementation 1e311689b3405f64c6ad9636732d2defb7ad67e26b4bc7e04b01a5b1b2b2d6ac', text)
        self.assertIn('--output fixed-capture-native-timing.json', text)
        self.assertIn('name: fixed-capture-native-timing-${{ github.run_id }}-${{ github.run_attempt }}', text)
        self.assertIn('path: fixed-capture-native-timing.json', text)
        self.assertNotIn('concurrency:', text)
        self.assertNotIn('schedule:', text)
        self.assertIn('runs-on: ubuntu-latest', text)
        self.assertIn("FOOTBALL_API_KEY: ''", text)
        self.assertNotIn('cloud_state_save_once', text)
        self.assertNotIn('advanced_research study', text)


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
