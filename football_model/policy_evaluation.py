"""Preregistered same-ticket policy comparison of actual immutable score bundles.

No policy output, grade, probability, result, or missing return is manufactured.
The comparison is descriptive; its bootstrap is not an automatic promotion gate.
"""
from __future__ import annotations
from copy import deepcopy

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import random

from .evidence import EvidenceError, _no_symlinks, _read_regular, _time

SCHEMA = "s2-policy-evaluation-1.0"
PROTOCOL_SCHEMA = "s2-policy-protocol-1.0"
BOOTSTRAP_SEED = 20261007
BOOTSTRAP_REPLICATES = 2000
IDENTITY = ("competition", "match_id", "home_id", "away_id", "kickoff")
METRIC_PATHS = {
    "one_x_two_brier": ("probability", "metrics", "one_x_two", "brier_score"),
    "one_x_two_log_loss": ("probability", "metrics", "one_x_two", "log_loss"),
    "five_state_brier": ("probability", "metrics", "five_state", "brier_score"),
    "five_state_log_loss": ("probability", "metrics", "five_state", "log_loss"),
}
RETURN_FIELDS = {"QUOTE_REF": "paper_reference_return", "QUOTE_OPEN": "open_simulated_return",
                 "QUOTE_ACTUAL": "actual_single_return"}


def _now():
    return datetime.now(timezone.utc)


