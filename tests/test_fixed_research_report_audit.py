import importlib.util
from pathlib import Path
import unittest
import json
import tempfile

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "scripts/fixed_research_report_audit.py"
if not SOURCE.exists():
    SOURCE = Path(__file__).with_name("fixed_research_report_audit.py")
spec = importlib.util.spec_from_file_location("fixed_research_report_audit", SOURCE)
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)


def score(value):
    return {"scoreline_nll": value}


class EqualityTests(unittest.TestCase):
    def test_numeric_tolerance(self):
        audit.equal({"x": [1.0]}, {"x": [1.0 + 1e-13]})

    def test_difference_rejected(self):
        with self.assertRaises(audit.AuditError):
            audit.equal(1.0, 1.00001)

    def test_nonfinite_rejected(self):
        for x in (float("nan"), float("inf")):
            with self.assertRaises(audit.AuditError):
                audit.equal(x, x)

    def test_bool_not_number(self):
        with self.assertRaises(audit.AuditError):
            audit.equal(True, 1)

    def test_missing_field(self):
        with self.assertRaises(audit.AuditError):
            audit.equal({}, {"a": None})

    def test_list_shape(self):
        with self.assertRaises(audit.AuditError):
            audit.equal([1], [1, 2])

    def test_no_naive_time(self):
        with self.assertRaises(audit.AuditError):
            audit.time_value("2026-01-01T00:00:00")


class DenominatorTests(unittest.TestCase):
    def setUp(self):
        self.rows = [{"scores": {"M0": score(3), "C": score(2), "C_policy": score(2)}},
                     {"scores": {"C": score(4), "C_policy": score(4)}},
                     {"scores": {"M0": score(5), "C_policy": score(5)}}, {"scores": {}}]

    def test_full_denominator_and_fallback(self):
        out = audit.denominator(self.rows, "C")
        self.assertEqual([out[k] for k in ("requested", "native_paired", "native_only", "fallback", "failed_unforecast")], [4,1,1,1,1])
        self.assertEqual(out["native_paired_nll_delta"], -1)
        self.assertEqual(out["policy_paired_nll_delta"], -.5)

    def test_unforecast_not_zero_loss(self):
        self.assertIsNone(audit.denominator([{"scores": {}}], "C")["policy_paired_nll_delta"])

    def test_changed_fallback_rejected(self):
        self.rows[2]["scores"]["C_policy"] = score(0)
        with self.assertRaises(audit.AuditError):
            audit.denominator(self.rows, "C")

    def test_invented_score_rejected(self):
        self.rows[3]["scores"]["C_policy"] = score(0)
        with self.assertRaises(audit.AuditError):
            audit.denominator(self.rows, "C")


class IncrementTests(unittest.TestCase):
    def test_matched_parent_not_global_parent_and_G1_EARLY_only(self):
        report = {"protocol": {"family_increment_comparisons": [["A", "B"]]},
            "scored_matches": [
                {"horizon": "EARLY", "scores": {"G0": score(3), "G1": score(2), "B": score(99)},
                 "matched_pool_ablation_scores": {"A_minus_B": {"added": score(2), "parent_on_added_training_pool": score(2.5)}}},
                {"horizon": "LINEUP", "scores": {"G0": score(3), "G1": score(1)}, "matched_pool_ablation_scores": {}}],
            "family_increments": {"A_minus_B": {"n":1,"mean_paired_scoreline_nll_difference":-.5},
                                  "G1_minus_G0": {"n":1,"mean_paired_scoreline_nll_difference":-1}}}
        out = audit.recompute_increments(report)
        self.assertEqual(out["A_minus_B"]["delta_added_minus_matched_parent"], -.5)
        self.assertEqual(out["G1_minus_G0"]["n"], 1)
        report["family_increments"]["A_minus_B"]["n"] = 2
        with self.assertRaises(audit.AuditError):
            audit.recompute_increments(report)


