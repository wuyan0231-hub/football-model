"""Read and export recorded F5-C originals; never capture, fit or fetch results."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import html
import json
import math
from pathlib import Path
import re
import tempfile
import zipfile

from . import total_shrink_shadow as ts
from .cli import atomic_json, safe_error
from .prospective import _digest, _identity, _same_identity, _timestamp
from .state_storage import MAX_FILE_BYTES, MAX_LOGICAL_BYTES, _relative, read_archive_bytes

INDEX = "integration/f5c-index.json"
COLLECTIONS = frozenset({"forecasts", "forecast_payloads", "protocols", "inputs", "sources",
    "production_parents", "production_sources", "selection_inputs", "selection_results",
    "results", "scores"})


def _json(raw):
    return json.loads(raw, parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite_json")))


class Originals:
    def __init__(self, runtime, state_tree=None):
        self.runtime = Path(runtime)
        self.state_tree = Path(state_tree) if state_tree else None
        self.read_bytes = {}
        for root in (self.runtime, self.state_tree):
            if root is not None and any(p.is_symlink() for p in (root, *root.parents)):
                raise ValueError("symlinked_original_root")

    def raw(self, relative):
        relative = _relative(relative)
        path = ts._path(self.runtime, *relative.split("/"))
        if path.is_file():
            if path.stat().st_size > MAX_FILE_BYTES:
                raise ValueError("original_file_limit")
            raw = path.read_bytes()
        elif self.state_tree:
            raw = read_archive_bytes(self.state_tree, relative)
        else:
            raise FileNotFoundError(relative)
        self.read_bytes[relative] = raw
        return raw

    def object(self, collection, object_id):
        if collection not in COLLECTIONS or not re.fullmatch(r"[0-9a-f]{64}", object_id or ""):
            raise ValueError("invalid_f5c_object_reference")
        value = _json(self.raw(f"{ts.ROOT}/{collection}/{object_id}.json"))
        if not isinstance(value, dict) or _digest(value) != object_id:
            raise ValueError("f5c_original_digest_mismatch")
        return value


def _references(collection, value):
    if collection in {"forecasts", "forecast_payloads", "inputs"}:
        for key, target in (("payload_id", "forecast_payloads"), ("protocol_id", "protocols"),
                            ("input_id", "inputs"), ("production_parent_id", "production_parents"),
                            ("config_source", "sources"), ("index_source", "sources")):
            if value.get(key):
                yield target, value[key]
        for key in ("data_source", "report_source", "schedule_source"):
            if value.get("parents", {}).get(key):
                yield "sources", value["parents"][key]
        selection = value.get("selection", {})
        if selection.get("selection_input_id"):
            yield "selection_inputs", selection["selection_input_id"]
        venue = value.get("venue") or {}
        for key, field in (("schedule", "source_id"), ("venue", "registry_source")):
            if (venue.get(key) or {}).get(field):
                yield "sources", venue[key][field]
    elif collection == "production_parents":
        for object_id in value["sources"].values():
            yield "production_sources", object_id
    elif collection == "selection_inputs":
        yield "protocols", value["protocol_id"]
        for member in value["eligible"]:
            yield "forecasts", member["forecast_id"]
            yield "selection_results", member["result_evidence_id"]
    elif collection == "scores":
        for key, target in (("forecast_id", "forecasts"), ("input_id", "inputs"),
                            ("protocol_id", "protocols"), ("result_id", "results")):
            yield target, value[key]


def materialize(originals, forecast_id, destination, score_id=None):
    """Restore only the hash-bound validation closure, preserving original bytes."""
    queue = [("forecasts", forecast_id)] + ([("scores", score_id)] if score_id else [])
    seen, total = set(), 0
    while queue:
        collection, object_id = queue.pop()
        if (collection, object_id) in seen:
            continue
        value = originals.object(collection, object_id)
        name = f"{ts.ROOT}/{collection}/{object_id}.json"
        raw = originals.read_bytes[name]
        total += len(raw)
        if total > MAX_LOGICAL_BYTES or len(seen) >= 10000:
            raise ValueError("f5c_evidence_closure_limit")
        target = ts._path(Path(destination), *name.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() and target.read_bytes() != raw:
            raise ValueError("f5c_evidence_destination_collision")
        target.write_bytes(raw)
        seen.add((collection, object_id))
        queue.extend(_references(collection, value))
    return ts._root(destination)


def _attempts(runtime):
    result = {}
    for operation, name in (("capture", "latest.json"), ("score", "score-latest.json")):
        path = ts._path(Path(runtime), ts.ROOT, "reports", name)
        try:
            value = _json(path.read_bytes())
            if not isinstance(value, dict):
                raise ValueError("invalid_latest_summary")
            result[operation] = {key: value.get(key) for key in
                ("status", "source_commit", "run_started_at", "generated_at", "reason",
                 "report_id", "errors", "captured_forecasts", "n_scored_matches")}
            if result[operation]["status"] is None:
                result[operation]["status"] = "completed_with_errors" if value.get("errors") else "completed"
        except FileNotFoundError:
            result[operation] = {"status": "report_missing"}
        except (OSError, ValueError, TypeError):
            result[operation] = {"status": "report_invalid"}
    return result


def _inventory(originals, collection):
    names = {p.stem for p in ts._path(originals.runtime, ts.ROOT, collection).glob("*.json")}
    if originals.state_tree:
        manifest_path = originals.state_tree / "state_storage.json"
        if manifest_path.is_symlink() or manifest_path.stat().st_size > MAX_FILE_BYTES:
            raise ValueError("invalid_f5c_manifest_path_or_size")
        manifest = _json(manifest_path.read_bytes())
        prefix = f"{ts.ROOT}/{collection}/"
        names.update(Path(e["path"]).stem for e in manifest["entries"] if e["path"].startswith(prefix))
    return sorted(names)


def build_index(runtime, *, state_commit=None):
    originals, fixtures, errors = Originals(runtime), {}, []
    for object_id in _inventory(originals, "forecasts"):
        try:
            forecast = originals.object("forecasts", object_id)
            key = f"{forecast['competition']}:{forecast['match_id']}"
            fixtures.setdefault(key, []).append({"forecast_id": object_id,
                **{k: forecast[k] for k in ("competition", "match_id", "kickoff", "home_id", "away_id",
                    "captured_at", "production_parent_id", "production_forecast_id", "input_id", "protocol_id")}})
        except (ValueError, KeyError, TypeError, OSError) as exc:
            errors.append({"forecast_id": object_id, "reason": safe_error(exc)})
    result = {"schema_version": 1, "generated_at": ts._iso(datetime.now(timezone.utc)),
        "state_commit": state_commit, "state_commit_meaning": "loaded_parent_state_not_save_receipt",
        "status": "ready" if not errors else "partial", "fixtures": fixtures, "errors": errors,
        "latest_attempts": _attempts(runtime), "full_validation_required_at_match_read": True,
        "confirmation_eligible": False, "new_fits": 0, "new_api_calls": 0}
    atomic_json(Path(runtime) / INDEX, result)
    return result


def _metrics_equal(saved, expected):
    if not isinstance(saved, dict) or saved.keys() != expected.keys():
        return False
    return all(_metrics_equal(saved[k], value) if isinstance(value, dict) else
               type(saved[k]) in (int, float) and math.isfinite(saved[k])
               and abs(saved[k] - value) <= 1e-12 for k, value in expected.items())


def _verified_score(originals, forecast, now):
    candidates = []
    for sid in _inventory(originals, "scores"):
        score = originals.object("scores", sid)
        if score.get("forecast_id") != forecast["object_id"]:
            continue
        result = originals.object("results", score["result_id"])
        if (score.get("status") != "scored" or score["input_id"] != forecast["input_id"]
                or score["protocol_id"] != forecast["protocol_id"]
                or score["competition"] != forecast["competition"]
                or score["match_id"] != forecast["match_id"]
                or score["selected_alpha"] != forecast["selection"]["alpha"]
                or _digest(result["content"]) != result["production_result_id"]):
            raise ValueError("f5c_score_binding_mismatch")
        ts._verified_frozen_result(result["content"], result["source"], forecast, now)
        arms = {name: ts._metrics(prediction, result["content"])
                for name, prediction in forecast["predictions"].items()}
        if not _metrics_equal(score["arms"], arms):
            raise ValueError("f5c_score_does_not_recompute")
        differences = {name: {k: v - arms["alpha_0"][k] for k, v in metrics.items()}
                       for name, metrics in arms.items()}
        if (not _metrics_equal(score["fixed_alpha_exploratory_minus_baseline"], differences)
                or not _metrics_equal(score["candidate_minus_baseline"], differences[ts._alpha_name(score["selected_alpha"])])):
            raise ValueError("f5c_score_difference_mismatch")
        candidates.append((result["content"]["observed_at"], result["content"]["captured_at"], sid, score))
    if not candidates:
        return {"status": "pending", "reason": "no_saved_verified_score"}
    *_, sid, score = max(candidates)
    return {**score, "score_id": sid}


def comparison(runtime, league, match_id, *, row=None, now=None, forecast_id=None, state_tree=None):
    now = now or datetime.now(timezone.utc)
    result = {"status": "not_captured", "competition": league, "match_id": match_id,
        "read_at": ts._iso(datetime.now(timezone.utc)), "validation_cutoff": ts._iso(now),
        "usage": "engineering_pilot_descriptive_only", "confirmation_eligible": False,
        "accuracy_improvement_verified": False, "new_fits": 0, "new_api_calls": 0,
        "latest_attempts": _attempts(runtime), "rejections": []}
    originals = Originals(runtime, state_tree)
    try:
        ids = [forecast_id] if forecast_id else _inventory(originals, "forecasts")
        if not forecast_id and (index := Path(runtime) / INDEX).exists():
            saved_index = _json(ts._path(Path(runtime), *INDEX.split("/")).read_bytes())
            if saved_index.get("schema_version") != 1 or not isinstance(saved_index.get("fixtures"), dict):
                raise ValueError("f5c_index_invalid")
            # Index is a hint, never an authority: include newer originals saved
            # after its generation and verify every referenced receipt below.
            ids = sorted(set(ids) | {v["forecast_id"] for v in
                saved_index["fixtures"].get(f"{league}:{match_id}", [])})
        matches = []
        for fid in ids:
            value = originals.object("forecasts", fid)
            if value.get("competition") != league or value.get("match_id") != match_id:
                if forecast_id:
                    raise ValueError("f5c_requested_identity_mismatch")
                continue
            if row is not None and not _same_identity(row, value):
                result["rejections"].append({"forecast_id": fid, "reason": "fixture_identity_mismatch"})
                continue
            if _timestamp(value["captured_at"], "captured_at") > now:
                result["rejections"].append({"forecast_id": fid, "reason": "recorded_after_analysis_cutoff"})
                continue
            matches.append((value["captured_at"], fid, value))
        for _, fid, value in sorted(matches):
            try:
                with tempfile.TemporaryDirectory(prefix="f5c-read-") as temporary:
                    root = materialize(originals, fid, temporary)
                    forecast = {**value, "object_id": fid}
                    ts._saved_forecast_valid(root, forecast, now)
                    parent = ts._load_object(root, "production_parents", value["production_parent_id"])
                    native_row = parent["forecast"]["forecast"]
                    if row is not None and native_row != row:
                        raise ValueError("different_production_parent_row")
                    score = _verified_score(originals, forecast, now)
                    if score.get("score_id"):
                        materialize(originals, fid, temporary, score["score_id"])
                    evidence_names = sorted(p.relative_to(Path(temporary)).as_posix()
                        for p in Path(temporary).rglob("*.json"))
                result.update(status="verified_same_parent" if row is not None else "verified_saved_original_audit",
                    forecast_id=fid, production_forecast_id=value["production_forecast_id"],
                    production_parent_id=value["production_parent_id"], input_id=value["input_id"],
                    protocol_id=value["protocol_id"], fixture=_identity(value),
                    production_run_id=parent["forecast"]["run_id"], production_model_id=native_row["model_id"],
                    source_commit=value["source_commit"], loaded_state_commit=value["state_commit"],
                    baseline=native_row["prediction"], predictions=value["predictions"],
                    selected_prediction=value["prediction"], selection=value["selection"],
                    input_cutoff=value["input_cutoff"], captured_at=value["captured_at"],
                    live_local_receipt_verified=True,
                    remote_persistence={"status": "not_verified_by_this_reader",
                        "immutable_receipt_field": value["remote_persistence_status"],
                        "required_evidence": "fixed_state_commit_and_first_successful_save_before_kickoff"},
                    alpha_zero_exactly_native=value["predictions"]["alpha_0"] == native_row["prediction"],
                    score=score, evidence=[{"path": name, "sha256": hashlib.sha256(originals.read_bytes[name]).hexdigest(),
                        "bytes": len(originals.read_bytes[name])} for name in evidence_names])
                return result
            except (ValueError, KeyError, TypeError, OSError) as exc:
                result["rejections"].append({"forecast_id": fid, "reason": safe_error(exc)})
        if result["rejections"]:
            result.update(status="rejected", reason="no_verified_pair_for_current_parent")
        else:
            result["reason"] = "no_saved_f5c_receipt_for_fixture"
    except (ValueError, KeyError, TypeError, OSError) as exc:
        result.update(status="rejected", reason=safe_error(exc))
    return result


def _new_output(path, runtime):
    path = Path(path).absolute()
    if (any(p.is_symlink() for p in (path, *path.parents)) or path.exists()
            or path.resolve().is_relative_to(Path(runtime).resolve())):
        raise ValueError("report_output_must_be_new_and_outside_original_runtime")
    return path


def export_report(packet, runtime, html_path, zip_path, *, state_tree=None):
    """Include a visible comparison and the exact original evidence closure."""
    f5c = packet.get("f5c_comparison", packet)
    title = f"F5-C 对照：{f5c.get('competition')} / {f5c.get('match_id')}"
    rows = []
    for name, prediction in f5c.get("predictions", {}).items():
        rates, probabilities = prediction["expected_goals"], prediction["probabilities"]
        rows.append(f"<tr><td>{html.escape(name)}</td><td>{rates['home']:.6f}</td><td>{rates['away']:.6f}</td>"
            f"<td>{probabilities['home_win']:.6%}</td><td>{probabilities['draw']:.6%}</td><td>{probabilities['away_win']:.6%}</td></tr>")
    document = ('<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>' + html.escape(title)
        + '</title><style>body{max-width:1050px;margin:40px auto;font:16px/1.7 sans-serif}table{border-collapse:collapse}td,th{border:1px solid #bbb;padding:8px}pre{white-space:pre-wrap;overflow-wrap:anywhere}</style><h1>'
        + html.escape(title) + '</h1><p>当前状态：' + html.escape(f5c["status"])
        + '</p><p>工程影子试运行；五档不是五个独立样本。α=0沿用生产原件，未通过未来确认与推广验收。历史审计不新增赛前资格。</p>'
        + '<table><tr><th>力度</th><th>主λ</th><th>客λ</th><th>主胜</th><th>平</th><th>客胜</th></tr>'
        + ''.join(rows) + '</table><h2>完整读取、父预测、评分及失败记录</h2><pre>'
        + html.escape(json.dumps(packet, ensure_ascii=False, indent=2, allow_nan=False)) + '</pre></html>')
    html_path, zip_path = _new_output(html_path, runtime), _new_output(zip_path, runtime)
    if html_path == zip_path:
        raise ValueError("report_and_archive_paths_must_differ")
    originals = Originals(runtime, state_tree)
    items = {"report.html": document.encode(),
             "comparison.json": (json.dumps(packet, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode()}
    for evidence in f5c.get("evidence", []):
        raw = originals.raw(evidence["path"])
        if hashlib.sha256(raw).hexdigest() != evidence["sha256"] or len(raw) != evidence["bytes"]:
            raise ValueError("f5c_original_changed_before_export")
        items["originals/" + evidence["path"]] = raw
    manifest = {"schema_version": 1, "files": [{"path": name, "sha256": hashlib.sha256(raw).hexdigest(),
        "bytes": len(raw)} for name, raw in sorted(items.items())]}
    html_path.parent.mkdir(parents=True, exist_ok=True)
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    with html_path.open("x", encoding="utf-8") as handle:
        handle.write(document)
    with zipfile.ZipFile(zip_path, "x", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, raw in items.items():
            archive.writestr(name, raw)
        archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
    return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    index = sub.add_parser("index")
    index.add_argument("--runtime", type=Path, default=Path("runtime"))
    index.add_argument("--state-commit")
    audit = sub.add_parser("audit", help="Historical original audit, never a fresh prematch forecast")
    audit.add_argument("--runtime", type=Path, default=Path("runtime"))
    audit.add_argument("--state-tree", type=Path)
    audit.add_argument("--league", required=True)
    audit.add_argument("--match-id", type=int, required=True)
    audit.add_argument("--forecast-id", required=True)
    audit.add_argument("--output", type=Path, required=True)
    audit.add_argument("--html", type=Path, required=True)
    audit.add_argument("--zip", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == "index":
        result = build_index(args.runtime, state_commit=args.state_commit)
        print(json.dumps({"status": result["status"], "fixtures": len(result["fixtures"]), "errors": result["errors"]}))
        return int(bool(result["errors"]))
    output = _new_output(args.output, args.runtime)
    _new_output(args.html, args.runtime)
    _new_output(args.zip, args.runtime)
    if len({output.resolve(), args.html.resolve(), args.zip.resolve()}) != 3:
        raise ValueError("audit_output_paths_must_differ")
    result = comparison(args.runtime, args.league, args.match_id, forecast_id=args.forecast_id, state_tree=args.state_tree)
    atomic_json(output, result)
    export_report(result, args.runtime, args.html, args.zip, state_tree=args.state_tree)
    print(json.dumps({"status": result["status"], "forecast_id": result.get("forecast_id"), "reason": result.get("reason")}, ensure_ascii=False))
    return int(result["status"] != "verified_saved_original_audit")


if __name__ == "__main__":
    raise SystemExit(main())
