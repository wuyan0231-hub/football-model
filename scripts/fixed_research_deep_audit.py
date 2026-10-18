"""One fixed historical raw-feature/latest-R reproducibility pilot; never save state.

The runner restores bytes in a separate process. This program imports numerical
code only from the report's old source checkout and creates new audit artifacts.
It neither claims historical PIT nor complete rejected-source discovery.
"""
from __future__ import annotations
import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import signal
import stat
import subprocess
import sys
import time

STATE = '49d58e87e4a4324039034f521ad7cba1a27349b0'
SOURCE = '4ae6764f2502c8892bb892c3a08bf0a34d604b1a'
IMPLEMENTATION = '1e311689b3405f64c6ad9636732d2defb7ad67e26b4bc7e04b01a5b1b2b2d6ac'
REPORT_SHA = 'fd7f48f457f79e6ac0ba1bb04ba600be280331be176c012bedac40aaf3b32a28'
REPORT_REF = {'path': 'advanced_models/primeira/18a3498b0d24276904c54fe8c063ff7dbf1ce24d300c6cb6cf8632206e0fd448/reports/history/' + REPORT_SHA + '.json', 'sha256': REPORT_SHA}
CANDIDATES = ('G0_full', 'G0', 'G1')
LIMITS = {'worker_seconds': 2040, 'outer_worker_seconds':2090, 'outer_worker_kill_grace_seconds':10,
          'restore_seconds':590, 'restore_kill_grace_seconds':10,'work_stages_hard_total_seconds':2700,
          'raw_seconds': 1200, 'original_bytes': 60*1024**2,
          'input_bytes': 1024**3, 'input_files': 6000, 'summary_bytes': 512*1024,
          'expanded_report_bytes': 512*1024**2, 'address_space_bytes': 2*1024**3,
          'result_bytes': 64*1024**2, 'R_per_fit_seconds': 180, 'R_processes': 3}
TOLERANCES = {'raw_feature_atol': 1e-12, 'refit_parameter_atol': 1e-8,
              'refit_parameter_rtol': 1e-8, 'refit_loglikelihood_per_game_atol': 1e-10,
              'refit_prediction_atol': 1e-10, 'native_saved_coefficient_probe_atol': 1e-10}
LIMITATIONS = [
    'Single fixed primeira report; latest3 real R fits, not all19 or historical block refits.',
    'No correction inner-selection, correction-model, or baseline optimizer reruns.',
    'Rebuild all admitted numeric feature rows; rejected/unselected discovery universe is not fully frozen.',
    'Native raw-to-feature functions rerun independently; no separate second feature-formula implementation is claimed.',
    '2GiB RLIMIT_AS applies separately per process, including inherited R child limits; it is not an aggregate job RSS limit.',
    '2700-second hard budget covers restore and numerical stages including kill grace; environment setup/artifact upload are separate.',
    'Historical downloaded sources and assumed availability remain historical development, never observed live PIT.',
    'Synthetic R diagnostic probes are not match forecasts or future confirmation samples.',
    'No API collection, production state write, current-card relabelling, promotion, or R08 activation.'
]
THRESHOLD_ORIGIN = 'Pre-registered conservative audit thresholds; not production gates or optimizer error bounds.'

class DeepAuditError(ValueError):
    pass

class DeepAuditDeadline(BaseException):
    pass


def require(ok, code):
    if not ok:
        raise DeepAuditError(code)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def encode(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)+'\n').encode()


def bounded_expanded_size(value):
    size=0
    for piece in json.JSONEncoder(ensure_ascii=False,allow_nan=False).iterencode(value):
        size+=len(piece.encode())
        require(size<=LIMITS['expanded_report_bytes'],'expanded_report_limit')
    return size


def save(path, value, limit=LIMITS['result_bytes']):
    raw = encode(value)
    require(len(raw) <= limit, 'audit_output_limit')
    temporary = path.with_suffix('.tmp')
    with temporary.open('wb') as handle:
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)
    return {'path': path.name, 'bytes': len(raw), 'sha256': digest(raw)}


