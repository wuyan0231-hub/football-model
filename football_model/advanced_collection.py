"""Resumable multi-league historical opportunity/personnel acquisition.

Every configured league and retained fixture is reported. Real daily quota,
production reservation and wall-clock deadline bound requests; there is no
arbitrary fixture cap. Round-robin ordering avoids a large league starving the
others. Collected data are historical downloads, never retroactive predictions.
"""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import nullcontext
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

from .advanced_features import (canonical, make_players_snapshot, read_json,
    source_ref, validate_evidence, _identity)
from .data import APIClient, APIError, DataError, RequestBudgetError, _fixture_identity, _iso, _utc
from .fundamentals import make_snapshot


ENDPOINTS = {"statistics":"/fixtures/statistics", "lineups":"/fixtures/lineups", "players":"/fixtures/players"}


def _save(runtime, directory, value):
    data = (canonical(value)+"\n").encode()
    path = Path(runtime)/directory/(hashlib.sha256(data).hexdigest()+".json")
    path.parent.mkdir(parents=True,exist_ok=True)
    if path.exists():
        if path.read_bytes() != data: raise DataError("immutable_collision")
    else:
        with path.open("xb") as stream: stream.write(data)
    return source_ref(path,runtime)


def _cache_contexts(runtime, now):
    found = {}
    for path in sorted((Path(runtime)/"cache"/"api_cache").glob("*.json")):
        try:
            envelope = read_json(path)
            if (envelope.get("endpoint") != "/fixtures" or envelope.get("errors")
                    or envelope.get("paging",{}).get("total",1) != 1
                    or envelope.get("paging",{}).get("current",1) != 1
                    or _utc(envelope["fetched_at"]) > now): continue
            params = envelope.get("params",{})
            if set(params) not in ({"id"},{"league","season"}): continue
            for raw in envelope["response"]:
                identity, _ = _fixture_identity(raw)
                if raw["fixture"]["status"]["short"] != "FT": continue
                mid = identity["match_id"]
                if mid not in found or envelope["fetched_at"] > found[mid][0]["fetched_at"]:
                    found[mid] = (envelope,raw)
        except (ValueError,TypeError,KeyError,OSError,AttributeError):
            continue
    return found


def _context(client, expected, metadata, contexts, now):
    mid = expected["match_id"]
    if mid in contexts:
        envelope,raw = contexts[mid]
    else:
        envelope = client.fetch_fixture(mid,force=False,max_age_seconds=7*86400)
        if envelope.get("endpoint") != "/fixtures" or envelope.get("params") != {"id":mid} or envelope.get("errors") or envelope.get("paging",{}).get("total",1)!=1:
            raise DataError("fixture_context_invalid")
        if not isinstance(envelope.get("response"),list) or len(envelope["response"])!=1:
            raise DataError("fixture_context_not_unique")
        raw = envelope["response"][0]
    actual,_ = _fixture_identity(raw)
    if _identity(actual) != _identity(expected) or _utc(envelope["fetched_at"]) > now():
        raise DataError("fixture_context_identity_or_clock_invalid")
    league = raw.get("league",{})
    if league.get("id") != metadata["league"]["id"]:
        raise DataError("fixture_league_identity_invalid")
    seasons = [s for s in metadata["seasons"] if s.get("year")==league.get("season")]
    if len(seasons)!=1: raise DataError("fixture_coverage_missing")
    return envelope,raw,seasons[0]["coverage"]


def _retained(runtime, kind, mid, expected, now, refresh, context_cache=None):
    found = []
    for path in (Path(runtime)/"advanced_features"/"evidence"/kind/str(mid)).glob("*.json"):
        try:
            value=read_json(path)
            snap=validate_evidence(value,runtime,context_cache)
            if _identity(snap["fixture"])!=_identity(expected) or _utc(snap["known_at"]) > now: continue
            if refresh: continue
            # Empty responses are retained evidence but are retried weekly.
            if not snap["raw_response"] and (now-_utc(snap["observed_at"])).total_seconds()>7*86400: continue
            found.append((snap,source_ref(path,runtime)))
        except (ValueError,TypeError,KeyError,OSError,AttributeError): continue
    return max(found,key=lambda p:(p[0]["known_at"],p[0]["snapshot_id"]),default=None)


