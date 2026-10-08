"""Synthetic F4 live-shadow receipts and native F3 result-sync integration."""
import copy
from datetime import timedelta
import hashlib
import json
from pathlib import Path
import shutil
import unittest
from unittest.mock import patch

import test_prospective
from test_f3_result_sync import FixtureClient
import test_shadow_models as shadow_fixtures
NOW, iso = shadow_fixtures.NOW, shadow_fixtures.iso
from football_model import prospective, result_sync, shadow_models
from football_model.data import RequestBudgetError
from football_model.model import _validated_matches


class F4ResultSyncTests(unittest.TestCase):
    def setUp(self):
        self.case = shadow_fixtures.ShadowModelTests()
        self.case.setUp()
        self.addCleanup(self.case.tearDown)
        self.runtime = self.case.runtime
        self.shadow = self.case.root
        code, _ = self.case.run_capture()
        self.assertEqual(code, 0)
        self.now = NOW + timedelta(days=1, hours=5, minutes=1)
        self.envelope = {'endpoint':'/fixtures', 'params':{'id':1001}, 'errors':[], 'results':1,
            'fetched_at':iso(self.now - timedelta(minutes=1)), 'from_cache':False,
            'paging':{'current':1,'total':1},
            'response':[{'fixture':{'id':1001,'date':self.case.rows[0]['kickoff'],'status':{'short':'FT'}},
                'league':{'id':39}, 'teams':{'home':{'id':1,'name':'Synthetic 1'},'away':{'id':2,'name':'Synthetic 2'}},
                'score':{'fulltime':{'home':2,'away':1}}}]}

    def sync(self, envelope=None, **kwargs):
        self.client = FixtureClient(self.envelope if envelope is None else envelope)
        return result_sync.run(self.runtime, self.case.config, client=self.client, clock=lambda:self.now, **kwargs)

    def production(self):
        """Capture a legitimate synthetic production parent matching shadow ID."""
        prod = test_prospective.ProspectiveTests()
        prod.setUp()
        self.addCleanup(prod.tearDown)
        prod.training[0]['kickoff'] = iso(NOW - timedelta(days=5))
        serial = [{k:v for k,v in r.items() if k != '_dt'} for r in _validated_matches(prod.training)]
        digest = hashlib.sha256(json.dumps(serial, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        prod.row.update(match_id=1001, kickoff=self.case.rows[0]['kickoff'],
            run_started_at=iso(NOW-timedelta(hours=1)), data_observed_at=iso(NOW-timedelta(minutes=59)),
            training_cutoff=iso(NOW-timedelta(hours=1)), model_fitted_at=iso(NOW-timedelta(minutes=58)),
            forecast_generated_at=iso(NOW-timedelta(minutes=57)))
        prod.row['prediction'].update(kickoff=prod.row['kickoff'], training_cutoff=prod.row['training_cutoff'],
                                     training_data_sha256=digest)
        prod.model['model']['training_data_sha256'] = digest
        prod.report.update(run_started_at=prod.row['run_started_at'], generated_at=iso(NOW-timedelta(minutes=56)))
        prod.report['competitions'][0]['provider_league_id'] = 39
        prod.data['observed_at'] = prod.row['data_observed_at']
        prod.clock = lambda:NOW
        prod.save_inputs()
        code, report = prod.invoke()
        self.assertEqual(code, 0)
        self.assertEqual(report['forecast_versions'], 1)
        shutil.copytree(prod.root/'prospective', self.runtime/'prospective', dirs_exist_ok=True)

    def test_shadow_only_mature_fixture_is_collected_and_scored_separately(self):
        before = {str(p.relative_to(self.runtime)):p.read_bytes() for folder in ('shadow_models','data','predictions')
                  for p in (self.runtime/folder).rglob('*.json')}
        self.assertEqual(prospective._objects(self.runtime/'prospective', 'forecasts'), [])
        code, report = self.sync()
        self.assertEqual(code, 0, report)
        self.assertEqual(report['target_matches'], 1)
        self.assertEqual(report['forecast_versions'], 0)
        self.assertEqual(report['verified_shadow_forecast_versions'], 3)
        self.assertEqual(report['counts'], {'result_appended':1})
        self.assertEqual(report['records'][0]['target_ledgers'], ['shadow_models'])
        self.assertEqual(self.client.requests_made, 1)
        self.assertFalse(report['shadow_forecasts_added_to_production'])
        self.assertEqual(prospective._objects(self.runtime/'prospective', 'forecasts'), [])
        for name, data in before.items():
            self.assertEqual((self.runtime/name).read_bytes(), data)
        _, scored = shadow_models.score(self.runtime, clock=lambda:self.now)
        self.assertEqual(scored['status_counts'], {'scored':2})
        self.assertEqual(prospective._objects(self.runtime/'prospective', 'result_sync_scores')[-1]['forecast_versions'], 0)

    def test_production_and_three_shadow_arms_share_one_fixture_request(self):
        self.production()
        code, report = self.sync()
        self.assertEqual(code, 0, report)
        self.assertEqual(report['target_matches'], 1)
        self.assertEqual(report['target_forecast_versions'], 4)
        self.assertEqual(report['records'][0]['target_ledgers'], ['prospective','shadow_models'])
        self.assertEqual(len(self.client.calls), 1)
        self.assertEqual(len(prospective._objects(self.runtime/'prospective', 'forecasts')), 1)

    def test_bad_receipt_hash_rejected_while_other_arms_remain_usable(self):
        path = next((self.shadow/'forecasts').glob('*.json'))
        value = json.loads(path.read_text())
        value['match_id'] = 999999
        path.write_text(json.dumps(value))
        code, report = self.sync()
        self.assertEqual(code, 1)
        self.assertEqual(report['counts'], {'shadow_target_rejected':1,'result_appended':1})
        self.assertEqual(report['verified_shadow_forecast_versions'], 2)
        self.assertEqual(self.client.calls[0][0], 1001)
        self.assertIn('shadow_forecast_object_hash_mismatch', report['errors'][0])

    def test_missing_durable_payload_rejects_only_its_receipt(self):
        saved = prospective._objects(self.shadow, 'forecasts')[0]
        (self.shadow/'forecast_payloads'/(saved['payload_id']+'.json')).unlink()
        code, report = self.sync()
        self.assertEqual(code, 1)
        self.assertEqual(report['verified_shadow_forecast_versions'], 2)
        self.assertEqual(report['counts']['shadow_target_rejected'], 1)
        self.assertEqual(report['counts']['result_appended'], 1)

    def test_corrupt_shared_parent_rejects_all_shadows_without_API(self):
        source_id = prospective._objects(self.shadow,'forecasts')[0]['parents']['data_source']
        path = self.shadow/'sources'/(source_id+'.json')
        value = json.loads(path.read_text())
        value['content']['observed_at'] = iso(NOW + timedelta(days=2))
        path.write_text(json.dumps(value))
        code, report = self.sync()
        self.assertEqual(code, 1)
        self.assertEqual(report['verified_shadow_forecast_versions'], 0)
        self.assertEqual(report['counts'], {'shadow_target_rejected':3})
        self.assertEqual(self.client.requests_made, 0)
        self.assertEqual(prospective._objects(self.runtime/'prospective', 'results'), [])

    def test_bad_shadow_does_not_block_valid_production_result(self):
        self.production()
        for path in (self.shadow/'forecasts').glob('*.json'):
            path.write_text('{}')
        code, report = self.sync()
        self.assertEqual(code, 1)
        self.assertEqual(report['counts'], {'shadow_target_rejected':3,'result_appended':1})
        self.assertEqual(self.client.requests_made, 1)

    def test_not_mature_shadow_never_requests_API(self):
        self.now = NOW + timedelta(days=1, hours=3, minutes=59)
        code, report = self.sync()
        self.assertEqual(code, 0)
        self.assertEqual(report['counts'], {'not_due':1})
        self.assertEqual(self.client.requests_made, 0)

    def test_provider_id_conflict_fails_without_request(self):
        self.case.config['competitions'][0]['provider_id'] = 999
        code, report = self.sync()
        self.assertEqual(code, 1)
        self.assertEqual(report['counts'], {'failed':1})
        self.assertIn('provider_league_identity_missing_or_conflicting', report['errors'][0])
        self.assertEqual(self.client.requests_made, 0)

    def test_budget_failure_and_old_result_revisions_preserved(self):
        code, report = self.sync(RequestBudgetError('synthetic quota'))
        self.assertEqual(code, 1)
        self.assertEqual(report['counts'], {'budget_deferred':1})
        self.sync()
        originals = {p.name:p.read_bytes() for p in (self.runtime/'prospective/results').glob('*.json')}
        self.now += timedelta(hours=1)
        self.envelope['fetched_at'] = iso(self.now - timedelta(minutes=1))
        self.envelope['response'][0]['score']['fulltime'] = {'home':0,'away':1}
        self.sync()
        self.assertEqual(len(prospective._objects(self.runtime/'prospective','results')), 2)
        for name, raw in originals.items():
            self.assertEqual((self.runtime/'prospective/results'/name).read_bytes(), raw)

    def pipeline_result(self, observed=None, captured=None):
        production = self.runtime/'prospective'
        observed = observed or self.now-timedelta(hours=1)
        captured = captured or observed
        row = {**self.case.rows[0], 'home_goals':2,'away_goals':1}
        source = prospective._append(production, 'sources', {'kind':'normalized_regulation_data',
            'content':{'matches':[row]}, 'original_bytes_sha256':'a'*64})
        return prospective.append_result(production, 'epl', row, observed=observed, captured=captured,
            source_id=source, forecasts=prospective._objects(self.shadow,'forecasts'))

    def test_same_score_pipeline_upgrades_to_independent_then_shadow_scores(self):
        old_id = self.pipeline_result()
        original = (self.runtime/'prospective/results'/(old_id+'.json')).read_bytes()
        code, report = self.sync()
        self.assertEqual(code, 0, report)
        self.assertEqual(report['counts'], {'result_appended':1})
        results = prospective._objects(self.runtime/'prospective','results')
        self.assertEqual(len(results), 2)
        new = next(r for r in results if r['collection_method'] == 'independent_fixture_sync')
        self.assertEqual(new['supersedes'], old_id)
        self.assertEqual((self.runtime/'prospective/results'/(old_id+'.json')).read_bytes(), original)
        _, scored = shadow_models.score(self.runtime, clock=lambda:self.now)
        self.assertEqual(scored['status_counts'], {'scored':2})
        # Later pipeline runs cannot replace the already independent provenance.
        self.assertIsNone(self.pipeline_result(observed=self.now, captured=self.now))
        self.assertEqual(len(prospective._objects(self.runtime/'prospective','results')), 2)

    def test_same_observation_time_can_upgrade_source_without_score_change(self):
        observed = self.now-timedelta(minutes=1)
        self.pipeline_result(observed=observed, captured=observed+timedelta(seconds=1))
        code, report = self.sync()
        self.assertEqual(code, 0)
        self.assertEqual(report['counts'], {'result_appended':1})
        self.assertEqual(len(prospective._objects(self.runtime/'prospective','results')), 2)

    def test_pipeline_result_outside_revision_window_still_needs_independent_shadow_evidence(self):
        self.pipeline_result()
        self.now += timedelta(days=20)
        self.envelope['fetched_at'] = iso(self.now-timedelta(minutes=1))
        code, report = self.sync()
        self.assertEqual(code, 0, report)
        self.assertEqual(report['counts'], {'result_appended':1})
        self.assertEqual(self.client.requests_made, 1)
        _, second = self.sync()
        self.assertEqual(second['counts'], {'outside_revision_recheck_window':1})
        self.assertEqual(self.client.requests_made, 0)

    def test_method_label_cannot_upgrade_without_independent_raw_source(self):
        self.pipeline_result()
        production = self.runtime/'prospective'
        row = {**self.case.rows[0], 'home_goals':2,'away_goals':1}
        sid = prospective._append(production,'sources',{'kind':'normalized_regulation_data','content':{}})
        with self.assertRaisesRegex(ValueError,'independent_result_upgrade_source_mismatch'):
            prospective.append_result(production,'epl',row,observed=self.now,captured=self.now,source_id=sid,
                forecasts=prospective._objects(self.shadow,'forecasts'), collection_method='independent_fixture_sync')
        self.assertEqual(len(prospective._objects(production,'results')), 1)

    def test_production_only_previous_behavior_is_unchanged(self):
        self.production()
        shutil.rmtree(self.shadow)
        code, report = self.sync()
        self.assertEqual(code, 0)
        self.assertEqual(report['counts'], {'result_appended':1})
        self.assertEqual(report['verified_shadow_forecast_versions'], 0)
        self.assertEqual(report['production_forecast_versions'], 1)


if __name__ == '__main__':
    unittest.main()
