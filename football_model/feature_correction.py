"""Research-only Poisson feature correction with an unchanged log offset.

Inputs are *historical out-of-sample* baseline rates.  Both sides of every
match use one shared coefficient vector and no additional intercept.  This
module checks the declared point-in-time boundaries; the replay builder must
also preserve evidence that the offset really came from the past-only fit.
It neither fetches data nor promotes this candidate to a production model.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
import warnings
from typing import Any

import numpy as np

from .model import (
    RESULT_AVAILABILITY_DELAY, ModelDataError, _distribution_from_rates,
    _integer, _iso, _timestamp,
)


CORRECTION_VERSION = "poisson_fixed_log_offset_l2_shared_v1"
CORRECTION_SCOPE = "fixed_baseline_log_offset_feature_correction"
TRAINING_TARGET = "regulation_time_90_plus_stoppage"
GRADIENT_NORM_TOLERANCE = 2e-5


def _finite(value: Any, field: str, *, positive: bool = False) -> float:
    if isinstance(value, (bool, np.bool_)) or not isinstance(value, (int, float, np.integer, np.floating)):
        raise ModelDataError(f"{field} must be a finite number, not null, bool or text")
    try:
        result = float(value)
    except (OverflowError, ValueError) as exc:
        raise ModelDataError(f"{field} must be representable as a finite number") from exc
    if not math.isfinite(result) or (positive and result <= 0):
        raise ModelDataError(f"{field} must be {'positive and ' if positive else ''}finite")
    return result


def _feature_names(feature_names: Any) -> list[str]:
    if not isinstance(feature_names, (list, tuple)) or not feature_names:
        raise ModelDataError("feature_names must be a nonempty ordered list")
    if any(not isinstance(name, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", name)
           for name in feature_names):
        raise ModelDataError("invalid feature name")
    if len(set(feature_names)) != len(feature_names):
        raise ModelDataError("duplicate feature name")
    if any(name.lower() in {"const", "intercept"} for name in feature_names):
        raise ModelDataError("an extra intercept is not part of this correction version")
    return list(feature_names)


def _features(features: Any, names: list[str], field: str) -> dict[str, float]:
    if not isinstance(features, dict) or set(features) != set(names):
        raise ModelDataError(f"{field} must contain exactly the declared feature columns")
    return {name: _finite(features[name], f"{field}.{name}") for name in names}


def _digest(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=False,
                         separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _training_rows(rows: Any, names: list[str], fit_cutoff: Any) -> list[dict[str, Any]]:
    if not isinstance(rows, (list, tuple)) or not rows:
        raise ModelDataError("rows must be a nonempty list of paired side observations")
    required = {"match_id", "side", "kickoff", "cutoff_at", "target_available_at",
                "goals", "lambda0", "features", "feature_available_at", "baseline_training_cutoff"}
    cleaned = []
    seen = set()
    pairs: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        if not isinstance(row, dict) or required - set(row):
            missing = sorted(required - set(row)) if isinstance(row, dict) else sorted(required)
            raise ModelDataError("correction row missing fields: " + ",".join(missing))
        match_id = _integer(row["match_id"], "match_id", 1)
        side = row["side"]
        if not isinstance(side, str) or side not in {"home", "away"}:
            raise ModelDataError("side must be home or away")
        if (match_id, side) in seen:
            raise ModelDataError("duplicate match side; versions are not independent observations")
        seen.add((match_id, side))
        times = {name: _timestamp(row[name], name) for name in
                 ("kickoff", "cutoff_at", "target_available_at", "baseline_training_cutoff")}
        if times["baseline_training_cutoff"] > times["cutoff_at"]:
            raise ModelDataError("baseline trained after prediction cutoff: offset leakage")
        if times["cutoff_at"] >= times["kickoff"]:
            raise ModelDataError("historical prediction cutoff must be before kickoff")
        if times["target_available_at"] <= times["kickoff"]:
            raise ModelDataError("target cannot be available at or before kickoff")
        if times["target_available_at"] < times["kickoff"] + RESULT_AVAILABILITY_DELAY:
            raise ModelDataError("target must respect the baseline four-hour result delay")
        if times["target_available_at"] > fit_cutoff:
            raise ModelDataError("target not available by correction training cutoff")
        features = _features(row["features"], names, "features")
        available = row["feature_available_at"]
        if not isinstance(available, dict) or set(available) != set(names):
            raise ModelDataError("feature_available_at must cover exactly the feature columns")
        feature_times = {}
        for name in names:
            observed = _timestamp(available[name], "feature_available_at." + name)
            if observed > times["cutoff_at"]:
                raise ModelDataError(f"feature {name} not available at prediction cutoff")
            feature_times[name] = _iso(observed)
        # An optional source ID list is an additional guard, not a replacement
        # for the frozen baseline fit and its full provenance in the replay.
        if "baseline_training_match_ids" in row:
            ids = row["baseline_training_match_ids"]
            if not isinstance(ids, list) or not ids:
                raise ModelDataError("baseline_training_match_ids must be a nonempty list")
            checked = [_integer(value, "baseline_training_match_ids", 1) for value in ids]
            if len(set(checked)) != len(checked) or match_id in checked:
                raise ModelDataError("baseline training IDs duplicate or contain target match")
        goals = _integer(row["goals"], "goals", 0)
        _finite(goals, "goals")
        normalized = {"match_id": match_id, "side": side,
                      **{name: _iso(value) for name, value in times.items()},
                      "goals": goals,
                      "lambda0": _finite(row["lambda0"], "lambda0", positive=True),
                      "features": features, "feature_available_at": feature_times}
        # Preserve extra provenance without allowing it into the design matrix.
        for name in ("baseline_training_data_sha256", "baseline_source_sha256", "time_semantics"):
            if name in row:
                if not isinstance(row[name], str) or not row[name]:
                    raise ModelDataError(f"{name} must be nonempty text")
                normalized[name] = row[name]
        if "baseline_training_match_ids" in row:
            normalized["baseline_training_match_ids"] = checked
        cleaned.append(normalized)
        pairs.setdefault(match_id, []).append(normalized)
    for pair in pairs.values():
        if len(pair) != 2 or {row["side"] for row in pair} != {"home", "away"}:
            raise ModelDataError("every match must have exactly one home and one away row")
        for field in ("kickoff", "cutoff_at", "target_available_at", "baseline_training_cutoff"):
            if pair[0][field] != pair[1][field]:
                raise ModelDataError("paired sides disagree on " + field)
    return sorted(cleaned, key=lambda row: (row["kickoff"], row["match_id"], row["side"]))


def fit_correction(
    rows: list[dict[str, Any]],
    feature_names: list[str],
    *,
    alpha: float,
    cutoff: str,
    min_rows: int = 80,
    half_life_days: float = 180.0,
) -> dict[str, Any]:
    """Fit an L2 Poisson GLM against historical out-of-sample ``lambda0``.

    Each row requires a ``feature_available_at`` timestamp per feature.  All
    supplied rows must be eligible; late targets or features are errors, never
    silently dropped. Missing feature values are errors and belong in coverage
    records in the caller. ``min_rows`` counts side rows (two per match).

    The loss is mean(normalized_weight * Poisson negative log likelihood)
    + 0.5 * alpha * sum(beta**2). Time weights halve every half_life_days,
    measured from kickoff to fit cutoff and normalized to mean one. Weighted
    training-only means/scales are saved. This alpha is not baseline ridge=3.
    """
    names = _feature_names(feature_names)
    fit_cutoff = _timestamp(cutoff, "cutoff")
    penalty = _finite(alpha, "alpha", positive=True)
    half_life = _finite(half_life_days, "half_life_days", positive=True)
    minimum = _integer(min_rows, "min_rows", 4)
    records = _training_rows(rows, names, fit_cutoff)
    if len(records) < minimum:
        raise ModelDataError(f"insufficient correction rows: {len(records)} < {minimum}")
    ages = np.array([(fit_cutoff - _timestamp(row["kickoff"], "kickoff")).total_seconds() / 86400
                     for row in records], dtype=float)
    raw_weights = np.exp(-math.log(2) * ages / half_life)
    if not np.all(np.isfinite(raw_weights)) or float(raw_weights.sum()) < 1e-8:
        raise ModelDataError("correction time weights have negligible or invalid mass")
    weights = raw_weights * (len(records) / float(raw_weights.sum()))
    x = np.array([[row["features"][name] for name in names] for row in records], dtype=float)
    means = np.average(x, axis=0, weights=weights)
    with np.errstate(over="raise", invalid="raise", divide="raise"):
        try:
            variances = np.average((x - means) ** 2, axis=0, weights=weights)
            scales = np.sqrt(variances)
            constants = np.array([bool(np.ptp(x[:, index]) == 0) for index in range(len(names))])
            if np.any((scales == 0) & ~constants):
                raise ModelDataError("feature variance underflow; cannot standardize reliably")
            scales[constants] = 1.0
            design = (x - means) / scales
            design[:, constants] = 0.0
        except FloatingPointError as exc:
            raise ModelDataError("nonfinite or unstable feature standardization") from exc
    if not all(np.all(np.isfinite(value)) for value in (means, scales, design)):
        raise ModelDataError("nonfinite feature standardization")
    goals = np.array([row["goals"] for row in records], dtype=float)
    offset = np.log(np.array([row["lambda0"] for row in records], dtype=float))
    try:
        import scipy
        import statsmodels
        from statsmodels.genmod.families import Poisson
        from statsmodels.genmod.generalized_linear_model import GLM
        from statsmodels.tools.sm_exceptions import ConvergenceWarning
    except ImportError as exc:
        raise ModelDataError("statsmodels Poisson correction dependencies unavailable") from exc
    glm = GLM(goals, design, family=Poisson(), offset=offset, freq_weights=weights)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        try:
            result = glm.fit_regularized(alpha=penalty, L1_wt=0.0, refit=False,
                                         start_params=np.zeros(len(names)), opt_method="bfgs")
        except (ValueError, FloatingPointError, np.linalg.LinAlgError) as exc:
            raise ModelDataError("correction optimizer failed") from exc
    if any(issubclass(item.category, (ConvergenceWarning, RuntimeWarning)) for item in caught):
        raise ModelDataError("correction optimizer reported nonconvergence or numerical instability")
    coefficients = np.asarray(result.params, dtype=float)
    if coefficients.shape != (len(names),) or not np.all(np.isfinite(coefficients)):
        raise ModelDataError("correction optimizer returned invalid coefficients")
    gradient = -np.asarray(glm.score(coefficients), dtype=float) / len(records) + penalty * coefficients
    gradient_norm = float(np.linalg.norm(gradient))
    objective = float(-glm.loglike(coefficients) / len(records)
                      + 0.5 * penalty * np.dot(coefficients, coefficients))
    if not math.isfinite(objective) or not math.isfinite(gradient_norm) or gradient_norm > GRADIENT_NORM_TOLERANCE:
        raise ModelDataError("correction optimizer failed gradient convergence check")
    model = {
        "model_version": CORRECTION_VERSION, "model_scope": CORRECTION_SCOPE,
        "purpose": "candidate_research_only", "target": TRAINING_TARGET,
        "fit_status": "converged", "trained": True, "validated_for_production": False,
        "automatic_promotion": False, "training_cutoff": _iso(fit_cutoff),
        "feature_names": names, "coefficients": dict(zip(names, map(float, coefficients))),
        "feature_scaling": {name: {"mean": float(means[index]), "scale": float(scales[index]),
                                    "constant_in_training": bool(constants[index])}
                            for index, name in enumerate(names)},
        "intercept_added": False, "shared_home_away_coefficients": True,
        "offset_definition": "log(out_of_sample_baseline_lambda), coefficient fixed to 1",
        "alpha": penalty, "L1_wt": 0.0, "refit": False,
        "loss_definition": "mean(mean_one_time_weight * poisson_nll) + 0.5 * alpha * sum(beta**2)",
        "time_weighting": {"half_life_days": half_life, "origin": "kickoff",
                           "reference": "correction_training_cutoff", "normalization": "mean_one",
                           "raw_weight_sum": float(raw_weights.sum()), "normalized_weight_sum": float(weights.sum())},
        "n_rows": len(records), "n_matches": len(records) // 2,
        "min_rows": minimum, "training_match_ids": sorted({row["match_id"] for row in records}),
        "training_data_sha256": _digest(records),
        "last_result_available_at": max(row["target_available_at"] for row in records),
        "effective_side_sample_size": float(weights.sum() ** 2 / np.dot(weights, weights)),
        "effective_match_sample_size": float(weights.sum() ** 2 / np.dot(weights, weights) / 2),
        "independent_evaluation_unit": "match_id, never a side row or repeated forecast version",
        "optimizer": {"method": "statsmodels_GLM_fit_regularized_bfgs", "gradient_norm": gradient_norm,
                      "gradient_norm_tolerance": GRADIENT_NORM_TOLERANCE, "objective": objective},
        "numeric_environment": {"numpy": np.__version__, "scipy": scipy.__version__,
                                "statsmodels": statsmodels.__version__},
        "point_in_time_validation": "declared_boundaries_checked; frozen_baseline_and_feature_evidence_required_upstream",
        "warnings": ["A fitted correction is not evidence of predictive or betting improvement.",
                     "Target regulation-time final-status verification belongs to the replay builder.",
                     "Recorded historical availability assumptions are not observed live publication times."],
    }
    model["correction_model_sha256"] = _digest(model)
    return model


def _validated_model(model: Any) -> tuple[list[str], dict[str, float], dict[str, Any]]:
    if not isinstance(model, dict) or model.get("model_version") != CORRECTION_VERSION or model.get("fit_status") != "converged":
        raise ModelDataError("incompatible or unfitted correction model")
    if model.get("intercept_added") is not False or model.get("shared_home_away_coefficients") is not True:
        raise ModelDataError("correction requires no added intercept and shared coefficients")
    names = _feature_names(model.get("feature_names"))
    coefficients = _features(model.get("coefficients"), names, "coefficients")
    scaling = model.get("feature_scaling")
    if not isinstance(scaling, dict) or set(scaling) != set(names):
        raise ModelDataError("invalid feature_scaling columns")
    for name in names:
        if not isinstance(scaling[name], dict):
            raise ModelDataError("invalid feature_scaling row")
        _finite(scaling[name].get("mean"), name + ".mean")
        _finite(scaling[name].get("scale"), name + ".scale", positive=True)
        if not isinstance(scaling[name].get("constant_in_training"), bool):
            raise ModelDataError("constant_in_training must be bool")
        if scaling[name]["constant_in_training"] and coefficients[name] != 0:
            raise ModelDataError("constant training feature cannot have nonzero coefficient")
    _timestamp(model.get("training_cutoff"), "correction.training_cutoff")
    if model.get("correction_model_sha256") != _digest({key: value for key, value in model.items()
                                                      if key != "correction_model_sha256"}):
        raise ModelDataError("correction model fingerprint mismatch")
    return names, coefficients, scaling


def apply_correction(model: dict[str, Any], lambda0: float, features: dict[str, float]) -> dict[str, Any]:
    """Apply saved train-only scaling and expose every additive log contribution.

    This arithmetic function has no forecast-time eligibility of its own. Use
    ``predict_correction`` plus frozen feature provenance for dated predictions.
    A zero total correction returns the original floating-point rate exactly.
    """
    names, coefficients, scaling = _validated_model(model)
    baseline = _finite(lambda0, "lambda0", positive=True)
    values = _features(features, names, "features")
    standardized, contributions = {}, {}
    for name in names:
        factor = scaling[name]
        value = (values[name] - factor["mean"]) / factor["scale"]
        if not math.isfinite(value):
            raise ModelDataError("nonfinite standardized prediction feature")
        contribution = value * coefficients[name]
        if not math.isfinite(contribution):
            raise ModelDataError("nonfinite log correction contribution")
        standardized[name] = value
        contributions[name] = contribution
    try:
        total = math.fsum(contributions.values())
        corrected = baseline if total == 0 else math.exp(math.log(baseline) + total)
    except (OverflowError, ValueError) as exc:
        raise ModelDataError("nonfinite corrected expected goals") from exc
    if not math.isfinite(corrected) or corrected <= 0:
        raise ModelDataError("corrected expected goals must be positive and finite")
    multiplier = corrected / baseline
    if not math.isfinite(multiplier) or multiplier <= 0:
        raise ModelDataError("correction multiplier must be positive and finite")
    return {"lambda": corrected, "lambda0": baseline, "features": values,
            "standardized_features": standardized, "log_contributions": contributions,
            "total_log_correction": total, "correction_multiplier": multiplier}


def predict_correction(
    model: dict[str, Any],
    baseline_prediction: dict[str, Any],
    home_features: dict[str, float],
    away_features: dict[str, float],
) -> dict[str, Any]:
    """Produce a candidate using the existing independent Poisson grid builder.

    The baseline must contain a real full distribution and ``cutoff_at``.
    Training and baseline cutoffs must precede that forecast cutoff and kickoff.
    Feature availability must be validated upstream against the same cutoff.
    """
    _validated_model(model)
    if not isinstance(baseline_prediction, dict):
        raise ModelDataError("baseline_prediction must be a distribution dictionary")
    kickoff = _timestamp(baseline_prediction.get("kickoff"), "prediction.kickoff")
    cutoff = _timestamp(baseline_prediction.get("cutoff_at"), "prediction.cutoff_at")
    trained = _timestamp(model["training_cutoff"], "correction.training_cutoff")
    baseline_cutoff = _timestamp(baseline_prediction.get("training_cutoff"), "baseline.training_cutoff")
    if cutoff >= kickoff or trained > cutoff or baseline_cutoff > cutoff:
        raise ModelDataError("prediction has future training or a non-prematch cutoff")
    if "match_id" in baseline_prediction:
        match_id = _integer(baseline_prediction["match_id"], "prediction.match_id", 1)
        if match_id in model.get("training_match_ids", []):
            raise ModelDataError("prediction target match was used to train the correction")
    expected = baseline_prediction.get("expected_goals")
    if not isinstance(expected, dict) or set(expected) != {"home", "away"}:
        raise ModelDataError("invalid baseline expected_goals")
    rates = {side: _finite(expected[side], "expected_goals." + side, positive=True)
             for side in ("home", "away")}
    original = _distribution_from_rates(rates["home"], rates["away"])
    for field in ("score_matrix", "probabilities", "score_matrix_axes"):
        if field not in baseline_prediction:
            raise ModelDataError("baseline distribution missing " + field)
    try:
        matrix = np.asarray(baseline_prediction["score_matrix"], dtype=float)
    except (ValueError, TypeError) as exc:
        raise ModelDataError("invalid baseline score matrix") from exc
    recomputed = np.asarray(original["score_matrix"])
    if (matrix.shape != recomputed.shape or not np.all(np.isfinite(matrix))
            or not np.all(np.abs(matrix - recomputed) <= 1e-15)
            or baseline_prediction["score_matrix_axes"] != original["score_matrix_axes"]):
        raise ModelDataError("baseline is not the unchanged independent Poisson distribution")
    probabilities = baseline_prediction["probabilities"]
    if not isinstance(probabilities, dict) or set(probabilities) != set(original["probabilities"]):
        raise ModelDataError("invalid baseline probability columns")
    for key, value in original["probabilities"].items():
        if abs(_finite(probabilities[key], "probabilities." + key) - value) > 1e-15:
            raise ModelDataError("baseline probabilities disagree with score matrix")
    home = apply_correction(model, rates["home"], home_features)
    away = apply_correction(model, rates["away"], away_features)
    prediction = _distribution_from_rates(home["lambda"], away["lambda"])
    for key in ("match_id", "competition", "home_id", "away_id", "home_name", "away_name", "neutral", "time_semantics"):
        if key in baseline_prediction:
            prediction[key] = copy.deepcopy(baseline_prediction[key])
    prediction.update({"kickoff": _iso(kickoff), "cutoff_at": _iso(cutoff),
                       "model_version": CORRECTION_VERSION, "model_scope": CORRECTION_SCOPE,
                       "training_cutoff": model["training_cutoff"], "target": TRAINING_TARGET,
                       "purpose": "candidate_research_only", "validated_for_production": False,
                       "automatic_promotion": False, "lineup_adjusted": False,
                       "features_adjusted": True,
                       "correction_model_sha256": model["correction_model_sha256"],
                       "correction": {"home": home, "away": away},
                       "baseline_prediction": copy.deepcopy(baseline_prediction),
                       "warnings": ["Research candidate; no accuracy or return improvement is established.",
                                    "Prediction feature timestamps and frozen evidence require upstream validation."]})
    return prediction
