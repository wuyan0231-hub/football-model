"""Capacity admission tests; no real network and no raised production limits."""
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest

from football_model.advanced_capacity import CapacityDeferred, CapacityGuard, guarded_cache_writes
from football_model.data import APIClient
from football_model.state_storage import estimate_runtime

NOW = datetime(2026, 10, 13, tzinfo=timezone.utc)


class CachedFake:
    _cached_request = APIClient._cached_request
    def __init__(self, runtime, response):
        self.data_dir = Path(runtime)/"cache"
        self.response = response
        self.calls = 0
        self._clock = lambda: NOW
    def _redact_full(self, value):
        return json.dumps(value, ensure_ascii=False)
    def _request(self, endpoint, params):
        self.calls += 1
        return {"response": self.response, "errors": [], "fetched_at": NOW.isoformat(), "from_cache": False}


class AdvancedCapacityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.runtime = Path(self.temp.name)

    def guard(self, **kw):
        defaults = dict(reserve_logical=2000, reserve_physical=2000,
                        max_file=2000, max_logical=1000000, max_physical=1000000)
        defaults.update(kw)
        return CapacityGuard(self.runtime, **defaults)

    def write(self, guard, name, value, **kw):
        raw=json.dumps(value).encode()
        path=self.runtime/name
        ticket=guard.check_write(path,raw,**kw)
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_bytes(raw)
        guard.commit_write(ticket)
        return ticket

    def test_exact_incremental_create_replace_and_noop_match_full_estimate(self):
        guard=self.guard()
        self.write(guard,"advanced_features/evidence/"+"a"*64+".json",{"value":"long"*100})
        self.write(guard,"advanced_features/collection/latest.json",{"status":"many"*100})
        self.write(guard,"advanced_features/collection/latest.json",{"status":"small"})
        self.write(guard,"advanced_features/collection/latest.json",{"status":"small"})
        actual=estimate_runtime(self.runtime)
        self.assertEqual(guard.logical,actual["logical_bytes"])
        self.assertEqual(guard.physical,actual["persisted_bytes"])

    def test_response_reservation_stops_before_network_and_no_cache_written(self):
        guard=self.guard(max_logical=5000,reserve_logical=2000,max_file=2000)
        client=CachedFake(self.runtime,[{"value":1}])
        original=client._request
        with guarded_cache_writes(client,guard):
            with self.assertRaises(CapacityDeferred):
                client._cached_request("/fixtures",{"id":1},force=False,max_age_seconds=60)
        self.assertEqual(client.calls,0)
        self.assertFalse((self.runtime/"cache").exists())
        self.assertEqual(client._request,original)

    def test_actual_oversized_response_rejected_before_cache_write(self):
        guard=self.guard(max_file=500)
        client=CachedFake(self.runtime,[{"value":"x"*1000}])
        with guarded_cache_writes(client,guard):
            with self.assertRaises(CapacityDeferred) as failure:
                client._cached_request("/fixtures",{"id":1},force=False,max_age_seconds=60)
        self.assertEqual(client.calls,1)
        self.assertEqual(failure.exception.details["reason"],"single_file_limit")
        self.assertFalse((self.runtime/"cache").exists())
        self.assertEqual(guard.writes,0)

    def test_cached_hit_still_usable_when_new_network_cannot_fit(self):
        client=CachedFake(self.runtime,[{"value":1}])
        guard=self.guard()
        with guarded_cache_writes(client,guard):
            first=client._cached_request("/fixtures",{"id":1},force=False,max_age_seconds=60)
        guard.max_logical=guard.logical+guard.reserve_logical
        with guarded_cache_writes(client,guard):
            second=client._cached_request("/fixtures",{"id":1},force=False,max_age_seconds=60)
        self.assertFalse(first["from_cache"])
        self.assertTrue(second["from_cache"])
        self.assertEqual(client.calls,1)
        self.assertEqual(guard.writes,1)
        self.assertEqual(guard.physical,estimate_runtime(self.runtime)["persisted_bytes"])

    def test_report_can_use_reserve_but_cannot_exceed_hard_limit(self):
        guard=self.guard(max_logical=100,reserve_logical=80)
        with self.assertRaises(CapacityDeferred):
            self.write(guard,"advanced_features/collection/latest.json",{"status":"x"*30})
        self.write(guard,"advanced_features/collection/latest.json",{"status":"x"*30},final=True)
        with self.assertRaises(CapacityDeferred):
            self.write(guard,"advanced_features/collection/latest.json",{"status":"x"*100},final=True)
        self.assertEqual(json.loads((self.runtime/"advanced_features/collection/latest.json").read_text())["status"],"x"*30)

    def test_compressible_data_still_obeys_logical_cap(self):
        guard=self.guard(max_logical=1000,reserve_logical=100,max_file=10000)
        with self.assertRaises(CapacityDeferred):
            self.write(guard,"cache/api_cache/a.json",{"repeated":"x"*1000})
        self.assertFalse((self.runtime/"cache").exists())

    def test_unaccounted_or_different_actual_write_is_detected(self):
        guard=self.guard()
        ticket=guard.check_write("record.json",b'{}')
        (self.runtime/"record.json").write_bytes(b'{"changed":true}')
        with self.assertRaisesRegex(ValueError,"actual_write_differs"):
            guard.commit_write(ticket)


if __name__ == "__main__": unittest.main()
