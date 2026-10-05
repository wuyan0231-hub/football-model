#!/usr/bin/env python3
"""Isolated API-Football capability probe. Stdlib only; never trains or writes production.
Live mode uses an already supplied FOOTBALL_API_KEY (or API_FOOTBALL_KEY) solely
in memory. Importing this module makes no network request and reads no secret.
"""
from __future__ import annotations
import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

BASE = 'https://v3.football.api-sports.io'
LEAGUES = {'unl': 5, 'segunda': 141, 'argentina_a': 128, 'brazil_a': 71}
ALLOWED = {'/status', '/leagues', '/fixtures', '/fixtures/lineups', '/fixtures/events',
           '/fixtures/statistics', '/fixtures/players', '/injuries', '/odds', '/odds/bookmakers', '/odds/bets'}
PAGINATED = {'/odds'}
FEATURES = {'lineups':'lineups', 'events':'events', 'statistics':'statistics_fixtures', 'players':'statistics_players'}
MISSING = object()

class StopProbe(RuntimeError):
    pass

def now():
    return datetime.now(timezone.utc)

def iso(value):
    return value.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')

def parse_time(value):
    d = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if d.tzinfo is None:
        raise ValueError('timestamp_requires_timezone')
    return d.astimezone(timezone.utc)

def digest(value):
    return hashlib.sha256(value).hexdigest()

def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')

def positive(value, zero=False):
    return type(value) is int and value >= (0 if zero else 1)

class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

class Budget:
    def __init__(self, attempts=24, paid=20, daily=5000, reserve=1000):
        for value in (attempts, paid, daily):
            if not positive(value):
                raise ValueError('budget_must_be_positive_integer')
        if attempts > 24 or paid > 20 or not positive(reserve, True):
            raise ValueError('probe_hard_ceiling_is_24_attempts_20_paid')
        self.max_attempts, self.max_paid, self.daily, self.reserve = attempts, paid, daily, reserve
        self.attempts = self.paid = 0
        self.remaining = self.used = self.provider_limit = None
        self.minute_limit = self.minute_remaining = None

    def snapshot(self):
        return dict(self.__dict__)

    def status(self, payload):
        response = payload.get('response', {})
        req = response.get('requests', {})
        active = response.get('subscription', {}).get('active')
        used, limit = req.get('current'), req.get('limit_day')
        if active is not True or not positive(used, True) or not positive(limit) or used > limit:
            raise StopProbe('status_or_daily_budget_unverified')
        self.used, self.provider_limit = used, limit
        status_remaining = max(0, min(self.daily, limit)-used)
        self.remaining = status_remaining if self.remaining is None else min(self.remaining,status_remaining)

    def observe_headers(self, headers):
        h = {str(k).lower(): v for k,v in headers.items()}
        def number(k):
            try:
                v = int(h[k]); return v if v >= 0 else None
            except (KeyError, TypeError, ValueError):
                return None
        lim, rem = number('x-ratelimit-requests-limit'), number('x-ratelimit-requests-remaining')
        if lim is not None and lim > 0 and rem is not None:
            if rem > lim:
                raise StopProbe('contradictory_daily_headers')
            candidate = min(rem, max(0, min(self.daily, lim)-(lim-rem)))
            # Same bounded run never assumes a day reset; shared external usage can only reduce allowance.
            self.remaining = candidate if self.remaining is None else min(self.remaining,candidate)
            self.used, self.provider_limit = lim-rem, lim
        elif rem is not None and self.remaining is not None:
            self.remaining = min(self.remaining,rem)
        self.minute_limit = number('x-ratelimit-limit') or self.minute_limit
        observed = number('x-ratelimit-remaining')
        if observed is not None:
            self.minute_remaining = observed

    def check(self, paid):
        if self.attempts >= self.max_attempts:
            raise StopProbe('attempt_budget_reached')
        if paid:
            if self.remaining is None:
                raise StopProbe('daily_remaining_unknown')
            if self.paid >= self.max_paid:
                raise StopProbe('paid_budget_reached')
            if self.remaining <= self.reserve:
                raise StopProbe('production_daily_reserve_protected')
        if self.minute_remaining == 0:
            raise StopProbe('minute_allowance_exhausted_stop_without_retry')

    def consume(self, paid):
        self.check(paid)
        self.attempts += 1
        if paid:
            self.paid += 1
            self.remaining -= 1
        if self.minute_remaining is not None:
            self.minute_remaining = max(0,self.minute_remaining-1)

