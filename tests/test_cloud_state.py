"""Real local-Git tests; no account, API key, or network access is needed."""

from __future__ import annotations

import importlib.util
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "cloud_state.py"
SPEC = importlib.util.spec_from_file_location("football_cloud_state", SCRIPT)
cloud_state = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cloud_state)


def git(repo: Path, *args: str) -> str:
    env = os.environ.copy()
    env.update({
        "GIT_AUTHOR_NAME": "State Test", "GIT_AUTHOR_EMAIL": "test@example.invalid",
        "GIT_COMMITTER_NAME": "State Test", "GIT_COMMITTER_EMAIL": "test@example.invalid",
        "GIT_CONFIG_NOSYSTEM": "1",
    })
    result = subprocess.run(
        ["git", "-C", str(repo), *args], text=True, capture_output=True,
        env=env, check=True,
    )
    return result.stdout.strip()


class CloudStateTests(unittest.TestCase):
    def setUp(self) -> None:
        # The fixtures below use a local main branch, independent of the PR
        # ref of the CI job running this test suite.
        environment = patch.dict(os.environ, {"GITHUB_REF": ""})
        environment.start()
        self.addCleanup(environment.stop)
        self.temp = tempfile.TemporaryDirectory(prefix="football-state-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.remote = self.root / "remote.git"
        self.remote.mkdir()
        git(self.remote, "init", "--bare", "--initial-branch=main")
        self.repo = self.root / "source"
        self.repo.mkdir()
        git(self.repo, "init", "--initial-branch=main")
        (self.repo / "README.md").write_text("Source code sentinel\n", encoding="utf-8")
        (self.repo / ".gitignore").write_text("runtime/\n", encoding="utf-8")
        git(self.repo, "add", "README.md", ".gitignore")
        git(self.repo, "-c", "commit.gpgsign=false", "commit", "-m", "Source commit")
        git(self.repo, "remote", "add", "origin", str(self.remote))
        git(self.repo, "push", "-u", "origin", "main")
        self.source_sha = git(self.repo, "rev-parse", "HEAD")

    def write_model(self, repo: Path, version: int) -> None:
        runtime = repo / "runtime"
        (runtime / "models").mkdir(parents=True, exist_ok=True)
        (runtime / "reports").mkdir(exist_ok=True)
        (runtime / "cache").mkdir(exist_ok=True)
        (runtime / "models" / "epl.json").write_text(
            json.dumps({"version": version, "parameters": [0.1, 0.2]}), encoding="utf-8",
        )
        (runtime / "reports" / "latest.md").write_text(f"Version {version}\n", encoding="utf-8")
        (runtime / "cache" / "fixtures.json").write_text('{"fixtures": []}', encoding="utf-8")

    def clone(self, name: str) -> Path:
        destination = self.root / name
        git(self.root, "clone", "--branch", "main", str(self.remote), str(destination))
        return destination

    def test_first_save_is_isolated_and_preserves_source_and_local_changes(self) -> None:
        (self.repo / "README.md").write_text("Uncommitted source work\n", encoding="utf-8")
        original_status = git(self.repo, "status", "--porcelain")
        self.assertIsNone(cloud_state.load_state(self.repo))
        self.write_model(self.repo, 1)
        commit = cloud_state.save_state(self.repo)
        self.assertEqual(git(self.remote, "rev-parse", "main"), self.source_sha)
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), self.source_sha)
        self.assertEqual(git(self.repo, "symbolic-ref", "--short", "HEAD"), "main")
        self.assertEqual(git(self.repo, "status", "--porcelain"), original_status)
        self.assertEqual((self.repo / "README.md").read_text(), "Uncommitted source work\n")
        self.assertEqual(git(self.remote, "rev-parse", "model-state"), commit)
        state_files = git(self.remote, "ls-tree", "-r", "--name-only", "model-state").splitlines()
        self.assertIn("runtime/models/epl.json", state_files)
        self.assertNotIn(".gitignore", state_files)
        # No common ancestor: source history was not copied into state history.
        with self.assertRaises(subprocess.CalledProcessError):
            git(self.remote, "merge-base", "main", "model-state")

    def test_restore_cache_models_and_report(self) -> None:
        cloud_state.load_state(self.repo)
        self.write_model(self.repo, 1)
        first = cloud_state.save_state(self.repo)
        restored = self.clone("restored")
        self.assertEqual(cloud_state.load_state(restored), first)
        self.assertEqual(
            (restored / "runtime/models/epl.json").read_bytes(),
            (self.repo / "runtime/models/epl.json").read_bytes(),
        )
        self.assertTrue((restored / "runtime/cache/fixtures.json").is_file())
        self.assertTrue((restored / "runtime/reports/latest.md").is_file())
        self.assertEqual(git(restored, "rev-parse", "HEAD"), self.source_sha)

    def test_venue_registry_snapshots_and_unknown_audits_survive_cloud_runs(self) -> None:
        def register(repo: Path, revision: int) -> tuple[Path, bytes]:
            registry = {"schema_version": 1, "revision": revision, "venues": [
                {"venue_id": revision, "name": f"Registered stadium {revision}",
                 "city": "Test city", "country": "Test country"},
            ]}
            canonical = json.dumps(registry, sort_keys=True, ensure_ascii=False,
                                   separators=(",", ":")).encode("utf-8")
            digest = hashlib.sha256(canonical).hexdigest()
            directory = repo / "runtime/registries/nations_venues"
            directory.mkdir(parents=True, exist_ok=True)
            snapshot = directory / f"{digest}.json"
            snapshot.write_bytes(canonical)
            current = repo / "runtime/registries/nations_venues.json"
            current.write_text(json.dumps(registry, indent=2) + "\n", encoding="utf-8")
            audits = repo / "runtime/audits/unl"
            audits.mkdir(parents=True, exist_ok=True)
            (audits / f"run-{revision}-venues.json").write_text(
                json.dumps({"unknown_venues": [{"match_id": 1000 + revision,
                                               "reason": "country_unverified"}]}),
                encoding="utf-8",
            )
            return snapshot.relative_to(repo), canonical

        cloud_state.load_state(self.repo)
        first_snapshot, first_content = register(self.repo, 1)
        first = cloud_state.save_state(self.repo)

        # A fresh checkout simulates the next scheduled run, not an in-memory
        # copy of the writer. The saved evidence must precede new registrations.
        next_run = self.clone("venues-next-run")
        self.assertEqual(cloud_state.load_state(next_run), first)
        self.assertEqual((next_run / first_snapshot).read_bytes(), first_content)
        self.assertTrue((next_run / "runtime/audits/unl/run-1-venues.json").is_file())
        second_snapshot, second_content = register(next_run, 2)
        second = cloud_state.save_state(next_run)

        restored = self.clone("venues-restored")
        self.assertEqual(cloud_state.load_state(restored), second)
        self.assertEqual((restored / first_snapshot).read_bytes(), first_content)
        self.assertEqual((restored / second_snapshot).read_bytes(), second_content)
        current = json.loads((restored / "runtime/registries/nations_venues.json").read_text())
        self.assertEqual(current["revision"], 2)
        for revision in (1, 2):
            self.assertEqual(
                (restored / f"runtime/audits/unl/run-{revision}-venues.json").read_bytes(),
                (next_run / f"runtime/audits/unl/run-{revision}-venues.json").read_bytes(),
            )
        # Previous Git commits remain usable for the original registry too.
        original = json.loads(git(self.remote, "show",
                                  f"{first}:runtime/registries/nations_venues.json"))
        self.assertEqual(original["revision"], 1)
        self.assertEqual(git(self.remote, "rev-parse", "model-state^"), first)
        self.assertEqual(git(restored, "rev-parse", "HEAD"), self.source_sha)

    def test_deferred_venue_progress_survives_fresh_cloud_checkout(self) -> None:
        from datetime import timedelta
        from football_model.venue_registry import resolve_venues
        from tests.test_venue_registry import NOW, VenueClient, fixture

        venues = [{"id": 980001 + i, "name": f"Cloud queue ground {i}",
                   "city": "London"} for i in range(25)]
        raw = [fixture(500001 + i, venue, status="FT",
                       when=NOW - timedelta(days=i + 1))
               for i, venue in enumerate(venues)]

        def response(venue_id=None, name=None):
            i = venue_id - 980001
            return [{**venues[i], "country": "England" if i == 24 else "United Kingdom"}]

        cloud_state.load_state(self.repo)
        first_client = VenueClient(response=response)
        _, first_audit = resolve_venues(raw, first_client, self.repo / "runtime", NOW,
                                         max_lookups=24)
        self.assertEqual(len(first_client.calls), 24)
        self.assertEqual(first_audit["added"], [])
        self.assertTrue(any(item["venue"]["id"] == 980025 and
                            item["reason_code"] == "lookup_budget_deferred"
                            for item in first_audit["pending"]))
        first = cloud_state.save_state(self.repo)

        # The next scheduled job has a fresh checkout and client; its only
        # knowledge of earlier attempts comes from the persisted runtime.
        next_run = self.clone("venue-queue-next-run")
        self.assertEqual(cloud_state.load_state(next_run), first)
        second_client = VenueClient(response=response)
        registry, second_audit = resolve_venues(raw, second_client,
                                               next_run / "runtime", NOW, max_lookups=24)
        self.assertEqual(second_client.calls[0]["venue_id"], 980025)
        self.assertEqual(len(second_audit["added"]), 1)
        self.assertTrue(any(item["id"] == 980025 for item in registry["venues"]))
        self.assertTrue((next_run / "runtime" / first_audit["registry_snapshot"]).is_file())
        second = cloud_state.save_state(next_run)
        restored = self.clone("venue-queue-restored")
        self.assertEqual(cloud_state.load_state(restored), second)
        registered = json.loads((restored / "runtime/registries/nations_venues.json").read_text())
        self.assertTrue(any(item["id"] == 980025 for item in registered["venues"]))
        self.assertEqual(git(restored, "rev-parse", "HEAD"), self.source_sha)

    def test_second_save_retains_previous_version_and_provenance(self) -> None:
        cloud_state.load_state(self.repo)
        self.write_model(self.repo, 1)
        first = cloud_state.save_state(self.repo)
        self.write_model(self.repo, 2)
        second = cloud_state.save_state(self.repo)
        self.assertNotEqual(first, second)
        self.assertEqual(git(self.remote, "rev-parse", "model-state^"), first)
        old = json.loads(git(self.remote, "show", f"{first}:runtime/models/epl.json"))
        self.assertEqual(old["version"], 1)
        metadata = json.loads(git(self.remote, "show", "model-state:state_metadata.json"))
        self.assertEqual(metadata["source_sha"], self.source_sha)
        self.assertEqual(metadata["previous_state_oid"], first)
        self.assertEqual(metadata["source_ref"], "refs/heads/main")

    def test_state_metadata_preserves_explicit_ci_ref(self) -> None:
        with patch.dict(os.environ, {"GITHUB_REF": "refs/pull/42/merge"}):
            cloud_state.load_state(self.repo)
            self.write_model(self.repo, 1)
            cloud_state.save_state(self.repo)
        metadata = json.loads(git(self.remote, "show", "model-state:state_metadata.json"))
        self.assertEqual(metadata["source_ref"], "refs/pull/42/merge")
        self.assertEqual(metadata["source_sha"], self.source_sha)

    def test_remote_conflict_never_overwrites_newer_state(self) -> None:
        cloud_state.load_state(self.repo)
        self.write_model(self.repo, 1)
        cloud_state.save_state(self.repo)
        stale = self.clone("stale")
        cloud_state.load_state(stale)
        self.write_model(self.repo, 2)
        current = cloud_state.save_state(self.repo)
        self.write_model(stale, 99)
        with self.assertRaisesRegex(cloud_state.StateError, "已变化"):
            cloud_state.save_state(stale)
        self.assertEqual(git(self.remote, "rev-parse", "model-state"), current)

    def test_push_race_is_rejected_by_normal_git_push(self) -> None:
        cloud_state.load_state(self.repo)
        self.write_model(self.repo, 1)
        first = cloud_state.save_state(self.repo)
        stale = self.clone("race")
        cloud_state.load_state(stale)
        self.write_model(self.repo, 2)
        current = cloud_state.save_state(self.repo)
        self.write_model(stale, 99)
        # Simulate a remote update immediately after the baseline check.
        with patch.object(cloud_state, "_remote_oid", return_value=first):
            with self.assertRaisesRegex(cloud_state.StateError, "push 失败"):
                cloud_state.save_state(stale)
        self.assertEqual(git(self.remote, "rev-parse", "model-state"), current)
        self.assertEqual(git(stale, "rev-parse", "HEAD"), self.source_sha)

    def test_first_save_race_does_not_replace_new_state_branch(self) -> None:
        cloud_state.load_state(self.repo)
        other = self.clone("first-race")
        cloud_state.load_state(other)
        self.write_model(self.repo, 1)
        first = cloud_state.save_state(self.repo)
        self.write_model(other, 99)
        with self.assertRaisesRegex(cloud_state.StateError, "已变化"):
            cloud_state.save_state(other)
        self.assertEqual(git(self.remote, "rev-parse", "model-state"), first)

    def test_restore_refuses_to_overwrite_existing_local_runtime(self) -> None:
        self.write_model(self.repo, 77)
        with self.assertRaisesRegex(cloud_state.StateError, "已有内容"):
            cloud_state.load_state(self.repo)
        self.assertEqual(json.loads((self.repo / "runtime/models/epl.json").read_text())["version"], 77)
        with self.assertRaisesRegex(cloud_state.StateError, "尚未成功 load"):
            cloud_state.save_state(self.repo)

    def test_failed_load_invalidates_previous_save_authorization(self) -> None:
        cloud_state.load_state(self.repo)
        self.write_model(self.repo, 1)
        with self.assertRaises(cloud_state.StateError):
            cloud_state.load_state(self.repo)
        with self.assertRaisesRegex(cloud_state.StateError, "尚未成功 load"):
            cloud_state.save_state(self.repo)

    def test_secrets_and_symlinks_are_not_persisted(self) -> None:
        cloud_state.load_state(self.repo)
        self.write_model(self.repo, 1)
        secret = "test-only-secret-123456"
        report = self.repo / "runtime/reports/latest.md"
        report.write_text(secret, encoding="utf-8")
        with patch.dict(os.environ, {"FOOTBALL_API_KEY": secret}):
            with self.assertRaisesRegex(cloud_state.StateError, "凭据"):
                cloud_state.save_state(self.repo)
        report.write_text("Safe", encoding="utf-8")
        link = self.repo / "runtime/cache/link.json"
        link.symlink_to(self.repo / "README.md")
        with self.assertRaisesRegex(cloud_state.StateError, "符号链接"):
            cloud_state.save_state(self.repo)
        self.assertIsNone(cloud_state._remote_oid(self.repo))

    def test_venue_registry_files_keep_credential_and_path_checks(self) -> None:
        cloud_state.load_state(self.repo)
        registries = self.repo / "runtime/registries"
        snapshots = registries / "nations_venues"
        snapshots.mkdir(parents=True)
        current = registries / "nations_venues.json"
        secret = "test-only-venue-secret-123456"
        current.write_text(json.dumps({"evidence": secret}), encoding="utf-8")
        with patch.dict(os.environ, {"FOOTBALL_API_KEY": secret}):
            with self.assertRaisesRegex(cloud_state.StateError, "凭据"):
                cloud_state.save_state(self.repo)
        current.write_text('{"venues": []}', encoding="utf-8")
        link = snapshots / ("a" * 64 + ".json")
        link.symlink_to(self.repo / "README.md")
        with self.assertRaisesRegex(cloud_state.StateError, "符号链接"):
            cloud_state.save_state(self.repo)
        link.unlink()
        hidden = snapshots / ".credentials.json"
        hidden.write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(cloud_state.StateError, "隐藏"):
            cloud_state.save_state(self.repo)
        self.assertIsNone(cloud_state._remote_oid(self.repo))

    def test_runtime_path_cannot_point_to_source(self) -> None:
        with self.assertRaisesRegex(cloud_state.StateError, "状态目录必须"):
            cloud_state.load_state(self.repo, self.repo)
        self.assertEqual((self.repo / "README.md").read_text(), "Source code sentinel\n")

    def test_empty_state_can_be_saved_and_restored(self) -> None:
        cloud_state.load_state(self.repo)
        cloud_state.save_state(self.repo)
        other = self.clone("empty")
        cloud_state.load_state(other)
        self.assertTrue((other / "runtime/state_manifest.json").is_file())


if __name__ == "__main__":
    unittest.main()
