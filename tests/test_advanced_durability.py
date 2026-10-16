"""Remote service timestamps must bind the exact state commit, not copied metadata."""
import io
from contextlib import redirect_stdout
import gzip
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


def closure_objects(*, source_name="sources/input.json", source_raw=None,
                    source_hash=None, source_extra=None, source_alias=None):
    objects = {}
    def obj(name, value):
        raw = json.dumps(value).encode()
        objects[name] = raw
        return {"path": name, "sha256": durability._sha(raw)}
    source = obj(source_name, {"original": "bytes"})
    if source_raw is not None:
        objects[source_name] = source_raw
        source["sha256"] = durability._sha(source_raw)
    if source_hash is not None:
        source["sha256"] = source_hash
    source.update(source_extra or {})
    payload = obj("advanced_snapshots/payloads/payload.json", {
        "nested": {"sources": [source] + ([source_alias] if source_alias is not None else [])}})
    receipt = obj("advanced_snapshots/receipts/receipt.json", {
        "storage_completed_at": "2026-10-13T00:50:00Z", "payload_ref": payload})
    anchor = obj("advanced_snapshots/protocol/captures/serie_a/1/EARLY/first.json", receipt)
    return objects, anchor, source


def replay_reference():
    from football_model.production_replay import SCHEMA, _canonical
    payload = {"schema": SCHEMA, "events": [{"op": "clock", "value": "2026-10-13T00:00:00Z"}],
        "initial_state": {"models/serie_a.json": {"content": "{}", "sha256": durability._sha(b"{}")}}}
    digest = durability._sha(_canonical(payload))
    raw = gzip.compress(_canonical({"sha256": digest, "payload": payload}) + b"\n", mtime=0)
    ref = {"path": "replay/bundles/" + digest + ".json.gz", "sha256": digest,
        "status": "recorded", "events": 1, "initial_state_files": 1}
    return raw, ref


