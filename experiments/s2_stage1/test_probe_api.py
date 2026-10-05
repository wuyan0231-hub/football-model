import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
from urllib.error import URLError
import probe_api as p

class FakeTime:
    def __init__(self): self.t=0
    def mono(self): return self.t
    def now(self): return datetime(2026,10,5,17,tzinfo=timezone.utc)+timedelta(seconds=self.t)
    def sleep(self,n): self.t+=n

def response(rows=None,current=1,total=1):
    rows=[] if rows is None else rows
    return {'get':'test','errors':[],'results':len(rows),'paging':{'current':current,'total':total},'response':rows}

def status(used=20,limit=7500,active=True):
    return {'get':'status','errors':[],'response':{'account':{'email':'PRIVATE'},
         'requests':{'current':used,'limit_day':limit,'private':'PRIVATE'},
         'subscription':{'active':active,'plan':'PRO','private':'PRIVATE'}}}

class ProbeTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.clock=FakeTime()
    def client(self,responses,budget=None,**kwargs):
        queue=list(responses)
        self.calls=[]
        def transport(endpoint,params,timeout):
            self.calls.append((endpoint,params))
            item=queue.pop(0)
            if isinstance(item,Exception): raise item
            if isinstance(item,tuple): return item
            return 200,{},json.dumps(item).encode()
        return p.Client(Path(self.tmp.name)/'out',transport,budget=budget,clock=self.clock.now,
              mono=self.clock.mono,sleep=self.clock.sleep,**kwargs)
    def test_unknown_balance_stops_before_network(self):
        c=self.client([])
        with self.assertRaisesRegex(p.StopProbe,'daily_remaining_unknown'): c.fetch('/leagues',{'id':5})
        self.assertEqual(self.calls,[])
    def test_status_respects_lower_header_balance(self):
        body=status()
        c=self.client([(200,{'x-ratelimit-requests-limit':'7500','x-ratelimit-requests-remaining':'10'},json.dumps(body).encode())])
        c.request('/status',{})
        self.assertEqual(c.budget.remaining,0)
        with self.assertRaisesRegex(p.StopProbe,'reserve'): c.fetch('/leagues',{'id':5})
        self.assertEqual(len(self.calls),1)
    def test_attempt_budget_counts_status_retry(self):
        c=self.client([status(),URLError('SECRET'),response()],p.Budget(attempts=2))
        c.request('/status',{})
        with self.assertRaisesRegex(p.StopProbe,'attempt_budget'): c.fetch('/leagues',{'id':5})
        self.assertEqual(c.budget.attempts,2);self.assertEqual(c.budget.paid,1)
        self.assertNotIn('SECRET',(c.out/'transport_manifest.json').read_text())
    def test_reserve_blocks_retry_after_failed_paid_attempt(self):
        c=self.client([status(3999,5000),URLError('failure'),response()])
        c.request('/status',{})
        with self.assertRaisesRegex(p.StopProbe,'reserve'): c.fetch('/leagues',{'id':5})
        self.assertEqual(c.budget.remaining,1000);self.assertEqual(len(self.calls),2)
    def test_paid_budget_counts_pagination(self):
        c=self.client([status(),response([{'id':1}],1,2),response([{'id':2}],2,2)],p.Budget(paid=1))
        c.request('/status',{})
        with self.assertRaisesRegex(p.StopProbe,'paid_budget'): c.fetch('/odds',{'fixture':100})
        self.assertFalse(c.queries[0]['paging_complete']);self.assertEqual(c.queries[0]['items_received'],1)
    def test_pagination_success_and_params(self):
        c=self.client([status(),response([{'id':1}],1,2),response([{'id':2}],2,2)])
        c.request('/status',{});rows,q=c.fetch('/odds',{'fixture':100})
        self.assertEqual(len(rows),2);self.assertTrue(q['paging_complete'])
        self.assertEqual([x[1].get('page') for x in self.calls[1:]],[1,2])
    def test_pagination_repeat_stops(self):
        c=self.client([status(),response([{'id':1}],1,2),response([{'id':2}],1,2)])
        c.request('/status',{})
        with self.assertRaisesRegex(p.StopProbe,'paging_changed'): c.fetch('/odds',{'fixture':100})
        self.assertFalse(c.queries[0]['paging_complete'])
    def test_page_ceiling_is_partial(self):
        c=self.client([status(),response([{'id':1}],1,2)])
        c.request('/status',{})
        with self.assertRaisesRegex(p.StopProbe,'page_ceiling'): c.fetch('/odds',{'fixture':100},max_pages=1)
        self.assertFalse(c.queries[0]['paging_complete'])
    def test_429_stops_without_retry(self):
        c=self.client([status(),(429,{},b'account details')])
        c.request('/status',{})
        with self.assertRaisesRegex(p.StopProbe,'http_429'): c.fetch('/leagues',{'id':5})
        self.assertEqual(len(self.calls),2)
        self.assertNotIn('account details',''.join(x.read_text() for x in c.out.rglob('*.json')))
    def test_status_no_pii_or_full_requests(self):
        c=self.client([status()]);c.request('/status',{})
        self.assertNotIn('PRIVATE',''.join(x.read_text() for x in c.out.rglob('*.json')))
    def test_error_key_credential_echo_never_persisted(self):
        c=self.client([status(),{'errors':{'FAKE_SECRET':'value'},'response':[]}],key='FAKE_SECRET')
        c.request('/status',{})
        with self.assertRaisesRegex(p.StopProbe,'credential'): c.fetch('/leagues',{'id':5})
        c.checkpoint()
        self.assertNotIn('FAKE_SECRET',''.join(x.read_text() for x in c.out.rglob('*.json')))
    def test_software_deadline_prevents_network(self):
        c=self.client([],max_seconds=10,timeout=20)
        with self.assertRaisesRegex(p.StopProbe,'deadline'): c.request('/status',{})
        self.assertEqual(self.calls,[])
    def test_utc_rollover_stops(self):
        c=self.client([status()]);c.request('/status',{});self.clock.t=86400
        with self.assertRaisesRegex(p.StopProbe,'utc_day'):c.fetch('/leagues',{'id':5})
    def test_inactive_status_stops_and_report_exists(self):
        c=self.client([status(active=False)])
        r=p.probe(c)
        self.assertEqual(r['status'],'partial_stopped');self.assertEqual(c.budget.paid,0)
    def test_malformed_nested_status_becomes_partial(self):
        c=self.client([{'errors':[],'response':[]}])
        r=p.probe(c)
        self.assertEqual(r['status'],'partial_stopped')
        self.assertEqual(r['stopped_reason'],'response_structure_or_value_invalid')
    def test_missing_null_zero_are_distinct(self):
        prof=p.field_profile([{'minutes':0},{'minutes':None},{}])['$.minutes']
        self.assertEqual((prof['missing'],prof['null'],prof['parent_objects']),(1,1,3))
    def test_end_to_end_cutoff_coverage_empty_and_attempts(self):
        samples=[];fixtures=[];coverage=[]
        for lid in p.LEAGUES.values():
            samples.append({'fixture_id':lid*100,'league_id':lid,'season':2026})
            fixtures.append({'fixture':{'id':lid*100,'date':'2026-10-04T12:00:00Z','status':{'short':'FT'}},
                 'league':{'id':lid,'season':2026},'teams':{},'lineups':[], 'events':[], 'statistics':[], 'players':[]})
            coverage.append(response([{'league':{'id':lid},'seasons':[{'year':2026,'coverage':{
                'fixtures':{x:True for x in p.FEATURES.values()},'injuries':lid==71,'odds':True}}]}]))
        c=self.client([status(),*coverage,response(fixtures),response()])
        r=p.probe(c,samples=samples,cutoff=p.parse_time('2026-10-01T00:00:00Z'),half=False)
        self.assertEqual(r['status'],'completed_configured_scope')
        self.assertEqual((r['budget']['attempts'],r['budget']['paid']),(7,6))
        self.assertEqual(len(r['samples']),4)
        self.assertTrue(all(s['eligible_snapshot_before_cutoff'] is False for s in r['samples']))
        self.assertTrue(all(s['provider_first_available_at'] is None for s in r['samples']))
        self.assertTrue(all(s['first_observed_in_this_run']==c.attempts[5]['received_at'] for s in r['samples']))
        self.assertEqual(len([x for x in r['skips'] if x['reason']=='coverage_false']),3)
        inj=[x for x in self.calls if x[0]=='/injuries']
        self.assertEqual(inj,[('/injuries',{'fixture':7100})])
        brazil=[s for s in r['samples'] if s['league_id']==71][0]
        self.assertEqual(brazil['injuries']['observation'],'empty_list_not_evidence_of_none_in_reality')
    def test_unexpected_redirect_not_followed(self):
        self.assertIsNone(p.NoRedirect().redirect_request(None,None,302,'',{},'https://bad.invalid'))
    def test_raw_body_hash_and_bytes_retained(self):
        raw=b'{ "errors": [], "results": 0, "paging": {"current":1,"total":1}, "response": [] }'
        c=self.client([status(),(200,{},raw)]);c.request('/status',{});c.fetch('/leagues',{'id':5})
        rec=c.attempts[-1]
        self.assertEqual((c.out/rec['source_path']).read_bytes(),raw)
        self.assertEqual(rec['original_body_sha256'],p.digest(raw))
    def test_envelope_missing_paging_stops(self):
        c=self.client([status(),{'errors':[],'response':[],'results':0}]);c.request('/status',{})
        with self.assertRaisesRegex(p.StopProbe,'invalid_paging'):c.fetch('/leagues',{'id':5})
    def test_no_unexpected_endpoint(self):
        c=self.client([])
        with self.assertRaisesRegex(p.StopProbe,'endpoint_not_allowed'):c.request('https://bad.invalid',{})
        self.assertEqual(self.calls,[])

if __name__=='__main__': unittest.main(verbosity=2)
