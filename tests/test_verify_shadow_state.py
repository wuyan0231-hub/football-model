"""Offline diagnostics test integrity, failures and safe logs, not model gains."""
import copy
from datetime import timedelta
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from unittest.mock import patch

from football_model.prospective import _append
from football_model.model import score_distribution
from tests import test_shadow_models as fixtures

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("verify_shadow_state", ROOT / "scripts/verify_shadow_state.py")
diagnostic = importlib.util.module_from_spec(spec)
spec.loader.exec_module(diagnostic)


def hashes(root):
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob("*") if p.is_file()}


class VerifyShadowStateTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ShadowModelTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        code, _ = self.fixture.run_capture()
        self.assertEqual(code, 0)
        self.runtime = self.fixture.runtime
        self.parent = self.runtime.parent
        self.now = fixtures.NOW + timedelta(hours=2)

    def test_every_saved_forecast_is_checked_without_writes_fit_or_network(self):
        before = hashes(self.runtime)
        forbidden = AssertionError("diagnostic must not mutate, fit or request")
        with patch("football_model.shadow_models.fit_model", side_effect=forbidden), \
                patch("football_model.shadow_models.capture", side_effect=forbidden), \
                patch("football_model.shadow_models.score", side_effect=forbidden), \
                patch("socket.socket.connect", side_effect=forbidden):
            report = diagnostic.verify(self.runtime, now=self.now)
        self.assertEqual(report["status"], "passed")
        self.assertEqual((report["forecast_count"], report["passed_count"], report["failed_count"]), (3, 3, 0))
        self.assertEqual(report["state"]["scope"], "local_runtime_scope_not_full_cloud_restore")
        self.assertEqual(hashes(self.runtime), before)

    def test_corrupt_forecast_does_not_stop_validation_of_other_records(self):
        path = sorted((self.runtime / "shadow_models/forecasts").glob("*.json"))[0]
        value = json.loads(path.read_text())
        value["prediction"]["home_name"] = "secret name must not enter diagnostics"
        path.write_text(json.dumps(value))
        report = diagnostic.verify(self.runtime, now=self.now)
        self.assertEqual((report["forecast_count"], report["passed_count"], report["failed_count"]), (3, 2, 1))
        self.assertEqual(report["failures"][0]["error"], "immutable_forecast_hash_mismatch")
        self.assertNotIn("secret name", json.dumps(report))

    def test_replay_failure_identifies_numeric_field_and_redacts_strings(self):
        def altered(*args, **kwargs):
            value = copy.deepcopy(score_distribution(*args, **kwargs))
            value["probabilities"]["home_win"] += 1e-10
            value["home_name"] = "sensitive name from parameter source"
            return value
        with patch("football_model.shadow_models.score_distribution", side_effect=altered), \
                patch.object(diagnostic, "score_distribution", side_effect=altered):
            report = diagnostic.verify(self.runtime, now=self.now)
        self.assertEqual(report["failed_count"], 3)
        for failure in report["failures"]:
            self.assertEqual(failure["error"], "shadow_prediction_does_not_recompute")
            replay = failure["replay"]
            fields = {d["field"]: d for d in replay["nonmatrix_differences"]}
            self.assertAlmostEqual(fields["probabilities.home_win"]["absolute_difference"], 1e-10)
            self.assertEqual(fields["home_name"]["kind"], "value_mismatch")
            self.assertEqual(replay["matrix_max_abs_diff"], 0)
        self.assertNotIn("sensitive name", json.dumps(report))

    def test_all_failures_counted_but_details_and_stdout_are_bounded(self):
        for index in range(23):
            _append(self.runtime / "shadow_models", "forecasts", {"private": "secret " + str(index)})
        report = diagnostic.verify(self.runtime, now=self.now)
        self.assertEqual((report["forecast_count"], report["passed_count"], report["failed_count"]), (26, 3, 23))
        self.assertEqual(len(report["failures"]), 20)
        self.assertEqual(report["omitted_failure_details"], 3)
        stdout = diagnostic.compact_stdout(report)
        self.assertLess(len(stdout.encode()), 20_000)
        self.assertNotIn("secret", stdout)
        # Even an unusually large diagnostic never overruns the log bound.
        report["failures"][0]["replay"] = {"nonmatrix_differences": [{"field": "warnings.0", "kind": "value_mismatch"}] * 1000}
        self.assertLess(len(diagnostic.compact_stdout(report).encode()), 20_000)

    def test_cloud_marker_binds_exact_loaded_state_and_source(self):
        marker = self.parent / "football-cloud-state.json"
        value = {"schema_version": 1, "state_oid": "b" * 40, "source_sha": "a" * 40,
                 "source_ref": "refs/heads/main", "loaded_at": fixtures.iso(self.now - timedelta(minutes=1))}
        marker.write_text(json.dumps(value))
        with patch.dict("os.environ", {"GITHUB_SHA": "a" * 40}):
            report = diagnostic.verify(self.runtime, state_marker=marker, now=self.now)
            self.assertEqual(report["status"], "passed")
            self.assertEqual(report["state"]["state_commit"], "b" * 40)
        with patch.dict("os.environ", {"GITHUB_SHA": "c" * 40}):
            report = diagnostic.verify(self.runtime, state_marker=marker, now=self.now)
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["setup_failure"]["error"], "state_marker_source_mismatch")
        self.assertEqual(report["forecast_count"], 0)

    def test_cli_output_cannot_overwrite_runtime_or_marker_and_empty_scope_fails(self):
        marker = self.parent / "marker.json"
        marker.write_text("{}")
        before = hashes(self.parent)
        for output in (self.runtime / "diagnostic.json", marker):
            with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                diagnostic.main(["--runtime", str(self.runtime), "--state-marker", str(marker), "--output", str(output)])
        self.assertEqual(hashes(self.parent), before)
        empty = self.parent / "empty-runtime"
        empty.mkdir()
        out = self.parent / "diagnostic.json"
        log = io.StringIO()
        with redirect_stdout(log):
            code = diagnostic.main(["--runtime", str(empty), "--output", str(out)])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out.read_text())["setup_failure"]["error"], "runtime_no_saved_forecasts")
        self.assertLess(len(log.getvalue().encode()), 20_000)


if __name__ == "__main__":
    unittest.main()
