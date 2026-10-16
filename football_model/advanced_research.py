"""Independent multi-league opportunity/personnel research and saved-fit forecasts.

Existing production, v1/v2 experiments and F5 records are never mutated. Every
requested target remains visible. Missing feature families reject that branch;
they are not made into zeros. Historical backfills remain development evidence.
"""
from __future__ import annotations

import argparse
import copy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
from pathlib import Path

import numpy as np
from scipy.stats import poisson

from .feature_correction import fit_correction, predict_correction
from .feature_dataset import FEATURE_NAMES, build_replay, feature_values
from .feature_research import (choose_alpha, digest, save_immutable, score_forecast,
                               paired_summary, _request_summary)
from .model import ModelDataError, RESULT_AVAILABILITY_DELAY, TAIL_TOLERANCE, _iso, _timestamp
from .advanced_storage import save_artifact, load_artifact, load_report
from .goalmodel_adapter import goalmodel_probabilities
from .history_archive import read_bytes, exists


FEATURE_GROUPS = {
    "schedule": list(FEATURE_NAMES),
    "opportunity_shots": ["shots_for_mean", "shots_on_target_for_mean",
        "opponent_shots_against_mean", "opponent_shots_on_target_against_mean"],
    "opportunity_xg": ["xg_for_mean", "opponent_xg_against_mean"],
    "lineup_early": ["prior_xi_continuity", "opponent_prior_xi_continuity"],
    "lineup_confirmed": ["confirmed_xi_overlap", "opponent_confirmed_xi_overlap"],
    "lineup_minutes": ["confirmed_xi_prior_minutes_share", "opponent_confirmed_xi_prior_minutes_share"],
    "lineup_rating": ["confirmed_xi_prior_rating_delta", "opponent_confirmed_xi_prior_rating_delta"],
    "personnel_early": ["reported_absence_recent_minutes_share", "reported_absence_previous_xi_share",
        "opponent_reported_absence_recent_minutes_share", "opponent_reported_absence_previous_xi_share"],
    "restricted_observed_national_load": ["restricted_observed_national_minutes_per_matched_xi",
        "restricted_observed_national_xi_match_count", "restricted_observed_national_xi_match_fraction",
        "opponent_restricted_observed_national_minutes_per_matched_xi",
        "opponent_restricted_observed_national_xi_match_count", "opponent_restricted_observed_national_xi_match_fraction"],
}


def implementation_fingerprint():
    root = Path(__file__).resolve().parents[1]
    # Freeze the complete local execution dependency closure, not just fitted
    # coefficient arithmetic. Admission, first-opportunity enrollment, fallback,
    # result normalization and durable-save verification can all change which
    # observations enter the same named trial. Their changes require fresh
    # development, or a new protocol if confirmation has already started.
    # Keep documentation/tests outside this identity. The dependency-closure
    # regression requires newly imported local modules to be added here too.
    modules = (
        "__init__", "advanced_capacity", "advanced_confirmation", "advanced_durability",
        "advanced_features", "advanced_personnel", "advanced_player_load", "advanced_research", "advanced_scoring",
        "advanced_snapshots", "advanced_storage", "cli", "cold_start",
        "coverage_audit", "data", "decision_audit", "decision_quotes", "decision_record",
        "evidence", "f5c_access",
        "feature_correction", "feature_dataset", "feature_decisions",
        "feature_research", "feature_schedule", "feature_scoring",
        "feature_snapshots", "fundamentals", "goalmodel_adapter", "integration",
        "history_archive", "research_checkpoints", "match_locations", "model", "nations", "production_replay", "prospective", "quality",
        "settlement", "shadow_grading", "shadow_models", "state_storage", "storage_policy",
        "total_shrink_shadow", "venue_registry",
    )
    paths = tuple("football_model/" + name + ".py" for name in modules) + (
        "requirements.txt", "scripts/goalmodel_bridge.R", "scripts/cloud_state.py",
        "third_party/goalmodel/R/goalmodel_fit.R", "third_party/goalmodel/R/goalmodel_predict.R")
    files = {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in paths}
    return {"schema_version": "advanced-implementation-fingerprint-v1", "files": files,
            "sha256": digest(files)}


def _read(path):
    return json.loads(read_bytes(path).decode("utf-8"))


