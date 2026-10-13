"""Preregistered, terminal-only confirmation of independently frozen S3 forecasts.

Development evidence can open a trial; it cannot constitute confirmation. Each
activation, cohort admission and terminal decision is immutable. Neither daily
monitoring nor a favorable result promotes a model to production.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
from pathlib import Path
from statistics import mean, variance

from scipy.stats import chi2, norm, t

VERSION = "advanced-confirmation-v1"
METRICS = ("scoreline_nll", "one_x_two_brier", "total_goal_nll", "home_tail_brier", "away_tail_brier")


class ConfirmationError(ValueError):
    pass


def _raw(value):
    return (json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n").encode()


def digest(value):
    # Shared with feature_research/advanced protocol namespace (no final newline).
    return hashlib.sha256(_raw(value)[:-1]).hexdigest()


def _time(value):
    if isinstance(value, datetime):
        result = value
    else:
        try:
            result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError as exc:
            raise ConfirmationError("invalid_timestamp") from exc
    if result.tzinfo is None:
        raise ConfirmationError("timezone_required")
    return result.astimezone(timezone.utc)


def _iso(value):
    return _time(value).isoformat().replace("+00:00", "Z")


def _read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _ref(root, path):
    return {"path": Path(path).resolve().relative_to(Path(root).resolve()).as_posix(),
            "sha256": hashlib.sha256(Path(path).read_bytes()).hexdigest()}


def _read_ref(root, ref):
    if not isinstance(ref, dict) or not isinstance(ref.get("path"), str):
        raise ConfirmationError("invalid_reference")
    relative = Path(ref["path"])
    path = (Path(root) / relative).resolve()
    if relative.is_absolute() or ".." in relative.parts or not path.is_relative_to(Path(root).resolve()):
        raise ConfirmationError("unsafe_reference")
    if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != ref.get("sha256"):
        raise ConfirmationError("reference_hash_mismatch")
    return _read(path)


def _read_development_artifact(runtime, ref):
    # Research storage transparently verifies every immutable chunk and object;
    # activation must never consume a partially materialized report/card.
    from .advanced_storage import load_report
    try:
        return load_report(runtime, ref)
    except (ValueError, OSError, KeyError, TypeError) as exc:
        raise ConfirmationError("development_artifact_integrity: " + str(exc)) from exc


def _save(root, category, value):
    data = _raw(value)
    path = Path(root) / category / (hashlib.sha256(data).hexdigest() + ".json")
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("xb") as handle:
            handle.write(data)
    except FileExistsError:
        if path.read_bytes() != data:
            raise ConfirmationError("immutable_collision")
    return path


def _pointer(path, value, *, first=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if first:
        try:
            with path.open("xb") as handle:
                handle.write(_raw(value))
        except FileExistsError:
            if _read(path) != value:
                raise ConfirmationError("immutable_pointer_changed")
    else:
        temporary = path.with_suffix(".tmp")
        temporary.write_bytes(_raw(value))
        temporary.replace(path)


def validate_design(design):
    if design.get("schema_version") != VERSION:
        raise ConfirmationError("unsupported_confirmation_design")
    if (design.get("primary_metric") != "scoreline_nll" or design.get("production_promotion") is not False
            or design.get("stopping_rule") != "first_fixed_n_queue_opportunities_terminal_only"
            or design.get("unit") != "unique_fixture_within_candidate_horizon"
            or design.get("family_control") != "bonferroni_all_registered_league_candidate_horizons_and_metrics"):
        raise ConfirmationError("confirmation_safety_contract_changed")
    for key in ("alpha", "power", "min_pair_coverage", "min_candidate_coverage", "pilot_variance_lower_quantile"):
        value = design.get(key)
        if type(value) not in (float, int) or not math.isfinite(value) or not 0 < value < 1:
            raise ConfirmationError("invalid_design_" + key)
    for key in ("minimum_effect_nll", "start_delay_hours", "max_calendar_days", "result_maturity_hours"):
        if type(design.get(key)) not in (int, float) or not math.isfinite(design[key]) or design[key] <= 0:
            raise ConfirmationError("invalid_design_" + key)
    if type(design.get("min_pilot_matches")) is not int or design["min_pilot_matches"] < 30:
        raise ConfirmationError("invalid_minimum_pilot")
    leagues = design.get("registered_leagues")
    if not isinstance(leagues, list) or not leagues or len(set(leagues)) != len(leagues) or any(not isinstance(k, str) or not k for k in leagues):
        raise ConfirmationError("invalid_registered_league_family")
    margins = design.get("noninferiority_margins", {})
    if set(margins) != set(METRICS) - {"scoreline_nll"} or any(
            type(v) not in (int, float) or not math.isfinite(v) or v <= 0 for v in margins.values()):
        raise ConfirmationError("invalid_guardrail_margins")
    return design


def project_metrics(score):
    if not isinstance(score, dict):
        raise ConfirmationError("score_missing")
    result = {key: score.get(key) for key in METRICS[:3]}
    for side in ("home", "away"):
        key = side + "_tail_brier"
        if key in score:
            result[key] = score[key]
        else:
            events = score.get("events", {})
            values = []
            for tail in (2, 3):
                event = events.get(side + "_net_ge" + str(tail), {})
                p, y = event.get("probability"), event.get("observed")
                if type(p) not in (float, int) or not math.isfinite(p) or not 0 <= p <= 1 or type(y) is not int or y not in (0, 1):
                    raise ConfirmationError("tail_metric_missing_or_invalid")
                values.append((p - y) ** 2)
            result[key] = mean(values)
    for key, value in result.items():
        if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
            raise ConfirmationError("invalid_metric_" + key)
        if "brier" in key and value > (2 if key == "one_x_two_brier" else 1):
            raise ConfirmationError("invalid_metric_bound_" + key)
    return result


def _differences(baseline, candidate):
    base, other = project_metrics(baseline), project_metrics(candidate)
    return {key: other[key] - base[key] for key in METRICS}


def sample_plan(differences, design, family_size):
    """Paired normal planning with an upper confidence bound on pilot variance.