class HTTPTransport:
    def __init__(self, key):
        self.key = key
        self.opener = build_opener(NoRedirect()).open

    def __call__(self, endpoint, params, timeout):
        url = BASE + endpoint + ('?' + urlencode(sorted(params.items())) if params else '')
        request = Request(url, headers={'x-apisports-key':self.key, 'Accept':'application/json'}, method='GET')
        try:
            with self.opener(request, timeout=timeout) as r:
                # Read up to 16 MiB; a pathological response cannot fill the workspace.
                body = r.read(16*1024*1024+1)
                if len(body)>16*1024*1024:
                    raise StopProbe('response_size_limit')
                return r.status,dict(r.headers.items()),body
        except HTTPError as e:
            return e.code,dict(e.headers.items()) if e.headers else {},e.read(16384)

class Client:
    def __init__(self, out, transport, budget=None, key='', retry=1, timeout=20,
                 max_seconds=300, clock=now, mono=time.monotonic, sleep=time.sleep):
        if retry not in (0,1) or not 0 < timeout <= 60 or not 0 < max_seconds <= 1800:
            raise ValueError('invalid_retry_or_timeout')
        self.out, self.transport, self.budget = Path(out), transport, budget or Budget()
        self.key,self.retry,self.timeout,self.max_seconds = key,retry,timeout,max_seconds
        self.clock,self.mono,self.sleep=clock,mono,sleep
        self.started,self.start_day,self.last=mono(),clock().date(),None
        self.attempts,self.queries=[],[]
        self.out.mkdir(parents=True,exist_ok=False)
        (self.out/'responses').mkdir()
        self.stopped = None

    def checkpoint(self):
        save(self.out/'transport_manifest.json', {'schema_version':1,'budget':self.budget.snapshot(),
             'attempts':self.attempts,'queries':self.queries,'stopped_reason':self.stopped,
             'note':'No request headers, credentials, or account identity are persisted.'})

    def before(self, paid):
        self.budget.check(paid)
        if self.clock().date()!=self.start_day:
            raise StopProbe('utc_day_changed_recheck_status_in_new_run')
        interval = max(0.25, 60/self.budget.minute_limit+0.05) if self.budget.minute_limit else 6.5
        wait = max(0,interval-(self.mono()-self.last)) if self.last is not None else 0
        if self.mono()-self.started+wait+self.timeout >= self.max_seconds:
            raise StopProbe('soft_deadline_protected')
        if wait:
            self.sleep(wait)
        self.budget.consume(paid)
        self.last=self.mono()

    def request(self, endpoint, params):
        if endpoint not in ALLOWED:
            raise StopProbe('endpoint_not_allowed')
        retries = 0 if endpoint=='/status' else self.retry
        for attempt in range(retries+1):
            self.before(endpoint!='/status')
            rec={'attempt':self.budget.attempts,'endpoint':endpoint,'params':params,
                 'started_at':iso(self.clock()),'budget_before_response':self.budget.snapshot()}
            self.attempts.append(rec)
            try:
                code,headers,body=self.transport(endpoint,params,self.timeout)
                rec.update(http_status=code,received_at=iso(self.clock()),original_body_sha256=digest(body))
                self.budget.observe_headers(headers)
                if self.key and self.key.encode() in body:
                    rec['credential_echo_detected']=True
                    raise StopProbe('provider_echoed_credential_body_not_saved')
            except (URLError,TimeoutError,OSError):
                rec.update(error='network_or_timeout',received_at=iso(self.clock()))
                self.checkpoint()
                if attempt < retries:
                    continue
                raise StopProbe('network_or_timeout') from None
            self.checkpoint()
            if code!=200:
                # Error bodies can contain account/proxy diagnostics; keep hash, never raw text.
                rec['error']='http_'+str(code)
                if code in (499,500,502,503,504) and attempt < retries:
                    continue
                raise StopProbe('http_'+str(code))
            try:
                payload=json.loads(body)
            except (ValueError,UnicodeDecodeError):
                raise StopProbe('invalid_json') from None
            if not isinstance(payload,dict) or not isinstance(payload.get('errors'),(list,dict)):
                raise StopProbe('invalid_response_envelope')
            if payload['errors']:
                rec['provider_error_count']=len(payload['errors'])
                raise StopProbe('provider_error')
            stored=body
            if endpoint=='/status':
                self.budget.status(payload)
                r=payload['response']
                stored=json.dumps({'response':{'requests':{k:r['requests'].get(k) for k in ('current','limit_day')},
                     'subscription':{k:r.get('subscription',{}).get(k) for k in ('active','plan')}}},ensure_ascii=False).encode()
                rec['storage_transform']='status_account_identity_removed'
            if self.key and self.key.encode() in stored:
                stored=stored.replace(self.key.encode(),b'[REDACTED]')
                rec['credential_echo_redacted']=True
                raise StopProbe('provider_echoed_credential_body_not_saved')
            name=f"responses/{self.budget.attempts:03d}_{endpoint.strip('/').replace('/','_')}.json"
            (self.out/name).write_bytes(stored)
            rec.update(source_path=name,saved_body_sha256=digest(stored),budget_after=self.budget.snapshot())
            self.checkpoint()
            return payload,rec
        raise StopProbe('request_not_completed')

    def fetch(self, endpoint, params, max_pages=8):
        query={'endpoint':endpoint,'params':dict(params),'pages':[], 'paging_complete':False}
        self.queries.append(query)
        items=[]; expected_total=None
        try:
            for page in range(1,max_pages+1):
                paged=dict(params)
                if endpoint in PAGINATED:
                    paged['page']=page
                payload,rec=self.request(endpoint,paged)
                paging=payload.get('paging')
                if not isinstance(paging,dict) or not positive(paging.get('current')) or not positive(paging.get('total')):
                    raise StopProbe('invalid_paging_envelope')
                current,total=paging['current'],paging['total']
                if current!=page or (expected_total is not None and total!=expected_total):
                    raise StopProbe('paging_changed_or_repeated')
                if endpoint not in PAGINATED and total!=1:
                    raise StopProbe('unexpected_pagination_on_nonpaged_endpoint')
                expected_total=total
                rows=payload.get('response')
                if not isinstance(rows,list) or type(payload.get('results')) is not int or payload['results']!=len(rows):
                    raise StopProbe('response_results_mismatch')
                fingerprint=digest(json.dumps(rows,sort_keys=True).encode())
                if any(p['rows_sha256']==fingerprint for p in query['pages']):
                    raise StopProbe('duplicate_page_content')
                query['pages'].append({'page':page,'total':total,'source_path':rec['source_path'],
                                       'received_at':rec['received_at'],'rows_sha256':fingerprint})
                items.extend(rows)
                if current==total:
                    query['paging_complete']=True
                    return items,query
            raise StopProbe('page_ceiling_reached_partial_query')
        except StopProbe as e:
            query['stopped_reason']=str(e)
            raise
        finally:
            query['items_received']=len(items)
            self.checkpoint()