def safe_path(root, relative):
    require(isinstance(relative,str) and bool(relative) and '\\' not in relative
            and not any(ord(c)<32 for c in relative), 'unsafe_path')
    p = PurePosixPath(relative)
    require(not p.is_absolute() and all(x not in ('','.','..') for x in relative.split('/')), 'unsafe_path')
    root = Path(root).resolve()
    out = root.joinpath(*p.parts)
    require(all(not parent.is_symlink() for parent in (out, *out.parents)), 'symlink_path')
    require(out.resolve().is_relative_to(root), 'unsafe_path')
    return out


def compare(actual, expected, *, atol=0.0, rtol=0.0):
    """Strict keys/shapes/nulls/bools; numeric leaves have preregistered bounds."""
    metrics = {'numeric_leaves': 0, 'max_absolute_difference': 0.0, 'max_relative_difference': 0.0,
               'mismatches': 0, 'first_mismatch': None}
    def bad(path):
        metrics['mismatches'] += 1
        if metrics['first_mismatch'] is None:
            metrics['first_mismatch'] = path
    def walk(a,b,path):
        if isinstance(b,(bool,str)) or b is None:
            if type(a) is not type(b) or a != b: bad(path)
        elif type(b) in (int,float):
            if type(a) not in (int,float) or not math.isfinite(a) or not math.isfinite(b):
                bad(path); return
            delta = abs(a-b); rel = delta/max(abs(b),1e-300)
            metrics['numeric_leaves'] += 1
            metrics['max_absolute_difference'] = max(metrics['max_absolute_difference'],delta)
            metrics['max_relative_difference'] = max(metrics['max_relative_difference'],min(rel,1e300))
            if delta > atol+rtol*abs(b): bad(path)
        elif isinstance(b,dict):
            if not isinstance(a,dict) or set(a) != set(b): bad(path); return
            for k in b: walk(a[k],b[k],path+'/'+str(k))
        elif isinstance(b,list):
            if not isinstance(a,list) or len(a)!=len(b): bad(path); return
            for i,(x,y) in enumerate(zip(a,b)): walk(x,y,path+'/'+str(i))
        else: bad(path)
    walk(actual,expected,'')
    metrics['passed'] = metrics['mismatches']==0
    return metrics


def exact(actual, expected, code):
    require(compare(actual, expected)['passed'], code)


def root_fd(path):
    path=Path(path)
    require(not any(p.is_symlink() for p in (path,*path.parents)),'symlink_path')
    return os.open(path,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)


def verify_root_binding(descriptor,path):
    require(not any(p.is_symlink() for p in (path,*path.parents)),'symlink_path')
    observed=os.stat(path,follow_symlinks=False);held=os.fstat(descriptor)
    require(stat.S_ISDIR(observed.st_mode) and
        (observed.st_dev,observed.st_ino)==(held.st_dev,held.st_ino),'materialized_root_changed')


def anchored_create(root_descriptor,relative,raw):
    """Never follow target directories, including swaps after source reads."""
    parts=PurePosixPath(relative).parts
    descriptor=os.dup(root_descriptor)
    try:
        for name in parts[:-1]:
            try:os.mkdir(name,dir_fd=descriptor)
            except FileExistsError:pass
            child=os.open(name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=descriptor)
            os.close(descriptor);descriptor=child
        target=os.open(parts[-1],os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=descriptor)
        with os.fdopen(target,'wb') as handle:handle.write(raw)
    finally:os.close(descriptor)


