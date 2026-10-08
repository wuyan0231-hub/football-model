"""Frozen, offline F5 development experiment; never fits or changes production.

The historical outer rows have already been inspected. Sequential selection is
an honest algorithmic time split, not an independent confirmatory experiment or
a reconstruction of data actually available at each historical forecast date.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import inspect
import json
import math
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import scipy
from scipy.special import gammaln
from scipy.stats import skellam

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
from football_model.model import _distribution_from_rates
from football_model.quality import summarize_distribution
from football_model.quality_half_life import _losses as original_losses, LOSS_NAMES

ALPHAS = (0., .25, .5, .75, 1.)
FLOOR = 1e-15
OUTCOMES = ("home_win", "draw", "away_win")
BASELINE = "alpha_0"
COMPETITIONS = ("epl", "laliga", "bundesliga", "serie_a", "ligue_1", "bundesliga_2", "ligue_2", "allsvenskan", "eredivisie", "primeira", "brazil_a", "j1", "k1", "mls", "unl", "liga_mx", "segunda", "argentina_a")


def strict_json(value):
    def pairs(items):
        obj = {}
        for key, item in items:
            if key in obj:
                raise ValueError("duplicate JSON key")
            obj[key] = item
        return obj
    def invalid(value):
        raise ValueError("non-finite JSON constant: "+value)
    def finite_float(value):
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("non-finite JSON number")
        return number
    return json.loads(value, object_pairs_hook=pairs, parse_constant=invalid, parse_float=finite_float)


def stamp():
    return datetime.now(timezone.utc).isoformat()


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def dump(path, value):
    with Path(path).open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")


def timestamp(value):
    if not isinstance(value, str):
        raise ValueError("timestamp must be an explicit timezone string")
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError("timestamp requires timezone")
    return dt.astimezone(timezone.utc)


def positive(value):
    return type(value) in (int, float) and math.isfinite(value) and value > 0


def validate_forecast(forecast):
    required = {"expected_goals", "probabilities", "events", "settlement_probabilities", "lineup_adjusted", "model_scope", "matrix_shape", "tail_mass_before_normalization"}
    if type(forecast) is not dict or set(forecast) != required:
        raise ValueError("saved forecast schema differs")
    shrink_rates(forecast["expected_goals"], forecast["expected_goals"], 0)
    groups = [(forecast["probabilities"], set(OUTCOMES), True),
              (forecast["events"], {"over_2_5", "home_win_by_2_plus"}, False)]
    if set(forecast["settlement_probabilities"]) != {"over_2_25", "over_2_75"}:
        raise ValueError("settlement market set differs")
    groups += [(states, {"win", "half_win", "push", "half_loss", "loss"}, True) for states in forecast["settlement_probabilities"].values()]
    for group, keys, sums_to_one in groups:
        if (set(group) != keys or any(type(v) not in (int,float) or not math.isfinite(v) or not 0 <= v <= 1 for v in group.values())
                or sums_to_one and not math.isclose(math.fsum(group.values()), 1., rel_tol=0, abs_tol=1e-12)):
            raise ValueError("invalid probability fields")
    shape, tail = forecast["matrix_shape"], forecast["tail_mass_before_normalization"]
    if (type(shape) is not list or len(shape) != 2 or any(type(v) is not int or not 1 <= v <= 401 for v in shape)
            or type(tail) not in (int,float) or not math.isfinite(tail) or not 0 <= tail <= 1
            or forecast["lineup_adjusted"] is not False or forecast["model_scope"] != "historical_scores_only_research"):
        raise ValueError("invalid saved matrix metadata")


def alpha_name(alpha):
    return "alpha_" + format(alpha, ".2f").rstrip("0").rstrip(".")


def shrink_rates(baseline, league_mean, alpha):
    if type(alpha) not in (int, float) or alpha not in ALPHAS:
        raise ValueError("alpha must be one of the five frozen values")
    for rates in (baseline, league_mean):
        if set(rates) != {"home", "away"} or not all(positive(v) for v in rates.values()):
            raise ValueError("rates must have precisely two finite positive values")
    # Reuse exactly, including last-bit representation, at alpha zero.
    if alpha == 0:
        return dict(baseline)
    total = baseline["home"] + baseline["away"]
    target = league_mean["home"] + league_mean["away"]
    new_total = (1-alpha) * total + alpha * target
    return {side: new_total * baseline[side] / total for side in ("home", "away")}


def total_log_loss(total_rate, total_goals):
    if not positive(total_rate) or type(total_goals) is not int or total_goals < 0:
        raise ValueError("invalid total rate or outcome")
    return float(total_rate - total_goals * math.log(total_rate) + gammaln(total_goals + 1))


def select_alpha(rows, origin, competition, *, lookback_days=180, delay_hours=48, minimum_matches=40):
    """Only the selector consumes past outcomes, under an explicit 48h embargo."""
    now = timestamp(origin)
    if lookback_days != 180 or delay_hours != 48 or minimum_matches != 40:
        raise ValueError("selector settings differ from frozen candidate")
    seen, eligible = set(), []
    for row in rows:
        identity = row["match_id"]
        if identity in seen:
            raise ValueError("duplicate match identity cannot add selection weight")
        seen.add(identity)
        if row["competition"] != competition:
            continue
        kickoff = timestamp(row["kickoff"])
        if (row["phase"] == "outer" and row["all_half_lives_common"] is True
                and now-timedelta(days=lookback_days) <= kickoff
                and kickoff+timedelta(hours=delay_hours) <= now
                and timestamp(row["forecast_origin"]) < now):
            eligible.append(row)
    eligible.sort(key=lambda r: (timestamp(r["kickoff"]), r["match_id"]))
    losses = {alpha_name(a): None for a in ALPHAS}
    if eligible:
        for alpha in ALPHAS:
            values = []
            for row in eligible:
                rates = shrink_rates(row["forecasts"]["half_life_180"]["expected_goals"],
                                     row["forecasts"]["league_mean"]["expected_goals"], alpha)
                values.append(total_log_loss(sum(rates.values()), row["actual_home_goals"]+row["actual_away_goals"]))
            losses[alpha_name(alpha)] = math.fsum(values) / len(values)
    if len(eligible) >= minimum_matches:
        best = min(losses.values())
        chosen = min(a for a in ALPHAS if losses[alpha_name(a)] <= best + 1e-12)
    else:
        chosen = 0.
    return {"alpha": chosen, "n": len(eligible), "eligible_match_ids": [r["match_id"] for r in eligible],
            "origin": origin, "competition": competition,
            "kickoff_lower_inclusive": (now-timedelta(days=lookback_days)).isoformat(),
            "kickoff_upper_inclusive": (now-timedelta(hours=delay_hours)).isoformat(),
            "latest_assumed_result_available_at": (max(timestamp(r["kickoff"]) for r in eligible)+timedelta(hours=delay_hours)).isoformat() if eligible else None,
            "mean_total_log_loss_by_alpha": losses,
            "reason": "past_minimum_total_log_loss" if len(eligible) >= minimum_matches else "fewer_than_40_use_alpha_0"}


def complete_forecast(row, alpha):
    base = row["forecasts"]["half_life_180"]
    validate_forecast(base)
    rates = shrink_rates(base["expected_goals"], row["forecasts"]["league_mean"]["expected_goals"], alpha)
    distribution = _distribution_from_rates(rates["home"], rates["away"])
    matrix = np.asarray(distribution["score_matrix"])
    summary = summarize_distribution(matrix, rates)
    summary.update(matrix_shape=list(matrix.shape), tail_mass_before_normalization=distribution["tail_mass_before_normalization"])
    if alpha == 0:
        # Saved summaries are the original baseline. Reconstructed fields only
        # supplement them; they never overwrite the saved probabilities.
        errors = [abs(summary["tail_mass_before_normalization"]-base["tail_mass_before_normalization"])]
        for group in ("probabilities", "events"):
            errors += [abs(summary[group][key]-value) for key, value in base[group].items()]
        for market, states in base["settlement_probabilities"].items():
            errors += [abs(summary["settlement_probabilities"][market][key]-value) for key, value in states.items()]
        if summary["matrix_shape"] != base["matrix_shape"] or max(errors) > 1e-15:
            raise ValueError("original alpha-zero reconstruction failed")
        summary = copy.deepcopy(base)
    h, a = np.indices(matrix.shape)
    summary.update(score_matrix=distribution["score_matrix"], score_matrix_axes=distribution["score_matrix_axes"],
                   matrix_renormalized=True, tail_tolerance=distribution["tail_tolerance"],
                   total_goals_distribution={str(k): float(matrix[h+a == k].sum()) for k in range(sum(matrix.shape)-1)},
                   goal_margin_distribution={str(k): float(matrix[h-a == k].sum()) for k in range(1-matrix.shape[1], matrix.shape[0])})
    return summary


def metrics(row, forecast):
    h, a = row["actual_home_goals"], row["actual_away_goals"]
    rh, ra = forecast["expected_goals"]["home"], forecast["expected_goals"]["away"]
    actual = 0 if h > a else 1 if h == a else 2
    ps = [forecast["probabilities"][key] for key in OUTCOMES]
    over = forecast["events"]["over_2_5"]
    over_y = int(h+a >= 3)
    return {"total_log_loss": total_log_loss(rh+ra, h+a),
            "score_log_loss": float(rh+ra-h*math.log(rh)-a*math.log(ra)+gammaln(h+1)+gammaln(a+1)),
            "margin_log_loss": -max(math.log(FLOOR), float(skellam.logpmf(h-a, rh, ra))),
            "one_x_two_log_loss": -math.log(max(FLOOR, ps[actual])),
            "one_x_two_brier": sum((p-int(i == actual))**2 for i,p in enumerate(ps)),
            "one_x_two_correct": float(max(range(3), key=lambda i: ps[i]) == actual),
            "over_2_5_log_loss": -math.log(max(FLOOR, over if over_y else 1-over)),
            "over_2_5_brier": (over-over_y)**2,
            "total_absolute_error": abs(rh+ra-h-a), "margin_absolute_error": abs(rh-ra-h+a)}


def validate_row(row, competition, window):
    if type(row.get("match_id")) is not int or row["match_id"] <= 0:
        raise ValueError("invalid match identity")
    if type(row.get("neutral")) is not bool:
        raise ValueError("neutral must be explicit boolean")
    for key in ("actual_home_goals", "actual_away_goals"):
        if type(row.get(key)) is not int or row[key] < 0:
            raise ValueError("outcomes must be nonnegative integers")
    origin, kickoff = timestamp(row["forecast_origin"]), timestamp(row["kickoff"])
    if (row["forecast_origin"] != window["origin"] or row["phase"] != "outer"
            or window["phase"] != "outer" or not origin <= kickoff < timestamp(window["end"])
            or row["training_data_sha256"] != window["training_data_sha256"]
            or row["match_id"] not in window["test_match_ids"]):
        raise ValueError("forecast row and its source window are not bound")
    base = window["model_parameters"]["half_life_180"]
    if (base["training_cutoff"] != row["forecast_origin"]
            or base["training_data_sha256"] != row["training_data_sha256"]
            or timestamp(window["last_result_assumed_available_at"]) > origin):
        raise ValueError("source baseline model is not bound to the same origin")
    means = window["league_mean_rates"]
    expected = {"home": sum(means.values())/2, "away": sum(means.values())/2} if row["neutral"] else means
    if row["forecasts"]["league_mean"]["expected_goals"] != expected:
        raise ValueError("league mean must come from this exact row's training window")
    shrink_rates(row["forecasts"]["half_life_180"]["expected_goals"], expected, 0)
    if not all(n in row["forecasts"] for n in ("half_life_180", "half_life_90", "half_life_365", "league_mean")):
        raise ValueError("common row lacks an original candidate")
    for name in ("half_life_180", "half_life_90", "half_life_365", "league_mean"):
        validate_forecast(row["forecasts"][name])
    return dict(row, competition=competition)


def load_rows(input_dir, protocol):
    manifest = strict_json((input_dir/"run_manifest.json").read_text())
    runs = manifest["runs"]
    competitions = [r["competition"] for r in runs]
    if len(set(competitions)) != len(competitions):
        raise ValueError("duplicate competition manifest entry")
    if set(competitions) != set(protocol["competitions"]):
        raise ValueError("manifest competitions differ from frozen scope")
    run_by_competition = {r["competition"]: r for r in runs}
    rows, excluded, seen = [], [], set()
    for competition in protocol["competitions"]:
        run = run_by_competition[competition]
        filename = competition+"_results.json"
        if protocol["inputs"][filename] != run["result_sha256"]:
            raise ValueError("input lock disagrees with original F4 manifest")
        data = strict_json((input_dir/filename).read_text())
        source_protocol = strict_json((input_dir/(competition+"_protocol.json")).read_text())["protocol"]
        if source_protocol != data["report"]["protocol"]:
            raise ValueError("original result's protocol differs from its locked source")
        windows = {w["origin"]: w for w in data["windows"]}
        if len(windows) != len(data["windows"]):
            raise ValueError("duplicate forecast origin in source windows")
        for row in data["predictions"]:
            if row["phase"] != "outer":
                continue
            identity = row["match_id"]
            if identity in seen:
                raise ValueError("duplicate global match identity")
            seen.add(identity)
            if row["all_half_lives_common"] is not True:
                excluded.append({"competition": competition, "match_id": identity, "unavailable": row["unavailable"]})
                continue
            rows.append(validate_row(row, competition, windows[row["forecast_origin"]]))
    if len(rows) != protocol["expected_common_matches"] or len(competitions) != protocol["expected_competitions"]:
        raise ValueError("frozen sample size or competition scope differs")
    cohort = [[r[key] for key in ("competition", "match_id", "kickoff", "forecast_origin")] for r in rows]
    cohort_sha = hashlib.sha256(json.dumps(cohort, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
    if cohort_sha != protocol["ordered_cohort_sha256"]:
        raise ValueError("ordered common cohort differs from freeze")
    if len(excluded) != protocol["coverage"]["F4_not_common"] or len(rows)+len(excluded) != protocol["coverage"]["F4_outer_matches"]:
        raise ValueError("original outer exclusions differ from freeze")
    return rows, excluded


def validate_protocol(protocol):
    if (protocol["schema_version"] != "f5_total_shrink_protocol_v1"
            or protocol["status"] != "frozen_before_candidate_execution"
            or protocol["alphas"] != list(ALPHAS)
            or any(type(a) not in (int, float) for a in protocol["alphas"])
            or any(protocol["selector"][k] != v for k,v in {"lookback_days": 180, "result_delay_hours": 48, "minimum_matches": 40, "tie_break": "smallest_alpha", "tie_absolute_tolerance": 1e-12}.items())
            or any(protocol["primary"][k] != v for k,v in {"candidate": "past_selected", "baseline": "alpha_0", "metric": "total_log_loss"}.items())):
        raise ValueError("unsupported or unfrozen candidate protocol")
    if timestamp(protocol["frozen_at"]) > datetime.now(timezone.utc):
        raise ValueError("protocol freeze cannot be in the future")
    expected_inputs = {"run_manifest.json"} | {c+suffix for c in COMPETITIONS for suffix in ("_results.json", "_protocol.json")}
    if (set(protocol["inputs"]) != expected_inputs or protocol["competitions"] != list(COMPETITIONS)
            or type(protocol["expected_competitions"]) is not int or protocol["expected_competitions"] != 18
            or type(protocol["expected_common_matches"]) is not int or protocol["expected_common_matches"] != 5802):
        raise ValueError("complete frozen 37-file/18-competition scope required")
    for key, value in {"repetitions":2000,"seed":20261008}.items():
        if type(protocol["uncertainty"][key]) is not int or protocol["uncertainty"][key] != value:
            raise ValueError("bootstrap parameter differs from freeze")
    for key, value in {"max_mean_total_log_loss_delta":-.01, "max_mean_over_2_5_brier_delta":0., "max_mean_one_x_two_log_loss_delta":.005, "max_mean_score_log_loss_delta":.005}.items():
        if type(protocol["development_screen"][key]) not in (int,float) or protocol["development_screen"][key] != value:
            raise ValueError("development screening boundary differs from freeze")
    for name, checksum in protocol["inputs"].items():
        if Path(name).name != name or len(checksum) != 64 or any(c not in "0123456789abcdef" for c in checksum):
            raise ValueError("input lock must contain simple filenames and SHA256")


def aggregate(rows):
    names = tuple(rows[0]["metrics"])
    keys = tuple(rows[0]["metrics"][BASELINE])
    result = {}
    for name in names:
        means = {key: math.fsum(r["metrics"][name][key] for r in rows)/len(rows) for key in keys}
        diffs = {key: math.fsum(r["metrics"][name][key]-r["metrics"][BASELINE][key] for r in rows)/len(rows) for key in keys}
        result[name] = {"n": len(rows), "mean_metrics": means, "candidate_minus_alpha_0": diffs}
    return result


def descriptive_intervals(rows, protocol):
    """Shared calendar blocks preserve contemporaneous cross-league dependence."""
    uncertainty = protocol["uncertainty"]
    if uncertainty["method"] != "shared_calendar_28_day_block_bootstrap_of_paired_difference_ratio_of_sums":
        raise ValueError("unsupported descriptive uncertainty method")
    names, keys = tuple(rows[0]["metrics"]), tuple(rows[0]["metrics"][BASELINE])
    blocks = defaultdict(list)
    anchor = timestamp(uncertainty["origin"])
    for row in rows:
        block_start = anchor+timedelta(days=((timestamp(row["kickoff"])-anchor).days//28)*28)
        if block_start != timestamp(row["evaluation_block_start"]):
            raise ValueError("saved evaluation block differs from frozen calendar")
        blocks[block_start.isoformat()].append(row)
    counts, sums = [], []
    for block in sorted(blocks):
        members = blocks[block]
        counts.append(len(members))
        sums.append([[math.fsum(r["metrics"][name][key]-r["metrics"][BASELINE][key] for r in members) for key in keys] for name in names])
    count_array, sum_array = np.asarray(counts), np.asarray(sums)
    rng = np.random.default_rng(uncertainty["seed"])
    indices = rng.integers(0, len(blocks), size=(uncertainty["repetitions"], len(blocks)))
    resampled = sum_array[indices].sum(axis=1) / count_array[indices].sum(axis=1)[:,None,None]
    limits = np.quantile(resampled, [.025,.975], axis=0)
    return {"method": uncertainty["method"], "repetitions": uncertainty["repetitions"], "seed": uncertainty["seed"],
            "block_count": len(blocks), "blocks": [{"start": b, "n": len(blocks[b])} for b in sorted(blocks)],
            "descriptive_only_not_confirmation": True,
            "candidate_minus_alpha_0_interval_95": {name: {key: limits[:,i,j].tolist() for j,key in enumerate(keys)} for i,name in enumerate(names)}}


def run_experiment(input_dir, protocol_path, output_dir):
    for path in (input_dir, protocol_path, output_dir):
        if any(item.is_symlink() for item in (path,*path.parents)):
            raise ValueError("input, protocol and output paths must not traverse symbolic links")
    if output_dir.resolve().is_relative_to(input_dir.resolve()):
        raise ValueError("output directory must be outside immutable input directory")
    if output_dir.exists():
        raise FileExistsError("output directory must be new; existing evidence is never overwritten")
    protocol_bytes = protocol_path.read_bytes()
    protocol = strict_json(protocol_bytes)
    validate_protocol(protocol)
    locked = {name: digest(input_dir/name) for name in protocol["inputs"]}
    if locked != protocol["inputs"]:
        raise ValueError("input bytes differ from frozen lock")
    code_before = {str(p.relative_to(REPO)): digest(p) for p in (Path(__file__), REPO/"football_model/model.py", REPO/"football_model/quality.py", REPO/"football_model/quality_half_life.py")}
    source_manifest = strict_json((input_dir/"run_manifest.json").read_text())
    for function, name in ((_distribution_from_rates,"model"),(summarize_distribution,"quality"),(original_losses,"quality_half_life")):
        if Path(inspect.getfile(function)).resolve() != (REPO/"football_model"/(name+".py")).resolve():
            raise ValueError("imported source is not the locked repository source")
    for name in ("football_model/model.py", "football_model/quality.py", "football_model/quality_half_life.py"):
        if code_before[name] != source_manifest["source_code_sha256"][name]:
            raise ValueError("original source code differs from F4 execution manifest")
    output_dir.mkdir(parents=True)
    started = stamp()
    provenance = {"started_at": started, "protocol_sha256": hashlib.sha256(protocol_bytes).hexdigest(),
                  "protocol_frozen_at": protocol["frozen_at"], "input_files_sha256": locked,
                  "source_code_sha256": code_before, "python": sys.version, "numpy": np.__version__, "scipy": scipy.__version__,
                  "new_base_model_fits": 0, "new_api_calls": 0, "production_changes": False,
                  "experiment_kind": "previously_inspected_historical_development_only"}
    dump(output_dir/"run_started.json", provenance)
    (output_dir/"protocol_exact_bytes.json").write_bytes(protocol_bytes)
    try:
        rows, excluded = load_rows(input_dir, protocol)
        groups, selections, results = defaultdict(list), {}, []
        algebra_max_error = 0.
        for row in rows:
            groups[row["competition"]].append(row)
        with (output_dir/"predictions.jsonl").open("x", encoding="utf-8") as stream:
            for row in rows:
                group = (row["competition"], row["forecast_origin"])
                if group not in selections:
                    selections[group] = select_alpha(groups[row["competition"]], row["forecast_origin"], row["competition"])
                selection = selections[group]
                forecasts = {alpha_name(a): complete_forecast(row, a) for a in ALPHAS}
                forecasts["past_selected"] = forecasts[alpha_name(selection["alpha"])]
                scored = {name: metrics(row, forecast) for name, forecast in forecasts.items()}
                # These seven original F4 diagnostics are used without changing
                # arithmetic or baseline probabilities.
                scored[BASELINE].update(dict(zip(LOSS_NAMES, original_losses(row, "half_life_180"))))
                if selection["alpha"] == 0:
                    scored["past_selected"] = dict(scored[BASELINE])
                for name, values in scored.items():
                    error = abs((values["score_log_loss"]-scored[BASELINE]["score_log_loss"])-(values["total_log_loss"]-scored[BASELINE]["total_log_loss"]))
                    algebra_max_error = max(algebra_max_error, error)
                    if error > 1e-12:
                        raise ValueError("fixed ratio score and total LL difference identity failed")
                record = {key: row[key] for key in ("competition", "match_id", "kickoff", "forecast_origin", "evaluation_block_start", "actual_home_goals", "actual_away_goals", "training_data_sha256")}
                record.update(selected_alpha=selection["alpha"], selection_n=selection["n"],
                              selection_kickoff_lower_inclusive=selection["kickoff_lower_inclusive"],
                              selection_kickoff_upper_inclusive=selection["kickoff_upper_inclusive"],
                              forecasts=forecasts, metrics=scored,
                              source_league_mean_expected_goals=row["forecasts"]["league_mean"]["expected_goals"])
                stream.write(json.dumps(record, ensure_ascii=False, allow_nan=False, separators=(",", ":"))+"\n")
                results.append({k: v for k, v in record.items() if k != "forecasts"})
        dump(output_dir/"selections.json", list(selections.values()))
        dump(output_dir/"excluded_outer_rows.json", excluded)
        by_competition = {c: aggregate([r for r in results if r["competition"] == c]) for c in groups}
        summary = {"schema_version": "f5_total_shrink_results_v1", "status": "historical_development_completed",
                   "n": len(results), "competitions": len(groups), "excluded_outer_n": len(excluded),
                   "independent_match_identities": len({r["match_id"] for r in results}),
                   "forecast_origins": len(selections), "primary": protocol["primary"],
                   "overall": aggregate(results), "by_competition": by_competition,
                   "selected_alpha_match_counts": dict(Counter(alpha_name(r["selected_alpha"]) for r in results)),
                   "insufficient_history_matches": sum(r["selection_n"] < 40 for r in results),
                   "selected_alpha_origin_counts": dict(Counter(alpha_name(s["alpha"]) for s in selections.values())),
                   "insufficient_history_origins": sum(s["n"] < 40 for s in selections.values()),
                   "fixed_ratio_score_total_ll_delta_identity_max_absolute_error": algebra_max_error,
                   "original_F4_baseline_metric_function_reused": True,
                   "equal_competition_mean_delta": {name: {key: math.fsum(v[name]["candidate_minus_alpha_0"][key] for v in by_competition.values())/len(by_competition) for key in results[0]["metrics"][BASELINE]} for name in results[0]["metrics"]},
                   "prospective_validation": False, "production_promotion": False,
                   "limitations": ["Already inspected development data; neither a clean holdout nor confirmation.",
                                   "48-hour selector maturity is a research assumption, not verified historical availability; inherited F4 base fitting used a 4-hour assumption.",
                                   "Only common outer rows select alpha; original F4 warmup rows are excluded. Insufficient selection history falls back to alpha zero.",
                                   "Adaptive and five fixed-alpha output versions do not multiply independent match count; team/time dependence remains.",
                                   "LLs for score/total/margin use untruncated rate-implied Poisson/Skellam, while 1X2/events use original normalized matrices.",
                                   "No price, executed stakes, independent GPT decision pairs or return-on-investment evidence; no production effect established."]}
        primary_delta = summary["overall"]["past_selected"]["candidate_minus_alpha_0"]
        screen = protocol["development_screen"]
        checks = {key: {"observed_delta": primary_delta[key], "maximum_allowed_delta": screen["max_mean_"+key+"_delta"],
                        "passed": primary_delta[key] <= screen["max_mean_"+key+"_delta"]}
                  for key in ("total_log_loss", "over_2_5_brier", "one_x_two_log_loss", "score_log_loss")}
        summary["development_screen"] = {"candidate": "past_selected", "conditions": checks,
                                         "all_passed": all(x["passed"] for x in checks.values()),
                                         "is_confirmation_or_production_promotion": False}
        summary["uncertainty"] = descriptive_intervals(results, protocol)
        dump(output_dir/"summary.json", summary)
        after = {name: digest(input_dir/name) for name in protocol["inputs"]}
        code_after = {name: digest(REPO/name) for name in code_before}
        if after != locked or code_after != code_before or protocol_path.read_bytes() != protocol_bytes:
            raise ValueError("input, protocol or source mutated during execution")
        output_hashes = {p.name: digest(p) for p in sorted(output_dir.iterdir()) if p.is_file()}
        dump(output_dir/"run_completed.json", {**provenance, "completed_at": stamp(), "success": True,
             "source_inputs_unchanged": True, "source_code_unchanged": True, "protocol_bytes_unchanged": True,
             "output_files_sha256": output_hashes, "n": len(results)})
        return summary
    except Exception as exc:
        dump(output_dir/"run_failed.json", {**provenance, "failed_at": stamp(), "success": False,
                                          "exception_type": type(exc).__name__, "message": str(exc)})
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", type=Path, required=True)
    parser.add_argument("--protocol", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    summary = run_experiment(args.input_dir.absolute(), args.protocol.absolute(), args.output_dir.absolute())
    print(json.dumps({"completed": True, "n": summary["n"], "production_promotion": False}, ensure_ascii=False))


if __name__ == "__main__":
    main()
