"""Real Git save peaks, old-object reuse, and exact guard reconciliation."""
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tests import test_cloud_state as helpers
from football_model.advanced_capacity import CapacityGuard, CapacityDeferred
from football_model import state_storage

cloud = helpers.cloud_state


class SavePeakTests(unittest.TestCase):
    setUp = helpers.CloudStateTests.setUp
    write_model = helpers.CloudStateTests.write_model
    clone = helpers.CloudStateTests.clone

    def test_preflight_reuses_parent_blobs_without_writing_objects_or_state(self):
        cloud.load_state(self.repo)
        self.write_model(self.repo, 1)
        path = self.repo / "runtime/advanced_models/objects" / ("a" * 64 + ".json")
        path.parent.mkdir(parents=True)
        path.write_bytes(json.dumps({"samples": list(range(10000))}).encode())
        first_peak = cloud.preflight_state(self.repo)
        first = cloud.save_state(self.repo)
        before = helpers.git(self.repo, "count-objects", "-v")
        marker = (self.repo / ".git/football-cloud-state.json").read_bytes()
        second_peak = cloud.preflight_state(self.repo)
        self.assertEqual(helpers.git(self.repo, "count-objects", "-v"), before)
        self.assertEqual((self.repo / ".git/football-cloud-state.json").read_bytes(), marker)
        self.assertEqual(helpers.git(self.remote, "rev-parse", "model-state"), first)
        self.assertGreater(second_peak["reused_git_blob_count"], 0)
        self.assertLess(second_peak["new_git_object_bytes"], first_peak["new_git_object_bytes"])
        self.assertEqual(second_peak["required_free_bytes"],
                         second_peak["additional_bytes"] + cloud._DISK_RESERVE_BYTES)

    def test_second_save_builds_empty_temporary_tree_and_keeps_old_commit(self):
        cloud.load_state(self.repo)
        self.write_model(self.repo, 1)
        first = cloud.save_state(self.repo)
        self.write_model(self.repo, 2)
        original = state_storage.pack_runtime
        seen = []
        def observe(runtime, tree, **kwargs):
            seen.append(sorted(p.name for p in Path(tree).iterdir()))
            return original(runtime, tree, **kwargs)
        with patch.object(state_storage, "pack_runtime", side_effect=observe):
            second = cloud.save_state(self.repo)
        self.assertEqual(seen, [[".git"]])
        self.assertEqual(helpers.git(self.remote, "rev-parse", "model-state^"), first)
        self.assertEqual(json.loads(helpers.git(self.remote, "show", first + ":runtime/models/epl.json"))["version"], 1)
        restored = self.clone("peak-restored")
        self.assertEqual(cloud.load_state(restored), second)
        self.assertEqual((restored / "runtime/models/epl.json").read_bytes(),
                         (self.repo / "runtime/models/epl.json").read_bytes())

    def test_failed_preflight_reports_required_space_without_authorizing_push(self):
        cloud.load_state(self.repo)
        self.write_model(self.repo, 1)
        first = cloud.save_state(self.repo)
        marker = (self.repo / ".git/football-cloud-state.json").read_bytes()
        with patch.object(cloud.shutil, "disk_usage", return_value=SimpleNamespace(free=1)):
            report = cloud.preflight_state(self.repo)
            self.assertFalse(report["can_save"])
            self.assertGreater(report["required_free_bytes"], report["available_disk_bytes"])
            with self.assertRaisesRegex(cloud.StateError, "磁盘"):
                cloud.save_state(self.repo)
        self.assertEqual(helpers.git(self.remote, "rev-parse", "model-state"), first)
        self.assertEqual((self.repo / ".git/football-cloud-state.json").read_bytes(), marker)


class CapacityRefreshTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.runtime = Path(self.temp.name)

    def test_refresh_reclaims_duplicate_cost_and_invalidates_outstanding_tickets(self):
        guard = CapacityGuard(self.runtime)
        raw = b'{"same":"' + b"x" * 10000 + b'"}\n'
        for identity in ("a", "b"):
            path = self.runtime / "advanced_models/objects" / (identity * 64 + ".json")
            ticket = guard.check_write(path, raw)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
            guard.commit_write(ticket)
        before = guard.physical
        ticket = guard.check_write("summary.json", b"{}")
        writes = guard.writes
        result = guard.refresh()
        self.assertLess(guard.physical, before)
        self.assertEqual(guard.physical, state_storage.estimate_runtime(self.runtime)["persisted_bytes"])
        self.assertEqual(guard.writes, writes)
        self.assertEqual(result["exact_refreshes"], 2)
        (self.runtime / "summary.json").write_bytes(b"{}")
        with self.assertRaisesRegex(ValueError, "stale"):
            guard.commit_write(ticket)

    def test_refresh_does_not_relax_true_logical_limit(self):
        guard = CapacityGuard(self.runtime, max_logical=100)
        path = self.runtime / "advanced_models/objects" / ("a" * 64 + ".json")
        path.parent.mkdir(parents=True)
        path.write_bytes(b'{"value":"' + b"a" * 100 + b'"}')
        with self.assertRaises(CapacityDeferred) as raised:
            guard.refresh()
        self.assertEqual(raised.exception.details["reason"], "measured_runtime_capacity_exceeded")
        self.assertEqual(guard.max_logical, 100)


if __name__ == "__main__":
    unittest.main()
