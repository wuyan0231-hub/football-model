"""All-opportunity future scoring for advanced research; no odds or trades.

First captures and scores are immutable. Candidate source damage excludes the
candidate while keeping independently verified M0; it cannot retrospectively
turn an already chosen candidate into a successful baseline fallback.
"""
from __future__ import annotations
import argparse
from collections import Counter
from datetime import datetime, timezone
import fcntl
import json
from pathlib import Path

from .data import APIClient, APIError, _iso, _utc
from .feature_research import score_forecast, _mean_metrics
from .feature_scoring import (RESULT_DELAY, RESULT_REFRESH, RESULT_RECHECK_WINDOW,
    _immutable, _normalize_result, _pointer, _provider_league, _read_ref, _reference)
from .feature_snapshots import SnapshotError, _digest, _read, _tick, HORIZONS
from .advanced_snapshots import (candidate_ids, load_verified_comparison,
    _commit, _reason, _root as capture_root, _VerifiedQueueIndex)

VERSION = "advanced-future-scoring-v1"


def _root(runtime, protocol):
    return Path(runtime).resolve() / "advanced_scoring" / _digest(protocol)


def _metrics(prediction, result):
    value = score_forecast(prediction, result, extended_diagnostics=True)
    return value


def _original_choices(comparison):
    original = comparison["payload"]["candidates"]
    statuses = comparison.get("original_candidate_statuses", {})
    return {cid: (cid if statuses.get(cid) == "verified" else "not_applicable" if statuses.get(cid)=="not_applicable" else "M0_fallback") for cid, row in original.items()}


def _scores(comparison, result, choices):
    base = _metrics(comparison["baseline"]["prediction"], result) if comparison["baseline"]["prediction"] is not None else None
    candidates, strategy = {}, {}
    for cid, choice in choices.items():
        branch = comparison["candidates"].get(cid, {})
        candidate = _metrics(branch["prediction"], result) if branch.get("status") in ("verified", "predicted") and branch.get("prediction") else None
        candidates[cid] = candidate
        strategy[cid] = base if choice == "M0_fallback" else candidate
    return {"M0": base, "candidates": candidates, "strategy": strategy}


def verified_score(runtime, saved, comparison, competition, now):
    runtime = Path(runtime).resolve()
    if (saved.get("schema_version") != VERSION or saved.get("protocol_sha256") != comparison["protocol_sha256"]
            or saved.get("capture_receipt_ref") != comparison["receipt_ref"]
            or saved.get("forecast_sha256") != comparison["forecast_sha256"]
            or saved.get("match_id") != comparison["fixture"]["match_id"] or saved.get("horizon") != comparison["horizon"]):
        raise SnapshotError("advanced_saved_score_parent_mismatch")
    provider = _provider_league(runtime, comparison["payload"]["baseline_payload"], competition)
    envelope = _read_ref(runtime, saved["result_ref"])
    result, _ = _normalize_result(envelope, comparison["fixture"], provider, now)
    if result != saved.get("result") or saved.get("result_available_at") != envelope.get("fetched_at"):
        raise SnapshotError("advanced_saved_result_replay_mismatch")
    if saved.get("original_strategy_choices") != _original_choices(comparison):
        raise SnapshotError("advanced_original_strategy_changed")
    for cid, choice in saved["original_strategy_choices"].items():
        original_strategy = saved["scores"]["M0"] if choice == "M0_fallback" else None if choice=="not_applicable" else saved["scores"]["candidates"].get(cid)
        if saved["scores"]["strategy"].get(cid) != original_strategy:
            raise SnapshotError("advanced_saved_strategy_score_replay_mismatch")
    current = _scores(comparison, result, saved["original_strategy_choices"])
    if current["M0"] != saved["scores"]["M0"]:
        raise SnapshotError("advanced_saved_baseline_score_replay_mismatch")
    for cid, metrics in current["candidates"].items():
        stored = saved["scores"]["candidates"].get(cid)
        if stored is not None and metrics is not None and stored != metrics:
            raise SnapshotError("advanced_saved_candidate_score_replay_mismatch")
        # A restored parent allows a derived current view; old score bytes stay.
        if stored is None and metrics is not None and saved["original_strategy_choices"][cid] != cid:
            current["candidates"][cid] = None
    return {**saved, "scores": current, "current_parent_reverification": True}


