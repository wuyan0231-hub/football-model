"""Point-in-time evidence and features; never an untrained probability adjustment.

API-Football endpoints are interpreted conservatively: lineup/injury publication
times are not supplied by the endpoint contract, so first observed time is the
earliest defensible knowledge time. A historical download is not a historical
pre-match snapshot. ``expected_goals`` is parsed only if actually supplied; fixture
statistics coverage does not promise xG coverage.

References checked 2026-10-06:
https://www.api-football.com/documentation-v3
https://www.api-football.com/news/post/how-to-get-started-with-api-football-the-complete-beginners-guide
"""

from __future__ import annotations

import copy
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import math
import re
from pathlib import Path

from .data import APIError, DataError, RequestBudgetError, _integer, _iso, _utc


SCHEMA_VERSION = "fundamentals-evidence-v1"
ENDPOINTS = {"injuries": "/injuries", "lineups": "/fixtures/lineups",
             "statistics": "/fixtures/statistics"}
TTL_SECONDS = {"injuries": 4 * 3600, "lineups": 15 * 60, "statistics": 24 * 3600}


def failure_diagnostic(kind, fixture, *, stage, error, checked_at, envelope=None,
                       context_observed_at=None, collection_started_at=None):
    """Safe, content-addressable evidence; never retain arbitrary exception text.

    This is a whitelist projection, not original HTTP bytes. Free text and
    unknown keys are omitted or fingerprinted, including provider error text.
    Replay qualification is checked on the projection instead of assumed.
    """
    keys = {"endpoint", "params", "fixture", "response", "paging", "total", "current",
            "fetched_at", "errors", "half", "id", "team", "player", "startXI",
            "substitutes", "statistics", "statistics_1h", "statistics_2h", "type",
            "value", "name", "pos", "number", "grid", "formation", "reason",
            "match_id", "kickoff", "home_id", "away_id", "status"}
    tokens = set(ENDPOINTS.values()) | {"/fixtures", "true", "false", "NS", "FT", "AET", "PEN",
        "expected_goals", "Total Shots", "Shots insidebox", "Shots outsidebox",
        "Shots on Goal", "Shots off Goal", "Blocked Shots"}

    def project(value, key=None):
        if isinstance(value, dict):
            out = {k: project(v, k) for k, v in value.items() if k in keys}
            if any(k not in keys for k in value):
                out["_omitted_fields"] = True
            return out
        if isinstance(value, list):
            return [project(v, key) for v in value]
        if value is None or isinstance(value, bool) or type(value) is int:
            return value
        if type(value) is float:
            return value if math.isfinite(value) else "nonfinite_redacted"
        if isinstance(value, str):
            if value in tokens or (key in ("fetched_at", "kickoff") and
                    re.fullmatch(r"[0-9TtZz:+.\- ]{10,40}", value)):
                return value
            if key == "value" and re.fullmatch(r"[+\-]?[0-9]{1,9}(\.[0-9]{1,9})?%?", value):
                return value
            return "text_sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()
        return "unsupported_type:" + type(value).__name__

    category = ("budget" if isinstance(error, RequestBudgetError) else
                "provider" if isinstance(error, APIError) else
                "storage" if isinstance(error, OSError) else "validation")
    codes = {"startXI 含重复球员": "duplicate_starter", "substitutes 含重复球员": "duplicate_substitute",
             "首发与替补名单重叠": "starter_substitute_overlap",
             "player.id 必须为不小于 1 的整数": "invalid_player_id",
             "基本面记录球队与本场身份不符": "team_identity_mismatch",
             "基本面响应包含重复球队": "duplicate_team",
             "不接受未完成分页的基本面响应": "incomplete_pagination",
             "expected_goals 必须为有限非负数或 null": "invalid_xg"}
    code = codes.get(str(error), category + "_failure")
    payload = {"fixture": project(fixture), "envelope": project(envelope),
               "context_observed_at": _iso(_utc(context_observed_at or checked_at))}
    response_observed_at = None
    if isinstance(envelope, dict) and envelope.get("fetched_at") is not None:
        try:
            response_observed_at = _iso(_utc(envelope["fetched_at"]))
        except (ValueError, TypeError):
            pass
    result = {"schema_version": "fundamentals-failure-v1", "kind": kind,
              "match_id": fixture.get("match_id"), "endpoint": ENDPOINTS.get(kind),
              "stage": stage, "category": category, "code": code,
              "checked_at": _iso(_utc(checked_at)), "failure_recorded_at": _iso(_utc(checked_at)),
              "collection_started_at": _iso(_utc(collection_started_at)) if collection_started_at is not None else None,
              "response_observed_at": response_observed_at, "payload": payload,
              "payload_sha256": _digest(payload), "fingerprint_scope": "whitelist_projection_canonical_json",
              "source_http_bytes_preserved": False, "replay_verified": False}
    if stage == "normalize" and envelope is not None:
        try:
            make_snapshot(kind, payload["envelope"], payload["fixture"],
                          context_observed_at=payload["context_observed_at"])
        except (ValueError, KeyError, TypeError, AttributeError) as replay_error:
            # Generic classes/codes are insufficient: two unrelated DataErrors
            # must not be reported as a reproduction of the same failure.
            result["replay_verified"] = (str(error) in codes and type(replay_error) is type(error)
                and codes.get(str(replay_error)) == code)
    return result


