#!/usr/bin/env python3
"""Independent diagonal lognormal cold-start mixture; engineering research only.

No fitting, data collection, production integration or uncertainty calibration.
The zero-variance arm returns the existing cold-start prediction unchanged.
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
import sys

import numpy as np
from numpy.polynomial.hermite import hermgauss
from scipy.stats import norm, poisson

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
from football_model.cold_start import score_cold_start

VERSION = "cold_start_diagonal_mean_rate_lognormal_mixture_v1"
DEFAULT_SETTINGS = {
    "node_orders": [12, 20, 32, 48, 64, 96, 128],
    "quadrature_l1_tolerance": 1e-10,
    "tail_tolerance": 1e-8,
    "max_goal": 512,
    "max_log_rate_variance": 1.0,
}


class UncertaintyError(ValueError):
    pass


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
        separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def finite(value, name, *, minimum=0.0):
    try:
        valid = type(value) in (int, float) and math.isfinite(value) and value >= minimum
    except (OverflowError, TypeError):
        valid = False
    if not valid:
        raise UncertaintyError(name + " must be a finite nonnegative number, never bool")
    return float(value)


def dependency_hashes():
    return {name: hashlib.sha256((REPO / name).read_bytes()).hexdigest()
        for name in ("football_model/__init__.py", "football_model/cold_start.py", "football_model/model.py")}


def validate(parameters, settings=None):
    required = {"tau", "attack_variance", "defence_variance", "attack_defence_correlation",
        "cross_team_correlation", "known_parameter_uncertainty"}
    if not isinstance(parameters, dict) or set(parameters) != required:
        raise UncertaintyError("parameters must contain exactly the declared diagonal-prior fields")
    values = {k: finite(parameters[k], k) for k in ("tau", "attack_variance", "defence_variance")}
    for name in ("attack_defence_correlation", "cross_team_correlation"):
        if type(parameters[name]) not in (int, float) or parameters[name] != 0:
            raise UncertaintyError("nonzero/invalid correlations require joint integration; outer product rejected")
    if parameters["known_parameter_uncertainty"] is not False:
        raise UncertaintyError("known-parameter uncertainty is not implemented in this calculator")
    values.update(attack_defence_correlation=0.0, cross_team_correlation=0.0,
        known_parameter_uncertainty=False)
    cfg = copy.deepcopy(DEFAULT_SETTINGS if settings is None else settings)
    if not isinstance(cfg, dict) or set(cfg) != set(DEFAULT_SETTINGS):
        raise UncertaintyError("unknown or missing numerical setting")
    orders = cfg["node_orders"]
    if (not isinstance(orders, (list, tuple)) or len(orders) < 3
            or any(type(n) is not int or not 4 <= n <= 128 for n in orders)
            or list(orders) != sorted(set(orders))):
        raise UncertaintyError("node orders must be at least three increasing integers from4 to128")
    cfg["node_orders"] = list(orders)
    tail = finite(cfg["tail_tolerance"], "tail_tolerance")
    qtol = finite(cfg["quadrature_l1_tolerance"], "quadrature_l1_tolerance")
    if not 1e-12 <= tail <= 1e-5 or not 1e-14 <= qtol <= tail / 32:
        raise UncertaintyError("invalid tail/quadrature precision budget")
    if type(cfg["max_goal"]) is not int or not 1 <= cfg["max_goal"] <= 1024:
        raise UncertaintyError("max_goal must be an explicit integer bound from1 to1024")
    maxv = finite(cfg["max_log_rate_variance"], "max_log_rate_variance")
    if not 0 < maxv <= 1.0:
        raise UncertaintyError("this first calculator supports max log-rate variance at most1")
    return values, cfg


def _gh_marginal(mean_rate, variance, order, last_goal):
    """Full-real-line Gauss-Hermite; no node rates or prior draws are clipped."""
    x, weights = hermgauss(order)
    weights = weights / math.sqrt(math.pi)
    logs = math.log(mean_rate) - variance / 2 + math.sqrt(2 * variance) * x
    with np.errstate(over="raise", invalid="raise", under="raise"):
        try:
            rates = np.exp(logs)
        except FloatingPointError as exc:
            raise UncertaintyError("quadrature node rate overflow/underflow; no clipping allowed") from exc
    if not np.all(np.isfinite(rates)) or np.any(rates <= 0) or not np.all(weights > 0):
        raise UncertaintyError("invalid quadrature node/weight")
    pmf = poisson.pmf(np.arange(last_goal + 1)[:, None], rates[None, :]) @ weights
    tail = float(np.dot(poisson.sf(last_goal, rates), weights))
    mean = float(np.dot(rates, weights))
    if (not np.all(np.isfinite(pmf)) or np.any(pmf < 0)
            or abs(float(pmf.sum()) + tail - 1) > 5e-12):
        raise UncertaintyError("node-integrated Poisson mass identity failed")
    return pmf, {"order": order, "quadrature_mean_rate": mean,
        "quadrature_tail_mass": tail, "weights_sum": float(weights.sum()),
        "largest_node_rate": float(rates.max()), "smallest_node_rate": float(rates.min())}


def marginal(mean_rate, variance, cfg):
    """Bound the actual grid tail separately from GH convergence diagnostics.

