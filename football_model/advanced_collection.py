"""Resumable multi-league historical opportunity/personnel acquisition.

Every configured league and retained fixture is reported. Real daily quota,
production reservation and wall-clock deadline bound requests; there is no
arbitrary fixture cap. Round-robin ordering avoids a large league starving the
others. Collected data are historical downloads, never retroactive predictions.
"""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import contextmanager, nullcontext
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

from .advanced_features import (canonical, embedded_snapshot, make_players_snapshot, read_json,
    source_ref, validate_evidence, _identity)
from .advanced_capacity import CapacityGuard, guarded_cache_writes
from .data import APIClient, APIError, DataError, RequestBudgetError, _fixture_identity, _iso, _utc
from .fundamentals import make_snapshot


ENDPOINTS = {"statistics":"/fixtures/statistics", "lineups":"/fixtures/lineups", "players":"/fixtures/players"}


def _failure_details(error, *, kind=None, rows=None):
    """Safe diagnostics only; do not alter rejection or retain exception text.

    Provider omissions and conflicting positive identities need different
    remediation. Report only fixed codes, integer IDs and source row positions;
    arbitrary API errors, names and other free text never enter this projection.
    """
    codes = {
        "startXI 含重复球员": ("provider_identity_conflict", "duplicate_starter"),
        "substitutes 含重复球员": ("provider_identity_conflict", "duplicate_substitute"),
        "首发与替补名单重叠": ("provider_identity_conflict", "starter_substitute_overlap"),
        "duplicate_player": ("provider_identity_conflict", "duplicate_player"),
        "基本面记录球队与本场身份不符": ("provider_identity_conflict", "team_identity_mismatch"),
        "基本面响应包含重复球队": ("provider_identity_conflict", "duplicate_team"),
        "players_team_identity_invalid": ("provider_identity_conflict", "players_team_identity_invalid"),
        "fixture_context_identity_or_clock_invalid": ("source_identity_or_clock_invalid", "fixture_context_identity_or_clock_invalid"),
        "fixture_league_identity_invalid": ("provider_identity_conflict", "fixture_league_identity_invalid"),
        "embedded_field_schema_invalid": ("provider_schema_invalid", "embedded_field_schema_invalid"),
        "player.id 必须为不小于 1 的整数": ("provider_schema_invalid", "invalid_player_id"),
    }
    default = ("provider_request_failure" if isinstance(error, APIError) else
               "storage_failure" if isinstance(error, OSError) else "validation_failure")
    category, code = codes.get(str(error), (default, default))
    findings = []
    first_invalid_player = None
    for row in rows if kind in ("lineups", "players") and isinstance(rows, list) else []:
        if not isinstance(row, dict): continue
        team = row.get("team")
        tid = team.get("id") if isinstance(team, dict) else None
        tid = tid if type(tid) is int and tid > 0 else None
        fields = ("startXI", "substitutes") if kind == "lineups" else ("players",)
        ids_by_field = {}
        for field in fields:
            values = row.get(field)
            if not isinstance(values, list): continue
            ids, missing_indices = [], []
            for index, value in enumerate(values):
                player = value.get("player") if isinstance(value, dict) else None
                if not isinstance(player, dict): continue
                pid = player.get("id")
                if type(pid) is int and pid > 0: ids.append(pid)
                elif pid is None:
                    missing_indices.append(index)
                    if field == "startXI" and first_invalid_player is None:
                        first_invalid_player = "missing"
                elif first_invalid_player is None:
                    first_invalid_player = "invalid"
            ids_by_field[field] = set(ids)
            duplicates = sorted(pid for pid, count in Counter(ids).items() if count > 1)
            if duplicates:
                findings.append({"team_id": tid, "field": field,
                    "code": "duplicate_player_ids", "player_ids": duplicates})
            if missing_indices:
                findings.append({"team_id": tid, "field": field,
                    "code": "missing_player_ids", "source_row_indices": missing_indices})
        overlap = sorted(ids_by_field.get("startXI", set()) & ids_by_field.get("substitutes", set()))
        if overlap:
            findings.append({"team_id": tid, "field": "startXI_and_substitutes",
                "code": "starter_substitute_overlap", "player_ids": overlap})
    if code == "invalid_player_id" and first_invalid_player == "missing":
        category, code = "provider_data_missing", "missing_starter_player_id"
    return {"failure_category": category, "failure_code": code, "identity_findings": findings}


@contextmanager
def _offline_requests(client, reason=None):
    """Continue valid cache reads after deferral, without another HTTP attempt."""
    if reason is None or not hasattr(client,"_request"):
        yield
        return
    original=client._request
    def denied(*args,**kwargs):
        error=RequestBudgetError(reason);error.reason=reason
        raise error
    client._request=denied
    try:yield
    finally:client._request=original


