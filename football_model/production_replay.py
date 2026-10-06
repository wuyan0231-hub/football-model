"""Record non-secret pipeline inputs; replay the identical policy without network.

A replay is research, never a newly issued forecast. The recording contains
observations (including failures), necessary prior state, and every clock read;
it cannot retrospectively supply inputs that a historical run never recorded.
"""
from __future__ import annotations

import argparse
import copy
from datetime import datetime, timezone
import hashlib
import gzip
import json
import os
import tempfile
from pathlib import Path
import re
import sys
from unittest.mock import patch

from .data import APIError, DataError, RequestBudgetError, RequestDeadlineError

SCHEMA = "production_pipeline_replay_v1"
METHODS = {"status", "resolve_league", "fetch_fixtures", "fetch_venues", "budget_snapshot", "reserve_requests"}
ATTRIBUTES = {"requests_made", "max_requests", "daily_remaining"}
BUDGET_FIELDS = {"configured_daily_limit", "provider_daily_limit", "provider_daily_used", "daily_remaining",
    "daily_remaining_reported", "minute_limit", "minute_remaining", "max_requests_per_run", "requests_made",
    "min_interval_seconds", "max_run_seconds", "elapsed_seconds", "quota_observed_at", "quota_source", "daily_quota_status"}
CONFIG_FIELDS = {"schema_version", "history_seasons", "max_requests_per_run", "api_daily_limit", "api_max_run_seconds",
    "half_life_days", "ridge", "min_train", "min_test", "forecast_days", "max_model_age_days", "competitions", "prospective"}
COMPETITION_FIELDS = {"key", "label", "name", "country", "mode", "provider_id"}
SOURCE_FILES = ("__init__.py", "cli.py", "model.py", "data.py", "nations.py", "venue_registry.py",
    "match_locations.py", "coverage_audit.py", "production_replay.py", "settlement.py", "nations_venues.json",
    "nations_match_locations.json", "nations_venue_city_aliases.json")
ERRORS = {cls.__name__: cls for cls in (APIError, DataError, RequestBudgetError, RequestDeadlineError,
    ValueError, KeyError, TypeError, RuntimeError, OSError, AttributeError)}


class ReplayError(ValueError):
    pass


class _RecordedKeyError(KeyError):
    def __str__(self):
        return self.args[0]


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _digest(data):
    return hashlib.sha256(data).hexdigest()


def _sources():
    root = Path(__file__).parent
    return {name: _digest((root / name).read_bytes()) for name in SOURCE_FILES}


def _safe_error(exc):
    from .cli import safe_error
    return {"type": type(exc).__name__ if type(exc).__name__ in ERRORS else "RuntimeError", "message": safe_error(exc)}


def _safe_config(config):
    # Do not silently drop future settings which might affect the policy.
    unknown = set(config) - CONFIG_FIELDS
    if unknown:
        raise ReplayError("回放配置存在未审核字段：" + ", ".join(sorted(unknown)))
    result = copy.deepcopy(config)
    if "prospective" in result:
        settings = result["prospective"]
        if not isinstance(settings, dict) or set(settings) - {"contracts"}:
            raise ReplayError("prospective配置含未审核字段")
        if "contracts" in settings:
            from .settlement import contract
            contracts = settings["contracts"]
            if not isinstance(contracts, list) or len(contracts) > 100:
                raise ReplayError("prospective contracts必须为最多100项的列表")
            for value in contracts:
                if not isinstance(value, dict) or set(value) - {"market", "side", "line", "settlement_basis"}:
                    raise ReplayError("prospective contract含未审核字段")
                contract(value)
    for competition in result.get("competitions", []):
        unknown = set(competition) - COMPETITION_FIELDS
        if unknown:
            raise ReplayError("回放赛事配置存在未审核字段：" + ", ".join(sorted(unknown)))
        if not re.fullmatch(r"[a-z0-9_]+", competition.get("key", "")):
            raise ReplayError("非法赛事key")
    return result


