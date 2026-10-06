#!/usr/bin/env python3
"""Isolated all-configured-competition xG sample audit; no training or production writes."""
from __future__ import annotations
import argparse
from collections import Counter
import json
import math
import os
from pathlib import Path
from probe_api import Budget, Client, HTTPTransport, StopProbe, iso, now, parse_time, save

class AuditBudget(Budget):
    def __init__(self):
        # This audit needs metadata, three seasons and paired-statistics checks
        # for 19 competitions. Keep the existing actual daily/minute guards.
        super().__init__(attempts=24, paid=20, daily=5000, reserve=1000)
        self.max_attempts = 130
        self.max_paid = 125

def identity(row, lid, season):
    if row.get('league', {}).get('id') != lid or row.get('league', {}).get('season') != season:
        raise StopProbe('fixture_league_or_season_mismatch')
    fid = row.get('fixture', {}).get('id')
    if type(fid) is not int or fid <= 0:
        raise StopProbe('invalid_fixture_id')
    if row['fixture'].get('status', {}).get('short') != 'FT':
        raise StopProbe('not_completed_regulation_time')
    if parse_time(row['fixture']['date']) >= now():
        raise StopProbe('finished_fixture_in_future')
    return fid

def xg_pair(stats, home_id, away_id):
    states, values = {}, {}
    for tid in (home_id, away_id):
        matches = [r for r in stats if isinstance(r, dict) and r.get('team', {}).get('id') == tid] if isinstance(stats, list) else []
        value = None
        if len(matches) != 1:
            state = 'team_record_absent' if not matches else 'duplicate_team_record'
        else:
            fields = [s for s in matches[0].get('statistics', []) if isinstance(s, dict) and s.get('type') == 'expected_goals']
            if not fields:
                state = 'field_absent'
            elif len(fields) > 1:
                state = 'duplicate_xg_field'
            elif fields[0].get('value') is None:
                state = 'provider_null'
            else:
                try:
                    raw = fields[0]['value']
                    if isinstance(raw, bool):
                        raise ValueError
                    value = float(raw)
                    if not math.isfinite(value) or value < 0:
                        raise ValueError
                    state = 'numeric'
                except (TypeError, ValueError):
                    state, value = 'invalid_numeric', None
        states[str(tid)], values[str(tid)] = state, value
    return {'paired_numeric': all(s == 'numeric' for s in states.values()),
            'team_states': states, 'team_values': values}

