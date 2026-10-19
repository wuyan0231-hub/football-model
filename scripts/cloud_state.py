"""Persist runtime state in a separate Git branch without touching source history.

Run ``load`` before the application and ``save`` afterwards. Both commands use
the existing checkout's origin credentials; neither prints credentials nor
embeds them in a URL. A remote state change after load causes save to fail.

The complete runtime tree is persisted, including the mutable venue registry
under ``registries/``, its content-addressed JSON snapshots and per-league audits.
Snapshot integrity is checked by the registry resolver and forecast reader;
these files receive the same path, type, size and credential checks as models.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from typing import Iterator

# Resolve the helper from this checked-out source, including direct script use.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from football_model import state_storage


STATE_BRANCH = "model-state"
STATE_REF = f"refs/heads/{STATE_BRANCH}"
_IDENTITY = {
    "GIT_AUTHOR_NAME": "Football Model Automation",
    "GIT_AUTHOR_EMAIL": "football-model@users.noreply.github.com",
    "GIT_COMMITTER_NAME": "Football Model Automation",
    "GIT_COMMITTER_EMAIL": "football-model@users.noreply.github.com",
}
_STATE_ROOT_FILES = {"runtime", "state_metadata.json", "README.md", ".git",
                     state_storage.MANIFEST_NAME, state_storage.ARCHIVE_DIR}
_ALLOWED_SUFFIXES = {".json", ".jsonl", ".csv", ".tsv", ".md", ".txt", ".npz", ".npy"}
_MAX_FILE_BYTES = 60 * 1024 * 1024
_MAX_TOTAL_BYTES = state_storage.MAX_PHYSICAL_BYTES
_MAX_LOGICAL_BYTES = state_storage.MAX_LOGICAL_BYTES
_DISK_RESERVE_BYTES = 512 * 1024 * 1024
_MIN_AVAILABLE_MEMORY_BYTES = 1024 * 1024 * 1024
_MAX_PUSH_PACK_BYTES = 1900 * 1024 * 1024
_PACK_TIMEOUT_SECONDS = 180


StateError = state_storage.StorageError


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ensure_disk_space(path: Path, additional_bytes: int) -> None:
    # This measures the filesystem actually used; unknown is not zero/free.
    if type(additional_bytes) is not int or additional_bytes < 0:
        raise StateError("状态磁盘需求无效，已停止。")
    try:
        available = shutil.disk_usage(path).free
    except OSError as exc:
        raise StateError("无法核实状态磁盘余量，已停止。") from exc
    if available < additional_bytes + _DISK_RESERVE_BYTES:
        raise StateError("状态磁盘余量不足，已停止；本次未覆盖远端状态。")


def _available_memory_bytes() -> int | None:
    available = None
    try:
        fields = dict(line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines())
        available = int(fields["MemAvailable"].split()[0]) * 1024
    except (OSError, ValueError, KeyError):
        try:
            available = os.sysconf("SC_AVPHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")
        except (OSError, ValueError, AttributeError):
            pass
    # On Linux containers the host reading alone can overstate our allowance.
    for limit_path, used_path in (("/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory.current"),
        ("/sys/fs/cgroup/memory/memory.limit_in_bytes", "/sys/fs/cgroup/memory/memory.usage_in_bytes")):
        try:
            limit_text = Path(limit_path).read_text().strip()
            if limit_text == "max":
                continue
            remaining = max(0, int(limit_text) - int(Path(used_path).read_text()))
            available = remaining if available is None else min(available, remaining)
        except (OSError, ValueError):
            continue
    return available


def _ensure_memory() -> None:
    available = _available_memory_bytes()
    if available is None or available < _MIN_AVAILABLE_MEMORY_BYTES:
        raise StateError("状态可用内存不足或无法核实，已停止；本次未覆盖远端状态。")


def _measure_push_pack(repo: Path, commit: str, previous: str | None) -> int:
    """Count new reachable Git bytes without materializing a second pack.

    No deltas/reuse and compression level 0 provide a conservative wire-size
    probe. Exclude only the exact remote state parent, never the source branch
    or arbitrary local refs. Missing history/objects fail closed.
    """
    if any(not re.fullmatch(r"[0-9a-f]{40,64}", value)
           for value in (commit, previous) if value is not None):
        raise StateError("状态推送提交无效，已停止。")
    _ensure_memory()
    command = ["git", "-C", str(repo), "pack-objects", "--revs", "--stdout",
               "--no-reuse-delta", "--no-reuse-object", "--window=0", "--threads=1", "--compression=0"]
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL)
    expired = threading.Event()
    def expire():
        expired.set()
        try:
            process.kill()
        except ProcessLookupError:
            pass
    timer = threading.Timer(_PACK_TIMEOUT_SECONDS, expire)
    total = 0
    timer.start()
    try:
        revisions = commit + "\n" + ("^" + previous + "\n" if previous else "")
        process.stdin.write(revisions.encode("ascii"))
        process.stdin.close()
        while chunk := process.stdout.read(1024 * 1024):
            total += len(chunk)
            if total > _MAX_PUSH_PACK_BYTES:
                raise StateError("状态新增 Git pack 超过安全单次推送大小，已停止；未推送。")
        if process.wait() or expired.is_set():
            raise StateError("无法核实状态新增 Git pack，已停止；未推送。")
        return total
    finally:
        timer.cancel()
        if process.poll() is None:
            process.kill()
        process.wait()
        process.stdout.close()
        if not process.stdin.closed:
            process.stdin.close()


def _git(repo: Path, *args: str, check: bool = True,
         stdin: str | None = None, identity: bool = False) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    if identity:
        env.update(_IDENTITY)
    result = subprocess.run(
        ["git", "-C", str(repo), *args], input=stdin,
        text=True, capture_output=True, env=env, timeout=180,
    )
    if check and result.returncode:
        # Git can echo a configured remote URL or credential errors. Do not
        # forward stdout/stderr. Authentication remains in checkout settings.
        raise StateError(f"Git 操作失败（退出码 {result.returncode}）。请检查仓库权限和网络；未覆盖远端状态。")
    return result


def _paths(repo: Path, runtime: Path) -> tuple[Path, Path, Path]:
    repo = repo.resolve()
    actual = Path(_git(repo, "rev-parse", "--show-toplevel").stdout.strip()).resolve()
    if repo != actual:
        raise StateError("请在项目仓库根目录运行状态命令。")
    current = _git(repo, "symbolic-ref", "--quiet", "--short", "HEAD", check=False)
    if current.stdout.strip() == STATE_BRANCH:
        raise StateError("请从程序分支运行，不能从 model-state 分支运行。")
    runtime = runtime if runtime.is_absolute() else repo / runtime
    # A fixed destination prevents a mistaken path from replacing source files.
    if runtime.is_symlink() or runtime.resolve() != repo / "runtime":
        raise StateError("状态目录必须是仓库根目录下的 runtime，且不能是符号链接。")
    git_dir = Path(_git(repo, "rev-parse", "--absolute-git-dir").stdout.strip())
    return repo, runtime, git_dir / "football-cloud-state.json"


def _remote_oid(repo: Path) -> str | None:
    result = _git(repo, "ls-remote", "--exit-code", "origin", STATE_REF, check=False)
    if result.returncode == 2:
        return None
    if result.returncode:
        raise StateError("无法读取远端状态分支。请检查 origin、认证及网络；未写入状态。")
    records = result.stdout.strip().splitlines()
    if len(records) != 1:
        raise StateError("远端状态分支响应异常，已停止。")
    oid, ref = records[0].split("\t", 1)
    if ref != STATE_REF or not re.fullmatch(r"[0-9a-f]{40,64}", oid):
        raise StateError("远端状态标识异常，已停止。")
    return oid


@contextmanager
def _worktree(repo: Path, oid: str, *, checkout: bool = True) -> Iterator[Path]:
    # This directory is independent of the source checkout. Cleanup is scoped
    # to this exact temporary directory; no glob or source reset is used.
    # Same filesystem as the checked-out repository, so resource checks cover
    # both the temporary transport and newly written Git objects.
    with tempfile.TemporaryDirectory(prefix="football-state-", dir=repo) as temp:
        path = Path(temp) / "checkout"
        command = ["worktree", "add", "--detach"]
        if not checkout:
            command.append("--no-checkout")
        _git(repo, *command, str(path), oid)
        try:
            if not checkout:
                # Keep HEAD as the real parent but start with a completely
                # empty isolated index. No old transport files are checked out
                # merely to be removed again. Source index is never touched.
                _git(path, "read-tree", "--empty")
            yield path
        finally:
            _git(repo, "worktree", "remove", "--force", str(path), check=False)


def _validate_runtime(runtime: Path, *, transport_observer=None) -> dict:
    state_storage.validate_runtime(runtime, max_file_bytes=_MAX_FILE_BYTES,
                                   max_logical_bytes=_MAX_LOGICAL_BYTES)
    estimate = state_storage.estimate_runtime(runtime, transport_observer=transport_observer)
    if estimate["persisted_bytes"] > _MAX_TOTAL_BYTES:
        raise StateError("状态预计超过安全物理保存大小，请检查缓存体积；未覆盖远端。")
    return estimate


def _validate_tree(tree: Path) -> dict:
    return state_storage.validate_storage_tree(tree, max_file_bytes=_MAX_FILE_BYTES,
                                       max_physical_bytes=_MAX_TOTAL_BYTES,
                                       max_logical_bytes=_MAX_LOGICAL_BYTES)


def _write_json(path: Path, content: dict) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(content, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _loaded_baseline(repo: Path, marker: Path) -> dict:
    if not marker.is_file():
        raise StateError("尚未成功 load；拒绝保存，以免覆盖已有状态。")
    baseline = json.loads(marker.read_text(encoding="utf-8"))
    oid = baseline.get("state_oid")
    if baseline.get("schema_version") != 1 or (
        oid is not None and not re.fullmatch(r"[0-9a-f]{40,64}", str(oid))
    ):
        raise StateError("本地状态基线损坏，拒绝保存。")
    if baseline.get("source_sha") != _git(repo, "rev-parse", "HEAD").stdout.strip():
        raise StateError("程序版本在运行中改变，拒绝混用版本保存。")
    return baseline


def preflight_state(repo: Path, runtime: Path = Path("runtime")) -> dict:
    """Measure the save peak without creating a checkout, commit or pack.

    The loaded parent already supplies most Git blobs. Reserve disk for the
    complete new temporary transport, only genuinely new Git payload objects,
    conservatively bounded tree/metadata objects, and the existing safety
    margin. Filesystem block rounding is included. The final pack probe and
    ordinary compare-and-swap push remain independent mandatory checks.
    """
    repo, runtime, marker = _paths(repo, runtime)
    baseline = _loaded_baseline(repo, marker)
    oid = baseline.get("state_oid")
    object_format = _git(repo, "rev-parse", "--show-object-format").stdout.strip()
    if object_format not in {"sha1", "sha256"}:
        raise StateError("无法核实 Git 对象格式，已停止。")
    old_blobs = set(_git(repo, "ls-tree", "-r", "--format=%(objectname)", oid).stdout.split()) if oid else set()
    try:
        filesystem = os.statvfs(repo)
        block = max(512, int(filesystem.f_frsize or filesystem.f_bsize))
    except (AttributeError, OSError, ValueError):
        block = 4096
    def allocated(size):
        return ((size + block - 1) // block) * block if size else 0
    def git_object_bound(size):
        # zlib's stored-block bound plus a generous Git blob/tree header.
        size += 64
        return allocated(size + (size // 16383 + 1) * 5 + 16)
    seen_objects, reused_objects, tree_edges, directories = set(), set(), set(), {""}
    transport_disk = new_git_disk = 0
    def tree_path(name):
        parts = name.split("/")
        parent = ""
        for part in parts:
            tree_edges.add((parent, part))
            parent = parent + "/" + part if parent else part
        directories.update("/".join(parts[:i]) for i in range(1, len(parts)))
    def observe(name, content):
        nonlocal transport_disk, new_git_disk
        transport_disk += allocated(len(content))
        tree_path(name)
        hasher = hashlib.new(object_format)
        hasher.update(f"blob {len(content)}\0".encode("ascii"))
        hasher.update(content)
        identity = hasher.hexdigest()
        if identity in old_blobs:
            reused_objects.add(identity)
        elif identity not in seen_objects:
            new_git_disk += git_object_bound(len(content))
        seen_objects.add(identity)
    estimate = _validate_runtime(runtime, transport_observer=observe)
    # Manifest and run metadata change on every save; do not grant them reuse
    # credit. The reserve includes state_metadata.json, README and an empty
    # runtime sentinel when needed.
    manifest = estimate["manifest_bytes"]
    metadata = state_storage.METADATA_RESERVE_BYTES
    transport_disk += allocated(manifest) + metadata
    new_git_disk += git_object_bound(manifest) + metadata
    for name in (state_storage.MANIFEST_NAME, "state_metadata.json", "README.md", "runtime/state_manifest.json"):
        tree_path(name)
    tree_sizes = {}
    object_id_size = 20 if object_format == "sha1" else 32
    for parent, name in tree_edges:
        tree_sizes[parent] = tree_sizes.get(parent, 0) + len(name.encode("utf-8")) + object_id_size + 10
    new_git_disk += sum(git_object_bound(size) for size in tree_sizes.values()) + block
    transport_disk += len(directories) * block
    additional = transport_disk + new_git_disk
    try:
        available = shutil.disk_usage(repo).free
    except OSError as exc:
        raise StateError("无法核实状态磁盘余量，已停止。") from exc
    return {"schema_version": 1, "state_oid": oid,
        "runtime_logical_bytes": estimate["logical_bytes"], "runtime_raw_bytes": estimate["raw_bytes"],
        "historical_raw_bytes": estimate["historical_raw_bytes"],
        "historical_file_count": estimate["historical_file_count"],
        "total_original_logical_bytes": estimate["total_original_logical_bytes"],
        "transport_bytes": estimate["persisted_bytes"], "temporary_transport_disk_bytes": transport_disk,
        "new_git_object_bytes": new_git_disk, "reused_git_blob_count": len(reused_objects),
        "transport_payload_count": estimate["transport_payload_count"],
        "additional_bytes": additional, "disk_reserve_bytes": _DISK_RESERVE_BYTES,
        "required_free_bytes": additional + _DISK_RESERVE_BYTES, "available_disk_bytes": available,
        "can_save": available >= additional + _DISK_RESERVE_BYTES,
        "estimation_method": "new_transport_plus_parent_deduplicated_git_objects_with_block_rounding"}


def prepare_state(repo: Path, runtime: Path = Path("runtime")) -> dict:
    """Compact only immutable originals already verified in the saved parent.

    This is a local, reversible working-set operation. No commit is created and
    no remote state is changed. New unsaved research output remains hot until a
    successful checkpoint gives it a real fixed ancestor to reference.
    """
    repo, runtime, marker = _paths(repo, runtime)
    baseline = _loaded_baseline(repo, marker)
    from football_model.history_archive import catalog_stats, compact_existing_history
    if baseline.get("state_oid") is None:
        return {**catalog_stats(runtime), "compacted_file_count": 0,
                "compacted_active_bytes": 0, "base_state_commit": None}
    return compact_existing_history(runtime, baseline["state_oid"], repo)


def _restore_disk_budget(tree: Path, stats: dict) -> dict:
    """Bound only the active files to be restored, plus the new cold catalog.

    Cold originals were already validated in the source transport. They stay
    there instead of being expanded into runtime, so reserving their complete
    raw total would incorrectly reject a viable tiered restore.
    """
    from football_model.history_archive import _catalog, is_catalog_member, select_cold_path
    cold = set(_catalog(tree / "runtime"))
    block = 4096
    try:
        filesystem = os.statvfs(tree)
        block = max(512, int(filesystem.f_frsize or filesystem.f_bsize))
    except (AttributeError, OSError, ValueError):
        pass
    def allocated(size):
        return ((size + block - 1) // block) * block if size else 0
    active_bytes = allocated_bytes = files = 0
    directories = {""}
    def active(name, size):
        nonlocal active_bytes, allocated_bytes, files
        active_bytes += size
        allocated_bytes += allocated(size)
        files += 1
        parts = name.split("/")
        directories.update("/".join(parts[:i]) for i in range(1, len(parts)))
    for name, path in state_storage._files(tree / "runtime"):
        if select_cold_path(name):
            cold.add(name)
        elif not is_catalog_member(name):
            active(name, path.stat().st_size)
    for entry in stats["entries"]:
        if select_cold_path(entry["path"]):
            cold.add(entry["path"])
        else:
            active(entry["path"], entry["raw_bytes"])
    # Every catalog record has bounded hashes/OIDs/lengths and at most two
    # copies of the logical path. This intentionally generous allowance also
    # covers chunk indexes and one atomic chunk-write temporary file.
    catalog_budget = (sum(2048 + 3 * len(name.encode("utf-8")) for name in cold)
                      + 8 * 1024 * 1024) if cold else 0
    return {"active_restore_raw_bytes": active_bytes, "active_restore_file_count": files,
            "cold_restore_file_count": len(cold), "catalog_disk_budget_bytes": catalog_budget,
            "additional_bytes": allocated_bytes + len(directories) * block + catalog_budget}


def load_state(repo: Path, runtime: Path = Path("runtime")) -> str | None:
    repo, runtime, marker = _paths(repo, runtime)
    # A failed load must never leave a marker that could authorize a save.
    marker.unlink(missing_ok=True)
    if runtime.exists() and (not runtime.is_dir() or any(runtime.iterdir())):
        raise StateError("runtime 已有内容。请使用全新工作目录恢复，避免覆盖本地结果。")
    oid = _remote_oid(repo)
    if oid is not None:
        _ensure_memory()
        # Fetch only the current complete snapshot, not an ever-growing history
        # of retained state commits. Remote old commits remain untouched.
        _ensure_disk_space(repo, 2 * _MAX_TOTAL_BYTES)
        _git(repo, "fetch", "--depth=1", "--no-tags", "origin", STATE_REF)
        oid = _git(repo, "rev-parse", "FETCH_HEAD").stdout.strip()
        with _worktree(repo, oid) as tree:
            def before_restore(stats):
                restore_resources = _restore_disk_budget(tree, stats)
                print("MODEL_STATE_RESTORE_PREFLIGHT=" + json.dumps(restore_resources, ensure_ascii=False, sort_keys=True))
                _ensure_disk_space(repo, restore_resources["additional_bytes"])
            # Validation precedes all destination writes. A partial local copy
            # still has no marker and therefore cannot be pushed by save.
            # Copy the whole tree: restoring only models/reports would lose
            # venue evidence and the immutable snapshot used by old forecasts.
            state_storage.restore_runtime(tree, runtime, max_file_bytes=_MAX_FILE_BYTES,
                                          max_physical_bytes=_MAX_TOTAL_BYTES,
                                          max_logical_bytes=_MAX_LOGICAL_BYTES,
                                          cold_state_commit=oid, cold_repo=repo,
                                          before_restore=before_restore)
    else:
        runtime.mkdir(exist_ok=True)
    source_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
    source_ref = os.environ.get("GITHUB_REF") or _git(
        repo, "symbolic-ref", "--quiet", "HEAD", check=False,
    ).stdout.strip() or "detached"
    _write_json(marker, {
        "schema_version": 1, "state_oid": oid, "source_sha": source_sha,
        "source_ref": source_ref, "loaded_at": _utc_now(),
    })
    return oid


def save_state(repo: Path, runtime: Path = Path("runtime")) -> str:
    repo, runtime, marker = _paths(repo, runtime)
    baseline = _loaded_baseline(repo, marker)
    oid = baseline.get("state_oid")
    _ensure_memory()
    compacted = prepare_state(repo, runtime)
    if compacted["compacted_file_count"]:
        print("MODEL_STATE_HISTORY_COMPACTED=" + json.dumps(compacted, ensure_ascii=False, sort_keys=True))
    resources = preflight_state(repo, runtime)
    print("MODEL_STATE_SAVE_PREFLIGHT=" + json.dumps(resources, ensure_ascii=False, sort_keys=True))
    _ensure_disk_space(repo, resources["additional_bytes"])
    if _remote_oid(repo) != oid:
        raise StateError("远端 model-state 已变化；本次结果未覆盖它。请从最新状态重新运行。")
    if oid is None:
        # Create a disconnected empty root commit, never an orphan checkout in
        # the user's source directory. Source branch/index remain untouched.
        empty_tree = _git(repo, "mktree", stdin="").stdout.strip()
        base = _git(repo, "commit-tree", empty_tree, "-m", "Initialize isolated model state", identity=True).stdout.strip()
    else:
        base = oid
    with _worktree(repo, base, checkout=False) as tree:
        # Registries, digest-named venue snapshots and audits are runtime state
        # too. Do not replace this with the shorter Actions artifact path list.
        storage_stats = state_storage.pack_runtime(runtime, tree, max_file_bytes=_MAX_FILE_BYTES,
                                                   max_logical_bytes=_MAX_LOGICAL_BYTES)
        # Empty directories do not survive Git. A tiny manifest guarantees a
        # restorable runtime even if the first run found no accessible data.
        if not (tree / "runtime" / "state_manifest.json").exists():
            _write_json(tree / "runtime" / "state_manifest.json", {"schema_version": 1})
        metadata = {
            "schema_version": 1, "saved_at": _utc_now(),
            "source_sha": baseline["source_sha"], "source_ref": baseline["source_ref"],
            "previous_state_oid": oid,
            "github_run_id": os.environ.get("GITHUB_RUN_ID"),
            "github_run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT"),
            "state_storage": {"schema_version": state_storage.SCHEMA_VERSION,
                              "physical_limit_bytes": _MAX_TOTAL_BYTES,
                              "logical_limit_bytes": _MAX_LOGICAL_BYTES,
                              "logical_limit_scope": "active_expanded_runtime",
                              "raw_bytes": storage_stats["raw_bytes"],
                              **{k: resources[k] for k in ("historical_raw_bytes", "historical_file_count", "total_original_logical_bytes")},
                              "archived_file_count": storage_stats["archived_file_count"],
                              "archive_blob_count": storage_stats["archive_blob_count"]},
            "model_note": "运行记录不表示模型已通过预测质量验证；以 reports 中的具体状态为准。",
        }
        _write_json(tree / "state_metadata.json", metadata)
        (tree / "README.md").write_text(
            "# 自动训练状态\n\n此分支仅保存 runtime 和运行元信息；程序在主分支。\n"
            "每次成功保存创建普通 Git 提交，可从提交历史恢复旧模型、缓存与报告。\n"
            "请勿强制推送、合并到主分支或在此分支手工编辑。\n"
            "仓库应保持 Private；Actions 附件只是 14 天便捷副本。\n", encoding="utf-8",
        )
        _validate_tree(tree)  # Includes actual manifest and metadata bytes, before push.
        _git(tree, "add", "--", "runtime", "state_metadata.json", "README.md",
             state_storage.MANIFEST_NAME, state_storage.ARCHIVE_DIR)
        _git(tree, "-c", "commit.gpgsign=false", "commit", "-m", "Save football model runtime state", identity=True)
        commit = _git(tree, "rev-parse", "HEAD").stdout.strip()
        # Use source checkout for authenticated network commands. Its configured
        # checkout credential may be scoped to its git directory. A normal push
        # rejects a concurrent branch update; --force is deliberately absent.
        pack_bytes = _measure_push_pack(repo, commit, oid)
        print("MODEL_STATE_NEW_PACK_BYTES=" + str(pack_bytes))
        result = _git(repo, "-c", "pack.threads=1", "-c", "pack.windowMemory=64m",
                      "-c", "pack.window=0", "push", "--porcelain", "origin", f"{commit}:{STATE_REF}", check=False)
        if result.returncode:
            raise StateError("状态 push 失败（权限、网络或并发更新）。远端未被强制覆盖，请保留报告后重试。")
    baseline["state_oid"] = commit
    _write_json(marker, baseline)
    return commit


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="安全恢复/保存 model-state 分支")
    parser.add_argument("operation", choices=("load", "save", "check", "preflight", "prepare"))
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--runtime", type=Path, default=Path("runtime"))
    args = parser.parse_args(argv)
    try:
        if args.operation == "load":
            oid = load_state(args.repo, args.runtime)
            print("已恢复 model-state 状态。" if oid else "首次运行：远端尚无 model-state 状态。")
        elif args.operation == "save":
            print("MODEL_STATE_SAVED_COMMIT=" + save_state(args.repo, args.runtime))
            print("已保存到独立 model-state 分支，程序分支未改动。")
        elif args.operation == "prepare":
            print(json.dumps(prepare_state(args.repo, args.runtime), ensure_ascii=False, sort_keys=True))
        elif args.operation == "preflight":
            report = preflight_state(args.repo, args.runtime)
            print(json.dumps(report, ensure_ascii=False, sort_keys=True))
            return int(not report["can_save"])
        else:
            _, runtime, _ = _paths(args.repo, args.runtime)
            _validate_runtime(runtime)
            print("运行文件通过上传前检查。")
        return 0
    except (StateError, OSError, ValueError, subprocess.TimeoutExpired):
        # StateError text is handcrafted and safe; OS and parser details are
        # intentionally not echoed because they can contain sensitive paths.
        error = sys.exc_info()[1]
        print(str(error) if isinstance(error, StateError) else "状态操作失败；未声明保存成功，请检查网络、权限和文件完整性。", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
