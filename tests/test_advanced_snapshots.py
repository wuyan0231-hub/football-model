"""Advanced capture/score integrity tests use an actual fitted synthetic M0."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from football_model.advanced_snapshots import (capture_run, load_verified_comparison, verify_capture,
    _root, freeze_inputs, frozen_runtime, queue_members)
from football_model.advanced_scoring import score_run, verified_confirmation_rows
from football_model.advanced_research import implementation_fingerprint
from football_model.feature_snapshots import _digest, _read, SnapshotError
from football_model.model import _distribution_from_rates
from football_model.advanced_capacity import CapacityGuard, CapacityDeferred
from football_model.state_storage import estimate_runtime
from tests.test_cli import BASE_CONFIG, FakeClient as TrainingClient
from tests.test_feature_scoring import FakeClient

TRAINED=datetime(2025,1,1,tzinfo=timezone.utc)
CAPTURE=TRAINED+timedelta(hours=38)
SHA="a"*40
PROTOCOL={"schema_version":"advanced-model-protocol-v1","candidates":{"M_test":{"horizon":"EARLY"}},"goalmodel_candidates":["G0_full"],"confirmation_active":False}


def iso(t): return t.isoformat().replace("+00:00","Z")
def write(p,value):
    p.parent.mkdir(parents=True,exist_ok=True);p.write_text(json.dumps(value,sort_keys=True));return p

def ref(root,p): return {"path":p.relative_to(root).as_posix(),"sha256":hashlib.sha256(p.read_bytes()).hexdigest()}


class AdvancedSnapshotTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from football_model.cli import run_pipeline
        cls.base=tempfile.TemporaryDirectory()
        with patch("football_model.integration.datetime") as dt:
            dt.now.return_value=TRAINED
            code,report=run_pipeline(BASE_CONFIG,Path(cls.base.name),"train",now=TRAINED,client=TrainingClient(),clock=lambda:TRAINED)
        assert code==0,report
        cls.report=report
    @classmethod
    def tearDownClass(cls): cls.base.cleanup()
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.runtime=Path(self.temp.name)/"runtime";shutil.copytree(self.base.name,self.runtime)
        self.row=_read(self.runtime/"predictions"/self.report["run_id"]/"epl.json")[0]
        self.identity={"fixture":{"id":self.row["match_id"],"date":self.row["kickoff"],"status":{"short":"NS"}},
            "teams":{"home":{"id":self.row["home_id"],"name":self.row["home_name"]},"away":{"id":self.row["away_id"],"name":self.row["away_name"]}},
            "league":{"id":999,"name":"Synthetic League","country":"Test"}}
        write(self.runtime/"fundamentals/fixtures/2000/current.json",{"fixture_response":{"endpoint":"/fixtures","params":{"id":2000},"errors":[],"fetched_at":iso(CAPTURE),"response":[self.identity]}})
        self.card=write(self.runtime/"test-card.json",{"beta":.1,"generated_at":iso(TRAINED),"implementation_fingerprint":implementation_fingerprint()})
        self.kickoff=datetime.fromisoformat(self.row["kickoff"].replace("Z","+00:00"))
    def live(self,runtime,league,protocol,baseline,features,cutoff,fixture):
        root=Path(runtime);p=root/"test-card.json";card=_read(p)
        prediction=_distribution_from_rates(1.4+card["beta"],1.1)
        return {"predictions":{"M_test":prediction},"failures":{"G0_full":"no_card"},"source_refs":[ref(root,p)],"cards":{"M_test":ref(root,p)}}
    def capture(self,at=CAPTURE,live=None,capacity_guard=None):
        with patch("football_model.integration.datetime") as dt,patch("football_model.advanced_snapshots._live",side_effect=live or self.live):
            dt.now.return_value=at
            return capture_run(self.runtime,"epl",source_commit=SHA,protocol=PROTOCOL,config=BASE_CONFIG,clock=lambda:at,
                capacity_guard=capacity_guard)
    def comparison(self,now=CAPTURE,**kw):
        with patch("football_model.advanced_snapshots._live",side_effect=self.live):
            return load_verified_comparison(self.runtime,"epl",2000,"EARLY",PROTOCOL,now=now,**kw)
    def score(self,now=None,envelope=None):
        now=now or self.kickoff+timedelta(hours=5)
        raw=deepcopy(self.identity);raw["fixture"]["status"]["short"]="FT";raw["score"]={"fulltime":{"home":2,"away":1}}
        envelope=envelope or {"endpoint":"/fixtures","params":{"id":2000},"errors":[],"fetched_at":iso(now),"response":[raw]}
        client=FakeClient(envelope)
        with patch("football_model.advanced_snapshots._live",side_effect=self.live):
            result=score_run(self.runtime,"epl",config=BASE_CONFIG,protocol=PROTOCOL,source_commit=SHA,clock=lambda:now,client_factory=lambda *a,**k:client)
        return (*result,client)
    def test_real_baseline_and_numeric_candidate_recomputed_from_frozen_sources(self):
        code,report=self.capture();self.assertEqual(code,0,report)
        got=self.comparison();self.assertEqual(got["status"],"available",got)
        self.assertEqual(got["candidates"]["M_test"]["status"],"verified",got)
        self.assertEqual(got["baseline_parent_row"],self.row)
        write(self.card,{"beta":8,"generated_at":iso(TRAINED)})
        self.assertEqual(self.comparison()["candidates"]["M_test"]["prediction"],got["candidates"]["M_test"]["prediction"])
    def test_changed_parent_row_is_rejected_and_all_candidate_failures_retained(self):
        self.capture();bad=deepcopy(self.row);bad["invented"]=True
        got=self.comparison(row=bad);self.assertEqual(got["status"],"unavailable")
        self.assertEqual(set(got["candidates"]),{"M_test","G0_full"})
    def test_candidate_source_damage_keeps_independently_verified_m0(self):
        self.capture();got=self.comparison();source=next(x for x in got["payload"]["source_refs"] if x["origin_runtime_path"]=="test-card.json")
        (self.runtime/source["runtime_relative_path"]).write_text('{}')
        got=self.comparison();self.assertEqual(got["status"],"available")
        self.assertEqual(got["candidates"]["M_test"]["status"],"rejected")
        self.assertEqual(got["baseline"]["prediction"],self.row["prediction"])
    def test_first_legal_baseline_preserves_candidate_failure_despite_later_success(self):
        def missing(*a,**k):return {"predictions":{},"failures":{"M_test":"first_fit_failed"},"source_refs":[],"cards":{}}
        self.capture(live=missing);first=(_root(self.runtime,PROTOCOL)/"captures/epl/2000/EARLY/first.json").read_bytes()
        self.capture(CAPTURE+timedelta(minutes=5));self.assertEqual((_root(self.runtime,PROTOCOL)/"captures/epl/2000/EARLY/first.json").read_bytes(),first)
        got=self.comparison(CAPTURE+timedelta(minutes=5));self.assertEqual(got["candidates"]["M_test"]["status"],"unavailable")
    def test_early_future_or_late_captures_not_eligible(self):
        self.capture();got=self.comparison(CAPTURE-timedelta(seconds=1));self.assertEqual(got["status"],"unavailable")
        root=self.runtime/"other";root.mkdir()
        with self.assertRaisesRegex(SnapshotError,"research_replay"):
            write(self.runtime/"RESEARCH_REPLAY.json",{})
            capture_run(self.runtime,"epl",source_commit=SHA,protocol=PROTOCOL,config=BASE_CONFIG,clock=lambda:CAPTURE)
    def test_full_queue_includes_other_horizon_without_capture(self):
        self.capture();members=queue_members(self.runtime,"epl",PROTOCOL,CAPTURE)
        self.assertEqual(len(members),2);self.assertEqual({v["horizon"] for v in members},{"EARLY","LINEUP"})
        code,report,client=self.score(now=CAPTURE);self.assertEqual(client.requests_made,0)
        self.assertEqual(report["requested_horizon_records"],2)
    def test_independent_regulation_scoring_fallback_and_idempotence(self):
        self.capture();code,report,client=self.score();self.assertEqual(code,0,report)
        self.assertEqual(report["scored_horizon_records"],1,report)
        summary=report["summaries"]["EARLY"]
        self.assertEqual(summary["M_test"]["paired_scored"],1)
        self.assertEqual(summary["G0_full"]["fallback_scored"],1)
        _,again,client=self.score();self.assertEqual(client.requests_made,0)
        self.assertEqual(again["summaries"],report["summaries"])
    def test_damaged_scored_candidate_is_not_reclassified_to_baseline_fallback(self):
        self.capture();self.score();got=self.comparison();source=next(x for x in got["payload"]["source_refs"] if x["origin_runtime_path"]=="test-card.json")
        p=self.runtime/source["runtime_relative_path"];original=p.read_bytes();p.write_text('{}')
        _,report,_=self.score();summary=report["summaries"]["EARLY"]["M_test"]
        self.assertEqual(summary["baseline_scored"],1);self.assertEqual(summary["policy_scored"],0)
        self.assertEqual(summary["fallback_scored"],0)
        p.write_bytes(original);_,restored,_=self.score();self.assertEqual(restored["summaries"]["EARLY"]["M_test"]["paired_scored"],1)
    def test_metadata_only_confirmation_adapter_never_reads_score_or_result(self):
        self.capture();self.score()
        import football_model.advanced_scoring as scoring
        original=scoring._read_ref
        def guarded(root,reference):
            self.assertNotIn('advanced_scoring/',reference['path']);return original(root,reference)
        with patch("football_model.advanced_snapshots._live",side_effect=self.live),patch.object(scoring,"_read_ref",side_effect=guarded):
            rows=verified_confirmation_rows(self.runtime,"epl",PROTOCOL,include_scores=False,config=BASE_CONFIG,now=self.kickoff+timedelta(hours=5))
        self.assertEqual(len(rows),4);self.assertTrue(all(r["score_ref"] is None and r["baseline_metrics"] is None for r in rows))
    def test_bad_result_identity_retains_failure_without_score(self):
        self.capture();now=self.kickoff+timedelta(hours=5);raw=deepcopy(self.identity);raw["fixture"]["id"]=999
        envelope={"endpoint":"/fixtures","params":{"id":2000},"errors":[],"fetched_at":iso(now),"response":[raw]}
        code,report,_=self.score(envelope=envelope);self.assertEqual(code,1);self.assertEqual(report["scored_horizon_records"],0)
    def test_freeze_rejects_unsafe_parent_path(self):
        with self.assertRaisesRegex(SnapshotError,"unsafe"):
            freeze_inputs(self.runtime,_root(self.runtime,PROTOCOL),[{"path":"../secret","sha256":"a"*64}],CAPTURE)

    def test_freeze_admission_precedes_copy_and_checks_both_hard_limits(self):
        source=write(self.runtime/'large-training.json',{'unchanged_training':'x'*8192})
        original=source.read_bytes();before=estimate_runtime(self.runtime)
        for limit in ('logical','physical'):
            with self.subTest(limit=limit):
                guard=CapacityGuard(self.runtime,reserve_logical=0,reserve_physical=0,
                    max_logical=before['logical_bytes']+(64 if limit=='logical' else 100000),
                    max_physical=before['persisted_bytes']+(64 if limit=='physical' else 100000))
                with self.assertRaises(CapacityDeferred):
                    freeze_inputs(self.runtime,_root(self.runtime,PROTOCOL),[ref(self.runtime,source)],CAPTURE,
                        capacity_guard=guard)
                self.assertEqual(estimate_runtime(self.runtime),before)
                self.assertEqual(source.read_bytes(),original)
                self.assertFalse(list((_root(self.runtime,PROTOCOL)/'sources').glob('*')))

    def test_rejected_future_freeze_leaves_no_unaccounted_source(self):
        source=write(self.runtime/'future-source.json',{'observed_at':iso(CAPTURE+timedelta(seconds=1))})
        before=estimate_runtime(self.runtime);guard=CapacityGuard(self.runtime)
        with self.assertRaises(SnapshotError):
            freeze_inputs(self.runtime,_root(self.runtime,PROTOCOL),[ref(self.runtime,source)],CAPTURE,
                capacity_guard=guard)
        self.assertEqual(estimate_runtime(self.runtime),before)
        self.assertEqual(guard.logical,before['logical_bytes'])
        self.assertEqual(guard.physical,before['persisted_bytes'])

    def test_capacity_deferral_keeps_full_queue_first_failure_and_safe_accounting(self):
        before=estimate_runtime(self.runtime)
        guard=CapacityGuard(self.runtime,max_logical=before['logical_bytes']+256*1024,
            reserve_logical=256*1024,reserve_physical=0)
        code,report=self.capture(capacity_guard=guard)
        self.assertEqual(code,0);self.assertEqual(report['execution_status'],'deferred')
        self.assertEqual(report['planned_horizon_count'],2)
        early=next(item for item in report['queue'] if item['horizon']=='EARLY')
        self.assertEqual(early['status'],'capacity_deferred')
        self.assertEqual(early['reason'],'storage_capacity_deferred')
        members=queue_members(self.runtime,'epl',PROTOCOL,CAPTURE)
        self.assertEqual(len(members),2)
        first=next(item for item in members if item['horizon']=='EARLY')['first_failures']
        self.assertEqual(len(first),1)
        self.assertEqual(json.loads((self.runtime/first[0]['path']).read_text())['status'],'capacity_deferred')
        root=_root(self.runtime,PROTOCOL)
        self.assertFalse(list((root/'sources').glob('*')))
        measured=estimate_runtime(self.runtime)
        self.assertEqual(guard.logical,measured['logical_bytes'])
        self.assertGreaterEqual(guard.physical,measured['persisted_bytes'])
        self.assertLessEqual(guard.physical,guard.max_physical)
        attempt=(root/'captures/epl/2000/EARLY/first_attempt.json').read_bytes()
        self.capture(CAPTURE+timedelta(minutes=1))
        self.assertEqual((root/'captures/epl/2000/EARLY/first_attempt.json').read_bytes(),attempt)
        self.assertEqual(next(item for item in queue_members(self.runtime,'epl',PROTOCOL,CAPTURE+timedelta(minutes=1))
            if item['horizon']=='EARLY')['first_failures'],first)
        self.assertLessEqual(guard.logical,guard.max_logical)
        self.assertLessEqual(guard.physical,guard.max_physical)

    def test_candidate_capacity_failure_preserves_m0_and_original_unavailability(self):
        source=write(self.runtime/'large-training.json',{'training':'x'*(2*1024*1024)})
        original=source.read_bytes();before=estimate_runtime(self.runtime)
        guard=CapacityGuard(self.runtime,max_logical=before['logical_bytes']+512*1024,
            reserve_logical=256*1024,reserve_physical=0)
        def large(*args,**kwargs):
            result=self.live(*args,**kwargs)
            result['source_refs'].append(ref(self.runtime,source))
            return result
        code,report=self.capture(live=large,capacity_guard=guard)
        self.assertEqual(code,0);self.assertEqual(report['execution_status'],'deferred')
        self.assertEqual(report['planned_horizon_count'],2)
        got=self.comparison();self.assertEqual(got['status'],'available')
        self.assertEqual(got['baseline']['prediction'],self.row['prediction'])
        self.assertEqual(got['candidates']['M_test']['status'],'unavailable')
        self.assertEqual(got['candidates']['M_test']['reason'],'storage_capacity_deferred')
        self.assertEqual(source.read_bytes(),original)
        self.assertFalse((_root(self.runtime,PROTOCOL)/'sources'/(hashlib.sha256(original).hexdigest()+'.json')).exists())
        measured=estimate_runtime(self.runtime)
        self.assertEqual(guard.logical,measured['logical_bytes'])
        self.assertGreaterEqual(guard.physical,measured['persisted_bytes'])
        self.assertLessEqual(guard.physical,guard.max_physical)
        self.assertLessEqual(guard.logical,guard.max_logical)
        first=(_root(self.runtime,PROTOCOL)/'captures/epl/2000/EARLY/first.json').read_bytes()
        self.capture(CAPTURE+timedelta(minutes=1))
        self.assertEqual((_root(self.runtime,PROTOCOL)/'captures/epl/2000/EARLY/first.json').read_bytes(),first)
        self.assertEqual(self.comparison(CAPTURE+timedelta(minutes=1))['candidates']['M_test']['reason'],'storage_capacity_deferred')

    def test_diagnostic_reserve_never_bypasses_hard_limit(self):
        before=estimate_runtime(self.runtime)
        guard=CapacityGuard(self.runtime,max_logical=before['logical_bytes'],reserve_logical=0,reserve_physical=0)
        with self.assertRaises(CapacityDeferred):
            self.capture(capacity_guard=guard)
        self.assertEqual(estimate_runtime(self.runtime),before)
    def test_result_revision_appended_without_changing_first(self):
        self.capture();self.score();root=self.runtime/'advanced_scoring'/_digest(PROTOCOL)/'scores/epl/2000/EARLY'
        original=(root/'first.json').read_bytes();now=self.kickoff+timedelta(days=2)
        raw=deepcopy(self.identity);raw['fixture']['status']['short']='FT';raw['score']={'fulltime':{'home':1,'away':1}}
        envelope={'endpoint':'/fixtures','params':{'id':2000},'errors':[],'fetched_at':iso(now),'response':[raw]}
        code,report,_=self.score(now=now,envelope=envelope);self.assertEqual(code,0,report)
        self.assertEqual((root/'first.json').read_bytes(),original);self.assertNotEqual((root/'latest.json').read_bytes(),original)

    def test_capture_time_rejected_candidate_uses_fallback_not_later_repair(self):
        calls=[0]
        def inconsistent(*args,**kwargs):
            result=self.live(*args,**kwargs);calls[0]+=1
            if calls[0]==1:result["predictions"]["M_test"]=_distribution_from_rates(9.0,1.1)
            return result
        self.capture(live=inconsistent)
        comparison=self.comparison();self.assertEqual(comparison["original_candidate_statuses"]["M_test"],"rejected")
        self.assertNotIn("prediction",comparison["candidates"]["M_test"])
        _,report,_=self.score();self.assertEqual(report["summaries"]["EARLY"]["M_test"]["fallback_scored"],1)
    def test_current_capture_damage_preserves_historical_score_reference(self):
        self.capture();self.score();got=self.comparison()
        (self.runtime/got["receipt_ref"]["path"]).write_text('{}')
        _,report,_=self.score();self.assertEqual(report["scored_horizon_records"],0)
        self.assertEqual(report["retained_historical_first_score_count"],1)
    def test_confirmation_tag_frozen_before_capture_and_no_retroactive_tagging(self):
        self.capture();first=self.comparison();self.assertFalse(first["candidates"]["M_test"].get("confirmation_eligible",False))
        activation={"league":"epl","candidate_id":"M_test","horizon":"EARLY","advanced_protocol_sha256":_digest(PROTOCOL),
            "created_at":iso(CAPTURE),"future_start":iso(CAPTURE),"last_kickoff":iso(self.kickoff+timedelta(days=30)),
            "implementation_fingerprint":implementation_fingerprint()}
        p=write(self.runtime/'activation-source.json',activation)
        write(self.runtime/'advanced_models/epl'/_digest(PROTOCOL)/'confirmation/design/M_test/EARLY/activation.json',ref(self.runtime,p))
        self.capture(CAPTURE+timedelta(minutes=1));self.assertFalse(self.comparison(CAPTURE+timedelta(minutes=1))["candidates"]["M_test"].get("confirmation_eligible",False))
    def test_candidate_only_unknown_team_is_scored_without_m0_or_paired_claim(self):
        global PROTOCOL
        original=PROTOCOL;PROTOCOL={**PROTOCOL,"cold_start_candidate":"COLD_START"}
        self.addCleanup(lambda:globals().__setitem__("PROTOCOL",original))
        from football_model.cli import run_pipeline
        client=TrainingClient();client.fixtures[-1]["teams"]["home"]={"id":999,"name":"New Club"}
        with patch("football_model.integration.datetime") as dt:
            later=TRAINED+timedelta(minutes=1)
            dt.now.return_value=later
            code,report=run_pipeline(BASE_CONFIG,self.runtime,"train",now=later,client=client,clock=lambda:later)
        self.assertEqual(code,0,report)
        self.identity['teams']['home']={'id':999,'name':'New Club'}
        write(self.runtime/'fundamentals/fixtures/2000/current.json',{"fixture_response":{"endpoint":"/fixtures","params":{"id":2000},"errors":[],"fetched_at":iso(CAPTURE),"response":[self.identity]}})
        code,report=self.capture();self.assertEqual(code,0,report)
        got=self.comparison();self.assertEqual(got['status'],'available',report)
        self.assertIsNone(got['baseline']['prediction'])
        self.assertEqual(got['candidates']['COLD_START']['status'],'verified')
        code,report,_=self.score();self.assertEqual(code,0,report)
        stats=report['summaries']['EARLY']['COLD_START'];self.assertEqual(stats['candidate_only_scored'],1)
        self.assertEqual(stats['paired_scored'],0);self.assertEqual(stats['policy_scored'],0)


    def test_actual_saved_correction_card_capture_without_predictor_mock(self):
        from football_model.advanced_research import study
        protocol=_read(Path(__file__).resolve().parents[1]/'advanced-models-domestic-v1.json')
        protocol.update(candidates={'M_schedule':protocol['candidates']['M_schedule']},goalmodel_candidates=[],
            goalmodel_feature_candidate='M_schedule',family_increment_comparisons=[],baseline_min_train=60,
            correction_min_matches=10,correction_alpha_grid=[1.0])
        report=study(self.runtime,'epl',protocol,source_commit=SHA,clock=lambda:TRAINED)
        with patch('football_model.integration.datetime') as dt:
            dt.now.return_value=CAPTURE
            code,captured=capture_run(self.runtime,'epl',source_commit=SHA,protocol=protocol,config=BASE_CONFIG,clock=lambda:CAPTURE)
        self.assertEqual(code,0,captured)
        got=load_verified_comparison(self.runtime,'epl',2000,'EARLY',protocol,now=CAPTURE)
        self.assertEqual(got['status'],'available',got)
        self.assertEqual(got['candidates']['M_schedule']['status'],'verified',got['candidates'])


    def test_damaged_confirmation_activation_keeps_m0_and_clears_formal_eligibility(self):
        activation={"league":"epl","candidate_id":"M_test","horizon":"EARLY","advanced_protocol_sha256":_digest(PROTOCOL),
            "created_at":iso(CAPTURE),"future_start":iso(CAPTURE),"last_kickoff":iso(self.kickoff+timedelta(days=30)),
            "implementation_fingerprint":implementation_fingerprint()}
        p=write(self.runtime/'activation-source.json',activation)
        write(self.runtime/'advanced_models/epl'/_digest(PROTOCOL)/'confirmation/design/M_test/EARLY/activation.json',ref(self.runtime,p))
        self.capture();before=self.comparison();self.assertTrue(before['candidates']['M_test']['confirmation_eligible'])
        write(self.card,{'beta':9,'implementation_fingerprint':{'sha256':'new-latest-card'}})
        self.assertTrue(self.comparison()['candidates']['M_test']['confirmation_eligible'])
        p.write_text('{}');after=self.comparison();self.assertEqual(after['status'],'available')
        self.assertFalse(after['candidates']['M_test']['confirmation_eligible'])
        self.assertEqual(after['candidates']['M_test']['status'],'verified')
    def test_confirmation_implementation_drift_blocks_candidate_and_baseline_fallback(self):
        activation={"league":"epl","candidate_id":"M_test","horizon":"EARLY","advanced_protocol_sha256":_digest(PROTOCOL),
            "created_at":iso(CAPTURE),"future_start":iso(CAPTURE),"last_kickoff":iso(self.kickoff+timedelta(days=30)),
            "implementation_fingerprint":{"sha256":"old-implementation"}}
        p=write(self.runtime/'activation-source.json',activation)
        write(self.runtime/'advanced_models/epl'/_digest(PROTOCOL)/'confirmation/design/M_test/EARLY/activation.json',ref(self.runtime,p))
        self.capture();got=self.comparison()
        self.assertEqual(got['status'],'available')
        self.assertEqual(got['candidates']['M_test']['status'],'verified')
        self.assertFalse(got['candidates']['M_test']['confirmation_eligible'])
        from football_model.advanced_snapshots import _confirmation_tag
        tag=_confirmation_tag(self.runtime,'epl',PROTOCOL,'M_test','EARLY',got['fixture'],CAPTURE,{"status":"unavailable"})
        self.assertFalse(tag['confirmation_eligible'])
        activation['implementation_fingerprint']=implementation_fingerprint();write(p,activation)
        write(self.runtime/'advanced_models/epl'/_digest(PROTOCOL)/'confirmation/design/M_test/EARLY/activation.json',ref(self.runtime,p))
        write(self.card,{"beta":.1,"generated_at":iso(TRAINED),"implementation_fingerprint":{"sha256":"old-card"}})
        tag=_confirmation_tag(self.runtime,'epl',PROTOCOL,'M_test','EARLY',got['fixture'],CAPTURE,{"model_ref":ref(self.runtime,self.card)})
        self.assertFalse(tag['confirmation_eligible'])

    def test_active_formal_trial_hides_repeated_aggregate_score_tests(self):
        self.capture()
        write(self.runtime/'advanced_models/epl'/_digest(PROTOCOL)/'confirmation/design/M_test/EARLY/activation.json',{})
        _,report,_=self.score();self.assertEqual(report['summaries']['EARLY']['M_test']['status'],'suppressed_during_registered_confirmation')
        self.assertNotIn('mean_paired_scoreline_nll_difference',report['summaries']['EARLY']['M_test'])
    def test_saved_strategy_score_independently_revalidated(self):
        from football_model.advanced_scoring import verified_score
        from football_model.feature_scoring import _read_ref
        self.capture();_,report,_=self.score();item=next(x for x in report['queue'] if x.get('current_score_ref'))
        saved=_read_ref(self.runtime,item['current_score_ref']);saved['scores']['strategy']['M_test']['scoreline_nll']+=1
        with self.assertRaisesRegex(SnapshotError,'strategy_score'):
            verified_score(self.runtime,saved,self.comparison(),BASE_CONFIG['competitions'][0],self.kickoff+timedelta(hours=5))


    def test_entire_hash_reference_closure_uses_frozen_not_mutable_inputs(self):
        self.capture();got=self.comparison()
        write(self.runtime/'data/epl.json',{'changed':'later'})
        write(self.card,{'changed':'later'})
        visited=set()
        def walk_ref(reference):
            relative=reference.get('runtime_relative_path',reference['path'])
            path=Path(relative)
            if not path.is_absolute():path=self.runtime/path
            key=(str(path),reference['sha256'])
            if key in visited:return
            visited.add(key)
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(),reference['sha256'],str(path))
            if path.suffix=='.json':walk(_read(path))
        def walk(value):
            if isinstance(value,dict):
                if isinstance(value.get('path'),str) and isinstance(value.get('sha256'),str):walk_ref(value)
                for child in value.values():walk(child)
            elif isinstance(value,list):
                for child in value:walk(child)
        walk_ref(got['first_capture_anchor_ref']);self.assertGreater(len(visited),8)


if __name__=='__main__':unittest.main()
