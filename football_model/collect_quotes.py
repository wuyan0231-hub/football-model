"""Append-only, current-time API-Football pre-match quote observations.

This module never changes a model, certifies bookmaker availability, or places
orders. The core training client deliberately rejects paginated odds; the
specialized client below reuses its quota, deadline, limiter and safe transport
primitives while admitting only /odds. Every received page is saved separately.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
import os
from pathlib import Path
import re
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request

from .competition_scope import select_competitions
from .collect_fundamentals import _canonical, _identity, _immutable, _read
from .data import (APIClient, APIError, BASE_URL, DataError, RequestBudgetError,
                   _integer, _iso, _utc, normalize_upcoming)
from .decision_quotes import adapt_quote_row

VERSION = "quote-collection-v1"
MARKETS = {1: ("Match Winner", "1x2"), 4: ("Asian Handicap", "asian_handicap"),
           5: ("Goals Over/Under", "totals"), 50: ("Goal Line", "totals")}
SIDES = {"1x2": ("home", "draw", "away"), "asian_handicap": ("home", "away"),
         "totals": ("over", "under")}


def _digest(value):
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


class OddsClient(APIClient):
    """Only this read-only endpoint extension accepts pagination.

    Status, fresh fixture identity, quota calibration and rate headers use the
    original client. Non-2xx responses retain safe HTTP status diagnostics, not
    arbitrary exception strings. Received JSON (including invalid envelopes) is
    returned for immutable storage before application validation.
    """

    def fetch_odds_page(self, match_id, page):
        _integer(match_id, "match_id", 1)
        _integer(page, "page", 1)
        params = {"fixture": match_id, "page": page}
        url = BASE_URL + "/odds?" + urlencode(sorted(params.items()))
        for attempt in range(self.max_retries + 1):
            self._before_request(paid=True)
            request = Request(url, headers={"x-apisports-key": self._key,
                                           "Accept": "application/json"}, method="GET")
            try:
                with self._opener(request, timeout=self.timeout) as response:
                    self._rate_headers(getattr(response, "headers", None))
                    body = response.read()
                observed = _iso(self._clock())
                # Raw text preserves the original body unless a secret echo
                # must be redacted; originals are not written before redaction.
                raw_text = body.decode("utf-8")
                redacted = raw_text.replace(self._key, "[REDACTED]")
                try:
                    payload = json.loads(redacted, parse_constant=lambda _: None)
                except ValueError:
                    payload = None
                return {"endpoint": "/odds", "params": params, "fetched_at": observed,
                        "from_cache": False, "payload": payload,
                        "raw_response_text": redacted,
                        "raw_body_sha256": hashlib.sha256(body).hexdigest(),
                        "stored_body_sha256": hashlib.sha256(redacted.encode()).hexdigest(),
                        "credential_echo_redacted": redacted != raw_text}
            except HTTPError as exc:
                self._rate_headers(exc.headers)
                if exc.code not in {429, 499, 500, 502, 503, 504} or attempt >= self.max_retries:
                    raise APIError(f"odds_http_{exc.code}") from None
                self._check_request_budget(paid=True)
                wait = 60.0 if exc.code == 429 else float(2 ** (attempt + 1))
                retry = exc.headers.get("Retry-After") if exc.headers else None
                if retry is not None:
                    try:
                        retry = float(retry)
                        if not math.isfinite(retry) or retry < 0:
                            raise ValueError
                        wait = max(wait, retry)
                    except ValueError:
                        raise APIError("odds_retry_after_not_numeric") from None
                self._pause(wait)
            except UnicodeDecodeError:
                raise APIError("odds_response_not_utf8") from None
            except (URLError, TimeoutError, OSError):
                if attempt >= self.max_retries:
                    raise APIError("odds_connection_failed") from None
                self._check_request_budget(paid=True)
                self._pause(float(2 ** (attempt + 1)))
        raise APIError("odds_request_incomplete")


def _candidates(runtime, config, league, match_id, now, horizon):
    """Use saved provider schedules, independently of model training success."""
    competitions = select_competitions(config, league)
    if not competitions:
        raise DataError("league_not_configured")
    sources, issues = {}, []
    for path in sorted((runtime / "cache" / "api_cache").glob("*.json")):
        try:
            envelope = _read(path)
            # Team-season/all-competition feature backfills are not complete
            # league-season candidate inventories. Do not index their absent
            # league parameter or misreport these valid caches as corruption.
            params = envelope.get("params", {})
            if (envelope.get("endpoint") != "/fixtures" or not isinstance(params, dict)
                    or set(params) != {"league", "season"}):
                continue
            observed = _utc(envelope["fetched_at"])
            if observed > now or envelope.get("errors"):
                continue
            raw = envelope["response"]
            if not isinstance(raw, list):
                continue
            # A complete saved season is the candidate source. Never assemble
            # it from a few single-match responses or incomplete pages.
            if envelope.get("paging", {}).get("total", 1) != 1:
                continue
            for competition in competitions:
                matching = [r for r in raw if isinstance(r, dict)
                            and r.get("league", {}).get("name") == competition["name"]
                            and r.get("league", {}).get("country") == competition["country"]]
                if not matching:
                    continue
                lid, season = envelope["params"]["league"], envelope["params"]["season"]
                if any(r["league"].get("id") != lid or r["league"].get("season") != season for r in matching):
                    raise DataError("cached_schedule_identity_conflict")
                key = (competition["key"], lid, season)
                source = {"observed": observed, "rows": matching, "league_id": lid,
                          "league": competition["key"], "path": str(path.relative_to(runtime)),
                          "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
                if key not in sources or observed > sources[key]["observed"]:
                    sources[key] = source
        except (ValueError, KeyError, TypeError, AttributeError, OSError):
            issues.append({"reason": "unreadable_schedule_cache", "severity": "error",
                           "path": str(path.relative_to(runtime))})
    covered = {source["league"] for source in sources.values()}
    for competition in competitions:
        if competition["key"] not in covered:
            issues.append({"league": competition["key"], "reason": "saved_provider_schedule_missing",
                           "severity": "error"})
    result, by_match, conflicts = [], {}, set()
    for source in sources.values():
        try:
            rows = normalize_upcoming(source["rows"], now, days=7)
            for row in rows:
                if match_id is not None and row["match_id"] != match_id:
                    continue
                if _utc(row["kickoff"]) > now + timedelta(hours=horizon):
                    continue
                value = {"league": source["league"], "league_id": source["league_id"],
                         "fixture": _identity(row), "candidate_source": source["path"],
                         "candidate_source_sha256": source["sha256"],
                         "candidate_observed_at": _iso(source["observed"])}
                mid = row["match_id"]
                if mid in conflicts:
                    continue
                if mid in by_match:
                    previous = by_match[mid]
                    if previous["fixture"] != value["fixture"] or previous["league"] != value["league"]:
                        result.remove(previous)
                        conflicts.add(mid)
                        raise DataError("candidate_identity_conflict")
                    if _utc(previous["candidate_observed_at"]) >= source["observed"]:
                        continue
                    result.remove(previous)
                by_match[mid] = value
                result.append(value)
        except (ValueError, KeyError, TypeError):
            issues.append({"league": source["league"], "reason": "invalid_cached_schedule",
                           "severity": "error"})
    if not sources:
        issues.append({"reason": "no_saved_provider_schedule", "severity": "error"})
    if match_id is not None and not result:
        issues.append({"reason": "requested_match_unavailable", "match_id": match_id, "severity": "error"})
    return sorted(result, key=lambda x: (x["fixture"]["kickoff"], x["fixture"]["match_id"])), issues


def _verify_fixture(envelope, candidate, observed):
    expected = candidate["fixture"]
    if (envelope.get("endpoint") != "/fixtures" or envelope.get("params") != {"id": expected["match_id"]}
            or envelope.get("errors") or envelope.get("from_cache") is not False
            or envelope.get("paging", {}).get("total", 1) != 1):
        raise DataError("fixture_source_invalid")
    rows = envelope.get("response")
    if not isinstance(rows, list) or len(rows) != 1:
        raise DataError("fixture_source_not_unique")
    raw = rows[0]
    actual = _identity({"match_id": raw["fixture"]["id"], "kickoff": raw["fixture"]["date"],
                        "home_id": raw["teams"]["home"]["id"], "away_id": raw["teams"]["away"]["id"]})
    if actual != expected or raw["league"]["id"] != candidate["league_id"]:
        raise DataError("fixture_identity_changed")
    if _utc(envelope["fetched_at"]) > observed:
        raise DataError("fixture_observed_in_future")
    return raw["fixture"]["status"]["short"] == "NS" and observed < _utc(actual["kickoff"])


def _page(envelope, mid, number, observed, expected_total=None):
    if (envelope.get("endpoint") != "/odds" or envelope.get("params") != {"fixture": mid, "page": number}
            or envelope.get("from_cache") is not False or _utc(envelope["fetched_at"]) > observed):
        raise DataError("odds_source_binding_invalid")
    payload = envelope.get("payload")
    if (not isinstance(payload, dict) or not isinstance(payload.get("errors"), (list, dict))
            or payload["errors"] or not isinstance(payload.get("response"), list)):
        raise DataError("odds_response_invalid")
    if payload.get("get") is not None and payload["get"] not in {"odds", "/odds"}:
        raise DataError("odds_endpoint_echo_conflict")
    if payload.get("parameters") is not None:
        parameters = payload["parameters"]
        if (not isinstance(parameters, dict) or set(parameters) != {"fixture", "page"}
                or type(parameters.get("fixture")) not in (str, int)
                or type(parameters.get("page")) not in (str, int)
                or str(parameters["fixture"]) != str(mid) or str(parameters["page"]) != str(number)):
            raise DataError("odds_parameter_echo_conflict")
    paging = payload.get("paging")
    if not isinstance(paging, dict) or type(paging.get("current")) is not int or paging["current"] != number:
        raise DataError("odds_page_number_mismatch")
    total = _integer(paging.get("total"), "total_pages", 0)
    if total == 0 and (number != 1 or payload["response"]):
        raise DataError("odds_zero_page_conflict")
    total = max(1, total)
    if number > total or expected_total is not None and total != expected_total:
        raise DataError("odds_pagination_changed")
    if type(payload.get("results")) is not int or payload["results"] != len(payload["response"]):
        raise DataError("odds_result_count_mismatch")
    return payload["response"], total


def _number(value, *, line=False):
    if isinstance(value, bool) or value is None or not isinstance(value, (str, int, float)):
        return None
    try:
        number = Decimal(str(value))
        if not number.is_finite() or not line and number <= 1 or line and number * 4 != (number * 4).to_integral_value():
            return None
        result = float(number)
        return (result if result else 0.0) if math.isfinite(result) else None
    except (InvalidOperation, OverflowError, ValueError):
        return None


def _closed(*objects):
    """Only explicit closure clues deny a row; absence never proves open."""
    return any(isinstance(obj, dict) and (obj.get("blocked") is True or obj.get("suspended") is True
               or isinstance(obj.get("status"), str) and obj["status"].lower() in {"closed", "suspended", "blocked"})
               for obj in objects)


def parse_records(records, candidate, *, observed_at, source_id,
                  record_observed_at=None, record_source_ids=None):
    """Project only explicitly recognized full-match markets; preserve unknowns.

    API Asian Handicap labels use the same home-line key for Home and Away.
    Opposite signs are not silently joined. The downstream adapter negates the
    home line only when deriving the away contract. All rows remain availability
    unknown (or closed); numeric odds by themselves do not grant open evidence.
    """
    rows, issues, seen = [], [], set()
    duplicate_count = 0
    for record_index, record in enumerate(records):
        record_time = record_observed_at[record_index] if record_observed_at is not None else observed_at
        record_source = record_source_ids[record_index] if record_source_ids is not None else source_id
        if not isinstance(record, dict):
            raise DataError("odds_record_not_object")
        fixture, league = record.get("fixture", {}), record.get("league", {})
        if (fixture.get("id") != candidate["fixture"]["match_id"]
                or _utc(fixture.get("date")) != _utc(candidate["fixture"]["kickoff"])
                or league.get("id") != candidate["league_id"]):
            raise DataError("odds_fixture_identity_conflict")
        if fixture.get("timestamp") is not None and (type(fixture["timestamp"]) is not int
                or abs(fixture["timestamp"] - _utc(fixture["date"]).timestamp()) > 1):
            raise DataError("odds_fixture_timestamp_conflict")
        updated = record.get("update")
        time_valid = True
        if updated is not None:
            try:
                if _utc(updated) > _utc(record_time):
                    raise DataError("source_update_in_future")
                if not isinstance(updated, str) or not re.search(r"T\d\d:\d\d:\d\d", updated):
                    raise DataError("source_update_precision_unknown")
            except (ValueError, TypeError):
                time_valid = False
                issues.append({"reason": "source_update_invalid"})
        bookmakers = record.get("bookmakers")
        if not isinstance(bookmakers, list):
            raise DataError("bookmakers_not_list")
        for bookmaker in bookmakers:
            bid = _integer(bookmaker.get("id"), "bookmaker_id", 1)
            if not isinstance(bookmaker.get("name"), str) or not bookmaker["name"].strip():
                raise DataError("bookmaker_name_missing")
            if not isinstance(bookmaker.get("bets"), list):
                raise DataError("bookmaker_bets_not_list")
            for bet in bookmaker["bets"]:
                if not isinstance(bet, dict):
                    raise DataError("market_not_object")
                betid = bet.get("id")
                if type(betid) is not int or betid not in MARKETS:
                    continue
                name, market = MARKETS[betid]
                if bet.get("name") != name or not isinstance(bet.get("values"), list):
                    issues.append({"bookmaker_id": bid, "market_id": betid, "reason": "market_mapping_unverified"})
                    continue
                groups = {}
                for value in bet["values"]:
                    if not isinstance(value, dict) or not isinstance(value.get("value"), str):
                        issues.append({"bookmaker_id": bid, "market_id": betid, "reason": "selection_unparsed"})
                        continue
                    label = value["value"]
                    if market == "1x2":
                        selection, line = label.lower(), None
                        valid = selection in SIDES[market]
                    else:
                        match = re.fullmatch(r"(Home|Away|Over|Under)\s+([+-]?\d+(?:\.\d+)?)", label)
                        selection = match[1].lower() if match else None
                        line = _number(match[2], line=True) if match else None
                        valid = selection in SIDES[market] and line is not None and (market != "totals" or line >= 0)
                    if not valid:
                        issues.append({"bookmaker_id": bid, "market_id": betid, "reason": "selection_unparsed"})
                        continue
                    group = groups.setdefault(line, {"values": {}, "conflict": False, "duplicates": 0})
                    if selection in group["values"]:
                        if group["values"][selection] == value:
                            group["duplicates"] += 1
                        else:
                            group["conflict"] = True
                    else:
                        group["values"][selection] = deepcopy(value)
                for line, group in groups.items():
                    odds = {side: _number(group["values"].get(side, {}).get("odd")) for side in SIDES[market]}
                    closed = _closed(record, bookmaker, bet, *group["values"].values())
                    incomplete = group["conflict"] or any(v is None for v in odds.values()) or not time_valid
                    content = {"bookmaker_id": bid, "company": bookmaker["name"], "market_id": betid,
                               "market_name": name, "market": market, "line": line,
                               "odds": odds, "source_updated_at": updated,
                               "status": "closed" if closed else "incomplete" if incomplete else "unknown",
                               "raw_values": group["values"], "duplicate_values": group["duplicates"]}
                    key = _digest(content)
                    if key in seen:
                        duplicate_count += 1
                        continue
                    seen.add(key)
                    index = len(rows)
                    rows.append({**content, "row_id": "api_row_" + _digest({"source_id": record_source, "content": content}),
                                 "collector_version": VERSION,
                                 "match_id": str(candidate["fixture"]["match_id"]), "source_id": record_source,
                                 "sequence": f"api-football:{bid}:{betid}:{line}", "row_order": index,
                                 "odds_format": "decimal", "quote_time": updated if time_valid else None,
                                 "time_precision": "second" if updated is not None and time_valid else None,
                                 "observed_at": record_time, "image_id": None, "settlement_basis": "REGULATION",
                                 "quote_time_semantics": "provider_record_update_not_proven_individual_price_change",
                                 "availability_status": "closed" if closed else "unknown",
                                 "reference_values_complete": not incomplete and not closed,
                                 "open_evidence": None, "execution_evidence": None,
                                 "provider_line_convention": "home_line_for_both_labels" if market == "asian_handicap" else None})
                    rows[-1]["line_mapping_status"] = ("empirical_inference_pending_provider_confirmation"
                                                       if market == "asian_handicap" else "recognized_market_labels")
    # Conflicting duplicate identities are preserved, and neither becomes a
    # silently selected best price or an interchangeable atomic market row.
    identities = {}
    for row in rows:
        key = (row["bookmaker_id"], row["market_id"], row["line"], row["source_updated_at"])
        identities.setdefault(key, []).append(row)
    for group in identities.values():
        if len(group) > 1:
            issues.append({"reason": "duplicate_row_conflict", "count": len(group)})
            for row in group:
                row["status"] = "closed" if row["status"] == "closed" else "incomplete"
                row["reference_values_complete"] = False
    return {"rows": rows, "issues": issues, "duplicate_rows": duplicate_count}


def adapt_api_reference_row(row, *, forecast_id=None):
    """Explicitly derive one numeric reference, never a live/filled quote.

    This does not select rows or verify the referenced files. The decision must
    freeze and bind the original snapshot separately. Asian-handicap semantics
    remain quarantined until the provider convention has independent evidence.
    """
    if (not isinstance(row, dict) or row.get("collector_version") != VERSION
            or row.get("market") not in {"1x2", "totals"}
            or row.get("status") != "unknown" or row.get("availability_status") != "unknown"
            or row.get("reference_values_complete") is not True
            or row.get("line_mapping_status") != "recognized_market_labels"
            or row.get("open_evidence") is not None or row.get("execution_evidence") is not None):
        raise DataError("api_row_not_eligible_for_numeric_reference")
    if MARKETS.get(row.get("market_id")) != (row.get("market_name"), row.get("market")):
        raise DataError("api_reference_market_identity_invalid")
    _utc(row.get("observed_at"))
    original = deepcopy(row)
    derived = {**original, "status": "reference_only", "reference_only": True,
               "parent_row_id": row["row_id"], "parent_row_sha256": _digest(original),
               "parent_source_status": row["status"],
               "row_id": "api_reference_" + _digest(original)}
    adapted = adapt_quote_row(derived, match_id=row["match_id"], forecast_id=forecast_id)
    if adapted["row_status"] != "reference_only":
        raise DataError("api_reference_row_not_complete")
    return {**adapted, "parent_api_row": original, "parent_api_row_sha256": _digest(original),
            "scope": "numeric_reference_only_source_availability_unknown_no_execution"}


def collect_run(config, runtime, league="all", match_id=None, *, horizon_hours=168,
                max_run_seconds=600, client=None, clock=None):
    if type(horizon_hours) not in (int, float) or not math.isfinite(horizon_hours) or not 0 < horizon_hours <= 168:
        raise DataError("horizon_hours_must_be_within_168")
    if match_id is not None:
        _integer(match_id, "match_id", 1)
    if type(max_run_seconds) not in (int, float) or not math.isfinite(max_run_seconds) or max_run_seconds <= 0:
        raise DataError("max_run_seconds_must_be_finite_positive")
    configured_seconds = config.get("api_max_run_seconds")
    if configured_seconds is not None and (type(configured_seconds) not in (int, float)
            or not math.isfinite(configured_seconds) or configured_seconds <= 0):
        raise DataError("configured_run_seconds_invalid")
    effective_seconds = min(max_run_seconds, configured_seconds) if configured_seconds is not None else max_run_seconds
    clock = clock or (lambda: datetime.now(timezone.utc))
    started = _utc(clock())
    runtime = Path(runtime)
    root = runtime / "quotes"
    run_id = started.strftime("%Y%m%dT%H%M%S%fZ")
    report_path = root / "reports" / "history" / (run_id + ".json")
    if report_path.exists():
        raise DataError("quote_run_id_exists")
    report = {"schema_version": 1, "collector_version": VERSION, "run_id": run_id,
              "run_started_at": _iso(started), "horizon_hours": horizon_hours,
              "source_commit": os.environ.get("GITHUB_SHA"),
              "max_run_seconds": effective_seconds,
              "scope": "current_api_reference_observations_no_execution_or_open_certification",
              "historical_backfill": False, "matches": [], "issues": []}

    def now():
        value = _utc(clock())
        if value < started:
            raise DataError("quote_collection_clock_moved_backwards")
        return value

    def save(directory, value):
        return str(_immutable(root / directory, value).relative_to(runtime))

    halted, candidates = False, []
    try:
        candidates, report["issues"] = _candidates(runtime, config, league, match_id, started, horizon_hours)
        report["candidate_count"] = len(candidates)
        report["candidate_manifest"] = deepcopy(candidates)
        if candidates:
            client = client or OddsClient(runtime / "cache", max_requests=config.get("max_requests_per_run"),
                daily_limit=config.get("api_daily_limit"), max_run_seconds=effective_seconds, clock=clock)
            client.status()
            report["budget_after_calibration"] = client.budget_snapshot()
        for candidate in candidates:
            mid = candidate["fixture"]["match_id"]
            item = {"match_id": mid, "league": candidate["league"], "status": "pending", "raw_pages": [],
                    "page_count": 0, "expected_pages": None, "pagination_complete": False,
                    "snapshot_path": None, "rows": 0}
            report["matches"].append(item)
            if halted:
                item.update(status="deferred", reason="budget_deferred")
                continue
            try:
                if now() >= _utc(candidate["fixture"]["kickoff"]):
                    item.update(status="skipped", reason="match_already_started")
                    continue
                fixture = client.fetch_fixture(mid, force=True)
                item["fixture_source"] = save("fixtures/" + str(mid), fixture)
                if not _verify_fixture(fixture, candidate, now()):
                    item.update(status="skipped", reason="fixture_not_current_prematch")
                    continue
                records, observations, record_times, record_sources = [], [], [], []
                page, total = 1, None
                while total is None or page <= total:
                    if now() >= _utc(candidate["fixture"]["kickoff"]):
                        raise DataError("match_started_during_collection")
                    envelope = client.fetch_odds_page(mid, page)
                    item["raw_pages"].append(save("raw/" + str(mid), envelope))
                    item["page_count"] += 1
                    entries, total = _page(envelope, mid, page, now(), total)
                    item["expected_pages"] = total
                    records.extend(entries)
                    observations.append(envelope["fetched_at"])
                    record_times.extend([envelope["fetched_at"]] * len(entries))
                    record_sources.extend(["api_odds_page_" + _digest(envelope)] * len(entries))
                    page += 1
                item["pagination_complete"] = True
                observed = max(map(_utc, observations))
                projection = parse_records(records, candidate, observed_at=_iso(observed),
                                           source_id="api_odds_" + _digest(item["raw_pages"]),
                                           record_observed_at=record_times, record_source_ids=record_sources)
                snapshot = {"schema_version": 1, "collector_version": VERSION, "run_id": run_id,
                            "candidate": candidate, "observed_at": _iso(observed),
                            "page_observed_at": observations, "raw_pages": item["raw_pages"],
                            "fixture_source": item["fixture_source"], "pagination_complete": True,
                            "acquisition_atomic": len(observations) == 1,
                            "prematch_observation": observed < _utc(candidate["fixture"]["kickoff"]),
                            "raw_record_count": len(records), **projection}
                item["snapshot_path"] = save("snapshots/" + str(mid), snapshot)
                item.update(status="collected" if records else "empty", rows=len(projection["rows"]),
                            raw_record_count=len(records), duplicate_rows=projection["duplicate_rows"],
                            issues=projection["issues"],
                            unknown_rows=sum(r["availability_status"] == "unknown" for r in projection["rows"]),
                            closed_rows=sum(r["availability_status"] == "closed" for r in projection["rows"]),
                            complete_reference_rows=sum(r["reference_values_complete"] for r in projection["rows"]))
                if not snapshot["prematch_observation"]:
                    item.update(status="evidence_only", reason="match_started_during_collection")
            except RequestBudgetError:
                halted = True
                item.update(status="deferred", reason="budget_deferred")
            except (APIError, ValueError, KeyError, TypeError, AttributeError, OSError) as exc:
                item.update(status="failed", reason="provider_error" if isinstance(exc, APIError)
                            else "save_error" if isinstance(exc, OSError) else "invalid_quote_evidence")
                if isinstance(exc, DataError) and re.fullmatch(r"[a-z][a-z0-9_]{0,80}", str(exc)):
                    item["diagnostic_code"] = str(exc)
        report["collection_finished_at"] = _iso(now())
    except (APIError, ValueError, KeyError, TypeError, AttributeError, OSError) as exc:
        report["issues"].append({"reason": "budget_deferred" if isinstance(exc, RequestBudgetError)
                                else "collection_initialization_failed", "severity": "error"})
        attempted = {m["match_id"] for m in report["matches"]}
        for candidate in candidates:
            if candidate["fixture"]["match_id"] not in attempted:
                report["matches"].append({"match_id": candidate["fixture"]["match_id"], "league": candidate["league"],
                    "status": "deferred" if isinstance(exc, RequestBudgetError) else "failed",
                    "reason": "budget_deferred" if isinstance(exc, RequestBudgetError) else "initialization_failed",
                    "raw_pages": [], "page_count": 0, "expected_pages": None, "pagination_complete": False,
                    "snapshot_path": None, "rows": 0})
    partial = (any(i.get("severity") == "error" for i in report["issues"])
               or any(m["status"] in {"deferred", "failed", "evidence_only"} for m in report["matches"]))
    report.update(status="partial" if partial else "complete" if report["matches"] else "no_upcoming",
                  generated_at=_iso(now()), exit_code=1 if partial else 0,
                  api_requests=getattr(client, "requests_made", 0),
                  api_budget=client.budget_snapshot() if client else None)
    report["counts"] = {state: sum(m["status"] == state for m in report["matches"])
                        for state in ("collected", "empty", "deferred", "failed", "skipped", "evidence_only")}
    report_path.parent.mkdir(parents=True, exist_ok=True)
    with report_path.open("x", encoding="utf-8") as handle:
        handle.write(_canonical(report))
    temporary = root / "reports" / "latest.tmp"
    temporary.write_text(_canonical(report), encoding="utf-8")
    temporary.replace(root / "reports" / "latest.json")
    return report["exit_code"], report


def main(argv=None):
    parser = argparse.ArgumentParser(description="追加保存当前赛前报价；不改变模型、不认定开放或成交")
    parser.add_argument("--runtime", default="runtime")
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--league", default="all")
    parser.add_argument("--match-id", type=int)
    parser.add_argument("--horizon-hours", type=float, default=168)
    parser.add_argument("--max-run-seconds", type=float, default=600)
    args = parser.parse_args(argv)
    try:
        code, report = collect_run(_read(args.config), args.runtime, args.league, args.match_id,
                                  horizon_hours=args.horizon_hours, max_run_seconds=args.max_run_seconds)
    except (ValueError, KeyError, TypeError, OSError):
        print("报价采集未完成：配置、输入或保存路径无效；未打印原始异常。")
        return 2
    print(f"报价采集：{report['status']}；{len(report['matches'])}场；当前开放及成交未认证。")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
