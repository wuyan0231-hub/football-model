"""Real arithmetic and provenance gates; synthetic fixtures prove no forecast skill."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from football_model.decision_quotes import adapt_quote_row
from football_model.feature_decisions import comparison, project_offer, schedule_comparisons, stage_decision_input, html_section
from football_model.f5c_access import export_report
from football_model.model import _distribution_from_rates
from tests.test_decision_quotes import row as quote_row

NOW = datetime(2026, 10, 7, 2, 30, tzinfo=timezone.utc)


class FeatureDecisionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.runtime = Path(self.temp.name)
        self.baseline = _distribution_from_rates(1.5, 1.1)
        self.row = {"match_id": 100, "kickoff": (NOW + timedelta(hours=1)).isoformat(),
                    "home_id": 1, "away_id": 2, "generated_at": (NOW-timedelta(hours=1)).isoformat(),
                    "prediction": self.baseline}
        self.source = self.runtime / "feature_decisions/sources/image.png"
        self.source.parent.mkdir(parents=True)
        self.source.write_bytes(b"\x89PNG\r\n\x1a\nsynthetic-test-only-image-bytes")
        self.source_ref = {"path": "feature_decisions/sources/image.png", "sha256": hashlib.sha256(self.source.read_bytes()).hexdigest(),
            "source_id": "screenshot-2", "observed_at": "2026-10-07T10:15:14+08:00", "reviewed": True}
        self.input = {"schema_version": "feature-decision-input-v1", "match_id": 100,
                      "analysis_as_of": NOW.isoformat(), "quote_rows": [quote_row()], "sources": [self.source_ref]}
        self.input_path = self.runtime / "quotes.json"

    def tearDown(self):
        self.temp.cleanup()

    def run_comparison(self, input=None, candidates=None):
        if input is not None:
            self.input_path.write_text(json.dumps(input))
        candidates = candidates or {"M2": {"status": "verified", "forecast_id": "m2", "prediction": _distribution_from_rates(2, .8)},
                                    "M3": {"status": "unavailable", "reason": "missing_lineup"}}
        advanced = {"status": "available", "baseline_parent_row": self.row,
                    "protocol_sha256": "a"*64, "candidates": candidates, "evidence": []}
        with patch("football_model.feature_decisions.schedule_comparisons", return_value=[]), patch(
                "football_model.advanced_snapshots.load_verified_comparison", return_value=advanced):
            return comparison(self.runtime, "serie_a", 100, row=self.row, now=NOW,
                              decision_input="quotes.json" if input is not None else None)

    def test_quarter_handicap_exact_five_states_and_probabilities(self):
        # Exact draw: home -0.25 half loses, away +0.25 half wins.
        prediction = {"score_matrix": [[1.0]]}
        home, away = adapt_quote_row(quote_row(), match_id="100")["offers"]
        h, a = project_offer(prediction, home, 0), project_offer(prediction, away, 0)
        self.assertEqual(h["probabilities"]["half_loss"], 1)
        self.assertEqual(a["probabilities"]["half_win"], 1)
        self.assertEqual(h["net_ev"], -.5)
        self.assertEqual(a["net_ev"], .49)
        self.assertEqual(a["positive_net_return_probability"], 1)

    def test_quarter_total_over_and_under(self):
        prediction = {"score_matrix": [[0, 0, 1.0]]}
        over, under = adapt_quote_row(quote_row(market="totals", line=2.25,
            odds={"over": .92, "under": .98}), match_id="100")["offers"]
        self.assertEqual(project_offer(prediction, over, 0)["net_ev"], -.5)
        self.assertEqual(project_offer(prediction, under, 0)["net_ev"], .49)

    def test_missing_fees_does_not_mean_zero(self):
        result = self.run_comparison(self.input)
        self.assertEqual(result["status"], "calculated")
        for c in result["calculations"]:
            if c["status"] == "calculated":
                self.assertIsNotNone(c["result"]["gross_ev"])
                self.assertIsNone(c["result"]["net_ev"])
                self.assertIsNone(c["delta_net_ev"])
                self.assertIsNone(c["net_ev_sign_disagreement"])
                self.assertEqual(c["result"]["execution_status"], "unknown")

    def test_explicit_fee_changes_push_to_negative_net_return(self):
        offer = adapt_quote_row(quote_row(line=0), match_id="100")["offers"][0]
        p = project_offer({"score_matrix": [[1.0]]}, offer, .01)
        self.assertEqual(p["nonnegative_gross_return_probability"], 1)
        self.assertEqual(p["nonnegative_net_return_probability"], 0)
        self.assertEqual(p["net_ev"], -.01)

    def test_no_quote_retains_exact_matrices_and_failed_branch(self):
        result = self.run_comparison()
        self.assertEqual(result["status"], "no_quote_supplied")
        self.assertEqual(result["calculations"], [])
        self.assertEqual(result["model_records"][0]["prediction"], self.baseline)
        self.assertTrue(any(m["status"] == "unavailable" for m in result["model_records"]))
        self.assertFalse(result["production_promotion"])

    def test_all_candidates_share_contract_and_quote_baseline_including_failure(self):
        result = self.run_comparison({**self.input, "cost_per_unit": 0})
        home = [c for c in result["calculations"] if c["contract_id"] == result["calculations"][0]["contract_id"]]
        self.assertEqual(len({c["quote_offer_id"] for c in home}), 1)
        self.assertEqual(len({c["row_sha256"] for c in home}), 1)
        self.assertEqual(len(home), 5)  # M0, and each EARLY/LINEUP M2/M3.
        base = next(c for c in home if c["model_key"] == "M0")
        for c in home:
            if c["status"] == "calculated":
                self.assertAlmostEqual(c["delta_net_ev"], c["result"]["net_ev"]-base["result"]["net_ev"])
            else:
                self.assertIsNone(c["result"])

    def test_forecast_with_rejected_verification_cannot_be_promoted_by_prediction_field(self):
        result = self.run_comparison(candidates={"bad": {"status": "rejected", "prediction": self.baseline}})
        self.assertEqual([m["model_key"] for m in result["model_records"] if m["status"] == "verified"], ["M0"])
        self.assertEqual(sum(m["status"] == "rejected" for m in result["model_records"]), 2)
        self.assertTrue(all(c["status"] == "rejected" for c in result["comparisons"]))

    def test_closed_latest_never_uses_older_price(self):
        data = deepcopy(self.input)
        data["quote_rows"] = [quote_row(status="closed"), quote_row(row_id="older", row_order=1,
            quote_time="2026-10-07T10:00:00+08:00")]
        result = self.run_comparison(data)
        self.assertEqual(len(result["quote_records"]), 1)
        self.assertEqual(result["quote_records"][0]["original_row"]["status"], "closed")
        self.assertTrue(all(c["result"]["net_ev"] is None for c in result["calculations"] if c["result"]))

    def test_reference_only_remains_reference_not_open(self):
        data = deepcopy(self.input)
        data["quote_rows"] = [quote_row(status="reference_only", reference_only=True, availability_status="unknown")]
        result = self.run_comparison(data)
        self.assertEqual(result["quote_records"][0]["row_status"], "reference_only")
        self.assertTrue(all(c["result"]["current_open_status"] == "unknown" for c in result["calculations"] if c["result"]))

    def test_after_cutoff_or_unfrozen_quote_source_is_rejected(self):
        for updates in ({"observed_at": (NOW+timedelta(minutes=1)).isoformat()},
                        {"reviewed": False}, {"sha256": "a"*64}, {"path": "../outside"}):
            data = deepcopy(self.input)
            data["sources"][0].update(updates)
            result = self.run_comparison(data)
            self.assertEqual(result["status"], "quote_input_rejected", updates)
            self.assertEqual(result["calculations"], [])

    def test_row_observation_after_cutoff_is_local_quote_failure(self):
        data = deepcopy(self.input)
        data["quote_rows"][0]["observed_at"] = (NOW+timedelta(seconds=1)).isoformat()
        result = self.run_comparison(data)
        self.assertEqual(result["status"], "no_eligible_quote")
        self.assertEqual(result["quote_issues"][0]["reason"], "decision_quote_observation_time_mismatch")

    def test_input_cutoff_and_fixture_must_match(self):
        for updates in ({"analysis_as_of": (NOW+timedelta(seconds=1)).isoformat()}, {"match_id": 101}):
            result = self.run_comparison({**self.input, **updates})
            self.assertEqual(result["status"], "quote_input_rejected")

    def test_malformed_input_preserves_baseline_and_never_partially_adopts(self):
        for updates in ({"quote_rows": "bad"}, {"sources": [None]}, {"selection": "home"}, {"cost_per_unit": True}):
            result = self.run_comparison({**self.input, **updates})
            self.assertEqual(result["status"], "quote_input_rejected")
            self.assertEqual(result["calculations"], [])
            self.assertEqual(result["model_records"][0]["prediction"], self.baseline)

    def test_manual_selection_links_record_not_automatic_ev_winner(self):
        data = {**self.input, "cost_per_unit": 0, "selection": {"model_key": "M0", "quote_row_id": "IMG02-ah-row1",
            "selection": "home", "fit": "条件适合", "reason": "synthetic declared judgment"}}
        result = self.run_comparison(data)
        self.assertEqual(result["decision"]["status"], "explicit_selection_linked")
        cid = result["decision"]["calculation_id"]
        self.assertEqual(next(c for c in result["calculations"] if c["calculation_id"] == cid)["model_key"], "M0")
        self.assertFalse(result["decision"]["automatic_recommendation"])
        self.assertFalse(result["decision"]["formal_grade_verified"])

    def test_unavailable_explicit_selection_is_not_substituted(self):
        data = {**self.input, "selection": {"model_key": "nonexistent", "quote_row_id": "IMG02-ah-row1", "selection": "home"}}
        result = self.run_comparison(data)
        self.assertEqual(result["decision"]["status"], "explicit_selection_unavailable")
        self.assertIsNone(result["decision"]["calculation_id"])

    def test_export_contains_exact_sources_matrices_and_visible_numeric_link(self):
        result = self.run_comparison({**self.input, "cost_per_unit": 0})
        packet = {"f5c_comparison": {"status": "not_captured", "competition": "serie_a", "match_id": 100},
                  "feature_decision_comparison": result}
        html, archive = self.runtime.parent / (self.runtime.name+".html"), self.runtime.parent / (self.runtime.name+".zip")
        try:
            export_report(packet, self.runtime, html, archive)
            self.assertIn("feature-decisions", html.read_text())
            with zipfile.ZipFile(archive) as z:
                self.assertEqual(z.read("originals/feature_decisions/sources/image.png"), self.source.read_bytes())
                self.assertEqual(z.read("originals/quotes.json"), self.input_path.read_bytes())
                saved = json.loads(z.read("comparison.json"))["feature_decision_comparison"]
                self.assertEqual(saved["model_records"][0]["prediction"], self.baseline)
        finally:
            html.unlink(missing_ok=True); archive.unlink(missing_ok=True)

    def test_corruption_between_comparison_and_export_rejects(self):
        result = self.run_comparison(self.input)
        self.source.write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "feature_decision_original_changed_before_export"):
            export_report({"f5c_comparison": {"status": "not_captured"}, "feature_decision_comparison": result},
                self.runtime, self.runtime.parent/(self.runtime.name+"bad.html"), self.runtime.parent/(self.runtime.name+"bad.zip"))

    def test_external_image_staging_state_restore_and_numeric_source_closure(self):
        from football_model import state_storage as storage
        with tempfile.TemporaryDirectory() as outside, tempfile.TemporaryDirectory() as transport, tempfile.TemporaryDirectory() as restored:
            external = Path(outside)
            png = self.source.read_bytes()
            (external/"original.png").write_bytes(png)
            source = {**self.source_ref, "path": "original.png"}
            original = {**self.input, "sources": [source]}
            original_bytes = json.dumps(original, ensure_ascii=False).encode()
            (external/"reviewed.json").write_bytes(original_bytes)
            staged_path = stage_decision_input(self.runtime, "reviewed.json", source_root=external)
            staged = json.loads((self.runtime/staged_path).read_bytes())
            self.assertEqual((self.runtime/staged["sources"][0]["path"]).read_bytes(), png)
            self.assertEqual((external/"reviewed.json").read_bytes(), original_bytes)
            storage.pack_runtime(self.runtime, Path(transport))
            storage.restore_runtime(Path(transport), Path(restored))
            with patch("football_model.feature_decisions.schedule_comparisons", return_value=[]), patch(
                    "football_model.advanced_snapshots.load_verified_comparison", return_value={"status":"unavailable","candidates":{}}):
                value = comparison(restored,"serie_a",100,row=self.row,now=NOW,decision_input=staged_path)
            self.assertEqual(value["status"], "calculated", value.get("quote_issues"))
            self.assertTrue(any(e["path"] == staged["staging"]["original_input_ref"]["path"] for e in value["evidence"]))
            self.assertEqual((Path(restored)/staged["sources"][0]["path"]).read_bytes(), png)

    def test_staging_rejects_bad_image_before_writing_runtime(self):
        with tempfile.TemporaryDirectory() as outside:
            external=Path(outside)
            (external/"bad.png").write_bytes(b"not an image")
            source={**self.source_ref,"path":"bad.png","sha256":hashlib.sha256(b"not an image").hexdigest()}
            (external/"reviewed.json").write_text(json.dumps({**self.input,"sources":[source]}))
            with self.assertRaisesRegex(ValueError,"decision_image_header_invalid"):
                stage_decision_input(self.runtime,"reviewed.json",source_root=external)
            self.assertFalse((self.runtime/"feature_decisions/inputs").exists())


class NativeDecisionLinkTests(unittest.TestCase):
    """Exercise the real S2 compiler/auditor, never a mocked approval status."""

    setUp = FeatureDecisionTests.setUp
    tearDown = FeatureDecisionTests.tearDown
    run_comparison = FeatureDecisionTests.run_comparison

    def native_input(self):
        from tests.test_decision_record import fixture
        record = fixture(self.runtime)
        record.update(match_id="100", analysis_cutoff=NOW.isoformat(), kickoff_at=self.row["kickoff"])
        record["inputs"] = [{"input_id": "image-input", "source_id": self.source_ref["source_id"],
            "path": self.source_ref["path"], "sha256": self.source_ref["sha256"],
            "role": "screenshot", "observed_at": self.source_ref["observed_at"],
            "published_at": None, "source_claimed_published_at": None}]
        record["quote_rows"] = deepcopy(self.input["quote_rows"])
        record["archive"]["sources"] = [{"source_id": self.source_ref["source_id"]}]
        candidate = record["archive"]["candidates"][0]
        candidate.update(quote_row_id="IMG02-ah-row1", selection="home", role="primary",
            support_refs=[self.source_ref["source_id"]],
            counterevidence=[{"source_refs": [self.source_ref["source_id"]], "handling": "retained", "reason": "Synthetic reason"}])
        alternate = deepcopy(candidate)
        alternate.update(candidate_id="c2", selection="away", role="comparison")
        alternate["formal_fit"] = {"single": "适合", "parlay_leg": "不建议"}
        record["archive"]["candidates"].append(alternate)
        record["decision_chain"] = {"strongest_alternative_candidate_id": "c2", "alternative_reason": "Synthetic payment comparison"}
        return {**self.input, "decision_record": record,
            "decision_calculation_links": [{"candidate_id": "c1", "model_key": "M0"}, {"candidate_id": "c2", "model_key": "M0"}]}

    def test_native_record_runs_real_audits_and_independent_fits(self):
        data = self.native_input()
        before = deepcopy(data)
        result = self.run_comparison(data)
        decision = result["decision"]
        self.assertEqual(decision["status"], "structural_checks_passed", decision)
        self.assertEqual(data, before)
        self.assertEqual(decision["views"]["membership"]["sets_by_usage"]["parlay_leg"]["不建议"], ["c2"])
        self.assertEqual(decision["decision_record"]["decision_chain"]["strongest_alternative_candidate_id"], "c2")
        self.assertEqual(len(decision["candidate_calculations"]), 2)
        for usage in ("single", "parlay_leg"):
            self.assertEqual(decision["validation"]["legacy_audits_by_usage"][usage]["status"], "structural_checks_passed")
        rendered = html_section(result)
        self.assertIn("feature-decision-fits", rendered)
        self.assertIn("最近替代", rendered)
        self.assertNotIn("<script>", rendered)
        self.assertFalse(decision["formal_grade_verified"])

    def test_missing_alternative_and_usage_stay_incomplete(self):
        data = self.native_input()
        data["decision_record"]["decision_chain"] = {}
        data["decision_record"]["archive"]["candidates"][0]["formal_fit"]["parlay_leg"] = None
        decision = self.run_comparison(data)["decision"]
        self.assertEqual(decision["status"], "incomplete")
        codes = {i["code"] for i in decision["validation"]["issues"]}
        self.assertIn("strongest_alternative_required", codes)
        self.assertIn("formal_fit_unknown", codes)

    def test_wrong_quote_or_cutoff_cannot_link_native_decision(self):
        for change in ("quote", "cutoff", "match"):
            data = self.native_input()
            if change == "quote":
                data["decision_record"]["quote_rows"][0]["odds"]["home"] = .99
            elif change == "cutoff":
                data["decision_record"]["analysis_cutoff"] = (NOW-timedelta(minutes=1)).isoformat()
            else:
                data["decision_record"]["match_id"] = "101"
            result = self.run_comparison(data)
            self.assertEqual(result["status"], "calculated")
            self.assertEqual(result["decision"]["status"], "blocked", (change, result["decision"]))

    def test_explicit_selection_cannot_contradict_native_primary(self):
        data = self.native_input()
        data["selection"] = {"model_key": "M0", "quote_row_id": "IMG02-ah-row1", "selection": "away"}
        decision = self.run_comparison(data)["decision"]
        self.assertEqual(decision["status"], "blocked")
        self.assertIn("native_decision_conflicts_with_selection", {i["code"] for i in decision["validation"]["issues"]})

    def test_all_ticket_model_disagreements_require_declared_treatment(self):
        data = {**self.native_input(), "cost_per_unit": 0}
        candidates = {"M2": {"status": "verified", "forecast_id": "m2", "prediction": _distribution_from_rates(.5, 2)}}
        first = self.run_comparison(data, candidates)
        disagreements = first["decision"]["model_disagreements"]
        self.assertTrue(disagreements)
        self.assertEqual(first["decision"]["status"], "incomplete")
        data["decision_record"]["decision_chain"]["model_disagreements"] = [
            {"candidate_id": d["candidate_id"], "model_key": d["model_key"], "treatment": "retain",
             "information_basis": "Synthetic changed rates only; no football evidence claim",
             "reason": "Keep original declared ticket for engineering comparison"} for d in disagreements]
        second = self.run_comparison(data, candidates)
        self.assertEqual(second["decision"]["status"], "structural_checks_passed", second["decision"])
        self.assertEqual(first["decision"]["primary_calculation_id"], second["decision"]["primary_calculation_id"])

    def test_native_evidence_and_numeric_failure_preserve_legal_calculations(self):
        data = self.native_input()
        data["decision_record"]["inputs"][0]["sha256"] = "0"*64
        data["decision_record"]["archive"]["candidates"][0]["value"]["ev"] = .99
        result = self.run_comparison(data)
        self.assertEqual(result["status"], "calculated")
        self.assertEqual(result["decision"]["status"], "blocked")
        codes = {i["code"] for i in result["decision"]["validation"]["issues"]}
        self.assertTrue({"input_sha256_mismatch", "native_decision_ev_does_not_match_calculation"} <= codes)

    def test_staged_native_record_keeps_exact_sources_and_exported_audit(self):
        data = self.native_input()
        with tempfile.TemporaryDirectory() as outside:
            external = Path(outside)
            (external/"original.png").write_bytes(self.source.read_bytes())
            data["sources"][0] = {**data["sources"][0], "path": "original.png"}
            data["decision_record"]["inputs"][0]["path"] = "original.png"
            (external/"reviewed.json").write_text(json.dumps(data))
            staged_path = stage_decision_input(self.runtime, "reviewed.json", source_root=external)
            with patch("football_model.feature_decisions.schedule_comparisons", return_value=[]), patch(
                    "football_model.advanced_snapshots.load_verified_comparison", return_value={"status": "unavailable", "candidates": {}}):
                result = comparison(self.runtime, "serie_a", 100, row=self.row, now=NOW, decision_input=staged_path)
            self.assertEqual(result["decision"]["status"], "structural_checks_passed", result["decision"])
            packet = {"f5c_comparison": {"status": "not_captured"}, "feature_decision_comparison": result}
            export_report(packet, self.runtime, external/"output.html", external/"output.zip")
            self.assertIn("feature-decision-fits", (external/"output.html").read_text())
            with zipfile.ZipFile(external/"output.zip") as archive:
                saved = json.loads(archive.read("comparison.json"))["feature_decision_comparison"]
                self.assertEqual(saved["decision"]["validation"], result["decision"]["validation"])
                for ref in result["evidence"]:
                    self.assertEqual(hashlib.sha256(archive.read("originals/"+ref["path"])).hexdigest(), ref["sha256"])

    def test_declared_numeric_value_binds_exact_forecast_and_actual_arithmetic(self):
        data = {**self.native_input(), "cost_per_unit": 0}
        initial = self.run_comparison(data)
        arithmetic = next(c for c in initial["calculations"] if c["model_key"] == "M0")
        p = arithmetic["result"]["probabilities"]
        breakeven = 1+(p["loss"]+.5*p["half_loss"])/(p["win"]+.5*p["half_win"])
        record = data["decision_record"]
        c = record["archive"]["candidates"][0]
        c["model_record_ids"] = ["actual-model"]
        c["value"].update(status="unclear", ev=arithmetic["result"]["net_ev"], breakeven_odds=breakeven,
            calculation_record_id="actual-calc", disclosed_model_record_ids=["actual-model"])
        record["archive"]["model_records"] = [{"model_record_id": "actual-model", "candidate_id": "c1",
            "version_id": record["version_id"], "forecast_id": arithmetic["forecast_id"],
            "applicable": True, "status": "recomputed", "probability_role": "F", "model_decision_role": "diagnostic"}]
        record["archive"]["calculation_records"] = [{"calculation_record_id": "actual-calc", "model_record_id": "actual-model",
            "contract_id": arithmetic["contract_id"], "quote_id": arithmetic["quote_offer_id"],
            "ev": c["value"]["ev"], "breakeven_odds": breakeven}]
        valid = self.run_comparison(data)["decision"]
        self.assertEqual(valid["status"], "structural_checks_passed", valid)
        record["archive"]["model_records"][0]["forecast_id"] = "stale-forecast-even-if-same-EV"
        invalid = self.run_comparison(data)["decision"]
        self.assertEqual(invalid["status"], "blocked")
        self.assertIn("native_decision_numeric_forecast_mismatch", {i["code"] for i in invalid["validation"]["issues"]})


class FeatureDecisionSavedSourcesTests(unittest.TestCase):
    def test_real_saved_schedule_capture_exact_parent_and_damage(self):
        from tests.test_feature_snapshots import FeatureSnapshotTests
        fixture = FeatureSnapshotTests()
        fixture.setUp()
        try:
            payload, receipt = fixture.real_v2_capture()
            at = datetime.fromisoformat(receipt["storage_completed_at"].replace("Z", "+00:00"))
            result = schedule_comparisons(fixture.runtime, "epl", 2000, row=payload["baseline_parent_row"], now=at)
            self.assertEqual(result[0]["status"], "verified_same_parent")
            self.assertTrue(any(c["status"] == "verified" for c in result[0]["candidates"].values()))
            bad_row = {**payload["baseline_parent_row"], "generated_at": "2025-01-02T01:00:00Z"}
            rejected = schedule_comparisons(fixture.runtime, "epl", 2000, row=bad_row, now=at)
            self.assertEqual(rejected[0]["reason"], "candidate_full_production_parent_mismatch")
            rejected = schedule_comparisons(fixture.runtime, "epl", 2000, row=payload["baseline_parent_row"], now=at-timedelta(seconds=1))
            self.assertEqual(rejected[0]["reason"], "capture_after_analysis_cutoff")
            ref = next(r for r in payload["source_refs"] if r["kind"] == "candidate_model")
            (fixture.runtime/ref["runtime_relative_path"]).write_text("{}")
            damaged = schedule_comparisons(fixture.runtime, "epl", 2000, row=payload["baseline_parent_row"], now=at)
            self.assertEqual(damaged[0]["status"], "verified_same_parent")
            self.assertTrue(all(c["status"] == "rejected" for c in damaged[0]["candidates"].values()))
        finally:
            fixture.tearDown()


if __name__ == "__main__":
    unittest.main()
