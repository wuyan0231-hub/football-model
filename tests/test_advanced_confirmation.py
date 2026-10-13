"""Meaningful confirmation gates: no retrospective enrollment or result peeking."""
import copy
from datetime import datetime, timedelta, timezone
import json
import math
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from football_model.advanced_confirmation import (
    ConfirmationError, METRICS, _read, _ref, _save, activate, development_gate,
    digest, monitor, project_metrics, run, sample_plan, verify_development,
    verify_durable_capture as actual_durable_verifier,
)

NOW = datetime(2026, 10, 13, 2, tzinfo=timezone.utc)


def design():
    return _read(Path(__file__).resolve().parent.parent / "advanced-confirmation-v1.json")


def advanced():
    return {"league": "serie_a", "candidates": {"M2": {"horizon": "EARLY", "groups": ["schedule"]}}}


def score(main, brier=0.5, total=1.5, tail=0.1):
    return {"scoreline_nll": main, "one_x_two_brier": brier, "total_goal_nll": total,
            "home_tail_brier": tail, "away_tail_brier": tail}


def research(n=80, improvement=0.03):
    return {"protocol_sha256": digest(advanced()), "scored_matches": [
        {"match_id": index, "kickoff": "2025-01-01T12:00:00Z", "horizon": "EARLY",
         "scores": {"M0": score(2), "M2": score(2 - improvement + (index % 2) * .002)}}
        for index in range(n)]}


class ConfirmationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path.cwd())
        self.addCleanup(self.temp.cleanup)
        self.runtime = Path(self.temp.name)
        self.root = self.runtime / "trial"
        self.design = design()
        durable = patch("football_model.advanced_confirmation.verify_durable_capture", return_value={"verified": True})
        durable.start()
        self.addCleanup(durable.stop)

    def development_refs(self):
        from football_model.model import score_distribution, MODEL_VERSION
        from football_model.feature_research import score_forecast
        from football_model.feature_correction import predict_correction, CORRECTION_VERSION
        from football_model.advanced_research import candidate_feature_names
        names = candidate_feature_names(advanced(), "M2")
        base_model = {"model_version": MODEL_VERSION, "fit_status": "converged", "graph_connected": True,
            "training_cutoff": "2024-01-01T00:00:00Z", "training_data_sha256": "synthetic-unit-fixture",
            "intercept": 0., "home_advantage": 0., "low_sample_team_threshold": 5,
            "team_parameters": {str(i): {"name": str(i), "training_matches": 80, "attack": 0., "defence": 0.} for i in (1, 2)}}
        correction = {"model_version": CORRECTION_VERSION, "fit_status": "converged", "intercept_added": False,
            "shared_home_away_coefficients": True, "feature_names": names,
            "coefficients": {k: (-math.log(2) if k == names[0] else 0.) for k in names},
            "feature_scaling": {k: {"mean": 0., "scale": 1., "constant_in_training": False} for k in names},
            "training_cutoff": "2024-01-01T00:00:00Z", "training_match_ids": []}
        correction["correction_model_sha256"] = digest(correction)
        forecasts, rows, matches, features = [], [], [], []
        for index in range(1, 81):
            fixture = {"match_id": index, "kickoff": "2025-01-01T12:00:00Z", "home_goals": 0, "away_goals": 0,
                "home_id": 1, "away_id": 2, "neutral": False}
            base = score_distribution(base_model, 1, 2, fixture["kickoff"], False)
            base.update(match_id=index, cutoff_at="2025-01-01T02:00:00Z")
            values = {k: 1. for k in names}
            candidate = predict_correction(correction, base, values, values)
            features.append({"match_id": index, "home": values, "away": values})
            matches.append(fixture)
            forecasts.append({**fixture, "baseline_prediction": base, "block": 0, "cutoff_at": base["cutoff_at"]})
            base_score = score_forecast(base, fixture, extended_diagnostics=True)
            candidate_score = score_forecast(candidate, fixture, extended_diagnostics=True)
            rows.append({"match_id": index, "kickoff": fixture["kickoff"], "horizon": "EARLY",
                "scores": {"M0": base_score, "M2": candidate_score, "M2_policy": candidate_score},
                "predictions": {"M2": candidate}})
        source = _save(self.runtime, "inputs", {"matches": matches})
        sources = [_ref(self.runtime, source)]
        report = {"schema_version": "advanced-research-v1", "league": "serie_a", "purpose": "historical_development_only",
            "protocol": advanced(), "protocol_sha256": digest(advanced()), "generated_at": NOW.isoformat(),
            "source_commit": "test-commit", "source_refs": sources,
            "replays": {"EARLY": {"forecasts": forecasts, "failures": [], "baseline_fits": [{"block": 0, "status": "fitted", "model": base_model}]}},
            "experiments": [{"horizon": "EARLY", "block": 0, "fits": {"M2": {"model": correction}}}],
            "feature_datasets": {"EARLY": {"rows": features}}, "scored_matches": rows}
        card = {"candidate_id": "M2", "fit_status": "fitted", "league": "serie_a", "horizon": "EARLY",
            "protocol_hash": digest(advanced()), "training_cutoff": NOW.isoformat(), "generated_at": NOW.isoformat(),
            "source_commit": "test-commit", "source_refs": sources}
        card["model_hash"] = digest(card)
        return _save(self.runtime, "reports", report), _save(self.runtime, "models", card)

    def activation(self):
        report, card = self.development_refs()
        value, gate = activate(self.runtime, self.root, design=self.design, advanced_protocol=advanced(),
            league="serie_a", candidate_id="M2", horizon="EARLY", development_ref=_ref(self.runtime, report),
            card_ref=_ref(self.runtime, card), family_size=1, now=NOW)
        self.assertEqual(gate["status"], "passed")
        return value

    def queue(self, activation, n=None):
        n = n or activation["sample_plan"]["required_queue_opportunities"]
        start = datetime.fromisoformat(activation["future_start"].replace("Z", "+00:00"))
        return [{"fixture_id": i + 1000, "league": "serie_a", "candidate_id": "M2", "horizon": "EARLY",
                 "first_observed_at": (NOW + timedelta(hours=1)).isoformat(),
                 "kickoff": (start + timedelta(days=1, minutes=i)).isoformat(),
                 "captured_at": (start + timedelta(hours=14, minutes=i)).isoformat(),
                 "protocol_sha256": digest(advanced()), "candidate_model_sha256": "a" * 64,
                 "confirmation_eligible": True, "confirmation_activation_sha256": digest(activation),
                 "confirmation_activation_ref": _read(self.root / "activation.json"),
                 "result_available_at": (start + timedelta(days=2)).isoformat(),
                 "status": "candidate_scored", "baseline_metrics": score(2),
                 "candidate_metrics": score(1.96 + (i % 2) * .002),
                 "strategy_metrics": score(1.96 + (i % 2) * .002)} for i in range(n)]

    def test_worse_current_development_is_blocked(self):
        gate = development_gate(research(improvement=-.01), "M2", "EARLY", self.design, 1)
        self.assertEqual(gate["status"], "blocked_pending_development")
        self.assertIn("development_minimum_primary_gain_not_met", gate["failures"])

    def test_development_uses_allqueue_fallback_and_rejects_selective_coverage(self):
        report = research()
        for row in report["scored_matches"][:30]:
            del row["scores"]["M2"]
        gate = development_gate(report, "M2", "EARLY", self.design, 1)
        self.assertEqual(gate["paired_count"], 80)
        self.assertEqual(gate["native_candidate_count"], 50)
        self.assertIn("development_candidate_coverage_below_gate", gate["failures"])
        self.assertAlmostEqual(gate["mean_policy_differences"]["scoreline_nll"], -.029 * 50 / 80)

    def test_duplicate_fixture_cannot_create_pilot_power(self):
        report = research()
        report["scored_matches"].extend(copy.deepcopy(report["scored_matches"]))
        gate = development_gate(report, "M2", "EARLY", self.design, 1)
        self.assertEqual(gate["unique_fixture_count"], 80)
        self.assertEqual(gate["paired_count"], 80)
        self.assertIn("duplicate_or_missing_development_fixture", gate["failures"])

    def test_missing_metric_cannot_be_assumed_zero(self):
        with self.assertRaises(ConfirmationError):
            project_metrics({"scoreline_nll": 2, "one_x_two_brier": .5, "total_goal_nll": 1.5})

    def test_power_responds_to_variance_family_and_effect(self):
        pilot = [{k: (1 if i % 2 else -1) * .2 for k in METRICS} for i in range(80)]
        one = sample_plan(pilot, self.design, 1)
        many = sample_plan(pilot, self.design, 10)
        self.assertGreater(many["required_scored_pairs"], one["required_scored_pairs"])
        self.assertLess(many["per_test_alpha"], one["per_test_alpha"])
        wider = copy.deepcopy(self.design)
        wider["noninferiority_margins"] = {k: 1.0 for k in wider["noninferiority_margins"]}
        wider["minimum_effect_nll"] = .1
        self.assertLess(sample_plan(pilot, wider, 1)["required_scored_pairs"], one["required_scored_pairs"])
        self.assertGreater(one["components"]["scoreline_nll"]["planning_variance_upper"], .04)

    def test_activation_is_immutable_and_future_start_cannot_roll(self):
        activation = self.activation()
        saved = _read(self.root / "activation.json")
        report, card = self.development_refs()
        later, _ = activate(self.runtime, self.root, design=self.design, advanced_protocol=advanced(),
            league="serie_a", candidate_id="M2", horizon="EARLY", development_ref=_ref(self.runtime, report),
            card_ref=_ref(self.runtime, card), family_size=1, now=NOW + timedelta(days=20))
        self.assertEqual(activation, later)
        self.assertEqual(saved, _read(self.root / "activation.json"))
        self.assertEqual(activation["future_start"], "2026-10-14T02:00:00Z")

    def test_bad_reference_cannot_activate(self):
        ref = {"path": "../secret.json", "sha256": "x"}
        with self.assertRaises(ConfirmationError):
            activate(self.runtime, self.root, design=self.design, advanced_protocol=advanced(), league="serie_a",
                     candidate_id="M2", horizon="EARLY", development_ref=ref, card_ref=ref, family_size=1, now=NOW)

    def test_no_daily_result_peeking_and_all_failures_enroll(self):
        activation = self.activation()
        rows = self.queue(activation)
        rows[0]["status"] = "missed_window"
        rows[0]["candidate_metrics"] = None
        def forbidden():
            self.fail("daily monitor opened scores before terminal")
        report = monitor(self.runtime, self.root, activation, rows, now=NOW + timedelta(hours=2), scored_rows_loader=forbidden)
        self.assertEqual(report["enrolled_unique_fixtures"], len(rows))
        self.assertIsNone(report["metrics"])
        self.assertFalse(report["scores_accessed"])

    def test_pre_activation_queues_and_retroactive_enrollment_rejected(self):
        activation = self.activation()
        rows = self.queue(activation, 3)
        rows[0]["first_observed_at"] = (NOW - timedelta(days=1)).isoformat()
        report = monitor(self.runtime, self.root, activation, rows, now=NOW + timedelta(days=20))
        self.assertEqual(report["enrolled_unique_fixtures"], 2)
        self.assertEqual(report["excluded_metadata_counts"]["pre_activation_queue_not_retroactively_enrolled"], 1)
        self.assertEqual(report["excluded_metadata_counts"]["enrollment_not_saved_before_kickoff"], 2)

    def test_terminal_has_fixed_cohort_and_never_promotes_or_retests(self):
        activation = self.activation()
        rows = self.queue(activation)
        monitor(self.runtime, self.root, activation, rows, now=NOW + timedelta(hours=2))
        result = monitor(self.runtime, self.root, activation, rows, now=NOW + timedelta(days=15), scored_rows_loader=lambda: rows)
        self.assertEqual(result["status"], "terminal_passed_probability_confirmation")
        self.assertFalse(result["production_promotion"])
        self.assertFalse(result["roi_claim"])
        def forbidden():
            self.fail("terminal outcomes were peeked again")
        again = monitor(self.runtime, self.root, activation, rows, now=NOW + timedelta(days=20), scored_rows_loader=forbidden)
        self.assertEqual(result, again)

    def test_damage_does_not_turn_previous_candidate_score_into_fallback(self):
        activation = self.activation()
        rows = self.queue(activation)
        monitor(self.runtime, self.root, activation, rows, now=NOW + timedelta(hours=2))
        for row in rows[:20]:
            row["strategy_metrics"] = None
            row["status"] = "candidate_parent_damaged"
        result = monitor(self.runtime, self.root, activation, rows, now=NOW + timedelta(days=15), scored_rows_loader=lambda: rows)
        self.assertEqual(result["status"], "terminal_not_confirmed")
        self.assertIn("terminal_pair_coverage_failed", result["failures"])
        self.assertEqual(result["all_queue_outcomes"]["no_legal_policy_pair"], 20)

    def test_terminal_primary_pass_does_not_override_guardrail(self):
        activation = self.activation()
        rows = self.queue(activation)
        monitor(self.runtime, self.root, activation, rows, now=NOW + timedelta(hours=2))
        for row in rows:
            row["strategy_metrics"]["one_x_two_brier"] += .03
        result = monitor(self.runtime, self.root, activation, rows, now=NOW + timedelta(days=15), scored_rows_loader=lambda: rows)
        self.assertEqual(result["status"], "terminal_not_confirmed")
        self.assertTrue(result["metrics"]["scoreline_nll"]["passed"])
        self.assertIn("terminal_metric_failed_one_x_two_brier", result["failures"])

    def test_corrupt_enrollment_detected(self):
        activation = self.activation()
        rows = self.queue(activation)
        monitor(self.runtime, self.root, activation, rows, now=NOW + timedelta(hours=2))
        cohort = _read(self.root / "cohort.json")
        cohort[0]["fixture_id"] = 99
        (self.root / "cohort.json").write_text(json.dumps(cohort))
        with self.assertRaises(ConfirmationError):
            monitor(self.runtime, self.root, activation, rows, now=NOW + timedelta(hours=3))

    def test_deleted_cohort_entry_cannot_be_silently_replaced(self):
        activation = self.activation()
        rows = self.queue(activation)
        monitor(self.runtime, self.root, activation, rows, now=NOW + timedelta(hours=2))
        cohort = _read(self.root / "cohort.json")
        (self.root / "cohort.json").write_text(json.dumps(cohort[1:]))
        with self.assertRaisesRegex(ConfirmationError, "cohort_entries_deleted"):
            monitor(self.runtime, self.root, activation, rows, now=NOW + timedelta(hours=3))

    def test_pretend_better_development_scores_fail_actual_recomputation(self):
        report_path, card_path = self.development_refs()
        report, card = _read(report_path), _read(card_path)
        report["scored_matches"][0]["scores"]["M2"]["scoreline_nll"] = .001
        with self.assertRaisesRegex(ConfirmationError, "failed_recomputation"):
            verify_development(self.runtime, report, card, protocol=advanced(), league="serie_a",
                candidate_id="M2", horizon="EARLY", now=NOW)

    def test_engineering_capture_cannot_be_promoted_by_later_activation(self):
        activation = self.activation()
        rows = self.queue(activation)
        monitor(self.runtime, self.root, activation, rows, now=NOW + timedelta(hours=2))
        for row in rows:
            row["confirmation_eligible"] = False
        result = monitor(self.runtime, self.root, activation, rows, now=NOW + timedelta(days=15), scored_rows_loader=lambda: rows)
        self.assertEqual(result["paired_fixtures"], 0)
        self.assertEqual(result["status"], "terminal_not_confirmed")
        self.assertEqual(result["all_queue_outcomes"]["capture_not_frozen_for_this_confirmation"], len(rows))

    def test_initial_warmup_separate_from_unknown_team_failure(self):
        report = research()
        report["scored_matches"] += [
            {"match_id": 100, "horizon": "EARLY", "scores": {}, "candidate_failures": {"M0": {"code": "insufficient_training_history"}}},
            {"match_id": 101, "horizon": "EARLY", "scores": {}, "candidate_failures": {"M0": {"code": "unseen_team"}}}]
        gate = development_gate(report, "M2", "EARLY", self.design, 1)
        self.assertEqual(gate["all_development_opportunities"], 82)
        self.assertEqual(gate["readiness_opportunities"], 81)
        self.assertEqual(gate["explicit_initial_warmup_opportunities"], 1)
        self.assertAlmostEqual(gate["pair_coverage"], 80 / 81)

    def test_all_scope_uses_fixed_registered_leagues_and_goalmodels(self):
        protocol = advanced()
        protocol["leagues"] = "all"
        protocol["goalmodel_candidates"] = ["G0_full", "G0", "G1"]
        result = run(self.runtime, "serie_a", protocol, self.design, auto_activate=True,
            clock=lambda: NOW, row_loader=lambda _: [])
        self.assertEqual(result["family_size"], 4 * 19)
        self.assertEqual(len(result["trials"]), 4)
        with self.assertRaisesRegex(ConfirmationError, "league_not_in_frozen"):
            run(self.runtime, "unknown", protocol, self.design, clock=lambda: NOW, row_loader=lambda _: [])

    def test_auto_activation_consumes_real_frozen_report(self):
        report_path, card_path = self.development_refs()
        report = _read(report_path)
        report["model_cards"] = {"M2": _ref(self.runtime, card_path)}
        saved = _save(self.runtime, "reports", report)
        namespace = self.runtime / "advanced_models" / "serie_a" / digest(advanced()) / "reports"
        namespace.mkdir(parents=True)
        (namespace / "latest.json").write_text(json.dumps({"full_report_ref": _ref(self.runtime, saved)}))
        calls = []
        result = run(self.runtime, "serie_a", advanced(), self.design, auto_activate=True, clock=lambda: NOW,
                     row_loader=lambda include: calls.append(include) or [])
        self.assertEqual(result["trials"][0]["status"], "active_waiting_real_results")
        self.assertEqual(calls, [False])

    def test_self_attested_durability_cannot_substitute_git_evidence(self):
        from types import SimpleNamespace
        anchor = {"path": "fake/first.json", "sha256": "b" * 64}
        fake = {"receipt_ref": anchor, "state_commit": "a" * 40, "verified": True,
                "durable_saved_at": NOW.isoformat()}
        path = _save(self.runtime, "durability", fake)
        row = {"first_capture_anchor_ref": anchor, "durability_ref": _ref(self.runtime, path)}
        with patch("subprocess.run", return_value=SimpleNamespace(returncode=0)), \
                patch("football_model.advanced_durability.prove", side_effect=ValueError("no_actual_git_closure")):
            with self.assertRaisesRegex(ValueError, "no_actual_git_closure"):
                actual_durable_verifier(self.runtime, row, NOW)

    def test_missing_durable_proof_is_not_accepted(self):
        with self.assertRaisesRegex(ConfirmationError, "invalid_reference"):
            actual_durable_verifier(self.runtime, {"durability_ref": None}, NOW)

    def test_deleted_activation_pointer_cannot_restart_trial(self):
        self.activation()
        (self.root / "activation.json").unlink()
        with self.assertRaisesRegex(ConfirmationError, "cannot_restart"):
            self.activation()

    def test_selfconsistent_candidate_matrix_and_score_tamper_rejected(self):
        from football_model.model import _distribution_from_rates
        from football_model.feature_research import score_forecast
        report_path, card_path = self.development_refs()
        report, card = _read(report_path), _read(card_path)
        forged = _distribution_from_rates(.01, .01)
        row = report["scored_matches"][0]
        row["predictions"]["M2"] = forged
        row["scores"]["M2"] = row["scores"]["M2_policy"] = score_forecast(forged, {"home_goals": 0, "away_goals": 0}, extended_diagnostics=True)
        with self.assertRaisesRegex(ConfirmationError, "development_model_prediction_does_not_recompute"):
            verify_development(self.runtime, report, card, protocol=advanced(), league="serie_a",
                candidate_id="M2", horizon="EARLY", now=NOW)

    def test_missed_first_queue_slots_consumed_never_replaced(self):
        activation = self.activation()
        rows = self.queue(activation)
        late_monitor = datetime.fromisoformat(rows[1]["kickoff"]) + timedelta(seconds=30)
        report = monitor(self.runtime, self.root, activation, rows, now=late_monitor)
        cohort = _read(self.root / "cohort.json")
        self.assertEqual(len(cohort), len(rows))
        self.assertEqual([r["enrollment_status"] for r in cohort[:3]], ["missed_enrollment", "missed_enrollment", "prospective"])
        result = monitor(self.runtime, self.root, activation, rows, now=NOW + timedelta(days=15), scored_rows_loader=lambda: rows)
        self.assertEqual(result["all_queue_outcomes"]["missed_enrollment"], 2)
        self.assertEqual(result["paired_fixtures"], len(rows) - 2)

    def test_no_report_is_blocked_development_not_waiting_results(self):
        result = run(self.runtime, "serie_a", advanced(), self.design, auto_activate=True,
                     clock=lambda: NOW, row_loader=lambda scores: self.fail("should not open observations"))
        self.assertEqual(result["trials"][0]["status"], "blocked_pending_development")
        self.assertFalse(result["trials"][0]["confirmation_active"])


if __name__ == "__main__":
    unittest.main()
