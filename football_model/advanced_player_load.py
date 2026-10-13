"""Restricted observed national-team player minutes with an auditable denominator.

This is deliberately NOT total international workload. The initial verified
scope is UEFA Nations League only. A player absent from its responses remains
unknown; an absent/null player never becomes a zero-minute observation. Every
fixture in the relevant window needs a complete fixture-list denominator and a
retained, identity-checked player-detail response. Model coefficients are fitted
by the existing correction pipeline, not specified here.
"""
from __future__ import annotations

import argparse
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path

from .advanced_capacity import CapacityGuard, guarded_cache_writes
from .data import APIClient, APIError, DataError, RequestBudgetError, _integer, _iso, _utc

SCHEMA = "advanced-restricted-player-load-v1"
SCOPE = "uefa_nations_league_only"
FEATURE_NAMES = ("restricted_observed_national_minutes_per_matched_xi",
                 "restricted_observed_national_xi_match_count",
                 "restricted_observed_national_xi_match_fraction",
                 "opponent_restricted_observed_national_minutes_per_matched_xi",
                 "opponent_restricted_observed_national_xi_match_count",
                 "opponent_restricted_observed_national_xi_match_fraction")
FINISHED = {"FT", "AET", "PEN"}
RESULT_DELAY = timedelta(hours=4)


def _read(path):
    from .advanced_features import read_json
    return read_json(path)


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def _ref(path, runtime):
    from .advanced_features import source_ref
    return source_ref(path, runtime)


def _save(runtime, directory, value, guard, *, final=False):
    raw = (_canonical(value) + "\n").encode()
    path = runtime / "advanced_features" / "player_load" / directory / (hashlib.sha256(raw).hexdigest() + ".json")
    ticket = guard.check_write(path, raw, final=final)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != raw:
            raise DataError("player_load_immutable_collision")
    else:
        with path.open("xb") as output:
            output.write(raw)
    guard.commit_write(ticket)
    return _ref(path, runtime)


def _copy_source(runtime, source, guard):
    """Freeze original bytes before the mutable production history can change."""
    raw = source.read_bytes()
    path = runtime / "advanced_features" / "player_load" / "sources" / (hashlib.sha256(raw).hexdigest() + ".json")
    ticket = guard.check_write(path, raw)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != raw:
            raise DataError("player_load_immutable_collision")
    else:
        with path.open("xb") as output:
            output.write(raw)
    guard.commit_write(ticket)
    return _ref(path, runtime)


def _resolve(runtime, ref):
    path = (runtime / ref["path"]).resolve()
    if not path.is_relative_to(runtime.resolve()) or _ref(path, runtime) != ref:
        raise DataError("player_load_source_hash_mismatch")
    return _read(path)


def _envelope(value, endpoint, params, now):
    if (not isinstance(value, dict) or value.get("endpoint") != endpoint
            or value.get("params") != params or value.get("errors")
            or not isinstance(value.get("response"), list)
            or value.get("paging", {}).get("current", 1) != 1
            or value.get("paging", {}).get("total", 1) != 1
            or ("results" in value and value["results"] != len(value["response"]))):
        raise DataError("player_load_envelope_or_denominator_invalid")
    if _utc(value["fetched_at"]) > now:
        raise DataError("player_load_source_observed_in_future")
    return value["response"]


def _metadata(value, competition, now):
    params = {"name": competition["name"], "country": competition["country"]}
    rows = _envelope(value, "/leagues", params, now)
    matches = [row for row in rows if row.get("league", {}).get("name") == "UEFA Nations League"
               and row.get("country", {}).get("name") == "World"]
    if len(matches) != 1:
        raise DataError("player_load_national_scope_not_unique")
    item = matches[0]
    _integer(item["league"]["id"], "league_id", 1)
    seasons = item.get("seasons")
    if not isinstance(seasons, list) or len({s["year"] for s in seasons}) != len(seasons):
        raise DataError("player_load_seasons_invalid")
    return item


