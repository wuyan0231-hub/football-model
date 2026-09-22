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
from .data import APIClient, APIError, normalize_fixtures, normalize_upcoming
from .model import fit_model, evaluate_time_holdout, score_distribution

UTC = timezone.utc


def iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def parse_date(value: str) -> datetime:
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


def markdown_report(report: dict) -> str:
    rows = ["# 足球模型运行报告", "", f"生成时间：{report['generated_at']}（UTC）",
            f"程序版本：{__version__}；运行模式：{report['mode']}。", "",
            "本程序生成探索性历史比分基线。没有球员/伤停、xG、战术、赛程特征，也没有经过验证的收益优势。",
            "数据来源 API-Football；基本面训练不读取赔率或供应商预测。欧冠只做数据审计。", "",
            "|赛事|状态|可用90分钟赛果|训练参数变化|说明|", "|---|---|---:|---|---|"]
    for item in report["competitions"]:
        detail = str(item.get("detail", "")).replace("|", "／").replace("\n", " ")
        rows.append(f"|{item['label']}|{item['status']}|{item.get('eligible_matches', 0)}|"
                    f"{item.get('model_change', '未训练')}|{detail}|")
    rows.extend(["", f"本次接口请求次数：{report.get('api_requests', 0)}。",
                 "", "## 怎样阅读", "",
                 "- 已取得数据，不代表该赛事全部历史数据完整；请查看各赛季统计和请求失败项。",
                 "- 已拟合／沿用参数，不代表通过准确性或收益验收。参数和所有概率都是探索性估计。",
                 "- 验证采用按日期划分的时间留出集，未来时段不参与那次拟合；不是完整滚动回测。",
                 "- 训练窗口、半衰期、正则强度采用预先固定默认值，尚未调优；不能据反复查看同一留出集声称独立验收。",
                 "- 历史数据可能包含事后修订；无原始发布时间快照，历史检查不是严格还原当时供应商资料。",
                 "- 正式预测文件保存数据截点、模型版本和未覆盖因素；阵容影响未量化，EV未量化。",
                 "- 中立场/特殊场地和赛制尚未自动识别，预测仅为按名义主客生成的条件基线，使用前必须核实。",
                 "- 新队或与训练队伍缺少连接的队伍不出数值；陈旧模型不自动发布新预测。",
                 "- 单项失败会保留旧模型文件，但本次不会把旧文件伪装为新训练，也不会用失败的数据生成预测。", ""])
    if report.get("fatal_error"):
        rows.extend(["## 本次未完成", "", report["fatal_error"], ""])
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
    return "\n".join(rows)


