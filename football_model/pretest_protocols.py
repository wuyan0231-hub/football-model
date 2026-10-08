"""Freeze actual policy originals and check prospective activation prerequisites.

Preparation is not an independent decision, an activated confirmation study or
source authentication.  No model, API, screenshot OCR or promotion runs here.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

from .evidence import EvidenceError, _no_symlinks, _read_regular, _relative, _time


FRAMEWORK_SCHEMA = "s2-framework-comparison-prepared-1.0"
F5_SCHEMA = "f5c_confirmation_protocol_independent_preparation_v2"
FREEZE_SCHEMA = "s2-pretest-preparation-freeze-1.0"
GATE_SCHEMA = "s2-pretest-activation-check-1.0"
FRAMEWORK_GATES = frozenset({"deployed_policy_source_binding", "neutral_input_and_model_readback",
    "independent_context_and_attempt_logging", "durable_output_and_restore"})
F5_GATES = frozenset({"deployed_policy_source_binding", "genuine_result_maturity_chain",
    "long_term_capacity_and_restore", "confirmation_capture_gate_acceptance"})


def _now():
    return datetime.now(timezone.utc)


def _iso(value):
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _bytes(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2,
                       allow_nan=False) + "\n").encode("utf-8")


def _sha(value):
    return hashlib.sha256(value).hexdigest()


def _hash(value, length=64):
    return isinstance(value, str) and len(value) == length and all(c in "0123456789abcdef" for c in value)


def _new(path, raw):
    path = _no_symlinks(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as handle:
        handle.write(raw)


def _validate(spec):
    if not isinstance(spec, dict) or spec.get("schema_version") not in {FRAMEWORK_SCHEMA, F5_SCHEMA}:
        raise EvidenceError("supported_preparation_required")
    if spec.get("status") != "prepared_not_active" or spec.get("pilot_counts_as_confirmation") is not False:
        raise EvidenceError("preparation_must_not_activate_or_reclassify_pilot")
    gates = spec.get("activation_rules", {}).get("required_gate_kinds")
    required = FRAMEWORK_GATES if spec["schema_version"] == FRAMEWORK_SCHEMA else F5_GATES
    if (not isinstance(gates, list) or not gates
            or any(not isinstance(g, str) or not g.strip() for g in gates)
            or len(gates) != len(set(gates)) or not required.issubset(set(gates))):
        raise EvidenceError("mandatory_activation_gate_set_cannot_be_deleted_or_duplicated")
    if spec.get("schema_version") == FRAMEWORK_SCHEMA:
        if spec.get("intervention", {}).get("only_changed_variable") != "frozen_analysis_policy_package":
            raise EvidenceError("single_policy_package_intervention_required")
        if spec.get("model_control") != "same_frozen_model_and_forecast":
            raise EvidenceError("same_frozen_model_control_required")
        if spec.get("shadow_grades", {}).get("automatic_formal_action") is not False:
            raise EvidenceError("shadow_grades_cannot_promote_formal_actions")
    else:
        stages = spec.get("stages", {})
        if (stages.get("target_enrolled_unique_baseline_opportunities") != 10000
                or stages.get("minimum_valid_unique_pairs_for_success") != 9000
                or stages.get("minimum_elapsed_calendar_days") != 365
                or stages.get("maximum_duration_days") != 548):
            raise EvidenceError("F5_v2_prespecified_counts_and_duration_required")
        if spec.get("final_decision", {}).get("automatic_production_promotion") is not False:
            raise EvidenceError("no_automatic_model_promotion_required")
    paths = spec.get("capture_source_paths")
    if not isinstance(paths, list) or not paths or len(paths) != len(set(paths)):
        raise EvidenceError("unique_actual_source_paths_required")
    for path in paths:
        _relative(path)
    return spec


def freeze_preparation(spec, output_dir, *, source_root, source_commit,
                       left_framework=None, left_odds=None, additional_sources=()):
    """Capture complete original bytes; emit policy and independent task templates.

    ``source_commit`` labels the declared source ancestor/release; every actual
    source byte is bound separately, because a working tree can differ from it.
    The caller must independently authenticate the eventual deployment commit.
    """
    _validate(spec)
    if not _hash(source_commit, 40):
        raise EvidenceError("explicit_full_source_commit_required")
    output = _no_symlinks(output_dir)
    output.mkdir(parents=True, exist_ok=False)
    files = {}
    originals = []
    paths = [*spec["capture_source_paths"], *additional_sources]
    if len(paths) != len(set(paths)):
        raise EvidenceError("duplicate_source_path")
    for path in paths:
        _relative(path)
        raw = _read_regular(source_root, path)
        ref = {"original_path": path, "path": "originals/" + _sha(raw),
               "sha256": _sha(raw), "bytes": len(raw), "role": "code" if path.endswith(".py") else "rules"}
        originals.append(ref)
        files[ref["path"]] = raw
    policy_refs = {}
    if spec["schema_version"] == FRAMEWORK_SCHEMA:
        if left_framework is None or left_odds is None:
            raise EvidenceError("complete_left_policy_originals_required")
        left = []
        for key, name, path in (("framework", "football-framework-v1.7-r4.md", left_framework),
                                ("odds", "odds-screenshot-reading-v1.2-r4.md", left_odds)):
            actual = _no_symlinks(path)
            raw = _read_regular(actual.parent, actual.name)
            expected = spec["policies"]["left"].get("expected_original_sha256s", {}).get(key)
            if expected is not None and _sha(raw) != expected:
                raise EvidenceError("left_policy_original_not_frozen_uploaded_version")
            ref = {"original_path": name, "path": "originals/" + _sha(raw),
                   "sha256": _sha(raw), "bytes": len(raw), "role": "rules"}
            files[ref["path"]] = raw
            left.append(ref)
        for side, entries in (("left", left), ("right", originals)):
            definition = {"schema_version": "s2-frozen-analysis-policy-package-1.0",
                "policy_id": spec["policies"][side]["policy_id"],
                "name": spec["policies"][side]["name"], "originals": entries,
                "adapter_rule": "retain full actual raw output; map only explicitly present fields; never add historical probability, grade, time, condition or execution"}
            raw = _bytes(definition)
            definition_path = "policies/" + side + ".json"
            files[definition_path] = raw
            policy_refs[side] = {"policy_id": definition["policy_id"],
                                 "definition": {"path": definition_path, "sha256": _sha(raw)}}
            files["tasks/" + side + ".json"] = _bytes({
                "schema_version": "s2-independent-analysis-task-template-1.0", "side": side,
                "policy": policy_refs[side], "status": "template_no_actual_analysis",
                "request_id": None, "common_opportunity_manifest_sha256": None,
                "context_isolation": "fresh context with only this policy package and the exact neutral common pool; no sibling answer, gold OCR truth or outcome",
                "task": spec["target_task"], "actual_run_required": True,
                "first_attempt": "preserve actual first output, explicit abstention or failure; all retries append",
                "shared_forecast": "the same complete saved score distribution and parent lineage; no refitting or manual lambda changes",
                "future_information_exposure": None,
                "warning": "a task template is not an executed independent judgment"})
    now = _now()
    frozen = {"schema_version": FREEZE_SCHEMA, "frozen_at": _iso(now),
              "status": "prepared_not_active", "protocol": deepcopy(spec),
              "declared_source_commit": source_commit,
              "commit_role": "declared_release_or_ancestor_not_proof_of_working_tree_identity",
              "sources": originals, "additional_source_paths": list(additional_sources),
              "policies": policy_refs,
              "pilot_counts_as_confirmation": False,
              "actual_independent_outputs": 0, "confirmation_activated": False}
    files["preparation.json"] = _bytes(frozen)
    manifest = {"schema_version": FREEZE_SCHEMA, "files": [
        {"path": path, "sha256": _sha(raw), "bytes": len(raw)} for path, raw in sorted(files.items())]}
    for path, raw in files.items():
        _new(output / path, raw)
    raw = _bytes(manifest)
    _new(output / "manifest.json", raw)
    return {"path": str(output), "manifest_sha256": _sha(raw), "frozen_at": frozen["frozen_at"],
            "status": frozen["status"], "policies": policy_refs,
            "actual_independent_outputs": 0, "confirmation_activated": False}


def read_preparation(root, expected_manifest_sha256):
    if not _hash(expected_manifest_sha256):
        raise EvidenceError("external_preparation_manifest_anchor_required")
    raw = _read_regular(root, "manifest.json")
    if _sha(raw) != expected_manifest_sha256:
        raise EvidenceError("preparation_manifest_hash_mismatch")
    manifest = json.loads(raw)
    if manifest.get("schema_version") != FREEZE_SCHEMA:
        raise EvidenceError("invalid_preparation_manifest")
    files = {}
    for ref in manifest["files"]:
        path = str(_relative(ref["path"]))
        if path in files:
            raise EvidenceError("duplicate_preparation_file")
        original = _read_regular(root, path)
        if _sha(original) != ref["sha256"] or len(original) != ref["bytes"]:
            raise EvidenceError("preparation_original_changed")
        files[path] = original
    actual_files = set()
    actual_root = _no_symlinks(root)
    for path in actual_root.rglob("*"):
        _no_symlinks(path)
        if path.is_file():
            actual_files.add(path.relative_to(actual_root).as_posix())
    if actual_files != {*files, "manifest.json"}:
        raise EvidenceError("unlisted_or_missing_preparation_file")
    preparation = json.loads(files["preparation.json"])
    _validate(preparation["protocol"])
    if (preparation.get("status") != "prepared_not_active" or _time(preparation["frozen_at"]) > _now()
            or preparation.get("pilot_counts_as_confirmation") is not False
            or preparation.get("confirmation_activated") is not False
            or preparation.get("actual_independent_outputs") != 0
            or not _hash(preparation.get("declared_source_commit"), 40)):
        raise EvidenceError("invalid_preparation_status_or_future_clock")
    expected_paths = [*preparation["protocol"]["capture_source_paths"],
                      *preparation.get("additional_source_paths", [])]
    for path in expected_paths:
        _relative(path)
    actual_paths = [ref["original_path"] for ref in preparation["sources"]]
    if (len(expected_paths) != len(set(expected_paths)) or len(actual_paths) != len(set(actual_paths))
            or set(expected_paths) != set(actual_paths)):
        raise EvidenceError("declared_capture_sources_not_complete")
    for ref in preparation["sources"]:
        if (ref["path"] != "originals/" + ref["sha256"]
                or _sha(files[ref["path"]]) != ref["sha256"]
                or len(files[ref["path"]]) != ref["bytes"]):
            raise EvidenceError("preparation_source_binding_mismatch")
    expected_sides = {"left", "right"} if preparation["protocol"]["schema_version"] == FRAMEWORK_SCHEMA else set()
    if set(preparation["policies"]) != expected_sides:
        raise EvidenceError("both_declared_policy_packages_required")
    for side, policy in preparation["policies"].items():
        definition = json.loads(files[policy["definition"]["path"]])
        if (_sha(files[policy["definition"]["path"]]) != policy["definition"]["sha256"]
                or definition["policy_id"] != policy["policy_id"]
                or policy["policy_id"] != preparation["protocol"]["policies"][side]["policy_id"]):
            raise EvidenceError("policy_definition_binding_mismatch")
        if side == "right" and definition["originals"] != preparation["sources"]:
            raise EvidenceError("right_policy_package_not_complete_declared_sources")
        if side == "left":
            expected_names = {"football-framework-v1.7-r4.md", "odds-screenshot-reading-v1.2-r4.md"}
            if ({ref["original_path"] for ref in definition["originals"]} != expected_names
                    or len(definition["originals"]) != 2):
                raise EvidenceError("left_complete_framework_and_odds_originals_required")
        for ref in definition["originals"]:
            if (ref["path"] != "originals/" + ref["sha256"]
                    or _sha(files[ref["path"]]) != ref["sha256"]
                    or len(files[ref["path"]]) != ref["bytes"]):
                raise EvidenceError("policy_original_binding_mismatch")
        task = json.loads(files["tasks/" + side + ".json"])
        if (task.get("side") != side or task.get("policy") != policy
                or task.get("task") != preparation["protocol"]["target_task"]
                or task.get("status") != "template_no_actual_analysis"
                or task.get("request_id") is not None):
            raise EvidenceError("independent_task_template_binding_mismatch")
    return preparation


def instantiate_requests(frozen_root, manifest_sha256, requests, trial_root):
    """Register future real request IDs with the existing F3/F4 APIs.

    No neutral pool is fabricated and no LLM is run. At request intake the caller
    must choose a genuinely future analysis cutoff so registration precedes it.
    """
    from .decision_scoring import register_scoring_protocol
    from .decision_trials import register_trial
    frozen = read_preparation(frozen_root, manifest_sha256)
    protocol = frozen["protocol"]
    if protocol["schema_version"] != FRAMEWORK_SCHEMA:
        raise EvidenceError("framework_request_preparation_required")
    if not isinstance(requests, list) or not requests:
        raise EvidenceError("real_future_request_universe_required")
    now = _now()
    allowed = protocol["scope"]["competitions"]
    for request in requests:
        if request.get("competition") not in allowed or request.get("target_task") != protocol["target_task"]:
            raise EvidenceError("request_outside_frozen_comparison_scope")
        if not now < _time(request.get("analysis_cutoff")) < _time(request.get("kickoff")):
            raise EvidenceError("cannot_backdate_request_or_reclassify_pilot")
    scoring = register_scoring_protocol({"name": protocol["study_name"],
        "quote_protocol": "QUOTE_REF", "usage": "single",
        "scope": {"competitions": allowed, "match_ids": "all"}, "evaluation_mode": "prospective",
        "unit_stake": 1, "cost_per_unit": 0,
        "decision_selection": "explicit_adopted_version_no_retrospective_switch",
        "condition_policy": "satisfied_or_no_conditions_only"}, trial_root)
    # Copy scoring原件 to a new staging folder, preserving the existing immutable
    # scoring protocol and the frozen package. The freeze itself is never edited.
    staging = _no_symlinks(trial_root) / "registration_sources" / scoring["protocol_id"]
    staging.mkdir(parents=True, exist_ok=False)
    # Carry a complete portable policy packet, including exact originals and
    # independent task templates. A definition-only copy loses its source bytes.
    packet = staging / "frozen_policies"
    manifest_raw = _read_regular(frozen_root, "manifest.json")
    for ref in json.loads(manifest_raw)["files"]:
        _new(packet / ref["path"], _read_regular(frozen_root, ref["path"]))
    _new(packet / "manifest.json", manifest_raw)
    read_preparation(packet, manifest_sha256)
    score_raw = Path(scoring["path"]).read_bytes()
    _new(staging / "scoring.json", score_raw)
    policies = deepcopy(frozen["policies"])
    for side, policy in policies.items():
        policy["definition"]["path"] = "frozen_policies/" + policy["definition"]["path"]
    spec = {"experiment_id": protocol["study_id"] + ":" + scoring["protocol_id"],
        "comparison": "independent_ticket_selection", "scope_description": protocol["scope"]["description"],
        "primary_objective": protocol["metrics"]["primary"],
        "allowed_degradation": protocol["metrics"]["guardrail"],
        "review_schedule": protocol["stopping"]["review"], "stopping_rule": protocol["stopping"]["rule"],
        "opportunity_sampling_rule": protocol["scope"]["sampling_rule"],
        "target": "REGULATION", "usage": "single", "quote_protocol": "QUOTE_REF", "unit_stake": 1,
        "model_control": "same_frozen_model_and_forecast", "abstention_zero_exposure": True,
        "version_selection": "first_registered_actual_output_including_failure", "policies": policies,
        "scoring_protocol": {"path": "scoring.json", "sha256": scoring["protocol_id"]},
        "requests": deepcopy(requests)}
    registered = register_trial(spec, trial_root, source_root=staging)
    return {"preparation_manifest_sha256": manifest_sha256, "scoring": scoring, "trial": registered,
            "portable_policy_packet": {"path": str(packet), "manifest_sha256": manifest_sha256},
            "actual_independent_outputs": 0, "neutral_opportunity_created": False,
            "qualification_warning": "registration does not execute analysis; open actual neutral evidence and submit independent first outputs"}


def check_activation(frozen_root, manifest_sha256, request, *, evidence_root):
    """Check explicit review receipts; never activate a pipeline or rewrite pilot.

    Gate declarations must reference original persisted bytes. Completing these
    declarations does not verify their football/storage semantics or readiness.
    The caller/reviewer remains responsible for authenticating the source and
    semantic truth of each evidence receipt; this checker cannot prove either.
    """
    frozen = read_preparation(frozen_root, manifest_sha256)
    protocol = frozen["protocol"]
    now = _now()
    reasons = []
    if not isinstance(request, dict):
        raise EvidenceError("activation_request_object_required")
    if request.get("preparation_manifest_sha256") != manifest_sha256:
        reasons.append("activation_not_bound_to_frozen_preparation")
    if request.get("pilot_reclassification") is not False or request.get("backfilled_samples", 0) != 0:
        reasons.append("pilot_or_historical_reclassification_forbidden")
    try:
        if not _time(frozen["frozen_at"]) <= now < _time(request.get("start_at")):
            reasons.append("future_exact_start_after_real_activation_required")
    except EvidenceError:
        reasons.append("future_exact_start_missing")
    if not _hash(request.get("deployed_source_commit"), 40):
        reasons.append("authenticated_deployed_source_commit_missing")
    gates = request.get("gates", {})
    if not isinstance(gates, dict):
        raise EvidenceError("activation_gates_object_required")
    reviewed = {}
    for kind in protocol["activation_rules"]["required_gate_kinds"]:
        ref = gates.get(kind)
        if not isinstance(ref, dict) or not _hash(ref.get("sha256")):
            reasons.append("gate_missing_" + kind)
            continue
        raw = _read_regular(evidence_root, ref.get("path"))
        if _sha(raw) != ref["sha256"]:
            raise EvidenceError("activation_evidence_hash_mismatch")
        gate = json.loads(raw)
        if (gate.get("schema_version") != "s2-pretest-activation-gate-review-1.0"
                or gate.get("gate_kind") != kind or gate.get("status") != "verified"
                or gate.get("preparation_manifest_sha256") != manifest_sha256
                or gate.get("source_commit") != request.get("deployed_source_commit")
                or not isinstance(gate.get("reviewer"), str) or not gate["reviewer"].strip()):
            reasons.append("gate_review_invalid_" + kind)
            continue
        try:
            if _time(gate.get("reviewed_at")) > now:
                reasons.append("gate_review_from_future_" + kind)
        except EvidenceError:
            reasons.append("gate_review_time_missing_" + kind)
        upstream = gate.get("original_evidence")
        if not isinstance(upstream, list) or not upstream:
            reasons.append("gate_original_evidence_missing_" + kind)
            continue
        for original in upstream:
            if not isinstance(original, dict) or not _hash(original.get("sha256")):
                raise EvidenceError("gate_original_reference_invalid")
            original_raw = _read_regular(evidence_root, original.get("path"))
            if _sha(original_raw) != original["sha256"]:
                raise EvidenceError("gate_original_hash_mismatch")
            if _time(original.get("persisted_at")) > now:
                reasons.append("gate_original_not_yet_persisted_" + kind)
        if kind == "genuine_result_maturity_chain":
            scored = gate.get("unique_real_scored_pilot_matches")
            mature = gate.get("unique_real_48h_mature_pilot_matches")
            if (type(scored) is not int or scored < 1
                    or type(mature) is not int or mature < 1
                    or gate.get("all_grids_recomputed") is not True
                    or gate.get("result_revision_append_only_verified") is not True):
                reasons.append("real_result_and_48h_maturity_chain_unverified")
        if kind == "long_term_capacity_and_restore":
            if (gate.get("funded_until_maximum_duration") is not True
                    or gate.get("restore_test_verified") is not True
                    or gate.get("forecast_upper_bound_not_just_current_snapshot") is not True):
                reasons.append("long_term_capacity_or_restore_unverified")
        reviewed[kind] = ref["sha256"]
    return {"schema_version": GATE_SCHEMA, "checked_at": _iso(now),
        "preparation_manifest_sha256": manifest_sha256,
        "status": "blocked" if reasons else "review_declarations_complete_needs_independent_activation_review",
        "blocking_reasons": sorted(set(reasons)), "reviewed_gate_sha256s": reviewed,
        "pipeline_activated": False, "confirmation_samples_created": 0,
        "actual_evidence_semantics_verified": False, "activation_readiness_established": False,
        "check_scope": "file_integrity_and_reviewer_declarations_only",
        "pilot_reclassified": False, "automatic_promotion": False,
        "evidence_limit": "checks original bytes and reviewed declarations; cannot authenticate source, independent cognition or unobserved history"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    freeze = sub.add_parser("freeze")
    freeze.add_argument("--protocol", required=True)
    freeze.add_argument("--source-root", required=True)
    freeze.add_argument("--source-commit", required=True)
    freeze.add_argument("--left-framework")
    freeze.add_argument("--left-odds")
    freeze.add_argument("--additional-source", action="append", default=[])
    freeze.add_argument("--output", required=True)
    for name in ("requests", "check-activation", "verify"):
        item = sub.add_parser(name)
        item.add_argument("--frozen-root", required=True)
        item.add_argument("--manifest-sha256", required=True)
        if name == "requests":
            item.add_argument("--requests", required=True)
            item.add_argument("--trial-root", required=True)
        elif name == "check-activation":
            item.add_argument("--request", required=True)
            item.add_argument("--evidence-root", required=True)
        item.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    if args.command == "freeze":
        value = freeze_preparation(json.loads(Path(args.protocol).read_bytes()), args.output,
            source_root=args.source_root, source_commit=args.source_commit,
            left_framework=args.left_framework, left_odds=args.left_odds, additional_sources=args.additional_source)
    elif args.command == "requests":
        value = instantiate_requests(args.frozen_root, args.manifest_sha256,
            json.loads(Path(args.requests).read_bytes()), args.trial_root)
        _new(Path(args.output), _bytes(value))
    elif args.command == "check-activation":
        value = check_activation(args.frozen_root, args.manifest_sha256,
            json.loads(Path(args.request).read_bytes()), evidence_root=args.evidence_root)
        _new(Path(args.output), _bytes(value))
    else:
        frozen = read_preparation(args.frozen_root, args.manifest_sha256)
        value = {"status": "verified_preparation_only", "frozen_at": frozen["frozen_at"],
                 "confirmation_activated": False, "actual_independent_outputs": 0}
        _new(Path(args.output), _bytes(value))
    print(json.dumps(value, ensure_ascii=False, indent=2))
    return int(args.command == "check-activation" and value["status"] == "blocked")


if __name__ == "__main__":
    raise SystemExit(main())