class SelectionTests(unittest.TestCase):
    def setUp(self):
        self.cutoff = "2026-01-02T00:00:00Z"
        self.fit = {"model": {"alpha": 2, "training_cutoff": self.cutoff, "last_result_available_at": "2026-01-01T00:00:00Z"},
                    "selection": {"status": "past_only_inner_time_validation", "inner_cutoff": "2026-01-01T00:00:00Z",
                                  "selected_alpha": 2, "trials": [{"alpha":1,"score":3,"status":"scored"},{"alpha":2,"score":3,"status":"scored"}]}}
        self.protocol = {"correction_alpha_grid": [1,2]}

    def test_tie_chooses_stronger_penalty(self):
        audit.check_selection(self.fit, self.cutoff, self.protocol)

    def test_bad_selection_rejected(self):
        self.fit["model"]["alpha"] = 1
        with self.assertRaises(audit.AuditError):
            audit.check_selection(self.fit, self.cutoff, self.protocol)

    def test_future_inner_cutoff_rejected(self):
        self.fit["selection"]["inner_cutoff"] = "2026-01-03T00:00:00Z"
        with self.assertRaises(audit.AuditError):
            audit.check_selection(self.fit, self.cutoff, self.protocol)

    def test_future_target_rejected(self):
        self.fit["model"]["last_result_available_at"] = "2026-01-03T00:00:00Z"
        with self.assertRaises(audit.AuditError):
            audit.check_selection(self.fit, self.cutoff, self.protocol)

    def test_fixed_penalty_fallback(self):
        self.fit["selection"] = {"status":"cold_start_fixed_strongest_penalty"}
        audit.check_selection(self.fit, self.cutoff, self.protocol)

    def test_no_unknown_selection(self):
        self.fit["selection"] = {"status":"invented"}
        with self.assertRaises(audit.AuditError):
            audit.check_selection(self.fit, self.cutoff, self.protocol)

    def test_training_pool_filters_future_missing_and_bool(self):
        replay = {"forecasts": [{"match_id": i, "target_available_at": t} for i,t in
            [(1,"2026-01-01T00:00:00Z"),(2,"2026-01-03T00:00:00Z"),(3,"2026-01-01T00:00:00Z"),(4,"2026-01-01T00:00:00Z")]]}
        features = {1:{"home":{"x":1},"away":{"x":2}},2:{"home":{"x":1},"away":{"x":2}},
                    3:{"home":{"x":1},"away":{}},4:{"home":{"x":True},"away":{"x":2}}}
        self.assertEqual(audit.expected_training_ids(replay,features,["x"],self.cutoff),[1])

    def test_duplicate_training_pool_rejected(self):
        replay = {"forecasts":[{"match_id":1,"target_available_at":"2026-01-01T00:00:00Z"}]*2}
        with self.assertRaises(audit.AuditError):
            audit.expected_training_ids(replay,{1:{"home":{},"away":{}}},[],self.cutoff)


class ProgressTests(unittest.TestCase):
    def test_atomic_partial_preserves_completed_leagues_and_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)/"audit.json"
            audit.checkpoint_output(p, {"status":"audit_in_progress","results":[{"league":"example","status":"completed_with_stated_scope"}]})
            out = json.loads(p.read_text());out["status"]="audit_incomplete"
            audit.checkpoint_output(p,out)
            self.assertEqual(json.loads(p.read_text())["results"],out["results"])
            self.assertEqual(list(Path(tmp).glob("*.tmp")),[])

    def test_output_bound_preserves_previous_checkpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)/"audit.json"
            audit.checkpoint_output(p,{"status":"audit_in_progress"})
            original = p.read_bytes()
            with self.assertRaises(audit.AuditError):
                audit.checkpoint_output(p,{"oversized":"x"*(513*1024)})
            self.assertEqual(p.read_bytes(),original)

    def test_nonfinite_checkpoint_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)/"audit.json"
            with self.assertRaises(ValueError):
                audit.checkpoint_output(p,{"value":float("nan")})
            self.assertFalse(p.exists())

    def test_exception_values_and_source_lines_not_exported(self):
        try:
            raise KeyError("private-input-not-for-export")
        except KeyError as exc:
            detail = audit.failure_detail(exc)
        self.assertNotIn("private-input-not-for-export",json.dumps(detail))
        self.assertTrue(detail["frames"])
        self.assertEqual(set(detail["frames"][-1]),{"file","function","line"})

    def test_existing_outputs_cannot_be_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/"audit.json";p.touch()
            with self.assertRaises(audit.AuditError):
                audit.validate_output(p,Path(tmp))

    def test_protected_runtime_and_source_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            for parent in ("runtime","fixed-research-audit-runtime","football_model","scripts","tests",".github",".git"):
                with self.assertRaises(audit.AuditError):
                    audit.validate_output(Path(tmp)/parent/"audit.json",Path(tmp))
            self.assertEqual(audit.validate_output(Path(tmp)/"audit.json",Path(tmp)),Path(tmp)/"audit.json")


if __name__ == "__main__":
    unittest.main()
