"""Read-only fixed-report audit. No collection, fitting, promotion or state save.

Loads complete compact reports and latest training bundles, not thin summaries.
Re-evaluates saved predictions and matched-pool scores with frozen arithmetic.
This is NOT an independent raw-to-feature rebuild or an independent R refit.
"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import gc
import hashlib
import json
import math
from pathlib import Path
import re
import sys
import os
import tempfile
import time
import traceback


class AuditError(ValueError):
    pass


def require(value, code):
    if not value:
        raise AuditError(code)


def equal(actual, expected, tolerance=1e-12):
    """Strict shapes/types; finite absolute tolerance only for numeric leaves."""
    if isinstance(expected, bool) or expected is None or isinstance(expected, str):
        require(type(actual) is type(expected) and actual == expected, "value_mismatch")
    elif isinstance(expected, (int, float)):
        require(type(actual) in (int, float) and math.isfinite(actual) and math.isfinite(expected)
                and abs(actual - expected) <= tolerance, "numeric_mismatch")
    elif isinstance(expected, dict):
        require(isinstance(actual, dict) and set(actual) == set(expected), "field_mismatch")
        for k, v in expected.items():
            equal(actual[k], v, tolerance)
    elif isinstance(expected, list):
        require(isinstance(actual, list) and len(actual) == len(expected), "length_mismatch")
        for a, b in zip(actual, expected):
            equal(a, b, tolerance)
    else:
        raise AuditError("unsupported_value")


def check_selection(fit, cutoff, protocol):
    model, selection = fit["model"], fit["selection"]
    require(model["training_cutoff"] == cutoff, "fit_cutoff_mismatch")
    require(time_value(model["last_result_available_at"]) <= time_value(cutoff), "future_training_target")
    if selection["status"] == "past_only_inner_time_validation":
        require(time_value(selection["inner_cutoff"]) <= time_value(cutoff), "future_inner_cutoff")
        trials = [t for t in selection["trials"] if t["status"] == "scored"]
        require(bool(trials) and all(math.isfinite(t["score"]) for t in trials), "invalid_alpha_trials")
        chosen = min(trials, key=lambda t: (t["score"], -t["alpha"]))["alpha"]
        require(chosen == selection["selected_alpha"] == model["alpha"], "past_alpha_selection_mismatch")
    else:
        require(selection["status"] == "cold_start_fixed_strongest_penalty"
                and model["alpha"] == protocol["correction_alpha_grid"][-1], "unknown_alpha_selection")


def time_value(value):
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    require(result.tzinfo is not None, "naive_timestamp")
    return result


def expected_training_ids(replay, feature_rows, names, cutoff):
    ids = []
    for f in replay["forecasts"]:
        values = feature_rows.get(f["match_id"], {})
        complete = all(isinstance(values.get(side), dict)
            and all(type(values[side].get(n)) in (int, float) and math.isfinite(values[side][n]) for n in names)
            for side in ("home", "away"))
        if complete and time_value(f["target_available_at"]) <= time_value(cutoff):
            ids.append(f["match_id"])
    require(len(ids) == len(set(ids)), "duplicate_training_target")
    return sorted(ids)


def recompute_increments(report):
    out = {}
    for added, parent in report["protocol"]["family_increment_comparisons"] + [["G1", "G0"]]:
        key = added + "_minus_" + parent
        if added == "G1":
            pairs = [(r["scores"][added], r["scores"][parent]) for r in report["scored_matches"]
                     if r["horizon"] == "EARLY" and added in r["scores"] and parent in r["scores"]]
        else:
            pairs = [(r["matched_pool_ablation_scores"][key]["added"],
                      r["matched_pool_ablation_scores"][key]["parent_on_added_training_pool"])
                     for r in report["scored_matches"] if key in r["matched_pool_ablation_scores"]]
        delta = math.fsum(a["scoreline_nll"] - b["scoreline_nll"] for a, b in pairs) / len(pairs) if pairs else None
        expected = report["family_increments"][key]
        equal(len(pairs), expected["n"], 0)
        equal(delta, expected["mean_paired_scoreline_nll_difference"])
        out[key] = {"n": len(pairs), "delta_added_minus_matched_parent": delta,
                    "scope": "EARLY_only" if added == "G1" else "same_training_and_target_pool"}
    return out


def denominator(rows, candidate):
    both = [r for r in rows if candidate in r["scores"] and "M0" in r["scores"]]
    only = [r for r in rows if candidate in r["scores"] and "M0" not in r["scores"]]
    fallback = [r for r in rows if candidate not in r["scores"] and "M0" in r["scores"]]
    failed = [r for r in rows if candidate not in r["scores"] and "M0" not in r["scores"]]
    require(len(rows) == len(both) + len(only) + len(fallback) + len(failed), "denominator_mismatch")
    for r in rows:
        expected = r["scores"].get(candidate, r["scores"].get("M0"))
        if expected is None:
            require(candidate + "_policy" not in r["scores"], "invented_policy_score")
        else:
            equal(r["scores"].get(candidate + "_policy"), expected)
    native_delta = [r["scores"][candidate]["scoreline_nll"] - r["scores"]["M0"]["scoreline_nll"] for r in both]
    policy_delta = native_delta + [0.0] * len(fallback)
    return {"requested": len(rows), "native_paired": len(both), "native_only": len(only),
            "fallback": len(fallback), "failed_unforecast": len(failed),
            "native_paired_nll_delta": math.fsum(native_delta) / len(native_delta) if native_delta else None,
            "policy_paired_nll_delta": math.fsum(policy_delta) / len(policy_delta) if policy_delta else None}


def audit_report(runtime, summary, expected_impl, progress=None):
    from football_model.advanced_storage import load_artifact
    from football_model.advanced_research import candidate_feature_names, _side_values, _validate_card_identity, verify_goalmodel_probes
    from football_model.feature_correction import predict_correction
    from football_model.feature_research import score_forecast, digest
    from football_model.history_archive import read_bytes
    progress = progress or (lambda stage, **counts: None)
    progress("expand_complete_report")
    report = load_artifact(runtime, summary["full_report_ref"])
    progress("report_expanded")
    require(report["implementation_fingerprint"]["sha256"] == expected_impl, "implementation_mismatch")
    require(len(report["implementation_fingerprint"]["files"]) == 50, "implementation_file_count")
    require(report["league"] == summary["league"] and report["protocol_hash"] == digest(report["protocol"]), "report_identity_mismatch")
    for k in ("source_commit", "protocol_hash", "implementation_fingerprint", "family_increments"):
        equal(report[k], summary[k], 0)
    source_count = 0
    progress("hash_direct_sources", total=len(report["source_refs"]))
    for ref in report["source_refs"]:
        relative = ref.get("runtime_relative_path", ref["path"])
        path = (runtime / relative).resolve()
        require(path.is_relative_to(runtime.resolve()), "source_path_escape")
        require(hashlib.sha256(read_bytes(path, runtime=runtime)).hexdigest() == ref["sha256"], "source_hash_mismatch")
        source_count += 1
        if source_count % 128 == 0:
            progress("hash_direct_sources", completed=source_count, total=len(report["source_refs"]))
    progress("index_replay_and_features", source_refs_hashed=source_count)
    fixtures, baseline, features, experiments = {}, {}, {}, {}
    for h, replay in report["replays"].items():
        features[h] = {r["match_id"]: r for r in report["feature_datasets"][h]["rows"]}
        for f in replay["forecasts"] + replay["failures"]:
            key = (h, f["match_id"])
            require(key not in fixtures, "duplicate_replay_target")
            fixtures[key] = f
            if f.get("baseline_prediction"):
                baseline[key] = f["baseline_prediction"]
    training_pairs = 0
    progress("check_matched_training_and_past_selection")
    for e in report["experiments"]:
        require((e["horizon"], e["block"]) not in experiments, "duplicate_experiment")
        experiments[(e["horizon"], e["block"])] = e
        for fit in e["fits"].values():
            check_selection(fit, e["cutoff"], report["protocol"])
            require(fit["training_match_ids"] == fit["model"]["training_match_ids"], "training_id_mismatch")
        for added, parent in report["protocol"]["family_increment_comparisons"]:
            key = added + "_minus_" + parent
            if key not in e.get("matched_pool_ablation_fits", {}):
                continue
            fit = e["matched_pool_ablation_fits"][key]
            check_selection(fit, e["cutoff"], report["protocol"])
            # A matched parent can fit even when the added fit failed: preserve it,
            # but it cannot contribute a paired score or count as a verified pair.
            if added not in e["fits"]:
                continue
            ids = e["fits"][added]["model"]["training_match_ids"]
            expected_ids = expected_training_ids(report["replays"][e["horizon"]], features[e["horizon"]],
                candidate_feature_names(report["protocol"], added), e["cutoff"])
            require(ids == expected_ids, "past_admitted_training_pool_mismatch")
            require(fit["same_training_ids_as_added"] is True and fit["model"]["training_match_ids"] == ids
                    and fit["training_match_ids"] == ids, "matched_training_pool_mismatch")
            require(time_value(fit["model"]["last_result_available_at"]) <= time_value(e["cutoff"]), "matched_future_target")
            training_pairs += 1
        pools = e.get("goalmodel_training_pools", {})
        if "G0" in pools and "G1" in pools:
            equal(pools["G0"], pools["G1"], 0)
    scores, parent_scores, seen = 0, 0, set()
    progress("recompute_prediction_and_matched_parent_scores")
    for row in report["scored_matches"]:
        key = (row["horizon"], row["match_id"])
        require(key not in seen and key in fixtures, "duplicate_or_unknown_scored_target")
        seen.add(key)
        f = fixtures[key]
        require(row["cutoff_at"] == f["cutoff_at"] and time_value(row["cutoff_at"]) < time_value(row["kickoff"]), "forecast_cutoff_mismatch")
        if key in baseline:
            equal(score_forecast(baseline[key], f, extended_diagnostics=True), row["scores"]["M0"])
            scores += 1
        for c, prediction in row["predictions"].items():
            equal(score_forecast(prediction, f, extended_diagnostics=True), row["scores"][c])
            scores += 1
        e = experiments[(row["horizon"], f["block"])]
        for added, parent in report["protocol"]["family_increment_comparisons"]:
            k = added + "_minus_" + parent
            if k not in row["matched_pool_ablation_scores"]:
                continue
            saved = row["matched_pool_ablation_scores"][k]
            equal(saved["added"], row["scores"][added])
            fit = e["matched_pool_ablation_fits"][k]
            values = _side_values(features[row["horizon"]][row["match_id"]], candidate_feature_names(report["protocol"], parent))
            predicted = predict_correction(fit["model"], baseline[key], values["home"], values["away"])
            equal(score_forecast(predicted, f, extended_diagnostics=True), saved["parent_on_added_training_pool"])
            parent_scores += 1
        if len(seen) % 200 == 0:
            progress("recompute_prediction_and_matched_parent_scores", horizons=len(seen), scores=scores, parent_scores=parent_scores)
    require(seen == set(fixtures) and len(seen) == report["requested_horizon_opportunities"], "request_universe_mismatch")
    cards, r_probes = 0, 0
    progress("expand_latest_training_bundles")
    for c, ref in report["model_cards"].items():
        card = load_artifact(runtime, ref)
        _validate_card_identity(card, c, report["league"], report["protocol"])
        require(card["implementation_fingerprint"] == report["implementation_fingerprint"], "card_implementation_mismatch")
        require(card["source_commit"] == report["source_commit"], "card_source_mismatch")
        if "goalmodel_training_ref" in card:
            bundle = load_artifact(runtime, card["goalmodel_training_ref"])
            real = bundle["real_R_result"]
            equal(card["goalmodel_fit"], real["fit"], 0)
            equal(card["training_match_ids"], real["training_match_ids"], 0)
            r_probes += verify_goalmodel_probes(card, real, {int(k):v for k,v in bundle["probe_features"].items()})["probe_count"]
        else:
            bundle = load_artifact(runtime, card["correction_training_ref"])
            equal(card["correction_model"], bundle["correction_model"], 0)
        cards += 1
        progress("expand_latest_training_bundles", completed=cards, total=len(report["model_cards"]))
    progress("recompute_full_denominators_and_increments")
    return {"league": report["league"], "source_commit": report["source_commit"],
            "report_ref": summary["full_report_ref"], "requested_matches": report["requested_matches"],
            "requested_horizons": len(seen), "source_refs_hashed": source_count,
            "saved_prediction_scores_recomputed": scores, "matched_parent_scores_recomputed": parent_scores,
            "matched_training_pairs_checked": training_pairs, "latest_cards_and_full_bundles": cards,
            "latest_real_R_engineering_probes_recomputed": r_probes,
            "family_increments": recompute_increments(report),
            "denominators": {h: {c: denominator([r for r in report["scored_matches"] if r["horizon"] == h], c)
                for c in [c for c,s in report["protocol"]["candidates"].items() if s["horizon"] == h]
                    + (report["protocol"].get("goalmodel_candidates", []) if h in report["protocol"].get("goalmodel_horizons", ["EARLY"]) else [])}
                for h in report["replays"]}, "status": "completed_with_stated_scope"}


def failure_detail(exc):
    # No free exception text, local variables, raw fixture values or inputs.
    frames = traceback.extract_tb(exc.__traceback__)
    return {"exception_class": type(exc).__name__,
            "error_code": str(exc) if isinstance(exc, AuditError) else "unclassified_audit_error",
            "frames": [{"file":Path(f.filename).name,"function":f.name,"line":f.lineno}for f in frames[-6:]]}


def checkpoint_output(path, out):
    raw = json.dumps(out, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    require(len(raw.encode()) <= 512 * 1024, "bounded_output_limit")
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
            prefix="audit-progress-", suffix=".tmp", delete=False) as handle:
        temporary = Path(handle.name)
        try:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def validate_output(path, repo):
    path = path.resolve()
    require(path.suffix == ".json" and not path.exists(), "output_must_be_new_json")
    require(not any(path.is_relative_to(repo.resolve() / p) for p in
        ("runtime", "fixed-research-audit-runtime", "football_model", "scripts", "tests", ".github", ".git")), "protected_output_path")
    return path


def main(argv=None):
    from fixed_fixture_validation_audit import restore_fixed, sha40
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--repo", type=Path, required=True)
    p.add_argument("--state-commit", type=sha40, required=True)
    p.add_argument("--implementation", required=True)
    p.add_argument("--leagues", required=True)
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args(argv)
    require(bool(re.fullmatch(r"[a-f0-9]{64}", args.implementation)), "invalid_implementation")
    leagues = args.leagues.split(",")
    require(len(leagues) == len(set(leagues)) and all(re.fullmatch(r"[a-z][a-z0-9_]*", l) for l in leagues), "invalid_leagues")
    repo = args.repo.resolve()
    args.output = validate_output(args.output, repo)
    sys.path.insert(0, str(repo))
    import cloud_state as cloud
    from football_model.history_archive import read_bytes
    out = {"schema_version": "fixed-research-report-audit-v1", "started_at": datetime.now(timezone.utc).isoformat(),
           "state_commit": args.state_commit, "implementation": args.implementation,
           "network_football_requests": 0, "fits": 0, "remote_state_writes": 0, "results": [], "status":"audit_in_progress",
           "limitations": ["Frozen research arithmetic, not independent R refitting.",
               "No independent raw API to feature reconstruction.", "Saved alpha trial selection checked, inner fits not rerun.",
               "Historical feature assumptions are not observed live PIT.", "Not all19 native current-input resume-only or final successor short acceptance."]}
    runtime = repo / "fixed-research-audit-runtime"
    started = time.monotonic()
    def progress(stage, **counts):
        out["active_stage"] = {"stage":stage,"at":datetime.now(timezone.utc).isoformat(),
            "elapsed_seconds":round(time.monotonic()-started,3), **counts}
        checkpoint_output(args.output, out)
        print(json.dumps({"audit_progress":out["active_stage"],"league":out.get("active_league")}), flush=True)
    try:
        progress("restore_exact_fixed_state")
        out["auditor_source_commit"] = cloud._git(repo, "rev-parse", "HEAD").stdout.strip()
        out["restore_preflight"] = restore_fixed(repo, runtime, args.state_commit, cloud)
        for league in leagues:
            out["active_league"] = league
            try:
                progress("read_fixed_summary")
                # Current advanced protocol lives in a hash namespace. Resolve
                # only the fixed snapshot's existing summary, never a live branch.
                paths = list((runtime / "advanced_models" / league).glob("*/reports/latest.json"))
                require(len(paths) == 1, "report_namespace_not_unique")
                raw = read_bytes(paths[0], runtime=runtime)
                summary = json.loads(raw)
                result = audit_report(runtime, summary, args.implementation, progress=progress)
                result["summary_sha256"] = hashlib.sha256(raw).hexdigest()
                out["results"].append(result)
                progress("league_completed", completed_leagues=len(out["results"]))
            except Exception as exc:
                out["results"].append({"league": league, "status": "audit_incomplete", **failure_detail(exc),
                    "failed_stage":out["active_stage"]["stage"]})
                progress("league_audit_incomplete", completed_leagues=len(out["results"]))
            gc.collect()
        out["status"] = "completed_with_stated_scope" if all(r["status"] == "completed_with_stated_scope" for r in out["results"]) else "audit_incomplete"
    except Exception as exc:
        out.update(status="audit_incomplete", **failure_detail(exc))
    except (KeyboardInterrupt, SystemExit):
        out["status"] = "audit_interrupted"
        raise
    finally:
        out["ended_at"] = datetime.now(timezone.utc).isoformat()
        checkpoint_output(args.output, out)
    print(json.dumps(out, ensure_ascii=False, allow_nan=False))
    return 0 if out["status"] == "completed_with_stated_scope" else 1


if __name__ == "__main__":
    raise SystemExit(main())
