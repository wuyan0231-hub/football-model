"""Regression tests for actual acquisition/completion clocks and legacy limits."""
from datetime import timedelta
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from football_model.cli import atomic_json, iso, run_pipeline
from football_model.integration import IntegrationError, build_index, match_packet
from football_model.model import fit_model, score_distribution
from test_cli import BASE_CONFIG, FakeClient, NOW


class Clock:
    def __init__(self, value=NOW):
        self.value = value

    def __call__(self):
        return self.value


class DelayedClient(FakeClient):
    def __init__(self, clock):
        super().__init__()
        self.clock = clock

    def fetch_fixtures(self, *args, **kwargs):
        response = super().fetch_fixtures(*args, **kwargs)
        self.clock.value = NOW + timedelta(minutes=10)
        response["fetched_at"] = iso(self.clock.value)
        return response


class TimeProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.runtime = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def read(self, path):
        return json.loads(path.read_text())

    def run_delayed(self):
        clock = Clock()

        def finish_fit(*args, **kwargs):
            result = fit_model(*args, **kwargs)
            clock.value = NOW + timedelta(minutes=11)
            return result

        def finish_forecast(*args, **kwargs):
            result = score_distribution(*args, **kwargs)
            clock.value = NOW + timedelta(minutes=12)
            return result

        with patch("football_model.cli.fit_model", side_effect=finish_fit), patch(
                "football_model.cli.score_distribution", side_effect=finish_forecast):
            code, report = run_pipeline(BASE_CONFIG, self.runtime, "train", now=NOW,
                client=DelayedClient(clock), clock=clock)
        self.assertEqual(code, 0, report)
        return report

    def test_real_acquisition_fit_and_forecast_are_distinct_from_cutoff(self):
        report = self.run_delayed()
        bundle = self.read(self.runtime / "models/epl.json")
        row = self.read(self.runtime / "predictions" / report["run_id"] / "epl.json")[0]
        self.assertEqual(report["run_started_at"], iso(NOW))
        self.assertEqual(bundle["model"]["training_cutoff"], iso(NOW))
        self.assertEqual(bundle["trained_at"], iso(NOW))
        self.assertEqual(bundle["data_observed_at"], iso(NOW + timedelta(minutes=10)))
        self.assertEqual(bundle["model_fitted_at"], iso(NOW + timedelta(minutes=11)))
        self.assertEqual(row["forecast_generated_at"], iso(NOW + timedelta(minutes=12)))
        self.assertEqual(report["generated_at"], row["forecast_generated_at"])
        packet = match_packet(self.runtime, BASE_CONFIG, "epl", 2000,
                              as_of=iso(NOW + timedelta(minutes=13)))
        self.assertTrue(packet["provenance"]["historical_availability_verified"])
        self.assertFalse(packet["provenance"]["source_publication_history_verified"])

    def test_original_late_source_repro_is_rejected(self):
        self.run_delayed()
        with self.assertRaisesRegex(IntegrationError, "前视"):
            match_packet(self.runtime, BASE_CONFIG, "epl", 2000,
                         as_of=iso(NOW + timedelta(minutes=1)))

    def test_backdating_cutoff_does_not_backdate_run_or_completion(self):
        current = NOW + timedelta(hours=1)
        code, report = run_pipeline(BASE_CONFIG, self.runtime, "train", now=NOW,
                                   client=FakeClient(), clock=lambda: current)
        self.assertEqual(code, 0, report)
        self.assertEqual(report["run_started_at"], iso(current))
        self.assertEqual(report["training_cutoff"], iso(NOW))
        self.assertEqual(report["generated_at"], iso(current))
        with self.assertRaises(IntegrationError):
            match_packet(self.runtime, BASE_CONFIG, "epl", 2000,
                         as_of=iso(NOW + timedelta(minutes=1)))

    def test_untrusted_future_envelope_blocks_fit(self):
        client = FakeClient()
        original = client.fetch_fixtures

        def future_envelope(*args, **kwargs):
            result = original(*args, **kwargs)
            result["fetched_at"] = iso(NOW + timedelta(minutes=10))
            return result

        client.fetch_fixtures = future_envelope
        code, report = run_pipeline(BASE_CONFIG, self.runtime, "train", now=NOW,
                                   client=client, clock=lambda: NOW)
        self.assertEqual(code, 1)
        self.assertIn("倒填时间", report["competitions"][0]["seasons"][0]["error"])
        self.assertFalse((self.runtime / "models/epl.json").exists())

    def test_source_envelopes_checked_independently_of_forecast_labels(self):
        report = self.run_delayed()
        report["competitions"][0]["seasons"][0]["fetched_at"] = iso(NOW + timedelta(minutes=14))
        atomic_json(self.runtime / "reports/history" / f"{report['run_id']}.json", report)
        with self.assertRaisesRegex(IntegrationError, "来源实际抓取"):
            match_packet(self.runtime, BASE_CONFIG, "epl", 2000,
                         as_of=iso(NOW + timedelta(minutes=15)))

    def test_completed_after_kickoff_is_not_published_as_prematch_prediction(self):
        def finish_late(*args, **kwargs):
            result = score_distribution(*args, **kwargs)
            clock.value = NOW + timedelta(days=2, seconds=1)
            return result

        clock = Clock()
        with patch("football_model.cli.score_distribution", side_effect=finish_late):
            code, report = run_pipeline(BASE_CONFIG, self.runtime, "train", now=NOW,
                                       client=FakeClient(), clock=clock)
        self.assertEqual(code, 0, report)
        row = self.read(self.runtime / "predictions" / report["run_id"] / "epl.json")[0]
        self.assertEqual(row["status"], "unsupported")
        self.assertNotIn("prediction", row)
        self.assertIn("已开赛", row["reason"])

    def test_existing_run_cannot_overwrite_immutable_outputs(self):
        _, report = run_pipeline(BASE_CONFIG, self.runtime, "train", now=NOW,
                                 client=FakeClient(), clock=lambda: NOW)
        path = self.runtime / "reports/history" / f"{report['run_id']}.json"
        original = path.read_bytes()
        with self.assertRaisesRegex(ValueError, "不可覆盖"):
            run_pipeline(BASE_CONFIG, self.runtime, "train", now=NOW,
                         client=FakeClient(), clock=lambda: NOW)
        self.assertEqual(path.read_bytes(), original)

    def test_legacy_schema_with_actual_fit_time_reuses_and_retains_current_only_limit(self):
        code, report = run_pipeline(BASE_CONFIG, self.runtime, "train", now=NOW,
                                   client=FakeClient(), clock=lambda: NOW)
        self.assertEqual(code, 0)
        model_path = self.runtime / "models/epl.json"
        bundle = self.read(model_path)
        # Keep the actual completion emitted by the real initial fit. An old
        # schema alone must not force migration; missing completion evidence
        # with upcoming fixtures now intentionally does (test_s2_fit_migration).
        actual_fitted_at = bundle["model_fitted_at"]
        for key in ("time_semantics", "run_started_at", "training_cutoff", "data_observed_at"):
            bundle.pop(key)
        bundle["schema_version"] = 1
        atomic_json(model_path, bundle)
        before = model_path.read_bytes()
        with patch("football_model.cli.fit_model", side_effect=AssertionError("must not refit unchanged data")):
            code, newer = run_pipeline(BASE_CONFIG, self.runtime, "train", now=NOW+timedelta(hours=2),
                client=FakeClient(), clock=lambda: NOW+timedelta(hours=2))
        self.assertEqual(code, 0, newer)
        self.assertEqual(model_path.read_bytes(), before)
        self.assertFalse(newer["competitions"][0]["model_refresh"]["required"])
        with patch("football_model.integration.datetime") as real_clock:
            real_clock.now.return_value = NOW+timedelta(hours=3)
            packet = match_packet(self.runtime, BASE_CONFIG, "epl", 2000)
        self.assertEqual(packet["provenance"]["time_semantics"], "legacy_time_evidence_limited")
        self.assertEqual(packet["provenance"]["model_fitted_at"], actual_fitted_at)
        with self.assertRaisesRegex(IntegrationError, "旧产物时间证据有限"):
            match_packet(self.runtime, BASE_CONFIG, "epl", 2000,
                         as_of=iso(NOW+timedelta(hours=3)))

    def test_untouched_legacy_index_and_forecast_remain_current_readable(self):
        _, report = run_pipeline(BASE_CONFIG, self.runtime, "train", now=NOW,
                                 client=FakeClient(), clock=lambda: NOW)
        model_path = self.runtime / "models/epl.json"
        prediction_path = self.runtime / "predictions" / report["run_id"] / "epl.json"
        report_path = self.runtime / "reports/history" / f"{report['run_id']}.json"
        bundle, rows = self.read(model_path), self.read(prediction_path)
        for item in (bundle, rows[0], report, report["competitions"][0]):
            for key in ("schema_version", "time_semantics", "run_started_at", "training_cutoff",
                        "model_fitted_at", "forecast_generated_at", "model_time_evidence"):
                item.pop(key, None)
        bundle.pop("data_observed_at", None)
        bundle["schema_version"] = 1
        report["competitions"][0].pop("data_observed_at", None)
        for season in bundle["source_seasons"] + report["competitions"][0]["seasons"]:
            season.pop("received_at", None)
        atomic_json(model_path, bundle)
        atomic_json(prediction_path, rows)
        atomic_json(report_path, report)
        build_index(self.runtime, BASE_CONFIG)
        before = {p: p.read_bytes() for p in (model_path, prediction_path, report_path)}
        with patch("football_model.integration.datetime") as current:
            current.now.return_value = NOW + timedelta(hours=1)
            result = match_packet(self.runtime, BASE_CONFIG, "epl", 2000)
        self.assertEqual(result["provenance"]["time_semantics"], "legacy_time_evidence_limited")
        self.assertEqual(before, {p: p.read_bytes() for p in before})


if __name__ == "__main__":
    unittest.main()
