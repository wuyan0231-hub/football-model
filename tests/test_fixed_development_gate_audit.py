"""R07 read-only boundaries and independent planning/denominator checks."""
import copy
from datetime import datetime, timedelta, timezone
import gzip
import importlib.util
import io
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("fixed_development_gate_audit", ROOT / "scripts/fixed_development_gate_audit.py")
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)
PROTOCOL = json.loads((ROOT / "advanced-models-v1.json").read_text())
DESIGN = json.loads((ROOT / "advanced-confirmation-v1.json").read_text())


def synthetic_historical_fingerprint():
    """Synthetic 50-entry scaffold; numerical files bind actual current bytes.

    These stand-ins exercise the immutable old reader contract. They are not
    evidence that current source has the historical 50-file identity.
    """
    value = audit.implementation_fingerprint()
    for index in range(50 - len(value["files"])):
        value["files"][f"synthetic-historical-only/dependency-{index}"] = "a" * 64
    value["sha256"] = audit.confirmation.digest(value["files"])
    return value


def score(value=2.0):
    return dict(scoreline_nll=value, one_x_two_brier=.4, total_goal_nll=1.4,
        home_tail_brier=.1, away_tail_brier=.2)


def research(n=64):
    rows, replays = [], {}
    for horizon in ("EARLY", "LINEUP"):
        forecasts = []
        for i in range(n):
            kickoff = datetime(2025, 1, 1, tzinfo=timezone.utc) + timedelta(days=i)
            cutoff = kickoff - timedelta(hours=10 if horizon == "EARLY" else 1)
            row = {"match_id": i + 1, "kickoff": kickoff.isoformat(), "cutoff_at": cutoff.isoformat(),
                "horizon": horizon, "scores": {"M0": score()}, "candidate_failures": {}}
            if horizon == "EARLY":
                other = score(1.97 + (i % 2) * .001)
                row["scores"].update(M_schedule=other, M_schedule_policy=copy.deepcopy(other))
            rows.append(row)
            forecasts.append({k: row[k] for k in ("match_id", "kickoff", "cutoff_at")})
        replays[horizon] = {"forecasts": forecasts, "failures": []}
    return {"scored_matches": rows, "replays": replays, "requested_matches": n,
        "requested_horizon_opportunities": len(rows)}


