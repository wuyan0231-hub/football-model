"""Preregistered independent decision trials, with append-only actual outputs.

This orchestrates existing F2 evidence verification and F3 scoring. It does not
invent independent decisions, call a model, select bets, or promote a policy.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

from .decision_record import (PROTOCOL as DECISION_BUNDLE, _read_manifest,
                              _write_bundle, build_decision_bundle, verify_decision_bundle)
from .decision_scoring import read_decision_score
from .evidence import EvidenceError, _no_symlinks, _read_regular, _time
from .policy_evaluation import IDENTITY, RETURN_FIELDS, cluster_interval

SCHEMA = "s2-independent-trial-1.0"
OPPORTUNITY = "s2-trial-opportunity-1.0"
SUBMISSION = "s2-trial-submission-1.0"
RUN_SCHEMA = "s2-independent-decision-run-1.0"
SIDES = ("left", "right")


def _now():
    return datetime.now(timezone.utc)


def _iso(value):
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _bytes(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n").encode()


def _sha(blob):
    return hashlib.sha256(blob).hexdigest()


def _hash(value):
    return isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def _external(ref, root):
    if not isinstance(ref, dict) or not _hash(ref.get("sha256")):
        raise EvidenceError("external_source_hash_required")
    raw = _read_regular(root, ref.get("path"))
    if _sha(raw) != ref["sha256"]:
        raise EvidenceError("external_source_hash_mismatch")
    return raw


def _new(path, raw):
    path = _no_symlinks(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(raw)


def _spec(spec):
    if not isinstance(spec, dict) or spec.get("comparison") != "independent_ticket_selection":
        raise EvidenceError("independent_ticket_selection_required")
    for field in ("experiment_id", "scope_description", "primary_objective", "allowed_degradation",
                  "review_schedule", "stopping_rule", "opportunity_sampling_rule"):
        if not _text(spec.get(field)):
            raise EvidenceError("protocol_field_required_" + field)
    if spec.get("target") != "REGULATION" or spec.get("usage") not in {"single", "parlay_leg"}:
        raise EvidenceError("regulation_and_explicit_usage_required")
    if spec.get("quote_protocol") not in RETURN_FIELDS:
        raise EvidenceError("explicit_quote_protocol_required")
    if spec.get("version_selection") != "first_registered_actual_output_including_failure":
        raise EvidenceError("first_registered_output_policy_required")
    if spec.get("model_control") != "same_frozen_model_and_forecast":
        raise EvidenceError("same_model_control_required")
    if spec.get("unit_stake") != 1 or isinstance(spec.get("unit_stake"), bool):
        raise EvidenceError("one_unit_per_opportunity_required")
    if not isinstance(spec.get("abstention_zero_exposure"), bool):
        raise EvidenceError("explicit_abstention_policy_required")
    policies = spec.get("policies")
    if not isinstance(policies, dict) or set(policies) != set(SIDES):
        raise EvidenceError("two_named_policies_required")
    for policy in policies.values():
        if not _text(policy.get("policy_id")) or not _hash(policy.get("definition", {}).get("sha256")):
            raise EvidenceError("policy_identity_and_definition_required")
    if policies["left"]["policy_id"] == policies["right"]["policy_id"]:
        raise EvidenceError("distinct_policy_ids_required")
    requests = spec.get("requests")
    if not isinstance(requests, list) or not requests:
        raise EvidenceError("nonempty_complete_request_universe_required")
    ids = set()
    tasks = set()
    for request in requests:
        if not isinstance(request, dict) or not _text(request.get("request_id")) or request["request_id"] in ids:
            raise EvidenceError("unique_request_ids_required")
        if any(request.get(k) is None for k in IDENTITY) or not _text(request.get("target_task")):
            raise EvidenceError("complete_request_identity_and_target_task_required")
        if _time(request.get("analysis_cutoff")) >= _time(request["kickoff"]):
            raise EvidenceError("cutoff_must_precede_kickoff")
        task = (str(request["competition"]), str(request["match_id"]),
                _iso(_time(request["analysis_cutoff"])), request["target_task"])
        if task in tasks:
            raise EvidenceError("duplicate_opportunity_under_different_request_id")
        tasks.add(task)
        ids.add(request["request_id"])
    return spec


def register_trial(spec, root, *, source_root):
    """Freeze the full request universe, both rule originals and F3 score protocol."""
    _spec(spec)
    sources = {s: _external(spec["policies"][s]["definition"], source_root) for s in SIDES}
    scoring_raw = _external(spec.get("scoring_protocol"), source_root)
    scoring = json.loads(scoring_raw)
    if scoring.get("schema_version") != "s2-decision-scoring-protocol-1.0":
        raise EvidenceError("verified_F3_scoring_protocol_required")
    for field in ("quote_protocol", "usage", "unit_stake"):
        if scoring.get("spec", {}).get(field) != spec[field]:
            raise EvidenceError("scoring_protocol_mismatch_" + field)
    scoring_spec = scoring.get("spec", {})
    if (scoring_spec.get("decision_selection") != "explicit_adopted_version_no_retrospective_switch"
            or scoring_spec.get("condition_policy") != "satisfied_or_no_conditions_only"
            or scoring_spec.get("evaluation_mode") not in {"prospective", "research"}):
        raise EvidenceError("unsupported_scoring_protocol_rules")
    cost = scoring_spec.get("cost_per_unit")
    if cost is not None and (isinstance(cost, bool) or not isinstance(cost, (int, float)) or cost < 0):
        raise EvidenceError("invalid_scoring_cost")
    scope = scoring_spec.get("scope", {})
    for request in spec["requests"]:
        for identity_key, scope_key in (("competition", "competitions"), ("match_id", "match_ids")):
            allowed = scope.get(scope_key)
            if allowed != "all" and (not isinstance(allowed, list) or str(request[identity_key]) not in allowed):
                raise EvidenceError("request_outside_scoring_protocol_scope")
    now = _now()
    if _time(scoring.get("registered_at")) > now:
        raise EvidenceError("scoring_protocol_from_future")
    record = {"schema_version": SCHEMA, "registered_at": _iso(now), "spec": deepcopy(spec),
              "qualification": "prospective_preregistered" if now < min(_time(r["analysis_cutoff"]) for r in spec["requests"]) else "research_only",
              "policy_originals_hex": {s: raw.hex() for s, raw in sources.items()},
              "scoring_protocol_hex": scoring_raw.hex(), "whole_parlay_evaluated": False}
    raw = _bytes(record)
    identity = _sha(raw)
    _new(_no_symlinks(root) / "protocols" / (identity + ".json"), raw)
    return {"protocol_id": identity, "registered_at": record["registered_at"], "qualification": record["qualification"]}


def read_trial(root, protocol_id):
    if not _hash(protocol_id):
        raise EvidenceError("trial_protocol_hash_required")
    raw = _read_regular(root, "protocols/" + protocol_id + ".json")
    if _sha(raw) != protocol_id:
        raise EvidenceError("trial_protocol_hash_mismatch")
    value = json.loads(raw)
    if value.get("schema_version") != SCHEMA or _time(value.get("registered_at")) > _now():
        raise EvidenceError("invalid_trial_protocol")
    _spec(value["spec"])
    for side in SIDES:
        if _sha(bytes.fromhex(value["policy_originals_hex"][side])) != value["spec"]["policies"][side]["definition"]["sha256"]:
            raise EvidenceError("policy_definition_corrupt")
    if _sha(bytes.fromhex(value["scoring_protocol_hex"])) != value["spec"]["scoring_protocol"]["sha256"]:
        raise EvidenceError("scoring_protocol_corrupt")
    return value


def _request(protocol, request_id):
    request = next((r for r in protocol["spec"]["requests"] if r["request_id"] == request_id), None)
    if request is None:
        raise EvidenceError("request_outside_fixed_universe")
    return request


def _folder(root, protocol_id, request_id):
    return _no_symlinks(root) / "trials" / protocol_id / _sha(request_id.encode())


def _identity(core, request):
    for field in IDENTITY:
        actual = core.get("kickoff_at") if field == "kickoff" else core.get(field)
        if field == "kickoff":
            matches = _time(actual) == _time(request[field])
        else:
            matches = str(actual) == str(request[field])
        if not matches:
            raise EvidenceError("request_identity_mismatch_" + field)
    if _time(core.get("analysis_cutoff")) != _time(request["analysis_cutoff"]):
        raise EvidenceError("request_cutoff_mismatch")


def _pool(files):
    core, entries = (json.loads(files[k]) for k in ("decision.json", "evidence.json"))
    factual = [e for e in entries if e.get("kind") == "external_input" and e.get("role") not in {"rules", "code"}]
    # Bind metadata as well as byte identity. Rewriting a source's observation
    # clock, role, or provenance must not manufacture a common information set.
    fields = ("evidence_id", "source_id", "sha256", "role", "observed_at", "published_at", "source_claimed_published_at")
    rows = [{k: e.get(k) for k in fields} for e in factual]
    rows.sort(key=lambda e: (str(e["evidence_id"]), e["sha256"]))
    by_id = {e["evidence_id"]: e for e in entries}
    forecast_entries = [e for e in entries if e.get("role") in {"model", "prediction"}
                        or (e.get("kind") == "derived" and e.get("artifact_type") == "forecast")]
    lineage = {}
    def include(entry):
        identity = entry["evidence_id"]
        if identity in lineage:
            return
        lineage[identity] = {k: v for k, v in entry.items() if k not in {"path", "original_path"}}
        for parent in entry.get("parent_ids", []):
            if parent not in by_id:
                raise EvidenceError("model_lineage_parent_missing")
            include(by_id[parent])
    for entry in forecast_entries:
        include(entry)
    adopted_refs = {ref for record in core["archive"]["model_records"]
                    if record.get("forecast_id") == core.get("forecast_id")
                    for ref in record.get("forecast_evidence_ids", [])}
    adopted_hashes = sorted({e["sha256"] for e in entries
                             if {e.get("evidence_id"), e.get("source_id")} & adopted_refs})
    return {"input_pool_sha256": _sha(_bytes(rows)),
            "quote_pool_sha256": _sha(_bytes(core["adapted_quote_rows"])),
            "model_sha256s": sorted({e["sha256"] for e in forecast_entries}),
            "model_lineage_sha256": _sha(_bytes(lineage)),
            "adopted_forecast_evidence_sha256s": adopted_hashes,
            "forecast_id": core.get("forecast_id"), "forecast_missing_reason": core.get("forecast_missing_reason")}


def _policy_inputs(files, common_files, policy):
    common_entries = json.loads(common_files["evidence.json"])
    common = {_sha(_bytes(e)) for e in common_entries
              if e.get("kind") == "external_input" and e.get("role") in {"rules", "code"}}
    for entry in json.loads(files["evidence.json"]):
        if entry.get("kind") == "external_input" and entry.get("role") in {"rules", "code"}:
            if _sha(_bytes(entry)) not in common and entry["sha256"] != policy["definition"]["sha256"]:
                raise EvidenceError("undeclared_extra_rule_or_code_input")


def open_opportunity(root, protocol_id, request_id, bundle, manifest_sha256):
    """Freeze a neutral F2 evidence-only bundle before either independent run.

    Its candidate list must be empty. A historical choice cannot silently become
    a shared input pool for a supposedly independent rerun.
    """
    protocol = read_trial(root, protocol_id)
    request = _request(protocol, request_id)
    if not _hash(manifest_sha256):
        raise EvidenceError("external_common_manifest_anchor_required")
    verified = verify_decision_bundle(bundle, manifest_sha256)
    files, _ = _read_manifest(bundle, manifest_sha256, DECISION_BUNDLE)
    core = json.loads(files["decision.json"])
    _identity(core, request)
    if core.get("primary_candidate_id") is not None or core["archive"]["candidates"]:
        raise EvidenceError("neutral_input_pool_must_not_contain_policy_choices")
    if verified["decision_status"] == "blocked":
        raise EvidenceError("blocked_common_evidence_bundle")
    now = _now()
    times = json.loads(files["times.json"])
    if _time(times["report_generated_at"]) > now:
        raise EvidenceError("common_bundle_completion_in_future")
    reasons = []
    if protocol["qualification"] != "prospective_preregistered":
        reasons.append("research_protocol")
    if verified["qualification"] != "prospective_input_candidate":
        reasons.append("common_input_qualification_limited")
    if not _time(protocol["registered_at"]) <= _time(times["freeze_started_at"]):
        reasons.append("common_bundle_precedes_registration")
    if now >= _time(request["kickoff"]):
        reasons.append("opportunity_opened_after_kickoff")
    record = {"schema_version": OPPORTUNITY, "protocol_id": protocol_id, "request_id": request_id,
              "opened_at": _iso(now), "common_manifest_sha256": manifest_sha256, "pool": _pool(files),
              "qualification": "research_only" if reasons else "prospective_candidate",
              "qualification_reasons": reasons}
    destination = _folder(root, protocol_id, request_id) / "opportunity"
    copied = {"common/" + k: v for k, v in files.items()}
    copied["common/manifest.json"] = _read_regular(bundle, "manifest.json")
    copied["opportunity.json"] = _bytes(record)
    digest = _write_bundle(destination, copied, OPPORTUNITY)
    return {**record, "manifest_sha256": digest, "path": str(destination)}


def _read_opportunity(root, protocol_id, request_id, expected=None):
    folder = _folder(root, protocol_id, request_id) / "opportunity"
    files, digest = _read_manifest(folder, expected, OPPORTUNITY)
    value = json.loads(files["opportunity.json"])
    if value.get("protocol_id") != protocol_id or value.get("request_id") != request_id:
        raise EvidenceError("opportunity_binding_mismatch")
    verify_decision_bundle(folder / "common", value["common_manifest_sha256"])
    common, _ = _read_manifest(folder / "common", value["common_manifest_sha256"], DECISION_BUNDLE)
    if value["pool"] != _pool(common) or _time(value["opened_at"]) > _now():
        raise EvidenceError("opportunity_pool_or_clock_mismatch")
    _identity(json.loads(common["decision.json"]), _request(read_trial(root, protocol_id), request_id))
    return value, digest


def _validate_run(run, protocol, request_id, side, opportunity, opportunity_hash, now):
    policy = protocol["spec"]["policies"][side]
    if (run.get("schema_version") != RUN_SCHEMA or run.get("request_id") != request_id
            or run.get("policy_id") != policy["policy_id"]
            or run.get("policy_definition_sha256") != policy["definition"]["sha256"]
            or run.get("opportunity_manifest_sha256") != opportunity_hash):
        raise EvidenceError("actual_run_binding_mismatch")
    if run.get("generation_mode") != "independent_output" or not _text(run.get("run_id")) or not _text(run.get("producer")):
        raise EvidenceError("independent_actual_run_required")
    if run.get("kind") not in {"decision", "abstention", "failure"}:
        raise EvidenceError("actual_output_kind_required")
    start, end = _time(run.get("started_at")), _time(run.get("completed_at"))
    if not _time(opportunity["opened_at"]) <= start <= end <= now:
        raise EvidenceError("run_must_follow_opportunity_and_not_be_from_future")
    if run.get("future_information_exposure") is not False and run.get("future_information_exposure") is not True and run.get("future_information_exposure") is not None:
        raise EvidenceError("invalid_future_information_exposure")
    if run["kind"] != "decision" and not _text(run.get("reason")):
        raise EvidenceError("explicit_abstention_or_failure_reason_required")
    if run["kind"] == "abstention" and run.get("abstention_type") not in {"purpose_mismatch", "no_qualifying_contract"}:
        raise EvidenceError("unknown_is_not_an_abstention")
    if run["kind"] != "decision" and "output_record" in run:
        raise EvidenceError("nondecision_cannot_hide_a_policy_output")


def submit_run(root, protocol_id, request_id, side, run_ref, *, source_root):
    """Register one actual independent output. No cloning or grade synthesis.

    The raw run includes the exact F2 input object when kind=decision. Its
    run/producer declarations are retained; hashes cannot authenticate cognition.
    First receipt (even a failure) is the primary output; retries remain visible.
    """
    if side not in SIDES:
        raise EvidenceError("left_or_right_required")
    protocol = read_trial(root, protocol_id)
    request = _request(protocol, request_id)
    raw = _external(run_ref, source_root)
    run = json.loads(raw)
    if not _hash(run.get("opportunity_manifest_sha256")):
        raise EvidenceError("external_opportunity_anchor_required")
    opportunity, opp_hash = _read_opportunity(root, protocol_id, request_id, run["opportunity_manifest_sha256"])
    now = _now()
    _validate_run(run, protocol, request_id, side, opportunity, opp_hash, now)
    base = _folder(root, protocol_id, request_id)
    for other in SIDES:
        for receipt in _receipts(root, protocol_id, request_id, other):
            if receipt["run"].get("run_id") == run["run_id"]:
                raise EvidenceError("run_id_already_registered_no_duplicate_or_relabel")
    parent = _no_symlinks(base / side)
    parent.mkdir(parents=True, exist_ok=True)
    # Directory creation is exclusive; concurrent attempts fail rather than race
    # to rewrite the primary submission. Incomplete slots stay visible failures.
    sequence = len(list(parent.iterdir())) + 1
    destination = parent / f"{sequence:06d}"
    destination.mkdir(exist_ok=False)
    _new(destination / "run.json", raw)
    record = {"schema_version": SUBMISSION, "protocol_id": protocol_id, "request_id": request_id,
              "side": side, "sequence": sequence, "registered_at": _iso(now),
              "opportunity_manifest_sha256": opp_hash, "run_sha256": _sha(raw),
              "decision_manifest_sha256": None, "kind": run["kind"], "status": "available",
              "reason": run.get("reason"), "qualification_reasons": []}
    if opportunity["qualification"] != "prospective_candidate":
        record["qualification_reasons"].append("opportunity_is_research_only")
    if run.get("future_information_exposure") is not False:
        record["qualification_reasons"].append("future_information_exposure_not_false")
    if run["kind"] == "decision":
        core = run.get("output_record")
        if not isinstance(core, dict):
            record.update(status="failed", reason="actual_F2_output_record_missing")
        else:
            try:
                _identity(core, request)
                if core.get("policy_id") != protocol["spec"]["policies"][side]["policy_id"]:
                    raise EvidenceError("decision_policy_identity_mismatch")
                if not core.get("primary_candidate_id"):
                    raise EvidenceError("explicit_abstention_kind_required_when_no_selection")
                if core.get("future_information_exposure") is not run.get("future_information_exposure"):
                    raise EvidenceError("run_and_decision_exposure_mismatch")
                built = build_decision_bundle(core, destination / "decision", source_root=source_root)
                record["decision_manifest_sha256"] = built["manifest_sha256"]
                files, _ = _read_manifest(destination / "decision", built["manifest_sha256"], DECISION_BUNDLE)
                common_files, _ = _read_manifest(base / "opportunity" / "common", opportunity["common_manifest_sha256"], DECISION_BUNDLE)
                _policy_inputs(files, common_files, protocol["spec"]["policies"][side])
                if _pool(files) != opportunity["pool"]:
                    raise EvidenceError("output_does_not_use_exact_common_input_quote_model_pool")
                if built["status"] == "blocked":
                    raise EvidenceError("actual_decision_structurally_blocked")
                if built["qualification"] != "prospective_input_candidate":
                    record["qualification_reasons"].append("decision_qualification_limited")
            except (EvidenceError, ValueError, KeyError, TypeError, OSError) as exc:
                record.update(status="failed", reason=str(exc))
    elif run["kind"] == "failure":
        record.update(status="failed")
    finished = _now()
    record["committed_at"] = _iso(finished)
    if finished >= _time(request["kickoff"]):
        record["qualification_reasons"].append("submission_committed_after_kickoff")
    record["qualification"] = "research_only" if record["qualification_reasons"] else "prospective_candidate"
    _new(destination / "receipt.json", _bytes(record))
    # Snapshot hashes include diagnostics of failed builds, never overwrite them.
    contents = {str(p.relative_to(destination)): p.read_bytes() for p in destination.rglob("*") if p.is_file()}
    manifest = {"protocol": SUBMISSION, "files": [{"path": k, "sha256": _sha(v), "bytes": len(v)} for k, v in sorted(contents.items())]}
    digest = _sha(_bytes(manifest))
    _new(destination / "manifest.json", _bytes(manifest))
    return {**record, "manifest_sha256": digest, "path": str(destination)}


def _receipts(root, protocol_id, request_id, side):
    folder = _folder(root, protocol_id, request_id) / side
    if not folder.exists():
        return []
    children = sorted(_no_symlinks(p) for p in folder.iterdir())
    if [p.name for p in children] != [f"{i:06d}" for i in range(1, len(children) + 1)]:
        raise EvidenceError("submission_sequence_gap_or_foreign_file")
    output = []
    for child in children:
        if not (child / "manifest.json").exists():
            output.append({"status": "failed", "reason": "incomplete_submission_slot", "kind": "failure",
                           "qualification": "research_only", "run": {}, "path": str(child), "manifest_sha256": None})
            continue
        files, digest = _read_manifest(child, None, SUBMISSION)
        record, run = json.loads(files["receipt.json"]), json.loads(files["run.json"])
        if (record.get("protocol_id") != protocol_id or record.get("request_id") != request_id
                or record.get("side") != side or record.get("sequence") != int(child.name)
                or record.get("run_sha256") != _sha(files["run.json"])):
            raise EvidenceError("submission_binding_mismatch")
        if not _time(record["registered_at"]) <= _time(record["committed_at"]) <= _now():
            raise EvidenceError("submission_clock_mismatch")
        protocol = read_trial(root, protocol_id)
        opportunity, opp_hash = _read_opportunity(root, protocol_id, request_id, record["opportunity_manifest_sha256"])
        _validate_run(run, protocol, request_id, side, opportunity, opp_hash, _time(record["registered_at"]))
        if record["status"] == "available" and record["kind"] == "decision":
            verify_decision_bundle(child / "decision", record["decision_manifest_sha256"])
            actual, _ = _read_manifest(child / "decision", record["decision_manifest_sha256"], DECISION_BUNDLE)
            common_files, _ = _read_manifest(_folder(root, protocol_id, request_id) / "opportunity" / "common", opportunity["common_manifest_sha256"], DECISION_BUNDLE)
            _policy_inputs(actual, common_files, protocol["spec"]["policies"][side])
            if json.loads(actual["input_record.json"]) != run["output_record"] or _pool(actual) != opportunity["pool"]:
                raise EvidenceError("submission_original_output_or_pool_mismatch")
        output.append({**record, "run": run, "path": str(child), "manifest_sha256": digest})
    return output


def compare_trials(root, protocol_id, scores, *, output_path=None):
    """Read every frozen opportunity and every submission; bind one score revision.

    Scores are {request_id, side, path, manifest_sha256}. Their source decision
    must be the first receipt. No later retry can replace a failed first output.
    """
    protocol = read_trial(root, protocol_id)
    spec = protocol["spec"]
    supplied = {}
    for ref in scores:
        request = _request(protocol, ref.get("request_id"))
        key = (request["request_id"], ref.get("side"))
        if key[1] not in SIDES or key in supplied:
            raise EvidenceError("explicit_single_score_revision_per_side_required")
        if not _hash(ref.get("manifest_sha256")):
            raise EvidenceError("external_score_manifest_anchor_required")
        supplied[key] = read_decision_score(ref["path"], ref["manifest_sha256"])
    rows, metrics = [], {"prospective": defaultdict(list), "research": defaultdict(list)}
    for request in spec["requests"]:
        rid = request["request_id"]
        row = {**request, "group": "unpaired", "prospective_pair": False, "reasons": [], "sides": {}}
        folder = _folder(root, protocol_id, rid)
        opportunity = None
        if (folder / "opportunity").exists():
            opportunity, _ = _read_opportunity(root, protocol_id, rid)
        all_receipts = {side: _receipts(root, protocol_id, rid, side) for side in SIDES}
        actual_run_ids = [receipt["run"].get("run_id") for items in all_receipts.values() for receipt in items
                          if receipt["run"].get("run_id") is not None]
        if len(actual_run_ids) != len(set(actual_run_ids)):
            raise EvidenceError("duplicate_actual_run_ids_not_independent")
        for side in SIDES:
            receipts = all_receipts[side]
            selected = receipts[0] if receipts else None
            out = {"status": "missing", "action": "unknown", "reason": "actual_output_missing", "versions": len(receipts),
                   "receipt_manifest_sha256s": [r["manifest_sha256"] for r in receipts],
                   "qualification": None, "ticket_return": None, "strategy_return": None, "score": None}
            row["sides"][side] = out
            if selected is None:
                if (rid, side) in supplied:
                    raise EvidenceError("score_without_registered_actual_output")
                continue
            out.update(status=selected["status"], reason=selected.get("reason"), qualification=selected["qualification"])
            if selected["status"] == "failed":
                out["action"] = "failed"
                if (rid, side) in supplied:
                    raise EvidenceError("cannot_score_retry_over_first_failure")
                continue
            if selected["kind"] == "abstention":
                out["action"] = "abstain"
                if (rid, side) in supplied:
                    raise EvidenceError("abstention_has_no_selected_ticket_to_score")
                continue
            files, _ = _read_manifest(Path(selected["path"]) / "decision", selected["decision_manifest_sha256"], DECISION_BUNDLE)
            core = json.loads(files["decision.json"])
            candidate = next(c for c in core["archive"]["candidates"] if c["candidate_id"] == core["primary_candidate_id"])
            out["action"] = {"适合": "candidate", "不建议": "abstain", "条件适合": "conditional"}.get(candidate["formal_fit"].get(spec["usage"]), "unknown")
            out["decision_id"] = core["decision_id"]
            score = supplied.get((rid, side))
            if score is None:
                continue
            if score["source"]["decision_manifest_sha256"] != selected["decision_manifest_sha256"]:
                raise EvidenceError("score_not_for_first_registered_decision")
            if score["source"]["protocol_sha256"] != spec["scoring_protocol"]["sha256"]:
                raise EvidenceError("score_uses_different_preregistered_scoring_protocol")
            out["score"] = score
            out["ticket_return"] = score["outcome"].get(RETURN_FIELDS[spec["quote_protocol"]])
            if out["action"] == "candidate":
                out["strategy_return"] = out["ticket_return"]
        a, b = (row["sides"][s] for s in SIDES)
        if a.get("decision_id") is not None and a.get("decision_id") == b.get("decision_id"):
            row["reasons"].append("same_decision_identity_not_independent_outputs")
        if opportunity is None:
            row["reasons"].append("common_opportunity_missing")
        comparable = not row["reasons"] and all(s["status"] == "available" and s["action"] in {"candidate", "abstain"} for s in (a, b))
        if comparable:
            actions = (a["action"], b["action"])
            row["group"] = {("candidate", "candidate"): "common", ("abstain", "candidate"): "restored_coverage",
                            ("candidate", "abstain"): "lost_coverage", ("abstain", "abstain"): "both_abstain"}[actions]
            row["prospective_pair"] = protocol["qualification"] == "prospective_preregistered" and all(s["qualification"] == "prospective_candidate" for s in (a, b))
        else:
            row["reasons"].append("missing_failed_unknown_or_unresolved_condition")
        valid_scores = [s["score"] for s in (a, b) if s["score"] is not None]
        result_ids = {s["source"]["result_sha256"] for s in valid_scores}
        return_reasons = []
        if not comparable:
            return_reasons.append("process_pair_unavailable")
        for side in (a, b):
            if side["action"] == "candidate" and side["score"] is None:
                return_reasons.append("selected_ticket_score_missing")
            elif side["action"] == "candidate" and side["ticket_return"] is None:
                return_reasons.append("selected_ticket_return_unavailable")
        known_zero = comparable and spec["abstention_zero_exposure"] and spec["quote_protocol"] != "QUOTE_ACTUAL" and a["action"] == b["action"] == "abstain"
        if not known_zero and (len(result_ids) != 1 or None in result_ids):
            return_reasons.append("same_verified_result_revision_required")
        if not known_zero and any(s.get("result_source_status") == "unverified_normalized_declaration" for s in valid_scores):
            return_reasons.append("result_originals_or_review_missing")
        if spec["quote_protocol"] == "QUOTE_OPEN" and not known_zero:
            for score in valid_scores:
                times = score.get("open_quote_times") or {}
                try:
                    if not _time(times["asserted_at"]) <= _time(times["observed_at"]) <= _time(request["analysis_cutoff"]):
                        return_reasons.append("open_quote_outside_common_cutoff")
                except (KeyError, TypeError, ValueError):
                    return_reasons.append("open_quote_times_missing")
        if spec["quote_protocol"] == "QUOTE_ACTUAL":
            # Real executions are not randomized interchangeable opportunities.
            # Preserve each actual outcome, but do not claim a controlled delta.
            return_reasons.append("actual_execution_is_observational_not_controlled_policy_comparison")
        if not return_reasons:
            for side, other in ((a, b), (b, a)):
                if side["action"] == "abstain" and spec["abstention_zero_exposure"] and (known_zero or side["ticket_return"] is not None or other["ticket_return"] is not None):
                    side["strategy_return"] = 0.0
        row["return_comparison_reasons"] = sorted(set(return_reasons))
        metric_set = metrics["prospective" if row["prospective_pair"] and (known_zero or all(s.get("qualification") == "prospective_decision_candidate" for s in valid_scores)) else "research"]
        if not return_reasons:
            metric_set["strategy_return"].append({**request, "left": a["strategy_return"], "right": b["strategy_return"]})
            if a["action"] == b["action"] == "candidate":
                metric_set["ticket_return"].append({**request, "left": a["ticket_return"], "right": b["ticket_return"]})
        # A different ticket changes the five-state outcome space: those scores
        # are not a fair accuracy comparison. Only full match 1X2 is comparable.
        if comparable and len(valid_scores) == 2 and len(result_ids) == 1 and None not in result_ids:
            for field in ("brier_score", "log_loss"):
                values = [(s.get("probability", {}).get("metrics", {}).get("one_x_two") or {}).get(field) for s in valid_scores]
                metric_set["one_x_two_" + field].append({**request, "left": values[0], "right": values[1]})
        for side in SIDES:
            score = row["sides"][side].pop("score")
            row["sides"][side]["score_manifest_sha256"] = score.get("manifest_sha256") if score else None
        rows.append(row)
    report = {"schema_version": SCHEMA, "protocol_id": protocol_id, "generated_at": _iso(_now()),
              "comparison": spec["comparison"], "quote_protocol": spec["quote_protocol"], "usage": spec["usage"],
              "request_count": len(rows), "unique_matches": len({(r["competition"], str(r["match_id"])) for r in rows}),
              "coverage": dict(Counter(r["group"] for r in rows)), "prospective_process_pairs": sum(r["prospective_pair"] for r in rows),
              "rows": rows, "metrics": {group: {name: cluster_interval(values[name]) for name in
                         ("strategy_return", "ticket_return", "one_x_two_brier_score", "one_x_two_log_loss")}
                         for group, values in metrics.items()},
              "quality_improvement_verified": False, "actual_bets_inferred": False, "whole_parlay_evaluated": False,
              "limitations": ["Run provenance and no-future-exposure are attributed declarations, not authentication of independent reasoning.",
                              "Hashes and local append-only files do not prove no administrator deleted a trailing submission; anchor manifests externally.",
                              "An absent output, unresolved condition, missing score/cost or failure is never zero.",
                              "Opportunity-average return and ROI per placed bet have different denominators; no ROI or whole-parlay result is inferred.",
                              "Per-match bootstrap is descriptive; no automatic promotion, significance or profitability claim."]}
    if output_path is not None:
        _new(output_path, _bytes(report))
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("register", "open", "submit", "compare"):
        command = sub.add_parser(name)
        command.add_argument("--root", type=Path, required=True)
        if name != "register":
            command.add_argument("--protocol-id", required=True)
        if name in {"open", "submit"}:
            command.add_argument("--request-id", required=True)
        if name in {"register", "submit"}:
            command.add_argument("--source-root", type=Path, required=True)
        if name == "register":
            command.add_argument("--spec", type=Path, required=True)
        elif name == "open":
            command.add_argument("--bundle", type=Path, required=True)
            command.add_argument("--manifest-sha256", required=True)
        elif name == "submit":
            command.add_argument("--side", choices=SIDES, required=True)
            command.add_argument("--run", required=True)
            command.add_argument("--run-sha256", required=True)
        else:
            command.add_argument("--scores", type=Path, required=True)
            command.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "register":
            answer = register_trial(json.loads(args.spec.read_text()), args.root, source_root=args.source_root)
        elif args.command == "open":
            answer = open_opportunity(args.root, args.protocol_id, args.request_id, args.bundle, args.manifest_sha256)
        elif args.command == "submit":
            answer = submit_run(args.root, args.protocol_id, args.request_id, args.side,
                                {"path": args.run, "sha256": args.run_sha256}, source_root=args.source_root)
        else:
            report = compare_trials(args.root, args.protocol_id, json.loads(args.scores.read_text()), output_path=args.output)
            answer = {"output": str(args.output), "request_count": report["request_count"], "coverage": report["coverage"]}
        print(json.dumps(answer, ensure_ascii=False))
        return 0
    except (EvidenceError, ValueError, TypeError, KeyError, OSError) as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
