"""Collect club fixtures across provider competitions for schedule research.

The original league data and API client are unchanged. All returned fixtures are
validated, friendly competitions are excluded, and 90-minute scores use the
existing normalizer. Historical downloads are observed now, not reconstructed
pre-match snapshots. Completion here means a complete set of successful provider
queries, not a independently certified census of every real-world match.
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
from pathlib import Path
import re
from contextlib import nullcontext

from .competition_scope import require_league, require_competition, select_competitions
from .data import (APIClient, APIError, DataError, RequestBudgetError, RequestDeadlineError,
                   _fixture_identity, _integer, _iso, _utc, normalize_fixtures)


VERSION = "club-schedule-collection-1.0"
STATUSES = {"TBD", "NS", "1H", "HT", "2H", "ET", "BT", "P", "SUSP", "INT",
            "FT", "AET", "PEN", "PST", "CANC", "ABD", "AWD", "WO", "LIVE"}
FINISHED = {"FT", "AET", "PEN"}
FRIENDLY = re.compile(r"\bfriendl(?:y|ies)\b|友谊", re.IGNORECASE)
SAFE_DIAGNOSTIC_CODES = {
    "fixture_envelope_endpoint_mismatch", "fixture_envelope_params_or_errors_invalid",
    "fixture_response_not_list", "fixture_pagination_not_complete", "fixture_observation_in_future",
    "fixture_structure_invalid", "fixture_league_missing", "fixture_competition_or_season_mismatch",
    "fixture_league_name_missing", "fixture_does_not_involve_requested_club", "fixture_status_invalid",
    "fixture_competition_type_invalid", "competition_metadata_envelope_invalid",
    "competition_metadata_observation_in_future", "competition_metadata_pagination_invalid",
    "competition_metadata_not_unique", "competition_metadata_type_invalid",
    "competition_metadata_name_missing", "competition_metadata_season_missing_or_duplicated",
    "league_fixture_response_empty_unverified", "league_match_maps_to_multiple_seasons",
    "collection_clock_moved_backwards", "schedule_evidence_hash_conflict",
}


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2,
                      allow_nan=False) + "\n"


def _read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _root(runtime, league):
    require_league(league)
    if not isinstance(league, str) or not re.fullmatch(r"[a-z][a-z0-9_]*", league):
        raise ValueError("invalid_league_key")
    return Path(runtime) / "feature_models" / "schedules" / league


def load_schedule(runtime, league):
    """Read the latest collector output, including explicit coverage limitations.

    ``matches`` contain validated regulation-score rows. ``coverage`` also has
    ``workload_events``: finished formal fixtures usable for rest/density only,
    even when regulation scores could not be established. No score is filled.
    """
    require_league(league)
    payload = _read(_root(runtime, league) / "latest.json")
    if payload.get("version") != VERSION or payload.get("league") != league:
        raise DataError("schedule_output_identity_mismatch")
    matches, coverage = payload.get("schedule_matches"), payload.get("coverage")
    if not isinstance(matches, list) or not isinstance(coverage, dict):
        raise DataError("schedule_output_structure_invalid")
    return matches, coverage


def _save_evidence(root, envelope, runtime, guard=None):
    # APIClient has already redacted unexpected credential echoes. Never add
    # account status responses, exception text, or authentication fields here.
    content = _canonical(envelope).encode("utf-8")
    digest = hashlib.sha256(content).hexdigest()
    path = root / "responses" / (digest + ".json")
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != content:
            raise DataError("schedule_evidence_hash_conflict")
    else:
        ticket = guard.check_write(path, content) if guard is not None else None
        path.write_bytes(content)
        if ticket is not None:
            guard.commit_write(ticket)
    return {"path": str(path.relative_to(runtime)), "sha256": digest,
            "fetched_at": envelope["fetched_at"],
            "from_cache": envelope.get("from_cache", False)}


def _envelope(envelope, params, now):
    if not isinstance(envelope, dict) or envelope.get("endpoint") != "/fixtures":
        raise DataError("fixture_envelope_endpoint_mismatch")
    if envelope.get("params") != params or envelope.get("errors"):
        raise DataError("fixture_envelope_params_or_errors_invalid")
    if not isinstance(envelope.get("response"), list):
        raise DataError("fixture_response_not_list")
    paging = envelope.get("paging", {})
    if not isinstance(paging, dict) or paging.get("total", 1) != 1 or paging.get("current", 1) != 1:
        raise DataError("fixture_pagination_not_complete")
    if _utc(envelope["fetched_at"]) > now:
        raise DataError("fixture_observation_in_future")


def _identity(raw, season, team=None, league_id=None):
    if not isinstance(raw, dict) or not isinstance(raw.get("fixture"), dict):
        raise DataError("fixture_structure_invalid")
    _integer(raw["fixture"].get("id"), "fixture.id", 1)
    row, _ = _fixture_identity(raw)
    comp = raw.get("league")
    if not isinstance(comp, dict):
        raise DataError("fixture_league_missing")
    lid = _integer(comp.get("id"), "league.id", 1)
    if comp.get("season") != season or (league_id is not None and lid != league_id):
        raise DataError("fixture_competition_or_season_mismatch")
    name = comp.get("name")
    if not isinstance(name, str) or not name.strip():
        raise DataError("fixture_league_name_missing")
    if team is not None and team not in (row["home_id"], row["away_id"]):
        raise DataError("fixture_does_not_involve_requested_club")
    status = raw["fixture"].get("status")
    if not isinstance(status, dict) or status.get("short") not in STATUSES:
        raise DataError("fixture_status_invalid")
    # Fixtures often omit league.type. If supplied, it must still be valid;
    # definitive competition type is obtained from /leagues metadata below.
    if "type" in comp and comp["type"] not in ("League", "Cup"):
        raise DataError("fixture_competition_type_invalid")
    row.update(league_id=lid, season=season, status=status["short"])
    return row


def _signature(raw):
    return {"fixture": {k: raw["fixture"].get(k) for k in ("date", "timestamp", "status")},
            "teams": raw["teams"], "score": raw.get("score"),
            "league": {k: raw["league"].get(k) for k in ("id", "season")}}


def _diagnostic_code(exc):
    """Retain module-defined validation codes, never arbitrary exception text."""
    value = str(exc)
    if isinstance(exc, RequestBudgetError) and getattr(exc, "reason", None) == "storage_capacity_deferred":
        return "storage_capacity_deferred"
    if isinstance(exc, RequestDeadlineError):
        return "runtime_deferred"
    if type(exc) is DataError and value in SAFE_DIAGNOSTIC_CODES:
        return value
    return "budget_deferred" if isinstance(exc, RequestBudgetError) else "provider_error" if isinstance(exc, APIError) else "validation_or_storage_error"


def _metadata(client, league_id, season, clock, root, runtime, guard=None):
    require_competition({}, league_id)
    params = {"id": league_id, "season": season}
    envelope = client._cached_request("/leagues", params, force=False, max_age_seconds=7 * 86400)
    # The provider's fetched_at is stamped after request completion. Compare to
    # a real post-request clock, not the caller's pre-request time. Saving the
    # sanitized original before validation also preserves rejected evidence.
    evidence = _save_evidence(root, envelope, runtime, guard)
    observed = _utc(clock())
    try:
        return _validate_metadata(envelope, league_id, season, params, observed, evidence)
    except (APIError, ValueError, KeyError, TypeError, OSError) as exc:
        exc.schedule_evidence = evidence
        raise


def _validate_metadata(envelope, league_id, season, params, observed, evidence):
    if (envelope.get("endpoint") != "/leagues" or envelope.get("params") != params
            or envelope.get("errors") or not isinstance(envelope.get("response"), list)):
        raise DataError("competition_metadata_envelope_invalid")
    if _utc(envelope["fetched_at"]) > observed:
        raise DataError("competition_metadata_observation_in_future")
    paging = envelope.get("paging", {})
    if not isinstance(paging, dict) or paging.get("total", 1) != 1 or paging.get("current", 1) != 1:
        raise DataError("competition_metadata_pagination_invalid")
    items = [x for x in envelope["response"] if isinstance(x, dict)
             and isinstance(x.get("league"), dict) and x["league"].get("id") == league_id]
    if len(items) != 1:
        raise DataError("competition_metadata_not_unique")
    item = items[0]
    comp = item["league"]
    if comp.get("type") not in ("League", "Cup"):
        raise DataError("competition_metadata_type_invalid")
    if not isinstance(comp.get("name"), str) or not comp["name"].strip():
        raise DataError("competition_metadata_name_missing")
    years = item.get("seasons")
    if not isinstance(years, list) or sum(isinstance(x, dict) and x.get("year") == season for x in years) != 1:
        raise DataError("competition_metadata_season_missing_or_duplicated")
    return {"league_id": league_id, "season": season, "name": comp["name"],
            "type": comp["type"], "friendly": bool(FRIENDLY.search(comp["name"])),
            "evidence": evidence}


def collect_schedule(config, runtime="runtime", league="serie_a", *, source_commit=None,
                     client=None, clock=None, reserve_requests=20, max_run_seconds=None,
                     current_max_age_seconds=None, pending_first=False,
                     check_account=True, capacity_guard=None):
    """Persist a finite club×season collection and all failures; no model fitting."""
    require_league(league)
    runtime = Path(runtime)
    requests_before = getattr(client, "requests_made", 0)
    root = _root(runtime, league)
    clock = clock or (lambda: datetime.now(timezone.utc))
    started = _utc(clock())
    _integer(reserve_requests, "reserve_requests")
    effective_seconds = config.get("api_max_run_seconds") or 3600
    if max_run_seconds is not None:
        effective_seconds = min(effective_seconds, max_run_seconds)
    if (type(effective_seconds) not in (int, float) or not math.isfinite(effective_seconds)
            or effective_seconds <= 0):
        raise ValueError("invalid_run_time_budget")
    if source_commit is not None and not re.fullmatch(r"[a-f0-9]{40}", source_commit):
        raise ValueError("invalid_source_commit")
    if current_max_age_seconds is not None and (
            type(current_max_age_seconds) not in (int, float)
            or not math.isfinite(current_max_age_seconds) or current_max_age_seconds <= 0):
        raise ValueError("invalid_current_cache_age")
    data_path = runtime / "data" / (league + ".json")
    data = _read(data_path)
    targets, seasons = data.get("matches"), data.get("requested_seasons")
    if not isinstance(targets, list) or not targets or not isinstance(seasons, list) or not seasons:
        raise DataError("saved_league_matches_or_requested_seasons_missing")
    for year in seasons:
        _integer(year, "season", 1900)
    if len(set(seasons)) != len(seasons):
        raise DataError("requested_seasons_duplicated")
    seasons = sorted(seasons, reverse=True)
    teams = sorted({_integer(row[side], side, 1) for row in targets for side in ("home_id", "away_id")})
    target_ids = [_integer(row.get("match_id"), "match_id", 1) for row in targets]
    if len(target_ids) != len(set(target_ids)):
        raise DataError("saved_league_match_ids_duplicated")
    targets_by_id = {row["match_id"]: row for row in targets}
    competitions = [x for x in config.get("competitions", []) if x.get("key") == league]
    if len(competitions) != 1 or competitions[0].get("mode") != "domestic_baseline":
        raise DataError("collector_requires_one_domestic_competition")
    data_hash = hashlib.sha256(data_path.read_bytes()).hexdigest()
    run_id = started.strftime("%Y%m%dT%H%M%S%fZ") + "_" + data_hash[:10]
    coverage = {"scope": "all_club_competitions_provider_scope", "requested_seasons": seasons,
        "selected_club_ids": teams, "club_selection_scope": "union_of_clubs_in_saved_normalized_league_matches",
        "expected_team_season_requests": len(teams) * len(seasons), "team_season_requests": [],
        "league_season_audit": [], "normalized_target_match_count": len(targets),
        "target_match_season_mapping": [], "issues": [], "conflict_match_ids": [],
        "target_match_identity_or_score_mismatches": [],
        "normalized_target_match_ids_missing_from_club_responses": [],
        "competition_metadata_failures": [],
        "all_club_competitions_complete": False, "independent_fixture_census_verified": False,
        "historical_availability_verified": False, "national_team_player_workload_included": False,
        "friendly_competitions_excluded": True, "workload_events": []}
    report = {"version": VERSION, "league": league, "run_id": run_id,
        "source_commit": source_commit, "run_started_at": _iso(started),
        "source_data": {"path": str(data_path.relative_to(runtime)), "sha256": data_hash,
                        "observed_at": data.get("observed_at"), "missing_seasons": data.get("missing_seasons", [])},
        "time_semantics": "historical_provider_records_observed_at_actual_collection_time_not_historical_PIT",
        "schedule_matches": [], "schedule_events": [], "coverage": coverage,
        "reserve_requests": reserve_requests, "max_run_seconds": effective_seconds}
    if current_max_age_seconds is not None:
        report["current_cache_policy"] = {"maximum_age_seconds": current_max_age_seconds,
            "force_refresh": False, "original_fetched_at_preserved": True}
    request_order = [(team, year) for team in teams for year in seasons]
    if pending_first and (root / "latest.json").is_file():
        previous = _read(root / "latest.json")
        if previous.get("version") != VERSION or previous.get("league") != league:
            raise DataError("schedule_output_identity_mismatch")
        unfinished = {(x.get("team_id"), x.get("season"))
            for x in previous.get("coverage", {}).get("team_season_requests", [])
            if x.get("status") != "collected"}
        request_order.sort(key=lambda item: item not in unfinished)
    report["request_order_policy"] = "previous_unfinished_first" if pending_first else "team_id_then_season"
    records, conflicts, metadata = {}, set(), {}
    season_join = {}
    stage = "client_initialization"

    def now():
        observed = _utc(clock())
        if observed < started:
            raise DataError("collection_clock_moved_backwards")
        return observed

    def failed(item, exc, failed_stage):
        item.update(status="deferred" if isinstance(exc, RequestBudgetError) else "failed",
            reason=_diagnostic_code(exc) if isinstance(exc, RequestBudgetError) else
                   "provider_error" if isinstance(exc, APIError) else "validation_or_storage_error",
            failure_stage=failed_stage, diagnostic_code=_diagnostic_code(exc), checked_at=_iso(now()))

    def competition_type(lid, year):
        key = (lid, year)
        if key not in metadata:
            try:
                metadata[key] = _metadata(client, lid, year, now, root, runtime, capacity_guard)
            except (APIError, ValueError, KeyError, TypeError, OSError) as exc:
                # Repeating failed metadata queries for every club does not add
                # evidence and needlessly consumes the same quota.
                metadata[key] = exc
                coverage["competition_metadata_failures"].append({"league_id": lid, "season": year,
                    "diagnostic_code": _diagnostic_code(exc), "checked_at": _iso(now()),
                    "evidence": getattr(exc, "schedule_evidence", None)})
        if isinstance(metadata[key], Exception):
            raise metadata[key]
        return metadata[key]

    try:
        client = client or APIClient(runtime / "cache", max_requests=config.get("max_requests_per_run"),
            daily_limit=config.get("api_daily_limit"), max_run_seconds=effective_seconds, clock=clock)
        stage = "account_budget_check"
        if check_account:
            client.status()  # observe real already-spent daily quota; never reset it
        with client.reserve_requests(reserve_requests):
            stage = "league_metadata"
            league_meta = client.resolve_league(competitions[0], force=False)
            lid = _integer(league_meta["league"].get("id"), "league.id", 1)
            report["provider_league_id"] = lid
            for year in seasons:
                audit = {"season": year, "status": "pending"}
                coverage["league_season_audit"].append(audit)
                audit_stage = "league_fixture_fetch"
                try:
                    env = client.fetch_fixtures(lid, year, current=(year == seasons[0]),
                        force=(year == seasons[0] and current_max_age_seconds is None),
                        max_age_seconds=(current_max_age_seconds if year == seasons[0]
                                         and current_max_age_seconds is not None else 7 * 86400))
                    _envelope(env, {"league": lid, "season": year}, now())
                    audit["evidence"] = _save_evidence(root, env, runtime, capacity_guard)
                    audit_stage = "league_fixture_validate"
                    if not env["response"]:
                        raise DataError("league_fixture_response_empty_unverified")
                    for raw in env["response"]:
                        row = _identity(raw, year, league_id=lid)
                        mid = row["match_id"]
                        if mid in season_join and season_join[mid] != year:
                            raise DataError("league_match_maps_to_multiple_seasons")
                        season_join[mid] = year
                        if mid in targets_by_id:
                            target = targets_by_id[mid]
                            old_identity = {key: target[key] for key in ("home_id", "away_id")}
                            old_identity["kickoff"] = _iso(_utc(target["kickoff"]))
                            current_identity = {key: row[key] for key in old_identity}
                            scores = normalize_fixtures([raw], now())
                            score_same = (len(scores) == 1 and all(scores[0][key] == target[key]
                                          for key in ("home_goals", "away_goals")))
                            if current_identity != old_identity or not score_same:
                                coverage["target_match_identity_or_score_mismatches"].append(mid)
                    audit.update(status="collected", returned_fixtures=len(env["response"]),
                        clubs=sorted({_integer(x["teams"][side]["id"], "team.id", 1)
                                      for x in env["response"] for side in ("home", "away")}))
                except (APIError, ValueError, KeyError, TypeError, OSError) as exc:
                    failed(audit, exc, audit_stage)

            for team, year in request_order:
                    entry = {"team_id": team, "season": year, "status": "pending",
                             "accepted_fixtures": 0, "excluded_friendlies": [], "invalid_fixtures": []}
                    coverage["team_season_requests"].append(entry)
                    request_stage = "club_fixture_fetch"
                    try:
                        params = {"team": team, "season": year}
                        env = client._cached_request("/fixtures", params,
                            force=(year == seasons[0] and current_max_age_seconds is None),
                            max_age_seconds=(current_max_age_seconds if year == seasons[0]
                                             and current_max_age_seconds is not None else 7 * 86400))
                        _envelope(env, params, now())
                        entry["evidence"] = _save_evidence(root, env, runtime, capacity_guard)
                        entry["returned_fixtures"] = len(env["response"])
                        if not env["response"]:
                            entry.update(status="empty_unverified", reason="empty_does_not_prove_no_fixtures")
                            continue
                        request_stage = "club_fixture_validate"
                        for index, raw in enumerate(env["response"]):
                            try:
                                row = _identity(raw, year, team=team)
                                mid = row["match_id"]
                                if FRIENDLY.search(raw["league"]["name"]):
                                    entry["excluded_friendlies"].append(mid)
                                    continue
                                comp = competition_type(row["league_id"], year)
                                if comp["friendly"]:
                                    entry["excluded_friendlies"].append(mid)
                                    continue
                                row.update(competition_type=comp["type"], observed_at=env["fetched_at"],
                                    received_at=_iso(now()), source_response_sha256=entry["evidence"]["sha256"])
                                signature = _signature(raw)
                                if mid in records and records[mid]["signature"] != signature:
                                    conflicts.add(mid)
                                elif mid not in records:
                                    records[mid] = {"raw": raw, "row": row, "signature": signature}
                                entry["accepted_fixtures"] += 1
                            except (APIError, ValueError, KeyError, TypeError, OSError) as exc:
                                entry["invalid_fixtures"].append({"response_index": index,
                                    "diagnostic_code": _diagnostic_code(exc),
                                    "reason": _diagnostic_code(exc) if isinstance(exc, RequestBudgetError) else
                                              "competition_or_fixture_validation_failed"})
                        entry["status"] = "partial" if entry["invalid_fixtures"] else "collected"
                    except (APIError, ValueError, KeyError, TypeError, OSError) as exc:
                        failed(entry, exc, request_stage)
    except (APIError, ValueError, KeyError, TypeError, OSError) as exc:
        issue = {"status": "pending"}
        failed(issue, exc, stage)
        coverage["issues"].append(issue)
    # Retain an explicit entry for every request, even initialization failure.
    attempted = {(x["team_id"], x["season"]) for x in coverage["team_season_requests"]}
    for team in teams:
        for year in seasons:
            if (team, year) not in attempted:
                coverage["team_season_requests"].append({"team_id": team, "season": year,
                    "status": "deferred", "reason": "collection_initialization_failed"})
    coverage["team_season_requests"].sort(key=lambda x: (x["team_id"], -x["season"]))
    coverage["target_match_season_mapping"] = [
        {"match_id": mid, "season": season_join.get(mid)} for mid in sorted(target_ids)]
    missing_join = sorted(set(target_ids) - set(season_join))
    if missing_join:
        coverage["issues"].append({"reason": "normalized_target_season_join_missing", "match_ids": missing_join})
    finished = now()
    for mid, item in sorted(records.items()):
        if mid in conflicts:
            continue
        row = item["row"]
        report["schedule_events"].append(row)
        if row["status"] not in FINISHED or _utc(row["kickoff"]) + timedelta(hours=4) > finished:
            continue
        # Workload needs an actual finished kickoff, not the winner or goals.
        coverage["workload_events"].append(row)
        try:
            normalized = normalize_fixtures([item["raw"]], finished)
            if len(normalized) != 1:
                raise DataError("finished_fixture_not_normalized")
            report["schedule_matches"].append({**normalized[0],
                **{k: row[k] for k in ("league_id", "season", "status", "competition_type", "observed_at",
                                       "received_at", "source_response_sha256")}})
        except (ValueError, KeyError, TypeError):
            coverage["issues"].append({"reason": "regulation_score_unavailable_workload_only", "match_id": mid})
    for field in ("schedule_matches", "schedule_events"):
        report[field].sort(key=lambda x: (x["kickoff"], x["match_id"]))
    normalized_by_id = {row["match_id"]: row for row in report["schedule_matches"]}
    coverage["normalized_target_match_ids_missing_from_club_responses"] = sorted(
        set(target_ids) - set(normalized_by_id))
    for mid in set(target_ids) & set(normalized_by_id):
        current, target = normalized_by_id[mid], targets_by_id[mid]
        different = (_utc(current["kickoff"]) != _utc(target["kickoff"])
            or any(current[key] != target[key] for key in ("home_id", "away_id", "home_goals", "away_goals"))
            or current["season"] != season_join.get(mid)
            or current["league_id"] != report.get("provider_league_id"))
        if different:
            coverage["target_match_identity_or_score_mismatches"].append(mid)
    coverage["target_match_identity_or_score_mismatches"] = sorted(set(
        coverage["target_match_identity_or_score_mismatches"]))
    coverage["workload_events"].sort(key=lambda x: (x["kickoff"], x["match_id"]))
    coverage["conflict_match_ids"] = sorted(conflicts)
    coverage["competition_metadata"] = [x for x in metadata.values() if isinstance(x, dict)]
    coverage["request_status_counts"] = dict(Counter(x["status"] for x in coverage["team_season_requests"]))
    coverage["normalized_target_matches_by_season"] = dict(Counter(
        str(season_join[mid]) if mid in season_join else "unknown" for mid in target_ids))
    query_complete = (all(x["status"] == "collected" for x in coverage["team_season_requests"])
        and len(coverage["league_season_audit"]) == len(seasons)
        and all(x["status"] == "collected" for x in coverage["league_season_audit"])
        and not missing_join and not conflicts and not coverage["target_match_identity_or_score_mismatches"]
        and not coverage["normalized_target_match_ids_missing_from_club_responses"]
        and not any(x.get("failure_stage") for x in coverage["issues"]))
    coverage["provider_queries_complete"] = query_complete
    coverage["completeness_status"] = "provider_queries_complete_census_unverified" if query_complete else "partial"
    report.update(status="complete_provider_scope" if query_complete else "partial",
                  collection_finished_at=_iso(finished), generated_at=_iso(now()),
                  api_requests=getattr(client, "requests_made", 0) - requests_before,
                  exit_code=0 if query_complete else 1)
    report["api_budget"] = client.budget_snapshot() if client and callable(getattr(client, "budget_snapshot", None)) else None
    path = root / "runs" / (run_id + ".json")
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = _canonical(report).encode("utf-8")
    ticket = capacity_guard.check_write(path, raw, final=True) if capacity_guard is not None else None
    with path.open("x", encoding="utf-8") as handle:
        handle.write(raw.decode("utf-8"))
    if ticket is not None:
        capacity_guard.commit_write(ticket)
    temporary = root / "latest.tmp"
    latest = root / "latest.json"
    ticket = capacity_guard.check_write(latest, raw, final=True) if capacity_guard is not None else None
    temporary.write_bytes(raw)
    temporary.replace(latest)
    if ticket is not None:
        capacity_guard.commit_write(ticket)
    return report["exit_code"], report


def _batch_outcome(report):
    """Keep budget deferral separate from rejected provider identity/evidence."""
    coverage = report["coverage"]
    if coverage.get("provider_queries_complete") is True:
        return "complete_provider_scope"
    deferred, errors = [], []
    deferred_codes = {"budget_deferred", "runtime_deferred", "storage_capacity_deferred"}
    for row in coverage.get("team_season_requests", []) + coverage.get("league_season_audit", []):
        if row.get("status") == "deferred":
            deferred.append(row)
        elif row.get("status") == "partial":
            for issue in row.get("invalid_fixtures", []):
                (deferred if issue.get("reason") in deferred_codes else errors).append(issue)
        elif row.get("status") != "collected":
            errors.append(row)
    for issue in coverage.get("competition_metadata_failures", []):
        (deferred if issue.get("diagnostic_code") in deferred_codes else errors).append(issue)
    for issue in coverage.get("issues", []):
        if issue.get("failure_stage"):
            (deferred if issue.get("reason") in deferred_codes else errors).append(issue)
    if coverage.get("target_match_identity_or_score_mismatches") or coverage.get("conflict_match_ids"):
        errors.append({"reason": "identity_or_score_conflict"})
    return "failed" if errors or not deferred else "deferred"


def collect_all_schedules(config, runtime="runtime", *, source_commit=None, client=None,
                          clock=None, reserve_requests=1500, max_run_seconds=600,
                          current_max_age_seconds=6 * 3600, capacity_guard=None):
    """Resume domestic leagues with one real account balance and network deadline.

    The supplied APIClient (when present) must enforce the shared max_run_seconds.
    Cache hits remain eligible after network deferral; fetched_at is never reset.
    """
    from .advanced_capacity import CapacityGuard, guarded_cache_writes
    runtime = Path(runtime).resolve()
    clock = clock or (lambda: datetime.now(timezone.utc))
    started = _utc(clock())
    _integer(reserve_requests, "reserve_requests")
    if (type(max_run_seconds) not in (int, float) or not math.isfinite(max_run_seconds)
            or max_run_seconds <= 0):
        raise ValueError("invalid_run_time_budget")
    if (type(current_max_age_seconds) not in (int, float) or not math.isfinite(current_max_age_seconds)
            or current_max_age_seconds <= 0):
        raise ValueError("invalid_current_cache_age")
    if source_commit is not None and not re.fullmatch(r"[a-f0-9]{40}", source_commit):
        raise ValueError("invalid_source_commit")
    domestic = [x["key"] for x in select_competitions(config) if x.get("mode") == "domestic_baseline"]
    if not domestic or len(set(domestic)) != len(domestic):
        raise ValueError("domestic_competition_set_invalid")
    for key in domestic:
        _root(runtime, key)
    root = runtime / "feature_models" / "schedules" / "collection"
    previous = _read(root / "latest.json") if (root / "latest.json").is_file() else {}
    if previous and previous.get("schema_version") != "club-schedule-batch-v1":
        raise DataError("schedule_batch_identity_mismatch")
    last = previous.get("last_attempted_league")
    order = list(domestic)
    if last in order:
        pivot = order.index(last) + 1
        order = order[pivot:] + order[:pivot]
    # Stable sort preserves round-robin order within each completion group.
    completion = {r.get("league"): r.get("status") for r in previous.get("results", [])}
    order.sort(key=lambda key: completion.get(key) == "complete_provider_scope")
    guard = capacity_guard or CapacityGuard(runtime)
    results, last_attempted = [], last
    report = {"schema_version": "club-schedule-batch-v1", "source_commit": source_commit,
        "started_at": _iso(started), "configured_domestic_leagues": domestic,
        "excluded_non_domestic_leagues": [x["key"] for x in config.get("competitions", [])
            if x.get("mode") != "domestic_baseline"],
        "processing_order": order, "current_cache_max_age_seconds": current_max_age_seconds,
        "global_network_max_seconds": max_run_seconds, "reserve_requests": reserve_requests,
        "resume_policy": "unfinished_leagues_first_then_persistent_round_robin; unfinished_club_seasons_first",
        "results": results, "historical_availability_verified": False,
        "independent_fixture_census_verified": False}
    initialization_error = None
    try:
        client = client or APIClient(runtime / "cache", max_requests=config.get("max_requests_per_run"),
            daily_limit=config.get("api_daily_limit"), max_run_seconds=max_run_seconds, clock=clock)
        # Account status is observed once for the entire batch, not per league.
        client.status()
    except (APIError, ValueError, KeyError, TypeError, OSError) as exc:
        initialization_error = {"status": "deferred" if isinstance(exc, RequestBudgetError) else "failed",
                                "reason": _diagnostic_code(exc), "stage": "shared_account_initialization"}
    context = guarded_cache_writes(client, guard) if initialization_error is None else nullcontext()
    with context:
        for league in order:
            if initialization_error is not None:
                results.append({"league": league, **initialization_error})
                continue
            before = getattr(client, "requests_made", 0)
            try:
                _, detail = collect_schedule(config, runtime, league, source_commit=source_commit,
                    client=client, clock=clock, reserve_requests=reserve_requests,
                    max_run_seconds=max_run_seconds, current_max_age_seconds=current_max_age_seconds,
                    pending_first=True, check_account=False, capacity_guard=guard)
                counts = detail["coverage"]["request_status_counts"]
                path = _root(runtime, league) / "runs" / (detail["run_id"] + ".json")
                result = {"league": league, "status": _batch_outcome(detail),
                    "provider_queries_complete": detail["coverage"]["provider_queries_complete"],
                    "expected_team_season_requests": detail["coverage"]["expected_team_season_requests"],
                    "request_status_counts": counts, "api_requests": detail["api_requests"],
                    "report_ref": {"path": path.relative_to(runtime).as_posix(),
                                   "sha256": hashlib.sha256(path.read_bytes()).hexdigest()},
                    "cache_reused_responses": sum(x.get("evidence", {}).get("from_cache") is True
                        for x in detail["coverage"]["team_season_requests"] + detail["coverage"]["league_season_audit"])}
            except (APIError, ValueError, KeyError, TypeError, OSError) as exc:
                result = {"league": league,
                    "status": "deferred" if isinstance(exc, RequestBudgetError) else "failed",
                    "reason": _diagnostic_code(exc), "stage": "league_collection",
                    "api_requests": getattr(client, "requests_made", 0) - before}
            results.append(result)
            # Cache-only scans after a soft stop cannot advance the network
            # cursor past every deferred league and restart the same prefix.
            if getattr(client, "requests_made", 0) > before:
                last_attempted = league
    report.update(finished_at=_iso(_utc(clock())), last_attempted_league=last_attempted,
        completed_leagues=sum(r["status"] == "complete_provider_scope" for r in results),
        deferred_leagues=sum(r["status"] == "deferred" for r in results),
        failed_leagues=sum(r["status"] == "failed" for r in results),
        full_collection_completed=all(r["status"] == "complete_provider_scope" for r in results),
        api_requests=getattr(client, "requests_made", 0),
        api_budget=client.budget_snapshot() if client is not None and hasattr(client, "budget_snapshot") else None,
        capacity=guard.summary())
    report["execution_status"] = "failed" if report["failed_leagues"] else "deferred" if report["deferred_leagues"] else "complete"
    raw = _canonical(report).encode("utf-8")
    archive = root / "runs" / (hashlib.sha256(raw).hexdigest() + ".json")
    for path in (archive, root / "latest.json"):
        ticket = guard.check_write(path, raw, final=True)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        guard.commit_write(ticket)
    return int(report["failed_leagues"] > 0), report


def main(argv=None):
    parser = argparse.ArgumentParser(description="跨正式俱乐部赛事采集赛程；不调整生产模型")
    parser.add_argument("--runtime", default="runtime")
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--league", default="serie_a")
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--reserve-requests", type=int)
    parser.add_argument("--max-run-seconds", type=float)
    parser.add_argument("--current-cache-seconds", type=float, default=6 * 3600)
    args = parser.parse_args(argv)
    try:
        if args.league == "all":
            code, report = collect_all_schedules(_read(args.config), args.runtime,
                source_commit=args.source_commit,
                reserve_requests=1500 if args.reserve_requests is None else args.reserve_requests,
                max_run_seconds=600 if args.max_run_seconds is None else args.max_run_seconds,
                current_max_age_seconds=args.current_cache_seconds)
        else:
            code, report = collect_schedule(_read(args.config), args.runtime, args.league,
                source_commit=args.source_commit,
                reserve_requests=20 if args.reserve_requests is None else args.reserve_requests,
                max_run_seconds=args.max_run_seconds)
    except (ValueError, KeyError, TypeError, OSError):
        print("俱乐部赛程采集未完成：配置、历史数据或保存路径无效；未打印原始异常。")
        return 2
    if args.league == "all":
        print(json.dumps({k: report[k] for k in ('execution_status', 'completed_leagues',
            'deferred_leagues', 'failed_leagues', 'full_collection_completed', 'last_attempted_league',
            'api_requests')}, ensure_ascii=False))
    else:
        print(f"俱乐部赛程采集：{report['status']}；{len(report['schedule_matches'])}场90分钟赛果；"
              f"{len(report['coverage']['workload_events'])}场正式已赛负荷事件；历史PIT未认证。")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