def _state_allowed(relative, keys):
    if relative in {"registries/nations_venues.json", "registries/nations_venue_retry.json"}:
        return "unl" in keys
    if re.fullmatch(r"registries/nations_venues/[a-f0-9]{64}\.json", relative):
        return "unl" in keys
    for key in keys:
        if relative == f"models/{key}.json" or re.fullmatch(rf"validation/{re.escape(key)}/[A-Za-z0-9_-]+\.json", relative):
            return True
    return False


def _initial_state(runtime, keys):
    root = Path(runtime).resolve()
    selected = []
    for key in keys:
        model = root / "models" / f"{key}.json"
        if not model.exists():
            continue
        selected.append(model)
        if model.is_symlink() or not model.resolve().is_relative_to(root):
            raise ReplayError("初始模型路径越界")
        bundle = json.loads(model.read_text(encoding="utf-8"))
        model_id = bundle.get("model_id")
        if isinstance(model_id, str) and model_id.startswith(key + "-"):
            old_run = model_id[len(key) + 1:]
            if re.fullmatch(r"[A-Za-z0-9_-]+", old_run):
                validation = root / "validation" / key / f"{old_run}.json"
                if validation.exists():
                    selected.append(validation)
        snapshot = bundle.get("national_audit", {}).get("registry_snapshot")
        if isinstance(snapshot, str) and _state_allowed(snapshot, keys):
            path = root / snapshot
            if path.exists():
                selected.append(path)
    if "unl" in keys:
        selected.extend(path for path in (root / "registries/nations_venues.json", root / "registries/nations_venue_retry.json") if path.exists())
    result = {}
    for path in selected:
        relative = path.relative_to(root).as_posix()
        if path.is_symlink() or not path.resolve().is_relative_to(root) or not _state_allowed(relative, keys):
            raise ReplayError("初始状态路径越界")
        content = path.read_bytes()
        result[relative] = {"sha256": _digest(content), "content": content.decode("utf-8")}
    return result


def _outcome(report, runtime):
    fields = ("run_started_at", "training_cutoff", "run_id", "mode", "requested_competitions", "competitions",
              "api_requests", "api_budget", "fatal_error", "forecast_availability_summary", "generated_at")
    result = {key: copy.deepcopy(report[key]) for key in fields if key in report}
    rows = {}
    for key in report["requested_competitions"]:
        path = Path(runtime) / "predictions" / report["run_id"] / f"{key}.json"
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            for row in data:
                row.pop("research_replay", None)
            rows[key] = data
    result["predictions"] = rows
    if isinstance(result.get("api_budget"), dict):
        result["api_budget"] = {k: v for k, v in result["api_budget"].items() if k in BUDGET_FIELDS}
    return result


class Recorder:
    """Transparent opt-in observation; failed capture never blocks a forecast."""
    def __init__(self, config, runtime, mode, league, clock, now=None):
        self.config = _safe_config(config)
        self.runtime = Path(runtime)
        self.mode, self.league = mode, league
        self.keys = [c["key"] for c in config["competitions"] if league == "all" or c["key"] == league]
        self.state = _initial_state(runtime, self.keys)
        self.sources = _sources()
        self.events = []
        self.clock_source = clock
        self.now = now.isoformat() if now is not None else None
        self.error = None

    def append(self, event):
        if self.error is not None:
            return
        try:
            self.events.append(json.loads(_canonical(event)))
        except (ValueError, TypeError, UnicodeError) as exc:
            self.error = _safe_error(exc)["message"]

    def clock(self):
        try:
            value = self.clock_source()
        except Exception as exc:
            self.append({"op": "clock", "error": _safe_error(exc)})
            raise
        self.append({"op": "clock", "value": value.isoformat()})
        return value

    def client(self, client):
        return _RecordingClient(client, self)

    def save(self, report):
        if self.error:
            raise ReplayError(self.error)
        payload = {"schema": SCHEMA, "recorded_run_id": report["run_id"],
            "config": self.config, "mode": self.mode, "league": self.league, "now": self.now,
            "initial_state": self.state, "source_sha256": self.sources,
            "events": self.events, "expected": _outcome(report, self.runtime),
            "usage": "Recorded production inputs; any replay is research and is not prospective evidence."}
        digest = _digest(_canonical(payload))
        path = self.runtime / "replay" / "bundles" / f"{digest}.json.gz"
        encoded = gzip.compress(_canonical({"sha256": digest, "payload": payload}) + b"\n", mtime=0)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".recording-", delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(encoded)
            try:
                # Link only the completed bytes; no partial gzip can obstruct
                # saving unrelated production predictions after a write error.
                os.link(temporary, path)
            except FileExistsError:
                if read_bundle(path) != {"sha256": digest, "payload": payload}:
                    raise ReplayError("内容寻址回放包冲突；不覆盖原包")
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return {"status": "recorded", "path": path.relative_to(self.runtime).as_posix(), "sha256": digest,
                "events": len(self.events), "initial_state_files": len(self.state)}


