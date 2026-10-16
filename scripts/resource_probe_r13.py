"""Read-only R13 resource acceptance; never save state, fit, or call football API."""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import resource
import shutil
import subprocess
import sys
import threading
import time

SOURCE_SHA = "8902dfc3223eda89e4d346326ee7bfd8cf7fc769"
SOURCE_TREE = "97546ba6bc4c20c3bb8c8a0ecd4bd6a4b80de6d7"
REQUESTED_STATE = "dae94ec91c5db4702bf7be7fb8a0b966c71beb1d"
MAX_ARTIFACT = 16 * 1024 * 1024
MAX_JSON = 2 * 1024 * 1024
PERSONNEL_INJURY_MAX_ORIGINAL_BYTES = 4 * 1024 * 1024
PERSONNEL_INJURY_SOURCE_STATE = "53c88a9a80b111b1909a4680cd9e490b229390a0"
PERSONNEL_INJURY_SELECTION_SHA256 = "c705b92b0039918995a6add9e73fca4f03ac1f26f655d6b86f171a1d8447adc6"
PERSONNEL_INJURY_FEATURE_CUTOFF = "2026-10-16T02:02:12.679113Z"
# Exact selection from audit/personnel-selected-injury-refs.json. These IDs
# identify normalized snapshots, never presumed raw-file hashes.
PERSONNEL_INJURY_REFS = (
    ("allsvenskan", 1494298, "0118a11b452ae1508bb4e4822bb2baf7ceb541e3039e3bb2e8852f998598b36e"),
    ("eredivisie", 1552189, "91fbcac5d50beb953df2d813c3dbaa46a1d24a61e8d595b20f22250420d670e7"),
    ("bundesliga", 1575187, "006a7d733e7be7fa0a26bfafa17fb946980be0c0b410a0d04ae78bff765c685a"),
    ("ligue_1", 1552785, "52369678687d939ac219fd3277e2bfcc3ce7bab4358840883381f2e84c5bc4e3"),
    ("serie_a", 1550151, "aa17cb41930cfd6aa32284a8ef20acfbb55306dd8375569a8ca4f2eb8128e45f"),
    ("championship", 1563205, "c31131b8bc54a3415f5de081952098f5ed65aed61588a62aeb35314da933972e"),
    ("laliga", 1570415, "14f57171dfe84ddd36ac60db29f5e70b46b51cdcf05e33f01706e76bc9e85c1f"),
    ("brazil_a", 1492417, "3b5cadb1e188d23d8edbbda1e95cd4b8e217478463bcba53b29b61e38f4a417c"),
)
PHASE_LIMITS = {"identity": 60, "load": 840, "check": 360, "preflight": 360,
                "cold": 300, "cumulative": 60, "personnel_injury_refs": 300,
                "integration": 180, "durability": 540}
RUN_BUDGET_SECONDS = 1500
FINALIZATION_RESERVE_SECONDS = 90


def now():
    return datetime.now(timezone.utc).isoformat()


class NativeProgress:
    """Observe native calls without replacing validation, bytes or arithmetic."""
    def __init__(self, phase, output=None):
        self.phase, self.started = phase, time.monotonic()
        self.stack, self.records = [], []
        self.counts = {"expand_calls": 0, "compressed_input_bytes_read": 0,
                       "gzip_calls": 0, "gzip_raw_bytes": 0,
                       "large_sha_calls": 0, "large_sha_bytes": 0}
        self.stop = threading.Event()
        self.output = output

    def emit(self, event, **details):
        print("RESOURCE_PROBE_PROGRESS=" + json.dumps({"at": now(), "phase": self.phase,
            "event": event, "elapsed_seconds": round(time.monotonic() - self.started, 3),
            "native_stage_stack": list(self.stack), "counters": dict(self.counts), **details},
            sort_keys=True), flush=True)

    def wrap(self, owner, name, label):
        original = getattr(owner, name)
        def tracked(*args, **kwargs):
            start = time.monotonic()
            self.stack.append(label)
            self.emit("native_stage_started", stage=label)
            status = "failed"
            try:
                result = original(*args, **kwargs)
                status = "passed"
                return result
            finally:
                elapsed = time.monotonic() - start
                self.records.append({"stage": label, "elapsed_seconds": elapsed, "status": status})
                self.emit("native_stage_finished", stage=label, status=status,
                          stage_elapsed_seconds=round(elapsed, 3))
                self.stack.pop()
        setattr(owner, name, tracked)
        return lambda: setattr(owner, name, original)

    @contextmanager
    def observe(self, repo, cloud=None):
        sys.path.insert(0, str(repo))
        from football_model import state_storage as storage, history_archive as history
        undo = []
        for owner, name, label in [(storage, "validate_storage_tree", "validate_complete_transport"),
                (storage, "validate_runtime", "validate_active_runtime"),
                (storage, "restore_runtime", "restore_active_and_cold_catalog"),
                (storage, "estimate_runtime", "estimate_actual_gzip_transport"),
                (history, "restore_history_catalog", "construct_fixed_history_catalog"),
                (history, "_write_catalog", "write_bounded_history_catalog")]:
            undo.append(self.wrap(owner, name, label))
        if cloud is not None:
            for name, label in [("_git", "native_git_read"), ("_restore_disk_budget", "measure_restore_disk_budget"),
                                ("_ensure_disk_space", "check_actual_disk_headroom")]:
                undo.append(self.wrap(cloud, name, label))
        expand, compress, digest = storage._expand, storage.gzip.compress, history._sha
        def counted_expand(raw, *args, **kwargs):
            self.counts["expand_calls"] += 1
            self.counts["compressed_input_bytes_read"] += len(raw)
            return expand(raw, *args, **kwargs)
        def counted_compress(raw, *args, **kwargs):
            self.counts["gzip_calls"] += 1
            self.counts["gzip_raw_bytes"] += len(raw)
            return compress(raw, *args, **kwargs)
        def counted_sha(raw):
            if len(raw) >= 32 * 1024 * 1024:
                self.counts["large_sha_calls"] += 1
                self.counts["large_sha_bytes"] += len(raw)
                if self.phase == "load" and self.counts["large_sha_calls"] > 4:
                    raise ValueError("probe_load_repeated_large_digest_guard")
            return digest(raw)
        storage._expand, storage.gzip.compress, history._sha = counted_expand, counted_compress, counted_sha
        def heartbeat():
            while not self.stop.wait(30):
                self.emit("heartbeat")
        thread = threading.Thread(target=heartbeat, daemon=True)
        thread.start()
        self.emit("phase_native_work_started")
        try:
            yield self
        finally:
            self.stop.set()
            thread.join(timeout=1)
            storage._expand, storage.gzip.compress, history._sha = expand, compress, digest
            for restore in reversed(undo):
                restore()
            self.emit("phase_native_work_finished")
            if self.output is not None:
                write_json(self.output / (self.phase + "-native-stages.json"),
                           {"stages": self.records, "counters": self.counts})


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def write_json(path, value):
    raw = (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()
    if len(raw) > MAX_JSON:
        raise ValueError("probe_json_size_limit")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)


