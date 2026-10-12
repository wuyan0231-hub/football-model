"""Past-only selection and honest experiment denominators; no effect claim."""

import copy
from datetime import datetime, timedelta, timezone
import json
import math
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

from football_model.feature_correction import fit_correction
from football_model.feature_dataset import FEATURE_NAMES
from football_model.feature_research import (
    choose_alpha, digest, save_immutable, score_forecast, study,
)
from football_model.model import ModelDataError, _distribution_from_rates


def matches(n=30):
    start = datetime(2024, 1, 1, 12, tzinfo=timezone.utc)
    pairs = ((1, 2), (3, 4), (1, 3), (2, 4), (1, 4), (2, 3))
    return [{"match_id": i + 1, "kickoff": (start + timedelta(days=i)).isoformat(),
             "home_id": pairs[i % 6][0], "away_id": pairs[i % 6][1],
             "home_name": "T" + str(pairs[i % 6][0]), "away_name": "T" + str(pairs[i % 6][1]),
             "home_goals": (i * 7 + 1) % 4, "away_goals": (i * 3 + 1) % 3,
             "neutral": i % 7 == 0} for i in range(n)]


def protocol():
    return {"feature_names": list(FEATURE_NAMES), "horizon_hours": 10,
            "block_days": 7, "baseline_min_train": 6, "baseline_half_life_days": 180,
            "baseline_ridge": 3, "correction_min_matches": 4,
            "correction_alpha_grid": [0.1, 1.0], "inner_validation_fraction": 0.25}


def selection_rows(n=24):
    result = []
    rng = np.random.default_rng(5924)
    start = datetime(2024, 1, 1, 12, tzinfo=timezone.utc)
    for index in range(n):
        kickoff = start + timedelta(days=index)
        cutoff = kickoff - timedelta(hours=10)
        for side in ("home", "away"):
            value = float(rng.normal())
            rate = 1.4 if side == "home" else 1.1
            result.append({"match_id": index + 1, "side": side,
                "kickoff": kickoff.isoformat(), "cutoff_at": cutoff.isoformat(),
                "baseline_training_cutoff": (cutoff - timedelta(days=1)).isoformat(),
                "target_available_at": (kickoff + timedelta(hours=4)).isoformat(),
                "lambda0": rate, "goals": int(rng.poisson(rate * math.exp(0.25 * value))),
                "features": {"signal": value}, "feature_available_at": {"signal": cutoff.isoformat()}})
    return result


