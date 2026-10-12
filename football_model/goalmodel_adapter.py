"""Isolated, pinned upstream goalmodel R research adapter.

G0 estimates attack/defence independently of the production fit. G1 jointly
estimates the same parameters and explicitly supplied covariates. Neither path
is a production fallback, an offset correction or evidence of predictive gain.
Only the upstream Poisson/DC functions are sourced; no R package installation,
API collection or production model mutation occurs here.
"""

from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timezone
from pathlib import Path
import shutil
import subprocess
import tempfile

import numpy as np
from scipy.stats import poisson

from .model import (ModelDataError, RESULT_AVAILABILITY_DELAY, TAIL_TOLERANCE,
                    _connected_components, _integer, _iso, _timestamp,
                    _validated_matches)


GOALMODEL_SOURCE_COMMIT = "84ecd6c2bbad3ccb967abf88ef49e5bcd074e545"
GOALMODEL_UPSTREAM_VERSION = "0.6.4"
GOALMODEL_ADAPTER_VERSION = "goalmodel_independent_research_v1"
RSCRIPT_TIMEOUT_SECONDS = 180
_SOURCE_HASHES = {
    "goalmodel_fit.R": "40114bcd92e26069a41b315878db7ddf7e460393e79a2806849ec019d66cfdd2",
    "goalmodel_predict.R": "7c303437aad6d40b0c04bcb7b1ae41d95e4a7f389e1976fea1d2dff070688ebc",
}


def _finite(value, label):
    if isinstance(value, bool) or not isinstance(value, (int, float, np.number)):
        raise ModelDataError(f"{label} 必须是有限数值")
    value = float(value)
    if not math.isfinite(value):
        raise ModelDataError(f"{label} 必须是有限数值")
    return value


def _features(records, names, features, label):
    if not names:
        if features not in (None, {}):
            raise ModelDataError(f"{label} 有值但 feature_names 为空")
        return {}
    if not isinstance(features, dict):
        raise ModelDataError(f"{label} 必须按 match_id 提供主客同列特征")
    result = {}
    for record in records:
        mid = record["match_id"]
        # Ambiguous duplicate int/string IDs are not resolved by preference.
        if mid in features and str(mid) in features:
            raise ModelDataError(f"{label} 比赛 {mid} 特征键重复")
        entry = features.get(mid, features.get(str(mid)))
        if not isinstance(entry, dict) or set(entry) != {"home", "away"}:
            raise ModelDataError(f"{label} 比赛 {mid} 需要 home/away 两侧")
        result[str(mid)] = {}
        for side in ("home", "away"):
            if not isinstance(entry[side], dict) or set(entry[side]) != set(names):
                raise ModelDataError(f"{label} 比赛 {mid} {side} 特征列必须与 feature_names 一致")
            result[str(mid)][side] = {
                name: _finite(entry[side][name], f"{label} {mid} {side} {name}")
                for name in names
            }
    return result


def _fixtures(fixtures, cutoff):
    if not isinstance(fixtures, (list, tuple)):
        raise ModelDataError("fixtures 必须是列表")
    result, seen = [], set()
    for item in fixtures:
        if not isinstance(item, dict):
            raise ModelDataError("fixture 必须是字典")
        mid = _integer(item.get("match_id"), "fixture match_id", 1)
        home = _integer(item.get("home_id"), "fixture home_id", 1)
        away = _integer(item.get("away_id"), "fixture away_id", 1)
        if mid in seen or home == away:
            raise ModelDataError("fixture ID 重复或主客队相同")
        seen.add(mid)
        kickoff = _timestamp(item.get("kickoff"), "fixture kickoff")
        if kickoff < cutoff:
            raise ModelDataError("待预测开赛时间不能早于训练截止时间")
        neutral = item.get("neutral", False)
        if not isinstance(neutral, bool):
            raise ModelDataError("fixture neutral 必须是布尔值")
        result.append({"match_id": mid, "home_id": home, "away_id": away,
                       "kickoff": _iso(kickoff), "neutral": neutral})
    return result


