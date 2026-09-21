"""Time-decayed, regularized independent Poisson football *baseline*.

This is not Dixon-Coles, a state-space model, a lineup model, or an EV engine.
Only verified regulation-time final scores belong in ``matches``.  A conservative
four-hour delay after kickoff is also required before a result enters training;
that delay cannot substitute for the caller checking actual final status.
"""

from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timedelta, timezone
from typing import Any

import numpy as np
from scipy.optimize import minimize
from scipy.special import gammaln
from scipy.stats import poisson


MODEL_VERSION = "independent_poisson_time_decay_l2_v1"
MODEL_SCOPE = "baseline_unadjusted_lineup"
RESULT_AVAILABILITY_DELAY = timedelta(hours=4)
TAIL_TOLERANCE = 1e-8


class ModelDataError(ValueError):
    """Input data or training eligibility does not satisfy the model contract."""


class UnsupportedTeamError(ModelDataError):
    """One or both requested teams were absent from the training sample."""


def _timestamp(value: Any, field: str) -> datetime:
    if not isinstance(value, str):
        raise ModelDataError(f"{field} 必须是包含时区的 ISO 时间字符串。")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ModelDataError(f"{field} 不是有效的 ISO 时间：{value!r}") from exc
    if result.tzinfo is None or result.utcoffset() is None:
        raise ModelDataError(f"{field} 缺少时区；请使用 UTC 的 +00:00 或 Z。")
    return result.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _integer(value: Any, field: str, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, np.integer)):
        raise ModelDataError(f"{field} 必须是整数，不能是空值、小数或文本。")
    if value < minimum:
        raise ModelDataError(f"{field} 不能小于 {minimum}。")
    return int(value)


def _validated_matches(matches: Any) -> list[dict[str, Any]]:
    if not isinstance(matches, (list, tuple)):
        raise ModelDataError("matches 必须是比赛记录列表。")
    result = []
    seen = set()
    for index, original in enumerate(matches):
        if not isinstance(original, dict):
            raise ModelDataError(f"第 {index + 1} 条比赛记录必须是字典。")
        required = (
            "match_id", "kickoff", "home_id", "away_id", "home_name",
            "away_name", "home_goals", "away_goals",
        )
        absent = [key for key in required if key not in original]
        if absent:
            raise ModelDataError(f"第 {index + 1} 条比赛缺少字段：{', '.join(absent)}")
        match_id = _integer(original["match_id"], "match_id", 1)
        if match_id in seen:
            raise ModelDataError(f"比赛 ID {match_id} 重复，必须先核对并去重。")
        seen.add(match_id)
        home_id = _integer(original["home_id"], "home_id", 1)
        away_id = _integer(original["away_id"], "away_id", 1)
        if home_id == away_id:
            raise ModelDataError(f"比赛 {match_id} 的主客队不能是同一支球队。")
        for name in ("home_name", "away_name"):
            if not isinstance(original[name], str) or not original[name].strip():
                raise ModelDataError(f"比赛 {match_id} 的 {name} 必须为非空文本。")
        neutral = original.get("neutral", False)
        if not isinstance(neutral, bool):
            raise ModelDataError(f"比赛 {match_id} 的 neutral 必须是布尔值。")
        dt = _timestamp(original["kickoff"], f"比赛 {match_id} kickoff")
        result.append({
            "match_id": match_id, "kickoff": _iso(dt), "_dt": dt,
            "home_id": home_id, "away_id": away_id,
            "home_name": original["home_name"].strip(),
            "away_name": original["away_name"].strip(),
            "home_goals": _integer(original["home_goals"], "home_goals", 0),
            "away_goals": _integer(original["away_goals"], "away_goals", 0),
            "neutral": neutral,
        })
    return sorted(result, key=lambda item: (item["_dt"], item["match_id"]))


