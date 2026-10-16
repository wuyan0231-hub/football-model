"""Selected cold ancestors remain fixed-source verified in offline bundles."""
import base64
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

from football_model import history_archive as history
from football_model.state_storage import pack_runtime, StorageError

ROOT = Path(__file__).resolve().parents[1]


def script(name):
    spec = importlib.util.spec_from_file_location("history_connector_" + name,
                                                ROOT / "scripts" / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


access = script("materialize_model_access")
selected = script("materialize_state_archive")


class HistoryConnectorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.source = self.base / "original"
        self.name = "advanced_models/inputs/" + "a" * 64 + ".json"
        self.raw = b'{"source":"exact historical original","failure":true}\n'
        target = self.source / self.name
        target.parent.mkdir(parents=True)
        target.write_bytes(self.raw)
        self.transport = self.base / "transport"
        self.transport.mkdir()
        pack_runtime(self.source, self.transport)
        old_manifest = (self.transport / "state_storage.json").read_bytes()
        e = json.loads(old_manifest)["entries"][0]
        self.catalog = {"schema_version": history.SCHEMA_VERSION, "entries": [{**e,
            "state_commit": "b" * 40, "codec": "gzip-single-member",
            "transport_path": "state_archives/" + e["blob_sha256"] + ".json.gz",
            "manifest_sha256": hashlib.sha256(old_manifest).hexdigest()}]}
        self.payload = {"schema_version": 2, "repository": access.REPOSITORY,
                        "source_commit": "a" * 40, "state_commit": "c" * 40, "files": []}
        def item(role, path, raw, commit):
            blob = hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()
            return {"role": role, "path": path, **({"commit": commit} if role == "history" else {}),
                "response": {"encoding": "base64", "content": base64.b64encode(raw).decode(),
                    "sha": blob, "display_url": "https://github.com/" + access.REPOSITORY + "/blob/" + commit + "/" + path}}
        self.payload["files"].append(item("state", "runtime/" + history.CATALOG_RELATIVE,
                                         json.dumps(self.catalog).encode(), "c" * 40))
        self.payload["files"].append(item("history", "state_storage.json", old_manifest, "b" * 40))
        path = "state_archives/" + e["blob_sha256"] + ".json.gz"
        self.payload["files"].append(item("history", path, (self.transport / path).read_bytes(), "b" * 40))
        self.destination = self.base / "selected"

    def test_selected_ancestor_materializes_and_restores_exact_bytes_without_git(self):
        result = access.materialize(self.payload, self.destination)
        self.assertEqual(result["schema_version"], 2)
        self.assertFalse((self.destination / ".git").exists())
        path = self.destination / "runtime" / self.name
        self.assertEqual(history.read_bytes(path), self.raw)
        self.assertFalse(path.exists())
        restored = selected.restore_selected(self.destination, [self.name], self.destination / "read.json")
        self.assertEqual(path.read_bytes(), self.raw)
        self.assertIn("not_full_state", restored["status"])

    def test_missing_selected_ancestor_fails_without_network_or_output(self):
        self.payload["files"].pop()
        access.materialize(self.payload, self.destination)
        with self.assertRaises(StorageError):
            selected.restore_selected(self.destination, [self.name], self.destination / "read.json")
        self.assertFalse((self.destination / "runtime" / self.name).exists())

    def test_history_url_commit_must_match_requested_exact_source(self):
        self.payload["files"][1]["commit"] = "d" * 40
        with self.assertRaises(access.MaterializationError):
            access.materialize(self.payload, self.destination)
        self.assertFalse(self.destination.exists())

    def test_modified_offline_catalog_is_rejected(self):
        access.materialize(self.payload, self.destination)
        path = self.destination / "runtime" / self.name
        self.assertEqual(history.read_bytes(path), self.raw)
        catalog = self.destination / "runtime" / history.CATALOG_RELATIVE
        catalog.write_bytes(catalog.read_bytes() + b" ")
        with self.assertRaises(StorageError):
            history.read_bytes(path)

    def test_modified_cached_source_manifest_is_rejected(self):
        access.materialize(self.payload, self.destination)
        path = self.destination / "runtime" / self.name
        self.assertEqual(history.read_bytes(path), self.raw)
        manifest = self.destination / "history_sources" / ("b" * 40) / "state_storage.json"
        manifest.write_bytes(manifest.read_bytes() + b" ")
        with self.assertRaises(StorageError):
            history.read_bytes(path)

    def test_mixed_current_and_cold_archives_restore_each_exact_source(self):
        source = self.base / "current-runtime"
        name = "quotes/raw/current.json"
        target = source / name
        target.parent.mkdir(parents=True)
        current_raw = b'{"current":"quote original"}\n'
        target.write_bytes(current_raw)
        tree = self.base / "current-transport"
        tree.mkdir()
        pack_runtime(source, tree)
        for path in [tree / "state_storage.json", *tree.glob("state_archives/*.json.gz")]:
            data = path.read_bytes()
            relative = path.relative_to(tree).as_posix()
            blob = hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()
            self.payload["files"].append({"role": "state", "path": relative, "response": {
                "encoding": "base64", "content": base64.b64encode(data).decode(), "sha": blob,
                "display_url": "https://github.com/" + access.REPOSITORY + "/blob/" + "c" * 40 + "/" + relative}})
        access.materialize(self.payload, self.destination)
        result = selected.restore_selected(self.destination, [self.name, name], self.destination / "mixed-read.json")
        self.assertEqual(len(result["files"]), 2)
        self.assertEqual((self.destination / "runtime" / self.name).read_bytes(), self.raw)
        self.assertEqual((self.destination / "runtime" / name).read_bytes(), current_raw)


if __name__ == "__main__":
    unittest.main()
