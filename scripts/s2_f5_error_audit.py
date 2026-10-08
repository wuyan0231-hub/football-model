"""Descriptive F5 preparation from immutable F4 historical predictions only.

No fit, network, future outcome read, forecast adjustment, or production change.
All source files are hashed before and after; F4 summary metrics are reproduced.
"""
from __future__ import annotations

import argparse
import hashlib
import re
import json
import math
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import NormalDist

import numpy as np
import scipy
from scipy.special import gammaln
from scipy.stats import skellam, poisson

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from football_model.model import _distribution_from_rates
from football_model.quality import summarize_distribution
from football_model.quality_half_life import make_protocol
NAMES = ("half_life_180", "half_life_90", "half_life_365", "league_mean", "selected_policy")
OUTCOMES = ("home_win", "draw", "away_win")
FLOOR = 1e-15
SOURCE_METRICS = ("one_x_two_log_loss", "one_x_two_brier", "score_log_loss", "total_log_loss", "margin_log_loss", "total_absolute_error", "margin_absolute_error")
COMPETITIONS = frozenset(("epl", "laliga", "bundesliga", "serie_a", "ligue_1", "bundesliga_2",
    "ligue_2", "allsvenskan", "eredivisie", "primeira", "brazil_a", "j1", "k1", "mls",
    "unl", "liga_mx", "segunda", "argentina_a"))
SETTLEMENTS = frozenset(("win", "half_win", "push", "half_loss", "loss"))


def require(condition, message):
    if not condition:
        raise ValueError(message)


def finite(value, label, minimum=None, maximum=None):
    require(type(value) in (int, float) and math.isfinite(value), "invalid_number:"+label)
    require(minimum is None or value >= minimum, "number_below_minimum:"+label)
    require(maximum is None or value <= maximum, "number_above_maximum:"+label)
    return value


def integer(value, label, minimum=0):
    require(type(value) is int and value >= minimum, "invalid_integer:"+label)
    return value


def timestamp(value):
    require(isinstance(value, str), "timestamp_must_be_text")
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    require(result.tzinfo is not None and result.utcoffset() == timedelta(0), "timestamp_must_be_utc")
    return result


def digest_text(value):
    require(isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value), "invalid_sha256")
    return value


def unique_competitions(value):
    require(isinstance(value, list) and all(isinstance(x, str) for x in value), "invalid_competitions")
    require(len(value) == len(set(value)) and set(value) == COMPETITIONS, "requires_exact_18_competitions")


def strict_json(path):
    require(path.is_file() and not path.is_symlink(), "input_must_be_regular_file:"+str(path))
    raw = path.read_bytes()

    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, "duplicate_json_key:"+key)
            result[key] = value
        return result

    def invalid_constant(value):
        raise ValueError("nonfinite_json_number:"+value)

    value = json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid_constant)
    # JSON exponents such as 1e999 overflow without invoking parse_constant.
    def walk(item):
        if isinstance(item, dict):
            for child in item.values():
                walk(child)
        elif isinstance(item, list):
            for child in item:
                walk(child)
        elif type(item) is float:
            require(math.isfinite(item), "nonfinite_json_number")
    walk(value)
    return value, hashlib.sha256(raw).hexdigest()


def validate_paths(input_dir, plan_path, output_dir):
    input_dir, plan_path, output_dir = map(Path, (input_dir, plan_path, output_dir))
    require(input_dir.is_dir() and not input_dir.is_symlink(), "input_directory_missing_or_symlink")
    require(plan_path.is_file() and not plan_path.is_symlink(), "plan_missing_or_symlink")
    require(not output_dir.exists() and not output_dir.is_symlink(), "output_already_exists")
    source, plan, output = input_dir.resolve(), plan_path.resolve(), output_dir.resolve()
    require(output.parent.is_dir(), "output_parent_must_exist")
    require(not output.is_relative_to(source), "output_must_be_outside_input_directory")
    return source, plan, output


