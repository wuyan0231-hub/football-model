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
from football_model.feature_dataset import FEATURE_NAMES, build_replay
from football_model.feature_research import (
    _mean_metrics, choose_alpha, digest, save_immutable, score_forecast, study,
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

    def test_extended_scores_project_one_matrix_and_preserve_legacy_output(self):
        prediction = _distribution_from_rates(0.7, 2.2)
        observed = {"home_goals": 0, "away_goals": 3}
        legacy = score_forecast(prediction, observed)
        self.assertNotIn("total_goal_nll", legacy)
        self.assertNotIn("away_net_ge3", legacy["events"])
        extended = score_forecast(prediction, observed, extended_diagnostics=True)
        matrix = np.asarray(prediction["score_matrix"])
        home, away = np.indices(matrix.shape)
        self.assertAlmostEqual(extended["total_goal_nll"], -math.log(matrix[home + away == 3].sum()))
        self.assertAlmostEqual(extended["goal_difference_nll"], -math.log(matrix[home - away == -3].sum()))
        self.assertAlmostEqual(extended["events"]["away_net_ge3"]["probability"], matrix[away-home >= 3].sum())
        self.assertEqual(extended["events"]["away_net_ge3"]["observed"], 1)
        self.assertEqual(extended["scoreline_nll"], legacy["scoreline_nll"])
        aggregate = _mean_metrics([legacy])
        self.assertIsNone(aggregate["total_goal_nll"])
        self.assertEqual(aggregate["total_goal_nll_available_n"], 0)
        self.assertIsNone(aggregate["event_calibration"]["away_net_ge2"]["brier"])

    def test_v2_requests_all_targets_and_isolates_artifact_namespace(self):
        root = self.runtime(matches())
        old_model = root / "feature_models" / "models" / "serie_a" / "latest.json"
        old_report = root / "feature_models" / "reports" / "serie_a" / "latest.json"
        for path in (old_model, old_report):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('{"unchanged_v1":true}\n')
        spec = {**protocol(), "research_design_version": "feature-research-v2",
                "research_artifact_namespace": "protocol_hash_v2"}
        report = study(root, "serie_a", spec, source_commit="source-test",
                       clock=lambda: datetime(2024, 2, 15, tzinfo=timezone.utc))
        self.assertEqual(report["requested_matches"], 30)
        self.assertEqual(len(report["scored_matches"]), 30)
        self.assertEqual(sum("M0" not in row["scores"] for row in report["scored_matches"]), len(report["replay"]["failures"]))
        for candidate in ("M1", "G0", "G1", "G0_full"):
            coverage = report["request_coverage"][candidate]
            self.assertEqual(coverage["requested_matches"], 30)
            self.assertTrue(coverage["denominator_preserved"])
            self.assertTrue(all(candidate in row["candidate_status"] for row in report["scored_matches"]))
        self.assertIn(digest(spec), report["model_card_path"])
        self.assertTrue((root / "feature_models" / "reports" / "serie_a" / digest(spec) / "latest.json").exists())
        self.assertEqual(old_model.read_text(), '{"unchanged_v1":true}\n')
        self.assertEqual(old_report.read_text(), '{"unchanged_v1":true}\n')
        self.assertNotIn("G0_full", study(self.runtime(matches()), "serie_a", protocol(),
            source_commit="source-test", clock=lambda: datetime(2024, 2, 15, tzinfo=timezone.utc))["comparisons"])

    def test_v2_goalmodel_full_pool_and_candidate_only_fallbacks(self):
        records = matches()
        replay = build_replay(records, horizon_hours=10, block_days=7, min_train=6)
        rejected = replay["forecasts"].pop()
        target = rejected["match_id"]
        failure = {key: value for key, value in rejected.items() if key != "baseline_prediction"}
        failure.update(stage="baseline_prediction", code="baseline_prediction_failed", reason="original failure retained")
        replay["failures"].append(failure)
        replay["coverage"]["baseline_forecasts"] -= 1
        replay["coverage"]["baseline_failures"] += 1
        replay["correction_rows"] = [row for row in replay["correction_rows"] if row["match_id"] != target]
        replay["rows"] = replay["correction_rows"]
        calls = []
        def adapter(training, fixtures, **kwargs):
            calls.append((copy.deepcopy(training), copy.deepcopy(fixtures), copy.deepcopy(kwargs)))
            forecasts, errors = [], []
            for fixture in fixtures:
                if fixture["match_id"] == 28:
                    errors.append({"match_id": 28, "code": "fit_rejected", "message": "first failure"})
                else:
                    forecasts.append({**_distribution_from_rates(1.4, 1.1), **fixture})
            return {"forecasts": forecasts, "errors": errors}
        spec = {**protocol(), "research_design_version": "feature-research-v2"}
        with patch("football_model.feature_research.build_replay", return_value=replay), \
                patch("football_model.goalmodel_adapter.fit_predict_goalmodel", side_effect=adapter):
            report = study(self.runtime(records), "serie_a", spec, source_commit="source-test", run_goalmodel=True,
                clock=lambda: datetime(2024, 2, 15, tzinfo=timezone.utc))
        by_cutoff = {}
        for train, fixtures, kwargs in calls:
            by_cutoff.setdefault(kwargs["cutoff"], []).append((train, fixtures, kwargs))
        for variants in by_cutoff.values():
            # The full pool may satisfy the minimum earlier than the common
            # feature-complete pool. That one-sided availability is retained.
            self.assertIn(len(variants), (1, 3))
            full = variants[-1]
            if len(variants) == 3:
                g0, g1, _ = variants
                self.assertEqual(g0[0], g1[0])
                self.assertEqual(g0[1], g1[1])
            cutoff = datetime.fromisoformat(full[2]["cutoff"].replace("Z", "+00:00"))
            expected = [m["match_id"] for m in records if datetime.fromisoformat(m["kickoff"]) + timedelta(hours=4) <= cutoff]
            self.assertEqual([m["match_id"] for m in full[0]], expected)
        only = next(row for row in report["scored_matches"] if row["match_id"] == target)
        self.assertNotIn("M0", only["scores"])
        self.assertIn("G0_full", only["scores"])
        self.assertEqual(only["policy_status"]["G0_full"], "candidate")
        self.assertIn(target, report["request_coverage"]["G0_full"]["candidate_only_match_ids"])
        fallback = next(row for row in report["scored_matches"] if row["match_id"] == 28)
        self.assertEqual(fallback["candidate_failures"]["G0_full"]["code"], "fit_rejected")
        self.assertEqual(fallback["policy_status"]["G0_full"], "unchanged_baseline_fallback")
        self.assertEqual(fallback["scores"]["G0_full_policy"], fallback["scores"]["M0"])
        self.assertTrue(all(report["request_coverage"][k]["denominator_preserved"] for k in ("M1", "G0", "G1", "G0_full")))

    def test_latest_fit_failure_saves_partial_research_and_keeps_prior_card(self):
        root = self.runtime(matches())
        clock = lambda: datetime(2024, 2, 15, tzinfo=timezone.utc)
        successful = study(root, "serie_a", protocol(), source_commit="source-test", clock=clock)
        card = root / "feature_models" / "models" / "serie_a" / "latest.json"
        previous = card.read_bytes()
        actual = choose_alpha
        def fail_latest(rows, spec, cutoff):
            if cutoff == "2024-02-15T00:00:00Z":
                raise ModelDataError("controlled latest fit rejection")
            return actual(rows, spec, cutoff)
        with patch("football_model.feature_research.choose_alpha", side_effect=fail_latest):
            partial = study(root, "serie_a", protocol(), source_commit="source-test", clock=clock)
        self.assertEqual(partial["latest_fit"]["status"], "failed")
        self.assertIn("controlled latest fit rejection", partial["latest_fit"]["detail"])
        self.assertIsNone(partial["model_card_path"])
        self.assertEqual(card.read_bytes(), previous)
        self.assertEqual(partial["scored_matches"], successful["scored_matches"])
        self.assertTrue(partial["latest_fit"]["previous_latest_preserved"])
        latest = json.loads((root / "feature_models" / "reports" / "serie_a" / "latest.json").read_text())
        full = json.loads((root / latest["full_report_ref"]["path"]).read_text())
        self.assertEqual(full, partial)


if __name__ == "__main__":
    unittest.main()
