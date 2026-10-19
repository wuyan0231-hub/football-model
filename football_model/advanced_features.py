"""Causal opportunity and lightweight personnel features with source closure.

No missing statistic is a zero. Historical use of data downloaded later is
explicitly a development assumption; prospective use requires observed sources
by the cutoff. EARLY never consumes the target's eventual confirmed lineup.
"""
from __future__ import annotations

from collections import Counter
from datetime import timedelta
import hashlib
import json
import math
from pathlib import Path

from .competition_scope import require_league
from .data import DataError, _integer, _iso, _utc
from .fundamentals import _fixture, _validate_snapshot, make_snapshot, statistics_diagnostics


SCHEMA_VERSION = "advanced-feature-rows-v1"
FEATURE_GROUPS = {
    "opportunity_shots": ("shots_for_mean", "shots_on_target_for_mean",
        "opponent_shots_against_mean", "opponent_shots_on_target_against_mean"),
    "opportunity_xg": ("xg_for_mean", "opponent_xg_against_mean"),
    "lineup_early": ("prior_xi_continuity", "opponent_prior_xi_continuity"),
    "lineup_confirmed": ("confirmed_xi_overlap", "opponent_confirmed_xi_overlap"),
    "lineup_minutes": ("confirmed_xi_prior_minutes_share", "opponent_confirmed_xi_prior_minutes_share"),
    "lineup_rating": ("confirmed_xi_prior_rating_delta", "opponent_confirmed_xi_prior_rating_delta"),
    "personnel_early": ("reported_absence_recent_minutes_share", "reported_absence_previous_xi_share",
        "opponent_reported_absence_recent_minutes_share", "opponent_reported_absence_previous_xi_share"),

}
FEATURE_NAMES = tuple(n for group in FEATURE_GROUPS.values() for n in group)
RESULT_DELAY = timedelta(hours=4)


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"),
        parse_constant=lambda _: (_ for _ in ()).throw(DataError("nonfinite_json")))


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def source_ref(path, runtime):
    path, runtime = Path(path), Path(runtime)
    if not path.resolve().is_relative_to(runtime.resolve()):
        raise DataError("source_outside_runtime")
    return {"path": str(path.relative_to(runtime)), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def _identity(row):
    result = {k: _integer(row.get(k), k, 1) for k in ("match_id", "home_id", "away_id")}
    if result["home_id"] == result["away_id"]:
        raise DataError("duplicate_team_identity")
    result["kickoff"] = _iso(_utc(row.get("kickoff")))
    return result


def make_players_snapshot(envelope, fixture, *, context_observed_at=None):
    """Validate fixture player minutes; no season aggregate or future results."""
    identity = _fixture(fixture)
    if (not isinstance(envelope, dict) or envelope.get("endpoint") != "/fixtures/players"
            or envelope.get("params") != {"fixture": identity["match_id"]}
            or envelope.get("errors") or not isinstance(envelope.get("response"), list)
            or envelope.get("paging", {}).get("total", 1) != 1
            or envelope.get("paging", {}).get("current", 1) != 1):
        raise DataError("players_envelope_invalid")
    observed = _utc(envelope.get("fetched_at"))
    context = _utc(context_observed_at) if context_observed_at is not None else observed
    teams, seen = {}, set()
    for row in envelope["response"]:
        tid = _integer(row.get("team", {}).get("id"), "team_id", 1)
        if tid not in (identity["home_id"], identity["away_id"]) or tid in seen:
            raise DataError("players_team_identity_invalid")
        seen.add(tid)
        players, pids, missing, unidentified = [], set(), [], []
        if not isinstance(row.get("players"), list):
            raise DataError("players_not_list")
        for index, raw in enumerate(row["players"]):
            pid = raw.get("player", {}).get("id")
            stats = raw.get("statistics")
            if not isinstance(stats, list) or len(stats) != 1 or not isinstance(stats[0], dict):
                raise DataError("player_statistics_not_unique")
            minutes = stats[0].get("games", {}).get("minutes")
            # Provider FT player minutes can include stoppage time. A 90-minute
            # cap or 990-team-minutes identity incorrectly rejects legitimate
            # regulation matches. FT status excludes extra time; this generous
            # bound is only a malformed-value sanity guard.
            if minutes is not None and (type(minutes) not in (int, float) or not math.isfinite(minutes) or not 0 <= minutes <= 180):
                raise DataError("invalid_regulation_player_minutes")
            raw_rating = stats[0].get("games", {}).get("rating")
            rating = None
            if raw_rating is not None:
                try:
                    if isinstance(raw_rating,bool): raise ValueError
                    rating = float(raw_rating)
                    if not math.isfinite(rating) or not 0 <= rating <= 10: raise ValueError
                except (ValueError,TypeError):
                    raise DataError("invalid_player_rating") from None
            if type(pid)is not int or pid<1:
                unidentified.append({"source_row_index":index,"minutes":minutes,"rating":rating,
                    "reason":"unidentified_player_not_assigned_synthetic_id"})
                continue
            if pid in pids:raise DataError("duplicate_player")
            pids.add(pid)
            if minutes is None:missing.append(pid)
            players.append({"player_id": pid, "minutes": minutes, "rating": rating})
        positive = sum(p["minutes"] is not None and p["minutes"] > 0 for p in players)
        teams[str(tid)] = {"players": players, "complete_minutes": not missing and not unidentified and positive >= 11,
            "missing_minutes_player_ids": missing, "player_count": len(players),"unidentified_players":unidentified,
            "minutes_scope":"provider_reported_FT_regulation_including_stoppage"}
    for tid in (identity["home_id"], identity["away_id"]):
        teams.setdefault(str(tid), {"players": [], "complete_minutes": False,
            "missing_minutes_player_ids": [], "player_count": 0,"unidentified_players":[],
            "minutes_scope":"provider_reported_FT_regulation_including_stoppage"})
    result = {"schema_version": "advanced-players-snapshot-v1", "kind": "players",
        "endpoint": "/fixtures/players", "params": envelope["params"], "fixture": identity,
        "observed_at": _iso(observed), "context_observed_at": _iso(context),
        "known_at": _iso(max(observed, context)), "raw_response": envelope["response"],
        "raw_sha256": digest(envelope["response"]), "teams": teams}
    result["snapshot_id"] = digest(result)
    return result


def validate_players_snapshot(snapshot):
    expected = make_players_snapshot({"endpoint": snapshot.get("endpoint"), "params": snapshot.get("params"),
        "response": snapshot.get("raw_response"), "fetched_at": snapshot.get("observed_at")},
        snapshot.get("fixture"), context_observed_at=snapshot.get("context_observed_at"))
    if expected != snapshot:
        raise DataError("players_snapshot_reconstruction_mismatch")


def _evidence_context(value,runtime,context_cache=None):
    if "fixture_envelope_ref" not in value:return value["fixture_envelope"]
    if runtime is None:raise DataError("source_runtime_required")
    ref=value["fixture_envelope_ref"]
    key=(ref["path"],ref["sha256"])
    context=context_cache.get(key) if context_cache is not None else None
    if context is None:
        path=Path(runtime)/ref["path"]
        if source_ref(path,runtime)!=ref:raise DataError("fixture_context_hash_mismatch")
        context=read_json(path)
        if context_cache is not None:context_cache[key]=context
    projection=value.get("fixture_envelope_projection")
    if projection is not None:
        if projection!="fixture_response":raise DataError("invalid_fixture_context_projection")
        context=context[projection]
    return context


def embedded_snapshot(kind,context,match_id):
    if kind not in ("statistics","lineups","players") or context.get("endpoint")!="/fixtures" or context.get("errors"):
        raise DataError("invalid_embedded_source")
    if context.get("paging",{}).get("total",1)!=1 or context.get("paging",{}).get("current",1)!=1:
        raise DataError("invalid_embedded_pagination")
    rows=[r for r in context["response"] if r.get("fixture",{}).get("id")==match_id]
    if len(rows)!=1:raise DataError("embedded_fixture_not_unique")
    raw=rows[0];identity=_fixture(raw);league=raw.get("league",{})
    if context.get("params") not in ({"id":match_id},{"league":league.get("id"),"season":league.get("season")}):
        raise DataError("embedded_context_params_mismatch")
    if not isinstance(raw.get(kind),list):raise DataError("embedded_field_missing")
    envelope={"endpoint":"/fixtures/"+kind,"params":{"fixture":match_id},
        "response":raw[kind],"fetched_at":context["fetched_at"]}
    # This is a declared adapter projection, not an invented network response;
    # the persisted wrapper's authority is the original /fixtures document.
    return (make_players_snapshot(envelope,raw,context_observed_at=context["fetched_at"])
        if kind=="players" else make_snapshot(kind,envelope,raw,context_observed_at=context["fetched_at"]))


def validate_evidence(value, runtime=None, context_cache=None):
    """Reconstruct a snapshot from its fixture/endpoint/metadata originals."""
    if value.get("schema_version")=="advanced-embedded-source-v1":
        context=_evidence_context(value,runtime,context_cache)
        snap=embedded_snapshot(value["kind"],context,value["match_id"])
        if snap["snapshot_id"]!=value["normalized_snapshot_id"]:
            raise DataError("embedded_snapshot_reconstruction_mismatch")
        if _utc(snap["known_at"])>_utc(value["created_at"]):raise DataError("embedded_observation_in_future")
        return snap
    if value.get("schema_version") != "advanced-source-v1":
        raise DataError("invalid_advanced_source_schema")
    snap, metadata = value["snapshot"], value["metadata"]
    context=_evidence_context(value,runtime,context_cache)
    if context.get("endpoint") != "/fixtures" or context.get("errors") or context.get("paging",{}).get("total",1) != 1:
        raise DataError("invalid_fixture_context")
    raw = [r for r in context["response"] if r.get("fixture",{}).get("id") == snap["fixture"]["match_id"]]
    if len(raw) != 1 or _fixture(raw[0]) != snap["fixture"]:
        raise DataError("source_fixture_context_mismatch")
    params = context.get("params",{})
    league = raw[0].get("league",{})
    if params not in ({"id":snap["fixture"]["match_id"]},{"league":league.get("id"),"season":league.get("season")}):
        raise DataError("fixture_context_params_mismatch")
    if league.get("id") != metadata.get("league",{}).get("id") or not any(s.get("year")==league.get("season") for s in metadata.get("seasons",[])):
        raise DataError("fixture_metadata_identity_mismatch")
    expected = (make_players_snapshot(value["evidence_envelope"],raw[0],context_observed_at=context["fetched_at"])
        if snap["kind"] == "players" else make_snapshot(snap["kind"],value["evidence_envelope"],raw[0],context_observed_at=context["fetched_at"]))
    if snap != expected:
        raise DataError("advanced_snapshot_reconstruction_mismatch")
    if _utc(metadata["fetched_at"]) > _utc(value["created_at"]) or _utc(snap["known_at"]) > _utc(value["created_at"]):
        raise DataError("advanced_source_observed_in_future")
    return snap


def load_sources(runtime, league, records=None):
    """Load validated retained sources once for efficient batch extraction."""
    require_league(league)
    runtime = Path(runtime)
    if not isinstance(league, str) or Path(league).name != league or league in ("", ".", ".."):
        raise DataError("invalid_league")
    path = runtime / "data" / (league + ".json")
    saved = read_json(path)
    if not isinstance(saved, dict) or not isinstance(saved.get("matches"), list):
        raise DataError("invalid_history")
    supplied = saved["matches"] if records is None else records
    identities, history = {}, []
    saved_ids = {r["match_id"]: _identity(r) for r in saved["matches"]}
    for record in supplied:
        item = _identity(record)
        if item["match_id"] in identities or saved_ids.get(item["match_id"]) != item:
            raise DataError("history_duplicate_or_source_mismatch")
        identities[item["match_id"]] = item
        history.append(item)
    snapshots, issues, dependencies, context_cache, known_times = {}, [], {}, {}, {}
    paths = list((runtime / "fundamentals" / "snapshots").glob("*/*/*.json"))
    paths += list((runtime / "advanced_features" / "snapshots").glob("players/*/*.json"))
    paths += list((runtime / "advanced_features" / "evidence").glob("*/*/*.json"))
    for path in sorted(paths):
        try:
            snap = read_json(path)
            if snap.get("schema_version") in ("advanced-source-v1","advanced-embedded-source-v1"):
                wrapper=snap
                snap = validate_evidence(wrapper,runtime,context_cache)
                known_times[str(path.relative_to(runtime))]=_iso(max(_utc(t) for t in
                    (snap["known_at"],wrapper.get("metadata",{}).get("fetched_at",snap["known_at"]),wrapper["created_at"])))
                if "fixture_envelope_ref" in wrapper:
                    dependencies[str(path.relative_to(runtime))]=[wrapper["fixture_envelope_ref"]]
            mid = snap.get("fixture", {}).get("match_id")
            # Ignore genuinely unrelated fixtures. Target LINEUP snapshots need
            # not be historical results and are retained by exact identity later.
            if snap.get("kind") not in ("statistics", "lineups", "players"):
                continue
            if snap.get("kind") == "players":
                validate_players_snapshot(snap)
            else:
                _validate_snapshot(snap)
            if mid in identities and _identity(snap["fixture"]) != identities[mid]:
                raise DataError("snapshot_fixture_identity_conflict")
            snapshots.setdefault((snap["kind"], mid), []).append((snap, source_ref(path, runtime)))
        except (ValueError, TypeError, KeyError, OSError, AttributeError):
            issues.append({"path": str(path.relative_to(runtime)), "reason": "invalid_source_snapshot"})
    from .advanced_personnel import load_personnel_sources
    return {"history": sorted(history, key=lambda r: (r["kickoff"], r["match_id"])),
        "personnel_sources": load_personnel_sources(runtime, league, records),
        "snapshots": snapshots, "history_source": source_ref(runtime / "data" / (league + ".json"), runtime),
        "history_observed_at": saved.get("observed_at"),
        "dependencies": dependencies,
        "known_times": known_times,
        "source_issues": issues}


def _select(sources, kind, record, cutoff, mode, *, target=False):
    choices = []
    for snap, ref in sources["snapshots"].get((kind, record["match_id"]), []):
        if _identity(snap["fixture"]) != _identity(record):
            continue
        if target:
            if mode == "prospective" and snap["fixture"]["status"] != "NS":
                continue
        elif snap["fixture"]["status"] != "FT":
            # AET/PEN aggregates cannot be used as regulation opportunity data.
            continue
        known=sources.get("known_times",{}).get(ref["path"],snap["known_at"])
        if mode == "prospective" and _utc(known) > cutoff:
            continue
        choices.append((snap, ref))
    return max(choices, key=lambda pair: (pair[0]["known_at"], pair[0]["snapshot_id"]), default=None)


def _stats(snap, team):
    xg = snap["teams"][str(team)].get("xg")
    quarantined = any(r["team_id"] == team for r in statistics_diagnostics(snap["raw_response"]))
    rows = [r for r in snap["raw_response"] if r.get("team", {}).get("id") == team]
    values = {}
    if len(rows) == 1 and not quarantined:
        for label, field in (("Total Shots", "shots"), ("Shots on Goal", "on_target")):
            found = [r.get("value") for r in rows[0].get("statistics", []) if r.get("type") == label]
            value = found[0] if len(found) == 1 else None
            values[field] = float(value) if type(value) in (int, float) and math.isfinite(value) and value >= 0 else None
        if values.get("shots") is not None and values.get("on_target") is not None and values["on_target"] > values["shots"]:
            values = {}
    return {"xg": xg, "shots": values.get("shots"), "on_target": values.get("on_target")}


def feature_values(runtime, league, fixture, cutoff_at, *, horizon="EARLY", mode="prospective",
                   recent_matches=5, min_history=3, records=None, sources=None):
    """Return numeric per-side features and exact inputs, never a probability."""
    require_league(league)
    if horizon not in ("EARLY", "LINEUP") or mode not in ("historical", "prospective"):
        raise DataError("invalid_feature_mode_or_horizon")
    if type(recent_matches) is not int or type(min_history) is not int or not 2 <= min_history <= recent_matches <= 20:
        raise DataError("invalid_history_thresholds")
    target, cutoff = _identity(fixture), _utc(cutoff_at)
    if cutoff >= _utc(target["kickoff"]):
        raise DataError("feature_cutoff_not_pre_match")
    sources = sources or load_sources(runtime, league, records)
    if mode == "prospective" and (sources.get("history_observed_at") is None or _utc(sources["history_observed_at"]) > cutoff):
        raise DataError("history_not_observed_by_prospective_cutoff")
    refs = {sources["history_source"]["path"]: sources["history_source"]}
    missing, evidence, own = [], {}, {}
    def retain(selected):
        if selected is not None:
            refs[selected[1]["path"]] = selected[1]
            for ref in sources.get("dependencies",{}).get(selected[1]["path"],[]):
                refs[ref["path"]]=ref
            return selected[0]
        return None
    for tid in (target["home_id"], target["away_id"]):
        prior = [r for r in sources["history"] if r["match_id"] != target["match_id"]
            and tid in (r["home_id"], r["away_id"])
            and _utc(r["kickoff"]) + RESULT_DELAY <= cutoff][-recent_matches:]
        stats_for = {name: [] for name in ("shots", "on_target", "xg")}
        stats_against = {name: [] for name in stats_for}
        lineups, minutes, ratings, rating_games = [], [], {}, 0
        for record in prior:
            other = record["away_id"] if tid == record["home_id"] else record["home_id"]
            stat = retain(_select(sources, "statistics", record, cutoff, mode))
            if stat is not None:
                f, a = _stats(stat, tid), _stats(stat, other)
                for key in stats_for:
                    if f[key] is not None: stats_for[key].append(f[key])
                    if a[key] is not None: stats_against[key].append(a[key])
            xi = retain(_select(sources, "lineups", record, cutoff, mode))
            players = retain(_select(sources, "players", record, cutoff, mode))
            if xi is not None and xi["teams"][str(tid)].get("complete_starting_eleven"):
                lineups.append((record["match_id"], {p["player_id"] for p in xi["teams"][str(tid)]["starters"]}))
            if players is not None and players["teams"][str(tid)]["complete_minutes"]:
                minutes.append({p["player_id"]: p["minutes"] for p in players["teams"][str(tid)]["players"]})
            if players is not None:
                observed_ratings=[p for p in players["teams"][str(tid)]["players"]
                    if p["rating"] is not None and p["minutes"] is not None and p["minutes"]>0]
                rating_games+=bool(observed_ratings)
                for p in observed_ratings:
                    total = ratings.setdefault(p["player_id"],[0.0,0.0])
                    total[0] += p["rating"] * p["minutes"]
                    total[1] += p["minutes"]
        value = {}
        for key in stats_for:
            value[key + "_for"] = sum(stats_for[key])/len(stats_for[key]) if len(stats_for[key]) >= min_history else None
            value[key + "_against"] = sum(stats_against[key])/len(stats_against[key]) if len(stats_against[key]) >= min_history else None
        # Require consecutive known lineups; do not bridge gaps and relabel them
        # as the team's immediately previous rotation.
        xi_map = dict(lineups)
        overlaps = [len(xi_map[a["match_id"]] & xi_map[b["match_id"]])/11 for a,b in zip(prior, prior[1:])
            if a["match_id"] in xi_map and b["match_id"] in xi_map]
        value["prior_xi_continuity"] = sum(overlaps)/len(overlaps) if len(overlaps) >= min_history-1 else None
        value["confirmed_xi_overlap"] = value["confirmed_xi_prior_minutes_share"] = None
        value["confirmed_xi_prior_rating_delta"] = None
        if horizon == "LINEUP":
            current = retain(_select(sources, "lineups", target, cutoff, mode, target=True))
            team = current["teams"][str(tid)] if current is not None else {}
            if team.get("complete_starting_eleven"):
                actual = {p["player_id"] for p in team["starters"]}
                previous = xi_map.get(prior[-1]["match_id"]) if prior else None
                if previous is not None:
                    value["confirmed_xi_overlap"] = len(actual & previous)/11
                    needed = actual | previous
                    if rating_games >= min_history and all(pid in ratings and ratings[pid][1] >= 90 for pid in needed):
                        ability = {pid: ratings[pid][0]/ratings[pid][1] for pid in needed}
                        value["confirmed_xi_prior_rating_delta"] = (sum(ability[p] for p in actual)-sum(ability[p] for p in previous))/11
                if len(minutes) >= min_history:
                    denominator = sum(sum(m.values()) for m in minutes)
                    if denominator > 0:
                        value["confirmed_xi_prior_minutes_share"] = sum(sum(m.get(pid,0) for pid in actual) for m in minutes)/denominator
        own[tid] = value
        evidence[str(tid)] = {"prior_match_ids": [r["match_id"] for r in prior],
            "statistic_available_counts_for": {k: len(v) for k,v in stats_for.items()},
            "statistic_available_counts_against": {k: len(v) for k,v in stats_against.items()},
            "prior_lineup_count": len(lineups), "consecutive_lineup_pairs": len(overlaps),
            "complete_player_minutes_match_count": len(minutes), "observed_rated_player_match_count": rating_games,
            "player_observed_rated_minutes": {str(pid):v[1] for pid,v in ratings.items()},
            "rating_scope": "observed_rated_minutes_only;missing_minutes_or_ratings_excluded_not_zero"}
    def side(tid, oid):
        a,b = own[tid], own[oid]
        return {"shots_for_mean": a["shots_for"], "shots_on_target_for_mean": a["on_target_for"],
            "opponent_shots_against_mean": b["shots_against"], "opponent_shots_on_target_against_mean": b["on_target_against"],
            "xg_for_mean": a["xg_for"], "opponent_xg_against_mean": b["xg_against"],
            "prior_xi_continuity": a["prior_xi_continuity"], "opponent_prior_xi_continuity": b["prior_xi_continuity"],
            "confirmed_xi_overlap": a["confirmed_xi_overlap"], "opponent_confirmed_xi_overlap": b["confirmed_xi_overlap"],
            "confirmed_xi_prior_minutes_share": a["confirmed_xi_prior_minutes_share"],
            "opponent_confirmed_xi_prior_minutes_share": b["confirmed_xi_prior_minutes_share"],
            "confirmed_xi_prior_rating_delta": a["confirmed_xi_prior_rating_delta"],
            "opponent_confirmed_xi_prior_rating_delta": b["confirmed_xi_prior_rating_delta"]}
    home, away = side(target["home_id"],target["away_id"]), side(target["away_id"],target["home_id"])
    # Current reported absences require an actual pre-cutoff injury archive even
    # in historical development; eventual injuries cannot be backfilled as EARLY.
    from .advanced_personnel import personnel_features
    personnel = personnel_features(runtime, league, fixture, cutoff_at,
        horizon=horizon, mode=mode, recent_matches=recent_matches,
        min_history=min_history, records=records, sources=sources)
    home.update(personnel["home"]); away.update(personnel["away"])
    refs.update((ref["path"], ref) for ref in personnel["source_refs"])
    evidence["personnel_early"] = personnel["evidence"]
    missing.extend(personnel["missing"])
    groups = {name: all(values[k] is not None for values in (home,away) for k in names)
        for name,names in FEATURE_GROUPS.items()}
    for side_name, values in (("home",home),("away",away)):
        for name,value in values.items():
            if value is None:
                missing.append({"side": side_name, "feature": name,
                    "reason": "not_applicable_early" if horizon == "EARLY" and "confirmed_xi" in name else "insufficient_eligible_observed_inputs"})
    return {"schema_version": SCHEMA_VERSION, **target, "cutoff_at": _iso(cutoff), "horizon": horizon,
        "mode": mode, "home": home, "away": away, "home_features": home, "away_features": away,
        "feature_groups": {k:list(v) for k,v in FEATURE_GROUPS.items()}, "groups": groups,
        "feature_available_at": {n:_iso(cutoff) for n in FEATURE_NAMES},
        "source_refs": sorted(refs.values(), key=lambda r:r["path"]), "missing": missing,
        "evidence": evidence, "source_issues": sources["source_issues"] + personnel["source_issues"],
        # Diagnostics are observations about rejected bytes, not dependencies.
        # Complete training bundles retain this row; generic source-closure
        # walkers must not read these paths as admitted numerical parents.
        "optional_diagnostic_refs": [],
        "optional_diagnostic_bytes_frozen": False,
        "availability_basis": "historical_kickoff_plus_four_hours_assumption" if mode == "historical" else "validated_source_known_at_not_after_cutoff",
        "historical_current_lineup_assumed_available": mode == "historical" and horizon == "LINEUP",
        "prospective_qualified": mode == "prospective", "target_match_statistics_excluded": True,
        "scope": "same_competition_retained_history", "recent_matches": recent_matches, "min_history": min_history}


def historical_features(runtime, league, matches, target, cutoff, protocol=None):
    protocol = protocol or {}
    return feature_values(runtime,league,target,cutoff, records=matches,mode="historical",
        horizon=protocol.get("horizon", "EARLY"),recent_matches=protocol.get("advanced_feature_recent_matches",5),
        min_history=protocol.get("advanced_feature_min_history",3))


def build_feature_rows(runtime, league, matches=None, *, horizon="EARLY", horizon_hours=10,
                       cutoff_at=None, mode="historical", recent_matches=5, min_history=3):
    require_league(league)
    sources = load_sources(runtime,league,matches)
    targets = matches if matches is not None else sources["history"]
    rows, failures = [], []
    for target in targets:
        cutoff = cutoff_at if cutoff_at is not None else _iso(_utc(target["kickoff"])-timedelta(hours=horizon_hours))
        try:
            rows.append(feature_values(runtime,league,target,cutoff,horizon=horizon,mode=mode,
                recent_matches=recent_matches,min_history=min_history,sources=sources))
        except (ValueError,TypeError,KeyError,OSError):
            failures.append({"match_id": target.get("match_id"), "reason": "feature_build_failed"})
    refs = {r["path"]:r for row in rows for r in row["source_refs"]}
    return {"schema_version":SCHEMA_VERSION,"league":league,"rows":rows,"failures":failures,
        "feature_groups":{k:list(v) for k,v in FEATURE_GROUPS.items()},
        "source_refs":sorted(refs.values(),key=lambda r:r["path"]),
        "coverage":{"requested":len(targets),"rows":len(rows),"failed":len(failures),
            "complete_groups":dict(Counter(g for row in rows for g,complete in row["groups"].items() if complete)),
            "denominator_preserved":len(targets)==len(rows)+len(failures)}}
