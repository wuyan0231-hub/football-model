"""Offline, time-ordered research datasets for small baseline corrections.

Only upstream-verified regulation-time final scores are accepted.  The four-hour
result delay is a historical availability *assumption*, not evidence that our API
had published a result at that time.  Current historical downloads never acquire
prospective qualification.  This module neither calls the API nor replaces the
production model, and its local coverage denominator is never supplier coverage.
"""
from __future__ import annotations

from collections import Counter
from datetime import timedelta
import hashlib
import json
import math
from pathlib import Path
from typing import Any

from .fundamentals import _validate_snapshot, make_snapshot
from .model import (ModelDataError, RESULT_AVAILABILITY_DELAY, UnsupportedTeamError,
                    _iso, _timestamp, _validated_matches, fit_model,
                    score_distribution)


DATASET_VERSION = "schedule-correction-replay-v1"
FEATURE_NAMES = ("rest_days_capped", "matches_last14d",
                 "opponent_rest_days_capped", "opponent_matches_last14d")
TARGET_SCOPE = "regulation_time_90_plus_stoppage"
REST_CAP_DAYS = 14.0


def _json(path: Path) -> Any:
    def invalid_constant(_):
        raise ValueError("non-finite JSON")
    return json.loads(path.read_text(encoding="utf-8"), parse_constant=invalid_constant)


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    allow_nan=False, separators=(",", ":")).encode()).hexdigest()


def _serial(match: dict) -> dict:
    return {key: value for key, value in match.items() if key != "_dt"}


def _workload_records(events) -> list[dict]:
    """Validate completed workload identities without inventing goal targets."""
    if not isinstance(events, (list, tuple)):
        raise ModelDataError("schedule_events must be a list")
    out, seen = [], set()
    for event in events:
        if not isinstance(event, dict):
            raise ModelDataError("schedule event must be an object")
        for key in ("match_id", "home_id", "away_id"):
            if type(event.get(key)) is not int or event[key] < 1:
                raise ModelDataError("schedule event identity must use positive integer IDs")
        if event["match_id"] in seen or event["home_id"] == event["away_id"]:
            raise ModelDataError("schedule event duplicate match/team identity")
        seen.add(event["match_id"])
        if event.get("status") not in ("FT", "AET", "PEN"):
            raise ModelDataError("workload event must be completed FT/AET/PEN")
        kickoff = _timestamp(event.get("kickoff"), "schedule event kickoff")
        out.append({"match_id": event["match_id"], "home_id": event["home_id"],
                    "away_id": event["away_id"], "kickoff": _iso(kickoff), "_dt": kickoff,
                    "status": event["status"], "score_target_used": False})
    return sorted(out, key=lambda m: (m["_dt"], m["match_id"]))


def _schedule_features(match: dict, cutoff, schedule: list[dict]) -> tuple[dict, dict, dict]:
    """Count only earlier completed results available by the forecast cutoff.

    Rest is kickoff-to-kickoff in days, capped at 14.  Density counts prior
    kickoffs in [target kickoff - 14 days, cutoff], after the availability gate.
    No prior observed match means unknown rest, not a zero-day break.
    """
    eligible = [m for m in schedule if m["_dt"] + RESULT_AVAILABILITY_DELAY <= cutoff]
    own = {}
    provenance = {}
    for team in (match["home_id"], match["away_id"]):
        prior = [m for m in eligible if team in (m["home_id"], m["away_id"])]
        previous = max(prior, key=lambda m: (m["_dt"], m["match_id"]), default=None)
        rest = (min(REST_CAP_DAYS, (match["_dt"] - previous["_dt"]).total_seconds() / 86400)
                if previous is not None else None)
        recent = [m for m in prior if m["_dt"] >= match["_dt"] - timedelta(days=14)]
        own[team] = {"rest_days_capped": rest, "matches_last14d": len(recent)}
        provenance[str(team)] = {
            "previous_match_id": previous["match_id"] if previous else None,
            "previous_kickoff": previous["kickoff"] if previous else None,
            "previous_result_assumed_available_at": _iso(previous["_dt"] + RESULT_AVAILABILITY_DELAY) if previous else None,
            "recent_match_ids": [m["match_id"] for m in recent],
            "rest_missing_reason": "no_prior_available_match_in_supplied_schedule" if previous is None else None,
        }
    def side(team, opponent):
        return {**own[team],
                "opponent_rest_days_capped": own[opponent]["rest_days_capped"],
                "opponent_matches_last14d": own[opponent]["matches_last14d"]}
    return side(match["home_id"], match["away_id"]), side(match["away_id"], match["home_id"]), provenance


