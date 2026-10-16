"""Real comparison diagnostics stay tied to captured bytes across producer updates."""
from datetime import timedelta
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from football_model.advanced_capacity import CapacityGuard
from football_model.advanced_snapshots import _freeze_baseline_packet, _root
from football_model import advanced_snapshots as snapshots
from football_model.feature_snapshots import SnapshotError, _read, _digest
from tests.test_advanced_durability import replay_reference
from tests import test_advanced_snapshots as fixtures
from tests.test_advanced_snapshots import CAPTURE, PROTOCOL, ref, write


class BaselinePacketFreezeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.runtime = Path(self.temp.name) / "runtime"
        self.runtime.mkdir()
        self.root = _root(self.runtime, PROTOCOL)

    def freeze(self, packet):
        return _freeze_baseline_packet(self.runtime, self.root, packet, CAPTURE, CapacityGuard(self.runtime))

    def test_mutable_attempt_summaries_are_frozen_with_original_metadata(self):
        source = write(self.runtime / "total_shrink_shadow/reports/latest.json", {"status": "failed", "errors": ["real_error"]})
        original = source.read_bytes()
        declared = {**ref(self.runtime, source), "bytes": len(original),
            "validation": "raw_attempt_summary_not_forecast_validation"}
        packet = {"f5c_comparison": {"attempt_evidence": [declared]}, "unknown_path": {"path": "not-a-reference"}}
        saved, refs = self.freeze(packet)
        edge = saved["f5c_comparison"]["attempt_evidence"][0]
        self.assertEqual(saved, packet)
        self.assertEqual(packet["f5c_comparison"]["attempt_evidence"][0], declared)
        self.assertEqual(edge["sha256"], declared["sha256"])
        self.assertEqual(edge["bytes"], len(original))
        self.assertEqual(edge["validation"], declared["validation"])
        self.assertEqual(saved["unknown_path"], packet["unknown_path"])
        self.assertNotEqual(refs[0]["runtime_relative_path"], declared["path"])
        write(source, {"status": "newer_success"})
        self.assertEqual((self.runtime / refs[0]["runtime_relative_path"]).read_bytes(), original)
        self.assertEqual(refs[0]["origin_runtime_path"], declared["path"])
        self.assertEqual(refs[0]["kind"], "baseline_packet_input")

    def test_recursive_raw_closure_routes_preserve_nested_original_bytes(self):
        child = write(self.runtime / "reports/nested-latest.json", {"unavailable": True})
        parent = write(self.runtime / "reports/latest.json", {"child": ref(self.runtime, child)})
        parent_raw = parent.read_bytes()
        saved, refs = self.freeze({"evidence": [ref(self.runtime, parent)]})
        self.assertEqual({r["origin_runtime_path"] for r in refs}, {"reports/latest.json", "reports/nested-latest.json"})
        routes = {(r["origin_runtime_path"], r["sha256"]): r["runtime_relative_path"] for r in refs}
        edge = saved["evidence"][0]
        self.assertEqual((self.runtime / routes[(edge["path"], edge["sha256"]) ]).read_bytes(), parent_raw)
        original_child = json.loads(parent_raw)["child"]
        frozen_child = self.runtime / routes[(original_child["path"], original_child["sha256"])]
        write(child, {"newer": 1})
        self.assertEqual(hashlib.sha256(frozen_child.read_bytes()).hexdigest(), original_child["sha256"])

    def test_typed_replay_keeps_canonical_contract_and_separate_raw_binding(self):
        raw, declared = replay_reference()
        source = self.runtime / declared["path"]
        source.parent.mkdir(parents=True)
        source.write_bytes(raw)
        saved, refs = self.freeze({"replay": declared})
        self.assertEqual(saved["replay"], declared)
        self.assertNotEqual(declared["sha256"], hashlib.sha256(raw).hexdigest())
        self.assertEqual(refs[0]["runtime_relative_path"], declared["path"])
        self.assertEqual(refs[0]["sha256"], hashlib.sha256(raw).hexdigest())
        self.assertEqual(source.read_bytes(), raw)

    def test_duplicate_typed_edge_still_checks_producer_metadata(self):
        raw, declared = replay_reference()
        source = self.runtime / declared["path"]
        source.parent.mkdir(parents=True)
        source.write_bytes(raw)
        with self.assertRaisesRegex(ValueError, "durable_replay_reference_metadata_mismatch"):
            self.freeze({"refs": [{**declared, "events": 999}, declared]})

    def test_missing_and_changed_originals_fail_closed(self):
        source = write(self.runtime / "reports/latest.json", {"real": "old"})
        declared = ref(self.runtime, source)
        write(source, {"real": "new"})
        with self.assertRaisesRegex(ValueError, "durable_source_hash_mismatch"):
            self.freeze({"evidence": [declared]})
        with self.assertRaises(FileNotFoundError):
            self.freeze({"evidence": [{"path": "reports/missing.json", "sha256": "a" * 64}]})

    def test_absolute_edge_requires_a_safe_runtime_relative_path(self):
        with self.assertRaisesRegex(SnapshotError, "escapes_runtime"):
            self.freeze({"evidence": [{"path": str(Path(self.temp.name) / "outside.json"), "sha256": "a" * 64}]})
        source = write(self.runtime / "reports/latest.json", {"real": True})
        packet = {"evidence": [{"path": str(source), "sha256": ref(self.runtime, source)["sha256"]}]}
        with self.assertRaisesRegex(SnapshotError, "requires_runtime_relative_path"):
            self.freeze(packet)
        packet["evidence"][0]["runtime_relative_path"] = "reports/latest.json"
        saved, refs = self.freeze(packet)
        self.assertEqual(saved, packet)
        self.assertEqual(refs[0]["origin_runtime_path"], "reports/latest.json")

    def test_future_original_is_rejected_before_copy(self):
        source = write(self.runtime / "reports/latest.json", {"created_at": (CAPTURE + timedelta(seconds=1)).isoformat()})
        with self.assertRaisesRegex(SnapshotError, "source_time_after_cutoff"):
            self.freeze({"evidence": [ref(self.runtime, source)]})
        self.assertFalse((self.root / "sources").exists())

    def test_noncanonical_original_edge_is_rejected_without_normalizing_or_copying(self):
        source = write(self.runtime / "reports/latest.json", {"actual": "original"})
        digest = ref(self.runtime, source)["sha256"]
        for name in ("reports/./latest.json", "reports//latest.json", "reports/../reports/latest.json",
                     "reports\\latest.json", "reports/latest:copy.json", "reports/.latest.json", "reports/latest.json/",
                     "reports/line\nbreak.json", "reports/tab\tname.json"):
            with self.subTest(name=name):
                packet = {"evidence": [{"path": name, "sha256": digest}], "comparison_id": "original-id"}
                encoded = json.dumps(packet, sort_keys=True)
                with self.assertRaisesRegex(SnapshotError, "baseline_packet_input_path_invalid"):
                    self.freeze(packet)
                self.assertEqual(json.dumps(packet, sort_keys=True), encoded)
                self.assertFalse((self.root / "sources").exists())

    def test_duplicate_json_keys_are_rejected(self):
        source = self.runtime / "report.json"
        source.write_bytes(b'{"status": "old", "status": "new"}')
        with self.assertRaisesRegex(SnapshotError, "duplicate_json_key"):
            self.freeze({"evidence": [ref(self.runtime, source)]})


class BaselinePacketCaptureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixtures.AdvancedSnapshotTests.setUpClass()

    @classmethod
    def tearDownClass(cls):
        fixtures.AdvancedSnapshotTests.tearDownClass()

    def setUp(self):
        self.harness = fixtures.AdvancedSnapshotTests(methodName="runTest")
        self.harness.setUp()
        self.addCleanup(self.harness.doCleanups)
        self.runtime = self.harness.runtime

    def test_real_capture_embeds_immutable_diagnostics_and_never_rewrites_first(self):
        source = write(self.runtime / "total_shrink_shadow/reports/latest.json", {"status": "completed", "errors": []})
        original = source.read_bytes()
        code, report = self.harness.capture()
        self.assertEqual(code, 0, report)
        first = _root(self.runtime, PROTOCOL) / "captures/epl/2000/EARLY/first.json"
        first_raw = first.read_bytes()
        receipt = _read(self.runtime / _read(first)["path"])
        payload = _read(self.runtime / receipt["payload_ref"]["path"])
        base_receipt = _read(self.runtime / payload["baseline_receipt_ref"]["path"])
        base = _read(self.runtime / base_receipt["payload_ref"]["path"])
        self.assertEqual(base["baseline_packet_reference_closure"], "frozen_raw_inputs_v1")
        attempt = base["baseline"]["f5c_comparison"]["attempt_evidence"][0]
        original_comparison = base["baseline"]["feature_decision_comparison"]
        self.assertEqual(original_comparison["comparison_id"], _digest({k: v for k, v in original_comparison.items() if k != "comparison_id"}))
        routes = {(r["origin_runtime_path"], r["sha256"]): r["runtime_relative_path"] for r in base["source_refs"]}
        frozen_attempt = self.runtime / routes[(attempt["path"], attempt["sha256"])]
        self.assertEqual(frozen_attempt.read_bytes(), original)
        self.assertTrue(any(r["kind"] == "baseline_packet_input" for r in base["source_refs"]))
        write(source, {"status": "new_producer_run"})
        code, report = self.harness.capture(CAPTURE + timedelta(minutes=1))
        self.assertEqual(code, 0, report)
        self.assertEqual(first.read_bytes(), first_raw)
        self.assertEqual(frozen_attempt.read_bytes(), original)
        self.assertEqual(self.harness.comparison(CAPTURE + timedelta(minutes=1))["status"], "available")

    def test_bad_declared_diagnostic_records_failure_without_creating_first(self):
        native_packet = snapshots.match_packet
        def changed(*args, **kwargs):
            packet = native_packet(*args, **kwargs)
            packet["f5c_comparison"]["attempt_evidence"] = [{"path": "reports/missing.json", "sha256": "a" * 64}]
            return packet
        with patch.object(snapshots, "match_packet", side_effect=changed):
            _, report = self.harness.capture()
        attempt = next(row for row in report["queue"] if row["horizon"] == "EARLY")
        self.assertEqual(attempt["status"], "ineligible")
        self.assertEqual(attempt["reason"], "FileNotFoundError")
        folder = _root(self.runtime, PROTOCOL) / "captures/epl/2000/EARLY"
        self.assertFalse((folder / "first.json").exists())
        self.assertTrue((folder / "first_attempt.json").exists())

    def test_noncanonical_diagnostic_cannot_create_first_capture(self):
        source = write(self.runtime / "reports/packet-latest.json", {"actual": "original"})
        digest = ref(self.runtime, source)["sha256"]
        native_packet = snapshots.match_packet
        observed = []
        bad_name = "reports/./packet-latest.json"
        def noncanonical(*args, **kwargs):
            packet = native_packet(*args, **kwargs)
            packet["f5c_comparison"]["attempt_evidence"] = [{"path": bad_name, "sha256": digest}]
            observed.append((packet, json.dumps(packet, sort_keys=True)))
            return packet
        for bad_name in ("reports/./packet-latest.json", "reports/line\nbreak.json", "reports/tab\tname.json"):
            with self.subTest(name=bad_name), patch.object(snapshots, "match_packet", side_effect=noncanonical):
                # Real files make these admissible-looking byte references;
                # rejection must come from the spelling, not a missing source.
                alternate = self.runtime / bad_name
                alternate.parent.mkdir(parents=True, exist_ok=True)
                alternate.write_bytes(source.read_bytes())
                _, report = self.harness.capture()
                attempt = next(row for row in report["queue"] if row["horizon"] == "EARLY")
                self.assertEqual(attempt["status"], "ineligible")
                self.assertEqual(attempt["reason"], "baseline_packet_input_path_invalid")
                folder = _root(self.runtime, PROTOCOL) / "captures/epl/2000/EARLY"
                self.assertFalse((folder / "first.json").exists())
                self.assertTrue((folder / "first_attempt.json").exists())
                self.assertFalse((_root(self.runtime, PROTOCOL) / "sources" / (digest + ".json")).exists())
        self.assertTrue(all(json.dumps(packet, sort_keys=True) == before for packet, before in observed))


if __name__ == "__main__":
    unittest.main()
