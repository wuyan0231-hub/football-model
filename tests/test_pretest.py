"""Engineering counterexamples only; all fixtures are synthetic, not new matches."""
import copy
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

from football_model.cli import iso, run_pipeline
from football_model.evidence import freeze_bundle
from football_model.decision_record import build_decision_bundle
from football_model.integration import match_packet
from football_model.pretest import assess_pretest, main, PretestError


NOW = datetime(2025, 1, 1, 0, 0, tzinfo=timezone.utc)
CUTOFF = NOW + timedelta(hours=1)
CHECKED = NOW + timedelta(hours=1, minutes=10)
KICKOFF = NOW + timedelta(days=2)
SOURCE = "a" * 40
STATE = "b" * 40


def archive():
    main_decision = {"candidate_id": "c1", "fit": "适合", "contract_id": "ct1", "quote_id": "q1",
                     "condition_ids": [], "value_status": "unquantified"}
    return {"schema_version": "match-archive-1.0", "decision_audit_version": "decision-audit-1.0",
            "match_id": "2000", "version_id": "r1", "analysis_cutoff": iso(CUTOFF), "kickoff_at": iso(KICKOFF),
            "sources": [{"source_id": "page", "url": "https://example.invalid/synthetic"}],
            "contracts": [{"contract_id": "ct1", "market": "ou", "selection": "under", "line": 2.5,
                           "settlement_basis": "REGULATION"}],
            "quotes": [{"quote_id": "q1", "contract_id": "ct1", "source_id": "page", "status": "open_reference",
                        "decimal_odds": 1.95}],
            "candidates": [{"candidate_id": "c1", "version_id": "r1", "contract_id": "ct1", "quote_id": "q1",
                            "fit": "适合", "support_refs": ["page"], "mechanism": "synthetic mechanism",
                            "payment_requirement": "synthetic payment", "condition_ids": [], "model_record_ids": [],
                            "counterevidence": [{"source_refs": ["page"], "handling": "retained", "reason": "synthetic"}],
                            "value": {"status": "unquantified", "ev": None, "breakeven_odds": None,
                                      "calculation_record_id": None, "disclosed_model_record_ids": []}}],
            "conditions": [], "model_records": [], "calculation_records": [],
            "versions": [{"version_id": "r1", "primary_candidate_id": "c1", "summary_decision": main_decision,
                          "body_decision": copy.deepcopy(main_decision)}]}


def row(**changes):
    value = {"row_id": "totals", "source_id": "page", "market": "totals", "sequence": "image-page",
             "row_order": 0, "quote_time": iso(NOW + timedelta(minutes=20)), "time_precision": "second",
             "observed_at": iso(NOW + timedelta(minutes=21)), "status": "open", "odds_format": "decimal",
             "line": 2.5, "odds": {"over": 1.95, "under": 1.95}, "settlement_basis": "REGULATION"}
    value.update(changes)
    return value


