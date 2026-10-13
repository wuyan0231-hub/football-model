"""Future scoring uses verified captures, genuine status and regulation scores."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from football_model.feature_correction import fit_correction, predict_correction
from football_model.feature_scoring import score_run
from football_model.feature_snapshots import (_digest, snapshot_from_evidence,
                                             verify_capture, verify_baseline_capture)
from football_model.fundamentals import build_features, make_snapshot
from football_model.model import _distribution_from_rates

SHA = "a" * 40
KICKOFF = datetime(2025, 1, 1, 18, tzinfo=timezone.utc)
PROTOCOL = {"version": "score-test-v1", "confirmation_active": False}
QUEUE_PROTOCOL = {"version": "queue-scoring-contract-test", "confirmation_active": False,
                  "future_scoring_denominator": "all_queue"}
CONFIG = {"api_daily_limit": 5000, "max_requests_per_run": None,
          "competitions": [{"key": "serie_a", "name": "Serie A", "country": "Italy"}]}


def iso(value):
    return value.isoformat().replace("+00:00", "Z")


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    return {"path": path.as_posix(), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def fixture(status="FT", goals=(2, 1), at=None):
    return {"endpoint": "/fixtures", "params": {"id": 100}, "errors": [], "paging": {"total": 1},
            "fetched_at": iso(at or KICKOFF + timedelta(hours=5)), "from_cache": False,
            "response": [{"fixture": {"id": 100, "date": iso(KICKOFF), "status": {"short": status}},
                "league": {"id": 135, "name": "Serie A", "country": "Italy"},
                "teams": {"home": {"id": 1, "name": "Home"}, "away": {"id": 2, "name": "Away"}},
                "goals": {"home": 9, "away": 8},
                "score": {"fulltime": {"home": goals[0], "away": goals[1]},
                          "halftime": {"home": 0, "away": 0},
                          "extratime": {"home": 7, "away": 6}, "penalty": {"home": 5, "away": 4}}}]}


class FakeClient:
    def __init__(self, envelope):
        self.envelope = envelope
        self.requests_made = 0
        self.daily_remaining = self.daily_remaining_reported = None
        self.daily_quota_status = "unknown"
        self.calls = []

    def status(self):
        self.requests_made += 1
        return {"requests": {}}

    def fetch_fixture(self, match_id, **kwargs):
        self.calls.append((match_id, kwargs))
        self.requests_made += 1
        return deepcopy(self.envelope)


class FeatureScoringTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        rows = []
        for mid, day in ((1, 1), (2, 2)):
            kickoff = datetime(2024, 12, day, 18, tzinfo=timezone.utc)
            cutoff = kickoff - timedelta(hours=10)
            for side in ("home", "away"):
                rows.append({"match_id": mid, "side": side, "kickoff": iso(kickoff),
                    "cutoff_at": iso(cutoff), "baseline_training_cutoff": iso(cutoff-timedelta(days=1)),
                    "target_available_at": iso(kickoff+timedelta(hours=4)), "goals": 1, "lambda0": 1.0,
                    "features": {"signal": 0.0}, "feature_available_at": {"signal": iso(cutoff)}})
        cls.model = fit_correction(rows, ["signal"], alpha=0.1,
                                   cutoff="2024-12-10T00:00:00Z", min_rows=4)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path.cwd())
        self.runtime = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)

    def capture(self, horizon="EARLY", *, protocol=PROTOCOL, candidate_predicted=True):
        cutoff = KICKOFF - timedelta(hours=10) if horizon == "EARLY" else KICKOFF - timedelta(minutes=30)
        game = {"match_id": 100, "kickoff": iso(KICKOFF), "home_id": 1, "away_id": 2, "status": "NS"}
        root = self.runtime / "feature_snapshots" / _digest(protocol)
        context = {"fixture_response": fixture("NS", at=cutoff)}
        context_path = root / "sources" / (horizon + "-context.json")
        write(context_path, context)
        card = {"correction_model": self.model, "model_hash": _digest(self.model),
                "generated_at": "2024-12-10T00:00:00Z"}
        card_path = root / "sources" / "card.json"
        write(card_path, card)
        def ref(path, kind):
            return {"path": str(path), "runtime_relative_path": path.relative_to(self.runtime).as_posix(),
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "available_at": iso(cutoff), "kind": kind}
        refs = [ref(context_path, "current_fixture_context"), ref(card_path, "candidate_model")]
        snapshots = []
        if horizon == "LINEUP":
            raw = [{"team": {"id": tid}, "startXI": [{"player": {"id": tid*100+i}} for i in range(11)],
                    "substitutes": []} for tid in (1, 2)]
            lineup = make_snapshot("lineups", {"endpoint": "/fixtures/lineups", "params": {"fixture": 100},
                "response": raw, "errors": [], "fetched_at": iso(cutoff)}, game, context_observed_at=cutoff)
            lineup_path = root / "sources" / "lineup.json"
            write(lineup_path, lineup)
            source = ref(lineup_path, "fundamentals")
            source["snapshot_id"] = lineup["snapshot_id"]
            refs.append(source)
            snapshots.append(lineup)
        features = build_features(game, cutoff=cutoff, snapshots=snapshots)
        baseline = _distribution_from_rates(1.5, 1.1)
        baseline.update(match_id=100, kickoff=iso(KICKOFF), cutoff_at=iso(cutoff),
                        training_cutoff="2024-12-10T00:00:00Z")
        packet = {"fixture": game, "prediction": baseline}
        payload = snapshot_from_evidence(game, packet, features, horizon=horizon,
            cutoff_at=cutoff, source_refs=refs, protocol=protocol, clock=lambda: cutoff)
        prediction = predict_correction(self.model, baseline, {"signal": 0.0}, {"signal": 0.0})
        payload.update(league="serie_a", candidate_status="predicted", candidate={
            "model_hash": card["model_hash"], "prediction": prediction,
            "feature_values": {"home": {"signal": 0.0}, "away": {"signal": 0.0}}})
        if not candidate_predicted:
            payload.update(candidate_status="not_run", candidate=None,
                           candidate_missing_reason="candidate_schedule_incomplete")
        payload.pop("snapshot_id")
        payload["snapshot_id"] = _digest(payload)
        suffix = "-candidate" if candidate_predicted else "-baseline"
        payload_path = root / "payloads" / (horizon + suffix + ".json")
        write(payload_path, payload)
        def relative(path):
            return {"path": path.relative_to(self.runtime).as_posix(), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        receipt = {"league": "serie_a", "match_id": 100, "horizon": horizon,
            "protocol_sha256": _digest(protocol), "payload_ref": relative(payload_path),
            "capture_started_at": iso(cutoff), "storage_completed_at": iso(cutoff+timedelta(seconds=1)),
            "status": "captured", "live_capture_verified": True, "confirmation_eligible": False}
        self.assertEqual(verify_capture(self.runtime, receipt), payload)
        receipt_path = root / "captures" / "serie_a" / "100" / horizon / "versions" / ("receipt"+suffix+".json")
        write(receipt_path, receipt)
        anchor = receipt_path.parent.parent / "first_candidate.json"
        if candidate_predicted:
            write(anchor, relative(receipt_path))
        first = anchor.with_name("first.json")
        if not first.exists():
            write(first, relative(receipt_path))
        self.queue_observation(protocol=protocol, horizon=horizon, status="captured" if candidate_predicted else "ineligible",
            receipt_ref=relative(receipt_path), evidence_status="captured",
            candidate_status="predicted" if candidate_predicted else "not_run",
            reason=None if candidate_predicted else "candidate_schedule_incomplete")
        return anchor, receipt_path, payload_path

    def queue_observation(self, *, protocol=QUEUE_PROTOCOL, match_id=100, horizon="EARLY",
                          status="ineligible", kickoff=KICKOFF, observed=None, **extras):
        value = {"league": "serie_a", "match_id": match_id, "horizon": horizon,
            "kickoff": iso(kickoff), "queue_observed_at": iso(observed or kickoff-timedelta(hours=10)),
            "planned_state_at_observation": "due", "status": status, **extras}
        root = self.runtime / "feature_snapshots" / _digest(protocol)
        path = root / "queue/history" / (_digest(value)+".json")
        write(path, value)
        first = root / "captures" / "serie_a" / str(match_id) / horizon / "first_attempt.json"
        if not first.exists() and value["planned_state_at_observation"] in ("due", "late"):
            write(first, {"path": path.relative_to(self.runtime).as_posix(),
                          "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
        return path

    def run_score(self, now, client=None, *, protocol=PROTOCOL):
        return score_run(self.runtime, config=CONFIG, protocol=protocol, source_commit=SHA,
                         clock=lambda: now, client_factory=lambda *a, **k: client)

    def test_empty_and_immature_queues_create_no_api_client(self):
        with patch("football_model.feature_scoring.APIClient", side_effect=AssertionError("API must not initialize")):
            code, report = score_run(self.runtime, config=CONFIG, protocol=PROTOCOL, source_commit=SHA,
                                    clock=lambda: KICKOFF)
        self.assertEqual(code, 0)
        self.assertEqual(report["api_requests"], 0)
        self.assertEqual(report["current_scores_by_horizon"]["EARLY"]["n"], 0)
        self.assertIsNone(report["current_scores_by_horizon"]["EARLY"]["mean_paired_scoreline_nll_difference"])
        self.capture()
        code, report = self.run_score(KICKOFF+timedelta(hours=3))
        self.assertEqual(code, 0)
        self.assertEqual(report["queue_status_counts"], {"pending": 1})
        self.assertEqual(report["api_requests"], 0)

    def test_two_verified_horizons_share_one_query_and_one_independent_fixture(self):
        self.capture()
        self.capture("LINEUP")
        client = FakeClient(fixture())
        code, report = self.run_score(KICKOFF+timedelta(hours=5), client)
        self.assertEqual(code, 0)
        self.assertEqual(client.calls, [(100, {"force": True, "max_age_seconds": 0})])
        self.assertEqual(report["scored_independent_matches"], 1)
        self.assertEqual(report["scored_horizon_records"], 2)
        self.assertEqual(report["api_requests"], 2)  # status plus one result
        self.assertIsNone(report["api_daily_remaining"])
        self.assertEqual(report["api_quota_status"], "unknown")
        for horizon in ("EARLY", "LINEUP"):
            metrics = report["current_scores_by_horizon"][horizon]
            self.assertEqual(metrics["n"], 1)
            self.assertEqual(metrics["mean_paired_scoreline_nll_difference"], 0)
        self.assertFalse(report["confirmation_eligible"])
        self.assertEqual(report["confirmation_independent_matches"], 0)
        self.assertIsNone(report["betting_returns"])
        again, repeated = self.run_score(KICKOFF+timedelta(hours=6))
        self.assertEqual(again, 0)
        self.assertEqual(repeated["api_requests"], 0)
        self.assertEqual(repeated["queue_status_counts"], {"already_scored": 2})

    def test_aet_and_penalty_use_only_explicit_fulltime_score(self):
        self.capture()
        client = FakeClient(fixture("PEN", goals=(1, 1)))
        code, report = self.run_score(KICKOFF+timedelta(hours=5), client)
        self.assertEqual(code, 0)
        ref = report["queue"][0]["first_score_ref"]
        saved = json.loads((self.runtime / ref["path"]).read_text())
        self.assertEqual((saved["result"]["home_goals"], saved["result"]["away_goals"]), (1, 1))
        self.assertEqual(saved["provider_status"], "PEN")
        self.assertEqual(saved["scores"]["M0"]["events"]["draw"]["observed"], 1)
        # Explicitly absent regulation score cannot be replaced by goals,
        # extra time, shootout or eventual winner.
        later = fixture("AET", at=KICKOFF+timedelta(hours=30))
        later["response"][0]["score"]["fulltime"] = {"home": None, "away": None}
        code, report = self.run_score(KICKOFF+timedelta(hours=30), FakeClient(later))
        self.assertEqual(code, 1)
        self.assertEqual(report["queue_status_counts"], {"failed": 1})

    def test_wrong_team_league_and_kickoff_are_never_scored(self):
        self.capture()
        for field in ("team", "league", "kickoff"):
            response = fixture()
            raw = response["response"][0]
            if field == "team": raw["teams"]["away"]["id"] = 99
            elif field == "league": raw["league"]["id"] = 136
            else: raw["fixture"]["date"] = iso(KICKOFF+timedelta(minutes=1))
            with self.subTest(field=field):
                code, report = self.run_score(KICKOFF+timedelta(hours=5), FakeClient(response))
                self.assertEqual(code, 1)
                self.assertEqual(report["requested_horizon_records"], 1)
                self.assertEqual(report["scored_horizon_records"], 0)

    def test_capture_fingerprint_tampering_fails_before_api(self):
        _, receipt, _ = self.capture()
        receipt.write_text(receipt.read_text()+" ", encoding="utf-8")
        code, report = self.run_score(KICKOFF+timedelta(hours=5))
        self.assertEqual(code, 1)
        self.assertEqual(report["api_requests"], 0)
        self.assertEqual(report["queue_status_counts"], {"failed": 1})
        self.assertIn("hash_mismatch", report["issues"][0]["reason"])

    def test_future_saved_capture_cannot_be_scored_by_past_clock(self):
        anchor, receipt_path, _ = self.capture()
        receipt = json.loads(receipt_path.read_text())
        receipt["storage_completed_at"] = iso(KICKOFF+timedelta(days=2))
        write(receipt_path, receipt)
        write(anchor, {"path": receipt_path.relative_to(self.runtime).as_posix(),
                       "sha256": hashlib.sha256(receipt_path.read_bytes()).hexdigest()})
        code, report = self.run_score(KICKOFF+timedelta(hours=5))
        self.assertEqual(code, 1)
        self.assertEqual(report["api_requests"], 0)
        self.assertEqual(report["scored_horizon_records"], 0)

    def test_result_revisions_append_and_preserve_exact_first_score_bytes(self):
        self.capture()
        code, first = self.run_score(KICKOFF+timedelta(hours=5), FakeClient(fixture()))
        self.assertEqual(code, 0)
        first_ref = first["queue"][0]["first_score_ref"]
        path = self.runtime / first_ref["path"]
        original = path.read_bytes()
        later = KICKOFF+timedelta(hours=30)
        code, report = self.run_score(later, FakeClient(fixture(goals=(1, 1), at=later)))
        self.assertEqual(code, 0)
        self.assertEqual(report["queue_status_counts"], {"result_revision_appended": 1})
        self.assertEqual(report["queue"][0]["first_score_ref"], first_ref)
        self.assertEqual(path.read_bytes(), original)
        self.assertNotEqual(report["queue"][0]["current_score_ref"], first_ref)
        self.assertNotEqual(report["first_scores_by_horizon"]["EARLY"]["baseline"]["one_x_two_nll"],
                            report["current_scores_by_horizon"]["EARLY"]["baseline"]["one_x_two_nll"])

    def test_unfinished_status_and_future_result_observation_remain_unscored(self):
        self.capture()
        code, report = self.run_score(KICKOFF+timedelta(hours=5), FakeClient(fixture("SUSP")))
        self.assertEqual(code, 0)
        self.assertEqual(report["queue_status_counts"], {"pending_result": 1})
        self.assertEqual(report["scored_horizon_records"], 0)
        code, report = self.run_score(KICKOFF+timedelta(hours=5),
                                     FakeClient(fixture(at=KICKOFF+timedelta(hours=6))))
        self.assertEqual(code, 1)
        self.assertEqual(report["scored_horizon_records"], 0)

    def test_all_queue_denominator_keeps_failures_rejections_windows_and_fallbacks(self):
        self.capture(protocol=QUEUE_PROTOCOL, candidate_predicted=False)
        self.capture("LINEUP", protocol=QUEUE_PROTOCOL)
        now = KICKOFF+timedelta(hours=5)
        due_kickoff = now+timedelta(hours=10)
        self.queue_observation(match_id=200, kickoff=due_kickoff, status="failed", reason="capture_worker_failed")
        self.queue_observation(match_id=201, kickoff=due_kickoff, status="unsupported", reason="unknown_team")
        self.queue_observation(match_id=202, status="late", reason="horizon_late")
        self.queue_observation(match_id=203, kickoff=now+timedelta(hours=20), status="not_due",
                               observed=now, planned_state_at_observation="not_due")
        client = FakeClient(fixture())
        code, report = self.run_score(now, client, protocol=QUEUE_PROTOCOL)
        self.assertEqual(code, 0, report)
        self.assertEqual(report["requested_horizon_records"], 6)
        self.assertEqual(report["identified_independent_matches"], 5)
        self.assertEqual(report["scored_independent_matches"], 1)
        self.assertEqual(report["scored_horizon_records"], 2)
        self.assertEqual(client.calls, [(100, {"force": True, "max_age_seconds": 0})])
        self.assertEqual(report["opportunity_status_counts"], {
            "baseline_only": 1, "success": 1, "failed": 1, "rejected": 1,
            "missed_window": 1, "pending_capture": 1})
        early = report["current_scores_by_horizon"]["EARLY"]
        self.assertEqual(early["requested_horizon_records"], 5)
        self.assertEqual((early["baseline_scored_n"], early["candidate_scored_n"], early["strategy_scored_n"]), (1, 0, 1))
        self.assertEqual(early["baseline_fallback_n"], 1)
        self.assertIsNone(early["mean_paired_scoreline_nll_difference"])
        self.assertEqual(early["baseline_all_available"], early["strategy_with_fallback"])
        self.assertEqual(report["current_scores_by_horizon"]["LINEUP"]["paired_scored_n"], 1)
        self.assertFalse(report["confirmation_eligible"])
        self.assertIsNone(report["betting_returns"])

    def test_first_baseline_failure_is_not_replaced_by_later_first_candidate(self):
        _, _, payload_path = self.capture(protocol=QUEUE_PROTOCOL, candidate_predicted=False)
        original = payload_path.read_bytes()
        first_anchor = payload_path.parent.parent / "captures/serie_a/100/EARLY/first.json"
        original_anchor = first_anchor.read_bytes()
        later_anchor, _, _ = self.capture(protocol=QUEUE_PROTOCOL)
        self.assertTrue(later_anchor.exists())
        code, report = self.run_score(KICKOFF+timedelta(hours=5), FakeClient(fixture()), protocol=QUEUE_PROTOCOL)
        self.assertEqual(code, 0, report)
        self.assertEqual(report["requested_horizon_records"], 1)
        item = report["queue"][0]
        self.assertEqual(item["observation_count"], 2)
        self.assertEqual(item["first_attempt_status"], "ineligible")
        self.assertEqual(item["opportunity_status"], "baseline_only")
        self.assertEqual(item["strategy_choice"], "M0_fallback")
        self.assertEqual(item["fallback_reason"], "candidate_schedule_incomplete")
        saved = json.loads((self.runtime/item["first_score_ref"]["path"]).read_text())
        self.assertIsNone(saved["scores"]["M1"])
        self.assertEqual(saved["scores"]["M0"], saved["scores"]["M_strategy"])
        self.assertIn("total_goal_nll", saved["scores"]["M0"])
        self.assertEqual(payload_path.read_bytes(), original)
        self.assertEqual(first_anchor.read_bytes(), original_anchor)

    def test_candidate_parent_corruption_falls_back_without_erasing_legal_baseline(self):
        self.capture(protocol=QUEUE_PROTOCOL)
        card = self.runtime/"feature_snapshots"/_digest(QUEUE_PROTOCOL)/"sources/card.json"
        card.write_text(card.read_text()+" ", encoding="utf-8")
        code, report = self.run_score(KICKOFF+timedelta(hours=5), FakeClient(fixture()), protocol=QUEUE_PROTOCOL)
        self.assertEqual(code, 0, report)
        item = report["queue"][0]
        self.assertTrue(item["baseline_available"])
        self.assertFalse(item["candidate_available"])
        self.assertEqual(item["result_status"], "scored")
        self.assertIn("candidate_rejected:", item["fallback_reason"])
        self.assertEqual(report["current_scores_by_horizon"]["EARLY"]["baseline_fallback_n"], 1)

    def test_candidate_damage_after_first_success_preserves_m0_and_original_m1_artifact(self):
        self.capture(protocol=QUEUE_PROTOCOL)
        code, original = self.run_score(KICKOFF+timedelta(hours=5), FakeClient(fixture()), protocol=QUEUE_PROTOCOL)
        self.assertEqual(code, 0, original)
        first_ref = original["queue"][0]["first_score_ref"]
        original_bytes = (self.runtime/first_ref["path"]).read_bytes()
        card = self.runtime/"feature_snapshots"/_digest(QUEUE_PROTOCOL)/"sources/card.json"
        original_card_bytes = card.read_bytes()
        card.write_text(card.read_text()+" ", encoding="utf-8")
        code, current = self.run_score(KICKOFF+timedelta(hours=6), protocol=QUEUE_PROTOCOL)
        self.assertEqual(code, 0, current)
        self.assertEqual(current["scored_horizon_records"], 1)
        item = current["queue"][0]
        self.assertEqual(item["result_status"], "scored")
        self.assertEqual(item["strategy_choice"], "M1")
        self.assertTrue(item["candidate_currently_unverified"])
        self.assertFalse(item["strategy_scored"])
        summary = current["current_scores_by_horizon"]["EARLY"]
        self.assertEqual(summary["baseline_scored_n"], 1)
        self.assertEqual(summary["candidate_scored_n"], 0)
        self.assertEqual(summary["strategy_scored_n"], 0)
        self.assertEqual(summary["baseline_fallback_n"], 0)
        self.assertEqual(summary["original_candidate_scores_currently_unverified_n"], 1)
        self.assertEqual((self.runtime/first_ref["path"]).read_bytes(), original_bytes)
        # A later genuine result correction still scores M0 independently;
        # it neither rewrites the original strategy nor approves broken M1.
        later = KICKOFF+timedelta(hours=30)
        code, revised = self.run_score(later, FakeClient(fixture(goals=(1, 1), at=later)), protocol=QUEUE_PROTOCOL)
        self.assertEqual(code, 0, revised)
        self.assertEqual(revised["queue"][0]["status"], "result_revision_appended")
        self.assertEqual(revised["queue"][0]["first_score_ref"], first_ref)
        self.assertEqual(revised["current_scores_by_horizon"]["EARLY"]["baseline_scored_n"], 1)
        self.assertEqual(revised["current_scores_by_horizon"]["EARLY"]["strategy_scored_n"], 0)
        self.assertEqual((self.runtime/first_ref["path"]).read_bytes(), original_bytes)
        revision_ref = revised["queue"][0]["current_score_ref"]
        revision_bytes = (self.runtime/revision_ref["path"]).read_bytes()
        card.write_bytes(original_card_bytes)
        code, restored = self.run_score(KICKOFF+timedelta(hours=31), protocol=QUEUE_PROTOCOL)
        self.assertEqual(code, 0, restored)
        self.assertTrue(restored["queue"][0]["candidate_reverified_projection"])
        self.assertEqual(restored["current_scores_by_horizon"]["EARLY"]["candidate_scored_n"], 1)
        self.assertEqual(restored["current_scores_by_horizon"]["EARLY"]["strategy_scored_n"], 1)
        self.assertEqual((self.runtime/first_ref["path"]).read_bytes(), original_bytes)
        self.assertEqual((self.runtime/revision_ref["path"]).read_bytes(), revision_bytes)

    def test_kickoff_conflict_preserves_old_first_score_locator_without_claiming_current_verification(self):
        self.capture(protocol=QUEUE_PROTOCOL)
        code, initial = self.run_score(KICKOFF+timedelta(hours=5), FakeClient(fixture()), protocol=QUEUE_PROTOCOL)
        self.assertEqual(code, 0)
        first_ref = initial["queue"][0]["first_score_ref"]
        old_bytes = (self.runtime/first_ref["path"]).read_bytes()
        self.queue_observation(kickoff=KICKOFF+timedelta(days=1), observed=KICKOFF+timedelta(hours=6),
                               status="not_due", planned_state_at_observation="not_due")
        code, report = self.run_score(KICKOFF+timedelta(hours=7), protocol=QUEUE_PROTOCOL)
        self.assertEqual(code, 1)
        self.assertEqual(report["scored_horizon_records"], 0)
        self.assertEqual(report["retained_historical_first_score_count"], 1)
        self.assertEqual(report["retained_historical_first_score_refs"][0]["score_ref"], first_ref)
        self.assertTrue(report["retained_historical_first_score_refs"][0]["current_verification_not_implied"])
        self.assertEqual((self.runtime/first_ref["path"]).read_bytes(), old_bytes)

    def test_strict_v2_pipeline_baseline_scores_from_its_frozen_parent_after_latest_is_removed(self):
        from football_model.cli import run_pipeline
        from football_model.feature_snapshots import capture_run
        from tests.test_cli import BASE_CONFIG, FakeClient as TrainingClient
        from tests.test_feature_snapshots import V2_PROTOCOL
        trained = datetime(2025, 1, 1, tzinfo=timezone.utc)
        at = trained+timedelta(hours=38)
        with patch("football_model.integration.datetime") as integration_clock:
            integration_clock.now.return_value = trained
            code, original = run_pipeline(BASE_CONFIG, self.runtime, "train", now=trained,
                                          client=TrainingClient(), clock=lambda: trained)
            self.assertEqual(code, 0, original)
            prediction_path = self.runtime/"predictions"/original["run_id"]/"epl.json"
            row = json.loads(prediction_path.read_text())[0]
            identity = {"fixture": {"id": row["match_id"], "date": row["kickoff"], "status": {"short": "NS"}},
                "teams": {"home": {"id": row["home_id"], "name": row["home_name"]},
                          "away": {"id": row["away_id"], "name": row["away_name"]}},
                "league": {"id": 999, "name": "Synthetic League", "country": "Test"}}
            write(self.runtime/"fundamentals/fixtures/2000/current.json", {"fixture_response": {
                "endpoint": "/fixtures", "params": {"id": row["match_id"]}, "errors": [],
                "fetched_at": iso(at), "response": [identity]}})
            integration_clock.now.return_value = at
            code, captured = capture_run(self.runtime, "epl", source_commit=SHA, protocol=V2_PROTOCOL,
                                        config=BASE_CONFIG, clock=lambda: at)
            self.assertEqual(code, 0, captured)
        self.assertEqual(captured["captured_horizon_records"], 1, captured)
        # The scorer consumes immutable originals rather than current pointers.
        prediction_path.unlink()
        (self.runtime/"models/epl.json").unlink()
        (self.runtime/"integration/index.json").unlink()
        kickoff = datetime.fromisoformat(row["kickoff"].replace("Z", "+00:00"))
        now = kickoff+timedelta(hours=5)
        result = deepcopy(identity)
        result["fixture"]["status"]["short"] = "FT"
        result["score"] = {"fulltime": {"home": 2, "away": 1}}
        envelope = {"endpoint": "/fixtures", "params": {"id": row["match_id"]}, "errors": [],
                    "fetched_at": iso(now), "response": [result]}
        client = FakeClient(envelope)
        code, scored = score_run(self.runtime, "epl", config=BASE_CONFIG, protocol=V2_PROTOCOL,
            source_commit=SHA, clock=lambda: now, client_factory=lambda *a, **k: client)
        self.assertEqual(code, 0, scored)
        self.assertEqual(scored["requested_horizon_records"], 2)
        self.assertEqual(scored["scored_horizon_records"], 1)
        self.assertEqual(scored["current_scores_by_horizon"]["EARLY"]["baseline_fallback_n"], 1)
        self.assertEqual(scored["opportunity_status_counts"], {"baseline_only": 1, "missed_window": 1})

    def test_result_failure_is_independent_and_does_not_shrink_queue_or_retained_score(self):
        self.capture(protocol=QUEUE_PROTOCOL, candidate_predicted=False)
        code, first = self.run_score(KICKOFF+timedelta(hours=5), FakeClient(fixture()), protocol=QUEUE_PROTOCOL)
        self.assertEqual(code, 0)
        old_ref = first["queue"][0]["first_score_ref"]
        old_bytes = (self.runtime/old_ref["path"]).read_bytes()
        later = KICKOFF+timedelta(hours=30)
        response = fixture(at=later)
        response["response"][0]["teams"]["away"]["id"] = 99
        code, report = self.run_score(later, FakeClient(response), protocol=QUEUE_PROTOCOL)
        self.assertEqual(code, 1)
        item = report["queue"][0]
        self.assertEqual(item["result_status"], "result_failed")
        self.assertEqual(item["opportunity_status"], "baseline_only")
        self.assertTrue(item["retained_verified_previous_score"])
        self.assertEqual(report["requested_horizon_records"], 1)
        self.assertEqual(report["scored_horizon_records"], 1)
        self.assertEqual(report["current_scores_by_horizon"]["EARLY"]["baseline_scored_n"], 1)
        self.assertEqual((self.runtime/old_ref["path"]).read_bytes(), old_bytes)

    def test_repeated_failed_polls_remain_one_fixture_horizon_opportunity(self):
        self.queue_observation(status="ineligible", reason="lineups_missing")
        self.queue_observation(status="ineligible", reason="candidate_unavailable",
                               observed=KICKOFF-timedelta(hours=9, minutes=30))
        self.queue_observation(horizon="LINEUP", status="late", observed=KICKOFF+timedelta(minutes=1),
                               planned_state_at_observation="late")
        with patch("football_model.feature_scoring.APIClient", side_effect=AssertionError("no legal forecast")):
            code, report = score_run(self.runtime, config=CONFIG, protocol=QUEUE_PROTOCOL, source_commit=SHA,
                                     clock=lambda: KICKOFF+timedelta(hours=5))
        self.assertEqual(code, 0, report)
        self.assertEqual(report["requested_horizon_records"], 2)
        self.assertEqual(report["identified_independent_matches"], 1)
        self.assertEqual(report["queue_status_counts"], {"missed_window": 2})
        self.assertEqual(report["queue"][0]["observation_count"], 2)
        self.assertEqual(len(report["queue"][0]["prior_failures_retained"]), 2)
        self.assertEqual(report["api_requests"], 0)

    def test_unbound_baseline_anchor_is_rejected_before_result_query(self):
        self.capture(protocol=QUEUE_PROTOCOL, candidate_predicted=False)
        root = self.runtime/"feature_snapshots"/_digest(QUEUE_PROTOCOL)
        for path in (root/"queue/history").glob("*.json"):
            path.unlink()
        (root/"captures/serie_a/100/EARLY/first_attempt.json").unlink()
        self.queue_observation(status="failed", reason="fixture_context_missing")
        code, report = self.run_score(KICKOFF+timedelta(hours=5), protocol=QUEUE_PROTOCOL)
        self.assertEqual(code, 1)
        self.assertEqual(report["api_requests"], 0)
        self.assertEqual(report["requested_horizon_records"], 1)
        self.assertEqual(report["scored_horizon_records"], 0)
        self.assertEqual(report["queue"][0]["reason"], "first_baseline_not_bound_to_queue_observation")


if __name__ == "__main__":
    unittest.main()
