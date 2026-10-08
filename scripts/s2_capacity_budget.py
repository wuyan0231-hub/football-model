"""Read-only capacity scenarios; no training, collection or state mutation.

Scenarios are stress calculations, never an estimate of identified daily growth.
"""
from __future__ import annotations

import argparse
from datetime import datetime
import json
import math
from pathlib import Path
import re

HARD_PHYSICAL = 500 * 1024 * 1024
HARD_LOGICAL = 2 * 1024 * 1024 * 1024
WRITER_LIMIT = 400 * 1024 * 1024
SHA = re.compile('[0-9a-f]{64}')


def integer(value, label, *, positive=False):
    if type(value) is not int or value < (1 if positive else 0):
        raise ValueError(label + '_invalid_integer')
    return value


def timestamp(value):
    dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if dt.tzinfo is None:
        raise ValueError('snapshot_timezone_required')
    return dt


def snapshot(value):
    if value.get('scope') != 'full_fixed_saved_tree':
        raise ValueError('full_saved_tree_snapshot_required')
    timestamp(value['saved_at'])
    for k in ('physical_bytes', 'logical_bytes'):
        integer(value[k], k)
    if 'estimated_writer_persisted_bytes' in value:
        integer(value['estimated_writer_persisted_bytes'], 'estimated_writer_persisted_bytes')
    return value


def manifest_entries(value):
    entries, seen, blob_sizes = {}, set(), {}
    if value.get('codec') != 'gzip-single-member' or value.get('schema_version') != 1:
        raise ValueError('unknown_storage_manifest')
    for e in value['entries']:
        path = e['path']
        if (not isinstance(path, str) or not path or path in seen or '\\' in path
                or path.startswith('/') or any(x in ('', '.', '..') for x in path.split('/'))):
            raise ValueError('manifest_path_invalid_or_duplicate')
        seen.add(path)
        if any(not SHA.fullmatch(e[k]) for k in ('raw_sha256', 'blob_sha256')):
            raise ValueError('manifest_hash_invalid')
        for k in ('raw_bytes', 'compressed_bytes'):
            integer(e[k], k)
        if e['blob_sha256'] in blob_sizes and blob_sizes[e['blob_sha256']] != e['compressed_bytes']:
            raise ValueError('blob_size_conflict')
        blob_sizes[e['blob_sha256']] = e['compressed_bytes']
        entries[path] = e
    return entries


def archive_increment(before, after):
    old, new = manifest_entries(before), manifest_entries(after)
    for path, value in old.items():
        if new.get(path) != value:
            raise ValueError('old_archive_path_missing_or_changed')
    old_blobs = {e['blob_sha256']: e['compressed_bytes'] for e in old.values()}
    new_blobs = {}
    added = [e for path, e in new.items() if path not in old]
    for e in added:
        key = e['blob_sha256']
        sizes = old_blobs if key in old_blobs else new_blobs
        if key in sizes and sizes[key] != e['compressed_bytes']:
            raise ValueError('blob_size_conflict')
        if key not in old_blobs:
            new_blobs[key] = e['compressed_bytes']
    return {'added_paths': len(added), 'added_raw_bytes': sum(e['raw_bytes'] for e in added),
            'new_unique_compressed_blobs': len(new_blobs),
            'new_unique_compressed_bytes': sum(new_blobs.values()),
            'reused_old_compressed_paths': sum(e['blob_sha256'] in old_blobs for e in added),
            'same_old_blob_referenced_bytes': sum(e['compressed_bytes'] for e in added if e['blob_sha256'] in old_blobs),
            'old_paths_unchanged': True}


