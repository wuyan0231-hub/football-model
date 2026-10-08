"""Selected connector archive access keeps state binding and original bytes."""
import base64
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

from football_model.state_storage import pack_runtime, StorageError

ROOT = Path(__file__).resolve().parents[1]

def load_script(name):
    spec = importlib.util.spec_from_file_location("archive_access_" + name, ROOT / "scripts" / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

materializer = load_script("materialize_model_access")
archive_reader = load_script("materialize_state_archive")

class ArchivedModelAccessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        runtime = self.root / "runtime"
        (runtime / "quotes/raw").mkdir(parents=True)
        self.raw = '{"quote":"原始价", "n":1}\n'.encode()
        (runtime / "quotes/raw/old.json").write_bytes(self.raw)
        self.tree = self.root / "transport"
        self.tree.mkdir()
        pack_runtime(runtime, self.tree)
        paths = [self.tree / "state_storage.json", *self.tree.glob("state_archives/*.json.gz")]
        self.payload = {"schema_version": 1, "repository": "wuyan0231-hub/football-model",
                        "source_commit": "a" * 40, "state_commit": "b" * 40, "files": []}
        for path in paths:
            data = path.read_bytes()
            relative = path.relative_to(self.tree).as_posix()
            blob = hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()
            self.payload["files"].append({"role": "state", "path": relative, "response": {
                "content": base64.b64encode(data).decode(), "encoding": "base64", "sha": blob,
                "display_url": "https://github.com/wuyan0231-hub/football-model/blob/" + "b" * 40 + "/" + relative}})
        self.selected = self.root / "selected"

    def materialize(self):
        materializer.materialize(self.payload, self.selected)

    def test_connector_materialization_then_selected_restore_exact_bytes(self):
        self.materialize()
        result = archive_reader.restore_selected(self.selected, ["quotes/raw/old.json"], self.selected / "archive-access.json")
        self.assertEqual((self.selected / "runtime/quotes/raw/old.json").read_bytes(), self.raw)
        self.assertEqual(result["state_commit"], "b" * 40)
        self.assertIn("not_full_state", result["status"])

    def test_archive_response_wrong_commit_rejected_before_write(self):
        self.payload["files"][1]["response"]["display_url"] = self.payload["files"][1]["response"]["display_url"].replace("b" * 40, "c" * 40)
        with self.assertRaises(materializer.MaterializationError): self.materialize()
        self.assertFalse(self.selected.exists())

    def test_transport_role_is_state_not_source(self):
        self.payload["files"][0]["role"] = "source"
        with self.assertRaises(materializer.MaterializationError): self.materialize()

    def test_changed_connector_blob_is_rejected(self):
        self.materialize()
        blob = next((self.selected / "state_archives").glob("*.gz"))
        blob.write_bytes(blob.read_bytes() + b"x")
        with self.assertRaises((StorageError, ValueError)):
            archive_reader.restore_selected(self.selected, ["quotes/raw/old.json"], self.selected / "access.json")
        self.assertFalse((self.selected / "runtime/quotes/raw/old.json").exists())

    def test_existing_original_file_is_not_overwritten(self):
        self.materialize()
        target = self.selected / "runtime/quotes/raw/old.json"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"original sentinel")
        with self.assertRaises(ValueError):
            archive_reader.restore_selected(self.selected, ["quotes/raw/old.json"], self.selected / "access.json")
        self.assertEqual(target.read_bytes(), b"original sentinel")

    def test_symlinked_archive_directory_is_rejected(self):
        self.materialize()
        archive = self.selected / "state_archives"
        elsewhere = self.selected / "moved"
        archive.rename(elsewhere)
        archive.symlink_to(elsewhere, target_is_directory=True)
        with self.assertRaises((StorageError, ValueError)):
            archive_reader.restore_selected(self.selected, ["quotes/raw/old.json"], self.selected / "access.json")

    def test_missing_verified_dependency_does_not_claim_connector_binding(self):
        self.materialize()
        manifest = self.selected / "model_access_manifest.json"
        value = json.loads(manifest.read_text())
        value["files"] = [r for r in value["files"] if r["path"] == "state_storage.json"]
        manifest.write_text(json.dumps(value))
        with self.assertRaises(ValueError):
            archive_reader.restore_selected(self.selected, ["quotes/raw/old.json"], self.selected / "access.json")
        self.assertFalse((self.selected / "runtime/quotes/raw/old.json").exists())

    def test_output_conflicts_rejected_before_restoring_any_bytes(self):
        self.materialize()
        target = self.selected / "runtime/quotes/raw/old.json"
        for output in [target, target.parent, target / "access.json"]:
            with self.subTest(output=output), self.assertRaises(ValueError):
                archive_reader.restore_selected(self.selected, ["quotes/raw/old.json"], output)
            self.assertFalse(target.exists())
        self.assertFalse((self.selected / "runtime").exists())

    def test_symlinked_output_parent_rejected_before_restoring(self):
        self.materialize()
        outside = self.root / "outside"
        outside.mkdir()
        link = self.selected / "linked-output"
        link.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ValueError):
            archive_reader.restore_selected(self.selected, ["quotes/raw/old.json"], link / "access.json")
        self.assertFalse((self.selected / "runtime").exists())

if __name__ == "__main__":
    unittest.main()
