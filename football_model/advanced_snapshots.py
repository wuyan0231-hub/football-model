"""Immutable real-clock captures for independent advanced research branches.

This intentionally does not mutate the older feature-snapshot namespaces.  A
capture contains a separately verifiable production M0 plus frozen, rebuildable
numeric inputs and model cards. No historical --as-of capture API is exposed.
"""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import fcntl
import hashlib
import json
from pathlib import Path
import re
import tempfile

from .data import _iso, _utc
from .advanced_capacity import CapacityGuard, CapacityDeferred
from .fundamentals import _fixture, build_features
from .integration import _entry_for_match, match_packet
from .feature_snapshots import (HORIZONS, SnapshotError, _bytes, _check_times,
    _digest, _evidence_inputs, _fixture_context, _horizon_state, _pairs,
    _path_ref, _prediction_same, _read, _reference_path, _save, _sha, _tick,
    _write_bytes, snapshot_from_evidence, verify_baseline_capture)
from .feature_scoring import _path, _read_ref, _pointer, _reference, _raw as _pointer_bytes
from .history_archive import read_bytes as history_read_bytes, exists as history_exists

VERSION = "advanced-snapshots-v1"


def candidate_ids(protocol):
    values = protocol.get("candidates", {})
    result = list(values) if isinstance(values, dict) else [v if isinstance(v, str) else v["candidate_id"] for v in values]
    for value in protocol.get("goalmodel_candidates", ["G0_full", "G0", "G1"]):
        key = value if isinstance(value, str) else value["candidate_id"]
        if key not in result:
            result.append(key)
    if protocol.get("cold_start_candidate"):
        result.append(protocol["cold_start_candidate"])
    if not result or any(not re.fullmatch(r"[A-Za-z0-9_-]+", key) for key in result):
        raise SnapshotError("invalid_advanced_candidate_ids")
    return result


def baseline_protocol(protocol):
    """Strict v2 verification contract embedded solely for the M0 subreceipt."""
    value = {"schema_version": "feature-model-protocol-v2", "research_design_version": "feature-research-v2",
        "snapshot_source_validation": "rebuild_from_frozen_sources_v2", "future_scoring_denominator": "all_queue",
        "research_artifact_namespace": "protocol_hash_v2", "confirmation_active": False,
        "advanced_parent_protocol_sha256": _digest(protocol)}
    for key in ("early_min_hours", "early_max_hours", "lineup_max_minutes", "capture_windows"):
        if key in protocol:
            value[key] = deepcopy(protocol[key])
    return value


def _commit(value, optional=False):
    if optional and value is None:
        return
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-fA-F]{40,64}", value):
        raise SnapshotError("full_source_or_state_commit_required")


def _reason(exc):
    return str(exc) if isinstance(exc, (SnapshotError, ValueError)) else type(exc).__name__


def _root(runtime, protocol):
    return Path(runtime).resolve() / "advanced_snapshots" / _digest(protocol)


def _guarded_save(root, category, value, guard, *, final=False):
    raw = _bytes(value)
    path = root / category / (hashlib.sha256(raw).hexdigest() + ".json")
    ticket = guard.check_write(path, raw, final=final)
    result = _save(root, category, value)
    guard.commit_write(ticket)
    return result


def _guarded_pointer(path, value, guard, *, first=False, final=False):
    ticket = guard.check_write(path, _pointer_bytes(value), final=final)
    _pointer(path, value, first=first)
    guard.commit_write(ticket)


def _guarded_freeze(path, runtime, root, cutoff, kind, guard):
    path = Path(path).resolve()
    if not path.is_relative_to(runtime):
        raise SnapshotError("source_path_escapes_runtime")
    raw = history_read_bytes(path, runtime=runtime)
    suffix = path.suffix if path.suffix in (".json", ".py") else ".bin"
    sha = hashlib.sha256(raw).hexdigest()
    destination = root / "sources" / (sha + suffix)
    value = json.loads(raw, object_pairs_hook=_pairs,
        parse_constant=lambda _: (_ for _ in ()).throw(SnapshotError("nonfinite_json"))) if suffix == ".json" else None
    latest = _check_times(value, cutoff) if value is not None else None
    ticket = guard.check_write(destination, raw)
    # Same bytes and reference contract as the original feature snapshot
    # freezer. Validate times before writing so rejected input cannot leave
    # unaccounted bytes behind after an exception.
    _write_bytes(destination, raw)
    guard.commit_write(ticket)
    reference = {"path": str(destination), "runtime_relative_path": str(destination.relative_to(runtime)),
        "origin_runtime_path": str(path.relative_to(runtime)), "sha256": sha,
        "kind": kind, "available_at": _iso(latest or cutoff)}
    if isinstance(value, dict) and value.get("schema_version") == "fundamentals-evidence-v1":
        reference["snapshot_id"] = value["snapshot_id"]
    return reference


@contextmanager
def _capture_storage(runtime, protocol, capacity_guard=None):
    runtime = Path(runtime).resolve()
    lockpath = runtime.parent / ".advanced-capture-locks" / (_digest([str(runtime), _digest(protocol)]) + ".lock")
    lockpath.parent.mkdir(parents=True, exist_ok=True)
    with lockpath.open("a") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        guard = capacity_guard if capacity_guard is not None else CapacityGuard(runtime)
        if guard.runtime != runtime:
            raise SnapshotError("advanced_capacity_runtime_mismatch")
        yield guard


def _refs_in(value):
    if isinstance(value, dict):
        if isinstance(value.get("path"), str) and re.fullmatch(r"[0-9a-f]{64}", str(value.get("sha256", ""))):
            yield value
        for item in value.values():
            if isinstance(item, (dict, list)):
                yield from _refs_in(item)
    elif isinstance(value, list):
        for item in value:
            yield from _refs_in(item)


