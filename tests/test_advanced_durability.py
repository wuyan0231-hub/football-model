"""Remote service timestamps must bind the exact state commit, not copied metadata."""
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, Mock
from urllib.error import HTTPError

from football_model import advanced_durability as durability


STATE = "a" * 40
SOURCE = "b" * 40
ENV = {"GITHUB_REPOSITORY": "owner/repo", "GITHUB_TOKEN": "test-token-not-a-real-credential"}
META = {"github_run_id": "123", "github_run_attempt": "1", "source_sha": SOURCE}


def api(url, token):
    if "/jobs?" in url:
        return {"total_count": 1, "jobs": [{"id": 456, "run_id": 123, "status": "completed",
            "completed_at": "2026-10-13T01:00:00Z", "name": "save runtime"}]}, "c" * 64
    return {"id": 123, "run_attempt": 1, "head_sha": SOURCE, "status": "completed"}, "d" * 64


class DurabilityTests(unittest.TestCase):
    def setUp(self):
        durability._VERIFIED_REMOTE_CACHE.clear()
    def remote(self, log, metadata=None):
        with patch.dict("os.environ", ENV), patch.object(durability, "_api_json", side_effect=api), \
                patch.object(durability, "_job_log", return_value=log):
            return durability._remote_completion(metadata or META, STATE)

    def test_copied_old_run_metadata_cannot_bind_new_state(self):
        self.assertIsNone(self.remote(b"2026-10-13T00:59:00Z MODEL_STATE_SAVED_COMMIT=" + b"e" * 40 + b"\n"))
        self.assertIsNone(self.remote(b"echo MODEL_STATE_SAVED_COMMIT=" + STATE.encode() + b"\n"))
        self.assertIsNone(self.remote(b"completed successfully\n"))

    def test_exact_service_job_log_commit_binding_uses_job_completion(self):
        proof = self.remote(b"2026-10-13T00:59:00.123Z MODEL_STATE_SAVED_COMMIT=" + STATE.encode() + b"\n")
        self.assertEqual(proof["state_commit"], STATE)
        self.assertEqual(proof["job_id"], "456")
        self.assertEqual(proof["observed_completion_upper_bound"], "2026-10-13T01:00:00Z")
        self.assertEqual(proof["evidence_sha256"], durability._sha(durability._canonical({k:v for k,v in proof.items() if k != "evidence_sha256"})))

    def test_fixed_attempt_endpoint_is_stable_across_later_rerun(self):
        calls = []
        def inspected(url, token):
            calls.append(url)
            self.assertIn("/attempts/1", url)
            return api(url, token)
        with patch.dict("os.environ", ENV), patch.object(durability, "_api_json", side_effect=inspected), \
                patch.object(durability, "_job_log", return_value=b"MODEL_STATE_SAVED_COMMIT=" + STATE.encode()):
            self.assertIsNotNone(durability._remote_completion(META, STATE))
        self.assertEqual(len(calls), 2)
        self.assertIsNone(self.remote(b"MODEL_STATE_SAVED_COMMIT=" + STATE.encode(), {**META, "github_run_attempt": "2"}))

    def test_completed_checkpoint_job_qualifies_while_research_job_still_runs(self):
        def in_progress(url, token):
            value, digest = api(url, token)
            if "/jobs?" not in url:
                value["status"] = "in_progress"
            return value, digest
        with patch.dict("os.environ", ENV), patch.object(durability, "_api_json", side_effect=in_progress), \
                patch.object(durability, "_job_log", return_value=b"MODEL_STATE_SAVED_COMMIT=" + STATE.encode()):
            self.assertIsNotNone(durability._remote_completion(META, STATE))

    def test_later_job_status_does_not_change_already_bound_job_identity(self):
        first = self.remote(b"MODEL_STATE_SAVED_COMMIT=" + STATE.encode())
        later = {**first, "run_response_sha256": "1" * 64, "job_list_response_sha256": ["2" * 64]}
        self.assertTrue(durability._same_bound_evidence(first, later))
        later["job_log_sha256"] = "3" * 64
        self.assertFalse(durability._same_bound_evidence(first, later))

    def test_job_log_signed_redirect_receives_no_authorization_header(self):
        authorized = Mock()
        authorized.open.side_effect = HTTPError("https://api.github.com/", 302, "redirect",
            {"Location": "https://logs.blob.core.windows.net/signed?sig=temporary"}, None)
        with patch.object(durability, "build_opener", return_value=authorized), \
                patch.object(durability, "urlopen", return_value=io.BytesIO(b"bound log")) as downloaded:
            self.assertEqual(durability._job_log("https://api.github.com/repos/o/r/actions/jobs/1/logs", "private"), b"bound log")
        request = downloaded.call_args.args[0]
        self.assertFalse(any(k.lower() == "authorization" for k, v in request.header_items()))
        self.assertEqual(authorized.open.call_args.args[0].get_header("Authorization"), "Bearer private")

    def test_expired_log_cache_requires_exact_previously_restored_git_bytes(self):
        evidence = self.remote(b"MODEL_STATE_SAVED_COMMIT=" + STATE.encode())
        proof = {"verified": True, "method": durability.METHOD, "state_commit": STATE,
            "remote_run_completion": evidence, "service_evidence_verified_at": "2026-10-13T02:00:00Z"}
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary); runtime = repo / "runtime"; runtime.mkdir()
            git = repo / "git"; git.mkdir()
            (git / "football-cloud-state.json").write_text(json.dumps({"state_oid": "f" * 40}))
            path = runtime / "proof.json"; path.write_text(json.dumps(proof))
            with patch.object(durability, "_git", return_value=str(git).encode()), \
                    patch.object(durability, "_tree_file", return_value=path.read_bytes()):
                self.assertEqual(durability._stored_service_evidence(repo, runtime, path, proof, META, STATE), evidence)
            with patch.object(durability, "_git", return_value=str(git).encode()), \
                    patch.object(durability, "_tree_file", return_value=b"{}"):
                self.assertIsNone(durability._stored_service_evidence(repo, runtime, path, proof, META, STATE))
            fake = json.loads(json.dumps(proof))
            fake["remote_run_completion"]["state_commit"] = "9" * 40
            self.assertIsNone(durability._stored_service_evidence(repo, runtime, path, fake, META, STATE))

    def test_no_token_or_remote_proof_is_not_success(self):
        with patch.dict("os.environ", {}, clear=True):
            self.assertIsNone(durability._remote_completion(META, STATE))

    def test_complete_tree_closure_unwraps_first_and_prefers_frozen_runtime_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary); runtime = repo / "runtime"; runtime.mkdir()
            objects = {}
            def obj(name, value):
                raw = json.dumps(value).encode(); objects[name] = raw
                return {"path": name, "sha256": durability._sha(raw)}
            source = obj("advanced_snapshots/inputs/source.json", {"original": "bytes"})
            payload = obj("advanced_snapshots/payloads/payload.json", {"source": {
                "path": "/old/runner/runtime/advanced_snapshots/inputs/source.json",
                "runtime_relative_path": source["path"], "sha256": source["sha256"]}})
            receipt = obj("advanced_snapshots/receipts/receipt.json", {
                "storage_completed_at": "2026-10-13T00:50:00Z", "payload_ref": payload})
            anchor = obj("advanced_snapshots/protocol/captures/serie_a/1/EARLY/first.json", receipt)
            def git(repo, *args):
                if args[:2] == ("cat-file", "-e"):
                    return b""
                if args[:3] == ("show", "-s", "--format=%cI"):
                    return b"2026-10-13T00:55:00+00:00\n"
                name = args[1].split(":", 1)[1]
                if name == "state_metadata.json": return json.dumps(META).encode()
                if name == "state_storage.json": return b'{"schema_version":1,"entries":[]}'
                return objects[name.removeprefix("runtime/")]
            completion = self.remote(b"MODEL_STATE_SAVED_COMMIT=" + STATE.encode())
            with patch.object(durability, "_git", side_effect=git), \
                    patch.object(durability, "_remote_completion", return_value=completion) as remote:
                result = durability.prove(repo, runtime, STATE, anchor)
                self.assertTrue(result["verified"])
                self.assertEqual(result["verified_file_count"], 4)
                path = durability._proof_path(runtime, anchor); path.parent.mkdir(parents=True)
                path.write_text(json.dumps(result))
                self.assertEqual(durability.prove(repo, runtime, STATE, anchor), result)
                self.assertEqual(remote.call_count, 1)
            path.unlink()
            durability._VERIFIED_REMOTE_CACHE.clear()
            with patch.object(durability, "_git", side_effect=git), \
                    patch.object(durability, "_remote_completion", return_value=None):
                result = durability.prove(repo, runtime, STATE, anchor)
                self.assertTrue(result["closure_verified"])
                self.assertFalse(result["verified"])
                self.assertIsNone(result["durable_saved_at"])


if __name__ == "__main__":
    unittest.main()
