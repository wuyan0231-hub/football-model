"""Counterexamples for evidence time, path and quote row boundaries."""
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from football_model.evidence import (EvidenceError, freeze_bundle, load_analysis_inputs,
                                     main, screenshot_benchmark, select_quotes, verify_bundle)


NOW = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)


class FrozenEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        (self.source / "odds.png").write_bytes(b"original exact image bytes\x00\xff")
        (self.source / "unlisted.txt").write_text("must never be copied")
        self.output = self.root / "frozen"
        self.spec = {"match_key": "provider:123", "version_id": "pre-r1",
                     "analysis_cutoff": "2026-10-06T11:59:00Z", "kickoff_at": "2026-10-06T14:00:00Z",
                     "future_information_exposure": False,
                     "files": [{"source_id": "image-1", "path": "odds.png", "role": "screenshot",
                                "observed_at": "2026-10-06T11:58:00Z", "published_at": None}]}
        self.clock = patch("football_model.evidence._now", return_value=NOW)
        self.clock.start()

    def tearDown(self):
        self.clock.stop()
        self.temp.cleanup()

    def freeze(self):
        return freeze_bundle(self.source, self.output, self.spec)

    def test_exact_original_allowlist_real_clock_and_anchor(self):
        receipt = self.freeze()
        self.assertEqual(receipt["created_at"], "2026-10-06T12:00:00Z")
        self.assertFalse(receipt["artifact_existed_at_analysis_cutoff"])
        self.assertEqual(receipt["qualification"], "prospective_input_candidate")
        self.assertFalse(receipt["historical_blindness_proven"])
        self.assertFalse((self.output / "originals/unlisted.txt").exists())
        verified = verify_bundle(self.output, receipt["manifest_sha256"])
        self.assertEqual(verified["status"], "verified")
        self.assertEqual(verified["integrity"], "externally_anchored")
        self.assertEqual(load_analysis_inputs(self.output)["inputs"]["image-1"],
                         (self.source / "odds.png").read_bytes())

    def test_freeze_crossing_kickoff_uses_completion_clock(self):
        self.spec["kickoff_at"] = "2026-10-06T12:00:01Z"
        with patch("football_model.evidence._now", side_effect=[NOW, NOW + timedelta(seconds=2)]):
            result = self.freeze()
        self.assertEqual(result["qualification"], "research_only")
        self.assertIn("frozen_after_kickoff", result["reasons"])

    def test_backdated_creation_cannot_be_supplied(self):
        self.spec["created_at"] = self.spec["analysis_cutoff"]
        with self.assertRaisesRegex(EvidenceError, "creation_time_cannot_be_supplied"):
            self.freeze()

    def test_late_acquisition_is_research_and_cannot_enter_analysis(self):
        self.spec["files"][0]["observed_at"] = "2026-10-06T12:00:00Z"
        result = self.freeze()
        self.assertEqual(result["qualification"], "research_only")
        self.assertIn("observed_after_cutoff", result["reasons"])
        self.assertEqual(verify_bundle(self.output)["status"], "verified")
        with self.assertRaisesRegex(EvidenceError, "ineligible_analysis_inputs"):
            load_analysis_inputs(self.output)

    def test_future_dated_acquisition_is_rejected(self):
        self.spec["files"][0]["observed_at"] = "2026-10-06T13:00:00Z"
        with self.assertRaisesRegex(EvidenceError, "source_time_in_future"):
            self.freeze()
        self.assertFalse(self.output.exists())

    def test_unknown_observation_and_exposure_never_become_prospective(self):
        self.spec["files"][0]["observed_at"] = None
        self.spec["future_information_exposure"] = None
        result = self.freeze()
        self.assertEqual(result["qualification"], "research_only")
        self.assertIn("observation_time_unknown", result["reasons"])
        self.assertIn("future_information_exposure_unknown", result["reasons"])

    def test_past_match_cannot_become_original_forecast(self):
        self.spec["kickoff_at"] = "2026-10-06T11:59:30Z"
        result = self.freeze()
        self.assertIn("frozen_after_kickoff", result["reasons"])
        # Timely original inputs remain readable for explicitly limited research.
        self.assertEqual(load_analysis_inputs(self.output)["eligibility"]["qualification"], "research_only")

    def test_timezone_is_required(self):
        self.spec["analysis_cutoff"] = "2026-10-06T11:59:00"
        with self.assertRaisesRegex(EvidenceError, "timezone_required"):
            self.freeze()

    def test_result_role_and_path_escape_are_rejected(self):
        self.spec["files"][0]["role"] = "result"
        with self.assertRaisesRegex(EvidenceError, "results_belong_to_scoring"):
            self.freeze()
        self.spec["files"][0]["role"] = "source"
        for path in ("../source/odds.png", str(self.source / "odds.png"), "sub/../odds.png", "sub\\odds.png"):
            self.spec["files"][0]["path"] = path
            with self.assertRaisesRegex(EvidenceError, "unsafe_path"):
                self.freeze()

    def test_source_and_destination_symlink_rejected(self):
        (self.source / "linked.png").symlink_to(self.source / "odds.png")
        self.spec["files"][0]["path"] = "linked.png"
        with self.assertRaisesRegex(EvidenceError, "symlink_forbidden"):
            self.freeze()
        self.spec["files"][0]["path"] = "odds.png"
        (self.root / "linked-dir").symlink_to(self.source, target_is_directory=True)
        with self.assertRaisesRegex(EvidenceError, "symlink_forbidden"):
            freeze_bundle(self.source, self.root / "linked-dir/frozen", self.spec)

    def test_no_overwrite(self):
        self.freeze()
        before = (self.output / "record.json").read_bytes()
        with self.assertRaisesRegex(EvidenceError, "destination_exists_no_overwrite"):
            self.freeze()
        self.assertEqual((self.output / "record.json").read_bytes(), before)

    def test_tampering_is_detected(self):
        self.freeze()
        (self.output / "originals/odds.png").write_bytes(b"edited")
        self.assertEqual(verify_bundle(self.output)["reason"], "file_hash_or_length_mismatch")

    def test_extra_files_are_not_silently_ingested(self):
        self.freeze()
        (self.output / "final-result.json").write_text('{"score": "2-0"}')
        self.assertEqual(verify_bundle(self.output)["reason"], "unlisted_or_missing_file")

    def test_consistent_rewrite_still_fails_external_anchor(self):
        receipt = self.freeze()
        manifest_file = self.output / "manifest.json"
        manifest = json.loads(manifest_file.read_bytes())
        # An attacker can make a self-consistent manifest; an external receipt matters.
        record_file = self.output / "record.json"
        record = json.loads(record_file.read_bytes())
        record["version_id"] = "changed"
        payload = json.dumps(record).encode()
        record_file.write_bytes(payload)
        for entry in manifest["files"]:
            if entry["path"] == "record.json":
                entry.update(bytes=len(payload), sha256=hashlib.sha256(payload).hexdigest())
        manifest_file.write_text(json.dumps(manifest))
        self.assertEqual(verify_bundle(self.output)["status"], "verified")
        self.assertEqual(verify_bundle(self.output, receipt["manifest_sha256"])["reason"], "manifest_anchor_mismatch")

    def test_cli_failure_returns_json_reason(self):
        output = io.StringIO()
        with redirect_stdout(output):
            code = main(["verify", "--bundle", str(self.output)])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(output.getvalue())["status"], "rejected")


