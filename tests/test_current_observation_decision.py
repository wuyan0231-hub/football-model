"""Synthetic current-reference gates; these tests claim no prediction skill."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from scripts import current_observation_decision as current
from football_model.feature_decisions import _digest

NOW = datetime(2026, 10, 16, 14, 30, tzinfo=timezone.utc)
OBS = (NOW - timedelta(minutes=10)).isoformat()
URL = "https://www.pinnacle.com/en/soccer/england-premier-league/everton-vs-chelsea/1637086528/#all"
DOM = """Soccer
England - Premier League
Everton vs. Chelsea
Saturday, October 17, 2026 at 19:30
Everton
Chelsea
Money Line – Match
Show All
Everton
3.300
Draw
3.770
Chelsea
2.150
Handicap – Match
Everton\tChelsea
+0.25
2.030
-0.25
1.884
See more
Total – Match
Over 2.75
1.847
Under 2.75
2.050
See more
""".encode()


class CurrentObservationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.runtime = Path(self.temp.name)
        self.fixture = {"match_id": 100, "home_name": "Everton", "away_name": "Chelsea",
            "home_id": 1, "away_id": 2, "kickoff": "2026-10-17T11:30:00Z"}
        source_path = "feature_decisions/sources/" + hashlib.sha256(DOM).hexdigest() + ".txt"
        self.source = {"source_id": "dom", "path": source_path, "sha256": hashlib.sha256(DOM).hexdigest(),
            "observed_at": OBS, "reviewed_at": OBS, "reviewed": True, "source_url": URL,
            "reading_kind": current.READING_KIND, "quote_role": "QUOTE_REF"}
        path = self.runtime / source_path
        path.parent.mkdir(parents=True)
        path.write_bytes(DOM)
        identity_raw = DOM + b"22:06:37 (GMT +08:00)\n"
        identity_path = "feature_decisions/sources/identity.txt"
        (self.runtime / identity_path).write_bytes(identity_raw)
        self.identity_source = {"source_id": "identity", "source_kind": "official_dom_identity_snapshot",
            "path": identity_path, "sha256": hashlib.sha256(identity_raw).hexdigest(), "source_url": URL,
            "observed_at": OBS, "reviewed_at": OBS, "reviewed": True}
        common = {"source_id": "dom", "company": "Pinnacle", "match_id": "100", "image_id": None,
            "observed_at": OBS, "quote_time": None, "time_precision": None,
            "quote_time_semantics": "actual_individual_price_change_time_not_displayed", "status": "reference_only",
            "reference_only": True, "availability_status": "unknown", "open_evidence": None, "execution_evidence": None,
            "odds_format": "decimal", "settlement_basis": "REGULATION",
            "collector_version": "public-rendered-dom-observation-v1", "source_url": URL,
            "source_type": "official_native_public_web_dom", "user_screenshot_read": False,
            "page_clock_is_not_quote_change_time": True, "row_order": 0}
        rows = [{**common, "row_id": "r1", "market": "1x2", "sequence": current.SEQUENCES["1x2"],
                    "line": None, "odds": {"home": 3.3, "draw": 3.77, "away": 2.15}},
                {**common, "row_id": "r2", "market": "asian_handicap", "sequence": current.SEQUENCES["asian_handicap"],
                    "line": .25, "odds": {"home": 2.03, "away": 1.884},
                    "line_mapping_status": "reviewed_native_web_explicit_home_and_away_opposite_lines",
                    "raw_team_coordinates": {"home": "+0.25", "away": "-0.25"}},
                {**common, "row_id": "r3", "market": "totals", "sequence": current.SEQUENCES["totals"],
                    "line": 2.75, "odds": {"over": 1.847, "under": 2.05}}]
        self.value = {"schema_version": "current-observation-decision-input-v1", "observation_mode": "current_observation",
            "usage": "reference_only", "match_id": 100, "analysis_as_of": OBS, "sources": [self.source, self.identity_source],
            "fixture_identity_source_id": "identity",
            "quote_rows": rows, "cost_per_unit": None, "selection": None}
        self.input = self.runtime / "reviewed/input.json"
        self.input.parent.mkdir()
        # A finite, normalized literal matrix makes the real five-state math
        # meaningful without requiring collection or a fitted model in tests.
        prediction = {"score_matrix": [[.2, .1, .05], [.2, .15, .1], [.1, .05, .05]]}
        self.packet = {"fixture": self.fixture, "analysis_as_of": NOW.isoformat(), "prediction": prediction,
            "feature_decision_comparison": {"baseline_parent_sha256": "a" * 64, "status": "no_quote_supplied",
                "comparisons": [{"status": "rejected", "reason": "missing_capture"}], "evidence": [],
                "model_records": [{"model_key": "M0", "status": "verified", "forecast_id": "m0", "prediction": prediction},
                    {"model_key": "F5-C/selected", "status": "verified", "forecast_id": "f5", "prediction": prediction},
                    {"model_key": "advanced/not_available", "status": "rejected", "reason": "missing_capture"}]}}

    def tearDown(self):
        self.temp.cleanup()

    def write(self, value=None):
        self.input.write_text(json.dumps(self.value if value is None else value))

    def load(self, value=None):
        self.write(value)
        return current.load_current_quotes(self.runtime, self.input, self.fixture, NOW)

    def build(self, value=None):
        self.write(value)
        with patch.object(current.integration, "match_packet", return_value=deepcopy(self.packet)) as call:
            result = current.build(self.runtime, {}, "epl", 100, self.input, "state")
        self.assertEqual(call.call_args.kwargs, {"as_of": None, "state_commit": "state", "decision_input": None})
        return result

    def test_real_original_functions_fourteen_calculations_and_unknown_fees(self):
        packet, result, originals = self.build()
        self.assertEqual(sum(c["status"] == "calculated" for c in result["calculations"]), 14)
        self.assertEqual(sum(c["status"] == "rejected" for c in result["calculations"]), 7)
        self.assertEqual(result["model_records"], packet["feature_decision_comparison"]["model_records"])
        self.assertEqual(result["decision"]["status"], "not_supplied")
        self.assertFalse(result["historical_as_of_eligible"])
        self.assertEqual(len(originals), 3)
        for quote in result["quote_records"]:
            self.assertIsNone(quote["original_row"]["quote_time"])
            self.assertEqual(quote["row_status"], "reference_only")
        for calculation in result["calculations"]:
            if calculation["status"] == "calculated":
                self.assertIsNone(calculation["result"]["net_ev"])
                self.assertIsNone(calculation["delta_net_ev"])
                self.assertEqual(calculation["result"]["net_ev_status"], "fee_unknown")
                self.assertEqual(calculation["result"]["current_open_status"], "unknown")
                self.assertEqual(calculation["result"]["execution_status"], "unknown")
        for group in result["original_historical_selector"]["groups"]:
            self.assertEqual(group["status"], "unknown_quote_time")
            self.assertIsNone(group["as_of_latest"])
            self.assertEqual(group["snapshot_latest"]["status"], "reference_only")

    def test_explicit_zero_fee_is_distinct_from_unknown(self):
        self.value["cost_per_unit"] = 0
        _, result, _ = self.build()
        for calculation in result["calculations"]:
            if calculation["status"] == "calculated":
                self.assertEqual(calculation["result"]["gross_ev"], calculation["result"]["net_ev"])

    def test_existing_r2_explicit_diagnostic_usage_is_accepted(self):
        self.value.update(schema_version="feature-decision-input-v1", usage="diagnostic_reference_only")
        self.assertEqual(len(self.load()[1]), 3)

    def test_no_implicit_current_or_historical_usage(self):
        self.value["usage"] = "historical_as_of"
        with self.assertRaisesRegex(ValueError, "explicit_current"):
            self.load()

    def test_closed_unknown_and_incomplete_top_never_fall_back(self):
        for state in ("closed", "unknown", "incomplete", "reference_only"):
            with self.subTest(state=state):
                value = deepcopy(self.value)
                top = value["quote_rows"][0]
                older = {**deepcopy(top), "row_id": "old", "row_order": 1}
                top["status"] = state
                if state == "reference_only":
                    top["odds"]["home"] = None
                value["quote_rows"] = [top, older]
                _, quotes, issues, *_ = self.load(value)
                self.assertEqual(quotes, [])
                self.assertEqual(len(issues), 1)
                self.assertNotIn("old", json.dumps(quotes))

    def test_no_zero_top_and_duplicate_orders_block(self):
        value = deepcopy(self.value)
        value["quote_rows"][0]["row_order"] = 1
        quotes, issues = self.load(value)[1:3]
        self.assertEqual(len(quotes), 2)
        self.assertEqual(issues[0]["reason"], "visible_top_row_order_zero_required")
        value["quote_rows"].append({**value["quote_rows"][0], "row_id": "dup"})
        with self.assertRaisesRegex(ValueError, "duplicate_row_order"):
            self.load(value)

    def test_sequence_cannot_make_older_row_a_new_top(self):
        self.value["quote_rows"][0]["sequence"] = "invented-new-group"
        with self.assertRaisesRegex(ValueError, "dom_scope"):
            self.load()

    def test_source_hash_and_path_and_review_are_enforced(self):
        for change, message in (({"sha256": "0" * 64}, "hash_mismatch"),
                ({"path": "../escape.txt"}, "unsafe_evidence_path"),
                ({"reviewed": False}, "review_or_actual_time")):
            value = deepcopy(self.value)
            value["sources"][0].update(change)
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, message):
                self.load(value)

    def test_future_observation_review_or_declared_time_rejected(self):
        future = (NOW + timedelta(seconds=1)).isoformat()
        for target, field in (("source", "observed_at"), ("source", "reviewed_at"), ("input", "analysis_as_of")):
            value = deepcopy(self.value)
            (value["sources"][0] if target == "source" else value)[field] = future
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.load(value)

    def test_row_observation_match_source_url_and_official_scope_bound(self):
        for field, bad in (("observed_at", NOW.isoformat()), ("match_id", "101"),
                ("source_url", "https://example.com"), ("source_type", "api"),
                ("collector_version", "quote-collection-v1"), ("user_screenshot_read", True)):
            value = deepcopy(self.value)
            value["quote_rows"][0][field] = bad
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.load(value)

    def test_source_url_must_be_official_and_match_teams(self):
        for url in ("https://pinnacle.com.evil/en/soccer/everton-vs-chelsea", "http://www.pinnacle.com/en/soccer/everton-vs-chelsea",
                "https://user@www.pinnacle.com/en/soccer/everton-vs-chelsea", "https://www.pinnacle.com/en/soccer/a-vs-b"):
            value = deepcopy(self.value)
            value["sources"][0]["source_url"] = url
            with self.subTest(url=url), self.assertRaises(ValueError):
                self.load(value)

    def test_fixture_date_timezone_and_same_observation_are_required(self):
        for raw in (DOM, DOM + b"22:06:37 (GMT +00:00)\n", (DOM + b"22:06:37 (GMT +08:00)\n").replace(b"October 17", b"October 18")):
            with self.subTest(raw_hash=hashlib.sha256(raw).hexdigest()), self.assertRaises(ValueError):
                current.fixture_identity(raw, self.fixture)
        value = deepcopy(self.value)
        value["sources"][1]["observed_at"] = (NOW - timedelta(minutes=11)).isoformat()
        with self.assertRaisesRegex(ValueError, "snapshot_observation_binding"):
            self.load(value)
        value = deepcopy(self.value)
        value.pop("fixture_identity_source_id")
        with self.assertRaisesRegex(ValueError, "fixture_identity_source_required"):
            self.load(value)

    def test_page_clock_and_change_time_not_promoted(self):
        for changes in ({"quote_time": OBS, "time_precision": "second"}, {"time_precision": "minute"},
                {"page_clock_is_not_quote_change_time": False}, {"availability_status": "open"},
                {"open_evidence": {}}, {"execution_evidence": {}}, {"reference_only": False}):
            value = deepcopy(self.value)
            value["quote_rows"][0].update(changes)
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.load(value)

    def test_api_ah_cannot_be_certified_by_dom_labels(self):
        for changes in ({"market_id": 4, "market_name": "Asian Handicap"},
                {"provider_line_convention": "home_line_for_both_labels"}, {"row_id": "api_row_123"}):
            value = deepcopy(self.value)
            value["quote_rows"][1].update(changes)
            with self.subTest(changes=changes), self.assertRaisesRegex(ValueError, "api_asian_handicap_mapping_not_certified"):
                self.load(value)

    def test_original_dom_prices_and_opposite_lines_must_match(self):
        for market_index, changes in ((0, {"odds": {"home": 8, "draw": 3.77, "away": 2.15}}),
                (1, {"line": -.25}), (1, {"raw_team_coordinates": {"home": "+.25", "away": "+.25"}}),
                (2, {"line": 3.25})):
            value = deepcopy(self.value)
            value["quote_rows"][market_index].update(changes)
            with self.subTest(changes=changes), self.assertRaisesRegex(ValueError, "values_mismatch"):
                self.load(value)

    def test_bad_fees_and_nonfinite_json_rejected(self):
        for cost in (True, -1, 1, float("inf")):
            value = deepcopy(self.value)
            value["cost_per_unit"] = cost
            with self.subTest(cost=cost), self.assertRaises(ValueError):
                self.load(value)
        with self.assertRaises(ValueError):
            current.strict_json('{"x":1e999}')

    def add_selection(self):
        binding = {"candidate_id": "human-home-ah", "quote_row_id": "r2", "selection": "home", "model_key": "M0",
            "quote_source_ref": {k: self.source[k] for k in ("source_id", "path", "sha256")}}
        raw = json.dumps({"match_id": 100, "primary": {"candidate_id": "human-home-ah"}, "selection_binding": binding,
            "scope": "synthetic analyst declaration, not a skill claim"}).encode()
        path = "feature_decisions/sources/declaration.json"
        (self.runtime / path).write_bytes(raw)
        sha = hashlib.sha256(raw).hexdigest()
        self.value["sources"].append({"source_id": "analyst", "source_kind": "reviewed_analyst_declaration",
            "path": path, "sha256": sha, "reviewed": True, "observed_at": OBS, "reviewed_at": OBS})
        self.value["selection"] = {"declaration_source_id": "analyst", "candidate_id": "human-home-ah",
            "declaration_ref": {"source_id": "analyst", "path": path, "sha256": sha},
            "quote_row_id": "r2", "selection": "home", "model_key": "M0"}

    def test_explicit_reviewed_choice_linked_without_ev_ranking(self):
        self.add_selection()
        _, result, _ = self.build()
        self.assertEqual(result["decision"]["status"], "explicit_selection_linked")
        self.assertFalse(result["decision"]["automatic_recommendation"])
        chosen = next(c for c in result["calculations"] if c["calculation_id"] == result["decision"]["calculation_id"])
        quote = next(q for q in result["quote_records"] if q["row_sha256"] == chosen["row_sha256"])
        self.assertEqual(quote["original_row"]["row_id"], "r2")

    def test_choice_requires_real_declaration_and_matching_ref(self):
        self.add_selection()
        for changes in ({"declaration_source_id": "dom"}, {"candidate_id": "other"},
                {"selection": "away"}, {"model_key": "F5-C/selected"}, {"quote_row_id": "r1"},
                {"declaration_ref": {"source_id": "analyst", "path": "other", "sha256": "0" * 64}}):
            value = deepcopy(self.value)
            value["selection"].update(changes)
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.build(value)

    def test_immutable_output_contains_byte_exact_sources_and_runner(self):
        packet, result, originals = self.build()
        runner = Path(current.__file__).read_bytes()
        out = current.save(self.runtime / "out", packet, result, originals, {"test": True}, runner)
        receipt = json.loads((out / "receipt.json").read_bytes())
        for ref in receipt["files"]:
            raw = (out / ref["path"]).read_bytes()
            self.assertEqual(hashlib.sha256(raw).hexdigest(), ref["sha256"])
        for ref in receipt["source_copies"]:
            self.assertEqual((out / ref["copy"]["path"]).read_bytes(), originals[ref["original_runtime_path"]])
        with self.assertRaises(FileExistsError):
            current.save(self.runtime / "out", packet, result, originals, {"test": True}, runner)

    def test_native_and_f5_model_source_closure_is_reread_and_archived(self):
        refs = []
        for name in ("model-original.json", "f5-original.json"):
            raw = json.dumps({"synthetic_original": name}).encode()
            (self.runtime / name).write_bytes(raw)
            refs.append({"path": name, "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)})
        self.packet["feature_decision_comparison"]["evidence"] = [refs[0]]
        self.packet["f5c_comparison"] = {"evidence": [refs[1]]}
        _, result, originals = self.build()
        self.assertEqual(result["native_model_evidence"], refs)
        self.assertEqual(originals[refs[0]["path"]], (self.runtime / refs[0]["path"]).read_bytes())
        self.assertEqual(originals[refs[1]["path"]], (self.runtime / refs[1]["path"]).read_bytes())
        (self.runtime / refs[1]["path"]).write_bytes(b"tampered")
        with self.assertRaisesRegex(ValueError, "hash_mismatch"):
            self.build()

    def test_input_cannot_grant_historical_eligibility_or_automatic_choice(self):
        self.value.update(historical_as_of_eligible=True, confirmation_eligible=True, automatic_recommendation=True)
        _, result, _ = self.build()
        self.assertFalse(result["historical_as_of_eligible"])
        self.assertFalse(result["confirmation_eligible"])
        self.assertFalse(result["automatic_recommendation"])
        self.assertEqual(result["decision"]["status"], "not_supplied")

    def test_cli_has_no_historical_clock_argument(self):
        run = subprocess.run([sys.executable, str(Path(current.__file__)), "--help"], capture_output=True, text=True)
        self.assertEqual(run.returncode, 0)
        self.assertNotIn("--as-of", run.stdout)
        self.assertNotIn("--now", run.stdout)

    def test_invalid_full_s2_cutoff_cannot_be_rewritten(self):
        self.value["decision_record"] = {"match_id": 100, "analysis_cutoff": OBS, "kickoff_at": self.fixture["kickoff"]}
        with self.assertRaisesRegex(ValueError, "native_decision_time_mismatch"):
            self.build()


if __name__ == "__main__":
    unittest.main()
