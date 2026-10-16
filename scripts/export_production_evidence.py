"""Bounded inspection bundle for this run's successfully saved state.

This is not the original evidence closure. Runs without a verified save, or
with a failed thin export, keep the existing convenience subset.
The existing complete state save is unchanged.
"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tarfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from football_model.state_storage import _scan_secret


def _git(repo, *args):
    return subprocess.check_output(['git', '-C', str(repo), *args], stderr=subprocess.DEVNULL)


def _time(value):
    result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if result.tzinfo is None:
        raise ValueError('artifact_time_requires_timezone')
    return result.astimezone(timezone.utc)


def _plain(path):
    path = Path(path).absolute()
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError('artifact_symlink_rejected')
    return path


def _saved_bytes(repo, state, path):
    from football_model.history_archive import read_state_bytes
    return read_state_bytes(repo, state, path)


def export(*, repo, runtime, context_path, summary_path, output, persist_outcome,
           source_sha, run_id, run_attempt, now=None, max_bytes=64 * 1024 * 1024,
           state_reader=_saved_bytes):
    """The marker alone proves only load; fixed same-run metadata proves save."""
    repo, runtime, context_path, summary_path, output = map(_plain, (repo, runtime, context_path, summary_path, output))
    if persist_outcome != 'success':
        raise ValueError('persist_not_success_keep_original_convenience_subset')
    if not (re.fullmatch('[0-9a-f]{40}', source_sha or '') and re.fullmatch(r'\d+', str(run_id or ''))
            and re.fullmatch(r'\d+', str(run_attempt or '')) and 0 < max_bytes <= 64 * 1024 * 1024):
        raise ValueError('artifact_run_identity_invalid')
    if _git(repo, 'rev-parse', 'HEAD').decode().strip() != source_sha:
        raise ValueError('artifact_source_changed')
    if context_path.stat().st_size > max_bytes:
        raise ValueError('thin_artifact_raw_limit_exceeded_keep_original_subset')
    context_raw = context_path.read_bytes(); _scan_secret(context_raw); context = json.loads(context_raw)
    if context['source_commit'] != source_sha:
        raise ValueError('context_source_mismatch')
    gitdir = Path(_git(repo, 'rev-parse', '--absolute-git-dir').decode().strip())
    marker = json.loads(_plain(gitdir / 'football-cloud-state.json').read_bytes())
    state = marker['state_oid']
    if marker.get('schema_version') != 1 or marker.get('source_sha') != source_sha or not re.fullmatch('[0-9a-f]{40}', str(state)):
        raise ValueError('saved_marker_invalid')
    # Bound before reading; no checkout, archive restore, remote ref mutation,
    # API/data collection, fit or new state-save operation is performed.
    metadata_size = int(_git(repo, 'cat-file', '-s', state + ':state_metadata.json'))
    if not 0 < metadata_size <= 1024 * 1024:
        raise ValueError('saved_metadata_size_invalid')
    metadata_raw = _git(repo, 'show', state + ':state_metadata.json'); _scan_secret(metadata_raw); metadata = json.loads(metadata_raw)
    now = now or datetime.now(timezone.utc)
    if (metadata.get('schema_version') != 1 or metadata.get('source_sha') != source_sha or str(metadata.get('github_run_id')) != str(run_id)
            or str(metadata.get('github_run_attempt')) != str(run_attempt)
            or not _time(context['started_at']) <= _time(metadata['saved_at']) <= now):
        raise ValueError('marker_not_a_save_from_this_run_attempt')
    entries, bodies, missing, raw_total = [], [], [], 0
    def add(name, raw, *, origin, role, state_path=None):
        nonlocal raw_total
        if len(raw) > max_bytes - raw_total:
            raise ValueError('thin_artifact_raw_limit_exceeded_keep_original_subset')
        _scan_secret(raw)
        entries.append({'path': name, 'raw_sha256': hashlib.sha256(raw).hexdigest(), 'raw_bytes': len(raw),
                        'origin': origin, 'role': role, 'state_runtime_path': state_path})
        bodies.append((name, raw)); raw_total += len(raw)
    add('saved-state-metadata.json', metadata_raw, origin='exact_saved_commit', role='actual_same_run_save_binding')
    add('football-run-context.json', context_raw, origin='current_job_after_save', role='run_context_not_claimed_saved_in_state')
    if summary_path.is_file():
        if summary_path.stat().st_size > max_bytes - raw_total:
            raise ValueError('thin_artifact_raw_limit_exceeded_keep_original_subset')
        add('football-run-summary.json', summary_path.read_bytes(), origin='current_job_after_save',
            role='layered_summary_not_claimed_saved_in_state')
    else:
        missing.append({'path': 'football-run-summary.json', 'reason': 'summary_not_created; original independent summary outcome unchanged'})
    selected = [('reports/latest.json', 'saved_report_snapshot'), ('reports/latest.md', 'saved_report_snapshot'),
                ('integration/index.json', 'saved_routing_index_not_all_entries_new_this_run'),
                ('integration/f5c-index.json', 'saved_routing_index_not_all_entries_new_this_run')]
    report_id = None
    for relative, role in selected:
        local = _plain(runtime / relative)
        if not local.is_file():
            missing.append({'path': relative, 'reason': 'not_present_in_saved_job_runtime'})
            continue
        if local.stat().st_size > max_bytes - raw_total:
            raise ValueError('thin_artifact_raw_limit_exceeded_keep_original_subset')
        raw = state_reader(repo, state, relative)
        if raw != local.read_bytes():
            raise ValueError('runtime_changed_after_save_keep_original_subset')
        if relative == 'reports/latest.json':
            report = json.loads(raw)
            if (report.get('source_commit') != source_sha or not _time(context['started_at']) <= _time(report['generated_at']) <= _time(metadata['saved_at'])):
                raise ValueError('latest_report_not_this_run')
            candidate = report.get('run_id')
            if isinstance(candidate, str) and re.fullmatch('[A-Za-z0-9_-]+', candidate):
                report_id = candidate
        add('runtime/' + relative, raw, origin='exact_saved_commit', role=role, state_path=relative)
    if report_id is not None:
        relative = 'reports/history/' + report_id + '.json'
        local = _plain(runtime / relative)
        if local.is_file():
            if local.stat().st_size > max_bytes - raw_total:
                raise ValueError('thin_artifact_raw_limit_exceeded_keep_original_subset')
            raw = state_reader(repo, state, relative)
            if raw != local.read_bytes():
                raise ValueError('runtime_changed_after_save_keep_original_subset')
            add('runtime/' + relative, raw, origin='exact_saved_commit', role='current_report_history', state_path=relative)
        else:
            missing.append({'path': relative, 'reason': 'saved_history_not_locally_materialized; use fixed state reader'})
    manifest = {'schema_version': 'production-thin-report-v1', 'bundle_kind': 'thin_run_inspection',
                'self_contained_original_closure': False, 'all_cache_or_runtime_included': False,
                'source_commit': source_sha, 'saved_state_commit': state, 'github_run_id': str(run_id),
                'github_run_attempt': str(run_attempt), 'saved_at': metadata['saved_at'],
                'save_binding': 'actual_persist_step_success+updated_marker+immutable_same_run_state_metadata',
                'independent_remote_head_probe_performed': False,
                'entries': entries, 'missing_or_omitted': missing,
                'omitted_originals': 'predictions, models, registry originals, fundamentals, ledgers, replay and cache not copied in full; retained in exact saved Git state',
                'original_convenience_subset_was_also_not_full_runtime_or_cache_closure': True,
                'read_route': {'repository': os.environ.get('GITHUB_REPOSITORY', 'wuyan0231-hub/football-model'),
                               'commit': state, 'storage_manifest': 'state_storage.json',
                               'history_catalog': 'runtime/history_archive/catalog.json',
                               'reader': 'football_model.history_archive.read_state_bytes(repo, saved_state_commit, runtime_relative_path)',
                               'instructions': 'Read exact source docs/model-access.md. Verify raw/transport hashes and fixed history ancestor routes. This bundle alone cannot rerun the model.'},
                'raw_payload_bytes_excluding_manifest': raw_total, 'max_raw_bytes_including_manifest': max_bytes}
    manifest_raw = json.dumps(manifest, ensure_ascii=False, indent=2).encode(); _scan_secret(manifest_raw)
    if raw_total + len(manifest_raw) > max_bytes:
        raise ValueError('thin_artifact_raw_limit_exceeded_keep_original_subset')
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.name + '.tmp')
    try:
        with tarfile.open(temporary, 'w:gz', compresslevel=6) as archive:
            for name, raw in [*bodies, ('production-evidence-manifest.json', manifest_raw)]:
                info = tarfile.TarInfo(name); info.size = len(raw); info.mode = 0o600
                archive.addfile(info, io.BytesIO(raw))
        temporary.replace(output)
    finally:
        temporary.unlink(missing_ok=True)
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    output.with_suffix('.sha256').write_text(digest + '  ' + output.name + '\n')
    return {'bundle_kind': 'thin_run_inspection', 'archive': str(output), 'bytes': output.stat().st_size,
            'saved_state_commit': state, 'self_contained_original_closure': False,
            'raw_bytes_including_manifest': raw_total + len(manifest_raw), 'raw_limit_bytes': max_bytes,
            'sha256': digest}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', type=Path, default=Path.cwd())
    parser.add_argument('--runtime', type=Path, default=Path('runtime'))
    parser.add_argument('--context', type=Path, required=True)
    parser.add_argument('--summary', type=Path, required=True)
    parser.add_argument('--output', type=Path, default=Path('football-production-report.tar.gz'))
    args = parser.parse_args()
    value = export(repo=args.repo, runtime=args.runtime, context_path=args.context, summary_path=args.summary,
                   output=args.output, persist_outcome=os.environ.get('PERSIST_OUTCOME'),
                   source_sha=os.environ.get('GITHUB_SHA'), run_id=os.environ.get('GITHUB_RUN_ID'),
                   run_attempt=os.environ.get('GITHUB_RUN_ATTEMPT'))
    print(json.dumps(value, ensure_ascii=False))
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        with Path(os.environ['GITHUB_STEP_SUMMARY']).open('a') as handle:
            handle.write('\n## 便捷工件\n\n```json\n' + json.dumps(value, ensure_ascii=False, indent=2) +
                         '\n```\n\n仅供本轮检查；单场完整原件按固定状态提交通过原接入工具读取。\n')


if __name__ == '__main__':
    main()