def _prepare_sources(repo_root):
    """Verify exact vendored upstream bytes; never fetch mutable R dependencies."""
    destination = Path(repo_root) / "third_party" / "goalmodel" / "R"
    result = {}
    for name, expected in _SOURCE_HASHES.items():
        path = destination / name
        contents = path.read_bytes()
        if hashlib.sha256(contents).hexdigest() != expected:
            raise ModelDataError(f"goalmodel 固定源文件指纹错误：{name}")
        result[name] = str(path.resolve())
    return result


def _validate_output(output, fixtures, dc):
    if not isinstance(output, dict) or output.get("schema_version") != 1:
        raise ModelDataError("R 输出不是 schema_version=1")
    if output.get("source_commit") != GOALMODEL_SOURCE_COMMIT:
        raise ModelDataError("R 输出 goalmodel 来源提交不匹配")
    if output.get("upstream_version") != GOALMODEL_UPSTREAM_VERSION:
        raise ModelDataError("R 输出 goalmodel 版本不匹配")
    if output.get("source_mode") != "pinned_official_R_sources":
        raise ModelDataError("R 输出 source_mode 不匹配")
    if output.get("dc") is not dc:
        raise ModelDataError("R 输出 DC 分支不匹配")
    if output.get("status") != "ok" or output.get("fit", {}).get("converged") is not True:
        raise ModelDataError("R 拟合失败或未收敛")
    forecasts, errors = output.get("forecasts"), output.get("errors")
    if not isinstance(forecasts, list) or not isinstance(errors, list):
        raise ModelDataError("R 输出 forecasts/errors 必须是列表")
    expected = {item["match_id"]: item for item in fixtures}
    seen = set()
    for forecast in forecasts:
        if not isinstance(forecast, dict):
            raise ModelDataError("R forecast 必须是对象")
        mid = forecast.get("match_id")
        if type(mid) is not int or mid not in expected or mid in seen:
            raise ModelDataError("R forecast 身份缺失、重复或未请求")
        seen.add(mid)
        for field in ("home_id", "away_id", "kickoff", "neutral"):
            if forecast.get(field) != expected[mid][field]:
                raise ModelDataError(f"R forecast {mid} {field} 不匹配")
        lh, la = (_finite(forecast.get(field), field)
                  for field in ("expected_home_goals", "expected_away_goals"))
        if lh <= 0 or la <= 0:
            raise ModelDataError("R 期望进球必须为正")
        matrix = np.asarray(forecast.get("score_matrix"), dtype=float)
        if (matrix.ndim != 2 or min(matrix.shape) < 2 or max(matrix.shape) > 128
                or not np.isfinite(matrix).all() or (matrix < 0).any()
                or abs(float(matrix.sum()) - 1) > 1e-10):
            raise ModelDataError("R 比分矩阵维度、有限性或归一化失败")
        if forecast.get("score_matrix_axes") != {
                "rows": "home_goals_from_0", "columns": "away_goals_from_0"}:
            raise ModelDataError("R 比分矩阵轴不匹配")
        raw = np.outer(poisson.pmf(np.arange(matrix.shape[0]), lh),
                       poisson.pmf(np.arange(matrix.shape[1]), la))
        rho = _finite(forecast.get("rho", 0), "rho")
        if not dc and rho != 0:
            raise ModelDataError("独立 Poisson 分支不能含非零 rho")
        if dc:
            raw[:2, :2] *= np.array([[1 - lh * la * rho, 1 + lh * rho],
                                     [1 + la * rho, 1 - rho]])
        if (raw < 0).any() or not np.isfinite(raw).all() or raw.sum() <= 0:
            raise ModelDataError("R DC 修正产生非法概率")
        tail = 1 - float(raw.sum())
        declared_tail = _finite(forecast.get("tail_mass_before_normalization"), "tail_mass")
        if abs(tail - declared_tail) > 1e-10 or tail > TAIL_TOLERANCE:
            raise ModelDataError("R 尾部质量未通过核验")
        if not np.allclose(matrix, raw / raw.sum(), atol=1e-10, rtol=0):
            raise ModelDataError("R 矩阵与上游 Poisson/DC 参数复算不一致")
        probabilities = forecast.get("probabilities_1x2")
        recalculated = {"home": float(np.tril(matrix, -1).sum()),
                        "draw": float(np.trace(matrix)),
                        "away": float(np.triu(matrix, 1).sum())}
        if not isinstance(probabilities, dict) or set(probabilities) != set(recalculated):
            raise ModelDataError("R 1X2 字段集合错误")
        for field, actual in recalculated.items():
            if abs(_finite(probabilities[field], field) - actual) > 1e-10:
                raise ModelDataError("R 1X2 与完整矩阵不一致")
    for error in errors:
        if not isinstance(error, dict) or type(error.get("match_id")) is not int:
            raise ModelDataError("R 逐场错误缺少 match_id")
        mid = error["match_id"]
        if mid not in expected or mid in seen or not error.get("code"):
            raise ModelDataError("R 错误身份重复、未知或缺少原因")
        seen.add(mid)
    if seen != set(expected):
        raise ModelDataError("R 输出遗漏比赛，不能静默视为已预测")
    return output


