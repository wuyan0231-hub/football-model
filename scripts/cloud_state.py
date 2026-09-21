"""Persist runtime state in a separate Git branch without touching source history.

Run ``load`` before the application and ``save`` afterwards. Both commands use
the existing checkout's origin credentials; neither prints credentials nor
embeds them in a URL. A remote state change after load causes save to fail.
"""

from __future__ import annotations

import argparse
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


STATE_BRANCH = "model-state"
STATE_REF = f"refs/heads/{STATE_BRANCH}"
_IDENTITY = {
    "GIT_AUTHOR_NAME": "Football Model Automation",
    "GIT_AUTHOR_EMAIL": "football-model@users.noreply.github.com",
    "GIT_COMMITTER_NAME": "Football Model Automation",
    "GIT_COMMITTER_EMAIL": "football-model@users.noreply.github.com",
}
_STATE_ROOT_FILES = {"runtime", "state_metadata.json", "README.md", ".git"}
_ALLOWED_SUFFIXES = {".json", ".jsonl", ".csv", ".tsv", ".md", ".txt", ".npz", ".npy"}
_MAX_FILE_BYTES = 60 * 1024 * 1024
_MAX_TOTAL_BYTES = 500 * 1024 * 1024


class StateError(RuntimeError):
    """A safe, user-visible failure that never contains Git stderr or secrets."""


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
    if not runtime.exists():
        raise StateError("runtime 不存在，不能保存。")
    if runtime.is_symlink() or not runtime.is_dir():
        raise StateError("runtime 必须是普通目录。")
    known_secrets = [
        value.encode() for name in (
            "FOOTBALL_API_KEY", "GITHUB_TOKEN", "GH_TOKEN", "ACTIONS_RUNTIME_TOKEN",
        ) if len(value := os.environ.get(name, "")) >= 6
    ]
    total = 0
    for path in runtime.rglob("*"):
        if path.is_symlink():
            raise StateError("runtime 包含符号链接，已停止保存。")
        if any(part.startswith(".") for part in path.relative_to(runtime).parts):
            raise StateError("runtime 包含隐藏文件或目录，已停止保存。")
        if path.is_dir():
            continue
        if not path.is_file() or path.suffix.lower() not in _ALLOWED_SUFFIXES:
            raise StateError("runtime 包含不允许上传的文件类型，已停止保存。")
        size = path.stat().st_size
        total += size
        if size > _MAX_FILE_BYTES or total > _MAX_TOTAL_BYTES:
            raise StateError("状态超过安全保存大小，请检查缓存体积；未覆盖远端。")
        if known_secrets:
            content = path.read_bytes()
            if any(secret in content for secret in known_secrets):
                raise StateError("状态文件疑似包含凭据，已停止保存；请检查程序输出。")


def _validate_tree(tree: Path) -> None:
    if any(path.name not in _STATE_ROOT_FILES for path in tree.iterdir()):
        raise StateError("model-state 分支含有预期之外的文件，已停止以免损坏其他内容。")
    for name in ("README.md", "state_metadata.json"):
        if (tree / name).is_symlink():
            raise StateError("model-state 元信息不允许使用符号链接。")
    _validate_runtime(tree / "runtime")


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
            shutil.copytree(tree / "runtime", runtime, dirs_exist_ok=True)
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
        shutil.copytree(runtime, tree / "runtime")
        # Empty directories do not survive Git. A tiny manifest guarantees a
        # restorable runtime even if the first run found no accessible data.
        _write_json(tree / "runtime" / "state_manifest.json", {"schema_version": 1})
        metadata = {
            "schema_version": 1, "saved_at": _utc_now(),
            "source_sha": baseline["source_sha"], "source_ref": baseline["source_ref"],
            "previous_state_oid": oid,
            "github_run_id": os.environ.get("GITHUB_RUN_ID"),
            "github_run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT"),
            "model_note": "运行记录不表示模型已通过预测质量验证；以 reports 中的具体状态为准。",
        }
        _write_json(tree / "state_metadata.json", metadata)
        (tree / "README.md").write_text(
            "# 自动训练状态\n\n此分支仅保存 runtime 和运行元信息；程序在主分支。\n"
            "每次成功保存创建普通 Git 提交，可从提交历史恢复旧模型、缓存与报告。\n"
            "请勿强制推送、合并到主分支或在此分支手工编辑。\n"
            "仓库应保持 Private；Actions 附件只是 14 天便捷副本。\n", encoding="utf-8",
        )
        _git(tree, "add", "--", "runtime", "state_metadata.json", "README.md")
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
            save_state(args.repo, args.runtime)
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
