"""Synthetic engineering fixtures, never prospective football evidence."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from football_model.evidence import EvidenceError
from football_model.policy_evaluation import (register_policy_protocol, read_policy_protocol,
    evaluate_policy_comparison, cluster_interval)

BASE = datetime(2030, 1, 1, tzinfo=timezone.utc)


def stamp(hours=0):
    return (BASE + timedelta(hours=hours)).isoformat()


def spec():
    return {"experiment_id": "synthetic-only", "scope_description": "synthetic engineering tests",
        "comparison": "same_ticket_grading_and_action", "version_selection": "first_actual_output_per_request",
        "quote_protocol": "QUOTE_REF", "usage": "single", "abstention_zero_exposure": True,
        "primary_objective": "synthetic matched delta", "allowed_degradation": "not an acceptance study",
        "review_schedule": "fixed synthetic review", "stopping_rule": "no automatic promotion",
        "policies": {"left": {"policy_id": "formal-r5", "view": "formal"},
            "right": {"policy_id": "shadow-v1", "policy_version": "1", "view": "shadow", "included_grades": ["A", "B"]}},
        "requests": [{"request_id": "req1", "competition": "synthetic", "match_id": 1, "home_id": 10,
            "away_id": 20, "kickoff": stamp(5), "analysis_cutoff": stamp(1)}]}


def score():
    return {"schema_version": "s2-decision-score-1.0", "binding": {
        **{k:v for k,v in spec()["requests"][0].items() if k != "request_id"},
        "decision_id": "d1", "version_id": "r1", "policy_id": "formal-r5", "forecast_id": "forecast1"},
        "decision_times": {"report_generated_at": stamp(2)}, "qualification": "prospective_decision_candidate",
        "source": {"decision_manifest_sha256": "c"*64, "result_sha256": "d"*64},
        "source_pool": {"input_pool_sha256": "i"*64, "quote_pool_sha256": "q"*64, "model_sha256s": ["m"*64]},
        "contract": {"market": "total_goals", "side": "under", "line": 2.5},
        "price_decimal": 1.95, "cost_per_unit": 0.0, "quote_protocol": "QUOTE_REF", "usage": "single",
        "formal_fit": {"single": "适合", "parlay_leg": "适合"},
        "shadow_grading": {"policy_id": "shadow-v1", "policy_version": "1", "evaluation_context": "current",
            "single": {"grade": "B", "action_status": "candidate"}},
        "probability": {"metrics": {"one_x_two": {"brier_score": .6, "log_loss": 1.0}, "five_state": {"brier_score": .4, "log_loss": .7}}},
        "outcome": {"settlement_state": "full_win", "paper_reference_return": .95,
            "open_simulated_return": None, "actual_single_return": None}}


class PolicyEvaluationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.spec = spec()
        self.scores = {"left": score(), "right": score()}

    def tearDown(self):
        self.tmp.cleanup()

    def register(self):
        with patch("football_model.policy_evaluation._now", return_value=BASE):
            return register_policy_protocol(self.spec, self.root)["protocol_id"]

    def compare(self, inputs=None, pid=None):
        pid = pid or self.register()
        refs = lambda name: [{"path": name, "manifest_sha256": ("a" if name == "left" else "b")*64}]
        inputs = inputs if inputs is not None else [{"request_id": "req1", "left": refs("left"), "right": refs("right")}]
        with patch("football_model.policy_evaluation._now", return_value=BASE+timedelta(hours=3)), \
             patch("football_model.policy_evaluation._read_score", side_effect=lambda ref: deepcopy(self.scores[ref["path"]])):
            return evaluate_policy_comparison(self.root, pid, inputs)

    def test_protocol_actual_clock_and_immutable_hash(self):
        pid=self.register()
        record=read_policy_protocol(self.root,pid)
        self.assertEqual(record['qualification'],'prospective_preregistered')
        self.assertEqual(record['registered_at'],'2030-01-01T00:00:00Z')
        with self.assertRaisesRegex(EvidenceError,'no_overwrite'):
            self.register()
        (self.root/(pid+'.json')).write_text('{}')
        with self.assertRaisesRegex(EvidenceError,'hash_mismatch'):
            read_policy_protocol(self.root,pid)

    def test_past_registration_remains_research(self):
        self.spec['requests'][0]['analysis_cutoff']=stamp(-1)
        self.assertEqual(read_policy_protocol(self.root,self.register())['qualification'],'research_only')

    def test_no_candidate_is_created_when_outputs_missing(self):
        result=self.compare([])
        self.assertEqual(result['request_count'],1)
        self.assertEqual(result['coverage'],{'unpaired':1})
        self.assertEqual(result['rows'][0]['left']['action'],'unknown')
        self.assertIsNone(result['rows'][0]['left']['strategy_return'])
        self.assertIsNone(result['prospective_paired_metrics']['strategy_return']['mean_difference'])

    def test_common_actual_same_ticket_output(self):
        result=self.compare()
        self.assertEqual(result['coverage'],{'common':1})
        self.assertEqual(result['prospective_pairs'],1)
        self.assertEqual(result['unique_matches'],1)
        self.assertEqual(result['prospective_paired_metrics']['strategy_return']['mean_difference'],0)
        self.assertIsNone(result['prospective_paired_metrics']['strategy_return']['interval_95'])

    def test_restored_coverage_has_declared_zero_exposure(self):
        self.scores['left']['formal_fit']['single']='不建议'
        result=self.compare()
        self.assertEqual(result['coverage'],{'restored_coverage':1})
        self.assertEqual(result['rows'][0]['left']['strategy_return'],0)
        self.assertEqual(result['rows'][0]['right']['strategy_return'],.95)

    def test_lost_coverage_from_actual_shadow_D(self):
        self.scores['right']['shadow_grading']['single']={'grade':'D','action_status':'not_recommended'}
        self.assertEqual(self.compare()['coverage'],{'lost_coverage':1})

    def test_unknowns_and_conditions_never_zero_exposure(self):
        self.scores['right']['shadow_grading']['single']={'grade':'U','action_status':'unable_to_judge'}
        result=self.compare()
        self.assertEqual(result['coverage'],{'unpaired':1})
        self.assertIsNone(result['rows'][0]['right']['strategy_return'])
        self.scores['right']['shadow_grading']['single']={'grade':None,'action_status':'awaiting_condition'}
        result=self.compare(pid=result['protocol_id'])
        self.assertEqual(result['rows'][0]['right']['action'],'conditional')
        self.assertIsNone(result['rows'][0]['right']['strategy_return'])

    def test_C_evidence_incomplete_not_zero(self):
        self.scores['right']['shadow_grading']['single']={'grade':'C','action_status':'watch_only'}
        result=self.compare()
        self.assertEqual(result['rows'][0]['right']['action'],'unknown')
        self.assertIsNone(result['rows'][0]['right']['strategy_return'])

    def test_missing_return_or_probability_remains_null(self):
        self.scores['left']['outcome']['paper_reference_return']=None
        self.scores['left']['probability']['metrics']['one_x_two']=None
        result=self.compare()
        metrics=result['prospective_paired_metrics']
        self.assertIsNone(metrics['strategy_return']['mean_difference'])
        self.assertIsNone(metrics['one_x_two_brier']['mean_difference'])
        self.assertEqual(metrics['five_state_brier']['mean_difference'],0)

    def test_same_cutoff_input_quote_model_and_contract_required(self):
        for field,value in [('input_pool_sha256','x'*64),('quote_pool_sha256','y'*64),('model_sha256s',['z'*64])]:
            with self.subTest(field=field):
                self.scores['right']=score()
                self.scores['right']['source_pool'][field]=value
                result=self.compare(pid=getattr(self,'pid',None))
                self.pid=result['protocol_id']
                self.assertEqual(result['coverage'],{'unpaired':1})
                self.assertIn(field+'_mismatch',result['rows'][0]['unpaired_reasons'])

    def test_price_or_cost_mismatch_not_policy_improvement(self):
        self.scores['right']['price_decimal']=2.0
        result=self.compare()
        self.assertIn('price_decimal_mismatch',result['rows'][0]['comparability']['reasons']['returns'])
        self.assertTrue(result['rows'][0]['comparability']['probability'])
        self.assertFalse(result['rows'][0]['comparability']['returns'])
        self.scores['right']=score(); self.scores['right']['cost_per_unit']=None
        result=self.compare(pid=result['protocol_id'])
        self.assertIn('cost_per_unit_missing',result['rows'][0]['comparability']['reasons']['returns'])

    def test_result_revision_mismatch_unpaired(self):
        self.scores['right']['source']['result_sha256']='e'*64
        self.assertIn('result_revision_mismatch',self.compare()['rows'][0]['comparability']['reasons']['probability'])

    def test_explicitly_qualitative_same_ticket_can_compare_returns(self):
        for value in self.scores.values():
            value['binding']['forecast_id']=None
            value['source_pool']['model_sha256s']=[]
            value['probability']={'status':'not_available','reason':'adopted_forecast_unavailable','metrics':None}
        result=self.compare()
        self.assertEqual(result['coverage'],{'common':1})
        self.assertFalse(result['rows'][0]['comparability']['probability'])
        self.assertEqual(result['prospective_paired_metrics']['strategy_return']['mean_difference'],0)

    def test_C_purpose_mismatch_is_declared_abstention(self):
        self.scores['right']['shadow_grading']['single']={'grade':'C','action_status':'watch_only','c_reason':'purpose_mismatch'}
        result=self.compare()
        self.assertEqual(result['coverage'],{'lost_coverage':1})
        self.assertEqual(result['rows'][0]['right']['strategy_return'],0)

    def test_output_identity_or_cutoff_mismatch_blocks(self):
        self.scores['right']['binding']['home_id']=99
        with self.assertRaisesRegex(EvidenceError,'identity_mismatch'):
            self.compare()

    def test_actual_output_policy_mismatch_cannot_generate_candidate(self):
        self.scores['right']['shadow_grading']['policy_id']='different'
        result=self.compare()
        self.assertEqual(result['coverage'],{'unpaired':1})
        self.assertEqual(result['rows'][0]['right']['reason'],'shadow_policy_identity_mismatch')

    def test_actual_never_synthesizes_abstention_as_transaction(self):
        self.spec['quote_protocol']='QUOTE_ACTUAL'
        for row in self.scores.values():
            row['quote_protocol']='QUOTE_ACTUAL'; row['outcome']['actual_single_return']=.95
        self.scores['left']['formal_fit']['single']='不建议'
        result=self.compare()
        self.assertIsNone(result['rows'][0]['left']['strategy_return'])

    def test_open_opportunity_must_be_identical_and_in_common_cutoff(self):
        self.spec['quote_protocol']='QUOTE_OPEN'
        for row in self.scores.values():
            row['quote_protocol']='QUOTE_OPEN'; row['outcome']['open_simulated_return']=.95
            row['source']['quote_evidence_sha256']='e'*64
            row['open_quote_times']={'asserted_at':stamp(.4),'observed_at':stamp(.5)}
        result=self.compare()
        self.assertTrue(result['rows'][0]['comparability']['returns'])
        self.scores['right']['open_quote_times']['observed_at']=stamp(1.5)
        result=self.compare(pid=result['protocol_id'])
        self.assertIn('open_quote_outside_common_cutoff',result['rows'][0]['comparability']['reasons']['returns'])
        self.scores['right']['open_quote_times']['observed_at']=stamp(.5)
        self.scores['right']['source']['quote_evidence_sha256']='f'*64
        result=self.compare(pid=result['protocol_id'])
        self.assertIn('different_or_missing_open_quote_opportunity',result['rows'][0]['comparability']['reasons']['returns'])

    def test_actual_different_transactions_not_same_opportunity(self):
        self.spec['quote_protocol']='QUOTE_ACTUAL'
        for row in self.scores.values():
            row['quote_protocol']='QUOTE_ACTUAL';row['outcome']['actual_single_return']=.95
            row['source']['event_manifest_sha256s']=['e'*64]
        result=self.compare()
        self.assertTrue(result['rows'][0]['comparability']['returns'])
        self.scores['right']['source']['event_manifest_sha256s']=['f'*64]
        result=self.compare(pid=result['protocol_id'])
        self.assertIn('different_or_missing_actual_execution',result['rows'][0]['comparability']['reasons']['returns'])

    def test_equivalent_timezone_identity_and_cutoff(self):
        self.scores['right']['binding']['kickoff']='2030-01-01T13:00:00+08:00'
        self.scores['right']['binding']['analysis_cutoff']='2030-01-01T09:00:00+08:00'
        self.scores['right']['binding']['match_id']='1'
        self.assertEqual(self.compare()['coverage'],{'common':1})

    def test_mathematical_infinite_log_loss_diagnostic_preserved(self):
        metric=self.scores['right']['probability']['metrics']['one_x_two']
        metric.update(mathematical_log_loss_status='positive_infinity',mathematical_log_loss=None,
            observed_zero_probability_outcomes=1,floor_affected_outcomes=1,log_loss_probability_floor=1e-15)
        result=self.compare()
        self.assertEqual(result['rows'][0]['right']['probability_diagnostics']['one_x_two']['observed_zero_probability_outcomes'],1)

    def test_earliest_output_retained_not_best_later_version(self):
        later=score(); later['decision_times']['report_generated_at']=stamp(2.5)
        later['source']['decision_manifest_sha256']='e'*64
        later['binding']['version_id']='r2'; later['outcome']['paper_reference_return']=10.0
        self.scores['later']=later
        inputs=[{'request_id':'req1','left':[{'path':'later','manifest_sha256':'c'*64},{'path':'left','manifest_sha256':'a'*64}],
                 'right':[{'path':'right','manifest_sha256':'b'*64}]}]
        result=self.compare(inputs)
        self.assertEqual(result['actual_output_versions']['left'],2)
        self.assertEqual(result['unique_matches'],1)
        self.assertEqual(result['rows'][0]['left']['ticket_return'],.95)

    def test_same_clock_order_ambiguity_not_sorted_as_truth(self):
        self.scores['another']=score()
        self.scores['another']['source']['decision_manifest_sha256']='e'*64
        inputs=[{'request_id':'req1','left':[{'path':'another','manifest_sha256':'c'*64},{'path':'left','manifest_sha256':'a'*64}]}]
        result=self.compare(inputs)
        self.assertEqual(result['rows'][0]['left']['reason'],'earliest_output_order_ambiguous')

    def test_score_result_revisions_are_not_new_decision_versions(self):
        self.scores['revision']=score()
        self.scores['revision']['source']['result_sha256']='e'*64
        inputs=[{'request_id':'req1','left':[{'path':'revision','manifest_sha256':'c'*64},{'path':'left','manifest_sha256':'a'*64}]}]
        result=self.compare(inputs)
        self.assertEqual(result['actual_output_versions']['left'],1)
        self.assertEqual(result['score_artifact_versions']['left'],2)
        self.assertEqual(result['rows'][0]['left']['reason'],'multiple_score_revisions_require_explicit_selection')
        self.assertEqual(len(result['rows'][0]['left']['versions'][0]['scoring_versions']),2)
        # Explicitly selecting one common result revision restores comparability.
        self.scores['right']=deepcopy(self.scores['revision'])
        result=self.compare([{'request_id':'req1','left':[{'path':'revision','manifest_sha256':'c'*64}],
            'right':[{'path':'right','manifest_sha256':'b'*64}]}],pid=result['protocol_id'])
        self.assertEqual(result['coverage'],{'common':1})

    def test_duplicate_artifact_not_additional_version(self):
        ref={'path':'left','manifest_sha256':'a'*64}
        result=self.compare([{'request_id':'req1','left':[ref,ref]}])
        self.assertEqual(result['actual_output_versions']['left'],1)

    def test_real_failure_source_retained_before_later_success(self):
        value={'request_id':'req1','policy_id':'formal-r5','status':'failed','reason':'synthetic engine failure','completed_at':stamp(1.5)}
        path=self.root/'failure.json'; path.write_text(json.dumps(value)); sha=hashlib.sha256(path.read_bytes()).hexdigest()
        result=self.compare([{'request_id':'req1','left':[{'kind':'failure','path':str(path),'sha256':sha}, {'path':'left','manifest_sha256':'a'*64}]}])
        self.assertEqual(result['rows'][0]['left']['status'],'failed')
        self.assertEqual(result['actual_output_versions']['left'],2)
        self.assertIsNone(result['rows'][0]['left']['strategy_return'])

    def test_unknown_request_cannot_expand_predeclared_scope(self):
        with self.assertRaisesRegex(EvidenceError,'unknown_or_duplicate_request'):
            self.compare([{'request_id':'invented'}])

    def test_research_and_prospective_metrics_separate(self):
        self.scores['right']['qualification']='research_only'
        result=self.compare()
        self.assertEqual(result['prospective_pairs'],0)
        self.assertEqual(result['research_paired_metrics']['strategy_return']['unique_matches'],1)
        self.assertEqual(result['prospective_paired_metrics']['strategy_return']['unique_matches'],0)

    def test_cluster_bootstrap_equal_match_weight_fixed_seed(self):
        rows=[{'competition':'x','match_id':1,'left':0,'right':1} for _ in range(20)]
        rows.append({'competition':'x','match_id':2,'left':1,'right':0})
        first=cluster_interval(rows)
        self.assertEqual(first['mean_difference'],0)
        self.assertEqual(first['unique_matches'],2)
        self.assertEqual(first['paired_rows'],21)
        self.assertEqual(first,cluster_interval(rows))
        self.assertIsNone(cluster_interval([])['mean_difference'])

    def test_nonfinite_metrics_not_smuggled_as_zero(self):
        self.scores['right']['probability']['metrics']['one_x_two']['log_loss']=float('inf')
        result=self.compare()
        self.assertIsNone(result['prospective_paired_metrics']['one_x_two_log_loss']['mean_difference'])

    def test_output_report_no_overwrite(self):
        pid=self.register(); path=self.root/'comparison.json'
        with patch('football_model.policy_evaluation._now',return_value=BASE+timedelta(hours=3)):
            evaluate_policy_comparison(self.root,pid,[],output_path=path)
            with self.assertRaisesRegex(EvidenceError,'no_overwrite'):
                evaluate_policy_comparison(self.root,pid,[],output_path=path)

    def test_real_score_bundle_end_to_end_without_mock_reader(self):
        from football_model.decision_record import build_decision_bundle
        from football_model.decision_quotes import adapt_quote_row
        from football_model.decision_scoring import register_scoring_protocol, score_decision
        from football_model.prospective import AXES
        from football_model.shadow_grading import CHAIN_FIELDS
        try:
            from test_decision_record import fixture
        except ModuleNotFoundError:
            from tests.test_decision_record import fixture
        record=fixture(self.root)
        record.update(competition='synthetic',home_id=10,away_id=20,policy_id='formal-r5',analysis_cutoff=stamp(1),kickoff_at=stamp(5))
        record['inputs'][0]['observed_at']=stamp(.25)
        record['quote_rows'][0].update(observed_at=stamp(.25),quote_time=stamp(.1))
        mid=record['match_id']
        model={'forecast_id':'forecast1','match_id':mid,'competition':'synthetic','home_id':10,'away_id':20,'kickoff':stamp(5),
               'prediction':{'home_id':10,'away_id':20,'kickoff':stamp(5),'score_matrix_axes':AXES,
                    'model_version':'synthetic-v1','training_data_sha256':'1'*64,
                    'score_matrix':[[.1,.1,0],[.1,.5,0],[.1,.1,0]],'probabilities':{'home_win':.3,'draw':.6,'away_win':.1}},
               'model_id':'synthetic-model','time_semantics':'observed_wall_clock_v1',
               'run_started_at':stamp(-.1),'data_observed_at':stamp(-.05),
               'forecast_generated_at':stamp(.1),'model_fitted_at':stamp(.05),'training_cutoff':stamp(0)}
        model_raw=json.dumps([model]).encode(); (self.root/'model.json').write_bytes(model_raw)
        record['inputs'].append({'input_id':'model-input','source_id':'model-source','role':'prediction','path':'model.json',
            'sha256':hashlib.sha256(model_raw).hexdigest(),'observed_at':stamp(.25),'published_at':None})
        record['forecast_id']='forecast1';record['forecast_missing_reason']=None
        record['archive']['model_records']=[{'model_record_id':'m1','forecast_id':'forecast1','forecast_evidence_ids':['model-input']}]
        offer=next(o for o in adapt_quote_row(record['quote_rows'][0],match_id=mid,forecast_id='forecast1')['offers'] if o['selection']=='under')
        arg=lambda:{'reason':'Synthetic engineering evidence only','refs':['s1']}
        view={'candidate_id':'c1','contract_id':offer['contract_id'],'quote_offer_id':offer['quote_offer_id'],
            'action_status':'candidate','grade':'B','objective':'Synthetic','reason':'Synthetic','condition_ids':[],
            'decision_chain':{'complete':True,**{k:arg() for k in CHAIN_FIELDS}},'ordinary_branch_robustness':arg(),
            'distinguishing_evidence':arg(),'weakest_assumption':arg(),'failure_path':arg(),'not_a_reason':arg()}
        record['shadow_grading']={'schema_version':'s2-shadow-grading-1.0','mode':'shadow','decision_id':record['decision_id'],
            'policy_id':'shadow-v1','policy_version':'1','evaluation_context':'current','single':view,'parlay_leg':deepcopy(view)}
        self.spec['requests'][0]['match_id']=mid
        pid=self.register()
        scoring_spec={'name':'Synthetic actual adapter chain','quote_protocol':'QUOTE_REF','usage':'single',
            'scope':{'competitions':['synthetic'],'match_ids':'all'},'evaluation_mode':'prospective','unit_stake':1,'cost_per_unit':0,
            'decision_selection':'explicit_adopted_version_no_retrospective_switch','condition_policy':'satisfied_or_no_conditions_only'}
        with patch('football_model.decision_scoring._now',return_value=BASE):
            scoring_protocol=register_scoring_protocol(scoring_spec,self.root/'scoring-protocols')
        with patch('football_model.decision_record._now',return_value=BASE+timedelta(hours=2)):
            built=build_decision_bundle(record,self.root/'decision',source_root=self.root)
        self.assertEqual(built['status'],'structural_checks_passed',built)
        result={'schema_version':1,'competition':'synthetic','match_id':mid,'home_id':10,'away_id':20,'kickoff':stamp(5),
            'home_goals':1,'away_goals':1,'observed_at':stamp(10),'captured_at':stamp(10.1),
            'score_scope':'normalized_API_Football_score_fulltime_90_plus_stoppage'}
        # A result declaration is insufficient for prospective scoring: preserve
        # the independently supplied normalized parent, as the real ledger does.
        normalized_data={'time_semantics':'observed_wall_clock_v1','observed_at':stamp(10),
                         'matches':[deepcopy(result)]}
        source={'kind':'normalized_regulation_data',
                'original_bytes_sha256':hashlib.sha256(json.dumps(normalized_data).encode()).hexdigest(),
                'content':normalized_data}
        source_raw=json.dumps(source).encode()
        source_sha=hashlib.sha256(source_raw).hexdigest()
        (self.root/'result-parent.json').write_bytes(source_raw)
        result['source_id']=source_sha
        raw=json.dumps(result).encode();(self.root/'result.json').write_bytes(raw)
        with patch('football_model.decision_scoring._now',return_value=BASE+timedelta(hours=12)), \
             patch('football_model.decision_record._now',return_value=BASE+timedelta(hours=12)):
            scored=score_decision(self.root/'decision',expected_manifest_sha256=built['manifest_sha256'],
                protocol_root=self.root/'scoring-protocols',protocol_id=scoring_protocol['protocol_id'],
                result={'path':'result.json','sha256':hashlib.sha256(raw).hexdigest(),
                        'source_evidence':[{'path':'result-parent.json','sha256':source_sha}]},result_source_root=self.root,
                output_dir=self.root/'score')
        self.assertEqual(scored['result_source_status'],'native_normalized_parent_verified')
        self.assertEqual(scored['qualification'],'prospective_decision_candidate')
        reference={'path':str(self.root/'score'),'manifest_sha256':scored['manifest_sha256']}
        with patch('football_model.policy_evaluation._now',return_value=BASE+timedelta(hours=12)), \
             patch('football_model.decision_scoring._now',return_value=BASE+timedelta(hours=12)), \
             patch('football_model.decision_record._now',return_value=BASE+timedelta(hours=12)):
            comparison=evaluate_policy_comparison(self.root,pid,[{'request_id':'req1','left':[reference],'right':[reference]}])
        self.assertEqual(comparison['coverage'],{'common':1})
        self.assertEqual(comparison['prospective_pairs'],1)
        self.assertEqual(comparison['prospective_paired_metrics']['one_x_two_brier']['mean_difference'],0,scored)
        self.assertEqual(comparison['prospective_paired_metrics']['five_state_brier']['mean_difference'],0)
        self.assertEqual(comparison['prospective_paired_metrics']['strategy_return']['mean_difference'],0)
        (self.root/'score'/'score.json').write_text('{}')
        with self.assertRaises(EvidenceError):
            evaluate_policy_comparison(self.root,pid,[{'request_id':'req1','left':[reference]}])


if __name__ == '__main__':
    unittest.main()