def fit_predict_goalmodel(training_matches, fixtures, *, cutoff,
                         feature_names=(), training_features=None,
                         prediction_features=None, dc=False,
                         rscript="Rscript", repo_root=None):
    """Fit one pinned independent R model and return validated research forecasts.

    Feature mappings are ``{match_id: {'home': {name: number}, 'away': ...}}``.
    Feature provenance/as-of admission remains the caller's responsibility;
    this adapter refuses absent or asymmetric columns, never fills them with 0.
    Goals are the existing normalized, verified 90-minute result contract.
    Training matches at kickoff+4h > cutoff are excluded; no synthetic result
    finality or historical feature availability is inferred from that delay.
    R absence, timeout and fitting errors return errors with no successful
    forecast. Invalid input raises ModelDataError before any R execution.
    """
    cutoff_dt = _timestamp(cutoff, "cutoff")
    if not isinstance(dc, bool):
        raise ModelDataError("dc 必须是布尔值")
    if not isinstance(feature_names, (tuple, list)) or any(
            not isinstance(name, str) or not name.strip() or name.startswith("__")
            for name in feature_names) or len(set(feature_names)) != len(feature_names):
        raise ModelDataError("feature_names 必须是唯一非空列名；__ 前缀保留")
    names = list(feature_names)
    all_records = _validated_matches(training_matches)
    records = [item for item in all_records
               if item["_dt"] + RESULT_AVAILABILITY_DELAY <= cutoff_dt]
    if len(records) < 2 or len(_connected_components(records)) != 1:
        raise ModelDataError("goalmodel 需要至少两场且训练对阵图连通")
    requested = _fixtures(fixtures, cutoff_dt)
    if {item["match_id"] for item in requested} & {item["match_id"] for item in records}:
        raise ModelDataError("待预测比赛与训练目标重叠")
    train_features = _features(records, names, training_features, "training_features")
    pred_features = _features(requested, names, prediction_features, "prediction_features")
    serial_records = [{key: value for key, value in item.items() if key != "_dt"}
                      for item in records]
    weights = [math.exp(-math.log(2) * (cutoff_dt - item["_dt"]).total_seconds()
                        / (86400 * 180.0)) for item in records]
    if sum(weights) < 1e-8:
        raise ModelDataError("goalmodel 训练权重过小")
    root = Path(repo_root).resolve() if repo_root else Path(__file__).resolve().parents[1]
    branch = ("G1" if names else "G0") + ("_DC" if dc else "")
    result = {"version": GOALMODEL_ADAPTER_VERSION,
              "upstream_version": GOALMODEL_UPSTREAM_VERSION,
              "source_commit": GOALMODEL_SOURCE_COMMIT,
              "source_sha256": dict(_SOURCE_HASHES),
              "source_mode": "pinned_official_R_sources", "branch": branch,
              "research_only": True, "predictive_gain_verified": False,
              "forecasts": [], "errors": [], "fit": None,
              "cutoff": _iso(cutoff_dt), "training_count": len(records),
              "excluded_training_count": len(all_records) - len(records),
              "training_match_ids": [item["match_id"] for item in records],
              "half_life_days": 180.0, "feature_names": names,
              "training_weights": weights, "dc": dc}

    def fail(code, message):
        if result.get("r_started_at"):
            result["r_finished_at"] = _iso(datetime.now(timezone.utc))
        result["status"] = "failed"
        result["errors"] = [{"match_id": item["match_id"], "code": code,
                              "message": message} for item in requested]
        result["execution_error"] = {"code": code, "message": message}
        return result

    executable = shutil.which(str(rscript))
    if executable is None:
        return fail("rscript_unavailable", "Rscript 不可用；未运行 R，不生成替代预测")
    bridge = root / "scripts" / "goalmodel_bridge.R"
    if not bridge.is_file():
        return fail("bridge_unavailable", "未找到 scripts/goalmodel_bridge.R")
    try:
        sources = _prepare_sources(root)
        request = {"schema_version": 1, "source_commit": GOALMODEL_SOURCE_COMMIT,
                   "upstream_version": GOALMODEL_UPSTREAM_VERSION,
                   "source_paths": sources, "cutoff": _iso(cutoff_dt),
                   "training_matches": serial_records, "fixtures": requested,
                   "training_weights": weights, "feature_names": names,
                   "training_features": train_features, "prediction_features": pred_features,
                   "dc": dc, "tail_tolerance": TAIL_TOLERANCE}
        fingerprint_input = {key: value for key, value in request.items() if key != "source_paths"}
        result["request_sha256"] = hashlib.sha256(json.dumps(
            fingerprint_input, sort_keys=True, separators=(",", ":"),
            allow_nan=False).encode()).hexdigest()
        with tempfile.TemporaryDirectory(prefix="goalmodel-") as temporary:
            input_path, output_path = Path(temporary) / "input.json", Path(temporary) / "output.json"
            input_path.write_text(json.dumps(request, ensure_ascii=False, allow_nan=False), encoding="utf-8")
            result["r_started_at"] = _iso(datetime.now(timezone.utc))
            process = subprocess.run([executable, "--vanilla", str(bridge),
                                      str(input_path), str(output_path)],
                                     capture_output=True, text=True,
                                     timeout=RSCRIPT_TIMEOUT_SECONDS, check=False)
            result["r_finished_at"] = _iso(datetime.now(timezone.utc))
            result["r_exit_code"] = process.returncode
            result["r_stderr"] = process.stderr[-4000:]
            if process.returncode != 0:
                return fail("r_execution_failed", "R bridge 失败；详情见 r_stderr")
            if not output_path.is_file() or output_path.stat().st_size > 64 * 1024 * 1024:
                return fail("r_output_invalid", "R 未生成有界结果文件")
            output = _validate_output(json.loads(output_path.read_text(encoding="utf-8")), requested, dc)
    except subprocess.TimeoutExpired:
        return fail("r_timeout", "R 执行超过固定时间预算；未生成预测")
    except (OSError, ValueError, TypeError) as exc:
        return fail("r_adapter_error", str(exc)[:1500])
    result.update({"status": "ok", "forecasts": output["forecasts"],
                   "errors": output["errors"], "fit": output["fit"],
                   "r_version": output.get("r_version"),
                   "r_warnings": output.get("warnings", [])})
    for forecast in result["forecasts"]:
        forecast["expected_goals"] = {"home": forecast["expected_home_goals"],
                                       "away": forecast["expected_away_goals"]}
        forecast["probabilities"] = {
            "home_win": forecast["probabilities_1x2"]["home"],
            "draw": forecast["probabilities_1x2"]["draw"],
            "away_win": forecast["probabilities_1x2"]["away"]}
        forecast["tail_tolerance"] = TAIL_TOLERANCE
        forecast["matrix_renormalized"] = True
        forecast["model_version"] = GOALMODEL_ADAPTER_VERSION
        forecast["model_scope"] = "independent_goalmodel_research_only"
    return result
