"""Actual M0 payload with a stale G1 local must not affect study scoring."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from football_model.advanced_research import predict_saved_goalmodel
from football_model.feature_research import score_forecast


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('verify_unl_probability_repair',
    ROOT / 'scripts/verify_unl_probability_repair.py')
HARNESS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(HARNESS)
GOAL_CALLSITE = 'row["scores"][candidate] = score_forecast(prediction, f, extended_diagnostics=True)'


class UnlRepairHarnessTests(unittest.TestCase):
    def baseline_case(self):
        return json.loads((ROOT / 'tests/fixtures/unl-harness-stale-candidate-m0-1545612.json').read_text())

    def goal_case(self):
        case = json.loads((ROOT / 'tests/fixtures/unl-goalmodel-roundoff-1545612.json').read_text())
        prediction = predict_saved_goalmodel(case['card'], case['fixture'], case['features'], case['as_of'])
        return case, prediction

    def test_real_m0_with_stale_g1_locals_retains_original_score(self):
        case = self.baseline_case()
        scored = score_forecast(case['prediction'], case['fixture'], extended_diagnostics=True)
        target_capture = Mock()
        for source_line in (case['source_line'], GOAL_CALLSITE):
            receipt = {'diagnostic_capture_errors': []}
            def capture():
                if HARNESS.is_repaired_target(case['prediction'], case['fixture'], case['stale_locals'], source_line):
                    target_capture()
            result = HARNESS.preserve_scored_output(scored, capture, receipt)
            self.assertIs(result, scored)
            self.assertEqual(receipt['diagnostic_capture_errors'], [])
        target_capture.assert_not_called()

    def test_actual_g1_requires_its_candidate_callsite(self):
        case, prediction = self.goal_case()
        local = self.baseline_case()['stale_locals']
        self.assertTrue(HARNESS.is_repaired_target(prediction, case['fixture'], local, GOAL_CALLSITE))
        for source_line in (self.baseline_case()['source_line'],
                'row["scores"]["COLD_START"] = score_forecast(prediction, f, extended_diagnostics=True)',
                'row["matched_pool_ablation_scores"][key]["parent_on_added_training_pool"] = score_forecast(prediction, f)',
                ''):
            with self.subTest(source_line=source_line):
                self.assertFalse(HARNESS.is_repaired_target(prediction, case['fixture'], local, source_line))

    def test_capture_exception_is_diagnostic_and_cannot_interrupt_scoring(self):
        case, prediction = self.goal_case()
        scored = score_forecast(prediction, case['observed'], extended_diagnostics=True)
        receipt = {'diagnostic_capture_errors': [], 'target_repair_verified': False}
        capture = Mock(side_effect=ValueError('missing R diagnostic context'))
        result = HARNESS.preserve_scored_output(scored, capture, receipt)
        self.assertIs(result, scored)
        capture.assert_called_once_with()
        self.assertFalse(receipt['target_repair_verified'])
        self.assertEqual(receipt['diagnostic_capture_errors'][0]['stage'], 'score_success_capture')
        self.assertEqual(receipt['diagnostic_capture_errors'][0]['class'], 'ValueError')

    def test_report_export_checks_disk_before_writing_and_preserves_bytes(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as temporary:
            runtime = Path(temporary) / 'runtime'
            runtime.mkdir()
            path = runtime / 'report.json'
            encoded = b'{"value": 1}\n'
            path.write_bytes(encoded)
            reference = {'path': 'report.json', 'sha256': HARNESS.hashlib.sha256(encoded).hexdigest()}
            output = Path(temporary) / 'output'
            from unittest.mock import patch
            from types import SimpleNamespace
            with patch.object(HARNESS.shutil, 'disk_usage', return_value=SimpleNamespace(free=0)):
                with self.assertRaisesRegex(ValueError, 'disk reserve'):
                    HARNESS.export_report_closure(runtime, output, reference)
            self.assertFalse((output / 'report.json').exists())
            HARNESS.export_report_closure(runtime, output, reference)
            self.assertEqual((output / 'report.json').read_bytes(), encoded)
            manifest = json.loads((output / 'manifest.json').read_text())
            self.assertEqual(manifest['logical_budget_bytes'], 512 * 1024 * 1024)

    def test_report_export_existing_symlink_cannot_write_outside_diagnostics(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as temporary:
            root = Path(temporary)
            runtime, output, outside = root / 'runtime', root / 'output', root / 'outside'
            (runtime / 'reports').mkdir(parents=True)
            output.mkdir()
            outside.mkdir()
            encoded = b'{"source": "actual"}\n'
            (runtime / 'reports/report.json').write_bytes(encoded)
            victim = outside / 'report.json'
            victim.write_bytes(b'untouched')
            (output / 'reports').symlink_to(outside.resolve(), target_is_directory=True)
            reference = {'path': 'reports/report.json', 'sha256': HARNESS.hashlib.sha256(encoded).hexdigest()}
            with self.assertRaisesRegex(ValueError, 'destination escapes'):
                HARNESS.export_report_closure(runtime, output, reference)
            self.assertEqual(victim.read_bytes(), b'untouched')
            self.assertEqual((runtime / 'reports/report.json').read_bytes(), encoded)
            self.assertFalse((output / 'manifest.json').exists())


if __name__ == '__main__':
    unittest.main()