def _fixture(raw, league_id, season):
    from .fundamentals import _fixture as identity
    row = identity(raw)
    if row["status"] not in {"TBD", "NS", "1H", "HT", "2H", "ET", "BT", "P", "SUSP", "INT", "FT", "AET", "PEN", "PST", "CANC", "ABD", "AWD", "WO", "LIVE"}:
        raise DataError("player_load_fixture_status_invalid")
    if raw.get("league", {}).get("id") != league_id or raw.get("league", {}).get("season") != season:
        raise DataError("player_load_fixture_competition_mismatch")
    return row


def _schedule(value, league_id, season, now):
    rows = _envelope(value, "/fixtures", {"league": league_id, "season": season}, now)
    if not rows:
        raise DataError("player_load_empty_schedule_unverified")
    seen, result = set(), []
    for raw in rows:
        row = _fixture(raw, league_id, season)
        if row["match_id"] in seen:
            raise DataError("player_load_duplicate_fixture")
        seen.add(row["match_id"])
        result.append(row)
    return sorted(result, key=lambda r: (r["kickoff"], r["match_id"]))


def _detail(value, expected, league_id, season, now):
    from .advanced_features import embedded_snapshot
    rows = _envelope(value, "/fixtures", {"id": expected["match_id"]}, now)
    if len(rows) != 1 or _fixture(rows[0], league_id, season) != expected:
        raise DataError("player_load_detail_identity_mismatch")
    if expected["status"] not in FINISHED:
        raise DataError("player_load_detail_not_finished")
    snap = embedded_snapshot("players", value, expected["match_id"])
    # Both teams must have a nonempty response. Null bench minutes remain null;
    # they are not fabricated as 0 or counted as observed appearances.
    if any(not snap["teams"][str(tid)]["players"] for tid in (expected["home_id"], expected["away_id"])):
        raise DataError("player_load_detail_team_missing")
    player_ids = [p["player_id"] for team in snap["teams"].values() for p in team["players"]]
    if len(player_ids) != len(set(player_ids)):
        raise DataError("player_load_player_on_both_national_teams")
    # This projection is used solely for physical minutes, not target scores.
    for team in snap["teams"].values():
        team["minutes_scope"] = ("provider_reported_including_extra_time" if expected["status"] in {"AET", "PEN"}
                                 else "provider_reported_regulation_including_stoppage")
    snap["normalization_parent_snapshot_id"] = snap.pop("snapshot_id")
    snap["schema_version"] = "advanced-restricted-load-minutes-v1"
    snap["load_source_status"] = expected["status"]
    snap["snapshot_id"] = hashlib.sha256(_canonical(snap).encode()).hexdigest()
    return snap


def _reason(exc):
    if isinstance(exc, RequestBudgetError):
        return getattr(exc, "reason", "actual_budget_or_runtime_deferred")
    if isinstance(exc, DataError) and str(exc).startswith("player_load_"):
        return str(exc)
    return "provider_or_source_validation_failed"


