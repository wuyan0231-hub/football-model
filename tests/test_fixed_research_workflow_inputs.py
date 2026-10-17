"""Exercise the exact workflow's pre-network input gate without loading model code."""
from pathlib import Path
import re
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / '.github/workflows/fixed-research-followup-audit.yml'
if not WORKFLOW.exists():
    WORKFLOW = Path(__file__).with_name('reusable-fixed-research-followup-audit.yml')


class FixedWorkflowInputTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = WORKFLOW.read_text()
        match = re.search(r"(?m)^          python - <<'VALIDATE_FIXED_AUDIT'\n(.*?)^          VALIDATE_FIXED_AUDIT$", cls.text, re.DOTALL | re.MULTILINE)
        assert match, 'missing actual pre-network validator'
        cls.validator = '\n'.join(s[10:] for s in match.group(1).splitlines())

    def run_gate(self, league, state='a' * 40):
        # Only two explicit non-sensitive input fields. No repository, credentials,
        # API client, fitting, state restore or network calls are imported here.
        return subprocess.run([sys.executable, '-c', self.validator],
            env={'AUDIT_LEAGUE': league, 'AUDIT_STATE_COMMIT': state},
            text=True, capture_output=True, timeout=5)

    def test_all_nineteen_exact_single_leagues(self):
        choices = re.search(r'options: \[([^\]]+)\]', self.text).group(1).split(', ')
        self.assertEqual(len(choices), 19)
        self.assertEqual(len(set(choices)), 19)
        for league in choices:
            with self.subTest(league=league):
                result = self.run_gate(league)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_multiple_unknown_paths_and_control_keys_rejected(self):
        for league in ('ligue_2,epl', 'epl,epl', '', 'all', 'ucl', '../epl', 'epl/other', 'EPL', 'ligue_2\n', 'epl;echo x'):
            with self.subTest(league=league):
                result = self.run_gate(league)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('invalid_single_audit_league', result.stderr)

    def test_only_full_fixed_lowercase_sha_accepted(self):
        for state in ('main', 'model-state', 'a' * 39, 'a' * 41, 'A' * 40, 'g' * 40, '', 'a' * 40 + '\n', '$(echo x)', 'a' * 40 + ';x'):
            with self.subTest(state=state):
                result = self.run_gate('ligue_2', state)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('exact_fixed_state_sha_required', result.stderr)

    def test_readonly_main_dispatch_and_unchanged_identity(self):
        self.assertIn("AUDIT_LEAGUE: ${{ github.event_name == 'push' && 'ligue_2' || inputs.league }}", self.text)
        self.assertIn("AUDIT_STATE_COMMIT: ${{ github.event_name == 'push' && 'e55c1dad479990567169341624345db239ed8f4e' || inputs.state_commit }}", self.text)
        self.assertIn("github.event_name == 'workflow_dispatch' && github.ref == 'refs/heads/main'", self.text)
        self.assertIn('contents: read\n  actions: read', self.text)
        self.assertIn('timeout-minutes: 45', self.text)
        self.assertIn("FOOTBALL_API_KEY: ''", self.text)
        self.assertIn('--state-commit "$AUDIT_STATE_COMMIT"', self.text)
        self.assertIn('--leagues "$AUDIT_LEAGUE"', self.text)
        self.assertIn('1e311689b3405f64c6ad9636732d2defb7ad67e26b4bc7e04b01a5b1b2b2d6ac', self.text)
        self.assertNotIn('schedule:', self.text)
        self.assertNotIn('contents: write', self.text)
        self.assertNotIn('cloud_state save', self.text)
        self.assertNotIn('cancel-in-progress: true', self.text)


if __name__ == '__main__':
    unittest.main()
