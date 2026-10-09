"""Traceable access to saved research forecasts; never trains or changes models."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import sys

import numpy as np

from .cli import atomic_json, iso, parse_date, safe_error, TIME_SEMANTICS, forecast_availability, availability_summary
from .model import score_distribution

UTC = timezone.utc


class IntegrationError(ValueError):
    """Saved outputs do not establish a usable forecast at the requested cutoff."""


def _read(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"), parse_constant=lambda _: (_ for _ in ()).throw(ValueError("non-finite JSON")))
    except (OSError, ValueError) as exc:
        raise IntegrationError(f"无法读取有效JSON：{path.name}") from exc


def _time(value, label):
    try:
        if not isinstance(value, str):
            raise ValueError()
        return parse_date(value)
    except (ValueError, TypeError) as exc:
        raise IntegrationError(f"{label} 缺少有效时区时间。") from exc


def _token(value, label):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise IntegrationError(f"{label} 不是安全标识。")
    return value


def _positive_id(value, label):
    if type(value) is not int or value <= 0:
        raise IntegrationError(f"{label} 必须为正整数。")
    return value


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _attempt(report, item, path):
    result = {"run_id": report["run_id"], "generated_at": report["generated_at"],
            "mode": report.get("mode"), "status": item.get("status"),
            "detail": item.get("detail"), "model_id": item.get("model_id"),
            "trained_at": item.get("trained_at"), "source_commit": report.get("source_commit"),
            "report_file": path, "forecast_count": item.get("forecast_count"),
            "unsupported_forecasts": item.get("unsupported_forecasts")}
    # Leave legacy index entries byte-for-byte equivalent in structure. Adding
    # invented null timestamp fields would break otherwise usable saved states.
    if report.get("time_semantics") == TIME_SEMANTICS:
        result.update(time_semantics=TIME_SEMANTICS, run_started_at=report["run_started_at"])
        for name in ("data_observed_at", "forecast_generated_at", "model_fitted_at", "training_cutoff"):
            if name in item:
                result[name] = item[name]
    for name in ("forecast_availability", "model_freshness", "model_refresh"):
        if name in item:
            result[name] = item[name]
    return result


def _source_times(seasons, observed, *, require_received=False):
    """Check the stored response envelopes, not only copied forecast labels."""
    if not isinstance(seasons, list) or not seasons:
        raise IntegrationError("缺少实际来源取得时间证据。")
    for season in seasons:
        fetched = _time(season.get("fetched_at"), "来源抓取时间")
        if fetched > observed:
            raise IntegrationError("来源实际抓取晚于声称的数据观测时间，存在前视信息。")
        if require_received:
            received = _time(season.get("received_at"), "来源接收时间")
            if not fetched <= received <= observed:
                raise IntegrationError("来源抓取、接收和数据观测时间顺序不合法。")


def _temporal_provenance(runtime, row, bundle, attempt, now, *, historical):
    """Validate modern completion clocks; legacy clocks cannot prove a past cutoff."""
    model = bundle["model"]
    cutoff = _time(model.get("training_cutoff"), "训练截止")
    if any(_time(value, "训练截止") != cutoff for value in
           (bundle.get("trained_at"), row.get("trained_at"), attempt.get("trained_at"))):
        raise IntegrationError("训练时间/截止不一致。")
    generated = _time(row.get("generated_at"), "预测生成时间")
    observed = _time(row.get("data_observed_at"), "数据观测时间")
    report_time = _time(attempt["generated_at"], "运行报告时间")
    kickoff = _time(row.get("kickoff"), "开赛时间")
    modern_forecast = row.get("time_semantics") == TIME_SEMANTICS
    modern_model = bundle.get("time_semantics") == TIME_SEMANTICS
    if ((row.get("schema_version", 1) != 1 and not modern_forecast)
            or (bundle.get("schema_version", 1) != 1 and not modern_model)):
        raise IntegrationError("新产物缺少适用的真实时间协议；不能静默降级为旧产物。")
    if historical and not (modern_forecast and modern_model):
        raise IntegrationError("旧产物时间证据有限，不能证明显式 --as-of 的历史可得性；省略 --as-of 仅可作当前未来比赛核查，不会补造历史资格。")
    if not cutoff <= generated <= report_time <= now < kickoff or observed > generated:
        raise IntegrationError("预测、来源或训练截点存在前视，或者比赛已到开赛时间。")
    report = _read(runtime / "reports" / "history" / f"{attempt['run_id']}.json")
    items = [item for item in report["competitions"] if item.get("key") == row["competition"]]
    if len(items) != 1:
        raise IntegrationError("预测引用的来源报告缺失或重复。")
    item = items[0]
    # Legacy observed_at was the task start, not source availability. We may
    # inspect it now, but never treat it as proof the response already existed.
    source_bound = observed if modern_forecast else now
    _source_times(item.get("seasons"), source_bound, require_received=modern_forecast)
    if modern_forecast:
        if row.get("schema_version") != 2 or attempt.get("time_semantics") != TIME_SEMANTICS:
            raise IntegrationError("预测与运行报告的时间协议不一致。")
        started = _time(row.get("run_started_at"), "运行开始")
        if (row.get("run_started_at") != attempt.get("run_started_at")
                or row.get("data_observed_at") != attempt.get("data_observed_at")
                or row.get("training_cutoff") != model.get("training_cutoff")
                or row.get("model_fitted_at") != bundle.get("model_fitted_at")
                or attempt.get("model_fitted_at") != bundle.get("model_fitted_at")
                or row.get("forecast_generated_at") != row.get("generated_at")):
            raise IntegrationError("预测时间字段与来源模型/实际运行记录不一致。")
        batch_generated = _time(attempt.get("forecast_generated_at"), "本赛事预测完成时间")
        metadata_received = _time(item.get("metadata_received_at"), "赛事元数据取得时间")
        if not started <= metadata_received <= observed <= generated <= batch_generated <= report_time:
            raise IntegrationError("真实取得、预测完成与报告完成时间顺序不合法。")
    elif generated != report_time or observed != report_time:
        raise IntegrationError("旧预测时间字段与原始运行记录不一致。")
    if modern_model:
        if bundle.get("schema_version") != 2 or bundle.get("training_cutoff") != model.get("training_cutoff"):
            raise IntegrationError("模型训练截止与时间协议不一致。")
        fitted = _time(bundle.get("model_fitted_at"), "模型拟合完成时间")
        model_observed = _time(bundle.get("data_observed_at"), "训练来源取得时间")
        model_started = _time(bundle.get("run_started_at"), "训练运行开始时间")
        if not (model_started <= model_observed <= fitted <= generated and cutoff <= fitted):
            raise IntegrationError("训练来源或拟合完成晚于预测，存在前视信息。")
        _source_times(bundle.get("source_seasons"), model_observed, require_received=True)
        venue_available = bundle.get("national_audit", {}).get("venue_temporal", {}).get("max_evidence_available_at")
        if venue_available and _time(venue_available, "训练场地证据可用时间") > model_observed:
            raise IntegrationError("训练使用的场地证据晚于模型的数据观测时间。")
    else:
        _source_times(bundle.get("source_seasons"), now)
    return {
        "time_semantics": TIME_SEMANTICS if modern_forecast and modern_model else "legacy_time_evidence_limited",
        "run_started_at": row.get("run_started_at"), "model_fitted_at": bundle.get("model_fitted_at"),
        "historical_availability_verified": bool(historical and modern_forecast and modern_model),
        "source_publication_history_verified": False,
        "time_evidence_note": ("真实取得及完成时间已核验；训练留出不等于供应商历史发布时间重演。"
            if modern_forecast and modern_model else
            "旧模型或预测未保存可信完成时间；只允许当前未来比赛复核，不能证明历史截点赛前资格。"),
    }


def build_index(runtime, config, current_report=None, *, write=True):
    """Index every configured league using report history, including failed runs.

    A successful audit is not a new forecast. A failed later attempt is a fence:
    it never silently exposes an earlier success as a current usable forecast.
    """
    runtime = Path(runtime)
    competitions = { _token(c["key"], "competition key"): c for c in config["competitions"] }
    reports = {}
    for path in sorted((runtime / "reports" / "history").glob("*.json")):
        report = _read(path)
        if not isinstance(report, dict):
            raise IntegrationError("运行报告不是JSON对象。")
        run_id = _token(report.get("run_id"), "run_id")
        if path.stem != run_id:
            raise IntegrationError("报告文件名与run_id不一致。")
        reports[run_id] = report
    if current_report is not None:
        reports[_token(current_report.get("run_id"), "run_id")] = current_report
    ordered = sorted(reports.values(), key=lambda r: (_time(r.get("generated_at"), "报告时间"), r["run_id"]))
    records = {key: {"key": key, "label": c["label"], "mode": c["mode"],
                     "latest_attempt": None, "latest_train_attempt": None,
                     "latest_successful_forecast": None} for key, c in competitions.items()}
    for report in ordered:
        run_id = report["run_id"]
        path = f"runtime/reports/history/{run_id}.json"
        items = report.get("competitions")
        if not isinstance(items, list):
            raise IntegrationError("运行报告缺少competitions列表。")
        seen = set()
        for item in items:
            if not isinstance(item, dict):
                raise IntegrationError("联赛运行记录不是JSON对象。")
            key = item.get("key")
            if key not in records:
                continue
            if key in seen:
                raise IntegrationError("同一报告包含重复联赛。")
            seen.add(key)
            entry = records[key]
            attempt = _attempt(report, item, path)
            entry["latest_attempt"] = attempt
            if report.get("mode") == "train":
                entry["latest_train_attempt"] = attempt
            if report.get("mode") == "train" and item.get("status") == "探索性基线":
                entry["latest_successful_forecast"] = {
                    **attempt, "prediction_file": f"runtime/predictions/{run_id}/{key}.json"}
        if report.get("fatal_error"):
            # Older reports did not record requested scope. Do not guess that a
            # global authentication/data failure left a particular league fresh.
            scope = report.get("requested_competitions", list(records))
            if not isinstance(scope, list):
                raise IntegrationError("失败报告的请求赛事范围无效。")
            for key in scope:
                if key not in records or key in seen:
                    continue
                attempt = _attempt(report, {"status": "未完成", "detail": report["fatal_error"]}, path)
                records[key]["latest_attempt"] = attempt
                if report.get("mode") == "train":
                    records[key]["latest_train_attempt"] = attempt
        if report.get("integration_error"):
            # A failed index update fences every league because the aggregate
            # index could not be established, even if individual fits succeeded.
            for entry in records.values():
                entry["latest_attempt"] = _attempt(report, {"status": "未完成", "detail": report["integration_error"]}, path)
    for key, entry in records.items():
        latest, trained, success = (entry[n] for n in ("latest_attempt", "latest_train_attempt", "latest_successful_forecast"))
        model_path = runtime / "models" / f"{key}.json"
        entry["current_model_file"] = f"runtime/models/{key}.json" if model_path.exists() else None
        entry["current_model_id"] = None
        entry["usable_saved_forecasts"] = False
        if model_path.exists():
            bundle = _read(model_path)
            if not isinstance(bundle, dict):
                raise IntegrationError(f"{key}模型文件不是JSON对象。")
            entry["current_model_id"] = bundle.get("model_id")
            entry["training_cutoff"] = bundle.get("model", {}).get("training_cutoff")
            entry["validation_status"] = bundle.get("validation", {}).get("validation_status")
        if entry["mode"] == "audit_only":
            entry["status"] = "audit_only"
        elif latest is None:
            entry["status"] = "no_run"
        elif latest["status"] == "未完成":
            entry["status"] = "blocked_latest_failure"
        elif trained is None:
            entry["status"] = "no_training_forecast"
        elif trained["status"] == "模型陈旧":
            # A genuine empty fixture window is distinct from failed coverage.
            coverage = trained.get("forecast_availability")
            entry["status"] = ("no_upcoming" if coverage == forecast_availability([]) else "stale_model")
        elif trained["status"] != "探索性基线" or success is None or trained["run_id"] != success["run_id"]:
            entry["status"] = "blocked_latest_training"
        elif entry["current_model_id"] != success["model_id"]:
            entry["status"] = "model_id_mismatch"
        else:
            rows = _read(runtime / "predictions" / success["run_id"] / f"{key}.json")
            if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                raise IntegrationError(f"{key}预测文件不是列表。")
            supported = sum(row.get("status") == "exploratory_baseline" for row in rows)
            unsupported = sum(row.get("status") == "unsupported" for row in rows)
            if supported + unsupported != len(rows) or supported != success["forecast_count"] or unsupported != success["unsupported_forecasts"]:
                raise IntegrationError(f"{key}预测数量与报告不一致。")
            entry.update(forecast_count=supported, unsupported_forecasts=unsupported,
                         status="no_upcoming" if not rows else "available" if supported else "unsupported_only",
                         usable_saved_forecasts=bool(supported))
            if "forecast_availability" in success:
                coverage = forecast_availability(rows)
                if coverage != success["forecast_availability"]:
                    raise IntegrationError(f"{key}预测可用性与报告不一致。")
        # Preserve old entry shapes for untouched legacy state snapshots. New
        # observability fields are emitted only when the producing run has them.
        if trained and "forecast_availability" in trained:
            entry["model_freshness"] = trained.get("model_freshness", {"status": "not_checked"})
            if entry["status"] in {"available", "unsupported_only", "no_upcoming"}:
                entry["forecast_availability"] = trained["forecast_availability"]
                if entry["status"] == "no_upcoming":
                    entry.update(forecast_count=0, unsupported_forecasts=0)
            else:
                entry["forecast_availability"] = {"status": entry["status"],
                    "upcoming_count": (latest or {}).get("forecast_availability", {}).get("upcoming_count"),
                    "supported_count": None, "unsupported_count": None, "unsupported_reasons": {}}
    index = {"schema_version": 1, "status": "ready", "generated_at": iso(datetime.now(UTC)),
             "source_commit": current_report.get("source_commit") if current_report else (ordered[-1].get("source_commit") if ordered else None),
             "history_reports": len(ordered), "competitions": list(records.values()),
             "forecast_availability_summary": availability_summary(list(records.values())),
             "usage": "索引仅标识已保存结果；运行成功不等于有可用预测。空窗、全部不支持和陈旧分别记录；单场仍须核查截点、赛程、场地和适用性。无自动质量验收。"}
    if write:
        destination = runtime / "integration"
        atomic_json(destination / "index.json", index)
        lines = ["# 模型接入索引", "", f"索引生成：{index['generated_at']}", "", index["usage"], "",
                 "|联赛|接入状态|预测可用性|模型时效|最近尝试|当前模型|可用/不支持预测|", "|---|---|---|---|---|---|---|"]
        for entry in index["competitions"]:
            attempt = entry["latest_attempt"] or {}
            lines.append(f"|{entry['label']}|{entry['status']}|{entry.get('forecast_availability', {}).get('status', 'legacy_unreported')}|{entry.get('model_freshness', {}).get('status', 'legacy_unreported')}|{attempt.get('run_id', '无')} / {attempt.get('status', '无')}|{entry['current_model_id'] or '无'}|{entry.get('forecast_count', '—')} / {entry.get('unsupported_forecasts', '—')}|")
        (destination / "index.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return index


def _entry_for_match(runtime, config, league):
    """Validate a target-only fixed snapshot; unrelated models are unnecessary.

    A caller must retrieve the current index and its referenced target reports
    at one fixed state commit. Any additional local report is also scanned, so
    an available newer failure cannot be hidden by a stale saved index.
    """
    chosen = [c for c in config["competitions"] if c["key"] == league]
    if len(chosen) != 1:
        raise IntegrationError("联赛未配置。")
    saved_path = runtime / "integration" / "index.json"
    if not saved_path.is_file():
        raise IntegrationError("缺少当前接入索引；请取得固定云端索引，或在完整状态上先运行index命令。不能从部分历史猜测最新状态。")
    saved_entry = None
    if saved_path.exists():
        saved = _read(saved_path)
        if not isinstance(saved, dict) or saved.get("status") == "failed":
            raise IntegrationError("最新接入索引更新失败；不得使用旧索引或旧预测。")
        if saved.get("schema_version") != 1 or not isinstance(saved.get("competitions"), list):
            raise IntegrationError("接入索引结构无效。")
        matching = [e for e in saved["competitions"] if isinstance(e, dict) and e.get("key") == league]
        if len(matching) != 1:
            raise IntegrationError("接入索引中目标联赛缺失或重复。")
        saved_entry = matching[0]
        for name in ("latest_attempt", "latest_train_attempt", "latest_successful_forecast"):
            reference = saved_entry.get(name)
            if reference is None:
                continue
            if not isinstance(reference, dict):
                raise IntegrationError("接入索引的报告引用无效。")
            run_id = _token(reference.get("run_id"), "run_id")
            if reference.get("report_file") != f"runtime/reports/history/{run_id}.json" or not (runtime / "reports" / "history" / f"{run_id}.json").is_file():
                raise IntegrationError("缺少索引引用的本联赛报告；请从同一状态提交读取。")
    targeted = {**config, "competitions": chosen}
    actual = build_index(runtime, targeted, write=False)["competitions"][0]
    if saved_entry is not None and saved_entry != actual:
        raise IntegrationError("索引与实际报告/当前模型不一致；请从最新固定状态重取，不能静默回退。")
    return actual


def _same(actual, expected, path="prediction"):
    """Compare complete saved output to original prediction function, not a clone."""
    if isinstance(expected, dict):
        if not isinstance(actual, dict) or set(actual) != set(expected):
            raise IntegrationError(f"{path}字段与原模型输出不一致。")
        for key, value in expected.items():
            _same(actual[key], value, f"{path}.{key}")
    elif isinstance(expected, list):
        if not isinstance(actual, list) or len(actual) != len(expected):
            raise IntegrationError(f"{path}维度不一致。")
        for i, value in enumerate(expected):
            _same(actual[i], value, f"{path}[{i}]")
    elif isinstance(expected, float):
        if isinstance(actual, bool) or not isinstance(actual, (float, int)) or not math.isfinite(actual) or not math.isclose(actual, expected, rel_tol=1e-12, abs_tol=1e-12):
            raise IntegrationError(f"{path}数值与原模型复算不一致。")
    elif type(actual) is not type(expected) or actual != expected:
        raise IntegrationError(f"{path}与原模型输出不一致。")


def match_packet(runtime, config, league, match_id, as_of=None, state_commit=None):
    """Read, validate and reproduce a saved forecast without fitting parameters."""
    runtime = Path(runtime)
    if any((base / "RESEARCH_REPLAY.json").exists() for base in (runtime, runtime.parent)):
        raise IntegrationError("研究回放目录不能作为真实保存的赛前预测接入。")
    now = _time(as_of, "分析截点") if as_of else datetime.now(UTC)
    if now > datetime.now(UTC):
        raise IntegrationError("分析截点不能晚于实际当前时间。")
    match_id = _positive_id(match_id, "match_id")
    league = _token(league, "league")
    if state_commit is not None and not re.fullmatch(r"[0-9a-fA-F]{40,64}", state_commit):
        raise IntegrationError("state_commit须为完整Git提交SHA。")
    entry = _entry_for_match(runtime, config, league)
    if entry["status"] != "available":
        raise IntegrationError(f"本联赛无可调用预测：{entry['status']}；不得静默使用旧预测。")
    attempt = entry["latest_successful_forecast"]
    latest = entry["latest_attempt"]
    for candidate in (attempt, latest):
        report = _read(runtime / "reports" / "history" / f"{candidate['run_id']}.json")
        if report.get("research_replay"):
            raise IntegrationError("研究回放报告不能作为真实保存的赛前预测接入。")
        if _time(candidate["generated_at"], "报告时间") > now:
            raise IntegrationError("分析截点早于已知报告，存在前视信息；须读取该时点真实保存的历史状态。")
    prediction_path = runtime / "predictions" / attempt["run_id"] / f"{league}.json"
    rows = _read(prediction_path)
    identities = [_positive_id(row.get("match_id"), "预测match_id") for row in rows]
    if len(set(identities)) != len(identities):
        raise IntegrationError("预测中有重复比赛ID。")
    matches = [row for row in rows if row["match_id"] == match_id]
    if len(matches) != 1:
        raise IntegrationError("该比赛不在本联赛最新保存预测中；不能按名称猜测或虚构输出。")
    row = matches[0]
    if row.get("research_replay"):
        raise IntegrationError("研究回放输出不能作为真实保存的赛前预测接入。")
    if row.get("status") != "exploratory_baseline":
        raise IntegrationError(f"本场不支持：{row.get('reason', '缺少有效预测')}。")
    model_path = runtime / "models" / f"{league}.json"
    bundle = _read(model_path)
    competition = next(c for c in config["competitions"] if c["key"] == league)
    if bundle.get("competition") != competition or competition["mode"] not in {"domestic_baseline", "national_baseline"} or row.get("competition") != league:
        raise IntegrationError("联赛模型、配置或预测目标不一致。")
    national = competition["mode"] == "national_baseline"
    if national and league != "unl":
        raise IntegrationError("国家队适配尚未覆盖该赛事。")
    if row.get("model_id") != bundle.get("model_id") or row.get("model_id") != attempt["model_id"]:
        raise IntegrationError("预测与当前模型ID不一致。")
    model = bundle["model"]
    validation = bundle.get("validation")
    if not isinstance(validation, dict) or not validation.get("meets_minimum_supported_test_matches"):
        raise IntegrationError("模型缺少实际验证记录或有效覆盖不足。")
    if bundle.get("maturity") != "exploratory_baseline" or model.get("validated_for_production") is not False or validation.get("deployment_approved") is not False:
        raise IntegrationError("模型成熟度与本接入协议不一致，不能宣称已通过正式验收。")
    if model.get("optimizer", {}).get("parameter_at_bound") is not False:
        raise IntegrationError("模型参数边界状态未经确认。")
    time_provenance = _temporal_provenance(runtime, row, bundle, attempt, now,
                                          historical=as_of is not None)
    trained = _time(model.get("training_cutoff"), "模型训练截止")
    kickoff = _time(row.get("kickoff"), "开赛时间")
    maximum_age = config["max_model_age_days"]
    if not isinstance(maximum_age, (float, int)) or isinstance(maximum_age, bool) or not math.isfinite(maximum_age) or maximum_age <= 0:
        raise IntegrationError("模型时效配置无效。")
    if max((now - trained).total_seconds(), (kickoff - trained).total_seconds()) > maximum_age * 86400:
        raise IntegrationError("当前或开赛时模型已超过配置时效；不得刷新训练时间代替重训。")
    home_id = _positive_id(row.get("home_id"), "home_id")
    away_id = _positive_id(row.get("away_id"), "away_id")
    if home_id == away_id or any(not isinstance(row.get(k), str) or not row[k].strip() for k in ("home_name", "away_name")):
        raise IntegrationError("主客队身份无效。")
    prediction = row.get("prediction")
    venue_registry_provenance = {}
    if national:
        from .nations import validate_nations_forecast, venue_temporal_audit
        from .venue_registry import load_forecast_registry
        try:
            registry = load_forecast_registry(runtime, row)
            validate_nations_forecast(row, registry=registry)
            temporal = venue_temporal_audit(row, registry, cutoff=iso(now),
                valid_at=row["kickoff"], strict=as_of is not None)
            available = temporal.get("evidence_available_at")
            if available and _time(available, "场地证据实际可用时间") > now:
                raise IntegrationError("场地证据晚于分析截点，不能回填历史预测。")
            if row.get("time_semantics") == TIME_SEMANTICS and available and (
                    _time(available, "场地证据实际可用时间") > _time(row["data_observed_at"], "数据观测时间")):
                raise IntegrationError("场地证据晚于预测的数据取得时间。")
            venue_registry_provenance["venue_temporal_audit"] = temporal
            if registry is not None:
                registry_path = runtime / "registries" / "nations_venues" / (
                    row["venue_evidence"]["registry_sha256"] + ".json")
                venue_registry_provenance.update({
                    "venue_registry_snapshot": row["registry_snapshot"],
                    "venue_registry_file_sha256": _sha(registry_path),
                    "venue_registry_digest": row["venue_evidence"]["registry_sha256"],
                })
        except (ValueError, KeyError, TypeError, OSError) as exc:
            raise IntegrationError("欧国联场地证据无效或与当前登记表不一致。") from exc
        if not isinstance(prediction, dict) or prediction.get("neutral") is not row["neutral"]:
            raise IntegrationError("欧国联场地标记与预测不一致。")
    elif not isinstance(prediction, dict) or prediction.get("neutral") is not False:
        raise IntegrationError("当前已保存预测应为名义主客基线；中立场须另核验后产生新记录。")
    try:
        recomputed = score_distribution(model, home_id, away_id, row["kickoff"],
                                        **({"neutral": row["neutral"]} if national else {}))
        matrix = np.asarray(prediction.get("score_matrix"), dtype=float)
    except (ValueError, KeyError, TypeError, OverflowError) as exc:
        raise IntegrationError("原函数无法复算本场，或比分矩阵无效。") from exc
    if matrix.ndim != 2 or matrix.size == 0 or not np.all(np.isfinite(matrix)) or np.any(matrix < 0) or not math.isclose(float(matrix.sum()), 1.0, rel_tol=0, abs_tol=1e-10):
        raise IntegrationError("比分矩阵非法或概率未归一。")
    _same(prediction, recomputed)
    # The existing original function checks IDs, unknown teams, connectedness,
    # score axes, probability direction and truncation; exact reproduction also
    # binds all saved metadata and tail accounting to that implementation.
    from .f5c_access import comparison
    f5c_comparison = comparison(runtime, league, match_id, row=row, now=now)
    return {
        "schema_version": 1, "packet_type": "saved_exploratory_forecast_for_framework",
        "created_at": iso(datetime.now(UTC)), "analysis_as_of": iso(now),
        "status": "baseline_verified_external_checks_pending", "competition": competition,
        "fixture": {k: row[k] for k in ("match_id", "kickoff", "home_id", "away_id", "home_name", "away_name") +
                    (("neutral", "venue_evidence") if national else ())},
        "provenance": {"model_id": bundle["model_id"], "model_file": f"runtime/models/{league}.json",
                       "model_file_sha256": _sha(model_path), "prediction_file": attempt["prediction_file"],
                       "prediction_file_sha256": _sha(prediction_path), "report_file": attempt["report_file"],
                       "source_commit": attempt["source_commit"], "state_commit": state_commit,
                       "state_commit_source": "caller_supplied; verify against checkout" if state_commit else "not_supplied",
                       "trained_at": bundle["trained_at"], "training_cutoff": model["training_cutoff"],
                       "training_data_sha256": model["training_data_sha256"],
                       "data_observed_at": row["data_observed_at"], "forecast_generated_at": row["generated_at"],
                       "read_and_recomputed": True, "fit_performed": False,
                       **time_provenance,
                       **venue_registry_provenance},
        "latest_league_attempt": latest,
        "prediction": prediction, "validation": validation,
        "f5c_comparison": f5c_comparison,
        "external_checks": {"current_fixture_status_and_kickoff": "pending", "actual_venue_and_neutral_status": "pending",
                            "team_identity_against_current_schedule": "pending", "lineups_injuries_news": "pending",
                            "current_odds_and_settlement_contract": "pending"},
        "unquantified_factors": ["球员与伤停/预计及实际阵容", "射门事件xG", "战术变化", "旅行与赛程特征", "天气与场地变化", "参数不确定性"],
        "market_mapping": {"status": "not_executed", "instruction": "以框架§8按真实合约映射五态及EV；本包未读取赔率、不计算盘口EV。预测λ不是射门xG。"},
        "usage": "仅为已读取并复算的探索性常规时间比分基线。尚须外部核实本场资料；不能称已纳入全部信息、已验证优势或已自动推荐。90分钟含补时，不含加时与点球大战。",
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description="索引云端已存结果，并生成可追溯单场分析包；不训练模型。")
    sub = parser.add_subparsers(dest="operation", required=True)
    for command in ("index", "match"):
        target = sub.add_parser(command)
        target.add_argument("--runtime", default="runtime")
        target.add_argument("--config", default="config.json")
        if command == "match":
            target.add_argument("--league", required=True)
            target.add_argument("--match-id", required=True, type=int)
            target.add_argument("--as-of")
            target.add_argument("--output")
            target.add_argument("--state-commit")
            target.add_argument("--html", help="Export visible F5-C comparison together with --zip")
            target.add_argument("--zip", help="Export exact F5-C originals together with --html")
    args = parser.parse_args(argv)
    try:
        config = _read(Path(args.config))
        if args.operation == "index":
            result = build_index(args.runtime, config)
            print(f"接入索引已保存：{len(result['competitions'])}个赛事；不代表质量验收。")
        else:
            if bool(args.html) != bool(args.zip):
                raise IntegrationError("--html 与 --zip 必须同时提供。")
            if args.html:
                from .f5c_access import _new_output
                _new_output(args.html, args.runtime)
                _new_output(args.zip, args.runtime)
            result = match_packet(args.runtime, config, args.league, args.match_id, args.as_of, args.state_commit)
            call_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
            output = Path(args.output) if args.output else Path(args.runtime) / "match_calls" / f"{call_id}-{args.league}-{args.match_id}.json"
            atomic_json(output, result)
            if args.html:
                from .f5c_access import export_report
                export_report(result, args.runtime, args.html, args.zip)
            print(f"单场分析包已保存：{output}；原预测复算一致，赛程和场地待外部核验。")
        return 0
    except (ValueError, KeyError, TypeError, OSError, RuntimeError) as exc:
        print(f"接入失败：{safe_error(exc)}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
