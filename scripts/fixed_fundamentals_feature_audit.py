"""Bounded read-only reconstruction of one saved fundamentals feature artifact.

This is current-input engineering verification, not R fitting, an all19 research
audit, qualification of a missed window or a new live capture.
"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import signal
import sys

OLD_IMPLEMENTATION = '1e311689b3405f64c6ad9636732d2defb7ad67e26b4bc7e04b01a5b1b2b2d6ac'

class FeatureAuditError(ValueError):
    pass

class FeatureDeadline(OSError):
    pass

def require(ok, code):
    if not ok:
        raise FeatureAuditError(code)

def identity_from_context(context, match_id, cutoff):
    """Rebuild fixture identity from the saved /fixtures response, never cache latest."""
    from football_model.data import _utc, _iso, _integer
    require(type(match_id) is int and match_id > 0, 'invalid_match_id')
    env = context['fixture_response']
    require(env.get('endpoint') == '/fixtures' and env.get('params') == {'id':match_id}
        and not env.get('errors'), 'fixture_envelope_contract')
    require(env.get('paging',{}).get('current',1) == env.get('paging',{}).get('total',1) == 1,
        'fixture_pagination')
    rows = env.get('response')
    require(isinstance(rows,list) and len(rows) == 1, 'fixture_response_not_unique')
    row = rows[0]; raw = row['fixture']
    require(raw['id'] == match_id, 'fixture_id_mismatch')
    home = _integer(row['teams']['home']['id'], 'home_id', 1)
    away = _integer(row['teams']['away']['id'], 'away_id', 1)
    require(home != away, 'duplicate_team')
    kickoff, observed = _utc(raw['date']), _utc(env['fetched_at'])
    require(observed <= _utc(cutoff), 'fixture_observed_after_cutoff')
    require(_utc(context['coverage_observed_at']) <= _utc(cutoff), 'coverage_observed_after_cutoff')
    require(context['league_id'] == row['league']['id'] and context['season'] == row['league']['season'],
        'fixture_coverage_identity')
    if raw.get('timestamp') is not None:
        require(type(raw['timestamp']) is int and abs(raw['timestamp']-kickoff.timestamp()) <= 1,
            'fixture_timestamp_mismatch')
    return dict(match_id=match_id,home_id=home,away_id=away,kickoff=_iso(kickoff),
        status=raw['status']['short'],observed_at=_iso(observed))

def read_original(runtime, relative, evidence):
    from scripts.fixed_fixture_validation_audit import safe_path
    from football_model.history_archive import read_bytes
    supplied = Path(runtime)/relative
    require(not any(p.is_symlink() for p in (supplied,*supplied.parents)), 'symlink_original')
    path = safe_path(runtime,relative)
    require(len(evidence) < 2000, 'original_count_limit')
    raw = read_bytes(path,runtime=runtime)
    require(len(raw) <= 60*1024*1024, 'original_file_limit')
    require(sum(e['bytes'] for e in evidence)+len(raw) <= 256*1024*1024, 'original_total_limit')
    value = json.loads(raw,parse_constant=lambda _: (_ for _ in ()).throw(FeatureAuditError('nonfinite_json')))
    if relative.startswith(('fundamentals/features/','fundamentals/fixtures/')):
        require(hashlib.sha256(raw).hexdigest() == path.stem, 'content_address_mismatch')
    evidence.append(dict(path=relative,bytes=len(raw),sha256=hashlib.sha256(raw).hexdigest()))
    return value

def audit(runtime, match_id, progress=lambda *_args,**_kw:None):
    from football_model.fundamentals import build_features, make_snapshot
    from scripts.fixed_research_report_audit import equal
    evidence=[]
    report = read_original(runtime,'fundamentals/reports/latest.json',evidence)
    matches = [m for m in report['matches'] if m['match_id'] == match_id and m['league'] == 'epl']
    require(len(matches) == 1, 'target_not_unique')
    entry=matches[0]
    require(entry['status'] == 'features_saved', 'feature_not_saved')
    saved = read_original(runtime,entry['feature_path'],evidence)
    cutoff=saved['forecast_cutoff']
    equal(cutoff,entry['feature_cutoff'],0)
    equal(saved['fixture']['match_id'],match_id,0)
    require(saved['fundamentals_used_in_probabilities'] is False, 'unexpected_probability_use')
    provenance=saved['collection_provenance']
    equal(provenance['run_id'],report['run_id'],0)
    equal(provenance['forecast_run_id'],entry['forecast_run_id'],0)
    equal(provenance['fixture_context_paths'],entry['fixture_context_paths'],0)
    require(0 < len(entry['fixture_context_paths']) <= 101, 'context_count_limit')
    contexts={}
    for relative in entry['fixture_context_paths']:
        require(bool(re.fullmatch(r'fundamentals/fixtures/[1-9][0-9]*/[a-f0-9]{64}\.json',relative)),
            'fixture_context_path')
        mid=int(relative.split('/')[2])
        require(mid not in contexts,'duplicate_fixture_context')
        contexts[mid]=identity_from_context(read_original(runtime,relative,evidence),mid,cutoff)
    require(match_id in contexts,'target_context_missing')
    target=contexts[match_id]
    equal({k:target[k] for k in saved['fixture']},saved['fixture'],0)
    history=[v for mid,v in contexts.items() if mid != match_id]
    require(all(v['status'] in ('FT','AET','PEN') for v in history),'history_not_finished')
    snapshots=[]; normalization_count=0
    for kind in ('injuries','lineups','statistics'):
        for mid in sorted(contexts):
            root=runtime/'fundamentals'/'snapshots'/kind/str(mid)
            paths=sorted(root.glob('*.json'))
            require(len(paths) <= 200, 'fixture_snapshot_count_limit')
            for path in paths:
                snap=read_original(runtime,path.relative_to(runtime).as_posix(),evidence)
                require(snap['kind'] == kind and snap['fixture']['match_id'] == mid,'snapshot_path_identity')
                equal(snap['snapshot_id'],path.stem,0)
                equal({k:v for k,v in snap['fixture'].items() if k != 'status'},
                    {k:contexts[mid][k] for k in snap['fixture'] if k != 'status'},0)
                rebuilt=make_snapshot(kind,dict(endpoint=snap['endpoint'],params=snap['params'],
                    response=snap['raw_response'],fetched_at=snap['observed_at']),snap['fixture'],
                    context_observed_at=snap['context_observed_at'],published_at=snap['published_at'])
                equal(rebuilt,snap,0)
                snapshots.append(rebuilt);normalization_count+=1
    known_paths={e['path'] for e in evidence}
    require(set(entry['snapshot_paths']) <= known_paths, 'reported_snapshot_not_consumed')
    progress('native_feature_reconstruction',snapshots=normalization_count,contexts=len(contexts))
    rebuilt=build_features(target,cutoff=cutoff,snapshots=snapshots,fixture_history=history,
        recent_matches=saved['recent_matches'])
    # Provenance is an immutable collection annotation, not a computed feature.
    equal(rebuilt,{k:v for k,v in saved.items() if k != 'collection_provenance'},1e-12)
    # Separate arithmetic from raw statistics and fixture timestamps. No stored
    # normalized xG value is used in this second calculation.
    from football_model.data import _utc
    independent={}
    for tid in (target['home_id'],target['away_id']):
        prior=sorted([v for v in history if tid in (v['home_id'],v['away_id'])],
            key=lambda x:(x['kickoff'],x['match_id']),reverse=True)[:saved['recent_matches']]
        pairs=[]
        for item in prior:
            eligible=[s for s in snapshots if s['kind']=='statistics' and s['fixture']['match_id']==item['match_id']
                and _utc(s['known_at']) <= _utc(cutoff)]
            if not eligible:continue
            snap=max(eligible,key=lambda x:(x['known_at'],x['snapshot_id']))
            xg={}
            for team in snap['raw_response']:
                values=[v['value'] for v in team['statistics'] if v['type']=='expected_goals']
                require(len(values) <= 1,'duplicate_raw_xg')
                if values and values[0] is not None:xg[team['team']['id']]=float(values[0])
            other=item['away_id'] if item['home_id']==tid else item['home_id']
            if tid in xg and other in xg:pairs.append((xg[tid],xg[other]))
        own=sum(a for a,b in pairs)/len(pairs) if pairs else None
        against=sum(b for a,b in pairs)/len(pairs) if pairs else None
        cell=saved['teams'][str(tid)]
        equal(own,cell['prior_xg']['for_mean']);equal(against,cell['prior_xg']['against_mean'])
        equal(len(pairs),cell['prior_xg']['paired_sample_count'],0)
        rest=(_utc(target['kickoff'])-_utc(prior[0]['kickoff'])).total_seconds()/86400 if prior else None
        equal(rest,cell['rest_days']['value'])
        independent[str(tid)]=dict(raw_prior_xg_pairs=len(pairs),for_mean=own,against_mean=against,rest_days=rest)
    return dict(status='completed_with_stated_scope',match_id=match_id,feature_cutoff=cutoff,
        normalized_raw_snapshots=normalization_count,fixture_contexts=len(contexts),
        source_originals=evidence,independent_raw_arithmetic=independent,
        live_lineup_available=all(saved['teams'][str(t)]['lineups'].get('complete_starting_eleven')
            for t in (target['home_id'],target['away_id'])))

def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo',type=Path,required=True)
    parser.add_argument('--state-commit',required=True)
    parser.add_argument('--match-id',type=int,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args(argv)
    repo=args.repo.resolve();sys.path.insert(0,str(repo));sys.path.insert(0,str(repo/'scripts'))
    from scripts.fixed_fixture_validation_audit import sha40,restore_fixed
    from scripts.fixed_research_report_audit import checkpoint_output,validate_output,failure_detail
    import cloud_state as cloud
    from football_model.advanced_research import implementation_fingerprint
    state=sha40(args.state_commit);require(args.match_id > 0,'invalid_match_id')
    output=validate_output(args.output,repo)
    runtime=repo/'fixed-fundamentals-audit-runtime'
    require(not output.is_relative_to(runtime),'protected_output_path')
    out=dict(schema_version='fixed-fundamentals-feature-audit-v1',state_commit=state,match_id=args.match_id,
        started_at=datetime.now(timezone.utc).isoformat(),network_football_requests=0,fits=0,
        remote_state_writes=0,live_capture_or_first_output_created=False,status='audit_in_progress',
        limitations=['Single current-input engineering artifact, not full historical advanced-feature rebuild.',
            'Original saved observations and cutoff are unchanged; no missed EARLY or future LINEUP qualification.',
            'No independent R fitting, all19 current-input verification, probability promotion or final short acceptance.'])
    def progress(stage,**counts):
        out['active_stage']=dict(stage=stage,at=datetime.now(timezone.utc).isoformat(),**counts)
        checkpoint_output(output,out);print(json.dumps(dict(audit_progress=out['active_stage'])),flush=True)
    previous=signal.signal(signal.SIGALRM,lambda *_: (_ for _ in ()).throw(FeatureDeadline('bounded_deadline')))
    signal.alarm(1800)
    try:
        require(implementation_fingerprint()['sha256']==OLD_IMPLEMENTATION,'implementation_changed')
        out['auditor_source_commit']=cloud._git(repo,'rev-parse','HEAD').stdout.strip()
        progress('restore_exact_fixed_state')
        out['restore_preflight']=restore_fixed(repo,runtime,state,cloud)
        progress('read_saved_raw_sources')
        out['result']=audit(runtime,args.match_id,progress)
        out['status']='completed_with_stated_scope'
    except Exception as exc:
        out.update(status='audit_incomplete',**failure_detail(exc),failed_stage=out.get('active_stage',{}).get('stage'))
    finally:
        signal.alarm(0);signal.signal(signal.SIGALRM,previous)
        out['ended_at']=datetime.now(timezone.utc).isoformat();checkpoint_output(output,out)
    print(json.dumps(out,ensure_ascii=False,allow_nan=False),flush=True)
    return 0 if out['status']=='completed_with_stated_scope' else 1

if __name__=='__main__':raise SystemExit(main())

