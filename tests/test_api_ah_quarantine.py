"""API AH quarantine regressions; not provider semantic certification or OCR accuracy."""
from copy import deepcopy
import unittest

from football_model.decision_quotes import QuoteAdapterError, adapt_quote_row


def api_ah(**changes):
    source = {
        "row_id": "api_row_real-source-shape", "source_id": "api_odds_saved-page",
        "collector_version": "quote-collection-v1", "company": "Example",
        "image_id": None, "sequence": "api-football:8:4:-0.25", "row_order": 0,
        "market_id": 4, "market_name": "Asian Handicap", "market": "asian_handicap",
        "status": "unknown", "availability_status": "unknown",
        "odds": {"home": 1.85, "away": 2.05}, "odds_format": "decimal", "line": -0.25,
        "quote_time": "2026-10-07T08:00:00Z", "time_precision": "second",
        "observed_at": "2026-10-07T08:10:00Z", "settlement_basis": "REGULATION",
        "line_mapping_status": "empirical_inference_pending_provider_confirmation",
        "provider_line_convention": "home_line_for_both_labels",
        "raw_values": {"home": {"value": "Home -0.25", "odd": "1.85"},
                       "away": {"value": "Away -0.25", "odd": "2.05"}},
    }
    source.update(changes)
    return source


class ApiAhQuarantineTests(unittest.TestCase):
    def test_unknown_api_ah_is_lossless_observation_without_settleable_contracts(self):
        source = api_ah()
        original = deepcopy(source)
        output = adapt_quote_row(source, match_id="10")
        self.assertEqual(source, original)
        self.assertEqual(output["original_row"], original)
        self.assertEqual(output["row_status"], "unknown")
        self.assertEqual(output["contracts"], [])
        self.assertEqual(output["line_mapping_status"], "quarantined_unverified_api_asian_handicap")
        self.assertTrue(all(offer["contract_id"] is None and offer["line"] is None
                            and offer["decimal_odds"] is None
                            and offer["mapping_quarantine"] is True
                            for offer in output["offers"]))

    def test_handwritten_open_or_reference_state_cannot_bypass_quarantine(self):
        for changes in ({"status": "open"}, {"status": "reference_only", "reference_only": True},
                        {"status": "open", "line_mapping_status": "provider_confirmed"},
                        {"status": "open", "mapping_certificate": {"verified": True}}):
            with self.subTest(changes=changes), self.assertRaisesRegex(
                    QuoteAdapterError, "api_asian_handicap_mapping_not_certified"):
                adapt_quote_row(api_ah(**changes), match_id="10")

    def test_each_api_marker_independently_preserves_quarantine(self):
        generic = api_ah()
        markers = {key: generic.pop(key) for key in (
            "collector_version", "market_id", "market_name", "line_mapping_status",
            "provider_line_convention", "row_id", "source_id", "sequence")}
        generic.update(row_id="screenshot-row", source_id="screenshot", sequence="ah")
        for key in ("collector_version", "line_mapping_status", "provider_line_convention",
                    "row_id", "source_id", "sequence"):
            with self.subTest(marker=key):
                output = adapt_quote_row({**generic, key: markers[key]}, match_id="10")
                self.assertEqual(output["contracts"], [])
        output = adapt_quote_row({**generic, "market_id": 4, "market_name": "Asian Handicap"},
                                 match_id="10")
        self.assertEqual(output["contracts"], [])

    def test_closed_and_incomplete_api_rows_preserve_source_state(self):
        for state in ("closed", "incomplete"):
            output = adapt_quote_row(api_ah(status=state), match_id="10")
            self.assertEqual(output["row_status"], state)
            self.assertEqual(output["contracts"], [])
            self.assertTrue(all(o["decimal_odds"] is None for o in output["offers"]))

    def test_non_api_screenshot_home_coordinate_contracts_are_unchanged(self):
        source = {k: v for k, v in api_ah().items() if k not in {
            "collector_version", "market_id", "market_name", "line_mapping_status",
            "provider_line_convention", "raw_values"}}
        source.update(row_id="image-row", source_id="screenshot", sequence="ah",
                      image_id="IMG01", status="open")
        output = adapt_quote_row(source, match_id="10")
        self.assertEqual([c["line"] for c in output["contracts"]], [-0.25, 0.25])
        self.assertEqual([o["decimal_odds"] for o in output["offers"]], [1.85, 2.05])

    def test_explicit_historical_replay_is_separate_from_new_default_quarantine(self):
        source = api_ah()
        old = adapt_quote_row(source, match_id="10", legacy_replay_only=True)
        self.assertEqual([c["line"] for c in old["contracts"]], [-0.25, 0.25])
        self.assertTrue(all(o["decimal_odds"] is None for o in old["offers"]))
        self.assertNotIn("api_ah_quarantine_version", old)
        fresh = adapt_quote_row(source, match_id="10")
        self.assertEqual(fresh["contracts"], [])
        self.assertEqual(fresh["api_ah_quarantine_version"], "api-ah-quarantine-v1")
        with self.assertRaises(QuoteAdapterError):
            adapt_quote_row(source, match_id="10", legacy_replay_only="True")


if __name__ == "__main__":
    unittest.main()
