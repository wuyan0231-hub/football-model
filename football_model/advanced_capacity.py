"""Bound optional acquisition before writes without changing production limits.

The complete restored runtime is measured once. Sequential writes are admitted
using exact logical deltas and conservative gzip transport costs. A reserve is
kept for summaries and downstream work; exhausted capacity is resumable.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
from pathlib import Path

from .data import RequestBudgetError
from .state_storage import (MAX_FILE_BYTES, MAX_LOGICAL_BYTES, MAX_PHYSICAL_BYTES,
                            estimate_runtime, storage_cost, _relative, _scan_secret)

MIB = 1024 * 1024


class CapacityDeferred(RequestBudgetError):
    reason = "storage_capacity_deferred"

    def __init__(self, details):
        self.details = details
        super().__init__(self.reason)


class CapacityGuard:
    def __init__(self, runtime, *, reserve_logical=128*MIB, reserve_physical=32*MIB,
                 max_logical=MAX_LOGICAL_BYTES, max_physical=MAX_PHYSICAL_BYTES,
                 max_file=MAX_FILE_BYTES):
        self.runtime = Path(runtime).resolve()
        self.max_logical, self.max_physical, self.max_file = max_logical, max_physical, max_file
        self.reserve_logical, self.reserve_physical = reserve_logical, reserve_physical
        estimate = estimate_runtime(self.runtime)
        self.logical = estimate["logical_bytes"]
        self.physical = estimate["persisted_bytes"]
        self.retained = frozenset(estimate["retained_inline"])
        self.writes = 0

    def summary(self):
        return {"logical_bytes": self.logical, "conservative_physical_bytes": self.physical,
                "logical_limit_bytes": self.max_logical, "physical_limit_bytes": self.max_physical,
                "reserve_logical_bytes": self.reserve_logical, "reserve_physical_bytes": self.reserve_physical,
                "accounted_writes": self.writes, "scope": "current_sequential_optional_collector_writes"}

    def _check(self, logical_delta, physical_delta, *, final=False):
        logical = self.logical + logical_delta
        physical = self.physical + physical_delta
        if (logical + (0 if final else self.reserve_logical) > self.max_logical
                or physical + (0 if final else self.reserve_physical) > self.max_physical):
            raise CapacityDeferred({**self.summary(), "proposed_logical_bytes": logical,
                "proposed_conservative_physical_bytes": physical, "report_reserve_in_use": final})

    def before_request(self, *, logical_bytes=None, physical_bytes=None):
        # The response is unknown before network. Reserve a full-size cache and
        # one full-size evidence file; actual response and wrapper are checked
        # exactly before either is written. This is not an arbitrary match cap.
        logical_bytes = 2*self.max_file if logical_bytes is None else logical_bytes
        physical_bytes = 2*self.max_file+8192 if physical_bytes is None else physical_bytes
        self._check(logical_bytes, physical_bytes)

    def check_write(self, path, raw, *, final=False):
        path = Path(path)
        if not path.is_absolute():
            path = self.runtime / path
        if any(p.is_symlink() for p in (path, *path.parents)):
            raise ValueError("capacity_symlinked_write")
        relative = _relative(path.resolve().relative_to(self.runtime).as_posix())
        if not isinstance(raw, bytes):
            raise ValueError("capacity_exact_bytes_required")
        if len(raw) > self.max_file:
            raise CapacityDeferred({**self.summary(), "reason": "single_file_limit", "path": relative, "proposed_file_bytes": len(raw)})
        _scan_secret(raw)
        old = path.read_bytes() if path.exists() else None
        old_cost = storage_cost(relative, old, self.retained) if old is not None else 0
        new_cost = storage_cost(relative, raw, self.retained)
        logical_delta = len(raw) - (len(old) if old is not None else 0)
        physical_delta = new_cost-old_cost
        self._check(logical_delta, physical_delta, final=final)
        return {"path": relative, "old_sha256": hashlib.sha256(old).hexdigest() if old is not None else None,
                "new_sha256": hashlib.sha256(raw).hexdigest(), "logical_delta": logical_delta,
                "physical_delta": physical_delta, "logical_before": self.logical, "physical_before": self.physical,
                "committed": False}

    def commit_write(self, ticket):
        if ticket["committed"] or self.logical != ticket["logical_before"] or self.physical != ticket["physical_before"]:
            raise ValueError("capacity_write_ticket_stale")
        path = self.runtime / ticket["path"]
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != ticket["new_sha256"]:
            raise ValueError("capacity_actual_write_differs_from_admission")
        self.logical += ticket["logical_delta"]
        self.physical += ticket["physical_delta"]
        self.writes += 1
        ticket["committed"] = True


@contextmanager
def guarded_cache_writes(client, guard):
    """Protect this collector's APIClient cache before its private write occurs.

    No keys are read or emitted. Redaction/JSON bytes use the client's existing
    serializer. Fake clients without disk-cache primitives need no interception;
    their collector still calls the public capacity checks before network.
    """
    if not all(hasattr(client, name) for name in ("_cached_request", "_request", "_redact_full", "data_dir")):
        yield client
        return
    request, cached = client._request, client._cached_request
    active = []
    def checked_request(endpoint, params):
        guard.before_request()
        result = request(endpoint, params)
        if active and active[-1]["endpoint"] == endpoint and active[-1]["params"] == params:
            frame = active[-1]
            raw = client._redact_full({**result, "endpoint": endpoint, "params": params}).encode("utf-8")
            frame["ticket"] = guard.check_write(frame["path"], raw)
        return result
    def checked_cached(endpoint, params, **kwargs):
        identity = json.dumps([endpoint, params], sort_keys=True).encode("utf-8")
        path = Path(client.data_dir)/"api_cache"/(hashlib.sha256(identity).hexdigest()+".json")
        frame = {"endpoint": endpoint, "params": params, "path": path, "ticket": None}
        active.append(frame)
        try:
            result = cached(endpoint, params, **kwargs)
            if frame["ticket"] is not None:
                guard.commit_write(frame["ticket"])
            return result
        finally:
            active.pop()
    client._request, client._cached_request = checked_request, checked_cached
    try:
        yield client
    finally:
        client._request, client._cached_request = request, cached
