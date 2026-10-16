#!/usr/bin/env python3
"""Bind a later frozen S2 record to one already saved actual current call.

This never calls match_packet or changes its clock. The caller supplies the
exact receipt hash of the original call. All original bytes are verified and
copied; the unmodified native S2 helper replays the later record at that call's
actual information boundary. Publication/freezing still occurs at the real
later wall clock and never grants historical or confirmatory eligibility.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from football_model.advanced_research import implementation_fingerprint
from football_model.decision_record import build_decision_bundle, verify_decision_bundle
from football_model.evidence import _time
from football_model.feature_decisions import _decision_record_link, _digest, _file_ref, _read_ref
from scripts.current_observation_decision import strict_json


def verify_run(run_dir, receipt_sha256):
    root = Path(run_dir)
    if any(p.is_symlink() for p in (root, *root.parents)):
        raise ValueError("original_run_symlink_forbidden")
    root = root.resolve()
    receipt_raw, _ = _read_ref(root, {"path": "receipt.json", "sha256": receipt_sha256})
    receipt = strict_json(receipt_raw)
    if receipt.get("schema_version") != "current-observation-receipt-v1":
        raise ValueError("original_current_receipt_required")
    files = {"receipt.json": receipt_raw}
    for ref in receipt["files"]:
        raw, checked = _read_ref(root, ref)
        if checked["path"] in files:
            raise ValueError("duplicate_original_run_path")
        files[checked["path"]] = raw
    actual = {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}
    if actual != set(files):
        raise ValueError("original_run_file_set_changed")
    packet = strict_json(files["actual-native-model-packet.json"])
    result = strict_json(files["current-observation-result.json"])
    core = {k: v for k, v in result.items() if k != "comparison_id"}
    if _digest(core) != result["comparison_id"]:
        raise ValueError("original_current_comparison_identity_mismatch")
    identity = receipt["identity"]
    if identity.get("entrypoint_path") != "scripts/current_observation_decision.py":
        raise ValueError("original_current_entrypoint_required")
    if (result.get("implementation_identity") != identity
            or _digest({"identity": identity, "comparison_id": result["comparison_id"]}) != receipt["run_id"]
            or root.name != receipt["run_id"]):
        raise ValueError("original_run_identity_mismatch")
    if hashlib.sha256(files["runner.py"]).hexdigest() != identity["entrypoint_sha256"]:
        raise ValueError("original_runner_identity_mismatch")
    if hashlib.sha256(files["config.json"]).hexdigest() != identity["config_sha256"]:
        raise ValueError("original_config_identity_mismatch")
    for name, sha in identity["existing_implementation_fingerprint"]["files"].items():
        if hashlib.sha256(files["code/" + name]).hexdigest() != sha:
            raise ValueError("original_code_closure_identity_mismatch")
    if (implementation_fingerprint() != identity["existing_implementation_fingerprint"]
            or hashlib.sha256((REPO / identity["entrypoint_path"]).read_bytes()).hexdigest() != identity["entrypoint_sha256"]):
        raise ValueError("current_code_differs_from_original_verified_code")
    if (result["native_packet_sha256"] != _digest(packet)
            or _time(result["analysis_as_of"]) != _time(packet["analysis_as_of"])
            or result["match_id"] != packet["fixture"]["match_id"]
            or result["model_records"] != packet["feature_decision_comparison"]["model_records"]
            or result["baseline_parent_sha256"] != packet["feature_decision_comparison"]["baseline_parent_sha256"]):
        raise ValueError("original_packet_result_binding_mismatch")
    if (result.get("historical_as_of_eligible") is not False or result.get("reference_only") is not True
            or result.get("observation_mode") != "current_observation"):
        raise ValueError("original_current_reference_scope_required")
    source_copies = {}
    for item in receipt["source_copies"]:
        ref = item["copy"]
        if ref["path"] not in files or hashlib.sha256(files[ref["path"]]).hexdigest() != ref["sha256"]:
            raise ValueError("original_source_copy_mismatch")
        source_copies[item["original_runtime_path"]] = ref
    for ref in result["evidence"] + result["native_model_evidence"]:
        copied = source_copies.get(ref["path"])
        if copied is None or copied["sha256"] != ref["sha256"]:
            raise ValueError("original_source_closure_incomplete")
    return packet, result, files, receipt


def finalize(run_dir, receipt_sha256, source_root, link_input, output):
    started = datetime.now(timezone.utc)
    runner_raw = Path(__file__).read_bytes()
    packet, result, original_files, original_receipt = verify_run(run_dir, receipt_sha256)
    # Reject this before mkdir or native bundle writes: detecting the changed
    # original file set after writing would already violate immutability.
    out_root = Path(output)
    if any(p.is_symlink() for p in (out_root, *out_root.parents)):
        raise ValueError("output_symlink_forbidden")
    if out_root.resolve().is_relative_to(Path(run_dir).resolve()):
        raise ValueError("finalization_output_inside_original_run_forbidden")
    source_root = Path(source_root)
    if any(p.is_symlink() for p in (source_root, *source_root.parents)):
        raise ValueError("source_root_symlink_forbidden")
    source_root = source_root.resolve()
    link_path = Path(link_input)
    if not link_path.is_absolute():
        link_path = source_root / link_path
    link_raw, link_ref = _read_ref(source_root, _file_ref(source_root, link_path))
    value = strict_json(link_raw)
    record = value["decision_record"]
    cutoff = _time(packet["analysis_as_of"])
    if (_time(record["analysis_cutoff"]) != cutoff
            or _time(record["kickoff_at"]) != _time(packet["fixture"]["kickoff"])
            or str(record["match_id"]) != str(packet["fixture"]["match_id"])):
        raise ValueError("later_record_must_bind_exact_actual_original_packet")
    sources = {}
    for ref in record.get("inputs", []):
        # Inputs frozen after the original call cannot become its information.
        # A later decision publication is allowed and has its own real clock.
        if _time(ref.get("observed_at")) > cutoff or (ref.get("published_at") is not None
                and _time(ref["published_at"]) > cutoff):
            raise ValueError("later_external_information_after_actual_packet_cutoff")
        raw, checked = _read_ref(source_root, ref)
        sources[checked["path"]] = raw
    linked = _decision_record_link(source_root, value, result, packet["fixture"], cutoff)
    for ref in linked.get("evidence", []):
        raw, checked = _read_ref(source_root, ref)
        sources[checked["path"]] = raw
    # Close the read race before reserving the append-only derived directory.
    for path, raw in sources.items():
        _read_ref(source_root, {"path": path, "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)})
    _read_ref(source_root, link_ref)
    verify_run(run_dir, receipt_sha256)
    if Path(__file__).read_bytes() != runner_raw:
        raise ValueError("finalizer_changed_during_execution")
    identity = {"schema_version": "current-observation-s2-finalization-identity-v1",
        "original_run_id": original_receipt["run_id"], "original_receipt_sha256": receipt_sha256,
        "link_input_ref": link_ref, "finalizer_sha256": hashlib.sha256(runner_raw).hexdigest(),
        "existing_implementation_fingerprint": original_receipt["identity"]["existing_implementation_fingerprint"]}
    out = out_root / _digest(identity)
    out.mkdir(parents=True, exist_ok=False)
    files = []
    def write(relative, raw):
        path = out / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("xb") as stream:
            stream.write(raw)
        ref = {"path": relative, "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}
        files.append(ref)
        return ref
    encode = lambda obj: (json.dumps(obj, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n").encode()
    for path, raw in original_files.items():
        write("original-current-call/" + path, raw)
    source_refs = []
    for index, (path, raw) in enumerate(sorted(sources.items())):
        source_refs.append({"original_source_path": path,
            "copy": write(f"s2-source-originals/{index:03d}-" + hashlib.sha256(raw).hexdigest() + Path(path).suffix, raw)})
    write("finalizer.py", runner_raw)
    write("original-link-input.json", link_raw)
    write("native-s2-link-result.json", encode(linked))
    # A fresh full S2 bundle is frozen now; its creation is never backdated to
    # cutoff. Native build and native verification both run on actual sources.
    built = build_decision_bundle(record, out / "s2-decision-bundle", source_root=source_root)
    verified = verify_decision_bundle(out / "s2-decision-bundle", built["manifest_sha256"])
    complete = (linked["status"] == "structural_checks_passed"
        and built.get("status") == "structural_checks_passed" and verified.get("status") == "verified"
        and verified.get("decision_status") == "structural_checks_passed"
        and verified.get("manifest_sha256") == built["manifest_sha256"])
    write("s2-build-receipt.json", encode(built))
    write("s2-verify-receipt.json", encode(verified))
    for path in sorted((out / "s2-decision-bundle").rglob("*")):
        if path.is_file():
            raw = path.read_bytes()
            files.append({"path": path.relative_to(out).as_posix(), "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)})
    # The originals and code must remain exact through both native operations.
    verify_run(run_dir, receipt_sha256)
    for path, raw in sources.items():
        _read_ref(source_root, {"path": path, "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)})
    _read_ref(source_root, link_ref)
    if Path(__file__).read_bytes() != runner_raw:
        raise ValueError("finalizer_changed_during_execution")
    receipt = {"schema_version": "current-observation-native-s2-finalization-receipt-v1",
        "identity": identity, "started_at": started.isoformat(),
        "completed_at": datetime.now(timezone.utc).isoformat(), "actual_original_analysis_as_of": packet["analysis_as_of"],
        "new_match_packet_call_performed": False, "monkey_patch_used": False,
        "original_run_unchanged": True, "historical_as_of_eligible": False, "confirmation_eligible": False,
        "status": "structural_checks_passed" if complete else "blocked",
        "native_link_status": linked["status"], "native_build_status": built.get("status"),
        "native_verify_status": verified.get("status"), "native_verify_decision_status": verified.get("decision_status"),
        "native_manifest_sha256": built["manifest_sha256"], "native_validation": linked["validation"],
        "source_copies": source_refs, "files": files,
        "scope": "later_decision_publication_bound_to_exact_actual_saved_call; no_retroactive_publication_or_blindness"}
    write("receipt.json", encode(receipt))
    return out, receipt


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--run-receipt-sha256", required=True)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--link-input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    out, receipt = finalize(args.run_dir, args.run_receipt_sha256, args.source_root, args.link_input, args.output)
    print(json.dumps({"output": str(out), "status": receipt["status"], "historical_as_of_eligible": False}))
    if receipt["status"] != "structural_checks_passed":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
