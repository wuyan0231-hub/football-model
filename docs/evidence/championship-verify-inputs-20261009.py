import collections
import hashlib
import json
import platform
from datetime import datetime, timezone
from pathlib import Path

import numpy
import scipy
from football_model.cli import input_fingerprint, select_seasons
from football_model.data import normalize_fixtures
from football_model.production_replay import read_bundle

root = Path.cwd()
runtime = root / 'runtime'
run = '20261009T162510706513Z'

def read(path):
    return json.loads(path.read_text(encoding='utf-8'))

config = read(root / 'config.json')
bundle = read(runtime / 'models/championship.json')
data = read(runtime / 'data/championship.json')
evaluation = read(runtime / 'validation/championship' / f'{run}.json')
report = read(runtime / 'reports/history' / f'{run}.json')
forecasts = read(runtime / 'predictions' / run / 'championship.json')
recording = read_bundle(runtime / report['replay_recording']['path'])
payload = recording['payload']
calls = [e for e in payload['events'] if e['op'] == 'call']
metadata = next(e['value'] for e in calls if e['name'] == 'resolve_league')
fixture_calls = [e for e in calls if e['name'] == 'fetch_fixtures']
assert recording['sha256'] == report['replay_recording']['sha256']
assert payload['recorded_run_id'] == run and payload['league'] == 'championship' and payload['mode'] == 'train'
assert payload['config'] == config and not payload['initial_state']
assert all(hashlib.sha256((root / 'football_model' / name).read_bytes()).hexdigest() == sha
           for name, sha in payload['source_sha256'].items())
assert metadata['league']['id'] == 40 and metadata['league']['name'] == 'Championship' and metadata['country']['name'] == 'England'
selected = select_seasons(metadata, datetime.fromisoformat(bundle['training_cutoff']), config['history_seasons'])
assert [s['year'] for s in selected] == [2026, 2025, 2024]
assert len(fixture_calls) == 3
raw, seasons, extra_time = [], [], []
for event, season in zip(fixture_calls, selected):
    envelope, year = event['value'], season['year']
    assert event['args'] == [40, year] and not envelope['errors'] and envelope['paging']['total'] == 1
    fixtures = envelope['response']
    assert envelope['results'] == len(fixtures)
    assert all(f['league']['id'] == 40 and f['league']['season'] == year for f in fixtures)
    raw.extend(fixtures)
    seasons.append({'year': year, 'source_fetched_at': envelope['fetched_at'], 'paging': envelope['paging'],
                    'errors': envelope['errors'], 'fixtures': len(fixtures),
                    'status_counts': dict(collections.Counter(f['fixture']['status']['short'] for f in fixtures)),
                    'rounds': dict(collections.Counter(f['league'].get('round') for f in fixtures))})
    for fixture in fixtures:
        if fixture['fixture']['status']['short'] == 'AET':
            extra_time.append({'match_id': fixture['fixture']['id'], 'year': year,
                               'round': fixture['league']['round'], 'fulltime': fixture['score']['fulltime'],
                               'extratime': fixture['score']['extratime']})
normalized = normalize_fixtures(raw, bundle['training_cutoff'])
assert normalized == data['matches']
assert len(normalized) == 1209 == bundle['model']['n_matches']
knobs = {key: config[key] for key in ('half_life_days', 'ridge', 'min_train', 'min_test')}
assert input_fingerprint(normalized, knobs, bundle['competition']) == bundle['data_fingerprint']
assert evaluation['report'] == bundle['validation'] and len(evaluation['predictions']) == 245
assert collections.Counter(p['status'] for p in evaluation['predictions']) == {'scored': 217, 'excluded_unknown_team': 28}
assert payload['expected']['predictions']['championship'] == forecasts and len(forecasts) == 24
assert bundle['model']['optimizer']['success'] and not bundle['model']['optimizer']['parameter_at_bound']
cache = []
for path in sorted((runtime / 'cache/api_cache').glob('*.json')):
    document = read(path)
    cache.append({'path': str(path.relative_to(root)), 'fetched_at': document['fetched_at'],
                  'endpoint': document['endpoint'], 'params': document['params'],
                  'paging': document.get('paging'), 'errors': document.get('errors'), 'results': document.get('results')})
result = {'schema_version': 1, 'checked_at': datetime.now(timezone.utc).isoformat(),
          'source_commit': report['source_commit'], 'state_commit': read(root / 'model_access_manifest.json')['state_commit'],
          'input_recording_sha256': recording['sha256'], 'recording_source_fingerprints_verified': True,
          'recording_predictions_match_saved_forecasts': True, 'recording_is_original_production_input_not_new_replay': True,
          'provider_league_id': 40, 'provider_name': 'Championship', 'provider_country': 'England',
          'seasons': seasons, 'original_normalization_matches_saved_training_data': True,
          'original_input_fingerprint_matches_model': True, 'eligible_matches': len(normalized),
          'model_training_matches': bundle['model']['n_matches'], 'team_count': bundle['model']['n_teams'],
          'model_optimizer': bundle['model']['optimizer'], 'holdout_records': 245, 'holdout_scored': 217,
          'holdout_excluded': 28, 'model_validation_matches_original_evaluation_file': True,
          'forecast_count': 24, 'AET_regulation_scores': extra_time, 'current_caches': cache,
          'training_last_match_kickoff': bundle['model']['last_match_kickoff'],
          'data_freshness_scope': 'Last admitted match is 2026-09-20. New fit time does not establish new results or match-specific external checks.',
          'first_inspection_attempt': 'Rejected incorrect checker assumption that per-match validation contained only 217 scored rows. Original validation has all 245 rows: 217 scored and 28 explicitly excluded. Checker corrected; data/model unchanged.',
          'local_environment': {'python': platform.python_version(), 'numpy': numpy.__version__, 'scipy': scipy.__version__}}
(root / 'acceptance/championship-input-verification.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
print(json.dumps({key: value for key, value in result.items() if key not in ('seasons', 'current_caches')}, ensure_ascii=False))