def freeze_inputs(runtime, root, refs, cutoff, *, capacity_guard=None):
    """Recursively preserve each runtime input, including card training parents."""
    runtime = Path(runtime).resolve()
    guard = capacity_guard if capacity_guard is not None else CapacityGuard(runtime)
    pending, saved = list(refs), {}
    while pending:
        ref = pending.pop()
        rel = ref.get("runtime_relative_path", ref["path"])
        p = Path(rel)
        if p.is_absolute():
            if not p.resolve().is_relative_to(runtime):
                raise SnapshotError("advanced_input_escapes_runtime")
            p = p.resolve().relative_to(runtime)
        path = _path(runtime, p.as_posix())
        if not history_exists(path) or _sha(path) != ref["sha256"]:
            raise SnapshotError("advanced_input_hash_mismatch")
        key = p.as_posix()
        if key in saved:
            if saved[key]["sha256"] != ref["sha256"]:
                raise SnapshotError("advanced_input_version_conflict")
            continue
        frozen = _guarded_freeze(path, runtime, root, cutoff, "advanced_input", guard)
        saved[key] = frozen
        if path.suffix == ".json":
            # References are assertions about exact bytes, not a license to
            # ignore missing training evidence or silently read another latest.
            pending.extend(_refs_in(_read(path)))
    return list(saved.values())



def _rebind_capture_refs(value, refs):
    lookup = {(r.get("origin_runtime_path"),r["sha256"]):r for r in refs if r.get("kind")=="advanced_input"}
    def walk(item):
        if isinstance(item, dict):
            path=item.get("runtime_relative_path",item.get("path"))
            frozen=lookup.get((path,item.get("sha256")))
            if frozen is not None:
                return {"path":frozen["runtime_relative_path"],"sha256":frozen["sha256"],"original_path":path}
            return {key:walk(v) for key,v in item.items()}
        if isinstance(item,list): return [walk(v) for v in item]
        return item
    return walk(value)


@contextmanager
def frozen_runtime(runtime, refs, cutoff):
    runtime = Path(runtime).resolve()
    with tempfile.TemporaryDirectory(prefix="advanced-verify-") as directory:
        output = Path(directory)
        seen = {}
        for ref in refs:
            if ref.get("kind") != "advanced_input":
                continue
            origin = ref.get("origin_runtime_path")
            dest = _path(output, origin)
            source = _reference_path(ref, runtime)
            if not history_exists(source) or _sha(source) != ref["sha256"]:
                raise SnapshotError("advanced_frozen_input_hash_mismatch")
            if origin in seen and seen[origin] != ref["sha256"]:
                raise SnapshotError("advanced_frozen_input_version_conflict")
            seen[origin] = ref["sha256"]
            if source.suffix == ".json":
                _check_times(_read(source), cutoff)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(history_read_bytes(source, runtime=runtime))
        yield output



def _confirmation_tag(runtime, league, protocol, candidate, horizon, fixture, cutoff, branch=None):
    base = Path(runtime) / "advanced_models" / league / _digest(protocol) / "confirmation"
    matches = []
    for path in base.glob("*/" + candidate + "/" + horizon + "/activation.json"):
        reference = _read(path)
        activation = _read_ref(Path(runtime), reference)
        if (activation.get("advanced_protocol_sha256") != _digest(protocol)
                or activation.get("candidate_id") != candidate or activation.get("horizon") != horizon
                or activation.get("league") != league):
            raise SnapshotError("confirmation_activation_identity_mismatch")
        if (_utc(activation["created_at"]) <= cutoff and _utc(activation["future_start"]) <= cutoff
                and _utc(fixture["kickoff"]) <= _utc(activation["last_kickoff"])):
            matches.append((reference, activation))
    if len(matches) > 1:
        raise SnapshotError("ambiguous_confirmation_activation")
    if not matches:
        return {"confirmation_eligible": False}
    reference, activation = matches[0]
    from .advanced_research import implementation_fingerprint
    current = implementation_fingerprint()
    if activation.get("implementation_fingerprint") != current:
        return {"confirmation_eligible": False, "confirmation_ineligible_reason": "confirmation_implementation_changed"}
    if (branch or {}).get("status") == "predicted" and not branch.get("model_ref"):
        return {"confirmation_eligible": False, "confirmation_ineligible_reason": "confirmation_candidate_model_missing"}
    if (branch or {}).get("model_ref"):
        card = _read_ref(Path(runtime), branch["model_ref"])
        if card.get("implementation_fingerprint") != current:
            return {"confirmation_eligible": False, "confirmation_ineligible_reason": "confirmation_candidate_implementation_changed"}
    return {"confirmation_eligible": True, "confirmation_activation_ref": reference,
        "confirmation_activation_sha256": _digest(activation), "confirmation_implementation_fingerprint": current}


def _verify_confirmation_tag(runtime, payload, candidate, item):
    if item.get("confirmation_eligible") is not True:
        return
    activation = _read_ref(Path(runtime), item["confirmation_activation_ref"])
    cutoff = _utc(payload["actual_cutoff_at"])
    from .advanced_research import implementation_fingerprint
    current = implementation_fingerprint()
    if item.get("status") in {"predicted", "verified"} and not item.get("model_ref"):
        # Verified views retain the immutable model ref from the payload.
        original = payload.get("candidates", {}).get(candidate, {})
        if not original.get("model_ref"):
            raise SnapshotError("confirmation_candidate_model_missing")
    if (item.get("confirmation_activation_sha256") != _digest(activation)
            or activation.get("implementation_fingerprint") != current
            or item.get("confirmation_implementation_fingerprint") != current
            or activation.get("advanced_protocol_sha256") != payload["protocol_sha256"]
            or activation.get("candidate_id") != candidate or activation.get("horizon") != payload["horizon"]
            or activation.get("league") != payload["league"]
            or _utc(activation["created_at"]) > cutoff or _utc(activation["future_start"]) > cutoff
            or _utc(payload["fixture"]["kickoff"]) > _utc(activation["last_kickoff"])):
        raise SnapshotError("confirmation_capture_tag_invalid")
    model_ref = item.get("model_ref") or payload.get("candidates", {}).get(candidate, {}).get("model_ref")
    if model_ref and _read_ref(Path(runtime), model_ref).get("implementation_fingerprint") != current:
        raise SnapshotError("confirmation_candidate_implementation_changed")