def feature_values(fixture, schedule_matches, cutoff_at, *, schedule_events=None,
                   scope="same_competition") -> dict:
    """Public past-only feature builder shared by correction and joint models.

    ``fixture`` needs only match/team IDs and kickoff.  ``schedule_matches`` are
    verified 90-minute results. Optional finished workload events may have no
    90-minute goals, and are used only for rest/density.  ``scope`` describes the
    caller's actual source; it never certifies schedule completeness.
    """
    if not isinstance(fixture, dict):
        raise ModelDataError("fixture must be an object")
    for key in ("match_id", "home_id", "away_id"):
        if type(fixture.get(key)) is not int or fixture[key] < 1:
            raise ModelDataError("fixture identity must use positive integer IDs")
    if fixture["home_id"] == fixture["away_id"]:
        raise ModelDataError("fixture teams must differ")
    kickoff = _timestamp(fixture.get("kickoff"), "fixture kickoff")
    cutoff = _timestamp(cutoff_at, "cutoff_at")
    if cutoff >= kickoff:
        raise ModelDataError("feature cutoff must precede fixture kickoff")
    target = {**fixture, "kickoff": _iso(kickoff), "_dt": kickoff}
    history = _validated_matches(schedule_matches)
    events = _workload_records(schedule_events) if schedule_events is not None else []
    by_id = {m["match_id"]: m for m in events}
    for match in history:
        event = by_id.get(match["match_id"])
        if event and any(event[k] != match[k] for k in ("kickoff", "home_id", "away_id")):
            raise ModelDataError("workload event conflicts with score match identity")
        by_id[match["match_id"]] = match
    schedule = sorted(by_id.values(), key=lambda m: (m["_dt"], m["match_id"]))
    home, away, evidence = _schedule_features(target, cutoff, schedule)
    return {"home": home, "away": away, "scope": scope,
            "completeness": "unknown", "evidence": evidence,
            "feature_available_at": {name: _iso(cutoff) for name in FEATURE_NAMES},
            "availability_basis": "historical_result_delay_assumption",
            "prospective_qualified": False}


