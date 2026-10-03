"""Budget-protected Nations League venue discovery and frozen registries.

Fixture tuples remain unchanged. Positive provider IDs can verify a name alias
when the independently returned city agrees exactly or a narrowly reviewed
same-stadium city spelling is supported by frozen evidence. Missing IDs require a
unique exact name/city result; neither path infers geography from the home team.
"""
from __future__ import annotations

import copy
from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path, PurePosixPath
import re

from . import nations
from .data import APIError, DataError, RequestBudgetError, _iso, _utc, normalize_fixtures, normalize_upcoming


CURRENT_PATH = Path("registries/nations_venues.json")
RETRY_PATH = Path("registries/nations_venue_retry.json")
SNAPSHOT_DIRECTORY = PurePosixPath("registries/nations_venues")
CITY_ALIASES_PATH = Path(__file__).with_name("nations_venue_city_aliases.json")
_ID_METHOD = "api_football_venue_id_city_country"
_NAME_METHOD = "api_football_exact_name_city_country"
_COUNTRY_ALIASES = {
    "Republic of Ireland": "Rep. Of Ireland", "Ireland": "Rep. Of Ireland",
    "Czech-Republic": "Czechia", "Czech Republic": "Czechia",
    "Turkey": "Türkiye", "North-Macedonia": "FYR Macedonia",
    "North Macedonia": "FYR Macedonia", "Macedonia": "FYR Macedonia",
    "Bosnia-and-Herzegovina": "Bosnia & Herzegovina",
    "Bosnia and Herzegovina": "Bosnia & Herzegovina",
    "Northern-Ireland": "Northern Ireland", "Faroe-Islands": "Faroe Islands",
}


def _association(country, teams):
    if not isinstance(country, str) or not country.strip():
        return None
    # Exact documented values and explicit aliases only. UK cannot identify one
    # of the four football associations, and is deliberately absent here.
    candidate = _COUNTRY_ALIASES.get(country, country)
    return candidate if candidate in teams.values() else None


def _observed(venue):
    return {key: venue.get(key) for key in ("id", "name", "city")} if isinstance(venue, dict) else None


def _positive_id(value):
    return type(value) is int and value > 0


def _complete_strings(venue):
    return isinstance(venue, dict) and all(
        isinstance(venue.get(key), str) and venue[key].strip() for key in ("name", "city"))