class ArithmeticTests(unittest.TestCase):
    def test_twenty_full_gates_and_unchanged_380_family_no_stateful_calls(self):
        with patch.object(audit.confirmation, "run", side_effect=AssertionError("stateful")), \
             patch.object(audit.confirmation, "activate", side_effect=AssertionError("activation")), \
             patch.object(audit.confirmation, "monitor", side_effect=AssertionError("monitor")):
            result = audit.audit_arithmetic(research(), PROTOCOL, DESIGN)
        self.assertEqual((len(result["gates"]), result["family_size"], result["metric_tests"]), (20, 380, 1900))
        self.assertEqual(result["counts"]["unique_fixtures"], 64)
        self.assertEqual(result["counts"]["horizon_rows"], 128)
        self.assertTrue(all(c["independent_plan_verified"] for c in result["independent_checks"]))
        gate = result["gates"][0]
        self.assertEqual(gate["status"], "passed")
        self.assertAlmostEqual(gate["mean_policy_differences"]["scoreline_nll"], -.0295)

    def test_original_warmup_missing_baseline_and_native_only_denominators(self):
        report = research()
        for horizon in ("EARLY", "LINEUP"):
            selected = [r for r in report["scored_matches"] if r["horizon"] == horizon]
            selected[0]["scores"] = {}
            selected[0]["candidate_failures"] = {"M0": {"code": "insufficient_training_history"}}
            selected[1]["scores"] = {}
            selected[1]["candidate_failures"] = {"M0": {"code": "unseen_team"}}
            if horizon == "EARLY":
                selected[2]["scores"] = {"M_schedule": score(1.8), "M_schedule_policy": score(1.8)}
                selected[3]["scores"] = {"M0": score(), "M_schedule_policy": score()}
        result = audit.audit_arithmetic(report, PROTOCOL, DESIGN)
        gate, check = result["gates"][0], result["independent_checks"][0]
        self.assertEqual((gate["all_development_opportunities"], gate["readiness_opportunities"], gate["paired_count"]), (64, 63, 61))
        self.assertEqual(gate["native_candidate_count"], 60)
        self.assertEqual(check["groups"], {"unforecast": 2, "native_only": 1, "fallback": 1, "native_paired": 60})
        self.assertEqual(gate["pair_coverage"], 61 / 63)

    def test_zero_native_floor_not_candidate_calendar_or_power_evidence(self):
        result = audit.audit_arithmetic(research(), PROTOCOL, DESIGN)
        gate, calendar = result["gates"][1], result["calendar"][1]
        self.assertEqual(gate["native_candidate_count"], 0)
        self.assertEqual((gate["sample_plan"]["required_scored_pairs"], gate["sample_plan"]["required_queue_opportunities"]), (60, 67))
        self.assertIsNone(calendar["candidate_based_calendar_estimate_days"])
        self.assertFalse(calendar["candidate_variance_evidence_available"])
        self.assertIsNotNone(calendar["raw_formula_calendar_projection"]["queue_days_at_readiness_cadence"])
        self.assertIsNone(calendar["absolute_completion_date"])

    def test_insufficient_pairs_preserve_null_plan(self):
        result = audit.audit_arithmetic(research(59), PROTOCOL, DESIGN)
        self.assertTrue(all(g["sample_plan"] is None for g in result["gates"]))
        self.assertTrue(all(c["raw_formula_calendar_projection"]["queue_days_at_readiness_cadence"] is None for c in result["calendar"]))

    def test_empty_report_preserves_all_twenty_failures_and_null_calendar(self):
        result = audit.audit_arithmetic(research(0), PROTOCOL, DESIGN)
        self.assertEqual(result["counts"]["blocked_gates"], 20)
        self.assertTrue(all(g["sample_plan"] is None for g in result["gates"]))
        self.assertIsNone(result["historical_cadence"]["EARLY"]["all_requested"]["opportunities_per_elapsed_day"])

    def test_changed_saved_fallback_rejected(self):
        report = research()
        report["scored_matches"][0]["scores"]["M_shots_policy"] = score(.1)
        with self.assertRaisesRegex(audit.AuditError, "saved_policy_changed"):
            audit.audit_arithmetic(report, PROTOCOL, DESIGN)

    def test_nonfinite_or_missing_metric_rejected(self):
        for change in (float("nan"), None):
            report = research()
            report["scored_matches"][0]["scores"]["M0"]["total_goal_nll"] = change
            with self.assertRaises(audit.confirmation.ConfirmationError):
                audit.audit_arithmetic(report, PROTOCOL, DESIGN)

    def test_full_fixture_universe_duplicate_and_missing_horizon_rejected(self):
        for mode in ("duplicate", "missing", "kickoff", "cutoff"):
            report = research(2)
            if mode == "duplicate":
                report["scored_matches"][1] = copy.deepcopy(report["scored_matches"][0])
            elif mode == "missing":
                report["replays"]["LINEUP"]["forecasts"].pop()
            elif mode == "kickoff":
                report["scored_matches"][0]["kickoff"] = "2025-01-03T00:00:00+00:00"
            else:
                report["scored_matches"][0]["cutoff_at"] = "2025-01-01T00:00:00"
            with self.subTest(mode=mode), self.assertRaises((audit.AuditError, ValueError)):
                audit.audit_arithmetic(report, PROTOCOL, DESIGN)

    def test_tail_metrics_original_event_route(self):
        direct = score()
        direct["home_tail_brier"] = direct["away_tail_brier"] = .25
        events = {k: v for k, v in direct.items() if not k.endswith("tail_brier")}
        events["events"] = {s + "_net_ge" + str(t): {"probability": .5, "observed": 0}
            for s in ("home", "away") for t in (2, 3)}
        self.assertEqual(audit.confirmation.project_metrics(events), audit.confirmation.project_metrics(direct))

    def test_independent_nontrivial_variances_match_original_native_plan(self):
        differences = [{m: ((i % 11) - 5) * (j + 1) / 100 for j, m in enumerate(audit.confirmation.METRICS)} for i in range(83)]
        expected = audit.confirmation.sample_plan(differences, DESIGN, 380)
        audit.bounded_equal(audit.independent_plan(differences, DESIGN, 380), expected)
        self.assertGreater(expected["required_scored_pairs"], 60)


