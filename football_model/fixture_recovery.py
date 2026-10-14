"""One bounded historical recovery, independent of pre-match collection.

Only the eight fixed Championship fixtures are requested. Original predictions,
failed runs, latest indexes and confirmation ledgers are never rewritten. Wire
responses are retained byte for byte, before validation, in this isolated scope.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

from .advanced_capacity import CapacityGuard, guarded_cache_writes
from .advanced_features import embedded_snapshot
from .data import (APIClient, APIError, DataError, RequestBudgetError,
                   _fixture_identity, _integer, _iso, _utc, normalize_fixtures)
from .state_storage import StorageError

TARGET_IDS = tuple(range(1563191, 1563199))
ROOT = Path("fundamentals/recovery/championship-rounds-20-21")


class ClockError(DataError):
    pass


def _json(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise DataError("duplicate_json_key")
            result[key] = value
        return result
    return json.loads(raw, object_pairs_hook=unique,
                      parse_constant=lambda _: (_ for _ in ()).throw(DataError("nonfinite_json")))


def _bytes(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True,
                       separators=(",", ":"), allow_nan=False) + "\n").encode()


def _save(runtime, directory, raw, guard, *, final=False, suffix=".json"):
    digest = hashlib.sha256(raw).hexdigest()
    path = runtime / directory / (digest + suffix)
    if path.exists():
        if path.read_bytes() != raw:
            raise DataError("immutable_collision")
    else:
        ticket = guard.check_write(path, raw, final=final)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("xb") as out:
            out.write(raw)
        guard.commit_write(ticket)
    return {"path": path.relative_to(runtime).as_posix(), "sha256": digest, "bytes": len(raw)}


def _parent(runtime, ref):
    relative = ref["path"]
    path = runtime / relative
    if (not isinstance(relative, str) or Path(relative).is_absolute()
            or ".." in Path(relative).parts or any(p.is_symlink() for p in (path, *path.parents))
            or not path.resolve().is_relative_to(runtime)):
        raise DataError("parent_path_invalid")
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != ref["sha256"]:
        raise DataError("parent_fingerprint_mismatch")
    return _json(raw)


def _identity(row):
    identity = {key: _integer(row[key], key, 1) for key in ("match_id", "home_id", "away_id")}
    if identity["home_id"] == identity["away_id"]:
        raise DataError("duplicate_team_identity")
    identity["kickoff"] = _iso(_utc(row["kickoff"]))
    return identity


def _candidates(runtime, request, config):
    if (request.get("schema_version") != "fixture-recovery-request-v1"
            or request.get("target_ids") != list(TARGET_IDS)
            or request.get("league") != "championship" or request.get("league_id") != 40
            or not re.fullmatch(r"[0-9a-f]{40}", request.get("original_state_commit", ""))):
        raise DataError("fixed_recovery_request_invalid")
    competitions = [c for c in config["competitions"] if c["key"] == "championship"]
    if len(competitions) != 1 or competitions[0].get("name") != "Championship" or competitions[0].get("country") != "England":
        raise DataError("championship_configuration_invalid")
    predictions = _parent(runtime, request["original_forecast"])
    if not isinstance(predictions, list):
        raise DataError("forecast_parent_invalid")
    selected, seen = {}, set()
    for row in predictions:
        identity = _identity(row)
        if identity["match_id"] in seen:
            raise DataError("duplicate_forecast_fixture")
        seen.add(identity["match_id"])
        if identity["match_id"] in TARGET_IDS:
            if row.get("competition") != "championship":
                raise DataError("forecast_competition_mismatch")
            selected[identity["match_id"]] = identity
    if set(selected) != set(TARGET_IDS):
        raise DataError("target_identity_missing")
    failed_runs = request.get("failed_runs")
    if not isinstance(failed_runs, list) or len(failed_runs) != 2:
        raise DataError("failed_run_parents_missing")
    for ref in failed_runs:
        failure = _parent(runtime, ref)
        if failure.get("status") != "partial":
            raise DataError("original_failure_status_changed")
        mids = {item.get("match_id") for item in failure["matches"]
                if item.get("league") == "championship" and item.get("status") == "deferred"}
        if mids != set(TARGET_IDS):
            raise DataError("original_failure_targets_mismatch")
    return [selected[mid] for mid in TARGET_IDS]


@contextmanager
def _observe_wire(client, runtime, guard, now, attempts):
    """Observe allowed response bodies only; never inspect request headers.

    /status passes through without retaining its account payload. APIClient keeps
    its own retry, deadline, daily/minute quota and redaction implementation.
    """
    original = client._opener
    def retain_body(record, raw):
        record["raw_body_preservation_status"] = "admission_pending"
        try:
            record["raw_body"] = _save(runtime, ROOT / "wire", raw, guard, suffix=".txt")
        except RequestBudgetError:
            record["raw_body_preservation_status"] = "capacity_deferred"
            raise
        except (StorageError, ValueError, OSError):
            record["raw_body_preservation_status"] = "credential_or_storage_admission_refused"
            raise
        record["raw_body_preservation_status"] = "exact_original_bytes_saved"
    class Response:
        def __init__(self, source, record):
            self.source, self.record = source, record
        @property
        def headers(self):
            return self.source.headers
        def __enter__(self):
            self.source.__enter__()
            return self
        def __exit__(self, *args):
            return self.source.__exit__(*args)
        def read(self):
            try:
                raw = self.source.read()
            except (OSError, TimeoutError):
                self.record.update(failure="response_read_failed", response_completed_at=_iso(now()))
                raise
            # Credential echo checks and exact capacity admission precede writes.
            retain_body(self.record, raw)
            self.record["response_completed_at"] = _iso(now())
            self.record["body_validation_status"] = "invalid_or_ambiguous_json"
            _json(raw)
            self.record["body_validation_status"] = "strict_json_valid"
            return raw
    def observed(request, **kwargs):
        url = urlsplit(request.full_url)
        if url.path == "/status" and not url.query:
            return original(request, **kwargs)
        params = parse_qs(url.query)
        if url.path != "/fixtures" or set(params) != {"id"} or len(params["id"]) != 1 or int(params["id"][0]) not in TARGET_IDS:
            raise APIError("recovery_endpoint_not_allowed")
        record = {"endpoint": "/fixtures", "params": {"id": int(params["id"][0])},
                  "request_started_at": _iso(now())}
        attempts.append(record)
        try:
            return Response(original(request, **kwargs), record)
        except HTTPError as exc:
            record["http_status"] = exc.code
            retain_body(record, exc.read())
            record["response_completed_at"] = _iso(now())
            raise
        except (OSError, TimeoutError):
            record.update(failure="network_or_timeout", response_completed_at=_iso(now()))
            raise
    client._opener = observed
    try:
        yield
    finally:
        client._opener = original


def _verify(envelope, expected, started, now):
    if (envelope.get("endpoint") != "/fixtures" or envelope.get("params") != {"id": expected["match_id"]}
            or envelope.get("errors") or envelope.get("from_cache") is not False
            or envelope.get("paging", {}).get("total", 1) != 1
            or envelope.get("paging", {}).get("current", 1) != 1
            or not isinstance(envelope.get("response"), list) or len(envelope["response"]) != 1):
        raise DataError("fixture_response_invalid_or_duplicate")
    observed = _utc(envelope["fetched_at"])
    if not started <= observed <= now or _utc(expected["kickoff"]) >= started:
        raise DataError("recovery_identity_or_real_clock_invalid")
    raw = envelope["response"][0]
    actual, _ = _fixture_identity(raw)
    if _identity(actual) != expected or raw["league"]["id"] != 40:
        raise DataError("fixture_identity_mismatch")
    return raw


def collect_run(config, runtime, request, *, source_commit, state_commit,
                client=None, clock=None, max_seconds=300, reserve_requests=500, capacity_guard=None):
    if (type(max_seconds) not in (int, float) or not math.isfinite(max_seconds) or not 0 < max_seconds <= 300
            or type(reserve_requests) is not int or reserve_requests < 500
            or any(not re.fullmatch(r"[0-9a-f]{40}", value) for value in (source_commit, state_commit))):
        raise DataError("recovery_bounds_or_commits_invalid")
    clock = clock or (lambda: datetime.now(timezone.utc))
    started, runtime = _utc(clock()), Path(runtime).resolve()
    last_clock = started
    def now():
        nonlocal last_clock
        value = _utc(clock())
        if value < last_clock:
            report.update(clock_integrity="failed", clock_failure={
                "previous_observed_at": _iso(last_clock), "actual_clock_reading": _iso(value)})
            raise ClockError("clock_moved_backwards")
        last_clock = value
        return value
    def safe_recorded_time():
        if report["clock_integrity"] == "failed":
            return None
        try:
            return _iso(now())
        except ClockError:
            return None
    guard = capacity_guard or CapacityGuard(runtime)
    report = {"schema_version": "fixture-recovery-v1", "started_at": _iso(started),
              "scope": "retrospective_historical_fixture_details", "time_source": "real_wall_clock",
              "clock_integrity": "valid",
              "prematch_eligible": False, "confirmation_eligible": False, "production_promoted": False,
              "original_failure_superseded": False, "source_commit": source_commit,
              "loaded_state_commit": state_commit, "request": request,
              "reserve_requests": reserve_requests, "max_seconds": max_seconds,
              "matches": [{"match_id": mid, "status": "pending", "http_attempts": []} for mid in TARGET_IDS],
              "issues": []}
    halted = None
    try:
        candidates = _candidates(runtime, request, config)
        if any(_utc(row["kickoff"]) >= started for row in candidates):
            raise DataError("recovery_target_not_historical")
        client = client or APIClient(runtime / ROOT / "cache", max_requests=config.get("max_requests_per_run"),
            daily_limit=config.get("api_daily_limit"), max_run_seconds=min(max_seconds, config.get("api_max_run_seconds") or max_seconds), clock=clock)
        guard.before_request()
        client.status()
        if client.budget_snapshot().get("daily_quota_status") != "known":
            raise DataError("actual_daily_balance_unavailable")
    except RequestBudgetError as exc:
        candidates = []
        halted = getattr(exc, "reason", "real_budget_or_deadline_deferred")
        report["issues"].append({"reason": halted})
    except (APIError, ValueError, KeyError, TypeError, OSError):
        candidates = []
        halted = "parent_configuration_or_real_budget_invalid"
        report["issues"].append({"reason": halted})
    for item, expected in zip(report["matches"], candidates):
        item["expected_identity"] = expected
        if halted:
            item.update(status="deferred", reason=halted)
            continue
        try:
            guard.before_request()
            with client.reserve_requests(reserve_requests), guarded_cache_writes(client, guard), _observe_wire(client, runtime, guard, now, item["http_attempts"]):
                envelope = client.fetch_fixture(expected["match_id"], force=True)
            item["response_envelope"] = _save(runtime, ROOT / "envelopes", _bytes(envelope), guard)
            raw = _verify(envelope, expected, started, now())
            results = normalize_fixtures([raw], now())
            item["regulation_result"] = results[0] if results else None
            item["result_status"] = "observed_finished_90_minutes" if results else "not_finished_or_four_hour_buffer_not_elapsed"
            item["embedded_fields"] = {}
            for kind in ("statistics", "lineups", "players"):
                if kind not in raw or raw[kind] == []:
                    item["embedded_fields"][kind] = {"status": "unavailable", "value": None}
                    continue
                try:
                    snapshot = embedded_snapshot(kind, envelope, expected["match_id"])
                    projection = {"schema_version": "historical-fixture-projection-v1", "kind": kind,
                        "network_endpoint": "/fixtures", "source_field": kind,
                        "response_envelope": item["response_envelope"],
                        "wire_parents": [attempt["raw_body"] for attempt in item["http_attempts"] if "raw_body" in attempt],
                        "scope": report["scope"], "prematch_eligible": False, "confirmation_eligible": False,
                        "created_at": _iso(now()), "normalized_snapshot": snapshot}
                    ref = _save(runtime, ROOT / "projections" / kind, _bytes(projection), guard)
                    item["embedded_fields"][kind] = {"status": "validated_historical_projection", "source": ref}
                except (ValueError, KeyError, TypeError, AttributeError):
                    item["embedded_fields"][kind] = {"status": "quarantined", "reason": "embedded_schema_or_identity_invalid"}
            item["status"] = "failed" if any(v["status"] == "quarantined" for v in item["embedded_fields"].values()) else "collected_with_reported_coverage"
        except RequestBudgetError as exc:
            halted = getattr(exc, "reason", "real_budget_or_deadline_deferred")
            item.update(status="deferred", reason=halted, recorded_at=safe_recorded_time(),
                request_refusal={"endpoint": "/fixtures", "params": {"id": expected["match_id"]},
                                 "reason": halted, "api_budget": client.budget_snapshot()})
        except ClockError:
            halted = "real_clock_integrity_failed"
            item.update(status="failed", reason=halted, recorded_at=None)
        except StorageError:
            halted = "response_preservation_credential_or_storage_refused"
            item.update(status="failed", reason=halted, recorded_at=safe_recorded_time())
        except (APIError, ValueError, KeyError, TypeError, OSError, AttributeError):
            item.update(status="failed", reason="provider_schema_identity_or_storage_failure", recorded_at=safe_recorded_time())
    for item in report["matches"]:
        if item["status"] == "pending":
            item.update(status="deferred", reason=halted or "not_attempted")
    report.update(finished_at=safe_recorded_time(), api_budget=client.budget_snapshot() if client is not None else None)
    report["full_collection_completed"] = report["clock_integrity"] == "valid" and all(item["status"] == "collected_with_reported_coverage" for item in report["matches"])
    report["status"] = "complete_with_reported_coverage" if report["full_collection_completed"] else "partial"
    report["exit_code"] = int(not report["full_collection_completed"])
    report["storage_capacity"] = guard.summary()
    ref = _save(runtime, ROOT / "reports", _bytes(report), guard, final=True)
    return report["exit_code"], report, ref


def main(argv=None):
    parser = argparse.ArgumentParser(description="独立补取固定8场历史比赛原件，不能恢复原赛前资格")
    parser.add_argument("--runtime", default="runtime")
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--request", default=".github/recovery-requests/championship-rounds-20-21.json")
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--state-commit", required=True)
    args = parser.parse_args(argv)
    code, report, ref = collect_run(_json(Path(args.config).read_bytes()), args.runtime,
        _json(Path(args.request).read_bytes()), source_commit=args.source_commit, state_commit=args.state_commit)
    # This receipt belongs to this invocation and does not change old latest pointers.
    Path("fixture-recovery-receipt.json").write_bytes(_bytes({"report": ref, "status": report["status"],
        "started_at": report["started_at"], "finished_at": report["finished_at"],
        "source_commit": args.source_commit, "loaded_state_commit": args.state_commit,
        "full_collection_completed": report["full_collection_completed"], "exit_code": code}))
    print(json.dumps({"report": ref, "status": report["status"], "full_collection_completed": report["full_collection_completed"],
                      "scope": report["scope"], "api_budget": report["api_budget"]}, ensure_ascii=False))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