class SyntheticClient:
    requests_made = 0

    def status(self):
        return {}

    def resolve_league(self, competition):
        return {"league": {"id": 999}, "seasons": [{"year": 2024, "start": "2024-01-01"}]}

    def fetch_fixtures(self, league_id, season, **kwargs):
        rng, rows = np.random.default_rng(726), []
        for i in range(181):
            future = i == 180
            kickoff = KICKOFF if future else NOW - timedelta(days=190-i)
            home = i % 6 + 1
            away = (home + 1 + (i // 6) % 5 - 1) % 6 + 1
            rows.append({"fixture": {"id": 2000 if future else i+1, "date": iso(kickoff),
                                      "timestamp": int(kickoff.timestamp()), "status": {"short": "NS" if future else "FT"}},
                         "league": {"id": 999, "season": 2024},
                         "teams": {"home": {"id": home, "name": f"Synthetic {home}"},
                                   "away": {"id": away, "name": f"Synthetic {away}"}},
                         "score": {"fulltime": {"home": None if future else int(rng.poisson(1.6)),
                                                  "away": None if future else int(rng.poisson(1.1))}}})
        return {"response": rows, "fetched_at": iso(NOW)}


class PretestTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base = tempfile.TemporaryDirectory()
        cls.config = {"history_seasons": 1, "max_requests_per_run": 80, "half_life_days": 180,
                      "ridge": 3, "min_train": 60, "min_test": 15, "forecast_days": 7,
                      "max_model_age_days": 14,
                      "competitions": [{"key": "epl", "label": "合成联赛", "name": "Synthetic League",
                                        "country": "Test", "mode": "domestic_baseline"}]}
        with patch.dict("os.environ", {"GITHUB_SHA": SOURCE}):
            code, report = run_pipeline(cls.config, cls.base.name, "train", now=NOW,
                                        clock=lambda: NOW, client=SyntheticClient())
        if code:
            raise AssertionError(report)

    @classmethod
    def tearDownClass(cls):
        cls.base.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.write("page.txt", b"synthetic observed page: no real match or result")
        self.write("rows.json", [row()])
        self.write("decision.json", archive())
        self.frozen_files = [{"source_id": "page", "path": "page.txt", "role": "source",
                              "observed_at": iso(NOW + timedelta(minutes=21)), "published_at": None},
                             {"source_id": "rows", "path": "rows.json", "role": "source",
                              "observed_at": iso(NOW + timedelta(minutes=21)), "published_at": None}]
        review = {"row_id": "totals", "source_kind": "sourced_page", "whole_visible_history_reviewed": True,
                  "whole_row_and_odds_format_verified": True, "evidence_refs": ["page"]}
        self.record = {"schema_version": "match-archive-1.0", "match_id": "2000", "match_key": "synthetic:2000",
                       "version_id": "r1", "home_id": 1, "away_id": 2,
                       "analysis_cutoff": iso(CUTOFF), "kickoff_at": iso(KICKOFF),
                       "pretest_checks": {
                           "evidence_bundle": {"path": "frozen", "completeness_status": "declared_complete",
                                               "required_source_ids": ["page", "rows"],
                                               "content_review": {"status": "reviewed", "no_result_content": True,
                                                                  "evidence_refs": ["page"]}},
                           "model": {"status": "unavailable", "reason": "synthetic qualitative fallback"},
                           "freshness": {"coverage_status": "unknown"},
                           "quotes": {"rows": self.ref("rows.json"), "row_reviews": [review], "selected_row_ids": ["totals"]},
                           "semantic_review": {"decision_archive": self.ref("decision.json"), "status": "reviewed",
                                               "evidence_refs": ["page"], "mechanism_supports_payment": True,
                                               "strongest_counterevidence_handled": True, "nearest_alternative_compared": True,
                                               "all_candidates_match_body": True, "conditions_not_reminders": True},
                           "save": {"artifacts": [self.ref("page.txt"), self.ref("decision.json")],
                                    "capacity": {"state_root": "saved", "hard_limit_bytes": 1048576, "reserve_bytes": 1024}},
                           "publication": {"status": "unknown"}, "execution": {"status": "unknown"}}}
        (self.root / "saved").mkdir()
        self.write("saved/local.json", {"synthetic": True})
        self.clock = patch("football_model.pretest._now", return_value=CHECKED)
        self.clock.start()
        self.freeze()
        self.build_views()

    def tearDown(self):
        self.clock.stop()
        self.temp.cleanup()

    def write(self, name, content):
        target = self.root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content if isinstance(content, bytes) else json.dumps(content, ensure_ascii=False).encode())

    def ref(self, name):
        return {"path": name, "sha256": hashlib.sha256((self.root / name).read_bytes()).hexdigest()}

    def freeze(self):
        output = self.root / "frozen"
        if output.exists():
            shutil.rmtree(output)
        with patch("football_model.evidence._now", return_value=CUTOFF + timedelta(minutes=1)):
            receipt = freeze_bundle(self.root, output,
                {"match_key": "synthetic:2000", "version_id": "r1", "analysis_cutoff": iso(CUTOFF),
                 "kickoff_at": iso(KICKOFF), "future_information_exposure": False, "files": self.frozen_files})
        self.record["pretest_checks"]["evidence_bundle"]["manifest_sha256"] = receipt["manifest_sha256"]

    def build_views(self):
        candidate = archive()["candidates"][0]
        candidate.pop("contract_id")
        candidate.pop("quote_id")
        candidate.pop("fit")
        candidate.update(quote_row_id="totals", selection="under", formal_fit={"single": "适合", "parlay_leg": "适合"})
        decision_input = {"schema_version": "s2-decision-1.0", "match_id": "2000", "decision_id": "synthetic-d1",
            "version_id": "r1", "policy_id": "framework-v1.7-r5", "analysis_cutoff": iso(CUTOFF),
            "kickoff_at": iso(KICKOFF), "future_information_exposure": False,
            "inputs": [{"input_id": "i-page", "source_id": "page", **self.ref("page.txt"),
                        "observed_at": iso(NOW + timedelta(minutes=21)), "published_at": None,
                        "source_claimed_published_at": None}], "derived_artifacts": [],
            "archive": {"sources": [{"source_id": "page"}], "facts": [], "contracts": [], "candidates": [candidate],
                        "conditions": [], "model_records": [], "calculation_records": []},
            "quote_rows": [row()], "primary_candidate_id": "c1", "forecast_id": None,
            "forecast_missing_reason": "Synthetic engineering only", "shadow_grading": None,
            "decision_chain": {"strongest_alternative_candidate_id": None,
                               "no_alternative_reason": "Synthetic single-candidate engineering fixture",
                               "alternative_reason": "Not a real football conclusion"}}
        with patch("football_model.decision_record._now", return_value=CUTOFF + timedelta(minutes=2)):
            receipt = build_decision_bundle(decision_input, self.root / "decision-bundle", source_root=self.root)
        if receipt["status"] != "structural_checks_passed":
            raise AssertionError(receipt)
        views = json.loads((self.root / "decision-bundle/audit_archives.json").read_bytes())
        self.write("decision.json", views["single"])
        semantic = self.record["pretest_checks"]["semantic_review"]
        semantic["decision_archive"] = self.ref("decision.json")
        semantic["decision_bundle"] = {"path": "decision-bundle", "manifest_sha256": receipt["manifest_sha256"]}
        self.record["pretest_checks"]["save"]["artifacts"] = [self.ref("page.txt"), self.ref("decision.json")]

    def assess(self, mode="ordinary"):
        return assess_pretest(self.record, source_root=self.root, mode=mode)

    def codes(self, mode="ordinary"):
        return {f["code"] for f in self.assess(mode)["findings"]}

    def add_model(self):
        shutil.copytree(self.base.name, self.root / "runtime")
        self.write("config.json", self.config)
        generated = NOW + timedelta(minutes=10)
        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return generated
        with patch("football_model.integration.datetime", Clock):
            packet = match_packet(self.root / "runtime", self.config, "epl", 2000,
                                  as_of=iso(generated), state_commit=STATE)
        self.write("packet.json", packet)
        self.write("receipt.json", {"argv": ["python", "-m", "football_model.integration", "match",
                                               "--runtime", "runtime", "--league", "epl", "--match-id", "2000"],
                                    "exit_code": 0, "started_at": iso(generated),
                                    "completed_at": iso(generated + timedelta(seconds=1)),
                                    "packet_sha256": self.ref("packet.json")["sha256"],
                                    "source_commit": "c" * 40, "state_commit": STATE})
        for i, target in enumerate(sorted((self.root / "runtime").rglob("*.json")) + [self.root / "config.json"]):
            self.frozen_files.append({"source_id": "runtime-" + str(i), "path": str(target.relative_to(self.root)),
                                      "role": "model", "observed_at": iso(NOW), "published_at": None})
        self.record["pretest_checks"]["model"] = {"status": "applicable", "packet": self.ref("packet.json"),
            "execution_receipt": self.ref("receipt.json"), "source_commit": "c" * 40,
            "prediction_source_commit": SOURCE, "state_commit": STATE, "runtime_path": "runtime",
            "config": self.ref("config.json"), "league": "epl",
            "actions": {"status": "checked", "checked_at": iso(NOW), "failure_scope": "none", "evidence_refs": ["page"]}}
        matches = json.loads((self.root / "runtime/data/epl.json").read_bytes())["matches"]
        teams = [{"team_id": t, "expected_match_ids": [m["match_id"] for m in matches if t in (m["home_id"], m["away_id"])],
                  "evidence_refs": ["page"]} for t in (1, 2)]
        self.record["pretest_checks"]["freshness"] = {
            "coverage_status": "complete", "scope": "model_supported_competition_and_requested_seasons",
            "expected_schedule_source_ids": ["page"], "training_data": self.ref("runtime/data/epl.json"), "teams": teams}
        self.freeze()
        return packet

    def test_no_model_is_qualitative_local_fallback_not_veto(self):
        result = self.assess()
        self.assertEqual(result["status"], "ready_with_local_fallbacks")
        self.assertTrue(result["qualitative_model_absence_is_not_veto"])
        self.assertEqual(result["numerical_use"], "unquantified_or_disclosed_historical_only")
        self.assertIsNone(result["details"]["execution"]["actual_profit"])

    def test_flow_pilot_may_continue_without_model(self):
        self.assertNotEqual(self.assess("flow_pilot")["status"], "blocked")

    def test_actual_original_function_recomputes_synthetic_snapshot(self):
        self.add_model()
        result = self.assess("flow_pilot")
        self.assertEqual(result["status"], "engineering_checks_passed", result)
        self.assertTrue(result["details"]["model"]["integration_original_function_rerun"])
        self.assertEqual(result["details"]["freshness"]["status"], "complete")
        self.assertNotEqual(result["details"]["model"]["prediction_source_commit"],
                            result["details"]["model"]["declared_recomputation_source_commit"])
        self.assertFalse(result["details"]["model"]["actual_runtime_source_commit_verified"])
        self.assertEqual(result["details"]["model"]["actual_runtime_implementation_sha256"]["model.py"],
                         hashlib.sha256(Path("football_model/model.py").read_bytes()).hexdigest())
        self.assertFalse(result["new_match_runtime_acceptance_observed"])

    def test_frozen_bytes_tampering_blocks_strict_not_plain_qualitative(self):
        (self.root / "frozen/originals/page.txt").write_bytes(b"modified")
        self.assertNotEqual(self.assess()["status"], "blocked")
        self.assertEqual(self.assess("flow_pilot")["status"], "blocked")

    def test_hash_without_independent_anchor_is_not_acceptance(self):
        del self.record["pretest_checks"]["evidence_bundle"]["manifest_sha256"]
        self.assertIn("evidence_unverified", self.codes("flow_pilot"))

    def test_required_missing_input_is_not_silently_complete(self):
        self.record["pretest_checks"]["evidence_bundle"]["required_source_ids"].append("missing")
        self.assertIn("evidence_unverified", self.codes("flow_pilot"))

    def test_rows_changed_with_updated_digest_still_must_match_frozen(self):
        self.write("rows.json", [row(line=3.5)])
        self.record["pretest_checks"]["quotes"]["rows"] = self.ref("rows.json")
        self.assertIn("quotes_unverified", self.codes())

    def test_api_ah_isolation_cannot_be_overridden_by_handwritten_screenshot_review(self):
        api_row = row(row_id="api_row_1", market="asian_handicap", odds={"home": 1.95, "away": 1.95},
                      line=-0.5, image_id="image")
        self.write("image.png", b"synthetic bytes, not a real screenshot")
        self.frozen_files.append({"source_id": "image", "path": "image.png", "role": "screenshot",
                                  "observed_at": iso(NOW), "published_at": None})
        self.write("rows.json", [api_row])
        quotes = self.record["pretest_checks"]["quotes"]
        quotes["rows"] = self.ref("rows.json")
        quotes["row_reviews"] = [{**quotes["row_reviews"][0], "row_id": "api_row_1", "source_kind": "screenshot",
                                  "second_image_reviewed": True, "sign_independently_verified": True,
                                  "sign_evidence_refs": ["image"]}]
        quotes["selected_row_ids"] = []
        self.freeze()
        result = self.assess()
        self.assertIn("api_ah_sign_unverified_isolated", {f["code"] for f in result["findings"]})
        self.assertEqual(result["details"]["quotes"]["usable_rows"], [])
        quotes["selected_row_ids"] = ["api_row_1"]
        self.assertEqual(self.assess()["status"], "blocked")

    def test_away_handicap_contract_is_correctly_negated(self):
        self.write("rows.json", [row(market="asian_handicap", odds={"home": 1.95, "away": 1.95}, line=0.5)])
        self.record["pretest_checks"]["quotes"]["rows"] = self.ref("rows.json")
        self.freeze()
        contracts = self.assess()["details"]["quotes"]["usable_rows"][0]["contracts"]
        self.assertEqual(next(c["line"] for c in contracts if c["selection"] == "away"), -0.5)

    def test_newer_closed_row_is_not_replaced_with_older_numbers(self):
        self.write("rows.json", [row(status="closed"), row(row_id="old", row_order=1,
                         quote_time=iso(NOW + timedelta(minutes=19)))])
        self.record["pretest_checks"]["quotes"]["rows"] = self.ref("rows.json")
        self.record["pretest_checks"]["quotes"]["selected_row_ids"] = []
        self.freeze()
        result = self.assess()
        self.assertEqual(result["details"]["quotes"]["usable_rows"], [])
        self.assertEqual(result["details"]["quotes"]["selection"]["groups"][0]["status"], "closed")

    def test_minute_boundary_is_ambiguous_and_does_not_fall_back(self):
        self.write("rows.json", [row(quote_time="2025-01-01T01:00+00:00", time_precision="minute"),
                                 row(row_id="old", row_order=1, quote_time=iso(NOW))])
        self.record["pretest_checks"]["quotes"]["rows"] = self.ref("rows.json")
        self.record["pretest_checks"]["quotes"]["selected_row_ids"] = []
        self.freeze()
        result = self.assess()
        self.assertEqual(result["details"]["quotes"]["selection"]["groups"][0]["status"], "ambiguous_at_cutoff")
        self.assertEqual(result["details"]["quotes"]["usable_rows"], [])

    def test_price_observed_after_cutoff_is_not_treated_as_current_input(self):
        self.write("rows.json", [row(observed_at=iso(CUTOFF + timedelta(minutes=1)))])
        self.record["pretest_checks"]["quotes"]["rows"] = self.ref("rows.json")
        self.record["pretest_checks"]["quotes"]["selected_row_ids"] = []
        self.freeze()
        self.assertEqual(self.assess()["details"]["quotes"]["usable_rows"], [])

    def test_latest_other_league_failure_does_not_veto_target(self):
        self.add_model()
        self.record["pretest_checks"]["model"]["actions"]["failure_scope"] = "other_leagues_only"
        self.assertEqual(self.assess()["details"]["model"]["status"], "recomputed")

    def test_latest_target_failure_removes_numeric_permission(self):
        self.add_model()
        self.record["pretest_checks"]["model"]["actions"]["failure_scope"] = "target_league"
        result = self.assess("flow_pilot")
        self.assertIn("model_not_currently_verified", {f["code"] for f in result["findings"]})
        self.assertNotEqual(result["status"], "blocked")

    def test_actual_training_digest_required_not_file_mtime(self):
        self.add_model()
        data = json.loads((self.root / "runtime/data/epl.json").read_bytes())
        data["matches"][0]["home_goals"] += 1
        self.write("runtime/data/epl.json", data)
        self.record["pretest_checks"]["freshness"]["training_data"] = self.ref("runtime/data/epl.json")
        self.freeze()
        result = self.assess()
        self.assertEqual(result["details"]["freshness"]["status"], "unknown")
        self.assertEqual(result["details"]["freshness"]["reason"], "training_rows_do_not_match_actual_fitted_model")
        self.assertFalse(result["details"]["freshness"]["file_mtime_used"])

    def test_expected_match_missing_is_explicit_not_filled_zero(self):
        self.add_model()
        self.record["pretest_checks"]["freshness"]["teams"][0]["expected_match_ids"].append(999)
        result = self.assess()
        self.assertEqual(result["details"]["freshness"]["teams"][0]["missing_match_ids"], [999])
        self.assertIn("expected_matches_not_in_model", self.codes())

    def test_unknown_expected_scope_remains_unknown_even_with_actual_model(self):
        self.add_model()
        self.record["pretest_checks"]["freshness"]["coverage_status"] = "unknown"
        self.assertEqual(self.assess()["details"]["freshness"]["status"], "unknown")

    def test_empty_expected_ids_cannot_self_certify_complete_training_scope(self):
        self.add_model()
        for team in self.record["pretest_checks"]["freshness"]["teams"]:
            team["expected_match_ids"] = []
        result = self.assess("flow_pilot")
        self.assertEqual(result["details"]["freshness"]["status"], "unknown")
        self.assertEqual(result["numerical_use"], "unquantified_or_disclosed_historical_only")
        self.assertNotEqual(result["status"], "engineering_checks_passed")
        self.assertNotIn("expected_scope_is_human_verified", result["details"]["freshness"])

    def test_partial_expected_ids_cannot_claim_full_requested_scope(self):
        self.add_model()
        self.record["pretest_checks"]["freshness"]["teams"][0]["expected_match_ids"] = [1]
        result = self.assess()
        self.assertEqual(result["details"]["freshness"]["status"], "unknown")
        self.assertEqual(result["details"]["freshness"]["reason"],
                         "declared_complete_expected_scope_omits_actual_training_matches")

    def test_packet_provenance_flag_alone_cannot_replace_actual_original_inputs(self):
        self.add_model()
        self.frozen_files = [f for f in self.frozen_files if f["path"] != "runtime/models/epl.json"]
        self.freeze()
        self.assertEqual(self.assess()["details"]["model_error"], "integration_raw_input_not_in_frozen_allowlist")

    def test_integration_receipt_sha_mismatch_is_not_real_recompute_evidence(self):
        self.add_model()
        receipt = json.loads((self.root / "receipt.json").read_bytes())
        receipt["packet_sha256"] = "d" * 64
        self.write("receipt.json", receipt)
        self.record["pretest_checks"]["model"]["execution_receipt"] = self.ref("receipt.json")
        self.assertIn("model_not_currently_verified", self.codes())

    def test_semantic_archive_version_and_cutoff_are_bound(self):
        wrong = archive()
        wrong["versions"][0]["version_id"] = "r2"
        self.write("decision.json", wrong)
        self.record["pretest_checks"]["semantic_review"]["decision_archive"] = self.ref("decision.json")
        self.assertIn("body_and_contract_unverified", self.codes("flow_pilot"))
        wrong = archive()
        wrong["analysis_cutoff"] = iso(NOW)
        self.write("decision.json", wrong)
        self.record["pretest_checks"]["semantic_review"]["decision_archive"] = self.ref("decision.json")
        self.assertIn("body_and_contract_unverified", self.codes("flow_pilot"))

    def test_structural_contradiction_blocks_even_ordinary(self):
        wrong = archive()
        wrong["versions"][0]["summary_decision"]["fit"] = "不建议"
        self.write("decision.json", wrong)
        self.record["pretest_checks"]["semantic_review"]["decision_archive"] = self.ref("decision.json")
        self.assertEqual(self.assess()["status"], "blocked")

    def test_semantic_attestation_is_not_authenticated_truth(self):
        result = self.assess()
        self.assertFalse(result["human_attestations_independently_authenticated"])
        self.assertFalse(result["predictive_quality_verified"])

    def test_save_readback_hash_tamper_blocks_flow_acceptance(self):
        self.write("page.txt", b"tampered actual save")
        self.assertIn("save_readback_not_verified", self.codes("flow_pilot"))
        self.assertEqual(self.assess("flow_pilot")["status"], "blocked")

    def test_capacity_measures_bytes_not_declared_used(self):
        capacity = self.record["pretest_checks"]["save"]["capacity"]
        capacity.update(hard_limit_bytes=100, reserve_bytes=20, used_bytes=0)
        self.write("saved/big.txt", b"x" * 100)
        result = self.assess("flow_pilot")
        self.assertGreater(result["details"]["capacity"]["measured_bytes"], 100)
        self.assertEqual(result["status"], "blocked")

    def test_native_capacity_invalid_file_is_unknown_not_unhandled_exception(self):
        self.write("saved/invalid.bin", b"not an allowed state file")
        result = self.assess("flow_pilot")
        self.assertEqual(result["details"]["capacity"]["status"], "unknown")
        self.assertEqual(result["status"], "blocked")

    def test_unknown_growth_does_not_become_365_day_capacity_proof(self):
        result = self.assess()
        self.assertIsNone(result["details"]["capacity"]["long_cycle_supported"])
        self.assertIn("long_cycle_capacity_not_demonstrated", self.codes("f5_confirmation"))

    def test_publication_and_execution_claims_remain_unknown_without_events(self):
        self.record["pretest_checks"]["publication"] = {"status": "published", "occurred_at": iso(NOW)}
        self.record["pretest_checks"]["execution"] = {"status": "accepted", "decimal_odds": 1.95}
        result = self.assess()
        self.assertEqual(result["details"]["publication"]["status"], "unknown")
        self.assertEqual(result["details"]["execution"]["status"], "unknown")
        self.assertEqual(result["actual_execution_status"], "unknown")

    def test_framework_comparison_needs_frozen_independent_policy_plan(self):
        self.assertEqual(self.assess("framework_comparison")["status"], "blocked")
        self.assertIn("comparison_plan_not_ready", self.codes("framework_comparison"))

    def test_f5_prepared_or_boolean_activated_claim_is_not_activation(self):
        self.record["pretest_checks"]["confirmation_protocol"] = {"status": "activated", "protocol_id": "fake"}
        self.assertEqual(self.assess("f5_confirmation")["status"], "blocked")
        self.assertIn("confirmation_protocol_not_ready", self.codes("f5_confirmation"))

    def test_after_kickoff_cannot_be_registered_as_prematch(self):
        with patch("football_model.pretest._now", return_value=KICKOFF):
            self.assertEqual(self.assess()["status"], "blocked")
            self.assertIn("not_current_prematch", self.codes())

    def test_unsafe_paths_and_naive_cutoff_rejected(self):
        self.record["analysis_cutoff"] = "2025-01-01T01:00:00"
        with self.assertRaises(PretestError):
            self.assess()

    def test_cli_saves_true_output_readback_and_never_overwrites(self):
        self.write("record.json", self.record)
        output = self.root / "audit.json"
        args = ["--record", str(self.root / "record.json"), "--source-root", str(self.root), "--output", str(output)]
        stream = io.StringIO()
        with redirect_stdout(stream):
            self.assertEqual(main(args), 0)
        receipt = json.loads(stream.getvalue())
        self.assertEqual(receipt["output_sha256"], hashlib.sha256(output.read_bytes()).hexdigest())
        self.assertTrue(receipt["output_readback_verified"])
        original = output.read_bytes()
        with redirect_stdout(io.StringIO()):
            self.assertEqual(main(args), 2)
        self.assertEqual(output.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
