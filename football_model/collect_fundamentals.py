"""Explicit, bounded evidence collection; imports never access the network.

Saved forecasts locate fixtures only. This command neither trains a model nor
changes saved predictions. Same-competition history does not establish complete
all-competition workload. Current downloads cannot backfill historical cutoffs.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
from pathlib import Path

from .competition_scope import select_competitions
from .data import APIClient, APIError, DataError, RequestBudgetError, _integer, _iso, _utc
from .fundamentals import (build_features, collect_fixture_evidence,
                           failure_diagnostic, quantitative_scope, save_snapshot)
from .integration import _entry_for_match


def _read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"),
                      parse_constant=lambda _: (_ for _ in ()).throw(DataError("non-finite JSON")))


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"


def _immutable(directory, value):
    payload = _canonical(value)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    path = Path(directory) / (digest + ".json")
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8") as handle:
            handle.write(payload)
    except FileExistsError:
        if path.read_text(encoding="utf-8") != payload:
            raise DataError("immutable artifact collision") from None
    return path


def _identity(row):
    if not isinstance(row, dict):
        raise DataError("invalid fixture identity")
    result = {name: _integer(row.get(name), name, 1) for name in ("match_id", "home_id", "away_id")}
    if result["home_id"] == result["away_id"]:
        raise DataError("duplicate team identity")
    result["kickoff"] = _iso(_utc(row.get("kickoff")))
    return result


def _verified_fixture(envelope, expected, metadata, now):
    """Only exact source responses establish current status and season coverage."""
    if (not isinstance(envelope, dict) or envelope.get("errors")
            or envelope.get("endpoint") != "/fixtures"
            or envelope.get("params") != {"id": expected["match_id"]}
            or not isinstance(envelope.get("paging", {}), dict)
            or envelope.get("paging", {}).get("total", 1) != 1):
        raise DataError("fixture_source_invalid")
    observed = _utc(envelope.get("fetched_at"))
    if observed > now:
        raise DataError("fixture_observation_in_future")
    rows = envelope.get("response")
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
        raise DataError("fixture_response_not_unique")
    raw = rows[0]
    fixture = raw.get("fixture")
    teams = raw.get("teams")
    if (not isinstance(fixture, dict) or not isinstance(fixture.get("status"), dict)
            or not isinstance(raw.get("league"), dict) or not isinstance(teams, dict)
            or any(not isinstance(teams.get(side), dict) for side in ("home", "away"))):
        raise DataError("fixture_structure_invalid")
    identity = _identity({"match_id": fixture["id"], "kickoff": fixture["date"],
                          "home_id": raw["teams"]["home"]["id"],
                          "away_id": raw["teams"]["away"]["id"]})
    if identity != _identity(expected):
        raise DataError("fixture_identity_changed")
    if fixture.get("timestamp") is not None and (
            type(fixture["timestamp"]) is not int
            or abs(fixture["timestamp"] - _utc(identity["kickoff"]).timestamp()) > 1):
        raise DataError("fixture_timestamp_conflict")
    if raw["league"]["id"] != metadata["league"]["id"]:
        raise DataError("fixture_league_mismatch")
    season = _integer(raw["league"]["season"], "season", 1900)
    coverages = [s["coverage"] for s in metadata["seasons"] if s.get("year") == season]
    if len(coverages) != 1 or not isinstance(coverages[0], dict):
        raise DataError("season_coverage_missing")
    status = fixture.get("status", {}).get("short")
    if not isinstance(status, str):
        raise DataError("fixture_status_missing")
    return {**identity, "status": status, "observed_at": _iso(observed)}, coverages[0], season


def _history_rows(runtime, league, fixture, now, count):
    payload = _read(runtime / "data" / f"{league}.json")
    if not isinstance(payload, dict) or not isinstance(payload.get("matches"), list):
        raise DataError("history_file_invalid")
    if _utc(payload.get("observed_at")) > now:
        raise DataError("history_observation_in_future")
    rows, seen = [], set()
    for raw in payload["matches"]:
        row = _identity(raw)
        if row["match_id"] in seen:
            raise DataError("history_duplicate_fixture")
        seen.add(row["match_id"])
        if row["match_id"] != fixture["match_id"] and _utc(row["kickoff"]) < now:
            rows.append(row)
    rows.sort(key=lambda r: (r["kickoff"], r["match_id"]), reverse=True)
    selected = {}
    for tid in (fixture["home_id"], fixture["away_id"]):
        relevant = [row for row in rows if tid in (row["home_id"], row["away_id"])][:count]
        selected.update({row["match_id"]: row for row in relevant})
    return sorted(selected.values(), key=lambda r: (r["kickoff"], r["match_id"]), reverse=True)


def _candidates(runtime, config, league, match_id, start, horizon):
    candidates, issues = [], []
    chosen = select_competitions(config, league)
    if not chosen:
        raise DataError("league_not_configured")
    for competition in chosen:
        key = competition["key"]
        try:
            league_candidates = []
            entry = _entry_for_match(runtime, config, key)
            state = entry["status"]
            if state in ("no_upcoming", "audit_only", "no_run", "no_training_forecast"):
                issues.append({"league": key, "reason": state, "severity": "info"})
                continue
            if state not in ("available", "unsupported_only"):
                issues.append({"league": key, "reason": "unreliable_index_state", "index_status": state,
                               "severity": "error"})
                continue
            attempt = entry["latest_successful_forecast"]
            rows = _read(runtime / "predictions" / attempt["run_id"] / f"{key}.json")
            seen = set()
            for raw in rows:
                row = _identity(raw)
                if row["match_id"] in seen:
                    raise DataError("duplicate forecast fixture")
                seen.add(row["match_id"])
                if match_id is not None and row["match_id"] != match_id:
                    continue
                if not start < _utc(row["kickoff"]) <= start + timedelta(hours=horizon):
                    continue
                if _utc(raw.get("forecast_generated_at", raw.get("generated_at", attempt["generated_at"]))) > start:
                    raise DataError("forecast observed in future")
                league_candidates.append({"league": key, "competition": competition, "fixture": row,
                    "forecast_status": raw.get("status"), "forecast_run_id": attempt["run_id"]})
            candidates.extend(league_candidates)
        except (ValueError, KeyError, TypeError, OSError):
            # Do not print arbitrary malformed source contents or exception text.
            issues.append({"league": key, "reason": "index_or_forecast_invalid", "severity": "error"})
    candidates.sort(key=lambda r: (r["fixture"]["kickoff"], r["league"], r["fixture"]["match_id"]))
    if match_id is not None and not candidates:
        issues.append({"match_id": match_id, "reason": "requested_match_unavailable", "severity": "error"})
    return candidates, issues


def collect_run(config, runtime, league="all", match_id=None, *, horizon_hours=24,
                recent_matches=5, client=None, clock=None):
    """Collect immutable source snapshots; return (exit_code, report).

    Missing provider coverage and unpublished lineups are expected omissions.
    Budget/provider/identity failures are partial runs, not synthetic zero data.
    A no-fixture window never constructs a client or probes account status.
    """
    if type(horizon_hours) not in (int, float) or not math.isfinite(horizon_hours) or not 0 < horizon_hours <= 168:
        raise ValueError("horizon_hours 必须大于0且不超过168")
    _integer(recent_matches, "recent_matches", 1)
    if recent_matches > 20:
        raise ValueError("recent_matches 不得超过20")
    if match_id is not None:
        _integer(match_id, "match_id", 1)
    clock = clock or (lambda: datetime.now(timezone.utc))
    started = _utc(clock())
    runtime = Path(runtime)
    root = runtime / "fundamentals"
    run_id = started.strftime("%Y%m%dT%H%M%S%fZ")
    report_path = root / "reports" / "history" / f"{run_id}.json"
    if report_path.exists():
        raise DataError("采集运行ID已存在；不能覆盖旧记录")
    report = {"schema_version": 1, "run_id": run_id, "run_started_at": _iso(started),
              "horizon_hours": horizon_hours, "recent_matches": recent_matches,
              "history_scope": "saved_same_competition_matches", "all_competitions_complete": False,
              "matches": [], "issues": [], **quantitative_scope()}

    def now():
        value = _utc(clock())
        if value < started:
            raise DataError("collection clock moved backwards")
        return value

    def record_failure(item, diagnostic):
        summary = {k: v for k, v in diagnostic.items() if k != "payload"}
        try:
            path = _immutable(root / "failures" / str(diagnostic["match_id"]), diagnostic)
            summary["diagnostic_path"] = str(path.relative_to(runtime))
        except (OSError, ValueError, TypeError):
            summary["diagnostic_save_failed"] = True
            item["missing"].append({"match_id": diagnostic["match_id"], "kind": diagnostic["kind"],
                                    "reason": "diagnostic_save_failed", "stage": "diagnostic_save",
                                    "category": "storage"})
        item["diagnostics"].append(summary)

    halted = False
    metadata_cache, fixture_cache = {}, {}
    staged = []
    initialization_stage = "candidate_lookup"
    try:
        candidates, report["issues"] = _candidates(runtime, config, league, match_id, started, horizon_hours)
        report["candidate_count"] = len(candidates)
        if candidates:
            initialization_stage = "client_initialization"
            client = client or APIClient(runtime / "cache",
                max_requests=config.get("max_requests_per_run"), daily_limit=config.get("api_daily_limit"),
                max_run_seconds=min(900, config.get("api_max_run_seconds") or 900), clock=clock)
            initialization_stage = "account_budget_check"
            client.status()
        for candidate in candidates:
            item = {k: candidate[k] for k in ("league", "forecast_status", "forecast_run_id")}
            item.update(match_id=candidate["fixture"]["match_id"], status="pending",
                        snapshot_paths=[], fixture_context_paths=[], missing=[],
                        diagnostics=[], quality_diagnostics=[])
            report["matches"].append(item)
            if halted:
                item.update(status="deferred", reason="budget_deferred")
                continue
            key = candidate["league"]

            def context(expected):
                mid = expected["match_id"]
                envelope, stage = None, "fixture_fetch"
                try:
                    if mid not in fixture_cache:
                        fixture_cache[mid] = client.fetch_fixture(mid, force=True)
                    envelope = fixture_cache[mid]
                    stage = "fixture_validate"
                    fixture, coverage, season = _verified_fixture(envelope, expected, metadata, now())
                    evidence = {"provider": "api-football", "fixture_response": envelope,
                                "coverage": coverage, "coverage_observed_at": metadata["fetched_at"],
                                "league_id": metadata["league"]["id"], "season": season}
                    stage = "fixture_save"
                    path = _immutable(root / "fixtures" / str(mid), evidence)
                    item["fixture_context_paths"].append(str(path.relative_to(runtime)))
                    return fixture, coverage
                except (APIError, ValueError, KeyError, TypeError, OSError) as exc:
                    diagnostic = failure_diagnostic("fixture_history", expected, stage=stage,
                        error=exc, checked_at=now(), envelope=envelope)
                    diagnostic["endpoint"] = "/fixtures"
                    record_failure(item, diagnostic)
                    raise

            def collect(fixture, coverage, kinds):
                nonlocal halted
                for kind in kinds:
                    if halted:
                        item["missing"].append({"match_id": fixture["match_id"], "kind": kind,
                                                "reason": "budget_deferred"})
                        continue
                    try:
                        result = collect_fixture_evidence(client, fixture, coverage=coverage,
                            observed_at=fixture["observed_at"], collection_at=now(), kinds=(kind,), clock=now)
                        for diagnostic in result["diagnostics"]:
                            record_failure(item, diagnostic)
                        item["quality_diagnostics"].extend(result["quality_diagnostics"])
                        for snapshot in result["snapshots"]:
                            try:
                                path = save_snapshot(root / "snapshots", snapshot)
                                item["snapshot_paths"].append(str(path.relative_to(runtime)))
                            except (ValueError, KeyError, TypeError, OSError) as exc:
                                envelope = {"endpoint": snapshot["endpoint"], "params": snapshot["params"],
                                    "fetched_at": snapshot["observed_at"], "response": snapshot["raw_response"]}
                                record_failure(item, failure_diagnostic(kind, fixture, stage="snapshot_save",
                                    error=exc, checked_at=now(), envelope=envelope,
                                    context_observed_at=fixture["observed_at"]))
                                item["missing"].append({"match_id": fixture["match_id"], "kind": kind,
                                    "reason": "snapshot_save_failed", "stage": "snapshot_save"})
                        for missing in result["missing"]:
                            item["missing"].append({"match_id": fixture["match_id"], **missing})
                            if missing["reason"] == "budget_deferred":
                                halted = True
                    except (ValueError, KeyError, TypeError, OSError) as exc:
                        record_failure(item, failure_diagnostic(kind, fixture, stage="collection_dispatch",
                            error=exc, checked_at=now()))
                        item["missing"].append({"match_id": fixture["match_id"], "kind": kind,
                                                "reason": "collection_dispatch_failed"})

            candidate_stage = "metadata_fetch"
            try:
                if key not in metadata_cache:
                    metadata_cache[key] = client.resolve_league(candidate["competition"], force=True)
                metadata = metadata_cache[key]
                candidate_stage = "metadata_validate"
                if _utc(metadata["fetched_at"]) > now():
                    raise DataError("coverage_observation_in_future")
                candidate_stage = "target_context"
                target, coverage = context(candidate["fixture"])
                if target["status"] != "NS" or _utc(target["kickoff"]) <= now():
                    item.update(status="skipped", reason="target_not_confirmed_future_NS")
                    continue
                collect(target, coverage, ("injuries", "lineups"))
                observed_history = []
                try:
                    prior_rows = _history_rows(runtime, key, target, now(), recent_matches)
                except (ValueError, KeyError, TypeError, OSError) as exc:
                    prior_rows = []
                    record_failure(item, failure_diagnostic("fixture_history", target,
                        stage="saved_history_load", error=exc, checked_at=now()))
                    item["missing"].append({"kind": "fixture_history", "reason": "saved_history_missing_or_invalid"})
                for prior in prior_rows:
                    if halted:
                        item["missing"].append({"kind": "fixture_history", "match_id": prior["match_id"],
                                                "reason": "budget_deferred"})
                        continue
                    try:
                        finished, prior_coverage = context(prior)
                        if finished["status"] not in ("FT", "AET", "PEN"):
                            item["missing"].append({"kind": "fixture_history", "match_id": prior["match_id"],
                                                    "reason": "prior_not_observed_finished"})
                            continue
                        observed_history.append(finished)
                        kinds = ("lineups", "statistics") if finished["status"] == "FT" else ("lineups",)
                        if finished["status"] != "FT":
                            item["missing"].append({"kind": "statistics", "match_id": prior["match_id"],
                                                    "reason": "not_completed_90_minute_fixture"})
                        collect(finished, prior_coverage, kinds)
                    except RequestBudgetError:
                        halted = True
                        item["missing"].append({"kind": "fixture_history", "match_id": prior["match_id"],
                                                "reason": "budget_deferred"})
                    except (APIError, ValueError, KeyError, TypeError, OSError):
                        item["missing"].append({"kind": "fixture_history", "match_id": prior["match_id"],
                                                "reason": "prior_context_unavailable_or_invalid"})
                staged.append((item, target, observed_history))
                item["status"] = "collected"
            except RequestBudgetError:
                halted = True
                item.update(status="deferred", reason="budget_deferred")
            except (APIError, ValueError, KeyError, TypeError, OSError) as exc:
                if candidate_stage.startswith("metadata"):
                    diagnostic = failure_diagnostic("coverage", candidate["fixture"],
                        stage=candidate_stage, error=exc, checked_at=now())
                    diagnostic["endpoint"] = "/leagues"
                    record_failure(item, diagnostic)
                item.update(status="failed", reason="fixture_or_coverage_unavailable_or_invalid")
        # This is the real end-of-requests time, never a caller-selected past date.
        cutoff = now()
        for item, target, history in staged:
            if cutoff >= _utc(target["kickoff"]):
                item.update(status="evidence_only", reason="match_started_before_feature_cutoff")
                continue
            feature_stage = "snapshot_load"
            try:
                # Keep corruption in unrelated fixtures from discarding this match.
                ids = {target["match_id"]} | {row["match_id"] for row in history}
                snapshots = []
                for kind in ("injuries", "lineups", "statistics"):
                    for mid in ids:
                        for path in sorted((root / "snapshots" / kind / str(mid)).glob("*.json")):
                            snapshots.append(_read(path))
                feature_stage = "feature_build"
                features = build_features(target, cutoff=cutoff, snapshots=snapshots,
                    fixture_history=history, recent_matches=recent_matches)
                features["collection_provenance"] = {"run_id": run_id, "league": item["league"],
                    "forecast_run_id": item["forecast_run_id"], "fixture_context_paths": item["fixture_context_paths"],
                    "missing": item["missing"], "history_scope": "saved_same_competition_matches",
                    "all_competitions_complete": False}
                feature_stage = "feature_save"
                path = _immutable(root / "features" / str(target["match_id"]), features)
                item.update(status="features_saved", feature_path=str(path.relative_to(runtime)),
                            feature_cutoff=_iso(cutoff), fundamentals_used_in_probabilities=False)
            except (ValueError, KeyError, TypeError, OSError) as exc:
                record_failure(item, failure_diagnostic("features", target, stage=feature_stage,
                    error=exc, checked_at=now()))
                item.update(status="evidence_only", reason="feature_build_or_save_failed")
        report["forecast_cutoff"] = _iso(cutoff)
    except (APIError, ValueError, KeyError, TypeError, OSError) as exc:
        report["issues"].append({"reason": "collection_initialization_or_clock_failed", "severity": "error",
            "stage": initialization_stage, "category": "budget" if isinstance(exc, RequestBudgetError)
            else "provider" if isinstance(exc, APIError) else "storage" if isinstance(exc, OSError) else "validation"})
    errors = {"budget_deferred", "provider_error", "invalid_evidence_or_save_failed",
              "prior_context_unavailable_or_invalid", "saved_history_missing_or_invalid",
              "invalid_evidence", "evidence_fetch_failed", "snapshot_save_failed",
              "diagnostic_save_failed", "collection_dispatch_failed"}
    partial = (any(i.get("severity") == "error" for i in report["issues"])
        or any(m["status"] in ("failed", "deferred", "evidence_only")
               or any(x["reason"] in errors for x in m["missing"]) for m in report["matches"]))
    report.update(status="partial" if partial else "complete" if report["matches"] else "no_upcoming",
                  generated_at=_iso(now()), api_requests=getattr(client, "requests_made", 0),
                  exit_code=1 if partial else 0)
    report["api_budget"] = client.budget_snapshot() if client and callable(getattr(client, "budget_snapshot", None)) else None
    report_path.parent.mkdir(parents=True, exist_ok=True)
    with report_path.open("x", encoding="utf-8") as handle:
        handle.write(_canonical(report))
    latest = root / "reports" / "latest.json"
    temporary = latest.with_suffix(".tmp")
    temporary.write_text(_canonical(report), encoding="utf-8")
    temporary.replace(latest)
    return report["exit_code"], report


def main(argv=None):
    parser = argparse.ArgumentParser(description="采集并保存基本面证据；不改变模型或预测")
    parser.add_argument("--runtime", default="runtime")
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--league", default="all")
    parser.add_argument("--match-id", type=int)
    parser.add_argument("--horizon-hours", type=float, default=24)
    parser.add_argument("--recent-matches", type=int, default=5)
    args = parser.parse_args(argv)
    try:
        code, report = collect_run(_read(args.config), args.runtime, args.league, args.match_id,
                                  horizon_hours=args.horizon_hours, recent_matches=args.recent_matches)
    except (ValueError, KeyError, TypeError, OSError):
        print("基本面采集未完成：配置、索引或保存路径无效；未打印原始异常。")
        return 2
    print(f"基本面采集：{report['status']}；{len(report['matches'])}场；概率调整未启用。")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
