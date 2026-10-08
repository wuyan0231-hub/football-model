"""Synthetic legacy API fixtures: replay preservation is not new scoring admission."""
import hashlib
import json
from unittest.mock import patch
import unittest

import football_model.decision_record as decisions
from football_model.decision_scoring import _score_decision, read_decision_score
import test_decision_scoring as scoring_helpers


class ApiAhScoringQuarantineTests(unittest.TestCase):
    setUp = scoring_helpers.DecisionScoringTests.setUp
    tearDown = scoring_helpers.DecisionScoringTests.tearDown
    t = scoring_helpers.DecisionScoringTests.t
    register = scoring_helpers.DecisionScoringTests.register
    external = scoring_helpers.DecisionScoringTests.external
    score = scoring_helpers.DecisionScoringTests.score

    def build_legacy_api(self):
        row = self.record["quote_rows"][0]
        row.update(market="asian_handicap", line=-0.25,
                   odds={"home": 1.85, "away": 2.05}, status="open",
                   collector_version="quote-collection-v1", sequence="api-football:8:4:-0.25",
                   market_id=4, market_name="Asian Handicap",
                   line_mapping_status="empirical_inference_pending_provider_confirmation")
        self.record["archive"]["candidates"][0]["selection"] = "home"
        compile_current = decisions._compile
        with patch.object(decisions, "_output_policy", return_value={}), patch.object(
                decisions, "_compile", side_effect=lambda record, entries, issues:
                compile_current(record, entries, issues, legacy_api_ah_replay=True)):
            scoring_helpers.DecisionScoringTests.build(self)
        # This is a newly manufactured engineering fixture of the pre-policy
        # envelope; no user original is modified or presented as historical.
        (self.bundle / "output_policy.json").unlink()
        manifest = json.loads((self.bundle / "manifest.json").read_text())
        manifest["files"] = [x for x in manifest["files"] if x["path"] != "output_policy.json"]
        blob = decisions._bytes(manifest)
        (self.bundle / "manifest.json").write_bytes(blob)
        self.built["manifest_sha256"] = hashlib.sha256(blob).hexdigest()
        self.assertEqual(decisions.verify_decision_bundle(
            self.bundle, self.built["manifest_sha256"])["status"], "verified")

    def test_old_open_api_adopted_ticket_cannot_get_new_reference_open_or_actual_scores(self):
        for quote_protocol in ("QUOTE_REF", "QUOTE_OPEN", "QUOTE_ACTUAL"):
            with self.subTest(quote_protocol=quote_protocol):
                self.spec["quote_protocol"] = quote_protocol
                self.spec["name"] = "synthetic-api-ah-" + quote_protocol
                self.protocol = self.register()
                self.build_legacy_api()
                result = self.score()
                self.assertEqual(result["outcome"]["status"], "not_available")
                self.assertIn("api_asian_handicap_mapping_not_certified", result["unavailable_reasons"])
                self.assertFalse(result["return_eligible"])
                self.assertIsNone(result["contract"])
                self.assertIsNone(result["price_decimal"])
                self.assertTrue(all(result["outcome"][k] is None for k in (
                    "paper_reference_return", "open_simulated_return", "actual_single_return")))

    def test_old_sealed_score_replays_but_is_explicitly_unavailable_for_new_api_admission(self):
        self.build_legacy_api()
        with patch.object(scoring_helpers, "score_decision", side_effect=lambda *args, **kwargs:
                          _score_decision(*args, **kwargs, _legacy_api_ah_score_replay=True)):
            old = self.score(output_dir=self.root / "legacy-score")
        readback = read_decision_score(old["output_dir"], old["manifest_sha256"])
        self.assertEqual(readback["verification_status"], "verified")
        self.assertTrue(readback["historical_api_ah_mapping_unverified"])
        self.assertFalse(readback["new_api_ah_admission_allowed"])
        self.assertNotIn("api_ah_scoring_policy_version", readback)
        fresh = self.score()
        self.assertIn("api_asian_handicap_mapping_not_certified", fresh["unavailable_reasons"])

    def test_screenshot_scoring_remains_available_and_readback_preserves_new_policy(self):
        scoring_helpers.DecisionScoringTests.build(self)
        score = self.score(output_dir=self.root / "new-score")
        self.assertEqual(score["api_ah_scoring_policy_version"], "api-ah-scoring-quarantine-v1")
        self.assertAlmostEqual(score["outcome"]["paper_reference_return"], 0.95)
        self.assertEqual(read_decision_score(score["output_dir"], score["manifest_sha256"])[
            "verification_status"], "verified")


if __name__ == "__main__":
    unittest.main()
