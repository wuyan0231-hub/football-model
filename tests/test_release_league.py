import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.select_release_league import main, select_league, read_request
from football_model.run_summary import LEAGUES, main as summary_main


class ReleaseLeagueTests(unittest.TestCase):
    def setUp(self):
        self.current = json.loads((Path(__file__).parents[1] / "config.json").read_text())
        self.before = copy.deepcopy(self.current)
        self.before["competitions"] = [c for c in self.before["competitions"] if c["key"] != "championship"]

    def test_new_championship_is_the_only_first_training_target(self):
        self.assertEqual(select_league(self.before, self.current), "championship")

    def test_summary_registry_accepts_every_real_configured_competition(self):
        self.assertEqual(LEAGUES, {c['key'] for c in self.current['competitions']})

    def test_release_training_target_can_initialize_real_workflow_context(self):
        league = select_league(self.before, self.current)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'context.json'
            with patch.dict('os.environ', {'GITHUB_SHA': 'a' * 40, 'MODE': 'train', 'LEAGUE': league}, clear=True):
                self.assertEqual(summary_main(['begin', '--context', str(path)]), 0)
            self.assertEqual(json.loads(path.read_text())['league'], 'championship')

    def test_later_releases_do_not_trigger_training(self):
        self.assertEqual(select_league(self.current, self.current), "none")

    def test_explicit_request_can_retry_an_already_configured_league(self):
        request = {'schema_version': 1, 'mode': 'train', 'league': 'championship'}
        self.assertEqual(select_league(self.current, self.current, request), 'championship')

    def test_request_rejects_unknown_audit_or_conflicting_targets(self):
        for league in ('unknown', 'ucl', 'championship\nMODE=audit'):
            with self.assertRaises(ValueError):
                select_league(self.current, self.current,
                              {'schema_version': 1, 'mode': 'train', 'league': league})
        with self.assertRaises(ValueError):
            select_league(self.before, self.current,
                          {'schema_version': 1, 'mode': 'train', 'league': 'epl'})
        with self.assertRaises(ValueError):
            select_league(self.current, self.current,
                          {'schema_version': 1, 'mode': 'audit', 'league': 'championship'})

    def test_request_file_is_read_from_the_exact_added_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            request = root / '.github/training-requests/championship-retry.json'
            request.parent.mkdir(parents=True)
            value = {'schema_version': 1, 'mode': 'train', 'league': 'championship'}
            request.write_text(json.dumps(value))
            listing = root / 'added.txt'
            listing.write_text('.github/training-requests/championship-retry.json\n')
            with patch('pathlib.Path.cwd', return_value=root):
                self.assertEqual(read_request(listing), value)
            for invalid in ('../private.json\n', '.github/training-requests/a.json\n.github/training-requests/b.json\n'):
                listing.write_text(invalid)
                with self.assertRaises(ValueError):
                    read_request(listing)

    def test_existing_competition_edit_does_not_trigger_championship_training(self):
        edited = copy.deepcopy(self.current)
        edited["competitions"][0]["label"] = "changed label"
        self.assertEqual(select_league(self.current, edited), "none")

    def test_multiple_or_non_domestic_additions_require_explicit_selection(self):
        for additions in ([{"key": "extra", "mode": "domestic_baseline"}], []):
            current = copy.deepcopy(self.current)
            if additions:
                current["competitions"].extend(additions)
            else:
                next(c for c in current["competitions"] if c["key"] == "championship")["mode"] = "audit_only"
            with self.assertRaises(ValueError):
                select_league(self.before, current)

    def test_duplicate_and_unsafe_keys_cannot_write_workflow_environment(self):
        for key in ("epl", "championship\nMODE=audit"):
            current = copy.deepcopy(self.current)
            current["competitions"].append({"key": key, "mode": "domestic_baseline"})
            with self.assertRaises(ValueError):
                select_league(self.before, current)

    def test_cli_appends_only_validated_league_to_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "before.json").write_text(json.dumps(self.before))
            (root / "current.json").write_text(json.dumps(self.current))
            env = root / "env"
            env.write_text("MODE=train\n")
            self.assertEqual(main(["--before-config", str(root / "before.json"), "--config",
                                   str(root / "current.json"), "--github-env", str(env)]), 0)
            self.assertEqual(env.read_text(), "MODE=train\nLEAGUE=championship\n")


if __name__ == "__main__":
    unittest.main()
