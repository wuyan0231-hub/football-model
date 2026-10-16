"""Read-only R13 resource acceptance; never save state, fit, or call football API."""
from __future__ import annotations

import argparse
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
import time

SOURCE_SHA = "93b8a12f1011653895dae4b027ed1693868b74db"
SOURCE_TREE = "d106f31ac951ad78ec6309b542dcf86633e5e9a6"
REQUESTED_STATE = "eaef60f12b25ee68885c8bd2f71ca7e655e85c8d"
MAX_ARTIFACT = 16 * 1024 * 1024
MAX_JSON = 2 * 1024 * 1024
PHASE_LIMITS = {"identity": 90, "load": 780, "check": 180, "preflight": 180,
                "cold": 120, "integration": 180, "durability": 540}


def now():
    return datetime.now(timezone.utc).isoformat()


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
            result = subprocess.run(["du", "-sx", "--block-size=1", str(path)],
                                    capture_output=True, timeout=90)
            if result.returncode == 0:
                allocated[name] = int(result.stdout.split()[0])
    return {"at": now(), "disk_total_bytes": disk.total, "disk_used_bytes": disk.used,
            "disk_free_bytes": disk.free, "allocated_bytes": allocated,
            "process_max_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss, **fields}


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
    estimate = cloud._validate_runtime(repo / "runtime")
    return {"validation": "native cloud_state._validate_runtime", "actual_state": marker(repo)["state_oid"],
            "estimate": {k: v for k, v in estimate.items() if k != "retained_inline"}}


def preflight(repo, out):
    report = cloud_module(repo).preflight_state(repo)
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
    proofs = []
    actual_state = marker(repo)["state_oid"]
    with history.FixedStateReader(repo, actual_state) as state_reader:
        manifest = state_reader.validate_manifest()
        for entry in selected:
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
            "catalog": history.catalog_stats(runtime), "selected_actual_originals": proofs}


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
    from football_model.advanced_durability import stamp, METHOD
    runtime = repo / "runtime"
    inventory = list((runtime / "advanced_snapshots").glob("*/captures/*/*/*/first.json"))
    proofs = stamp(repo, runtime, marker(repo)["state_oid"])
    summaries = [{key: proof.get(key) for key in ("receipt_ref", "state_commit", "verified", "method",
                 "closure_sha256", "verified_file_count", "durable_saved_at", "service_evidence_verified_at")}
                 for proof in proofs]
    write_json(out / "durability-proofs-summary.json", summaries)
    result = {"actual_state": marker(repo)["state_oid"], "first_receipt_inventory": len(inventory),
        "expected_inventory_for_requested_state": 1397, "method": METHOD,
        "new_local_proofs": len(proofs), "verified": sum(p["verified"] is True for p in proofs),
        "unverified": sum(p["verified"] is not True for p in proofs),
        "remote_service_evidence": "native verifier uses existing GitHub actions:read token",
        "local_runtime_only": True, "state_save_performed": False, "remote_write_performed": False}
    if result["unverified"]:
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
    deadline = time.monotonic() + args.budget_seconds
    runs = []
    for name, maximum in PHASE_LIMITS.items():
        if name == "durability" and not args.include_durability:
            continue
        remaining = int(deadline - time.monotonic() - 90)
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
            while chunk := process.stdout.read(65536):
                allowed = max(0, 256 * 1024 - logged)
                stream.write(chunk[:allowed])
                logged += min(len(chunk), allowed)
        code = process.wait()
        result = json.loads((out / (name + ".json")).read_text()) if (out / (name + ".json")).exists() else {}
        runs.append({"phase": name, "exit_code": code, "status": result.get("status", "terminated_without_result"),
                     "time_v": parse_time(timing)})
        if name == "identity" and code:
            break
    manifest = []
    total = 0
    for path in sorted(out.iterdir()):
        if path.is_file():
            raw = path.read_bytes()
            manifest.append({"name": path.name, "bytes": len(raw), "sha256": sha(raw)})
            total += len(raw)
    summary = {"schema_version": "r13-read-only-resource-probe-v1", "completed_at": now(),
        "source_sha": SOURCE_SHA, "requested_state": REQUESTED_STATE, "phases": runs,
        "new_fits": 0, "football_api_calls": 0, "state_saves": 0, "remote_writes": 0,
        "durability_optional_probe_requested": args.include_durability,
        "evidence": manifest, "evidence_bytes_before_summary": total,
        "artifact_limit_bytes": MAX_ARTIFACT, "all_executed_phases_passed": all(r.get("exit_code") == 0 for r in runs),
        "no_resource_result_implies_accuracy_improvement": True}
    write_json(out / "summary.json", summary)
    if sum(p.stat().st_size for p in out.iterdir() if p.is_file()) > MAX_ARTIFACT:
        raise ValueError("probe_artifact_size_limit_exceeded")
    print(json.dumps({"phase": "run", "evidence_bytes": total, "all_executed_phases_passed": summary["all_executed_phases_passed"]}), flush=True)
    return int(not summary["all_executed_phases_passed"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=["run", *PHASE_LIMITS], default="run")
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path, default=Path("r13-resource-evidence"))
    parser.add_argument("--budget-seconds", type=int, default=1440)
    parser.add_argument("--include-durability", action="store_true")
    args = parser.parse_args()
    return run(args) if args.phase == "run" else phase(args)


if __name__ == "__main__":
    raise SystemExit(main())
