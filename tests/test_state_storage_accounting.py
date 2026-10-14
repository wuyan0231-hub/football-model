"""Transport measurement and conservative writes preserve the real size gates."""
import gzip
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from football_model import state_storage as storage
from football_model import shadow_models as shadow
from football_model.advanced_capacity import CapacityGuard, CapacityDeferred


class ExactTransportAccountingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.runtime = self.base / "runtime"
        self.runtime.mkdir()

    def write(self, name, raw):
        path = self.runtime / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        return path

    def pack(self, name):
        tree = self.base / name
        tree.mkdir()
        storage.pack_runtime(self.runtime, tree)
        return tree

    def test_complete_measurement_matches_transport_bytes_and_exact_roundtrip(self):
        shared = b'{  "padding": "' + b"x" * 12000 + b'" }\n'
        different = b'{"different":[1,2,3],"padding":"' + b"y" * 9000 + b'"}\n'
        self.write("quotes/raw/1/共享.json", shared)
        self.write("quotes/fixtures/2/copy.json", shared)
        self.write("quotes/raw/3/other.json", different)
        self.write("models/epl.json", b'{"model_id":"epl-observed"}\n')
        # Current evidence must stay inline even if its content matches a blob.
        self.write("quotes/raw/4/current.json", shared)
        self.write("quotes/reports/latest.json", b'{"source":"quotes/raw/4/current.json"}\n')
        replay_raw = b'{"payload":"' + b"z" * 500 + b'"}'
        self.write("replay/bundles/" + "a" * 64 + ".json.gz", gzip.compress(replay_raw, mtime=0))
        before = {p.relative_to(self.runtime).as_posix(): p.read_bytes()
                  for p in self.runtime.rglob("*") if p.is_file()}
        estimate = storage.estimate_runtime(self.runtime)
        tree = self.pack("transport")
        actual = storage.validate_storage_tree(tree)
        self.assertEqual(estimate["persisted_bytes"] - storage.METADATA_RESERVE_BYTES,
                         actual["physical_bytes"])
        self.assertEqual(estimate["manifest_bytes"], (tree / storage.MANIFEST_NAME).stat().st_size)
        self.assertEqual(estimate["archived_file_count"], 3)
        self.assertEqual(estimate["archive_blob_count"], 2)
        self.assertEqual(estimate["unique_archive_bytes"],
                         sum(p.stat().st_size for p in (tree / storage.ARCHIVE_DIR).iterdir()))
        self.assertEqual(estimate["logical_bytes"], actual["logical_bytes"])
        (tree / "README.md").write_bytes(b"recorded transport\n")
        (tree / "state_metadata.json").write_bytes(b'{"observed":true}\n')
        self.assertGreaterEqual(estimate["persisted_bytes"], storage.validate_storage_tree(tree)["physical_bytes"])
        restored = self.base / "restored"
        storage.restore_runtime(tree, restored)
        self.assertEqual(before, {p.relative_to(restored).as_posix(): p.read_bytes()
                                  for p in restored.rglob("*") if p.is_file()})
        self.assertEqual(before, {p.relative_to(self.runtime).as_posix(): p.read_bytes()
                                  for p in self.runtime.rglob("*") if p.is_file()})

    def test_empty_runtime_accounts_for_the_real_manifest(self):
        estimate = storage.estimate_runtime(self.runtime)
        tree = self.pack("empty")
        actual = storage.validate_storage_tree(tree)
        self.assertEqual(estimate["persisted_bytes"], actual["physical_bytes"] + storage.METADATA_RESERVE_BYTES)
        self.assertEqual(estimate["archive_blob_count"], 0)

    def test_shared_archive_replacement_does_not_credit_old_blob(self):
        old = b'{"data":"' + b"a" * 8000 + b'"}\n'
        new = b'{"data":"' + b"b" * 8000 + b'"}\n'
        name = "quotes/raw/1/old.json"
        path = self.write(name, old)
        self.write("quotes/raw/2/shared.json", old)
        baseline = storage.estimate_runtime(self.runtime)
        delta = storage.storage_write_delta(name, new, old)
        self.assertEqual(delta, storage.storage_cost(name, new))
        path.write_bytes(new)
        updated = storage.estimate_runtime(self.runtime)
        self.assertEqual(updated["archive_blob_count"], 2)
        self.assertGreater(updated["persisted_bytes"], baseline["persisted_bytes"])
        self.assertGreaterEqual(baseline["persisted_bytes"] + delta, updated["persisted_bytes"])

    def test_single_archive_and_inline_replacements_keep_safe_growth(self):
        name = "quotes/raw/1/only.json"
        old, new = b'{"value":"old"}\n', b'{"value":"new"}\n'
        path = self.write(name, old)
        before = storage.estimate_runtime(self.runtime)["persisted_bytes"]
        delta = storage.storage_write_delta(name, new, old)
        path.write_bytes(new)
        self.assertGreaterEqual(before + delta, storage.estimate_runtime(self.runtime)["persisted_bytes"])
        self.assertEqual(storage.storage_write_delta(name, new, new), 0)
        self.assertEqual(storage.storage_write_delta("models/epl.json", b"12345", b"123"), 2)
        self.assertEqual(storage.storage_write_delta("models/epl.json", b"1", b"123"), 0)
        self.assertEqual(storage.storage_write_delta(name, b"12345", b"123", {name}), 2)

    def test_advanced_shared_replacement_rejected_before_any_write(self):
        old, new = b'{"same":"old"}\n', b'{"same":"new"}\n'
        name = "cache/api_cache/one.json"
        path = self.write(name, old)
        self.write("cache/api_cache/two.json", old)
        baseline = storage.estimate_runtime(self.runtime)["persisted_bytes"]
        delta = storage.storage_write_delta(name, new, old)
        guard = CapacityGuard(self.runtime, reserve_logical=0, reserve_physical=0,
                              max_physical=baseline + delta - 1)
        with self.assertRaises(CapacityDeferred):
            guard.check_write(path, new)
        self.assertEqual(path.read_bytes(), old)
        self.assertEqual(guard.physical, baseline)
        guard = CapacityGuard(self.runtime, reserve_logical=0, reserve_physical=0,
                              max_physical=baseline + delta)
        ticket = guard.check_write(path, new)
        path.write_bytes(new)
        guard.commit_write(ticket)
        self.assertGreaterEqual(guard.physical, storage.estimate_runtime(self.runtime)["persisted_bytes"])

    def test_shadow_shared_replacement_rejected_at_the_same_threshold(self):
        old, new = b'{"value":"old"}\n', b'{"value":"new"}\n'
        name = "shadow_models/sources/" + "a" * 64 + ".json"
        path = self.write(name, old)
        self.write("shadow_models/sources/" + "b" * 64 + ".json", old)
        baseline = storage.estimate_runtime(self.runtime)["persisted_bytes"]
        delta = storage.storage_write_delta(name, new, old)
        with patch.object(shadow, "STORAGE_LIMIT_BYTES", baseline + delta - 1):
            with shadow._writer(self.runtime):
                with self.assertRaises(shadow.ShadowCapacityError) as raised:
                    shadow._write_cost(path, new)
                self.assertEqual(raised.exception.budget["capacity_constraint"], "estimated_persisted_limit")
        self.assertEqual(path.read_bytes(), old)


if __name__ == "__main__":
    unittest.main()