def collect_run(config, runtime="runtime", national_league="unl", *, client=None,
                clock=None, reserve_requests=250, max_seconds=None, refresh=False,
                capacity_guard=None):
    """Executable, resumable collection; deferred rows retain endpoint/params.

    Uses only already-approved /leagues and /fixtures endpoints. Valid cache
    entries retain their original fetched_at. No per-run match-count cap is
    introduced; the real client budgets, time budget and capacity guard apply.
    """
    _integer(reserve_requests, "reserve_requests", 0)
    runtime = Path(runtime).resolve()
    clock = clock or (lambda: datetime.now(timezone.utc))
    started = _utc(clock())
    guard = capacity_guard or CapacityGuard(runtime)
    def now():
        value = _utc(clock())
        if value < started:
            raise DataError("player_load_clock_moved_backwards")
        return value
    chosen = [c for c in config["competitions"] if c.get("key") == national_league]
    if (len(chosen) != 1 or chosen[0].get("mode") != "national_baseline"
            or chosen[0].get("name") != "UEFA Nations League" or chosen[0].get("country") != "World"):
        raise DataError("player_load_requires_verified_nations_league_scope")
    competition = chosen[0]
    history_path = runtime / "data" / (national_league + ".json")
    saved = _read(history_path)
    years = saved.get("requested_seasons")
    if not isinstance(years, list) or not years or len(set(years)) != len(years):
        raise DataError("player_load_requested_seasons_missing")
    for year in years:
        _integer(year, "season", 1900)
    history_ref, history_deferred = None, None
    try:
        history_ref = _copy_source(runtime, history_path, guard)
    except RequestBudgetError as exc:
        history_deferred = _reason(exc)
    report = {"schema_version": SCHEMA, "scope": SCOPE, "national_league": national_league,
              "scope_is_all_international_matches": False, "competition": competition,
              "requested_seasons": sorted(years), "started_at": _iso(started),
              "history_ref": history_ref, "metadata_ref": None,
              "schedule_queries": [], "details": [], "pending_requests": [],
              "issues": [], "production_reservation_requests": reserve_requests,
              "historical_download_not_point_in_time": True}
    if history_deferred:
        report.update(finished_at=_iso(now()), provider_schedule_queries_complete=False,
            detail_counts={"requested":0,"complete":0}, full_collection_completed=False,
            execution_status="deferred", api_budget=None, api_requests=0,
            history_identity_mismatch_ids=[])
        report["pending_requests"].append({"operation":"freeze_national_history",
            "source_path":str(history_path.relative_to(runtime)), "reason":history_deferred})
        _save(runtime, "manifests", report, guard, final=True)
        return 0, report
    initialization_failed = None
    try:
        client = client or APIClient(runtime / "cache", max_requests=config.get("max_requests_per_run"),
            daily_limit=config.get("api_daily_limit", 5000),
            max_run_seconds=max_seconds or config.get("api_max_run_seconds", 3600), clock=clock)
        client.status()
        budget = client.budget_snapshot() if hasattr(client, "budget_snapshot") else {}
        if budget.get("daily_quota_status") in ("unknown", "incomplete") and budget.get("configured_daily_limit") is not None:
            raise DataError("player_load_actual_daily_balance_unknown")
    except (APIError, ValueError, OSError) as exc:
        initialization_failed = _reason(exc)
        report["issues"].append({"reason": initialization_failed})
    reservation = client.reserve_requests(reserve_requests) if client is not None and hasattr(client, "reserve_requests") else nullcontext()
    metadata = None
    expected = {}
    with reservation, guarded_cache_writes(client, guard):
        metadata_params = {"name": competition["name"], "country": competition["country"]}
        try:
            if initialization_failed:
                raise DataError(initialization_failed)
            env = client._cached_request("/leagues", metadata_params, force=refresh, max_age_seconds=86400)
            report["metadata_ref"] = _save(runtime, "sources", env, guard)
            metadata = _metadata(env, competition, now())
        except (APIError, ValueError, TypeError, KeyError, OSError, AttributeError) as exc:
            report["pending_requests"].append({"endpoint": "/leagues", "params": metadata_params, "reason": _reason(exc)})
            if not isinstance(exc, RequestBudgetError):
                report["issues"].append({"reason": _reason(exc), "stage": "national_metadata"})
        for year in sorted(years, reverse=True):
            query = {"season": year, "status": "pending", "source_ref": None}
            report["schedule_queries"].append(query)
            if metadata is None:
                query.update(status="deferred", reason="national_metadata_unavailable")
                report["pending_requests"].append({"endpoint": "/fixtures", "season": year,
                    "league_from_metadata": national_league, "reason": query["reason"]})
                continue
            lid = metadata["league"]["id"]
            params = {"league": lid, "season": year}
            try:
                if not any(s["year"] == year for s in metadata["seasons"]):
                    raise DataError("player_load_season_not_in_metadata")
                env = client.fetch_fixtures(lid, year, current=False, force=refresh, max_age_seconds=86400)
                query["source_ref"] = _save(runtime, "sources", env, guard)
                rows = _schedule(env, lid, year, now())
                for row in rows:
                    mid = row["match_id"]
                    if mid in expected:
                        raise DataError("player_load_fixture_in_multiple_seasons")
                    expected[mid] = (row, lid, year)
                query.update(status="complete", fixture_count=len(rows))
            except (APIError, ValueError, TypeError, KeyError, OSError, AttributeError) as exc:
                query.update(status="deferred" if isinstance(exc, RequestBudgetError) else "failed", reason=_reason(exc))
                report["pending_requests"].append({"endpoint": "/fixtures", "params": params, "reason": query["reason"]})
        # Preserve the original national-history IDs in the denominator audit.
        actual = {mid: {k: row[k] for k in ("match_id", "home_id", "away_id", "kickoff")}
                  for mid, (row, _, _) in expected.items()}
        from .advanced_features import _identity
        history_mismatches = [r["match_id"] for r in saved["matches"] if actual.get(r["match_id"]) != _identity(r)]
        report["history_identity_mismatch_ids"] = history_mismatches
        if history_mismatches and all(q["status"] == "complete" for q in report["schedule_queries"]):
            report["issues"].append({"reason": "player_load_history_identity_or_denominator_mismatch"})
        for mid, (row, lid, year) in sorted(expected.items(), key=lambda p: (p[1][0]["kickoff"], p[0]), reverse=True):
            if row["status"] not in FINISHED or _utc(row["kickoff"]) + RESULT_DELAY > now():
                continue
            item = {"fixture": row, "league_id": lid, "season": year, "status": "pending", "source_ref": None}
            report["details"].append(item)
            try:
                env = client.fetch_fixture(mid, force=refresh, max_age_seconds=7 * 86400)
                item["source_ref"] = _save(runtime, "sources", env, guard)
                try:
                    _detail(env, row, lid, year, now())
                except (ValueError, TypeError, KeyError, AttributeError):
                    if refresh or env.get("from_cache") is not True:
                        raise
                    # A recent pre-match /fixtures?id cache can lack players or
                    # still say NS. Preserve it and refresh once immediately;
                    # do not defer valid final minutes for the cache's 7 days.
                    item["rejected_source_refs"] = [item["source_ref"]]
                    env = client.fetch_fixture(mid, force=True, max_age_seconds=0)
                    item["source_ref"] = _save(runtime, "sources", env, guard)
                    _detail(env, row, lid, year, now())
                item["status"] = "complete"
            except (APIError, ValueError, TypeError, KeyError, OSError, AttributeError) as exc:
                item.update(status="deferred" if isinstance(exc, RequestBudgetError) else "failed", reason=_reason(exc))
                report["pending_requests"].append({"endpoint": "/fixtures", "params": {"id": mid}, "reason": item["reason"]})
    report["finished_at"] = _iso(now())
    report["provider_schedule_queries_complete"] = bool(metadata and all(q["status"] == "complete" for q in report["schedule_queries"]) and not report["history_identity_mismatch_ids"])
    report["detail_counts"] = {"requested": len(report["details"]), "complete": sum(i["status"] == "complete" for i in report["details"])}
    report["full_collection_completed"] = report["provider_schedule_queries_complete"] and not report["pending_requests"]
    report["execution_status"] = ("complete" if report["full_collection_completed"] else "failed" if report["issues"] or any(i["status"] == "failed" for i in report["schedule_queries"] + report["details"]) else "deferred")
    report["api_budget"] = client.budget_snapshot() if client is not None and hasattr(client, "budget_snapshot") else None
    report["api_requests"] = getattr(client, "requests_made", 0)
    _save(runtime, "manifests", report, guard, final=True)
    return int(report["execution_status"] == "failed"), report


