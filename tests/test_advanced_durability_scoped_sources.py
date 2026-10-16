"""Immutable packet scopes and exact declared ancestors never repair other edges."""
import io
import json
from pathlib import Path
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch, Mock
from football_model import advanced_durability as d
from tests.test_advanced_durability import STATE, SOURCE, META, ENV, replay_reference

OLD = "c" * 40
ORIGIN = "total_shrink_shadow/reports/latest.json"


def put(objects, name, value):
    raw = value if isinstance(value, bytes) else d._canonical(value)
    objects[name] = raw
    return {"path": name, "sha256": d._sha(raw)}


def baseline(objects, refs, *, marker=True, declared=OLD, routes=None):
    source_refs = []
    for ref in refs:
        if marker:
            raw = objects[ref["path"]]
            target = "advanced_snapshots/p/sources/" + d._sha(raw) + ".json"
            put(objects, target, raw)
            source_refs.append({"kind": "baseline_packet_input", "origin_runtime_path": ref["path"],
                "runtime_relative_path": target, "path": "/old/runner/" + target,
                "sha256": d._sha(raw)})
    payload = {"schema_version": "feature-snapshots-1.0", "state_commit": declared,
        "baseline": {"f5c_comparison": {"attempt_evidence": refs}}, "source_refs": source_refs}
    if marker:
        payload["baseline_packet_reference_closure"] = "frozen_raw_inputs_v1"
    if routes is not None:
        payload["source_refs"] = routes
    name = "advanced_snapshots/p/baseline_payloads/" + d._sha(d._canonical(payload)) + ".json"
    return put(objects, name, payload)


def anchor(objects, refs):
    payload = put(objects, "advanced_snapshots/p/payloads/payload.json", {"baseline_refs": refs})
    receipt = put(objects, "advanced_snapshots/p/receipts/receipt.json", {
        "storage_completed_at": "2026-10-13T00:50:00Z", "payload_ref": payload})
    return put(objects, "advanced_snapshots/p/captures/league/1/EARLY/first.json", receipt)


def prove(objects, ref, *, resolver=None, completion=None):
    def git(repo, *args):
        if args[:2] == ("cat-file", "-e"): return b""
        if args[:3] == ("show", "-s", "--format=%cI"): return b"2026-10-13T00:55:00+00:00"
        if args == ("show", STATE + ":state_metadata.json"): return json.dumps(META).encode()
        raise AssertionError(args)
    with tempfile.TemporaryDirectory() as tmp, patch.object(d, "_git", side_effect=git), \
            patch.object(d, "_remote_completion", return_value=completion):
        return d.prove(tmp, Path(tmp) / "runtime", STATE, ref, _read_file=objects.__getitem__,
            _completion_cache={}, _legacy_resolver=resolver)