class CalendarTests(unittest.TestCase):
    def test_actual_prior_EPL_calendar_golden(self):
        # Exact numeric receipt in attachment06, result.json SHA bf24f547...84d94.
        # This tests calendar reuse only, not unavailable original EPL score rows.
        gate = {"candidate_id": "M_schedule", "horizon": "EARLY", "paired_count": 608,
            "native_candidate_count": 568, "status": "blocked_pending_development",
            "sample_plan": {"required_scored_pairs": 2385, "required_queue_opportunities": 2650,
                "components": {"scoreline_nll": {"pilot_variance": .009262010835248736}}}}
        rates = {"all_requested": {"opportunities_per_elapsed_day": 820 / 787},
            "readiness_opportunities": {"opportunities_per_elapsed_day": 621 / (635 + 47 / 48)},
            "M0_scored_pairs_same_readiness_window": {"opportunities_per_elapsed_day": 608 / (635 + 47 / 48)}}
        result = audit.calendar_projection(gate, rates, DESIGN)
        self.assertAlmostEqual(result["candidate_based_calendar_estimate_days"], 2713.9207595276434)
        self.assertFalse(result["raw_formula_calendar_projection"]["fits_730_day_protocol_at_primary_historical_cadence"])
        self.assertAlmostEqual(result["raw_formula_calendar_projection"]["projected_terminal_elapsed_days_from_hypothetical_activation"], 2722.379092860977)

    def test_timezone_sorting_and_zero_span(self):
        rows = [{"kickoff": "2025-01-02T01:00:00+08:00"}, {"kickoff": "2025-01-01T18:00:00+00:00"}]
        self.assertAlmostEqual(audit.cadence(rows)["elapsed_days"], 1 / 24)
        self.assertIsNone(audit.cadence(rows[:1])["opportunities_per_elapsed_day"])
        self.assertIsNone(audit.cadence([])["opportunities_per_elapsed_day"])


