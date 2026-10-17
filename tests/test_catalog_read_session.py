"""A catalog scope saves metadata I/O, never original integrity checks."""
from contextlib import nullcontext
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from football_model import history_archive as history
from football_model import advanced_snapshots as snapshots
from football_model.advanced_capacity import CapacityGuard, CapacityDeferred
from football_model.feature_snapshots import SnapshotError
from football_model.state_storage import StorageError, pack_runtime


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n").encode()


class CatalogReadSessionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.repo = self.base / "repo"
        self.repo.mkdir()
        self.git("init", "-q")
        self.git("config", "user.email", "test@example.invalid")
        self.git("config", "user.name", "Test")
        self.name = "advanced_models/inputs/" + "a" * 64 + ".json"
        self.raw = encoded({"original": [1, 2, 3], "at": "2025-01-01T00:00:00Z"})
        source = self.base / "source"
        path = source / self.name
        path.parent.mkdir(parents=True)
        path.write_bytes(self.raw)
        pack_runtime(source, self.repo)
        self.git("add", ".")
        self.git("commit", "-qm", "immutable original")
        self.oid = self.git("rev-parse", "HEAD").decode().strip()
        self.manifest = json.loads((self.repo / "state_storage.json").read_bytes())
        self.runtime = self.repo / "active"
        self.runtime.mkdir()
        history.restore_history_catalog(self.runtime, self.repo, self.oid,
                                        self.repo, self.manifest["entries"])
        self.addCleanup(history._CATALOG_CACHE.clear)
        self.addCleanup(history._MANIFEST_CACHE.clear)

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.repo), *args], check=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout

    def chunks(self, count=3):
        original = dict(next(iter(history._catalog(self.runtime).values())))
        refs = []
        for index in range(count):
            entry = {**original, "path": self.name if not index else
                     "advanced_models/inputs/" + f"{index:064x}" + ".json"}
            raw = encoded({"schema_version": history.SCHEMA_VERSION, "entries": [entry]})
            name = history.CATALOG_CHUNK_PREFIX + digest(raw) + ".json"
            path = self.runtime / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
            refs.append({"path": name, "sha256": digest(raw), "bytes": len(raw), "entry_count": 1})
        (self.runtime / history.CATALOG_RELATIVE).write_bytes(encoded({
            "schema_version": history.INDEX_SCHEMA_VERSION, "chunks": refs, "entry_count": count}))
        return refs

    def offline(self):
        root = self.base / "offline"
        shutil.copytree(self.runtime, root / "runtime")
        state = "d" * 40
        records = []
        def record(path, raw, commit, role, source_path=None):
            target = root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)
            item = {"path": path, "commit": commit, "role": role, "bytes": len(raw),
                    "sha256": digest(raw), "git_blob_sha": hashlib.sha1(
                        b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()}
            if source_path is not None:
                item["source_path"] = source_path
            records.append(item)
        for path in (root / "runtime" / "history_archive").rglob("*.json"):
            record(path.relative_to(root).as_posix(), path.read_bytes(), state, "state")
        for source in ["state_storage.json", *[entry["transport_path"] for entry in
                         history._catalog(self.runtime).values()]]:
            record("history_sources/" + self.oid + "/" + source,
                   (self.repo / source).read_bytes(), self.oid, "history", source)
        (root / "model_access_manifest.json").write_bytes(encoded({
            "status": "files_materialized_not_model_verified", "state_commit": state, "files": records}))
        return root

    def test_cold_reads_keep_all_original_checks_and_never_materialize(self):
        self.chunks(17)
        with history.verified_catalog_read_session(self.runtime):
            for _ in range(3):
                self.assertTrue(history.exists(self.runtime / self.name))
                self.assertEqual(history.read_bytes(self.runtime / self.name), self.raw)
        self.assertFalse((self.runtime / self.name).exists())
        self.assertIsNone(history._CATALOG_SCOPE.get())

    def test_all_chunks_and_index_redecoded_only_at_boundaries(self):
        self.chunks(17)
        with patch.object(history, "_decode_catalog", wraps=history._decode_catalog) as decode:
            with history.verified_catalog_read_session(self.runtime):
                for _ in range(100):
                    self.assertTrue(history.exists(self.runtime / self.name))
            self.assertEqual(decode.call_count, 2)

    def test_scope_does_not_trust_or_share_global_cache(self):
        self.chunks()
        history._catalog(self.runtime)
        for entries, _ in history._CATALOG_CACHE.values():
            entries.clear()
        with history.verified_catalog_read_session(self.runtime):
            entries = history._catalog(self.runtime)
            self.assertIn(self.name, entries)
            history._CATALOG_CACHE.clear()
            history._CATALOG_CACHE["unrelated"] = ({}, {})
            self.assertEqual(history.read_bytes(self.runtime / self.name), self.raw)
            with self.assertRaises(TypeError):
                entries[self.name]["raw_sha256"] = "b" * 64
            with self.assertRaises(TypeError):
                entries[self.name] = {}

    def test_modified_index_cannot_return_success_even_if_unreferenced(self):
        with self.assertRaises(StorageError):
            with history.verified_catalog_read_session(self.runtime):
                path = self.runtime / history.CATALOG_RELATIVE
                path.write_bytes(path.read_bytes() + b" ")
        self.assertIsNone(history._CATALOG_SCOPE.get())

    def test_same_size_chunk_change_after_cached_read_rejected(self):
        refs = self.chunks()
        with self.assertRaises(StorageError):
            with history.verified_catalog_read_session(self.runtime):
                self.assertEqual(history.read_bytes(self.runtime / self.name), self.raw)
                path = self.runtime / refs[-1]["path"]
                raw = path.read_bytes()
                path.write_bytes(raw.replace(b'"raw_bytes":', b'"raw_byteZ":'))
                self.assertEqual(path.stat().st_size, len(raw))

    def test_catalog_delete_or_creation_rejected(self):
        catalog = self.runtime / history.CATALOG_RELATIVE
        raw = catalog.read_bytes()
        for mutation in (lambda: catalog.unlink(), lambda: catalog.write_bytes(raw)):
            with self.subTest(mutation=mutation):
                with self.assertRaises(StorageError):
                    with history.verified_catalog_read_session(self.runtime):
                        mutation()
                catalog.unlink(missing_ok=True)

    def test_chunk_symlink_and_parent_symlink_rejected(self):
        refs = self.chunks()
        chunk = self.runtime / refs[0]["path"]
        backup = self.base / "backup.json"
        backup.write_bytes(chunk.read_bytes())
        with self.assertRaises(StorageError):
            with history.verified_catalog_read_session(self.runtime):
                chunk.unlink()
                chunk.symlink_to(backup)
        chunk.unlink()
        chunk.write_bytes(backup.read_bytes())
        directory = chunk.parent
        moved = self.base / "chunks"
        with self.assertRaises(StorageError):
            with history.verified_catalog_read_session(self.runtime):
                directory.rename(moved)
                directory.symlink_to(moved, target_is_directory=True)

    def test_root_replacement_fails_even_with_identical_catalog(self):
        moved = self.repo / "old-active"
        with self.assertRaises(StorageError):
            with history.verified_catalog_read_session(self.runtime):
                self.runtime.rename(moved)
                shutil.copytree(moved, self.runtime)

    def test_illegal_operations_poison_scope_when_locally_caught(self):
        other = self.base / "other"
        other.mkdir()
        hot = other / "hot.json"
        hot.write_bytes(b"{}")
        for operation in (lambda: history._write_catalog(self.runtime, {}),
                          lambda: history._write_catalog(other, {}),
                          lambda: history._catalog(other),
                          lambda: history.read_bytes(hot),
                          lambda: history.exists(hot),
                          lambda: history.read_bytes(self.runtime / self.name, runtime=other)):
            with self.subTest(operation=operation):
                with self.assertRaises(StorageError):
                    with history.verified_catalog_read_session(self.runtime):
                        with self.assertRaises(StorageError):
                            operation()
                self.assertIsNone(history._CATALOG_SCOPE.get())
        with self.assertRaises(StorageError):
            with history.verified_catalog_read_session(self.runtime):
                with self.assertRaises(StorageError):
                    with history.verified_catalog_read_session(self.runtime):
                        self.fail("nested scope entered")

    def test_business_exception_preserved_and_scope_cleaned(self):
        failure = ValueError("real_business_failure")
        with self.assertRaises(ValueError) as raised:
            with history.verified_catalog_read_session(self.runtime):
                (self.runtime / history.CATALOG_RELATIVE).unlink()
                raise failure
        self.assertIs(raised.exception, failure)
        self.assertIn("catalog_session_final_check", failure.__notes__[0])
        self.assertIsNone(history._CATALOG_SCOPE.get())
        history._write_catalog(self.runtime, {})
        with history.verified_catalog_read_session(self.runtime):
            self.assertEqual(dict(history._catalog(self.runtime)), {})

    def test_two_scopes_do_not_reuse_mutable_entries(self):
        with history.verified_catalog_read_session(self.runtime):
            self.assertIn(self.name, history._catalog(self.runtime))
        history._write_catalog(self.runtime, {})
        with history.verified_catalog_read_session(self.runtime):
            self.assertEqual(dict(history._catalog(self.runtime)), {})

    def test_hot_cold_conflict_still_rejected_in_scope(self):
        path = self.runtime / self.name
        path.parent.mkdir(parents=True, exist_ok=True)
        with history.verified_catalog_read_session(self.runtime):
            self.assertEqual(history.read_bytes(path), self.raw)
            path.write_bytes(b"{}")
            with self.assertRaises(StorageError):
                history.read_bytes(path)

    def test_offline_bound_original_reads_and_mutations(self):
        root = self.offline()
        runtime = root / "runtime"
        with history.verified_catalog_read_session(runtime):
            self.assertEqual(history.read_bytes(runtime / self.name), self.raw)
        entry = next(iter(history._catalog(runtime).values()))
        targets = [root / "model_access_manifest.json",
                   root / "history_sources" / self.oid / "state_storage.json",
                   root / "history_sources" / self.oid / entry["transport_path"]]
        for target in targets:
            with self.subTest(target=target.name):
                raw = target.read_bytes()
                with self.assertRaises(StorageError):
                    with history.verified_catalog_read_session(runtime):
                        self.assertEqual(history.read_bytes(runtime / self.name), self.raw)
                        target.write_bytes(raw + b" ")
                target.write_bytes(raw)

    def test_offline_invalid_selected_catalog_binding_cannot_enter(self):
        root = self.offline()
        path = root / "runtime" / history.CATALOG_RELATIVE
        path.write_bytes(path.read_bytes() + b" ")
        with self.assertRaises(StorageError):
            with history.verified_catalog_read_session(root / "runtime"):
                self.fail("unbound catalog entered")

    def test_deleted_offline_binding_cannot_fall_through_to_git(self):
        root = self.offline()
        runtime = root / "runtime"
        with patch.object(history, "_git", side_effect=AssertionError("offline_git_attempt")) as git:
            with self.assertRaises(StorageError):
                with history.verified_catalog_read_session(runtime):
                    (root / "model_access_manifest.json").unlink()
                    history.read_bytes(runtime / self.name)
            self.assertEqual(git.call_count, 0)

    def test_offline_missing_bad_transport_and_bad_manifest_rejected(self):
        root = self.offline()
        runtime = root / "runtime"
        entry = next(iter(history._catalog(runtime).values()))
        target = root / "history_sources" / self.oid / entry["transport_path"]
        raw = target.read_bytes()
        for broken in (None, b"x" * len(raw)):
            if broken is None:
                target.unlink()
            else:
                target.write_bytes(broken)
            with self.assertRaises(StorageError):
                with history.verified_catalog_read_session(runtime):
                    history.read_bytes(runtime / self.name)
        target.write_bytes(raw)
        target = root / "history_sources" / self.oid / "state_storage.json"
        target.write_bytes(b"{}")
        with self.assertRaises(StorageError):
            with history.verified_catalog_read_session(runtime):
                history.read_bytes(runtime / self.name)

    def test_freeze_matches_unscoped_bytes_order_and_capacity(self):
        parent = self.runtime / "parent.json"
        child = {"path": self.name, "sha256": digest(self.raw)}
        parent.write_bytes(encoded({"refs": [child] * 5}))
        refs = [{"path": "parent.json", "sha256": digest(parent.read_bytes())}, child, child]
        twin = self.repo / "twin"
        shutil.copytree(self.runtime, twin)
        cutoff = datetime(2025, 1, 2, tzinfo=timezone.utc)
        first, second = CapacityGuard(self.runtime), CapacityGuard(twin)
        with patch.object(snapshots, "verified_catalog_read_session", side_effect=lambda _: nullcontext()):
            old = snapshots.freeze_inputs(self.runtime, self.runtime / "frozen", refs, cutoff,
                                          capacity_guard=first)
        new = snapshots.freeze_inputs(twin, twin / "frozen", refs, cutoff, capacity_guard=second)
        def normalized(values, runtime):
            return [{**value, "path": str(Path(value["path"]).relative_to(runtime))} for value in values]
        self.assertEqual(normalized(old, self.runtime), normalized(new, twin))
        self.assertEqual(first.summary(), second.summary())
        for value in new:
            self.assertEqual((twin / value["runtime_relative_path"]).read_bytes(),
                             history.read_bytes(self.runtime / value["origin_runtime_path"]))

    def test_freeze_duplicate_edges_do_not_hide_bad_sha_or_future_json(self):
        cutoff = datetime(2025, 1, 2, tzinfo=timezone.utc)
        good = {"path": self.name, "sha256": digest(self.raw)}
        bad = {**good, "sha256": "f" * 64}
        with self.assertRaisesRegex(SnapshotError, "advanced_input_hash_mismatch"):
            snapshots.freeze_inputs(self.runtime, self.runtime / "frozen", [bad, good], cutoff)
        for raw in (b'{"a":1,"a":2}', b'{"a":NaN}',
                    b'{"nested":{"generated_at":"2026-01-01T00:00:00Z"}}'):
            path = self.runtime / "bad.json"
            path.write_bytes(raw)
            ref = {"path": "bad.json", "sha256": digest(raw)}
            with self.assertRaises((SnapshotError, ValueError)):
                snapshots.freeze_inputs(self.runtime, self.runtime / "bad-frozen", [ref, ref], cutoff)
        self.assertIsNone(history._CATALOG_SCOPE.get())

    def test_freeze_capacity_admission_remains_enforced(self):
        guard = CapacityGuard(self.runtime, max_file=1)
        ref = {"path": self.name, "sha256": digest(self.raw)}
        with self.assertRaises(CapacityDeferred):
            snapshots.freeze_inputs(self.runtime, self.runtime / "frozen", [ref],
                                   datetime(2025, 1, 2, tzinfo=timezone.utc), capacity_guard=guard)
        self.assertEqual(guard.writes, 0)
        self.assertFalse((self.runtime / "frozen").exists())


if __name__ == "__main__":
    unittest.main()
