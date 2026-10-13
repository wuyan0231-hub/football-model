"""Verified research predictions to identical contracts and reviewed quote rows.

This is a calculation/record link, never an automatic recommendation or a trade.
Every candidate is reproduced by its snapshot reader before entering this layer.
No missing price, fee, current-open evidence or actual fill is inferred.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import html
import json
import math
from pathlib import Path
import re

from .decision_quotes import adapt_quote_row
from .evidence import _quote_interval, _time, select_quotes
from .settlement import STATES, probabilities, matrix_array, unit_return

VERSION = "feature-decision-comparison-v1"
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _read_ref(runtime, ref):
    """Read an exact runtime-relative regular original, bounded and hash checked."""
    if not isinstance(ref, dict):
        raise ValueError("decision_evidence_reference_object_required")
    root = Path(runtime).resolve()
    relative = ref.get("runtime_relative_path", ref.get("path"))
    if (not isinstance(relative, str) or not relative or "\\" in relative
            or Path(relative).is_absolute() or ".." in Path(relative).parts):
        raise ValueError("decision_unsafe_evidence_path")
    path = root / relative
    if any(p.is_symlink() for p in (path, *path.parents)) or not path.is_file():
        raise ValueError("decision_evidence_not_regular")
    if path.stat().st_size > 60 * 1024 * 1024:
        raise ValueError("decision_evidence_size_limit")
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if digest != ref.get("sha256") or ("bytes" in ref and len(raw) != ref["bytes"]):
        raise ValueError("decision_evidence_hash_mismatch")
    return raw, {"path": relative, "sha256": digest, "bytes": len(raw)}


def _json(raw):
    return json.loads(raw, parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite_json")))


def stage_decision_input(runtime, input_path, *, source_root):
    """Copy explicitly selected external input/sources to immutable runtime paths.

    This preserves original bytes and records the path-only transformation. It
    does not review images or authenticate a caller's reviewed declaration.
    """
    runtime, source_root = Path(runtime).resolve(), Path(source_root).resolve()
    input_path = Path(input_path)
    if not input_path.is_absolute():
        input_path = source_root / input_path
    raw, original_ref = _read_ref(source_root, _file_ref(source_root, input_path))
    document = _json(raw)
    if not isinstance(document, dict) or document.get("schema_version") != "feature-decision-input-v1":
        raise ValueError("decision_input_schema_required")
    if not isinstance(document.get("sources"), list):
        raise ValueError("decision_sources_list_required")
    prepared, decision_prepared = [], []
    def prepare(ref):
        content, checked = _read_ref(source_root, ref)
        suffix = Path(checked["path"]).suffix.lower()
        if suffix not in IMAGE_SUFFIXES | {".json", ".txt", ".md", ".csv", ".tsv"}:
            raise ValueError("decision_source_format_not_supported")
        if suffix in IMAGE_SUFFIXES:
            from .state_storage import _valid_decision_image_header
            if not _valid_decision_image_header(suffix, content):
                raise ValueError("decision_image_header_invalid")
        return content, "feature_decisions/sources/" + checked["sha256"] + suffix
    for ref in document["sources"]:
        content, destination = prepare(ref)
        prepared.append((ref, content, destination))
    if document.get("decision_record") is not None:
        record = document["decision_record"]
        if not isinstance(record, dict):
            raise ValueError("native_s2_decision_record_required")
        for group in ("inputs", "derived_artifacts"):
            rows = record.get(group, [])
            if not isinstance(rows, list) or any(not isinstance(item, dict) for item in rows):
                raise ValueError("native_decision_evidence_list_required")
            for index, item in enumerate(rows):
                content, destination = prepare(item)
                decision_prepared.append((group, index, item["path"], content, destination))
    def save(relative, content):
        target = runtime / relative
        if any(p.is_symlink() for p in (target, *target.parents)):
            raise ValueError("decision_evidence_not_regular")
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            with target.open("xb") as handle:
                handle.write(content)
        except FileExistsError:
            if not target.is_file() or target.read_bytes() != content:
                raise ValueError("immutable_decision_source_collision")
        return {"path": relative, "sha256": hashlib.sha256(content).hexdigest(), "bytes": len(content)}
    original = save("feature_decisions/inputs/original/" + original_ref["sha256"] + ".json", raw)
    staged, mapping = deepcopy(document), []
    staged["sources"] = []
    for ref, content, destination in prepared:
        copied = save(destination, content)
        moved = {**ref, **copied}
        if "runtime_relative_path" in moved:
            moved["runtime_relative_path"] = copied["path"]
        staged["sources"].append(moved)
        mapping.append({"original_path": ref["path"], "copied_ref": copied, "source_id": ref.get("source_id")})
    decision_paths = []
    for group, index, old_path, content, destination in decision_prepared:
        copied = save(destination, content)
        staged["decision_record"][group][index]["path"] = copied["path"]
        decision_paths.append({"group": group, "index": index, "original_path": old_path, "copied_ref": copied})
    staged["staging"] = {"operation": "exact_source_bytes_path_relocation_only", "original_input_ref": original,
                         "sources": mapping, "created_at": datetime.now(timezone.utc).isoformat(),
                         "qualification": "archived_now_does_not_establish_historical_blindness"}
    if decision_paths:
        staged["staging"]["decision_paths"] = decision_paths
    payload = json.dumps(staged, ensure_ascii=False, sort_keys=True, allow_nan=False).encode()
    return save("feature_decisions/inputs/" + hashlib.sha256(payload).hexdigest() + ".json", payload)["path"]


def _file_ref(runtime, path):
    path = Path(path)
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError("decision_evidence_not_regular")
    return {"path": str(path.resolve().relative_to(Path(runtime).resolve())),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def schedule_comparisons(runtime, league, match_id, *, row, now):
    """Read every v2 protocol's immutable first legal capture, keeping failures."""
    from .feature_snapshots import verify_baseline_capture, verify_capture
    results = []
    pattern = f"*/captures/{league}/{match_id}/*/first.json"
    for path in sorted((Path(runtime) / "feature_snapshots").glob(pattern)):
        result = {"source": "schedule", "horizon": path.parent.name,
                  "status": "rejected", "candidates": {}, "evidence": []}
        try:
            descriptor_raw, descriptor_ref = _read_ref(runtime, _file_ref(runtime, path))
            receipt_raw, receipt_ref = _read_ref(runtime, _json(descriptor_raw))
            receipt = _json(receipt_raw)
            if _time(receipt["storage_completed_at"]) > now:
                raise ValueError("capture_after_analysis_cutoff")
            snapshot = verify_baseline_capture(runtime, receipt)
            if snapshot["protocol"].get("schema_version") != "feature-model-protocol-v2":
                continue  # v1 stays on its original reader and score policy.
            if snapshot.get("baseline_parent_row") != row:
                raise ValueError("candidate_full_production_parent_mismatch")
            if receipt.get("live_capture_verified") is not True:
                raise ValueError("candidate_not_legal_live_capture")
            _, payload_ref = _read_ref(runtime, receipt["payload_ref"])
            key = snapshot["protocol"].get("candidate_id", "M1_schedule")
            candidate = {"status": snapshot.get("candidate_status"),
                         "reason": snapshot.get("candidate_missing_reason"),
                         "forecast_id": snapshot["snapshot_id"],
                         "captured_at": receipt["storage_completed_at"]}
            result.update(status="verified_same_parent", baseline_parent_row=deepcopy(row),
                baseline=deepcopy(snapshot["baseline"]), protocol_sha256=snapshot["protocol_sha256"],
                evidence=[descriptor_ref, receipt_ref, payload_ref])
            for ref in snapshot["source_refs"]:
                try:
                    _, checked = _read_ref(runtime, ref)
                    result["evidence"].append(checked)
                except (ValueError, KeyError, TypeError, OSError):
                    # A broken candidate source remains referenced by its original
                    # payload and is rejected below; keep independent M0 closure.
                    pass
            try:
                verify_capture(runtime, receipt)
                if snapshot["candidate_status"] == "predicted":
                    candidate.update(status="verified", prediction=deepcopy(snapshot["candidate"]["prediction"]))
            except (ValueError, KeyError, TypeError, OSError) as exc:
                candidate.update(status="rejected", reason=str(exc))
            result["candidates"][key] = candidate
        except (ValueError, KeyError, TypeError, OSError) as exc:
            result["reason"] = str(exc)
            if not result["candidates"]:
                result["candidates"] = {"M1_schedule_offset_l2_v2": {"status": "rejected", "reason": str(exc)}}
        results.append(result)
    if not results:
        results.append({"source": "schedule", "status": "not_captured", "candidates": {
                        "M1_schedule_offset_l2_v2": {"status": "not_captured", "reason": "no_v2_legal_capture_for_match"}},
                        "evidence": [], "reason": "no_v2_legal_capture_for_match"})
    return results


