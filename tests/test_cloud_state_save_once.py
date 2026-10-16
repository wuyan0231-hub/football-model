from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
import sys
import tempfile
import unittest

from scripts.cloud_state_save_once import save_once


class SaveOnceTests(unittest.TestCase):
    def invoke(self, logs, code=0, marker_commit='b' * 40):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'receipt.json'
            program = 'import sys; print(' + repr('\n'.join(logs)) + '); raise SystemExit(' + str(code) + ')'
            with redirect_stdout(StringIO()):
                result = save_once([sys.executable, '-c', program], receipt_path=path,
                    before_marker={'state_oid': 'a' * 40, 'source_sha': 'c' * 40},
                    after_marker=lambda: {'state_oid': marker_commit, 'source_sha': 'c' * 40},
                    source_sha='c' * 40, runtime_path=Path(directory) / 'runtime', run_id='123', run_attempt='1')
            self.assertEqual(json.loads(path.read_text()), result[1])
            return result

    def preflight(self, can_save=True):
        return 'MODEL_STATE_SAVE_PREFLIGHT=' + json.dumps({'state_oid': 'a' * 40, 'can_save': can_save,
                                                         'runtime_logical_bytes': 42, 'available_disk_bytes': 100})

    def test_one_real_save_logs_bind_success(self):
        code, value = self.invoke([self.preflight(), 'MODEL_STATE_NEW_PACK_BYTES=12', 'MODEL_STATE_SAVED_COMMIT=' + 'b' * 40])
        self.assertEqual(code, 0)
        self.assertEqual(value['runtime_validation']['status'], 'passed')
        self.assertEqual(value['save']['status'], 'saved_and_marker_verified')

    def test_push_failure_retains_validation_pass_but_never_save_pass(self):
        code, value = self.invoke([self.preflight(), 'MODEL_STATE_NEW_PACK_BYTES=12'], code=1, marker_commit='a' * 40)
        self.assertEqual(code, 1)
        self.assertEqual(value['runtime_validation']['status'], 'passed')
        self.assertEqual(value['save']['status'], 'not_proven_saved')

    def test_before_preflight_failure_does_not_fabricate_check_success(self):
        code, value = self.invoke(['safe child error'], code=1, marker_commit='a' * 40)
        self.assertEqual(code, 1)
        self.assertEqual(value['runtime_validation']['status'], 'not_proven')

    def test_insufficient_disk_is_separate_from_actual_runtime_validation(self):
        code, value = self.invoke([self.preflight(False)], code=1, marker_commit='a' * 40)
        self.assertEqual(code, 1)
        self.assertEqual(value['runtime_validation']['status'], 'passed')
        self.assertEqual(value['resource_preflight']['status'], 'insufficient_resource')

    def test_stale_malformed_missing_commit_or_mismatching_marker_cannot_turn_green(self):
        for logs, marker in ((['MODEL_STATE_SAVE_PREFLIGHT={}'], 'b' * 40),
                             ([self.preflight(), 'MODEL_STATE_NEW_PACK_BYTES=12'], 'b' * 40),
                             ([self.preflight(), 'MODEL_STATE_NEW_PACK_BYTES=12', 'MODEL_STATE_SAVED_COMMIT=' + 'b' * 40], 'a' * 40)):
            with self.subTest(logs=logs):
                code, value = self.invoke(logs, marker_commit=marker)
                self.assertEqual(code, 1)
                self.assertEqual(value['save']['status'], 'not_proven_saved')


if __name__ == '__main__':
    unittest.main()