def _summary(records, candidate):
    paired = [v for v in records if v["scores"]["M0"] is not None and v["scores"]["candidates"].get(candidate) is not None]
    policy = [v for v in records if v["scores"]["M0"] is not None and v["scores"]["strategy"].get(candidate) is not None]
    def delta(rows, branch):
        return sum(v["scores"][branch][candidate]["scoreline_nll"]-v["scores"]["M0"]["scoreline_nll"] for v in rows)/len(rows) if rows else None
    return {"paired_scored": len(paired), "policy_scored": len(policy), "baseline_scored": sum(v["scores"]["M0"] is not None for v in records),
        "candidate_only_scored": sum(v["scores"]["M0"] is None and v["scores"]["candidates"].get(candidate) is not None for v in records),
        "fallback_scored": sum(v["original_strategy_choices"][candidate] == "M0_fallback" for v in policy),
        "current_candidate_unverified": sum(v["original_strategy_choices"][candidate] == candidate and v["scores"]["candidates"].get(candidate) is None for v in records),
        "mean_paired_scoreline_nll_difference": delta(paired, "candidates"),
        "mean_policy_scoreline_nll_difference": delta(policy, "strategy"),
        "baseline": _mean_metrics([v["scores"]["M0"] for v in paired]),
        "candidate": _mean_metrics([v["scores"]["candidates"][candidate] for v in paired]),
        "confirmation_eligible": False, "betting_returns": None}