def _contract(offer):
    market = {"1x2": "one_x_two", "ah": "asian_handicap", "ou": "total_goals"}[offer["market"]]
    side = ({"home": "home_win", "away": "away_win", "draw": "draw"}[offer["selection"]]
            if market == "one_x_two" else offer["selection"])
    return {"market": market, "side": side, "line": offer["line"],
            "settlement_basis": "regulation_time_90_plus_stoppage"}


def project_offer(prediction, offer, cost_per_unit=None):
    """Deterministic five-state projection; fees must be explicit for net EV."""
    if cost_per_unit is not None and (isinstance(cost_per_unit, bool)
            or not isinstance(cost_per_unit, (int, float)) or not math.isfinite(cost_per_unit)
            or not 0 <= cost_per_unit < 1):
        raise ValueError("explicit_cost_per_unit_must_be_in_zero_one")
    projected = probabilities(prediction["score_matrix"], _contract(offer))
    p = projected["probabilities"]
    price = offer.get("decimal_odds")
    returns = {s: unit_return(s, price, 0) for s in STATES} if price is not None else None
    gross = math.fsum(p[s] * returns[s] for s in STATES) if returns else None
    net_returns = ({s: unit_return(s, price, cost_per_unit) for s in STATES}
                   if price is not None and cost_per_unit is not None else None)
    return {**projected, "contract": _contract(offer), "decimal_odds": price,
        "cost_per_unit": cost_per_unit, "gross_unit_returns": returns,
        "gross_ev": gross, "net_ev": gross - cost_per_unit if net_returns else None,
        "positive_gross_return_probability": p["win"] + p["half_win"] if returns else None,
        "nonnegative_gross_return_probability": p["win"] + p["half_win"] + p["push"] if returns else None,
        "positive_net_return_probability": math.fsum(p[s] for s in STATES if net_returns[s] > 0) if net_returns else None,
        "nonnegative_net_return_probability": math.fsum(p[s] for s in STATES if net_returns[s] >= 0) if net_returns else None,
        "price_status": "reference_price" if price is not None else "unknown_or_closed",
        "net_ev_status": "calculated_under_explicit_cost" if net_returns else
            ("fee_unknown" if returns else "price_unknown"),
        "current_open_status": "claim_only" if offer.get("current_open_claim") else "unknown",
        "execution_status": "claim_only" if offer.get("actual_execution_claim") else "unknown"}


