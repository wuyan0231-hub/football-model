"""Lossless cold originals pinned to immutable model-state Git commits.

The catalog is an index, never a substitute for the original.  Cold reads check
the original transport manifest, compressed hash and restored bytes.  Enumerate
logical paths through this module so a deliberately unexpanded original is not
mistaken for missing evidence.  New, unsaved output is never evicted.
"""
from __future__ import annotations

import fnmatch
import atexit
from collections import OrderedDict
from contextlib import contextmanager
from contextvars import ContextVar
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import stat as stat_module
import subprocess
import sys
import threading
from types import MappingProxyType

from .state_storage import (MAX_FILE_BYTES, StorageError, _expand, _manifest_json,
                            _relative, _scan_secret, _supported_limits)
from .storage_policy import MAX_PHYSICAL_BYTES

CATALOG_RELATIVE = "history_archive/catalog.json"
SCHEMA_VERSION = "fixed-git-history-catalog-v1"
INDEX_SCHEMA_VERSION = "fixed-git-history-catalog-index-v1"
CATALOG_CHUNK_PREFIX = "history_archive/catalog_chunks/"
CATALOG_INLINE_BYTES = 1024 * 1024
CATALOG_CHUNK_BYTES = 4 * 1024 * 1024
_OID = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")
_SHA = re.compile(r"[0-9a-f]{64}")
_FIELDS = {"path", "state_commit", "transport_path", "codec", "raw_sha256",
           "raw_bytes", "blob_sha256", "compressed_bytes", "manifest_sha256"}
_CATALOG_CACHE = {}
_MANIFEST_CACHE = {}
_STATE_CATALOG_CACHE = OrderedDict()
_VERIFIED_COMMITS = set()
_READERS = OrderedDict()
_READER_SCOPE = ContextVar("fixed_history_reader", default=None)
_CATALOG_SCOPE = ContextVar("verified_history_catalog", default=None)

# A depth-1 fetch has no blob filter: a missing source may require the whole
# immutable transport snapshot, not merely the selected logical original.
# index-pack --stdin writes the incoming pack once; its temporary file is
# renamed, not copied into a second complete pack. Keep index/metadata space and
# a separate full save envelope. Thin-pack repair can append out-of-tree bases,
# so a child-process file limit, not the assumed snapshot size, enforces the
# actual maximum receiving pack. Disable fetch maintenance/repacking explicitly.
COLD_FETCH_SNAPSHOT_BYTES = MAX_PHYSICAL_BYTES
COLD_FETCH_WORKSPACE_BYTES = 512 * 1024 * 1024
COLD_FETCH_SAVE_RESERVE_BYTES = 2 * MAX_PHYSICAL_BYTES + 512 * 1024 * 1024
COLD_FETCH_REQUIRED_FREE_BYTES = (COLD_FETCH_SNAPSHOT_BYTES + COLD_FETCH_WORKSPACE_BYTES
                                  + COLD_FETCH_SAVE_RESERVE_BYTES)


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def select_cold_path(name):
    """Only immutable, content-addressed large research objects are cold.

    Pointers, queues, first failures, active models and receipts stay hot.  Their
    referenced originals remain readable regardless of where bytes are stored.
    """
    name = _relative(name)
    return bool(re.fullmatch(r"advanced_models/(?:objects|inputs)/[0-9a-f]{64}\.json", name))


def _safe_path(path):
    path = Path(path).absolute()
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise StorageError("历史原件路径含符号链接，已停止。")
    return path


