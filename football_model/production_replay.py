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
import math
import os
import tempfile
from pathlib import Path
import re
import sys
from unittest.mock import patch

from .competition_scope import require_competition, select_competitions
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
SOURCE_FILES = ("__init__.py", "cli.py", "model.py", "data.py", "competition_scope.py",
    "coverage_audit.py", "production_replay.py", "settlement.py")
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
        require_competition(competition)
        unknown = set(competition) - COMPETITION_FIELDS
        if unknown:
            raise ReplayError("回放赛事配置存在未审核字段：" + ", ".join(sorted(unknown)))
        if not re.fullmatch(r"[a-z0-9_]+", competition.get("key", "")):
            raise ReplayError("非法赛事key")
    return result


def _state_allowed(relative, keys):
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
        self.comparison_policy = copy.deepcopy(COMPARISON_POLICY)
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
            "comparison_policy": self.comparison_policy,
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



COMPARISON_POLICY = {
    "version": "replay_outcome_comparison_v1",
    "exact_gate": "same_structure_scalar_types_and_float_hex_values",
    "strict_relative_tolerance": 1e-12,
    "strict_absolute_tolerance": 0.0,
    "tail_mass_absolute_tolerance": 1e-12,
    "tail_mass_path": r"^/predictions/[a-z0-9_]+/[0-9]+/prediction/tail_mass_before_normalization$",
    "tail_mass_basis": "model.py computes max(0, 1 - retained_mass); subtraction near 1 loses relative precision",
    "prior_bound_source": "integration._same uses 1e-12; this policy restricts its absolute bound to the cancellation residual",
    "zero_and_sign_transitions": "never_tolerated",
    "discrete_values": "exact_only",
    "usage": "Fixed formula-aware comparison; numerically_consistent is separate from exact matched. No operator tolerance override.",
}


def compare_outcomes(expected, actual):
    """Explain every difference while keeping the exact replay gate strict.

    All floats use a relative-only bound except the precise forecast tail-mass
    field, computed as max(0, 1 - retained_mass). Only that cancellation residual
    can use an absolute bound. Neither rule waives discrete, sign, zero or event
    changes, and numerical consistency never establishes exact equality.
    """
    differences = []
    def pointer(path, key):
        return path + "/" + str(key).replace("~", "~0").replace("/", "~1")

    def discrete(path, kind, **detail):
        differences.append({"path": path or "/", "kind": kind,
                            "strict_relative_consistent": False,
                            "numerically_consistent": False, **detail})

    def walk(left, right, path):
        if type(left) is not type(right):
            discrete(path, "type_mismatch", expected_type=type(left).__name__, actual_type=type(right).__name__)
            return
        if isinstance(left, dict):
            if set(left) != set(right):
                discrete(path, "keys_mismatch", missing_keys=sorted(set(left) - set(right)),
                         unexpected_keys=sorted(set(right) - set(left)))
            for key in sorted(set(left) & set(right)):
                walk(left[key], right[key], pointer(path, key))
        elif isinstance(left, list):
            if len(left) != len(right):
                discrete(path, "length_mismatch", expected_length=len(left), actual_length=len(right))
            for index, (old, new) in enumerate(zip(left, right)):
                walk(old, new, pointer(path, index))
        elif type(left) is float:
            if not math.isfinite(left) or not math.isfinite(right):
                discrete(path, "nonfinite_float", expected=repr(left), actual=repr(right))
                return
            if left.hex() == right.hex():
                return
            absolute = abs(left - right)
            zero_transition = left == 0.0 or right == 0.0
            sign_transition = (left > 0.0) != (right > 0.0)
            scale = max(abs(left), abs(right))
            relative = (absolute / scale if math.isfinite(absolute)
                        else abs(left / scale - right / scale)) if scale else None
            allowed_domain = not zero_transition and not sign_transition
            strict = allowed_domain and relative <= COMPARISON_POLICY["strict_relative_tolerance"]
            tail_residual = bool(re.fullmatch(COMPARISON_POLICY["tail_mass_path"], path))
            absolute_tolerance = COMPARISON_POLICY["tail_mass_absolute_tolerance"] if tail_residual else 0.0
            reference = allowed_domain and math.isclose(left, right,
                rel_tol=COMPARISON_POLICY["strict_relative_tolerance"], abs_tol=absolute_tolerance)
            differences.append({"path": path or "/", "kind": "float_change",
                "expected": left, "actual": right,
                "absolute_error": absolute if math.isfinite(absolute) else None,
                "absolute_error_overflow": not math.isfinite(absolute), "relative_error": relative,
                "zero_transition": zero_transition, "sign_transition": sign_transition,
                "absolute_tolerance": absolute_tolerance, "tail_cancellation_residual": tail_residual,
                "strict_relative_consistent": strict, "numerically_consistent": reference})
        elif left != right:
            discrete(path, "scalar_change", expected=left, actual=right)

    walk(expected, actual, "")
    floating = [row for row in differences if row["kind"] == "float_change"]
    overflowing = any(row["absolute_error_overflow"] for row in floating)
    relative_errors = [row["relative_error"] for row in floating if row["relative_error"] is not None]
    return {"policy": copy.deepcopy(COMPARISON_POLICY), "exact_outcomes_equal": not differences,
        "strict_relative_consistent": all(row["strict_relative_consistent"] for row in differences),
        "numerically_consistent": all(row["numerically_consistent"] for row in differences),
        "difference_count": len(differences), "float_difference_count": len(floating),
        "non_float_difference_count": len(differences) - len(floating),
        "zero_transition_count": sum(row["zero_transition"] for row in floating),
        "sign_transition_count": sum(row["sign_transition"] for row in floating),
        "maximum_absolute_error": None if overflowing else max((row["absolute_error"] for row in floating), default=0.0),
        "maximum_absolute_error_overflow": overflowing,
        "maximum_relative_error": max(relative_errors, default=0.0),
        "differences": differences}


def replay_bundle(bundle_path, output_dir):
    from .cli import atomic_json, run_pipeline
    bundle_path, output = Path(bundle_path).resolve(), Path(output_dir).resolve()
    envelope = read_bundle(bundle_path)
    payload = envelope.get("payload")
    digest = _digest(_canonical(payload))
    if envelope.get("sha256") != digest or not isinstance(payload, dict) or payload.get("schema") != SCHEMA:
        raise ReplayError("回放包指纹/schema不匹配")
    if payload.get("comparison_policy") != COMPARISON_POLICY:
        raise ReplayError("回放包缺少匹配的预先冻结数值比较政策")
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
    comparison = compare_outcomes(payload["expected"], _outcome(report, output))
    equal = comparison["exact_outcomes_equal"]
    numerical = complete and comparison["numerically_consistent"]
    status = "matched" if complete and equal else "numerically_consistent" if numerical else "mismatch"
    result = {**marker, "status": status,
              "recorded_events": len(trace.events), "consumed_events": trace.position,
              "outcomes_equal": equal, "exact_outcomes_equal": equal, "numerical_consistency": numerical,
              "comparison": comparison,
              "pipeline_exit_code": code,
              "limitation": "Policy execution reproducibility, not predictive accuracy or betting returns."}
    atomic_json(output / "replay_result.json", result)
    return (0 if numerical else 1), result


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
