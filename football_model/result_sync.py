"""Independently observe and append 90-minute results for frozen forecasts.

This path never trains, changes model/data files, or creates forecasts. Provider
responses and every result revision are retained under the prospective result
ledger. Valid live shadow targets share result collection, never production
forecast membership.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
import json
import math
import os
from pathlib import Path

from .competition_scope import is_retired, require_competition
from .cli import atomic_json, safe_error
from .data import APIClient, APIError, RequestBudgetError, normalize_fixtures
from .model import RESULT_AVAILABILITY_DELAY, _timestamp, _iso, _integer
from .prospective import (_append, _digest, _identity, _now, _objects, _path, _read,
                          _same_identity, append_result, ledger_writer, summarize, record_schedule_revision, _event_order)

SCHEMA = "independent_regulation_result_sync_v1"


def _provider_league(root: Path, records: list[dict], config: dict) -> int:
    ids = set()
    for forecast in records:
        origin = forecast.get("_result_sync_ledger", "prospective")
        source_root = root if origin == "prospective" else _path(root.parent, "shadow_models")
        source_id = forecast["report_source"] if origin == "prospective" else forecast["parents"]["report_source"]
        source, _ = _read(source_root, "sources", source_id + ".json")
        if _digest(source) != source_id:
            raise ValueError("report_source_integrity_mismatch")
        if origin == "shadow_models":
            ids.add(_integer(forecast["parents"]["provider_league_id"], "shadow.provider_league_id", 1))
        for item in source.get("content", {}).get("competitions", []):
            if item.get("key") == forecast["competition"] and item.get("provider_league_id") is not None:
                ids.add(_integer(item["provider_league_id"], "provider_league_id", 1))
    # An explicit configured provider ID is authoritative; names are not IDs.
    for item in config.get("competitions", []):
        if item.get("key") == records[0]["competition"] and item.get("provider_id") is not None:
            ids.add(_integer(item["provider_id"], "provider_id", 1))
    if len(ids) != 1:
        raise ValueError("provider_league_identity_missing_or_conflicting")
    provider_id = next(iter(ids))
    require_competition({"key": records[0]["competition"]}, provider_id)
    return provider_id


def _shadow_targets(runtime: Path, now: datetime) -> tuple[list, list, list]:
    """Read live shadow receipts through their native integrity/recompute gate.

    Failures are retained in this sync report and never become target identities.
    Nothing from this reader is appended to the production forecast collection.
    """
    directory = _path(runtime, "shadow_models", "forecasts")
    if not directory.exists():
        return [], [], []
    from .shadow_models import _saved_forecast_valid, _writer
    valid, diagnostics, errors = [], [], []
    try:
        # Stabilize append-only receipts while capture/score writers may run.
        with _writer(runtime) as shadow_root:
            for path in sorted(directory.glob("*.json")):
                identity = {"forecast_id": path.stem, "target_ledger": "shadow_models"}
                try:
                    value, _ = _read(shadow_root, "forecasts", path.name)
                    if not isinstance(value, dict) or path.stem != _digest(value):
                        raise ValueError("shadow_forecast_object_hash_mismatch")
                    forecast = {**value, "object_id": path.stem}
                    _identity(forecast)
                    identity.update(competition=forecast.get("competition"), match_id=forecast["match_id"])
                    _saved_forecast_valid(shadow_root, forecast, now)
                    valid.append({**forecast, "_result_sync_ledger": "shadow_models"})
                except (ValueError, KeyError, TypeError, AttributeError, OSError) as exc:
                    diagnostics.append({**identity, "status": "shadow_target_rejected", "reason": safe_error(exc)})
                    errors.append("shadow_target:" + path.stem + ":" + safe_error(exc))
    except (ValueError, KeyError, TypeError, RuntimeError, OSError) as exc:
        diagnostics.append({"target_ledger": "shadow_models", "status": "shadow_targets_unavailable", "reason": safe_error(exc)})
        errors.append("shadow_targets:" + safe_error(exc))
    return valid, diagnostics, errors


def _targets(root: Path, forecasts: list[dict], now: datetime, recheck_days: int) -> tuple[list, list]:
    groups = defaultdict(list)
    for row in forecasts:
        if is_retired(row.get("competition")):
            continue
        if row.get("forecast", {}).get("research_replay"):
            raise ValueError("research_forecast_in_prospective_ledger")
        groups[(row["competition"], row["match_id"])].append(row)
    results = defaultdict(list)
    for row in _objects(root, "results"):
        results[(row["competition"], row["match_id"])].append(row)
    revisions = defaultdict(list)
    for row in _objects(root, "schedule_revisions"):
        revisions[(row["competition"], row["match_id"])].append(row)
    due, skipped = [], []
    for key, rows in sorted(groups.items()):
        # Any mature registered identity makes the match eligible for inspection;
        # the returned real identity still has to match one frozen version.
        mature = [r for r in rows if _timestamp(r["kickoff"], "kickoff") + RESULT_AVAILABILITY_DELAY <= now]
        if not mature:
            skipped.append({"competition": key[0], "match_id": key[1], "status": "not_due"})
            continue
        latest = max(results[key], key=lambda r: (_timestamp(r["observed_at"], "observed_at"),
            _timestamp(r["captured_at"], "captured_at"), r["object_id"]), default=None)
        revision = max(revisions[key], key=lambda r: (_timestamp(r["observed_at"], "observed_at"),
            _timestamp(r["captured_at"], "captured_at"), r["object_id"]), default=None)
        if (revision and (latest is None or _event_order(revision) > _event_order(latest))
                and not any(_same_identity(revision, r) for r in rows)
                and all(_timestamp(r["kickoff"], "kickoff") + timedelta(days=recheck_days) < now for r in rows + [revision])):
            skipped.append({"competition": key[0], "match_id": key[1], "status": "unscorable_schedule_revision",
                            "schedule_revision_id": revision["object_id"], "reason": "outside_revision_recheck_window"})
            continue
        pending_independent_shadow = any(r.get("_result_sync_ledger") == "shadow_models" for r in rows) and (
            latest is None or latest.get("collection_method") != "independent_fixture_sync")
        if latest and not pending_independent_shadow and all(_timestamp(r["kickoff"], "kickoff") + timedelta(days=recheck_days) < now for r in mature):
            skipped.append({"competition": key[0], "match_id": key[1], "status": "outside_revision_recheck_window"})
            continue
        due.append((key, rows))
    return due, skipped


def run(runtime: Path, config: dict, *, client=None, clock=None,
        revision_recheck_days: int = 7, cache_max_age_seconds: int = 600,
        max_run_seconds: int = 300) -> tuple[int, dict]:
    """Fetch due fixtures even if this run's model/report never succeeded.

    The revision window only bounds routine rechecks of already settled games.
    Pending games have no age expiry; pass a larger window for a deliberate
    correction audit. Cached observations retain their original fetched_at.
    """
    for label, value in (("revision_recheck_days", revision_recheck_days),
                         ("cache_max_age_seconds", cache_max_age_seconds), ("max_run_seconds", max_run_seconds)):
        if type(value) is not int or value < (1 if label == "max_run_seconds" else 0):
            raise ValueError(label + " must be a nonnegative integer (runtime strictly positive)")
    configured_deadline = config.get("api_max_run_seconds")
    if configured_deadline is not None:
        if type(configured_deadline) not in (int, float) or not math.isfinite(configured_deadline) or configured_deadline <= 0:
            raise ValueError("api_max_run_seconds must be finite and strictly positive")
        max_run_seconds = min(max_run_seconds, configured_deadline)
    runtime = Path(runtime)
    clock = clock or (lambda: datetime.now(timezone.utc))
    started = _now(clock)
    with ledger_writer(runtime) as root:
        forecasts = [{**row, "_result_sync_ledger": "prospective"} for row in _objects(root, "forecasts")]
        shadows, shadow_diagnostics, errors = _shadow_targets(runtime, started)
        targets, records = _targets(root, forecasts + shadows, started, revision_recheck_days)
        records.extend(shadow_diagnostics)
        report = {"schema": SCHEMA, "started_at": _iso(started), "run_started_at": _iso(started),
            "source_commit": os.environ.get("GITHUB_SHA"), "time_semantics": "observed_wall_clock_v1",
            "forecast_versions": len(forecasts), "target_matches": len(targets),
            "production_forecast_versions": len(forecasts), "verified_shadow_forecast_versions": len(shadows),
            "target_forecast_versions": len(forecasts) + len(shadows),
            "shadow_forecasts_added_to_production": False,
            "revision_recheck_days": revision_recheck_days, "cache_max_age_seconds": cache_max_age_seconds,
            "max_run_seconds": max_run_seconds, "records": records,
            "model_or_training_required": False, "changes_training_data": False}
        if targets:
            try:
                client = client or APIClient(runtime / "cache", max_requests=config.get("max_requests_per_run"),
                    daily_limit=config.get("api_daily_limit"), max_run_seconds=max_run_seconds)
                client.status()
            except (APIError, ValueError, RuntimeError, OSError) as exc:
                errors.append(safe_error(exc))
                records.extend({"competition": key[0], "match_id": key[1], "status": "client_unavailable"}
                               for key, _ in targets)
                targets = []
        stopped = False
        fixture_responses = {}
        for (competition, mid), related in targets:
            entry = {"competition": competition, "match_id": mid,
                     "target_ledgers": sorted({f["_result_sync_ledger"] for f in related})}
            if stopped:
                entry["status"] = "budget_deferred"
                records.append(entry)
                continue
            try:
                provider_id = _provider_league(root, related, config)
                if mid not in fixture_responses:
                    try:
                        fixture_responses[mid] = client.fetch_fixture(mid, force=False, max_age_seconds=cache_max_age_seconds)
                    except (APIError, ValueError, RuntimeError, OSError) as exc:
                        fixture_responses[mid] = exc
                envelope = fixture_responses[mid]
                if isinstance(envelope, Exception):
                    raise envelope
                received = _now(clock)
                # Preserve the complete sanitized APIClient response before semantic
                # rejection, including empty/pending/wrong-identity responses.
                source_id = _append(root, "sources", {"kind": "independent_fixture_result_response",
                    "content": envelope, "request": {"fixture_id": mid},
                    "client_response_sha256": _digest(envelope)})
                entry.update(source_id=source_id, received_at=_iso(received),
                             from_cache=envelope.get("from_cache"))
                observed = _timestamp(envelope.get("fetched_at"), "response.fetched_at")
                if observed > received:
                    raise ValueError("result_response_observation_in_future")
                if envelope.get("errors"):
                    raise ValueError("result_response_has_errors")
                paging = envelope.get("paging", {})
                if not isinstance(paging, dict) or any(
                        type(paging.get(field, 1)) is not int or paging.get(field, 1) != 1
                        for field in ("current", "total")):
                    raise ValueError("result_response_incomplete_pagination")
                if envelope.get("endpoint", "/fixtures") != "/fixtures" or envelope.get("params", {"id": mid}) != {"id": mid}:
                    raise ValueError("result_response_request_mismatch")
                items = envelope.get("response")
                if not isinstance(items, list) or len(items) != 1:
                    raise ValueError("result_response_not_exactly_one_fixture")
                if "results" in envelope and (type(envelope["results"]) is not int or envelope["results"] != len(items)):
                    raise ValueError("result_response_count_mismatch")
                item = items[0]
                if _integer(item.get("fixture", {}).get("id"), "result.fixture.id", 1) != mid:
                    raise ValueError("result_fixture_id_mismatch")
                if _integer(item.get("league", {}).get("id"), "result.league.id", 1) != provider_id:
                    raise ValueError("result_provider_league_mismatch")
                # Validate identity even for NS/abandoned responses. A postponed
                # kickoff is recorded as a conflict, not silently substituted.
                identity = {"match_id": mid, "kickoff": item["fixture"]["date"],
                    "home_id": item["teams"]["home"]["id"], "away_id": item["teams"]["away"]["id"]}
                _identity(identity)
                rows = normalize_fixtures(items, observed)
                entry.update(observed_at=_iso(observed), provider_status=item["fixture"]["status"]["short"])
                if not any(_same_identity(identity, forecast) for forecast in related):
                    revision_id = record_schedule_revision(root, competition, identity,
                        observed=observed, captured=received, source_id=source_id, forecasts=related,
                        collection_method="independent_fixture_sync")
                    entry.update(status="unscorable_schedule_revision", schedule_revision_id=revision_id,
                                 reason="source_kickoff_changed_from_all_frozen_forecasts", result_id=None)
                elif not rows:
                    entry["status"] = "pending_or_not_mature_at_observation"
                else:
                    result_id = append_result(root, competition, rows[0], observed=observed, captured=received,
                        source_id=source_id, forecasts=related, collection_method="independent_fixture_sync")
                    entry.update(status="result_appended" if result_id else "result_unchanged", result_id=result_id)
            except RequestBudgetError as exc:
                stopped = True
                entry.update(status="budget_deferred", reason=safe_error(exc))
                errors.append(competition + ":" + str(mid) + ":" + safe_error(exc))
            except (APIError, ValueError, RuntimeError, KeyError, TypeError, AttributeError, OSError) as exc:
                entry.update(status="failed", reason=safe_error(exc))
                errors.append(competition + ":" + str(mid) + ":" + safe_error(exc))
            records.append(entry)
        report.update(completed_at=_iso(_now(clock)), status_counts=dict(Counter(r["status"] for r in records)),
            api_requests=getattr(client, "requests_made", 0), errors=errors, exit_code=int(bool(errors)))
        report["collection_completed_at"] = report["completed_at"]
        report["counts"] = report["status_counts"]
        budget = getattr(client, "budget_snapshot", None)
        if callable(budget):
            report["api_budget"] = budget()
        scoring = summarize(root, clock)
        scoring.update(scoring_origin="independent_result_sync", errors=errors,
                       exit_code=report["exit_code"])
        report["score_summary_id"] = _append(root, "result_sync_scores", scoring)
        generated = _now(clock)
        if generated < _timestamp(scoring["generated_at"], "score.generated_at"):
            raise ValueError("result_sync_report_clock_regressed_after_scoring")
        report["generated_at"] = report["completed_at"] = _iso(generated)
        report_id = _append(root, "result_sync_runs", report)
        report["report_id"] = report_id
        atomic_json(_path(runtime, "result_sync", "reports", "latest.json"), report)
        return report["exit_code"], report


def main(argv=None):
    parser = argparse.ArgumentParser(description="Independently synchronize frozen forecasts' regulation-time results")
    parser.add_argument("--runtime", type=Path, default=Path("runtime"))
    parser.add_argument("--config", type=Path, default=Path("config.json"))
    parser.add_argument("--revision-recheck-days", type=int, default=7)
    args = parser.parse_args(argv)
    try:
        code, report = run(args.runtime, json.loads(args.config.read_text()), revision_recheck_days=args.revision_recheck_days)
        print(json.dumps({k: report[k] for k in ("report_id", "target_matches", "status_counts", "api_requests", "exit_code")}, ensure_ascii=False))
        return code
    except (ValueError, KeyError, TypeError, OSError, RuntimeError) as exc:
        print(json.dumps({"status": "failed", "exit_code": 2, "reason": safe_error(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
