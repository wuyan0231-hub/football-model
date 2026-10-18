"""Bounded diagnostic of native freezing of saved training-card input closures.

No live capture, queue, first output, football API, fitting, promotion or remote
state write. Local diagnostic copies are disposable; this is NOT an EARLY replay.
"""
from __future__ import annotations
import argparse
import cProfile
from collections import OrderedDict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import shutil
import signal
import sys
import threading
import time


class ProfileError(ValueError):
    pass


class ProfileDeadline(OSError):
    pass


class ReaderCacheObservation:
    """Read-only bounded telemetry; native methods still run exactly once.

    Trace keys are hashes, never source paths or provider bytes. The two cache
    simulations retain sizes only and stop when their exact trace is bounded.
    They predict cache requests, not runtime or a validated production speedup.
    """
    MAX_EVENTS = 50000
    MAX_KEYS = 4096
    MAX_READERS = 8

    def __init__(self, reader_type, *, native_policy='unspecified'):
        self.reader_type = reader_type
        self.native_policy = native_policy
        self.lock = threading.RLock()
        self.readers = {}
        self.keys = []
        self.key_ids = {}
        self.events = []
        self.resets = []
        self.initial = []
        self.complete = True
        self.truncation = None
        self.calls = self.exceptions = self.missing = self.process_replacements = 0
        self.categories = {name: {'calls': 0, 'hits': 0, 'misses': 0,
            'positive_calls': 0, 'returned_bytes': 0, 'miss_bytes': 0,
            'max_blob_bytes': 0, 'size_bins': [0] * 6} for name in ('manifest', 'transport', 'other')}

    def truncate(self, reason):
        self.complete = False
        self.truncation = self.truncation or reason

    @staticmethod
    def category(relative):
        return 'manifest' if relative == 'state_storage.json' else ('transport' if relative.startswith('state_archives/') else 'other')

    def key(self, state, commit, relative, size):
        value = hashlib.sha256(json.dumps([state['number'], commit, relative], separators=(',', ':')).encode()).hexdigest()
        if value not in self.key_ids:
            if len(self.keys) >= self.MAX_KEYS:
                self.truncate('key_limit')
                return None
            self.key_ids[value] = len(self.keys)
            self.keys.append([value, state['number'], self.category(relative), size])
        index = self.key_ids[value]
        if self.keys[index][3] != size:
            self.truncate('immutable_key_size_changed')
            return None
        return index

    def reader(self, reader):
        identity = reader
        if identity not in self.readers:
            if len(self.readers) >= self.MAX_READERS:
                self.truncate('reader_limit')
                return None
            state = {'number': len(self.readers), 'budget': reader.cache_bytes,
                'original': OrderedDict(), 'skip_manifest': OrderedDict(),
                'original_bytes': 0, 'skip_manifest_bytes': 0,
                'original_hits': 0, 'original_misses': 0, 'skip_manifest_hits': 0, 'skip_manifest_misses': 0,
                'original_transport_hits': 0, 'skip_manifest_transport_hits': 0,
                'actual_vs_original_hit_mismatches': 0, 'actual_vs_skip_manifest_hit_mismatches': 0,
                'close_calls': 0,
                'close_with_process': 0, 'close_cached_bytes': 0,
                'peak_actual_cached_bytes': reader._cached_bytes}
            self.readers[identity] = state
            if self.complete:
                if len(reader._cache) > self.MAX_KEYS:
                    self.truncate('initial_cache_key_limit')
                else:
                    for (commit, relative), raw in reader._cache.items():
                        if not isinstance(raw, bytes):
                            self.truncate('unexpected_native_cache_value'); break
                        index = self.key(state, commit, relative, len(raw))
                        if index is None: break
                        self.initial.append(index)
                        for policy in ('original', 'skip_manifest'):
                            state[policy][index] = len(raw)
                            state[policy + '_bytes'] += len(raw)
        return self.readers[identity]

    def replay(self, state, policy, index, actual_hit):
        cache = state[policy]
        size = self.keys[index][3]
        category = self.keys[index][2]
        hit = index in cache
        state[policy + ('_hits' if hit else '_misses')] += 1
        if hit:
            cache.move_to_end(index)
            if category == 'transport': state[policy + '_transport_hits'] += 1
        elif size <= state['budget'] and not (policy == 'skip_manifest' and category == 'manifest'):
            while cache and state[policy + '_bytes'] + size > state['budget']:
                _, previous = cache.popitem(last=False)
                state[policy + '_bytes'] -= previous
            cache[index] = size
            state[policy + '_bytes'] += size
        if hit != actual_hit:
            state['actual_vs_' + policy + '_hit_mismatches'] += 1

    def start(self):
        if self.reader_type is None: return
        self.original_blob = self.reader_type._blob
        self.original_close = self.reader_type.close
        observation = self
        def blob(reader, commit, relative, *, missing=False):
            with observation.lock:
                state = observation.reader(reader)
                category = observation.category(relative)
                counters = observation.categories[category]
                hit = (commit, relative) in reader._cache
                counters['calls'] += 1
                counters['hits' if hit else 'misses'] += 1
                observation.calls += 1
                before_process = reader._process
            try:
                raw = observation.original_blob(reader, commit, relative, missing=missing)
            except BaseException:
                with observation.lock: observation.exceptions += 1
                raise
            with observation.lock:
                if reader._process is not None and reader._process is not before_process:
                    observation.process_replacements += 1
                if state is not None:
                    state['peak_actual_cached_bytes'] = max(state['peak_actual_cached_bytes'], reader._cached_bytes)
                if raw is None:
                    observation.missing += 1
                elif isinstance(raw, bytes):
                    size = len(raw)
                    counters['positive_calls'] += 1
                    counters['returned_bytes'] += size
                    if not hit: counters['miss_bytes'] += size
                    counters['max_blob_bytes'] = max(counters['max_blob_bytes'], size)
                    bounds = (4096, 65536, 1048576, 8388608, 33554432)
                    counters['size_bins'][next((i for i, bound in enumerate(bounds) if size <= bound), 5)] += 1
                    if observation.complete and state is not None:
                        if len(observation.events) + len(observation.resets) >= observation.MAX_EVENTS:
                            observation.truncate('event_limit')
                        else:
                            index = observation.key(state, commit, relative, size)
                            if index is not None:
                                observation.events.append([index, int(hit)])
                                observation.replay(state, 'original', index, hit)
                                observation.replay(state, 'skip_manifest', index, hit)
                return raw
        def close(reader):
            with observation.lock:
                state = observation.readers.get(reader)
                if state is not None:
                    state['close_calls'] += 1
                    state['close_with_process'] += int(reader._process is not None)
                    state['close_cached_bytes'] += reader._cached_bytes
                    if observation.complete:
                        if len(observation.events) + len(observation.resets) >= observation.MAX_EVENTS:
                            observation.truncate('event_limit')
                        else:
                            observation.resets.append([len(observation.events), state['number']])
                            for policy in ('original', 'skip_manifest'):
                                state[policy].clear(); state[policy + '_bytes'] = 0
            return observation.original_close(reader)
        self.reader_type._blob, self.reader_type.close = blob, close

    def stop(self):
        if self.reader_type is not None:
            self.reader_type._blob, self.reader_type.close = self.original_blob, self.original_close

    def snapshot(self, *, trace=False):
        with self.lock:
            readers = []
            for state in self.readers.values():
                summary = {key: value for key, value in state.items() if not isinstance(value, OrderedDict)}
                for policy in ('original', 'skip_manifest'):
                    summary[policy + '_cached_bytes_by_category'] = {category:
                        sum(size for index, size in state[policy].items() if self.keys[index][2] == category)
                        for category in self.categories}
                readers.append(summary)
            categories = json.loads(json.dumps(self.categories))
            for category, counters in categories.items():
                observed = [key[3] for key in self.keys if key[2] == category]
                counters['traced_distinct_keys'] = len(observed)
                counters['traced_distinct_bytes'] = sum(observed)
            result = {'calls': self.calls, 'exceptions': self.exceptions, 'missing_returns': self.missing,
                'process_replacements': self.process_replacements, 'categories': categories,
                'reader_summaries': readers, 'trace_complete': self.complete, 'trace_truncation': self.truncation,
                'traced_events': len(self.events), 'traced_resets': len(self.resets), 'traced_keys': len(self.keys),
                'limits': {'events_including_resets': self.MAX_EVENTS, 'keys': self.MAX_KEYS, 'readers': self.MAX_READERS},
                'native_policy': self.native_policy,
                'policy_labels': {'original': 'prior_cache_all_baseline', 'skip_manifest': 'exclude_manifest_admission'},
                'native_replay_match_counter': ('actual_vs_' + self.native_policy + '_hit_mismatches'
                    if self.native_policy in ('original', 'skip_manifest') else None),
                'replay_scope': 'Exact recorded prefix, seeded from observed native cache; alternative is request-count simulation only',
                'native_cache_or_return_values_changed': False}
            if trace:
                result.update(key_columns=['sha256_of_exact_reader_commit_path', 'reader', 'category', 'bytes'],
                    event_columns=['key_index', 'actual_hit'], keys=list(self.keys), events=list(self.events),
                    resets=list(self.resets), initial_cache_key_indices=list(self.initial))
            return result

    def minute_lines(self, snapshot=None):
        """Keep each browser-visible log row below its truncation boundary."""
        snapshot = self.snapshot() if snapshot is None else snapshot
        def line(name, value, limit):
            encoded = json.dumps({name: value}, separators=(',', ':'), ensure_ascii=True)
            return encoded if len(encoded) < limit else json.dumps(
                {'profile_cache_log_bound': {'section': name, 'full_snapshot_saved': True}}, separators=(',', ':'))
        lines = [line('profile_cache_brief', {'calls': snapshot['calls'], 'exceptions': snapshot['exceptions'],
            'missing': snapshot['missing_returns'], 'process_new': snapshot['process_replacements'],
            'native': snapshot['native_policy'], 'trace_complete': snapshot['trace_complete'],
            'truncation': snapshot['trace_truncation'], 'events': snapshot['traced_events'], 'keys': snapshot['traced_keys']}, 400)]
        for category, counters in snapshot['categories'].items():
            lines.append(line('profile_cache_category', {'kind': category, 'calls': counters['calls'],
                'hits': counters['hits'], 'misses': counters['misses'], 'positive': counters['positive_calls'],
                'returned_bytes': counters['returned_bytes'], 'miss_bytes': counters['miss_bytes'],
                'max_bytes': counters['max_blob_bytes'], 'distinct': counters['traced_distinct_keys'],
                'distinct_bytes': counters['traced_distinct_bytes']}, 350))
        for reader in snapshot['reader_summaries']:
            policies = {}
            for policy, label in (('original', 'prior_all_baseline'), ('skip_manifest', 'skip_manifest')):
                counts = reader[policy + '_cached_bytes_by_category']
                policies[label] = {'hits': reader[policy + '_hits'], 'misses': reader[policy + '_misses'],
                    'transport_hits': reader[policy + '_transport_hits'],
                    'native_hit_diffs': reader['actual_vs_' + policy + '_hit_mismatches'],
                    'prefix_bytes': reader[policy + '_bytes'], 'manifest_bytes': counts['manifest'],
                    'transport_bytes': counts['transport']}
            lines.append(line('profile_cache_reader', {'id': reader['number'], 'native': snapshot['native_policy'],
                'budget': reader['budget'], 'close': reader['close_calls'],
                'close_with_process': reader['close_with_process'], 'replay_prefix_only': not snapshot['trace_complete'],
                **policies}, 800))
        return lines


