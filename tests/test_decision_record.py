"""Synthetic engineering fixtures; no historical grades or football skill claim."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from football_model.decision_record import (build_decision_bundle, verify_decision_bundle,
                                            append_decision_event)
from football_model.evidence import EvidenceError


def stamp(delta=0):
    return (datetime.now(timezone.utc)+timedelta(seconds=delta)).isoformat()


def digest(blob):
    return hashlib.sha256(blob).hexdigest()


def fixture(root):
    blob=b'SYNTHETIC evidence only; this is not a football source.'
    (root/'source.txt').write_bytes(blob)
    observed=stamp(-300)
    return {"schema_version":"s2-decision-1.0","match_id":"synthetic-match", "decision_id":"synthetic-d1",
            "version_id":"synthetic-r1", "policy_id":"framework-v1.7-r5", "analysis_cutoff":stamp(-10),"kickoff_at":stamp(7200),
            "future_information_exposure":False,"inputs":[{"input_id":"i1","source_id":"s1","path":"source.txt","sha256":digest(blob),
                "observed_at":observed,"published_at":None,"source_claimed_published_at":None}],"derived_artifacts":[],
            "archive":{"sources":[{"source_id":"s1"}],"facts":[],"contracts":[],"candidates":[
                {"candidate_id":"c1","quote_row_id":"row1","selection":"under","formal_fit":{"single":"适合","parlay_leg":"适合"},
                 "support_refs":["s1"],"mechanism":"SYNTHETIC mechanism <script>alert(1)</script>","payment_requirement":"SYNTHETIC under total",
                 "counterevidence":[{"source_refs":["s1"],"handling":"retained","reason":"SYNTHETIC weighted reason"}],
                 "condition_ids":[],"model_record_ids":[],"value":{"status":"unquantified","ev":None,"breakeven_odds":None,
                     "calculation_record_id":None,"disclosed_model_record_ids":[]}}],"conditions":[],"model_records":[],"calculation_records":[]},
            "quote_rows":[{"row_id":"row1","source_id":"s1","sequence":"synthetic-page","row_order":0,"market":"totals",
                "status":"open","line":2.5,"settlement_basis":"REGULATION","quote_time":stamp(-400),"time_precision":"second",
                "observed_at":observed,"odds_format":"decimal","odds":{"over":1.9,"under":1.95}}],
            "primary_candidate_id":"c1","forecast_id":None,"forecast_missing_reason":"Synthetic qualitative fixture, no model invoked",
            "shadow_grading":None,"decision_chain":{"strongest_alternative_candidate_id":None,"no_alternative_reason":"Synthetic single-candidate schema test",
                "alternative_reason":"No claim about actual football alternative coverage"}}


class DecisionRecordTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.root=Path(self.tmp.name)
        self.record=fixture(self.root)
        self.out=self.root/'bundle'

    def tearDown(self):
        self.tmp.cleanup()

    def build(self):
        return build_decision_bundle(self.record,self.out,source_root=self.root)

    def read(self,name):
        return json.loads((self.out/name).read_text())

    def codes(self,result):
        return {i['code'] for i in result['issues']}

    def test_actual_frozen_bytes_views_audit_and_no_mutation(self):
        original=deepcopy(self.record)
        result=self.build()
        self.assertEqual(result['status'],'structural_checks_passed',result)
        self.assertEqual(self.record,original)
        self.assertEqual((self.out/'originals/source.txt').read_bytes(),(self.root/'source.txt').read_bytes())
        self.assertEqual(verify_decision_bundle(self.out,result['manifest_sha256'])['status'],'verified')
        archives=self.read('audit_archives.json')
        for view in archives.values():
            self.assertEqual(view['versions'][0]['body_decision'],view['versions'][0]['summary_decision'])
        card=self.read('main_card.json')
        self.assertEqual(card['primary_candidate']['quote_offer_id'],card['quote']['quote_id'])
        self.assertEqual(self.read('scoring.json')['quote_protocol'],'UNASSIGNED')
        self.assertIsNone(self.read('times.json')['user_received_at'])

    def test_complete_body_and_html_escape(self):
        self.build()
        body=(self.out/'report.html').read_text()
        self.assertNotIn('<script>',body)
        self.assertIn('&lt;script&gt;',body)
        for label in ('决定正文与核验视图','反证处理','最强备选','条件','模型','来源'):
            self.assertIn(label,body)

    def test_distinct_single_and_leg_not_copied(self):
        self.record['archive']['candidates'][0]['formal_fit']['parlay_leg']='不建议'
        result=self.build()
        self.assertEqual(result['status'],'structural_checks_passed',result)
        views=self.read('audit_archives.json')
        self.assertEqual(views['single']['candidates'][0]['fit'],'适合')
        self.assertEqual(views['parlay_leg']['candidates'][0]['fit'],'不建议')

    def test_input_and_output_cannot_be_overwritten(self):
        self.build()
        with self.assertRaisesRegex(EvidenceError,'no_overwrite'):
            self.build()

    def test_missing_quote_retains_qualitative_fit_incomplete(self):
        candidate=self.record['archive']['candidates'][0]
        candidate.pop('quote_row_id')
        candidate['contract_id']='no-price-contract'
        self.record['archive']['contracts']=[{'contract_id':'no-price-contract','market':'ou','selection':'under','line':2.5,'settlement_basis':'REGULATION'}]
        self.record['quote_rows']=[]
        result=self.build()
        self.assertEqual(result['status'],'incomplete',result)
        self.assertEqual(self.read('main_card.json')['primary_candidate']['formal_fit']['single'],'适合')
        self.assertIsNone(self.read('scoring.json')['realized_return'])

    def test_late_external_inputs_only_research(self):
        self.record['inputs'][0]['observed_at']=stamp(-1)
        result=self.build()
        self.assertEqual(result['qualification'],'research_only')
        self.assertIn('observed_after_cutoff',self.read('eligibility.json')['reasons'])

    def test_unknown_exposure_never_prospective(self):
        self.record['future_information_exposure']=None
        self.assertEqual(self.build()['qualification'],'research_only')

    def test_late_derived_calculation_allowed_with_real_parents(self):
        blob=b'SYNTHETIC calculated content'
        (self.root/'derived.json').write_bytes(blob)
        self.record['derived_artifacts']=[{'artifact_id':'d1','path':'derived.json','sha256':digest(blob),
            'kind':'computed_from_frozen_inputs','transformation':'Synthetic deterministic test transform','parent_ids':['i1'],'calculated_at':stamp(-2)}]
        result=self.build()
        self.assertEqual(result['status'],'structural_checks_passed',result)
        self.assertEqual(result['qualification'],'prospective_input_candidate')

    def test_derived_cycles_and_unknown_parents_block(self):
        blob=b'synthetic'
        (self.root/'d').write_bytes(blob)
        self.record['derived_artifacts']=[{'artifact_id':'d1','path':'d','sha256':digest(blob),'kind':'computed_from_frozen_inputs',
            'transformation':'synthetic','parent_ids':['d1','missing'],'calculated_at':stamp(-1)}]
        result=self.build()
        self.assertEqual(result['status'],'blocked')
        self.assertTrue({'derived_dependency_cycle','unknown_derived_parent'} <= self.codes(result))

    def test_late_external_masquerading_as_derived_block(self):
        blob=b'synthetic'
        (self.root/'d').write_bytes(blob)
        self.record['derived_artifacts']=[{'artifact_id':'d1','path':'d','sha256':digest(blob),'kind':'computed_from_frozen_inputs',
            'transformation':'download','parent_ids':['i1'],'calculated_at':stamp(-1),'observed_at':stamp(-1)}]
        result=self.build()
        self.assertIn('external_input_cannot_be_renamed_derived',self.codes(result))

    def test_hash_mismatch_is_saved_as_blocked(self):
        self.record['inputs'][0]['sha256']='0'*64
        result=self.build()
        self.assertEqual(result['status'],'blocked')
        self.assertIn('input_sha256_mismatch',self.codes(result))
        self.assertTrue((self.out/'report.html').exists())

    def test_source_path_escape_and_symlink_block_without_read(self):
        for value in ('../not-allowed','/absolute/not-allowed'):
            with self.subTest(value=value):
                self.out=self.root/('output-'+str(len(value)))
                self.record['inputs'][0]['path']=value
                result=self.build()
                self.assertIn('unsafe_path',self.codes(result))
        self.out=self.root/'symlink-bundle'
        (self.root/'link.txt').symlink_to(self.root/'source.txt')
        self.record['inputs'][0]['path']='link.txt'
        self.assertIn('symlink_forbidden',self.codes(self.build()))

    def test_caller_cannot_prefill_views_or_program_times(self):
        self.record['archive']['versions']=[]
        self.record['freeze_started_at']='2000-01-01T00:00:00Z'
        result=self.build()
        self.assertEqual(result['status'],'blocked')
        self.assertIn('caller_cannot_supply_derived_field',self.codes(result))

    def test_conflicting_source_claim_kept_for_review(self):
        self.record['inputs'][0]['published_at']=stamp(-500)
        self.record['inputs'][0]['source_claimed_published_at']=stamp(-200)
        result=self.build()
        self.assertEqual(result['status'],'incomplete',result)
        self.assertIn('claimed_publication_needs_review',self.codes(result))
        self.assertEqual(self.read('input_record.json')['inputs'][0],self.record['inputs'][0])

    def test_conflicting_quote_id_never_overrides_input_silently(self):
        self.record['archive']['candidates'][0]['quote_offer_id']='fake-offer'
        result=self.build()
        self.assertEqual(result['status'],'blocked')
        self.assertIn('candidate_quote_binding_conflict',self.codes(result))

    def test_forged_forecast_id_not_verified(self):
        self.record['forecast_id']='not-read-from-model'
        result=self.build()
        self.assertEqual(result['status'],'incomplete',result)
        self.assertIn('adopted_forecast_evidence_missing',self.codes(result))

    def test_tampered_file_and_extra_file_fail_verification(self):
        self.build()
        (self.out/'main_card.json').write_text('{}')
        with self.assertRaisesRegex(EvidenceError,'file_hash_or_length_mismatch'):
            verify_decision_bundle(self.out)

    def test_external_anchor_detects_manifest_rewrite(self):
        result=self.build()
        content=json.loads((self.out/'manifest.json').read_text())
        content['another']='rewrite'
        (self.out/'manifest.json').write_text(json.dumps(content))
        with self.assertRaisesRegex(EvidenceError,'manifest_anchor_mismatch'):
            verify_decision_bundle(self.out,result['manifest_sha256'])

    def event(self,event_type='publication'):
        blob=b'SYNTHETIC event evidence'
        (self.root/'receipt.txt').write_bytes(blob)
        return {'event_id':'evt-1','event_type':event_type,'match_id':self.record['match_id'],'decision_id':self.record['decision_id'],
            'version_id':self.record['version_id'],'occurred_at':stamp(),'evidence':[{'path':'receipt.txt','sha256':digest(blob),'observed_at':stamp()}]}

    def test_publication_event_separate_append_and_no_overwrite(self):
        built=self.build()
        event=self.event()
        result=append_decision_event(self.out,event,self.root/'events',source_root=self.root)
        self.assertEqual(result['status'],'event_appended')
        self.assertIsNone(self.read('times.json')['publicly_published_at'])
        self.assertEqual(verify_decision_bundle(self.out,built['manifest_sha256'])['status'],'verified')
        with self.assertRaisesRegex(EvidenceError,'no_overwrite'):
            append_decision_event(self.out,event,self.root/'events',source_root=self.root)

    def test_future_event_rejected(self):
        self.build()
        event=self.event()
        event['occurred_at']=stamp(500)
        with self.assertRaisesRegex(EvidenceError,'outside_real_bounds'):
            append_decision_event(self.out,event,self.root/'events',source_root=self.root)

    def test_condition_satisfied_requires_new_decision_not_auto_grade(self):
        self.record['archive']['conditions']=[{'condition_id':'cond1','state':'unknown','critical':False,'observable':'Synthetic test',
            'actions':{'satisfied':'reassess','not_satisfied':'reassess','unknown':'retain_unknown'}}]
        self.build()
        event=self.event('condition_changed')
        event.update(condition_id='cond1',state='satisfied')
        result=append_decision_event(self.out,event,self.root/'events',source_root=self.root)
        self.assertIn('reevaluation_required',result['effect'])
        self.assertEqual(self.read('decision.json')['archive']['conditions'][0]['state'],'unknown')

    def test_execution_cannot_be_inferred_from_reference(self):
        self.build()
        event=self.event('execution')
        event.update(candidate_id='c1',quote_offer_id=self.read('main_card.json')['primary_candidate']['quote_offer_id'],status='accepted')
        with self.assertRaisesRegex(EvidenceError,'accepted_execution_receipt_required'):
            append_decision_event(self.out,event,self.root/'events',source_root=self.root)

    def test_invalid_hash_never_qualifies_prospective(self):
        self.record['inputs'][0]['sha256']='0'*64
        self.assertEqual(self.build()['qualification'],'research_only')

    def test_forged_validation_and_manifest_fail_replay(self):
        from football_model.decision_record import _bytes, _html, _views
        self.record['inputs'][0]['sha256']='0'*64
        self.build()
        core=self.read('decision.json')
        validation=self.read('validation.json')
        validation.update(status='structural_checks_passed',issues=[],publishable=None)
        eligibility=self.read('eligibility.json')
        views=_views(core,validation,eligibility)
        for name,value in views.items():
            (self.out/(name+'.json')).write_bytes(_bytes(value))
        (self.out/'validation.json').write_bytes(_bytes(validation))
        (self.out/'report.html').write_bytes(_html(core,views,validation,eligibility,self.read('times.json')))
        manifest=self.read('manifest.json')
        for row in manifest['files']:
            blob=(self.out/row['path']).read_bytes()
            row.update(bytes=len(blob),sha256=digest(blob))
        (self.out/'manifest.json').write_bytes(_bytes(manifest))
        with self.assertRaisesRegex(EvidenceError,'validation_replay_mismatch'):
            verify_decision_bundle(self.out)

    def test_quote_timestamp_cannot_be_earlier_than_frozen_source(self):
        self.record['quote_rows'][0]['observed_at']=stamp(-350)
        result=self.build()
        self.assertEqual(result['status'],'blocked')
        self.assertEqual(result['qualification'],'research_only')
        self.assertIn('quote_observation_conflicts_with_frozen_source',self.codes(result))

    def test_report_completion_occurs_after_html_written(self):
        self.build()
        from football_model.evidence import _time
        times=self.read('times.json')
        self.assertGreaterEqual(_time(times['report_generated_at']),_time(times['freeze_committed_at']))
        self.assertLessEqual((self.out/'report.html').stat().st_mtime,_time(times['report_generated_at']).timestamp()+.001)

    def test_unknown_user_reported_execution_time_not_fabricated(self):
        self.build()
        event=self.event('execution')
        event.update(candidate_id='c1',quote_offer_id=self.read('main_card.json')['primary_candidate']['quote_offer_id'],
            status='accepted',execution_basis='user_confirmation',occurred_at=None,accepted_decimal_odds=None)
        event['evidence'][0]['evidence_type']='user_confirmation'
        result=append_decision_event(self.out,event,self.root/'events',source_root=self.root)
        self.assertEqual(result['status'],'event_appended')
        saved=json.loads((self.root/'events/evt-1/event.json').read_text())
        self.assertIsNone(saved['occurred_at'])
        self.assertEqual(saved['timing_status'],'unknown')
        self.assertIsNone(self.read('membership.json')['executed'])

    def test_unknown_value_shape_saved_as_blocked_without_exception(self):
        self.record['archive']['candidates'][0]['value']=None
        result=self.build()
        self.assertEqual(result['status'],'incomplete')
        self.assertIn('missing_value_state',self.codes(result))

    def add_prediction(self, value):
        from football_model.prospective import _json_bytes
        blob=_json_bytes(value)
        (self.root/'prediction.json').write_bytes(blob)
        self.record['inputs'].append({'input_id':'ip','source_id':'sp','path':'prediction.json','sha256':digest(blob),
            'role':'prediction','observed_at':stamp(-300),'published_at':None})
        forecast_id=value.get('forecast_id',digest(blob))
        self.record['forecast_id']=forecast_id
        self.record['archive']['model_records']=[{'model_record_id':'m1','forecast_id':forecast_id,'forecast_evidence_ids':['ip'],
            'candidate_id':'c1','version_id':'synthetic-r1'}]
        return forecast_id

    def test_cross_match_forecast_is_blocked(self):
        self.add_prediction({'forecast_id':'foreign-id','match_id':'another-match'})
        result=self.build()
        self.assertEqual(result['status'],'blocked')
        self.assertEqual(result['qualification'],'research_only')
        self.assertIn('adopted_forecast_match_mismatch',self.codes(result))

    def test_native_ledger_id_uses_actual_bytes_without_self_id(self):
        native={'schema_version':1,'match_id':'synthetic-match','home_id':1,'away_id':2,
            'kickoff':self.record['kickoff_at'],'forecast':{'match_id':'synthetic-match','prediction':{'synthetic':True}}}
        forecast_id=self.add_prediction(native)
        result=self.build()
        self.assertEqual(result['status'],'structural_checks_passed',result)
        self.assertEqual(self.read('main_card.json')['forecast_id'],forecast_id)
        self.assertEqual(verify_decision_bundle(self.out)['status'],'verified')

    def test_unfrozen_source_blocks_prospective_qualification(self):
        self.record['archive']['sources'].append({'source_id':'not-frozen'})
        self.record['archive']['candidates'][0]['support_refs']=['not-frozen']
        result=self.build()
        self.assertEqual(result['qualification'],'research_only')
        self.assertIn('source_not_frozen',self.codes(result))

    def test_no_primary_preserves_abstention(self):
        self.record['primary_candidate_id']=None
        self.record['no_primary_reason']='Synthetic identity unresolved, no ticket chosen'
        result=self.build()
        self.assertEqual(result['status'],'structural_checks_passed',result)
        self.assertIsNone(self.read('main_card.json')['primary_candidate'])
        self.assertEqual(self.read('main_card.json')['no_primary_reason'],self.record['no_primary_reason'])

    def test_usage_specific_conditions_preserve_independent_formal_fits(self):
        candidate=self.record['archive']['candidates'][0]
        candidate['formal_fit']['parlay_leg']='条件适合'
        candidate['formal_condition_ids']={'single':[],'parlay_leg':['leg-condition']}
        self.record['archive']['conditions']=[{'condition_id':'leg-condition','state':'unknown','critical':True,'observable':True,
            'actions':{'satisfied':'reassess at new quote','not_satisfied':'do not use as parlay leg','unknown':'pending'}}]
        result=self.build()
        self.assertEqual(result['status'],'structural_checks_passed',result)
        archives=self.read('audit_archives.json')
        self.assertEqual(archives['single']['candidates'][0]['condition_ids'],[])
        self.assertEqual(archives['parlay_leg']['candidates'][0]['condition_ids'],['leg-condition'])

    def test_usage_conditions_cannot_erase_common_critical_condition(self):
        candidate=self.record['archive']['candidates'][0]
        candidate['condition_ids']=['common-critical']
        candidate['formal_condition_ids']={'single':[],'parlay_leg':[]}
        self.record['archive']['conditions']=[{'condition_id':'common-critical','state':'unknown','critical':True,'observable':True,
            'actions':{'satisfied':'reassess','not_satisfied':'reassess','unknown':'wait'}}]
        result=self.build()
        self.assertEqual(result['status'],'blocked')
        self.assertIn('unresolved_critical_condition',self.codes(result))


if __name__=='__main__':
    unittest.main()