class IdentityOutputTests(unittest.TestCase):
    def full_report(self, league="epl"):
        report = research(3)
        report.update(purpose="historical_development_only", protocol=PROTOCOL,
            protocol_hash=audit.confirmation.digest(PROTOCOL), league=league, source_commit="a" * 40,
            implementation_fingerprint=synthetic_historical_fingerprint())
        return report

    def test_synthetic_historical_closure_binds_real_numeric_code_and_rejects_tamper(self):
        report = self.full_report()
        summary = copy.deepcopy(report)
        fingerprint = report["implementation_fingerprint"]["sha256"]
        identities = audit.validate_identity(report, summary, fingerprint, PROTOCOL, ROOT)
        self.assertEqual(len(identities), len(audit.NUMERICAL_FILES))
        report["implementation_fingerprint"]["files"]["football_model/advanced_confirmation.py"] = "0" * 64
        with self.assertRaisesRegex(audit.AuditError, "saved_implementation_mismatch"):
            audit.validate_identity(report, summary, fingerprint, PROTOCOL, ROOT)

    def test_current_47_file_implementation_cannot_impersonate_old_fifty(self):
        report = self.full_report()
        current = audit.implementation_fingerprint()
        self.assertNotEqual(len(current["files"]), 50)
        report["implementation_fingerprint"] = current
        with self.assertRaisesRegex(audit.AuditError, "saved_implementation_mismatch"):
            audit.validate_identity(report, copy.deepcopy(report), current["sha256"], PROTOCOL, ROOT)

    def test_valid_self_digest_but_different_numeric_source_rejected(self):
        report = self.full_report()
        fp = report["implementation_fingerprint"]
        fp["files"]["football_model/advanced_confirmation.py"] = "0" * 64
        fp["sha256"] = audit.confirmation.digest(fp["files"])
        with self.assertRaisesRegex(audit.AuditError, "numerical_source_differs"):
            audit.validate_identity(report, copy.deepcopy(report), fp["sha256"], PROTOCOL, ROOT)

    def test_numeric_commit_identity_is_not_coerced_to_string(self):
        report = self.full_report()
        report["source_commit"] = int("1" * 40)
        with self.assertRaisesRegex(audit.AuditError, "report_source_invalid"):
            audit.validate_identity(report, copy.deepcopy(report),
                report["implementation_fingerprint"]["sha256"], PROTOCOL, ROOT)

    def test_gzip_numeric_witness_exact_roundtrip_and_no_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = audit.Output(Path(tmp) / "out", ROOT)
            witness = out.witness("test.jsonl.gz")
            row = {"match_id": 1, "policy_minus_M0": {"scoreline_nll": -.1}}
            witness.row(row)
            ref = witness.close(True)
            data = (out.path / ref["path"]).read_bytes()
            self.assertEqual(ref["sha256"], audit.sha(data))
            self.assertEqual(gzip.decompress(data), audit.raw(row))
            self.assertEqual(ref["uncompressed_sha256"], audit.sha(audit.raw(row)))
            with self.assertRaises(audit.AuditError):
                audit.Output(out.path, ROOT)

    def test_output_bounds_and_symlinks_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out = audit.Output(root / "out", ROOT)
            with self.assertRaisesRegex(audit.AuditError, "budget"):
                out.write_json("too-large.json", {"x": "x" * audit.JSON_LIMIT})
            self.assertFalse((out.path / "too-large.json").exists())
            (root / "linked").symlink_to(out.path, target_is_directory=True)
            with self.assertRaisesRegex(audit.AuditError, "symlink"):
                audit.Output(root / "linked" / "new", ROOT)

    def test_main_restores_once_preserves_success_when_second_league_fails(self):
        sys.path.insert(0, str(ROOT / "scripts"))
        import cloud_state
        import fixed_fixture_validation_audit
        current_before = audit.implementation_fingerprint()
        before = synthetic_historical_fingerprint()
        def fake_restore(repo, runtime, state, cloud):
            for league in ("epl", "championship"):
                report = self.full_report(league)
                if league == "championship":
                    report["scored_matches"][0]["cutoff_at"] = "invalid"
                payload = audit.raw(report)
                target = runtime / "reports" / (league + ".json")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(payload)
                pointer = runtime / "advanced_models" / league / audit.confirmation.digest(PROTOCOL) / "reports/latest.json"
                pointer.parent.mkdir(parents=True, exist_ok=True)
                summary = {k: report[k] for k in ("league", "source_commit", "protocol_hash", "implementation_fingerprint")}
                summary["full_report_ref"] = {"path": str(target.relative_to(runtime)), "sha256": audit.sha(payload)}
                pointer.write_bytes(audit.raw(summary))
            return {"synthetic_restore": True}
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(fixed_fixture_validation_audit, "restore_fixed", side_effect=fake_restore) as restore, \
             patch.object(cloud_state, "_git", return_value=subprocess.CompletedProcess([], 0, "a" * 40, "")), \
             patch.object(cloud_state, "save_state", side_effect=AssertionError("save")), \
             patch.object(audit.confirmation, "run", side_effect=AssertionError("run")), \
             redirect_stdout(io.StringIO()):
            output = Path(tmp) / "out"
            status = audit.main(["--repo", str(ROOT), "--state-commit", "a" * 40,
                "--implementation", before["sha256"], "--leagues", "epl,championship", "--output", str(output)])
            self.assertEqual(status, 1)
            self.assertEqual(restore.call_count, 1)
            final = json.loads((output / "run-final.json").read_text())
            self.assertEqual(final["completed_leagues"], ["epl"])
            self.assertEqual(final["status"], "audit_incomplete")
            self.assertEqual(final["full_family_size"], 380)
            self.assertTrue((output / "epl-gates.json").exists())
            self.assertEqual(final["results"][1]["failed_stage"], "recompute_all_twenty_original_gates")
            self.assertFalse(final["activation_performed"])
        self.assertEqual(audit.implementation_fingerprint(), current_before)


class WorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = (ROOT / ".github/workflows/fixed-development-gate-audit.yml").read_text()
        match = re.search(r"(?m)^          python - <<'VALIDATE_R07'\n(.*?)^          VALIDATE_R07$", cls.text, re.DOTALL | re.MULTILINE)
        assert match
        cls.validator = "\n".join(s[10:] for s in match.group(1).splitlines())

    def run_gate(self, state, leagues):
        return subprocess.run([sys.executable, "-c", self.validator],
            env={"AUDIT_STATE_COMMIT": state, "AUDIT_LEAGUES": leagues}, capture_output=True, timeout=5)

    def test_batch_identity_gate_before_network(self):
        self.assertEqual(self.run_gate("a" * 40, ",".join(key for key in DESIGN["registered_leagues"] if key != "unl")).returncode, 0)
        for state, leagues in (("main", "epl"), ("a" * 40, "epl,epl"), ("a" * 40, "epl;echo x"),
                ("a" * 40 + "\n", "epl"), ("a" * 40, "epl, unknown"), ("a" * 40, "unl")):
            with self.subTest(state=state, leagues=leagues):
                self.assertNotEqual(self.run_gate(state, leagues).returncode, 0)

    def test_read_only_no_api_activation_or_training_commands(self):
        self.assertIn("contents: read\n  actions: read", self.text)
        self.assertIn("FOOTBALL_API_KEY: ''", self.text)
        self.assertIn("timeout-minutes: 90", self.text)
        self.assertNotIn("schedule:", self.text)
        self.assertNotIn("contents: write", self.text)
        self.assertNotIn("--auto-activate", self.text)
        self.assertNotIn("--run-goalmodel", self.text)
        self.assertNotIn("cloud_state.py save", self.text)
        self.assertIn("a99395311e2eaf3cea2f06487aab564362021f23", self.text)


