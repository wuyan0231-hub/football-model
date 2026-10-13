"""Actual isolated Git state round trips and fail-before-push resource guards."""
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tests import test_cloud_state as helpers
from football_model import storage_policy

cloud = helpers.cloud_state


class CloudStateResourceTests(unittest.TestCase):
    setUp = helpers.CloudStateTests.setUp
    write_model = helpers.CloudStateTests.write_model
    clone = helpers.CloudStateTests.clone

    def test_pack_probe_counts_actual_new_objects_and_excludes_only_old_state(self):
        cloud.load_state(self.repo)
        self.write_model(self.repo, 1)
        first = cloud.save_state(self.repo)
        complete = cloud._measure_push_pack(self.repo, first, None)
        empty = cloud._measure_push_pack(self.repo, first, first)
        self.assertGreater(complete, empty)
        self.assertEqual(empty, 32)  # PACK header and checksum, zero objects.
        self.write_model(self.repo, 2)
        second = cloud.save_state(self.repo)
        delta = cloud._measure_push_pack(self.repo, second, first)
        self.assertGreater(delta, empty)
        self.assertLess(delta, cloud._measure_push_pack(self.repo, second, None))
        self.assertLess(cloud._MAX_PUSH_PACK_BYTES, 2 * 1024**3)
        self.assertLess(storage_policy.MAX_PHYSICAL_BYTES, cloud._MAX_PUSH_PACK_BYTES)

    def test_pack_limit_refusal_preserves_remote_and_load_marker(self):
        cloud.load_state(self.repo)
        self.write_model(self.repo, 1)
        first = cloud.save_state(self.repo)
        self.write_model(self.repo, 2)
        with patch.object(cloud, "_MAX_PUSH_PACK_BYTES", 1):
            with self.assertRaisesRegex(cloud.StateError, "pack"):
                cloud.save_state(self.repo)
        self.assertEqual(helpers.git(self.remote, "rev-parse", "model-state"), first)
        marker = json.loads((self.repo / ".git/football-cloud-state.json").read_text())
        self.assertEqual(marker["state_oid"], first)

    def test_disk_or_unknown_memory_refuses_save_without_remote_mutation(self):
        cloud.load_state(self.repo)
        self.write_model(self.repo, 1)
        first = cloud.save_state(self.repo)
        self.write_model(self.repo, 2)
        with patch.object(cloud.shutil, "disk_usage", return_value=SimpleNamespace(free=0)):
            with self.assertRaisesRegex(cloud.StateError, "磁盘"):
                cloud.save_state(self.repo)
        with patch.object(cloud, "_available_memory_bytes", return_value=None):
            with self.assertRaisesRegex(cloud.StateError, "内存"):
                cloud.save_state(self.repo)
        with patch.object(cloud, "_available_memory_bytes", return_value=1024):
            with self.assertRaisesRegex(cloud.StateError, "内存"):
                cloud.save_state(self.repo)
        self.assertEqual(helpers.git(self.remote, "rev-parse", "model-state"), first)

    def test_restore_resource_failure_leaves_no_authorizing_marker(self):
        cloud.load_state(self.repo)
        self.write_model(self.repo, 1)
        cloud.save_state(self.repo)
        other = self.clone("resource-blocked")
        with patch.object(cloud.shutil, "disk_usage", return_value=SimpleNamespace(free=0)):
            with self.assertRaisesRegex(cloud.StateError, "磁盘"):
                cloud.load_state(other)
        self.assertFalse((other / ".git/football-cloud-state.json").exists())
        self.assertFalse((other / "runtime").exists())

    def test_shallow_source_and_state_keep_complete_current_evidence_and_old_remote_history(self):
        cloud.load_state(self.repo)
        self.write_model(self.repo, 1)
        first = cloud.save_state(self.repo)
        original = (self.repo / "runtime/models/epl.json").read_bytes()
        self.write_model(self.repo, 2)
        second = cloud.save_state(self.repo)
        other = self.root / "shallow-source"
        helpers.git(self.root, "clone", "--depth=1", "--branch=main", self.remote.as_uri(), str(other))
        self.assertEqual(cloud.load_state(other), second)
        self.write_model(other, 3)
        third = cloud.save_state(other)
        self.assertEqual(helpers.git(self.remote, "rev-parse", "model-state^"), second)
        self.assertEqual(helpers.git(self.remote, "show", first + ":runtime/models/epl.json").encode(), original)
        self.assertEqual(helpers.git(self.remote, "rev-parse", "model-state"), third)

    def test_f4_f5_budget_is_finite_and_retains_production_headroom(self):
        self.assertEqual(storage_policy.SHADOW_WRITE_LIMIT_BYTES, 1536 * 1024**2)
        self.assertEqual(storage_policy.MAX_PHYSICAL_BYTES - storage_policy.SHADOW_WRITE_LIMIT_BYTES,
                         256 * 1024**2)
        self.assertIn((4096 * 1024**2, 1024 * 1024**2), storage_policy.SUPPORTED_MANIFEST_LIMITS)
        self.assertIn((2048 * 1024**2, 500 * 1024**2), storage_policy.SUPPORTED_MANIFEST_LIMITS)


if __name__ == "__main__":
    unittest.main()