def git(repo, *args):
    if "push" in args:
        raise ValueError("probe_forbids_remote_writes")
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                            timeout=180, env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
    if result.returncode:
        raise ValueError("probe_git_read_failed")
    return result.stdout.decode().strip()


def cloud_module(repo):
    sys.path.insert(0, str(repo))
    spec = importlib.util.spec_from_file_location("r13_probe_cloud_state", repo / "scripts/cloud_state.py")
    cloud = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cloud)
    return cloud


def marker(repo):
    directory = Path(git(repo, "rev-parse", "--absolute-git-dir"))
    value = json.loads((directory / "football-cloud-state.json").read_text())
    return {key: value.get(key) for key in ("state_oid", "source_sha", "source_ref", "loaded_at")}


def snapshot(repo):
    disk = shutil.disk_usage(repo)
    fields = {}
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            key, value = line.split(":", 1)
            if key in {"MemTotal", "MemAvailable"}:
                fields[key + "_bytes"] = int(value.split()[0]) * 1024
    except (OSError, ValueError):
        pass
    for key, path in (("cgroup_memory_max", "/sys/fs/cgroup/memory.max"),
                      ("cgroup_memory_current", "/sys/fs/cgroup/memory.current")):
        try:
            value = Path(path).read_text().strip()
            fields[key] = int(value) if value.isdigit() else value
        except OSError:
            pass
    allocated = {}
    for name in ("runtime", ".git"):
        path = repo / name
        if path.exists():
            try:
                result = subprocess.run(["du", "-sx", "--block-size=1", str(path)],
                                        capture_output=True, timeout=20)
                allocated[name] = int(result.stdout.split()[0]) if result.returncode == 0 else None
            except (OSError, ValueError, subprocess.TimeoutExpired):
                allocated[name] = None  # Unknown allocation is never zero/free.
    return {"at": now(), "disk_total_bytes": disk.total, "disk_used_bytes": disk.used,
            "disk_free_bytes": disk.free, "allocated_bytes": allocated,
            "process_max_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss, **fields}


def git_storage_snapshot(repo):
    """Measure only the Git store around a selected native cold read."""
    directory = Path(git(repo, "rev-parse", "--absolute-git-dir"))
    disk = shutil.disk_usage(directory)
    allocated = None
    try:
        result = subprocess.run(["du", "-sx", "--block-size=1", str(directory)],
                                capture_output=True, timeout=20)
        if result.returncode == 0:
            allocated = int(result.stdout.split()[0])
    except (OSError, ValueError, subprocess.TimeoutExpired):
        pass
    return {"at": now(), "git_allocated_bytes": allocated,
            "disk_free_bytes": disk.free, "disk_total_bytes": disk.total}


def git_commit_present(repo, commit):
    """Check local availability without fetching or changing the object store."""
    result = subprocess.run(["git", "-C", str(repo), "cat-file", "--batch-check=%(objecttype)"],
                            input=(commit + "^{commit}\n").encode(), capture_output=True,
                            timeout=30, env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
    if result.returncode:
        raise ValueError("probe_local_commit_presence_check_failed")
    value = result.stdout.decode().strip()
    if value == "commit":
        return True
    if value == commit + "^{commit} missing":
        return False
    raise ValueError("probe_local_commit_presence_result_invalid")


def identity(repo, out):
    try:
        source_tree = git(repo, "rev-parse", SOURCE_SHA + "^{tree}")
    except ValueError:
        git(repo, "fetch", "--quiet", "--no-tags", "--depth=1", "origin", SOURCE_SHA)
        source_tree = git(repo, "rev-parse", SOURCE_SHA + "^{tree}")
    changes = git(repo, "diff", "--name-only", SOURCE_SHA, "HEAD").splitlines()
    allowed = {"scripts/resource_probe_r13.py", ".github/workflows/r13-resource-check.yml"}
    if source_tree != SOURCE_TREE or not set(changes).issubset(allowed):
        raise ValueError("probe_source_differs_from_released_r13")
    return {"source_sha": SOURCE_SHA, "source_tree": source_tree,
            "probe_commit": git(repo, "rev-parse", "HEAD"), "probe_only_changes": changes,
            "requested_state": REQUESTED_STATE,
            "state_routing": "native cloud_state.load; actual fetched immutable SHA is authoritative",
            "permissions": {"contents": "read", "actions": "read"},
            "football_api_requests": 0, "new_fits": 0, "remote_writes": 0,
            "resource_expectation": "standard Ubuntu runner; actual memory and disk are measured"}


def load(repo, out):
    cloud = cloud_module(repo)
    progress = NativeProgress("load", out)
    with progress.observe(repo, cloud):
        observed = cloud._remote_oid(repo)
        actual = cloud.load_state(repo)
    if not actual:
        raise ValueError("probe_requires_existing_saved_state")
    saved = marker(repo)
    if saved["state_oid"] != actual:
        raise ValueError("probe_loaded_state_marker_mismatch")
    return {"requested_state": REQUESTED_STATE, "observed_state_before_load": observed,
            "actual_loaded_state": actual, "requested_state_was_loaded": actual == REQUESTED_STATE,
            "marker": saved, "fit_performed": False, "remote_write_performed": False}


def check(repo, out):
    cloud = cloud_module(repo)
    progress = NativeProgress("check", out)
    with progress.observe(repo, cloud):
        estimate = cloud._validate_runtime(repo / "runtime")
    return {"validation": "native cloud_state._validate_runtime", "actual_state": marker(repo)["state_oid"],
            "estimate": {k: v for k, v in estimate.items() if k != "retained_inline"}}


def preflight(repo, out):
    cloud = cloud_module(repo)
    progress = NativeProgress("preflight", out)
    with progress.observe(repo, cloud):
        report = cloud.preflight_state(repo)
    if report["can_save"] is not True:
        write_json(out / "preflight-capacity-details.json", report)
        raise ValueError("native_save_preflight_does_not_have_resource_headroom")
    return report


def cold(repo, out):
    sys.path.insert(0, str(repo))
    from football_model import history_archive as history
    runtime = repo / "runtime"
    entries = history._catalog(runtime)
    if not entries:
        raise ValueError("probe_no_actual_cold_originals")
    selected = []
    for prefix in ("advanced_models/objects/", "advanced_models/inputs/"):
        group = [e for e in entries.values() if e["path"].startswith(prefix)]
        if group:
            selected.append(max(group, key=lambda e: e["raw_bytes"]))
    last = min(entries.values(), key=lambda e: (e["raw_bytes"], e["path"]))
    if last["path"] not in {e["path"] for e in selected}:
        selected.append(last)
    proofs, first_source_reads, observed_sources = [], [], set()
    progress = NativeProgress("cold", out)
    actual_state = marker(repo)["state_oid"]
    with history.FixedStateReader(repo, actual_state) as state_reader:
        manifest = state_reader.validate_manifest()
        for entry in selected:
            source_commit = entry["state_commit"]
            if source_commit not in observed_sources:
                observed_sources.add(source_commit)
                measurement = {"source_commit": source_commit,
                    "selected_original_path": entry["path"],
                    "source_transport_path": entry["transport_path"],
                    "expected_raw_sha256": entry["raw_sha256"], "expected_raw_bytes": entry["raw_bytes"],
                    "expected_blob_sha256": entry["blob_sha256"], "expected_blob_bytes": entry["compressed_bytes"],
                    "expected_source_manifest_sha256": entry["manifest_sha256"],
                    "commit_present_before_first_selected_read": git_commit_present(repo, source_commit),
                    "before": git_storage_snapshot(repo), "status": "started"}
                first_source_reads.append(measurement)
                write_json(out / "cold-first-source-git-reads.json", first_source_reads)
                progress.emit("first_selected_source_read_started", source_commit=source_commit,
                              commit_present_before=measurement["commit_present_before_first_selected_read"])
                started = time.monotonic()
                try:
                    raw = history.read_bytes(runtime / entry["path"])
                    measurement.update(status="passed", actual_raw_bytes=len(raw), actual_raw_sha256=sha(raw))
                finally:
                    measurement.update(native_read_elapsed_seconds=time.monotonic() - started,
                        after=git_storage_snapshot(repo),
                        commit_present_after_first_selected_read=git_commit_present(repo, source_commit))
                    if measurement["status"] == "started":
                        measurement["status"] = "failed"
                    before, after = measurement["before"], measurement["after"]
                    measurement["git_allocated_delta_bytes"] = (
                        after["git_allocated_bytes"] - before["git_allocated_bytes"]
                        if after["git_allocated_bytes"] is not None and before["git_allocated_bytes"] is not None else None)
                    measurement["disk_free_consumed_bytes"] = before["disk_free_bytes"] - after["disk_free_bytes"]
                    measurement["source_commit_acquired_during_native_read"] = (
                        not measurement["commit_present_before_first_selected_read"]
                        and measurement["commit_present_after_first_selected_read"])
                    write_json(out / "cold-first-source-git-reads.json", first_source_reads)
                    progress.emit("first_selected_source_read_finished", source_commit=source_commit,
                        status=measurement["status"], native_read_elapsed_seconds=measurement["native_read_elapsed_seconds"],
                        git_allocated_delta_bytes=measurement["git_allocated_delta_bytes"])
                if measurement["git_allocated_delta_bytes"] is None:
                    raise ValueError("probe_first_source_git_allocation_unmeasured")
            else:
                raw = history.read_bytes(runtime / entry["path"])
            fixed = state_reader.read(entry["path"])
            with history.FixedStateReader(repo, entry["state_commit"]) as source:
                blob = source.root_bytes(entry["transport_path"])
                original_manifest = source.root_bytes("state_storage.json", missing=True)
            actual_manifest_sha = sha(original_manifest) if original_manifest is not None else None
            if (sha(raw) != entry["raw_sha256"] or raw != fixed
                    or len(raw) != entry["raw_bytes"]
                    or sha(blob) != entry["blob_sha256"] or len(blob) != entry["compressed_bytes"]
                    or actual_manifest_sha != entry["manifest_sha256"]):
                raise ValueError("probe_cold_original_hash_mismatch")
            if (runtime / entry["path"]).exists():
                raise ValueError("probe_selected_original_was_expanded")
            proofs.append({**entry, "actual_raw_sha256": sha(raw), "actual_raw_bytes": len(raw),
                "actual_blob_sha256": sha(blob), "actual_blob_bytes": len(blob),
                "actual_manifest_sha256": actual_manifest_sha,
                "runtime_file_remains_unexpanded": not (runtime / entry["path"]).exists(),
                "native_history_reader_and_fixed_state_reader_agree": True})
    return {"actual_state": actual_state, "state_manifest": manifest,
            "catalog": history.catalog_stats(runtime), "selected_actual_originals": proofs,
            "first_selected_read_by_source": first_source_reads,
            "source_fetch_measurement_scope": "only existing selected-original loop; no extra source fetch",
            "source_commits_acquired_during_native_reads": sum(
                m["source_commit_acquired_during_native_read"] for m in first_source_reads)}


def cumulative(repo, out):
    """Audit the saved cumulative view without associating it with this probe job."""
    sys.path.insert(0, str(repo))
    from football_model import history_archive as history, run_summary
    path = repo / "runtime/prospective/latest.json"
    value, raw_sha = run_summary._read(path)
    actual_state = marker(repo)["state_oid"]
    with history.FixedStateReader(repo, actual_state) as reader:
        saved = reader.read("prospective/latest.json")
    if sha(saved) != raw_sha:
        raise ValueError("probe_cumulative_view_differs_from_fixed_saved_state")
    native = run_summary._cumulative(value)
    statuses = value.get("match_statuses")
    if not isinstance(statuses, list):
        raise ValueError("probe_cumulative_legal_status_inventory_missing")
    counts = dict(sorted(Counter(row["status"] for row in statuses).items()))
    return {"actual_state": actual_state, "source_path": "runtime/prospective/latest.json",
            "source_raw_sha256": raw_sha, "source_raw_bytes": len(saved),
            "validator": "football_model.run_summary._cumulative", "validator_version": run_summary.VERSION,
            "native_validator_status": native.pop("status"), "status": "fixed_saved_cumulative_view",
            "legal_status_counts": counts, "cumulative": native,
            "current_probe_job_association_claimed": False,
            "counts_are_accuracy_or_profit_acceptance": False}


def injury_tree_blob(repo, reader, relative, cache, *, missing=False):
    """Bind exact bytes to the fixed Git tree; cache immutable metadata only."""
    key = (reader.state_commit, relative)
    if key in cache:
        return cache[key]
    raw = reader.root_bytes(relative, missing=missing)
    if raw is None:
        return None
    oid = git(repo, "rev-parse", "--verify", reader.state_commit + ":" + relative)
    algorithm = "sha1" if len(oid) == 40 else "sha256" if len(oid) == 64 else None
    if algorithm is None or any(c not in "0123456789abcdef" for c in oid):
        raise ValueError("probe_injury_invalid_git_blob_identity")
    actual = hashlib.new(algorithm, b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()
    if actual != oid:
        raise ValueError("probe_injury_git_blob_hash_mismatch")
    result = {"state_commit": reader.state_commit, "repository_path": relative,
              "git_blob_sha": oid, "computed_git_blob_sha": actual,
              "raw_sha256": sha(raw), "bytes": len(raw)}
    cache[key] = result
    return result


def injury_original_provenance(repo, reader, logical_path, raw, cache):
    from football_model import history_archive as history
    direct = injury_tree_blob(repo, reader, "runtime/" + logical_path, cache, missing=True)
    if direct is not None:
        if direct["raw_sha256"] != sha(raw) or direct["bytes"] != len(raw):
            raise ValueError("probe_injury_direct_original_mismatch")
        return {"route": "fixed_direct_git_blob", "original": direct}
    manifest_sha, index = history._manifest_index(repo, reader.state_commit)
    if logical_path in index:
        entry = {**index[logical_path], "state_commit": reader.state_commit,
                 "transport_path": "state_archives/" + index[logical_path]["blob_sha256"] + ".json.gz",
                 "codec": "gzip-single-member", "manifest_sha256": manifest_sha}
    else:
        entry = history._state_catalog(repo, reader.state_commit).get(logical_path)
    if entry is None:
        raise ValueError("probe_injury_original_route_unavailable")
    if entry["raw_sha256"] != sha(raw) or entry["raw_bytes"] != len(raw):
        raise ValueError("probe_injury_manifest_original_hash_mismatch")
    with history.FixedStateReader(repo, entry["state_commit"]) as source:
        transport = injury_tree_blob(repo, source, entry["transport_path"], cache)
        manifest = injury_tree_blob(repo, source, "state_storage.json", cache, missing=True)
        if entry["codec"] == "gzip-single-member":
            _, source_index = history._manifest_index(repo, source.state_commit, entry["manifest_sha256"])
            expected = {key: entry[key] for key in ("path", "raw_sha256", "raw_bytes", "blob_sha256", "compressed_bytes")}
            if (manifest is None or manifest["raw_sha256"] != entry["manifest_sha256"]
                    or source_index.get(logical_path) != expected):
                raise ValueError("probe_injury_source_manifest_record_mismatch")
        if (transport["raw_sha256"] != entry["blob_sha256"]
                or transport["bytes"] != entry["compressed_bytes"]):
            raise ValueError("probe_injury_transport_hash_mismatch")
    return {"route": "fixed_manifest_or_history_transport", "manifest_entry": entry,
            "source_manifest": manifest, "transport": transport,
            "native_fixed_reader_strict_restore_verified": True}


def injury_field_inventory(rows):
    objects = [row for row in rows if isinstance(row, dict)]
    present = Counter(key for row in objects for key in row)
    nulls = Counter(key for row in objects for key, value in row.items() if value is None)
    return {"row_count": len(rows), "object_row_count": len(objects),
            "field_present_row_counts": dict(sorted(present.items())),
            "field_null_row_counts": dict(sorted(nulls.items()))}


def injury_snapshot_audit(raw, league, match_id, snapshot_id):
    from football_model import fundamentals, history_archive as history
    value = history._manifest_json(raw)
    if not isinstance(value, dict):
        raise ValueError("probe_injury_snapshot_not_object")
    rows = value.get("raw_response")
    raw_digest = fundamentals._digest(rows) if "raw_response" in value else None
    normalized_id = fundamentals._digest({k: v for k, v in value.items() if k != "snapshot_id"})
    teams = value.get("teams")
    team_counts, records = [], []
    for tid, team in sorted(teams.items()) if isinstance(teams, dict) else []:
        if not isinstance(team, dict):
            continue
        own = team.get("records")
        if isinstance(own, list):
            records.extend(own)
        team_counts.append({"team_id": tid, "reported_count": team.get("reported_count"),
            "actual_record_count": len(own) if isinstance(own, list) else None,
            "duplicate_rows_removed": team.get("duplicate_rows_removed"),
            "complete_roster_status": team.get("complete_roster_status"),
            "missing_reason": team.get("missing_reason")})
    errors = []
    if any(key not in value for key in ("snapshot_id", "raw_sha256", "raw_response")):
        errors.append("required_internal_digest_or_response_field_missing")
    if (value.get("kind") != "injuries" or not isinstance(value.get("fixture"), dict)
            or value["fixture"].get("match_id") != match_id):
        errors.append("snapshot_kind_or_match_identity_mismatch")
    if value.get("snapshot_id") != snapshot_id or normalized_id != snapshot_id:
        errors.append("normalized_snapshot_id_mismatch")
    if value.get("raw_sha256") != raw_digest:
        errors.append("embedded_raw_response_digest_mismatch")
    try:
        fundamentals._validate_snapshot(value)
    except Exception:
        errors.append("native_snapshot_reconstruction_failed")
    try:
        known_before_cutoff = fundamentals._utc(value["known_at"]) <= fundamentals._utc(PERSONNEL_INJURY_FEATURE_CUTOFF)
    except (KeyError, TypeError, ValueError):
        known_before_cutoff = False
    if not known_before_cutoff:
        errors.append("snapshot_not_known_by_selected_feature_cutoff")
    duplicate_count = sum(t["duplicate_rows_removed"] for t in team_counts
                          if type(t["duplicate_rows_removed"]) is int)
    row_accounting = isinstance(rows, list) and len(rows) == len(records) + duplicate_count
    if not row_accounting:
        errors.append("raw_and_normalized_row_accounting_mismatch")
    return {"status": "passed" if not errors else "failed", "errors": errors,
        "league_label_source": "fixed_selection_report", "league": league, "match_id": match_id,
        "raw_file_sha256": sha(raw), "raw_file_bytes": len(raw),
        "expected_original_file_sha256_before_read": None, "expected_raw_response_digest_before_read": None,
        "expected_normalized_snapshot_id": snapshot_id, "embedded_snapshot_id": value.get("snapshot_id"),
        "computed_normalized_snapshot_id": normalized_id,
        "embedded_raw_response_digest_present": "raw_sha256" in value,
        "embedded_raw_response_digest": value.get("raw_sha256"), "computed_raw_response_digest": raw_digest,
        "snapshot_field_inventory": injury_field_inventory([value]),
        "raw_response_field_inventory": injury_field_inventory(rows) if isinstance(rows, list) else None,
        "raw_nested_field_inventory": {name: injury_field_inventory([
            row[name] for row in rows if isinstance(row, dict) and isinstance(row.get(name), dict)])
            for name in ("fixture", "team", "player")} if isinstance(rows, list) else None,
        "raw_nested_inventory_row_basis": "only raw_response rows whose named nested field is an object",
        "normalized_record_field_inventory": injury_field_inventory(records), "normalized_team_counts": team_counts,
        "raw_response_rows": len(rows) if isinstance(rows, list) else None,
        "normalized_record_rows": len(records), "duplicate_rows_removed": duplicate_count,
        "raw_rows_equal_normalized_plus_duplicates": row_accounting,
        "selected_feature_cutoff": PERSONNEL_INJURY_FEATURE_CUTOFF,
        "known_at": value.get("known_at"), "known_by_selected_feature_cutoff": known_before_cutoff,
        "native_validator": "football_model.fundamentals._validate_snapshot",
        "empty_response_is_verified_healthy": False, "injuries_entered_probability_model_claimed": False}


def personnel_injury_refs(repo, out):
    sys.path.insert(0, str(repo))
    from football_model import history_archive as history
    results, cache, original_bytes = [], {}, 0
    progress = NativeProgress("personnel_injury_refs", out)
    with history.FixedStateReader(repo, PERSONNEL_INJURY_SOURCE_STATE) as reader:
        validated_manifest = reader.validate_manifest()
        manifest = injury_tree_blob(repo, reader, "state_storage.json", cache)
        if manifest["raw_sha256"] != validated_manifest["sha256"]:
            raise ValueError("probe_injury_fixed_manifest_hash_mismatch")
        for league, match_id, snapshot_id in PERSONNEL_INJURY_REFS:
            logical = f"fundamentals/snapshots/injuries/{match_id}/{snapshot_id}.json"
            raw = reader.read(logical)
            original_bytes += len(raw)
            if original_bytes > PERSONNEL_INJURY_MAX_ORIGINAL_BYTES:
                raise ValueError("probe_injury_selected_originals_size_limit")
            provenance = injury_original_provenance(repo, reader, logical, raw, cache)
            destination = out / "personnel-injury-originals" / str(match_id) / (snapshot_id + ".json")
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(raw)
            audit = injury_snapshot_audit(raw, league, match_id, snapshot_id)
            results.append({"logical_path": logical, "original_artifact_path": destination.relative_to(out).as_posix(),
                            "provenance": provenance, **audit})
            write_json(out / "personnel-injury-refs-details.json", results)
            progress.emit("selected_injury_original_checked", match_id=match_id, status=audit["status"],
                          original_bytes=len(raw), completed_originals=len(results))
    summary = {"source_state_commit": PERSONNEL_INJURY_SOURCE_STATE,
        "selection_source_path": "audit/personnel-selected-injury-refs.json",
        "selection_source_raw_sha256": PERSONNEL_INJURY_SELECTION_SHA256,
        "selection_report_or_feature_reverified_in_this_phase": False,
        "scope": "exact_eight_preselected_fixed_53c88a9_refs; original_inventory_scope_unchanged",
        "fixed_state_manifest": manifest, "expected_originals": 8, "read_originals": len(results),
        "passed_originals": sum(r["status"] == "passed" for r in results),
        "original_artifact_bytes": original_bytes, "original_bytes_limit": PERSONNEL_INJURY_MAX_ORIGINAL_BYTES,
        "details_path": "personnel-injury-refs-details.json", "new_api_calls": 0, "new_fits": 0,
        "state_save_performed": False, "remote_write_performed": False,
        "current_probe_job_forecast_association_claimed": False}
    write_json(out / "personnel-injury-refs-summary.json", summary)
    if len(results) != 8 or summary["passed_originals"] != 8:
        raise ValueError("probe_injury_selected_refs_not_all_verified")
    return summary


def integration(repo, out):
    sys.path.insert(0, str(repo))
    from football_model.integration import match_packet
    runtime = repo / "runtime"
    config = json.loads((repo / "config.json").read_text())
    index = json.loads((runtime / "integration/index.json").read_text())
    matches = []
    for entry in index["competitions"]:
        attempt = entry.get("latest_successful_forecast") or {}
        if attempt.get("run_id"):
            path = runtime / "predictions" / attempt["run_id"] / (entry["key"] + ".json")
            if path.is_file():
                rows = json.loads(path.read_text())
                if any(row.get("match_id") == 1493186 for row in rows):
                    matches.append(entry["key"])
    if len(matches) != 1:
        raise ValueError("probe_match_1493186_not_unique_in_latest_saved_predictions")
    packet = match_packet(runtime, config, matches[0], 1493186, state_commit=marker(repo)["state_oid"])
    f5c = packet["f5c_comparison"]
    write_json(out / "integration-1493186-packet.json", packet)
    evidence = f5c.get("evidence", [])
    result = {"league": matches[0], "match_id": 1493186, "status": packet["status"],
        "read_and_recomputed": packet["provenance"]["read_and_recomputed"],
        "fit_performed": packet["provenance"]["fit_performed"],
        "model_file_sha256": packet["provenance"]["model_file_sha256"],
        "prediction_file_sha256": packet["provenance"]["prediction_file_sha256"],
        "f5c_status": f5c["status"], "f5c_forecast_id": f5c.get("forecast_id"),
        "f5c_production_parent_id": f5c.get("production_parent_id"),
        "f5c_alpha_zero_exactly_native": f5c.get("alpha_zero_exactly_native"),
        "f5c_grid_names": sorted(f5c.get("predictions", {})),
        "f5c_evidence_file_count": len(evidence), "f5c_evidence_raw_bytes": sum(e["bytes"] for e in evidence),
        "f5c_evidence_manifest_sha256": sha(json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode()),
        "f5c_score_status": f5c.get("score", {}).get("status"),
        "usage": "current engineering recomputation; no retrospective pre-match claim",
        "accuracy_improvement_verified": False, "new_api_calls": 0}
    if (result["read_and_recomputed"] is not True or result["fit_performed"] is not False
            or result["f5c_status"] != "verified_same_parent"
            or result["f5c_alpha_zero_exactly_native"] is not True or not evidence
            or len(result["f5c_grid_names"]) != 5):
        write_json(out / "integration-failure-summary.json", result)
        raise ValueError("probe_integration_or_f5c_same_parent_not_verified")
    return result


def durability(repo, out):
    sys.path.insert(0, str(repo))
    from football_model import advanced_durability as native, history_archive as history
    runtime = repo / "runtime"
    state = marker(repo)["state_oid"]
    def is_first(name):
        parts = name.split("/")
        return len(parts) == 7 and parts[0] == "advanced_snapshots" and parts[2] == "captures" and parts[-1] == "first.json"
    local = {p.relative_to(runtime).as_posix() for p in
             (runtime / "advanced_snapshots").glob("*/captures/*/*/*/first.json")}
    local.update(name for name in history._catalog(runtime) if is_first(name))
    fixed = {name.removeprefix("runtime/") for name in
        git(repo, "ls-tree", "-r", "-z", "--name-only", state, "--", "runtime/advanced_snapshots/").split("\0")
        if name.startswith("runtime/") and is_first(name.removeprefix("runtime/"))}
    attempts, completion_cache = [], {}
    progress = NativeProgress("durability", out)
    with history.FixedStateReader(repo, state) as reader:
        reader.validate_manifest()
        _, index = history._manifest_index(repo, state)
        fixed.update(name for name in index if is_first(name))
        fixed.update(name for name in history._state_catalog(repo, state) if is_first(name))
        write_json(out / "durability-first-inventory.json", {"actual_state": state,
            "local_first_paths": sorted(local), "fixed_state_first_paths": sorted(fixed),
            "inventory_equal": local == fixed})
        if local != fixed:
            raise ValueError("probe_durability_local_and_fixed_first_inventory_differ")
        if not fixed:
            raise ValueError("probe_durability_no_fixed_first_receipts")
        for name in sorted(fixed):
            attempt = {"first_path": name, "status": "failed"}
            try:
                raw = reader.read(name)
                if history.read_bytes(runtime / name) != raw:
                    raise ValueError("probe_durability_first_differs_from_fixed_state")
                receipt_ref = {"path": name, "sha256": sha(raw)}
                attempt["receipt_ref"] = receipt_ref
                proof = native.prove(repo, runtime, state, receipt_ref,
                    _completion_cache=completion_cache, _read_file=reader.read)
                attempt["proof"] = proof
                if (not isinstance(proof, dict) or proof.get("status") == "failed_verification_attempt"
                        or proof.get("verified") is not True or proof.get("closure_verified") is not True
                        or proof.get("method") != native.METHOD or proof.get("receipt_ref") != receipt_ref
                        or proof.get("state_commit") != state
                        or not isinstance(proof.get("closure_sha256"), str)
                        or len(proof["closure_sha256"]) != 64
                        or any(c not in "0123456789abcdef" for c in proof["closure_sha256"])
                        or type(proof.get("verified_file_count")) is not int or proof["verified_file_count"] < 1
                        or not isinstance(proof.get("remote_run_completion"), dict)
                        or proof["remote_run_completion"].get("state_commit") != state
                        or proof["remote_run_completion"].get("saved_commit_marker") != "MODEL_STATE_SAVED_COMMIT=" + state
                        or not proof.get("durable_saved_at") or not proof.get("service_evidence_verified_at")):
                    raise ValueError("probe_durability_exact_first_proof_not_verified")
                attempt["status"] = "passed"
            except Exception as exc:
                attempt["error_type"] = type(exc).__name__
                attempt["error_code"] = str(exc)[:300] if isinstance(exc, ValueError) else type(exc).__name__
                if isinstance(exc, native.DurabilityReferenceError):
                    attempt["failure"] = exc.details
            attempts.append(attempt)
            write_json(out / "durability-proofs-summary.json", attempts)
            progress.emit("fixed_first_durability_checked", first_path=name,
                          status=attempt["status"], completed_firsts=len(attempts), fixed_first_count=len(fixed))
    result = {"actual_state": state, "first_receipt_inventory": len(fixed),
        "historical_reference_originals_count": 1397,
        "historical_reference_scope": "unique originals in one anchor from old eaef60f12b25ee68885c8bd2f71ca7e655e85c8d; not a current anchor inventory or assertion",
        "method": native.METHOD, "verification_attempts": len(attempts), "new_local_proofs": 0,
        "verified": sum(p["status"] == "passed" for p in attempts),
        "unverified_or_failed": sum(p["status"] != "passed" for p in attempts),
        "verification_basis": "prove every exact first from the fixed saved tree, including previously verified receipts",
        "remote_service_evidence": "native verifier uses existing GitHub actions:read token",
        "local_runtime_modified": False, "state_save_performed": False, "remote_write_performed": False}
    if result["unverified_or_failed"] or result["verification_attempts"] != result["first_receipt_inventory"]:
        write_json(out / "durability-partial-summary.json", result)
        raise ValueError("probe_durability_has_unverified_closures")
    return result


def phase(args):
    repo, out = args.repo.resolve(), args.output.resolve()
    start, before = time.monotonic(), snapshot(repo)
    result = {"phase": args.phase, "started_at": now(), "before": before}
    code = 0
    try:
        result.update(status="passed", result=globals()[args.phase](repo, out))
    except Exception as exc:
        code = 1
        # Existing storage/verifier errors are handcrafted. Avoid tracebacks,
        # raw HTTP responses, remotes, credentials, or environment dumps.
        reason = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
        result.update(status="failed", error_type=type(exc).__name__, error_code=reason[:300])
    result.update(finished_at=now(), elapsed_seconds=time.monotonic() - start, after=snapshot(repo))
    write_json(out / (args.phase + ".json"), result)
    print(json.dumps({k: result[k] for k in ("phase", "status", "elapsed_seconds")}), flush=True)
    return code


def parse_time(path):
    result = {}
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            for title in ("Maximum resident set size (kbytes)", "Elapsed (wall clock) time (h:mm:ss or m:ss)",
                          "File system inputs", "File system outputs", "Exit status"):
                if line.startswith(title + ":"):
                    result[title] = line[len(title) + 1:].strip()
    return result


def run(args):
    repo, out = args.repo.resolve(), args.output.resolve()
    out.mkdir(parents=True, exist_ok=True)
    if not 30 <= args.budget_seconds <= RUN_BUDGET_SECONDS:
        raise ValueError("probe_budget_outside_bounded_job_window")
    deadline = time.monotonic() + args.budget_seconds
    runs = []
    for name, maximum in PHASE_LIMITS.items():
        if name == "durability" and not args.include_durability:
            continue
        remaining = int(deadline - time.monotonic() - FINALIZATION_RESERVE_SECONDS)
        if remaining < 30 or (name not in {"identity", "load"} and not (out / "load.json").exists()):
            runs.append({"phase": name, "status": "not_run", "reason": "budget_or_load_not_completed"})
            continue
        if name not in {"identity", "load"} and json.loads((out / "load.json").read_text())["status"] != "passed":
            runs.append({"phase": name, "status": "not_run", "reason": "load_failed"})
            continue
        limit = max(1, min(maximum, remaining))
        timing, log = out / (name + ".time.txt"), out / (name + ".log")
        command = ["/usr/bin/time", "-v", "-o", str(timing), "timeout", "--signal=TERM", "--kill-after=15s", str(limit),
                   sys.executable, str(Path(__file__).resolve()), "--phase", name,
                   "--repo", str(repo), "--output", str(out)]
        print("RESOURCE_PROBE_PHASE=" + name + " limit_seconds=" + str(limit), flush=True)
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   env={**os.environ, "FOOTBALL_API_KEY": "", "GIT_TERMINAL_PROMPT": "0"})
        logged = 0
        with log.open("wb") as stream:
            while chunk := process.stdout.readline(16 * 1024):
                allowed = max(0, 256 * 1024 - logged)
                stream.write(chunk[:allowed])
                stream.flush()
                logged += min(len(chunk), allowed)
                if chunk.startswith((b"RESOURCE_PROBE_PROGRESS=", b"MODEL_STATE_RESTORE_PREFLIGHT=")):
                    print(chunk.decode("utf-8", errors="replace").rstrip(), flush=True)
        code = process.wait()
        if not (out / (name + ".json")).exists():
            write_json(out / (name + ".json"), {"phase": name, "status": "failed",
                "exit_code": code, "error_code": "phase_timeout" if code in (124, 137) else "terminated_without_result",
                "limit_seconds": limit, "finished_at": now()})
        result = json.loads((out / (name + ".json")).read_text()) if (out / (name + ".json")).exists() else {}
        runs.append({"phase": name, "exit_code": code, "status": result.get("status", "terminated_without_result"),
                     "time_v": parse_time(timing)})
        if name == "identity" and code:
            break
    manifest = []
    total = 0
    for path in sorted(out.rglob("*")):
        if path.is_file():
            raw = path.read_bytes()
            manifest.append({"name": path.relative_to(out).as_posix(), "bytes": len(raw), "sha256": sha(raw)})
            total += len(raw)
    summary = {"schema_version": "r13-read-only-resource-probe-v1", "completed_at": now(),
        "source_sha": SOURCE_SHA, "requested_state": REQUESTED_STATE, "phases": runs,
        "new_fits": 0, "football_api_calls": 0, "state_saves": 0, "remote_writes": 0,
        "durability_optional_probe_requested": args.include_durability,
        "run_budget_seconds": args.budget_seconds, "workflow_total_limit_seconds": 1800,
        "finalization_reserve_seconds": FINALIZATION_RESERVE_SECONDS,
        "evidence": manifest, "evidence_bytes_before_summary": total,
        "artifact_limit_bytes": MAX_ARTIFACT, "all_executed_phases_passed": all(r.get("exit_code") == 0 for r in runs),
        "no_resource_result_implies_accuracy_improvement": True}
    write_json(out / "summary.json", summary)
    if sum(p.stat().st_size for p in out.rglob("*") if p.is_file()) > MAX_ARTIFACT:
        raise ValueError("probe_artifact_size_limit_exceeded")
    print(json.dumps({"phase": "run", "evidence_bytes": total, "all_executed_phases_passed": summary["all_executed_phases_passed"]}), flush=True)
    return int(not summary["all_executed_phases_passed"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=["run", *PHASE_LIMITS], default="run")
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path, default=Path("r13-resource-evidence"))
    parser.add_argument("--budget-seconds", type=int, default=RUN_BUDGET_SECONDS)
    parser.add_argument("--include-durability", action="store_true")
    args = parser.parse_args()
    return run(args) if args.phase == "run" else phase(args)


if __name__ == "__main__":
    raise SystemExit(main())