def validate_forecast(forecast):
    require(isinstance(forecast, dict), "forecast_must_be_object")
    require(set(forecast["expected_goals"]) == {"home", "away"}, "expected_goals_keys")
    for side in ("home", "away"):
        require(finite(forecast["expected_goals"][side], "expected_goals") > 0, "nonpositive_expected_goals")
    require(set(forecast["probabilities"]) == set(OUTCOMES), "probability_keys")
    require(set(forecast["events"]) == {"over_2_5", "home_win_by_2_plus"}, "event_keys")
    require(set(forecast["settlement_probabilities"]) == {"over_2_25", "over_2_75"}, "settlement_market_keys")
    for group in (forecast["probabilities"], *forecast["settlement_probabilities"].values()):
        for probability in group.values():
            finite(probability, "probability", 0, 1)
        require(math.isclose(sum(group.values()), 1., abs_tol=1e-12, rel_tol=0), "probabilities_do_not_sum_to_one")
    for group in forecast["settlement_probabilities"].values():
        require(set(group) == SETTLEMENTS, "settlement_class_keys")
    for probability in forecast["events"].values():
        finite(probability, "event_probability", 0, 1)
    shape = forecast["matrix_shape"]
    require(isinstance(shape, list) and len(shape) == 2, "invalid_matrix_shape")
    for size in shape:
        require(1 < integer(size, "matrix_shape") <= 401, "invalid_matrix_shape")
    finite(forecast["tail_mass_before_normalization"], "tail_mass", 0, 1)


