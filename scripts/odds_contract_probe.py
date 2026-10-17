"""Bounded, read-only, two-fixture API-Football odds contract observation.

No training, model-state access, order placement or Asian-line interpretation.
The existing OddsClient owns credentials, redirects, quotas and rate limits.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

TOTAL_REQUEST_LIMIT = 30  # Includes /status, fallback discovery and every page.
SOFT_SECONDS = 480
BIG_FIVE = {39: "England", 140: "Spain", 135: "Italy", 78: "Germany", 61: "France"}
CORE_MARKETS = {1, 4, 5, 50}  # Raw provider labels only; never infer AH signs.


class ProbeValidationError(ValueError):
    pass


def utc(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ProbeValidationError("timezone_missing")
    return value.astimezone(timezone.utc)


def iso(value):
    return utc(value).isoformat().replace("+00:00", "Z")


def save(path, value):
    raw = (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2,
                      allow_nan=False) + "\n").encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as handle:
        handle.write(raw)
    return {"path": str(path.name), "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}


def failure(exc):
    # Never print arbitrary exception text, request headers, or account data.
    if isinstance(exc, ProbeValidationError):
        return str(exc) if re.fullmatch(r"[a-z0-9_]{1,90}", str(exc)) else "validation_failed"
    name = type(exc).__name__
    if name in {"RequestBudgetError", "RequestDeadlineError"}:
        return "request_budget_or_deadline_blocked"
    if name == "APIError":
        return str(exc) if re.fullmatch(r"odds_http_[0-9]{3}", str(exc)) else "provider_or_configuration_error"
    return "storage_or_response_contract_error"


def make_client(output):
    from football_model.collect_quotes import OddsClient
    config = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    return OddsClient(output / "cache", max_requests=TOTAL_REQUEST_LIMIT,
        max_retries=0, timeout=15, daily_limit=config["api_daily_limit"],
        max_run_seconds=SOFT_SECONDS)


def fixture_from(envelope, expected_id=None):
    rows = envelope.get("response")
    if (envelope.get("errors") or envelope.get("from_cache") is not False
            or envelope.get("endpoint") != "/fixtures"
            or envelope.get("paging", {}).get("total", 1) != 1
            or not isinstance(rows, list) or len(rows) != 1):
        raise ProbeValidationError("fixture_response_not_unique_or_fresh")
    row = rows[0]
    fid = row["fixture"]["id"]
    if type(fid) is not int or fid <= 0 or expected_id is not None and fid != expected_id:
        raise ProbeValidationError("fixture_id_mismatch")
    return row


def eligible(row, role, now):
    lid = row["league"]["id"]
    if lid not in BIG_FIVE or row["league"].get("country") != BIG_FIVE[lid]:
        return False
    kickoff = utc(row["fixture"]["date"])
    state = row["fixture"]["status"]["short"]
    if role == "future":
        return state == "NS" and now < kickoff <= now + timedelta(days=7)
    return state == "FT" and now - timedelta(days=14) <= kickoff < now


def validate_page(envelope, fixture, page, expected_total, now):
    fid = fixture["fixture"]["id"]
    if (envelope.get("endpoint") != "/odds"
            or envelope.get("params") != {"fixture": fid, "page": page}
            or envelope.get("from_cache") is not False
            or utc(envelope["fetched_at"]) > now):
        raise ProbeValidationError("odds_source_binding_invalid")
    payload = envelope.get("payload")
    if (not isinstance(payload, dict) or not isinstance(payload.get("errors"), (dict, list))
            or payload["errors"] or not isinstance(payload.get("response"), list)):
        raise ProbeValidationError("odds_response_invalid")
    if payload.get("get") not in {None, "odds", "/odds"}:
        raise ProbeValidationError("odds_endpoint_echo_conflict")
    parameters = payload.get("parameters")
    if parameters is not None and (not isinstance(parameters, dict)
            or set(parameters) != {"fixture", "page"}
            or str(parameters["fixture"]) != str(fid) or str(parameters["page"]) != str(page)):
        raise ProbeValidationError("odds_parameter_echo_conflict")
    paging = payload.get("paging", {})
    total = paging.get("total")
    if (type(paging.get("current")) is not int or paging["current"] != page
            or type(total) is not int or total < 0
            or total == 0 and (page != 1 or payload["response"])):
        raise ProbeValidationError("odds_paging_invalid")
    total = max(1, total)
    if page > total or expected_total is not None and total != expected_total:
        raise ProbeValidationError("odds_pagination_changed")
    rows = payload["response"]
    if type(payload.get("results")) is not int or payload["results"] != len(rows):
        raise ProbeValidationError("odds_result_count_mismatch")
    for record in rows:
        if (record["fixture"]["id"] != fid or record["league"]["id"] != fixture["league"]["id"]
                or utc(record["fixture"]["date"]) != utc(fixture["fixture"]["date"])):
            raise ProbeValidationError("odds_fixture_identity_mismatch")
        if not isinstance(record.get("bookmakers"), list):
            raise ProbeValidationError("bookmakers_missing")
    return rows, total


def summarize(records):
    # Counts retain duplicate source observations; unique company/market counts
    # are explicitly separate. No selection, best-price or latest-row inference.
    companies, all_markets, observations = {}, set(), []
    value_count = 0
    for ri, record in enumerate(records):
        for company in record["bookmakers"]:
            bid = company["id"]
            if type(bid) is not int or bid <= 0 or not isinstance(company.get("bets"), list):
                raise ProbeValidationError("bookmaker_contract_invalid")
            companies.setdefault(bid, set()).add(company.get("name"))
            markets, core = [], []
            for bet in company["bets"]:
                values = bet.get("values")
                if not isinstance(values, list):
                    raise ProbeValidationError("market_values_invalid")
                all_markets.add((bet.get("id"), bet.get("name")))
                value_count += len(values)
                markets.append({"id": bet.get("id"), "name": bet.get("name"), "value_count": len(values)})
                if bet.get("id") in CORE_MARKETS:
                    core.append(bet)  # Entire original bet, including every raw line/label.
            observations.append({"record_index": ri, "company_id": bid, "company": company.get("name"),
                "provider_update": record.get("update"), "market_count": len(markets),
                "value_count": sum(m["value_count"] for m in markets),
                "markets": markets, "core_markets_raw": core})
    return {"record_count": len(records), "unique_company_count": len(companies),
        "companies": [{"id": bid, "names": sorted(names, key=str)} for bid, names in sorted(companies.items())],
        "unique_market_count": len(all_markets), "source_value_count_including_repeats": value_count,
        "company_observations": observations}


def run_probe(output, future_id=1557434, completed_id=1557430, *, client=None, clock=None, source_commit=None):
    if any(type(fid) is not int or fid <= 0 for fid in (future_id, completed_id)):
        raise ValueError("fixture IDs must be positive integers")
    if source_commit is not None and not re.fullmatch(r"[0-9a-f]{40}", source_commit):
        raise ValueError("source commit must be 40 lowercase hexadecimal characters")
    clock = clock or (lambda: datetime.now(timezone.utc))
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    report = {"schema_version": "odds-contract-probe-v1", "source_commit": source_commit,
        "started_at": iso(clock()), "limits": {"total_network_attempts": TOTAL_REQUEST_LIMIT,
        "max_retries": 0, "network_soft_seconds": SOFT_SECONDS, "recent_completed_days": 14},
        "semantics": {"training_performed": False, "production_state_modified": False,
        "asian_handicap_labels_interpreted": False, "executable_price_verified": False,
        "provider_update_is_price_change_time": False, "complete_price_history_requested": False,
        "raw_storage": "existing_client_redacted_envelopes; odds raw_response_text retained"},
        "samples": [], "issues": []}
    try:
        client = client or make_client(output)
        client.status()  # Discard account/subscription fields; persist numeric budget only.
        report["budget_before"] = client.budget_snapshot()
        for role, requested_id in (("future", future_id), ("completed", completed_id)):
            item = {"role": role, "requested_id": requested_id, "raw_pages": [],
                    "pagination_complete": False, "status": "pending"}
            report["samples"].append(item)
            records = []
            try:
                envelope = client.fetch_fixture(requested_id, force=True)
                item["requested_fixture_source"] = save(output / "raw" / (role + "-requested-fixture.json"), envelope)
                row = fixture_from(envelope, requested_id)
                if not eligible(row, role, utc(clock())):
                    parameter = "next" if role == "future" else "last"
                    fallback = client._cached_request("/fixtures", {"league": 39, parameter: 1},
                                                      force=True, max_age_seconds=0)
                    item["fallback_source"] = save(output / "raw" / (role + "-fallback-fixture.json"), fallback)
                    row = fixture_from(fallback)
                    if not eligible(row, role, utc(clock())):
                        raise ProbeValidationError("fallback_fixture_not_eligible")
                fid = row["fixture"]["id"]
                item["fixture"] = {"id": fid, "kickoff": row["fixture"]["date"],
                    "status": row["fixture"]["status"], "league": row["league"],
                    "home": row["teams"]["home"], "away": row["teams"]["away"],
                    "goals": row.get("goals"), "score": row.get("score")}
                page, total = 1, None
                while total is None or page <= total:
                    if role == "future" and utc(clock()) >= utc(row["fixture"]["date"]):
                        raise ProbeValidationError("future_fixture_started_during_probe")
                    envelope = client.fetch_odds_page(fid, page)
                    saved = save(output / "raw" / (role + "-odds-page-" + str(page) + ".json"), envelope)
                    item["raw_pages"].append({**saved, "page": page, "fetched_at": envelope.get("fetched_at")})
                    rows, total = validate_page(envelope, row, page, total, utc(clock()))
                    item["expected_pages"] = total
                    records.extend(rows)
                    page += 1
                item["details"] = summarize(records)
                item["pagination_complete"] = True
                item["status"] = "complete" if records else "empty_complete"
                item["empty_meaning"] = "no odds returned now; not proof odds never existed" if not records else None
            except Exception as exc:
                item.update(status="incomplete", failure_category=failure(exc), validated_records_retained=len(records))
                if records:
                    try:
                        item["partial_details"] = summarize(records)
                    except Exception:
                        item["partial_summary_status"] = "invalid_record_structure"
            item["finished_at"] = iso(clock())
            save(output / (role + "-summary.json"), item)
            print("ODDS_SAMPLE " + json.dumps(item, ensure_ascii=False, separators=(",", ":"), allow_nan=False))
    except Exception as exc:
        report["issues"].append({"stage": "initialization_or_summary", "failure_category": failure(exc)})
    report["budget_after"] = client.budget_snapshot() if client is not None else None
    report["finished_at"] = iso(clock())
    report["complete"] = (not report["issues"] and len(report["samples"]) == 2
                           and all(x["pagination_complete"] for x in report["samples"]))
    save(output / "summary.json", report)
    print("ODDS_PROBE_STATUS " + json.dumps({k: report[k] for k in ("complete", "budget_after", "issues")},
                                           ensure_ascii=False, separators=(",", ":")))
    return 0 if report["complete"] else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--future-id", type=int, default=1557434)
    parser.add_argument("--completed-id", type=int, default=1557430)
    parser.add_argument("--output", required=True)
    parser.add_argument("--source-commit", default=os.environ.get("GITHUB_SHA"))
    args = parser.parse_args()
    return run_probe(args.output, args.future_id, args.completed_id, source_commit=args.source_commit)


if __name__ == "__main__":
    raise SystemExit(main())
