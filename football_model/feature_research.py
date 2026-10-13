"""Past-only feature experiments and auditable model cards, never auto promotion."""
from __future__ import annotations

import argparse
import copy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
from pathlib import Path
import platform

import numpy as np

from .feature_correction import fit_correction, predict_correction, _training_rows
from .feature_dataset import build_replay, coverage_report, feature_values
from .model import ModelDataError, RESULT_AVAILABILITY_DELAY, _iso, _timestamp
from .settlement import matrix_array, probabilities


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def save_immutable(directory, value):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    key = digest(value)
    path = directory / (key + ".json")
    data = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n"
    if path.exists():
        if json.loads(path.read_text()) != value:
            raise ModelDataError("immutable artifact collision")
    else:
        with path.open("x", encoding="utf-8") as out:
            out.write(data)
    return path


def _read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _complete(features, names):
    return isinstance(features, dict) and set(features) == set(names) and all(
        type(features[k]) in (int, float) and math.isfinite(features[k]) for k in names)


def score_forecast(prediction, match, *, extended_diagnostics=False):
    matrix = matrix_array(prediction["score_matrix"])
    if prediction.get("score_matrix_axes") != {"rows": "home_goals_from_0", "columns": "away_goals_from_0"}:
        raise ModelDataError("score matrix axes mismatch")
    h, a = match["home_goals"], match["away_goals"]
    if any(type(v) is not int or v < 0 for v in (h,a)):
        raise ModelDataError("invalid observed regulation score")
    p = float(matrix[h, a]) if h < matrix.shape[0] and a < matrix.shape[1] else 0.0
    order = ("home_win", "draw", "away_win")
    predicted = prediction["probabilities"]
    if set(predicted) != set(order):
        raise ModelDataError("invalid 1X2 fields")
    y = "home_win" if h > a else ("draw" if h == a else "away_win")
    values = np.array([predicted[k] for k in order])
    if not np.all(np.isfinite(values)) or np.any(values<0) or np.any(values>1):
        raise ModelDataError("invalid 1X2 probability values")
    recomputed = np.array([np.tril(matrix,-1).sum(),np.trace(matrix),np.triu(matrix,1).sum()])
    if np.max(np.abs(values-recomputed)) > 1e-15:
        raise ModelDataError("1X2 probabilities disagree with matrix")
    home, away = np.indices(matrix.shape)
    events = {"draw": (home == away, h == a), "home_net_ge2": (home-away >= 2, h-a >= 2),
        "home_net_ge3": (home-away >= 3, h-a >= 3), "total_ge3": (home+away >= 3, h+a >= 3)}
    if extended_diagnostics:
        events.update(away_net_ge2=(away-home >= 2, a-h >= 2), away_net_ge3=(away-home >= 3, a-h >= 3))
    result = {"scoreline_nll": -math.log(max(p, 1e-15)),
        "one_x_two_nll": -math.log(max(predicted[y], 1e-15)),
        "one_x_two_brier": float(np.sum((values - np.array([k == y for k in order]))**2)),
        "events": {key: {"probability": float(matrix[mask].sum()), "observed": int(observed)}
            for key, (mask, observed) in events.items()},
        "probability_floor": 1e-15, "observed_score_outside_grid": p == 0}
    if extended_diagnostics:
        total_probability = float(matrix[home + away == h + a].sum())
        difference_probability = float(matrix[home - away == h - a].sum())
        result.update(total_goal_nll=-math.log(max(total_probability, 1e-15)),
                      goal_difference_nll=-math.log(max(difference_probability, 1e-15)))
    return result


def _mean_metrics(rows):
    if not rows:
        return {"n": 0, "scoreline_nll": None, "one_x_two_nll": None, "one_x_two_brier": None,
                "total_goal_nll": None, "goal_difference_nll": None,
                "total_goal_nll_available_n": 0, "goal_difference_nll_available_n": 0}
    result = {"n": len(rows), **{key: float(np.mean([r[key] for r in rows]))
        for key in ("scoreline_nll", "one_x_two_nll", "one_x_two_brier")}}
    # Old immutable scores lack the new projection and away-tail diagnostics.
    # Their absence is unknown, never a zero or an invented historic score.
    for key in ("total_goal_nll", "goal_difference_nll"):
        available = [r[key] for r in rows if r.get(key) is not None]
        result[key] = float(np.mean(available)) if available else None
        result[key + "_available_n"] = len(available)
    event_names = sorted({key for row in rows for key in row["events"]}
                         | {"away_net_ge2", "away_net_ge3"})
    result["event_calibration"] = {}
    for key in event_names:
        available = [row["events"][key] for row in rows if key in row["events"]]
        result["event_calibration"][key] = {
            "n": len(available), "missing_n": len(rows) - len(available),
            "mean_predicted": float(np.mean([r["probability"] for r in available])) if available else None,
            "observed_rate": float(np.mean([r["observed"] for r in available])) if available else None,
            "brier": float(np.mean([(r["probability"] - r["observed"])**2 for r in available])) if available else None}
    return result