class _RecordingClient:
    def __init__(self, client, recorder):
        object.__setattr__(self, "_client", client)
        object.__setattr__(self, "_recorder", recorder)

    def __getattr__(self, name):
        if name not in METHODS | ATTRIBUTES:
            raise AttributeError(name)
        event = {"op": "get", "name": name}
        try:
            value = getattr(self._client, name)
        except AttributeError:
            self._recorder.append({**event, "missing": True})
            raise
        if name in METHODS and callable(value):
            self._recorder.append({**event, "callable": True})
            def call(*args, **kwargs):
                invocation = {"op": "call", "name": name, "args": list(args), "kwargs": kwargs}
                try:
                    result = value(*args, **kwargs)
                except Exception as exc:
                    self._recorder.append({**invocation, "error": _safe_error(exc)})
                    raise
                # Never persist the status/account response, even transiently.
                recorded = {} if name == "status" else result
                if name == "reserve_requests":
                    recorded = "context_manager"
                elif name == "budget_snapshot" and isinstance(result, dict):
                    recorded = {k: v for k, v in result.items() if k in BUDGET_FIELDS}
                self._recorder.append({**invocation, "value": recorded})
                return _RecordingContext(result, self._recorder) if name == "reserve_requests" else result
            return call
        self._recorder.append({**event, "value": value})
        return value

    def __setattr__(self, name, value):
        if name not in ATTRIBUTES:
            raise AttributeError(name)
        setattr(self._client, name, value)
        self._recorder.append({"op": "set", "name": name, "value": value})


class _RecordingContext:
    def __init__(self, context, recorder):
        self.context, self.recorder = context, recorder

    def __enter__(self):
        try:
            self.context.__enter__()
        except Exception as exc:
            self.recorder.append({"op": "context_enter", "error": _safe_error(exc)})
            raise
        self.recorder.append({"op": "context_enter", "value": None})
        return None

    def __exit__(self, *args):
        try:
            result = self.context.__exit__(*args)
        except Exception as exc:
            self.recorder.append({"op": "context_exit", "error": _safe_error(exc)})
            raise
        self.recorder.append({"op": "context_exit", "value": result})
        return result


class _Trace:
    def __init__(self, events):
        self.events, self.position = events, 0

    def take(self, requested):
        if self.position >= len(self.events):
            raise ReplayError("回放轨迹提前结束")
        event = self.events[self.position]
        self.position += 1
        if any(event.get(key) != value for key, value in requested.items()):
            raise ReplayError(f"回放行为与录制不符：event {self.position}, {requested.get('op')}")
        if "error" in event:
            error = event["error"]
            cls = _RecordedKeyError if error["type"] == "KeyError" else ERRORS.get(error["type"], RuntimeError)
            raise cls(error["message"])
        return copy.deepcopy(event)

    def clock(self):
        return datetime.fromisoformat(self.take({"op": "clock"})["value"])


class ReplayClient:
    """No underlying transport or credentials; only strict recorded events."""
    def __init__(self, trace):
        object.__setattr__(self, "_trace", trace)

    def __getattr__(self, name):
        if name not in METHODS | ATTRIBUTES:
            raise AttributeError(name)
        event = self._trace.take({"op": "get", "name": name})
        if event.get("missing"):
            raise AttributeError(name)
        if event.get("callable"):
            def call(*args, **kwargs):
                event = self._trace.take({"op": "call", "name": name, "args": list(args), "kwargs": kwargs})
                return _ReplayContext(self._trace) if name == "reserve_requests" else event.get("value")
            return call
        return event.get("value")

    def __setattr__(self, name, value):
        self._trace.take({"op": "set", "name": name, "value": value})


