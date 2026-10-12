"""Lossless transport storage; application runtime paths and bytes stay unchanged.

Only selected cold JSON is compressed in the temporary model-state worktree.
The next load validates the complete tree before restoring original bytes.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import zlib
from .storage_policy import (MAX_PHYSICAL_BYTES, MAX_LOGICAL_BYTES,
                             SUPPORTED_MANIFEST_LIMITS)

MAX_FILE_BYTES = 60 * 1024 * 1024
METADATA_RESERVE_BYTES = 256 * 1024
MANIFEST_NAME = "state_storage.json"
ARCHIVE_DIR = "state_archives"
SCHEMA_VERSION = 1
_SUFFIXES = {".json", ".jsonl", ".csv", ".tsv", ".md", ".txt", ".npz", ".npy"}
_SHA = re.compile(r"[0-9a-f]{64}")


def _supported_limits(document):
    logical, physical = document.get("logical_limit_bytes"), document.get("physical_limit_bytes")
    return (type(logical) is int and type(physical) is int
            and (logical, physical) in SUPPORTED_MANIFEST_LIMITS)


class StorageError(RuntimeError):
    """A safe error containing no file contents or credential values."""


def _sha(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _relative(value: str) -> str:
    if (not isinstance(value, str) or not value or "\\" in value or "\x00" in value
        or len(value.encode("utf-8")) > 4096
        or any(len(part.encode("utf-8")) > 255 for part in value.split("/"))):
        raise StorageError("状态路径无效，已停止。")
    path = PurePosixPath(value)
    if path.is_absolute() or path.as_posix() != value or any(
        p in ("", ".", "..") or p.startswith(".") or ":" in p for p in value.split("/")
    ):
        raise StorageError("状态路径或隐藏文件无效，已停止。")
    return value


def _secrets() -> list[bytes]:
    return [v.encode() for name in ("FOOTBALL_API_KEY", "GITHUB_TOKEN", "GH_TOKEN", "ACTIONS_RUNTIME_TOKEN")
            if len(v := os.environ.get(name, "")) >= 6]


def _scan_secret(content: bytes) -> None:
    if any(secret in content for secret in _secrets()):
        raise StorageError("状态文件疑似包含凭据，已停止；请检查程序输出。")


def _json(content: bytes):
    try:
        return json.loads(content)
    except (ValueError, UnicodeError) as exc:
        raise StorageError("状态压缩 JSON 内容无效，已停止。") from exc


def _manifest_json(content: bytes):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate")
            result[key] = value
        return result
    try:
        return json.loads(content, object_pairs_hook=unique)
    except (ValueError, UnicodeError) as exc:
        raise StorageError("状态压缩清单无效，已停止。") from exc


def _expand(content: bytes, limit: int) -> bytes:
    """One gzip member, no trailing data; bounded before allocating output."""
    try:
        stream = zlib.decompressobj(16 + zlib.MAX_WBITS)
        result = stream.decompress(content, limit + 1)
        if len(result) > limit or stream.unconsumed_tail:
            raise StorageError("状态解压内容超过安全大小，已停止。")
        if not stream.eof or stream.unused_data:
            raise StorageError("状态压缩文件无效，已停止。")
        return result
    except zlib.error as exc:
        raise StorageError("状态压缩文件无效，已停止。") from exc


def _files(root: Path):
    if root.is_symlink() or not root.is_dir():
        raise StorageError("runtime 必须是普通目录，不能为符号链接。")
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise StorageError("状态包含符号链接，已停止。")
        relative = _relative(path.relative_to(root).as_posix())
        if path.is_dir():
            continue
        if not path.is_file():
            raise StorageError("状态包含非常规文件，已停止。")
        yield relative, path


def validate_runtime(runtime: Path, *, max_file_bytes: int = MAX_FILE_BYTES,
                     max_logical_bytes: int = MAX_LOGICAL_BYTES) -> dict:
    total = expanded = count = 0
    for relative, path in _files(Path(runtime)):
        replay = bool(re.fullmatch(r"replay/bundles/[0-9a-f]{64}\.json\.gz", relative))
        if path.suffix.lower() not in _SUFFIXES and not replay:
            raise StorageError("runtime 包含不允许上传的文件类型，已停止。")
        size = path.stat().st_size
        total += size
        count += 1
        if size > max_file_bytes or total + expanded > max_logical_bytes:
            raise StorageError("状态超过安全逻辑大小，已停止。")
        content = path.read_bytes()
        if len(content) != size:
            raise StorageError("状态文件在读取时改变，已停止。")
        _scan_secret(content)
        if replay:
            plain = _expand(content, min(max_file_bytes, max_logical_bytes - total - expanded))
            _json(plain)
            _scan_secret(plain)
            expanded += len(plain)
        if total + expanded > max_logical_bytes:
            raise StorageError("状态解压内容超过安全逻辑大小，已停止。")
    return {"raw_bytes": total, "expanded_replay_bytes": expanded, "file_count": count}


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for child in value.values():
            yield from _strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from _strings(child)


def retained_inline_paths(runtime: Path) -> set[str]:
    """Current access/quote evidence closure, selected from references not mtime."""
    runtime = Path(runtime)
    retained: set[str] = set()
    queue = ["quotes/reports/latest.json", "reports/latest.json"]
    queue.extend(p.relative_to(runtime).as_posix() for p in (runtime / "models").glob("*.json"))
    index = runtime / "integration/index.json"
    if index.is_file() and not index.is_symlink():
        document = _json(index.read_bytes())
        if not isinstance(document, dict) or not isinstance(document.get("competitions", []), list):
            raise StorageError("状态模型索引无效，已停止。")
        for competition in document.get("competitions", []):
            key = competition.get("key")
            for field in ("latest_attempt", "latest_train_attempt", "latest_successful_forecast"):
                item = competition.get(field) or {}
                queue.extend(_strings(item))
                if isinstance(key, str) and isinstance(item.get("run_id"), str):
                    queue.append(f"validation/{key}/{item['run_id']}.json")
    while queue:
        name = queue.pop()
        if name.startswith("runtime/"):
            name = name[8:]
        try:
            name = _relative(name)
        except StorageError:
            continue  # Non-path prose and outside references are not evidence paths.
        path = runtime / name
        if name in retained or path.is_symlink() or not path.is_file():
            continue
        retained.add(name)
        if path.suffix == ".json":
            document = _json(path.read_bytes())
            queue.extend(_strings(document))
            if name.startswith("models/") and isinstance(document, dict):
                key = path.stem
                model_id = document.get("model_id", "")
                if isinstance(model_id, str) and model_id.startswith(key + "-"):
                    queue.append(f"validation/{key}/{model_id[len(key)+1:]}.json")
    return retained


def should_archive(relative_path: str, retained_inline=frozenset()) -> bool:
    name = _relative(relative_path)
    if name in retained_inline or not name.endswith(".json"):
        return False
    return (name.startswith(("quotes/raw/", "quotes/snapshots/", "validation/"))
            or (name.startswith(("shadow_models/", "total_shrink_shadow/")) and name.split("/")[1] != "reports"
                and bool(re.fullmatch(r"[0-9a-f]{64}\.json", name.split("/")[-1])))
            or (name.startswith(("feature_models/", "feature_snapshots/"))
                and bool(re.fullmatch(r"[0-9a-f]{64}\.json",name.split("/")[-1]))))


def storage_cost(relative_path: str, raw_bytes: bytes, retained_inline=frozenset()) -> int:
    """Conservative new persisted bytes, including manifest overhead, no dedup discount."""
    if should_archive(relative_path, retained_inline):
        return len(gzip.compress(raw_bytes, compresslevel=6, mtime=0)) + 1024 + 8 * len(relative_path.encode())
    return len(raw_bytes)


def estimate_runtime(runtime: Path) -> dict:
    runtime = Path(runtime)
    retained = retained_inline_paths(runtime)
    raw = persisted = count = archived = expanded = 0
    for name, path in _files(runtime):
        content = path.read_bytes()
        raw += len(content)
        persisted += storage_cost(name, content, retained)
        count += 1
        archived += should_archive(name, retained)
        if re.fullmatch(r"replay/bundles/[0-9a-f]{64}\.json\.gz", name):
            expanded += len(_expand(content, MAX_FILE_BYTES))
    return {"raw_bytes": raw, "persisted_bytes": persisted + METADATA_RESERVE_BYTES,
            "logical_bytes": raw + expanded, "expanded_replay_bytes": expanded,
            "file_count": count, "archived_file_count": archived, "retained_inline": sorted(retained)}


def pack_runtime(runtime: Path, tree: Path, *, max_file_bytes=MAX_FILE_BYTES,
                 max_logical_bytes=MAX_LOGICAL_BYTES) -> dict:
    """Build only in an empty temporary state tree; never rewrite business runtime."""
    runtime, tree = Path(runtime), Path(tree)
    stats = validate_runtime(runtime, max_file_bytes=max_file_bytes, max_logical_bytes=max_logical_bytes)
    for name in ("runtime", ARCHIVE_DIR, MANIFEST_NAME):
        if (tree / name).exists() or (tree / name).is_symlink():
            raise StorageError("状态传输目标已有内容，拒绝覆盖。")
    retained = retained_inline_paths(runtime)
    (tree / "runtime").mkdir()
    (tree / ARCHIVE_DIR).mkdir()
    entries = []
    for name, path in _files(runtime):
        content = path.read_bytes()
        if should_archive(name, retained):
            _json(content)
            blob = gzip.compress(content, compresslevel=6, mtime=0)
            digest = _sha(blob)
            target = tree / ARCHIVE_DIR / (digest + ".json.gz")
            if target.exists() and target.read_bytes() != blob:
                raise StorageError("状态压缩内容地址冲突，已停止。")
            if not target.exists():
                target.write_bytes(blob)
            entries.append({"path": name, "raw_sha256": _sha(content), "raw_bytes": len(content),
                            "blob_sha256": digest, "compressed_bytes": len(blob)})
        else:
            target = tree / "runtime" / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
    manifest = {"schema_version": SCHEMA_VERSION, "codec": "gzip-single-member",
                "logical_limit_bytes": MAX_LOGICAL_BYTES, "physical_limit_bytes": MAX_PHYSICAL_BYTES,
                "entries": entries}
    (tree / MANIFEST_NAME).write_text(json.dumps(manifest, ensure_ascii=False, separators=(",", ":")) + "\n")
    return {**stats, "archived_file_count": len(entries), "archive_blob_count": len({e['blob_sha256'] for e in entries})}


def validate_storage_tree(tree: Path, *, max_file_bytes=MAX_FILE_BYTES,
                          max_physical_bytes=MAX_PHYSICAL_BYTES,
                          max_logical_bytes=MAX_LOGICAL_BYTES) -> dict:
    """Validate every blob and logical destination before any restore writes."""
    tree = Path(tree)
    allowed = {"runtime", "README.md", "state_metadata.json", ".git", MANIFEST_NAME, ARCHIVE_DIR}
    if tree.is_symlink() or any(p.name not in allowed for p in tree.iterdir()):
        raise StorageError("model-state 分支含有预期之外的文件，已停止。")
    physical = 0
    for top in tree.iterdir():
        if top.name == ".git":
            continue
        if top.is_symlink():
            raise StorageError("状态包含符号链接，已停止。")
        paths = list(top.rglob("*")) if top.is_dir() else [top]
        for path in paths:
            if path.is_symlink():
                raise StorageError("状态包含符号链接，已停止。")
            if path.is_dir():
                continue
            if not path.is_file():
                raise StorageError("状态包含非常规文件，已停止。")
            _relative(path.relative_to(tree).as_posix())
            physical += path.stat().st_size
            if physical > max_physical_bytes:
                raise StorageError("状态超过安全物理保存大小，已停止。")
            if top.name not in ("runtime", ARCHIVE_DIR):
                _scan_secret(path.read_bytes())
    stats = validate_runtime(tree / "runtime", max_file_bytes=max_file_bytes, max_logical_bytes=max_logical_bytes)
    manifest_path = tree / MANIFEST_NAME
    archive = tree / ARCHIVE_DIR
    if not manifest_path.exists():
        if archive.exists():
            raise StorageError("状态压缩目录缺少清单，已停止。")
        return {**stats, "physical_bytes": physical, "entries": []}
    document = _manifest_json(manifest_path.read_bytes())
    if (not isinstance(document, dict) or set(document) != {"schema_version", "codec", "logical_limit_bytes", "physical_limit_bytes", "entries"}
        or type(document.get("schema_version")) is not int or document.get("schema_version") != SCHEMA_VERSION or document.get("codec") != "gzip-single-member"
        or not _supported_limits(document)
        or not isinstance(document.get("entries"), list)
        or (archive.exists() and not archive.is_dir())
        or (document.get("entries") and not archive.is_dir())):
        raise StorageError("状态压缩清单无效，已停止。")
    seen = set()
    prefixes = set()
    blobs = set()
    logical = stats["raw_bytes"] + stats["expanded_replay_bytes"]
    for entry in document["entries"]:
        if not isinstance(entry, dict) or set(entry) != {"path", "raw_sha256", "raw_bytes", "blob_sha256", "compressed_bytes"}:
            raise StorageError("状态压缩清单记录无效，已停止。")
        name = _relative(entry["path"])
        parents = [p.as_posix() for p in PurePosixPath(name).parents if p.as_posix() != "."]
        if (not should_archive(name) or name in seen or (tree / "runtime" / name).exists()
            or name in prefixes or any(p in seen for p in parents)
            or any(p.is_file() for p in (tree / "runtime" / name).parents if p != tree)):
            raise StorageError("状态压缩路径重复、覆盖或类型无效，已停止。")
        if any(type(entry[k]) is not int or not 0 <= entry[k] <= max_file_bytes for k in ("raw_bytes", "compressed_bytes")):
            raise StorageError("状态压缩长度超过安全大小，已停止。")
        if any(not isinstance(entry[k], str) or not _SHA.fullmatch(entry[k]) for k in ("raw_sha256", "blob_sha256")):
            raise StorageError("状态压缩指纹无效，已停止。")
        seen.add(name)
        prefixes.update(parents)
        logical += entry["raw_bytes"]
        if logical > max_logical_bytes:
            raise StorageError("状态超过安全逻辑大小，已停止。")
        blob_name = entry["blob_sha256"] + ".json.gz"
        path = archive / blob_name
        if not path.is_file() or path.stat().st_size != entry["compressed_bytes"]:
            raise StorageError("状态压缩原件缺失或长度不一致，已停止。")
        blob = path.read_bytes()
        if _sha(blob) != entry["blob_sha256"]:
            raise StorageError("状态压缩原件指纹不一致，已停止。")
        plain = _expand(blob, entry["raw_bytes"])
        if len(plain) != entry["raw_bytes"] or _sha(plain) != entry["raw_sha256"]:
            raise StorageError("状态解压原件指纹或长度不一致，已停止。")
        _json(plain)
        _scan_secret(plain)
        blobs.add(blob_name)
    archive_paths = list(archive.iterdir()) if archive.exists() else []
    if {p.name for p in archive_paths} != blobs or any(not p.is_file() for p in archive_paths):
        raise StorageError("状态压缩目录含未登记对象，已停止。")
    return {**stats, "logical_bytes": logical, "physical_bytes": physical, "entries": document["entries"]}


def restore_runtime(tree: Path, runtime: Path, **limits) -> dict:
    stats = validate_storage_tree(tree, **limits)
    runtime = Path(runtime)
    if runtime.is_symlink() or (runtime.exists() and (not runtime.is_dir() or any(runtime.iterdir()))):
        raise StorageError("runtime 已有内容，拒绝覆盖恢复。")
    shutil.copytree(Path(tree) / "runtime", runtime, dirs_exist_ok=True)
    for entry in stats["entries"]:
        target = runtime / entry["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() or target.is_symlink():
            raise StorageError("状态恢复目的路径已存在，拒绝覆盖。")
        blob = (Path(tree) / ARCHIVE_DIR / (entry["blob_sha256"] + ".json.gz")).read_bytes()
        target.write_bytes(_expand(blob, entry["raw_bytes"]))
    return stats


def read_archive_bytes(transport_dir: Path, logical_relative_path: str) -> bytes:
    """Verify one selected original against the real manifest, without other blobs.

    This reader supports bounded model-access materialization of historical
    parents. It does not authorize restoration or validate a whole state tree.
    """
    tree = Path(transport_dir)
    name = _relative(logical_relative_path)
    manifest_path = tree / MANIFEST_NAME
    archive = tree / ARCHIVE_DIR
    if tree.is_symlink() or manifest_path.is_symlink() or archive.is_symlink() or not manifest_path.is_file():
        raise StorageError("选定状态传输原件路径无效，已停止。")
    if manifest_path.stat().st_size > MAX_FILE_BYTES:
        raise StorageError("状态压缩清单超过安全大小，已停止。")
    document = _manifest_json(manifest_path.read_bytes())
    if (not isinstance(document, dict) or set(document) != {"schema_version", "codec", "logical_limit_bytes", "physical_limit_bytes", "entries"}
        or type(document.get("schema_version")) is not int or document.get("schema_version") != SCHEMA_VERSION or document.get("codec") != "gzip-single-member"
        or not _supported_limits(document)
        or not isinstance(document.get("entries"), list)):
        raise StorageError("状态压缩清单无效，已停止。")
    selected = None
    seen, prefixes = set(), set()
    logical = 0
    for entry in document["entries"]:
        if not isinstance(entry, dict) or set(entry) != {"path", "raw_sha256", "raw_bytes", "blob_sha256", "compressed_bytes"}:
            raise StorageError("状态压缩清单记录无效，已停止。")
        path = _relative(entry["path"])
        parents = [p.as_posix() for p in PurePosixPath(path).parents if p.as_posix() != "."]
        if not should_archive(path) or path in seen or path in prefixes or any(p in seen for p in parents):
            raise StorageError("状态压缩路径重复或类型无效，已停止。")
        seen.add(path)
        prefixes.update(parents)
        if any(type(entry[k]) is not int or not 0 <= entry[k] <= MAX_FILE_BYTES for k in ("raw_bytes", "compressed_bytes")):
            raise StorageError("状态压缩长度超过安全大小，已停止。")
        if any(not isinstance(entry[k], str) or not _SHA.fullmatch(entry[k]) for k in ("raw_sha256", "blob_sha256")):
            raise StorageError("状态压缩指纹无效，已停止。")
        logical += entry["raw_bytes"]
        if logical > MAX_LOGICAL_BYTES:
            raise StorageError("状态超过安全逻辑大小，已停止。")
        if path == name:
            selected = entry
    if selected is None:
        raise StorageError("选定逻辑路径未在真实压缩清单中登记，已停止。")
    blob_path = archive / (selected["blob_sha256"] + ".json.gz")
    if blob_path.is_symlink() or not blob_path.is_file() or blob_path.stat().st_size != selected["compressed_bytes"]:
        raise StorageError("选定压缩原件缺失或长度不一致，已停止。")
    blob = blob_path.read_bytes()
    if _sha(blob) != selected["blob_sha256"]:
        raise StorageError("选定压缩原件指纹不一致，已停止。")
    plain = _expand(blob, selected["raw_bytes"])
    if len(plain) != selected["raw_bytes"] or _sha(plain) != selected["raw_sha256"]:
        raise StorageError("选定解压原件指纹或长度不一致，已停止。")
    _json(plain)
    _scan_secret(plain)
    return plain
