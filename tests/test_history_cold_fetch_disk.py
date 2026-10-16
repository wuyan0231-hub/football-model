"""Cold-source admission preserves disk for a later normal state save."""
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from football_model import history_archive as history
from football_model.storage_policy import MAX_PHYSICAL_BYTES


class ColdFetchDiskTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name)
        self.commit = "a" * 40
        self.key = (str(self.repo), self.commit)
        history._VERIFIED_COMMITS.discard(self.key)
        self.addCleanup(history._VERIFIED_COMMITS.discard, self.key)

    def test_budget_preserves_complete_receive_and_later_save_envelopes(self):
        self.assertEqual(history.COLD_FETCH_SNAPSHOT_BYTES, MAX_PHYSICAL_BYTES)
        self.assertEqual(history.COLD_FETCH_WORKSPACE_BYTES, MAX_PHYSICAL_BYTES + 256 * 1024**2)
        self.assertEqual(history.COLD_FETCH_SAVE_RESERVE_BYTES, 2 * MAX_PHYSICAL_BYTES + 512 * 1024**2)
        self.assertEqual(history.COLD_FETCH_REQUIRED_FREE_BYTES, 7936 * 1024**2)

    def test_tight_disk_refuses_before_fetch_and_does_not_cache_commit(self):
        with patch.object(history, "_git", return_value=None) as git, patch.object(
                history.shutil, "disk_usage", return_value=SimpleNamespace(
                    free=history.COLD_FETCH_REQUIRED_FREE_BYTES - 1)):
            with self.assertRaisesRegex(history.StorageError, "磁盘余量不足"):
                history._ensure_commit(self.repo, self.commit)
        self.assertEqual(len(git.call_args_list), 1)
        self.assertEqual(git.call_args.args[1][0], "cat-file")
        self.assertNotIn(self.key, history._VERIFIED_COMMITS)

    def test_unknown_disk_refuses_before_fetch_and_does_not_cache_commit(self):
        with patch.object(history, "_git", return_value=None) as git, patch.object(
                history.shutil, "disk_usage", side_effect=OSError("synthetic unavailable disk")):
            with self.assertRaisesRegex(history.StorageError, "无法核实"):
                history._ensure_commit(self.repo, self.commit)
        self.assertEqual(len(git.call_args_list), 1)
        self.assertNotIn(self.key, history._VERIFIED_COMMITS)

    def test_present_commit_and_cache_never_run_new_source_disk_gate(self):
        with patch.object(history, "_git", return_value=b"") as git, patch.object(
                history.shutil, "disk_usage", side_effect=AssertionError("unexpected disk gate")):
            history._ensure_commit(self.repo, self.commit)
            history._ensure_commit(self.repo, self.commit)
        self.assertEqual(len(git.call_args_list), 1)
        self.assertIn(self.key, history._VERIFIED_COMMITS)

    def test_exact_admission_boundary_fetches_once_and_then_reuses_commit(self):
        with patch.object(history, "_git", side_effect=[None, b"", b""]) as git, patch.object(
                history.shutil, "disk_usage", return_value=SimpleNamespace(
                    free=history.COLD_FETCH_REQUIRED_FREE_BYTES)) as disk:
            history._ensure_commit(self.repo, self.commit)
            history._ensure_commit(self.repo, self.commit)
        self.assertEqual(len(git.call_args_list), 3)
        self.assertEqual(git.call_args_list[1].args[1],
                         ["fetch", "--quiet", "--no-tags", "--depth=1", "origin", self.commit])
        self.assertEqual(disk.call_count, 1)
        self.assertIn(self.key, history._VERIFIED_COMMITS)

    def test_failed_fetch_does_not_cache_and_retry_rechecks_disk(self):
        with patch.object(history, "_git", side_effect=[None, history.StorageError("fetch_failed"),
                None, b"", b""]) as git, patch.object(history.shutil, "disk_usage",
                return_value=SimpleNamespace(free=history.COLD_FETCH_REQUIRED_FREE_BYTES)) as disk:
            with self.assertRaisesRegex(history.StorageError, "fetch_failed"):
                history._ensure_commit(self.repo, self.commit)
            self.assertNotIn(self.key, history._VERIFIED_COMMITS)
            history._ensure_commit(self.repo, self.commit)
        self.assertEqual(disk.call_count, 2)
        self.assertEqual(len(git.call_args_list), 5)
        self.assertIn(self.key, history._VERIFIED_COMMITS)

    def test_each_new_source_rechecks_actual_remaining_disk(self):
        second = "b" * 40
        second_key = (str(self.repo), second)
        history._VERIFIED_COMMITS.discard(second_key)
        self.addCleanup(history._VERIFIED_COMMITS.discard, second_key)
        with patch.object(history, "_git", side_effect=[None, b"", b"", None]) as git, patch.object(
                history.shutil, "disk_usage", side_effect=[
                    SimpleNamespace(free=history.COLD_FETCH_REQUIRED_FREE_BYTES),
                    SimpleNamespace(free=history.COLD_FETCH_REQUIRED_FREE_BYTES - 1)]) as disk:
            history._ensure_commit(self.repo, self.commit)
            with self.assertRaisesRegex(history.StorageError, "磁盘余量不足"):
                history._ensure_commit(self.repo, second)
        self.assertEqual(disk.call_count, 2)
        self.assertEqual(len(git.call_args_list), 4)
        self.assertIn(self.key, history._VERIFIED_COMMITS)
        self.assertNotIn(second_key, history._VERIFIED_COMMITS)


if __name__ == "__main__":
    unittest.main()
