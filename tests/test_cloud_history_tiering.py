"""Fresh shallow Actions-style round trips retain pinned cold originals."""
import hashlib
import json
from unittest import TestCase
from unittest.mock import patch
from types import SimpleNamespace

from tests import test_cloud_state as helpers
from football_model import history_archive as history
from football_model.state_storage import estimate_runtime

cloud = helpers.cloud_state


class CloudHistoryTieringTests(TestCase):
    setUp = helpers.CloudStateTests.setUp
    write_model = helpers.CloudStateTests.write_model

    def original(self, value):
        raw = json.dumps(value, separators=(",", ":")).encode()
        name = "advanced_models/objects/" + hashlib.sha256(raw).hexdigest() + ".json"
        path = self.repo / "runtime" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        return name, raw

    def test_shallow_restore_and_next_save_keep_all_fixed_originals_without_expanding(self):
        cloud.load_state(self.repo)
        self.write_model(self.repo, 1)
        note = self.repo / "runtime/history_archive/operator_note.json"
        note.parent.mkdir(parents=True)
        note.write_bytes(b'{"preserve_unknown_metadata":true}')
        first_name, first_raw = self.original({"samples": list(range(2000))})
        first = cloud.save_state(self.repo)
        self.assertTrue((self.repo / "runtime" / first_name).is_file())
        second_name, second_raw = self.original({"samples": list(range(3000))})
        prepared = cloud.prepare_state(self.repo)
        self.assertEqual(prepared["compacted_file_count"], 1)
        self.assertFalse((self.repo / "runtime" / first_name).exists())
        self.assertTrue((self.repo / "runtime" / second_name).is_file())
        self.assertEqual(history.read_bytes(self.repo / "runtime" / first_name), first_raw)
        second = cloud.save_state(self.repo)
        self.assertEqual(helpers.git(self.remote, "rev-parse", "model-state^"), first)

        restored = self.root / "shallow-history"
        helpers.git(self.root, "clone", "--depth=1", "--single-branch", "--branch=main",
                    self.remote.as_uri(), str(restored))
        self.assertEqual(cloud.load_state(restored), second)
        self.assertEqual((restored / "runtime/history_archive/operator_note.json").read_bytes(),
                         b'{"preserve_unknown_metadata":true}')
        for name, raw in ((first_name, first_raw), (second_name, second_raw)):
            path = restored / "runtime" / name
            self.assertFalse(path.exists())
            self.assertTrue(history.exists(path))
            self.assertEqual(history.read_bytes(path), raw)
            self.assertEqual(history.read_state_bytes(restored, second, name), raw)
        self.assertEqual(history.read_state_bytes(restored, first, first_name), first_raw)
        measured = estimate_runtime(restored / "runtime")
        self.assertEqual(measured["historical_raw_bytes"], len(first_raw) + len(second_raw))
        self.assertEqual(measured["historical_file_count"], 2)
        self.assertGreater(measured["total_original_logical_bytes"], measured["active_logical_bytes"])
        self.assertLess(measured["active_logical_bytes"], len(first_raw) + len(second_raw))
        third = cloud.save_state(restored)
        self.assertEqual(helpers.git(self.remote, "rev-parse", "model-state^"), second)
        self.assertEqual(history.read_state_bytes(restored, third, first_name), first_raw)
        self.assertEqual(history.read_state_bytes(restored, third, second_name), second_raw)

    def test_compaction_never_evicts_uncommitted_or_changed_bytes(self):
        cloud.load_state(self.repo)
        name, raw = self.original({"saved": True})
        first = cloud.save_state(self.repo)
        path = self.repo / "runtime" / name
        changed = b'{"saved":false}'
        path.write_bytes(changed)
        unsaved_name, unsaved_raw = self.original({"new": True})
        prepared = cloud.prepare_state(self.repo)
        self.assertEqual(prepared["compacted_file_count"], 0)
        self.assertEqual(path.read_bytes(), changed)
        self.assertEqual((self.repo / "runtime" / unsaved_name).read_bytes(), unsaved_raw)
        self.assertEqual(history.read_state_bytes(self.repo, first, name), raw)

    def test_load_reserves_active_restore_instead_of_full_cold_expansion(self):
        cloud.load_state(self.repo)
        name, raw = self.original({"padding": "x" * (16 * 1024 * 1024)})
        first = cloud.save_state(self.repo)
        restored = self.root / "bounded-restore"
        helpers.git(self.root, "clone", "--branch=main", str(self.remote), str(restored))
        # Initial fetch/checkout guard has room; after that only ten MiB plus
        # the safety reserve remain. The catalog fits, a 16 MiB expansion does
        # not. A correct cold restore must not demand the omitted raw bytes.
        with patch.object(cloud.shutil, "disk_usage", side_effect=[
                SimpleNamespace(free=4 * 1024**3),
                SimpleNamespace(free=cloud._DISK_RESERVE_BYTES + 10 * 1024**2)]):
            self.assertEqual(cloud.load_state(restored), first)
        self.assertFalse((restored / "runtime" / name).exists())
        self.assertEqual(history.read_bytes(restored / "runtime" / name), raw)
