"""Verify an ambiguously timed-out ordinary state push without retrying it.

``run -- python -m football_model.advanced_research study ...`` executes that
unchanged module in a separate Python child.  Only its actual, unhandled,
exactly-whitelisted push TimeoutExpired is eligible for recovery.  A recovered
research child still failed and did not finish its command: its receipt says
so.  Workflow consumers must inspect that receipt rather than infer completion
from this launcher's zero recovery status.  ``save`` invokes the unchanged save.

No force push, second push, credential inspection, numerical change, or runtime
rewrite occurs in recovery.  A future ordinary save retains the old CAS check.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import runpy
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
VERSION = "cloud-state-save-checked-v1"
STATE_REF = "refs/heads/model-state"
OID = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")


class RecoveryError(ValueError):
    """The ambiguous save cannot be proved; do not advance its marker."""


def _now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _time(value):
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None or result.utcoffset() is None:
        raise RecoveryError("timezone_required")
    return result.astimezone(timezone.utc)


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _json(raw):
    return json.loads(raw)


def _exclusive(path, value):
    with Path(path).open("x", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
        handle.write("\n")


def _git(repo, *args):
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
        timeout=180, env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
    if result.returncode:
        raise RecoveryError("git_read_failed")
    return result.stdout


def _planned_push(command, repo):
    """Accept the exact existing cloud_state._git push, never a URL or force."""
    prefix = ["git", "-C", str(Path(repo).resolve()), "-c", "pack.threads=1",
        "-c", "pack.windowMemory=64m", "-c", "pack.window=0", "push", "--porcelain", "origin"]
    if not isinstance(command, list) or command[:-1] != prefix or len(command) != len(prefix) + 1:
        raise RecoveryError("not_exact_state_push_timeout")
    target = command[-1]
    if not isinstance(target, str) or not target.endswith(":" + STATE_REF):
        raise RecoveryError("not_exact_state_push_timeout")
    planned = target[:-(len(STATE_REF) + 1)]
    if not OID.fullmatch(planned):
        raise RecoveryError("invalid_planned_commit")
    return planned


def _study_args(command, repo, runtime, source):
    if len(command) < 4 or command[1:4] != ["-m", "football_model.advanced_research", "study"]:
        raise RecoveryError("only_advanced_study_module_is_supported")
    executable = shutil.which(command[0])
    if not executable or Path(executable).resolve() != Path(sys.executable).resolve():
        raise RecoveryError("study_requires_current_python")
    parser = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    parser.add_argument("--runtime", default="runtime")
    parser.add_argument("--league", required=True)
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--protocol", default="advanced-models-v1.json")
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--state-commit")
    parser.add_argument("--run-goalmodel", action="store_true")
    parser.add_argument("--checkpoint-state", action="store_true")
    parser.add_argument("--resume-only", action="store_true")
    parser.add_argument("--repo", default=str(repo))
    options = command[4:]
    flags = [v.split("=", 1)[0] for v in options if v.startswith("--")]
    if len(flags) != len(set(flags)):
        raise RecoveryError("duplicate_study_option")
    try:
        args = parser.parse_args(options)
    except SystemExit as exc:
        raise RecoveryError("unsupported_study_arguments") from exc
    path = lambda value: (Path(repo) / value).resolve()
    if (path(args.runtime) != runtime or path(args.repo) != repo
            or args.source_commit != source or not args.checkpoint_state
            or not re.fullmatch(r"[a-z][a-z0-9_]*", args.league) or args.league == "all"):
        raise RecoveryError("unbound_single_league_checkpoint_command")
    from football_model.advanced_research import validate_protocol, digest
    protocol_path = path(args.protocol)
    if (not protocol_path.is_relative_to(repo) or path(args.config) != repo / "config.json"
            or protocol_path.is_symlink()
            or (args.state_commit is not None and not OID.fullmatch(args.state_commit))):
        raise RecoveryError("study_paths_or_parent_invalid")
    protocol_raw = protocol_path.read_bytes()
    protocol = validate_protocol(_json(protocol_raw))
    return {"league": args.league, "protocol_path": str(protocol_path),
        "protocol_raw_sha256": _sha(protocol_raw), "protocol_hash": digest(protocol),
        "goalmodel_requested": args.run_goalmodel}


def _context(repo, runtime, operation, command):
    from football_model.advanced_research import implementation_fingerprint
    repo, runtime = Path(repo).resolve(), Path(runtime).resolve()
    if repo != ROOT or runtime != repo / "runtime" or runtime.is_symlink():
        raise RecoveryError("fixed_source_and_runtime_required")
    source = _git(repo, "rev-parse", "HEAD").decode().strip()
    gitdir = Path(_git(repo, "rev-parse", "--absolute-git-dir").decode().strip())
    marker = gitdir / "football-cloud-state.json"
    if marker.is_symlink():
        raise RecoveryError("marker_symlink")
    raw = marker.read_bytes()
    baseline = _json(raw)
    run_id, attempt = os.environ.get("GITHUB_RUN_ID"), os.environ.get("GITHUB_RUN_ATTEMPT")
    if (os.environ.get("GITHUB_REF") != "refs/heads/main" or os.environ.get("GITHUB_SHA") != source
            or not re.fullmatch(r"[1-9][0-9]*", str(run_id))
            or not re.fullmatch(r"[1-9][0-9]*", str(attempt))
            or baseline.get("schema_version") != 1 or baseline.get("source_sha") != source
            or baseline.get("source_ref") != "refs/heads/main"
            or (baseline.get("state_oid") is not None and not OID.fullmatch(str(baseline["state_oid"])))):
        raise RecoveryError("matching_loaded_main_run_required")
    value = {"schema_version": VERSION, "repo": str(repo), "runtime": str(runtime),
        "operation": operation, "command": command, "started_at": _now(), "source_sha": source,
        "github_run_id": run_id, "github_run_attempt": attempt, "source_ref": "refs/heads/main",
        "marker_path": str(marker), "before_marker": baseline, "before_marker_sha256": _sha(raw),
        "launcher_sha256": _sha(Path(__file__).read_bytes()),
        "implementation_fingerprint": implementation_fingerprint()}
    if operation == "run":
        value["study"] = _study_args(command, repo, runtime, source)
    return value


def _check_source(context):
    from football_model.advanced_research import implementation_fingerprint
    if (_git(context["repo"], "rev-parse", "HEAD").decode().strip() != context["source_sha"]
            or implementation_fingerprint() != context["implementation_fingerprint"]
            or _sha(Path(__file__).read_bytes()) != context["launcher_sha256"]):
        raise RecoveryError("source_or_original_marker_changed")


def _check_unchanged(context):
    _check_source(context)
    marker = Path(context["marker_path"])
    if marker.is_symlink() or _sha(marker.read_bytes()) != context["before_marker_sha256"]:
        raise RecoveryError("source_or_original_marker_changed")


def _checkpoint(context, planned):
    """Verify actual local and immutable planned bytes using old typed contracts."""
    from football_model.history_archive import FixedStateReader, read_bytes
    from football_model.advanced_durability import verify_reference_bytes
    from football_model.advanced_storage import load_artifact
    from football_model.research_checkpoints import _references, _digest, VERSION as CHECKPOINT_VERSION
    study, root = context["study"], Path(context["runtime"])
    if _sha(Path(study["protocol_path"]).read_bytes()) != study["protocol_raw_sha256"]:
        raise RecoveryError("study_protocol_changed")
    reader = FixedStateReader(context["repo"], planned)
    seen = set()

    def read(relative):
        path = PurePosixPath(relative)
        if (path.is_absolute() or ".." in path.parts or path.as_posix() != relative
                or not relative or any(ord(c) < 32 for c in relative)):
            raise RecoveryError("unsafe_checkpoint_reference")
        full = root / relative
        if any(p.is_symlink() for p in (full, *full.parents)) or not full.resolve().is_relative_to(root):
            raise RecoveryError("unsafe_checkpoint_reference")
        saved = reader.read(relative)
        if saved != read_bytes(full, runtime=root):
            raise RecoveryError("checkpoint_local_bytes_not_planned_bytes")
        return saved

    try:
        relative = f"advanced_models/checkpoints/{study['league']}/{study['protocol_hash']}/latest.json"
        pointer_raw = read(relative)
        pointer = _json(pointer_raw)
        ref = pointer["checkpoint_ref"]
        checkpoint_raw = read(ref["path"])
        if _sha(checkpoint_raw) != ref["sha256"]:
            raise RecoveryError("checkpoint_hash_mismatch")
        checkpoint = _json(checkpoint_raw)
        identity = checkpoint["input_identity"]
        result = checkpoint["result"]
        if (checkpoint.get("schema_version") != CHECKPOINT_VERSION
                or checkpoint.get("source_commit") != context["source_sha"]
                or checkpoint.get("league") != study["league"]
                or checkpoint.get("calculation_completed") is not True
                or identity.get("league") != study["league"]
                or identity.get("protocol_hash") != study["protocol_hash"]
                or identity.get("implementation_sha256") != context["implementation_fingerprint"]["sha256"]
                or identity.get("goalmodel_requested") is not study["goalmodel_requested"]
                or result.get("league") != study["league"]
                or result.get("status") != "completed_development"
                or result.get("calculation_completed") is not True):
            raise RecoveryError("checkpoint_identity_or_calculation_mismatch")
        if not _time(context["started_at"]) <= _time(checkpoint["calculation_generated_at"]) <= _time(context["timeout_captured_at"]):
            raise RecoveryError("checkpoint_calculation_outside_command")
        pending = list(_references(checkpoint))
        while pending:
            item = pending.pop()
            key = _digest(item)
            if key in seen:
                continue
            seen.add(key)
            if len(seen) > 1000000:
                raise RecoveryError("checkpoint_closure_exceeds_bound")
            raw = read(item["path"])
            decoded = verify_reference_bytes(item["path"], item, raw)
            pending.extend(_references(decoded["value"]))
        report = load_artifact(root, checkpoint["report_ref"])
        if (report.get("league") != study["league"] or report.get("protocol_hash") != study["protocol_hash"]
                or report.get("source_commit") != context["source_sha"]
                or report.get("implementation_fingerprint") != context["implementation_fingerprint"]
                or report.get("goalmodel_requested") is not study["goalmodel_requested"]
                or report.get("generated_at") != checkpoint["calculation_generated_at"]):
            raise RecoveryError("checkpoint_report_identity_mismatch")
        return {"league": study["league"], "protocol_hash": study["protocol_hash"],
            "checkpoint_ref": ref, "pointer_sha256": _sha(pointer_raw),
            "nested_references_verified": len(seen), "calculation_completed": True,
            "research_command_completed": False, "prospective_qualified": False}
    finally:
        reader.close()


def recover(context, event):
    """Prove exactly one already accepted save and advance only state_oid."""
    from scripts import cloud_state
    planned = _planned_push(event["timeout_command"], context["repo"])
    if (event.get("error_type") != "TimeoutExpired" or event.get("timeout_seconds") != 180
            or event.get("planned_commit") != planned):
        raise RecoveryError("unbound_timeout_event")
    context = {**context, "timeout_captured_at": event["captured_at"]}
    _check_unchanged(context)
    parent = context["before_marker"].get("state_oid")
    if parent is None:
        raise RecoveryError("initial_empty_state_recovery_not_supported")
    lineage = _git(context["repo"], "rev-list", "--parents", "-n", "1", planned).decode().strip().split()
    if lineage != [planned, parent]:
        raise RecoveryError("planned_commit_is_not_exact_child_of_loaded_state")
    metadata_raw = _git(context["repo"], "show", f"{planned}:state_metadata.json")
    metadata = _json(metadata_raw)
    expected = {"schema_version": 1, "source_sha": context["source_sha"],
        "source_ref": context["source_ref"], "previous_state_oid": parent,
        "github_run_id": context["github_run_id"], "github_run_attempt": context["github_run_attempt"]}
    if any(metadata.get(key) != value for key, value in expected.items()):
        raise RecoveryError("planned_metadata_does_not_match_original_attempt")
    if not _time(context["started_at"]) <= _time(metadata["saved_at"]) <= _time(event["captured_at"]):
        raise RecoveryError("planned_metadata_outside_command")
    first = cloud_state._remote_oid(Path(context["repo"]))
    if first != planned:
        raise RecoveryError("remote_does_not_equal_planned_commit")
    # The old worktree helper checks out the bounded transport tree.  Preserve
    # its disk reserve before creating that additional local copy.
    cloud_state._ensure_disk_space(Path(context["repo"]), cloud_state._MAX_TOTAL_BYTES)
    with cloud_state._worktree(Path(context["repo"]), planned) as tree:
        stats = cloud_state._validate_tree(tree)
    checkpoint = _checkpoint(context, planned) if context["operation"] == "run" else None
    second = cloud_state._remote_oid(Path(context["repo"]))
    if second != planned:
        raise RecoveryError("remote_changed_during_recovery")
    _check_unchanged(context)
    marker = Path(context["marker_path"])
    updated = {**context["before_marker"], "state_oid": planned}
    cloud_state._write_json(marker, updated)
    if _json(marker.read_bytes()) != updated:
        raise RecoveryError("marker_write_not_verified")
    return {"status": "push_timeout_remote_save_verified", "planned_commit": planned,
        "remote_observations": [first, second], "metadata_sha256": _sha(metadata_raw),
        "tree_oid": _git(context["repo"], "rev-parse", f"{planned}^{{tree}}").decode().strip(),
        "storage_validation": stats, "checkpoint": checkpoint,
        "post_marker_sha256": _sha(marker.read_bytes()), "recovered_at": _now(),
        "marker_only_field_changed": "state_oid", "push_retried": False}


def _child(context_path, event_path):
    raw = Path(context_path).read_bytes()
    context = _json(raw)
    event = {"schema_version": VERSION, "context_sha256": _sha(raw)}
    code = 1
    try:
        os.chdir(context["repo"])
        if context["operation"] == "save":
            from scripts import cloud_state
            commit = cloud_state.save_state(Path(context["repo"]), Path(context["runtime"]))
            print("MODEL_STATE_SAVED_COMMIT=" + commit, flush=True)
            event.update(status="save_completed", saved_commit=commit)
            code = 0
        else:
            sys.argv = ["football_model.advanced_research", *context["command"][3:]]
            try:
                runpy.run_module("football_model.advanced_research", run_name="__main__")
                code = 0
            except SystemExit as exc:
                code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
            event.update(status="command_completed", original_exit_code=code)
    except subprocess.TimeoutExpired as exc:
        try:
            planned = _planned_push(exc.cmd, context["repo"])
            if exc.timeout != 180:
                raise RecoveryError("unexpected_timeout_bound")
            event.update(status="push_timeout", planned_commit=planned,
                error_type="TimeoutExpired", timeout_command=exc.cmd,
                timeout_seconds=exc.timeout, captured_at=_now())
            print("MODEL_STATE_CHECKED_PUSH_TIMEOUT=" + json.dumps({
                "planned_commit": planned, "timeout_seconds": exc.timeout,
                "original_exit_code": 124}), flush=True)
        except RecoveryError:
            event.update(status="unrecoverable_timeout", error_code="not_exact_state_push_timeout")
        code = 124
    except BaseException as exc:
        event.update(status="command_failed", error_code=type(exc).__name__)
        print("Checked state child failed: " + type(exc).__name__, file=sys.stderr, flush=True)
        code = 1
    event.update(ended_at=_now(), original_exit_code=code)
    _exclusive(event_path, event)
    return code


def execute(operation, *, repo, runtime, receipt_path, command=()):
    repo = Path(repo).resolve()
    runtime = Path(runtime)
    runtime = runtime.resolve() if runtime.is_absolute() else (repo / runtime).resolve()
    context = _context(repo, runtime, operation, list(command))
    receipt_path = Path(receipt_path).resolve()
    if receipt_path.is_relative_to(Path(context["runtime"])):
        raise RecoveryError("receipt_must_be_outside_saved_runtime")
    receipt = {**context, "status": "started", "operation_completed": False,
        "recovered_state_only": False, "push_retried": False}
    _exclusive(receipt_path, receipt)
    code = 1
    try:
        gitdir = Path(context["marker_path"]).parent
        with tempfile.TemporaryDirectory(prefix="checked-state-", dir=gitdir) as directory:
            directory = Path(directory)
            context_path, event_path = directory / "context.json", directory / "event.json"
            _exclusive(context_path, context)
            result = subprocess.run([sys.executable, str(Path(__file__).resolve()), "_child",
                "--context", str(context_path), "--event", str(event_path)],
                env={**os.environ, "PYTHONUNBUFFERED": "1"}, check=False)
            receipt["original_exit_code"] = result.returncode
            event = _json(event_path.read_bytes())
            if (event.get("context_sha256") != _sha(context_path.read_bytes())
                    or event.get("schema_version") != VERSION
                    or event.get("original_exit_code") != result.returncode):
                raise RecoveryError("child_event_not_bound")
            receipt.update(original_exit_code=result.returncode, child_event=event)
            _check_source(context)
            if result.returncode == 124 and event.get("status") == "push_timeout":
                receipt["recovery"] = recover(context, event)
                receipt.update(status="push_timeout_remote_save_verified",
                    operation_completed=operation == "save", recovered_state_only=operation == "run")
                # This is a verified save fact for either mode.  The separate
                # original exit/operation fields still say the study aborted.
                print("MODEL_STATE_SAVED_COMMIT=" + event["planned_commit"], flush=True)
                code = 0
            else:
                code = result.returncode
                receipt.update(status=event["status"], operation_completed=code == 0)
                if operation == "save" and code == 0:
                    saved = event.get("saved_commit")
                    marker = _json(Path(context["marker_path"]).read_bytes())
                    if not OID.fullmatch(str(saved)) or marker != {**context["before_marker"], "state_oid": saved}:
                        raise RecoveryError("normal_save_marker_mismatch")
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, subprocess.SubprocessError) as exc:
        receipt.update(status="not_proven_saved", recovery_error_code=type(exc).__name__,
            recovery_error=str(exc) if isinstance(exc, RecoveryError) else "recovery_check_failed")
        code = 1
    finally:
        receipt.update(ended_at=_now(), wrapper_exit_code=code)
        temporary = receipt_path.with_name(receipt_path.name + ".final.tmp")
        _exclusive(temporary, receipt)
        temporary.replace(receipt_path)
    return code, receipt


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("operation", choices=("save", "run", "_child"))
    parser.add_argument("--repo", type=Path, default=ROOT)
    parser.add_argument("--runtime", type=Path, default=ROOT / "runtime")
    parser.add_argument("--receipt", type=Path)
    parser.add_argument("--context", type=Path)
    parser.add_argument("--event", type=Path)
    args, command = parser.parse_known_args(argv)
    if args.operation == "_child":
        if not args.context or not args.event or command:
            parser.error("internal child requires context and event only")
        return _child(args.context, args.event)
    if args.receipt is None:
        parser.error("--receipt is required and must not already exist")
    command = command[1:] if command[:1] == ["--"] else command
    if (args.operation == "save" and command) or (args.operation == "run" and not command):
        parser.error("run requires -- python -m football_model.advanced_research study ...")
    try:
        code, _ = execute(args.operation, repo=args.repo, runtime=args.runtime,
            receipt_path=args.receipt, command=command)
        return code
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, subprocess.SubprocessError) as exc:
        print("Checked state launcher refused: " + type(exc).__name__, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