def collect_run(config, runtime, league="all", *, client=None, clock=None, max_seconds=None,
                reserve_requests=250, refresh=False, kinds=("statistics","lineups","players")):
    if type(reserve_requests)is not int or reserve_requests<0: raise DataError("invalid_request_reservation")
    if not kinds or any(k not in ENDPOINTS for k in kinds) or len(set(kinds))!=len(kinds): raise DataError("invalid_kinds")
    clock=clock or (lambda:datetime.now(timezone.utc))
    started=_utc(clock()); runtime=Path(runtime)
    def now():
        value=_utc(clock())
        if value<started: raise DataError("clock_moved_backwards")
        return value
    chosen=[c for c in config["competitions"] if league=="all" or c["key"]==league]
    if not chosen: raise DataError("unknown_league")
    report={"schema_version":"advanced-collection-v1","started_at":_iso(started),"league_selection":league,
        "historical_download_not_point_in_time":True,"production_reservation_requests":reserve_requests,
        "requested_kinds":list(kinds),"leagues":[],"issues":[],"matches":[],"provider_census_verified":False}
    rows_by_league={}; leagues={}
    for competition in chosen:
        key=competition["key"]
        item={"league":key,"configured_mode":competition.get("mode"),"status":"pending","requested_matches":None}
        report["leagues"].append(item); leagues[key]=(competition,item)
        if competition.get("mode")=="audit_only":
            item.update(status="audit_only_not_model_scope",requested_matches=None)
            continue
        try:
            path=runtime/"data"/(key+".json")
            rows=read_json(path)["matches"]
            identities=[_identity(r) for r in rows]
            if len({r["match_id"] for r in identities})!=len(identities): raise DataError("duplicate_history_fixture")
            rows_by_league[key]=sorted(identities,key=lambda r:(r["kickoff"],r["match_id"]),reverse=True)
            item.update(status="history_loaded",requested_matches=len(rows),history_source=source_ref(path,runtime))
        except (ValueError,KeyError,TypeError,OSError):
            item.update(status="history_missing_or_invalid",requested_matches=None)
    contexts=_cache_contexts(runtime,started)
    # Rotate the first league on each resume; completion is still per fixture.
    keys=list(rows_by_league)
    latest=runtime/"advanced_features"/"collection"/"latest.json"
    try:
        previous=read_json(latest).get("last_attempted_league")
        if previous in keys:
            i=keys.index(previous)+1; keys=keys[i:]+keys[:i]
    except (ValueError,KeyError,TypeError,OSError): pass
    queue=[(key,rows_by_league[key][i]) for i in range(max(map(len,rows_by_league.values()),default=0))
        for key in keys if i<len(rows_by_league[key])]
    metadata={}; context_refs={}; context_cache={}; halted=False
    if queue:
        try:
            client=client or APIClient(runtime/"cache",max_requests=config.get("max_requests_per_run"),
                daily_limit=config.get("api_daily_limit",5000),
                max_run_seconds=max_seconds or config.get("api_max_run_seconds",3600),clock=clock)
            client.status()
            initial_budget=client.budget_snapshot() if hasattr(client,"budget_snapshot") else {}
            if (initial_budget.get("daily_quota_status") in ("unknown","incomplete")
                    and initial_budget.get("configured_daily_limit") is not None):
                raise DataError("actual_daily_balance_unavailable")
        except (APIError,ValueError,TypeError,OSError):
            halted=True; report["issues"].append({"reason":"client_or_real_daily_budget_unavailable"})
    reservation=client.reserve_requests(reserve_requests) if client is not None and hasattr(client,"reserve_requests") else nullcontext()
    with reservation:
        for key,expected in queue:
            mid=expected["match_id"]
            item={"league":key,"match_id":mid,"kinds":{},"status":"pending"}; report["matches"].append(item)
            pending=[]
            for kind in kinds:
                existing=_retained(runtime,kind,mid,expected,now(),refresh,context_cache)
                if existing:
                    snap,ref=existing
                    item["kinds"][kind]={"status":"retained","source":ref,"response_rows":len(snap["raw_response"])}
                else: pending.append(kind)
            if not pending:
                item["status"]="retained"; continue
            if halted:
                for kind in pending: item["kinds"][kind]={"status":"deferred","reason":"real_budget_or_runtime_deferred"}
                item["status"]="deferred"; continue
            report["last_attempted_league"]=key
            try:
                if key not in metadata:
                    metadata[key]=client.resolve_league(leagues[key][0],force=False)
                    if _utc(metadata[key]["fetched_at"])>now(): raise DataError("metadata_clock_in_future")
                envelope,raw,coverage=_context(client,expected,metadata[key],contexts,now)
                context_key=id(envelope)
                if context_key not in context_refs:
                    context_refs[context_key]=(envelope,_save(runtime,Path("advanced_features/contexts"),envelope))
                if raw["fixture"]["status"]["short"]!="FT":
                    for kind in pending: item["kinds"][kind]={"status":"unsupported","reason":"not_regulation_FT_statistics"}
                    item["status"]="unsupported"; continue
                for kind in pending:
                    covered=coverage.get("fixtures",{}).get({"statistics":"statistics_fixtures","lineups":"lineups","players":"statistics_players"}[kind])
                    if covered is False:
                        item["kinds"][kind]={"status":"unsupported","reason":"provider_declares_coverage_false"}; continue
                    source=None
                    try:
                        # Existing client's allowlist intentionally excludes
                        # players; use its same validated cache/budget primitive.
                        source=client._cached_request(ENDPOINTS[kind],{"fixture":mid},force=refresh,max_age_seconds=7*86400)
                        snap=(make_players_snapshot(source,raw,context_observed_at=envelope["fetched_at"])
                            if kind=="players" else make_snapshot(kind,source,raw,context_observed_at=envelope["fetched_at"]))
                        value={"schema_version":"advanced-source-v1","snapshot":snap,"fixture_envelope_ref":context_refs[context_key][1],
                            "metadata":metadata[key],"evidence_envelope":source,"created_at":_iso(now())}
                        validate_evidence(value,runtime,context_cache)
                        ref=_save(runtime,Path("advanced_features/evidence")/kind/str(mid),value)
                        item["kinds"][kind]={"status":"collected","source":ref,"response_rows":len(snap["raw_response"])}
                    except RequestBudgetError:
                        halted=True; item["kinds"][kind]={"status":"deferred","reason":"real_budget_or_runtime_deferred"}
                    except (APIError,ValueError,KeyError,TypeError,OSError,AttributeError):
                        item["kinds"][kind]={"status":"failed","reason":"provider_schema_identity_or_storage_failure"}
                        # APIClient already redacts credential echoes. Preserve
                        # the actual returned envelope when normalization failed;
                        # it stays quarantined and is never a feature snapshot.
                        if source is not None:
                            try:
                                diagnostic={"schema_version":"advanced-source-failure-v1","kind":kind,
                                    "match_id":mid,"recorded_at":_iso(now()),"evidence_envelope":source,
                                    "fixture_envelope_ref":context_refs[context_key][1],
                                    "reason":"provider_schema_identity_or_storage_failure","feature_eligible":False}
                                item["kinds"][kind]["diagnostic_ref"]=_save(runtime,Path("advanced_features/failures")/kind/str(mid),diagnostic)
                            except (ValueError,TypeError,OSError):
                                item["kinds"][kind]["diagnostic_save_failed"]=True
                item["status"]="partial" if any(v["status"] in ("failed","deferred") for v in item["kinds"].values()) else "collected"
            except RequestBudgetError:
                halted=True
                for kind in pending: item["kinds"].setdefault(kind,{"status":"deferred","reason":"real_budget_or_runtime_deferred"})
                item["status"]="deferred"
            except (APIError,ValueError,KeyError,TypeError,OSError,AttributeError):
                for kind in pending: item["kinds"].setdefault(kind,{"status":"failed","reason":"fixture_metadata_or_context_invalid"})
                item["status"]="failed"
    for summary in report["leagues"]:
        items=[i for i in report["matches"] if i["league"]==summary["league"]]
        summary["kind_status_counts"]={kind:dict(Counter(i["kinds"][kind]["status"] for i in items)) for kind in kinds}
        if summary["requested_matches"] is not None:
            summary["status"]="complete_with_reported_coverage" if all(i["status"] not in ("partial","failed","deferred") for i in items) else "partial"
            summary["denominator_preserved"]=len(items)==summary["requested_matches"]
    report["finished_at"]=_iso(now())
    report["api_budget"]=client.budget_snapshot() if client is not None and hasattr(client,"budget_snapshot") else None
    report["api_requests"]=getattr(client,"requests_made",0)
    report["status"]="complete_with_reported_coverage" if all(l["status"] in ("complete_with_reported_coverage","audit_only_not_model_scope") for l in report["leagues"]) and not report["issues"] else "partial"
    report["pending_requests"]=sum(v["status"] in ("failed","deferred") for i in report["matches"] for v in i["kinds"].values())
    report["failed_requests"]=sum(v["status"]=="failed" for i in report["matches"] for v in i["kinds"].values())
    report["deferred_requests"]=sum(v["status"]=="deferred" for i in report["matches"] for v in i["kinds"].values())
    report["engineering_failures"]={"failed_endpoint_requests":report["failed_requests"],
        "history_missing_or_invalid_leagues":sum(l["status"]=="history_missing_or_invalid" for l in report["leagues"]),
        "initialization_or_collection_issues":len(report["issues"]),
        "diagnostic_save_failures":sum(bool(v.get("diagnostic_save_failed")) for i in report["matches"] for v in i["kinds"].values())}
    report["engineering_failure_count"]=sum(report["engineering_failures"].values())
    report["execution_status"]=("failed" if report["engineering_failure_count"] else
        "deferred" if report["deferred_requests"] else "complete")
    report["full_collection_completed"]=report["status"]=="complete_with_reported_coverage"
    # Quota/runtime deferral is normal resumable execution, never full coverage.
    # A schema, identity, account initialization or storage failure stays fatal.
    report["exit_code"]=int(report["engineering_failure_count"]>0)
    ref=_save(runtime,Path("advanced_features/collection/history"),report)
    if Path(league).name!=league or league in (".",".."):raise DataError("invalid_league_selection_path")
    for pointer in (latest,runtime/"advanced_features"/"collection"/"by_selection"/league/"latest.json"):
        pointer.parent.mkdir(parents=True,exist_ok=True)
        temporary=pointer.with_suffix(".tmp"); temporary.write_text(canonical(report)+"\n",encoding="utf-8"); temporary.replace(pointer)
    return report["exit_code"],report