def require(condition, code):
    if not condition:
        raise ProfileError(code)


def safe_stack(frame, limit=12):
    """Only code locations: never frame locals, source lines or exception text."""
    result = []
    while frame is not None and len(result) < limit:
        result.append({'file': Path(frame.f_code.co_filename).name,
                       'function': frame.f_code.co_name, 'line': frame.f_lineno})
        frame = frame.f_back
    return result


def profile_rows(profiler, limit=40):
    rows = []
    for item in profiler.getstats():
        code = item.code
        if isinstance(code, str):
            # Builtin reprs can contain arbitrary object reprs. Do not export.
            continue
        rows.append({'file': Path(code.co_filename).name, 'function': code.co_name,
                     'line': code.co_firstlineno, 'calls': item.callcount,
                     'recursive_calls': item.reccallcount,
                     'own_seconds': round(item.inlinetime, 6),
                     'cumulative_seconds': round(item.totaltime, 6)})
    return sorted(rows, key=lambda r: -r['cumulative_seconds'])[:limit]


def validate_output(path, repo):
    path = Path(path)
    require(not path.is_symlink(), 'output_symlink')
    path = path.resolve()
    require(path.parent == repo.resolve() and path.suffix == '.json'
            and not path.exists(), 'output_must_be_new_root_json')
    return path


