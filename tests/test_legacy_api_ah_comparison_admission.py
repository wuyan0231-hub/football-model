"""Consumer-boundary regressions using authentic synthetic trial receipts.

The sealed-score reader's legacy flags are injected at its boundary; they are
not rewritten into raw scores or claimed to be football observations.
"""
from copy import deepcopy
import unittest
from unittest.mock import patch

import football_model.decision_trials as trials
import test_decision_trials as trial_helpers
import test_policy_evaluation as policy_helpers


class LegacyApiAhTrialAdmissionTests(unittest.TestCase):
    setUp = trial_helpers.DecisionTrialsTests.setUp
    tearDown = trial_helpers.DecisionTrialsTests.tearDown
    dt = trial_helpers.DecisionTrialsTests.dt
    t = trial_helpers.DecisionTrialsTests.t
    source = trial_helpers.DecisionTrialsTests.source
    run_record = trial_helpers.DecisionTrialsTests.run_record
    submit = trial_helpers.DecisionTrialsTests.submit
    score = trial_helpers.DecisionTrialsTests.score

    def test_legacy_reader_flags_block_current_returns_and_probability_pairs(self):
        left, right = self.submit("left"), self.submit("right", seconds=410)
        refs = [self.score("left", left), self.score("right", right)]
        reader = trials.read_decision_score
        def flagged(*args):
            result = reader(*args)
            result.update(historical_api_ah_mapping_unverified=True, new_api_ah_admission_allowed=False)
            result["probability"]["metrics"]["one_x_two"] = {"brier_score": .6, "log_loss": 1.0}
            return result
        with patch.object(trials, "read_decision_score", side_effect=flagged):
            report = trials.compare_trials(self.state, self.pid, refs)
        row = report["rows"][0]
        self.assertIn("api_asian_handicap_mapping_not_certified", row["return_comparison_reasons"])
        self.assertIn("api_asian_handicap_mapping_not_certified", row["probability_comparison_reasons"])
        self.assertAlmostEqual(row["sides"]["left"]["historical_ticket_return"], .95)
        self.assertIsNone(row["sides"]["left"]["ticket_return"])
        self.assertIsNone(row["sides"]["left"]["strategy_return"])
        for group in ("prospective", "research"):
            for metric in report["metrics"][group].values():
                self.assertEqual(metric["paired_rows"], 0)


class LegacyApiAhPolicyAdmissionTests(unittest.TestCase):
    setUp = policy_helpers.PolicyEvaluationTests.setUp
    tearDown = policy_helpers.PolicyEvaluationTests.tearDown
    register = policy_helpers.PolicyEvaluationTests.register
    compare = policy_helpers.PolicyEvaluationTests.compare

    def test_three_quote_protocols_keep_historical_values_without_current_numeric_pairs(self):
        fields = {"QUOTE_REF": "paper_reference_return", "QUOTE_OPEN": "open_simulated_return",
                  "QUOTE_ACTUAL": "actual_single_return"}
        for protocol, field in fields.items():
            with self.subTest(protocol=protocol):
                self.spec["quote_protocol"] = protocol
                for score in self.scores.values():
                    score["quote_protocol"] = protocol
                    score["outcome"][field] = .95
                self.scores["left"].update(historical_api_ah_mapping_unverified=True,
                                            new_api_ah_admission_allowed=False)
                report = self.compare()
                row = report["rows"][0]
                self.assertAlmostEqual(row["left"]["historical_ticket_return"], .95)
                self.assertIsNone(row["left"]["ticket_return"])
                self.assertIsNone(row["left"]["strategy_return"])
                self.assertTrue(all(v is None for v in row["left"]["metrics"].values()))
                self.assertFalse(row["comparability"]["probability"])
                self.assertFalse(row["comparability"]["returns"])
                for layer in ("probability", "returns"):
                    self.assertIn("api_asian_handicap_mapping_not_certified",
                                  row["comparability"]["reasons"][layer])
                for group in ("paired_metrics_all_descriptive_only", "prospective_paired_metrics", "research_paired_metrics"):
                    for metric in report[group].values():
                        self.assertEqual(metric["paired_rows"], 0)


if __name__ == "__main__":
    unittest.main()
