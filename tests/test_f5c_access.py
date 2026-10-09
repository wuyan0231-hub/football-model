"""Counterexamples for original-only F5-C access and report evidence binding."""
import copy
from datetime import timedelta
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from football_model import f5c_access as access, state_storage, total_shrink_shadow as ts
from football_model.prospective import _objects
from tests import test_total_shrink_shadow as fixture_helpers


class F5CAccessTests(unittest.TestCase):
    def setUp(self):
        self.fx = fixture_helpers.TotalShrinkShadowTests(methodName="runTest")
        self.fx.setUp()
        self.addCleanup(self.fx.tearDown)
        code, report = self.fx.capture()
        self.assertEqual(code, 0, report)
        self.runtime = self.fx.runtime
        self.forecast = _objects(self.fx.root, "forecasts")[0]

    def call(self, **kwargs):
        return access.comparison(self.runtime, "epl", 1001, now=self.fx.now, **kwargs)

    def test_same_parent_and_all_five_arms_without_fit_or_original_changes(self):
        before = {p: p.read_bytes() for p in self.runtime.rglob("*.json")}
        access.build_index(self.runtime, state_commit="c" * 40)
        with patch("football_model.model.fit_model", side_effect=AssertionError("no fit")), \
             patch("football_model.shadow_models.fit_model", side_effect=AssertionError("no fit")):
            packet = self.call(row=self.fx.rows[0])
        self.assertEqual(packet["status"], "verified_same_parent", packet)
        self.assertEqual(packet["baseline"], self.fx.rows[0]["prediction"])
        self.assertEqual(packet["predictions"]["alpha_0"], packet["baseline"])
        self.assertEqual(len(packet["predictions"]), 5)
        self.assertEqual(packet["selection"]["n"], 0)
        self.assertFalse(packet["confirmation_eligible"])
        self.assertEqual(packet["remote_persistence"]["status"], "not_verified_by_this_reader")
        self.assertTrue(packet["evidence"])
        for path, raw in before.items():
            self.assertEqual(path.read_bytes(), raw)

    def test_current_model_run_or_fixture_mismatch_cannot_reuse_older_candidate(self):
        for field, replacement in (("model_id", "new-model"), ("home_id", 3), ("kickoff", "2022-05-07T00:00:00Z")):
            row = copy.deepcopy(self.fx.rows[0])
            row[field] = replacement
            with self.subTest(field=field):
                result = self.call(row=row)
                self.assertEqual(result["status"], "rejected", result)
                self.assertNotIn("predictions", result)

    def test_receipt_after_analysis_cutoff_cannot_be_backfilled(self):
        packet = access.comparison(self.runtime, "epl", 1001, row=self.fx.rows[0],
                                   now=self.fx.now - timedelta(seconds=1))
        self.assertEqual(packet["status"], "rejected", packet)
        self.assertIn("recorded_after_analysis_cutoff", str(packet))

    def test_missing_fixture_retains_capacity_failure_as_distinct_attempt(self):
        self.fx.write("total_shrink_shadow/reports/latest.json", {"status": "capacity_deferred", "reason": "space"})
        packet = access.comparison(self.runtime, "epl", 9999, now=self.fx.now)
        self.assertEqual(packet["status"], "not_captured", packet)
        self.assertEqual(packet["latest_attempts"]["capture"]["status"], "capacity_deferred")
        self.assertNotIn("predictions", packet)

    def test_corrupt_parent_and_forged_index_never_become_success(self):
        parent = self.fx.root / "production_parents" / (self.forecast["production_parent_id"] + ".json")
        parent.write_text('{}')
        access.build_index(self.runtime)
        packet = self.call(row=self.fx.rows[0])
        self.assertEqual(packet["status"], "rejected", packet)
        self.assertIn("digest_mismatch", str(packet))

    def test_export_rejects_existing_runtime_and_symlink_outputs_before_writes(self):
        packet = self.call(row=self.fx.rows[0])
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for path in (self.runtime / "models/epl.json", self.runtime / "new.html"):
                with self.subTest(path=path), self.assertRaisesRegex(ValueError, "new_and_outside"):
                    access.export_report(packet, self.runtime, path, root / "new.zip")
            existing = root / "existing.html"
            existing.write_text("original")
            with self.assertRaises(ValueError):
                access.export_report(packet, self.runtime, existing, root / "new.zip")
            self.assertEqual(existing.read_text(), "original")
            alias = root / "alias"
            alias.symlink_to(self.runtime, target_is_directory=True)
            with self.assertRaises(ValueError):
                access.export_report(packet, self.runtime, alias / "new.html", root / "new.zip")
            self.assertFalse((root / "new.zip").exists())

    def test_independent_saved_score_is_recomputed_not_inferred_from_user_score(self):
        self.fx.now += timedelta(days=2)
        self.fx.result(home=2, away=1)
        code, report = ts.score(self.runtime, clock=self.fx.clock)
        self.assertEqual(code, 0, report)
        packet = self.call(forecast_id=self.forecast["object_id"])
        self.assertEqual(packet["status"], "verified_saved_original_audit", packet)
        self.assertEqual(packet["score"]["status"], "scored")
        self.assertEqual(packet["score"]["candidate_minus_baseline"]["total_goals_log_loss"], 0)
        self.assertTrue(any('/results/' in e["path"] for e in packet["evidence"]))
        self.assertEqual(packet["selection"]["n"], 0)

    def test_metric_comparison_rejects_nonfinite_boolean_and_material_change(self):
        expected = {"alpha_0": {"loss": 0.8}}
        self.assertTrue(access._metrics_equal({"alpha_0": {"loss": 0.8 + 1e-15}}, expected))
        for value in (float("nan"), float("inf"), True, 0.9):
            with self.subTest(value=value):
                self.assertFalse(access._metrics_equal({"alpha_0": {"loss": value}}, expected))

    def test_old_transport_selectively_restores_closure_and_exports_exact_originals(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            tree = root / "tree"
            tree.mkdir()
            state_storage.pack_runtime(self.runtime, tree)
            manifest_path = tree / state_storage.MANIFEST_NAME
            manifest = json.loads(manifest_path.read_text())
            manifest.update(logical_limit_bytes=2 * 1024**3, physical_limit_bytes=500 * 1024**2)
            manifest_path.write_text(json.dumps(manifest))
            # Unrelated archive members need not be materialized or read.
            selected = self.forecast["object_id"]
            isolated = root / "isolated"
            isolated.mkdir()
            packet = access.comparison(isolated, "epl", 1001, now=self.fx.now,
                                       forecast_id=selected, state_tree=tree)
            self.assertEqual(packet["status"], "verified_saved_original_audit", packet)
            self.assertFalse(any(isolated.iterdir()))
            report, archive_path = root / "report.html", root / "report.zip"
            access.export_report(packet, isolated, report, archive_path, state_tree=tree)
            self.assertIn("alpha_0.25", report.read_text())
            with zipfile.ZipFile(archive_path) as archive:
                manifest = json.loads(archive.read('manifest.json'))
                for entry in manifest["files"]:
                    self.assertEqual(hashlib.sha256(archive.read(entry["path"])).hexdigest(), entry["sha256"])
                for evidence in packet["evidence"]:
                    self.assertEqual(archive.read('originals/' + evidence["path"]), (self.runtime/evidence["path"]).read_bytes())


if __name__ == "__main__":
    unittest.main()
