#!/usr/bin/env python3
"""Replay a fixed, hash-verified state offline without impersonating a new fetch.

This is an engineering acceptance tool, not a production forecast publisher.
Supply a reviewed manifest of original Git blobs and a separate empty output
directory. No /status response is fabricated: the tool calls process_competition
directly and records that account authentication was not tested. All calculation
completion times use the actual wall clock; only the training cutoff is reused.
"""
from __future__ import annotations

import argparse
import copy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from football_model import __version__
from football_model.cli import atomic_json, iso, parse_date, process_competition, TIME_SEMANTICS
from football_model.data import APIClient, APIError
from football_model.integration import build_index, match_packet, IntegrationError


def git_blob(raw):
    return hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()


def verified_files(root, entries):
    """Reject modified or missing original bytes before performing the replay."""
    receipts = []
    for entry in entries:
        relative = Path(entry["path"])
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("Manifest paths must stay inside the source directory")
        path = root / relative
        raw = path.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        if git_blob(raw) != entry["sha"] or (entry.get("sha256") and digest != entry["sha256"]):
            raise ValueError(f"Original Git blob mismatch: {entry['path']}")
        receipts.append({"path": entry["path"], "git_blob": entry["sha"],
                         "sha256": digest, "bytes": len(raw)})
    return receipts


