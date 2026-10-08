import json
from pathlib import Path
import shutil
import unittest
from unittest.mock import patch

from tests import test_cloud_state as helpers
from football_model import state_storage

cloud = helpers.cloud_state


class CloudStorageMigrationTests(unittest.TestCase):
    setUp = helpers.CloudStateTests.setUp
    write_model = helpers.CloudStateTests.write_model
    clone = helpers.CloudStateTests.clone

    def test_local_bare_git_roundtrip_compresses_history_without_changing_runtime(self):
        cloud.load_state(self.repo)
        self.write_model(self.repo, 1)
        runtime = self.repo / "runtime"
        historical = runtime / "quotes/raw/1/old.json"
        historical.parent.mkdir(parents=True)
        original = b'{ "pad": "' + b"x" * 18000 + b'" }\n'
        historical.write_bytes(original)
        snapshot = runtime / "shadow_models/sources" / ("b" * 64 + ".json")
        snapshot.parent.mkdir(parents=True)
        snapshot.write_bytes(original)
        before = {p.relative_to(runtime).as_posix():p.read_bytes() for p in runtime.rglob("*") if p.is_file()}
        first = cloud.save_state(self.repo)
        state_files = helpers.git(self.remote, "ls-tree", "-r", "--name-only", "model-state").splitlines()
        self.assertIn("state_storage.json", state_files)
        self.assertNotIn("runtime/quotes/raw/1/old.json", state_files)
        self.assertIn("runtime/models/epl.json", state_files)
        self.assertEqual(before, {p.relative_to(runtime).as_posix():p.read_bytes() for p in runtime.rglob("*") if p.is_file()})
        restored = self.clone("migration-restored")
        self.assertEqual(cloud.load_state(restored), first)
        self.assertEqual((restored / "runtime/quotes/raw/1/old.json").read_bytes(), original)
        self.assertEqual((restored / "runtime/shadow_models/sources" / snapshot.name).read_bytes(), original)
        self.write_model(restored, 2)
        second = cloud.save_state(restored)
        self.assertEqual(helpers.git(self.remote, "rev-parse", "model-state^"), first)
        self.assertNotEqual(first, second)

    def test_legacy_plain_state_loads_and_next_save_migrates(self):
        cloud.load_state(self.repo)
        self.write_model(self.repo, 1)
        first = cloud.save_state(self.repo)
        with cloud._worktree(self.repo, first) as tree:
            (tree / state_storage.MANIFEST_NAME).unlink()
            shutil.rmtree(tree / state_storage.ARCHIVE_DIR, ignore_errors=True)
            legacy = tree / "runtime/validation/epl/old.json"
            legacy.parent.mkdir(parents=True)
            legacy.write_bytes(b'{ "legacy":true }\n')
            cloud._git(tree, "add", "-A")
            cloud._git(tree, "-c", "commit.gpgsign=false", "commit", "-m", "Synthetic old plain state", identity=True)
            old = cloud._git(tree, "rev-parse", "HEAD").stdout.strip()
            cloud._git(self.repo, "push", "origin", f"{old}:{cloud.STATE_REF}")
        restored = self.clone("legacy-restored")
        self.assertEqual(cloud.load_state(restored), old)
        self.assertEqual((restored / "runtime/validation/epl/old.json").read_bytes(), b'{ "legacy":true }\n')
        migrated = cloud.save_state(restored)
        self.assertEqual(helpers.git(self.remote, "rev-parse", "model-state^"), old)
        names = helpers.git(self.remote, "ls-tree", "-r", "--name-only", migrated).splitlines()
        self.assertNotIn("runtime/validation/epl/old.json", names)
        self.assertIn(state_storage.MANIFEST_NAME, names)

    def test_failure_does_not_push_or_authorize_partial_restore(self):
        cloud.load_state(self.repo)
        self.write_model(self.repo, 1)
        first = cloud.save_state(self.repo)
        with patch.object(cloud, "_MAX_TOTAL_BYTES", 100):
            with self.assertRaisesRegex(cloud.StateError, "物理"):
                cloud.save_state(self.repo)
        self.assertEqual(helpers.git(self.remote, "rev-parse", "model-state"), first)
        other = self.clone("small-limit")
        with patch.object(cloud, "_MAX_LOGICAL_BYTES", 1):
            with self.assertRaisesRegex(cloud.StateError, "逻辑"):
                cloud.load_state(other)
        self.assertFalse((other / ".git/football-cloud-state.json").exists())
        self.assertFalse((other / "runtime").exists())


if __name__ == "__main__":
    unittest.main()
