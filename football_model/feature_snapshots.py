"""Real-time feature/candidate captures with separate EARLY and LINEUP clocks.

This module consumes saved evidence. It never collects API data, changes the
baseline, backfills a historical cutoff, or inherits another experiment's
confirmation eligibility. The CLI deliberately has no ``--as-of`` option.
"""
from __future__ import annotations

import argparse
from collections import Counter
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re

from .data import DataError, _integer, _iso, _utc
from .fundamentals import _fixture, _validate_snapshot, build_features
from .integration import _entry_for_match, match_packet
from .settlement import matrix_array

VERSION = "feature-snapshots-1.0"
HORIZONS = ("EARLY", "LINEUP")
DEFAULT_WINDOWS = {"early_hours_min": 9.0, "early_hours_max": 11.0,
                   "early_target_hours": 10.0, "lineup_minutes_max": 90.0}
TIME_KEYS = {"fetched_at", "received_at", "observed_at", "context_observed_at",
             "known_at", "published_at", "created_at", "generated_at",
             "forecast_generated_at", "data_observed_at", "model_fitted_at",
             "run_started_at", "storage_started_at", "storage_completed_at",
             "persisted_at", "captured_at", "training_cutoff", "fit_cutoff",
             "analysis_as_of", "forecast_cutoff", "cutoff_at", "feature_cutoff",
             "collection_finished_at", "run_completed_at", "payload_completed_at"}


class SnapshotError(DataError):
    """A bounded reason code; raw provider/exception messages are not saved."""


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def _digest(value):
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _bytes(value):
    return (_canonical(value) + "\n").encode("utf-8")


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise SnapshotError("duplicate_json_key")
        result[key] = value
    return result


def _read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"), object_pairs_hook=_pairs,
                      parse_constant=lambda _: (_ for _ in ()).throw(SnapshotError("nonfinite_json")))


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _times(value):
    if isinstance(value, dict):
        for key, item in value.items():
            if key in TIME_KEYS and item is not None:
                yield _utc(item)
            elif isinstance(item, (dict, list)):
                yield from _times(item)
    elif isinstance(value, list):
        for item in value:
            yield from _times(item)


def _check_times(value, cutoff):
    values = list(_times(value))
    if any(when > cutoff for when in values):
        raise SnapshotError("source_time_after_cutoff")
    return max(values) if values else None


def _windows(protocol):
    result = {**DEFAULT_WINDOWS, **protocol.get("capture_windows", {})}
    if set(result) != set(DEFAULT_WINDOWS) or any(
            type(v) not in (int, float) or not math.isfinite(v) or v <= 0 for v in result.values()):
        raise SnapshotError("invalid_capture_windows")
    if not result["early_hours_min"] <= result["early_target_hours"] <= result["early_hours_max"]:
        raise SnapshotError("invalid_early_capture_window")
    if result["lineup_minutes_max"] / 60 >= result["early_hours_min"]:
        raise SnapshotError("horizon_windows_overlap")
    return result


def _horizon_state(fixture, horizon, cutoff, protocol):
    if horizon not in HORIZONS:
        raise SnapshotError("unknown_horizon")
    windows = _windows(protocol)
    kickoff = _utc(fixture["kickoff"])
    hours = (kickoff - cutoff).total_seconds() / 3600
    if horizon == "EARLY":
        lo, hi = windows["early_hours_min"], windows["early_hours_max"]
        target = kickoff - timedelta(hours=windows["early_target_hours"])
    else:
        lo, hi = 0, windows["lineup_minutes_max"] / 60
        target = None  # Publication/first observation, not an invented fixed time.
    state = "not_due" if hours > hi else "late" if hours < lo or hours <= 0 else "due"
    return {"status": state, "hours_before_kickoff": hours,
            "target_at": _iso(target) if target else None,
            "window_opens_at": _iso(kickoff - timedelta(hours=hi)),
            "window_closes_at": _iso(kickoff - timedelta(hours=lo)),
            "cutoff_at": _iso(cutoff)}


def _identity_equal(left, right):
    return {k: _fixture(left)[k] for k in ("match_id", "kickoff", "home_id", "away_id")} == {
        k: _fixture(right)[k] for k in ("match_id", "kickoff", "home_id", "away_id")}


def _reference_path(ref, runtime=None):
    if runtime is not None and ref.get("runtime_relative_path"):
        root = Path(runtime).resolve()
        relative = Path(ref["runtime_relative_path"])
        if relative.is_absolute() or ".." in relative.parts:
            raise SnapshotError("unsafe_source_path")
        path = (root / relative).resolve()
        if not path.is_relative_to(root):
            raise SnapshotError("source_path_escapes_runtime")
        return path
    return Path(ref["path"])


