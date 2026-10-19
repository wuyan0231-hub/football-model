"""Narrow current handoff contracts; synthetic receipts are not cloud evidence."""
from datetime import datetime, timedelta, timezone
from contextlib import redirect_stdout
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import sys
import unittest
from unittest.mock import patch

from tests.test_advanced_research_only_workflow import condition,evaluate

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('domestic_handoff_once',ROOT/'scripts/domestic_handoff_once.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)

class HandoffWorkflowTests(unittest.TestCase):
    def test_only_exact_one_time_create_scope_and_job_lock(self):
        text=(ROOT/'.github/workflows/domestic-handoff-once.yml').read_text()
        job=text.split('  handoff:\n',1)[1]
        github={'repository':'wuyan0231-hub/football-model','event_name':'create',
            'ref':'refs/heads/codex/domestic-handoff-20261019','run_attempt':1,
            'event':{'repository':{'id':1380205666},'ref_type':'branch','ref':'codex/domestic-handoff-20261019'}}
        self.assertTrue(evaluate(condition(job,'    '),{'github':github}))
        for key,value in (('event_name','push'),('repository','other/repo'),('run_attempt',2),('ref','refs/heads/main')):
            self.assertFalse(evaluate(condition(job,'    '),{'github':{**github,key:value}}))
        for key,value in (('ref_type','tag'),('ref','another'),('repository',{'id':1})):
            self.assertFalse(evaluate(condition(job,'    '),{'github':{**github,'event':{**github['event'],key:value}}}))
        self.assertNotIn('concurrency:',text.split('jobs:',1)[0])
        self.assertIn('    concurrency:\n      group: football-model-state',text)
        self.assertIn('timeout-minutes: 110',text)
        self.assertIn('test "$(git rev-parse HEAD)" = "$(git rev-parse FETCH_HEAD)"',text)
        for forbidden in ('workflow_dispatch:','schedule:','inputs.','--auto-activate','apt-get','r-base','workflow enable'):
            self.assertNotIn(forbidden,text)
        self.assertIn("steps.check.outcome == 'success'",text)
        self.assertIn("steps.receipt.outcome != 'success'",text)

    def test_fixed_commands_have_no_fit_or_activation_path_and_bounded_api_stages(self):
        prepared={'source_commit':'a'*40,'parent_state_commit':'b'*40}
        forbidden={'study','fit','--run-goalmodel','--auto-activate','football_model.cli',
            'football_model.shadow_models','football_model.feature_research'}
        for stage in m.LIMITS:
            self.assertFalse(set(m.command(stage,prepared))&forbidden,stage)
        for stage in ('facts','scoring','legacy_scoring_v1','legacy_scoring_v2'):
            self.assertEqual(m.LIMITS[stage],600)

class HandoffReceiptTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.previous=Path.cwd();os.chdir(self.temp.name);self.addCleanup(os.chdir,self.previous)
        for name in ('config.json',m.PROTOCOL,'feature-models.json','feature-models-v2.json'):
            Path(name).write_bytes((ROOT/name).read_bytes())
        self.source='a'*40;self.parent='b'*40
        environment=patch.dict(os.environ,{'GITHUB_SHA':self.source,'GITHUB_RUN_ID':'42','GITHUB_RUN_ATTEMPT':'1'})
        environment.start();self.addCleanup(environment.stop)
        self.git=Path('gitdir');self.git.mkdir()
        m.write(self.git/'football-cloud-state.json',{'source_sha':self.source,'state_oid':self.parent})
        with patch.object(m.subprocess,'check_output',return_value=str(self.git.resolve())):
            self.assertEqual(m.prepare(),0)

    def completed(self):
        now=datetime.now(timezone.utc)
        for stage in m.LIMITS:
            m.write(m.OUT/('stage-'+stage+'.json'),{'stage':stage,'status':'completed',
                'source_commit':self.source,'parent_state_commit':self.parent,
                'github_run_id':'42','github_run_attempt':'1','exit_code':0,
                'started_at':(now-timedelta(seconds=2)).isoformat(),'completed_at':(now+timedelta(seconds=2)).isoformat()})
        for stage,paths in m.report_paths().items():
            for path in paths:
                league=(path.parts[2] if stage=='confirmation' else path.parts[4]
                    if stage in ('capture','scoring') else 'serie_a')
                m.write(path,{'league':league,'generated_at':now.isoformat(),'source_commit':self.source,
                    'protocol_sha256':m.digest(m.read(m.PROTOCOL)),'status':'complete','api_requests':0})
        m.write('handoff-state-save-run-receipt.json',self.native_receipt())

    def native_receipt(self):
        return {'schema_version':'cloud-state-save-receipt-v1','source_sha':self.source,
            'parent_state_oid':self.parent,'post_state_oid':'c'*40,
            'github_run_id':'42','github_run_attempt':'1','child_exit_code':0,'receipt_parse_status':'valid',
            'runtime_validation':{'status':'passed','evidence':'same_child_MODEL_STATE_SAVE_PREFLIGHT'},
            'resource_preflight':{'status':'passed','report':{'state_oid':self.parent,'can_save':True}},
            'pack_probe':{'status':'passed','actual_new_pack_bytes':123},
            'save':{'status':'saved_and_marker_verified','reported_commit':'c'*40,
                    'marker_matches_reported_commit':True}}

    def test_scope_excludes_audit_only_without_changing_original_config(self):
        original=m.read('config.json');derived=m.read(m.CONFIG)
        self.assertEqual(len(derived['competitions']),18)
        self.assertEqual([r['key'] for r in derived['competitions']],list(m.LEAGUES))
        self.assertEqual(original,json.loads((ROOT/'config.json').read_bytes()))

    def test_only_real_stage_success_save_binding_and_new_reports_complete(self):
        self.completed();self.assertEqual(m.finalize(),0)
        result=m.read(m.OUT/'handoff-receipt.json')
        self.assertEqual(result['saved_state_commit'],'c'*40)
        self.assertFalse(result['accuracy_improvement_verified'])
        self.assertFalse(result['historical_samples_requalified'])
        self.assertEqual(result['new_fits'],0)
        self.assertEqual((m.OUT/'handoff-state-save-run-receipt.json').read_bytes(),
                         Path('handoff-state-save-run-receipt.json').read_bytes())
        m.write(m.OUT/'stage-facts.json',{'stage':'facts','status':'failed','started_at':m.timestamp(),'completed_at':m.timestamp()})
        self.assertEqual(m.finalize(),1)
        self.assertEqual(m.read(m.OUT/'handoff-receipt.json')['status'],'incomplete_or_failed')

    def test_old_latest_missing_save_and_new_activation_cannot_claim_success(self):
        self.completed()
        path=m.report_paths()['capture'][0]
        value=m.read(path);value['source_commit']='d'*40;m.write(path,value)
        self.assertEqual(m.finalize(),1)
        self.completed();Path('handoff-state-save-run-receipt.json').unlink()
        self.assertEqual(m.finalize(),1)
        self.completed()
        ph=m.digest(m.read(m.PROTOCOL))
        m.write(Path('runtime/advanced_models/epl')/ph/'confirmation/d/M/EARLY/activation.json',{'unexpected':True})
        self.assertEqual(m.finalize(),1)
        self.assertFalse(m.read(m.OUT/'handoff-receipt.json')['activation_pointers_unchanged'])

    def test_incomplete_or_foreign_native_save_receipts_never_claim_completion(self):
        self.completed();good=self.native_receipt()
        cases=[]
        missing=copy.deepcopy(good);missing.pop('post_state_oid');missing['save'].pop('reported_commit');cases.append(missing)
        for key,value in (('parent_state_oid','d'*40),('source_sha','d'*40),
                          ('github_run_id','43'),('github_run_attempt','2'),
                          ('child_exit_code',False),('child_exit_code',1),('receipt_parse_status','invalid')):
            cases.append({**good,key:value})
        for key in ('runtime_validation','resource_preflight','pack_probe'):
            bad=copy.deepcopy(good);bad[key]['status']='not_proven';cases.append(bad)
        for key,value in (('reported_commit',None),('reported_commit','c'*39),
                          ('reported_commit','C'*40),('marker_matches_reported_commit',False)):
            bad=copy.deepcopy(good);bad['save'][key]=value
            if key=='reported_commit':bad['post_state_oid']=value
            cases.append(bad)
        for index,value in enumerate(cases):
            with self.subTest(case=index):
                m.write('handoff-state-save-run-receipt.json',value)
                self.assertEqual(m.finalize(),1)
                result=m.read(m.OUT/'handoff-receipt.json')
                self.assertIsNone(result['saved_state_commit'])
                self.assertFalse(result['saved_and_marker_verified'])

    def test_native_save_once_output_is_compatible_and_sha256_oid_is_allowed(self):
        from scripts.cloud_state_save_once import save_once
        self.completed()
        preflight={'state_oid':self.parent,'can_save':True,'runtime_logical_bytes':1,'available_disk_bytes':2}
        child='\n'.join('print('+repr(line)+')' for line in (
            'MODEL_STATE_SAVE_PREFLIGHT='+json.dumps(preflight),
            'MODEL_STATE_NEW_PACK_BYTES=123','MODEL_STATE_SAVED_COMMIT='+'c'*40))
        with redirect_stdout(io.StringIO()):
            code,native=save_once([sys.executable,'-c',child],receipt_path='handoff-state-save-run-receipt.json',
                before_marker={'state_oid':self.parent,'source_sha':self.source},
                after_marker=lambda:{'state_oid':'c'*40,'source_sha':self.source},source_sha=self.source,
                runtime_path='runtime',run_id='42',run_attempt='1')
        self.assertEqual(code,0)
        self.assertTrue(m.verified_save(native,m.read(m.OUT/'preparation.json')))
        self.assertEqual(m.finalize(),0)
        native['save']['reported_commit']='c'*64;native['post_state_oid']='c'*64
        m.write('handoff-state-save-run-receipt.json',native)
        self.assertEqual(m.finalize(),0)

    def test_changed_activation_in_any_domestic_protocol_prevents_save(self):
        self.completed()
        m.write(Path('runtime/advanced_models/epl/old-protocol/confirmation/d/M/EARLY/activation.json'),{'unexpected':True})
        with patch.object(m.subprocess,'Popen') as child:
            self.assertEqual(m.stage_run('persist'),1)
        child.assert_not_called()
        self.assertEqual(m.read(m.OUT/'stage-persist.json')['status'],'activation_guard_rejected')
        self.assertEqual(m.finalize(),1)
        self.assertFalse(m.read(m.OUT/'handoff-receipt.json')['domestic_all_protocol_activation_pointers_unchanged'])

if __name__=='__main__':unittest.main()