def materialize_inputs(report, runtime, destination, reader):
    require(not destination.exists(), 'materialized_root_exists')
    require(not destination.resolve().is_relative_to(runtime.resolve())
            and not runtime.resolve().is_relative_to(destination.resolve()), 'source_destination_overlap')
    refs = report['source_refs']
    require(isinstance(refs,list) and 0<len(refs)<=LIMITS['input_files'], 'source_count_limit')
    destination.mkdir(parents=True)
    by_original = {}; manifest=[]; total=0
    descriptor=root_fd(destination)
    try:
        for ref in refs:
            relative = ref['original_path']
            safe_path(destination, relative)
            original = safe_path(runtime,ref.get('runtime_relative_path',ref['path']))
            require(isinstance(ref['sha256'],str) and len(ref['sha256'])==64, 'source_sha_invalid')
            require(relative not in by_original or by_original[relative]==ref['sha256'], 'conflicting_original_path')
            raw = reader(original, runtime=runtime)
            require(len(raw)<=LIMITS['original_bytes'], 'source_file_limit')
            require(digest(raw)==ref['sha256'], 'source_hash_mismatch')
            if relative in by_original:
                continue
            total += len(raw); require(total<=LIMITS['input_bytes'], 'source_total_limit')
            anchored_create(descriptor,relative,raw)
            by_original[relative]=ref['sha256']
            manifest.append({'original_path':relative,'frozen_path':ref['path'],'sha256':ref['sha256'],'bytes':len(raw)})
        verify_root_binding(descriptor,destination)
    finally:os.close(descriptor)
    return manifest


def stable_input_tree(destination,manifest):
    expected = {item['original_path']:item['sha256'] for item in manifest}
    actual={}
    def walk(descriptor,prefix):
        for name in os.listdir(descriptor):
            info=os.stat(name,dir_fd=descriptor,follow_symlinks=False)
            require(not stat.S_ISLNK(info.st_mode),'symlink_path')
            relative=prefix+name
            if stat.S_ISDIR(info.st_mode):
                child=os.open(name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=descriptor)
                try:walk(child,relative+'/')
                finally:os.close(child)
            else:
                require(stat.S_ISREG(info.st_mode),'unsupported_materialized_member')
                require(relative in expected,'unexpected_materialized_original')
                child=os.open(name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=descriptor)
                with os.fdopen(child,'rb') as handle:
                    current=os.fstat(handle.fileno())
                    require(stat.S_ISREG(current.st_mode) and
                        (current.st_dev,current.st_ino)==(info.st_dev,info.st_ino),'materialized_original_changed')
                    raw=handle.read(LIMITS['original_bytes']+1)
                    require(len(raw)<=LIMITS['original_bytes'],'source_file_limit')
                actual[relative]=digest(raw)
    descriptor=root_fd(destination)
    try:
        walk(descriptor,'')
        verify_root_binding(descriptor,destination)
    finally:os.close(descriptor)
    exact(actual,expected,'materialized_input_changed')


def reconstruct_features(report, root):
    from football_model.advanced_features import build_feature_rows
    from football_model.feature_dataset import feature_values
    from football_model.advanced_research import _rebind_frozen_refs
    from football_model.data import _utc, _iso
    data=json.loads((root/'data/primeira.json').read_bytes())
    started=_utc(report['run_started_at'])
    matches=[m for m in data['matches'] if _utc(m['kickoff'])+timedelta(hours=4)<=started]
    exact(len(matches),report['requested_matches'],'requested_match_count_mismatch')
    exact([{'match_id':m['match_id'],'reason':'result_not_mature_at_study_start'} for m in data['matches'] if m not in matches],report['unavailable_targets'],'unavailable_targets_mismatch')
    schedule_path=root/'feature_models/schedules/primeira/latest.json'
    schedule_events=None
    if schedule_path.exists():
        schedule=json.loads(schedule_path.read_bytes())
        if schedule.get('coverage',{}).get('provider_queries_complete'):
            schedule_events=schedule['coverage']['workload_events']
    protocol=report['protocol']; sets={}; receipts={}
    lookup={(r['original_path'],r['sha256']):r for r in report['source_refs']}
    # These two fields describe rejected/unselected sources, explicitly not a
    # complete frozen raw dependency universe in the original report contract.
    diagnostic_fields={'source_issues','optional_diagnostic_refs'}
    for horizon in ('EARLY','LINEUP'):
        hours=protocol['horizon_hours'] if horizon=='EARLY' else protocol.get('lineup_horizon_hours',.75)
        rebuilt=build_feature_rows(root,'primeira',matches=matches,horizon=horizon,horizon_hours=hours,
            mode='historical',recent_matches=protocol['advanced_feature_recent_matches'],
            min_history=protocol['advanced_feature_min_history'],
            national_load_lookback_days=protocol.get('national_load_lookback_days',14))
        rows={r['match_id']:r for r in rebuilt['rows']}
        require(len(rows)==len(rebuilt['rows']),'duplicate_rebuilt_feature_target')
        for m in matches:
            cutoff=_iso(_utc(m['kickoff'])-timedelta(hours=hours))
            extra=feature_values(m,matches,cutoff,schedule_events=schedule_events)
            row=rows.setdefault(m['match_id'],{'match_id':m['match_id'],'home':{},'away':{}})
            for side in ('home','away'):row.setdefault(side,{}).update(extra[side])
        rebound=_rebind_frozen_refs(rebuilt,lookup)
        expected=report['feature_datasets'][horizon]
        exact({k:v for k,v in rebound.items() if k!='rows'},
              {k:v for k,v in expected.items() if k!='rows'},'feature_dataset_metadata_mismatch')
        projected=lambda rows:[{k:v for k,v in row.items() if k not in diagnostic_fields} for row in rows]
        result=compare(projected(rebound['rows']),projected(expected['rows']),atol=TOLERANCES['raw_feature_atol'])
        require(result['passed'],'raw_feature_numeric_or_admitted_provenance_mismatch')
        diag=compare([{k:r.get(k) for k in diagnostic_fields} for r in rebound['rows']],
                     [{k:r.get(k) for k in diagnostic_fields} for r in expected['rows']])
        receipts[horizon]={'requested':len(matches),'rows':len(rebuilt['rows']),
            'failed':len(rebuilt['failures']),'numeric_and_admitted_provenance':result,
            'diagnostic_fields_equal':diag['passed'],'complete_original_discovery_universe_verified':False,
            'raw_rebuilt_rows_sha256':digest(encode(projected(rebound['rows'])))}
        sets[horizon]=rows
    return matches,sets,receipts


