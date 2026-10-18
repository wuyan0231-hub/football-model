"""Read-only fixed-state fixture validation audit; never constructs an API client.

Original failures remain immutable. Output contains identity fields and hashes,
not raw provider responses or credentials. No collecting, fitting or state save.
"""
from __future__ import annotations
import argparse
import copy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import sys


class AuditError(ValueError):
    pass


def sha40(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{40}", value):
        raise AuditError("invalid_commit")
    return value


def safe_path(root, relative):
    if not isinstance(relative, str) or not relative:
        raise AuditError("unsafe_path")
    p = PurePosixPath(relative)
    if p.is_absolute() or any(
        c in ("", ".", "..") for c in relative.split("/")
    ) or "\\" in relative or any(ord(c) < 32 for c in relative):
        raise AuditError("unsafe_path")
    result = (Path(root) / relative).resolve()
    if not result.is_relative_to(Path(root).resolve()):
        raise AuditError("unsafe_path")
    return result


def read_json(path, evidence):
    path = Path(path)
    if not path.is_file():
        raise AuditError("required_original_unavailable")
    if path.stat().st_size > 60 * 1024 * 1024:
        raise AuditError("original_size_limit")
    raw = path.read_bytes()
    obj = json.loads(raw, parse_constant=lambda _: (_ for _ in ()).throw(AuditError("nonfinite_json")))
    evidence.append({"path": str(path), "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()})
    return obj


def cache_relative(endpoint, params):
    if endpoint not in ("/fixtures", "/leagues") or not isinstance(params, dict):
        raise AuditError("unsupported_cache_request")
    ident = json.dumps([endpoint, params], sort_keys=True).encode("utf-8")
    return "cache/api_cache/" + hashlib.sha256(ident).hexdigest() + ".json"


class ReadOnlyCache:
    """Only reads local originals; cannot delegate a miss to network or status."""
    def __init__(self, runtime, evidence, cutoff, parse_time):
        self.runtime, self.evidence, self.cutoff, self.parse_time = runtime, evidence, cutoff, parse_time

    def _cached_request(self, endpoint, params, **_ignored):
        data = read_json(safe_path(self.runtime, cache_relative(endpoint, params)), self.evidence)
        if data.get("endpoint") != endpoint or data.get("params") != params or data.get("errors"):
            raise AuditError("cache_identity_or_error")
        if not isinstance(data.get("response"), list):
            raise AuditError("cache_response_schema")
        if self.parse_time(data["fetched_at"]) > self.cutoff:
            raise AuditError("cache_observed_after_original_failure")
        result = copy.deepcopy(data)
        result.setdefault("from_cache", False)
        return result


def restore_fixed(repo, runtime, state, cloud):
    sha40(state)
    if runtime.exists() and any(runtime.iterdir()):
        raise AuditError("runtime_must_be_empty")
    cloud._ensure_memory()
    cloud._ensure_disk_space(repo, 2 * cloud._MAX_TOTAL_BYTES)
    cloud._git(repo, "fetch", "--depth=1", "--no-tags", "origin", state)
    if cloud._git(repo, "rev-parse", "FETCH_HEAD").stdout.strip() != state:
        raise AuditError("fetched_commit_mismatch")
    with cloud._worktree(repo, state) as tree:
        resources = None
        def before_restore(stats):
            nonlocal resources
            resources = cloud._restore_disk_budget(tree, stats)
            cloud._ensure_disk_space(repo, resources["additional_bytes"])
        cloud.state_storage.restore_runtime(
            tree, runtime, max_file_bytes=cloud._MAX_FILE_BYTES,
            max_physical_bytes=cloud._MAX_TOTAL_BYTES,
            max_logical_bytes=cloud._MAX_LOGICAL_BYTES,
            cold_state_commit=state, cold_repo=repo,
            before_restore=before_restore,
        )
    # Deliberately do NOT create a cloud-state marker authorizing a later save.
    return resources


def audit(runtime, repo, match_id, expected_source):
    from football_model.data import APIClient, _utc
    from football_model.collect_fundamentals import _verified_fixture
    from football_model.fundamentals import failure_diagnostic
    evidence = []
    report = read_json(runtime / "fundamentals/reports/latest.json", evidence)
    matches = [m for m in report["matches"] if m["match_id"] == match_id]
    if len(matches) != 1:
        raise AuditError("target_not_unique_in_fixed_report")
    diagnostics = [d for d in matches[0]["diagnostics"] if d["match_id"] == match_id and d["stage"] == "fixture_validate"]
    if len(diagnostics) != 1:
        raise AuditError("fixture_validation_diagnostic_not_unique")
    entry = diagnostics[0]
    diagnostic = read_json(safe_path(runtime, entry["diagnostic_path"]), evidence)
    if hashlib.sha256(safe_path(runtime, entry["diagnostic_path"]).read_bytes()).hexdigest() != Path(entry["diagnostic_path"]).stem:
        raise AuditError("diagnostic_content_address_mismatch")
    expected = diagnostic["payload"]["fixture"]
    cutoff = _utc(diagnostic["checked_at"])
    reader = ReadOnlyCache(runtime, evidence, cutoff, _utc)
    envelope = reader._cached_request("/fixtures", {"id": match_id})
    if _utc(envelope["fetched_at"]) != _utc(diagnostic["response_observed_at"]):
        raise AuditError("cache_not_exact_original_observation")
    replay_projection = failure_diagnostic(
        diagnostic["kind"], expected, stage=diagnostic["stage"], error=ValueError(),
        checked_at=diagnostic["checked_at"], envelope=envelope,
        context_observed_at=diagnostic["payload"]["context_observed_at"],
    )
    if replay_projection["payload_sha256"] != diagnostic["payload_sha256"]:
        raise AuditError("cache_original_projection_mismatch")
    config = read_json(repo / "config.json", evidence)
    comps = [c for c in config["competitions"] if c["key"] == matches[0]["league"]]
    if len(comps) != 1:
        raise AuditError("competition_not_unique")
    # Call only the existing pure metadata selector with our local-only reader.
    metadata = APIClient.resolve_league(reader, comps[0], force=True)
    allowed = {"fixture_source_invalid", "fixture_observation_in_future", "fixture_response_not_unique",
        "fixture_structure_invalid", "fixture_identity_changed", "fixture_timestamp_conflict",
        "fixture_league_mismatch", "season_coverage_missing", "fixture_status_missing"}
    result = {"match_id": match_id, "league": matches[0]["league"], "original_checked_at": diagnostic["checked_at"],
        "original_diagnostic_replay_verified": diagnostic["replay_verified"], "original_projection_match": True,
        "metadata_observed_at": metadata["fetched_at"], "source_commit_of_original_state": expected_source,
        "status": "not_rejected_on_replay", "validator_code": None, "evidence": evidence}
    try:
        _verified_fixture(envelope, expected, metadata, cutoff)
    except (ValueError, KeyError, TypeError) as exc:
        result.update(status="replayed_original_rejection", validator_code=str(exc) if str(exc) in allowed else "unclassified_validation_error",
                      exception_class=type(exc).__name__)
    # Do not print raw response values or free text. The exact originals remain
    # only in the already-saved fixed model-state; this report carries hashes.
    return result


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--repo", type=Path, required=True)
    p.add_argument("--state-commit", type=sha40, required=True)
    p.add_argument("--expected-source", type=sha40, required=True)
    p.add_argument("--match-id", type=int, required=True)
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args(argv)
    if args.match_id < 1:
        p.error("match-id must be positive")
    repo = args.repo.resolve()
    sys.path.insert(0, str(repo))
    sys.path.insert(0, str(repo / "scripts"))
    import cloud_state as cloud
    output = {"schema_version": "fixed-fixture-validation-audit-v1", "started_at": datetime.now(timezone.utc).isoformat(),
        "state_commit": args.state_commit, "expected_source": args.expected_source, "match_id": args.match_id,
        "network_football_requests": 0, "fits": 0, "remote_state_writes": 0, "original_failures_modified": False,
        "production_promoted": False}
    try:
        output["probe_source_commit"] = cloud._git(repo, "rev-parse", "HEAD").stdout.strip()
        runtime = repo / "fixed-fixture-audit-runtime"
        output["restore_preflight"] = restore_fixed(repo, runtime, args.state_commit, cloud)
        with cloud._worktree(repo, args.state_commit) as tree:
            meta = read_json(tree / "state_metadata.json", [])
        if meta["source_sha"] != args.expected_source:
            raise AuditError("state_source_mismatch")
        output["original_metadata"] = {k:meta[k] for k in ["source_sha", "previous_state_oid", "github_run_id", "github_run_attempt"]}
        output["result"] = audit(runtime, repo, args.match_id, args.expected_source)
        output["status"] = output["result"]["status"]
    except Exception as exc:
        output.update(status="audit_incomplete", error_code=str(exc) if isinstance(exc, AuditError) else "unclassified_audit_error", exception_class=type(exc).__name__)
    output["ended_at"] = datetime.now(timezone.utc).isoformat()
    args.output.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(output, ensure_ascii=False))
    return 0 if output["status"] == "replayed_original_rejection" and output["result"]["validator_code"] != "unclassified_validation_error" else 1


if __name__ == "__main__":
    raise SystemExit(main())
