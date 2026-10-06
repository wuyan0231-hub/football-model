"""Auditable event-time replay of three predeclared score-model candidates.

This is an offline research command, never an automatic production selector.
Inputs must contain verified regulation-time final scores from one competition.
The normalized match schema cannot itself authenticate final status or league.
Historical final-score exports cannot establish what was actually observable at
an old date. Every output therefore remains a development replay, even though
training and sequential candidate selection obey an explicit four-hour embargo.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from datetime import timedelta
from pathlib import Path
from typing import Any

import numpy as np

from .model import (
    RESULT_AVAILABILITY_DELAY, ModelDataError, UnsupportedTeamError,
    _distribution_from_rates, _integer, _iso, _timestamp, _validated_matches,
    fit_model, score_distribution,
)

VERSION = "predeclared_three_candidate_walk_forward_v1"
# This order is also the deterministic tie-break: prefer the simpler candidate.
CANDIDATES = ("league_mean", "poisson_mix_50", "team_poisson")
OUTCOMES = ("home_win", "draw", "away_win")
STATES = ("win", "half_win", "push", "half_loss", "loss")
FLOOR = 1e-15


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    allow_nan=False).encode()).hexdigest()


def _serial(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{k: v for k, v in row.items() if k != "_dt"} for row in records]


def mix_matrices(first: Any, second: Any) -> np.ndarray:
    """A fixed 50/50 mixture of probabilities, not a Poisson at averaged rates."""
    arrays = [np.asarray(first, dtype=float), np.asarray(second, dtype=float)]
    for matrix in arrays:
        if matrix.ndim != 2 or min(matrix.shape) < 1 or not np.all(np.isfinite(matrix)):
            raise ModelDataError("混合要求有限的二维比分概率矩阵。")
        if np.any(matrix < 0) or not math.isclose(float(matrix.sum()), 1.0, abs_tol=1e-8):
            raise ModelDataError("混合要求非负且概率和为1的比分矩阵。")
    shape = tuple(max(m.shape[i] for m in arrays) for i in range(2))
    result = np.zeros(shape)
    for matrix in arrays:
        result[:matrix.shape[0], :matrix.shape[1]] += 0.5 * matrix
    return result / result.sum()


def _over_state(total: int, quarter_line: int) -> str:
    """Settle an over bet. Lines are exact integer quarter-goal units."""
    legs = [quarter_line] if quarter_line % 2 == 0 else [quarter_line - 1, quarter_line + 1]
    signs = [int(np.sign(4 * total - line)) for line in legs]
    value = sum(signs) / len(signs)
    return {1: "win", 0.5: "half_win", 0: "push", -0.5: "half_loss", -1: "loss"}[value]


def summarize_distribution(matrix: Any, expected_goals: dict[str, float]) -> dict[str, Any]:
    matrix = np.asarray(matrix, dtype=float)
    h, a = np.indices(matrix.shape)
    markets = {}
    for name, line in (("over_2_25", 9), ("over_2_75", 11)):
        probabilities = {state: 0.0 for state in STATES}
        for total in range(sum(matrix.shape) - 1):
            probabilities[_over_state(total, line)] += float(matrix[(h + a) == total].sum())
        markets[name] = probabilities
    return {
        "expected_goals": expected_goals,
        "probabilities": {
            "home_win": float(matrix[h > a].sum()),
            "draw": float(matrix[h == a].sum()),
            "away_win": float(matrix[h < a].sum()),
        },
        "events": {
            "over_2_5": float(matrix[h + a >= 3].sum()),
            "home_win_by_2_plus": float(matrix[h - a >= 2].sum()),
        },
        "settlement_probabilities": markets,
        "lineup_adjusted": False,
        "model_scope": "historical_scores_only_research",
    }


def _calibration(probabilities: np.ndarray, observed: np.ndarray) -> list[dict[str, Any]]:
    """Ten fixed equal-width bins; empty bins remain explicit, no fitted bins."""
    indices = np.minimum((probabilities * 10).astype(int), 9)
    result = []
    for index in range(10):
        chosen = indices == index
        count = int(chosen.sum())
        result.append({"lower": index / 10, "upper": (index + 1) / 10, "n": count,
                       "mean_predicted": float(probabilities[chosen].mean()) if count else None,
                       "observed_rate": float(observed[chosen].mean()) if count else None})
    return result


def _categorical_metrics(probabilities: list[list[float]], outcomes: list[int],
                         labels: tuple[str, ...]) -> dict[str, Any] | None:
    if not outcomes:
        return None
    probs = np.asarray(probabilities)
    actual = np.eye(len(labels))[outcomes]
    return {
        "n": len(outcomes),
        "log_loss": float(-np.log(np.clip(probs[np.arange(len(outcomes)), outcomes], FLOOR, 1)).mean()),
        "brier_score": float(np.square(probs - actual).sum(axis=1).mean()),
        "brier_definition": "mean(sum_over_classes((p-y)^2))",
        "calibration": {label: _calibration(probs[:, i], actual[:, i]) for i, label in enumerate(labels)},
    }


def _binary_metrics(probabilities: list[float], outcomes: list[bool]) -> dict[str, Any] | None:
    if not outcomes:
        return None
    probs, actual = np.asarray(probabilities), np.asarray(outcomes, dtype=float)
    loss = -(actual * np.log(np.clip(probs, FLOOR, 1)) +
             (1 - actual) * np.log(np.clip(1 - probs, FLOOR, 1)))
    return {"n": len(outcomes), "log_loss": float(loss.mean()),
            "brier_score": float(np.square(probs - actual).mean()),
            "brier_definition": "mean((p-y)^2)",
            "calibration": _calibration(probs, actual)}


def _outcome(row: dict[str, Any]) -> int:
    h, a = row["actual_home_goals"], row["actual_away_goals"]
    return 0 if h > a else (1 if h == a else 2)


def _metrics(rows: list[dict[str, Any]], name: str) -> dict[str, Any]:
    pairs = [(row, row["forecasts"][name]) for row in rows if name in row["forecasts"]]
    result = {
        "n": len(pairs),
        "one_x_two": _categorical_metrics(
            [[forecast["probabilities"][key] for key in OUTCOMES] for _, forecast in pairs],
            [_outcome(row) for row, _ in pairs], OUTCOMES),
    }
    for event in ("over_2_5", "home_win_by_2_plus"):
        actual = [(row["actual_home_goals"] + row["actual_away_goals"] >= 3
                   if event == "over_2_5" else row["actual_home_goals"] - row["actual_away_goals"] >= 2)
                  for row, _ in pairs]
        result[event] = _binary_metrics([forecast["events"][event] for _, forecast in pairs], actual)
    for market, line in (("over_2_25", 9), ("over_2_75", 11)):
        result[market] = _categorical_metrics(
            [[forecast["settlement_probabilities"][market][state] for state in STATES]
             for _, forecast in pairs],
            [STATES.index(_over_state(row["actual_home_goals"] + row["actual_away_goals"], line))
             for row, _ in pairs], STATES)
    return result


def _select(previous: list[dict[str, Any]], origin: Any, minimum: int) -> dict[str, Any]:
    mature = [row for row in previous if row["common_coverage"] and
              _timestamp(row["result_assumed_available_at"], "availability") <= origin]
    scores = {}
    for name in CANDIDATES:
        losses = [-math.log(max(FLOOR, row["forecasts"][name]["probabilities"][OUTCOMES[_outcome(row)]]))
                  for row in mature]
        scores[name] = float(np.mean(losses)) if losses else None
    enough = len(mature) >= minimum
    selected = min(CANDIDATES, key=lambda key: (scores[key], CANDIDATES.index(key))) if enough else "league_mean"
    return {"candidate": selected, "mature_common_matches": len(mature),
            "minimum_selection_matches": minimum, "prior_one_x_two_log_loss": scores,
            "reason": "lowest_prior_common_log_loss" if enough else "cold_start_league_mean",
            "selection_data_latest_available_at": max((r["result_assumed_available_at"] for r in mature), default=None)}


def evaluate_walk_forward(
    matches: list[dict[str, Any]], evaluation_start_iso: str, evaluation_end_iso: str,
    *, step_days: int = 1, half_life_days: float = 180.0, ridge: float = 3.0,
    min_train: int = 180, minimum_selection_matches: int = 40,
) -> dict[str, Any]:
    """Refit at predetermined origins; choose only on matured earlier predictions.

    The three candidates and their mixture weight are fixed in source. All
    candidate comparisons use an identical common sample. A separate policy
    row exposes baseline fallbacks and total coverage instead of hiding them.
    """
    start, end = _timestamp(evaluation_start_iso, "evaluation_start"), _timestamp(evaluation_end_iso, "evaluation_end")
    if start >= end:
        raise ModelDataError("evaluation_start 必须早于 evaluation_end。")
    if start.hour or start.minute or start.second or start.microsecond:
        raise ModelDataError("前推起点必须是UTC午夜，以免人为切开同日比赛。")
    step_days = _integer(step_days, "step_days", 1)
    min_train = _integer(min_train, "min_train", 2)
    minimum_selection_matches = _integer(minimum_selection_matches, "minimum_selection_matches", 1)
    if not math.isfinite(half_life_days) or half_life_days <= 0 or not math.isfinite(ridge) or ridge <= 0:
        raise ModelDataError("half_life_days 和 ridge 必须为正有限数。")
    records = _validated_matches(matches)
    targets = [m for m in records if start <= m["_dt"] < end and m["_dt"] + RESULT_AVAILABILITY_DELAY <= end]
    if not targets:
        raise ModelDataError("指定区间没有已满4小时的可评分比赛。")
    predictions, windows = [], []
    origin = start
    while origin < end:
        stop = min(origin + timedelta(days=step_days), end)
        testing = [m for m in targets if origin <= m["_dt"] < stop]
        if not testing:
            origin = stop
            continue
        training = [m for m in records if m["_dt"] + RESULT_AVAILABILITY_DELAY <= origin]
        serial_training = _serial(training)
        selection = _select(predictions, origin, minimum_selection_matches)
        model, fit_error, base_rates, rejected_model = None, None, None, None
        if len(training) >= min_train:
            base_rates = {"home": (sum(m["home_goals"] for m in training) + 0.5) / (len(training) + 0.5),
                          "away": (sum(m["away_goals"] for m in training) + 0.5) / (len(training) + 0.5)}
            try:
                model = fit_model(serial_training, _iso(origin), half_life_days, ridge, min_train)
                if model.get("optimizer", {}).get("parameter_at_bound"):
                    rejected_model, model = model, None
                    fit_error = "原模型拟合参数触及优化边界；按生产相同规则排除球队模型及混合候选。"
            except ModelDataError as exc:
                fit_error = str(exc)
        else:
            fit_error = f"合格训练场数{len(training)}少于{min_train}。"
        window = {
            "origin": _iso(origin), "end": _iso(stop), "training_count": len(training),
            "training_data_sha256": _digest(serial_training),
            "last_result_assumed_available_at": _iso(training[-1]["_dt"] + RESULT_AVAILABILITY_DELAY) if training else None,
            "test_match_ids": [m["match_id"] for m in testing], "selection": selection,
            "team_model_parameters": model, "team_model_fit_error": fit_error,
            "rejected_team_model_parameters": rejected_model,
            "league_mean_rates": base_rates, "mixture_team_weight": 0.5,
        }
        current = []
        for match in testing:
            row = {"match_id": match["match_id"], "kickoff": match["kickoff"],
                   "home_id": match["home_id"], "away_id": match["away_id"],
                   "home_name": match["home_name"], "away_name": match["away_name"],
                   "neutral": match["neutral"], "forecast_origin": _iso(origin),
                   "actual_home_goals": match["home_goals"], "actual_away_goals": match["away_goals"],
                   "result_assumed_available_at": _iso(match["_dt"] + RESULT_AVAILABILITY_DELAY),
                   "training_data_sha256": window["training_data_sha256"],
                   "forecasts": {}, "unavailable": {}}
            baseline = None
            if base_rates is not None:
                neutral_rate = sum(base_rates.values()) / 2
                rates = {"home": neutral_rate, "away": neutral_rate} if match["neutral"] else base_rates
                baseline = _distribution_from_rates(rates["home"], rates["away"])
                row["forecasts"]["league_mean"] = summarize_distribution(baseline["score_matrix"], rates)
            else:
                row["unavailable"]["league_mean"] = fit_error
            if model is not None:
                try:
                    forecast = score_distribution(model, match["home_id"], match["away_id"], match["kickoff"], match["neutral"])
                    row["forecasts"]["team_poisson"] = summarize_distribution(forecast["score_matrix"], forecast["expected_goals"])
                    mixed = mix_matrices(baseline["score_matrix"], forecast["score_matrix"])
                    mixed_rates = {side: 0.5 * (baseline["expected_goals"][side] + forecast["expected_goals"][side]) for side in ("home", "away")}
                    row["forecasts"]["poisson_mix_50"] = summarize_distribution(mixed, mixed_rates)
                except (UnsupportedTeamError, ModelDataError) as exc:
                    row["unavailable"].update({"team_poisson": str(exc), "poisson_mix_50": str(exc)})
            else:
                row["unavailable"].update({"team_poisson": fit_error, "poisson_mix_50": fit_error})
            row["common_coverage"] = all(name in row["forecasts"] for name in CANDIDATES)
            chosen = selection["candidate"]
            row["selected_candidate_at_origin"] = chosen
            row["policy_fallback"] = chosen not in row["forecasts"] and "league_mean" in row["forecasts"]
            effective = chosen if chosen in row["forecasts"] else ("league_mean" if "league_mean" in row["forecasts"] else None)
            row["effective_policy_candidate"] = effective
            if effective is not None:
                row["forecasts"]["sequential_policy"] = row["forecasts"][effective]
            current.append(row)
        window_common = [r for r in current if r["common_coverage"]]
        window["common_scored_count"] = len(window_common)
        # Per-window losses expose heterogeneity without presenting a fragile
        # small-sample or correlated-game confidence interval as significance.
        window["one_x_two_common_log_loss"] = {
            name: (float(np.mean([-math.log(max(FLOOR, r["forecasts"][name]["probabilities"][OUTCOMES[_outcome(r)]]))
                                  for r in window_common])) if window_common else None)
            for name in (*CANDIDATES, "sequential_policy")}
        windows.append(window)
        predictions.extend(current)
        origin = stop
    common = [r for r in predictions if r["common_coverage"]]
    all_names = (*CANDIDATES, "sequential_policy")
    common_metrics = {name: _metrics(common, name) for name in all_names}
    comparisons = {}
    for name in all_names[1:]:
        comparisons[name] = {}
        for event in ("one_x_two", "over_2_5", "home_win_by_2_plus", "over_2_25", "over_2_75"):
            model_metric, baseline_metric = common_metrics[name][event], common_metrics["league_mean"][event]
            comparisons[name][event] = ({key + "_minus_league_mean": model_metric[key] - baseline_metric[key]
                                        for key in ("log_loss", "brier_score")} if model_metric else None)
    report = {
        "version": VERSION, "validation_status": "historical_development_replay",
        "deployment_approved": False, "prospective_validation": False,
        "is_event_time_walk_forward": True, "point_in_time_sources_verified": False,
        "evaluation_start": _iso(start), "evaluation_end": _iso(end),
        "input_data_sha256": _digest(_serial(records)),
        "protocol": {"step_days": step_days, "result_availability_delay_hours": 4,
                     "half_life_days": half_life_days, "ridge": ridge, "min_train": min_train,
                     "minimum_selection_matches": minimum_selection_matches,
                     "candidates": list(CANDIDATES), "mixture_team_weight": 0.5,
                     "selection_metric": "prior_mature_common_1x2_log_loss",
                     "selection_tie_order": list(CANDIDATES), "cold_start_candidate": "league_mean",
                     "unavailable_selection_fallback": "league_mean_if_available",
                     "log_loss_probability_floor": FLOOR,
                     "baseline_smoothing": "(sum_goals+0.5)/(n_matches+0.5)",
                     "calibration_bins": "10_fixed_equal_width_bins"},
        "coverage": {"total": len(predictions), "common": len(common),
                     "common_ratio": len(common) / len(predictions),
                     "by_candidate": {name: sum(name in r["forecasts"] for r in predictions) for name in all_names},
                     "policy_fallback_count": sum(r["policy_fallback"] for r in predictions),
                     "not_common_match_ids": [r["match_id"] for r in predictions if not r["common_coverage"]]},
        "common_sample_metrics": common_metrics,
        "candidate_own_coverage_metrics": {name: _metrics(predictions, name) for name in all_names},
        "paired_common_comparison": comparisons, "lower_metric_is_better": True,
        "warnings": [
            "历史开发重演；既知留出及事后取得的数据不得称为未见未来独立验收。",
            "4小时仅为比分可用性假设；不替代真实终场、当时供应商快照、场地事实和消息发布时间证据。",
            "每窗口重新拟合的研究策略与线上沿用参数策略可不同；本命令不改变生产候选、配置或参数。",
            "候选及0.5权重预先固定；顺序选择只用此前已满4小时且全部候选共同覆盖的预测。",
            "只有共同样本指标可直接比较；各自覆盖指标用于披露覆盖，不能比较后冒充增量。",
            "净胜2+仅为主让1.5的覆盖事件；五态校准不是赔率EV、收益或完整亚洲盘盈利验证。",
            "未量化首发、伤停、球员、xG；没有赔率、EV或盈利声明。",
        ],
    }
    return {"report": report, "windows": windows, "predictions": predictions}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--matches", type=Path, required=True, help="同一赛事已核准常规时间终场比分列表，或包含matches列表的JSON对象")
    parser.add_argument("--start", required=True, help="显式UTC午夜前推起点，带时区")
    parser.add_argument("--end", required=True, help="显式评价数据截止时间，带时区")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--step-days", type=int, default=1)
    parser.add_argument("--half-life-days", type=float, default=180.0)
    parser.add_argument("--ridge", type=float, default=3.0)
    parser.add_argument("--min-train", type=int, default=180)
    parser.add_argument("--minimum-selection-matches", type=int, default=40)
    args = parser.parse_args()
    source_bytes = args.matches.read_bytes()
    source = json.loads(source_bytes)
    matches = source.get("matches") if isinstance(source, dict) else source
    result = evaluate_walk_forward(matches, args.start, args.end, step_days=args.step_days,
                                   half_life_days=args.half_life_days, ridge=args.ridge,
                                   min_train=args.min_train, minimum_selection_matches=args.minimum_selection_matches)
    result["report"]["input_file_sha256"] = hashlib.sha256(source_bytes).hexdigest()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(args.output), "status": result["report"]["validation_status"],
                      "coverage": result["report"]["coverage"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
