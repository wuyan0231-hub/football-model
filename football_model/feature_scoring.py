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

from .data import APIClient, APIError, DataError, _iso, _utc, normalize_fixtures
from .feature_research import _mean_metrics, score_forecast
from .feature_snapshots import HORIZONS, SnapshotError, _digest, _read, verify_capture

VERSION = "feature-future-scoring-v1"
RESULT_DELAY = timedelta(hours=4)
RESULT_REFRESH = timedelta(hours=24)
RESULT_RECHECK_WINDOW = timedelta(days=7)


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


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
    if not path.is_file() or _sha(path) != ref["sha256"]:
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


def score_run(runtime, league="serie_a", *, config, protocol, source_commit,
              clock=None, client_factory=None):
    """Return (exit_code, report), retaining every first-candidate queue member.

    ``clock``/``client_factory`` are internal tests hooks. The CLI cannot backdate
    time. Each fixture is fetched once per run across horizons. After success,
    actual later checks are bounded to every 24h during the first seven days;
    the first score stays unchanged and result revisions are separate records.
    """
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
