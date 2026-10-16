"""Run the existing save once and retain its actual validation/save evidence.

No cached estimate authorizes a save. The child still executes prepare,
preflight, complete pack/tree validation, bounded Git pack and ordinary CAS push.
"""
from datetime import datetime, timezone
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys


def timestamp():
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


def save_once(command, *, receipt_path, before_marker, after_marker, source_sha,
              runtime_path, run_id, run_attempt):
    """after_marker is a callable, so marker is checked after the child exits."""
    receipt_path = Path(receipt_path)
    receipt_path.unlink(missing_ok=True)
    receipt = {'schema_version': 'cloud-state-save-receipt-v1',
               'started_at': timestamp(), 'source_sha': source_sha,
               'github_run_id': run_id, 'github_run_attempt': run_attempt,
               'runtime_path': str(Path(runtime_path).resolve()),
               'parent_state_oid': before_marker.get('state_oid'),
               'runtime_validation': {'status': 'not_proven'},
               'resource_preflight': {'status': 'not_proven'},
               'save': {'status': 'not_proven'}, 'pack_probe': {'status': 'not_proven'}}
    preflight, saved, pack_bytes, malformed = None, None, None, False
    child = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, bufsize=1, env={**os.environ, 'PYTHONUNBUFFERED': '1'})
    for line in child.stdout:
        print(line, end='', flush=True)
        if line.startswith('MODEL_STATE_SAVE_PREFLIGHT='):
            try:
                if preflight is not None:
                    raise ValueError('duplicate preflight')
                preflight = json.loads(line.split('=', 1)[1])
                if (preflight['state_oid'] != before_marker.get('state_oid')
                        or type(preflight['can_save']) is not bool
                        or before_marker.get('source_sha') != source_sha
                        or preflight['runtime_logical_bytes'] < 0
                        or preflight['available_disk_bytes'] < 0):
                    raise ValueError('unbound preflight')
            except (ValueError, KeyError, TypeError):
                malformed = True
        elif line.startswith('MODEL_STATE_SAVED_COMMIT='):
            candidate = line.split('=', 1)[1].strip()
            if saved is not None or not re.fullmatch('[0-9a-f]{40}(?:[0-9a-f]{24})?', candidate):
                malformed = True
            saved = candidate
        elif line.startswith('MODEL_STATE_NEW_PACK_BYTES='):
            try:
                pack_bytes = int(line.split('=', 1)[1])
                if pack_bytes < 0:
                    raise ValueError('negative pack')
            except ValueError:
                malformed = True
    code = child.wait(); child.stdout.close()
    marker = after_marker()
    receipt.update(ended_at=timestamp(), child_exit_code=code,
                   post_state_oid=marker.get('state_oid'))
    if preflight is not None and not malformed:
        receipt['runtime_validation'] = {'status': 'passed', 'evidence': 'same_child_MODEL_STATE_SAVE_PREFLIGHT',
                                         'meaning': 'save internal _validate_runtime completed; not a separate check invocation'}
        receipt['resource_preflight'] = {'status': 'passed' if preflight['can_save'] else 'insufficient_resource',
                                         'report': preflight}
    if pack_bytes is not None and not malformed:
        receipt['pack_probe'] = {'status': 'passed', 'actual_new_pack_bytes': pack_bytes}
    saved_ok = (code == 0 and preflight is not None and preflight.get('can_save') is True
                and saved is not None and marker.get('state_oid') == saved
                and marker.get('source_sha') == source_sha and pack_bytes is not None and not malformed)
    receipt['save'] = {'status': 'saved_and_marker_verified' if saved_ok else 'not_proven_saved',
                       'reported_commit': saved, 'marker_matches_reported_commit': saved is not None and marker.get('state_oid') == saved}
    receipt['receipt_parse_status'] = 'invalid' if malformed else 'valid'
    receipt_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2))
    # Preserve every child failure. A successful child with missing evidence is
    # a receipt verification failure; its possible remote change stays visible.
    return code if code else (0 if saved_ok else 1), receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', type=Path, default=Path.cwd())
    parser.add_argument('--runtime', type=Path, default=Path('runtime'))
    parser.add_argument('--receipt', type=Path, default=Path('advanced-state-save-run-receipt.json'))
    parser.add_argument('--preflight-report', type=Path)
    args = parser.parse_args()
    repo = args.repo.resolve(); runtime = args.runtime.resolve()
    gitdir = Path(subprocess.check_output(['git', '-C', str(repo), 'rev-parse', '--absolute-git-dir'], text=True).strip())
    source = subprocess.check_output(['git', '-C', str(repo), 'rev-parse', 'HEAD'], text=True).strip()
    def marker():
        try:
            return json.loads((gitdir / 'football-cloud-state.json').read_text())
        except (OSError, ValueError):
            return {}
    if args.preflight_report:
        args.preflight_report.unlink(missing_ok=True)
    code, receipt = save_once([sys.executable, str(repo / 'scripts/cloud_state.py'), 'save', '--repo', str(repo), '--runtime', str(runtime)],
        receipt_path=args.receipt, before_marker=marker(), after_marker=marker, source_sha=source,
        runtime_path=runtime, run_id=os.environ.get('GITHUB_RUN_ID'), run_attempt=os.environ.get('GITHUB_RUN_ATTEMPT'))
    if args.preflight_report and 'report' in receipt['resource_preflight']:
        args.preflight_report.write_text(json.dumps(receipt['resource_preflight']['report'], ensure_ascii=False, sort_keys=True))
    return code


if __name__ == '__main__':
    raise SystemExit(main())