def build_replay(matches, *, start=None, end=None, horizon_hours=10,
                 block_days=30, min_train=180, half_life_days=180, ridge=3,
                 schedule_matches=None, schedule_events=None) -> dict:
    """Reconstruct historical early forecasts using periodic past-only fits.

    ``start``/``end`` are inclusive, timezone-bearing kickoff boundaries.  A
    block is anchored to the first requested prediction cutoff.  The unchanged
    baseline is fitted at that boundary and reused for at most ``block_days``;
    therefore its training cutoff never exceeds any prediction's cutoff.
    Minimum-history and unseen-team failures stay in the requested denominator.

    ``schedule_matches`` may add cross-competition *completed* matches for the
    same club.  Supplying such a list does not certify all-competition coverage:
    the returned scope explicitly retains unknown completeness.
    """
    for name, value in (("horizon_hours", horizon_hours), ("block_days", block_days),
                        ("half_life_days", half_life_days), ("ridge", ridge)):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            raise ModelDataError(f"{name} must be a finite positive number")
    if type(min_train) is not int or min_train < 2:
        raise ModelDataError("min_train must be an integer >= 2")
    records = _validated_matches(matches)
    lower = _timestamp(start, "start") if start is not None else None
    upper = _timestamp(end, "end") if end is not None else None
    if lower is not None and upper is not None and lower > upper:
        raise ModelDataError("start exceeds end")
    targets = [m for m in records if (lower is None or m["_dt"] >= lower)
               and (upper is None or m["_dt"] <= upper)]
    schedule = records if schedule_matches is None else _validated_matches(schedule_matches)
    schedule_scope = ("same_competition" if schedule_matches is None and schedule_events is None
                      else "supplied_schedule_completeness_unverified")
    # If a supplied schedule repeats a league match, its identity must agree.
    by_id = {m["match_id"]: m for m in schedule}
    for match in records:
        same = by_id.get(match["match_id"])
        if same is not None and any(same[k] != match[k] for k in
                                    ("kickoff", "home_id", "away_id", "home_goals", "away_goals", "neutral")):
            raise ModelDataError("supplied schedule conflicts with baseline match identity/result")
    # Preserve domestic history even when the caller supplies only extra games.
    schedule = sorted({**{m["match_id"]: m for m in records}, **by_id}.values(),
                      key=lambda m: (m["_dt"], m["match_id"]))
    if schedule_events is not None:
        events = {m["match_id"]: m for m in _workload_records(schedule_events)}
        for match in schedule:
            event = events.get(match["match_id"])
            if event and any(event[k] != match[k] for k in ("kickoff", "home_id", "away_id")):
                raise ModelDataError("workload event conflicts with score match identity")
            events[match["match_id"]] = match
        schedule = sorted(events.values(), key=lambda m: (m["_dt"], m["match_id"]))
    forecasts, failures, fits, rows = [], [], [], []
    cached_models = {}
    anchor = targets[0]["_dt"] - timedelta(hours=horizon_hours) if targets else None
    for match in targets:
        cutoff = match["_dt"] - timedelta(hours=horizon_hours)
        block = int((cutoff - anchor).total_seconds() // (block_days * 86400))
        training_cutoff = anchor + timedelta(days=block * block_days)
        if training_cutoff > cutoff:
            raise AssertionError("training cutoff exceeds prediction cutoff")
        training = [m for m in records if m["_dt"] + RESULT_AVAILABILITY_DELAY <= training_cutoff]
        entry = {"match_id": match["match_id"], "kickoff": match["kickoff"],
                 "cutoff_at": _iso(cutoff), "baseline_training_cutoff": _iso(training_cutoff),
                 "target_available_at": _iso(match["_dt"] + RESULT_AVAILABILITY_DELAY),
                 "home_id": match["home_id"], "away_id": match["away_id"],
                 "home_name": match["home_name"], "away_name": match["away_name"],
                 "home_goals": match["home_goals"], "away_goals": match["away_goals"],
                 "neutral": match["neutral"], "block": block,
                 "result_availability_basis": "historical_assumption_kickoff_plus_four_hours"}
        if block not in cached_models:
            fit_info = {"block": block, "training_cutoff": _iso(training_cutoff),
                        "training_count": len(training),
                        "training_match_ids": [m["match_id"] for m in training]}
            try:
                baseline = fit_model([_serial(m) for m in training], _iso(training_cutoff),
                                     half_life_days=half_life_days, ridge=ridge, min_train=min_train)
                fit_info.update(status="fitted", model=baseline)
                cached_models[block] = baseline
            except ModelDataError as exc:
                cached_models[block] = {"error": str(exc), "training_count": len(training)}
                fit_info.update(status="failed", reason=str(exc), failure_class=type(exc).__name__)
            fits.append(fit_info)
        baseline = cached_models[block]
        if "error" in baseline:
            failures.append({**entry, "stage": "baseline_fit", "reason": baseline["error"],
                             "code": "insufficient_training_history" if len(training) < min_train else "baseline_fit_failed"})
            continue
        try:
            prediction = score_distribution(baseline, match["home_id"], match["away_id"],
                                            match["kickoff"], match["neutral"])
        except ModelDataError as exc:
            failures.append({**entry, "stage": "baseline_prediction", "reason": str(exc),
                             "code": "unseen_team" if isinstance(exc, UnsupportedTeamError) else "baseline_prediction_failed"})
            continue
        home_features, away_features, feature_evidence = _schedule_features(match, cutoff, schedule)
        prediction.update(match_id=match["match_id"], cutoff_at=_iso(cutoff),
                          prospective_qualified=False, availability_scope="reconstructed_historical_score_availability")
        entry.update(status="forecast", baseline_prediction=prediction,
                     home_features=home_features, away_features=away_features,
                     feature_available_at={name: _iso(cutoff) for name in FEATURE_NAMES},
                     feature_evidence=feature_evidence, schedule_scope=schedule_scope,
                     schedule_completeness="unknown", prospective_qualified=False,
                     feature_complete=all(v is not None for v in home_features.values()))
        forecasts.append(entry)
        for side, features in (("home", home_features), ("away", away_features)):
            rows.append({"match_id": entry["match_id"], "side": side,
                         "team_id": entry[f"{side}_id"],
                         "opponent_id": entry["away_id" if side == "home" else "home_id"],
                         "kickoff": entry["kickoff"], "cutoff_at": entry["cutoff_at"],
                         "target_available_at": entry["target_available_at"],
                         "baseline_training_cutoff": entry["baseline_training_cutoff"],
                         "goals": entry[f"{side}_goals"],
                         "lambda0": prediction["expected_goals"][side],
                         "features": features, "feature_available_at": dict(entry["feature_available_at"]),
                         "feature_complete": entry["feature_complete"],
                         "schedule_scope": schedule_scope, "prospective_qualified": False})
    complete = sum(f["feature_complete"] for f in forecasts)
    return {
        "schema_version": DATASET_VERSION, "status": "historical_research_only",
        "target_scope": TARGET_SCOPE, "prospective_qualified": False,
        "production_adoption_approved": False,
        "data_sha256": _digest([_serial(m) for m in records]),
        "schedule_sha256": _digest([_serial(m) for m in schedule]),
        "schedule_scope": schedule_scope, "all_competition_completeness": "unknown",
        "settings": {"start": _iso(lower) if lower else None, "end": _iso(upper) if upper else None,
                     "horizon_hours": horizon_hours, "block_days": block_days,
                     "min_train": min_train, "half_life_days": half_life_days, "ridge": ridge},
        "availability": {"result_delay_hours": 4, "is_historical_assumption": True,
                         "actual_first_observed_at_known": False,
                         "feature_available_at_basis": "derivation_at_reconstructed_cutoff_not_api_snapshot"},
        "feature_names": list(FEATURE_NAMES),
        "feature_definitions": {
            "rest_days_capped": "days from previous eligible kickoff to target kickoff; capped14; missing=None",
            "matches_last14d": "eligible prior kickoffs >= target kickoff minus14days",
            "opponent_rest_days_capped": "same definition for opponent",
            "opponent_matches_last14d": "same definition for opponent"},
        "coverage": {"input_matches": len(records), "requested_matches": len(targets),
                     "baseline_forecasts": len(forecasts), "baseline_failures": len(failures),
                     "complete_feature_matches": complete,
                     "incomplete_feature_matches": len(forecasts) - complete,
                     "correction_rows": len(rows),
                     "failure_codes": dict(Counter(f["code"] for f in failures)),
                     "denominator_preserved": len(targets) == len(forecasts) + len(failures)},
        "forecasts": forecasts, "failures": failures, "baseline_fits": fits,
        "correction_rows": rows, "rows": rows,
    }


def coverage_report(runtime, league) -> dict:
    """Audit only this runtime's retained scores, API cache and snapshots.

    Fixture IDs in the normalized history define the denominator.  Requested
    seasons, empty cache responses and successful writes do not establish that
    complete official seasons or whole-club schedules were obtained.
    """
    runtime = Path(runtime)
    if not isinstance(league, str) or not league or Path(league).name != league or league in (".", ".."):
        raise ValueError("league must be a single competition key")
    path = runtime / "data" / f"{league}.json"
    payload = _json(path)
    if not isinstance(payload, dict) or not isinstance(payload.get("matches"), list):
        raise ModelDataError("history file must contain matches")
    records = _validated_matches(payload["matches"])
    targets = {m["match_id"]: m for m in records}
    observations = {kind: {} for kind in ("lineups", "statistics", "injuries", "players")}
    fixture_records, issues, cache_endpoints = {}, [], Counter()

    def retain(snapshot):
        identity = snapshot["fixture"]
        mid = identity["match_id"]
        if mid not in targets:
            return
        expected = targets[mid]
        if any(identity[k] != expected[k] for k in ("kickoff", "home_id", "away_id")):
            raise ValueError("source_identity_mismatch")
        kind = snapshot["kind"]
        if kind not in observations:
            return
        observations[kind].setdefault(mid, []).append(snapshot)

    snapshot_root = runtime / "fundamentals" / "snapshots"
    for source in sorted(snapshot_root.glob("*/*/*.json")):
        try:
            snapshot = _json(source)
            _validate_snapshot(snapshot)
            retain(snapshot)
        except (ValueError, KeyError, TypeError, OSError) as exc:
            issues.append({"path": source.relative_to(runtime).as_posix(), "reason": "invalid_snapshot",
                           "error_class": type(exc).__name__})
    cache_root = runtime / "cache" / "api_cache"
    for source in sorted(cache_root.glob("*.json")):
        try:
            envelope = _json(source)
            if not isinstance(envelope, dict):
                raise ValueError("cache must be object")
            endpoint = envelope.get("endpoint")
            cache_endpoints[str(endpoint)] += 1
            if endpoint not in ("/fixtures", "/fixtures/lineups", "/fixtures/statistics", "/injuries", "/fixtures/players"):
                continue
            if envelope.get("errors") or not isinstance(envelope.get("response"), list):
                raise ValueError("cache_response_invalid")
            if envelope.get("paging", {}).get("total", 1) != 1:
                raise ValueError("incomplete_cache_pagination")
            fetched = _timestamp(envelope.get("fetched_at"), "cache.fetched_at")
            if endpoint == "/fixtures":
                for raw in envelope["response"]:
                    mid = raw.get("fixture", {}).get("id")
                    if mid not in targets:
                        continue
                    expected = targets[mid]
                    if (raw["teams"]["home"]["id"] != expected["home_id"]
                            or raw["teams"]["away"]["id"] != expected["away_id"]
                            or _timestamp(raw["fixture"]["date"], "fixture.date") != expected["_dt"]):
                        raise ValueError("fixture_cache_identity_mismatch")
                    fixture_records[mid] = {"season": raw.get("league", {}).get("season"),
                                           "status": raw["fixture"].get("status", {}).get("short"),
                                           "fetched_at": _iso(fetched)}
                continue
            mid = envelope.get("params", {}).get("fixture")
            if mid not in targets:
                continue
            match = targets[mid]
            fixture = {**_serial(match), "status": "FT"}
            if endpoint == "/fixtures/players":
                teams = {}
                for raw in envelope["response"]:
                    tid = raw.get("team", {}).get("id")
                    if tid not in (match["home_id"], match["away_id"]) or tid in teams:
                        raise ValueError("player_cache_team_identity_invalid")
                    players = raw.get("players")
                    if not isinstance(players, list):
                        raise ValueError("player_cache_missing_players")
                    valid = [p for p in players if type(p.get("player", {}).get("id")) is int
                             and p["player"]["id"] > 0 and isinstance(p.get("statistics"), list) and p["statistics"]]
                    teams[str(tid)] = {"identified_players_with_statistics": len(valid),
                                       "reported_players": len(players)}
                observations["players"].setdefault(mid, []).append({"kind": "players", "fixture": fixture,
                    "known_at": _iso(fetched), "teams": teams})
            else:
                kind = {"/fixtures/lineups": "lineups", "/fixtures/statistics": "statistics", "/injuries": "injuries"}[endpoint]
                retain(make_snapshot(kind, envelope, fixture, context_observed_at=_iso(fetched)))
        except (ValueError, KeyError, TypeError, AttributeError, OSError) as exc:
            issues.append({"path": source.relative_to(runtime).as_posix(), "reason": "invalid_cache_evidence",
                           "error_class": type(exc).__name__})

    by_kind = {}
    for kind, matches_observed in observations.items():
        both_qualified, early, before_kickoff, after_kickoff = set(), set(), set(), set()
        nonempty_injuries, empty_injuries = set(), set()
        for mid, snapshots in matches_observed.items():
            match = targets[mid]
            for snapshot in snapshots:
                known = _timestamp(snapshot["known_at"], "snapshot.known_at")
                teams = [snapshot["teams"].get(str(tid), {}) for tid in (match["home_id"], match["away_id"])]
                if kind == "lineups":
                    complete = all(t.get("complete_starting_eleven") is True for t in teams)
                elif kind == "statistics":
                    complete = all(type(t.get("xg")) in (int, float) and math.isfinite(t["xg"]) and t["xg"] >= 0 for t in teams)
                elif kind == "players":
                    complete = all(t.get("identified_players_with_statistics", 0) > 0 for t in teams)
                else:
                    complete = False  # No injury endpoint certifies healthy complete rosters.
                    (nonempty_injuries if any(t.get("reported_count", 0) > 0 for t in teams) else empty_injuries).add(mid)
                if complete:
                    both_qualified.add(mid)
                    if known <= match["_dt"] - timedelta(hours=10):
                        early.add(mid)
                    if known < match["_dt"]:
                        before_kickoff.add(mid)
                    else:
                        after_kickoff.add(mid)
        by_kind[kind] = {
            "local_denominator": len(records), "matches_with_observations": len(matches_observed),
            "both_sides_qualified": len(both_qualified),
            "qualified_observed_by_T_minus_10h": len(early),
            "qualified_observed_before_kickoff": len(before_kickoff),
            "qualified_with_after_kickoff_observation": len(after_kickoff),
            "match_ids": sorted(matches_observed),
            "both_sides_qualified_match_ids": sorted(both_qualified)}
        if kind == "injuries":
            by_kind[kind].update(matches_with_reported_cases=len(nonempty_injuries),
                                 matches_with_empty_observations=len(empty_injuries),
                                 complete_roster_verified=0, empty_means_healthy=False)
    seasons = Counter(str(x["season"]) if x["season"] is not None else "unknown" for x in fixture_records.values())
    unknown_season = len(records) - len(fixture_records) + seasons.pop("unknown", 0)
    return {"schema_version": "feature-data-local-coverage-v1", "league": league,
            "status": "completed_local_sample_audit", "target_scope": TARGET_SCOPE,
            "coverage_scope": "retained_runtime_match_ids_only",
            "supplier_full_coverage_certified": False, "official_season_denominator": None,
            "all_competition_schedule_complete": False,
            "history": {"matches": len(records), "source_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                        "observed_at": payload.get("observed_at"),
                        "requested_seasons": payload.get("requested_seasons"),
                        "missing_seasons": payload.get("missing_seasons"),
                        "first_kickoff": records[0]["kickoff"] if records else None,
                        "last_kickoff": records[-1]["kickoff"] if records else None},
            "fixture_cache": {"matching_match_ids": len(fixture_records),
                              "observed_season_counts": dict(seasons), "season_unknown_matches": unknown_season},
            "fundamentals": by_kind, "cache_endpoint_file_counts": dict(cache_endpoints),
            "issues": issues,
            "notes": ["Counts describe available local evidence, not complete provider coverage.",
                      "Historical download times are never backdated to historical prediction cutoffs.",
                      "Observed lineups and player summaries do not establish panna event data or rating eligibility.",
                      "Requested seasons and empty injury responses do not certify completeness."]}