class _ReplayContext:
    def __init__(self, trace):
        self.trace = trace

    def __enter__(self):
        return self.trace.take({"op": "context_enter"}).get("value")

    def __exit__(self, *args):
        return self.trace.take({"op": "context_exit"}).get("value")


def read_bundle(path):
    """Read gzip production recordings (plain JSON accepted for inspection tools)."""
    encoded = Path(path).read_bytes()
    if encoded[:2] == b"\x1f\x8b":
        encoded = gzip.decompress(encoded)
    return json.loads(encoded)


def replay_bundle(bundle_path, output_dir):
    from .cli import atomic_json, run_pipeline
    bundle_path, output = Path(bundle_path).resolve(), Path(output_dir).resolve()
    envelope = read_bundle(bundle_path)
    payload = envelope.get("payload")
    digest = _digest(_canonical(payload))
    if envelope.get("sha256") != digest or not isinstance(payload, dict) or payload.get("schema") != SCHEMA:
        raise ReplayError("回放包指纹/schema不匹配")
    if payload.get("source_sha256") != _sources():
        raise ReplayError("回放源码指纹不匹配；须使用录制时的源码版本")
    if output.exists():
        raise ReplayError("回放output必须是尚不存在的新目录；禁止覆盖生产状态或旧回放")
    # Reject descendants of production runtimes, including not-yet-created dirs.
    for parent in (output, *output.parents):
        if any((parent / name).exists() for name in ("models", "predictions", "reports", "cache", "replay/bundles")):
            raise ReplayError("回放output不能位于已有runtime内")
    config = _safe_config(payload["config"])
    keys = [c["key"] for c in config["competitions"] if payload["league"] == "all" or c["key"] == payload["league"]]
    state = payload["initial_state"]
    for relative, record in state.items():
        if not _state_allowed(relative, keys) or _digest(record["content"].encode()) != record["sha256"]:
            raise ReplayError("初始状态路径或指纹无效")
    output.mkdir(parents=True)
    marker = {"schema_version": 1, "research_replay": True, "prospective_eligible": False,
              "bundle_sha256": digest, "recorded_run_id": payload["recorded_run_id"],
              "replayed_at": datetime.now(timezone.utc).isoformat(),
              "usage": "Entire directory is a research replay; recorded timestamps are not a new publication."}
    atomic_json(output / "RESEARCH_REPLAY.json", marker)
    for relative, record in state.items():
        path = output / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(record["content"].encode())
    trace = _Trace(payload["events"])
    def no_network(*args, **kwargs):
        raise ReplayError("离线回放禁止网络/真实API")
    with patch("socket.socket", no_network), patch("socket.create_connection", no_network), patch("football_model.cli.APIClient", no_network):
        code, report = run_pipeline(config, output, payload["mode"], payload["league"],
            now=datetime.fromisoformat(payload["now"]) if payload.get("now") else None,
            client=ReplayClient(trace), clock=trace.clock, research_replay=True)
    complete = trace.position == len(trace.events)
    equal = _outcome(report, output) == payload["expected"]
    result = {**marker, "status": "matched" if complete and equal else "mismatch",
              "recorded_events": len(trace.events), "consumed_events": trace.position,
              "outcomes_equal": equal, "pipeline_exit_code": code,
              "limitation": "Policy execution reproducibility, not predictive accuracy or betting returns."}
    atomic_json(output / "replay_result.json", result)
    return (0 if complete and equal else 1), result


def main(argv=None):
    parser = argparse.ArgumentParser(description="严格离线重演已录制生产更新逻辑；不产生新赛前资格")
    parser.add_argument("--bundle", required=True)
    parser.add_argument("--output-dir", required=True, help="必须为独立的全新目录")
    args = parser.parse_args(argv)
    try:
        code, result = replay_bundle(args.bundle, args.output_dir)
    except (ValueError, OSError, KeyError, TypeError) as exc:
        print(f"回放失败：{exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return code


if __name__ == "__main__":
    sys.exit(main())