def load_sources(runtime, *, cutoff_at=None, mode="historical"):
    """Validate latest manifest and every dependency; never fall back silently.

    Pass this result to feature_values for a whole batch. A frozen capture only
    needs returned source_refs; no mutable latest pointer is required.
    """
    runtime = Path(runtime).resolve()
    cutoff = _utc(cutoff_at) if cutoff_at is not None else None
    if mode not in ("historical", "prospective"):
        raise DataError("player_load_invalid_mode")
    if mode == "prospective" and cutoff is None:
        raise DataError("player_load_prospective_cutoff_required")
    candidates = []
    for path in (runtime / "advanced_features" / "player_load" / "manifests").glob("*.json"):
        value = _read(path)
        if value.get("schema_version") != SCHEMA or value.get("scope") != SCOPE:
            continue
        if mode == "prospective" and _utc(value["finished_at"]) > cutoff:
            continue
        candidates.append((value["finished_at"], path.name, path, value))
    if not candidates:
        return {"scope": SCOPE, "complete_schedule": False, "fixtures": [], "snapshots": {},
                "source_refs": [], "issues": [{"reason": "player_load_collection_missing"}]}
    _, _, path, value = max(candidates)
    observed = _utc(value["finished_at"])
    refs = {_ref(path, runtime)["path"]: _ref(path, runtime)}
    def read_ref(ref):
        result = _resolve(runtime, ref)
        refs[ref["path"]] = ref
        if mode == "prospective" and "fetched_at" in result and _utc(result["fetched_at"]) > cutoff:
            raise DataError("player_load_source_after_cutoff")
        return result
    # Retain the complete manifest dependency closure, including rejected
    # response originals. Hash validity is distinct from feature admissibility.
    def dependency_refs(item):
        if isinstance(item, dict):
            if set(item) == {"path", "sha256"}:
                yield item
            else:
                for child in item.values():
                    yield from dependency_refs(child)
        elif isinstance(item, list):
            for child in item:
                yield from dependency_refs(child)
    for ref in dependency_refs(value):
        read_ref(ref)
    history = read_ref(value["history_ref"])
    if mode == "prospective" and (history.get("observed_at") is None or _utc(history["observed_at"]) > cutoff):
        raise DataError("player_load_history_after_cutoff")
    issues, rows, snaps = [], {}, {}
    metadata = _metadata(read_ref(value["metadata_ref"]), value["competition"], observed) if value["metadata_ref"] else None
    complete = metadata is not None
    expected_years = value["requested_seasons"]
    queries = value["schedule_queries"]
    if sorted(q["season"] for q in queries) != sorted(expected_years) or not expected_years:
        raise DataError("player_load_schedule_request_denominator_mismatch")
    for query in queries:
        if query["status"] != "complete" or metadata is None:
            complete = False
            continue
        year = query["season"]
        if not any(s["year"] == year for s in metadata["seasons"]):
            raise DataError("player_load_season_not_in_metadata")
        for row in _schedule(read_ref(query["source_ref"]), metadata["league"]["id"], year, observed):
            mid = row["match_id"]
            if mid in rows:
                raise DataError("player_load_fixture_in_multiple_seasons")
            rows[mid] = {**row, "season": year, "league_id": metadata["league"]["id"]}
    from .advanced_features import _identity
    mismatches = [r["match_id"] for r in history["matches"] if r["match_id"] not in rows or _identity(rows[r["match_id"]]) != _identity(r)]
    if mismatches != value["history_identity_mismatch_ids"]:
        raise DataError("player_load_history_audit_mismatch")
    complete = complete and not mismatches
    seen_details = set()
    for item in value["details"]:
        mid = item["fixture"]["match_id"]
        if mid in seen_details or mid not in rows or item["fixture"] != {k: rows[mid][k] for k in ("match_id", "kickoff", "home_id", "away_id", "status")}:
            raise DataError("player_load_detail_denominator_mismatch")
        seen_details.add(mid)
        if item["status"] != "complete":
            issues.append({"match_id": mid, "reason": item.get("reason", "player_load_detail_missing")})
            continue
        snaps[mid] = _detail(read_ref(item["source_ref"]), item["fixture"], item["league_id"], item["season"], observed)
    return {"scope": SCOPE, "complete_schedule": complete, "fixtures": sorted(rows.values(), key=lambda r:(r["kickoff"], r["match_id"])),
            "snapshots": snaps, "source_refs": sorted(refs.values(), key=lambda r:r["path"]), "issues": issues,
            "manifest_observed_at": value["finished_at"], "requested_seasons": expected_years}