def _load_quotes(runtime, input_path, match_id, now):
    path = Path(input_path)
    if not path.is_absolute():
        path = Path(runtime) / path
    raw, input_ref = _read_ref(runtime, _file_ref(runtime, path))
    value = _json(raw)
    if not isinstance(value, dict) or value.get("schema_version") != "feature-decision-input-v1":
        raise ValueError("decision_input_schema_required")
    if str(value.get("match_id")) != str(match_id):
        raise ValueError("decision_input_match_mismatch")
    if _time(value.get("analysis_as_of")) != now:
        raise ValueError("decision_input_cutoff_mismatch")
    evidence, sources = [input_ref], {}
    if value.get("staging") is not None:
        staging = value["staging"]
        if not isinstance(staging, dict) or staging.get("operation") != "exact_source_bytes_path_relocation_only":
            raise ValueError("decision_staging_contract_invalid")
        original_raw, original_ref = _read_ref(runtime, staging["original_input_ref"])
        original = _json(original_raw)
        rebuilt = deepcopy(value)
        rebuilt.pop("staging")
        mapped = staging.get("sources", [])
        if len(mapped) != len(rebuilt.get("sources", [])) or len(mapped) != len(original.get("sources", [])):
            raise ValueError("decision_staging_sources_mismatch")
        for old, new, mapping in zip(original["sources"], rebuilt["sources"], mapped):
            expected = {**old, **mapping["copied_ref"]}
            if "runtime_relative_path" in expected:
                expected["runtime_relative_path"] = mapping["copied_ref"]["path"]
            if mapping["original_path"] != old["path"] or new != expected or old["sha256"] != new["sha256"]:
                raise ValueError("decision_staging_source_changed")
        rebuilt["sources"] = original["sources"]
        for mapping in staging.get("decision_paths", []):
            group, index = mapping["group"], mapping["index"]
            if group not in {"inputs", "derived_artifacts"} or type(index) is not int or index < 0:
                raise ValueError("decision_staging_record_path_invalid")
            if index >= len(rebuilt["decision_record"].get(group, [])) or index >= len(original["decision_record"].get(group, [])):
                raise ValueError("decision_staging_record_path_invalid")
            target = rebuilt["decision_record"][group][index]
            old = original["decision_record"][group][index]
            if (mapping["original_path"] != old["path"] or target["path"] != mapping["copied_ref"]["path"]
                    or target["sha256"] != mapping["copied_ref"]["sha256"]):
                raise ValueError("decision_staging_record_source_changed")
            target["path"] = old["path"]
        if rebuilt != original:
            raise ValueError("decision_staging_changed_input_content")
        evidence.append(original_ref)
    if not isinstance(value.get("sources", []), list):
        raise ValueError("decision_sources_list_required")
    for ref in value.get("sources", []):
        source_raw, checked = _read_ref(runtime, ref)
        suffix = Path(checked["path"]).suffix.lower()
        if suffix in IMAGE_SUFFIXES:
            if not checked["path"].startswith("feature_decisions/sources/"):
                raise ValueError("decision_images_must_use_feature_decisions_sources_directory")
            from .state_storage import _valid_decision_image_header
            if not _valid_decision_image_header(suffix, source_raw):
                raise ValueError("decision_image_header_invalid")
        source_id = ref.get("source_id")
        if not isinstance(source_id, str) or not source_id or source_id in sources:
            raise ValueError("decision_quote_source_identity_required")
        if _time(ref.get("observed_at")) > now:
            raise ValueError("decision_quote_source_after_cutoff")
        if ref.get("reviewed") is not True:
            raise ValueError("decision_quote_source_review_required")
        sources[source_id] = ref
        evidence.append(checked)
    rows = value.get("quote_rows", [])
    if not isinstance(rows, list) or any(not isinstance(r, dict) for r in rows):
        raise ValueError("decision_quote_rows_list_required")
    if value.get("selection") is not None and not isinstance(value["selection"], dict):
        raise ValueError("decision_selection_object_required")
    # Existing selector requires an open state to validate numeric reference-only
    # rows. Selection consumes the temporary view; adapter receives original bytes.
    selector_rows = [{**r, "status": "open"} if r.get("status") == "reference_only" else r for r in rows]
    selected = select_quotes(selector_rows, now.isoformat())
    by_id = {r["row_id"]: r for r in rows}
    adapted, issues = [], []
    for group in selected["groups"]:
        chosen = group.get("as_of_latest")
        if chosen is None:
            issues.append({"source_id": group["source_id"], "reason": group["status"]})
            continue
        original = by_id[chosen["row_id"]]
        try:
            source = sources.get(original.get("source_id"))
            if source is None:
                raise ValueError("decision_quote_source_not_frozen")
            observed = _time(original.get("observed_at"))
            if not _time(source["observed_at"]) <= observed <= now:
                raise ValueError("decision_quote_observation_time_mismatch")
            for field in ("open_evidence", "execution_evidence"):
                claims = original.get(field) or {}
                if not isinstance(claims, dict) or any(not isinstance(c, dict) for c in claims.values()):
                    raise ValueError("decision_quote_claims_object_required")
                for claim in claims.values():
                    claim_source = sources.get(claim.get("source_id"))
                    if claim_source is None or not _time(claim_source["observed_at"]) <= _time(claim["observed_at"]) <= now:
                        raise ValueError("decision_quote_claim_not_frozen_at_cutoff")
            adapted.append(adapt_quote_row(original, match_id=str(match_id)))
        except (ValueError, KeyError, TypeError) as exc:
            issues.append({"row_id": original["row_id"], "reason": str(exc)})
    cost = value.get("cost_per_unit")
    if cost is not None:
        # Validate zero explicitly too: omission never silently means zero fees.
        unit_return("push", 2, cost)
    return value, adapted, issues, evidence


