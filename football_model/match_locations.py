"""Match-bound, reviewed official host-association evidence for Nations League.

These records confirm only a match's host association, never a stadium identity.
They are frozen with the venue registry and cannot be reused after a fixture's
teams, kickoff, competition or raw venue context changes.
"""
from __future__ import annotations

import copy
import hashlib
from datetime import timedelta
import json
from pathlib import Path
import re
from urllib.parse import urlsplit

from .data import DataError, _iso, _utc

SEED_PATH = Path(__file__).with_name("nations_match_locations.json")
METHOD = "official_match_host_association"


def observed_venue(venue):
    return {k: venue.get(k) for k in ("id", "name", "city")} if isinstance(venue, dict) else None


def binding(item):
    return {"match_id": item["fixture"].get("id"), "league_id": item.get("league", {}).get("id"),
            "season": item.get("league", {}).get("season"),
            "home_id": item["teams"]["home"].get("id"), "away_id": item["teams"]["away"].get("id"),
            "kickoff": _iso(_utc(item["fixture"].get("date"))),
            "original_venue": observed_venue(item["fixture"].get("venue"))}


def validate(entry, teams):
    if not isinstance(entry, dict):
        raise DataError("比赛举办地区证据结构无效。")
    for key in ("match_id", "home_id", "away_id"):
        if type(entry.get(key)) is not int or entry[key] <= 0:
            raise DataError("比赛举办地区证据的比赛或球队ID无效。")
    if (entry.get("league_id") != 5 or type(entry.get("league_id")) is not int
            or type(entry.get("season")) is not int or entry["season"] < 2018
            or entry["home_id"] == entry["away_id"]
            or any(str(entry[k]) not in teams for k in ("home_id", "away_id"))
            or entry.get("association") not in teams.values()):
        raise DataError("比赛举办地区证据不属于欧国联或足协身份不明确。")
    _utc(entry.get("kickoff"))
    if "original_venue" not in entry:
        raise DataError("比赛举办地区证据缺少原始场地上下文。")
    venue = entry["original_venue"]
    if venue is not None and (not isinstance(venue, dict) or set(venue) != {"id", "name", "city"}
            or venue["id"] is not None and (type(venue["id"]) is not int or venue["id"] < 0)
            or any(venue[k] is not None and not isinstance(venue[k], str) for k in ("name", "city"))):
        raise DataError("比赛举办地区证据的原始场地上下文无效。")
    verified = _utc(entry.get("verified_at"))
    sources = entry.get("sources")
    if not isinstance(sources, list) or not sources:
        raise DataError("比赛举办地区证据缺少官方来源。")
    for source in sources:
        if (not isinstance(source, dict)
                or not isinstance(source.get("url"), str)
                or urlsplit(source["url"]).scheme != "https" or not urlsplit(source["url"]).hostname
                or any(not isinstance(source.get(k), str) or not source[k].strip()
                       for k in ("statement", "publisher"))
                or not isinstance(source.get("source_sha256"), str)
                or re.fullmatch(r"[0-9a-f]{64}", source["source_sha256"]) is None
                or "source_published_at" not in source):
            raise DataError("比赛举办地区证据缺少来源URL、官方发布方、原文或指纹。")
        if hashlib.sha256(source["statement"].encode("utf-8")).hexdigest() != source["source_sha256"]:
            raise DataError("比赛举办地区证据的原文摘录UTF-8 SHA256不一致。")
        fetched = _utc(source.get("fetched_at"))
        if fetched > verified or (source["source_published_at"] is not None
                and _utc(source["source_published_at"]) > fetched):
            raise DataError("比赛举办地区证据的取得、发布与审核时间不一致。")
    return entry


def entries(registry, teams):
    rows = registry.get("match_locations", []) if isinstance(registry, dict) else []
    if not isinstance(rows, list):
        raise DataError("比赛举办地区登记列表无效。")
    result = {}
    for row in rows:
        validate(row, teams)
        if row["match_id"] in result:
            raise DataError("比赛举办地区登记存在重复比赛ID。")
        result[row["match_id"]] = row
    return result


def load_seed(teams):
    try:
        seed = json.loads(SEED_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise DataError("比赛举办地区官方证据种子缺失或损坏。") from exc
    if not isinstance(seed, dict) or seed.get("schema_version") != 1 or not isinstance(seed.get("matches"), list):
        raise DataError("比赛举办地区官方证据种子版本无效。")
    return entries({"match_locations": seed["matches"]}, teams)


def matches(entry, item):
    expected = binding(item)
    actual = {key: entry.get(key) for key in expected}
    actual["kickoff"] = _iso(_utc(actual["kickoff"]))
    return expected == actual


def temporal(entry):
    # A match-specific source applies to its bound kickoff only, represented as
    # a one-second interval so it cannot assert timeless stadium geography.
    kickoff = _utc(entry["kickoff"])
    known = max(_utc(s["fetched_at"]) for s in entry["sources"])
    published = [s["source_published_at"] for s in entry["sources"]]
    return {"schema_version": 1, "known_at": _iso(known),
            "valid_from": _iso(kickoff), "valid_to": _iso(kickoff + timedelta(seconds=1)),
            "source_published_at": max(published, key=_utc) if all(published) else None,
            "validity_status": "documented_interval", "basis": "reviewed_official_match_source",
            "evidence_available_at": _iso(_utc(entry["verified_at"]))}


def merge_seed(registry, originals, now):
    """Freeze only currently matching records; leave previous snapshots intact."""
    known = entries(registry, registry["teams"])
    audit = {"added": [], "pending": []}
    for mid, entry in load_seed(registry["teams"]).items():
        if mid not in originals:
            continue
        if not matches(entry, originals[mid]):
            audit["pending"].append({"match_id": mid, "reason_code": "match_evidence_context_changed"})
            continue
        if _utc(entry["verified_at"]) > _utc(now):
            audit["pending"].append({"match_id": mid, "reason_code": "match_evidence_in_future"})
            continue
        replacement = copy.deepcopy(entry)
        # A transient API failure must never erase a conflict discovered in a
        # previous run. Clearing this requires a separately reviewed registry
        # repair; simply reloading the same official seed is not resolution.
        if known.get(mid, {}).get("conflict"):
            replacement["conflict"] = copy.deepcopy(known[mid]["conflict"])
        if known.get(mid) != replacement:
            known[mid] = replacement
            audit["added"].append(mid)
    if known or "match_locations" in registry:
        registry["match_locations"] = list(known.values())
    return audit
