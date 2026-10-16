"""Cold-source admission preserves disk for a later normal state save."""
from pathlib import Path
import os
import subprocess
import sys
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
        self.assertEqual(history.COLD_FETCH_WORKSPACE_BYTES, 512 * 1024**2)
        self.assertEqual(history.COLD_FETCH_SAVE_RESERVE_BYTES, 2 * MAX_PHYSICAL_BYTES + 512 * 1024**2)
        self.assertEqual(history.COLD_FETCH_REQUIRED_FREE_BYTES, 6400 * 1024**2)

    def test_tight_disk_refuses_before_fetch_and_does_not_cache_commit(self):
        with patch.object(history, "_git", return_value=None) as git, patch.object(history, "_fetch_cold_commit") as fetch, patch.object(
                history.shutil, "disk_usage", return_value=SimpleNamespace(
                    free=history.COLD_FETCH_REQUIRED_FREE_BYTES - 1)):
            with self.assertRaisesRegex(history.StorageError, "磁盘余量不足"):
                history._ensure_commit(self.repo, self.commit)
        self.assertEqual(len(git.call_args_list), 1)
        self.assertEqual(git.call_args.args[1][0], "cat-file")
        fetch.assert_not_called()
        self.assertNotIn(self.key, history._VERIFIED_COMMITS)

    def test_unknown_disk_refuses_before_fetch_and_does_not_cache_commit(self):
        with patch.object(history, "_git", return_value=None) as git, patch.object(history, "_fetch_cold_commit") as fetch, patch.object(
                history.shutil, "disk_usage", side_effect=OSError("synthetic unavailable disk")):
            with self.assertRaisesRegex(history.StorageError, "无法核实"):
                history._ensure_commit(self.repo, self.commit)
        self.assertEqual(len(git.call_args_list), 1)
        fetch.assert_not_called()
        self.assertNotIn(self.key, history._VERIFIED_COMMITS)

    def test_present_commit_and_cache_never_run_new_source_disk_gate(self):
        with patch.object(history, "_git", return_value=b"") as git, patch.object(history, "_fetch_cold_commit") as fetch, patch.object(
                history.shutil, "disk_usage", side_effect=AssertionError("unexpected disk gate")):
            history._ensure_commit(self.repo, self.commit)
            history._ensure_commit(self.repo, self.commit)
        self.assertEqual(len(git.call_args_list), 1)
        fetch.assert_not_called()
        self.assertIn(self.key, history._VERIFIED_COMMITS)

    def test_exact_admission_boundary_fetches_once_and_then_reuses_commit(self):
        with patch.object(history, "_git", side_effect=[None, b""]) as git, patch.object(history, "_fetch_cold_commit") as fetch, patch.object(
                history.shutil, "disk_usage", return_value=SimpleNamespace(
                    free=history.COLD_FETCH_REQUIRED_FREE_BYTES)) as disk:
            history._ensure_commit(self.repo, self.commit)
            history._ensure_commit(self.repo, self.commit)
        self.assertEqual(len(git.call_args_list), 2)
        fetch.assert_called_once_with(self.repo, self.commit, MAX_PHYSICAL_BYTES)
        self.assertEqual(disk.call_count, 1)
        self.assertIn(self.key, history._VERIFIED_COMMITS)

    def test_failed_fetch_does_not_cache_and_retry_rechecks_disk(self):
        with patch.object(history, "_git", side_effect=[None, None, b""]) as git, patch.object(
                history, "_fetch_cold_commit", side_effect=[history.StorageError("fetch_failed"), None]) as fetch, patch.object(history.shutil, "disk_usage",
                return_value=SimpleNamespace(free=history.COLD_FETCH_REQUIRED_FREE_BYTES)) as disk:
            with self.assertRaisesRegex(history.StorageError, "fetch_failed"):
                history._ensure_commit(self.repo, self.commit)
            self.assertNotIn(self.key, history._VERIFIED_COMMITS)
            history._ensure_commit(self.repo, self.commit)
        self.assertEqual(disk.call_count, 2)
        self.assertEqual(len(git.call_args_list), 3)
        self.assertEqual(fetch.call_count, 2)
        self.assertIn(self.key, history._VERIFIED_COMMITS)

    def test_each_new_source_rechecks_actual_remaining_disk(self):
        second = "b" * 40
        second_key = (str(self.repo), second)
        history._VERIFIED_COMMITS.discard(second_key)
        self.addCleanup(history._VERIFIED_COMMITS.discard, second_key)
        with patch.object(history, "_git", side_effect=[None, b"", None]) as git, patch.object(history, "_fetch_cold_commit") as fetch, patch.object(
                history.shutil, "disk_usage", side_effect=[
                    SimpleNamespace(free=history.COLD_FETCH_REQUIRED_FREE_BYTES),
                    SimpleNamespace(free=history.COLD_FETCH_REQUIRED_FREE_BYTES - 1)]) as disk:
            history._ensure_commit(self.repo, self.commit)
            with self.assertRaisesRegex(history.StorageError, "磁盘余量不足"):
                history._ensure_commit(self.repo, second)
        self.assertEqual(disk.call_count, 2)
        self.assertEqual(len(git.call_args_list), 3)
        self.assertEqual(fetch.call_count, 1)
        self.assertIn(self.key, history._VERIFIED_COMMITS)
        self.assertNotIn(second_key, history._VERIFIED_COMMITS)

    def test_fetch_has_no_background_repack_and_uses_separate_limited_launcher(self):
        with patch.object(history.subprocess, "Popen") as popen:
            popen.return_value.communicate.return_value = (b"", None)
            popen.return_value.returncode = 0
            history._fetch_cold_commit(self.repo, self.commit, 123456)
        command = popen.call_args.args[0]
        self.assertEqual(command[:2], [sys.executable, "-c"])
        self.assertIn("RLIMIT_FSIZE", command[2])
        self.assertEqual(command[3], "123456")
        self.assertIn("--keep", command)
        self.assertIn("--no-auto-maintenance", command)
        self.assertIn("--no-write-commit-graph", command)
        self.assertIn("--no-recurse-submodules", command)
        self.assertNotIn("--refetch", command)
        self.assertNotIn("preexec_fn", popen.call_args.kwargs)
        self.assertIs(popen.call_args.kwargs["start_new_session"], True)

    @unittest.skipUnless(os.name == "posix", "POSIX resource hard limit")
    def test_real_subprocess_cannot_extend_pack_like_file_past_hard_limit(self):
        target = self.repo / "receiving-pack"
        script = "from pathlib import Path; import sys; Path(sys.argv[1]).write_bytes(b'x'*16384)"
        command = history._file_limited_command([sys.executable, "-c", script, str(target)], 4096)
        result = subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                timeout=10)
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(target.is_file())
        self.assertLessEqual(target.stat().st_size, 4096)

    def test_file_limit_uses_actual_free_space_after_both_reserves(self):
        extra = 128 * 1024**2
        with patch.object(history.shutil, "disk_usage", return_value=SimpleNamespace(
                free=history.COLD_FETCH_REQUIRED_FREE_BYTES + extra)):
            cap = history._ensure_cold_fetch_disk(self.repo)
        self.assertEqual(cap, MAX_PHYSICAL_BYTES + extra)

    def test_timeout_kills_entire_isolated_receive_process_group(self):
        with patch.object(history.subprocess, "Popen") as popen, patch.object(history.os, "killpg") as kill:
            process = popen.return_value
            process.pid = 12345
            process.communicate.side_effect = [subprocess.TimeoutExpired("git", 180), (b"", None)]
            with self.assertRaisesRegex(history.StorageError, "超时"):
                history._fetch_cold_commit(self.repo, self.commit, 123456)
        kill.assert_called_once_with(12345, history.signal.SIGKILL)
        self.assertEqual(process.communicate.call_count, 2)


if __name__ == "__main__":
    unittest.main()
