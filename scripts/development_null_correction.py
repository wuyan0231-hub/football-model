#!/usr/bin/env python3
"""Independent exploratory null-correction experiment; never changes v1 or M0.

Only this new entry point imports the existing numerical functions. No old
module imports it. Exact local compact JSON objects are read without networking.
"""
from __future__ import annotations

import argparse
import copy
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import platform
import statistics
import sys

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
from football_model.feature_correction import _training_rows, apply_correction, fit_correction, predict_correction
from football_model.feature_research import choose_alpha, score_forecast
from football_model.model import ModelDataError, _iso, _timestamp


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
        separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def reserve_output(output, protocol_hash, run_identity, entrypoint_raw, protocol_raw, identity):
    out = Path(output).resolve() / protocol_hash / run_identity
    out.parent.mkdir(parents=True, exist_ok=True)
    out.mkdir()  # Atomic, exclusive reservation, including a previous partial run.
    with (out / "entrypoint.py").open("xb") as handle:
        handle.write(entrypoint_raw)
    with (out / "protocol.json").open("xb") as handle:
        handle.write(protocol_raw)
    with (out / "run-started.json").open("x") as handle:
        json.dump({"run_identity": run_identity, "identity_inputs": identity,
            "started_at": datetime.now(timezone.utc).isoformat()}, handle, indent=2)
    return out


class OfflineObjects:
    """Read already materialized exact refs; missing data never triggers fetch."""
    def __init__(self, runtime):
        self.runtime = Path(runtime).resolve()
        self.refs, self.cache, self.active = {}, {}, set()

    def raw(self, ref):
        relative = ref.get("runtime_relative_path", ref["path"])
        path = (self.runtime / relative).resolve()
        if Path(relative).is_absolute() or ".." in Path(relative).parts or not path.is_relative_to(self.runtime):
            raise ValueError("source path escapes runtime")
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != ref["sha256"]:
            raise ValueError("source fingerprint mismatch")
        self.refs[relative] = {"path": relative, "sha256": ref["sha256"], "bytes": len(raw)}
        return json.loads(raw)

    def root(self, ref):
        value = self.raw(ref)
        while isinstance(value, dict) and set(value) == {"$advanced_object_ref"}:
            value = self.raw(value["$advanced_object_ref"])
        return value

    def load(self, ref):
        key = (ref["path"], ref["sha256"])
        if key in self.active:
            raise ValueError("cyclic source object")
        if key not in self.cache:
            self.active.add(key)
            self.cache[key] = self.unpack(self.raw(ref))
            self.active.remove(key)
        return self.cache[key]

    def unpack(self, value):
        if isinstance(value, list):
            return [self.unpack(v) for v in value]
        if not isinstance(value, dict):
            return value
        if "$advanced_object_ref" in value:
            if set(value) != {"$advanced_object_ref"}:
                raise ValueError("ambiguous object wrapper")
            return self.load(value["$advanced_object_ref"])
        if "$advanced_list_chunks" in value:
            if set(value) != {"$advanced_list_chunks", "length"}:
                raise ValueError("ambiguous list wrapper")
            result = []
            for ref in value["$advanced_list_chunks"]:
                part = self.load(ref)
                if not isinstance(part, list):
                    raise ValueError("chunk must contain list")
                result.extend(part)
            if len(result) != value["length"]:
                raise ValueError("source list length mismatch")
            return result
        if "$advanced_dict_items" in value:
            if set(value) != {"$advanced_dict_items"}:
                raise ValueError("ambiguous dict wrapper")
            pairs = self.unpack(value["$advanced_dict_items"])
            result = dict(pairs)
            if len(result) != len(pairs):
                raise ValueError("duplicate dict keys")
            return result
        return {k: self.unpack(v) for k, v in value.items()}


