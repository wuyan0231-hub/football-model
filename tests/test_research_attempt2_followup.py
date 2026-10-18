"""Synthetic adversarial contracts; real K1 originals are separately verified."""
import ast
import copy
import gzip
import hashlib
import io
import json
import re
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch
import urllib.request
import zipfile

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import research_attempt2_followup as a


def raw(value):
    return json.dumps(value,sort_keys=True,separators=(',',':')).encode()


class ContractTests(unittest.TestCase):
    def setUp(self):
        self.league='mls';self.parent='1'*40;self.checkpoint='2'*40;self.final='3'*40
        files={f'file-{i}':hashlib.sha256(str(i).encode()).hexdigest() for i in range(50)}
        self.fp={'schema_version':'advanced-implementation-fingerprint-v1','files':files,'sha256':a.sha(raw(files))}
        self.old=patch.object(a,'OLD',self.fp['sha256']);self.old.start();self.addCleanup(self.old.stop)
        common={'source_sha':a.SOURCE,'github_run_id':str(a.RUN),'github_run_attempt':str(a.ATTEMPT)}
        self.checked={**common,'source_ref':'refs/heads/main','operation_completed':True,
            'original_exit_code':0,'wrapper_exit_code':0,'status':'command_completed','recovered_state_only':False,
            'study':{'league':self.league,'protocol_hash':a.PROTOCOL,'goalmodel_requested':True,
                'protocol_raw_sha256':'4139069bbe6f13c27020b32655dedfa8620475ff80287955a43ee96b00501364'},
            'implementation_fingerprint':self.fp,'before_marker':{'state_oid':self.parent},
            'command':['python','-m','football_model.advanced_research','study','--runtime','runtime',
                '--league',self.league,'--protocol','advanced-models-v1.json','--source-commit',a.SOURCE,
                '--state-commit',self.parent,'--run-goalmodel','--checkpoint-state','--repo','.']}
        checkpoint_ref={'path':'checkpoint/history/original.json','sha256':'f'*64}
        self.native={'source_commit':a.SOURCE,'configured_leagues':[self.league],'stored_verified_completed_leagues':1,
            'stored_research_durable_full_completed':True,'results':[{'league':self.league,'durable_saved':True,
                'calculation_completed':True,'durable_state_commit':self.checkpoint,'checkpoint_ref':checkpoint_ref}]}
        self.window={**common,'source_commit':a.SOURCE,'configured_leagues':[self.league],
            'command_returncode':0,'report_sha256':a.sha(raw(self.native)),'report_changed':True,
            'started_at':'2026-10-18T00:00:01Z','ended_at':'2026-10-18T00:01:00Z',
            'report_path':'runtime/advanced_models/study_runs/latest.json'}
        self.run={'GITHUB_SHA':a.SOURCE,'GITHUB_RUN_ID':str(a.RUN),'GITHUB_RUN_ATTEMPT':str(a.ATTEMPT),
            'STUDY_LEAGUE':self.league,'ADVANCED_PARENT_STATE':self.parent,
            'original_research_command':{'receipt_sha256':a.sha(raw(self.checked))},
            'post_save_state':{'state_oid':self.final},'study_report_binding':{'status':'current_report_verified',
                'sha256':a.sha(raw(self.native)),'window_sha256':a.sha(raw(self.window))}}
        self.save={**common,'child_exit_code':0,'receipt_parse_status':'valid','parent_state_oid':self.checkpoint,
            'post_state_oid':self.final,'runtime_validation':{'status':'passed'},'resource_preflight':{'status':'passed','report':{'can_save':True}},
            'pack_probe':{'status':'passed'},'save':{'status':'saved_and_marker_verified','reported_commit':self.final,'marker_matches_reported_commit':True}}
        self.summary={'league':self.league,'source_commit':a.SOURCE,'protocol_hash':a.PROTOCOL,'implementation_fingerprint':self.fp}
        self.members={'advanced-study-run-receipt.json':raw(self.run),'advanced-league-save-run-receipt.json':raw(self.save),
            'runtime/advanced_models/study_runs/latest.json':raw(self.native),'advanced-study-run-window.json':raw(self.window),
            f'runtime/advanced_models/{self.league}/{a.PROTOCOL}/reports/latest.json':raw(self.summary),
            f'runtime/advanced_models/checkpoints/{self.league}/{a.PROTOCOL}/latest.json':raw({'checkpoint_ref':checkpoint_ref})}
        self.manifest={'schema_version':'advanced-run-thin-evidence-v1','league_selection':self.league,
            'source_commit':a.SOURCE,'github_run_id':str(a.RUN),'github_run_attempt':str(a.ATTEMPT),
            'state_marker':{'state_oid':self.final},'entries':[{'path':k,'bytes':len(v),'sha256':a.sha(v)} for k,v in self.members.items()]}
        self.outer={'advanced-study-run-receipt.json':raw(self.run),'advanced-checkpoint-checked-save.json':raw(self.checked)}
        self.job={'id':123,'name':f'study_leagues ({self.league})','run_id':a.RUN,'run_attempt':2,
            'head_sha':a.SOURCE,'head_branch':'main','status':'completed','conclusion':'success',
            'started_at':'2026-10-18T00:00:00Z','completed_at':'2026-10-18T00:02:00Z',
            'steps':[{'name':name,'conclusion':'success'} for name in ('单联赛真实研究与独立持久检查点','保存当次汇总与失败原件')]}
        self.log=f'2026-10-18T00:00:00Z MODEL_STATE_SAVED_COMMIT={self.checkpoint}\n2026-10-18T00:01:00Z MODEL_STATE_SAVED_COMMIT={self.final}\n'.encode()

    def bind(self):
        return a.bind(self.outer,self.members,self.manifest,self.league,self.job,self.log)

    def package(self, members=None, compression=zipfile.ZIP_STORED):
        data=io.BytesIO()
        with tarfile.open(fileobj=data,mode='w:gz') as t:
            for name,body in {**(members or self.members),'advanced-evidence-manifest.json':raw(self.manifest)}.items():
                info=tarfile.TarInfo(name);info.size=len(body);t.addfile(info,io.BytesIO(body))
        out=io.BytesIO();body=data.getvalue()
        with zipfile.ZipFile(out,'w',compression=compression) as z:
            for n,b in {**self.outer,'advanced-models-evidence.tar.gz':body,
                'advanced-models-evidence.tar.sha256':(a.sha(body)+'  advanced-models-evidence.tar.gz\n').encode()}.items():z.writestr(n,b)
        body=out.getvalue()
        return body,{'name':f'advanced-study-{self.league}-{a.RUN}-2','expired':False,'size_in_bytes':len(body),
            'digest':'sha256:'+a.sha(body),'workflow_run':{'id':a.RUN,'repository_id':a.REPO_ID,
                'head_repository_id':a.REPO_ID,'head_sha':a.SOURCE,'head_branch':'main'}}

    def test_normal_save_distinguishes_checkpoint_and_final(self):
        value=self.bind();self.assertEqual(value['state_commit'],self.final);self.assertEqual(value['checkpoint_state'],self.checkpoint)
    def test_real_format_zip_tar_roundtrip(self):
        blob,meta=self.package();outer,members,manifest=a.unpack(blob,meta,self.league)
        self.assertEqual(a.bind(outer,members,manifest,self.league,self.job,self.log)['state_commit'],self.final)
    def test_checkpoint_must_not_replace_final_save(self):
        self.manifest['state_marker']['state_oid']=self.checkpoint
        with self.assertRaisesRegex(a.Refused,'final_checkpoint_confusion'):self.bind()
    def test_checked_raw_hash_binding(self):
        self.outer['advanced-checkpoint-checked-save.json']+=b' '
        with self.assertRaisesRegex(a.Refused,'checked_receipt_hash'):self.bind()
    def test_old_source_cannot_be_retagged(self):
        self.run['GITHUB_SHA']='f'*40;self.outer['advanced-study-run-receipt.json']=raw(self.run)
        with self.assertRaisesRegex(a.Refused,'native_receipt_identity'):self.bind()
    def test_real_recovery_is_incomplete_not_fake_unsaved(self):
        self.checked['recovered_state_only']=True;self.outer['advanced-checkpoint-checked-save.json']=raw(self.checked)
        self.run['original_research_command']['receipt_sha256']=a.sha(raw(self.checked));self.outer['advanced-study-run-receipt.json']=raw(self.run)
        with self.assertRaisesRegex(a.Refused,'recovery_saved_but_audit_unsupported'):self.bind()
    def test_ambiguous_official_saved_lines_refused(self):
        self.log+=self.log
        with self.assertRaisesRegex(a.Refused,'official_saved_log_chain'):self.bind()
    def test_failed_native_save_step_refused(self):
        self.job['steps'][1]['conclusion']='failure'
        with self.assertRaisesRegex(a.Refused,'native_save_step_failure'):self.bind()
    def test_native_missing_receipt_refused(self):
        del self.members['advanced-league-save-run-receipt.json']
        with self.assertRaises(KeyError):self.bind()
    def test_duplicate_keys_and_nonfinite_refused(self):
        for value in (b'{"x":1,"x":2}',b'{"x":NaN}'):
            with self.assertRaises(a.Refused):a.strict_json(value)
    def test_path_escape_refused(self):
        for value in ('../x','/x','a\\b','a/../b','a//b','a\x00b'):
            with self.assertRaises(a.Refused):a.canonical_path(value)
    def test_gzip_budget_before_tar_parser(self):
        blob,meta=self.package()
        with patch.object(a,'MIB',16):
            with self.assertRaises(a.Refused):a.unpack(blob,meta,self.league)
    def test_compressed_zip_outside_original_contract_refused(self):
        for compression in (zipfile.ZIP_DEFLATED,zipfile.ZIP_BZIP2,zipfile.ZIP_LZMA):
            blob,meta=self.package(compression=compression)
            with self.assertRaisesRegex(a.Refused,'zip_original_stored_contract'):a.unpack(blob,meta,self.league)
    def test_artifact_digest_mismatch_refused(self):
        blob,meta=self.package();meta['digest']='sha256:'+'0'*64
        with self.assertRaisesRegex(a.Refused,'official_artifact_digest'):a.unpack(blob,meta,self.league)
    def test_tar_manifest_member_hash_mismatch(self):
        members={**self.members,'advanced-study-run-window.json':b'{}'};blob,meta=self.package(members)
        with self.assertRaisesRegex(a.Refused,'manifest_entry_hash'):a.unpack(blob,meta,self.league)
    def test_unique_git_parent_and_metadata_agree(self):
        meta={'source_sha':a.SOURCE,'source_ref':'refs/heads/main','github_run_id':str(a.RUN),
            'github_run_attempt':'2','previous_state_oid':self.checkpoint}
        commit={'sha':self.final,'parents':[{'sha':self.checkpoint}]}
        a.verify_commit(commit,meta,self.final,self.checkpoint)
        commit['parents'].append({'sha':'f'*40})
        with self.assertRaisesRegex(a.Refused,'unique_git_parent'):a.verify_commit(commit,meta,self.final,self.checkpoint)
    def test_redirect_strips_auth_and_refuses_arbitrary_host(self):
        handler=a.SafeRedirect();request=urllib.request.Request('https://api.github.com/repos/x',headers={'Authorization':'Bearer example'})
        moved=handler.redirect_request(request,None,302,'Found',{},'https://example.blob.core.windows.net/file')
        self.assertIsNone(moved.get_header('Authorization'))
        for url in ('http://example.blob.core.windows.net/file','https://attacker.example/file'):
            with self.assertRaises(a.Refused):handler.redirect_request(request,None,302,'Found',{},url)
    def test_target_denominator_excludes_already_accepted(self):
        self.assertEqual(len(a.TARGETS),5);self.assertEqual(set(a.TARGETS)&set(a.SKIPPED),set())
        self.assertEqual(len(a.TARGETS)+len(a.SKIPPED),19)
    def test_reviewed_helper_hashes_are_fixed_and_drift_refuses(self):
        repo=Path(__file__).resolve().parents[1]
        self.assertEqual(a.verify_helpers(repo),a.HELPERS)
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            for name in a.HELPERS:
                path=root/name;path.parent.mkdir(exist_ok=True,parents=True);path.write_bytes((repo/name).read_bytes())
            self.assertEqual(a.verify_helpers(root),a.HELPERS)
            (root/next(iter(a.HELPERS))).write_bytes(b'changed helper')
            with self.assertRaisesRegex(a.Refused,'reviewed_helper_bytes_changed'):a.verify_helpers(root)
    def test_hidden_gnu_and_sparse_tar_headers_refused(self):
        for kind in (tarfile.GNUTYPE_LONGNAME,tarfile.GNUTYPE_SPARSE,tarfile.LNKTYPE,tarfile.SYMTYPE):
            stream=io.BytesIO()
            with tarfile.open(fileobj=stream,mode='w') as archive:
                item=tarfile.TarInfo('advanced-test.json');item.type=kind;item.size=0;archive.addfile(item)
            with self.assertRaisesRegex(a.Refused,'tar_extended_header'):a.tar_headers(stream.getvalue())
    def test_non_path_pax_header_refused(self):
        data=io.BytesIO()
        with tarfile.open(fileobj=data,mode='w') as archive:
            item=tarfile.TarInfo('advanced-test.json');item.pax_headers={'comment':'x'*2048};archive.addfile(item)
        with self.assertRaisesRegex(a.Refused,'tar_extended_header'):a.tar_headers(data.getvalue())
    def test_aggregate_hashes_actual_downloaded_files_and_keeps_five(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);directory=root/'receipts';directory.mkdir()
            item=directory/'research-attempt2-followup-mls-1-1';item.mkdir()
            (item/'report.json').write_bytes(b'{}')
            receipt={'league':'mls','original_run_id':a.RUN,'original_run_attempt':2,'original_source_commit':a.SOURCE,
                'status':'audit_incomplete','output_files':[{'path':'report.json','bytes':2,'sha256':a.sha(b'{}')}]}
            (item/'run-final.json').write_bytes(raw(receipt));output=root/'closure.json'
            self.assertEqual(a.aggregate(directory,output),1)
            result=json.loads(output.read_bytes());self.assertEqual(len(result['results']),5)
            self.assertEqual(result['results'][0]['verified_output_bytes'],2)
            (item/'report.json').write_bytes(b'[]')
            a.aggregate(directory,output);result=json.loads(output.read_bytes())
            self.assertEqual(result['results'][0]['error_code'],'downloaded_output_hash')
    def test_workflow_is_readonly_fixed_source_and_bounded(self):
        text=(Path(__file__).resolve().parents[1]/'.github/workflows/research-attempt2-followup.yml').read_text()
        for token in ('workflow_run:','workflow_dispatch: {}','contents: read','actions: read','max-parallel: 2','timeout-minutes: 60','3300',a.SOURCE,str(a.RUN)):
            self.assertIn(token,text)
        for token in ('secrets.FOOTBALL_API_KEY','cloud_state.py save','--auto-activate'):
            self.assertNotIn(token,text)

    def test_manual_replay_has_no_inputs_or_other_triggers(self):
        text=(Path(__file__).resolve().parents[1]/'.github/workflows/research-attempt2-followup.yml').read_text()
        triggers=text.split('\non:\n',1)[1].split('\npermissions:',1)[0]
        self.assertEqual(re.findall(r'^  ([a-z_]+):',triggers,re.M),['workflow_run','workflow_dispatch'])
        self.assertIn('  workflow_dispatch: {}',triggers)
        self.assertNotIn('inputs:',triggers)
        self.assertNotRegex(triggers,r'(?m)^\s*(push|schedule|pull_request|repository_dispatch):')

    def test_entrypoint_guard_keeps_main_replay_and_exact_upstream(self):
        text=(Path(__file__).resolve().parents[1]/'.github/workflows/research-attempt2-followup.yml').read_text()
        expression=text.split('    if: >-\n',1)[1].split('    strategy:',1)[0]
        expression=' '.join(expression.split()).replace('&&',' and ').replace('||',' or ')
        tree=ast.parse(expression,mode='eval')
        def admit(github):
            def read(node):
                if isinstance(node,ast.Constant):return node.value
                if isinstance(node,ast.Name):
                    self.assertEqual(node.id,'github');return github
                if isinstance(node,ast.Attribute):
                    parent=read(node.value);return parent.get(node.attr) if isinstance(parent,dict) else None
                if isinstance(node,ast.BoolOp):
                    self.assertIsInstance(node.op,(ast.And,ast.Or))
                    return all(read(v) for v in node.values) if isinstance(node.op,ast.And) else any(read(v) for v in node.values)
                if isinstance(node,ast.Compare):
                    self.assertEqual(len(node.ops),1);self.assertIsInstance(node.ops[0],ast.Eq)
                    return read(node.left)==read(node.comparators[0])
                self.fail('unsupported workflow guard syntax: '+type(node).__name__)
            return bool(read(tree.body))
        manual={'repository':a.REPO,'ref':'refs/heads/main','event_name':'workflow_dispatch',
            'event':{'repository':{'id':a.REPO_ID}}}
        self.assertTrue(admit(manual))
        for ref in ('refs/heads/other','refs/tags/main','refs/tags/v1',''):
            bad=copy.deepcopy(manual);bad['ref']=ref;self.assertFalse(admit(bad),ref)
        for kind in ('push','schedule','pull_request','repository_dispatch','workflow_run'):
            bad=copy.deepcopy(manual);bad['event_name']=kind;self.assertFalse(admit(bad),kind)
        upstream={'id':a.RUN,'run_attempt':a.ATTEMPT,'head_sha':a.SOURCE,'head_branch':'main',
            'event':'workflow_dispatch','path':'.github/workflows/advanced-models.yml',
            'head_repository':{'id':a.REPO_ID}}
        automatic=copy.deepcopy(manual);automatic['event_name']='workflow_run'
        automatic['event']['workflow_run']=upstream
        self.assertTrue(admit(automatic))
        for key,value in (('id',a.RUN+1),('run_attempt',1),('head_sha','0'*40),('head_branch','other'),
                ('event','push'),('path','.github/workflows/other.yml'),('head_repository',{'id':a.REPO_ID+1})):
            bad=copy.deepcopy(automatic);bad['event']['workflow_run'][key]=value
            self.assertFalse(admit(bad),key)
        for original in (manual,automatic):
            bad=copy.deepcopy(original);bad['repository']='other/repository';self.assertFalse(admit(bad))
            bad=copy.deepcopy(original);bad['event']['repository']['id']=a.REPO_ID+1;self.assertFalse(admit(bad))


if __name__=='__main__':unittest.main()
