"""Fixed-state, read-only R07 arithmetic audit. No fits or activation.

Consumes complete immutable saved research reports, but claims only saved-score
gate/variance/planning verification. It does not replace raw-feature rebuilding,
independent R fitting, current-implementation eligibility or future confirmation.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import gc
import gzip
import hashlib
import json
import math
from pathlib import Path
import re
import sys
import tempfile
import time
import traceback

sys.dont_write_bytecode = True
REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
from football_model import advanced_confirmation as confirmation
from football_model.advanced_research import implementation_fingerprint
from scipy.stats import chi2, norm

OLD_IMPLEMENTATION = "1e311689b3405f64c6ad9636732d2defb7ad67e26b4bc7e04b01a5b1b2b2d6ac"
DESIGN_RAW_SHA256 = "a6fa0c1b8266631266e28ae9de6567403d6a6d628e80b676263edfd48d0da007"
PROTOCOL_RAW_SHA256 = "4139069bbe6f13c27020b32655dedfa8620475ff80287955a43ee96b00501364"
NUMERICAL_FILES = ("football_model/advanced_confirmation.py", "football_model/advanced_research.py",
    "football_model/advanced_storage.py", "football_model/feature_research.py",
    "football_model/feature_correction.py", "football_model/model.py", "requirements.txt")
JSON_LIMIT = 512 * 1024
OUTPUT_LIMIT = 32 * 1024 * 1024
WITNESS_LIMIT = 28 * 1024 * 1024
TERMINAL_RESERVE = 1024 * 1024


class AuditError(ValueError):
    pass


def require(value, code):
    if not value:
        raise AuditError(code)


def raw(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False) + "\n").encode()


def sha(data):
    return hashlib.sha256(data).hexdigest()


def timestamp(value):
    require(isinstance(value, str), "timestamp_not_string")
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    require(result.tzinfo is not None, "naive_timestamp")
    return result.astimezone(timezone.utc)


def validate_selection(state, implementation, leagues, design):
    require(bool(re.fullmatch(r"[a-f0-9]{40}", state)), "exact_fixed_state_sha_required")
    require(bool(re.fullmatch(r"[a-f0-9]{64}", implementation)), "invalid_implementation")
    items = leagues.split(",")
    require(bool(items) and len(set(items)) == len(items)
        and all(item in design["registered_leagues"] for item in items), "invalid_league_selection")
    return items


def bounded_equal(actual, expected):
    if type(expected) in (int, str, bool) or expected is None:
        require(type(actual) is type(expected) and actual == expected, "independent_exact_mismatch")
    elif isinstance(expected, float):
        require(type(actual) in (int, float) and math.isfinite(actual) and math.isfinite(expected)
            and math.isclose(actual, expected, rel_tol=1e-12, abs_tol=1e-15), "independent_numeric_mismatch")
    elif isinstance(expected, dict):
        require(isinstance(actual, dict) and set(actual) == set(expected), "independent_field_mismatch")
        for key in expected:
            bounded_equal(actual[key], expected[key])
    else:
        raise AuditError("unsupported_independent_comparison")


class Output:
    def __init__(self, path, repo):
        path, repo = Path(path), Path(repo).resolve()
        require(not path.exists() and not path.is_symlink(), "output_must_be_new")
        absolute = path.absolute()
        require(not any(p.is_symlink() for p in (absolute, *absolute.parents)), "output_symlink")
        absolute = absolute.resolve()
        require(not any(absolute.is_relative_to(repo / p) for p in
            ("runtime", "football_model", "scripts", "tests", ".github", ".git")), "protected_output_path")
        absolute.parent.mkdir(parents=True, exist_ok=True)
        absolute.mkdir()
        self.path, self.bytes, self.witness_bytes = absolute, 0, 0
        self.files = []

    def write_json(self, name, value, *, terminal=False):
        data = raw(value)
        limit = OUTPUT_LIMIT if terminal else OUTPUT_LIMIT - TERMINAL_RESERVE
        require(len(data) <= JSON_LIMIT and self.bytes + len(data) <= limit, "output_budget_exceeded")
        with (self.path / name).open("xb") as handle:
            handle.write(data)
        self.bytes += len(data)
        ref = {"path": name, "bytes": len(data), "sha256": sha(data)}
        self.files.append(ref)
        return ref

    def progress(self, stage, **fields):
        event = {"stage": stage, "at": datetime.now(timezone.utc).isoformat(), **fields}
        # Progress stays small and does not expose provider text or payloads.
        print(json.dumps({"r07_progress": event}, ensure_ascii=False), flush=True)
        return event

    def witness(self, name):
        require(self.bytes + 32 <= OUTPUT_LIMIT - TERMINAL_RESERVE
            and self.witness_bytes + 32 <= WITNESS_LIMIT, "numeric_witness_budget_exceeded")
        return Witness(self, name)


class Witness:
    def __init__(self, output, name):
        self.output, self.name = output, name
        self.file = (output.path / (name + ".partial")).open("xb")
        self.hash, self.raw_hash = hashlib.sha256(), hashlib.sha256()
        self.bytes, self.raw_bytes, self.rows = 0, 0, 0
        self.closed_ref = None
        self.stream = gzip.GzipFile(filename="", fileobj=self, mode="wb", mtime=0)

    def write(self, data):
        require(self.output.bytes + len(data) <= OUTPUT_LIMIT - TERMINAL_RESERVE
            and self.output.witness_bytes + len(data) <= WITNESS_LIMIT, "numeric_witness_budget_exceeded")
        written = self.file.write(data)
        self.hash.update(data)
        self.bytes += written
        self.output.bytes += written
        self.output.witness_bytes += written
        return written

    def flush(self):
        self.file.flush()

    def tell(self):
        return self.file.tell()

    def row(self, value):
        data = raw(value)
        require(len(data) <= 16 * 1024, "numeric_witness_row_limit")
        self.raw_hash.update(data)
        self.raw_bytes += len(data)
        self.rows += 1
        self.stream.write(data)

    def close(self, success):
        if self.closed_ref is not None:
            return self.closed_ref
        close_error = None
        try:
            self.stream.close()
        except Exception as exc:
            close_error = exc
            success = False
        finally:
            self.file.close()
        ref = {"path": self.name if success else self.name + ".partial", "bytes": self.bytes,
            "sha256": self.hash.hexdigest(), "complete": success}
        if success:
            ref.update(uncompressed_bytes=self.raw_bytes,
                uncompressed_sha256=self.raw_hash.hexdigest(), rows=self.rows)
        else:
            ref.update(attempted_uncompressed_bytes=self.raw_bytes,
                attempted_uncompressed_sha256=self.raw_hash.hexdigest(), attempted_rows=self.rows,
                complete_decompressed_rows=None)
        if close_error is not None:
            ref["close_error_class"] = type(close_error).__name__
        if success:
            (self.output.path / (self.name + ".partial")).rename(self.output.path / self.name)
        self.output.files.append(ref)
        self.closed_ref = ref
        if close_error is not None and isinstance(close_error, AuditError):
            # Preserve the hard-limit failure while retaining its actual partial
            # bytes in the manifest; the outer handler can close idempotently.
            raise close_error
        if close_error is not None:
            raise close_error
        return ref


def validate_identity(report, summary, expected, protocol, repo):
    fingerprint = report.get("implementation_fingerprint")
    require(isinstance(fingerprint, dict) and set(fingerprint) == {"schema_version", "files", "sha256"}
        and fingerprint["schema_version"] == "advanced-implementation-fingerprint-v1", "invalid_saved_implementation")
    files = fingerprint["files"]
    require(isinstance(files, dict) and len(files) == 50
        and all(isinstance(k, str) and isinstance(v, str) and re.fullmatch(r"[0-9a-f]{64}", v)
                for k, v in files.items())
        and confirmation.digest(files) == fingerprint["sha256"] == expected, "saved_implementation_mismatch")
    require(report.get("purpose") == "historical_development_only", "not_historical_development")
    require(report.get("protocol") == protocol and report.get("protocol_hash") == confirmation.digest(protocol),
        "report_protocol_mismatch")
    for key in ("league", "source_commit", "protocol_hash", "implementation_fingerprint"):
        require(report.get(key) == summary.get(key), "summary_full_report_identity_mismatch")
    require(isinstance(report.get("source_commit"), str)
        and re.fullmatch(r"[a-f0-9]{40}", report["source_commit"]), "report_source_invalid")
    require(report.get("state_commit") is None or isinstance(report["state_commit"], str)
        and re.fullmatch(r"[a-f0-9]{40}", report["state_commit"]), "report_input_state_invalid")
    if report.get("generated_at") is not None:
        timestamp(report["generated_at"])
    identities = []
    for path in NUMERICAL_FILES:
        digest = sha((repo / path).read_bytes())
        require(digest == files.get(path), "numerical_source_differs_from_saved_study")
        identities.append({"path": path, "sha256": digest})
    return identities


def validate_rows(report):
    rows = report.get("scored_matches")
    require(isinstance(rows, list), "scored_rows_missing")
    by_horizon = {"EARLY": [], "LINEUP": []}
    seen = set()
    for row in rows:
        require(isinstance(row, dict) and type(row.get("match_id")) is int and row["match_id"] > 0,
            "invalid_fixture_id")
        horizon = row.get("horizon")
        require(horizon in by_horizon, "invalid_horizon")
        key = (horizon, row["match_id"])
        require(key not in seen, "duplicate_scored_fixture")
        seen.add(key)
        require(timestamp(row["cutoff_at"]) < timestamp(row["kickoff"]), "cutoff_not_before_kickoff")
        require(isinstance(row.get("scores"), dict) and isinstance(row.get("candidate_failures", {}), dict),
            "invalid_score_row")
        by_horizon[horizon].append(row)
    require(report.get("requested_horizon_opportunities") == len(rows), "requested_opportunity_count_mismatch")
    replays = report.get("replays")
    require(isinstance(replays, dict) and set(replays) == set(by_horizon), "replay_horizons_mismatch")
    for horizon, selected in by_horizon.items():
        expected = {}
        for row in replays[horizon]["forecasts"] + replays[horizon]["failures"]:
            identity = row["match_id"]
            require(type(identity) is int and identity not in expected, "duplicate_replay_fixture")
            expected[identity] = row
        require({r["match_id"] for r in selected} == set(expected), "full_request_universe_mismatch")
        for row in selected:
            require(row["cutoff_at"] == expected[row["match_id"]]["cutoff_at"], "replay_cutoff_mismatch")
            require(timestamp(row["kickoff"]) == timestamp(expected[row["match_id"]]["kickoff"]), "replay_kickoff_mismatch")
        selected.sort(key=lambda row: (timestamp(row["kickoff"]), row["match_id"]))
    early = {r["match_id"]: timestamp(r["kickoff"]) for r in by_horizon["EARLY"]}
    lineup = {r["match_id"]: timestamp(r["kickoff"]) for r in by_horizon["LINEUP"]}
    require(early == lineup and report.get("requested_matches") == len(early), "cross_horizon_universe_mismatch")
    return by_horizon


def cadence(rows, window=None):
    times = [timestamp(row["kickoff"]) for row in rows]
    if window is None:
        first, last = (min(times), max(times)) if times else (None, None)
    else:
        first, last = window
    days = (last - first).total_seconds() / 86400 if first is not None and last is not None else None
    rate = len(rows) / days if days is not None and days > 0 else None
    return {"count": len(rows), "first_kickoff": first.isoformat() if first else None,
        "last_kickoff": last.isoformat() if last else None, "elapsed_days": days,
        "opportunities_per_elapsed_day": rate, "opportunities_per_365_25_days": rate * 365.25 if rate is not None else None,
        "unavailable_reason": None if rate is not None else "empty_or_zero_elapsed_span",
        "method": "observed_count_divided_by_actual_first_to_last_elapsed_days;historical_extrapolation_only"}


def independent_plan(differences, design, family_size):
    n = len(differences)
    if n < design["min_pilot_matches"]:
        return None
    alpha = design["alpha"] / (family_size * len(confirmation.METRICS))
    upper_scale = (n - 1) / float(chi2.ppf(design["pilot_variance_lower_quantile"], n - 1))
    zsum = float(norm.ppf(1 - alpha) + norm.ppf(design["power"]))
    components = {}
    for metric in confirmation.METRICS:
        values = [r[metric] for r in differences]
        avg = math.fsum(values) / n
        # Independent ddof=1 calculation, not statistics.variance used by native.
        variance = math.fsum((value - avg) ** 2 for value in values) / (n - 1)
        upper = upper_scale * variance
        effect = design["minimum_effect_nll"] if metric == "scoreline_nll" else design["noninferiority_margins"][metric]
        required = max(design["min_pilot_matches"], math.ceil(upper * (zsum / effect) ** 2))
        components[metric] = {"pilot_n": n, "pilot_mean_difference": avg, "pilot_variance": variance,
            "planning_variance_upper": upper, "assumed_detectable_effect_or_margin": effect,
            "required_scored_pairs": required}
    pairs = max(x["required_scored_pairs"] for x in components.values())
    return {"family_size": family_size, "metric_tests_per_hypothesis": len(confirmation.METRICS),
        "per_test_alpha": alpha, "target_power": design["power"], "required_scored_pairs": pairs,
        "required_queue_opportunities": math.ceil(pairs / design["min_pair_coverage"]), "components": components,
        "method": "paired_normal_upper_pilot_variance_then_terminal_student_t",
        "qualification": "approximate_planning_power_not_guaranteed;fixture_dependence_not_eliminated"}


def candidate_differences(rows, candidate, horizon, witness=None):
    differences, native, readiness, baseline = [], 0, 0, 0
    groups = Counter()
    digest = hashlib.sha256()
    for row in rows:
        warmup = row.get("candidate_failures", {}).get("M0", {}).get("code") == "insufficient_training_history"
        scores = row["scores"]
        base, original = scores.get("M0"), scores.get(candidate)
        native_present, base_present = candidate in scores, "M0" in scores
        expected = original if native_present else base
        policy = scores.get(candidate + "_policy", expected)
        if expected is None:
            require(candidate + "_policy" not in scores, "invented_policy_score")
        else:
            # Verify all five metric values of the saved policy, including true fallback.
            require(confirmation.project_metrics(policy) == confirmation.project_metrics(expected), "saved_policy_changed")
        group = "native_paired" if base_present and native_present else "fallback" if base_present else "native_only" if native_present else "unforecast"
        groups[group] += 1
        difference = None
        if not warmup:
            readiness += 1
            if base_present:
                baseline += 1
                native += int(native_present)
                base_values, values = confirmation.project_metrics(base), confirmation.project_metrics(policy)
                difference = {metric: values[metric] - base_values[metric] for metric in confirmation.METRICS}
                differences.append(difference)
        record = {"match_id": row["match_id"], "kickoff": row["kickoff"], "horizon": horizon,
            "candidate_id": candidate, "explicit_warmup": warmup, "denominator_group": group,
            "M0_present": base_present, "native_present": native_present, "policy_minus_M0": difference}
        digest.update(raw(record))
        if witness is not None:
            witness.row(record)
    return differences, {"requested": len(rows), "readiness": readiness, "M0_pairs": baseline,
        "native_pairs": native, "groups": dict(groups), "difference_witness_sha256": digest.hexdigest()}


def calendar_projection(gate, rates, design):
    plan = gate["sample_plan"]
    queue_rate = rates["readiness_opportunities"]["opportunities_per_elapsed_day"]
    all_rate = rates["all_requested"]["opportunities_per_elapsed_day"]
    pair_rate = rates["M0_scored_pairs_same_readiness_window"]["opportunities_per_elapsed_day"]
    def divide(value, rate):
        return value / rate if rate is not None and rate > 0 else None
    days = divide(plan["required_queue_opportunities"], queue_rate) if plan else None
    all_days = divide(plan["required_queue_opportunities"], all_rate) if plan else None
    pair_days = divide(plan["required_scored_pairs"], pair_rate) if plan else None
    variance = bool(plan and any(x["pilot_variance"] > 0 for x in plan["components"].values()))
    qualified = gate["native_candidate_count"] > 0 and variance
    span = design["max_calendar_days"] - 11 / 24
    return {"candidate_id": gate["candidate_id"], "horizon": gate["horizon"],
        "native_count": gate["native_candidate_count"], "paired_count": gate["paired_count"],
        "candidate_variance_evidence_available": qualified,
        "candidate_based_calendar_estimate_days": days if qualified else None,
        "candidate_plan_null_reason": "insufficient_valid_development_pairs" if plan is None else
            "native_absent_or_zero_variance_floor_only" if not qualified else None,
        "raw_formula_calendar_projection": {"queue_days_at_readiness_cadence": days,
            "queue_years_at_readiness_cadence": days / 365.25 if days is not None else None,
            "queue_days_at_all_requested_cadence_sensitivity": all_days,
            "pairs_days_at_observed_pair_cadence_sensitivity": pair_days,
            "projected_terminal_elapsed_days_from_hypothetical_activation": days + design["start_delay_hours"] / 24 + 11 / 24 + design["result_maturity_hours"] / 24 if days is not None else None,
            "admissible_kickoff_span_days": span,
            "fits_730_day_protocol_at_primary_historical_cadence": days <= span if days is not None else None,
            "fits_730_day_protocol_at_all_requested_sensitivity_cadence": all_days <= span if all_days is not None else None},
        "development_gate_status": gate["status"], "actual_future_calendar_verified": False,
        "absolute_completion_date": None, "activation_performed": False,
        "qualification": "historical_arithmetic_only_not_future_schedule_or_joint_power_guarantee"}


def audit_arithmetic(report, protocol, design, witness=None, progress=None):
    confirmation.validate_design(design)
    specs = confirmation._candidate_specs(protocol)
    require(len(specs) == 20 and len(design["registered_leagues"]) == 19, "original_family_mismatch")
    family = len(specs) * len(design["registered_leagues"])
    by_horizon = validate_rows(report)
    rates = {}
    for horizon, rows in by_horizon.items():
        ready = [r for r in rows if r.get("candidate_failures", {}).get("M0", {}).get("code") != "insufficient_training_history"]
        paired = [r for r in ready if "M0" in r["scores"]]
        window = (timestamp(ready[0]["kickoff"]), timestamp(ready[-1]["kickoff"])) if ready else (None, None)
        rates[horizon] = {"all_requested": cadence(rows), "readiness_opportunities": cadence(ready),
            "M0_scored_pairs_same_readiness_window": cadence(paired, window)}
    gates, calendars, checks = [], [], []
    for candidate, horizon in specs:
        gate = confirmation.development_gate(report, candidate, horizon, design, family)
        differences, counts = candidate_differences(by_horizon[horizon], candidate, horizon, witness)
        require(counts["readiness"] == gate["readiness_opportunities"] and len(differences) == gate["paired_count"]
            and counts["native_pairs"] == gate["native_candidate_count"], "independent_denominator_mismatch")
        require(gate["pair_coverage"] == (len(differences) / counts["readiness"] if counts["readiness"] else 0.0)
            and gate["native_candidate_coverage"] == (counts["native_pairs"] / counts["M0_pairs"] if counts["M0_pairs"] else 0.0),
            "independent_coverage_mismatch")
        bounded_equal({m: math.fsum(d[m] for d in differences) / len(differences) for m in confirmation.METRICS} if differences else {},
            gate["mean_policy_differences"])
        repeated = confirmation.sample_plan(differences, design, family) if len(differences) >= design["min_pilot_matches"] else None
        require(repeated == gate["sample_plan"], "native_plan_recomputation_mismatch")
        bounded_equal(independent_plan(differences, design, family), gate["sample_plan"])
        gates.append(gate)
        calendars.append(calendar_projection(gate, rates[horizon], design))
        checks.append({"candidate_id": candidate, "horizon": horizon, **counts,
            "native_plan_exact_match": True, "independent_plan_verified": True})
        if progress:
            progress("candidate_completed", candidates=len(gates), candidate_id=candidate, horizon=horizon)
    return {"gates": gates, "calendar": calendars, "independent_checks": checks, "historical_cadence": rates,
        "family_size": family, "metrics_per_hypothesis": len(confirmation.METRICS), "metric_tests": family * len(confirmation.METRICS),
        "counts": {"unique_fixtures": len(by_horizon["EARLY"]), "horizon_rows": len(report["scored_matches"]),
            "blocked_gates": sum(g["status"] != "passed" for g in gates),
            "zero_native_floor_plans": sum(g["native_candidate_count"] == 0 and g["sample_plan"] is not None for g in gates)},
        "calendar_rules": {"start_delay_hours": design["start_delay_hours"], "earliest_kickoff_after_start_hours": 11,
            "max_calendar_days": design["max_calendar_days"], "maturity_hours": design["result_maturity_hours"],
            "latest_terminal_days_after_activation": design["start_delay_hours"] / 24 + design["max_calendar_days"] + design["result_maturity_hours"] / 24}}


def failure(exc, stage):
    return {"status": "audit_incomplete", "failed_stage": stage, "exception_class": type(exc).__name__,
        "error_code": str(exc) if isinstance(exc, AuditError) else "unclassified_audit_failure",
        "frames": [{"file": Path(f.filename).name, "function": f.name, "line": f.lineno}
            for f in traceback.extract_tb(exc.__traceback__)[-4:]]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--state-commit", required=True)
    parser.add_argument("--implementation", default=OLD_IMPLEMENTATION)
    parser.add_argument("--leagues", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    repo = args.repo.resolve()
    require(repo == REPO.resolve(), "auditor_repo_mismatch")
    design_raw, protocol_raw = (repo / "advanced-confirmation-v1.json").read_bytes(), (repo / "advanced-models-v1.json").read_bytes()
    require(sha(design_raw) == DESIGN_RAW_SHA256 and sha(protocol_raw) == PROTOCOL_RAW_SHA256,
        "original_protocol_bytes_changed")
    design, protocol = json.loads(design_raw), json.loads(protocol_raw)
    leagues = validate_selection(args.state_commit, args.implementation, args.leagues, design)
    confirmation.validate_design(design)
    before = implementation_fingerprint()
    out = Output(args.output, repo)
    receipt = {"schema_version": "fixed-development-gate-audit-v1", "started_at": datetime.now(timezone.utc).isoformat(),
        "retrieved_state_commit": args.state_commit, "requested_leagues": leagues, "expected_saved_implementation": args.implementation,
        "auditor_implementation": before, "protocol_raw_sha256": sha(protocol_raw), "design_raw_sha256": sha(design_raw),
        "protocol_hash": confirmation.digest(protocol), "design_hash": confirmation.digest(design),
        "auditor_script_sha256": sha(Path(__file__).read_bytes()), "results": [], "status": "audit_in_progress",
        "network_football_requests": 0, "fits": 0, "remote_state_writes": 0, "activation_performed": False,
        "production_promoted": False, "limits": {"json_file_bytes": JSON_LIMIT, "total_output_bytes": OUTPUT_LIMIT,
            "compressed_witness_bytes": WITNESS_LIMIT, "terminal_reserve_bytes": TERMINAL_RESERVE,
            "production_resource_limits_changed": False},
        "scope": "fixed_saved_score_gate_variance_sample_plan_and_historical_calendar_only",
        "limitations": ["Not independent raw-to-feature reconstruction or independent R refit.",
            "Old saved implementation remains old; numerical source equality does not retag current eligibility.",
            "Historical cadence is not a verified future calendar, capture coverage or joint power guarantee.",
            "Gate success alone does not verify complete model provenance or authorize activation."]}
    out.write_json("run-started.json", receipt)
    stage, started = "initialize", time.monotonic()
    def progress(label, **fields):
        nonlocal stage
        stage = label
        return out.progress(label, elapsed_seconds=round(time.monotonic() - started, 3), **fields)
    try:
        sys.path.insert(0, str(repo / "scripts"))
        import cloud_state as cloud
        from fixed_fixture_validation_audit import restore_fixed
        from football_model.advanced_storage import load_artifact
        from football_model.history_archive import read_bytes
        receipt["auditor_source_commit"] = cloud._git(repo, "rev-parse", "HEAD").stdout.strip()
        with tempfile.TemporaryDirectory(prefix="fixed-r07-", dir=repo) as temporary:
            runtime = Path(temporary) / "runtime"
            progress("restore_exact_fixed_state_once")
            receipt["restore_preflight"] = restore_fixed(repo, runtime, args.state_commit, cloud)
            for league in leagues:
                witness = None
                report = computation = body = None
                result = {"league": league}
                try:
                    progress("read_fixed_summary", league=league)
                    pointer = runtime / "advanced_models" / league / confirmation.digest(protocol) / "reports/latest.json"
                    summary_raw = read_bytes(pointer, runtime=runtime)
                    summary = json.loads(summary_raw)
                    require(summary.get("league") == league, "selected_league_mismatch")
                    progress("expand_complete_report", league=league)
                    report = load_artifact(runtime, summary["full_report_ref"])
                    numeric_sources = validate_identity(report, summary, args.implementation, protocol, repo)
                    result.update(report_ref=summary["full_report_ref"], summary_sha256=sha(summary_raw),
                        source_commit=report["source_commit"], report_input_state_commit=report.get("state_commit"),
                        report_generated_at=report.get("generated_at"), saved_implementation=report["implementation_fingerprint"],
                        numerical_sources_verified=numeric_sources)
                    witness = out.witness(league + "-numeric-witness.jsonl.gz")
                    progress("recompute_all_twenty_original_gates", league=league)
                    computation = audit_arithmetic(report, protocol, design, witness, progress)
                    progress("finalize_numeric_witness", league=league)
                    witness_ref = witness.close(True)
                    witness = None
                    body = {"schema_version": "fixed-league-development-gates-v1", **result,
                        "retrieved_state_commit": args.state_commit, "protocol_hash": confirmation.digest(protocol),
                        "design_hash": confirmation.digest(design), "numeric_witness": witness_ref,
                        **computation, "status": "completed_with_stated_scope", "activation_performed": False,
                        "current_implementation_eligibility_claimed": False}
                    progress("write_league_gate_receipt", league=league)
                    record = out.write_json(league + "-gates.json", body)
                    result.update(status="completed_with_stated_scope", receipt=record, counts=computation["counts"])
                    del report, computation, body
                    progress("league_completed", league=league)
                except Exception as exc:
                    result.update(failure(exc, stage))
                    if witness is not None:
                        try:
                            result["partial_numeric_witness"] = witness.close(False)
                        except Exception:
                            # close() indexes actual partial bytes before raising.
                            # Do not drop that reference when trailer flush fails.
                            if witness.closed_ref is not None:
                                result["partial_numeric_witness"] = witness.closed_ref
                    out.write_json(league + "-failure.json", result, terminal=True)
                receipt["results"].append(result)
                report = computation = body = None
                gc.collect()
                if result.get("error_code") in ("numeric_witness_budget_exceeded", "output_budget_exceeded"):
                    # Emergency receipts already persist this result. Do not
                    # spend reserved terminal bytes on a redundant checkpoint.
                    break
                # Persist each completed result without replacing older receipts.
                out.write_json("checkpoint-%02d.json" % len(receipt["results"]),
                    {"completed_league_records": len(receipt["results"]), "results": [
                        {k: item.get(k) for k in ("league", "status", "receipt", "failed_stage", "error_code")}
                        for item in receipt["results"]]})
        receipt["source_implementation_unchanged"] = implementation_fingerprint() == before
        receipt["protocol_bytes_unchanged"] = (repo / "advanced-confirmation-v1.json").read_bytes() == design_raw and (repo / "advanced-models-v1.json").read_bytes() == protocol_raw
        require(receipt["source_implementation_unchanged"] and receipt["protocol_bytes_unchanged"], "audit_input_mutated")
        receipt["status"] = "completed_with_stated_scope" if len(receipt["results"]) == len(leagues) and all(x["status"] == "completed_with_stated_scope" for x in receipt["results"]) else "audit_incomplete"
    except Exception as exc:
        receipt.update(failure(exc, stage))
    except BaseException as exc:
        receipt.update(failure(exc, stage))
        raise
    finally:
        receipt["ended_at"] = datetime.now(timezone.utc).isoformat()
        receipt["elapsed_seconds"] = time.monotonic() - started
        completed = [x["league"] for x in receipt["results"] if x["status"] == "completed_with_stated_scope"]
        receipt["completed_leagues"] = completed
        receipt["not_audited_registered_leagues"] = [l for l in design["registered_leagues"] if l not in completed]
        receipt["full_family_size"] = len(design["registered_leagues"]) * len(confirmation._candidate_specs(protocol))
        receipt["audited_candidate_horizons"] = len(completed) * 20
        receipt["entire_R07_closed"] = False
        out.write_json("run-final.json", receipt, terminal=True)
        out.write_json("manifest.json", {"schema_version": "fixed-r07-output-manifest-v1", "files": out.files,
            "status": receipt["status"], "total_before_manifest_bytes": out.bytes}, terminal=True)
    print(json.dumps({"status": receipt["status"], "completed_leagues": receipt["completed_leagues"],
        "audited_candidate_horizons": receipt["audited_candidate_horizons"]}, ensure_ascii=False), flush=True)
    return 0 if receipt["status"] == "completed_with_stated_scope" else 1


if __name__ == "__main__":
    raise SystemExit(main())