def field_profile(records):
    """Union of observed paths plus missing/null counts at each object location."""
    groups={}
    def walk(x,path):
        if isinstance(x,dict):
            groups.setdefault(path,[]).append(x)
            for k,v in x.items(): walk(v,path+'.'+k)
        elif isinstance(x,list):
            for v in x: walk(v,path+'[]')
    for item in records: walk(item,'$')
    output={}
    for path,objects in groups.items():
        for key in sorted(set().union(*(set(x) for x in objects))):
            present=[o[key] for o in objects if key in o]
            output[path+'.'+key]={'parent_objects':len(objects),'missing':len(objects)-len(present),
                'null':sum(v is None for v in present),'empty_list':sum(v==[] for v in present),
                'types':dict(Counter(type(v).__name__ for v in present)),
                'null_rate_among_present':sum(v is None for v in present)/len(present)}
    return output

def observed(value):
    if value is MISSING: return 'field_absent'
    if value is None: return 'explicit_null'
    if value==[]: return 'empty_list_not_evidence_of_none_in_reality'
    return 'nonempty_observed'

def statistics_profile(rows):
    out=[]
    for row in rows:
        if not isinstance(row,dict): continue
        for key,value in row.items():
            if key in {'statistics','statistics_1h','statistics_2h'} and isinstance(value,list):
                for s in value:
                    if not isinstance(s,dict): continue
                    v=s.get('value',MISSING)
                    kind='missing' if v is MISSING else ('null' if v is None else type(v).__name__)
                    unit='percent_string' if isinstance(v,str) and v.endswith('%') else 'provider_unspecified'
                    name=s.get('type')
                    out.append({'team_id':row.get('team',{}).get('id'),'period':key,
                     'name':name,'value_kind':kind,'unit':unit,'value':None if v is MISSING else v,
                     'xg_name_candidate':isinstance(name,str) and ('expected' in name.lower() or 'xg' in name.lower())})
    return out