This is a planning approximation, not a guarantee of empirical power. Guardrail
planning assumes true difference zero; primary assumes the minimum target gain.
The terminal decision itself uses one-sided Student t confidence bounds.
"""
    validate_design(design)
    if type(family_size) is not int or family_size < 1:
        raise ConfirmationError("invalid_family_size")
    n = len(differences)
    if n < design["min_pilot_matches"]:
        raise ConfirmationError("insufficient_pilot_matches")
    per_test_alpha = design["alpha"] / (family_size * len(METRICS))
    quantile = float(chi2.ppf(design["pilot_variance_lower_quantile"], n - 1))
    if not math.isfinite(quantile) or quantile <= 0:
        raise ConfirmationError("invalid_variance_quantile")
    zsum = float(norm.ppf(1 - per_test_alpha) + norm.ppf(design["power"]))
    components = {}
    for key in METRICS:
        values = [row[key] for row in differences]
        if not all(math.isfinite(v) for v in values):
            raise ConfirmationError("nonfinite_pilot")
        variance_upper = (n - 1) * variance(values) / quantile
        delta = design["minimum_effect_nll"] if key == "scoreline_nll" else design["noninferiority_margins"][key]
        required = max(design["min_pilot_matches"], math.ceil(variance_upper * (zsum / delta) ** 2))
        components[key] = {"pilot_n": n, "pilot_mean_difference": mean(values),
            "pilot_variance": variance(values), "planning_variance_upper": variance_upper,
            "assumed_detectable_effect_or_margin": delta, "required_scored_pairs": required}
    required_pairs = max(x["required_scored_pairs"] for x in components.values())
    return {"family_size": family_size, "metric_tests_per_hypothesis": len(METRICS),
        "per_test_alpha": per_test_alpha, "target_power": design["power"],
        "required_scored_pairs": required_pairs,
        "required_queue_opportunities": math.ceil(required_pairs / design["min_pair_coverage"]),
        "components": components, "method": "paired_normal_upper_pilot_variance_then_terminal_student_t",
        "qualification": "approximate_planning_power_not_guaranteed;fixture_dependence_not_eliminated"}


def development_gate(report, candidate_id, horizon, design, family_size):
    """Recompute the entire policy pool, including baseline fallbacks, for gating."""
    validate_design(design)
    rows = [r for r in report.get("scored_matches", report.get("rows", [])) if r.get("horizon", "EARLY") == horizon]
    all_rows = rows
    rows = [r for r in all_rows if r.get("candidate_failures", {}).get("M0", {}).get("code") != "insufficient_training_history"]
    failures, seen, diffs = [], set(), []
    native = baseline = 0
    for row in rows:
        identity = row.get("match_id")
        if identity is None or identity in seen:
            failures.append("duplicate_or_missing_development_fixture")
            continue
        seen.add(identity)
        scores = row.get("scores", {})
        if "M0" not in scores:
            continue
        baseline += 1
        if candidate_id in scores:
            native += 1
        policy = scores.get(candidate_id + "_policy", scores.get(candidate_id, scores["M0"]))
        try:
            diffs.append(_differences(scores["M0"], policy))
        except ConfirmationError as exc:
            failures.append(str(exc))
    if len(diffs) < design["min_pilot_matches"]:
        failures.append("insufficient_valid_development_pairs")
    pair_coverage = len(diffs) / len(rows) if rows else 0.0
    native_coverage = native / baseline if baseline else 0.0
    if pair_coverage < design["min_pair_coverage"]:
        failures.append("development_pair_coverage_below_gate")
    if native_coverage < design["min_candidate_coverage"]:
        failures.append("development_candidate_coverage_below_gate")
    means = {key: mean(r[key] for r in diffs) for key in METRICS} if diffs else {}
    if means and means["scoreline_nll"] > -design["minimum_effect_nll"]:
        failures.append("development_minimum_primary_gain_not_met")
    for key, margin in design["noninferiority_margins"].items():
        if means and means[key] >= margin:
            failures.append("development_guardrail_failed_" + key)
    plan = sample_plan(diffs, design, family_size) if len(diffs) >= design["min_pilot_matches"] else None
    return {"status": "passed" if not failures else "blocked_pending_development",
        "candidate_id": candidate_id, "horizon": horizon, "failures": sorted(set(failures)),
        "all_development_opportunities": len(all_rows), "readiness_opportunities": len(rows),
        "explicit_initial_warmup_opportunities": len(all_rows) - len(rows), "unique_fixture_count": len(seen),
        "paired_count": len(diffs), "native_candidate_count": native,
        "pair_coverage": pair_coverage, "native_candidate_coverage": native_coverage,
        "mean_policy_differences": means, "sample_plan": plan,
        "qualification": "development_gate_only_not_confirmed_effect"}


def verify_development(runtime, report, card, *, protocol, league, candidate_id, horizon, now):
    """Check actual frozen inputs/card and recompute all registered pilot scores."""
    from .feature_research import score_forecast
    from .model import score_distribution
    from .feature_correction import predict_correction
    from .advanced_research import predict_saved_goalmodel, candidate_feature_names, _side_values, implementation_fingerprint
    if (report.get("schema_version") != "advanced-research-v1" or report.get("league") != league
            or report.get("purpose") != "historical_development_only"
            or report.get("protocol_hash", report.get("protocol_sha256")) != digest(protocol)
            or digest(report.get("protocol")) != digest(protocol)):
        raise ConfirmationError("development_report_contract_invalid")
    implementation = implementation_fingerprint()
    if report.get("implementation_fingerprint") != implementation or card.get("implementation_fingerprint") != implementation:
        raise ConfirmationError("development_implementation_fingerprint_mismatch")
    if _time(report.get("generated_at")) > now:
        raise ConfirmationError("development_report_from_future")
    if (card.get("fit_status") != "fitted" or card.get("candidate_id") != candidate_id
            or card.get("league") != league or horizon not in card.get("prediction_horizons", [card.get("horizon")])
            or card.get("protocol_hash") != digest(protocol)
            or card.get("source_commit") != report.get("source_commit")
            or not report.get("source_commit")):
        raise ConfirmationError("candidate_card_contract_invalid")
    if _time(card.get("training_cutoff")) > now or _time(card.get("generated_at")) > now:
        raise ConfirmationError("candidate_card_from_future")
    if card.get("model_hash") != digest({k: v for k, v in card.items() if k != "model_hash"}):
        raise ConfirmationError("candidate_card_hash_invalid")
    refs = report.get("source_refs", []) + card.get("source_refs", [])
    if not refs:
        raise ConfirmationError("development_frozen_sources_missing")
    sources = [_read_development_artifact(runtime, ref) for ref in refs]
    source_matches = {}
    for source in sources:
        if isinstance(source, dict) and isinstance(source.get("matches"), list):
            for match in source["matches"]:
                source_matches[match["match_id"]] = match
    if not source_matches:
        raise ConfirmationError("development_match_source_missing")
    replay = report.get("replays", {}).get(horizon, {})
    forecasts = {f["match_id"]: f for f in replay.get("forecasts", [])}
    failures = {f["match_id"]: f for f in replay.get("failures", [])}
    rows = [r for r in report.get("scored_matches", []) if r.get("horizon") == horizon]
    if (len({r.get("match_id") for r in rows}) != len(rows)
            or {r.get("match_id") for r in rows} != set(forecasts) | set(failures)):
        raise ConfirmationError("development_allqueue_identity_mismatch")
    fits = {f["block"]: f for f in replay.get("baseline_fits", [])}
    experiments = {(x["horizon"], x["block"]): x for x in report.get("experiments", [])}
    features = {f["match_id"]: f for f in report.get("feature_datasets", {}).get(horizon, {}).get("rows", [])}
    early_replay = {f["match_id"]: f for f in report.get("replays", {}).get("EARLY", {}).get("forecasts", [])}
    def same_prediction(saved, calculated):
        import numpy as np
        if saved.get("score_matrix_axes") != calculated.get("score_matrix_axes"):
            raise ConfirmationError("development_model_prediction_axes_mismatch")
        x, y = np.asarray(saved["score_matrix"], dtype=float), np.asarray(calculated["score_matrix"], dtype=float)
        if x.shape != y.shape or not np.allclose(x, y, atol=1e-10, rtol=0):
            raise ConfirmationError("development_model_prediction_does_not_recompute")
        if set(saved.get("probabilities", {})) != set(calculated.get("probabilities", {})) or any(
                abs(saved["probabilities"][key] - value) > 1e-10 for key, value in calculated["probabilities"].items()):
            raise ConfirmationError("development_model_probabilities_do_not_recompute")
        for side in ("home", "away"):
            if abs(saved["expected_goals"][side] - calculated["expected_goals"][side]) > 1e-10:
                raise ConfirmationError("development_model_rate_does_not_recompute")
    for row in rows:
        mid = row["match_id"]
        fixture = source_matches.get(mid)
        if not fixture or row.get("kickoff") != fixture.get("kickoff"):
            raise ConfirmationError("development_fixture_source_mismatch")
        scores = row.get("scores", {})
        if mid not in forecasts:
            if "M0" in scores or row.get("candidate_failures", {}).get("M0", {}).get("code") != failures[mid].get("code"):
                raise ConfirmationError("development_failure_changed")
            continue
        f = forecasts[mid]
        parent = fits.get(f.get("block"), {})
        if parent.get("status") != "fitted" or "model" not in parent:
            raise ConfirmationError("development_baseline_fit_missing")
        parent_cutoff = _time(parent["model"]["training_cutoff"])
        if parent_cutoff > _time(f["cutoff_at"]) or mid in parent.get("training_match_ids", []):
            raise ConfirmationError("development_baseline_training_leakage")
        for training_id in parent.get("training_match_ids", []):
            if training_id not in source_matches or _time(source_matches[training_id]["kickoff"]) + timedelta(hours=4) > parent_cutoff:
                raise ConfirmationError("development_baseline_training_result_not_available")
        regenerated = score_distribution(parent["model"], f["home_id"], f["away_id"], f["kickoff"], f["neutral"])
        regenerated.update(match_id=mid, cutoff_at=f["cutoff_at"])
        same_prediction(f["baseline_prediction"], regenerated)
        baseline = score_forecast(regenerated, fixture, extended_diagnostics=True)
        expected = {"M0": baseline}
        if candidate_id in scores:
            prediction = row.get("predictions", {}).get(candidate_id)
            if prediction is None:
                raise ConfirmationError("development_candidate_prediction_missing")
            names = candidate_feature_names(protocol, candidate_id)
            values = _side_values(features.get(mid, {}), names)
            if candidate_id in protocol.get("goalmodel_candidates", []):
                early = early_replay.get(mid, {})
                experiment = experiments.get(("EARLY", early.get("block")), {})
                result = experiment.get("goalmodel_runs", {}).get(candidate_id, {})
                if result.get("status") != "ok" or "fit" not in result:
                    raise ConfirmationError("development_goalmodel_fit_missing")
                if candidate_id == "G0":
                    _side_values(features.get(mid, {}), candidate_feature_names(protocol, "G1"))
                saved_fit = {"kind": "goalmodel_saved_fit", "fit_status": "fitted", "dc": False,
                    "training_cutoff": result["cutoff"], "training_match_ids": result["training_match_ids"],
                    "feature_names": names, "goalmodel_fit": result["fit"]}
                regenerated_candidate = predict_saved_goalmodel(saved_fit, f, values, f["cutoff_at"])
            else:
                experiment = experiments.get((horizon, f.get("block")), {})
                fitted = experiment.get("fits", {}).get(candidate_id, {}).get("model")
                if fitted is None:
                    raise ConfirmationError("development_offset_fit_missing")
                regenerated_candidate = predict_correction(fitted, regenerated, values["home"], values["away"])
            same_prediction(prediction, regenerated_candidate)
            expected[candidate_id] = score_forecast(regenerated_candidate, fixture, extended_diagnostics=True)
        elif candidate_id in row.get("predictions", {}):
            raise ConfirmationError("development_candidate_score_removed")
        expected[candidate_id + "_policy"] = expected.get(candidate_id, baseline)
        for key, actual in expected.items():
            actual_metrics, saved = project_metrics(actual), project_metrics(scores.get(key))
            if any(abs(actual_metrics[metric] - saved[metric]) > 1e-12 for metric in METRICS):
                raise ConfirmationError("development_scores_failed_recomputation")
    return {"verified": True, "candidate_id": candidate_id, "horizon": horizon,
            "source_files_checked": len(refs), "allqueue_rows_checked": len(rows),
            "baseline_predictions_recomputed": len(forecasts)}


def activate(runtime, root, *, design, advanced_protocol, league, candidate_id, horizon,
             development_ref, card_ref, family_size, now):
    """Only this gate can write an activation; no historical trial backdating."""
    now = _time(now)
    report = _read_development_artifact(runtime, development_ref)
    card = _read_development_artifact(runtime, card_ref)
    if report.get("protocol_sha256", report.get("protocol_hash")) != digest(advanced_protocol):
        raise ConfirmationError("development_protocol_mismatch")
    if not isinstance(card, dict) or card.get("candidate_id", candidate_id) != candidate_id:
        raise ConfirmationError("candidate_card_identity_mismatch")
    gate = development_gate(report, candidate_id, horizon, design, family_size)
    if gate["status"] != "passed":
        return None, gate
    verification = verify_development(runtime, report, card, protocol=advanced_protocol, league=league,
                                      candidate_id=candidate_id, horizon=horizon, now=now)
    gate["source_and_score_verification"] = verification
    path = Path(root) / "activation.json"
    if not path.exists() and list((Path(root) / "activations").glob("*.json")):
        raise ConfirmationError("activation_pointer_missing_cannot_restart")
    if path.exists():
        existing = _read_ref(runtime, _read(path))
        if (existing["advanced_protocol_sha256"] != digest(advanced_protocol)
                or existing["design_sha256"] != digest(design)):
            raise ConfirmationError("activation_policy_changed")
        return existing, gate
    created = now
    start = created + timedelta(hours=design["start_delay_hours"])
    value = {"schema_version": VERSION, "status": "active_waiting_real_results",
        "league": league, "candidate_id": candidate_id, "horizon": horizon,
        "created_at": _iso(created), "future_start": _iso(start),
        "last_kickoff": _iso(start + timedelta(days=design["max_calendar_days"])),
        "design": design, "design_sha256": digest(design),
        "advanced_protocol_sha256": digest(advanced_protocol),
        "implementation_fingerprint": report["implementation_fingerprint"],
        "development_ref": development_ref, "development_gate": gate,
        "anchor_model_card_ref": card_ref, "sample_plan": gate["sample_plan"],
        "cohort_rule": "first_observed_queue_order;before_kickoff;all_failures_included",
        "candidate_update_rule": "fixed_protocol_recipe;past_only_rolling_refits_allowed;each_card_hash_retained",
        "confirmation_metrics_unread_until_terminal": True, "production_promotion": False,
        "roi_inference": "not_assessed_probability_trial_has_no_execution_contract"}
    saved = _save(root, "activations", value)
    _pointer(path, _ref(runtime, saved), first=True)
    return value, gate


def _identity(row):
    return (str(row.get("fixture_id")), row.get("horizon"), row.get("candidate_id"))


def _meta(row):
    return {key: row.get(key) for key in ("fixture_id", "league", "horizon", "candidate_id", "kickoff", "first_observed_at")}


def _select_metadata(rows, activation):
    selected, rejected, seen = [], [], set()
    for row in rows:
        if (row.get("league"), row.get("candidate_id"), row.get("horizon")) != (
                activation["league"], activation["candidate_id"], activation["horizon"]):
            continue
        identity = _identity(row)
        if identity in seen:
            raise ConfirmationError("duplicate_fixture_in_queue")
        seen.add(identity)
        try:
            observed, kickoff = _time(row["first_observed_at"]), _time(row["kickoff"])
            if observed < _time(activation["created_at"]):
                reason = "pre_activation_queue_not_retroactively_enrolled"
            elif kickoff < _time(activation["future_start"]) + timedelta(hours=11):
                reason = "outside_future_start_horizon"
            elif kickoff > _time(activation["last_kickoff"]):
                reason = "after_calendar_end"
            elif observed >= kickoff:
                reason = "first_queue_observation_after_kickoff"
            else:
                selected.append(row)
                continue
        except (KeyError, ConfirmationError):
            reason = "invalid_queue_time"
        rejected.append({**_meta(row), "reason": reason})
    selected.sort(key=lambda r: (_time(r["first_observed_at"]), _time(r["kickoff"]), str(r["fixture_id"])))
    return selected, rejected


def verify_durable_capture(runtime, row, now):
    """Re-prove Git-tree persistence; a JSON claim of verification is insufficient."""
    from .advanced_durability import prove
    import re
    import subprocess
    proof = _read_ref(runtime, row.get("durability_ref"))
    anchor = row.get("first_capture_anchor_ref")
    if not anchor or proof.get("receipt_ref") != anchor:
        raise ConfirmationError("durable_capture_anchor_mismatch")
    commit = proof.get("state_commit", "")
    if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ConfirmationError("durable_state_commit_invalid")
    repo = Path(runtime).resolve().parent
    exists = subprocess.run(["git", "-C", str(repo), "cat-file", "-e", commit + "^{commit}"],
        capture_output=True, timeout=30)
    if exists.returncode:
        # Only the known repository's existing origin authentication is used.
        # No credential path or process environment is ever inspected.
        fetched = subprocess.run(["git", "-C", str(repo), "fetch", "--no-tags", "origin", commit],
            capture_output=True, timeout=60)
        if fetched.returncode:
            raise ConfirmationError("durable_state_commit_unavailable")
    actual = prove(repo, runtime, commit, anchor)
    if actual != proof:
        raise ConfirmationError("durable_proof_does_not_recompute")
    saved = _time(actual["durable_saved_at"])
    if not _time(row["captured_at"]) <= saved < _time(row["kickoff"]) or saved > now:
        raise ConfirmationError("capture_not_durably_saved_before_kickoff")
    return actual


def monitor(runtime, root, activation, rows, *, now, scored_rows_loader=None):
    """Enroll metadata first. Score loader is never called before terminal maturity."""
    now = _time(now)
    if activation["design_sha256"] != digest(activation["design"]):
        raise ConfirmationError("activation_design_corrupt")
    from .advanced_research import implementation_fingerprint
    if activation.get("implementation_fingerprint") != implementation_fingerprint():
        raise ConfirmationError("activation_implementation_changed_new_protocol_required")
    design = validate_design(activation["design"])
    terminal_pointer = Path(root) / "terminal.json"
    if terminal_pointer.exists():
        terminal = _read_ref(runtime, _read(terminal_pointer))
        if terminal.get("activation_sha256") != digest(activation):
            raise ConfirmationError("terminal_activation_mismatch")
        return terminal
    eligible, rejected = _select_metadata(rows, activation)
    required = activation["sample_plan"]["required_queue_opportunities"]
    cohort_path = Path(root) / "cohort.json"
    existing = _read(cohort_path) if cohort_path.exists() else []
    ids = {_identity(r) for r in existing}
    recorded = list((Path(root) / "enrollments").glob("*.json"))
    if len(ids) != len(existing) or {r["enrollment_ref"]["path"] for r in existing} != {_ref(runtime, path)["path"] for path in recorded}:
        raise ConfirmationError("cohort_entries_deleted_or_duplicated")
    for row in eligible:
        if len(existing) >= required:
            break
        if _identity(row) in ids:
            continue
        enrolled = {**_meta(row), "enrolled_at": _iso(now), "enrollment_status": "prospective"}
        if _time(row["kickoff"]) <= now:
            # The deterministic first-N queue slot is consumed, never replaced
            # by a more convenient later fixture. Its prediction cannot count.
            enrolled["enrollment_status"] = "missed_enrollment"
            rejected.append({**_meta(row), "reason": "enrollment_not_saved_before_kickoff"})
        saved = _save(root, "enrollments", enrolled)
        enrolled["enrollment_ref"] = _ref(runtime, saved)
        existing.append(enrolled)
        ids.add(_identity(row))
    _pointer(cohort_path, existing)
    for row in existing:
        origin = _read_ref(runtime, row["enrollment_ref"])
        if {k: v for k, v in row.items() if k != "enrollment_ref"} != origin:
            raise ConfirmationError("cohort_enrollment_changed")
    completed = len(existing) >= required
    calendar_expired = now >= _time(activation["last_kickoff"]) + timedelta(hours=design["result_maturity_hours"])
    maturity_at = max((_time(r["kickoff"]) for r in existing), default=_time(activation["last_kickoff"])) + timedelta(hours=design["result_maturity_hours"])
    status = {"schema_version": VERSION, "league": activation["league"], "candidate_id": activation["candidate_id"],
        "horizon": activation["horizon"], "generated_at": _iso(now),
        "activation_sha256": digest(activation), "enrolled_unique_fixtures": len(existing),
        "required_queue_opportunities": required, "required_scored_pairs": activation["sample_plan"]["required_scored_pairs"],
        "excluded_metadata_counts": dict(Counter(r["reason"] for r in rejected)),
        "excluded_metadata": rejected, "production_promotion": False, "roi_claim": False}
    if not (calendar_expired or (completed and now >= maturity_at)):
        status.update(status="active_waiting_real_results", metrics=None, terminal=False,
                      next_gate="fixed_queue_count_and_final_kickoff_plus_maturity",
                      scores_accessed=False)
        return status
    if scored_rows_loader is None:
        raise ConfirmationError("terminal_score_loader_required")
    rows_with_scores = scored_rows_loader()
    lookup = {}
    for row in rows_with_scores:
        key = _identity(row)
        if key in lookup:
            raise ConfirmationError("duplicate_fixture_in_terminal_scores")
        lookup[key] = row
    pairs, native, outcomes, missing = [], 0, Counter(), []
    for enrolled in existing:
        if enrolled.get("enrollment_status") != "prospective":
            outcomes["missed_enrollment"] += 1
            missing.append(enrolled["fixture_id"])
            continue
        row = lookup.get(_identity(enrolled))
        if row is None:
            outcomes["missing_queue_row"] += 1
            missing.append(enrolled["fixture_id"])
            continue
        try:
            if row.get("kickoff") != enrolled["kickoff"]:
                raise ConfirmationError("kickoff_revision_excluded")
            if (row.get("confirmation_eligible") is not True
                    or row.get("confirmation_activation_sha256") != digest(activation)):
                raise ConfirmationError("capture_not_frozen_for_this_confirmation")
            if _read_ref(runtime, row.get("confirmation_activation_ref")) != activation:
                raise ConfirmationError("capture_activation_parent_mismatch")
            if row.get("protocol_sha256") != activation["advanced_protocol_sha256"]:
                raise ConfirmationError("future_protocol_mismatch")
            if not row.get("captured_at") or _time(row["captured_at"]) < _time(activation["future_start"]):
                raise ConfirmationError("capture_before_trial_start_or_missing")
            if _time(row["captured_at"]) >= _time(row["kickoff"]):
                raise ConfirmationError("capture_not_prematch")
            if not row.get("result_available_at") or _time(row["result_available_at"]) > now:
                raise ConfirmationError("result_not_available")
            if _time(row["result_available_at"]) < _time(row["kickoff"]) + timedelta(hours=4):
                raise ConfirmationError("result_maturity_invalid")
            verify_durable_capture(runtime, row, now)
            baseline, policy = row.get("baseline_metrics"), row.get("strategy_metrics")
            if baseline is None or policy is None:
                raise ConfirmationError("no_legal_policy_pair")
            difference = _differences(baseline, policy)
            if row.get("candidate_metrics") is not None:
                _differences(baseline, row["candidate_metrics"])
                native += 1
            pairs.append(difference)
            outcomes[row.get("status", "scored")] += 1
        except (ConfirmationError, ValueError, OSError, KeyError) as exc:
            outcomes[str(exc)] += 1
            missing.append(enrolled["fixture_id"])
    coverage = len(pairs) / len(existing) if existing else 0
    native_coverage = native / len(pairs) if pairs else 0
    failure_reasons = []
    if not completed:
        failure_reasons.append("calendar_limit_before_required_cohort")
    if len(pairs) < activation["sample_plan"]["required_scored_pairs"]:
        failure_reasons.append("insufficient_terminal_pairs")
    if coverage < design["min_pair_coverage"]:
        failure_reasons.append("terminal_pair_coverage_failed")
    if native_coverage < design["min_candidate_coverage"]:
        failure_reasons.append("terminal_candidate_coverage_failed")
    estimates = {}
    if len(pairs) > 1:
        alpha = activation["sample_plan"]["per_test_alpha"]
        critical = float(t.ppf(1 - alpha, len(pairs) - 1))
        for metric in METRICS:
            values = [p[metric] for p in pairs]
            avg = mean(values)
            upper = avg + critical * math.sqrt(variance(values) / len(values))
            threshold = 0.0 if metric == "scoreline_nll" else design["noninferiority_margins"][metric]
            passed = upper < threshold
            if metric == "scoreline_nll":
                passed = passed and avg <= -design["minimum_effect_nll"]
            estimates[metric] = {"mean_candidate_minus_baseline": avg,
                "one_sided_upper_confidence_bound": upper, "per_test_alpha": alpha,
                "threshold": threshold, "passed": passed, "n_unique_fixtures": len(values)}
            if not passed:
                failure_reasons.append("terminal_metric_failed_" + metric)
    else:
        failure_reasons.append("insufficient_terminal_variance")
    status.update(status="terminal_passed_probability_confirmation" if not failure_reasons else "terminal_not_confirmed",
        terminal=True, scores_accessed=True, metrics=estimates, failures=failure_reasons,
        paired_fixtures=len(pairs), native_candidate_fixtures=native, pair_coverage=coverage,
        native_candidate_coverage=native_coverage, all_queue_outcomes=dict(outcomes), missing_fixture_ids=missing,
        qualification="paired_fixture_t_inference_assumes_independent_fixture_differences;no_profit_or_production_claim",
        result_revision_policy="terminal_original_preserved;later_corrections_require_separate_audit_not_retest")
    saved = _save(root, "terminals", status)
    _pointer(terminal_pointer, _ref(runtime, saved), first=True)
    return status


def _candidate_specs(protocol):
    candidates = protocol.get("candidates", {})
    if isinstance(candidates, dict):
        specs = [(key, value.get("horizon", "EARLY")) for key, value in candidates.items()]
    else:
        specs = [(item["candidate_id"], item.get("horizon", "EARLY")) for item in candidates]
    specs += [(key, horizon) for key in protocol.get("goalmodel_candidates", [])
              for horizon in protocol.get("goalmodel_horizons", ["EARLY"])]
    import re
    if len(specs) != len(set(specs)) or any(
            not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", key)
            or horizon not in ("EARLY", "LINEUP") for key, horizon in specs):
        raise ConfirmationError("duplicate_or_invalid_candidate_horizon")
    return specs


def run(runtime, league, advanced_protocol, design, *, auto_activate=False, clock=None, row_loader=None):
    """Continuous orchestration entry point; blocked trials still produce receipts."""
    validate_design(design)
    from .advanced_research import implementation_fingerprint
    current_implementation = implementation_fingerprint()
    runtime = Path(runtime).resolve()
    now = _time((clock or (lambda: datetime.now(timezone.utc)))())
    protocol_hash = digest(advanced_protocol)
    root = runtime / "advanced_models" / league / protocol_hash
    report_pointer = root / "reports" / "latest.json"
    report_ref, report, development_error = None, None, None
    if report_pointer.exists():
        try:
            pointer = _read(report_pointer)
            report_ref = pointer.get("full_report_ref", pointer.get("report_ref"))
            report = _read_development_artifact(runtime, report_ref)
        except (OSError, ValueError, TypeError) as exc:
            development_error = str(exc)
    if row_loader is None:
        from .advanced_scoring import verified_confirmation_rows
        row_loader = lambda include_scores: verified_confirmation_rows(runtime, league, advanced_protocol, include_scores=include_scores)
    specs = _candidate_specs(advanced_protocol)
    leagues = design["registered_leagues"]
    if league not in leagues:
        raise ConfirmationError("league_not_in_frozen_confirmation_family")
    family_size = len(specs) * len(leagues)
    observations = None
    results = []
    for candidate_id, horizon in specs:
        trial = root / "confirmation" / digest(design) / candidate_id / horizon
        activation_path = trial / "activation.json"
        try:
            if activation_path.exists():
                activation = _read_ref(runtime, _read(activation_path))
                if activation.get("implementation_fingerprint") != current_implementation:
                    raise ConfirmationError("activation_implementation_changed_new_protocol_required")
                if (activation["advanced_protocol_sha256"] != protocol_hash or activation["design_sha256"] != digest(design)
                        or (activation.get("league"), activation.get("candidate_id"), activation.get("horizon")) != (league, candidate_id, horizon)):
                    raise ConfirmationError("activation_policy_changed")
            else:
                activation = None
            gate = None
            if activation is None:
                if development_error is not None:
                    raise ConfirmationError(development_error)
                if report is None:
                    gate = {"status": "blocked_pending_development", "failures": [development_error or "development_report_unavailable"]}
                elif report.get("implementation_fingerprint") != current_implementation:
                    gate = {"status": "blocked_pending_development", "failures": ["implementation_changed_requires_fresh_development_study"],
                        "report_implementation_sha256": (report.get("implementation_fingerprint") or {}).get("sha256"),
                        "current_implementation_sha256": current_implementation["sha256"]}
                else:
                    gate = development_gate(report, candidate_id, horizon, design, family_size)
                    card_ref = report.get("model_cards", {}).get(candidate_id)
                    if card_ref is None:
                        gate["status"] = "blocked_pending_development"
                        gate["failures"].append("candidate_model_card_unavailable")
                    elif auto_activate and gate["status"] == "passed":
                        activation, gate = activate(runtime, trial, design=design, advanced_protocol=advanced_protocol,
                            league=league, candidate_id=candidate_id, horizon=horizon, development_ref=report_ref,
                            card_ref=card_ref, family_size=family_size, now=now)
                if activation is None:
                    result = {"candidate_id": candidate_id, "horizon": horizon, "status": gate["status"],
                        "gate": gate, "confirmation_active": False, "production_promotion": False,
                        "metrics": None, "scores_accessed": False}
                    if gate["status"] == "passed":
                        result["status"] = "ready_not_activated"
                    results.append(result)
                    continue
            if observations is None:
                observations = row_loader(False)
            result = monitor(runtime, trial, activation, observations, now=now,
                             scored_rows_loader=lambda: row_loader(True))
            results.append(result)
        except (OSError, ValueError, TypeError, KeyError) as exc:
            results.append({"candidate_id": candidate_id, "horizon": horizon,
                "status": "blocked_integrity_or_execution", "error": str(exc),
                "confirmation_active": activation_path.exists(), "production_promotion": False})
    summary = {"schema_version": VERSION, "league": league, "generated_at": _iso(now),
        "protocol_sha256": protocol_hash, "design_sha256": digest(design), "family_size": family_size,
        "auto_activation_requested": auto_activate, "trials": results, "production_promotion": False,
        "effect_claim": "only_terminal_passed_probability_confirmation_has_new_confirmatory_probability_evidence",
        "roi_claim": False}
    saved = _save(root, "confirmation/reports/history", summary)
    _pointer(root / "confirmation" / "latest.json", {**summary, "full_report_ref": _ref(runtime, saved)})
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", default="runtime")
    parser.add_argument("--league", required=True)
    parser.add_argument("--advanced-protocol", default="advanced-models-v1.json")
    parser.add_argument("--protocol", default="advanced-confirmation-v1.json")
    parser.add_argument("--auto-activate", action="store_true")
    args = parser.parse_args()
    design, protocol = _read(args.protocol), _read(args.advanced_protocol)
    leagues = design["registered_leagues"] if args.league == "all" else [args.league]
    reports = [run(args.runtime, league, protocol, design, auto_activate=args.auto_activate) for league in leagues]
    report = reports[0] if args.league != "all" else {"schema_version": VERSION, "leagues": reports, "production_promotion": False}
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if any(r["status"] == "blocked_integrity_or_execution" for item in reports for r in item["trials"]) else 0


if __name__ == "__main__":
    raise SystemExit(main())
