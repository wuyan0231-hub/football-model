"""Independent PF-02 audit of already retained /fixtures originals.

This module does not supply research features, fit abilities, or repair provider
values.  In particular a null bench minute is never turned into zero, and event
times are never converted to a duration.  The original bytes and JSON pointers
remain the authority for every observation.  This audit alone cannot establish
a complete eligible roster, complete events, clinical severity, or a clean
historical pre-match observation.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path, PurePosixPath
import re


CONTRACT_VERSION = "personnel-exposure-contract-v1"
MODES = ("historical_development", "prospective_input", "outcome_label")
MAX_ORIGINAL_BYTES = 8 * 1024 * 1024


class PersonnelContractError(ValueError):
    """The source or its caller-supplied identity cannot be safely audited."""


def _positive_id(value):
    return type(value) is int and value > 0


def _time(value, field):
    try:
        if not isinstance(value, str):
            raise ValueError
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is None or result.utcoffset() is None:
            raise ValueError
        return result.astimezone(timezone.utc)
    except (ValueError, TypeError, OverflowError):
        raise PersonnelContractError("invalid_aware_time:" + field) from None


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise PersonnelContractError("duplicate_json_key:" + key)
        result[key] = value
    return result


def _float(value):
    parsed = float(value)
    if not math.isfinite(parsed):
        raise PersonnelContractError("nonfinite_json_number:" + value)
    return parsed


def _object(value):
    return value if isinstance(value, dict) else {}


def _id(value):
    return value if _positive_id(value) else None


def audit_fixture_context(original_bytes, *, expected_sha256, source_state_sha,
                          source_path, expected_fixture_id, cutoff_at,
                          mode="historical_development"):
    """Return all fixture player rows and separate, source-bound diagnostics.

    Structural/provenance failures raise ``PersonnelContractError``. Semantic
    row defects are retained with pointers and reasons, so damaged or unknown
    rows stay in the denominator. ``prospective_input`` concerns *past fixture*
    observations known by ``cutoff_at``; this is not EARLY XI prediction or
    trial qualification. Historical development may use later downloads but is
    always explicitly non-prospective. No API calls or file writes occur.
    """
    if mode not in MODES:
        raise PersonnelContractError("invalid_mode")
    if not isinstance(original_bytes, bytes) or not 0 < len(original_bytes) <= MAX_ORIGINAL_BYTES:
        raise PersonnelContractError("original_bytes_missing_or_over_limit")
    if not isinstance(expected_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        raise PersonnelContractError("invalid_expected_sha256")
    actual_sha256 = hashlib.sha256(original_bytes).hexdigest()
    if actual_sha256 != expected_sha256:
        raise PersonnelContractError("original_bytes_sha256_mismatch")
    if not isinstance(source_state_sha, str) or not re.fullmatch(r"[0-9a-f]{40}", source_state_sha):
        raise PersonnelContractError("invalid_fixed_state_sha")
    if (not isinstance(source_path, str) or not source_path
            or source_path.startswith("/") or "\\" in source_path
            or any(ord(c) < 32 for c in source_path)
            or any(part in ("", ".", "..") for part in source_path.split("/"))
            or str(PurePosixPath(source_path)) != source_path):
        raise PersonnelContractError("unsafe_source_path")
    if not _positive_id(expected_fixture_id):
        raise PersonnelContractError("invalid_expected_fixture_id")
    cutoff = _time(cutoff_at, "cutoff_at")
    try:
        envelope = json.loads(original_bytes.decode("utf-8"), object_pairs_hook=_pairs, parse_float=_float,
                              parse_constant=lambda value: (_ for _ in ()).throw(
                                  PersonnelContractError("nonfinite_json_number:" + value)))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        raise PersonnelContractError("invalid_json_original") from None
    if not isinstance(envelope, dict) or envelope.get("endpoint") != "/fixtures" or envelope.get("errors"):
        raise PersonnelContractError("invalid_fixture_envelope")
    paging = _object(envelope.get("paging"))
    if any(type(paging.get(key)) is not int or paging[key] != 1 for key in ("current", "total")):
        raise PersonnelContractError("fixture_pagination_not_complete")
    response = envelope.get("response")
    if not isinstance(response, list) or len(response) != 1 or not isinstance(response[0], dict):
        raise PersonnelContractError("fixture_response_not_unique")
    raw = response[0]
    fixture, teams, league = (_object(raw.get(key)) for key in ("fixture", "teams", "league"))
    mid = fixture.get("id")
    home, away = (_object(teams.get(side)).get("id") for side in ("home", "away"))
    if (mid != expected_fixture_id or type(mid) is not int or not _positive_id(home)
            or not _positive_id(away) or home == away):
        raise PersonnelContractError("fixture_identity_mismatch")
    if not _positive_id(league.get("id")) or not _positive_id(league.get("season")):
        raise PersonnelContractError("invalid_league_identity")
    params = envelope.get("params")
    if params not in ({"id": mid}, {"league": league["id"], "season": league["season"]}):
        raise PersonnelContractError("fixture_request_identity_mismatch")
    # Equality alone accepts bool as integer in Python; validate values too.
    if any(type(v) is not int for v in params.values()):
        raise PersonnelContractError("fixture_request_identity_mismatch")
    observed = _time(envelope.get("fetched_at"), "fetched_at")
    kickoff = _time(fixture.get("date"), "fixture.date")
    status = _object(fixture.get("status")).get("short")
    if not isinstance(status, str) or not status:
        raise PersonnelContractError("invalid_fixture_status")
    issues = []

    def issue(code, pointer, **details):
        issues.append({"reason": code, "source_pointer": pointer, **details})

    base = "/response/0"
    # Every original row remains listed. Invalid/duplicate source team rows
    # cannot be silently chosen as the convenient copy.
    def rows_for(kind):
        value = raw.get(kind)
        if not isinstance(value, list):
            issue(kind + "_missing_or_not_list", base + "/" + kind)
            return []
        return value

    lineup_rows, player_teams, event_rows = (rows_for(k) for k in ("lineups", "players", "events"))
    lineups = defaultdict(list)
    lineup_members = defaultdict(lambda: {"starters": [], "bench": []})
    all_lineup_rows = []
    for i, team in enumerate(lineup_rows):
        pointer = f"{base}/lineups/{i}"
        tid = _id(_object(_object(team).get("team")).get("id"))
        if tid not in (home, away):
            issue("lineup_team_identity_invalid", pointer)
        lineups[tid].append(i)
        for field, role in (("startXI", "starters"), ("substitutes", "bench")):
            people = _object(team).get(field)
            if not isinstance(people, list):
                issue("lineup_players_missing_or_not_list", pointer + "/" + field)
                continue
            for j, person in enumerate(people):
                pid = _id(_object(_object(person).get("player")).get("id"))
                pp = f"{pointer}/{field}/{j}"
                all_lineup_rows.append({"team_id": tid, "player_id": pid, "role": role,
                                        "source_pointer": pp, "raw_row": person})
                lineup_members[tid][role].append(pid)
                if pid is None:
                    issue("lineup_player_identity_unknown", pp)
    lineup_valid = {}
    for tid in (home, away):
        a, b = (lineup_members[tid][role] for role in ("starters", "bench"))
        known = [pid for pid in a + b if pid is not None]
        valid = len(lineups[tid]) == 1 and len(a) == 11 and None not in a and len(set(known)) == len(known)
        lineup_valid[tid] = valid
        if not valid:
            issue("lineup_identity_or_eleven_not_closed", base + "/lineups", team_id=tid)
    home_lineup = set(pid for role in lineup_members[home].values() for pid in role if pid is not None)
    away_lineup = set(pid for role in lineup_members[away].values() for pid in role if pid is not None)
    for pid in sorted(home_lineup & away_lineup):
        issue("player_listed_for_both_teams", base + "/lineups", player_id=pid)
        lineup_valid[home] = lineup_valid[away] = False

    known_membership = defaultdict(set)
    for tid, roles in lineup_members.items():
        for members in roles.values():
            for pid in members:
                if pid is not None:
                    known_membership[pid].add(tid)
    for team in player_teams:
        tid = _id(_object(_object(team).get("team")).get("id"))
        people = _object(team).get("players")
        for person in people if isinstance(people, list) else []:
            pid = _id(_object(_object(person).get("player")).get("id"))
            if pid is not None:
                known_membership[pid].add(tid)

    def event_signature(event):
        value = _object(event)
        # Display names and provider numbering are not event identity. Preserve
        # the whole original separately, but detect the same substitution when
        # only those presentation fields differ.
        return json.dumps({"team_id": _object(value.get("team")).get("id"),
                           "player_id": _object(value.get("player")).get("id"),
                           "assist_id": _object(value.get("assist")).get("id"),
                           "elapsed": _object(value.get("time")).get("elapsed"),
                           "extra": _object(value.get("time")).get("extra")},
                          sort_keys=True, separators=(",", ":"), allow_nan=False)

    event_counts = Counter(event_signature(e) for e in event_rows if _object(e).get("type") == "subst")
    incoming_counts, outgoing_counts = Counter(), Counter()
    for event in event_rows:
        value = _object(event)
        if value.get("type") == "subst":
            tid = _id(_object(value.get("team")).get("id"))
            in_id = _id(_object(value.get("assist")).get("id"))
            out_id = _id(_object(value.get("player")).get("id"))
            if in_id is not None:
                incoming_counts[(tid, in_id)] += 1
            if out_id is not None:
                outgoing_counts[(tid, out_id)] += 1
    events, incoming, outgoing = [], defaultdict(list), defaultdict(list)
    consistent_incoming, consistent_outgoing = defaultdict(list), defaultdict(list)
    for i, event in enumerate(event_rows):
        pointer = f"{base}/events/{i}"
        value = _object(event)
        tid = _id(_object(value.get("team")).get("id"))
        player = _id(_object(value.get("player")).get("id"))
        assist = _id(_object(value.get("assist")).get("id"))
        time = _object(value.get("time"))
        elapsed, extra = time.get("elapsed"), time.get("extra")
        time_valid = (type(elapsed) is int and 0 <= elapsed <= 200
                      and (extra is None or type(extra) is int and 0 <= extra <= 100))
        kind = value.get("type")
        substitution = kind == "subst"
        red_card = kind == "Card" and value.get("detail") in ("Red Card", "Second Yellow card")
        identity_valid = tid in (home, away) and player is not None and (not substitution or assist is not None)
        # Goal team IDs can describe the beneficiary (not the own-goal actor).
        # Only exposure events have the team-membership contract checked here.
        people_to_check = (((player, "player"), (assist, "assist")) if substitution else
                           ((player, "player"),) if red_card else ())
        for pid, label in people_to_check:
            if pid is not None and known_membership.get(pid) and known_membership[pid] != {tid}:
                identity_valid = False
                issue("event_" + label + "_team_conflict", pointer, player_id=pid)
        if substitution and player is not None and player == assist:
            identity_valid = False
            issue("substitution_same_incoming_and_outgoing_player", pointer)
        if substitution and event_counts[event_signature(event)] > 1:
            identity_valid = False
            issue("duplicate_substitution_observation", pointer)
        if substitution and assist is not None and incoming_counts[(tid, assist)] > 1:
            identity_valid = False
            issue("ambiguous_multiple_substitution_in_observations", pointer)
        if substitution and player is not None and outgoing_counts[(tid, player)] > 1:
            identity_valid = False
            issue("ambiguous_multiple_substitution_out_observations", pointer)
        if substitution:
            if not identity_valid:
                issue("substitution_identity_not_closed", pointer)
            if not time_valid:
                issue("substitution_time_unknown_or_invalid", pointer)
            if tid in (home, away) and player is not None:
                outgoing[(tid, player)].append(i)
            if tid in (home, away) and assist is not None:
                incoming[(tid, assist)].append(i)
            if identity_valid and time_valid:
                consistent_outgoing[(tid, player)].append(i)
                consistent_incoming[(tid, assist)].append(i)
        if red_card and (not identity_valid or not time_valid):
            issue("red_card_identity_or_time_not_closed", pointer)
        events.append({"source_pointer": pointer, "raw_row": event,
                       "reported_substitution": substitution, "reported_red_card": red_card,
                       "team_id": tid, "player_id": player, "assist_player_id": assist,
                       "reported_elapsed": elapsed, "reported_extra": extra,
                       "time_fields_valid": time_valid, "identity_fields_valid": identity_valid,
                       "identity_team_consistency_assessed": substitution or red_card,
                       "eligible_for_substitution_consistency_matching": substitution and identity_valid and time_valid,
                       "derived_duration": None})

    players, team_indices, pid_teams = [], defaultdict(list), defaultdict(set)
    for ti, team in enumerate(player_teams):
        pointer = f"{base}/players/{ti}"
        tid = _id(_object(_object(team).get("team")).get("id"))
        team_indices[tid].append(ti)
        if tid not in (home, away):
            issue("players_team_identity_invalid", pointer)
        people = _object(team).get("players")
        if not isinstance(people, list):
            issue("player_rows_missing_or_not_list", pointer + "/players")
            continue
        for pi, person in enumerate(people):
            pp = f"{pointer}/players/{pi}"
            obj = _object(person)
            raw_pid = _object(obj.get("player")).get("id")
            pid = _id(raw_pid)
            stats = obj.get("statistics")
            games = (_object(stats[0]).get("games") if isinstance(stats, list) and len(stats) == 1 else None)
            readable = isinstance(games, dict)
            games = _object(games)
            minutes = games.get("minutes")
            if not readable:
                minute_status, reason = "unextractable", "player_statistics_or_games_not_unique"
            elif "minutes" not in games:
                minute_status, reason = "absent", "minutes_field_absent"
            elif minutes is None:
                minute_status, reason = "null", "provider_reported_null_minutes"
            elif type(minutes) not in (int, float) or not 0 <= minutes <= 180 or not math.isfinite(minutes):
                minute_status, reason = "invalid", "reported_minutes_invalid"
            else:
                minute_status, reason = ("positive" if minutes > 0 else "zero"), None
            if pid is None:
                issue("player_identity_unknown_or_invalid", pp + "/player/id")
            else:
                pid_teams[pid].add(tid)
            if reason is not None:
                issue(reason, pp + "/statistics", team_id=tid, player_id=pid)
            starter_ids, bench_ids = (lineup_members[tid][role] for role in ("starters", "bench"))
            starter = pid in starter_ids if pid is not None and lineup_valid.get(tid) else None
            bench = pid in bench_ids if pid is not None and lineup_valid.get(tid) else None
            ins, outs = incoming[(tid, pid)] if pid is not None else [], outgoing[(tid, pid)] if pid is not None else []
            valid_ins = consistent_incoming[(tid, pid)] if pid is not None else []
            valid_outs = consistent_outgoing[(tid, pid)] if pid is not None else []
            if minute_status == "positive" and starter is not True and not valid_ins:
                issue("positive_minutes_without_matching_xi_or_substitution_in", pp, team_id=tid, player_id=pid)
            if minute_status in ("null", "absent", "unextractable") and ins:
                issue("unknown_minutes_with_substitution_in_observation", pp, team_id=tid, player_id=pid)
            raw_substitute = games.get("substitute")
            if starter is True and raw_substitute is True or bench is True and raw_substitute is False:
                issue("reported_substitute_conflicts_with_lineup", pp, team_id=tid, player_id=pid)
            players.append({"team_id": tid, "player_id": pid, "reported_player_id": raw_pid,
                "source_pointer": pp, "raw_row": person, "minutes_status": minute_status,
                "reported_minutes": minutes, "minutes_missing_reason": reason,
                "reported_position": games.get("position"), "reported_substitute": raw_substitute,
                "listed_starting_eleven": starter, "listed_bench": bench,
                "substitution_in_event_indices": list(ins), "substitution_out_event_indices": list(outs),
                "consistent_substitution_in_event_indices": list(valid_ins),
                "consistent_substitution_out_event_indices": list(valid_outs),
                "inferred_minutes": None, "complete_roster_membership": None})
    for pid, tids in sorted(pid_teams.items()):
        if len(tids) > 1:
            issue("player_minutes_reported_for_both_teams", base + "/players", player_id=pid)
    for tid in (home, away):
        if len(team_indices[tid]) != 1:
            issue("player_team_row_missing_or_duplicated", base + "/players", team_id=tid)
        pids = [p["player_id"] for p in players if p["team_id"] == tid and p["player_id"] is not None]
        for pid, count in sorted(Counter(pids).items()):
            if count > 1:
                issue("duplicate_player_minutes_rows", base + "/players", team_id=tid, player_id=pid, row_count=count)

    counts = Counter(p["minutes_status"] for p in players)
    time_reasons = []
    if observed > cutoff:
        time_reasons.append("original_observed_after_cutoff")
    if kickoff >= cutoff:
        time_reasons.append("fixture_not_past_at_cutoff")
    if status not in ("FT", "AET", "PEN"):
        time_reasons.append("fixture_not_final")
    elif observed < kickoff:
        time_reasons.append("final_fixture_observed_before_kickoff")
    scope = ("provider_reported_FT_regulation_including_stoppage" if status == "FT" else
             "provider_reported_may_include_extra_time" if status in ("AET", "PEN") else
             "provider_reported_in_progress_or_not_final")
    return {
        "schema_version": CONTRACT_VERSION,
        "implementation_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "source": {"fixed_state_sha": source_state_sha, "path": source_path,
                   "raw_sha256": actual_sha256, "raw_bytes": len(original_bytes),
                   "authority": "caller_supplied_fixed_state_binding;original_bytes_sha256_verified",
                   "git_tree_membership_independently_verified_here": False,
                   "endpoint": envelope["endpoint"], "params": params,
                   "observed_at": observed.isoformat(), "published_at": None},
        "fixture": {"match_id": mid, "home_id": home, "away_id": away,
                    "competition_id": league["id"], "season": league["season"],
                    "kickoff_at": kickoff.isoformat(), "status": status},
        "time_contract": {"mode": mode, "cutoff_at": cutoff.isoformat(),
            "known_by_cutoff": observed <= cutoff,
            "past_fixture_input_time_checks_pass": not time_reasons,
            "past_fixture_input_time_failure_reasons": time_reasons,
            "prospective_input_time_admissible": mode == "prospective_input" and not time_reasons,
            "clean_historical_prematch_claim": False,
            "formal_trial_qualification_assessed": False},
        "minutes_scope": scope,
        "membership_semantics": "exact_positive_id_appears_in_this_source_list_only;false_is_not_verified_absence_or_roster_ineligibility",
        "denominator": {"original_player_team_rows": len(player_teams),
            "original_player_rows": len(players), "retained_player_rows": len(players),
            "minutes_status_counts": {key: counts[key] for key in ("positive", "zero", "null", "absent", "invalid", "unextractable")},
            "original_lineup_team_rows": len(lineup_rows), "original_lineup_player_rows": len(all_lineup_rows),
            "original_event_rows": len(event_rows), "retained_event_rows": len(events),
            "eligible_roster_population": None, "complete_event_population_proven": False},
        "player_rows": players, "lineup_rows": all_lineup_rows, "event_rows": events,
        "issues": issues, "issue_counts": dict(sorted(Counter(i["reason"] for i in issues).items())),
        "limits": {"null_minutes_replaced_with_zero": False, "event_durations_inferred": False,
            "reentry_or_temporary_substitution_contract_supported": False,
            "team_minutes_normalized_to_990": False, "eligible_roster_inferred": False,
            "ability_or_replacement_quality_estimated": False, "injury_severity_estimated": False,
            "predicted_starting_eleven_estimated": False, "production_or_research_features_changed": False},
    }
