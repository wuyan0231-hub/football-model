"""One immutable decision, derived views and append-only evidence events.

This module checks declarations and bytes, not football reasoning or source truth.
It never selects a ticket, changes a probability, or promotes a shadow grade.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone
from html import escape
import hashlib
import json
import os
from pathlib import Path
from typing import Any

from .decision_audit import FITS, validate_decision_record
from .evidence import EvidenceError, _no_symlinks, _read_regular, _relative, _time, _quote_interval

SCHEMA = "s2-decision-1.0"
PROTOCOL = "s2-decision-bundle-1.0"
EVENT_PROTOCOL = "s2-decision-event-1.0"
ROLES = {"screenshot", "source", "model", "prediction", "rules", "code"}
CLOCK_FIELDS = {"created_at", "generated_at", "freeze_started_at", "freeze_committed_at",
                "report_generated_at", "publicly_published_at", "user_received_at"}
DOWNSTREAM = {"versions", "body_decision", "summary_decision", "membership", "scoring",
              "main_card", "report_html", "quotes"}


def _now():
    return datetime.now(timezone.utc)


def _iso(value):
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _bytes(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2,
                       allow_nan=False) + "\n").encode("utf-8")


def _sha(blob):
    return hashlib.sha256(blob).hexdigest()


def _hash(value):
    return isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def _issue(issues, code, path, severity="error"):
    item = {"code": code, "path": path, "severity": severity}
    if item not in issues:
        issues.append(item)


def _status(issues):
    return "blocked" if any(i["severity"] == "error" for i in issues) else "incomplete" if issues else "structural_checks_passed"


def _timestamp(value, issues, path, *, optional=False):
    if value is None and optional:
        return None
    try:
        return _time(value)
    except (ValueError, TypeError):
        _issue(issues, "invalid_or_missing_timestamp", path)
        return None


def _rows(value, issues, path):
    if not isinstance(value, list) or any(not isinstance(r, dict) for r in value):
        _issue(issues, "invalid_record_array", path)
        return []
    return value


def _collect_evidence(record, source_root, now, issues):
    """Read only named bytes; preserve late material as research evidence."""
    contents, entries, identities, paths = {}, [], set(), set()
    inputs = _rows(record.get("inputs", []), issues, "inputs")
    derived = _rows(record.get("derived_artifacts", []), issues, "derived_artifacts")
    cutoff = _timestamp(record.get("analysis_cutoff"), issues, "analysis_cutoff")
    kickoff = _timestamp(record.get("kickoff_at"), issues, "kickoff_at")
    if cutoff and cutoff > now:
        _issue(issues, "analysis_cutoff_in_future", "analysis_cutoff")
    exposure = record.get("future_information_exposure")
    if not any(exposure is v for v in (True, False, None)):
        _issue(issues, "invalid_future_information_exposure", "future_information_exposure")
    reasons = []
    if exposure is not False:
        reasons.append("future_information_seen" if exposure is True else "future_information_exposure_unknown")
    if not inputs:
        _issue(issues, "empty_input_allowlist", "inputs", "not_evaluated")
        reasons.append("no_frozen_inputs")
    source_ids = set()
    for group, key, rows in (("inputs", "input_id", inputs), ("derived_artifacts", "artifact_id", derived)):
        for i, item in enumerate(rows):
            path = f"{group}[{i}]"
            identity = item.get(key)
            if not _text(identity) or identity in identities:
                _issue(issues, "missing_or_duplicate_evidence_id", path)
                continue
            identities.add(identity)
            entry = deepcopy(item)
            entry["evidence_id"] = identity
            entry["kind"] = "external_input" if group == "inputs" else "derived"
            if group == "inputs":
                sid = item.get("source_id")
                if not _text(sid) or sid in source_ids:
                    _issue(issues, "missing_or_duplicate_source_id", path)
                source_ids.add(sid) if isinstance(sid, str) else None
                if item.get("role", "source") not in ROLES:
                    _issue(issues, "role_not_allowed_results_belong_to_scoring", path)
                observed = _timestamp(item.get("observed_at"), issues, path + ".observed_at", optional=True)
                published = _timestamp(item.get("published_at"), issues, path + ".published_at", optional=True)
                claimed = _timestamp(item.get("source_claimed_published_at"), issues, path + ".source_claimed_published_at", optional=True)
                if observed is None:
                    _issue(issues, "observation_time_unknown", path, "not_evaluated")
                    reasons.append("observation_time_unknown")
                elif cutoff and observed > cutoff:
                    reasons.append("observed_after_cutoff")
                if published and observed and published > observed:
                    _issue(issues, "publication_after_observation", path)
                if any(t and t > now for t in (observed, published)):
                    _issue(issues, "source_time_in_future", path)
                if published and cutoff and published > cutoff:
                    reasons.append("published_after_cutoff")
                if claimed and (published and claimed != published or observed and claimed > observed):
                    _issue(issues, "claimed_publication_needs_review", path, "not_evaluated")
                entry["evidence_time"] = item.get("observed_at")
            else:
                if item.get("kind") != "computed_from_frozen_inputs" or not _text(item.get("transformation")):
                    _issue(issues, "derived_transform_declaration_required", path)
                if any(field in item for field in ("observed_at", "published_at", "source_id", "source_url")):
                    _issue(issues, "external_input_cannot_be_renamed_derived", path)
                calculated = _timestamp(item.get("calculated_at"), issues, path + ".calculated_at")
                if calculated and calculated > now:
                    _issue(issues, "calculation_time_in_future", path)
                if not isinstance(item.get("parent_ids"), list) or not item["parent_ids"]:
                    _issue(issues, "derived_parents_required", path)
                entry["evidence_time"] = item.get("calculated_at")
            try:
                relative = str(_relative(item.get("path")))
                if relative in paths:
                    raise EvidenceError("duplicate_source_path")
                paths.add(relative)
                blob = _read_regular(source_root, relative)
                if not _hash(item.get("sha256")):
                    _issue(issues, "expected_sha256_required", path)
                elif item["sha256"] != _sha(blob):
                    _issue(issues, "input_sha256_mismatch", path)
                destination = "originals/" + relative
                contents[destination] = blob
                entry.update(path=destination, original_path=relative, sha256=_sha(blob), bytes=len(blob))
            except (OSError, ValueError, TypeError) as exc:
                _issue(issues, str(exc) if isinstance(exc, EvidenceError) else "input_unreadable", path)
                entry["read_status"] = "failed"
            entries.append(entry)
    table = {e["evidence_id"]: e for e in entries}
    visiting, visited = set(), set()

    def walk(identity):
        if identity in visiting:
            _issue(issues, "derived_dependency_cycle", identity)
            return
        if identity in visited:
            return
        visiting.add(identity)
        item = table[identity]
        if item["kind"] == "derived":
            parents = item.get("parent_ids", [])
            if not isinstance(parents, list):
                parents = []
            for parent in parents:
                if not isinstance(parent, str) or parent not in table:
                    _issue(issues, "unknown_derived_parent", identity)
                    continue
                walk(parent)
                parent_time = table[parent].get("evidence_time")
                child_time = item.get("calculated_at")
                if parent_time and child_time:
                    p = _timestamp(parent_time, issues, parent)
                    c = _timestamp(child_time, issues, identity)
                    if p and c and p > c:
                        _issue(issues, "derived_precedes_parent", identity)
        visiting.remove(identity)
        visited.add(identity)
    for identity in table:
        walk(identity)
    if cutoff and kickoff and cutoff >= kickoff:
        reasons.append("analysis_cutoff_not_pre_match")
    if any(i["severity"] == "error" for i in issues):
        reasons.append("invalid_frozen_evidence")
    # Actual forecast IDs must occur in parsed frozen JSON, not only metadata.
    for entry in entries:
        blob = contents.get(entry.get("path"))
        found, bindings = set(), []
        def forecast_ids(value, match_scope=None):
            if isinstance(value, dict):
                scope = value.get("match_id", value.get("fixture_id", match_scope))
                if _text(value.get("forecast_id")):
                    found.add(value["forecast_id"])
                    binding = {"forecast_id": value["forecast_id"], "match_id": scope, "kickoff": value.get("kickoff")}
                    if binding not in bindings:
                        bindings.append(binding)
                for child in value.values():
                    forecast_ids(child, scope)
            elif isinstance(value, list):
                for child in value:
                    forecast_ids(child, match_scope)
        if blob is not None and (entry.get("role") in {"model", "prediction"} or entry.get("artifact_type") == "forecast"):
            try:
                parsed = json.loads(blob)
                forecast_ids(parsed)
                # Native prospective ledger IDs are content hashes, not fields
                # written into their own content-addressed object.
                if (isinstance(parsed, dict) and parsed.get("schema_version") == 1
                        and isinstance(parsed.get("forecast"), dict)
                        and all(parsed.get(key) is not None for key in ("match_id", "home_id", "away_id", "kickoff"))):
                    forecast_id = _sha(blob)
                    found.add(forecast_id)
                    bindings.append({"forecast_id": forecast_id, "match_id": parsed["match_id"],
                                     "kickoff": parsed["kickoff"], "identity_kind": "native_prospective_content_hash"})
            except (ValueError, UnicodeDecodeError, RecursionError):
                pass
        entry["observed_forecast_ids"] = sorted(found)
        entry["observed_forecast_bindings"] = sorted(bindings, key=lambda b: (b["forecast_id"], str(b["match_id"])))
    return entries, contents, reasons


def _compile(record, entries, issues):
    """Normalize once; downstream views only consume this canonical object."""
    from .decision_quotes import adapt_quote_row
    core = deepcopy(record)
    if core.get("schema_version") != SCHEMA:
        _issue(issues, "unsupported_decision_schema", "schema_version")
    for field in ("match_id", "decision_id", "version_id", "policy_id"):
        if not _text(core.get(field)):
            _issue(issues, "required_identity", field)
    for field in sorted(CLOCK_FIELDS | DOWNSTREAM):
        if field in core:
            _issue(issues, "caller_cannot_supply_derived_field", field)
    archive = core.get("archive", {})
    if not isinstance(archive, dict):
        _issue(issues, "archive_object_required", "archive")
        archive = {}
    for field in sorted(CLOCK_FIELDS | DOWNSTREAM):
        if field in archive:
            _issue(issues, "caller_cannot_supply_derived_field", "archive." + field)
    archive = {key: deepcopy(value) for key, value in archive.items() if key not in DOWNSTREAM | CLOCK_FIELDS}
    for field in ("sources", "facts", "contracts", "candidates", "conditions", "model_records", "calculation_records"):
        archive[field] = _rows(archive.get(field, []), issues, "archive." + field)
    archive.update(schema_version="match-archive-1.0", decision_audit_version="decision-audit-1.0", match_id=core.get("match_id"))
    verified_sources = {e.get("source_id"): e for e in entries if e["kind"] == "external_input" and e.get("read_status") != "failed"}
    source_records = {r.get("source_id"): r for r in archive["sources"] if isinstance(r.get("source_id"), str)}
    for sid, entry in verified_sources.items():
        source = source_records.get(sid)
        if source is None:
            source = {"source_id": sid}
            archive["sources"].append(source)
        for field in ("sha256", "observed_at", "published_at"):
            if field in source and source[field] != entry.get(field):
                _issue(issues, "source_metadata_conflict", "archive.sources." + str(sid) + "." + field)
            source[field] = entry.get(field)
        source["frozen_path"] = entry["path"]
    for source in archive["sources"]:
        if source.get("source_id") not in verified_sources:
            _issue(issues, "source_not_frozen", "archive.sources", "not_evaluated")
    contracts = {r.get("contract_id"): r for r in archive["contracts"] if isinstance(r.get("contract_id"), str)}
    adapted_rows, offers, row_aliases = [], [], {}
    for row in _rows(core.get("quote_rows", []), issues, "quote_rows"):
        try:
            adapted = adapt_quote_row(row, match_id=core.get("match_id"), forecast_id=core.get("forecast_id"))
            adapted_rows.append(adapted)
            row_aliases.setdefault(row.get("row_id"), []).append(adapted["quote_row_id"])
            if row.get("source_id") not in verified_sources:
                _issue(issues, "quote_source_not_frozen", "quote_rows", "not_evaluated")
            else:
                source = verified_sources[row["source_id"]]
                row_time = _timestamp(row.get("observed_at"), issues, "quote_rows.observed_at", optional=True)
                source_time = _timestamp(source.get("observed_at"), issues, "inputs.observed_at", optional=True)
                if row_time and source_time and row_time != source_time:
                    _issue(issues, "quote_observation_conflicts_with_frozen_source", "quote_rows")
            cutoff = _timestamp(core.get("analysis_cutoff"), issues, "analysis_cutoff")
            interval = _quote_interval(row)
            if cutoff and interval and interval[1] > cutoff:
                _issue(issues, "quote_not_proven_at_cutoff", "quote_rows", "not_evaluated")
            for claim_field in ("open_evidence", "execution_evidence"):
                claims = row.get(claim_field) or {}
                for claim in claims.values():
                    if not isinstance(claim, dict):
                        continue
                    source = verified_sources.get(claim.get("source_id"))
                    if source is None:
                        _issue(issues, "quote_claim_source_not_frozen", "quote_rows." + claim_field, "not_evaluated")
                        continue
                    observed = _timestamp(claim.get("observed_at"), issues, "quote_rows." + claim_field)
                    actual = _timestamp(source.get("observed_at"), issues, "inputs.observed_at", optional=True)
                    if actual and observed and actual != observed:
                        _issue(issues, "quote_claim_observation_conflict", "quote_rows." + claim_field)
                    if observed and cutoff and observed > cutoff:
                        _issue(issues, "quote_claim_observed_after_cutoff", "quote_rows." + claim_field, "not_evaluated")
            for contract in adapted["contracts"]:
                cid = contract["contract_id"]
                if cid in contracts and contracts[cid] != contract:
                    _issue(issues, "generated_contract_conflict", "archive.contracts")
                if cid not in contracts:
                    archive["contracts"].append(contract)
                    contracts[cid] = contract
            offers.extend(adapted["offers"])
        except (ValueError, TypeError, KeyError) as exc:
            _issue(issues, "quote_adaptation_failed", "quote_rows")
    archive["quotes"] = offers
    archive["quote_id_semantics"] = "offer_id_in_decision_audit_view"
    archive["quote_id_mapping"] = [{"quote_row_id": a["quote_row_id"], "original_row_id": a["original_row"]["row_id"],
                                    "quote_offer_ids": [o["quote_offer_id"] for o in a["offers"]],
                                    "adapter_version": a["quote_adapter_version"]} for a in adapted_rows]
    for candidate in archive["candidates"]:
        cid = candidate.get("candidate_id")
        if candidate.get("version_id", core.get("version_id")) != core.get("version_id"):
            _issue(issues, "cross_version_candidate", str(cid))
        candidate["version_id"] = core.get("version_id")
        formal = candidate.get("formal_fit")
        if not isinstance(formal, dict):
            formal = {"single": candidate.get("fit"), "parlay_leg": None}
            _issue(issues, "formal_usage_fits_required", str(cid), "not_evaluated")
        for usage in ("single", "parlay_leg"):
            if formal.get(usage) is None:
                _issue(issues, "formal_fit_unknown", str(cid) + "." + usage, "not_evaluated")
            elif not isinstance(formal[usage], str) or formal[usage] not in FITS:
                _issue(issues, "invalid_formal_fit", str(cid) + "." + usage)
        if "fit" in candidate and candidate["fit"] != formal.get("single"):
            _issue(issues, "duplicate_formal_fit_conflict", str(cid))
        candidate["formal_fit"] = formal
        scoped_conditions = candidate.get("formal_condition_ids")
        if scoped_conditions is not None:
            if not isinstance(scoped_conditions, dict) or set(scoped_conditions) != {"single", "parlay_leg"}:
                _issue(issues, "formal_condition_scope_required", str(cid))
            else:
                for usage, condition_ids in scoped_conditions.items():
                    if not isinstance(condition_ids, list) or any(not isinstance(cond, str) for cond in condition_ids):
                        _issue(issues, "invalid_formal_condition_ids", str(cid) + "." + usage)
        candidate["fit"] = formal.get("single")
        rowid = candidate.get("quote_row_id")
        if rowid is not None:
            if isinstance(rowid, str) and rowid in row_aliases:
                if len(row_aliases[rowid]) == 1:
                    candidate["original_quote_row_id"] = rowid
                    rowid = row_aliases[rowid][0]
                    candidate["quote_row_id"] = rowid
                else:
                    _issue(issues, "ambiguous_original_row_id", str(cid))
            possible = [o for o in offers if o.get("quote_row_id") == rowid and o.get("selection") == candidate.get("selection")]
            if len(possible) != 1:
                _issue(issues, "candidate_quote_selection_not_unique", str(cid))
            else:
                offer = possible[0]
                for field in ("quote_id", "quote_offer_id", "contract_id"):
                    expected = offer.get(field)
                    if candidate.get(field) is not None and candidate[field] != expected:
                        _issue(issues, "candidate_quote_binding_conflict", str(cid) + "." + field)
                    candidate[field] = expected
        elif candidate.get("quote_id") is not None or candidate.get("quote_offer_id") is not None:
            _issue(issues, "quoted_candidate_requires_original_row", str(cid))
        else:
            candidate["quote_id"] = None
            candidate["quote_offer_id"] = None
    primary_id = core.get("primary_candidate_id")
    candidates = {c.get("candidate_id"): c for c in archive["candidates"] if isinstance(c.get("candidate_id"), str)}
    if primary_id is None:
        if not _text(core.get("no_primary_reason")):
            _issue(issues, "no_primary_reason_required", "primary_candidate_id", "not_evaluated")
    elif not isinstance(primary_id, str) or primary_id not in candidates:
        _issue(issues, "unknown_primary_candidate", "primary_candidate_id")
    primary = candidates.get(primary_id) if isinstance(primary_id, str) else None
    chain = core.get("decision_chain")
    if primary:
        if not isinstance(chain, dict):
            _issue(issues, "decision_chain_required", "decision_chain", "not_evaluated")
        else:
            alternative = chain.get("strongest_alternative_candidate_id")
            if alternative is None:
                if not _text(chain.get("no_alternative_reason")):
                    _issue(issues, "strongest_alternative_required", "decision_chain", "not_evaluated")
            elif not isinstance(alternative, str) or alternative == primary_id or alternative not in candidates:
                _issue(issues, "invalid_strongest_alternative", "decision_chain")
            if not _text(chain.get("alternative_reason")):
                _issue(issues, "alternative_reason_required", "decision_chain", "not_evaluated")
    forecast = core.get("forecast_id")
    if forecast is None:
        if not _text(core.get("forecast_missing_reason")):
            _issue(issues, "forecast_missing_reason_required", "forecast_id", "not_evaluated")
    elif not _text(forecast):
        _issue(issues, "invalid_forecast_id", "forecast_id")
    else:
        linked = [m for m in archive["model_records"] if m.get("forecast_id") == forecast]
        forecast_evidence = {}
        for entry in entries:
            if entry.get("role") in {"model", "prediction"} or (entry["kind"] == "derived" and entry.get("artifact_type") == "forecast"):
                forecast_evidence[entry["evidence_id"]] = entry
                if entry.get("source_id"):
                    forecast_evidence[entry["source_id"]] = entry
        valid_binding = False
        for model in linked:
            refs = model.get("forecast_evidence_ids")
            if not isinstance(refs, list) or not refs:
                continue
            if any(not isinstance(ref, str) or ref not in forecast_evidence for ref in refs):
                _issue(issues, "forecast_reference_is_not_frozen_prediction", "forecast_id")
                continue
            for ref in refs:
                for binding in forecast_evidence[ref]["observed_forecast_bindings"]:
                    if binding["forecast_id"] != forecast:
                        continue
                    if binding["match_id"] is None:
                        _issue(issues, "forecast_match_scope_unknown", "forecast_id", "not_evaluated")
                    elif str(binding["match_id"]) != str(core.get("match_id")):
                        _issue(issues, "adopted_forecast_match_mismatch", "forecast_id")
                    else:
                        recorded_kickoff = binding.get("kickoff")
                        if recorded_kickoff is not None and _timestamp(recorded_kickoff, issues, "forecast.kickoff") != _timestamp(core.get("kickoff_at"), issues, "kickoff_at"):
                            _issue(issues, "adopted_forecast_kickoff_mismatch", "forecast_id")
                        else:
                            valid_binding = True
        if not linked or not valid_binding:
            _issue(issues, "adopted_forecast_evidence_missing", "forecast_id", "not_evaluated")
    core["archive"] = archive
    core["adapted_quote_rows"] = adapted_rows
    core.pop("versions", None)
    return core


def _audit_views(core):
    result = {}
    archive = core["archive"]
    for usage in ("single", "parlay_leg"):
        view = deepcopy(archive)
        for candidate in view["candidates"]:
            candidate["fit"] = candidate["formal_fit"].get(usage)
            if isinstance(candidate.get("formal_condition_ids"), dict):
                common = candidate.get("condition_ids", [])
                scoped = candidate["formal_condition_ids"].get(usage, [])
                if isinstance(common, list) and isinstance(scoped, list) and all(isinstance(c, str) for c in common + scoped):
                    candidate["condition_ids"] = list(dict.fromkeys(common + scoped))
        primary = next((c for c in view["candidates"] if c.get("candidate_id") == core.get("primary_candidate_id")), None)
        decision = ({"candidate_id": primary.get("candidate_id"), "fit": primary.get("fit"),
                     "contract_id": primary.get("contract_id"), "quote_id": primary.get("quote_id"),
                     "condition_ids": primary.get("condition_ids"),
                     "value_status": primary["value"].get("status") if isinstance(primary.get("value"), dict) else None} if primary else None)
        view["versions"] = [{"version_id": core.get("version_id"), "primary_candidate_id": core.get("primary_candidate_id"),
                             "no_primary_reason": core.get("no_primary_reason"),
                             "summary_decision": deepcopy(decision), "body_decision": deepcopy(decision)}]
        result[usage] = view
    return result


def _validation(core, issues):
    audits = {}
    for usage, view in _audit_views(core).items():
        audit = validate_decision_record(view)
        for item in audit["issues"]:
            item = deepcopy(item)
            # Preserve a football fit without inventing an offer. The legacy
            # validator conflates absent price with a contradicted fit.
            if item["code"] == "unsuitable_quote":
                cid = item["path"].removeprefix("candidates[").removesuffix("]")
                candidate = next((c for c in view["candidates"] if c.get("candidate_id") == cid), {})
                if candidate.get("quote_id") is None:
                    item.update(code="price_absent_fit_retained", severity="not_evaluated")
            _issue(issues, item["code"], usage + ":" + item["path"], item["severity"])
        audits[usage] = audit
    shadow = core.get("shadow_grading")
    shadow_audit = None
    if shadow is not None:
        from .shadow_grading import validate_shadow_grading
        primary = next((c for c in core["archive"]["candidates"] if c.get("candidate_id") == core.get("primary_candidate_id")), None)
        evidence_ids = {e.get("input_id") for e in core.get("inputs", [])} | {e.get("artifact_id") for e in core.get("derived_artifacts", [])}
        evidence_ids |= {s.get("source_id") for s in core["archive"]["sources"]} | {f.get("fact_id") for f in core["archive"]["facts"]}
        shadow_candidate = {**primary, "decision_id": core.get("decision_id")} if primary else None
        shadow_audit = validate_shadow_grading(shadow, candidate=shadow_candidate, conditions=core["archive"]["conditions"], evidence_ids=evidence_ids)
        for item in shadow_audit.get("issues", []):
            _issue(issues, item.get("code", "shadow_invalid"), "shadow_grading:" + item.get("path", ""), item.get("severity", "error"))
    issues.sort(key=lambda i: (i["path"], i["code"], i["severity"]))
    return {"status": _status(issues), "issues": issues, "legacy_audits_by_usage": audits,
            "shadow_audit": shadow_audit, "manual_semantic_review_required": True,
            "publishable": False if _status(issues) == "blocked" else None,
            "predictive_quality_verified": False, "economic_advantage_verified": False}


def _views(core, validation, eligibility):
    archive = core["archive"]
    primary = next((c for c in archive["candidates"] if c.get("candidate_id") == core.get("primary_candidate_id")), None)
    quotes = {q.get("quote_id"): q for q in archive["quotes"]}
    contracts = {c.get("contract_id"): c for c in archive["contracts"]}
    card = {"match_id": core.get("match_id"), "decision_id": core.get("decision_id"), "version_id": core.get("version_id"),
            "policy_id": core.get("policy_id"), "forecast_id": core.get("forecast_id"),
            "forecast_missing_reason": core.get("forecast_missing_reason"),
            "primary_candidate": deepcopy(primary), "no_primary_reason": core.get("no_primary_reason"),
            "contract": deepcopy(contracts.get(primary.get("contract_id"))) if primary else None,
            "quote": deepcopy(quotes.get(primary.get("quote_id"))) if primary else None,
            "decision_chain": deepcopy(core.get("decision_chain")), "conditions": deepcopy(archive["conditions"]),
            "shadow_grading": deepcopy(core.get("shadow_grading")), "structural_status": validation["status"]}
    membership = {"decision_id": core.get("decision_id"), "version_id": core.get("version_id"),
                  "candidate_id": primary.get("candidate_id") if primary else None,
                  "formal_fit": deepcopy(primary.get("formal_fit")) if primary else None,
                  "formal_condition_ids": deepcopy(primary.get("formal_condition_ids")) if primary else None,
                  "shadow_grading": deepcopy(core.get("shadow_grading")),
                  "whole_parlay_assessed": False, "execution_status": "unknown", "executed": None}
    scoring = {"match_id": core.get("match_id"), "decision_id": core.get("decision_id"), "version_id": core.get("version_id"),
               "policy_id": core.get("policy_id"), "forecast_id": core.get("forecast_id"),
            "forecast_missing_reason": core.get("forecast_missing_reason"),
               "candidate_id": primary.get("candidate_id") if primary else None,
               "contract_id": primary.get("contract_id") if primary else None,
               "quote_row_id": primary.get("quote_row_id") if primary else None,
               "quote_offer_id": primary.get("quote_offer_id") if primary else None,
               "analysis_cutoff": core.get("analysis_cutoff"), "qualification": eligibility["qualification"],
               "decision_structural_status": validation["status"], "quote_protocol": "UNASSIGNED",
               "realized_return": None, "result": None, "execution_status": "unknown"}
    return {"main_card": card, "membership": membership, "scoring": scoring,
            "audit_archives": _audit_views(core)}


def _html(core, views, validation, eligibility, times):
    def pre(value):
        return "<pre>" + escape(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)) + "</pre>"
    body = ["<!doctype html><html lang='zh-CN'><meta charset='utf-8'><title>决定正文与核验视图</title>",
            "<style>body{font-family:system-ui;max-width:1100px;margin:32px auto;padding:16px;line-height:1.6}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f3f5f7;padding:16px}h2{margin-top:32px}</style>",
            "<h1>决定正文与核验视图</h1><p>完整比赛报告和累计ZIP由上层归档流程编排；本视图覆盖关键决定部分。结构检查与同源渲染不证明足球依据充分、预测正确或已成交。影子等级不改正式四态。</p>",
            "<h2>主卡</h2>", pre(views["main_card"]), "<h2>完整候选：机制、支付区域与反证处理</h2>", pre(core["archive"]["candidates"]),
            "<h2>最强备选与比较理由</h2>", pre(core.get("decision_chain")), "<h2>条件与后续动作</h2>", pre(core["archive"]["conditions"]),
            "<h2>完整报价原行与单票映射</h2>", pre(core["adapted_quote_rows"]), "<h2>模型与实际计算记录</h2>", pre({k:core["archive"][k] for k in ("model_records","calculation_records")}),
            "<h2>来源与事实</h2>", pre({k:core["archive"][k] for k in ("sources","facts")}),
            "<h2>证据、时间资格和诊断</h2>", pre({"times":{k:v for k,v in times.items() if k != "report_generated_at"},"report_completion_record":"times.json/report_generated_at","eligibility":eligibility,"validation":validation}), "</html>"]
    return "".join(body).encode("utf-8")


def _write_bundle(output, contents, protocol):
    output.mkdir(parents=True, exist_ok=False)
    for relative, blob in contents.items():
        dest = output / _relative(relative)
        dest.parent.mkdir(parents=True, exist_ok=True)
        with dest.open("xb") as stream:
            stream.write(blob)
    manifest = {"protocol": protocol, "files": [{"path": path, "sha256": _sha(blob), "bytes":len(blob)} for path, blob in sorted(contents.items())]}
    blob = _bytes(manifest)
    with (output / "manifest.json").open("xb") as stream:
        stream.write(blob)
    return _sha(blob)


def build_decision_bundle(record, output_dir, *, source_root):
    """Create a new diagnostic or review bundle; never overwrite inputs or outputs."""
    if not isinstance(record, dict):
        raise EvidenceError("decision_object_required")
    output = _no_symlinks(output_dir)
    if output.exists():
        raise EvidenceError("destination_exists_no_overwrite")
    root = _no_symlinks(source_root)
    source_record = deepcopy(record)
    original_bytes = _bytes(source_record)  # rejects non-JSON and NaN
    started = _now()
    issues = []
    entries, originals, reasons = _collect_evidence(source_record, root, started, issues)
    core = _compile(source_record, entries, issues)
    validation = _validation(core, issues)
    if any(i["code"] in {"quote_not_proven_at_cutoff", "quote_claim_observed_after_cutoff", "quote_observation_conflicts_with_frozen_source", "quote_claim_observation_conflict", "source_not_frozen", "quote_source_not_frozen", "quote_claim_source_not_frozen", "adopted_forecast_evidence_missing", "forecast_reference_is_not_frozen_prediction", "forecast_match_scope_unknown", "adopted_forecast_match_mismatch", "adopted_forecast_kickoff_mismatch"} for i in issues):
        reasons.append("decision_evidence_not_fully_frozen_at_cutoff")
    # Committed marks completion of actual byte copying, not supplied as-of.
    output.mkdir(parents=True, exist_ok=False)
    for path, blob in originals.items():
        dest = output / path
        dest.parent.mkdir(parents=True, exist_ok=True)
        with dest.open("xb") as stream:
            stream.write(blob)
    committed = _now()
    kickoff = _timestamp(source_record.get("kickoff_at"), issues, "kickoff_at")
    cutoff = _timestamp(source_record.get("analysis_cutoff"), issues, "analysis_cutoff")
    if kickoff and committed >= kickoff:
        reasons.append("frozen_after_kickoff")
    eligibility = {"qualification": "research_only" if reasons else "prospective_input_candidate",
                   "reasons": sorted(set(reasons)), "historical_blindness_proven":False,
                   "prediction_validated":False,"artifact_existed_at_analysis_cutoff": bool(cutoff and committed <= cutoff)}
    validation["status"] = _status(issues)
    views = _views(core, validation, eligibility)
    times = {"freeze_started_at":_iso(started),"freeze_committed_at":_iso(committed),
             "report_generated_at":None,"publicly_published_at":None,"user_received_at":None}
    contents = {**originals, "input_record.json": original_bytes, "decision.json": _bytes(core), "evidence.json":_bytes(entries),
                "validation.json":_bytes(validation), "eligibility.json":_bytes(eligibility),"times.json":_bytes(times)}
    contents.update({name+".json":_bytes(value) for name,value in views.items()})
    contents["report.html"] = _html(core, views, validation, eligibility, times)
    # Originals already copied; every other file uses exclusive creation too.
    for path, blob in contents.items():
        if path in originals or path == "times.json":
            continue
        with (output / path).open("xb") as stream:
            stream.write(blob)
    times["report_generated_at"] = _iso(_now())
    contents["times.json"] = _bytes(times)
    with (output / "times.json").open("xb") as stream:
        stream.write(contents["times.json"])
    manifest = {"protocol":PROTOCOL,"files":[{"path":path,"sha256":_sha(blob),"bytes":len(blob)} for path,blob in sorted(contents.items())]}
    manifest_bytes = _bytes(manifest)
    with (output/"manifest.json").open("xb") as stream:
        stream.write(manifest_bytes)
    return {"status":validation["status"],"output_dir":str(output),"manifest_sha256":_sha(manifest_bytes),
            "qualification":eligibility["qualification"],"times":times,"issues":issues,
            "manual_semantic_review_required":True,"publishable":validation["publishable"]}


def _read_manifest(bundle_dir, expected_manifest_sha256, protocol):
    root = _no_symlinks(bundle_dir)
    manifest_bytes = _read_regular(root,"manifest.json")
    digest = _sha(manifest_bytes)
    if expected_manifest_sha256 is not None and expected_manifest_sha256 != digest:
        raise EvidenceError("manifest_anchor_mismatch")
    manifest = json.loads(manifest_bytes)
    if not isinstance(manifest,dict) or manifest.get("protocol") != protocol or not isinstance(manifest.get("files"),list):
        raise EvidenceError("unsupported_manifest")
    contents = {}
    for item in manifest["files"]:
        path = str(_relative(item.get("path")))
        if path in contents or path == "manifest.json":
            raise EvidenceError("duplicate_manifest_path")
        blob = _read_regular(root,path)
        if item.get("bytes") != len(blob) or item.get("sha256") != _sha(blob):
            raise EvidenceError("file_hash_or_length_mismatch")
        contents[path] = blob
    actual = set()
    for folder, dirs, files in os.walk(root, followlinks=False):
        for name in dirs+files:
            _no_symlinks(Path(folder)/name)
        actual.update(str((Path(folder)/name).relative_to(root)) for name in files)
    if actual != set(contents)|{"manifest.json"}:
        raise EvidenceError("unlisted_or_missing_file")
    return contents, digest


def verify_decision_bundle(bundle_dir, expected_manifest_sha256=None):
    contents,digest = _read_manifest(bundle_dir,expected_manifest_sha256,PROTOCOL)
    try:
        core, validation, eligibility, times = [json.loads(contents[n+".json"]) for n in ("decision","validation","eligibility","times")]
        original = json.loads(contents["input_record.json"])
        entries = json.loads(contents["evidence.json"])
        ordered = [_time(times[k]) for k in ("freeze_started_at","freeze_committed_at","report_generated_at")]
        if ordered != sorted(ordered) or ordered[-1] > _now() or times.get("publicly_published_at") is not None or times.get("user_received_at") is not None:
            raise EvidenceError("invalid_program_clock_record")
        issues = []
        replay_entries, _, reasons = _collect_evidence(original, _no_symlinks(bundle_dir) / "originals", ordered[0], issues)
        if entries != replay_entries:
            raise EvidenceError("evidence_binding_mismatch")
        expected = _compile(original,replay_entries,issues)
        if core != expected:
            raise EvidenceError("canonical_decision_mismatch")
        replay_validation = _validation(expected,issues)
        if any(i["code"] in {"quote_not_proven_at_cutoff", "quote_claim_observed_after_cutoff", "quote_observation_conflicts_with_frozen_source", "quote_claim_observation_conflict", "source_not_frozen", "quote_source_not_frozen", "quote_claim_source_not_frozen", "adopted_forecast_evidence_missing", "forecast_reference_is_not_frozen_prediction", "forecast_match_scope_unknown", "adopted_forecast_match_mismatch", "adopted_forecast_kickoff_mismatch"} for i in issues):
            reasons.append("decision_evidence_not_fully_frozen_at_cutoff")
        if validation != replay_validation:
            raise EvidenceError("validation_replay_mismatch")
        kickoff = _timestamp(original.get("kickoff_at"), issues, "kickoff_at")
        cutoff = _timestamp(original.get("analysis_cutoff"), issues, "analysis_cutoff")
        if kickoff and ordered[1] >= kickoff:
            reasons.append("frozen_after_kickoff")
        replay_eligibility = {"qualification": "research_only" if reasons else "prospective_input_candidate",
                            "reasons": sorted(set(reasons)), "historical_blindness_proven": False,
                            "prediction_validated": False, "artifact_existed_at_analysis_cutoff": bool(cutoff and ordered[1] <= cutoff)}
        if eligibility != replay_eligibility:
            raise EvidenceError("eligibility_replay_mismatch")
        views = _views(core,validation,eligibility)
        if any(contents.get(name+".json") != _bytes(value) for name,value in views.items()):
            raise EvidenceError("derived_view_mismatch")
        if contents.get("report.html") != _html(core,views,validation,eligibility,times):
            raise EvidenceError("rendered_report_mismatch")
    except (KeyError,TypeError,ValueError) as exc:
        if isinstance(exc,EvidenceError):
            raise
        raise EvidenceError("malformed_decision_bundle") from None
    return {"status":"verified","manifest_sha256":digest,"externally_anchored":expected_manifest_sha256 is not None,
            "decision_status":validation["status"],"qualification":eligibility["qualification"],
            "historical_blindness_proven":False,"manual_semantic_review_required":True}


def append_decision_event(bundle_dir,event,events_dir,*,source_root):
    """Append evidence; conditions never revise the original decision or grade."""
    verified = verify_decision_bundle(bundle_dir)
    contents,_ = _read_manifest(bundle_dir,verified["manifest_sha256"],PROTOCOL)
    base = json.loads(contents["decision.json"])
    times = json.loads(contents["times.json"])
    if not isinstance(event,dict):
        raise EvidenceError("event_object_required")
    event = deepcopy(event)
    if any(k in event for k in CLOCK_FIELDS | {"recorded_at","base_manifest_sha256","effect"}):
        raise EvidenceError("caller_cannot_supply_event_derived_fields")
    identity = event.get("event_id")
    if not _text(identity) or str(_relative(identity)) != identity or "/" in identity:
        raise EvidenceError("safe_event_id_required")
    event_type = event.get("event_type")
    if event_type not in {"publication","user_received","execution","condition_changed"}:
        raise EvidenceError("unsupported_event_type")
    now = _now()
    occurred = _time(event["occurred_at"]) if event.get("occurred_at") is not None else None
    if occurred is not None and (occurred > now or occurred < _time(times["report_generated_at"])):
        raise EvidenceError("event_time_outside_real_bounds")
    for key in ("decision_id","version_id","match_id"):
        if event.get(key) != base.get(key):
            raise EvidenceError("event_decision_binding_mismatch")
    evidence = event.get("evidence")
    if not isinstance(evidence,list) or not evidence:
        raise EvidenceError("event_evidence_required")
    blobs, saved = {}, []
    paths = set()
    for item in evidence:
        relative = str(_relative(item.get("path")))
        if relative in paths:
            raise EvidenceError("duplicate_event_evidence")
        paths.add(relative)
        blob = _read_regular(source_root,relative)
        if not _hash(item.get("sha256")) or item["sha256"] != _sha(blob):
            raise EvidenceError("event_evidence_hash_mismatch")
        observed = _time(item.get("observed_at"))
        if observed > now or observed < _time(times["report_generated_at"]) or occurred is not None and observed < occurred:
            raise EvidenceError("event_evidence_time_invalid")
        destination="evidence/"+relative
        blobs[destination]=blob
        saved.append({**item,"path":destination,"bytes":len(blob)})
    if event_type == "condition_changed":
        condition_ids = {c.get("condition_id") for c in base["archive"]["conditions"]}
        if event.get("condition_id") not in condition_ids or event.get("state") not in {"satisfied","not_satisfied","unknown"}:
            raise EvidenceError("invalid_condition_event")
        event["effect"]="reevaluation_required_new_decision_version_no_automatic_grade_promotion"
    elif event_type == "execution":
        candidates = {c.get("candidate_id"):c for c in base["archive"]["candidates"]}
        candidate = candidates.get(event.get("candidate_id"))
        if not candidate or event.get("quote_offer_id") != candidate.get("quote_offer_id") or candidate.get("quote_offer_id") is None:
            raise EvidenceError("execution_offer_binding_required")
        if event.get("status") not in {"accepted","rejected","cancelled","unknown"}:
            raise EvidenceError("execution_status_required")
        if event["status"] == "accepted":
            price=event.get("accepted_decimal_odds")
            user_confirmation = event.get("execution_basis") == "user_confirmation" and any(item.get("evidence_type") == "user_confirmation" for item in evidence)
            if user_confirmation:
                if price is not None and (not isinstance(price,(int,float)) or isinstance(price,bool) or not 1 < price < float("inf")):
                    raise EvidenceError("invalid_user_confirmed_execution_price")
            elif not isinstance(price,(int,float)) or isinstance(price,bool) or not 1 < price < float("inf") or not _text(event.get("transaction_reference")):
                raise EvidenceError("accepted_execution_receipt_required")
        event["effect"]="evidence_declaration_only_semantic_receipt_review_required"
    else:
        event["effect"]="publication_or_receipt_claim_separate_from_report_generation"
    event.update(execution_details_verified=False, timing_status="unknown" if occurred is None else "declared_with_evidence", schema_version=EVENT_PROTOCOL,base_manifest_sha256=verified["manifest_sha256"],recorded_at=_iso(_now()),evidence=saved)
    blobs["event.json"]=_bytes(event)
    output=_no_symlinks(events_dir)/identity
    if output.exists():
        raise EvidenceError("event_exists_no_overwrite")
    digest=_write_bundle(output,blobs,EVENT_PROTOCOL)
    return {"status":"event_appended","event_id":identity,"manifest_sha256":digest,"effect":event["effect"],"output_dir":str(output)}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    commands=parser.add_subparsers(dest="command",required=True)
    build=commands.add_parser("build")
    build.add_argument("--record",required=True)
    build.add_argument("--source-root",required=True)
    build.add_argument("--output",required=True)
    verify=commands.add_parser("verify")
    verify.add_argument("--bundle",required=True)
    verify.add_argument("--manifest-sha256")
    append=commands.add_parser("append-event")
    append.add_argument("--bundle",required=True)
    append.add_argument("--event",required=True)
    append.add_argument("--source-root",required=True)
    append.add_argument("--events-dir",required=True)
    args=parser.parse_args(argv)
    def read(path):
        path=_no_symlinks(path)
        return json.loads(_read_regular(path.parent,path.name))
    try:
        if args.command=="build":
            result=build_decision_bundle(read(args.record),args.output,source_root=args.source_root)
        elif args.command=="verify":
            result=verify_decision_bundle(args.bundle,args.manifest_sha256)
        else:
            result=append_decision_event(args.bundle,read(args.event),args.events_dir,source_root=args.source_root)
    except (ValueError,TypeError,KeyError,OSError) as exc:
        result={"status":"rejected","reason":str(exc) if isinstance(exc,EvidenceError) else "malformed_or_unreadable_input"}
    print(_bytes(result).decode(),end="")
    return 0 if result["status"] in {"structural_checks_passed","verified","event_appended"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
