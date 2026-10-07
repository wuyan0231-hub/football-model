"""Synthetic engineering checks; none of these fixtures are football samples."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from football_model.decision_record import build_decision_bundle, append_decision_event
from football_model.decision_scoring import (register_scoring_protocol, score_decision,
                                             read_decision_score)
from football_model.evidence import EvidenceError
from football_model.prospective import AXES
from test_decision_record import fixture


def sha(blob):
    return hashlib.sha256(blob).hexdigest()


def _time_for_test(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class DecisionScoringTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.base = datetime.now(timezone.utc) - timedelta(days=3)
        self.record = fixture(self.root)
        self.record.update(competition="synthetic", home_id=10, away_id=20,
                           analysis_cutoff=self.t(300), kickoff_at=self.t(8 * 3600))
        self.record["inputs"][0]["observed_at"] = self.t(0)
        self.record["quote_rows"][0].update(observed_at=self.t(0), quote_time=self.t(-60))
        self.spec = {"name": "synthetic-only", "quote_protocol": "QUOTE_REF", "usage": "single",
                     "scope": {"competitions": ["synthetic"], "match_ids": "all"},
                     "evaluation_mode": "prospective", "unit_stake": 1, "cost_per_unit": 0,
                     "decision_selection": "explicit_adopted_version_no_retrospective_switch",
                     "condition_policy": "satisfied_or_no_conditions_only"}
        self.protocol = self.register()
        self.result = {"schema_version": 1, "competition": "synthetic", "match_id": "synthetic-match",
                       "home_id": 10, "away_id": 20, "kickoff": self.t(8*3600), "home_goals": 1,
                       "away_goals": 1, "observed_at": self.t(14*3600), "captured_at": self.t(14*3600+1),
                       "score_scope": "normalized_API_Football_score_fulltime_90_plus_stoppage"}

    def tearDown(self):
        self.tmp.cleanup()

    def t(self, seconds):
        return (self.base + timedelta(seconds=seconds)).isoformat()

    def register(self, seconds=-120):
        with patch("football_model.decision_scoring._now", return_value=self.base+timedelta(seconds=seconds)):
            return register_scoring_protocol(self.spec, self.root)

    def build(self, seconds=600):
        self.bundle = self.root / ("bundle-" + str(len(list(self.root.glob("bundle-*")))))
        with patch("football_model.decision_record._now", return_value=self.base+timedelta(seconds=seconds)):
            self.built = build_decision_bundle(self.record, self.bundle, source_root=self.root)
        self.assertNotEqual(self.built["status"], "blocked", self.built)

    def external(self, name, value):
        blob = (json.dumps(value, sort_keys=True) + "\n").encode()
        (self.root/name).write_bytes(blob)
        return {"path": name, "sha256": sha(blob)}

    def score(self, **kwargs):
        descriptor = self.external("result.json", self.result)
        source = self.external("result-original.json", {"synthetic_source": True, "match_result": self.result})
        source.update(observed_at=self.result["observed_at"], semantic_review={"status": "verified", "reviewer": "synthetic",
                      "source_sha256": source["sha256"], "reviewed_at": self.t(15*3600)})
        descriptor.update(source_evidence=[source], semantic_review={"status": "verified", "reviewer": "synthetic",
                          "source_sha256": descriptor["sha256"], "reviewed_at": self.t(15*3600)})
        args = {"expected_manifest_sha256": self.built["manifest_sha256"], "protocol_root": self.root,
                "protocol_id": self.protocol["protocol_id"], "result": descriptor,
                "result_source_root": self.root}
        args.update(kwargs)
        return score_decision(self.bundle, **args)

    def model(self, identity="adopted", matrix=None):
        matrix = matrix or [[0.1, 0.1, 0.0], [0.1, 0.5, 0.0], [0.1, 0.1, 0.0]]
        prediction = {"home_id": 10, "away_id": 20, "kickoff": self.record["kickoff_at"],
                      "score_matrix_axes": AXES, "score_matrix": matrix,
                      "model_version": "synthetic-v1", "training_data_sha256": "1"*64,
                      "probabilities": {"home_win": 0.3, "draw": 0.6, "away_win": 0.1}}
        value = {"forecast_id": identity, "match_id": "synthetic-match", "competition": "synthetic",
                 "home_id": 10, "away_id": 20, "kickoff": self.record["kickoff_at"],
                 "prediction": prediction, "forecast_generated_at": self.t(-200),
                 "model_id": "synthetic-model", "time_semantics": "observed_wall_clock_v1",
                 "run_started_at": self.t(-500), "data_observed_at": self.t(-450),
                 "model_fitted_at": self.t(-300), "training_cutoff": self.t(-400)}
        return value

    def add_forecast(self, values=None):
        values = values or [self.model()]
        descriptor = self.external("model.json", values)
        self.record["inputs"].append({"input_id": "model-input", "source_id": "model-source", "role": "prediction",
                                     **descriptor, "observed_at": self.t(0), "published_at": None})
        self.record["forecast_id"] = "adopted"
        self.record["archive"]["model_records"].append({"model_record_id": "m1", "forecast_id": "adopted",
                                                          "forecast_evidence_ids": ["model-input"]})

    def execution(self, **updates):
        card = json.loads((self.bundle/"main_card.json").read_text())
        descriptor = self.external("receipt.json", {"synthetic": "receipt bytes only"})
        event = {"event_id": "execution-1", "event_type": "execution", "match_id": "synthetic-match",
                 "decision_id": self.record["decision_id"], "version_id": self.record["version_id"],
                 "occurred_at": self.t(700), "evidence": [{**descriptor, "observed_at": self.t(710)}],
                 "candidate_id": "c1", "quote_offer_id": card["primary_candidate"]["quote_offer_id"],
                 "status": "accepted", "accepted_decimal_odds": 2.05, "transaction_reference": "synthetic-receipt",
                 "usage": "single", "conditions_status": "satisfied", "cost_per_unit": 0.01, "stake": 2}
        event.update(updates)
        with patch("football_model.decision_record._now", return_value=self.base+timedelta(seconds=720)):
            saved = append_decision_event(self.bundle, event, self.root/"events", source_root=self.root)
        return {"path": saved["output_dir"], "manifest_sha256": saved["manifest_sha256"],
                "semantic_review": {"status": "verified", "reviewer": "synthetic-reviewer",
                                    "source_sha256": saved["manifest_sha256"], "reviewed_at": self.t(800)}}

    def test_qualitative_reference_settles_without_probability_or_execution(self):
        self.build()
        score = self.score()
        self.assertEqual(score["outcome"]["settlement_state"], "win")
        self.assertAlmostEqual(score["outcome"]["paper_reference_return"], 0.95)
        self.assertIsNone(score["outcome"]["actual_single_return"])
        self.assertIsNone(score["probability"]["net_ev"])
        self.assertEqual(score["execution_status"], "unknown")
        self.assertEqual(score["qualification"], "prospective_decision_candidate")

    def test_unknown_cost_not_zero_but_five_state_still_settles(self):
        self.spec["cost_per_unit"] = None
        self.protocol = self.register()
        self.build()
        score = self.score()
        self.assertEqual(score["outcome"]["settlement_state"], "win")
        self.assertIsNone(score["outcome"]["paper_reference_return"])
        self.assertIn("cost_unknown_not_zero", score["unavailable_reasons"])

    def test_only_actually_adopted_forecast_scored(self):
        background = self.model("first-background")
        background["prediction"]["score_matrix"] = [[0, 0], [1, 0]]
        background["prediction"]["probabilities"] = {"home_win": 1, "draw": 0, "away_win": 0}
        self.add_forecast([background, self.model()])
        self.build()
        score = self.score()
        self.assertEqual(score["binding"]["forecast_id"], "adopted")
        self.assertAlmostEqual(score["probability"]["five_state_probabilities"]["win"], 0.9)
        self.assertAlmostEqual(score["probability"]["net_ev"], .9*.95-.1)
        self.assertAlmostEqual(score["probability"]["metrics"]["one_x_two"]["brier_score"], .26)

    def test_swapped_teams_in_adopted_forecast_rejected(self):
        value = self.model()
        value["home_id"], value["away_id"] = 20, 10
        self.add_forecast([value])
        self.build()
        with self.assertRaisesRegex(EvidenceError, "home_id_identity_mismatch"):
            self.score()

    def test_inner_training_cutoff_cannot_use_later_model_under_old_outer_cutoff(self):
        value = self.model()
        value["prediction"]["training_cutoff"] = self.t(-100)
        self.add_forecast([value])
        self.build()
        with self.assertRaisesRegex(EvidenceError, "adopted_prediction_training_cutoff_mismatch"):
            self.score()

    def test_missing_decision_team_identity_does_not_quantify(self):
        self.add_forecast()
        self.record.pop("home_id")
        self.build()
        self.assertEqual(self.score()["probability"]["reason"], "decision_complete_team_identity_missing")

    def test_result_wrong_kickoff_or_scope_rejected(self):
        self.build()
        self.result["kickoff"] = self.t(7*3600)
        with self.assertRaisesRegex(EvidenceError, "kickoff_identity_mismatch"):
            self.score()
        self.result["kickoff"] = self.t(8*3600)
        self.result["score_scope"] = "120_minutes"
        with self.assertRaisesRegex(EvidenceError, "independent_regulation"):
            self.score()

    def test_missing_result_parents_remains_declared_research_arithmetic(self):
        self.build()
        descriptor = self.external("result.json", self.result)
        score = self.score(result=descriptor)
        self.assertEqual(score["result_source_status"], "unverified_normalized_declaration")
        self.assertEqual(score["qualification"], "research_only")
        self.assertEqual(score["outcome"]["settlement_state"], "win")

    def test_native_result_parent_matches_frozen_normalized_data(self):
        self.build()
        source = {"kind": "normalized_regulation_data", "original_bytes_sha256": "1"*64,
                  "content": {"time_semantics": "observed_wall_clock_v1", "observed_at": self.result["observed_at"],
                              "matches": [deepcopy(self.result)]}}
        parent = self.external("native-source.json", source)
        self.result["source_id"] = parent["sha256"]
        descriptor = self.external("result.json", self.result)
        descriptor["source_evidence"] = [parent]
        score = self.score(result=descriptor, output_dir=self.root/"native-score")
        self.assertEqual(score["result_source_status"], "native_normalized_parent_verified")
        self.assertEqual(read_decision_score(self.root/"native-score", score["manifest_sha256"])["verification_status"], "verified")

    def test_result_unknown_or_premature_retains_pending_or_rejects(self):
        self.build()
        score = self.score(result=None)
        self.assertEqual(score["outcome"]["status"], "pending")
        self.assertIsNone(score["outcome"]["paper_reference_return"])
        self.result["observed_at"] = self.t(9*3600)
        with self.assertRaisesRegex(EvidenceError, "before_maturity"):
            self.score()

    def test_research_and_late_protocol_never_promoted(self):
        self.record["future_information_exposure"] = True
        self.protocol = self.register(400)
        self.build()
        score = self.score()
        self.assertEqual(score["qualification"], "research_only")
        self.assertIn("protocol_registered_after_decision_cutoff", score["qualification_reasons"])

    def test_reference_not_open_even_when_original_row_is_open(self):
        self.spec["quote_protocol"] = "QUOTE_OPEN"
        self.protocol = self.register()
        self.build()
        score = self.score()
        self.assertIsNone(score["outcome"]["open_simulated_return"])
        self.assertIn("independent_open_evidence_missing", score["unavailable_reasons"])

    def open_quote(self):
        card = json.loads((self.bundle/"main_card.json").read_text())
        primary = card["primary_candidate"]
        raw = self.external("raw-open-response.json", {"synthetic": "independent provider open response bytes"})
        raw.update(observed_at=self.t(705), semantic_review={"status": "verified", "reviewer": "synthetic-reviewer",
                   "source_sha256": raw["sha256"], "reviewed_at": self.t(800)})
        value = {**{k: self.result[k] for k in ("competition", "match_id", "home_id", "away_id", "kickoff")},
                 "decision_id": self.record["decision_id"], "version_id": self.record["version_id"],
                 **{k: primary[k] for k in ("candidate_id", "contract_id", "quote_offer_id")},
                 "contract": {"market": "total_goals", "side": "under", "line": 2.5}, "decimal_odds": 1.95,
                 "status": "open", "conditions_status": "satisfied", "asserted_at": self.t(700), "observed_at": self.t(710),
                 "source_evidence": [raw]}
        descriptor = self.external("open.json", value)
        descriptor["semantic_review"] = {"status": "verified", "reviewer": "synthetic-reviewer",
                                         "source_sha256": descriptor["sha256"], "reviewed_at": self.t(800)}
        return descriptor, value

    def test_open_requires_bound_semantic_review_and_exact_price(self):
        self.spec["quote_protocol"] = "QUOTE_OPEN"
        self.protocol = self.register()
        self.build()
        descriptor, value = self.open_quote()
        score = self.score(quote_evidence=descriptor, quote_source_root=self.root)
        self.assertAlmostEqual(score["outcome"]["open_simulated_return"], .95)
        self.assertIsNone(score["outcome"]["paper_reference_return"])
        descriptor.pop("semantic_review")
        self.assertIsNone(self.score(quote_evidence=descriptor, quote_source_root=self.root)["outcome"]["open_simulated_return"])
        value["decimal_odds"] = 2
        descriptor = self.external("open.json", value)
        with self.assertRaisesRegex(EvidenceError, "price_or_contract_mismatch"):
            self.score(quote_evidence=descriptor, quote_source_root=self.root)

    def test_actual_uses_receipted_price_and_explicit_cost(self):
        self.spec["quote_protocol"] = "QUOTE_ACTUAL"
        self.protocol = self.register()
        self.build()
        event = self.execution()
        score = self.score(events=[event])
        self.assertAlmostEqual(score["outcome"]["actual_single_return"], 1.04)
        self.assertAlmostEqual(score["outcome"]["actual_stake_return"], 2.08)
        self.assertIsNone(score["outcome"]["paper_reference_return"])
        self.assertIsNone(score["outcome"]["whole_parlay_return"])

    def test_actual_user_confirmation_unknown_price_not_reference_fill(self):
        self.spec["quote_protocol"] = "QUOTE_ACTUAL"
        self.protocol = self.register()
        self.build()
        descriptor = self.external("confirmation.txt", {"synthetic": "confirmed but details unknown"})
        event = self.execution(accepted_decimal_odds=None, execution_basis="user_confirmation", cost_per_unit=None,
                               evidence=[{**descriptor, "observed_at": self.t(710), "evidence_type": "user_confirmation"}])
        score = self.score(events=[event])
        self.assertEqual(score["execution_status"], "accepted")
        self.assertIsNone(score["price_decimal"])
        self.assertIsNone(score["outcome"]["actual_single_return"])

    def test_actual_parlay_leg_never_becomes_single_or_whole_parlay_return(self):
        self.spec.update(quote_protocol="QUOTE_ACTUAL", usage="parlay_leg")
        self.protocol = self.register()
        self.build()
        score = self.score(events=[self.execution(usage="parlay_leg")])
        self.assertEqual(score["outcome"]["settlement_state"], "win")
        self.assertIsNone(score["outcome"]["actual_single_return"])
        self.assertIsNone(score["outcome"]["whole_parlay_return"])

    def test_actual_unreviewed_event_not_true_execution_profit(self):
        self.spec["quote_protocol"] = "QUOTE_ACTUAL"
        self.protocol = self.register()
        self.build()
        event = self.execution()
        event.pop("semantic_review")
        self.assertIsNone(self.score(events=[event])["outcome"]["actual_single_return"])

    def test_unknown_conditions_not_zero_or_automatic_execution(self):
        candidate = self.record["archive"]["candidates"][0]
        candidate["formal_fit"]["single"] = "条件适合"
        candidate["formal_condition_ids"] = {"single": ["cond"], "parlay_leg": []}
        self.record["archive"]["conditions"] = [{"condition_id": "cond", "critical": True, "state": "unknown",
              "observable": "synthetic", "actions": {"satisfied": "reassess", "not_satisfied": "skip", "unknown": "watch"}}]
        self.build()
        score = self.score()
        self.assertEqual(score["conditions_status"], "unknown")
        self.assertIsNone(score["outcome"]["paper_reference_return"])

    def test_quarter_goal_half_win_exact(self):
        self.record["quote_rows"][0]["line"] = 2.25
        self.build()
        score = self.score()
        self.assertEqual(score["outcome"]["settlement_state"], "half_win")
        self.assertAlmostEqual(score["outcome"]["paper_reference_return"], .475)

    def test_append_and_replay_integrity_no_score_overwrite(self):
        self.add_forecast()
        self.build()
        output = self.root / "score"
        score = self.score(output_dir=output)
        read = read_decision_score(output, score["manifest_sha256"])
        self.assertEqual(read["verification_status"], "verified")
        with self.assertRaisesRegex(EvidenceError, "no_overwrite"):
            self.score(output_dir=output)
        self.result["home_goals"] = 3
        revised = self.score(output_dir=self.root/"score-revision")
        self.assertEqual(revised["outcome"]["settlement_state"], "loss")
        self.assertEqual(read_decision_score(output, score["manifest_sha256"])["outcome"]["settlement_state"], "win")

    def test_actual_event_score_replay(self):
        self.spec["quote_protocol"] = "QUOTE_ACTUAL"
        self.protocol = self.register()
        self.build()
        score = self.score(events=[self.execution()], output_dir=self.root/"score")
        self.assertEqual(read_decision_score(self.root/"score", score["manifest_sha256"])["verification_status"], "verified")

    def test_external_hash_and_event_parent_binding_checked(self):
        self.build()
        with self.assertRaisesRegex(EvidenceError, "hash_mismatch"):
            self.score(result={"path": "result.json", "sha256": "0"*64})
        event = self.execution()
        self.record["version_id"] = "synthetic-r2"
        self.build()
        with self.assertRaisesRegex(EvidenceError, "event_parent_manifest_mismatch"):
            self.score(events=[event])

    def test_zero_probability_log_loss_exposed_not_hidden(self):
        value = self.model()
        value["prediction"]["score_matrix"] = [[0, 0], [1, 0]]
        value["prediction"]["probabilities"] = {"home_win": 1, "draw": 0, "away_win": 0}
        self.add_forecast([value])
        self.build()
        metric = self.score()["probability"]["metrics"]["one_x_two"]
        self.assertEqual(metric["mathematical_log_loss_status"], "positive_infinity")
        self.assertIsNone(metric["mathematical_log_loss"])

    def test_model_lineage_missing_does_not_invent_probabilities(self):
        value = self.model()
        value["prediction"].pop("training_data_sha256")
        self.add_forecast([value])
        self.build()
        score = self.score()
        self.assertIsNone(score["probability"]["five_state_probabilities"])
        self.assertEqual(score["probability"]["reason"], "adopted_forecast_model_lineage_missing")
        self.assertEqual(score["outcome"]["settlement_state"], "win")

    def test_research_forecast_never_promotes_even_if_lineage_missing(self):
        value = self.model()
        value["research_replay"] = True
        value["prediction"].pop("training_data_sha256")
        self.add_forecast([value])
        self.build()
        score = self.score()
        self.assertEqual(score["qualification"], "research_only")
        self.assertIn("research_replay_forecast", score["qualification_reasons"])
        self.assertIsNone(score["probability"]["five_state_probabilities"])

    def test_chinese_satisfied_condition_and_formal_abstention_preserve_contract_return(self):
        candidate = self.record["archive"]["candidates"][0]
        candidate["formal_fit"] = {"single": "不建议", "parlay_leg": "不建议"}
        candidate["condition_ids"] = ["cond"]
        self.record["archive"]["conditions"] = [{"condition_id": "cond", "critical": True, "state": "满足",
              "observable": "synthetic", "actions": {"satisfied": "reassess", "not_satisfied": "skip", "unknown": "watch"}}]
        self.build()
        score = self.score()
        self.assertEqual(score["conditions_status"], "satisfied")
        self.assertAlmostEqual(score["outcome"]["paper_reference_return"], .95)

    def test_open_originals_and_mapping_replay_without_external_files(self):
        self.spec["quote_protocol"] = "QUOTE_OPEN"
        self.protocol = self.register()
        self.build()
        descriptor, value = self.open_quote()
        saved = self.score(quote_evidence=descriptor, quote_source_root=self.root, output_dir=self.root/"score")
        (self.root/"raw-open-response.json").unlink()
        checked = read_decision_score(self.root/"score", saved["manifest_sha256"])
        self.assertAlmostEqual(checked["outcome"]["open_simulated_return"], .95)

    def test_all_three_market_contracts_are_scored_without_side_reversal(self):
        for market, selection, odds, line, state in (
                ("1x2", "draw", {"home": 2.1, "draw": 3.2, "away": 3.3}, None, "win"),
                ("asian_handicap", "away", {"home": 1.9, "away": 1.95}, -.25, "half_win")):
            with self.subTest(market=market):
                self.record["quote_rows"][0].update(market=market, odds=odds, line=line)
                self.record["archive"]["candidates"][0]["selection"] = selection
                self.build()
                score = self.score()
                self.assertEqual(score["outcome"]["settlement_state"], state)
                if market == "asian_handicap":
                    self.assertEqual(score["contract"]["line"], .25)

    def test_api_reference_bridge_to_f2_and_paper_score(self):
        from football_model.collect_quotes import parse_records, adapt_api_reference_row
        from test_quote_collection import record as api_record
        raw = api_record(10)
        raw["fixture"].update(date=self.record["kickoff_at"], timestamp=int(_time_for_test(self.record["kickoff_at"]).timestamp()))
        raw["update"] = self.t(-60)
        candidate = {"league": "synthetic", "league_id": 999,
                     "fixture": {"match_id": 10, "home_id": 10, "away_id": 20, "kickoff": self.record["kickoff_at"]}}
        parsed = parse_records([raw], candidate, observed_at=self.t(0), source_id="s1")
        source = self.external("api-source.json", {"response": [raw]})
        self.record["inputs"][0].update(source)
        self.record["match_id"] = "10"
        self.result["match_id"] = "10"
        for market, selection in (("1x2", "draw"), ("totals", "under")):
            with self.subTest(market=market):
                row = next(r for r in parsed["rows"] if r["market"] == market)
                bridge = adapt_api_reference_row(row)
                self.record["quote_rows"] = [bridge["original_row"]]
                self.record["archive"]["candidates"][0].update(quote_row_id=bridge["original_row"]["row_id"], selection=selection)
                self.build()
                score = self.score()
                self.assertIsNotNone(score["outcome"]["paper_reference_return"])
                self.assertIsNone(score["outcome"]["open_simulated_return"])
                self.assertEqual(score["execution_status"], "unknown")

    def test_independent_result_sync_source_replays_into_decision_score(self):
        from test_f3_result_sync import ResultSyncTests
        from football_model.prospective import _objects
        case = ResultSyncTests()
        case.setUp()
        try:
            self.assertEqual(case.sync()[0], 0)
            result = _objects(case.root/"prospective", "results")[0]
            self.record.update(match_id=str(result["match_id"]), competition=result["competition"],
                               home_id=result["home_id"], away_id=result["away_id"], kickoff_at=result["kickoff"])
            self.spec["scope"]["competitions"] = "all"
            self.protocol = self.register()
            self.build()
            result_path = case.root/"prospective"/"results"/(result["object_id"]+".json")
            source_path = case.root/"prospective"/"sources"/(result["source_id"]+".json")
            descriptor = {"path": "results/"+result_path.name, "sha256": sha(result_path.read_bytes()),
                          "source_evidence": [{"path": "sources/"+source_path.name, "sha256": sha(source_path.read_bytes())}]}
            score = self.score(result=descriptor, result_source_root=case.root/"prospective", output_dir=self.root/"synced-score")
            self.assertEqual(score["result_source_status"], "native_normalized_parent_verified")
            self.assertEqual(score["outcome"]["settlement_state"], "loss")
            self.assertEqual(read_decision_score(self.root/"synced-score", score["manifest_sha256"])["verification_status"], "verified")
            parent = json.loads(source_path.read_text())
            parent["content"]["response"][0]["score"]["fulltime"] = {"home": 0, "away": 0}
            blob = (json.dumps(parent)+"\n").encode()
            (self.root/"bad-parent.json").write_bytes(blob)
            bad_result = {k:v for k,v in result.items() if k != "object_id"}
            bad_result["source_id"] = sha(blob)
            bad_descriptor = self.external("bad-result.json", bad_result)
            bad_descriptor["source_evidence"] = [{"path": "bad-parent.json", "sha256": sha(blob)}]
            with self.assertRaisesRegex(EvidenceError, "native_fixture_response_hash_mismatch"):
                self.score(result=bad_descriptor)
        finally:
            case.doCleanups()


if __name__ == "__main__":
    unittest.main()
