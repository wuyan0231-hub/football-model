"""Safety/identity tests for new read-only audit. No real research R refits."""
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('deep_audit',ROOT/'scripts/fixed_research_deep_audit.py')
audit=importlib.util.module_from_spec(spec);spec.loader.exec_module(audit)

class DeepAuditTests(unittest.TestCase):
    def test_numeric_tolerance_does_not_accept_null_bool_shape_or_future_extra(self):
        self.assertTrue(audit.compare({'v':1+5e-13},{'v':1},atol=1e-12)['passed'])
        for actual,expected in [(0,None),(True,1),([1,2],[1]),({'v':1,'extra':2},{'v':1})]:
            self.assertFalse(audit.compare(actual,expected,atol=1)['passed'])
        for number in [float('nan'),float('inf')]:
            self.assertFalse(audit.compare(number,1,atol=1)['passed'])

    def test_materializer_all_declared_sources_includes_schedule_and_national_load(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);source=root/'source';source.mkdir();dest=root/'copy'
            refs=[]
            for i,name in enumerate(['data/primeira.json','feature_models/schedules/primeira/latest.json',
                                    'advanced_features/player_load/manifests/test.json']):
                raw=json.dumps({'value':i}).encode();(source/f'{i}.json').write_bytes(raw)
                refs.append({'path':f'{i}.json','original_path':name,'sha256':audit.digest(raw)})
            manifest=audit.materialize_inputs({'source_refs':refs},source,dest,lambda p,**_:p.read_bytes())
            self.assertEqual(len(manifest),3)
            audit.stable_input_tree(dest,manifest)
            (dest/'data/primeira.json').write_text('{}')
            self.assertRaisesRegex(audit.DeepAuditError,'changed',audit.stable_input_tree,dest,manifest)
            self.assertEqual((source/'0.json').read_text(),'{"value": 0}')

    def test_bad_hash_conflicting_paths_and_symlink_fail_without_fallback(self):
        for mode in ['hash','conflict','symlink']:
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temporary:
                root=Path(temporary);source=root/'source';source.mkdir();dest=root/'copy'
                (source/'a').write_bytes(b'{}');(source/'b').write_bytes(b'[]')
                ref={'path':'a','original_path':'data/primeira.json','sha256':audit.digest(b'{}')}
                refs=[ref]
                if mode=='hash':ref['sha256']='0'*64
                if mode=='conflict':refs.append({'path':'b','original_path':ref['original_path'],'sha256':audit.digest(b'[]')})
                if mode=='symlink':
                    (source/'link').symlink_to(source/'a');ref['path']='link'
                with self.assertRaises(audit.DeepAuditError):
                    audit.materialize_inputs({'source_refs':refs},source,dest,lambda p,**_:p.read_bytes())

    def test_path_escape_and_noncanonical(self):
        with tempfile.TemporaryDirectory() as temporary:
            for path in ['../outside','/absolute','a//b','a/./b','a\\b','a\nb']:
                with self.subTest(path=path),self.assertRaises(audit.DeepAuditError):
                    audit.safe_path(Path(temporary),path)

    def test_duplicate_consistent_ref_is_deduplicated_but_still_verified(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);source=root/'source';source.mkdir();(source/'a').write_bytes(b'{}')
            ref={'path':'a','original_path':'data/primeira.json','sha256':audit.digest(b'{}')}
            calls=[]
            def reader(path,**_):calls.append(path);return path.read_bytes()
            records=audit.materialize_inputs({'source_refs':[ref,dict(ref)]},source,root/'copy',reader)
            self.assertEqual(len(records),1);self.assertEqual(len(calls),2)

    def test_request_rebuild_matches_adapter_without_running_R(self):
        from football_model.goalmodel_adapter import fit_predict_goalmodel
        from unittest.mock import patch
        records=[{'match_id':1,'home_id':1,'away_id':2,'home_name':'a','away_name':'b',
            'kickoff':'2026-01-01T12:00:00Z','home_goals':1,'away_goals':0,'neutral':False},
            {'match_id':2,'home_id':2,'away_id':1,'home_name':'b','away_name':'a',
            'kickoff':'2026-01-02T12:00:00Z','home_goals':0,'away_goals':2,'neutral':False}]
        probes=[{'match_id':3,'home_id':1,'away_id':2,'kickoff':'2026-01-04T12:00:00Z','neutral':False}]
        cutoff='2026-01-03T12:00:00Z'
        # Exact request hash is generated before subprocess; deliberately return
        # nonzero so this engineering test cannot masquerade as a successful fit.
        class Process:returncode=1;stderr='intentional engineering-test failure'
        with patch('football_model.goalmodel_adapter.shutil.which',return_value='/test/Rscript'),\
             patch('football_model.goalmodel_adapter.subprocess.run',return_value=Process()) as run:
            result=fit_predict_goalmodel(records,probes,cutoff=cutoff,repo_root=ROOT)
        self.assertEqual(result['request_sha256'],audit.request_identity(records,probes,[],None,None,cutoff))
        self.assertEqual(result['status'],'failed');self.assertEqual(run.call_count,1)
        changed=copy.deepcopy(records);changed[0]['home_goals']=2
        self.assertNotEqual(result['request_sha256'],audit.request_identity(changed,probes,[],None,None,cutoff))

    def test_workflow_has_no_push_schedule_API_secret_or_state_save(self):
        text=(ROOT/'.github/workflows/fixed-research-deep-audit.yml').read_text()
        self.assertIn('workflow_dispatch:',text)
        self.assertNotIn('  push:',text);self.assertNotIn('  schedule:',text)
        self.assertNotIn('secrets.FOOTBALL_API',text);self.assertNotIn('contents: write',text)
        self.assertNotIn('cloud_state.py save',text)
        self.assertIn('ref: '+audit.SOURCE,text)
        self.assertIn('audit-output/audit-*.json',text)
        self.assertNotIn('audit-output/**',text)
        self.assertIn('590s python',text)
        self.assertIn('10s 2090s',text)
        self.assertEqual(audit.LIMITS['restore_seconds']+audit.LIMITS['restore_kill_grace_seconds']+audit.LIMITS['outer_worker_seconds']+audit.LIMITS['outer_worker_kill_grace_seconds'],2700)
        self.assertIn("runtime=repo/'fixed-runtime'",text)
        self.assertIn('--reader-repo reader --runtime reader/fixed-runtime',text)
        self.assertIn("'reader_repository':str(repo)",text)
        self.assertIn('R02_NUMERIC_FIXTURE_REPO="$GITHUB_WORKSPACE/original-numeric"',text)

    def test_cold_route_preflight_rejects_wrong_parent_and_receipt(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);reader=root/'reader';runtime=reader/'fixed-runtime'
            runtime.mkdir(parents=True)
            receipt={'reader_repository':str(reader)}
            result=audit.cold_storage_preflight(runtime,reader,receipt,lambda _:reader)
            self.assertTrue(result['native_ancestor_repository_matches_reader'])
            cases=[(root/'fixed-runtime',reader,receipt,lambda _:reader),
                   (runtime,reader,{'reader_repository':str(root)},lambda _:reader),
                   (runtime,reader,receipt,lambda _:root)]
            for args in cases:
                with self.assertRaises(audit.DeepAuditError):audit.cold_storage_preflight(*args)

    def test_native_cold_object_reads_only_from_matching_reader_layout(self):
        # The real deep workflow points this subprocess at exact old source;
        # ordinary regression runs exercise the same native reader contract.
        numeric=Path(os.environ.get('R02_NUMERIC_FIXTURE_REPO',str(ROOT))).resolve()
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);reader=root/'reader';reader.mkdir()
            def git(*args):
                return subprocess.run(['git','-C',str(reader),*args],check=True,
                    capture_output=True,text=True).stdout.strip()
            git('init','-q')
            raw=b'{"cold_original":true,"matches":[]}'
            relative='advanced_models/primeira/cold-layout.json'
            source=reader/'runtime'/relative;source.parent.mkdir(parents=True);source.write_bytes(raw)
            git('add','runtime')
            git('-c','user.name=Audit Fixture','-c','user.email=audit@invalid.local',
                'commit','-qm','Local immutable cold-object fixture')
            state=git('rev-parse','HEAD');sha=audit.digest(raw)
            entry={'path':relative,'state_commit':state,'transport_path':'runtime/'+relative,
                'codec':'identity','raw_sha256':sha,'raw_bytes':len(raw),'blob_sha256':sha,
                'compressed_bytes':len(raw),'manifest_sha256':None}
            for runtime in [root/'fixed-runtime',reader/'fixed-runtime']:
                (runtime/'history_archive').mkdir(parents=True)
                (runtime/'history_archive/catalog.json').write_text(json.dumps({
                    'schema_version':'fixed-git-history-catalog-v1','entries':[entry]}))
            script="""
import json,sys
from pathlib import Path
from football_model import history_archive as history
from football_model.advanced_storage import load_artifact
root,numeric=Path(sys.argv[1]),Path(sys.argv[2]);reference=json.loads(sys.argv[3])
assert Path(history.__file__).resolve().is_relative_to(numeric)
def no_fetch(*_):raise history.StorageError('fixture_network_fetch_forbidden')
history._fetch_cold_commit=no_fetch
wrong=root/'fixed-runtime';right=root/'reader/fixed-runtime'
try:
    assert history._repo(wrong)!=root/'reader'
except history.StorageError as exc:
    assert str(exc)=='历史原件需要已授权的本地 Git 对象库，已停止。'
try:
    load_artifact(wrong,reference)
except history.StorageError as exc:
    assert str(exc) in ('历史原件需要已授权的本地 Git 对象库，已停止。',
                       'fixture_network_fetch_forbidden')
    wrong_reason='no_ancestor_repository' if str(exc).startswith('历史原件需要') else 'wrong_ancestor_repository'
else:
    raise AssertionError('sibling cold runtime unexpectedly succeeded')
assert history._repo(right)==root/'reader'
value=load_artifact(right,reference)
assert value=={'cold_original':True,'matches':[]}
print(json.dumps({'old_or_current_native_cold_read_verified':True,'wrong_layout_reason':wrong_reason}))
"""
            result=subprocess.run([sys.executable,'-c',script,str(root),str(numeric),
                json.dumps({'path':relative,'sha256':sha})],cwd=root,
                env={**os.environ,'PYTHONPATH':str(numeric)},capture_output=True,text=True,
                check=True,timeout=20)
            self.assertTrue(json.loads(result.stdout)['old_or_current_native_cold_read_verified'])

    def test_safe_exception_diagnostics_omit_arbitrary_payloads(self):
        from football_model.state_storage import StorageError
        known=StorageError('历史原件需要已授权的本地 Git 对象库，已停止。')
        self.assertEqual(audit.safe_exception_details(known,ROOT)['error_code'],
                         'cold_git_repository_not_found')
        self.assertEqual(audit.safe_exception_details(MemoryError(),ROOT)['error_code'],
                         'worker_memory_limit')
        for kind in (StorageError,ValueError,audit.DeepAuditError):
            try:raise kind('arbitrary_private_payload_DO_NOT_OUTPUT')
            except Exception as exc:result=audit.safe_exception_details(exc,ROOT)
            self.assertNotIn('arbitrary_private_payload',json.dumps(result))
            self.assertFalse(result['exception_text_included'])
            self.assertLessEqual(len(result['safe_traceback']),12)
            self.assertTrue(result['safe_traceback'])

    def test_comparison_failure_has_hashes_and_paths_without_values(self):
        actual={'home':{'xg_for_mean':2,'PRIVATE_UNREGISTERED_FIELD':'PRIVATE_ACTUAL_VALUE'}}
        expected={'home':{'xg_for_mean':1,'PRIVATE_UNREGISTERED_FIELD':'PRIVATE_EXPECTED_VALUE'}}
        with self.assertRaises(audit.DeepAuditError) as raised:
            audit.exact(actual,expected,'feature_dataset_metadata_mismatch')
        result=audit.safe_exception_details(raised.exception,ROOT)['comparison_failure']
        self.assertEqual(result['mismatches'],2)
        self.assertEqual(result['first_mismatch'],'/home/xg_for_mean')
        self.assertNotEqual(result['actual_canonical_sha256'],result['expected_canonical_sha256'])
        self.assertNotIn('PRIVATE_',json.dumps(result))
        actual['home']['xg_for_mean']=1
        result=audit.comparison_failure(actual,expected,audit.compare(actual,expected))
        self.assertTrue(result['first_mismatch'].startswith('/home/field_sha256_'))
        self.assertNotIn('PRIVATE_',json.dumps(result))
        self.assertFalse(result['mismatching_values_included'])

    def test_fixed_thresholds_are_predeclared_and_distinct(self):
        self.assertEqual(audit.TOLERANCES['refit_parameter_atol'],1e-8)
        self.assertEqual(audit.TOLERANCES['refit_parameter_rtol'],1e-8)
        self.assertEqual(audit.TOLERANCES['refit_prediction_atol'],1e-10)
        self.assertEqual(audit.TOLERANCES['refit_loglikelihood_per_game_atol'],1e-10)
        self.assertEqual(audit.TOLERANCES['native_saved_coefficient_probe_atol'],1e-10)
        self.assertEqual(audit.LIMITS['R_processes'],3)

    def test_full_native_feature_rebuild_and_tamper_detection(self):
        from datetime import datetime,timezone
        from football_model.advanced_research import study
        from football_model.fundamentals import make_snapshot,save_snapshot
        from tests.test_advanced_features import envelope
        from tests.test_feature_research import matches
        with tempfile.TemporaryDirectory() as temporary:
            base=Path(temporary);runtime=base/'runtime';(runtime/'data').mkdir(parents=True)
            records=matches(12)
            (runtime/'data/primeira.json').write_text(json.dumps({'matches':records}))
            for row in records:
                fixture={k:row[k] for k in ('match_id','kickoff','home_id','away_id')}
                fixture['status']='FT'
                for kind in ('statistics','lineups'):
                    save_snapshot(runtime/'fundamentals/snapshots',make_snapshot(kind,envelope(kind,fixture),fixture))
            protocol=json.loads((ROOT/'advanced-models-v1.json').read_text())
            report=study(runtime,'primeira',protocol,source_commit='engineering-test-only',
                clock=lambda:datetime(2024,3,1,tzinfo=timezone.utc))
            manifest=audit.materialize_inputs(report,runtime,base/'copy',lambda p,**_:p.read_bytes())
            actual,features,receipt=audit.reconstruct_features(report,base/'copy')
            self.assertEqual(len(actual),12)
            self.assertEqual(set(receipt),{'EARLY','LINEUP'})
            self.assertTrue(all(r['numeric_and_admitted_provenance']['passed'] for r in receipt.values()))
            self.assertTrue(any(row['home']['xg_for_mean'] is not None for row in features['EARLY'].values()))
            changed=copy.deepcopy(report)
            changed['feature_datasets']['EARLY']['rows'][0]['home']['xg_for_mean']=0
            with self.assertRaisesRegex(audit.DeepAuditError,'raw_feature'):
                audit.reconstruct_features(changed,base/'copy')
            changed=copy.deepcopy(report)
            changed['feature_datasets']['EARLY']['rows'][0]['optional_diagnostic_refs']=[{'observed_path':'unfrozen','observed_sha256':'0'*64}]
            _,_,receipt=audit.reconstruct_features(changed,base/'copy')
            self.assertFalse(receipt['EARLY']['diagnostic_fields_equal'])
            self.assertFalse(receipt['EARLY']['complete_original_discovery_universe_verified'])
            audit.stable_input_tree(base/'copy',manifest)

    def test_expanded_size_has_a_real_limit(self):
        from unittest.mock import patch
        with patch.dict(audit.LIMITS,{'expanded_report_bytes':10}):
            self.assertRaisesRegex(audit.DeepAuditError,'expanded_report_limit',audit.bounded_expanded_size,{'large':'x'*100})

    def test_symlink_swap_after_source_read_cannot_create_outside(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);source=root/'source';source.mkdir();outside=root/'outside';outside.mkdir()
            (source/'a').write_bytes(b'{}');destination=root/'copy'
            ref={'path':'a','original_path':'data/test.json','sha256':audit.digest(b'{}')}
            def reader(path,**_):
                (destination/'data').symlink_to(outside,target_is_directory=True)
                return path.read_bytes()
            with self.assertRaises((OSError,audit.DeepAuditError)):
                audit.materialize_inputs({'source_refs':[ref]},source,destination,reader)
            self.assertFalse((outside/'test.json').exists())

    def test_same_bytes_symlink_is_not_stable_original(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);source=root/'source';source.mkdir();destination=root/'copy'
            (source/'a').write_bytes(b'{}');outside=root/'outside.json';outside.write_bytes(b'{}')
            ref={'path':'a','original_path':'data/test.json','sha256':audit.digest(b'{}')}
            manifest=audit.materialize_inputs({'source_refs':[ref]},source,destination,lambda p,**_:p.read_bytes())
            target=destination/'data/test.json';target.unlink();target.symlink_to(outside)
            with self.assertRaisesRegex(audit.DeepAuditError,'symlink'):
                audit.stable_input_tree(destination,manifest)

if __name__=='__main__':unittest.main()
