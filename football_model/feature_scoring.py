"""Independent future-result scoring of immutable first candidate captures.

This evaluates probability forecasts, never odds, EV, trades or confirmation.
No API client is created for an empty/pending queue. Final scores retain their
first evaluation; bounded later result checks append corrections separately.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import json
from pathlib import Path
import re

from .competition_scope import require_league
from .data import APIClient, APIError, DataError, _iso, _utc, normalize_fixtures
from .feature_research import _mean_metrics, score_forecast
from .feature_snapshots import HORIZONS, SnapshotError, _digest, _read, verify_capture
from .history_archive import read_bytes as history_read_bytes, exists as history_exists

VERSION = "feature-future-scoring-v1"
QUEUE_VERSION = "feature-future-scoring-v2"
RESULT_DELAY = timedelta(hours=4)
RESULT_REFRESH = timedelta(hours=24)
RESULT_RECHECK_WINDOW = timedelta(days=7)


def _sha(path):
    return hashlib.sha256(history_read_bytes(path)).hexdigest()


def _raw(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True,
                       separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")


def _path(runtime, relative):
    if not isinstance(relative, str) or not relative:
        raise SnapshotError("invalid_scoring_reference_path")
    requested = Path(relative)
    if requested.is_absolute() or ".." in requested.parts:
        raise SnapshotError("unsafe_scoring_reference_path")
    result = (runtime / requested).resolve()
    if not result.is_relative_to(runtime):
        raise SnapshotError("scoring_reference_escapes_runtime")
    return result


def _reference(runtime, path):
    return {"path": path.relative_to(runtime).as_posix(), "sha256": _sha(path)}


def _read_ref(runtime, ref):
    if not isinstance(ref, dict) or not re.fullmatch(r"[0-9a-f]{64}", str(ref.get("sha256", ""))):
        raise SnapshotError("invalid_scoring_hash_reference")
    path = _path(runtime, ref.get("path"))
    if not history_exists(path) or _sha(path) != ref["sha256"]:
        raise SnapshotError("scoring_reference_hash_mismatch")
    return _read(path)


def _immutable(root, category, value):
    raw = _raw(value)
    path = root / category / (hashlib.sha256(raw).hexdigest() + ".json")
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("xb") as handle:
            handle.write(raw)
    except FileExistsError:
        if path.read_bytes() != raw:
            raise SnapshotError("immutable_scoring_artifact_collision") from None
    return path


def _pointer(path, value, *, first=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    if first:
        try:
            with path.open("xb") as handle:
                handle.write(_raw(value))
        except FileExistsError:
            if _read(path) != value:
                raise SnapshotError("first_scoring_pointer_cannot_change") from None
    else:
        temporary = path.with_name("." + path.name + ".tmp")
        temporary.write_bytes(_raw(value))
        temporary.replace(path)


def _tick(clock, previous):
    current = _utc(clock())
    if current < previous:
        raise SnapshotError("scoring_clock_moved_backwards")
    return current


def _reason(exc):
    return str(exc) if isinstance(exc, SnapshotError) else type(exc).__name__


def _capture(runtime, anchor, league, protocol_hash, now):
    descriptor = _read(anchor)
    receipt = _read_ref(runtime, descriptor)
    payload = verify_capture(runtime, receipt)
    expected_horizon, expected_match = anchor.parent.name, anchor.parent.parent.name
    fixture = payload["fixture"]
    if (expected_horizon not in HORIZONS or not expected_match.isdigit()
            or str(fixture["match_id"]) != expected_match or payload["horizon"] != expected_horizon
            or payload.get("league") != league or receipt.get("league") != league
            or receipt.get("match_id") != fixture["match_id"] or receipt.get("horizon") != expected_horizon
            or payload.get("protocol_sha256") != protocol_hash or receipt.get("protocol_sha256") != protocol_hash
            or _digest(payload.get("protocol")) != protocol_hash):
        raise SnapshotError("capture_anchor_identity_or_protocol_mismatch")
    if (receipt.get("status") != "captured" or receipt.get("live_capture_verified") is not True
            or payload.get("candidate_status") != "predicted"):
        raise SnapshotError("first_candidate_not_live_and_predicted")
    if _utc(receipt["storage_completed_at"]) > now or _utc(payload["created_at"]) > now:
        raise SnapshotError("capture_saved_in_future")
    if _utc(receipt["storage_completed_at"]) >= _utc(fixture["kickoff"]):
        raise SnapshotError("capture_not_completed_before_kickoff")
    return payload, receipt, _reference(runtime, anchor)


def _provider_league(runtime, payload, competition):
    refs = [ref for ref in payload["source_refs"] if ref.get("kind") == "current_fixture_context"]
    if len(refs) != 1:
        raise SnapshotError("capture_provider_league_source_missing")
    ref = refs[0]
    context = _read_ref(runtime, {"path": ref.get("runtime_relative_path", ref.get("path")),
                                  "sha256": ref.get("sha256")})
    rows = context.get("fixture_response", {}).get("response")
    if not isinstance(rows, list) or len(rows) != 1:
        raise SnapshotError("capture_provider_league_source_invalid")
    result = rows[0].get("league", {})
    if (type(result.get("id")) is not int or result["id"] <= 0
            or result.get("name") != competition["name"] or result.get("country") != competition["country"]):
        raise SnapshotError("capture_provider_league_identity_mismatch")
    return {key: result[key] for key in ("id", "name", "country")}


def _normalize_result(envelope, fixture, league, fetched_now):
    if (not isinstance(envelope, dict) or envelope.get("endpoint") != "/fixtures"
            or envelope.get("params") != {"id": fixture["match_id"]} or envelope.get("errors")
            or envelope.get("paging", {}).get("total", 1) != 1):
        raise SnapshotError("invalid_result_response_envelope")
    fetched = _utc(envelope.get("fetched_at"))
    if fetched > fetched_now or fetched < _utc(fixture["kickoff"]) + RESULT_DELAY:
        raise SnapshotError("result_observation_time_invalid")
    rows = envelope.get("response")
    if not isinstance(rows, list) or len(rows) != 1:
        raise SnapshotError("result_response_count_not_one")
    raw = rows[0]
    raw_identity = raw.get("fixture", {})
    if (raw_identity.get("id") != fixture["match_id"]
            or _utc(raw_identity.get("date")) != _utc(fixture["kickoff"])
            or raw.get("teams", {}).get("home", {}).get("id") != fixture["home_id"]
            or raw.get("teams", {}).get("away", {}).get("id") != fixture["away_id"]
            or any(raw.get("league", {}).get(key) != value for key, value in league.items())):
        raise SnapshotError("result_fixture_or_league_identity_mismatch")
    status = raw_identity.get("status", {}).get("short")
    if status not in ("FT", "AET", "PEN"):
        return None, status
    normalized = normalize_fixtures(rows, fetched_now)
    if len(normalized) != 1:
        raise SnapshotError("result_normalization_did_not_return_exact_match")
    result = normalized[0]
    if any(result[key] != fixture[key] for key in ("match_id", "kickoff", "home_id", "away_id")):
        raise SnapshotError("normalized_result_identity_mismatch")
    return result, status


def _summary(records):
    return {"n": len(records), "baseline": _mean_metrics([row["scores"]["M0"] for row in records]),
            "candidate": _mean_metrics([row["scores"]["M1"] for row in records]),
            "mean_paired_scoreline_nll_difference": (sum(row["scores"]["M1"]["scoreline_nll"]
                - row["scores"]["M0"]["scoreline_nll"] for row in records) / len(records)) if records else None,
            "independent_units": "fixture_id_within_one_horizon",
            "confirmation_eligible": False, "betting_returns": None}


def _queue_members(runtime, league, protocol_hash, now):
    """Retain all observed opportunities, including failed/no-candidate cases.

    A repeated poll is a version of one fixture/horizon opportunity, never an
    independent forecast. The first due attempt and all subsequent failures
    remain visible; a later candidate cannot replace the first frozen baseline.
    """
    root = runtime / "feature_snapshots" / protocol_hash
    members, rejected = {}, []
    for path in sorted((root / "queue/history").glob("*.json")):
        ref = _reference(runtime, path)
        try:
            value = _read(path)
            if value.get("league") != league:
                continue
            mid, horizon = value.get("match_id"), value.get("horizon")
            if type(mid) is not int or mid <= 0 or horizon not in HORIZONS:
                raise SnapshotError("queue_fixture_or_horizon_identity_invalid")
            observed = _utc(value["queue_observed_at"])
            kickoff = _utc(value["kickoff"])
            if observed > now:
                raise SnapshotError("queue_observation_saved_in_future")
            member = members.setdefault((mid, horizon), {"match_id": mid, "horizon": horizon,
                "kickoff": _iso(kickoff), "observations": []})
            if member["kickoff"] != _iso(kickoff):
                member["identity_conflict"] = True
            member["observations"].append((observed, ref["sha256"], value, ref))
        except (ValueError, KeyError, TypeError, AttributeError, OSError) as exc:
            rejected.append({"queue_record_ref": ref, "status": "rejected",
                             "reason": _reason(exc)})
    for member in members.values():
        observations = sorted(member.pop("observations"), key=lambda row: (row[0], row[1]))
        member["first_observation"], member["latest_observation"] = observations[0][2], observations[-1][2]
        member["queue_record_refs"] = [row[3] for row in observations]
        member["capture_receipt_refs"] = [row[2]["receipt_ref"] for row in observations if isinstance(row[2].get("receipt_ref"), dict)]
        member["observation_count"] = len(observations)
        member["observation_status_counts"] = dict(Counter(row[2].get("status", "unknown") for row in observations))
        member["first_queue_ref"], member["latest_queue_ref"] = observations[0][3], observations[-1][3]
        attempts = [row for row in observations if row[2].get("planned_state_at_observation") in ("due", "late")]
        first_attempt_path = root / "captures" / league / str(member["match_id"]) / member["horizon"] / "first_attempt.json"
        if first_attempt_path.exists():
            try:
                saved_ref = _read(first_attempt_path)
                saved = _read_ref(runtime, saved_ref)
                selected = [row for row in attempts if row[3] == saved_ref and row[2] == saved]
                if len(selected) != 1:
                    raise SnapshotError("first_attempt_not_bound_to_queue_observation")
                attempts = selected + [row for row in attempts if row[3] != saved_ref]
            except (ValueError, KeyError, TypeError, AttributeError, OSError) as exc:
                member["queue_validation_error"] = _reason(exc)
        member["first_attempt_ref"] = attempts[0][3] if attempts else None
        member["first_attempt_status"] = attempts[0][2].get("status") if attempts else None
        member["first_attempt_reason"] = attempts[0][2].get("reason") if attempts else None
        member["prior_failures_retained"] = [row[3] for row in observations if
            row[2].get("status") in ("ineligible", "unsupported", "late", "failed", "missed_window")]
    return list(members.values()), rejected


def _queue_capture(runtime, anchor, member, league, protocol_hash, now):
    from .feature_snapshots import verify_baseline_capture
    receipt_ref = _read(anchor)
    if receipt_ref not in member["capture_receipt_refs"]:
        raise SnapshotError("first_baseline_not_bound_to_queue_observation")
    receipt = _read_ref(runtime, receipt_ref)
    payload = verify_baseline_capture(runtime, receipt)
    fixture = payload["fixture"]
    if (fixture["match_id"] != member["match_id"] or payload["horizon"] != member["horizon"]
            or fixture["kickoff"] != member["kickoff"] or payload.get("league") != league
            or receipt.get("league") != league or receipt.get("match_id") != fixture["match_id"]
            or receipt.get("horizon") != member["horizon"]
            or payload.get("protocol_sha256") != protocol_hash
            or receipt.get("protocol_sha256") != protocol_hash
            or _digest(payload.get("protocol")) != protocol_hash):
        raise SnapshotError("capture_anchor_identity_or_protocol_mismatch")
    if (receipt.get("status") != "captured" or receipt.get("live_capture_verified") is not True
            or _utc(receipt["storage_completed_at"]) > now
            or _utc(payload["created_at"]) > now
            or _utc(receipt["storage_completed_at"]) >= _utc(fixture["kickoff"])):
        raise SnapshotError("first_baseline_capture_not_live_or_saved_in_future")
    candidate_reason = payload.get("candidate_missing_reason", "candidate_not_available_at_first_capture")
    candidate = None
    if payload.get("candidate_status") == "predicted":
        try:
            verify_capture(runtime, receipt)
            candidate = payload["candidate"]["prediction"]
            candidate_reason = None
        except (ValueError, KeyError, TypeError, AttributeError, OSError) as exc:
            candidate_reason = "candidate_rejected:" + _reason(exc)
    return payload, receipt, candidate, candidate_reason


def _queue_summary(records, items):
    paired = [row for row in records if row["scores"].get("M1") is not None]
    strategies = [row for row in records if row["scores"].get("M_strategy") is not None]
    summary = _summary(paired)
    summary.update(requested_horizon_records=len(items), baseline_scored_n=len(records),
        candidate_scored_n=len(paired), paired_scored_n=len(paired), strategy_scored_n=len(strategies),
        baseline_all_available=_mean_metrics([row["scores"]["M0"] for row in records]),
        strategy_with_fallback=_mean_metrics([row["scores"]["M_strategy"] for row in strategies]),
        baseline_fallback_n=sum(row["strategy_choice"] == "M0_fallback" for row in records),
        original_candidate_scores_currently_unverified_n=sum(row.get("candidate_currently_unverified") is True for row in records),
        opportunity_status_counts=dict(Counter(item.get("opportunity_status", item["status"]) for item in items)),
        result_status_counts=dict(Counter(item.get("result_status", "not_requested") for item in items)),
        unscored_horizon_records=len(items)-len(records),
        failed_first_attempt_n=sum(item.get("first_attempt_status") in ("failed", "ineligible", "unsupported", "late") for item in items),
        denominator_scope="all_observed_queue_opportunities_including_failures_and_fallbacks")
    return summary


def _retained_score_view(saved, payload, baseline, candidate, result):
    """Replay M0 independently; current candidate damage never changes history.

    The returned diagnostic projection can exclude an unverified original M1
    while retaining its exact immutable artifact. It does not turn the original
    M1 decision into a retrospectively successful baseline fallback.
    """
    base = score_forecast(baseline, result, extended_diagnostics=True)
    if saved.get("baseline_prediction_sha256") != _digest(baseline) or saved["scores"].get("M0") != base:
        raise SnapshotError("retained_baseline_score_replay_mismatch")
    choice = saved.get("strategy_choice")
    if choice not in ("M1", "M0_fallback"):
        raise SnapshotError("retained_strategy_choice_invalid")
    raw_candidate = payload.get("candidate") or {}
    stored_candidate = saved["scores"].get("M1")
    if choice == "M1":
        prediction = raw_candidate.get("prediction")
        if (not isinstance(prediction, dict) or saved.get("candidate_prediction_sha256") != _digest(prediction)
                or saved.get("candidate_model_hash") != raw_candidate.get("model_hash")):
            raise SnapshotError("retained_candidate_forecast_fingerprint_mismatch")
        if stored_candidate is not None and stored_candidate != score_forecast(prediction, result, extended_diagnostics=True):
            raise SnapshotError("retained_candidate_score_replay_mismatch")
        if saved["scores"].get("M_strategy") != stored_candidate:
            raise SnapshotError("retained_original_candidate_strategy_mismatch")
    elif stored_candidate is not None or saved["scores"].get("M_strategy") != base:
        raise SnapshotError("retained_original_baseline_fallback_mismatch")
    view = {**saved, "scores": dict(saved["scores"])}
    if choice == "M1" and candidate is None:
        view.update(candidate_currently_unverified=True,
                    integrity_projection="M0_verified; original_M1_and_original_strategy_excluded")
        view["scores"].update(M1=None, M_strategy=None)
    elif choice == "M1" and stored_candidate is None:
        corrected = score_forecast(candidate, result, extended_diagnostics=True)
        view["scores"].update(M1=corrected, M_strategy=corrected)
        view.update(candidate_reverified_projection=True,
                    integrity_projection="reverified_original_M1_on_preserved_independent_result")
    return view


def _score_queue_run(runtime, league, *, config, protocol, source_commit, clock, client_factory):
    """v2: independent results for first legal frozen M0 and explicit fallback."""
    require_league(league)
    started = _utc(clock())
    protocol_hash = _digest(protocol)
    root = runtime / "feature_models" / "scoring" / protocol_hash
    root.mkdir(parents=True, exist_ok=True)
    competition = next(item for item in config["competitions"] if item["key"] == league)
    report = {"schema_version": QUEUE_VERSION, "league": league, "source_commit": source_commit,
        "protocol_sha256": protocol_hash, "run_started_at": _iso(started),
        "future_scoring_denominator": "all_queue", "confirmation_eligible": False,
        "confirmation_independent_matches": 0, "accuracy_improvement_verified": False,
        "betting_returns": None, "quotes_used": False, "api_requests": 0,
        "api_collection_performed": False, "api_daily_remaining": None,
        "api_quota_status": "not_observed", "queue": [], "issues": [],
        "result_recheck_policy": {"refresh_hours": 24, "window_days_after_kickoff": 7},
        "first_score_scope": "first_scores_by_horizon_contains_only_first_scores_independently_reverified_this_run",
        "retained_historical_first_score_refs": [],
        "forecast_selection": "first_legal_baseline_capture; first_candidate_never_replaces_failure"}
    client, fetched, first_records, current_records = None, {}, [], []
    lock_root = runtime.parent / ".feature-scoring-locks"
    lock_root.mkdir(parents=True, exist_ok=True)
    lock_name = hashlib.sha256((str(runtime)+protocol_hash).encode()).hexdigest()+".lock"
    with (lock_root / lock_name).open("a") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        members, rejected = _queue_members(runtime, league, protocol_hash, started)
        report["queue"].extend(rejected)
        report["issues"].extend({"queue_record_ref": item["queue_record_ref"], "reason": item["reason"]} for item in rejected)
        for member in sorted(members, key=lambda item: (item["match_id"], item["horizon"])):
            latest = member.pop("latest_observation")
            member.pop("first_observation")
            item = {**member, "status": "pending_capture", "result_status": "not_requested",
                    "opportunity_status": "pending_capture", "latest_capture_status": latest.get("status")}
            report["queue"].append(item)
            historical_first = root / "scores" / league / str(member["match_id"]) / member["horizon"] / "first.json"
            if historical_first.exists():
                try:
                    historical_ref = _read(historical_first)
                    report["retained_historical_first_score_refs"].append({
                        "match_id": member["match_id"], "horizon": member["horizon"],
                        "score_ref": historical_ref, "current_verification_not_implied": True})
                    item["retained_historical_first_score_ref"] = historical_ref
                except (ValueError, KeyError, TypeError, OSError):
                    item["historical_first_pointer_unreadable"] = True
            retained_first = retained_current = None
            try:
                now = _tick(clock, started)
                if member.get("identity_conflict"):
                    raise SnapshotError("queue_fixture_kickoff_changed")
                if member.get("queue_validation_error"):
                    raise SnapshotError(member["queue_validation_error"])
                anchor = runtime / "feature_snapshots" / protocol_hash / "captures" / league / str(member["match_id"]) / member["horizon"] / "first.json"
                if not anchor.exists():
                    from .feature_snapshots import _horizon_state
                    state = _horizon_state({"kickoff": member["kickoff"]}, member["horizon"], now, protocol)
                    if state["status"] == "late":
                        status = "missed_window"
                    elif latest.get("status") in ("failed", "ineligible"):
                        status = "failed"
                    elif latest.get("status") == "unsupported":
                        status = "rejected"
                    else:
                        status = "pending_capture"
                    item.update(status=status, opportunity_status=status,
                        reason=latest.get("reason", "no_legal_frozen_baseline"), baseline_available=False,
                        candidate_available=False, strategy_scored=False)
                    continue
                payload, receipt, candidate, fallback_reason = _queue_capture(runtime, anchor, member, league, protocol_hash, now)
                baseline = payload["baseline"].get("prediction", payload["baseline"])
                anchor_ref = _reference(runtime, anchor)
                fixture, horizon = payload["fixture"], payload["horizon"]
                item.update(anchor_path=anchor_ref["path"], capture_anchor_ref=anchor_ref,
                    baseline_available=True, candidate_available=candidate is not None,
                    opportunity_status="success" if candidate is not None else "baseline_only",
                    fallback_reason=fallback_reason, strategy_choice="M1" if candidate is not None else "M0_fallback")
                mature_at = _utc(fixture["kickoff"]) + RESULT_DELAY
                if now < mature_at:
                    item.update(status="pending", result_status="pending_maturity",
                                reason="kickoff_plus_four_hours_not_reached", mature_at=_iso(mature_at))
                    continue
                ledger = root / "scores" / league / str(fixture["match_id"]) / horizon
                first_path, latest_path = ledger / "first.json", ledger / "latest.json"
                previous = _read_ref(runtime, _read(latest_path)) if latest_path.exists() else None
                provider = _provider_league(runtime, payload, competition)
                original_choice = previous["strategy_choice"] if previous is not None else item["strategy_choice"]
                original_fallback_reason = previous["fallback_reason"] if previous is not None else fallback_reason
                def scores(result):
                    base = score_forecast(baseline, result, extended_diagnostics=True)
                    corrected = (score_forecast(candidate, result, extended_diagnostics=True)
                        if candidate is not None and original_choice == "M1" else None)
                    return {"M0": base, "M1": corrected,
                        "M_strategy": corrected if original_choice == "M1" else base}
                if previous is not None:
                    first = _read_ref(runtime, _read(first_path))
                    views = []
                    for saved in (first, previous):
                        if saved.get("capture_anchor_ref") != anchor_ref:
                            raise SnapshotError("existing_score_capture_anchor_changed")
                        if (saved.get("strategy_choice") != first.get("strategy_choice")
                                or saved.get("fallback_reason") != first.get("fallback_reason")):
                            raise SnapshotError("retained_original_strategy_changed")
                        result, _ = _normalize_result(_read_ref(runtime, saved["result_ref"]), fixture, provider, now)
                        if result != saved["result"]:
                            raise SnapshotError("retained_score_replay_mismatch")
                        views.append(_retained_score_view(saved, payload, baseline, candidate, result))
                    retained_first, retained_current = views
                    if retained_current.get("candidate_reverified_projection"):
                        item.update(candidate_reverified_projection=True,
                            integrity_projection=retained_current["integrity_projection"])
                    item.update(original_strategy_choice=first["strategy_choice"],
                                original_fallback_reason=first["fallback_reason"],
                                strategy_choice=first["strategy_choice"], fallback_reason=first["fallback_reason"],
                                candidate_current_rejection_reason=fallback_reason)
                    if candidate is None and first["strategy_choice"] == "M1":
                        item.update(candidate_currently_unverified=True,
                            original_candidate_score_ref=_read(first_path),
                            integrity_projection="M0_verified; original_M1_and_original_strategy_excluded")
                    last_checked = _utc(previous["result_observed_at"])
                    check_path = ledger / "last_checked.json"
                    if check_path.exists():
                        check = _read_ref(runtime, _read(check_path))
                        raw = _read_ref(runtime, check["result_ref"])
                        _normalize_result(raw, fixture, provider, now)
                        if (check.get("match_id") != fixture["match_id"] or check.get("horizon") != horizon
                                or check.get("observed_at") != raw.get("fetched_at")):
                            raise SnapshotError("retained_result_check_identity_or_time_invalid")
                        last_checked = max(last_checked, _utc(check["observed_at"]))
                    if now-last_checked < RESULT_REFRESH or now > _utc(fixture["kickoff"])+RESULT_RECHECK_WINDOW:
                        first_records.append(retained_first)
                        current_records.append(retained_current)
                        item.update(status="already_scored", result_status="scored",
                                    strategy_scored=retained_current["scores"].get("M_strategy") is not None,
                                    first_score_ref=_read(first_path), current_score_ref=_read(latest_path))
                        continue
                mid = fixture["match_id"]
                if mid not in fetched:
                    if client is None:
                        try:
                            client = (client_factory or APIClient)(runtime / "cache",
                                max_requests=config.get("max_requests_per_run"), daily_limit=config.get("api_daily_limit"),
                                max_run_seconds=config.get("api_max_run_seconds"))
                            client.status()
                        except (APIError, ValueError, OSError):
                            raise SnapshotError("result_api_initialization_or_status_failed") from None
                    envelope = client.fetch_fixture(mid, force=True, max_age_seconds=0)
                    result, status = _normalize_result(envelope, fixture, provider, _tick(clock, now))
                    result_path = _immutable(root, "result_observations", envelope)
                    fetched[mid] = result, status, envelope, result_path
                result, status, envelope, result_path = fetched[mid]
                checked, _ = _normalize_result(envelope, fixture, provider, _tick(clock, now))
                if checked != result:
                    raise SnapshotError("shared_fixture_result_mismatch")
                if result is None:
                    item.update(status="pending_result", result_status="pending_result", provider_status=status,
                                result_ref=_reference(runtime, result_path))
                    if retained_first is not None:
                        first_records.append(retained_first)
                        current_records.append(retained_current)
                        item["retained_verified_previous_score"] = True
                    continue
                result_hash = _digest({key: result[key] for key in
                    ("match_id", "kickoff", "home_id", "away_id", "home_goals", "away_goals")})
                saved = {"schema_version": QUEUE_VERSION, "league": league, "match_id": mid, "horizon": horizon,
                    "protocol_sha256": protocol_hash, "capture_anchor_ref": anchor_ref,
                    "capture_receipt_ref": _read(anchor), "capture_payload_ref": receipt["payload_ref"],
                    "snapshot_id": payload["snapshot_id"], "first_queue_ref": member["first_queue_ref"],
                    "first_attempt_ref": member["first_attempt_ref"], "first_attempt_status": member["first_attempt_status"],
                    "baseline_prediction_sha256": _digest(baseline),
                    "candidate_prediction_sha256": _digest(payload["candidate"]["prediction"]) if original_choice == "M1" else None,
                    "candidate_model_hash": payload["candidate"].get("model_hash") if original_choice == "M1" else None,
                    "strategy_choice": original_choice, "fallback_reason": original_fallback_reason,
                    "candidate_validation_status": "verified" if candidate is not None else "unavailable_or_rejected",
                    "current_candidate_rejection_reason": fallback_reason,
                    "result": result, "provider_status": status, "result_semantic_sha256": result_hash,
                    "result_ref": _reference(runtime, result_path), "result_observed_at": envelope["fetched_at"],
                    "scored_at": _iso(_tick(clock, now)), "confirmation_eligible": False, "scores": scores(result),
                    "target": "regulation_time_90_plus_stoppage", "betting_returns": None}
                if previous is None or previous["result_semantic_sha256"] != result_hash:
                    if previous is not None:
                        saved["revision_of"] = _read(latest_path)
                    score_ref = _reference(runtime, _immutable(root, "score_versions", saved))
                    _pointer(latest_path, score_ref)
                    if previous is None:
                        _pointer(first_path, score_ref, first=True)
                    item["status"] = "scored" if previous is None else "result_revision_appended"
                else:
                    saved, score_ref = previous, _read(latest_path)
                    item["status"] = "result_unchanged"
                observation = {"match_id": mid, "horizon": horizon, "observed_at": envelope["fetched_at"],
                               "result_ref": _reference(runtime, result_path)}
                _pointer(ledger / "last_checked.json", _reference(runtime, _immutable(root, "result_check_receipts", observation)))
                first_saved = _read_ref(runtime, _read(first_path))
                first_result, _ = _normalize_result(_read_ref(runtime, first_saved["result_ref"]), fixture, provider, now)
                first_records.append(_retained_score_view(first_saved, payload, baseline, candidate, first_result))
                current_view = _retained_score_view(saved, payload, baseline, candidate, result)
                current_records.append(current_view)
                item.update(result_status="scored", strategy_scored=current_view["scores"].get("M_strategy") is not None,
                            first_score_ref=_read(first_path), current_score_ref=score_ref)
            except (ValueError, APIError, KeyError, TypeError, AttributeError, OSError) as exc:
                item.update(status="failed", reason=_reason(exc))
                if item.get("baseline_available") is True:
                    item["result_status"] = "result_failed"
                else:
                    item["opportunity_status"] = "rejected"
                report["issues"].append({"match_id": member["match_id"], "horizon": member["horizon"], "reason": _reason(exc)})
                if retained_first is not None:
                    first_records.append(retained_first)
                    current_records.append(retained_current)
                    item["retained_verified_previous_score"] = True
        report["queue_status_counts"] = dict(Counter(item["status"] for item in report["queue"]))
        report["opportunity_status_counts"] = dict(Counter(item.get("opportunity_status", item["status"]) for item in report["queue"]))
        report["requested_horizon_records"] = len(report["queue"])
        report["identified_independent_matches"] = len({item["match_id"] for item in report["queue"] if "match_id" in item})
        report["scored_independent_matches"] = len({row["match_id"] for row in current_records})
        report["scored_horizon_records"] = len(current_records)
        report["retained_historical_first_score_count"] = len(report["retained_historical_first_score_refs"])
        report["first_scores_by_horizon"] = {h: _queue_summary([row for row in first_records if row["horizon"] == h],
            [item for item in report["queue"] if item.get("horizon") == h]) for h in HORIZONS}
        report["current_scores_by_horizon"] = {h: _queue_summary([row for row in current_records if row["horizon"] == h],
            [item for item in report["queue"] if item.get("horizon") == h]) for h in HORIZONS}
        if client is not None:
            report.update(api_requests=client.requests_made, api_collection_performed=client.requests_made > 0,
                api_daily_remaining=client.daily_remaining, api_daily_remaining_reported=client.daily_remaining_reported,
                api_quota_status=client.daily_quota_status)
        report["run_completed_at"] = _iso(_tick(clock, started))
        report["status"] = "partial" if report["issues"] else "complete" if report["queue"] else "no_queue_observations"
        report["report_ref"] = _reference(runtime, _immutable(root, "history", report))
        _pointer(root / "latest.json", report)
    return int(bool(report["issues"])), report


def score_run(runtime, league="serie_a", *, config, protocol, source_commit,
              clock=None, client_factory=None):
    """Return (exit_code, report), retaining every first-candidate queue member.

    ``clock``/``client_factory`` are internal tests hooks. The CLI cannot backdate
    time. Each fixture is fetched once per run across horizons. After success,
    actual later checks are bounded to every 24h during the first seven days;
    the first score stays unchanged and result revisions are separate records.
    """
    require_league(league)
    if not isinstance(league, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", league):
        raise SnapshotError("unsafe_scoring_league")
    if not isinstance(source_commit, str) or not re.fullmatch(r"[0-9a-fA-F]{40,64}", source_commit):
        raise SnapshotError("scoring_source_commit_full_sha_required")
    if not isinstance(config, dict) or not isinstance(protocol, dict):
        raise SnapshotError("scoring_config_and_protocol_required")
    competitions = [item for item in config.get("competitions", []) if item.get("key") == league]
    if len(competitions) != 1:
        raise SnapshotError("scoring_competition_config_not_unique")
    runtime = Path(runtime).resolve()
    if any((parent / "RESEARCH_REPLAY.json").exists() for parent in (runtime, runtime.parent)):
        raise SnapshotError("research_replay_cannot_be_future_scoring")
    clock = clock or (lambda: datetime.now(timezone.utc))
    if protocol.get("future_scoring_denominator") == "all_queue":
        return _score_queue_run(runtime, league, config=config, protocol=protocol,
            source_commit=source_commit, clock=clock, client_factory=client_factory)
    started = _utc(clock())
    protocol_hash = _digest(protocol)
    root = runtime / "feature_models" / "scoring" / protocol_hash
    root.mkdir(parents=True, exist_ok=True)
    report = {"schema_version": VERSION, "league": league, "source_commit": source_commit,
              "protocol_sha256": protocol_hash, "run_started_at": _iso(started),
              "confirmation_eligible": False, "confirmation_independent_matches": 0,
              "accuracy_improvement_verified": False, "betting_returns": None, "quotes_used": False,
              "api_requests": 0, "api_collection_performed": False, "api_daily_remaining": None,
              "api_quota_status": "not_observed", "queue": [], "issues": [],
              "result_recheck_policy": {"refresh_hours": 24, "window_days_after_kickoff": 7}}
    client, fetched, first_records, current_records = None, {}, [], []
    lock_root=runtime.parent/".feature-scoring-locks"
    lock_root.mkdir(parents=True,exist_ok=True)
    lock_name=hashlib.sha256((str(runtime)+protocol_hash).encode()).hexdigest()+".lock"
    with (lock_root/lock_name).open("a") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        anchors = sorted((runtime / "feature_snapshots" / protocol_hash / "captures" / league).glob("*/*/first_candidate.json"))
        for anchor in anchors:
            item = {"anchor_path": anchor.relative_to(runtime).as_posix(), "status": "invalid"}
            report["queue"].append(item)
            retained_first, retained_current = None, None
            try:
                now = _tick(clock, started)
                payload, receipt, anchor_ref = _capture(runtime, anchor, league, protocol_hash, now)
                fixture, horizon = payload["fixture"], payload["horizon"]
                item.update(match_id=fixture["match_id"], horizon=horizon)
                mature_at = _utc(fixture["kickoff"]) + RESULT_DELAY
                if now < mature_at:
                    item.update(status="pending", reason="kickoff_plus_four_hours_not_reached", mature_at=_iso(mature_at))
                    continue
                ledger = root / "scores" / league / str(fixture["match_id"]) / horizon
                first_path, latest_path = ledger / "first.json", ledger / "latest.json"
                previous = _read_ref(runtime, _read(latest_path)) if latest_path.exists() else None
                if previous is not None:
                    if previous.get("capture_anchor_ref") != anchor_ref:
                        raise SnapshotError("existing_score_capture_anchor_changed")
                    _read_ref(runtime, previous["result_ref"])
                    first = _read_ref(runtime, _read(first_path))
                    # Verify retained scores against their preserved raw results.
                    provider = _provider_league(runtime, payload, competitions[0])
                    for saved in (first, previous):
                        raw_result = _read_ref(runtime, saved["result_ref"])
                        checked, _ = _normalize_result(raw_result, fixture, provider, now)
                        if checked != saved["result"] or saved["scores"] != {
                                "M0": score_forecast(payload["baseline"].get("prediction", payload["baseline"]), checked),
                                "M1": score_forecast(payload["candidate"]["prediction"], checked)}:
                            raise SnapshotError("retained_score_replay_mismatch")
                    last_checked = _utc(previous["result_observed_at"])
                    observation_path = ledger / "last_checked.json"
                    if observation_path.exists():
                        observation = _read_ref(runtime, _read(observation_path))
                        observation_raw = _read_ref(runtime, observation["result_ref"])
                        if (observation.get("match_id") != fixture["match_id"] or observation.get("horizon") != horizon
                                or observation.get("observed_at") != observation_raw.get("fetched_at")
                                or _utc(observation["observed_at"]) > now):
                            raise SnapshotError("retained_result_check_identity_or_time_invalid")
                        last_checked = max(last_checked, _utc(observation["observed_at"]))
                    retained_first, retained_current = first, previous
                    if now - last_checked < RESULT_REFRESH or now > _utc(fixture["kickoff"]) + RESULT_RECHECK_WINDOW:
                        first_records.append(first)
                        current_records.append(previous)
                        item.update(status="already_scored", first_score_ref=_read(first_path), current_score_ref=_read(latest_path))
                        continue
                provider = _provider_league(runtime, payload, competitions[0])
                if fixture["match_id"] not in fetched:
                    if client is None:
                        try:
                            client = (client_factory or APIClient)(runtime / "cache",
                                max_requests=config.get("max_requests_per_run"),
                                daily_limit=config.get("api_daily_limit"),
                                max_run_seconds=config.get("api_max_run_seconds"))
                            client.status()
                        except (APIError, ValueError, OSError):
                            raise SnapshotError("result_api_initialization_or_status_failed") from None
                    envelope = client.fetch_fixture(fixture["match_id"], force=True, max_age_seconds=0)
                    observed_now = _tick(clock, now)
                    result, status = _normalize_result(envelope, fixture, provider, observed_now)
                    result_path = _immutable(root, "result_observations", envelope)
                    fetched[fixture["match_id"]] = (result, status, envelope, result_path)
                result, status, envelope, result_path = fetched[fixture["match_id"]]
                if result is None:
                    item.update(status="pending_result", provider_status=status,
                                result_ref=_reference(runtime, result_path))
                    continue
                # If a second horizon shares the fetch, recheck its identity too.
                checked, _ = _normalize_result(envelope, fixture, provider, _tick(clock, now))
                if checked != result:
                    raise SnapshotError("shared_fixture_result_mismatch")
                result_hash = _digest({key: result[key] for key in
                    ("match_id", "kickoff", "home_id", "away_id", "home_goals", "away_goals")})
                saved = {"schema_version": VERSION, "league": league, "match_id": fixture["match_id"],
                         "horizon": horizon, "protocol_sha256": protocol_hash,
                         "capture_anchor_ref": anchor_ref, "capture_receipt_ref": _read(anchor),
                         "capture_payload_ref": receipt["payload_ref"], "snapshot_id": payload["snapshot_id"],
                         "candidate_model_hash": payload["candidate"]["model_hash"],
                         "baseline_prediction_sha256": _digest(payload["baseline"].get("prediction", payload["baseline"])),
                         "candidate_prediction_sha256": _digest(payload["candidate"]["prediction"]),
                         "result": result, "provider_status": status, "result_semantic_sha256": result_hash,
                         "result_ref": _reference(runtime, result_path), "result_observed_at": envelope["fetched_at"],
                         "scored_at": _iso(_tick(clock, now)), "confirmation_eligible": False,
                         "scores": {"M0": score_forecast(payload["baseline"].get("prediction", payload["baseline"]), result),
                                    "M1": score_forecast(payload["candidate"]["prediction"], result)},
                         "target": "regulation_time_90_plus_stoppage", "betting_returns": None}
                if previous is None or previous["result_semantic_sha256"] != result_hash:
                    if previous is not None:
                        saved["revision_of"] = _read(latest_path)
                    score_path = _immutable(root, "score_versions", saved)
                    score_ref = _reference(runtime, score_path)
                    _pointer(latest_path, score_ref)
                    if previous is None:
                        _pointer(first_path, score_ref, first=True)
                    item.update(status="scored" if previous is None else "result_revision_appended")
                else:
                    saved = previous
                    score_ref = _read(latest_path)
                    item["status"] = "result_unchanged"
                observation = {"match_id": fixture["match_id"], "horizon": horizon,
                               "observed_at": envelope["fetched_at"], "result_ref": _reference(runtime, result_path)}
                observation_file = _immutable(root, "result_check_receipts", observation)
                _pointer(ledger / "last_checked.json", _reference(runtime, observation_file))
                first_records.append(_read_ref(runtime, _read(first_path)))
                current_records.append(saved)
                item.update(first_score_ref=_read(first_path), current_score_ref=score_ref)
            except (ValueError, APIError, KeyError, TypeError, AttributeError, OSError) as exc:
                item.update(status="failed", reason=_reason(exc))
                report["issues"].append({"anchor_path": item["anchor_path"], "reason": _reason(exc)})
                # Failed later refreshes never erase an independently replayed
                # first score or shrink its cumulative scored denominator.
                if retained_first is not None:
                    first_records.append(retained_first)
                    current_records.append(retained_current)
                    item["retained_verified_previous_score"] = True
        report["queue_status_counts"] = dict(Counter(item["status"] for item in report["queue"]))
        report["requested_horizon_records"] = len(anchors)
        report["identified_independent_matches"] = len({item["match_id"] for item in report["queue"] if "match_id" in item})
        report["scored_independent_matches"] = len({row["match_id"] for row in current_records})
        report["scored_horizon_records"] = len(current_records)
        report["first_scores_by_horizon"] = {h: _summary([row for row in first_records if row["horizon"] == h]) for h in HORIZONS}
        report["current_scores_by_horizon"] = {h: _summary([row for row in current_records if row["horizon"] == h]) for h in HORIZONS}
        if client is not None:
            report.update(api_requests=client.requests_made, api_collection_performed=client.requests_made > 0,
                          api_daily_remaining=client.daily_remaining,
                          api_daily_remaining_reported=client.daily_remaining_reported,
                          api_quota_status=client.daily_quota_status)
        report["run_completed_at"] = _iso(_tick(clock, started))
        report["status"] = "partial" if report["issues"] else "complete" if anchors else "no_candidate_captures"
        path = _immutable(root, "history", report)
        report["report_ref"] = _reference(runtime, path)
        _pointer(root / "latest.json", report)
    return int(bool(report["issues"])), report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("score")
    run.add_argument("--runtime", default="runtime")
    run.add_argument("--config", default="config.json")
    run.add_argument("--league", default="serie_a")
    run.add_argument("--protocol", default="feature-models.json")
    run.add_argument("--source-commit", required=True)
    args = parser.parse_args(argv)
    code, report = score_run(args.runtime, args.league, config=_read(args.config),
                             protocol=_read(args.protocol), source_commit=args.source_commit)
    print(json.dumps({key: report[key] for key in ("status", "requested_horizon_records",
        "scored_independent_matches", "scored_horizon_records", "queue_status_counts", "api_requests",
        "confirmation_eligible", "report_ref")}, ensure_ascii=False))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
