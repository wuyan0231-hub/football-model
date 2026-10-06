"""Offline frozen inputs, historical quote selection and screenshot benchmarks.

Hashes establish consistency, not authenticity or historical blindness. This
module neither downloads material nor recognises images nor reads match results.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import stat


PROTOCOL = "frozen-inputs-v1"
ROLES = {"screenshot", "source", "model", "prediction", "rules", "decision", "report", "code"}
MARKETS = {"1x2", "asian_handicap", "totals"}


class EvidenceError(ValueError):
    """A stable machine-readable failure code; never include raw source data."""


def _time(value):
    if not isinstance(value, str):
        raise EvidenceError("time_missing_or_invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise EvidenceError("time_missing_or_invalid") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise EvidenceError("timezone_required")
    return parsed.astimezone(timezone.utc)


def _iso(value):
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _now():
    return datetime.now(timezone.utc)


def _bytes(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2,
                       allow_nan=False) + "\n").encode("utf-8")


def _hash(value):
    return hashlib.sha256(value).hexdigest()


def _name(value):
    if not isinstance(value, str) or not value.strip():
        raise EvidenceError("identifier_required")
    return value


def _relative(value):
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise EvidenceError("unsafe_path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {".", "..", ""} for part in value.split("/")):
        raise EvidenceError("unsafe_path")
    return path


def _no_symlinks(path):
    path = Path(path).absolute()
    for part in [path, *path.parents]:
        if part.is_symlink():
            raise EvidenceError("symlink_forbidden")
    return path


def _read_regular(root, relative):
    path = _no_symlinks(Path(root) / _relative(relative))
    if not stat.S_ISREG(path.lstat().st_mode):
        raise EvidenceError("regular_file_required")
    # O_NOFOLLOW prevents substitution of the final component by a symlink.
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    with os.fdopen(fd, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise EvidenceError("regular_file_required")
        return handle.read()


def _file_entry(item, created):
    if not isinstance(item, dict):
        raise EvidenceError("invalid_source_record")
    if item.get("role") not in ROLES:
        raise EvidenceError("role_not_allowed_results_belong_to_scoring")
    path = str(_relative(item.get("path")))
    observed = item.get("observed_at")
    published = item.get("published_at")
    for value in (observed, published):
        if value is not None and _time(value) > created:
            raise EvidenceError("source_time_in_future")
    if observed is not None and published is not None and _time(published) > _time(observed):
        raise EvidenceError("publication_after_observation")
    result = {"source_id": _name(item.get("source_id")), "role": item["role"],
              "original_path": path, "path": "originals/" + path,
              "observed_at": _iso(_time(observed)) if observed is not None else None,
              "published_at": _iso(_time(published)) if published is not None else None}
    # Locations and source identifiers are metadata only; never fetch them.
    if item.get("source_ref") is not None:
        result["source_ref"] = _name(item["source_ref"])
    return result


def _qualification(record):
    cutoff, kickoff, created = map(_time, (record["analysis_cutoff"], record["kickoff_at"], record["created_at"]))
    reasons, source_checks = [], []
    for source in record["sources"]:
        problems = []
        observed = source.get("observed_at")
        if observed is None:
            problems.append("observation_time_unknown")
        elif _time(observed) > cutoff:
            problems.append("observed_after_cutoff")
        published = source.get("published_at")
        if published is not None and _time(published) > cutoff:
            problems.append("published_after_cutoff")
        source_checks.append({"source_id": source["source_id"], "eligible_at_cutoff": not problems,
                              "reasons": problems})
        reasons.extend(problems)
    if cutoff >= kickoff:
        reasons.append("analysis_cutoff_not_pre_match")
    if created >= kickoff:
        reasons.append("frozen_after_kickoff")
    exposure = record.get("future_information_exposure")
    if exposure is not False:
        reasons.append("future_information_seen" if exposure is True else "future_information_exposure_unknown")
    return {"qualification": "research_only" if reasons else "prospective_input_candidate",
            "reasons": sorted(set(reasons)), "source_checks": source_checks,
            "artifact_existed_at_analysis_cutoff": created <= cutoff,
            "historical_blindness_proven": False,
            "prediction_validated": False,
            "scope": "declared_timestamps_and_bytes_only_no_content_truth_or_blindness_proof"}


def freeze_bundle(source_root, output_dir, spec):
    """Copy only declared files into a NEW immutable-by-policy directory.

    The wall clock is never supplied by the caller or copied from analysis_cutoff.
    Late/unknown evidence is preserved but classed as research-only; use
    load_analysis_inputs to enforce cutoff eligibility before analysis.
    """
    if not isinstance(spec, dict) or any(k in spec for k in ("created_at", "generated_at")):
        raise EvidenceError("creation_time_cannot_be_supplied")
    source_root, output_dir = _no_symlinks(source_root), _no_symlinks(output_dir)
    if output_dir.exists():
        raise EvidenceError("destination_exists_no_overwrite")
    created = _now()
    cutoff, kickoff = _time(spec.get("analysis_cutoff")), _time(spec.get("kickoff_at"))
    if cutoff > created:
        raise EvidenceError("analysis_cutoff_in_future")
    if not any(spec.get("future_information_exposure") is v for v in (None, True, False)):
        raise EvidenceError("invalid_future_information_exposure")
    files = spec.get("files")
    if not isinstance(files, list) or not files:
        raise EvidenceError("explicit_nonempty_file_allowlist_required")
    entries, contents, ids, paths = [], {}, set(), set()
    for item in files:
        entry = _file_entry(item, created)
        if entry["source_id"] in ids or entry["path"] in paths:
            raise EvidenceError("duplicate_source_or_path")
        ids.add(entry["source_id"])
        paths.add(entry["path"])
        content = _read_regular(source_root, entry["original_path"])
        entry.update(bytes=len(content), sha256=_hash(content))
        entries.append(entry)
        contents[entry["path"]] = content
    output_dir.mkdir(parents=True, exist_ok=False)
    for path, content in contents.items():
        dest = output_dir / path
        dest.parent.mkdir(parents=True, exist_ok=True)
        with dest.open("xb") as handle:
            handle.write(content)
    # Record the real time after copying originals, not the caller's as-of time.
    created = _now()
    record = {"schema_version": "match-archive-1.0", "evidence_protocol": PROTOCOL,
              "archive_scope": "frozen_analysis_inputs_subset_not_complete_match_report",
              "match_key": _name(spec.get("match_key")), "version_id": _name(spec.get("version_id")),
              "analysis_cutoff": _iso(cutoff), "kickoff_at": _iso(kickoff),
              "created_at": _iso(created), "sources": entries,
              "future_information_exposure": spec.get("future_information_exposure")}
    record["eligibility"] = _qualification(record)
    contents["record.json"] = _bytes(record)
    manifest = {"evidence_protocol": PROTOCOL,
                "files": [{"path": path, "bytes": len(content), "sha256": _hash(content)}
                          for path, content in sorted(contents.items())]}
    manifest_bytes = _bytes(manifest)
    with (output_dir / "record.json").open("xb") as handle:
        handle.write(contents["record.json"])
    with (output_dir / "manifest.json").open("xb") as handle:
        handle.write(manifest_bytes)
    return {"status": "frozen", "manifest_sha256": _hash(manifest_bytes),
            "created_at": record["created_at"], **record["eligibility"]}


def _verified_record(bundle_dir, expected_manifest_sha256=None):
    root = _no_symlinks(bundle_dir)
    manifest_bytes = _read_regular(root, "manifest.json")
    digest = _hash(manifest_bytes)
    if expected_manifest_sha256 is not None and digest != expected_manifest_sha256:
        raise EvidenceError("manifest_anchor_mismatch")
    manifest = json.loads(manifest_bytes)
    if not isinstance(manifest, dict) or manifest.get("evidence_protocol") != PROTOCOL or not isinstance(manifest.get("files"), list):
        raise EvidenceError("unsupported_manifest")
    listed, content_by_path = set(), {}
    for entry in manifest["files"]:
        if not isinstance(entry, dict):
            raise EvidenceError("invalid_manifest_entry")
        path = str(_relative(entry["path"]))
        if path in listed or (path != "record.json" and not path.startswith("originals/")):
            raise EvidenceError("invalid_or_duplicate_manifest_path")
        listed.add(path)
        content = _read_regular(root, path)
        if len(content) != entry["bytes"] or _hash(content) != entry["sha256"]:
            raise EvidenceError("file_hash_or_length_mismatch")
        content_by_path[path] = content
    actual = set()
    for directory, directories, files in os.walk(root, followlinks=False):
        for name in directories + files:
            _no_symlinks(Path(directory) / name)
        actual.update(str((Path(directory) / name).relative_to(root)) for name in files)
    if actual != listed | {"manifest.json"} or "record.json" not in listed:
        raise EvidenceError("unlisted_or_missing_file")
    record = json.loads(content_by_path["record.json"])
    if not isinstance(record, dict) or record.get("schema_version") != "match-archive-1.0" or record.get("evidence_protocol") != PROTOCOL:
        raise EvidenceError("unsupported_record")
    for key in ("match_key", "version_id"):
        _name(record.get(key))
    created = _time(record.get("created_at"))
    if created > _now() or _time(record.get("analysis_cutoff")) > created:
        raise EvidenceError("invalid_creation_or_cutoff_time")
    _time(record.get("kickoff_at"))
    sources = record.get("sources")
    if not isinstance(sources, list) or not sources:
        raise EvidenceError("invalid_sources")
    source_paths, source_ids = set(), set()
    for source in sources:
        if not isinstance(source, dict):
            raise EvidenceError("invalid_source_record")
        normalized = _file_entry({**source, "path": source["original_path"]}, created)
        if any(source.get(key) != value for key, value in normalized.items()):
            raise EvidenceError("source_record_mismatch")
        path = source["path"]
        if path in source_paths or source["source_id"] in source_ids or path not in content_by_path:
            raise EvidenceError("source_manifest_mismatch")
        source_paths.add(path)
        source_ids.add(source["source_id"])
        content = content_by_path[path]
        if len(content) != source["bytes"] or _hash(content) != source["sha256"]:
            raise EvidenceError("source_hash_or_length_mismatch")
    if listed != source_paths | {"record.json"}:
        raise EvidenceError("source_manifest_mismatch")
    qualification = _qualification(record)
    if record.get("eligibility") != qualification:
        raise EvidenceError("eligibility_record_mismatch")
    return record, digest, content_by_path


def verify_bundle(bundle_dir, expected_manifest_sha256=None):
    """Recompute hashes and time rules; an external digest is an optional anchor."""
    try:
        record, digest, _ = _verified_record(bundle_dir, expected_manifest_sha256)
        return {"status": "verified", "manifest_sha256": digest,
                "integrity": "externally_anchored" if expected_manifest_sha256 else "internal_consistency_only",
                **record["eligibility"]}
    except (EvidenceError, OSError, ValueError, KeyError, TypeError) as exc:
        return {"status": "rejected", "reason": str(exc) if isinstance(exc, EvidenceError) else "malformed_or_unreadable_bundle",
                "historical_blindness_proven": False, "prediction_validated": False}


def load_analysis_inputs(bundle_dir, expected_manifest_sha256=None):
    """Return ONLY allowlisted input bytes after integrity and cutoff checks.

    A historical research replay may use timely original inputs, but the returned
    qualification still cannot certify it as a blind historical forecast.
    """
    record, _, contents = _verified_record(bundle_dir, expected_manifest_sha256)
    if any(not row["eligible_at_cutoff"] for row in record["eligibility"]["source_checks"]):
        raise EvidenceError("ineligible_analysis_inputs")
    return {"match_key": record["match_key"], "version_id": record["version_id"],
            "eligibility": record["eligibility"],
            "inputs": {s["source_id"]: contents[s["path"]] for s in record["sources"]}}


def _quote_interval(row):
    value, precision = row.get("quote_time"), row.get("time_precision")
    if value is None:
        return None
    timestamp = _time(value)
    if precision == "minute":
        if timestamp.second or timestamp.microsecond:
            raise EvidenceError("minute_precision_must_not_invent_seconds")
        return timestamp, timestamp + timedelta(minutes=1)
    if precision != "second":
        raise EvidenceError("quote_time_precision_required")
    if not re.search(r"[T ]\d{2}:\d{2}:\d{2}", value):
        raise EvidenceError("second_precision_requires_explicit_seconds")
    return timestamp, timestamp


def _quote_state(row):
    state = row.get("status")
    if state == "closed":
        return "closed"
    if state not in {"open", "incomplete", "unknown"}:
        raise EvidenceError("invalid_quote_status")
    if state != "open":
        return state
    market, odds = row["market"], row.get("odds")
    keys = {"1x2": {"home", "draw", "away"}, "asian_handicap": {"home", "away"}, "totals": {"over", "under"}}[market]
    if not isinstance(odds, dict) or set(odds) != keys:
        return "incomplete"
    fmt = row.get("odds_format")
    if fmt not in {"decimal", "hong_kong", "malay", "indonesian"}:
        return "incomplete"
    for value in odds.values():
        if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value):
            return "incomplete"
        if (fmt == "decimal" and value <= 1) or (fmt == "hong_kong" and value <= 0) or (fmt == "malay" and (not -1 <= value <= 1 or value == 0)) or (fmt == "indonesian" and abs(value) < 1):
            return "incomplete"
    if market != "1x2":
        line = row.get("line")
        if isinstance(line, bool) or not isinstance(line, (int, float)) or not math.isfinite(line) or not float(line * 4).is_integer():
            return "incomplete"
        if market == "totals" and line < 0:
            return "incomplete"
    return "open"


def select_quotes(rows, analysis_cutoff):
    """Select complete ROW STATES per source/market/page; never mix row cells.

    row_order 0 is the top/newest row. Minute precision denotes an interval,
    not a made-up :00 second. An unknown/overlapping newer row blocks fallback.
    """
    cutoff, groups, ids = _time(analysis_cutoff), {}, set()
    if not isinstance(rows, list):
        raise EvidenceError("quote_rows_required")
    for row in rows:
        if not isinstance(row, dict) or row.get("market") not in MARKETS:
            raise EvidenceError("invalid_quote_market")
        row_id = _name(row.get("row_id"))
        if row_id in ids:
            raise EvidenceError("duplicate_quote_id")
        ids.add(row_id)
        order = row.get("row_order")
        if isinstance(order, bool) or not isinstance(order, int) or order < 0:
            raise EvidenceError("invalid_row_order")
        key = (_name(row.get("source_id")), row["market"], _name(row.get("sequence")))
        _quote_interval(row)
        _quote_state(row)
        groups.setdefault(key, []).append(row)
    results = []
    for (source, market, sequence), group in sorted(groups.items()):
        group = sorted(group, key=lambda row: row["row_order"])
        if len({row["row_order"] for row in group}) != len(group):
            raise EvidenceError("duplicate_row_order")
        previous_intervals = []
        for row in group:
            interval = _quote_interval(row)
            if interval is not None:
                for prior_lower, prior_upper in previous_intervals:
                    conflict = (interval[0] > prior_upper if prior_lower == prior_upper
                                else interval[0] >= prior_upper)
                    if conflict:
                        raise EvidenceError("page_order_conflicts_with_quote_times")
                previous_intervals.append(interval)
        result = {"source_id": source, "market": market, "sequence": sequence,
                  "snapshot_latest": group[0], "as_of_latest": None,
                  "status": "no_quote_at_cutoff", "excluded_after_cutoff": [], "blocking_row": None}
        for row in group:
            interval = _quote_interval(row)
            if interval is None:
                result.update(status="unknown_quote_time", blocking_row=row)
                break
            lower, upper = interval
            if lower > cutoff:
                result["excluded_after_cutoff"].append(row["row_id"])
                continue
            if upper > cutoff:
                result.update(status="ambiguous_at_cutoff", blocking_row=row)
                break
            result.update(as_of_latest=row, status=_quote_state(row))
            break
        results.append(result)
    return {"analysis_cutoff": _iso(cutoff), "groups": results,
            "scope": "structured_rows_only_no_ocr_or_unseen_history_inference"}


def screenshot_benchmark(truth, predictions, critical_fields=None):
    """Compare labeled real-image rows; refusals count against coverage.

    Label fields are explicit, avoiding fabricated universal OCR denominators.
    Truth must carry each original image's SHA-256 and a reviewer identifier.
    """
    critical = set(critical_fields or {"source_id", "market", "sequence", "row_order", "quote_time", "status", "line", "odds", "odds_format"})
    if not isinstance(truth, list) or not truth or not isinstance(predictions, list):
        raise EvidenceError("nonempty_benchmark_truth_required")
    labels, predicted = {}, {}
    for row in truth:
        if not isinstance(row, dict):
            raise EvidenceError("invalid_truth_record")
        key = _name(row.get("row_id"))
        digest = row.get("image_sha256")
        if not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise EvidenceError("original_image_sha256_required")
        _name(row.get("verified_by"))
        if key in labels or not isinstance(row.get("fields"), dict) or not row["fields"]:
            raise EvidenceError("invalid_or_duplicate_truth")
        labels[key] = row
    for row in predictions:
        if not isinstance(row, dict):
            raise EvidenceError("invalid_prediction_record")
        key = _name(row.get("row_id"))
        if key not in labels or key in predicted:
            raise EvidenceError("unexpected_or_duplicate_prediction")
        if not isinstance(row.get("fields", {}), dict):
            raise EvidenceError("invalid_prediction_fields")
        predicted[key] = row
    def equal(left, right):
        # Python's False == 0 must not turn an invalid line/price into a correct read.
        if isinstance(left, bool) or isinstance(right, bool):
            return type(left) is type(right) and left == right
        if isinstance(left, dict) and isinstance(right, dict):
            return set(left) == set(right) and all(equal(left[k], right[k]) for k in left)
        if isinstance(left, list) and isinstance(right, list):
            return len(left) == len(right) and all(equal(a, b) for a, b in zip(left, right))
        return left == right

    total = read = correct = read_rows = exact_rows = critical_rows = critical_errors = 0
    complete_rows = critical_labeled = critical_read = 0
    field_counts, failures = {}, []
    for key, label in labels.items():
        prediction = predicted.get(key, {})
        fields = {} if prediction.get("abstain") is True else prediction.get("fields", {})
        accepted = bool(set(fields) & set(label["fields"]))
        read_rows += accepted
        complete_rows += set(label["fields"]).issubset(fields)
        row_exact, row_critical = True, False
        for field, expected in label["fields"].items():
            total += 1
            count = field_counts.setdefault(field, {"labeled": 0, "read": 0, "correct": 0})
            count["labeled"] += 1
            critical_labeled += field in critical
            if field not in fields:
                row_exact = False
                continue
            read += 1
            critical_read += field in critical
            count["read"] += 1
            if equal(fields[field], expected):
                correct += 1
                count["correct"] += 1
            else:
                row_exact = False
                is_critical = field in critical
                row_critical |= is_critical
                critical_errors += is_critical
                failures.append({"row_id": key, "field": field, "critical": is_critical})
        exact_rows += row_exact
        critical_rows += row_critical
    return {"scope": "labeled_dataset_only_not_general_ocr_validation", "rows_labeled": len(labels),
            "rows_read": read_rows, "rows_exact": exact_rows, "fields_labeled": total,
            "fields_read": read, "fields_correct": correct, "row_coverage": read_rows / len(labels),
            "complete_row_coverage": complete_rows / len(labels),
            "field_coverage": read / total, "accuracy_among_read_fields": correct / read if read else None,
            "correct_fraction_of_all_labeled_fields": correct / total,
            "critical_error_rows": critical_rows, "critical_error_fields": critical_errors,
            "critical_fields_missing": critical_labeled - critical_read,
            "critical_field_coverage": critical_read / critical_labeled if critical_labeled else None,
            "critical_error_rate_among_read_rows": critical_rows / read_rows if read_rows else None,
            "per_field": field_counts, "failures": failures}


def main(argv=None):
    parser = argparse.ArgumentParser(description="离线证据冻结/核验、历史截点选行、真实截图标注评估")
    commands = parser.add_subparsers(dest="command", required=True)
    freeze = commands.add_parser("freeze")
    freeze.add_argument("--source-root", required=True)
    freeze.add_argument("--output", required=True)
    freeze.add_argument("--spec", required=True)
    verify = commands.add_parser("verify")
    verify.add_argument("--bundle", required=True)
    verify.add_argument("--manifest-sha256")
    quotes = commands.add_parser("quotes")
    quotes.add_argument("--input", required=True)
    quotes.add_argument("--as-of", required=True)
    benchmark = commands.add_parser("benchmark")
    benchmark.add_argument("--truth", required=True)
    benchmark.add_argument("--predictions", required=True)
    args = parser.parse_args(argv)
    def read(path):
        file = _no_symlinks(path)
        return json.loads(_read_regular(file.parent, file.name))
    try:
        if args.command == "freeze":
            result = freeze_bundle(args.source_root, args.output, read(args.spec))
        elif args.command == "verify":
            result = verify_bundle(args.bundle, args.manifest_sha256)
        elif args.command == "quotes":
            result = select_quotes(read(args.input), args.as_of)
        else:
            result = screenshot_benchmark(read(args.truth), read(args.predictions))
    except (ValueError, OSError, KeyError, TypeError) as exc:
        result = {"status": "rejected", "reason": str(exc) if isinstance(exc, EvidenceError) else "malformed_or_unreadable_input"}
    print(_bytes(result).decode("utf-8"), end="")
    return 2 if result.get("status") == "rejected" else 0


if __name__ == "__main__":
    raise SystemExit(main())
