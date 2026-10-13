"""Additional-family isolation, chronological offsets and saved R reproduction."""
import copy
from datetime import datetime, timedelta, timezone
import json
import hashlib
import io
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

from football_model.advanced_research import (FEATURE_GROUPS, study, digest, load_cards,
    build_live_predictions, predict_saved_goalmodel, verify_goalmodel_probes, validate_protocol, main)
from football_model.advanced_capacity import CapacityDeferred
from football_model.goalmodel_adapter import fit_predict_goalmodel
from football_model.model import ModelDataError, _distribution_from_rates
from tests.test_feature_research import matches


ROOT = Path(__file__).resolve().parents[1]


def spec():
    result = json.loads((ROOT / "advanced-models-v1.json").read_text())
    result.update(baseline_min_train=6, correction_min_matches=4, block_days=7,
                  correction_alpha_grid=[1.0], advanced_feature_min_history=2)
    result["candidates"] = {k: result["candidates"][k] for k in ("M_schedule", "M_shots", "M_xg")}
    result["goalmodel_feature_candidate"] = "M_shots"
    result["family_increment_comparisons"] = [["M_shots", "M_schedule"], ["M_xg", "M_schedule"]]
    return result


def dataset(runtime, league, matches=None, horizon="EARLY", **kwargs):
    return {"rows": [{"match_id": m["match_id"], "home": {n: float((i + j) % 7) for j, n in enumerate(sum(FEATURE_GROUPS.values(), []))},
                     "away": {n: float((i + j + 2) % 7) for j, n in enumerate(sum(FEATURE_GROUPS.values(), []))}}
                    for i, m in enumerate(matches)], "source_refs": [], "coverage": {}}


