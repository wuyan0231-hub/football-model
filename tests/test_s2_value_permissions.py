"""Permission regressions, synthetic only; no prediction-quality validation."""
import copy
import unittest

from football_model.decision_audit import compare_decision_runs, validate_decision_record
from tests.test_decision_audit import codes, fixture, numeric, qualify_value


def strong(direction="negative", fit="不建议"):
    status = "confirmed_adverse" if direction == "negative" else "confirmed_positive"
    ev = -0.04 if direction == "negative" else 0.04
    record = qualify_value(numeric(fixture(), ev=ev, status=status), direction)
    record["candidates"][0]["fit"] = fit
    for key in ("summary_decision", "body_decision"):
        record["versions"][0][key]["fit"] = fit
    return record


class ValuePermissionTests(unittest.TestCase):
    def test_point_estimates_and_qualitative_fit_are_not_mechanically_denied(self):
        cases = [fixture()]
        for ev, status in ((0.04, "unclear"), (-0.04, "unclear"), (-0.04, "negative")):
            record = numeric(fixture(), ev=ev, status=status)
            record["model_records"][0]["model_decision_role"] = "diagnostic_only"
            cases.append(record)
        for record in cases:
            with self.subTest(value=record["candidates"][0]["value"]):
                result = validate_decision_record(record)
                self.assertEqual(result["status"], "structural_checks_passed")
                self.assertNotIn("adverse_model_fit_conflict", codes(record))

    def test_confirmations_have_symmetric_permissions_and_qualification(self):
        for direction in ("positive", "negative"):
            for field, code in (("model_decision_role", "missing_value_permission"),
                                ("role_declaration_ref", "missing_role_declaration_evidence"),
                                ("model_output_refs", "missing_value_output_evidence"),
                                ("task_qualification", "missing_value_task_qualification")):
                with self.subTest(direction=direction, missing=field):
                    record = strong(direction)
                    del record["model_records"][0][field]
                    self.assertIn(code, codes(record))
                    self.assertNotEqual(validate_decision_record(record)["status"], "structural_checks_passed")

    def test_qualified_confirmations_pass_and_never_claim_semantic_verification(self):
        for direction in ("positive", "negative"):
            record = strong(direction)
            original = copy.deepcopy(record)
            result = validate_decision_record(record)
            self.assertEqual(result["status"], "structural_checks_passed")
            self.assertFalse(result["economic_advantage_verified"])
            self.assertTrue(result["manual_review_required"])
            self.assertEqual(record, original)

    def test_diagnostic_only_cannot_escalate_to_either_confirmation(self):
        for direction in ("positive", "negative"):
            record = strong(direction, fit="适合")
            record["model_records"][0]["model_decision_role"] = "diagnostic_only"
            record["sources"][1]["model_decision_role"] = "diagnostic_only"
            self.assertIn("value_permission_denied", codes(record))
            self.assertNotIn("adverse_model_fit_conflict", codes(record))
        record = numeric(fixture(), ev=-0.04, status="negative")
        record["model_records"][0].update(model_decision_role="diagnostic_only", value_assessment="valid_negative")
        self.assertIn("value_permission_denied", codes(record))
        self.assertNotIn("adverse_model_fit_conflict", codes(record))

    def test_predeclaration_requires_referenced_fingerprint_and_known_order(self):
        for direction in ("positive", "negative"):
            for timestamp in ("2026-10-07T08:01:00+08:00", "2026-10-07T08:02:00+08:00"):
                record = strong(direction)
                record["sources"][1]["observed_at"] = timestamp
                self.assertIn("role_not_declared_before_output", codes(record))
            for field, value in (("sha256", "bad"), ("observed_at", "2026-10-07T08:00:00")):
                record = strong(direction)
                record["sources"][1][field] = value
                self.assertIn("missing_role_declaration_evidence", codes(record))
            record = strong(direction)
            record["sources"][1]["model_settings_sha256"] = "f" * 64
            self.assertIn("role_declaration_mismatch", codes(record))

    def test_first_output_and_real_timezone_order_control_predeclaration(self):
        record = strong()
        record["sources"][2]["observed_at"] = "2026-10-07T00:01:00Z"
        self.assertEqual(validate_decision_record(record)["status"], "structural_checks_passed")
        record["sources"].append({"source_id": "older-output", "url": "https://example.invalid/older",
                                  "sha256": "f" * 64, "observed_at": "2026-10-07T07:59:00+08:00"})
        record["model_records"][0]["model_output_refs"].append("older-output")
        self.assertIn("role_not_declared_before_output", codes(record))

    def test_scope_and_uncertainty_refs_must_resolve_for_both_signs(self):
        for direction in ("positive", "negative"):
            record = strong(direction)
            record["model_records"][0]["task_qualification"]["status"] = "exploratory"
            self.assertIn("unaccepted_value_task", codes(record))
            record = strong(direction)
            record["model_records"][0]["task_qualification"]["quote_id"] = "another-price"
            self.assertIn("value_task_mismatch", codes(record))
            record = strong(direction)
            record["model_records"][0]["task_qualification"]["scope_refs"] = ["missing-scope"]
            self.assertIn("missing_value_task_scope", codes(record))
            for field in ("uncertainty_refs", "validation_refs"):
                record = strong(direction)
                record["model_records"][0][field] = ["missing-evidence"]
                self.assertIn("unresolved_reference", codes(record))
                self.assertIn("positive_without_uncertainty" if direction == "positive" else "adverse_without_uncertainty", codes(record))

    def test_adverse_fit_rule_requires_qualified_sign_bound_to_actual_calculation(self):
        record = strong(fit="适合")
        self.assertIn("adverse_model_fit_conflict", codes(record))
        record["calculation_records"][0]["ev"] = 0.04
        self.assertIn("value_claim_sign_mismatch", codes(record))
        self.assertNotIn("adverse_model_fit_conflict", codes(record))
        record = strong(fit="适合")
        record["calculation_records"][0]["quote_id"] = "other-price"
        self.assertIn("value_claim_calculation_mismatch", codes(record))
        self.assertNotIn("adverse_model_fit_conflict", codes(record))

    def test_negative_claim_cannot_hide_by_omitting_value_object_or_model_id(self):
        for omit in ("value", "model_record_ids"):
            record = strong(fit="适合")
            record["model_records"][0]["value_assessment"] = "valid_negative"
            if omit == "value":
                del record["candidates"][0]["value"]
            else:
                record["candidates"][0]["model_record_ids"] = []
            self.assertIn("hidden_adverse_model", codes(record))
            self.assertIn("adverse_model_fit_conflict", codes(record))

    def test_nonapplicable_or_market_probability_never_acquires_veto(self):
        for field, value in (("applicable", False), ("status", "read_not_recomputed"), ("probability_role", "MKT")):
            record = strong(fit="适合")
            record["model_records"][0].update(value_assessment="valid_negative")
            record["model_records"][0][field] = value
            self.assertIn("unqualified_value_model", codes(record))
            self.assertNotIn("adverse_model_fit_conflict", codes(record))

    def test_malformed_permission_metadata_reports_gaps_without_crashing(self):
        for field, value in (("role_declaration_ref", []), ("model_output_refs", "output-source"),
                             ("task_qualification", []), ("uncertainty_refs", {"s1": True})):
            record = strong()
            record["model_records"][0][field] = value
            self.assertNotEqual(validate_decision_record(record)["status"], "structural_checks_passed")

    def test_failed_calculation_resolution_cannot_acquire_veto_on_second_check(self):
        record = strong(fit="适合")
        record["model_records"][0].update(value_assessment="valid_negative", calculation_record_id="missing-calc")
        result = validate_decision_record(record)
        self.assertIn("unresolved_reference", codes(record))
        self.assertNotIn("adverse_model_fit_conflict", codes(record))
        self.assertEqual(result["status"], "blocked")

    def test_other_candidate_or_version_cannot_lend_qualified_veto(self):
        for field, value in (("candidate_id", "c2"), ("version_id", "r0")):
            record = strong(fit="适合")
            record["model_records"][0][field] = value
            self.assertIn("value_claim_candidate_mismatch", codes(record))
            self.assertNotIn("adverse_model_fit_conflict", codes(record))

    def test_conflicting_use_does_not_hide_a_separately_qualified_adverse_record(self):
        record = strong(fit="适合")
        record["model_records"][0]["value_assessment"] = "valid_negative"
        # A previous invalid cross-candidate use must not poison the local check
        # for the correctly bound candidate when issues are deduplicated.
        wrong = copy.deepcopy(record["candidates"][0])
        wrong["candidate_id"] = "wrong-candidate"
        record["candidates"].insert(0, wrong)
        result = validate_decision_record(record)
        conflicts = [item for item in result["issues"] if item["code"] == "adverse_model_fit_conflict"]
        self.assertEqual([item["path"] for item in conflicts], ["candidates[c1].fit"])

    def test_validator_and_policy_versions_are_recorded_and_repeat_accepts_new_status(self):
        left, right = strong(), strong()
        right["decision_context"]["run_id"] = "run2"
        result = compare_decision_runs([left, right])
        self.assertEqual(result["status"], "comparable")
        self.assertEqual(result["validator_version"], "decision-audit-validator-1.1")
        self.assertEqual(result["value_permission_policy"], "s2-value-permissions-1.0")
        self.assertEqual(result["audits"][0]["validator_version"], result["validator_version"])


if __name__ == "__main__":
    unittest.main()
