"""Synthetic temporal/admission checks; these fixtures are not research evidence."""
import copy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

from scripts import cold_start_past_masking as study


def protocol():
    # Public unit tests do not load the withheld real experiment configuration.
    # This synthetic fixture preserves algorithm gates, not real source identity.
    value = json.loads((Path(__file__).resolve().parent / "fixtures" /
        "cold-start-masking-synthetic-protocol-v1.json").read_text())
    assert value["purpose"] == "synthetic_unit_test_only_not_actual_experiment_protocol"
    assert value["source_ref"]["sha256"] == "0" * 64
    return value


def record(i, day, h=1, a=2):
    return {"match_id": i, "kickoff": day.isoformat(), "home_id": h, "away_id": a,
        "home_name": f"T{h}", "away_name": f"T{a}", "home_goals": i % 3,
        "away_goals": (i+1) % 3, "neutral": False}


def history_row(i, block, *, available="2025-02-01T04:00:00Z", duplicate=False):
    point = {"scoreline_nll": 2.0}
    return {"scenario_id": f"b{block}:m{i}:{duplicate}", "block": block, "match_id": i,
        "training_cutoff": f"2025-0{block+1}-01T00:00:00Z",
        "target_assumed_available_at": available, "point_metrics": point,
        "trials": [{"tau": t, "metrics": {"scoreline_nll": (1.0 if t == .5 else 3.0)}}
            for t in [0.0, .5, 1.0, 2.0]]}


class NeverReadOutcome(dict):
    def __getitem__(self, key):
        if key in ("home_goals", "away_goals"):
            raise AssertionError("excluded outcome accessed")
        return super().__getitem__(key)


