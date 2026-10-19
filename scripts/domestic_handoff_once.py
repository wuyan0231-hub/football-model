"""Fixed domestic handoff commands and bounded, typed receipts; no fitting.

The workflow supplies no command or league inputs. Existing current protocols
retain their identities; an execution receipt never grants old samples new
qualification. Thin output contains routes and counts, never raw inputs.
"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from football_model.feature_research import digest

LEAGUES = ('epl','championship','laliga','bundesliga','serie_a','ligue_1',
    'bundesliga_2','ligue_2','allsvenskan','eredivisie','primeira','brazil_a',
    'j1','k1','mls','liga_mx','segunda','argentina_a')
PROTOCOL = 'advanced-models-domestic-v1.json'
CONFIG = 'handoff-domestic-config.json'
OUT = Path('handoff-output')
LIMITS = {'durable':600,'facts':600,'confirmation':600,'capture':600,
    'legacy_capture_v1':600,'legacy_capture_v2':600,'scoring':600,
    'legacy_scoring_v1':600,'legacy_scoring_v2':600,'check':900,'persist':1800}
FIELDS = {'schema_version','league','status','execution_status','run_id',
    'source_commit','state_commit','run_started_at','started_at','generated_at',
    'run_completed_at','completed_at','api_requests','planned_horizon_count',
    'planned_match_count','requested_horizon_records','scored_horizon_records',
    'confirmation_active','production_promotion','accuracy_improvement_verified'}

def timestamp(): return datetime.now(timezone.utc).isoformat()
def read(path): return json.loads(Path(path).read_bytes())
def write(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False)+'\n')
def sha(path):
    with Path(path).open('rb') as handle:return hashlib.file_digest(handle,'sha256').hexdigest()

def report_paths():
    ph=digest(read(PROTOCOL))
    result={'facts':[Path('runtime/fundamentals/reports/latest.json')]}
    result['confirmation']=[Path('runtime/advanced_models')/key/ph/'confirmation/latest.json' for key in LEAGUES]
    result['capture']=[Path('runtime/advanced_snapshots')/ph/'reports'/key/'latest.json' for key in LEAGUES]
    result['scoring']=[Path('runtime/advanced_scoring')/ph/'reports'/key/'latest.json' for key in LEAGUES]
    for version,filename in (('v1','feature-models.json'),('v2','feature-models-v2.json')):
        protocol_hash=digest(read(filename))
        result['legacy_capture_'+version]=[Path('runtime/feature_snapshots')/protocol_hash/'reports/latest.json']
        result['legacy_scoring_'+version]=[Path('runtime/feature_models/scoring')/protocol_hash/'latest.json']
    return result

def activation_paths():
    result={}
    for key in LEAGUES:
        for path in (Path('runtime/advanced_models')/key).glob('*/confirmation/*/*/*/activation.json'):
            if path.stat().st_size>64*1024:raise ValueError('activation_pointer_budget')
            result[path.as_posix()]=sha(path)
    return result

def prepare():
    config=read('config.json')
    active=[c['key'] for c in config['competitions'] if c.get('mode')!='audit_only']
    if active!=list(LEAGUES):raise ValueError('fixed_domestic_18_scope_required')
    # UCL is audit-only in the repository config. Explicitly exclude it from
    # this job's collection/capture/scoring scope without editing that config.
    projected={**config,'competitions':[c for c in config['competitions'] if c['key'] in LEAGUES]}
    write(CONFIG,projected)
    source=os.environ.get('GITHUB_SHA','')
    if not re.fullmatch('[0-9a-f]{40}',source):raise ValueError('exact_source_required')
    run_id,attempt=(os.environ.get(key,'') for key in ('GITHUB_RUN_ID','GITHUB_RUN_ATTEMPT'))
    if not all(re.fullmatch('[1-9][0-9]*',value) for value in (run_id,attempt)):
        raise ValueError('actual_run_and_attempt_required')
    gitdir=Path(subprocess.check_output(['git','rev-parse','--absolute-git-dir'],text=True).strip())
    marker=read(gitdir/'football-cloud-state.json')
    if marker.get('source_sha')!=source or not re.fullmatch('[0-9a-f]{40}',str(marker.get('state_oid',''))):
        raise ValueError('actual_loaded_parent_required')
    value={'schema_version':'domestic-handoff-preparation-v1','source_commit':source,
        'github_run_id':run_id,'github_run_attempt':attempt,
        'parent_state_commit':marker['state_oid'],'loaded_at':marker.get('loaded_at'),
        'prepared_at':timestamp(),'leagues':list(LEAGUES),'protocol_raw_sha256':sha(PROTOCOL),
        'configured_scope_file':CONFIG,'configured_scope_sha256':sha(CONFIG),
        'activation_pointers_before':activation_paths(),
        'reports_before':{p.as_posix():sha(p) if p.is_file() and p.stat().st_size<=2*1024*1024 else None
            for paths in report_paths().values() for p in paths}}
    write(OUT/'preparation.json',value)
    return 0

def command(stage,prepared):
    source,parent=prepared['source_commit'],prepared['parent_state_commit']
    base=[sys.executable,'-m']
    commands={
        'durable':base+['football_model.advanced_durability','--runtime','runtime','--state-commit',parent],
        'facts':base+['football_model.collect_fundamentals','--runtime','runtime','--config',CONFIG,'--league','all','--horizon-hours','11','--recent-matches','5'],
        'confirmation':base+['football_model.advanced_confirmation','--runtime','runtime','--league','all','--advanced-protocol',PROTOCOL,'--protocol','advanced-confirmation-v1.json'],
        'capture':base+['football_model.advanced_snapshots','capture','--runtime','runtime','--config',CONFIG,'--league','all','--protocol',PROTOCOL,'--source-commit',source,'--state-commit',parent],
        'scoring':base+['football_model.advanced_scoring','score','--runtime','runtime','--config',CONFIG,'--league','all','--protocol',PROTOCOL,'--source-commit',source],
        'check':[sys.executable,'scripts/cloud_state.py','check','--runtime','runtime'],
        'persist':[sys.executable,'scripts/cloud_state_save_once.py','--runtime','runtime','--receipt','handoff-state-save-run-receipt.json']}
    for version,filename in (('v1','feature-models.json'),('v2','feature-models-v2.json')):
        commands['legacy_capture_'+version]=base+['football_model.feature_snapshots','capture','--runtime','runtime','--config',CONFIG,'--league','serie_a','--source-commit',source,'--state-commit',parent,'--protocol',filename]
        commands['legacy_scoring_'+version]=base+['football_model.feature_scoring','score','--runtime','runtime','--config',CONFIG,'--league','serie_a','--source-commit',source,'--protocol',filename]
    return commands[stage]

def stage_run(stage):
    prepared=read(OUT/'preparation.json')
    if (prepared['source_commit']!=os.environ.get('GITHUB_SHA')
            or sha(CONFIG)!=prepared['configured_scope_sha256']
            or sha(PROTOCOL)!=prepared['protocol_raw_sha256']):
        raise ValueError('prepared_handoff_source_or_scope_changed')
    args=command(stage,prepared)
    result={'stage':stage,'started_at':timestamp(),'limit_seconds':LIMITS[stage],
        'command':args,'source_commit':prepared['source_commit'],
        'github_run_id':prepared['github_run_id'],'github_run_attempt':prepared['github_run_attempt'],
        'parent_state_commit':prepared['parent_state_commit'],'exit_code':None,'status':'not_completed'}
    child=None
    try:
        if stage=='persist' and activation_paths()!=prepared['activation_pointers_before']:
            result.update(exit_code=1,status='activation_guard_rejected')
            return 1
        child=subprocess.Popen(args,start_new_session=True)
        try:code=child.wait(timeout=LIMITS[stage])
        except subprocess.TimeoutExpired:
            os.killpg(child.pid,signal.SIGTERM)
            try:child.wait(timeout=10)
            except subprocess.TimeoutExpired:os.killpg(child.pid,signal.SIGKILL);child.wait()
            code=124;result['deadline_exceeded']=True
        result.update(exit_code=code,status='completed' if code==0 else 'failed')
        return code
    except OSError:
        result.update(exit_code=1,status='startup_failed');return 1
    finally:
        result['completed_at']=timestamp();write(OUT/('stage-'+stage+'.json'),result)

def verified_save(save,prepared):
    """Accept only the complete native save_once receipt for this exact run."""
    if not isinstance(save,dict):return False
    actual=save.get('save',{})
    runtime=save.get('runtime_validation',{})
    resource=save.get('resource_preflight',{})
    pack=save.get('pack_probe',{})
    if not all(isinstance(value,dict) for value in (actual,runtime,resource,pack)):return False
    preflight=resource.get('report',{})
    if not isinstance(preflight,dict):return False
    commit=actual.get('reported_commit')
    return (save.get('schema_version')=='cloud-state-save-receipt-v1'
        and isinstance(commit,str) and re.fullmatch('[0-9a-f]{40}(?:[0-9a-f]{24})?',commit) is not None
        and save.get('post_state_oid')==commit
        and save.get('parent_state_oid')==prepared['parent_state_commit']
        and save.get('source_sha')==prepared['source_commit']
        and save.get('github_run_id')==prepared['github_run_id']
        and save.get('github_run_attempt')==prepared['github_run_attempt']
        and type(save.get('child_exit_code')) is int and save['child_exit_code']==0
        and save.get('receipt_parse_status')=='valid'
        and runtime.get('status')=='passed'
        and runtime.get('evidence')=='same_child_MODEL_STATE_SAVE_PREFLIGHT'
        and resource.get('status')=='passed' and preflight.get('can_save') is True
        and preflight.get('state_oid')==prepared['parent_state_commit']
        and pack.get('status')=='passed' and type(pack.get('actual_new_pack_bytes')) is int
        and pack['actual_new_pack_bytes']>=0
        and actual.get('status')=='saved_and_marker_verified'
        and actual.get('marker_matches_reported_commit') is True)

def finalize():
    prepared=read(OUT/'preparation.json')
    steps={key:read(OUT/('stage-'+key+'.json')) if (OUT/('stage-'+key+'.json')).is_file()
        else {'stage':key,'status':'not_executed'} for key in LIMITS}
    routes=[]
    for stage,paths in report_paths().items():
        for path in paths:
            row={'stage':stage,'path':path.as_posix(),'exists':path.is_file(),
                'current_run_verified':False,'original_included':False}
            if path.is_file():
                row.update(bytes=path.stat().st_size,sha256=None)
                if path.stat().st_size<=2*1024*1024:
                    try:
                        row['sha256']=sha(path)
                        row['changed_since_restore']=row['sha256']!=prepared['reports_before'].get(path.as_posix())
                        body=read(path)
                        row['typed_summary']={key:value for key,value in body.items() if key in FIELDS
                            and (value is None or type(value) in (bool,int,float) or
                                 isinstance(value,str) and len(value)<=160)}
                        when=body.get('run_started_at',body.get('started_at',body.get('generated_at')))
                        window=steps[stage]
                        if stage=='confirmation':expected_league=path.parts[2]
                        elif stage in ('capture','scoring'):expected_league=path.parts[4]
                        else:expected_league='serie_a' if stage.startswith('legacy_') else None
                        identity_ok=(expected_league is None or body.get('league')==expected_league)
                        row['current_run_verified']=(row['changed_since_restore'] and
                            identity_ok and
                            window.get('status') in ('completed','failed') and isinstance(when,str) and
                            datetime.fromisoformat(window['started_at'])<=datetime.fromisoformat(when.replace('Z','+00:00'))<=datetime.fromisoformat(window['completed_at']) and
                            (stage=='facts' or body.get('source_commit')==prepared['source_commit'] or
                             stage=='confirmation' and body.get('protocol_sha256')==digest(read(PROTOCOL))))
                    except (ValueError,TypeError,AttributeError):row['summary_status']='unreadable_or_unbound'
                else:row['summary_status']='over_small_report_budget_route_only'
            routes.append(row)
    save_path=Path('handoff-state-save-run-receipt.json');save={};save_ref=None
    if save_path.is_file() and save_path.stat().st_size<=1024*1024:
        # Preserve the complete native receipt, byte exact, for independent
        # readback. Its bounded content is produced by save_once, not raw inputs.
        raw=save_path.read_bytes();copied=OUT/save_path.name;copied.write_bytes(raw)
        save_ref={'path':copied.name,'bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest()}
        try:save=json.loads(raw)
        except (ValueError,UnicodeDecodeError):save={}
    actual=save.get('save',{}) if isinstance(save,dict) else {}
    if not isinstance(actual,dict):actual={}
    run_bound=(prepared['source_commit']==os.environ.get('GITHUB_SHA')
        and prepared['github_run_id']==os.environ.get('GITHUB_RUN_ID')
        and prepared['github_run_attempt']==os.environ.get('GITHUB_RUN_ATTEMPT'))
    stages_ok=all(row.get('stage')==key and row.get('status')=='completed'
        and type(row.get('exit_code')) is int and row['exit_code']==0
        and row.get('source_commit')==prepared['source_commit']
        and row.get('parent_state_commit')==prepared['parent_state_commit']
        and row.get('github_run_id')==prepared['github_run_id']
        and row.get('github_run_attempt')==prepared['github_run_attempt'] for key,row in steps.items())
    save_ok=(steps['persist'].get('status')=='completed' and run_bound and verified_save(save,prepared))
    no_activation_change=activation_paths()==prepared['activation_pointers_before']
    reports_current=all(row['current_run_verified'] for row in routes)
    success=stages_ok and save_ok and no_activation_change and reports_current
    receipt={'schema_version':'domestic-handoff-once-v1','source_commit':prepared['source_commit'],
        'parent_state_commit':prepared['parent_state_commit'],'saved_state_commit':actual.get('reported_commit') if save_ok else None,
        'saved_and_marker_verified':save_ok,'leagues':list(LEAGUES),'steps':steps,
        'native_save_receipt':save_ref,'stage_receipts_bound_to_run':stages_ok,
        'all_expected_latest_reports_current_run_bound':reports_current,
        'activation_pointers_unchanged':no_activation_change,'activation_requested':False,
        'domestic_all_protocol_activation_pointers_unchanged':no_activation_change,
        'activation_pointer_scope':'18 domestic leagues, every advanced protocol namespace',
        'job_hard_limit_seconds':6600,'sum_stage_limit_seconds':sum(LIMITS.values()),
        'hard_timeout_receipt_not_guaranteed':True,
        'new_fits':0,'production_promoted':False,'accuracy_improvement_verified':False,
        'all18_current_research_verified':False,'historical_samples_requalified':False,
        'scope':'One actual current domestic short cycle; independent original v1/v2 Serie A namespaces retained.',
        'status':'completed_with_stated_scope' if success else 'incomplete_or_failed',
        'self_contained_original_closure':False,'completed_at':timestamp()}
    write(OUT/'handoff-receipt.json',receipt)
    write(OUT/'report-routes.json',{'schema_version':'domestic-handoff-typed-report-routes-v1',
        'saved_state_commit':receipt['saved_state_commit'],'routes':routes,
        'original_read_route':'Fix saved_state_commit in model-state; verify transport manifest and original path/hash. Thin summaries are not raw evidence closure.'})
    return 0 if success else 1

def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('action',choices=('prepare','stage','finalize'))
    p.add_argument('--stage',choices=tuple(LIMITS));a=p.parse_args(argv)
    if a.action=='prepare':return prepare()
    if a.action=='finalize':return finalize()
    if not a.stage:p.error('stage requires --stage')
    return stage_run(a.stage)
if __name__=='__main__':raise SystemExit(main())