def prove_objects(objects, anchor):
    def git(repo, *args):
        if args[:2] == ("cat-file", "-e"):
            return b""
        if args[:3] == ("show", "-s", "--format=%cI"):
            return b"2026-10-13T00:55:00+00:00\n"
        name = args[1].split(":", 1)[1]
        if name == "state_metadata.json": return json.dumps(META).encode()
        if name == "state_storage.json": return b'{"schema_version":1,"entries":[]}'
        return objects[name.removeprefix("runtime/")]
    with tempfile.TemporaryDirectory() as temporary:
        repo = Path(temporary)
        runtime = repo / "runtime"
        runtime.mkdir()
        with patch.object(durability, "_git", side_effect=git), \
                patch.object(durability, "_remote_completion", return_value=None):
            return durability.prove(repo, runtime, STATE, anchor,
                _read_file=lambda name: durability._tree_file(repo, STATE, name, {}))


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

    def test_hash_mismatch_still_rejected_with_fixed_tree_path_and_reference_chain(self):
        objects, anchor, source = closure_objects()
        original = objects[source["path"]]
        objects[source["path"]] = b'{"changed":"bytes"}'
        with self.assertRaisesRegex(durability.DurabilityReferenceError, "durable_source_hash_mismatch") as rejected:
            prove_objects(objects, anchor)
        detail = rejected.exception.details
        self.assertEqual(detail["state_commit"], STATE)
        self.assertEqual(detail["path"], source["path"])
        self.assertEqual(detail["expected_sha256"], durability._sha(original))
        self.assertEqual(detail["actual_sha256"], durability._sha(objects[source["path"]]))
        self.assertEqual([edge["json_pointer"] for edge in detail["reference_chain"]],
            ["", "/payload_ref", "/nested/sources/0"])
        self.assertEqual(detail["reference_chain"][-1]["source_path"],
            "advanced_snapshots/payloads/payload.json")

    def test_same_original_bytes_continue_to_pass_strict_closure(self):
        objects, anchor, _ = closure_objects()
        result = prove_objects(objects, anchor)
        self.assertTrue(result["closure_verified"])
        self.assertFalse(result["verified"])
        self.assertEqual(result["verified_file_count"], 4)

    def test_cli_hash_failure_emits_coordinates_and_returns_failure(self):
        error = durability.DurabilityReferenceError("durable_source_hash_mismatch",
            state_commit=STATE, path="sources/input.json", expected_sha256="c" * 64,
            actual_sha256="d" * 64, reference_chain=[])
        stream = io.StringIO()
        with patch("sys.argv", ["durability", "--state-commit", STATE]), \
                patch.object(durability, "stamp", side_effect=error), redirect_stdout(stream):
            self.assertEqual(durability.main(), 1)
        output = json.loads(stream.getvalue())
        self.assertEqual(output["status"], "failed_verification_attempt")
        self.assertEqual(output["error"], "durable_source_hash_mismatch")
        self.assertEqual(output["failure"], error.details)

    def test_registered_replay_payload_digest_preserves_exact_gzip_byte_binding(self):
        raw, reference = replay_reference()
        self.assertNotEqual(durability._sha(raw), reference["sha256"])
        objects, anchor, _ = closure_objects(source_name=reference["path"], source_raw=raw,
            source_hash=reference["sha256"], source_extra=reference)
        result = prove_objects(objects, anchor)
        self.assertEqual(result["schema_version"], "advanced-durability-v4")
        self.assertEqual(result["raw_sha256"][reference["path"]], durability._sha(raw))
        self.assertEqual(result["reference_hash_semantics"][reference["path"]],
            ["production_replay_canonical_payload_sha256"])
        self.assertTrue(result["closure_verified"])

    def test_replay_raw_and_logical_references_can_bind_the_same_immutable_file(self):
        raw, reference = replay_reference()
        raw_ref = {"path": reference["path"], "sha256": durability._sha(raw)}
        objects, anchor, _ = closure_objects(source_name=reference["path"], source_raw=raw,
            source_hash=reference["sha256"], source_extra=reference, source_alias=raw_ref)
        result = prove_objects(objects, anchor)
        self.assertEqual(result["reference_hash_semantics"][reference["path"]],
            ["production_replay_canonical_payload_sha256", "raw_bytes_sha256"])

    def test_registered_replay_rejects_payload_tampering_and_metadata_changes(self):
        raw, reference = replay_reference()
        value = json.loads(gzip.decompress(raw))
        value["payload"]["events"].append({"op": "invented"})
        changed = gzip.compress(json.dumps(value).encode(), mtime=0)
        for content, ref, error in ((changed, reference, "durable_replay_payload_hash_mismatch"),
                (raw, {**reference, "events": 2}, "durable_replay_reference_metadata_mismatch")):
            with self.subTest(error=error):
                objects, anchor, _ = closure_objects(source_name=reference["path"], source_raw=content,
                    source_hash=reference["sha256"], source_extra=ref)
                with self.assertRaisesRegex(ValueError, error):
                    prove_objects(objects, anchor)

    def test_gzip_name_or_digest_alone_does_not_waive_byte_hash_check(self):
        raw, reference = replay_reference()
        for name, extra in (("unrelated/" + Path(reference["path"]).name, reference),
                            (reference["path"], {})):
            with self.subTest(name=name, status=extra.get("status")):
                # Keep the actual requested logical path when adding metadata.
                extra = {k: v for k, v in extra.items() if k != "path"}
                objects, anchor, _ = closure_objects(source_name=name, source_raw=raw,
                    source_hash=reference["sha256"], source_extra=extra)
                with self.assertRaisesRegex(durability.DurabilityReferenceError, "durable_source_hash_mismatch"):
                    prove_objects(objects, anchor)

    def test_registered_replay_rejects_multiple_gzip_members_or_trailing_data(self):
        raw, reference = replay_reference()
        for changed in (raw + gzip.compress(b"{}"), raw + b"trailing"):
            objects, anchor, _ = closure_objects(source_name=reference["path"], source_raw=changed,
                source_hash=reference["sha256"], source_extra=reference)
            with self.assertRaises(ValueError):
                prove_objects(objects, anchor)

    def test_duplicate_replay_reference_does_not_skip_metadata_validation(self):
        raw, reference = replay_reference()
        objects, anchor, _ = closure_objects(source_name=reference["path"], source_raw=raw,
            source_hash=reference["sha256"], source_extra=reference,
            source_alias={**reference, "events": 99})
        with self.assertRaisesRegex(ValueError, "durable_replay_reference_metadata_mismatch"):
            prove_objects(objects, anchor)

    def test_old_verified_method_is_not_silently_skipped_and_is_preserved(self):
        with tempfile.TemporaryDirectory() as temporary:
            runtime = Path(temporary) / "runtime"
            anchor = runtime / "advanced_snapshots/p/captures/serie_a/1/EARLY/first.json"
            anchor.parent.mkdir(parents=True)
            anchor.write_text("{}")
            reference = {"path": anchor.relative_to(runtime).as_posix(), "sha256": durability._sha(anchor.read_bytes())}
            out = durability._proof_path(runtime, reference)
            out.parent.mkdir(parents=True)
            old = {"verified": True, "method": "git_tree_closure_and_fixed_attempt_job_log_exact_saved_commit_v2"}
            out.write_text(json.dumps(old))
            with patch.object(durability, "prove", return_value={"verified": True, "method": durability.METHOD}) as proved, \
                    patch("football_model.history_archive.FixedStateReader"):
                durability.stamp(temporary, runtime, STATE)
            proved.assert_called_once()
            previous = out.with_name(out.stem + "-previous-" + durability._sha(json.dumps(old).encode()) + ".json")
            self.assertEqual(json.loads(previous.read_text()), old)
            self.assertEqual(json.loads(out.read_text())["method"], durability.METHOD)

    def test_fixed_cold_original_is_read_without_treating_unexpanded_as_missing(self):
        calls = []
        def git(repo, *args):
            calls.append(args)
            if args[1].endswith(":runtime/history_archive/catalog.json"):
                return b'{"schema_version":"fixed-git-history-catalog-v1","entries":[]}'
            raise ValueError("durability_git_object_unavailable")
        with patch.object(durability, "_git", side_effect=git), \
                patch("football_model.history_archive.read_state_bytes", return_value=b'{"original":1}') as cold:
            self.assertEqual(durability._tree_file("repo", STATE, "advanced_models/objects/input.json", {}),
                b'{"original":1}')
        cold.assert_called_once_with("repo", STATE, "advanced_models/objects/input.json")
        self.assertEqual(len(calls), 2)

    def test_corrupt_legacy_archive_never_falls_back_to_cold_storage(self):
        entry = {"blob_sha256": "a" * 64, "compressed_bytes": 1,
            "raw_sha256": "b" * 64, "raw_bytes": 1}
        with patch.object(durability, "_git", return_value=b"broken"), \
                patch("football_model.history_archive.read_state_bytes") as cold:
            with self.assertRaisesRegex(ValueError, "archive_hash_mismatch"):
                durability._tree_file("repo", STATE, "source.json", {"source.json": entry})
        cold.assert_not_called()

    def test_diagnostic_original_is_verified_and_never_ignored_by_field_name(self):
        diagnostic = b'{"raw":"legitimate diagnostic original"}'
        diagnostic_ref = {"path": "diagnostic/frozen.json", "sha256": durability._sha(diagnostic)}
        parent = json.dumps({"diagnostic_evidence": [diagnostic_ref]}).encode()
        objects, anchor, _ = closure_objects(source_raw=parent)
        objects[diagnostic_ref["path"]] = diagnostic
        good = prove_objects(objects, anchor)
        self.assertTrue(good["closure_verified"])
        self.assertEqual(good["verified_file_count"], 5)
        objects[diagnostic_ref["path"]] = b'{"raw":"damaged"}'
        with self.assertRaisesRegex(durability.DurabilityReferenceError, "durable_source_hash_mismatch"):
            prove_objects(objects, anchor)

    def test_mutable_latest_does_not_replace_an_explicit_frozen_source(self):
        frozen = b'{"coefficients":[0.1,0.2]}'
        digest = durability._sha(frozen)
        name = "advanced_snapshots/protocol/sources/" + digest + ".json"
        objects, anchor, _ = closure_objects(source_name=name, source_raw=frozen,
            source_extra={"origin_runtime_path": "advanced_models/latest.json", "kind": "advanced_input"})
        objects["advanced_models/latest.json"] = b'{"coefficients":[99.0]}'
        first = prove_objects(objects, anchor)
        objects["advanced_models/latest.json"] = b'{"coefficients":[-99.0]}'
        self.assertEqual(prove_objects(objects, anchor), first)
        objects[name] = b'{"coefficients":[99.0]}'
        with self.assertRaisesRegex(durability.DurabilityReferenceError, "durable_source_hash_mismatch"):
            prove_objects(objects, anchor)

    def test_shared_reference_validator_keeps_replay_raw_and_logical_hashes_separate(self):
        raw, reference = replay_reference()
        result = durability.verify_reference_bytes(reference["path"], reference, raw)
        self.assertEqual(result["reference_sha256"], reference["sha256"])
        self.assertEqual(result["raw_sha256"], durability._sha(raw))
        with self.assertRaisesRegex(ValueError, "durable_source_hash_mismatch"):
            durability.verify_reference_bytes("unregistered.gz", {"sha256": reference["sha256"]}, raw)

    def test_stamp_shares_one_fixed_reader_and_one_manifest_validation(self):
        with tempfile.TemporaryDirectory() as temporary:
            runtime = Path(temporary) / "runtime"
            for match in (1, 2):
                anchor = runtime / ("advanced_snapshots/p/captures/serie_a/" + str(match) + "/EARLY/first.json")
                anchor.parent.mkdir(parents=True)
                anchor.write_text(json.dumps({"match": match}))
            with patch("football_model.history_archive.FixedStateReader") as factory, \
                    patch.object(durability, "prove", return_value={"verified": False, "method": durability.METHOD}) as prove:
                reports = durability.stamp(temporary, runtime, STATE)
            factory.assert_called_once_with(Path(temporary), STATE)
            reader = factory.return_value.__enter__.return_value
            reader.validate_manifest.assert_called_once_with()
            self.assertEqual(len(reports), 2)
            self.assertEqual(prove.call_count, 2)
            self.assertTrue(all(call.kwargs["_read_file"] is reader.read for call in prove.call_args_list))

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
                result = durability.prove(repo, runtime, STATE, anchor,
                    _read_file=lambda name: durability._tree_file(repo, STATE, name, {}))
                self.assertTrue(result["verified"])
                self.assertEqual(result["verified_file_count"], 4)
                path = durability._proof_path(runtime, anchor); path.parent.mkdir(parents=True)
                path.write_text(json.dumps(result))
                self.assertEqual(durability.prove(repo, runtime, STATE, anchor,
                    _read_file=lambda name: durability._tree_file(repo, STATE, name, {})), result)
                self.assertEqual(remote.call_count, 1)
            path.unlink()
            durability._VERIFIED_REMOTE_CACHE.clear()
            with patch.object(durability, "_git", side_effect=git), \
                    patch.object(durability, "_remote_completion", return_value=None):
                result = durability.prove(repo, runtime, STATE, anchor,
                    _read_file=lambda name: durability._tree_file(repo, STATE, name, {}))
                self.assertTrue(result["closure_verified"])
                self.assertFalse(result["verified"])
                self.assertIsNone(result["durable_saved_at"])


if __name__ == "__main__":
    unittest.main()
