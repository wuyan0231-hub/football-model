"""A source manifest is hashed once, never once per historical original."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from football_model import history_archive as history


class ManifestDigestScalingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.tree, self.runtime = self.root / "transport", self.root / "runtime"
        (self.tree / "runtime").mkdir(parents=True)
        self.manifest_raw = b'{"validated_source_manifest":"original bytes stay fixed"}\n'
        (self.tree / "state_storage.json").write_bytes(self.manifest_raw)

    def test_actual_86857_entry_scale_restoration_hashes_manifest_once(self):
        # The production fixed-state manifest has 86,857 selected originals.
        # Metadata-only construction tests the real iteration count without
        # requiring archived provider data or replacing full cloud validation.
        count = 86857
        originals = [{"path": "advanced_models/objects/" + f"{n:064x}" + ".json",
                      "raw_sha256": "a" * 64, "raw_bytes": 2,
                      "blob_sha256": "b" * 64, "compressed_bytes": 22}
                     for n in range(count)]
        with patch.object(history, "_ensure_commit"), patch.object(history, "_write_catalog") as write, \
             patch.object(history, "_sha", wraps=history._sha) as digest:
            cold = history.restore_history_catalog(self.runtime, self.tree, "c" * 40,
                                                   self.root, originals)
        self.assertEqual(len(cold), count)
        self.assertEqual(digest.call_count, 1)
        self.assertEqual(digest.call_args.args[0], self.manifest_raw)
        entries = write.call_args.args[1]
        expected = hashlib.sha256(self.manifest_raw).hexdigest()
        self.assertEqual({e["manifest_sha256"] for e in entries.values()}, {expected})

    def test_compaction_hashes_one_parent_manifest_once_for_multiple_files(self):
        raw = b'{}\n'
        raw_hash = hashlib.sha256(raw).hexdigest()
        originals = []
        for n in range(3):
            name = "advanced_models/objects/" + f"{n:064x}" + ".json"
            target = self.runtime / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)
            originals.append({"path": name, "raw_sha256": raw_hash, "raw_bytes": len(raw),
                              "blob_sha256": "b" * 64, "compressed_bytes": 22})
        manifest_raw = json.dumps({"entries": originals}).encode()
        def fixed_bytes(repo, commit, path, **kwargs):
            return manifest_raw if path == "state_storage.json" else None
        with patch.object(history, "_ensure_commit"), patch.object(history, "_git_bytes", side_effect=fixed_bytes), \
             patch.object(history, "_entry_body", return_value=raw), patch.object(history, "_write_catalog"), \
             patch.object(history, "_sha", wraps=history._sha) as digest:
            report = history.compact_existing_history(self.runtime, "c" * 40, self.root)
        manifest_calls = [call for call in digest.call_args_list if call.args[0] == manifest_raw]
        self.assertEqual(len(manifest_calls), 1)
        self.assertEqual(report["compacted_file_count"], 3)


if __name__ == "__main__": unittest.main()
