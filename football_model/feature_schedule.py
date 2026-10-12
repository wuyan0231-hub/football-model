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

from .data import (APIClient, APIError, DataError, RequestBudgetError,
                   _fixture_identity, _integer, _iso, _utc, normalize_fixtures)


VERSION = "club-schedule-collection-1.0"
STATUSES = {"TBD", "NS", "1H", "HT", "2H", "ET", "BT", "P", "SUSP", "INT",
            "FT", "AET", "PEN", "PST", "CANC", "ABD", "AWD", "WO", "LIVE"}
FINISHED = {"FT", "AET", "PEN"}
FRIENDLY = re.compile(r"\bfriendl(?:y|ies)\b|友谊", re.IGNORECASE)


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2,
                      allow_nan=False) + "\n"


def _read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _root(runtime, league):
    if not isinstance(league, str) or not re.fullmatch(r"[a-z][a-z0-9_]*", league):
        raise ValueError("invalid_league_key")
    return Path(runtime) / "feature_models" / "schedules" / league


def load_schedule(runtime, league):
    """Read the latest collector output, including explicit coverage limitations.

    ``matches`` contain validated regulation-score rows. ``coverage`` also has
    ``workload_events``: finished formal fixtures usable for rest/density only,
    even when regulation scores could not be established. No score is filled.
    """
    payload = _read(_root(runtime, league) / "latest.json")
    if payload.get("version") != VERSION or payload.get("league") != league:
        raise DataError("schedule_output_identity_mismatch")
    matches, coverage = payload.get("schedule_matches"), payload.get("coverage")
    if not isinstance(matches, list) or not isinstance(coverage, dict):
        raise DataError("schedule_output_structure_invalid")
    return matches, coverage


def _save_evidence(root, envelope, runtime):
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
        path.write_bytes(content)
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


def _metadata(client, league_id, season, now, root, runtime):
    params = {"id": league_id, "season": season}
    envelope = client._cached_request("/leagues", params, force=False, max_age_seconds=7 * 86400)
    if (envelope.get("endpoint") != "/leagues" or envelope.get("params") != params
            or envelope.get("errors") or not isinstance(envelope.get("response"), list)
            or _utc(envelope["fetched_at"]) > now):
        raise DataError("competition_metadata_envelope_invalid")
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
            "evidence": _save_evidence(root, envelope, runtime)}


def collect_schedule(config, runtime="runtime", league="serie_a", *, source_commit=None,
                     client=None, clock=None, reserve_requests=20, max_run_seconds=None):
    """Persist a finite club×season collection and all failures; no model fitting."""
    runtime = Path(runtime)
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
            reason="budget_deferred" if isinstance(exc, RequestBudgetError) else
                   "provider_error" if isinstance(exc, APIError) else "validation_or_storage_error",
            failure_stage=failed_stage, checked_at=_iso(now()))

    def competition_type(lid, year):
        key = (lid, year)
        if key not in metadata:
            try:
                metadata[key] = _metadata(client, lid, year, now(), root, runtime)
            except (APIError, ValueError, KeyError, TypeError, OSError) as exc:
                # Repeating failed metadata queries for every club does not add
                # evidence and needlessly consumes the same quota.
                metadata[key] = exc
        if isinstance(metadata[key], Exception):
            raise metadata[key]
        return metadata[key]

    try:
        client = client or APIClient(runtime / "cache", max_requests=config.get("max_requests_per_run"),
            daily_limit=config.get("api_daily_limit"), max_run_seconds=effective_seconds, clock=clock)
        stage = "account_budget_check"
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
                        force=(year == seasons[0]), max_age_seconds=7 * 86400)
                    _envelope(env, {"league": lid, "season": year}, now())
                    audit["evidence"] = _save_evidence(root, env, runtime)
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

            for team in teams:
                for year in seasons:
                    entry = {"team_id": team, "season": year, "status": "pending",
                             "accepted_fixtures": 0, "excluded_friendlies": [], "invalid_fixtures": []}
                    coverage["team_season_requests"].append(entry)
                    request_stage = "club_fixture_fetch"
                    try:
                        params = {"team": team, "season": year}
                        env = client._cached_request("/fixtures", params,
                            force=(year == seasons[0]), max_age_seconds=7 * 86400)
                        _envelope(env, params, now())
                        entry["evidence"] = _save_evidence(root, env, runtime)
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
                                    "reason": "budget_deferred" if isinstance(exc, RequestBudgetError) else
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
                  api_requests=getattr(client, "requests_made", 0), exit_code=0 if query_complete else 1)
    report["api_budget"] = client.budget_snapshot() if client and callable(getattr(client, "budget_snapshot", None)) else None
    path = root / "runs" / (run_id + ".json")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as handle:
        handle.write(_canonical(report))
    temporary = root / "latest.tmp"
    temporary.write_text(_canonical(report), encoding="utf-8")
    temporary.replace(root / "latest.json")
    return report["exit_code"], report


def main(argv=None):
    parser = argparse.ArgumentParser(description="跨正式俱乐部赛事采集赛程；不调整生产模型")
    parser.add_argument("--runtime", default="runtime")
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--league", default="serie_a")
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--reserve-requests", type=int, default=20)
    parser.add_argument("--max-run-seconds", type=float)
    args = parser.parse_args(argv)
    try:
        code, report = collect_schedule(_read(args.config), args.runtime, args.league,
            source_commit=args.source_commit, reserve_requests=args.reserve_requests,
            max_run_seconds=args.max_run_seconds)
    except (ValueError, KeyError, TypeError, OSError):
        print("俱乐部赛程采集未完成：配置、历史数据或保存路径无效；未打印原始异常。")
        return 2
    print(f"俱乐部赛程采集：{report['status']}；{len(report['schedule_matches'])}场90分钟赛果；"
          f"{len(report['coverage']['workload_events'])}场正式已赛负荷事件；历史PIT未认证。")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
