import copy
from datetime import datetime, timezone
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from football_model.fundamentals import build_features, make_snapshot
from scripts.fixed_fundamentals_feature_audit import identity_from_context, audit, FeatureAuditError

TARGET=dict(match_id=1557430,home_id=45,away_id=49,kickoff='2026-10-17T11:30:00Z',status='NS')
PRIOR=dict(match_id=1557400,home_id=45,away_id=49,kickoff='2026-10-10T11:30:00Z',status='FT')
CUTOFF='2026-10-17T06:00:00Z'

def context(fixture):
    return dict(coverage_observed_at='2026-10-17T05:00:00Z',league_id=39,season=2026,
        fixture_response=dict(endpoint='/fixtures',params={'id':fixture['match_id']},errors=[],
            fetched_at='2026-10-17T05:30:00Z',response=[dict(fixture=dict(id=fixture['match_id'],
            date=fixture['kickoff'],status={'short':fixture['status']}),league={'id':39,'season':2026},
            teams={'home':{'id':45},'away':{'id':49}})]))

def fixture_runtime(root):
    raw=[{'team':{'id':45},'statistics':[{'type':'expected_goals','value':'1.20'}]},
         {'team':{'id':49},'statistics':[{'type':'expected_goals','value':'0.80'}]}]
    snap=make_snapshot('statistics',dict(endpoint='/fixtures/statistics',params={'fixture':PRIOR['match_id']},
        response=raw,fetched_at='2026-10-17T05:31:00Z'),PRIOR,context_observed_at='2026-10-17T05:30:00Z')
    relative=f'fundamentals/snapshots/statistics/{PRIOR["match_id"]}/{snap["snapshot_id"]}.json'
    path=root/relative;path.parent.mkdir(parents=True);path.touch()
    fixtures=[f'fundamentals/fixtures/{f["match_id"]}/{str(i)*64}.json' for i,f in enumerate((TARGET,PRIOR),1)]
    feature=build_features(TARGET,cutoff=CUTOFF,snapshots=[snap],fixture_history=[
        {**PRIOR,'observed_at':'2026-10-17T05:30:00Z'}],recent_matches=5)
    feature['collection_provenance']={'fixture_context_paths':fixtures,'run_id':'facts1','forecast_run_id':'forecast1'}
    feature_path='fundamentals/features/1557430/'+('a'*64)+'.json'
    entry=dict(match_id=1557430,league='epl',status='features_saved',feature_path=feature_path,
        feature_cutoff=CUTOFF,fixture_context_paths=fixtures,snapshot_paths=[relative],forecast_run_id='forecast1')
    return {'fundamentals/reports/latest.json':{'matches':[entry],'run_id':'facts1'},feature_path:feature,
        fixtures[0]:context(TARGET),fixtures[1]:context(PRIOR),relative:snap},feature_path,relative

class FixedRawFeatureTests(unittest.TestCase):
    def test_fixture_raw_identity(self):
        self.assertEqual(identity_from_context(context(TARGET),1557430,CUTOFF)['home_id'],45)

    def test_future_observation_rejected(self):
        obj=context(TARGET);obj['fixture_response']['fetched_at']='2026-10-17T07:00:00Z'
        with self.assertRaisesRegex(FeatureAuditError,'observed_after'):
            identity_from_context(obj,1557430,CUTOFF)

    def test_metadata_identity_rejected(self):
        obj=context(TARGET);obj['season']=2025
        with self.assertRaisesRegex(FeatureAuditError,'coverage_identity'):
            identity_from_context(obj,1557430,CUTOFF)

    def test_pagination_rejected(self):
        obj=context(TARGET);obj['fixture_response']['paging']={'current':1,'total':2}
        with self.assertRaisesRegex(FeatureAuditError,'pagination'):
            identity_from_context(obj,1557430,CUTOFF)

    def test_timestamp_conflict_rejected(self):
        obj=context(TARGET);obj['fixture_response']['response'][0]['fixture']['timestamp']=1
        with self.assertRaisesRegex(FeatureAuditError,'timestamp'):
            identity_from_context(obj,1557430,CUTOFF)

    def run_fixture(self, mutate=None):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder); values,feature_path,snapshot_path=fixture_runtime(root)
            if mutate:mutate(values,feature_path,snapshot_path)
            def reader(runtime,relative,evidence):
                evidence.append(dict(path=relative,bytes=1,sha256='0'*64))
                return copy.deepcopy(values[relative])
            with patch('scripts.fixed_fundamentals_feature_audit.read_original',reader):
                return audit(root,1557430)

    def test_raw_to_feature_reconstruction(self):
        result=self.run_fixture()
        self.assertEqual(result['status'],'completed_with_stated_scope')
        self.assertEqual(result['normalized_raw_snapshots'],1)
        self.assertFalse(result['live_lineup_available'])
        self.assertEqual(result['independent_raw_arithmetic']['45']['for_mean'],1.2)
        self.assertEqual(result['independent_raw_arithmetic']['45']['rest_days'],7)

    def test_saved_numeric_feature_tamper_rejected(self):
        with self.assertRaises(ValueError):
            self.run_fixture(lambda d,f,s:d[f]['teams']['45']['prior_xg'].update(for_mean=8))

    def test_raw_normalized_tamper_rejected(self):
        with self.assertRaises(ValueError):
            self.run_fixture(lambda d,f,s:d[s]['raw_response'][0]['statistics'][0].update(value='9'))

    def test_duplicate_context_rejected(self):
        def change(d,f,s):
            entry=d['fundamentals/reports/latest.json']['matches'][0]
            entry['fixture_context_paths'].append(entry['fixture_context_paths'][0])
        with self.assertRaisesRegex(FeatureAuditError,'duplicate_fixture'):
            self.run_fixture(change)

    def test_collection_run_mismatch_rejected(self):
        with self.assertRaises(ValueError):
            self.run_fixture(lambda d,f,s:d[f]['collection_provenance'].update(run_id='another-run'))

    def test_unlisted_snapshot_rejected(self):
        def change(d,f,s):
            d['fundamentals/reports/latest.json']['matches'][0]['snapshot_paths'].append('fundamentals/snapshots/statistics/99/missing.json')
        with self.assertRaisesRegex(FeatureAuditError,'reported_snapshot_not_consumed'):
            self.run_fixture(change)

if __name__=='__main__':unittest.main()

