"""Current entrypoints retire national competition and avoid implicit refits."""
from pathlib import Path
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import textwrap
import unittest

from tests.test_advanced_research_only_workflow import condition, evaluate


ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / '.github/workflows'


class DomesticWorkflowScopeTests(unittest.TestCase):
    def test_production_release_selects_before_guarding_every_state_or_api_step(self):
        text = (WORKFLOWS / 'football.yml').read_text()
        body = text.split('    steps:\n', 1)[1]
        blocks = re.split(r'(?m)^      - ', body)[1:]
        self.assertIn('uses: actions/checkout@v7', blocks[0])
        self.assertEqual(condition(blocks[0]), 'true')
        self.assertIn('id: release', blocks[1])
        self.assertEqual(condition(blocks[1]), "${{ github.event_name == 'push' }}")
        self.assertIn("github.event_name == 'push' && 'none'", text)
        # A no-change release must not restore/write state, call an API, fit,
        # or turn deliberately skipped work into a false job failure.
        for block in blocks[3:]:
            expression = condition(block)
            self.assertIn("env.LEAGUE != 'none'", expression, block.splitlines()[0])
            self.assertFalse(evaluate(expression, {'env': {'LEAGUE': 'none'}}))

    def test_active_entrypoints_have_no_removed_competition_choice_or_collector(self):
        for name in ('football.yml', 'advanced-models.yml', 'fundamentals.yml',
                     'fixed-research-followup-audit.yml', 'fixed-development-gate-audit.yml'):
            text = (WORKFLOWS / name).read_text()
            self.assertNotRegex(text, r'(?i)\bunl\b|欧国联', name)
        self.assertFalse((WORKFLOWS / 'research-attempt2-followup.yml').exists())
        self.assertFalse((WORKFLOWS / 'unl-probability-repair-verification.yml').exists())

    def test_ordinary_release_does_not_launch_historical_research(self):
        text = (WORKFLOWS / 'advanced-models.yml').read_text()
        push = text.split('  push:\n', 1)[1].split('  workflow_dispatch:\n', 1)[0]
        branches = re.search(r'branches: \[([^\]]+)\]', push).group(1).split(', ')
        self.assertNotIn('main', branches)
        self.assertIn('codex/s3-r11-complete', branches)

    def test_retirement_entrypoint_requires_new_exact_branch_main_source_and_first_attempt(self):
        text = (WORKFLOWS / 'retire-competition-once.yml').read_text()
        block = text.split('  retire:\n', 1)[1]
        expression = condition(block, '    ')
        github = {'repository': 'wuyan0231-hub/football-model',
                  'event_name': 'create', 'ref': 'refs/heads/codex/retire-unl-20261019',
                  'run_attempt': 1, 'event': {'repository': {'id': 1380205666},
                  'ref_type': 'branch', 'ref': 'codex/retire-unl-20261019'}}
        self.assertTrue(evaluate(expression, {'github': github}))
        for key, value in (('repository', 'another/football-model'), ('event_name', 'workflow_dispatch'),
                           ('ref', 'refs/heads/main'), ('run_attempt', 2)):
            self.assertFalse(evaluate(expression, {'github': {**github, key: value}}), key)
        for key, value in (('ref_type', 'tag'), ('ref', 'another-branch'),
                           ('repository', {'id': 1})):
            modified = {**github, 'event': {**github['event'], key: value}}
            self.assertFalse(evaluate(expression, {'github': modified}), key)
        self.assertIn('test "$(git rev-parse HEAD)" = "$(git rev-parse FETCH_HEAD)"', text)
        self.assertIn('group: football-model-state', text)
        self.assertIn('cancel-in-progress: false', text)
        self.assertIn('python scripts/cloud_state_save_once.py', text)
        for forbidden in ('workflow_dispatch:', 'schedule:', 'inputs.', 'secrets.',
                          'football_model.cli', '--run-goalmodel', '--auto-activate'):
            self.assertNotIn(forbidden, text)

    def test_actual_inventory_counts_saved_domestic_models_without_freshness_claim(self):
        text = (WORKFLOWS / 'retire-competition-once.yml').read_text()
        script = re.search(r"          python - <<'PY'\n(.*?)          PY", text, re.S).group(1)
        script = textwrap.dedent(script)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = json.loads((ROOT / 'config.json').read_bytes())
            leagues = [c['key'] for c in config['competitions'] if c.get('mode') != 'audit_only']
            self.assertEqual(len(leagues), 18)
            (root / 'config.json').write_text(json.dumps(config))
            path = root / 'runtime/integration/index.json'
            path.parent.mkdir(parents=True)
            rows = [{'key': league, 'status': 'no_run', 'forecast_count': None} for league in leagues]
            path.write_text(json.dumps({'source_commit': 'old-source', 'competitions': rows}))
            model = root / 'runtime/models' / (leagues[0] + '.json')
            model.parent.mkdir(); model.write_bytes(b'{"existing_model":true}')
            activation = root / 'runtime/advanced_models/epl/p/confirmation/d/M/EARLY/activation.json'
            activation.parent.mkdir(parents=True)
            target = activation.parent / 'activations/synthetic.json'
            target.parent.mkdir()
            body = json.dumps({'league': 'epl', 'candidate_id': 'M', 'horizon': 'EARLY',
                               'future_start': '2050-01-01T00:00:00Z',
                               'last_kickoff': '2051-01-01T00:00:00Z'}).encode()
            target.write_bytes(body)
            activation.write_text(json.dumps({'path': target.relative_to(root / 'runtime').as_posix(),
                                               'sha256': hashlib.sha256(body).hexdigest()}))
            queue = root / 'runtime/advanced_snapshots/p/queue/history/synthetic.json'
            queue.parent.mkdir(parents=True)
            queue.write_text(json.dumps({'league': 'epl', 'status': 'late'}))
            environment = {**os.environ, 'GITHUB_SHA': 'current-source', 'GITHUB_RUN_ID': '42',
                           'GITHUB_RUN_ATTEMPT': '1', 'PYTHONPATH': str(ROOT)}
            result = subprocess.run([sys.executable, '-c', script], cwd=root, env=environment,
                                    capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            inventory = json.loads((root / 'domestic-inventory.json').read_bytes())
            self.assertEqual(inventory['domestic_count'], 18)
            self.assertEqual(inventory['saved_model_count'], 1)
            self.assertEqual(inventory['index_original_source_commit'], 'old-source')
            self.assertEqual(inventory['fits'], 0)
            self.assertFalse(inventory['advanced_current_freshness_verified'])
            self.assertFalse(inventory['prediction_improvement_verified'])
            self.assertEqual(inventory['confirmation_pointer_count'], 1)
            self.assertTrue(inventory['activation_inventory_complete'])
            self.assertEqual(inventory['activation_windows'][0]['window_state'], 'before_future_start')
            self.assertEqual(inventory['queue_status_counts'], {'late': 1})
            # A corrupt activation is unknown, never a false zero-active claim.
            target.write_bytes(body + b' ')
            result = subprocess.run([sys.executable, '-c', script], cwd=root, env=environment,
                                    capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            inventory = json.loads((root / 'domestic-inventory.json').read_bytes())
            self.assertFalse(inventory['activation_inventory_complete'])
            self.assertEqual(len(inventory['activation_inventory_errors']), 1)
            path.write_text(json.dumps({'competitions': rows[:-1]}))
            result = subprocess.run([sys.executable, '-c', script], cwd=root, env=environment,
                                    capture_output=True, text=True, timeout=5)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('post_retirement_index_scope_mismatch', result.stderr)


if __name__ == '__main__':
    unittest.main()