def select_references(summary, league, implementation):
    require(summary['league'] == league, 'summary_league_mismatch')
    require(summary['implementation_fingerprint']['sha256'] == implementation
            and len(summary['implementation_fingerprint']['files']) == 50,
            'implementation_mismatch')
    refs = summary.get('model_cards', {})
    require(isinstance(refs, dict) and 0 < len(refs) <= 17, 'bounded_latest_cards_required')
    for candidate, ref in refs.items():
        require(bool(re.fullmatch(r'[A-Za-z0-9_]+', candidate)), 'invalid_candidate')
        require(isinstance(ref, dict) and isinstance(ref.get('path'), str)
                and ref['path'].startswith('advanced_models/' + league + '/')
                and all(part not in ('', '.', '..') for part in ref['path'].split('/'))
                and '\\' not in ref['path'] and not any(ord(c) < 32 for c in ref['path'])
                and re.fullmatch(r'[0-9a-f]{64}', str(ref.get('sha256', ''))),
                'invalid_card_reference')
    return list(refs.values())


def research_implementation(execution, saved=None):
    """Bind old saved input identity separately from the code under diagnosis.

    This does not authorize capturing or promoting old cards with a new
    implementation. The only operation here is an isolated closure experiment.
    """
    require(isinstance(execution, str) and bool(re.fullmatch(r'[0-9a-f]{64}', execution)), 'invalid_implementation')
    value = execution if saved is None else saved
    require(isinstance(value, str) and bool(re.fullmatch(r'[0-9a-f]{64}', value)),
            'invalid_research_implementation')
    return value