def load_player_load_sources(runtime, league=None, records=None, *, cutoff_at=None, mode="historical"):
    """Optional-source adapter: corrupt national data cannot erase other models.

    Diagnostic references identify actual rejected bytes, not admitted parents.
    They intentionally remain separate from numeric source_refs. The existing
    snapshot protocol freezes admitted sources only; it does not claim a full
    frozen copy of these rejected originals. Missing and corrupt optional data
    have the same null numeric feature result.
    """
    try:
        return load_sources(runtime, cutoff_at=cutoff_at, mode=mode)
    except (ValueError, TypeError, KeyError, OSError, AttributeError) as exc:
        runtime = Path(runtime).resolve()
        diagnostics = []
        # Record actual file hashes without trusting expected references in a
        # broken manifest. No exception string can expose a provider secret.
        for path in sorted((runtime / "advanced_features" / "player_load" / "manifests").glob("*.json")):
            try:
                manifest = _read(path)
                if mode == "prospective" and cutoff_at is not None and _utc(manifest["finished_at"]) > _utc(cutoff_at):
                    continue
                diagnostics.append(_ref(path, runtime))
                refs = [manifest.get("history_ref"), manifest.get("metadata_ref")]
                refs += [q.get("source_ref") for q in manifest.get("schedule_queries", [])]
                refs += [q.get("source_ref") for q in manifest.get("details", [])]
                for ref in refs:
                    if not isinstance(ref, dict):
                        continue
                    source = (runtime / ref["path"]).resolve()
                    if source.is_relative_to(runtime) and source.is_file():
                        actual = _ref(source, runtime)
                        if actual != ref:
                            diagnostics.append(actual)
            except (ValueError, TypeError, KeyError, OSError, AttributeError):
                if path.is_file():
                    diagnostics.append(_ref(path, runtime))
        return {"scope": SCOPE, "complete_schedule": False, "fixtures": [], "snapshots": {},
                "source_refs": [], "diagnostic_refs": diagnostics,
                "issues": [{"reason": _reason(exc), "action": "restricted_player_load_group_unavailable",
                            "rejected_originals_fully_frozen": False}]}