def validate_league(data, protocol, run, global_ids):
    require(isinstance(data, dict) and isinstance(data["predictions"], list)
            and isinstance(data["windows"], list), "invalid_result_structure")
    start, end = timestamp(protocol["start"]), timestamp(protocol["end"])
    inner_start = start - timedelta(days=protocol["inner_days"])
    # Recreate the original calendar only; this neither trains nor selects anew.
    calendar, origin = {}, inner_start
    while origin < end:
        stop = min(origin+timedelta(days=protocol["step_days"]), start if origin < start else end)
        calendar[origin] = stop
        origin = stop
    windows = {}
    for window in data["windows"]:
        origin = timestamp(window["origin"])
        require(origin not in windows, "duplicate_forecast_origin")
        require(origin in calendar and timestamp(window["end"]) == calendar[origin], "forecast_window_calendar_mismatch")
        require(window["phase"] == ("outer" if origin >= start else "inner_warmup"), "window_phase_mismatch")
        digest_text(window["training_data_sha256"])
        if window["last_result_assumed_available_at"] is not None:
            require(timestamp(window["last_result_assumed_available_at"]) <= origin, "training_result_after_origin")
        ids = window["test_match_ids"]
        require(isinstance(ids, list) and ids and len(set(ids)) == len(ids), "invalid_window_match_ids")
        for match_id in ids:
            integer(match_id, "window_match_id", 1)
        windows[origin] = window
    identities, row_ids = set(), defaultdict(list)
    for row in data["predictions"]:
        match_id = integer(row["match_id"], "match_id", 1)
        require(match_id not in global_ids, "duplicate_match_id")
        global_ids.add(match_id)
        h = integer(row["home_id"], "home_id", 1)
        a = integer(row["away_id"], "away_id", 1)
        require(h != a, "same_home_away_team")
        kickoff, origin = timestamp(row["kickoff"]), timestamp(row["forecast_origin"])
        identity = (h, a, kickoff)
        require(identity not in identities, "duplicate_match_identity")
        identities.add(identity)
        require(origin in windows and origin <= kickoff < calendar[origin], "match_outside_forecast_window")
        require(row["phase"] == windows[origin]["phase"], "match_phase_mismatch")
        require(timestamp(row["result_assumed_available_at"]) == kickoff+timedelta(hours=4)
                and timestamp(row["result_assumed_available_at"]) <= end, "result_availability_mismatch")
        block = start+timedelta(days=((kickoff-start).days//28)*28)
        require(timestamp(row["evaluation_block_start"]) == block, "evaluation_block_mismatch")
        require(row["training_data_sha256"] == windows[origin]["training_data_sha256"], "training_input_origin_mismatch")
        require(type(row["neutral"]) is bool, "neutral_must_be_boolean")
        integer(row["actual_home_goals"], "home_goals")
        integer(row["actual_away_goals"], "away_goals")
        forecasts = row["forecasts"]
        require(isinstance(forecasts, dict) and set(forecasts) <= set(NAMES), "unknown_forecast_candidate")
        for forecast in forecasts.values():
            validate_forecast(forecast)
        common = all(name in forecasts for name in NAMES[:3])
        require(type(row["all_half_lives_common"]) is bool and row["all_half_lives_common"] == common, "common_flag_mismatch")
        require(not common or set(forecasts) == set(NAMES), "incomplete_common_forecasts")
        chosen = row["selected_candidate_at_origin"]
        require(chosen in NAMES[:3] and chosen == windows[origin]["selection"]["candidate"], "selected_candidate_origin_mismatch")
        effective = chosen if chosen in forecasts else (NAMES[0] if NAMES[0] in forecasts else None)
        require(row["effective_policy_candidate"] == effective, "effective_candidate_mismatch")
        require(type(row["policy_fallback"]) is bool and row["policy_fallback"] == (effective is not None and effective != chosen), "policy_fallback_mismatch")
        require((effective is None and "selected_policy" not in forecasts)
                or (effective is not None and forecasts["selected_policy"] == forecasts[effective]), "selected_policy_not_original_candidate")
        row_ids[origin].append(match_id)
    require(set(row_ids) == set(windows), "empty_or_unbound_window")
    for origin, window in windows.items():
        require(set(row_ids[origin]) == set(window["test_match_ids"]), "window_match_identity_mismatch")
    outer = [r for r in data["predictions"] if r["phase"] == "outer"]
    common = [r for r in outer if r["all_half_lives_common"]]
    require(len(common) >= 2, "requires_at_least_two_common_matches_per_competition")
    coverage = data["report"]["coverage"]
    require(coverage == run["coverage"], "manifest_coverage_mismatch")
    for key, expected in (("outer_total", len(outer)), ("all_candidates_common", len(common)),
                          ("inner_warmup_count", len(data["predictions"])-len(outer))):
        require(integer(coverage[key], key) == expected, "coverage_count_mismatch:"+key)
    require(set(coverage["by_candidate"]) == set(NAMES), "coverage_candidate_keys")
    for name in NAMES:
        require(integer(coverage["by_candidate"][name], "candidate_coverage") == sum(name in r["forecasts"] for r in outer), "candidate_coverage_mismatch")
        metric = data["report"]["common_sample_metrics"][name]
        require(integer(metric["n"], "metric_n") == len(common), "metric_sample_size_mismatch")
        for key in SOURCE_METRICS:
            finite(metric["distribution_diagnostics"][key], "source_metric", 0)


def load_inputs(source, plan_path):
    fingerprints = {}
    def read(path):
        value, digest = strict_json(path)
        fingerprints[str(path)] = digest
        return value
    plan, manifest = read(plan_path), read(source/"run_manifest.json")
    require(plan["schema_version"] == "s2-f4-execution-plan-1", "unsupported_plan_version")
    unique_competitions(plan["competitions"])
    unique_competitions(manifest["competitions"])
    require(set(plan["data_sha256"]) == COMPETITIONS, "plan_data_set_mismatch")
    require(manifest["execution_plan_sha256"] == fingerprints[str(plan_path)], "execution_plan_hash_mismatch")
    require(manifest["all_successful"] is True, "incomplete_original_run")
    runs = manifest["runs"]
    unique_competitions([run["competition"] for run in runs])
    for run in runs:
        require(type(run["returncode"]) is int and run["returncode"] == 0
                and run["source_code_unchanged_during_run"] is True, "unsuccessful_original_run")
    for suffix in ("_results.json", "_protocol.json"):
        require({p.name for p in source.glob("*"+suffix)} == {c+suffix for c in COMPETITIONS}, "incomplete_or_extra_input_files")
    for relative in ("football_model/model.py", "football_model/quality.py", "football_model/quality_half_life.py"):
        path = REPO/relative
        fingerprints[str(path)] = sha(path)
        require(fingerprints[str(path)] == manifest["source_code_sha256"][relative], "source_code_fingerprint_mismatch:"+relative)
        module = sys.modules[relative[:-3].replace("/", ".")]
        require(Path(module.__file__).resolve() == path.resolve(), "imported_module_not_current_repository")
    fingerprints[str(Path(__file__).resolve())] = sha(Path(__file__))
    historical = plan["historical"]
    expected = make_protocol(historical["start"], historical["end"], ridge=historical["ridge"],
        min_train=historical["min_train"], step_days=historical["origin_step_days"],
        inner_days=historical["selection_lookback_days"], minimum_inner_matches=historical["minimum_selection_matches"])
    require(historical["candidate_half_life_days"] == [90, 180, 365]
        and historical["baseline_half_life_days"] == 180
        and historical["primary_comparison"] == "past_only_selected_vs_180"
        and historical["primary_metric"] == "one_x_two_log_loss"
        and historical["interval_block_days"] == 28, "unsupported_historical_plan")
    inputs, global_ids = {}, set()
    for run in runs:
        name = run["competition"]
        protocol_path, result_path = source/(name+"_protocol.json"), source/(name+"_results.json")
        bundle, data = read(protocol_path), read(result_path)
        require(fingerprints[str(result_path)] == digest_text(run["result_sha256"]), "result_hash_mismatch:"+name)
        require(bundle["protocol"] == expected == data["report"]["protocol"], "protocol_mismatch:"+name)
        report = data["report"]
        require(report["protocol_file_sha256"] == fingerprints[str(protocol_path)], "protocol_hash_mismatch:"+name)
        require(digest_text(plan["data_sha256"][name]) == bundle["input_file_sha256"] == report["input_file_sha256"], "input_data_hash_mismatch:"+name)
        require(bundle["created_at"] == report["protocol_created_at"], "protocol_created_at_mismatch")
        require(timestamp(bundle["created_at"]) <= timestamp(report["run_started_at"])
                <= timestamp(report["run_completed_at"]), "protocol_run_time_order_mismatch")
        require(report["validation_status"] == "historical_development_replay"
                and report["prospective_validation"] is False and report["deployment_approved"] is False
                and report["point_in_time_sources_verified"] is False, "unsupported_result_scope")
        validate_league(data, expected, run, global_ids)
        inputs[name] = data
    return inputs, fingerprints


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def dump(path, obj):
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2, allow_nan=False) + "\n")