class FixedStateReader:
    """Reuse one bounded Git batch process and source indexes for a closure.

    Repeated references reuse exact immutable transport bytes.  Callers still
    verify the saved compressed and raw fingerprints on every logical read.
    """
    def __init__(self, repo, state_commit, *, cache_bytes=64 * 1024 * 1024):
        self.repo = _safe_path(repo)
        self.state_commit = state_commit
        self.cache_bytes = cache_bytes
        self._cache = OrderedDict()
        self._cached_bytes = 0
        self._process = None
        self._lock = threading.Lock()

    def _blob(self, commit, relative, *, missing=False):
        if any(ord(c) < 32 for c in relative):
            raise StorageError("历史原件路径含控制字符，已停止。")
        key = (commit, relative)
        with self._lock:
            if key in self._cache:
                self._cache.move_to_end(key)
                return self._cache[key]
            if self._process is None or self._process.poll() is not None:
                self.close()
                try:
                    self._process = subprocess.Popen(
                        ["git", "-C", str(self.repo), "cat-file", "--batch"],
                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
                except OSError as exc:
                    raise StorageError("固定历史对象读取不可用，已停止。") from exc
            process = self._process
            expired = threading.Event()
            def expire():
                expired.set()
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
            timer = threading.Timer(180, expire)
            timer.start()
            try:
                process.stdin.write((commit + ":" + relative + "\n").encode())
                process.stdin.flush()
                header = process.stdout.readline(8192)
                if expired.is_set():
                    raise StorageError("固定历史对象读取超时，已停止。")
                if header.endswith(b" missing\n"):
                    if missing:
                        return None
                    raise StorageError("固定历史对象未保存，已停止。")
                fields = header.split()
                if (len(fields) != 3 or fields[1] != b"blob"
                        or not re.fullmatch(rb"[0-9a-f]{40}(?:[0-9a-f]{24})?", fields[0])):
                    raise StorageError("固定历史对象类型无效，已停止。")
                try:
                    size = int(fields[2])
                except ValueError as exc:
                    raise StorageError("固定历史对象长度无效，已停止。") from exc
                if not 0 <= size <= MAX_FILE_BYTES:
                    self.close()
                    raise StorageError("固定历史原件超过单文件安全上限，已停止。")
                raw = process.stdout.read(size)
                terminator = process.stdout.read(1)
                if expired.is_set() or len(raw) != size or terminator != b"\n":
                    raise StorageError("固定历史对象长度不符或读取超时，已停止。")
                if size <= self.cache_bytes:
                    while self._cache and self._cached_bytes + size > self.cache_bytes:
                        _, previous = self._cache.popitem(last=False)
                        self._cached_bytes -= len(previous)
                    self._cache[key] = raw
                    self._cached_bytes += size
                return raw
            except (OSError, BrokenPipeError) as exc:
                raise StorageError("固定历史对象读取失败，已停止。") from exc
            finally:
                timer.cancel()

    def read(self, logical_path):
        token = _READER_SCOPE.set(self)
        try:
            return read_state_bytes(self.repo, self.state_commit, logical_path)
        finally:
            _READER_SCOPE.reset(token)

    def root_bytes(self, relative, *, missing=False):
        """Exact non-runtime transport/metadata bytes from this saved commit."""
        token = _READER_SCOPE.set(self)
        try:
            return _git_bytes(self.repo, self.state_commit, _relative(relative), missing=missing)
        finally:
            _READER_SCOPE.reset(token)

    def validate_manifest(self):
        """Validate this state's legacy transport index once, even for hot refs."""
        token = _READER_SCOPE.set(self)
        try:
            digest, entries = _manifest_index(self.repo, self.state_commit)
            if digest is None:
                raise StorageError("固定状态缺少运输原件清单，已停止。")
            return {"sha256": digest, "entry_count": len(entries)}
        finally:
            _READER_SCOPE.reset(token)

    def close(self):
        process, self._process = self._process, None
        if process is not None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            process.communicate()
        self._cache.clear()
        self._cached_bytes = 0

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def _reader(repo, commit):
    root = _safe_path(repo)
    scoped = _READER_SCOPE.get()
    if scoped is not None and scoped.repo == root:
        return scoped
    key = str(root)
    if key not in _READERS:
        if len(_READERS) >= 3:
            _, old = _READERS.popitem(last=False)
            old.close()
        _READERS[key] = FixedStateReader(root, commit)
    _READERS.move_to_end(key)
    return _READERS[key]


@atexit.register
def _close_readers():
    for reader in _READERS.values():
        reader.close()
    _READERS.clear()


def _validate_catalog(value):
    if (not isinstance(value, dict) or set(value) != {"schema_version", "entries"}
            or value["schema_version"] != SCHEMA_VERSION or not isinstance(value["entries"], list)):
        raise StorageError("历史原件目录无效，已停止。")
    result, parents = {}, set()
    for entry in value["entries"]:
        if not isinstance(entry, dict) or set(entry) != _FIELDS:
            raise StorageError("历史原件目录记录无效，已停止。")
        name = _relative(entry["path"])
        transport = _relative(entry["transport_path"])
        if (name == CATALOG_RELATIVE or name in result or name in parents
                or any(p.as_posix() in result for p in Path(name).parents if p.as_posix() != ".")
                or not isinstance(entry["state_commit"], str) or not _OID.fullmatch(entry["state_commit"])
                or any(type(entry[k]) is not int or not 0 <= entry[k] <= MAX_FILE_BYTES
                       for k in ("raw_bytes", "compressed_bytes"))
                or any(not isinstance(entry[k], str) or not _SHA.fullmatch(entry[k])
                       for k in ("raw_sha256", "blob_sha256"))):
            raise StorageError("历史原件目录路径、长度或指纹无效，已停止。")
        if entry["codec"] == "gzip-single-member":
            if (transport != "state_archives/" + entry["blob_sha256"] + ".json.gz"
                    or not isinstance(entry["manifest_sha256"], str)
                    or not _SHA.fullmatch(entry["manifest_sha256"])):
                raise StorageError("历史压缩原件目录无效，已停止。")
        elif entry["codec"] == "identity":
            if (transport != "runtime/" + name or entry["manifest_sha256"] is not None
                    or entry["raw_sha256"] != entry["blob_sha256"]
                    or entry["raw_bytes"] != entry["compressed_bytes"]):
                raise StorageError("历史直读原件目录无效，已停止。")
        else:
            raise StorageError("历史原件编码无效，已停止。")
        result[name] = entry
        parents.update(p.as_posix() for p in Path(name).parents if p.as_posix() != ".")
    return result


def _decode_catalog(raw, read_member):
    value = _manifest_json(raw)
    if not isinstance(value, dict) or value.get("schema_version") != INDEX_SCHEMA_VERSION:
        return _validate_catalog(value)
    if (set(value) != {"schema_version", "chunks", "entry_count"}
            or not isinstance(value["chunks"], list) or type(value["entry_count"]) is not int
            or value["entry_count"] < 0):
        raise StorageError("历史分块目录索引无效，已停止。")
    entries, chunks = {}, set()
    for ref in value["chunks"]:
        if (not isinstance(ref, dict) or set(ref) != {"path", "sha256", "bytes", "entry_count"}
                or not isinstance(ref["sha256"], str) or not _SHA.fullmatch(ref["sha256"])
                or ref["path"] != CATALOG_CHUNK_PREFIX + ref["sha256"] + ".json"
                or ref["path"] in chunks or type(ref["bytes"]) is not int
                or not 0 <= ref["bytes"] <= CATALOG_CHUNK_BYTES
                or type(ref["entry_count"]) is not int or ref["entry_count"] <= 0):
            raise StorageError("历史分块目录引用无效，已停止。")
        body = read_member(ref["path"])
        if len(body) != ref["bytes"] or _sha(body) != ref["sha256"]:
            raise StorageError("历史分块目录长度或指纹不符，已停止。")
        members = _validate_catalog(_manifest_json(body))
        if len(members) != ref["entry_count"] or set(members) & set(entries):
            raise StorageError("历史分块目录条目重复或数量不符，已停止。")
        entries.update(members)
        chunks.add(ref["path"])
    if len(entries) != value["entry_count"]:
        raise StorageError("历史分块目录总数不符，已停止。")
    return entries


def _catalog_unscoped(runtime, *, use_cache=True):
    runtime = _safe_path(runtime)
    path = runtime / CATALOG_RELATIVE
    if not path.exists():
        return {}
    _safe_path(path)
    stat = path.stat()
    if not path.is_file() or stat.st_size > MAX_FILE_BYTES:
        raise StorageError("历史原件目录大小或类型无效，已停止。")
    offline_identity = None
    for parent in (runtime, *runtime.parents):
        access = parent / "model_access_manifest.json"
        if access.is_file() and not (parent / ".git").exists():
            a = access.stat()
            offline_identity = (str(access), a.st_mtime_ns, a.st_ctime_ns, a.st_size)
            break
    key = (str(path), stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size, offline_identity)
    cached = _CATALOG_CACHE.get(key) if use_cache else None
    if cached is not None:
        entries, signatures = cached
        if all((runtime / name).is_file() and not (runtime / name).is_symlink()
               and ((runtime / name).stat().st_size, (runtime / name).stat().st_mtime_ns,
                    (runtime / name).stat().st_ctime_ns) == signature
               for name, signature in signatures.items()):
            return entries
    raw = path.read_bytes()
    # Connector materializations bind the local catalog too; a modified index
    # must never redirect an otherwise valid selected ancestor original.
    for parent in (runtime, *runtime.parents):
        access = parent / "model_access_manifest.json"
        if access.is_file() and not (parent / ".git").exists():
            document = _manifest_json(access.read_bytes())
            if runtime == parent / "runtime":
                verified = _git_bytes(parent, document.get("state_commit"), "runtime/" + CATALOG_RELATIVE)
                if verified != raw:
                    raise StorageError("历史离线目录与固定来源不符，已停止。")
            break
    _scan_secret(raw)
    signatures = {}
    def member(name):
        child = _safe_path(runtime / name)
        stat = child.stat()
        if not child.is_file() or stat.st_size > CATALOG_CHUNK_BYTES:
            raise StorageError("历史分块目录大小或类型无效，已停止。")
        signatures[name] = (stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
        return child.read_bytes()
    result = _decode_catalog(raw, member)
    _CATALOG_CACHE.clear()  # Never retain unbounded catalogs across temp states.
    _CATALOG_CACHE[key] = (result, signatures)
    return result


def _file_identity(path):
    """Identity of mutable local input; do not follow a replaced symlink."""
    _safe_path(path)
    try:
        value = path.stat()
    except FileNotFoundError:
        return None
    return (value.st_dev, value.st_ino, value.st_mode, value.st_size,
            value.st_mtime_ns, value.st_ctime_ns)


class _VerifiedCatalogReadSession:
    """One fixed catalog view, revalidated before it can return success.

    This caches only the fully decoded routing index. Original byte, transport,
    time and capacity checks still run on every original read/write. Local
    connector sources remain mutable inputs and are pinned separately.
    """
    def __init__(self, runtime):
        self.runtime = _safe_path(runtime)
        self.entries = None
        self.files = {}
        self.directories = {}
        self.git_markers = {}
        self.offline_repo = None
        self.source_repo = None
        self.failure = None

    def fail(self, reason):
        self.failure = self.failure or reason
        raise StorageError(reason)

    def pin(self, path, raw=None, *, limit=MAX_FILE_BYTES):
        path = _safe_path(path)
        before = _file_identity(path)
        if before is not None:
            if not stat_module.S_ISREG(before[2]) or before[3] > limit:
                self.fail("verified_catalog_session_invalid_file")
            raw = path.read_bytes() if raw is None else raw
            if _file_identity(path) != before or len(raw) != before[3]:
                self.fail("verified_catalog_session_input_changed")
        elif raw is not None:
            self.fail("verified_catalog_session_input_changed")
        record = (before, _sha(raw) if raw is not None else None)
        if path in self.files and self.files[path] != record:
            self.fail("verified_catalog_session_input_changed")
        self.files[path] = record
        return raw

    def pin_offline(self, repo, path, raw=None):
        if _safe_path(repo) != self.offline_repo:
            self.fail("verified_catalog_session_cross_runtime")
        return self.pin(path, raw)

    def check_repo_binding(self, repo):
        if repo != self.source_repo:
            self.fail("verified_catalog_session_cross_runtime")
        git = repo / ".git"
        current = _file_identity(git)
        if (current[:3] if current is not None else None) != self.git_markers[git]:
            self.fail("verified_catalog_session_binding_changed")
        if git in self.files and current != self.files[git][0]:
            self.fail("verified_catalog_session_binding_changed")
        access = repo / "model_access_manifest.json"
        if _file_identity(access) != self.files[access][0]:
            self.fail("verified_catalog_session_binding_changed")

    def verify(self, *, contents):
        if self.failure:
            raise StorageError(self.failure)
        for path, identity in self.directories.items():
            current = _file_identity(path)
            if current is None or current[:3] != identity:
                self.fail("verified_catalog_session_root_changed")
        for path, identity in self.git_markers.items():
            current = _file_identity(path)
            # Git's object store may grow, but the binding cannot move/appear.
            current = current[:3] if current is not None else None
            if current != identity:
                self.fail("verified_catalog_session_binding_changed")
        for path, (identity, digest) in self.files.items():
            if _file_identity(path) != identity:
                self.fail("verified_catalog_session_input_changed")
            if contents and identity is not None:
                raw = path.read_bytes()
                if _sha(raw) != digest or _file_identity(path) != identity:
                    self.fail("verified_catalog_session_input_changed")

    def start(self):
        for parent in (self.runtime, *self.runtime.parents):
            identity = _file_identity(parent)
            if identity is None or not stat_module.S_ISDIR(identity[2]):
                self.fail("verified_catalog_session_invalid_runtime")
            self.directories[parent] = identity[:3]
            git = parent / ".git"
            marker = _file_identity(git)
            self.git_markers[git] = marker[:3] if marker is not None else None
            if marker is not None and stat_module.S_ISREG(marker[2]):
                self.pin(git)
            access = parent / "model_access_manifest.json"
            raw = self.pin(access)
            if self.source_repo is None and (raw is not None or (marker is not None and
                    (stat_module.S_ISDIR(marker[2]) or stat_module.S_ISREG(marker[2])))):
                self.source_repo = parent
            if self.offline_repo is None and raw is not None and marker is None:
                self.offline_repo = parent
        raw = self.pin(self.runtime / CATALOG_RELATIVE)
        if raw is not None:
            value = _manifest_json(raw)
            if isinstance(value, dict) and value.get("schema_version") == INDEX_SCHEMA_VERSION:
                if not isinstance(value.get("chunks"), list):
                    self.fail("verified_catalog_session_invalid_chunk")
                for ref in value.get("chunks", []):
                    if (not isinstance(ref, dict) or not isinstance(ref.get("sha256"), str)
                            or not _SHA.fullmatch(ref["sha256"])
                            or ref.get("path") != CATALOG_CHUNK_PREFIX + ref["sha256"] + ".json"):
                        self.fail("verified_catalog_session_invalid_chunk")
                    self.pin(self.runtime / ref["path"], limit=CATALOG_CHUNK_BYTES)
        # Always decode all entries/chunks and validate offline source binding,
        # even if another caller previously populated the process-wide cache.
        entries = _catalog_unscoped(self.runtime, use_cache=False)
        self.entries = MappingProxyType({name: MappingProxyType(dict(entry))
                                         for name, entry in entries.items()})
        self.verify(contents=False)

    def finish(self):
        self.verify(contents=True)
        entries = _catalog_unscoped(self.runtime, use_cache=False)
        if entries != self.entries:
            self.fail("verified_catalog_session_catalog_changed")
        self.verify(contents=False)

    def lookup(self, runtime):
        if self.failure:
            raise StorageError(self.failure)
        if _safe_path(runtime) != self.runtime or self.entries is None:
            self.fail("verified_catalog_session_cross_runtime")
        return self.entries


@contextmanager
def verified_catalog_read_session(runtime):
    """Pin one complete catalog for a read operation, with no nested scopes.

    A changed catalog, chunk, root or read offline original rejects the entire
    operation at exit. Catalog writes and cross-runtime reads are forbidden,
    including when their immediate exception is caught inside the scope.
    No cache survives this context, and original reads are never cached here.
    """
    previous = _CATALOG_SCOPE.get()
    if previous is not None:
        previous.fail("verified_catalog_session_nested")
    session = _VerifiedCatalogReadSession(runtime)
    token = _CATALOG_SCOPE.set(session)
    try:
        session.start()
        try:
            yield session
        except BaseException as exc:
            try:
                session.finish()
            except Exception as verification:
                # Preserve the actual business failure, while retaining the
                # separate failed final-integrity check for its caller.
                exc.add_note("catalog_session_final_check_failed: " + type(verification).__name__)
            raise
        else:
            session.finish()
    finally:
        _CATALOG_SCOPE.reset(token)


def _catalog(runtime):
    session = _CATALOG_SCOPE.get()
    return session.lookup(runtime) if session is not None else _catalog_unscoped(runtime)


def _write_catalog(runtime, entries):
    session = _CATALOG_SCOPE.get()
    if session is not None:
        session.fail("verified_catalog_session_write_forbidden")
    runtime = _safe_path(runtime)
    value = {"schema_version": SCHEMA_VERSION,
             "entries": [entries[name] for name in sorted(entries)]}
    _validate_catalog(value)
    raw = (json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
    used_chunks = set()
    if len(raw) > CATALOG_INLINE_BYTES:
        pending, pending_bytes, chunks = [], 0, []
        remaining = dict(entries)
        previous_index = runtime / CATALOG_RELATIVE
        if previous_index.is_file():
            previous = _manifest_json(previous_index.read_bytes())
            if isinstance(previous, dict) and previous.get("schema_version") == INDEX_SCHEMA_VERSION:
                # Keep already immutable chunks intact; new saved originals add
                # new chunks instead of rewriting the entire historical index.
                for ref in previous.get("chunks", []):
                    body = (runtime / ref["path"]).read_bytes()
                    if len(body) != ref["bytes"] or _sha(body) != ref["sha256"]:
                        raise StorageError("历史目录既有分块指纹不符，已停止。")
                    members = _validate_catalog(_manifest_json(body))
                    if all(remaining.get(name) == member for name, member in members.items()):
                        chunks.append(ref)
                        used_chunks.add(ref["path"])
                        for name in members:
                            remaining.pop(name)
        def flush():
            body = (json.dumps({"schema_version": SCHEMA_VERSION, "entries": pending},
                              ensure_ascii=False, separators=(",", ":")) + "\n").encode()
            if len(body) > CATALOG_CHUNK_BYTES:
                raise StorageError("历史目录分块超过安全上限，已停止。")
            digest = _sha(body)
            name = CATALOG_CHUNK_PREFIX + digest + ".json"
            target = _safe_path(runtime / name)
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                if target.read_bytes() != body:
                    raise StorageError("历史目录不可变分块冲突，已停止。")
            else:
                with target.open("xb") as handle:
                    handle.write(body)
            used_chunks.add(name)
            chunks.append({"path": name, "sha256": digest, "bytes": len(body), "entry_count": len(pending)})
        for name in sorted(remaining):
            entry = remaining[name]
            size = len(json.dumps(entry, ensure_ascii=False, separators=(",", ":")).encode()) + 1
            if pending and pending_bytes + size > CATALOG_CHUNK_BYTES - 1024:
                flush()
                pending, pending_bytes = [], 0
            pending.append(entry)
            pending_bytes += size
        if pending:
            flush()
        raw = (json.dumps({"schema_version": INDEX_SCHEMA_VERSION, "chunks": chunks,
                           "entry_count": len(entries)}, separators=(",", ":")) + "\n").encode()
    _scan_secret(raw)
    target = runtime / CATALOG_RELATIVE
    _safe_path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".tmp")
    temporary.write_bytes(raw)
    temporary.replace(target)
    # Only superseded derived index chunks are pruned. Original evidence stays
    # in its immutable state commit and is never deleted by this maintenance.
    directory = runtime / CATALOG_CHUNK_PREFIX
    if directory.is_dir():
        for child in directory.iterdir():
            _safe_path(child)
            if child.relative_to(runtime).as_posix() not in used_chunks:
                if not re.fullmatch(r"[0-9a-f]{64}\.json", child.name) or not child.is_file():
                    raise StorageError("历史目录分块目录含未知内容，已停止。")
                child.unlink()
    _CATALOG_CACHE.clear()


def _find_runtime(path, runtime=None):
    path = _safe_path(path)
    session = _CATALOG_SCOPE.get()
    if session is not None and (not path.is_relative_to(session.runtime)
            or (runtime is not None and _safe_path(runtime) != session.runtime)):
        session.fail("verified_catalog_session_cross_runtime")
    if runtime is not None:
        root = _safe_path(runtime)
        if not path.is_relative_to(root):
            raise StorageError("历史原件路径越出 runtime，已停止。")
        return root
    for parent in path.parents:
        if (parent / CATALOG_RELATIVE).is_file():
            return parent
    return None


def _repo(runtime):
    for parent in (_safe_path(runtime), *_safe_path(runtime).parents):
        git = parent / ".git"
        if git.is_dir() or git.is_file():
            return parent
        if (parent / "model_access_manifest.json").is_file():
            return parent
    raise StorageError("历史原件需要已授权的本地 Git 对象库，已停止。")


def _git(repo, args, *, allowed_missing=False):
    try:
        result = subprocess.run(["git", "-C", str(_safe_path(repo)), *args],
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False,
                                timeout=180, env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise StorageError("固定历史状态读取超时或不可用，已停止。") from exc
    if result.returncode:
        if allowed_missing:
            return None
        raise StorageError("固定历史状态原件无法读取，已停止。")
    return result.stdout


def _ensure_cold_fetch_disk(repo):
    """Refuse a new fixed-source network fetch before consuming save space."""
    try:
        available = shutil.disk_usage(_safe_path(repo)).free
    except OSError as exc:
        raise StorageError("无法核实历史冷源获取前的磁盘余量；未发起网络请求。") from exc
    if type(available) is not int or available < 0:
        raise StorageError("无法核实历史冷源获取前的磁盘余量；未发起网络请求。")
    if available < COLD_FETCH_REQUIRED_FREE_BYTES:
        raise StorageError("历史冷源获取前磁盘余量不足（可用 " + str(available)
                           + " 字节，保守要求 " + str(COLD_FETCH_REQUIRED_FREE_BYTES)
                           + " 字节）；未发起网络请求，未缓存来源提交。")
    return available - COLD_FETCH_SAVE_RESERVE_BYTES - COLD_FETCH_WORKSPACE_BYTES


def _file_limited_command(arguments, file_limit):
    """Set the inherited POSIX file bound in a fresh interpreter before exec.

    A separate launcher avoids preexec_fn in a process that also uses reader
    timeout threads. The receiving Git/index-pack process and its children
    inherit this hard limit, including bytes appended while repairing thin
    packs. No partial clone or refetch is needed to retain normal negotiation.
    """
    if os.name != "posix" or type(file_limit) is not int or file_limit <= 0:
        raise StorageError("无法启用历史冷源接收文件硬限；未发起网络请求。")
    launcher = ("import os,resource,sys\n"
                "limit=int(sys.argv[1])\n"
                "resource.setrlimit(resource.RLIMIT_FSIZE,(limit,limit))\n"
                "os.execvp(sys.argv[2],sys.argv[2:])\n")
    return [sys.executable, "-c", launcher, str(file_limit), *arguments]


def _fetch_cold_commit(repo, commit, pack_file_limit):
    arguments = ["git", "-C", str(_safe_path(repo)), "fetch", "--quiet", "--no-tags",
                 "--depth=1", "--keep", "--no-auto-maintenance", "--no-write-commit-graph",
                 "--no-recurse-submodules", "origin", commit]
    command = _file_limited_command(arguments, pack_file_limit)
    process = None
    try:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"}, start_new_session=True)
        process.communicate(timeout=180)
    except (OSError, subprocess.TimeoutExpired) as exc:
        if process is not None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.communicate()
        raise StorageError("历史冷源获取超时或不可用；未缓存来源提交。") from exc
    if process.returncode:
        raise StorageError("历史冷源获取失败（接收文件硬限、网络或权限）；未缓存来源提交。")


def _ensure_commit(repo, commit):
    if not isinstance(commit, str) or not _OID.fullmatch(commit):
        raise StorageError("历史原件状态提交无效，已停止。")
    key = (str(_safe_path(repo)), commit)
    if key in _VERIFIED_COMMITS:
        return
    if _git(repo, ["cat-file", "-e", commit + "^{commit}"], allowed_missing=True) is None:
        pack_file_limit = _ensure_cold_fetch_disk(repo)
        # Exact immutable SHA only; credentials remain in the existing origin.
        _fetch_cold_commit(repo, commit, pack_file_limit)
        _git(repo, ["cat-file", "-e", commit + "^{commit}"])
    _VERIFIED_COMMITS.add(key)


def _git_bytes(repo, commit, relative, *, missing=False):
    _relative(relative)
    repo = _safe_path(repo)
    session = _CATALOG_SCOPE.get()
    if session is not None:
        session.check_repo_binding(repo)
    access = repo / "model_access_manifest.json"
    if access.is_file() and not (repo / ".git").exists():
        # A selected connector bundle carries separately bound ancestor source
        # files. Never contact the network or invent Git membership offline.
        access_raw = access.read_bytes()
        if session is not None:
            session.pin_offline(repo, access, access_raw)
        document = _manifest_json(access_raw)
        if (document.get("status") != "files_materialized_not_model_verified"
                or not isinstance(document.get("files"), list)):
            raise StorageError("历史原件离线接入清单无效，已停止。")
        location = ("history_sources/" + commit + "/" + relative)
        candidates = [r for r in document["files"] if r.get("commit") == commit and
                      ((r.get("role") == "history" and r.get("path") == location
                        and r.get("source_path") == relative)
                       or (r.get("role") == "state" and r.get("path") == relative
                           and document.get("state_commit") == commit))]
        if len(candidates) != 1:
            if missing and not candidates:
                return None
            raise StorageError("历史原件缺少精确提交的离线来源，已停止。")
        record = candidates[0]
        path = _safe_path(repo / _relative(record["path"]))
        if not path.is_file() or path.stat().st_size > MAX_FILE_BYTES:
            raise StorageError("历史离线原件缺失或大小无效，已停止。")
        raw = path.read_bytes()
        git_blob = hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()
        if (len(raw) != record.get("bytes") or _sha(raw) != record.get("sha256")
                or git_blob != record.get("git_blob_sha")):
            raise StorageError("历史离线原件来源指纹不符，已停止。")
        if session is not None:
            session.pin_offline(repo, path, raw)
        return raw
    _ensure_commit(repo, commit)
    return _reader(repo, commit)._blob(commit, relative, missing=missing)


def _manifest_index(repo, commit, expected_sha=None):
    repo = _safe_path(repo)
    session = _CATALOG_SCOPE.get()
    if session is not None:
        session.check_repo_binding(repo)
    identity = None
    access = repo / "model_access_manifest.json"
    if access.is_file() and not (repo / ".git").exists():
        a = access.stat()
        # A local connector bundle is mutable input, unlike a Git object. Bind
        # cache identity to both access records and the actual source manifest.
        source = repo / ("state_storage.json" if _manifest_json(access.read_bytes()).get("state_commit") == commit
                         else "history_sources/" + commit + "/state_storage.json")
        session = _CATALOG_SCOPE.get()
        if session is not None:
            session.pin_offline(repo, access)
            session.pin_offline(repo, source)
        s = source.stat() if source.is_file() else None
        identity = (a.st_mtime_ns, a.st_ctime_ns, a.st_size,
                    (s.st_mtime_ns, s.st_ctime_ns, s.st_size) if s else None)
    key = (str(repo), commit, identity)
    if key not in _MANIFEST_CACHE:
        raw = _git_bytes(repo, commit, "state_storage.json", missing=True)
        if raw is None:
            return None, {}
        if expected_sha is not None and _sha(raw) != expected_sha:
            raise StorageError("历史原件来源清单指纹不符，已停止。")
        document = _manifest_json(raw)
        if (not isinstance(document, dict) or set(document) != {"schema_version", "codec", "logical_limit_bytes", "physical_limit_bytes", "entries"}
                or type(document.get("schema_version")) is not int or document["schema_version"] != 1
                or document.get("codec") != "gzip-single-member" or not _supported_limits(document)
                or not isinstance(document.get("entries"), list)):
            raise StorageError("历史原件来源清单无效，已停止。")
        indexed = {}
        for e in document["entries"]:
            if not isinstance(e, dict) or not isinstance(e.get("path"), str) or e["path"] in indexed:
                raise StorageError("历史原件来源清单路径重复或无效，已停止。")
            indexed[e["path"]] = e
        if len(_MANIFEST_CACHE) >= 2:
            _MANIFEST_CACHE.clear()
        _MANIFEST_CACHE[key] = (_sha(raw), indexed)
    result = _MANIFEST_CACHE[key]
    if expected_sha is not None and result[0] != expected_sha:
        raise StorageError("历史原件来源清单指纹不符，已停止。")
    return result


def _state_catalog(repo, commit):
    repo = _safe_path(repo)
    access = repo / "model_access_manifest.json"
    if access.is_file() and not (repo / ".git").exists():
        document = _manifest_json(access.read_bytes())
        if document.get("state_commit") == commit and (repo / "runtime" / CATALOG_RELATIVE).is_file():
            return _catalog(repo / "runtime")
        # Selected ancestor catalogs may be small; their record bytes are
        # rechecked rather than caching mutable offline input as immutable Git.
        raw = _git_bytes(repo, commit, "runtime/" + CATALOG_RELATIVE, missing=True)
        return _decode_catalog(raw, lambda name: _git_bytes(repo, commit, "runtime/" + name)) if raw else {}
    key = (str(repo), commit)
    if key not in _STATE_CATALOG_CACHE:
        raw = _git_bytes(repo, commit, "runtime/" + CATALOG_RELATIVE, missing=True)
        entries = _decode_catalog(raw, lambda name: _git_bytes(repo, commit, "runtime/" + name)) if raw else {}
        if len(_STATE_CATALOG_CACHE) >= 2:
            _STATE_CATALOG_CACHE.popitem(last=False)
        _STATE_CATALOG_CACHE[key] = entries
    _STATE_CATALOG_CACHE.move_to_end(key)
    return _STATE_CATALOG_CACHE[key]


def _entry_body(repo, entry, seen):
    commit = entry["state_commit"]
    if entry["codec"] == "gzip-single-member":
        _, index = _manifest_index(repo, commit, entry["manifest_sha256"])
        expected = {key: entry[key] for key in ("path", "raw_sha256", "raw_bytes", "blob_sha256", "compressed_bytes")}
        if index.get(entry["path"]) != expected:
            raise StorageError("历史原件与固定来源清单不符，已停止。")
        blob = _git_bytes(repo, commit, entry["transport_path"])
        if len(blob) != entry["compressed_bytes"] or _sha(blob) != entry["blob_sha256"]:
            raise StorageError("历史压缩原件长度或指纹不符，已停止。")
        raw = _expand(blob, entry["raw_bytes"])
    else:
        raw = _read_state_bytes(repo, commit, entry["path"], seen)
    if len(raw) != entry["raw_bytes"] or _sha(raw) != entry["raw_sha256"]:
        raise StorageError("历史恢复原件长度或指纹不符，已停止。")
    _manifest_json(raw)
    _scan_secret(raw)
    return raw


def _read_state_bytes(repo, commit, logical_path, seen):
    key = (commit, logical_path)
    if key in seen or len(seen) >= 128:
        raise StorageError("历史原件目录存在循环或链过长，已停止。")
    seen = {*seen, key}
    direct = _git_bytes(repo, commit, "runtime/" + logical_path, missing=True)
    if direct is not None:
        _scan_secret(direct)
        return direct
    manifest_sha, manifest_index = _manifest_index(repo, commit)
    if logical_path in manifest_index:
        e = manifest_index[logical_path]
        if set(e) != {"path", "raw_sha256", "raw_bytes", "blob_sha256", "compressed_bytes"}:
            raise StorageError("历史原件来源清单记录无效，已停止。")
        entry = {**e, "state_commit": commit, "codec": "gzip-single-member",
                 "transport_path": "state_archives/" + str(e["blob_sha256"]) + ".json.gz",
                 "manifest_sha256": manifest_sha}
        _validate_catalog({"schema_version": SCHEMA_VERSION, "entries": [entry]})
        return _entry_body(repo, entry, seen)
    entry = _state_catalog(repo, commit).get(logical_path)
    if entry is not None:
        return _entry_body(repo, entry, seen)
    raise FileNotFoundError("固定状态中未保存该逻辑原件。")


def read_state_bytes(repo, state_commit, logical_path):
    """Read one logical original from exactly this saved commit, no checkout."""
    return _read_state_bytes(repo, state_commit, _relative(logical_path), set())


def read_bytes(path, *, runtime=None):
    path = _safe_path(path)
    root = _find_runtime(path, runtime)
    entry = _catalog(root).get(path.relative_to(root).as_posix()) if root is not None else None
    if path.is_file():
        session = _CATALOG_SCOPE.get()
        # Hot inputs remain mutable even when routing is pinned. Read and
        # register the same bytes atomically with respect to file identity;
        # scope exit rejects any later change, including a change then revert.
        raw = session.pin(path) if session is not None else path.read_bytes()
        if entry is not None and (_sha(raw) != entry["raw_sha256"] or len(raw) != entry["raw_bytes"]):
            raise StorageError("活动原件与冻结历史目录冲突，已停止。")
        return raw
    if entry is None:
        raise FileNotFoundError(path)
    return _entry_body(_repo(root), entry, set())


def exists(path, *, runtime=None):
    path = _safe_path(path)
    root = _find_runtime(path, runtime)
    entry = _catalog(root).get(path.relative_to(root).as_posix()) if root is not None else None
    return path.is_file() or entry is not None


def iter_paths(runtime, prefix=""):
    """Union of physical and catalog file paths; never hide cold evidence."""
    runtime = _safe_path(runtime)
    if prefix:
        prefix = _relative(prefix).rstrip("/") + "/"
    names = set(_catalog(runtime))
    for path in runtime.rglob("*"):
        _safe_path(path)
        if path.is_file():
            names.add(path.relative_to(runtime).as_posix())
    for name in sorted(names):
        if not prefix or name.startswith(prefix):
            yield runtime / name


def _glob_match(parts, pattern):
    if not pattern:
        return not parts
    if pattern[0] == "**":
        return _glob_match(parts, pattern[1:]) or bool(parts) and _glob_match(parts[1:], pattern)
    return bool(parts) and fnmatch.fnmatchcase(parts[0], pattern[0]) and _glob_match(parts[1:], pattern[1:])


def glob_paths(directory, pattern):
    directory = _safe_path(directory)
    root = _find_runtime(directory / "placeholder")
    if root is None:
        return sorted(directory.glob(pattern))
    prefix = directory.relative_to(root).as_posix()
    return [p for p in iter_paths(root, "" if prefix == "." else prefix)
            if _glob_match(p.relative_to(directory).parts, Path(pattern).parts)]


def catalog_stats(runtime):
    runtime = _safe_path(runtime)
    entries = _catalog(runtime)
    blobs = {(e["state_commit"], e["transport_path"]): e["compressed_bytes"] for e in entries.values()}
    return {"historical_raw_bytes": sum(e["raw_bytes"] for e in entries.values()),
            "historical_only_raw_bytes": sum(e["raw_bytes"] for name, e in entries.items()
                                               if not (runtime / name).is_file()),
            "historical_file_count": len(entries),
            "historical_transport_bytes": sum(blobs.values()),
            "historical_source_commits": sorted({e["state_commit"] for e in entries.values()}),
            "historical_accounting": "immutable_git_originals_not_active_expanded_runtime"}


def is_catalog_member(name):
    name = _relative(name)
    return name == CATALOG_RELATIVE or bool(re.fullmatch(
        re.escape(CATALOG_CHUNK_PREFIX) + r"[0-9a-f]{64}\.json", name))


def _transport_entry(name, entry, commit, manifest_sha):
    return {**entry, "path": name, "state_commit": commit,
            "transport_path": "state_archives/" + entry["blob_sha256"] + ".json.gz",
            "codec": "gzip-single-member", "manifest_sha256": manifest_sha}


def restore_history_catalog(runtime, tree, state_commit, repo, manifest_entries):
    """Called only after the source transport has passed complete validation."""
    runtime, tree = _safe_path(runtime), _safe_path(tree)
    _ensure_commit(repo, state_commit)
    catalog_path = tree / "runtime" / CATALOG_RELATIVE
    entries = _decode_catalog(catalog_path.read_bytes(), lambda name: (tree / "runtime" / name).read_bytes()) if catalog_path.is_file() else {}
    manifest_raw = (tree / "state_storage.json").read_bytes() if (tree / "state_storage.json").is_file() else None
    # One source digest identifies every member. Hashing the full 56 MiB
    # manifest inside the 86k-entry loop would scan terabytes per restoration.
    manifest_sha = _sha(manifest_raw) if manifest_raw is not None else None
    cold = set(entries)
    for original in manifest_entries:
        name = original["path"]
        if select_cold_path(name):
            candidate = _transport_entry(name, original, state_commit, manifest_sha)
            if name in entries and entries[name]["raw_sha256"] != candidate["raw_sha256"]:
                raise StorageError("历史目录与已保存原件冲突，已停止。")
            entries.setdefault(name, candidate)
            cold.add(name)
    for path in (tree / "runtime" / "advanced_models").rglob("*.json"):
        name = path.relative_to(tree / "runtime").as_posix()
        if select_cold_path(name):
            raw = path.read_bytes()
            entries[name] = {"path": name, "state_commit": state_commit,
                "transport_path": "runtime/" + name, "codec": "identity",
                "raw_sha256": _sha(raw), "raw_bytes": len(raw), "blob_sha256": _sha(raw),
                "compressed_bytes": len(raw), "manifest_sha256": None}
            cold.add(name)
    if entries:
        _write_catalog(runtime, entries)
    return cold


def compact_existing_history(runtime, base_oid, repo):
    """Evict only exact bytes already durable at the fixed saved parent."""
    runtime = _safe_path(runtime)
    _ensure_commit(repo, base_oid)
    entries = dict(_catalog(runtime))
    manifest_raw = _git_bytes(repo, base_oid, "state_storage.json", missing=True)
    manifest_sha = _sha(manifest_raw) if manifest_raw is not None else None
    manifest = _manifest_json(manifest_raw) if manifest_raw is not None else {"entries": []}
    by_path = {e["path"]: e for e in manifest.get("entries", [])}
    previous_catalog_raw = _git_bytes(repo, base_oid, "runtime/" + CATALOG_RELATIVE, missing=True)
    previous_catalog = _decode_catalog(previous_catalog_raw,
        lambda name: _git_bytes(repo, base_oid, "runtime/" + name)) if previous_catalog_raw else {}
    evict = []
    for path in sorted((runtime / "advanced_models").rglob("*.json")):
        name = path.relative_to(runtime).as_posix()
        if not select_cold_path(name):
            continue
        _safe_path(path)
        raw = path.read_bytes()
        if name in by_path:
            original = by_path[name]
            if _sha(raw) != original["raw_sha256"] or len(raw) != original["raw_bytes"]:
                continue
            candidate = _transport_entry(name, original, base_oid, manifest_sha)
        elif name in previous_catalog:
            candidate = previous_catalog[name]
            if _sha(raw) != candidate["raw_sha256"] or len(raw) != candidate["raw_bytes"]:
                continue
        else:
            saved = _git_bytes(repo, base_oid, "runtime/" + name, missing=True)
            if saved is None or saved != raw:
                continue  # This round's unsaved output remains active.
            candidate = {"path": name, "state_commit": base_oid, "transport_path": "runtime/" + name,
                "codec": "identity", "raw_sha256": _sha(raw), "raw_bytes": len(raw),
                "blob_sha256": _sha(raw), "compressed_bytes": len(raw), "manifest_sha256": None}
        # Verify the complete compressed/original provenance before any eviction.
        if _entry_body(repo, candidate, set()) != raw:
            raise StorageError("历史原件归档前复核失败，已停止。")
        if name in entries and entries[name]["raw_sha256"] != candidate["raw_sha256"]:
            raise StorageError("历史不可变原件内容冲突，已停止。")
        entries.setdefault(name, candidate)
        evict.append(path)
    if entries:
        _write_catalog(runtime, entries)  # Commit the index before deleting hot copies.
    removed_bytes = 0
    for path in evict:
        removed_bytes += path.stat().st_size
        path.unlink()
    return {**catalog_stats(runtime), "compacted_file_count": len(evict),
            "compacted_active_bytes": removed_bytes, "base_state_commit": base_oid}
