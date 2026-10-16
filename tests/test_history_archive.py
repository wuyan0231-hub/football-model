"""Cold evidence stays byte-exact, durable and visible without full expansion."""
import copy
import gzip
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
from unittest.mock import patch
import unittest

from football_model import history_archive as history
from football_model.state_storage import pack_runtime, StorageError
from football_model.advanced_storage import load_artifact


class HistoryArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.repo = self.base / "repo"
        self.repo.mkdir()
        self.git("init", "-q")
        self.git("config", "user.email", "test@example.invalid")
        self.git("config", "user.name", "Test")
        self.source = self.base / "source"
        self.name = "advanced_models/inputs/" + "a" * 64 + ".json"
        self.raw = '{"原始": [1, 2, 3], "failure": null}\n'.encode()
        target = self.source / self.name
        target.parent.mkdir(parents=True)
        target.write_bytes(self.raw)
        (self.source / "advanced_models/queue").mkdir(parents=True)
        (self.source / "advanced_models/queue/latest.json").write_text('{"status":"failed"}\n')
        pack_runtime(self.source, self.repo)
        self.git("add", ".")
        self.git("commit", "-qm", "original state")
        self.oid = self.git("rev-parse", "HEAD").decode().strip()
        self.manifest = json.loads((self.repo / "state_storage.json").read_bytes())
        self.active = self.repo / "active"
        self.active.mkdir()

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.repo), *args], check=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout

    def catalog(self):
        return history.restore_history_catalog(self.active, self.repo, self.oid,
                                               self.repo, self.manifest["entries"])

    def test_cold_read_matches_original_and_never_materializes(self):
        self.assertEqual(self.catalog(), {self.name})
        path = self.active / self.name
        self.assertFalse(path.exists())
        self.assertTrue(history.exists(path))
        self.assertEqual(history.read_bytes(path), self.raw)
        self.assertFalse(path.exists())
        self.assertEqual(history.read_state_bytes(self.repo, self.oid, self.name), self.raw)

    def test_catalog_stats_and_enumeration_keep_failure_pointer_visible(self):
        self.catalog()
        hot = self.active / "advanced_models/queue/latest.json"
        hot.parent.mkdir(parents=True)
        hot.write_bytes(b'{"status":"failed"}')
        names = {p.relative_to(self.active).as_posix() for p in history.iter_paths(self.active)}
        self.assertIn(self.name, names)
        self.assertIn("advanced_models/queue/latest.json", names)
        self.assertEqual(history.glob_paths(self.active / "advanced_models/inputs", "*.json"),
                         [self.active / self.name])
        self.assertEqual(history.glob_paths(self.active, "advanced_models/*/*.json"),
                         sorted([self.active / self.name, hot]))
        stats = history.catalog_stats(self.active)
        self.assertEqual(stats["historical_raw_bytes"], len(self.raw))
        self.assertEqual(stats["historical_file_count"], 1)

    def test_compaction_only_evicts_previously_saved_same_bytes(self):
        old = self.active / self.name
        old.parent.mkdir(parents=True)
        old.write_bytes(self.raw)
        new = old.with_name("b" * 64 + ".json")
        new.write_bytes(b'{"new":"unsaved"}\n')
        report = history.compact_existing_history(self.active, self.oid, self.repo)
        self.assertEqual(report["compacted_file_count"], 1)
        self.assertFalse(old.exists())
        self.assertEqual(history.read_bytes(old), self.raw)
        self.assertTrue(new.is_file())

    def test_saved_path_with_changed_bytes_remains_hot(self):
        old = self.active / self.name
        old.parent.mkdir(parents=True)
        old.write_bytes(b'{"changed":"not saved"}\n')
        self.assertEqual(history.compact_existing_history(self.active, self.oid, self.repo)["compacted_file_count"], 0)
        self.assertTrue(old.is_file())

    def test_existing_catalog_and_different_hot_original_rejects(self):
        self.catalog()
        path = self.active / self.name
        path.parent.mkdir(parents=True)
        path.write_bytes(b"{}")
        with self.assertRaises(StorageError):
            history.read_bytes(path)

    def test_bad_manifest_binding_is_rejected_without_materializing(self):
        self.catalog()
        value = json.loads((self.active / history.CATALOG_RELATIVE).read_bytes())
        value["entries"][0]["manifest_sha256"] = "b" * 64
        (self.active / history.CATALOG_RELATIVE).write_text(json.dumps(value))
        with self.assertRaises(StorageError):
            history.read_bytes(self.active / self.name)
        self.assertFalse((self.active / self.name).exists())

    def test_raw_hash_is_checked_after_valid_compressed_blob(self):
        entry = self.manifest["entries"][0]
        entry["raw_sha256"] = "b" * 64
        (self.repo / "state_storage.json").write_text(json.dumps(self.manifest))
        self.git("add", "state_storage.json")
        self.git("commit", "-qm", "bad original hash")
        commit = self.git("rev-parse", "HEAD").decode().strip()
        with self.assertRaises(StorageError):
            history.read_state_bytes(self.repo, commit, self.name)

    def test_compressed_hash_is_checked(self):
        entry = self.manifest["entries"][0]
        blob = self.repo / "state_archives" / (entry["blob_sha256"] + ".json.gz")
        blob.write_bytes(b"x" * entry["compressed_bytes"])
        self.git("add", "state_archives")
        self.git("commit", "-qm", "bad compressed bytes")
        commit = self.git("rev-parse", "HEAD").decode().strip()
        with self.assertRaises(StorageError):
            history.read_state_bytes(self.repo, commit, self.name)

    def test_catalog_path_traversal_and_duplicate_are_rejected(self):
        self.catalog()
        path = self.active / history.CATALOG_RELATIVE
        value = json.loads(path.read_bytes())
        for bad in ("../escape.json", "/escape.json", "a/../../escape.json"):
            broken = copy.deepcopy(value)
            broken["entries"][0]["path"] = bad
            path.write_text(json.dumps(broken))
            with self.assertRaises(StorageError):
                history.catalog_stats(self.active)
        value["entries"].append(copy.deepcopy(value["entries"][0]))
        path.write_text(json.dumps(value))
        with self.assertRaises(StorageError):
            history.catalog_stats(self.active)

    def test_fixed_saved_catalog_can_resolve_an_ancestor_original(self):
        self.catalog()
        target = self.repo / "runtime" / history.CATALOG_RELATIVE
        target.parent.mkdir(parents=True)
        target.write_bytes((self.active / history.CATALOG_RELATIVE).read_bytes())
        # The new state no longer contains the old large compressed blob.
        shutil.rmtree(self.repo / "state_archives")
        (self.repo / "state_storage.json").write_text(json.dumps({**self.manifest, "entries": []}))
        self.git("add", "runtime", "state_storage.json", "state_archives")
        self.git("commit", "-qm", "cold index references original parent")
        current = self.git("rev-parse", "HEAD").decode().strip()
        self.assertEqual(history.read_state_bytes(self.repo, current, self.name), self.raw)

    def test_compact_chunk_loader_reads_cold_dependencies(self):
        self.catalog()
        ref = {"path": self.name, "sha256": hashlib.sha256(self.raw).hexdigest()}
        self.assertEqual(load_artifact(self.active, ref), json.loads(self.raw))

    def test_cold_selector_keeps_all_mutable_or_non_content_addressed_paths_hot(self):
        for name in ("advanced_models/epl/latest.json", "advanced_models/epl/queue/first.json",
                     "advanced_models/checkpoints/latest.json", "advanced_models/objects/old.json",
                     "advanced_snapshots/sources/" + "a" * 64 + ".json"):
            self.assertFalse(history.select_cold_path(name))

    def test_symlink_path_rejected_even_when_catalog_has_original(self):
        self.catalog()
        path = self.active / "linked"
        path.symlink_to(self.active / "advanced_models", target_is_directory=True)
        with self.assertRaises(StorageError):
            history.read_bytes(path / "inputs" / ("a" * 64 + ".json"))

    def test_sharded_catalog_detects_modified_chunk_and_reuses_unchanged_chunks(self):
        self.catalog()
        entries = dict(history._catalog(self.active))
        original = next(iter(entries.values()))
        for n in range(30):
            entry = {**original, "path": "advanced_models/inputs/" + f"{n:064x}" + ".json"}
            entries[entry["path"]] = entry
        with patch.object(history, "CATALOG_INLINE_BYTES", 256), patch.object(history, "CATALOG_CHUNK_BYTES", 4096):
            history._write_catalog(self.active, entries)
            index = json.loads((self.active / history.CATALOG_RELATIVE).read_bytes())
            self.assertEqual(index["schema_version"], history.INDEX_SCHEMA_VERSION)
            self.assertGreater(len(index["chunks"]), 1)
            self.assertEqual(len(history._catalog(self.active)), len(entries))
            old_chunks = {ref["path"] for ref in index["chunks"]}
            new = {**original, "path": "advanced_models/inputs/" + "f" * 64 + ".json"}
            entries[new["path"]] = new
            history._write_catalog(self.active, entries)
            updated = json.loads((self.active / history.CATALOG_RELATIVE).read_bytes())
            self.assertTrue(old_chunks <= {ref["path"] for ref in updated["chunks"]})
            child = self.active / updated["chunks"][0]["path"]
            child.write_bytes(child.read_bytes().replace(b"gzip-single-member", b"gzip-broken-member"))
            with self.assertRaises(StorageError):
                history.catalog_stats(self.active)

    def test_fixed_reader_reuses_decoded_saved_catalog(self):
        self.catalog()
        target = self.repo / "runtime" / history.CATALOG_RELATIVE
        target.parent.mkdir(parents=True)
        target.write_bytes((self.active / history.CATALOG_RELATIVE).read_bytes())
        shutil.rmtree(self.repo / "state_archives")
        (self.repo / "state_storage.json").write_text(json.dumps({**self.manifest, "entries": []}))
        self.git("add", "runtime", "state_storage.json", "state_archives")
        self.git("commit", "-qm", "cold index")
        current = self.git("rev-parse", "HEAD").decode().strip()
        with patch.object(history, "_decode_catalog", wraps=history._decode_catalog) as decoded:
            with history.FixedStateReader(self.repo, current) as reader:
                self.assertEqual(reader.read(self.name), self.raw)
                self.assertEqual(reader.read(self.name), self.raw)
            self.assertEqual(decoded.call_count, 1)


if __name__ == "__main__":
    unittest.main()
