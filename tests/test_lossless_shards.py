import json
import hashlib
from pathlib import Path
import tempfile
import unittest

from scripts.s2_lossless_shards import create, restore, emit, sha


class LosslessShardsTest(unittest.TestCase):
    def test_exact_multishard_restore_with_duplicate_bytes(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);source=root/'source';source.mkdir();(source/'nested').mkdir()
            # Low compressibility and a small shard budget force a true split.
            raw=hashlib.shake_128(b'first independent object').digest(800)
            (source/'one.json').write_bytes(raw);(source/'nested/two.json').write_bytes(raw)
            (source/'three.json').write_bytes(hashlib.shake_128(b'second independent object').digest(800))
            value=create(source,root/'packed',1024)
            restored=restore(root/'packed',root/'restored')
            self.assertEqual(value['unique_objects'],2)
            self.assertEqual(restored['restored_files'],3)
            self.assertEqual(value['shards'],2)
            for path in ['one.json','nested/two.json','three.json']:
                self.assertEqual((source/path).read_bytes(),(root/'restored'/path).read_bytes())

    def test_corruption_or_missing_shard_never_leaves_partial_restoration(self):
        for kind in ['corrupt','missing']:
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as d:
                root=Path(d);source=root/'source';source.mkdir();(source/'one.json').write_text('{"ok":true}\n')
                create(source,root/'packed');manifest=json.loads((root/'packed/manifest.json').read_text())
                p=root/'packed'/manifest['shards'][0]['file']
                if kind=='missing':p.unlink()
                else:p.write_bytes(p.read_bytes()+b'x')
                with self.assertRaises(ValueError):restore(root/'packed',root/'restored')
                self.assertFalse((root/'restored').exists())

    def test_traversal_rejected_even_if_manifest_hash_is_recomputed(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);source=root/'source';source.mkdir();(source/'one.json').write_text('{}')
            create(source,root/'packed');m=json.loads((root/'packed/manifest.json').read_text());m['entries'][0]['path']='../escape'
            emit(root/'packed/manifest.json',m);(root/'packed/manifest.sha256').write_text(sha((root/'packed/manifest.json').read_bytes())+'\n')
            with self.assertRaisesRegex(ValueError,'invalid_path'):restore(root/'packed',root/'restored')
            self.assertFalse((root/'escape').exists());self.assertFalse((root/'restored').exists())

    def test_existing_destination_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);source=root/'source';source.mkdir();(source/'one.json').write_text('{}')
            create(source,root/'packed');out=root/'restored';out.mkdir();(out/'user.txt').write_text('keep')
            with self.assertRaises(ValueError):restore(root/'packed',out)
            self.assertEqual((out/'user.txt').read_text(),'keep')

    def test_source_and_destination_ancestor_symlinks_are_rejected(self):
        for operation,which in [('create','source'),('create','dest'),('restore','source'),('restore','dest')]:
            with self.subTest(operation=operation,which=which), tempfile.TemporaryDirectory() as d:
                root=Path(d);holder=root/'holder';holder.mkdir();source=holder/'source';source.mkdir()
                (source/'one.json').write_text('{"original":true}\n');packed=holder/'packed';create(source,packed)
                alias=root/'alias';alias.symlink_to(holder,target_is_directory=True)
                actual_source=source if operation=='create' else packed
                chosen_source=alias/actual_source.name if which=='source' else actual_source
                chosen_dest=alias/'new_output' if which=='dest' else root/'new_output'
                original=(source/'one.json').read_bytes()
                with self.assertRaisesRegex(ValueError,'path_symlink'):
                    (create if operation=='create' else restore)(chosen_source,chosen_dest)
                self.assertEqual((source/'one.json').read_bytes(),original)
                self.assertFalse((holder/'new_output').exists())
                self.assertFalse((root/'new_output').exists())

    def test_source_and_destination_cannot_overlap(self):
        for operation in ['create','restore']:
            with self.subTest(operation=operation), tempfile.TemporaryDirectory() as d:
                root=Path(d);source=root/'source';source.mkdir();(source/'one.json').write_text('{}')
                packed=root/'packed';create(source,packed)
                actual_source=source if operation=='create' else packed
                before={p.relative_to(actual_source).as_posix():p.read_bytes() for p in actual_source.rglob('*') if p.is_file()}
                with self.assertRaisesRegex(ValueError,'source_destination_overlap'):
                    (create if operation=='create' else restore)(actual_source,actual_source/'new_output')
                self.assertEqual(before,{p.relative_to(actual_source).as_posix():p.read_bytes() for p in actual_source.rglob('*') if p.is_file()})


if __name__=='__main__':unittest.main()
