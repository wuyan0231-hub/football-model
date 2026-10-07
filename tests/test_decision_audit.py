"""Decision audit enforces declarations; none of these tests establish prediction skill."""
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from football_model.decision_audit import VERSION, compare_decision_runs, validate_decision_record


def fixture():
    decision = {"candidate_id": "c1", "fit": "适合", "contract_id": "ct1", "quote_id": "q1",
                "condition_ids": [], "value_status": "unquantified"}
    return {"schema_version": "match-archive-1.0", "decision_audit_version": VERSION,
            "match_id": "demonstration-only",
            "decision_context": {"evidence_bundle_sha256": "a" * 64, "rules_sha256": "b" * 64,
                                 "model_settings_sha256": "c" * 64, "run_id": "run1", "version_id": "r1"},
            "sources": [{"source_id": "s1", "url": "https://example.invalid/source"}],
            "contracts": [{"contract_id": "ct1", "market": "ah", "selection": "home", "line": -0.25,
                           "settlement_basis": "REGULATION"}],
            "quotes": [{"quote_id": "q1", "contract_id": "ct1", "source_id": "s1", "decimal_odds": 1.95,
                        "status": "open_reference", "market": "ah", "selection": "home", "line": -0.25,
                        "settlement_basis": "REGULATION"}],
            "candidates": [{"candidate_id": "c1", "version_id": "r1", "contract_id": "ct1", "quote_id": "q1",
                            "fit": "适合", "support_refs": ["s1"], "mechanism": "演示机制，待人工审核",
                            "payment_requirement": "演示支付，待人工审核",
                            "counterevidence": [{"source_refs": ["s1"], "handling": "retained", "reason": "演示"}],
                            "condition_ids": [], "model_record_ids": [],
                            "value": {"status": "unquantified", "ev": None, "breakeven_odds": None,
                                      "calculation_record_id": None, "disclosed_model_record_ids": []}}],
            "conditions": [], "model_records": [], "calculation_records": [],
            "versions": [{"version_id": "r1", "primary_candidate_id": "c1",
                          "summary_decision": copy.deepcopy(decision), "body_decision": copy.deepcopy(decision)}]}


def numeric(record, ev=0.02, status="unclear"):
    record["model_records"] = [{"model_record_id": "m1", "candidate_id": "c1", "version_id": "r1",
                                "applicable": True, "status": "recomputed", "probability_role": "F",
                                "uncertainty_assessed": False}]
    record["calculation_records"] = [{"calculation_record_id": "calc1", "model_record_id": "m1",
                                      "contract_id": "ct1", "quote_id": "q1", "ev": ev, "breakeven_odds": 1.9}]
    record["candidates"][0]["model_record_ids"] = ["m1"]
    record["candidates"][0]["value"] = {"status": status, "ev": ev, "breakeven_odds": 1.9,
                                          "calculation_record_id": "calc1", "disclosed_model_record_ids": ["m1"]}
    for key in ("summary_decision", "body_decision"):
        record["versions"][0][key]["value_status"] = status
    return record


def codes(record):
    return {i["code"] for i in validate_decision_record(record)["issues"]}


def qualify_value(record, direction="positive"):
    """Synthetic declaration only; not evidence of real model/task acceptance."""
    record["sources"].extend([
        {"source_id": "role-source", "url": "https://example.invalid/frozen-role",
         "sha256": "d" * 64, "observed_at": "2026-10-07T08:00:00+08:00",
         "model_decision_role": "value_assessment", "model_settings_sha256": "c" * 64},
        {"source_id": "output-source", "url": "https://example.invalid/model-output",
         "sha256": "e" * 64, "observed_at": "2026-10-07T08:01:00+08:00"},
    ])
    record["model_records"][0].update(
        model_decision_role="value_assessment", role_declaration_ref="role-source",
        model_output_refs=["output-source"], calculation_record_id="calc1",
        task_qualification={"status": "accepted", "contract_id": "ct1", "quote_id": "q1", "scope_refs": ["s1"]},
        uncertainty_assessed=True, uncertainty_support=direction,
        uncertainty_refs=["s1"], validation_refs=["s1"],
    )
    return record