def mean(x):
    return float(np.mean(x)) if len(x) else None


def metrics(rows, name):
    require(bool(rows), "metrics_require_nonempty_matches")
    forecasts = [r["forecasts"][name] for r in rows]
    p = np.asarray([[f["probabilities"][k] for k in OUTCOMES] for f in forecasts])
    h = np.asarray([r["actual_home_goals"] for r in rows])
    a = np.asarray([r["actual_away_goals"] for r in rows])
    rh = np.asarray([f["expected_goals"]["home"] for f in forecasts])
    ra = np.asarray([f["expected_goals"]["away"] for f in forecasts])
    actual = np.where(h > a, 0, np.where(h == a, 1, 2))
    y = np.eye(3)[actual]
    over = np.asarray([f["events"]["over_2_5"] for f in forecasts])
    over_y = (h + a > 2).astype(int)
    home2 = np.asarray([f["events"]["home_win_by_2_plus"] for f in forecasts])
    home2_y = (h - a >= 2).astype(int)
    ll = lambda pr, ob: -(ob * np.log(np.clip(pr, FLOOR, 1)) + (1-ob) * np.log(np.clip(1-pr, FLOOR, 1)))
    values = {
        "one_x_two_log_loss": -np.log(np.clip(p[np.arange(len(rows)), actual], FLOOR, 1)),
        "one_x_two_brier": np.square(p-y).sum(axis=1),
        "score_log_loss": rh+ra-h*np.log(rh)-a*np.log(ra)+gammaln(h+1)+gammaln(a+1),
        "total_log_loss": rh+ra-(h+a)*np.log(rh+ra)+gammaln(h+a+1),
        "margin_log_loss": -np.maximum(math.log(FLOOR), skellam.logpmf(h-a, rh, ra)),
        "total_absolute_error": np.abs(rh+ra-h-a),
        "margin_absolute_error": np.abs(rh-ra-h+a),
        "over_2_5_log_loss": ll(over, over_y),
        "over_2_5_brier": np.square(over-over_y),
        "home_win_by_2_plus_log_loss": ll(home2, home2_y),
        "one_x_two_correct": (np.argmax(p, axis=1) == actual).astype(float),
    }
    for market in ("over_2_25", "over_2_75"):
        settlements = np.where(h+a >= 3, "win", np.where(h+a == 2, "half_loss", "loss")) if market == "over_2_25" else np.where(h+a >= 4, "win", np.where(h+a == 3, "half_win", "loss"))
        values[market+"_settlement_log_loss"] = -np.log(np.clip([f["settlement_probabilities"][market][label] for f,label in zip(forecasts,settlements)], FLOOR, 1))
    return values


def calibration(probs, actual):
    probs, actual = np.asarray(probs), np.asarray(actual)
    require(probs.ndim == actual.ndim == 1 and len(probs) == len(actual), "calibration_shape_mismatch")
    require(bool(np.all(np.isfinite(probs))) and bool(np.all((probs >= 0) & (probs <= 1))), "invalid_calibration_probability")
    require(bool(np.all(np.isin(actual, (0, 1)))), "invalid_binary_outcome")
    bins = []
    for i in range(10):
        mask = (probs >= i/10) & ((probs < (i+1)/10) if i<9 else (probs <= 1))
        bins.append({"lower": i/10, "upper": (i+1)/10, "n": int(mask.sum()),
                     "predicted_mean": mean(probs[mask]), "observed_rate": mean(actual[mask])})
    return {"n":len(probs), "predicted_mean":mean(probs),"observed_rate":mean(actual),
            "observed_minus_predicted":mean(actual-probs),"binary_brier":mean((probs-actual)**2),"binary_log_loss":mean(-(actual*np.log(np.clip(probs,FLOOR,1))+(1-actual)*np.log(np.clip(1-probs,FLOOR,1)))),"fixed_10_bin_ECE_descriptive":sum(b["n"]*abs(b["observed_rate"]-b["predicted_mean"]) for b in bins if b["n"])/len(probs) if len(probs) else None,"bins":bins}