def select_with_null(rows, protocol, cutoff):
    """Select only inside mature past rows; null is exact M0, never alpha=0."""
    names = protocol["feature_names"]
    records = _training_rows(rows, names, _timestamp(cutoff, "cutoff")) if rows else []
    grouped = {}
    for row in records:
        grouped.setdefault(row["match_id"], []).append(row)
    pairs = sorted(grouped.values(), key=lambda p: (p[0]["kickoff"], p[0]["match_id"]))
    minimum = 2 * protocol["correction_min_matches"]
    info = {"outer_cutoff": cutoff, "past_match_ids": [p[0]["match_id"] for p in pairs], "trials": []}
    split = int(len(pairs) * (1 - protocol["inner_validation_fraction"]))
    validation = pairs[split:]
    if len(records) < minimum or not validation:
        return {**info, "mode": "null", "reason": "insufficient_past_or_validation_history"}
    inner_cutoff = min(r["cutoff_at"] for pair in validation for r in pair)
    training = [r for pair in pairs[:split] for r in pair
                if _timestamp(r["target_available_at"], "available") <= _timestamp(inner_cutoff, "inner cutoff")]
    info.update(inner_cutoff=inner_cutoff,
        inner_training_match_ids=sorted({r["match_id"] for r in training}),
        inner_validation_match_ids=[p[0]["match_id"] for p in validation])
    if len(training) < minimum:
        return {**info, "mode": "null", "reason": "insufficient_inner_training_history"}
    null_losses = [r["lambda0"] - r["goals"] * math.log(r["lambda0"]) + math.lgamma(r["goals"] + 1)
                   for pair in validation for r in pair]
    null_score = statistics.mean(null_losses)
    info["trials"].append({"mode": "null", "score": null_score, "status": "scored"})
    for alpha in protocol["correction_alpha_grid"]:
        try:
            model = fit_correction(training, names, alpha=alpha, cutoff=inner_cutoff,
                min_rows=minimum, half_life_days=protocol["baseline_half_life_days"])
            losses = []
            for pair in validation:
                for row in pair:
                    rate = apply_correction(model, row["lambda0"], row["features"])["lambda"]
                    losses.append(rate - row["goals"] * math.log(rate) + math.lgamma(row["goals"] + 1))
            info["trials"].append({"mode": "correction", "alpha": alpha,
                "score": statistics.mean(losses), "status": "scored"})
        except (ValueError, ArithmeticError) as exc:
            info["trials"].append({"mode": "correction", "alpha": alpha,
                "status": "failed", "error_class": type(exc).__name__, "detail": str(exc)})
    successful = [t for t in info["trials"] if t["mode"] == "correction" and t["status"] == "scored"]
    best = min(successful, key=lambda t: (t["score"], -t["alpha"])) if successful else None
    if best is None or best["score"] >= null_score - protocol["inner_tie_tolerance"]:
        return {**info, "mode": "null", "reason": "null_best_or_within_tie_tolerance"}
    return {**info, "mode": "correction", "alpha": best["alpha"], "reason": "past_inner_score_improved"}


def fit_arm(rows, protocol, cutoff, arm):
    past = [r for r in rows if _timestamp(r["target_available_at"], "available") <= _timestamp(cutoff, "cutoff")]
    selection, stage = None, "inner_selection"
    try:
        if arm == "v2_null_allowed":
            selection = select_with_null(past, protocol, cutoff)
            if selection["mode"] == "null":
                return {"status": "null", "selection": selection, "model": None}
            alpha = selection["alpha"]
        elif arm == "existing_forced_nonzero":
            alpha, selection = choose_alpha(past, protocol, cutoff)
        else:
            raise ValueError("unknown arm")
        stage = "outer_refit"
        model = fit_correction(past, protocol["feature_names"], alpha=alpha, cutoff=cutoff,
            min_rows=2 * protocol["correction_min_matches"], half_life_days=protocol["baseline_half_life_days"])
        return {"status": "fitted", "selection": selection, "model": model}
    except (ValueError, ArithmeticError) as exc:
        return {"status": "failed", "model": None, "selection": selection,
            "failed_stage": stage, "error_class": type(exc).__name__, "detail": str(exc)}


def evaluate(rows, requested, protocol):
    """All selections precede reading each outer target's score for evaluation."""
    pairs = {}
    for row in rows:
        pairs.setdefault(row["match_id"], {})[row["side"]] = row
    blocks = {}
    for fixture in requested:
        blocks.setdefault(fixture["baseline_training_cutoff"], []).append(fixture)
    results, experiments = [], []
    for cutoff, targets in sorted(blocks.items()):
        arms = {arm: fit_arm(rows, protocol, cutoff, arm)
                for arm in ("existing_forced_nonzero", "v2_null_allowed")}
        experiments.append({"cutoff": cutoff, "requested_target_ids": [f["match_id"] for f in targets], "arms": arms})
        for fixture in targets:
            baseline = copy.deepcopy(fixture.get("baseline_prediction"))
            row = {"match_id": fixture["match_id"], "kickoff": fixture["kickoff"],
                "cutoff_at": fixture["cutoff_at"], "outer_training_cutoff": cutoff,
                "scores": {}, "status": {}, "predictions": {}}
            if baseline is None:
                row["baseline_failure"] = {"code": fixture.get("code"), "reason": fixture.get("reason")}
                row["status"] = {arm: "unforecast_no_M0" for arm in arms}
                results.append(row)
                continue
            baseline["cutoff_at"] = fixture["cutoff_at"]
            row["scores"]["M0"] = score_forecast(baseline, fixture, extended_diagnostics=True)
            for arm, fit in arms.items():
                prediction = baseline
                if fit["status"] == "null":
                    row["status"][arm] = "null_unchanged_M0"
                elif fit["status"] == "failed":
                    row["status"][arm] = "unchanged_M0_fit_failure_fallback"
                else:
                    try:
                        pair = pairs[fixture["match_id"]]
                        prediction = predict_correction(fit["model"], baseline, pair["home"]["features"], pair["away"]["features"])
                        row["status"][arm] = "nonzero_correction_predicted"
                    except (ValueError, ArithmeticError, KeyError) as exc:
                        row["status"][arm] = "unchanged_M0_prediction_failure_fallback"
                        row.setdefault("failures", {})[arm] = {"error_class": type(exc).__name__, "detail": str(exc)}
                row["predictions"][arm] = prediction
                row["scores"][arm] = score_forecast(prediction, fixture, extended_diagnostics=True)
            results.append(row)
    return results, experiments


