"""A recorded run and its offline replay must follow the same production gates."""
import copy
from contextlib import contextmanager
from datetime import timedelta
import json
import math
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from football_model.cli import run_pipeline
from football_model.data import APIError, RequestBudgetError
from football_model.model import fit_model
from football_model.production_replay import replay_bundle, read_bundle, compare_outcomes, COMPARISON_POLICY, ReplayError, _outcome
from test_cli import BASE_CONFIG, BudgetClient, FakeClient, NOW, fixture


class ProductionReplayTests(unittest.TestCase):
    def record_and_replay(self, root, *, config=None, now=NOW, client=None, mode="train", league="epl"):
        config = config or BASE_CONFIG
        runtime = Path(root) / "production"
        code, report = run_pipeline(config, runtime, mode, league, now=now,
            clock=lambda: now, client=client or FakeClient(), record_replay=True)
        self.assertEqual(report["replay_recording"]["status"], "recorded", report)
        bundle = runtime / report["replay_recording"]["path"]
        output = Path(root) / "research"
        with patch("football_model.data.APIClient", side_effect=AssertionError("network factory forbidden")):
            replay_code, result = replay_bundle(bundle, output)
        self.assertEqual(replay_code, 0, result)
        self.assertTrue(result["outcomes_equal"])
        self.assertTrue(result["research_replay"])
        replay_report = json.loads((output / "reports/latest.json").read_text())
        self.assertTrue(replay_report["research_replay"])
        self.assertFalse((output / "integration/index.json").exists())
        for file in (output / "predictions").glob("*/*.json"):
            self.assertTrue(all(row["research_replay"] for row in json.loads(file.read_text())))
        return code, report, bundle, output

    def test_first_fit_reproduces_all_predictions_and_keeps_status_private(self):
        client = BudgetClient()
        client.status = lambda: {"account": {"email": "private@example.test"}, "api_key": "NOT_FOR_RECORDING"}
        with tempfile.TemporaryDirectory() as tmp:
            _, report, bundle, _ = self.record_and_replay(tmp, client=client)
            serialized = json.dumps(read_bundle(bundle))
            self.assertNotIn("private@example.test", serialized)
            self.assertNotIn("NOT_FOR_RECORDING", serialized)
            self.assertEqual(report["competitions"][0]["model_change"], "完成新拟合")
            self.assertEqual(json.loads(serialized)["payload"]["initial_state"], {})

    def test_unchanged_reuses_and_snapshots_only_requested_league(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp) / "production"
            run_pipeline(BASE_CONFIG, runtime, "train", now=NOW, clock=lambda: NOW, client=FakeClient())
            foreign = runtime / "models/laliga.json"
            foreign.write_text('{"private_unrelated_state":true}')
            later = NOW + timedelta(hours=2)
            with patch("football_model.cli.fit_model", side_effect=AssertionError("must reuse")):
                _, report, bundle, _ = self.record_and_replay(tmp, now=later)
            item = report["competitions"][0]
            self.assertEqual(item["model_change"], "数据未变，沿用参数")
            state = read_bundle(bundle)["payload"]["initial_state"]
            self.assertEqual(len(state), 2)  # selected model + its matching validation
            self.assertIn("models/epl.json", state)
            self.assertNotIn("models/laliga.json", state)

    def test_unchanged_but_expired_actually_refits_in_both_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp) / "production"
            run_pipeline(BASE_CONFIG, runtime, "train", now=NOW, clock=lambda: NOW, client=FakeClient())
            later = NOW + timedelta(days=15)
            client = FakeClient()
            client.fixtures[-1] = fixture(2001, later + timedelta(days=2), 1, 2, None, None, "NS")
            with patch("football_model.cli.fit_model", wraps=fit_model) as actual:
                _, report, _, _ = self.record_and_replay(tmp, now=later, client=client)
            self.assertEqual(actual.call_count, 2)
            item = report["competitions"][0]
            self.assertTrue(item["model_refresh"]["performed"])
            self.assertFalse(item["model_refresh"]["data_changed"])
            self.assertIn("expired_at_run", item["model_refresh"]["reasons"])

    def test_failed_expiry_refit_reproduces_failure_and_preserves_initial_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp) / "production"
            run_pipeline(BASE_CONFIG, runtime, "train", now=NOW, clock=lambda: NOW, client=FakeClient())
            original = (runtime / "models/epl.json").read_bytes()
            later = NOW + timedelta(days=15)
            client = FakeClient()
            client.fixtures[-1] = fixture(2001, later + timedelta(days=2), 1, 2, None, None, "NS")
            with patch("football_model.cli.fit_model", side_effect=RuntimeError("synthetic optimizer failure")):
                code, report, _, output = self.record_and_replay(tmp, now=later, client=client)
            self.assertEqual(code, 1)
            self.assertFalse(report["competitions"][0]["model_refresh"]["performed"])
            self.assertEqual((runtime / "models/epl.json").read_bytes(), original)
            self.assertEqual((output / "models/epl.json").read_bytes(), original)

    def test_valid_prospective_settings_can_be_recorded(self):
        config = {**BASE_CONFIG, "prospective": {"contracts": [
            {"market": "total_goals", "side": "over", "line": 2.25}]}}
        with tempfile.TemporaryDirectory() as tmp:
            self.record_and_replay(tmp, config=config)

    def test_expired_empty_window_never_refits(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp) / "production"
            run_pipeline(BASE_CONFIG, runtime, "train", now=NOW, clock=lambda: NOW, client=FakeClient())
            later = NOW + timedelta(days=15)
            with patch("football_model.cli.fit_model", side_effect=AssertionError("must not refit empty window")):
                _, report, _, _ = self.record_and_replay(tmp, now=later)
            self.assertEqual(report["competitions"][0]["forecast_availability"]["status"], "no_upcoming")
            self.assertEqual(report["competitions"][0]["model_freshness"]["status"], "stale")

    def test_identity_and_download_failures_are_replayed(self):
        for failure in ("identity", "download", "status"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as tmp:
                client = FakeClient(failing=failure == "download")
                if failure == "identity":
                    client.fixtures[0]["league"]["id"] = 123
                if failure == "status":
                    client.status = lambda: (_ for _ in ()).throw(RequestBudgetError("额度耗尽"))
                code, report, _, _ = self.record_and_replay(tmp, client=client)
                self.assertEqual(code, 2 if failure == "status" else 1)
                self.assertIn("fatal_error", report) if failure == "status" else self.assertEqual(report["competitions"][0]["status"], "未完成")

    def test_kickoff_age_gate_is_not_bypassed(self):
        config = {**BASE_CONFIG, "forecast_days": 21}
        client = FakeClient()
        client.fixtures[-1] = fixture(2001, NOW + timedelta(days=20), 1, 2, None, None, "NS")
        with tempfile.TemporaryDirectory() as tmp:
            _, report, _, _ = self.record_and_replay(tmp, config=config, client=client)
            self.assertEqual(report["competitions"][0]["forecast_count"], 0)
            self.assertEqual(report["competitions"][0]["forecast_availability"]["status"], "unavailable")


    def test_default_off_and_recording_failure_preserves_predictions(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp) / "production"
            _, report = run_pipeline(BASE_CONFIG, runtime, "train", now=NOW, clock=lambda: NOW, client=FakeClient())
            self.assertNotIn("replay_recording", report)
            self.assertFalse((runtime / "replay").exists())
            later = NOW + timedelta(hours=2)
            with patch("football_model.production_replay.Recorder.save", side_effect=OSError("disk full")):
                code, failed = run_pipeline(BASE_CONFIG, runtime, "train", now=later,
                    clock=lambda: later, client=FakeClient(), record_replay=True)
            self.assertEqual(code, 0)
            self.assertEqual(failed["replay_recording"]["status"], "failed")
            self.assertTrue((runtime / "predictions" / failed["run_id"] / "epl.json").exists())

    def test_rejects_overwrite_tamper_and_changed_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, _, bundle, output = self.record_and_replay(tmp)
            with self.assertRaisesRegex(ReplayError, "新目录"):
                replay_bundle(bundle, output)
            with self.assertRaisesRegex(ReplayError, "runtime"):
                replay_bundle(bundle, Path(tmp) / "production/child")
            altered = read_bundle(bundle)
            altered["payload"]["events"] = []
            bad = Path(tmp) / "tampered.json"
            bad.write_text(json.dumps(altered))
            with self.assertRaisesRegex(ReplayError, "指纹"):
                replay_bundle(bad, Path(tmp) / "new")
            with patch("football_model.production_replay._sources", return_value={}):
                with self.assertRaisesRegex(ReplayError, "源码"):
                    replay_bundle(bundle, Path(tmp) / "changed")
            self.assertFalse((Path(tmp) / "new").exists())


class ReplayComparisonTests(unittest.TestCase):
    @staticmethod
    def tail(value):
        return {"predictions": {"epl": [{"prediction": {"tail_mass_before_normalization": value}}]}}

    def test_exact_comparison_preserves_structure_types_and_all_values(self):
        value = {"x": [1, 1.0, True, None, "2026-10-06T15:00:00Z", {"rate": 2.3}]}
        comparison = compare_outcomes(value, copy.deepcopy(value))
        self.assertTrue(comparison["exact_outcomes_equal"])
        self.assertTrue(comparison["numerically_consistent"])
        self.assertEqual(comparison["difference_count"], 0)
        self.assertEqual(comparison["policy"], COMPARISON_POLICY)

    def test_one_ulp_difference_is_numerical_but_never_exact(self):
        comparison = compare_outcomes({"p": 0.25}, {"p": math.nextafter(0.25, math.inf)})
        self.assertFalse(comparison["exact_outcomes_equal"])
        self.assertTrue(comparison["numerically_consistent"])
        self.assertTrue(comparison["strict_relative_consistent"])
        self.assertEqual(comparison["float_difference_count"], 1)
        self.assertGreater(comparison["maximum_absolute_error"], 0)
        self.assertEqual(comparison["differences"][0]["path"], "/p")

    def test_only_exact_tail_field_allows_cancellation_absolute_bound(self):
        old, new = 1e-9, 1e-9 + 1e-16
        comparison = compare_outcomes(self.tail(old), self.tail(new))
        self.assertFalse(comparison["exact_outcomes_equal"])
        self.assertFalse(comparison["strict_relative_consistent"])
        self.assertTrue(comparison["numerically_consistent"])
        self.assertEqual(comparison["differences"][0]["absolute_tolerance"], 1e-12)
        self.assertFalse(compare_outcomes({"p": old}, {"p": new})["numerically_consistent"])
        # A similarly named arbitrary field must not receive the exception.
        self.assertFalse(compare_outcomes({"tail_mass_before_normalization": old},
                                        {"tail_mass_before_normalization": new})["numerically_consistent"])
        self.assertFalse(compare_outcomes(self.tail(old), self.tail(old + 1e-10))["numerically_consistent"])

    def test_tiny_nonzero_probability_changes_do_not_hide_under_absolute_floor(self):
        for old, new in ((1e-30, 1e-13), (1e-300, 2e-300), (0.1, 0.10000001)):
            with self.subTest(old=old, new=new):
                comparison = compare_outcomes({"p": old}, {"p": new})
                self.assertFalse(comparison["numerically_consistent"])
                self.assertFalse(comparison["exact_outcomes_equal"])

    def test_zero_and_sign_transitions_rejected_even_for_tail(self):
        for old, new in ((0.0, 1e-20), (1e-20, 0.0), (-1e-20, 1e-20), (-0.0, 0.0)):
            with self.subTest(old=old, new=new):
                comparison = compare_outcomes(self.tail(old), self.tail(new))
                self.assertFalse(comparison["numerically_consistent"])
                self.assertFalse(comparison["exact_outcomes_equal"])

    def test_discrete_structure_and_scalar_changes_always_fail(self):
        for old, new in ((1, 1.0), (True, 1), (1, 2), ("20:00", "20:01"),
                         ({"a": 1}, {"b": 1}), ([1], [1, 2]), (None, 0)):
            with self.subTest(old=old, new=new):
                comparison = compare_outcomes(old, new)
                self.assertFalse(comparison["numerically_consistent"])
                self.assertGreater(comparison["non_float_difference_count"], 0)

    def test_nonfinite_never_passes_and_diagnostics_are_json_safe(self):
        for old, new in ((math.nan, math.nan), (math.inf, math.inf), (1.0, math.inf),
                         (-1e308, 1e308)):
            with self.subTest(old=old, new=new):
                comparison = compare_outcomes(old, new)
                self.assertFalse(comparison["numerically_consistent"])
                json.dumps(comparison, allow_nan=False)

    def test_replay_status_separates_numeric_exact_and_meaningful_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp) / "production"
            _, report = run_pipeline(BASE_CONFIG, runtime, "train", now=NOW,
                                     clock=lambda: NOW, client=FakeClient(), record_replay=True)
            bundle = runtime / report["replay_recording"]["path"]
            self.assertEqual(read_bundle(bundle)["payload"]["comparison_policy"], COMPARISON_POLICY)
            for change, wanted in (("roundoff", "numerically_consistent"), ("meaningful", "mismatch"),
                                   ("discrete", "mismatch")):
                def changed_outcome(report, runtime, change=change):
                    actual = _outcome(report, runtime)
                    if change == "discrete":
                        actual["api_requests"] += 1
                    else:
                        matrix = actual["predictions"]["epl"][0]["prediction"]["score_matrix"]
                        matrix[1][1] = (math.nextafter(matrix[1][1], math.inf) if change == "roundoff"
                                         else matrix[1][1] + 1e-7)
                    return actual
                with self.subTest(change=change), patch("football_model.production_replay._outcome", side_effect=changed_outcome):
                    code, result = replay_bundle(bundle, Path(tmp) / ("research_" + change))
                self.assertEqual(result["status"], wanted)
                self.assertFalse(result["outcomes_equal"])
                self.assertFalse(result["exact_outcomes_equal"])
                self.assertEqual(result["numerical_consistency"], change == "roundoff")
                self.assertEqual(code, 0 if change == "roundoff" else 1)


if __name__ == "__main__":
    unittest.main()
