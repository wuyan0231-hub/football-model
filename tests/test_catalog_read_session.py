"""A catalog scope saves metadata I/O, never original integrity checks."""
from contextlib import nullcontext
from datetime import datetime, timezone
import errno
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import sys
import types
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


class SafePathTraversalTests(unittest.TestCase):
    @staticmethod
    def reference(path):
        path = Path(path).absolute()
        if any(p.is_symlink() for p in (path, *path.parents)):
            raise StorageError("历史原件路径含符号链接，已停止。")
        return path

    def observed(self, function, path):
        original_stat = os.stat
        calls = []
        def forwarded(value, *args, **kwargs):
            self.assertIs(kwargs.get("follow_symlinks"), False)
            calls.append(os.fspath(value))
            return original_stat(value, *args, **kwargs)
        with patch.object(os, "stat", side_effect=forwarded):
            try:
                result = function(path)
                value = ("returned", type(result), result)
            except Exception as exc:
                value = ("raised", type(exc), getattr(exc, "errno", None), str(exc))
        return value, calls

    def test_real_paths_match_original_results_order_and_each_actual_stat(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory); parent = base / "a" / "b"; parent.mkdir(parents=True)
            leaf = parent / "file"; leaf.write_bytes(b"{}")
            link = base / "link"; link.symlink_to(parent, target_is_directory=True)
            broken = base / "broken"; broken.symlink_to(base / "absent")
            loop = base / "loop"; loop.symlink_to(loop)
            paths = [parent, leaf, parent / "missing", parent / "missing" / "child", leaf / "child",
                     Path("/"), Path("//"), Path("///"), Path("."), Path(".."), "",
                     "/" + str(leaf), str(parent) + "/../b/file", str(parent) + "/./file/", str(parent) + "/nul\0name",
                     str(parent) + "/bad\ud800name", link, link / "file", broken, broken / "child", loop / "child"]
            for path in paths:
                with self.subTest(path=repr(path)):
                    expected, original_calls = self.observed(self.reference, path)
                    actual, calls = self.observed(history._safe_path, path)
                    self.assertEqual(actual, expected)
                    self.assertEqual(calls, original_calls)
                    self.assertGreaterEqual(len(calls), 1)
            for bad in (None, 123, object()):
                for function in (self.reference, history._safe_path):
                    with self.assertRaises(TypeError): function(bad)

    def test_error_policy_matches_original_and_preserves_propagated_objects(self):
        errors = [FileNotFoundError(errno.ENOENT, "missing"), NotADirectoryError(errno.ENOTDIR, "notdir"),
                  OSError(errno.EBADF, "badfd"), OSError(errno.ELOOP, "loop"), ValueError("encoding"),
                  PermissionError(errno.EACCES, "denied"), PermissionError(errno.EPERM, "denied"), OSError(errno.EIO, "io")]
        for number in (21, 123, 1921, 5):
            error = OSError("synthetic Windows error"); error.winerror = number; errors.append(error)
        mixed = OSError(errno.EIO, "ignored Windows error overrides errno")
        mixed.winerror = 123; errors.append(mixed)
        path = Path("/missing/child")
        for error in errors:
            results, orders = [], []
            for function in (self.reference, history._safe_path):
                calls = []
                def fail(value, **kwargs):
                    self.assertIs(kwargs.get("follow_symlinks"), False)
                    calls.append(os.fspath(value)); raise error
                with patch.object(os, "stat", side_effect=fail):
                    try:
                        self.assertEqual(function(path), path); results.append("returned")
                    except (OSError, ValueError) as exc:
                        self.assertIs(exc, error); results.append(type(exc))
                orders.append(calls)
            self.assertEqual(results[0], results[1])
            self.assertEqual(orders[0], orders[1])
            if (getattr(error, "errno", None) in (errno.EACCES, errno.EPERM, errno.EIO)
                    and getattr(error, "winerror", None) not in (21, 123, 1921)):
                self.assertEqual(results, [type(error), type(error)])

    def test_first_propagated_ancestor_error_stops_at_the_original_position(self):
        path = Path("/alpha/beta/missing")
        denied = PermissionError(errno.EACCES, "ancestor denied")
        for function in (self.reference, history._safe_path):
            calls = []
            def fail(value, **kwargs):
                value = os.fspath(value); calls.append(value)
                if value == str(path): raise FileNotFoundError(errno.ENOENT, "missing leaf")
                if value == "/alpha/beta": raise denied
                self.fail("traversal passed the first propagated error")
            with patch.object(os, "stat", side_effect=fail):
                with self.assertRaises(PermissionError) as raised: function(path)
                self.assertIs(raised.exception, denied)
                self.assertEqual(calls, [str(path), "/alpha/beta"])

    def test_baseexception_deadline_is_never_swallowed(self):
        from scripts.fixed_capture_input_profile import ProfileDeadline
        for function in (self.reference, history._safe_path):
            deadline = ProfileDeadline("bounded_diagnostic_deadline")
            with patch.object(os, "stat", side_effect=deadline) as observed:
                with self.assertRaises(ProfileDeadline) as raised:
                    function("/missing/child")
                self.assertIs(raised.exception, deadline)
                self.assertEqual(observed.call_count, 1)

    def test_native_path_reuse_does_not_trust_custom_subclass_methods(self):
        class UntrustedPath(type(Path())):
            def is_absolute(self):
                raise AssertionError("custom absolute predicate must not run")
            def __fspath__(self):
                raise AssertionError("custom path conversion must not run")
            def is_symlink(self):
                return False
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory); parent = base / "parent"; parent.mkdir()
            leaf = parent / "file"; leaf.write_bytes(b"{}")
            link = base / "link"; link.symlink_to(parent, target_is_directory=True)
            for path in (UntrustedPath(leaf), UntrustedPath("relative/missing"), UntrustedPath(link / "file")):
                expected, original_calls = self.observed(self.reference, path)
                actual, calls = self.observed(history._safe_path, path)
                self.assertEqual(actual, expected)
                self.assertEqual(calls, original_calls)
            with self.assertRaisesRegex(StorageError, "符号链接"):
                history._safe_path(UntrustedPath(link / "file"))

    def test_every_call_rechecks_replaced_ancestor_and_restored_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory); parent = base / "parent"; parent.mkdir()
            leaf = parent / "file"; leaf.write_bytes(b"{}")
            moved = base / "moved"
            for function in (self.reference, history._safe_path):
                for _ in range(2):
                    self.assertEqual(function(leaf), leaf)
                    parent.rename(moved); parent.symlink_to(moved, target_is_directory=True)
                    for _ in range(2):
                        with self.assertRaisesRegex(StorageError, "符号链接"):
                            function(leaf)
                    parent.unlink(); moved.rename(parent)
                    self.assertEqual(function(leaf), leaf)

    def test_file_identity_and_scope_change_revert_gate_are_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory); leaf = base / "file"; target = base / "target"; target.write_bytes(b"{}")
            for function in (self.reference, history._safe_path):
                leaf.write_bytes(b"original")
                with patch.object(history, "_safe_path", function):
                    actual = leaf.stat()
                    self.assertEqual(history._file_identity(leaf), (actual.st_dev, actual.st_ino, actual.st_mode,
                        actual.st_size, actual.st_mtime_ns, actual.st_ctime_ns))
                    session = history._VerifiedCatalogReadSession(base); session.pin(leaf)
                    leaf.unlink(); leaf.symlink_to(target)
                    with self.assertRaises(StorageError): history._file_identity(leaf)
                    leaf.unlink(); leaf.write_bytes(b"original")
                    with self.assertRaisesRegex(StorageError, "input_changed"):
                        session.verify(contents=True)
                leaf.unlink()


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

    def test_digest_read_hashes_each_actual_raw_once_and_preserves_all_body_checks(self):
        with history.FixedStateReader(self.repo, self.oid) as reader:
            token = history._READER_SCOPE.set(reader)
            try:
                with history.verified_catalog_read_session(self.runtime):
                    history.read_bytes_with_digest(self.runtime / self.name)
                    with patch.object(history, "_sha", wraps=history._sha) as sha, \
                            patch.object(history, "_expand", wraps=history._expand) as expand, \
                            patch.object(history, "_manifest_json", wraps=history._manifest_json) as parse, \
                            patch.object(history, "_scan_secret", wraps=history._scan_secret) as secret:
                        for _ in range(3):
                            raw, actual = history.read_bytes_with_digest(self.runtime / self.name)
                            self.assertIsInstance(raw, bytes)
                            self.assertEqual(raw, self.raw)
                            self.assertEqual(actual, digest(self.raw))
                        self.assertEqual(sum(call.args[0] == self.raw for call in sha.call_args_list), 3)
                        self.assertEqual(expand.call_count, 3)
                        self.assertEqual(parse.call_count, 3)
                        self.assertEqual(secret.call_count, 3)
                    self.assertIsInstance(history.read_bytes(self.runtime / self.name), bytes)
            finally:
                history._READER_SCOPE.reset(token)

    def test_digest_read_rejects_wrong_body_before_json_and_preserves_business_errors(self):
        with history.verified_catalog_read_session(self.runtime):
            history.read_bytes_with_digest(self.runtime / self.name)
            for read in (history.read_bytes, history.read_bytes_with_digest):
                with patch.object(history, "_expand", return_value=b"x" * len(self.raw)), \
                        patch.object(history, "_manifest_json", wraps=history._manifest_json) as parse:
                    with self.assertRaisesRegex(StorageError, "恢复原件长度或指纹不符"):
                        read(self.runtime / self.name)
                    self.assertEqual(parse.call_count, 0)
                for stage in ("_manifest_json", "_scan_secret"):
                    error = StorageError("actual body validation failure")
                    with patch.object(history, stage, side_effect=error):
                        with self.assertRaises(StorageError) as raised:
                            read(self.runtime / self.name)
                        self.assertIs(raised.exception, error)

    def test_digest_hot_read_uses_current_bytes_hash_and_keeps_change_revert_guard(self):
        path = self.runtime / "hot.json"
        path.write_bytes(self.raw)
        with self.assertRaisesRegex(StorageError, "input_changed"):
            with history.verified_catalog_read_session(self.runtime):
                with patch.object(history, "_sha", wraps=history._sha) as sha:
                    for _ in range(3):
                        self.assertEqual(history.read_bytes_with_digest(path), (self.raw, digest(self.raw)))
                    self.assertEqual(sum(call.args[0] == self.raw for call in sha.call_args_list), 3)
                changed = self.raw.replace(b"1", b"9", 1)
                path.write_bytes(changed)
                path.write_bytes(self.raw)
        self.assertIsNone(history._CATALOG_SCOPE.get())

    def test_digest_read_keeps_offline_source_binding_and_identity_routes(self):
        root = self.offline()
        with history.verified_catalog_read_session(root / "runtime"):
            self.assertEqual(history.read_bytes_with_digest(root / "runtime" / self.name),
                             (self.raw, digest(self.raw)))
        source = self.repo / "runtime" / self.name
        source.parent.mkdir(parents=True)
        source.write_bytes(self.raw)
        self.git("add", "runtime"); self.git("commit", "-qm", "direct original")
        commit = self.git("rev-parse", "HEAD").decode().strip()
        entry = {"path": self.name, "state_commit": commit, "transport_path": "runtime/" + self.name,
                 "codec": "identity", "raw_sha256": digest(self.raw), "raw_bytes": len(self.raw),
                 "blob_sha256": digest(self.raw), "compressed_bytes": len(self.raw), "manifest_sha256": None}
        history._write_catalog(self.runtime, {self.name: entry})
        with history.verified_catalog_read_session(self.runtime):
            self.assertEqual(history.read_bytes_with_digest(self.runtime / self.name),
                             (self.raw, digest(self.raw)))

    def test_digest_read_rejects_transport_corruption_and_changed_git_binding_after_a_hit(self):
        with history.FixedStateReader(self.repo, self.oid) as reader:
            token = history._READER_SCOPE.set(reader)
            try:
                with self.assertRaisesRegex(StorageError, "binding_changed"):
                    with history.verified_catalog_read_session(self.runtime):
                        history.read_bytes_with_digest(self.runtime / self.name)
                        entry = history._catalog(self.runtime)[self.name]
                        key = (self.oid, entry["transport_path"])
                        compressed = reader._cache[key]
                        reader._cache[key] = b"x" * len(compressed)
                        with patch.object(history, "_expand", wraps=history._expand) as expand:
                            with self.assertRaisesRegex(StorageError, "压缩原件长度或指纹不符"):
                                history.read_bytes_with_digest(self.runtime / self.name)
                            self.assertEqual(expand.call_count, 0)
                        reader._cache[key] = compressed
                        marker = self.repo / ".git"
                        moved = self.repo / "original-git"
                        marker.rename(moved); shutil.copytree(moved, marker)
                        history.read_bytes_with_digest(self.runtime / self.name)
                self.assertEqual(reader.member_cache_metrics()["current_entries"], 0)
            finally:
                history._READER_SCOPE.reset(token)

    def test_freeze_uses_the_digest_from_each_validated_read_without_rehashing_each_edge(self):
        cutoff = datetime(2025, 1, 2, tzinfo=timezone.utc)
        ref = {"path": self.name, "sha256": digest(self.raw)}
        sha = hashlib.sha256
        freeze = snapshots._guarded_freeze
        freezer_hashes = 0
        def raw_hash_count():
            return sum(call.args[0] == self.raw for call in actual.call_args_list)
        def counted_freeze(*args, **kwargs):
            nonlocal freezer_hashes
            before = raw_hash_count()
            result = freeze(*args, **kwargs)
            freezer_hashes += raw_hash_count() - before
            return result
        # The actual freezer and capacity guard retain their own checks.
        # Outside that first-write span, each edge hashes its raw once.
        with patch.object(hashlib, "sha256", wraps=sha) as actual, \
                patch.object(history, "_expand", wraps=history._expand) as expand, \
                patch.object(snapshots, "_guarded_freeze", side_effect=counted_freeze) as freezer:
            frozen = snapshots.freeze_inputs(self.runtime, self.runtime / "frozen", [ref] * 4, cutoff)
        self.assertEqual(raw_hash_count() - freezer_hashes, 4)
        self.assertEqual(freezer.call_count, 1)
        self.assertEqual(expand.call_count, 4)
        self.assertEqual(len(frozen), 1)
        self.assertEqual(Path(frozen[0]["path"]).read_bytes(), self.raw)

    def test_manifest_reads_do_not_evict_transport_or_change_git_bytes(self):
        manifest_raw = (self.repo / "state_storage.json").read_bytes()
        transport = "state_archives/" + self.manifest["entries"][0]["blob_sha256"] + ".json.gz"
        compressed = (self.repo / transport).read_bytes()
        self.assertGreater(len(manifest_raw), len(compressed))
        with history.FixedStateReader(self.repo, self.oid, cache_bytes=len(manifest_raw)) as reader:
            first = reader._blob(self.oid, transport)
            self.assertEqual(first, compressed)
            output = reader._process.stdout
            class CountOutput:
                def __init__(self): self.headers = 0
                def readline(self, *args):
                    self.headers += 1
                    return output.readline(*args)
                def __getattr__(self, name): return getattr(output, name)
            counter = CountOutput(); reader._process.stdout = counter
            for _ in range(2):
                self.assertEqual(reader._blob(self.oid, "state_storage.json"), manifest_raw)
                self.assertIs(reader._blob(self.oid, transport), first)
            self.assertEqual(counter.headers, 2)
            self.assertNotIn((self.oid, "state_storage.json"), reader._cache)
            self.assertEqual(reader._cached_bytes, len(compressed))
            self.assertEqual(reader.cache_bytes, len(manifest_raw))

    def test_manifest_admission_rule_preserves_each_cold_edge_validation(self):
        with history.FixedStateReader(self.repo, self.oid) as reader:
            token = history._READER_SCOPE.set(reader)
            try:
                with patch.object(history, "_expand", wraps=history._expand) as expand, \
                        patch.object(history, "_manifest_index", wraps=history._manifest_index) as manifest, \
                        patch.object(history, "_manifest_member", wraps=history._manifest_member) as member:
                    with history.verified_catalog_read_session(self.runtime):
                        for _ in range(3):
                            self.assertEqual(history.read_bytes(self.runtime / self.name), self.raw)
                        self.assertEqual(reader.cache_bytes, 56 * 1024 * 1024)
                        self.assertEqual(reader.member_cache_metrics()["hits"], 2)
                    self.assertEqual(expand.call_count, 3)
                    self.assertEqual(member.call_count, 3)
                    self.assertEqual(manifest.call_count, 1)
                self.assertEqual(reader.cache_bytes, 64 * 1024 * 1024)
                self.assertEqual(reader.member_cache_metrics()["current_entries"], 0)
                self.assertNotIn((self.oid, "state_storage.json"), reader._cache)
            finally:
                history._READER_SCOPE.reset(token)

    def test_member_reservation_evicts_existing_payload_and_survives_initial_close(self):
        with history.FixedStateReader(self.repo, self.oid) as reader:
            # Exercise admission before a batch process exists, as production
            # does; _blob's first internal close must keep the reservation.
            large = b"x" * (30 * 1024 * 1024)
            reader._cache[(self.oid, "one")] = large
            reader._cache[(self.oid, "two")] = large
            reader._cached_bytes = 2 * len(large)
            token = history._READER_SCOPE.set(reader)
            try:
                with history.verified_catalog_read_session(self.runtime) as session:
                    self.assertTrue(reader._reserve_members(session))
                    self.assertEqual(reader._cached_bytes, len(large))
                    self.assertEqual(history.read_bytes(self.runtime / self.name), self.raw)
                    self.assertEqual(reader.cache_bytes, 56 * 1024 * 1024)
                    self.assertLessEqual(reader._cached_bytes, reader.cache_bytes)
                    metrics = reader.member_cache_metrics()
                    self.assertEqual(metrics["current_entries"], 1)
                    self.assertLessEqual(reader._cached_bytes + metrics["current_bytes"], reader.total_cache_bytes)
                    self.assertEqual(reader.cache_budget_generation, 1)
                    reader.close()
                    self.assertEqual(reader.member_cache_metrics()["current_entries"], 0)
                    self.assertEqual(reader.member_cache_budget_bytes, 8 * 1024 * 1024)
                    self.assertEqual(reader.cache_bytes, 56 * 1024 * 1024)
                    self.assertEqual(history.read_bytes(self.runtime / self.name), self.raw)
                self.assertEqual(reader.cache_bytes, reader.total_cache_bytes)
                self.assertEqual(reader.member_cache_budget_bytes, 0)
                self.assertEqual(reader.cache_budget_generation, 2)
            finally: history._READER_SCOPE.reset(token)

    def test_member_cache_has_deep_byte_and_entry_bounds_with_oversize_opt_out(self):
        paths = ["advanced_models/inputs/" + f"{i:064x}" + ".json" for i in range(108665)]
        session = types.SimpleNamespace(member_readers=set(), failure=None,
            entries=dict.fromkeys(paths, {"codec": "identity"}))
        with history.FixedStateReader(self.repo, self.oid) as reader:
            self.assertTrue(reader._reserve_members(session))
            for i in range(160):
                key = ("库" * 40000 + str(i), self.oid, f"{i:064x}")
                reader._remember_members(session, key, {})
            metrics = reader.member_cache_metrics()
            self.assertGreater(metrics["evictions"], 0)
            self.assertLessEqual(metrics["peak_bytes"], history.MEMBER_CACHE_BYTES)
            self.assertLessEqual(metrics["peak_entries"], history.MEMBER_CACHE_MAX_SOURCES)
            self.assertEqual(metrics["indexed_paths"], 108665)
            self.assertEqual(metrics["index_charge_bytes"], 20 * 108665 + 1024)
            self.assertEqual(metrics["index_charge_bytes"] + metrics["proof_charge_bytes"], metrics["current_bytes"])
            original_path_ids = {id(path) for path in paths}
            self.assertTrue(all(id(path) in original_path_ids for path in reader._member_paths))
            seen = set()
            def deep_size(value):
                if id(value) in seen: return 0
                seen.add(id(value)); total = sys.getsizeof(value)
                if isinstance(value, dict):
                    total += sum(deep_size(k) + deep_size(v) for k, v in value.items())
                elif isinstance(value, tuple): total += sum(map(deep_size, value))
                return total
            self.assertLessEqual(deep_size(reader._member_cache) + sys.getsizeof(reader._member_paths), metrics["current_bytes"])
            oversized = ("x" * history.MEMBER_CACHE_BYTES, self.oid, "d" * 64)
            reader._remember_members(session, oversized, {})
            self.assertNotIn(oversized, reader._member_cache)
            reader._clear_members()
            with patch.object(history, "MEMBER_CACHE_MAX_SOURCES", 2):
                for i in range(3):
                    reader._remember_members(session, ("repo", "commit", str(i)), {})
                self.assertEqual(len(reader._member_cache), 2)
            reader._release_members(session)
            self.assertEqual(reader.member_cache_metrics()["current_bytes"], 0)

    def test_member_cache_scope_concurrency_and_custom_readers_opt_out(self):
        first = types.SimpleNamespace(member_readers=set(), failure=None, entries={})
        second = types.SimpleNamespace(member_readers=set(), failure=None, entries={})
        with history.FixedStateReader(self.repo, self.oid) as reader:
            self.assertTrue(reader._reserve_members(first))
            self.assertFalse(reader._reserve_members(second))
            self.assertEqual(reader.cache_bytes + reader.member_cache_budget_bytes, reader.total_cache_bytes)
            reader._release_members(first)
            self.assertTrue(reader._reserve_members(second))
            reader._release_members(second)
        with history.FixedStateReader(self.repo, self.oid, cache_bytes=128) as reader:
            self.assertFalse(reader._reserve_members(first))
            self.assertEqual(reader.cache_bytes, 128)
        with history.FixedStateReader(self.repo, self.oid) as reader:
            with patch.object(history, "MEMBER_CACHE_MAX_PATHS", -1):
                self.assertFalse(reader._reserve_members(first))
            with patch.object(history, "MEMBER_CACHE_BYTES", 100):
                self.assertFalse(reader._reserve_members(first))
            self.assertEqual(reader.cache_bytes, history.READER_CACHE_BYTES)

    def test_member_hits_cannot_hide_route_hash_or_five_field_changes(self):
        with history.FixedStateReader(self.repo, self.oid) as reader:
            token = history._READER_SCOPE.set(reader)
            try:
                with history.verified_catalog_read_session(self.runtime):
                    self.assertEqual(history.read_bytes(self.runtime / self.name), self.raw)
                    entry = dict(history._catalog(self.runtime)[self.name])
                    before = reader.member_cache_metrics()["current_entries"]
                    for field, value in (("manifest_sha256", "f" * 64), ("raw_bytes", len(self.raw) + 1),
                                         ("raw_sha256", "e" * 64), ("path", self.name + "other"),
                                         ("state_commit", "d" * 40)):
                        changed = {**entry, field: value}
                        with self.subTest(field=field), \
                                patch.object(history, "_fetch_cold_commit", side_effect=StorageError("unknown_source")), \
                                self.assertRaises((StorageError, FileNotFoundError)):
                            history._entry_body(self.repo, changed, set())
                        self.assertEqual(reader.member_cache_metrics()["current_entries"], before)
                    key = next(iter(reader._member_cache))
                    bits, charge, _ = reader._member_cache[key]
                    reader._member_cache[key] = (bytes(len(bits)), charge, 0)
                    with patch.object(history, "_manifest_index", wraps=history._manifest_index) as manifest:
                        self.assertEqual(history.read_bytes(self.runtime / self.name), self.raw)
                        self.assertEqual(manifest.call_count, 1)
            finally: history._READER_SCOPE.reset(token)

    def test_member_hits_keep_transport_expand_raw_json_and_secret_validation(self):
        with history.FixedStateReader(self.repo, self.oid) as reader:
            token = history._READER_SCOPE.set(reader)
            try:
                with history.verified_catalog_read_session(self.runtime):
                    self.assertEqual(history.read_bytes(self.runtime / self.name), self.raw)
                    entry = history._catalog(self.runtime)[self.name]
                    payload = reader._cache[(self.oid, entry["transport_path"])]
                    reader._cache[(self.oid, entry["transport_path"])] = b"x" * len(payload)
                    with self.assertRaisesRegex(StorageError, "压缩原件长度或指纹不符"):
                        history.read_bytes(self.runtime / self.name)
                    reader._cache[(self.oid, entry["transport_path"])] = payload
                    for replacement, message in ((self.raw + b" ", "恢复原件长度或指纹不符"),):
                        with patch.object(history, "_expand", return_value=replacement):
                            with self.assertRaisesRegex(StorageError, message):
                                history.read_bytes(self.runtime / self.name)
                    with patch.object(history, "_manifest_json", wraps=history._manifest_json) as parse, \
                            patch.object(history, "_scan_secret", wraps=history._scan_secret) as scan, \
                            patch.object(history, "_expand", wraps=history._expand) as expand:
                        for _ in range(3): self.assertEqual(history.read_bytes(self.runtime / self.name), self.raw)
                        self.assertEqual(expand.call_count, 3)
                        self.assertEqual(parse.call_count, 3)
                        self.assertEqual(scan.call_count, 3)
                self.assertEqual(reader.member_cache_metrics()["current_entries"], 0)
            finally: history._READER_SCOPE.reset(token)

    def test_member_cache_cleared_on_business_failure_and_source_binding_change(self):
        with history.FixedStateReader(self.repo, self.oid) as reader:
            token = history._READER_SCOPE.set(reader)
            try:
                with self.assertRaisesRegex(ValueError, "business"):
                    with history.verified_catalog_read_session(self.runtime):
                        history.read_bytes(self.runtime / self.name)
                        raise ValueError("business")
                self.assertEqual(reader.member_cache_metrics()["current_entries"], 0)
                self.assertEqual(reader.cache_bytes, history.READER_CACHE_BYTES)
                with self.assertRaisesRegex(StorageError, "binding_changed"):
                    with history.verified_catalog_read_session(self.runtime):
                        history.read_bytes(self.runtime / self.name)
                        marker = self.repo / ".git"
                        moved = self.repo / "original-git"
                        marker.rename(moved); shutil.copytree(moved, marker)
                        history.read_bytes(self.runtime / self.name)
                self.assertEqual(reader.member_cache_metrics()["current_entries"], 0)
                self.assertEqual(reader.cache_bytes, history.READER_CACHE_BYTES)
            finally: history._READER_SCOPE.reset(token)

    def test_member_cache_offline_and_unscoped_keep_original_path(self):
        root = self.offline()
        with patch.object(history.FixedStateReader, "_reserve_members", side_effect=AssertionError("ineligible")):
            self.assertEqual(history.read_bytes(self.runtime / self.name), self.raw)
            with history.verified_catalog_read_session(root / "runtime"):
                for _ in range(2): self.assertEqual(history.read_bytes(root / "runtime" / self.name), self.raw)

    def test_member_cache_reuses_three_real_git_sources_with_two_full_indexes(self):
        entries, originals = {}, []
        for i in range(3):
            source = self.base / ("source-" + str(i))
            group = []
            for j in range(200):
                name = "advanced_models/inputs/" + f"{i * 200 + j:064x}" + ".json"
                raw = encoded({"original": i * 200 + j})
                path = source / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(raw)
                group.append((name, raw))
            packed = self.base / ("packed-" + str(i))
            packed.mkdir()
            pack_runtime(source, packed)
            manifest_raw = (packed / "state_storage.json").read_bytes()
            manifest = json.loads(manifest_raw)
            shutil.copy2(packed / "state_storage.json", self.repo / "state_storage.json")
            for blob in (packed / "state_archives").iterdir():
                shutil.copy2(blob, self.repo / "state_archives" / blob.name)
            self.git("add", "state_storage.json", "state_archives")
            self.git("commit", "-qm", "another immutable source")
            commit = self.git("rev-parse", "HEAD").decode().strip()
            for item in manifest["entries"]:
                entries[item["path"]] = history._transport_entry(item["path"], item, commit, digest(manifest_raw))
            originals.append(group)
        history._write_catalog(self.runtime, entries)
        history._MANIFEST_CACHE.clear()
        with history.FixedStateReader(self.repo, self.oid) as reader:
            token = history._READER_SCOPE.set(reader)
            try:
                with patch.object(history, "_manifest_index", wraps=history._manifest_index) as manifest, \
                        patch.object(history, "_expand", wraps=history._expand) as expand:
                    with history.verified_catalog_read_session(self.runtime):
                        # Every initial edge is a new path, interleaved across
                        # three commits. A lazy per-request tuple cache would
                        # still reload all 600 complete source manifests.
                        for group in zip(*originals):
                            for name, raw in group:
                                self.assertEqual(history.read_bytes(self.runtime / name), raw)
                        self.assertEqual(manifest.call_count, 3)
                        self.assertEqual(expand.call_count, 600)
                        self.assertLessEqual(len(history._MANIFEST_CACHE), 2)
                        self.assertEqual(reader.member_cache_metrics()["hits"], 597)
                        self.assertEqual(reader.member_cache_metrics()["current_entries"], 3)
                        self.assertEqual(reader.member_cache_metrics()["current_proved_members"], 600)
                self.assertEqual(reader.member_cache_metrics()["current_entries"], 0)
            finally: history._READER_SCOPE.reset(token)

    def test_member_first_read_failures_never_admit_projection(self):
        with history.FixedStateReader(self.repo, self.oid) as reader:
            token = history._READER_SCOPE.set(reader)
            try:
                with history.verified_catalog_read_session(self.runtime):
                    with patch.object(history, "_expand", return_value=self.raw + b" "):
                        with self.assertRaisesRegex(StorageError, "恢复原件长度或指纹不符"):
                            history.read_bytes(self.runtime / self.name)
                    self.assertEqual(reader.member_cache_metrics()["current_entries"], 0)
                    self.assertEqual(history.read_bytes(self.runtime / self.name), self.raw)
                    self.assertEqual(reader.member_cache_metrics()["current_entries"], 1)
            finally: history._READER_SCOPE.reset(token)
        original = dict(history._catalog(self.runtime)[self.name])
        duplicate = {**self.manifest, "entries": self.manifest["entries"] * 2}
        for raw, expected_sha, message in ((encoded(self.manifest), "f" * 64, "来源清单指纹不符"),
                (b"{}", None, "来源清单无效"),
                (encoded(duplicate), None, "路径重复或无效")):
            (self.repo / "state_storage.json").write_bytes(raw)
            self.git("add", "state_storage.json"); self.git("commit", "--allow-empty", "-qm", "invalid source")
            commit = self.git("rev-parse", "HEAD").decode().strip()
            entry = {**original, "state_commit": commit, "manifest_sha256": expected_sha or digest(raw)}
            history._write_catalog(self.runtime, {self.name: entry})
            with history.FixedStateReader(self.repo, commit) as reader:
                token = history._READER_SCOPE.set(reader)
                try:
                    with self.assertRaisesRegex(StorageError, message):
                        with history.verified_catalog_read_session(self.runtime):
                            history.read_bytes(self.runtime / self.name)
                    self.assertEqual(reader.member_cache_metrics()["current_entries"], 0)
                    self.assertEqual(reader.cache_bytes, history.READER_CACHE_BYTES)
                finally: history._READER_SCOPE.reset(token)

    def test_bulk_proof_zero_bits_keep_unrelated_bad_entries_local(self):
        other = "advanced_models/inputs/" + "b" * 64 + ".json"
        original = dict(history._catalog(self.runtime)[self.name])
        for extra in (None, {"extra": True}, {"raw_bytes": len(self.raw) + 1}):
            rows = list(self.manifest["entries"])
            if extra is not None:
                rows.append({**rows[0], "path": other, **extra})
            raw = encoded({**self.manifest, "entries": rows})
            (self.repo / "state_storage.json").write_bytes(raw)
            self.git("add", "state_storage.json"); self.git("commit", "--allow-empty", "-qm", "partly invalid source")
            commit = self.git("rev-parse", "HEAD").decode().strip()
            entries = {name: {**original, "path": name, "state_commit": commit,
                              "manifest_sha256": digest(raw)} for name in (self.name, other)}
            history._write_catalog(self.runtime, entries)
            with history.FixedStateReader(self.repo, commit) as reader:
                token = history._READER_SCOPE.set(reader)
                try:
                    with history.verified_catalog_read_session(self.runtime):
                        # The unrelated bad/missing member cannot invalidate a
                        # good requested path or acquire a positive proof bit.
                        self.assertEqual(history.read_bytes(self.runtime / self.name), self.raw)
                        self.assertEqual(reader.member_cache_metrics()["current_proved_members"], 1)
                        with patch.object(history, "_manifest_index", wraps=history._manifest_index) as manifest:
                            with self.assertRaisesRegex(StorageError, "固定来源清单不符"):
                                history.read_bytes(self.runtime / other)
                            self.assertEqual(manifest.call_count, 1)
                        self.assertEqual(reader.member_cache_metrics()["current_proved_members"], 1)
                finally: history._READER_SCOPE.reset(token)

    def test_bulk_proof_construction_is_precharged_and_overflow_falls_back(self):
        entry = dict(history._catalog(self.runtime)[self.name])
        session = types.SimpleNamespace(member_readers=set(), failure=None, entries={self.name: entry})
        key = (str(self.repo), self.oid, entry["manifest_sha256"])
        index = {self.name: {field: entry[field] for field in history._MEMBER_FIELDS}}
        with history.FixedStateReader(self.repo, self.oid) as reader:
            self.assertTrue(reader._reserve_members(session))
            sorted_native, bytearray_native = sorted, bytearray
            def observed_sort(value):
                self.assertEqual(reader.member_cache_metrics()["index_charge_bytes"], 20 + 1024)
                self.assertEqual(reader.member_cache_metrics()["current_bytes"], 20 + 1024)
                return sorted_native(value)
            def observed_bits(size):
                metrics = reader.member_cache_metrics()
                self.assertGreater(metrics["proof_charge_bytes"], 2 * size)
                self.assertLessEqual(metrics["current_bytes"], metrics["budget_bytes"])
                return bytearray_native(size)
            with patch.object(history, "sorted", observed_sort, create=True), \
                    patch.object(history, "bytearray", observed_bits, create=True):
                reader._remember_members(session, key, index)
            self.assertTrue(reader._member(session, key, self.name))
            reader._clear_members()
            with patch.object(history, "MEMBER_CACHE_MAX_SOURCES", 0):
                reader._remember_members(session, key, index)
            self.assertEqual(reader.member_cache_metrics()["current_entries"], 0)
            with patch.object(history, "MEMBER_CACHE_MAX_PATHS", 0):
                reader._remember_members(session, key, index)
            self.assertEqual(reader.member_cache_metrics()["current_entries"], 0)
            reader.member_cache_budget_bytes = 1024
            reader._remember_members(session, key, index)
            self.assertEqual(reader.member_cache_metrics()["current_entries"], 0)
            reader._release_members(session)

    def test_manifest_missing_and_invalid_errors_are_preserved(self):
        (self.repo / "state_storage.json").unlink()
        self.git("add", "state_storage.json"); self.git("commit", "-qm", "manifest absent")
        missing_commit = self.git("rev-parse", "HEAD").decode().strip()
        with history.FixedStateReader(self.repo, missing_commit) as reader:
            self.assertIsNone(reader._blob(missing_commit, "state_storage.json", missing=True))
            with self.assertRaisesRegex(StorageError, "固定历史对象未保存"):
                reader._blob(missing_commit, "state_storage.json")
        (self.repo / "state_storage.json").write_bytes(b'{"invalid_manifest":true}')
        self.git("add", "state_storage.json"); self.git("commit", "-qm", "manifest invalid")
        bad_commit = self.git("rev-parse", "HEAD").decode().strip()
        with history.FixedStateReader(self.repo, bad_commit) as reader:
            token = history._READER_SCOPE.set(reader)
            try:
                with self.assertRaisesRegex(StorageError, "来源清单无效"):
                    history._manifest_index(self.repo, bad_commit)
                self.assertNotIn((bad_commit, "state_storage.json"), reader._cache)
            finally:
                history._READER_SCOPE.reset(token)

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

    def test_hot_original_change_then_revert_rejects_scope(self):
        hot = self.runtime / "hot.json"
        raw = encoded({"value": 1})
        hot.write_bytes(raw)
        with self.assertRaisesRegex(StorageError, "verified_catalog_session_input_changed"):
            with history.verified_catalog_read_session(self.runtime):
                self.assertEqual(history.read_bytes(hot), raw)
                hot.write_bytes(encoded({"value": 2}))
                hot.write_bytes(raw)
        self.assertIsNone(history._CATALOG_SCOPE.get())

    def test_freeze_hash_and_copy_use_the_same_bytes_when_hot_input_changes(self):
        hot = self.runtime / "hot.json"
        raw = encoded({"value": 1})
        hot.write_bytes(raw)
        reference = {"path": "hot.json", "sha256": digest(raw)}
        freeze = snapshots._guarded_freeze
        copied = []
        def change_then_freeze(path, *args, **kwargs):
            path.write_bytes(encoded({"value": 2}))
            result = freeze(path, *args, **kwargs)
            copied.append(result)
            return result
        with patch.object(snapshots, "_guarded_freeze", side_effect=change_then_freeze):
            with self.assertRaisesRegex(StorageError, "verified_catalog_session_input_changed"):
                snapshots.freeze_inputs(self.runtime, self.runtime / "frozen", [reference],
                                        datetime(2025, 1, 2, tzinfo=timezone.utc))
        # Even a rejected operation can only have copied the verified bytes.
        self.assertEqual(copied[0]["sha256"], reference["sha256"])
        self.assertEqual(Path(copied[0]["path"]).read_bytes(), raw)

    def test_freeze_recursion_never_follows_later_hot_input_bytes(self):
        hot = self.runtime / "hot.json"
        raw = encoded({"value": 1})
        hot.write_bytes(raw)
        reference = {"path": "hot.json", "sha256": digest(raw)}
        freeze = snapshots._guarded_freeze
        def freeze_then_change(path, *args, **kwargs):
            result = freeze(path, *args, **kwargs)
            path.write_bytes(encoded({"ref": {"path": "injected-missing.json", "sha256": "e" * 64}}))
            return result
        with patch.object(snapshots, "_guarded_freeze", side_effect=freeze_then_change), \
                patch.object(snapshots, "history_exists", wraps=snapshots.history_exists) as exists:
            with self.assertRaisesRegex(StorageError, "verified_catalog_session_input_changed"):
                snapshots.freeze_inputs(self.runtime, self.runtime / "frozen", [reference],
                                        datetime(2025, 1, 2, tzinfo=timezone.utc))
        self.assertEqual([call.args[0].name for call in exists.call_args_list], ["hot.json"])

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