def _feature_numeric(value):
    """All model inputs and completeness, without path-dependent diagnostics."""
    return {key: value.get(key) for key in ("home", "away", "groups", "missing", "availability_basis",
        "horizon", "forecast_cutoff", "cutoff_at", "prospective_qualified") if key in value}


def _live(runtime, league, protocol, baseline, values, cutoff, fixture):
    from .advanced_research import build_live_predictions
    return build_live_predictions(runtime, league, protocol, baseline, values, _iso(cutoff), fixture={**fixture, "neutral": (baseline or {}).get("neutral", fixture.get("neutral", False))})


def _features(runtime, league, fixture, cutoff, horizon, protocol):
    from .advanced_features import feature_values
    value = feature_values(runtime, league, fixture, _iso(cutoff), horizon=horizon, mode="prospective",
        recent_matches=protocol.get("advanced_feature_recent_matches", 5), min_history=protocol.get("advanced_feature_min_history", 3),
        national_load_lookback_days=protocol.get("national_load_lookback_days", 14))
    from .feature_dataset import feature_values as schedule_values
    runtime = Path(runtime)
    history = _read(runtime / "data" / (league + ".json"))["matches"]
    events = None
    schedule_path = runtime / "feature_models" / "schedules" / league / "latest.json"
    coverage = {}
    if schedule_path.exists():
        schedule = _read(schedule_path)
        _check_times(schedule, cutoff)
        coverage = schedule.get("coverage", {})
        if coverage.get("provider_queries_complete"):
            events = coverage["workload_events"]
            value["source_refs"].append(_reference(runtime, schedule_path))
    extra = schedule_values(fixture, history, _iso(cutoff), schedule_events=events)
    if events is not None:
        from .feature_schedule import load_schedule
        from .feature_snapshots import _used_schedule_sources
        schedule_history, checked_coverage = load_schedule(runtime, league)
        for source, _ in _used_schedule_sources(extra, schedule_history, events, checked_coverage, runtime, cutoff):
            value["source_refs"].append(_reference(runtime, source))
    for side in ("home", "away"):
        value[side].update(extra[side])
    value["schedule_evidence"] = extra["evidence"]
    value["schedule_scope"] = "all_club_competitions_provider_scope" if events is not None else "same_competition"
    return value


def verify_capture(runtime, receipt, *, candidates=True, now=None):
    """Return qualified M0 and independently verified/rejected branch states."""
    runtime = Path(runtime).resolve()
    payload = _read_ref(runtime, receipt["payload_ref"])
    if payload.get("schema_version") != VERSION or receipt.get("schema_version") != VERSION:
        raise SnapshotError("advanced_capture_schema_invalid")
    if payload.get("snapshot_id") != _digest({k: v for k, v in payload.items() if k != "snapshot_id"}):
        raise SnapshotError("advanced_payload_digest_mismatch")
    protocol, fixture = payload["protocol"], payload["fixture"]
    for key in ("league", "horizon", "protocol_sha256"):
        if receipt.get(key) != payload.get(key):
            raise SnapshotError("advanced_receipt_identity_mismatch")
    if (receipt.get("match_id") != fixture["match_id"] or payload["protocol_sha256"] != _digest(protocol)
            or payload.get("research_only") is not True or payload.get("confirmation_eligible") is not False):
        raise SnapshotError("advanced_capture_identity_or_eligibility_invalid")
    cutoff, completed = _utc(payload["actual_cutoff_at"]), _utc(receipt["storage_completed_at"])
    if not _utc(receipt["capture_started_at"]) <= cutoff <= _utc(payload["created_at"]) <= completed:
        raise SnapshotError("advanced_capture_clock_order_invalid")
    if now is not None and completed > _utc(now):
        raise SnapshotError("advanced_capture_saved_after_requested_time")
    live = _horizon_state(fixture, payload["horizon"], completed, baseline_protocol(protocol))["status"] == "due"
    if not live or receipt.get("live_capture_verified") is not True or receipt.get("status") != "captured":
        raise SnapshotError("advanced_capture_not_within_live_horizon")
    if payload.get("candidate_only"):
        return _verify_candidate_only(runtime, payload, receipt, candidates=candidates)
    base_receipt = _read_ref(runtime, payload["baseline_receipt_ref"])
    base = verify_baseline_capture(runtime, base_receipt)
    if (_fixture(base["fixture"]) != _fixture(fixture) or base["horizon"] != payload["horizon"]
            or base["actual_cutoff_at"] != payload["actual_cutoff_at"]
            or base["protocol"] != baseline_protocol(protocol)):
        raise SnapshotError("advanced_baseline_subreceipt_mismatch")
    result = {**payload, "baseline": base["baseline"], "baseline_parent_row": base["baseline_parent_row"],
        "verified_candidates": {}, "baseline_payload": base}
    if not candidates:
        return result
    ids = candidate_ids(protocol)
    captured = payload.get("candidates", {})
    if set(captured) != set(ids):
        raise SnapshotError("advanced_candidate_registry_mismatch")
    verified_tags = {}
    for cid, item in captured.items():
        try:
            _verify_confirmation_tag(runtime, payload, cid, item)
            verified_tags[cid] = {k: v for k, v in item.items() if k.startswith("confirmation_")}
        except (ValueError, KeyError, TypeError, OSError) as exc:
            verified_tags[cid] = {"confirmation_eligible":False, "confirmation_rejection_reason":_reason(exc)}
    if not any(item.get("status") == "predicted" for item in captured.values()):
        # Explicit unavailability has no numeric candidate claim to replay.
        # In particular a capacity rejection must retain its original reason
        # and the separately verified M0 instead of inventing missing inputs.
        result["verified_candidates"] = {
            cid: {**{k: v for k, v in deepcopy(item).items() if not k.startswith("confirmation_")},
                  **verified_tags[cid]} for cid, item in captured.items()}
        return result
    try:
        with frozen_runtime(runtime, payload["source_refs"], cutoff) as frozen:
            rebuilt = _features(frozen, payload["league"], fixture, cutoff, payload["horizon"], protocol)
            if _feature_numeric(rebuilt) != _feature_numeric(payload["feature_values"]):
                raise SnapshotError("advanced_features_do_not_rebuild")
            predictions = _live(frozen, payload["league"], protocol, base["baseline"]["prediction"], rebuilt, cutoff, fixture)
        for cid in ids:
            item = {k:v for k,v in deepcopy(captured[cid]).items() if not k.startswith("confirmation_")}
            item.update(verified_tags[cid])
            if item.get("status") == "predicted":
                expected = predictions.get("predictions", {}).get(cid)
                try:
                    if expected is None:
                        raise SnapshotError("advanced_candidate_cannot_rebuild")
                    _prediction_same(item["prediction"], expected, "advanced_candidate_numeric_replay_mismatch")
                    item["verification"] = "recomputed_from_frozen_sources"
                    item["status"] = "verified"
                except (ValueError, KeyError, TypeError) as exc:
                    item.update(status="rejected", reason=_reason(exc))
                    item.pop("prediction", None)
            result["verified_candidates"][cid] = item
    except (ImportError, ValueError, KeyError, TypeError, OSError) as exc:
        # Corrupt/missing advanced parents never invalidate independent M0.
        result["verified_candidates"] = {cid: {**verified_tags[cid], "status": "rejected" if captured[cid].get("status") == "predicted" else captured[cid].get("status", "unavailable"),
            "reason": _reason(exc), "original_status": captured[cid].get("status")} for cid in ids}
    return result


