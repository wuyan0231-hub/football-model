"""Persist runtime state in a separate Git branch without touching source history.

Run ``load`` before the application and ``save`` afterwards. Both commands use
the existing checkout's origin credentials; neither prints credentials nor
embeds them in a URL. A remote state change after load causes save to fail.

The complete runtime tree is persisted, including the mutable venue registry
at ``registries/nations_venues.json``, its content-addressed JSON snapshots under
``registries/nations_venues/``, and unknown-venue audits under ``audits/unl/``.
Snapshot integrity is checked by the registry resolver and forecast reader;
these files receive the same path, type, size and credential checks as models.
"""

from __future__ import annotations

import argparse
import gzip
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


StateError = state_storage.StorageError


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


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
def _worktree(repo: Path, oid: str) -> Iterator[Path]:
    # This directory is independent of the source checkout. Cleanup is scoped
    # to this exact temporary directory; no glob or source reset is used.
    with tempfile.TemporaryDirectory(prefix="football-state-") as temp:
        path = Path(temp) / "checkout"
        _git(repo, "worktree", "add", "--detach", str(path), oid)
        try:
            yield path
        finally:
            _git(repo, "worktree", "remove", "--force", str(path), check=False)


def _validate_runtime(runtime: Path) -> None:
    state_storage.validate_runtime(runtime, max_file_bytes=_MAX_FILE_BYTES,
                                   max_logical_bytes=_MAX_LOGICAL_BYTES)
    if state_storage.estimate_runtime(runtime)["persisted_bytes"] > _MAX_TOTAL_BYTES:
        raise StateError("状态预计超过安全物理保存大小，请检查缓存体积；未覆盖远端。")


def _validate_tree(tree: Path) -> None:
    state_storage.validate_storage_tree(tree, max_file_bytes=_MAX_FILE_BYTES,
                                       max_physical_bytes=_MAX_TOTAL_BYTES,
                                       max_logical_bytes=_MAX_LOGICAL_BYTES)


def _write_json(path: Path, content: dict) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(content, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def load_state(repo: Path, runtime: Path = Path("runtime")) -> str | None:
    repo, runtime, marker = _paths(repo, runtime)
    # A failed load must never leave a marker that could authorize a save.
    marker.unlink(missing_ok=True)
    if runtime.exists() and (not runtime.is_dir() or any(runtime.iterdir())):
        raise StateError("runtime 已有内容。请使用全新工作目录恢复，避免覆盖本地结果。")
    oid = _remote_oid(repo)
    if oid is not None:
        _git(repo, "fetch", "--no-tags", "origin", STATE_REF)
        oid = _git(repo, "rev-parse", "FETCH_HEAD").stdout.strip()
        with _worktree(repo, oid) as tree:
            _validate_tree(tree)
            # Validation precedes all destination writes. A partial local copy
            # still has no marker and therefore cannot be pushed by save.
            # Copy the whole tree: restoring only models/reports would lose
            # venue evidence and the immutable snapshot used by old forecasts.
            state_storage.restore_runtime(tree, runtime, max_file_bytes=_MAX_FILE_BYTES,
                                          max_physical_bytes=_MAX_TOTAL_BYTES,
                                          max_logical_bytes=_MAX_LOGICAL_BYTES)
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
    _validate_runtime(runtime)
    if _remote_oid(repo) != oid:
        raise StateError("远端 model-state 已变化；本次结果未覆盖它。请从最新状态重新运行。")
    if oid is None:
        # Create a disconnected empty root commit, never an orphan checkout in
        # the user's source directory. Source branch/index remain untouched.
        empty_tree = _git(repo, "mktree", stdin="").stdout.strip()
        base = _git(repo, "commit-tree", empty_tree, "-m", "Initialize isolated model state", identity=True).stdout.strip()
    else:
        base = oid
    with _worktree(repo, base) as tree:
        if oid is not None:
            _validate_tree(tree)
            shutil.rmtree(tree / "runtime")
            shutil.rmtree(tree / state_storage.ARCHIVE_DIR, ignore_errors=True)
            (tree / state_storage.MANIFEST_NAME).unlink(missing_ok=True)
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
                              "raw_bytes": storage_stats["raw_bytes"],
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
        result = _git(repo, "push", "--porcelain", "origin", f"{commit}:{STATE_REF}", check=False)
        if result.returncode:
            raise StateError("状态 push 失败（权限、网络或并发更新）。远端未被强制覆盖，请保留报告后重试。")
    baseline["state_oid"] = commit
    _write_json(marker, baseline)
    return commit


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="安全恢复/保存 model-state 分支")
    parser.add_argument("operation", choices=("load", "save", "check"))
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
