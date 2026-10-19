"""Guard the separate F5-C release route against unwanted training/API work."""
from pathlib import Path
import re
import unittest


class F5CWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).parents[1]
        self.daily = (self.root / '.github/workflows/football.yml').read_text()
        self.shadow = (self.root / '.github/workflows/football-total-shrink.yml').read_text()

    def test_maintenance_release_only_reaches_shadow_route(self):
        daily_push = self.daily.split('  workflow_dispatch:', 1)[0]
        shadow_push = self.shadow.split('  workflow_dispatch:', 1)[0]
        for name in ('football_model/state_storage.py', 'football_model/run_summary.py',
                     'football_model/storage_policy.py', 'football_model/f5c_access.py',
                     'football_model/integration.py', 'football_model/shadow_models.py', 'scripts/cloud_state.py'):
            self.assertNotIn("'" + name + "'", daily_push)
            self.assertIn("'" + name + "'", shadow_push)
        for name in ('football_model/total_shrink_shadow.py', '.github/workflows/football-total-shrink.yml'):
            self.assertIn("'" + name + "'", shadow_push)
        # Domestic configuration and explicit training requests retain selection.
        for name in ('football_model/cli.py', 'football_model/data.py', 'config.json'):
            self.assertIn("'" + name + "'", daily_push)

    def test_same_state_lock_and_no_added_schedule(self):
        for text in (self.daily, self.shadow):
            self.assertIn('group: football-model-state', text)
            self.assertIn('cancel-in-progress: false', text)
        self.assertNotRegex(self.shadow, r'(?m)^  schedule:')
        self.assertEqual(re.findall(r"cron: '([^']+)'", self.daily), ['17 1 * * *'])

    def test_dedicated_route_has_no_training_or_api_invocation(self):
        for forbidden in ('football_model.cli', 'football_model.shadow_models', 'football_model.result_sync',
                          'football_model.collect_quotes', 'football_model.collect_fundamentals', '--record-replay'):
            self.assertNotIn(forbidden, self.shadow)
        blocks = re.split(r'(?m)^      - name:', self.shadow)
        for block in blocks:
            if 'FOOTBALL_API_KEY:' in block:
                self.assertRegex(block, r'python scripts/cloud_state\.py (check|save) --runtime runtime')
                self.assertNotIn('football_model.total_shrink_shadow', block)
        self.assertIn("'football_model.total_shrink_shadow', 'capture'", self.shadow)
        self.assertIn("'football_model.total_shrink_shadow', 'score'", self.shadow)
        self.assertIn("'--league', 'all'", self.shadow)

    def test_restored_state_provenance_comes_from_load_marker(self):
        for text in (self.daily, self.shadow):
            self.assertIn("Path(git_dir) / 'football-cloud-state.json'", text)
            self.assertIn("marker.get('source_sha') != os.environ['GITHUB_SHA']", text)
            self.assertIn("['--state-commit', marker['state_oid']]", text)
            self.assertNotIn('print(marker', text)

    def test_capture_score_failures_never_hidden_by_continue_on_error(self):
        for text in (self.daily, self.shadow):
            self.assertIn('raise SystemExit(0 if capture == 0 and score == 0 else 1)', text)
            self.assertIn('TOTAL_SHRINK_SHADOW_OUTCOME: ${{ steps.total_shrink_shadow.outcome }}', text)
            self.assertNotIn('.conclusion', text)
            self.assertLess(text.index('id: persist'), text.index('id: summary'))
            self.assertIn('runtime/total_shrink_shadow/', text)
        for stage in ('restore', 'total_shrink_shadow', 'check', 'persist', 'summary'):
            self.assertIn(f"steps.{stage}.outcome != 'success'", self.shadow)
        self.assertIn("steps.total_shrink_shadow.outcome == 'failure'", self.daily)


if __name__ == '__main__':
    unittest.main()
