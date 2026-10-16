"""Stale summaries may refuse identity; they can never authorize activation."""
import copy
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from football_model import advanced_confirmation as confirmation
from football_model import advanced_storage
from football_model.advanced_research import implementation_fingerprint
from tests import test_advanced_confirmation as existing


def stale_fingerprint(current):
    value = copy.deepcopy(current)
    name = next(iter(value["files"]))
    value["files"][name] = "0" * 64 if value["files"][name] != "0" * 64 else "1" * 64
    value["sha256"] = confirmation.digest(value["files"])
    return value


class SummaryIdentityTests(unittest.TestCase):
    def setUp(self):
        self.fixture = existing.ConfirmationTests("test_no_report_is_blocked_development_not_waiting_results")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.runtime, self.design, self.protocol = self.fixture.runtime, self.fixture.design, existing.advanced()
        report_path, card_path = self.fixture.development_refs()
        self.full = confirmation._read(report_path)
        self.full["model_cards"] = {"M2": confirmation._ref(self.runtime, card_path)}
        self.current = implementation_fingerprint()
        self.stale = stale_fingerprint(self.current)

    def publish(self, summary_fingerprint, *, full_fingerprint=None):
        report = copy.deepcopy(self.full)
        if full_fingerprint is not None:
            report["implementation_fingerprint"] = copy.deepcopy(full_fingerprint)
        saved = confirmation._save(self.runtime, "reports", report)
        summary = {key: value for key, value in report.items()
                   if key not in ("replays", "feature_datasets", "experiments", "scored_matches")}
        if summary_fingerprint is None:
            summary.pop("implementation_fingerprint", None)
        else:
            summary["implementation_fingerprint"] = copy.deepcopy(summary_fingerprint)
        summary["full_report_ref"] = confirmation._ref(self.runtime, saved)
        path = self.runtime / "advanced_models/serie_a" / confirmation.digest(self.protocol) / "reports/latest.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(summary))
        return path, summary

    def run_trial(self):
        return confirmation.run(self.runtime, "serie_a", self.protocol, self.design,
            auto_activate=True, clock=lambda: existing.NOW, row_loader=lambda _: [])

    def assert_stale(self, report):
        trial = report["trials"][0]
        self.assertEqual(trial["status"], "blocked_pending_development")
        self.assertEqual(trial["gate"]["failures"], ["implementation_changed_requires_fresh_development_study"])
        self.assertFalse(trial["confirmation_active"])
        self.assertFalse(trial["scores_accessed"])
        return trial

    def test_valid_stale_summary_skips_full_graph_and_keeps_original_block(self):
        _, summary = self.publish(self.stale, full_fingerprint=self.stale)
        with patch.object(advanced_storage, "load_report", side_effect=AssertionError("must not load old graph")) as loader:
            trial = self.assert_stale(self.run_trial())
        loader.assert_not_called()
        self.assertEqual(trial["gate"]["development_ref"], summary["full_report_ref"])
        self.assertEqual(trial["gate"]["full_report_verification"], "not_performed_identity_rejected")

    def test_stale_summary_current_full_report_is_only_conservatively_rejected(self):
        self.publish(self.stale, full_fingerprint=self.current)
        with patch.object(advanced_storage, "load_report", side_effect=AssertionError("no activation from summary")) as loader:
            self.assert_stale(self.run_trial())
        loader.assert_not_called()

    def test_current_summary_stale_full_report_still_loads_and_rejects(self):
        self.publish(self.current, full_fingerprint=self.stale)
        with patch.object(advanced_storage, "load_report", wraps=advanced_storage.load_report) as loader:
            trial = self.assert_stale(self.run_trial())
        self.assertGreater(loader.call_count, 0)
        self.assertNotIn("full_report_verification", trial["gate"])

    def test_current_summary_uses_complete_sources_and_real_score_verification(self):
        self.publish(self.current)
        with patch.object(advanced_storage, "load_report", wraps=advanced_storage.load_report) as loader, \
             patch.object(confirmation, "verify_development", wraps=confirmation.verify_development) as verify:
            report = self.run_trial()
        self.assertGreaterEqual(loader.call_count, 3)  # full report, card, frozen match source
        verify.assert_called_once()
        self.assertEqual(report["trials"][0]["status"], "active_waiting_real_results")
        self.assertFalse(report["trials"][0]["scores_accessed"])

    def test_missing_or_internally_invalid_fingerprint_cannot_skip_full_validation(self):
        malformed = copy.deepcopy(self.stale)
        malformed["sha256"] = "f" * 64  # Does not bind its declared file hashes.
        for fingerprint in (None, {}, malformed):
            with self.subTest(fingerprint=fingerprint):
                self.publish(fingerprint)
                with patch.object(advanced_storage, "load_report", side_effect=ValueError("corrupt actual graph")) as loader:
                    report = self.run_trial()
                loader.assert_called_once()
                self.assertEqual(report["trials"][0]["status"], "blocked_integrity_or_execution")
                self.assertFalse(report["trials"][0]["confirmation_active"])

    def test_wrong_summary_schema_cannot_use_early_rejection(self):
        path, summary = self.publish(self.stale)
        summary["schema_version"] = "unknown-summary"
        path.write_text(json.dumps(summary))
        with patch.object(advanced_storage, "load_report", side_effect=ValueError("corrupt actual graph")) as loader:
            report = self.run_trial()
        loader.assert_called_once()
        self.assertEqual(report["trials"][0]["status"], "blocked_integrity_or_execution")


class FullFamilySummaryIdentityTests(unittest.TestCase):
    def test_all_380_registered_trials_remain_with_no_stale_graph_loads(self):
        root = Path(__file__).resolve().parents[1]
        protocol = json.loads((root / "advanced-models-v1.json").read_text())
        design = json.loads((root / "advanced-confirmation-v1.json").read_text())
        stale = stale_fingerprint(implementation_fingerprint())
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory)
            for league in design["registered_leagues"]:
                path = runtime / "advanced_models" / league / confirmation.digest(protocol) / "reports/latest.json"
                path.parent.mkdir(parents=True)
                path.write_text(json.dumps({"schema_version": "advanced-research-v1", "league": league,
                    "purpose": "historical_development_only", "protocol": protocol,
                    "protocol_hash": confirmation.digest(protocol), "implementation_fingerprint": stale,
                    "full_report_ref": {"path": "old/research.json", "sha256": "a" * 64}}))
            with patch.object(advanced_storage, "load_report", side_effect=AssertionError("old graph must stay unread")) as loader:
                reports = [confirmation.run(runtime, league, protocol, design, auto_activate=True,
                    clock=lambda: datetime(2026, 10, 16, tzinfo=timezone.utc),
                    row_loader=lambda _: self.fail("inactive trials must not read future scores"))
                    for league in design["registered_leagues"]]
            loader.assert_not_called()
            self.assertEqual(len(reports), 19)
            self.assertEqual(sum(len(report["trials"]) for report in reports), 380)
            self.assertEqual({report["family_size"] for report in reports}, {380})
            self.assertTrue(all(not trial["confirmation_active"] for report in reports for trial in report["trials"]))
            self.assertTrue(all(trial["gate"]["full_report_verification"] == "not_performed_identity_rejected"
                                for report in reports for trial in report["trials"]))


if __name__ == "__main__":
    unittest.main()
