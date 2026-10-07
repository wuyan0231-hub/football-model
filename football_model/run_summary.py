"""Whitelist-only workflow summary; never infer this run from a restored latest file.

The summary is observational. It neither changes forecast eligibility nor changes
the workflow's final failure gate. Missing/corrupt/unassociated inputs are unknown.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re

VERSION = "layered-workflow-summary-v1"
LEAGUES = frozenset("epl laliga bundesliga serie_a ligue_1 bundesliga_2 ligue_2 ucl allsvenskan eredivisie primeira brazil_a j1 k1 mls unl liga_mx segunda argentina_a".split())
OUTCOMES = frozenset(("success", "failure", "cancelled", "skipped", "unknown"))
STAGES = ("restore", "model", "replay", "prospective", "fundamentals", "check", "persist")
COVERAGE_STATES = frozenset(("prospective", "rejected", "unsupported", "capture_error", "no_current_prediction_file", "no_upcoming_matches"))
AVAILABILITY = frozenset(("available", "partial", "unavailable", "no_upcoming", "audit_only", "not_requested", "failed"))
NORMAL_MISSING = frozenset(("coverage_false", "coverage_unknown", "lineup_window_not_open", "not_completed_90_minute_fixture"))
REASONS = NORMAL_MISSING | frozenset((
    "budget_deferred", "provider_error", "invalid_evidence_or_save_failed", "invalid_evidence", "evidence_save_failed",
    "prior_context_unavailable_or_invalid", "saved_history_missing_or_invalid", "fixture_or_coverage_unavailable_or_invalid",
    "feature_build_or_save_failed", "match_started_before_feature_cutoff", "target_not_confirmed_future_NS",
    "evidence_fetch_failed", "diagnostic_save_failed", "collection_dispatch_failed",
    "collection_initialization_or_clock_failed", "prior_not_observed_finished", "audit_only", "no_upcoming",
    "unreliable_index_state", "target_fixture_not_scheduled", "prematch_collection_window_closed",
    "lineup_collection_requires_prematch_or_finished_fixture", "evidence_validation_failed", "snapshot_save_failed",
    "research_replay_is_not_prospective", "capture_at_or_after_kickoff_not_prospective", "missing_observed_wall_clock_semantics",
    "forecast_does_not_belong_to_current_run", "invalid_actual_observation_generation_capture_order",
    "invalid_model_cutoff_fit_generation_order", "missing_prediction", "prediction_identity_mismatch",
    "prediction_training_cutoff_mismatch", "missing_model_version_provenance", "unknown_score_matrix_axes",
    "1X2_probabilities_disagree_with_matrix", "competition_mismatch", "model_snapshot_provenance_mismatch",
    "source_not_supported", "immutable_run_prediction_file_changed", "result_source_missing_or_future_observation",
    "result_identity_or_kickoff_mismatch", "result_observed_before_regulation_maturity",
    "result_source_observation_regressed", "conflicting_result_same_observation_time"))


def _time(value):
    if not isinstance(value, str):
        raise ValueError("invalid_timestamp")
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("invalid_timestamp")
    return result.astimezone(timezone.utc)


def _iso(value):
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _canonical(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False, indent=2) + "\n").encode()


def _digest(value):
    return hashlib.sha256(_canonical(value)).hexdigest()


def _read(path):
    # Summary is read-only; reject links rather than accidentally reading outside runtime.
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError("symlinked_input")
    raw = path.read_bytes()
    value = json.loads(raw, parse_constant=lambda _: (_ for _ in ()).throw(ValueError("invalid_json")))
    if not isinstance(value, dict):
        raise ValueError("invalid_object")
    return value, hashlib.sha256(raw).hexdigest()


def _count(value):
    return value if type(value) is int and value >= 0 else None


def _reason(value):
    if isinstance(value, str) and value in REASONS:
        return value
    # Legacy timestamp exception is mapped exactly; raw exceptions are never emitted.
    if value == "model_fitted_at 必须是包含时区的 ISO 时间字符串。":
        return "model_fitted_at_missing_or_invalid"
    return "unclassified_reason"


def _objects(root, collection):
    directory = root / collection
    if not directory.is_dir() or directory.is_symlink():
        raise ValueError("ledger_collection_missing")
    rows = []
    for path in sorted(directory.glob("*.json")):
        value, _ = _read(path)
        if path.stem != _digest(value):
            raise ValueError("ledger_integrity_failed")
        rows.append(value)
    return rows


def _unknown(reason="missing_or_invalid_current_report"):
    return {"status": "unknown", "reason": reason}


def _associated(path, context, stage, now):
    value, digest = _read(path)
    start = _time(context["stage_started_at"][stage])
    generated = _time(value["generated_at"])
    if not start <= generated <= now:
        raise ValueError("report_outside_current_stage")
    if "run_started_at" in value and not start <= _time(value["run_started_at"]) <= generated:
        raise ValueError("report_outside_current_stage")
    return value, digest


def _model_summary(report):
    requested = report.get("requested_competitions")
    items = report.get("competitions")
    if (not isinstance(requested, list) or not requested or any(k not in LEAGUES for k in requested)
            or len(set(requested)) != len(requested) or not isinstance(items, list)):
        raise ValueError("invalid_competition_scope")
    mapping = {item["key"]: item for item in items if isinstance(item, dict) and item.get("key") in requested}
    if len(mapping) != len(items):
        raise ValueError("duplicate_or_invalid_competitions")
    rows, known = [], []
    unknown = 0
    for key in requested:
        coverage = mapping.get(key, {}).get("forecast_availability", {})
        state = coverage.get("status") if coverage.get("status") in AVAILABILITY else "unknown"
        counts = {field: _count(coverage.get(field)) for field in ("supported_count", "unsupported_count", "upcoming_count")}
        applicable = state not in ("audit_only", "not_requested")
        if applicable:
            if any(v is None for v in counts.values()) or counts["supported_count"] + counts["unsupported_count"] != counts["upcoming_count"]:
                unknown += 1
            else:
                known.append(counts)
        rows.append({"competition": key, "status": state, **counts})
    return {"status": "associated_current_report", "competitions": rows,
            "coverage_status_counts": dict(Counter(r["status"] for r in rows)),
            "unknown_prediction_coverage_competitions": unknown,
            "known_supported_predictions": sum(r["supported_count"] for r in known) if known else None,
            "supported_predictions": sum(r["supported_count"] for r in known) if known and unknown == 0 else None,
            "unsupported_predictions": sum(r["unsupported_count"] for r in known) if known and unknown == 0 else None,
            "prediction_support_is_quality_acceptance": False}


def _current_capture(runtime, report, report_sha, context, now):
    source = _digest({"kind": "run_report", "original_bytes_sha256": report_sha, "content": report})
    start = _time(context["stage_started_at"]["prospective"])
    current, seen = [], set()
    for row in _objects(runtime / "prospective", "coverage"):
        if row.get("run_id") != report["run_id"]:
            continue
        if row.get("report_source") != source or not start <= _time(row["captured_at"]) <= now:
            raise ValueError("coverage_association_failed")
        key = row.get("competition")
        mid = row.get("match_id")
        state = row.get("status")
        if (key not in report["requested_competitions"] or state not in COVERAGE_STATES
                or (mid is not None and (type(mid) is not int or mid < 1)) or (key, mid) in seen):
            raise ValueError("ambiguous_current_coverage")
        if state in ("prospective", "rejected", "unsupported") and mid is None:
            raise ValueError("current_prediction_identity_missing")
        if state == "prospective":
            fid = row.get("forecast_id")
            if not isinstance(fid, str) or not re.fullmatch(r"[0-9a-f]{64}", fid):
                raise ValueError("accepted_forecast_missing")
            forecast, _ = _read(runtime / "prospective" / "forecasts" / (fid + ".json"))
            if (_digest(forecast) != fid or forecast.get("run_id") != report["run_id"]
                    or forecast.get("competition") != key or forecast.get("match_id") != mid
                    or forecast.get("report_source") != source):
                raise ValueError("accepted_forecast_unassociated")
        seen.add((key, mid))
        current.append(row)
    if not current:
        raise ValueError("no_current_coverage_records")
    states = Counter(r["status"] for r in current)
    reasons = Counter(_reason(r.get("reason")) for r in current if r["status"] in ("rejected", "unsupported", "capture_error"))
    by_competition = []
    for key in report["requested_competitions"]:
        rows = [r for r in current if r["competition"] == key]
        by_competition.append({"competition": key, "observed_records": len(rows) if rows else None,
            "status_counts": dict(Counter(r["status"] for r in rows)) if rows else None,
            "reason_counts": dict(Counter(_reason(r.get("reason")) for r in rows
                if r["status"] in ("rejected", "unsupported", "capture_error"))) if rows else None})
    return {"status": "associated_current_ledger_records", "count_basis": "current_run_coverage_objects_no_cumulative_subtraction",
            "observed_records": len(current), "observed_accepted_predictions": states["prospective"],
            "observed_rejected_predictions": states["rejected"], "observed_unsupported_predictions": states["unsupported"],
            "status_counts": dict(states), "reason_counts": dict(reasons), "competitions": by_competition}


def _cumulative(value):
    fields = ("forecast_versions", "unique_primary_matches_by_protocol", "result_versions")
    result = {key: _count(value.get(key)) for key in fields}
    statuses = value.get("match_statuses")
    if not isinstance(statuses, list):
        result.update(mature_scored_matches_by_protocol=None, pending_matches_by_protocol=None,
                      unique_matches_across_protocols=None)
    else:
        identities, protocol_identities, mature_identities = set(), set(), set()
        scored = pending = 0
        for row in statuses:
            key, mid = row.get("competition"), row.get("match_id")
            pid = row.get("protocol_id")
            if key not in LEAGUES or type(mid) is not int or mid < 1 or not isinstance(pid, str):
                raise ValueError("invalid_cumulative_identity")
            if (pid, key, mid) in protocol_identities:
                raise ValueError("duplicate_cumulative_identity")
            protocol_identities.add((pid, key, mid))
            identities.add((key, mid))
            if row.get("status") not in ("scored", "pending", "result_identity_conflict"):
                raise ValueError("unknown_cumulative_status")
            if row.get("status") == "scored":
                mature_identities.add((key, mid))
            scored += row.get("status") == "scored"
            pending += row.get("status") == "pending"
        if result["unique_primary_matches_by_protocol"] is not None and len(protocol_identities) != result["unique_primary_matches_by_protocol"]:
            raise ValueError("cumulative_count_mismatch")
        result.update(mature_scored_matches_by_protocol=scored, pending_matches_by_protocol=pending,
                      unique_matches_across_protocols=len(identities), mature_unique_matches_across_protocols=len(mature_identities))
    return {"status": "associated_current_cumulative_view", **result,
            "counts_are_accuracy_or_profit_acceptance": False}


def _fundamentals_summary(value, model_run_id):
    rows = value.get("matches")
    if not isinstance(rows, list):
        raise ValueError("invalid_collection_rows")
    counts = Counter()
    reasons = Counter()
    issues = value.get("issues")
    issue_counts = (dict(Counter(_reason(row.get("reason")) for row in issues if isinstance(row, dict)))
                    if isinstance(issues, list) else None)
    for row in rows:
        if row.get("forecast_run_id") != model_run_id:
            raise ValueError("collection_forecast_run_mismatch")
        missing = row.get("missing")
        if not isinstance(missing, list):
            counts["unknown"] += 1
            continue
        names = [_reason(m.get("reason")) for m in missing if isinstance(m, dict)]
        if len(names) != len(missing):
            counts["unknown"] += 1
            continue
        if row.get("reason") is not None:
            names.append(_reason(row["reason"]))
        reasons.update(names)
        if row.get("status") != "features_saved":
            counts["partial"] += 1
        else:
            counts["features_saved"] += 1
            counts["complete" if not names else "normal_missing" if set(names) <= NORMAL_MISSING else "partial"] += 1
    return {"status": value.get("status") if value.get("status") in ("complete", "partial", "no_upcoming") else "unknown",
            "candidate_matches": _count(value.get("candidate_count")), "reported_matches": len(rows),
            "complete_collection_matches": counts["complete"], "partial_collection_matches": counts["partial"],
            "normal_missing_matches": counts["normal_missing"], "unknown_collection_matches": counts["unknown"],
            "features_saved_matches": counts["features_saved"], "reason_counts": dict(reasons),
            "global_issue_counts": issue_counts,
            "completeness_scope": "requested_collection_operations_not_all_feature_fields_or_all_competitions",
            "fundamentals_used_in_probabilities": False, "is_model_probability_gate": False}


def build_summary(runtime, context, outcomes, *, now=None):
    """Return only fixed keys/enums, counts, verified timestamps and run identifiers."""
    now = now or datetime.now(timezone.utc)
    runtime = Path(runtime)
    states = {k: outcomes.get(k) if outcomes.get(k) in OUTCOMES else "unknown" for k in STAGES}
    required = ("restore", "model", "replay", "check", "persist") + (("prospective",) if context.get("mode") == "train" else ())
    not_successful = [k for k in required if states[k] != "success"]
    result = {"schema_version": VERSION, "generated_at": _iso(now), "step_outcomes": states,
              "required_steps": list(required), "necessary_steps_outcome": "failure" if not_successful else "success",
              "necessary_steps_not_successful": not_successful,
              "job_conclusion": "not_observed_summary_precedes_final_gate_and_artifact_upload",
              "output_check_outcome": states["check"], "state_save_outcome": states["persist"],
              "summary_saved_in_same_model_state_commit": False,
              "model": _unknown(), "prospective_current": _unknown(), "prospective_cumulative": _unknown(),
              "fundamentals": _unknown()}
    if states["model"] not in ("success", "failure"):
        return result
    try:
        report, sha = _associated(runtime / "reports" / "latest.json", context, "model", now)
        started = _time(report["run_started_at"])
        if (report.get("run_id") != started.strftime("%Y%m%dT%H%M%S%fZ")
                or report.get("source_commit") != context.get("source_commit")
                or not re.fullmatch(r"[0-9a-f]{40}", context.get("source_commit", ""))
                or report.get("mode") != context.get("mode")):
            raise ValueError("model_run_association_failed")
        if context.get("league") != "all" and report.get("requested_competitions") != [context.get("league")]:
            raise ValueError("model_scope_association_failed")
        result["model"] = _model_summary(report)
        result["model_run_id"] = report["run_id"]
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return result
    if states["prospective"] in ("success", "failure"):
        try:
            result["prospective_current"] = _current_capture(runtime, report, sha, context, now)
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            result["prospective_current"] = _unknown("current_coverage_missing_invalid_or_unassociated")
        try:
            current, _ = _associated(runtime / "prospective" / "latest.json", context, "prospective", now)
            if current.get("current_run_id") != report["run_id"]:
                raise ValueError("prospective_run_mismatch")
            result["prospective_cumulative"] = _cumulative(current)
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            pass
    if states["fundamentals"] in ("success", "failure"):
        try:
            facts, _ = _associated(runtime / "fundamentals" / "reports" / "latest.json", context, "fundamentals", now)
            result["fundamentals"] = _fundamentals_summary(facts, report["run_id"])
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            pass
    return result


def markdown(value):
    def n(row, key):
        return str(row[key]) if row.get(key) is not None else "unknown"
    model, current, cumulative, facts = (value[k] for k in ("model", "prospective_current", "prospective_cumulative", "fundamentals"))
    lines = ["# 足球模型分层运行结果", "", "| 层次 | 本次实际结果 |", "|---|---|",
        f"| 必要步骤 | {value['necessary_steps_outcome']} |",
        f"| 模型 CLI | {value['step_outcomes']['model']}；已知支持 {n(model, 'known_supported_predictions')} 场；范围完整时合计 {n(model, 'supported_predictions')} 场 |",
        f"| 本轮前瞻 | {value['step_outcomes']['prospective']}；观测到接受 {n(current, 'observed_accepted_predictions')}、拒绝 {n(current, 'observed_rejected_predictions')}、原模型不支持 {n(current, 'observed_unsupported_predictions')} |",
        f"| 累计前瞻 | 版本 {n(cumulative, 'forecast_versions')}；协议内去重 {n(cumulative, 'unique_primary_matches_by_protocol')}；跨协议独立比赛 {n(cumulative, 'unique_matches_across_protocols')} |",
        f"| 累计成熟结果 | 协议内已评分 {n(cumulative, 'mature_scored_matches_by_protocol')}；待赛果 {n(cumulative, 'pending_matches_by_protocol')}；结果版本 {n(cumulative, 'result_versions')} |",
        f"| 基本面采集 | {value['step_outcomes']['fundamentals']}；报告 {facts['status']}；采集完整 {n(facts, 'complete_collection_matches')}、部分 {n(facts, 'partial_collection_matches')}、正常缺失 {n(facts, 'normal_missing_matches')}、未知 {n(facts, 'unknown_collection_matches')} |",
        f"| 输出检查 | {value['output_check_outcome']} |",
        f"| 状态保存 | {value['state_save_outcome']} |",
        f"| 回放包检查 | {value['step_outcomes']['replay']} |", "",
        "本轮前瞻按关联的 coverage 对象计数；步骤未完成时仅表示已观测记录，不代表目标全集。累计数未用于相减推算本轮。",
        "基本面的完整仅指本次请求的采集操作；正常缺失不代表数值零，特征已保存不代表全字段完整或已经调整概率。",
        "模型支持、前瞻资格、保存成功与预测准确率验收分别处理。基本面局部失败不否决独立比分模型。", ""]
    for label, row in (("本轮前瞻原因", current), ("基本面原因", facts)):
        reasons = row.get("reason_counts")
        lines.append(label + "：" + ("；".join(f"{key}={count}" for key, count in sorted(reasons.items())) if reasons else "unknown / 未记录原因"))
    if facts.get("global_issue_counts"):
        lines.append("基本面全局事项：" + "；".join(f"{key}={count}" for key, count in sorted(facts["global_issue_counts"].items())))
    lines.extend(["", "这些是步骤真实 outcome，不是 continue-on-error 处理后的 conclusion；必要步骤结果不是最终 GitHub job conclusion。",
        "本摘要在状态保存之后生成，以免预告保存成功；摘要自身在运行摘要和便捷附件中，不声称已进入同次 model-state 提交。", ""])
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description="本轮关联与分层运行摘要；不接入认证、不改模型")
    parser.add_argument("command", choices=("begin", "mark", "render"))
    parser.add_argument("--context", required=True)
    parser.add_argument("--stage", choices=("model", "prospective", "fundamentals"))
    parser.add_argument("--runtime", default="runtime")
    parser.add_argument("--output")
    parser.add_argument("--summary")
    args = parser.parse_args(argv)
    path = Path(args.context)
    now = datetime.now(timezone.utc)
    if args.command == "begin":
        sha, mode, league = os.environ.get("GITHUB_SHA", ""), os.environ.get("MODE"), os.environ.get("LEAGUE")
        if not re.fullmatch(r"[0-9a-f]{40}", sha) or mode not in ("audit", "train") or league not in LEAGUES | {"all"}:
            raise SystemExit("invalid_workflow_context")
        value = {"schema_version": VERSION, "started_at": _iso(now), "source_commit": sha,
                 "mode": mode, "league": league, "stage_started_at": {}}
        path.write_bytes(_canonical(value))
    elif args.command == "mark":
        value, _ = _read(path)
        if args.stage is None or args.stage in value["stage_started_at"]:
            raise SystemExit("invalid_or_repeated_stage_marker")
        value["stage_started_at"][args.stage] = _iso(now)
        path.write_bytes(_canonical(value))
    else:
        try:
            value, _ = _read(path)
        except (OSError, ValueError, TypeError):
            value = {"mode": os.environ.get("MODE")}
        outcomes = {k: os.environ.get(k.upper() + "_OUTCOME") for k in STAGES}
        report = build_summary(args.runtime, value, outcomes, now=now)
        if args.output:
            Path(args.output).write_bytes(_canonical(report))
        if args.summary:
            with Path(args.summary).open("a", encoding="utf-8") as handle:
                handle.write(markdown(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
