"""Offline coverage diagnostics for immutable fixed-holdout validation outputs.

This module never refits, chooses a split, adjusts probabilities, or approves a
model. Matching observations are necessary for descriptive paired scores; they
do not establish causal improvement or point-in-time/prospective qualification.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta
import math
from typing import Any


OUTCOMES = ("home_win", "draw", "away_win")
TARGET_SCOPE = "regulation_time_90_plus_stoppage"
FLOOR = 1e-15


def _timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return result if result.tzinfo is not None else None
    except ValueError:
        return None


def _identifier(value: Any) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    result = str(value).strip()
    return result if result else None


def _probabilities(value: Any) -> list[float] | None:
    if not isinstance(value, dict):
        return None
    values = [value.get(key) for key in OUTCOMES]
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) or
           not math.isfinite(v) or not 0 <= v <= 1 for v in values):
        return None
    if not math.isclose(sum(values), 1.0, rel_tol=0, abs_tol=1e-8):
        return None
    return [float(v) for v in values]


def _row_errors(row: dict[str, Any], report: dict[str, Any], model: dict[str, Any]) -> list[str]:
    if row.get("status") != "scored":
        return []
    errors = []
    if any(_identifier(row.get(k)) is None for k in ("home_id", "away_id")):
        errors.append("missing_team_identity")
    kickoff = _timestamp(row.get("kickoff"))
    split = report.get("split", {})
    if kickoff is None:
        errors.append("missing_or_invalid_kickoff")
    else:
        start, end = _timestamp(split.get("test_start")), _timestamp(split.get("test_end"))
        if start is None or end is None or not start <= kickoff <= end:
            errors.append("kickoff_outside_or_unknown_evaluation_window")
        cutoff = _timestamp(split.get("train_cutoff"))
        if cutoff is None or kickoff < cutoff:
            errors.append("kickoff_before_or_unknown_training_cutoff")
        available = _timestamp(report.get("evaluation_data_cutoff"))
        delay = split.get("result_availability_delay_hours")
        if (available is None or isinstance(delay, bool) or not isinstance(delay, (int, float)) or
                not math.isfinite(delay) or delay < 0):
            errors.append("unknown_result_availability_rule")
        elif kickoff + timedelta(hours=delay) > available:
            errors.append("result_unavailable_at_evaluation_cutoff")
    if any(type(row.get(k)) is not int or row[k] < 0
           for k in ("actual_home_goals", "actual_away_goals")):
        errors.append("missing_or_invalid_regulation_result")
    forecast = row.get("forecast")
    if not isinstance(forecast, dict):
        return errors + ["missing_forecast"]
    for field in ("model_version", "model_scope"):
        if not model.get(field) or forecast.get(field) != model[field]:
            errors.append(f"forecast_validation_{field}_mismatch")
    if _probabilities(forecast.get("probabilities")) is None:
        errors.append("invalid_1x2_probabilities")
    if _timestamp(forecast.get("training_cutoff")) is None:
        errors.append("missing_or_invalid_forecast_training_cutoff")
    elif _timestamp(forecast["training_cutoff"]) != _timestamp(report.get("split", {}).get("train_cutoff")):
        errors.append("forecast_report_training_cutoff_mismatch")
    for field in ("match_id", "home_id", "away_id"):
        if field in forecast and _identifier(forecast[field]) != _identifier(row.get(field)):
            errors.append(f"forecast_{field}_mismatch")
    if "kickoff" in forecast and _timestamp(forecast["kickoff"]) != _timestamp(row.get("kickoff")):
        errors.append("forecast_kickoff_mismatch")
    return errors


def _index(validation: dict[str, Any]) -> dict[str, Any]:
    report = validation.get("report", {})
    if not isinstance(report, dict):
        report = {}
    model = validation.get("model", {})
    if not isinstance(model, dict):
        model = {}
    rows = validation.get("predictions")
    errors: list[dict[str, Any]] = []
    if not isinstance(rows, list):
        rows = []
        errors.append({"reason": "missing_per_match_predictions"})
    grouped: dict[str, list[dict[str, Any]]] = {}
    for position, row in enumerate(rows):
        if not isinstance(row, dict) or _identifier(row.get("match_id")) is None:
            errors.append({"position": position, "reason": "missing_match_identity"})
            continue
        grouped.setdefault(_identifier(row["match_id"]), []).append(row)
    unique, scored, excluded = {}, {}, []
    for match_id, matches in grouped.items():
        if len(matches) != 1:
            errors.append({"match_id": match_id, "reason": "duplicate_match_id", "count": len(matches)})
            continue
        row = matches[0]
        unique[match_id] = row
        problems = _row_errors(row, report, model)
        if row.get("status") == "scored" and not problems:
            scored[match_id] = row
        else:
            excluded.append({"match_id": match_id, "status": row.get("status", "unknown"),
                             "reason": row.get("reason") or ("; ".join(problems) if problems else "reason_not_recorded"),
                             "data_errors": problems})
            errors.extend({"match_id": match_id, "reason": p} for p in problems)
    coverage = report.get("coverage", {})
    if isinstance(coverage, dict):
        for key, actual in (("total", len(rows)), ("scored", len(scored))):
            if key in coverage and coverage[key] != actual:
                errors.append({"reason": f"reported_{key}_count_mismatch", "reported": coverage[key], "actual": actual})
    return {"report": report, "rows": unique, "scored": scored, "exclusions": excluded,
            "input_count": len(rows), "errors": errors}


def _scores(rows: list[dict[str, Any]], *, baseline: bool = False) -> dict[str, Any] | None:
    losses, briers = [], []
    for row in rows:
        probabilities = _probabilities(row.get("baseline_probabilities") if baseline else row["forecast"]["probabilities"])
        if probabilities is None:
            return None  # Never silently shrink the baseline denominator.
        h, a = row["actual_home_goals"], row["actual_away_goals"]
        outcome = 0 if h > a else (1 if h == a else 2)
        losses.append(-math.log(max(FLOOR, probabilities[outcome])))
        briers.append(sum((p - int(i == outcome)) ** 2 for i, p in enumerate(probabilities)))
    if not rows:
        return None
    return {"n": len(rows), "log_loss": sum(losses) / len(rows), "brier_score": sum(briers) / len(rows)}


def _single_group(rows: list[dict[str, Any]]) -> dict[str, Any]:
    baseline = _scores(rows, baseline=True)
    return {"n": len(rows), "match_ids": [_identifier(row["match_id"]) for row in rows],
            "model_metrics": _scores(rows), "baseline_metrics": baseline,
            "baseline_status": "complete_same_rows" if rows and baseline is not None
            else "unavailable_or_incomplete"}


def _same_time(a: Any, b: Any) -> bool:
    return _timestamp(a) is not None and _timestamp(a) == _timestamp(b)


def _conditions(old: dict[str, Any], new: dict[str, Any], old_source: dict[str, Any],
                new_source: dict[str, Any]) -> list[str]:
    reasons = []
    for field in ("competition", "target_scope"):
        a, b = old_source.get(field), new_source.get(field)
        if not a or not b:
            reasons.append(f"missing_{field}")
        elif a != b:
            reasons.append(f"different_{field}")
    if new_source.get("target_scope") != TARGET_SCOPE or old_source.get("target_scope") != TARGET_SCOPE:
        reasons.append("regulation_target_not_verified")
    for field in ("evaluation_data_cutoff",):
        if not _same_time(old["report"].get(field), new["report"].get(field)):
            reasons.append(f"different_or_unknown_{field}")
    a, b = old["report"].get("split", {}), new["report"].get("split", {})
    for field in ("method", "grouped_by", "result_availability_delay_hours"):
        if field not in a or field not in b or a[field] != b[field]:
            reasons.append(f"different_or_unknown_split_{field}")
    for field in ("train_cutoff", "test_start", "test_end"):
        if not _same_time(a.get(field), b.get(field)):
            reasons.append(f"different_or_unknown_split_{field}")
    for field in ("is_rolling_backtest", "minimum_test_matches"):
        a, b = old["report"].get(field), new["report"].get(field)
        if a is None or b is None or a != b:
            reasons.append(f"different_or_unknown_{field}")
    return reasons


def _pair_reasons(old: dict[str, Any], new: dict[str, Any]) -> list[str]:
    reasons = []
    for field in ("home_id", "away_id"):
        if _identifier(old.get(field)) != _identifier(new.get(field)):
            reasons.append(f"different_{field}")
    if not _same_time(old.get("kickoff"), new.get("kickoff")):
        reasons.append("different_kickoff")
    for field in ("actual_home_goals", "actual_away_goals"):
        if old.get(field) != new.get(field):
            reasons.append(f"different_{field}")
    a, b = old["forecast"], new["forecast"]
    if not _same_time(a.get("training_cutoff"), b.get("training_cutoff")):
        reasons.append("different_forecast_training_cutoff")
    for field in ("model_version", "model_scope"):
        if not a.get(field) or not b.get(field) or a[field] != b[field]:
            reasons.append(f"different_or_unknown_{field}")
    return reasons


def _temporal_summary(match_ids: list[str], audit: dict[str, Any] | None) -> dict[str, Any]:
    fields = ("known_at", "valid_at", "valid_from", "valid_to", "evidence_available_at")
    records: dict[str, list[dict[str, Any]]] = {}
    if isinstance(audit, dict):
        for row in audit.get("by_match", []):
            if isinstance(row, dict) and _identifier(row.get("match_id")) is not None:
                records.setdefault(_identifier(row["match_id"]), []).append(row)
    counts = Counter({"eligible": 0, "ineligible": 0, "unknown": 0})
    unknown = Counter({field: 0 for field in fields})
    reasons: Counter[str] = Counter()
    for match_id in match_ids:
        candidates = records.get(match_id, [])
        row = candidates[0] if len(candidates) == 1 else {}
        state = row.get("eligible")
        counts["eligible" if state is True else ("ineligible" if state is False else "unknown")] += 1
        if not row:
            reasons["missing_or_duplicate_temporal_record"] += 1
        for field in fields:
            if _timestamp(row.get(field)) is None:
                unknown[field] += 1
        for reason in row.get("reasons", []):
            reasons[str(reason)] += 1
    return {"n": len(match_ids), "mode": audit.get("mode", "unknown") if isinstance(audit, dict) else "unknown",
            "provided_eligibility_counts": dict(counts), "missing_or_invalid_time_counts": dict(unknown),
            "reason_counts": dict(reasons), "historical_point_in_time_validated": False,
            "note": "只汇总传入证据。当前回填资格不证明历史训练截点已可知；未知时间不补造，开放有效区间的null也不推定有界。"}


def compare_validation_coverage(
    old_validation: dict[str, Any] | None,
    new_validation: dict[str, Any],
    *,
    old_source: dict[str, Any] | None = None,
    new_source: dict[str, Any] | None = None,
    old_temporal_audit: dict[str, Any] | None = None,
    new_temporal_audit: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Compare per-match held-out 1X2 outputs without changing their samples.

    ``source`` should identify the immutable path/model and explicitly provide
    ``competition`` and ``target_scope=TARGET_SCOPE`` after the caller verifies
    them. Missing old validation is not evidence that all new rows are new
    coverage. Temporal audits are optional, unmodified source evidence; their
    omission remains unknown and never grants prospective qualification.
    """
    new_source, old_source = new_source or {}, old_source or {}
    new = _index(new_validation)
    old = _index(old_validation) if old_validation is not None else None
    reasons = _conditions(old, new, old_source, new_source) if old else ["missing_previous_per_match_validation"]
    for side, validation, source in (("new", new_validation, new_source), ("old", old_validation, old_source)):
        if validation is None:
            continue
        model = validation.get("model", {})
        if not isinstance(model, dict):
            model = {}
        if model.get("training_cutoff") and not _same_time(model["training_cutoff"], validation.get("report", {}).get("split", {}).get("train_cutoff")):
            reasons.append(f"{side}_model_report_training_cutoff_mismatch")
    if old:
        for field in ("model_version", "model_scope", "half_life_days", "ridge"):
            a, b = old_validation.get("model", {}).get(field), new_validation.get("model", {}).get(field)
            if a is None or b is None or a != b:
                reasons.append(f"different_or_unknown_model_{field}")
    if new["errors"] or (old and old["errors"]):
        reasons.append("invalid_or_incomplete_validation_rows")
    common, unpaired = [], []
    if old:
        for match_id in sorted(old["scored"].keys() & new["scored"].keys()):
            problems = _pair_reasons(old["scored"][match_id], new["scored"][match_id])
            if problems:
                unpaired.append({"match_id": match_id, "reasons": problems})
            else:
                common.append(match_id)
    if unpaired:
        reasons.append("unpaired_common_matches")
    new_only_ids = sorted(new["scored"].keys() - old["scored"].keys()) if old else []
    removed_ids = sorted(old["scored"].keys() - new["scored"].keys()) if old else []
    old_common = _single_group([old["scored"][k] for k in common]) if old else _single_group([])
    new_common = _single_group([new["scored"][k] for k in common])
    deltas = None
    if common:
        deltas = {f"new_minus_old_{metric}": new_common["model_metrics"][metric] - old_common["model_metrics"][metric]
                  for metric in ("log_loss", "brier_score")}
    def transition(match_id: str, other: dict[str, Any] | None) -> dict[str, Any]:
        row = other["rows"].get(match_id) if other else None
        return {"match_id": match_id, "other_status": row.get("status") if row else "absent_from_validation",
                "other_reason": row.get("reason", "reason_not_recorded") if row else
                "未出现在另一份验证逐场记录；可能是清洗排除或评价窗口变化，不能猜具体原因。"}
    result = {
        "schema_version": 1,
        "status": ("missing_previous_validation" if old is None else
                   ("noncomparable_diagnostic" if reasons else "matched_conditions_diagnostic")),
        "sources": {"old": old_source or None, "new": new_source or None},
        "target": "regulation_time_1x2",
        "evaluation_context": {
            "old": {"evaluation_data_cutoff": old["report"].get("evaluation_data_cutoff"),
                    "split": old["report"].get("split")} if old else None,
            "new": {"evaluation_data_cutoff": new["report"].get("evaluation_data_cutoff"),
                    "split": new["report"].get("split")}},
        "comparison_eligibility": {"same_evaluation_conditions": not reasons, "reasons": reasons,
                                   "pure_repair_effect_validated": False,
                                   "note": "共同样本配对评分仅为描述性检查；因果增量还需固定修复外的资料、实现和验证条件。"},
        "counts": {"old_total": old["input_count"] if old else None, "new_total": new["input_count"],
                   "old_scored": len(old["scored"]) if old else None, "new_scored": len(new["scored"]),
                   "common_scored": len(common) + len(unpaired), "paired_common_scored": len(common),
                   "new_only_scored": len(new_only_ids) if old else None,
                   "removed_scored": len(removed_ids) if old else None, "unpaired_common_scored": len(unpaired),
                   "old_excluded": len(old["exclusions"]) if old else None, "new_excluded": len(new["exclusions"])},
        "groups": {
            "common": {"n": len(common), "old": old_common, "new": new_common,
                       "paired_diagnostic_delta": deltas,
                       "controlled_comparison_eligible": not reasons and bool(common)},
            "new_only": {**_single_group([new["scored"][k] for k in new_only_ids]),
                         "transitions": [transition(k, old) for k in new_only_ids],
                         "classification": "newly_scored_in_supplied_validation_only" if old else "unknown_without_previous_validation"},
            "removed": {**(_single_group([old["scored"][k] for k in removed_ids]) if old else _single_group([])),
                        "transitions": [transition(k, new) for k in removed_ids]},
            "unpaired_common": {"n": len(unpaired), "matches": unpaired,
                                "old": _single_group([old["scored"][row["match_id"]] for row in unpaired]) if old else _single_group([]),
                                "new": _single_group([new["scored"][row["match_id"]] for row in unpaired]),
                                "controlled_comparison_eligible": False,
                                "note": "同ID但身份、赛果或训练条件不一致；各侧诊断评分不能视为相同条件的配对增量。"},
            "current_all": _single_group(list(new["scored"].values())),
        },
        "exclusions": {"old": old["exclusions"] if old else None, "new": new["exclusions"]},
        "data_errors": {"old": old["errors"] if old else None, "new": new["errors"]},
        "temporal_evidence": {"old": _temporal_summary(list(old["scored"]), old_temporal_audit) if old else None,
                              "new": _temporal_summary(list(new["scored"]), new_temporal_audit),
                              "common_old": _temporal_summary(common, old_temporal_audit),
                              "common_new": _temporal_summary(common, new_temporal_audit),
                              "new_only": _temporal_summary(new_only_ids, new_temporal_audit)},
        "metric_definitions": {"outcome_order": list(OUTCOMES), "lower_is_better": True,
                               "brier": "mean(sum_over_3_outcomes((probability - outcome_indicator)^2))",
                               "log_loss_probability_floor": FLOOR},
        "prospective_increment_validated": False,
        "deep_handicap_validated": False,
        "betting_return_validated": False,
        "deployment_approved": False,
        "limitations": [
            "本报告不改变固定留出、训练参数、比分目标或原始逐场记录。",
            "验证中新增出现的样本可能来自评价窗口或历史回填，不能称为前瞻新增覆盖。",
            "原预处理就排除的比赛若不在验证JSON中，不在本报告分母；须同时保留国家队场地审计分母。",
            "概率评分、覆盖数量及代码测试不等于深盘、实际投注收益或模型质量验收。",
        ],
    }
    return result