def prepare_from_saved_report(reader, report):
    """M_schedule projection exactly matches the current frozen four-column arm."""
    horizon = "EARLY"
    replays = reader.root(report["replays"]["$advanced_object_ref"])
    raw = reader.root(replays[horizon]["$advanced_object_ref"])
    forecasts = reader.unpack(raw["forecasts"])
    failures = reader.unpack(raw["failures"])
    fits = reader.unpack(raw["baseline_fits"])
    by_block = {f["block"]: f for f in fits}
    names = report["protocol"]["candidates"]["M_schedule"]["groups"]
    if names != ["schedule"]:
        raise ValueError("source M_schedule no longer has exactly the schedule feature family")
    rows = []
    for f in forecasts:
        if not all(all(type(f[side + "_features"].get(n)) in (int, float)
                   and math.isfinite(f[side + "_features"][n]) for n in
                   ("rest_days_capped", "matches_last14d", "opponent_rest_days_capped", "opponent_matches_last14d"))
                   for side in ("home", "away")):
            # Match the original _correction_rows admission: both sides or neither.
            continue
        for side in ("home", "away"):
            values = f[side + "_features"]
            rows.append({"match_id": f["match_id"], "side": side,
                "kickoff": f["kickoff"], "cutoff_at": f["cutoff_at"], "target_available_at": f["target_available_at"],
                "baseline_training_cutoff": f["baseline_training_cutoff"],
                "baseline_training_match_ids": by_block[f["block"]]["training_match_ids"],
                "baseline_training_data_sha256": digest(by_block[f["block"]]),
                "goals": f[side + "_goals"], "lambda0": f["baseline_prediction"]["expected_goals"][side],
                "features": values, "feature_available_at": {n: f["cutoff_at"] for n in values},
                "time_semantics": "historical_feature_availability_assumption_not_live_PIT"})
    return rows, sorted(forecasts + failures, key=lambda f: (f["kickoff"], f["match_id"]))


