"""Read-only connector materialization: bytes, version binding and safe writes."""
import base64
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import quote


spec = importlib.util.spec_from_file_location(
    "materialize_model_access", Path(__file__).resolve().parents[1]
    / "scripts" / "materialize_model_access.py")
access = importlib.util.module_from_spec(spec)
spec.loader.exec_module(access)


def file_item(path, data, role="source", encoding="utf-8"):
    commit = "a" * 40 if role == "source" else "b" * 40
    blob = hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()
    return {"role": role, "path": path, "response": {
        "encoding": encoding,
        "content": data.decode() if encoding == "utf-8" else base64.encodebytes(data).decode(),
        "sha": blob,
        "display_url": "https://github.com/" + access.REPOSITORY + "/blob/"
                       + commit + "/" + quote(path),
    }}


class ModelAccessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.destination = self.root / "new"
        self.payload = {
            "schema_version": 1, "repository": access.REPOSITORY,
            "source_commit": "a" * 40, "state_commit": "b" * 40,
            "files": [file_item("docs/中文.md", "原文\r\n".encode()),
                      file_item("runtime/integration/index.json", b'{"complete":true}\n',
                                "state", "base64")],
        }

    def reject(self, payload=None):
        with self.assertRaises(access.MaterializationError):
            access.materialize(payload or self.payload, self.destination)
        self.assertFalse(self.destination.exists())
        self.assertEqual([], list(self.root.glob(".model-access-*")))

    def test_utf8_and_wrapped_base64_preserve_bytes_and_manifest(self):
        binary = bytes(range(256))
        self.payload["files"].append(file_item("runtime/binary.dat", binary, "state", "base64"))
        result = access.materialize(self.payload, self.destination)
        self.assertEqual("原文\r\n".encode(), (self.destination / "docs/中文.md").read_bytes())
        self.assertEqual(binary, (self.destination / "runtime/binary.dat").read_bytes())
        saved = json.loads((self.destination / access.MANIFEST).read_text())
        self.assertEqual(result, saved)
        self.assertEqual("files_materialized_not_model_verified", saved["status"])
        self.assertEqual(hashlib.sha256(binary).hexdigest(), saved["files"][2]["sha256"])
        self.assertEqual(256, saved["files"][2]["bytes"])

    def test_wrong_schema_repository_and_short_commit_rejected(self):
        for key, value in [("schema_version", True), ("schema_version", 2),
                           ("repository", "other/repository"), ("source_commit", "a" * 7),
                           ("state_commit", "b" * 39)]:
            with self.subTest(key=key, value=value):
                payload = copy.deepcopy(self.payload)
                payload[key] = value
                self.reject(payload)

    def test_url_must_bind_exact_repo_commit_role_and_path(self):
        original = self.payload["files"][0]["response"]["display_url"]
        for url in [original.replace("a" * 40, "main"), original.replace("a" * 40, "b" * 40),
                    original.replace(access.REPOSITORY, "other/repository"),
                    original + "?raw=1", original + "#fragment", original + "/other",
                    original.replace("https://", "http://"),
                    original.replace("github.com", "github.com.evil.example")]:
            with self.subTest(url=url):
                payload = copy.deepcopy(self.payload)
                payload["files"][0]["response"]["display_url"] = url
                self.reject(payload)

    def test_rejects_unsafe_noncanonical_and_reserved_paths(self):
        for path in ["/absolute", "../escape", "docs/../escape", "docs//a", "docs/./a",
                     "docs\\a", ".git/config", "nested/.GIT/config", "a/", "a\0b",
                     "model_access_manifest.json", "model_access_manifest.json/child"]:
            with self.subTest(path=path):
                payload = copy.deepcopy(self.payload)
                payload["files"][0] = file_item(path, b"text")
                self.reject(payload)

    def test_source_and_state_paths_are_separate(self):
        for role, path in [("source", "runtime/a"), ("source", "runtime"),
                           ("state", "docs/a"), ("state", "runtime")]:
            with self.subTest(role=role, path=path):
                payload = copy.deepcopy(self.payload)
                payload["files"] = [file_item(path, b"text", role)]
                self.reject(payload)

    def test_windows_drive_and_stream_paths_rejected_on_all_platforms(self):
        for path in ["C:/escape", "C:escape", "nested/C:escape", "file:stream"]:
            with self.subTest(path=path):
                payload = copy.deepcopy(self.payload)
                payload["files"] = [file_item(path, b"text")]
                self.reject(payload)

    def test_duplicate_and_parent_child_paths_rejected(self):
        for paths in [("same", "same"), ("parent", "parent/child"),
                      ("parent/child", "parent")]:
            with self.subTest(paths=paths):
                payload = copy.deepcopy(self.payload)
                payload["files"] = [file_item(path, b"text") for path in paths]
                self.reject(payload)

    def test_truncated_or_changed_response_is_rejected_before_any_write(self):
        self.payload["files"][-1]["response"]["content"] = base64.b64encode(b'{"complete":').decode()
        self.reject()

    def test_malformed_base64_is_not_silently_ignored(self):
        for content in ["YWJj!", "YWJ", "YWJj\u00a0"]:
            with self.subTest(content=content):
                payload = copy.deepcopy(self.payload)
                payload["files"][-1]["response"]["content"] = content
                self.reject(payload)

    def test_missing_response_url_cannot_be_replaced_with_declared_sha(self):
        del self.payload["files"][0]["response"]["display_url"]
        self.reject()

    def test_existing_destination_preserved(self):
        self.destination.mkdir()
        marker = self.destination / "keep"
        marker.write_text("original")
        with self.assertRaises(access.MaterializationError):
            access.materialize(self.payload, self.destination)
        self.assertEqual("original", marker.read_text())

    def test_symlink_destination_and_symlink_ancestor_rejected(self):
        target = self.root / "real"
        target.mkdir()
        link = self.root / "link"
        link.symlink_to(target, target_is_directory=True)
        for destination in [link, link / "new", link / "missing" / "new"]:
            with self.subTest(destination=destination):
                with self.assertRaises(access.MaterializationError):
                    access.materialize(self.payload, destination)
        self.assertEqual([], list(target.iterdir()))

    def test_dangling_destination_symlink_rejected(self):
        self.destination.symlink_to(self.root / "missing")
        with self.assertRaises(access.MaterializationError):
            access.materialize(self.payload, self.destination)
        self.assertTrue(self.destination.is_symlink())

    def test_write_failure_removes_temporary_package(self):
        original = Path.write_bytes
        calls = []

        def fail_second(path, data):
            calls.append(path)
            if len(calls) == 2:
                raise OSError("simulated write failure")
            return original(path, data)

        with patch.object(Path, "write_bytes", fail_second):
            with self.assertRaises(OSError):
                access.materialize(self.payload, self.destination)
        self.assertFalse(self.destination.exists())
        self.assertEqual([], list(self.root.glob(".model-access-*")))


if __name__ == "__main__":
    unittest.main()