def score_run(runtime, league="serie_a", *, config, protocol, source_commit, state_commit=None, clock=None, client_factory=None):
    runtime = Path(runtime).resolve(); _commit(source_commit); _commit(state_commit, optional=True)
    clock = clock or (lambda: datetime.now(timezone.utc)); started = _utc(clock())
    root = _root(runtime, protocol); root.mkdir(parents=True, exist_ok=True)
    competition = next(v for v in config["competitions"] if v["key"] == league)
    report = {"schema_version": VERSION, "league": league, "protocol_sha256": _digest(protocol),
        "source_commit": source_commit, "state_commit": state_commit, "run_started_at": _iso(started),
        "future_scoring_denominator": "all_queue", "research_only": True, "confirmation_eligible": False,
        "queue": [], "issues": [], "api_requests": 0, "betting_returns": None,
        "retained_historical_first_score_refs": []}
    lockpath = runtime.parent / ".advanced-scoring-locks" / (_digest([str(runtime),_digest(protocol)]) + ".lock")
    lockpath.parent.mkdir(parents=True, exist_ok=True)
    client = None; fetched = {}; current_records = []
    with lockpath.open("a") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        queue_index = _VerifiedQueueIndex(runtime, protocol)
        try:
            members = queue_index.members(runtime, league, protocol, started)
        except (ValueError, KeyError, TypeError, OSError) as exc:
            members = []; report["issues"].append({"reason": _reason(exc)})
        for member in members:
            now = _tick(clock, started)
            item = {k: v for k, v in member.items() if k != "observations"}
            item.update(status="pending_capture", candidates={})
            report["queue"].append(item)
            historical_path = root / "scores" / league / str(member["match_id"]) / member["horizon"] / "first.json"
            if historical_path.exists():
                try:
                    historical_ref = _read(historical_path)
                    item["historical_first_score_ref"] = historical_ref
                    report["retained_historical_first_score_refs"].append({"match_id":member["match_id"],
                        "horizon":member["horizon"],"score_ref":historical_ref,"current_verification_not_implied":True})
                except (ValueError, OSError):
                    item["historical_first_pointer_unreadable"] = True
            comparison = load_verified_comparison(runtime, league, member["match_id"], member["horizon"], protocol, now=now, _queue_index=queue_index)
            if comparison["status"] != "available":
                item.update(status="missed_window" if now >= _utc(member["kickoff"]) else member["latest_status"],
                    reason=comparison.get("reason"), baseline_available=False)
                continue
            item.update(baseline_available=comparison["baseline"]["prediction"] is not None, candidates={k: v["status"] for k,v in comparison["candidates"].items()})
            fixture = comparison["fixture"]
            if now < _utc(fixture["kickoff"]) + RESULT_DELAY:
                item.update(status="pending_result", reason="kickoff_plus_four_hours_not_reached")
                continue
            ledger = root / "scores" / league / str(member["match_id"]) / member["horizon"]
            first_path, latest_path = ledger / "first.json", ledger / "latest.json"
            retained = None
            try:
                previous = _read_ref(runtime, _read(latest_path)) if latest_path.exists() else None
                if previous is not None:
                    retained = verified_score(runtime, previous, comparison, competition, now)
                    item["historical_first_score_ref"] = _read(first_path)
                    # Always also validate immutable original; current revision
                    # never overwrites a failed or changed initial score.
                    verified_score(runtime, _read_ref(runtime, _read(first_path)), comparison, competition, now)
                    last_checked = _utc(previous["result_available_at"])
                    if (ledger / "last_checked.json").exists():
                        check = _read_ref(runtime, _read(ledger / "last_checked.json"))
                        envelope = _read_ref(runtime, check["result_ref"])
                        _normalize_result(envelope, fixture, _provider_league(runtime, comparison["payload"]["baseline_payload"], competition), now)
                        if check["observed_at"] != envelope["fetched_at"]:
                            raise SnapshotError("advanced_result_check_time_mismatch")
                        last_checked = max(last_checked,_utc(check["observed_at"]))
                    if now-last_checked < RESULT_REFRESH or now > _utc(fixture["kickoff"])+RESULT_RECHECK_WINDOW:
                        current_records.append(retained)
                        item.update(status="already_scored", current_score_ref=_read(latest_path)); continue
                mid = fixture["match_id"]
                provider = _provider_league(runtime, comparison["payload"]["baseline_payload"], competition)
                if mid not in fetched:
                    if client is None:
                        client = (client_factory or APIClient)(runtime / "cache", max_requests=config.get("max_requests_per_run"),
                            daily_limit=config.get("api_daily_limit"), max_run_seconds=config.get("api_max_run_seconds"))
                        client.status()
                    envelope = client.fetch_fixture(mid, force=True, max_age_seconds=0)
                    _normalize_result(envelope, fixture, provider, _tick(clock, now))
                    fetched[mid] = envelope, _reference(runtime, _immutable(root,"result_observations",envelope))
                envelope, result_ref = fetched[mid]
                result, status = _normalize_result(envelope, fixture, provider, _tick(clock, now))
                check = {"match_id": mid,"horizon":member["horizon"],"observed_at":envelope["fetched_at"],"result_ref":result_ref}
                _pointer(ledger / "last_checked.json", _reference(runtime,_immutable(root,"result_checks",check)))
                if result is None:
                    item.update(status="pending_result", provider_status=status)
                    if retained is not None: current_records.append(retained)
                    continue
                choices = _original_choices(comparison)
                saved = {"schema_version": VERSION,"league":league,"match_id":mid,"horizon":member["horizon"],
                    "protocol_sha256":_digest(protocol),"capture_receipt_ref":comparison["receipt_ref"],
                    "forecast_sha256":comparison["forecast_sha256"],"captured_at":comparison["captured_at"],
                    "first_observed_at":member["first_observed_at"],"original_strategy_choices":choices,
                    "scores":_scores(comparison,result,choices),"result":result,"result_ref":result_ref,
                    "result_available_at":envelope["fetched_at"],"scored_at":_iso(_tick(clock,now)),
                    "target":"regulation_time_90_plus_stoppage","confirmation_eligible":False,"betting_returns":None}
                if previous is None or previous["result"] != result:
                    if previous is not None: saved["revision_of"] = _read(latest_path)
                    ref = _reference(runtime,_immutable(root,"score_versions",saved))
                    _pointer(latest_path,ref)
                    if previous is None: _pointer(first_path,ref,first=True)
                    item["status"] = "scored" if previous is None else "result_revision_appended"
                else:
                    saved = previous; ref = _read(latest_path); item["status"] = "result_unchanged"
                current_records.append(verified_score(runtime,saved,comparison,competition,_tick(clock,now)))
                item["current_score_ref"] = ref
            except (ValueError, APIError, KeyError, TypeError, OSError) as exc:
                item.update(status="failed",reason=_reason(exc))
                report["issues"].append({"match_id":member["match_id"],"horizon":member["horizon"],"reason":_reason(exc)})
                if retained is not None: current_records.append(retained)
        report.update(queue_status_counts=dict(Counter(v["status"] for v in report["queue"])),
            requested_horizon_records=len(report["queue"]),scored_horizon_records=len(current_records),
            retained_historical_first_score_count=len(report["retained_historical_first_score_refs"]),
            scored_independent_matches=len({v["match_id"] for v in current_records}),
            summaries={h:{cid:_summary([r for r in current_records if r["horizon"]==h],cid) for cid in candidate_ids(protocol)} for h in HORIZONS},
            run_completed_at=_iso(_tick(clock,started)))
        confirmation_base = runtime / "advanced_models" / league / _digest(protocol) / "confirmation"
        for horizon in HORIZONS:
            for candidate in candidate_ids(protocol):
                if list(confirmation_base.glob("*/" + candidate + "/" + horizon + "/activation.json")):
                    report["summaries"][horizon][candidate] = {"status":"suppressed_during_registered_confirmation",
                        "reason":"terminal_controller_only; no repeated hypothesis scoring", "confirmation_eligible":False}
        if client is not None:
            report.update(api_requests=client.requests_made,api_daily_remaining=client.daily_remaining,api_quota_status=client.daily_quota_status)
        report["status"] = "partial" if report["issues"] else "complete" if report["queue"] else "no_queue_observations"
        report["report_ref"] = _reference(runtime,_immutable(root,"reports/history",report))
        _pointer(root / "reports" / league / "latest.json",report)
    return int(bool(report["issues"])),report


