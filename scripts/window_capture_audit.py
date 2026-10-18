"""Bounded single-league capture receipt/readback; never collects or fits.

The writer uses the unmodified native CLI. This helper only records actual
outcomes, then independently checks the saved fixed-tree target in a read job.
It is outside the research implementation identity. The live entry point is
locked to the reviewed execution version; this is not a compatibility map for
historical cards, which still pass their native exact-identity checks.
"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys

EXPECTED_IMPLEMENTATION = 'f4b3d8aafaf0cea8b8c0d6b557cf9efa5d6da124296c57a561de7d5c8f4e526f'
ALLOWED_LEAGUES = ('epl',)
SCHEDULE_DATE = '2026-10-17'
SCHEDULE_START = datetime(2026, 10, 17, 10, 0, tzinfo=timezone.utc)
SCHEDULE_END = datetime(2026, 10, 17, 11, 30, tzinfo=timezone.utc)


def validate_inputs(league, match_id, ref):
    if league not in ALLOWED_LEAGUES:
        raise ValueError('single_allowed_league_required')
    if not isinstance(match_id, str) or not re.fullmatch('[1-9][0-9]{0,11}', match_id):
        raise ValueError('positive_match_id_required')
    if ref != 'refs/heads/main':
        raise ValueError('main_dispatch_only')
    return int(match_id)


def dispatch_enabled(event_name, instant):
    if instant.tzinfo is None:
        raise ValueError('timezone_required')
    if event_name == 'workflow_dispatch':
        return True
    if event_name == 'schedule':
        # GitHub cron has no year. Recheck the actual clock outside the writer
        # lock so later annual runs cannot collect or save for this old fixture.
        return SCHEDULE_START <= instant.astimezone(timezone.utc) < SCHEDULE_END
    raise ValueError('unsupported_dispatch_event')


def safe_relative(name):
    if (not isinstance(name, str) or not name or PurePosixPath(name).is_absolute()
            or '\\' in name or any(ord(c) < 32 for c in name)
            or any(c in ('', '.', '..') for c in name.split('/'))):
        raise ValueError('unsafe_runtime_reference')
    return name


def validate_ref(ref):
    if (not isinstance(ref, dict) or set(ref) != {'path', 'sha256'}
            or not re.fullmatch('[0-9a-f]{64}', str(ref.get('sha256', '')))):
        raise ValueError('invalid_capture_reference')
    safe_relative(ref['path'])
    return ref


def read_json(path):
    raw = Path(path).read_bytes()
    if len(raw) > 60 * 1024 * 1024:
        raise ValueError('receipt_size_limit')
    return json.loads(raw)


def now():
    return datetime.now(timezone.utc).isoformat()


def check_implementation():
    from football_model.advanced_research import implementation_fingerprint
    result = implementation_fingerprint()
    if result['sha256'] != EXPECTED_IMPLEMENTATION or len(result['files']) != 50:
        raise ValueError('research_implementation_changed')
    return result


def record(repo, runtime, league, match_id, output):
    from football_model.feature_research import digest
    source = subprocess.check_output(['git', '-C', str(repo), 'rev-parse', 'HEAD'], text=True).strip()
    protocol = read_json(repo / 'advanced-models-v1.json')
    root = runtime / 'advanced_snapshots' / digest(protocol)
    report_path = root / 'reports' / league / 'latest.json'
    receipt = {'schema_version': 'single-league-window-capture-v1', 'observed_at': now(),
        'source_commit': source, 'run_id': os.environ.get('GITHUB_RUN_ID'),
        'run_attempt': os.environ.get('GITHUB_RUN_ATTEMPT'), 'league': league,
        'target_match_id': match_id, 'requested_horizon': 'LINEUP',
        'not_all_league_final_followup': True, 'confirmation_activated_here': False,
        'production_promoted': False, 'accuracy_improvement_verified': False,
        'implementation': check_implementation(),
        'outcomes': {k: os.environ.get(k) for k in ('RESTORE', 'FACTS', 'CAPTURE', 'CHECK', 'PERSIST')},
        'capture_ref': None, 'first_ref': None, 'saved_state_commit': None,
        'target_status': 'not_proven_captured', 'remote_durability': 'not_yet_independently_proven'}
    save_path = repo / 'window-save-run-receipt.json'
    if save_path.is_file():
        saved = read_json(save_path)
        receipt['save_receipt'] = saved
        if (saved.get('source_sha') == source and saved.get('github_run_id') == receipt['run_id']
                and saved.get('github_run_attempt') == receipt['run_attempt']
                and saved.get('child_exit_code') == 0
                and saved.get('save', {}).get('status') == 'saved_and_marker_verified'):
            receipt['saved_state_commit'] = saved['save']['reported_commit']
    if report_path.is_file() and receipt['outcomes']['CAPTURE'] not in (None, '', 'skipped'):
        report = read_json(report_path)
        receipt['capture_report'] = {'path': report_path.relative_to(runtime).as_posix(),
            'sha256': hashlib.sha256(report_path.read_bytes()).hexdigest(),
            'run_started_at': report.get('run_started_at'), 'run_completed_at': report.get('run_completed_at'),
            'status': report.get('status'), 'execution_status': report.get('execution_status')}
        if (report.get('source_commit') == source and report.get('league') == league
                and report.get('state_commit') == os.environ.get('WINDOW_PARENT_STATE')):
            rows = [row for row in report.get('queue', [])
                    if row.get('match_id') == match_id and row.get('horizon') == 'LINEUP']
            if len(rows) == 1:
                row = rows[0]
                receipt['target_status'] = row.get('status')
                receipt['target_reason'] = row.get('reason')
                receipt['queue_observed_at'] = row.get('queue_observed_at')
                receipt['kickoff'] = row.get('kickoff')
                receipt['candidate_statuses'] = row.get('candidate_statuses')
                if row.get('status') == 'captured' and isinstance(row.get('receipt_ref'), dict):
                    receipt['capture_ref'] = row['receipt_ref']
                    first = root / 'captures' / league / str(match_id) / 'LINEUP' / 'first.json'
                    if first.is_file():
                        receipt['first_ref'] = {'path': first.relative_to(runtime).as_posix(),
                            'sha256': hashlib.sha256(first.read_bytes()).hexdigest()}
                        receipt['is_original_first_output'] = read_json(first) == receipt['capture_ref']
    output.write_text(json.dumps(receipt, ensure_ascii=False, indent=2))
    state = receipt['saved_state_commit'] or ''
    with Path(os.environ['GITHUB_OUTPUT']).open('a') as out:
        out.write('saved_state=' + state + '\n')
        out.write('capture_ref=' + json.dumps(receipt['capture_ref'], separators=(',', ':')) + '\n')
        out.write('first_ref=' + json.dumps(receipt['first_ref'], separators=(',', ':')) + '\n')
    return 0


def audit(repo, state, capture_ref, first_ref, league, match_id, output):
    if not re.fullmatch('[0-9a-f]{40}', state or ''):
        raise ValueError('exact_saved_state_required')
    if not isinstance(capture_ref, dict) or not isinstance(first_ref, dict):
        raise ValueError('target_lineup_capture_not_saved')
    validate_ref(capture_ref); validate_ref(first_ref)
    from football_model.feature_research import digest
    root = 'advanced_snapshots/' + digest(read_json(repo / 'advanced-models-v1.json'))
    if (first_ref['path'] != root + '/captures/' + league + '/' + str(match_id) + '/LINEUP/first.json'
            or capture_ref['path'] != root + '/receipts/' + capture_ref['sha256'] + '.json'):
        raise ValueError('target_reference_scope_mismatch')
    check_implementation()
    from football_model.advanced_durability import prove
    from football_model.advanced_snapshots import verify_capture
    from football_model.history_archive import FixedStateReader
    subprocess.run(['git', '-C', str(repo), 'fetch', '--depth=1', '--no-tags', 'origin', state], check=True)
    fetched = subprocess.check_output(['git', '-C', str(repo), 'rev-parse', 'FETCH_HEAD'], text=True).strip()
    if fetched != state:
        raise ValueError('fixed_fetch_mismatch')
    runtime = repo / 'window-readback-runtime'
    if runtime.exists() and any(runtime.iterdir()):
        raise ValueError('readback_runtime_must_be_empty')
    runtime.mkdir(exist_ok=True)
    with FixedStateReader(repo, state) as reader:
        manifest = reader.validate_manifest()
        def read_file(name):
            name = safe_relative(name)
            raw = reader.read(name)
            path = runtime / name
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists() and path.read_bytes() != raw:
                raise ValueError('fixed_materialized_original_changed')
            path.write_bytes(raw)
            return raw
        first = json.loads(read_file(first_ref['path']))
        if hashlib.sha256((runtime / first_ref['path']).read_bytes()).hexdigest() != first_ref['sha256']:
            raise ValueError('first_reference_hash_mismatch')
        first_proof = prove(repo, runtime, state, first_ref, _read_file=read_file)
        if first != capture_ref:
            raise ValueError('current_capture_is_not_immutable_first_output')
        capture_proof = prove(repo, runtime, state, capture_ref, _read_file=read_file)
    captured = read_json(runtime / safe_relative(capture_ref['path']))
    checked = verify_capture(runtime, captured)
    fixture = checked['fixture']
    if (checked['league'] != league or fixture['match_id'] != match_id or checked['horizon'] != 'LINEUP'
            or checked.get('candidate_only') or not checked.get('baseline')):
        raise ValueError('target_live_lineup_baseline_not_verified')
    completion = capture_proof.get('durable_saved_at')
    parse = lambda value: datetime.fromisoformat(value.replace('Z', '+00:00'))
    saved_before_kickoff = bool(completion and parse(completion) < parse(fixture['kickoff']))
    result = {'schema_version': 'single-league-window-readback-v1', 'observed_at': now(),
        'state_commit': state, 'league': league, 'target_match_id': match_id,
        'horizon': 'LINEUP', 'manifest': manifest, 'native_capture_reverified': True,
        'immutable_first_output_verified': True, 'capture_proof': capture_proof, 'first_proof': first_proof,
        'actual_cutoff_at': checked['actual_cutoff_at'], 'capture_storage_completed_at': captured['storage_completed_at'],
        'durable_saved_at': completion, 'kickoff': fixture['kickoff'],
        'remote_persist_completed_before_kickoff': saved_before_kickoff,
        'candidate_statuses': {key: row.get('status') for key, row in checked['verified_candidates'].items()},
        'network_football_requests': 0, 'fits': 0, 'remote_state_writes': 0,
        'not_all_league_final_followup': True, 'production_promoted': False}
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if (first_proof['verified'] is True and capture_proof['verified'] is True
                 and first_proof['closure_verified'] is True and capture_proof['closure_verified'] is True
                 and saved_before_kickoff) else 1


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('mode', choices=('validate', 'record', 'audit'))
    p.add_argument('--repo', type=Path, default=Path.cwd())
    p.add_argument('--runtime', type=Path, default=Path('runtime'))
    p.add_argument('--output', type=Path, default=Path('window-run-receipt.json'))
    a = p.parse_args(argv)
    repo = a.repo.resolve()
    sys.path.insert(0, str(repo))
    try:
        mid = validate_inputs(os.environ.get('WINDOW_LEAGUE'), os.environ.get('WINDOW_MATCH_ID'), os.environ.get('GITHUB_REF'))
        if a.mode == 'validate':
            enabled = dispatch_enabled(os.environ.get('GITHUB_EVENT_NAME'), datetime.now(timezone.utc))
            with Path(os.environ['GITHUB_OUTPUT']).open('a') as out:
                out.write('proceed=' + str(enabled).lower() + '\n')
            print('Single-league live-window dispatch gate: ' + ('enabled' if enabled else 'skipped_outside_exact_20261017_window'))
            return 0
        if a.mode == 'record':
            return record(repo, a.runtime.resolve(), os.environ['WINDOW_LEAGUE'], mid, a.output)
        return audit(repo, os.environ.get('WINDOW_SAVED_STATE'), json.loads(os.environ.get('WINDOW_CAPTURE_REF', 'null')),
            json.loads(os.environ.get('WINDOW_FIRST_REF', 'null')), os.environ['WINDOW_LEAGUE'], mid, a.output)
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        a.output.write_text(json.dumps({'schema_version': 'single-league-window-failure-v1', 'observed_at': now(),
            'mode': a.mode, 'status': 'not_verified', 'error_category': type(exc).__name__,
            'not_all_league_final_followup': True, 'production_promoted': False}, indent=2))
        print('Single-league window step not verified; bounded failure receipt retained.')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