def _city_alias_rules():
    try:
        catalog = json.loads(CITY_ALIASES_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise DataError("已审核场地城市别名表缺失或损坏。") from exc
    if not isinstance(catalog, dict) or catalog.get("schema_version") != 1 or not isinstance(catalog.get("aliases"), list):
        raise DataError("场地城市别名表版本或结构无效。")
    ids = set()
    for rule in catalog["aliases"]:
        if (not isinstance(rule, dict) or not isinstance(rule.get("rule_id"), str)
                or not rule["rule_id"] or rule["rule_id"] in ids
                or not _positive_id(rule.get("provider_id"))
                or not isinstance(rule.get("association"), str)
                or not _complete_strings(rule.get("fixture_venue"))
                or not _complete_strings(rule.get("provider_venue"))
                or rule["fixture_venue"].get("id") != rule["provider_id"]
                or rule["provider_venue"].get("id") != rule["provider_id"]
                or not isinstance(rule.get("sources"), list) or not rule["sources"]):
            raise DataError("场地城市别名缺少唯一审核编号、精确身份或独立来源。")
        reviewed = _utc(rule.get("reviewed_at"))
        for source in rule["sources"]:
            if (not isinstance(source, dict) or not isinstance(source.get("url"), str)
                    or not source["url"].startswith("https://")
                    or not isinstance(source.get("statement"), str) or not source["statement"].strip()
                    or not isinstance(source.get("source_sha256"), str)
                    or re.fullmatch(r"[0-9a-f]{64}", source["source_sha256"]) is None
                    or _utc(source.get("fetched_at")) > reviewed):
                raise DataError("场地城市别名独立来源或审核时间无效。")
        ids.add(rule["rule_id"])
    return catalog["aliases"]


def _city_alias(venue, provider, association):
    if not _positive_id(venue.get("id")) or provider.get("id") != venue["id"]:
        return None
    for rule in _city_alias_rules():
        if (rule["provider_id"] == venue["id"] and rule["association"] == association
                and rule["fixture_venue"] == _observed(venue)
                and rule["provider_venue"] == _observed(provider)):
            return copy.deepcopy(rule)
    return None


def _compatible_registered_locations(first, second):
    if first["association"] != second["association"]:
        return False
    if first["city"] == second["city"]:
        return True
    # Only the two reviewed complete tuples for this provider ID are aliases.
    # Never normalize accents globally or ignore city/name disagreements.
    return (_city_alias(first, second, first["association"]) is not None
            or _city_alias(second, first, first["association"]) is not None)


def _base_registry():
    try:
        base = json.loads(nations.REGISTRY_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise DataError("静态欧国联场地登记表缺失或损坏。") from exc
    nations._registry(base)
    return base


def _validate_verification(entry, teams):
    verification = entry.get("verification")
    if not isinstance(verification, dict) or verification.get("schema_version") not in (1, 2):
        raise DataError("自动场地登记缺少有效来源证据。")
    observed = _observed(entry)
    if verification.get("original_venue") != observed:
        raise DataError("自动场地登记的原始元组与来源证据不一致。")
    provider = verification.get("provider_record")
    if (not isinstance(provider, dict) or not _positive_id(provider.get("id"))
            or not _complete_strings(provider)):
        raise DataError("自动场地登记的供应商场馆记录无效。")
    association = _association(provider.get("country"), teams)
    if association is None or association != entry.get("association"):
        raise DataError("自动场地登记的国家与足协归属不一致或不能明确识别。")
    city_alias = verification.get("city_alias_evidence")
    if provider["city"] != observed["city"]:
        expected_alias = _city_alias(observed, provider, association)
        if expected_alias is None or city_alias != expected_alias or verification["schema_version"] != 2:
            raise DataError("自动场地登记的城市与供应商来源不一致或别名证据无效。")
    elif city_alias is not None:
        raise DataError("完全相同的城市不能附加不适用的别名证据。")
    if verification.get("endpoint") != "/venues":
        raise DataError("自动场地登记的查询端点无效。")
    if _positive_id(observed["id"]):
        expected_method, expected_params = _ID_METHOD, {"id": observed["id"]}
        if provider["id"] != observed["id"]:
            raise DataError("自动场地登记的供应商ID与原始ID不一致。")
    elif observed["id"] is None or type(observed["id"]) is int and observed["id"] == 0:
        expected_method, expected_params = _NAME_METHOD, {"name": observed["name"]}
        if provider["name"] != observed["name"]:
            raise DataError("缺少场地ID时必须有精确场馆名称证据。")
    else:
        raise DataError("自动场地登记的原始场地ID无效。")
    if verification.get("method") != expected_method or verification.get("params") != expected_params:
        raise DataError("自动场地登记的查询方法与原始元组不一致。")
    if verification.get("name_alias") is not (provider["name"] != observed["name"]):
        raise DataError("自动场地登记的名称别名标记不一致。")
    for field in ("fetched_at", "verified_at"):
        _utc(verification.get(field))
    nations._entry_temporal(entry)
    if type(verification.get("from_cache")) is not bool:
        raise DataError("自动场地登记缺少明确的缓存来源状态。")


def _effective_id(entry):
    if _positive_id(entry.get("id")):
        return entry["id"]
    evidence = entry.get("verification", {}).get("provider_record", {})
    return evidence.get("id") if _positive_id(evidence.get("id")) else None


def _validate_registry(registry, base):
    if not isinstance(registry, dict) or registry.get("schema_version") != base.get("schema_version"):
        raise DataError("持久场地登记表结构版本无效。")
    if registry.get("teams") != base["teams"]:
        raise DataError("持久场地登记表不能更改静态国家队身份。")
    teams, entries, digest = nations._registry(registry)
    _, base_entries, _ = nations._registry(base)
    for key, original in base_entries.items():
        if entries.get(key) != original:
            raise DataError("持久场地登记表缺少或修改了静态登记条目。")
    dynamic = []
    for key, entry in entries.items():
        if key not in base_entries:
            _validate_verification(entry, teams)
            dynamic.append(entry)
    # Static legacy entries are retained verbatim even if their provider ID was
    # historically reused. New entries cannot extend any such contradiction.
    for entry in dynamic:
        canonical_id = _effective_id(entry)
        for other in registry["venues"]:
            if other is entry or _effective_id(other) != canonical_id:
                continue
            if not _compatible_registered_locations(entry, other):
                raise DataError("自动场地登记的同ID城市或足协归属互相冲突。")
    return digest


def _read_registry(path, base):
    try:
        registry = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise DataError("持久场地登记表或冻结快照缺失或损坏。") from exc
    _validate_registry(registry, base)
    return registry


def _atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def _query_params(venue):
    return {"id": venue["id"]} if _positive_id(venue["id"]) else {"name": venue["name"]}


def _query_key(params):
    return json.dumps(params, sort_keys=True, ensure_ascii=False)


def _read_retry_state(path):
    """Scheduling state is separate from the registry and prediction digest."""
    if not path.exists():
        return {"schema_version": 1, "run": 0, "next_order": 0, "queries": {}}
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        if (not isinstance(state, dict) or state.get("schema_version") != 1
                or type(state.get("run")) is not int or state["run"] < 0
                or type(state.get("next_order")) is not int or state["next_order"] < 0
                or not isinstance(state.get("queries"), dict)):
            raise ValueError("invalid retry state")
        orders = set()
        for key, entry in state["queries"].items():
            params = json.loads(key)
            valid_params = isinstance(params, dict) and (
                set(params) == {"id"} and _positive_id(params["id"])
                or set(params) == {"name"} and isinstance(params["name"], str) and params["name"].strip())
            if not valid_params or _query_key(params) != key or not isinstance(entry, dict):
                raise ValueError("invalid retry query")
            if any(type(entry.get(field)) is not int for field in
                   ("first_seen_run", "first_seen_order", "last_attempt_run", "attempts")):
                raise ValueError("invalid retry counters")
            if (not 1 <= entry["first_seen_run"] <= state["run"]
                    or not 1 <= entry["first_seen_order"] <= state["next_order"]
                    or entry["first_seen_order"] in orders
                    or not 0 <= entry["last_attempt_run"] <= state["run"]
                    or entry["attempts"] < 0
                    or (entry["attempts"] == 0) != (entry["last_attempt_run"] == 0)
                    or entry["attempts"] > state["run"]):
                raise ValueError("inconsistent retry counters")
            orders.add(entry["first_seen_order"])
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise DataError("场地查询重试队列缺失字段或损坏，不能静默重置调度记录。") from exc
    return state


@contextmanager
def _reserve_query_budget(client, reserve_requests):
    guard = getattr(client, "reserve_requests", None)
    if callable(guard):
        # APIClient checks both budgets on each network attempt, after trying
        # the cache. Do not turn a low balance into a cache-read rejection.
        with guard(reserve_requests):
            yield
        return
    # Compatibility for clients without the actual per-attempt guard. Their
    # run limit remains protected; the caller also checks known daily balance.
    maximum = getattr(client, "max_requests", None)
    if type(maximum) is int:
        client.max_requests = maximum - reserve_requests
    try:
        yield
    finally:
        if type(maximum) is int:
            client.max_requests = maximum


def _freeze(runtime, registry, base):
    digest = _validate_registry(registry, base)
    relative = SNAPSHOT_DIRECTORY / f"{digest}.json"
    path = runtime / Path(relative)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        existing = _read_registry(path, base)
        if nations._registry(existing)[2] != digest:
            raise DataError("既有冻结场地快照与文件名指纹不一致。")
    else:
        # An existing digest-named file is never replaced. Exclusive creation
        # protects the same guarantee if two local callers race.
        try:
            with path.open("x", encoding="utf-8") as handle:
                handle.write(json.dumps(registry, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
        except FileExistsError:
            existing = _read_registry(path, base)
            if nations._registry(existing)[2] != digest:
                raise DataError("冻结场地快照出现并发指纹冲突。")
    _atomic_json(runtime / CURRENT_PATH, registry)
    return relative.as_posix(), digest


def _select_provider(envelope, venue, teams, params):
    if not isinstance(envelope, dict) or not isinstance(envelope.get("response"), list):
        return None, "invalid_response", "场馆查询没有有效列表响应。"
    if envelope.get("errors"):
        return None, "provider_errors", "场馆查询返回供应商错误。"
    if envelope.get("endpoint", "/venues") != "/venues" or envelope.get("get", "venues") not in ("venues", "/venues"):
        return None, "endpoint_mismatch", "场馆查询响应端点不一致。"
    if "params" in envelope and envelope["params"] != params:
        return None, "params_mismatch", "场馆查询响应参数不一致。"
    if "parameters" in envelope:
        returned = envelope["parameters"]
        if not isinstance(returned, dict) or set(returned) != set(params) or any(str(returned[k]) != str(v) for k, v in params.items()):
            return None, "params_mismatch", "场馆查询响应参数不一致。"
    paging = envelope.get("paging", {"total": 1})
    if not isinstance(paging, dict) or paging.get("total", 1) != 1:
        return None, "incomplete_response", "场馆查询分页不完整。"
    if _positive_id(venue["id"]):
        if len(envelope["response"]) != 1:
            return None, "not_unique", "按场馆ID查询必须返回唯一场馆记录。"
        candidates = [record for record in envelope["response"]
                      if isinstance(record, dict) and type(record.get("id")) is int and record["id"] == venue["id"]]
    else:
        candidates = [record for record in envelope["response"]
                      if isinstance(record, dict) and record.get("name") == venue["name"] and record.get("city") == venue["city"]]
    if len(candidates) != 1:
        return None, "not_unique", "场馆查询没有唯一精确匹配候选。"
    provider = candidates[0]
    if not _positive_id(provider.get("id")) or not _complete_strings(provider):
        return None, "invalid_provider_venue", "供应商场馆身份或名称城市无效。"
    association = _association(provider.get("country"), teams)
    if association is None:
        return None, "unknown_association", "场馆国家缺失、非支持足协或不能明确区分足协。"
    alias = _city_alias(venue, provider, association) if provider["city"] != venue["city"] else None
    if provider["city"] != venue["city"] and alias is None:
        return None, "city_mismatch", "同ID场馆城市与原始赛程城市不一致且无已审核精确别名。"
    try:
        _utc(envelope.get("fetched_at"))
    except DataError:
        return None, "missing_source_time", "场馆查询没有有效原始抓取时间。"
    if type(envelope.get("from_cache")) is not bool:
        return None, "missing_cache_state", "场馆查询没有明确缓存来源状态。"
    return (copy.deepcopy(provider), association, alias), None, None


def resolve_venues(raw, client, runtime, asof, days=7, *, max_lookups=None, reserve_requests=0, clock=None):
    """Resolve unknown tuples without fitting models or modifying raw fixtures.

    None applies no additional logical lookup cap; the client's network, daily
    and reserved-request budgets still apply. Explicit nonnegative caps remain
    supported for callers that need to bound an optional discovery batch.
    """
    if max_lookups is not None and (type(max_lookups) is not int or max_lookups < 0):
        raise DataError("场馆查询上限必须为None（无额外逻辑上限）或非负整数。")
    if type(reserve_requests) is not int or reserve_requests < 0:
        raise DataError("预留请求数必须为非负整数。")
    _utc(asof)
    clock = clock or (lambda: datetime.now(timezone.utc))
    runtime = Path(runtime)
    base = _base_registry()
    # Identity/date/score conflicts fail before reading or writing any mutable
    # registry, so malformed input cannot produce partial registrations.
    originals = nations._validate_raw(raw, base["teams"])
    upcoming = normalize_upcoming(raw, asof, days)
    historical = normalize_fixtures(raw, asof)
    current = runtime / CURRENT_PATH
    registry = _read_registry(current, base) if current.exists() else copy.deepcopy(base)
    teams, known, _ = nations._registry(registry)
    retry_state = _read_retry_state(runtime / RETRY_PATH)
    retry_state["run"] += 1
    retry_run = retry_state["run"]
    groups = {}
    known_keys = set()
    future_ids = {row["match_id"] for row in upcoming}
    for row in upcoming + list(reversed(historical)):
        venue = _observed(originals[row["match_id"]]["fixture"].get("venue"))
        key = nations._venue_key(venue)
        if key is not None and key in known:
            known_keys.add(key)
            continue
        identity = json.dumps(venue, sort_keys=True, ensure_ascii=False)
        group = groups.setdefault(identity, {"venue": venue, "match_ids": [],
                                             "priority": 0 if row["match_id"] in future_ids else 1})
        if row["match_id"] not in group["match_ids"]:
            group["match_ids"].append(row["match_id"])
    audit = {"schema_version": 1, "scope": "provider_backed_automatic_venue_registry",
             "added": [], "pending": [], "queries": [], "max_lookups": max_lookups,
             "reserve_requests": reserve_requests}
    lookup_results = {}
    attempted_queries = set()
    budget_deferred_queries = set()
    deferred = 0

    for group in groups.values():
        venue = group["venue"]
        if _complete_strings(venue) and (venue["id"] is None or type(venue["id"]) is int and venue["id"] >= 0):
            key = _query_key(_query_params(venue))
            if key not in retry_state["queries"]:
                retry_state["next_order"] += 1
                retry_state["queries"][key] = {"first_seen_run": retry_run,
                    "first_seen_order": retry_state["next_order"], "last_attempt_run": 0, "attempts": 0}

    def scheduling_order(group):
        venue = group["venue"]
        if not _complete_strings(venue) or not (venue["id"] is None or type(venue["id"]) is int and venue["id"] >= 0):
            return (-1, 0, group["priority"], 0)
        entry = retry_state["queries"][_query_key(_query_params(venue))]
        # Older unattempted deferred queries precede repeated failures, across
        # both future and historical classes. In the same attempt/batch future
        # fixtures retain their original kickoff order. Newly arriving work
        # cannot continually displace an older unattempted batch.
        return (entry["attempts"], entry["first_seen_run"], group["priority"], entry["first_seen_order"])

    def mark_attempt(key):
        if key not in attempted_queries:
            entry = retry_state["queries"][key]
            entry["attempts"] += 1
            entry["last_attempt_run"] = retry_run
            attempted_queries.add(key)

    def pending(group, code, reason):
        audit["pending"].append({"venue": copy.deepcopy(group["venue"]),
            "match_ids": copy.deepcopy(group["match_ids"]), "reason_code": code, "reason": reason})

    for group in sorted(groups.values(), key=scheduling_order):
        venue = group["venue"]
        if not _complete_strings(venue) or not (venue["id"] is None or type(venue["id"]) is int and venue["id"] >= 0):
            pending(group, "incomplete_fixture_venue", "场地ID、名称或城市缺失/无效，无法安全发现。")
            continue
        params = _query_params(venue)
        query_key = _query_key(params)
        if query_key not in lookup_results:
            if max_lookups is not None and len(attempted_queries) >= max_lookups:
                deferred += 1
                pending(group, "lookup_budget_deferred", "本次唯一场馆查询已达上限，保留后续查询。")
                continue
            maximum, used = getattr(client, "max_requests", None), getattr(client, "requests_made", None)
            daily = getattr(client, "daily_remaining", None)
            guarded = callable(getattr(client, "reserve_requests", None))
            if not guarded and ((type(maximum) is int and type(used) is int and used >= maximum - reserve_requests)
                                or type(daily) is int and daily <= reserve_requests):
                deferred += 1
                pending(group, "request_budget_deferred", "请求预算需保留后续赛事下载，场馆查询延后。")
                continue
            try:
                with _reserve_query_budget(client, reserve_requests):
                    envelope = client.fetch_venues(venue_id=venue["id"]) if "id" in params else client.fetch_venues(name=venue["name"])
                lookup_results[query_key] = envelope
                mark_attempt(query_key)
                audit["queries"].append({"params": params, "status": "received",
                    "fetched_at": envelope.get("fetched_at") if isinstance(envelope, dict) else None,
                    "from_cache": envelope.get("from_cache") if isinstance(envelope, dict) else None})
            except RequestBudgetError:
                lookup_results[query_key] = None
                budget_deferred_queries.add(query_key)
                current_used = getattr(client, "requests_made", None)
                # A guard before the first HTTP attempt is truly deferred.
                # A network failure followed by a protected retry has already
                # used a turn and must not displace untouched deferred work.
                if type(used) is int and type(current_used) is int and current_used > used:
                    mark_attempt(query_key)
                    audit["queries"].append({"params": params, "status": "request_budget_deferred",
                                             "request_attempts": current_used - used})
            except APIError:
                # APIError may carry provider text; keep a safe constant and
                # leave known venues usable instead of recording credentials.
                lookup_results[query_key] = None
                mark_attempt(query_key)
                audit["queries"].append({"params": params, "status": "api_error"})
        if query_key in budget_deferred_queries:
            deferred += 1
            pending(group, "request_budget_deferred", "请求预算需保留后续赛事下载，场馆查询延后。")
            continue
        envelope = lookup_results[query_key]
        if envelope is None:
            pending(group, "api_error", "场馆API查询失败，未自动登记。")
            continue
        resolved, code, reason = _select_provider(envelope, venue, teams, params)
        if resolved is None:
            pending(group, code, reason)
            continue
        provider, association, alias = resolved
        verified_at = _utc(clock())
        if (_utc(envelope["fetched_at"]) > verified_at
                or alias is not None and _utc(alias["reviewed_at"]) > verified_at):
            pending(group, "source_time_in_future", "来源取得或别名审核时间晚于实际核实时钟，未自动登记。")
            continue
        canonical_id = provider["id"]
        candidate = {**venue, "association": association}
        conflicts = [entry for entry in registry["venues"] if _effective_id(entry) == canonical_id
                     and not _compatible_registered_locations(candidate, entry)]
        if conflicts:
            pending(group, "registered_id_conflict", "同ID已登记场馆城市或足协归属冲突，保留原登记。")
            continue
        entry = {**copy.deepcopy(venue), "association": association,
            "verification": {"schema_version": 2,
                "method": _ID_METHOD if _positive_id(venue["id"]) else _NAME_METHOD,
                "original_venue": copy.deepcopy(venue), "provider_record": provider,
                "endpoint": "/venues", "params": params,
                "fetched_at": envelope["fetched_at"], "verified_at": _iso(verified_at),
                "from_cache": envelope["from_cache"], "name_alias": provider["name"] != venue["name"],
                "temporal": {"schema_version": 1, "known_at": envelope["fetched_at"],
                    "valid_from": None, "valid_to": None, "source_published_at": None,
                    "validity_status": "unknown", "basis": "provider_current_response"}}}
        if alias is not None:
            entry["verification"]["city_alias_evidence"] = alias
        _validate_verification(entry, teams)
        registry["venues"].append(entry)
        audit["added"].append({"match_ids": group["match_ids"], "entry": copy.deepcopy(entry)})
    snapshot, digest = _freeze(runtime, registry, base)
    _atomic_json(runtime / RETRY_PATH, retry_state)
    audit.update(registry_snapshot=snapshot, registry_sha256=digest,
                 retry_state_path=RETRY_PATH.as_posix(), retry_run=retry_run,
                 counts={"candidate_tuples": len(groups), "known_tuples": len(known_keys),
                         "queried": len(attempted_queries), "added": len(audit["added"]),
                         "pending": len(audit["pending"]), "deferred": deferred})
    return registry, audit


def load_forecast_registry(runtime, row):
    """Load the forecast's immutable snapshot; never substitute today's table."""
    if not isinstance(row, dict):
        raise DataError("比赛预测记录无效。")
    if "registry_snapshot" not in row:
        return None
    evidence = row.get("venue_evidence")
    digest = evidence.get("registry_sha256") if isinstance(evidence, dict) else None
    if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        raise DataError("比赛预测没有有效冻结场地指纹。")
    expected = (SNAPSHOT_DIRECTORY / f"{digest}.json").as_posix()
    if row["registry_snapshot"] != expected:
        raise DataError("比赛预测的冻结场地路径与指纹不一致。")
    root = Path(runtime).resolve()
    path = root / expected
    if not path.resolve().is_relative_to(root):
        raise DataError("冻结场地快照路径越出runtime。")
    registry = _read_registry(path, _base_registry())
    if nations._registry(registry)[2] != digest:
        raise DataError("冻结场地快照内容与预测指纹不一致。")
    return registry