def audit(client, competitions):
    report = {'schema_version': 1, 'scope': 'configured_competition_sample_coverage_not_full_season_coverage',
              'started_at': iso(now()), 'competitions': [], 'samples': [], 'errors': [],
              'sample_rule': 'three most recent FT fixtures in each of the latest three started provider seasons; current-season latest sample also checked with direct statistics endpoint',
              'limitations': ['Small deterministic samples do not prove full-season completeness.',
                              'FT only: aggregate AET/PEN statistics may include extra time.',
                              'First observation is not historical pre-match availability.',
                              'xG is not used in production probabilities.']}
    selected = {}
    summary_path = client.out / 'coverage_report.json'
    try:
        client.request('/status', {})
        for c in competitions:
            item = {**c, 'seasons': [], 'errors': []}
            report['competitions'].append(item)
            try:
                rows, query = client.fetch('/leagues', {'id': c['league_id']})
                rows = [r for r in rows if r.get('league', {}).get('id') == c['league_id']]
                if len(rows) != 1:
                    raise StopProbe('league_identity_not_unique')
                item.update(provider_name=rows[0]['league']['name'], country=rows[0]['country']['name'],
                            metadata_source=query['pages'])
                today = now().date().isoformat()
                seasons = sorted([s for s in rows[0]['seasons']
                                  if isinstance(s.get('start'), str) and s['start'] <= today],
                                 key=lambda s: (s['start'], s['year']), reverse=True)[:3]
                for season in seasons:
                    entry = {'year': season['year'], 'start': season['start'], 'coverage_flags': season.get('coverage', {}), 'sample_ids': []}
                    item['seasons'].append(entry)
                    fixtures, fq = client.fetch('/fixtures', {'league': c['league_id'], 'season': season['year'], 'status': 'FT'})
                    for row in fixtures:
                        identity(row, c['league_id'], season['year'])
                    picks = sorted(fixtures, key=lambda r: (parse_time(r['fixture']['date']), r['fixture']['id']), reverse=True)[:3]
                    entry.update(finished_fixture_count=len(fixtures), fixture_source=fq['pages'])
                    for row in picks:
                        fid = row['fixture']['id']
                        if fid in selected:
                            raise StopProbe('duplicate_selected_fixture')
                        selected[fid] = {'key': c['key'], 'league_id': c['league_id'], 'season': season['year'],
                                         'fixture_id': fid, 'kickoff': row['fixture']['date'],
                                         'home_id': row['teams']['home']['id'], 'away_id': row['teams']['away']['id'],
                                         'home_name': row['teams']['home']['name'], 'away_name': row['teams']['away']['name']}
                        entry['sample_ids'].append(fid)
            except StopProbe as e:
                item['errors'].append(str(e))
                if any(word in str(e) for word in ('budget', 'reserve', 'deadline', 'allowance', 'unknown', 'changed_recheck')):
                    raise
            save(summary_path, report)

        fixture_ids = sorted(selected)
        for offset in range(0, len(fixture_ids), 20):
            batch = fixture_ids[offset:offset+20]
            rows, q = client.fetch('/fixtures', {'ids': '-'.join(map(str, batch))})
            if len(rows) != len(batch) or {r['fixture']['id'] for r in rows} != set(batch):
                raise StopProbe('batch_missing_duplicate_or_unexpected_fixtures')
            for row in rows:
                fid = row['fixture']['id']
                original = selected[fid]
                identity(row, original['league_id'], original['season'])
                if any(row['teams'][side]['id'] != original[side+'_id'] for side in ('home', 'away')):
                    raise StopProbe('fixture_team_identity_mismatch')
                if row['fixture']['date'] != original['kickoff']:
                    raise StopProbe('fixture_kickoff_changed')
                sample = {**original, 'statistics_source': q['pages'], 'source_mode': 'batch_embedded',
                          'statistics_rows': row.get('statistics', []),
                          **xg_pair(row.get('statistics', []), original['home_id'], original['away_id'])}
                report['samples'].append(sample)
            save(summary_path, report)

        # Independently verify the latest selected current-season fixture per league.
        for c in report['competitions']:
            if not c['seasons'] or not c['seasons'][0]['sample_ids']:
                continue
            fid = c['seasons'][0]['sample_ids'][0]
            sample = next(s for s in report['samples'] if s['fixture_id'] == fid)
            rows, q = client.fetch('/fixtures/statistics', {'fixture': fid})
            direct = xg_pair(rows, sample['home_id'], sample['away_id'])
            sample.update(batch_xg_pair={k: sample[k] for k in direct},
                          direct_matches_batch=all(sample[k] == direct[k] for k in direct),
                          direct_statistics_source=q['pages'], source_mode='direct_endpoint',
                          statistics_rows=rows, **direct)
            save(summary_path, report)
        report['status'] = 'completed'
    except StopProbe as e:
        report['status'] = 'partial'
        report['errors'].append(str(e))
        client.stopped = str(e)
    finally:
        for c in report['competitions']:
            samples = [s for s in report['samples'] if s['key'] == c['key']]
            c['sampled_fixtures'] = len(samples)
            c['paired_numeric_fixtures'] = sum(s['paired_numeric'] for s in samples)
            c['team_field_states'] = dict(Counter(v for s in samples for v in s['team_states'].values()))
            for season in c['seasons']:
                subset = [s for s in samples if s['season'] == season['year']]
                season.update(sampled=len(subset), paired_numeric=sum(s['paired_numeric'] for s in subset),
                              field_states=dict(Counter(v for s in subset for v in s['team_states'].values())))
        report['finished_at'], report['budget'] = iso(now()), client.budget.snapshot()
        save(summary_path, report)
        client.checkpoint()
    return report

def main():
    p = argparse.ArgumentParser()
    p.add_argument('--output', required=True)
    p.add_argument('--competitions', required=True)
    args = p.parse_args()
    key = os.environ.get('FOOTBALL_API_KEY')
    if not key:
        raise SystemExit('approved_API_credential_not_available')
    comps = json.loads(Path(args.competitions).read_text(encoding='utf-8'))
    if len(comps) != 19 or len({c['league_id'] for c in comps}) != 19:
        raise SystemExit('competition_list_must_match_19_unique_configured_competitions')
    client = Client(args.output, HTTPTransport(key), AuditBudget(), key=key, retry=0,
                    timeout=20, max_seconds=900)
    report = audit(client, comps)
    print(json.dumps({'status': report['status'], 'sampled': len(report['samples']),
                      'paired_xg': sum(s['paired_numeric'] for s in report['samples']),
                      'paid_requests': report['budget']['paid']}, ensure_ascii=False))
    return 0 if report['status'] == 'completed' else 1

if __name__ == '__main__':
    raise SystemExit(main())
