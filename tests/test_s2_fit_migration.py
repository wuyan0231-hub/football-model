"""S2 migration regressions: synthetic inputs, actual fitting and original gates."""
import copy
from datetime import timedelta
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from football_model import cli, prospective
from football_model.model import evaluate_time_holdout, fit_model
from football_model.production_replay import replay_bundle
from test_cli import BASE_CONFIG, FakeClient, NOW
from test_nations_pipeline import CONFIG as NATIONAL_CONFIG, NOW as NATIONAL_NOW, NationalClient


class FitTimeMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def read(self, relative):
        return json.loads((self.root / relative).read_text())

    def seed_legacy(self, config=None, client=None, now=NOW, league="epl", absent=False):
        config = config or BASE_CONFIG
        code, report = cli.run_pipeline(config, self.root, "train", league=league,
                                       now=now, clock=lambda: now, client=client or FakeClient())
        self.assertEqual(code, 0, report)
        for item in report["competitions"]:
            model_path = self.root / "models" / (item["key"] + ".json")
            model = json.loads(model_path.read_text())
            if absent:
                model.pop("model_fitted_at")
            else:
                model["model_fitted_at"] = None
            # Deliberately irregular formatting proves exact byte preservation.
            model_path.write_text(json.dumps(model, ensure_ascii=False, indent=1) + "\n\n")
            prediction_path = self.root / "predictions" / report["run_id"] / (item["key"] + ".json")
            predictions = json.loads(prediction_path.read_text())
            for row in predictions:
                row["model_fitted_at"] = None
            prediction_path.write_text(json.dumps(predictions))
        return report

    def invoke(self, later=None, config=None, **kwargs):
        later = later or NOW + timedelta(hours=2)
        return cli.run_pipeline(config or BASE_CONFIG, self.root, "train", now=later,
                                clock=lambda: later, client=kwargs.pop("client", FakeClient()), **kwargs)

    def test_missing_or_null_time_performs_real_fit_then_strict_capture_and_preserves_old_rejection(self):
        for absent in (False, True):
            with self.subTest(absent=absent), tempfile.TemporaryDirectory() as temp:
                self.root = Path(temp)
                original = self.seed_legacy(absent=absent)
                old_bytes = (self.root / "models/epl.json").read_bytes()
                old = json.loads(old_bytes)
                # Seeding now performs a genuine timely capture before we turn
                # the synthetic model into a legacy fixture. Preserve that
                # capture, and inspect the missing-fit fixture as a separate
                # historical run rather than mutating the frozen run identity.
                seed_forecasts = prospective._objects(self.root / "prospective", "forecasts")
                seed_ids = {row["object_id"] for row in seed_forecasts}
                legacy_report = copy.deepcopy(original)
                legacy_report["run_id"] += "_legacy_missing_fit"
                legacy_rows = self.read("predictions/" + original["run_id"] + "/epl.json")
                cli.atomic_json(self.root / "predictions" / legacy_report["run_id"] / "epl.json", legacy_rows)
                cli.atomic_json(self.root / "reports" / "latest.json", legacy_report)
                code, rejected = prospective.run(self.root, BASE_CONFIG, clock=lambda: NOW + timedelta(hours=1))
                self.assertEqual(code, 1, rejected)
                legacy_coverage = [row for row in prospective._objects(self.root / "prospective", "coverage")
                                   if row["run_id"] == legacy_report["run_id"]]
                self.assertEqual(len(legacy_coverage), 1)
                self.assertEqual(legacy_coverage[0]["status"], "rejected")
                self.assertIn("model_fitted_at", legacy_coverage[0]["reason"])
                self.assertEqual(rejected["forecast_versions"], len(seed_ids))
                old_coverage = {p.name: p.read_bytes() for p in (self.root / "prospective/coverage").glob("*.json")}
                original_files = {p: p.read_bytes() for directory in ("predictions", "reports/history", "model_history")
                                  for p in (self.root / directory).rglob("*.json")}
                later = NOW + timedelta(hours=2)
                ticks = iter(later + timedelta(seconds=n) for n in range(100))
                with patch("football_model.cli.fit_model", wraps=fit_model) as actual_fit, patch(
                        "football_model.cli.evaluate_time_holdout", wraps=evaluate_time_holdout) as actual_validation:
                    code, report = cli.run_pipeline(BASE_CONFIG, self.root, "train", now=later,
                        clock=lambda: next(ticks), client=FakeClient())
                self.assertEqual(code, 0, report)
                actual_fit.assert_called_once()
                actual_validation.assert_called_once()
                self.assertEqual(actual_fit.call_args.args[1], cli.iso(later))
                self.assertEqual(actual_fit.call_args.kwargs, {"half_life_days": 180, "ridge": 3, "min_train": 60})
                item = report["competitions"][0]
                self.assertEqual(item["model_refresh"]["reasons"], ["missing_model_fitted_at"])
                self.assertFalse(item["model_refresh"]["data_changed"])
                self.assertFalse(item["model_refresh"]["training_inputs_changed"])
                migration = item["model_refresh"]["time_evidence_migration"]
                self.assertEqual(migration["status"], "completed")
                new = self.read("models/epl.json")
                self.assertGreater(cli.parse_date(new["model_fitted_at"]), later)
                self.assertEqual(new["trained_at"], cli.iso(later))
                self.assertNotEqual(new["model_id"], old["model_id"])
                self.assertEqual(new["data_fingerprint"], old["data_fingerprint"])
                self.assertEqual(new["model"]["training_data_sha256"], old["model"]["training_data_sha256"])
                self.assertFalse(new["model"]["validated_for_production"])
                evidence = migration["prior_model_evidence"]
                self.assertEqual(evidence["sha256"], hashlib.sha256(old_bytes).hexdigest())
                self.assertEqual((self.root / evidence["path"].removeprefix("runtime/")).read_bytes(), old_bytes)
                self.assertEqual(new["time_evidence_migration"]["prior_model_fitted_at"], None)
                for path, payload in original_files.items():
                    self.assertEqual(path.read_bytes(), payload)
                code, captured = prospective.run(self.root, BASE_CONFIG, clock=lambda: later + timedelta(minutes=1))
                self.assertEqual(code, 0, captured)
                all_forecasts = prospective._objects(self.root / "prospective", "forecasts")
                current = [row for row in all_forecasts if row["run_id"] == report["run_id"]]
                self.assertEqual(len(current), 1)
                self.assertEqual(current[0]["competition"], "epl")
                self.assertEqual(captured["forecast_versions"], len(seed_ids) + len(current))
                self.assertTrue(seed_ids <= {row["object_id"] for row in all_forecasts})
                for name, payload in old_coverage.items():
                    self.assertEqual((self.root / "prospective/coverage" / name).read_bytes(), payload)
                with patch("football_model.cli.fit_model", side_effect=AssertionError("migration must happen once")):
                    code, following = self.invoke(later=later + timedelta(hours=1))
                self.assertEqual(code, 0, following)
                self.assertFalse(following["competitions"][0]["model_refresh"]["required"])

    def test_no_upcoming_retains_unknown_time_without_refitting(self):
        self.seed_legacy()
        before = (self.root / "models/epl.json").read_bytes()
        client = FakeClient()
        client.fixtures = client.fixtures[:-1]
        with patch("football_model.cli.fit_model", side_effect=AssertionError("no future must not migrate")):
            code, report = self.invoke(client=client)
        self.assertEqual(code, 0, report)
        item = report["competitions"][0]
        self.assertEqual(item["forecast_availability"]["status"], "no_upcoming")
        self.assertFalse(item["model_refresh"]["required"])
        self.assertIsNone(item["model_fitted_at"])
        self.assertEqual((self.root / "models/epl.json").read_bytes(), before)
        self.assertFalse((self.root / "model_history/epl/predecessors").exists())

    def test_holdout_failure_retains_old_model_without_calling_final_fit(self):
        self.seed_legacy()
        before = (self.root / "models/epl.json").read_bytes()
        with patch("football_model.cli.evaluate_time_holdout", return_value={"report": {
                "meets_minimum_supported_test_matches": False}}), patch("football_model.cli.fit_model") as fit:
            code, report = self.invoke()
        fit.assert_not_called()
        self.assertEqual(code, 1, report)
        item = report["competitions"][0]
        migration = item["model_refresh"]["time_evidence_migration"]
        self.assertEqual((migration["status"], migration["stage"]), ("failed", "evaluate_time_holdout"))
        self.assertEqual((self.root / "models/epl.json").read_bytes(), before)
        self.assertFalse((self.root / "predictions" / report["run_id"] / "epl.json").exists())

    def test_failed_fit_retains_evidence_and_does_not_reuse_old_forecast(self):
        self.seed_legacy()
        before = (self.root / "models/epl.json").read_bytes()
        with patch("football_model.cli.fit_model", side_effect=RuntimeError("synthetic optimizer failure")):
            code, report = self.invoke()
        self.assertEqual(code, 1, report)
        item = report["competitions"][0]
        self.assertFalse(item["model_refresh"]["performed"])
        self.assertEqual(item["forecast_availability"]["status"], "failed")
        migration = item["model_refresh"]["time_evidence_migration"]
        self.assertEqual((migration["status"], migration["stage"]), ("failed", "fit_model"))
        self.assertIn("synthetic optimizer failure", migration["failure"])
        self.assertEqual((self.root / "models/epl.json").read_bytes(), before)
        self.assertFalse((self.root / "predictions" / report["run_id"] / "epl.json").exists())
        self.assertEqual(self.read("integration/index.json")["competitions"][0]["status"], "blocked_latest_failure")

    def test_failed_save_retains_old_model_and_marks_migration_failed(self):
        self.seed_legacy()
        before = (self.root / "models/epl.json").read_bytes()
        original_save = cli.atomic_json
        def fail_latest(path, value):
            if path == self.root / "models/epl.json":
                raise OSError("synthetic model save failure")
            original_save(path, value)
        with patch("football_model.cli.atomic_json", side_effect=fail_latest):
            code, report = self.invoke()
        self.assertEqual(code, 1, report)
        migration = report["competitions"][0]["model_refresh"]["time_evidence_migration"]
        self.assertEqual((migration["status"], migration["stage"]), ("failed", "save_model_and_validation"))
        self.assertEqual((self.root / "models/epl.json").read_bytes(), before)
        self.assertFalse((self.root / "predictions" / report["run_id"] / "epl.json").exists())

    def test_one_failed_league_does_not_discard_other_league_migration(self):
        config = copy.deepcopy(BASE_CONFIG)
        config["competitions"].append({**config["competitions"][0], "key": "second"})
        self.seed_legacy(config=config, league="all")
        seed_ids = {r["object_id"] for r in prospective._objects(self.root / "prospective", "forecasts")}
        first_bytes = (self.root / "models/epl.json").read_bytes()
        calls = []
        def fail_first(*args, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("synthetic first-league fit failure")
            return fit_model(*args, **kwargs)
        with patch("football_model.cli.fit_model", side_effect=fail_first):
            code, report = self.invoke(config=config, league="all")
        self.assertEqual(code, 1, report)
        self.assertEqual((self.root / "models/epl.json").read_bytes(), first_bytes)
        first, second = report["competitions"]
        self.assertEqual(first["forecast_availability"]["status"], "failed")
        self.assertEqual(second["model_refresh"]["time_evidence_migration"]["status"], "completed")
        self.assertTrue((self.root / "predictions" / report["run_id"] / "second.json").exists())
        code, captured = prospective.run(self.root, config, clock=lambda: NOW + timedelta(hours=3))
        self.assertEqual(code, 0, captured)
        all_forecasts = prospective._objects(self.root / "prospective", "forecasts")
        current = [r for r in all_forecasts if r["run_id"] == report["run_id"]]
        self.assertEqual([r["competition"] for r in current], ["second"])
        self.assertEqual(captured["forecast_versions"], len(seed_ids) + len(current))
        self.assertTrue(seed_ids <= {r["object_id"] for r in all_forecasts})

    def test_shared_national_path_migrates_without_loosening_venue_gate(self):
        self.seed_legacy(config=NATIONAL_CONFIG, client=NationalClient(), now=NATIONAL_NOW, league="unl")
        later = NATIONAL_NOW + timedelta(hours=2)
        code, report = self.invoke(later=later, config=NATIONAL_CONFIG,
                                   client=NationalClient(later), league="unl")
        self.assertEqual(code, 0, report)
        item = report["competitions"][0]
        self.assertEqual(item["model_refresh"]["reasons"], ["missing_model_fitted_at"])
        self.assertEqual((item["forecast_count"], item["unsupported_forecasts"]), (2, 1))
        rows = self.read("predictions/" + report["run_id"] + "/unl.json")
        self.assertEqual(next(r for r in rows if r["match_id"] == 2002)["status"], "unsupported")
        self.assertTrue(next(r for r in rows if r["match_id"] == 2001)["prediction"]["neutral"])

    def test_migration_research_output_still_cannot_enter_prospective_ledger(self):
        self.seed_legacy()
        seed_ids = {r["object_id"] for r in prospective._objects(self.root / "prospective", "forecasts")}
        code, report = self.invoke(research_replay=True)
        self.assertEqual(code, 0, report)
        code, captured = prospective.run(self.root, BASE_CONFIG, clock=lambda: NOW + timedelta(hours=3))
        self.assertEqual(code, 1, captured)
        all_forecasts = prospective._objects(self.root / "prospective", "forecasts")
        self.assertEqual(captured["forecast_versions"], len(seed_ids))
        self.assertEqual({r["object_id"] for r in all_forecasts}, seed_ids)
        self.assertFalse(any(r["run_id"] == report["run_id"] for r in all_forecasts))
        coverage = [r for r in prospective._objects(self.root / "prospective", "coverage")
                    if r["run_id"] == report["run_id"]]
        self.assertEqual(len(coverage), 1)
        self.assertEqual(coverage[0]["status"], "rejected")
        self.assertEqual(coverage[0]["reason"], "research_replay_is_not_prospective")

    def test_corrupt_preserved_predecessor_is_not_overwritten(self):
        self.seed_legacy()
        before = (self.root / "models/epl.json").read_bytes()
        saved = cli.preserve_model_evidence(self.root, "epl", before)
        path = self.root / saved["path"].removeprefix("runtime/")
        path.write_bytes(b"corrupted test evidence")
        with patch("football_model.cli.fit_model") as fit:
            code, report = self.invoke()
        fit.assert_not_called()
        self.assertEqual(code, 1, report)
        self.assertEqual((self.root / "models/epl.json").read_bytes(), before)
        self.assertEqual(path.read_bytes(), b"corrupted test evidence")

    def test_migration_can_be_recorded_and_exactly_replayed_as_research(self):
        self.seed_legacy()
        code, report = self.invoke(record_replay=True)
        self.assertEqual(code, 0, report)
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "offline_replay"
            code, comparison = replay_bundle(self.root / report["replay_recording"]["path"], output)
            self.assertEqual(code, 0, comparison)
            self.assertTrue(comparison["outcomes_equal"])
            replayed = json.loads((output / "reports/latest.json").read_text())
            self.assertTrue(replayed["research_replay"])
            self.assertEqual(replayed["competitions"][0]["model_refresh"]["reasons"], ["missing_model_fitted_at"])
            self.assertFalse((output / "prospective").exists())


if __name__ == "__main__":
    unittest.main()