def quote(order, time, **changes):
    row = {"row_id": f"q{order}", "source_id": "bet365", "market": "totals", "sequence": "image1",
           "row_order": order, "quote_time": time, "time_precision": "second", "status": "open",
           "line": 2.25, "odds": {"over": 1.95, "under": 1.95}, "odds_format": "decimal"}
    row.update(changes)
    return row


class HistoricalQuoteTests(unittest.TestCase):
    def selected(self, rows, cutoff="2026-10-06T20:00:00+08:00"):
        return select_quotes(rows, cutoff)["groups"][0]

    def test_future_row_removed_and_latest_closed_not_skipped(self):
        rows = [quote(0, "2026-10-06T20:05:00+08:00"),
                quote(1, "2026-10-06T19:58:00+08:00", status="closed", odds=None),
                quote(2, "2026-10-06T19:55:00+08:00")]
        result = self.selected(rows)
        self.assertEqual(result["snapshot_latest"]["row_id"], "q0")
        self.assertEqual(result["as_of_latest"]["row_id"], "q1")
        self.assertEqual(result["status"], "closed")
        self.assertEqual(result["excluded_after_cutoff"], ["q0"])

    def test_minute_straddling_cutoff_blocks_fallback(self):
        rows = [quote(0, "2026-10-06T20:00:00+08:00", time_precision="minute"),
                quote(1, "2026-10-06T19:59:00+08:00", time_precision="minute")]
        result = self.selected(rows, "2026-10-06T20:00:30+08:00")
        self.assertEqual(result["status"], "ambiguous_at_cutoff")
        self.assertIsNone(result["as_of_latest"])
        self.assertEqual(self.selected(rows, "2026-10-06T20:01:00+08:00")["as_of_latest"]["row_id"], "q0")

    def test_same_minute_uses_page_order_without_invented_seconds(self):
        rows = [quote(0, "2026-10-06T19:59:00+08:00", time_precision="minute", status="closed"),
                quote(1, "2026-10-06T19:59:00+08:00", time_precision="minute")]
        self.assertEqual(self.selected(rows)["status"], "closed")

    def test_overlapping_precision_does_not_fabricate_order_conflict(self):
        rows = [quote(0, "2026-10-06T19:59:00+08:00", time_precision="minute"),
                quote(1, "2026-10-06T19:59:30+08:00")]
        self.assertEqual(self.selected(rows)["as_of_latest"]["row_id"], "q0")

    def test_minute_must_not_contain_invented_seconds(self):
        with self.assertRaisesRegex(EvidenceError, "minute_precision_must_not_invent_seconds"):
            self.selected([quote(0, "2026-10-06T19:59:30+08:00", time_precision="minute")])

    def test_minute_text_cannot_be_claimed_exact_second(self):
        with self.assertRaisesRegex(EvidenceError, "second_precision_requires_explicit_seconds"):
            self.selected([quote(0, "2026-10-06T19:59+08:00")])

    def test_missing_time_does_not_qualify(self):
        rows = [quote(0, None), quote(1, "2026-10-06T19:59:00+08:00")]
        result = self.selected(rows)
        self.assertEqual(result["status"], "unknown_quote_time")
        self.assertIsNone(result["as_of_latest"])

    def test_incomplete_latest_is_not_repaired_from_old_row(self):
        rows = [quote(0, "2026-10-06T19:59:00+08:00", odds={"over": 1.95}),
                quote(1, "2026-10-06T19:58:00+08:00")]
        result = self.selected(rows)
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(result["as_of_latest"]["odds"], {"over": 1.95})

    def test_each_source_market_page_is_independent(self):
        rows = [quote(0, "2026-10-06T19:59:00+08:00"),
                quote(0, "2026-10-06T19:58:00+08:00", row_id="other-page", sequence="image2"),
                quote(0, "2026-10-06T19:57:00+08:00", row_id="other-market", market="asian_handicap", odds={"home": 1.9, "away": 1.9})]
        self.assertEqual(len(select_quotes(rows, "2026-10-06T20:00:00+08:00")["groups"]), 3)

    def test_known_order_inversion_and_timezone_rejected(self):
        with self.assertRaisesRegex(EvidenceError, "page_order_conflicts"):
            self.selected([quote(0, "2026-10-06T19:58:00+08:00"), quote(1, "2026-10-06T19:59:00+08:00")])
        with self.assertRaisesRegex(EvidenceError, "timezone_required"):
            self.selected([quote(0, "2026-10-06T19:58:00")])