def baseline_diagnostics(rows):
    require(bool(rows), "baseline_diagnostics_require_nonempty_matches")
    f = [r["forecasts"]["half_life_180"] for r in rows]
    h, a = np.asarray([r["actual_home_goals"] for r in rows]), np.asarray([r["actual_away_goals"] for r in rows])
    rh, ra = np.asarray([x["expected_goals"]["home"] for x in f]), np.asarray([x["expected_goals"]["away"] for x in f])
    actual = np.where(h>a,0,np.where(h==a,1,2))
    p = np.asarray([[x["probabilities"][k] for k in OUTCOMES] for x in f])
    result = {k: calibration(p[:,i], (actual==i).astype(int)) for i,k in enumerate(OUTCOMES)}
    result["over_2_5"] = calibration([x["events"]["over_2_5"] for x in f], (h+a>2).astype(int))
    result["home_win_by_2_plus"] = calibration([x["events"]["home_win_by_2_plus"] for x in f], (h-a>=2).astype(int))
    result["top_class_confidence"] = calibration(p.max(axis=1), (p.argmax(axis=1)==actual).astype(int))
    # These are distribution adequacy summaries, not fitted dispersion estimates.
    result["goals"] = {"observed_home_mean":mean(h),"predicted_home_mean":mean(rh),"observed_away_mean":mean(a),"predicted_away_mean":mean(ra),
        "observed_total_mean":mean(h+a),"predicted_total_mean":mean(rh+ra),
        "home_squared_standardized_residual_mean":mean((h-rh)**2/rh),
        "away_squared_standardized_residual_mean":mean((a-ra)**2/ra),
        "total_squared_standardized_residual_mean":mean((h+a-rh-ra)**2/(rh+ra)),
        "home_away_residual_product_mean":mean((h-rh)*(a-ra))}
    # Untruncated independent-Poisson implications of saved rates, not original saved matrix cells.
    result["rate_implied_low_scores"] = {f"{x}-{y}":{"predicted_mean":mean(poisson.pmf(x,rh)*poisson.pmf(y,ra)),"observed_rate":mean((h==x)&(a==y))} for x,y in [(0,0),(0,1),(1,0),(1,1)]}
    result["rate_implied_total_tails"] = {str(k)+"_plus":{"predicted_mean":mean(poisson.sf(k-1,rh+ra)),"observed_rate":mean(h+a>=k),"observed_n":int((h+a>=k).sum())} for k in (4,5,6,7)}
    result["rate_implied_absolute_margin_tails"] = {str(k)+"_plus":{"predicted_mean":mean(skellam.cdf(-k,rh,ra)+skellam.sf(k-1,rh,ra)),"observed_rate":mean(np.abs(h-a)>=k),"observed_n":int((np.abs(h-a)>=k).sum())} for k in (2,3)}
    result["reconstructed_original_normalized_matrix_events"] = {key:{**calibration([r["_derived_baseline"][key] for r in rows],observed.astype(int)),"observed_n":int(np.sum(observed))} for key,observed in {
        **{f"score_{x}_{y}":(h==x)&(a==y) for x,y in [(0,0),(0,1),(1,0),(1,1)]},
        **{f"total_{k}_plus":h+a>=k for k in (4,5,6,7)},
        **{f"absolute_margin_{k}_plus":np.abs(h-a)>=k for k in (2,3)}
    }.items()}
    tail = np.asarray([x["tail_mass_before_normalization"] for x in f])
    result["saved_matrix_truncation"] = {"max_tail_mass":float(tail.max()),"mean_tail_mass":mean(tail),"actual_score_outside_saved_matrix_shape_n":sum(r["actual_home_goals"]>=x["matrix_shape"][0] or r["actual_away_goals"]>=x["matrix_shape"][1] for r,x in zip(rows,f))}
    return result


