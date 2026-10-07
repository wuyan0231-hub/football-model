"""Structural shadow-grade regressions; synthetic inputs do not prove accuracy."""
import copy
import unittest

from football_model.shadow_grading import CHAIN_FIELDS, validate_shadow_grading


def argument(ref="source1"):
    return {"reason": "合成测试文字；须回原文人工判断", "refs": [ref]}


def fixture(grade="A"):
    view = {
        "candidate_id": "c1", "contract_id": "ct1", "quote_offer_id": "offer1",
        "action_status": "candidate", "grade": grade,
        "objective": "盈利命中与支付风险的定性检查", "reason": "合成结构回归用例",
        "condition_ids": [],
        "decision_chain": {"complete": True, **{key: argument() for key in CHAIN_FIELDS}},
        "ordinary_branch_robustness": argument(), "distinguishing_evidence": argument(),
        "weakest_assumption": argument(), "failure_path": argument(), "not_a_reason": argument(),
    }
    record = {"decision_id": "decision1", "policy_id": "S2-decision-shadow",
              "policy_version": "0.1", "mode": "shadow", "evaluation_context": "current",
              "single": view, "parlay_leg": copy.deepcopy(view)}
    candidate = {"decision_id": "decision1", "candidate_id": "c1", "contract_id": "ct1",
                 "quote_id": "offer1", "condition_ids": [], "fit": "适合",
                 "model_record_ids": [], "value": {"status": "unquantified", "ev": None}}
    return record, candidate


def check(record, candidate, conditions=None, evidence_ids=None):
    return validate_shadow_grading(record, candidate=candidate,
                                   conditions=conditions if conditions is not None else [],
                                   evidence_ids=evidence_ids if evidence_ids is not None else {"source1"})


def codes(result):
    return {i["code"] for i in result["issues"]}


