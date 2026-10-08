"""Synthetic full-chain trials; these tests are not football evidence."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from football_model.decision_record import build_decision_bundle
from football_model.decision_scoring import register_scoring_protocol, score_decision
from football_model.decision_trials import (RUN_SCHEMA, compare_trials, open_opportunity,
                                          register_trial, submit_run)
from football_model.evidence import EvidenceError
from test_decision_record import fixture


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


class DecisionTrialsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.base = datetime.now(timezone.utc) - timedelta(days=2)
        self.state = self.root / 'trial'
        self.record = fixture(self.root)
        self.record.update(competition='synthetic', home_id=10, away_id=20,
                           analysis_cutoff=self.t(300), kickoff_at=self.t(3600))
        self.record['inputs'][0]['observed_at'] = self.t(0)
        self.record['quote_rows'][0].update(observed_at=self.t(0), quote_time=self.t(-60))
        scoring_spec = {'name':'synthetic trial', 'quote_protocol':'QUOTE_REF', 'usage':'single',
                        'scope':{'competitions':['synthetic'], 'match_ids':'all'}, 'evaluation_mode':'prospective',
                        'unit_stake':1, 'cost_per_unit':0,
                        'decision_selection':'explicit_adopted_version_no_retrospective_switch',
                        'condition_policy':'satisfied_or_no_conditions_only'}
        with patch('football_model.decision_scoring._now', return_value=self.dt(-200)):
            scoring = register_scoring_protocol(scoring_spec, self.root)
        self.scoring_id = scoring['protocol_id']
        left = self.source('left.txt', b'SYNTHETIC baseline policy original')
        right = self.source('right.txt', b'SYNTHETIC alternative policy original')
        self.spec = {'experiment_id':'synthetic', 'comparison':'independent_ticket_selection',
                     'scope_description':'all synthetic requested matches', 'primary_objective':'unit return',
                     'allowed_degradation':'none specified for synthetic test', 'review_schedule':'fixed test end',
                     'stopping_rule':'engineering only', 'opportunity_sampling_rule':'all listed requests',
                     'target':'REGULATION', 'usage':'single', 'quote_protocol':'QUOTE_REF', 'unit_stake':1,
                     'model_control':'same_frozen_model_and_forecast', 'abstention_zero_exposure':True,
                     'version_selection':'first_registered_actual_output_including_failure',
                     'policies':{'left':{'policy_id':'baseline', 'definition':left},
                                 'right':{'policy_id':'candidate', 'definition':right}},
                     'scoring_protocol':{'path':'protocols/'+self.scoring_id+'.json', 'sha256':self.scoring_id},
                     'requests':[{'request_id':'r1','competition':'synthetic','match_id':'synthetic-match',
                                  'home_id':10,'away_id':20,'kickoff':self.t(3600), 'analysis_cutoff':self.t(300),
                                  'target_task':'one main ticket from complete pool'}]}
        with patch('football_model.decision_trials._now', return_value=self.dt(-100)):
            self.protocol = register_trial(self.spec, self.state, source_root=self.root)
        self.pid = self.protocol['protocol_id']
        neutral = deepcopy(self.record)
        neutral['primary_candidate_id'] = None
        neutral['archive']['candidates'] = []
        neutral['no_primary_reason'] = 'Neutral shared input pool, not a policy decision.'
        neutral['decision_chain'] = {}
        with patch('football_model.decision_record._now', return_value=self.dt(310)):
            built = build_decision_bundle(neutral, self.root/'neutral', source_root=self.root)
        self.assertNotEqual(built['status'], 'blocked', built)
        with patch('football_model.decision_trials._now', return_value=self.dt(320)):
            self.opportunity = open_opportunity(self.state, self.pid, 'r1', self.root/'neutral', built['manifest_sha256'])
        self.runs = {}

    def tearDown(self):
        self.temp.cleanup()

    def dt(self, seconds):
        return self.base + timedelta(seconds=seconds)

    def t(self, seconds):
        return self.dt(seconds).isoformat()

    def source(self, name, raw):
        (self.root/name).write_bytes(raw)
        return {'path':name, 'sha256':sha(raw)}

    def run_record(self, side, kind='decision', seconds=400, **changes):
        core = deepcopy(self.record)
        core.update(policy_id=self.spec['policies'][side]['policy_id'], decision_id='actual-'+side)
        if side == 'right':
            core['archive']['candidates'][0]['selection'] = 'over'
            core['archive']['candidates'][0]['mechanism'] = 'SYNTHETIC independently chosen over test'
        run = {'schema_version':RUN_SCHEMA, 'request_id':'r1', 'kind':kind, 'run_id':side+'-'+str(seconds),
               'producer':'synthetic fixture generator', 'policy_id':core['policy_id'],
               'policy_definition_sha256':self.spec['policies'][side]['definition']['sha256'],
               'opportunity_manifest_sha256':self.opportunity['manifest_sha256'],
               'generation_mode':'independent_output', 'future_information_exposure':False,
               'started_at':self.t(seconds-20), 'completed_at':self.t(seconds-10)}
        if kind == 'decision':
            run['output_record'] = core
        else:
            run.update(reason='synthetic explicit reason', abstention_type='no_qualifying_contract')
        run.update(changes)
        return run

    def submit(self, side, kind='decision', seconds=400, run=None, **changes):
        run = run or self.run_record(side, kind, seconds, **changes)
        ref = self.source('run-'+side+'-'+str(seconds)+'.json', json.dumps(run).encode())
        with patch('football_model.decision_trials._now', return_value=self.dt(seconds)), \
             patch('football_model.decision_record._now', return_value=self.dt(seconds)):
            result = submit_run(self.state, self.pid, 'r1', side, ref, source_root=self.root)
        self.runs[(side, seconds)] = result
        return result

    def score(self, side, receipt):
        result = {'schema_version':1, 'competition':'synthetic','match_id':'synthetic-match',
                  'home_id':10,'away_id':20,'kickoff':self.t(3600),'home_goals':1,'away_goals':1,
                  'observed_at':self.t(20000),'captured_at':self.t(20001),
                  'score_scope':'normalized_API_Football_score_fulltime_90_plus_stoppage'}
        source = self.source('result-original.json', json.dumps({'synthetic':True,'result':result}).encode())
        source.update(observed_at=self.t(20000), semantic_review={'status':'verified','reviewer':'synthetic',
                      'source_sha256':source['sha256'],'reviewed_at':self.t(21000)})
        ref = self.source('result.json', json.dumps(result).encode())
        ref.update(source_evidence=[source],semantic_review={'status':'verified','reviewer':'synthetic',
                    'source_sha256':ref['sha256'],'reviewed_at':self.t(21000)})
        dest = self.root/('score-'+side)
        score = score_decision(Path(receipt['path'])/'decision', expected_manifest_sha256=receipt['decision_manifest_sha256'],
                               protocol_root=self.root, protocol_id=self.scoring_id, result=ref,
                               result_source_root=self.root, output_dir=dest)
        return {'request_id':'r1','side':side,'path':str(dest),'manifest_sha256':score['manifest_sha256']}

    def test_independent_different_tickets_real_F2_F3_chain(self):
        left, right = self.submit('left'), self.submit('right', seconds=410)
        self.assertEqual(left['status'], 'available', left)
        self.assertEqual(right['status'], 'available', right)
        self.assertEqual(left['qualification'], 'prospective_candidate', left)
        scores = [self.score('left', left), self.score('right', right)]
        report = compare_trials(self.state, self.pid, scores)
        self.assertEqual(report['coverage'], {'common':1})
        self.assertEqual(report['prospective_process_pairs'], 1)
        metric = report['metrics']['prospective']['strategy_return']
        self.assertEqual(metric['paired_rows'], 1)
        self.assertAlmostEqual(metric['mean_difference'], -1.95)
        self.assertIsNone(metric['interval_95'])
        self.assertFalse(report['quality_improvement_verified'])
        self.assertFalse(report['whole_parlay_evaluated'])

    def test_all_missing_preserved_in_universe(self):
        report = compare_trials(self.state, self.pid, [])
        self.assertEqual(report['request_count'], 1)
        self.assertEqual(report['coverage'], {'unpaired':1})
        self.assertIsNone(report['rows'][0]['sides']['left']['strategy_return'])

    def test_abstention_zero_only_with_verified_other_ticket_settlement(self):
        self.submit('left', 'abstention')
        right = self.submit('right', seconds=410)
        pending = compare_trials(self.state, self.pid, [])
        self.assertEqual(pending['coverage'], {'restored_coverage':1})
        self.assertIsNone(pending['rows'][0]['sides']['left']['strategy_return'])
        report = compare_trials(self.state, self.pid, [self.score('right', right)])
        self.assertEqual(report['rows'][0]['sides']['left']['strategy_return'], 0)
        self.assertEqual(report['metrics']['prospective']['strategy_return']['paired_rows'], 1)

    def test_both_explicit_abstentions_remain_in_opportunity_denominator(self):
        self.submit('left', 'abstention')
        self.submit('right', 'abstention', seconds=410)
        report = compare_trials(self.state, self.pid, [])
        self.assertEqual(report['coverage'], {'both_abstain':1})
        metric = report['metrics']['prospective']['strategy_return']
        self.assertEqual(metric['paired_rows'], 1)
        self.assertEqual(metric['mean_difference'], 0)
        self.assertEqual(report['rows'][0]['sides']['left']['strategy_return'], 0)
        self.assertEqual(report['rows'][0]['sides']['right']['strategy_return'], 0)

    def test_extra_derived_forecast_changes_the_model_pool(self):
        run = self.run_record('right')
        # The chosen forecast remains unavailable; even an extra available model
        # is not an identical information pool and must not sneak into one side.
        ref = self.source('derived-forecast.json', json.dumps({'forecast_id':'same-id',
            'match_id':'synthetic-match','kickoff':self.t(3600),'prediction':{'score_matrix':[[1]]}}).encode())
        run['output_record']['derived_artifacts'] = [{'artifact_id':'d1', **ref,
            'kind':'computed_from_frozen_inputs','artifact_type':'forecast','parent_ids':['i1'],
            'calculated_at':self.t(310),'transformation':'synthetic fixed model calculation'}]
        receipt = self.submit('right', run=run)
        self.assertEqual(receipt['status'], 'failed')
        self.assertIn('exact_common', receipt['reason'])

    def test_same_forecast_id_different_derived_bytes_cannot_pair(self):
        self.state = self.root/'trial-with-derived'
        with patch('football_model.decision_trials._now', return_value=self.dt(-100)):
            self.pid = register_trial(self.spec, self.state, source_root=self.root)['protocol_id']
        forecast = {'forecast_id':'same-id','match_id':'synthetic-match','kickoff':self.t(3600),
                    'prediction':{'score_matrix':[[1,0],[0,0]]}}
        ref = self.source('derived-forecast.json', json.dumps(forecast).encode())
        self.record['derived_artifacts'] = [{'artifact_id':'d1', **ref,
            'kind':'computed_from_frozen_inputs','artifact_type':'forecast','parent_ids':['i1'],
            'calculated_at':self.t(305),'transformation':'synthetic frozen calculation'}]
        self.record['forecast_id'] = 'same-id'
        self.record['archive']['model_records'] = [{'model_record_id':'m1','forecast_id':'same-id',
                                                  'forecast_evidence_ids':['d1']}]
        neutral = deepcopy(self.record)
        neutral.update(primary_candidate_id=None, no_primary_reason='Neutral input evidence only', decision_chain={})
        neutral['archive']['candidates'] = []
        with patch('football_model.decision_record._now', return_value=self.dt(310)):
            built = build_decision_bundle(neutral, self.root/'neutral-derived', source_root=self.root)
        self.assertNotEqual(built['status'], 'blocked', built)
        with patch('football_model.decision_trials._now', return_value=self.dt(320)):
            self.opportunity = open_opportunity(self.state, self.pid, 'r1', self.root/'neutral-derived', built['manifest_sha256'])
        left = self.submit('left')
        self.assertEqual(left['status'], 'available', left)
        forecast['prediction']['score_matrix'] = [[0,0],[0,1]]
        ref = self.source('derived-forecast.json', json.dumps(forecast).encode())
        run = self.run_record('right', seconds=420)
        run['output_record']['derived_artifacts'][0].update(ref)
        right = self.submit('right', seconds=420, run=run)
        self.assertEqual(right['status'], 'failed')
        self.assertIn('exact_common', right['reason'])
        self.assertEqual(compare_trials(self.state, self.pid, [])['prospective_process_pairs'], 0)

    def test_first_failure_not_replaced_by_successful_retry(self):
        self.submit('left', 'failure')
        retry = self.submit('left', seconds=430)
        self.submit('right', seconds=440)
        report = compare_trials(self.state, self.pid, [])
        self.assertEqual(report['coverage'], {'unpaired':1})
        self.assertEqual(report['rows'][0]['sides']['left']['versions'], 2)
        with self.assertRaisesRegex(EvidenceError,'first_failure'):
            compare_trials(self.state, self.pid, [self.score('left', retry)])

    def test_different_pool_is_saved_as_failure(self):
        run = self.run_record('right')
        run['output_record']['quote_rows'][0]['odds']['over'] = 2.1
        receipt = self.submit('right', run=run)
        self.assertEqual(receipt['status'], 'failed')
        self.assertIn('exact_common', receipt['reason'])
        self.assertEqual(compare_trials(self.state, self.pid, [])['rows'][0]['sides']['right']['action'], 'failed')

    def test_changed_identity_is_saved_as_failure(self):
        run = self.run_record('right')
        run['output_record']['home_id'] = 11
        self.assertEqual(self.submit('right', run=run)['reason'], 'request_identity_mismatch_home_id')

    def test_same_run_id_cannot_be_reused_for_other_policy(self):
        self.submit('left')
        with self.assertRaisesRegex(EvidenceError,'run_id_already'):
            self.submit('right', seconds=420, run_id='left-400')

    def test_comparison_rechecks_duplicate_run_ids_after_parallel_submission(self):
        self.submit('left')
        with patch('football_model.decision_trials._receipts', return_value=[]):
            self.submit('right', seconds=420, run_id='left-400')
        with self.assertRaisesRegex(EvidenceError,'duplicate_actual_run_ids'):
            compare_trials(self.state, self.pid, [])

    def test_unknown_is_not_explicit_abstention(self):
        with self.assertRaisesRegex(EvidenceError,'unknown_is_not'):
            self.submit('left', 'abstention', abstention_type='unknown')

    def test_future_run_time_rejected(self):
        with self.assertRaisesRegex(EvidenceError,'not_be_from_future'):
            self.submit('left', completed_at=self.t(900))

    def test_run_before_shared_pool_rejected(self):
        with self.assertRaisesRegex(EvidenceError,'must_follow_opportunity'):
            self.submit('left', started_at=self.t(310))

    def test_after_kickoff_never_prospective(self):
        receipt = self.submit('left', seconds=4000)
        self.assertEqual(receipt['qualification'], 'research_only')
        self.assertIn('submission_committed_after_kickoff', receipt['qualification_reasons'])

    def test_seen_future_stays_research(self):
        run = self.run_record('left')
        run['future_information_exposure'] = True
        run['output_record']['future_information_exposure'] = True
        self.assertEqual(self.submit('left', run=run)['qualification'], 'research_only')

    def test_common_pool_cannot_be_a_prior_policy_choice(self):
        with patch('football_model.decision_record._now', return_value=self.dt(310)):
            built = build_decision_bundle(self.record, self.root/'chosen', source_root=self.root)
        with self.assertRaisesRegex(EvidenceError,'neutral_input_pool'):
            open_opportunity(self.state, self.pid, 'r1', self.root/'chosen', built['manifest_sha256'])

    def test_same_decision_identity_is_not_independent(self):
        self.submit('left')
        run = self.run_record('right')
        run['output_record']['decision_id'] = 'actual-left'
        self.submit('right', seconds=410, run=run)
        report = compare_trials(self.state, self.pid, [])
        self.assertEqual(report['coverage'], {'unpaired':1})
        self.assertIn('same_decision_identity_not_independent_outputs', report['rows'][0]['reasons'])

    def test_tamper_actual_run_stops_comparison(self):
        receipt = self.submit('left')
        (Path(receipt['path'])/'run.json').write_text('{}')
        with self.assertRaisesRegex(EvidenceError,'file_hash'):
            compare_trials(self.state, self.pid, [])

    def test_damaged_submission_sequence_is_not_silently_skipped(self):
        receipt = self.submit('left')
        (Path(receipt['path']).parent/'000003').mkdir()
        with self.assertRaisesRegex(EvidenceError,'sequence_gap'):
            compare_trials(self.state, self.pid, [])

    def test_incomplete_first_slot_retained_as_failure(self):
        receipt = self.submit('left')
        (Path(receipt['path'])/'manifest.json').unlink()
        report = compare_trials(self.state, self.pid, [])
        self.assertEqual(report['rows'][0]['sides']['left']['reason'], 'incomplete_submission_slot')

    def test_incomplete_first_slot_allows_retry_but_does_not_replace_it(self):
        receipt = self.submit('left')
        (Path(receipt['path'])/'manifest.json').unlink()
        retry = self.submit('left', seconds=450)
        self.assertEqual(retry['sequence'], 2)
        report = compare_trials(self.state, self.pid, [])
        self.assertEqual(report['rows'][0]['sides']['left']['reason'], 'incomplete_submission_slot')
        self.assertEqual(report['rows'][0]['sides']['left']['versions'], 2)

    def test_extra_evidence_cannot_be_hidden_as_policy_code(self):
        run = self.run_record('left')
        ref = self.source('hidden.txt', b'SYNTHETIC undeclared extra information')
        run['output_record']['inputs'].append({'input_id':'secret','source_id':'s2', 'role':'code',
            **ref, 'observed_at':self.t(0), 'published_at':None})
        receipt = self.submit('left', run=run)
        self.assertEqual(receipt['status'], 'failed')
        self.assertIn('undeclared_extra_rule_or_code_input', receipt['reason'])

    def test_duplicate_opportunity_not_extra_sample(self):
        spec = deepcopy(self.spec)
        spec['requests'].append({**spec['requests'][0], 'request_id':'alias'})
        with self.assertRaisesRegex(EvidenceError,'duplicate_opportunity'):
            register_trial(spec, self.state, source_root=self.root)

    def test_unknown_request_id_rejected(self):
        with self.assertRaisesRegex(EvidenceError,'outside_fixed_universe'):
            compare_trials(self.state, self.pid, [{'request_id':'missing'}])

    def test_only_one_explicit_score_revision(self):
        receipt = self.submit('left')
        score = self.score('left', receipt)
        with self.assertRaisesRegex(EvidenceError,'single_score_revision'):
            compare_trials(self.state, self.pid, [score, score])

    def test_report_no_overwrite(self):
        target = self.root/'report.json'
        compare_trials(self.state, self.pid, [], output_path=target)
        with self.assertRaises(FileExistsError):
            compare_trials(self.state, self.pid, [], output_path=target)

    def test_rules_original_hash_must_match(self):
        (self.root/'right.txt').write_text('changed')
        with self.assertRaisesRegex(EvidenceError,'external_source_hash_mismatch'):
            register_trial(self.spec, self.state, source_root=self.root)


if __name__ == '__main__':
    unittest.main()
