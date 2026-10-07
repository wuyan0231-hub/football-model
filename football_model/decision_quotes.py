"""Lossless whole-row to single-contract quote adapters for F2 decisions.

No OCR, live-market check, transaction or historical selection happens here.
Numeric rows provide reference prices only. Explicit availability/execution
evidence remains a claim whose provenance and contents require separate review.
"""
from __future__ import annotations

from copy import deepcopy
from decimal import Decimal
import hashlib
import json
import math

from .evidence import EvidenceError, _quote_interval, _quote_state, _time
from .settlement import contract as settlement_contract


VERSION = "decision-quotes-v1"
REGULATION = "regulation_time_90_plus_stoppage"
SELECTIONS = {"1x2": ("home", "draw", "away"),
              "asian_handicap": ("home", "away"), "totals": ("over", "under")}


class QuoteAdapterError(ValueError):
    """A machine-readable structural failure, never raw source contents."""


def _name(value):
    if not isinstance(value, str) or not value.strip():
        raise QuoteAdapterError("identifier_required")
    return value


def _number(value):
    try:
        return (not isinstance(value, bool) and isinstance(value, (int, float))
                and math.isfinite(value))
    except OverflowError:
        return False


def _digest(value):
    try:
        data = json.dumps(value, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError):
        raise QuoteAdapterError("quote_row_must_be_finite_json") from None
    return hashlib.sha256(data).hexdigest()


def odds_to_decimal(value, odds_format):
    """Convert a supplied numeric price; null is missing, never zero or even money."""
    if not isinstance(odds_format, str) or odds_format not in {"decimal", "hong_kong", "malay", "indonesian"}:
        raise QuoteAdapterError("unsupported_odds_format")
    if value is None:
        return None
    if not _number(value):
        raise QuoteAdapterError("invalid_odds")
    numeric = Decimal(str(value))
    if odds_format == "decimal":
        valid, result = value > 1, float(value)
    elif odds_format == "hong_kong":
        valid, result = value > 0, float(1 + numeric)
    elif odds_format == "malay":
        valid = -1 <= value <= 1 and value != 0
        result = float(1 + numeric if value >= 0 else 1 - 1 / numeric)
    else:
        valid = abs(value) >= 1
        result = float(1 + numeric if value >= 0 else 1 - 1 / numeric)
    if not valid or not math.isfinite(result) or result <= 1:
        raise QuoteAdapterError("odds_outside_format_domain")
    return float(result)


def _contract(row, selection, match_id):
    market = {"1x2": "1x2", "asian_handicap": "ah", "totals": "ou"}[row["market"]]
    line = row.get("line")
    if market == "1x2":
        if line is not None:
            raise QuoteAdapterError("one_x_two_cannot_have_line")
    elif line is None:
        return None
    if market == "ah" and selection == "away":
        # The entire source row's handicap is always expressed for the home team.
        if not _number(line):
            raise QuoteAdapterError("invalid_contract_line")
        line = -line
    if market != "1x2":
        if not _number(line):
            raise QuoteAdapterError("invalid_contract_line")
        # Normalize +/- zero and int/float into one economic contract identity.
        line = float(line) if line else 0.0
    value = {"market": market, "selection": selection, "line": line,
             "settlement_basis": "REGULATION"}
    canonical = {"market": {"1x2": "one_x_two", "ah": "asian_handicap", "ou": "total_goals"}[market],
                 "side": {"home": "home_win", "draw": "draw", "away": "away_win"}[selection]
                 if market == "1x2" else selection,
                 "line": line, "settlement_basis": REGULATION}
    try:
        settlement_contract(canonical)
    except ValueError:
        raise QuoteAdapterError("invalid_contract_line_or_scope") from None
    return {"contract_id": "ct_" + _digest({"match_id": match_id, **value}), **value}


def _claim(row, field, selection, reference_price):
    claims = row.get(field)
    if claims is None:
        return None
    if not isinstance(claims, dict) or set(claims) - set(SELECTIONS[row["market"]]):
        raise QuoteAdapterError("invalid_quote_evidence_selections")
    claim = claims.get(selection)
    if claim is None:
        return None
    if not isinstance(claim, dict):
        raise QuoteAdapterError("invalid_quote_evidence")
    if claim.get("row_id") != row["row_id"] or claim.get("selection") != selection:
        raise QuoteAdapterError("quote_evidence_binding_mismatch")
    _name(claim.get("source_id"))
    try:
        observed = _time(claim.get("observed_at"))
        interval = _quote_interval(row)
        if interval is not None and observed < interval[0]:
            raise QuoteAdapterError("quote_claim_before_quote_change")
        if field == "execution_evidence":
            _name(claim.get("receipt_id"))
            executed = _time(claim.get("executed_at"))
            if executed > observed:
                raise QuoteAdapterError("execution_after_evidence_observation")
            if interval is not None and executed < interval[0]:
                raise QuoteAdapterError("execution_before_quote_change")
            actual_price = odds_to_decimal(claim.get("decimal_odds"), "decimal")
            if actual_price is None:
                raise QuoteAdapterError("execution_price_required")
            # A fill at a different price requires its own whole row; no hidden repricing.
            if reference_price is None or actual_price != reference_price:
                raise QuoteAdapterError("execution_price_or_row_state_mismatch")
        elif reference_price is None:
            raise QuoteAdapterError("open_evidence_requires_reference_row")
    except EvidenceError as exc:
        raise QuoteAdapterError(str(exc)) from None
    return {"evidence_status": "claimed_metadata_only", "fact_verified": False,
            "evidence": deepcopy(claim)}