def _save(runtime, directory, value, *, guard=None, final=False):
    data = (canonical(value)+"\n").encode()
    path = Path(runtime)/directory/(hashlib.sha256(data).hexdigest()+".json")
    ticket=guard.check_write(path,data,final=final) if guard is not None else None
    path.parent.mkdir(parents=True,exist_ok=True)
    if path.exists():
        if path.read_bytes() != data: raise DataError("immutable_collision")
    else:
        with path.open("xb") as stream: stream.write(data)
    if ticket is not None:guard.commit_write(ticket)
    return source_ref(path,runtime)


def _cache_contexts(runtime, now):
    found = {}
    paths=list((Path(runtime)/"cache"/"api_cache").glob("*.json"))
    paths+=list((Path(runtime)/"fundamentals"/"fixtures").glob("*/*.json"))
    for path in sorted(paths):
        try:
            document = read_json(path)
            projection="fixture_response" if "fixture_response" in document else None
            envelope=document[projection] if projection else document
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
                priority=(sum(isinstance(raw.get(k),list) for k in ENDPOINTS),_utc(envelope["fetched_at"]))
                previous=found.get(mid)
                previous_priority=(sum(isinstance(previous[1].get(k),list) for k in ENDPOINTS),_utc(previous[0]["fetched_at"])) if previous else None
                if previous is None or priority>previous_priority:
                    found[mid] = (envelope,raw,path,projection)
        except (ValueError,TypeError,KeyError,OSError,AttributeError):
            continue
    return found


def _context(client, expected, metadata, contexts, now,refresh=False,offline_reason=None):
    mid = expected["match_id"]
    fetched_detail=False
    if mid in contexts and not refresh:
        envelope,raw,path,projection = contexts[mid]
    else:
        with _offline_requests(client,offline_reason):
            envelope = client.fetch_fixture(mid,force=refresh,max_age_seconds=7*86400)
        fetched_detail=True
        path=projection=None
        if envelope.get("endpoint") != "/fixtures" or envelope.get("params") != {"id":mid} or envelope.get("errors") or envelope.get("paging",{}).get("total",1)!=1:
            raise DataError("fixture_context_invalid")
        if not isinstance(envelope.get("response"),list) or len(envelope["response"])!=1:
            raise DataError("fixture_context_not_unique")
        raw = envelope["response"][0]
    if not fetched_detail and offline_reason is None and (not all(isinstance(raw.get(k),list) for k in ENDPOINTS) or
            any(raw.get(k)==[] for k in ENDPOINTS) and (now()-_utc(envelope["fetched_at"])).total_seconds()>7*86400):
        # League-season fixture lists lack nested statistics; one approved
        # fixture-detail request yields all embedded feature endpoints together.
        envelope=client.fetch_fixture(mid,force=refresh,max_age_seconds=7*86400)
        if envelope.get("endpoint")!="/fixtures" or envelope.get("params")!={"id":mid} or envelope.get("errors") or len(envelope.get("response",[]))!=1:
            raise DataError("fixture_detail_invalid")
        raw=envelope["response"][0];path=projection=None
    actual,_ = _fixture_identity(raw)
    if _identity(actual) != _identity(expected) or _utc(envelope["fetched_at"]) > now():
        raise DataError("fixture_context_identity_or_clock_invalid")
    league = raw.get("league",{})
    if league.get("id") != metadata["league"]["id"]:
        raise DataError("fixture_league_identity_invalid")
    seasons = [s for s in metadata["seasons"] if s.get("year")==league.get("season")]
    if len(seasons)!=1: raise DataError("fixture_coverage_missing")
    return envelope,raw,seasons[0]["coverage"],path,projection


