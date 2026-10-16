"""Checkpoint durability, invalidation and interrupted all-league denominators."""
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from football_model.advanced_capacity import CapacityGuard
from football_model.advanced_research import _write, digest, main
from football_model.advanced_storage import save_artifact
from football_model.research_checkpoints import ResearchCheckpoints, CheckpointError


ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 10, 16, tzinfo=timezone.utc)
IMPLEMENTATION = {"sha256": "a" * 64}


class ResearchCheckpointTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ROOT)
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name)
        self.git("init", "-b", "main")
        self.git("config", "user.name", "Checkpoint Tests")
        self.git("config", "user.email", "checkpoint@example.invalid")
        (self.repo / "source.txt").write_text("fixed source\n")
        self.git("add", "source.txt")
        self.git("commit", "-m", "source")
        self.source = self.git("rev-parse", "HEAD")
        self.runtime = self.repo / "runtime"
        (self.runtime / "data").mkdir(parents=True)
        (self.runtime / "data/serie_a.json").write_text(json.dumps({"matches": [
            {"match_id": 1, "kickoff": "2026-10-01T12:00:00Z"}]}))
        self.marker = self.repo / ".git/football-cloud-state.json"
        self.marker.write_text(json.dumps({"schema_version": 1, "state_oid": self.source,
            "source_sha": self.source, "source_ref": "refs/heads/main"}))
        self.environment = patch.dict(os.environ, {"GITHUB_REF": "refs/heads/main", "GITHUB_SHA": self.source})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.guard = CapacityGuard(self.runtime)
        self.checkpoints = ResearchCheckpoints(self.runtime, self.repo, self.source,
            guard=self.guard, clock=lambda: NOW)
        self.identity = self.checkpoints.identity("serie_a", "p" * 64, IMPLEMENTATION, True)

    def git(self, *args):
        result = subprocess.run(["git", "-C", str(self.repo), *args], check=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return result.stdout.decode().strip()

    def save(self, *args):
        self.git("add", "runtime")
        tree = self.git("write-tree")
        previous = json.loads(self.marker.read_text())["state_oid"]
        commit = self.git("commit-tree", tree, "-p", previous, "-m", "saved test state")
        self.git("update-ref", "refs/heads/model-state", commit)
        body = json.loads(self.marker.read_text())
        body["state_oid"] = commit
        self.marker.write_text(json.dumps(body))
        return commit

    def report(self, extra_refs=()):
        raw = b'{"past":1}'
        source = self.runtime / "advanced_models/inputs" / (hashlib.sha256(raw).hexdigest() + ".json")
        source.parent.mkdir(parents=True)
        source.write_bytes(raw)
        ref = {"path": source.relative_to(self.runtime).as_posix(),
               "sha256": hashlib.sha256(source.read_bytes()).hexdigest()}
        report = {"league": "serie_a", "generated_at": NOW.isoformat(), "source_refs": [ref, *extra_refs],
            "protocol_hash": "p" * 64, "implementation_fingerprint": IMPLEMENTATION,
            "goalmodel_requested": True, "requested_matches": 1, "latest_fits": {}, "request_coverage": {}}
        directory = self.runtime / "advanced_models/serie_a" / ("p" * 64) / "reports"
        path = save_artifact(self.runtime, directory / "history", report)
        _write(directory / "latest.json", {"full_report_ref": {
            "path": path.relative_to(self.runtime).as_posix(), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}})
        return report, source

    def persist(self, can_save=True, extra_refs=()):
        report, source = self.report(extra_refs)
        result = {"league": "serie_a", "status": "completed_development", "durable_saved": False,
                  "durability_status": "awaiting_checkpoint_save"}
        with patch("scripts.cloud_state.prepare_state", create=True), \
                patch("scripts.cloud_state.preflight_state", return_value={"can_save": can_save}), \
                patch("scripts.cloud_state.save_state", side_effect=self.save) as save, \
                patch("sys.stdout", new_callable=io.StringIO):
            receipt = self.checkpoints.persist("serie_a", "p" * 64, self.identity, report, result, _write)
        return receipt, source, save.call_count

    def test_saved_git_closure_can_resume_without_refitting(self):
        receipt, _, count = self.persist()
        result, reason = self.checkpoints.resume("serie_a", "p" * 64, self.identity, 14)
        self.assertEqual(count, 1)
        self.assertIsNone(reason)
        self.assertTrue(result["calculation_reused"])
        self.assertTrue(result["durable_saved"])
        self.assertEqual(result["durability_status"], "verified_saved_git_closure")
        self.assertEqual(result["durable_state_commit"], receipt["durable_state_commit"])
        self.assertGreaterEqual(result["checkpoint_closure_files_verified"], 2)

    def test_local_unpushed_receipt_never_counts_as_saved(self):
        receipt, _, count = self.persist(can_save=False)
        self.assertEqual(count, 0)
        self.assertFalse(receipt["durable_saved"])
        result, reason = self.checkpoints.resume("serie_a", "p" * 64, self.identity, 14)
        self.assertIsNone(result)
        self.assertEqual(reason, "checkpoint_saved_closure_verification_failed")

    def test_nested_source_corruption_blocks_reuse_even_with_intact_report(self):
        _, source, _ = self.persist()
        source.write_text('{"past":2}')
        result, reason = self.checkpoints.resume("serie_a", "p" * 64, self.identity, 14)
        self.assertIsNone(result)
        self.assertEqual(reason, "checkpoint_saved_closure_verification_failed")

    def test_saved_checkpoint_resumes_after_original_moves_to_cold_catalog(self):
        from football_model.history_archive import compact_existing_history
        receipt, source, _ = self.persist()
        compact_existing_history(self.runtime, receipt["durable_state_commit"], self.repo)
        self.assertFalse(source.exists())
        result, reason = self.checkpoints.resume("serie_a", "p" * 64, self.identity, 14)
        self.assertIsNone(reason)
        self.assertTrue(result["durable_saved"])

    def test_changed_implementation_or_input_requires_new_calculation(self):
        self.persist()
        for field in ("implementation_sha256", "source_inventory_sha256", "mature_target_ids_sha256",
                      "protocol_hash", "history_sha256", "goalmodel_requested"):
            identity = {**self.identity, field: "changed"}
            result, reason = self.checkpoints.resume("serie_a", "p" * 64, identity, 14)
            self.assertIsNone(result, field)
            self.assertEqual(reason, "checkpoint_inputs_protocol_or_implementation_changed")

    def test_new_source_file_changes_inventory_and_invalidates_prior_missing_data(self):
        old = self.identity["source_inventory_sha256"]
        path = self.runtime / "fundamentals/snapshots/lineups/1/new.json"
        path.parent.mkdir(parents=True)
        path.write_text('{"lineup":"newly available"}')
        after = ResearchCheckpoints(self.runtime, self.repo, self.source, guard=self.guard, clock=lambda: NOW)
        self.assertNotEqual(old, after.identity("serie_a", "p" * 64, IMPLEMENTATION, True)["source_inventory_sha256"])

    def test_stale_or_future_calculation_is_not_reused(self):
        self.persist()
        for moment in (NOW - timedelta(seconds=1), NOW + timedelta(days=15)):
            self.checkpoints.clock = lambda: moment
            result, reason = self.checkpoints.resume("serie_a", "p" * 64, self.identity, 14)
            self.assertIsNone(result)
            self.assertEqual(reason, "checkpoint_stale_or_from_future")

    def test_finalize_verifies_original_snapshot_without_claiming_current_refit(self):
        self.persist()
        changed = {**self.identity, "source_inventory_sha256": "new-source-hash"}
        result, reason = self.checkpoints.resume("serie_a", "p" * 64, changed, 14, allow_saved_snapshot=True)
        self.assertIsNone(reason)
        self.assertEqual(result["status"], "verified_saved_development_snapshot")
        self.assertEqual(result["durability_status"], "verified_saved_git_closure")
        self.assertTrue(result["saved_snapshot_verified"])
        self.assertTrue(result["durable_saved"])
        self.assertFalse(result["current_inputs_match"])
        self.assertTrue(result["current_refit_required"])
        self.assertFalse(result["calculation_reused"])
        self.assertEqual(result["snapshot_input_identity"], self.identity)

    def replay(self):
        from football_model.production_replay import Recorder
        recorder = Recorder({"competitions": [{"key": "serie_a", "mode": "domestic_baseline"}]},
            self.runtime, "train", "serie_a", lambda: NOW)
        recorder.clock()
        return recorder.save({"run_id": "real-recorder-fixture", "requested_competitions": ["serie_a"]})

    def test_actual_recorder_logical_gzip_reference_can_resume(self):
        replay = self.replay()
        raw = (self.runtime / replay["path"]).read_bytes()
        self.assertNotEqual(hashlib.sha256(raw).hexdigest(), replay["sha256"])
        self.persist(extra_refs=[replay])
        result, reason = self.checkpoints.resume("serie_a", "p" * 64, self.identity, 14)
        self.assertIsNone(reason)
        self.assertTrue(result["durable_saved"])

    def test_replay_filename_does_not_excuse_missing_registered_status(self):
        replay = self.replay()
        replay.pop("status")
        self.persist(extra_refs=[replay])
        result, reason = self.checkpoints.resume("serie_a", "p" * 64, self.identity, 14)
        self.assertIsNone(result)
        self.assertEqual(reason, "checkpoint_saved_closure_verification_failed")

    def test_registered_replay_metadata_still_must_match_actual_events(self):
        replay = self.replay()
        replay["events"] += 1
        self.persist(extra_refs=[replay])
        result, reason = self.checkpoints.resume("serie_a", "p" * 64, self.identity, 14)
        self.assertIsNone(result)
        self.assertEqual(reason, "checkpoint_saved_closure_verification_failed")

    def test_valid_reference_does_not_mask_conflicting_metadata_for_same_replay(self):
        replay = self.replay()
        bad = {**replay, "events": replay["events"] + 1}
        self.persist(extra_refs=[bad, replay])
        result, reason = self.checkpoints.resume("serie_a", "p" * 64, self.identity, 14)
        self.assertIsNone(result)
        self.assertEqual(reason, "checkpoint_saved_closure_verification_failed")

    def test_engineering_branch_cannot_enable_state_writer(self):
        with patch.dict(os.environ, {"GITHUB_REF": "refs/heads/codex/research"}):
            with self.assertRaisesRegex(CheckpointError, "matching_main_release"):
                ResearchCheckpoints(self.runtime, self.repo, self.source, guard=self.guard)

    def test_missing_restored_marker_cannot_enable_state_writer(self):
        self.marker.unlink()
        with self.assertRaises(FileNotFoundError):
            ResearchCheckpoints(self.runtime, self.repo, self.source, guard=self.guard)


class CheckpointRunDenominatorTests(unittest.TestCase):
    def run_command(self, *, save_result=None, save_error=None, resume=False, resume_only=False, snapshot=False):
        temporary = tempfile.TemporaryDirectory(dir=ROOT)
        self.addCleanup(temporary.cleanup)
        runtime = Path(temporary.name)
        (runtime / "data").mkdir()
        for league in ("serie_a", "epl", "laliga"):
            (runtime / "data" / (league + ".json")).write_text('{"matches":[]}')
        config = runtime / "test_config.json"
        config.write_text(json.dumps({"competitions": [{"key": k, "mode": "domestic_baseline"}
            for k in ("serie_a", "epl", "laliga")]}))
        with patch("football_model.research_checkpoints.ResearchCheckpoints") as manager, \
                patch("football_model.advanced_research.study", return_value={
                    "requested_matches": 0, "latest_fits": {}, "request_coverage": {}}) as study, \
                patch("sys.stdout", new_callable=io.StringIO):
            instance = manager.return_value
            instance.identity.return_value = {"input": "fixed"}
            if resume:
                instance.resume.side_effect = lambda league, *args, **kwargs: ({"league": league,
                    "status": "verified_saved_development_snapshot" if snapshot else "completed_development",
                    "calculation_reused": not snapshot,
                    "current_inputs_match": not snapshot, "current_refit_required": snapshot,
                    "durable_saved": True, "durable_state_commit": "f" * 40,
                    "checkpoint_ref": {"path": "checkpoint", "sha256": "f" * 64}}, None)
            else:
                instance.resume.return_value = (None, "checkpoint_unavailable")
            instance.persist.return_value = save_result
            instance.persist.side_effect = save_error
            code = main(["study", "--runtime", str(runtime), "--config", str(config),
                "--protocol", str(ROOT / "advanced-models-v1.json"), "--source-commit", "source",
                "--checkpoint-state", "--repo", str(ROOT), *( ["--resume-only"] if resume_only else [])])
        summary = json.loads((runtime / "advanced_models/study_runs/latest.json").read_text())
        return code, summary, study.call_count

    def test_resource_deferral_retains_computed_result_and_all_pending_leagues(self):
        code, summary, fits = self.run_command(save_result={"durable_saved": False,
            "checkpoint_status": "resource_deferred", "checkpoint_preflight": {"can_save": False}})
        self.assertEqual(code, 0)
        self.assertEqual(fits, 1)
        self.assertEqual(summary["calculation_completed_leagues"], 1)
        self.assertEqual(summary["durable_completed_leagues"], 0)
        self.assertEqual(summary["pending_leagues"], 3)
        self.assertFalse(summary["research_full_completed"])
        self.assertEqual(len(summary["results"]), 3)
        self.assertEqual(summary["results"][1]["status"], "checkpoint_blocked")

    def test_failed_save_is_failure_without_erasing_completed_calculation(self):
        code, summary, fits = self.run_command(save_error=RuntimeError("safe save failure"))
        self.assertEqual(code, 1)
        self.assertEqual(fits, 1)
        self.assertEqual(summary["checkpoint_failed_leagues"], 1)
        self.assertEqual(summary["calculation_completed_leagues"], 1)
        self.assertEqual(summary["durable_completed_leagues"], 0)
        self.assertFalse(summary["research_full_completed"])

    def test_verified_saved_all_league_resume_does_not_fit_again(self):
        code, summary, fits = self.run_command(resume=True)
        self.assertEqual(code, 0)
        self.assertEqual(fits, 0)
        self.assertEqual(summary["resumed_leagues"], 3)
        self.assertEqual(summary["durable_completed_leagues"], 3)
        self.assertTrue(summary["research_full_completed"])

    def test_resume_only_never_fits_when_checkpoint_is_missing(self):
        code, summary, fits = self.run_command(resume_only=True)
        self.assertEqual(code, 0)
        self.assertEqual(fits, 0)
        self.assertEqual(summary["pending_leagues"], 3)
        self.assertEqual(summary["current_refit_required_leagues"], 3)
        self.assertTrue(all(r["status"] == "checkpoint_resume_deferred" for r in summary["results"]))

    def test_finalize_counts_saved_snapshots_separately_from_current_input_refits(self):
        code, summary, fits = self.run_command(resume=True, resume_only=True, snapshot=True)
        self.assertEqual(code, 0)
        self.assertEqual(fits, 0)
        self.assertEqual(summary["stored_verified_completed_leagues"], 3)
        self.assertTrue(summary["stored_research_durable_full_completed"])
        self.assertEqual(summary["durable_completed_leagues"], 0)
        self.assertEqual(summary["current_refit_required_leagues"], 3)
        self.assertFalse(summary["research_full_completed"])


if __name__ == "__main__":
    unittest.main()