class AdvancedResearchTests(unittest.TestCase):
    def runtime(self, records):
        temporary = tempfile.TemporaryDirectory(dir=ROOT)
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        (root / "data").mkdir()
        (root / "data" / "serie_a.json").write_text(json.dumps({"matches": records}))
        return root

    def run_study(self, root, protocol=None, builder=dataset):
        with patch("football_model.advanced_features.build_feature_rows", side_effect=builder):
            return study(root, "serie_a", protocol or spec(), source_commit="test-source",
                         clock=lambda: datetime(2024, 3, 1, tzinfo=timezone.utc))

    def test_missing_xg_does_not_erase_shots_or_drop_requests(self):
        records = matches(40)
        records[-1].update(away_id=999, away_name="Unknown")
        root = self.runtime(records)
        def missing(*args, **kwargs):
            result = dataset(*args, **kwargs)
            for row in result["rows"]:
                for side in ("home", "away"):
                    for name in FEATURE_GROUPS["opportunity_xg"]:
                        row[side][name] = None
            return result
        report = self.run_study(root, builder=missing)
        self.assertEqual(report["requested_matches"], 40)
        self.assertEqual(len(report["scored_matches"]), 40)
        self.assertGreater(report["request_coverage"]["M_shots"]["candidate_forecasts"], 0)
        self.assertEqual(report["request_coverage"]["M_xg"]["candidate_forecasts"], 0)
        self.assertTrue(report["request_coverage"]["M_xg"]["denominator_preserved"])
        self.assertGreater(report["request_coverage"]["M_xg"]["baseline_only_fallback"], 0)
        self.assertEqual(report["cold_start_diagnostics"]["unique_matches"], 1)
        unknown = report["scored_matches"][-1]
        self.assertNotIn("M0", unknown["scores"])
        self.assertIn("COLD_START", unknown["scores"])
        self.assertFalse(report["accuracy_improvement_verified"])

    def test_every_offset_fit_uses_only_mature_historical_oof_rows(self):
        root = self.runtime(matches(40))
        report = self.run_study(root)
        for experiment in report["experiments"]:
            for candidate, fit in experiment["fits"].items():
                model = fit["model"]
                self.assertLessEqual(model["last_result_available_at"], experiment["cutoff"])
                self.assertEqual(model["offset_definition"], "log(out_of_sample_baseline_lambda), coefficient fixed to 1")
            for key, fit in experiment.get("matched_pool_ablation_fits", {}).items():
                if key.split("_minus_")[0] in experiment["fits"]:
                    self.assertTrue(fit["same_training_ids_as_added"])
        for ref in report["model_cards"].values():
            card = json.loads((root / ref["path"]).read_text())
            bundle = json.loads((root / card["source_refs"][-1]["path"]).read_text())
            for row in bundle["correction_rows"]:
                self.assertNotIn(row["match_id"], row["baseline_training_match_ids"])
                self.assertLessEqual(row["baseline_training_cutoff"], row["cutoff_at"])
                self.assertLessEqual(max(row["feature_available_at"].values()), row["cutoff_at"])

    def test_failed_refit_preserves_card_but_writes_current_failure(self):
        root = self.runtime(matches(40))
        report = self.run_study(root)
        previous = load_cards(root, "serie_a", spec())["M_shots"]
        def missing(*args, **kwargs):
            result = dataset(*args, **kwargs)
            for row in result["rows"]:
                row["home"]["shots_for_mean"] = None
            return result
        second = self.run_study(root, builder=missing)
        self.assertEqual(load_cards(root, "serie_a", spec())["M_shots"], previous)
        self.assertEqual(second["latest_fits"]["M_shots"]["status"], "failed")
        self.assertTrue(second["latest_fits"]["M_shots"]["previous_latest_preserved"])

    def test_live_card_time_source_integrity_and_missing_feature_are_independent(self):
        root = self.runtime(matches(40))
        self.run_study(root)
        features = dataset(root, "serie_a", matches=[matches(1)[0]])["rows"][0]
        features["horizon"] = "EARLY"
        for side in ("home", "away"):
            features[side].update({n: 5.0 for n in FEATURE_GROUPS["schedule"]})
        baseline = {**_distribution_from_rates(1.4, 1.1), "match_id": 999,
            "home_id": 1, "away_id": 2, "neutral": False, "kickoff": "2024-03-02T12:00:00Z",
            "training_cutoff": "2024-03-01T00:00:00Z"}
        output = build_live_predictions(root, "serie_a", spec(), baseline, features, "2024-03-02T02:00:00Z")
        self.assertIn("M_shots", output["predictions"])
        features["home"]["xg_for_mean"] = None
        partial = build_live_predictions(root, "serie_a", spec(), baseline, features, "2024-03-02T02:00:00Z")
        self.assertIn("M_shots", partial["predictions"])
        self.assertNotIn("M_xg", partial["predictions"])
        early = build_live_predictions(root, "serie_a", spec(), baseline, features, "2024-02-29T22:00:00Z")
        self.assertFalse(early["predictions"])
        card = load_cards(root, "serie_a", spec())["M_shots"]
        (root / card["source_refs"][-1]["path"]).write_text("{}")
        corrupt = build_live_predictions(root, "serie_a", spec(), baseline, features, "2024-03-02T02:00:00Z")
        self.assertNotIn("M_shots", corrupt["predictions"])
        self.assertIn("M_schedule", corrupt["predictions"])

    def test_self_consistent_card_coefficient_edit_is_rejected_against_fitted_bundle(self):
        root = self.runtime(matches(40))
        self.run_study(root)
        card = load_cards(root, "serie_a", spec())["M_shots"]
        card["correction_model"]["coefficients"]["shots_for_mean"] += 0.2
        model = card["correction_model"]
        model["correction_model_sha256"] = digest({k:v for k,v in model.items() if k != "correction_model_sha256"})
        card["model_hash"] = digest({k:v for k,v in card.items() if k != "model_hash"})
        path = root / "advanced_models" / "serie_a" / digest(spec()) / "models" / "M_shots" / "latest.json"
        path.write_text(json.dumps(card))
        values = dataset(root, "serie_a", matches=[matches(1)[0]])["rows"][0]
        baseline = {**_distribution_from_rates(1.4, 1.1), "match_id": 999,
            "home_id": 1, "away_id": 2, "neutral": False, "kickoff": "2024-03-02T12:00:00Z",
            "training_cutoff": "2024-03-01T00:00:00Z"}
        output = build_live_predictions(root, "serie_a", spec(), baseline, values, "2024-03-02T02:00:00Z")
        self.assertNotIn("M_shots", output["predictions"])
        self.assertIn("frozen fitted model", output["failures"]["M_shots"]["detail"])

    def test_protocol_rejects_confirmed_lineup_at_early_cutoff(self):
        protocol = spec()
        protocol["candidates"]["M_shots"]["groups"].append("lineup_confirmed")
        with self.assertRaises(ModelDataError):
            validate_protocol(protocol)

    def test_relative_runtime_freezes_original_refs_inside_training_bundle(self):
        root = self.runtime(matches(40))
        def with_ref(*args, **kwargs):
            result = dataset(*args, **kwargs)
            ref = {"path": "data/serie_a.json", "sha256": hashlib.sha256((root / "data/serie_a.json").read_bytes()).hexdigest()}
            result["source_refs"] = [ref]
            for row in result["rows"]:
                row["source_refs"] = [ref]
            return result
        self.run_study(root.relative_to(Path.cwd()), builder=with_ref)
        card = load_cards(root, "serie_a", spec())["M_shots"]
        bundle = json.loads((root / card["correction_training_ref"]["path"]).read_text())
        self.assertTrue(bundle["feature_dataset"]["source_refs"][0]["path"].startswith("advanced_models/inputs/"))
        self.assertTrue(bundle["feature_dataset"]["rows"][0]["source_refs"][0]["path"].startswith("advanced_models/inputs/"))

    def test_cli_capacity_defer_preserves_all_leagues_without_repeated_fit(self):
        root = self.runtime(matches(10))
        for league in ("epl", "laliga"):
            (root / "data" / (league + ".json")).write_text(json.dumps({"matches": matches(10)}))
        config = root / "test_config.json"
        config.write_text(json.dumps({"competitions": [{"key": k, "mode": "domestic_baseline"}
            for k in ("serie_a", "epl", "laliga")]}))
        with patch("football_model.advanced_research.study", side_effect=CapacityDeferred({"reason": "test_actual_limit"})) as run, \
                patch("sys.stdout", new_callable=io.StringIO):
            code = main(["study", "--runtime", str(root), "--league", "all", "--config", str(config),
                         "--protocol", str(ROOT / "advanced-models-v1.json"), "--source-commit", "test-source"])
        self.assertEqual(code, 0)
        self.assertEqual(run.call_count, 1)
        summary = json.loads((root / "advanced_models/study_runs/latest.json").read_text())
        self.assertFalse(summary["research_full_completed"])
        self.assertEqual(summary["capacity_deferred_leagues"], 3)
        self.assertTrue(all(r["requested_history_matches"] == 10 for r in summary["results"]))

    @unittest.skipUnless(shutil.which("Rscript"), "real R is required for pinned upstream integration")
    def test_real_R_saved_coefficients_reproduce_neutral_and_feature_forecasts(self):
        training = matches(40)
        names = ["signal"]
        features = {m["match_id"]: {"home": {"signal": float(i % 5)},
                    "away": {"signal": float((i + 3) % 5)}} for i, m in enumerate(training)}
        fixtures = [{"match_id": 100 + i, "home_id": 1, "away_id": 2,
                     "kickoff": "2024-03-01T12:00:00Z", "neutral": bool(i)} for i in range(2)]
        values = {f["match_id"]: {"home": {"signal": 2.3}, "away": {"signal": 1.2}} for f in fixtures}
        result = fit_predict_goalmodel(training, fixtures, cutoff="2024-02-28T00:00:00Z",
            feature_names=names, training_features=features, prediction_features=values)
        self.assertEqual(result["status"], "ok", result)
        card = {"kind": "goalmodel_saved_fit", "fit_status": "fitted", "dc": False,
            "training_cutoff": result["cutoff"], "training_match_ids": result["training_match_ids"],
            "feature_names": names, "goalmodel_fit": result["fit"]}
        audit = verify_goalmodel_probes(card, result, values)
        self.assertTrue(audit["verified"])
        card["goalmodel_fit"]["parameters"]["beta"]["signal"] += 0.2
        with self.assertRaises(ModelDataError):
            verify_goalmodel_probes(card, result, values)


if __name__ == "__main__":
    unittest.main()
