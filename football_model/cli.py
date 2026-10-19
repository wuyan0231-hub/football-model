"""Audit actual API data, fit research baselines and export versioned reports."""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone, timedelta
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile

from . import __version__
from .competition_scope import require_competition, select_competitions
from .data import APIClient, APIError, normalize_fixtures, normalize_upcoming
from .model import fit_model, evaluate_time_holdout, score_distribution

UTC = timezone.utc
TIME_SEMANTICS = "observed_wall_clock_v1"


def observed_time(clock, *not_before):
    """Read completion time; a training cutoff is never a completion clock."""
    value = clock()
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("实际完成时钟必须返回含时区的时间。")
    value = value.astimezone(UTC)
    if any(value < moment for moment in not_before if moment is not None):
        raise ValueError("实际完成时间早于已取得的来源或前序步骤；不能倒填时间。")
    return value


def iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def parse_date(value: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError("时间必须是含时区的字符串。")
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("时间必须含时区。")
    return result.astimezone(UTC)


def atomic_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as fp:
        temporary = Path(fp.name)
        fp.write(payload)
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def preserve_model_evidence(runtime: Path, key: str, payload: bytes) -> dict:
    """Keep exact legacy bytes before migration; never rewrite prior evidence."""
    digest = hashlib.sha256(payload).hexdigest()
    relative = Path("model_history") / key / "predecessors" / f"{digest}.json"
    path = runtime / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("wb", dir=path.parent, delete=False) as fp:
            temporary = Path(fp.name)
            fp.write(payload)
        # Publish complete bytes atomically without replacing an existing file.
        try:
            os.link(temporary, path)
        except FileExistsError:
            if path.read_bytes() != payload:
                raise ValueError("旧模型证据副本与其指纹不一致；停止迁移，不能覆盖。")
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return {"path": "runtime/" + relative.as_posix(), "sha256": digest}


def safe_error(error: Exception) -> str:
    text = str(error)
    key = os.environ.get("FOOTBALL_API_KEY", "")
    if key:
        text = text.replace(key, "[REDACTED]")
    return text[:1200]


def select_seasons(metadata: dict, now: datetime, limit: int) -> list[dict]:
    seasons = []
    for season in metadata.get("seasons", []):
        if not isinstance(season.get("year"), int) or isinstance(season.get("year"), bool):
            continue
        start = season.get("start")
        if not isinstance(start, str):
            continue
        try:
            # API season.start is an ISO calendar date, not a fixture timestamp.
            if datetime.fromisoformat(start).date() <= now.date():
                seasons.append(season)
        except ValueError:
            continue
    seasons.sort(key=lambda row: (row["start"], row["year"]), reverse=True)
    if not seasons:
        raise ValueError("没有可识别的已开始赛季；不能猜测联赛年份。")
    seen = set()
    result = []
    for season in seasons:
        if season["year"] not in seen:
            result.append(season)
            seen.add(season["year"])
    return result[:limit]


def input_fingerprint(matches: list, settings: dict, competition: dict) -> str:
    # Including the source code makes a corrected implementation trigger a refit.
    source = hashlib.sha256(Path(__file__).with_name("model.py").read_bytes()).hexdigest()
    payload = {"version": __version__, "source": source, "competition": competition,
               "settings": settings, "matches": matches}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def forecast_availability(rows):
    """Describe saved output coverage, independently of pipeline completion."""
    supported = sum(row.get("status") == "exploratory_baseline" for row in rows)
    unsupported = sum(row.get("status") == "unsupported" for row in rows)
    return {"status": ("no_upcoming" if not rows else "available" if supported == len(rows)
                       else "partial" if supported else "unavailable"),
            "upcoming_count": len(rows), "supported_count": supported,
            "unsupported_count": unsupported,
            "unsupported_reasons": dict(Counter(row.get("reason", "未说明") for row in rows
                                                 if row.get("status") == "unsupported"))}


def availability_summary(items):
    """Run-level observability; a zero exit code is not a coverage guarantee."""
    records = [item.get("forecast_availability", {"status": item.get("status", "unknown")}) for item in items]
    counts = Counter(row["status"] for row in records)
    return {"competition_status_counts": dict(counts),
            "upcoming_count": sum(row.get("upcoming_count") or 0 for row in records),
            "supported_count": sum(row.get("supported_count") or 0 for row in records),
            "unsupported_count": sum(row.get("unsupported_count") or 0 for row in records),
            "unknown_coverage_competitions": sum(row.get("upcoming_count") is None for row in records),
            "usage": "流程成功不代表存在可用预测；空窗、全部不支持与失败分别计数。"}


def markdown_report(report: dict) -> str:
    rows = ["# 足球模型运行报告", "", f"生成时间：{report['generated_at']}（UTC）",
            f"程序版本：{__version__}；运行模式：{report['mode']}。", "",
            "本程序生成探索性历史比分基线。没有球员/伤停、xG、战术、赛程特征，也没有经过验证的收益优势。",
            "数据来源 API-Football；基本面训练不读取赔率或供应商预测。欧冠只做数据审计。", "",
            "|赛事|运行状态|预测可用性|可用/不支持/未来场次|可用90分钟赛果|训练参数变化|说明|", "|---|---|---|---|---:|---|---|"]
    for item in report["competitions"]:
        detail = str(item.get("detail", "")).replace("|", "／").replace("\n", " ")
        coverage = item.get("forecast_availability", {})
        rows.append(f"|{item['label']}|{item['status']}|{coverage.get('status', 'unknown')}|"
                    f"{coverage.get('supported_count', '—')}/{coverage.get('unsupported_count', '—')}/{coverage.get('upcoming_count', '—')}|{item.get('eligible_matches', 0)}|"
                    f"{item.get('model_change', '未训练')}|{detail}|")
    rows.extend(["", "预测可用性：available=全部已保存可用；partial=部分可用；unavailable=有赛程但全部不支持；"
                 "no_upcoming=窗口内无待预测比赛；failed=处理失败。以上不表示准确性或收益验收。"])
    rows.extend(["", f"本次接口网络尝试次数：{report.get('api_requests', 0)}（含状态查询与重试，不等于当日计费已用量）。"])
    budget = report.get("api_budget")
    if isinstance(budget, dict):
        def quota_value(field):
            value = budget.get(field)
            return "未知" if value is None else str(value)

        per_run = budget.get("max_requests_per_run")
        rows.extend(["", "## API额度与限速", "",
                     "|项目|本次记录|", "|---|---|",
                     f"|配置的每日上限|{quota_value('configured_daily_limit')}|",
                     f"|供应商日上限／当日已用（最近观测）|{quota_value('provider_daily_limit')}／{quota_value('provider_daily_used')}|",
                     f"|供应商最近回报余额|{quota_value('daily_remaining_reported')}|",
                     f"|当前有效日余额（保守估计）|{quota_value('daily_remaining')}|",
                     f"|日额证据状态|{quota_value('daily_quota_status')}|",
                     f"|每轮网络次数上限|{'不设额外次数上限' if per_run is None else per_run}|",
                     f"|供应商分钟上限／最近剩余|{quota_value('minute_limit')}／{quota_value('minute_remaining')}|",
                     f"|当前请求最小间隔（秒）|{quota_value('min_interval_seconds')}|",
                     f"|网络请求软截止／已运行（秒）|{quota_value('max_run_seconds')}／{quota_value('elapsed_seconds')}|",
                     f"|额度最近观测时间／来源|{quota_value('quota_observed_at')}／{quota_value('quota_source')}|",
                     "", "每轮不设额外次数上限时，仍遵守已核实的当日额度、后续下载预留、分钟限速及软截止；不把每日上限当作每轮可重新使用的余额。未知额度不填零，也不以配置值冒充已核实余额。", ""])
    else:
        rows.extend(["", "API额度快照不可用；当日已用、剩余和分钟限速未知，不能由配置上限推定。", ""])
    rows.extend(["", "## 怎样阅读", "",
                 "- 已取得数据，不代表该赛事全部历史数据完整；请查看各赛季统计和请求失败项。",
                 "- 已拟合／沿用参数，不代表通过准确性或收益验收。参数和所有概率都是探索性估计。",
                 "- 验证采用按日期划分的时间留出集，未来时段不参与那次拟合；不是完整滚动回测。",
                 "- 训练窗口、半衰期、正则强度采用预先固定默认值，尚未调优；不能据反复查看同一留出集声称独立验收。",
                 "- 历史数据可能包含事后修订；无原始发布时间快照，历史检查不是严格还原当时供应商资料。",
                 "- 正式预测文件保存数据截点、模型版本和未覆盖因素；阵容影响未量化，EV未量化。",
                 "- 国内联赛按名义主客生成条件基线；实际场地和特殊赛制仍须核实。",
                 "- 新队或与训练队伍缺少连接的队伍不出数值；陈旧模型不自动发布新预测。",
                 "- 单项失败会保留旧模型文件，但本次不会把旧文件伪装为新训练，也不会用失败的数据生成预测。", ""])
    if report.get("fatal_error"):
        rows.extend(["## 本次未完成", "", report["fatal_error"], ""])
    if report.get("integration_error"):
        rows.extend(["## 模型接入索引失败", "", report["integration_error"],
                     "", "本次任务标为失败，旧接入索引已失效；不能据旧索引声称已取得最新预测。", ""])
    for item in report["competitions"]:
        rows.extend([f"## {item['label']}", ""])
        for season in item.get("seasons", []):
            rows.append(f"- {season['year']}：返回{season.get('fixtures', 0)}场，"
                        f"抓取时间{season.get('fetched_at', '无')}；"
                        f"{season.get('error', '接口返回成功；完整性尚需核验')}。")
        validation = item.get("validation")
        if validation:
            rows.extend(["", "验证结果（完整字段见同名JSON）：", "", "```json",
                         json.dumps(validation, ensure_ascii=False, indent=2), "```", ""])
    if report.get("research_replay"):
        rows.insert(0, "> 研究回放：所有记录时间来自原运行；不具备新的赛前预测资格。\n")
    if report.get("replay_recording", {}).get("status") == "failed":
        rows.extend(["", "## 回放证据记录失败", "", report["replay_recording"]["detail"],
                     "预测文件保留；本轮不能据此声称可完整复现。"])
    return "\n".join(rows)


def process_competition(client, competition, settings, runtime, now, mode, run_id,
                        venue_reserve_requests=0, clock=None, run_started=None, research_replay=False):
    clock = clock or (lambda: datetime.now(UTC))
    run_started = run_started or observed_time(clock, now)
    key = competition["key"]
    require_competition(competition)
    if competition.get("mode") not in {"domestic_baseline", "audit_only"}:
        raise ValueError("unsupported_competition_mode")
    result = {"key": key, "label": competition["label"], "status": "待检查", "seasons": [],
              "configured": True, "model_change": "未训练", "lineup_model": "未实现",
              "xg_model": "未实现", "return_advantage": "未经验证", "ev": "未量化",
              "time_semantics": TIME_SEMANTICS, "run_started_at": iso(run_started),
              "forecast_availability": {"status": "not_requested", "upcoming_count": None,
                  "supported_count": None, "unsupported_count": None, "unsupported_reasons": {}},
              "model_freshness": {"status": "not_checked"}}
    try:
        metadata = client.resolve_league(competition)
        metadata_received = observed_time(clock, now, run_started)
        result["metadata_received_at"] = iso(metadata_received)
        league = metadata["league"]
        require_competition(competition, league["id"])
        result["provider_league_id"] = league["id"]
        selected = select_seasons(metadata, now, settings["history_seasons"])
        raw = []
        newest_raw = []
        failed_seasons = []
        for index, season in enumerate(selected):
            entry = {"year": season["year"], "coverage_flags": season.get("coverage", {})}
            try:
                # Refresh the latest season even during the off-season; old seasons weekly.
                envelope = client.fetch_fixtures(league["id"], season["year"],
                                                current=(index == 0), force=(index == 0),
                                                max_age_seconds=7 * 86400)
                fetched = parse_date(envelope["fetched_at"])
                received = observed_time(clock, metadata_received, fetched)
                fixtures = envelope["response"]
                if not isinstance(fixtures, list) or not fixtures:
                    raise ValueError("返回空赛程；可能是赛季权限或来源缺口，不能按完整赛季处理。")
                # Never trust a successful HTTP response to identify the requested competition.
                for fixture in fixtures:
                    record_league = fixture.get("league", {})
                    if record_league.get("id") != league["id"] or record_league.get("season") != season["year"]:
                        raise ValueError("返回的联赛ID或赛季与请求不一致。")
                entry.update(fixtures=len(fixtures), fetched_at=envelope["fetched_at"],
                             received_at=iso(received),
                             from_cache=envelope.get("from_cache", False),
                             status_counts=dict(Counter(x.get("fixture", {}).get("status", {}).get("short", "missing") for x in fixtures)))
                raw.extend(fixtures)
                if index == 0:
                    newest_raw = fixtures
            except (APIError, ValueError, KeyError, TypeError) as exc:
                entry["error"] = safe_error(exc)
                failed_seasons.append(season["year"])
            result["seasons"].append(entry)
        matches = normalize_fixtures(raw, iso(now))
        result["eligible_matches"] = len(matches)
        result["missing_seasons"] = failed_seasons
        result["last_match_kickoff"] = max((x["kickoff"] for x in matches), default=None)
        data_observed = observed_time(clock, now, metadata_received,
            *(parse_date(s["received_at"]) for s in result["seasons"] if "received_at" in s))
        result["observed_at"] = result["data_observed_at"] = iso(data_observed)
        atomic_json(runtime / "data" / f"{key}.json", {"schema_version": 2,
                                                       "time_semantics": TIME_SEMANTICS,
                                                       "run_started_at": iso(run_started),
                                                       "observed_at": iso(data_observed), "matches": matches,
                                                       "requested_seasons": [s["year"] for s in selected],
                                                       "missing_seasons": failed_seasons})
        if failed_seasons:
            raise ValueError(f"这些已请求赛季未完整取得：{failed_seasons}。本次不训练；核对套餐或明确缩短训练窗口。")
        if competition["mode"] == "audit_only":
            result["forecast_availability"]["status"] = "audit_only"
            result.update(status="仅审计", detail=("欧冠跨联赛模型尚未实现；不会拼接国内参数生成欧冠概率。" if key == "ucl" else "此赛事先完成独立数据审计，再决定训练适用范围。"))
            return result
        if mode == "audit":
            result.update(status="已取得数据", detail="已检查接口与基本字段；尚未执行训练或准确性验证。")
            return result

        upcoming = normalize_upcoming(newest_raw, iso(now), days=settings["forecast_days"])
        result["forecast_availability"]["upcoming_count"] = len(upcoming)
        knobs = {k: settings[k] for k in ["half_life_days", "ridge", "min_train", "min_test"]}
        fingerprint = input_fingerprint(matches, knobs, competition)
        model_path = runtime / "models" / f"{key}.json"
        old_bytes = model_path.read_bytes() if model_path.exists() else None
        old = json.loads(old_bytes) if old_bytes is not None else None
        refresh_reasons = []
        if old and upcoming:
            previous_cutoff = parse_date(old["trained_at"])
            if (now - previous_cutoff).total_seconds() > settings["max_model_age_days"] * 86400:
                refresh_reasons.append("expired_at_run")
            if any((parse_date(row["kickoff"]) - previous_cutoff).total_seconds() >
                   settings["max_model_age_days"] * 86400 for row in upcoming):
                refresh_reasons.append("expires_before_kickoff")
        refresh_window = bool(refresh_reasons)
        # A legacy cutoff is not evidence of when fitting actually completed.
        # Migrate only when forecasts are needed, through the full original fit
        # and holdout path; never copy today's date into the old model.
        migrate_fit_time = bool(old and upcoming and old.get("model_fitted_at") is None)
        if migrate_fit_time:
            refresh_reasons.append("missing_model_fitted_at")
        data_changed = bool(old and old.get("data_fingerprint") != fingerprint)
        result["model_refresh"] = {"required": bool(not old or data_changed or refresh_window or migrate_fit_time),
            "reasons": (["initial_fit"] if not old else (["training_inputs_changed"] if data_changed else [])) + refresh_reasons,
            "training_inputs_changed": data_changed,
            "data_changed": False if old and not data_changed else None,
            "performed": False, "prior_model_id": (old or {}).get("model_id")}
        if migrate_fit_time:
            result["model_refresh"]["time_evidence_migration"] = {"status": "required",
                "policy": "refit_legacy_model_when_upcoming_v1", "stage": "preserve_prior_model"}
        if old and old.get("data_fingerprint") == fingerprint and not refresh_window and not migrate_fit_time:
            bundle = old
            result["model_change"] = "数据未变，沿用参数"
        else:
            migration = result["model_refresh"].get("time_evidence_migration")
            if migration is not None:
                migration["prior_model_evidence"] = preserve_model_evidence(runtime, key, old_bytes)
                migration["stage"] = "evaluate_time_holdout"
            previous_validation = None
            previous_source = {"competition": (old or {}).get("competition", {}).get("key"),
                               "target_scope": ("regulation_time_90_plus_stoppage" if old and
                                    "score.fulltime (regulation time incl. stoppage time)" in old.get("source", "") else None),
                               "model_id": old.get("model_id") if old else None, "path": None}
            # An unchanged forecast run is not necessarily the training run.
            # Pair the old validation with its model ID before writing new data.
            if old and isinstance(old.get("model_id"), str) and old["model_id"].startswith(key + "-"):
                old_run = old["model_id"][len(key) + 1:]
                if old_run and all(c.isalnum() or c in "_-" for c in old_run):
                    old_validation_path = runtime / "validation" / key / f"{old_run}.json"
                    previous_source["path"] = f"runtime/validation/{key}/{old_run}.json"
                    try:
                        previous_validation = json.loads(old_validation_path.read_text(encoding="utf-8"))
                    except (OSError, ValueError):
                        pass
            evaluation = evaluate_time_holdout(matches, iso(now), **knobs)
            if not evaluation["report"]["meets_minimum_supported_test_matches"]:
                raise ValueError("留出期已覆盖比赛不足；保留旧模型，不发布新的球队参数。")
            if migration is not None:
                migration["stage"] = "fit_model"
            fitted = fit_model(matches, iso(now), **{k: v for k, v in knobs.items() if k != "min_test"})
            fitted_at = observed_time(clock, data_observed)
            if fitted["optimizer"]["parameter_at_bound"]:
                raise ValueError("拟合参数触及预设优化边界，须检查数据；本次不发布新模型。")
            bundle = {"schema_version": 2, "time_semantics": TIME_SEMANTICS,
                      "run_started_at": iso(run_started), "training_cutoff": iso(now),
                      "data_observed_at": iso(data_observed), "model_fitted_at": iso(fitted_at),
                      "program_version": __version__, "model_id": f"{key}-{run_id}",
                      "competition": competition, "data_fingerprint": fingerprint,
                      "trained_at": iso(now), "source": "API-Football FT/AET/PEN score.fulltime (regulation time incl. stoppage time)",
                      "maturity": "exploratory_baseline", "lineup_effects": "未量化", "ev": "未量化",
                      "model": fitted, "validation": evaluation["report"],
                      "source_seasons": result["seasons"]}
            if migration is not None:
                bundle["time_evidence_migration"] = {"policy": migration["policy"],
                    "reason": "missing_model_fitted_at", "prior_model_id": old.get("model_id"),
                    "prior_model_fitted_at": None,
                    "prior_model_evidence": migration["prior_model_evidence"],
                    "new_model_fitted_at": iso(fitted_at)}
                migration["stage"] = "save_model_and_validation"
            from .coverage_audit import compare_validation_coverage
            coverage = compare_validation_coverage(previous_validation, evaluation,
                old_source=previous_source,
                new_source={"competition": key, "target_scope": "regulation_time_90_plus_stoppage",
                            "model_id": bundle["model_id"], "path": f"runtime/validation/{key}/{run_id}.json"},
                old_temporal_audit=None, new_temporal_audit=None)
            coverage_path = f"runtime/audits/{key}/{run_id}-coverage.json"
            atomic_json(runtime / "audits" / key / f"{run_id}-coverage.json", coverage)
            result["coverage_audit"] = {"path": coverage_path, "status": coverage["status"],
                                        "counts": coverage["counts"],
                                        "comparison_eligibility": coverage["comparison_eligibility"],
                                        "prospective_increment_validated": False}
            # Save immutable version first; latest changes only after a successful complete fit.
            atomic_json(runtime / "model_history" / key / f"{run_id}.json", bundle)
            atomic_json(runtime / "validation" / key / f"{run_id}.json", evaluation)
            atomic_json(model_path, bundle)
            result["model_refresh"]["performed"] = True
            if migration is not None:
                migration.update(status="completed", stage="completed", model_fitted_at=iso(fitted_at))
            prior_data_hash = (old or {}).get("model", {}).get("training_data_sha256")
            result["model_refresh"]["data_changed"] = (prior_data_hash != fitted["training_data_sha256"]
                                                        if prior_data_hash else None)
            result["model_change"] = ("旧拟合时间证据缺失，已实际重新拟合" if migrate_fit_time else
                                      "按真实当前截点重拟合预测窗口" if refresh_window else "完成新拟合")
        result["validation"] = bundle["validation"]
        result["model_id"] = bundle["model_id"]
        result["trained_at"] = bundle["trained_at"]
        result["training_cutoff"] = bundle["model"]["training_cutoff"]
        result["model_fitted_at"] = bundle.get("model_fitted_at")
        result.update(status="探索性基线", detail="未含阵容伤停、xG与赛程；未证实预测或收益优势。")
        age = (now - parse_date(bundle["trained_at"])).total_seconds() / 86400
        result["model_freshness"] = {"status": "stale" if age > settings["max_model_age_days"] else "current",
            "evaluated_at": iso(now), "age_basis": "training_cutoff",
            "age_days": age, "max_age_days": settings["max_model_age_days"],
            "latest_upcoming_model_age_days": max(((parse_date(row["kickoff"]) - parse_date(bundle["trained_at"])).total_seconds() / 86400
                                                    for row in upcoming), default=None),
            "training_last_match_kickoff": bundle["model"].get("last_match_kickoff"),
            "training_data_age_days": ((now - parse_date(bundle["model"]["last_match_kickoff"])).total_seconds() / 86400
                                       if bundle["model"].get("last_match_kickoff") else None),
            "note": "真实重拟合可沿用相同历史样本；不代表取得新赛果、阵容或其他基本面。"}
        if age > settings["max_model_age_days"]:
            result.update(status="模型陈旧", detail="旧模型超过有效期；空窗不为更新时间戳而重训，本次不发布预测。")
            if not upcoming:
                result.update(forecast_count=0, unsupported_forecasts=0,
                              forecast_availability=forecast_availability([]))
            else:
                result["forecast_availability"]["status"] = "unavailable"
            return result
        forecasts = []
        for fixture in upcoming:
            row = {**fixture, "schema_version": 2, "time_semantics": TIME_SEMANTICS,
                   "run_started_at": iso(run_started), "competition": key, "model_id": bundle["model_id"],
                   "trained_at": bundle["trained_at"], "training_cutoff": bundle["model"]["training_cutoff"],
                   "model_fitted_at": bundle.get("model_fitted_at"),
                   "model_time_evidence": (TIME_SEMANTICS if bundle.get("time_semantics") == TIME_SEMANTICS
                                           else "legacy_time_evidence_limited"),
                   "data_observed_at": iso(data_observed),
                   "lineup_adjustment": "未量化", "ev": "未量化", "venue_status": "名义主客；实际场地未核实",
                   "usage": "探索性条件基线；不能据此宣称已纳入本场全部信息"}
            try:
                if (parse_date(fixture["kickoff"]) - parse_date(bundle["trained_at"])).total_seconds() / 86400 > settings["max_model_age_days"]:
                    raise ValueError("预计开赛时模型将超过配置的有效天数；须在真实新截点重新拟合，不能仅刷新时间戳。")
                row["prediction"] = score_distribution(bundle["model"], fixture["home_id"],
                                                         fixture["away_id"], fixture["kickoff"])
                row["status"] = "exploratory_baseline"
            except (ValueError, KeyError, TypeError) as exc:
                row.update(status="unsupported", reason=safe_error(exc))
            completed = observed_time(clock, data_observed,
                parse_date(bundle["model_fitted_at"]) if bundle.get("model_fitted_at") else None)
            row["generated_at"] = row["forecast_generated_at"] = iso(completed)
            if completed >= parse_date(fixture["kickoff"]):
                row.pop("prediction", None)
                row.update(status="unsupported", reason="预测实际完成时比赛已开赛；不能倒填赛前预测。")
            if research_replay:
                row["research_replay"] = True
            forecasts.append(row)
        result["forecast_generated_at"] = iso(observed_time(clock, data_observed,
            *(parse_date(r["generated_at"]) for r in forecasts)))
        result["forecast_count"] = sum(row["status"] == "exploratory_baseline" for row in forecasts)
        result["unsupported_forecasts"] = sum(row["status"] == "unsupported" for row in forecasts)
        result["forecast_availability"] = forecast_availability(forecasts)
        atomic_json(runtime / "predictions" / run_id / f"{key}.json", forecasts)
    except (APIError, ValueError, KeyError, TypeError, RuntimeError, OSError) as exc:
        result.update(status="未完成", detail=safe_error(exc))
        migration = result.get("model_refresh", {}).get("time_evidence_migration")
        if migration is not None and migration["status"] != "completed":
            migration.update(status="failed", failure=safe_error(exc))
        result["forecast_availability"].update(status="failed", supported_count=None,
                                               unsupported_count=None, unsupported_reasons={})
    return result


def run_pipeline(config, runtime, mode="audit", league="epl", now=None, client=None, *, clock=None,
                 record_replay=False, research_replay=False):
    clock = clock or (lambda: datetime.now(UTC))
    capture_clock = clock  # Keep ledger side effects out of production replay clock events.
    recorder, recording_error = None, None
    if record_replay and not research_replay:
        try:
            from .production_replay import Recorder
            recorder = Recorder(config, runtime, mode, league, clock, now)
            clock = recorder.clock
        except (ValueError, KeyError, TypeError, OSError) as exc:
            recording_error = safe_error(exc)
    run_started = observed_time(clock)
    now = now or run_started
    if now.tzinfo is None:
        raise ValueError("运行/训练截点必须含时区。")
    now = now.astimezone(UTC)
    runtime = Path(runtime)
    run_id = run_started.strftime("%Y%m%dT%H%M%S%fZ")
    if ((runtime / "reports" / "history" / f"{run_id}.json").exists()
            or (runtime / "predictions" / run_id).exists()
            or any((runtime / "model_history").glob(f"*/{run_id}.json"))):
        raise ValueError("运行ID已存在；旧运行及预测不可覆盖，请使用新的实际运行截点。")
    chosen = select_competitions(config, league)
    if not chosen:
        raise ValueError(f"未配置赛事：{league}")
    if mode not in {"audit", "train"}:
        raise ValueError("mode 必须为 audit 或 train。")
    if not 1 <= config["history_seasons"] <= 10:
        raise ValueError("history_seasons 必须为 1 到 10。")
    report = {"schema_version": 2, "time_semantics": TIME_SEMANTICS,
              "run_started_at": iso(run_started), "training_cutoff": iso(now), "run_id": run_id, "mode": mode,
              "program_version": __version__, "source_commit": os.environ.get("GITHUB_SHA"),
              "requested_competitions": [c["key"] for c in chosen],
              "competitions": [], "api_requests": 0, "api_budget": None}
    if research_replay:
        report["research_replay"] = True
        report["prospective_eligible"] = False
    exit_code = 0
    try:
        client = client or APIClient(runtime / "cache",
                                    max_requests=config.get("max_requests_per_run"),
                                    daily_limit=config.get("api_daily_limit"),
                                    max_run_seconds=config.get("api_max_run_seconds"))
        if recorder is not None:
            client = recorder.client(client)
        client.status()  # Confirm key validity first; do not save account identifiers or credentials.
        for position, competition in enumerate(chosen):
            # Venue enrichment must not consume the ordinary data-fetch budget
            # reserved for later competitions in the same all-league run.
            reserve = (len(chosen) - position - 1) * (1 + config["history_seasons"])
            item = process_competition(client, competition, config, runtime, now, mode,
                                       run_id, venue_reserve_requests=reserve, clock=clock, run_started=run_started,
                                       research_replay=research_replay)
            report["competitions"].append(item)
            if mode == "train" and not research_replay:
                # Final all-league reporting can happen after this match kicks off.
                # Freeze now, before any later league/API work, from an immutable
                # partial report linked to the eventual final report by run_id.
                try:
                    from .prospective import capture_report
                    checkpoint = {key: report[key] for key in (
                        "schema_version", "time_semantics", "run_started_at", "training_cutoff",
                        "run_id", "mode", "program_version", "source_commit")}
                    checkpoint.update(requested_competitions=[competition["key"]], competitions=[item],
                        generated_at=iso(observed_time(capture_clock)),
                        report_scope="completed_competition_checkpoint")
                    captured = capture_report(runtime, config, checkpoint, clock=capture_clock)
                    report.setdefault("prospective_captures", []).append(captured)
                    if captured["exit_code"]:
                        exit_code = max(exit_code, 1)
                except (ValueError, KeyError, TypeError, RuntimeError, OSError) as exc:
                    report.setdefault("prospective_captures", []).append({"competitions": [competition["key"]],
                        "exit_code": 1, "errors": [safe_error(exc)], "status": "failed"})
                    exit_code = max(exit_code, 1)
            print(f"{item['label']}：{item['status']}；{item.get('eligible_matches', 0)}场可用赛果。", flush=True)
            if item["status"] == "未完成":
                exit_code = 1
    except (APIError, ValueError, RuntimeError, OSError) as exc:
        report["fatal_error"] = safe_error(exc)
        exit_code = 2
    report["api_requests"] = getattr(client, "requests_made", 0)
    budget_snapshot = getattr(client, "budget_snapshot", None)
    if callable(budget_snapshot):
        report["api_budget"] = budget_snapshot()
    report["exit_code"] = exit_code
    report["forecast_availability_summary"] = availability_summary(report["competitions"])
    report["forecast_availability_summary"]["requested_competitions"] = len(chosen)
    report["forecast_availability_summary"]["unreported_competitions"] = [
        c["key"] for c in chosen if c["key"] not in {item["key"] for item in report["competitions"]}]
    report["generated_at"] = iso(observed_time(clock, now,
        *(parse_date(item["forecast_generated_at"]) for item in report["competitions"]
          if item.get("forecast_generated_at"))))
    # Index all saved league runs, not only this run's possibly single-league
    # report. This access layer never changes fitting or validation inputs.
    from .integration import build_index
    try:
        if research_replay:
            report["integration_index"] = {"status": "research_replay_not_published"}
        else:
            build_index(runtime, config, current_report=report)
            report["integration_index"] = {"status": "updated", "path": "runtime/integration/index.json"}
    except (ValueError, KeyError, TypeError, RuntimeError, OSError) as exc:
        report["integration_index"] = {"status": "failed", "detail": safe_error(exc)}
        report["integration_error"] = safe_error(exc)
        exit_code = max(exit_code, 1)
        report["exit_code"] = exit_code
        invalid_index = runtime / "integration" / "index.json"
        try:
            atomic_json(invalid_index, {"schema_version": 1, "status": "failed", "run_id": run_id,
                        "generated_at": report["generated_at"], "reason": safe_error(exc), "competitions": []})
        except OSError:
            invalid_index.unlink(missing_ok=True)
        invalid_markdown = runtime / "integration" / "index.md"
        try:
            invalid_markdown.parent.mkdir(parents=True, exist_ok=True)
            invalid_markdown.write_text(
                f"# 模型接入索引失败\n\n运行ID：{run_id}\n\n时间：{report['generated_at']}\n\n"
                f"原因：{safe_error(exc)}\n\n旧索引已失效，不能据此调用旧预测。\n",
                encoding="utf-8",
            )
        except OSError:
            invalid_markdown.unlink(missing_ok=True)
        print(f"模型接入索引更新失败：{safe_error(exc)}", flush=True)
    if recorder is not None:
        try:
            if client is None:
                raise ValueError("真实API客户端未建立；本轮无法冻结完整API行为，原失败报告保留。")
            report["replay_recording"] = recorder.save(report)
        except (ValueError, KeyError, TypeError, OSError) as exc:
            recording_error = safe_error(exc)
    if recording_error:
        report["replay_recording"] = {"status": "failed", "detail": recording_error,
                                      "forecast_files_preserved": True}
    atomic_json(runtime / "reports" / "history" / f"{run_id}.json", report)
    atomic_json(runtime / "reports" / "latest.json", report)
    markdown = markdown_report(report)
    (runtime / "reports" / "latest.md").write_text(markdown, encoding="utf-8")
    print(f"报告：{runtime / 'reports' / 'latest.md'}", flush=True)
    return exit_code, report


def main(argv=None):
    parser = argparse.ArgumentParser(description="足球模型：真实数据审计与探索性自动训练")
    parser.add_argument("--mode", choices=["audit", "train"], default="audit")
    parser.add_argument("--league", default="epl", help="config.json 中赛事key，或 all")
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--runtime", default="runtime")
    parser.add_argument("--record-replay", action="store_true", help="冻结无敏感API响应、初始状态与时钟，供严格离线研究回放")
    args = parser.parse_args(argv)
    with open(args.config, encoding="utf-8") as fp:
        config = json.load(fp)
    return run_pipeline(config, args.runtime, args.mode, args.league, record_replay=args.record_replay)[0]


if __name__ == "__main__":
    sys.exit(main())