class PastMaskingTests(unittest.TestCase):
    def test_exposed_and_future_outcomes_not_projected(self):
        p = protocol()
        old = record(1, datetime(2025, 1, 1, tzinfo=timezone.utc))
        exposed = NeverReadOutcome(record(p["exposed_current_match_ids"][0], datetime(2025, 1, 1, tzinfo=timezone.utc)))
        future = NeverReadOutcome(record(2, datetime(2025, 8, 16, tzinfo=timezone.utc)))
        records, exclusions = study.past_records({"matches": [old, exposed, future]}, p)
        self.assertEqual([r["match_id"] for r in records], [1])
        self.assertEqual(len(exclusions), 2)

    def test_original_gate_and_tau_grid_cannot_be_lowered(self):
        p = protocol(); study.validate_protocol(p)
        for key, value in (("baseline_min_train", 179), ("tau_grid", [0, 1]),
                ("learning_end", "2026-01-01T00:00:00Z"), ("inner_min_blocks", 1)):
            q = copy.deepcopy(p); q[key] = value
            with self.assertRaises(ValueError): study.validate_protocol(q)

    def test_all_held_team_training_matches_removed_and_blocks_past_only(self):
        p = protocol(); start = datetime(2024, 8, 1, tzinfo=timezone.utc)
        source = {"matches": [record(i+1, start+timedelta(days=i), 1+i % 20, 1+(i+1) % 20) for i in range(360)]}
        records, _ = study.past_records(source, p)
        plan = study.inventory(records, p); self.assertEqual(plan["status"], "ready")
        lookup = {r["match_id"]: r for r in records}
        for block in plan["blocks"]:
            cutoff = study._timestamp(block["training_cutoff"], "test cutoff")
            for mask in block["masks"]:
                team = mask["held_team_id"]
                for i in mask["retained_training_match_ids"]:
                    r = lookup[i]
                    self.assertNotIn(team, (r["home_id"], r["away_id"]))
                    self.assertLessEqual(r["_dt"]+timedelta(hours=4), cutoff)
                for i in mask["removed_match_ids"]:
                    self.assertIn(team, (lookup[i]["home_id"], lookup[i]["away_id"]))
                if mask["inventory_eligible"]:
                    self.assertGreaterEqual(mask["retained_training_count"], 180)
                    self.assertEqual(len(mask["connected_components"]), 1)

    def test_insufficient_data_is_blocked_without_lowering_gate(self):
        p = protocol(); start = datetime(2025, 1, 1, tzinfo=timezone.utc)
        records, _ = study.past_records({"matches": [record(i+1, start+timedelta(days=i), 1+i % 20,
            1+(i+1) % 20) for i in range(179)]}, p)
        plan = study.inventory(records, p)
        self.assertEqual(plan["status"], "blocked")
        self.assertEqual(plan["planned_refit_attempts"], 0)

    def test_reference_uses_actual_remaining_count_and_not_held_team(self):
        model = {"team_parameters": {str(i): {"attack": i/10, "defence": -i/20,
            "training_matches": 6+i} for i in range(1, 11)}}
        result = study.reference_variances(model, protocol())
        self.assertEqual(result["actual_fitted_team_count"], 10)
        self.assertAlmostEqual(result["attack_variance"], np.var(np.arange(1, 11)/10, ddof=1))
        with self.assertRaises(ValueError):
            study.reference_variances({"team_parameters": dict(list(model["team_parameters"].items())[:7])}, protocol())

    def test_inner_history_maturity_future_and_block_gate(self):
        rows = [history_row(i, i % 2) for i in range(1, 21)]
        poison = history_row(300, 2, available="2025-07-01T04:00:00Z")
        poison["trials"] = [{"tau": t, "metrics": {"scoreline_nll": 0 if t == 2 else 1000}}
            for t in [0, .5, 1, 2]]
        result = study.select_tau(rows+[poison], "2025-04-01T00:00:00Z", protocol())
        self.assertEqual(result["tau"], .5)
        self.assertNotIn(300, result["inner_unique_match_ids"])
        self.assertEqual(result["inner_blocks"], [0, 1])

    def test_inadequate_inner_unique_matches_warms_up_null(self):
        rows = [history_row(1, 0), history_row(1, 1)]*20
        result = study.select_tau(rows, "2025-04-01T00:00:00Z", protocol())
        self.assertEqual(result["tau"], 0)
        self.assertIn("insufficient", result["selection_status"])

    def test_failures_stay_in_same_pool_and_ties_choose_null(self):
        rows = [history_row(i, i % 2) for i in range(1, 21)]
        for row in rows:
            for trial in row["trials"]:
                trial["metrics"] = None
        result = study.select_tau(rows, "2025-04-01T00:00:00Z", protocol())
        self.assertEqual(result["tau"], 0)
        self.assertTrue(all(t["same_pool_as_point"] for t in result["trials"]))
        self.assertTrue(all(t["candidate_failures_point_fallback"] == 20 for t in result["trials"]))

    def test_repeated_masks_average_within_unique_match(self):
        rows = [history_row(i, i % 2) for i in range(1, 21)]
        duplicated = copy.deepcopy(rows[0])
        duplicated["scenario_id"] += ":repeat"
        result = study.select_tau(rows+[duplicated], "2025-04-01T00:00:00Z", protocol())
        self.assertEqual(len(result["inner_unique_match_ids"]), 20)
        self.assertEqual(result["inner_scenarios"], 21)
        self.assertEqual(result["trials"][1]["scoreline_nll"], 1.0)

    def test_metrics_full_matrix_and_interval_events(self):
        matrix = [[.25, .25], [.25, .25]]
        result = study.metrics({"score_matrix": matrix}, {"home_goals": 1, "away_goals": 0}, protocol())
        self.assertAlmostEqual(result["scoreline_nll"], np.log(4))
        self.assertAlmostEqual(result["1X2_Brier"], .875)
        self.assertEqual(result["events"]["total_goals_ge5"]["probability"], 0)
        self.assertTrue(result["intervals"]["difference_0.95"]["covered"])
        with self.assertRaises(ValueError): study.metrics({"score_matrix": matrix}, {"home_goals": 2, "away_goals": 0}, protocol())

    def test_output_refuses_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/"receipt.json"
            study.save(path, {"saved": 1})
            with self.assertRaises(FileExistsError): study.save(path, {"saved": 2})


if __name__ == "__main__":
    unittest.main()
