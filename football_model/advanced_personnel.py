"""EARLY reported-absence features from actual archived pre-match evidence.

This module supplies inputs, never a hand-written probability adjustment.  Only
an independently trained correction model may turn these inputs into a lambda
change.  A later historical injury download is never a pre-match observation.
Reported absences are incomplete provider reports, not a healthy-roster census.
"""
from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from .competition_scope import require_league
from .advanced_features import (_identity, _select, canonical, load_sources, read_json,
                                source_ref, RESULT_DELAY)
from .data import DataError, _iso, _utc
from .fundamentals import _fixture, _validate_snapshot, TTL_SECONDS


FEATURE_GROUPS = {
    "personnel_early": (
        "reported_absence_recent_minutes_share", "reported_absence_previous_xi_share",
        "opponent_reported_absence_recent_minutes_share", "opponent_reported_absence_previous_xi_share"),
}
FEATURE_NAMES = FEATURE_GROUPS["personnel_early"]
ABSENT_TYPE = "Missing Fixture"


def load_personnel_sources(runtime, league=None, records=None):
    """Index immutable injury snapshots and their saved coverage/context evidence."""
    runtime = Path(runtime)
    snapshots, contexts, issues = {}, {}, []
    for path in sorted((runtime / "fundamentals" / "snapshots" / "injuries").glob("*/*.json")):
        try:
            value = read_json(path)
            _validate_snapshot(value)
            if value["kind"] != "injuries":
                raise DataError("not_injury_snapshot")
            snapshots.setdefault(value["fixture"]["match_id"], []).append((value, source_ref(path, runtime)))
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            issues.append({"path": str(path.relative_to(runtime)), "reason": "invalid_personnel_snapshot",
                           "source_ref": source_ref(path, runtime)})
    for path in sorted((runtime / "fundamentals" / "fixtures").glob("*/*.json")):
        try:
            value = read_json(path)
            env = value["fixture_response"]
            if (env.get("endpoint") != "/fixtures" or env.get("errors")
                    or env.get("paging", {}).get("total", 1) != 1
                    or env.get("paging", {}).get("current", 1) != 1
                    or len(env.get("response", [])) != 1):
                raise DataError("invalid_injury_fixture_context")
            raw = env["response"][0]
            identity = _fixture(raw)
            if (env.get("params") != {"id": identity["match_id"]}
                    or raw["league"]["id"] != value["league_id"]
                    or raw["league"]["season"] != value["season"]):
                raise DataError("injury_context_identity_mismatch")
            _utc(value["coverage_observed_at"])
            _utc(env["fetched_at"])
            contexts.setdefault(identity["match_id"], []).append((value, identity, source_ref(path, runtime)))
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            issues.append({"path": str(path.relative_to(runtime)), "reason": "invalid_personnel_context",
                           "source_ref": source_ref(path, runtime)})
    return {"snapshots": snapshots, "contexts": contexts, "issues": issues}


def _current_injuries(index, target, cutoff):
    mid = target["match_id"]
    # A damaged current original must not silently select a convenient older one.
    invalid = [issue for issue in index["issues"] if Path(issue["path"]).parent.name == str(mid)]
    invalid_injuries = [issue for issue in invalid if issue["reason"] == "invalid_personnel_snapshot"]
    if invalid_injuries:
        return None, [issue["source_ref"] for issue in invalid_injuries], "personnel_source_invalid"
    choices = [pair for pair in index["snapshots"].get(mid, [])
        if _identity(pair[0]["fixture"]) == target and pair[0]["fixture"]["status"] == "NS"
        and _utc(pair[0]["known_at"]) <= cutoff]
    if not choices:
        return None, [], "no_actual_prematch_injury_snapshot"
    latest_time = max(_utc(pair[0]["known_at"]) for pair in choices)
    latest = [pair for pair in choices if _utc(pair[0]["known_at"]) == latest_time]
    latest_refs = [ref for _, ref in latest]
    # Equal knowledge times provide no evidence to choose one conflicting
    # availability report. A digest's lexicographic order is not chronology.
    if len({canonical(pair[0]["teams"]) for pair in latest}) > 1:
        return None, latest_refs, "ambiguous_current_injury_snapshot"
    snapshot, ref = max(latest, key=lambda pair: pair[0]["snapshot_id"])
    if invalid:
        return None, latest_refs + [issue["source_ref"] for issue in invalid], "personnel_source_invalid"
    if cutoff - _utc(snapshot["observed_at"]) > timedelta(seconds=TTL_SECONDS["injuries"]):
        return None, latest_refs, "injury_snapshot_stale"
    contexts = [(value, context_ref) for value, identity, context_ref in index["contexts"].get(mid, [])
        if _identity(identity) == target and identity["status"] == "NS"
        and _utc(value["fixture_response"]["fetched_at"]) == _utc(snapshot["context_observed_at"])
        and _utc(value["coverage_observed_at"]) <= cutoff
        and _utc(value["fixture_response"]["fetched_at"]) <= cutoff]
    if len(contexts) != 1:
        return None, latest_refs + [item[1] for item in contexts], "injury_coverage_context_missing_or_ambiguous"
    context, context_ref = contexts[0]
    if context.get("coverage", {}).get("injuries") is not True:
        return None, latest_refs + [context_ref], "injury_coverage_not_confirmed"
    return snapshot, latest_refs + [context_ref], None