def statistics_diagnostics(rows):
    """Report additive shot inconsistencies without repairing supplied values.

    Only compare fields that are all present, unique and numeric; percentages,
    xG and missing/null cells are never added or silently converted to zero.
    """
    findings = []
    def values(items):
        result, repeated = {}, set()
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict):
                continue
            name, value = item.get("type"), item.get("value")
            if not isinstance(name, str):
                continue
            if name in result:
                repeated.add(name)
            result[name] = value if type(value) in (int, float) and math.isfinite(value) and value >= 0 else None
        return {k: v for k, v in result.items() if k not in repeated}
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict) or not isinstance(row.get("team"), dict):
            continue
        tid = row["team"].get("id")
        full, first, second = [values(row.get(k)) for k in ("statistics", "statistics_1h", "statistics_2h")]
        checks = [("shots_by_location", full.get("Total Shots"),
                   [full.get("Shots insidebox"), full.get("Shots outsidebox")]),
                  ("shots_by_outcome", full.get("Total Shots"),
                   [full.get("Shots on Goal"), full.get("Shots off Goal"), full.get("Blocked Shots")])]
        for name in ("Total Shots", "Shots insidebox", "Shots outsidebox", "Shots on Goal", "Shots off Goal", "Blocked Shots"):
            checks.append(("halves_" + name, full.get(name), [first.get(name), second.get(name)]))
        for check, total, components in checks:
            if total is not None and all(v is not None for v in components) and not math.isclose(total, sum(components), abs_tol=1e-9):
                findings.append({"team_id": tid, "check": check, "reported_total": total,
                    "reported_components": components, "components_sum": sum(components),
                    "code": "shot_statistics_inconsistent", "action": "quarantine_shot_derived_features",
                    "raw_values_changed": False, "fundamentals_used_in_probabilities": False})
    return findings


def quantitative_scope() -> dict:
    """State what current model probabilities actually include."""
    return {"numerical_model_scope": "historical_scores_only",
            "fundamentals_feature_schema": SCHEMA_VERSION,
            "fundamentals_used_in_probabilities": False,
            "unfitted_features": ["injuries", "lineups", "lineup_continuity",
                                  "prior_match_xg", "rest_days"],
            "note": "基本面特征已结构化不等于已进入胜率或EV；须时序训练和独立验证后接入。"}