def paired_summary(rows, candidate):
    pairs = [r for r in rows if candidate in r["scores"] and "M0" in r["scores"]]
    differences = [r["scores"][candidate]["scoreline_nll"] - r["scores"]["M0"]["scoreline_nll"]
        for r in pairs]
    return {"n": len(pairs), "candidate": _mean_metrics([r["scores"][candidate] for r in pairs]),
        "baseline_on_same_matches": _mean_metrics([r["scores"]["M0"] for r in pairs]),
        "mean_paired_scoreline_nll_difference": float(np.mean(differences)) if differences else None,
        "qualification": "historical_development_only_not_confirmation"}


def _request_summary(rows, candidate):
    """Keep the entire requested universe, including unsupported baseline rows.

    Policy forecasts use the candidate when available and the unchanged M0
    otherwise. When neither exists they remain unforecast, never a zero loss.
    Candidate-only rows contribute to deployment metrics, not paired scores.
    """
    both = [r for r in rows if candidate in r["scores"] and "M0" in r["scores"]]
    candidate_only = [r for r in rows if candidate in r["scores"] and "M0" not in r["scores"]]
    baseline_only = [r for r in rows if candidate not in r["scores"] and "M0" in r["scores"]]
    neither = [r for r in rows if candidate not in r["scores"] and "M0" not in r["scores"]]
    policy = [r["scores"][candidate + "_policy"] for r in rows if candidate + "_policy" in r["scores"]]
    return {"requested_matches": len(rows), "both_available": len(both),
        "candidate_only": len(candidate_only), "baseline_only_fallback": len(baseline_only),
        "neither_available": len(neither), "candidate_forecasts": len(both) + len(candidate_only),
        "policy_forecasts": len(policy), "policy_metrics": _mean_metrics(policy),
        "baseline_all_available": _mean_metrics([r["scores"]["M0"] for r in rows if "M0" in r["scores"]]),
        "paired_comparison": paired_summary(rows, candidate),
        "candidate_only_match_ids": [r["match_id"] for r in candidate_only],
        "baseline_only_match_ids": [r["match_id"] for r in baseline_only],
        "neither_available_match_ids": [r["match_id"] for r in neither],
        "denominator_preserved": len(rows) == len(both) + len(candidate_only) + len(baseline_only) + len(neither),
        "qualification": "historical_development_only_not_confirmation",
        "unforecast_rows_are_not_scored": True}


