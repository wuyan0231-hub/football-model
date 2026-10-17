"""Bounded diagnostic of native freezing of saved training-card input closures.

No live capture, queue, first output, football API, fitting, promotion or remote
state write. Local diagnostic copies are disposable; this is NOT an EARLY replay.
"""
from __future__ import annotations
import argparse
import cProfile
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
            with lock:
                if len(out['stack_samples']) < 30:
                    out['stack_samples'].append({'elapsed_seconds': round(time.monotonic()-started, 3),
                                                'stage': out['active_stage']['stage'], 'frames': frames})
                checkpoint_output(output, out)
            print(json.dumps({'profile_stack': frames}), flush=True)

    def alarm(_signum, _frame):
        nonlocal expired
        expired = True
        raise ProfileDeadline('bounded_diagnostic_deadline')

    previous = signal.signal(signal.SIGALRM, alarm)
    signal.alarm(args.deadline_seconds)
    thread = threading.Thread(target=sample, daemon=True)
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
        _close_readers()
        out['profile_functions'] = profile_rows(profiler)
        out['ended_at'] = datetime.now(timezone.utc).isoformat()
        out['elapsed_seconds'] = round(time.monotonic()-started, 3)
        flush()
    print(json.dumps(out, ensure_ascii=False, allow_nan=False), flush=True)
    return 0 if out['status'] == 'diagnostic_completed_with_stated_scope' else 1


if __name__ == '__main__':
    raise SystemExit(main())

