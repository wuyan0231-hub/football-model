"""Finalization reuses a saved real clock; synthetic gates prove no skill."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from football_model.advanced_research import implementation_fingerprint
from football_model.feature_decisions import _digest
from scripts import current_observation_decision as current
from scripts import finalize_current_observation_decision as finalizer
from tests import test_current_observation_decision as fixture_module


class CurrentObservationFinalizeTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture_module.CurrentObservationTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.runtime = self.fixture.runtime
        packet, result, originals = self.fixture.build()
        raw = Path(current.__file__).read_bytes()
        self.identity = {"entrypoint_path": "scripts/current_observation_decision.py",
            "entrypoint_sha256": hashlib.sha256(raw).hexdigest(),
            "existing_implementation_fingerprint": implementation_fingerprint(), "config_sha256": hashlib.sha256(b"{}").hexdigest()}
        result["implementation_identity"] = self.identity
        result.pop("comparison_id")
        result["comparison_id"] = _digest(result)
        self.run = current.save(self.runtime / "base", packet, result, originals, self.identity, raw, b"{}")
        self.receipt_sha = hashlib.sha256((self.run / "receipt.json").read_bytes()).hexdigest()
        self.value = {"decision_record": {"match_id": "100", "analysis_cutoff": packet["analysis_as_of"],
            "kickoff_at": packet["fixture"]["kickoff"], "inputs": [self.fixture.source]},
            "decision_calculation_links": []}
        self.link_input = self.runtime / "s2-link.json"
        self.link_input.write_text(json.dumps(self.value))
        self.linked = {"status": "structural_checks_passed", "validation": {"status": "structural_checks_passed", "issues": []},
            "evidence": [self.fixture.source], "automatic_recommendation": False}

    def run_finalize(self, build_status="structural_checks_passed", verify_status="verified"):
        def builder(record, output, source_root):
            output.mkdir()
            (output / "manifest.json").write_bytes(b"synthetic bundle\n")
            return {"manifest_sha256": hashlib.sha256(b"synthetic bundle\n").hexdigest(), "status": build_status}
        with patch.object(finalizer, "_decision_record_link", return_value=deepcopy(self.linked)) as linked, patch.object(
                finalizer, "build_decision_bundle", side_effect=builder) as built, patch.object(
                finalizer, "verify_decision_bundle", return_value={"status": verify_status,
                    "decision_status": "structural_checks_passed", "manifest_sha256": hashlib.sha256(b"synthetic bundle\n").hexdigest()}) as verified, patch.object(
                current.integration, "match_packet", side_effect=AssertionError("no second packet call")):
            out, receipt = finalizer.finalize(self.run, self.receipt_sha, self.runtime, self.link_input, self.runtime / "final")
        self.assertEqual(linked.call_args.args[-1], fixture_module.NOW)
        self.assertEqual(built.call_args.args[0]["analysis_cutoff"], self.value["decision_record"]["analysis_cutoff"])
        self.assertEqual(verified.call_count, 1)
        return out, receipt

    def test_exact_saved_clock_and_byte_closure_no_new_packet(self):
        original = {p.relative_to(self.run).as_posix(): p.read_bytes() for p in self.run.rglob("*") if p.is_file()}
        out, receipt = self.run_finalize()
        self.assertFalse(receipt["new_match_packet_call_performed"])
        self.assertFalse(receipt["historical_as_of_eligible"])
        self.assertTrue(receipt["original_run_unchanged"])
        self.assertEqual(receipt["status"], "structural_checks_passed")
        for name, raw in original.items():
            self.assertEqual((self.run / name).read_bytes(), raw)
            self.assertEqual((out / "original-current-call" / name).read_bytes(), raw)
        for ref in receipt["files"]:
            self.assertEqual(hashlib.sha256((out / ref["path"]).read_bytes()).hexdigest(), ref["sha256"])

    def test_receipt_anchor_and_original_byte_tampering_rejected(self):
        with self.assertRaisesRegex(ValueError, "hash_mismatch"):
            finalizer.verify_run(self.run, "0" * 64)
        (self.run / "actual-native-model-packet.json").write_bytes(b"tampered")
        with self.assertRaisesRegex(ValueError, "hash_mismatch"):
            finalizer.verify_run(self.run, self.receipt_sha)

    def test_added_original_file_is_not_ignored(self):
        (self.run / "unlisted.json").write_bytes(b"{}")
        with self.assertRaisesRegex(ValueError, "file_set_changed"):
            finalizer.verify_run(self.run, self.receipt_sha)

    def test_prior_cutoff_and_match_cannot_replace_actual_clock(self):
        for field, bad in (("analysis_cutoff", fixture_module.OBS), ("match_id", "101"),
                ("kickoff_at", "2026-10-17T12:30:00Z")):
            value = deepcopy(self.value)
            value["decision_record"][field] = bad
            self.link_input.write_text(json.dumps(value))
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "exact_actual_original_packet"):
                self.run_finalize()

    def test_later_external_information_rejected(self):
        self.value["decision_record"]["inputs"][0]["observed_at"] = "2026-10-16T14:30:01Z"
        self.link_input.write_text(json.dumps(self.value))
        with self.assertRaisesRegex(ValueError, "later_external_information"):
            self.run_finalize()

    def test_s2_source_original_hash_enforced(self):
        (self.runtime / self.fixture.source["path"]).write_bytes(b"changed source")
        with self.assertRaisesRegex(ValueError, "hash_mismatch"):
            self.run_finalize()

    def test_second_finalization_never_overwrites(self):
        out, _ = self.run_finalize()
        before = (out / "receipt.json").read_bytes()
        with self.assertRaises(FileExistsError):
            self.run_finalize()
        self.assertEqual((out / "receipt.json").read_bytes(), before)

    def test_native_build_block_is_preserved_in_final_receipt(self):
        _, receipt = self.run_finalize(build_status="blocked")
        self.assertEqual(receipt["status"], "blocked")
        self.assertEqual(receipt["native_build_status"], "blocked")

    def test_native_verification_failure_cannot_inherit_link_pass(self):
        _, receipt = self.run_finalize(verify_status="rejected")
        self.assertEqual(receipt["native_link_status"], "structural_checks_passed")
        self.assertEqual(receipt["status"], "blocked")
        self.assertEqual(receipt["native_verify_status"], "rejected")

    def test_output_inside_original_run_rejected_before_any_write(self):
        before = {p.relative_to(self.run).as_posix(): p.read_bytes() for p in self.run.rglob("*") if p.is_file()}
        with self.assertRaisesRegex(ValueError, "output_inside_original_run"):
            finalizer.finalize(self.run, self.receipt_sha, self.runtime, self.link_input, self.run / "new-subdir")
        after = {p.relative_to(self.run).as_posix(): p.read_bytes() for p in self.run.rglob("*") if p.is_file()}
        self.assertEqual(before, after)
        self.assertFalse((self.run / "new-subdir").exists())

    def test_original_run_root_symlink_rejected(self):
        linked = self.runtime / "run-link"
        linked.symlink_to(self.run, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "original_run_symlink_forbidden"):
            finalizer.verify_run(linked, self.receipt_sha)


if __name__ == "__main__":
    unittest.main()