class ShadowGradingTests(unittest.TestCase):
    def test_qualitative_a_without_model_xg_or_confirmed_lineup(self):
        record, candidate = fixture()
        candidate.update(model_available=False, xg_available=False, lineup_status="ordinary_unknown")
        before = copy.deepcopy((record, candidate))
        output = check(record, candidate)
        self.assertEqual(output["status"], "structural_checks_passed")
        self.assertEqual((record, candidate), before)
        self.assertTrue(output["manual_review_required"])
        self.assertFalse(output["predictive_quality_verified"])
        self.assertFalse(output["economic_advantage_verified"])
        self.assertFalse(output["whole_ticket_grade_inferred"])
        self.assertFalse(output["formal_decision_modified"])

    def test_empty_legacy_extension_is_not_an_automatic_downgrade(self):
        output = validate_shadow_grading(None)
        self.assertEqual(output["status"], "not_evaluated")
        self.assertEqual(output["issues"], [])
        self.assertFalse(output["historical_grade_backfilled"])

    def test_version_required_and_formal_grade_refused(self):
        record, candidate = fixture()
        record.pop("policy_version")
        self.assertIn("missing_shadow_identity", codes(check(record, candidate)))
        record["mode"] = "formal"
        self.assertIn("shadow_is_not_formal", codes(check(record, candidate)))

    def test_nonempty_fields_never_grant_semantic_validation(self):
        record, candidate = fixture("B")
        output = check(record, candidate)
        self.assertEqual(output["status"], "structural_checks_passed")
        self.assertTrue(any("非空" in x for x in output["manual_review_required"]))
        record["single"]["decision_chain"]["complete"] = False
        self.assertIn("incomplete_shadow_chain", codes(check(record, candidate)))

    def test_archive_input_and_derived_refs_resolve_without_implying_sufficiency(self):
        record, candidate = fixture()
        record["single"]["decision_chain"]["support"]["refs"] = ["source1", "input1", "derived1"]
        output = check(record, candidate, evidence_ids={"source1", "input1", "derived1"})
        self.assertEqual(output["status"], "structural_checks_passed")
        output = check(record, candidate)
        self.assertIn("unknown_shadow_reference", codes(output))

    def test_missing_indexes_remain_incomplete(self):
        record, candidate = fixture()
        output = validate_shadow_grading(record, candidate=candidate)
        self.assertEqual(output["status"], "incomplete")
        self.assertIn("unverified_shadow_reference", codes(output))
        self.assertIn("unverified_shadow_conditions", codes(output))

    def test_binding_cannot_change_decision_candidate_contract_or_offer(self):
        for key in ("candidate_id", "contract_id", "quote_offer_id"):
            with self.subTest(key=key):
                record, candidate = fixture()
                record["single"][key] = "another"
                self.assertIn("shadow_candidate_mismatch", codes(check(record, candidate)))
        record, candidate = fixture()
        record["decision_id"] = "another"
        self.assertIn("shadow_decision_mismatch", codes(check(record, candidate)))

    def test_critical_condition_cannot_be_omitted_from_shadow_list(self):
        record, candidate = fixture()
        candidate["condition_ids"] = ["condition1"]
        condition = {"condition_id": "condition1", "critical": True, "state": "unknown"}
        self.assertIn("unresolved_shadow_condition", codes(check(record, candidate, [condition])))

    def test_waiting_is_separate_and_has_no_grade(self):
        record, candidate = fixture()
        for use in ("single", "parlay_leg"):
            record[use].update(grade=None, action_status="awaiting_condition", condition_ids=["condition1"])
        condition = {"condition_id": "condition1", "critical": True, "state": "unknown", "observable": True,
                     "actions": {"satisfied": "用新报价重评", "not_satisfied": "重评或不建议", "unknown": "继续待条件"}}
        self.assertEqual(check(record, candidate, [condition])["status"], "structural_checks_passed")
        record["single"]["grade"] = "A"
        self.assertIn("conditional_grade_not_null", codes(check(record, candidate, [condition])))

    def test_parlay_only_condition_does_not_block_single(self):
        record, candidate = fixture()
        candidate["formal_condition_ids"] = {"single": [], "parlay_leg": ["leg-condition"]}
        condition = {"condition_id": "leg-condition", "critical": True, "state": "unknown", "observable": True,
                     "actions": {"satisfied": "按新报价重评", "not_satisfied": "重新比较", "unknown": "继续待条件"}}
        output = check(record, candidate, [condition])
        paths = {item["path"] for item in output["issues"] if item["code"] == "unresolved_shadow_condition"}
        self.assertEqual(paths, {"parlay_leg.grade"})
        record["parlay_leg"].update(grade=None, action_status="awaiting_condition")
        record["difference_reason"] = {"objective_difference": "单关与串关的用途不同",
                                       "risk_distribution_reason": "腿专属关键事实尚未确认", "refs": ["source1"]}
        self.assertEqual(check(record, candidate, [condition])["status"], "structural_checks_passed")

    def test_empty_scopes_cannot_erase_shared_critical_condition(self):
        record, candidate = fixture()
        candidate["condition_ids"] = ["common-condition"]
        candidate["formal_condition_ids"] = {"single": [], "parlay_leg": []}
        condition = {"condition_id": "common-condition", "critical": True, "state": "unknown"}
        output = check(record, candidate, [condition])
        paths = {item["path"] for item in output["issues"] if item["code"] == "unresolved_shadow_condition"}
        self.assertEqual(paths, {"single.grade", "parlay_leg.grade"})

    def test_condition_satisfied_demands_new_assessment_not_old_grade(self):
        record, candidate = fixture()
        record["previous_action_status"] = "awaiting_condition"
        self.assertIn("missing_shadow_reassessment", codes(check(record, candidate)))
        record["reassessment"] = {"completed": True, "inherited_grade": False,
                                  "previous_decision_id": "old-decision", "decision_id": "decision1",
                                  "contract_id": "ct1", "quote_offer_id": "offer1", "basis": argument()}
        self.assertEqual(check(record, candidate)["status"], "structural_checks_passed")
        record["reassessment"]["quote_offer_id"] = "old-offer"
        self.assertIn("stale_shadow_reassessment", codes(check(record, candidate)))
        record["reassessment"]["inherited_grade"] = True
        self.assertIn("automatic_grade_inheritance", codes(check(record, candidate)))

    def test_single_parlay_difference_needs_purpose_and_risk_reason(self):
        record, candidate = fixture()
        record["parlay_leg"]["grade"] = "B"
        self.assertIn("unexplained_shadow_difference", codes(check(record, candidate)))
        record["difference_reason"] = {"objective_difference": "单腿和整票用途差异",
                                       "risk_distribution_reason": "已说明共同失败路径；不声称精确归零率",
                                       "refs": ["source1"]}
        self.assertEqual(check(record, candidate)["status"], "structural_checks_passed")

    def test_c_requires_explicit_evidence_or_purpose_limitation(self):
        record, candidate = fixture("C")
        for use in ("single", "parlay_leg"):
            record[use].update(action_status="watch_only", c_reason="purpose_mismatch", limitation=argument())
        self.assertEqual(check(record, candidate)["status"], "structural_checks_passed")
        record["single"]["c_reason"] = "small_stake"
        self.assertIn("missing_c_category", codes(check(record, candidate)))

    def test_d_and_u_are_distinct_and_do_not_rank_probabilities(self):
        for grade in ("D", "U"):
            record, candidate = fixture(grade)
            for use in ("single", "parlay_leg"):
                record[use].update(action_status="not_recommended" if grade == "D" else "unable_to_judge",
                                 rejection_basis=argument(), unknown_reason="无法确定必要合约状态")
            self.assertEqual(check(record, candidate)["status"], "structural_checks_passed")

    def test_fixed_probability_or_ev_and_automatic_inheritance_are_rejected(self):
        for key in ("fixed_hit_rate", "fixed_win_probability", "fixed_ev", "grade_probability", "grade_ev"):
            record, candidate = fixture()
            record["single"][key] = 0.8
            self.assertIn("fixed_grade_quantification", codes(check(record, candidate)))
        record, candidate = fixture()
        record["single"]["grade_inherited"] = True
        self.assertIn("automatic_grade_inheritance", codes(check(record, candidate)))

    def test_explicit_action_contradiction_is_not_silently_changed(self):
        record, candidate = fixture("U")
        before = copy.deepcopy(record)
        self.assertIn("shadow_grade_action_conflict", codes(check(record, candidate)))
        self.assertEqual(record, before)

    def test_unknown_condition_criticality_is_not_assumed_harmless(self):
        record, candidate = fixture()
        candidate["condition_ids"] = ["condition1"]
        condition = {"condition_id": "condition1", "critical": None, "state": "unknown"}
        self.assertEqual(check(record, candidate, [condition])["status"], "incomplete")
        candidate["condition_ids"] = "condition1"
        self.assertIn("invalid_formal_condition_ids", codes(check(record, candidate, [condition])))

    def test_retrospective_development_does_not_backfill_original(self):
        record, candidate = fixture()
        record["evaluation_context"] = "retrospective_development"
        self.assertIn("historical_grade_backfill", codes(check(record, candidate)))
        record.update(original_report_modified=False, historical_grade_claim=False)
        self.assertEqual(check(record, candidate)["status"], "structural_checks_passed")
        record["evaluation_context"] = "historical_original"
        self.assertIn("invalid_shadow_context", codes(check(record, candidate)))

    def test_malformed_values_report_issues_instead_of_raising(self):
        for bad in ([], "A", 1, False):
            self.assertEqual(validate_shadow_grading(bad)["status"], "blocked")
        for field, bad in (("grade", []), ("action_status", {}), ("condition_ids", [None])):
            record, candidate = fixture()
            record["single"][field] = bad
            self.assertNotEqual(check(record, candidate)["status"], "structural_checks_passed")


if __name__ == "__main__":
    unittest.main()
