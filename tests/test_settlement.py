import unittest
import numpy as np
from football_model.settlement import contract, probabilities, settlement, structural_states, unit_return


class SettlementTests(unittest.TestCase):
    def test_quarter_goals_payoff_boundaries_and_away_orientation(self):
        checks = [
            ({"market": "total_goals", "side": "over", "line": 2.25}, 1, 1, "half_loss"),
            ({"market": "total_goals", "side": "over", "line": 2.75}, 2, 1, "half_win"),
            ({"market": "total_goals", "side": "under", "line": 2.25}, 1, 1, "half_win"),
            ({"market": "asian_handicap", "side": "home", "line": -0.25}, 1, 1, "half_loss"),
            ({"market": "asian_handicap", "side": "away", "line": 0.25}, 1, 1, "half_win"),
            ({"market": "asian_handicap", "side": "home", "line": -1.0}, 2, 1, "push"),
            ({"market": "asian_handicap", "side": "away", "line": -0.75}, 0, 1, "half_win"),
        ]
        for value, home, away, expected in checks:
            self.assertEqual(settlement(home, away, value), expected)
        self.assertAlmostEqual(unit_return("half_win", 1.8, .01), .39)
        self.assertAlmostEqual(unit_return("half_loss", 1.8, .01), -.51)

    def test_structural_support_not_inferred_from_matrix_zeros(self):
        line = {"market": "total_goals", "side": "over", "line": 2.25}
        result = probabilities([[1.0]], line)
        self.assertEqual(result["possible_states"], ["win", "half_loss", "loss"])
        self.assertEqual(result["structural_zero_states"], ["half_win", "push"])
        self.assertEqual(result["probabilities"]["win"], 0)
        self.assertEqual(structural_states({"market": "total_goals", "side": "over", "line": 0}), ("win", "push"))

    def test_opposite_contracts_partition_returns_at_fair_even_odds(self):
        for quarter in range(-13, 14):
            for home in range(5):
                for away in range(5):
                    first = settlement(home, away, {"market": "asian_handicap", "side": "home", "line": quarter / 4})
                    second = settlement(home, away, {"market": "asian_handicap", "side": "away", "line": -quarter / 4})
                    self.assertEqual(unit_return(first, 2), -unit_return(second, 2))

    def test_nonregulation_and_live_contract_fields_rejected(self):
        for extra in ({"settlement_basis": "first_half"}, {"start_score": [1, 0]}, {"period": "live"}):
            with self.assertRaises(ValueError):
                contract({"market": "total_goals", "side": "over", "line": 2.5, **extra})

    def test_invalid_contract_and_matrix(self):
        for line in (0.1, float("nan"), True, 1000):
            with self.assertRaises(ValueError):
                contract({"market": "asian_handicap", "side": "home", "line": line})
        for matrix in ([[float("nan")]], [[-.1, 1.1]], [[.2]], [1.0]):
            with self.assertRaises(ValueError):
                probabilities(matrix, {"market": "total_goals", "side": "over", "line": 2.5})