def _write(path, value, guard=None, *, final=False):
    # The writer and the admission guard must address the same file. The guard
    # interprets relative paths from its runtime, whereas this writer uses cwd.
    path = Path(path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    raw = (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode()
    ticket = guard.check_write(path, raw, final=final) if guard is not None else None
    temporary.write_bytes(raw)
    temporary.replace(path)
    if ticket is not None:
        guard.commit_write(ticket)


def _ref(root, path):
    return {"path": str(Path(path).resolve().relative_to(Path(root).resolve())),
            "sha256": hashlib.sha256(read_bytes(path)).hexdigest()}


def _resolve(root, ref):
    root = Path(root).resolve()
    path = (root / ref["path"]).resolve()
    if not path.is_relative_to(root) or hashlib.sha256(read_bytes(path)).hexdigest() != ref["sha256"]:
        raise ModelDataError("advanced source path/fingerprint mismatch")
    return path


def _freeze(root, path, guard=None):
    root = Path(root).resolve()
    path = Path(path).resolve()
    raw = read_bytes(path)
    key = hashlib.sha256(raw).hexdigest()
    destination = root / "advanced_models" / "inputs" / (key + ".json")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if exists(destination):
        if read_bytes(destination) != raw:
            raise ModelDataError("advanced immutable source collision")
    else:
        ticket = guard.check_write(destination, raw) if guard is not None else None
        with destination.open("xb") as handle:
            handle.write(raw)
        if ticket is not None:
            guard.commit_write(ticket)
    return {**_ref(root, destination), "original_path": str(Path(path).relative_to(root))}


def _rebind_frozen_refs(value, lookup):
    """Use immutable copies inside training bundles too, not mutable old paths."""
    if isinstance(value, dict):
        key = (value.get("path"), value.get("sha256"))
        if key in lookup:
            return {**value, **lookup[key]}
        return {k: _rebind_frozen_refs(v, lookup) for k, v in value.items()}
    if isinstance(value, list):
        return [_rebind_frozen_refs(v, lookup) for v in value]
    return value


def validate_protocol(protocol):
    if protocol.get("schema_version") != "advanced-model-protocol-v1":
        raise ModelDataError("unsupported advanced protocol")
    if protocol.get("confirmation_active") or protocol.get("production_promotion"):
        raise ModelDataError("research protocol cannot activate confirmation or production")
    if not protocol.get("candidates"):
        raise ModelDataError("advanced candidates missing")
    for name, spec in protocol["candidates"].items():
        if not name.replace("_", "").isalnum() or spec.get("horizon") not in ("EARLY", "LINEUP"):
            raise ModelDataError("invalid advanced candidate identity/horizon")
        groups = spec.get("groups", [])
        if not groups or len(groups) != len(set(groups)) or set(groups) - set(FEATURE_GROUPS):
            raise ModelDataError("unknown/duplicate feature family")
        if {"lineup_confirmed", "lineup_minutes", "lineup_rating"} & set(groups) and spec["horizon"] != "LINEUP":
            raise ModelDataError("confirmed lineup cannot enter EARLY model")
        if "personnel_early" in groups and spec["horizon"] != "EARLY":
            raise ModelDataError("reported absence cannot duplicate confirmed lineup adjustment")
    if any("restricted_observed_national_load" in spec["groups"] for spec in protocol["candidates"].values()):
        if (protocol.get("national_load_scope") != "uefa_nations_league_only"
                or type(protocol.get("national_load_lookback_days")) is not int
                or not 1 <= protocol["national_load_lookback_days"] <= 30):
            raise ModelDataError("restricted national workload scope or window invalid")
    return protocol


def candidate_feature_names(protocol, candidate):
    if candidate in protocol["candidates"]:
        groups = protocol["candidates"][candidate]["groups"]
        return [name for group in groups for name in FEATURE_GROUPS[group]]
    if candidate == "G1":
        return candidate_feature_names(protocol, protocol["goalmodel_feature_candidate"])
    if candidate in ("G0", "G0_full"):
        return []
    raise ModelDataError("unknown advanced candidate")


def _complete(features, names):
    return isinstance(features, dict) and all(type(features.get(name)) in (int, float)
        and math.isfinite(features[name]) for name in names)


def _side_values(features, names):
    if not all(_complete(features.get(side), names) for side in ("home", "away")):
        raise ModelDataError("incomplete required feature family")
    return {side: {name: features[side][name] for name in names} for side in ("home", "away")}


def _correction_rows(replay, features, names):
    rows = []
    fits = {f["block"]: f for f in replay["baseline_fits"]}
    for forecast in replay["forecasts"]:
        values = features.get(forecast["match_id"], {})
        if not all(_complete(values.get(side), names) for side in ("home", "away")):
            continue
        for side in ("home", "away"):
            rows.append({"match_id": forecast["match_id"], "side": side,
                "kickoff": forecast["kickoff"], "cutoff_at": forecast["cutoff_at"],
                "target_available_at": forecast["target_available_at"],
                "baseline_training_cutoff": forecast["baseline_training_cutoff"],
                "baseline_training_match_ids": fits[forecast["block"]]["training_match_ids"],
                "baseline_training_data_sha256": digest(fits[forecast["block"]]),
                "goals": forecast[side + "_goals"],
                "lambda0": forecast["baseline_prediction"]["expected_goals"][side],
                "features": {name: values[side][name] for name in names},
                "feature_available_at": {name: forecast["cutoff_at"] for name in names},
                "time_semantics": "historical_feature_availability_assumption_not_live_PIT"})
    return rows


def _fit_offset(rows, names, cutoff, protocol):
    past = [r for r in rows if _timestamp(r["target_available_at"], "available") <= _timestamp(cutoff, "cutoff")]
    options = {**protocol, "feature_names": names}
    alpha, selection = choose_alpha(past, options, cutoff)
    model = fit_correction(past, names, alpha=alpha, cutoff=cutoff,
        min_rows=2 * protocol["correction_min_matches"], half_life_days=protocol["baseline_half_life_days"])
    return model, selection, past


def _failure(exc, stage):
    return {"stage": stage, "code": type(exc).__name__, "detail": str(exc)}


def _goal_pool(matches, features, names, cutoff):
    mature = [m for m in matches if _timestamp(m["kickoff"], "kickoff") + RESULT_AVAILABILITY_DELAY
              <= _timestamp(cutoff, "cutoff")]
    return [m for m in mature if all(_complete(features.get(m["match_id"], {}).get(side), names)
                                    for side in ("home", "away"))] if names else mature


def predict_saved_goalmodel(card, fixture, features, as_of):
    """Reproduce pinned goalmodel::lambda_pred from the actual saved R fit.

    This is restricted to the bridge's independent Poisson model with its
    explicit neutral-aware home column. It is not a replacement goalmodel fit.
    Every published card must pass real R output probes before it is saved.
    """
    if card.get("kind") != "goalmodel_saved_fit" or card.get("fit_status") != "fitted":
        raise ModelDataError("not a saved converged goalmodel fit")
    if card.get("dc") is not False:
        raise ModelDataError("saved prediction currently requires independent Poisson goalmodel")
    cutoff = _timestamp(as_of, "as_of")
    if _timestamp(card["training_cutoff"], "training_cutoff") > cutoff or cutoff >= _timestamp(fixture["kickoff"], "kickoff"):
        raise ModelDataError("goalmodel forecast temporal mismatch")
    if fixture["match_id"] in card["training_match_ids"]:
        raise ModelDataError("goalmodel target is in training")
    names = card["feature_names"]
    values = _side_values(features, names)
    params = card["goalmodel_fit"]["parameters"]
    if set(params) != {"attack", "defense", "intercept", "beta"}:
        raise ModelDataError("unsupported saved goalmodel parameter structure")
    beta = params["beta"]
    if set(beta) != {"__home_field", *names}:
        raise ModelDataError("goalmodel saved coefficient columns mismatch")
    home, away = str(fixture["home_id"]), str(fixture["away_id"])
    if any(team not in params[key] for team in (home, away) for key in ("attack", "defense")):
        raise ModelDataError("unsupported_team")
    rates, contributions = {}, {}
    for side, own, opponent in (("home", home, away), ("away", away, home)):
        contribution = {name: values[side][name] * beta[name] for name in names}
        contribution["__home_field"] = beta["__home_field"] if side == "home" and not fixture.get("neutral", False) else 0.0
        eta = params["intercept"] + params["attack"][own] - params["defense"][opponent]
        eta += math.fsum(contribution.values())
        rates[side] = math.exp(eta)
        contributions[side] = contribution
    lh, la = rates["home"], rates["away"]
    if not all(math.isfinite(v) and v > 0 for v in rates.values()):
        raise ModelDataError("invalid saved goalmodel rate")
    limit = max(2, int(poisson.ppf(1 - TAIL_TOLERANCE / 4, lh)), int(poisson.ppf(1 - TAIL_TOLERANCE / 4, la)))
    if limit > 127:
        raise ModelDataError("unbounded saved goalmodel matrix")
    raw = np.outer(poisson.pmf(np.arange(limit + 1), lh), poisson.pmf(np.arange(limit + 1), la))
    mass = float(raw.sum())
    if not math.isfinite(mass) or 1 - mass > TAIL_TOLERANCE:
        raise ModelDataError("saved goalmodel tail exceeded")
    matrix = raw / mass
    return {**copy.deepcopy(fixture), "cutoff_at": _iso(cutoff), "expected_goals": rates,
        "score_matrix": matrix.tolist(),
        "score_matrix_axes": {"rows": "home_goals_from_0", "columns": "away_goals_from_0"},
        "probabilities": goalmodel_probabilities(matrix),
        "tail_mass_before_normalization": max(0.0, 1 - mass), "tail_tolerance": TAIL_TOLERANCE,
        "training_cutoff": card["training_cutoff"], "model_scope": "independent_goalmodel_research_only",
        "model_version": "pinned_goalmodel_saved_fit_prediction_v3", "log_feature_contributions": contributions,
        "validated_for_production": False, "automatic_promotion": False}


def verify_goalmodel_probes(card, result, prediction_features):
    differences = []
    for forecast in result.get("forecasts", []):
        fixture = {k: forecast[k] for k in ("match_id", "home_id", "away_id", "kickoff", "neutral")}
        values = prediction_features.get(forecast["match_id"], {"home": {}, "away": {}})
        actual = predict_saved_goalmodel(card, fixture, values, card["training_cutoff"])
        for side in ("home", "away"):
            differences.append(abs(actual["expected_goals"][side] - forecast["expected_goals"][side]))
        a, b = np.asarray(actual["score_matrix"]), np.asarray(forecast["score_matrix"])
        if a.shape != b.shape:
            raise ModelDataError("saved goalmodel grid differs from actual R probe")
        differences.append(float(np.max(np.abs(a - b))))
    if not differences or max(differences) > 1e-10:
        raise ModelDataError("saved goalmodel failed real R reproduction probes")
    return {"verified": True, "probe_count": len(result["forecasts"]), "absolute_tolerance": 1e-10,
            "maximum_absolute_difference": max(differences), "source": "actual_R_bridge_forecasts"}


def _validate_card_identity(card, candidate, league, protocol):
    if card.get("protocol_hash") != digest(protocol) or card.get("candidate_id") != candidate or card.get("league") != league:
        raise ModelDataError("advanced model-card identity mismatch")
    if card.get("model_hash") != digest({k: v for k, v in card.items() if k != "model_hash"}):
        raise ModelDataError("advanced model-card payload mismatch")


def load_cards(runtime, league, protocol):
    validate_protocol(protocol)
    root = Path(runtime).resolve()
    directory = root / "advanced_models" / league / digest(protocol) / "models"
    cards = {}
    for candidate in list(protocol["candidates"]) + protocol.get("goalmodel_candidates", []):
        path = directory / candidate / "latest.json"
        if path.exists():
            card = _read(path)
            _validate_card_identity(card, candidate, league, protocol)
            cards[candidate] = card
    return cards


def build_live_predictions(runtime, league, protocol, baseline_prediction, features, as_of,
                           fixture=None, run_goalmodel=True):
    """Consume saved fits only; source/cutoff and feature admission are explicit.

    The snapshot caller must independently rebuild baseline and feature inputs
    from frozen raw evidence. This function checks every model source hash.
    """
    root = Path(runtime).resolve()
    validate_protocol(protocol)
    predictions, failures, refs, used_cards = {}, {}, {}, {}
    fixture = fixture or {**{k: baseline_prediction[k] for k in ("match_id", "home_id", "away_id", "kickoff")},
                          "neutral": baseline_prediction.get("neutral", False)}
    cutoff = _timestamp(as_of, "as_of")
    horizon = features.get("horizon", "EARLY")
    for candidate in list(protocol["candidates"]) + protocol.get("goalmodel_candidates", []):
        wanted = protocol["candidates"].get(candidate, {"horizon": "EARLY"})["horizon"]
        if wanted != horizon and not (candidate.startswith("G") and horizon in protocol.get("goalmodel_horizons", ["EARLY"])):
            continue
        try:
            model_path = root / "advanced_models" / league / digest(protocol) / "models" / candidate / "latest.json"
            if not model_path.exists():
                raise ModelDataError("no_successful_model_card")
            card = _read(model_path)
            _validate_card_identity(card, candidate, league, protocol)
            if card.get("implementation_fingerprint") != implementation_fingerprint():
                raise ModelDataError("candidate implementation differs from saved fit")
            if candidate.startswith("G") and not run_goalmodel:
                raise ModelDataError("goalmodel_disabled")
            if _timestamp(card["generated_at"], "generated_at") > cutoff:
                raise ModelDataError("model_card_created_after_forecast_cutoff")
            if cutoff - _timestamp(card["training_cutoff"], "training_cutoff") > timedelta(days=protocol.get("max_model_age_days", 14)):
                raise ModelDataError("model_card_stale")
            for ref in card["source_refs"]:
                _resolve(root, ref)
                refs[ref["path"]] = ref
            used_cards[candidate] = _ref(root, model_path)
            names = card["feature_names"]
            values = _side_values(features, names)
            # G0 shares G1's exact feature-complete prediction pool.
            if candidate == "G0":
                _side_values(features, candidate_feature_names(protocol, "G1"))
            if card["kind"] == "offset_correction":
                if baseline_prediction is None:
                    raise ModelDataError("baseline_unavailable")
                bundle = load_artifact(root, card["correction_training_ref"])
                if bundle.get("correction_model") != card["correction_model"]:
                    raise ModelDataError("correction card differs from frozen fitted model")
                baseline = {**copy.deepcopy(baseline_prediction), "cutoff_at": _iso(cutoff)}
                prediction = predict_correction(card["correction_model"], baseline, values["home"], values["away"])
                prediction["lineup_adjusted"] = any("lineup" in g for g in card["feature_groups"])
            else:
                if card.get("reproduction_verification", {}).get("verified") is not True:
                    raise ModelDataError("saved_goalmodel_not_R_verified")
                bundle = load_artifact(root, card["goalmodel_training_ref"])
                original = bundle["real_R_result"]
                if (card["goalmodel_fit"] != original["fit"] or card["training_match_ids"] != original["training_match_ids"]
                        or card["training_cutoff"] != original["cutoff"] or card["feature_names"] != original["feature_names"]):
                    raise ModelDataError("goalmodel card differs from frozen actual R output")
                verify_goalmodel_probes(card, original, {int(k): v for k, v in bundle["probe_features"].items()})
                prediction = predict_saved_goalmodel(card, fixture, values, as_of)
            prediction.update(candidate_id=candidate, model_card_hash=card["model_hash"], horizon=horizon)
            predictions[candidate] = prediction
        except (OSError, ValueError, ArithmeticError, KeyError, TypeError) as exc:
            failures[candidate] = _failure(exc, "live_candidate_prediction")
    return {"predictions": predictions, "failures": failures, "source_refs": list(refs.values()),
            "cards": used_cards, "generated_at": _iso(datetime.now(timezone.utc)),
            "production_changed": False, "predictive_improvement_verified": False}


def study(runtime, league, protocol, *, source_commit, state_commit=None, run_goalmodel=False, clock=None, capacity_guard=None):
    validate_protocol(protocol)
    from .advanced_features import build_feature_rows
    from .goalmodel_adapter import fit_predict_goalmodel
    root = Path(runtime).resolve()
    clock = clock or (lambda: datetime.now(timezone.utc))
    started = clock()
    now = _iso(started)
    implementation = implementation_fingerprint()
    original_path = root / "data" / (league + ".json")
    original = _read(original_path)
    matches = [m for m in original["matches"] if _timestamp(m["kickoff"], "kickoff") + RESULT_AVAILABILITY_DELAY <= started]
    directory = root / "advanced_models" / league / digest(protocol)
    input_refs = [_freeze(root, original_path, capacity_guard)]
    schedule_path = root / "feature_models" / "schedules" / league / "latest.json"
    schedule_events = None
    if schedule_path.exists():
        schedule = _read(schedule_path)
        if schedule.get("coverage", {}).get("provider_queries_complete"):
            schedule_events = schedule["coverage"]["workload_events"]
            input_refs.append(_freeze(root, schedule_path, capacity_guard))
    replays, feature_sets, datasets = {}, {}, {}
    for horizon in sorted({v["horizon"] for v in protocol["candidates"].values()} | {"EARLY"}):
        hours = protocol["horizon_hours"] if horizon == "EARLY" else protocol.get("lineup_horizon_hours", .75)
        replay = build_replay(matches, horizon_hours=hours, block_days=protocol["block_days"],
            min_train=protocol["baseline_min_train"], half_life_days=protocol["baseline_half_life_days"],
            ridge=protocol["baseline_ridge"], schedule_events=schedule_events)
        dataset = build_feature_rows(root, league, matches=matches, horizon=horizon,
            horizon_hours=hours, mode="historical", recent_matches=protocol["advanced_feature_recent_matches"],
            min_history=protocol["advanced_feature_min_history"],
            national_load_lookback_days=protocol.get("national_load_lookback_days", 14))
        features = {r["match_id"]: r for r in dataset["rows"]}
        for m in matches:
            cutoff = _iso(_timestamp(m["kickoff"], "kickoff") - timedelta(hours=hours))
            extra = feature_values(m, matches, cutoff, schedule_events=schedule_events)
            row = features.setdefault(m["match_id"], {"match_id": m["match_id"], "home": {}, "away": {}})
            for side in ("home", "away"):
                row.setdefault(side, {}).update(extra[side])
        replays[horizon], feature_sets[horizon], datasets[horizon] = replay, features, dataset
        for source_ref in dataset.get("source_refs", []):
            source_path = _resolve(root, source_ref)
            input_refs.append(_freeze(root, source_path, capacity_guard))
    input_refs = list({r["path"]: r for r in input_refs}.values())
    frozen_lookup = {(r["original_path"], r["sha256"]): r for r in input_refs}
    datasets = _rebind_frozen_refs(datasets, frozen_lookup)
    all_rows, experiments, model_cards, fit_attempts = [], [], {}, {}
    historical_goal_cards = {}
    for horizon, replay in replays.items():
        candidates = [c for c, spec in protocol["candidates"].items() if spec["horizon"] == horizon]
        features = feature_sets[horizon]
        candidate_rows = {c: _correction_rows(replay, features, candidate_feature_names(protocol, c)) for c in candidates}
        requested = sorted(replay["forecasts"] + replay["failures"], key=lambda x: (x["kickoff"], x["match_id"]))
        block_groups = {}
        for row in requested:
            block_groups.setdefault(row["block"], []).append(row)
        for block_id, block in sorted(block_groups.items()):
            cutoff = block[0]["baseline_training_cutoff"]
            experiment = {"horizon": horizon, "block": block_id, "cutoff": cutoff,
                          "fits": {}, "failures": {}, "goalmodel_runs": {}}
            fits = {}
            for candidate in candidates:
                try:
                    model, selection, past = _fit_offset(candidate_rows[candidate], candidate_feature_names(protocol, candidate), cutoff, protocol)
                    fits[candidate] = model
                    experiment["fits"][candidate] = {"model": model, "selection": selection,
                        "training_match_ids": sorted({r["match_id"] for r in past})}
                except (ValueError, ArithmeticError) as exc:
                    experiment["failures"][candidate] = _failure(exc, "offset_fit")
            matched_parents = {}
            for added, parent in protocol.get("family_increment_comparisons", []):
                if added not in candidates or parent not in candidates:
                    continue
                key = added + "_minus_" + parent
                names = candidate_feature_names(protocol, parent)
                matched_rows = [{**r, "features": {n: r["features"][n] for n in names},
                    "feature_available_at": {n: r["feature_available_at"][n] for n in names}}
                    for r in candidate_rows[added]]
                try:
                    model, selection, past = _fit_offset(matched_rows, names, cutoff, protocol)
                    matched_parents[key] = model
                    experiment.setdefault("matched_pool_ablation_fits", {})[key] = {
                        "model": model, "selection": selection,
                        "training_match_ids": sorted({r["match_id"] for r in past}),
                        "same_training_ids_as_added": candidate_feature_names(protocol, added) is not None
                            and model["training_match_ids"] == fits.get(added, {}).get("training_match_ids")}
                except (ValueError, ArithmeticError, KeyError) as exc:
                    experiment.setdefault("matched_pool_ablation_failures", {})[key] = _failure(exc, "matched_parent_fit")
            goal_forecasts, goal_failures = {}, {}
            goal_candidates = protocol.get("goalmodel_candidates", []) if horizon in protocol.get("goalmodel_horizons", ["EARLY"]) else []
            if goal_candidates and run_goalmodel and horizon == "EARLY":
                joint_names = candidate_feature_names(protocol, "G1")
                for candidate in goal_candidates:
                    admission_names = [] if candidate == "G0_full" else joint_names
                    train = _goal_pool(matches, features, admission_names, cutoff)
                    targets = [f for f in block if all(_complete(features.get(f["match_id"], {}).get(s), admission_names) for s in ("home", "away"))]
                    names = joint_names if candidate == "G1" else []
                    experiment.setdefault("goalmodel_training_pools", {})[candidate] = {
                        "match_ids": [m["match_id"] for m in train], "training_data_sha256": digest(train),
                        "target_match_ids": [m["match_id"] for m in targets]}
                    try:
                        if len(train) < protocol["baseline_min_train"]:
                            raise ModelDataError("insufficient goalmodel training pool")
                        result = fit_predict_goalmodel(train, [{k: f[k] for k in ("match_id", "home_id", "away_id", "kickoff", "neutral")} for f in targets],
                            cutoff=cutoff, feature_names=names,
                            training_features={m["match_id"]: _side_values(features[m["match_id"]], names) for m in train} if names else None,
                            prediction_features={f["match_id"]: _side_values(features[f["match_id"]], names) for f in targets} if names else None)
                        experiment["goalmodel_runs"][candidate] = result
                        if result.get("status") == "ok":
                            saved = {"kind": "goalmodel_saved_fit", "fit_status": "fitted", "dc": False,
                                "training_cutoff": result["cutoff"], "training_match_ids": result["training_match_ids"],
                                "feature_names": names, "goalmodel_fit": result["fit"]}
                            for target in block:
                                historical_goal_cards.setdefault(target["match_id"], {})[candidate] = saved
                        for p in result["forecasts"]:
                            goal_forecasts.setdefault(p["match_id"], {})[candidate] = p
                        for error in result.get("errors", []):
                            goal_failures.setdefault(error["match_id"], {})[candidate] = error
                        if result.get("status") != "ok":
                            experiment["failures"][candidate] = result.get("execution_error", {"code": "R_fit_failed"})
                    except (ValueError, ArithmeticError) as exc:
                        experiment["failures"][candidate] = _failure(exc, "goalmodel_fit")
            elif goal_candidates and run_goalmodel and horizon == "LINEUP":
                # Deployment reuses the saved EARLY-trained independent fit at
                # either forecast horizon. Reproduce that same definition here;
                # do not evaluate a hypothetical later refit that is not deployed.
                for f in block:
                    for candidate in goal_candidates:
                        try:
                            card = historical_goal_cards.get(f["match_id"], {}).get(candidate)
                            if card is None:
                                raise ModelDataError("no_matching_EARLY_goalmodel_fit")
                            if candidate == "G0":
                                _side_values(features[f["match_id"]], candidate_feature_names(protocol, "G1"))
                            values = _side_values(features[f["match_id"]], card["feature_names"])
                            fixture = {k: f[k] for k in ("match_id", "home_id", "away_id", "kickoff", "neutral")}
                            predicted = predict_saved_goalmodel(card, fixture, values, f["cutoff_at"])
                            goal_forecasts.setdefault(f["match_id"], {})[candidate] = predicted
                            experiment.setdefault("goalmodel_saved_EARLY_fit_reuse", {}).setdefault(candidate, {})[str(f["match_id"])] = {
                                "training_cutoff": card["training_cutoff"], "training_match_ids_hash": digest(card["training_match_ids"])}
                        except (ValueError, ArithmeticError, KeyError) as exc:
                            goal_failures.setdefault(f["match_id"], {})[candidate] = _failure(exc, "LINEUP_saved_EARLY_fit_prediction")
            for f in block:
                baseline = copy.deepcopy(f.get("baseline_prediction"))
                row = {"match_id": f["match_id"], "kickoff": f["kickoff"], "cutoff_at": f["cutoff_at"],
                    "horizon": horizon, "scores": {}, "predictions": {}, "candidate_status": {}, "candidate_failures": {}}
                if baseline:
                    row["scores"]["M0"] = score_forecast(baseline, f, extended_diagnostics=True)
                else:
                    row["candidate_failures"]["M0"] = {"code": f["code"], "detail": f["reason"]}
                    if f.get("code") == "unseen_team":
                        try:
                            from .cold_start import score_cold_start
                            parent_fit = next(x for x in replay["baseline_fits"] if x["block"] == block_id)
                            prediction = score_cold_start(parent_fit["model"], f)
                            prediction.update(match_id=f["match_id"], cutoff_at=f["cutoff_at"])
                            row["predictions"]["COLD_START"] = prediction
                            row["scores"]["COLD_START"] = score_forecast(prediction, f, extended_diagnostics=True)
                            row["candidate_status"]["COLD_START"] = "unpaired_research_coverage_only"
                        except (ValueError, ArithmeticError, KeyError) as exc:
                            row["candidate_failures"]["COLD_START"] = _failure(exc, "cold_start_prediction")
                for candidate in candidates:
                    try:
                        if baseline is None:
                            raise ModelDataError("baseline_unavailable")
                        if candidate not in fits:
                            raise ModelDataError("no_successful_fit")
                        values = _side_values(features.get(f["match_id"], {}), candidate_feature_names(protocol, candidate))
                        prediction = predict_correction(fits[candidate], baseline, values["home"], values["away"])
                        prediction["lineup_adjusted"] = any("lineup" in g for g in protocol["candidates"][candidate]["groups"])
                        # The unchanged full baseline is already present once in
                        # this report's replay; do not duplicate it per candidate.
                        prediction.pop("baseline_prediction", None)
                        prediction["baseline_replay_forecast_ref"] = {"horizon": horizon, "match_id": f["match_id"], "block": block_id}
                        row["predictions"][candidate] = prediction
                        row["scores"][candidate] = score_forecast(prediction, f, extended_diagnostics=True)
                    except (ValueError, ArithmeticError) as exc:
                        row["candidate_failures"][candidate] = _failure(exc, "offset_prediction")
                for candidate in goal_candidates:
                    if candidate in goal_forecasts.get(f["match_id"], {}):
                        prediction = goal_forecasts[f["match_id"]][candidate]
                        row["predictions"][candidate] = prediction
                        row["scores"][candidate] = score_forecast(prediction, f, extended_diagnostics=True)
                    else:
                        row["candidate_failures"][candidate] = goal_failures.get(f["match_id"], {}).get(candidate,
                            experiment["failures"].get(candidate, {"code": "missing_features_or_not_run"}))
                row["matched_pool_ablation_scores"] = {}
                for added, parent in protocol.get("family_increment_comparisons", []):
                    key = added + "_minus_" + parent
                    if baseline is None or key not in matched_parents or added not in row["scores"]:
                        continue
                    try:
                        names = candidate_feature_names(protocol, parent)
                        values = _side_values(features[f["match_id"]], names)
                        predicted = predict_correction(matched_parents[key], baseline, values["home"], values["away"])
                        row["matched_pool_ablation_scores"][key] = {
                            "added": row["scores"][added],
                            "parent_on_added_training_pool": score_forecast(predicted, f, extended_diagnostics=True)}
                    except (ValueError, ArithmeticError) as exc:
                        row.setdefault("matched_pool_ablation_failures", {})[key] = _failure(exc, "matched_parent_prediction")
                for candidate in candidates + goal_candidates:
                    row["candidate_status"][candidate] = "predicted" if candidate in row["scores"] else "unavailable"
                    if candidate in row["scores"] or baseline:
                        row["scores"][candidate + "_policy"] = row["scores"].get(candidate, row["scores"].get("M0"))
                all_rows.append(row)
            experiments.append(experiment)
        for candidate in candidates:
            names = candidate_feature_names(protocol, candidate)
            attempt = {"candidate_id": candidate, "horizon": horizon, "training_cutoff": now,
                       "status": "failed", "generated_at": _iso(clock()), "source_commit": source_commit}
            try:
                model, selection, past = _fit_offset(candidate_rows[candidate], names, now, protocol)
                bundle = {"schema_version": "advanced-training-input-v1", "protocol": protocol,
                    "horizon": horizon, "correction_rows": past, "baseline_fits": replay["baseline_fits"],
                    "correction_model": model,
                    "feature_dataset": datasets[horizon], "source_files": input_refs,
                    "training_cutoff": now, "historical_PIT_verified": False}
                bundle_path = save_artifact(root, directory / "training_inputs", bundle, guard=capacity_guard)
                card = {"schema_version": "advanced-model-card-v1", "candidate_id": candidate,
                    "league": league, "horizon": horizon, "kind": "offset_correction",
                    "feature_names": names, "feature_groups": protocol["candidates"][candidate]["groups"],
                    "fit_status": "fitted", "correction_model": model, "selection": selection,
                    "training_cutoff": now, "generated_at": _iso(clock()), "source_commit": source_commit,
                    "state_commit": state_commit, "protocol_hash": digest(protocol),
                    "implementation_fingerprint": implementation,
                    "source_refs": input_refs + [_ref(root, bundle_path)],
                    "correction_training_ref": _ref(root, bundle_path),
                    "usage": "engineering_shadow_only", "predictive_improvement_verified": False,
                    "automatic_promotion": False}
                card["model_hash"] = digest(card)
                card_path = save_artifact(root, directory / "models" / candidate / "history", card, guard=capacity_guard)
                _write(directory / "models" / candidate / "latest.json", card, capacity_guard)
                model_cards[candidate] = _ref(root, card_path)
                attempt.update(status="fitted", model_card_ref=model_cards[candidate])
            except (ValueError, ArithmeticError) as exc:
                attempt.update(_failure(exc, "latest_offset_fit"))
            attempt["previous_latest_preserved"] = attempt["status"] != "fitted" and (directory / "models" / candidate / "latest.json").exists()
            path = save_artifact(root, directory / "models" / candidate / "fit_attempts", attempt, guard=capacity_guard)
            _write(directory / "models" / candidate / "latest_fit_status.json", {**attempt, "attempt_ref": _ref(root, path)}, capacity_guard)
            fit_attempts[candidate] = attempt
    if run_goalmodel:
        features = feature_sets["EARLY"]
        joint_names = candidate_feature_names(protocol, "G1")
        for candidate in protocol.get("goalmodel_candidates", []):
            attempt = {"candidate_id": candidate, "status": "failed", "training_cutoff": now,
                       "generated_at": _iso(clock()), "source_commit": source_commit}
            try:
                admission = [] if candidate == "G0_full" else joint_names
                train = _goal_pool(matches, features, admission, now)
                if len(train) < protocol["baseline_min_train"]:
                    raise ModelDataError("insufficient latest goalmodel training pool")
                names = joint_names if candidate == "G1" else []
                train_features = {m["match_id"]: _side_values(features[m["match_id"]], names) for m in train} if names else {}
                # Genuine R diagnostic probes use known team IDs and last admitted
                # feature vectors, but explicitly synthetic fixture IDs/times.
                # They check arithmetic only, never count as match forecasts.
                probe_records = train[-min(6, len(train)):]
                used_ids = {m["match_id"] for m in matches}
                next_id = max(used_ids, default=0) + 1
                probes = [{"match_id": next_id + i, "home_id": m["home_id"], "away_id": m["away_id"],
                    "kickoff": _iso(started + timedelta(days=1)), "neutral": bool(i % 2)} for i, m in enumerate(probe_records)]
                probe_features = {p["match_id"]: _side_values(features[m["match_id"]], names)
                                  for p, m in zip(probes, probe_records)} if names else {}
                result = fit_predict_goalmodel(train, probes, cutoff=now, feature_names=names,
                    training_features=train_features or None, prediction_features=probe_features or None)
                if result.get("status") != "ok" or not result.get("forecasts"):
                    raise ModelDataError("latest real R fit failed: " + str(result.get("execution_error")))
                bundle = {"schema_version": "advanced-goalmodel-training-v1", "matches": train,
                    "training_features": train_features, "feature_names": names, "cutoff": now,
                    "source_files": input_refs, "real_R_result": result,
                    "probe_features": probe_features, "probe_scope": "synthetic_engineering_only_not_match_predictions"}
                bundle_path = save_artifact(root, directory / "training_inputs", bundle, guard=capacity_guard)
                card = {"schema_version": "advanced-model-card-v1", "kind": "goalmodel_saved_fit",
                    "candidate_id": candidate, "league": league, "horizon": "EARLY", "dc": False,
                    "training_feature_horizon": "EARLY", "prediction_horizons": protocol.get("goalmodel_horizons", ["EARLY"]),
                    "feature_names": names, "fit_status": "fitted", "goalmodel_fit": result["fit"],
                    "goalmodel_source_commit": result["source_commit"], "goalmodel_source_sha256": result["source_sha256"],
                    "training_match_ids": result["training_match_ids"], "training_data_sha256": digest(train),
                    "training_cutoff": now, "generated_at": _iso(clock()), "source_commit": source_commit,
                    "state_commit": state_commit, "protocol_hash": digest(protocol),
                    "implementation_fingerprint": implementation,
                    "source_refs": input_refs + [_ref(root, bundle_path)], "goalmodel_training_ref": _ref(root, bundle_path),
                    "usage": "engineering_shadow_only", "predictive_improvement_verified": False,
                    "automatic_promotion": False}
                card["reproduction_verification"] = verify_goalmodel_probes(card, result, probe_features)
                card["model_hash"] = digest(card)
                card_path = save_artifact(root, directory / "models" / candidate / "history", card, guard=capacity_guard)
                _write(directory / "models" / candidate / "latest.json", card, capacity_guard)
                model_cards[candidate] = _ref(root, card_path)
                attempt.update(status="fitted", model_card_ref=model_cards[candidate])
            except (ValueError, ArithmeticError) as exc:
                attempt.update(_failure(exc, "latest_goalmodel_fit"))
            attempt["previous_latest_preserved"] = attempt["status"] != "fitted" and (directory / "models" / candidate / "latest.json").exists()
            path = save_artifact(root, directory / "models" / candidate / "fit_attempts", attempt, guard=capacity_guard)
            _write(directory / "models" / candidate / "latest_fit_status.json", {**attempt, "attempt_ref": _ref(root, path)}, capacity_guard)
            fit_attempts[candidate] = attempt
    comparisons, coverage = {}, {}
    for candidate in list(protocol["candidates"]) + protocol.get("goalmodel_candidates", []):
        horizon = protocol["candidates"].get(candidate, {"horizon": "EARLY"})["horizon"]
        rows = [r for r in all_rows if r["horizon"] == horizon]
        comparisons[candidate] = paired_summary(rows, candidate)
        coverage[candidate] = {"horizon": horizon, **_request_summary(rows, candidate)}
        if candidate in protocol.get("goalmodel_candidates", []):
            coverage[candidate]["by_horizon"] = {h: _request_summary([r for r in all_rows if r["horizon"] == h], candidate)
                for h in protocol.get("goalmodel_horizons", ["EARLY"])}
    increments = {}
    for added, parent in protocol.get("family_increment_comparisons", []) + [["G1", "G0"]]:
        key = added + "_minus_" + parent
        if added == "G1":
            rows = [r for r in all_rows if added in r["scores"] and parent in r["scores"] and r["horizon"] == "EARLY"]
            differences = [r["scores"][added]["scoreline_nll"] - r["scores"][parent]["scoreline_nll"] for r in rows]
        else:
            rows = [r for r in all_rows if key in r["matched_pool_ablation_scores"]]
            differences = [r["matched_pool_ablation_scores"][key]["added"]["scoreline_nll"] -
                           r["matched_pool_ablation_scores"][key]["parent_on_added_training_pool"]["scoreline_nll"] for r in rows]
        increments[key] = {"n": len(rows), "mean_paired_scoreline_nll_difference":
            float(np.mean(differences)) if differences else None,
            "interpretation": "same_training_and_target_pool_feature_increment; alpha_selected_separately_inside_past_only_data"}
    report = {"schema_version": "advanced-research-v1", "league": league,
        "run_started_at": now, "generated_at": _iso(clock()), "source_commit": source_commit,
        "state_commit": state_commit, "protocol": protocol, "protocol_hash": digest(protocol),
        "implementation_fingerprint": implementation,
        "purpose": "historical_development_only", "prospective_qualified": False,
        "requested_matches": len(matches), "requested_horizon_opportunities": len(all_rows),
        "unavailable_targets": [{"match_id": m["match_id"], "reason": "result_not_mature_at_study_start"}
            for m in original["matches"] if m not in matches],
        "replays": replays, "feature_datasets": datasets, "experiments": experiments,
        "scored_matches": all_rows, "comparisons": comparisons, "request_coverage": coverage,
        "family_increments": increments, "model_cards": model_cards, "latest_fits": fit_attempts,
        "comparison_design": {
            "offset_family_increment": "same_training_and_target_pool_parent_refit_with_past_only_alpha_selection",
            "G0_G1": "same_feature_complete_training_and_target_pool",
            "G0_full_EARLY": "same_result_mature_training_pool_and_cutoff_as_EARLY_M0",
            "G0_full_LINEUP": "reuse_actual_EARLY_fit; LINEUP_M0_may_have_later_training_cutoff; policy_comparison_not_pure_structure",
            "goalmodel_live_fit_definition": "saved_EARLY_feature_training_fit_reused_at_both_horizons; forecast_features_rebuilt_at_each_cutoff"},
        "source_refs": input_refs, "goalmodel_requested": run_goalmodel,
        "production_changed": False, "accuracy_improvement_verified": False,
        "betting_returns": None, "quotes_used": False,
        "warnings": ["Historical downloaded source availability is assumed, not prospective.",
            "EARLY lineup features describe past continuity, not the unknown current XI.",
            "LINEUP effects use current XI versus history only; no subjective player ratings.",
            "International individual workload and unknown-team fallback require separate evidence."]}
    report["cold_start_diagnostics"] = {"forecast_horizon_opportunities": sum("COLD_START" in r["scores"] for r in all_rows),
        "unique_matches": len({r["match_id"] for r in all_rows if "COLD_START" in r["scores"]}),
        "paired_improvement_claim": False, "production_baseline_failures_preserved": True}
    path = save_artifact(root, directory / "reports" / "history", report, guard=capacity_guard)
    summary = {k: v for k, v in report.items() if k not in ("replays", "feature_datasets", "experiments", "scored_matches")}
    summary["full_report_ref"] = _ref(root, path)
    _write(directory / "reports" / "latest.json", summary, capacity_guard)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["study"])
    parser.add_argument("--runtime", default="runtime")
    parser.add_argument("--league", default="all")
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--protocol", default="advanced-models-v1.json")
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--state-commit")
    parser.add_argument("--run-goalmodel", action="store_true")
    parser.add_argument("--checkpoint-state", action="store_true",
                        help="main release only: CAS-save each completed league and resume verified saved closures")
    parser.add_argument("--resume-only", action="store_true",
                        help="verify saved development snapshots and current input match; never fit missing/stale leagues")
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    if args.resume_only and not args.checkpoint_state:
        parser.error("--resume-only requires --checkpoint-state")
    runtime = Path(args.runtime).resolve()
    protocol = validate_protocol(_read(args.protocol))
    leagues = [c["key"] for c in _read(args.config)["competitions"] if c.get("mode") != "audit_only"] if args.league == "all" else [args.league]
    results = []
    from .advanced_capacity import CapacityGuard, CapacityDeferred
    guard = CapacityGuard(runtime)
    checkpoints = None
    if args.checkpoint_state:
        from .research_checkpoints import ResearchCheckpoints
        try:
            checkpoints = ResearchCheckpoints(runtime, args.repo, args.source_commit, guard=guard)
        except (OSError, ValueError, RuntimeError, KeyError, TypeError) as exc:
            print(json.dumps({"status": "checkpoint_setup_failed", "error_code": type(exc).__name__,
                "research_full_completed": False, "durable_completed_leagues": 0}, ensure_ascii=False), flush=True)
            return 1

    def record_attempt(value, *, append=True):
        directory = runtime / "advanced_models" / value["league"] / digest(protocol) / "reports"
        path = save_artifact(runtime, directory / "failures", value, guard=guard, final=True)
        _write(directory / "latest_attempt.json", {**value, "failure_ref": _ref(runtime, path)}, guard, final=True)
        if append:
            results.append(value)
        print(json.dumps({**value, "capacity_after_report": guard.summary()}, ensure_ascii=False), flush=True)

    def run_summary(*, final=False):
        completed = [r for r in results if r["status"] == "completed_development"]
        durable = [r for r in completed if r.get("durable_saved") is True]
        stored = [r for r in results if r.get("durable_saved") is True and r["status"] in
            ("completed_development", "verified_saved_development_snapshot")]
        calculation_full = bool(leagues) and len(completed) == len(leagues)
        durable_full = calculation_full and len(durable) == len(leagues)
        summary = {"results": results, "production_changed": False,
            "research_full_completed": durable_full if checkpoints else calculation_full,
            "research_calculation_full_completed": calculation_full,
            "research_durable_full_completed": durable_full,
            "durability_mode": "per_league_cas_checkpoint" if checkpoints else "not_requested_in_this_command",
            "configured_leagues": leagues, "completed_leagues": len(completed),
            "calculation_completed_leagues": len(completed), "durable_completed_leagues": len(durable),
            "resumed_leagues": sum(r.get("calculation_reused") is True for r in completed),
            "stored_verified_completed_leagues": len(stored),
            "stored_research_durable_full_completed": bool(leagues) and len(stored) == len(leagues),
            "current_inputs_match_leagues": sum(r.get("current_inputs_match") is True for r in stored),
            "current_refit_required_leagues": sum(r.get("current_refit_required") is True
                or r["status"] == "checkpoint_resume_deferred" for r in results),
            "pending_leagues": len(leagues) - (len(durable) if checkpoints else len(completed)),
            "capacity_deferred_leagues": sum(r["status"] == "capacity_deferred" for r in results),
            "checkpoint_deferred_leagues": sum(r.get("checkpoint_status") == "resource_deferred" for r in results),
            "checkpoint_failed_leagues": sum(r.get("checkpoint_status") in
                ("save_failed", "saved_postprocess_failed") for r in results),
            "source_commit": args.source_commit, "protocol_hash": digest(protocol),
            "generated_at": _iso(datetime.now(timezone.utc)), "capacity": guard.summary(),
            "run_calculation_ended": final,
            "resume_only": args.resume_only,
            "summary_file_durability": "requires_final_state_save; league_receipts_verified_separately"}
        run_root = runtime / "advanced_models" / "study_runs"
        path = save_artifact(runtime, run_root / "history", summary, guard=guard, final=True)
        _write(run_root / "latest.json", {**summary, "full_report_ref": _ref(runtime, path)}, guard, final=True)
        return summary

    for index, league in enumerate(leagues):
        try:
            # Other writers (notably compact/deduplicated object creation) may
            # change physical accounting; do not carry a stale estimate forward.
            guard.refresh()
            print(json.dumps({"league": league, "status": "research_started", "capacity": guard.summary()}, ensure_ascii=False), flush=True)
            identity, resume_reason = None, None
            if checkpoints:
                identity = checkpoints.identity(league, digest(protocol), implementation_fingerprint(), args.run_goalmodel)
                resumed, resume_reason = checkpoints.resume(league, digest(protocol), identity,
                    protocol.get("max_model_age_days", 14),
                    **({"allow_saved_snapshot": True} if args.resume_only else {}))
                if resumed is not None:
                    results.append(resumed)
                    print(json.dumps({"league": league, "status": "research_resumed_from_saved_checkpoint"
                        if resumed.get("calculation_reused") else "research_verified_saved_snapshot_current_refit_required",
                        "durable_state_commit": resumed["durable_state_commit"],
                        "checkpoint_ref": resumed["checkpoint_ref"]}, ensure_ascii=False), flush=True)
                    run_summary()
                    continue
                if args.resume_only:
                    record_attempt({"league": league, "status": "checkpoint_resume_deferred",
                        "reason": resume_reason, "calculation_completed": False,
                        "fit_attempted_this_run": False, "durable_saved": False,
                        "current_refit_required": True, "generated_at": _iso(datetime.now(timezone.utc)),
                        "source_commit": args.source_commit, "protocol_hash": digest(protocol)})
                    continue
            report = study(runtime, league, protocol, source_commit=args.source_commit,
                state_commit=args.state_commit, run_goalmodel=args.run_goalmodel, capacity_guard=guard)
            result = {"league": league, "status": "completed_development",
                "requested_matches": report["requested_matches"], "latest_fits": report["latest_fits"],
                "request_coverage": report["request_coverage"], "accuracy_improvement_verified": False,
                "calculation_completed": True, "calculation_reused": False, "durable_saved": False,
                "durability_status": "awaiting_checkpoint_save" if checkpoints else "not_requested_in_this_command"}
            if checkpoints:
                result["resume_not_used_reason"] = resume_reason
            results.append(result)
            guard.refresh()
            print(json.dumps({"league": league, "status": "research_completed", "requested_matches": report["requested_matches"],
                "fitted_candidates": [k for k,v in report["latest_fits"].items() if v["status"] == "fitted"],
                "capacity": guard.summary()}, ensure_ascii=False), flush=True)
            if checkpoints:
                # Save progress and the whole configured denominator before
                # attempting a network save. A killed job leaves honest local
                # calculation records; only verified Git closures are reused.
                run_summary()
                try:
                    saved = checkpoints.persist(league, digest(protocol), identity, report, result, _write)
                    result.update(saved)
                    result["durability_status"] = "verified_cas_save" if saved["durable_saved"] else "resource_deferred"
                except CapacityDeferred as exc:
                    result.update(checkpoint_status="resource_deferred", checkpoint_capacity=exc.details,
                                  durability_status="resource_deferred", durable_saved=False)
                except (OSError, ValueError, RuntimeError) as exc:
                    result.update(checkpoint_status="save_failed", checkpoint_error_code=type(exc).__name__,
                                  durability_status="save_failed", durable_saved=False)
                if not result["durable_saved"] or result.get("continue_after_save") is False:
                    record_attempt({"league": league, "status": "checkpoint_save_incomplete",
                        "calculation_completed": True, "durable_saved": result["durable_saved"],
                        "checkpoint_status": result["checkpoint_status"],
                        "generated_at": _iso(datetime.now(timezone.utc)), "source_commit": args.source_commit,
                        "protocol_hash": digest(protocol)}, append=False)
                    for pending in leagues[index + 1:]:
                        record_attempt({"league": pending, "status": "checkpoint_blocked",
                            "calculation_completed": False, "durable_saved": False,
                            "fit_attempted_this_run": False, "reason": "pending_after_checkpoint_save_incomplete",
                            "generated_at": _iso(datetime.now(timezone.utc)), "source_commit": args.source_commit,
                            "protocol_hash": digest(protocol)})
                    break
                print(json.dumps({"league": league, "status": "research_checkpoint_saved",
                    "state_commit": result["durable_state_commit"], "capacity": guard.summary()}, ensure_ascii=False), flush=True)
        except CapacityDeferred as exc:
            # Preserve the remaining configured denominator without launching
            # additional expensive fits once the shared guard is exhausted.
            calculated = bool(results and results[-1].get("league") == league
                and results[-1]["status"] == "completed_development")
            if calculated:
                results[-1].update(checkpoint_status="resource_deferred", checkpoint_capacity=exc.details,
                    durable_saved=False, durability_status="resource_deferred_after_calculation")
            for pending in leagues[index + int(calculated):]:
                history_path = runtime / "data" / (pending + ".json")
                try:
                    count = len(_read(history_path)["matches"])
                except (OSError, ValueError, KeyError, TypeError):
                    count = None
                record_attempt({"league": pending, "status": "capacity_deferred",
                    "requested_history_matches": count, "research_full_completed": False,
                    "fit_attempted_this_run": pending == league,
                    "reason": "storage_capacity_exhausted" if pending == league else "pending_after_capacity_exhaustion",
                    "capacity": exc.details, "generated_at": _iso(datetime.now(timezone.utc)),
                    "source_commit": args.source_commit, "protocol_hash": digest(protocol)})
            break
        except (OSError, ValueError, RuntimeError, ArithmeticError) as exc:
            failure = {"league": league, "status": "failed", **_failure(exc, "study"),
                "generated_at": _iso(datetime.now(timezone.utc)), "source_commit": args.source_commit}
            calculated = bool(results and results[-1].get("league") == league
                and results[-1]["status"] == "completed_development")
            if calculated:
                results[-1].update(checkpoint_status="save_failed", durable_saved=False,
                    post_calculation_error=_failure(exc, "after_study"))
            record_attempt(failure, append=not calculated)
    summary = run_summary(final=True)
    print(json.dumps(summary, ensure_ascii=False), flush=True)
    return int(any(r["status"] == "failed" or r.get("checkpoint_status") in
        ("save_failed", "saved_postprocess_failed") for r in results))


if __name__ == "__main__":
    raise SystemExit(main())