class OfflineCacheClient(APIClient):
    """Reuse production parsing, while every possible network access is denied."""

    def __init__(self, cache, allowed_paths, max_requests):
        self.data_dir = cache
        self.allowed_paths = set(allowed_paths)
        self.max_requests = max_requests
        self.requests_made = 0
        self.daily_remaining = None
        self._reserved_requests = 0
        self.calls = []
        self.missing = []

    def _request(self, endpoint, params):
        raise APIError("Offline replay forbids network requests")

    def status(self):
        raise APIError("Account status was not cached; offline replay cannot verify authentication")

    def _cached_request(self, endpoint, params, *, force, max_age_seconds):
        identity = json.dumps([endpoint, params], sort_keys=True).encode("utf-8")
        name = hashlib.sha256(identity).hexdigest() + ".json"
        path = self.data_dir / "api_cache" / name
        call = {"endpoint": endpoint, "params": copy.deepcopy(params),
                "production_force_requested": force,
                "production_max_age_seconds": max_age_seconds,
                "source_file": f"runtime/cache/api_cache/{name}"}
        self.calls.append(call)
        if call["source_file"] not in self.allowed_paths or not path.is_file():
            call["status"] = "missing_verified_cache"
            self.missing.append(copy.deepcopy(call))
            raise APIError("离线固定状态中无已核验缓存，未调用网络：" + json.dumps([endpoint, params], ensure_ascii=False))
        envelope = json.loads(path.read_text(encoding="utf-8"))
        if (envelope.get("endpoint") != endpoint or envelope.get("params") != params
                or not isinstance(envelope.get("response"), list) or envelope.get("errors")
                or parse_date(envelope["fetched_at"]) > datetime.now(timezone.utc)):
            raise APIError("Invalid original offline cache")
        call.update(status="replayed_verified_cache", original_fetched_at=envelope["fetched_at"],
                    original_git_blob=git_blob(path.read_bytes()))
        # Cache timestamps and source responses remain verbatim. The change in
        # from_cache describes this read, not a new provider acquisition.
        return {**copy.deepcopy(envelope), "from_cache": True}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache-root", type=Path, required=True,
                        help="Root holding runtime/cache/api_cache from fixed state")
    parser.add_argument("--cache-manifest", type=Path, required=True)
    parser.add_argument("--baseline-root", type=Path, required=True,
                        help="Root holding original runtime and baseline models")
    parser.add_argument("--baseline-manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True,
                        help="New, separate output directory; must not already exist")
    parser.add_argument("--config", type=Path, default=ROOT / "config.json")
    parser.add_argument("--expected-new-match", type=int, default=1528939)
    parser.add_argument("--historical-match", type=int, default=1528918)
    args = parser.parse_args(argv)
    cache_manifest = json.loads(args.cache_manifest.read_text(encoding="utf-8"))
    baseline_manifest = json.loads(args.baseline_manifest.read_text(encoding="utf-8"))
    cache_receipts = verified_files(args.cache_root, cache_manifest["files"])
    baseline_receipts = verified_files(args.baseline_root, baseline_manifest)
    args.output.mkdir(parents=True, exist_ok=False)
    runtime = args.output / "runtime"
    shutil.copytree(args.baseline_root / "runtime", runtime)
    config = json.loads(args.config.read_text(encoding="utf-8"))
    competition = next(c for c in config["competitions"] if c["key"] == "unl")
    # The evidence package contains complete UNL forecasts, not every domestic
    # forecast file. Scope only the replay index; all modelling knobs stay exact.
    config = {**config, "competitions": [competition]}
    previous_model = json.loads((runtime / "models/unl.json").read_text(encoding="utf-8"))
    previous_index = json.loads((runtime / "integration/index.json").read_text(encoding="utf-8"))
    previous_entry = next(c for c in previous_index["competitions"] if c["key"] == "unl")
    previous_run = previous_entry["latest_successful_forecast"]["run_id"]
    previous_predictions = json.loads((runtime / "predictions" / previous_run / "unl.json").read_text(encoding="utf-8"))
    old_supported = {r["match_id"] for r in previous_predictions if r["status"] == "exploratory_baseline"}
    cutoff = parse_date(previous_model["model"]["training_cutoff"])
    started = datetime.now(timezone.utc)
    run_id = started.strftime("%Y%m%dT%H%M%S%fZ")
    client = OfflineCacheClient(args.cache_root / "runtime/cache",
                                [r["path"] for r in cache_receipts], config["max_requests_per_run"])
    item = process_competition(client, competition, config, runtime, cutoff, "train", run_id,
                               clock=lambda: datetime.now(timezone.utc), run_started=started)
    report = {"schema_version": 2, "time_semantics": TIME_SEMANTICS,
              "run_id": run_id, "run_started_at": iso(started), "training_cutoff": iso(cutoff),
              "generated_at": iso(datetime.now(timezone.utc)), "mode": "train",
              "program_version": __version__, "source_commit": None,
              "requested_competitions": ["unl"], "competitions": [item],
              "api_requests": 0, "exit_code": 0 if item["status"] == "探索性基线" else 1,
              "engineering_replay": {"offline": True, "production_publish": False,
                   "competition_scope": ["unl"],
                   "source_state_commit": cache_manifest["state_commit"],
                   "account_authentication": "not_tested_no_cached_status",
                   "timestamps": "actual_local_calculation_times; original_source_fetch_times_preserved",
                   "training_cutoff": "reused_only_for_comparable_engineering_replay"}}
    atomic_json(runtime / "reports/history" / f"{run_id}.json", report)
    summary = {"schema_version": 1, "status": "failed", "run_id": run_id,
               "engineering_replay": report["engineering_replay"],
               "source_manifest_sha256": hashlib.sha256(args.cache_manifest.read_bytes()).hexdigest(),
               "verified_cache_files": cache_receipts, "verified_baseline_files": baseline_receipts,
               "requests_made": client.requests_made, "cache_reads": client.calls,
               "missing_cache_queries": client.missing, "pipeline_item": item, "checks": {}}
    checks = summary["checks"]
    checks["pipeline_completed"] = item["status"] == "探索性基线"
    if checks["pipeline_completed"]:
        build_index(runtime, config, current_report=report)
        predictions = json.loads((runtime / "predictions" / run_id / "unl.json").read_text(encoding="utf-8"))
        supported = {r["match_id"] for r in predictions if r["status"] == "exploratory_baseline"}
        summary["forecast_coverage"] = {"old_supported": len(old_supported), "new_supported": len(supported),
                                       "added_ids": sorted(supported - old_supported),
                                       "removed_ids": sorted(old_supported - supported),
                                       "upcoming_total": len(predictions)}
        checks["old_supported_retained"] = old_supported <= supported
        checks["target_added"] = args.expected_new_match in supported - old_supported
        checks["historical_not_forecast"] = args.historical_match not in {r["match_id"] for r in predictions}
        packets = {}
        for match_id in sorted(supported):
            try:
                packet = match_packet(runtime, config, "unl", match_id)
                atomic_json(args.output / "packets" / f"{match_id}.json", packet)
                packets[str(match_id)] = {"read_and_recomputed": packet["provenance"]["read_and_recomputed"],
                                          "time_evidence": packet["provenance"].get("time_evidence")}
            except IntegrationError as exc:
                packets[str(match_id)] = {"error": str(exc)}
        summary["packets"] = packets
        checks["all_supported_recomputed"] = all(p.get("read_and_recomputed") is True for p in packets.values())
        try:
            match_packet(runtime, config, "unl", args.historical_match)
            checks["historical_call_refused"] = False
        except IntegrationError as exc:
            checks["historical_call_refused"] = True
            summary["historical_call_reason"] = str(exc)
        try:
            match_packet(runtime, config, "unl", args.expected_new_match, as_of=iso(cutoff))
            checks["old_cutoff_refused"] = False
        except IntegrationError as exc:
            checks["old_cutoff_refused"] = True
            summary["old_cutoff_reason"] = str(exc)
        try:
            match_packet(runtime, config, "unl", args.expected_new_match, as_of=iso(datetime.now(timezone.utc)))
            checks["unproven_historical_venue_refused"] = False
        except IntegrationError as exc:
            checks["unproven_historical_venue_refused"] = True
            summary["unproven_historical_venue_reason"] = str(exc)
        checks["generation_after_data_observed"] = all(
            parse_date(r["generated_at"]) >= parse_date(r["data_observed_at"])
            >= max(parse_date(s["fetched_at"]) for s in item["seasons"]) for r in predictions)
        coverage = runtime / "audits/unl" / f"{run_id}-coverage.json"
        if coverage.is_file():
            summary["coverage_audit"] = json.loads(coverage.read_text(encoding="utf-8"))
    # Original inputs are not altered by the replay or used as output roots.
    checks["original_inputs_unchanged"] = (
        verified_files(args.cache_root, cache_manifest["files"]) == cache_receipts and
        verified_files(args.baseline_root, baseline_manifest) == baseline_receipts)
    checks["zero_api_requests"] = client.requests_made == 0
    summary["status"] = "passed" if all(checks.values()) else "failed"
    summary["completed_at"] = iso(datetime.now(timezone.utc))
    atomic_json(args.output / "summary.json", summary)
    print(json.dumps({"status": summary["status"], "checks": checks,
                      "forecast_coverage": summary.get("forecast_coverage"),
                      "missing_cache_queries": len(client.missing), "summary": str(args.output / "summary.json")},
                     ensure_ascii=False, indent=2))
    return 0 if summary["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