def _verify_refs(refs, cutoff, *, runtime=None):
    if not isinstance(refs, list) or not refs:
        raise SnapshotError("source_refs_required")
    snapshots = {}
    for ref in refs:
        if not isinstance(ref, dict) or not isinstance(ref.get("path"), str) or not re.fullmatch(
                r"[0-9a-f]{64}", ref.get("sha256", "")):
            raise SnapshotError("invalid_source_ref")
        path = _reference_path(ref, runtime)
        if not path.is_file() or _sha(path) != ref["sha256"]:
            raise SnapshotError("source_file_hash_mismatch")
        available = _utc(ref["available_at"])
        if available > cutoff:
            raise SnapshotError("source_available_after_cutoff")
        if path.suffix == ".json":
            value = _read(path)
            latest = _check_times(value, cutoff)
            if latest is not None and latest > available:
                raise SnapshotError("source_ref_backdates_availability")
            if isinstance(value, dict) and value.get("schema_version") == "fundamentals-evidence-v1":
                _validate_snapshot(value)
                if ref.get("snapshot_id") != value["snapshot_id"]:
                    raise SnapshotError("snapshot_source_identity_mismatch")
                snapshots[value["snapshot_id"]] = value
    return snapshots


def _feature_snapshot_ids(value):
    ids = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "snapshot_id" and item is not None:
                ids.add(item)
            elif key == "snapshot_ids":
                if not isinstance(item, list):
                    raise SnapshotError("invalid_feature_source_ids")
                ids.update(item)
            elif isinstance(item, (dict, list)):
                ids.update(_feature_snapshot_ids(item))
    elif isinstance(value, list):
        for item in value:
            ids.update(_feature_snapshot_ids(item))
    return ids


def _lineups_known(fixture, features, snapshots, cutoff):
    for tid in (fixture["home_id"], fixture["away_id"]):
        team = features.get("teams", {}).get(str(tid), {})
        lineup = team.get("lineups", {})
        if not isinstance(lineup, dict) or lineup.get("complete_starting_eleven") is not True:
            return False
        players = lineup.get("starters")
        if not isinstance(players, list) or len(players) != 11:
            return False
        ids = [p.get("player_id") for p in players if isinstance(p, dict)]
        if len(ids) != 11 or len(set(ids)) != 11 or any(type(pid) is not int or pid <= 0 for pid in ids):
            return False
        source = snapshots.get(lineup.get("snapshot_id"))
        if (source is None or source["kind"] != "lineups" or not _identity_equal(source["fixture"], fixture)
                or source["fixture"].get("status") != "NS" or _utc(source["known_at"]) > cutoff
                or lineup.get("known_at") != source["known_at"]):
            return False
        actual = source["teams"][str(tid)]
        if actual.get("complete_starting_eleven") is not True or actual.get("starters") != players:
            return False
    return True


def _candidate_hash(protocol):
    supplied = protocol.get("candidate_sha256")
    if supplied is not None:
        if not isinstance(supplied, str) or not re.fullmatch(r"[0-9a-f]{64}", supplied):
            raise SnapshotError("invalid_candidate_sha256")
        return supplied
    return _digest({"candidate": protocol.get("candidate", {}),
                    "snapshot_implementation_sha256": _sha(__file__)})


def snapshot_from_evidence(fixture, baseline, features, *, horizon, cutoff_at,
                           source_refs, protocol, clock=None):
    """Validate and construct an unpersisted engineering snapshot.

    Source references must resolve to actual, SHA-verified files. A constructed
    object has no live/persisted eligibility; only ``capture_run`` can create the
    completion receipt using the actual wall clock. Historical callers cannot
    gain prospective qualification by supplying a historical ``cutoff_at``.
    ``clock`` is an internal test hook, never a command-line argument.
    """
    identity, cutoff = _fixture(fixture), _utc(cutoff_at)
    if not isinstance(protocol, dict) or not isinstance(features, dict) or not isinstance(baseline, dict):
        raise SnapshotError("invalid_snapshot_inputs")
    state = _horizon_state(identity, horizon, cutoff, protocol)
    if state["status"] != "due":
        raise SnapshotError("horizon_" + state["status"])
    if identity.get("status") != "NS":
        raise SnapshotError("fixture_not_scheduled")
    if not _identity_equal(features.get("fixture", {}), identity):
        raise SnapshotError("feature_fixture_identity_mismatch")
    if _utc(features["forecast_cutoff"]) > cutoff:
        raise SnapshotError("features_after_cutoff")
    _check_times(features, cutoff)
    _check_times(baseline, cutoff)
    if "fixture" in baseline and not _identity_equal(baseline["fixture"], identity):
        raise SnapshotError("baseline_fixture_identity_mismatch")
    prediction = baseline.get("prediction", baseline)
    matrix_array(prediction.get("score_matrix"))
    snapshots = _verify_refs(source_refs, cutoff)
    if not _feature_snapshot_ids(features) <= snapshots.keys():
        raise SnapshotError("feature_source_not_preserved")
    known_lineups = _lineups_known(identity, features, snapshots, cutoff)
    if horizon == "LINEUP" and not known_lineups:
        raise SnapshotError("confirmed_lineups_not_observed")
    created = _utc((clock or (lambda: datetime.now(timezone.utc)))())
    if created < cutoff:
        raise SnapshotError("capture_clock_before_cutoff")
    result = {"schema_version": VERSION, "fixture": identity, "horizon": horizon,
              "actual_cutoff_at": _iso(cutoff), "created_at": _iso(created),
              "horizon_window": state, "protocol_sha256": _digest(protocol),
              "candidate_sha256": _candidate_hash(protocol), "protocol": deepcopy(protocol),
              "source_refs": deepcopy(source_refs), "baseline": deepcopy(baseline),
              "features": deepcopy(features), "lineups_unknown": not known_lineups,
              "quote_status": "unknown", "quote_unknown": True,
              "lineup_adjusted": False, "baseline_probabilities_changed": False,
              "candidate_status": "not_run", "candidate": None,
              "eligibility": "engineering_only", "confirmation_eligible": False,
              "f5_eligibility_inherited": False, "production_ledger_eligible": False,
              "live_capture_verified": False, "storage_completed_at": None}
    result["snapshot_id"] = _digest(result)
    return result


