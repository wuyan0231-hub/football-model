"""New capture/scoring reports must pass the unchanged durable-state gate."""
import json
from pathlib import Path
import tempfile
import unittest

from football_model.feature_scoring import score_run
from football_model.feature_snapshots import capture_run
from football_model.state_storage import validate_runtime,pack_runtime,restore_runtime,should_archive


class FeatureStorageTests(unittest.TestCase):
    def test_feature_immutable_evidence_roundtrips_without_changing_bytes(self):
        with tempfile.TemporaryDirectory() as temp:
            base=Path(temp);runtime=base/'runtime';runtime.mkdir()
            paths=['feature_models/reports/serie_a/history/'+('a'*64)+'.json',
                   'feature_snapshots/'+('b'*64)+'/payloads/'+('c'*64)+'.json']
            raw=b'{"scope":"engineering","padding":"'+b'x'*10000+b'"}\n'
            for name in paths:
                p=runtime/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(raw)
                self.assertTrue(should_archive(name))
            self.assertFalse(should_archive('feature_models/models/serie_a/latest.json'))
            tree=base/'transport';tree.mkdir();pack_runtime(runtime,tree)
            restored=base/'restored';restore_runtime(tree,restored)
            for name in paths:self.assertEqual((restored/name).read_bytes(),raw)

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
