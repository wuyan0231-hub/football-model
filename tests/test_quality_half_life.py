"""Protocol/anti-leakage tests; synthetic fixtures are not efficacy evidence."""
import copy
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import numpy as np

from football_model import quality_half_life as research
from football_model.model import ModelDataError, _distribution_from_rates


def matches(n=120):
    rng = np.random.default_rng(29)
    start = datetime(2024, 1, 1, 12, tzinfo=timezone.utc)
    records = []
    for i in range(n):
        home, away = rng.choice(6, 2, replace=False) + 1
        records.append({"match_id": i + 1, "kickoff": (start + timedelta(days=i // 2, hours=i % 2)).isoformat(),
                        "home_id": int(home), "away_id": int(away), "home_name": str(home), "away_name": str(away),
                        "home_goals": int(rng.poisson(1.5)), "away_goals": int(rng.poisson(1.1))})
    return records


def protocol():
    return research.make_protocol("2024-02-10T00:00:00Z", "2024-03-01T00:00:00Z",
                                  min_train=30, inner_days=21, minimum_inner_matches=12,
                                  bootstrap_replicates=100)


class HalfLifeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = matches()
        cls.result = research.evaluate_half_life(cls.source, protocol())

    def test_fixed_candidates_weekly_origins_and_research_only(self):
        report = self.result["report"]
        self.assertEqual(report["protocol"]["half_lives"],
                         {"half_life_180": 180.0, "half_life_90": 90.0, "half_life_365": 365.0})
        self.assertFalse(report["deployment_approved"])
        self.assertFalse(report["prospective_validation"])
        self.assertFalse(report["point_in_time_sources_verified"])
        for window in self.result["windows"]:
            self.assertLessEqual(window["last_result_assumed_available_at"], window["origin"])
            delta = research._timestamp(window["end"], "end") - research._timestamp(window["origin"], "origin")
            self.assertLessEqual(delta, timedelta(days=7))
        json.dumps(self.result, allow_nan=False)

    def test_future_results_cannot_change_origin_selection_or_forecasts(self):
        changed = copy.deepcopy(self.source)
        for row in changed:
            if row["kickoff"] >= "2024-02-17":
                row["home_goals"], row["away_goals"] = 8, 0
        after = research.evaluate_half_life(changed, protocol())
        for before, now in zip(self.result["windows"], after["windows"]):
            if before["origin"] <= "2024-02-17T00:00:00Z":
                for field in ("selection", "training_data_sha256", "model_parameters"):
                    self.assertEqual(before[field], now[field])
        for before, now in zip(self.result["predictions"], after["predictions"]):
            if before["forecast_origin"] <= "2024-02-17T00:00:00Z":
                self.assertEqual(before["forecasts"], now["forecasts"])

    def test_selection_only_mature_common_inner_rows_and_baseline_ties(self):
        origin = datetime(2024, 2, 10, tzinfo=timezone.utc)
        p = protocol()
        p["minimum_inner_matches"] = 1
        row = {"match_id": 1, "kickoff": "2024-02-09T22:00:00Z",
               "result_assumed_available_at": "2024-02-10T02:00:00Z",
               "actual_home_goals": 1, "actual_away_goals": 0,
               "forecasts": {n: {"probabilities": {"home_win": 0.8 if n == "half_life_90" else 0.2}}
                             for n in research.HALF_LIVES}}
        self.assertEqual(research._choose([row], origin, p)["n_inner_common"], 0)
        self.assertEqual(research._choose([row], origin + timedelta(hours=2), p)["candidate"], "half_life_90")
        row["forecasts"]["half_life_90"]["probabilities"]["home_win"] = 0.2
        self.assertEqual(research._choose([row], origin + timedelta(hours=2), p)["candidate"], research.BASELINE)
        del row["forecasts"]["half_life_365"]
        self.assertEqual(research._choose([row], origin + timedelta(hours=2), p)["n_inner_common"], 0)

    def test_unseen_team_is_preserved_and_not_imputed(self):
        source = copy.deepcopy(self.source)
        for row in source:
            if row["kickoff"].startswith("2024-02-10"):
                row["home_id"], row["home_name"] = 999, "New"
        result = research.evaluate_half_life(source, protocol())
        rows = [r for r in result["predictions"] if r["kickoff"].startswith("2024-02-10")]
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertIn("league_mean", row["forecasts"])
            self.assertNotIn("selected_policy", row["forecasts"])
            self.assertIn("未知球队", row["unavailable"][research.BASELINE])
        self.assertEqual(len(result["report"]["paired_comparisons"]["league_mean"]["candidate_only_ids"]), 2)

    def test_failed_candidate_does_not_disappear_from_coverage(self):
        original = research.fit_model
        def one_failure(rows, cutoff, days, ridge, min_train):
            if days == 90:
                return {"optimizer": {"parameter_at_bound": True}}
            return original(rows, cutoff, days, ridge, min_train)
        with patch.object(research, "fit_model", side_effect=one_failure):
            result = research.evaluate_half_life(self.source, protocol())
        report = result["report"]
        self.assertEqual(report["coverage"]["by_candidate"]["half_life_90"], 0)
        self.assertGreater(len(report["paired_comparisons"]["half_life_90"]["baseline_only_ids"]), 0)
        self.assertEqual(report["coverage"]["all_candidates_common"], 0)
        self.assertTrue(all(w["selection"]["candidate"] == research.BASELINE for w in result["windows"]))
        self.assertTrue(all(w["fit_errors"]["half_life_90"] == "parameter_at_bound" for w in result["windows"]))

    def test_exact_score_total_losses_and_matrix_primary_metric(self):
        forecast = research._forecast(_distribution_from_rates(1.5, 1.0))
        row = {"actual_home_goals": 0, "actual_away_goals": 0, "forecasts": {research.BASELINE: forecast}}
        values = dict(zip(research.LOSS_NAMES, research._losses(row, research.BASELINE)))
        self.assertAlmostEqual(values["score_log_loss"], 2.5)
        self.assertAlmostEqual(values["total_log_loss"], 2.5)
        self.assertAlmostEqual(values["one_x_two_log_loss"], -np.log(forecast["probabilities"]["draw"]))

    def test_bootstrap_uses_28_day_blocks_not_rows_or_weekly_origins(self):
        rows = [copy.deepcopy(r) for r in self.result["predictions"] if r["phase"] == "outer"]
        paired = research._paired(rows, "half_life_90", protocol())
        self.assertEqual(paired["time_block_count"], 1)
        self.assertEqual(paired["interval_status"], "insufficient_time_blocks")
        self.assertIsNone(paired["metrics"]["one_x_two_log_loss"]["interval_95"])
        eight_blocks = []
        for i in range(8):
            row = copy.deepcopy(rows[0])
            row["match_id"] = i + 1
            row["evaluation_block_start"] = f"block{i}"
            eight_blocks.append(row)
        a = research._paired(eight_blocks, "half_life_90", protocol())
        b = research._paired(eight_blocks, "half_life_90", protocol())
        self.assertEqual(a, b)
        interval = a["metrics"]["one_x_two_log_loss"]["interval_95"]
        self.assertAlmostEqual(interval[0], interval[1])

    def test_invalid_or_modified_protocol_is_rejected(self):
        for changes in ({"step_days": 0}, {"ridge": float("nan")}, {"min_train": True}):
            with self.assertRaises(ModelDataError):
                research.make_protocol("2024-02-10T00:00:00Z", "2024-03-01T00:00:00Z", **changes)
        altered = protocol()
        altered["half_lives"]["half_life_90"] = 10
        with self.assertRaises(ModelDataError):
            research.evaluate_half_life(self.source, altered)

    def test_cli_frozen_source_binding_and_no_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data, frozen, output = root / "data.json", root / "protocol.json", root / "result.json"
            data.write_text(json.dumps(self.source))
            freeze = ["research", "--matches", str(data), "--start", "2024-02-10T00:00:00Z", "--end", "2024-03-01T00:00:00Z", "--freeze-protocol", str(frozen), "--min-train", "30"]
            with patch("sys.argv", freeze):
                self.assertEqual(research.main(), 0)
            run = ["research", "--matches", str(data), "--protocol", str(frozen), "--output", str(output)]
            original = data.read_bytes()
            data.write_bytes(original + b" ")
            with patch("sys.argv", run), self.assertRaisesRegex(ModelDataError, "指纹"):
                research.main()
            data.write_bytes(original)
            output.write_text("preserve me")
            with patch("sys.argv", run), self.assertRaises(SystemExit):
                research.main()
            self.assertEqual(output.read_text(), "preserve me")


if __name__ == "__main__":
    unittest.main()