def _baseline(runtime, root, league, row, protocol, state_commit, started, clock, config, guard):
    context, context_path = _fixture_context(runtime, _fixture(row), started)
    snapshots, history, paths, excluded = _evidence_inputs(runtime, context, started)
    packet = match_packet(runtime, config, league, context["match_id"], as_of=_iso(started), state_commit=state_commit)
    cutoff = _tick(clock, started)
    features = build_features(context, cutoff=cutoff, snapshots=snapshots, fixture_history=history)
    parents = [(runtime / "integration/index.json", "integration_index"), (context_path, "current_fixture_context")]
    for field, kind in (("model_file", "baseline_model"), ("prediction_file", "baseline_forecast"), ("report_file", "baseline_report")):
        rel = packet["provenance"][field].removeprefix("runtime/")
        path = _path(runtime, rel)
        expected = packet["provenance"].get(field + "_sha256")
        if expected is not None and _sha(path) != expected:
            raise SnapshotError("advanced_baseline_changed_during_capture")
        parents.append((path, kind))
    refs = [_guarded_freeze(p, runtime, root, cutoff, k, guard) for p, k in parents]
    refs.extend(_guarded_freeze(p, runtime, root, cutoff, "fundamentals", guard) for p in paths)
    return context, cutoff, packet, features, refs, excluded


def capture_run(runtime, league="serie_a", *, source_commit, protocol, state_commit=None, config=None, clock=None, capacity_guard=None):
    runtime = Path(runtime).resolve()
    with _capture_storage(runtime, protocol, capacity_guard) as guard:
        return _capture_run_locked(runtime, league, source_commit=source_commit, protocol=protocol,
            state_commit=state_commit, config=config, clock=clock, guard=guard)


