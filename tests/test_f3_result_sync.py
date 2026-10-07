"""Engineering fixtures verify evidence boundaries; no claim of football accuracy."""
import copy
from datetime import timedelta
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import test_prospective as fixtures
from test_cli import BASE_CONFIG, FakeClient, NOW, fixture
from football_model import cli, prospective, result_sync
from football_model.data import APIError, RequestBudgetError


class FixtureClient:
    def __init__(self, envelope):
        self.envelope = envelope
        self.requests_made = 0
        self.calls = []

    def status(self):
        return {}

    def fetch_fixture(self, mid, **kwargs):
        self.requests_made += 1
        self.calls.append((mid, kwargs))
        if isinstance(self.envelope, Exception):
            raise self.envelope
        return copy.deepcopy(self.envelope)


class ResultSyncTests(unittest.TestCase):
    def setUp(self):
        self.case = fixtures.ProspectiveTests()
        self.case.setUp()
        self.addCleanup(self.case.tearDown)
        self.root = self.case.root
        self.case.report['competitions'][0]['provider_league_id'] = 999
        self.case.save_inputs()
        self.case.invoke()
        self.now = fixtures.dt('2026-10-06T21:01:00Z')
        self.item = fixture(22, fixtures.dt(self.case.row['kickoff']), 1, 2, 2, 1)
        self.envelope = {'response': [self.item], 'errors': [], 'paging': {'current': 1, 'total': 1},
            'fetched_at': '2026-10-06T21:00:00Z', 'from_cache': False,
            'endpoint': '/fixtures', 'params': {'id': 22}}

    def sync(self, envelope=None, **kwargs):
        self.client = FixtureClient(self.envelope if envelope is None else envelope)
        return result_sync.run(self.root, self.case.config, client=self.client, clock=lambda: self.now, **kwargs)

    def test_independent_of_missing_model_report_and_training_inputs(self):
        before = {str(p.relative_to(self.root)): p.read_bytes() for sub in ('models', 'data', 'predictions')
                  for p in (self.root/sub).rglob('*.json')}
        (self.root/'reports/latest.json').unlink()
        code, report = self.sync()
        self.assertEqual(code, 0)
        self.assertEqual(report['counts']['result_appended'], 1)
        scores = prospective._objects(self.root/'prospective', 'result_sync_scores')
        self.assertEqual(scores[-1]['groups'][0]['scored'], 1)
        for name, content in before.items():
            self.assertEqual((self.root/name).read_bytes(), content)
        self.assertTrue((self.root/'result_sync/reports/latest.json').is_file())

    def test_aet_uses_fulltime_not_goals_or_extratime(self):
        self.item['fixture']['status']['short'] = 'AET'
        self.item['goals'] = {'home': 5, 'away': 1}
        self.item['score']['extratime'] = {'home': 3, 'away': 0}
        self.assertEqual(self.sync()[0], 0)
        row = prospective._objects(self.root/'prospective', 'results')[0]
        self.assertEqual((row['home_goals'], row['away_goals']), (2, 1))
        self.assertEqual(row['collection_method'], 'independent_fixture_sync')

    def test_pending_status_never_becomes_result_even_with_score(self):
        self.item['fixture']['status']['short'] = '2H'
        code, report = self.sync()
        self.assertEqual(code, 0)
        self.assertEqual(report['counts']['pending_or_not_mature_at_observation'], 1)
        self.assertEqual(prospective._objects(self.root/'prospective', 'results'), [])

    def test_wrong_league_team_kickoff_and_fixture_are_rejected_with_response_retained(self):
        mutations = [lambda i:i['league'].update(id=7), lambda i:i['teams']['home'].update(id=9),
            lambda i:i['fixture'].update(date='2026-10-06T17:00:00Z'), lambda i:i['fixture'].update(id=24)]
        for change in mutations:
            with self.subTest(change=change):
                env = copy.deepcopy(self.envelope)
                change(env['response'][0])
                code, report = self.sync(env)
                self.assertEqual(code, 1)
                self.assertEqual(report['counts']['failed'], 1)
                source_id = report['records'][0]['source_id']
                self.assertTrue((self.root/'prospective/sources'/f'{source_id}.json').exists())
        self.assertEqual(prospective._objects(self.root/'prospective', 'results'), [])

    def test_missing_fulltime_not_substituted(self):
        self.item['score']['fulltime'] = {'home': None, 'away': None}
        self.item['goals'] = {'home': 2, 'away': 1}
        self.assertEqual(self.sync()[0], 1)
        self.assertEqual(prospective._objects(self.root/'prospective', 'results'), [])

    def test_cached_observation_kept_and_immature_cache_not_promoted(self):
        self.envelope.update(fetched_at='2026-10-06T19:59:00Z', from_cache=True)
        code, report = self.sync()
        self.assertEqual(code, 0)
        self.assertEqual(report['counts']['pending_or_not_mature_at_observation'], 1)
        self.assertEqual(self.client.calls[0][1], {'force':False, 'max_age_seconds':600})
        self.envelope['fetched_at'] = '2026-10-06T20:00:00Z'
        self.sync()
        self.assertEqual(prospective._objects(self.root/'prospective', 'results')[0]['observed_at'], '2026-10-06T20:00:00Z')

    def test_retries_idempotent_and_score_revisions_append(self):
        self.sync()
        old = {p.name:p.read_bytes() for p in (self.root/'prospective/results').glob('*.json')}
        self.assertEqual(self.sync()[1]['counts']['result_unchanged'], 1)
        self.envelope['fetched_at'] = '2026-10-06T22:00:00Z'
        self.now = fixtures.dt('2026-10-06T22:01:00Z')
        self.item['score']['fulltime'] = {'home':0,'away':3}
        self.sync()
        results = prospective._objects(self.root/'prospective', 'results')
        self.assertEqual(len(results), 2)
        self.assertIn(next(r for r in results if r['home_goals'] == 0)['supersedes']+'.json', old)
        for name, content in old.items():
            self.assertEqual((self.root/'prospective/results'/name).read_bytes(), content)

    def test_old_pipeline_data_does_not_regress_independent_result(self):
        self.sync()
        self.case.result(home=0, away=0, observed='2026-10-06T20:30:00Z')
        code, summary = prospective.run(self.root,self.case.config,clock=lambda:self.now)
        self.assertEqual(code, 0)
        self.assertEqual(summary['result_versions'], 1)
        self.assertEqual(prospective._objects(self.root/'prospective','results')[0]['home_goals'], 2)

    def test_future_response_and_pagination_fail(self):
        for update in ({'fetched_at':'2026-10-07T00:00:00Z'}, {'paging':{'total':2}},
                       {'paging':{'current':2,'total':1}}, {'results':2}, {'results':True}):
            env = copy.deepcopy(self.envelope);env.update(update)
            self.assertEqual(self.sync(env)[0], 1)
        self.assertEqual(prospective._objects(self.root/'prospective','results'), [])

    def test_budget_deferred_and_client_failure_saved(self):
        code, report = self.sync(RequestBudgetError('quota'))
        self.assertEqual((code,report['counts']), (1,{'budget_deferred':1}))
        client = FixtureClient(self.envelope)
        client.status = lambda: (_ for _ in ()).throw(APIError('unavailable'))
        code, report = result_sync.run(self.root,self.case.config,client=client,clock=lambda:self.now)
        self.assertEqual(report['counts'], {'client_unavailable':1})
        self.assertEqual(code,1)

    def test_no_due_matches_does_not_create_api_client_and_research_is_rejected(self):
        self.now = fixtures.dt('2026-10-06T18:00:00Z')
        with patch('football_model.result_sync.APIClient', side_effect=AssertionError('no credentials needed')):
            code, report = result_sync.run(self.root,{},clock=lambda:self.now)
        self.assertEqual(code,0)
        self.assertEqual(report['counts'],{'not_due':1})
        (self.root/'RESEARCH_REPLAY.json').write_text('{}')
        with self.assertRaisesRegex(ValueError, 'research replay'):
            self.sync()

    def test_partial_fixture_failure_keeps_success(self):
        self.case.row['match_id'] = 23
        self.case.report['run_id'] = 'r2'
        self.case.save_inputs();self.case.invoke()
        class Partial(FixtureClient):
            def fetch_fixture(inner, mid, **kwargs):
                if mid == 22:
                    raise APIError('one fixture fails')
                env=copy.deepcopy(inner.envelope);env['params']['id']=23
                env['response'][0]['fixture']['id']=23
                return env
        code, report=result_sync.run(self.root,self.case.config,client=Partial(self.envelope),clock=lambda:self.now)
        self.assertEqual(code,1)
        self.assertEqual(report['counts'],{'failed':1,'result_appended':1})
        self.assertEqual(prospective._objects(self.root/'prospective','results')[0]['match_id'],23)

    def test_final_report_is_generated_after_score_summary(self):
        ticks=[self.now + timedelta(seconds=i) for i in range(10)]
        values=iter(ticks)
        code, report=result_sync.run(self.root,self.case.config,client=FixtureClient(self.envelope),clock=lambda:next(values))
        self.assertEqual(code,0)
        score=next(r for r in prospective._objects(self.root/'prospective','result_sync_scores')
                   if r['object_id']==report['score_summary_id'])
        self.assertLess(fixtures.dt(report['collection_completed_at']),fixtures.dt(score['generated_at']))
        self.assertLess(fixtures.dt(score['generated_at']),fixtures.dt(report['generated_at']))
        self.assertEqual(report['completed_at'],report['generated_at'])

    def test_result_client_respects_smaller_configured_runtime_budget(self):
        config={**self.case.config,'api_max_run_seconds':12.5}
        with patch('football_model.result_sync.APIClient',return_value=FixtureClient(self.envelope)) as factory:
            code, report=result_sync.run(self.root,config,clock=lambda:self.now)
        self.assertEqual(code,0)
        self.assertEqual(factory.call_args.kwargs['max_run_seconds'],12.5)
        self.assertEqual(report['max_run_seconds'],12.5)
        for invalid in (False,0,-1,float('inf')):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                result_sync.run(self.root,{**config,'api_max_run_seconds':invalid},clock=lambda:self.now)

    def test_recheck_window_does_not_expire_pending_matches(self):
        self.now=fixtures.dt('2026-11-01T21:01:00Z')
        self.envelope['fetched_at']='2026-11-01T21:00:00Z'
        self.assertEqual(self.sync()[1]['counts'],{'result_appended':1})
        self.assertEqual(self.sync()[1]['counts'],{'outside_revision_recheck_window':1})
        self.assertEqual(self.sync(revision_recheck_days=30)[1]['counts'],{'result_unchanged':1})