def _fixture(value: dict) -> dict:
    """Normalize identity only; match status must come from source evidence."""
    if not isinstance(value, dict):
        raise DataError("fixture 必须为对象")
    if "fixture" in value:
        try:
            result = {"match_id": value["fixture"]["id"],
                      "kickoff": value["fixture"]["date"],
                      "home_id": value["teams"]["home"]["id"],
                      "away_id": value["teams"]["away"]["id"],
                      "status": value["fixture"].get("status", {}).get("short")}
        except (KeyError, TypeError, AttributeError):
            raise DataError("fixture 身份结构缺失") from None
    else:
        result = {key: value.get(key) for key in
                  ("match_id", "kickoff", "home_id", "away_id", "status")}
    for key in ("match_id", "home_id", "away_id"):
        _integer(result[key], key, 1)
    if result["home_id"] == result["away_id"]:
        raise DataError("主客队不能相同")
    result["kickoff"] = _iso(_utc(result["kickoff"]))
    return result


def _canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False)


def _digest(value) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _team_id(row: dict, identity: dict) -> int:
    try:
        team_id = _integer(row["team"]["id"], "team.id", 1)
    except (KeyError, TypeError):
        raise DataError("基本面记录缺少 team.id") from None
    if team_id not in (identity["home_id"], identity["away_id"]):
        raise DataError("基本面记录球队与本场身份不符")
    return team_id


def _players(rows, label: str, *, allow_unidentified=False) -> list[dict]:
    if not isinstance(rows, list):
        raise DataError(f"{label} 必须为列表")
    result, seen = [], set()
    for row in rows:
        player = row.get("player") if isinstance(row, dict) else None
        if not isinstance(player, dict):
            raise DataError(f"{label} 缺少 player")
        pid = player.get("id")
        unidentified = allow_unidentified and pid is None
        if not unidentified:
            pid = _integer(pid, "player.id", 1)
        if not unidentified and pid in seen:
            raise DataError(f"{label} 含重复球员")
        if not unidentified:
            seen.add(pid)
        result.append({"player_id": pid, "name": player.get("name"),
                       "position": player.get("pos"), "number": player.get("number"),
                       "grid": player.get("grid"),
                       **({"identity_status": "provider_id_missing"} if unidentified else {})})
    return result


