"""Bounded storage-only acceptance: one ordinary save, then fixed readback.

No football API, fit, forecast, queue, first-output or model-card operation is
performed. The only new runtime original is a small hash/identity receipt.
The native load/check/save gates remain responsible for the complete state;
the separate read job verifies this new receipt, metadata and the exact parent.
A previously tested read-only freeze is a separate acceptance item, not repeated
or silently claimed by this storage-only entrypoint.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
VERSION = "engineering-storage-roundtrip-v1"
MAX_RECEIPT_BYTES = 64 * 1024
OID = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")
SHA = re.compile(r"[0-9a-f]{64}")
SCOPE = {"football_api_requests": 0, "fits": 0, "new_forecasts": 0,
         "new_queue_entries": 0, "new_first_outputs": 0, "model_cards_relabelled": 0,
         "confirmation_activated": False, "production_promoted": False,
         "accuracy_improvement_verified": False}


def now():
    return datetime.now(timezone.utc).isoformat()


def stamp(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timezone_required")
    return parsed.astimezone(timezone.utc)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def git(repo, *args):
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
        timeout=180, env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
    if result.returncode:
        raise ValueError("git_operation_failed")
    return result.stdout.decode().strip()


def bounded_json(path):
    path = Path(path)
    if path.is_symlink() or path.stat().st_size > MAX_RECEIPT_BYTES:
        raise ValueError("receipt_path_or_size_invalid")
    raw = path.read_bytes()
    return raw, json.loads(raw)


def context(repo):
    repo = Path(repo).resolve()
    source = git(repo, "rev-parse", "HEAD")
    run, attempt = os.environ.get("GITHUB_RUN_ID"), os.environ.get("GITHUB_RUN_ATTEMPT")
    if (repo != ROOT or os.environ.get("GITHUB_REF") != "refs/heads/main"
            or os.environ.get("GITHUB_EVENT_NAME") != "workflow_dispatch"
            or os.environ.get("GITHUB_REPOSITORY") != "wuyan0231-hub/football-model"
            or os.environ.get("GITHUB_SHA") != source or not OID.fullmatch(source)
            or not re.fullmatch(r"[1-9][0-9]*", str(run))
            or not re.fullmatch(r"[1-9][0-9]*", str(attempt))):
        raise ValueError("matching_main_manual_run_required")
    return {"source_sha": source, "source_ref": "refs/heads/main",
            "github_run_id": run, "github_run_attempt": attempt}


def relative(ctx):
    return "engineering/storage_roundtrips/" + ctx["github_run_id"] + "/" + ctx["github_run_attempt"] + ".json"


def implementation():
    from football_model.advanced_research import implementation_fingerprint
    value = implementation_fingerprint()
    if not SHA.fullmatch(str(value.get("sha256"))):
        raise ValueError("invalid_implementation_fingerprint")
    return {"sha256": value["sha256"], "file_count": len(value["files"])}


def marker(repo):
    gitdir = Path(git(repo, "rev-parse", "--absolute-git-dir"))
    return bounded_json(gitdir / "football-cloud-state.json")[1]


def write_json(path, value, *, exclusive=False):
    raw = (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n").encode()
    if len(raw) > MAX_RECEIPT_BYTES:
        raise ValueError("receipt_size_limit")
    path = Path(path)
    if path.is_symlink() or any(parent.is_symlink() for parent in path.parents):
        raise ValueError("receipt_symlink")
    with path.open("xb" if exclusive else "wb") as handle:
        handle.write(raw)
    return raw


def validate_payload(payload, ctx):
    if (payload.get("schema_version") != VERSION or payload.get("run_identity") != ctx
            or not OID.fullmatch(str(payload.get("parent_state_oid")))
            or payload.get("scope") != SCOPE or payload.get("implementation") != implementation()
            or payload.get("readonly_freeze") != "separate_profile_acceptance_required_not_claimed_here"):
        raise ValueError("receipt_identity_or_scope_mismatch")
    stamp(payload["created_at"])
    return payload


def prepare(repo, runtime):
    ctx = context(repo)
    runtime = Path(runtime).absolute()
    if runtime != repo / "runtime" or runtime.is_symlink():
        raise ValueError("fixed_runtime_required")
    baseline = marker(repo)
    if (baseline.get("schema_version") != 1 or baseline.get("source_sha") != ctx["source_sha"]
            or baseline.get("source_ref") != ctx["source_ref"]
            or not OID.fullmatch(str(baseline.get("state_oid")))):
        raise ValueError("valid_existing_loaded_parent_required")
    payload = {"schema_version": VERSION, "created_at": now(), "run_identity": ctx,
        "parent_state_oid": baseline["state_oid"], "implementation": implementation(),
        "scope": SCOPE, "readonly_freeze": "separate_profile_acceptance_required_not_claimed_here"}
    path = runtime / relative(ctx)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json(path, payload, exclusive=True)
    return payload


def record(repo, runtime, save_receipt, output):
    ctx = context(repo)
    raw, payload = bounded_json(Path(runtime) / relative(ctx))
    validate_payload(payload, ctx)
    outcomes = {key: os.environ.get(key) for key in ("RESTORE", "PREPARE", "CHECK", "PERSIST")}
    _, saved = bounded_json(save_receipt)
    reported = saved.get("save", {}).get("reported_commit")
    baseline = marker(repo)
    valid = (all(value == "success" for value in outcomes.values())
        and saved.get("schema_version") == "cloud-state-save-receipt-v1"
        and all(saved.get(key) == ctx[key] for key in ("source_sha", "github_run_id", "github_run_attempt"))
        and saved.get("parent_state_oid") == payload["parent_state_oid"]
        and saved.get("child_exit_code") == 0 and saved.get("receipt_parse_status") == "valid"
        and saved.get("runtime_validation", {}).get("status") == "passed"
        and saved.get("resource_preflight", {}).get("status") == "passed"
        and saved.get("resource_preflight", {}).get("report", {}).get("state_oid") == payload["parent_state_oid"]
        and saved.get("resource_preflight", {}).get("report", {}).get("can_save") is True
        and saved.get("pack_probe", {}).get("status") == "passed"
        and saved.get("save", {}).get("status") == "saved_and_marker_verified"
        and saved.get("save", {}).get("marker_matches_reported_commit") is True
        and OID.fullmatch(str(reported)) is not None
        and saved.get("post_state_oid") == reported
        and baseline.get("state_oid") == reported and baseline.get("source_sha") == ctx["source_sha"])
    result = {"schema_version": VERSION + "-run", "observed_at": now(), "run_identity": ctx,
        "parent_state_oid": payload["parent_state_oid"], "scope": SCOPE, "outcomes": outcomes,
        "receipt_ref": {"path": relative(ctx), "sha256": sha(raw)},
        "native_save_receipt_sha256": sha(Path(save_receipt).read_bytes()),
        "saved_state_oid": reported if valid else None,
        "status": "saved_pending_independent_readback" if valid else "not_proven_saved"}
    write_json(output, result)
    if valid:
        with Path(os.environ["GITHUB_OUTPUT"]).open("a") as out:
            out.write("saved_state=" + reported + "\n")
            out.write("parent_state=" + payload["parent_state_oid"] + "\n")
            out.write("receipt_ref=" + json.dumps(result["receipt_ref"], separators=(",", ":")) + "\n")
    return 0 if valid else 1


def audit(repo, state, parent, receipt_ref, output):
    ctx = context(repo)
    if (not OID.fullmatch(str(state)) or not OID.fullmatch(str(parent)) or state == parent
            or not isinstance(receipt_ref, dict) or set(receipt_ref) != {"path", "sha256"}
            or receipt_ref["path"] != relative(ctx) or not SHA.fullmatch(str(receipt_ref["sha256"]))):
        raise ValueError("exact_fixed_state_parent_and_run_receipt_required")
    git(repo, "fetch", "--depth=2", "--no-tags", "origin", state)
    if git(repo, "rev-parse", "FETCH_HEAD") != state or git(repo, "show", "-s", "--format=%P", state) != parent:
        raise ValueError("fixed_fetch_or_exact_parent_mismatch")
    from football_model.history_archive import FixedStateReader
    with FixedStateReader(repo, state) as reader:
        manifest = reader.validate_manifest()
        raw = reader.read(receipt_ref["path"])
        if len(raw) > MAX_RECEIPT_BYTES or sha(raw) != receipt_ref["sha256"]:
            raise ValueError("saved_receipt_raw_hash_mismatch")
        payload = validate_payload(json.loads(raw), ctx)
        metadata_raw = reader.root_bytes("state_metadata.json")
        if len(metadata_raw) > MAX_RECEIPT_BYTES:
            raise ValueError("metadata_size_limit")
        metadata = json.loads(metadata_raw)
    if (metadata.get("schema_version") != 1 or metadata.get("previous_state_oid") != parent
            or payload["parent_state_oid"] != parent
            or any(metadata.get(key) != ctx[key] for key in ctx)
            or stamp(metadata["saved_at"]) < stamp(payload["created_at"])):
        raise ValueError("fixed_metadata_parent_source_or_run_mismatch")
    result = {"schema_version": VERSION + "-readback", "observed_at": now(), "status": "verified",
        "state_oid": state, "parent_state_oid": parent, "run_identity": ctx,
        "receipt_ref": receipt_ref, "metadata_sha256": sha(metadata_raw), "manifest": manifest,
        "exact_git_parent_verified": True, "receipt_raw_bytes_verified": True,
        "native_complete_state_gates": "load_check_save_in_writer_job_not_repeated_here",
        "readonly_freeze": payload["readonly_freeze"], "scope": SCOPE,
        "remote_state_writes_in_this_read_job": 0}
    write_json(output, result)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("prepare", "record", "audit"))
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--runtime", type=Path, default=Path("runtime"))
    parser.add_argument("--save-receipt", type=Path, default=Path("engineering-save-receipt.json"))
    parser.add_argument("--output", type=Path, default=Path("engineering-roundtrip.json"))
    args = parser.parse_args(argv)
    repo, runtime = args.repo.resolve(), args.runtime.absolute()
    try:
        if args.mode == "prepare":
            prepare(repo, runtime)
            return 0
        if args.mode == "record":
            return record(repo, runtime, args.save_receipt, args.output)
        return audit(repo, os.environ.get("ENGINEERING_SAVED_STATE"), os.environ.get("ENGINEERING_PARENT_STATE"),
            json.loads(os.environ.get("ENGINEERING_RECEIPT_REF", "null")), args.output)
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        write_json(args.output, {"schema_version": VERSION + "-failure", "observed_at": now(),
            "mode": args.mode, "status": "not_verified", "error_category": type(exc).__name__, "scope": SCOPE})
        print("Engineering storage acceptance not verified; bounded failure receipt retained.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