class FeatureResearchTests(unittest.TestCase):
    def runtime(self, records):
        directory = tempfile.TemporaryDirectory(dir=Path.cwd())
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        (root / "data").mkdir()
        (root / "data" / "serie_a.json").write_text(json.dumps({
            "matches": records, "observed_at": "2024-02-15T00:00:00Z"}), encoding="utf-8")
        return root

    def test_inner_alpha_only_trains_before_validation_and_before_outer_cutoff(self):
        rows = selection_rows()
        spec = {**protocol(), "feature_names": ["signal"]}
        outer = "2024-02-10T00:00:00Z"
        with patch("football_model.feature_research.fit_correction", wraps=fit_correction) as fits:
            alpha, audit = choose_alpha(rows, spec, outer)
        self.assertIn(alpha, spec["correction_alpha_grid"])
        self.assertEqual(audit["status"], "past_only_inner_time_validation")
        self.assertEqual(len(fits.call_args_list), 2)
        observed_sets = []
        for call in fits.call_args_list:
            train, names = call.args
            cutoff = datetime.fromisoformat(call.kwargs["cutoff"].replace("Z", "+00:00"))
            self.assertEqual(names, ["signal"])
            self.assertLess(cutoff, datetime.fromisoformat(outer.replace("Z", "+00:00")))
            self.assertTrue(all(datetime.fromisoformat(row["target_available_at"].replace("Z", "+00:00")) <= cutoff
                                for row in train))
            observed_sets.append({row["match_id"] for row in train})
        self.assertEqual(observed_sets[0], observed_sets[1])
        self.assertTrue(observed_sets[0].isdisjoint({19, 20, 21, 22, 23, 24}))

    def test_alpha_selection_rejects_late_features_and_targets_even_during_cold_start(self):
        rows = selection_rows(8)
        spec = {**protocol(), "feature_names": ["signal"], "correction_min_matches": 4}
        future = copy.deepcopy(rows)
        future[-1]["target_available_at"] = "2024-03-01T00:00:00Z"
        with self.assertRaises(ModelDataError):
            choose_alpha(future, spec, "2024-02-10T00:00:00Z")
        late = copy.deepcopy(rows)
        late[-1]["feature_available_at"]["signal"] = late[-1]["kickoff"]
        with self.assertRaises(ModelDataError):
            choose_alpha(late, spec, "2024-02-10T00:00:00Z")
        cold = copy.deepcopy(spec)
        cold["correction_min_matches"] = 8
        with self.assertRaises(ModelDataError):
            choose_alpha(future, cold, "2024-02-10T00:00:00Z")

    def test_alpha_selection_is_invariant_to_timezone_representation(self):
        rows = selection_rows()
        represented = copy.deepcopy(rows)
        local = timezone(timedelta(hours=8))
        for row in represented:
            for key in ("kickoff", "cutoff_at", "baseline_training_cutoff", "target_available_at"):
                row[key] = datetime.fromisoformat(row[key]).astimezone(local).isoformat()
            row["feature_available_at"]["signal"] = datetime.fromisoformat(row["feature_available_at"]["signal"]).astimezone(local).isoformat()
        spec = {**protocol(), "feature_names": ["signal"]}
        alpha_a, audit_a = choose_alpha(rows, spec, "2024-02-10T00:00:00Z")
        alpha_b, audit_b = choose_alpha(represented, spec, "2024-02-10T08:00:00+08:00")
        self.assertEqual(alpha_a, alpha_b)
        self.assertEqual(audit_a, audit_b)

    def test_score_uses_full_grid_in_declared_home_away_order(self):
        prediction = _distribution_from_rates(2.5, 0.5)
        observed = {"home_goals": 2, "away_goals": 0}
        result = score_forecast(prediction, observed)
        self.assertAlmostEqual(result["scoreline_nll"], -math.log(prediction["score_matrix"][2][0]))
        self.assertEqual(result["events"]["home_net_ge2"]["observed"], 1)
        self.assertEqual(result["events"]["home_net_ge3"]["observed"], 0)
        self.assertFalse(result["observed_score_outside_grid"])
        corrupt = copy.deepcopy(prediction)
        corrupt["score_matrix_axes"] = {"rows": "away_goals_from_0", "columns": "home_goals_from_0"}
        with self.assertRaises(ModelDataError):
            score_forecast(corrupt, observed)
        corrupt = copy.deepcopy(prediction)
        corrupt["probabilities"]["draw"] = float("nan")
        with self.assertRaises(ModelDataError):
            score_forecast(corrupt, observed)
        for invalid in (-1, True, 1.5):
            with self.subTest(score=invalid), self.assertRaises(ModelDataError):
                score_forecast(prediction, {"home_goals": invalid, "away_goals": 0})

    def test_study_saves_real_json_cards_and_keeps_fallback_denominator(self):
        root = self.runtime(matches())
        report = study(root, "serie_a", protocol(), source_commit="source-test",
                       clock=lambda: datetime(2024, 2, 15, tzinfo=timezone.utc))
        replay = report["replay"]
        self.assertEqual(replay["coverage"]["requested_matches"], 30)
        scored = report["scored_matches"]
        self.assertEqual(len(scored), replay["coverage"]["baseline_forecasts"])
        self.assertEqual(len(scored) + len(replay["failures"]), 30)
        policy = report["comparisons"]["M1_policy"]
        self.assertEqual(policy["n"], len(scored))
        fallback = [row for row in scored if "M1" not in row["scores"]]
        self.assertGreater(len(fallback), 0)
        self.assertTrue(all(row["scores"]["M1_policy"] == row["scores"]["M0"] for row in fallback))
        self.assertLess(report["comparisons"]["M1"]["n"], policy["n"])
        self.assertFalse(report["prospective_qualified"])
        self.assertFalse(report["accuracy_improvement_verified"])
        self.assertFalse(report["quotes_used"])
        self.assertIsNone(report["betting_returns"])
        card_path = root / report["model_card_path"]
        card = json.loads(card_path.read_text(encoding="utf-8"))
        self.assertEqual(card["model_hash"], digest(card["correction_model"]))
        self.assertEqual(card_path.name, digest(card) + ".json")
        self.assertEqual(json.loads((card_path.parent.parent / "latest.json").read_text()), card)
        report_paths = list((root / "feature_models" / "reports" / "serie_a" / "history").glob("*.json"))
        self.assertEqual(len(report_paths), 1)
        self.assertEqual(json.loads(report_paths[0].read_text()), report)
        json.dumps(report, allow_nan=False)

    def test_changing_later_results_cannot_change_earlier_outer_predictions(self):
        original = matches()
        changed = copy.deepcopy(original)
        for match in changed[-6:]:
            match["home_goals"], match["away_goals"] = 0, 6
        clock = lambda: datetime(2024, 2, 15, tzinfo=timezone.utc)
        before = study(self.runtime(original), "serie_a", protocol(), source_commit="source-test", clock=clock)
        after = study(self.runtime(changed), "serie_a", protocol(), source_commit="source-test", clock=clock)
        limit = original[-6]["kickoff"]
        first = {row["match_id"]: row for row in before["scored_matches"] if row["kickoff"] < limit}
        second = {row["match_id"]: row for row in after["scored_matches"] if row["kickoff"] < limit}
        self.assertEqual(first, second)
        early_models_a = [exp.get("correction_model") for exp in before["experiments"] if exp["cutoff"] < limit]
        early_models_b = [exp.get("correction_model") for exp in after["experiments"] if exp["cutoff"] < limit]
        self.assertEqual(early_models_a, early_models_b)

    def test_study_never_scores_results_not_available_at_run_start(self):
        records = matches()
        records[-1]["kickoff"] = "2024-02-20T12:00:00Z"
        root = self.runtime(records)
        # A history that falsely contains a future final must be rejected or
        # separately excluded, with that source failure visible in the report.
        try:
            report = study(root, "serie_a", protocol(), source_commit="source-test",
                           clock=lambda: datetime(2024, 2, 15, tzinfo=timezone.utc))
        except ModelDataError:
            return
        self.assertNotIn(records[-1]["match_id"], {row["match_id"] for row in report["scored_matches"]})
        self.assertNotIn(records[-1]["match_id"], report["replay"]["baseline_fits"][-1]["training_match_ids"])
        visible = json.dumps(report, ensure_ascii=False)
        self.assertTrue(any(reason in visible for reason in ("not_available", "future", "unavailable")))

    def test_goalmodel_g0_and_g1_share_past_training_targets_and_neutral_identity(self):
        calls = []
        def adapter(training, fixtures, **kwargs):
            calls.append((copy.deepcopy(training), copy.deepcopy(fixtures), copy.deepcopy(kwargs)))
            forecasts = []
            for fixture in fixtures:
                prediction = _distribution_from_rates(1.4, 1.1)
                prediction.update(fixture)
                forecasts.append(prediction)
            return {"forecasts": forecasts, "errors": []}
        records = matches()
        root = self.runtime(records)
        with patch("football_model.goalmodel_adapter.fit_predict_goalmodel", side_effect=adapter):
            report = study(root, "serie_a", protocol(), source_commit="source-test", run_goalmodel=True,
                           clock=lambda: datetime(2024, 2, 15, tzinfo=timezone.utc))
        self.assertGreater(len(calls), 0)
        by_cutoff = {}
        for train, fixtures, kwargs in calls:
            by_cutoff.setdefault(kwargs["cutoff"], []).append((train, fixtures, kwargs))
            cutoff = datetime.fromisoformat(kwargs["cutoff"].replace("Z", "+00:00"))
            self.assertTrue(all(datetime.fromisoformat(row["kickoff"]) + timedelta(hours=4) <= cutoff for row in train))
            self.assertTrue({row["match_id"] for row in train}.isdisjoint({row["match_id"] for row in fixtures}))
            for row in fixtures:
                source = next(match for match in records if match["match_id"] == row["match_id"])
                self.assertIn("neutral", row)
                self.assertEqual(row["neutral"], source["neutral"])
        for variants in by_cutoff.values():
            self.assertEqual(len(variants), 2)
            g0, g1 = variants
            self.assertEqual(g0[0], g1[0])
            self.assertEqual(g0[1], g1[1])
            self.assertEqual(tuple(g0[2]["feature_names"]), ())
            self.assertEqual(tuple(g1[2]["feature_names"]), FEATURE_NAMES)
        self.assertEqual(report["comparisons"]["G0"]["n"], report["comparisons"]["G1"]["n"])

    def test_immutable_artifact_never_replaces_altered_existing_file(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            value = {"candidate": "test", "version": 1}
            path = save_immutable(directory, value)
            self.assertEqual(save_immutable(directory, value), path)
            path.write_text(json.dumps({"candidate": "tampered"}), encoding="utf-8")
            with self.assertRaisesRegex(ModelDataError, "collision"):
                save_immutable(directory, value)
            self.assertEqual(json.loads(path.read_text()), {"candidate": "tampered"})


if __name__ == "__main__":
    unittest.main()
