"""Pipeline integration uses synthetic fixtures only, never a real API account."""
import copy
from datetime import datetime, timedelta, timezone
import json
import re
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

from football_model.cli import run_pipeline, select_seasons, iso
from football_model.data import APIError, RequestBudgetError


NOW = datetime(2025, 1, 1, tzinfo=timezone.utc)
BASE_CONFIG = {
    "history_seasons": 1, "max_requests_per_run": 80,
    "half_life_days": 180, "ridge": 3, "min_train": 60, "min_test": 15,
    "forecast_days": 7, "max_model_age_days": 14,
    "competitions": [{"key": "epl", "label": "合成测试联赛",
                      "name": "Synthetic League", "country": "Test",
                      "mode": "domestic_baseline"}]
}


def fixture(index, kickoff, home, away, gh, ga, status="FT"):
    return {"fixture": {"id": index, "date": iso(kickoff), "timestamp": int(kickoff.timestamp()),
                        "status": {"short": status}},
            "league": {"id": 999, "season": 2024},
            "teams": {"home": {"id": home, "name": f"Synthetic {home}"},
                      "away": {"id": away, "name": f"Synthetic {away}"}},
            "score": {"fulltime": {"home": gh, "away": ga}}}


class FakeClient:
    def __init__(self, failing=False):
        self.requests_made = 0
        self.failing = failing
        self.fetch_calls = []
        rng = np.random.default_rng(726)
        self.fixtures = []
        for i in range(180):
            home = i % 6 + 1
            away = (home + 1 + (i // 6) % 5 - 1) % 6 + 1
            self.fixtures.append(fixture(i + 1, NOW - timedelta(days=190-i), home, away,
                                         int(rng.poisson(1.6)), int(rng.poisson(1.1))))
        self.fixtures.append(fixture(2000, NOW + timedelta(days=2), 1, 2, None, None, "NS"))

    def status(self):
        self.requests_made += 1
        return {"requests": {"current": 1, "limit_day": 100}}

    def resolve_league(self, competition):
        self.requests_made += 1
        return {"league": {"id": 999, "name": competition["name"]},
                "seasons": [{"year": 2024, "start": "2024-01-01", "end": "2025-06-01", "coverage": {}}]}

    def fetch_fixtures(self, league_id, season, **kwargs):
        self.requests_made += 1
        self.fetch_calls.append(kwargs)
        if self.failing:
            raise APIError("测试：赛季权限不足")
        return {"response": copy.deepcopy(self.fixtures), "fetched_at": iso(NOW), "from_cache": False}


class BudgetClient(FakeClient):
    """Report the same budget contract without an account or real network."""

    def budget_snapshot(self):
        return {"configured_daily_limit": 5000, "provider_daily_limit": 5000,
                "provider_daily_used": 4988, "daily_remaining": 10,
                "daily_remaining_reported": 12, "minute_limit": 60,
                "minute_remaining": 58, "max_requests_per_run": None,
                "requests_made": self.requests_made, "min_interval_seconds": 1.0,
                "max_run_seconds": 3600, "elapsed_seconds": 3.0,
                "quota_observed_at": iso(NOW), "quota_source": "status"}


class PipelineTests(unittest.TestCase):
    def test_segunda_single_run_keeps_laliga_model_independent(self):
        production = json.loads((Path(__file__).parents[1] / "config.json").read_text())
        competition = next(c for c in production["competitions"] if c["key"] == "segunda")
        config = copy.deepcopy(BASE_CONFIG)
        config["competitions"].append(competition)
        with tempfile.TemporaryDirectory() as tmp:
            existing = Path(tmp) / "models" / "laliga.json"
            existing.parent.mkdir()
            original = b'{"sentinel":"existing La Liga model"}'
            existing.write_bytes(original)
            code, report = run_pipeline(config, tmp, "train", league="segunda",
                                        now=NOW, clock=lambda: NOW, client=FakeClient())
            self.assertEqual(code, 0, report)
            self.assertEqual(report["requested_competitions"], ["segunda"])
            self.assertEqual(report["competitions"][0]["forecast_count"], 1)
            model = json.loads((Path(tmp) / "models" / "segunda.json").read_text())
            self.assertEqual(model["competition"], competition)
            self.assertEqual(existing.read_bytes(), original)
            self.assertFalse((Path(tmp) / "models" / "epl.json").exists())

    def test_real_config_all_selection_and_workflow_menu_include_segunda(self):
        root = Path(__file__).parents[1]
        config = json.loads((root / "config.json").read_text())
        workflow = (root / ".github/workflows/football.yml").read_text()
        keys = [c["key"] for c in config["competitions"]]
        self.assertEqual(len(keys), len(set(keys)))
        menus = re.findall(r"options: \[([^\]]+)\]", workflow)
        league_menu = next([k.strip() for k in line.split(",")] for line in menus if "all" in line)
        self.assertEqual(set(league_menu), set(keys) | {"all"})
        self.assertIn("github.event_name == 'schedule' && 'all'", workflow)
        self.assertIn("cron: '17 1 * * *'", workflow)
        def audit_stub(client, competition, *args, **kwargs):
            return {**competition, "status": "仅审计"}
        with tempfile.TemporaryDirectory() as tmp, patch(
                "football_model.cli.process_competition", side_effect=audit_stub) as process:
            code, report = run_pipeline(config, tmp, "audit", league="all",
                                        now=NOW, clock=lambda: NOW, client=FakeClient())
            self.assertEqual(code, 0, report)
            self.assertEqual(report["requested_competitions"], keys)
            self.assertEqual([call.args[1]["key"] for call in process.call_args_list].count("segunda"), 1)

    def test_actual_fit_and_unchanged_data_reuses_same_parameters(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = FakeClient()
            code, report = run_pipeline(BASE_CONFIG, tmp, "train", now=NOW, clock=lambda: NOW, client=client)
            self.assertEqual(code, 0, report)
            item = report["competitions"][0]
            self.assertEqual(item["model_change"], "完成新拟合")
            self.assertEqual(item["forecast_count"], 1)
            self.assertTrue(client.fetch_calls[0]["force"])
            model_path = Path(tmp)/"models/epl.json"
            before = model_path.read_bytes()
            changed_budget = {**BASE_CONFIG, "max_requests_per_run": None,
                              "api_daily_limit": 5000, "api_max_run_seconds": 3600}
            with patch("football_model.cli.fit_model", side_effect=AssertionError("不应重训")):
                code, second = run_pipeline(changed_budget, tmp, "train", now=NOW+timedelta(hours=2), clock=lambda: NOW+timedelta(hours=2), client=FakeClient())
            self.assertEqual(code, 0, second)
            self.assertEqual(model_path.read_bytes(), before)
            self.assertEqual(second["competitions"][0]["model_change"], "数据未变，沿用参数")
            self.assertEqual(len(list((Path(tmp)/"reports/history").glob("*.json"))), 2)

    def test_failed_download_retains_model_without_new_forecast(self):
        with tempfile.TemporaryDirectory() as tmp:
            model_path = Path(tmp)/"models/epl.json"
            model_path.parent.mkdir()
            model_path.write_text('{"sentinel":"existing model"}', encoding="utf-8")
            code, report = run_pipeline(BASE_CONFIG, tmp, "train", now=NOW, clock=lambda: NOW, client=FakeClient(True))
            self.assertEqual(code, 1)
            self.assertEqual(report["competitions"][0]["status"], "未完成")
            self.assertEqual(json.loads(model_path.read_text())["sentinel"], "existing model")
            self.assertFalse((Path(tmp)/"predictions").exists())

    def test_ucl_stays_audit_only_even_in_train_mode(self):
        config = copy.deepcopy(BASE_CONFIG)
        config["competitions"][0].update(key="ucl", label="欧冠", mode="audit_only")
        with tempfile.TemporaryDirectory() as tmp, patch("football_model.cli.fit_model", side_effect=AssertionError("欧冠不能训练")):
            code, report = run_pipeline(config, tmp, "train", league="all", now=NOW, clock=lambda: NOW, client=FakeClient())
            self.assertEqual(code, 0)
            self.assertEqual(report["competitions"][0]["status"], "仅审计")
            self.assertFalse((Path(tmp)/"models").exists())

    def test_missing_regulation_score_retains_model_without_new_forecast(self):
        client = FakeClient()
        invalid = client.fixtures[0]
        invalid["fixture"]["status"]["short"] = "AET"
        invalid["score"]["fulltime"] = {"home": None, "away": None}
        invalid["score"]["extratime"] = {"home": 2, "away": 1}
        invalid["goals"] = {"home": 2, "away": 1}
        with tempfile.TemporaryDirectory() as tmp:
            model_path = Path(tmp)/"models/epl.json"
            model_path.parent.mkdir()
            model_path.write_text('{"sentinel":"existing model"}', encoding="utf-8")
            code, report = run_pipeline(BASE_CONFIG, tmp, "train", now=NOW, clock=lambda: NOW, client=client)
            self.assertEqual(code, 1)
            item = report["competitions"][0]
            self.assertEqual(item["status"], "未完成")
            self.assertIn("match_id=1 status=AET", item["detail"])
            self.assertEqual(json.loads(model_path.read_text())["sentinel"], "existing model")
            self.assertFalse((Path(tmp)/"predictions").exists())

    def test_audit_does_not_train(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, report = run_pipeline(BASE_CONFIG, tmp, "audit", now=NOW, clock=lambda: NOW, client=FakeClient())
            self.assertEqual(code, 0)
            self.assertEqual(report["competitions"][0]["status"], "已取得数据")
            self.assertFalse((Path(tmp)/"models").exists())
            self.assertIsNone(report["api_budget"])
            self.assertIn("API额度快照不可用", (Path(tmp)/"reports/latest.md").read_text())

    def test_config_passes_daily_limit_and_deadline_without_per_run_cap(self):
        config = {**BASE_CONFIG, "max_requests_per_run": None,
                  "api_daily_limit": 5000, "api_max_run_seconds": 3600}
        client = BudgetClient()
        # Even an account-bearing status response is never copied to reports.
        client.status = lambda: {"account": {"email": "private-account@example.test"},
                                 "requests": {"current": 4988, "limit_day": 5000}}
        with tempfile.TemporaryDirectory() as tmp, patch("football_model.cli.APIClient", return_value=client) as factory:
            code, report = run_pipeline(config, tmp, "audit", now=NOW, clock=lambda: NOW)
            self.assertEqual(code, 0, report)
            factory.assert_called_once_with(Path(tmp)/"cache", max_requests=None,
                                            daily_limit=5000, max_run_seconds=3600)
            saved = json.loads((Path(tmp)/"reports/latest.json").read_text())
            self.assertEqual(saved["api_budget"], client.budget_snapshot())
            markdown = (Path(tmp)/"reports/latest.md").read_text()
            self.assertIn("不设额外次数上限", markdown)
            self.assertIn("5000／4988", markdown)
            self.assertIn("当前有效日余额（保守估计）|10", markdown)
            self.assertNotIn("private-account@example.test", json.dumps(saved) + markdown)

    def test_daily_exhaustion_and_soft_deadline_preserve_failure_reports_and_model(self):
        for phase, reason in (("status", "今日剩余额度为 0"),
                              ("fetch_fixtures", "达到网络请求软截止 3600 秒")):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as tmp:
                client = BudgetClient()
                client.requests_made = 7
                snapshot = client.budget_snapshot()
                snapshot.update(daily_remaining=0 if phase == "status" else 10,
                                elapsed_seconds=3600 if phase == "fetch_fixtures" else 3)
                client.budget_snapshot = lambda: {**snapshot, "requests_made": client.requests_made}
                model_path = Path(tmp)/"models/epl.json"
                model_path.parent.mkdir()
                original = b'{"sentinel":"keep existing model"}'
                model_path.write_bytes(original)
                with patch.object(client, phase, side_effect=RequestBudgetError(reason)):
                    code, report = run_pipeline(BASE_CONFIG, tmp, "train", now=NOW,
                                                clock=lambda: NOW, client=client)
                self.assertEqual(code, 2 if phase == "status" else 1, report)
                self.assertEqual(model_path.read_bytes(), original)
                self.assertFalse((Path(tmp)/"predictions").exists())
                saved = json.loads((Path(tmp)/"reports/latest.json").read_text())
                self.assertEqual(saved, report)
                histories = list((Path(tmp)/"reports/history").glob("*.json"))
                self.assertEqual(len(histories), 1)
                self.assertEqual(json.loads(histories[0].read_text()), report)
                self.assertIn(reason, (Path(tmp)/"reports/latest.md").read_text())
                self.assertEqual(saved["api_budget"], client.budget_snapshot())

    def test_wrong_competition_response_does_not_train(self):
        client = FakeClient()
        client.fixtures[0]["league"]["id"] = 123
        with tempfile.TemporaryDirectory() as tmp:
            code, report = run_pipeline(BASE_CONFIG, tmp, "train", now=NOW, clock=lambda: NOW, client=client)
            self.assertEqual(code, 1)
            self.assertIn("未完成", report["competitions"][0]["status"])
            self.assertFalse((Path(tmp)/"models").exists())

    def test_missing_key_has_report_and_no_dummy_model(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict("os.environ", {"FOOTBALL_API_KEY": ""}):
            code, report = run_pipeline(BASE_CONFIG, tmp, "audit", now=NOW, clock=lambda: NOW)
            self.assertEqual(code, 2)
            self.assertIn("FOOTBALL_API_KEY", report["fatal_error"])
            self.assertTrue((Path(tmp)/"reports/latest.md").exists())
            self.assertFalse((Path(tmp)/"models").exists())

    def test_seasons_use_actual_start_dates_not_calendar_year_guess(self):
        metadata = {"seasons": [{"year": 2024, "start": "2024-08-01"},
                                {"year": 2025, "start": "2025-08-01"},
                                {"year": 2023, "start": "2023-08-01"}]}
        self.assertEqual([x["year"] for x in select_seasons(metadata, NOW, 2)], [2024, 2023])


if __name__ == "__main__":
    unittest.main()