def _connected_components(matches: list[dict[str, Any]]) -> list[list[int]]:
    graph: dict[int, set[int]] = {}
    for match in matches:
        h, a = match["home_id"], match["away_id"]
        graph.setdefault(h, set()).add(a)
        graph.setdefault(a, set()).add(h)
    components = []
    unseen = set(graph)
    while unseen:
        stack = [min(unseen)]
        component = set()
        while stack:
            team = stack.pop()
            if team in component:
                continue
            component.add(team)
            stack.extend(graph[team] - component)
        unseen -= component
        components.append(sorted(component))
    return components


def fit_model(
    matches: list[dict[str, Any]],
    cutoff_iso: str,
    half_life_days: float = 180.0,
    ridge: float = 3.0,
    min_train: int = 180,
) -> dict[str, Any]:
    """Fit a JSON-serializable baseline, using only results available by cutoff.

    log(home rate) = intercept + home advantage + home attack - away defence
    log(away rate) = intercept + away attack - home defence

    ``neutral=True`` removes home advantage. Attack and defence each sum to
    zero. Weights have the specified half-life relative to training cutoff.
    Regularization is 0.5 * ridge * squared team coefficients, plus a small
    penalty on home advantage. Hyperparameters here are explicit defaults,
    not values claimed to have been tuned or validated.
    """
    cutoff = _timestamp(cutoff_iso, "cutoff_iso")
    if not math.isfinite(half_life_days) or half_life_days <= 0:
        raise ModelDataError("half_life_days 必须是大于零的有限数。")
    if not math.isfinite(ridge) or ridge <= 0:
        raise ModelDataError("ridge 必须是大于零的有限数，以约束小样本估计。")
    min_train = _integer(min_train, "min_train", 2)
    records = _validated_matches(matches)
    eligible = [m for m in records if m["_dt"] + RESULT_AVAILABILITY_DELAY <= cutoff]
    if len(eligible) < min_train:
        raise ModelDataError(
            f"截止 {_iso(cutoff)} 只有 {len(eligible)} 场合格已赛数据，"
            f"训练至少需要 {min_train} 场；开球后须满 4 小时且赛果已核准。"
        )
    components = _connected_components(eligible)
    if len(components) != 1:
        raise ModelDataError(
            f"训练对阵图存在 {len(components)} 个不连通分组，"
            "不能据此比较跨组球队；请分别训练或补充真实跨组比赛数据。"
        )
    team_ids = components[0]
    n_teams = len(team_ids)
    indices = {team: i for i, team in enumerate(team_ids)}
    home = np.array([indices[m["home_id"]] for m in eligible], dtype=int)
    away = np.array([indices[m["away_id"]] for m in eligible], dtype=int)
    goals_h = np.array([m["home_goals"] for m in eligible], dtype=float)
    goals_a = np.array([m["away_goals"] for m in eligible], dtype=float)
    home_flag = np.array([not m["neutral"] for m in eligible], dtype=float)
    ages = np.array([(cutoff - m["_dt"]).total_seconds() / 86400 for m in eligible])
    weights = np.exp(-math.log(2) * ages / float(half_life_days))
    if float(weights.sum()) < 1e-8:
        raise ModelDataError("训练数据距离截止时间过远，时间权重已接近零。")
    constants = gammaln(goals_h + 1) + gammaln(goals_a + 1)

    def objective(theta: np.ndarray) -> tuple[float, np.ndarray]:
        raw_a = theta[2:2 + n_teams]
        raw_d = theta[2 + n_teams:]
        attack = raw_a - raw_a.mean()
        defence = raw_d - raw_d.mean()
        log_h = theta[0] + theta[1] * home_flag + attack[home] - defence[away]
        log_a = theta[0] + attack[away] - defence[home]
        rate_h, rate_a = np.exp(log_h), np.exp(log_a)
        loss = np.sum(weights * (rate_h + rate_a - goals_h * log_h - goals_a * log_a + constants))
        loss += 0.5 * ridge * (np.dot(raw_a, raw_a) + np.dot(raw_d, raw_d))
        loss += 0.05 * ridge * theta[1] ** 2
        error_h = weights * (rate_h - goals_h)
        error_a = weights * (rate_a - goals_a)
        grad_a = np.bincount(home, error_h, minlength=n_teams) + np.bincount(away, error_a, minlength=n_teams)
        grad_d = -np.bincount(away, error_h, minlength=n_teams) - np.bincount(home, error_a, minlength=n_teams)
        gradient = np.concatenate((
            [np.sum(error_h + error_a), np.sum(error_h * home_flag) + 0.1 * ridge * theta[1]],
            grad_a - grad_a.mean() + ridge * raw_a,
            grad_d - grad_d.mean() + ridge * raw_d,
        ))
        return float(loss), gradient

    mean_h = (np.dot(weights, goals_h) + 0.5) / (weights.sum() + 0.5)
    mean_a = (np.dot(weights, goals_a) + 0.5) / (weights.sum() + 0.5)
    initial = np.zeros(2 + n_teams * 2)
    initial[0] = np.clip(np.log(mean_a), -4, 3)
    initial[1] = np.clip(np.log(mean_h / mean_a), -3, 3)
    result = minimize(
        objective, initial, method="L-BFGS-B", jac=True,
        bounds=[(-4, 3), (-3, 3)] + [(-3, 3)] * (2 * n_teams),
        options={"maxiter": 1500, "ftol": 1e-11, "gtol": 1e-6},
    )
    if not result.success or not np.all(np.isfinite(result.x)):
        raise ModelDataError(f"模型优化未收敛，不能发布参数：{result.message}")
    attack = result.x[2:2 + n_teams]
    attack = attack - attack.mean()
    defence = result.x[2 + n_teams:]
    defence = defence - defence.mean()
    names = {}
    team_counts = {team: 0 for team in team_ids}
    for match in eligible:
        names[match["home_id"]] = match["home_name"]
        names[match["away_id"]] = match["away_name"]
        team_counts[match["home_id"]] += 1
        team_counts[match["away_id"]] += 1
    low_sample_teams = [team for team in team_ids if team_counts[team] < 5]
    serial_records = [{k: v for k, v in item.items() if k != "_dt"} for item in eligible]
    digest = hashlib.sha256(json.dumps(serial_records, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    bound_values = np.array([[-4, 3], [-3, 3]] + [[-3, 3]] * (2 * n_teams))
    on_bound = bool(np.any(np.isclose(result.x, bound_values[:, 0], atol=1e-5)) or
                    np.any(np.isclose(result.x, bound_values[:, 1], atol=1e-5)))
    return {
        "model_version": MODEL_VERSION,
        "model_scope": MODEL_SCOPE,
        "fit_status": "converged",
        "trained": True,
        "validated_for_production": False,
        "training_cutoff": _iso(cutoff),
        "last_match_kickoff": eligible[-1]["kickoff"],
        "last_result_assumed_available_at": _iso(eligible[-1]["_dt"] + RESULT_AVAILABILITY_DELAY),
        "n_matches": len(eligible), "n_teams": n_teams,
        "min_team_matches": min(team_counts.values()),
        "low_sample_team_threshold": 5,
        "low_sample_team_ids": low_sample_teams,
        "excluded_after_cutoff_or_availability_delay": len(records) - len(eligible),
        "training_data_sha256": digest,
        "half_life_days": float(half_life_days), "ridge": float(ridge),
        "effective_sample_size": float(weights.sum() ** 2 / np.dot(weights, weights)),
        "intercept": float(result.x[0]), "home_advantage": float(result.x[1]),
        "team_parameters": {
            str(team): {"name": names[team], "attack": float(attack[i]), "defence": float(defence[i]),
                        "training_matches": team_counts[team]}
            for i, team in enumerate(team_ids)
        },
        "graph_connected": True,
        "constraints": {"attack_sum": float(attack.sum()), "defence_sum": float(defence.sum())},
        "optimizer": {"success": True, "message": str(result.message), "iterations": int(result.nit),
                      "objective": float(result.fun), "parameter_at_bound": on_bound},
        "warnings": (["有参数触及优化边界，需人工检查，不能直接投入使用。"] if on_bound else []) +
        ([f"球队 {low_sample_teams} 各自训练不足 5 场，估计强烈依赖正则化，须保留小样本标记。"] if low_sample_teams else []) + [
            "仅为球队比分基线，未量化伤停、预计阵容、球员、xG、天气及赛程因素。",
            "已拟合不代表已通过有效性验证；参数须通过实际运行程序才能更新。",
            "输入须由上游核准为常规时间终场比分，不能包含加时或点球大战。",
        ],
    }


def _distribution_from_rates(rate_h: float, rate_a: float) -> dict[str, Any]:
    if not all(math.isfinite(x) and x > 0 for x in (rate_h, rate_a)):
        raise ModelDataError("预期进球必须是大于零的有限数。")
    # Each marginal leaves at most 1/4 of the joint tolerance in its upper tail.
    limits = [int(poisson.isf(TAIL_TOLERANCE / 4, rate)) for rate in (rate_h, rate_a)]
    if max(limits) > 400:
        raise ModelDataError("预期进球异常，所需比分矩阵过大；请检查训练数据及参数。")
    home_pmf = poisson.pmf(np.arange(limits[0] + 1), rate_h)
    away_pmf = poisson.pmf(np.arange(limits[1] + 1), rate_a)
    matrix = np.outer(home_pmf, away_pmf)
    mass = float(matrix.sum())
    tail_mass = max(0.0, 1.0 - mass)
    if tail_mass > TAIL_TOLERANCE:
        raise ModelDataError("比分矩阵未达到尾部误差要求，拒绝输出不完整概率。")
    matrix /= mass
    return {
        "expected_goals": {"home": float(rate_h), "away": float(rate_a)},
        "probabilities": {"home_win": float(np.tril(matrix, -1).sum()),
                          "draw": float(np.trace(matrix)),
                          "away_win": float(np.triu(matrix, 1).sum())},
        "score_matrix": matrix.tolist(),
        "score_matrix_axes": {"rows": "home_goals_from_0", "columns": "away_goals_from_0"},
        "tail_mass_before_normalization": tail_mass,
        "tail_tolerance": TAIL_TOLERANCE,
        "matrix_renormalized": True,
    }


def score_distribution(
    model: dict[str, Any],
    home_id: int,
    away_id: int,
    kickoff_iso: str,
    neutral: bool = False,
) -> dict[str, Any]:
    """Return a regulation-time score distribution; no odds, lineup effect or EV."""
    if model.get("model_version") != MODEL_VERSION or model.get("fit_status") != "converged":
        raise ModelDataError("模型版本不兼容或尚未成功拟合。")
    if not model.get("graph_connected"):
        raise ModelDataError("模型的训练对阵图不连通，不能进行跨组比较。")
    if not isinstance(neutral, bool):
        raise ModelDataError("neutral 必须是布尔值。")
    home_id, away_id = _integer(home_id, "home_id", 1), _integer(away_id, "away_id", 1)
    if home_id == away_id:
        raise ModelDataError("主客队不能是同一支球队。")
    kickoff = _timestamp(kickoff_iso, "kickoff_iso")
    cutoff = _timestamp(model["training_cutoff"], "model.training_cutoff")
    if kickoff < cutoff:
        raise ModelDataError("预测开球时间早于模型训练截止时间，会产生未来信息泄漏。")
    teams = model["team_parameters"]
    unknown = [team for team in (home_id, away_id) if str(team) not in teams]
    if unknown:
        raise UnsupportedTeamError(f"未知球队 ID：{unknown}；缺少训练数据，当前模型不支持。")
    home, away = teams[str(home_id)], teams[str(away_id)]
    log_h = model["intercept"] + (0.0 if neutral else model["home_advantage"]) + home["attack"] - away["defence"]
    log_a = model["intercept"] + away["attack"] - home["defence"]
    if not all(math.isfinite(value) and value < 7 for value in (log_h, log_a)):
        raise ModelDataError("模型参数产生异常预期进球，请检查参数文件。")
    forecast = _distribution_from_rates(math.exp(log_h), math.exp(log_a))
    forecast.update({
        "home_id": home_id, "away_id": away_id,
        "home_name": home["name"], "away_name": away["name"],
        "kickoff": _iso(kickoff), "neutral": neutral,
        "model_version": MODEL_VERSION, "model_scope": MODEL_SCOPE,
        "training_cutoff": model["training_cutoff"],
        "training_data_sha256": model["training_data_sha256"],
        "lineup_adjusted": False,
        "team_training_matches": {"home": home["training_matches"], "away": away["training_matches"]},
        "low_sample": min(home["training_matches"], away["training_matches"]) < model["low_sample_team_threshold"],
        "model_age_days": (kickoff - cutoff).total_seconds() / 86400,
        "warnings": ["仅为未调整阵容的球队基线；尚未量化本场人员变化，不含 EV。"] +
        (["至少一队训练不足 5 场，本场为小样本估计。"]
         if min(home["training_matches"], away["training_matches"]) < model["low_sample_team_threshold"] else []),
    })
    return forecast


def _metric_summary(rows: list[tuple[np.ndarray, int]]) -> dict[str, Any] | None:
    if not rows:
        return None
    probabilities = np.array([row[0] for row in rows])
    outcomes = np.array([row[1] for row in rows])
    observed = np.eye(3)[outcomes]
    selected = probabilities[np.arange(len(rows)), outcomes]
    return {
        "n": len(rows),
        "log_loss": float(-np.mean(np.log(np.clip(selected, 1e-15, 1.0)))),
        "brier_score": float(np.mean(np.sum((probabilities - observed) ** 2, axis=1))),
        "brier_definition": "mean(sum_over_3_outcomes((probability - outcome_indicator)^2))",
        "log_loss_probability_floor": 1e-15,
    }


def evaluate_time_holdout(
    matches: list[dict[str, Any]],
    cutoff_iso: str,
    test_fraction: float = 0.2,
    half_life_days: float = 180.0,
    ridge: float = 3.0,
    min_train: int = 180,
    min_test: int = 40,
) -> dict[str, Any]:
    """Exploratory fixed-model future holdout, grouped by UTC kickoff date.

    The model is fitted once at the first test day's midnight and remains fixed
    throughout the holdout. This is explicitly not rolling validation. Unknown
    teams are reported as exclusions and reduce scored coverage. This function
    never marks a model production-approved or claims betting profitability.
    """
    cutoff = _timestamp(cutoff_iso, "cutoff_iso")
    if not math.isfinite(test_fraction) or not 0 < test_fraction < 1:
        raise ModelDataError("test_fraction 必须在 0 与 1 之间。")
    min_train = _integer(min_train, "min_train", 2)
    min_test = _integer(min_test, "min_test", 1)
    records = _validated_matches(matches)
    eligible = [m for m in records if m["_dt"] + RESULT_AVAILABILITY_DELAY <= cutoff]
    if len(eligible) < min_train + min_test:
        raise ModelDataError(
            f"只有 {len(eligible)} 场合格数据，按时间验证至少需要训练 {min_train} 场"
            f"及留出 {min_test} 场。"
        )
    dates = sorted({m["_dt"].date() for m in eligible})
    candidates = []
    for day in dates[1:]:
        boundary = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
        training_count = sum(m["_dt"] + RESULT_AVAILABILITY_DELAY <= boundary for m in eligible)
        testing_count = sum(m["_dt"] >= boundary for m in eligible)
        if training_count >= min_train and testing_count >= min_test:
            candidates.append((abs(testing_count - len(eligible) * test_fraction), boundary))
    if not candidates:
        raise ModelDataError("按 UTC 日期分组并保留 4 小时赛果延迟后，无法满足训练/留出样本数。")
    _, boundary = min(candidates, key=lambda item: (item[0], item[1]))
    training = [m for m in eligible if m["_dt"] + RESULT_AVAILABILITY_DELAY <= boundary]
    testing = [m for m in eligible if m["_dt"] >= boundary]
    embargoed = [m for m in eligible if m["_dt"] < boundary < m["_dt"] + RESULT_AVAILABILITY_DELAY]
    model = fit_model(training, _iso(boundary), half_life_days, ridge, min_train)
    # Half-goal smoothing avoids assigning literal zero probability after an
    # all-zero training column. The baseline uses training observations only.
    baseline_h = (sum(m["home_goals"] for m in training) + 0.5) / (len(training) + 0.5)
    baseline_a = (sum(m["away_goals"] for m in training) + 0.5) / (len(training) + 0.5)
    baseline_normal = _distribution_from_rates(baseline_h, baseline_a)
    baseline_neutral = _distribution_from_rates((baseline_h + baseline_a) / 2, (baseline_h + baseline_a) / 2)
    model_rows, baseline_rows, predictions, exclusions = [], [], [], []
    order = ("home_win", "draw", "away_win")
    for match in testing:
        entry = {
            "match_id": match["match_id"], "kickoff": match["kickoff"],
            "home_id": match["home_id"], "away_id": match["away_id"],
            "actual_home_goals": match["home_goals"], "actual_away_goals": match["away_goals"],
        }
        try:
            forecast = score_distribution(model, match["home_id"], match["away_id"], match["kickoff"], match["neutral"])
        except UnsupportedTeamError as exc:
            entry.update({"status": "excluded_unknown_team", "reason": str(exc)})
            predictions.append(entry)
            exclusions.append({"match_id": match["match_id"], "reason": str(exc)})
            continue
        baseline = baseline_neutral if match["neutral"] else baseline_normal
        outcome = 0 if match["home_goals"] > match["away_goals"] else (1 if match["home_goals"] == match["away_goals"] else 2)
        model_rows.append((np.array([forecast["probabilities"][key] for key in order]), outcome))
        baseline_rows.append((np.array([baseline["probabilities"][key] for key in order]), outcome))
        entry.update({"status": "scored", "forecast": forecast, "baseline_probabilities": baseline["probabilities"]})
        predictions.append(entry)
    metrics = _metric_summary(model_rows)
    baseline_metrics = _metric_summary(baseline_rows)
    enough_scored = len(model_rows) >= min_test
    report = {
        "validation_status": "exploratory_single_fixed_holdout" if enough_scored else "insufficient_supported_test_matches",
        "deployment_approved": False,
        "is_rolling_backtest": False,
        "minimum_test_matches": min_test,
        "meets_minimum_supported_test_matches": enough_scored,
        "evaluation_data_cutoff": _iso(cutoff),
        "split": {
            "method": "chronological_fixed_model_holdout", "grouped_by": "kickoff_utc_calendar_date",
            "train_cutoff": _iso(boundary), "train_last_kickoff": training[-1]["kickoff"],
            "test_start": testing[0]["kickoff"], "test_end": testing[-1]["kickoff"],
            "training_count": len(training), "testing_count": len(testing),
            "result_availability_delay_hours": 4,
            "embargoed_count": len(embargoed), "embargoed_match_ids": [m["match_id"] for m in embargoed],
        },
        "coverage": {"total": len(testing), "scored": len(model_rows), "excluded": len(exclusions),
                     "ratio": len(model_rows) / len(testing), "exclusions": exclusions},
        "model_metrics": metrics, "baseline_metrics": baseline_metrics,
        "baseline": {"name": "training_league_home_away_mean_poisson", "home_rate": baseline_h,
                     "away_rate": baseline_a, "smoothing": "(sum_goals + 0.5) / (n_matches + 0.5)",
                     "same_scored_matches_as_model": True},
        "comparison": ({"model_minus_baseline_log_loss": metrics["log_loss"] - baseline_metrics["log_loss"],
                        "model_minus_baseline_brier_score": metrics["brier_score"] - baseline_metrics["brier_score"],
                        "lower_is_better": True} if metrics is not None else None),
        "warnings": [
            "单次固定模型留出验证仅为探索性检查，不是滚动回测，也不构成上线或盈利证明。",
            "不含赔率、投注收益、阵容修正或提前十小时伤停历史快照验证。",
            "超参数为显式默认值，不能根据本留出期反复挑选参数后再声称独立验证。",
            "未知球队比赛单独列为未覆盖，指标只针对 model_metrics.n 场已覆盖比赛。",
        ],
    }
    return {"report": report, "predictions": predictions, "model": model}
