#!/usr/bin/env python3
"""Validate restored immutable shadow forecasts without fitting or changing state.

The only write is the requested diagnostic JSON outside runtime. No forecast,
score report, training input, network response or state branch is created here.
Only hashes, field labels and numeric differences are exposed in diagnostics;
saved names, free text and exception messages are deliberately not emitted.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import numpy as np

from football_model.model import _iso, _timestamp, score_distribution
from football_model.prospective import _digest, _name, _path, _read
from football_model.shadow_models import (
    NUMERIC_REPLAY_MATRIX_ABS_TOL, _numerical_environment,
    _numeric_replay_equivalent, _saved_forecast_valid,
)

DETAIL_LIMIT = 20
STDOUT_LIMIT_BYTES = 20_000
_SAFE_FIELDS = frozenset({
    "expected_goals", "home", "away", "probabilities", "home_win", "draw", "away_win",
    "score_matrix", "score_matrix_axes", "rows", "columns", "tail_mass_before_normalization",
    "tail_tolerance", "matrix_renormalized", "home_id", "away_id", "home_name", "away_name",
    "kickoff", "neutral", "model_version", "model_scope", "training_cutoff",
    "training_data_sha256", "lineup_adjusted", "team_training_matches", "low_sample",
    "model_age_days", "warnings",
})
_SAFE_ERRORS = frozenset({
    "immutable_forecast_hash_mismatch", "forecast_not_an_object", "runtime_no_saved_forecasts",
    "state_marker_invalid", "state_marker_source_mismatch", "shadow_prediction_does_not_recompute",
    "not_a_live_research_shadow", "shadow_durable_payload_binding_mismatch",
    "shadow_storage_completion_time_invalid", "shadow_actual_time_order_invalid",
    "shadow_parent_hash_mismatch", "shadow_model_input_binding_mismatch",
    "shadow_protocol_registered_after_cutoff", "shadow_input_source_hash_or_kind_mismatch",
    "shadow_config_source_hash_mismatch", "shadow_registered_fit_parameters_mismatch",
    "shadow_training_source_after_cutoff", "shadow_training_cutoff_or_minimum_mismatch",
    "shadow_training_input_fingerprint_mismatch", "shadow_schedule_identity_mismatch",
    "shadow_national_registry_source_mismatch", "shadow_prediction_identity_mismatch",
    "shadow_neutral_does_not_match_source",
})


def _sha(value, length=64):
    return value if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{%d}" % length, value) else None


def _error(exc):
    message = str(exc)
    if message in _SAFE_ERRORS:
        return {"error": message, "error_type": type(exc).__name__}
    if message.startswith("shadow_common_provenance_mismatch:"):
        # This suffix is from a fixed source-code tuple, never an input value.
        suffix = message.partition(":")[2]
        if suffix in {"competition", "input_cutoff", "parents", "source_commit", "state_commit",
                      "config_source", "implementation_sha256"}:
            return {"error": message, "error_type": type(exc).__name__}
    return {"error": "validation_exception", "error_type": type(exc).__name__,
            "error_message_sha256": hashlib.sha256(message.encode()).hexdigest()}


def _field_label(key):
    if key in _SAFE_FIELDS:
        return key
    return "unknown_field_sha256_" + hashlib.sha256(str(key).encode()).hexdigest()[:16]


def _nonmatrix_differences(recomputed, saved):
    """Describe differing leaves without copying any string or container values."""
    differences = []
    remaining = [10_000]

    def walk(left, right, path, depth=0):
        remaining[0] -= 1
        if remaining[0] < 0 or depth > 32:
            raise ValueError("diagnostic_structure_budget_exceeded")
        if isinstance(left, dict) and isinstance(right, dict):
            for key in sorted(set(left) | set(right)):
                label = path + [_field_label(key)]
                if key not in left or key not in right:
                    differences.append({"field": ".".join(label), "kind": "missing_field",
                                        "missing_from": "recomputed" if key not in left else "saved"})
                else:
                    walk(left[key], right[key], label, depth + 1)
        elif isinstance(left, list) and isinstance(right, list):
            if len(left) != len(right):
                differences.append({"field": ".".join(path), "kind": "list_length_mismatch",
                                    "recomputed_length": len(left), "saved_length": len(right)})
            for index, (a, b) in enumerate(zip(left, right)):
                walk(a, b, path + [str(index)], depth + 1)
        elif left != right:
            record = {"field": ".".join(path), "kind": "value_mismatch",
                      "recomputed_type": type(left).__name__, "saved_type": type(right).__name__}
            if (type(left) in (int, float) and type(right) in (int, float)
                    and math.isfinite(left) and math.isfinite(right)):
                delta = abs(left - right)
                if math.isfinite(delta):
                    record.update(kind="numeric_mismatch", absolute_difference=delta)
            differences.append(record)

    walk({k: v for k, v in recomputed.items() if k != "score_matrix"},
         {k: v for k, v in saved.items() if k != "score_matrix"}, [])
    return differences


def _replay_details(root, forecast):
    # Called only after the native validator reached its final numerical gate:
    # hash, full parent chain, neutral status and chronology already passed.
    model, _ = _read(root, "models", _name(forecast["model_id"]) + ".json")
    saved = forecast["prediction"]
    recomputed = score_distribution(model["model"], forecast["home_id"], forecast["away_id"],
                                    forecast["kickoff"], saved["neutral"])
    details = {"current_replay_equivalent": _numeric_replay_equivalent(recomputed, saved),
               "nonmatrix_differences": _nonmatrix_differences(recomputed, saved)}
    try:
        a, b = (np.asarray(value["score_matrix"], dtype=float) for value in (recomputed, saved))
        details.update(matrix_shape_recomputed=list(a.shape), matrix_shape_saved=list(b.shape))
        if a.ndim != 2 or b.ndim != 2 or a.shape != b.shape or not a.size:
            details["matrix_comparison"] = "shape_mismatch_or_empty"
        elif not np.all(np.isfinite(a)) or not np.all(np.isfinite(b)):
            details["matrix_comparison"] = "nonfinite_values"
        else:
            delta = np.abs(a - b)
            details.update(matrix_comparison="finite_same_shape", matrix_max_abs_diff=float(delta.max()),
                           matrix_cells_differ=int(np.count_nonzero(delta)),
                           matrix_cells_exceed_current_tol=int(np.count_nonzero(delta > NUMERIC_REPLAY_MATRIX_ABS_TOL)))
    except (TypeError, ValueError, OverflowError):
        details["matrix_comparison"] = "matrix_inspection_failed"
    return details


def _marker_context(marker, now):
    if marker is None:
        return {"scope": "local_runtime_scope_not_full_cloud_restore", "state_commit": None}
    value = json.loads(Path(marker).read_text(encoding="utf-8"))
    if (not isinstance(value, dict) or type(value.get("schema_version")) is not int
            or value["schema_version"] != 1 or _sha(value.get("state_oid"), 40) is None
            or _sha(value.get("source_sha"), 40) is None
            or _timestamp(value["loaded_at"], "loaded_at") > now):
        raise ValueError("state_marker_invalid")
    if value["source_sha"] != _sha(os.environ.get("GITHUB_SHA"), 40):
        raise ValueError("state_marker_source_mismatch")
    return {"scope": "restored_state_bound_to_cloud_load_marker", "state_commit": value["state_oid"],
            "loaded_at": value["loaded_at"]}


def verify(runtime, *, state_marker=None, now=None):
    """Read every saved forecast, retaining failures rather than stopping at one."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("aware_clock_required")
    now = now.astimezone(timezone.utc)
    report = {"schema_version": 1, "operation": "offline_saved_shadow_forecast_validation",
              "started_at": _iso(now), "source_commit": _sha(os.environ.get("GITHUB_SHA"), 40),
              "github_run_id": os.environ.get("GITHUB_RUN_ID") if os.environ.get("GITHUB_RUN_ID", "").isdigit() else None,
              "github_run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT") if os.environ.get("GITHUB_RUN_ATTEMPT", "").isdigit() else None,
              "numerical_environment": _numerical_environment(),
              "numeric_replay_matrix_abs_tol": NUMERIC_REPLAY_MATRIX_ABS_TOL,
              "fit_attempts": 0, "api_requests": 0, "state_writes": 0,
              "forecast_count": 0, "passed_count": 0, "failed_count": 0,
              "failure_detail_limit": DETAIL_LIMIT, "failures": [], "failure_counts": {},
              "accuracy_improvement_verified": False, "profit_evaluation": False}
    counts = Counter()
    try:
        report["state"] = _marker_context(state_marker, now)
        runtime = Path(runtime)
        if runtime.is_symlink() or not runtime.is_dir():
            raise ValueError("runtime_invalid")
        root = _path(runtime, "shadow_models")
        paths = sorted(_path(root, "forecasts").glob("*.json"))
        if not paths:
            raise ValueError("runtime_no_saved_forecasts")
        for path in paths:
            report["forecast_count"] += 1
            forecast = None
            try:
                value, _ = _read(root, "forecasts", path.name)
                if not isinstance(value, dict):
                    raise ValueError("forecast_not_an_object")
                if path.stem != _digest(value):
                    raise ValueError("immutable_forecast_hash_mismatch")
                forecast = {**value, "object_id": path.stem}
                _saved_forecast_valid(root, forecast, now)
                report["passed_count"] += 1
            except Exception as exc:
                report["failed_count"] += 1
                record = {"forecast_id": _sha(path.stem), **_error(exc)}
                counts[record["error"]] += 1
                if len(report["failures"]) < DETAIL_LIMIT:
                    if forecast is not None:
                        record.update(model_id=_sha(forecast.get("model_id")),
                                      saved_source_commit=_sha(forecast.get("source_commit"), 40))
                        if forecast.get("arm") in {"baseline180", "candidate90", "candidate365"}:
                            record["arm"] = forecast["arm"]
                    if record["error"] == "shadow_prediction_does_not_recompute":
                        try:
                            record["replay"] = _replay_details(root, forecast)
                        except Exception as replay_exc:
                            record["replay_diagnostic_error"] = _error(replay_exc)
                    report["failures"].append(record)
    except Exception as exc:
        report["setup_failure"] = _error(exc)
        counts[report["setup_failure"]["error"]] += 1
    report["failure_counts"] = dict(sorted(counts.items()))
    report["omitted_failure_details"] = max(0, report["failed_count"] - len(report["failures"]))
    report["status"] = "passed" if not counts else "failed"
    report["completed_at"] = _iso(datetime.now(timezone.utc))
    return report


