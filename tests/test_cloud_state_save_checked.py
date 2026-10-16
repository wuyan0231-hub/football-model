"""Use real local bare Git remotes; no credentials or network are involved."""
from contextlib import redirect_stdout, redirect_stderr
from io import StringIO
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from scripts import cloud_state
from scripts import cloud_state_save_checked as checked
from football_model.advanced_research import implementation_fingerprint
from football_model.advanced_storage import save_artifact


class CheckedStateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.repo = self.base / "source"
        self.repo.mkdir()
        self.git("init", "-b", "main")
        self.git("config", "user.name", "Local checked-state tests")
        self.git("config", "user.email", "local@example.invalid")
        (self.repo / "source.txt").write_text("unchanged numerical source\n")
        self.git("add", "source.txt")
        self.git("commit", "-m", "source")
        self.source = self.git("rev-parse", "HEAD")
        self.remote = self.base / "state.git"
        subprocess.run(["git", "init", "--bare", str(self.remote)], check=True, capture_output=True)
        self.git("remote", "add", "origin", str(self.remote))
        self.runtime = self.repo / "runtime"
        self.runtime.mkdir()
        (self.runtime / "small.json").write_text('{"revision":1}\n')
        self.marker = self.repo / ".git/football-cloud-state.json"
        self.marker.write_text(json.dumps({"schema_version": 1, "state_oid": None,
            "source_sha": self.source, "source_ref": "refs/heads/main", "extra_preserved": {"a": 1}}))
        self.env = patch.dict(os.environ, {"GITHUB_REF": "refs/heads/main", "GITHUB_SHA": self.source,
            "GITHUB_RUN_ID": "1234", "GITHUB_RUN_ATTEMPT": "2"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.memory = patch.object(cloud_state, "_ensure_memory")
        self.memory.start()
        self.addCleanup(self.memory.stop)
        with redirect_stdout(StringIO()):
            self.initial = cloud_state.save_state(self.repo, self.runtime)
        self.original_marker = self.marker.read_bytes()

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.repo), *args], check=True,
            capture_output=True, text=True).stdout.strip()

    def context(self, operation="save"):
        return {"schema_version": checked.VERSION, "repo": str(self.repo), "runtime": str(self.runtime),
            "operation": operation, "started_at": checked._now(), "source_sha": self.source,
            "source_ref": "refs/heads/main", "github_run_id": "1234", "github_run_attempt": "2",
            "marker_path": str(self.marker), "before_marker": json.loads(self.marker.read_bytes()),
            "before_marker_sha256": checked._sha(self.marker.read_bytes()),
            "implementation_fingerprint": implementation_fingerprint(),
            "launcher_sha256": checked._sha(Path(checked.__file__).read_bytes())}

    def timeout_save(self, context, accepted=True):
        real_git = cloud_state._git
        captured = []

        def push(repo, *args, **kwargs):
            if "push" in args:
                command = ["git", "-C", str(repo), *args]
                captured.append(command)
                if accepted:
                    outcome = real_git(repo, *args, **kwargs)
                    self.assertEqual(outcome.returncode, 0)
                raise subprocess.TimeoutExpired(command, 180)
            return real_git(repo, *args, **kwargs)

        (self.runtime / "small.json").write_text('{"revision":2}\n')
        with patch.object(cloud_state, "_git", side_effect=push), redirect_stdout(StringIO()):
            with self.assertRaises(subprocess.TimeoutExpired):
                cloud_state.save_state(self.repo, self.runtime)
        self.assertEqual(len(captured), 1)
        self.assertEqual(self.marker.read_bytes(), self.original_marker)
        command = captured[0]
        return {"error_type": "TimeoutExpired", "timeout_command": command, "timeout_seconds": 180,
            "planned_commit": checked._planned_push(command, self.repo), "captured_at": checked._now()}

    def test_real_accepted_push_timeout_advances_only_loaded_marker_state(self):
        context = self.context()
        event = self.timeout_save(context)
        before = json.loads(self.marker.read_bytes())
        result = checked.recover(context, event)
        self.assertEqual(result["remote_observations"], [event["planned_commit"]] * 2)
        self.assertEqual(json.loads(self.marker.read_bytes()), {**before, "state_oid": event["planned_commit"]})
        self.assertFalse(result["push_retried"])
        self.assertEqual(self.git("rev-parse", "HEAD"), self.source)

    def test_timeout_before_remote_acceptance_never_advances_marker_or_pushes(self):
        context = self.context()
        event = self.timeout_save(context, accepted=False)
        with self.assertRaisesRegex(checked.RecoveryError, "remote_does_not_equal"):
            checked.recover(context, event)
        self.assertEqual(cloud_state._remote_oid(self.repo), self.initial)
        self.assertEqual(self.marker.read_bytes(), self.original_marker)

    def test_different_remote_commit_never_adopted_even_if_descendant(self):
        context = self.context()
        event = self.timeout_save(context)
        tree = self.git("rev-parse", event["planned_commit"] + "^{tree}")
        other = self.git("commit-tree", tree, "-p", event["planned_commit"], "-m", "other writer")
        self.git("push", "origin", other + ":refs/heads/model-state")
        with self.assertRaisesRegex(checked.RecoveryError, "remote_does_not_equal"):
            checked.recover(context, event)
        self.assertEqual(self.marker.read_bytes(), self.original_marker)

    def test_remote_change_between_reads_is_not_adopted(self):
        context = self.context()
        event = self.timeout_save(context)
        with patch.object(cloud_state, "_remote_oid", side_effect=[event["planned_commit"], self.initial]):
            with self.assertRaisesRegex(checked.RecoveryError, "remote_changed"):
                checked.recover(context, event)
        self.assertEqual(self.marker.read_bytes(), self.original_marker)

    def test_foreign_attempt_metadata_is_rejected_before_marker_change(self):
        context = self.context()
        with patch.dict(os.environ, {"GITHUB_RUN_ATTEMPT": "3"}):
            event = self.timeout_save(context)
        with self.assertRaisesRegex(checked.RecoveryError, "metadata_does_not_match"):
            checked.recover(context, event)
        self.assertEqual(self.marker.read_bytes(), self.original_marker)

    def test_wrong_parent_is_rejected_even_with_remote_and_metadata(self):
        context = self.context()
        event = self.timeout_save(context)
        context["before_marker"]["state_oid"] = self.source
        with self.assertRaisesRegex(checked.RecoveryError, "exact_child"):
            checked.recover(context, event)

    def test_changed_loaded_marker_is_never_silently_replaced(self):
        context = self.context()
        event = self.timeout_save(context)
        self.marker.write_text(self.marker.read_text() + "\n")
        changed = self.marker.read_bytes()
        with self.assertRaisesRegex(checked.RecoveryError, "original_marker_changed"):
            checked.recover(context, event)
        self.assertEqual(self.marker.read_bytes(), changed)

    def test_timestamp_outside_child_window_is_rejected(self):
        context = self.context()
        event = self.timeout_save(context)
        context["started_at"] = "2999-01-01T00:00:00Z"
        with self.assertRaisesRegex(checked.RecoveryError, "metadata_outside"):
            checked.recover(context, event)

    def test_only_exact_ordinary_push_command_is_eligible(self):
        command = ["git", "-C", str(self.repo), "-c", "pack.threads=1", "-c", "pack.windowMemory=64m",
            "-c", "pack.window=0", "push", "--porcelain", "origin", "a" * 40 + ":refs/heads/model-state"]
        self.assertEqual(checked._planned_push(command, self.repo), "a" * 40)
        for changed in (command[:-1] + ["a" * 40 + ":refs/heads/main"],
                        command[:10] + ["--force"] + command[10:],
                        command[:-2] + ["https://example.invalid/repo", command[-1]],
                        " ".join(command), command[:-1] + ["HEAD:refs/heads/model-state"]):
            with self.subTest(command=changed):
                with self.assertRaises(checked.RecoveryError):
                    checked._planned_push(changed, self.repo)

    def test_child_captures_actual_internal_module_timeout_and_preserves_124(self):
        context = {**self.context("run"), "command": [sys.executable, "-m",
            "football_model.advanced_research", "study", "--league", "championship"]}
        event = self.timeout_save(context)
        context_path, event_path = self.base / "context.json", self.base / "event.json"
        checked._exclusive(context_path, context)
        original_cwd, original_argv = Path.cwd(), sys.argv
        try:
            with patch.object(checked.runpy, "run_module", side_effect=subprocess.TimeoutExpired(event["timeout_command"], 180)) as run, redirect_stdout(StringIO()):
                code = checked._child(context_path, event_path)
            self.assertEqual(code, 124)
            run.assert_called_once_with("football_model.advanced_research", run_name="__main__")
            observed = json.loads(event_path.read_bytes())
            self.assertEqual(observed["planned_commit"], event["planned_commit"])
            self.assertEqual(observed["original_exit_code"], 124)
            self.assertEqual(observed["context_sha256"], checked._sha(context_path.read_bytes()))
        finally:
            os.chdir(original_cwd)
            sys.argv = original_argv

    def test_unrelated_timeout_event_cannot_be_turned_into_save(self):
        context = {**self.context("run"), "command": [sys.executable, "-m", "football_model.advanced_research", "study"]}
        context_path, event_path = self.base / "context.json", self.base / "event.json"
        checked._exclusive(context_path, context)
        original_cwd, original_argv = Path.cwd(), sys.argv
        try:
            with patch.object(checked.runpy, "run_module", side_effect=subprocess.TimeoutExpired(["Rscript", "fit.R"], 180)):
                self.assertEqual(checked._child(context_path, event_path), 124)
            self.assertEqual(json.loads(event_path.read_bytes())["status"], "unrecoverable_timeout")
            self.assertEqual(self.marker.read_bytes(), self.original_marker)
        finally:
            os.chdir(original_cwd)
            sys.argv = original_argv

    def checkpoint(self, context, *, bad_reference=False):
        protocol_path = self.base / "frozen-protocol.json"
        protocol_path.write_text('{"frozen":true}\n')
        protocol_hash = "a" * 64
        context["study"] = {"league": "championship", "protocol_hash": protocol_hash,
            "protocol_path": str(protocol_path), "protocol_raw_sha256": checked._sha(protocol_path.read_bytes()),
            "goalmodel_requested": True}
        generated = checked._now()
        raw = b'{"real_source":1}\n'
        source = self.runtime / "advanced_models/inputs" / (checked._sha(raw) + ".json")
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(raw)
        source_ref = {"path": source.relative_to(self.runtime).as_posix(), "sha256": checked._sha(raw)}
        if bad_reference:
            source_ref["sha256"] = "0" * 64
        report = {"league": "championship", "protocol_hash": protocol_hash, "source_commit": self.source,
            "implementation_fingerprint": context["implementation_fingerprint"], "goalmodel_requested": True,
            "generated_at": generated, "source_refs": [source_ref]}
        path = save_artifact(self.runtime, self.runtime / "advanced_models/reports/history", report)
        report_ref = {"path": path.relative_to(self.runtime).as_posix(), "sha256": checked._sha(path.read_bytes())}
        checkpoint = {"schema_version": "advanced-research-checkpoint-v1", "league": "championship",
            "source_commit": self.source, "input_identity": {"league": "championship", "protocol_hash": protocol_hash,
                "implementation_sha256": context["implementation_fingerprint"]["sha256"], "goalmodel_requested": True},
            "calculation_completed": True, "calculation_generated_at": generated, "report_ref": report_ref,
            "result": {"league": "championship", "status": "completed_development", "calculation_completed": True}}
        directory = self.runtime / "advanced_models/checkpoints/championship" / protocol_hash
        directory.mkdir(parents=True, exist_ok=True)
        checkpoint_raw = json.dumps(checkpoint).encode()
        checkpoint_path = directory / (checked._sha(checkpoint_raw) + ".json")
        checkpoint_path.write_bytes(checkpoint_raw)
        pointer = {"checkpoint_ref": {"path": checkpoint_path.relative_to(self.runtime).as_posix(),
            "sha256": checked._sha(checkpoint_raw)}}
        (directory / "latest.json").write_text(json.dumps(pointer))
        return source

    def test_internal_checkpoint_timeout_requires_real_complete_saved_closure(self):
        context = self.context("run")
        self.checkpoint(context)
        event = self.timeout_save(context)
        result = checked.recover(context, event)
        self.assertTrue(result["checkpoint"]["calculation_completed"])
        self.assertFalse(result["checkpoint"]["research_command_completed"])
        self.assertGreater(result["checkpoint"]["nested_references_verified"], 1)

    def test_missing_or_corrupt_nested_source_blocks_checkpoint_recovery(self):
        for bad_hash in (True, False):
            with self.subTest(bad_hash=bad_hash):
                if not bad_hash:
                    # Reset only this test's real bare-ref and local marker,
                    # so the two independent scenarios start from one parent.
                    self.git("--git-dir=" + str(self.remote), "update-ref", "refs/heads/model-state", self.initial)
                context = self.context("run")
                source = self.checkpoint(context, bad_reference=bad_hash)
                event = self.timeout_save(context)
                if not bad_hash:
                    source.unlink()
                with self.assertRaises((ValueError, OSError)):
                    checked.recover(context, event)
                self.assertEqual(self.marker.read_bytes(), self.original_marker)

    def test_recovery_never_accepts_current_source_fingerprint_change(self):
        context = self.context()
        event = self.timeout_save(context)
        context["implementation_fingerprint"] = {"sha256": "0" * 64}
        with self.assertRaisesRegex(checked.RecoveryError, "source_or_original_marker_changed"):
            checked.recover(context, event)
        self.assertEqual(self.marker.read_bytes(), self.original_marker)

    def test_run_receipt_reports_original_failure_and_only_recovered_save(self):
        context = self.context("run")
        self.checkpoint(context)
        event = self.timeout_save(context)
        receipt_path = self.base / "checked-receipt.json"
        real_run = subprocess.run

        def child(command, **kwargs):
            if len(command) > 2 and command[2] == "_child":
                context_path = Path(command[command.index("--context") + 1])
                event_path = Path(command[command.index("--event") + 1])
                checked._exclusive(event_path, {**event, "status": "push_timeout",
                    "schema_version": checked.VERSION, "original_exit_code": 124,
                    "context_sha256": checked._sha(context_path.read_bytes())})
                return subprocess.CompletedProcess(command, 124)
            return real_run(command, **kwargs)

        with patch.object(checked, "_context", return_value=context), patch.object(checked.subprocess, "run", side_effect=child), redirect_stdout(StringIO()) as output:
            code, receipt = checked.execute("run", repo=self.repo, runtime=self.runtime,
                receipt_path=receipt_path, command=[sys.executable, "-m", "football_model.advanced_research", "study"])
        self.assertEqual(code, 0)
        self.assertEqual(receipt["original_exit_code"], 124)
        self.assertEqual(receipt["wrapper_exit_code"], 0)
        self.assertFalse(receipt["operation_completed"])
        self.assertTrue(receipt["recovered_state_only"])
        self.assertEqual(json.loads(receipt_path.read_bytes()), receipt)
        self.assertIn("MODEL_STATE_SAVED_COMMIT=" + event["planned_commit"], output.getvalue())
        with self.assertRaises(FileExistsError):
            with patch.object(checked, "_context", return_value=context):
                checked.execute("run", repo=self.repo, runtime=self.runtime, receipt_path=receipt_path)

    def test_unsafe_or_duplicate_study_arguments_are_not_executed(self):
        protocol = self.repo / "advanced-models-v1.json"
        protocol.write_bytes((Path(checked.__file__).resolve().parents[1] / "advanced-models-v1.json").read_bytes())
        valid = [sys.executable, "-m", "football_model.advanced_research", "study", "--runtime", str(self.runtime),
            "--league", "championship", "--source-commit", self.source, "--checkpoint-state",
            "--protocol", str(protocol), "--repo", str(self.repo)]
        value = checked._study_args(valid, self.repo, self.runtime, self.source)
        self.assertEqual(value["league"], "championship")
        for command in (valid + ["--league", "epl"], valid + ["--unknown"],
                        [*valid[:1], "-m", "other_module", *valid[3:]],
                        ["/bin/sh", *valid[1:]],
                        ["all" if item == "championship" else item for item in valid]):
            with self.subTest(command=command):
                with self.assertRaises(checked.RecoveryError), redirect_stderr(StringIO()):
                    checked._study_args(command, self.repo, self.runtime, self.source)


if __name__ == "__main__":
    unittest.main()
