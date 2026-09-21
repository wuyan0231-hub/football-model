"""Pipeline integration uses synthetic fixtures only, never a real API account."""
import copy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

from football_model.cli import run_pipeline, select_seasons, iso
from football_model.data import APIError


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


class PipelineTests(unittest.TestCase):
    def test_actual_fit_and_unchanged_data_reuses_same_parameters(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = FakeClient()
            code, report = run_pipeline(BASE_CONFIG, tmp, "train", now=NOW, client=client)
            self.assertEqual(code, 0, report)
            item = report["competitions"][0]
            self.assertEqual(item["model_change"], "完成新拟合")
            self.assertEqual(item["forecast_count"], 1)
            self.assertTrue(client.fetch_calls[0]["force"])
            model_path = Path(tmp)/"models/epl.json"
            before = model_path.read_bytes()
            with patch("football_model.cli.fit_model", side_effect=AssertionError("不应重训")):
                code, second = run_pipeline(BASE_CONFIG, tmp, "train", now=NOW+timedelta(hours=2), client=FakeClient())
            self.assertEqual(code, 0, second)
            self.assertEqual(model_path.read_bytes(), before)
            self.assertEqual(second["competitions"][0]["model_change"], "数据未变，沿用参数")
            self.assertEqual(len(list((Path(tmp)/"reports/history").glob("*.json"))), 2)

    def test_failed_download_retains_model_without_new_forecast(self):
        with tempfile.TemporaryDirectory() as tmp:
            model_path = Path(tmp)/"models/epl.json"
            model_path.parent.mkdir()
            model_path.write_text('{"sentinel":"existing model"}', encoding="utf-8")
            code, report = run_pipeline(BASE_CONFIG, tmp, "train", now=NOW, client=FakeClient(True))
            self.assertEqual(code, 1)
            self.assertEqual(report["competitions"][0]["status"], "未完成")
            self.assertEqual(json.loads(model_path.read_text())["sentinel"], "existing model")
            self.assertFalse((Path(tmp)/"predictions").exists())

    def test_ucl_stays_audit_only_even_in_train_mode(self):
        config = copy.deepcopy(BASE_CONFIG)
        config["competitions"][0].update(key="ucl", label="欧冠", mode="audit_only")
        with tempfile.TemporaryDirectory() as tmp, patch("football_model.cli.fit_model", side_effect=AssertionError("欧冠不能训练")):
            code, report = run_pipeline(config, tmp, "train", league="all", now=NOW, client=FakeClient())
            self.assertEqual(code, 0)
            self.assertEqual(report["competitions"][0]["status"], "仅审计")
            self.assertFalse((Path(tmp)/"models").exists())

    def test_audit_does_not_train(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, report = run_pipeline(BASE_CONFIG, tmp, "audit", now=NOW, client=FakeClient())
            self.assertEqual(code, 0)
            self.assertEqual(report["competitions"][0]["status"], "已取得数据")
            self.assertFalse((Path(tmp)/"models").exists())

    def test_wrong_competition_response_does_not_train(self):
        client = FakeClient()
        client.fixtures[0]["league"]["id"] = 123
        with tempfile.TemporaryDirectory() as tmp:
            code, report = run_pipeline(BASE_CONFIG, tmp, "train", now=NOW, client=client)
            self.assertEqual(code, 1)
            self.assertIn("未完成", report["competitions"][0]["status"])
            self.assertFalse((Path(tmp)/"models").exists())

    def test_missing_key_has_report_and_no_dummy_model(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict("os.environ", {"FOOTBALL_API_KEY": ""}):
            code, report = run_pipeline(BASE_CONFIG, tmp, "audit", now=NOW)
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
