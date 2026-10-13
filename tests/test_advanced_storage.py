"""Lossless bounded content objects, shared unchanged sections and hash failures."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from football_model.advanced_storage import save_artifact, load_report, MAX_PIECE_BYTES
from football_model.advanced_capacity import CapacityGuard, CapacityDeferred


def ref(root, path):
    return {"path": path.relative_to(root).as_posix(), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


class AdvancedStorageTests(unittest.TestCase):
    def test_roundtrip_large_report_and_reuse_unchanged_training_sections(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            shared = [{"match_id": i, "matrix": [[.0123456789] * 20 for _ in range(20)],
                       "source_refs": [{"path": "raw/retained.json", "sha256": "a" * 64}]} for i in range(1000)]
            original = {"replays": shared, "training": shared, "run_at": "first", "failures": [None, True, 0]}
            first = save_artifact(root, root / "reports", original)
            self.assertEqual(load_report(root, ref(root, first)), original)
            objects_before = {p.name for p in (root / "advanced_models/objects").glob("*.json")}
            second = save_artifact(root, root / "reports", {**original, "run_at": "second"})
            self.assertEqual(load_report(root, ref(root, second))["replays"], shared)
            objects_after = {p.name for p in (root / "advanced_models/objects").glob("*.json")}
            self.assertLessEqual(len(objects_after - objects_before), 1)
            self.assertTrue(all(p.stat().st_size <= MAX_PIECE_BYTES for p in root.rglob("*.json")))

    def test_missing_or_tampered_chunk_is_rejected_and_plain_legacy_reads(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            p = save_artifact(root, root / "reports", {"values": list(range(5000))}, inline_bytes=128)
            target = next((root / "advanced_models/objects").glob("*.json"))
            raw = target.read_bytes(); target.write_bytes(b"{}")
            with self.assertRaises(ValueError): load_report(root, ref(root, p))
            target.write_bytes(raw); target.unlink()
            with self.assertRaises(FileNotFoundError): load_report(root, ref(root, p))
            legacy = root / "old.json"; legacy.write_text('{"a": [1, 2, null]}')
            self.assertEqual(load_report(root, ref(root, legacy)), {"a": [1, 2, None]})

    def test_unsafe_ref_and_forged_length_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaises(ValueError): load_report(root, {"path": "../outside.json", "sha256": "x"})
            target = root / "part.json"; target.write_text("[1,2]")
            wrapper = root / "wrapper.json"
            wrapper.write_text(json.dumps({"$advanced_list_chunks": [ref(root, target)], "length": 3}))
            with self.assertRaises(ValueError): load_report(root, ref(root, wrapper))

    def test_capacity_is_checked_against_actual_new_bytes_before_write(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            guard = CapacityGuard(root, reserve_logical=0, reserve_physical=0,
                max_logical=1500, max_physical=100000)
            value = {"values": list(range(5000))}
            with self.assertRaises(CapacityDeferred):
                save_artifact(root, root / "reports", value, inline_bytes=128, guard=guard)
            self.assertLessEqual(sum(p.stat().st_size for p in root.rglob("*.json")), 1500)


if __name__ == "__main__":
    unittest.main()