def process_competition(client, competition, settings, runtime, now, mode, run_id):
    key = competition["key"]
    result = {"key": key, "label": competition["label"], "status": "待检查", "seasons": [],
              "configured": True, "model_change": "未训练", "lineup_model": "未实现",
              "xg_model": "未实现", "return_advantage": "未经验证", "ev": "未量化"}
    try:
        metadata = client.resolve_league(competition)
        league = metadata["league"]
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
                fixtures = envelope["response"]
                if not isinstance(fixtures, list) or not fixtures:
                    raise ValueError("返回空赛程；可能是赛季权限或来源缺口，不能按完整赛季处理。")
                # Never trust a successful HTTP response to identify the requested competition.
                for fixture in fixtures:
                    record_league = fixture.get("league", {})
                    if record_league.get("id") != league["id"] or record_league.get("season") != season["year"]:
                        raise ValueError("返回的联赛ID或赛季与请求不一致。")
                entry.update(fixtures=len(fixtures), fetched_at=envelope["fetched_at"],
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
        result["observed_at"] = iso(now)
        atomic_json(runtime / "data" / f"{key}.json", {"observed_at": iso(now), "matches": matches,
                                                       "requested_seasons": [s["year"] for s in selected],
                                                       "missing_seasons": failed_seasons})
        if failed_seasons:
            raise ValueError(f"这些已请求赛季未完整取得：{failed_seasons}。本次不训练；核对套餐或明确缩短训练窗口。")
        if competition["mode"] == "audit_only":
            result.update(status="仅审计", detail="欧冠跨联赛模型尚未实现；不会拼接国内参数生成欧冠概率。")
            return result
        if mode == "audit":
            result.update(status="已取得数据", detail="已检查接口与基本字段；尚未执行训练或准确性验证。")
            return result

        knobs = {k: settings[k] for k in ["half_life_days", "ridge", "min_train", "min_test"]}
        fingerprint = input_fingerprint(matches, knobs, competition)
        model_path = runtime / "models" / f"{key}.json"
        old = json.loads(model_path.read_text(encoding="utf-8")) if model_path.exists() else None
        if old and old.get("data_fingerprint") == fingerprint:
            bundle = old
            result["model_change"] = "数据未变，沿用参数"
        else:
            evaluation = evaluate_time_holdout(matches, iso(now), **knobs)
            if not evaluation["report"]["meets_minimum_supported_test_matches"]:
                raise ValueError("留出期已覆盖比赛不足；保留旧模型，不发布新的球队参数。")
            fitted = fit_model(matches, iso(now), **{k: v for k, v in knobs.items() if k != "min_test"})
            if fitted["optimizer"]["parameter_at_bound"]:
                raise ValueError("拟合参数触及预设优化边界，须检查数据；本次不发布新模型。")
            bundle = {"schema_version": 1, "program_version": __version__, "model_id": f"{key}-{run_id}",
                      "competition": competition, "data_fingerprint": fingerprint,
                      "trained_at": iso(now), "source": "API-Football FT/AET/PEN score.fulltime (regulation time incl. stoppage time)",
                      "maturity": "exploratory_baseline", "lineup_effects": "未量化", "ev": "未量化",
                      "model": fitted, "validation": evaluation["report"],
                      "source_seasons": result["seasons"]}
            # Save immutable version first; latest changes only after a successful complete fit.
            atomic_json(runtime / "model_history" / key / f"{run_id}.json", bundle)
            atomic_json(runtime / "validation" / key / f"{run_id}.json", evaluation)
            atomic_json(model_path, bundle)
            result["model_change"] = "完成新拟合"
        result["validation"] = bundle["validation"]
        result["model_id"] = bundle["model_id"]
        result["trained_at"] = bundle["trained_at"]
        result.update(status="探索性基线", detail="未含阵容伤停、xG与赛程；未证实预测或收益优势。")
        age = (now - parse_date(bundle["trained_at"])).total_seconds() / 86400
        if age > settings["max_model_age_days"]:
            result.update(status="模型陈旧", detail="没有新赛果可更新且旧模型超过有效期；本次不发布预测。")
            return result
        upcoming = normalize_upcoming(newest_raw, iso(now), days=settings["forecast_days"])
        forecasts = []
        for fixture in upcoming:
            row = {**fixture, "competition": key, "generated_at": iso(now), "model_id": bundle["model_id"],
                   "trained_at": bundle["trained_at"], "data_observed_at": iso(now),
                   "lineup_adjustment": "未量化", "ev": "未量化", "venue_status": "名义主客；实际场地未核实",
                   "usage": "探索性条件基线；不能据此宣称已纳入本场全部信息"}
            try:
                if (parse_date(fixture["kickoff"]) - parse_date(bundle["trained_at"])).total_seconds() / 86400 > settings["max_model_age_days"]:
                    raise ValueError("预计开赛时模型将超过配置的有效天数；请临近比赛后重新更新。")
                row["prediction"] = score_distribution(bundle["model"], fixture["home_id"],
                                                         fixture["away_id"], fixture["kickoff"])
                row["status"] = "exploratory_baseline"
            except (ValueError, KeyError, TypeError) as exc:
                row.update(status="unsupported", reason=safe_error(exc))
            forecasts.append(row)
        result["forecast_count"] = sum(row["status"] == "exploratory_baseline" for row in forecasts)
        result["unsupported_forecasts"] = sum(row["status"] == "unsupported" for row in forecasts)
        atomic_json(runtime / "predictions" / run_id / f"{key}.json", forecasts)
    except (APIError, ValueError, KeyError, TypeError, RuntimeError, OSError) as exc:
        result.update(status="未完成", detail=safe_error(exc))
    return result


def run_pipeline(config, runtime, mode="audit", league="epl", now=None, client=None):
    now = now or datetime.now(UTC)
    runtime = Path(runtime)
    run_id = now.strftime("%Y%m%dT%H%M%S%fZ")
    chosen = [c for c in config["competitions"] if league == "all" or c["key"] == league]
    if not chosen:
        raise ValueError(f"未配置赛事：{league}")
    if mode not in {"audit", "train"}:
        raise ValueError("mode 必须为 audit 或 train。")
    if not 1 <= config["history_seasons"] <= 10:
        raise ValueError("history_seasons 必须为 1 到 10。")
    report = {"generated_at": iso(now), "run_id": run_id, "mode": mode,
              "program_version": __version__, "source_commit": os.environ.get("GITHUB_SHA"),
              "competitions": [], "api_requests": 0}
    exit_code = 0
    try:
        client = client or APIClient(runtime / "cache", max_requests=config["max_requests_per_run"])
        client.status()  # Confirm key validity first; do not save account identifiers or credentials.
        for competition in chosen:
            item = process_competition(client, competition, config, runtime, now, mode, run_id)
            report["competitions"].append(item)
            print(f"{item['label']}：{item['status']}；{item.get('eligible_matches', 0)}场可用赛果。", flush=True)
            if item["status"] == "未完成":
                exit_code = 1
    except (APIError, ValueError, RuntimeError, OSError) as exc:
        report["fatal_error"] = safe_error(exc)
        exit_code = 2
    report["api_requests"] = getattr(client, "requests_made", 0)
    report["exit_code"] = exit_code
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
    args = parser.parse_args(argv)
    with open(args.config, encoding="utf-8") as fp:
        config = json.load(fp)
    return run_pipeline(config, args.runtime, args.mode, args.league)[0]


if __name__ == "__main__":
    sys.exit(main())