class DecisionAuditTests(unittest.TestCase):
    def test_qualitative_suitable_remains_permitted_without_model_or_xg(self):
        record = fixture()
        untouched = copy.deepcopy(record)
        audit = validate_decision_record(record)
        self.assertEqual(audit["status"], "structural_checks_passed")
        self.assertFalse(audit["predictive_quality_verified"])
        self.assertTrue(audit["manual_review_required"])
        self.assertEqual(record, untouched)

    def test_legacy_missing_fields_do_not_rewrite_or_pass(self):
        legacy = {"schema_version": "match-archive-1.0", "candidates": [{"candidate_id": "old", "fit": "不建议"}]}
        original = copy.deepcopy(legacy)
        result = validate_decision_record(legacy)
        self.assertEqual(result["status"], "incomplete")
        self.assertGreater(result["not_evaluated_count"], 0)
        self.assertEqual(legacy, original)

    def test_fit_is_not_supported_by_empty_evidence_or_unresolvable_ids(self):
        r = fixture()
        r["candidates"][0]["support_refs"] = []
        self.assertIn("unsupported_fit", codes(r))
        r["candidates"][0]["support_refs"] = ["nonexistent"]
        self.assertIn("unresolved_reference", codes(r))
        r["sources"].append(copy.deepcopy(r["sources"][0]))
        self.assertIn("duplicate_id", codes(r))

    def test_unknown_labels_and_unhandled_counterevidence_cannot_form_pass(self):
        for key in ("fit", "value"):
            with self.subTest(key=key):
                r = fixture()
                if key == "fit":
                    r["candidates"][0]["fit"] = None
                    for field in ("summary_decision", "body_decision"):
                        r["versions"][0][field]["fit"] = None
                else:
                    r["candidates"][0]["value"]["status"] = None
                    for field in ("summary_decision", "body_decision"):
                        r["versions"][0][field]["value_status"] = None
                self.assertEqual(validate_decision_record(r)["status"], "incomplete")
        r = fixture()
        r["candidates"][0]["counterevidence"][0]["handling"] = None
        self.assertIn("unhandled_counterevidence", codes(r))

    def test_circular_support_and_infinite_price_do_not_pass(self):
        r = fixture()
        r["candidates"][0]["support_refs"] = ["c1"]
        self.assertIn("invalid_support_reference", codes(r))
        r["quotes"][0]["decimal_odds"] = float("inf")
        self.assertIn("invalid_quote_odds", codes(r))

    def test_contract_quote_sign_direction_and_scope_must_match(self):
        for field, wrong in (("selection", "away"), ("line", 0.25), ("settlement_basis", "EXTRA_TIME")):
            with self.subTest(field=field):
                r = fixture()
                r["quotes"][0][field] = wrong
                self.assertIn("contract_mismatch", codes(r))
        r = fixture()
        r["quotes"][0]["contract_id"] = "other-contract"
        self.assertIn("quote_contract_mismatch", codes(r))

    def test_closed_quote_blocks_unconditional_fit(self):
        r = fixture()
        r["quotes"][0]["status"] = "closed"
        self.assertIn("unsuitable_quote", codes(r))

    def test_unknown_quote_status_and_unstructured_counterevidence_block(self):
        r = fixture()
        r["quotes"][0]["status"] = "banana"
        self.assertIn("invalid_quote_status", codes(r))
        for invalid in ("已处理", None, {"handling": "retained"}):
            with self.subTest(invalid=invalid):
                r = fixture()
                r["candidates"][0]["counterevidence"] = invalid
                self.assertIn("invalid_counterevidence", codes(r))

    def test_null_primary_contract_never_passes_even_when_not_recommended(self):
        r = fixture()
        r["candidates"][0].update(contract_id=None, quote_id=None, fit="不建议")
        for field in ("summary_decision", "body_decision"):
            r["versions"][0][field].update(contract_id=None, quote_id=None, fit="不建议")
        self.assertEqual(validate_decision_record(r)["status"], "incomplete")
        self.assertIn("unknown_candidate_binding", codes(r))

    def test_only_explicit_critical_unknown_prevents_unconditional_fit(self):
        r = fixture()
        r["conditions"] = [{"condition_id": "cond1", "state": "unknown", "critical": False,
                             "observable": "演示非关键普通首发未知", "actions": {}}]
        r["candidates"][0]["condition_ids"] = ["cond1"]
        for k in ("summary_decision", "body_decision"):
            r["versions"][0][k]["condition_ids"] = ["cond1"]
        self.assertEqual(validate_decision_record(r)["status"], "structural_checks_passed")
        r["conditions"][0]["critical"] = True
        self.assertIn("unresolved_critical_condition", codes(r))

    def test_conditional_fit_requires_observable_branch_actions(self):
        r = fixture()
        r["candidates"][0]["fit"] = "条件适合"
        self.assertIn("missing_critical_condition", codes(r))
        r["conditions"] = [{"condition_id": "cond1", "state": "unknown", "critical": True,
                             "observable": "明确可核条件", "actions": {"satisfied": "更新", "not_satisfied": "不建议", "unknown": "保持条件"}}]
        r["candidates"][0]["condition_ids"] = ["cond1"]
        for k in ("summary_decision", "body_decision"):
            r["versions"][0][k].update(fit="条件适合", condition_ids=["cond1"])
        self.assertEqual(validate_decision_record(r)["status"], "structural_checks_passed")
        del r["conditions"][0]["actions"]["unknown"]
        self.assertIn("unactionable_condition", codes(r))

    def test_unknown_is_not_zero_and_ev_requires_applicable_model(self):
        r = fixture()
        r["candidates"][0]["value"]["ev"] = 0
        self.assertIn("unquantified_is_not_zero", codes(r))
        self.assertIn("numeric_without_model", codes(r))
        r = numeric(fixture())
        self.assertEqual(validate_decision_record(r)["status"], "structural_checks_passed")
        r["model_records"][0]["applicable"] = False
        self.assertIn("inapplicable_model_numeric", codes(r))
        r["model_records"][0]["status"] = "read_not_recomputed"
        self.assertIn("inapplicable_model_numeric", codes(r))

    def test_reported_numbers_are_bound_to_actual_calculation_record(self):
        r = numeric(fixture())
        r["calculation_records"][0]["breakeven_odds"] = 1.7
        self.assertIn("calculation_value_mismatch", codes(r))
        r["calculation_records"][0]["quote_id"] = "different-price"
        self.assertIn("calculation_contract_mismatch", codes(r))
        r["calculation_records"] = []
        self.assertIn("unresolved_reference", codes(r))

    def test_positive_point_estimate_does_not_confirm_advantage(self):
        r = numeric(fixture(), status="confirmed_positive")
        self.assertIn("positive_without_uncertainty", codes(r))
        r["model_records"][0].update(uncertainty_assessed=True, uncertainty_support="positive",
                                     uncertainty_refs=["s1"], validation_refs=["s1"])
        self.assertNotEqual(validate_decision_record(r)["status"], "structural_checks_passed")
        qualify_value(r)
        self.assertEqual(validate_decision_record(r)["status"], "structural_checks_passed")
        self.assertFalse(validate_decision_record(r)["economic_advantage_verified"])

    def test_valid_negative_cannot_be_hidden_by_qualitative_fallback(self):
        r = numeric(fixture(), ev=-0.04, status="negative")
        r["model_records"][0]["value_assessment"] = "valid_negative"
        self.assertIn("adverse_without_uncertainty", codes(r))
        self.assertNotIn("adverse_model_fit_conflict", codes(r))
        qualify_value(r, "negative")
        r["candidates"][0]["model_record_ids"] = []
        r["candidates"][0]["value"] = copy.deepcopy(fixture()["candidates"][0]["value"])
        self.assertIn("hidden_adverse_model", codes(r))
        self.assertIn("adverse_model_fit_conflict", codes(r))
        # Historical evidence from another version is not relabelled as current.
        r["model_records"][0]["version_id"] = "r0"
        self.assertNotIn("hidden_adverse_model", codes(r))

    def test_body_summary_are_checked_per_version(self):
        r = fixture()
        r["versions"][0]["summary_decision"]["fit"] = "不建议"
        self.assertIn("rendered_decision_mismatch", codes(r))
        del r["versions"][0]["body_decision"]
        self.assertGreater(validate_decision_record(r)["not_evaluated_count"], 0)

    def test_repeat_disagrees_without_selecting_majority_or_adding_matches(self):
        runs = [fixture(), fixture(), fixture()]
        for i, r in enumerate(runs):
            r["decision_context"]["run_id"] = str(i)
        runs[2]["contracts"][0]["selection"] = "away"
        result = compare_decision_runs(runs)
        self.assertEqual(result["status"], "comparable")
        self.assertAlmostEqual(result["primary_direction_pairwise_agreement"], 1 / 3)
        self.assertEqual(result["independent_match_count"], 1)
        self.assertIsNone(result["selected_direction"])
        self.assertGreater(result["rule_violations_by_run"][2], 0)

    def test_repeat_different_evidence_rules_settings_and_duplicate_runs_excluded(self):
        for field in ("evidence_bundle_sha256", "rules_sha256", "model_settings_sha256"):
            with self.subTest(field=field):
                left, right = fixture(), fixture()
                right["decision_context"].update(run_id="run2")
                right["decision_context"][field] = "d" * 64
                result = compare_decision_runs([left, right])
                self.assertEqual(result["status"], "not_comparable")
                self.assertIsNone(result["primary_direction_pairwise_agreement"])
        self.assertEqual(compare_decision_runs([fixture(), fixture()])["status"], "not_comparable")

    def test_repeat_contract_ids_are_not_direction_identity(self):
        left, right = fixture(), fixture()
        right["decision_context"]["run_id"] = "run2"
        right["contracts"][0]["contract_id"] = "another-id"
        right["candidates"][0]["contract_id"] = "another-id"
        result = compare_decision_runs([left, right])
        self.assertEqual(result["primary_direction_pairwise_agreement"], 1.0)
        left["contracts"][0]["line"] = 0
        right["contracts"][0]["line"] = -0.0
        self.assertEqual(compare_decision_runs([left, right])["primary_direction_pairwise_agreement"], 1.0)

    def test_cli_preserves_archive_and_refuses_overwriting_audit(self):
        with tempfile.TemporaryDirectory() as temp:
            path, out = Path(temp) / "record.json", Path(temp) / "audit.json"
            path.write_text(json.dumps(fixture(), ensure_ascii=False), encoding="utf-8")
            original = path.read_bytes()
            command = [sys.executable, "-m", "football_model.decision_audit", "validate", "--record", str(path), "--output", str(out)]
            result = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(subprocess.run(command, capture_output=True).returncode, 2)
            self.assertEqual(subprocess.run(command[:-1] + [str(path)], capture_output=True).returncode, 2)
            self.assertEqual(path.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