def _capture_run_locked(runtime, league="serie_a", *, source_commit, protocol, state_commit=None, config=None, clock=None, guard):
    runtime = Path(runtime).resolve()
    _commit(source_commit); _commit(state_commit, optional=True)
    candidate_ids(protocol)
    if any((p / "RESEARCH_REPLAY.json").exists() for p in (runtime, runtime.parent)):
        raise SnapshotError("research_replay_cannot_capture_live")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", league):
        raise SnapshotError("unsafe_league")
    clock = clock or (lambda: datetime.now(timezone.utc))
    config = config or _read("config.json")
    started = _utc(clock()); root = _root(runtime, protocol); root.mkdir(parents=True, exist_ok=True)
    report = {"schema_version": VERSION, "league": league, "source_commit": source_commit, "state_commit": state_commit,
        "protocol_sha256": _digest(protocol), "run_started_at": _iso(started), "queue": [], "issues": [],
        "research_only": True, "confirmation_eligible": False, "api_requests": 0}
    capacity_halted = None
    try:
        entry = _entry_for_match(runtime, config, league)
        attempt = entry.get("latest_successful_forecast")
        rows = _read(runtime / "predictions" / attempt["run_id"] / (league + ".json")) if attempt else []
        if not isinstance(rows, list) or len({r["match_id"] for r in rows}) != len(rows):
            raise SnapshotError("advanced_forecast_rows_invalid")
        for row in rows:
            fixture = _fixture(row)
            for horizon in HORIZONS:
                actual = _tick(clock, started)
                state = _horizon_state(fixture, horizon, actual, baseline_protocol(protocol))
                item = {"league": league, "fixture": fixture, "match_id": fixture["match_id"], "kickoff": fixture["kickoff"],
                    "horizon": horizon, "protocol_sha256": _digest(protocol), "queue_observed_at": _iso(actual),
                    "planned_state_at_observation": state["status"], "status": state["status"]}
                folder = root / "captures" / league / str(fixture["match_id"]) / horizon
                try:
                    if capacity_halted is not None and state["status"] == "due":
                        raise CapacityDeferred(capacity_halted)
                    if row.get("status") != "exploratory_baseline" or not isinstance(row.get("prediction"), dict):
                        item.update(status="unsupported", reason=row.get("reason", "baseline_unsupported"))
                        if state["status"] == "due" and row.get("status") == "unsupported":
                            receipt_ref = _capture_candidate_only(runtime, root, league, row, entry, protocol,
                                source_commit, state_commit, horizon, actual, clock, guard)
                            item.update(status="candidate_only", receipt_ref=receipt_ref)
                            if not (folder / "first.json").exists():
                                _guarded_pointer(folder / "first.json", receipt_ref, guard, first=True, final=True)
                    elif entry.get("status") != "available":
                        item.update(status="ineligible", reason="latest_league_forecast_not_available")
                    elif state["status"] == "due":
                        fixture, cutoff, packet, fundamentals, refs, excluded = _baseline(runtime, root, league, row,
                            protocol, state_commit, actual, clock, config, guard)
                        base = snapshot_from_evidence(fixture, packet, fundamentals, horizon=horizon, cutoff_at=cutoff,
                            source_refs=refs, protocol=baseline_protocol(protocol), clock=clock)
                        base.update(league=league, source_commit=source_commit, state_commit=state_commit,
                            baseline_parent_row=deepcopy(row), excluded_read_evidence=excluded)
                        fixture = base["fixture"]
                        base.pop("snapshot_id", None); base["snapshot_id"] = _digest(base)
                        base_path = _guarded_save(root, "baseline_payloads", base, guard)
                        base_done = _tick(clock, _utc(base["created_at"]))
                        base_receipt = {"schema_version": "feature-snapshots-1.0", "league": league, "match_id": fixture["match_id"],
                            "horizon": horizon, "protocol_sha256": _digest(baseline_protocol(protocol)),
                            "capture_started_at": _iso(actual), "actual_cutoff_at": _iso(cutoff), "storage_completed_at": _iso(base_done),
                            "payload_ref": _path_ref(base_path, runtime), "live_capture_verified": True, "status": "captured", "confirmation_eligible": False}
                        verify_baseline_capture(runtime, base_receipt)
                        base_receipt_ref = _path_ref(_guarded_save(root, "baseline_receipts", base_receipt, guard), runtime)
                        candidates = {}; advanced_refs = []; numeric = {}
                        try:
                            numeric = _features(runtime, league, fixture, cutoff, horizon, protocol)
                            result = _live(runtime, league, protocol, packet["prediction"], numeric, cutoff, fixture)
                            origins = list(numeric.get("source_refs", [])) + list(result.get("source_refs", [])) + list(_refs_in(result.get("cards", {})))
                            advanced_refs = freeze_inputs(runtime, root, origins, cutoff, capacity_guard=guard)
                            for cid in candidate_ids(protocol):
                                if (cid == protocol.get("cold_start_candidate") or (cid in protocol.get("candidates", {})
                                        and protocol["candidates"][cid].get("horizon") != horizon)):
                                    candidates[cid] = {"status":"not_applicable", "reason":"candidate_not_registered_for_this_horizon_or_supported_baseline"}
                                    continue
                                prediction = result.get("predictions", {}).get(cid)
                                candidates[cid] = ({"status": "predicted", "prediction": prediction, "forecast_id": _digest(prediction),
                                    "model_ref": result.get("cards", {}).get(cid)} if prediction is not None else
                                    {"status": "unavailable", "reason": result.get("failures", {}).get(cid, "candidate_not_returned")})
                        except CapacityDeferred as exc:
                            capacity_halted = exc.details
                            item.update(candidate_capacity_deferred=True, capacity=exc.details)
                            # Keep the independently frozen M0. Unfrozen numeric
                            # inputs and candidates cannot claim qualification.
                            numeric = {}; advanced_refs = []
                            candidates = {cid: {"status": "unavailable", "reason": exc.reason}
                                for cid in candidate_ids(protocol)}
                        except (ImportError, ValueError, KeyError, TypeError, OSError) as exc:
                            candidates = {cid: {"status": "unavailable", "reason": _reason(exc)} for cid in candidate_ids(protocol)}
                        for cid, branch in candidates.items():
                            branch.update(_confirmation_tag(runtime, league, protocol, cid, horizon, fixture, cutoff, branch))
                        created = _tick(clock, base_done)
                        payload = {"schema_version": VERSION, "league": league, "fixture": fixture, "horizon": horizon,
                            "protocol": deepcopy(protocol), "protocol_sha256": _digest(protocol), "source_commit": source_commit,
                            "state_commit": state_commit, "actual_cutoff_at": _iso(cutoff), "created_at": _iso(created),
                            "baseline_receipt_ref": base_receipt_ref, "source_refs": advanced_refs,
                            "feature_values": _rebind_capture_refs(numeric, advanced_refs),
                            "candidates": _rebind_capture_refs(candidates, advanced_refs), "research_only": True, "confirmation_eligible": False}
                        payload["snapshot_id"] = _digest(payload)
                        payload_ref = _path_ref(_guarded_save(root, "payloads", payload, guard, final=True), runtime)
                        completed = _tick(clock, created)
                        live = _horizon_state(fixture, horizon, completed, baseline_protocol(protocol))["status"] == "due"
                        receipt = {"schema_version": VERSION, "league": league, "match_id": fixture["match_id"], "horizon": horizon,
                            "protocol_sha256": _digest(protocol), "capture_started_at": _iso(actual), "actual_cutoff_at": _iso(cutoff),
                            "storage_completed_at": _iso(completed), "payload_ref": payload_ref,
                            "live_capture_verified": live, "status": "captured" if live else "late", "confirmation_eligible": False}
                        if live:
                            checked = verify_capture(runtime, receipt, now=completed)
                            # Never label a source-replay failure as a successful candidate.
                            item["candidate_statuses"] = {cid: v["status"] for cid, v in checked["verified_candidates"].items()}
                            receipt["original_candidate_statuses"] = item["candidate_statuses"]
                        receipt_ref = _path_ref(_guarded_save(root, "receipts", receipt, guard, final=True), runtime)
                        item.update(status=receipt["status"], receipt_ref=receipt_ref)
                        if live and not (folder / "first.json").exists():
                            _guarded_pointer(folder / "first.json", receipt_ref, guard, first=True, final=True)
                except CapacityDeferred as exc:
                    capacity_halted = exc.details
                    item.update(status="capacity_deferred", reason=exc.reason, capacity=exc.details)
                except (ValueError, KeyError, TypeError, OSError) as exc:
                    item.update(status="ineligible", reason=_reason(exc))
                record_ref = _path_ref(_guarded_save(root, "queue/history", item, guard, final=True), runtime)
                if not (folder / "first_observation.json").exists():
                    _guarded_pointer(folder / "first_observation.json", record_ref, guard, first=True, final=True)
                if state["status"] in ("due", "late") and not (folder / "first_attempt.json").exists():
                    _guarded_pointer(folder / "first_attempt.json", record_ref, guard, first=True, final=True)
                report["queue"].append({**item, "queue_record_ref": record_ref})
    except (ValueError, KeyError, TypeError, OSError) as exc:
        report["issues"].append({"reason": _reason(exc)})
    report.update(run_completed_at=_iso(_tick(clock, started)), queue_status_counts=dict(Counter(x["status"] for x in report["queue"])),
        planned_horizon_count=len(report["queue"]), planned_match_count=len({x["match_id"] for x in report["queue"]}))
    report["capacity_deferred_horizon_count"] = sum(x["status"] == "capacity_deferred" or
        bool(x.get("candidate_capacity_deferred")) for x in report["queue"])
    report["execution_status"] = "failed" if report["issues"] else "deferred" if report["capacity_deferred_horizon_count"] else "complete"
    report["status"] = "partial" if report["issues"] or report["capacity_deferred_horizon_count"] else "complete" if report["queue"] else "no_saved_forecasts"
    report["storage_capacity_before_report_writes"] = guard.summary()
    report["report_ref"] = _path_ref(_guarded_save(root, "reports/history", report, guard, final=True), runtime)
    _guarded_pointer(root / "reports" / league / "latest.json", report, guard, final=True)
    return int(bool(report["issues"])), report



