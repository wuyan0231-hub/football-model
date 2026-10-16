"""Manual, read-only API contract probe; never trains or writes model-state.

ContractProbeClient is an explicitly separate, versioned extension of APIClient
for exactly two additional official read-only endpoints. Its existing fixture
endpoint also preserves safe error evidence. Production data.py is unchanged.
The inherited client owns authentication setup, quota, rate limiting and cache
redaction. Probe results are not eligible production or historical model input.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
from datetime import date, datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from football_model.data import APIClient, APIError, BASE_URL, DataError, RequestBudgetError, _integer, _iso, _utc


MAX_PAID_REQUESTS = 7  # fixture + two current squads + at most four players
MAX_TOTAL_REQUESTS = MAX_PAID_REQUESTS + 1  # account status is a network attempt
DAILY_LIMIT = 7460
DAILY_RESERVE = 400
OFFICIAL_REFERENCES = [
    "https://www.api-football.com/news/post/how-to-get-started-with-api-football-the-complete-beginners-guide",
    "https://www.api-football.com/news/post/how-ratelimit-works",
    "https://www.api-football.com/documentation-v3",
]
SEMANTICS = {
    "scope": "current_contract_observation_only",
    "current_squads_are_not_historical_rosters": True,
    "sidelined_dates_are_not_publication_times": True,
    "sidelined_is_not_a_health_census_or_medical_severity": True,
    "empty_response_does_not_establish_health": True,
    "null_minutes_remain_null": True,
    "starting_xi_probabilities_created": False,
    "prediction_or_training_performed": False,
    "production_state_modified": False,
    "subject_selection_does_not_verify_roster": True,
}


class ContractProbeError(APIError):
    """Fixed safe category plus already-redacted provider evidence, never headers."""

    def __init__(self, category, evidence=None):
        super().__init__(category)
        self.category = category
        self.safe_evidence = evidence


class ContractProbeClient(APIClient):
    """Probe-only extension v2; explicitly adds squads and sidelined, not models.

    The normal APIClient constructor consumes the configured GitHub secret.
    Its no-redirect opener, real budget/header accounting and cache redaction
    are reused. Every network attempt has both the eight-attempt hard guard and
    a 400-request daily reserve. No endpoint has additional probe retries.
    """

    implementation_version = "personnel-contract-probe-client-v2"

    def __init__(self, data_dir, **kwargs):
        if any(key in kwargs for key in ("max_requests", "daily_limit", "max_retries")):
            raise ValueError("probe budgets and retry policy are fixed")
        super().__init__(data_dir, max_requests=MAX_TOTAL_REQUESTS + DAILY_RESERVE,
                         daily_limit=DAILY_LIMIT, max_retries=0, **kwargs)

    def _before_request(self, *, paid=True):
        if self.requests_made >= MAX_TOTAL_REQUESTS:
            raise RequestBudgetError("probe total attempt bound reached")
        with self.reserve_requests(DAILY_RESERVE):
            return super()._before_request(paid=paid)

    def _request(self, endpoint, params):
        names = {"/fixtures": "id", "/players/squads": "team", "/sidelined": "player"}
        if endpoint not in names:
            return super()._request(endpoint, params)
        name = names[endpoint]
        if not isinstance(params, dict) or set(params) != {name}:
            raise DataError("probe endpoint requires its exact official numeric parameter")
        _integer(params[name], name, 1)
        self._before_request(paid=True)
        request = Request(BASE_URL + endpoint + "?" + urlencode(params),
            headers={"x-apisports-key": self._key, "Accept": "application/json"}, method="GET")
        try:
            with self._opener(request, timeout=self.timeout) as response:
                self._rate_headers(getattr(response, "headers", None))
                body = response.read()
                status = getattr(response, "status", 200)
            safe_body = self._safe_body(body)
            evidence = {"http_status": status, **safe_body,
                        "fetched_at": _iso(self._clock()), "endpoint": endpoint, "params": params}
            if safe_body["body_kind"] != "json":
                raise ContractProbeError("provider_invalid_json", evidence)
            clean = safe_body["payload"]
            if (not isinstance(clean, dict) or not isinstance(clean.get("response"), list)
                    or not isinstance(clean.get("errors"), (list, dict))):
                raise ContractProbeError("provider_response_contract_invalid", evidence)
            if clean["errors"]:
                raise ContractProbeError("provider_plan_season_or_quota_error", evidence)
            paging = clean.get("paging", {})
            if (not isinstance(paging, dict) or type(paging.get("total", 1)) is not int
                    or paging.get("total", 1) != 1):
                raise ContractProbeError("provider_pagination_incomplete", evidence)
            return {**clean, "fetched_at": evidence["fetched_at"], "from_cache": False,
                    "probe_http_status": status}
        except HTTPError as exc:
            self._rate_headers(exc.headers)
            # A redirect is refused by the inherited no-redirect opener. Never
            # follow Location or persist arbitrary non-JSON body/header text.
            category = ("provider_redirect_refused" if 300 <= exc.code < 400
                        else "provider_http_" + str(exc.code))
            evidence = {"http_status": exc.code, "fetched_at": _iso(self._clock()),
                        "endpoint": endpoint, "params": params}
            try:
                # Error documents are bounded. A large response is identified
                # as a prefix digest, never mislabeled as complete raw bytes.
                body = exc.read(1024 * 1024 + 1)
                if isinstance(body, bytes):
                    if len(body) > 1024 * 1024:
                        evidence.update(body_kind="oversized_error_body", observed_bytes=len(body),
                            body_sha256=hashlib.sha256(body).hexdigest(), body_hash_scope="observed_prefix")
                    else:
                        evidence.update(self._safe_body(body))
                else:
                    evidence["body_kind"] = "unavailable"
            except (OSError, ValueError, TypeError):
                evidence["body_kind"] = "read_failed"
            raise ContractProbeError(category, evidence) from None
        except (URLError, TimeoutError, OSError):
            raise ContractProbeError("provider_transport_error") from None

    def _safe_body(self, body):
        def reject_nonfinite(_):
            raise ValueError("nonfinite JSON")
        try:
            raw = json.loads(body, parse_constant=reject_nonfinite)
            # A numeric exponent such as 1e999 can become inf even without a
            # literal Infinity token. Recheck the redacted serialization too.
            clean = json.loads(self._redact_full(raw), parse_constant=reject_nonfinite)
            return {"body_kind": "json", "payload": clean}
        except (ValueError, UnicodeDecodeError):
            return {"body_kind": "non_json", "body_bytes": len(body),
                    "body_sha256": hashlib.sha256(body).hexdigest(), "body_hash_scope": "complete_body"}


def _json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2,
                       allow_nan=False) + "\n").encode("utf-8")


def _write(path, value):
    payload = _json_bytes(value)
    with path.open("xb") as out:
        out.write(payload)
    return {"path": path.name, "bytes": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest()}


def _schema(value):
    """Observed types only, with array positions merged; no value imputation."""
    fields = {}

    def walk(node, path):
        kind = ("null" if node is None else "boolean" if type(node) is bool else
                "integer" if type(node) is int else "number" if type(node) is float else
                "string" if isinstance(node, str) else "object" if isinstance(node, dict)
                else "array" if isinstance(node, list) else "unsupported")
        fields.setdefault(path, set()).add(kind)
        if isinstance(node, dict):
            for key, child in node.items():
                walk(child, path + "." + key)
        elif isinstance(node, list):
            for child in node:
                walk(child, path + "[]")

    walk(value, "$")
    return {path: sorted(kinds) for path, kinds in sorted(fields.items())}


def _failure(exc):
    # Do not persist arbitrary exception strings, provider echoes or account data.
    # Only fixed classifications survive; the existing client redacts its cache.
    if isinstance(exc, ContractProbeError):
        return exc.category
    if isinstance(exc, RequestBudgetError):
        return "request_budget_or_deadline_blocked"
    message = str(exc).lower()
    if "未配置 football_api_key" in message:
        return "credential_configuration_missing"
    if "不允许该 endpoint" in message:
        return "client_endpoint_not_allowed"
    if "http 403" in message:
        return "provider_http_403"
    if "套餐" in message or "subscription" in message:
        return "provider_plan_season_or_quota_error"
    if "http 429" in message:
        return "provider_http_429"
    if isinstance(exc, DataError):
        return "response_contract_invalid"
    if isinstance(exc, APIError):
        return "provider_or_transport_error"
    return "unexpected_error"


def _validate_envelope(value, endpoint, params, checked_at):
    if (not isinstance(value, dict) or value.get("endpoint") != endpoint
            or value.get("params") != params or "errors" not in value
            or not isinstance(value["errors"], (list, dict)) or value["errors"]
            or not isinstance(value.get("response"), list)
            or not isinstance(value.get("paging", {}), dict)):
        raise DataError("invalid envelope")
    pages = value.get("paging", {}).get("total", 1)
    if type(pages) is not int or pages != 1:
        raise DataError("incomplete paginated response")
    if value.get("from_cache") is not False:
        raise DataError("a fresh contract observation was required")
    if _utc(value.get("fetched_at")) > checked_at:
        raise DataError("observation in future")
    return value["response"]


def _fixture_teams(rows, fixture_id):
    if len(rows) != 1 or not isinstance(rows[0], dict):
        raise DataError("fixture must be unique")
    item = rows[0]
    try:
        if _integer(item["fixture"]["id"], "fixture.id", 1) != fixture_id:
            raise DataError("fixture identity mismatch")
        kickoff = _utc(item["fixture"]["date"])
        timestamp = item["fixture"].get("timestamp")
        if timestamp is not None and abs(_integer(timestamp, "fixture.timestamp") - kickoff.timestamp()) > 1:
            raise DataError("fixture timestamp conflict")
        teams = [_integer(item["teams"][side]["id"], side + ".id", 1)
                 for side in ("home", "away")]
        if teams[0] == teams[1]:
            raise DataError("duplicate team identity")
    except (KeyError, TypeError) as exc:
        raise DataError("fixture contract fields missing") from exc
    return teams


def _squad_players(rows, team_id):
    if not rows:
        return [], {"raw_player_rows": 0, "observed_player_ids": [], "unique_subject_count": 0,
                    "issues": [], "subject_selection_scope": "endpoint_contract_only_not_roster_verification"}
    if len(rows) != 1 or not isinstance(rows[0], dict):
        raise DataError("squad team must be unique")
    try:
        if _integer(rows[0]["team"]["id"], "team.id", 1) != team_id:
            raise DataError("squad team mismatch")
        players = rows[0]["players"]
        if not isinstance(players, list):
            raise DataError("squad players missing")
        by_id = {}
        for index, player in enumerate(players):
            pid = _integer(player["id"], "player.id", 1)
            by_id.setdefault(pid, []).append((index, player))
    except (KeyError, TypeError) as exc:
        raise DataError("squad contract fields missing") from exc
    issues, selectable = [], []
    for pid, occurrences in by_id.items():
        if len(occurrences) == 1:
            selectable.append((0, pid))
            continue
        versions = {_json_bytes(player) for _, player in occurrences}
        issue = {"category": "duplicate_squad_source_rows", "player_id": pid,
                 "source_row_indices": [index for index, _ in occurrences],
                 "occurrences": len(occurrences)}
        if len(versions) == 1:
            issue["duplicate_kind"] = "exact_source_row_repetition"
            selectable.append((1, pid))
        else:
            names = [player.get("name") for _, player in occurrences]
            fields = sorted({name for _, player in occurrences for name in player})
            issue["conflicting_fields"] = [name for name in fields if len({
                _json_bytes({"present": name in player, "value": player.get(name)})
                for _, player in occurrences}) > 1]
            if all(isinstance(name, str) and name.strip() and name == names[0] for name in names):
                issue["duplicate_kind"] = "consistent_id_name_other_fields_conflict"
                selectable.append((2, pid))
            else:
                issue["duplicate_kind"] = "subject_name_conflict_or_unresolved"
                issue["subject_excluded"] = True
        issues.append(issue)
    diagnostic = {"raw_player_rows": len(players), "observed_player_ids": sorted(by_id),
        "unique_subject_count": len(by_id), "duplicate_subject_count": len(issues),
        "extra_duplicate_rows": len(players) - len(by_id), "issues": issues,
        "subject_selection_scope": "endpoint_contract_only_not_roster_verification",
        "selection_order": "singleton_then_exact_duplicate_then_consistent_id_name;numeric_id_within_tier",
        "raw_response_rewritten_or_cleaned": False,
        "roster_contract_verified": False}
    return [pid for _, pid in sorted(selectable)], diagnostic


def _sidelined_contract(rows, player_id):
    missing = set()
    for row in rows:
        if not isinstance(row, dict):
            raise DataError("sidelined item not object")
        if "player" in row:
            if (not isinstance(row["player"], dict)
                    or _integer(row["player"].get("id"), "player.id", 1) != player_id):
                raise DataError("sidelined subject mismatch")
        for name in ("type", "start", "end"):
            if name not in row:
                missing.add(name)
    # Unknown end strings and null values are retained, never date-imputed.
    return sorted(missing)


def _date_field_states(rows):
    states = []
    for index, row in enumerate(rows):
        entry = {"response_index": index}
        for name in ("start", "end"):
            value = row.get(name)
            if name not in row:
                state = "absent"
            elif value is None:
                state = "null"
            elif isinstance(value, str) and value.casefold() == "unknown":
                state = "unknown_string"
            elif isinstance(value, str):
                try:
                    parsed = date.fromisoformat(value)
                    state = "calendar_date" if parsed.isoformat() == value else "invalid_date_string"
                except ValueError:
                    state = "invalid_date_string"
            else:
                state = "invalid_type"
            entry[name] = state
        states.append(entry)
    return states


def run_probe(fixture_id, output, *, client=None, clock=None, source_commit=None):
    """Return (exit_code, manifest); injected clients are for offline tests only.

    Seven paid calls maximum, with no network retries, plus one /status call.
    Official current endpoints go through the existing client's cache/request
    admission. There is no alternate HTTP transport or credential handling.
    """
    _integer(fixture_id, "fixture_id", 1)
    if source_commit is not None and not re.fullmatch(r"[0-9a-f]{40}", source_commit):
        raise ValueError("source_commit must be a full lowercase commit SHA")
    clock = clock or (lambda: datetime.now(timezone.utc))
    started = _utc(clock())
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    sources = []
    for relative in ("football_model/data.py", "config.json", "scripts/personnel_contract_probe.py"):
        path = ROOT / relative
        sources.append({"path": relative, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    report = {"schema_version": "personnel-contract-probe-v2", "fixture_id": fixture_id,
              "started_at": _iso(started), "source_commit": source_commit,
              "implementation_files": sources, "official_reference_urls": OFFICIAL_REFERENCES,
              "client_implementation": ContractProbeClient.implementation_version,
              "new_probe_only_endpoints": ["/players/squads", "/sidelined"],
              "probe_evidence_transport_paths": ["/fixtures", "/players/squads", "/sidelined"],
              "request_limits": {"paid": MAX_PAID_REQUESTS, "total_including_status": MAX_TOTAL_REQUESTS,
                                 "retries": 0, "configured_daily_limit": DAILY_LIMIT,
                                 "daily_requests_reserved": DAILY_RESERVE},
              "response_storage_representation": "redacted_client_envelope_canonical_json_not_http_wire_bytes",
              "semantics": SEMANTICS.copy(), "observations": [], "issues": [], "contract_issues": []}
    paid_calls = 0

    def observe(endpoint, params, fetch):
        nonlocal paid_calls
        if paid_calls >= MAX_PAID_REQUESTS:
            raise RequestBudgetError("probe paid request bound reached")
        paid_calls += 1
        item = {"endpoint": endpoint, "params": params,
                "request_started_at": _iso(_utc(clock())), "status": "pending"}
        report["observations"].append(item)
        try:
            value = fetch()
            # APIClient has already redacted every returned/cached payload.
            reference = _write(output / f"response-{len(report['observations']):02d}.json", value)
            item["source"] = reference
            item["observed_schema"] = _schema(value)
            rows = _validate_envelope(value, endpoint, params, _utc(clock()))
            item.update(status="observed", fetched_at=value["fetched_at"], response_count=len(rows),
                        empty_response_meaning="unknown_not_health" if not rows else None)
            return rows, item
        except (APIError, DataError, ValueError, KeyError, TypeError, OSError) as exc:
            item.update(status="blocked", failure_category=_failure(exc))
            if isinstance(exc, ContractProbeError) and exc.safe_evidence is not None:
                item["failure_source"] = _write(output / f"failure-{len(report['observations']):02d}.json",
                                                 exc.safe_evidence)
                item["failure_schema"] = _schema(exc.safe_evidence)
            return None, item

    stack = ExitStack()
    try:
        client = client or ContractProbeClient(output / "cache", max_run_seconds=120, clock=clock)
        stack.enter_context(client.reserve_requests(DAILY_RESERVE))
        # status() returns numeric quota evidence, not the raw account response.
        client.status()
        report["initial_budget"] = client.budget_snapshot()
        fixture, item = observe("/fixtures", {"id": fixture_id},
                                lambda: client.fetch_fixture(fixture_id, force=True))
        teams = None
        if fixture is not None:
            try:
                teams = _fixture_teams(fixture, fixture_id)
                report["fixture_team_ids"] = teams
            except (DataError, ValueError) as exc:
                item.update(status="blocked", failure_category=_failure(exc))
        if teams:
            squads = []
            for team_id in teams:
                params = {"team": team_id}
                rows, item = observe("/players/squads", params,
                    lambda params=params: client._cached_request("/players/squads", params,
                                                               force=True, max_age_seconds=0))
                if rows is None:
                    continue
                try:
                    ids, diagnostic = _squad_players(rows, team_id)
                    item["subject_selection"] = diagnostic
                    if diagnostic["issues"]:
                        item["status"] = "observed_with_contract_issues"
                        report["contract_issues"].extend({"endpoint": "/players/squads", "team_id": team_id,
                                                         **issue} for issue in diagnostic["issues"])
                    squads.append((team_id, ids, diagnostic["observed_player_ids"], item))
                    if not ids:
                        report["issues"].append({"category": "no_observed_player_ids", "team_id": team_id})
                except (DataError, ValueError) as exc:
                    item.update(status="blocked", failure_category=_failure(exc))
            # Cross-check whole observed ID sets, including non-selected and
            # unresolved-name subjects. A conflict cannot hide behind top-two.
            counts = {}
            for _, _, observed_ids, _ in squads:
                for pid in observed_ids:
                    counts[pid] = counts.get(pid, 0) + 1
            conflicts = sorted(pid for pid, count in counts.items() if count > 1)
            report["contract_issues"].extend({"category": "player_team_identity_conflict", "player_id": pid}
                                              for pid in conflicts)
            selected = []
            for team_id, candidates, _, item in squads:
                ids = [pid for pid in candidates if counts[pid] == 1][:2]
                item["selected_player_ids"] = ids
                item["cross_team_excluded_player_ids"] = [pid for pid in candidates if counts[pid] > 1]
                if item["cross_team_excluded_player_ids"]:
                    item["status"] = "observed_with_contract_issues"
                selected.extend((team_id, pid) for pid in ids)
            for team_id, player_id in selected:
                params = {"player": player_id}
                rows, item = observe("/sidelined", params,
                    lambda params=params: client._cached_request("/sidelined", params,
                                                               force=True, max_age_seconds=0))
                item["selected_from_team_id"] = team_id
                if rows is not None:
                    try:
                        missing = _sidelined_contract(rows, player_id)
                        item["missing_contract_fields"] = missing
                        item["date_field_states"] = _date_field_states(rows)
                        if missing:
                            item.update(status="blocked", failure_category="contract_fields_missing")
                    except (DataError, ValueError) as exc:
                        item.update(status="blocked", failure_category=_failure(exc))
    except (APIError, DataError, ValueError, KeyError, TypeError, OSError) as exc:
        report["issues"].append({"category": _failure(exc), "stage": "initialization_or_status"})
    finally:
        if client is not None:
            report["final_budget"] = client.budget_snapshot()
        report["finished_at"] = _iso(_utc(clock()))
        report["paid_probe_calls_attempted"] = paid_calls
        observed = report["observations"]
        sidelined = [item for item in observed if item["endpoint"] == "/sidelined"]
        report["selected_subject_sidelined_observation_status"] = (
            "observed_for_selected_subjects_only" if sidelined and all(item["status"] == "observed" for item in sidelined)
            else "partial_or_blocked" if sidelined else "not_attempted")
        complete = (len(observed) >= 3 and not report["issues"] and not report["contract_issues"]
                    and all(item["status"] == "observed" for item in observed))
        report["status"] = "contract_observations_complete" if complete else "incomplete_or_blocked"
        report["effect_or_provider_coverage_verified"] = False
        report["exit_code"] = 0 if complete else 2
        _write(output / "manifest.json", report)
        stack.close()
    return report["exit_code"], report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source-commit")
    args = parser.parse_args()
    if not re.fullmatch(r"[1-9][0-9]*", args.fixture_id):
        parser.error("fixture-id must be a real positive integer provider ID")
    try:
        code, report = run_probe(int(args.fixture_id), args.output, source_commit=args.source_commit)
    except (ValueError, OSError):
        parser.exit(2, "probe input or output directory rejected; no previous output overwritten\n")
    print(json.dumps({"status": report["status"], "fixture_id": report["fixture_id"],
                      "paid_probe_calls_attempted": report["paid_probe_calls_attempted"],
                      "manifest": str(args.output / "manifest.json")}, ensure_ascii=False))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
