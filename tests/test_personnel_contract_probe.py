"""Offline stub-transport checks; these do not establish live API coverage."""
from datetime import timedelta
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

from test_data import NOW, SECRET, Response, envelope, fixture
from football_model.data import APIError, RequestBudgetError, _NoRedirect
from scripts.personnel_contract_probe import (
    ContractProbeClient, ContractProbeError, DAILY_RESERVE, MAX_TOTAL_REQUESTS,
    ROOT, _date_field_states, run_probe,
)
import scripts.personnel_contract_probe as probe


class PersonnelContractProbeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        env = patch.dict(os.environ, {"FOOTBALL_API_KEY": SECRET})
        env.start()
        self.addCleanup(env.stop)
        self.calls = []

    def transport(self, request, **kwargs):
        parsed = urlsplit(request.full_url)
        params = {key: int(value[0]) for key, value in parse_qs(parsed.query).items()}
        self.calls.append((parsed.path, params))
        self.assertEqual(parsed.netloc, "v3.football.api-sports.io")
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(request.get_header("X-apisports-key"), SECRET)
        if parsed.path == "/status":
            return Response(envelope({"requests": {"current": 10, "limit_day": 7460},
                                      "subscription": {"active": True}}))
        if parsed.path == "/fixtures":
            return Response(envelope([fixture(params["id"])]))
        if parsed.path == "/players/squads":
            tid = params["team"]
            return Response(envelope([{"team": {"id": tid}, "players": [
                {"id": tid * 100 + offset, "minutes": None, "number": None}
                for offset in (3, 2, 1)]}]))
        if parsed.path == "/sidelined":
            return Response(envelope([{"type": "Injury", "start": "2026-10-01", "end": "Unknown"}]))
        self.fail("unexpected endpoint")

    def client(self, transport=None, **kwargs):
        return ContractProbeClient(Path(self.temp.name) / "cache", opener=transport or self.transport,
            clock=lambda: NOW, min_interval=0, sleep=lambda _: None, **kwargs)

    def run_probe(self, client=None):
        output = Path(self.temp.name) / "probe"
        code, manifest = run_probe(123, output, client=client or self.client(), clock=lambda: NOW,
                                   source_commit="a" * 40)
        return code, manifest, output

    def test_full_probe_uses_only_seven_paid_requests_eight_attempts_and_observed_ids(self):
        client = self.client()
        code, report, output = self.run_probe(client)
        self.assertEqual(code, 0)
        self.assertEqual(len(self.calls), MAX_TOTAL_REQUESTS)
        self.assertEqual(client.requests_made, 8)
        self.assertEqual(report["paid_probe_calls_attempted"], 7)
        self.assertEqual(self.calls[0], ("/status", {}))
        self.assertEqual(self.calls[1], ("/fixtures", {"id": 123}))
        self.assertEqual([params["player"] for path, params in self.calls if path == "/sidelined"],
                         [1101, 1102, 2201, 2202])
        self.assertEqual(report["final_budget"]["daily_remaining"], 7443)
        self.assertEqual(client._reserved_requests, 0)
        self.assertFalse((output / "model-state").exists())

    def test_eighth_attempt_hard_bound_applies_even_outside_reserve_context(self):
        client = self.client()
        with client.reserve_requests(DAILY_RESERVE):
            client.status()
            for _ in range(7):
                client._cached_request("/sidelined", {"player": 1101}, force=True, max_age_seconds=0)
        with self.assertRaises(RequestBudgetError):
            client.status()
        self.assertEqual(len(self.calls), 8)

    def test_daily_reserve_blocks_optional_requests_without_spending_fixture_call(self):
        def transport(request, **kwargs):
            self.calls.append(request.full_url)
            return Response(envelope({"requests": {"current": 7060, "limit_day": 7460}}))
        code, report, _ = self.run_probe(self.client(transport))
        self.assertEqual(code, 2)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(report["observations"][0]["failure_category"], "request_budget_or_deadline_blocked")

    def test_unknown_daily_usage_is_not_a_new_local_allowance(self):
        opener = Mock(return_value=Response(envelope({"requests": {}})))
        code, report, _ = self.run_probe(self.client(opener))
        self.assertEqual(code, 2)
        self.assertEqual(opener.call_count, 1)
        self.assertIsNone(report["initial_budget"]["daily_remaining"])

    def test_invalid_ids_fail_before_output_or_client_network(self):
        client = self.client()
        output = Path(self.temp.name) / "invalid"
        for value in (True, False, 0, -1, 1.0, "123", None):
            with self.assertRaises(ValueError):
                run_probe(value, output, client=client)
        self.assertFalse(output.exists())
        self.assertEqual(self.calls, [])
        for endpoint, params in [("/players/squads", {"team": True}),
                                 ("/sidelined", {"player": 0}),
                                 ("/players/squads", {"team": 11, "season": 2026}),
                                 ("/fixtures", {"id": True}), ("/fixtures", {}),
                                 ("/fixtures", {"id": 123, "season": 2026})]:
            with self.assertRaises(ValueError):
                client._request(endpoint, params)
        with self.assertRaises(APIError):
            client._request("https://example.invalid", {})
        self.assertEqual(self.calls, [])

    def test_fixture_identity_mismatch_stops_before_guessing_teams(self):
        def transport(request, **kwargs):
            if urlsplit(request.full_url).path == "/fixtures":
                self.calls.append(("/fixtures", {}))
                return Response(envelope([fixture(999)]))
            return self.transport(request, **kwargs)
        code, report, output = self.run_probe(self.client(transport))
        self.assertEqual(code, 2)
        self.assertEqual(len(self.calls), 2)
        self.assertNotIn("fixture_team_ids", report)
        self.assertEqual(report["observations"][0]["status"], "blocked")
        self.assertTrue((output / "response-01.json").is_file())

    def test_empty_squads_and_sidelined_are_unknown_not_health(self):
        def transport(request, **kwargs):
            if urlsplit(request.full_url).path == "/players/squads":
                self.calls.append(("/players/squads", {}))
                return Response(envelope([]))
            return self.transport(request, **kwargs)
        code, report, _ = self.run_probe(self.client(transport))
        self.assertEqual(code, 2)
        self.assertEqual(len(self.calls), 4)
        self.assertEqual([r["empty_response_meaning"] for r in report["observations"][1:]],
                         ["unknown_not_health", "unknown_not_health"])
        self.assertTrue(report["semantics"]["empty_response_does_not_establish_health"])

    def test_null_minutes_retained_and_unknown_sidelined_dates_not_imputed(self):
        _, report, output = self.run_probe()
        squad = json.loads((output / "response-02.json").read_text())
        self.assertIsNone(squad["response"][0]["players"][0]["minutes"])
        self.assertEqual(report["observations"][1]["observed_schema"]["$.response[].players[].minutes"], ["null"])
        self.assertEqual(report["observations"][3]["date_field_states"][0]["end"], "unknown_string")
        self.assertEqual(_date_field_states([{"start": None, "end": "bad"}, {"end": 42}]),
                         [{"response_index": 0, "start": "null", "end": "invalid_date_string"},
                          {"response_index": 1, "start": "absent", "end": "invalid_type"}])
        self.assertTrue(report["semantics"]["current_squads_are_not_historical_rosters"])
        self.assertFalse(report["semantics"]["starting_xi_probabilities_created"])

    def test_provider_errors_keep_redacted_payload_and_failure_category(self):
        def transport(request, **kwargs):
            if urlsplit(request.full_url).path == "/players/squads":
                self.calls.append(("/players/squads", {}))
                return Response(envelope([], {"plan": "Subscription denied " + SECRET}))
            return self.transport(request, **kwargs)
        code, report, output = self.run_probe(self.client(transport))
        self.assertEqual(code, 2)
        self.assertEqual(len(self.calls), 4)
        item = report["observations"][1]
        self.assertEqual(item["failure_category"], "provider_plan_season_or_quota_error")
        failure = json.loads((output / item["failure_source"]["path"]).read_text())
        self.assertEqual(failure["http_status"], 200)
        self.assertIn("[REDACTED]", failure["payload"]["errors"]["plan"])
        for path in output.rglob("*.json"):
            self.assertNotIn(SECRET, path.read_text())

    def test_403_and_redirect_refused_no_retry_or_redirected_request(self):
        for http_status in (403, 302):
            with self.subTest(http_status=http_status):
                def transport(request, **kwargs):
                    if urlsplit(request.full_url).path == "/players/squads":
                        self.calls.append(("/players/squads", {}))
                        raise HTTPError(request.full_url, http_status, SECRET,
                                        {"Location": "https://example.invalid/" + SECRET}, None)
                    return self.transport(request, **kwargs)
                client = self.client(transport)
                client.status()
                with self.assertRaises(ContractProbeError) as caught:
                    with client.reserve_requests(DAILY_RESERVE):
                        client._cached_request("/players/squads", {"team": 11}, force=True, max_age_seconds=0)
                self.assertEqual(caught.exception.safe_evidence["http_status"], http_status)
                self.assertNotIn(SECRET, str(caught.exception))
        default_client = ContractProbeClient(Path(self.temp.name) / "default")
        self.assertTrue(any(isinstance(handler, _NoRedirect) for handler in default_client._opener.__self__.handlers))
        self.assertIsNone(_NoRedirect().redirect_request(None, None, 302, "", {}, "https://example.invalid"))

    def test_paginated_response_is_recorded_as_incomplete_not_cached_as_complete(self):
        def transport(request, **kwargs):
            if urlsplit(request.full_url).path == "/players/squads":
                self.calls.append(("/players/squads", {}))
                value = envelope([])
                value["paging"]["total"] = 2
                return Response(value)
            return self.transport(request, **kwargs)
        code, report, _ = self.run_probe(self.client(transport))
        self.assertEqual(code, 2)
        self.assertEqual(report["observations"][1]["failure_category"], "provider_pagination_incomplete")

    def test_source_hashes_response_hashes_and_real_observation_time_are_verifiable(self):
        _, report, output = self.run_probe()
        self.assertEqual(report["source_commit"], "a" * 40)
        for source in report["implementation_files"]:
            self.assertEqual(source["sha256"], hashlib.sha256((ROOT / source["path"]).read_bytes()).hexdigest())
        for item in report["observations"]:
            reference = item["source"]
            self.assertEqual(reference["sha256"], hashlib.sha256((output / reference["path"]).read_bytes()).hexdigest())
            self.assertEqual(item["fetched_at"], NOW.isoformat().replace("+00:00", "Z"))
        with self.assertRaises(FileExistsError):
            run_probe(123, output, client=self.client())

    def test_schema_field_missing_and_explicit_wrong_player_id_are_not_accepted(self):
        def transport(request, **kwargs):
            if urlsplit(request.full_url).path == "/sidelined":
                self.calls.append(("/sidelined", {}))
                return Response(envelope([{"player": {"id": 999}, "type": "Injury", "start": None}]))
            return self.transport(request, **kwargs)
        code, report, _ = self.run_probe(self.client(transport))
        self.assertEqual(code, 2)
        self.assertTrue(all(item["status"] == "blocked" for item in report["observations"][3:]))

    def test_future_observation_rejected_without_squad_probes(self):
        client = self.client()
        client._clock = lambda: NOW + timedelta(hours=1)
        code, report, _ = self.run_probe(client)
        self.assertEqual(code, 2)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(report["observations"][0]["failure_category"], "response_contract_invalid")

    def test_workflow_is_manual_source_main_only_read_only_and_separate_from_state(self):
        workflow = (ROOT / ".github/workflows/personnel-contract-probe.yml").read_text()
        self.assertIn("workflow_dispatch:", workflow)
        self.assertIn("github.ref == 'refs/heads/main'", workflow)
        self.assertIn("github.repository == 'wuyan0231-hub/football-model'", workflow)
        self.assertIn("contents: read", workflow)
        self.assertIn("persist-credentials: false", workflow)
        self.assertIn("--fixture-id \"$FIXTURE_ID\"", workflow)
        self.assertNotIn("schedule:", workflow)
        self.assertNotIn("push:", workflow)
        self.assertNotIn("football-model-state", workflow)
        self.assertNotIn("cloud_state.py", workflow)
        self.assertNotIn("contents: write", workflow)

    def test_standalone_script_help_imports_without_runtime_install_or_network(self):
        result = subprocess.run([sys.executable, "-I", str(ROOT / "scripts/personnel_contract_probe.py"), "--help"],
                                cwd=self.temp.name, capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0)
        self.assertIn("--fixture-id", result.stdout)
        self.assertEqual(result.stderr, "")

    def test_cli_rejects_non_numeric_ids_before_output_creation(self):
        output = Path(self.temp.name) / "invalid-cli"
        for value in ("-1", "1.0", "true", "0", "1;echo bad", "01"):
            result = subprocess.run([sys.executable, str(ROOT / "scripts/personnel_contract_probe.py"),
                                     "--fixture-id", value, "--output", str(output)],
                                     cwd=self.temp.name, capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 2)
        self.assertFalse(output.exists())
        self.assertEqual(self.calls, [])

    def test_standalone_missing_credentials_creates_failure_manifest_without_network(self):
        output = Path(self.temp.name) / "missing-credential-cli"
        with patch.dict(os.environ, {"FOOTBALL_API_KEY": ""}):
            result = subprocess.run([sys.executable, str(ROOT / "scripts/personnel_contract_probe.py"),
                                     "--fixture-id", "123", "--source-commit", "a" * 40,
                                     "--output", str(output)], cwd=self.temp.name,
                                     capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stderr, "")
        report = json.loads((output / "manifest.json").read_text())
        self.assertEqual(report["issues"], [{"category": "credential_configuration_missing",
                                           "stage": "initialization_or_status"}])
        self.assertEqual(report["observations"], [])
        self.assertEqual(report["source_commit"], "a" * 40)

    def test_cli_complete_stub_run_reports_and_saves_the_source_identity(self):
        output = Path(self.temp.name) / "cli-complete"
        client = self.client()
        def factory(*args, **kwargs):
            return client
        factory.implementation_version = ContractProbeClient.implementation_version
        arguments = ["personnel_contract_probe.py", "--fixture-id", "123", "--output", str(output),
                     "--source-commit", "a" * 40]
        with patch.object(probe, "ContractProbeClient", factory), patch.object(sys, "argv", arguments), \
                patch.object(probe, "datetime") as current, patch("builtins.print") as printed:
            current.now.return_value = NOW
            self.assertEqual(probe.main(), 0)
        summary = json.loads(printed.call_args.args[0])
        self.assertEqual(summary["manifest"], str(output / "manifest.json"))
        self.assertEqual(summary["status"], "contract_observations_complete")
        self.assertEqual(len(self.calls), 8)

    def test_fixture_provider_error_retains_redacted_source_and_stops_at_two_attempts(self):
        def transport(request, **kwargs):
            if urlsplit(request.full_url).path == "/fixtures":
                self.calls.append(("/fixtures", {}))
                return Response(envelope([], {"plan": SECRET}))
            return self.transport(request, **kwargs)
        code, report, output = self.run_probe(self.client(transport))
        self.assertEqual(code, 2)
        self.assertEqual(len(self.calls), 2)
        item = report["observations"][0]
        source = json.loads((output / item["failure_source"]["path"]).read_text())
        self.assertEqual(source["payload"]["errors"]["plan"], "[REDACTED]")
        self.assertEqual(item["failure_category"], "provider_plan_season_or_quota_error")

    def test_fixture_missing_response_contract_is_retained_without_probing_guessed_teams(self):
        def transport(request, **kwargs):
            if urlsplit(request.full_url).path == "/fixtures":
                self.calls.append(("/fixtures", {}))
                return Response({"unexpected": {"echo": SECRET}})
            return self.transport(request, **kwargs)
        code, report, output = self.run_probe(self.client(transport))
        self.assertEqual(code, 2)
        self.assertEqual(len(self.calls), 2)
        item = report["observations"][0]
        self.assertEqual(item["failure_category"], "provider_response_contract_invalid")
        source = json.loads((output / item["failure_source"]["path"]).read_text())
        self.assertEqual(source["payload"]["unexpected"]["echo"], "[REDACTED]")

    def test_http_error_json_is_redacted_and_non_json_preserves_only_size_hash(self):
        for raw, expected_kind in [(json.dumps({"error": SECRET}).encode(), "json"),
                                   (("<html>" + SECRET + "</html>").encode(), "non_json")]:
            with self.subTest(kind=expected_kind):
                output = Path(self.temp.name) / ("http-error-" + expected_kind)
                calls = []
                def transport(request, **kwargs):
                    if urlsplit(request.full_url).path == "/fixtures":
                        calls.append("fixture")
                        raise HTTPError(request.full_url, 403, SECRET,
                                        {"Location": "https://example.invalid/" + SECRET}, io.BytesIO(raw))
                    return self.transport(request, **kwargs)
                code, report = run_probe(123, output, client=self.client(transport), clock=lambda: NOW)
                self.assertEqual(code, 2)
                self.assertEqual(calls, ["fixture"])
                item = report["observations"][0]
                self.assertEqual(item["failure_category"], "provider_http_403")
                source = json.loads((output / item["failure_source"]["path"]).read_text())
                self.assertEqual(source["body_kind"], expected_kind)
                if expected_kind == "json":
                    self.assertEqual(source["payload"], {"error": "[REDACTED]"})
                else:
                    self.assertNotIn("payload", source)
                    self.assertEqual(source["body_bytes"], len(raw))
                    self.assertEqual(source["body_sha256"], hashlib.sha256(raw).hexdigest())
                for path in output.rglob("*.json"):
                    self.assertNotIn(SECRET, path.read_text())
                    self.assertNotIn("Location", path.read_text())

    def test_overflowing_json_exponents_are_retained_only_as_safe_body_digest(self):
        client = self.client()
        raw = b'{"response":[],"errors":[],"extra":1e999}'
        self.assertEqual(client._safe_body(raw), {"body_kind": "non_json", "body_bytes": len(raw),
            "body_sha256": hashlib.sha256(raw).hexdigest(), "body_hash_scope": "complete_body"})


if __name__ == "__main__":
    unittest.main()