def fixture_identity(item,selected):
    fid=item.get('fixture',{}).get('id')
    if fid not in selected: raise StopProbe('unexpected_fixture_identity')
    expect=selected[fid]
    if item.get('league',{}).get('id')!=expect['league_id'] or item.get('league',{}).get('season')!=expect['season']:
        raise StopProbe('fixture_league_or_season_mismatch')
    parse_time(item['fixture']['date'])
    for side in ('home','away'):
        if side+'_id' in expect and item.get('teams',{}).get(side,{}).get('id')!=expect[side+'_id']:
            raise StopProbe('fixture_team_identity_mismatch')
    return fid


def probe(client, season=2026, samples=None, cutoff=None, half=True):
    report={'schema_version':1,'scope':'capability_only_not_prediction_validation',
      'cutoff':iso(cutoff) if cutoff else None,'coverage':[],'samples':[], 'skips':[],
      'provider_first_available_at':None,
      'time_rule':'First observed in this run does not prove provider first availability or historical pre-match availability.'}
    try:
        client.request('/status',{})
        covers={}
        for key,lid in LEAGUES.items():
            rows,q=client.fetch('/leagues',{'id':lid})
            matching=[r for r in rows if r.get('league',{}).get('id')==lid]
            if len(matching)!=1: raise StopProbe('league_identity_not_unique')
            for s in matching[0].get('seasons',[]):
                covers[(lid,s.get('year'))]=s.get('coverage',{})
            report['coverage'].append({'league':key,'league_id':lid,'seasons':matching[0].get('seasons',[]),
                                       'source_pages':q['pages']})
        selected={}
        if samples is None:
            for key,lid in LEAGUES.items():
                rows,q=client.fetch('/fixtures',{'league':lid,'season':season})
                if any(r.get('league',{}).get('id')!=lid or r.get('league',{}).get('season')!=season for r in rows):
                    raise StopProbe('fixture_list_league_mismatch')
                finished=sorted([r for r in rows if r.get('fixture',{}).get('status',{}).get('short') in ('FT','AET','PEN')],
                                  key=lambda r:parse_time(r['fixture']['date']))
                upcoming=sorted([r for r in rows if r.get('fixture',{}).get('status',{}).get('short')=='NS'
                                  and parse_time(r['fixture']['date'])>client.clock()],key=lambda r:parse_time(r['fixture']['date']))
                picks=finished[-1:]+upcoming[:1]
                for r in picks:
                    selected[r['fixture']['id']]={'league_id':lid,'season':season,'purpose':'finished' if r in finished else 'upcoming'}
                if not picks: report['skips'].append({'league_id':lid,'reason':'no_finished_or_future_sample'})
        else:
            if not isinstance(samples,list) or not 1<=len(samples)<=20:
                raise StopProbe('samples_must_be_1_to_20_records')
            for s in samples:
                if s.get('league_id') not in LEAGUES.values() or not positive(s.get('fixture_id')) or not positive(s.get('season')):
                    raise StopProbe('invalid_sample_identity')
                if s['fixture_id'] in selected: raise StopProbe('duplicate_sample_id')
                selected[s['fixture_id']]=dict(s)
        if not selected: raise StopProbe('no_samples_selected')
        rows,q=client.fetch('/fixtures',{'ids':'-'.join(str(x) for x in sorted(selected))})
        if len(rows)!=len(selected) or len({x.get('fixture',{}).get('id') for x in rows})!=len(selected):
            raise StopProbe('batch_missing_or_duplicate_fixtures')
        for row in rows:
            fid=fixture_identity(row,selected)
            received=q['pages'][0]['received_at']; kickoff=row['fixture']['date']
            s={'fixture_id':fid,'league_id':row['league']['id'],'season':row['league']['season'],
               'kickoff':kickoff,'status':row['fixture']['status'],'batch_source':q['pages'],
               'input_sample':selected[fid],
               'kickoff_changed_from_input':selected[fid].get('source_kickoff') not in (None,kickoff),
               'statistics_regulation_time_qualification':'FT_only_90_plus_stoppage' if row['fixture']['status'].get('short')=='FT' else 'unverified_for_90_minutes; AET_PEN_may_include_extra_time; schema_probe_only',
               'first_observed_in_this_run':received,'provider_first_available_at':None,
               'eligible_snapshot_before_cutoff':parse_time(received)<=cutoff if cutoff else None,
               'observed_before_kickoff':parse_time(received)<parse_time(kickoff),
               'field_profile':field_profile([row]),'modules':{},'statistics_fields':statistics_profile(row.get('statistics',[]))}
            for module in FEATURES:
                value=row.get(module,MISSING)
                s['modules'][module]={'observation':observed(value),'source':'batch',
                     'coverage_flag':covers.get((s['league_id'],s['season']),{}).get('fixtures',{}).get(FEATURES[module])}
            report['samples'].append(s)
        save(client.out/'capability_report.json',report)
        # Batch supplies per-fixture lineups/events/statistics/players. Absent modules
        # get endpoint fallback only on one finished sample per league, in module rounds.
        targets=[]
        for lid in LEAGUES.values():
            group=[s for s in report['samples'] if s['league_id']==lid and s['status'].get('short') in ('FT','AET','PEN')]
            if group:
                ft=[s for s in group if s['status'].get('short')=='FT']
                targets.append(max(ft or group,key=lambda s:parse_time(s['kickoff'])))
        for module in FEATURES:
            for s in targets:
                info=s['modules'][module]
                if info['coverage_flag'] is not True or info['observation']!='field_absent': continue
                detail,dq=client.fetch('/fixtures/'+module,{'fixture':s['fixture_id']})
                info.update(observation=observed(detail),source=dq['pages'],field_profile=field_profile(detail))
                if module=='statistics': s['statistics_fields']=statistics_profile(detail)
        if half:
            for s in targets:
                if s['modules']['statistics']['coverage_flag'] is not True: continue
                detail,dq=client.fetch('/fixtures/statistics',{'fixture':s['fixture_id'],'half':'true'})
                s['half_statistics']={'fields':statistics_profile(detail),'field_profile':field_profile(detail),'source':dq['pages']}
        for s in targets:
            flag=covers.get((s['league_id'],s['season']),{}).get('injuries')
            if flag is not True:
                report['skips'].append({'fixture_id':s['fixture_id'],'module':'injuries','reason':'coverage_false' if flag is False else 'coverage_unknown'})
                continue
            detail,dq=client.fetch('/injuries',{'fixture':s['fixture_id']})
            s['injuries']={'observation':observed(detail),'field_profile':field_profile(detail),'source':dq['pages'],
                          'interpretation':'empty_response_does_not_establish_no_injuries; historical_fetch_not_pre_match_evidence'}
        future=[s for s in report['samples'] if s['status'].get('short')=='NS' and parse_time(s['kickoff'])>client.clock()]
        future=[s for s in future if covers.get((s['league_id'],s['season']),{}).get('odds') is True]
        if future:
            for endpoint in ('/odds/bookmakers','/odds/bets'):
                maps,mq=client.fetch(endpoint,{})
                report[endpoint.strip('/').replace('/','_')]={'records':maps,'source':mq['pages']}
            for s in future:
                detail,dq=client.fetch('/odds',{'fixture':s['fixture_id']},max_pages=8)
                if any(d.get('fixture',{}).get('id')!=s['fixture_id'] for d in detail):
                    raise StopProbe('odds_fixture_identity_mismatch')
                s['odds']={'observation':observed(detail),'field_profile':field_profile(detail),'source':dq['pages'],
                           'provider_update_values':[d.get('update') for d in detail],
                           'contract_mapping_to_user_screenshots':'unverified', 'actual_execution':'unknown'}
        report['status']='completed_configured_scope'
    except StopProbe as e:
        report['status']='partial_stopped'
        report['stopped_reason']=str(e)
        client.stopped=str(e)
    except (KeyError,TypeError,ValueError,AttributeError,OverflowError):
        report['status']='partial_stopped'
        report['stopped_reason']='response_structure_or_value_invalid'
        client.stopped=report['stopped_reason']
    finally:
        report['finished_at']=iso(client.clock())
        report['budget']=client.budget.snapshot()
        save(client.out/'capability_report.json',report)
        client.checkpoint()
    return report


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output',required=True,help='New directory, must not exist')
    p.add_argument('--season',type=int,default=2026)
    p.add_argument('--samples',type=Path,help='Optional 1–20 fixture_id/league_id/season objects')
    p.add_argument('--cutoff',type=parse_time,help='Comparison timestamp only; cannot relabel fetched_at')
    p.add_argument('--production-reserve',type=int,default=1000)
    p.add_argument('--daily-limit',type=int,default=5000)
    p.add_argument('--max-attempts',type=int,default=24)
    p.add_argument('--max-paid',type=int,default=20)
    p.add_argument('--no-half',action='store_true')
    p.add_argument('--plan-only',action='store_true',help='Print nonsecret plan, no credentials read, no network')
    args=p.parse_args()
    budget=Budget(args.max_attempts,args.max_paid,args.daily_limit,args.production_reserve)
    if args.plan_only:
        print(json.dumps({'mode':'plan_only','leagues':LEAGUES,'season':args.season,
           'budget':budget.snapshot(),'selection':'one latest finished and one next NS fixture per league unless explicit samples',
           'production_writes':False},ensure_ascii=False,indent=2))
        return 0
    # Existing environment injection only. Do not search credential files or prompt for a key.
    key=os.environ.get('FOOTBALL_API_KEY') or os.environ.get('API_FOOTBALL_KEY')
    if not key:
        print(json.dumps({'status':'not_run','reason':'existing_credential_environment_unavailable','network_attempts':0}))
        return 2
    samples=json.loads(args.samples.read_text(encoding='utf-8')) if args.samples else None
    c=Client(args.output,HTTPTransport(key),budget=budget,key=key)
    report=probe(c,args.season,samples,args.cutoff,not args.no_half)
    print(json.dumps({k:report.get(k) for k in ('status','stopped_reason','budget')},ensure_ascii=False))
    return 0 if report['status']=='completed_configured_scope' else 3

if __name__=='__main__':
    raise SystemExit(main())
