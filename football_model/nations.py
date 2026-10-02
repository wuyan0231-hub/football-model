"""Isolated UEFA Nations League venue adapter; no fitting or parameter changes.

The registry records exact observed provider identities and reviewed or
provider-verified football-association venue locations. Unknown locations never imply a
home advantage. A venue in the nominal away association is deliberately not
represented by this baseline, which cannot apply home advantage to that side.
"""
from __future__ import annotations

from collections import Counter
import hashlib
import json
from pathlib import Path

from .data import DataError, normalize_fixtures, normalize_upcoming
from .model import _connected_components


REGISTRY_PATH = Path(__file__).with_name("nations_venues.json")
PROVIDER_LEAGUE_ID = 5


def _venue_key(venue):
    if not isinstance(venue, dict):
        return None
    vid, name, city = venue.get("id"), venue.get("name"), venue.get("city")
    # The provider also uses 0 for an unknown ID. Keep that raw identity;
    # it can only classify after a name/city lookup has verified a registry entry.
    if vid is not None and (type(vid) is not int or vid < 0):
        return None
    if not isinstance(name, str) or not name.strip() or not isinstance(city, str) or not city.strip():
        return None
    # Exact text is intentional: changes need review rather than fuzzy matching.
    return vid, name, city


def _registry(registry):
    if registry is None:
        try:
            registry = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise DataError("欧国联场地登记表缺失或不是有效JSON。") from exc
    if not isinstance(registry, dict) or not isinstance(registry.get("teams"), dict) or not registry["teams"]:
        raise DataError("欧国联登记表缺少国家队身份列表。")
    teams = registry["teams"]
    for team_id, name in teams.items():
        if (not isinstance(team_id, str) or not team_id.isdigit() or int(team_id) <= 0
                or str(int(team_id)) != team_id or not isinstance(name, str) or not name.strip()):
            raise DataError("欧国联登记表国家队ID或名称无效。")
    if len(set(teams.values())) != len(teams):
        raise DataError("欧国联登记表国家队名称重复，不能唯一确认协会。")
    if not isinstance(registry.get("venues"), list):
        raise DataError("欧国联登记表缺少场地列表。")
    venues = {}
    for entry in registry["venues"]:
        key = _venue_key(entry)
        if key is None or entry.get("association") not in teams.values():
            raise DataError("欧国联登记表场地身份或所属协会无效。")
        if key in venues:
            raise DataError("欧国联登记表包含重复场地身份。")
        venues[key] = entry
    try:
        digest = hashlib.sha256(json.dumps(registry, sort_keys=True, ensure_ascii=False,
                                         allow_nan=False).encode()).hexdigest()
    except (TypeError, ValueError) as exc:
        raise DataError("欧国联登记表包含无法保存的资料。") from exc
    return teams, venues, digest


def _validate_raw(raw, teams):
    if not isinstance(raw, list):
        raise DataError("欧国联原始赛程必须为列表。")
    records = {}
    for item in raw:
        if not isinstance(item, dict) or not isinstance(item.get("fixture"), dict):
            raise DataError("欧国联fixture结构缺失。")
        fixture, league = item["fixture"], item.get("league")
        mid = fixture.get("id")
        if type(mid) is not int or mid <= 0:
            raise DataError("欧国联比赛ID无效。")
        if (not isinstance(league, dict) or type(league.get("id")) is not int
                or league["id"] != PROVIDER_LEAGUE_ID
                or type(league.get("season")) is not int or league["season"] < 2018):
            raise DataError(f"match_id={mid} 不是已确认的欧国联赛事或届次。")
        identities = item.get("teams")
        if not isinstance(identities, dict):
            raise DataError(f"match_id={mid} 国家队身份结构缺失。")
        for side in ("home", "away"):
            team = identities.get(side)
            if (not isinstance(team, dict) or type(team.get("id")) is not int
                    or str(team["id"]) not in teams or teams[str(team["id"])] != team.get("name")):
                raise DataError(f"match_id={mid} {side} 国家队ID与登记名称不匹配。")
        # Existing generic deduplication checks scores/status but omits venues.
        # Reject these differences before the generic normalizers can collapse them.
        signature = {"venue": fixture.get("venue"), "league_id": league["id"],
                     "season": league["season"], "round": league.get("round")}
        if mid in records and records[mid][0] != signature:
            raise DataError(f"match_id={mid} 重复记录的场地、届次或阶段互相冲突。")
        records[mid] = (signature, item)
    return {mid: record[1] for mid, record in records.items()}


def _classify(item, venues, registry_sha):
    fixture = item["fixture"]
    key = _venue_key(fixture.get("venue"))
    if key is None:
        return None, "场地ID/名称/城市缺失或无效，不能推定为主场。"
    entry = venues.get(key)
    if entry is None:
        return None, "场地ID、名称与城市的精确组合未登记核实。"
    home, away = item["teams"]["home"]["name"], item["teams"]["away"]["name"]
    association = entry["association"]
    if association == away:
        return None, "场地位于名义客队协会，当前基线不支持反向主场优势。"
    neutral = association != home
    evidence = {"venue": {"id": key[0], "name": key[1], "city": key[2]},
                "association": association, "home_association": home,
                "away_association": away, "neutral": neutral,
                "classification": "third_association_neutral" if neutral else "home_association",
                "registry_sha256": registry_sha,
                "method": "exact_reviewed_venue_tuple_and_national_team_identity"}
    return evidence, None