def paired_summary(rows, delta):
    require(len(rows) == len(delta) and len(delta) >= 2, "paired_summary_requires_two_matches")
    require(bool(np.all(np.isfinite(delta))), "nonfinite_paired_difference")
    n, mu = len(delta), mean(delta)
    var = float(np.var(delta, ddof=1))
    grouped = defaultdict(list)
    for r,d in zip(rows,delta): grouped[r["evaluation_block_start"]].append(float(d))
    blocks = [{"block_start":k,"n":len(v),"sum_difference":sum(v),"mean_difference":mean(v)} for k,v in sorted(grouped.items())]
    g = len(blocks)
    # Calendar blocks are shared across leagues, preserving cross-league date dependence.
    cluster_mean_var = g/(g-1)*sum((b["sum_difference"]-b["n"]*mu)**2 for b in blocks)/n**2 if g>1 else None
    scaled_var = n*cluster_mean_var if cluster_mean_var is not None else None
    z = NormalDist().inv_cdf(.975)+NormalDist().inv_cdf(.8)
    n_iid = math.ceil(z*z*var/.01**2)
    n_cluster = math.ceil(z*z*scaled_var/.01**2) if scaled_var is not None else None
    return {"n":n,"mean_candidate_minus_baseline":mu,"paired_sd":math.sqrt(var),"iid_standard_error":math.sqrt(var/n),
        "calendar_28_day_blocks":g,"calendar_cluster_standard_error_descriptive":math.sqrt(cluster_mean_var) if cluster_mean_var is not None else None,
        "variance_inflation_vs_iid_descriptive":scaled_var/var if var and scaled_var is not None else None,"blocks":blocks,
        "sample_size_sensitivity_not_validation_gate":{"alpha_one_sided":.025,"target_power":.8,"hypothesized_true_reduction":.01,"null_reduction":0.,"normal_approximation_n_iid":n_iid,"normal_approximation_n_empirical_calendar_cluster":n_cluster,"normal_approximation_n_no_cluster_discount":max(n_iid,n_cluster or 0),"n_iid_times_design_effect":{"1":n_iid,"2":math.ceil(2*z*z*var/.01**2),"4":math.ceil(4*z*z*var/.01**2)},"detectable_reduction_at_n185_iid_80pct":z*math.sqrt(var/185),"formula":"ceil((z_0.975 + z_0.80)^2 * planning_variance / (true_reduction - null_reduction)^2)","minimum_improvement_gate_note":"These n values test improvement versus zero when true reduction is 0.01. For a CI upper bound below -0.01, the same n applies only if true reduction is 0.02; at true reduction 0.01 the boundary rejection probability is alpha, not 80%.","planning_scope_note":"This is a normal approximation for this existing comparison only, not a sample-size target for an untrained new candidate. IID is an idealized reference, not a mathematical lower bound; empirical block variance below IID is not used to discount the no-cluster-discount planning sensitivity."}}