def verify_snapshot(snapshot, *, runtime=None):
    """Verify the frozen payload and all saved parent bytes, without fitting."""
    if not isinstance(snapshot, dict) or snapshot.get("schema_version") != VERSION:
        raise SnapshotError("invalid_snapshot_schema")
    expected = _digest({k: v for k, v in snapshot.items() if k != "snapshot_id"})
    if snapshot.get("snapshot_id") != expected:
        raise SnapshotError("snapshot_payload_hash_mismatch")
    cutoff = _utc(snapshot["actual_cutoff_at"])
    _verify_refs(snapshot["source_refs"], cutoff, runtime=runtime)
    if snapshot.get("confirmation_eligible") is not False or snapshot.get("f5_eligibility_inherited") is not False:
        raise SnapshotError("invalid_experiment_eligibility")
    if snapshot.get("candidate_status") == "predicted":
        candidate = snapshot["candidate"]
        card_refs = [ref for ref in snapshot["source_refs"] if ref.get("kind") == "candidate_model"]
        if len(card_refs) != 1:
            raise SnapshotError("candidate_model_parent_missing")
        card = _read(_reference_path(card_refs[0], runtime))
        if candidate.get("model_hash") != card.get("model_hash") or _digest(card["correction_model"]) != card["model_hash"]:
            raise SnapshotError("candidate_model_hash_mismatch")
        from .feature_correction import predict_correction
        from .integration import _same
        expected = predict_correction(card["correction_model"], candidate["prediction"]["baseline_prediction"],
                                      candidate["feature_values"]["home"], candidate["feature_values"]["away"])
        _same(candidate["prediction"], expected, "candidate")
    return True