def verified_confirmation_rows(runtime, league, advanced_protocol, *, include_scores=True, config=None, now=None):
    """Metadata-only mode does not open score or result files.

    Every candidate receives every queue opportunity. Enrollment therefore cannot
    depend on eventual prediction success, result availability or a good score.
    """
    runtime = Path(runtime).resolve(); now = _utc(now or datetime.now(timezone.utc)); output=[]
    config = config or _read(Path(__file__).resolve().parents[1] / "config.json")
    competition = next(v for v in config["competitions"] if v["key"]==league)
    queue_index = _VerifiedQueueIndex(runtime, advanced_protocol)
    for member in queue_index.members(runtime, league, advanced_protocol, now):
        comparison = load_verified_comparison(runtime,league,member["match_id"],member["horizon"],advanced_protocol,now=now, _queue_index=queue_index)
        saved=None; score_ref=None
        status=comparison["status"]
        if include_scores and status=="available":
            latest = _root(runtime,advanced_protocol)/"scores"/league/str(member["match_id"])/member["horizon"]/"latest.json"
            if latest.exists():
                try:
                    score_ref=_read(latest); saved=verified_score(runtime,_read_ref(runtime,score_ref),comparison,competition,now)
                except (ValueError,KeyError,TypeError,OSError): status="score_rejected"
        for cid in candidate_ids(advanced_protocol):
            branch=comparison.get("candidates",{}).get(cid,{})
            ref=branch.get("model_ref") or {}
            choices=_original_choices(comparison) if comparison["status"]=="available" else {}
            output.append({"fixture_id":member["match_id"],"league":league,"horizon":member["horizon"],"candidate_id":cid,
                "kickoff":member["kickoff"],"first_observed_at":member["first_observed_at"],"captured_at":comparison.get("captured_at"),
                "forecast_sha256":comparison.get("forecast_sha256"),"protocol_sha256":_digest(advanced_protocol),
                "candidate_model_sha256":ref.get("sha256"),"result_available_at":saved.get("result_available_at") if saved else None,
                "score_ref":score_ref,"status":"scored" if saved else status,"candidate_status":branch.get("status","unavailable"),
                "baseline_metrics":saved["scores"]["M0"] if saved else None,
                "candidate_metrics":saved["scores"]["candidates"].get(cid) if saved else None,
                "strategy_metrics":saved["scores"]["strategy"].get(cid) if saved else None,
                "original_strategy_choice":choices.get(cid),"first_failure_refs":member["first_failures"],
                "confirmation_eligible":branch.get("confirmation_eligible",False),
                "confirmation_activation_sha256":branch.get("confirmation_activation_sha256"),
                "confirmation_activation_ref":branch.get("confirmation_activation_ref"),
                "first_capture_anchor_ref":comparison.get("first_capture_anchor_ref"),
                "durability_ref":comparison.get("durability_ref")})
    return output


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__);s=p.add_subparsers(dest="command",required=True);r=s.add_parser("score")
    r.add_argument("--runtime",default="runtime");r.add_argument("--league",default="all");r.add_argument("--config",default="config.json")
    r.add_argument("--protocol",default="advanced-models-v1.json");r.add_argument("--source-commit",required=True);r.add_argument("--state-commit")
    a=p.parse_args(argv);config=_read(a.config);protocol=_read(a.protocol);code=0
    leagues=[v["key"] for v in config["competitions"] if v.get("enabled",True)] if a.league=="all" else [a.league]
    for league in leagues:
        status,report=score_run(a.runtime,league,config=config,protocol=protocol,source_commit=a.source_commit,state_commit=a.state_commit)
        code=max(code,status);print(json.dumps({k:report[k] for k in ("league","status","requested_horizon_records","scored_horizon_records","api_requests","report_ref")},ensure_ascii=False))
    return code


if __name__=="__main__": raise SystemExit(main())