def _normalize(kind: str, rows: list, identity: dict) -> dict:
    """Preserve reported facts; empty injury data never means verified healthy."""
    teams = {str(tid): {} for tid in (identity["home_id"], identity["away_id"])}
    if kind == "injuries":
        for team in teams.values():
            team.update(records=[], reported_count=0, complete_roster_status=False,
                        duplicate_rows_removed=0,
                        missing_reason="empty_response_or_no_reported_cases")
        seen = {}
        for row in rows:
            if not isinstance(row, dict):
                raise DataError("injuries 行必须为对象")
            tid = _team_id(row, identity)
            if row.get("fixture", {}).get("id") != identity["match_id"]:
                raise DataError("伤停 fixture.id 与查询不符")
            player = row.get("player")
            if not isinstance(player, dict):
                raise DataError("伤停记录缺少 player")
            pid = _integer(player.get("id"), "player.id", 1)
            record = {"player_id": pid,
                "name": player.get("name"), "provider_type": player.get("type"),
                "provider_reason": player.get("reason"), "effect_estimate": None}
            if (tid, pid) in seen:
                if seen[(tid, pid)] != record:
                    raise DataError("同球员伤停记录相互冲突")
                teams[str(tid)]["duplicate_rows_removed"] += 1
                continue
            seen[(tid, pid)] = record
            teams[str(tid)]["records"].append(record)
        for team in teams.values():
            team["reported_count"] = len(team["records"])
            if team["records"]:
                team["missing_reason"] = None
        return teams
    seen = set()
    for row in rows:
        if not isinstance(row, dict):
            raise DataError("基本面行必须为对象")
        tid = _team_id(row, identity)
        if tid in seen:
            raise DataError("基本面响应包含重复球队")
        seen.add(tid)
        if kind == "lineups":
            starters = _players(row.get("startXI", []), "startXI")
            substitutes = _players(row.get("substitutes", []), "substitutes", allow_unidentified=True)
            if {p["player_id"] for p in starters} & {p["player_id"] for p in substitutes}:
                raise DataError("首发与替补名单重叠")
            complete = len(starters) == 11
            teams[str(tid)] = {"formation": row.get("formation"),
                "starters": starters, "substitutes": substitutes,
                "complete_starting_eleven": complete,
                "missing_reason": None if complete else "incomplete_starting_eleven"}
        elif kind == "statistics":
            stats = row.get("statistics")
            if not isinstance(stats, list):
                raise DataError("statistics 必须为列表")
            xg = [s for s in stats if isinstance(s, dict) and s.get("type") == "expected_goals"]
            if len(xg) > 1:
                raise DataError("expected_goals 字段重复")
            raw = xg[0].get("value") if xg else None
            missing = "provider_null" if xg and raw is None else "field_absent" if not xg else None
            val = None
            if missing is None:
                try:
                    if isinstance(raw, bool):
                        raise ValueError
                    val = float(raw)
                    if not math.isfinite(val) or val < 0:
                        raise ValueError
                except (TypeError, ValueError):
                    raise DataError("expected_goals 必须为有限非负数或 null") from None
            teams[str(tid)] = {"xg": val, "provider_field": "expected_goals",
                              "provider_value": raw, "missing_reason": missing}
    for tid, team in teams.items():
        if not team:
            teams[tid] = {"missing_reason": "team_record_absent"}
            if kind == "statistics":
                teams[tid].update(xg=None, provider_field="expected_goals", provider_value=None)
            else:
                teams[tid].update(starters=[], substitutes=[], formation=None,
                                  complete_starting_eleven=False)
    return teams


def make_snapshot(kind: str, envelope: dict, fixture: dict, *,
                  context_observed_at: datetime | str | None = None,
                  published_at: datetime | str | None = None) -> dict:
    """Freeze an authenticated adapter response, retaining original fetched_at.

    ``published_at`` may record a separately proven publication time, but never
    backdates our knowledge. Fixture status/context also must have been observed
    by the claimed cutoff; its observation time participates in known_at.
    """
    if kind not in ENDPOINTS:
        raise DataError("未知基本面种类")
    identity = _fixture(fixture)
    if not isinstance(envelope, dict) or envelope.get("errors"):
        raise DataError("基本面响应缺失或含供应商错误")
    params = envelope.get("params")
    valid_params = (isinstance(params, dict) and params.get("fixture") == identity["match_id"]
                    and set(params) <= ({"fixture", "half"} if kind == "statistics" else {"fixture"})
                    and ("half" not in params or params["half"] in ("true", "false", True, False)))
    if envelope.get("endpoint") != ENDPOINTS[kind] or not valid_params:
        raise DataError("基本面响应 endpoint/params 身份不符")
    rows = envelope.get("response")
    if not isinstance(rows, list):
        raise DataError("基本面 response 必须为完整列表")
    if envelope.get("paging", {}).get("total", 1) != 1:
        raise DataError("不接受未完成分页的基本面响应")
    observed = _utc(envelope.get("fetched_at"))
    context_time = observed if context_observed_at is None else _utc(context_observed_at)
    published = None if published_at is None else _utc(published_at)
    known = max([observed, context_time] + ([published] if published else []))
    raw = copy.deepcopy(rows)
    result = {"schema_version": SCHEMA_VERSION, "provider": "api-football",
              "kind": kind, "endpoint": ENDPOINTS[kind],
              "params": copy.deepcopy(params), "fixture": identity,
              "observed_at": _iso(observed), "context_observed_at": _iso(context_time),
              "published_at": _iso(published) if published else None,
              "known_at": _iso(known), "known_at_basis": "observed_response_and_fixture_context",
              "status": "observed", "teams": _normalize(kind, raw, identity),
              "raw_response": raw, "raw_sha256": _digest(raw)}
    result["snapshot_id"] = _digest(result)
    return result