def validate_nations_forecast(row, registry=None):
    """Recheck saved venue evidence against this exact registry; return neutral.

    Probability recomputation remains the integration layer's responsibility.
    A changed registry or changed identity requires a new forecast record.
    """
    teams, venues, registry_sha = _registry(registry)
    if not isinstance(row, dict) or "venue_error" in row or not isinstance(row.get("venue_evidence"), dict):
        raise DataError("欧国联预测缺少可核实场地依据，或已标为场地不支持。")
    identities = {}
    for side in ("home", "away"):
        team_id, name = row.get(f"{side}_id"), row.get(f"{side}_name")
        if type(team_id) is not int or str(team_id) not in teams or teams[str(team_id)] != name:
            raise DataError("欧国联预测国家队ID与登记名称不匹配。")
        identities[side] = {"id": team_id, "name": name}
    if identities["home"]["id"] == identities["away"]["id"]:
        raise DataError("欧国联预测双方国家队不能相同。")
    item = {"fixture": {"venue": row["venue_evidence"].get("venue")}, "teams": identities}
    expected, error = _classify(item, venues, registry_sha)
    if error:
        raise DataError(f"欧国联预测场地核验失败：{error}")
    if (type(row.get("neutral")) is not bool or row["neutral"] != expected["neutral"]
            or row["venue_evidence"] != expected):
        raise DataError("欧国联预测场地、中立场标记或登记表来源与当前核验不一致。")
    return expected["neutral"]


def prepare_nations(raw, asof, days=7, registry=None):
    """Return ``(matches, upcoming, audit)`` using explicit venue evidence.

    Historical unknown/unsupported venues are excluded with a match-level
    reason. Upcoming unknown/unsupported venues remain visible with
    ``venue_error`` and must not receive a probability. Identity/score conflicts
    are malformed data and fail the run rather than being silently excluded.
    """
    teams, venues, registry_sha = _registry(registry)
    originals = _validate_raw(raw, teams)
    eligible = normalize_fixtures(raw, asof)
    scheduled = normalize_upcoming(raw, asof, days)
    matches, upcoming, exclusions = [], [], []
    season_counts, stage_counts = {}, {}

    def group_count(groups, key, field):
        group = groups.setdefault(str(key), {"eligible_regulation": 0, "admitted": 0,
                                            "excluded": 0, "neutral": 0})
        group[field] += 1

    for row in eligible:
        item = originals[row["match_id"]]
        season, stage = item["league"]["season"], item["league"].get("round") or "unspecified"
        for groups, key in ((season_counts, season), (stage_counts, stage)):
            group_count(groups, key, "eligible_regulation")
        evidence, error = _classify(item, venues, registry_sha)
        if error:
            exclusions.append({"match_id": row["match_id"], "season": season,
                               "stage": stage, "reason": error,
                               "venue": item["fixture"].get("venue")})
            for groups, key in ((season_counts, season), (stage_counts, stage)):
                group_count(groups, key, "excluded")
            continue
        matches.append({**row, "neutral": evidence["neutral"], "venue_evidence": evidence})
        for groups, key in ((season_counts, season), (stage_counts, stage)):
            group_count(groups, key, "admitted")
            if evidence["neutral"]:
                group_count(groups, key, "neutral")

    for row in scheduled:
        evidence, error = _classify(originals[row["match_id"]], venues, registry_sha)
        upcoming.append({**row, **({"venue_error": error} if error else
                                 {"neutral": evidence["neutral"], "venue_evidence": evidence})})
    components = _connected_components(matches)
    audit = {"scope": "uefa_nations_league_only", "provider_league_id": PROVIDER_LEAGUE_ID,
             "registry_sha256": registry_sha, "raw_records": len(raw),
             "unique_raw_fixtures": len(originals), "duplicate_records": len(raw) - len(originals),
             "raw_status_counts": dict(sorted(Counter(
                 item["fixture"].get("status", {}).get("short", "missing")
                 for item in originals.values()).items())),
             "eligible_regulation": len(eligible), "admitted": len(matches),
             "excluded": len(exclusions), "neutral": sum(row["neutral"] for row in matches),
             "exclusions": exclusions, "by_season": season_counts, "by_stage": stage_counts,
             "upcoming_total": len(upcoming),
             "upcoming_supported_venue": sum("venue_error" not in row for row in upcoming),
             "upcoming_unsupported_venue": sum("venue_error" in row for row in upcoming),
             "upcoming_venue_errors": [{"match_id": row["match_id"], "reason": row["venue_error"]}
                                       for row in upcoming if "venue_error" in row],
             "training_graph": {"connected": len(components) == 1,
                                "component_count": len(components), "components": components},
             "limitations": ["场地位置按已审核精确登记表分类，不量化球迷、旅行或特殊主场环境。",
                             "完整训练对阵图检查不代替留出切分训练图检查；原训练保护仍适用。",
                             "仅90分钟加伤停补时；阵容、伤停、xG与战术均未量化。"]}
    return matches, upcoming, audit