class ScopedSourceTests(unittest.TestCase):
    def setUp(self):
        d._ANCESTRY_CACHE.clear()

    def test_mutable_nested_edges_use_only_exact_frozen_scope_and_keep_packet_bytes(self):
        objects = {}
        child = put(objects, "shadow/reports/latest.json", {"original": 1})
        parent = put(objects, ORIGIN, {"nested": child})
        base = baseline(objects, [parent, child])
        original_packet = objects[base["path"]]
        first = anchor(objects, [base])
        objects[ORIGIN] = b'{"new":99}'
        objects[child["path"]] = b'{"new":100}'
        result = prove(objects, first)
        self.assertTrue(result["closure_verified"])
        self.assertEqual(objects[base["path"]], original_packet)
        self.assertTrue(all(x["resolution"] == "scoped_frozen_raw_input" for x in result["resolved_reference_bindings"]))
        origins = {x["origin_runtime_path"] for x in result["resolved_reference_bindings"]}
        self.assertEqual(origins, {ORIGIN, child["path"]})
        self.assertEqual(prove(objects, first)["closure_sha256"], result["closure_sha256"])

    def test_two_baselines_can_freeze_different_versions_of_same_origin(self):
        objects = {}
        refs = []
        for version in (1, 2):
            original = put(objects, ORIGIN, {"version": version})
            refs.append(baseline(objects, [original]))
        first = anchor(objects, refs)
        objects[ORIGIN] = b'{"version":3}'
        result = prove(objects, first)
        bindings = result["resolved_reference_bindings"]
        self.assertEqual(len({x["baseline_payload_sha256"] for x in bindings}), 2)
        self.assertEqual(len({x["expected_sha256"] for x in bindings}), 2)

    def test_frozen_route_does_not_leak_into_sibling_without_marker(self):
        objects = {}
        original = put(objects, ORIGIN, {"old": 1})
        frozen = baseline(objects, [original])
        legacy = baseline(objects, [original], marker=False)
        first = anchor(objects, [frozen, legacy])
        objects[ORIGIN] = b'{"changed":true}'
        resolver = Mock(); resolver.resolve.side_effect = ValueError("ancestry_unverified")
        with self.assertRaises(d.DurabilityReferenceError):
            prove(objects, first, resolver=resolver)
        resolver.resolve.assert_called_once()

    def test_tampered_frozen_source_is_rejected(self):
        objects = {}
        original = put(objects, ORIGIN, {"old": 1})
        base = baseline(objects, [original]); first = anchor(objects, [base])
        route = json.loads(objects[base["path"]])["source_refs"][0]
        objects[route["runtime_relative_path"]] = b'{"tampered":true}'
        with self.assertRaisesRegex(ValueError, "durable_source_hash_mismatch"):
            prove(objects, first)

    def test_route_target_and_origin_path_traversal_are_rejected(self):
        for field, changed in (("runtime_relative_path", "../escape.json"),
                ("origin_runtime_path", "../escape.json"),
                ("runtime_relative_path", "other/sources/" + "d" * 64 + ".json")):
            with self.subTest(field=field, changed=changed):
                objects = {}; original = put(objects, ORIGIN, {"old": 1})
                firstbase = baseline(objects, [original])
                routes = json.loads(objects[firstbase["path"]])["source_refs"]
                routes[0][field] = changed
                base = baseline(objects, [original], routes=routes)
                with self.assertRaisesRegex(ValueError, "unsafe"):
                    prove(objects, anchor(objects, [base]))

    def test_wrong_route_hash_does_not_replace_expected_edge(self):
        objects = {}; original = put(objects, ORIGIN, {"old": 1})
        other = put(objects, "other.json", {"other": 2})
        target = "advanced_snapshots/p/sources/" + other["sha256"] + ".json"
        objects[target] = objects["other.json"]
        base = baseline(objects, [original], routes=[{"kind": "baseline_packet_input",
            "origin_runtime_path": ORIGIN, "runtime_relative_path": target, "sha256": other["sha256"]}])
        objects[ORIGIN] = b'{"changed":true}'
        with self.assertRaises(d.DurabilityReferenceError):
            prove(objects, anchor(objects, [base]))

    def test_typed_replay_logical_edge_and_raw_route_remain_distinct(self):
        objects = {}; raw, replay = replay_reference(); objects[replay["path"]] = raw
        base = baseline(objects, [replay], routes=[{"kind": "baseline_packet_input",
            "origin_runtime_path": replay["path"], "runtime_relative_path": replay["path"],
            "sha256": d._sha(raw)}])
        result = prove(objects, anchor(objects, [base]))
        self.assertEqual(result["raw_sha256"][replay["path"]], d._sha(raw))
        self.assertEqual(result["reference_hash_semantics"][replay["path"]],
            ["production_replay_canonical_payload_sha256", "raw_bytes_sha256"])

    def test_exact_declared_original_is_distinct_from_current_bytes_and_not_backdated(self):
        objects = {}; original = put(objects, ORIGIN, {"old": 1}); raw = objects[ORIGIN]
        base = baseline(objects, [original], marker=False); first = anchor(objects, [base])
        objects[ORIGIN] = b'{"changed":true}'
        binding = {"source_commit": OLD, "source_path": ORIGIN, "raw_sha256": d._sha(raw),
            "transport_git_blob": "d" * 40, "source_manifest_sha256": "e" * 64,
            "ancestry": {"method": "github", "source_commit": OLD, "current_commit": STATE,
                "compare_response_sha256": "1" * 64}}
        resolver = Mock(); resolver.resolve.return_value = (raw, binding)
        completion = {"observed_completion_upper_bound": "2026-10-16T02:43:00Z"}
        result = prove(objects, first, resolver=resolver, completion=completion)
        self.assertEqual(result["durable_saved_at"], "2026-10-16T02:43:00Z")
        self.assertEqual(result["historical_resolution_count"], 1)
        actual = result["resolved_reference_bindings"][0]
        self.assertEqual(actual["resolved_state_commit"], OLD)
        self.assertEqual(actual["baseline_payload_sha256"], base["sha256"])
        self.assertEqual(actual["reference_chain"][-1]["json_pointer"], "/baseline/f5c_comparison/attempt_evidence/0")
        resolver.resolve.return_value = (raw, {**binding, "ancestry": {**binding["ancestry"], "compare_response_sha256": "2" * 64}})
        self.assertEqual(prove(objects, first, resolver=resolver, completion=completion)["closure_sha256"], result["closure_sha256"])

    def test_legacy_resolver_never_accepts_wrong_bytes_or_immutable_repair(self):
        resolver = d._LegacyResolver("repo", STATE)
        scope = {"frozen": False, "declared_state": OLD}
        with self.assertRaisesRegex(ValueError, "no_registered"):
            resolver.resolve(scope, "advanced_models/objects/" + "a" * 64 + ".json", {"sha256": "b" * 64})
        with patch.object(d, "_declared_ancestor", return_value={}), \
                patch("football_model.history_archive.FixedStateReader") as factory:
            factory.return_value.read.return_value = b'{"wrong":true}'
            with self.assertRaisesRegex(ValueError, "durable_source_hash_mismatch"):
                resolver.resolve(scope, ORIGIN, {"sha256": "b" * 64})
        resolver.close()

    def test_compare_uses_bounded_metadata_page_and_exact_git_head(self):
        compare = {"status": "ahead", "base_commit": {"sha": OLD},
            "merge_base_commit": {"sha": OLD}, "behind_by": 0, "ahead_by": 2, "total_commits": 2}
        def git(repo, *args):
            if args[0] == "merge-base": raise ValueError("shallow")
            return b"e" * 40
        with patch.dict("os.environ", ENV), patch.object(d, "_git", side_effect=git), \
                patch.object(d, "_api_json", side_effect=[(compare, "1" * 64), ({"sha": STATE}, "2" * 64)]) as api:
            proof = d._declared_ancestor("repo", OLD, STATE)
            self.assertIn("?per_page=1&page=2", api.call_args_list[0].args[0])
            self.assertTrue(api.call_args_list[1].args[0].endswith("/git/commits/" + STATE))
            self.assertEqual(proof["source_commit"], OLD)
        for changed in ({**compare, "behind_by": 1}, {**compare, "merge_base_commit": {"sha": STATE}}):
            d._ANCESTRY_CACHE.clear()
            with patch.dict("os.environ", ENV), patch.object(d, "_git", side_effect=git), \
                    patch.object(d, "_api_json", return_value=(changed, "1" * 64)):
                with self.assertRaisesRegex(ValueError, "ancestry_unverified"):
                    d._declared_ancestor("repo", OLD, STATE)
        d._ANCESTRY_CACHE.clear()
        with patch.dict("os.environ", ENV), patch.object(d, "_git", side_effect=git), \
                patch.object(d, "_api_json", side_effect=[(compare, "1" * 64), ({"sha": OLD}, "2" * 64)]):
            with self.assertRaisesRegex(ValueError, "head_unverified"):
                d._declared_ancestor("repo", OLD, STATE)

    def test_local_ancestry_does_not_need_network_or_accept_missing_declaration(self):
        with patch.object(d, "_git", return_value=b""), patch.object(d, "_api_json") as api:
            self.assertEqual(d._declared_ancestor("repo", OLD, STATE)["method"], "local_git_is_ancestor")
            api.assert_not_called()
        with self.assertRaisesRegex(ValueError, "invalid_declared"):
            d._declared_ancestor("repo", None, STATE)

    def test_bad_first_isolated_preserves_prior_proof_and_all_first_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp) / "runtime"; anchors = []
            for league in ("a_bad", "z_good"):
                path = runtime / ("advanced_snapshots/p/captures/" + league + "/1/EARLY/first.json")
                path.parent.mkdir(parents=True); path.write_text(json.dumps({"league": league})); anchors.append(path)
            bad_ref = {"path": anchors[0].relative_to(runtime).as_posix(), "sha256": d._sha(anchors[0].read_bytes())}
            old_out = d._proof_path(runtime, bad_ref); old_out.parent.mkdir(parents=True); old_out.write_text('{"old":"proof"}')
            error = d.DurabilityReferenceError("durable_source_hash_mismatch", state_commit=STATE,
                path=ORIGIN, expected_sha256="1" * 64, actual_sha256="2" * 64, reference_chain=[])
            good = {"verified": True, "closure_verified": True, "method": d.METHOD}
            before = [p.read_bytes() for p in anchors]
            with patch("football_model.history_archive.FixedStateReader"), patch.object(d, "prove", side_effect=[error, good]):
                reports = d.stamp(tmp, runtime, STATE)
            self.assertEqual([p["closure_verified"] for p in reports], [False, True])
            self.assertEqual(old_out.read_text(), '{"old":"proof"}')
            self.assertEqual([p.read_bytes() for p in anchors], before)
            self.assertEqual(len(list((old_out.parent / "failures").glob("*.json"))), 1)
            with patch("sys.argv", ["durability", "--state-commit", STATE]), \
                    patch.object(d, "stamp", return_value=reports), redirect_stdout(io.StringIO()) as output:
                self.assertEqual(d.main(), 1)
            summary = json.loads(output.getvalue())
            self.assertEqual(summary["status"], "partial_verification_attempt")
            self.assertEqual(summary["new_durable_closures"], 1)
            self.assertEqual(summary["failed"], 1)

    def test_existing_current_method_proof_is_reverified(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp) / "runtime"
            path = runtime / "advanced_snapshots/p/captures/league/1/EARLY/first.json"
            path.parent.mkdir(parents=True); path.write_text("{}")
            ref = {"path": path.relative_to(runtime).as_posix(), "sha256": d._sha(path.read_bytes())}
            out = d._proof_path(runtime, ref); out.parent.mkdir(parents=True)
            proof = {"verified": True, "closure_verified": True, "method": d.METHOD}
            out.write_text(json.dumps(proof))
            with patch("football_model.history_archive.FixedStateReader"), patch.object(d, "prove", return_value=proof) as verify:
                self.assertEqual(d.stamp(tmp, runtime, STATE), [proof])
            verify.assert_called_once()


