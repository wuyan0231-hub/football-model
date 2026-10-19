"""Append-only, wall-clock prospective forecasts and separate result revisions.

This command has no historical --as-of and never fits a team model. Immutable
objects are content addressed; latest.json is a replaceable derived view only.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import fcntl
import json
import math
import os
from pathlib import Path
import re
from typing import Any

import numpy as np

from .competition_scope import is_retired, require_league
from .model import (RESULT_AVAILABILITY_DELAY, _distribution_from_rates, _integer,
                    _iso, _timestamp, _validated_matches)
from .quality import _categorical_metrics as _floored_categorical_metrics
from .settlement import OUTCOMES, STATES, contract, matrix_array, probabilities, settlement, unit_return

VERSION = "prospective_frozen_forecasts_v1"
TIME_SEMANTICS = "observed_wall_clock_v1"
AXES = {"rows": "home_goals_from_0", "columns": "away_goals_from_0"}


def _categorical_metrics(probabilities: list, outcomes: list, labels: tuple) -> dict | None:
    """Retain a numeric floored display while exposing mathematical infinite loss."""
    result = _floored_categorical_metrics(probabilities, outcomes, labels)
    if result is None:
        return None
    actual_probabilities = np.asarray(probabilities)[np.arange(len(outcomes)), outcomes]
    zeros = int(np.count_nonzero(actual_probabilities == 0))
    floored = int(np.count_nonzero(actual_probabilities < 1e-15))
    result.update(log_loss_definition="mean(-log(max(1e-15,p_observed)))",
                  log_loss_probability_floor=1e-15, floor_affected_outcomes=floored,
                  observed_zero_probability_outcomes=zeros,
                  mathematical_log_loss_status="positive_infinity" if zeros else "finite",
                  mathematical_log_loss=None if zeros else float(-np.log(actual_probabilities).mean()))
    return result


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False, indent=2) + "\n").encode()


def _digest(value: Any) -> str:
    return hashlib.sha256(_json_bytes(value)).hexdigest()


def _name(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", value):
        raise ValueError("unsafe path component")
    return value


def _path(root: Path, *parts: str) -> Path:
    path = root.joinpath(*parts)
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError("path escapes runtime root")
    # No symlinked object directories, even if they happen to resolve inside root.
    cursor = path
    while cursor != root and cursor != cursor.parent:
        if cursor.is_symlink():
            raise ValueError("symlinked runtime paths are not permitted")
        cursor = cursor.parent
    return path


def _read(root: Path, *parts: str) -> tuple[Any, str]:
    raw = _path(root, *parts).read_bytes()
    value = json.loads(raw, parse_constant=lambda v: (_ for _ in ()).throw(ValueError("nonfinite JSON")))
    return value, hashlib.sha256(raw).hexdigest()


def _append(root: Path, collection: str, value: dict) -> str:
    object_id = _digest(value)
    path = _path(root, collection, object_id + ".json")
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = _json_bytes(value)
    try:
        with path.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError:
        if path.read_bytes() != payload:
            raise ValueError("immutable object collision or corruption")
    return object_id


def _objects(root: Path, collection: str) -> list[dict]:
    directory = _path(root, collection)
    result = []
    for path in sorted(directory.glob("*.json")):
        value, _ = _read(root, collection, path.name)
        if not isinstance(value, dict) or path.stem != _digest(value):
            raise ValueError("immutable object hash mismatch in " + collection)
        result.append({**value, "object_id": path.stem})
    return result


def _snapshot(root: Path, kind: str, content: Any, sha: str) -> str:
    return _append(root, "sources", {"kind": kind, "original_bytes_sha256": sha, "content": content})


def _now(clock) -> datetime:
    value = clock()
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("clock must return an aware datetime")
    return value.astimezone(timezone.utc)


def _identity(row: dict) -> dict:
    result = {field: _integer(row.get(field), field, 1) for field in ("match_id", "home_id", "away_id")}
    if result["home_id"] == result["away_id"]:
        raise ValueError("home and away identities coincide")
    result["kickoff"] = _iso(_timestamp(row.get("kickoff"), "kickoff"))
    return result


def _same_identity(left: dict, right: dict) -> bool:
    return _identity(left) == _identity(right)


def _event_order(row: dict) -> tuple:
    return (_timestamp(row["observed_at"], "observed_at"),
            _timestamp(row["captured_at"], "captured_at"), row["object_id"])


def record_schedule_revision(root: Path, competition: str, row: dict, *, observed: datetime,
                             captured: datetime, source_id: str, forecasts: list[dict],
                             collection_method: str) -> str:
    """Keep a changed kickoff as unscorable evidence, never as an altered result."""
    identity = _identity(row)
    relevant = [f for f in forecasts if f["competition"] == competition and f["match_id"] == identity["match_id"]]
    if (not relevant or any(_same_identity(f, identity) for f in relevant)
            or not any(all(f[k] == identity[k] for k in ("match_id", "home_id", "away_id")) for f in relevant)):
        raise ValueError("result_identity_or_kickoff_mismatch")
    if observed > captured:
        raise ValueError("result_source_missing_or_future_observation")
    source, _ = _read(root, "sources", source_id + ".json")
    if _digest(source) != source_id:
        raise ValueError("schedule_revision_source_integrity_mismatch")
    originals = []
    for forecast in relevant:
        ledger = forecast.get("_result_sync_ledger", "prospective")
        source_root = root if ledger == "prospective" else _path(root.parent, "shadow_models")
        original, _ = _read(source_root, "forecasts", forecast["object_id"] + ".json")
        if _digest(original) != forecast["object_id"]:
            raise ValueError("schedule_revision_forecast_integrity_mismatch")
        source_ids = {forecast[k] for k in ("source_id", "report_source", "model_source") if forecast.get(k)}
        source_ids.update(v for k, v in forecast.get("parents", {}).items() if k.endswith("_source") and isinstance(v, str))
        frozen_source_refs = []
        for sid in sorted(source_ids):
            frozen_source, _ = _read(source_root, "sources", _name(sid) + ".json")
            if _digest(frozen_source) != sid:
                raise ValueError("schedule_revision_original_source_integrity_mismatch")
            frozen_source_refs.append({"path": f"{ledger}/sources/{sid}.json", "sha256": sid})
        originals.append({"ledger": ledger, "forecast_id": forecast["object_id"], "identity": _identity(forecast),
            "forecast_ref": {"path": f"{ledger}/forecasts/{forecast['object_id']}.json", "sha256": _digest(original)},
            "frozen_source_refs": frozen_source_refs,
            "prediction_source_id": forecast.get("source_id", forecast.get("parents", {}).get("prediction_source")),
            "report_source_id": forecast.get("report_source", forecast.get("parents", {}).get("report_source"))})
    previous = [r for r in _objects(root, "schedule_revisions")
                if r["competition"] == competition and r["match_id"] == identity["match_id"]]
    latest = max(previous, key=_event_order, default=None)
    if latest and observed < _timestamp(latest["observed_at"], "observed_at"):
        if collection_method == "pipeline_normalized_data":
            return latest["object_id"]
        raise ValueError("schedule_revision_observation_regressed")
    if latest and latest["source_id"] == source_id and latest["original_forecasts"] == originals:
        return latest["object_id"]
    return _append(root, "schedule_revisions", {"schema_version": 1, "competition": competition, **identity,
        "status": "unscorable_schedule_revision", "observed_at": _iso(observed), "captured_at": _iso(captured),
        "source_id": source_id, "source_ref": {"path": f"prospective/sources/{source_id}.json", "sha256": _digest(source)},
        "original_forecasts": originals, "collection_method": collection_method,
        "supersedes": latest["object_id"] if latest else None, "original_forecasts_unchanged": True,
        "result_appended": False, "paired_scoring_eligible": False})


def current_results(root: Path, *, as_of: datetime | None = None) -> list[dict]:
    """Preserve history while excluding results superseded by schedule evidence."""
    revisions = {}
    for row in _objects(root, "schedule_revisions"):
        if as_of is not None and max(_timestamp(row["observed_at"], "observed_at"),
                                    _timestamp(row["captured_at"], "captured_at")) > as_of:
            continue
        key = (row["competition"], row["match_id"])
        if key not in revisions or _event_order(row) > _event_order(revisions[key]):
            revisions[key] = row
    return [r for r in _objects(root, "results") if not (
        (revision := revisions.get((r["competition"], r["match_id"])))
        and _event_order(revision) >= _event_order(r) and not _same_identity(revision, r))]


def make_protocol(config: dict) -> dict:
    settings = config.get("prospective", {})
    if not isinstance(settings, dict):
        raise ValueError("prospective config must be an object")
    defaults = []
    for line in (-1.5, -0.5, 0, 0.5, 1.5):
        defaults.extend([{"market": "asian_handicap", "side": "home", "line": line},
                         {"market": "asian_handicap", "side": "away", "line": -line}])
    for line in (2.25, 2.5, 2.75):
        defaults.extend({"market": "total_goals", "side": side, "line": line} for side in ("over", "under"))
    if set(settings) - {"contracts"}:
        raise ValueError("unknown prospective settings")
    contracts = settings.get("contracts", defaults)
    if not isinstance(contracts, list) or len(contracts) > 100:
        raise ValueError("contracts must be a list of at most 100 predeclared contracts")
    contracts = [contract(value) for value in contracts]
    if len({_digest(c) for c in contracts}) != len(contracts):
        raise ValueError("duplicate diagnostic contracts")
    return {"version": VERSION, "target": "regulation_time_90_plus_stoppage", "contracts": contracts,
            "primary_forecast": "first_valid_actual_precapture_matching_final_identity_per_competition_match",
            "reschedule_policy": "retain_identity_changed_versions_use_first_matching_final_identity_no_extra_match",
            "baseline": "unweighted_poisson_league_mean_plus_0_5_smoothing_same_training_digest",
            "baseline_comparison": "paired_common_matches_only",
            "structural_zero_policy": "exclude_impossible_states_from_categorical_calibration",
            "result_policy": "latest_observed_valid_revision_keep_all_previous_versions",
            "probability_log_floor": 1e-15, "calibration_bins": 10,
            "quote_max_age_seconds": 300, "quote_max_forecast_age_seconds": 3600,
            "simulation": "first_registered_eligible_quote_per_match_and_contract_one_unit_no_ev_filter",
            "quote_selection_caveat": "input quote set is externally selected; no claim of a validated betting strategy",
            "profit_cost_definition": "fixed_cost_per_one_unit_stake_subtracted_in_every_settlement_state",
            "automatic_quality_acceptance": False}


def _forecast_valid(row: dict, report: dict, capture: datetime) -> None:
    if row.get("research_replay") or report.get("research_replay"):
        raise ValueError("research_replay_is_not_prospective")
    identity = _identity(row)
    kickoff = _timestamp(identity["kickoff"], "kickoff")
    if capture >= kickoff:
        raise ValueError("capture_at_or_after_kickoff_not_prospective")
    if row.get("time_semantics") != TIME_SEMANTICS or report.get("time_semantics") != TIME_SEMANTICS:
        raise ValueError("missing_observed_wall_clock_semantics")
    start = _timestamp(report.get("run_started_at"), "run_started_at")
    if _timestamp(row.get("run_started_at"), "row.run_started_at") != start:
        raise ValueError("forecast_does_not_belong_to_current_run")
    generated = _timestamp(row.get("forecast_generated_at"), "forecast_generated_at")
    observed = _timestamp(row.get("data_observed_at"), "data_observed_at")
    cutoff = _timestamp(row.get("training_cutoff"), "training_cutoff")
    fitted = _timestamp(row.get("model_fitted_at"), "model_fitted_at")
    report_generated = _timestamp(report.get("generated_at"), "report.generated_at")
    if not (start <= observed <= generated <= report_generated <= capture < kickoff):
        raise ValueError("invalid_actual_observation_generation_capture_order")
    if not cutoff <= fitted <= generated:
        raise ValueError("invalid_model_cutoff_fit_generation_order")
    prediction = row.get("prediction")
    if not isinstance(prediction, dict):
        raise ValueError("missing_prediction")
    for field in ("home_id", "away_id", "kickoff"):
        if prediction.get(field) != row.get(field):
            raise ValueError("prediction_identity_mismatch")
    if prediction.get("training_cutoff") != row.get("training_cutoff"):
        raise ValueError("prediction_training_cutoff_mismatch")
    if not prediction.get("model_version") or not prediction.get("training_data_sha256") or not row.get("model_id"):
        raise ValueError("missing_model_version_provenance")
    if prediction.get("score_matrix_axes") != AXES:
        raise ValueError("unknown_score_matrix_axes")
    matrix = matrix_array(prediction.get("score_matrix"))
    h, a = np.indices(matrix.shape)
    for outcome, mask in zip(OUTCOMES, (h > a, h == a, h < a)):
        supplied = prediction.get("probabilities", {}).get(outcome)
        if (isinstance(supplied, bool) or not isinstance(supplied, (int, float)) or
                not math.isfinite(supplied) or not math.isclose(supplied, float(matrix[mask].sum()), abs_tol=1e-8)):
            raise ValueError("1X2_probabilities_disagree_with_matrix")


def _baseline(data: dict, row: dict, source_id: str) -> dict:
    observed = _timestamp(data.get("observed_at"), "data.observed_at")
    generated = _timestamp(row["forecast_generated_at"], "generated_at")
    if data.get("time_semantics") != TIME_SEMANTICS or observed > generated:
        raise ValueError("baseline_data_not_available_at_forecast")
    cutoff = _timestamp(row["training_cutoff"], "training_cutoff")
    eligible = [value for value in _validated_matches(data.get("matches"))
                if value["_dt"] + RESULT_AVAILABILITY_DELAY <= cutoff]
    serial = [{k: v for k, v in value.items() if k != "_dt"} for value in eligible]
    # This is exactly model.fit_model's documented training fingerprint encoding.
    training_digest = hashlib.sha256(json.dumps(serial, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    if not eligible or training_digest != row["prediction"]["training_data_sha256"]:
        raise ValueError("baseline_training_digest_does_not_match_forecast")
    home = (sum(value["home_goals"] for value in eligible) + 0.5) / (len(eligible) + 0.5)
    away = (sum(value["away_goals"] for value in eligible) + 0.5) / (len(eligible) + 0.5)
    if row["prediction"].get("neutral"):
        home = away = (home + away) / 2
    return {"status": "available", "source_id": source_id, "training_cutoff": row["training_cutoff"],
            "source_observed_at": data["observed_at"], "training_data_sha256": training_digest,
            "n_training_matches": len(eligible), "prediction": _distribution_from_rates(home, away)}


def capture_current(runtime: Path, root: Path, report: dict, report_sha: str,
                    protocol_id: str, clock) -> list[str]:
    errors = []
    run_id = _name(report.get("run_id"))
    report_source = _snapshot(root, "run_report", report, report_sha)
    sequence = max((r.get("capture_sequence", 0) for r in _objects(root, "forecasts")), default=0)
    existing = {(r["run_id"], r["competition"], r.get("match_id")): r for r in _objects(root, "coverage")}
    requested = report.get("requested_competitions", [])
    if not isinstance(requested, list) or len(set(requested)) != len(requested):
        raise ValueError("invalid requested competitions")
    items = {item["key"]: item for item in report.get("competitions", [])}
    for key in requested:
        if is_retired(key):
            continue
        key = _name(key)
        item = items.get(key, {})
        filename = _path(runtime, "predictions", run_id, key + ".json")
        if not filename.exists():
            entry = {"run_id": run_id, "competition": key, "match_id": None,
                     "captured_at": _iso(_now(clock)), "status": "no_current_prediction_file",
                     "report_source": report_source, "reported_status": item.get("status", "unreported"),
                     "reported_availability": item.get("forecast_availability"), "protocol_id": protocol_id}
            if (run_id, key, None) not in existing:
                _append(root, "coverage", entry)
            continue
        try:
            rows, source_sha = _read(runtime, "predictions", run_id, key + ".json")
            if not isinstance(rows, list):
                raise ValueError("predictions must be a list")
            ids = [_integer(row.get("match_id"), "match_id", 1) for row in rows]
            if len(set(ids)) != len(ids):
                raise ValueError("duplicate prediction match identities")
            source_id = _snapshot(root, "prediction_file", rows, source_sha)
        except (ValueError, TypeError, KeyError, OSError) as exc:
            errors.append(key + ":" + str(exc))
            _append(root, "coverage", {"run_id": run_id, "competition": key, "match_id": None,
                "captured_at": _iso(_now(clock)), "status": "capture_error", "reason": str(exc),
                "report_source": report_source, "protocol_id": protocol_id})
            continue
        try:
            data, data_sha = _read(runtime, "data", key + ".json")
            data_id = _snapshot(root, "normalized_regulation_data", data, data_sha)
        except (ValueError, TypeError, KeyError, OSError):
            data, data_id = {}, None
        if not rows and (run_id, key, None) not in existing:
            _append(root, "coverage", {"run_id": run_id, "competition": key, "match_id": None,
                "captured_at": _iso(_now(clock)), "status": "no_upcoming_matches", "report_source": report_source,
                "source_id": source_id, "protocol_id": protocol_id})
        for row in rows:
            previous = existing.get((run_id, key, row["match_id"]))
            if previous:
                if previous.get("source_id") != source_id:
                    errors.append(key + ":immutable_run_prediction_file_changed")
                continue
            capture = _now(clock)
            entry = {"run_id": run_id, "competition": key, "match_id": row["match_id"],
                "captured_at": _iso(capture), "source_id": source_id, "report_source": report_source,
                "protocol_id": protocol_id, "reported_status": row.get("status")}
            try:
                entry["identity"] = _identity(row)
                if row.get("competition") != key:
                    raise ValueError("competition_mismatch")
                if row.get("status") != "exploratory_baseline":
                    entry.update(status="unsupported", reason=row.get("reason", "source_not_supported"))
                else:
                    _forecast_valid(row, report, capture)
                    model, model_sha = _read(runtime, "models", key + ".json")
                    if (model.get("model_id") != row["model_id"] or
                            model.get("competition", {}).get("key") != key or
                            model.get("model", {}).get("training_data_sha256") != row["prediction"]["training_data_sha256"]):
                        raise ValueError("model_snapshot_provenance_mismatch")
                    model_id = _snapshot(root, "model_bundle", model, model_sha)
                    try:
                        baseline = _baseline(data, row, data_id)
                    except (ValueError, KeyError, TypeError) as exc:
                        baseline = {"status": "unavailable", "reason": str(exc), "source_id": data_id}
                    capture = _now(clock)
                    _forecast_valid(row, report, capture)
                    entry["captured_at"] = _iso(capture)
                    sequence += 1
                    forecast = {"schema_version": 1, "capture_sequence": sequence, "protocol_id": protocol_id, "run_id": run_id,
                        "competition": key, **_identity(row), "captured_at": _iso(capture),
                        "source_id": source_id, "report_source": report_source, "model_source": model_id,
                        "source_commit": report.get("source_commit"), "forecast": row, "baseline": baseline}
                    entry.update(status="prospective", forecast_id=_append(root, "forecasts", forecast))
            except (ValueError, TypeError, KeyError, OSError) as exc:
                entry.update(status="rejected", reason=str(exc))
                errors.append(key + ":" + str(exc))
            _append(root, "coverage", entry)
    return errors


def append_result(root: Path, competition: str, row: dict, *, observed: datetime,
                  captured: datetime, source_id: str, forecasts: list[dict],
                  collection_method: str = "pipeline_normalized_data") -> str | None:
    """Append a verified regulation result revision; never mutate original evidence."""
    relevant = [r for r in forecasts if r["competition"] == competition and r["match_id"] == row["match_id"]]
    if not relevant:
        raise ValueError("result_identity_or_kickoff_mismatch")
    if observed > captured:
        raise ValueError("result_source_missing_or_future_observation")
    if _timestamp(row["kickoff"], "kickoff") + RESULT_AVAILABILITY_DELAY > observed:
        raise ValueError("result_observed_before_regulation_maturity")
    scores = {field: _integer(row[field], field, 0) for field in ("home_goals", "away_goals")}
    if not any(_same_identity(r, row) for r in relevant):
        record_schedule_revision(root, competition, row, observed=observed, captured=captured,
            source_id=source_id, forecasts=relevant, collection_method=collection_method)
        return None
    revisions = [r for r in _objects(root, "schedule_revisions")
                 if r["competition"] == competition and r["match_id"] == row["match_id"]]
    revision = max(revisions, key=_event_order, default=None)
    if revision and observed <= _timestamp(revision["observed_at"], "observed_at"):
        if collection_method == "pipeline_normalized_data":
            return None
        raise ValueError("result_source_observation_regressed_after_schedule_revision")
    versions = [r for r in _objects(root, "results")
                if r["competition"] == competition and r["match_id"] == row["match_id"]]
    latest = max(versions, key=lambda v: (_timestamp(v["observed_at"], "observed_at"),
        _timestamp(v["captured_at"], "captured_at"), v["object_id"]), default=None)
    if latest:
        previous_observed = _timestamp(latest["observed_at"], "previous observation")
        if observed < previous_observed:
            # An independent later observation supersedes older pipeline data;
            # stale pipeline caches are neither errors nor a new revision.
            if collection_method == "pipeline_normalized_data" and latest.get("collection_method") == "independent_fixture_sync":
                return None
            raise ValueError("result_source_observation_regressed")
        unchanged = _same_identity(latest, row) and all(latest[field] == score for field, score in scores.items())
        evidence_upgrade = (unchanged and latest.get("collection_method") == "pipeline_normalized_data"
                            and collection_method == "independent_fixture_sync")
        if unchanged and not evidence_upgrade and not (revision and _event_order(revision) > _event_order(latest)):
            return None
        if evidence_upgrade:
            # Same goals can still add materially stronger independent evidence.
            # Preserve the old result and require actual immutable raw bytes;
            # changing only the method label cannot upgrade its provenance.
            source, _ = _read(root, "sources", _name(source_id) + ".json")
            envelope = source.get("content", {})
            if (_digest(source) != source_id or source.get("kind") != "independent_fixture_result_response"
                    or source.get("request") != {"fixture_id": row["match_id"]}
                    or source.get("client_response_sha256") != _digest(envelope)
                    or _timestamp(envelope.get("fetched_at"), "upgrade.fetched_at") != observed
                    or envelope.get("errors") or envelope.get("endpoint", "/fixtures") != "/fixtures"
                    or envelope.get("params", {"id": row["match_id"]}) != {"id": row["match_id"]}):
                raise ValueError("independent_result_upgrade_source_mismatch")
            items, paging = envelope.get("response"), envelope.get("paging", {})
            if (not isinstance(items, list) or len(items) != 1 or type(envelope.get("results", 1)) is not int
                    or envelope.get("results", 1) != 1 or not isinstance(paging, dict)
                    or any(type(paging.get(k, 1)) is not int or paging.get(k, 1) != 1 for k in ("current", "total"))):
                raise ValueError("independent_result_upgrade_response_incomplete")
            from .data import normalize_fixtures
            normalized = normalize_fixtures(items, observed)
            if (len(normalized) != 1 or not _same_identity(normalized[0], row)
                    or any(normalized[0][field] != value for field, value in scores.items())):
                raise ValueError("independent_result_upgrade_raw_score_mismatch")
        if observed == previous_observed and not evidence_upgrade:
            raise ValueError("conflicting_result_same_observation_time")
    result = {"schema_version": 1, "competition": competition, **_identity(row), **scores,
        "observed_at": _iso(observed), "captured_at": _iso(captured), "source_id": source_id,
        "score_scope": "normalized_API_Football_score_fulltime_90_plus_stoppage",
        "collection_method": collection_method, "supersedes": latest["object_id"] if latest else None}
    return _append(root, "results", result)


def collect_results(runtime: Path, root: Path, forecasts: list[dict], clock) -> list[str]:
    errors = []
    by_competition = defaultdict(list)
    for forecast in forecasts:
        if not is_retired(forecast["competition"]):
            by_competition[forecast["competition"]].append(forecast)
    for key, records in by_competition.items():
        try:
            data, data_sha = _read(runtime, "data", _name(key) + ".json")
            capture = _now(clock)
            observed = _timestamp(data.get("observed_at"), "result.observed_at")
            if data.get("time_semantics") != TIME_SEMANTICS or observed > capture:
                raise ValueError("result_source_missing_or_future_observation")
            rows = {row["match_id"]: row for row in _validated_matches(data.get("matches"))}
            source_id = _snapshot(root, "normalized_regulation_data", data, data_sha)
        except (ValueError, KeyError, TypeError, OSError) as exc:
            errors.append(key + ":" + str(exc))
            continue
        for mid in sorted({record["match_id"] for record in records}):
            if mid not in rows:
                continue
            try:
                append_result(root, key, rows[mid], observed=observed, captured=capture,
                              source_id=source_id, forecasts=records)
            except (ValueError, TypeError, KeyError) as exc:
                errors.append(key + ":" + str(mid) + ":" + str(exc))
    return errors


def register_quotes(root: Path, inputs: list, clock) -> list[str]:
    if not isinstance(inputs, list):
        raise ValueError("quotes must be a JSON list")
    forecasts = {row["object_id"]: row for row in _objects(root, "forecasts")}
    protocols = {row["object_id"]: row for row in _objects(root, "protocols")}
    prior = {_digest(row["input"]): row for row in _objects(root, "quotes")}
    sequence = max((row.get("quote_sequence", 0) for row in prior.values()), default=0)
    errors = []
    for value in inputs:
        capture = _now(clock)
        sequence += 1
        record = {"input": value, "quote_sequence": sequence, "captured_at": _iso(capture), "status": "not_available"}
        if _digest(value) in prior:
            continue
        try:
            forecast = forecasts[value["forecast_id"]]
            protocol = protocols[forecast["protocol_id"]]
            line = contract(value["contract"])
            if value.get("competition") != forecast["competition"] or not _same_identity(value, forecast):
                raise ValueError("quote_identity_mismatch")
            observed = _timestamp(value.get("observed_at"), "quote.observed_at")
            quoted = _timestamp(value.get("quoted_at"), "quote.quoted_at")
            forecast_time = _timestamp(forecast["captured_at"], "forecast capture")
            if not (quoted <= observed <= capture < _timestamp(forecast["kickoff"], "kickoff")):
                raise ValueError("quote_not_captured_before_kickoff_or_future_timestamp")
            if (capture - quoted).total_seconds() > protocol["quote_max_age_seconds"]:
                raise ValueError("quote_stale_at_actual_registration")
            if not 0 <= (capture - forecast_time).total_seconds() <= protocol["quote_max_forecast_age_seconds"]:
                raise ValueError("forecast_not_current_at_quote_registration")
            if value.get("price_status") != "open" or value.get("conditions_status") != "satisfied":
                raise ValueError("quote_not_open_or_conditions_unresolved")
            if not isinstance(value.get("source"), str) or not value["source"].strip():
                raise ValueError("quote_source_required")
            if not isinstance(value.get("bookmaker"), str) or not value["bookmaker"].strip():
                raise ValueError("bookmaker_required")
            unit_return("push", value.get("decimal_odds"), value.get("cost_per_unit"))
            record.update(status="eligible_simulation_only", forecast_id=value["forecast_id"],
                protocol_id=forecast["protocol_id"], competition=forecast["competition"],
                match_id=forecast["match_id"], contract=line,
                probabilities=probabilities(forecast["forecast"]["prediction"]["score_matrix"], line))
        except (ValueError, KeyError, TypeError) as exc:
            record["reason"] = str(exc)
            errors.append("quote:" + str(exc))
        _append(root, "quotes", record)
        prior[_digest(value)] = record
    return errors


def _markets(rows: list[tuple[dict, dict]], forecast_key: str, contracts: list) -> dict:
    x_probs, x_outcomes = [], []
    markets = defaultdict(lambda: {"probabilities": [], "outcomes": [], "support": None, "contract": None})
    for forecast, result in rows:
        prediction = (forecast["forecast"]["prediction"] if forecast_key == "model"
                      else forecast["baseline"]["prediction"])
        x_probs.append([prediction["probabilities"][outcome] for outcome in OUTCOMES])
        h, a = result["home_goals"], result["away_goals"]
        x_outcomes.append(0 if h > a else (1 if h == a else 2))
        for line in contracts:
            summary = probabilities(prediction["score_matrix"], line)
            support = summary["possible_states"]
            entry = markets[_digest(line)]
            entry["support"], entry["contract"] = support, line
            entry["probabilities"].append([summary["probabilities"][state] for state in support])
            entry["outcomes"].append(support.index(settlement(h, a, line)))
    return {"unique_matches": len(rows), "one_x_two": _categorical_metrics(x_probs, x_outcomes, OUTCOMES),
            "contracts": [{"contract": entry["contract"], "possible_states": entry["support"],
                "structural_zero_states": [s for s in STATES if s not in entry["support"]],
                "metrics": _categorical_metrics(entry["probabilities"], entry["outcomes"], tuple(entry["support"]))}
                for _, entry in sorted(markets.items())]}


def summarize(root: Path, clock) -> dict:
    forecasts, results, quotes = (_objects(root, name) for name in ("forecasts", "results", "quotes"))
    schedule_revisions = _objects(root, "schedule_revisions")
    revision_map = {}
    for revision in sorted(schedule_revisions, key=_event_order):
        revision_map[(revision["competition"], revision["match_id"])] = revision
    result_map = {}
    for result in sorted(current_results(root), key=_event_order):
        result_map[(result["competition"], result["match_id"])] = result
    primary = {}
    changed_identity = []
    ordered = sorted(forecasts, key=lambda r: (_timestamp(r["captured_at"], "captured_at"),
                                              r.get("capture_sequence", 0), r["object_id"]))
    for forecast in ordered:
        key = (forecast["protocol_id"], forecast["competition"], forecast["match_id"])
        result = result_map.get((forecast["competition"], forecast["match_id"]))
        if result and not _same_identity(forecast, result):
            changed_identity.append({"forecast_id": forecast["object_id"], "competition": forecast["competition"],
                "match_id": forecast["match_id"], "status": "result_identity_changed_unscorable", "result_id": result["object_id"]})
            continue
        primary.setdefault(key, forecast)
    # If no matching final identity exists, preserve the earliest pending/conflict
    # row in coverage instead of making the entire match disappear.
    for forecast in ordered:
        primary.setdefault((forecast["protocol_id"], forecast["competition"], forecast["match_id"]), forecast)
    protocols = {p["object_id"]: p for p in _objects(root, "protocols")}
    groups = defaultdict(list)
    for (pid, competition, _), forecast in primary.items():
        groups[(pid, competition)].append(forecast)
    summaries, statuses = [], []
    for (pid, competition), rows in sorted(groups.items()):
        scored = [(row, result_map[(competition, row["match_id"])]) for row in rows
                  if (competition, row["match_id"]) in result_map and _same_identity(row, result_map[(competition, row["match_id"])])]
        common = [(row, result) for row, result in scored if row["baseline"]["status"] == "available"]
        for row in rows:
            result = result_map.get((competition, row["match_id"]))
            revision = revision_map.get((competition, row["match_id"]))
            statuses.append({"protocol_id": pid, "competition": competition, "match_id": row["match_id"],
                "primary_forecast_id": row["object_id"], "status": ("scored" if _same_identity(row, result) else "result_identity_conflict") if result else ("unscorable_schedule_revision" if revision else "pending"),
                "result_id": result["object_id"] if result and _same_identity(row, result) else None,
                "schedule_revision_id": revision["object_id"] if revision else None})
        unscorable = sum((competition, row["match_id"]) in revision_map
                         and (competition, row["match_id"]) not in result_map for row in rows)
        summaries.append({"protocol_id": pid, "competition": competition, "unique_matches": len(rows),
            "pending": len(rows) - len(scored) - unscorable, "scored": len(scored),
            "unscorable_schedule_revision": unscorable,
            "all_model_coverage": _markets(scored, "model", protocols[pid]["contracts"]),
            "common_sample": {"n": len(common), "model": _markets(common, "model", protocols[pid]["contracts"]),
                              "league_mean": _markets(common, "baseline", protocols[pid]["contracts"])},
            "baseline_missing": len(scored) - len(common)})
    selected_quotes = {}
    forecast_map = {row["object_id"]: row for row in forecasts}
    for quote in sorted(quotes, key=lambda r: (_timestamp(r["captured_at"], "captured_at"), r.get("quote_sequence", 0), r["object_id"])):
        if quote["status"] == "eligible_simulation_only":
            selected_quotes.setdefault((quote["protocol_id"], quote["competition"], quote["match_id"], _digest(quote["contract"])), quote)
    returns = []
    for key, quote in selected_quotes.items():
        result = result_map.get((quote["competition"], quote["match_id"]))
        if result is None or not _same_identity(result, forecast_map[quote["forecast_id"]]):
            continue
        state = settlement(result["home_goals"], result["away_goals"], quote["contract"])
        value = quote["input"]
        profit = unit_return(state, value["decimal_odds"], value["cost_per_unit"])
        expected = sum(p * unit_return(s, value["decimal_odds"], value["cost_per_unit"])
                       for s, p in quote["probabilities"]["probabilities"].items())
        returns.append({"quote_id": quote["object_id"], "protocol_id": key[0], "competition": key[1],
                        "match_id": key[2], "result_id": result["object_id"], "settlement": state,
                        "stake": 1, "net_return": profit, "frozen_model_ev": expected,
                        "contract": quote["contract"], "decimal_odds": value["decimal_odds"]})
    quote_groups = defaultdict(list)
    for row in returns:
        quote_groups[(row["protocol_id"], row["competition"], _digest(row["contract"]))].append(row)
    quote_scores = []
    for (pid, competition, _), group in sorted(quote_groups.items()):
        distributions, actuals = [], []
        for row in group:
            quote = next(q for q in selected_quotes.values() if q["object_id"] == row["quote_id"])
            support = quote["probabilities"]["possible_states"]
            distributions.append([quote["probabilities"]["probabilities"][s] for s in support])
            actuals.append(support.index(row["settlement"]))
        quote_scores.append({"protocol_id": pid, "competition": competition, "contract": group[0]["contract"],
            "metrics": _categorical_metrics(distributions, actuals, tuple(support)),
            "possible_states": support, "structural_zero_states": [s for s in STATES if s not in support],
            "unit_stakes": len(group), "unique_matches": len({r["match_id"] for r in group}),
            "net_return": sum(r["net_return"] for r in group)})
    coverage = _objects(root, "coverage")
    return {"version": VERSION, "generated_at": _iso(_now(clock)), "forecast_versions": len(forecasts),
            "unique_primary_matches_by_protocol": len(primary), "coverage_records": len(coverage),
            "coverage_status_counts": dict(Counter(row["status"] for row in coverage)),
            "result_versions": len(results), "groups": summaries, "match_statuses": statuses,
            "schedule_revision_versions": len(schedule_revisions),
            "unscorable_schedule_revision_matches": sum(s["status"] == "unscorable_schedule_revision" for s in statuses),
            "identity_changed_forecasts": changed_identity,
            "quote_simulation": {"status": "available" if returns else "not_available",
                "eligible_quotes": len(selected_quotes), "scored_quotes": len(returns),
                "pending_quotes": len(selected_quotes) - len(returns), "unit_stakes": len(returns),
                "unique_match_protocols": len({(r["protocol_id"], r["competition"], r["match_id"]) for r in returns}),
                "contract_metrics": quote_scores, "settlements": returns,
                "not_actual_bets": True, "multiple_contracts_are_correlated": True},
            "quality_claim": "descriptive_scores_only_no_automatic_accuracy_or_profit_acceptance",
            "limitations": ["Inputs rely on repository provider normalization; four-hour delay alone does not prove final status.",
                "No historical captures are promoted to clean prospective samples.",
                "Latest valid result revisions change derived scores but never overwrite frozen forecasts or previous result records.",
                "Quotes require real pre-kickoff registration; no automatic price collection or transaction proof."]}


def _run_locked(runtime: Path, config: dict | None = None, *, quotes: list | None = None, clock=None) -> tuple[int, dict]:
    runtime = Path(runtime)
    if (_path(runtime, "RESEARCH_REPLAY.json").exists() or
            (runtime.parent / "RESEARCH_REPLAY.json").exists()):
        raise ValueError("research replay runtime cannot enter prospective ledger")
    root = _path(runtime, "prospective")
    root.mkdir(parents=True, exist_ok=True)
    clock = clock or (lambda: datetime.now(timezone.utc))
    protocol = make_protocol(config or {})
    protocol_id = _append(root, "protocols", protocol)
    errors = []
    report, report_sha = _read(runtime, "reports", "latest.json")
    errors.extend(capture_current(runtime, root, report, report_sha, protocol_id, clock))
    forecasts = _objects(root, "forecasts")
    errors.extend(collect_results(runtime, root, forecasts, clock))
    if quotes is not None:
        errors.extend(register_quotes(root, quotes, clock))
    summary = summarize(root, clock)
    summary.update(current_run_id=report["run_id"], current_protocol_id=protocol_id, errors=errors,
                   exit_code=1 if errors else 0)
    _append(root, "runs", summary)
    latest = _path(root, "latest.json")
    temporary = _path(root, "latest-" + _digest(summary) + ".tmp")
    try:
        temporary.write_bytes(_json_bytes(summary))
        os.replace(temporary, latest)
    finally:
        temporary.unlink(missing_ok=True)
    return summary["exit_code"], summary



@contextmanager
def ledger_writer(runtime: Path):
    """All independent ledger writers share one nonblocking OS lock."""
    runtime = Path(runtime)
    if (_path(runtime, "RESEARCH_REPLAY.json").exists() or
            (runtime.parent / "RESEARCH_REPLAY.json").exists()):
        raise ValueError("research replay runtime cannot enter prospective ledger")
    root = _path(runtime, "prospective")
    root.mkdir(parents=True, exist_ok=True)
    with _path(root, "writer-lock.txt").open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("prospective ledger writer already active") from exc
        yield root


def capture_report(runtime: Path, config: dict, report: dict, *, clock=None) -> dict:
    """Freeze one completed competition immediately using its actual checkpoint.

    The final run report is linked later by run_id; this immutable partial report
    does not claim that later competitions or state persistence have succeeded.
    """
    runtime = Path(runtime)
    clock = clock or (lambda: datetime.now(timezone.utc))
    if report.get("research_replay"):
        raise ValueError("research replay report cannot enter prospective ledger")
    with ledger_writer(runtime) as root:
        protocol_id = _append(root, "protocols", make_protocol(config))
        report_bytes = _json_bytes(report)
        checkpoint_id = _append(root, "capture_checkpoints", report)
        errors = capture_current(runtime, root, report, hashlib.sha256(report_bytes).hexdigest(), protocol_id, clock)
        coverage = [r for r in _objects(root, "coverage") if r["run_id"] == report["run_id"]
                    and r["competition"] in report["requested_competitions"]]
        summary = {"run_id": report["run_id"], "checkpoint_id": checkpoint_id,
            "protocol_id": protocol_id, "completed_at": _iso(_now(clock)),
            "competitions": report["requested_competitions"],
            "coverage_status_counts": dict(Counter(r["status"] for r in coverage)),
            "errors": errors, "exit_code": int(bool(errors))}
        summary["capture_run_id"] = _append(root, "capture_runs", summary)
        return summary


def run(runtime: Path, config: dict | None = None, *, quotes: list | None = None, clock=None) -> tuple[int, dict]:
    """Serialize ledger writers; a second invocation fails without changing inputs."""
    with ledger_writer(Path(runtime)):
        return _run_locked(runtime, config, quotes=quotes, clock=clock)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Freeze current real forecasts and score immutable prospective records")
    parser.add_argument("--runtime", type=Path, default=Path("runtime"))
    parser.add_argument("--config", type=Path)
    parser.add_argument("--quotes", type=Path, help="JSON list of actual pre-kickoff observed quotes; never historical backfill")
    args = parser.parse_args(argv)
    try:
        config = json.loads(args.config.read_text()) if args.config else {}
        quotes = json.loads(args.quotes.read_text()) if args.quotes else None
        code, result = run(args.runtime, config, quotes=quotes)
        print(json.dumps({"status": "completed" if code == 0 else "partial", "exit_code": code,
                          "run_id": result["current_run_id"], "forecast_versions": result["forecast_versions"],
                          "result_versions": result["result_versions"], "errors": result["errors"]}, ensure_ascii=False))
        return code
    except (ValueError, KeyError, TypeError, OSError) as exc:
        # This process does not modify model/prediction files on any failure path.
        print(json.dumps({"status": "failed", "exit_code": 2, "reason": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