def analyze(before, after, *, opportunities, goal_opportunities=9000, goal_days=365,
            version_multipliers=(1,7), daily_opportunities=(0,25,185), daily_snapshots=(),
            manifest_before=None, manifest_after=None):
    before, after = snapshot(before), snapshot(after)
    integer(opportunities, 'opportunities', positive=True)
    integer(goal_opportunities, 'goal_opportunities', positive=True)
    integer(goal_days, 'goal_days', positive=True)
    if timestamp(after['saved_at']) <= timestamp(before['saved_at']):
        raise ValueError('snapshot_order_invalid')
    growth = {k: after[k] - before[k] for k in ('physical_bytes', 'logical_bytes')}
    if any(x < 0 for x in growth.values()):
        raise ValueError('negative_increment_requires_separate_migration_audit')
    writer_after = after.get('estimated_writer_persisted_bytes')
    writer_growth = (writer_after - before['estimated_writer_persisted_bytes']
                     if writer_after is not None and 'estimated_writer_persisted_bytes' in before else None)
    if writer_growth is not None and writer_growth < 0:
        raise ValueError('negative_writer_increment')
    increments = None
    if (manifest_before is None) != (manifest_after is None):
        raise ValueError('both_storage_manifests_required')
    if manifest_before is not None:
        increments = archive_increment(manifest_before, manifest_after)
    daily = sorted((snapshot(v) for v in daily_snapshots), key=lambda v: timestamp(v['saved_at']))
    unique_days = {timestamp(v['saved_at']).date() for v in daily if v.get('daily_observation') is True}
    empirical = []
    # A snapshot is a declared daily observation, not any convenient historical snapshot.
    for left, right in zip(daily, daily[1:]):
        elapsed = (timestamp(right['saved_at']) - timestamp(left['saved_at'])).total_seconds()/86400
        if elapsed <= 0 or not left.get('daily_observation') or not right.get('daily_observation'):
            continue
        empirical.append({'elapsed_days':elapsed,
                          'physical_net_bytes_per_day':(right['physical_bytes']-left['physical_bytes'])/elapsed,
                          'logical_net_bytes_per_day':(right['logical_bytes']-left['logical_bytes'])/elapsed})
    daily_rate_identified = len(unique_days) >= 14 and len(empirical) >= 13
    headroom = {'hard_physical_bytes': HARD_PHYSICAL-after['physical_bytes'],
                'hard_logical_bytes': HARD_LOGICAL-after['logical_bytes'],
                'writer_estimated_bytes': WRITER_LIMIT-writer_after if writer_after is not None else None}
    scenarios=[]
    for mult in version_multipliers:
        integer(mult,'version_multiplier',positive=True)
        scale=goal_opportunities*mult/opportunities
        physical=math.ceil(after['physical_bytes']+growth['physical_bytes']*scale)
        logical=math.ceil(after['logical_bytes']+growth['logical_bytes']*scale)
        writer=math.ceil(writer_after+writer_growth*scale) if writer_growth is not None else None
        scenarios.append({'assumed_versions_per_opportunity':mult,'goal_future_opportunities':goal_opportunities,
                          'pilot_equivalent_batches':scale,'projected_physical_bytes':physical,
                          'projected_logical_bytes':logical,'projected_writer_estimated_bytes':writer,
                          'within_hard_physical':physical<=HARD_PHYSICAL,'within_hard_logical':logical<=HARD_LOGICAL,
                          'within_writer_limit':writer<=WRITER_LIMIT if writer is not None else None,
                          'semantics':'conditional_linear_stress_scenario_not_forecast_or_upper_bound',
                          'excludes':['new_regular_production_growth','result_and_revision_growth','mature_selection_pool_growth','future_changed_object_sizes','git_history_and_clone_bytes']})
    day_scenarios=[]
    for n in daily_opportunities:
        integer(n,'daily_opportunities')
        scale=n*goal_days/opportunities
        day_scenarios.append({'assumed_pilot_equivalent_forecast_versions_per_day':n,'days':goal_days,
                              'conditional_physical_growth_bytes':math.ceil(growth['physical_bytes']*scale),
                              'conditional_logical_growth_bytes':math.ceil(growth['logical_bytes']*scale),
                              'semantics':'assumed_intensity_not_measured_daily_rate'})
    current_under_hard = headroom['hard_physical_bytes']>=0 and headroom['hard_logical_bytes']>=0
    next_writer = (writer_after+writer_growth<=WRITER_LIMIT if writer_growth is not None else None)
    return {'schema':'s2_readonly_capacity_budget_v1','mutates_state':False,'opportunity_unit':'one_declared_independent_match_opportunity; version_multipliers model repeat receipts',
            'observed_pilot_opportunities':opportunities,'before':before,'after':after,
            'observed_operation_growth':growth,'observed_writer_estimated_growth_bytes':writer_growth,
            'archive_increment':increments,'headroom':headroom,
            'current_storage_gate':{'passed':current_under_hard,'scope':'current_saved_content_only'},
            'next_same_sized_operation_gate':{'passed':current_under_hard and next_writer is True
                        and growth['physical_bytes']<=headroom['hard_physical_bytes'] and growth['logical_bytes']<=headroom['hard_logical_bytes'],
                    'writer_check':next_writer,'scope':'same_size_conditional; all writers retain native append-time guards'},
            'daily_observation':{'declared_daily_days':len(unique_days),'daily_rate_identified':daily_rate_identified,
                    'empirical_intervals':empirical,'true_future_minimum_or_maximum_rate_known':False,
                    'note':'Net observed intervals, even if available, are not a proven future upper bound.'},
            'goal_scenarios':scenarios,'assumed_daily_scenarios':day_scenarios,
            'long_term_gate':{'passed':False,'reason_codes':['external_durable_archive_and_selective_restore_not_deployed',
                     'yearly_load_not_proven']+([] if daily_rate_identified else ['insufficient_true_daily_observations']),
                    'schema_or_offline_roundtrip_is_not_production_acceptance':True},
            'limits':{'hard_physical_bytes':HARD_PHYSICAL,'hard_logical_bytes':HARD_LOGICAL,'conservative_writer_bytes':WRITER_LIMIT}}


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--before',type=Path,required=True);p.add_argument('--after',type=Path,required=True)
    p.add_argument('--opportunities',type=int,required=True)
    p.add_argument('--goal-opportunities',type=int,default=9000);p.add_argument('--goal-days',type=int,default=365)
    p.add_argument('--manifest-before',type=Path);p.add_argument('--manifest-after',type=Path)
    p.add_argument('--daily-snapshot',type=Path,action='append',default=[])
    args=p.parse_args(argv)
    try:
        read=lambda path:json.loads(path.read_text(encoding='utf-8'))
        report=analyze(read(args.before),read(args.after),opportunities=args.opportunities,
                    goal_opportunities=args.goal_opportunities,goal_days=args.goal_days,
                    daily_snapshots=[read(path) for path in args.daily_snapshot],
                    manifest_before=read(args.manifest_before) if args.manifest_before else None,
                    manifest_after=read(args.manifest_after) if args.manifest_after else None)
        print(json.dumps(report,ensure_ascii=False,indent=2));return 0
    except (ValueError,KeyError,TypeError,OSError) as exc:
        print(json.dumps({'error':str(exc),'mutates_state':False},ensure_ascii=False));return 2


if __name__=='__main__':raise SystemExit(main())
