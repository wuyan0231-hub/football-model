"""Offline nested temporal comparison of three fixed half-life candidates.

The original fitter, score distribution and quality scoring are reused. This
module is never called by production and never approves a model for deployment.
Historical exports cannot authenticate what a supplier exposed at an old date.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np
from scipy.special import gammaln
from scipy.stats import skellam

from .model import (RESULT_AVAILABILITY_DELAY, ModelDataError,
                    _distribution_from_rates, _integer, _iso, _timestamp,
                    _validated_matches, fit_model, score_distribution)
from .quality import (FLOOR, OUTCOMES, _digest, _metrics, _outcome, _serial,
                      summarize_distribution)

VERSION = "s2_half_life_nested_temporal_v1"
HALF_LIVES = {"half_life_180": 180.0, "half_life_90": 90.0, "half_life_365": 365.0}
BASELINE = "half_life_180"
NAMES = (*HALF_LIVES, "league_mean", "selected_policy")
LOSS_NAMES = ("one_x_two_log_loss", "one_x_two_brier", "score_log_loss",
              "total_log_loss", "margin_log_loss", "total_absolute_error",
              "margin_absolute_error")


def make_protocol(start: str, end: str, *, ridge: float = 3.0,
                  min_train: int = 180, step_days: int = 7,
                  inner_days: int = 180, minimum_inner_matches: int = 40,
                  bootstrap_replicates: int = 2000) -> dict[str, Any]:
    begin, finish = _timestamp(start, "start"), _timestamp(end, "end")
    if begin >= finish or any((begin.hour, begin.minute, begin.second, begin.microsecond)):
        raise ModelDataError("研究起点必须为UTC午夜且早于终点。")
    if isinstance(ridge, bool) or not math.isfinite(ridge) or ridge <= 0:
        raise ModelDataError("ridge必须为正有限数。")
    return {"version": VERSION, "start": _iso(begin), "end": _iso(finish),
            "ridge": float(ridge), "min_train": _integer(min_train, "min_train", 2),
            "step_days": _integer(step_days, "step_days", 1),
            "inner_days": _integer(inner_days, "inner_days", 1),
            "minimum_inner_matches": _integer(minimum_inner_matches, "minimum_inner_matches", 1),
            "bootstrap_replicates": _integer(bootstrap_replicates, "bootstrap_replicates", 100),
            "half_lives": HALF_LIVES.copy(), "tie_order": list(HALF_LIVES),
            "primary_comparison": "selected_policy_minus_half_life_180",
            "primary_metric": "one_x_two_log_loss", "bootstrap_seed": 20261007,
            "bootstrap_minimum_blocks": 8, "confidence_level": 0.95,
            "interval_block_days": 28,
            "result_availability_delay_hours": 4,
            "selection_metric": "prior_mature_common_one_x_two_log_loss",
            "cold_start_and_unavailable_fallback": BASELINE,
            "no_available_baseline_action": "abstain_and_report",
            "promotion_rule": "none_historical_development_only"}


def _check_protocol(protocol: dict[str, Any]) -> dict[str, Any]:
    fields = ("ridge", "min_train", "step_days", "inner_days",
              "minimum_inner_matches", "bootstrap_replicates")
    expected = make_protocol(protocol["start"], protocol["end"],
                             **{key: protocol[key] for key in fields})
    if protocol != expected:
        raise ModelDataError("协议字段不匹配；不能在执行时替换候选、选择规则或指标。")
    return expected


def _one_loss(row: dict[str, Any], name: str) -> float:
    return -math.log(max(FLOOR, row["forecasts"][name]["probabilities"][OUTCOMES[_outcome(row)]]))


def _choose(previous: list[dict[str, Any]], origin: Any, protocol: dict[str, Any]) -> dict[str, Any]:
    floor = origin - timedelta(days=protocol["inner_days"])
    eligible = [r for r in previous if floor <= _timestamp(r["kickoff"], "kickoff") < origin
                and _timestamp(r["result_assumed_available_at"], "availability") <= origin
                and all(name in r["forecasts"] for name in HALF_LIVES)]
    losses = {name: float(np.mean([_one_loss(r, name) for r in eligible])) if eligible else None
              for name in HALF_LIVES}
    enough = len(eligible) >= protocol["minimum_inner_matches"]
    chosen = min(HALF_LIVES, key=lambda n: (losses[n], list(HALF_LIVES).index(n))) if enough else BASELINE
    return {"candidate": chosen, "n_inner_common": len(eligible),
            "inner_match_ids": [r["match_id"] for r in eligible],
            "inner_start": _iso(floor), "inner_end_exclusive": _iso(origin),
            "latest_inner_result_available_at": max((r["result_assumed_available_at"] for r in eligible), default=None),
            "inner_one_x_two_log_loss": losses,
            "reason": "lowest_prior_common_loss" if enough else "insufficient_inner_use_baseline"}


def _forecast(distribution: dict[str, Any]) -> dict[str, Any]:
    summary = summarize_distribution(distribution["score_matrix"], distribution["expected_goals"])
    summary.update({"matrix_shape": [len(distribution["score_matrix"]), len(distribution["score_matrix"][0])],
                    "tail_mass_before_normalization": distribution["tail_mass_before_normalization"]})
    return summary


def _losses(row: dict[str, Any], name: str) -> list[float]:
    forecast = row["forecasts"][name]
    h, a = row["actual_home_goals"], row["actual_away_goals"]
    rh, ra = forecast["expected_goals"]["home"], forecast["expected_goals"]["away"]
    p = np.asarray([forecast["probabilities"][k] for k in OUTCOMES])
    actual = np.eye(3)[_outcome(row)]
    # Exact untruncated independent-Poisson score/total/margin diagnostics use
    # the original model's rates; primary 1X2 uses its actual normalized matrix.
    score_loss = rh + ra - h * math.log(rh) - a * math.log(ra) + gammaln(h + 1) + gammaln(a + 1)
    total_loss = rh + ra - (h + a) * math.log(rh + ra) + gammaln(h + a + 1)
    margin_log_probability = float(skellam.logpmf(h - a, rh, ra))
    margin_loss = -max(math.log(FLOOR), margin_log_probability)
    return [_one_loss(row, name), float(np.square(p - actual).sum()),
            float(score_loss), float(total_loss), margin_loss,
            abs(rh + ra - h - a), abs(rh - ra - h + a)]


def _summary(rows: list[dict[str, Any]], name: str) -> dict[str, Any]:
    result = _metrics(rows, name)
    covered = [r for r in rows if name in r["forecasts"]]
    values = np.asarray([_losses(r, name) for r in covered])
    result["distribution_diagnostics"] = ({key: float(values[:, i].mean())
                                             for i, key in enumerate(LOSS_NAMES)} if covered else None)
    if covered:
        result["one_x_two"]["argmax_accuracy"] = float(np.mean([
            int(np.argmax([r["forecasts"][name]["probabilities"][k] for k in OUTCOMES])) == _outcome(r)
            for r in covered]))
    return result


def _paired(rows: list[dict[str, Any]], candidate: str, protocol: dict[str, Any]) -> dict[str, Any]:
    common = [r for r in rows if candidate in r["forecasts"] and BASELINE in r["forecasts"]]
    gained = [r["match_id"] for r in rows if candidate in r["forecasts"] and BASELINE not in r["forecasts"]]
    lost = [r["match_id"] for r in rows if candidate not in r["forecasts"] and BASELINE in r["forecasts"]]
    neither = [r["match_id"] for r in rows if candidate not in r["forecasts"] and BASELINE not in r["forecasts"]]
    grouped: dict[str, list[list[float]]] = {}
    for row in common:
        grouped.setdefault(row["evaluation_block_start"], []).append(
            (np.asarray(_losses(row, candidate)) - _losses(row, BASELINE)).tolist())
    groups = list(grouped.values())
    intervals = None
    if len(groups) >= protocol["bootstrap_minimum_blocks"]:
        rng = np.random.default_rng(protocol["bootstrap_seed"])
        sums = np.asarray([np.asarray(g).sum(axis=0) for g in groups])
        counts = np.asarray([len(g) for g in groups])
        selected = rng.integers(0, len(groups), size=(protocol["bootstrap_replicates"], len(groups)))
        means = sums[selected].sum(axis=1) / counts[selected].sum(axis=1)[:, None]
        intervals = np.quantile(means, [0.025, 0.975], axis=0)
    means = np.mean([x for group in groups for x in group], axis=0) if groups else None
    return {"baseline": BASELINE, "candidate": candidate, "common_n": len(common),
            "common_match_ids": [r["match_id"] for r in common], "candidate_only_ids": gained,
            "baseline_only_ids": lost, "neither_ids": neither, "time_block_count": len(groups),
            "interval_status": "descriptive_block_bootstrap" if intervals is not None else "insufficient_time_blocks",
            "metrics": {key: {"candidate_minus_baseline": float(means[i]) if means is not None else None,
                              "interval_95": intervals[:, i].tolist() if intervals is not None else None}
                        for i, key in enumerate(LOSS_NAMES)}}


def evaluate_half_life(matches: list[dict[str, Any]], protocol: dict[str, Any]) -> dict[str, Any]:
    protocol = _check_protocol(protocol)
    records = _validated_matches(matches)
    start, end = _timestamp(protocol["start"], "start"), _timestamp(protocol["end"], "end")
    inner_start = start - timedelta(days=protocol["inner_days"])
    targets = [r for r in records if inner_start <= r["_dt"] < end
               and r["_dt"] + RESULT_AVAILABILITY_DELAY <= end]
    if not any(r["_dt"] >= start for r in targets):
        raise ModelDataError("外层评价区间没有已满4小时的可评分比赛。")
    predictions, windows = [], []
    origin = inner_start
    while origin < end:
        stop = min(origin + timedelta(days=protocol["step_days"]), start if origin < start else end)
        testing = [r for r in targets if origin <= r["_dt"] < stop]
        if not testing:
            origin = stop
            continue
        training = [r for r in records if r["_dt"] + RESULT_AVAILABILITY_DELAY <= origin]
        serial = _serial(training)
        selection = _choose(predictions, origin, protocol)
        models, errors, rejected = {}, {}, {}
        rates = None
        if len(training) >= protocol["min_train"]:
            rates = {side: (sum(r[f"{side}_goals"] for r in training) + 0.5) / (len(training) + 0.5)
                     for side in ("home", "away")}
            for name, days in HALF_LIVES.items():
                try:
                    model = fit_model(serial, _iso(origin), days, protocol["ridge"], protocol["min_train"])
                    if model.get("optimizer", {}).get("parameter_at_bound"):
                        rejected[name] = model
                        errors[name] = "parameter_at_bound"
                    else:
                        models[name] = model
                except ModelDataError as exc:
                    errors[name] = str(exc)
        else:
            errors = {name: f"insufficient_training:{len(training)}<{protocol['min_train']}" for name in HALF_LIVES}
        window = {"origin": _iso(origin), "end": _iso(stop), "phase": "outer" if origin >= start else "inner_warmup",
                  "training_count": len(training), "training_data_sha256": _digest(serial),
                  "last_result_assumed_available_at": _iso(training[-1]["_dt"] + RESULT_AVAILABILITY_DELAY) if training else None,
                  "selection": selection, "model_parameters": models, "fit_errors": errors,
                  "rejected_model_parameters": rejected, "league_mean_rates": rates,
                  "test_match_ids": [r["match_id"] for r in testing]}
        for match in testing:
            row = {"match_id": match["match_id"], "kickoff": match["kickoff"],
                   "home_id": match["home_id"], "away_id": match["away_id"],
                   "home_name": match["home_name"], "away_name": match["away_name"], "neutral": match["neutral"],
                   "forecast_origin": _iso(origin), "phase": window["phase"],
                   "evaluation_block_start": _iso(start + timedelta(days=((match["_dt"] - start).days // protocol["interval_block_days"]) * protocol["interval_block_days"])),
                   "training_data_sha256": window["training_data_sha256"],
                   "actual_home_goals": match["home_goals"], "actual_away_goals": match["away_goals"],
                   "result_assumed_available_at": _iso(match["_dt"] + RESULT_AVAILABILITY_DELAY),
                   "forecasts": {}, "unavailable": errors.copy()}
            for name, model in models.items():
                try:
                    row["forecasts"][name] = _forecast(score_distribution(
                        model, match["home_id"], match["away_id"], match["kickoff"], match["neutral"]))
                except ModelDataError as exc:
                    row["unavailable"][name] = str(exc)
            if rates is not None:
                h, a = rates["home"], rates["away"]
                if match["neutral"]:
                    h = a = (h + a) / 2
                row["forecasts"]["league_mean"] = _forecast(_distribution_from_rates(h, a))
            else:
                row["unavailable"]["league_mean"] = "insufficient_training"
            chosen = selection["candidate"]
            effective = chosen if chosen in row["forecasts"] else (BASELINE if BASELINE in row["forecasts"] else None)
            row.update({"selected_candidate_at_origin": chosen, "effective_policy_candidate": effective,
                        "policy_fallback": effective is not None and effective != chosen,
                        "all_half_lives_common": all(n in row["forecasts"] for n in HALF_LIVES)})
            if effective is not None:
                row["forecasts"]["selected_policy"] = row["forecasts"][effective]
            else:
                row["unavailable"]["selected_policy"] = "selected_and_baseline_unavailable"
            predictions.append(row)
        windows.append(window)
        origin = stop
    outer = [r for r in predictions if r["phase"] == "outer"]
    common = [r for r in outer if all(n in r["forecasts"] for n in NAMES)]
    metrics = {n: _summary(common, n) for n in NAMES}
    return {"report": {"version": VERSION, "validation_status": "historical_development_replay",
                       "deployment_approved": False, "prospective_validation": False,
                       "point_in_time_sources_verified": False, "protocol": protocol,
                       "input_data_sha256": _digest(_serial(records)),
                       "coverage": {"outer_total": len(outer), "all_candidates_common": len(common),
                                    "by_candidate": {n: sum(n in r["forecasts"] for r in outer) for n in NAMES},
                                    "inner_warmup_count": len(predictions) - len(outer),
                                    "pending_at_end_ids": [r["match_id"] for r in records if start <= r["_dt"] < end < r["_dt"] + RESULT_AVAILABILITY_DELAY]},
                       "common_sample_metrics": metrics,
                       "own_coverage_metrics": {n: _summary(outer, n) for n in NAMES},
                       "paired_comparisons": {n: _paired(outer, n, protocol) for n in NAMES if n != BASELINE},
                       "baseline_diagnostics": {
                           "by_forecast_origin": {w["origin"]: _summary([r for r in outer if r["forecast_origin"] == w["origin"]], BASELINE)
                                                  for w in windows if w["phase"] == "outer"},
                           "by_observed_total": {label: _summary([r for r in outer if low <= r["actual_home_goals"] + r["actual_away_goals"] <= high], BASELINE)
                                                 for label, low, high in (("0_1", 0, 1), ("2_3", 2, 3), ("4_plus", 4, 1000))}},
                       "warnings": ["当前修订后的历史比分，只能做事件时间开发重演；不能证明当时真实可得。",
                                    "每外层原点只用此前成熟的内层验证；之前外层比赛可在之后原点作为内层数据。",
                                    "7天默认重拟合间隔是研究协议，并非每日生产执行的逐字回放；区间按28天块聚类。",
                                    "置信区间仅为时间块近似描述；块间依赖、跨联赛多重比较和开发选择影响仍存在，不授权上线。",
                                    "比分/总球/净胜球概率诊断使用相同λ的未截断分布；1X2采用生产矩阵。",
                                    "按真实总球分组仅为赛后误差诊断，不能当作赛前筛选规则。",
                                    "无历史报价、费用和成交证据，不计算EV或收益。"]},
            "windows": windows, "predictions": predictions}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--matches", type=Path, required=True)
    parser.add_argument("--freeze-protocol", type=Path)
    parser.add_argument("--protocol", type=Path)
    parser.add_argument("--start")
    parser.add_argument("--end")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--ridge", type=float, default=3.0)
    parser.add_argument("--min-train", type=int, default=180)
    args = parser.parse_args()
    source_bytes = args.matches.read_bytes()
    digest = hashlib.sha256(source_bytes).hexdigest()
    if args.freeze_protocol:
        if args.protocol or args.output or not args.start or not args.end:
            parser.error("冻结需要--start/--end，不能同时执行或输出评分。")
        bundle = {"created_at": _iso(datetime.now(timezone.utc)), "input_file_sha256": digest,
                  "protocol": make_protocol(args.start, args.end, ridge=args.ridge, min_train=args.min_train)}
        args.freeze_protocol.parent.mkdir(parents=True, exist_ok=True)
        with args.freeze_protocol.open("x", encoding="utf-8") as handle:
            json.dump(bundle, handle, ensure_ascii=False, indent=2, allow_nan=False)
        print(json.dumps({"frozen": str(args.freeze_protocol), "input_sha256": digest}))
        return 0
    if not args.protocol or not args.output or args.start or args.end:
        parser.error("执行需要--protocol和--output，不接收另给起止时间。")
    if args.output.exists():
        parser.error("输出已存在；不得覆盖原研究结果。")
    protocol_bytes = args.protocol.read_bytes()
    bundle = json.loads(protocol_bytes)
    if digest != bundle["input_file_sha256"]:
        raise ModelDataError("数据指纹与预冻结协议不一致。")
    began = _iso(datetime.now(timezone.utc))
    if _timestamp(bundle["created_at"], "created_at") > _timestamp(began, "run_started_at"):
        raise ModelDataError("协议冻结时间不能晚于运行时间。")
    source = json.loads(source_bytes)
    result = evaluate_half_life(source.get("matches") if isinstance(source, dict) else source, bundle["protocol"])
    result["report"].update({"input_file_sha256": digest, "protocol_file_sha256": hashlib.sha256(protocol_bytes).hexdigest(),
                            "protocol_created_at": bundle["created_at"], "run_started_at": began,
                            "run_completed_at": _iso(datetime.now(timezone.utc))})
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2, allow_nan=False)
    print(json.dumps({"output": str(args.output), "coverage": result["report"]["coverage"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