def choose_alpha(rows, protocol, cutoff):
    """Inner chronological selection; every validation target precedes outer cutoff."""
    names = protocol["feature_names"]
    rows = _training_rows(rows,names,_timestamp(cutoff,"cutoff"))
    minimum = 2 * protocol["correction_min_matches"]
    grouped = {}
    for row in rows:
        grouped.setdefault(row["match_id"], []).append(row)
    pairs = sorted(grouped.values(), key=lambda p: (p[0]["kickoff"], p[0]["match_id"]))
    if len(rows) < minimum:
        raise ModelDataError("insufficient past correction matches")
    split = int(len(pairs) * (1-protocol["inner_validation_fraction"]))
    validation = pairs[split:]
    if not validation:
        return protocol["correction_alpha_grid"][-1], {"status": "cold_start_fixed_strongest_penalty"}
    inner_cutoff = _iso(min(_timestamp(r["cutoff_at"],"inner cutoff") for pair in validation for r in pair))
    training = [r for pair in pairs[:split] for r in pair if _timestamp(r["target_available_at"],"available") <= _timestamp(inner_cutoff,"inner cutoff")]
    if len(training) < minimum:
        return protocol["correction_alpha_grid"][-1], {"status": "cold_start_fixed_strongest_penalty",
            "inner_training_rows": len(training)}
    from .feature_correction import apply_correction
    trials = []
    for alpha in protocol["correction_alpha_grid"]:
        try:
            model = fit_correction(training, names, alpha=alpha, cutoff=inner_cutoff, min_rows=minimum)
            losses = []
            for pair in validation:
                # This is an inner score using only historical targets already available.
                if any(_timestamp(r["target_available_at"], "available") > _timestamp(cutoff, "cutoff") for r in pair):
                    raise ModelDataError("future target entered alpha selection")
                for row in pair:
                    rate = apply_correction(model, row["lambda0"], row["features"])["lambda"]
                    y = row["goals"]
                    losses.append(rate - y*math.log(rate) + math.lgamma(y+1))
            trials.append({"alpha": alpha, "score": float(np.mean(losses)), "status": "scored"})
        except (ValueError, ArithmeticError) as exc:
            trials.append({"alpha": alpha, "status": "failed", "reason": type(exc).__name__})
    available = [t for t in trials if t["status"] == "scored"]
    if not available:
        raise ModelDataError("all inner correction candidates failed")
    chosen = min(available, key=lambda t: (t["score"], -t["alpha"]))["alpha"]
    return chosen, {"status": "past_only_inner_time_validation", "inner_cutoff": inner_cutoff,
        "validation_matches": len(validation), "trials": trials, "selected_alpha": chosen}


