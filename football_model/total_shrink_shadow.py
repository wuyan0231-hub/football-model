"""Append-only F5-C total-intensity pilot; reuses a recorded production forecast.

No fit, API call, historical --as-of, production model update or promotion exists
in this module. The pilot is a new daily-production implementation, not a
confirmation sample of the weekly retrospective F5-v1 experiment.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
from pathlib import Path
import re

import numpy as np

from . import shadow_models as sm
from .data import normalize_fixtures
from .model import _distribution_from_rates, _iso, _timestamp, score_distribution
from .prospective import (_baseline, _digest, _forecast_valid, _identity, _json_bytes,
                          _name, _now, _objects, _path, _read, _same_identity,
                          _append as _unbudgeted_append, current_results)
from .settlement import matrix_array

VERSION = "s2-f5c-daily-total-shrink-pilot-1.0"
ROOT = "total_shrink_shadow"
ALPHAS = (0., .25, .5, .75, 1.)
LOOKBACK_DAYS = 180
MINIMUM_MATCHES = 40
RESULT_EMBARGO = timedelta(hours=48)
DERIVED_FIELDS = ("expected_goals", "probabilities", "score_matrix", "score_matrix_axes",
                  "tail_mass_before_normalization", "tail_tolerance", "matrix_renormalized")


def _root(runtime):
    return _path(Path(runtime), ROOT)


def _alpha_name(alpha):
    return "alpha_" + format(alpha, ".2f").rstrip("0").rstrip(".")


def shrink_prediction(prediction, mean, alpha):
    """Alpha zero preserves the entire native prediction, including metadata."""
    if type(alpha) not in (int, float) or alpha not in ALPHAS:
        raise ValueError("unsupported_frozen_alpha")
    for rates in (prediction["expected_goals"], mean):
        if (set(rates) != {"home", "away"} or any(type(v) not in (int, float)
                or not math.isfinite(v) or v <= 0 for v in rates.values())):
            raise ValueError("invalid_positive_rates")
    if alpha == 0:
        return deepcopy(prediction)
    rates = prediction["expected_goals"]
    total = math.fsum(rates.values())
    target = math.fsum(mean.values())
    shrunk = (1 - alpha) * total + alpha * target
    # Exactly the retrospective F5 formula, retaining the original home share.
    derived = _distribution_from_rates(shrunk * rates["home"] / total,
                                       shrunk * rates["away"] / total)
    result = deepcopy(prediction)
    result.update(derived)
    return result


def _total_loss(rate, goals):
    if (type(rate) not in (int, float) or not math.isfinite(rate) or rate <= 0
            or type(goals) is not int or goals < 0):
        raise ValueError("invalid_total_loss_inputs")
    return rate - goals * math.log(rate) + math.lgamma(goals + 1)


def _spec(config):
    if config.get("half_life_days") != 180:
        raise ValueError("registered_production_baseline_requires_180_days")
    return {"version": VERSION, "target": "regulation_time_90_plus_stoppage",
        "alpha_grid": list(ALPHAS), "lookback_days": LOOKBACK_DAYS,
        "production_ridge": config["ridge"], "production_min_train": config["min_train"],
        "minimum_selection_matches": MINIMUM_MATCHES, "result_embargo_hours": 48,
        "result_availability": "max_first_verified_independent_result_observed_and_captured_plus_48h",
        "league_mean": "same_production_training_digest_unweighted_goals_plus_0_5_over_n_plus_0_5",
        "base_model": "reuse_native_recorded_daily_production_180_no_fit",
        "selection": "past_minimum_untruncated_poisson_total_log_loss_tie_smallest_alpha",
        "history": "first_valid_f5c_pair_matching_latest_result_identity_available_at_cutoff",
        "evaluation": "first_valid_pair_matching_latest_final_identity_per_competition_match",
        "forecast_days": config["forecast_days"], "max_model_age_days": config["max_model_age_days"],
        "purpose": "engineering_pilot_only_until_independent_future_protocol_freeze",
        "confirmation_eligible": False, "automatic_promotion": False, "profit_evaluation": False,
        "new_fits": 0, "new_api_calls": 0,
        "retrospective_protocol_equivalence": False,
        "changes_from_historical_f5_v1": ["daily_recorded_production_instead_of_weekly_refit",
            "true_independent_result_first_observed_and_captured_plus_48h_instead_of_kickoff_plus_48h",
            "only_native_f5c_recorded_history_no_historical_backfill"]}


def _protocol(root, config, clock):
    spec = _spec(config)
    for record in _objects(root, "protocols"):
        if record.get("spec") == spec:
            if _timestamp(record["registered_at"], "registered_at") > _now(clock):
                raise ValueError("protocol_registered_in_future")
            return record["object_id"]
    return sm._append(root, "protocols", {"spec": spec, "registered_at": _iso(_now(clock))})


def _parent(runtime, root, production_record, row, data, data_sha, cutoff):
    """Freeze and verify the native production receipt and all native parents."""
    production = _path(Path(runtime), "prospective")
    clean = {k: v for k, v in production_record.items() if k != "object_id"}
    if _digest(clean) != production_record["object_id"]:
        raise ValueError("production_forecast_object_hash_mismatch")
    sources = {}
    for field in ("source_id", "report_source", "model_source"):
        sid = _name(clean[field])
        source, raw_sha = _read(production, "sources", sid + ".json")
        if _digest(source) != sid:
            raise ValueError("production_source_object_hash_mismatch")
        sources[field] = sm._append(root, "production_sources",
            {"object_id": sid, "original_bytes_sha256": raw_sha, "value": source})
    report = _load_object(root, "production_sources", sources["report_source"])["value"]["content"]
    saved_row = clean["forecast"]
    if (saved_row != row or not _same_identity(clean, row)
            or clean["competition"] != row["competition"]):
        raise ValueError("production_parent_not_current_row")
    captured = _timestamp(clean["captured_at"], "production.captured_at")
    if captured > cutoff:
        raise ValueError("production_parent_recorded_after_cutoff")
    _forecast_valid(saved_row, report, captured)
    data_id = _name(clean["baseline"]["source_id"])
    data_source, raw_sha = _read(production, "sources", data_id + ".json")
    if (_digest(data_source) != data_id or data_source.get("kind") != "normalized_regulation_data"
            or data_source["content"] != data
            or data_source["original_bytes_sha256"] != data_sha):
        raise ValueError("production_parent_data_source_mismatch")
    sources["data_source"] = sm._append(root, "production_sources",
        {"object_id": data_id, "original_bytes_sha256": raw_sha, "value": data_source})
    parent = {"production_forecast_id": production_record["object_id"], "forecast": clean,
              "sources": sources}
    _validate_parent(root, parent, cutoff)
    return sm._append(root, "production_parents", parent)


def _parent_source(root, parent, field):
    return _load_object(root, "production_sources", parent["sources"][field])


def _registered_model(root, parent, spec):
    model = _parent_source(root, parent, "model_source")["value"]["content"]["model"]
    if (model["ridge"] != spec["production_ridge"]
            or model["n_matches"] < spec["production_min_train"]):
        raise ValueError("total_shrink_registered_production_fit_parameters_mismatch")


def _validate_parent(root, parent, now):
    forecast = parent["forecast"]
    if _digest(forecast) != parent["production_forecast_id"]:
        raise ValueError("frozen_production_parent_hash_mismatch")
    expected = {"source_id": "prediction_file", "report_source": "run_report",
                "model_source": "model_bundle", "data_source": "normalized_regulation_data"}
    values = {}
    for field, kind in expected.items():
        source = _parent_source(root, parent, field)
        if (_digest(source["value"]) != source["object_id"]
                or source["value"].get("kind") != kind
                or not re.fullmatch(r"[0-9a-f]{64}", source.get("original_bytes_sha256", ""))):
            raise ValueError("frozen_production_source_hash_or_kind_mismatch")
        sid = forecast["baseline"]["source_id"] if field == "data_source" else forecast[field]
        if sid != source["object_id"]:
            raise ValueError("frozen_production_source_binding_mismatch")
        values[field] = source["value"]["content"]
    row, report, bundle, data = (forecast["forecast"], values["report_source"],
                                values["model_source"], values["data_source"])
    captured = _timestamp(forecast["captured_at"], "production.captured_at")
    if captured > now or not _same_identity(forecast, row) or forecast["competition"] != row["competition"]:
        raise ValueError("frozen_production_identity_or_time_invalid")
    _forecast_valid(row, report, captured)
    if (report["run_id"] != forecast["run_id"] or forecast["source_commit"] != report.get("source_commit")
            or row.get("status") != "exploratory_baseline"):
        raise ValueError("frozen_production_run_binding_mismatch")
    rows = [r for r in values["source_id"] if r.get("match_id") == row["match_id"]]
    if len(rows) != 1 or rows[0] != row:
        raise ValueError("frozen_production_prediction_file_mismatch")
    model = bundle["model"]
    if (bundle["model_id"] != row["model_id"] or bundle["competition"]["key"] != forecast["competition"]
            or model["half_life_days"] != 180 or model.get("optimizer", {}).get("parameter_at_bound")
            or model["training_data_sha256"] != row["prediction"]["training_data_sha256"]):
        raise ValueError("frozen_production_model_binding_mismatch")
    rebuilt = score_distribution(model, row["home_id"], row["away_id"], row["kickoff"],
                                 row["prediction"]["neutral"])
    if not sm._numeric_replay_equivalent(rebuilt, row["prediction"]):
        raise ValueError("native_production_prediction_does_not_recompute")
    baseline = _baseline(data, row, forecast["baseline"]["source_id"])
    original = forecast["baseline"]
    if (any(original.get(k) != v for k, v in baseline.items() if k != "prediction")
            or not sm._numeric_replay_equivalent(baseline["prediction"], original["prediction"])):
        raise ValueError("native_same_training_league_mean_mismatch")
    return row["prediction"], original["prediction"]["expected_goals"]


def _result_order(result):
    return (_timestamp(result["observed_at"], "observed_at"),
            _timestamp(result["captured_at"], "captured_at"), result["object_id"])


def _report_binding(current, native, competition, cutoff):
    """A league checkpoint and completed all-league report are distinct originals."""
    for field in ("run_id", "source_commit", "run_started_at", "mode", "time_semantics"):
        if current.get(field) != native.get(field):
            raise ValueError("total_shrink_report_run_binding_mismatch:" + field)
    if current.get("mode") != "train" or current.get("time_semantics") != sm.TIME:
        raise ValueError("total_shrink_report_scope_invalid")
    if native.get("report_scope") not in (None, "completed_competition_checkpoint"):
        raise ValueError("total_shrink_native_report_scope_unknown")
    for report in (current, native):
        if competition not in report.get("requested_competitions", []):
            raise ValueError("total_shrink_report_requested_scope_mismatch")
    current_items = [i for i in current["competitions"] if i["key"] == competition]
    native_items = [i for i in native["competitions"] if i["key"] == competition]
    if len(current_items) != 1 or len(native_items) != 1 or current_items[0] != native_items[0]:
        raise ValueError("total_shrink_report_competition_item_mismatch")
    if not (_timestamp(native["generated_at"], "native_report.generated_at")
            <= _timestamp(current["generated_at"], "current_report.generated_at") <= cutoff):
        raise ValueError("total_shrink_report_checkpoint_final_time_invalid")


def _selection(root, production, protocol_id, competition, cutoff):
    """Freeze complete observed history; never seed it with known historical scores."""
    forecasts = [f for f in _objects(root, "forecasts")
                 if f["protocol_id"] == protocol_id and f["competition"] == competition]
    results = [r for r in current_results(production, as_of=cutoff) if r["competition"] == competition]
    grouped = defaultdict(list)
    for forecast in forecasts:
        _saved_forecast_valid(root, forecast, cutoff, validate_selection=False)
        grouped[forecast["match_id"]].append(forecast)
    eligible, coverage = [], []
    for mid, versions in sorted(grouped.items()):
        diagnostic = {"match_id": mid, "forecast_ids": [v["object_id"] for v in versions]}
        if any(_timestamp(v["captured_at"], "captured_at") >= cutoff for v in versions):
            versions = [v for v in versions if _timestamp(v["captured_at"], "captured_at") < cutoff]
        available = [r for r in results if r["match_id"] == mid and
                     max(_timestamp(r["observed_at"], "observed_at"),
                         _timestamp(r["captured_at"], "captured_at")) <= cutoff]
        if not available:
            coverage.append({**diagnostic, "status": "excluded", "reason": "no_result_recorded_by_cutoff"})
            continue
        latest = max(available, key=_result_order)
        matching = [v for v in versions if _same_identity(v, latest)]
        if not matching:
            coverage.append({**diagnostic, "status": "excluded", "reason": "no_prior_pair_matching_result_identity"})
            continue
        first = min(matching, key=lambda f: (f["captured_at"], f["object_id"]))
        kickoff = _timestamp(first["kickoff"], "kickoff")
        if kickoff < cutoff - timedelta(days=LOOKBACK_DAYS):
            coverage.append({**diagnostic, "status": "excluded", "reason": "outside_180_day_selection_window"})
            continue
        verified = []
        for result in available:
            if not _same_identity(result, first):
                continue
            try:
                source = sm._verified_result(production, result, first, cutoff)
                verified.append((result, source))
            except (ValueError, KeyError, TypeError, OSError) as exc:
                coverage.append({**diagnostic, "status": "result_rejected", "result_id": result["object_id"], "reason": str(exc)})
        if not any(r["object_id"] == latest["object_id"] for r, _ in verified):
            coverage.append({**diagnostic, "status": "excluded", "reason": "latest_result_not_verified_independent"})
            continue
        earliest = min(verified, key=lambda pair: max(_timestamp(pair[0]["observed_at"], "observed_at"),
                                                    _timestamp(pair[0]["captured_at"], "captured_at")))
        first_available = max(_timestamp(earliest[0]["observed_at"], "observed_at"),
                              _timestamp(earliest[0]["captured_at"], "captured_at"))
        mature = first_available + RESULT_EMBARGO
        if mature > cutoff:
            coverage.append({**diagnostic, "status": "excluded", "reason": "true_first_recorded_result_not_48h_mature",
                             "first_result_available_at": _iso(first_available), "mature_at": _iso(mature)})
            continue
        result, source = next(pair for pair in verified if pair[0]["object_id"] == latest["object_id"])
        evidence = {"production_result_id": result["object_id"],
                    "result": {k: v for k, v in result.items() if k != "object_id"}, "source": source,
                    "first_result_id": earliest[0]["object_id"],
                    "first_result": {k: v for k, v in earliest[0].items() if k != "object_id"},
                    "first_source": earliest[1], "first_available_at": _iso(first_available)}
        evidence_id = sm._append(root, "selection_results", evidence)
        eligible.append({"match_id": mid, "forecast_id": first["object_id"],
                         "result_evidence_id": evidence_id, "mature_at": _iso(mature)})
        coverage.append({**diagnostic, "status": "eligible", "selected_forecast_id": first["object_id"],
                         "result_evidence_id": evidence_id})
    frozen = {"protocol_id": protocol_id, "competition": competition, "input_cutoff": _iso(cutoff),
              "eligible": eligible, "history_coverage": coverage}
    frozen_id = sm._append(root, "selection_inputs", frozen)
    values = _selection_values(root, frozen, cutoff)
    return {**values, "selection_input_id": frozen_id}


def _load_object(root, collection, object_id):
    value, _ = _read(root, collection, _name(object_id) + ".json")
    if _digest(value) != object_id:
        raise ValueError("total_shrink_parent_object_hash_mismatch")
    return value


def _selection_values(root, frozen, cutoff):
    """Recompute only the frozen pool, including the original result revisions."""
    if _timestamp(frozen["input_cutoff"], "input_cutoff") != cutoff:
        raise ValueError("selection_cutoff_mismatch")
    losses = {_alpha_name(a): [] for a in ALPHAS}
    seen = set()
    for member in frozen["eligible"]:
        mid = member["match_id"]
        if mid in seen:
            raise ValueError("duplicate_match_in_selection_pool")
        seen.add(mid)
        forecast = _load_object(root, "forecasts", member["forecast_id"])
        _saved_forecast_valid(root, forecast, cutoff, validate_selection=False)
        evidence = _load_object(root, "selection_results", member["result_evidence_id"])
        if (forecast["protocol_id"] != frozen["protocol_id"] or forecast["competition"] != frozen["competition"]
                or forecast["match_id"] != mid or _timestamp(forecast["captured_at"], "captured_at") >= cutoff
                or _timestamp(forecast["kickoff"], "kickoff") < cutoff - timedelta(days=LOOKBACK_DAYS)):
            raise ValueError("frozen_selection_member_identity_or_window_invalid")
        result, first_result = evidence["result"], evidence["first_result"]
        for value, sid, source in ((result, evidence["production_result_id"], evidence["source"]),
                                    (first_result, evidence["first_result_id"], evidence["first_source"])):
            if _digest(value) != sid or not _same_identity(value, forecast):
                raise ValueError("frozen_selection_result_hash_or_identity_mismatch")
            # Use a minimal immutable source reader rather than mutable current results.
            _verified_frozen_result(value, source, forecast, cutoff)
        first_available = max(_timestamp(first_result["observed_at"], "observed_at"),
                              _timestamp(first_result["captured_at"], "captured_at"))
        if (evidence["first_available_at"] != _iso(first_available)
                or member["mature_at"] != _iso(first_available + RESULT_EMBARGO)
                or first_available + RESULT_EMBARGO > cutoff):
            raise ValueError("frozen_selection_true_48h_embargo_invalid")
        goals = result["home_goals"] + result["away_goals"]
        for alpha in ALPHAS:
            rate = math.fsum(forecast["predictions"][_alpha_name(alpha)]["expected_goals"].values())
            losses[_alpha_name(alpha)].append(_total_loss(rate, goals))
    means = {k: math.fsum(v) / len(v) if v else None for k, v in losses.items()}
    if len(seen) >= MINIMUM_MATCHES:
        best = min(means.values())
        chosen = min(a for a in ALPHAS if means[_alpha_name(a)] <= best + 1e-12)
        reason = "past_minimum_total_log_loss"
    else:
        chosen, reason = 0., "fewer_than_40_true_recorded_mature_pairs_use_alpha_0"
    return {"alpha": chosen, "n": len(seen), "mean_total_log_loss_by_alpha": means, "reason": reason,
            "eligible_match_ids": sorted(seen)}


def _verified_frozen_result(result, source, forecast, now):
    """The native independent-result contract, using the frozen source object."""
    if not _same_identity(result, forecast) or result["competition"] != forecast["competition"]:
        raise ValueError("frozen_result_identity_conflict")
    if result.get("collection_method") != "independent_fixture_sync":
        raise ValueError("frozen_independent_result_response_required")
    observed = _timestamp(result["observed_at"], "result.observed_at")
    if not observed <= _timestamp(result["captured_at"], "result.captured_at") <= now:
        raise ValueError("frozen_result_timestamp_invalid")
    if _digest(source) != result["source_id"] or source.get("kind") != "independent_fixture_result_response":
        raise ValueError("frozen_independent_result_source_hash_mismatch")
    envelope = source["content"]
    mid = result["match_id"]
    if source.get("client_response_sha256") != _digest(envelope) or source.get("request") != {"fixture_id": mid}:
        raise ValueError("frozen_result_response_hash_or_request_mismatch")
    if envelope.get("errors") or envelope.get("endpoint", "/fixtures") != "/fixtures" or envelope.get("params", {"id": mid}) != {"id": mid}:
        raise ValueError("frozen_result_response_request_or_errors")
    paging = envelope.get("paging", {})
    items = envelope.get("response")
    if (not isinstance(paging, dict) or any(type(paging.get(k, 1)) is not int or paging.get(k, 1) != 1 for k in ("current", "total"))
            or not isinstance(items, list) or len(items) != 1 or envelope.get("results", 1) != 1):
        raise ValueError("frozen_result_response_partial")
    if _timestamp(envelope["fetched_at"], "fetched_at") != observed:
        raise ValueError("frozen_result_response_time_mismatch")
    if items[0].get("league", {}).get("id") != forecast["parents"]["provider_league_id"]:
        raise ValueError("frozen_result_competition_mismatch")
    rows = normalize_fixtures(items, observed)
    if (len(rows) != 1 or not _same_identity(rows[0], forecast)
            or any(rows[0][k] != result[k] for k in ("home_goals", "away_goals"))):
        raise ValueError("frozen_result_score_or_identity_mismatch")
    return source


def _saved_forecast_valid(root, forecast, now, *, validate_selection=True):
    clean = {k: v for k, v in forecast.items() if k not in ("object_id", "_result_sync_ledger")}
    if forecast.get("object_id") is not None and _digest(clean) != forecast["object_id"]:
        raise ValueError("total_shrink_forecast_hash_mismatch")
    if (forecast.get("version") != VERSION or forecast.get("live_before_kickoff") is not True
            or forecast.get("production_ledger_eligible") is not False
            or forecast.get("confirmation_eligible") is not False or forecast.get("research_replay")):
        raise ValueError("not_live_total_shrink_engineering_pilot")
    cutoff = _timestamp(forecast["input_cutoff"], "input_cutoff")
    captured = _timestamp(forecast["captured_at"], "captured_at")
    generated = _timestamp(forecast["forecast_generated_at"], "forecast_generated_at")
    storage = _timestamp(forecast["storage_started_at"], "storage_started_at")
    if not cutoff <= generated <= storage <= captured <= now or captured >= _timestamp(forecast["kickoff"], "kickoff"):
        raise ValueError("total_shrink_actual_time_order_invalid")
    if forecast["storage_completed_at"] != forecast["captured_at"]:
        raise ValueError("total_shrink_storage_completion_mismatch")
    payload = _load_object(root, "forecast_payloads", forecast["payload_id"])
    if payload != {k: v for k, v in clean.items()
                   if k not in ("payload_id", "storage_completed_at", "captured_at", "live_before_kickoff")}:
        raise ValueError("total_shrink_durable_payload_binding_mismatch")
    protocol = _load_object(root, "protocols", forecast["protocol_id"])
    config_source = _load_object(root, "sources", forecast["config_source"])
    if (config_source["kind"] != "config" or protocol["spec"] != _spec(config_source["content"])
            or _timestamp(protocol["registered_at"], "registered_at") > cutoff):
        raise ValueError("total_shrink_protocol_or_config_binding_invalid")
    inputs = _load_object(root, "inputs", forecast["input_id"])
    for field in ("competition", "input_cutoff", "protocol_id", "parents", "config_source", "index_source",
                  "source_commit", "state_commit", "implementation_sha256", "selection"):
        if inputs[field] != forecast[field]:
            raise ValueError("total_shrink_common_input_binding_mismatch:" + field)
    if _identity(forecast) not in inputs["requested_identities"]:
        raise ValueError("total_shrink_not_in_request_universe")
    parent = _load_object(root, "production_parents", forecast["production_parent_id"])
    _registered_model(root, parent, protocol["spec"])
    baseline, mean = _validate_parent(root, parent, cutoff)
    if (parent["production_forecast_id"] != forecast["production_forecast_id"]
            or not _same_identity(parent["forecast"], forecast)
            or forecast["competition"] != parent["forecast"]["competition"]
            or forecast["league_mean_rates"] != mean):
        raise ValueError("total_shrink_production_parent_binding_mismatch")
    if forecast["predictions"][_alpha_name(0)] != baseline:
        raise ValueError("total_shrink_alpha_zero_must_reuse_native_original")
    if set(forecast["predictions"]) != {_alpha_name(a) for a in ALPHAS}:
        raise ValueError("total_shrink_grid_incomplete")
    for alpha in ALPHAS:
        derived = shrink_prediction(baseline, mean, alpha)
        if not sm._numeric_replay_equivalent(derived, forecast["predictions"][_alpha_name(alpha)]):
            raise ValueError("total_shrink_derived_prediction_does_not_recompute")
    if forecast["prediction"] != forecast["predictions"][_alpha_name(forecast["selection"]["alpha"])]:
        raise ValueError("total_shrink_selected_prediction_binding_mismatch")
    # Provider identity and all current source wrappers are checked against the
    # frozen production run; scores cannot silently switch source competitions.
    report_source = _load_object(root, "sources", forecast["parents"]["report_source"])
    if report_source["kind"] != "production_report":
        raise ValueError("total_shrink_report_parent_mismatch")
    _report_binding(report_source["content"], _parent_source(root, parent, "report_source")["value"]["content"],
                    forecast["competition"], cutoff)
    items = [i for i in report_source["content"]["competitions"] if i["key"] == forecast["competition"]]
    if len(items) != 1 or items[0]["provider_league_id"] != forecast["parents"]["provider_league_id"]:
        raise ValueError("total_shrink_provider_identity_mismatch")
    for field, kind in (("data_source", "normalized_regulation_data"), ("schedule_source", "production_schedule")):
        source = _load_object(root, "sources", forecast["parents"][field])
        native = _parent_source(root, parent, "data_source" if field == "data_source" else "source_id")["value"]
        if (source["kind"] != kind or source["content"] != native["content"]
                or source["original_bytes_sha256"] != native["original_bytes_sha256"]):
            raise ValueError("total_shrink_source_parent_cross_binding_mismatch:" + field)
        if field == "data_source" and source["original_bytes_sha256"] != forecast["parents"]["original_data_sha256"]:
            raise ValueError("total_shrink_original_data_digest_mismatch")
    index_source = _load_object(root, "sources", forecast["index_source"])
    if index_source["kind"] != "integration_index" or index_source["content"].get("status") != "ready":
        raise ValueError("total_shrink_current_index_invalid")
    indexed = [e for e in index_source["content"]["competitions"] if e["key"] == forecast["competition"]]
    if (len(indexed) != 1 or indexed[0]["status"] not in ("available", "unsupported_only", "no_upcoming")
            or indexed[0]["latest_successful_forecast"]["run_id"] != parent["forecast"]["run_id"]):
        raise ValueError("total_shrink_current_index_parent_run_mismatch")
    competition = next(c for c in config_source["content"]["competitions"] if c["key"] == forecast["competition"])
    if competition["mode"] == "national_baseline":
        from .nations import validate_nations_forecast, venue_temporal_audit
        registry = _load_object(root, "sources", forecast["venue"]["venue"]["registry_source"])
        if registry["kind"] != "national_registry":
            raise ValueError("total_shrink_national_registry_kind_mismatch")
        native_row = parent["forecast"]["forecast"]
        neutral = validate_nations_forecast(native_row, registry["content"])
        if neutral != baseline["neutral"]:
            raise ValueError("total_shrink_native_neutral_registry_mismatch")
        temporal = venue_temporal_audit(native_row, registry["content"], cutoff=_iso(cutoff), strict=False)
        for field in ("known_at", "evidence_available_at"):
            if temporal.get(field) and _timestamp(temporal[field], field) > cutoff:
                raise ValueError("total_shrink_venue_evidence_after_input_cutoff")
    schedule = forecast["venue"]["schedule"]
    if schedule.get("source_id"):
        fixture = _load_object(root, "sources", schedule["source_id"])
        if fixture["kind"] != "quote_fixture_response":
            raise ValueError("total_shrink_quote_fixture_source_kind_mismatch")
        envelope = fixture["content"]
        if _timestamp(envelope["fetched_at"], "fixture.fetched_at") > cutoff:
            raise ValueError("total_shrink_quote_fixture_after_cutoff")
        rows = envelope["response"]
        if envelope.get("errors") or len(rows) != 1 or envelope["params"] != {"id": forecast["match_id"]}:
            raise ValueError("total_shrink_quote_fixture_invalid")
        fixture_row = rows[0]
        identity = {"match_id": fixture_row["fixture"]["id"], "kickoff": fixture_row["fixture"]["date"],
                    "home_id": fixture_row["teams"]["home"]["id"], "away_id": fixture_row["teams"]["away"]["id"]}
        if (not _same_identity(identity, forecast) or fixture_row["fixture"]["status"]["short"] != "NS"
                or fixture_row["league"]["id"] != forecast["parents"]["provider_league_id"]):
            raise ValueError("total_shrink_quote_fixture_identity_or_state_invalid")
    if validate_selection:
        frozen = _load_object(root, "selection_inputs", forecast["selection"]["selection_input_id"])
        if frozen["protocol_id"] != forecast["protocol_id"] or frozen["competition"] != forecast["competition"]:
            raise ValueError("total_shrink_frozen_selection_scope_mismatch")
        selected = _selection_values(root, frozen, cutoff)
        if forecast["selection"] != {**selected, "selection_input_id": forecast["selection"]["selection_input_id"]}:
            raise ValueError("total_shrink_selection_does_not_recompute")


def _capture(runtime, config, *, source_commit, state_commit=None, league="all", clock=None):
    clock = clock or (lambda: datetime.now(timezone.utc))
    if not re.fullmatch(r"[0-9a-f]{40}", source_commit or ""):
        raise ValueError("full_source_commit_required")
    if state_commit is not None and not re.fullmatch(r"[0-9a-f]{40}", state_commit):
        raise ValueError("invalid_state_commit")
    runtime = Path(runtime)
    # Share the mature F4 OS writer lock and conservative persisted/raw budget.
    with sm._writer(runtime):
        root = _root(runtime)
        started = _now(clock)
        protocol_id = _protocol(root, config, clock)
        config_id = sm._snapshot(root, "config", config, _digest(config))
        index, index_sha = _read(runtime, "integration", "index.json")
        if index.get("status") != "ready":
            raise ValueError("integration_index_not_ready")
        index_id = sm._snapshot(root, "integration_index", index, index_sha)
        implementations = {name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
                           for name in ("total_shrink_shadow.py", "shadow_models.py", "prospective.py", "model.py")}
        report = {"version": VERSION, "started_at": _iso(started), "protocol_id": protocol_id,
            "source_commit": source_commit, "state_commit": state_commit, "index_source": index_id,
            "numerical_environment": sm._numerical_environment(), "new_fits": 0, "new_api_calls": 0,
            "production_models_changed": False, "confirmation_eligible": False,
            "accuracy_improvement_verified": False, "competitions": [], "errors": []}
        competitions = [c for c in config["competitions"] if c["mode"] != "audit_only"
                        and (league == "all" or c["key"] == league)]
        if not competitions:
            raise ValueError("unknown_or_audit_only_competition")
        production = _path(runtime, "prospective")
        recorded = _objects(production, "forecasts")
        existing = _objects(root, "forecasts")
        for competition in competitions:
            key = _name(competition["key"])
            entry = {"competition": key, "coverage": []}
            report["competitions"].append(entry)
            try:
                read_started = sm._tick(clock, started)
                rows, data, parents, _ = sm._source_files(runtime, root, config, key, index, read_started)
                cutoff = sm._tick(clock, read_started)
                selection = _selection(root, production, protocol_id, key, cutoff)
                base = {"version": VERSION, "competition": key, "input_cutoff": _iso(cutoff),
                    "protocol_id": protocol_id, "parents": parents, "config_source": config_id,
                    "index_source": index_id,
                    "source_commit": source_commit, "state_commit": state_commit,
                    "implementation_sha256": implementations, "selection": selection}
                input_id = sm._append(root, "inputs", {**base, "requested_identities": [_identity(r) for r in rows],
                                                       "frozen_at": _iso(cutoff)})
                entry.update(input_id=input_id, requested_matches=len(rows), selection=selection)
                for row in rows:
                    coverage = {"competition": key, **_identity(row), "input_id": input_id,
                                "protocol_id": protocol_id, "source_status": row.get("status")}
                    try:
                        kickoff = _timestamp(row["kickoff"], "kickoff")
                        if kickoff <= cutoff:
                            raise ValueError("not_future_at_real_capture")
                        if kickoff > cutoff + timedelta(days=config["forecast_days"]):
                            raise ValueError("outside_registered_forecast_window")
                        if row.get("status") != "exploratory_baseline":
                            raise ValueError("production_source_unsupported:" + str(row.get("reason")))
                        matching = [f for f in recorded if f["competition"] == key
                                    and f["run_id"] == parents["source_run_id"] and f["forecast"] == row]
                        if not matching:
                            raise ValueError("no_native_recorded_production_forecast")
                        parent_forecast = min(matching, key=lambda f: (f["captured_at"], f["object_id"]))
                        schedule = sm._check_schedule(runtime, root, key, row, parents, cutoff)
                        neutral, venue = sm._neutral(runtime, root, competition, row, cutoff)
                        if neutral != row["prediction"]["neutral"]:
                            raise ValueError("native_production_neutral_conflict")
                        duplicate = [f for f in existing if f["protocol_id"] == protocol_id
                                     and f["production_forecast_id"] == parent_forecast["object_id"]]
                        if duplicate:
                            for old in duplicate:
                                _saved_forecast_valid(root, old, cutoff)
                            coverage.update(status="already_captured", forecast_id=duplicate[0]["object_id"])
                        else:
                            parent_id = _parent(runtime, root, parent_forecast, row, data,
                                                parents["original_data_sha256"], cutoff)
                            parent = _load_object(root, "production_parents", parent_id)
                            _registered_model(root, parent, _spec(config))
                            _report_binding(_load_object(root, "sources", parents["report_source"])["content"],
                                _parent_source(root, parent, "report_source")["value"]["content"], key, cutoff)
                            baseline, mean = _validate_parent(root, parent, cutoff)
                            if baseline["model_age_days"] > config["max_model_age_days"]:
                                raise ValueError("production_model_expires_before_kickoff")
                            predictions = {_alpha_name(a): shrink_prediction(baseline, mean, a) for a in ALPHAS}
                            generated = sm._tick(clock, cutoff)
                            before = sm._tick(clock, generated)
                            if before >= kickoff:
                                raise ValueError("completed_at_or_after_kickoff")
                            payload = {**base, **_identity(row), "input_id": input_id,
                                "production_parent_id": parent_id, "production_forecast_id": parent_forecast["object_id"],
                                "league_mean_rates": mean, "predictions": predictions,
                                "prediction": predictions[_alpha_name(selection["alpha"])],
                                "forecast_generated_at": _iso(generated), "storage_started_at": _iso(before),
                                "venue": {"venue": venue, "schedule": schedule}, "research_only": True,
                                "research_replay": False, "production_ledger_eligible": False,
                                "confirmation_eligible": False,
                                "remote_persistence_status": "pending_state_commit_verification"}
                            payload_id = sm._append(root, "forecast_payloads", payload)
                            captured = sm._tick(clock, before)
                            coverage["payload_id"] = payload_id
                            if captured >= kickoff:
                                raise ValueError("storage_completed_at_or_after_kickoff")
                            receipt = {**payload, "payload_id": payload_id, "captured_at": _iso(captured),
                                       "storage_completed_at": _iso(captured), "live_before_kickoff": True}
                            fid = sm._append(root, "forecasts", receipt)
                            existing.append({**receipt, "object_id": fid})
                            coverage.update(status="captured", forecast_id=fid)
                    except (ValueError, KeyError, TypeError, OSError) as exc:
                        coverage.update(status="rejected", reason=str(exc))
                    entry["coverage"].append(coverage)
                    sm._append(root, "coverage", coverage)
                entry["status"] = "completed"
            except (ValueError, KeyError, TypeError, OSError) as exc:
                entry.update(status="source_rejected", reason=str(exc))
                report["errors"].append(key + ":" + str(exc))
                sm._append(root, "coverage", {"competition": key, "protocol_id": protocol_id,
                    "status": "source_rejected", "reason": str(exc), "recorded_at": _iso(sm._tick(clock, cutoff if 'cutoff' in locals() else started))})
        report["completed_at"] = _iso(sm._tick(clock, started))
        coverage = [c for entry in report["competitions"] for c in entry["coverage"]]
        report["captured_forecasts"] = sum(c["status"] == "captured" for c in coverage)
        report["captured_matches"] = len({(c["competition"], c["match_id"]) for c in coverage if c["status"] == "captured"})
        report["coverage_status_counts"] = dict(Counter(c["status"] for c in coverage))
        report["coverage_rejection_reasons"] = dict(Counter(c["reason"].split(":", 1)[0] for c in coverage if c["status"] == "rejected"))
        report["storage_budget"] = sm._budget_public(sm._BUDGET.get())
        report["report_id"] = sm._append(root, "runs", report)
        sm._latest(root, "latest.json", {k: v for k, v in report.items() if k != "competitions"})
        return int(bool(report["errors"])), report


def _metrics(prediction, result):
    home, away = result["home_goals"], result["away_goals"]
    matrix = matrix_array(prediction["score_matrix"])
    h, a = np.indices(matrix.shape)
    over = float(matrix[h + a >= 3].sum())
    outcome = 0 if home > away else 1 if home == away else 2
    ps = [prediction["probabilities"][k] for k in ("home_win", "draw", "away_win")]
    rates = prediction["expected_goals"]
    return {"total_goals_log_loss": _total_loss(math.fsum(rates.values()), home + away),
            "over_2_5_brier": (over - int(home + away >= 3)) ** 2,
            "log_loss_1x2": -math.log(max(1e-15, ps[outcome])),
            "brier_1x2": math.fsum((p - int(i == outcome)) ** 2 for i, p in enumerate(ps)),
            "score_log_loss": _total_loss(rates["home"], home) + _total_loss(rates["away"], away),
            "argmax_1x2_hit": int(int(np.argmax(ps)) == outcome)}


def _score(runtime, *, source_commit=None, clock=None):
    clock = clock or (lambda: datetime.now(timezone.utc))
    runtime = Path(runtime)
    if source_commit is not None and not re.fullmatch(r"[0-9a-f]{40}", source_commit):
        raise ValueError("invalid_source_commit")
    with sm._writer(runtime):
        root, now = _root(runtime), _now(clock)
        forecasts = _objects(root, "forecasts")
        production = _path(runtime, "prospective")
        results = current_results(production, as_of=now)
        report = {"version": VERSION, "scored_at": _iso(now), "forecast_versions": len(forecasts),
            "source_commit": source_commit, "run_started_at": _iso(now),
            "confirmation_eligible": False, "accuracy_improvement_verified": False, "profit_evaluation": False,
            "production_models_changed": False, "new_fits": 0, "new_api_calls": 0,
            "numeric_replay_matrix_abs_tol": sm.NUMERIC_REPLAY_MATRIX_ABS_TOL,
            "numeric_replay_probability_abs_tol": sm.NUMERIC_REPLAY_PROBABILITY_ABS_TOL,
            "numerical_environment": sm._numerical_environment(), "matches": [], "errors": []}
        groups = defaultdict(list)
        for forecast in forecasts:
            try:
                _saved_forecast_valid(root, forecast, now)
                groups[(forecast["protocol_id"], forecast["competition"], forecast["match_id"])].append(forecast)
            except (ValueError, TypeError, KeyError, OSError) as exc:
                report["errors"].append(forecast["object_id"] + ":" + str(exc))
        for key, versions in sorted(groups.items()):
            record = {"protocol_id": key[0], "competition": key[1], "match_id": key[2]}
            report["matches"].append(record)
            available = [r for r in results if r["competition"] == key[1] and r["match_id"] == key[2]]
            result = max(available, key=_result_order) if available else None
            matching = [v for v in versions if result is None or _same_identity(v, result)]
            record["identity_excluded_forecast_ids"] = [v["object_id"] for v in versions if v not in matching]
            if not matching:
                record.update(status="rejected", reason="no_pair_matching_latest_result_identity")
                continue
            first = min(matching, key=lambda f: (f["captured_at"], f["object_id"]))
            record.update(forecast_id=first["object_id"], selected_alpha=first["selection"]["alpha"], input_id=first["input_id"])
            if result is None:
                record.update(status="pending", reason="no_independent_result_available")
                continue
            try:
                source = sm._verified_result(production, result, first, now)
                rid = sm._append(root, "results", {"production_result_id": result["object_id"],
                    "content": {k: v for k, v in result.items() if k != "object_id"}, "source": source})
                arms = {name: _metrics(prediction, result) for name, prediction in first["predictions"].items()}
                base = arms[_alpha_name(0)]
                differences = {name: {metric: value - base[metric] for metric, value in metrics.items()}
                               for name, metrics in arms.items()}
                record.update(status="scored", result_id=rid, arms=arms,
                    candidate_minus_baseline=differences[_alpha_name(first["selection"]["alpha"])],
                    fixed_alpha_exploratory_minus_baseline=differences)
                record["score_id"] = sm._append(root, "scores", record)
            except (ValueError, KeyError, TypeError, OSError) as exc:
                record.update(status="rejected", reason=str(exc))
                report["errors"].append(key[1] + ":" + str(key[2]) + ":" + str(exc))
        report["selected_matches"] = len(report["matches"])
        report["status_counts"] = dict(Counter(r["status"] for r in report["matches"]))
        scored = [r for r in report["matches"] if r["status"] == "scored"]
        report["n_scored_matches"] = len(scored)
        report["mean_candidate_minus_baseline"] = {metric: math.fsum(r["candidate_minus_baseline"][metric] for r in scored) / len(scored)
            if scored else None for metric in ("total_goals_log_loss", "over_2_5_brier", "log_loss_1x2", "brier_1x2", "score_log_loss", "argmax_1x2_hit")}
        report["inferential_status"] = "engineering_pilot_descriptive_only_not_confirmation_or_promotion"
        report["generated_at"] = _iso(sm._tick(clock, now))
        report["report_id"] = sm._append(root, "score_runs", report)
        sm._latest(root, "score-latest.json", {k: v for k, v in report.items() if k != "matches"})
        return int(bool(report["errors"])), report


def _capacity_locked(runtime, operation, error, clock):
    """Bound diagnostics by hard transport limits, outside the write budget."""
    from .cli import atomic_json
    from .state_storage import storage_cost, storage_write_delta
    runtime = Path(runtime)
    context = getattr(error, "run_context", None)
    started, source_commit = context if context is not None else (_now(clock), None)
    completed = sm._tick(clock, started)
    value = {"version": VERSION, "status": "capacity_deferred", "operation": operation,
             "run_started_at": _iso(started), "started_at": _iso(started), "completed_at": _iso(completed),
             "generated_at": _iso(completed), "source_commit": source_commit,
             "reason": str(error), **error.budget, "partial_objects_preserved": True,
             "confirmation_eligible": False, "production_models_changed": False,
             "accuracy_improvement_verified": False}
    if operation == "score":
        value["scored_at"] = _iso(completed)
    if len(_json_bytes(value)) > sm.CAPACITY_REPORT_RESERVE_BYTES:
        raise ValueError("capacity_diagnostic_too_large")
    root = _root(runtime)
    latest = _path(root, "reports", "latest.json" if operation == "capture" else "score-latest.json")
    previous = latest.read_bytes() if latest.exists() else None
    relative = latest.relative_to(runtime).as_posix()
    value.update(immutable_event_saved=True, diagnostic_evidence="immutable_capacity_event_and_current_latest")
    raw = _json_bytes(value)
    event = _path(root, "capacity_events", _digest(value) + ".json")
    event_cost = 0 if event.exists() else storage_cost(event.relative_to(runtime).as_posix(), raw)
    event_bytes = 0 if event.exists() else len(raw)
    candidate = {**value, "capacity_event_id": _digest(value)}
    latest_raw = (json.dumps(candidate, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode()
    persisted, logical = error.budget["estimated_persisted_bytes"], error.budget["logical_bytes"]
    latest_delta = storage_write_delta(relative, latest_raw, previous)
    if (persisted + event_cost + latest_delta < min(sm.MAX_PHYSICAL_BYTES, sm.STORAGE_LIMIT_BYTES + sm.CAPACITY_REPORT_RESERVE_BYTES)
            and logical + event_bytes + len(latest_raw) - (len(previous) if previous else 0) <= sm.MAX_LOGICAL_BYTES):
        value["capacity_event_id"] = _unbudgeted_append(root, "capacity_events", value)
        logical += event_bytes
        persisted += event_cost
    else:
        value.update(capacity_event_id=None, immutable_event_saved=False,
                     diagnostic_evidence="bounded_current_latest_only")
    latest_raw = (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode()
    if (logical + len(latest_raw) - (len(previous) if previous else 0) > sm.MAX_LOGICAL_BYTES
            or persisted + storage_write_delta(relative, latest_raw, previous) >= sm.MAX_PHYSICAL_BYTES):
        raise ValueError("total_shrink_capacity_diagnostic_exceeds_shared_limits")
    atomic_json(latest, value)
    return 1, value


def _capacity(runtime, operation, error, clock):
    # The failing capture released its lock. Reacquire it and remeasure every
    # dimension before using diagnostic headroom; a concurrent writer may have
    # advanced the state in between. Never consume an old exception's budget.
    with sm._writer(Path(runtime)):
        refreshed = sm.ShadowCapacityError({**sm._budget_public(sm._BUDGET.get()),
            "capacity_constraint": error.budget.get("capacity_constraint")})
        if hasattr(error, "run_context"):
            refreshed.run_context = error.run_context
        return _capacity_locked(runtime, operation, refreshed, clock)


def capture(runtime, config, *, source_commit, state_commit=None, league="all", clock=None):
    clock = clock or (lambda: datetime.now(timezone.utc))
    started = _now(clock)
    try:
        return _capture(runtime, config, source_commit=source_commit, state_commit=state_commit, league=league, clock=clock)
    except sm.ShadowCapacityError as exc:
        exc.run_context = (started, source_commit)
        return _capacity(runtime, "capture", exc, clock)


def score(runtime, *, source_commit=None, clock=None):
    clock = clock or (lambda: datetime.now(timezone.utc))
    started = _now(clock)
    try:
        return _score(runtime, source_commit=source_commit, clock=clock)
    except sm.ShadowCapacityError as exc:
        exc.run_context = (started, source_commit)
        return _capacity(runtime, "score", exc, clock)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="operation", required=True)
    for name in ("capture", "score"):
        item = sub.add_parser(name)
        item.add_argument("--runtime", type=Path, default=Path("runtime"))
        if name == "capture":
            item.add_argument("--config", type=Path, default=Path("config.json"))
            item.add_argument("--source-commit", required=True)
            item.add_argument("--state-commit")
            item.add_argument("--league", default="all")
        else:
            item.add_argument("--source-commit")
    args = parser.parse_args(argv)
    if args.operation == "capture":
        config = json.loads(args.config.read_text())
        code, report = capture(args.runtime, config, source_commit=args.source_commit,
                               state_commit=args.state_commit, league=args.league)
    else:
        code, report = score(args.runtime, source_commit=args.source_commit)
    print(json.dumps({k: report.get(k) for k in ("version", "status", "captured_forecasts", "selected_matches", "n_scored_matches", "errors")}, ensure_ascii=False))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