def _save_context(runtime,envelope,path=None,projection=None,guard=None):
    if path is not None and projection=="fixture_response":
        # Existing fundamentals contexts are immutable originals, retained at
        # their original bytes. The wrapper explicitly names the projection.
        return source_ref(path,runtime),projection
    if path is not None:
        data=Path(path).read_bytes()
        destination=Path(runtime)/"advanced_features"/"contexts"/(hashlib.sha256(data).hexdigest()+".json")
        ticket=guard.check_write(destination,data) if guard is not None else None
        destination.parent.mkdir(parents=True,exist_ok=True)
        if destination.exists():
            if destination.read_bytes()!=data:raise DataError("immutable_context_collision")
        else:
            with destination.open("xb") as out:out.write(data)
        if ticket is not None:guard.commit_write(ticket)
        return source_ref(destination,runtime),None
    return _save(runtime,Path("advanced_features/contexts"),envelope,guard=guard),None


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
                reserve_requests=250, refresh=False, kinds=("statistics","lineups","players"),capacity_guard=None):
    if type(reserve_requests)is not int or reserve_requests<0: raise DataError("invalid_request_reservation")
    if not kinds or any(k not in ENDPOINTS for k in kinds) or len(set(kinds))!=len(kinds): raise DataError("invalid_kinds")
    clock=clock or (lambda:datetime.now(timezone.utc))
    # Writers below address paths from cwd, while CapacityGuard interprets a
    # relative admission path from runtime. Resolve once so CLI --runtime
    # runtime writes and its capacity tickets always refer to the same file.
    started=_utc(clock()); runtime=Path(runtime).resolve()
    guard=capacity_guard if capacity_guard is not None else CapacityGuard(runtime)
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
    metadata={}; context_refs={}; context_cache={}; halted=False;initialization_failed=False
    halted_reason="real_budget_or_runtime_deferred"
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
            halted=True;initialization_failed=True
            report["issues"].append({"reason":"client_or_real_daily_budget_unavailable"})
    reservation=client.reserve_requests(reserve_requests) if client is not None and hasattr(client,"reserve_requests") else nullcontext()
    with reservation, guarded_cache_writes(client,guard):
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
            if initialization_failed or (halted and (mid not in contexts or refresh)):
                for kind in pending: item["kinds"][kind]={"status":"deferred","reason":halted_reason}
                item["status"]="deferred"; continue
            report["last_attempted_league"]=key
            try:
                if key not in metadata:
                    with _offline_requests(client,halted_reason if halted else None):
                        metadata[key]=client.resolve_league(leagues[key][0],force=False)
                    if _utc(metadata[key]["fetched_at"])>now(): raise DataError("metadata_clock_in_future")
                retry_invalid_cache=False
                if mid in contexts and not refresh:
                    invalid_kind="context"
                    try:
                        # Inspect the retained original without a request. A
                        # complete, nonempty but invalid historical response
                        # must not win cache selection forever.
                        cached=_context(client,expected,metadata[key],contexts,now,
                            offline_reason="cached_context_validation_only")
                        for invalid_kind in pending:
                            if invalid_kind in cached[1]:
                                if not isinstance(cached[1][invalid_kind],list):
                                    raise DataError("embedded_field_schema_invalid")
                                embedded_snapshot(invalid_kind,cached[0],mid)
                    except (ValueError,KeyError,TypeError,AttributeError) as exc:
                        old_envelope,_,old_path,old_projection=contexts[mid]
                        old_ref,old_projection=_save_context(runtime,old_envelope,old_path,old_projection,guard)
                        diagnostic={"schema_version":"advanced-source-failure-v1","kind":invalid_kind,
                            "match_id":mid,"recorded_at":_iso(now()),"evidence_envelope":None,
                            "fixture_envelope_ref":old_ref,"fixture_envelope_projection":old_projection,
                            "embedded_field":None if invalid_kind=="context" else invalid_kind,
                            "reason":"cached_context_schema_or_identity_invalid","feature_eligible":False,
                            **_failure_details(exc,kind=invalid_kind,rows=contexts[mid][1].get(invalid_kind))}
                        diagnostic_ref=_save(runtime,Path("advanced_features/failures")/invalid_kind/str(mid),diagnostic,guard=guard)
                        item["cached_context_retry"]={"reason":"cached_context_schema_or_identity_invalid",
                            "diagnostic_ref":diagnostic_ref,"force_fixture_request":True}
                        retry_invalid_cache=True
                envelope,raw,coverage,context_path,context_projection=_context(client,expected,metadata[key],contexts,now,refresh or retry_invalid_cache,
                    halted_reason if halted else None)
                context_key=id(envelope)
                if context_key not in context_refs:
                    ref,projection=_save_context(runtime,envelope,context_path,context_projection,guard)
                    context_refs[context_key]=(envelope,ref,projection)
                if raw["fixture"]["status"]["short"]!="FT":
                    for kind in pending: item["kinds"][kind]={"status":"unsupported","reason":"not_regulation_FT_statistics"}
                    item["status"]="unsupported"; continue
                for kind in pending:
                    covered=coverage.get("fixtures",{}).get({"statistics":"statistics_fixtures","lineups":"lineups","players":"statistics_players"}[kind])
                    if covered is False and not raw.get(kind):
                        item["kinds"][kind]={"status":"unsupported","reason":"provider_declares_coverage_false"}; continue
                    source=None
                    try:
                        if kind in raw and not isinstance(raw[kind],list):
                            raise DataError("embedded_field_schema_invalid")
                        if isinstance(raw.get(kind),list):
                            snap=embedded_snapshot(kind,envelope,mid)
                            value={"schema_version":"advanced-embedded-source-v1","kind":kind,"match_id":mid,
                                "fixture_envelope_ref":context_refs[context_key][1],
                                "fixture_envelope_projection":context_refs[context_key][2],
                                "normalized_snapshot_id":snap["snapshot_id"],"created_at":_iso(now()),
                                "source_kind":"declared_projection_of_original_fixture_embedded_field",
                                "network_endpoint":"/fixtures","field":kind}
                        elif kind=="players":
                            # The production API client intentionally has no
                            # players endpoint permission. Do not evade that
                            # allowlist; fixture detail is the authorized source.
                            item["kinds"][kind]=({"status":"deferred","reason":halted_reason} if halted else
                                {"status":"unsupported","reason":"fixture_detail_players_field_absent"})
                            continue
                        else:
                            with _offline_requests(client,halted_reason if halted else None):
                                source=client._cached_request(ENDPOINTS[kind],{"fixture":mid},force=refresh,max_age_seconds=7*86400)
                            snap=make_snapshot(kind,source,raw,context_observed_at=envelope["fetched_at"])
                            value={"schema_version":"advanced-source-v1","snapshot":snap,"fixture_envelope_ref":context_refs[context_key][1],
                                "fixture_envelope_projection":context_refs[context_key][2],
                                "metadata":metadata[key],"evidence_envelope":source,"created_at":_iso(now())}
                        validate_evidence(value,runtime,context_cache)
                        ref=_save(runtime,Path("advanced_features/evidence")/kind/str(mid),value,guard=guard)
                        item["kinds"][kind]={"status":"collected","source":ref,"response_rows":len(snap["raw_response"]),
                            "collection_method":"fixture_embedded_projection" if source is None else "endpoint_request_or_cache"}
                    except RequestBudgetError as exc:
                        halted=True;halted_reason=getattr(exc,"reason","real_budget_or_runtime_deferred")
                        item["kinds"][kind]={"status":"deferred","reason":halted_reason}
                    except (APIError,ValueError,KeyError,TypeError,OSError,AttributeError) as exc:
                        details=_failure_details(exc,kind=kind,
                            rows=source.get("response") if isinstance(source,dict) else raw.get(kind))
                        item["kinds"][kind]={"status":"failed","reason":"provider_schema_identity_or_storage_failure",**details}
                        # APIClient already redacts credential echoes. Preserve
                        # the actual returned envelope when normalization failed;
                        # it stays quarantined and is never a feature snapshot.
                        if source is not None or kind in raw:
                            try:
                                diagnostic={"schema_version":"advanced-source-failure-v1","kind":kind,
                                    "match_id":mid,"recorded_at":_iso(now()),"evidence_envelope":source,
                                    "fixture_envelope_ref":context_refs[context_key][1],
                                    "fixture_envelope_projection":context_refs[context_key][2],
                                    "embedded_field":kind if source is None else None,
                                    "reason":"provider_schema_identity_or_storage_failure","feature_eligible":False,**details}
                                item["kinds"][kind]["diagnostic_ref"]=_save(runtime,Path("advanced_features/failures")/kind/str(mid),diagnostic,guard=guard)
                            except (ValueError,TypeError,OSError):
                                item["kinds"][kind]["diagnostic_save_failed"]=True
                item["status"]="partial" if any(v["status"] in ("failed","deferred") for v in item["kinds"].values()) else "collected"
            except RequestBudgetError as exc:
                halted=True;halted_reason=getattr(exc,"reason","real_budget_or_runtime_deferred")
                for kind in pending: item["kinds"].setdefault(kind,{"status":"deferred","reason":halted_reason})
                item["status"]="deferred"
            except (APIError,ValueError,KeyError,TypeError,OSError,AttributeError) as exc:
                for kind in pending: item["kinds"].setdefault(kind,{"status":"failed","reason":"fixture_metadata_or_context_invalid",**_failure_details(exc)})
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
    report["capacity_deferred_requests"]=sum(v.get("reason")=="storage_capacity_deferred" for i in report["matches"] for v in i["kinds"].values())
    failures=[v for i in report["matches"] for v in i["kinds"].values() if v["status"]=="failed"]
    report["failure_category_counts"]=dict(Counter(v["failure_category"] for v in failures))
    report["failure_code_counts"]=dict(Counter(v["failure_code"] for v in failures))
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
    report["storage_capacity_before_report_writes"]=guard.summary()
    ref=_save(runtime,Path("advanced_features/collection/history"),report,guard=guard,final=True)
    if Path(league).name!=league or league in (".",".."):raise DataError("invalid_league_selection_path")
    for pointer in (latest,runtime/"advanced_features"/"collection"/"by_selection"/league/"latest.json"):
        pointer.parent.mkdir(parents=True,exist_ok=True)
        raw=(canonical(report)+"\n").encode()
        ticket=guard.check_write(pointer,raw,final=True)
        temporary=pointer.with_suffix(".tmp"); temporary.write_bytes(raw); temporary.replace(pointer)
        guard.commit_write(ticket)
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
