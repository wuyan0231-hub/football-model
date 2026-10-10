"""Counterexamples for original-only F5-C access and report evidence binding."""
import copy
from datetime import timedelta
import hashlib
import json
from pathlib import Path
import re
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

    def test_new_current_parent_does_not_relabel_the_frozen_primary_scoring_version(self):
        self.fx.now += timedelta(minutes=1)
        self.fx.publish([1001], fixture_helpers.NOW + timedelta(days=1), run_id="r2")
        code, report = self.fx.capture()
        self.assertEqual(code, 0, report)
        code, report = ts.score(self.runtime, clock=self.fx.clock)
        self.assertEqual(code, 0, report)
        access.build_index(self.runtime)
        packet = self.call(row=self.fx.rows[0])
        self.assertEqual(packet["status"], "verified_same_parent", packet)
        self.assertEqual(packet["score"]["status"], "not_primary_evaluation_version")
        self.assertEqual(packet["score"]["primary_records"][0]["forecast_id"], self.forecast["object_id"])

    def test_missing_fixture_retains_capacity_failure_as_distinct_attempt(self):
        self.fx.write("total_shrink_shadow/reports/latest.json", {"status": "capacity_deferred", "reason": "space"})
        packet = access.comparison(self.runtime, "epl", 9999, now=self.fx.now)
        self.assertEqual(packet["status"], "not_captured", packet)
        self.assertEqual(packet["latest_attempts"]["capture"]["status"], "capacity_deferred")
        self.assertNotIn("predictions", packet)
        name = "total_shrink_shadow/reports/latest.json"
        self.assertIn(name, [entry["path"] for entry in packet["attempt_evidence"]])
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            access.export_report(packet, self.runtime, root / "report.html", root / "report.zip")
            with zipfile.ZipFile(root / "report.zip") as archive:
                self.assertEqual(archive.read("attempts/" + name), (self.runtime / name).read_bytes())

    def test_corrupt_parent_and_forged_index_never_become_success(self):
        parent = self.fx.root / "production_parents" / (self.forecast["production_parent_id"] + ".json")
        parent.write_text('{}')
        access.build_index(self.runtime)
        packet = self.call(row=self.fx.rows[0])
        self.assertEqual(packet["status"], "rejected", packet)
        self.assertIn("digest_mismatch", str(packet))

    def test_non_object_index_is_rejected_but_score_summary_keeps_verified_forecast(self):
        self.fx.write(access.INDEX, [])
        packet = self.call(row=self.fx.rows[0])
        self.assertEqual(packet["status"], "rejected", packet)
        (self.runtime / access.INDEX).unlink()
        self.fx.write('total_shrink_shadow/reports/score-latest.json', [])
        packet = self.call(row=self.fx.rows[0])
        self.assertEqual(packet["status"], "verified_same_parent", packet)
        self.assertEqual(packet["score"]["status"], "rejected")
        self.assertEqual(packet["baseline"], self.fx.rows[0]["prediction"])
        self.assertEqual(packet["latest_attempts"]["score"]["status"], "report_invalid")
        self.assertEqual(packet["diagnostic_evidence"][0]["path"], 'total_shrink_shadow/reports/score-latest.json')

    def test_pending_score_is_distinct_from_saved_result_rejection(self):
        _, report = ts.score(self.runtime, clock=self.fx.clock)
        packet = self.call(row=self.fx.rows[0])
        self.assertEqual(packet["score"]["status"], "pending", packet)
        self.assertEqual(packet["score"]["observed_records"][0]["reason"], "no_independent_result_available")
        self.fx.now += timedelta(days=2)
        self.fx.result(league_id=999)
        code, report = ts.score(self.runtime, clock=self.fx.clock)
        self.assertEqual(code, 1, report)
        packet = self.call(row=self.fx.rows[0])
        self.assertEqual(packet["status"], "verified_same_parent", packet)
        self.assertEqual(packet["score"]["status"], "rejected")
        self.assertEqual(packet["score"]["reason"], "saved_score_attempt_rejected")
        self.assertTrue(packet["score"]["observed_records"][0]["reason"])

    def test_missing_score_original_does_not_reject_verified_prediction(self):
        self.fx.now += timedelta(days=2)
        self.fx.result()
        code, report = ts.score(self.runtime, clock=self.fx.clock)
        self.assertEqual(code, 0, report)
        sid = report["matches"][0]["score_id"]
        (self.fx.root / "scores" / (sid + ".json")).unlink()
        packet = self.call(row=self.fx.rows[0])
        self.assertEqual(packet["status"], "verified_same_parent", packet)
        self.assertEqual(packet["score"]["status"], "read_failed")
        self.assertNotIn("score_id", packet["score"])
        self.assertEqual(len(packet["predictions"]), 5)

    def test_malformed_immutable_score_run_does_not_escape_reader(self):
        from football_model.prospective import _append
        run_id = _append(self.fx.root, "score_runs", {"version": ts.VERSION,
            "scored_at": fixture_helpers.iso(self.fx.now), "matches": [None]})
        self.fx.write("total_shrink_shadow/reports/score-latest.json", {"report_id": run_id})
        packet = self.call(row=self.fx.rows[0])
        self.assertEqual(packet["status"], "verified_same_parent", packet)
        self.assertEqual(packet["score"]["status"], "rejected")
        self.assertIn("f5c_score_run_invalid", packet["score"]["reason"])

    def test_hash_consistent_but_wrong_score_is_separately_rejected_and_exported(self):
        from football_model.prospective import _append
        self.fx.now += timedelta(days=2)
        self.fx.result()
        code, report = ts.score(self.runtime, clock=self.fx.clock)
        self.assertEqual(code, 0, report)
        forged_score = {k: v for k, v in report["matches"][0].items() if k != "score_id"}
        forged_score["arms"]["alpha_0"]["total_goals_log_loss"] += .5
        sid = _append(self.fx.root, "scores", forged_score)
        changed = {k: v for k, v in report.items() if k != "report_id"}
        changed["matches"][0] = {**forged_score, "score_id": sid}
        rid = _append(self.fx.root, "score_runs", changed)
        self.fx.write("total_shrink_shadow/reports/score-latest.json", {"report_id": rid})
        packet = self.call(row=self.fx.rows[0])
        self.assertEqual(packet["status"], "verified_same_parent", packet)
        self.assertEqual(packet["score"]["status"], "rejected")
        self.assertIn("does_not_recompute", packet["score"]["reason"])
        self.assertNotIn("score_id", packet["score"])
        diagnostic_path = f"total_shrink_shadow/scores/{sid}.json"
        self.assertIn(diagnostic_path, [e["path"] for e in packet["diagnostic_evidence"]])
        self.assertNotIn(diagnostic_path, [e["path"] for e in packet["evidence"]])
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            access.export_report(packet, self.runtime, root / "report.html", root / "report.zip")
            with zipfile.ZipFile(root / "report.zip") as archive:
                self.assertEqual(archive.read("diagnostics/" + diagnostic_path),
                                 (self.runtime / diagnostic_path).read_bytes())
                manifest = json.loads(archive.read("manifest.json"))
                for entry in manifest["files"]:
                    self.assertEqual(hashlib.sha256(archive.read(entry["path"])).hexdigest(), entry["sha256"])

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

    def test_html_primary_table_has_two_verified_sides_and_difference_row(self):
        packet = self.call(row=self.fx.rows[0])
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            access.export_report(packet, self.runtime, root / "report.html", root / "report.zip")
            document = (root / "report.html").read_text()
            primary = re.search(r'<table id="primary-comparison">.*?<tbody>(.*?)</tbody></table>',
                                document, re.DOTALL).group(1)
            rows = re.findall(r'<tr>(.*?)</tr>', primary, re.DOTALL)
            self.assertEqual(len(rows), 3)
            self.assertIn("180天生产基线", rows[0])
            self.assertIn("实际所选F5-C（α=0.0）", rows[1])
            self.assertIn("候选减基线", rows[2])
            self.assertEqual(rows[2].count("+0.000000个百分点"), 3)
            self.assertEqual(rows[2].count("<td>+0.000000</td>"), 2)
            native = packet["baseline"]
            self.assertIn(f"<td>{native['expected_goals']['home']:.6f}</td>", rows[0])
            self.assertIn(f"<td>{native['probabilities']['home_win']:.6%}</td>", rows[0])
            self.assertIn('<table id="exploratory-grid">', document)
            # An unverified top-level state must not turn supplied numerical
            # fields into a verified primary table or an evaluated difference.
            rejected = {**packet, "status": "rejected"}
            access.export_report(rejected, self.runtime, root / "rejected.html", root / "rejected.zip")
            failed = (root / "rejected.html").read_text()
            self.assertNotIn('<table id="primary-comparison">', failed)
            self.assertNotIn("选中候选相对生产基线的概率差：", failed)

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
        self.fx.now += timedelta(days=2)
        self.fx.result(home=2, away=1)
        code, report = ts.score(self.runtime, clock=self.fx.clock)
        self.assertEqual(code, 0, report)
        from football_model.prospective import _append
        unrelated = _append(self.fx.root, "scores", {"forecast_id": "f" * 64, "unrelated": True})
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            tree = root / "tree"
            tree.mkdir()
            state_storage.pack_runtime(self.runtime, tree)
            manifest_path = tree / state_storage.MANIFEST_NAME
            manifest = json.loads(manifest_path.read_text())
            manifest.update(logical_limit_bytes=2 * 1024**3, physical_limit_bytes=500 * 1024**2)
            manifest_path.write_text(json.dumps(manifest))
            unrelated_entry = next(e for e in manifest["entries"] if e["path"].endswith('/scores/' + unrelated + '.json'))
            (tree / state_storage.ARCHIVE_DIR / (unrelated_entry["blob_sha256"] + '.json.gz')).unlink()
            # Unrelated archive members need not be materialized or read.
            selected = self.forecast["object_id"]
            isolated = root / "isolated"
            isolated.mkdir()
            packet = access.comparison(isolated, "epl", 1001, now=self.fx.now,
                                       forecast_id=selected, state_tree=tree)
            self.assertEqual(packet["status"], "verified_saved_original_audit", packet)
            self.assertEqual(packet["score"]["status"], "scored")
            self.assertEqual(packet["latest_attempts"]["score"]["report_id"], report["report_id"])
            self.assertFalse(any(isolated.iterdir()))
            report, archive_path = root / "report.html", root / "report.zip"
            access.export_report(packet, isolated, report, archive_path, state_tree=tree)
            self.assertIn("alpha_0.25", report.read_text())
            self.assertIn("实际选中 α：0.0", report.read_text())
            self.assertIn("合格成熟样本：0", report.read_text())
            self.assertIn("评分状态：scored", report.read_text())
            self.assertIn("选中候选与生产基线完全相同", report.read_text())
            self.assertIn("主胜+0.000000个百分点", report.read_text())
            with zipfile.ZipFile(archive_path) as archive:
                manifest = json.loads(archive.read('manifest.json'))
                for entry in manifest["files"]:
                    self.assertEqual(hashlib.sha256(archive.read(entry["path"])).hexdigest(), entry["sha256"])
                for evidence in packet["evidence"]:
                    self.assertEqual(archive.read('originals/' + evidence["path"]), (self.runtime/evidence["path"]).read_bytes())

    def test_exported_mature_selection_pool_is_sufficient_for_independent_read(self):
        # A genuine one-match selection history must bring its earlier forecast
        # and frozen result evidence into the target's export closure.
        self.fx.now += timedelta(days=4)
        self.fx.result()
        self.fx.now += timedelta(days=3)
        self.fx.publish([1002], self.fx.now + timedelta(days=1), run_id="r2")
        code, report = self.fx.capture()
        self.assertEqual(code, 0, report)
        packet = access.comparison(self.runtime, "epl", 1002, now=self.fx.now, row=self.fx.rows[0])
        self.assertEqual(packet["status"], "verified_same_parent", packet)
        self.assertEqual(packet["selection"]["n"], 1)
        paths = {e["path"] for e in packet["evidence"]}
        self.assertIn(f"total_shrink_shadow/forecasts/{self.forecast['object_id']}.json", paths)
        self.assertTrue(any("/selection_results/" in name for name in paths))
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            access.export_report(packet, self.runtime, root / "report.html", root / "report.zip")
            isolated = root / "isolated"
            with zipfile.ZipFile(root / "report.zip") as archive:
                for name in archive.namelist():
                    if name.startswith("originals/"):
                        target = isolated / name.removeprefix("originals/")
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_bytes(archive.read(name))
            restored = access.comparison(isolated, "epl", 1002, now=self.fx.now,
                                         forecast_id=packet["forecast_id"])
            self.assertEqual(restored["status"], "verified_saved_original_audit", restored)
            self.assertEqual(restored["predictions"], packet["predictions"])
            self.assertEqual(restored["selection"], packet["selection"])


if __name__ == "__main__":
    unittest.main()