if __name__ == "__main__": unittest.main()

class EarlierProofRetentionTests(unittest.TestCase):
    def test_saved_timely_proof_is_kept_when_later_state_has_late_service_bound(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp) / "runtime"
            path = runtime / "advanced_snapshots/p/captures/league/1/EARLY/first.json"
            path.parent.mkdir(parents=True); path.write_text("{}")
            ref = {"path": path.relative_to(runtime).as_posix(), "sha256": d._sha(path.read_bytes())}
            out = d._proof_path(runtime, ref); out.parent.mkdir(parents=True)
            proof = {"schema_version": "advanced-durability-v4", "method": d.METHOD,
                "verified": True, "closure_verified": True, "receipt_ref": ref,
                "state_commit": OLD, "closure_sha256": "f" * 64,
                "commit_clock_at": "2026-10-13T00:55:00Z", "durable_saved_at": "2026-10-13T01:00:00Z"}
            raw = json.dumps(proof).encode(); out.write_bytes(raw)
            with patch("football_model.history_archive.FixedStateReader") as factory, \
                    patch.object(d, "_declared_ancestor", return_value={}) as ancestor, \
                    patch.object(d, "_stored_service_evidence", return_value={"observed_completion_upper_bound": proof["durable_saved_at"]}), \
                    patch.object(d, "prove") as prove:
                reader = factory.return_value.__enter__.return_value
                reader.read.side_effect = lambda name: path.read_bytes() if name == ref["path"] else raw
                reader.root_bytes.return_value = json.dumps(META).encode()
                reports = d.stamp(tmp, runtime, STATE)
            prove.assert_not_called()
            ancestor.assert_called_once_with(Path(tmp), OLD, STATE)
            self.assertEqual(out.read_bytes(), raw)
            self.assertEqual(reports[0]["durable_saved_at"], "2026-10-13T01:00:00Z")
            self.assertEqual(reports[0]["verification_status"], "retained_fixed_tree_service_verified_proof")
            with patch("sys.argv", ["durability", "--state-commit", STATE]), \
                    patch.object(d, "stamp", return_value=reports), redirect_stdout(io.StringIO()) as output:
                self.assertEqual(d.main(), 0)
            self.assertEqual(json.loads(output.getvalue())["new_durable_closures"], 0)
            self.assertEqual(json.loads(output.getvalue())["retained_saved_closures"], 1)

    def test_local_verified_assertion_cannot_skip_when_not_in_fixed_tree(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp) / "runtime"
            path = runtime / "advanced_snapshots/p/captures/league/1/EARLY/first.json"
            path.parent.mkdir(parents=True); path.write_text("{}")
            ref = {"path": path.relative_to(runtime).as_posix(), "sha256": d._sha(path.read_bytes())}
            out = d._proof_path(runtime, ref); out.parent.mkdir(parents=True)
            fake = {"schema_version": "advanced-durability-v4", "method": d.METHOD,
                "verified": True, "closure_verified": True, "receipt_ref": ref,
                "state_commit": OLD, "closure_sha256": "f" * 64}
            out.write_text(json.dumps(fake))
            with patch("football_model.history_archive.FixedStateReader") as factory, \
                    patch.object(d, "_stored_service_evidence") as stored, \
                    patch.object(d, "prove", return_value={"verified": False, "closure_verified": True}) as prove:
                factory.return_value.__enter__.return_value.read.return_value = b'{}'
                d.stamp(tmp, runtime, STATE)
            prove.assert_called_once()
            stored.assert_not_called()

    def test_current_fixed_first_must_equal_local_first_before_retention(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp) / "runtime"
            path = runtime / "advanced_snapshots/p/captures/league/1/EARLY/first.json"
            path.parent.mkdir(parents=True); path.write_text("{}")
            ref = {"path": path.relative_to(runtime).as_posix(), "sha256": d._sha(path.read_bytes())}
            out = d._proof_path(runtime, ref); out.parent.mkdir(parents=True)
            proof = {"schema_version": "advanced-durability-v4", "method": d.METHOD,
                "verified": True, "closure_verified": True, "receipt_ref": ref,
                "state_commit": OLD, "closure_sha256": "f" * 64}
            raw = json.dumps(proof).encode(); out.write_bytes(raw)
            with patch("football_model.history_archive.FixedStateReader") as factory, \
                    patch.object(d, "_stored_service_evidence") as stored, \
                    patch.object(d, "prove", return_value={"verified": False, "closure_verified": True}) as prove:
                factory.return_value.__enter__.return_value.read.side_effect = lambda name: b'{"current":"changed"}' if name == ref["path"] else raw
                d.stamp(tmp, runtime, STATE)
            prove.assert_called_once()
            stored.assert_not_called()