def _diagnostics(prediction, baseline):
    matrix = matrix_array(prediction["score_matrix"])
    other = matrix_array(baseline["score_matrix"])
    def win_probabilities(m):
        import numpy as np
        h, a = np.indices(m.shape)
        return {"home_win": float(m[h > a].sum()), "draw": float(m[h == a].sum()), "away_win": float(m[h < a].sum())}
    probabilities_, baseline_p = win_probabilities(matrix), win_probabilities(other)
    rates, brates = prediction.get("expected_goals"), baseline.get("expected_goals")
    return {"probabilities": probabilities_, "delta_probabilities": {k: probabilities_[k]-baseline_p[k] for k in probabilities_},
            "expected_goals": rates, "delta_expected_goals": {s: rates[s]-brates[s] for s in ("home", "away")} if rates and brates else None,
            "score_matrix_shape": list(matrix.shape), "score_matrix_sha256": _digest(prediction["score_matrix"]),
            "tail_handling": prediction.get("tail_handling", prediction.get("score_matrix_axes"))}


def _decision_record_link(runtime, value, result, row, now):
    """Replay the existing S2 decision schema, then bind its declared choices.

    This is a read-only derived view, not a newly published S2 bundle. Neither
    candidate roles nor fit/grade declarations are generated from EV rankings.
    """
    from .decision_record import _collect_evidence, _compile, _validation, _views, _issue
    record = value.get("decision_record")
    if not isinstance(record, dict):
        raise ValueError("native_s2_decision_record_required")
    if str(record.get("match_id")) != str(result["match_id"]):
        raise ValueError("native_decision_match_mismatch")
    if _time(record.get("analysis_cutoff")) != now or _time(record.get("kickoff_at")) != _time(row["kickoff"]):
        raise ValueError("native_decision_time_mismatch")
    issues = []
    actual = datetime.now(timezone.utc)
    entries, _, reasons = _collect_evidence(record, Path(runtime), actual, issues)
    evidence = []
    for entry in entries:
        if entry.get("read_status") == "failed":
            continue
        # Re-read exact original bytes rather than trusting a compiled source.
        _, checked = _read_ref(runtime, {"path": entry["original_path"], "sha256": entry["sha256"]})
        evidence.append(checked)
    if any(reason in reasons for reason in ("observed_after_cutoff", "published_after_cutoff")):
        _issue(issues, "native_decision_external_information_after_cutoff", "inputs")
    effective = {**record, "membership_view_version": "all_candidates_v1",
                 "api_ah_mapping_policy_version": "api-ah-quarantine-v1"}
    core = _compile(effective, entries, issues)
    quotes = {q["quote_row_id"]: q for q in result["quote_records"]}
    for quoted in core["adapted_quote_rows"]:
        other = quotes.get(quoted["quote_row_id"])
        if other is None or other["original_row"] != quoted["original_row"]:
            _issue(issues, "native_decision_quote_not_selected_at_cutoff", "quote_rows")
    links = value.get("decision_calculation_links", [])
    if not isinstance(links, list) or any(not isinstance(link, dict) for link in links):
        raise ValueError("decision_calculation_links_list_required")
    candidates = {c.get("candidate_id"): c for c in core["archive"]["candidates"]}
    mapping = {}
    for link in links:
        cid = link.get("candidate_id")
        if not isinstance(cid, str) or cid not in candidates or cid in mapping:
            _issue(issues, "native_decision_candidate_link_not_unique", "decision_calculation_links")
            continue
        mapping[cid] = link
    handling = (core.get("decision_chain") or {}).get("model_disagreements", [])
    if not isinstance(handling, list) or any(not isinstance(item, dict) for item in handling):
        _issue(issues, "native_decision_disagreements_list_required", "decision_chain")
        handling = []
    bindings, disagreements = [], []
    for cid, candidate in candidates.items():
        link = mapping.get(cid)
        if link is None:
            _issue(issues, "native_decision_numeric_binding_not_supplied", cid, "not_evaluated")
            continue
        matches = [c for c in result["calculations"] if c["model_key"] == link.get("model_key")
                   and c["quote_offer_id"] == candidate.get("quote_offer_id")
                   and c["contract_id"] == candidate.get("contract_id") and c["status"] == "calculated"]
        if len(matches) != 1:
            _issue(issues, "native_decision_calculation_unavailable", cid)
            continue
        calculated = matches[0]
        binding = {"candidate_id": cid, "model_key": calculated["model_key"],
            "calculation_id": calculated["calculation_id"], "forecast_id": calculated.get("forecast_id"),
            "contract_id": calculated["contract_id"], "quote_offer_id": calculated["quote_offer_id"],
            "formal_fit": deepcopy(candidate["formal_fit"]), "result": deepcopy(calculated["result"])}
        bindings.append(binding)
        declared_value = candidate.get("value") or {}
        if declared_value.get("ev") is not None:
            computed_ev = calculated["result"]["net_ev"]
            if computed_ev is None or not math.isclose(declared_value["ev"], computed_ev, abs_tol=1e-12, rel_tol=1e-9):
                _issue(issues, "native_decision_ev_does_not_match_calculation", cid)
        if declared_value.get("ev") is not None or declared_value.get("breakeven_odds") is not None:
            archived_calculation = next((c for c in core["archive"]["calculation_records"]
                if c.get("calculation_record_id") == declared_value.get("calculation_record_id")), {})
            archived_model = next((m for m in core["archive"]["model_records"]
                if m.get("model_record_id") == archived_calculation.get("model_record_id")), {})
            if archived_model.get("forecast_id") != calculated.get("forecast_id"):
                _issue(issues, "native_decision_numeric_forecast_mismatch", cid)
        if declared_value.get("breakeven_odds") is not None:
            p = calculated["result"]["probabilities"]
            denominator = p["win"] + .5 * p["half_win"]
            fee = calculated["result"]["cost_per_unit"]
            expected = 1 + (p["loss"] + .5*p["half_loss"] + fee)/denominator if fee is not None and denominator > 0 else None
            if expected is None or not math.isclose(declared_value["breakeven_odds"], expected, abs_tol=1e-12, rel_tol=1e-9):
                _issue(issues, "native_decision_breakeven_does_not_match_calculation", cid)
        if calculated["model_key"] != "M0" and declared_value.get("status") in {"confirmed_positive", "confirmed_adverse"}:
            _issue(issues, "research_candidate_cannot_confirm_value", cid)
        # Every sign disagreement for this declared ticket is retained, even
        # when the analyst chose M0 or a different candidate model.
        for peer in result["calculations"]:
            if (peer["quote_offer_id"] != candidate.get("quote_offer_id")
                    or peer.get("net_ev_sign_disagreement") is not True):
                continue
            declarations = [d for d in handling if d.get("candidate_id") == cid and d.get("model_key") == peer["model_key"]]
            declaration = declarations[0] if len(declarations) == 1 else None
            valid = (declaration is not None and declaration.get("treatment") in
                     {"retain", "reduce", "switch", "condition", "reject"}
                     and all(isinstance(declaration.get(k), str) and declaration[k].strip()
                             for k in ("reason", "information_basis")))
            disagreements.append({"candidate_id": cid, "model_key": peer["model_key"],
                "calculation_id": peer["calculation_id"], "net_ev": peer["result"]["net_ev"],
                "delta_net_ev": peer["delta_net_ev"], "handling": deepcopy(declaration),
                "status": "declared_semantics_require_review" if valid else "unhandled"})
            if not valid:
                _issue(issues, "native_decision_model_disagreement_unhandled", cid + ":" + peer["model_key"], "not_evaluated")
    primary = next((b for b in bindings if b["candidate_id"] == core.get("primary_candidate_id")), None)
    selection = value.get("selection")
    if selection is not None and (primary is None or result["decision"].get("calculation_id") != primary["calculation_id"]):
        _issue(issues, "native_decision_conflicts_with_selection", "selection")
    if primary and core.get("forecast_id") is not None and core["forecast_id"] != primary["forecast_id"]:
        _issue(issues, "native_decision_adopted_forecast_mismatch", "forecast_id")
    validation = _validation(core, issues)
    eligibility = {"qualification": "research_only", "reasons": reasons,
        "historical_blindness_proven": False, "prediction_validated": False,
        "scope": "read_only_decision_replay_not_publication_or_confirmatory_evidence"}
    views = _views(core, validation, eligibility)
    return {"status": validation["status"], "schema_version": "feature-s2-decision-link-v1",
        "decision_record": core, "validation": validation, "views": views, "eligibility": eligibility,
        "candidate_calculations": bindings, "model_disagreements": disagreements,
        "primary_calculation_id": primary["calculation_id"] if primary else None,
        "automatic_recommendation": False, "formal_grade_verified": False,
        "independent_gpt_runs_verified": False, "execution_status": "unknown", "evidence": evidence}