def _iso(value):
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _bytes(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n").encode()


def _sha(blob):
    return hashlib.sha256(blob).hexdigest()


def _digest(value):
    return _sha(_bytes(value))


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def _hash(value):
    return isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def _finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _write_new(path, value):
    path = _no_symlinks(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("xb") as handle:
            handle.write(_bytes(value))
    except FileExistsError as exc:
        raise EvidenceError("policy_artifact_exists_no_overwrite") from exc


def _validate_spec(spec):
    if not isinstance(spec, dict) or spec.get("comparison") != "same_ticket_grading_and_action":
        raise EvidenceError("only_same_ticket_grading_and_action_supported")
    for key in ("experiment_id", "scope_description", "primary_objective", "allowed_degradation",
                "review_schedule", "stopping_rule"):
        if not _text(spec.get(key)):
            raise EvidenceError("missing_protocol_" + key)
    if spec.get("version_selection") != "first_actual_output_per_request":
        raise EvidenceError("first_actual_output_selection_required")
    if spec.get("quote_protocol") not in RETURN_FIELDS or spec.get("usage") not in {"single", "parlay_leg"}:
        raise EvidenceError("explicit_quote_protocol_and_usage_required")
    if spec.get("abstention_zero_exposure") not in (True, False) or not isinstance(spec.get("abstention_zero_exposure"), bool):
        raise EvidenceError("explicit_abstention_policy_required")
    policies = spec.get("policies")
    if not isinstance(policies, dict) or set(policies) != {"left", "right"}:
        raise EvidenceError("two_named_policies_required")
    for policy in policies.values():
        if not isinstance(policy, dict) or not _text(policy.get("policy_id")) or policy.get("view") not in {"formal", "shadow"}:
            raise EvidenceError("invalid_policy_identity_or_view")
        if policy["view"] == "shadow":
            if not _text(policy.get("policy_version")):
                raise EvidenceError("shadow_policy_version_required")
            grades = policy.get("included_grades")
            if not isinstance(grades, list) or not grades or len(grades) != len(set(grades)) or set(grades) - {"A", "B"}:
                raise EvidenceError("explicit_A_B_shadow_inclusion_required")
    if policies["left"] == policies["right"]:
        raise EvidenceError("distinct_policy_views_required")
    requests = spec.get("requests")
    if not isinstance(requests, list) or not requests:
        raise EvidenceError("nonempty_frozen_request_universe_required")
    ids = set()
    for request in requests:
        if not isinstance(request, dict) or not _text(request.get("request_id")) or request["request_id"] in ids:
            raise EvidenceError("unique_request_ids_required")
        ids.add(request["request_id"])
        if any(request.get(k) is None for k in IDENTITY):
            raise EvidenceError("complete_request_match_identity_required")
        if _time(request.get("analysis_cutoff")) >= _time(request["kickoff"]):
            raise EvidenceError("request_cutoff_must_precede_kickoff")
    return spec


def register_policy_protocol(spec, root):
    """Freeze a request universe and rules using the actual clock, never --as-of.

    A past cutoff is retained as research-only, not falsely preregistered.
    Actual common input/quote hashes are compared from verified score bundles.
    They need not be known before the future observation cutoff.
    """
    _validate_spec(spec)
    now = _now()
    cutoffs = [_time(r["analysis_cutoff"]) for r in spec["requests"]]
    record = {"schema_version": PROTOCOL_SCHEMA, "registered_at": _iso(now), "spec": spec,
              "score_revision_policy": "explicit_single_score_revision_for_selected_decision",
              "qualification": "prospective_preregistered" if now < min(cutoffs) else "research_only",
              "bootstrap": {"seed": BOOTSTRAP_SEED, "replicates": BOOTSTRAP_REPLICATES,
                            "cluster": "competition_and_match_id", "interval": "percentile_95"}}
    pid = _digest(record)
    root = _no_symlinks(root)
    path = root / (pid + ".json")
    _write_new(path, record)
    return {"protocol_id": pid, "path": str(path), "registered_at": record["registered_at"],
            "qualification": record["qualification"]}


def read_policy_protocol(root, protocol_id):
    if not _hash(protocol_id):
        raise EvidenceError("invalid_policy_protocol_id")
    raw = _read_regular(root, protocol_id + ".json")
    if _sha(raw) != protocol_id:
        raise EvidenceError("policy_protocol_hash_mismatch")
    record = json.loads(raw)
    if record.get("schema_version") != PROTOCOL_SCHEMA:
        raise EvidenceError("unsupported_policy_protocol")
    _validate_spec(record.get("spec"))
    return record


def _read_score(reference):
    from .decision_scoring import read_decision_score
    if not isinstance(reference, dict) or not _hash(reference.get("manifest_sha256")):
        raise EvidenceError("verified_score_manifest_required")
    return read_decision_score(reference.get("path"), reference["manifest_sha256"])


def _failure(reference, request, policy):
    if not isinstance(reference, dict) or not _hash(reference.get("sha256")):
        raise EvidenceError("failure_source_hash_required")
    path = _no_symlinks(reference.get("path"))
    raw = _read_regular(path.parent, path.name)
    if _sha(raw) != reference["sha256"]:
        raise EvidenceError("failure_source_hash_mismatch")
    record = json.loads(raw)
    if (record.get("request_id") != request["request_id"] or record.get("policy_id") != policy["policy_id"]
            or record.get("status") != "failed" or not _text(record.get("reason"))):
        raise EvidenceError("failure_source_binding_required")
    completed = _time(record.get("completed_at"))
    if completed > _now():
        raise EvidenceError("failure_timestamp_in_future")
    return {"kind": "failure", "time": completed, "digest": reference["sha256"], "record": record}


def _select(references, request, policy):
    """Choose the first *actual* output, including failures; retain every version."""
    if not isinstance(references, list):
        raise EvidenceError("output_reference_list_required")
    records, seen = [], set()
    for reference in references:
        if reference.get("kind") == "failure":
            item = _failure(reference, request, policy)
        else:
            score = _read_score(reference)
            if score.get("schema_version") != "s2-decision-score-1.0":
                raise EvidenceError("unsupported_actual_score_schema")
            binding = score.get("binding", {})
            if (binding.get("competition") != request["competition"]
                    or any(str(binding.get(k)) != str(request[k]) for k in ("match_id", "home_id", "away_id"))
                    or _time(binding.get("kickoff")) != _time(request["kickoff"])):
                raise EvidenceError("output_request_identity_mismatch")
            if _time(binding.get("analysis_cutoff")) != _time(request["analysis_cutoff"]):
                raise EvidenceError("output_request_cutoff_mismatch")
            completed = _time(score.get("decision_times", {}).get("report_generated_at"))
            if completed > _now():
                raise EvidenceError("decision_timestamp_in_future")
            item = {"kind": "score", "time": completed, "digest": reference["manifest_sha256"], "record": score}
        if item["digest"] not in seen:
            records.append(item)
            seen.add(item["digest"])
    # Score/result revisions are not new decisions. Preserve their identities,
    # but require callers to select a single result revision explicitly.
    groups = {}
    for item in records:
        identity = (item["record"].get("source") or {}).get("decision_manifest_sha256") if item["kind"] == "score" else None
        identity = ("decision:" + identity) if identity else ("artifact:" + item["digest"])
        group = groups.setdefault(identity, {**item, "scoring_versions": []})
        if group["time"] != item["time"]:
            raise EvidenceError("same_decision_conflicting_completion_times")
        if item["kind"] == "score":
            group["scoring_versions"].append({"source_sha256": item["digest"],
                "result_sha256": (item["record"].get("source") or {}).get("result_sha256"),
                "scored_at": item["record"].get("scored_at")})
    ordered = sorted(groups.values(), key=lambda x: (x["time"], x["digest"]))
    # Two different outputs at one clock precision have no observed order.
    ambiguous = "earliest_output_order_ambiguous" if len(ordered) > 1 and ordered[0]["time"] == ordered[1]["time"] else None
    if not ambiguous and ordered and len(ordered[0]["scoring_versions"]) > 1:
        ambiguous = "multiple_score_revisions_require_explicit_selection"
    return (None if ambiguous or not ordered else ordered[0]), ordered, ambiguous


def _action(score, policy, usage):
    if policy["view"] == "formal":
        if score.get("binding", {}).get("policy_id") != policy["policy_id"]:
            return "unknown", "formal_policy_identity_mismatch"
        value = (score.get("formal_fit") or {}).get(usage)
        return {"适合": ("candidate", None), "不建议": ("abstain", None),
                "条件适合": ("conditional", "conditions_require_new_decision"),
                "暂无法判断": ("unknown", "formal_unable_to_judge")}.get(value, ("unknown", "formal_fit_missing"))
    shadow = score.get("shadow_grading") or {}
    if shadow.get("policy_id") != policy["policy_id"] or shadow.get("policy_version") != policy["policy_version"]:
        return "unknown", "shadow_policy_identity_mismatch"
    actual = shadow.get(usage) or {}
    grade, action = actual.get("grade"), actual.get("action_status")
    if action == "awaiting_condition":
        return "conditional", "conditions_require_new_decision"
    if action == "not_recommended" and grade == "D":
        return "abstain", None
    if grade == "C" and action == "watch_only" and actual.get("c_reason") == "purpose_mismatch":
        return "abstain", None
    if grade in {"A", "B"} and action in {"candidate", "watch_only"}:
        return ("candidate", None) if action == "candidate" and grade in policy["included_grades"] else ("abstain", None)
    return "unknown", "shadow_missing_or_incomplete_grade"


def _get(value, keys):
    for key in keys:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value if _finite(value) else None


def _side(selected, versions, ambiguous, policy, spec, registered_at):
    result = {"status": "missing", "action": "unknown", "reason": "actual_output_missing",
              "selected_source_sha256": None, "versions": [{"kind": v["kind"], "source_sha256": v["digest"],
              "completed_at": _iso(v["time"]), "scoring_versions": v.get("scoring_versions", [])} for v in versions], "metrics": {k: None for k in METRIC_PATHS},
              "ticket_return": None, "strategy_return": None, "score": None, "prospective": False,
              "probability_diagnostics": None}
    if ambiguous:
        result.update(status="unpaired", reason=ambiguous)
        return result
    if selected is None:
        return result
    result["selected_source_sha256"] = selected["digest"]
    if selected["kind"] == "failure":
        result.update(status="failed", action="failed", reason=selected["record"]["reason"])
        return result
    score = selected["record"]
    action, reason = _action(score, policy, spec["usage"])
    result.update(status="available", action=action, reason=reason, score=score)
    result["prospective"] = (score.get("qualification") == "prospective_decision_candidate"
                             and registered_at < selected["time"]
                             and _time(score["binding"]["analysis_cutoff"]) >= registered_at
                             and selected["time"] < _time(score["binding"]["kickoff"]))
    if score.get("quote_protocol") != spec["quote_protocol"] or score.get("usage") != spec["usage"]:
        result.update(status="unpaired", reason="quote_protocol_or_usage_mismatch")
        return result
    from .decision_scoring import current_score_admission_reasons
    admission_reasons = current_score_admission_reasons(score)
    if admission_reasons:
        value = (score.get("outcome") or {}).get(RETURN_FIELDS[spec["quote_protocol"]])
        result.update(prospective=False, current_score_admission=False,
                      admission_reasons=admission_reasons,
                      historical_ticket_return=value if _finite(value) else None,
                      historical_probability_metrics=deepcopy((score.get("probability") or {}).get("metrics")))
        return result
    result["metrics"] = {name: _get(score, path) for name, path in METRIC_PATHS.items()}
    source_metrics = (score.get("probability") or {}).get("metrics") or {}
    result["probability_diagnostics"] = {market: {key: (source_metrics.get(market) or {}).get(key)
        for key in ("mathematical_log_loss_status", "mathematical_log_loss", "observed_zero_probability_outcomes",
                    "floor_affected_outcomes", "log_loss_probability_floor")}
        for market in ("one_x_two", "five_state")}
    value = (score.get("outcome") or {}).get(RETURN_FIELDS[spec["quote_protocol"]])
    result["ticket_return"] = value if _finite(value) else None
    if action == "candidate":
        result["strategy_return"] = result["ticket_return"]
    # Zero is explicit zero exposure, only after the ticket itself was scored.
    # ACTUAL never synthesizes a hypothetical trade or a nonexistent zero trade.
    elif action == "abstain" and spec["abstention_zero_exposure"] and spec["quote_protocol"] != "QUOTE_ACTUAL" and result["ticket_return"] is not None:
        result["strategy_return"] = 0.0
    return result


def _pair_reasons(left, right):
    reasons = {"process": [], "probability": [], "returns": []}
    if left["status"] != "available" or right["status"] != "available":
        reasons["process"].append("actual_pair_unavailable")
        return reasons
    a, b = left["score"], right["score"]
    for side in (left, right):
        reasons["probability"].extend(side.get("admission_reasons", []))
        reasons["returns"].extend(side.get("admission_reasons", []))
    for field in ("contract", "quote_protocol", "usage"):
        if a.get(field) is None or b.get(field) is None:
            reasons["process"].append(field + "_missing")
        elif a[field] != b[field]:
            reasons["process"].append(field + "_mismatch")
    for field in ("price_decimal", "cost_per_unit"):
        if a.get(field) is None or b.get(field) is None:
            reasons["returns"].append(field + "_missing")
        elif a[field] != b[field]:
            reasons["returns"].append(field + "_mismatch")
    for field in ("quote_row_id", "quote_offer_id"):
        av, bv = a["binding"].get(field), b["binding"].get(field)
        if av != bv:
            reasons["process"].append(field + "_mismatch")
    pools = (a.get("source_pool") or {}, b.get("source_pool") or {})
    for field in ("input_pool_sha256", "quote_pool_sha256"):
        av, bv = pools[0].get(field), pools[1].get(field)
        if not av or not bv:
            reasons["process"].append(field + "_missing")
        elif av != bv:
            reasons["process"].append(field + "_mismatch")
    qualitative = all(s["binding"].get("forecast_id") is None
        and (s.get("probability") or {}).get("reason") == "adopted_forecast_unavailable"
        and not (s.get("source_pool") or {}).get("model_sha256s") for s in (a,b))
    if qualitative:
        reasons["probability"].append("both_explicitly_qualitative_no_model")
    else:
        models = [p.get("model_sha256s") for p in pools]
        if not all(models):
            reasons["process"].append("model_sha256s_missing")
        elif sorted(models[0]) != sorted(models[1]):
            reasons["process"].append("model_sha256s_mismatch")
    # Same score pool does not imply the same adopted forecast version.
    if not qualitative and (not a["binding"].get("forecast_id") or a["binding"].get("forecast_id") != b["binding"].get("forecast_id")):
        reasons["process"].append("adopted_forecast_missing_or_mismatch")
    if (a.get("source") or {}).get("result_sha256") != (b.get("source") or {}).get("result_sha256"):
        reasons["probability"].append("result_revision_mismatch")
        reasons["returns"].append("result_revision_mismatch")
    if a.get("quote_protocol") == "QUOTE_OPEN":
        source_ids = [(s.get("source") or {}).get("quote_evidence_sha256") for s in (a,b)]
        if not all(source_ids) or source_ids[0] != source_ids[1]:
            reasons["returns"].append("different_or_missing_open_quote_opportunity")
        for s in (a,b):
            times = s.get("open_quote_times") or {}
            try:
                if not _time(times.get("asserted_at")) <= _time(times.get("observed_at")) <= _time(s["binding"]["analysis_cutoff"]):
                    reasons["returns"].append("open_quote_outside_common_cutoff")
            except (ValueError, TypeError):
                reasons["returns"].append("open_quote_times_missing")
    if a.get("quote_protocol") == "QUOTE_ACTUAL":
        sources = [(s.get("source") or {}).get("event_manifest_sha256s") for s in (a,b)]
        if not all(sources) or sorted(sources[0]) != sorted(sources[1]):
            reasons["returns"].append("different_or_missing_actual_execution")
    return reasons


def cluster_interval(rows, *, seed=BOOTSTRAP_SEED, replicates=BOOTSTRAP_REPLICATES):
    """Equal-match mean of paired right-minus-left deltas; no IID ticket fiction."""
    grouped = defaultdict(list)
    for row in rows:
        if _finite(row.get("left")) and _finite(row.get("right")):
            grouped[(row["competition"], str(row["match_id"]))].append(row["right"] - row["left"])
    means = [sum(values) / len(values) for _, values in sorted(grouped.items())]
    result = {"paired_rows": sum(len(v) for v in grouped.values()), "unique_matches": len(means),
              "estimand": "equal_match_mean_of_within_match_mean_right_minus_left", "mean_difference": None,
              "interval_95": None, "seed": seed, "replicates": replicates,
              "status": "not_available", "quality_acceptance": False,
              "limitation": "Descriptive match-cluster bootstrap; few matches give unstable intervals; no automatic significance or profitability claim."}
    if not means:
        return result
    result.update(mean_difference=sum(means) / len(means), status="insufficient_clusters" if len(means) < 2 else "descriptive_only")
    if len(means) < 2:
        return result
    rng = random.Random(seed)
    draws = sorted(sum(rng.choice(means) for _ in means) / len(means) for _ in range(replicates))
    def quantile(q):
        index = (len(draws) - 1) * q
        low = int(index)
        return draws[low] + (draws[min(low + 1, len(draws) - 1)] - draws[low]) * (index - low)
    result["interval_95"] = [quantile(.025), quantile(.975)]
    return result


def evaluate_policy_comparison(protocol_root, protocol_id, inputs, *, output_path=None):
    """Read all actual output references; absent requests remain in the denominator.

    ``inputs`` contains request_id and left/right lists of immutable score bundle
    references {path,manifest_sha256}; actual failures use kind=failure,path,sha256.
    A reference integrity error rejects the run rather than silently dropping it.
    """
    protocol = read_policy_protocol(protocol_root, protocol_id)
    spec = protocol["spec"]
    if not isinstance(inputs, list):
        raise EvidenceError("comparison_inputs_must_be_list")
    requested = {r["request_id"]: r for r in spec["requests"]}
    supplied = {}
    for item in inputs:
        if not isinstance(item, dict) or item.get("request_id") not in requested or item["request_id"] in supplied:
            raise EvidenceError("unknown_or_duplicate_request_input")
        supplied[item["request_id"]] = item
    registered = _time(protocol["registered_at"])
    rows, metric_rows, prospective_metrics, research_metrics = [], defaultdict(list), defaultdict(list), defaultdict(list)
    for rid, request in requested.items():
        entry = supplied.get(rid, {})
        sides = {}
        for side in ("left", "right"):
            selected, versions, ambiguous = _select(entry.get(side, []), request, spec["policies"][side])
            sides[side] = _side(selected, versions, ambiguous, spec["policies"][side], spec, registered)
        layer_reasons = _pair_reasons(sides["left"], sides["right"])
        reasons = list(layer_reasons["process"])
        left, right = sides["left"]["action"], sides["right"]["action"]
        process_comparable = not reasons
        comparable = process_comparable and left in {"candidate", "abstain"} and right in {"candidate", "abstain"}
        group = ("common" if left == right == "candidate" else "restored_coverage" if left == "abstain" and right == "candidate"
                 else "lost_coverage" if left == "candidate" and right == "abstain" else "both_abstain") if comparable else "unpaired"
        if not comparable and not reasons:
            reasons.append("action_unknown_failed_or_condition_unresolved")
        row = {"request_id": rid, **{k: request[k] for k in IDENTITY}, "analysis_cutoff": request["analysis_cutoff"],
               "group": group, "unpaired_reasons": reasons,
               "comparability": {"process": process_comparable, "probability": process_comparable and not layer_reasons["probability"],
                    "returns": process_comparable and not layer_reasons["returns"], "reasons": layer_reasons},
               "prospective_pair": process_comparable and protocol["qualification"] == "prospective_preregistered" and all(v["prospective"] for v in sides.values())}
        for side, value in sides.items():
            row[side] = {k: v for k, v in value.items() if k != "score"}
        rows.append(row)
        if row["comparability"]["probability"]:
            for name in METRIC_PATHS:
                value = {**request, "left": sides["left"]["metrics"][name], "right": sides["right"]["metrics"][name]}
                metric_rows[name].append(value)
                (prospective_metrics if row["prospective_pair"] else research_metrics)[name].append(value)
        if row["comparability"]["returns"]:
            for name in ("ticket_return", "strategy_return"):
                value = {**request, "left": sides["left"][name], "right": sides["right"][name]}
                metric_rows[name].append(value)
                (prospective_metrics if row["prospective_pair"] else research_metrics)[name].append(value)
    report = {"schema_version": SCHEMA, "protocol_id": protocol_id, "generated_at": _iso(_now()),
              "comparison": spec["comparison"], "qualification": protocol["qualification"], "qualification_scope": "protocol_registration_only_see_each_pair",
              "quote_protocol": spec["quote_protocol"],
              "usage": spec["usage"], "request_count": len(rows),
              "unique_matches": len({(r["competition"], str(r["match_id"])) for r in rows}),
              "actual_output_versions": {s: sum(len(r[s]["versions"]) for r in rows) for s in ("left", "right")},
              "score_artifact_versions": {s: sum(len(v["scoring_versions"]) for r in rows for v in r[s]["versions"]) for s in ("left", "right")},
              "coverage": dict(Counter(r["group"] for r in rows)), "prospective_pairs": sum(r["prospective_pair"] for r in rows),
              "rows": rows, "paired_metrics_all_descriptive_only": {k: cluster_interval(metric_rows[k]) for k in (*METRIC_PATHS, "ticket_return", "strategy_return")},
              "prospective_paired_metrics": {k: cluster_interval(prospective_metrics[k]) for k in (*METRIC_PATHS, "ticket_return", "strategy_return")},
              "research_paired_metrics": {k: cluster_interval(research_metrics[k]) for k in (*METRIC_PATHS, "ticket_return", "strategy_return")},
              "actual_bets_inferred": False, "whole_parlay_evaluated": False, "quality_improvement_verified": False,
              "log_loss_comparison_definition": "Existing floored display values; mathematical infinity and affected counts remain in each side's probability_diagnostics.",
              "limitations": ["Only actual supplied output versions are observable; omitted versions cannot be detected by this tool.",
                  "Same-ticket grading/actions only; no independent ticket selection is tested.",
                  "Missing outputs, prices, costs, results, execution and unknown conditions are not zero returns.",
                  "Different protocols, result revisions or models are not pooled; used historical cases remain development data.",
                  "Bootstrap intervals are descriptive; prospective and research rows remain explicitly distinguishable."]}
    if output_path is not None:
        _write_new(output_path, report)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    register = commands.add_parser("register")
    register.add_argument("--spec", type=Path, required=True)
    register.add_argument("--root", type=Path, required=True)
    compare = commands.add_parser("compare")
    compare.add_argument("--root", type=Path, required=True)
    compare.add_argument("--protocol-id", required=True)
    compare.add_argument("--inputs", type=Path, required=True)
    compare.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "register":
            result = register_policy_protocol(json.loads(args.spec.read_text()), args.root)
        else:
            report = evaluate_policy_comparison(args.root, args.protocol_id, json.loads(args.inputs.read_text()), output_path=args.output)
            result = {"output": str(args.output), "request_count": report["request_count"], "coverage": report["coverage"]}
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (ValueError, KeyError, TypeError, OSError) as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
