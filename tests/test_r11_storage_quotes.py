import json
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from football_model import state_storage as storage
from football_model.collect_quotes import _candidates


class R11StorageQuotes(unittest.TestCase):
    def test_backfill_cache_does_not_claim_complete_league_schedule(self):
        with tempfile.TemporaryDirectory() as tmp:
            r=Path(tmp); p=r/'cache/api_cache/a.json';p.parent.mkdir(parents=True)
            p.write_text(json.dumps(dict(endpoint='/fixtures',params={'team':1,'season':2026},
                fetched_at='2026-10-01T00:00:00Z',response=[{'league':{'name':'Serie A','country':'Italy'}}])))
            rows, issues=_candidates(r,{'competitions':[{'key':'serie_a','name':'Serie A','country':'Italy'}]},
                'all',None,datetime(2026,10,13,tzinfo=timezone.utc),168)
            self.assertFalse(rows)
            self.assertIn('saved_provider_schedule_missing',[i['reason'] for i in issues])
            self.assertNotIn('unreadable_schedule_cache',[i['reason'] for i in issues])
    def test_new_and_cold_sources_roundtrip_without_losing_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);r=root/'runtime';r.mkdir();t=root/'transport';t.mkdir()
            data=b'{  "source": "'+b'x'*20000+b'" }\n'
            names=['cache/api_cache/a.json','fundamentals/snapshots/1/a.json',
                'advanced_models/epl/history/'+ 'a'*64+'.json','advanced_snapshots/inputs/'+ 'b'*64+'.json']
            for name in names:
                p=r/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(data)
            latest=r/'advanced_models/epl/latest.json';latest.write_text('{"status":"ready"}')
            storage.pack_runtime(r,t);storage.restore_runtime(t,root/'restored')
            for name in names:
                self.assertEqual((root/'restored'/name).read_bytes(),data)
                self.assertFalse((t/'runtime'/name).exists())
            self.assertTrue((t/'runtime/advanced_models/epl/latest.json').exists())
            self.assertFalse(storage.should_archive(names[0],{names[0]}))
    def test_images_only_in_named_decision_source_directory_and_still_bounded(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); image=root/'feature_decisions/sources/a.png';image.parent.mkdir(parents=True)
            image.write_bytes(b'\x89PNG\r\n\x1a\nsource')
            storage.validate_runtime(root)
            with self.assertRaises(storage.StorageError):
                storage.validate_runtime(root,max_file_bytes=4)
            image.write_bytes(b'not png')
            with self.assertRaisesRegex(storage.StorageError,'格式'):
                storage.validate_runtime(root)
            image.unlink();(root/'a.png').write_bytes(b'\x89PNG\r\n\x1a\nsource')
            with self.assertRaisesRegex(storage.StorageError,'类型'):
                storage.validate_runtime(root)