def adapt_quote_row(row, *, match_id, forecast_id=None):
    """Preserve one complete source row and derive its single-selection offers.

    Input follows evidence.select_quotes: market, row_id, source_id, sequence,
    row_order, quote_time/time_precision, status, odds_format, odds and line.
    The added settlement_basis must explicitly specify full regulation time.
    company, image_id and observed_at may be null; unknown provenance is not
    invented. Optional open_evidence/execution_evidence are keyed by selection.

    This function does not select a latest/as-of row. Use select_quotes first
    when historical cutoff selection is required. Missing/closed/incomplete
    row states yield null prices even if some old numeric cells remain visible.
    """
    match_id = _name(match_id)
    if forecast_id is not None:
        _name(forecast_id)
    if (not isinstance(row, dict) or not isinstance(row.get("market"), str)
            or row["market"] not in SELECTIONS):
        raise QuoteAdapterError("invalid_quote_market")
    if row.get("match_id") is not None and row["match_id"] != match_id:
        raise QuoteAdapterError("quote_match_mismatch")
    if not isinstance(row.get("settlement_basis"), str) or row["settlement_basis"] not in {"REGULATION", REGULATION}:
        raise QuoteAdapterError("explicit_regulation_time_required")
    for field in ("row_id", "source_id", "sequence"):
        _name(row.get(field))
    for field in ("company", "image_id"):
        if row.get(field) is not None:
            _name(row[field])
    order = row.get("row_order")
    if isinstance(order, bool) or not isinstance(order, int) or order < 0:
        raise QuoteAdapterError("invalid_row_order")
    original = deepcopy(row)
    digest = _digest(original)
    try:
        interval = _quote_interval(row)
        reference_only = row.get("status") == "reference_only"
        if reference_only:
            if (row.get("reference_only") is not True or row.get("availability_status") != "unknown"
                    or row.get("open_evidence") is not None or row.get("execution_evidence") is not None):
                raise QuoteAdapterError("reference_only_cannot_claim_open_or_execution")
            # Validate complete numeric cells without changing the persisted
            # source state or granting live-market/execution authority.
            checked = _quote_state({**row, "status": "open"})
            state = "reference_only" if checked == "open" else checked
        else:
            state = _quote_state(row)
        observed = _time(row["observed_at"]) if row.get("observed_at") is not None else None
        if observed is not None and interval is not None and interval[0] > observed:
            raise QuoteAdapterError("quote_change_after_observation")
    except EvidenceError as exc:
        raise QuoteAdapterError(str(exc)) from None
    except (TypeError, OverflowError):
        # Legacy row validators assume scalar statuses/formats and machine-sized
        # numerics. Malformed input must not escape the public adapter boundary.
        raise QuoteAdapterError("invalid_quote_scalar_or_numeric_domain") from None
    # Retain original quote precision and timezone. Do not create synthetic :00 times.
    provenance = {field: deepcopy(row.get(field)) for field in
                  ("source_id", "company", "image_id", "row_id", "sequence", "row_order",
                   "quote_time", "time_precision", "observed_at", "odds_format")}
    row_id = "qr_" + _digest({"match_id": match_id, "row_sha256": digest})
    contracts, offers = [], []
    for selection in SELECTIONS[row["market"]]:
        contract = _contract(row, selection, match_id)
        if contract is not None:
            contracts.append(contract)
        price = odds_to_decimal(row["odds"][selection], row["odds_format"]) if state in {"open", "reference_only"} else None
        if contract is None:
            price = None
        offer_id = "qo_" + _digest({"quote_row_id": row_id, "selection": selection})
        offers.append({"quote_offer_id": offer_id, "quote_id": offer_id,
                       "quote_row_id": row_id, "forecast_id": forecast_id,
                       "contract_id": contract["contract_id"] if contract else None,
                       "market": {"1x2": "1x2", "asian_handicap": "ah", "totals": "ou"}[row["market"]],
                       "selection": selection, "line": contract["line"] if contract else None,
                       "settlement_basis": "REGULATION", "decimal_odds": price,
                       "status": ("reference_only" if state == "reference_only" else "open_reference")
                       if price is not None else ("closed" if state == "closed" else "unknown"),
                       **({"reference_only": True, "availability_status": "unknown"} if reference_only else {}),
                       "row_status": state, "original_home_line": row.get("line") if row["market"] == "asian_handicap" else None,
                       "original_odds": deepcopy(row.get("odds", {}).get(selection)) if isinstance(row.get("odds"), dict) else None,
                       "current_open_claim": _claim(row, "open_evidence", selection, price),
                       "actual_execution_claim": _claim(row, "execution_evidence", selection, price),
                       "provenance": deepcopy(provenance), "source_id": row["source_id"]})
    return {"quote_adapter_version": VERSION, "match_id": match_id, "forecast_id": forecast_id,
            "quote_row_id": row_id, "row_sha256": digest, "original_row": original,
            "contracts": contracts, "offers": offers, "row_status": state,
            "scope": "structured_row_conversion_only_no_live_check_or_execution_verification"}
