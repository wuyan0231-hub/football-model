"""Access checks on saved forecasts; no API account or accuracy claims."""
import copy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

from football_model.cli import atomic_json, iso, run_pipeline
from football_model.integration import IntegrationError, build_index, match_packet, main

NOW = datetime(2025, 1, 1, tzinfo=timezone.utc)
CONFIG = {
    "history_seasons": 1, "max_requests_per_run": 80, "half_life_days": 180,
    "ridge": 3, "min_train": 60, "min_test": 15, "forecast_days": 7,
    "max_model_age_days": 14,
    "competitions": [{"key": "epl", "label": "合成联赛", "name": "Synthetic League",
                      "country": "Test", "mode": "domestic_baseline"},
                     {"key": "ucl", "label": "欧冠", "name": "UEFA Champions League",
                      "country": "World", "mode": "audit_only"},
                     {"key": "mls", "label": "未运行联赛", "name": "Other League",
                      "country": "Other", "mode": "domestic_baseline"}]
}


class FakeClient:
    requests_made = 0

    def status(self):
        return {}

    def resolve_league(self, competition):
        return {"league": {"id": 999}, "seasons": [{"year": 2024, "start": "2024-01-01"}]}

    def fetch_fixtures(self, league_id, season, **kwargs):
        rng = np.random.default_rng(726)
        rows = []
        for i in range(181):
            future = i == 180
            kickoff = NOW + timedelta(days=2) if future else NOW - timedelta(days=190-i)
            home = i % 6 + 1
            away = (home + 1 + (i // 6) % 5 - 1) % 6 + 1
            rows.append({"fixture": {"id": 2000 if future else i+1, "date": iso(kickoff),
                                      "timestamp": int(kickoff.timestamp()), "status": {"short": "NS" if future else "FT"}},
                         "league": {"id": 999, "season": 2024},
                         "teams": {"home": {"id": home, "name": f"Synthetic {home}"},
                                   "away": {"id": away, "name": f"Synthetic {away}"}},
                         "score": {"fulltime": {"home": None if future else int(rng.poisson(1.6)),
                                                  "away": None if future else int(rng.poisson(1.1))}}})
        return {"response": rows, "fetched_at": iso(NOW)}


class IntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base = tempfile.TemporaryDirectory()
        code, report = run_pipeline(CONFIG, cls.base.name, "train", now=NOW, client=FakeClient())
        if code:
            raise AssertionError(report)
        cls.run_id = report["run_id"]

    @classmethod
    def tearDownClass(cls):
        cls.base.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.runtime = Path(self.temp.name) / "runtime"
        shutil.copytree(self.base.name, self.runtime)
        self.report_path = self.runtime / "reports" / "history" / f"{self.run_id}.json"
        self.prediction_path = self.runtime / "predictions" / self.run_id / "epl.json"
        self.model_path = self.runtime / "models" / "epl.json"

    def tearDown(self):
        self.temp.cleanup()

    def read(self, path):
        return json.loads(path.read_text(encoding="utf-8"))

    def packet(self, **kwargs):
        options = {"runtime": self.runtime, "config": CONFIG, "league": "epl", "match_id": 2000,
                   "as_of": iso(NOW + timedelta(hours=1))}
        options.update(kwargs)
        return match_packet(**options)

    def add_attempt(self, key="epl", mode="train", status="未完成", fatal=False):
        report = {"run_id": "later", "generated_at": iso(NOW + timedelta(minutes=30)),
                  "mode": mode, "source_commit": "a"*40, "requested_competitions": [key],
                  "competitions": [] if fatal else [{"key": key, "status": status, "detail": "test"}]}
        if fatal:
            report["fatal_error"] = "test authentication failure"
        atomic_json(self.runtime / "reports" / "history" / "later.json", report)
        atomic_json(self.runtime / "reports" / "latest.json", report)
        build_index(self.runtime, CONFIG)

    def test_index_all_leagues_ignores_global_latest_shortcut(self):
        self.add_attempt(key="mls")
        index = build_index(self.runtime, CONFIG)
        rows = {row["key"]: row for row in index["competitions"]}
        self.assertEqual(rows["epl"]["status"], "available")
        self.assertEqual(rows["mls"]["status"], "blocked_latest_failure")
        self.assertEqual(rows["ucl"]["status"], "audit_only")
        self.assertTrue((self.runtime / "integration" / "index.md").is_file())
        with self.assertRaisesRegex(IntegrationError, "audit_only"):
            self.packet(league="ucl")

    def test_current_report_hook_retains_source_and_all_leagues(self):
        report = self.read(self.report_path)
        report["source_commit"] = "b"*40
        index = build_index(self.runtime, CONFIG, report)
        self.assertEqual(index["source_commit"], "b"*40)
        self.assertEqual(len(index["competitions"]), 3)
        self.assertEqual(index["competitions"][0]["latest_attempt"]["source_commit"], "b"*40)

    def test_saved_prediction_recomputed_and_full_validation_retained(self):
        before = self.model_path.read_bytes()
        result = self.packet(state_commit="c"*40)
        self.assertEqual(result["prediction"], self.read(self.prediction_path)[0]["prediction"])
        self.assertEqual(result["validation"], self.read(self.model_path)["validation"])
        self.assertTrue(result["provenance"]["read_and_recomputed"])
        self.assertFalse(result["provenance"]["fit_performed"])
        self.assertEqual(result["external_checks"]["actual_venue_and_neutral_status"], "pending")
        self.assertEqual(result["market_mapping"]["status"], "not_executed")
        self.assertEqual(self.model_path.read_bytes(), before)

    def test_zero_forecasts_explicit_and_no_fallback(self):
        report = self.read(self.report_path)
        report["competitions"][0]["forecast_count"] = 0
        report["competitions"][0]["unsupported_forecasts"] = 0
        atomic_json(self.report_path, report)
        atomic_json(self.prediction_path, [])
        self.assertEqual(build_index(self.runtime, CONFIG)["competitions"][0]["status"], "no_upcoming")
        with self.assertRaisesRegex(IntegrationError, "no_upcoming"):
            self.packet()

    def test_latest_failed_train_and_audit_block_old_success(self):
        for mode in ("train", "audit"):
            with self.subTest(mode=mode):
                self.add_attempt(mode=mode)
                row = build_index(self.runtime, CONFIG)["competitions"][0]
                self.assertEqual(row["status"], "blocked_latest_failure")
                self.assertEqual(row["latest_successful_forecast"]["run_id"], self.run_id)
                with self.assertRaisesRegex(IntegrationError, "blocked_latest_failure"):
                    self.packet()

    def test_fatal_scoped_attempt_blocks_without_league_result(self):
        self.add_attempt(fatal=True)
        with self.assertRaisesRegex(IntegrationError, "blocked_latest_failure"):
            self.packet()

    def test_recent_successful_audit_does_not_pretend_to_generate_prediction(self):
        self.add_attempt(mode="audit", status="已取得数据")
        row = build_index(self.runtime, CONFIG)["competitions"][0]
        self.assertEqual(row["latest_attempt"]["run_id"], "later")
        self.assertEqual(row["latest_successful_forecast"]["run_id"], self.run_id)
        self.assertEqual(self.packet()["provenance"]["forecast_generated_at"], iso(NOW))

    def test_future_information_and_started_match_rejected(self):
        for asof in (NOW - timedelta(seconds=1), NOW + timedelta(days=2)):
            with self.subTest(asof=asof), self.assertRaises(IntegrationError):
                self.packet(as_of=iso(asof))

    def test_stale_at_kickoff_and_stale_training_run_rejected(self):
        config = copy.deepcopy(CONFIG)
        config["max_model_age_days"] = 1
        with self.assertRaisesRegex(IntegrationError, "时效"):
            self.packet(config=config)
        self.add_attempt(status="模型陈旧")
        with self.assertRaisesRegex(IntegrationError, "stale_model"):
            self.packet()

    def test_new_current_model_cannot_reuse_old_forecast(self):
        bundle = self.read(self.model_path)
        bundle["model_id"] = "epl-new-model"
        atomic_json(self.model_path, bundle)
        with self.assertRaisesRegex(IntegrationError, "不一致"):
            self.packet()

    def test_wrong_identity_unknown_team_and_duplicate_match_rejected(self):
        original = self.read(self.prediction_path)
        cases = []
        wrong = copy.deepcopy(original); wrong[0]["home_id"] = 999; cases.append(wrong)
        wrong = copy.deepcopy(original); wrong[0]["competition"] = "mls"; cases.append(wrong)
        wrong = copy.deepcopy(original); wrong[0]["home_id"], wrong[0]["away_id"] = wrong[0]["away_id"], wrong[0]["home_id"]; cases.append(wrong)
        wrong = copy.deepcopy(original); wrong[0]["prediction"]["kickoff"] = "2025-01-04T00:00:00Z"; cases.append(wrong)
        for rows in cases:
            atomic_json(self.prediction_path, rows)
            with self.subTest(rows=rows[0].get("home_id")), self.assertRaises(IntegrationError):
                self.packet()
        atomic_json(self.prediction_path, original + original)
        with self.assertRaises(IntegrationError):
            self.packet()

    def test_tampered_probabilities_axes_tail_and_timestamps_rejected(self):
        original = self.read(self.prediction_path)
        for field, value in (("tail_mass_before_normalization", 0.1),
                             ("score_matrix_axes", {"rows": "away", "columns": "home"}),
                             ("probabilities", {"home_win": 0.9, "draw": 0.05, "away_win": 0.05}),
                             ("score_matrix", [[1.1]])):
            rows = copy.deepcopy(original)
            rows[0]["prediction"][field] = value
            atomic_json(self.prediction_path, rows)
            with self.subTest(field=field), self.assertRaises(IntegrationError):
                self.packet()
        for field in ("trained_at", "data_observed_at", "generated_at"):
            rows = copy.deepcopy(original)
            rows[0][field] = iso(NOW + timedelta(hours=2))
            atomic_json(self.prediction_path, rows)
            with self.subTest(field=field), self.assertRaises(IntegrationError):
                self.packet()

    def test_index_failure_changes_task_exit_and_is_saved_in_report(self):
        destination = self.runtime
        with patch("football_model.integration.build_index", side_effect=OSError("index cannot be saved")):
            code, report = run_pipeline(CONFIG, destination, "train", now=NOW+timedelta(minutes=30), client=FakeClient())
        self.assertEqual(code, 1)
        self.assertEqual(report["integration_index"]["status"], "failed")
        self.assertTrue((destination / "models" / "epl.json").is_file())
        self.assertEqual(self.read(destination / "reports" / "latest.json")["exit_code"], 1)
        self.assertEqual(self.read(destination / "integration" / "index.json")["status"], "failed")
        index_markdown = (destination / "integration" / "index.md").read_text()
        self.assertIn("模型接入索引失败", index_markdown)
        self.assertIn(report["run_id"], index_markdown)
        self.assertNotIn("|available|", index_markdown)
        self.assertIn("模型接入索引失败", (destination / "reports" / "latest.md").read_text())
        with self.assertRaisesRegex(IntegrationError, "索引更新失败"):
            self.packet()

    def test_target_only_snapshot_works_without_unrelated_files(self):
        code, report = run_pipeline(CONFIG, self.runtime, "train", league="mls",
                                    now=NOW+timedelta(minutes=30), client=FakeClient())
        self.assertEqual(code, 0)
        (self.runtime / "models" / "mls.json").unlink()
        (self.runtime / "predictions" / report["run_id"] / "mls.json").unlink()
        (self.runtime / "reports" / "history" / f"{report['run_id']}.json").unlink()
        self.assertEqual(self.packet()["fixture"]["match_id"], 2000)

    def test_missing_referenced_report_and_stale_index_rejected(self):
        self.report_path.unlink()
        with self.assertRaisesRegex(IntegrationError, "缺少索引引用"):
            self.packet()
        shutil.copy(Path(self.base.name) / "reports" / "history" / self.report_path.name, self.report_path)
        stale = self.read(self.runtime / "integration" / "index.json")
        self.add_attempt(mode="audit")
        atomic_json(self.runtime / "integration" / "index.json", stale)
        with self.assertRaisesRegex(IntegrationError, "索引与实际报告"):
            self.packet()

    def test_missing_index_cannot_fallback_to_incomplete_history(self):
        (self.runtime / "integration" / "index.json").unlink()
        with self.assertRaisesRegex(IntegrationError, "缺少当前接入索引"):
            self.packet()

    def test_cli_creates_real_match_call_artifact(self):
        config_path = Path(self.temp.name) / "config.json"
        output = self.runtime / "match_calls" / "test.json"
        atomic_json(config_path, CONFIG)
        code = main(["match", "--runtime", str(self.runtime), "--config", str(config_path),
                     "--league", "epl", "--match-id", "2000", "--as-of", iso(NOW+timedelta(hours=1)),
                     "--output", str(output)])
        self.assertEqual(code, 0)
        self.assertTrue(output.is_file())


if __name__ == "__main__":
    unittest.main()