def _prospective_sources(runtime, sources, cutoff):
    """Recheck preloaded originals before admitting this optional feature group.

    The shared feature index is loaded in historical mode for batch reuse. Its
    validated hashes do not establish prospective availability. Check the actual
    dependency closure here, including rejected responses retained by a manifest,
    so neither late data nor a changed original reaches the global freezer.
    """
    from .feature_snapshots import _check_times, SnapshotError
    runtime = Path(runtime).resolve()
    pending = list(sources.get("source_refs", []))
    seen, diagnostics = set(), {}
    def references(value):
        if isinstance(value, dict):
            if isinstance(value.get("path"), str) and isinstance(value.get("sha256"), str):
                yield value
            for child in value.values():
                yield from references(child)
        elif isinstance(value, list):
            for child in value:
                yield from references(child)
    try:
        if sources.get("manifest_observed_at") and _utc(sources["manifest_observed_at"]) > cutoff:
            raise DataError("player_load_manifest_after_cutoff")
        while pending:
            ref = pending.pop()
            key = (ref["path"], ref["sha256"])
            if key in seen:
                continue
            seen.add(key)
            path = (runtime / ref["path"]).resolve()
            if path.is_relative_to(runtime) and path.is_file():
                diagnostics[ref["path"]] = _ref(path, runtime)
            value = _resolve(runtime, ref)
            try:
                _check_times(value, cutoff)
            except SnapshotError as exc:
                raise DataError("player_load_source_after_cutoff") from exc
            if value.get("schema_version") == SCHEMA:
                if _utc(value["finished_at"]) > cutoff:
                    raise DataError("player_load_manifest_after_cutoff")
                history = _resolve(runtime, value["history_ref"])
                if history.get("observed_at") is None or _utc(history["observed_at"]) > cutoff:
                    raise DataError("player_load_history_after_cutoff")
            pending.extend(references(value))
        return sources
    except (ValueError, TypeError, KeyError, OSError, AttributeError) as exc:
        # Preserve actual byte identities as diagnostics only. An expected hash
        # from a stale index is never asserted to describe the changed original.
        for ref in sources.get("source_refs", []):
            try:
                path = (runtime / ref["path"]).resolve()
                if path.is_relative_to(runtime) and path.is_file():
                    diagnostics[ref["path"]] = _ref(path, runtime)
            except (ValueError, TypeError, KeyError, OSError, AttributeError):
                continue
        return {"scope": SCOPE, "complete_schedule": False, "fixtures": [], "snapshots": {},
                "source_refs": [], "diagnostic_refs": sorted(diagnostics.values(), key=lambda r:r["path"]),
                "issues": [{"reason": _reason(exc), "action": "restricted_player_load_group_unavailable",
                            "rejected_originals_fully_frozen": False}]}