For any positive cap L, P(G>K)<=P(lambda>L)+Poisson.sf(K,L).
L is used only for this bound, never to truncate the integrated prior or rates.
"""
    if not math.isfinite(mean_rate) or mean_rate <= 0:
        raise UncertaintyError("base mean rate must be positive and finite")
    if not math.isfinite(variance) or variance < 0 or variance > cfg["max_log_rate_variance"]:
        raise UncertaintyError("log-rate variance outside explicit engineering scope")
    allowance = cfg["tail_tolerance"] / 8
    if variance:
        z = float(norm.isf(allowance))
        cap = math.exp(math.log(mean_rate) - variance / 2 + math.sqrt(variance) * z)
        prior_bound = float(norm.sf(z))
    else:
        cap, prior_bound = mean_rate, 0.0
    # sf is monotone in lambda; this provides a distribution-level bound.
    quantile = float(poisson.isf(allowance, cap))
    if not math.isfinite(quantile) or quantile > cfg["max_goal"]:
        raise UncertaintyError("required probability grid exceeds max_goal; wide tail is not discarded")
    last_goal = max(1, int(math.ceil(quantile)))
    poisson_bound = float(poisson.sf(last_goal, cap))
    tail_bound = prior_bound + poisson_bound
    if tail_bound > cfg["tail_tolerance"] / 4 + 1e-15:
        raise UncertaintyError("analytic prior/Poisson tail budget not met")
    base = {"analytic_mean_rate": mean_rate, "log_rate_variance": variance,
        "log_rate_mean_shift": -variance / 2, "last_goal": last_goal,
        "rate_cap_for_bound_only": cap, "prior_above_cap_probability_bound": prior_bound,
        "poisson_tail_at_cap_bound": poisson_bound, "actual_distribution_grid_tail_upper_bound": tail_bound,
        "prior_or_node_rates_clipped": False}
    if variance == 0:
        pmf = poisson.pmf(np.arange(last_goal + 1), mean_rate)
        return pmf, {**base, "node_converged": True, "quadrature_method": "analytic_point_rate",
            "quadrature_l1_last_difference": 0.0, "orders": []}
    previous, streak, trials, pmf = None, 0, [], None
    for order in cfg["node_orders"]:
        pmf, info = _gh_marginal(mean_rate, variance, order, last_goal)
        delta = float(np.abs(pmf - previous).sum()) if previous is not None else None
        mean_error = abs(info["quadrature_mean_rate"] - mean_rate)
        info.update(l1_difference_from_previous_order=delta, mean_rate_error=mean_error)
        trials.append(info)
        converged = (delta is not None and delta <= cfg["quadrature_l1_tolerance"]
            and mean_error <= cfg["quadrature_l1_tolerance"] * max(1.0, mean_rate))
        streak = streak + 1 if converged else 0
        if streak >= 2:
            if info["quadrature_tail_mass"] > tail_bound + 4 * cfg["quadrature_l1_tolerance"]:
                raise UncertaintyError("quadrature tail contradicts independent analytic bound")
            return pmf, {**base, "node_converged": True,
                "quadrature_method": "Gauss_Hermite_full_normal_domain",
                "quadrature_error_kind": "successive_order_L1_diagnostic_not_a_rigorous_integration_error_bound",
                "quadrature_l1_last_difference": delta, "orders": trials}
        previous = pmf
    raise UncertaintyError("quadrature nodes did not converge; no normalized output produced")


def predict_uncertainty(model, fixture, parameters, settings=None):
    params, cfg = validate(parameters, settings)
    original_model = json.dumps(model, sort_keys=True, separators=(",", ":"), allow_nan=False)
    point = score_cold_start(model, fixture)
    unknown = point["unknown_team_ids"]
    try:
        scale = params["tau"] ** 2
    except OverflowError as exc:
        raise UncertaintyError("variance scaling overflow") from exc
    if not math.isfinite(scale):
        raise UncertaintyError("variance scaling overflow")
    va, vd = scale * params["attack_variance"], scale * params["defence_variance"]
    home_unknown = fixture["home_id"] in unknown
    away_unknown = fixture["away_id"] in unknown
    vh = (va if home_unknown else 0) + (vd if away_unknown else 0)
    vaway = (va if away_unknown else 0) + (vd if home_unknown else 0)
    identity = {"calculator_version": VERSION, "parameters": params, "settings": cfg,
        "calculator_source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "existing_dependency_sha256": dependency_hashes()}
    wrapper = {"schema_version": VERSION, "parameter_input": params,
        "numerical_settings": cfg, "parameter_identity_sha256": digest(identity),
        "calculator_source_sha256": identity["calculator_source_sha256"],
        "existing_dependency_sha256": identity["existing_dependency_sha256"],
        "research_only": True, "calibration_verified": False, "production_promoted": False,
        "known_parameters_uncertainty_included": False,
        "independence_assumptions": "all_unknown_A_D_within_and_across_teams_independent;known_parameters_fixed",
        "latent_variable_assignment": {"home_rate": "A_home_unknown_minus_D_away_unknown",
            "away_rate": "A_away_unknown_minus_D_home_unknown"}}
    if vh == 0 and vaway == 0:
        wrapper.update(prediction=point, exact_point_prior_branch=True,
            diagnostics={"integration": "existing_score_cold_start_called_unchanged",
                "mean_rate_preservation": "exact_existing_point_prediction"})
    else:
        h, hd = marginal(point["expected_goals"]["home"], vh, cfg)
        a, ad = marginal(point["expected_goals"]["away"], vaway, cfg)
        raw = np.outer(h, a)
        mass = float(raw.sum())
        tail_bound = hd["actual_distribution_grid_tail_upper_bound"] + ad["actual_distribution_grid_tail_upper_bound"]
        q_indicator = hd["quadrature_l1_last_difference"] + ad["quadrature_l1_last_difference"]
        if (not np.isfinite(raw).all() or (raw < 0).any() or not 0 < mass <= 1 + 1e-11
                or 1 - mass > tail_bound + 4 * cfg["quadrature_l1_tolerance"]
                or tail_bound + 4 * q_indicator > cfg["tail_tolerance"]):
            raise UncertaintyError("joint retained mass or numerical error budget not met")
        matrix = raw / mass  # Only after independent tail and node checks; disclosed below.
        prediction = copy.deepcopy(point)
        prediction.update(model_version=VERSION, candidate_id="COLD_START_UNCERTAINTY_DEVELOPMENT",
            model_scope="unknown_team_diagonal_prior_mixture_research_only",
            unknown_team_coefficients="independent_normal_A_D_latents_with_mean_rate_preserving_exponential_multipliers",
            unknown_team_coefficient_prior={"attack_mean": -va / 2, "attack_variance": va,
                "defence_mean": vd / 2, "defence_variance": vd, "attack_defence_correlation": 0.0,
                "independent_across_unknown_teams": True},
            score_matrix=matrix.tolist(), matrix_renormalized=True,
            tail_tolerance=cfg["tail_tolerance"],
            tail_mass_before_normalization=max(0.0, 1 - mass),
            negative_tail_roundoff_clamped=mass > 1,
            probabilities={"home_win": float(np.tril(matrix, -1).sum()),
                "draw": float(np.trace(matrix)), "away_win": float(np.triu(matrix, 1).sum())},
            uncertainty_integrated=True, calibrated_uncertainty_available=False)
        prediction["warnings"].append("未知攻防以固定对角分布积分；方差仅为工程参数，未经数据拟合或校准。")
        gh, ga = np.indices(matrix.shape)
        wrapper.update(prediction=prediction, exact_point_prior_branch=False,
            diagnostics={"home_marginal": hd, "away_marginal": ad,
                "raw_home_pmf": h.tolist(), "raw_away_pmf": a.tolist(),
                "raw_joint_matrix_mass": mass, "signed_numerical_missing_mass": 1 - mass,
                "actual_joint_grid_tail_upper_bound": tail_bound,
                "quadrature_L1_convergence_indicator": q_indicator,
                "normalization_applied_after_checks": True,
                "analytic_mean_rates": copy.deepcopy(point["expected_goals"]),
                "normalized_matrix_mean_rates": {"home": float((matrix * gh).sum()), "away": float((matrix * ga).sum())},
                "outer_product_justification": "disjoint_independent_latent_variable_sets;not_valid_with_nonzero_correlations"})
    if json.dumps(model, sort_keys=True, separators=(",", ":"), allow_nan=False) != original_model:
        raise UncertaintyError("parent model mutated")
    return wrapper


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inputs", type=Path, required=True)
    parser.add_argument("--parameters", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    source_raw, parameter_raw, runner_raw = args.inputs.read_bytes(), args.parameters.read_bytes(), Path(__file__).read_bytes()
    source, document = json.loads(source_raw), json.loads(parameter_raw)
    if hashlib.sha256(source_raw).hexdigest() != document["source_input_sha256"]:
        raise UncertaintyError("demo source identity differs from preregistered input")
    dependencies = dependency_hashes()
    numerical_closure = source["report_implementation_fingerprint"]
    from football_model.advanced_research import implementation_fingerprint
    approved_closure = implementation_fingerprint()  # Reads only its fixed, approved paths.
    if (numerical_closure.get("files") != approved_closure["files"]
            or numerical_closure.get("sha256") != approved_closure["sha256"]):
        raise UncertaintyError("source does not match the current approved50-file implementation manifest")
    for name, expected in numerical_closure["files"].items():
        if hashlib.sha256((REPO / name).read_bytes()).hexdigest() != expected:
            raise UncertaintyError("existing 50-file numerical closure differs from source")
    run_key = digest({"inputs": hashlib.sha256(source_raw).hexdigest(),
        "parameters": hashlib.sha256(parameter_raw).hexdigest(), "entrypoint": hashlib.sha256(runner_raw).hexdigest(),
        "dependencies": dependencies, "original_numerical_implementation": numerical_closure["sha256"]})
    out = args.output.resolve() / run_key
    out.parent.mkdir(parents=True, exist_ok=True)
    out.mkdir()
    for name, raw in (("entrypoint.py", runner_raw), ("parameters.json", parameter_raw)):
        with (out / name).open("xb") as handle:
            handle.write(raw)
    results = []
    for row in source["records"]:
        if row["horizon"] != document["horizon"]:
            continue
        model = source["parent_fits"][row["parent_fit_key"]]["model"]
        for tau in document["engineering_demo_tau_values"]:
            parameters = {**document["prior_parameters"], "tau": tau}
            try:
                result = predict_uncertainty(model, row["fixture"], parameters, document["numerical_settings"])
                if result["prediction"]["parent_model_sha256"] != row["saved_prediction"]["parent_model_sha256"]:
                    raise UncertaintyError("recomputed parent model differs from saved source hash")
                result.update(match_id=row["fixture"]["match_id"], horizon=row["horizon"], tau=tau,
                    parent_model_sha256=row["saved_prediction"]["parent_model_sha256"],
                    status="calculated")
            except (ValueError, ArithmeticError) as exc:
                result = {"match_id": row["fixture"]["match_id"], "horizon": row["horizon"], "tau": tau,
                    "status": "failed", "error_class": type(exc).__name__, "reason": str(exc)}
            results.append(result)
    if not results:
        raise UncertaintyError("no eligible fixed demo records; cannot report zero-work success")
    if args.inputs.read_bytes() != source_raw or args.parameters.read_bytes() != parameter_raw or Path(__file__).read_bytes() != runner_raw:
        raise UncertaintyError("source, fixed parameters or runner changed during demonstration")
    if dependency_hashes() != dependencies or any(hashlib.sha256((REPO / name).read_bytes()).hexdigest() != expected
            for name, expected in numerical_closure["files"].items()):
        raise UncertaintyError("existing numerical dependency changed during demonstration")
    import scipy
    receipt = {"schema_version": "cold-start-uncertainty-fixed-engineering-demo-v1",
        "created_at": datetime.now(timezone.utc).isoformat(), "run_identity": run_key,
        "input_sha256": hashlib.sha256(source_raw).hexdigest(), "parameters_raw_sha256": hashlib.sha256(parameter_raw).hexdigest(),
        "runner_sha256": hashlib.sha256(runner_raw).hexdigest(), "fixed_document": document,
        "existing_dependency_sha256": dependencies,
        "original_50_file_implementation_sha256": numerical_closure["sha256"], "original_50_files_unchanged": True,
        "requested_records": len(results), "independent_matches": len({r["match_id"] for r in results}),
        "successful_records": sum(r["status"] == "calculated" for r in results),
        "execution_status": "complete" if all(r["status"] == "calculated" for r in results) else "partial",
        "failures": [r for r in results if r["status"] != "calculated"],
        "parameter_fitting_performed": False, "scale_selected_from_results": False,
        "match_outcomes_scored": False, "calibration_verified": False,
        "new_API_requests": 0, "GitHub_workflow_dispatches": 0, "production_changed": False,
        "numeric_environment": {"python": platform.python_version(), "numpy": np.__version__, "scipy": scipy.__version__}}
    with (out / "predictions.json").open("x") as handle:
        handle.write(json.dumps(results, ensure_ascii=False, indent=2) + "\n")
    receipt["predictions_sha256"] = hashlib.sha256((out / "predictions.json").read_bytes()).hexdigest()
    with (out / "receipt.json").open("x") as handle:
        handle.write(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"output": str(out), "receipt": receipt}, ensure_ascii=False))
    return receipt


if __name__ == "__main__":
    receipt = main()
    raise SystemExit(0 if receipt["execution_status"] == "complete" else 2)
