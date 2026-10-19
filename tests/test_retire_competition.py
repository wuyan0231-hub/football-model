"""Retirement preserves domestic numerical originals and archived evidence."""
import gzip
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
from unittest import TestCase
from unittest.mock import patch

from football_model import history_archive as history
from football_model import state_storage

_SPEC = importlib.util.spec_from_file_location("retire_competition",
    Path(__file__).resolve().parents[1] / "scripts/retire_competition.py")
retirement = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(retirement)


class RetirementTests(TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.runtime = self.root / "runtime"
        self.runtime.mkdir()
        self.receipt = self.root / "evidence/retirement.json"

    def save(self, name, value):
        raw = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
        path = self.runtime / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        return raw

    def run_retirement(self, apply=True):
        return retirement.retire(self.runtime, competition="unl", receipt=self.receipt,
            apply=apply, base_commit="a" * 40, competitions={"epl", "liga_mx"})

    def test_default_dry_run_changes_no_runtime_bytes(self):
        self.save("models/unl.json", {"competition": "unl"})
        self.save("models/epl.json", {"competition": "epl", "lambda": 1.18})
        before = {p.relative_to(self.runtime): p.read_bytes() for p in self.runtime.rglob("*") if p.is_file()}
        report = self.run_retirement(apply=False)
        after = {p.relative_to(self.runtime): p.read_bytes() for p in self.runtime.rglob("*") if p.is_file()}
        self.assertEqual(before, after)
        self.assertEqual(report["status"], "planned")
        self.assertEqual([r["logical_path"] for r in report["deleted"]], ["models/unl.json"])

    def test_api_identity_match_ids_and_projected_index_not_generic_id_five(self):
        domestic_row = {"key": "epl", "latest_attempt": {"path": "models/epl.json"}}
        self.save("integration/index.json", {"schema_version": 1,
            "competitions": [domestic_row, {"key": "unl"}]})
        domestic = self.save("models/epl.json", {"competition": "epl", "team_id": 5, "rates": [1.1, 0.8]})
        self.save("data/unl.json", {"matches": [{"match_id": 1528939}]})
        self.save("cache/api_cache/a.json", {"endpoint": "/fixtures",
            "response": [{"league": {"id": 5}, "fixture": {"id": 1528939}}]})
        self.save("fundamentals/snapshots/lineups/1528939/a.json", {"match_id": 1528939})
        self.save("cache/api_cache/lineups.json", {"endpoint": "/fixtures/lineups",
            "params": {"fixture": 1528939}, "response": [{"team": {"id": 5}}]})
        venue = self.save("cache/api_cache/venue.json", {"endpoint": "/venues",
            "params": {"id": 5}, "response": [{"id": 5, "name": "Domestic venue"}]})
        report = self.run_retirement()
        self.assertFalse((self.runtime / "data/unl.json").exists())
        self.assertFalse((self.runtime / "cache/api_cache/a.json").exists())
        self.assertFalse((self.runtime / "fundamentals/snapshots/lineups/1528939/a.json").exists())
        self.assertFalse((self.runtime / "cache/api_cache/lineups.json").exists())
        self.assertEqual((self.runtime / "models/epl.json").read_bytes(), domestic)
        self.assertEqual((self.runtime / "cache/api_cache/venue.json").read_bytes(), venue)
        self.assertEqual(json.loads((self.runtime / "integration/index.json").read_bytes())["competitions"], [domestic_row])
        self.assertTrue(report["other_competitions_original_bytes_preserved"])

    def test_private_anonymous_dependency_removed_but_shared_immutable_not_rewritten(self):
        shared = {"competitions": [{"key": "unl"}, {"key": "epl"}], "records": ["original"]}
        shared_raw = json.dumps(shared, separators=(",", ":")).encode()
        shared_name = "prospective/sources/" + hashlib.sha256(shared_raw).hexdigest() + ".json"
        self.save(shared_name, shared)
        only_raw = b'{"values":[1,2,3]}'
        only_name = "advanced_models/objects/" + hashlib.sha256(only_raw).hexdigest() + ".json"
        self.save(only_name, {"values": [1, 2, 3]})
        self.save("advanced_models/unl/reports/latest.json", {"source": shared_name,
            "private": {"path": only_name}})
        domestic = self.save("prospective/forecasts/epl.json", {"competition": "epl", "source_ref": {"path": shared_name}})
        report = self.run_retirement()
        self.assertFalse((self.runtime / only_name).exists())
        self.assertFalse((self.runtime / "advanced_models/unl/reports/latest.json").exists())
        self.assertEqual((self.runtime / shared_name).read_bytes(), shared_raw)
        self.assertEqual((self.runtime / "prospective/forecasts/epl.json").read_bytes(), domestic)
        self.assertIn(shared_name, [r["logical_path"] for r in
            report["shared_historical_objects_retained_due_to_domestic_dependencies"]])
        self.assertFalse(report["git_history_rewritten"])

    def test_mixed_report_old_retired_row_does_not_keep_live_unl_model(self):
        shared = self.save("reports/history/old.json", {"competitions": [
            {"key": "epl", "model_file": "runtime/models/epl.json"},
            {"key": "unl", "model_file": "runtime/models/unl.json"}]})
        domestic = self.save("models/epl.json", {"competition": "epl", "rate": 1.3})
        self.save("models/unl.json", {"competition": "unl"})
        self.save("prospective/forecasts/epl.json", {"competition": "epl",
            "report_ref": {"path": "reports/history/old.json"}})
        report = self.run_retirement()
        self.assertFalse((self.runtime / "models/unl.json").exists())
        self.assertEqual((self.runtime / "models/epl.json").read_bytes(), domestic)
        self.assertEqual((self.runtime / "reports/history/old.json").read_bytes(), shared)
        self.assertTrue(report["current_unl_entrypoints_removed"])

    def test_mixed_global_latest_retires_instead_of_becoming_a_live_historical_menu(self):
        self.save("advanced_features/collection/latest.json", {"leagues": ["epl", "unl"],
            "completed": 2, "partial": False})
        domestic = self.save("advanced_models/epl/history/old.json", {"competition": "epl",
            "collection_ref": {"path": "advanced_features/collection/latest.json"}})
        self.run_retirement()
        self.assertFalse((self.runtime / "advanced_features/collection/latest.json").exists())
        self.assertEqual((self.runtime / "advanced_models/epl/history/old.json").read_bytes(), domestic)

    def test_fully_restored_compressed_payloads_are_scanned_and_repacked_normally(self):
        domestic = self.save("models/epl.json", {"competition": "epl", "team_id": 5})
        self.save("cache/api_cache/a.json", {"endpoint": "/fixtures", "params": {"league": 5}})
        transport = self.root / "transport"
        transport.mkdir()
        state_storage.pack_runtime(self.runtime, transport)
        self.assertTrue(list((transport / "state_archives").glob("*.json.gz")))
        restored = self.root / "restored/runtime"
        state_storage.restore_runtime(transport, restored)
        report = retirement.retire(restored, competition="unl", receipt=self.receipt,
            apply=True, base_commit="a" * 40, competitions={"epl"})
        self.assertFalse((restored / "cache/api_cache/a.json").exists())
        self.assertEqual((restored / "models/epl.json").read_bytes(), domestic)
        (self.root / "new-transport").mkdir()
        state_storage.pack_runtime(restored, self.root / "new-transport")
        self.assertTrue(report["other_competitions_original_bytes_preserved"])

    def test_scoped_match_ids_do_not_mark_other_rows_from_a_mixed_report(self):
        target, mids, scoped, _ = retirement._facts({"competitions": [
            {"key": "unl", "matches": [{"match_id": 100}]},
            {"key": "epl", "matches": [{"match_id": 200}]}]})
        self.assertTrue(target)
        self.assertEqual(mids, {100, 200})
        self.assertEqual(scoped, {100})

    def test_replay_gzip_is_expanded_and_raw_payload_identity_inspected(self):
        path = self.runtime / "replay/bundles" / ("a" * 64 + ".json.gz")
        path.parent.mkdir(parents=True)
        path.write_bytes(gzip.compress(b'{"competition":"unl"}', mtime=0))
        self.run_retirement()
        self.assertFalse(path.exists())

    def test_wrong_scope_symlink_and_unrestored_apply_fail_before_mutation(self):
        original = self.save("models/unl.json", {"competition": "unl"})
        with self.assertRaisesRegex(retirement.RetirementError, "scope_not_authorized"):
            retirement.retire(self.runtime, competition="epl", receipt=self.receipt, apply=True)
        with self.assertRaisesRegex(retirement.RetirementError, "fixed_loaded_state_required"):
            retirement.retire(self.runtime, competition="unl", receipt=self.receipt, apply=True)
        link = self.runtime / "unsafe.json"
        link.symlink_to(self.runtime / "models/unl.json")
        with self.assertRaises(state_storage.StorageError):
            self.run_retirement()
        self.assertEqual((self.runtime / "models/unl.json").read_bytes(), original)

    def test_failed_cold_read_is_not_misreported_absent_or_applied(self):
        self.save("models/unl.json", {"competition": "unl"})
        with patch.object(history, "read_bytes_with_digest", side_effect=state_storage.StorageError("unavailable")):
            with self.assertRaises(state_storage.StorageError):
                self.run_retirement()
        self.assertTrue((self.runtime / "models/unl.json").is_file())
        self.assertFalse(self.receipt.exists())

    def test_current_derived_index_is_rebuilt_with_domestic_summary_and_markdown(self):
        config = {"competitions": [{"key": "epl", "label": "英超", "mode": "domestic_baseline"}]}
        model = self.save("models/epl.json", {"model_id": "epl-old", "model": {}, "validation": {}})
        self.save("models/unl.json", {"competition": "unl"})
        self.save("integration/index.json", {"schema_version": 1, "competitions": [
            {"key": "epl", "status": "no_run"}, {"key": "unl", "status": "no_run"}],
            "forecast_availability_summary": {"unknown_coverage_competitions": 2}})
        text = self.runtime / "integration/index.md"
        text.write_text("Old UNL menu\n")
        report = retirement.retire(self.runtime, competition="unl", receipt=self.receipt,
            apply=True, base_commit="a" * 40, competitions={"epl"}, config=config)
        index = json.loads((self.runtime / "integration/index.json").read_bytes())
        self.assertEqual([r["key"] for r in index["competitions"]], ["epl"])
        self.assertEqual(index["forecast_availability_summary"]["unknown_coverage_competitions"], 1)
        self.assertNotIn("UNL", text.read_text())
        self.assertIn("英超", text.read_text())
        self.assertEqual((self.runtime / "models/epl.json").read_bytes(), model)
        indexes = {r["path"]: r for r in report["mutable_indexes_projected"]}
        self.assertEqual(indexes["integration/index.md"]["after_sha256"],
            hashlib.sha256(text.read_bytes()).hexdigest())

    def test_rejected_receipt_inside_runtime_writes_nothing_even_on_cli_failure(self):
        receipt = self.runtime / "retirement.json"
        status = retirement.main(["--state-dir", str(self.runtime), "--competition", "unl",
            "--apply", "--receipt", str(receipt)])
        self.assertEqual(status, 2)
        self.assertFalse(receipt.exists())


class ColdRetirementRoundTripTests(TestCase):
    from tests.test_cloud_state import CloudStateTests as _Helpers
    setUp = _Helpers.setUp
    write_model = _Helpers.write_model
    clone = _Helpers.clone

    def test_cold_routes_stay_exact_without_fetching_ancestors_and_save_is_ordinary(self):
        from tests.test_cloud_state import cloud_state as cloud, git
        cloud.load_state(self.repo)
        self.write_model(self.repo, 1)
        runtime = self.repo / "runtime"
        domestic = (runtime / "models/epl.json").read_bytes()
        (runtime / "models/unl.json").write_bytes(b'{"competition":"unl"}')
        names = []
        for league in ("unl", "epl"):
            raw = json.dumps({"competition": league, "numbers": list(range(20))}).encode()
            name = "advanced_models/objects/" + hashlib.sha256(raw).hexdigest() + ".json"
            target = runtime / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)
            names.append(name)
        first = cloud.save_state(self.repo)
        cloud.prepare_state(self.repo)
        second = cloud.save_state(self.repo)
        self.assertEqual(git(self.remote, "rev-parse", "model-state^"), first)
        restored = self.clone("retirement-restored")
        self.assertEqual(cloud.load_state(restored), second)
        current = restored / "runtime"
        before_catalog = dict(history._catalog(current))
        self.assertTrue(before_catalog)
        with patch.object(history, "_entry_body", side_effect=AssertionError("no ancestor payload fetch")):
            report = retirement.retire(current, competition="unl", receipt=restored / "retirement.json",
                apply=True, base_commit=second, competitions={"epl"})
        self.assertFalse((current / "models/unl.json").exists())
        self.assertEqual((current / "models/epl.json").read_bytes(), domestic)
        self.assertEqual(history._catalog(current), before_catalog)
        self.assertFalse(report["cold_payloads_reverified"])
        self.assertFalse(report["all_related_contents_removed"])
        self.assertEqual(report["unexpanded_historical_originals_retained"], 2)
        third = cloud.save_state(restored)
        self.assertEqual(git(self.remote, "rev-parse", "model-state^"), second)
        final = self.clone("retirement-readback")
        self.assertEqual(cloud.load_state(final), third)
        self.assertFalse((final / "runtime/models/unl.json").exists())
        self.assertEqual((final / "runtime/models/epl.json").read_bytes(), domestic)
        for name in names:
            self.assertEqual(history.read_bytes(final / "runtime" / name),
                history.read_state_bytes(self.repo, second, name))
