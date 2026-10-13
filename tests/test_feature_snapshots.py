"""Real-clock and immutable-evidence safeguards; no provider calls or trades."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from football_model.feature_snapshots import (SnapshotError, _digest, capture_run,
    snapshot_from_evidence, verify_baseline_capture, verify_capture, verify_snapshot, _windows)
from football_model.fundamentals import build_features, make_snapshot, save_snapshot
from football_model.model import _distribution_from_rates

NOW = datetime(2025, 1, 1, 8, tzinfo=timezone.utc)
PROTOCOL = {"version": "schedule-offset-engineering-v1", "confirmation_active": False}
V2_PROTOCOL = {"schema_version": "feature-model-protocol-v2", "research_design_version": "feature-research-v2",
    "snapshot_source_validation": "rebuild_from_frozen_sources_v2", "future_scoring_denominator": "all_queue",
    "research_artifact_namespace": "protocol_hash_v2", "early_min_hours": 9, "early_max_hours": 11,
    "lineup_max_minutes": 75, "confirmation_active": False}
SHA = "a" * 40


def iso(value):
    return value.isoformat().replace("+00:00", "Z")


def fixture(kickoff=None):
    return {"match_id": 100, "kickoff": iso(kickoff or NOW + timedelta(hours=10)),
            "home_id": 1, "away_id": 2, "status": "NS"}


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    return path


class FeatureSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.runtime = Path(self.temp.name)
        self.game = fixture()
        self.prediction = _distribution_from_rates(1.5, 1.1)
        self.packet = {"fixture": self.game, "created_at": iso(NOW), "prediction": self.prediction,
            "provenance": {"model_file": "runtime/models/serie_a.json",
                "prediction_file": "runtime/predictions/test/serie_a.json",
                "report_file": "runtime/reports/history/test.json", "training_cutoff": iso(NOW - timedelta(days=1))}}
        self.source = write(self.runtime / "source.json", {"fetched_at": iso(NOW)})
        self.refs = [{"path": str(self.source), "sha256": hashlib.sha256(self.source.read_bytes()).hexdigest(),
                      "available_at": iso(NOW)}]
        self.features = build_features(self.game, cutoff=NOW, snapshots=[])

    def tearDown(self):
        self.temp.cleanup()

    def pure(self, **updates):
        kwargs = {"horizon": "EARLY", "cutoff_at": NOW, "source_refs": self.refs,
                  "protocol": PROTOCOL, "clock": lambda: NOW}
        kwargs.update(updates)
        return snapshot_from_evidence(self.game, self.packet, self.features, **kwargs)

    def lineup(self, at):
        raw = [{"team": {"id": tid}, "startXI": [{"player": {"id": tid * 100 + i}} for i in range(11)],
                "substitutes": [], "formation": "4-3-3"} for tid in (1, 2)]
        return make_snapshot("lineups", {"endpoint": "/fixtures/lineups", "params": {"fixture": 100},
            "response": raw, "fetched_at": iso(at)}, self.game, context_observed_at=at)

    def runtime_sources(self):
        self.rows = [{**self.game, "status": "exploratory_baseline", "prediction": self.prediction}]
        write(self.runtime / "integration/index.json", {"generated_at": iso(NOW)})
        write(self.runtime / "models/serie_a.json", {"training_cutoff": iso(NOW - timedelta(days=1))})
        write(self.runtime / "predictions/test/serie_a.json", self.rows)
        write(self.runtime / "reports/history/test.json", {"generated_at": iso(NOW)})
        write(self.runtime / "fundamentals/fixtures/100/current.json", {
            "fixture_response": {"endpoint": "/fixtures", "params": {"id": 100}, "fetched_at": iso(NOW),
                "response": [{"fixture": {"id": 100, "date": self.game["kickoff"], "status": {"short": "NS"}},
                              "teams": {"home": {"id": 1}, "away": {"id": 2}}}]}})
        self.entry = {"status": "available", "latest_successful_forecast": {"run_id": "test"}}

    def capture(self, at=NOW, *, clock=None):
        with patch("football_model.feature_snapshots._entry_for_match", return_value=self.entry), patch(
                "football_model.feature_snapshots.match_packet", return_value=self.packet):
            return capture_run(self.runtime, source_commit=SHA, protocol=PROTOCOL,
                               config={"competitions": []}, clock=clock or (lambda: at))

    def candidate_model(self):
        from football_model.feature_correction import fit_correction
        from football_model.feature_dataset import FEATURE_NAMES
        records = []
        for index in range(40):
            kickoff = NOW - timedelta(days=100 - index)
            cutoff = kickoff - timedelta(hours=10)
            for side in ("home", "away"):
                records.append({"match_id": index + 1, "side": side, "kickoff": iso(kickoff),
                    "cutoff_at": iso(cutoff), "baseline_training_cutoff": iso(cutoff - timedelta(days=1)),
                    "target_available_at": iso(kickoff + timedelta(hours=4)), "lambda0": 1.2, "goals": 1,
                    "features": {name: 1.0 for name in FEATURE_NAMES},
                    "feature_available_at": {name: iso(cutoff) for name in FEATURE_NAMES}})
        model = fit_correction(records, list(FEATURE_NAMES), alpha=0.1, cutoff=iso(NOW - timedelta(days=1)))
        card = {"schema_version": "feature-model-card-v1", "league": "serie_a",
            "candidate_id": "M1_schedule_offset_l2_v1", "generated_at": iso(NOW),
            "source_commit": SHA, "training_cutoff": model["training_cutoff"],
            "feature_names": list(FEATURE_NAMES), "correction_model": model,
            "model_hash": _digest(model), "protocol_hash": _digest(PROTOCOL),
            "usage": "engineering_shadow_only", "lineup_adjusted": False,
            "feature_scope": "same_competition"}
        path = write(self.runtime / "feature_models/models/serie_a/latest.json", card)
        matches = [{"match_id": 70 + i, "kickoff": iso(NOW - timedelta(days=7-i)),
            "home_id": 1 if i == 0 else 4, "away_id": 3 if i == 0 else 2,
            "home_name": "Synthetic Home", "away_name": "Synthetic Away",
            "home_goals": 1, "away_goals": 1, "neutral": False} for i in (0, 1)]
        write(self.runtime / "data/serie_a.json", {"observed_at": iso(NOW), "matches": matches})
        return path, card

    def allclub_sources(self):
        card_path, card = self.candidate_model()
        card["feature_scope"] = "all_club_competitions_provider_scope"
        write(card_path, card)
        matches = json.loads((self.runtime / "data/serie_a.json").read_text())["matches"]
        root = self.runtime / "feature_models/schedules/serie_a"

        def save_raw(name, value):
            path = write(root / "responses" / name, value)
            return {"path": str(path.relative_to(self.runtime)),
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "fetched_at": iso(NOW)}

        requests = []
        for index, match in enumerate(matches):
            envelope = {"endpoint": "/fixtures", "params": {"team": match["home_id"], "season": 2024},
                "fetched_at": iso(NOW), "response": [{"fixture": {"id": match["match_id"],
                    "date": match["kickoff"], "status": {"short": "FT"}},
                    "teams": {"home": {"id": match["home_id"]}, "away": {"id": match["away_id"]}},
                    "league": {"id": 135, "season": 2024}}]}
            ref = save_raw(f"fixture{index}.json", envelope)
            requests.append({"status": "collected", "evidence": ref})
            match.update(status="FT", league_id=135, season=2024, competition_type="League",
                         observed_at=iso(NOW), received_at=iso(NOW), source_response_sha256=ref["sha256"])
        metadata = {"endpoint": "/leagues", "params": {"id": 135, "season": 2024},
            "fetched_at": iso(NOW), "response": [{"league": {"id": 135, "name": "Synthetic Serie A", "type": "League"},
                                                    "seasons": [{"year": 2024}]}]}
        metadata_ref = save_raw("metadata.json", metadata)
        coverage = {"scope": card["feature_scope"], "provider_queries_complete": True,
            "all_club_competitions_complete": False, "team_season_requests": requests,
            "league_season_audit": [], "workload_events": matches,
            "competition_metadata": [{"league_id": 135, "season": 2024, "friendly": False,
                                      "name": "Synthetic Serie A", "evidence": metadata_ref}]}
        latest = write(root / "latest.json", {"version": "club-schedule-collection-1.0",
            "league": "serie_a", "generated_at": iso(NOW), "collection_finished_at": iso(NOW),
            "schedule_matches": matches, "coverage": coverage})
        return latest, requests

    def real_v2_capture(self, *, allclub=False, with_candidate=True, horizon="EARLY", with_injuries=False):
        from football_model.cli import run_pipeline
        from tests.test_cli import BASE_CONFIG, FakeClient
        trained = datetime(2025, 1, 1, tzinfo=timezone.utc)
        at = trained + timedelta(hours=38 if horizon == "EARLY" else 47.5)
        with patch("football_model.integration.datetime") as integration_clock:
            integration_clock.now.return_value = trained
            code, original = run_pipeline(BASE_CONFIG, self.runtime, "train", now=trained,
                client=FakeClient(), clock=lambda: trained)
            self.assertEqual(code, 0, original)
            row = json.loads((self.runtime / "predictions" / original["run_id"] / "epl.json").read_text())[0]
            integration_clock.now.return_value = at
            write(self.runtime / "fundamentals/fixtures/2000/current.json", {
                "fixture_response": {"endpoint": "/fixtures", "params": {"id": 2000}, "fetched_at": iso(at),
                    "response": [{"fixture": {"id": 2000, "date": row["kickoff"], "status": {"short": "NS"}},
                                  "teams": {"home": {"id": row["home_id"]}, "away": {"id": row["away_id"]}}}]}})
            current = {**row, "status": "NS"}
            if horizon == "LINEUP":
                raw = [{"team": {"id": tid}, "startXI": [{"player": {"id": tid * 100 + i}}
                    for i in range(11)], "substitutes": [], "formation": "4-3-3"}
                    for tid in (row["home_id"], row["away_id"])]
                save_snapshot(self.runtime / "fundamentals/snapshots", make_snapshot("lineups", {
                    "endpoint": "/fixtures/lineups", "params": {"fixture": row["match_id"]},
                    "response": raw, "fetched_at": iso(at)}, current, context_observed_at=at))
            if with_injuries:
                save_snapshot(self.runtime / "fundamentals/snapshots", make_snapshot("injuries", {
                    "endpoint": "/injuries", "params": {"fixture": row["match_id"]},
                    "response": [], "fetched_at": iso(at)}, current, context_observed_at=at))
            if with_candidate:
                if allclub:
                    schedule, _ = self.allclub_sources()
                    copied = json.loads(schedule.read_text())
                    copied["league"] = "epl"
                    write(self.runtime / "feature_models/schedules/epl/latest.json", copied)
                    card = json.loads((self.runtime / "feature_models/models/serie_a/latest.json").read_text())
                else:
                    _, card = self.candidate_model()
                card.update(league="epl", protocol_hash=_digest(V2_PROTOCOL))
                write(self.runtime / "feature_models/models/epl" / _digest(V2_PROTOCOL) / "latest.json", card)
            _, report = capture_run(self.runtime, "epl", source_commit=SHA, protocol=V2_PROTOCOL,
                                    config=BASE_CONFIG, clock=lambda: at)
        item = next(item for item in report["queue"] if item["horizon"] == horizon)
        self.assertEqual(item["evidence_status"], "captured", report)
        receipt = json.loads((self.runtime / item["receipt_ref"]["path"]).read_text())
        return verify_capture(self.runtime, receipt), receipt

    def resave_payload(self, payload, receipt):
        payload["snapshot_id"] = _digest({k: v for k, v in payload.items() if k != "snapshot_id"})
        path = self.runtime / receipt["payload_ref"]["path"]
        write(path, payload)
        receipt["payload_ref"]["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()

    def test_protocol_top_level_windows_are_used_and_conflicts_rejected(self):
        self.assertEqual(_windows(V2_PROTOCOL)["lineup_minutes_max"], 75)
        p = {**V2_PROTOCOL, "early_min_hours": 8.5, "early_max_hours": 12}
        self.assertEqual(_windows(p)["early_hours_min"], 8.5)
        self.assertEqual(_windows(p)["early_hours_max"], 12)
        with self.assertRaisesRegex(SnapshotError, "conflicting_capture_windows"):
            _windows({**p, "capture_windows": {"early_hours_min": 9}})
        with self.assertRaisesRegex(SnapshotError, "invalid_capture_windows"):
            _windows({**p, "lineup_max_minutes": True})

    def test_v2_protocol_cannot_disable_source_validation(self):
        p = deepcopy(V2_PROTOCOL)
        p.pop("snapshot_source_validation")
        with self.assertRaisesRegex(SnapshotError, "snapshot_source_validation_required"):
            self.pure(protocol=p)

    def test_v2_actual_parent_and_features_replay_after_latest_files_are_removed(self):
        payload, receipt = self.real_v2_capture(allclub=True)
        self.assertEqual(payload["baseline_parent_row"]["prediction"], payload["baseline"]["prediction"])
        for ref in payload["source_refs"]:
            original = self.runtime / ref["origin_runtime_path"]
            if original.exists():
                original.unlink()
        self.assertTrue(verify_capture(self.runtime, receipt))

    def test_v2_fabricated_features_are_rejected_even_with_consistent_candidate_arithmetic(self):
        from football_model.feature_correction import predict_correction
        payload, receipt = self.real_v2_capture()
        ref = next(r for r in payload["source_refs"] if r["kind"] == "candidate_model")
        model = json.loads((self.runtime / ref["runtime_relative_path"]).read_text())["correction_model"]
        candidate = payload["candidate"]
        candidate["feature_values"]["home"]["matches_last14d"] += 1
        candidate["prediction"] = predict_correction(model, candidate["prediction"]["baseline_prediction"],
            candidate["feature_values"]["home"], candidate["feature_values"]["away"])
        self.resave_payload(payload, receipt)
        self.assertTrue(verify_baseline_capture(self.runtime, receipt))
        with self.assertRaisesRegex(SnapshotError, "candidate_features_not_rebuilt_from_frozen_sources"):
            verify_capture(self.runtime, receipt)

    def test_v2_candidate_baseline_must_be_the_same_production_parent(self):
        from football_model.feature_correction import predict_correction
        payload, receipt = self.real_v2_capture()
        ref = next(r for r in payload["source_refs"] if r["kind"] == "candidate_model")
        model = json.loads((self.runtime / ref["runtime_relative_path"]).read_text())["correction_model"]
        candidate = payload["candidate"]
        fake = deepcopy(candidate["prediction"]["baseline_prediction"])
        fake["match_id"] += 1
        candidate["prediction"] = predict_correction(model, fake,
            candidate["feature_values"]["home"], candidate["feature_values"]["away"])
        self.resave_payload(payload, receipt)
        self.assertTrue(verify_baseline_capture(self.runtime, receipt))
        with self.assertRaisesRegex(SnapshotError, "candidate_baseline_not_same_frozen_parent"):
            verify_capture(self.runtime, receipt)

    def test_v2_full_parent_row_identity_and_original_function_are_verified(self):
        payload, receipt = self.real_v2_capture()
        payload["baseline_parent_row"]["home_name"] = "invented same ID team name"
        self.resave_payload(payload, receipt)
        with self.assertRaisesRegex(SnapshotError, "baseline_parent_full_row_mismatch"):
            verify_baseline_capture(self.runtime, receipt)

    def test_v2_rehashing_an_altered_model_does_not_bypass_original_function_replay(self):
        payload, receipt = self.real_v2_capture()
        ref = next(r for r in payload["source_refs"] if r["kind"] == "baseline_model")
        path = self.runtime / ref["runtime_relative_path"]
        bundle = json.loads(path.read_text())
        bundle["model"]["intercept"] += 0.1
        write(path, bundle)
        ref["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        payload["baseline"]["provenance"]["model_file_sha256"] = ref["sha256"]
        self.resave_payload(payload, receipt)
        with self.assertRaisesRegex(SnapshotError, "baseline_original_function_mismatch"):
            verify_baseline_capture(self.runtime, receipt)

    def test_v2_broken_candidate_source_preserves_a_verified_baseline(self):
        payload, receipt = self.real_v2_capture()
        ref = next(r for r in payload["source_refs"] if r["kind"] == "candidate_schedule_history")
        (self.runtime / ref["runtime_relative_path"]).unlink()
        self.assertTrue(verify_baseline_capture(self.runtime, receipt))
        with self.assertRaisesRegex(SnapshotError, "source_file_hash_mismatch"):
            verify_capture(self.runtime, receipt)

    def test_v2_first_source_capture_without_candidate_retains_real_baseline(self):
        payload, receipt = self.real_v2_capture(with_candidate=False)
        self.assertEqual(payload["candidate_status"], "model_unavailable")
        self.assertEqual(receipt["status"], "captured")
        self.assertTrue(verify_baseline_capture(self.runtime, receipt))
        root = self.runtime / "feature_snapshots" / _digest(V2_PROTOCOL) / "captures/epl/2000/EARLY"
        self.assertTrue((root / "first.json").exists())
        self.assertFalse((root / "first_candidate.json").exists())

    def test_v2_fundamental_payload_claims_are_rebuilt_from_preserved_sources(self):
        payload, receipt = self.real_v2_capture()
        payload["features"]["teams"]["1"]["lineups"] = {"complete_starting_eleven": True,
                                                                 "starters": []}
        self.resave_payload(payload, receipt)
        self.assertTrue(verify_baseline_capture(self.runtime, receipt))
        with self.assertRaisesRegex(SnapshotError, "fundamental_features_not_rebuilt_from_frozen_sources"):
            verify_capture(self.runtime, receipt)

    def test_v2_lineup_parent_breakage_rejects_the_entire_horizon_not_just_candidate(self):
        payload, receipt = self.real_v2_capture(horizon="LINEUP")
        self.assertTrue(verify_baseline_capture(self.runtime, receipt))
        ref = next(ref for ref in payload["source_refs"] if ref.get("kind") == "fundamentals"
                   and json.loads((self.runtime / ref["runtime_relative_path"]).read_text()).get("kind") == "lineups")
        (self.runtime / ref["runtime_relative_path"]).write_text("{}")
        with self.assertRaisesRegex(SnapshotError, "source_file_hash_mismatch"):
            verify_baseline_capture(self.runtime, receipt)
        with self.assertRaisesRegex(SnapshotError, "source_file_hash_mismatch"):
            verify_capture(self.runtime, receipt)

    def test_v2_lineup_horizon_survives_unrelated_injury_or_candidate_source_breakage(self):
        payload, receipt = self.real_v2_capture(horizon="LINEUP", with_injuries=True)
        for ref in payload["source_refs"]:
            if ref.get("kind") == "candidate_model":
                (self.runtime / ref["runtime_relative_path"]).unlink()
            elif ref.get("kind") == "fundamentals":
                path = self.runtime / ref["runtime_relative_path"]
                if json.loads(path.read_text()).get("kind") == "injuries":
                    path.write_text("{}")
        self.assertTrue(verify_baseline_capture(self.runtime, receipt))
        with self.assertRaisesRegex(SnapshotError, "source_file_hash_mismatch"):
            verify_capture(self.runtime, receipt)

    def test_constructed_object_never_gains_live_or_confirmation_qualification(self):
        payload = self.pure()
        self.assertFalse(payload["live_capture_verified"])
        self.assertFalse(payload["confirmation_eligible"])
        self.assertFalse(payload["f5_eligibility_inherited"])
        self.assertIsNone(payload["storage_completed_at"])
        self.assertTrue(payload["quote_unknown"])
        self.assertTrue(payload["lineups_unknown"])
        self.assertTrue(verify_snapshot(payload))
        self.assertEqual(payload["baseline"]["prediction"], self.prediction)

    def test_future_fetched_bytes_cannot_hide_behind_earlier_reference_time(self):
        write(self.source, {"fetched_at": iso(NOW + timedelta(seconds=1))})
        self.refs[0]["sha256"] = hashlib.sha256(self.source.read_bytes()).hexdigest()
        with self.assertRaisesRegex(SnapshotError, "source_time_after_cutoff"):
            self.pure()

    def test_late_persistence_or_availability_is_rejected(self):
        write(self.source, {"fetched_at": iso(NOW), "persisted_at": iso(NOW + timedelta(seconds=1))})
        self.refs[0]["sha256"] = hashlib.sha256(self.source.read_bytes()).hexdigest()
        with self.assertRaisesRegex(SnapshotError, "source_time_after_cutoff"):
            self.pure()
        write(self.source, {"fetched_at": iso(NOW)})
        self.refs[0]["sha256"] = hashlib.sha256(self.source.read_bytes()).hexdigest()
        self.refs[0]["available_at"] = iso(NOW + timedelta(seconds=1))
        with self.assertRaisesRegex(SnapshotError, "source_available_after_cutoff"):
            self.pure()

    def test_parent_byte_tampering_is_detected(self):
        payload = self.pure()
        write(self.source, {"fetched_at": iso(NOW), "modified": True})
        with self.assertRaisesRegex(SnapshotError, "source_file_hash_mismatch"):
            verify_snapshot(payload)

    def test_future_baseline_or_clock_cannot_claim_an_earlier_cutoff(self):
        self.packet["created_at"] = iso(NOW + timedelta(seconds=1))
        with self.assertRaisesRegex(SnapshotError, "source_time_after_cutoff"):
            self.pure()
        self.packet["created_at"] = iso(NOW)
        with self.assertRaisesRegex(SnapshotError, "capture_clock_before_cutoff"):
            self.pure(clock=lambda: NOW - timedelta(seconds=1))

    def test_historical_backfill_and_unknown_lineups_are_blocked(self):
        with self.assertRaisesRegex(SnapshotError, "horizon_late"):
            self.pure(cutoff_at=NOW + timedelta(hours=10))
        with self.assertRaisesRegex(SnapshotError, "horizon_not_due"):
            self.pure(horizon="LINEUP")
        near = NOW + timedelta(hours=9)
        with self.assertRaisesRegex(SnapshotError, "confirmed_lineups_not_observed"):
            self.pure(horizon="LINEUP", cutoff_at=near, clock=lambda: near)

    def test_lineup_requires_sha_linked_complete_actual_starting_elevens(self):
        at = NOW + timedelta(hours=9)
        snapshot = self.lineup(at)
        path = save_snapshot(self.runtime / "fundamentals/snapshots", snapshot)
        self.refs.append({"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                          "available_at": iso(at), "snapshot_id": snapshot["snapshot_id"]})
        self.features = build_features(self.game, cutoff=at, snapshots=[snapshot])
        result = self.pure(horizon="LINEUP", cutoff_at=at, clock=lambda: at)
        self.assertFalse(result["lineups_unknown"])
        self.assertFalse(result["lineup_adjusted"])
        self.features["teams"]["1"]["lineups"]["starters"][0]["player_id"] = 9999
        with self.assertRaisesRegex(SnapshotError, "confirmed_lineups_not_observed"):
            self.pure(horizon="LINEUP", cutoff_at=at, clock=lambda: at)

    def test_two_horizons_and_repeated_versions_count_one_independent_match(self):
        self.runtime_sources()
        code, first = self.capture()
        self.assertEqual(code, 0)
        self.assertEqual(first["captured_independent_matches"], 1)
        self.assertEqual(first["captured_candidate_independent_matches"], 0)
        self.assertEqual(first["queue"][0]["candidate_status"], "model_unavailable")
        anchor = next((self.runtime / "feature_snapshots").glob("*/captures/serie_a/100/EARLY/first.json"))
        original = anchor.read_bytes()
        self.capture(at=NOW + timedelta(minutes=5))
        self.assertEqual(anchor.read_bytes(), original)
        at = NOW + timedelta(hours=9)
        save_snapshot(self.runtime / "fundamentals/snapshots", self.lineup(at))
        _, later = self.capture(at=at)
        self.assertEqual(later["captured_independent_matches"], 1)
        self.assertEqual(later["captured_by_horizon"], {"EARLY": 1, "LINEUP": 1})
        self.assertEqual(later["captured_horizon_records"], 2)
        self.assertEqual(later["confirmation_independent_matches"], 0)

    def test_storage_crossing_window_is_retained_as_late_not_live(self):
        self.runtime_sources()
        current = [NOW]
        original_save = __import__("football_model.feature_snapshots", fromlist=["_save"])._save

        def delayed_save(root, category, value):
            path = original_save(root, category, value)
            if category == "payloads":
                current[0] = NOW + timedelta(hours=1, seconds=1)
            return path

        with patch("football_model.feature_snapshots._save", side_effect=delayed_save):
            _, report = self.capture(clock=lambda: current[0])
        item = report["queue"][0]
        self.assertEqual(item["status"], "late")
        self.assertEqual(report["captured_independent_matches"], 0)
        receipt = json.loads((self.runtime / item["receipt_ref"]["path"]).read_text())
        self.assertFalse(receipt["live_capture_verified"])
        self.assertTrue(verify_capture(self.runtime, receipt))

    def test_completion_clock_or_payload_bytes_are_independently_checked(self):
        self.runtime_sources()
        _, report = self.capture()
        ref = report["queue"][0]["receipt_ref"]
        receipt = json.loads((self.runtime / ref["path"]).read_text())
        receipt["storage_completed_at"] = iso(NOW - timedelta(seconds=1))
        with self.assertRaisesRegex(SnapshotError, "capture_completion_time_order_invalid"):
            verify_capture(self.runtime, receipt)
        receipt["storage_completed_at"] = iso(NOW)
        payload_path = self.runtime / receipt["payload_ref"]["path"]
        payload_path.write_bytes(payload_path.read_bytes() + b" ")
        with self.assertRaisesRegex(SnapshotError, "capture_payload_file_hash_mismatch"):
            verify_capture(self.runtime, receipt)

    def test_current_parent_updates_do_not_mutate_the_frozen_closed_input(self):
        self.runtime_sources()
        _, report = self.capture()
        receipt = json.loads((self.runtime / report["queue"][0]["receipt_ref"]["path"]).read_text())
        write(self.runtime / "models/serie_a.json", {"new_model": True})
        write(self.runtime / "integration/index.json", {"new_index": True})
        self.assertTrue(verify_capture(self.runtime, receipt))

    def test_unsupported_and_not_due_rows_remain_in_planned_denominator(self):
        self.runtime_sources()
        unsupported = {**self.game, "match_id": 101, "status": "unsupported", "reason": "unknown_team"}
        write(self.runtime / "predictions/test/serie_a.json", self.rows + [unsupported])
        _, report = self.capture()
        self.assertEqual(report["planned_match_count"], 2)
        self.assertEqual(report["planned_horizon_count"], 4)
        self.assertEqual(report["queue_status_counts"]["unsupported"], 2)
        self.assertEqual(report["queue_status_counts"]["not_due"], 1)

    def test_duplicate_json_and_unsafe_restore_paths_fail_visibly(self):
        self.source.write_text('{"fetched_at":"' + iso(NOW) + '","fetched_at":"' + iso(NOW) + '"}')
        self.refs[0]["sha256"] = hashlib.sha256(self.source.read_bytes()).hexdigest()
        with self.assertRaisesRegex(SnapshotError, "duplicate_json_key"):
            self.pure()
        write(self.source, {"fetched_at": iso(NOW)})
        self.refs[0]["sha256"] = hashlib.sha256(self.source.read_bytes()).hexdigest()
        payload = self.pure()
        payload["source_refs"][0]["runtime_relative_path"] = "../source.json"
        payload["snapshot_id"] = _digest({k: v for k, v in payload.items() if k != "snapshot_id"})
        with self.assertRaisesRegex(SnapshotError, "unsafe_source_path"):
            verify_snapshot(payload, runtime=self.runtime)

    def test_candidate_is_really_run_replayable_and_keeps_the_original_baseline(self):
        self.runtime_sources()
        self.candidate_model()
        _, report = self.capture()
        item = report["queue"][0]
        self.assertEqual(item["status"], "captured", report)
        self.assertEqual(report["captured_candidate_independent_matches"], 1)
        receipt = json.loads((self.runtime / item["receipt_ref"]["path"]).read_text())
        payload = verify_capture(self.runtime, receipt)
        self.assertEqual(payload["candidate_status"], "predicted")
        self.assertEqual(payload["baseline"]["prediction"], self.prediction)
        self.assertEqual(payload["candidate"]["prediction"]["score_matrix"], self.prediction["score_matrix"])
        self.assertEqual(payload["candidate"]["generated_at"], iso(NOW))
        self.assertFalse(payload["candidate"]["lineup_adjusted"])
        self.assertEqual(len(payload["candidate"]["used_in_probabilities"]), 4)
        model_refs = [r for r in payload["source_refs"] if r["kind"] == "candidate_model"]
        self.assertEqual(len(model_refs), 1)

    def test_failed_first_opportunity_survives_a_later_candidate_success(self):
        self.runtime_sources()
        self.capture()
        first = next((self.runtime / "feature_snapshots").glob("*/captures/serie_a/100/EARLY/first_attempt.json"))
        original = first.read_bytes()
        self.candidate_model()
        _, later = self.capture(at=NOW + timedelta(minutes=1))
        self.assertEqual(first.read_bytes(), original)
        self.assertEqual(later["captured_candidate_independent_matches"], 1)
        failed_ref = json.loads(original)
        failed = json.loads((self.runtime / failed_ref["path"]).read_text())
        self.assertEqual(failed["candidate_status"], "model_unavailable")

    def test_wrong_protocol_or_future_model_is_not_a_candidate_prediction(self):
        self.runtime_sources()
        path, card = self.candidate_model()
        card["protocol_hash"] = "b" * 64
        write(path, card)
        _, result = self.capture()
        self.assertEqual(result["queue"][0]["status"], "ineligible")
        self.assertEqual(result["queue"][0]["reason"], "candidate_protocol_mismatch")
        self.assertEqual(result["captured_candidate_independent_matches"], 0)
        card["protocol_hash"] = _digest(PROTOCOL)
        card["generated_at"] = iso(NOW + timedelta(hours=2))
        write(path, card)
        _, result = self.capture(at=NOW + timedelta(minutes=1))
        self.assertEqual(result["queue"][0]["reason"], "source_time_after_cutoff")

    def test_unmocked_saved_baseline_integration_captures_the_actual_pipeline_row(self):
        from football_model.cli import run_pipeline
        from tests.test_cli import BASE_CONFIG, FakeClient
        test_time = datetime(2025, 1, 1, tzinfo=timezone.utc)
        with patch("football_model.integration.datetime") as integration_clock:
            integration_clock.now.return_value = test_time
            code, original = run_pipeline(BASE_CONFIG, self.runtime, "train", now=test_time,
                client=FakeClient(), clock=lambda: test_time)
            self.assertEqual(code, 0, original)
            rows = json.loads((self.runtime / "predictions" / original["run_id"] / "epl.json").read_text())
            row = rows[0]
            at = test_time + timedelta(hours=38)
            integration_clock.now.return_value = at
            write(self.runtime / "fundamentals/fixtures/2000/current.json", {
                "fixture_response": {"endpoint": "/fixtures", "params": {"id": 2000}, "fetched_at": iso(at),
                    "response": [{"fixture": {"id": 2000, "date": row["kickoff"], "status": {"short": "NS"}},
                                  "teams": {"home": {"id": row["home_id"]}, "away": {"id": row["away_id"]}}}]}})
            _, result = capture_run(self.runtime, "epl", source_commit=SHA, protocol=PROTOCOL,
                                    config=BASE_CONFIG, clock=lambda: at)
        self.assertEqual(result["captured_independent_matches"], 1, result)
        item = result["queue"][0]
        receipt = json.loads((self.runtime / item["receipt_ref"]["path"]).read_text())
        payload = verify_capture(self.runtime, receipt)
        self.assertTrue(payload["baseline"]["provenance"]["read_and_recomputed"])
        self.assertFalse(payload["baseline"]["provenance"]["fit_performed"])
        self.assertEqual(payload["baseline"]["prediction"], row["prediction"])

    def test_allclub_actual_used_raw_responses_and_metadata_are_in_the_closed_input(self):
        self.runtime_sources()
        self.allclub_sources()
        _, report = self.capture()
        self.assertEqual(report["queue"][0]["status"], "captured", report)
        receipt = json.loads((self.runtime / report["queue"][0]["receipt_ref"]["path"]).read_text())
        payload = verify_capture(self.runtime, receipt)
        fixture_refs = [r for r in payload["source_refs"] if r["kind"] == "all_club_used_fixture_response"]
        metadata_refs = [r for r in payload["source_refs"] if r["kind"] == "all_club_used_competition_metadata"]
        self.assertEqual(len(fixture_refs), 2)
        self.assertEqual(len(metadata_refs), 1)
        self.assertFalse(payload["candidate"]["schedule_coverage"]["all_club_competitions_complete"])
        original = self.runtime / fixture_refs[0]["origin_runtime_path"]
        original.unlink()
        self.assertTrue(verify_capture(self.runtime, receipt))

    def test_missing_or_wrong_used_raw_response_is_a_visible_candidate_failure(self):
        self.runtime_sources()
        latest, requests = self.allclub_sources()
        path = self.runtime / requests[0]["evidence"]["path"]
        path.unlink()
        _, result = self.capture()
        self.assertEqual(result["queue"][0]["status"], "ineligible")
        self.assertEqual(result["queue"][0]["reason"], "used_schedule_source_hash_mismatch")
        self.assertEqual(result["captured_candidate_independent_matches"], 0)


if __name__ == "__main__":
    unittest.main()