def personnel_features(runtime, league, fixture, cutoff_at, *, horizon="EARLY", mode="prospective",
                       recent_matches=5, min_history=3, records=None, sources=None, personnel_sources=None):
    """Return reproducible current reported-absence inputs in the common row shape.

    Historical mode may use later-downloaded *past* minutes as development
    evidence, but still requires injuries actually observed before this target's
    cutoff.  Empty reports, doubtful status, unknown coverage and incomplete
    past minutes are missing, not numerical absence of injuries.
    """
    require_league(league)
    if horizon not in ("EARLY", "LINEUP") or mode not in ("historical", "prospective"):
        raise DataError("invalid_personnel_mode_or_horizon")
    if (type(recent_matches) is not int or type(min_history) is not int
            or not 2 <= min_history <= recent_matches <= 20):
        raise DataError("invalid_history_thresholds")
    runtime, target, cutoff = Path(runtime), _identity(fixture), _utc(cutoff_at)
    if cutoff >= _utc(target["kickoff"]):
        raise DataError("personnel_cutoff_not_pre_match")
    sources = sources if sources is not None else load_sources(runtime, league, records)
    index = (personnel_sources if personnel_sources is not None else
             sources.get("personnel_sources") if sources.get("personnel_sources") is not None else
             load_personnel_sources(runtime))
    if mode == "prospective" and (sources.get("history_observed_at") is None
            or _utc(sources["history_observed_at"]) > cutoff):
        raise DataError("history_not_observed_by_prospective_cutoff")
    refs = {sources["history_source"]["path"]: sources["history_source"]}
    injury, injury_refs, reason = _current_injuries(index, target, cutoff)
    refs.update((ref["path"], ref) for ref in injury_refs)
    if horizon != "EARLY":
        reason = "not_applicable_confirmed_lineup_uses_actual_replacements"
    own, evidence, missing = {}, {}, []

    def retain(pair):
        if pair is None:
            return None
        snap, ref = pair
        refs[ref["path"]] = ref
        for dependency in sources.get("dependencies", {}).get(ref["path"], []):
            refs[dependency["path"]] = dependency
        return snap

    for tid in (target["home_id"], target["away_id"]):
        value = {"reported_absence_recent_minutes_share": None, "reported_absence_previous_xi_share": None}
        team_reason = reason
        reported = injury["teams"][str(tid)]["records"] if injury is not None else []
        absent = {row["player_id"] for row in reported if row.get("provider_type") == ABSENT_TYPE}
        unresolved = {row["player_id"] for row in reported if row.get("provider_type") != ABSENT_TYPE}
        if team_reason is None and not reported:
            team_reason = "empty_injury_report_not_verified_healthy"
        if team_reason is None and unresolved:
            team_reason = "unresolved_reported_availability"
        prior = [r for r in sources["history"] if r["match_id"] != target["match_id"]
            and tid in (r["home_id"], r["away_id"])
            and _utc(r["kickoff"]) + RESULT_DELAY <= cutoff][-recent_matches:]
        minutes, ratings, previous = {}, {}, None
        complete_ids, incomplete_ids = [], []
        if team_reason is None:
            for row in prior:
                snap = retain(_select(sources, "players", row, cutoff, mode))
                team = snap["teams"][str(tid)] if snap is not None else {}
                if team.get("complete_minutes"):
                    complete_ids.append(row["match_id"])
                    for player in team["players"]:
                        pid, played = player["player_id"], player["minutes"]
                        minutes[pid] = minutes.get(pid, 0.0) + played
                        if player.get("rating") is not None and played > 0:
                            weight, total = ratings.get(pid, (0.0, 0.0))
                            ratings[pid] = (weight + played, total + played * player["rating"])
                else:
                    incomplete_ids.append(row["match_id"])
            if prior:
                xi = retain(_select(sources, "lineups", prior[-1], cutoff, mode))
                if xi is not None and xi["teams"][str(tid)].get("complete_starting_eleven"):
                    previous = {p["player_id"] for p in xi["teams"][str(tid)]["starters"]}
            # Do not skip a recent missing game to make a regular player's
            # unknown participation look like a long-absorbed zero.
            if len(prior) < min_history or incomplete_ids:
                team_reason = "recent_player_minutes_incomplete"
            elif previous is None:
                team_reason = "immediately_previous_starting_eleven_missing"
            elif sum(minutes.values()) <= 0:
                team_reason = "nonpositive_recent_minutes_denominator"
            else:
                denominator = sum(minutes.values())
                value["reported_absence_recent_minutes_share"] = sum(minutes.get(pid, 0.0) for pid in absent) / denominator
                value["reported_absence_previous_xi_share"] = len(absent & previous) / 11
        own[tid] = value
        evidence[str(tid)] = {"reported_absent_ids": sorted(absent), "unresolved_player_ids": sorted(unresolved),
            "prior_match_ids": [row["match_id"] for row in prior], "complete_minutes_match_ids": complete_ids,
            "incomplete_minutes_match_ids": incomplete_ids,
            "previous_xi_ids": sorted(previous) if previous is not None else None,
            "reported_absent_in_previous_xi": sorted(absent & previous) if previous is not None else None,
            "reported_absent_not_in_previous_xi": sorted(absent - previous) if previous is not None else None,
            "reported_absent_recent_minutes": {str(pid): minutes.get(pid) for pid in sorted(absent)},
            "reported_absent_recent_rating": {str(pid): ratings[pid][1] / ratings[pid][0] if pid in ratings else None for pid in sorted(absent)},
            "historical_team_minutes": sum(minutes.values()) if complete_ids else None,
            "missing_reason": team_reason, "complete_roster_status": False,
            "interpretation": "reported_absences_relative_to_recent_actual_usage;not_injury_onset_or_replacement_ability",
            "long_absence_rule": "zero_only_if_all_recent_match_minutes_known_and_player_did_not_play;no_second_manual_penalty",
            "rating_scope": "diagnostic_provider_rating_of_past_minutes;not_a_probability_coefficient"}
        if team_reason:
            missing.append({"team_id": tid, "group": "personnel_early", "reason": team_reason})

    def side(tid, opponent):
        return {**own[tid], **{"opponent_" + key: value for key, value in own[opponent].items()}}
    home, away = side(target["home_id"], target["away_id"]), side(target["away_id"], target["home_id"])
    available = all(value is not None for row in (home, away) for value in row.values())
    return {"schema_version": "advanced-personnel-features-v1", **target, "cutoff_at": _iso(cutoff),
        "horizon": horizon, "mode": mode, "home": home, "away": away, "home_features": home, "away_features": away,
        "feature_groups": {key: list(names) for key, names in FEATURE_GROUPS.items()},
        "groups": {"personnel_early": available}, "source_refs": sorted(refs.values(), key=lambda r: r["path"]),
        "missing": missing, "evidence": evidence, "source_issues": index["issues"],
        "availability_basis": "actual_prematch_injury_archive;past_minutes_historical_assumption" if mode == "historical" else "all_sources_observed_by_cutoff",
        "historical_current_injuries_assumed_available": False, "prospective_qualified": mode == "prospective" and available,
        "feature_available_at": {name: _iso(cutoff) for name in FEATURE_NAMES},
        "scope": "provider_reported_absences_and_same_competition_recent_usage",
        "recent_matches": recent_matches, "min_history": min_history,
        "probability_adjustment": "none_here_requires_past_fitted_offset_coefficients"}