def prewarm_saved_originals(runtime, refs, *, read_bytes, refs_in, select_cold_path,
                           guard, progress, max_nodes=10000, max_edges=200000,
                           max_warm_bytes=512*1024*1024, max_occurrences=2000000):
    """Materialize only native immutable cold paths, then still run native freeze.

    This is disposable fixed-state preparation, never a cache of admission
    decisions. Native reads, capacity checks and later freezing remain mandatory.
    No existing file, mutable pointer, API cache or capture output is rewritten.
    """
    runtime = Path(runtime).absolute()
    require(not any(p.is_symlink() for p in (runtime, *runtime.parents)), 'warm_root_symlink')
    pending, scheduled = [], {}
    seen, inventory = {}, []
    edges = warmed = warm_bytes = unique_bytes = peak_pending = 0

    def schedule(ref):
        nonlocal edges, peak_pending
        edges += 1
        require(edges <= max_occurrences, 'warm_reference_occurrence_limit')
        require(isinstance(ref, dict), 'warm_invalid_reference')
        name = ref.get('runtime_relative_path', ref.get('path'))
        require(isinstance(name, str) and name and not Path(name).is_absolute()
                and all(p not in ('', '.', '..') for p in name.split('/'))
                and '\\' not in name and not any(ord(c) < 32 or ord(c) == 127 for c in name),
                'warm_invalid_reference_path')
        digest = ref.get('sha256')
        require(isinstance(digest, str) and re.fullmatch(r'[0-9a-f]{64}', digest), 'warm_invalid_digest')
        # Bound pending work by exact path/digest bindings, not duplicates
        # waiting on the stack. Check conflicts on every occurrence, including
        # those not yet read. This is only preparation: native freeze below
        # still validates every original edge and its complete bytes.
        if name in scheduled:
            require(scheduled[name] == digest, 'warm_reference_version_conflict')
            return
        require(len(scheduled) < max_edges, 'warm_edge_limit')
        require(len(scheduled) < max_nodes, 'warm_node_limit')
        scheduled[name] = digest
        pending.append((name, digest))
        peak_pending = max(peak_pending, len(pending))

    for ref in refs:
        schedule(ref)
    while pending:
        name, digest = pending.pop()
        path = runtime / name
        require(not any(p.is_symlink() for p in (path, *path.parents)), 'warm_reference_symlink')
        raw = read_bytes(path, runtime=runtime)
        require(isinstance(raw, bytes) and hashlib.sha256(raw).hexdigest() == digest,
                'warm_reference_hash_mismatch')
        seen[name] = digest
        unique_bytes += len(raw)
        # Bound traversal independently of materialized bytes and the native
        # per-file limit. This is a resource stop, not a relaxed closure pass.
        require(unique_bytes <= 4*max_warm_bytes, 'warm_read_byte_limit')
        if select_cold_path(name) and not path.exists():
            require(warm_bytes + len(raw) <= max_warm_bytes, 'warm_byte_limit')
            require(shutil.disk_usage(runtime).free >= len(raw) + 256*1024*1024, 'warm_disk_reserve')
            ticket = guard.check_write(path, raw)
            path.parent.mkdir(parents=True, exist_ok=True)
            require(not any(p.is_symlink() for p in (path, *path.parents)), 'warm_reference_symlink')
            with path.open('xb') as handle:
                handle.write(raw)
            guard.commit_write(ticket)
            inventory.append((name, digest))
            warmed += 1; warm_bytes += len(raw)
        if path.suffix == '.json':
            def pairs(items):
                value = {}
                for key, item in items:
                    require(key not in value, 'warm_duplicate_json_key')
                    value[key] = item
                return value
            def nonfinite(_value):
                raise ProfileError('warm_nonfinite_json')
            body = json.loads(raw, object_pairs_hook=pairs, parse_constant=nonfinite)
            for child in refs_in(body):
                schedule(child)
        if len(seen) % 128 == 0:
            progress('prewarm_exact_saved_originals', unique_nodes=len(seen), edges=edges,
                     scheduled_bindings=len(scheduled), peak_pending=peak_pending,
                     warmed_originals=warmed, warmed_bytes=warm_bytes)
    return {'unique_nodes': len(seen), 'edges': edges, 'warmed_originals': warmed,
            'scheduled_bindings': len(scheduled), 'deduplicated_occurrences': edges-len(scheduled),
            'peak_pending': peak_pending,
            'warmed_bytes': warm_bytes, 'unique_read_bytes': unique_bytes,
            'inventory_sha256': hashlib.sha256(json.dumps(sorted(inventory), separators=(',', ':')).encode()).hexdigest(),
            'closure_inventory_sha256': hashlib.sha256(json.dumps(sorted(seen.items()), separators=(',', ':')).encode()).hexdigest(),
            'limits': {'nodes': max_nodes, 'scheduled_bindings': max_edges,
                       'reference_occurrences': max_occurrences, 'warm_bytes': max_warm_bytes},
            'native_freeze_still_required': True}


