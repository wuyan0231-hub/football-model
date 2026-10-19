"""Retired competition cannot re-enter through stale configs or saved states."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from football_model import cli, integration
from football_model.competition_scope import require_competition, select_competitions
from football_model.data import APIClient
from football_model.advanced_confirmation import run
from football_model.advanced_research import validate_protocol
from test_cli import BASE_CONFIG, FakeClient, NOW


ROOT = Path(__file__).resolve().parents[1]
RETIRED = {"key": "unl", "name": "UEFA Nations League", "label": "欧洲国家联赛",
           "country": "World", "mode": "national_baseline"}


class RetirementScopeTests(unittest.TestCase):
    def test_current_config_is_eighteen_domestic_models_and_one_audit(self):
        config = json.loads((ROOT / "config.json").read_text())
        self.assertEqual(len(select_competitions(config, models_only=True)), 18)
        self.assertEqual([c["key"] for c in config["competitions"] if c["mode"] == "audit_only"], ["ucl"])

    def test_explicit_retired_aliases_fail_before_api_or_saved_index_read(self):
        config = copy.deepcopy(BASE_CONFIG)
        config["competitions"].append(RETIRED)
        for name in ("unl", "UNL", "UEFA Nations League", "UEFA_Nations-League", "欧国联", "欧洲国家联赛"):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temp, patch.object(cli, "APIClient") as transport:
                with self.assertRaisesRegex(ValueError, "competition_removed_by_user"):
                    cli.run_pipeline(config, temp, "train", name, now=NOW, clock=lambda: NOW)
                transport.assert_not_called()
                with self.assertRaisesRegex(ValueError, "competition_removed_by_user"):
                    integration.match_packet(temp, config, name, 1)
                self.assertEqual(list(Path(temp).iterdir()), [])

    def test_all_with_stale_config_never_calls_retired_competition(self):
        config = copy.deepcopy(BASE_CONFIG)
        config["competitions"].append(RETIRED)
        with tempfile.TemporaryDirectory() as temp, patch.object(cli, "process_competition",
                return_value={"key": "epl", "label": "英超", "status": "仅审计"}) as process:
            code, report = cli.run_pipeline(config, temp, "audit", "all", now=NOW, clock=lambda: NOW, client=FakeClient())
        self.assertEqual(code, 0)
        self.assertEqual(report["requested_competitions"], ["epl"])
        self.assertEqual(process.call_args.args[1]["key"], "epl")
        process.assert_called_once()

    def test_renamed_key_provider_id_and_national_mode_cannot_bypass_scope(self):
        for item in ({"key": "renamed", "provider_id": 5}, {"key": "renamed", "provider_id": "5"},
                     {"key": "renamed", "mode": "national_baseline"}, {"key": "renamed", "name": "UEFA Nations League"}):
            with self.subTest(item=item), self.assertRaisesRegex(ValueError, "competition_removed_by_user"):
                require_competition(item)
        client = object.__new__(APIClient)
        client._cached_request = Mock()
        with self.assertRaisesRegex(ValueError, "competition_removed_by_user"):
            client.fetch_fixtures(5, 2026)
        client._cached_request.assert_not_called()
        with self.assertRaisesRegex(ValueError, "competition_removed_by_user"):
            client.resolve_league(RETIRED)
        client._cached_request.assert_not_called()

    def test_provider_identity_mismatch_is_rejected_before_fixture_fetch(self):
        client = FakeClient()
        client.resolve_league = Mock(return_value={"league": {"id": 5}})
        client.fetch_fixtures = Mock(side_effect=AssertionError("must not download retired league"))
        with tempfile.TemporaryDirectory() as temp:
            code, report = cli.run_pipeline(BASE_CONFIG, temp, "train", "epl", now=NOW, clock=lambda: NOW, client=client)
        self.assertEqual(code, 1)
        self.assertIn("competition_removed_by_user", report["competitions"][0]["detail"])
        client.fetch_fixtures.assert_not_called()

    def test_domestic_fingerprint_retains_original_math_identity(self):
        competition = BASE_CONFIG["competitions"][0]
        settings, matches = {"ridge": 3, "half_life_days": 180}, [{"match_id": 1}]
        original_payload = {"version": cli.__version__, "source": hashlib.sha256((ROOT / "football_model/model.py").read_bytes()).hexdigest(),
            "competition": competition, "settings": settings, "matches": matches}
        expected = hashlib.sha256(json.dumps(original_payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        self.assertEqual(cli.input_fingerprint(matches, settings, competition), expected)

    def test_new_domestic_protocol_has_new_identity_and_full_original_penalty(self):
        protocol = validate_protocol(json.loads((ROOT / "advanced-models-domestic-v1.json").read_text()))
        original = json.loads((ROOT / "advanced-models-v1.json").read_text())
        self.assertNotEqual(protocol["protocol_id"], original["protocol_id"])
        self.assertEqual(len(protocol["candidates"]), 12)
        self.assertFalse(protocol["confirmation_active"])
        self.assertFalse(protocol["production_promotion"])
        design = json.loads((ROOT / "advanced-confirmation-v1.json").read_text())
        with tempfile.TemporaryDirectory() as temp:
            result = run(temp, "epl", protocol, design, clock=lambda: NOW)
        self.assertEqual(result["family_size"], 380)
        self.assertEqual(len(result["trials"]), 18)
        self.assertTrue(all(row["confirmation_active"] is False for row in result["trials"]))
        # Removing optional descriptive metadata cannot shrink the penalty.
        protocol.pop("original_family_size_minimum")
        with tempfile.TemporaryDirectory() as temp:
            result = run(temp, "epl", protocol, design, clock=lambda: NOW)
        self.assertEqual(result["family_size"], 380)


if __name__ == "__main__":
    unittest.main()