def run_audit(input_dir, plan_path, output_dir):
    source, plan_path, output = validate_paths(input_dir, plan_path, output_dir)
    inputs, before = load_inputs(source, plan_path)
    allrows, leagues, reproduce = [], {}, []
    pairlines = []
    rebuilt_count = 0
    rebuilt_max_error = 0.
    # Preserve the frozen analysis's filename ordering, including bundesliga_2
    # before bundesliga. It fixes both per-match order and floating sum order.
    for name, data in sorted(inputs.items(), key=lambda item: item[0]+"_results.json"):
        outer=[r for r in data["predictions"] if r["phase"]=="outer"]
        rows=[dict(r,competition=name) for r in outer if r["all_half_lives_common"]]
        for r in rows:
            for model in NAMES:
                f=r["forecasts"][model]
                distribution=_distribution_from_rates(f["expected_goals"]["home"],f["expected_goals"]["away"])
                rebuilt=summarize_distribution(distribution["score_matrix"],distribution["expected_goals"])
                matrix=np.asarray(distribution["score_matrix"])
                require(list(matrix.shape)==f["matrix_shape"], "reconstructed_matrix_shape_mismatch")
                errors=[abs(distribution["tail_mass_before_normalization"]-f["tail_mass_before_normalization"])]
                for group in ("probabilities","events"):
                    errors.extend(abs(rebuilt[group][k]-v) for k,v in f[group].items())
                for market,settlements in f["settlement_probabilities"].items():
                    errors.extend(abs(rebuilt["settlement_probabilities"][market][k]-v) for k,v in settlements.items())
                require(max(errors)<=1e-15, f"saved_forecast_does_not_recompute:{name}:{r['match_id']}:{model}")
                rebuilt_max_error=max(rebuilt_max_error,max(errors))
                rebuilt_count+=1
                if model=="half_life_180":
                    hi,ai=np.indices(matrix.shape)
                    r["_derived_baseline"]={**{f"score_{x}_{y}":float(matrix[x,y]) for x,y in [(0,0),(0,1),(1,0),(1,1)]},**{f"total_{k}_plus":float(matrix[hi+ai>=k].sum()) for k in (4,5,6,7)},**{f"absolute_margin_{k}_plus":float(matrix[np.abs(hi-ai)>=k].sum()) for k in (2,3)}}
        values={model:metrics(rows,model) for model in NAMES}
        for model in NAMES:
            for key in SOURCE_METRICS:
                old=data["report"]["common_sample_metrics"][model]["distribution_diagnostics"][key]
                new=mean(values[model][key])
                require(math.isclose(old,new,rel_tol=0,abs_tol=2e-12), f"source_metric_does_not_reproduce:{name}:{model}:{key}")
                reproduce.append({"competition":name,"model":model,"metric":key,"absolute_difference":abs(old-new)})
        leagues[name]={"outer_n":len(outer),"common_n":len(rows),"excluded_n":len(outer)-len(rows),"excluded_ids":[r["match_id"] for r in outer if not r["all_half_lives_common"]],"calendar_blocks":len({r["evaluation_block_start"] for r in rows}),"forecast_origins":len({r["forecast_origin"] for r in rows}),"metrics":{model:{key:mean(v) for key,v in vals.items()} for model,vals in values.items()},"baseline_diagnostics":baseline_diagnostics(rows),"paired_1x2":{model:paired_summary(rows,values[model]["one_x_two_log_loss"]-values["half_life_180"]["one_x_two_log_loss"]) for model in NAMES[1:]}}
        for i,r in enumerate(rows):
            pairlines.append({"competition":name,"match_id":r["match_id"],"kickoff":r["kickoff"],"forecast_origin":r["forecast_origin"],"evaluation_block_start":r["evaluation_block_start"],"actual_home_goals":r["actual_home_goals"],"actual_away_goals":r["actual_away_goals"],"metrics":{model:{key:float(v[i]) for key,v in vals.items()} for model,vals in values.items()}})
        allrows.extend(rows)
    values={model:metrics(allrows,model) for model in NAMES}
    after = {path:sha(Path(path)) for path in before}
    require(before == after, "input_or_source_changed_during_analysis")
    summary={"schema_version":"f5_historical_error_localization_1","analysis_status":"exploratory_descriptive_only","created_at":datetime.now(timezone.utc).isoformat(),"source_inputs_unchanged":True,"new_model_fits":0,"new_api_calls":0,"future_results_read":False,"metrics_reproduction_checks":len(reproduce),"max_metrics_reproduction_error":max(x["absolute_difference"] for x in reproduce),"scope":{"outer_total":sum(v["outer_n"] for v in leagues.values()),"all_candidate_common_matches":len(allrows),"competitions":len(leagues),"calendar_blocks":len({r["evaluation_block_start"] for r in allrows}),"kickoff_min":min(r["kickoff"] for r in allrows),"kickoff_max":max(r["kickoff"] for r in allrows),"excluded_total":sum(v["excluded_n"] for v in leagues.values()),"weighting":"match_weighted_not_equal_league_weighted"},"overall_metrics":{model:{key:mean(v) for key,v in vals.items()} for model,vals in values.items()},"baseline_diagnostics":baseline_diagnostics(allrows),"paired_1x2":{model:paired_summary(allrows,values[model]["one_x_two_log_loss"]-values["half_life_180"]["one_x_two_log_loss"]) for model in NAMES[1:]},"by_competition":leagues,"limitations":["Previously inspected historical development data; not a new independent confirmation set.","Corrected historical source records lack verified historical availability timestamps; this does not recreate genuine past information sets.","Weekly research-origin 180-day model differs from daily deployed scheduling; comparison is against the F4 research baseline.","Exploratory subgroup/calibration inspection has no multiplicity-adjusted inference and makes no improvement claim.","Score, total and margin diagnostics use the saved rates' untruncated independent-Poisson distribution; primary 1X2 uses the original saved normalized probabilities.","Calibration error is descriptive and bin-dependent; small bins are not estimates to apply as correction factors.","Squared residuals combine parameter error, omitted factors and distribution mismatch; they do not identify causation.","Sample-size estimates assume stable paired variance and match mix, fixed one comparison, fixed terminal analysis, and no repeated peeking. The 0.01 target is log-loss units, not one percentage point of accuracy.","Only 13 pooled calendar blocks and 3 Nations League blocks; cluster-based planning variance is noisy, cross-block serial dependence is not resolved.","No real odds, executed stakes, matched GPT decisions or future settled results are present in these historical forecasts; ROI, EV improvement and GPT contribution cannot be estimated."]}
    summary["original_function_reconstruction"]={"forecast_count":rebuilt_count,"max_absolute_saved_field_error":rebuilt_max_error,"absolute_tolerance":1e-15,"new_fit_performed":False,"matrix_source":"Deterministically reconstructed using F4 original _distribution_from_rates and saved rates; saved 1X2/events/settlement probabilities/tail/shape checked for every common-match forecast."}
    summary["original_function_reconstruction"]["sample_unit_note"] = f"{rebuilt_count:,} = {len(NAMES)} model/policy outputs x {len(allrows):,} independent match identities; selected policy can reuse a fixed candidate. Reconstructions are not additional samples. Match identities still have team/time dependence."
    protocol = next(iter(inputs.values()))["report"]["protocol"]
    full_spans, partial_days = divmod((timestamp(protocol["end"])-timestamp(protocol["start"])).days, 28)
    partial_start = timestamp(protocol["start"])+timedelta(days=28*full_spans)
    partial_n = sum(timestamp(row["kickoff"]) >= partial_start for row in allrows)
    partial_note = f" plus the final {partial_days}-day partial span with {partial_n} matches" if partial_days else ""
    summary["limitations"][8] = (f"{summary['scope']['calendar_blocks']} occupied pooled calendar blocks: "
        f"{full_spans} full 28-day calendar spans{partial_note}; Nations League has only "
        f"{leagues['unl']['calendar_blocks']} occupied blocks. Unequal block sizes are preserved; "
        "cluster variance is noisy and cross-block serial dependence is not resolved.")
    summary["equal_competition_weighted_metrics"]={model:{key:mean([v["metrics"][model][key] for v in leagues.values()]) for key in values[model]} for model in NAMES}
    summary["equal_competition_weighted_baseline_calibration_gap"]={key:mean([v["baseline_diagnostics"][key]["observed_minus_predicted"] for v in leagues.values()]) for key in (*OUTCOMES,"over_2_5","home_win_by_2_plus","top_class_confidence")}
    summary["equal_competition_weighted_baseline_matrix_event_gap"]={key:mean([v["baseline_diagnostics"]["reconstructed_original_normalized_matrix_events"][key]["observed_minus_predicted"] for v in leagues.values()]) for key in allrows[0]["_derived_baseline"]}
    summary["equal_competition_weighted_scope_note"]="Equal-competition weighting is a separate estimand used only as descriptive sensitivity; it does not replace the match-weighted observed competition mix."
    provenance = {"source_files":before,"unchanged_after_analysis":before==after,
        "script_sha256":sha(Path(__file__)),"python":sys.version,"numpy":np.__version__,
        "scipy":scipy.__version__,"metric_reproduction_tolerance_absolute":2e-12,
        "checks":reproduce,"input_directory":str(source),"plan_path":str(plan_path),
        "imported_modules": {"model":str(Path(sys.modules["football_model.model"].__file__).resolve()),
                             "quality":str(Path(sys.modules["football_model.quality"].__file__).resolve())}}
    # Serialize every byte before the sole filesystem mutation. mkdir is exclusive,
    # including an empty existing directory, and never overwrites prior evidence.
    contents = {
        "historical_error_localization.json": json.dumps(summary,ensure_ascii=False,indent=2,allow_nan=False)+"\n",
        "source_lock_and_reproduction.json": json.dumps(provenance,ensure_ascii=False,indent=2,allow_nan=False)+"\n",
        "historical_paired_metrics.jsonl": "".join(json.dumps(line,ensure_ascii=False,allow_nan=False,separators=(",",":"))+"\n" for line in pairlines),
    }
    output.mkdir(exist_ok=False)
    for name, content in contents.items():
        with (output/name).open("x", encoding="utf-8") as handle:
            handle.write(content)
    return {"completed":True,"common_n":len(allrows),"reproduction_checks":len(reproduce),
            "outputs":list(contents),"new_model_fits":0,"new_api_calls":0}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", type=Path, required=True, help="Complete F4 historical_v1 directory")
    parser.add_argument("--plan", type=Path, required=True, help="Frozen F4 execution plan")
    parser.add_argument("--output-dir", type=Path, required=True, help="New directory; parent must exist")
    args = parser.parse_args(argv)
    try:
        result = run_audit(args.input_dir, args.plan, args.output_dir)
    except (ValueError, KeyError, TypeError, OSError, OverflowError) as exc:
        print(json.dumps({"completed":False,"error":str(exc)},ensure_ascii=False),file=sys.stderr)
        return 1
    print(json.dumps(result,ensure_ascii=False))
    return 0


if __name__=="__main__":
    raise SystemExit(main())