class BudgetRecoveryTests(unittest.TestCase):
    def execute_two_leagues(self, output, n=64):
        sys.path.insert(0, str(ROOT / "scripts"))
        import cloud_state
        import fixed_fixture_validation_audit
        current_before = audit.implementation_fingerprint()
        before = synthetic_historical_fingerprint()
        def fake_restore(repo, runtime, state, cloud):
            for league in ("epl", "championship"):
                report = research(n)
                report.update(purpose="historical_development_only", protocol=PROTOCOL,
                    protocol_hash=audit.confirmation.digest(PROTOCOL), league=league,
                    source_commit="a" * 40, implementation_fingerprint=before)
                payload = audit.raw(report)
                target = runtime / "reports" / (league + ".json")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(payload)
                pointer = runtime / "advanced_models" / league / audit.confirmation.digest(PROTOCOL) / "reports/latest.json"
                pointer.parent.mkdir(parents=True, exist_ok=True)
                summary = {k: report[k] for k in ("league", "source_commit", "protocol_hash", "implementation_fingerprint")}
                summary["full_report_ref"] = {"path": str(target.relative_to(runtime)), "sha256": audit.sha(payload)}
                pointer.write_bytes(audit.raw(summary))
            return {"synthetic_restore": True}
        with patch.object(fixed_fixture_validation_audit, "restore_fixed", side_effect=fake_restore) as restore, \
             patch.object(cloud_state, "_git", return_value=subprocess.CompletedProcess([], 0, "a" * 40, "")), \
             patch.object(cloud_state, "save_state", side_effect=AssertionError("save")), \
             patch.object(audit.confirmation, "run", side_effect=AssertionError("run")), \
             redirect_stdout(io.StringIO()):
            status = audit.main(["--repo", str(ROOT), "--state-commit", "a" * 40,
                "--implementation", before["sha256"], "--leagues", "epl,championship", "--output", str(output)])
        self.assertEqual(restore.call_count, 1)
        self.assertEqual(audit.implementation_fingerprint(), current_before)
        return status, json.loads((output / "run-final.json").read_text())

    def assert_manifest_actual_bytes(self, output):
        manifest = json.loads((output / "manifest.json").read_text())
        listed = {item["path"]: item for item in manifest["files"]}
        actual = {p.name for p in output.iterdir() if p.is_file()}
        self.assertEqual(set(listed), actual - {"manifest.json"})
        for name, ref in listed.items():
            data = (output / name).read_bytes()
            self.assertEqual(ref["bytes"], len(data))
            self.assertEqual(ref["sha256"], audit.sha(data))
        return manifest

    def test_main_json_budget_exhaustion_preserves_first_league_and_terminal(self):
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp)
            baseline = parent / "baseline"
            status, _ = self.execute_two_leagues(baseline)
            self.assertEqual(status, 0)
            # Actual encoded receipts establish a tight second-league budget.
            # Leave room for its witness but only half its full gate receipt.
            preceding = ["run-started.json", "epl-numeric-witness.jsonl.gz", "epl-gates.json",
                "checkpoint-01.json", "championship-numeric-witness.jsonl.gz"]
            regular = sum((baseline / n).stat().st_size for n in preceding) + (baseline / "championship-gates.json").stat().st_size // 2
            reserve = 64 * 1024
            output = parent / "limited"
            with patch.object(audit, "OUTPUT_LIMIT", regular + reserve), patch.object(audit, "TERMINAL_RESERVE", reserve):
                status, final = self.execute_two_leagues(output)
            self.assertEqual(status, 1)
            self.assertEqual(final["status"], "audit_incomplete")
            self.assertEqual(final["completed_leagues"], ["epl"])
            rejected = final["results"][1]
            self.assertEqual(rejected["error_code"], "output_budget_exceeded")
            self.assertEqual(rejected["failed_stage"], "write_league_gate_receipt")
            self.assertTrue((output / "epl-gates.json").exists())
            self.assertTrue((output / "championship-failure.json").exists())
            self.assertFalse((output / "championship-gates.json").exists())
            self.assertLessEqual(sum(p.stat().st_size for p in output.iterdir()), regular + reserve)
            self.assert_manifest_actual_bytes(output)

    def test_main_second_gzip_close_limit_indexes_partial_and_keeps_terminal(self):
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp)
            baseline = parent / "baseline"
            status, _ = self.execute_two_leagues(baseline, n=1)
            self.assertEqual(status, 0)
            witness_budget = (baseline / "epl-numeric-witness.jsonl.gz").stat().st_size + 32
            output = parent / "limited"
            with patch.object(audit, "WITNESS_LIMIT", witness_budget):
                status, final = self.execute_two_leagues(output, n=1)
            self.assertEqual(status, 1)
            self.assertEqual(final["completed_leagues"], ["epl"])
            rejected = final["results"][1]
            self.assertEqual(rejected["error_code"], "numeric_witness_budget_exceeded")
            self.assertEqual(rejected["failed_stage"], "finalize_numeric_witness")
            ref = rejected["partial_numeric_witness"]
            self.assertFalse(ref["complete"])
            self.assertIsNone(ref["complete_decompressed_rows"])
            self.assertEqual(ref["attempted_rows"], 20)
            self.assertNotIn("uncompressed_sha256", ref)
            data = (output / ref["path"]).read_bytes()
            self.assertEqual(ref["sha256"], audit.sha(data))
            self.assertEqual(ref["bytes"], len(data))
            self.assertLessEqual(len(data) + (output / "epl-numeric-witness.jsonl.gz").stat().st_size, witness_budget)
            manifest = self.assert_manifest_actual_bytes(output)
            self.assertIn(ref, manifest["files"])

    def test_partial_close_idempotent_and_terminal_cannot_exceed_hard_total(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(audit, "OUTPUT_LIMIT", 2048), \
             patch.object(audit, "TERMINAL_RESERVE", 1024), patch.object(audit, "WITNESS_LIMIT", 32):
            out = audit.Output(Path(tmp) / "out", ROOT)
            witness = out.witness("witness.jsonl.gz")
            witness.row({"match_id": 1, "values": list(range(100))})
            with self.assertRaisesRegex(audit.AuditError, "numeric_witness_budget_exceeded"):
                witness.close(True)
            ref = witness.close(False)
            self.assertEqual(witness.close(False), ref)
            self.assertEqual(out.files, [ref])
            self.assertFalse(ref["complete"])
            self.assertEqual(ref["sha256"], audit.sha((out.path / ref["path"]).read_bytes()))
            out.write_json("final.json", {"status": "audit_incomplete", "partial": ref}, terminal=True)
            with self.assertRaisesRegex(audit.AuditError, "output_budget_exceeded"):
                out.write_json("too-big.json", {"text": "x" * 2048}, terminal=True)
            self.assertFalse((out.path / "too-big.json").exists())
            self.assertEqual(out.bytes, sum(p.stat().st_size for p in out.path.iterdir()))
            self.assertLessEqual(out.bytes, 2048)


if __name__ == "__main__":
    unittest.main()
