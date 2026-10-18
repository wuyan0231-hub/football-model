"""Storage acceptance regressions using only disposable local Git origins."""
from contextlib import redirect_stdout
from io import StringIO
import json
import os
import shutil
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from scripts import cloud_state
from scripts import engineering_storage_roundtrip as target
from scripts.cloud_state_save_once import save_once

ROOT = Path(__file__).resolve().parents[1]


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args], stderr=subprocess.DEVNULL,
        env={**os.environ, "GIT_AUTHOR_NAME": "Synthetic test", "GIT_AUTHOR_EMAIL": "test@example.invalid",
             "GIT_COMMITTER_NAME": "Synthetic test", "GIT_COMMITTER_EMAIL": "test@example.invalid"}, text=True).strip()


class RoundtripTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ.get("ENGINEERING_TEST_TMP"))
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.origin = self.root / "origin.git"
        self.origin.mkdir()
        git(self.origin, "init", "--bare", "--initial-branch=main")
        self.repo = self.root / "source"
        self.repo.mkdir()
        git(self.repo, "init", "--initial-branch=main")
        (self.repo / "README.md").write_text("Synthetic source\n")
        git(self.repo, "add", "README.md")
        git(self.repo, "-c", "commit.gpgsign=false", "commit", "-m", "Synthetic source")
        git(self.repo, "remote", "add", "origin", str(self.origin))
        git(self.repo, "push", "origin", "main")
        self.source = git(self.repo, "rev-parse", "HEAD")
        self.runtime = self.repo / "runtime"
        self.output = self.repo / "result.json"
        self.env = {"GITHUB_REF": "refs/heads/main", "GITHUB_EVENT_NAME": "workflow_dispatch",
            "GITHUB_SHA": self.source, "GITHUB_REPOSITORY": "wuyan0231-hub/football-model",
            "GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "1", "FOOTBALL_API_KEY": "",
            "GITHUB_OUTPUT": str(self.repo / "github-output"),
            "RESTORE": "success", "PREPARE": "success", "CHECK": "success", "PERSIST": "success"}
        for manager in (patch.dict(os.environ, self.env), patch.object(target, "ROOT", self.repo)):
            manager.start(); self.addCleanup(manager.stop)
        with redirect_stdout(StringIO()):
            cloud_state.load_state(self.repo)
            (self.runtime / "models").mkdir()
            (self.runtime / "models/synthetic.json").write_text('{"synthetic":true}')
            self.parent = cloud_state.save_state(self.repo)
        # The actual roundtrip must start from a complete successful load.
        shutil.rmtree(self.runtime)
        with redirect_stdout(StringIO()):
            cloud_state.load_state(self.repo)

    def prepare(self):
        return target.prepare(self.repo, self.runtime)

    def saved(self):
        self.prepare()
        cloud_state._validate_runtime(self.runtime)
        before = target.marker(self.repo)
        receipt = self.repo / "save.json"
        # A local Python child invokes the exact native cloud_state save. It
        # has no network/API endpoint; its origin is this disposable bare Git.
        command = [os.sys.executable, str(ROOT / "scripts/cloud_state.py"), "save", "--repo", str(self.repo), "--runtime", str(self.runtime)]
        with redirect_stdout(StringIO()):
            code, value = save_once(command, receipt_path=receipt, before_marker=before,
                after_marker=lambda: target.marker(self.repo), source_sha=self.source,
                runtime_path=self.runtime, run_id="123", run_attempt="1")
        self.assertEqual(code, 0)
        self.assertEqual(target.record(self.repo, self.runtime, receipt, self.output), 0)
        return json.loads(self.output.read_text()), receipt

    def test_real_local_native_roundtrip_then_fresh_checkout_fixed_readback(self):
        original = (self.runtime / "models/synthetic.json").read_bytes()
        result, _ = self.saved()
        reader = self.root / "fresh-read-job"
        git(self.root, "clone", "--branch", "main", str(self.origin), str(reader))
        with patch.object(target, "ROOT", reader):
            self.assertEqual(target.audit(reader, result["saved_state_oid"], self.parent,
                result["receipt_ref"], reader / "readback.json"), 0)
        self.assertFalse((reader / "runtime").exists())
        from football_model.history_archive import FixedStateReader
        with FixedStateReader(reader, result["saved_state_oid"]) as fixed:
            self.assertEqual(fixed.read("models/synthetic.json"), original)
        self.assertEqual(json.loads((reader / "readback.json").read_text())["status"], "verified")

    def test_receipt_and_native_save_are_bound_to_parent_source_run_and_sha(self):
        result, receipt = self.saved()
        saved = json.loads(receipt.read_text())
        self.assertEqual(result["parent_state_oid"], self.parent)
        self.assertNotEqual(result["saved_state_oid"], self.parent)
        reference = result["receipt_ref"]
        self.assertEqual(reference["path"], "engineering/storage_roundtrips/123/1.json")
        self.assertEqual(target.audit(self.repo, result["saved_state_oid"], self.parent, reference, self.output), 0)
        readback = json.loads(self.output.read_text())
        self.assertEqual(readback["status"], "verified")
        self.assertEqual(readback["scope"]["fits"], 0)
        self.assertEqual(saved["save"]["status"], "saved_and_marker_verified")

    def test_corrupted_reference_and_parent_cannot_pass_independent_readback(self):
        result, _ = self.saved()
        for state, parent, ref in ((result["saved_state_oid"], self.parent, {**result["receipt_ref"], "sha256": "f" * 64}),
                (result["saved_state_oid"], "f" * 40, result["receipt_ref"]),
                (result["saved_state_oid"], self.parent, {**result["receipt_ref"], "path": "../outside"})):
            with self.subTest(parent=parent, ref=ref), self.assertRaises(ValueError):
                target.audit(self.repo, state, parent, ref, self.output)

    def test_prior_attempt_or_implementation_identity_cannot_relabel_receipt(self):
        result, _ = self.saved()
        with patch.dict(os.environ, {"GITHUB_RUN_ATTEMPT": "2"}), self.assertRaises(ValueError):
            target.audit(self.repo, result["saved_state_oid"], self.parent, result["receipt_ref"], self.output)
        with patch.object(target, "implementation", return_value={"sha256": "d" * 64, "file_count": 50}), self.assertRaises(ValueError):
            target.audit(self.repo, result["saved_state_oid"], self.parent, result["receipt_ref"], self.output)

    def test_saved_metadata_source_run_parent_and_time_are_independently_required(self):
        result, _ = self.saved()
        from football_model.history_archive import FixedStateReader
        class MetadataReader:
            def __init__(self, repo, state, mutation):
                self.reader, self.mutation = FixedStateReader(repo, state), mutation
            def __enter__(self):
                return self
            def __exit__(self, *args):
                self.reader.close()
            def validate_manifest(self):
                return self.reader.validate_manifest()
            def read(self, relative):
                return self.reader.read(relative)
            def root_bytes(self, relative):
                value = json.loads(self.reader.root_bytes(relative))
                return json.dumps({**value, **self.mutation}).encode()
        for changed in ({"source_sha": "f" * 40}, {"github_run_id": "456"},
                        {"github_run_attempt": "2"}, {"previous_state_oid": "f" * 40},
                        {"source_ref": "refs/heads/other"}, {"saved_at": "2000-01-01T00:00:00Z"}):
            with self.subTest(changed=changed), patch("football_model.history_archive.FixedStateReader",
                    side_effect=lambda repo, state: MetadataReader(repo, state, changed)), self.assertRaises(ValueError):
                target.audit(self.repo, result["saved_state_oid"], self.parent, result["receipt_ref"], self.output)

    def test_failed_skipped_and_unbound_save_not_promoted(self):
        result, receipt = self.saved()
        baseline = json.loads(receipt.read_text())
        for changes in ({"child_exit_code": 1}, {"github_run_id": "999"}, {"parent_state_oid": "a" * 40},
                        {"receipt_parse_status": "invalid"}, {"post_state_oid": self.parent}):
            receipt.write_text(json.dumps({**baseline, **changes}))
            with self.subTest(changes=changes):
                self.assertEqual(target.record(self.repo, self.runtime, receipt, self.output), 1)
                self.assertIsNone(json.loads(self.output.read_text())["saved_state_oid"])
        receipt.write_text(json.dumps(baseline))
        with patch.dict(os.environ, {"CHECK": "skipped"}):
            self.assertEqual(target.record(self.repo, self.runtime, receipt, self.output), 1)

    def test_existing_receipt_or_nonmanual_nonmain_context_rejected(self):
        self.prepare()
        with self.assertRaises(FileExistsError):
            self.prepare()
        for changed in ({"GITHUB_EVENT_NAME": "schedule"}, {"GITHUB_REF": "refs/heads/other"},
                        {"GITHUB_SHA": "f" * 40}, {"GITHUB_RUN_ID": "1\n"}):
            with self.subTest(changed=changed), patch.dict(os.environ, changed), self.assertRaises(ValueError):
                self.prepare()

    def test_existing_loaded_parent_required_and_no_state_init(self):
        before = target.marker(self.repo)
        gitdir = Path(git(self.repo, "rev-parse", "--absolute-git-dir"))
        (gitdir / "football-cloud-state.json").write_text(json.dumps({**before, "state_oid": None}))
        with self.assertRaises(ValueError):
            self.prepare()

    def test_oversized_and_symlink_receipts_rejected(self):
        self.prepare()
        path = self.runtime / "engineering/storage_roundtrips/123/1.json"
        path.write_bytes(b" " * (target.MAX_RECEIPT_BYTES + 1))
        with self.assertRaises(ValueError):
            target.bounded_json(path)
        path.unlink()
        path.symlink_to(self.repo / "README.md")
        with self.assertRaises(ValueError):
            target.bounded_json(path)


class WorkflowTests(unittest.TestCase):
    def test_manual_only_shared_writer_then_separate_fixed_reader(self):
        workflow = (ROOT / ".github/workflows/engineering-storage-roundtrip.yml").read_text()
        self.assertIn("workflow_dispatch:", workflow)
        self.assertNotIn("schedule:", workflow)
        self.assertNotIn("push:", workflow)
        self.assertIn("group: football-model-state\n      cancel-in-progress: false\n      queue: max", workflow)
        self.assertEqual(workflow.count("scripts/cloud_state_save_once.py"), 1)
        self.assertIn("needs: storage", workflow)
        self.assertIn("needs.storage.result == 'success'", workflow)
        self.assertNotIn("secrets.FOOTBALL_API_KEY", workflow)
        self.assertNotIn("advanced_research study", workflow)
        self.assertNotIn("advanced_snapshots capture", workflow)
        self.assertNotIn("runtime/\n", workflow.split("path: |", 1)[1])


if __name__ == "__main__":
    unittest.main()
