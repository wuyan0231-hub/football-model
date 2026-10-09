import copy
import json
import tempfile
import unittest
from pathlib import Path

from scripts.select_release_league import main, select_league


class ReleaseLeagueTests(unittest.TestCase):
    def setUp(self):
        self.current = json.loads((Path(__file__).parents[1] / "config.json").read_text())
        self.before = copy.deepcopy(self.current)
        self.before["competitions"] = [c for c in self.before["competitions"] if c["key"] != "championship"]

    def test_new_championship_is_the_only_first_training_target(self):
        self.assertEqual(select_league(self.before, self.current), "championship")

    def test_later_releases_keep_existing_unl_check(self):
        self.assertEqual(select_league(self.current, self.current), "unl")

    def test_existing_competition_edit_does_not_trigger_championship_training(self):
        edited = copy.deepcopy(self.current)
        edited["competitions"][0]["label"] = "changed label"
        self.assertEqual(select_league(self.current, edited), "unl")

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