def compact_stdout(report):
    """Keep the CI log useful even when a malformed object has many differences."""
    result = {k: v for k, v in report.items() if k != "failures"}
    result["failures"] = []
    result["stdout_omitted_failure_details"] = 0
    for failure in report["failures"]:
        result["failures"].append(failure)
        encoded = json.dumps(result, ensure_ascii=True, separators=(",", ":"), allow_nan=False)
        if len(encoded.encode()) >= STDOUT_LIMIT_BYTES - 200:
            result["failures"].pop()
            break
    result["stdout_omitted_failure_details"] = len(report["failures"]) - len(result["failures"])
    return json.dumps(result, ensure_ascii=True, separators=(",", ":"), allow_nan=False)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", type=Path, default=Path("runtime"))
    parser.add_argument("--state-marker", type=Path,
                        help="Exact football-cloud-state.json produced by successful cloud_state load")
    parser.add_argument("--output", type=Path, required=True, help="Diagnostic JSON outside runtime")
    args = parser.parse_args(argv)
    if (args.output.resolve().is_relative_to(args.runtime.resolve())
            or args.runtime.resolve().is_relative_to(args.output.resolve())
            or (args.state_marker is not None and args.output.resolve() == args.state_marker.resolve())):
        parser.error("diagnostic output must be outside runtime")
    # A report must not overwrite an input via a symbolic link.
    cursor = args.output.absolute()
    while cursor != cursor.parent:
        if cursor.is_symlink():
            parser.error("symlinked diagnostic output is not permitted")
        cursor = cursor.parent
    report = verify(args.runtime, state_marker=args.state_marker)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=True, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    print(compact_stdout(report))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