def request_identity(train,probes,names,features,probe_features,cutoff):
    from football_model import goalmodel_adapter as adapter
    from football_model.model import _validated_matches,_timestamp,_iso,RESULT_AVAILABILITY_DELAY,TAIL_TOLERANCE
    dt=_timestamp(cutoff,'cutoff')
    records=[r for r in _validated_matches(train) if r['_dt']+RESULT_AVAILABILITY_DELAY<=dt]
    fixtures=adapter._fixtures(probes,dt)
    request={'schema_version':1,'source_commit':adapter.GOALMODEL_SOURCE_COMMIT,
        'upstream_version':adapter.GOALMODEL_UPSTREAM_VERSION,'cutoff':_iso(dt),
        'training_matches':[{k:v for k,v in r.items() if k!='_dt'} for r in records],
        'fixtures':fixtures,'training_weights':[math.exp(-math.log(2)*(dt-r['_dt']).total_seconds()/(86400*180.0)) for r in records],
        'feature_names':names,'training_features':adapter._features(records,names,features,'training_features'),
        'prediction_features':adapter._features(fixtures,names,probe_features,'prediction_features'),
        'dc':False,'tail_tolerance':TAIL_TOLERANCE}
    return digest(json.dumps(request,sort_keys=True,separators=(',',':'),allow_nan=False).encode())


