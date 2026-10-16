#!/usr/bin/env python3
"""Explicit current official-DOM reference extension, never historical admission.

The original integration and historical selector remain unchanged. This runner
reverifies the saved models at the actual wall clock, preserves the historical
selector's refusal of unknown change times, then projects reviewed current
snapshot top rows through the existing quote adapter and settlement functions.
No collection, fitting, automatic choice, open-market claim or trade occurs.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
from pathlib import Path
import re
import sys
from urllib.parse import urlsplit

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from football_model import integration
from football_model.advanced_research import implementation_fingerprint
from football_model.decision_quotes import adapt_quote_row
from football_model.evidence import _time, select_quotes
from football_model.feature_decisions import (_digest, _file_ref, _read_ref,
    _decision_record_link, project_offer)
from football_model.settlement import STATES, matrix_array

VERSION = "current-observation-decision-v1"
READING_KIND = "official_native_rendered_web_whole_rows_not_search_snippet_or_API"
SEQUENCES = {"1x2": "native_web_match_1x2",
    "asian_handicap": "native_web_match_handicap", "totals": "native_web_match_total"}
HEADINGS = {"1x2": "Money Line – Match", "asian_handicap": "Handicap – Match",
    "totals": "Total – Match"}


def strict_json(raw):
    value = json.loads(raw, parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite_json")))
    _digest(value)  # Also rejects numeric exponent overflow, not just NaN tokens.
    return value


def official_url(value, fixture):
    if not isinstance(value, str):
        raise ValueError("official_dom_url_required")
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or parsed.hostname not in {"pinnacle.com", "www.pinnacle.com"}
            or parsed.username is not None or parsed.password is not None
            or parsed.port not in {None, 443} or parsed.query
            or not parsed.path.startswith("/en/soccer/")):
        raise ValueError("official_dom_url_scope_required")
    slug = parsed.path.lower()
    for name in (fixture["home_name"], fixture["away_name"]):
        normalized = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
        if not normalized or normalized not in slug:
            raise ValueError("official_dom_url_fixture_mismatch")
    return value


def dom_row_binding(raw, row, fixture):
    """Narrow, explicit public Pinnacle whole-row transcript grammar.

    This checks the archived transcript against the supplied numeric row; it
    does not authenticate a website, screenshot, or a caller's human review.
    Unsupported page layouts fail rather than silently loosen the binding.
    """
    text = raw.decode("utf-8")
    tokens = [p.strip() for line in text.splitlines() for p in line.split("\t") if p.strip()]
    heading = HEADINGS[row["market"]]
    if tokens.count(heading) != 1 or fixture["home_name"] not in tokens or fixture["away_name"] not in tokens:
        raise ValueError("official_dom_whole_row_fixture_or_market_missing")
    start = tokens.index(heading) + 1
    values = []
    for token in tokens[start:]:
        if token in HEADINGS.values() or token == "See more":
            break
        if token != "Show All":
            values.append(token)
    def numeric(a, b):
        try:
            left, right = Decimal(str(a)), Decimal(str(b))
            return left.is_finite() and right.is_finite() and left == right
        except InvalidOperation:
            return False
    odds, market = row["odds"], row["market"]
    home, away = fixture["home_name"], fixture["away_name"]
    if market == "1x2":
        valid = (len(values) == 6 and values[0::2] == [home, "Draw", away]
            and all(numeric(values[i], odds[k]) for i, k in ((1, "home"), (3, "draw"), (5, "away"))))
    elif market == "asian_handicap":
        valid = (len(values) == 6 and values[:2] == [home, away]
            and numeric(values[2], row["line"]) and numeric(values[4], -row["line"])
            and numeric(values[3], odds["home"]) and numeric(values[5], odds["away"]))
        coordinates = row.get("raw_team_coordinates")
        valid = (valid and isinstance(coordinates, dict)
            and numeric(coordinates.get("home"), row["line"])
            and numeric(coordinates.get("away"), -row["line"])
            and row.get("line_mapping_status") == "reviewed_native_web_explicit_home_and_away_opposite_lines")
    else:
        valid = (len(values) == 4 and values[0].startswith("Over ") and values[2].startswith("Under ")
            and numeric(values[0][5:], row["line"]) and numeric(values[2][6:], row["line"])
            and numeric(values[1], odds["over"]) and numeric(values[3], odds["under"]))
    if not valid:
        raise ValueError("official_dom_whole_row_values_mismatch")


def fixture_identity(raw, fixture):
    """Match the full archived rendered DOM's real page zone and kickoff.

    The page clock is used only to read its displayed GMT offset. It is never
    treated as a quote-change time or inserted into any quote row.
    """
    text = raw.decode("utf-8")
    zones = set(re.findall(r"\b\d{2}:\d{2}:\d{2} \(GMT ([+-]\d{2}:\d{2})\)", text))
    dates = set(re.findall(r"(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday), "
        r"(?:January|February|March|April|May|June|July|August|September|October|November|December) "
        r"\d{1,2}, \d{4} at \d{2}:\d{2}", text))
    if len(zones) != 1 or len(dates) != 1:
        raise ValueError("official_dom_unique_page_timezone_and_kickoff_required")
    zone, displayed = next(iter(zones)), next(iter(dates))
    hours, minutes = map(int, zone[1:].split(":"))
    if hours > 14 or minutes > 59 or (hours == 14 and minutes):
        raise ValueError("official_dom_page_timezone_invalid")
    delta = timedelta(hours=hours, minutes=minutes) * (1 if zone[0] == "+" else -1)
    kickoff = datetime.strptime(displayed, "%A, %B %d, %Y at %H:%M").replace(tzinfo=timezone(delta))
    if kickoff.astimezone(timezone.utc) != _time(fixture["kickoff"]):
        raise ValueError("official_dom_fixture_kickoff_mismatch")
    if (fixture["home_name"] + " vs. " + fixture["away_name"]) not in text:
        raise ValueError("official_dom_fixture_title_mismatch")
    return {"displayed_kickoff": displayed, "displayed_timezone": "GMT " + zone,
        "normalized_kickoff": kickoff.astimezone(timezone.utc).isoformat(),
        "page_clock_is_quote_change_time": False}


def load_current_quotes(runtime, input_path, fixture, now):
    runtime = Path(runtime)
    path = Path(input_path)
    if not path.is_absolute():
        path = runtime / path
    raw, input_ref = _read_ref(runtime, _file_ref(runtime, path))
    value = strict_json(raw)
    schema = value.get("schema_version")
    explicit = (schema == "current-observation-decision-input-v1"
        and value.get("observation_mode") == "current_observation" and value.get("usage") == "reference_only")
    original_r2 = schema == "feature-decision-input-v1" and value.get("usage") == "diagnostic_reference_only"
    if not (explicit or original_r2):
        raise ValueError("explicit_current_reference_input_required")
    if value.get("staging") is not None:
        raise ValueError("current_extension_requires_direct_verified_originals")
    if isinstance(value.get("match_id"), bool) or str(value.get("match_id")) != str(fixture["match_id"]):
        raise ValueError("current_input_match_mismatch")
    declared = _time(value["analysis_as_of"]) if value.get("analysis_as_of") is not None else None
    if declared is not None and declared > now:
        raise ValueError("input_declared_analysis_in_future")
    sources, originals = {}, {input_ref["path"]: raw}
    refs = [input_ref]
    if not isinstance(value.get("sources"), list) or not value["sources"]:
        raise ValueError("reviewed_sources_required")
    for source in value["sources"]:
        if not isinstance(source, dict) or not isinstance(source.get("source_id"), str) or not source["source_id"]:
            raise ValueError("source_identity_required")
        sid = source["source_id"]
        if sid in sources:
            raise ValueError("duplicate_source_id")
        source_raw, ref = _read_ref(runtime, source)
        observed, reviewed = _time(source.get("observed_at")), _time(source.get("reviewed_at"))
        if source.get("reviewed") is not True or not observed <= reviewed <= now:
            raise ValueError("source_review_or_actual_time_invalid")
        if declared is not None and observed > declared:
            raise ValueError("source_observed_after_input_declaration")
        kind = source.get("source_kind", "official_dom")
        if kind in {"official_dom", "official_dom_identity_snapshot", "official_native_public_web_dom_identity_timezone_excerpt"}:
            official_url(source.get("source_url"), fixture)
            if (not ref["path"].startswith("feature_decisions/sources/")
                    or Path(ref["path"]).suffix != ".txt"):
                raise ValueError("reviewed_official_dom_scope_required")
            if kind == "official_dom" and (source.get("reading_kind") != READING_KIND
                    or source.get("quote_role") != "QUOTE_REF"):
                raise ValueError("reviewed_official_dom_scope_required")
            source_raw.decode("utf-8")
        elif kind == "reviewed_analyst_declaration":
            strict_json(source_raw)
        else:
            raise ValueError("unsupported_current_source_kind")
        sources[sid] = {"metadata": source, "ref": ref, "raw": source_raw, "kind": kind}
        refs.append({**ref, "source_id": sid})
        originals[ref["path"]] = source_raw
    identity_source = sources.get(value.get("fixture_identity_source_id"))
    if identity_source is None or identity_source["kind"] != "official_dom_identity_snapshot":
        raise ValueError("reviewed_official_dom_fixture_identity_source_required")
    identity = fixture_identity(identity_source["raw"], fixture)
    identity["source_ref"] = deepcopy(identity_source["ref"])
    rows = value.get("quote_rows")
    if not isinstance(rows, list) or not rows:
        raise ValueError("explicit_whole_quote_rows_required")
    checked_rows, by_id = [], {}
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("whole_quote_row_required")
        source = sources.get(row.get("source_id"))
        if source is None or source["kind"] != "official_dom":
            raise ValueError("quote_official_source_binding_required")
        meta = source["metadata"]
        identity_meta = identity_source["metadata"]
        if (meta["source_url"] != identity_meta["source_url"]
                or _time(meta["observed_at"]) != _time(identity_meta["observed_at"])):
            raise ValueError("quote_identity_snapshot_observation_binding_mismatch")
        if (row.get("match_id") != str(fixture["match_id"])
                or row.get("source_url") != meta["source_url"]
                or _time(row.get("observed_at")) != _time(meta["observed_at"])):
            raise ValueError("quote_source_fixture_or_observation_mismatch")
        if (row.get("source_type") != "official_native_public_web_dom"
                or row.get("collector_version") != "public-rendered-dom-observation-v1"
                or row.get("company") != "Pinnacle" or row.get("user_screenshot_read") is not False
                or row.get("sequence") != SEQUENCES.get(row.get("market"))):
            raise ValueError("quote_official_dom_scope_required")
        if row.get("quote_time") is not None or row.get("time_precision") is not None:
            raise ValueError("current_extension_requires_unknown_change_time")
        if (row.get("quote_time_semantics") != "actual_individual_price_change_time_not_displayed"
                or row.get("page_clock_is_not_quote_change_time") is not True
                or row.get("reference_only") is not True or row.get("availability_status") != "unknown"
                or row.get("open_evidence") is not None or row.get("execution_evidence") is not None):
            raise ValueError("current_reference_cannot_claim_change_open_or_execution")
        # Adapter's API AH quarantine runs on the untouched original metadata.
        adapted = adapt_quote_row(row, match_id=str(fixture["match_id"]))
        if row.get("row_id") in by_id:
            raise ValueError("duplicate_quote_row_id")
        by_id[row["row_id"]] = (row, adapted, source)
        checked_rows.append({**row, "status": "open"} if row.get("status") == "reference_only" else deepcopy(row))
    historical = select_quotes(checked_rows, now.isoformat())
    # Retain original reference-only rows in the output. The open conversion
    # above is solely the old structural validator's numeric-cell vocabulary.
    for group in historical["groups"]:
        for field in ("snapshot_latest", "as_of_latest", "blocking_row"):
            if group.get(field) is not None:
                group[field] = deepcopy(by_id[group[field]["row_id"]][0])
    historical["structural_validation_adapter"] = "reference_only_numeric_cells_checked_as_open; original_rows_restored"
    quotes, issues = [], []
    for group in historical["groups"]:
        latest = group.get("snapshot_latest")
        if latest is None:
            issues.append({"reason": "missing_snapshot_top", "group": deepcopy(group)})
            continue
        # select_quotes intentionally permits nonzero first row orders. This
        # explicit current extension requires the actual visible top, order0.
        original, adapted, source = by_id[latest["row_id"]]
        if original["row_order"] != 0:
            issues.append({"reason": "visible_top_row_order_zero_required", "row_id": original["row_id"]})
            continue
        if adapted["row_status"] != "reference_only":
            issues.append({"reason": "top_row_" + adapted["row_status"], "row_id": original["row_id"]})
            continue
        dom_row_binding(source["raw"], original, fixture)
        quotes.append(adapted)
    cost = value.get("cost_per_unit")
    if cost is not None and (type(cost) not in (int, float) or not math.isfinite(cost) or not 0 <= cost < 1):
        raise ValueError("explicit_cost_per_unit_must_be_in_zero_one")
    historical["current_fixture_identity_check"] = identity
    return value, quotes, issues, refs, originals, historical, sources


def link_selection(value, result, sources):
    selected = value.get("selection")
    if selected is None:
        return {"status": "not_supplied", "automatic_recommendation": False, "formal_grade_verified": False}
    if not isinstance(selected, dict):
        raise ValueError("explicit_analyst_selection_required")
    source = sources.get(selected.get("declaration_source_id"))
    if source is None or source["kind"] != "reviewed_analyst_declaration":
        raise ValueError("selection_reviewed_declaration_source_required")
    declared_ref = selected.get("declaration_ref")
    if (not isinstance(declared_ref, dict)
            or declared_ref.get("source_id") != selected["declaration_source_id"]
            or declared_ref.get("path") != source["ref"]["path"]
            or declared_ref.get("sha256") != source["ref"]["sha256"]):
        raise ValueError("selection_declaration_ref_mismatch")
    declaration = strict_json(source["raw"])
    if (str(declaration.get("match_id")) != str(result["match_id"])
            or not selected.get("candidate_id")
            or declaration.get("primary", {}).get("candidate_id") != selected["candidate_id"]):
        raise ValueError("selection_declaration_candidate_binding_mismatch")
    binding = declaration.get("selection_binding")
    fields = ("candidate_id", "model_key", "quote_row_id", "selection")
    if not isinstance(binding, dict) or any(binding.get(k) != selected.get(k) for k in fields):
        raise ValueError("selection_declaration_contract_binding_mismatch")
    quote_row = next((q for q in result["quote_records"] if q["original_row"]["row_id"] == selected.get("quote_row_id")), None)
    quote_source = sources.get(quote_row["original_row"]["source_id"]) if quote_row else None
    quoted_ref = binding.get("quote_source_ref")
    if (quote_source is None or not isinstance(quoted_ref, dict)
            or quoted_ref.get("source_id") != quote_row["original_row"]["source_id"]
            or any(quoted_ref.get(k) != quote_source["ref"][k] for k in ("path", "sha256"))):
        raise ValueError("selection_declaration_quote_source_mismatch")
    # New explicit linkage is a reviewed analyst assertion. Free-form football
    # prose is preserved, never interpreted into a contract by this program.
    offers = [o for q in result["quote_records"] for o in q["offers"]
        if q["original_row"]["row_id"] == selected.get("quote_row_id") and o["selection"] == selected.get("selection")]
    linked = [c for c in result["calculations"] if len(offers) == 1
        and c["model_key"] == selected.get("model_key") and c["quote_offer_id"] == offers[0]["quote_offer_id"]
        and c["status"] == "calculated"]
    return {"status": "explicit_selection_linked" if len(linked) == 1 else "explicit_selection_unavailable",
        "declaration": deepcopy(selected), "declaration_ref": deepcopy(source["ref"]),
        "calculation_id": linked[0]["calculation_id"] if len(linked) == 1 else None,
        "automatic_recommendation": False, "formal_grade_verified": False,
        "meaning": "reviewed_analyst_declared_contract_link; no_EV_ranking_or_trade"}


def build(runtime, config, league, match_id, input_path, state_commit=None):
    # No caller clock, historical override, monkey patch, or decision-input
    # rewrite: all baseline/F5-C/advanced verification uses the actual clock.
    packet = integration.match_packet(runtime, config, league, match_id,
        as_of=None, state_commit=state_commit, decision_input=None)
    now = _time(packet["analysis_as_of"])
    fixture = packet["fixture"]
    value, quotes, issues, refs, originals, historical, sources = load_current_quotes(runtime, input_path, fixture, now)
    native = packet["feature_decision_comparison"]
    model_refs = native["evidence"] + packet.get("f5c_comparison", {}).get("evidence", [])
    for ref in model_refs:
        source_raw, checked = _read_ref(runtime, ref)
        originals[checked["path"]] = source_raw
    models = deepcopy(native["model_records"])
    baseline = packet["prediction"]
    if len([m for m in models if m["model_key"] == "M0" and m["status"] == "verified" and m["prediction"] == baseline]) != 1:
        raise ValueError("actual_packet_verified_M0_required")
    for model in models:
        if model.get("status") == "verified":
            matrix_array(model["prediction"]["score_matrix"])
    result = {"schema_version": VERSION, "match_id": match_id, "competition": league,
        "analysis_as_of": now.isoformat(), "input_declared_analysis_as_of": value.get("analysis_as_of"),
        "observation_mode": "current_observation", "quote_selection_mode": "snapshot_latest_row_order_zero",
        "historical_as_of_eligible": False, "research_only": True, "reference_only": True,
        "confirmation_eligible": False, "production_promotion": False, "automatic_recommendation": False,
        "current_open_verified": False, "actual_execution_verified": False,
        "status": "calculated_current_reference" if quotes else "no_eligible_current_top_row",
        "scope": "explicit_current_reference_extension; original_native_historical_pipeline_unchanged",
        "source_authority": "archived_exact_bytes_and_reviewed_declarations; no_network_authentication_or_content_truth_proof",
        "baseline_parent_sha256": native["baseline_parent_sha256"],
        "native_packet_sha256": _digest(packet), "native_comparison_status": native["status"],
        "original_historical_selector": historical,
        "comparisons": deepcopy(native["comparisons"]), "model_records": models,
        "quote_records": quotes, "quote_issues": issues, "calculations": [],
        "evidence": refs, "native_model_evidence": deepcopy(model_refs)}
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
                        delta_gross_ev=projected["gross_ev"] - base["gross_ev"],
                        delta_net_ev=projected["net_ev"] - base["net_ev"] if base["net_ev"] is not None else None,
                        net_ev_sign_disagreement=(projected["net_ev"] > 0) != (base["net_ev"] > 0) if base["net_ev"] is not None else None)
                else:
                    calculation.update(reason=model.get("reason"), result=None)
                calculation["calculation_id"] = _digest(calculation)
                result["calculations"].append(calculation)
    result["decision"] = link_selection(value, result, sources)
    if value.get("decision_record") is not None:
        # Exact actual cutoff still applies; never rewrite a frozen S2 record.
        linked = _decision_record_link(runtime, value, result, fixture, now)
        linked_refs = linked.pop("evidence")
        result["evidence"].extend(linked_refs)
        for ref in linked_refs:
            source_raw, checked = _read_ref(runtime, ref)
            originals[checked["path"]] = source_raw
        linked["selection_declaration"] = result["decision"]
        result["decision"] = linked
    result["comparison_id"] = _digest(result)
    return packet, result, originals


def save(output, packet, result, originals, identity, entrypoint_raw, config_raw=None):
    output = Path(output)
    if any(p.is_symlink() for p in (output, *output.parents)):
        raise ValueError("output_symlink_forbidden")
    run_id = _digest({"identity": identity, "comparison_id": result["comparison_id"]})
    directory = output / run_id
    directory.mkdir(parents=True, exist_ok=False)
    manifest = {"schema_version": "current-observation-receipt-v1", "run_id": run_id,
        "identity": identity, "files": [], "source_copies": [],
        "historical_as_of_eligible": False, "created_at": datetime.now(timezone.utc).isoformat()}
    def write(relative, raw):
        path = directory / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("xb") as handle:
            handle.write(raw)
        ref = {"path": relative, "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}
        manifest["files"].append(ref)
        return ref
    encode = lambda value: (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n").encode()
    write("current-observation-result.json", encode(result))
    write("actual-native-model-packet.json", encode(packet))
    write("runner.py", entrypoint_raw)
    if config_raw is not None:
        write("config.json", config_raw)
    for name, sha in identity.get("existing_implementation_fingerprint", {}).get("files", {}).items():
        raw = (REPO / name).read_bytes()
        if hashlib.sha256(raw).hexdigest() != sha:
            raise ValueError("implementation_changed_before_archive")
        write("code/" + name, raw)
    copied = {}
    for original_path, raw in originals.items():
        sha = hashlib.sha256(raw).hexdigest()
        if sha not in copied:
            copied[sha] = write("originals/" + sha + Path(original_path).suffix, raw)
        manifest["source_copies"].append({"original_runtime_path": original_path, "copy": copied[sha]})
    write("receipt.json", encode(manifest))
    return directory


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--config", type=Path, default=REPO / "config.json")
    parser.add_argument("--league", required=True)
    parser.add_argument("--match-id", type=int, required=True)
    parser.add_argument("--state-commit")
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    entrypoint_raw = Path(__file__).read_bytes()
    config_raw = args.config.read_bytes()
    fingerprint = implementation_fingerprint()
    identity = {"entrypoint_path": "scripts/current_observation_decision.py",
        "entrypoint_sha256": hashlib.sha256(entrypoint_raw).hexdigest(),
        "existing_implementation_fingerprint": fingerprint,
        "config_sha256": hashlib.sha256(config_raw).hexdigest()}
    packet, result, originals = build(args.runtime, strict_json(config_raw), args.league,
        args.match_id, args.input, args.state_commit)
    # Verify exact bytes after reads/calculation too; source changes cannot be
    # hidden by a copied in-memory object or a successful earlier hash check.
    for path, raw in originals.items():
        _read_ref(args.runtime, {"path": path, "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)})
    if (implementation_fingerprint() != fingerprint or Path(__file__).read_bytes() != entrypoint_raw
            or args.config.read_bytes() != config_raw):
        raise ValueError("implementation_changed_during_execution")
    result["implementation_identity"] = identity
    result.pop("comparison_id")
    result["comparison_id"] = _digest(result)
    directory = save(args.output, packet, result, originals, identity, entrypoint_raw, config_raw)
    print(json.dumps({"output": str(directory), "status": result["status"],
        "calculated": sum(c["status"] == "calculated" for c in result["calculations"]),
        "historical_as_of_eligible": False}, ensure_ascii=False))


if __name__ == "__main__":
    main()