class ExactProofRecomputationTests(unittest.TestCase):
    def test_independent_api_to_local_and_changed_http_envelope_keep_exact_proof_for_confirmation(self):
        from football_model import advanced_confirmation as confirmation
        objects = {}; original = put(objects, ORIGIN, {"old": 1}); raw = objects[ORIGIN]
        base = baseline(objects, [original], marker=False); first = anchor(objects, [base])
        objects[ORIGIN] = b'{"new":2}'
        facts = {"source_commit": OLD, "source_path": ORIGIN, "raw_sha256": d._sha(raw),
            "raw_bytes": len(raw), "transport_git_blob": "d" * 40, "source_manifest_sha256": "e" * 64}
        def binding(method, response="1" * 64):
            return {**facts, "ancestry": {"method": method, "source_commit": OLD,
                "current_commit": STATE, "compare_response_sha256": response}}
        evidence = {"observed_completion_upper_bound": "2026-10-13T01:00:00Z"}
        evidence["evidence_sha256"] = d._sha(d._canonical(evidence))
        def git(repo, *args):
            if args[:2] == ("cat-file", "-e"): return b""
            if args[:3] == ("show", "-s", "--format=%cI"): return b"2026-10-13T00:55:00+00:00"
            if args == ("show", STATE + ":state_metadata.json"): return json.dumps(META).encode()
            raise AssertionError(args)
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp) / "runtime"; runtime.mkdir()
            resolver = Mock(); resolver.resolve.return_value = (raw, binding("github"))
            with patch.object(d, "_git", side_effect=git), patch.object(d, "_remote_completion", return_value=evidence):
                proof = d.prove(tmp, runtime, STATE, first, _read_file=objects.__getitem__,
                    _legacy_resolver=resolver, _completion_cache={})
                out = d._proof_path(runtime, first); out.parent.mkdir(parents=True); out.write_text(json.dumps(proof))
                for ancestry in (binding("local_git_is_ancestor"), binding("github", "2" * 64)):
                    resolver.resolve.return_value = (raw, ancestry)
                    actual = d.prove(tmp, runtime, STATE, first, _read_file=objects.__getitem__,
                        _legacy_resolver=resolver, _completion_cache={})
                    self.assertEqual(actual, proof)
                # Exercise the real confirmation equality contract, including
                # its original saved-at-before-kickoff check.
                resolver.resolve.return_value = (raw, binding("local_git_is_ancestor"))
                with patch.object(d, "_LegacyResolver", return_value=resolver), \
                        patch("football_model.history_archive.FixedStateReader") as factory, \
                        patch.object(confirmation, "_read_ref", return_value=proof), \
                        patch("subprocess.run", return_value=Mock(returncode=0)):
                    factory.return_value.__enter__.return_value.read.side_effect = objects.__getitem__
                    row = {"durability_ref": {}, "first_capture_anchor_ref": first,
                        "captured_at": "2026-10-13T00:50:00Z", "kickoff": "2026-10-13T02:00:00Z"}
                    self.assertEqual(confirmation.verify_durable_capture(runtime, row,
                        d._time("2026-10-13T03:00:00Z")), proof)
                resolver.resolve.return_value = (raw, {**binding("local"), "source_commit": "f" * 40})
                changed = d.prove(tmp, runtime, STATE, first, _read_file=objects.__getitem__,
                    _legacy_resolver=resolver, _completion_cache={})
                self.assertNotEqual(changed["closure_sha256"], proof["closure_sha256"])
                self.assertNotEqual(changed, proof)
                resolver.resolve.return_value = (b'{"wrong":3}', binding("local"))
                with self.assertRaises(d.DurabilityReferenceError):
                    d.prove(tmp, runtime, STATE, first, _read_file=objects.__getitem__,
                        _legacy_resolver=resolver, _completion_cache={})