def summarize(results):
    from collections import Counter
    summaries = {}
    for arm in ("existing_forced_nonzero", "v2_null_allowed"):
        scored = [r for r in results if arm in r["scores"]]
        summaries[arm] = {"requested_matches": len(results), "policy_scored_matches": len(scored),
            "status_counts": dict(Counter(r["status"][arm] for r in results)),
            "null_fraction_of_M0_available_matches": sum(r["status"][arm] == "null_unchanged_M0" for r in scored) / len(scored) if scored else None,
            "metrics": {k: statistics.mean(r["scores"][arm][k] for r in scored) for k in ("scoreline_nll", "one_x_two_nll", "one_x_two_brier", "total_goal_nll", "goal_difference_nll")} if scored else {},
            "scoreline_difference_from_same_match_M0": statistics.mean(r["scores"][arm]["scoreline_nll"] - r["scores"]["M0"]["scoreline_nll"] for r in scored) if scored else None}
    paired = [r for r in results if all(a in r["scores"] for a in summaries)]
    return {"arms": summaries, "same_match_count": len(paired),
        "v2_minus_existing_policy_scoreline_nll": statistics.mean(r["scores"]["v2_null_allowed"]["scoreline_nll"] - r["scores"]["existing_forced_nonzero"]["scoreline_nll"] for r in paired) if paired else None}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--report-summary", type=Path, required=True)
    parser.add_argument("--protocol", type=Path, default=REPO / "development-null-correction-v2.json")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    protocol_raw = args.protocol.read_bytes()
    protocol = json.loads(protocol_raw)
    summary_raw = args.report_summary.read_bytes()
    if hashlib.sha256(summary_raw).hexdigest() != protocol["source_report_summary_sha256"]:
        raise ValueError("source summary identity changed")
    saved = json.loads(summary_raw)
    if saved["full_report_ref"]["sha256"] != protocol["source_report_sha256"]:
        raise ValueError("source report identity changed")
    reader = OfflineObjects(args.runtime)
    report = reader.root(saved["full_report_ref"])
    before = report["implementation_fingerprint"]
    if before["sha256"] != protocol["expected_existing_implementation_sha256"]:
        raise ValueError("source implementation differs from protocol")
    for name, expected in before["files"].items():
        if hashlib.sha256((REPO / name).read_bytes()).hexdigest() != expected:
            raise ValueError("existing numerical closure changed: " + name)
    entrypoint_raw = Path(__file__).read_bytes()
    implementation = {"schema_version": "independent-null-runner-implementation-v1",
        "entrypoint_path": "scripts/development_null_correction.py",
        "entrypoint_sha256": hashlib.sha256(entrypoint_raw).hexdigest(),
        "existing_numerical_implementation_sha256": before["sha256"]}
    implementation["sha256"] = digest(implementation)
    identity = {"protocol_hash": digest(protocol), "runner_implementation_sha256": implementation["sha256"],
        "source_summary_sha256": hashlib.sha256(summary_raw).hexdigest(),
        "source_report_sha256": saved["full_report_ref"]["sha256"]}
    run_identity = digest(identity)
    out = reserve_output(args.output, digest(protocol), run_identity, entrypoint_raw,
        protocol_raw, identity)
    rows, requested = prepare_from_saved_report(reader, report)
    results, experiments = evaluate(rows, requested, protocol)
    # Verify the reproduced existing arm against the saved policy on every M0 row.
    original_rows = reader.unpack(report["scored_matches"])
    original = {r["match_id"]: r for r in original_rows if r["horizon"] == "EARLY"}
    max_old_delta = max(abs(r["scores"]["existing_forced_nonzero"][k] - original[r["match_id"]]["scores"]["M_schedule_policy"][k])
        for r in results if "M0" in r["scores"] for k in ("scoreline_nll", "one_x_two_nll", "one_x_two_brier", "total_goal_nll", "goal_difference_nll"))
    if max_old_delta > 1e-10:
        raise ValueError("existing comparator does not reproduce saved original policy")
    for name, expected in before["files"].items():
        if hashlib.sha256((REPO / name).read_bytes()).hexdigest() != expected:
            raise ValueError("existing numerical closure mutated during experiment")
    if Path(__file__).read_bytes() != entrypoint_raw or args.protocol.read_bytes() != protocol_raw:
        raise ValueError("independent entrypoint or protocol mutated during experiment")
    import numpy, scipy, statsmodels
    result = {"schema_version": "exploratory-null-correction-development-result-v2",
        "generated_at": datetime.now(timezone.utc).isoformat(), "protocol": protocol,
        "protocol_hash": digest(protocol), "source_implementation": before,
        "protocol_raw_sha256": hashlib.sha256(protocol_raw).hexdigest(),
        "runner_implementation": implementation, "run_identity": run_identity,
        "numeric_environment": {"python": platform.python_version(), "numpy": numpy.__version__,
            "scipy": scipy.__version__, "statsmodels": statsmodels.__version__},
        "existing_50_file_closure_unchanged": True,
        "source_rows": len(rows), "source_unique_training_matches": len({r["match_id"] for r in rows}),
        "target_availability_delay_hours": sorted({(_timestamp(r["target_available_at"], "available") - _timestamp(r["kickoff"], "kickoff")).total_seconds() / 3600 for r in rows}),
        "original_comparator_max_five_metric_difference": max_old_delta,
        "summary": summarize(results), "experiments": experiments, "outer_results": results,
        "source_refs": list(reader.refs.values()),
        "source_scope": "consumed_compact_objects_only;not_complete_parent_evidence_closure",
        "source_state_commit": protocol["source_state_commit"],
        "source_state_commit_basis": "caller_verified_fixed_state_access_manifest;all_consumed_bytes_hash_checked_locally",
        "outer_block_count": len(experiments),
        "v2_null_block_count": sum(e["arms"]["v2_null_allowed"]["status"] == "null" for e in experiments),
        "confirmatory": False,
        "EPL_results_seen_before_design": True, "production_changed": False, "existing_380_family_changed": False,
        "new_API_requests": 0, "workflow_dispatches": 0, "predictive_improvement_confirmed": False}
    path = out / "result.json"
    with path.open("x") as handle:
        handle.write(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    with (out / "summary.json").open("x") as handle:
        handle.write(json.dumps({k: v for k, v in result.items() if k not in ("experiments", "outer_results")}, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"result": str(path), "protocol_hash": digest(protocol), "summary": result["summary"], "original_comparator_max_difference": max_old_delta}, ensure_ascii=False))
    return result


if __name__ == "__main__":
    main()