def prepare_refits(report,runtime,matches,features):
    from football_model.advanced_storage import load_artifact
    from football_model.advanced_research import _goal_pool,_side_values,candidate_feature_names,_validate_card_identity
    from football_model.feature_research import digest as value_digest
    plans=[]
    for candidate in CANDIDATES:
        card=load_artifact(runtime,report['model_cards'][candidate])
        _validate_card_identity(card,candidate,'primeira',report['protocol'])
        exact(card['implementation_fingerprint'],report['implementation_fingerprint'],'card_implementation_mismatch')
        exact(card['source_commit'],SOURCE,'card_source_mismatch')
        bundle=load_artifact(runtime,card['goalmodel_training_ref'])
        original=bundle['real_R_result']
        joint=candidate_feature_names(report['protocol'],'G1')
        train=_goal_pool(matches,features,[] if candidate=='G0_full' else joint,card['training_cutoff'])
        names=joint if candidate=='G1' else []
        train_features={m['match_id']:_side_values(features[m['match_id']],names) for m in train} if names else {}
        # JSON keys are strings; normalize only map key representation, never values.
        exact(train,bundle['matches'],'raw_rebuilt_R_training_matches_mismatch')
        exact(value_digest(train),card['training_data_sha256'],'R_training_digest_mismatch')
        exact(names,bundle['feature_names'],'R_feature_names_mismatch')
        exact({str(k):v for k,v in train_features.items()},bundle['training_features'],'raw_rebuilt_R_training_features_mismatch')
        exact(card['goalmodel_fit'],original['fit'],'saved_R_fit_mismatch')
        exact(card['training_cutoff'],bundle['cutoff'],'R_cutoff_mismatch')
        require(original.get('status')=='ok' and not original.get('errors'),'original_latest_R_not_successful')
        probes=[{k:p[k] for k in ('match_id','home_id','away_id','kickoff','neutral')} for p in original['forecasts']]
        require(len(probes)==6,'unexpected_probe_count')
        # Reconstruct probe features from the last admitted real inputs; original
        # synthetic identities are retained, never their expected probabilities.
        expected_features={p['match_id']:_side_values(features[m['match_id']],names)
            for p,m in zip(probes,train[-6:])} if names else {}
        exact({str(k):v for k,v in expected_features.items()},bundle['probe_features'],'raw_rebuilt_probe_features_mismatch')
        request_sha=request_identity(train,probes,names,train_features or None,expected_features or None,card['training_cutoff'])
        exact(request_sha,original['request_sha256'],'original_R_request_mismatch')
        plans.append({'candidate':candidate,'card':card,'original':original,'train':train,'probes':probes,
            'names':names,'training_features':train_features,'probe_features':expected_features,'request_sha256':request_sha})
    exact(plans[1]['train'],plans[2]['train'],'G0_G1_training_pool_mismatch')
    return plans