def feature_values(runtime, league, fixture, cutoff_at, *, horizon="EARLY", mode="prospective",
                   lookback_days=14, sources=None, club_sources=None):
    """Per-side restricted observed minutes plus independent XI coverage.

    EARLY uses the immediately previous eligible club XI. LINEUP uses only the
    current verified XI (historical LINEUP remains an explicit assumption).
    """
    from .advanced_features import _identity, _select, load_sources as club_load_sources
    if mode not in ("historical", "prospective") or horizon not in ("EARLY", "LINEUP"):
        raise DataError("player_load_invalid_mode_or_horizon")
    if type(lookback_days) is not int or not 1 <= lookback_days <= 30:
        raise DataError("player_load_invalid_lookback")
    target, cutoff = _identity(fixture), _utc(cutoff_at)
    if cutoff >= _utc(target["kickoff"]):
        raise DataError("player_load_cutoff_not_pre_match")
    sources = sources if sources is not None else load_player_load_sources(runtime, cutoff_at=cutoff, mode=mode)
    club_sources = club_sources if club_sources is not None else club_load_sources(runtime, league)
    if mode == "prospective" and (club_sources.get("history_observed_at") is None or _utc(club_sources["history_observed_at"]) > cutoff):
        raise DataError("player_load_club_history_after_cutoff")
    if mode == "prospective":
        sources = _prospective_sources(runtime, sources, cutoff)
    refs = {r["path"]: r for r in sources["source_refs"]}
    refs[club_sources["history_source"]["path"]] = club_sources["history_source"]
    window = [r for r in sources["fixtures"] if cutoff-timedelta(days=lookback_days) <= _utc(r["kickoff"]) and _utc(r["kickoff"])+RESULT_DELAY <= cutoff and r["status"] in FINISHED and r["match_id"] != target["match_id"]]
    missing_details = [r["match_id"] for r in window if r["match_id"] not in sources["snapshots"]]
    complete = sources["complete_schedule"] and not missing_details
    own, evidence = {}, {}
    for tid in (target["home_id"], target["away_id"]):
        prior = [r for r in club_sources["history"] if r["match_id"] != target["match_id"] and tid in (r["home_id"], r["away_id"]) and _utc(r["kickoff"])+RESULT_DELAY <= cutoff]
        roster_record = target if horizon == "LINEUP" else (prior[-1] if prior else None)
        selected = _select(club_sources, "lineups", roster_record, cutoff, mode, target=horizon == "LINEUP") if roster_record else None
        roster = None
        if selected:
            snap, ref = selected
            refs[ref["path"]] = ref
            for dep in club_sources.get("dependencies", {}).get(ref["path"], []):
                refs[dep["path"]] = dep
            team = snap["teams"].get(str(tid), {})
            if team.get("complete_starting_eleven"):
                roster = {p["player_id"] for p in team["starters"]}
        totals, unknown_ids, appearances, unknown_identity = {}, set(), [], False
        if roster and complete:
            for row in window:
                snap = sources["snapshots"][row["match_id"]]
                if mode == "prospective" and _utc(snap["known_at"]) > cutoff:
                    raise DataError("player_load_detail_after_cutoff")
                for team in snap["teams"].values():
                    unknown_identity = unknown_identity or bool(team.get("unidentified_players"))
                    for p in team["players"]:
                        if p["player_id"] not in roster:
                            continue
                        if p["minutes"] is None:
                            unknown_ids.add(p["player_id"])
                        elif p["minutes"] > 0:
                            totals[p["player_id"]] = totals.get(p["player_id"], 0) + p["minutes"]
                            appearances.append({"player_id":p["player_id"], "match_id":row["match_id"], "minutes":p["minutes"], "status":row["status"]})
        eligible = bool(roster and complete and totals and not unknown_ids and not unknown_identity)
        own[tid] = {"minutes": sum(totals.values()) / len(totals) if eligible else None,
                    "count": len(totals) if eligible else None,
                    "fraction": len(totals) / len(roster) if eligible else None}
        reason = ("available_restricted_observed_appearances" if eligible else
                  "national_schedule_incomplete" if not sources["complete_schedule"] else
                  "national_fixture_details_missing" if missing_details else
                  "eligible_xi_missing" if not roster else
                  "national_player_identity_or_minutes_unknown" if unknown_ids or unknown_identity else
                  "no_observed_matching_appearance_not_zero_load")
        evidence[str(tid)] = {"status": reason, "xi_basis": "current_confirmed" if horizon == "LINEUP" else "immediately_previous_club_xi_proxy",
            "xi_match_id": roster_record["match_id"] if roster_record else None,
            "xi_denominator": len(roster) if roster else None, "xi_player_ids": sorted(roster) if roster else [],
            "observed_matching_player_ids": sorted(totals), "matched_player_count": len(totals),
            "unobserved_xi_player_ids": sorted(roster-set(totals)) if roster else [],
            "unknown_minutes_player_ids": sorted(unknown_ids), "unidentified_national_player_rows": unknown_identity,
            "observed_appearances": appearances,
            "unobserved_player_load": None, "absent_response_is_zero": False}
    def side(tid, oid):
        a, b = own[tid], own[oid]
        return dict(zip(FEATURE_NAMES, (a["minutes"], a["count"], a["fraction"], b["minutes"], b["count"], b["fraction"])))
    home, away = side(target["home_id"], target["away_id"]), side(target["away_id"], target["home_id"])
    return {"schema_version": SCHEMA, "scope": SCOPE, "scope_is_all_international_matches": False,
            "cutoff_at": _iso(cutoff), "horizon": horizon, "mode": mode, "lookback_days": lookback_days,
            "home": home, "away": away, "home_features": home, "away_features": away,
            "groups": {"restricted_observed_national_load": all(v is not None for d in (home,away) for v in d.values())},
            "feature_names": list(FEATURE_NAMES), "source_refs": sorted(refs.values(), key=lambda r:r["path"]),
            "source_issues": sources.get("issues", []), "diagnostic_refs": sources.get("diagnostic_refs", []),
            "evidence": evidence, "window_fixture_ids": [r["match_id"] for r in window],
            "missing_detail_fixture_ids": missing_details, "provider_schedule_queries_complete": sources["complete_schedule"],
            "historical_current_lineup_assumed_available": mode == "historical" and horizon == "LINEUP",
            "prospective_qualified": mode == "prospective" and all(v is not None for d in (home,away) for v in d.values()), "minutes_include_extra_time_when_status_AET_or_PEN": True,
            "numeric_scope": "observed_appearance_minutes_only;unobserved_not_zero;not_total_international_workload"}


def main(argv=None):
    parser = argparse.ArgumentParser(description="采集并保留限定国家队赛事出场分钟、分母和可恢复请求")
    parser.add_argument("--runtime", default="runtime")
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--national-league", default="unl")
    parser.add_argument("--reserve-requests", type=int, default=250)
    parser.add_argument("--max-seconds", type=float)
    parser.add_argument("--refresh", action="store_true")
    args = parser.parse_args(argv)
    code, report = collect_run(_read(args.config), args.runtime, args.national_league,
        reserve_requests=args.reserve_requests, max_seconds=args.max_seconds, refresh=args.refresh)
    print(json.dumps({"scope": report["scope"], "status": report["execution_status"],
        "details": report["detail_counts"], "pending_requests": len(report["pending_requests"]),
        "api_requests": report["api_requests"], "full_collection_completed": report["full_collection_completed"]}, ensure_ascii=False))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