class ScreenshotBenchmarkTests(unittest.TestCase):
    def truth(self):
        return [{"row_id": str(i), "image_sha256": "a" * 64, "verified_by": "reviewer-1",
                 "fields": {"line": 2.25, "status": "open"}} for i in range(2)]

    def test_abstention_is_coverage_loss_not_correct_recognition(self):
        result = screenshot_benchmark(self.truth(), [{"row_id": "0", "fields": {"line": 2.25, "status": "open"}},
                                                     {"row_id": "1", "abstain": True}])
        self.assertEqual(result["accuracy_among_read_fields"], 1)
        self.assertEqual(result["field_coverage"], .5)
        self.assertEqual(result["correct_fraction_of_all_labeled_fields"], .5)
        self.assertEqual(result["rows_exact"], 1)

    def test_wrong_line_counts_as_critical_error(self):
        result = screenshot_benchmark(self.truth(), [{"row_id": "0", "fields": {"line": 2.5, "status": "open"}}])
        self.assertEqual(result["critical_error_rows"], 1)
        self.assertEqual(result["critical_error_fields"], 1)
        self.assertEqual(result["critical_error_rate_among_read_rows"], 1)

    def test_boolean_numeric_and_partial_row_do_not_look_fully_correct(self):
        truth = self.truth()
        truth[0]["fields"] = {"line": 0.0, "odds": {"over": 1.0}, "status": "open"}
        result = screenshot_benchmark(truth, [{"row_id": "0", "fields": {
            "line": False, "odds": {"over": True}, "status": "open"}},
            {"row_id": "1", "fields": {"status": "open"}}])
        self.assertEqual(result["critical_error_fields"], 2)
        self.assertEqual(result["rows_exact"], 0)
        self.assertEqual(result["row_coverage"], 1)
        self.assertEqual(result["complete_row_coverage"], .5)
        self.assertEqual(result["critical_fields_missing"], 1)
        self.assertEqual(result["critical_field_coverage"], .8)

    def test_requires_image_provenance_and_rejects_unmatched_output(self):
        with self.assertRaisesRegex(EvidenceError, "unexpected_or_duplicate_prediction"):
            screenshot_benchmark(self.truth(), [{"row_id": "3", "fields": {"line": 2.25}}])
        truth = self.truth()
        truth[0]["image_sha256"] = None
        with self.assertRaisesRegex(EvidenceError, "original_image_sha256_required"):
            screenshot_benchmark(truth, [])


if __name__ == "__main__":
    unittest.main()
