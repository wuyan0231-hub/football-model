"""Whitelist-only workflow summary; never infer this run from a restored latest file.

The summary is observational. It neither changes forecast eligibility nor changes
the workflow's final failure gate. Missing/corrupt/unassociated inputs are unknown.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re

VERSION = "layered-workflow-summary-v3"
LEAGUES = frozenset("epl laliga bundesliga serie_a ligue_1 bundesliga_2 ligue_2 ucl allsvenskan eredivisie primeira brazil_a j1 k1 mls unl liga_mx segunda argentina_a".split())
OUTCOMES = frozenset(("success", "failure", "cancelled", "skipped", "unknown"))
STAGES = ("restore", "model", "replay", "prospective", "result_sync", "quotes", "fundamentals", "shadow_models", "check", "persist")
COVERAGE_STATES = frozenset(("prospective", "rejected", "unsupported", "capture_error", "no_current_prediction_file", "no_upcoming_matches"))
AVAILABILITY = frozenset(("available", "partial", "unavailable", "no_upcoming", "audit_only", "not_requested", "failed"))
NORMAL_MISSING = frozenset(("coverage_false", "coverage_unknown", "lineup_window_not_open", "not_completed_90_minute_fixture"))
REASONS = NORMAL_MISSING | frozenset((
    "budget_deferred", "provider_error", "invalid_evidence_or_save_failed", "invalid_evidence", "evidence_save_failed",
    "prior_context_unavailable_or_invalid", "saved_history_missing_or_invalid", "fixture_or_coverage_unavailable_or_invalid",
    "feature_build_or_save_failed", "match_started_before_feature_cutoff", "target_not_confirmed_future_NS",
    "evidence_fetch_failed", "diagnostic_save_failed", "collection_dispatch_failed",
    "collection_initialization_or_clock_failed", "prior_not_observed_finished", "audit_only", "no_upcoming",
    "unreliable_index_state", "target_fixture_not_scheduled", "prematch_collection_window_closed",
    "lineup_collection_requires_prematch_or_finished_fixture", "evidence_validation_failed", "snapshot_save_failed",
    "research_replay_is_not_prospective", "capture_at_or_after_kickoff_not_prospective", "missing_observed_wall_clock_semantics",
    "forecast_does_not_belong_to_current_run", "invalid_actual_observation_generation_capture_order",
    "invalid_model_cutoff_fit_generation_order", "missing_prediction", "prediction_identity_mismatch",
    "prediction_training_cutoff_mismatch", "missing_model_version_provenance", "unknown_score_matrix_axes",
    "1X2_probabilities_disagree_with_matrix", "competition_mismatch", "model_snapshot_provenance_mismatch",
    "source_not_supported", "immutable_run_prediction_file_changed", "result_source_missing_or_future_observation",
    "result_identity_or_kickoff_mismatch", "result_observed_before_regulation_maturity",
    "result_source_observation_regressed", "conflicting_result_same_observation_time"))


def _time(value):
    if not isinstance(value, str):
        raise ValueError("invalid_timestamp")
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("invalid_timestamp")
    return result.astimezone(timezone.utc)


def _iso(value):
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _canonical(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False, indent=2) + "\n").encode()


def _digest(value):
    return hashlib.sha256(_canonical(value)).hexdigest()


def _read(path):
    # Summary is read-only; reject links rather than accidentally reading outside runtime.
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError("symlinked_input")
    raw = path.read_bytes()
    value = json.loads(raw, parse_constant=lambda _: (_ for _ in ()).throw(ValueError("invalid_json")))
    if not isinstance(value, dict):
        raise ValueError("invalid_object")
    return value, hashlib.sha256(raw).hexdigest()


def _count(value):
    return value if type(value) is int and value >= 0 else None


def _reason(value):
    if isinstance(value, str) and value in REASONS:
        return value
    # Legacy timestamp exception is mapped exactly; raw exceptions are never emitted.
    if value == "model_fitted_at 必须是包含时区的 ISO 时间字符串。":
        return "model_fitted_at_missing_or_invalid"
    return "unclassified_reason"


def _objects(root, collection):
    directory = root / collection
    if not directory.is_dir() or directory.is_symlink():
        raise ValueError("ledger_collection_missing")
    rows = []
    for path in sorted(directory.glob("*.json")):
        value, _ = _read(path)
        if path.stem != _digest(value):
            raise ValueError("ledger_integrity_failed")
        rows.append(value)
    return rows


def _unknown(reason="missing_or_invalid_current_report"):
    return {"status": "unknown", "reason": reason}


def _associated(path, context, stage, now):
    value, digest = _read(path)
    start = _time(context["stage_started_at"][stage])
    generated = _time(value["generated_at"])
    if not start <= generated <= now:
        raise ValueError("report_outside_current_stage")
    if "run_started_at" in value and not start <= _time(value["run_started_at"]) <= generated:
        raise ValueError("report_outside_current_stage")
    return value, digest


def _model_summary(report):
    requested = report.get("requested_competitions")
    items = report.get("competitions")
    if (not isinstance(requested, list) or not requested or any(k not in LEAGUES for k in requested)
            or len(set(requested)) != len(requested) or not isinstance(items, list)):
        raise ValueError("invalid_competition_scope")
    mapping = {item["key"]: item for item in items if isinstance(item, dict) and item.get("key") in requested}
    if len(mapping) != len(items):
        raise ValueError("duplicate_or_invalid_competitions")
    rows, known = [], []
    unknown = 0
    for key in requested:
        coverage = mapping.get(key, {}).get("forecast_availability", {})
        state = coverage.get("status") if coverage.get("status") in AVAILABILITY else "unknown"
        counts = {field: _count(coverage.get(field)) for field in ("supported_count", "unsupported_count", "upcoming_count")}
        applicable = state not in ("audit_only", "not_requested")
        if applicable:
            if any(v is None for v in counts.values()) or counts["supported_count"] + counts["unsupported_count"] != counts["upcoming_count"]:
                unknown += 1
            else:
                known.append(counts)
        rows.append({"competition": key, "status": state, **counts})
    return {"status": "associated_current_report", "competitions": rows,
            "coverage_status_counts": dict(Counter(r["status"] for r in rows)),
            "unknown_prediction_coverage_competitions": unknown,
            "known_supported_predictions": sum(r["supported_count"] for r in known) if known else None,
            "supported_predictions": sum(r["supported_count"] for r in known) if known and unknown == 0 else None,
            "unsupported_predictions": sum(r["unsupported_count"] for r in known) if known and unknown == 0 else None,
            "prediction_support_is_quality_acceptance": False}


def _current_capture(runtime, report, report_sha, context, now):
    source = _digest({"kind": "run_report", "original_bytes_sha256": report_sha, "content": report})
    sources = {}
    if "prospective" in context["stage_started_at"]:
        sources[source] = (set(report["requested_competitions"]), _time(context["stage_started_at"]["prospective"]), now)
    # Per-league captures precede the final reporting step. Verify each immutable
    # checkpoint against the current completed league, instead of widening the
    # old final-report time window or accepting an arbitrary report_source.
    for capture in report.get("prospective_captures", []):
        checkpoint_id = capture.get("checkpoint_id")
        if checkpoint_id is None and capture.get("status") == "failed":
            continue
        capture_id = capture.get("capture_run_id")
        if any(not isinstance(v, str) or not re.fullmatch(r"[0-9a-f]{64}", v)
               for v in (checkpoint_id, capture_id)):
            raise ValueError("invalid_checkpoint_binding")
        checkpoint, checkpoint_sha = _read(runtime / "prospective" / "capture_checkpoints" / (checkpoint_id + ".json"))
        capture_record, _ = _read(runtime / "prospective" / "capture_runs" / (capture_id + ".json"))
        if (_digest(checkpoint) != checkpoint_id or _digest(capture_record) != capture_id
                or capture_record != {k: v for k, v in capture.items() if k != "capture_run_id"}):
            raise ValueError("checkpoint_integrity_failed")
        keys = checkpoint.get("requested_competitions")
        if (not isinstance(keys, list) or len(keys) != 1 or keys[0] not in report["requested_competitions"]
                or checkpoint.get("report_scope") != "completed_competition_checkpoint"
                or capture_record.get("competitions") != keys
                or capture_record.get("run_id") != report["run_id"]
                or any(checkpoint.get(k) != report.get(k) for k in ("run_id", "source_commit", "run_started_at", "mode"))
                or checkpoint.get("competitions") != [r for r in report["competitions"] if r["key"] in keys]):
            raise ValueError("checkpoint_association_failed")
        start = _time(context["stage_started_at"]["model"])
        if not start <= _time(checkpoint["generated_at"]) <= _time(capture_record["completed_at"]) <= _time(report["generated_at"]) <= now:
            raise ValueError("checkpoint_time_invalid")
        checkpoint_source = _digest({"kind": "run_report", "original_bytes_sha256": checkpoint_sha, "content": checkpoint})
        sources[checkpoint_source] = (set(keys), _time(checkpoint["generated_at"]), _time(capture_record["completed_at"]))
    current, seen = [], set()
    for row in _objects(runtime / "prospective", "coverage"):
        if row.get("run_id") != report["run_id"]:
            continue
        binding = sources.get(row.get("report_source"))
        if (binding is None or row.get("competition") not in binding[0]
                or not binding[1] <= _time(row["captured_at"]) <= binding[2]):
            raise ValueError("coverage_association_failed")
        key = row.get("competition")
        mid = row.get("match_id")
        state = row.get("status")
        if (key not in report["requested_competitions"] or state not in COVERAGE_STATES
                or (mid is not None and (type(mid) is not int or mid < 1)) or (key, mid) in seen):
            raise ValueError("ambiguous_current_coverage")
        if state in ("prospective", "rejected", "unsupported") and mid is None:
            raise ValueError("current_prediction_identity_missing")
        if state == "prospective":
            fid = row.get("forecast_id")
            if not isinstance(fid, str) or not re.fullmatch(r"[0-9a-f]{64}", fid):
                raise ValueError("accepted_forecast_missing")
            forecast, _ = _read(runtime / "prospective" / "forecasts" / (fid + ".json"))
            if (_digest(forecast) != fid or forecast.get("run_id") != report["run_id"]
                    or forecast.get("competition") != key or forecast.get("match_id") != mid
                    or forecast.get("report_source") != row.get("report_source")
                    or forecast.get("captured_at") != row.get("captured_at")):
                raise ValueError("accepted_forecast_unassociated")
        seen.add((key, mid))
        current.append(row)
    if not current:
        raise ValueError("no_current_coverage_records")
    states = Counter(r["status"] for r in current)
    reasons = Counter(_reason(r.get("reason")) for r in current if r["status"] in ("rejected", "unsupported", "capture_error"))
    by_competition = []
    for key in report["requested_competitions"]:
        rows = [r for r in current if r["competition"] == key]
        by_competition.append({"competition": key, "observed_records": len(rows) if rows else None,
            "status_counts": dict(Counter(r["status"] for r in rows)) if rows else None,
            "reason_counts": dict(Counter(_reason(r.get("reason")) for r in rows
                if r["status"] in ("rejected", "unsupported", "capture_error"))) if rows else None})
    return {"status": "associated_current_ledger_records", "count_basis": "current_run_coverage_objects_no_cumulative_subtraction",
            "observed_records": len(current), "observed_accepted_predictions": states["prospective"],
            "observed_rejected_predictions": states["rejected"], "observed_unsupported_predictions": states["unsupported"],
            "status_counts": dict(states), "reason_counts": dict(reasons), "competitions": by_competition}


def _cumulative(value):
    fields = ("forecast_versions", "unique_primary_matches_by_protocol", "result_versions")
    result = {key: _count(value.get(key)) for key in fields}
    statuses = value.get("match_statuses")
    if not isinstance(statuses, list):
        result.update(mature_scored_matches_by_protocol=None, pending_matches_by_protocol=None,
                      unique_matches_across_protocols=None)
    else:
        identities, protocol_identities, mature_identities = set(), set(), set()
        scored = pending = 0
        for row in statuses:
            key, mid = row.get("competition"), row.get("match_id")
            pid = row.get("protocol_id")
            if key not in LEAGUES or type(mid) is not int or mid < 1 or not isinstance(pid, str):
                raise ValueError("invalid_cumulative_identity")
            if (pid, key, mid) in protocol_identities:
                raise ValueError("duplicate_cumulative_identity")
            protocol_identities.add((pid, key, mid))
            identities.add((key, mid))
            if row.get("status") not in ("scored", "pending", "result_identity_conflict"):
                raise ValueError("unknown_cumulative_status")
            if row.get("status") == "scored":
                mature_identities.add((key, mid))
            scored += row.get("status") == "scored"
            pending += row.get("status") == "pending"
        if result["unique_primary_matches_by_protocol"] is not None and len(protocol_identities) != result["unique_primary_matches_by_protocol"]:
            raise ValueError("cumulative_count_mismatch")
        result.update(mature_scored_matches_by_protocol=scored, pending_matches_by_protocol=pending,
                      unique_matches_across_protocols=len(identities), mature_unique_matches_across_protocols=len(mature_identities))
    return {"status": "associated_current_cumulative_view", **result,
            "counts_are_accuracy_or_profit_acceptance": False}


def _fundamentals_summary(value, model_run_id):
    rows = value.get("matches")
    if not isinstance(rows, list):
        raise ValueError("invalid_collection_rows")
    counts = Counter()
    reasons = Counter()
    issues = value.get("issues")
    issue_counts = (dict(Counter(_reason(row.get("reason")) for row in issues if isinstance(row, dict)))
                    if isinstance(issues, list) else None)
    for row in rows:
        if row.get("forecast_run_id") != model_run_id:
            raise ValueError("collection_forecast_run_mismatch")
        missing = row.get("missing")
        if not isinstance(missing, list):
            counts["unknown"] += 1
            continue
        names = [_reason(m.get("reason")) for m in missing if isinstance(m, dict)]
        if len(names) != len(missing):
            counts["unknown"] += 1
            continue
        if row.get("reason") is not None:
            names.append(_reason(row["reason"]))
        reasons.update(names)
        if row.get("status") != "features_saved":
            counts["partial"] += 1
        else:
            counts["features_saved"] += 1
            counts["complete" if not names else "normal_missing" if set(names) <= NORMAL_MISSING else "partial"] += 1
    return {"status": value.get("status") if value.get("status") in ("complete", "partial", "no_upcoming") else "unknown",
            "candidate_matches": _count(value.get("candidate_count")), "reported_matches": len(rows),
            "complete_collection_matches": counts["complete"], "partial_collection_matches": counts["partial"],
            "normal_missing_matches": counts["normal_missing"], "unknown_collection_matches": counts["unknown"],
            "features_saved_matches": counts["features_saved"], "reason_counts": dict(reasons),
            "global_issue_counts": issue_counts,
            "completeness_scope": "requested_collection_operations_not_all_feature_fields_or_all_competitions",
            "fundamentals_used_in_probabilities": False, "is_model_probability_gate": False}


def _independent_results(runtime, context, now):
    value, _ = _associated(runtime / "result_sync" / "reports" / "latest.json", context, "result_sync", now)
    if (not re.fullmatch(r"[0-9a-f]{40}", context.get("source_commit", ""))
            or value.get("source_commit") != context["source_commit"]):
        raise ValueError("independent_sync_source_mismatch")
    allowed = ("result_appended", "result_unchanged", "pending_or_not_mature_at_observation", "not_due",
               "outside_revision_recheck_window", "budget_deferred", "failed", "client_unavailable")
    counts = value.get("counts", value.get("status_counts"))
    if not isinstance(counts, dict):
        raise ValueError("invalid_sync_counts")
    unclassified = [_count(v) for k, v in counts.items() if k not in allowed]
    summary = {"status": "associated_current_report", "target_matches": _count(value.get("target_matches")),
               "api_requests": _count(value.get("api_requests")),
               "status_counts": {k: _count(counts.get(k, 0)) for k in allowed},
               "unknown_status_records": sum(unclassified) if all(v is not None for v in unclassified) else None,
               "independent_of_model_training": True}
    score_id = value.get("score_summary_id")
    if isinstance(score_id, str) and re.fullmatch(r"[0-9a-f]{64}", score_id):
        score, _ = _read(runtime / "prospective" / "result_sync_scores" / (score_id + ".json"))
        if (_digest(score) != score_id or not _time(value["run_started_at"]) <= _time(score["generated_at"]) <= _time(value["generated_at"])):
            raise ValueError("independent_score_summary_mismatch")
        summary["cumulative"] = _cumulative(score)
    return summary


def _quote_collection(runtime, context, now):
    value, _ = _associated(runtime / "quotes" / "reports" / "latest.json", context, "quotes", now)
    if (not re.fullmatch(r"[0-9a-f]{40}", context.get("source_commit", ""))
            or value.get("source_commit") != context["source_commit"]):
        raise ValueError("quote_collection_source_mismatch")
    counts, rows = value.get("counts"), value.get("matches")
    if not isinstance(counts, dict) or not isinstance(rows, list):
        raise ValueError("invalid_quote_collection_report")
    allowed = ("collected", "empty", "deferred", "failed", "skipped", "evidence_only")
    fields = ("rows", "unknown_rows", "closed_rows", "complete_reference_rows")
    # Some skipped/deferred matches have no parsed snapshot. Do not silently
    # count those as zero prices; totals describe only known parsed snapshots.
    parsed = [r for r in rows if isinstance(r, dict) and r.get("snapshot_path")]
    known = {k: [_count(r.get(k)) for r in parsed] for k in fields}
    return {"status": value.get("status") if value.get("status") in ("complete", "partial", "no_upcoming") else "unknown",
            "candidate_matches": _count(value.get("candidate_count")), "reported_matches": len(rows),
            "api_requests": _count(value.get("api_requests")),
            "status_counts": {k: _count(counts.get(k, 0)) for k in allowed},
            "parsed_snapshots": len(parsed),
            **{k: sum(v) if v and all(x is not None for x in v) else None for k, v in known.items()},
            "totals_scope": "parsed_snapshots_only_missing_snapshots_not_zero",
            "reference_prices_are_open_or_executed": False,
            "quotation_effectiveness_validated": False}


def _shadow_object(root, collection, object_id):
    if not isinstance(object_id, str) or not re.fullmatch(r"[0-9a-f]{64}", object_id):
        raise ValueError("invalid_shadow_object_id")
    value, digest = _read(root / collection / (object_id + ".json"))
    if _digest(value) != object_id or digest != object_id:
        raise ValueError("shadow_immutable_object_hash_mismatch")
    return value


def _shadow_capacity(root, value, digest, operation):
    event = _shadow_object(root, "capacity_events", value.get("capacity_event_id"))
    if event != {k: v for k, v in value.items() if k != "capacity_event_id"} or event.get("operation") != operation:
        raise ValueError("shadow_capacity_event_binding_mismatch")
    fields = ("runtime_bytes", "limit_bytes", "capacity_report_reserve_bytes")
    if any(_count(event.get(k)) is None for k in fields):
        raise ValueError("shadow_capacity_count_missing")
    return {"status": "capacity_deferred", "reason": "shadow_storage_capacity_deferred",
        "capacity_event_id": value["capacity_event_id"], "latest_sha256": digest,
        **{k: event[k] for k in fields}, "partial_objects_preserved": True,
        "captured_forecast_versions": None, "captured_matches_this_run": None,
        "primary_pair_captures_this_run": None, "accuracy_or_profit_improvement_verified": False}


def _shadow_capture(runtime, context, now):
    root = runtime / "shadow_models"
    latest, latest_sha = _associated(root / "reports" / "latest.json", context, "shadow_models", now)
    if (not re.fullmatch(r"[0-9a-f]{40}", context.get("source_commit", ""))
            or latest.get("source_commit") != context["source_commit"]):
        raise ValueError("shadow_source_commit_mismatch")
    if latest.get("status") == "capacity_deferred":
        return _shadow_capacity(root, latest, latest_sha, "capture")
    run = _shadow_object(root, "runs", latest.get("report_id"))
    for key in ("source_commit", "protocol_id", "started_at", "completed_at", "captured_forecasts", "captured_matches"):
        if run.get(key) != latest.get(key):
            raise ValueError("shadow_latest_run_binding_mismatch")
    if (run.get("purpose") != "prospective_model_experiment" or
            latest.get("run_started_at") != run["started_at"] or latest.get("generated_at") != run["completed_at"]):
        raise ValueError("shadow_run_scope_or_time_mismatch")
    protocol = _shadow_object(root, "protocols", run["protocol_id"])
    if _time(protocol["registered_at"]) > _time(run["started_at"]):
        # First registration can occur after stage/run begin but before input
        # freeze. Do not falsely require it to predate the first task start.
        if _time(protocol["registered_at"]) > _time(run["completed_at"]):
            raise ValueError("shadow_protocol_registration_after_run")
    entries = run.get("competitions")
    if not isinstance(entries, list) or any(not isinstance(e, dict) or e.get("competition") not in LEAGUES for e in entries):
        raise ValueError("invalid_shadow_competitions")
    if len({e["competition"] for e in entries}) != len(entries):
        raise ValueError("duplicate_shadow_competition")
    arms = ("baseline180", "candidate90", "candidate365")
    captured = Counter({arm: 0 for arm in arms})
    rejected = Counter({arm: 0 for arm in arms})
    failed = Counter({arm: 0 for arm in arms})
    matches, primary_pairs = set(), set()
    eligibility_rejections = 0
    competition_status = Counter()
    for entry in entries:
        key = entry["competition"]
        if entry.get("status") not in ("completed", "completed_with_failures", "source_rejected", "no_eligible_future_matches"):
            raise ValueError("unrecognized_shadow_competition_status")
        competition_status[entry["status"]] += 1
        coverage, arm_entries = entry.get("coverage"), entry.get("arms")
        if not isinstance(coverage, list) or not isinstance(arm_entries, list):
            raise ValueError("missing_shadow_coverage")
        eligibility_rejections += len(coverage)
        seen_arms, pair_ids = set(), {arm: set() for arm in arms}
        for arm_entry in arm_entries:
            arm = arm_entry.get("arm")
            if arm not in arms or arm in seen_arms or arm_entry.get("status") not in ("fitted", "fit_failed"):
                raise ValueError("invalid_shadow_arm")
            seen_arms.add(arm)
            failed[arm] += arm_entry["status"] == "fit_failed"
            rows = arm_entry.get("forecasts")
            if not isinstance(rows, list):
                raise ValueError("shadow_arm_forecast_rows_missing")
            seen_matches = set()
            for row in rows:
                mid = row.get("match_id")
                if type(mid) is not int or mid < 1 or mid in seen_matches or row.get("status") not in ("captured", "rejected"):
                    raise ValueError("invalid_shadow_forecast_coverage")
                seen_matches.add(mid)
                if row["status"] == "rejected":
                    rejected[arm] += 1
                    continue
                forecast = _shadow_object(root, "forecasts", row.get("forecast_id"))
                if (forecast.get("competition") != key or forecast.get("match_id") != mid
                        or forecast.get("arm") != arm or forecast.get("input_id") != entry.get("input_id")
                        or forecast.get("protocol_id") != run["protocol_id"]
                        or forecast.get("source_commit") != run["source_commit"]
                        or not _time(run["started_at"]) <= _time(forecast["captured_at"]) <= _time(run["completed_at"])):
                    raise ValueError("shadow_forecast_run_binding_mismatch")
                captured[arm] += 1
                matches.add((key, mid))
                pair_ids[arm].add(mid)
            if arm_entry["status"] == "fit_failed" and rows:
                raise ValueError("failed_shadow_fit_cannot_have_forecasts")
        primary_pairs.update((key, mid) for mid in pair_ids["baseline180"] & pair_ids["candidate90"])
    for field, actual in (("captured_forecasts", sum(captured.values())), ("captured_matches", len(matches)),
                          ("competition_count", len(entries)), ("primary_pair_captures_this_run", len(primary_pairs)),
                          ("eligibility_rejections", eligibility_rejections)):
        if _count(latest.get(field)) != actual:
            raise ValueError("shadow_count_binding_mismatch")
    if latest.get("captured_by_arm") != dict(captured):
        raise ValueError("shadow_arm_count_binding_mismatch")
    return {"status": "associated_current_capture", "source_commit": run["source_commit"],
        "run_id": latest["report_id"], "protocol_id": run["protocol_id"], "latest_sha256": latest_sha,
        "started_at": run["started_at"], "completed_at": run["completed_at"],
        "competition_count": len(entries), "competition_status_counts": dict(competition_status),
        "captured_forecast_versions": sum(captured.values()), "captured_matches_this_run": len(matches),
        "captured_by_arm": dict(captured), "arm_fit_failures": dict(failed), "arm_forecast_rejections": dict(rejected),
        "eligibility_rejections": eligibility_rejections, "primary_pair_captures_this_run": len(primary_pairs),
        "count_basis": "this_run_pair_versions_not_new_independent_matches_or_settled_results",
        "production_models_changed": False, "accuracy_or_profit_improvement_verified": False}


def _shadow_scores(runtime, context, now, capture):
    root = runtime / "shadow_models"
    latest, latest_sha = _read(root / "reports" / "score-latest.json")
    if latest.get("status") == "capacity_deferred":
        if not _time(context["stage_started_at"]["shadow_models"]) <= _time(latest["scored_at"]) <= now:
            raise ValueError("shadow_capacity_score_outside_current_stage")
        return _shadow_capacity(root, latest, latest_sha, "score")
    score = _shadow_object(root, "score_runs", latest.get("report_id"))
    if {k: v for k, v in score.items() if k != "matches"} != {k: v for k, v in latest.items() if k != "report_id"}:
        raise ValueError("shadow_score_latest_parent_mismatch")
    scored = _time(score["scored_at"])
    if not _time(context["stage_started_at"]["shadow_models"]) <= scored <= now:
        raise ValueError("shadow_score_outside_current_stage")
    if capture.get("status") == "associated_current_capture" and scored < _time(capture["completed_at"]):
        raise ValueError("shadow_score_predates_current_capture")
    rows = score.get("matches")
    if not isinstance(rows, list):
        raise ValueError("shadow_score_rows_missing")
    allowed = ("candidate90_vs_baseline180", "candidate365_vs_baseline180")
    statuses = ("scored", "pending", "rejected")
    seen, identities = set(), set()
    for row in rows:
        identity = (row.get("protocol_id"), row.get("competition"), row.get("match_id"))
        if (not isinstance(identity[0], str) or not re.fullmatch(r"[0-9a-f]{64}", identity[0]) or
                identity[1] not in LEAGUES or type(identity[2]) is not int or identity[2] < 1 or
                row.get("comparison") not in allowed or row.get("status") not in statuses or
                (identity, row["comparison"]) in seen):
            raise ValueError("invalid_shadow_score_identity")
        seen.add((identity, row["comparison"]))
        identities.add(identity)
    if (_count(score.get("selected_matches")) != len(identities)
            or score.get("status_counts") != dict(Counter(r["status"] for r in rows))):
        raise ValueError("shadow_score_count_mismatch")
    if _count(score.get("forecast_versions")) is None:
        raise ValueError("shadow_score_forecast_count_missing")
    comparisons = []
    for comparison in allowed:
        selected = [r for r in rows if r["comparison"] == comparison]
        comparisons.append({"comparison": comparison,
            "paired_matches_by_protocol": len(selected),
            "status_counts": dict(Counter(r["status"] for r in selected)),
            "scored_matches_by_protocol": sum(r["status"] == "scored" for r in selected)})
    return {"status": "associated_current_score_snapshot", "scored_at": score["scored_at"],
        "score_run_id": latest["report_id"], "latest_sha256": latest_sha,
        "association": "current_stage_timestamp_and_immutable_score_hash_no_score_source_commit_field",
        "score_source_commit_provided": False, "forecast_versions": score["forecast_versions"],
        "selected_matches_by_protocol": len(identities),
        "independent_matches_across_protocols": len({i[1:] for i in identities}),
        "comparisons": comparisons, "comparison_rows_are_not_independent_matches": True,
        "accuracy_or_profit_improvement_verified": False}


def build_summary(runtime, context, outcomes, *, now=None):
    """Return only fixed keys/enums, counts, verified timestamps and run identifiers."""
    now = now or datetime.now(timezone.utc)
    runtime = Path(runtime)
    states = {k: outcomes.get(k) if outcomes.get(k) in OUTCOMES else "unknown" for k in STAGES}
    required = ("restore", "model", "replay", "check", "persist") + (("prospective",) if context.get("mode") == "train" else ())
    # Older summary contexts remain readable; newly wired independent stages
    # become explicit required operations when their stage marker is present.
    required += tuple(k for k in ("result_sync", "quotes", "shadow_models") if k in context.get("stage_started_at", {}))
    not_successful = [k for k in required if states[k] != "success"]
    result = {"schema_version": VERSION, "generated_at": _iso(now), "step_outcomes": states,
              "required_steps": list(required), "necessary_steps_outcome": "failure" if not_successful else "success",
              "necessary_steps_not_successful": not_successful,
              "job_conclusion": "not_observed_summary_precedes_final_gate_and_artifact_upload",
              "output_check_outcome": states["check"], "state_save_outcome": states["persist"],
              "summary_saved_in_same_model_state_commit": False,
              "model": _unknown(), "prospective_current": _unknown(), "prospective_cumulative": _unknown(),
              "fundamentals": _unknown(), "result_sync": _unknown(), "quotes": _unknown(),
              "shadow_models": {"capture": _unknown(), "scoring": _unknown()}}
    if states["shadow_models"] in ("success", "failure"):
        try:
            result["shadow_models"]["capture"] = _shadow_capture(runtime, context, now)
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            pass
        try:
            result["shadow_models"]["scoring"] = _shadow_scores(runtime, context, now, result["shadow_models"]["capture"])
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            pass
    if states["result_sync"] in ("success", "failure"):
        try:
            result["result_sync"] = _independent_results(runtime, context, now)
            if "cumulative" in result["result_sync"]:
                result["prospective_cumulative"] = result["result_sync"]["cumulative"]
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            pass
    if states["quotes"] in ("success", "failure"):
        try:
            result["quotes"] = _quote_collection(runtime, context, now)
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            pass
    if states["model"] not in ("success", "failure"):
        return result
    try:
        report, sha = _associated(runtime / "reports" / "latest.json", context, "model", now)
        started = _time(report["run_started_at"])
        if (report.get("run_id") != started.strftime("%Y%m%dT%H%M%S%fZ")
                or report.get("source_commit") != context.get("source_commit")
                or not re.fullmatch(r"[0-9a-f]{40}", context.get("source_commit", ""))
                or report.get("mode") != context.get("mode")):
            raise ValueError("model_run_association_failed")
        if context.get("league") != "all" and report.get("requested_competitions") != [context.get("league")]:
            raise ValueError("model_scope_association_failed")
        result["model"] = _model_summary(report)
        result["model_run_id"] = report["run_id"]
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return result
    if states["prospective"] in ("success", "failure") or report.get("prospective_captures"):
        try:
            result["prospective_current"] = _current_capture(runtime, report, sha, context, now)
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            result["prospective_current"] = _unknown("current_coverage_missing_invalid_or_unassociated")
        try:
            current, _ = _associated(runtime / "prospective" / "latest.json", context, "prospective", now)
            if current.get("current_run_id") != report["run_id"]:
                raise ValueError("prospective_run_mismatch")
            if "cumulative" not in result["result_sync"]:
                result["prospective_cumulative"] = _cumulative(current)
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            pass
    if states["fundamentals"] in ("success", "failure"):
        try:
            facts, _ = _associated(runtime / "fundamentals" / "reports" / "latest.json", context, "fundamentals", now)
            result["fundamentals"] = _fundamentals_summary(facts, report["run_id"])
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            pass
    return result


def markdown(value):
    def n(row, key):
        return str(row[key]) if row.get(key) is not None else "unknown"
    model, current, cumulative, facts = (value[k] for k in ("model", "prospective_current", "prospective_cumulative", "fundamentals"))
    synced, quotes = value.get("result_sync", {}), value.get("quotes", {})
    shadow = value.get("shadow_models", {})
    shadow_capture, shadow_score = shadow.get("capture", {}), shadow.get("scoring", {})
    lines = ["# 足球模型分层运行结果", "", "| 层次 | 本次实际结果 |", "|---|---|",
        f"| 必要步骤 | {value['necessary_steps_outcome']} |",
        f"| 模型 CLI | {value['step_outcomes']['model']}；已知支持 {n(model, 'known_supported_predictions')} 场；范围完整时合计 {n(model, 'supported_predictions')} 场 |",
        f"| 本轮前瞻 | {value['step_outcomes']['prospective']}；观测到接受 {n(current, 'observed_accepted_predictions')}、拒绝 {n(current, 'observed_rejected_predictions')}、原模型不支持 {n(current, 'observed_unsupported_predictions')} |",
        f"| 累计前瞻 | 版本 {n(cumulative, 'forecast_versions')}；协议内去重 {n(cumulative, 'unique_primary_matches_by_protocol')}；跨协议独立比赛 {n(cumulative, 'unique_matches_across_protocols')} |",
        f"| 累计成熟结果 | 协议内已评分 {n(cumulative, 'mature_scored_matches_by_protocol')}；待赛果 {n(cumulative, 'pending_matches_by_protocol')}；结果版本 {n(cumulative, 'result_versions')} |",
        f"| 独立赛果同步 | {value['step_outcomes'].get('result_sync', 'unknown')}；目标 {n(synced, 'target_matches')}；新结果版本 {n(synced.get('status_counts', {}), 'result_appended')}；请求 {n(synced, 'api_requests')} |",
        f"| 真实报价积累 | {value['step_outcomes'].get('quotes', 'unknown')}；候选 {n(quotes, 'candidate_matches')}；已解析快照 {n(quotes, 'parsed_snapshots')}；完整参考行 {n(quotes, 'complete_reference_rows')}；请求 {n(quotes, 'api_requests')} |",
        f"| F4 模型影子记录 | {value['step_outcomes'].get('shadow_models', 'unknown')}；记录状态 {shadow_capture.get('status', 'unknown')}；本次预测版本 {n(shadow_capture, 'captured_forecast_versions')}；覆盖比赛 {n(shadow_capture, 'captured_matches_this_run')}；90/180配对版本 {n(shadow_capture, 'primary_pair_captures_this_run')}；准入拒绝 {n(shadow_capture, 'eligibility_rejections')} |",
        f"| F4 影子累计评分 | 预测版本 {n(shadow_score, 'forecast_versions')}；协议内配对比赛 {n(shadow_score, 'selected_matches_by_protocol')}；跨协议独立比赛 {n(shadow_score, 'independent_matches_across_protocols')} |",
        f"| 基本面采集 | {value['step_outcomes']['fundamentals']}；报告 {facts['status']}；采集完整 {n(facts, 'complete_collection_matches')}、部分 {n(facts, 'partial_collection_matches')}、正常缺失 {n(facts, 'normal_missing_matches')}、未知 {n(facts, 'unknown_collection_matches')} |",
        f"| 输出检查 | {value['output_check_outcome']} |",
        f"| 状态保存 | {value['state_save_outcome']} |",
        f"| 回放包检查 | {value['step_outcomes']['replay']} |", "",
        "本轮前瞻按关联的 coverage 对象计数；步骤未完成时仅表示已观测记录，不代表目标全集。累计数未用于相减推算本轮。",
        "基本面的完整仅指本次请求的采集操作；正常缺失不代表数值零，特征已保存不代表全字段完整或已经调整概率。",
        "逐联赛冻结由独立checkpoint核关联；赛果同步与报价采集不要求本次训练成功。参考报价不代表开放可买或实际成交。",
        "F4本次配对版本不等于新增独立比赛；90/180与365/180评分分别统计，不能把两组相加当比赛数。影子评分按本stage时间和不可变评分指纹关联，评分原件未单列执行源码提交；不强迫历史预测使用本次源码。",
        "模型支持、前瞻资格、保存成功与预测准确率验收分别处理。基本面局部失败不否决独立比分模型。", ""]
    for label, row in (("本轮前瞻原因", current), ("基本面原因", facts)):
        reasons = row.get("reason_counts")
        lines.append(label + "：" + ("；".join(f"{key}={count}" for key, count in sorted(reasons.items())) if reasons else "unknown / 未记录原因"))
    if facts.get("global_issue_counts"):
        lines.append("基本面全局事项：" + "；".join(f"{key}={count}" for key, count in sorted(facts["global_issue_counts"].items())))
    lines.extend(["", "这些是步骤真实 outcome，不是 continue-on-error 处理后的 conclusion；必要步骤结果不是最终 GitHub job conclusion。",
        "本摘要在状态保存之后生成，以免预告保存成功；摘要自身在运行摘要和便捷附件中，不声称已进入同次 model-state 提交。", ""])
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description="本轮关联与分层运行摘要；不接入认证、不改模型")
    parser.add_argument("command", choices=("begin", "mark", "render"))
    parser.add_argument("--context", required=True)
    parser.add_argument("--stage", choices=("model", "prospective", "result_sync", "quotes", "fundamentals", "shadow_models"))
    parser.add_argument("--runtime", default="runtime")
    parser.add_argument("--output")
    parser.add_argument("--summary")
    args = parser.parse_args(argv)
    path = Path(args.context)
    now = datetime.now(timezone.utc)
    if args.command == "begin":
        sha, mode, league = os.environ.get("GITHUB_SHA", ""), os.environ.get("MODE"), os.environ.get("LEAGUE")
        if not re.fullmatch(r"[0-9a-f]{40}", sha) or mode not in ("audit", "train") or league not in LEAGUES | {"all"}:
            raise SystemExit("invalid_workflow_context")
        value = {"schema_version": VERSION, "started_at": _iso(now), "source_commit": sha,
                 "mode": mode, "league": league, "stage_started_at": {}}
        path.write_bytes(_canonical(value))
    elif args.command == "mark":
        value, _ = _read(path)
        if args.stage is None or args.stage in value["stage_started_at"]:
            raise SystemExit("invalid_or_repeated_stage_marker")
        value["stage_started_at"][args.stage] = _iso(now)
        path.write_bytes(_canonical(value))
    else:
        try:
            value, _ = _read(path)
        except (OSError, ValueError, TypeError):
            value = {"mode": os.environ.get("MODE")}
        outcomes = {k: os.environ.get(k.upper() + "_OUTCOME") for k in STAGES}
        report = build_summary(args.runtime, value, outcomes, now=now)
        if args.output:
            Path(args.output).write_bytes(_canonical(report))
        if args.summary:
            with Path(args.summary).open("a", encoding="utf-8") as handle:
                handle.write(markdown(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