def comparison(runtime, league, match_id, *, row, now, baseline_refs=(),
               f5c=None, decision_input=None):
    """Build the actual immutable numeric-record link for integration.match."""
    if not re.fullmatch(r"[A-Za-z0-9_-]+", str(league)) or isinstance(match_id, bool) or int(match_id) <= 0:
        raise ValueError("decision_invalid_fixture_identity")
    now = _time(now) if isinstance(now, str) else now
    baseline = row["prediction"]
    result = {"schema_version": VERSION, "match_id": match_id, "competition": league,
        "analysis_as_of": now.isoformat(), "baseline_parent_sha256": _digest(row),
        "research_only": True, "production_promotion": False, "confirmation_eligible": False,
        "status": "no_quote_supplied", "comparisons": [], "model_records": [],
        "quote_records": [], "calculations": [], "quote_issues": [], "evidence": [],
        "decision": {"status": "not_supplied", "automatic_recommendation": False},
        "scope": "same_contract_same_reference_quote_diagnostics; differing horizons retain their information ages"}
    for ref in baseline_refs:
        _, checked = _read_ref(runtime, ref)
        result["evidence"].append(checked)
    models = [{"model_key": "M0", "status": "verified", "forecast_id": _digest(row), "prediction": deepcopy(baseline),
               "captured_at": row.get("generated_at"), "source": "production_baseline"}]
    results = schedule_comparisons(runtime, league, match_id, row=row, now=now)
    from .advanced_snapshots import load_verified_comparison
    for horizon in ("EARLY", "LINEUP"):
        try:
            advanced = load_verified_comparison(runtime, league, match_id, horizon, row=row, now=now)
            results.append({**advanced, "source": "advanced", "horizon": horizon})
        except (ValueError, KeyError, TypeError, OSError) as exc:
            results.append({"source": "advanced", "horizon": horizon, "status": "rejected", "reason": str(exc), "candidates": {}})
    for item in results:
        scope = "/".join(str(item.get(k, "unknown")) for k in ("source", "protocol_sha256", "horizon"))
        staged, source_failures = [], []
        try:
            parent = item.get("baseline_parent_row")
            if parent is not None and parent != row:
                raise ValueError("candidate_full_production_parent_mismatch")
            for ref in item.get("evidence", []):
                try:
                    _, checked = _read_ref(runtime, ref)
                    result["evidence"].append(checked)
                except (ValueError, KeyError, TypeError, OSError) as exc:
                    source_failures.append({"path": ref.get("path"), "reason": str(exc)})
            for key, candidate in item.get("candidates", {}).items():
                model = {**deepcopy(candidate), "model_key": scope + "/" + key, "source": item["source"], "horizon": item.get("horizon")}
                if model.get("prediction") is not None:
                    if parent != row or item.get("status") not in {"verified_same_parent", "available"} or candidate.get("status") != "verified":
                        raise ValueError("candidate_verified_full_parent_required")
                    if source_failures:
                        model.pop("prediction", None)
                        model.update(status="rejected", reason="candidate_evidence_changed_or_missing", evidence_failures=source_failures)
                    else:
                        matrix_array(model["prediction"]["score_matrix"])
                        model["status"] = "verified"
                staged.append(model)
            if source_failures:
                item = {**item, "status": "partial_evidence_rejected", "evidence_failures": source_failures}
        except (ValueError, KeyError, TypeError, OSError) as exc:
            staged = [{"model_key": scope + "/" + key, "source": item["source"], "horizon": item.get("horizon"),
                       "status": "rejected", "reason": str(exc), "original_status": candidate.get("status")}
                      for key, candidate in item.get("candidates", {}).items()]
            item = {**item, "status": "rejected", "reason": str(exc)}
        models.extend(staged)
        result["comparisons"].append(item)
    if f5c and f5c.get("status") == "verified_same_parent":
        if f5c.get("baseline") == baseline and f5c.get("selected_prediction"):
            models.append({"model_key": "F5-C/selected", "status": "verified", "source": "f5c",
                "forecast_id": f5c.get("forecast_id"), "prediction": deepcopy(f5c["selected_prediction"]),
                "selection": deepcopy(f5c.get("selection"))})
    for model in models:
        if model.get("status") == "verified":
            model["diagnostics"] = _diagnostics(model["prediction"], baseline)
    result["model_records"] = models
    if decision_input is not None:
        try:
            input_path = Path(decision_input)
            if not input_path.is_absolute():
                input_path = Path(runtime) / input_path
            _, input_ref = _read_ref(runtime, _file_ref(runtime, input_path))
            result["evidence"].append(input_ref)
            value, quotes, issues, refs = _load_quotes(runtime, decision_input, match_id, now)
            result.update(quote_records=quotes, quote_issues=issues,
                          status="calculated" if quotes else "no_eligible_quote")
            result["evidence"].extend(refs)
            for quote in quotes:
                for offer in quote["offers"]:
                    if offer.get("contract_id") is None:
                        continue
                    base = project_offer(baseline, offer, value.get("cost_per_unit"))
                    for model in models:
                        calculation = {"model_key": model["model_key"], "forecast_id": model.get("forecast_id"),
                            "quote_offer_id": offer["quote_offer_id"], "contract_id": offer["contract_id"],
                            "row_sha256": quote["row_sha256"], "status": model["status"]}
                        if model["status"] == "verified":
                            projected = project_offer(model["prediction"], offer, value.get("cost_per_unit"))
                            calculation.update(status="calculated", result=projected,
                                delta_probabilities={s: projected["probabilities"][s] - base["probabilities"][s] for s in STATES},
                                delta_gross_ev=projected["gross_ev"]-base["gross_ev"] if base["gross_ev"] is not None else None,
                                delta_net_ev=projected["net_ev"]-base["net_ev"] if base["net_ev"] is not None else None,
                                net_ev_sign_disagreement=(projected["net_ev"] > 0) != (base["net_ev"] > 0) if base["net_ev"] is not None else None)
                        else:
                            calculation.update(reason=model.get("reason"), result=None)
                        calculation["calculation_id"] = _digest(calculation)
                        result["calculations"].append(calculation)
            selected = value.get("selection")
            if selected is not None:
                offers = [o for q in quotes for o in q["offers"] if
                    q["original_row"]["row_id"] == selected.get("quote_row_id") and o["selection"] == selected.get("selection")]
                linked = [c for c in result["calculations"] if c["model_key"] == selected.get("model_key")
                          and len(offers) == 1 and c["quote_offer_id"] == offers[0]["quote_offer_id"] and c["status"] == "calculated"]
                result["decision"] = {"status": "explicit_selection_linked" if len(linked) == 1 else "explicit_selection_unavailable",
                    "declaration": deepcopy(selected), "calculation_id": linked[0]["calculation_id"] if len(linked) == 1 else None,
                    "selected_model": next((m for m in models if m["model_key"] == selected.get("model_key")), None),
                    "automatic_recommendation": False, "formal_grade_verified": False,
                    "meaning": "user_or_analyst_declared_selection; no automatic highest_EV_adoption_or_trade"}
                if len(linked) == 1:
                    chosen = linked[0]
                    result["decision"]["numeric_diagnostics"] = {
                        "five_state_probabilities": chosen["result"]["probabilities"],
                        "net_ev": chosen["result"]["net_ev"], "net_ev_status": chosen["result"]["net_ev_status"],
                        "delta_net_ev": chosen["delta_net_ev"],
                        "net_ev_sign_disagreement": chosen["net_ev_sign_disagreement"],
                        "review_reason": "fees_or_price_unknown" if chosen["result"]["net_ev"] is None else
                            "selected_model_net_ev_negative" if chosen["result"]["net_ev"] < 0 else
                            "model_and_baseline_ev_sign_disagree" if chosen["net_ev_sign_disagreement"] else
                            "quantified_diagnostics_do_not_validate_fit_or_grades"}
            if value.get("decision_record") is not None:
                try:
                    linked_record = _decision_record_link(runtime, value, result, row, now)
                    result["evidence"].extend(linked_record.pop("evidence"))
                    # Preserve an old selection declaration as a separately
                    # checked input; the native record owns the rendered views.
                    linked_record["selection_declaration"] = deepcopy(result["decision"])
                    result["decision"] = linked_record
                except (ValueError, KeyError, TypeError, OSError) as exc:
                    result["decision"] = {"status": "blocked", "reason": str(exc),
                        "automatic_recommendation": False, "formal_grade_verified": False}
        except (ValueError, KeyError, TypeError, OSError) as exc:
            result.update(status="quote_input_rejected", quote_issues=[{"reason": str(exc)}],
                          calculations=[], quote_records=[],
                          decision={"status": "input_rejected", "automatic_recommendation": False})
    result["evidence"] = list({e["path"]: e for e in result["evidence"]}.values())
    result["comparison_id"] = _digest(result)
    return result


