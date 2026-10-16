#!/usr/bin/env python3
"""Bounded retrospective team-masking experiment; no production/state writes.

Only canonical results admitted before the protocol learning end are projected.
The current exposed thirteen outcomes are not accessed as result records.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
from pathlib import Path
import platform
import sys

import numpy as np

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
from football_model.model import (RESULT_AVAILABILITY_DELAY, ModelDataError,
    _connected_components, _iso, _timestamp, _validated_matches, fit_model)
from football_model.cold_start import score_cold_start
from scripts.cold_start_uncertainty import predict_uncertainty

VERSION = "cold_start_past_masking_development_v1"
EXPECTED_50 = "1e311689b3405f64c6ad9636732d2defb7ad67e26b4bc7e04b01a5b1b2b2d6ac"
SERIAL_FIELDS = ("match_id", "kickoff", "home_id", "away_id", "home_name",
    "away_name", "home_goals", "away_goals", "neutral")


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
        separators=(",", ":"), allow_nan=False).encode()


def digest(value):
    return sha(encoded(value))


def save(path, value):
    with Path(path).open("xb") as stream:
        stream.write(encoded(value) + b"\n")


def serial(row):
    return {k: row[k] for k in SERIAL_FIELDS}


def validate_protocol(protocol):
    """One explicit design, not a backdoor for changing training/admission gates."""
    required = {"baseline_min_train": 180, "baseline_half_life_days": 180,
        "baseline_ridge": 3.0, "horizon": "EARLY", "horizon_hours": 10,
        "block_days": 30, "held_teams_per_block": 4, "held_team_min_past_matches": 5,
        "reference_min_fitted_teams": 8, "inner_min_blocks": 2,
        "inner_min_unique_matches": 20, "tau_grid": [0.0, 0.5, 1.0, 2.0],
        "learning_end": "2025-08-11T09:00:00Z", "confirmation_active": False,
        "production_promotion": False}
    if not isinstance(protocol, dict) or any(protocol.get(k) != v for k, v in required.items()):
        raise ValueError("protocol does not match frozen v1 design/gates")
    if protocol.get("schema_version") != "cold-start-uncertainty-past-masking-protocol-v1":
        raise ValueError("wrong protocol schema")
    ref = protocol.get("source_ref", {})
    if (ref.get("original_path") != "data/epl.json" or not isinstance(ref.get("sha256"), str)
            or len(ref["sha256"]) != 64):
        raise ValueError("frozen canonical source reference required")
    bounds = protocol.get("execution_bounds", {})
    if bounds != {"maximum_refit_attempts": 140, "maximum_parameter_scenarios": 6000}:
        raise ValueError("execution bound changed")
    return protocol


def past_records(source, protocol):
    """Read timestamp/identity first, then project only admitted past outcomes."""
    end = _timestamp(protocol["learning_end"], "learning_end")
    exposed = set(protocol["exposed_current_match_ids"])
    admitted, excluded, projected_keys = [], [], set()
    for row in source["matches"]:
        match_id = row["match_id"]
        kickoff = _timestamp(row["kickoff"], "canonical kickoff")
        if match_id in exposed:
            excluded.append({"match_id": match_id, "reason": "exposed_13_outcome_never_projected"})
            continue
        if kickoff + RESULT_AVAILABILITY_DELAY > end:
            excluded.append({"match_id": match_id, "reason": "after_learning_end"})
            continue
        # No result-field access above this admission gate.
        admitted.append({**{k: row[k] for k in SERIAL_FIELDS if k != "neutral"},
            "neutral": row.get("neutral", False)})
        projected_keys.add(match_id)
    records = _validated_matches(admitted)
    if projected_keys & exposed:
        raise AssertionError("exposed current outcomes entered past result projection")
    return records, excluded


def inventory(records, protocol):
    if not records:
        return {"status": "blocked", "reason": "no_pre_learning_end_records", "blocks": []}
    anchor = records[0]["_dt"] - timedelta(hours=protocol["horizon_hours"])
    grouped = defaultdict(list)
    for row in records:
        forecast_cutoff = row["_dt"] - timedelta(hours=protocol["horizon_hours"])
        block = int((forecast_cutoff - anchor).total_seconds() // (protocol["block_days"] * 86400))
        grouped[block].append(row)
    blocks, refit_attempts, scenarios = [], 0, 0
    for block, targets in sorted(grouped.items()):
        cutoff = anchor + timedelta(days=block * protocol["block_days"])
        training = [r for r in records if r["_dt"] + RESULT_AVAILABILITY_DELAY <= cutoff]
        counts = Counter(t for r in training for t in (r["home_id"], r["away_id"]))
        held = sorted(t for t, n in counts.items()
            if n >= protocol["held_team_min_past_matches"])[:protocol["held_teams_per_block"]]
        masks = []
        for team in held:
            removed = [r for r in training if team in (r["home_id"], r["away_id"])]
            retained = [r for r in training if team not in (r["home_id"], r["away_id"])]
            team_targets = [r for r in targets if team in (r["home_id"], r["away_id"])]
            components = _connected_components(retained)
            nteams = sum(map(len, components))
            reasons = []
            if len(retained) < protocol["baseline_min_train"]:
                reasons.append("insufficient_training_after_complete_team_removal")
            if len(components) != 1:
                reasons.append("retained_training_graph_not_connected")
            if nteams < protocol["reference_min_fitted_teams"]:
                reasons.append("insufficient_remaining_teams_for_reference_variance")
            if not team_targets:
                reasons.append("no_held_team_targets_in_block")
            if not reasons:
                refit_attempts += 1
                scenarios += len(team_targets) * len(protocol["tau_grid"])
            masks.append({"held_team_id": team, "removed_match_ids": [r["match_id"] for r in removed],
                "retained_training_match_ids": [r["match_id"] for r in retained],
                "retained_training_count": len(retained), "remaining_team_count": nteams,
                "connected_components": components, "target_match_ids": [r["match_id"] for r in team_targets],
                "inventory_eligible": not reasons, "failure_reasons": reasons})
        blocks.append({"block": block, "training_cutoff": _iso(cutoff),
            "span_start_inclusive": _iso(cutoff),
            "span_end_exclusive": _iso(cutoff + timedelta(days=protocol["block_days"])),
            "first_target_kickoff": targets[0]["kickoff"], "last_target_kickoff": targets[-1]["kickoff"],
            "unmasked_mature_training_count": len(training),
            "target_match_ids": [r["match_id"] for r in targets], "masks": masks,
            "no_held_team_eligible": not held})
    eligible_blocks = [b["block"] for b in blocks if any(m["inventory_eligible"] for m in b["masks"])]
    within_bound = (refit_attempts <= protocol["execution_bounds"]["maximum_refit_attempts"]
        and scenarios <= protocol["execution_bounds"]["maximum_parameter_scenarios"])
    return {"status": "ready" if eligible_blocks and within_bound else "blocked",
        "reason": None if eligible_blocks and within_bound else
            ("execution_bound_exceeded" if not within_bound else "no_block_preserves_original_training_gate"),
        "anchor": _iso(anchor), "pre_end_matches": len(records),
        "first_kickoff": records[0]["kickoff"], "last_kickoff": records[-1]["kickoff"],
        "eligible_block_ids": eligible_blocks, "eligible_blocks": len(eligible_blocks),
        "planned_refit_attempts": refit_attempts, "planned_parameter_scenarios": scenarios,
        "blocks": blocks}


def reference_variances(model, protocol):
    teams = model["team_parameters"]
    if len(teams) < protocol["reference_min_fitted_teams"]:
        raise ModelDataError("actual fitted team count below frozen variance-reference gate")
    values = np.asarray([[v["attack"], v["defence"]] for _, v in sorted(teams.items())])
    if values.shape[1] != 2 or not np.isfinite(values).all():
        raise ModelDataError("invalid fitted coefficient reference")
    variances = np.var(values, axis=0, ddof=1)
    return {"attack_variance": float(variances[0]), "defence_variance": float(variances[1]),
        "actual_fitted_team_count": len(teams), "fitted_team_ids": sorted(map(int, teams)),
        "ddof": 1, "coefficient_origin": "retained_real_fit_after_all_held_team_matches_removed",
        "minimum_retained_team_training_matches": min(t["training_matches"] for t in teams.values()),
        "reference_is_shrunken_coefficient_variance_not_latent_truth": True,
        "attack_defence_sample_covariance_diagnostic_only": float(np.cov(values.T, ddof=1)[0, 1])}


def metrics(prediction, truth, protocol):
    matrix = np.asarray(prediction["score_matrix"], dtype=float)
    if (matrix.ndim != 2 or not np.isfinite(matrix).all() or (matrix < 0).any()
            or abs(float(matrix.sum()) - 1) > 1e-10):
        raise ValueError("invalid complete prediction matrix")
    h, a = truth["home_goals"], truth["away_goals"]
    if h >= matrix.shape[0] or a >= matrix.shape[1] or matrix[h, a] <= 0:
        raise ValueError("observed_score_outside_supported_matrix_or_zero_probability")
    gh, ga = np.indices(matrix.shape)
    p = np.asarray([matrix[gh > ga].sum(), matrix[gh == ga].sum(), matrix[gh < ga].sum()])
    category = 0 if h > a else 1 if h == a else 2
    truth_vector = np.zeros(3); truth_vector[category] = 1
    intervals = {}
    for label, values, actual in (("total", gh + ga, h + a), ("difference", gh - ga, h - a)):
        support = np.arange(int(values.min()), int(values.max()) + 1)
        pmf = np.asarray([matrix[values == value].sum() for value in support])
        cdf = pmf.cumsum()
        for level in protocol["calibration_diagnostics"]["interval_levels"]:
            lo = int(support[min(len(support)-1, int(np.searchsorted(cdf, (1-level)/2)))])
            hi = int(support[min(len(support)-1, int(np.searchsorted(cdf, 1-(1-level)/2)))])
            intervals[f"{label}_{level:g}"] = {"lower": lo, "upper": hi, "width": hi-lo,
                "covered": lo <= actual <= hi}
    events = {"home_goals_ge4": (gh >= 4, h >= 4), "away_goals_ge4": (ga >= 4, a >= 4),
        "total_goals_ge5": (gh + ga >= 5, h + a >= 5),
        "absolute_goal_difference_ge3": (abs(gh-ga) >= 3, abs(h-a) >= 3)}
    return {"scoreline_nll": -math.log(float(matrix[h, a])),
        "1X2_Brier": float(np.square(p-truth_vector).sum()), "1X2_nll": -math.log(float(p[category])),
        "intervals": intervals, "events": {name: {"probability": float(matrix[mask].sum()),
            "observed": bool(observed)} for name, (mask, observed) in events.items()}}


def select_tau(history, cutoff, protocol):
    """Only earlier, mature blocks; each fixture gets equal weight across masks."""
    cutoff_dt = _timestamp(cutoff, "outer_cutoff")
    pool = [r for r in history if _timestamp(r["training_cutoff"], "inner_training_cutoff") < cutoff_dt
        and _timestamp(r["target_assumed_available_at"], "target availability") <= cutoff_dt
        and r.get("point_metrics") is not None]
    blocks = sorted({r["block"] for r in pool})
    unique = sorted({r["match_id"] for r in pool})
    info = {"inner_blocks": blocks, "inner_unique_match_ids": unique,
        "inner_scenarios": len(pool), "all_inner_outcomes_assumed_mature_by_outer_cutoff": True,
        "actual_observed_by_cutoff": "unknown_retrospective_download",
        "inner_scenario_ids": [r["scenario_id"] for r in pool]}
    if len(blocks) < protocol["inner_min_blocks"] or len(unique) < protocol["inner_min_unique_matches"]:
        return {**info, "tau": 0.0, "selection_status": "insufficient_inner_history_explicit_point_warmup",
            "trials": []}
    trials = []
    for tau in protocol["tau_grid"]:
        by_match = defaultdict(list); fallbacks = 0
        for row in pool:
            trial = next(t for t in row["trials"] if t["tau"] == tau)
            use = trial.get("metrics") or row["point_metrics"]
            fallbacks += int(trial.get("metrics") is None)
            by_match[row["match_id"]].append(use["scoreline_nll"])
        loss = float(np.mean([np.mean(v) for v in by_match.values()]))
        trials.append({"tau": tau, "scoreline_nll": loss, "candidate_failures_point_fallback": fallbacks,
            "unique_matches": len(by_match), "same_pool_as_point": set(by_match) == set(unique)})
    best = min(t["scoreline_nll"] for t in trials)
    chosen = min(t["tau"] for t in trials if t["scoreline_nll"] <= best + protocol["selection_tie_tolerance"])
    return {**info, "tau": chosen, "selection_status": "selected_only_from_earlier_mature_blocks", "trials": trials}


def summarize(rows, protocol):
    def arm_summary(field, subset=None):
        valid = [r for r in (rows if subset is None else subset) if r.get(field) is not None]
        grouped = defaultdict(list)
        for row in valid:
            grouped[row["match_id"]].append(row[field])
        output = {"unique_matches": len(grouped), "mask_scenarios": len(valid),
            "scoreline_nll": None, "1X2_Brier": None, "1X2_nll": None,
            "calibration_status": "exploratory_repeated_masking_diagnostic_not_calibrated_target_unknown_population"}
        if not grouped:
            return output
        for key in ("scoreline_nll", "1X2_Brier", "1X2_nll"):
            output[key] = float(np.mean([np.mean([m[key] for m in group]) for group in grouped.values()]))
        example = next(iter(grouped.values()))[0]
        output["intervals"] = {key: {"coverage": float(np.mean([np.mean([m["intervals"][key]["covered"] for m in group])
            for group in grouped.values()])), "mean_width": float(np.mean([np.mean([m["intervals"][key]["width"]
            for m in group]) for group in grouped.values()]))} for key in example["intervals"]}
        output["events"] = {key: {"mean_probability": float(np.mean([np.mean([m["events"][key]["probability"]
            for m in group]) for group in grouped.values()])), "observed_frequency": float(np.mean([
            np.mean([m["events"][key]["observed"] for m in group]) for group in grouped.values()]))}
            for key in example["events"]}
        return output
    point, strategy = arm_summary("point_metrics"), arm_summary("strategy_metrics")
    selected = [r for r in rows if r.get("strategy_metrics") is not None
        and r.get("selection_status") == "selected_only_from_earlier_mature_blocks"]
    failures = Counter(t.get("failed_stage", "unknown") for r in rows for t in r.get("trials", []) if t["status"] != "success")
    return {"requested_mask_scenarios": len(rows), "unique_requested_matches": len({r["match_id"] for r in rows}),
        "opportunity_status_counts": dict(Counter(r["status"] for r in rows)),
        "parent_or_point_failures_by_first_stage": dict(Counter(r["failed_stage"] for r in rows if r.get("failed_stage"))),
        "point": point, "strategy": strategy,
        "strategy_minus_point_nll": None if point["scoreline_nll"] is None else strategy["scoreline_nll"] - point["scoreline_nll"],
        "nested_selected_mask_scenarios": len(selected),
        "nested_selected_unique_matches": len({r["match_id"] for r in selected}),
        "nested_outer_point": arm_summary("point_metrics", selected),
        "nested_outer_strategy": arm_summary("strategy_metrics", selected),
        "warmup_requested_mask_scenarios": sum(r.get("selection_status") == "insufficient_inner_history_explicit_point_warmup" for r in rows),
        "warmup_scored_mask_scenarios": sum(r.get("selection_status") == "insufficient_inner_history_explicit_point_warmup"
            and r.get("strategy_metrics") is not None for r in rows),
        "attempted_parameter_trials": sum(len(r.get("trials", [])) for r in rows),
        "parameter_trials_not_attempted_parent_or_point_failure": sum(not r.get("point_metrics") for r in rows)*len(protocol["tau_grid"]),
        "chosen_tau_scenario_counts": dict(Counter(str(r.get("selected_tau")) for r in rows if r.get("strategy_metrics") is not None)),
        "selected_candidate_failures_point_fallback": sum(bool(r.get("strategy_used_fallback")) for r in rows),
        "parameter_trial_failures_by_stage": dict(failures),
        "calibration_verified": False, "real_future_unknown_transfer_verified": False,
        "production_adoption": "retain_existing_point_research_prior;no_promotion_from_this_exploratory_experiment",
        "independence": "one_fixture_id_is_one_match;mask_scenarios_and_tau_outputs_are_repeated_measurements"}


def run_experiment(records, protocol, plan, *, progress=None):
    by_id = {r["match_id"]: r for r in records}
    rows, fits, selections = [], [], []
    for block in plan["blocks"]:
        cutoff = block["training_cutoff"]
        selection = select_tau(rows, cutoff, protocol)
        selections.append({"block": block["block"], "outer_training_cutoff": cutoff, **selection})
        for mask in block["masks"]:
            fit_info = {"block": block["block"], "training_cutoff": cutoff, **mask}
            model = None
            if not mask["inventory_eligible"]:
                fit_info.update(status="inventory_failed", failed_stage="pre_fit", reason=mask["failure_reasons"])
            else:
                try:
                    training = [serial(by_id[i]) for i in mask["retained_training_match_ids"]]
                    model = fit_model(training, cutoff, half_life_days=protocol["baseline_half_life_days"],
                        ridge=protocol["baseline_ridge"], min_train=protocol["baseline_min_train"])
                    if str(mask["held_team_id"]) in model["team_parameters"]:
                        raise AssertionError("held team survived full-match removal")
                    reference = reference_variances(model, protocol)
                    fit_info.update(status="success", model=model, reference=reference,
                        model_sha256=digest(model), retained_training_rows_sha256=digest(training))
                except (ValueError, ArithmeticError, KeyError) as exc:
                    fit_info.update(status="failed", failed_stage="parent_refit_or_reference", reason=str(exc), failure_class=type(exc).__name__)
                    model = None
            fits.append(fit_info)
            for match_id in mask["target_match_ids"]:
                truth = by_id[match_id]
                fixture = {k: v for k, v in serial(truth).items() if k not in ("home_goals", "away_goals")}
                row = {"scenario_id": f"b{block['block']}:held{mask['held_team_id']}:m{match_id}",
                    "block": block["block"], "held_team_id": mask["held_team_id"],
                    "match_id": match_id, "fixture": fixture, "truth": {k: truth[k] for k in ("home_goals", "away_goals")},
                    "training_cutoff": cutoff, "prediction_cutoff": _iso(truth["_dt"]-timedelta(hours=protocol["horizon_hours"])),
                    "target_assumed_available_at": _iso(truth["_dt"] + RESULT_AVAILABILITY_DELAY),
                    "actual_observed_by_cutoff": "unknown_retrospective_download", "trials": [],
                    "selected_tau": selection["tau"], "selection_status": selection["selection_status"]}
                if model is None:
                    row.update(status="unscorable_parent_failure", failed_stage=fit_info["failed_stage"],
                        first_failure=fit_info.get("reason"), point_metrics=None, strategy_metrics=None)
                    rows.append(row); continue
                try:
                    point = score_cold_start(model, fixture)
                    row.update(point_prediction=point, point_metrics=metrics(point, truth, protocol))
                except (ValueError, ArithmeticError, KeyError) as exc:
                    row.update(status="unscorable_point_failure", failed_stage="point_prediction_or_scoring", first_failure=str(exc),
                        point_metrics=None, strategy_metrics=None)
                    rows.append(row); continue
                for tau in protocol["tau_grid"]:
                    params = {"tau": tau, "attack_variance": reference["attack_variance"],
                        "defence_variance": reference["defence_variance"], "attack_defence_correlation": 0.0,
                        "cross_team_correlation": 0.0, "known_parameter_uncertainty": False}
                    trial = {"tau": tau, "parameters": params}
                    try:
                        calc = predict_uncertainty(model, fixture, params, protocol["numerical_settings"])
                        trial.update(status="success", calculation=calc,
                            metrics=metrics(calc["prediction"], truth, protocol))
                    except (ValueError, ArithmeticError, KeyError) as exc:
                        trial.update(status="failed", failed_stage="mixture_numerics_or_scoring", reason=str(exc),
                            failure_class=type(exc).__name__, fallback="unchanged_point", metrics=None)
                    row["trials"].append(trial)
                chosen = next(t for t in row["trials"] if t["tau"] == selection["tau"])
                row.update(status="scored", strategy_metrics=chosen.get("metrics") or row["point_metrics"],
                    strategy_used_fallback=chosen.get("metrics") is None,
                    strategy_first_failure=chosen.get("reason"), point_candidate_paired_same_simulated_fixture=True)
                rows.append(row)
        if progress:
            progress({"completed_block": block["block"], "refit_records": len(fits), "mask_scenarios": len(rows),
                "selected_tau": selection["tau"], "selection_status": selection["selection_status"]})
    summary = summarize(rows, protocol)
    summary["parent_fit_status_counts"] = dict(Counter(r["status"] for r in fits))
    final_selection = select_tau(rows, protocol["learning_end"], protocol)
    return {"schema_version": VERSION, "fits": fits, "outer_selections": selections, "rows": rows,
        "final_past_only_tau_selection": {"cutoff": protocol["learning_end"], **final_selection},
        "summary": summary}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--protocol", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--inventory-only", action="store_true")
    args = parser.parse_args(argv)
    script_raw, protocol_raw, source_raw = Path(__file__).read_bytes(), args.protocol.read_bytes(), args.source.read_bytes()
    protocol = validate_protocol(json.loads(protocol_raw))
    if sha(source_raw) != protocol["source_ref"]["sha256"]:
        raise ValueError("full canonical source bytes do not match frozen reference")
    from football_model.advanced_research import implementation_fingerprint
    before = implementation_fingerprint()
    if before["sha256"] != EXPECTED_50 or len(before["files"]) != 50:
        raise ValueError("original 50-file closure changed; refuse this frozen experiment")
    calculator_path = REPO / "scripts/cold_start_uncertainty.py"
    calculator_raw = calculator_path.read_bytes()
    source = json.loads(source_raw)
    records, excluded = past_records(source, protocol)
    plan = inventory(records, protocol)
    identity = {"runner_sha256": sha(script_raw), "protocol_raw_sha256": sha(protocol_raw),
        "canonical_source_sha256": sha(source_raw), "calculator_sha256": sha(calculator_raw),
        "original_50_file_implementation": before, "mode": "inventory" if args.inventory_only else "execute",
        "past_input_rows_sha256": digest([serial(r) for r in records])}
    run_id = digest(identity)
    out = args.output / run_id
    out.mkdir(parents=True, exist_ok=False)
    save(out / "identity.json", identity)
    for name, raw in (("runner.py", script_raw), ("protocol.json", protocol_raw),
            ("canonical-source.json", source_raw), ("calculator.py", calculator_raw)):
        with (out / name).open("xb") as stream: stream.write(raw)
    save(out / "past-inputs.json", {"records": [serial(r) for r in records], "excluded_id_reasons": excluded,
        "exposed_13_result_projection": False, "actual_observed_by_cutoff": "unknown_retrospective_download"})
    save(out / "inventory.json", plan)
    started = datetime.now(timezone.utc).isoformat()
    result = None
    if not args.inventory_only and plan["status"] == "ready":
        result = run_experiment(records, protocol, plan, progress=lambda info: print(json.dumps(info), flush=True))
        save(out / "result.json", result)
        save(out / "summary.json", result["summary"])
    after = implementation_fingerprint()
    if (after != before or Path(__file__).read_bytes() != script_raw or args.protocol.read_bytes() != protocol_raw
            or args.source.read_bytes() != source_raw or calculator_path.read_bytes() != calculator_raw):
        raise ValueError("frozen code/source identity changed during run; output not accepted")
    import scipy
    receipt = {"schema_version": VERSION, "run_identity": run_id, "started_at": started,
        "completed_at": datetime.now(timezone.utc).isoformat(), "output": str(out),
        "execution_status": "inventory_only" if args.inventory_only else ("complete" if result is not None else "blocked"),
        "identity": identity, "inventory_status": plan["status"], "inventory_reason": plan["reason"],
        "original_50_files_unchanged": after == before,
        "numeric_environment": {"python": platform.python_version(), "numpy": np.__version__, "scipy": scipy.__version__},
        "files": {p.name: {"sha256": sha(p.read_bytes()), "bytes": p.stat().st_size} for p in sorted(out.iterdir()) if p.is_file()},
        "actual_observed_by_cutoff": "unknown_retrospective_download", "exposed_13_outcomes_used": False,
        "calibration_verified": False, "production_changed": False, "new_API_requests": 0}
    save(out / "receipt.json", receipt)
    print(json.dumps({"run_identity": run_id, "output": str(out), "status": receipt["execution_status"],
        "inventory": {k: v for k, v in plan.items() if k != "blocks"}, "summary": result["summary"] if result else None}), flush=True)
    return receipt


if __name__ == "__main__":
    receipt = main()
    sys.exit(2 if receipt["execution_status"] == "blocked" else 0)
