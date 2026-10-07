"""Engineering regression only: these fixtures do not establish OCR or forecast skill."""
import copy
import unittest

from football_model.decision_quotes import QuoteAdapterError, adapt_quote_row, odds_to_decimal
from football_model.evidence import select_quotes
from football_model.settlement import settlement


def row(**changes):
    result = {"row_id": "IMG02-ah-row1", "source_id": "screenshot-2", "company": "example-book",
              "image_id": "IMG02", "sequence": "ah", "row_order": 0, "market": "asian_handicap",
              "status": "open", "odds": {"home": 0.92, "away": 0.98}, "line": -0.25,
              "odds_format": "hong_kong", "quote_time": "2026-10-07T10:05:00+08:00",
              "time_precision": "minute", "observed_at": "2026-10-07T10:15:14+08:00",
              "settlement_basis": "REGULATION"}
    result.update(changes)
    return result


def adapt(value=None, **kwargs):
    return adapt_quote_row(row() if value is None else value, match_id="fixture-test-only", **kwargs)


def evidence(selection="home", **changes):
    claim = {"row_id": "IMG02-ah-row1", "selection": selection, "source_id": "explicit-evidence",
             "observed_at": "2026-10-07T10:20:14+08:00"}
    claim.update(changes)
    return claim


class DecisionQuotesTests(unittest.TestCase):
    def test_reference_only_has_numbers_but_no_open_or_execution_authority(self):
        source = row(status="reference_only", reference_only=True, availability_status="unknown")
        result = adapt(source)
        self.assertEqual(result["row_status"], "reference_only")
        self.assertEqual([o["decimal_odds"] for o in result["offers"]], [1.92, 1.98])
        self.assertTrue(all(o["status"] == "reference_only" and o["current_open_claim"] is None
                            and o["actual_execution_claim"] is None for o in result["offers"]))
        for updates in ({"reference_only": False}, {"availability_status": "open"},
                        {"open_evidence": {}}, {"execution_evidence": {}}):
            with self.assertRaises(QuoteAdapterError):
                adapt({**source, **updates})

    def test_all_four_price_formats_and_both_signed_domains(self):
        for fmt, value, expected in (("decimal", 1.92, 1.92), ("hong_kong", .92, 1.92),
                                     ("malay", .8, 1.8), ("malay", -.8, 2.25),
                                     ("malay", 1, 2), ("malay", -1, 2),
                                     ("indonesian", 1.25, 2.25), ("indonesian", -1.25, 1.8),
                                     ("indonesian", 1, 2), ("indonesian", -1, 2)):
            with self.subTest(fmt=fmt, value=value):
                self.assertAlmostEqual(odds_to_decimal(value, fmt), expected)

    def test_invalid_prices_never_become_valid_references(self):
        invalid = {"decimal": [0, 1, -1], "hong_kong": [0, -.01],
                   "malay": [0, 1.01, -1.01], "indonesian": [0, .99, -.99]}
        for fmt, values in invalid.items():
            for value in values + [True, False, "1.95", float("nan"), float("inf"), float("-inf"), 10 ** 1000]:
                with self.subTest(fmt=fmt, value=value), self.assertRaises(QuoteAdapterError):
                    odds_to_decimal(value, fmt)

    def test_null_price_is_not_even_money(self):
        for fmt in ("decimal", "hong_kong", "malay", "indonesian"):
            self.assertIsNone(odds_to_decimal(None, fmt))
        with self.assertRaises(QuoteAdapterError):
            odds_to_decimal(None, "guessed")

    def test_decimal_conversion_matches_recorded_display_price_without_float_artifact(self):
        self.assertEqual(odds_to_decimal(1.03, "hong_kong"), 2.03)
        self.assertEqual(odds_to_decimal(1.01, "hong_kong"), 2.01)
        result = adapt(row(odds={"home": 1.03, "away": .83},
                           execution_evidence={"home": evidence(executed_at="2026-10-07T10:19:14+08:00",
                                                                receipt_id="receipt", decimal_odds=2.03)}))
        self.assertEqual(result["offers"][0]["decimal_odds"], 2.03)

    def test_whole_original_row_is_preserved_without_input_mutation(self):
        source = row(note={"visible_text": "原图文字", "unknown_value": None})
        original = copy.deepcopy(source)
        result = adapt(source)
        self.assertEqual(source, original)
        self.assertEqual(result["original_row"], original)
        result["original_row"]["note"]["visible_text"] = "changed-copy"
        result["offers"][0]["provenance"]["company"] = "changed-copy"
        self.assertEqual(source, original)
        self.assertEqual(result["offers"][1]["provenance"]["company"], "example-book")

    def test_away_handicap_is_negated_and_matches_payment(self):
        result = adapt()
        home, away = result["offers"]
        self.assertEqual((home["line"], away["line"]), (-.25, .25))
        self.assertEqual(away["original_home_line"], -.25)
        self.assertEqual(settlement(1, 1, {"market": "asian_handicap", "side": "away", "line": away["line"]}), "half_win")
        self.assertEqual((home["decimal_odds"], away["decimal_odds"]), (1.92, 1.98))

    def test_positive_home_handicap_and_zero(self):
        self.assertEqual([o["line"] for o in adapt(row(line=.75))["offers"]], [.75, -.75])
        first, second = adapt(row(line=0)), adapt(row(line=-0.0))
        self.assertEqual(first["contracts"], second["contracts"])

    def test_one_x_two_preserves_all_three_sides(self):
        source = row(market="1x2", sequence="1x2", line=None, odds_format="decimal",
                     odds={"home": 1.8, "draw": 3.6, "away": 4.5})
        result = adapt(source)
        self.assertEqual([o["selection"] for o in result["offers"]], ["home", "draw", "away"])
        self.assertEqual([o["decimal_odds"] for o in result["offers"]], [1.8, 3.6, 4.5])
        self.assertTrue(all(c["line"] is None for c in result["contracts"]))

    def test_totals_do_not_negate_under_line(self):
        result = adapt(row(market="totals", sequence="ou", line=2.25, odds={"over": .94, "under": .96}))
        self.assertEqual([o["line"] for o in result["offers"]], [2.25, 2.25])
        self.assertEqual([o["market"] for o in result["offers"]], ["ou", "ou"])

    def test_closed_row_preserves_contract_and_original_numbers_without_reference(self):
        source = row(status="closed")
        result = adapt(source)
        self.assertEqual(len(result["contracts"]), 2)
        self.assertEqual(result["original_row"]["odds"], source["odds"])
        for offer in result["offers"]:
            self.assertIsNone(offer["decimal_odds"])
            self.assertEqual(offer["status"], "closed")
            self.assertIsNone(offer["current_open_claim"])
            self.assertIsNone(offer["actual_execution_claim"])

    def test_incomplete_row_cannot_publish_only_the_readable_side(self):
        for prices in ({"home": .92}, {"home": .92, "away": None}, {"home": .92, "away": True}):
            result = adapt(row(odds=prices))
            self.assertEqual(result["row_status"], "incomplete")
            self.assertTrue(all(o["decimal_odds"] is None for o in result["offers"]))

    def test_no_price_and_no_line_remain_distinct_unknowns(self):
        result = adapt(row(status="unknown", odds=None, line=None))
        self.assertEqual(result["contracts"], [])
        for offer in result["offers"]:
            self.assertIsNone(offer["contract_id"])
            self.assertIsNone(offer["decimal_odds"])
            self.assertIsNone(offer["line"])

    def test_provenance_and_time_unknowns_stay_null(self):
        result = adapt(row(company=None, image_id=None, observed_at=None, quote_time=None, time_precision=None))
        for offer in result["offers"]:
            for key in ("company", "image_id", "observed_at", "quote_time", "time_precision"):
                self.assertIsNone(offer["provenance"][key])
            self.assertEqual(offer["status"], "open_reference")
            self.assertIsNone(offer["forecast_id"])

    def test_reference_does_not_imply_open_or_executed_and_has_no_age_gate(self):
        result = adapt(row(quote_time="2026-10-06T10:05:00+08:00"))
        for offer in result["offers"]:
            self.assertEqual(offer["status"], "open_reference")
            self.assertIsNone(offer["current_open_claim"])
            self.assertIsNone(offer["actual_execution_claim"])

    def test_evidence_claims_stay_separate_and_never_become_verified(self):
        source = row(open_evidence={"home": evidence()},
                     execution_evidence={"home": evidence(executed_at="2026-10-07T10:19:14+08:00",
                                                          receipt_id="synthetic-receipt", decimal_odds=1.92)})
        result = adapt(source)
        home, away = result["offers"]
        for key in ("current_open_claim", "actual_execution_claim"):
            self.assertEqual(home[key]["evidence_status"], "claimed_metadata_only")
            self.assertFalse(home[key]["fact_verified"])
            self.assertIsNone(away[key])
        self.assertEqual(home["status"], "open_reference")

    def test_claims_require_exact_row_and_selection_binding(self):
        for changes in ({"row_id": "different"}, {"selection": "away"}, {"source_id": None}, {"observed_at": None}):
            with self.subTest(changes=changes), self.assertRaises(QuoteAdapterError):
                adapt(row(open_evidence={"home": evidence(**changes)}))

    def test_execution_price_and_time_cannot_be_inferred_or_backfilled(self):
        base = evidence(executed_at="2026-10-07T10:19:14+08:00", receipt_id="receipt", decimal_odds=1.92)
        for changes in ({"decimal_odds": None}, {"decimal_odds": 2.1}, {"receipt_id": None},
                        {"executed_at": "2026-10-07T10:21:14+08:00"}):
            with self.subTest(changes=changes), self.assertRaises(QuoteAdapterError):
                adapt(row(execution_evidence={"home": {**base, **changes}}))
        with self.assertRaises(QuoteAdapterError):
            adapt(row(status="closed", open_evidence={"home": evidence()}))

    def test_ids_are_reproducible_match_bound_and_independent_of_forecast(self):
        first, second = adapt(forecast_id="f1"), adapt(forecast_id="f2")
        self.assertEqual(first["quote_row_id"], second["quote_row_id"])
        self.assertEqual([o["quote_offer_id"] for o in first["offers"]], [o["quote_offer_id"] for o in second["offers"]])
        self.assertEqual(first["contracts"], second["contracts"])
        other_match = adapt_quote_row(row(), match_id="different-match")
        self.assertNotEqual(first["quote_row_id"], other_match["quote_row_id"])
        self.assertNotEqual(first["contracts"], other_match["contracts"])
        changed_row = adapt(row(company="another-book"))
        self.assertNotEqual(first["quote_row_id"], changed_row["quote_row_id"])
        self.assertEqual(first["contracts"], changed_row["contracts"])

    def test_adaptation_does_not_skip_closed_historical_row(self):
        newer = row(status="closed")
        older = row(row_id="older", row_order=1, quote_time="2026-10-07T10:00:00+08:00")
        selected = select_quotes([newer, older], "2026-10-07T10:07:00+08:00")["groups"][0]
        result = adapt(selected["as_of_latest"])
        self.assertEqual(result["row_status"], "closed")
        self.assertTrue(all(o["decimal_odds"] is None for o in result["offers"]))

    def test_only_explicit_regulation_contracts_are_accepted(self):
        for basis in (None, "EXTRA_TIME", "first_half"):
            with self.subTest(basis=basis), self.assertRaises(QuoteAdapterError):
                adapt(row(settlement_basis=basis))
        self.assertEqual(adapt(row(settlement_basis="regulation_time_90_plus_stoppage"))["contracts"], adapt()["contracts"])

    def test_invalid_lines_and_match_binding_are_rejected(self):
        for value in (True, "-0.25", .1, 101):
            with self.subTest(value=value), self.assertRaises(QuoteAdapterError):
                adapt(row(line=value))
        with self.assertRaises(QuoteAdapterError):
            adapt(row(match_id="another-match"))
        with self.assertRaises(QuoteAdapterError):
            adapt(row(market="totals", line=-.25))
        with self.assertRaises(QuoteAdapterError):
            adapt(row(market="1x2", line=0))

    def test_impossible_and_fabricated_precision_times_rejected(self):
        for changes in ({"observed_at": "2026-10-07T10:04:00+08:00"},
                        {"observed_at": "2026-10-07T10:15:00"},
                        {"quote_time": "2026-10-07T10:05:17+08:00"}):
            with self.subTest(changes=changes), self.assertRaises(QuoteAdapterError):
                adapt(row(**changes))

    def test_non_finite_original_rows_cannot_receive_normal_fingerprints(self):
        for bad in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(bad=bad), self.assertRaises(QuoteAdapterError):
                adapt(row(odds={"home": bad, "away": .98}))

    def test_claim_cannot_precede_its_bound_quote_change(self):
        with self.assertRaises(QuoteAdapterError):
            adapt(row(open_evidence={"home": evidence(observed_at="2026-10-07T10:04:00+08:00")}))
        with self.assertRaises(QuoteAdapterError):
            adapt(row(execution_evidence={"home": evidence(executed_at="2026-10-07T10:04:00+08:00",
                                                          receipt_id="receipt", decimal_odds=1.92)}))

    def test_invalid_container_types_have_controlled_errors(self):
        for source in ([], None, row(market=[]), row(settlement_basis=[])):
            with self.subTest(source=source), self.assertRaises(QuoteAdapterError):
                adapt_quote_row(source, match_id="fixture-test-only")
        with self.assertRaises(QuoteAdapterError):
            odds_to_decimal(.9, [])

    def test_legacy_row_validator_shape_and_overflow_failures_are_controlled(self):
        for changes in ({"status": []}, {"odds_format": []},
                        {"odds": {"home": 10 ** 1000, "away": .98}}, {"line": 10 ** 1000}):
            source = row(**changes)
            original = copy.deepcopy(source)
            with self.subTest(changes=changes), self.assertRaises(QuoteAdapterError):
                adapt(source)
            self.assertEqual(source, original)


if __name__ == "__main__":
    unittest.main()
