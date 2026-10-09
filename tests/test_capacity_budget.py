import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from scripts.s2_capacity_budget import analyze, archive_increment, HARD_PHYSICAL, WRITER_LIMIT


def snap(day, physical=100, logical=500, writer=120):
    return {'scope':'full_fixed_saved_tree','saved_at':f'2026-10-{day:02d}T00:00:00Z',
            'physical_bytes':physical,'logical_bytes':logical,'estimated_writer_persisted_bytes':writer,
            'daily_observation':False}


def manifest(rows):
    return {'schema_version':1,'codec':'gzip-single-member','entries':[
        {'path':path,'raw_sha256':raw*64,'blob_sha256':blob*64,'raw_bytes':size,'compressed_bytes':compressed}
        for path,raw,blob,size,compressed in rows]}


class CapacityBudgetTest(unittest.TestCase):
    def test_one_snapshot_pair_never_certifies_year_or_daily_rate(self):
        out=analyze(snap(1),snap(2,200,1000,240),opportunities=185)
        self.assertTrue(out['next_same_sized_operation_gate']['passed'])
        self.assertFalse(out['daily_observation']['daily_rate_identified'])
        self.assertFalse(out['long_term_gate']['passed'])
        self.assertFalse(out['mutates_state'])

    def test_conservative_writer_can_block_while_saved_tree_is_under_hard_limit(self):
        before=snap(1,HARD_PHYSICAL-100000,500,WRITER_LIMIT-1000)
        after=snap(2,HARD_PHYSICAL-99000,1000,WRITER_LIMIT-100)
        out=analyze(before,after,opportunities=1)
        self.assertTrue(out['current_storage_gate']['passed'])
        self.assertFalse(out['next_same_sized_operation_gate']['writer_check'])
        self.assertFalse(out['next_same_sized_operation_gate']['passed'])

    def test_shared_blob_is_counted_once_and_old_evidence_reuse_is_excluded(self):
        before=manifest([('old.json','a','b',100,20)])
        after=manifest([('old.json','a','b',100,20),('new/reused.json','a','b',100,20),
                        ('new/one.json','c','d',500,80),('new/two.json','c','d',500,80)])
        out=archive_increment(before,after)
        self.assertEqual(out['new_unique_compressed_bytes'],80)
        self.assertEqual(out['new_unique_compressed_blobs'],1)
        self.assertEqual(out['reused_old_compressed_paths'],1)
        self.assertEqual(out['added_raw_bytes'],1100)

    def test_loss_of_original_archive_is_not_accepted_as_capacity_improvement(self):
        with self.assertRaisesRegex(ValueError,'old_archive_path_missing_or_changed'):
            archive_increment(manifest([('old.json','a','b',100,20)]),manifest([]))

    def test_conflicting_size_for_identical_blob_rejects_manifest(self):
        with self.assertRaisesRegex(ValueError,'blob_size_conflict'):
            archive_increment(manifest([]),manifest([('one.json','a','b',100,20),('two.json','a','b',100,21)]))

    def test_repeat_receipts_are_not_independent_opportunities(self):
        out=analyze(snap(1),snap(2,200,1000,240),opportunities=10,goal_opportunities=100,version_multipliers=(1,7))
        one,seven=out['goal_scenarios']
        self.assertEqual(one['pilot_equivalent_batches'],10)
        self.assertEqual(seven['pilot_equivalent_batches'],70)
        self.assertEqual(seven['projected_logical_bytes']-1000,7*(one['projected_logical_bytes']-1000))

    def test_true_daily_observations_do_not_prove_durable_archive_deployed(self):
        samples=[]
        for day in range(1,15):
            s=snap(day,100+day*5,500+day*20,150+day*5);s['daily_observation']=True;samples.append(s)
        out=analyze(snap(1),snap(2,200,1000,240),opportunities=10,daily_snapshots=samples)
        self.assertTrue(out['daily_observation']['daily_rate_identified'])
        self.assertFalse(out['daily_observation']['true_future_minimum_or_maximum_rate_known'])
        self.assertFalse(out['long_term_gate']['passed'])

    def test_missing_writer_estimate_is_unknown_and_does_not_pass_operation_gate(self):
        before,after=snap(1),snap(2,200,1000,240)
        before.pop('estimated_writer_persisted_bytes');after.pop('estimated_writer_persisted_bytes')
        out=analyze(before,after,opportunities=10)
        self.assertIsNone(out['next_same_sized_operation_gate']['writer_check'])
        self.assertFalse(out['next_same_sized_operation_gate']['passed'])

    def test_reversed_or_decreasing_snapshot_rejected(self):
        for before,after in [(snap(2),snap(1)),(snap(1),snap(2,99,500,120))]:
            with self.assertRaises(ValueError):analyze(before,after,opportunities=10)

    def test_cli_reads_only_input_paths(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);before=root/'before.json';after=root/'after.json'
            before.write_text(json.dumps(snap(1)));after.write_text(json.dumps(snap(2,200,1000,240)))
            original={p.name:p.read_bytes() for p in root.iterdir()}
            result=subprocess.run([sys.executable,str(Path(__file__).parents[1]/'scripts/s2_capacity_budget.py'),
                 '--before',str(before),'--after',str(after),'--opportunities','185'],capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)
            output=json.loads(result.stdout)
            self.assertFalse(output['mutates_state'])
            self.assertEqual(output['limits'], {'hard_physical_bytes': 1024**3,
                'hard_logical_bytes': 4*1024**3, 'conservative_writer_bytes': 800*1024**2})
            self.assertEqual(original,{p.name:p.read_bytes() for p in root.iterdir()})


if __name__=='__main__':unittest.main()
