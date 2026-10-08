import gzip
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from football_model import state_storage as storage


class StateStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.runtime = self.root / "runtime"
        self.runtime.mkdir()
        self.tree = self.root / "transport"
        self.tree.mkdir()

    def write(self, name, content=b'{"items":[]}\n'):
        path = self.runtime / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def pack(self):
        return storage.pack_runtime(self.runtime, self.tree)

    def manifest(self):
        return json.loads((self.tree / storage.MANIFEST_NAME).read_text())

    def mutate(self, callback):
        manifest = self.manifest()
        callback(manifest)
        (self.tree / storage.MANIFEST_NAME).write_text(json.dumps(manifest))

    def test_exact_bytes_roundtrip_and_dedup(self):
        content = b'{  "padding" : "' + b"x" * 12000 + b'" }\n'
        self.write("quotes/raw/1/old.json", content)
        self.write("quotes/snapshots/1/old.json", content)
        self.write("validation/epl/old.json", content)
        self.write("shadow_models/sources/" + "a" * 64 + ".json", content)
        self.write("shadow_models/reports/latest.json", b'{"report_id":"observed"}')
        original = {p.relative_to(self.runtime).as_posix(): p.read_bytes() for p in self.runtime.rglob("*") if p.is_file()}
        estimate = storage.estimate_runtime(self.runtime)
        stats = self.pack()
        self.assertEqual(stats["archived_file_count"], 4)
        self.assertEqual(stats["archive_blob_count"], 1)
        actual = storage.validate_storage_tree(self.tree)
        self.assertGreaterEqual(estimate["persisted_bytes"], actual["physical_bytes"])
        restored = self.root / "restored"
        storage.restore_runtime(self.tree, restored)
        self.assertEqual(original, {p.relative_to(restored).as_posix(): p.read_bytes() for p in restored.rglob("*") if p.is_file()})
        self.assertEqual(original, {p.relative_to(self.runtime).as_posix(): p.read_bytes() for p in self.runtime.rglob("*") if p.is_file()})

    def test_total_shrink_cold_evidence_restores_exact_original_bytes(self):
        content = b'{  "freeze" : "' + b"x" * 12000 + b'" }\n'
        raw = self.write("total_shrink_shadow/forecasts/" + "a" * 64 + ".json", content)
        run = self.write("total_shrink_shadow/runs/" + "b" * 64 + ".json", content)
        report = self.write("total_shrink_shadow/reports/latest.json", b'{"status":"observed"}\n')
        ordinary = self.write("total_shrink_shadow/local-unaddressed.json", b'{"keep":"inline"}')
        estimate = storage.estimate_runtime(self.runtime)
        stats = self.pack()
        self.assertEqual(stats["archived_file_count"], 2)
        self.assertFalse((self.tree / "runtime" / raw.relative_to(self.runtime)).exists())
        for path in (report, ordinary):
            self.assertEqual((self.tree / "runtime" / path.relative_to(self.runtime)).read_bytes(), path.read_bytes())
        actual = storage.validate_storage_tree(self.tree)
        self.assertGreaterEqual(estimate["persisted_bytes"], actual["physical_bytes"])
        restored = self.root / "restored"
        storage.restore_runtime(self.tree, restored)
        for path in (raw, run, report, ordinary):
            self.assertEqual((restored / path.relative_to(self.runtime)).read_bytes(), path.read_bytes())
        self.assertEqual(storage.read_archive_bytes(self.tree, raw.relative_to(self.runtime).as_posix()), content)

    def test_total_shrink_reports_even_digest_named_stay_inline(self):
        name = "total_shrink_shadow/reports/" + "a" * 64 + ".json"
        self.assertFalse(storage.should_archive(name))
        self.assertFalse(storage.should_archive("total_shrink_shadow/runs/not-a-digest.json"))
        self.assertTrue(storage.should_archive("total_shrink_shadow/runs/" + "a" * 64 + ".json"))
        self.assertFalse(storage.should_archive("total_shrink_shadow/runs/" + "a" * 64 + ".json",
                                               retained_inline={"total_shrink_shadow/runs/" + "a" * 64 + ".json"}))

    def test_latest_quote_and_access_validation_closure_stays_inline(self):
        self.write("quotes/raw/2/current.json")
        self.write("quotes/raw/2/old.json")
        self.write("quotes/snapshots/2/current.json", b'{"raw_source":"quotes/raw/2/current.json"}')
        self.write("quotes/reports/latest.json", b'{"snapshot_path":"quotes/snapshots/2/current.json"}')
        for run in ("attempt", "train", "success", "model", "coverage_old", "cold"):
            self.write(f"validation/epl/{run}.json")
        self.write("models/epl.json", b'{"model_id":"epl-model"}')
        self.write("reports/history/attempt.json", b'{"coverage_audit":{"path":"coverage/recent.json"}}')
        self.write("coverage/recent.json", b'{"old_source":{"path":"validation/epl/coverage_old.json"}}')
        index = {"competitions": [{"key":"epl", "latest_attempt":{"run_id":"attempt", "report_file":"runtime/reports/history/attempt.json"},
                  "latest_train_attempt":{"run_id":"train"}, "latest_successful_forecast":{"run_id":"success"}}]}
        self.write("integration/index.json", json.dumps(index).encode())
        retained = storage.retained_inline_paths(self.runtime)
        for run in ("attempt", "train", "success", "model", "coverage_old"):
            self.assertIn(f"validation/epl/{run}.json", retained)
        self.assertIn("quotes/raw/2/current.json", retained)
        self.assertNotIn("validation/epl/cold.json", retained)
        self.pack()
        self.assertTrue((self.tree / "runtime/quotes/raw/2/current.json").is_file())
        self.assertFalse((self.tree / "runtime/quotes/raw/2/old.json").exists())
        self.assertFalse((self.tree / "runtime/validation/epl/cold.json").exists())

    def test_long_nonpath_json_values_do_not_block_closure(self):
        self.write("models/epl.json", json.dumps({"notes":"x" * 10000, "model_id":"epl-current"}).encode())
        self.write("validation/epl/current.json", json.dumps({"observations":"x" * 10000}).encode())
        self.assertIn("validation/epl/current.json", storage.retained_inline_paths(self.runtime))
        self.pack()
        self.assertTrue((self.tree / "runtime/validation/epl/current.json").is_file())

    def test_legacy_plain_tree_is_restorable(self):
        (self.tree / "runtime/models").mkdir(parents=True)
        (self.tree / "runtime/models/epl.json").write_bytes(b'{"model": "legacy"}\n')
        storage.restore_runtime(self.tree, self.root / "restored")
        self.assertEqual((self.root / "restored/models/epl.json").read_bytes(), b'{"model": "legacy"}\n')

    def test_physical_and_logical_limits_are_independent_and_finite(self):
        self.assertEqual(storage.MAX_PHYSICAL_BYTES, 500 * 1024 * 1024)
        self.assertEqual(storage.MAX_LOGICAL_BYTES, 2 * 1024 * 1024 * 1024)
        self.write("quotes/raw/1/old.json", b'{"pad":"' + b"x" * 20000 + b'"}')
        self.pack()
        stats = storage.validate_storage_tree(self.tree, max_physical_bytes=2000, max_logical_bytes=30000)
        self.assertLess(stats["physical_bytes"], 2000)
        self.assertGreater(stats["logical_bytes"], 2000)
        with self.assertRaisesRegex(storage.StorageError, "物理"):
            storage.validate_storage_tree(self.tree, max_physical_bytes=100)
        with self.assertRaisesRegex(storage.StorageError, "逻辑"):
            storage.validate_storage_tree(self.tree, max_logical_bytes=1000)

    def test_traversal_absolute_hidden_and_backslash_paths_rejected_before_write(self):
        for name in ("../outside.json", "/absolute.json", "quotes/raw/../bad.json", "quotes/raw/.hidden.json", "quotes\\raw\\bad.json"):
            with self.subTest(name=name):
                self.write("quotes/raw/1/old.json")
                if not (self.tree / storage.MANIFEST_NAME).exists():
                    self.pack()
                self.mutate(lambda m: m["entries"][0].update(path=name))
                with self.assertRaises(storage.StorageError):
                    storage.restore_runtime(self.tree, self.root / "restore")
                self.assertFalse((self.root / "restore").exists())

    def test_duplicate_and_inline_collision_rejected_before_write(self):
        self.write("quotes/raw/1/old.json")
        self.pack()
        original = self.manifest()
        self.mutate(lambda m: m["entries"].append(dict(m["entries"][0])))
        with self.assertRaisesRegex(storage.StorageError, "重复"):
            storage.restore_runtime(self.tree, self.root / "restore")
        (self.tree / storage.MANIFEST_NAME).write_text(json.dumps(original))
        inline = self.tree / "runtime/quotes/raw/1/old.json"
        inline.parent.mkdir(parents=True)
        inline.write_text("{}")
        with self.assertRaisesRegex(storage.StorageError, "覆盖"):
            storage.restore_runtime(self.tree, self.root / "restore")
        self.assertFalse((self.root / "restore").exists())

    def test_compressed_hash_length_and_raw_hash_length_checked(self):
        self.write("quotes/raw/1/old.json")
        self.pack()
        original = self.manifest()
        mutations = ({"compressed_bytes": 1}, {"raw_bytes": 1}, {"raw_sha256":"0" * 64}, {"blob_sha256":"0" * 64})
        for update in mutations:
            with self.subTest(update=update):
                (self.tree / storage.MANIFEST_NAME).write_text(json.dumps(original))
                self.mutate(lambda m: m["entries"][0].update(update))
                with self.assertRaises(storage.StorageError):
                    storage.validate_storage_tree(self.tree)

    def test_corrupt_trailing_gzip_and_zip_bomb_rejected(self):
        self.write("quotes/raw/1/old.json")
        self.pack()
        original = self.manifest()
        for blob in (b"not gzip", gzip.compress(b"{}").strip() + b"extra", gzip.compress(b"x" * 1000000)):
            with self.subTest(size=len(blob)):
                for path in (self.tree / storage.ARCHIVE_DIR).iterdir():
                    path.unlink()
                digest = hashlib.sha256(blob).hexdigest()
                (self.tree / storage.ARCHIVE_DIR / (digest + ".json.gz")).write_bytes(blob)
                manifest = json.loads(json.dumps(original))
                manifest["entries"][0].update(blob_sha256=digest, compressed_bytes=len(blob), raw_bytes=100)
                (self.tree / storage.MANIFEST_NAME).write_text(json.dumps(manifest))
                with self.assertRaises(storage.StorageError):
                    storage.validate_storage_tree(self.tree)

    def test_secret_scanned_before_compression_and_after_decompression(self):
        secret = "synthetic-storage-secret-123"
        self.write("quotes/raw/1/old.json", json.dumps({"secret":secret}).encode())
        with patch.dict(os.environ, {"FOOTBALL_API_KEY":secret}):
            with self.assertRaisesRegex(storage.StorageError, "凭据"):
                self.pack()
        self.pack()
        with patch.dict(os.environ, {"FOOTBALL_API_KEY":secret}):
            with self.assertRaisesRegex(storage.StorageError, "凭据"):
                storage.restore_runtime(self.tree, self.root / "restored")
        self.assertFalse((self.root / "restored").exists())

    def test_symlinks_and_unmanifested_objects_rejected(self):
        self.write("quotes/raw/1/old.json")
        self.pack()
        (self.tree / storage.ARCHIVE_DIR / ("0" * 64 + ".json.gz")).write_bytes(gzip.compress(b"{}"))
        with self.assertRaisesRegex(storage.StorageError, "未登记"):
            storage.validate_storage_tree(self.tree)
        (self.tree / storage.ARCHIVE_DIR / ("0" * 64 + ".json.gz")).unlink()
        (self.tree / "runtime/link.json").symlink_to(self.runtime / "quotes/raw/1/old.json")
        with self.assertRaisesRegex(storage.StorageError, "符号链接"):
            storage.validate_storage_tree(self.tree)

    def test_selective_reader_needs_only_target_real_blob(self):
        first = b'{ "history": 1 }\n'
        self.write("quotes/raw/1/first.json", first)
        self.write("quotes/raw/1/second.json", b'{"history":2}')
        self.pack()
        selected = self.root / "selected"
        selected.mkdir()
        (selected / storage.ARCHIVE_DIR).mkdir()
        (selected / storage.MANIFEST_NAME).write_bytes((self.tree / storage.MANIFEST_NAME).read_bytes())
        entry = next(e for e in self.manifest()["entries"] if e["path"].endswith("first.json"))
        blob_name = entry["blob_sha256"] + ".json.gz"
        (selected / storage.ARCHIVE_DIR / blob_name).write_bytes((self.tree / storage.ARCHIVE_DIR / blob_name).read_bytes())
        self.assertEqual(storage.read_archive_bytes(selected, "quotes/raw/1/first.json"), first)
        with self.assertRaises(storage.StorageError):
            storage.read_archive_bytes(selected, "quotes/raw/1/second.json")
        with self.assertRaises(storage.StorageError):
            storage.read_archive_bytes(selected, "../outside.json")

    def test_replay_expansion_included_in_logical_estimate(self):
        content = b'{"padding":"' + b"x" * 1000 + b'"}'
        self.write("replay/bundles/" + "a" * 64 + ".json.gz", gzip.compress(content))
        estimate = storage.estimate_runtime(self.runtime)
        self.assertEqual(estimate["logical_bytes"], estimate["raw_bytes"] + len(content))
        with self.assertRaises(storage.StorageError):
            storage.validate_runtime(self.runtime, max_logical_bytes=500)

    def test_manifest_duplicate_keys_invalid_extension_and_no_manifest_rejected(self):
        self.write("quotes/raw/1/old.json")
        self.pack()
        manifest = self.tree / storage.MANIFEST_NAME
        original = self.manifest()
        self.mutate(lambda m: m["entries"][0].update(path="quotes/raw/1/old.txt"))
        with self.assertRaises(storage.StorageError):
            storage.validate_storage_tree(self.tree)
        manifest.write_text(json.dumps(original))
        self.mutate(lambda m: m.update(schema_version=True))
        with self.assertRaises(storage.StorageError):
            storage.validate_storage_tree(self.tree)
        manifest.write_text('{"schema_version":1,"schema_version":1}')
        with self.assertRaises(storage.StorageError):
            storage.validate_storage_tree(self.tree)
        manifest.unlink()
        with self.assertRaisesRegex(storage.StorageError, "缺少清单"):
            storage.validate_storage_tree(self.tree)


if __name__ == "__main__":
    unittest.main()
