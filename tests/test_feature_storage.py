"""New capture/scoring reports must pass the unchanged durable-state gate."""
import json
from pathlib import Path
import tempfile
import unittest

from football_model.feature_scoring import score_run
from football_model.feature_snapshots import capture_run
from football_model.state_storage import validate_runtime


class FeatureStorageTests(unittest.TestCase):
    def test_empty_scoring_and_failed_capture_leave_saveable_runtime(self):
        with tempfile.TemporaryDirectory() as temp:
            runtime=Path(temp)/"runtime"
            runtime.mkdir()
            root=Path(__file__).resolve().parents[1]
            protocol=json.loads((root/"feature-models.json").read_text())
            config=json.loads((root/"config.json").read_text())
            code,_=score_run(runtime,config=config,protocol=protocol,source_commit="a"*40)
            self.assertEqual(code,0)
            # Missing baseline evidence is a recorded capture failure, still saveable.
            code,_=capture_run(runtime,config=config,protocol=protocol,source_commit="a"*40)
            self.assertNotEqual(code,0)
            self.assertTrue(validate_runtime(runtime))
            self.assertFalse(any(p.name.endswith('.lock') for p in runtime.rglob('*')))


if __name__=="__main__":
    unittest.main()
