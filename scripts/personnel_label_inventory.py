"""Offline labels and restricted past-seen roster inventory. No model fitting/API.

The current-source reconstruction is an analytical development dataset, never a
claim that these later downloads existed at a historical prediction cutoff.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from football_model.advanced_features import load_sources, canonical, _identity, RESULT_DELAY
from football_model.data import _utc, _iso

VERSION = "personnel-label-inventory-v1"


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"),
        parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite_json")))


def safe_relative(value):
    if (not isinstance(value, str) or not value or "\\" in value
            or any(ord(c) < 32 for c in value)):
        raise ValueError("unsafe_source_path")
    path = Path(value)
    if path.is_absolute() or any(part in ("..", ".") for part in path.parts):
        raise ValueError("unsafe_source_path")
    return path


def materialize(report, source_runtime, output_runtime):
    """Read only exact supplied references; missing bytes remain explicit."""
    source_runtime, output_runtime = Path(source_runtime).resolve(), Path(output_runtime).resolve()
    if (source_runtime == output_runtime or source_runtime in output_runtime.parents
            or output_runtime in source_runtime.parents):
        raise ValueError("output_runtime_must_be_separate_from_source")
    manifest, by_original = [], {}
    for ref in report["source_refs"]:
        original = safe_relative(ref["original_path"])
        route = safe_relative(ref["path"])
        expected = ref["sha256"]
        if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
            raise ValueError("invalid_source_sha256")
        if str(original) in by_original and by_original[str(original)] != expected:
            raise ValueError("conflicting_original_path")
        by_original[str(original)] = expected
        # Sources outside the personnel/history dependency graph are inventoried
        # but need not be copied; no new data collection is implied.
        wanted = (str(original).startswith(("data/", "advanced_features/contexts/",
                  "advanced_features/evidence/", "advanced_features/snapshots/",
                  "fundamentals/fixtures/", "fundamentals/snapshots/")))
        item = {**ref, "materialized": False, "required_for_personnel_audit": wanted}
        if not wanted:
            item["status"] = "not_needed_by_this_inventory"
            manifest.append(item)
            continue
        source = source_runtime / route
        if not source.resolve().is_relative_to(source_runtime):
            raise ValueError("source_symlink_escapes_runtime")
        if not source.is_file():
            item["status"] = "missing_local_original"
            manifest.append(item)
            continue
        raw = source.read_bytes()
        if digest(raw) != expected:
            item["status"] = "source_hash_mismatch"
            manifest.append(item)
            continue
        destination = output_runtime / original
        if not destination.resolve().is_relative_to(output_runtime):
            raise ValueError("destination_symlink_escapes_output")
        destination.parent.mkdir(parents=True, exist_ok=True)
        # Copy, never hardlink: a later caller cannot mutate the shared original.
        destination.write_bytes(raw)
        item.update(status="verified_and_copied", materialized=True, bytes=len(raw))
        manifest.append(item)
    return manifest


def choose_observation(sources, kind, record, *, max_known_at=None):
    target = _identity(record)
    choices = [(snap, ref) for snap, ref in sources["snapshots"].get((kind, target["match_id"]), [])
        if _identity(snap["fixture"]) == target and snap["fixture"]["status"] == "FT"]
    known_times = sources.get("known_times", {})
    known = lambda pair: _utc(known_times.get(pair[1]["path"], pair[0]["known_at"]))
    if max_known_at is not None:
        choices = [pair for pair in choices if known(pair) <= _utc(max_known_at)]
    if not choices:
        return None, "no_valid_retained_" + kind + "_observation"
    latest = max(map(known, choices))
    same_time = [pair for pair in choices if known(pair) == latest]
    if len({canonical(pair[0]["teams"]) for pair in same_time}) > 1:
        return None, "conflicting_observations_at_same_knowledge_time"
    snap, ref = max(same_time, key=lambda pair: pair[0]["snapshot_id"])
    refs = [ref] + sources.get("dependencies", {}).get(ref["path"], [])
    return {"snapshot": snap, "source_refs": refs, "effective_known_at": _iso(latest)}, None


def membership(observation, kind, tid):
    teams = observation["snapshot"]["teams"]
    team = teams.get(str(tid), {})
    def listed_ids(row):
        people = (row.get("starters", []) + row.get("substitutes", [])
                  if kind == "lineups" else row.get("players", []))
        return {p["player_id"] for p in people if type(p.get("player_id")) is int and p["player_id"] > 0}
    own_ids = listed_ids(team)
    other_ids = set().union(*(listed_ids(row) for key, row in teams.items() if key != str(tid)))
    conflicts = own_ids & other_ids
    if kind == "lineups":
        people = team.get("starters", []) + team.get("substitutes", [])
        pids = {p["player_id"] for p in people if type(p.get("player_id")) is int and p["player_id"] > 0}
        meaning = "past_reported_xi_or_bench_membership_only"
    else:
        pids = {p["player_id"] for p in team.get("players", [])
            if type(p.get("player_id")) is int and p["player_id"] > 0
            and type(p.get("minutes")) in (int, float) and p["minutes"] > 0}
        meaning = "positive_provider_reported_past_minutes_only"
    return pids - conflicts, meaning, sorted(conflicts)


def origin(observation, kind, past, pids, meaning, conflicts):
    return {"match_id": past["match_id"], "kickoff": past["kickoff"], "kind": kind,
        "player_ids": sorted(pids), "meaning": meaning,
        "effective_known_at": observation["effective_known_at"], "source_refs": observation["source_refs"],
        "excluded_cross_team_identity_conflict_ids": conflicts}


def original_player_rows(observation, tid):
    """Retain raw scalar presence/null states, including unidentified rows."""
    if observation is None:
        return []
    rows = []
    for team_index, team in enumerate(observation["snapshot"]["raw_response"]):
        if team.get("team", {}).get("id") != tid:
            continue
        for index, person in enumerate(team.get("players", [])):
            stats = person.get("statistics", [])
            games = stats[0].get("games", {}) if len(stats) == 1 and isinstance(stats[0], dict) else {}
            rows.append({"source_pointer": f"/raw_response/{team_index}/players/{index}",
                "player_id": person.get("player", {}).get("id"),
                "minutes_field_present": "minutes" in games, "reported_minutes": games.get("minutes"),
                "rating_field_present": "rating" in games, "reported_rating": games.get("rating"),
                "reported_position": games.get("position"), "reported_substitute": games.get("substitute"),
                "inferred_minutes": None, "verified_nonappearance": False})
    return rows


def inventory(history, sources, *, history_observed_at, window_matches=5, horizon_hours=10):
    if type(window_matches) is not int or not 1 <= window_matches <= 20:
        raise ValueError("invalid_history_window")
    if type(horizon_hours) not in (int, float) or not 0 < horizon_hours <= 168:
        raise ValueError("invalid_horizon")
    history = sorted(history, key=lambda r: (_utc(r["kickoff"]), r["match_id"]))
    if len({r["match_id"] for r in history}) != len(history):
        raise ValueError("duplicate_history_match")
    observations, issues, matches = {}, [], []
    for record in history:
        mid = record["match_id"]
        kinds = {}
        for kind in ("lineups", "players"):
            observed, reason = choose_observation(sources, kind, record)
            if observed is None:
                issues.append({"match_id": mid, "kind": kind, "reason": reason})
            kinds[kind] = observed
        observations[mid] = kinds
    all_ids = set()
    team_rows = []
    for record in history:
        mid, kickoff = record["match_id"], _utc(record["kickoff"])
        cutoff = kickoff - timedelta(hours=horizon_hours)
        target_obs = observations[mid]["lineups"]
        target_teams = target_obs["snapshot"]["teams"] if target_obs else {}
        starter_sets = [{p["player_id"] for p in target_teams.get(str(tid), {}).get("starters", [])
            if type(p.get("player_id")) is int and p["player_id"] > 0}
            for tid in (record["home_id"], record["away_id"])]
        all_lineup_sets = [{p["player_id"] for field in ("starters", "substitutes")
            for p in target_teams.get(str(tid), {}).get(field, [])
            if type(p.get("player_id")) is int and p["player_id"] > 0}
            for tid in (record["home_id"], record["away_id"])]
        cross_team_identity_conflict = bool((starter_sets[0] & all_lineup_sets[1])
                                            | (starter_sets[1] & all_lineup_sets[0]))
        target_membership_conflicts = all_lineup_sets[0] & all_lineup_sets[1]
        result = {"match_id": mid, "kickoff": record["kickoff"],
            "home_id": record["home_id"], "away_id": record["away_id"],
            "home_goals": record.get("home_goals"), "away_goals": record.get("away_goals"),
            "prediction_cutoff_at": _iso(cutoff), "teams": {}}
        for tid in (record["home_id"], record["away_id"]):
            target = target_obs["snapshot"]["teams"].get(str(tid), {}) if target_obs else {}
            xi = target.get("starters", [])
            ids = [p.get("player_id") for p in xi]
            legal = (target.get("complete_starting_eleven") is True and len(ids) == 11
                     and all(type(pid) is int and pid > 0 for pid in ids) and len(set(ids)) == 11
                     and not cross_team_identity_conflict)
            actual = set(ids) if legal else None
            if actual is not None:
                all_ids.update(actual)
            prior = [r for r in history if r["match_id"] != mid
                and tid in (r["home_id"], r["away_id"])
                and _utc(r["kickoff"]) + RESULT_DELAY <= cutoff][-window_matches:]
            pool, strict_pool, origins, strict_origins = set(), set(), [], []
            missing_prior = []
            strict_history = (history_observed_at is not None and _utc(history_observed_at) <= cutoff)
            for past in prior:
                past_mid = past["match_id"]
                seen = False
                for kind in ("lineups", "players"):
                    obs = observations[past_mid][kind]
                    # Strict input selection is its own as-of query. A later
                    # revision must not hide an earlier genuinely saved source.
                    strict_obs, _ = (choose_observation(sources, kind, past, max_known_at=cutoff)
                                     if strict_history else (None, None))
                    if strict_obs is not None:
                        strict_ids, strict_meaning, strict_conflicts = membership(strict_obs, kind, tid)
                        if strict_ids:
                            strict_pool.update(strict_ids)
                            strict_origins.append(origin(strict_obs, kind, past, strict_ids, strict_meaning, strict_conflicts))
                    if obs is None:
                        missing_prior.append({"match_id": past_mid, "kind": kind,
                                              "reason": "retained_observation_unavailable"})
                        continue
                    pids, meaning, conflicts = membership(obs, kind, tid)
                    if conflicts:
                        missing_prior.append({"match_id": past_mid, "kind": kind,
                            "reason": "cross_team_player_identity_conflict_excluded",
                            "player_ids": conflicts, "source_refs": obs["source_refs"]})
                    if not pids:
                        continue
                    seen = True
                    item = origin(obs, kind, past, pids, meaning, conflicts)
                    pool.update(pids)
                    origins.append(item)
                if not seen:
                    missing_prior.append({"match_id": past_mid, "kind": "both",
                                          "reason": "no_qualified_prior_player_membership"})
            players_obs = observations[mid]["players"]
            players_team = players_obs["snapshot"]["teams"].get(str(tid), {}) if players_obs else {}
            player_rows = players_team.get("players", [])
            unavailable = []
            if not legal:
                unavailable.append("target_xi_missing_or_identity_not_closed")
            if cross_team_identity_conflict:
                    unavailable.append("starter_identity_also_listed_for_opposing_team")
            if not prior:
                unavailable.append("no_previous_match_in_retained_history")
            if not pool:
                unavailable.append("empty_past_seen_candidate_pool")
            if not strict_history:
                unavailable.append("history_schedule_not_actually_observed_by_cutoff")
            strict_missing_count = sum(_utc(o["effective_known_at"]) > cutoff for o in origins)
            row = {"team_id": tid, "target_xi_legal": legal,
                "target_xi_ids": sorted(actual) if actual is not None else None,
                "target_bench_ids": sorted(p["player_id"] for p in target.get("substitutes", [])
                    if type(p.get("player_id")) is int and p["player_id"] > 0
                    and p["player_id"] not in target_membership_conflicts) if target_obs else None,
                "target_reported_membership_conflict_ids": sorted(target_membership_conflicts),
                "target_unknown_bench_id_count": sum(p.get("player_id") is None for p in target.get("substitutes", [])) if target_obs else None,
                "label_source_refs": target_obs["source_refs"] if target_obs else [],
                "label_effective_known_at": target_obs["effective_known_at"] if target_obs else None,
                "label_provider_published_at": target_obs["snapshot"].get("published_at") if target_obs else None,
                "label_may_be_postmatch": True,
                "target_reported_starters": xi if target_obs else None,
                "prior_match_ids": [r["match_id"] for r in prior],
                "history_window_preserves_missing_matches": True,
                "historical_development_assumption": {
                    "candidate_ids": sorted(pool), "candidate_count": len(pool),
                    "positive_ids": sorted(pool & actual) if actual is not None else None,
                    "negative_ids": sorted(pool - actual) if actual is not None else None,
                    "outside_pool_starting_ids": sorted(actual - pool) if actual is not None else None,
                    "label_rows_available": legal and bool(pool), "candidate_origins": origins,
                    "historical_point_in_time_confirmed": False},
                "actual_observed_by_cutoff": {
                    "history_observed_by_cutoff": strict_history,
                    "candidate_ids": sorted(strict_pool), "candidate_count": len(strict_pool),
                    "positive_ids": sorted(strict_pool & actual) if actual is not None else None,
                    "negative_ids": sorted(strict_pool - actual) if actual is not None else None,
                    "outside_pool_starting_ids": sorted(actual - strict_pool) if actual is not None else None,
                    "label_rows_available": legal and bool(strict_pool) and strict_history,
                    "candidate_origins": strict_origins,
                    "late_observed_reconstruction_source_count": strict_missing_count},
                "target_player_statistics": {
                    "available": players_obs is not None,
                    "complete_reported_minutes": players_team.get("complete_minutes") if players_obs else None,
                    "positive_minutes_count": sum(p.get("minutes") is not None and p["minutes"] > 0 for p in player_rows),
                    "null_minutes_count": sum(p.get("minutes") is None for p in player_rows),
                    "reported_zero_minutes_count": sum(p.get("minutes") == 0 for p in player_rows),
                    "non_null_rating_count": sum(p.get("rating") is not None for p in player_rows),
                    "unidentified_row_count": len(players_team.get("unidentified_players", [])),
                    "raw_player_rows": original_player_rows(players_obs, tid),
                    "source_refs": players_obs["source_refs"] if players_obs else [],
                    "not_used_as_this_match_predictor": True},
                "missing_prior": missing_prior, "limitations": unavailable,
                "transfer_status": "unknown_not_inferred_from_absence_or_pool_membership",
                "pool_outside_reason": "first_seen_or_window_expired_or_source_missing_or_transfer_unknown",
                "negative_label_meaning": "not_starting_in_this_match_only_not_injury_health_or_eligibility"}
            result["teams"][str(tid)] = row
            team_rows.append(row)
        result["both_teams_legal_xi"] = all(r["target_xi_legal"] for r in result["teams"].values())
        result["both_teams_historical_labels_available"] = all(r["historical_development_assumption"]["label_rows_available"] for r in result["teams"].values())
        result["both_teams_actual_cutoff_labels_available"] = all(r["actual_observed_by_cutoff"]["label_rows_available"] for r in result["teams"].values())
        matches.append(result)
    historical = [r["historical_development_assumption"] for r in team_rows if r["target_xi_legal"]]
    return {"schema_version": VERSION, "configuration": {
        "window_matches": window_matches, "horizon_hours": horizon_hours,
        "past_result_availability_delay_hours": RESULT_DELAY.total_seconds() / 3600,
        "candidate_scope": "same_competition_past_fixed_window_xi_bench_or_positive_reported_minutes",
        "complete_registered_roster_claim": False, "transfer_status_inferred": False,
        "probabilities_trained": False, "null_minutes_filled_with_zero": False,
        "historical_inputs_are_reconstruction": True, "label_time_separate_from_input_time": True,
        "native_complete_minutes_is_not_verified_event_or_roster_completeness": True,
        "missing_retained_observation_is_not_a_supplier_unavailability_claim": True},
        "summary": {"requested_matches": len(history), "retained_matches": len(matches),
            "denominator_preserved": len(history) == len(matches), "team_match_opportunities": len(team_rows),
            "matches_both_legal_xi": sum(r["both_teams_legal_xi"] for r in matches),
            "team_matches_legal_xi": sum(r["target_xi_legal"] for r in team_rows),
            "unique_legal_xi_player_ids": len(all_ids),
            "matches_both_historical_labels_available": sum(r["both_teams_historical_labels_available"] for r in matches),
            "matches_both_actual_cutoff_labels_available": sum(r["both_teams_actual_cutoff_labels_available"] for r in matches),
            "historical_candidate_player_labels": sum(r["candidate_count"] for r in historical),
            "historical_pool_positive_labels": sum(len(r["positive_ids"]) for r in historical),
            "historical_pool_negative_labels": sum(len(r["negative_ids"]) for r in historical),
            "outside_pool_starting_labels": sum(len(r["outside_pool_starting_ids"]) for r in historical),
            "legal_xi_team_matches_with_outside_pool_starters": sum(bool(r["outside_pool_starting_ids"]) for r in historical),
            "historical_pool_covers_all_11_team_matches": sum(not r["outside_pool_starting_ids"] and bool(r["candidate_ids"]) for r in historical),
            "target_player_statistics_team_matches": sum(r["target_player_statistics"]["available"] for r in team_rows),
            "target_player_statistics_complete_minutes_team_matches": sum(r["target_player_statistics"]["complete_reported_minutes"] is True for r in team_rows),
            "reported_null_minutes_rows": sum(r["target_player_statistics"]["null_minutes_count"] for r in team_rows),
            "reported_zero_minutes_rows": sum(r["target_player_statistics"]["reported_zero_minutes_count"] for r in team_rows),
            "missing_observation_reasons": dict(sorted(Counter(r["reason"] for r in issues).items())),
            "native_source_issue_count": len(sources.get("source_issues", []))},
        "matches": matches, "observation_issues": issues,
        "native_source_issues": sources.get("source_issues", [])}


def run(source_report, source_runtime, output, state_sha, *, league="epl", window_matches=5, horizon_hours=10):
    if not isinstance(league, str) or not re.fullmatch(r"[a-z][a-z0-9_]*", league):
        raise ValueError("invalid_league_key")
    if not re.fullmatch(r"[0-9a-f]{40}", state_sha):
        raise ValueError("invalid_fixed_state_sha")
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    report_raw = Path(source_report).read_bytes()
    report = json.loads(report_raw)
    materialized = output / "materialized"
    # A new inventory is a fresh local reconstruction; stale files cannot fill a
    # source currently declared missing. Never delete any existing directory.
    if materialized.exists() or materialized.is_symlink():
        raise ValueError("materialized_output_exists_use_new_output_directory")
    refs = materialize(report, source_runtime, materialized)
    data = read_json(materialized / "data" / (league + ".json"))
    sources = load_sources(materialized, league)
    result = inventory(data["matches"], sources, history_observed_at=data.get("observed_at"),
        window_matches=window_matches, horizon_hours=horizon_hours)
    result["provenance"] = {"fixed_state_sha": state_sha, "fixed_state_binding": "caller_supplied_research_audit_binding",
        "source_report_sha256": digest(report_raw), "source_runtime": str(Path(source_runtime).resolve()),
        "history_observed_at": data.get("observed_at"), "source_commit": report.get("source_commit"),
        "research_implementation_fingerprint": report.get("implementation_fingerprint"),
        "inventory_script_sha256": digest(Path(__file__).read_bytes()),
        "executed_at": _iso(datetime.now(timezone.utc)),
        "source_refs_count": len(refs), "verified_materialized_sources": sum(r["materialized"] for r in refs),
        "missing_local_source_count": sum(r["status"] == "missing_local_original" for r in refs),
        "source_hash_mismatch_count": sum(r["status"] == "source_hash_mismatch" for r in refs),
        "paid_api_requests": 0, "fit_performed": False, "production_changed": False}
    for name, value in (("inventory.json", result), ("source-manifest.json", refs),
                        ("summary.json", {k: v for k, v in result.items() if k not in ("matches", "observation_issues")})):
        (output / name).write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-report", required=True)
    parser.add_argument("--source-runtime", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--state-sha", required=True)
    parser.add_argument("--league", default="epl")
    parser.add_argument("--window-matches", type=int, default=5)
    parser.add_argument("--horizon-hours", type=float, default=10)
    args = parser.parse_args(argv)
    result = run(args.source_report, args.source_runtime, args.output, args.state_sha,
        league=args.league, window_matches=args.window_matches, horizon_hours=args.horizon_hours)
    brief = {k: v for k, v in result["provenance"].items() if k != "research_implementation_fingerprint"}
    brief["research_implementation_fingerprint_sha256"] = result["provenance"]["research_implementation_fingerprint"].get("sha256")
    print(json.dumps({"summary": result["summary"], "provenance": brief}, ensure_ascii=False))
    return 0 if result["provenance"]["missing_local_source_count"] == 0 and result["provenance"]["source_hash_mismatch_count"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
