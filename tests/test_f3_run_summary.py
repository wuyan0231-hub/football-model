import copy
import hashlib
import unittest

from football_model.run_summary import _canonical, _digest, markdown
import test_s2_run_summary as summary_fixtures


class F3SummaryTests(unittest.TestCase):
    def setUp(self):
        self.f = summary_fixtures.RunSummaryTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)

    def checkpoint(self):
        f = self.f
        for folder in ("coverage", "forecasts"):
            for path in (f.root / "prospective" / folder).glob("*.json"):
                path.unlink()
        checkpoint = copy.deepcopy(f.report)
        checkpoint.update(generated_at="2026-10-07T10:00:20Z", report_scope="completed_competition_checkpoint")
        cid = f.append("capture_checkpoints", checkpoint)
        capture = {"run_id": f.run_id, "checkpoint_id": cid, "protocol_id": "p1",
                   "completed_at": "2026-10-07T10:00:31Z", "competitions": ["epl"],
                   "coverage_status_counts": {"prospective": 1}, "errors": [], "exit_code": 0}
        capture_id = f.append("capture_runs", capture)
        f.report["prospective_captures"] = [{**capture, "capture_run_id": capture_id}]
        f.write("reports/latest.json", f.report)
        source = _digest({"kind": "run_report", "original_bytes_sha256": hashlib.sha256(_canonical(checkpoint)).hexdigest(), "content": checkpoint})
        base = {"run_id": f.run_id, "competition": "epl", "match_id": 1,
                "report_source": source, "captured_at": "2026-10-07T10:00:30Z"}
        fid = f.append("forecasts", base)
        f.append("coverage", {**base, "status": "prospective", "forecast_id": fid})
        return cid

    def test_capture_before_final_stage_is_associated_via_checkpoint(self):
        self.checkpoint()
        self.assertEqual(self.f.build()["prospective_current"]["observed_accepted_predictions"], 1)

    def test_checkpoint_tampering_rejected_without_fabricated_zero(self):
        cid = self.checkpoint()
        self.f.write("prospective/capture_checkpoints/" + cid + ".json", {"changed": True})
        value = self.f.build()["prospective_current"]
        self.assertEqual(value["status"], "unknown")
        self.assertNotIn("observed_accepted_predictions", value)

    def test_checkpoint_rejects_capture_outside_its_actual_window(self):
        for observed in ("2026-10-07T10:00:19Z", "2026-10-07T10:00:32Z"):
            with self.subTest(observed=observed):
                self.checkpoint()
                for path in (self.f.root / "prospective/coverage").glob("*.json"):
                    import json
                    row = json.loads(path.read_text())
                    path.unlink()
                    row["captured_at"] = observed
                    self.f.append("coverage", row)
                self.assertEqual(self.f.build()["prospective_current"]["status"], "unknown")

    def test_timely_capture_survives_skipped_final_prospective_stage(self):
        self.checkpoint()
        self.f.outcomes["prospective"] = "skipped"
        del self.f.context["stage_started_at"]["prospective"]
        self.assertEqual(self.f.build()["prospective_current"]["observed_accepted_predictions"], 1)

    def independent(self):
        f = self.f
        f.context["stage_started_at"].update(result_sync="2026-10-07T10:03:00Z", quotes="2026-10-07T10:04:00Z")
        f.outcomes.update(result_sync="success", quotes="success")
        score = {**f.prospective, "generated_at": "2026-10-07T10:03:20Z"}
        sid = f.append("result_sync_scores", score)
        f.write("result_sync/reports/latest.json", {"run_started_at": "2026-10-07T10:03:01Z",
            "generated_at": "2026-10-07T10:03:30Z", "source_commit": "a" * 40,
            "target_matches": 3, "api_requests": 4, "counts": {"result_appended": 2, "pending_or_not_mature_at_observation": 1},
            "score_summary_id": sid})
        f.write("quotes/reports/latest.json", {"run_started_at": "2026-10-07T10:04:01Z",
            "generated_at": "2026-10-07T10:04:30Z", "source_commit": "a" * 40,
            "status": "partial", "candidate_count": 2, "api_requests": 5,
            "counts": {"collected": 1, "deferred": 1}, "matches": [
                {"snapshot_path": "test", "rows": 3, "unknown_rows": 3, "closed_rows": 0, "complete_reference_rows": 3},
                {"status": "deferred"}]})

    def test_independent_work_remains_visible_when_training_report_missing(self):
        self.independent()
        (self.f.root / "reports/latest.json").unlink()
        self.f.outcomes["model"] = "failure"
        value = self.f.build()
        self.assertEqual(value["model"]["status"], "unknown")
        self.assertEqual(value["result_sync"]["status_counts"]["result_appended"], 2)
        self.assertEqual(value["quotes"]["complete_reference_rows"], 3)
        self.assertEqual(value["prospective_cumulative"]["forecast_versions"], 7)
        self.assertIn("独立赛果同步", markdown(value))

    def test_independent_report_needs_current_commit_and_stage(self):
        self.independent()
        self.f.context["source_commit"] = "b" * 40
        value = self.f.build()
        self.assertEqual(value["quotes"]["status"], "unknown")
        self.assertEqual(value["result_sync"]["status"], "unknown")

    def test_unknown_sync_status_count_is_not_silently_zeroed(self):
        self.independent()
        import json
        path = self.f.root / "result_sync/reports/latest.json"
        report = json.loads(path.read_text())
        report["counts"]["future_unrecognized_status"] = None
        self.f.write("result_sync/reports/latest.json", report)
        self.assertIsNone(self.f.build()["result_sync"]["unknown_status_records"])

    def test_independent_failure_does_not_hide_saved_model(self):
        self.independent()
        self.f.outcomes["quotes"] = "failure"
        value = self.f.build()
        self.assertEqual(value["necessary_steps_not_successful"], ["quotes"])
        self.assertEqual(value["state_save_outcome"], "success")
        self.assertEqual(value["model"]["supported_predictions"], 2)

    def test_absent_parsed_snapshot_is_unknown_not_zero_price_count(self):
        self.independent()
        self.f.write("quotes/reports/latest.json", {"run_started_at": "2026-10-07T10:04:01Z",
            "generated_at": "2026-10-07T10:04:30Z", "source_commit": "a" * 40,
            "status": "partial", "candidate_count": 1, "api_requests": 0,
            "counts": {"deferred": 1}, "matches": [{"status": "deferred"}]})
        self.assertIsNone(self.f.build()["quotes"]["complete_reference_rows"])


if __name__ == "__main__":
    unittest.main()
