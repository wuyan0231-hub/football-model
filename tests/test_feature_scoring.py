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
from football_model.feature_snapshots import _digest, snapshot_from_evidence, verify_capture
from football_model.fundamentals import build_features, make_snapshot
from football_model.model import _distribution_from_rates

SHA = "a" * 40
KICKOFF = datetime(2025, 1, 1, 18, tzinfo=timezone.utc)
PROTOCOL = {"version": "score-test-v1", "confirmation_active": False}
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

    def capture(self, horizon="EARLY"):
        cutoff = KICKOFF - timedelta(hours=10) if horizon == "EARLY" else KICKOFF - timedelta(minutes=30)
        game = {"match_id": 100, "kickoff": iso(KICKOFF), "home_id": 1, "away_id": 2, "status": "NS"}
        root = self.runtime / "feature_snapshots" / _digest(PROTOCOL)
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
            cutoff_at=cutoff, source_refs=refs, protocol=PROTOCOL, clock=lambda: cutoff)
        prediction = predict_correction(self.model, baseline, {"signal": 0.0}, {"signal": 0.0})
        payload.update(league="serie_a", candidate_status="predicted", candidate={
            "model_hash": card["model_hash"], "prediction": prediction,
            "feature_values": {"home": {"signal": 0.0}, "away": {"signal": 0.0}}})
        payload.pop("snapshot_id")
        payload["snapshot_id"] = _digest(payload)
        payload_path = root / "payloads" / (horizon + ".json")
        write(payload_path, payload)
        def relative(path):
            return {"path": path.relative_to(self.runtime).as_posix(), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        receipt = {"league": "serie_a", "match_id": 100, "horizon": horizon,
            "protocol_sha256": _digest(PROTOCOL), "payload_ref": relative(payload_path),
            "capture_started_at": iso(cutoff), "storage_completed_at": iso(cutoff+timedelta(seconds=1)),
            "status": "captured", "live_capture_verified": True, "confirmation_eligible": False}
        self.assertEqual(verify_capture(self.runtime, receipt), payload)
        receipt_path = root / "captures" / "serie_a" / "100" / horizon / "versions" / "receipt.json"
        write(receipt_path, receipt)
        anchor = receipt_path.parent.parent / "first_candidate.json"
        write(anchor, relative(receipt_path))
        return anchor, receipt_path, payload_path

    def run_score(self, now, client=None):
        return score_run(self.runtime, config=CONFIG, protocol=PROTOCOL, source_commit=SHA,
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


if __name__ == "__main__":
    unittest.main()
