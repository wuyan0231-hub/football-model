"""Synthetic readiness checks; not actual independent judgment or confirmation."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from football_model.evidence import EvidenceError
from football_model.pretest_protocols import (
    F5_SCHEMA, FRAMEWORK_SCHEMA, check_activation, freeze_preparation,
    instantiate_requests, read_preparation,
)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


class PretestProtocolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.now = datetime.now(timezone.utc)
        self.source_commit = "a" * 40
        (self.root / "right.txt").write_bytes(b"SYNTHETIC complete right source\n")
        (self.root / "left-framework.md").write_bytes("SYNTHETIC 左侧框架全文\r\n".encode())
        (self.root / "left-odds.md").write_bytes(b"SYNTHETIC left reading source\n")
        self.spec = {
            "schema_version": FRAMEWORK_SCHEMA, "status": "prepared_not_active",
            "study_id": "synthetic", "study_name": "synthetic preparation",
            "pilot_counts_as_confirmation": False, "capture_source_paths": ["right.txt"],
            "policies": {"left": {"policy_id": "left", "name": "left"},
                         "right": {"policy_id": "right", "name": "right"}},
            "intervention": {"only_changed_variable": "frozen_analysis_policy_package"},
            "model_control": "same_frozen_model_and_forecast",
            "shadow_grades": {"automatic_formal_action": False},
            "target_task": "synthetic same task",
            "scope": {"competitions": ["synthetic"], "description": "synthetic scope",
                      "sampling_rule": "all actual first attempts"},
            "metrics": {"primary": "synthetic flow", "guardrail": "no inventing"},
            "stopping": {"review": "one frozen review", "rule": "synthetic test only"},
            "activation_rules": {"required_gate_kinds": ["deployed_policy_source_binding",
                "neutral_input_and_model_readback", "independent_context_and_attempt_logging",
                "durable_output_and_restore"]},
        }

    def tearDown(self):
        self.tmp.cleanup()

    def freeze(self, name="frozen", spec=None):
        with patch("football_model.pretest_protocols._now", return_value=self.now - timedelta(seconds=10)):
            value = freeze_preparation(spec or self.spec, self.root / name,
                source_root=self.root, source_commit=self.source_commit,
                left_framework=self.root / "left-framework.md", left_odds=self.root / "left-odds.md")
        return value

    def activation(self, frozen):
        return {"preparation_manifest_sha256": frozen["manifest_sha256"],
                "start_at": (self.now + timedelta(days=1)).isoformat(),
                "deployed_source_commit": self.source_commit,
                "pilot_reclassification": False, "backfilled_samples": 0, "gates": {}}

    def test_full_originals_both_policy_definitions_and_no_actual_analysis(self):
        frozen = self.freeze()
        self.assertFalse(frozen["confirmation_activated"])
        self.assertEqual(frozen["actual_independent_outputs"], 0)
        read = read_preparation(frozen["path"], frozen["manifest_sha256"])
        for side in ("left", "right"):
            policy = read["policies"][side]
            raw = (Path(frozen["path"]) / policy["definition"]["path"]).read_bytes()
            self.assertEqual(sha(raw), policy["definition"]["sha256"])
            entries = json.loads(raw)["originals"]
            for original in entries:
                actual = (Path(frozen["path"]) / original["path"]).read_bytes()
                self.assertEqual(sha(actual), original["sha256"])
            task = json.loads((Path(frozen["path"]) / "tasks" / (side + ".json")).read_bytes())
            self.assertIsNone(task["request_id"])
            self.assertIsNone(task["future_information_exposure"])
        left = json.loads((Path(frozen["path"]) / "policies/left.json").read_bytes())
        raw = (Path(frozen["path"]) / left["originals"][0]["path"]).read_bytes()
        self.assertEqual(raw, (self.root / "left-framework.md").read_bytes())

    def test_same_destination_cannot_overwrite_failed_or_successful_freeze(self):
        self.freeze()
        with self.assertRaises(FileExistsError):
            self.freeze()

    def test_external_anchor_is_required(self):
        frozen = self.freeze()
        with self.assertRaisesRegex(EvidenceError, "external_preparation"):
            read_preparation(frozen["path"], None)

    def test_original_tampering_is_rejected(self):
        frozen = self.freeze()
        read = read_preparation(frozen["path"], frozen["manifest_sha256"])
        (Path(frozen["path"]) / read["sources"][0]["path"]).write_bytes(b"changed")
        with self.assertRaisesRegex(EvidenceError, "preparation_original_changed"):
            read_preparation(frozen["path"], frozen["manifest_sha256"])

    def test_manifest_tampering_is_rejected(self):
        frozen = self.freeze()
        (Path(frozen["path"]) / "manifest.json").write_bytes(b"{}")
        with self.assertRaisesRegex(EvidenceError, "manifest_hash_mismatch"):
            read_preparation(frozen["path"], frozen["manifest_sha256"])

    def test_unlisted_fact_cannot_enter_frozen_execution_packet(self):
        frozen = self.freeze()
        (Path(frozen["path"]) / "future-result.json").write_bytes(b'{"synthetic_future":true}')
        with self.assertRaisesRegex(EvidenceError, "unlisted_or_missing_preparation_file"):
            read_preparation(frozen["path"], frozen["manifest_sha256"])

    def test_prepared_cannot_claim_active_or_pilot_confirmation(self):
        for field, value in (("status", "active"), ("pilot_counts_as_confirmation", True)):
            spec = deepcopy(self.spec)
            spec[field] = value
            with self.subTest(field=field), self.assertRaisesRegex(EvidenceError, "must_not_activate"):
                self.freeze(field, spec)

    def test_shadow_grade_cannot_be_promoted(self):
        spec = deepcopy(self.spec)
        spec["shadow_grades"]["automatic_formal_action"] = True
        with self.assertRaisesRegex(EvidenceError, "shadow_grades_cannot_promote"):
            self.freeze(spec=spec)

    def test_source_traversal_rejected(self):
        spec = deepcopy(self.spec)
        spec["capture_source_paths"] = ["../outside"]
        with self.assertRaisesRegex(EvidenceError, "unsafe_path"):
            self.freeze(spec=spec)

    def test_source_symlink_rejected(self):
        (self.root / "linked.txt").symlink_to(self.root / "right.txt")
        spec = deepcopy(self.spec)
        spec["capture_source_paths"] = ["linked.txt"]
        with self.assertRaisesRegex(EvidenceError, "symlink_forbidden"):
            self.freeze(spec=spec)

    def test_source_commit_is_not_automatically_authenticated(self):
        frozen = self.freeze()
        read = read_preparation(frozen["path"], frozen["manifest_sha256"])
        self.assertIn("not_proof", read["commit_role"])

    def test_wrong_uploaded_policy_version_cannot_be_relabelled(self):
        spec = deepcopy(self.spec)
        spec["policies"]["left"]["expected_original_sha256s"] = {"framework": "0" * 64}
        with self.assertRaisesRegex(EvidenceError, "not_frozen_uploaded_version"):
            self.freeze(spec=spec)

    def rebuild_changed_packet(self, frozen, change):
        root = Path(frozen["path"])
        change(root)
        manifest = json.loads((root / "manifest.json").read_bytes())
        for ref in manifest["files"]:
            raw = (root / ref["path"]).read_bytes()
            ref.update(sha256=sha(raw), bytes=len(raw))
        raw = json.dumps(manifest).encode()
        (root / "manifest.json").write_bytes(raw)
        return sha(raw)

    def test_content_consistent_packet_with_declared_source_missing_rejected(self):
        frozen = self.freeze()
        def change(root):
            preparation = json.loads((root / "preparation.json").read_bytes())
            preparation["sources"] = []
            (root / "preparation.json").write_text(json.dumps(preparation))
        digest = self.rebuild_changed_packet(frozen, change)
        with self.assertRaisesRegex(EvidenceError, "declared_capture_sources_not_complete"):
            read_preparation(frozen["path"], digest)

    def test_content_consistent_right_policy_cannot_omit_declared_originals(self):
        frozen = self.freeze()
        def change(root):
            policy = json.loads((root / "policies/right.json").read_bytes())
            policy["originals"] = []
            raw = json.dumps(policy).encode()
            (root / "policies/right.json").write_bytes(raw)
            preparation = json.loads((root / "preparation.json").read_bytes())
            preparation["policies"]["right"]["definition"]["sha256"] = sha(raw)
            (root / "preparation.json").write_text(json.dumps(preparation))
        digest = self.rebuild_changed_packet(frozen, change)
        with self.assertRaisesRegex(EvidenceError, "right_policy_package_not_complete"):
            read_preparation(frozen["path"], digest)

    def test_empty_actual_requests_not_fabricated(self):
        frozen = self.freeze()
        with self.assertRaisesRegex(EvidenceError, "real_future_request_universe_required"):
            instantiate_requests(frozen["path"], frozen["manifest_sha256"], [], self.root / "trials")

    def request(self, cutoff=None):
        return {"request_id": "synthetic-r1", "competition": "synthetic", "match_id": "synthetic-m1",
                "home_id": "synthetic-h", "away_id": "synthetic-a",
                "kickoff": (self.now + timedelta(days=2)).isoformat(),
                "analysis_cutoff": (cutoff or (self.now + timedelta(days=1))).isoformat(),
                "target_task": self.spec["target_task"]}

    def test_future_requests_register_existing_schema_without_creating_outputs(self):
        frozen = self.freeze()
        result = instantiate_requests(frozen["path"], frozen["manifest_sha256"],
                                      [self.request()], self.root / "trials")
        self.assertEqual(result["trial"]["qualification"], "prospective_preregistered")
        self.assertEqual(result["actual_independent_outputs"], 0)
        self.assertFalse(result["neutral_opportunity_created"])
        self.assertFalse((self.root / "trials/trials").exists())

    def test_registered_full_packet_survives_moving_and_original_removal(self):
        frozen = self.freeze()
        result = instantiate_requests(frozen["path"], frozen["manifest_sha256"],
                                      [self.request()], self.root / "trials")
        packet = result["portable_policy_packet"]
        moved = self.root / "moved_packet"
        shutil.move(packet["path"], moved)
        shutil.rmtree(frozen["path"])
        read = read_preparation(moved, packet["manifest_sha256"])
        for side, policy in read["policies"].items():
            definition = json.loads((moved / policy["definition"]["path"]).read_bytes())
            for original in definition["originals"]:
                self.assertEqual(sha((moved / original["path"]).read_bytes()), original["sha256"])
            self.assertTrue((moved / "tasks" / (side + ".json")).is_file())

    def test_historical_request_or_pilot_cannot_be_preregistered(self):
        frozen = self.freeze()
        with self.assertRaisesRegex(EvidenceError, "cannot_backdate"):
            instantiate_requests(frozen["path"], frozen["manifest_sha256"],
                [self.request(cutoff=self.now - timedelta(days=1))], self.root / "trials")

    def test_common_target_scope_cannot_change_at_registration(self):
        frozen = self.freeze()
        request = self.request()
        request["target_task"] = "different task"
        with self.assertRaisesRegex(EvidenceError, "outside_frozen_comparison_scope"):
            instantiate_requests(frozen["path"], frozen["manifest_sha256"], [request], self.root / "trials")

    def test_current_missing_real_gates_blocks_activation(self):
        frozen = self.freeze()
        result = check_activation(frozen["path"], frozen["manifest_sha256"],
                                  self.activation(frozen), evidence_root=self.root)
        self.assertEqual(result["status"], "blocked")
        self.assertIn("gate_missing_deployed_policy_source_binding", result["blocking_reasons"])
        self.assertFalse(result["pipeline_activated"])

    def test_past_start_and_reclassification_are_explicit_blockers(self):
        frozen = self.freeze()
        request = self.activation(frozen)
        request.update(start_at=(self.now - timedelta(days=1)).isoformat(),
                       pilot_reclassification=True, backfilled_samples=185)
        result = check_activation(frozen["path"], frozen["manifest_sha256"], request, evidence_root=self.root)
        self.assertIn("pilot_or_historical_reclassification_forbidden", result["blocking_reasons"])
        self.assertIn("future_exact_start_after_real_activation_required", result["blocking_reasons"])

    def add_gate(self, request, kind, **changes):
        original_path = kind + "-original.json"
        original_raw = b'{"synthetic":true,"not_real_football_evidence":true}'
        (self.root / original_path).write_bytes(original_raw)
        gate = {"schema_version": "s2-pretest-activation-gate-review-1.0", "gate_kind": kind,
                "status": "verified", "reviewer": "SYNTHETIC unittest", "source_commit": self.source_commit,
                "preparation_manifest_sha256": request["preparation_manifest_sha256"],
                "reviewed_at": (self.now - timedelta(seconds=1)).isoformat(),
                "original_evidence": [{"path": original_path, "sha256": sha(original_raw),
                    "persisted_at": (self.now - timedelta(seconds=2)).isoformat()}]}
        gate.update(changes)
        raw = json.dumps(gate).encode()
        path = kind + "-review.json"
        (self.root / path).write_bytes(raw)
        request["gates"][kind] = {"path": path, "sha256": sha(raw)}

    def test_checked_declarations_never_establish_actual_readiness_or_activation(self):
        frozen = self.freeze()
        request = self.activation(frozen)
        for kind in self.spec["activation_rules"]["required_gate_kinds"]:
            self.add_gate(request, kind)
        result = check_activation(frozen["path"], frozen["manifest_sha256"], request, evidence_root=self.root)
        self.assertEqual(result["status"], "review_declarations_complete_needs_independent_activation_review")
        self.assertFalse(result["pipeline_activated"])
        self.assertEqual(result["confirmation_samples_created"], 0)
        self.assertFalse(result["actual_evidence_semantics_verified"])
        self.assertFalse(result["activation_readiness_established"])

    def test_review_without_actual_original_is_insufficient(self):
        frozen = self.freeze()
        request = self.activation(frozen)
        self.add_gate(request, "deployed_policy_source_binding", original_evidence=[])
        result = check_activation(frozen["path"], frozen["manifest_sha256"], request, evidence_root=self.root)
        self.assertIn("gate_original_evidence_missing_deployed_policy_source_binding", result["blocking_reasons"])

    def test_gate_upstream_tampering_rejected(self):
        frozen = self.freeze()
        request = self.activation(frozen)
        self.add_gate(request, "deployed_policy_source_binding")
        (self.root / "deployed_policy_source_binding-original.json").write_bytes(b"changed")
        with self.assertRaisesRegex(EvidenceError, "gate_original_hash_mismatch"):
            check_activation(frozen["path"], frozen["manifest_sha256"], request, evidence_root=self.root)

    def test_f5_preparation_keeps_real_maturity_capacity_gates_and_count_time_support(self):
        source = Path(__file__).resolve().parents[1] / "docs/research/s2-f5c-confirmation-prepared-v2.json"
        protocol = json.loads(source.read_bytes())
        self.assertEqual(protocol["schema_version"], F5_SCHEMA)
        protocol["capture_source_paths"] = ["right.txt"]
        frozen = self.freeze(spec=protocol)
        request = self.activation(frozen)
        result = check_activation(frozen["path"], frozen["manifest_sha256"], request, evidence_root=self.root)
        self.assertIn("gate_missing_genuine_result_maturity_chain", result["blocking_reasons"])
        self.assertIn("gate_missing_long_term_capacity_and_restore", result["blocking_reasons"])
        self.assertEqual(protocol["stages"]["target_enrolled_unique_baseline_opportunities"], 10000)
        self.assertEqual(protocol["stages"]["minimum_valid_unique_pairs_for_success"], 9000)
        self.assertIn("both", protocol["stages"]["official_check"])
        self.assertFalse(protocol["final_decision"]["automatic_production_promotion"])

    def test_f5_preserved_candidate_selector_development_and_power_boundaries(self):
        source = Path(__file__).resolve().parents[1] / "docs/research"
        v1 = json.loads((source / "s2-f5c-confirmation-prepared-v1.json").read_bytes())
        v2 = json.loads((source / "s2-f5c-confirmation-prepared-v2.json").read_bytes())
        dev = json.loads((source / "s2-f5-total-shrink-protocol-v1.json").read_bytes())
        for field in ("candidate", "selector", "score", "dependence"):
            self.assertEqual(v1[field], v2[field])
        self.assertEqual(v2["development_screen_unchanged"], dev["development_screen"])
        self.assertEqual(v1["stages"]["sample_planning"], v2["stages"]["sample_planning"])
        self.assertEqual(v2["predecessor"]["sha256"], sha((source / "s2-f5c-confirmation-prepared-v1.json").read_bytes()))

    def test_f5_immature_real_result_and_snapshot_only_capacity_cannot_pass(self):
        source = Path(__file__).resolve().parents[1] / "docs/research/s2-f5c-confirmation-prepared-v2.json"
        protocol = json.loads(source.read_bytes())
        protocol["capture_source_paths"] = ["right.txt"]
        frozen = self.freeze(spec=protocol)
        request = self.activation(frozen)
        for kind in protocol["activation_rules"]["required_gate_kinds"]:
            self.add_gate(request, kind)
        result = check_activation(frozen["path"], frozen["manifest_sha256"], request, evidence_root=self.root)
        self.assertIn("real_result_and_48h_maturity_chain_unverified", result["blocking_reasons"])
        self.assertIn("long_term_capacity_or_restore_unverified", result["blocking_reasons"])
        self.assertFalse(result["pipeline_activated"])

    def test_required_gate_list_cannot_be_deleted_reduced_or_duplicated(self):
        source = Path(__file__).resolve().parents[1] / "docs/research/s2-f5c-confirmation-prepared-v2.json"
        f5 = json.loads(source.read_bytes())
        for original in (self.spec, f5):
            for gates in ([], ["deployed_policy_source_binding"],
                          original["activation_rules"]["required_gate_kinds"] * 2):
                with self.subTest(schema=original["schema_version"], gates=gates):
                    spec = deepcopy(original)
                    spec["capture_source_paths"] = ["right.txt"]
                    spec["activation_rules"]["required_gate_kinds"] = gates
                    with self.assertRaisesRegex(EvidenceError, "mandatory_activation_gate_set"):
                        self.freeze(spec=spec)

    def test_f5_boolean_counts_are_not_actual_match_counts(self):
        source = Path(__file__).resolve().parents[1] / "docs/research/s2-f5c-confirmation-prepared-v2.json"
        protocol = json.loads(source.read_bytes())
        protocol["capture_source_paths"] = ["right.txt"]
        frozen = self.freeze(spec=protocol)
        request = self.activation(frozen)
        for kind in protocol["activation_rules"]["required_gate_kinds"]:
            self.add_gate(request, kind, unique_real_scored_pilot_matches=True,
                unique_real_48h_mature_pilot_matches=True, all_grids_recomputed=True,
                result_revision_append_only_verified=True, funded_until_maximum_duration=True,
                restore_test_verified=True, forecast_upper_bound_not_just_current_snapshot=True)
        result = check_activation(frozen["path"], frozen["manifest_sha256"], request, evidence_root=self.root)
        self.assertEqual(result["status"], "blocked")
        self.assertIn("real_result_and_48h_maturity_chain_unverified", result["blocking_reasons"])

    def test_f5_arbitrary_reviewed_bytes_cannot_establish_actual_readiness(self):
        source = Path(__file__).resolve().parents[1] / "docs/research/s2-f5c-confirmation-prepared-v2.json"
        protocol = json.loads(source.read_bytes())
        protocol["capture_source_paths"] = ["right.txt"]
        frozen = self.freeze(spec=protocol)
        request = self.activation(frozen)
        for kind in protocol["activation_rules"]["required_gate_kinds"]:
            self.add_gate(request, kind, unique_real_scored_pilot_matches=1,
                unique_real_48h_mature_pilot_matches=1, all_grids_recomputed=True,
                result_revision_append_only_verified=True, funded_until_maximum_duration=True,
                restore_test_verified=True, forecast_upper_bound_not_just_current_snapshot=True)
        result = check_activation(frozen["path"], frozen["manifest_sha256"], request, evidence_root=self.root)
        self.assertEqual(result["status"], "review_declarations_complete_needs_independent_activation_review")
        self.assertFalse(result["activation_readiness_established"])
        self.assertFalse(result["actual_evidence_semantics_verified"])
        self.assertFalse(result["pipeline_activated"])


if __name__ == "__main__":
    unittest.main()