def _only_parents(runtime, payload):
    refs = [r for r in payload["source_refs"] if r.get("kind") != "advanced_input"]
    from .feature_snapshots import _verify_refs, _one_source, _identity_equal
    cutoff = _utc(payload["actual_cutoff_at"])
    _verify_refs(refs, cutoff, runtime=runtime)
    def one(kind): return _one_source({"source_refs":refs}, kind, runtime)[1]
    bundle, rows, report, index, context = [one(k) for k in
        ("baseline_model", "baseline_forecast", "baseline_report", "integration_index", "current_fixture_context")]
    row = payload["baseline_parent_row"]
    selected = [r for r in rows if r.get("match_id") == payload["fixture"]["match_id"]]
    entries = [e for e in index.get("competitions",[]) if e.get("key")==payload["league"]]
    if (len(selected)!=1 or selected[0]!=row or row.get("status")!="unsupported"
            or row.get("model_id")!=bundle.get("model_id") or len(entries)!=1
            or entries[0].get("current_model_id")!=bundle.get("model_id")
            or entries[0].get("latest_successful_forecast",{}).get("run_id")!=report.get("run_id")
            or row.get("competition")!=payload["league"] or not _identity_equal(row,payload["fixture"])):
        raise SnapshotError("candidate_only_parent_identity_mismatch")
    envelope=context.get("fixture_response",{}); raw=envelope.get("response",[])
    if (envelope.get("endpoint")!="/fixtures" or envelope.get("params")!={"id":row["match_id"]}
            or envelope.get("errors") or len(raw)!=1 or not _identity_equal(raw[0],row)
            or _fixture(raw[0])["status"]!="NS"):
        raise SnapshotError("candidate_only_fixture_context_invalid")
    if (row.get("research_replay") or report.get("research_replay")
            or bundle.get("maturity")!="exploratory_baseline"
            or bundle.get("training_cutoff")!=row.get("training_cutoff")
            or not _utc(row["training_cutoff"])<=_utc(row["model_fitted_at"])<=_utc(row["generated_at"])<=cutoff):
        raise SnapshotError("candidate_only_source_time_invalid")
    model=bundle["model"]
    unknown=[t for t in (row["home_id"],row["away_id"]) if str(t) not in model.get("team_parameters",{})]
    if not unknown:
        raise SnapshotError("candidate_only_failure_not_unknown_team")
    if payload["horizon"]=="LINEUP":
        from .feature_snapshots import _lineups_known
        snapshots=_verify_refs(refs,cutoff,runtime=runtime)
        if not _lineups_known(payload["fixture"],payload["fundamentals"],snapshots,cutoff):
            raise SnapshotError("candidate_only_confirmed_lineups_missing")
    return model


