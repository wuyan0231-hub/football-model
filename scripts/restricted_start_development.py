"""Independent past-seen-player start probabilities; historical development only."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sys

import numpy as np
from scipy.optimize import minimize
from scipy.special import expit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from football_model.advanced_research import implementation_fingerprint


VERSION = "restricted-start-development-v1"


def utc(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def write(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")


def phase_of(kickoff, protocol):
    for phase in ("train", "select", "calibrate"):
        if utc(kickoff) <= utc(protocol["splits"][phase + "_end"]):
            return phase
    return "test"


def prepare(inventory, protocol):
    """Inputs use only frozen pool and past observations; target XI supplies y."""
    indexed = {m["match_id"]: m for m in inventory["matches"]}
    if len(indexed) != len(inventory["matches"]):
        raise ValueError("duplicate_fixture")
    rows, opportunities = [], []
    delay = timedelta(hours=protocol["result_availability_delay_hours"])
    for m in sorted(indexed.values(), key=lambda m: (utc(m["kickoff"]), m["match_id"])):
        phase = phase_of(m["kickoff"], protocol)
        cutoff = utc(m["prediction_cutoff_at"])
        if cutoff != utc(m["kickoff"]) - timedelta(hours=protocol["horizon_hours"]):
            raise ValueError("inventory_horizon_mismatch")
        eligible = m["both_teams_historical_labels_available"] is True
        opportunity = {"match_id": m["match_id"], "kickoff": m["kickoff"],
            "cutoff_at": m["prediction_cutoff_at"], "phase": phase,
            "eligible_paired": eligible, "included_after_embargo": eligible,
            "reason": None if eligible else "paired_xi_or_nonempty_restricted_pool_unavailable",
            "outside_pool_starters": sum(len(r["historical_development_assumption"]["outside_pool_starting_ids"] or [])
                for r in m["teams"].values()),
            "legal_xi_teams": sum(r["target_xi_legal"] for r in m["teams"].values())}
        opportunities.append(opportunity)
        if not eligible:
            continue
        for tid, team in sorted(m["teams"].items()):
            pool = team["historical_development_assumption"]
            pids, positive = set(pool["candidate_ids"]), set(pool["positive_ids"])
            if positive != pids & set(team["target_xi_ids"]):
                raise ValueError("target_label_partition_mismatch")
            past = [indexed[mid] for mid in team["prior_match_ids"]]
            if len(past) > protocol["candidate_window_matches"]:
                raise ValueError("history_window_exceeded")
            for previous in past:
                if previous["match_id"] == m["match_id"] or utc(previous["kickoff"]) + delay > cutoff:
                    raise ValueError("past_input_not_before_cutoff")
            known = [previous for previous in past if previous["teams"][tid]["target_xi_legal"]]
            candidate_origins = pool["candidate_origins"]
            for origin in candidate_origins:
                if origin["match_id"] not in team["prior_match_ids"] or utc(origin["kickoff"]) + delay > cutoff:
                    raise ValueError("candidate_origin_not_past")
            for pid in sorted(pids):
                starts = sum(pid in previous["teams"][tid]["target_xi_ids"] for previous in known)
                frequency = (starts + 0.5) / (len(known) + 1)
                last = (int(pid in past[-1]["teams"][tid]["target_xi_ids"])
                    if past and past[-1]["teams"][tid]["target_xi_legal"] else None)
                origins = [o for o in candidate_origins if pid in o["player_ids"]]
                if not origins:
                    raise ValueError("candidate_without_past_origin")
                membership_count = len({o["match_id"] for o in origins})
                days = (cutoff - max(utc(o["kickoff"]) for o in origins)).total_seconds() / 86400
                rows.append({"match_id": m["match_id"], "team_id": int(tid), "player_id": pid,
                    "kickoff": m["kickoff"], "cutoff_at": m["prediction_cutoff_at"], "phase": phase,
                    "y": int(pid in positive), "baseline_probability": frequency,
                    "x": [frequency, last, membership_count, len(known), days],
                    "prior_match_ids": team["prior_match_ids"],
                    "label_known_at": team["label_effective_known_at"],
                    "source_mode": "historical_development_assumption_not_actual_pit"})
    included = {}
    for phase in ("train", "select", "calibrate", "test"):
        prior = [o for name, values in included.items() for o in values]
        mature = max((utc(o["kickoff"]) + delay for o in prior), default=None)
        current = [o for o in opportunities if o["phase"] == phase and o["eligible_paired"]]
        for o in current:
            if mature is not None and utc(o["cutoff_at"]) < mature:
                o.update(included_after_embargo=False, reason="earlier_phase_labels_not_mature_at_prediction_cutoff")
        included[phase] = [o for o in current if o["included_after_embargo"]]
    included_ids = {o["match_id"] for values in included.values() for o in values}
    for row in rows:
        row["included_after_embargo"] = row["match_id"] in included_ids
    gates = {phase: {"actual": len(included[phase]), "minimum": minimum,
                    "passed": len(included[phase]) >= minimum}
             for phase, minimum in protocol["minimum_paired_fixtures"].items()}
    if len(opportunities) != protocol["expected_total_opportunities"]:
        raise ValueError("full_opportunity_denominator_mismatch")
    return rows, opportunities, gates


def fit_preprocessor(rows):
    raw = np.array([[np.nan if x is None else x for x in r["x"]] for r in rows], dtype=float)
    missing = np.isnan(raw)
    medians = np.array([np.median(raw[~missing[:, i], i]) if (~missing[:, i]).any() else 0.0
                        for i in range(raw.shape[1])])
    filled = np.where(missing, medians, raw)
    expanded = np.hstack([filled, missing.astype(float)])
    means, scales = expanded.mean(axis=0), expanded.std(axis=0)
    scales[scales < 1e-12] = 1
    return {"medians": medians.tolist(), "means": means.tolist(), "scales": scales.tolist(),
            "fit_fixture_ids": sorted({r["match_id"] for r in rows}),
            "raw_nulls_preserved_in_dataset": True, "missing_indicator_columns_appended": True,
            "all_training_missing_columns": [int(i) for i in range(raw.shape[1]) if missing[:, i].all()],
            "all_missing_column_constant_zero_is_model_encoding_not_observed_fact": True}


def design(rows, preprocessor):
    raw = np.array([[np.nan if x is None else x for x in r["x"]] for r in rows], dtype=float)
    missing = np.isnan(raw)
    filled = np.where(missing, np.array(preprocessor["medians"]), raw)
    expanded = np.hstack([filled, missing.astype(float)])
    scaled = (expanded - np.array(preprocessor["means"])) / np.array(preprocessor["scales"])
    return np.column_stack([np.ones(len(rows)), scaled])


def fit_logistic(rows, alpha, settings):
    pre = fit_preprocessor(rows)
    x = design(rows, pre)
    y = np.array([r["y"] for r in rows], dtype=float)
    if len(np.unique(y)) != 2:
        raise ValueError("training_requires_both_labels")
    initial = np.zeros(x.shape[1])
    initial[0] = np.log(y.mean() / (1 - y.mean()))
    def objective(beta):
        z = x @ beta
        penalty = beta.copy(); penalty[0] = 0
        return (np.mean(np.logaddexp(0, z) - y * z) + 0.5 * alpha * np.dot(penalty, penalty),
                x.T @ (expit(z) - y) / len(y) + alpha * penalty)
    result = minimize(objective, initial, jac=True, method="L-BFGS-B",
        options={k: settings[k] for k in ("maxiter", "gtol", "ftol")})
    if not result.success or not np.all(np.isfinite(result.x)):
        raise ValueError("logistic_solver_not_converged")
    return {"preprocessor": pre, "coefficients": result.x.tolist(), "alpha": alpha,
        "solver_success": bool(result.success), "iterations": int(result.nit),
        "objective": float(result.fun), "fit_player_labels": len(rows),
        "fit_fixture_ids": sorted({r["match_id"] for r in rows})}


def predict(rows, model):
    return expit(design(rows, model["preprocessor"]) @ np.array(model["coefficients"]))


def fit_calibration(probabilities, rows, epsilon):
    p = np.clip(np.asarray(probabilities), epsilon, 1 - epsilon)
    logits = np.log(p / (1 - p)); y = np.array([r["y"] for r in rows], dtype=float)
    if len(np.unique(y)) != 2:
        raise ValueError("calibration_requires_both_labels")
    def objective(theta):
        a, b = theta; z = a * logits + b; residual = expit(z) - y
        return (np.mean(np.logaddexp(0, z) - y * z) + .001 * ((a - 1) ** 2 + b ** 2),
                np.array([np.mean(residual * logits) + .002 * (a - 1), np.mean(residual) + .002 * b]))
    result = minimize(objective, [1., 0.], jac=True, method="L-BFGS-B", bounds=[(0, None), (None, None)],
                      options={"maxiter": 1000, "gtol": 1e-8, "ftol": 1e-12})
    if not result.success or not np.all(np.isfinite(result.x)):
        raise ValueError("calibration_solver_not_converged")
    return {"slope": float(result.x[0]), "intercept": float(result.x[1]), "solver_success": True,
        "fit_fixture_ids": sorted({r["match_id"] for r in rows}), "fit_player_labels": len(rows)}


def calibrate(probabilities, calibration, epsilon):
    p = np.clip(np.asarray(probabilities), epsilon, 1 - epsilon)
    return expit(calibration["slope"] * np.log(p / (1 - p)) + calibration["intercept"])


def score(rows, probabilities, epsilon):
    p = np.clip(np.asarray(probabilities), epsilon, 1 - epsilon)
    y = np.array([r["y"] for r in rows], dtype=float)
    nll = -(y * np.log(p) + (1 - y) * np.log(1 - p)); brier = (p - y) ** 2
    fixtures, teams = defaultdict(list), defaultdict(list)
    for i, row in enumerate(rows):
        fixtures[row["match_id"]].append(i)
        teams[(row["match_id"], row["team_id"])].append(i)
    reliability = []
    bins = np.minimum((p * 10).astype(int), 9)
    for b in range(10):
        mask = bins == b; count = int(mask.sum())
        reliability.append({"lower": b / 10, "upper": (b + 1) / 10, "count": count,
            "mean_probability": float(p[mask].mean()) if count else None,
            "observed_start_rate": float(y[mask].mean()) if count else None})
    sums = [float(p[indices].sum()) for indices in teams.values()]
    return {"fixture_count": len(fixtures), "player_labels": len(rows),
        "equal_fixture_macro_bernoulli_nll": float(np.mean([nll[indices].mean() for indices in fixtures.values()])),
        "equal_fixture_macro_brier": float(np.mean([brier[indices].mean() for indices in fixtures.values()])),
        "player_micro_bernoulli_nll": float(nll.mean()), "player_micro_brier": float(brier.mean()),
        "micro_ece": float(sum(r["count"] * abs(r["mean_probability"] - r["observed_start_rate"])
            for r in reliability if r["count"]) / len(rows)), "reliability": reliability,
        "per_team_probability_sum": {"min": min(sums), "max": max(sums), "mean": float(np.mean(sums)),
            "normalized_to_eleven": False}}


def execute(rows, opportunities, gates, protocol):
    if not all(g["passed"] for g in gates.values()):
        return {"status": "blocked_insufficient_time_partition_samples", "sample_gates": gates,
                "fits_performed": 0}, {}, []
    partitions = {phase: [r for r in rows if r["phase"] == phase and r["included_after_embargo"]]
                  for phase in ("train", "select", "calibrate", "test")}
    eps = protocol["evaluation"]["probability_clip_epsilon"]
    attempts, fitted = [], []
    for alpha in protocol["model"]["alpha_grid"]:
        try:
            model = fit_logistic(partitions["train"], alpha, protocol["model"])
            metrics = score(partitions["select"], predict(partitions["select"], model), eps)
            attempts.append({"alpha": alpha, "status": "fitted", "selection_metrics": metrics, "model": model})
            fitted.append((metrics["equal_fixture_macro_bernoulli_nll"], -alpha, model))
        except ValueError as exc:
            attempts.append({"alpha": alpha, "status": "failed", "error_code": str(exc)})
    if not fitted:
        return {"status": "blocked_no_converged_logistic_candidate", "sample_gates": gates,
                "selection_attempts": attempts, "fits_performed": len(attempts)}, {}, []
    _, _, selected = min(fitted, key=lambda x: (x[0], x[1]))
    final_model = fit_logistic(partitions["train"] + partitions["select"], selected["alpha"], protocol["model"])
    cal_rows = partitions["calibrate"]
    cal_base = np.array([r["baseline_probability"] for r in cal_rows]); cal_logistic = predict(cal_rows, final_model)
    base_cal = fit_calibration(cal_base, cal_rows, eps); logistic_cal = fit_calibration(cal_logistic, cal_rows, eps)
    test = partitions["test"]; base = np.array([r["baseline_probability"] for r in test]); logistic = predict(test, final_model)
    probabilities = {"frequency_raw": base, "frequency_calibrated": calibrate(base, base_cal, eps),
        "logistic_raw": logistic, "logistic_calibrated": calibrate(logistic, logistic_cal, eps)}
    metrics = {name: score(test, p, eps) for name, p in probabilities.items()}
    before, after = metrics["frequency_calibrated"], metrics["logistic_calibrated"]
    delta = {key: after[key] - before[key] for key in ("equal_fixture_macro_bernoulli_nll", "equal_fixture_macro_brier",
        "player_micro_bernoulli_nll", "player_micro_brier", "micro_ece")}
    predictions = [{"match_id": r["match_id"], "team_id": r["team_id"], "player_id": r["player_id"],
        "kickoff": r["kickoff"], "cutoff_at": r["cutoff_at"], "y": r["y"],
        **{name: float(p[i]) for name, p in probabilities.items()}} for i, r in enumerate(test)]
    phase_coverage = {}
    for phase in partitions:
        opps = [o for o in opportunities if o["phase"] == phase]
        used = [o for o in opps if o["included_after_embargo"]]
        phase_coverage[phase] = {"all_opportunities": len(opps), "eligible_before_embargo": sum(o["eligible_paired"] for o in opps),
            "included_after_embargo": len(used), "player_labels": len(partitions[phase]),
            "outside_pool_starters_included_matches": sum(o["outside_pool_starters"] for o in used),
            "within_pool_positive_labels": sum(r["y"] for r in partitions[phase])}
    report = {"status": "historical_development_executed_not_formal_confirmation", "sample_gates": gates,
        "selected_alpha": selected["alpha"], "selection_attempts": attempts, "fits_performed": len(attempts) + 3,
        "phase_coverage": phase_coverage, "test_metrics": metrics,
        "calibrated_logistic_minus_calibrated_frequency": delta,
        "calibration_fit_metrics_are_apparent_not_independent_validation": True,
        "calibration_fit_apparent_metrics": {
            "frequency_raw": score(cal_rows, cal_base, eps), "frequency_calibrated": score(cal_rows, calibrate(cal_base, base_cal, eps), eps),
            "logistic_raw": score(cal_rows, cal_logistic, eps), "logistic_calibrated": score(cal_rows, calibrate(cal_logistic, logistic_cal, eps), eps)},
        "complete_xi_joint_distribution": False, "historical_pit_confirmed": False,
        "independent_future_accuracy_or_returns_improvement_verified": False}
    return report, {"logistic": final_model, "frequency_calibration": base_cal, "logistic_calibration": logistic_cal}, predictions


def run(inventory_path, protocol_path, freeze_path, output):
    started = datetime.now(timezone.utc)
    raw_inventory, raw_protocol = Path(inventory_path).read_bytes(), Path(protocol_path).read_bytes()
    inventory, protocol = json.loads(raw_inventory), json.loads(raw_protocol)
    freeze = json.loads(Path(freeze_path).read_text())
    if (sha(raw_inventory) != protocol["inventory_sha256"] or sha(raw_protocol) != freeze["protocol_sha256"]
            or freeze["status"] != "frozen_before_fit" or freeze["probability_fits_performed"] != 0
            or utc(freeze["frozen_at"]) > started):
        raise ValueError("input_identity_or_before_fit_freeze_invalid")
    if inventory["configuration"]["window_matches"] != protocol["candidate_window_matches"]:
        raise ValueError("inventory_window_mismatch")
    output = Path(output)
    if output.exists():
        raise ValueError("use_new_output_directory")
    rows, opportunities, gates = prepare(inventory, protocol)
    output.mkdir(parents=True)
    write(output / "before-fit-validation.json", {"validated_at": datetime.now(timezone.utc).isoformat(),
        "protocol_sha256": sha(raw_protocol), "sample_gates": gates, "fits_performed": 0})
    report, models, predictions = execute(rows, opportunities, gates, protocol)
    fingerprint = implementation_fingerprint()
    report["schema_version"] = VERSION
    report["provenance"] = {"started_at": started.isoformat(), "completed_at": datetime.now(timezone.utc).isoformat(),
        "protocol_sha256": sha(raw_protocol), "protocol_frozen_at": freeze["frozen_at"],
        "inventory_sha256": sha(raw_inventory), "fixed_state_sha": protocol["fixed_state_sha"],
        "script_sha256": sha(Path(__file__).read_bytes()), "native_fingerprint_sha256": fingerprint["sha256"],
        "native_50_dependency_fingerprint_unchanged": fingerprint == inventory["provenance"]["research_implementation_fingerprint"],
        "paid_api_requests": 0, "production_changed": False, "model_state_written": False,
        "full_personnel_model_completed": False}
    write(output / "report.json", report); write(output / "models.json", models)
    write(output / "all-opportunities.json", opportunities)
    for name, values in (("player-labels.jsonl", rows), ("test-predictions.jsonl", predictions)):
        (output / name).write_text("".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in values))
    write(output / "artifacts.json", {p.name: {"sha256": sha(p.read_bytes()), "bytes": p.stat().st_size}
        for p in sorted(output.iterdir()) if p.is_file()})
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory", required=True); parser.add_argument("--protocol", required=True)
    parser.add_argument("--before-fit-freeze", required=True); parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    report = run(args.inventory, args.protocol, args.before_fit_freeze, args.output)
    print(json.dumps({k: report[k] for k in ("status", "sample_gates", "selected_alpha", "test_metrics", "calibrated_logistic_minus_calibrated_frequency", "provenance") if k in report}, ensure_ascii=False))
    return 0 if report["status"] == "historical_development_executed_not_formal_confirmation" else 1


if __name__ == "__main__":
    raise SystemExit(main())
