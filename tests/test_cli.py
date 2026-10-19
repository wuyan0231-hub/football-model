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
from football_model.model import fit_model


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
    def test_added_domestic_league_run_keeps_existing_models_independent(self):
        production = json.loads((Path(__file__).parents[1] / "config.json").read_text())
        for key in ("segunda", "argentina_a", "championship"):
            with self.subTest(league=key), tempfile.TemporaryDirectory() as tmp:
                competition = next(c for c in production["competitions"] if c["key"] == key)
                config = copy.deepcopy(BASE_CONFIG)
                config["competitions"].append(competition)
                existing = Path(tmp) / "models" / "laliga.json"
                existing.parent.mkdir()
                original = b'{"sentinel":"existing La Liga model"}'
                existing.write_bytes(original)
                code, report = run_pipeline(config, tmp, "train", league=key,
                                            now=NOW, clock=lambda: NOW, client=FakeClient())
                self.assertEqual(code, 0, report)
                self.assertEqual(report["requested_competitions"], [key])
                self.assertEqual(report["competitions"][0]["forecast_count"], 1)
                model = json.loads((Path(tmp) / "models" / (key + ".json")).read_text())
                self.assertEqual(model["competition"], competition)
                self.assertEqual(existing.read_bytes(), original)
                self.assertFalse((Path(tmp) / "models" / "epl.json").exists())

    def test_real_config_all_selection_and_workflow_menu_include_added_leagues(self):
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
            for key in ("segunda", "argentina_a", "championship"):
                self.assertEqual([call.args[1]["key"] for call in process.call_args_list].count(key), 1)

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

    def test_domestic_same_data_expiring_window_really_refits_and_keeps_history(self):
        for days in (13, 15):
            with self.subTest(days=days), tempfile.TemporaryDirectory() as tmp:
                code, original = run_pipeline(BASE_CONFIG, tmp, "train", now=NOW,
                                             clock=lambda: NOW, client=FakeClient())
                self.assertEqual(code, 0, original)
                model_path = Path(tmp)/"models/epl.json"
                old = json.loads(model_path.read_text())
                prior_history = Path(tmp)/"model_history/epl"/(original["run_id"] + ".json")
                history_bytes = prior_history.read_bytes()
                later = NOW + timedelta(days=days)
                client = FakeClient()
                client.fixtures[-1] = fixture(2001, later + timedelta(days=2), 1, 2, None, None, "NS")
                with patch("football_model.cli.fit_model", wraps=fit_model) as actual_fit:
                    code, report = run_pipeline(BASE_CONFIG, tmp, "train", now=later,
                                                clock=lambda: later, client=client)
                self.assertEqual(code, 0, report)
                actual_fit.assert_called_once()
                self.assertEqual(actual_fit.call_args.args[1], iso(later))
                refreshed = json.loads(model_path.read_text())
                self.assertEqual(refreshed["data_fingerprint"], old["data_fingerprint"])
                self.assertEqual(refreshed["model"]["training_data_sha256"], old["model"]["training_data_sha256"])
                self.assertNotEqual(refreshed["model_id"], old["model_id"])
                self.assertEqual(refreshed["trained_at"], iso(later))
                self.assertEqual(refreshed["model_fitted_at"], iso(later))
                self.assertEqual(prior_history.read_bytes(), history_bytes)
                item = report["competitions"][0]
                self.assertTrue(item["model_refresh"]["performed"])
                self.assertFalse(item["model_refresh"]["data_changed"])
                self.assertIn("expires_before_kickoff", item["model_refresh"]["reasons"])
                self.assertEqual(item["model_freshness"]["age_days"], 0)
                self.assertGreater(item["model_freshness"]["training_data_age_days"], days)
                self.assertEqual(item["forecast_availability"]["status"], "available")
                self.assertEqual(item["forecast_count"], 1)

    def test_domestic_stale_empty_window_is_reported_without_fake_refresh(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_pipeline(BASE_CONFIG, tmp, "train", now=NOW, clock=lambda: NOW, client=FakeClient())
            model_path = Path(tmp)/"models/epl.json"
            before = model_path.read_bytes()
            later = NOW + timedelta(days=15)
            with patch("football_model.cli.fit_model", side_effect=AssertionError("empty window must not refit")):
                code, report = run_pipeline(BASE_CONFIG, tmp, "train", now=later,
                                            clock=lambda: later, client=FakeClient())
            self.assertEqual(code, 0, report)
            self.assertEqual(model_path.read_bytes(), before)
            item = report["competitions"][0]
            self.assertEqual(item["model_freshness"]["status"], "stale")
            self.assertEqual(item["forecast_availability"]["status"], "no_upcoming")
            self.assertEqual(item["forecast_availability"]["upcoming_count"], 0)
            index = json.loads((Path(tmp)/"integration/index.json").read_text())
            self.assertEqual(index["competitions"][0]["status"], "no_upcoming")
            self.assertFalse(index["competitions"][0]["usable_saved_forecasts"])

    def test_kickoff_at_exact_age_limit_keeps_existing_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_pipeline(BASE_CONFIG, tmp, "train", now=NOW, clock=lambda: NOW, client=FakeClient())
            model_path = Path(tmp)/"models/epl.json"
            before = model_path.read_bytes()
            later = NOW + timedelta(days=12)
            client = FakeClient()
            client.fixtures[-1] = fixture(2001, NOW + timedelta(days=14), 1, 2, None, None, "NS")
            with patch("football_model.cli.fit_model", side_effect=AssertionError("age boundary is still valid")):
                code, report = run_pipeline(BASE_CONFIG, tmp, "train", now=later,
                                            clock=lambda: later, client=client)
            self.assertEqual(code, 0, report)
            self.assertEqual(model_path.read_bytes(), before)
            self.assertEqual(report["competitions"][0]["forecast_count"], 1)

    def test_long_forecast_horizon_does_not_bypass_model_age_gate(self):
        config = {**BASE_CONFIG, "forecast_days": 21}
        client = FakeClient()
        client.fixtures[-1] = fixture(2001, NOW + timedelta(days=20), 1, 2, None, None, "NS")
        with tempfile.TemporaryDirectory() as tmp:
            code, report = run_pipeline(config, tmp, "train", now=NOW, clock=lambda: NOW, client=client)
            self.assertEqual(code, 0, report)
            item = report["competitions"][0]
            self.assertEqual(item["forecast_availability"]["status"], "unavailable")
            self.assertEqual(item["forecast_count"], 0)
            rows = json.loads((Path(tmp)/"predictions"/report["run_id"]/"epl.json").read_text())
            self.assertNotIn("prediction", rows[0])
            self.assertIn("不能仅刷新时间戳", rows[0]["reason"])

    def test_failed_expiry_refit_preserves_model_and_blocks_old_forecast(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_pipeline(BASE_CONFIG, tmp, "train", now=NOW, clock=lambda: NOW, client=FakeClient())
            model_path = Path(tmp)/"models/epl.json"
            before = model_path.read_bytes()
            later = NOW + timedelta(days=15)
            client = FakeClient()
            client.fixtures[-1] = fixture(2001, later + timedelta(days=2), 1, 2, None, None, "NS")
            with patch("football_model.cli.fit_model", side_effect=RuntimeError("synthetic refit failure")):
                code, report = run_pipeline(BASE_CONFIG, tmp, "train", now=later,
                                            clock=lambda: later, client=client)
            self.assertEqual(code, 1, report)
            self.assertEqual(model_path.read_bytes(), before)
            item = report["competitions"][0]
            self.assertEqual(item["forecast_availability"]["status"], "failed")
            self.assertFalse(item["model_refresh"]["performed"])
            self.assertFalse((Path(tmp)/"predictions"/report["run_id"]/"epl.json").exists())
            index = json.loads((Path(tmp)/"integration/index.json").read_text())
            self.assertEqual(index["competitions"][0]["status"], "blocked_latest_failure")

    def test_unknown_team_coverage_differs_from_an_empty_window_and_process_success(self):
        for known, expected in ((False, "unavailable"), (True, "partial")):
            with self.subTest(known=known), tempfile.TemporaryDirectory() as tmp:
                client = FakeClient()
                unknown = fixture(2001, NOW + timedelta(days=3), 999, 2, None, None, "NS")
                if known:
                    client.fixtures.append(unknown)
                else:
                    client.fixtures[-1] = unknown
                code, report = run_pipeline(BASE_CONFIG, tmp, "train", now=NOW,
                                            clock=lambda: NOW, client=client)
                self.assertEqual(code, 0, report)  # Execution success is not availability.
                item = report["competitions"][0]
                coverage = item["forecast_availability"]
                self.assertEqual(coverage["status"], expected)
                self.assertEqual(coverage["supported_count"], int(known))
                self.assertEqual(coverage["unsupported_count"], 1)
                self.assertEqual(sum(coverage["unsupported_reasons"].values()), 1)
                self.assertEqual(report["forecast_availability_summary"]["competition_status_counts"], {expected: 1})
                index = json.loads((Path(tmp)/"integration/index.json").read_text())
                self.assertEqual(index["competitions"][0]["forecast_availability"], coverage)
                self.assertEqual(index["competitions"][0]["usable_saved_forecasts"], known)

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
