"""Scoped queue reuse preserves full scoring and rechecks changed originals."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from football_model import advanced_scoring, advanced_snapshots
from football_model.feature_snapshots import _digest, SnapshotError
import tests.test_advanced_snapshots as fixtures

NOW = datetime(2026, 10, 16, tzinfo=timezone.utc)
PROTOCOL = fixtures.PROTOCOL


class UncachedQueueIndex:
    """Same public validation repeated for every row, as before this change."""
    def __init__(self, runtime, protocol):
        pass

    def members(self, runtime, league, protocol, now):
        return advanced_snapshots.queue_members(runtime, league, protocol, now)

    def member(self, runtime, league, fixture_id, horizon, protocol, now):
        return [v for v in self.members(runtime, league, protocol, now)
                if v['match_id'] == fixture_id and v['horizon'] == horizon]


class VerifiedQueueIndexTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.runtime = Path(self.temp.name) / 'runtime'
        self.directory = advanced_snapshots._root(self.runtime, PROTOCOL) / 'queue/history'
        self.directory.mkdir(parents=True)
        self.index = advanced_snapshots._VerifiedQueueIndex(self.runtime, PROTOCOL)

    def observation(self, mid=1000, **changes):
        value = {'league': 'mls', 'protocol_sha256': _digest(PROTOCOL), 'match_id': mid,
                 'horizon': 'EARLY', 'kickoff': (NOW + timedelta(hours=10)).isoformat(),
                 'queue_observed_at': NOW.isoformat(), 'status': 'not_due'}
        value.update(changes)
        raw = (json.dumps(value, sort_keys=True, separators=(',', ':')) + '\n').encode()
        path = self.directory / (hashlib.sha256(raw).hexdigest() + '.json')
        path.write_bytes(raw)
        return path, value

    def members(self, now=NOW):
        return self.index.members(self.runtime, 'mls', PROTOCOL, now)

    def test_scale_reads_each_original_once_and_keeps_all_denominators(self):
        for i in range(442):
            self.observation(i, league='mls' if i < 62 else 'other')
        original = advanced_snapshots.history_read_bytes
        with patch.object(advanced_snapshots, 'history_read_bytes', wraps=original) as read:
            code, report = advanced_scoring.score_run(self.runtime, 'mls',
                config={'competitions': [{'key': 'mls'}]}, protocol=PROTOCOL,
                source_commit='a' * 40, clock=lambda: NOW)
        self.assertEqual(code, 0)
        self.assertEqual(report['requested_horizon_records'], 62)
        self.assertEqual(report['queue_status_counts'], {'not_due': 62})
        self.assertEqual(report['scored_horizon_records'], 0)
        self.assertEqual(report['api_requests'], 0)
        self.assertEqual(read.call_count, 442)

    def test_same_size_changed_bytes_and_restored_mtime_are_reverified(self):
        path, value = self.observation(status='failed')
        first = self.members()[0]
        stat = path.stat()
        value['status'] = 'missed'
        raw = (json.dumps(value, sort_keys=True, separators=(',', ':')) + '\n').encode()
        self.assertEqual(len(raw), stat.st_size)
        path.write_bytes(raw)
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        second = self.members()[0]
        self.assertEqual(second['latest_status'], 'missed')
        self.assertNotEqual(first['observations'][0][2], second['observations'][0][2])
        self.assertEqual(first['first_failures'], [first['observations'][0][2]])
        self.assertEqual(second['first_failures'], [])

    def test_added_conflict_deleted_file_and_replaced_inode_are_not_cached(self):
        path, value = self.observation()
        self.assertFalse(self.members()[0].get('identity_conflict'))
        other, _ = self.observation(kickoff=(NOW + timedelta(hours=11)).isoformat())
        self.assertTrue(self.members()[0]['identity_conflict'])
        other.unlink()
        self.assertFalse(self.members()[0].get('identity_conflict'))
        substitute = path.with_suffix('.replacement')
        substitute.write_text('{"league":"mls","protocol_sha256":"bad"}')
        substitute.replace(path)
        with self.assertRaisesRegex(SnapshotError, 'protocol_or_horizon'):
            self.members()
        path.unlink()
        self.assertEqual(self.members(), [])

    def test_new_malformed_original_is_rejected_and_repair_is_not_error_cached(self):
        self.observation()
        self.members()
        bad = self.directory / ('f' * 64 + '.json')
        bad.write_text('{"league":"mls","league":"mls"}')
        with self.assertRaisesRegex(SnapshotError, 'duplicate_json_key'):
            self.members()
        bad.unlink()
        self.assertEqual(len(self.members()), 1)

    def test_future_observation_boundary_and_backward_lookup_match_fresh(self):
        self.observation(status='failed')
        self.observation(status='captured', queue_observed_at=(NOW + timedelta(seconds=10)).isoformat())
        for now in (NOW, NOW + timedelta(seconds=9), NOW + timedelta(seconds=10), NOW):
            self.assertEqual(self.members(now), advanced_snapshots.queue_members(self.runtime, 'mls', PROTOCOL, now))
        self.assertEqual(self.members()[0]['latest_status'], 'failed')

    def test_returned_members_cannot_mutate_cached_parent_evidence(self):
        self.observation(status='failed')
        first = self.members()
        first[0]['observations'][0][1]['status'] = 'captured'
        first[0]['first_failures'].clear()
        second = self.members()
        self.assertEqual(second[0]['latest_status'], 'failed')
        self.assertEqual(second[0]['observations'][0][1]['status'], 'failed')
        self.assertEqual(len(second[0]['first_failures']), 1)

    def test_index_cannot_cross_runtime_or_protocol_identity(self):
        self.observation()
        self.members()
        changed = deepcopy(PROTOCOL)
        changed['confirmation_active'] = True
        for runtime, protocol in ((self.runtime / 'other', PROTOCOL), (self.runtime, changed)):
            with self.assertRaisesRegex(SnapshotError, 'index_identity_mismatch'):
                self.index.members(runtime, 'mls', protocol, NOW)

    def test_queue_symlink_replacement_is_not_accepted_from_cache(self):
        path, _ = self.observation()
        self.members()
        original = path.with_suffix('.original')
        path.replace(original)
        path.symlink_to(original)
        with self.assertRaisesRegex(RuntimeError, '符号链接'):
            self.members()

    def test_new_catalog_binding_reverifies_unchanged_queue_bytes(self):
        path, _ = self.observation()
        self.members()
        name = path.relative_to(self.runtime).as_posix()
        entry = {'path': name, 'state_commit': 'a' * 40, 'codec': 'identity',
                 'transport_path': 'runtime/' + name, 'raw_sha256': 'b' * 64,
                 'blob_sha256': 'b' * 64, 'raw_bytes': path.stat().st_size,
                 'compressed_bytes': path.stat().st_size, 'manifest_sha256': None}
        catalog = self.runtime / 'history_archive/catalog.json'
        catalog.parent.mkdir()
        catalog.write_text(json.dumps({'schema_version': 'fixed-git-history-catalog-v1', 'entries': [entry]}))
        with self.assertRaisesRegex(RuntimeError, '活动原件与冻结历史目录冲突'):
            self.members()


class ScoreEquivalenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixtures.AdvancedSnapshotTests.setUpClass.__func__(cls)

    @classmethod
    def tearDownClass(cls):
        fixtures.AdvancedSnapshotTests.tearDownClass.__func__(cls)

    def setUp(self):
        fixtures.AdvancedSnapshotTests.setUp(self)

    live = fixtures.AdvancedSnapshotTests.live
    capture = fixtures.AdvancedSnapshotTests.capture
    score = fixtures.AdvancedSnapshotTests.score
    comparison = fixtures.AdvancedSnapshotTests.comparison

    def test_full_real_fitted_baseline_scores_and_bytes_equal_uncached_replay(self):
        self.capture()
        with patch.object(advanced_scoring, '_VerifiedQueueIndex', UncachedQueueIndex):
            before_code, before, before_client = self.score()
        root = advanced_scoring._root(self.runtime, PROTOCOL)
        before_files = {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob('*') if p.is_file()}
        shutil.rmtree(root)
        after_code, after, after_client = self.score()
        after_files = {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob('*') if p.is_file()}
        self.assertEqual((before_code, before), (after_code, after))
        self.assertEqual(before_files, after_files)
        self.assertEqual(before_client.requests_made, after_client.requests_made)
        self.assertEqual(after['scored_horizon_records'], 1)
        self.assertEqual(after['requested_horizon_records'], 2)
        self.assertEqual(after['summaries']['EARLY']['M_test']['paired_scored'], 1)
        self.assertEqual(after['summaries']['EARLY']['G0_full']['fallback_scored'], 1)

    def test_capture_anchor_bytes_changed_after_index_warmup_are_rejected(self):
        self.capture()
        index = advanced_snapshots._VerifiedQueueIndex(self.runtime, PROTOCOL)
        index.members(self.runtime, 'epl', PROTOCOL, fixtures.CAPTURE)
        root = advanced_snapshots._root(self.runtime, PROTOCOL)
        anchor = root / 'captures/epl/2000/EARLY/first.json'
        anchor.write_text('{}')
        result = advanced_snapshots.load_verified_comparison(self.runtime, 'epl', 2000, 'EARLY',
            PROTOCOL, now=fixtures.CAPTURE, _queue_index=index)
        self.assertEqual(result['status'], 'unavailable')
        self.assertEqual(result['reason'], 'invalid_scoring_hash_reference')