def save_snapshot(directory: str | Path, snapshot: dict) -> Path:
    """Append by content identity. Never overwrite an earlier evidence version."""
    _validate_snapshot(snapshot)
    path = Path(directory) / snapshot["kind"] / str(snapshot["fixture"]["match_id"]) / (snapshot["snapshot_id"] + ".json")
    path.parent.mkdir(parents=True, exist_ok=True)
    text = _canonical(snapshot) + "\n"
    try:
        with path.open("x", encoding="utf-8") as handle:
            handle.write(text)
    except FileExistsError:
        if path.read_text(encoding="utf-8") != text:
            raise DataError("快照地址存在但内容不一致") from None
    return path


def _validate_snapshot(snapshot: dict) -> None:
    if not isinstance(snapshot, dict) or snapshot.get("schema_version") != SCHEMA_VERSION:
        raise DataError("基本面快照版本无效")
    identity = {k: v for k, v in snapshot.items() if k != "snapshot_id"}
    if snapshot.get("snapshot_id") != _digest(identity) or snapshot.get("raw_sha256") != _digest(snapshot.get("raw_response")):
        raise DataError("基本面快照指纹校验失败")
    reconstructed = make_snapshot(snapshot["kind"], {"endpoint": snapshot["endpoint"],
        "params": snapshot["params"], "response": snapshot["raw_response"],
        "fetched_at": snapshot["observed_at"]}, snapshot["fixture"],
        context_observed_at=snapshot["context_observed_at"], published_at=snapshot["published_at"])
    if reconstructed != snapshot:
        raise DataError("基本面快照规范化结果不一致")


def load_snapshots(directory: str | Path) -> list[dict]:
    """Read only this evidence directory; malformed snapshots fail visibly."""
    root = Path(directory)
    result = []
    if not root.exists():
        return result
    for path in sorted(root.glob("*/*/*.json")):
        value = json.loads(path.read_text(encoding="utf-8"))
        _validate_snapshot(value)
        result.append(value)
    return result


