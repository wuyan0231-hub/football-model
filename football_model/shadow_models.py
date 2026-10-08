"""Real-time, append-only research arms; never writes a production model/ledger.

The public command deliberately has no historical --as-of. Test clock injection
is internal. Historical experiments belong in a separate retrospective runner.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import json
from pathlib import Path
import platform
import re

import numpy as np
import scipy

from .data import normalize_fixtures
from .model import (RESULT_AVAILABILITY_DELAY, fit_model, score_distribution,
                    _iso, _timestamp, _validated_matches)
from .prospective import (_append as _ledger_append, _digest, _identity, _json_bytes, _name, _now,
                          _objects, _path, _read, _same_identity)
from .settlement import matrix_array
from .state_storage import MAX_FILE_BYTES, MAX_LOGICAL_BYTES, estimate_runtime, storage_cost

VERSION = "s2-shadow-models-1.0"
TIME = "observed_wall_clock_v1"
ARMS = (("baseline180", 180), ("candidate90", 90), ("candidate365", 365))
STORAGE_LIMIT_BYTES = 400 * 1024 * 1024
CAPACITY_REPORT_RESERVE_BYTES = 256 * 1024
NUMERIC_REPLAY_MATRIX_ABS_TOL = 1e-15
NUMERIC_REPLAY_PROBABILITY_ABS_TOL = 1e-15
_BUDGET = ContextVar("shadow_storage_budget", default=None)


def _numerical_environment():
    return {"python_version": platform.python_version(), "numpy_version": np.__version__,
            "scipy_version": scipy.__version__, "platform_machine": platform.machine()}


def _numeric_replay_equivalent(recomputed, saved):
    """Only computed matrix, 1X2 and tail probabilities allow bounded roundoff."""
    probability_fields = {"home_win", "draw", "away_win"}
    rounded_fields = {"score_matrix", "probabilities", "tail_mass_before_normalization"}
    if (not isinstance(recomputed, dict) or not isinstance(saved, dict)
            or not rounded_fields <= recomputed.keys() or not rounded_fields <= saved.keys()
            or {k: v for k, v in recomputed.items() if k not in rounded_fields}
               != {k: v for k, v in saved.items() if k not in rounded_fields}):
        return False
    scalar_values = []
    for value in (recomputed, saved):
        probabilities = value["probabilities"]
        if not isinstance(probabilities, dict) or probabilities.keys() != probability_fields:
            return False
        scalars = [probabilities[k] for k in ("home_win", "draw", "away_win")]
        scalars.append(value["tail_mass_before_normalization"])
        if any(type(v) not in (int, float) or not 0 <= v <= 1 or not np.isfinite(v) for v in scalars):
            return False
        scalar_values.append(scalars)
    if any(abs(a - b) > NUMERIC_REPLAY_PROBABILITY_ABS_TOL
           for a, b in zip(*scalar_values)):
        return False
    arrays = []
    for value in (recomputed["score_matrix"], saved["score_matrix"]):
        if (not isinstance(value, list) or not value
                or any(not isinstance(row, list) or not row
                       or any(type(cell) not in (int, float) for cell in row) for row in value)):
            return False
        try:
            array = matrix_array(value)
        except (ValueError, TypeError):
            return False
        if np.any(array > 1):
            return False
        arrays.append(array)
    return (arrays[0].shape == arrays[1].shape
            and bool(np.all(np.abs(arrays[0] - arrays[1]) <= NUMERIC_REPLAY_MATRIX_ABS_TOL)))


class ShadowCapacityError(RuntimeError):
    def __init__(self, budget):
        self.budget = _budget_public(budget)
        super().__init__("shadow_storage_capacity_deferred")


def _budget_public(budget):
    return {k: v for k, v in budget.items() if not k.startswith("_")}


def _write_cost(path, raw):
    """Check both dimensions before writing, update them only after success."""
    budget = _BUDGET.get()
    if budget is None:
        return None
    if len(raw) > MAX_FILE_BYTES:
        raise ShadowCapacityError({**budget, "capacity_constraint": "file_limit"})
    previous = path.read_bytes() if path.exists() else None
    relative = path.relative_to(budget["_runtime"]).as_posix()
    persisted = storage_cost(relative, raw, budget["_retained_inline"])
    old_cost = storage_cost(relative, previous, budget["_retained_inline"]) if previous is not None else 0
    raw_delta = len(raw) - (len(previous) if previous is not None else 0)
    # A smaller replacement never grants space for extra immutable captures in
    # this run; the estimate remains conservative until the next full scan.
    persisted_delta = max(0, persisted - old_cost)
    if budget["estimated_persisted_bytes"] + persisted_delta > budget["limit_bytes"]:
        raise ShadowCapacityError({**budget, "capacity_constraint": "estimated_persisted_limit"})
    if budget["logical_bytes"] + raw_delta > budget["logical_capture_limit_bytes"]:
        raise ShadowCapacityError({**budget, "capacity_constraint": "logical_limit"})
    return raw_delta, persisted_delta


def _record_write(cost):
    if cost is not None:
        budget = _BUDGET.get()
        budget["runtime_bytes"] += cost[0]
        budget["logical_bytes"] += cost[0]
        budget["estimated_persisted_bytes"] += cost[1]


def _append(root, collection, value):
    path = _path(root, collection, _digest(value) + ".json")
    cost = _write_cost(path, _json_bytes(value)) if not path.exists() else None
    object_id = _ledger_append(root, collection, value)
    _record_write(cost)
    return object_id


def _snapshot(root, kind, content, sha):
    return _append(root, "sources", {"kind": kind, "original_bytes_sha256": sha, "content": content})


@contextmanager
def _writer(runtime):
    runtime = Path(runtime)
    if any(p.exists() for p in (runtime / "RESEARCH_REPLAY.json", runtime.parent / "RESEARCH_REPLAY.json")):
        raise ValueError("historical_replay_cannot_be_live_shadow")
    root = _path(runtime, "shadow_models")
    root.mkdir(parents=True, exist_ok=True)
    with _path(root, "writer-lock.txt").open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("shadow_writer_already_active") from exc
        estimated = estimate_runtime(runtime)
        token = _BUDGET.set({"runtime_bytes": estimated["raw_bytes"],
            "logical_bytes": estimated["logical_bytes"],
            "estimated_persisted_bytes": estimated["persisted_bytes"],
            "limit_bytes": STORAGE_LIMIT_BYTES, "logical_limit_bytes": MAX_LOGICAL_BYTES,
            "file_limit_bytes": MAX_FILE_BYTES,
            "logical_capture_limit_bytes": MAX_LOGICAL_BYTES - CAPACITY_REPORT_RESERVE_BYTES,
            "capacity_measurement": "lossless_archive_conservative_persisted_and_original_logical_bytes",
            "_runtime": runtime, "_retained_inline": frozenset(estimated["retained_inline"])})
        try:
            yield root
        finally:
            _BUDGET.reset(token)


def _protocol(root, config, clock):
    if config.get("half_life_days") != 180:
        raise ValueError("registered_baseline_requires_config_half_life_180")
    spec = {"version": VERSION, "target": "regulation_time_90_plus_stoppage",
        "arms": [{"arm": arm, "half_life_days": half} for arm, half in ARMS],
        "ridge": config["ridge"], "min_train": config["min_train"],
        "forecast_days": config["forecast_days"], "max_model_age_days": config["max_model_age_days"],
        "fit": "same_frozen_data_same_current_cutoff_existing_fit_model",
        "selection": "first_pair_capture_matching_final_identity_no_extra_match",
        "primary_comparison": "candidate90_vs_baseline180",
        "secondary_comparison": "candidate365_vs_baseline180",
        "primary_metric": "paired_1x2_log_loss_difference_vs_baseline180",
        "minimum_meaningful_log_loss_improvement": 0.01,
        "secondary_metrics": ["1x2_brier", "over_2_5_brier"],
        "purpose": "preregistered_exploratory_not_production_promotion",
        "automatic_promotion": False, "profit_evaluation": False,
        "independent_unit": "competition_and_match_id_not_versions"}
    for old in _objects(root, "protocols"):
        if old.get("spec") == spec:
            if _timestamp(old["registered_at"], "registered_at") > _now(clock):
                raise ValueError("protocol_registered_in_future")
            return old["object_id"]
    return _append(root, "protocols", {"spec": spec, "registered_at": _iso(_now(clock))})


def _tick(clock, previous):
    now = _now(clock)
    if now < previous:
        raise ValueError("wall_clock_regressed")
    return now


def _source_files(runtime, root, config, key, index, cutoff):
    entries = [e for e in index["competitions"] if e.get("key") == key]
    if len(entries) != 1:
        raise ValueError("competition_index_not_unique")
    entry = entries[0]
    if entry.get("status") not in ("available", "unsupported_only", "no_upcoming"):
        raise ValueError("production_source_blocked:" + str(entry.get("status")))
    success = entry.get("latest_successful_forecast")
    if not success:
        if entry.get("status") == "no_upcoming":
            return [], {}, {"no_upcoming_without_current_forecast": True}, {}
        raise ValueError("no_saved_schedule_source")
    run_id = _name(success["run_id"])
    report, report_sha = _read(runtime, "reports", "history", run_id + ".json")
    rows, row_sha = _read(runtime, "predictions", run_id, key + ".json")
    data, data_sha = _read(runtime, "data", key + ".json")
    if report.get("research_replay") or data.get("research_replay"):
        raise ValueError("research_input_cannot_be_live_shadow")
    if report.get("run_id") != run_id or report.get("mode") != "train":
        raise ValueError("source_report_identity_mismatch")
    if report.get("time_semantics") != TIME or data.get("time_semantics") != TIME:
        raise ValueError("source_wall_clock_evidence_missing")
    if _timestamp(report["generated_at"], "report.generated_at") > cutoff:
        raise ValueError("source_report_in_future")
    items = [x for x in report["competitions"] if x.get("key") == key]
    if len(items) != 1 or items[0].get("status") != "探索性基线":
        raise ValueError("source_competition_report_invalid")
    item = items[0]
    if data.get("missing_seasons") or item.get("missing_seasons"):
        raise ValueError("incomplete_training_seasons")
    observed = _timestamp(data["observed_at"], "data.observed_at")
    from .integration import _source_times
    _source_times(item.get("seasons"), observed, require_received=True)
    if observed > cutoff or item.get("data_observed_at", item.get("observed_at")) != data["observed_at"]:
        raise ValueError("training_data_observation_mismatch")
    if not isinstance(rows, list) or len({r.get("match_id") for r in rows}) != len(rows):
        raise ValueError("duplicate_or_invalid_schedule")
    if len(rows) != success["forecast_count"] + success["unsupported_forecasts"]:
        raise ValueError("schedule_count_mismatch")
    for row in rows:
        _identity(row)
        if row.get("research_replay") or row.get("competition") != key:
            raise ValueError("schedule_identity_or_research_mismatch")
        if row.get("run_started_at") != report.get("run_started_at") or row.get("data_observed_at") != data["observed_at"]:
            raise ValueError("schedule_data_report_binding_mismatch")
        if _timestamp(row["forecast_generated_at"], "source.forecast_generated_at") > cutoff:
            raise ValueError("schedule_generated_in_future")
    parents = {"data_source": _snapshot(root, "normalized_regulation_data", data, data_sha),
        "schedule_source": _snapshot(root, "production_schedule", rows, row_sha),
        "report_source": _snapshot(root, "production_report", report, report_sha),
        "source_run_id": run_id, "original_data_sha256": data_sha,
        "data_observed_at": data["observed_at"], "source_production_commit": report.get("source_commit"),
        "provider_league_id": item.get("provider_league_id")}
    return rows, data, parents, item


def _neutral(runtime, root, competition, row, cutoff):
    if competition["mode"] != "national_baseline":
        return False, None
    if competition["key"] != "unl":
        raise ValueError("unsupported_national_competition")
    from .nations import validate_nations_forecast, venue_temporal_audit
    from .venue_registry import load_forecast_registry
    if not row.get("registry_snapshot"):
        raise ValueError("national_registry_snapshot_missing")
    registry = load_forecast_registry(runtime, row)
    neutral = validate_nations_forecast(row, registry)
    temporal = venue_temporal_audit(row, registry, cutoff=_iso(cutoff), strict=False)
    # Unknown historical validity is retained, not made a new blanket exclusion.
    for field in ("known_at", "evidence_available_at"):
        if temporal.get(field) and _timestamp(temporal[field], field) > cutoff:
            raise ValueError("venue_evidence_after_input_cutoff")
    registry_id = _snapshot(root, "national_registry", registry, hashlib.sha256(
        _path(runtime, row["registry_snapshot"]).read_bytes()).hexdigest())
    return neutral, {"registry_source": registry_id, "temporal_audit": temporal,
        "venue_evidence": row["venue_evidence"]}


def _check_schedule(runtime, root, competition, row, parents, cutoff):
    """A newer known reschedule/non-NS response fences the older schedule."""
    latest = []
    directory = _path(runtime, "quotes", "fixtures", str(row["match_id"]))
    for path in sorted(directory.glob("*.json")):
        value, digest = _read(runtime, *path.relative_to(runtime).parts)
        if path.stem != digest:
            raise ValueError("quote_fixture_response_hash_mismatch")
        observed = _timestamp(value["fetched_at"], "quote_fixture.fetched_at")
        if observed > cutoff:
            raise ValueError("quote_fixture_observed_in_future")
        latest.append((observed, digest, value))
    if not latest:
        return {"status": "daily_schedule_only", "current_official_schedule_verified": False}
    latest.sort(key=lambda x: (x[0], x[1]))
    observed, digest, value = latest[-1]
    if observed < _timestamp(parents["data_observed_at"], "data_observed_at"):
        return {"status": "quote_fixture_older_than_daily_schedule", "current_official_schedule_verified": False}
    if len({d for t, d, _ in latest if t == observed}) != 1:
        raise ValueError("conflicting_quote_fixture_same_observation")
    source_id = _snapshot(root, "quote_fixture_response", value, digest)
    if value.get("errors") or value.get("endpoint") != "/fixtures" or value.get("params") != {"id": row["match_id"]}:
        raise ValueError("known_quote_fixture_response_invalid:" + source_id)
    paging = value.get("paging", {})
    items = value.get("response")
    if (not isinstance(paging, dict) or any(type(paging.get(k, 1)) is not int or paging.get(k, 1) != 1 for k in ("current", "total"))
            or not isinstance(items, list) or len(items) != 1):
        raise ValueError("known_quote_fixture_response_partial:" + source_id)
    item = items[0]
    identity = {"match_id": item["fixture"]["id"], "kickoff": item["fixture"]["date"],
                "home_id": item["teams"]["home"]["id"], "away_id": item["teams"]["away"]["id"]}
    if not _same_identity(identity, row) or item["league"]["id"] != parents["provider_league_id"]:
        raise ValueError("known_newer_fixture_identity_conflict:" + source_id)
    if item["fixture"]["status"]["short"] != "NS":
        raise ValueError("known_newer_fixture_not_NS:" + source_id)
    return {"status": "matched_available_quote_fixture_response", "source_id": source_id,
            "observed_at": _iso(observed), "current_official_schedule_verified": False}


def _capture(runtime, config, *, source_commit, state_commit=None, league="all", clock=None):
    """Fit all declared arms now; freeze inputs once; no production mutations."""
    clock = clock or (lambda: datetime.now(timezone.utc))
    if not re.fullmatch(r"[0-9a-f]{40}", source_commit or ""):
        raise ValueError("full_source_commit_required")
    if state_commit is not None and not re.fullmatch(r"[0-9a-f]{40}", state_commit):
        raise ValueError("invalid_state_commit")
    runtime = Path(runtime)
    with _writer(runtime) as root:
        if _BUDGET.get()["estimated_persisted_bytes"] >= _BUDGET.get()["limit_bytes"]:
            raise ShadowCapacityError({**_BUDGET.get(), "capacity_constraint": "estimated_persisted_limit"})
        if _BUDGET.get()["logical_bytes"] >= _BUDGET.get()["logical_capture_limit_bytes"]:
            raise ShadowCapacityError({**_BUDGET.get(), "capacity_constraint": "logical_limit"})
        started = _now(clock)
        protocol_id = _protocol(root, config, clock)
        index, index_sha = _read(runtime, "integration", "index.json")
        if index.get("status") != "ready":
            raise ValueError("integration_index_not_ready")
        index_id = _snapshot(root, "integration_index", index, index_sha)
        config_id = _snapshot(root, "config", config, _digest(config))
        implementations = {name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
                           for name in ("shadow_models.py", "model.py", "nations.py", "venue_registry.py")}
        report = {"version": VERSION, "started_at": _iso(started), "protocol_id": protocol_id,
            "source_commit": source_commit, "state_commit": state_commit,
            "numerical_environment": _numerical_environment(),
            "implementation_sha256": implementations, "config_source": config_id,
            "index_source": index_id, "purpose": "prospective_model_experiment",
            "schedule_scope": "saved_daily_schedule_checked_against_available_quote_fixture_responses_not_live_official_confirmation",
            "production_models_changed": False, "accuracy_improvement_verified": False,
            "competitions": [], "errors": []}
        competitions = [c for c in config["competitions"] if c["mode"] != "audit_only" and
                        (league == "all" or c["key"] == league)]
        if not competitions:
            raise ValueError("unknown_or_audit_only_competition")
        for competition in competitions:
            key = _name(competition["key"])
            entry = {"competition": key, "arms": [], "coverage": []}
            report["competitions"].append(entry)
            try:
                input_read_started = _tick(clock, started)
                rows, data, parents, item = _source_files(runtime, root, config, key, index, input_read_started)
                # Freeze one common input and cutoff before fitting any arm.
                cutoff = _tick(clock, input_read_started)
                base = {"protocol_id": protocol_id, "competition": key,
                    "input_cutoff": _iso(cutoff), "source_commit": source_commit,
                    "state_commit": state_commit, "parents": parents,
                    "implementation_sha256": implementations, "config_source": config_id}
                candidates = []
                for row in rows:
                    reason = None
                    kickoff = _timestamp(row["kickoff"], "kickoff")
                    if kickoff <= cutoff:
                        reason = "not_future_at_real_capture"
                    elif kickoff > cutoff + timedelta(days=config["forecast_days"]):
                        reason = "outside_registered_forecast_window"
                    try:
                        if reason:
                            raise ValueError(reason)
                        schedule_evidence = _check_schedule(runtime, root, key, row, parents, cutoff)
                        neutral, venue = _neutral(runtime, root, competition, row, cutoff)
                        candidates.append((row, neutral, {"venue": venue, "schedule": schedule_evidence}))
                    except (ValueError, KeyError, TypeError, OSError) as exc:
                        entry["coverage"].append({**_identity(row), "status": "rejected", "reason": str(exc)})
                input_id = _append(root, "inputs", {**base, "candidate_identities": [_identity(r) for r, _, _ in candidates],
                    "rejected": entry["coverage"], "frozen_at": _iso(cutoff)})
                entry.update(input_id=input_id, source_rows=len(rows), eligible_candidates=len(candidates))
                if not candidates:
                    entry["status"] = "no_eligible_future_matches"
                    continue
                if competition["mode"] == "national_baseline":
                    from .nations import validate_nations_forecast
                    from .venue_registry import load_forecast_registry
                    registry = load_forecast_registry(runtime, candidates[0][0])
                    for training_row in data["matches"]:
                        validate_nations_forecast(training_row, registry)
                previous = cutoff
                for arm, half_life in ARMS:
                    arm_entry = {"arm": arm, "half_life_days": half_life, "forecasts": []}
                    entry["arms"].append(arm_entry)
                    try:
                        fitted = fit_model(data["matches"], _iso(cutoff), half_life_days=half_life,
                                           ridge=config["ridge"], min_train=config["min_train"])
                        completed = _tick(clock, previous)
                        previous = completed
                        if fitted["optimizer"]["parameter_at_bound"]:
                            raise ValueError("fitted_parameters_at_optimization_bound")
                        model_id = _append(root, "models", {**base, "input_id": input_id,
                            "arm": arm, "half_life_days": half_life, "model_fitted_at": _iso(completed),
                            "model": fitted, "research_only": True, "production_promotion": False})
                        arm_entry.update(status="fitted", model_id=model_id)
                        for row, neutral, venue in candidates:
                            coverage = {**_identity(row), "arm": arm, "input_id": input_id,
                                "protocol_id": protocol_id, "competition": key,
                                "source_status": row.get("status"), "source_reason": row.get("reason")}
                            try:
                                prediction = score_distribution(fitted, row["home_id"], row["away_id"], row["kickoff"], neutral)
                                if prediction["model_age_days"] > config["max_model_age_days"]:
                                    raise ValueError("model_expires_before_kickoff")
                                generated = _tick(clock, completed)
                                matrix_array(prediction["score_matrix"])
                                before_storage = _tick(clock, generated)
                                if before_storage >= _timestamp(row["kickoff"], "kickoff"):
                                    raise ValueError("completed_at_or_after_kickoff")
                                payload = {**base, **_identity(row),
                                    "input_id": input_id, "arm": arm, "model_id": model_id,
                                    "model_fitted_at": _iso(completed), "forecast_generated_at": _iso(generated),
                                    "storage_started_at": _iso(before_storage), "prediction": prediction,
                                    "venue": venue, "research_only": True,
                                    "production_ledger_eligible": False, "research_replay": False}
                                # Persist the complete prediction before checking admission.
                                # A slow fsync crossing kickoff leaves a research payload,
                                # not a supposedly live forecast with an earlier timestamp.
                                payload_id = _append(root, "forecast_payloads", payload)
                                captured = _tick(clock, before_storage)
                                coverage["payload_id"] = payload_id
                                if captured >= _timestamp(row["kickoff"], "kickoff"):
                                    raise ValueError("storage_completed_at_or_after_kickoff")
                                forecast_id = _append(root, "forecasts", {**payload,
                                    "payload_id": payload_id, "storage_completed_at": _iso(captured),
                                    "captured_at": _iso(captured), "live_before_kickoff": True})
                                coverage.update(status="captured", forecast_id=forecast_id)
                            except (ValueError, KeyError, TypeError) as exc:
                                coverage.update(status="rejected", reason=str(exc))
                            arm_entry["forecasts"].append(coverage)
                            _append(root, "coverage", coverage)
                    except (ValueError, KeyError, TypeError) as exc:
                        arm_entry.update(status="fit_failed", reason=str(exc))
                        report["errors"].append(key + ":" + arm + ":" + str(exc))
                entry["status"] = "completed_with_failures" if any(a["status"] == "fit_failed" for a in entry["arms"]) else "completed"
            except (ValueError, KeyError, TypeError, OSError) as exc:
                entry.update(status="source_rejected", reason=str(exc))
                report["errors"].append(key + ":" + str(exc))
        report["completed_at"] = _iso(_tick(clock, started))
        report["captured_forecasts"] = sum(c["status"] == "captured" for e in report["competitions"] for a in e["arms"] for c in a["forecasts"])
        report["captured_matches"] = len({(e["competition"], c["match_id"]) for e in report["competitions"] for a in e["arms"] for c in a["forecasts"] if c["status"] == "captured"})
        report["storage_budget"] = {**_budget_public(_BUDGET.get()), "observation": "before_run_and_latest_report_writes"}
        report["report_id"] = _append(root, "runs", report)
        captured_by_arm = {arm: sum(c["status"] == "captured" for e in report["competitions"] for a in e["arms"] if a["arm"] == arm for c in a["forecasts"]) for arm, _ in ARMS}
        primary_pairs = sum(len(set(c["match_id"] for a in e["arms"] if a["arm"] == "baseline180" for c in a["forecasts"] if c["status"] == "captured") &
            set(c["match_id"] for a in e["arms"] if a["arm"] == "candidate90" for c in a["forecasts"] if c["status"] == "captured")) for e in report["competitions"])
        summary = {k: v for k, v in report.items() if k != "competitions"}
        summary.update(generated_at=report["completed_at"], run_started_at=report["started_at"],
            competition_count=len(report["competitions"]), captured_by_arm=captured_by_arm,
            eligibility_rejections=sum(len(e["coverage"]) for e in report["competitions"]),
            eligibility_rejection_reasons=dict(Counter(c["reason"].split(":", 1)[0] for e in report["competitions"] for c in e["coverage"])),
            arm_failures=dict(Counter(a["arm"] for e in report["competitions"] for a in e["arms"] if a["status"] == "fit_failed")),
            arm_forecast_rejections=dict(Counter(a["arm"] for e in report["competitions"] for a in e["arms"] for c in a["forecasts"] if c["status"] != "captured")),
            primary_pair_captures_this_run=primary_pairs,
            competitions=[{k: v for k, v in e.items() if k not in ("arms", "coverage")} for e in report["competitions"]])
        _latest(root, "latest.json", summary)
        return int(bool(report["errors"])), report


def _verified_result(production, result, forecast, now):
    if not _same_identity(result, forecast) or result["competition"] != forecast["competition"]:
        raise ValueError("result_identity_conflict")
    if result.get("collection_method") != "independent_fixture_sync":
        raise ValueError("independent_result_response_required")
    observed = _timestamp(result["observed_at"], "result.observed_at")
    if not observed <= _timestamp(result["captured_at"], "result.captured_at") <= now:
        raise ValueError("result_timestamp_invalid")
    source, _ = _read(production, "sources", _name(result["source_id"]) + ".json")
    if _digest(source) != result["source_id"] or source.get("kind") != "independent_fixture_result_response":
        raise ValueError("result_source_hash_or_kind_mismatch")
    envelope = source["content"]
    mid = result["match_id"]
    if source.get("client_response_sha256") != _digest(envelope) or source.get("request") != {"fixture_id": mid}:
        raise ValueError("result_response_hash_or_request_mismatch")
    if envelope.get("errors") or envelope.get("endpoint", "/fixtures") != "/fixtures" or envelope.get("params", {"id": mid}) != {"id": mid}:
        raise ValueError("result_response_request_or_errors")
    paging = envelope.get("paging", {})
    if not isinstance(paging, dict) or any(type(paging.get(k, 1)) is not int or paging.get(k, 1) != 1 for k in ("current", "total")):
        raise ValueError("result_response_partial")
    if _timestamp(envelope["fetched_at"], "fetched_at") != observed:
        raise ValueError("result_response_time_mismatch")
    items = envelope.get("response")
    if not isinstance(items, list) or len(items) != 1 or envelope.get("results", 1) != 1:
        raise ValueError("result_response_count")
    if items[0].get("league", {}).get("id") != forecast["parents"]["provider_league_id"]:
        raise ValueError("result_competition_mismatch")
    rows = normalize_fixtures(items, observed)
    if len(rows) != 1 or not _same_identity(rows[0], forecast):
        raise ValueError("result_not_mature_or_identity_mismatch")
    if any(rows[0][k] != result[k] for k in ("home_goals", "away_goals")):
        raise ValueError("result_score_disagrees_with_raw")
    return source


def _saved_forecast_valid(root, forecast, now):
    if (forecast.get("research_replay") or forecast.get("live_before_kickoff") is not True
            or forecast.get("production_ledger_eligible") is not False):
        raise ValueError("not_a_live_research_shadow")
    cutoff = _timestamp(forecast["input_cutoff"], "input_cutoff")
    fitted = _timestamp(forecast["model_fitted_at"], "model_fitted_at")
    generated = _timestamp(forecast["forecast_generated_at"], "forecast_generated_at")
    captured = _timestamp(forecast["captured_at"], "captured_at")
    payload, _ = _read(root, "forecast_payloads", _name(forecast["payload_id"]) + ".json")
    if _digest(payload) != forecast["payload_id"] or payload != {k: v for k, v in forecast.items()
            if k not in ("payload_id", "storage_completed_at", "captured_at", "live_before_kickoff", "object_id")}:
        raise ValueError("shadow_durable_payload_binding_mismatch")
    if (forecast["storage_completed_at"] != forecast["captured_at"] or
            not generated <= _timestamp(payload["storage_started_at"], "storage_started_at") <= captured):
        raise ValueError("shadow_storage_completion_time_invalid")
    if not cutoff <= fitted <= generated <= captured <= now or captured >= _timestamp(forecast["kickoff"], "kickoff"):
        raise ValueError("shadow_actual_time_order_invalid")
    parents = {}
    for collection, field in (("models", "model_id"), ("inputs", "input_id"), ("protocols", "protocol_id")):
        value, _ = _read(root, collection, _name(forecast[field]) + ".json")
        if _digest(value) != forecast[field]:
            raise ValueError("shadow_parent_hash_mismatch")
        parents[collection] = value
    model = parents["models"]
    if (model["arm"] != forecast["arm"] or model["input_id"] != forecast["input_id"]
            or model["protocol_id"] != forecast["protocol_id"] or model["model_fitted_at"] != forecast["model_fitted_at"]
            or parents["inputs"]["input_cutoff"] != forecast["input_cutoff"]):
        raise ValueError("shadow_model_input_binding_mismatch")
    if _timestamp(parents["protocols"]["registered_at"], "registered_at") > cutoff:
        raise ValueError("shadow_protocol_registered_after_cutoff")
    for field in ("competition", "input_cutoff", "parents", "source_commit", "state_commit", "config_source", "implementation_sha256"):
        if not forecast[field] == model[field] == parents["inputs"][field]:
            raise ValueError("shadow_common_provenance_mismatch:" + field)
    source_values = {}
    for field, kind in (("data_source", "normalized_regulation_data"), ("schedule_source", "production_schedule"),
                        ("report_source", "production_report")):
        sid = _name(forecast["parents"][field])
        source, _ = _read(root, "sources", sid + ".json")
        if _digest(source) != sid or source.get("kind") != kind:
            raise ValueError("shadow_input_source_hash_or_kind_mismatch")
        source_values[field] = source["content"]
    config, _ = _read(root, "sources", _name(forecast["config_source"]) + ".json")
    if _digest(config) != forecast["config_source"] or config.get("kind") != "config":
        raise ValueError("shadow_config_source_hash_mismatch")
    spec = parents["protocols"]["spec"]
    arm = next((a for a in spec["arms"] if a["arm"] == forecast["arm"]), None)
    if (arm is None or model["model"]["half_life_days"] != arm["half_life_days"]
            or model["half_life_days"] != arm["half_life_days"]
            or model["model"]["ridge"] != spec["ridge"] or spec["ridge"] != config["content"]["ridge"]
            or spec["min_train"] != config["content"]["min_train"]):
        raise ValueError("shadow_registered_fit_parameters_mismatch")
    data = source_values["data_source"]
    if _timestamp(data["observed_at"], "data.observed_at") > cutoff:
        raise ValueError("shadow_training_source_after_cutoff")
    eligible = [m for m in _validated_matches(data["matches"]) if m["_dt"] + RESULT_AVAILABILITY_DELAY <= cutoff]
    if len(eligible) < spec["min_train"] or _timestamp(model["model"]["training_cutoff"], "model.training_cutoff") != cutoff:
        raise ValueError("shadow_training_cutoff_or_minimum_mismatch")
    serial = [{k: v for k, v in m.items() if k != "_dt"} for m in eligible]
    training_sha = hashlib.sha256(json.dumps(serial, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    if model["model"]["training_data_sha256"] != training_sha or model["model"]["n_matches"] != len(eligible):
        raise ValueError("shadow_training_input_fingerprint_mismatch")
    schedule = [r for r in source_values["schedule_source"] if _same_identity(r, forecast)]
    if len(schedule) != 1 or schedule[0]["competition"] != forecast["competition"]:
        raise ValueError("shadow_schedule_identity_mismatch")
    competition = next(c for c in config["content"]["competitions"] if c["key"] == forecast["competition"])
    neutral = False
    if competition["mode"] == "national_baseline":
        from .nations import validate_nations_forecast
        venue_source = forecast["venue"]["venue"]["registry_source"]
        registry, _ = _read(root, "sources", _name(venue_source) + ".json")
        if _digest(registry) != venue_source or registry["kind"] != "national_registry":
            raise ValueError("shadow_national_registry_source_mismatch")
        neutral = validate_nations_forecast(schedule[0], registry["content"])
    p = forecast["prediction"]
    if not _same_identity({"match_id": forecast["match_id"], **p}, forecast):
        raise ValueError("shadow_prediction_identity_mismatch")
    if p["neutral"] != neutral:
        raise ValueError("shadow_neutral_does_not_match_source")
    recomputed = score_distribution(model["model"], forecast["home_id"], forecast["away_id"], forecast["kickoff"], neutral)
    if not _numeric_replay_equivalent(recomputed, p):
        raise ValueError("shadow_prediction_does_not_recompute")


def _latest(root, filename, value):
    from .cli import atomic_json
    path = _path(root, "reports", filename)
    # atomic_json preserves insertion order rather than sorting keys.
    raw = (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode()
    cost = _write_cost(path, raw)
    atomic_json(path, value)
    _record_write(cost)


def _score(runtime, *, clock=None):
    """Score first paired capture matching final identity; revisions stay separate."""
    import math
    clock = clock or (lambda: datetime.now(timezone.utc))
    runtime = Path(runtime)
    with _writer(runtime) as root:
        now = _now(clock)
        production = _path(runtime, "prospective")
        forecasts = _objects(root, "forecasts")
        results = _objects(production, "results")
        grouped = defaultdict(list)
        for forecast in forecasts:
            _saved_forecast_valid(root, forecast, now)
            grouped[(forecast["protocol_id"], forecast["competition"], forecast["match_id"])].append(forecast)
        report = {"version": VERSION, "scored_at": _iso(now), "selection": "first_pair_capture_matching_final_identity_no_extra_match",
            "numerical_environment": _numerical_environment(),
            "numeric_replay_matrix_abs_tol": NUMERIC_REPLAY_MATRIX_ABS_TOL,
            "numeric_replay_probability_abs_tol": NUMERIC_REPLAY_PROBABILITY_ABS_TOL,
            "forecast_versions": len(forecasts), "selected_matches": 0, "incomplete_groups": [],
            "matches": [], "errors": [], "profit_evaluation": False, "accuracy_improvement_verified": False}
        selected_matches = set()
        for key, all_values in sorted(grouped.items()):
            available = [r for r in results if r["competition"] == key[1] and r["match_id"] == key[2]]
            result = max(available, key=lambda r: (r["observed_at"], r["captured_at"], r["object_id"])) if available else None
            matching = [v for v in all_values if result is None or _same_identity(v, result)]
            identity_exclusions = [v["object_id"] for v in all_values if v not in matching]
            inputs = defaultdict(dict)
            for v in matching:
                if v["arm"] in inputs[v["input_id"]]:
                    raise ValueError("duplicate_arm_in_frozen_capture")
                inputs[v["input_id"]][v["arm"]] = v
            for candidate in ("candidate90", "candidate365"):
                base_record = {"protocol_id": key[0], "competition": key[1], "match_id": key[2],
                    "comparison": candidate + "_vs_baseline180", "identity_excluded_forecast_ids": identity_exclusions}
                pairs = [arms for arms in inputs.values() if "baseline180" in arms and candidate in arms]
                if not pairs:
                    report["incomplete_groups"].append({**base_record, "reason": "no_complete_pair_matching_final_identity"})
                    continue
                arms = min(pairs, key=lambda a: max(a[candidate]["captured_at"], a["baseline180"]["captured_at"]))
                values = [arms["baseline180"], arms[candidate]]
                if not _same_identity(values[0], values[1]):
                    raise ValueError("paired_shadow_identity_mismatch")
                selected_matches.add(key)
                prototype = values[0]
                record = {**base_record, "input_id": prototype["input_id"]}
                report["matches"].append(record)
                if result is None:
                    record.update(status="pending", reason="no_independent_result_available")
                    continue
                try:
                    source = _verified_result(production, result, prototype, now)
                    result_id = _append(root, "results", {"production_result_id": result["object_id"], "content": {k: v for k, v in result.items() if k != "object_id"}, "source": source})
                    home, away = result["home_goals"], result["away_goals"]
                    outcome = 0 if home > away else 1 if home == away else 2
                    scores = {}
                    for forecast in values:
                        p = forecast["prediction"]["probabilities"]
                        probabilities = [p[k] for k in ("home_win", "draw", "away_win")]
                        matrix = matrix_array(forecast["prediction"]["score_matrix"])
                        total_p = sum(float(matrix[h, a]) for h in range(matrix.shape[0]) for a in range(matrix.shape[1]) if h + a > 2.5)
                        scores[forecast["arm"]] = {"forecast_id": forecast["object_id"],
                            "brier_1x2": sum((v - int(i == outcome)) ** 2 for i, v in enumerate(probabilities)),
                            "log_loss_1x2": -math.log(max(1e-15, probabilities[outcome])),
                            "log_loss_floor": 1e-15, "over_2_5_brier": (total_p - int(home + away > 2.5)) ** 2}
                    differences = {metric: scores[candidate][metric] - scores["baseline180"][metric]
                                   for metric in ("brier_1x2", "log_loss_1x2", "over_2_5_brier")}
                    record.update(status="scored", result_id=result_id, arms=scores, candidate_minus_baseline=differences)
                    record["score_id"] = _append(root, "scores", record)
                except (ValueError, TypeError, KeyError, OSError) as exc:
                    record.update(status="rejected", reason=str(exc))
                    report["errors"].append(key[1] + ":" + str(key[2]) + ":" + str(exc))
        report["selected_matches"] = len(selected_matches)
        report["status_counts"] = dict(Counter(r["status"] for r in report["matches"]))
        report["comparisons"] = []
        for comparison in ("candidate90_vs_baseline180", "candidate365_vs_baseline180"):
            records = [r for r in report["matches"] if r["comparison"] == comparison]
            scored = [r for r in records if r["status"] == "scored"]
            report["comparisons"].append({"comparison": comparison, "status_counts": dict(Counter(r["status"] for r in records)),
                "n_scored_matches": len(scored), "mean_candidate_minus_baseline": {metric:
                sum(r["candidate_minus_baseline"][metric] for r in scored) / len(scored) if scored else None
                for metric in ("brier_1x2", "log_loss_1x2", "over_2_5_brier")},
                "inferential_status": "descriptive_only_no_promotion_or_significance_claim"})
        report["storage_budget"] = {**_budget_public(_BUDGET.get()), "observation": "before_score_run_and_latest_report_writes"}
        report["report_id"] = _append(root, "score_runs", report)
        _latest(root, "score-latest.json", {k: v for k, v in report.items() if k != "matches"})
        return int(bool(report["errors"])), report


def _capacity_report(runtime, operation, error, started, clock, source_commit=None):
    """Bounded diagnostics may use 256KiB of the reserved production headroom."""
    from .cli import atomic_json
    runtime = Path(runtime)
    root = _path(runtime, "shadow_models")
    completed = _iso(_now(clock))
    value = {"version": VERSION, "status": "capacity_deferred", "operation": operation,
        "numerical_environment": _numerical_environment(),
        "run_started_at": _iso(started), "started_at": _iso(started), "generated_at": completed,
        "completed_at": completed, "source_commit": source_commit,
        "reason": "shadow_storage_capacity_deferred", **error.budget,
        "capacity_report_reserve_bytes": CAPACITY_REPORT_RESERVE_BYTES,
        "partial_objects_preserved": True, "counts_for_partial_capture": None,
        "production_models_changed": False, "accuracy_improvement_verified": False}
    if operation == "score":
        value["scored_at"] = completed
        value["numeric_replay_matrix_abs_tol"] = NUMERIC_REPLAY_MATRIX_ABS_TOL
        value["numeric_replay_probability_abs_tol"] = NUMERIC_REPLAY_PROBABILITY_ABS_TOL
    latest_path = _path(root, "reports", "latest.json" if operation == "capture" else "score-latest.json")
    previous = latest_path.read_bytes() if latest_path.exists() else None
    previous_cost = storage_cost(latest_path.relative_to(runtime).as_posix(), previous) if previous is not None else 0
    value.update(immutable_event_saved=True, diagnostic_evidence="immutable_capacity_event_and_current_latest")
    event_raw = _json_bytes(value)
    event_path = _path(root, "capacity_events", _digest(value) + ".json")
    event_cost = 0 if event_path.exists() else storage_cost(event_path.relative_to(runtime).as_posix(), event_raw)
    event_raw_size = 0 if event_path.exists() else len(event_raw)
    candidate = {**value, "capacity_event_id": _digest(value)}
    latest_raw = (json.dumps(candidate, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode()
    latest_cost = storage_cost(latest_path.relative_to(runtime).as_posix(), latest_raw)
    persisted = error.budget["estimated_persisted_bytes"]
    logical = error.budget["logical_bytes"]
    # Only bounded diagnostics may use the small reserved headroom. Capacity is
    # measured in the same archive units as normal objects, not raw/gzip mixed.
    if (persisted + event_cost + max(0, latest_cost - previous_cost) < STORAGE_LIMIT_BYTES + CAPACITY_REPORT_RESERVE_BYTES
            and logical + event_raw_size + len(latest_raw) - (len(previous) if previous is not None else 0) <= MAX_LOGICAL_BYTES):
        value["capacity_event_id"] = _ledger_append(root, "capacity_events", value)
    else:
        value["capacity_event_id"] = None
        value["immutable_event_saved"] = False
        value["diagnostic_evidence"] = "bounded_current_latest_only"
    raw = (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode()
    if logical + len(raw) - (len(previous) if previous is not None else 0) > MAX_LOGICAL_BYTES:
        raise ValueError("shadow_capacity_diagnostic_exceeds_logical_limit")
    atomic_json(latest_path, value)
    return 1, value


def capture(runtime, config, *, source_commit, state_commit=None, league="all", clock=None):
    clock = clock or (lambda: datetime.now(timezone.utc))
    started = _now(clock)
    try:
        return _capture(runtime, config, source_commit=source_commit, state_commit=state_commit, league=league, clock=clock)
    except ShadowCapacityError as exc:
        return _capacity_report(runtime, "capture", exc, started, clock, source_commit)


def score(runtime, *, clock=None):
    clock = clock or (lambda: datetime.now(timezone.utc))
    started = _now(clock)
    try:
        return _score(runtime, clock=clock)
    except ShadowCapacityError as exc:
        return _capacity_report(runtime, "score", exc, started, clock)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    capture_parser = sub.add_parser("capture")
    capture_parser.add_argument("--runtime", type=Path, default=Path("runtime"))
    capture_parser.add_argument("--config", type=Path, default=Path("config.json"))
    capture_parser.add_argument("--source-commit", required=True)
    capture_parser.add_argument("--state-commit")
    capture_parser.add_argument("--league", default="all")
    score_parser = sub.add_parser("score")
    score_parser.add_argument("--runtime", type=Path, default=Path("runtime"))
    args = parser.parse_args(argv)
    if args.command == "capture":
        code, report = capture(args.runtime, json.loads(args.config.read_text()), source_commit=args.source_commit,
                               state_commit=args.state_commit, league=args.league)
    else:
        code, report = score(args.runtime)
    print(json.dumps({k: v for k, v in report.items() if k not in ("competitions", "matches")}, ensure_ascii=False, allow_nan=False))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
