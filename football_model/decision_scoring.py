"""Append-only scoring of the report's adopted decision and its actual forecast.

The old prospective first-forecast/300-second quote protocol is not changed.
Evidence checks establish byte/identity/time consistency, not source honesty.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
from pathlib import Path

from .decision_record import (EVENT_PROTOCOL, PROTOCOL, _read_manifest, _write_bundle,
                              verify_decision_bundle)
from .evidence import EvidenceError, _no_symlinks, _read_regular, _relative, _time
from .prospective import AXES, _categorical_metrics
from .settlement import (OUTCOMES, STATES, contract as normalize_contract,
                         matrix_array, probabilities, settlement, unit_return)

SCHEMA = "s2-decision-score-1.0"
SCORE_BUNDLE = "s2-decision-score-bundle-1.0"
PROTOCOL_SCHEMA = "s2-decision-scoring-protocol-1.0"
REGULATION = "regulation_time_90_plus_stoppage"
API_AH_SCORING_POLICY = "api-ah-scoring-quarantine-v1"


def current_score_admission_reasons(score):
    """Historical replay can be viewed without admitting its numbers anew."""
    if (score.get("historical_api_ah_mapping_unverified") is True
            or score.get("new_api_ah_admission_allowed") is False
            or "api_asian_handicap_mapping_not_certified" in score.get("unavailable_reasons", [])):
        return ["api_asian_handicap_mapping_not_certified"]
    return []


def _now():
    return datetime.now(timezone.utc)


def _iso(value):
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _bytes(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2,
                       allow_nan=False) + "\n").encode()


def _sha(value):
    return hashlib.sha256(value).hexdigest()


def _hash(value):
    return isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _cost(value):
    if value is not None and (not _number(value) or not 0 <= value < 1):
        raise EvidenceError("invalid_explicit_fixed_cost")
    return value


def _candidate_uses_unverified_api_ah(core, candidate):
    """Find the adopted offer's original provider row, including sealed old cores.

    Historical adapter reproduction does not certify the provider convention.
    The policy is shared with the new row adapter, while immutable source review
    remains responsible for stripped or falsely relabeled provenance.
    """
    if candidate is None:
        return False
    from .decision_quotes import _unverified_api_asian_handicap
    for row in core.get("adapted_quote_rows", []):
        original = row.get("original_row", {})
        if not _unverified_api_asian_handicap(original):
            continue
        matches = (candidate.get("quote_row_id") in {row.get("quote_row_id"), original.get("row_id")}
                   or candidate.get("quote_offer_id") in {o.get("quote_offer_id") for o in row.get("offers", [])}
                   or candidate.get("contract_id") in {c.get("contract_id") for c in row.get("contracts", [])})
        if matches:
            return True
    return False


def register_scoring_protocol(spec, root):
    """Freeze a protocol at the real current time; no historical clock argument."""
    if not isinstance(spec, dict):
        raise EvidenceError("scoring_protocol_object_required")
    spec = deepcopy(spec)
    required = {"name", "quote_protocol", "usage", "scope", "evaluation_mode",
                "decision_selection", "condition_policy", "cost_per_unit", "unit_stake"}
    if set(spec) != required:
        raise EvidenceError("explicit_scoring_protocol_fields_required")
    if not isinstance(spec["name"], str) or not spec["name"].strip():
        raise EvidenceError("protocol_name_required")
    if spec["quote_protocol"] not in {"QUOTE_REF", "QUOTE_OPEN", "QUOTE_ACTUAL"}:
        raise EvidenceError("unsupported_quote_protocol")
    if spec["usage"] not in {"single", "parlay_leg"}:
        raise EvidenceError("whole_parlay_requires_separate_protocol_and_all_legs")
    if spec["evaluation_mode"] not in {"research", "prospective"}:
        raise EvidenceError("evaluation_mode_required")
    if spec["decision_selection"] != "explicit_adopted_version_no_retrospective_switch":
        raise EvidenceError("adopted_version_selection_required")
    if spec["condition_policy"] != "satisfied_or_no_conditions_only":
        raise EvidenceError("explicit_condition_policy_required")
    if isinstance(spec["unit_stake"], bool) or spec["unit_stake"] != 1:
        raise EvidenceError("only_one_unit_reference_stake_supported")
    _cost(spec["cost_per_unit"])
    scope = spec["scope"]
    if not isinstance(scope, dict) or set(scope) != {"competitions", "match_ids"}:
        raise EvidenceError("explicit_protocol_scope_required")
    for key in ("competitions", "match_ids"):
        if scope[key] != "all" and (not isinstance(scope[key], list) or not scope[key]
                                   or any(not isinstance(v, str) or not v for v in scope[key])
                                   or len(set(scope[key])) != len(scope[key])):
            raise EvidenceError("invalid_protocol_scope")
    value = {"schema_version": PROTOCOL_SCHEMA, "registered_at": _iso(_now()), "spec": spec,
             "forecast_selection": "only_frozen_forecast_id_adopted_by_decision",
             "target": REGULATION, "result_revisions": "append_only_keep_old_scores",
             "cost_definition": "explicit_fixed_cost_per_unit_in_every_state",
             "source_authenticity": "byte_consistency_is_not_independent_source_authentication"}
    blob = _bytes(value)
    identity = _sha(blob)
    folder = _no_symlinks(root) / "protocols"
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / (identity + ".json")).open("xb") as stream:
        stream.write(blob)
    return {"protocol_id": identity, "path": str(folder / (identity + ".json")),
            "registered_at": value["registered_at"]}


def _read_protocol(root, identity):
    if not _hash(identity):
        raise EvidenceError("protocol_content_hash_required")
    blob = _read_regular(_no_symlinks(root), "protocols/" + identity + ".json")
    if _sha(blob) != identity:
        raise EvidenceError("protocol_hash_mismatch")
    value = json.loads(blob)
    if value.get("schema_version") != PROTOCOL_SCHEMA or _time(value["registered_at"]) > _now():
        raise EvidenceError("invalid_protocol_or_registration_time")
    return value, blob


def _external(descriptor, root):
    if not isinstance(descriptor, dict) or not _hash(descriptor.get("sha256")):
        raise EvidenceError("external_evidence_descriptor_required")
    blob = _read_regular(_no_symlinks(root), str(_relative(descriptor.get("path"))))
    if _sha(blob) != descriptor["sha256"]:
        raise EvidenceError("external_evidence_hash_mismatch")
    value = json.loads(blob)
    if not isinstance(value, dict):
        raise EvidenceError("external_evidence_object_required")
    return value, blob


def _identity(core):
    return {"competition": core.get("competition"), "match_id": core["match_id"],
            "home_id": core.get("home_id"), "away_id": core.get("away_id"),
            "kickoff": core["kickoff_at"]}


def _same_identity(value, identity):
    if str(value.get("match_id")) != str(identity["match_id"]):
        raise EvidenceError("scoring_match_identity_mismatch")
    if _time(value.get("kickoff")) != _time(identity["kickoff"]):
        raise EvidenceError("scoring_kickoff_identity_mismatch")
    for key in ("competition", "home_id", "away_id"):
        if identity.get(key) is not None and str(value.get(key)) != str(identity[key]):
            raise EvidenceError("scoring_" + key + "_identity_mismatch")


def _contract(value):
    if value.get("settlement_basis") != "REGULATION":
        raise EvidenceError("explicit_regulation_contract_required")
    market = {"1x2": "one_x_two", "ah": "asian_handicap", "ou": "total_goals"}.get(value.get("market"))
    side = value.get("selection")
    if market == "one_x_two":
        side = {"home": "home_win", "draw": "draw", "away": "away_win"}.get(side)
    return normalize_contract({"market": market, "side": side, "line": value.get("line"),
                               "settlement_basis": REGULATION})


def _forecast(core, entries, contents, identity):
    """Resolve only the chosen ID in its declared frozen evidence, not a latest file."""
    selected = core.get("forecast_id")
    if selected is None:
        return None, [], "adopted_forecast_unavailable"
    if any(identity.get(key) is None for key in ("competition", "home_id", "away_id")):
        return None, [], "decision_complete_team_identity_missing"
    records = [r for r in core["archive"]["model_records"] if r.get("forecast_id") == selected]
    refs = {ref for row in records for ref in row.get("forecast_evidence_ids", [])}
    matches = []
    for entry in entries:
        if not ({entry.get("evidence_id"), entry.get("source_id")} & refs):
            continue
        blob = contents.get(entry["path"])
        if blob is None:
            continue
        try:
            parsed = json.loads(blob)
        except (ValueError, UnicodeDecodeError):
            continue
        native = isinstance(parsed, dict) and isinstance(parsed.get("forecast"), dict) and _sha(blob) == selected

        def walk(value):
            if isinstance(value, dict):
                if value.get("forecast_id") == selected and isinstance(value.get("prediction"), dict):
                    matches.append((value, entry))
                for child in value.values():
                    walk(child)
            elif isinstance(value, list):
                for child in value:
                    walk(child)
        if native:
            row = deepcopy(parsed["forecast"])
            if parsed.get("research_replay"):
                row["research_replay"] = True
            for field in identity:
                if field in parsed:
                    if field in row and str(row[field]) != str(parsed[field]):
                        raise EvidenceError("native_forecast_identity_conflict")
                    row[field] = parsed[field]
            matches.append((row, entry))
        else:
            walk(parsed)
    if not matches:
        return None, [], "adopted_forecast_matrix_not_available"
    unique = {_sha(_bytes(row)) for row, _ in matches}
    if len(unique) != 1:
        raise EvidenceError("adopted_forecast_id_has_conflicting_payloads")
    row = matches[0][0]
    _same_identity(row, identity)
    if str(identity["home_id"]) == str(identity["away_id"]):
        raise EvidenceError("home_away_identity_coincides")
    research_reason = "research_replay_forecast" if row.get("research_replay") else None
    if row.get("forecast_generated_at") is not None and _time(row["forecast_generated_at"]) >= _time(core["kickoff_at"]):
        research_reason = "adopted_forecast_generated_after_kickoff"
    prediction = row["prediction"]
    hashes = sorted({entry["sha256"] for _, entry in matches})
    if (not prediction.get("model_version") or not _hash(prediction.get("training_data_sha256"))
            or not row.get("model_id") or row.get("time_semantics") != "observed_wall_clock_v1"):
        return None, hashes, research_reason or "adopted_forecast_model_lineage_missing"
    if prediction.get("score_matrix_axes") != AXES:
        raise EvidenceError("unknown_adopted_score_matrix_axes")
    for key in ("home_id", "away_id", "kickoff"):
        if key in row and prediction.get(key) != row[key]:
            raise EvidenceError("adopted_prediction_identity_mismatch")
    if prediction.get("training_cutoff") is not None and _time(prediction["training_cutoff"]) != _time(row.get("training_cutoff")):
        raise EvidenceError("adopted_prediction_training_cutoff_mismatch")
    for field in ("forecast_generated_at", "model_fitted_at", "training_cutoff", "data_observed_at", "run_started_at"):
        if row.get(field) is None:
            return None, sorted({entry["sha256"] for _, entry in matches}), research_reason or "adopted_forecast_time_evidence_missing"
    fitted, generated, training = (_time(row[k]) for k in ("model_fitted_at", "forecast_generated_at", "training_cutoff"))
    # F2 permits computations from frozen inputs after the external-information
    # cutoff; the frozen artifact and dependency clocks bound that computation.
    if not training <= fitted <= generated or not _time(row["run_started_at"]) <= _time(row["data_observed_at"]) <= generated:
        raise EvidenceError("adopted_forecast_time_order_invalid")
    if generated >= _time(core["kickoff_at"]):
        research_reason = "adopted_forecast_generated_after_kickoff"
    for _, entry in matches:
        if _time(entry["evidence_time"]) < generated:
            raise EvidenceError("adopted_forecast_observed_before_generated")
    matrix = matrix_array(prediction.get("score_matrix"))
    import numpy as np
    h, a = np.indices(matrix.shape)
    expected = [float(matrix[mask].sum()) for mask in (h > a, h == a, h < a)]
    declared = prediction.get("probabilities", {})
    for label, probability in zip(OUTCOMES, expected):
        if not _number(declared.get(label)) or not math.isclose(declared[label], probability, abs_tol=1e-8):
            raise EvidenceError("adopted_1x2_matrix_disagreement")
    return matrix, hashes, research_reason


def _conditions(core, candidate, usage):
    ids = set(candidate.get("condition_ids", []))
    ids.update(candidate.get("formal_condition_ids", {}).get(usage, []))
    conditions = {row["condition_id"]: row for row in core["archive"]["conditions"]}
    rows = [deepcopy(conditions[key]) for key in sorted(ids)]
    for row in rows:
        row["state"] = {"满足": "satisfied", "不满足": "not_satisfied", "未知": "unknown"}.get(row.get("state"), row.get("state"))
    critical = [row for row in rows if row.get("critical") is True]
    state = "not_satisfied" if any(r.get("state") == "not_satisfied" for r in critical) else (
        "unknown" if any(r.get("state") != "satisfied" for r in critical) else "satisfied")
    return state, rows


def _events(descriptors, base_digest, core, contents, now):
    result, sources = [], {}
    for descriptor in descriptors:
        if not isinstance(descriptor, dict) or not _hash(descriptor.get("manifest_sha256")):
            raise EvidenceError("event_manifest_anchor_required")
        files, digest = _read_manifest(descriptor.get("path"), descriptor["manifest_sha256"], EVENT_PROTOCOL)
        event = json.loads(files["event.json"])
        if event.get("schema_version") != EVENT_PROTOCOL or event.get("base_manifest_sha256") != base_digest:
            raise EvidenceError("event_parent_manifest_mismatch")
        for key in ("match_id", "decision_id", "version_id"):
            if event.get(key) != core.get(key):
                raise EvidenceError("event_decision_binding_mismatch")
        recorded = _time(event.get("recorded_at"))
        generated = _time(json.loads(contents["times.json"])["report_generated_at"])
        occurred = _time(event["occurred_at"]) if event.get("occurred_at") else None
        if recorded > now or recorded < generated or occurred and not generated <= occurred <= recorded:
            raise EvidenceError("event_time_outside_real_bounds")
        if not event.get("evidence"):
            raise EvidenceError("event_evidence_required")
        if event.get("event_type") not in {"publication", "user_received", "execution", "condition_changed"}:
            raise EvidenceError("unsupported_event_type")
        if event["event_type"] == "execution":
            if event.get("status") not in {"accepted", "rejected", "cancelled", "unknown"}:
                raise EvidenceError("execution_status_required")
            candidate = next((c for c in core["archive"]["candidates"] if c["candidate_id"] == event.get("candidate_id")), None)
            if candidate is None or not candidate.get("quote_offer_id") or event.get("quote_offer_id") != candidate["quote_offer_id"]:
                raise EvidenceError("execution_offer_binding_required")
            user_confirmed = (event.get("execution_basis") == "user_confirmation"
                              and any(e.get("evidence_type") == "user_confirmation" for e in event["evidence"]))
            if event["status"] == "accepted" and not user_confirmed and (not _number(event.get("accepted_decimal_odds"))
                     or event["accepted_decimal_odds"] <= 1 or not event.get("transaction_reference")):
                raise EvidenceError("accepted_execution_receipt_required")
        for item in event["evidence"]:
            blob = files.get(item.get("path"))
            observed = _time(item.get("observed_at"))
            if blob is None or item.get("sha256") != _sha(blob) or item.get("bytes") != len(blob):
                raise EvidenceError("event_evidence_binding_mismatch")
            if not generated <= observed <= recorded or occurred and observed < occurred:
                raise EvidenceError("event_evidence_time_invalid")
        result.append({**event, "manifest_sha256": digest})
        sources[digest] = files
    if len({e["event_id"] for e in result}) != len(result):
        raise EvidenceError("duplicate_event_id")
    return result, sources


def _review(review, digest, now):
    if (not isinstance(review, dict) or review.get("status") != "verified"
            or review.get("source_sha256") != digest or not isinstance(review.get("reviewer"), str)
            or not review["reviewer"].strip()):
        return False
    if _time(review.get("reviewed_at")) > now:
        raise EvidenceError("evidence_review_in_future")
    return True


def _result_parents(descriptor, value, root, now, evidence_copies):
    """Verify normalized native ancestry or reviewed independent result originals."""
    parents = descriptor.get("source_evidence", [])
    if not isinstance(parents, list):
        raise EvidenceError("result_source_evidence_array_required")
    native = False
    reviewed = bool(parents) and _review(descriptor.get("semantic_review"), descriptor["sha256"], now)
    hashes, seen = [], set()
    for parent in parents:
        relative = str(_relative(parent.get("path")))
        if relative in seen or relative == "result.json":
            raise EvidenceError("duplicate_or_reserved_result_source_path")
        seen.add(relative)
        blob = _read_regular(_no_symlinks(root), relative)
        digest = _sha(blob)
        if not _hash(parent.get("sha256")) or digest != parent["sha256"] or digest == descriptor["sha256"]:
            raise EvidenceError("result_parent_hash_mismatch")
        hashes.append(digest)
        evidence_copies["result_sources/" + relative] = blob
        if digest == value.get("source_id"):
            source = json.loads(blob)
            data = source.get("content", {})
            if source.get("kind") == "normalized_regulation_data":
                if data.get("time_semantics") != "observed_wall_clock_v1":
                    raise EvidenceError("native_result_parent_scope_mismatch")
                if _time(data.get("observed_at")) != _time(value["observed_at"]):
                    raise EvidenceError("native_result_parent_observation_mismatch")
                rows = [r for r in data.get("matches", []) if str(r.get("match_id")) == str(value["match_id"])]
            elif source.get("kind") == "independent_fixture_result_response":
                from .data import normalize_fixtures
                if source.get("client_response_sha256") != _sha(_bytes(data)):
                    raise EvidenceError("native_fixture_response_hash_mismatch")
                mid = int(value["match_id"])
                if source.get("request") != {"fixture_id": mid} or data.get("endpoint", "/fixtures") != "/fixtures" or data.get("params", {"id": mid}) != {"id": mid}:
                    raise EvidenceError("native_fixture_response_request_mismatch")
                if _time(data.get("fetched_at")) != _time(value["observed_at"]):
                    raise EvidenceError("native_result_parent_observation_mismatch")
                paging = data.get("paging", {})
                if data.get("errors") or not isinstance(paging, dict) or any(type(paging.get(k, 1)) is not int or paging.get(k, 1) != 1 for k in ("current", "total")):
                    raise EvidenceError("native_fixture_response_error_or_partial")
                items = data.get("response")
                if not isinstance(items, list) or len(items) != 1 or ("results" in data and (type(data["results"]) is not int or data["results"] != 1)):
                    raise EvidenceError("native_fixture_response_count_invalid")
                rows = normalize_fixtures(items, _time(value["observed_at"]))
            else:
                raise EvidenceError("native_result_parent_scope_mismatch")
            if len(rows) != 1:
                raise EvidenceError("native_result_parent_match_not_unique")
            row = rows[0]
            _same_identity({"competition": value["competition"], **row}, value)
            if any(row.get(k) != value[k] for k in ("home_goals", "away_goals")):
                raise EvidenceError("native_result_parent_score_mismatch")
            native = True
        else:
            parent_observed = _time(parent.get("observed_at"))
            if parent_observed > _time(value["observed_at"]) or parent_observed > now:
                raise EvidenceError("result_parent_observation_invalid")
            reviewed = reviewed and _review(parent.get("semantic_review"), digest, now)
    status = "native_normalized_parent_verified" if native else (
        "independent_originals_reviewed" if reviewed else "unverified_normalized_declaration")
    return status, sorted(hashes)


def _score_decision(bundle_dir, *, expected_manifest_sha256, protocol_root, protocol_id,
                   result=None, result_source_root=None, events=(), quote_evidence=None,
                   quote_source_root=None, output_dir=None, _legacy_api_ah_score_replay=False):
    """Score an immutable F2 bundle; result and execution layers remain independent.

    result/quote_evidence are {path, sha256} descriptors. QUOTE_OPEN additionally
    needs a semantic_review bound to that hash. Events are {path, manifest_sha256,
    semantic_review?}. Unknown prices/costs/conditions remain unavailable.
    The private legacy switch is used only to reproduce an already sealed score
    after its entire manifest has been checked. Fresh scoring never sets it.
    """
    if not isinstance(_legacy_api_ah_score_replay, bool):
        raise EvidenceError("legacy_score_replay_flag_must_be_boolean")
    if not _hash(expected_manifest_sha256):
        raise EvidenceError("external_decision_manifest_anchor_required")
    now = _now()
    verified = verify_decision_bundle(bundle_dir, expected_manifest_sha256)
    contents, base_digest = _read_manifest(bundle_dir, expected_manifest_sha256, PROTOCOL)
    core, entries = (json.loads(contents[k]) for k in ("decision.json", "evidence.json"))
    protocol, protocol_blob = _read_protocol(protocol_root, protocol_id)
    spec = protocol["spec"]
    identity = _identity(core)
    for field, key in (("competition", "competitions"), ("match_id", "match_ids")):
        if spec["scope"][key] != "all" and str(identity[field]) not in spec["scope"][key]:
            raise EvidenceError("decision_outside_preregistered_scope")
    qualification_reasons = []
    if verified["qualification"] != "prospective_input_candidate":
        qualification_reasons.append("decision_is_research_only")
    if spec["evaluation_mode"] == "research":
        qualification_reasons.append("research_protocol")
    if _time(protocol["registered_at"]) > _time(core["analysis_cutoff"]):
        qualification_reasons.append("protocol_registered_after_decision_cutoff")
    if verified["decision_status"] == "blocked":
        qualification_reasons.append("decision_structurally_blocked")
    events_read, event_sources = _events(events, base_digest, core, contents, now)
    candidate = next((c for c in core["archive"]["candidates"] if c["candidate_id"] == core.get("primary_candidate_id")), None)
    api_ah_quarantine = not _legacy_api_ah_score_replay and _candidate_uses_unverified_api_ah(core, candidate)
    record = {"schema_version": SCHEMA, "scored_at": _iso(now), "quote_protocol": spec["quote_protocol"],
              "decision_times": json.loads(contents["times.json"]),
              "usage": spec["usage"], "binding": {**identity, **{k: core.get(k) for k in
                  ("decision_id", "version_id", "policy_id", "forecast_id", "analysis_cutoff")}},
              "source": {"decision_manifest_sha256": base_digest, "protocol_sha256": protocol_id,
                         "result_sha256": None, "event_manifest_sha256s": sorted(event_sources), "quote_evidence_sha256": None},
              "source_pool": {"input_pool_sha256": _sha(_bytes(sorted((e["sha256"], e.get("observed_at")) for e in entries
                                  if e.get("kind") == "external_input" and e.get("role") not in {"rules", "code"}))),
                              "quote_pool_sha256": _sha(_bytes(core["adapted_quote_rows"])), "model_sha256s": []},
              "formal_fit": deepcopy(candidate.get("formal_fit")) if candidate else None,
              "shadow_grading": deepcopy(core.get("shadow_grading")), "conditions_status": "unknown",
              "contract": None, "price_decimal": None, "cost_per_unit": None,
              "execution_status": "unknown", "execution_event_id": None,
              "probability": {"status": "not_available", "reason": None, "five_state_probabilities": None,
                              "profit_probability": None, "no_loss_probability": None, "net_ev": None,
                              "metrics": {"one_x_two": None, "five_state": None}},
              "outcome": {"status": "pending", "settlement_state": None, "paper_reference_return": None,
                          "open_simulated_return": None, "actual_single_return": None, "actual_stake_return": None,
                          "whole_parlay_return": None}, "unavailable_reasons": [],
              "source_authenticity": "semantic_reviews_are_attributed_declarations_not_provider_authentication",
              "original_decision_unmodified": True, "predictive_quality_verified": False,
              "whole_parlay_assessed": False}
    if not _legacy_api_ah_score_replay:
        record["api_ah_scoring_policy_version"] = API_AH_SCORING_POLICY
    evidence_copies = {"protocol.json": protocol_blob}
    for key in ("candidate_id", "contract_id", "quote_row_id", "quote_offer_id"):
        record["binding"][key] = candidate.get(key) if candidate else None
    state = None
    if result is not None:
        result_value, result_blob = _external(result, result_source_root)
        _same_identity(result_value, identity)
        if result_value.get("score_scope") not in {REGULATION, "normalized_API_Football_score_fulltime_90_plus_stoppage"}:
            raise EvidenceError("result_must_be_independent_regulation_score")
        observed = _time(result_value.get("observed_at"))
        captured = _time(result_value.get("captured_at", result_value.get("observed_at")))
        if not _time(identity["kickoff"]) + timedelta(hours=4) <= observed <= captured <= now:
            raise EvidenceError("result_observation_before_maturity_or_in_future")
        for key in ("home_goals", "away_goals"):
            value = result_value.get(key)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise EvidenceError("invalid_regulation_score")
        record["source"]["result_sha256"] = _sha(result_blob)
        record["result"] = deepcopy(result_value)
        evidence_copies["result.json"] = result_blob
        result_source_status, result_source_hashes = _result_parents(result, result_value, result_source_root, now, evidence_copies)
        record["result_source_status"] = result_source_status
        record["result_source_sha256s"] = result_source_hashes
        record["result_evidence_descriptor"] = deepcopy(result)
        if result_source_status == "unverified_normalized_declaration":
            qualification_reasons.append("result_originals_or_review_missing")
    else:
        result_value = None
        record["result"] = None
    if candidate is None:
        record["outcome"]["status"] = "abstained"
        record["unavailable_reasons"].append(core.get("no_primary_reason") or "no_primary_candidate")
    elif verified["decision_status"] == "blocked":
        record["outcome"]["status"] = "blocked"
        record["unavailable_reasons"].append("decision_structurally_blocked")
    elif api_ah_quarantine:
        record["outcome"]["status"] = "not_available"
        record["unavailable_reasons"].append("api_asian_handicap_mapping_not_certified")
        record["probability"]["reason"] = "api_asian_handicap_mapping_not_certified"
        record["return_eligible"] = False
        qualification_reasons.append("api_asian_handicap_mapping_not_certified")
    else:
        raw_contract = next((r for r in core["archive"]["contracts"] if r["contract_id"] == candidate.get("contract_id")), None)
        if raw_contract is None:
            record["outcome"]["status"] = "not_available"
            record["unavailable_reasons"].append("contract_not_available")
        else:
            selected_contract = _contract(raw_contract)
            if raw_contract["selection"] != candidate.get("selection"):
                raise EvidenceError("candidate_contract_selection_mismatch")
            record["contract"] = selected_contract
            conditions, condition_rows = _conditions(core, candidate, spec["usage"])
            record["conditions_status"] = conditions
            record["conditions"] = condition_rows
            if any(e["event_type"] == "condition_changed" for e in events_read):
                record["unavailable_reasons"].append("condition_event_requires_new_decision_no_automatic_promotion")
            offer = next((r for r in core["archive"]["quotes"] if r["quote_offer_id"] == candidate.get("quote_offer_id")), None)
            price = offer.get("decimal_odds") if offer else None
            cost = _cost(spec["cost_per_unit"])
            # Contract settlement is policy-neutral. Formal/shadow action is
            # applied by the separately preregistered policy evaluator.
            eligible = conditions == "satisfied"
            if not eligible:
                record["unavailable_reasons"].append("conditions_unresolved")
            if spec["quote_protocol"] == "QUOTE_OPEN":
                eligible = False
                if quote_evidence is None:
                    record["unavailable_reasons"].append("independent_open_evidence_missing")
                else:
                    quote, blob = _external(quote_evidence, quote_source_root)
                    # A normalized assertion alone is not independent opening
                    # evidence. Preserve the underlying screenshot/response and
                    # an attributed review of its mapping to this ticket.
                    raw_sources = quote.get("source_evidence")
                    raw_reviewed = bool(isinstance(raw_sources, list) and raw_sources)
                    raw_hashes, seen_paths = [], set()
                    for raw in raw_sources if isinstance(raw_sources, list) else []:
                        relative = str(_relative(raw.get("path")))
                        if relative in seen_paths or relative == "open_quote.json":
                            raise EvidenceError("duplicate_or_reserved_open_source_path")
                        seen_paths.add(relative)
                        raw_blob = _read_regular(_no_symlinks(quote_source_root), relative)
                        if not _hash(raw.get("sha256")) or _sha(raw_blob) != raw["sha256"] or _sha(raw_blob) == _sha(blob):
                            raise EvidenceError("open_source_original_hash_mismatch")
                        raw_observed = _time(raw.get("observed_at"))
                        if raw_observed > now:
                            raise EvidenceError("open_source_observed_in_future")
                        raw_reviewed = raw_reviewed and _review(raw.get("semantic_review"), _sha(raw_blob), now)
                        if _time(quote.get("observed_at")) < raw_observed:
                            raise EvidenceError("open_assertion_precedes_source_observation")
                        raw_hashes.append(_sha(raw_blob))
                        evidence_copies["open_sources/" + relative] = raw_blob
                    _same_identity(quote, identity)
                    for key in ("decision_id", "version_id", "candidate_id", "contract_id", "quote_offer_id"):
                        if quote.get(key) != record["binding"].get(key):
                            raise EvidenceError("open_evidence_ticket_binding_mismatch")
                    if normalize_contract(quote.get("contract")) != selected_contract or quote.get("decimal_odds") != price:
                        raise EvidenceError("open_evidence_price_or_contract_mismatch")
                    observed, asserted = _time(quote.get("observed_at")), _time(quote.get("asserted_at"))
                    if not asserted <= observed <= now or asserted >= _time(identity["kickoff"]):
                        raise EvidenceError("open_evidence_time_invalid")
                    if observed >= _time(identity["kickoff"]):
                        qualification_reasons.append("open_evidence_acquired_after_kickoff")
                    if _sha(blob) in {e["sha256"] for e in entries if e.get("source_id") == (offer or {}).get("source_id")}:
                        raise EvidenceError("reference_quote_is_not_independent_open_evidence")
                    eligible = (conditions == "satisfied"
                                and quote.get("status") == "open" and quote.get("conditions_status") == "satisfied"
                                and raw_reviewed and _review(quote_evidence.get("semantic_review"), _sha(blob), now))
                    if not eligible:
                        record["unavailable_reasons"].append("open_evidence_or_semantic_review_not_eligible")
                    record["source"]["quote_evidence_sha256"] = _sha(blob)
                    evidence_copies["open_quote.json"] = blob
                    record["open_quote_review"] = deepcopy(quote_evidence.get("semantic_review"))
                    record["open_quote_times"] = {"asserted_at": quote["asserted_at"], "observed_at": quote["observed_at"]}
                    record["open_source_sha256s"] = sorted(raw_hashes)
            elif spec["quote_protocol"] == "QUOTE_ACTUAL":
                eligible = False
                execution = [e for e in events_read if e.get("event_type") == "execution"]
                if len(execution) > 1:
                    raise EvidenceError("select_one_execution_event_explicitly_no_retrospective_choice")
                if not execution:
                    record["unavailable_reasons"].append("actual_execution_evidence_missing")
                    price, cost = None, None
                else:
                    event = execution[0]
                    for key in ("candidate_id", "quote_offer_id"):
                        if event.get(key) != record["binding"][key]:
                            raise EvidenceError("execution_ticket_binding_mismatch")
                    descriptor = next(e for e in events if e["manifest_sha256"] == event["manifest_sha256"])
                    review_ok = _review(descriptor.get("semantic_review"), event["manifest_sha256"], now)
                    record["execution_status"] = event.get("status", "unknown")
                    record["execution_event_id"] = event["event_id"]
                    record["execution_review"] = deepcopy(descriptor.get("semantic_review"))
                    price, cost = event.get("accepted_decimal_odds"), _cost(event.get("cost_per_unit"))
                    occurred = _time(event["occurred_at"]) if event.get("occurred_at") else None
                    eligible = (event.get("status") == "accepted" and event.get("usage") == "single"
                                and spec["usage"] == "single" and review_ok and occurred is not None
                                and occurred < _time(identity["kickoff"]) and event.get("conditions_status") == "satisfied")
                    if event.get("usage") == "parlay_leg" or spec["usage"] == "parlay_leg":
                        record["unavailable_reasons"].append("parlay_leg_has_no_separate_actual_single_return")
                    if not eligible:
                        record["unavailable_reasons"].append("actual_execution_details_or_review_not_eligible")
                    if _time(event["recorded_at"]) >= _time(identity["kickoff"]):
                        qualification_reasons.append("execution_recorded_after_kickoff")
                    record["actual_stake"] = event.get("stake")
            if price is not None and (not _number(price) or price <= 1):
                raise EvidenceError("invalid_scoring_decimal_odds")
            record["price_decimal"], record["cost_per_unit"] = price, cost
            if price is None:
                record["unavailable_reasons"].append("price_unknown")
            if cost is None:
                record["unavailable_reasons"].append("cost_unknown_not_zero")
            matrix, model_hashes, reason = _forecast(core, entries, contents, identity)
            record["source_pool"]["model_sha256s"] = model_hashes
            record["probability"]["reason"] = reason
            if reason in {"research_replay_forecast", "adopted_forecast_generated_after_kickoff"}:
                qualification_reasons.append(reason)
            if matrix is not None:
                prob = probabilities(matrix, selected_contract)
                record["probability"].update(status="available", five_state_probabilities=prob["probabilities"],
                                             possible_states=prob["possible_states"], structural_zero_states=prob["structural_zero_states"])
                if price is not None and cost is not None:
                    returns = {s: unit_return(s, price, cost) for s in STATES}
                    record["probability"].update(
                        profit_probability=sum(p for s, p in prob["probabilities"].items() if returns[s] > 0),
                        no_loss_probability=sum(p for s, p in prob["probabilities"].items() if returns[s] >= 0),
                        net_ev=sum(p * returns[s] for s, p in prob["probabilities"].items()))
            if result_value is not None:
                state = settlement(result_value["home_goals"], result_value["away_goals"], selected_contract)
                record["outcome"].update(status="settled", settlement_state=state)
                if matrix is not None:
                    support = prob["possible_states"]
                    record["probability"]["metrics"]["five_state"] = _categorical_metrics(
                        [[prob["probabilities"][s] for s in support]], [support.index(state)], tuple(support))
                    import numpy as np
                    home, away = np.indices(matrix.shape)
                    one_x_two = [float(matrix[mask].sum()) for mask in (home > away, home == away, home < away)]
                    observed_outcome = 0 if result_value["home_goals"] > result_value["away_goals"] else (
                        1 if result_value["home_goals"] == result_value["away_goals"] else 2)
                    record["probability"]["metrics"]["one_x_two"] = _categorical_metrics([one_x_two], [observed_outcome], OUTCOMES)
                if eligible and price is not None and cost is not None:
                    field = {"QUOTE_REF": "paper_reference_return", "QUOTE_OPEN": "open_simulated_return",
                             "QUOTE_ACTUAL": "actual_single_return"}[spec["quote_protocol"]]
                    if field != "actual_single_return" or record.get("result_source_status") != "unverified_normalized_declaration":
                        record["outcome"][field] = unit_return(state, price, cost)
                    if field == "actual_single_return" and record.get("actual_stake") is not None:
                        stake = record["actual_stake"]
                        if not _number(stake) or stake <= 0:
                            raise EvidenceError("invalid_actual_stake")
                        if record["outcome"][field] is not None:
                            record["outcome"]["actual_stake_return"] = stake * record["outcome"][field]
            record["return_eligible"] = eligible and price is not None and cost is not None
    record["qualification"] = "research_only" if qualification_reasons else "prospective_decision_candidate"
    record["qualification_reasons"] = sorted(set(qualification_reasons))
    record["unavailable_reasons"] = sorted(set(record["unavailable_reasons"]))
    if output_dir is not None:
        output = _no_symlinks(output_dir)
        if output.exists():
            raise EvidenceError("score_exists_no_overwrite")
        # Freeze all supplied parents needed for replay. Native upstream API
        # originals not supplied here are not claimed to have been reverified.
        files = {"score.json": _bytes(record), **{"evidence/" + k: v for k, v in evidence_copies.items()},
                 **{"decision/" + k: v for k, v in contents.items()},
                 "decision/manifest.json": _read_regular(bundle_dir, "manifest.json")}
        for digest, event_files in event_sources.items():
            descriptor = next(e for e in events if e["manifest_sha256"] == digest)
            files.update({"events/" + digest + "/" + k: v for k, v in event_files.items()})
            files["events/" + digest + "/manifest.json"] = _read_regular(descriptor["path"], "manifest.json")
        digest = _write_bundle(output, files, SCORE_BUNDLE)
        return {**record, "output_dir": str(output), "manifest_sha256": digest}
    return record


def score_decision(bundle_dir, *, expected_manifest_sha256, protocol_root, protocol_id,
                   result=None, result_source_root=None, events=(), quote_evidence=None,
                   quote_source_root=None, output_dir=None):
    """Fresh scoring always applies the current API AH quarantine policy.

    Historical replay is deliberately absent from this public/CLI interface.
    Only the sealed-score reader invokes the private legacy computation path.
    """
    return _score_decision(bundle_dir, expected_manifest_sha256=expected_manifest_sha256,
                           protocol_root=protocol_root, protocol_id=protocol_id,
                           result=result, result_source_root=result_source_root,
                           events=events, quote_evidence=quote_evidence,
                           quote_source_root=quote_source_root, output_dir=output_dir)


def read_decision_score(path, expected_manifest_sha256):
    """Read a content-checked score and check its immutable parent identities."""
    if not _hash(expected_manifest_sha256):
        raise EvidenceError("external_score_manifest_anchor_required")
    files, digest = _read_manifest(path, expected_manifest_sha256, SCORE_BUNDLE)
    score = json.loads(files["score.json"])
    if score.get("schema_version") != SCHEMA:
        raise EvidenceError("unsupported_score_schema")
    if score.get("api_ah_scoring_policy_version") not in {None, API_AH_SCORING_POLICY}:
        raise EvidenceError("unsupported_api_ah_scoring_policy_version")
    source = score["source"]
    verify_decision_bundle(_no_symlinks(path) / "decision", source["decision_manifest_sha256"])
    if _sha(files["evidence/protocol.json"]) != source["protocol_sha256"]:
        raise EvidenceError("score_protocol_parent_mismatch")
    for field, filename in (("result_sha256", "result.json"), ("quote_evidence_sha256", "open_quote.json")):
        if source.get(field) is not None and _sha(files["evidence/" + filename]) != source[field]:
            raise EvidenceError("score_evidence_parent_mismatch")
    # Recompute using frozen parents, retaining the original observed score time.
    import tempfile
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        (root / "protocols").mkdir()
        (root / "protocols" / (source["protocol_sha256"] + ".json")).write_bytes(files["evidence/protocol.json"])
        event_args = []
        for event_hash in source["event_manifest_sha256s"]:
            event_args.append({"path": str(_no_symlinks(path) / "events" / event_hash), "manifest_sha256": event_hash,
                               "semantic_review": score.get("execution_review")})
        result = deepcopy(score.get("result_evidence_descriptor"))
        result_root = path
        if result:
            result_root = root / "result"
            result_root.mkdir()
            (result_root / "result.json").write_bytes(files["evidence/result.json"])
            result["path"] = "result.json"
            for relative, blob in files.items():
                if relative.startswith("evidence/result_sources/"):
                    destination = result_root / _relative(relative[len("evidence/result_sources/"):])
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_bytes(blob)
        quote = {"path": "evidence/open_quote.json", "sha256": source["quote_evidence_sha256"],
                 "semantic_review": score.get("open_quote_review")} if source.get("quote_evidence_sha256") else None
        quote_root = path
        if quote:
            quote_root = root / "open"
            quote_root.mkdir()
            (quote_root / "open_quote.json").write_bytes(files["evidence/open_quote.json"])
            quote["path"] = "open_quote.json"
            for relative, blob in files.items():
                if relative.startswith("evidence/open_sources/"):
                    destination = quote_root / _relative(relative[len("evidence/open_sources/"):])
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_bytes(blob)
        replay = _score_decision(_no_symlinks(path) / "decision", expected_manifest_sha256=source["decision_manifest_sha256"],
                                protocol_root=root, protocol_id=source["protocol_sha256"], result=result,
                                result_source_root=result_root, events=event_args, quote_evidence=quote, quote_source_root=quote_root,
                                _legacy_api_ah_score_replay=score.get("api_ah_scoring_policy_version") is None)
    scored_at = _time(score.get("scored_at"))
    earliest = max(_time(score["decision_times"]["report_generated_at"]),
                   _time(json.loads(files["evidence/protocol.json"])["registered_at"]))
    if score.get("result"):
        earliest = max(earliest, _time(score["result"].get("captured_at", score["result"]["observed_at"])))
    for event_hash in source["event_manifest_sha256s"]:
        earliest = max(earliest, _time(json.loads(files["events/" + event_hash + "/event.json"])["recorded_at"]))
    if scored_at > _now() or scored_at < earliest:
        raise EvidenceError("score_time_in_future")
    replay["scored_at"] = score["scored_at"]
    if result:
        replay["result_evidence_descriptor"]["path"] = score["result_evidence_descriptor"]["path"]
    if replay != score:
        raise EvidenceError("score_replay_mismatch")
    parent_core = json.loads(files["decision/decision.json"])
    parent_candidate = next((c for c in parent_core["archive"]["candidates"]
                             if c["candidate_id"] == parent_core.get("primary_candidate_id")), None)
    unverified_api_parent = _candidate_uses_unverified_api_ah(parent_core, parent_candidate)
    return {**score, "manifest_sha256": digest, "verification_status": "verified",
            **({"historical_api_ah_mapping_unverified": True, "new_api_ah_admission_allowed": False,
                "verification_scope": "sealed_historical_score_replay_not_provider_mapping_certification"}
               if unverified_api_parent else {})}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    register = sub.add_parser("register")
    register.add_argument("--spec", required=True)
    register.add_argument("--root", required=True)
    score = sub.add_parser("score")
    score.add_argument("--request", required=True, help="JSON keyword arguments to score_decision")
    verify = sub.add_parser("verify")
    verify.add_argument("--bundle", required=True)
    verify.add_argument("--manifest-sha256", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "register":
            answer = register_scoring_protocol(json.loads(Path(args.spec).read_text()), args.root)
        elif args.command == "score":
            answer = score_decision(**json.loads(Path(args.request).read_text()))
        else:
            answer = read_decision_score(args.bundle, args.manifest_sha256)
        print(json.dumps(answer, ensure_ascii=False, indent=2, allow_nan=False))
        return 0
    except (OSError, KeyError, ValueError, TypeError) as exc:
        print(json.dumps({"status": "error", "error_type": type(exc).__name__,
                          "error": str(exc) if isinstance(exc, EvidenceError) else "malformed_scoring_input"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