def main(argv=None):
    parser=argparse.ArgumentParser(description="按真实额度跨联赛采集机会/阵容历史，保留全请求和断点")
    parser.add_argument("--runtime",default="runtime"); parser.add_argument("--config",default="config.json")
    parser.add_argument("--league",default="all"); parser.add_argument("--max-seconds",type=float)
    parser.add_argument("--reserve-requests",type=int,default=250); parser.add_argument("--refresh",action="store_true")
    parser.add_argument("--kinds",nargs="+",choices=list(ENDPOINTS),default=list(ENDPOINTS))
    args=parser.parse_args(argv)
    code,report=collect_run(read_json(args.config),args.runtime,args.league,max_seconds=args.max_seconds,
        reserve_requests=args.reserve_requests,refresh=args.refresh,kinds=tuple(args.kinds))
    print(json.dumps({"status":report["status"],"leagues":len(report["leagues"]),"fixtures":len(report["matches"]),
        "pending_requests":report["pending_requests"],"deferred_requests":report["deferred_requests"],
        "engineering_failure_count":report["engineering_failure_count"],"execution_status":report["execution_status"],
        "full_collection_completed":report["full_collection_completed"],"api_requests":report["api_requests"]},ensure_ascii=False))
    return code


if __name__=="__main__": raise SystemExit(main())
