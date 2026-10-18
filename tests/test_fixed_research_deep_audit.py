"""Safety/identity tests for new read-only audit. No real research R refits."""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
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