class TimelyCaptureTests(unittest.TestCase):
    def test_first_league_frozen_before_later_failure_and_kickoff(self):
        config=copy.deepcopy(BASE_CONFIG)
        config['competitions'].append({**config['competitions'][0], 'key':'second'})
        clock=[NOW]
        client=FakeClient()
        client.fixtures[-1]=fixture(2000,NOW+timedelta(minutes=10),1,2,None,None,'NS')
        original=cli.process_competition
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            def process(*args,**kwargs):
                if args[1]['key']=='second':
                    frozen=prospective._objects(root/'prospective','forecasts')
                    self.assertEqual(len(frozen),1)
                    clock[0]=NOW+timedelta(hours=1)
                    raise APIError('later league failed after kickoff')
                return original(*args,**kwargs)
            with patch('football_model.cli.process_competition',side_effect=process):
                code,report=cli.run_pipeline(config,root,'train','all',client=client,clock=lambda:clock[0])
            self.assertEqual(code,2)
            self.assertEqual(report['prospective_captures'][0]['coverage_status_counts'],{'prospective':1})
            frozen=prospective._objects(root/'prospective','forecasts')[0]
            self.assertLess(fixtures.dt(frozen['captured_at']), fixtures.dt(frozen['kickoff']))
            self.assertGreater(fixtures.dt(report['generated_at']),fixtures.dt(frozen['kickoff']))
            code,summary=prospective.run(root,config,clock=lambda:clock[0])
            self.assertEqual(summary['forecast_versions'],1)
            self.assertEqual(summary['coverage_status_counts']['no_current_prediction_file'],1)

    def test_research_pipeline_never_creates_ledger(self):
        with tempfile.TemporaryDirectory() as tmp:
            code,report=cli.run_pipeline(BASE_CONFIG,tmp,'train',client=FakeClient(),clock=lambda:NOW,research_replay=True)
            self.assertEqual(code,0)
            self.assertFalse((Path(tmp)/'prospective').exists())
            self.assertNotIn('prospective_captures',report)

    def test_capture_error_does_not_abort_later_league(self):
        config=copy.deepcopy(BASE_CONFIG)
        config['competitions'].append({**config['competitions'][0],'key':'second'})
        with tempfile.TemporaryDirectory() as tmp:
            with patch('football_model.prospective.capture_report',side_effect=OSError('write failed')) as capture:
                code,report=cli.run_pipeline(config,tmp,'train','all',client=FakeClient(),clock=lambda:NOW)
            self.assertEqual(code,1)
            self.assertEqual(len(report['competitions']),2)
            self.assertEqual(capture.call_count,2)
            self.assertEqual(len(report['prospective_captures']),2)