def _capture_candidate_only(runtime,root,league,row,entry,protocol,source_commit,state_commit,horizon,started,clock,guard):
    context, context_path=_fixture_context(runtime,_fixture(row),started)
    if context["status"]!="NS": raise SnapshotError("candidate_only_fixture_not_scheduled")
    snapshots,history,paths,excluded=_evidence_inputs(runtime,context,started)
    cutoff=_tick(clock,started)
    from .cold_start import score_cold_start
    model_path=runtime/"models"/(league+".json")
    bundle=_read(model_path)
    run=entry["latest_successful_forecast"]["run_id"]
    parents=[(runtime/"integration/index.json","integration_index"),(context_path,"current_fixture_context"),
        (model_path,"baseline_model"),(runtime/"predictions"/run/(league+".json"),"baseline_forecast"),
        (runtime/"reports/history"/(run+".json"),"baseline_report")]
    refs=[_guarded_freeze(p,runtime,root,cutoff,k,guard) for p,k in parents]
    refs.extend(_guarded_freeze(p,runtime,root,cutoff,"fundamentals",guard) for p in paths)
    numeric=_features(runtime,league,context,cutoff,horizon,protocol)
    generated=_live(runtime,league,protocol,None,numeric,cutoff,context)
    refs.extend(freeze_inputs(runtime,root,list(numeric.get("source_refs",[]))+list(generated.get("source_refs",[]))+list(_refs_in(generated.get("cards",{}))),cutoff,capacity_guard=guard))
    candidates={}
    for cid in candidate_ids(protocol):
        prediction=generated.get("predictions",{}).get(cid)
        if cid==protocol.get("cold_start_candidate"):
            prediction=score_cold_start(bundle["model"],row)
        candidates[cid]=({"status":"predicted","prediction":prediction,"forecast_id":_digest(prediction),
            "model_ref":generated.get("cards",{}).get(cid),"confirmation_eligible":False} if prediction is not None else
            {"status":"unavailable","reason":generated.get("failures",{}).get(cid,"baseline_unavailable"),"confirmation_eligible":False})
    payload={"schema_version":VERSION,"league":league,"fixture":_fixture(context),"horizon":horizon,
        "protocol":deepcopy(protocol),"protocol_sha256":_digest(protocol),"source_commit":source_commit,"state_commit":state_commit,
        "actual_cutoff_at":_iso(cutoff),"created_at":_iso(_tick(clock,cutoff)),"candidate_only":True,
        "baseline_receipt_ref":None,"baseline_parent_row":deepcopy(row),"source_refs":refs,"feature_values":_rebind_capture_refs(numeric,refs),
        "fundamentals":build_features(context,cutoff=cutoff,snapshots=snapshots,fixture_history=history),
        "candidates":_rebind_capture_refs(candidates,refs),"research_only":True,"confirmation_eligible":False}
    payload["snapshot_id"]=_digest(payload)
    reference=_path_ref(_guarded_save(root,"payloads",payload,guard),runtime); completed=_tick(clock,_utc(payload["created_at"]))
    live=_horizon_state(context,horizon,completed,baseline_protocol(protocol))["status"]=="due"
    receipt={"schema_version":VERSION,"league":league,"match_id":row["match_id"],"horizon":horizon,
        "protocol_sha256":_digest(protocol),"capture_started_at":_iso(started),"actual_cutoff_at":_iso(cutoff),
        "storage_completed_at":_iso(completed),"payload_ref":reference,"live_capture_verified":live,
        "status":"captured" if live else "late","confirmation_eligible":False}
    checked=verify_capture(runtime,receipt,now=completed)
    receipt["original_candidate_statuses"]={cid:v["status"] for cid,v in checked["verified_candidates"].items()}
    return _path_ref(_guarded_save(root,"receipts",receipt,guard,final=True),runtime)


def _verify_candidate_only(runtime,payload,receipt,*,candidates=True):
    model=_only_parents(runtime,payload)
    result={**payload,"baseline":{"prediction":None},"baseline_payload":{"source_refs":payload["source_refs"]},"verified_candidates":{}}
    if not candidates:return result
    cutoff=_utc(payload["actual_cutoff_at"])
    with frozen_runtime(runtime,payload["source_refs"],cutoff) as frozen:
        values=_features(frozen,payload["league"],payload["fixture"],cutoff,payload["horizon"],payload["protocol"])
        if _feature_numeric(values)!=_feature_numeric(payload["feature_values"]):
            raise SnapshotError("candidate_only_features_do_not_rebuild")
        predictions=_live(frozen,payload["league"],payload["protocol"],None,values,cutoff,payload["fixture"])
    from .cold_start import score_cold_start
    for cid,item in payload["candidates"].items():
        verified=deepcopy(item)
        if item.get("status")=="predicted":
            try:
                prediction=(score_cold_start(model,payload["baseline_parent_row"]) if cid==payload["protocol"].get("cold_start_candidate") else predictions["predictions"][cid])
                _prediction_same(item["prediction"],prediction,"candidate_only_numeric_replay_mismatch")
                verified.update(status="verified",verification="recomputed_from_frozen_sources")
            except (ValueError,KeyError,TypeError) as exc:
                verified.update(status="rejected",reason=_reason(exc));verified.pop("prediction",None)
        result["verified_candidates"][cid]=verified
    return result


def queue_members(runtime, league, protocol, now=None):
    runtime = Path(runtime).resolve(); now = _utc(now or datetime.now(timezone.utc)); members = {}
    for p in sorted((_root(runtime, protocol) / "queue/history").glob("*.json")):
        value = _read(p)
        if value.get("league") != league:
            continue
        if value.get("protocol_sha256") != _digest(protocol) or value.get("horizon") not in HORIZONS:
            raise SnapshotError("advanced_queue_protocol_or_horizon_mismatch")
        observed = _utc(value["queue_observed_at"])
        if observed > now:
            continue
        key = (value["match_id"], value["horizon"])
        member = members.setdefault(key, {"match_id": key[0], "horizon": key[1], "kickoff": value["kickoff"], "observations": []})
        if value["kickoff"] != member["kickoff"]:
            member["identity_conflict"] = True
        member["observations"].append((observed, value, _reference(runtime, p)))
    for member in members.values():
        member["observations"].sort(key=lambda x: (x[0], x[2]["sha256"]))
        member["first_observed_at"] = _iso(member["observations"][0][0])
        member["latest_status"] = member["observations"][-1][1]["status"]
        member["first_failures"] = [ref for _, v, ref in member["observations"] if v["status"] in ("ineligible", "unsupported", "late", "failed", "capacity_deferred")]
    return list(members.values())