def study(runtime, league, protocol, *, source_commit, state_commit=None, run_goalmodel=False, clock=None):
    clock = clock or (lambda: datetime.now(timezone.utc))
    started = clock()
    design = protocol.get("research_design_version", "feature-research-v1")
    if design not in ("feature-research-v1", "feature-research-v2"):
        raise ModelDataError("unsupported research design version")
    expanded = design == "feature-research-v2"
    if expanded and protocol.get("research_artifact_namespace", "protocol_hash_v2") != "protocol_hash_v2":
        raise ModelDataError("v2 research requires its protocol hash artifact namespace")
    root = Path(runtime)
    original_path = root / "data" / (league+".json")
    original = _read(original_path)
    original_matches = original["matches"]
    matches = [m for m in original_matches if _timestamp(m["kickoff"],"kickoff")+RESULT_AVAILABILITY_DELAY<=started]
    unavailable_targets = [{"match_id":m["match_id"],"kickoff":m["kickoff"],"reason":"result_not_available_by_study_start"}
        for m in original_matches if _timestamp(m["kickoff"],"kickoff")+RESULT_AVAILABILITY_DELAY>started]
    names = protocol["feature_names"]
    now = _iso(started)
    schedule_document = root / "feature_models" / "schedules" / league / "latest.json"
    schedule_events, scope, schedule_coverage = None, "same_competition", None
    if schedule_document.exists():
        sched = _read(schedule_document)
        schedule_coverage = sched.get("coverage")
        if schedule_coverage and schedule_coverage.get("provider_queries_complete") is True:
            schedule_events = schedule_coverage["workload_events"]
            scope = "all_club_competitions_provider_scope"
    replay = build_replay(matches, horizon_hours=protocol["horizon_hours"],
        block_days=protocol["block_days"], min_train=protocol["baseline_min_train"],
        half_life_days=protocol["baseline_half_life_days"], ridge=protocol["baseline_ridge"],
        schedule_events=schedule_events)
    eligible = [r for r in replay["correction_rows"] if _complete(r["features"], names)]
    forecasts = replay["forecasts"]
    requested = forecasts
    if expanded:
        # V1's frozen score table is retained unchanged. V2 preregisters every
        # target before baseline/model admission so single-sided coverage exists.
        requested = sorted(forecasts + replay["failures"],
                           key=lambda row: (row["kickoff"], row["match_id"]))
        for row in requested:
            if "home_features" not in row:
                values = feature_values(row, matches, row["cutoff_at"], schedule_events=schedule_events)
                row["home_features"], row["away_features"] = values["home"], values["away"]
    goal_candidates = ("G0", "G1", "G0_full") if expanded else ("G0", "G1")
    entries, experiments = [], []
    groups = {}
    for f in requested:
        groups.setdefault(f["baseline_training_cutoff"], []).append(f)
    for cutoff, block in sorted(groups.items()):
        past = [r for r in eligible if _timestamp(r["target_available_at"], "available") <= _timestamp(cutoff, "cutoff")]
        experiment = {"cutoff": cutoff, "matches": len(block), "training_side_rows": len(past), "errors": []}
        correction = None
        try:
            alpha, selection = choose_alpha(past, protocol, cutoff)
            correction = fit_correction(past, names, alpha=alpha, cutoff=cutoff,
                min_rows=2*protocol["correction_min_matches"])
            experiment.update(alpha=alpha, selection=selection, correction_model=correction)
        except (ValueError, ArithmeticError) as exc:
            experiment["errors"].append({"candidate": "M1", "reason": type(exc).__name__, "detail": str(exc)})
        goal_forecasts, goal_failures = {}, {}
        if run_goalmodel:
            from .goalmodel_adapter import fit_predict_goalmodel
            training, training_features = [], {}
            for m in matches:
                t = _timestamp(m["kickoff"], "kickoff")
                if t + RESULT_AVAILABILITY_DELAY > _timestamp(cutoff, "cutoff"):
                    continue
                vals = feature_values(m, matches, _iso(t-timedelta(hours=protocol["horizon_hours"])), schedule_events=schedule_events)
                if all(_complete(vals[s], names) for s in ("home", "away")):
                    training.append(m)
                    training_features[m["match_id"]] = {s: vals[s] for s in ("home", "away")}
            predicted_features = {f["match_id"]: {"home": f["home_features"], "away": f["away_features"]}
                for f in block if all(_complete(f[s+"_features"], names) for s in ("home", "away"))}
            goal_targets = [f for f in block if f["match_id"] in predicted_features]
            fixtures = [{k: f[k] for k in ("match_id", "home_id", "away_id", "kickoff", "neutral")} for f in goal_targets]
            complete_targets = {f["match_id"] for f in goal_targets}
            full_training = [m for m in matches if _timestamp(m["kickoff"], "kickoff")
                + RESULT_AVAILABILITY_DELAY <= _timestamp(cutoff, "cutoff")]
            runs = [("G0", (), training, fixtures), ("G1", names, training, fixtures)]
            if expanded:
                full_fixtures = [{k: f[k] for k in ("match_id", "home_id", "away_id", "kickoff", "neutral")}
                                 for f in block]
                runs.append(("G0_full", (), full_training, full_fixtures))
            for candidate, features, train_pool, prediction_pool in runs:
                if expanded and candidate != "G0_full":
                    for f in block:
                        if f["match_id"] not in complete_targets:
                            goal_failures.setdefault(f["match_id"], {})[candidate] = {
                                "code": "incomplete_prediction_features", "stage": "feature_admission"}
                if expanded:
                    experiment.setdefault("goalmodel_training_pools", {})[candidate] = {
                        "match_ids": [m["match_id"] for m in train_pool],
                        "requested_match_ids": [f["match_id"] for f in prediction_pool],
                        "training_data_sha256": digest(train_pool),
                        "definition": "same_as_M0_result_eligible_pool" if candidate == "G0_full"
                                      else "feature_complete_common_G0_G1_pool"}
                try:
                    if len(train_pool)<protocol["baseline_min_train"]:
                        raise ModelDataError("insufficient common goalmodel training pool")
                    result = fit_predict_goalmodel(train_pool, prediction_pool, cutoff=cutoff, feature_names=features,
                        training_features=training_features if features else None,
                        prediction_features=predicted_features if features else None)
                    experiment.setdefault("goalmodel_runs",{})[candidate]=result
                    for p in result["forecasts"]:
                        goal_forecasts.setdefault(p["match_id"], {})[candidate] = p
                    experiment["errors"].extend({"candidate": candidate, **e} for e in result.get("errors", []))
                    if expanded:
                        errors = {e["match_id"]: e for e in result.get("errors", [])}
                        for fixture in prediction_pool:
                            mid = fixture["match_id"]
                            if candidate not in goal_forecasts.get(mid, {}):
                                goal_failures.setdefault(mid, {})[candidate] = {
                                    "stage": "goalmodel_fit_or_prediction", **errors.get(mid, {
                                        "code": "goalmodel_output_missing", "message": "requested forecast absent"})}
                except (ValueError, ArithmeticError) as exc:
                    experiment["errors"].append({"candidate": candidate, "reason": type(exc).__name__, "detail": str(exc)})
                    if expanded:
                        for fixture in prediction_pool:
                            goal_failures.setdefault(fixture["match_id"], {})[candidate] = {
                                "code": type(exc).__name__, "stage": "goalmodel_fit_or_prediction", "detail": str(exc)}
        for f in block:
            baseline = copy.deepcopy(f.get("baseline_prediction"))
            if baseline is not None:
                baseline["cutoff_at"] = f["cutoff_at"]
            row = {"match_id": f["match_id"], "kickoff": f["kickoff"], "cutoff_at": f["cutoff_at"],
                "home_goals": f["home_goals"], "away_goals": f["away_goals"],
                "scores": {"M0": score_forecast(baseline, f, extended_diagnostics=expanded)} if baseline is not None else {}, "candidate_status": {}}
            if expanded:
                row["candidate_status"]["M0"] = "predicted" if baseline is not None else "unavailable:" + f["code"]
                row["candidate_failures"] = {} if baseline is not None else {"M0": {
                    "code": f["code"], "stage": f["stage"], "detail": f["reason"]}}
            if baseline is None:
                row["candidate_status"]["M1"] = "unavailable:baseline_unavailable"
                row["candidate_failures"]["M1"] = {"code": "baseline_unavailable", "stage": "offset_admission"}
            elif correction is not None:
                try:
                    predicted = predict_correction(correction, baseline, f["home_features"], f["away_features"])
                    row["scores"]["M1"] = score_forecast(predicted, f, extended_diagnostics=expanded)
                    row["candidate_prediction"] = predicted
                    row["candidate_status"]["M1"] = "predicted"
                except (ValueError, ArithmeticError) as exc:
                    row["candidate_status"]["M1"] = "fallback:"+type(exc).__name__
                    if expanded:
                        row["candidate_failures"]["M1"] = {"code": type(exc).__name__,
                            "stage": "correction_prediction", "detail": str(exc)}
            else:
                row["candidate_status"]["M1"] = "fallback:insufficient_or_failed_training"
                if expanded:
                    row["candidate_failures"]["M1"] = {"code": "insufficient_or_failed_training",
                        "stage": "correction_fit", "errors": copy.deepcopy(experiment["errors"][:1])}
            # The deployment policy includes each recorded fallback unchanged.
            if baseline is not None:
                row["scores"]["M1_policy"] = row["scores"].get("M1", row["scores"]["M0"])
            for candidate, predicted in goal_forecasts.get(f["match_id"], {}).items():
                row["scores"][candidate] = score_forecast(predicted, f, extended_diagnostics=expanded)
            if expanded:
                for candidate in goal_candidates:
                    if candidate in row["scores"]:
                        row["candidate_status"][candidate] = "predicted"
                    else:
                        failure = goal_failures.get(f["match_id"], {}).get(candidate, {
                            "code": "goalmodel_not_requested" if not run_goalmodel else "goalmodel_output_missing",
                            "stage": "goalmodel_admission"})
                        row["candidate_status"][candidate] = "unavailable:" + failure["code"]
                        row["candidate_failures"][candidate] = copy.deepcopy(failure)
                row["policy_status"] = {}
                for candidate in ("M1",) + goal_candidates:
                    if candidate in row["scores"]:
                        row["scores"][candidate + "_policy"] = row["scores"][candidate]
                        row["policy_status"][candidate] = "candidate"
                    elif baseline is not None:
                        row["scores"][candidate + "_policy"] = row["scores"]["M0"]
                        row["policy_status"][candidate] = "unchanged_baseline_fallback"
                    else:
                        row["policy_status"][candidate] = "unforecast_neither_available"
            entries.append(row)
        experiments.append(experiment)
    latest_past = [r for r in eligible if _timestamp(r["target_available_at"], "available") <= started]
    latest_model, latest_selection = None, None
    latest_fit = {"status": "failed", "training_cutoff": now, "training_side_rows": len(latest_past)}
    try:
        latest_alpha, latest_selection = choose_alpha(latest_past, protocol, now)
        latest_model = fit_correction(latest_past, names, alpha=latest_alpha, cutoff=now,
            min_rows=2*protocol["correction_min_matches"])
        latest_fit.update(status="fitted", selected_alpha=latest_alpha)
    except (ValueError, ArithmeticError) as exc:
        # A failed latest fit must not erase successful earlier experiments or
        # leave an old card masquerading as this run's successful latest fit.
        latest_fit.update(reason=type(exc).__name__, detail=str(exc))
    input_refs=[]
    for source_path in ([original_path,schedule_document] if scope=="all_club_competitions_provider_scope" else [original_path]):
        raw=source_path.read_bytes()
        raw_hash=hashlib.sha256(raw).hexdigest()
        frozen=root/"feature_models"/"inputs"/(raw_hash+".json")
        frozen.parent.mkdir(parents=True,exist_ok=True)
        if frozen.exists():
            if frozen.read_bytes()!=raw:raise ModelDataError("immutable training source collision")
        else:
            with frozen.open("xb") as out:out.write(raw)
        input_refs.append({"path":str(frozen.relative_to(root)),"sha256":raw_hash,
            "original_path":str(source_path.relative_to(root))})
    training_bundle=save_immutable(root/"feature_models"/"training_inputs",{
        "schema_version":"feature-training-input-v1","source_files":input_refs,
        "protocol":protocol,"baseline_fits":replay["baseline_fits"],
        "correction_rows":latest_past,"training_cutoff":now,
        "scope":scope,"historical_PIT_verified":False})
    input_refs.append({"path":str(training_bundle.relative_to(root)),
        "sha256":hashlib.sha256(training_bundle.read_bytes()).hexdigest(),"kind":"training_bundle"})
    card, card_path = None, None
    model_root = root / "feature_models" / "models" / league
    report_root = root / "feature_models" / "reports" / league
    if expanded:
        # A distinct protocol must never replace the old queue's model/report
        # latest pointers. Shared raw inputs are immutable and content addressed.
        model_root = model_root / digest(protocol)
        report_root = report_root / digest(protocol)
    if latest_model is not None:
        card = {"schema_version": "feature-model-card-v1", "league": league,
            "candidate_id": protocol.get("candidate_id", "M1_schedule_offset_l2_v1"), "source_commit": source_commit,
            "state_commit": state_commit, "training_cutoff": now, "generated_at": _iso(clock()),
            "feature_names": names, "feature_scope": scope, "correction_model": latest_model,
            "model_hash": digest(latest_model), "protocol_hash": digest(protocol),
            "usage": "engineering_shadow_only", "lineup_adjusted": False,
            "source_refs": input_refs,
            "data_observed_at": original.get("observed_at"), "selection": latest_selection,
            "predictive_improvement_verified": False, "automatic_promotion": False}
        card_path = save_immutable(model_root / "history", card)
        (model_root / "latest.json").write_text(json.dumps(card,ensure_ascii=False,indent=2)+"\n")
    latest_fit.update(source_commit=source_commit, protocol_hash=digest(protocol),
        model_card_path=str(card_path.relative_to(root)) if card_path else None,
        previous_latest_preserved=latest_model is None and (model_root / "latest.json").exists())
    fit_path = save_immutable(model_root / "fit_attempts", latest_fit)
    (model_root / "latest_fit_status.json").write_text(json.dumps({**latest_fit,
        "attempt_ref": {"path": str(fit_path.relative_to(root)),
                        "sha256": hashlib.sha256(fit_path.read_bytes()).hexdigest()}}, ensure_ascii=False, indent=2)+"\n")
    comparison_keys = ("M1", "M1_policy", "G0", "G1")
    if expanded:
        comparison_keys += ("G0_full", "G0_policy", "G1_policy", "G0_full_policy")
    report = {"schema_version": design, "league": league,
        "run_started_at": now, "generated_at": _iso(clock()), "source_commit": source_commit,
        "state_commit": state_commit, "protocol": protocol, "protocol_hash": digest(protocol),
        "purpose": "historical_development_only", "prospective_qualified": False,
        "coverage": coverage_report(root, league), "schedule_scope": scope,
        "unavailable_targets_excluded":unavailable_targets,
        "schedule_coverage": schedule_coverage, "replay": replay, "experiments": experiments,
        "scored_matches": entries, "baseline_all_available": _mean_metrics([r["scores"]["M0"] for r in entries if "M0" in r["scores"]]),
        "comparisons": {key: paired_summary(entries,key) for key in comparison_keys},
        "goalmodel_feature_increment": {"n":sum("G0" in r["scores"] and "G1" in r["scores"] for r in entries),
            "mean_paired_scoreline_nll_difference_G1_minus_G0":float(np.mean([
                r["scores"]["G1"]["scoreline_nll"]-r["scores"]["G0"]["scoreline_nll"]
                for r in entries if "G0" in r["scores"] and "G1" in r["scores"]]))
                if any("G0" in r["scores"] and "G1" in r["scores"] for r in entries) else None},
        "model_card_path": str(card_path.relative_to(root)) if card_path else None,
        "model_hash": card["model_hash"] if card else None, "latest_fit": latest_fit,
        "production_changed": False, "accuracy_improvement_verified": False,
        "betting_returns": None, "quotes_used": False,
        "numeric_environment": {"python": platform.python_version(), "numpy": np.__version__},
        "warnings": ["Historical data were downloaded later; result availability uses the existing four-hour assumption.",
            "All candidate comparisons are development diagnostics; no confirmation or profit claim.",
            "Club schedule completeness is only the audited provider scope; player international workload is not included."]}
    if expanded:
        report["request_coverage"] = {key: _request_summary(entries, key) for key in ("M1",) + goal_candidates}
        report["requested_matches"] = len(requested)
        report["research_design"] = {"version": design,
            "G0_G1_feature_increment": "identical_feature_complete_training_and_prediction_pool",
            "G0_full_structural_comparison": "same_result_eligible_training_pool_as_M0_all_requested_targets",
            "all_requested_targets_preserved": len(entries) == replay["coverage"]["requested_matches"],
            "unforecast_targets_not_scored": True}
    report["goalmodel_execution"]={"requested":run_goalmodel,
        "status":"executed_on_real_data" if all(any(k in r["scores"] for r in entries) for k in goal_candidates) else "incomplete" if run_goalmodel else "not_run",
        "branches":{k:{"successful_forecast_matches":sum(k in r["scores"] for r in entries),
            "successful_fits":sum(bool(e.get("goalmodel_runs",{}).get(k,{}).get("forecasts")) for e in experiments),
            "failures":sum(x.get("candidate")==k for e in experiments for x in e["errors"])} for k in goal_candidates}}
    path = save_immutable(report_root / "history", report)
    summary={k:v for k,v in report.items() if k not in ("replay","experiments","scored_matches")}
    summary.update(schema_version="feature-research-summary-v2" if expanded else "feature-research-summary-v1",full_report_ref={
        "path":str(path.relative_to(root)),"sha256":hashlib.sha256(path.read_bytes()).hexdigest()},
        baseline_forecast_matches=sum("M0" in row["scores"] for row in entries),
        bulk_fields_stored_in_full_report=["replay","experiments","scored_matches"])
    (path.parent.parent/"latest.json").write_text(json.dumps(summary,ensure_ascii=False,indent=2)+"\n")
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("study")
    run.add_argument("--runtime", default="runtime")
    run.add_argument("--league", default="serie_a")
    run.add_argument("--protocol", default="feature-models.json")
    run.add_argument("--source-commit", required=True)
    run.add_argument("--state-commit")
    run.add_argument("--run-goalmodel", action="store_true")
    args = parser.parse_args(argv)
    protocol = _read(args.protocol)
    if protocol["league"] != args.league or protocol.get("confirmation_active") or protocol.get("production_promotion"):
        parser.error("this entry is a frozen single-league development protocol, not activation")
    report = study(args.runtime,args.league,protocol,source_commit=args.source_commit,
        state_commit=args.state_commit,run_goalmodel=args.run_goalmodel)
    complete=(not args.run_goalmodel or report["goalmodel_execution"]["status"]=="executed_on_real_data") and report["latest_fit"]["status"]=="fitted"
    print(json.dumps({"status":"completed_development" if complete else "partial_development", "league":args.league,
        "forecast_matches":sum(bool(row["scores"]) for row in report["scored_matches"]),
        "requested_matches":report["replay"]["coverage"]["requested_matches"], "comparisons":report["comparisons"],
        "model_card_path":report["model_card_path"],"latest_fit":report["latest_fit"],
        "goalmodel_execution":report["goalmodel_execution"],"accuracy_improvement_verified":False},ensure_ascii=False))
    return 0 if complete else 1


if __name__ == "__main__":
    raise SystemExit(main())