def collect_fixture_evidence(client, fixture: dict, *, coverage: dict,
                             observed_at: datetime | str,
                             collection_at: datetime | str | None = None,
                             kinds=("injuries", "lineups"),
                             reserved_requests: int = 0, force: bool = False,
                             clock=None) -> dict:
    """Optional bounded collection using the project's existing APIClient adapter.

    Adapter: fetch_fundamentals(endpoint, fixture_id, force, max_age_seconds).
    Calls share APIClient quota, retries, redaction, cache and request reservation.
    Statistics can be requested only for completed 90-minute fixtures. Collection
    does not grant a snapshot eligibility at any earlier forecast cutoff.
    """
    identity, context_time = _fixture(fixture), _utc(observed_at)
    now = context_time if collection_at is None else _utc(collection_at)
    failure_clock = clock or (lambda: datetime.now(timezone.utc))
    snapshots, missing, diagnostics, quality = [], [], [], []
    for kind in kinds:
        if kind not in ENDPOINTS:
            raise DataError("未知基本面种类")
        flag = (coverage.get("injuries") if kind == "injuries" else
                coverage.get("fixtures", {}).get("lineups" if kind == "lineups" else "statistics_fixtures"))
        reason = None
        if flag is not True:
            reason = "coverage_false" if flag is False else "coverage_unknown"
        elif kind == "statistics" and identity["status"] != "FT":
            reason = "not_completed_90_minute_fixture"
        elif kind == "injuries" and identity["status"] != "NS":
            reason = "target_fixture_not_scheduled"
        elif kind == "injuries" and now >= _utc(identity["kickoff"]):
            reason = "prematch_collection_window_closed"
        elif (kind == "lineups" and now >= _utc(identity["kickoff"])
              and identity["status"] not in ("FT", "AET", "PEN")):
            reason = "lineup_collection_requires_prematch_or_finished_fixture"
        elif kind == "lineups" and identity["status"] not in ("NS", "FT", "AET", "PEN"):
            reason = "lineup_collection_requires_prematch_or_finished_fixture"
        elif kind == "lineups" and (_utc(identity["kickoff"]) - now).total_seconds() > 90 * 60:
            reason = "lineup_window_not_open"
        if reason:
            missing.append({"kind": kind, "reason": reason, "checked_at": _iso(now)})
            continue
        envelope = None
        stage = "fetch"
        try:
            with client.reserve_requests(reserved_requests):
                envelope = client.fetch_fundamentals(ENDPOINTS[kind], identity["match_id"],
                    force=force, max_age_seconds=TTL_SECONDS[kind])
            stage = "normalize"
            snapshot = make_snapshot(kind, envelope, identity, context_observed_at=context_time)
            snapshots.append(snapshot)
            if kind == "statistics":
                quality.extend({"match_id": identity["match_id"], "kind": kind,
                    "snapshot_id": snapshot["snapshot_id"], **finding}
                    for finding in statistics_diagnostics(envelope["response"]))
            elif kind == "lineups":
                for tid, team in snapshot["teams"].items():
                    count = sum(p["player_id"] is None for p in team["substitutes"])
                    if count:
                        quality.append({"match_id": identity["match_id"], "kind": kind,
                            "snapshot_id": snapshot["snapshot_id"], "team_id": int(tid),
                            "code": "unidentified_substitute", "count": count,
                            "action": "preserve_null_id_exclude_from_player_identity_features"})
        except (APIError, ValueError, KeyError, TypeError, AttributeError, OSError) as exc:
            # Actual exception-observation time, separate from the pre-request
            # eligibility check and the response's original fetched_at. Never
            # invent an end time from max(start, response time).
            failed_at = _utc(failure_clock())
            reason = ("budget_deferred" if isinstance(exc, RequestBudgetError) else
                      "provider_error" if isinstance(exc, APIError) else
                      "evidence_fetch_failed" if stage == "fetch" else "invalid_evidence")
            diagnostic = failure_diagnostic(kind, identity, stage=stage, error=exc,
                checked_at=failed_at, envelope=envelope, context_observed_at=context_time,
                collection_started_at=now)
            diagnostics.append(diagnostic)
            missing.append({"kind": kind, "reason": reason, "checked_at": _iso(failed_at),
                            "collection_started_at": _iso(now),
                            "stage": stage, "category": diagnostic["category"],
                            "code": diagnostic["code"], "payload_sha256": diagnostic["payload_sha256"]})
    return {"snapshots": snapshots, "missing": missing, "diagnostics": diagnostics,
            "quality_diagnostics": quality, **quantitative_scope()}


def _cell(value, reason=None, **details) -> dict:
    return {"value": value, "missing_reason": reason, **details}