def load_verified_comparison(runtime, league, fixture_id, horizon, protocol=None, *, row=None, now=None):
    runtime = Path(runtime).resolve(); now = _utc(now or datetime.now(timezone.utc))
    protocol = protocol or _read(Path(__file__).resolve().parents[1] / "advanced-models-v1.json")
    anchor = _root(runtime, protocol) / "captures" / league / str(fixture_id) / horizon / "first.json"
    output = {"status": "unavailable", "match_id": fixture_id, "horizon": horizon, "research_only": True,
        "candidates": {}, "evidence": [], "protocol_sha256": _digest(protocol)}
    try:
        members = [v for v in queue_members(runtime, league, protocol, now) if v["match_id"] == fixture_id and v["horizon"] == horizon]
        if len(members) != 1 or members[0].get("identity_conflict"):
            raise SnapshotError("advanced_queue_missing_or_identity_conflict")
        if not anchor.exists():
            raise SnapshotError("advanced_no_legal_baseline_capture")
        receipt_ref = _read(anchor); receipt = _read_ref(runtime, receipt_ref)
        if receipt_ref not in [v.get("receipt_ref") for _, v, _ in members[0]["observations"]]:
            raise SnapshotError("advanced_capture_not_bound_to_queue")
        payload = verify_capture(runtime, receipt, now=now)
        if payload["league"] != league or payload["fixture"]["match_id"] != fixture_id or payload["horizon"] != horizon:
            raise SnapshotError("advanced_capture_anchor_identity_mismatch")
        if row is not None and payload["baseline_parent_row"] != row:
            raise SnapshotError("advanced_exact_production_parent_row_mismatch")
        evidence = [_reference(runtime, anchor), receipt_ref, receipt["payload_ref"]]
        if payload.get("baseline_receipt_ref"):
            evidence.append(payload["baseline_receipt_ref"])
            base_receipt = _read_ref(runtime, payload["baseline_receipt_ref"])
            evidence.append(base_receipt["payload_ref"])
        evidence.extend({"path": ref["runtime_relative_path"], "sha256": ref["sha256"]}
                        for ref in payload["source_refs"] + payload["baseline_payload"]["source_refs"])
        for cid, branch in payload["verified_candidates"].items():
            original_status = receipt.get("original_candidate_statuses", {}).get(cid)
            branch["original_capture_status"] = original_status
            if original_status != "verified":
                branch.pop("prediction", None)
                branch.update(status="not_applicable" if original_status=="not_applicable" else "unavailable", reason=branch.get("reason", "original_capture_candidate_not_verified"))
            try:
                _verify_confirmation_tag(runtime, payload, cid, branch)
            except (ValueError,KeyError,TypeError,OSError) as exc:
                for key in list(branch):
                    if key.startswith("confirmation_"): branch.pop(key)
                branch.update(confirmation_eligible=False,confirmation_rejection_reason=_reason(exc))
            branch.update(forecast_id=receipt["payload_ref"]["sha256"] + ":" + cid,
                forecast_id_semantics="capture_sha256_and_candidate_id", captured_at=receipt["storage_completed_at"],
                actual_cutoff_at=payload["actual_cutoff_at"])
            if branch.get("confirmation_eligible") is True and branch.get("confirmation_activation_ref"):
                evidence.append(branch["confirmation_activation_ref"])
        output.update(status="available", fixture=payload["fixture"], baseline=payload["baseline"],
            baseline_parent_row=payload["baseline_parent_row"], candidates=payload["verified_candidates"],
            actual_cutoff_at=payload["actual_cutoff_at"], captured_at=receipt["storage_completed_at"],
            forecast_sha256=receipt["payload_ref"]["sha256"], receipt_ref=receipt_ref, payload=payload,
            original_candidate_statuses=receipt.get("original_candidate_statuses", {}),
            first_capture_anchor_ref=_reference(runtime,anchor),
            durability_ref=(_reference(runtime,_root(runtime,protocol)/"durability"/(_sha(anchor)+".json"))
                if (_root(runtime,protocol)/"durability"/(_sha(anchor)+".json")).exists() else None),
            evidence=evidence, first_observed_at=members[0]["first_observed_at"])
    except (ValueError, KeyError, TypeError, OSError) as exc:
        output.update(status="unavailable", reason=_reason(exc), candidates={cid: {"status": "unavailable", "reason": _reason(exc)} for cid in candidate_ids(protocol)})
    return output


def comparison(runtime, league, match_id, *, row=None, now=None, horizon="EARLY", protocol=None):
    return load_verified_comparison(runtime, league, match_id, horizon, protocol, row=row, now=now)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__); sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("capture"); p.add_argument("--runtime", default="runtime"); p.add_argument("--config", default="config.json")
    p.add_argument("--league", default="all"); p.add_argument("--protocol", default="advanced-models-v1.json")
    p.add_argument("--source-commit", required=True); p.add_argument("--state-commit")
    args = parser.parse_args(argv); protocol = _read(args.protocol); config = _read(args.config)
    leagues = [x["key"] for x in config["competitions"] if x.get("enabled", True)] if args.league == "all" else [args.league]
    code = 0
    # One guarded sequential session measures the complete runtime only once,
    # with the same lock protecting both the estimate and all league writes.
    with _capture_storage(args.runtime, protocol) as guard:
        for league in leagues:
            status, report = _capture_run_locked(args.runtime, league, protocol=protocol, config=config,
                source_commit=args.source_commit, state_commit=args.state_commit, guard=guard)
            code = max(code, status); print(json.dumps({k: report[k] for k in ("league", "status", "execution_status",
                "capacity_deferred_horizon_count", "planned_horizon_count", "queue_status_counts", "report_ref")}, ensure_ascii=False))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