def main(argv=None):
    from fixed_fixture_validation_audit import restore_fixed, sha40
    from fixed_research_report_audit import checkpoint_output
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', type=Path, required=True)
    parser.add_argument('--state-commit', type=sha40, required=True)
    parser.add_argument('--league', choices=['championship'], required=True)
    parser.add_argument('--implementation', required=True)
    parser.add_argument('--research-implementation', help='Exact original fingerprint of the saved cards; never rewritten')
    parser.add_argument('--deadline-seconds', type=int, default=1500)
    parser.add_argument('--prewarm', action='store_true', help='Materialize exact immutable cold originals before unchanged native freeze')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    saved_implementation = research_implementation(args.implementation, args.research_implementation)
    require(60 <= args.deadline_seconds <= 1500, 'invalid_deadline')
    repo = args.repo.resolve()
    output = validate_output(args.output, repo)
    runtime = repo / 'fixed-capture-profile-runtime'
    require(not runtime.exists() and not runtime.is_symlink(), 'diagnostic_runtime_must_be_new')
    sys.path.insert(0, str(repo))
    import cloud_state as cloud
    from football_model import history_archive as history_observed
    from football_model.history_archive import read_bytes, _close_readers
    from football_model.advanced_snapshots import freeze_inputs
    from football_model.advanced_capacity import CapacityGuard
    from football_model.advanced_research import implementation_fingerprint
    out = {'schema_version': 'fixed-capture-input-profile-v1',
           'state_commit': args.state_commit, 'league': args.league,
           'implementation': args.implementation,
           'research_implementation': saved_implementation,
           'started_at': datetime.now(timezone.utc).isoformat(),
           'network_football_requests': 0, 'fits': 0, 'remote_state_writes': 0,
           'live_capture_or_first_output_created': False,
           'status': 'diagnostic_in_progress', 'stack_samples': [],
           'limitations': ['Instrumented fixed saved-card closure only, not the canceled job stack.',
                          'No original EARLY output recovered or live horizon qualified.',
                          'No production speedup, full short, independent R refit or all19 acceptance.']}
    started = time.monotonic()
    lock = threading.Lock()
    stop = threading.Event()
    main_ident = threading.get_ident()
    expired = False
    profiler = cProfile.Profile()
    cache_observation = ReaderCacheObservation(getattr(history_observed, 'FixedStateReader', None), native_policy='skip_manifest')

    def flush():
        with lock:
            checkpoint_output(output, out)

    def progress(stage, **counts):
        with lock:
            out['active_stage'] = {'stage': stage, 'elapsed_seconds': round(time.monotonic()-started, 3),
                                   'at': datetime.now(timezone.utc).isoformat(), **counts}
            checkpoint_output(output, out)
        print(json.dumps({'profile_progress': out['active_stage']}), flush=True)

    def sample():
        while not stop.wait(60):
            frames = safe_stack(sys._current_frames().get(main_ident))
            cache_snapshot = cache_observation.snapshot()
            with lock:
                out['reader_cache_observation'] = cache_snapshot
                if len(out['stack_samples']) < 30:
                    out['stack_samples'].append({'elapsed_seconds': round(time.monotonic()-started, 3),
                                                'stage': out['active_stage']['stage'], 'frames': frames})
                checkpoint_output(output, out)
            print(json.dumps({'profile_stack': frames}), flush=True)
            for cache_line in cache_observation.minute_lines(cache_snapshot):
                print(cache_line, flush=True)

    def alarm(_signum, _frame):
        nonlocal expired
        expired = True
        raise ProfileDeadline('bounded_diagnostic_deadline')

    previous = signal.signal(signal.SIGALRM, alarm)
    signal.alarm(args.deadline_seconds)
    thread = threading.Thread(target=sample, daemon=True)
    cache_observation.start()
    try:
        progress('restore_exact_fixed_state')
        thread.start()
        out['diagnostic_source_commit'] = cloud._git(repo, 'rev-parse', 'HEAD').stdout.strip()
        actual = implementation_fingerprint()
        require(actual['sha256'] == args.implementation, 'actual_implementation_mismatch')
        out['execution_implementation_fingerprint'] = actual
        out['restore_preflight'] = restore_fixed(repo, runtime, args.state_commit, cloud)
        progress('read_fixed_latest_card_references')
        paths = list((runtime / 'advanced_models' / args.league).glob('*/reports/latest.json'))
        require(len(paths) == 1, 'summary_namespace_not_unique')
        raw = read_bytes(paths[0], runtime=runtime)
        summary = json.loads(raw)
        refs = select_references(summary, args.league, saved_implementation)
        out.update(summary_sha256=hashlib.sha256(raw).hexdigest(),
                   research_source_commit=summary['source_commit'], selected_cards=len(refs))
        progress('native_capacity_guard')
        guard = CapacityGuard(runtime)
        out['prewarm_enabled'] = args.prewarm
        if args.prewarm:
            from football_model.history_archive import select_cold_path
            from football_model.history_archive import verified_catalog_read_session
            from football_model.advanced_snapshots import _refs_in
            progress('prewarm_exact_saved_originals')
            with verified_catalog_read_session(runtime):
                out['prewarm'] = prewarm_saved_originals(runtime, refs, read_bytes=read_bytes,
                    refs_in=_refs_in, select_cold_path=select_cold_path, guard=guard, progress=progress)
            progress('prewarm_completed', **out['prewarm'])
        cutoff = datetime.now(timezone.utc)
        progress('native_freeze_saved_card_inputs', selected_cards=len(refs))
        profiler.enable()
        frozen = freeze_inputs(runtime, runtime / 'diagnostic-profile-inputs', refs, cutoff, capacity_guard=guard)
        profiler.disable()
        # Native freezing already validates every edge. Bind the diagnostic copy
        # inventory without exporting provider content or mutable current files.
        inventory = sorted((r['origin_runtime_path'], r['sha256']) for r in frozen)
        out['frozen_originals'] = len(inventory)
        out['frozen_inventory_sha256'] = hashlib.sha256(json.dumps(inventory, separators=(',', ':')).encode()).hexdigest()
        if args.prewarm:
            require(out['prewarm']['unique_nodes'] == len(inventory)
                    and out['prewarm']['closure_inventory_sha256'] == out['frozen_inventory_sha256'],
                    'prewarm_native_inventory_mismatch')
        out['status'] = 'diagnostic_completed_with_stated_scope'
        progress('native_freeze_completed', frozen_originals=len(inventory))
    except Exception as exc:
        out.update(status='diagnostic_deadline' if expired else 'diagnostic_incomplete',
                   exception_class=type(exc).__name__,
                   error_code=str(exc) if isinstance(exc, ProfileError) else 'native_diagnostic_failure',
                   failed_stage=out['active_stage']['stage'])
    finally:
        profiler.disable()
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)
        stop.set()
        if thread.is_alive():
            thread.join(timeout=2)
        try:
            _close_readers()
        finally:
            cache_observation.stop()
            out['reader_cache_observation'] = cache_observation.snapshot(trace=True)
        out['profile_functions'] = profile_rows(profiler)
        out['ended_at'] = datetime.now(timezone.utc).isoformat()
        out['elapsed_seconds'] = round(time.monotonic()-started, 3)
        flush()
    print(json.dumps(out, ensure_ascii=False, allow_nan=False), flush=True)
    return 0 if out['status'] == 'diagnostic_completed_with_stated_scope' else 1


if __name__ == '__main__':
    raise SystemExit(main())