def build_features(fixture: dict, *, cutoff: datetime | str,
                   snapshots: list[dict], fixture_history: list[dict] | None = None,
                   recent_matches: int = 5) -> dict:
    """Compute auditable features from information known before this forecast.

    fixture_history items require normalized identity, status FT/AET/PEN and a
    real ``observed_at`` for that finished status. Missing observation timestamps
    are excluded, not replaced with kickoff plus an assumed completion delay.
    Rest is the kickoff-to-kickoff gap in the supplied competition history, not
    a claim that the player's total all-competition workload is complete.
    """
    identity, when = _fixture(fixture), _utc(cutoff)
    _integer(recent_matches, "recent_matches", 1)
    if when >= _utc(identity["kickoff"]):
        raise DataError("基本面赛前特征截点必须早于本场开赛")
    latest, excluded = {}, Counter()
    for snapshot in snapshots:
        _validate_snapshot(snapshot)
        if _utc(snapshot["known_at"]) > when:
            excluded["known_after_cutoff"] += 1
            continue
        key = (snapshot["kind"], snapshot["fixture"]["match_id"])
        if (key in latest and _utc(snapshot["known_at"]) == _utc(latest[key]["known_at"])
                and snapshot["snapshot_id"] != latest[key]["snapshot_id"]):
            raise DataError("同截点基本面快照相互冲突，不能任意选择")
        if key not in latest or (_utc(snapshot["known_at"]), snapshot["snapshot_id"]) > (_utc(latest[key]["known_at"]), latest[key]["snapshot_id"]):
            latest[key] = snapshot
    history = {}
    for row in fixture_history or []:
        item = _fixture(row)
        if not row.get("observed_at"):
            excluded["history_observation_time_missing"] += 1
            continue
        observed = _utc(row["observed_at"])
        if observed > when or item["match_id"] == identity["match_id"] or _utc(item["kickoff"]) >= when:
            excluded["history_not_known_prior_fixture"] += 1
            continue
        if item["status"] not in ("FT", "AET", "PEN") or observed < _utc(item["kickoff"]):
            excluded["history_not_observed_finished"] += 1
            continue
        prior = history.get(item["match_id"])
        if prior is None or observed > _utc(prior["observed_at"]):
            history[item["match_id"]] = {**item, "observed_at": _iso(observed)}
    # Finished fixture context archived with statistics also supplies legitimate
    # history, with the response's actual knowledge time, never inferred dates.
    for snapshot in latest.values():
        item = snapshot["fixture"]
        if (item["match_id"] != identity["match_id"] and item["status"] in ("FT", "AET", "PEN")
                and _utc(item["kickoff"]) < when and _utc(snapshot["known_at"]) >= _utc(item["kickoff"])):
            prior = history.get(item["match_id"])
            if prior is None or _utc(snapshot["known_at"]) > _utc(prior["observed_at"]):
                history[item["match_id"]] = {**item, "observed_at": snapshot["known_at"]}
    result = {"schema_version": SCHEMA_VERSION, "fixture": identity,
              "forecast_cutoff": _iso(when), "recent_matches": recent_matches,
              "teams": {}, "excluded_evidence": dict(excluded), **quantitative_scope()}
    for tid in (identity["home_id"], identity["away_id"]):
        team = {}
        for kind in ("injuries", "lineups"):
            snapshot = latest.get((kind, identity["match_id"]))
            if snapshot:
                source_id = snapshot["fixture"]
                if any(source_id[k] != identity[k] for k in ("kickoff", "home_id", "away_id")):
                    raise DataError("当前基本面快照与预测身份不一致")
            team[kind] = ({"snapshot_id": snapshot["snapshot_id"], "known_at": snapshot["known_at"],
                           "observation_age_seconds": (when - _utc(snapshot["observed_at"])).total_seconds(),
                           **copy.deepcopy(snapshot["teams"][str(tid)])} if snapshot else
                          {"missing_reason": "no_snapshot_known_at_cutoff", "snapshot_id": None})
        prior_stats, prior_lineups = [], []
        for (kind, mid), snapshot in latest.items():
            source_id = snapshot["fixture"]
            if mid == identity["match_id"] or tid not in (source_id["home_id"], source_id["away_id"]):
                continue
            finished = history.get(mid)
            if finished and any(finished[k] != source_id[k] for k in ("kickoff", "home_id", "away_id")):
                raise DataError("历史基本面与已完赛记录身份冲突")
            if _utc(source_id["kickoff"]) >= when or not finished:
                continue
            if kind == "statistics":
                # FT excludes unspecified extra-time contamination in provider totals.
                if source_id["status"] == "FT" and _utc(snapshot["observed_at"]) >= _utc(source_id["kickoff"]):
                    prior_stats.append(snapshot)
            elif kind == "lineups" and snapshot["teams"][str(tid)].get("complete_starting_eleven"):
                prior_lineups.append(snapshot)
        for collection in (prior_stats, prior_lineups):
            collection.sort(key=lambda s: (s["fixture"]["kickoff"], s["fixture"]["match_id"]), reverse=True)
        team_history = sorted([h for h in history.values() if tid in (h["home_id"], h["away_id"])],
                              key=lambda h: (h["kickoff"], h["match_id"]), reverse=True)
        recent_history = team_history[:recent_matches]
        recent_ids = {h["match_id"] for h in recent_history}
        selected = [s for s in prior_stats if s["fixture"]["match_id"] in recent_ids]
        paired = []
        for snapshot in selected:
            other = snapshot["fixture"]["away_id"] if snapshot["fixture"]["home_id"] == tid else snapshot["fixture"]["home_id"]
            own, opponent = snapshot["teams"][str(tid)]["xg"], snapshot["teams"][str(other)]["xg"]
            if own is not None and opponent is not None:
                paired.append((own, opponent, snapshot["snapshot_id"]))
        team["prior_xg"] = {"for_mean": sum(p[0] for p in paired) / len(paired) if paired else None,
            "against_mean": sum(p[1] for p in paired) / len(paired) if paired else None,
            "paired_sample_count": len(paired), "observed_recent_fixture_count": len(selected),
            "recent_finished_fixture_count": len(recent_history),
            "coverage_fraction": len(paired) / len(recent_history) if recent_history else None,
            "requested_window": recent_matches, "snapshot_ids": [p[2] for p in paired],
            "missing_reason": None if paired else "no_complete_prior_xg_pair_known_at_cutoff",
            "window_scope": "most_recent_supplied_finished_fixture_history",
            "all_competition_history_complete": False, "provider": "api-football"}
        last = team_history[0] if team_history else None
        team["rest_days"] = _cell(((_utc(identity["kickoff"]) - _utc(last["kickoff"])).total_seconds() / 86400) if last else None,
            None if last else "no_observed_finished_prior_fixture", prior_match_id=last["match_id"] if last else None,
            definition="kickoff_gap_days", history_scope="supplied_fixture_history", all_competitions_complete=False)
        current = team["lineups"]
        starters = {p["player_id"] for p in current.get("starters", [])} if current.get("complete_starting_eleven") else None
        prior = [s for s in prior_lineups if s["fixture"]["match_id"] in recent_ids]
        if starters is None or not prior:
            reason = "current_lineup_missing_or_incomplete" if starters is None else "prior_lineups_missing"
            team["lineup_continuity"] = {"previous_starter_fraction": None, "usual_starter_fraction": None,
                "prior_lineup_count": len(prior), "missing_reason": reason}
        else:
            lineups = [{p["player_id"] for p in s["teams"][str(tid)]["starters"]} for s in prior]
            frequency = Counter(pid for xi in lineups for pid in xi)
            # Average historical starter frequency avoids an arbitrary tie-broken usual XI.
            continuity = sum(frequency[pid] / len(lineups) for pid in starters) / 11
            actual_previous = prior[0]["fixture"]["match_id"] == last["match_id"]
            team["lineup_continuity"] = {"previous_starter_fraction": len(starters & lineups[0]) / 11 if actual_previous else None,
                "previous_missing_reason": None if actual_previous else "actual_previous_fixture_lineup_missing",
                "previous_match_id": last["match_id"],
                "latest_observed_lineup_overlap": len(starters & lineups[0]) / 11,
                "latest_observed_lineup_match_id": prior[0]["fixture"]["match_id"],
                "usual_starter_fraction": continuity, "prior_lineup_count": len(prior),
                "recent_finished_fixture_count": len(recent_history),
                "coverage_fraction": len(prior) / len(recent_history) if recent_history else None,
                "definition": "current_XI_average_prior_start_frequency",
                "snapshot_ids": [s["snapshot_id"] for s in prior], "missing_reason": None}
        result["teams"][str(tid)] = team
    return result