def compare_refit(actual,original):
    metadata=('status','version','source_commit','source_sha256','branch','cutoff','training_count',
              'excluded_training_count','training_match_ids','half_life_days','feature_names','training_weights','dc','request_sha256')
    exact({k:actual[k] for k in metadata},{k:original[k] for k in metadata},'R_refit_identity_mismatch')
    exact(actual['errors'],original['errors'],'R_refit_error_set_mismatch')
    for k in ('converged','ngames','fitter','regularization','home_field_method'):
        exact(actual['fit'][k],original['fit'][k],'R_refit_fit_contract_mismatch')
    params=compare(actual['fit']['parameters'],original['fit']['parameters'],
        atol=TOLERANCES['refit_parameter_atol'],rtol=TOLERANCES['refit_parameter_rtol'])
    ll=abs(actual['fit']['loglikelihood']-original['fit']['loglikelihood'])/max(1,original['fit']['ngames'])
    selected=('match_id','home_id','away_id','kickoff','neutral','expected_goals','score_matrix',
              'score_matrix_axes','tail_mass_before_normalization','probabilities','probabilities_1x2')
    predictions=compare([{k:r[k] for k in selected} for r in actual['forecasts']],
        [{k:r[k] for k in selected} for r in original['forecasts']],atol=TOLERANCES['refit_prediction_atol'])
    return {'parameters':params,'loglikelihood_difference_per_game':ll,
        'loglikelihood_passed':ll<=TOLERANCES['refit_loglikelihood_per_game_atol'],
        'predictions':predictions,'r_version_original':original.get('r_version'),'r_version_refit':actual.get('r_version'),
        'same_R_version':original.get('r_version')==actual.get('r_version'),
        'passed':params['passed'] and predictions['passed'] and ll<=TOLERANCES['refit_loglikelihood_per_game_atol']}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--numeric-repo',type=Path,required=True)
    parser.add_argument('--runtime',type=Path,required=True)
    parser.add_argument('--restore-receipt',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args(argv)
    numeric=args.numeric_repo.resolve();runtime=args.runtime.resolve();output=args.output.resolve()
    require(not output.exists(),'audit_output_must_be_new')
    require(not output.is_relative_to(numeric) and not output.is_relative_to(runtime)
            and not runtime.is_relative_to(output),'protected_output_path')
    output.mkdir(parents=True)
    out={'schema_version':'fixed-research-deep-audit-v1','status':'audit_in_progress',
         'started_at':datetime.now(timezone.utc).isoformat(),'state_commit':STATE,'research_source_commit':SOURCE,
         'implementation_fingerprint':IMPLEMENTATION,'report_ref':REPORT_REF,'league':'primeira',
         'tolerances':TOLERANCES,'threshold_origin':THRESHOLD_ORIGIN,
         'limits':LIMITS,'limitations':LIMITATIONS,'football_api_requests':0,
         'remote_state_writes':0,'real_R_processes_started':0,'real_R_refits_completed':0,'results':[]}
    out['R_attempts']=[]
    out['real_R_process_count_complete']=True
    active_R=None
    began=time.monotonic()
    def checkpoint(stage):
        out['stage']=stage;out['elapsed_seconds']=round(time.monotonic()-began,3)
        save(output/'audit-summary.json',out,LIMITS['summary_bytes'])
        print(json.dumps({'audit_stage':stage,'elapsed_seconds':out['elapsed_seconds']}),flush=True)
    previous=signal.signal(signal.SIGALRM,lambda *_: (_ for _ in ()).throw(DeepAuditDeadline()))
    signal.alarm(LIMITS['worker_seconds'])
    try:
        checkpoint('identity_preflight')
        import resource
        current,hard=resource.getrlimit(resource.RLIMIT_AS)
        memory=LIMITS['address_space_bytes']
        if hard!=resource.RLIM_INFINITY:memory=min(memory,hard)
        resource.setrlimit(resource.RLIMIT_AS,(memory,hard))
        out['enforced_address_space_bytes']=memory
        receipt_raw=args.restore_receipt.read_bytes();receipt=json.loads(receipt_raw)
        exact(receipt['state_commit'],STATE,'restore_state_mismatch')
        require(receipt['status']=='restored_exact_state' and receipt['save_marker_created'] is False,'restore_not_readonly')
        exact(Path(receipt['runtime']).resolve().as_posix(),runtime.as_posix(),'restored_runtime_mismatch')
        out['restore_receipt_sha256']=digest(receipt_raw)
        actual_source=subprocess.run(['git','rev-parse','HEAD'],cwd=numeric,text=True,capture_output=True,check=True).stdout.strip()
        exact(actual_source,SOURCE,'numeric_source_commit_mismatch')
        require(not any(name=='football_model' or name.startswith('football_model.') for name in sys.modules),'numerical_modules_preloaded')
        sys.path.insert(0,str(numeric))
        from football_model.advanced_research import implementation_fingerprint,verify_goalmodel_probes
        from football_model.advanced_storage import load_artifact
        from football_model.history_archive import read_bytes
        from football_model.goalmodel_adapter import fit_predict_goalmodel,RSCRIPT_TIMEOUT_SECONDS
        from football_model.data import APIClient
        def forbidden_api(*_a,**_kw):raise DeepAuditError('API_client_forbidden')
        APIClient.__init__=forbidden_api
        exact(implementation_fingerprint()['sha256'],IMPLEMENTATION,'numeric_implementation_changed')
        exact(RSCRIPT_TIMEOUT_SECONDS,LIMITS['R_per_fit_seconds'],'R_timeout_changed')
        import football_model.advanced_features as numerical_module
        require(Path(numerical_module.__file__).resolve().is_relative_to(numeric),'numeric_import_wrong_checkout')
        out['auditor_script_sha256']=digest(Path(__file__).read_bytes())
        out['protocol_before_fits']=save(output/'audit-preregistered-policy.json',{'tolerances':TOLERANCES,'threshold_origin':THRESHOLD_ORIGIN,'limits':LIMITS,'state':STATE,'source':SOURCE,'implementation':IMPLEMENTATION,'report':REPORT_REF,'candidates':CANDIDATES,'limitations':LIMITATIONS})
        checkpoint('expand_fixed_report')
        report=load_artifact(runtime,REPORT_REF)
        out['expanded_report_bytes']=bounded_expanded_size(report)
        exact(report['source_commit'],SOURCE,'report_source_mismatch')
        exact(report['implementation_fingerprint'],implementation_fingerprint(),'report_implementation_mismatch')
        exact(report['league'],'primeira','report_league_mismatch')
        from football_model.feature_research import digest as value_digest
        exact(value_digest(report['protocol']),report['protocol_hash'],'protocol_hash_mismatch')
        checkpoint('materialize_exact_frozen_sources')
        materialized=output/'frozen-inputs'
        manifest=materialize_inputs(report,runtime,materialized,read_bytes)
        out['input_manifest']=save(output/'audit-input-manifest.json',manifest)
        out['input_count']=len(manifest);out['input_bytes']=sum(m['bytes'] for m in manifest)
        checkpoint('rebuild_complete_admitted_feature_values')
        matches,sets,out['feature_reconstruction']=reconstruct_features(report,materialized)
        require(time.monotonic()-began<LIMITS['raw_seconds'],'raw_stage_deadline')
        checkpoint('preflight_all_three_R_requests')
        plans=prepare_refits(report,runtime,matches,sets['EARLY'])
        out['R_request_preflight']=[{'candidate':p['candidate'],'request_sha256':p['request_sha256'],
            'training_matches':len(p['train']),'probe_count':len(p['probes'])} for p in plans]
        stable_input_tree(materialized,manifest)
        checkpoint('fresh_R_processes')
        for plan in plans:
            active_R=plan['candidate']
            out['R_attempts'].append({'candidate':active_R,'status':'adapter_call_entered',
                'at':datetime.now(timezone.utc).isoformat()})
            out['real_R_process_count_complete']=False
            checkpoint('fresh_R_'+plan['candidate'])
            actual=fit_predict_goalmodel(plan['train'],plan['probes'],cutoff=plan['card']['training_cutoff'],
                feature_names=plan['names'],training_features=plan['training_features'] or None,
                prediction_features=plan['probe_features'] or None,repo_root=numeric)
            out['real_R_processes_started']+=int(bool(actual.get('r_started_at')))
            out['R_attempts'][-1]['status']='adapter_returned'
            out['R_attempts'][-1]['R_process_started']=bool(actual.get('r_started_at'))
            out['real_R_process_count_complete']=True
            active_R=None
            output_ref=save(output/('audit-R-refit-'+plan['candidate']+'.json'),actual)
            record={'candidate':plan['candidate'],'new_R_result':output_ref,'status':actual.get('status'),
                'r_started_at':actual.get('r_started_at'),'r_finished_at':actual.get('r_finished_at'),
                'r_exit_code':actual.get('r_exit_code'),'request_sha256':actual.get('request_sha256')}
            out['results'].append(record)
            if actual.get('status')!='ok':
                record['passed']=False;continue
            out['real_R_refits_completed']+=1
            native_card={**plan['card'],'goalmodel_fit':actual['fit']}
            record['new_fit_native_probe']=verify_goalmodel_probes(native_card,actual,plan['probe_features'])
            record['comparison']=compare_refit(actual,plan['original'])
            record['passed']=record['comparison']['passed']
        stable_input_tree(materialized,manifest)
        out['status']='completed_with_stated_scope' if len(out['results'])==3 and all(r['passed'] for r in out['results']) else 'audit_incomplete'
    except (Exception,DeepAuditDeadline) as exc:
        if active_R is not None:
            out['known_R_process_starts_lower_bound']=out['real_R_processes_started']
            out['real_R_processes_started']=None
            out['interrupted_R_candidate']=active_R
            out['real_R_process_count_complete']=False
        out.update(status='audit_incomplete',exception_class=type(exc).__name__,
            error_code=str(exc) if isinstance(exc,DeepAuditError) else 'bounded_deadline' if isinstance(exc,DeepAuditDeadline) else 'unclassified_audit_error')
    finally:
        signal.alarm(0);signal.signal(signal.SIGALRM,previous)
        out['ended_at']=datetime.now(timezone.utc).isoformat()
        checkpoint(out.get('stage','unknown'))
    print(json.dumps(out,ensure_ascii=False,allow_nan=False),flush=True)
    return 0 if out['status']=='completed_with_stated_scope' else 1

if __name__=='__main__':raise SystemExit(main())
