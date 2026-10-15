import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('artifact_shards', Path(__file__).parents[1] / 'scripts/export_artifact_shards.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class ArtifactShardTests(unittest.TestCase):
    def test_reconstruction_preserves_binary_bytes_and_all_boundaries(self):
        for length in (1, 7, 8, 9, 24, 25, 1025):
            with self.subTest(length=length), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                raw = bytes(i % 256 for i in range(length))
                (root / 'input.zip').write_bytes(raw)
                digest = hashlib.sha256(raw).hexdigest()
                result = module.split_artifact(root / 'input.zip', root / 'parts', digest, 123, part_bytes=8)
                self.assertEqual((root / 'input.zip').read_bytes(), raw)
                joined = b''
                for part in result['parts']:
                    b = (root / 'parts' / part['name']).read_bytes()
                    self.assertGreater(len(b), 0)
                    self.assertLessEqual(len(b), 8)
                    self.assertEqual(hashlib.sha256(b).hexdigest(), part['sha256'])
                    self.assertEqual(len(b), part['bytes'])
                    joined += b
                self.assertEqual(joined, raw)
                self.assertEqual(hashlib.sha256(joined).hexdigest(), digest)
                self.assertEqual(json.loads((root / 'parts/manifest.json').read_text()), result)

    def test_corrupt_or_empty_original_cannot_publish_a_manifest(self):
        for raw in (b'changed artifact', b''):
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / 'input.zip').write_bytes(raw)
                with self.assertRaises(ValueError):
                    module.split_artifact(root / 'input.zip', root / 'parts', '0' * 64, 123, part_bytes=8)
                self.assertFalse((root / 'parts/manifest.json').exists())

    def test_existing_export_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'input.zip').write_bytes(b'a')
            (root / 'parts').mkdir()
            (root / 'parts/retained').write_bytes(b'original evidence')
            with self.assertRaises(FileExistsError):
                module.split_artifact(root / 'input.zip', root / 'parts', hashlib.sha256(b'a').hexdigest(), 123)
            self.assertEqual((root / 'parts/retained').read_bytes(), b'original evidence')