def html_section(result):
    """Visible summary; full matrices, rows, declarations and failures stay in JSON."""
    rows, models = [], []
    def cell(value):
        return html.escape("未知" if value is None else str(value))
    for model in result.get("model_records", []):
        d = model.get("diagnostics", {})
        models.append("<tr>" + "".join("<td>" + cell(v) + "</td>" for v in
            (model["model_key"], model["status"], d.get("expected_goals"),
             d.get("delta_expected_goals"), d.get("delta_probabilities"), model.get("reason"))) + "</tr>")
    for c in result.get("calculations", []):
        values = c.get("result") or {}
        rows.append("<tr>" + "".join("<td>" + cell(v) + "</td>" for v in
            (c["model_key"], c["quote_offer_id"], c["status"], values.get("gross_ev"),
             values.get("net_ev"), c.get("delta_net_ev"), c.get("net_ev_sign_disagreement"))) + "</tr>")
    decision = result.get("decision", {})
    decision_section = ""
    if decision.get("schema_version") == "feature-s2-decision-link-v1":
        core = decision["decision_record"]
        def pre(value):
            return "<pre>" + html.escape(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)) + "</pre>"
        fit_rows = []
        for candidate in core["archive"]["candidates"]:
            fits = candidate.get("formal_fit", {})
            fit_rows.append("<tr>" + "".join("<td>" + cell(v) + "</td>" for v in
                (candidate["candidate_id"], candidate.get("role"),
                 "是" if candidate["candidate_id"] == core.get("primary_candidate_id") else "否",
                 fits.get("single"), fits.get("parlay_leg"), candidate.get("action_status"))) + "</tr>")
        decision_section = ("<h3>实际决定：原生S2记录与双用途核验</h3><p>结构状态：" + cell(decision["status"])
            + "；足球依据、分歧理由仍须人工语义复核。四态和影子等级不证明预测有效或已经成交。</p>"
            + "<table id='feature-decision-fits'><tr><th>候选</th><th>角色</th><th>主选</th><th>单关</th><th>串关腿</th><th>行动</th></tr>"
            + "".join(fit_rows) + "</table><h3>最近替代、比较理由与分歧处置</h3>"
            + pre({"decision_chain": core.get("decision_chain"), "detected_disagreements": decision["model_disagreements"]})
            + "<h3>条件及单关／串腿影子等级</h3>" + pre({"conditions": core["archive"]["conditions"],
                "shadow_grading": core.get("shadow_grading")}) + "<h3>实际决定计算绑定及校验缺件</h3>"
            + pre({"candidate_calculations": decision["candidate_calculations"], "validation": decision["validation"]}))
    return ("<h2>新候选：同合约、同参考价计算</h2><p>状态：" + html.escape(result.get("status", "未知"))
        + "。未提供费用则净EV未知；参考价、当前开放与实际成交分别记录。计算不自动产生推荐等级或替换生产模型。</p>"
        + "<table id='feature-models'><tr><th>模型</th><th>状态</th><th>主客λ</th><th>λ差</th><th>胜平负概率差</th><th>缺失或拒绝原因</th></tr>"
        + "".join(models) + "</table>"
        + "<table id='feature-decisions'><tr><th>模型</th><th>报价</th><th>状态</th><th>费用前EV</th><th>净EV</th><th>净EV差</th><th>正负分歧</th></tr>"
        + "".join(rows) + "</table><p>选择记录：" + html.escape(decision.get("status", "未提供")) + "</p>" + decision_section)