def _write_bytes(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError:
        if path.read_bytes() != payload:
            raise SnapshotError("immutable_file_collision") from None
    return path


def _save(root, category, value):
    raw = _bytes(value)
    sha = hashlib.sha256(raw).hexdigest()
    return _write_bytes(root / category / (sha + ".json"), raw)


def _path_ref(path, runtime):
    return {"path": str(path.relative_to(runtime)), "sha256": _sha(path)}


def _freeze_source(path, runtime, root, cutoff, kind):
    path = Path(path).resolve()
    runtime = runtime.resolve()
    if not path.is_relative_to(runtime):
        raise SnapshotError("source_path_escapes_runtime")
    raw = path.read_bytes()
    sha = hashlib.sha256(raw).hexdigest()
    suffix = path.suffix if path.suffix in (".json", ".py") else ".bin"
    saved = _write_bytes(root / "sources" / (sha + suffix), raw)
    value = _read(saved) if suffix == ".json" else None
    latest = _check_times(value, cutoff) if value is not None else None
    ref = {"path": str(saved), "runtime_relative_path": str(saved.relative_to(runtime)),
           "origin_runtime_path": str(path.relative_to(runtime)), "sha256": sha,
           "kind": kind, "available_at": _iso(latest or cutoff)}
    if isinstance(value, dict) and value.get("schema_version") == "fundamentals-evidence-v1":
        ref["snapshot_id"] = value["snapshot_id"]
    return ref


def _fixture_context(runtime, fixture, cutoff):
    candidates = []
    for path in sorted((runtime / "fundamentals" / "fixtures" / str(fixture["match_id"])).glob("*.json")):
        value = _read(path)
        envelope = value.get("fixture_response", {})
        if envelope.get("endpoint") != "/fixtures" or envelope.get("params") != {"id": fixture["match_id"]} or envelope.get("errors"):
            raise SnapshotError("invalid_fixture_context")
        rows = envelope.get("response")
        if not isinstance(rows, list) or len(rows) != 1:
            raise SnapshotError("invalid_fixture_response_count")
        if envelope.get("paging", {}).get("total", 1) != 1:
            raise SnapshotError("partial_fixture_context")
        if not envelope.get("fetched_at"):
            raise SnapshotError("fixture_context_observation_missing")
        known = _check_times(value, cutoff)
        if known is None:
            raise SnapshotError("fixture_context_observation_missing")
        normalized = _fixture(rows[0])
        if not _identity_equal(normalized, fixture):
            raise SnapshotError("fixture_identity_changed")
        candidates.append((known, path, normalized))
    if not candidates:
        raise SnapshotError("current_fixture_context_missing")
    latest = max(when for when, _, _ in candidates)
    choices = [(path, item) for when, path, item in candidates if when == latest]
    if len({_canonical(item) for _, item in choices}) > 1:
        raise SnapshotError("fixture_context_same_time_conflict")
    path, identity = choices[0]
    return {**identity, "observed_at": _iso(latest)}, path


def _evidence_inputs(runtime, fixture, cutoff):
    snapshots, paths, history, excluded = [], [], [], []
    for kind in ("injuries", "lineups", "statistics"):
        for path in sorted((runtime / "fundamentals" / "snapshots" / kind).glob("*/*.json")):
            value = _read(path)
            _validate_snapshot(value)
            source = value["fixture"]
            if not {fixture["home_id"], fixture["away_id"]} & {source["home_id"], source["away_id"]}:
                continue
            if _utc(value["known_at"]) > cutoff:
                excluded.append({"path": str(path.relative_to(runtime)), "sha256": _sha(path),
                                 "reason": "known_after_actual_cutoff"})
                continue
            snapshots.append(value)
            paths.append(path)
            if source["match_id"] != fixture["match_id"] and source.get("status") in ("FT", "AET", "PEN"):
                history.append({**source, "observed_at": value["known_at"]})
    # Context responses give a finished-status observation for rest/workload,
    # including fixtures whose statistics or lineups were not supplied.
    for path in sorted((runtime / "fundamentals" / "fixtures").glob("*/*.json")):
        value = _read(path)
        envelope = value.get("fixture_response", {})
        rows = envelope.get("response", [])
        if not isinstance(rows, list) or len(rows) != 1:
            continue
        item = _fixture(rows[0])
        if item["match_id"] == fixture["match_id"] or not {fixture["home_id"], fixture["away_id"]} & {item["home_id"], item["away_id"]}:
            continue
        times = list(_times(value))
        if not times or max(times) > cutoff:
            continue
        if item.get("status") in ("FT", "AET", "PEN") and _utc(item["kickoff"]) < cutoff:
            history.append({**item, "observed_at": _iso(max(times))})
            paths.append(path)
    return snapshots, history, list(dict.fromkeys(paths)), excluded


def _safe_reason(exc):
    return str(exc) if isinstance(exc, SnapshotError) else "invalid_saved_evidence"


def _tick(clock, previous):
    current = _utc(clock())
    if current < previous:
        raise SnapshotError("capture_clock_moved_backwards")
    return current


def verify_capture(runtime, receipt):
    """Verify payload bytes, completion time and capture eligibility."""
    runtime = Path(runtime)
    ref = receipt["payload_ref"]
    path = _reference_path({"path": ref["path"], "runtime_relative_path": ref["path"]}, runtime)
    if not path.is_file() or _sha(path) != ref["sha256"]:
        raise SnapshotError("capture_payload_file_hash_mismatch")
    snapshot = _read(path)
    verify_snapshot(snapshot, runtime=runtime)
    cutoff, created = _utc(snapshot["actual_cutoff_at"]), _utc(snapshot["created_at"])
    started, completed = _utc(receipt["capture_started_at"]), _utc(receipt["storage_completed_at"])
    if not started <= cutoff <= created <= completed:
        raise SnapshotError("capture_completion_time_order_invalid")
    state = _horizon_state(snapshot["fixture"], snapshot["horizon"], completed, snapshot["protocol"])
    valid = state["status"] == "due"
    if receipt.get("live_capture_verified") is not valid or receipt.get("status") != ("captured" if valid else "late"):
        raise SnapshotError("capture_completion_eligibility_mismatch")
    if receipt.get("confirmation_eligible") is not False:
        raise SnapshotError("invalid_capture_confirmation_eligibility")
    return snapshot


def _cumulative_counts(root, runtime):
    by_horizon = {h: set() for h in HORIZONS}
    candidates = {h: set() for h in HORIZONS}
    issues = []
    for path in sorted((root / "captures").glob("*/*/*/first.json")):
        try:
            descriptor = _read(path)
            receipt_path = runtime / descriptor["path"]
            if _sha(receipt_path) != descriptor["sha256"]:
                raise SnapshotError("first_capture_receipt_hash_mismatch")
            receipt = _read(receipt_path)
            snapshot = verify_capture(runtime, receipt)
            by_horizon[snapshot["horizon"]].add((snapshot["league"], snapshot["fixture"]["match_id"]))
        except (ValueError, KeyError, TypeError, OSError) as exc:
            issues.append({"path": str(path.relative_to(runtime)), "reason": _safe_reason(exc)})
    for path in sorted((root / "captures").glob("*/*/*/first_candidate.json")):
        try:
            descriptor = _read(path)
            receipt_path = runtime / descriptor["path"]
            if _sha(receipt_path) != descriptor["sha256"]:
                raise SnapshotError("first_capture_receipt_hash_mismatch")
            snapshot = verify_capture(runtime, _read(receipt_path))
            if snapshot["candidate_status"] != "predicted":
                raise SnapshotError("first_candidate_not_predicted")
            candidates[snapshot["horizon"]].add((snapshot["league"], snapshot["fixture"]["match_id"]))
        except (ValueError, KeyError, TypeError, OSError) as exc:
            issues.append({"path": str(path.relative_to(runtime)), "reason": _safe_reason(exc)})
    observations = {}
    for path in sorted((root / "queue/history").glob("*.json")):
        value = _read(path)
        key = (value["league"], value["match_id"], value["horizon"])
        if key not in observations or _utc(value["queue_observed_at"]) > _utc(observations[key]["queue_observed_at"]):
            observations[key] = value
    union = set().union(*by_horizon.values())
    return {"captured_independent_matches": len(union),
            "captured_by_horizon": {h: len(ids) for h, ids in by_horizon.items()},
            "captured_horizon_records": sum(len(ids) for ids in by_horizon.values()),
            "captured_candidate_independent_matches": len(set().union(*candidates.values())),
            "captured_candidates_by_horizon": {h: len(ids) for h, ids in candidates.items()},
            "cumulative_planned_match_count": len({key[:2] for key in observations}),
            "cumulative_planned_horizon_count": len(observations),
            "cumulative_queue_latest_status_counts": dict(Counter(v["status"] for v in observations.values())),
            "existing_capture_validation_failures": issues}


def capture_run(runtime, league="serie_a", *, source_commit, protocol, state_commit=None,
                config=None, clock=None):
    """Capture both planned horizons from saved current evidence; no API calls.

    Return ``(exit_code, report)``. Every forecast row has two queue observations,
    including not-due, late, unsupported and ineligible cases. Versions append;
    the first legal capture is immutable and independent counts use match IDs.
    """
    if not isinstance(source_commit, str) or not re.fullmatch(r"[0-9a-fA-F]{40,64}", source_commit):
        raise SnapshotError("source_commit_full_sha_required")
    if state_commit is not None and not re.fullmatch(r"[0-9a-fA-F]{40,64}", state_commit):
        raise SnapshotError("state_commit_full_sha_required")
    if not isinstance(protocol, dict):
        raise SnapshotError("protocol_object_required")
    _windows(protocol)
    if not isinstance(league, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", league):
        raise SnapshotError("unsafe_league_key")
    runtime = Path(runtime).resolve()
    if any((base / "RESEARCH_REPLAY.json").exists() for base in (runtime, runtime.parent)):
        raise SnapshotError("research_replay_cannot_capture_live")
    config = config if config is not None else _read(Path("config.json"))
    clock = clock or (lambda: datetime.now(timezone.utc))
    started = _utc(clock())
    root = runtime / "feature_snapshots" / _digest(protocol)
    root.mkdir(parents=True, exist_ok=True)
    report = {"schema_version": VERSION, "run_started_at": _iso(started), "league": league,
              "source_commit": source_commit, "state_commit": state_commit,
              "state_commit_verification": "caller_supplied_not_git_verified" if state_commit else "not_supplied",
              "protocol_sha256": _digest(protocol), "candidate_sha256": _candidate_hash(protocol),
              "eligibility": "engineering_only", "confirmation_eligible": False,
              "api_collection_performed": False, "api_requests": 0,
              "baseline_models_changed": False, "queue": [], "issues": []}
    # A process lock is not model evidence; keep it outside persisted runtime.
    lock_root=runtime.parent / ".feature-snapshot-locks"
    lock_root.mkdir(parents=True,exist_ok=True)
    lock_name=hashlib.sha256((str(runtime)+_digest(protocol)).encode()).hexdigest()+".lock"
    with (lock_root / lock_name).open("a") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            entry = _entry_for_match(runtime, config, league)
            attempt = entry.get("latest_successful_forecast")
            if not attempt:
                report["issues"].append({"reason": "league_has_no_saved_forecast", "index_status": entry["status"]})
                rows = []
            else:
                rows = _read(runtime / "predictions" / attempt["run_id"] / (league + ".json"))
                if not isinstance(rows, list):
                    raise SnapshotError("forecast_rows_not_list")
            seen = set()
            for row in rows:
                identity = _fixture(row)
                if identity["match_id"] in seen:
                    raise SnapshotError("duplicate_forecast_fixture")
                seen.add(identity["match_id"])
                for horizon in HORIZONS:
                    attempt_started = _tick(clock, started)
                    state = _horizon_state(identity, horizon, attempt_started, protocol)
                    item = {"league": league, "match_id": identity["match_id"], "kickoff": identity["kickoff"],
                            "horizon": horizon, "forecast_status": row.get("status"),
                            "queue_observed_at": _iso(attempt_started), "planned_state_at_observation": state["status"], **state}
                    report["queue"].append(item)
                    if row.get("status") != "exploratory_baseline" or not isinstance(row.get("prediction"), dict):
                        item.update(status="unsupported", reason=row.get("reason", "baseline_forecast_unsupported"))
                    elif entry["status"] != "available":
                        item.update(status="ineligible", reason="latest_league_forecast_not_available")
                    elif state["status"] != "due":
                        item["reason"] = "horizon_" + state["status"]
                    else:
                        payload_path = None
                        try:
                            context, context_path = _fixture_context(runtime, identity, attempt_started)
                            if context["status"] != "NS":
                                raise SnapshotError("current_fixture_not_scheduled")
                            snapshots, history, paths, excluded = _evidence_inputs(runtime, context, attempt_started)
                            packet = match_packet(runtime, config, league, identity["match_id"],
                                                  as_of=_iso(attempt_started), state_commit=state_commit)
                            cutoff = _tick(clock, attempt_started)
                            features = build_features(context, cutoff=cutoff, snapshots=snapshots, fixture_history=history)
                            parents = [(runtime / "integration" / "index.json", "integration_index"),
                                       (context_path, "current_fixture_context")]
                            for field, kind in (("model_file", "baseline_model"), ("prediction_file", "baseline_forecast"),
                                                ("report_file", "baseline_report")):
                                relative = packet["provenance"][field]
                                if relative.startswith("runtime/"):
                                    relative = relative[len("runtime/"):]
                                path = runtime / relative
                                expected = packet["provenance"].get(field + "_sha256")
                                if expected is not None and _sha(path) != expected:
                                    raise SnapshotError("baseline_parent_changed_during_capture")
                                parents.append((path, kind))
                            refs = [_freeze_source(path, runtime, root, cutoff, kind) for path, kind in parents]
                            refs.extend(_freeze_source(path, runtime, root, cutoff, "fundamentals") for path in paths)
                            payload = snapshot_from_evidence(context, packet, features, horizon=horizon,
                                cutoff_at=cutoff, source_refs=refs, protocol=protocol, clock=clock)
                            payload.update(league=league, source_commit=source_commit, state_commit=state_commit,
                                           capture_started_at=_iso(attempt_started), excluded_read_evidence=excluded)
                            # Candidate integration is deliberately separate from baseline.
                            _attach_candidate(payload, runtime, root, cutoff)
                            built_at = _tick(clock, _utc(payload["created_at"]))
                            payload["created_at"] = _iso(built_at)
                            payload["payload_completed_at"] = _iso(built_at)
                            if payload["candidate_status"] == "predicted":
                                payload["candidate"]["generated_at"] = _iso(built_at)
                            payload.pop("snapshot_id", None)
                            payload["snapshot_id"] = _digest(payload)
                            payload_path = _save(root, "payloads", payload)
                            finished = _tick(clock, _utc(payload["created_at"]))
                            completion = _horizon_state(context, horizon, finished, protocol)
                            live = completion["status"] == "due"
                            receipt = {"schema_version": VERSION, "league": league,
                                "match_id": identity["match_id"], "horizon": horizon,
                                "protocol_sha256": report["protocol_sha256"],
                                "candidate_sha256": report["candidate_sha256"],
                                "capture_started_at": _iso(attempt_started), "actual_cutoff_at": _iso(cutoff),
                                "storage_completed_at": _iso(finished), "payload_ref": _path_ref(payload_path, runtime),
                                "live_capture_verified": live, "status": "captured" if live else "late",
                                "eligibility": "engineering_only", "confirmation_eligible": False}
                            verify_capture(runtime, receipt)
                            receipt_path = _save(root, f"captures/{league}/{identity['match_id']}/{horizon}/versions", receipt)
                            first = root / "captures" / league / str(identity["match_id"]) / horizon / "first.json"
                            if live and not first.exists():
                                _write_bytes(first, _bytes(_path_ref(receipt_path, runtime)))
                                item["first_legal_capture"] = True
                            else:
                                item["first_legal_capture"] = False
                            first_candidate = first.with_name("first_candidate.json")
                            if live and payload["candidate_status"] == "predicted" and not first_candidate.exists():
                                _write_bytes(first_candidate, _bytes(_path_ref(receipt_path, runtime)))
                            item.update(status=receipt["status"], receipt_ref=_path_ref(receipt_path, runtime),
                                        actual_cutoff_at=_iso(cutoff), storage_completed_at=_iso(finished),
                                        candidate_status=payload["candidate_status"])
                            item["evidence_status"] = receipt["status"]
                            if live and payload["candidate_status"] != "predicted":
                                item.update(status="ineligible", reason=payload["candidate_missing_reason"])
                            if not live:
                                item["reason"] = "storage_completed_outside_horizon"
                        except (ValueError, KeyError, TypeError, OSError) as exc:
                            item.update(status="ineligible", reason=_safe_reason(exc))
                            if payload_path is not None:
                                item["diagnostic_payload_ref"] = _path_ref(payload_path, runtime)
                    attempt_path = _save(root, "queue/history", item)
                    item["queue_record_ref"] = _path_ref(attempt_path, runtime)
                    if state["status"] in ("due", "late"):
                        first_attempt = root / "captures" / league / str(identity["match_id"]) / horizon / "first_attempt.json"
                        if not first_attempt.exists():
                            _write_bytes(first_attempt, _bytes(item["queue_record_ref"]))
        except (ValueError, KeyError, TypeError, OSError) as exc:
            report["issues"].append({"reason": _safe_reason(exc), "stage": "index_or_forecast_load"})
        report.update(_cumulative_counts(root, runtime))
        report["issues"].extend({**item, "stage": "existing_capture_validation"}
                                 for item in report["existing_capture_validation_failures"])
        report["queue_status_counts"] = dict(Counter(item["status"] for item in report["queue"]))
        report["planned_match_count"] = len({item["match_id"] for item in report["queue"]})
        report["planned_horizon_count"] = len(report["queue"])
        report["captured_versions_this_run"] = sum(item["status"] == "captured" for item in report["queue"])
        report["captured_evidence_versions_this_run"] = sum(item.get("evidence_status") == "captured" for item in report["queue"])
        report["candidate_status_counts"] = dict(Counter(item.get("candidate_status", "not_attempted") for item in report["queue"]))
        report["confirmation_independent_matches"] = 0
        report["run_completed_at"] = _iso(_tick(clock, started))
        report["status"] = "partial" if report["issues"] else "complete" if report["queue"] else "no_saved_forecasts"
        report_path = _save(root, "reports/history", report)
        report["report_ref"] = _path_ref(report_path, runtime)
        temporary = root / "reports" / ".latest.tmp"
        temporary.write_bytes(_bytes(report))
        temporary.replace(root / "reports" / "latest.json")
        return int(bool(report["issues"])), report


def _attach_candidate(payload, runtime, root, cutoff):
    """Optional correction model adapter; absent models remain explicit."""
    payload["candidate_status"] = "model_unavailable"
    payload["candidate_missing_reason"] = "correction_model_not_saved"
    model_path = runtime / "feature_models" / "models" / payload["league"] / "latest.json"
    if not model_path.exists():
        return
    try:
        card = _read(model_path)
        _check_times(card, cutoff)
        if (card.get("schema_version") != "feature-model-card-v1" or card.get("league") != payload["league"]
                or card.get("usage") != "engineering_shadow_only" or card.get("lineup_adjusted") is not False):
            raise SnapshotError("candidate_model_card_incompatible")
        if card.get("protocol_hash") != payload["protocol_sha256"]:
            raise SnapshotError("candidate_protocol_mismatch")
        if card.get("model_hash") != _digest(card["correction_model"]):
            raise SnapshotError("candidate_model_hash_mismatch")
        if _utc(card["training_cutoff"]) > cutoff or card["training_cutoff"] != card["correction_model"]["training_cutoff"]:
            raise SnapshotError("candidate_training_cutoff_invalid")
        from .feature_dataset import FEATURE_NAMES, feature_values
        from .feature_correction import predict_correction
        if card.get("feature_names") != list(FEATURE_NAMES):
            raise SnapshotError("candidate_feature_definition_mismatch")
        scope = card.get("feature_scope")
        data_path = runtime / "data" / (payload["league"] + ".json")
        data = _read(data_path)
        _check_times(data, cutoff)
        parents = [(model_path, "candidate_model"), (data_path, "candidate_schedule_history")]
        for ref in card.get("source_refs",[]):
            rel=Path(ref["path"])
            if rel.is_absolute() or ".." in rel.parts:
                raise SnapshotError("candidate_training_source_path_invalid")
            source=runtime/rel
            if not source.is_file() or _sha(source)!=ref["sha256"]:
                raise SnapshotError("candidate_training_source_hash_mismatch")
            parents.append((source,"candidate_training_source"))
        events = None
        history = data["matches"]
        coverage = {"scope": "same_competition", "all_club_competitions_complete": False}
        if scope == "all_club_competitions_provider_scope":
            from .feature_schedule import load_schedule
            schedule_path = runtime / "feature_models" / "schedules" / payload["league"] / "latest.json"
            _check_times(_read(schedule_path), cutoff)
            history, coverage = load_schedule(runtime, payload["league"])
            if coverage.get("scope") != scope or coverage.get("provider_queries_complete") is not True:
                raise SnapshotError("candidate_schedule_scope_incomplete")
            events = coverage.get("workload_events", [])
            parents.append((schedule_path, "all_club_schedule"))
        elif scope != "same_competition":
            raise SnapshotError("candidate_feature_scope_unknown")
        values = feature_values(payload["fixture"], history, _iso(cutoff),
                                schedule_events=events, scope=scope)
        if scope == "all_club_competitions_provider_scope":
            parents.extend(_used_schedule_sources(values, history, events, coverage, runtime, cutoff))
        # Historical-delay arithmetic is combined with actually saved source
        # clocks here, rather than inventing old API publication timestamps.
        for when in values.get("feature_available_at", {}).values():
            if _utc(when) > cutoff:
                raise SnapshotError("candidate_feature_after_cutoff")
        values["availability_basis"] = "observed_saved_schedule_at_real_capture_with_4h_maturity_rule"
        values["prospective_qualified"] = False
        baseline = deepcopy(payload["baseline"]["prediction"])
        baseline.update(match_id=payload["fixture"]["match_id"], kickoff=payload["fixture"]["kickoff"],
                        cutoff_at=_iso(cutoff), training_cutoff=payload["baseline"]["provenance"]["training_cutoff"])
        prediction = predict_correction(card["correction_model"], baseline, values["home"], values["away"])
        refs = [_freeze_source(path, runtime, root, cutoff, kind) for path, kind in parents]
        _verify_refs(refs, cutoff)
        payload["source_refs"].extend(refs)
        payload.update(candidate_status="predicted", candidate_missing_reason=None,
            candidate={"candidate_id": card["candidate_id"], "model_hash": card["model_hash"],
                       "training_cutoff": card["training_cutoff"], "prediction": prediction,
                       "feature_values": values, "feature_scope": scope, "schedule_coverage": coverage,
                       "lineup_adjusted": False, "used_in_probabilities": list(FEATURE_NAMES),
                       "accuracy_improvement_verified": False, "return_improvement_verified": False})
    except (ImportError, ValueError, KeyError, TypeError, OSError) as exc:
        payload.update(candidate_status="ineligible", candidate_missing_reason=_safe_reason(exc))


def _used_schedule_sources(values, history, events, coverage, runtime, cutoff):
    """Close actual four-feature inputs against the collector's raw responses.

    Only previous/recent fixture IDs contributing to this prediction are used.
    A report reference, a normalized row or a claimed complete query does not
    substitute for the original response bytes and identity checks.
    """
    ids = set()
    for evidence in values["evidence"].values():
        if evidence.get("previous_match_id") is not None:
            ids.add(evidence["previous_match_id"])
        ids.update(evidence.get("recent_match_ids", []))
    rows = {row["match_id"]: row for row in events or []}
    rows.update({row["match_id"]: row for row in history})
    refs = {}
    for entry in coverage.get("team_season_requests", []) + coverage.get("league_season_audit", []):
        ref = entry.get("evidence")
        if isinstance(ref, dict) and isinstance(ref.get("sha256"), str):
            refs.setdefault(ref["sha256"], []).append(ref)
    parents, seen = [], set()

    def original(ref, kind):
        path = _reference_path({"path": ref["path"], "runtime_relative_path": ref["path"]}, runtime)
        if not re.fullmatch(r"[0-9a-f]{64}", ref.get("sha256", "")) or not path.is_file() or _sha(path) != ref["sha256"]:
            raise SnapshotError("used_schedule_source_hash_mismatch")
        value = _read(path)
        _check_times(value, cutoff)
        if _utc(ref["fetched_at"]) != _utc(value["fetched_at"]):
            raise SnapshotError("used_schedule_ref_time_mismatch")
        if value.get("errors") or not isinstance(value.get("response"), list):
            raise SnapshotError("used_schedule_source_invalid")
        paging = value.get("paging", {})
        if any(paging.get(key, 1) != 1 for key in ("current", "total")):
            raise SnapshotError("used_schedule_source_partial")
        if (path, kind) not in seen:
            seen.add((path, kind))
            parents.append((path, kind))
        return value

    for mid in sorted(ids):
        row = rows.get(mid)
        if not row or row.get("status") not in ("FT", "AET", "PEN"):
            raise SnapshotError("used_schedule_match_not_preserved")
        source_hash = row.get("source_response_sha256")
        candidates = refs.get(source_hash, [])
        if not candidates:
            raise SnapshotError("used_schedule_response_ref_missing")
        # Every referenced alternative must resolve and match its hash. Do not
        # silently rescue a broken reference by picking an easier sibling.
        verified = [original(ref, "all_club_used_fixture_response") for ref in candidates]
        for envelope in verified:
            if envelope.get("endpoint") != "/fixtures":
                raise SnapshotError("used_schedule_endpoint_mismatch")
            choices = [raw for raw in envelope["response"] if isinstance(raw, dict)
                       and isinstance(raw.get("fixture"), dict) and raw["fixture"].get("id") == mid]
            if len(choices) != 1 or not _identity_equal(choices[0], row) or _fixture(choices[0])["status"] != row["status"]:
                raise SnapshotError("used_schedule_response_identity_mismatch")
            raw = choices[0]
            league = raw.get("league", {})
            if league.get("id") != row.get("league_id") or league.get("season") != row.get("season"):
                raise SnapshotError("used_schedule_competition_mismatch")
            params = envelope.get("params", {})
            if (not isinstance(params, dict) or params.get("season") != row["season"]
                    or ("team" in params and params["team"] not in (row["home_id"], row["away_id"]))
                    or ("league" in params and params["league"] != row["league_id"])
                    or set(params) not in ({"team", "season"}, {"league", "season"})):
                raise SnapshotError("used_schedule_request_identity_mismatch")
            if _utc(row["observed_at"]) != _utc(envelope["fetched_at"]) or _utc(row["received_at"]) > cutoff:
                raise SnapshotError("used_schedule_row_time_mismatch")
        metadata = [entry for entry in coverage.get("competition_metadata", [])
                    if entry.get("league_id") == row["league_id"] and entry.get("season") == row["season"]]
        if len(metadata) != 1 or metadata[0].get("friendly") is not False:
            raise SnapshotError("used_schedule_competition_metadata_missing")
        entry = metadata[0]
        envelope = original(entry["evidence"], "all_club_used_competition_metadata")
        if envelope.get("endpoint") != "/leagues" or envelope.get("params") != {"id": row["league_id"], "season": row["season"]}:
            raise SnapshotError("used_schedule_metadata_request_mismatch")
        records = [item for item in envelope["response"] if isinstance(item, dict)
                   and isinstance(item.get("league"), dict) and item["league"].get("id") == row["league_id"]]
        if (len(records) != 1 or records[0]["league"].get("type") != row.get("competition_type")
                or records[0]["league"].get("name") != entry.get("name")
                or sum(s.get("year") == row["season"] for s in records[0].get("seasons", []) if isinstance(s, dict)) != 1):
            raise SnapshotError("used_schedule_metadata_identity_mismatch")
    return parents


def main(argv=None):
    parser = argparse.ArgumentParser(description="保存真实双时点影子快照；不采集API、不倒填历史、不改变基线")
    sub = parser.add_subparsers(dest="operation", required=True)
    command = sub.add_parser("capture")
    command.add_argument("--runtime", default="runtime")
    command.add_argument("--config", default="config.json")
    command.add_argument("--league", default="serie_a")
    command.add_argument("--source-commit", required=True)
    command.add_argument("--state-commit")
    command.add_argument("--protocol", required=True)
    args = parser.parse_args(argv)
    try:
        code, report = capture_run(args.runtime, args.league, source_commit=args.source_commit,
            state_commit=args.state_commit, protocol=_read(args.protocol), config=_read(args.config))
    except (ValueError, KeyError, TypeError, OSError):
        print("双时点快照未完成：配置、协议、索引或原件不合格。")
        return 2
    print(f"双时点快照：{report['status']}；本轮 {report['captured_versions_this_run']} 个版本；"
          f"累计 {report['captured_independent_matches']} 场；确认样本 0；API 请求 0。")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
