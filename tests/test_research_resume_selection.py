"""Run the manual validation shell and evaluate its actual fit selection."""
import ast
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import textwrap
import unittest
from unittest import mock

ROOT=Path(__file__).resolve().parents[1]
WORKFLOW=(ROOT/'.github/workflows/advanced-models.yml').read_text()


class ResearchResumeSelectionTests(unittest.TestCase):
    def test_native_zero_exit_with_missing_checkpoint_cannot_pass_saved_semantics(self):
        block=WORKFLOW.split('      - name: 核实际检查点重验语义并保留延期失败\n',1)[1].split('      - name:',1)[0]
        script=textwrap.dedent(block.split("          python - <<'PY'\n",1)[1].split('          PY',1)[0])
        verified={'league':'laliga','durable_saved':True,'saved_snapshot_verified':True,
                  'durability_status':'verified_saved_git_closure','current_inputs_match':False,'current_refit_required':True}
        valid={'stored_research_durable_full_completed':True,'stored_verified_completed_leagues':1,'results':[verified]}
        cases=[(valid,True),
               ({'pending_leagues':1,'stored_research_durable_full_completed':False,'stored_verified_completed_leagues':0,
                 'results':[{'league':'laliga','status':'checkpoint_resume_deferred','durable_saved':False}]},False),
               ({**valid,'results':[{**verified,'saved_snapshot_verified':False}]},False),
               ({**valid,'results':[{**verified,'league':'epl'}]},False),
               ({**valid,'results':[verified,verified]},False),
               ({**valid,'stored_verified_completed_leagues':0},False)]
        for value,ok in cases:
            with self.subTest(value=value),tempfile.TemporaryDirectory() as directory:
                previous=Path.cwd();os.chdir(directory)
                try:
                    binding={'status':'current_report_verified','path':'runtime/advanced_models/study_runs/latest.json','sha256':'a'*64,'report':value}
                    with mock.patch('scripts.advanced_study_window.current_report',return_value=binding), \
                         mock.patch.dict(os.environ,RESUME_RESEARCH='success',STUDY_LEAGUE='laliga',GITHUB_SHA='b'*40,GITHUB_RUN_ID='123',GITHUB_RUN_ATTEMPT='1'):
                        if ok:exec(compile(script,'<actual resume semantics>','exec'),{})
                        else:
                            with self.assertRaisesRegex(ValueError,'native zero exit'):
                                exec(compile(script,'<actual resume semantics>','exec'),{})
                    out=Path('advanced-resume-verification.json')
                    self.assertEqual(out.exists(),ok)
                    if ok:
                        receipt=json.loads(out.read_bytes())
                        self.assertFalse(receipt['fit_attempted_by_resume'])
                        self.assertFalse(receipt['current_inputs_match'])
                        self.assertTrue(receipt['current_refit_required'])
                finally:os.chdir(previous)

    def test_recovery_consumer_preserves_first_window_and_rejects_wrong_attempt_or_proof(self):
        block=WORKFLOW.split('      - name: 分列原命令失败与精确远端保存恢复\n',1)[1].split('      - name:',1)[0]
        script=textwrap.dedent(block.split("          python - <<'PY'\n",1)[1].split('          PY',1)[0])
        planned='c'*40
        valid={'schema_version':'cloud-state-save-checked-v1','source_sha':'a'*40,
               'github_run_id':'123','github_run_attempt':'1','study':{'league':'laliga','protocol_hash':'p'},
               'recovered_state_only':True,'status':'push_timeout_remote_save_verified','operation_completed':False,
               'original_exit_code':124,'wrapper_exit_code':0,'recovery':{'planned_commit':planned,
                   'remote_observations':[planned,planned],'checkpoint':{'calculation_completed':True,'league':'laliga','protocol_hash':'p'}}}
        for patch,ok in (({},True),({'github_run_attempt':'2'},False),({'source_sha':'b'*40},False),
                         ({'operation_completed':True},False),({'original_exit_code':0},False),
                         ({'recovery':{**valid['recovery'],'remote_observations':[planned,'d'*40]}},False),
                         ({'recovery':{**valid['recovery'],'checkpoint':{'calculation_completed':True,'league':'epl','protocol_hash':'p'}}},False)):
            with self.subTest(patch=patch),tempfile.TemporaryDirectory() as directory:
                root=Path(directory);(root/'advanced-checkpoint-checked-save.json').write_text(json.dumps({**valid,**patch}))
                original=b'{"original_wrapper_exit_code":0,"original_child_exit_code":124}\n'
                (root/'advanced-study-run-window.json').write_bytes(original)
                out=root/'output'
                result=subprocess.run([os.sys.executable,'-c',script],cwd=root,capture_output=True,text=True,
                    env={**os.environ,'GITHUB_SHA':'a'*40,'GITHUB_RUN_ID':'123','GITHUB_RUN_ATTEMPT':'1',
                         'STUDY_LEAGUE':'laliga','GITHUB_OUTPUT':str(out)})
                self.assertEqual(result.returncode==0,ok,result.stdout+result.stderr)
                self.assertEqual((root/'advanced-study-run-window.json').read_bytes(),original)
                if ok:
                    self.assertEqual((root/'advanced-study-run-first-window.json').read_bytes(),original)
                    self.assertIn('recovered=true\nstate='+planned+'\n',out.read_text())
                else:self.assertFalse(out.exists())

    def test_resume_only_accepts_main_manual_research_mode_and_known_choice(self):
        block=WORKFLOW.split('      - name: 验证手动模式互斥与短轮分支\n',1)[1].split('  verify:\n',1)[0]
        script='\n'.join(x[10:] for x in block.split('        run: |\n',1)[1].splitlines())
        for selection,research,short,ref,expected in (
            ('all','false','false','refs/heads/main',0),
            ('all','false','true','refs/heads/main',0),
            ('remaining_after_epl_championship','true','false','refs/heads/main',0),
            ('remaining_after_epl_championship','false','false','refs/heads/main',1),
            ('remaining_after_epl_championship','true','true','refs/heads/main',1),
            ('remaining_after_epl_championship','true','false','refs/heads/review',1),
            ('epl','true','false','refs/heads/main',1),
            ('all; echo unsafe','true','false','refs/heads/main',1),
        ):
            with self.subTest(selection=selection,research=research,short=short,ref=ref):
                result=subprocess.run(['bash','-c',script],capture_output=True,text=True,
                    env={**os.environ,'RESEARCH_LEAGUES':selection,'RESEARCH_ONLY':research,
                         'SHORT_ONLY':short,'DISPATCH_REF':ref})
                self.assertEqual(result.returncode,expected,result.stdout+result.stderr)

    def test_selected_fit_jobs_are_17_only_for_explicit_manual_continuation(self):
        expression=re.search(r'(?m)^        league: \$\{\{ fromJSON\((.+)\) \}\}$',WORKFLOW).group(1)
        expression=expression.replace('&&',' and ').replace('||',' or ').replace('true','True')
        configured=[c['key'] for c in json.loads((ROOT/'config.json').read_text())['competitions'] if c.get('mode')!='audit_only']
        from types import SimpleNamespace as NS
        for event,research,selection,expected in (
            ('workflow_dispatch',True,'remaining_after_epl_championship',configured[2:]),
            ('workflow_dispatch',True,'all',configured),
            ('workflow_dispatch',False,'remaining_after_epl_championship',configured),
            ('schedule',True,'remaining_after_epl_championship',configured),
            ('push',True,'remaining_after_epl_championship',configured),
        ):
            value=eval(compile(ast.parse(expression,mode='eval'),'<actual matrix>','eval'),{'__builtins__':{}},
                       {'github':NS(event_name=event),'inputs':NS(research_only=research,research_leagues=selection)})
            self.assertEqual(json.loads(value),expected)
        final=WORKFLOW.split('  study_finalize:\n',1)[1]
        self.assertIn('--selection all -- python -m football_model.advanced_research study',final)
        self.assertIn('--resume-only',final)
        self.assertIn('research_full_completed',final)

    def test_invalid_choice_blocks_preparation_writer_before_collection(self):
        from tests.test_advanced_research_only_workflow import condition,context,evaluate,job
        body=job('pre_study_capture')
        self.assertIn('    needs: validate_dispatch',body)
        value=context(research_only=False)
        for result in ('failure','skipped','cancelled'):
            value['needs']['validate_dispatch']['result']=result
            self.assertFalse(evaluate(condition(body,'    '),value))
        for event in ('push','schedule'):
            value=context(event,schedule='7 2 * * *')
            value['needs']['validate_dispatch']['result']='skipped'
            self.assertTrue(evaluate(condition(body,'    '),value))


if __name__=='__main__':unittest.main()
