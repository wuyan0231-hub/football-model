"""Offline observed-sample admission audit. Never modifies production features.

Missing minutes are not zero; a bench listing is not proof of nonappearance.
All coverage denominators are supplied samples, never unobserved league games.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import timedelta
import hashlib
import json
import math
from pathlib import Path

from .data import DataError, _integer, _iso, _utc
from .fundamentals import make_snapshot, statistics_diagnostics
from .model import RESULT_AVAILABILITY_DELAY, _validated_matches

VERSION = "f4-observed-data-quality-v1"


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _read(path):
    raw = Path(path).read_bytes()
    value = json.loads(raw, parse_constant=lambda _: (_ for _ in ()).throw(DataError("nonfinite JSON")))
    return value, {"path": str(Path(path).resolve()), "sha256": _sha(raw), "bytes": len(raw)}


def minute_record(raw, *, minute_present=True, started=False, substitution_event=False,
                  final_status=None, identified=True):
    """Preserve provider minutes and keep observation/appearance/scope separate.

    No event is NOT evidence of no appearance. A substitute can appear for less
    than a reported minute, so zero plus a substitution event is retained. FT
    values over 90 are not clipped: added-time conventions remain unverified.
    """
    numeric = type(raw) in (int, float) and math.isfinite(raw) and raw >= 0 and float(raw).is_integer()
    state = ("absent" if not minute_present else "null" if raw is None else
             "reported_zero" if numeric and raw == 0 else "reported_positive" if numeric else "invalid")
    finished = final_status in ("FT", "AET", "PEN")
    evidence = []
    if finished:
        if numeric and raw > 0:
            evidence.append("positive_reported_minutes")
        if started:
            evidence.append("starting_eleven")
        if substitution_event:
            evidence.append("substitution_event_participant")
    appearance = "appeared_supported" if evidence else "unknown"
    if not identified:
        reason = "player_identity_missing"
    elif final_status != "FT":
        reason = "not_verified_finished_regulation_fixture"
    elif state in ("absent", "null", "invalid"):
        reason = "minutes_" + state
    elif raw == 0:
        reason = "zero_reported_not_precise_exposure"
    elif raw > 90:
        reason = "over_90_provider_convention_unverified"
    else:
        reason = None
    return {"raw_minutes": raw, "minute_field_present": minute_present, "minute_state": state,
            "appearance_status": appearance, "appearance_evidence": evidence,
            "verified_nonappearance": False,
            "reported_minutes_candidate": int(raw) if reason is None else None,
            "minute_feature_exclusion": reason,
            "exact_90_minute_exposure_verified": False,
            "used_in_probabilities": False}


def _identity(raw):
    try:
        f, t, league = raw["fixture"], raw["teams"], raw["league"]
        out = {"match_id": _integer(f["id"], "match_id", 1),
               "home_id": _integer(t["home"]["id"], "home_id", 1),
               "away_id": _integer(t["away"]["id"], "away_id", 1),
               "kickoff": _iso(_utc(f["date"])), "status": f["status"]["short"],
               "league_id": _integer(league["id"], "league_id", 1),
               "season": _integer(league["season"], "season", 1900)}
        if out["home_id"] == out["away_id"] or not isinstance(out["status"], str):
            raise DataError("invalid fixture identity")
        return out
    except (KeyError, TypeError, AttributeError):
        raise DataError("invalid fixture identity") from None


def inspect_fixture(raw, observed_at, source):
    """Audit one full /fixtures row; all calculations retain the true fetch time."""
    identity = _identity(raw)
    observed = _iso(_utc(observed_at))
    if identity["status"] in ("FT", "AET", "PEN") and _utc(observed) < _utc(identity["kickoff"]):
        raise DataError("finished status observed before kickoff")
    teams, players, issues = [], [], []
    lineup_teams = {}
    if "lineups" in raw:
        try:
            snapshot = make_snapshot("lineups", {"endpoint": "/fixtures/lineups", "params": {"fixture": identity["match_id"]},
                "response": raw["lineups"], "fetched_at": observed}, identity)
            lineup_teams = snapshot["teams"]
        except (ValueError, TypeError, KeyError, AttributeError):
            issues.append({"code": "invalid_lineup_module", "action": "exclude_lineup_features_keep_other_modules"})
    raw_stats = raw.get("statistics", [])
    try:
        stats_snapshot = make_snapshot("statistics", {"endpoint": "/fixtures/statistics", "params": {"fixture": identity["match_id"]},
            "response": raw_stats, "fetched_at": observed}, identity)
    except (ValueError, TypeError, KeyError, AttributeError):
        issues.append({"code": "invalid_statistics_module", "action": "exclude_statistics_features_keep_other_modules"})
        stats_snapshot = {"teams": {str(tid): {"xg": None, "missing_reason": "invalid_statistics_module"}
                                   for tid in (identity["home_id"], identity["away_id"])}}
    # Existing checks retain original values; this module does not invent repairs.
    issues.extend(statistics_diagnostics(raw_stats))
    blocks = raw.get("players", [])
    if not isinstance(blocks, list):
        issues.append({"code": "invalid_players_module", "action": "exclude_player_features_keep_other_modules"})
        blocks = []
    block_map, invalid_player_teams = {}, set()
    for block in blocks:
        team_object = block.get("team") if isinstance(block, dict) else None
        tid = team_object.get("id") if isinstance(team_object, dict) else None
        if type(tid) is not int or tid not in (identity["home_id"], identity["away_id"]):
            issues.append({"code": "invalid_player_team_identity", "action": "exclude_player_team_block_keep_other_modules"})
            continue
        if tid in block_map or tid in invalid_player_teams:
            issues.append({"team_id": tid, "code": "duplicate_player_team_block", "action": "exclude_all_duplicate_team_player_blocks"})
            invalid_player_teams.add(tid)
            block_map.pop(tid, None)
            continue
        if not isinstance(block.get("players"), list):
            issues.append({"team_id": tid, "code": "invalid_player_rows", "action": "exclude_player_team_block_keep_other_modules"})
            invalid_player_teams.add(tid)
            continue
        block_map[tid] = block["players"]
    events = raw.get("events", [])
    if not isinstance(events, list):
        issues.append({"code": "invalid_events_module", "action": "exclude_event_appearance_evidence"})
        events = []
    valid_events = []
    for event in events:
        if not isinstance(event, dict) or not isinstance(event.get("team"), dict):
            issues.append({"code": "invalid_event_row", "action": "exclude_event_appearance_evidence"})
            continue
        valid_events.append(event)
    for tid in (identity["home_id"], identity["away_id"]):
        lineup = lineup_teams.get(str(tid), {})
        starters = {p["player_id"] for p in lineup.get("starters", [])}
        subs = lineup.get("substitutes", [])
        participants = set()
        for event in valid_events:
            if event.get("type") == "subst" and event.get("team", {}).get("id") == tid:
                for key in ("player", "assist"):
                    participant = event.get(key)
                    pid = participant.get("id") if isinstance(participant, dict) else None
                    if type(pid) is int and pid > 0:
                        participants.add(pid)
        seen = set()
        for position, player in enumerate(block_map.get(tid, [])):
            if not isinstance(player, dict):
                issues.append({"team_id": tid, "source_player_index": position, "code": "invalid_player_row",
                               "action": "exclude_player_row_keep_other_modules"})
                continue
            player_object = player.get("player")
            raw_pid = player_object.get("id") if isinstance(player_object, dict) else None
            pid = raw_pid if type(raw_pid) is int and raw_pid > 0 else None
            if raw_pid is not None and pid is None:
                issues.append({"team_id": tid, "source_player_index": position, "code": "invalid_statistic_player_identity",
                               "action": "preserve_raw_id_exclude_player_identity_features"})
            if pid is not None:
                if pid in seen:
                    # Both duplicate rows remain visible; neither gains feature
                    # eligibility because arbitrary row order is not evidence.
                    for earlier in players:
                        if earlier["team_id"] == tid and earlier["player_id"] == pid:
                            earlier["reported_minutes_candidate"] = None
                            earlier["minute_feature_exclusion"] = "duplicate_player_statistic_identity"
                    issues.append({"team_id": tid, "player_id": pid, "code": "duplicate_player_statistic_identity",
                                   "action": "exclude_duplicate_player_minutes"})
                duplicate = pid in seen
                seen.add(pid)
            else:
                duplicate = False
            statistics = player.get("statistics")
            if not isinstance(statistics, list) or len(statistics) != 1 or not isinstance(statistics[0], dict):
                issues.append({"team_id": tid, "player_id": pid, "code": "ambiguous_player_statistic_blocks",
                               "action": "exclude_player_minute_feature"})
                games = {}
            else:
                games = statistics[0].get("games", {})
                if not isinstance(games, dict):
                    games = {}
            clean = minute_record(games.get("minutes"), minute_present="minutes" in games,
                started=pid is not None and pid in starters,
                substitution_event=pid is not None and pid in participants,
                final_status=identity["status"], identified=pid is not None)
            if duplicate:
                clean.update(reported_minutes_candidate=None, minute_feature_exclusion="duplicate_player_statistic_identity")
            players.append({"team_id": tid, "player_id": pid, "raw_player_id": raw_pid,
                            "source_player_index": position, **clean})
        xg = stats_snapshot["teams"][str(tid)]
        eligible = identity["status"] == "FT"
        team_players = [p for p in players if p["team_id"] == tid]
        usable_minutes = any(p["reported_minutes_candidate"] is not None for p in team_players)
        present_minutes = any(p["minute_state"] in ("reported_zero", "reported_positive") for p in team_players)
        present_identity = any(p["player_id"] is not None for p in team_players)
        fields = {
            "xg": {"present": xg["xg"] is not None, "missing_reason": xg["missing_reason"],
                   "candidate_usable": xg["xg"] is not None and eligible},
            "starting_eleven": {"present": bool(lineup.get("complete_starting_eleven")),
                "missing_reason": lineup.get("missing_reason", "module_absent"),
                "candidate_usable": bool(lineup.get("complete_starting_eleven")) and eligible},
            "player_statistics": {"present": bool(team_players),
                "missing_reason": None if team_players else "empty_or_absent_player_rows",
                "candidate_usable": usable_minutes, "candidate_field": "reported_player_minutes_only"},
            "player_minutes": {"present": present_minutes,
                "missing_reason": None if present_minutes else "no_numeric_minute_values",
                "candidate_usable": usable_minutes},
            "player_identity": {"present": present_identity,
                "missing_reason": None if present_identity else "no_valid_player_id",
                "candidate_usable": present_identity and eligible},
        }
        teams.append({"team_id": tid, "fields": fields,
            "xg_value": xg["xg"], "unknown_substitute_count": sum(p["player_id"] is None for p in subs),
            "minute_states": dict(Counter(p["minute_state"] for p in team_players)),
            "minute_feature_exclusions": dict(Counter(p["minute_feature_exclusion"] for p in team_players if p["minute_feature_exclusion"])),
            "regulation_statistics_eligible": eligible})
    return {**identity, "observed_at": observed, "source": source, "teams": teams, "players": players,
            "quality_findings": issues, "scope": "observed_fixture_only",
            "historical_prematch_availability_verified": False}


def summarize_samples(records, *, as_of):
    """Latest observed version per fixture; conflicts at one timestamp fail closed."""
    cutoff, selected, excluded = _utc(as_of), {}, Counter()
    for item in records:
        observed = _utc(item["observed_at"])
        if observed > cutoff:
            excluded["observed_after_cutoff"] += 1
            continue
        key = item["match_id"]
        old = selected.get(key)
        if old:
            for identity_key in ("home_id", "away_id", "league_id", "season"):
                if old[identity_key] != item[identity_key]:
                    raise DataError("fixture identity conflict across observations")
            if _utc(old["observed_at"]) == observed:
                a, b = [{k: v for k, v in rec.items() if k != "source"} for rec in (old, item)]
                if a != b:
                    raise DataError("same-time fixture observations conflict")
            excluded["superseded_or_duplicate_observation"] += 1
        if not old or observed > _utc(old["observed_at"]):
            selected[key] = item
    latest = sorted(selected.values(), key=lambda x: (x["league_id"], x["season"], x["match_id"]))
    groups = defaultdict(list)
    for item in latest:
        for team in item["teams"]:
            groups[(item["league_id"], item["season"], None)].append((item, team))
            groups[(item["league_id"], item["season"], team["team_id"])].append((item, team))
    coverage = []
    for (league, season, tid), cells in sorted(groups.items(), key=lambda x: tuple(-1 if z is None else z for z in x[0])):
        fields = {}
        for field in ("xg", "starting_eleven", "player_statistics", "player_minutes", "player_identity"):
            observed_count = sum(t["fields"][field]["present"] for _, t in cells)
            fields[field] = {"observed_team_fixtures": len(cells), "present": observed_count,
                "missing": len(cells)-observed_count,
                "observed_presence_fraction": observed_count/len(cells),
                "candidate_usable": sum(t["fields"][field]["candidate_usable"] for _, t in cells),
                "missing_reasons": dict(Counter(t["fields"][field]["missing_reason"] for _, t in cells if not t["fields"][field]["present"]))}
        coverage.append({"league_id": league, "season": season, "team_id": tid,
                         "distinct_observed_fixtures": len({i["match_id"] for i, _ in cells}), "fields": fields})
    return {"as_of": _iso(cutoff), "raw_observations": len(records), "selected_fixtures": len(latest),
        "excluded_observations": dict(excluded), "coverage_denominator": "supplied_observed_team_fixtures_only",
        "continuous_league_coverage_verified": False, "selection_bias_possible": True,
        "field_coverage": coverage, "fixtures": latest,
        "minute_states": dict(Counter(p["minute_state"] for r in latest for p in r["players"])),
        "minute_exclusions": dict(Counter(p["minute_feature_exclusion"] for r in latest for p in r["players"] if p["minute_feature_exclusion"])),
        "fundamentals_used_in_probabilities": False}


def training_recency(payload, *, as_of, source, samples=None):
    """Describe supplied model-training inputs by team, never infer feed freshness.

    The league-wide latest result can hide stale teams. Four hours is a research
    availability convention; kickoff alone is not authenticated result receipt.
    """
    if not isinstance(payload, dict) or not isinstance(payload.get("matches"), list):
        raise DataError("training source requires matches object")
    cutoff = _utc(as_of)
    all_rows = _validated_matches(payload["matches"])
    rows = [r for r in all_rows if r["_dt"] + RESULT_AVAILABILITY_DELAY <= cutoff]
    grouped = defaultdict(list)
    process = {r["match_id"]: r for r in samples or []}
    sample_overlap = []
    for row in rows:
        sample = process.get(row["match_id"])
        if sample:
            if (row["home_id"], row["away_id"], row["kickoff"]) != (sample["home_id"], sample["away_id"], sample["kickoff"]):
                raise DataError("training/process fixture identity mismatch")
            sample_overlap.append(sample)
        for tid in (row["home_id"], row["away_id"]):
            grouped[tid].append(row)
    teams = []
    for tid, matches in sorted(grouped.items()):
        latest = max(m["_dt"] for m in matches)
        counts = {str(days): sum(m["_dt"] >= cutoff - timedelta(days=days) for m in matches) for days in (30, 90, 180, 365)}
        observed_samples = [process[m["match_id"]] for m in matches if m["match_id"] in process]
        usable_xg_ids = {s["match_id"] for s in observed_samples
                         if any(t["team_id"] == tid and t["fields"]["xg"]["candidate_usable"] for t in s["teams"])}
        teams.append({"team_id": tid, "matches": len(matches), "latest_match_kickoff": _iso(latest),
            "days_since_latest_match": (cutoff-latest).total_seconds()/86400,
            "matches_in_trailing_days": counts, "recent_match_ids": [m["match_id"] for m in matches[-5:]],
            "no_observed_match_in_90_days": counts["90"] == 0,
            "supplied_training_matches_with_process_sample": len(observed_samples),
            "supplied_training_matches_with_usable_xg": len(usable_xg_ids),
            "latest_5_training_matches_with_usable_xg": sum(m["match_id"] in usable_xg_ids for m in matches[-5:]),
            "all_competition_workload_complete": False})
    observed = payload.get("observed_at")
    observed_known = None if observed is None else _iso(_utc(observed))
    return {"source": source, "as_of": _iso(cutoff), "input_matches": len(all_rows), "eligible_matches": len(rows),
        "excluded_not_mature": len(all_rows)-len(rows), "supplied_data_observed_at": observed_known,
        "data_observed_after_as_of": None if observed is None else _utc(observed)>cutoff,
        "latest_observed_match_kickoff": max((t["latest_match_kickoff"] for t in teams), default=None),
        "team_count": len(teams), "teams_without_match_90_days": sum(t["no_observed_match_in_90_days"] for t in teams),
        "teams": teams, "feed_freshness_verified": False, "full_fixture_population_verified": False,
        "process_sample_overlap": {"training_matches": len(rows), "sampled_training_matches": len(sample_overlap),
            "training_matches_with_both_xg": sum(all(t["fields"]["xg"]["candidate_usable"] for t in s["teams"]) for s in sample_overlap),
            "denominator": "supplied_training_file_only", "continuous_coverage_verified": False},
        "requested_seasons": payload.get("requested_seasons"), "missing_seasons": payload.get("missing_seasons"),
        "point_in_time_sources_verified": False, "scope": "supplied_training_file_event_time_diagnostic",
        "caution": "Inactive or relegated teams can be old without a stale feed; latest file/update time does not prove team recency."}


def audit_saved(*, as_of, fundamentals_dir=None, probe_dir=None, data_dir=None):
    records, sources, rejected = [], [], []
    def append_rows(rows, observed, source):
        for raw in rows:
            try:
                records.append(inspect_fixture(raw, observed, source))
            except (ValueError, TypeError, KeyError, AttributeError) as exc:
                rejected.append({"source": source, "match_id": raw.get("fixture", {}).get("id") if isinstance(raw, dict) else None,
                                 "reason": str(exc), "action": "quarantine_fixture_audit"})
    if fundamentals_dir:
        for path in sorted((Path(fundamentals_dir)/"fixtures").glob("*/*.json")):
            value, source = _read(path)
            if path.stem != source["sha256"]:
                raise DataError("saved fixture content address mismatch")
            sources.append(source)
            env = value.get("fixture_response", {})
            rows = env.get("response")
            if env.get("errors") or env.get("endpoint") != "/fixtures" or not isinstance(rows, list) or len(rows) != 1:
                raise DataError("invalid saved fixture envelope")
            if env.get("paging", {}).get("total", 1) != 1 or env.get("params") != {"id": rows[0]["fixture"]["id"]}:
                raise DataError("saved fixture request or paging mismatch")
            if rows[0]["league"]["id"] != value.get("league_id") or rows[0]["league"]["season"] != value.get("season"):
                raise DataError("saved fixture league/season mismatch")
            append_rows(rows, env["fetched_at"], source)
    if probe_dir:
        root = Path(probe_dir)
        manifest, src = _read(root/"transport_manifest.json")
        sources.append(src)
        attempts = [a for a in manifest["attempts"] if a.get("source_path") == "responses/006_fixtures.json"]
        if len(attempts) != 1:
            raise DataError("probe fixture capture not unique")
        attempt = attempts[0]
        value, source = _read(root/attempt["source_path"])
        if source["sha256"] != attempt["saved_body_sha256"]:
            raise DataError("probe saved body fingerprint mismatch")
        if value.get("errors") or value.get("paging", {}).get("total", 1) != 1 or not isinstance(value.get("response"), list):
            raise DataError("invalid probe fixture response")
        sources.append(source)
        append_rows(value["response"], attempt["received_at"], source)
    sample_audit = summarize_samples(records, as_of=as_of)
    training = []
    if data_dir:
        for path in sorted(Path(data_dir).glob("*.json")):
            payload, source = _read(path)
            sources.append(source)
            training.append({"league_key": path.stem, "competition_role": "audit_only" if path.stem == "ucl" else "configured_score_model",
                             **training_recency(payload, as_of=as_of, source=source, samples=sample_audit["fixtures"])})
    return {"schema_version": VERSION, "research_only": True, "production_changed": False,
        "as_of": _iso(_utc(as_of)), "source_files": sources, "rejected_fixtures": rejected,
        "sample_audit": sample_audit, "training_recency": training,
        "improvement_proven": False, "candidate_admission": {
            "personnel_or_xg_model": "blocked_pending_representative_continuous_coverage_and_point_in_time_features",
            "score_only_research": "not_blocked_by_missing_process_fields"}}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fundamentals-dir")
    parser.add_argument("--probe-dir")
    parser.add_argument("--data-dir")
    parser.add_argument("--as-of", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    if not any((args.fundamentals_dir, args.probe_dir, args.data_dir)):
        parser.error("at least one explicit evidence directory is required")
    result = audit_saved(as_of=args.as_of, fundamentals_dir=args.fundamentals_dir,
                         probe_dir=args.probe_dir, data_dir=args.data_dir)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8") as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2, allow_nan=False)
        handle.write("\n")
    print(json.dumps({"output": str(output), "selected_fixtures": result["sample_audit"]["selected_fixtures"],
                      "rejected_fixtures": len(result["rejected_fixtures"]), "training_leagues": len(result["training_recency"])}))


if __name__ == "__main__":
    main()
