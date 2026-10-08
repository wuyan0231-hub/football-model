"""Synthetic functional/temporal checks; these are never efficacy evidence."""
import copy
import importlib.util
import math
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from football_model.model import _distribution_from_rates
from football_model.quality import summarize_distribution

spec = importlib.util.spec_from_file_location("f5_total_shrink", Path(__file__).resolve().parents[1]/"scripts/s2_f5_total_shrink.py")
f5 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(f5)


def forecast(home=3., away=1.):
    d = _distribution_from_rates(home, away)
    result = summarize_distribution(d["score_matrix"], d["expected_goals"])
    result.update(matrix_shape=[len(d["score_matrix"]), len(d["score_matrix"][0])],
                  tail_mass_before_normalization=d["tail_mass_before_normalization"])
    return result


def row(identity=1, kickoff="2026-01-01T00:00:00Z", origin="2025-12-31T00:00:00Z", goals=2):
    base = forecast()
    return {"competition": "test", "match_id": identity, "kickoff": kickoff, "forecast_origin": origin,
            "phase": "outer", "all_half_lives_common": True, "neutral": False,
            "actual_home_goals": goals, "actual_away_goals": 0, "training_data_sha256": "x",
            "forecasts": {"half_life_180": base, "half_life_90": base, "half_life_365": base,
                          "league_mean": forecast(1.,1.)}}


def history(n=40):
    start = datetime(2026,1,1,tzinfo=timezone.utc)
    return [row(i+1, (start+timedelta(hours=i)).isoformat()) for i in range(n)]


class TotalShrinkTests(unittest.TestCase):
    def test_hand_calculated_total_shrink_preserves_ratio(self):
        got = f5.shrink_rates({"home":3.,"away":1.}, {"home":1.,"away":1.}, .5)
        self.assertEqual(got, {"home":2.25,"away":.75})
        self.assertEqual(got["home"]/got["away"],3.)
        full = f5.shrink_rates({"home":3.,"away":1.}, {"home":1.,"away":1.}, 1.)
        self.assertEqual(full, {"home":1.5,"away":.5})
        self.assertNotEqual(full,{"home":1.,"away":1.})

    def test_alpha_zero_rates_and_saved_fields_exact(self):
        source = row()
        source["forecasts"]["half_life_180"]["probabilities"]["home_win"] += 1e-16
        before = copy.deepcopy(source)
        got = f5.complete_forecast(source,0)
        for key,value in source["forecasts"]["half_life_180"].items():
            self.assertEqual(got[key],value)
        self.assertEqual(source,before)

    def test_saved_baseline_wrong_shape_or_probability_rejected(self):
        for field in ("shape", "probability"):
            source = row()
            if field == "shape":
                source["forecasts"]["half_life_180"]["matrix_shape"]=[1,1]
            else:
                source["forecasts"]["half_life_180"]["probabilities"]["draw"]+=1e-5
            with self.assertRaises(ValueError):
                f5.complete_forecast(source,0)

    def test_distributions_sum_and_complete_axes(self):
        got = f5.complete_forecast(row(),.5)
        self.assertAlmostEqual(sum(got["total_goals_distribution"].values()),1.,places=14)
        self.assertAlmostEqual(sum(got["goal_margin_distribution"].values()),1.,places=14)
        self.assertEqual(len(got["total_goals_distribution"]),sum(got["matrix_shape"])-1)
        self.assertAlmostEqual(sum(map(sum,got["score_matrix"])),1.,places=14)

    def test_invalid_alphas_and_rates_rejected(self):
        for alpha in (True,None,"0.5",-.25,.3,1.25,float("nan")):
            with self.subTest(alpha=alpha),self.assertRaises(ValueError):
                f5.shrink_rates({"home":1.,"away":1.},{"home":1.,"away":1.},alpha)
        for invalid in (0,-1,float("inf"),float("nan"),True,"1"):
            with self.subTest(invalid=invalid),self.assertRaises(ValueError):
                f5.shrink_rates({"home":invalid,"away":1.},{"home":1.,"away":1.},0)
        with self.assertRaises(ValueError):
            f5.shrink_rates({"home":1.,"away":1.,"extra":1.},{"home":1.,"away":1.},0)

    def test_total_log_loss_manual(self):
        self.assertAlmostEqual(f5.total_log_loss(2.,2),2.-2*math.log(2)+math.log(2),places=15)
        self.assertEqual(f5.total_log_loss(2.,0),2.)
        for goal in (-1,True,2.1):
            with self.assertRaises(ValueError):
                f5.total_log_loss(2.,goal)

    def test_selector_exact_48h_and_180d_boundaries(self):
        origin = datetime(2026,7,1,tzinfo=timezone.utc)
        offsets = [timedelta(hours=48),timedelta(hours=48)-timedelta(microseconds=1),
                   timedelta(days=180),timedelta(days=180,microseconds=1)]
        source = [row(i+1,(origin-offset).isoformat(),(origin-timedelta(days=181)).isoformat()) for i,offset in enumerate(offsets)]
        choice = f5.select_alpha(source,origin.isoformat(),"test")
        self.assertEqual(choice["eligible_match_ids"],[3,1])
        self.assertEqual(choice["latest_assumed_result_available_at"],origin.isoformat())

    def test_minimum_40_and_loss_selection(self):
        source = history()
        self.assertEqual(f5.select_alpha(source[:-1],"2026-02-01T00:00:00Z","test")["alpha"],0.)
        chosen = f5.select_alpha(source,"2026-02-01T00:00:00Z","test")
        self.assertEqual(chosen["n"],40)
        self.assertEqual(chosen["alpha"],1.)

    def test_ties_and_numerical_tolerance_choose_smallest_alpha(self):
        source=history()
        for r in source:
            r["forecasts"]["league_mean"] = copy.deepcopy(r["forecasts"]["half_life_180"])
        self.assertEqual(f5.select_alpha(source,"2026-02-01T00:00:00Z","test")["alpha"],0.)
        with patch.object(f5,"total_log_loss",side_effect=[1.0]*40+[1.-5e-13]*160):
            self.assertEqual(f5.select_alpha(source,"2026-02-01T00:00:00Z","test")["alpha"],0.)

    def test_future_or_other_scope_cannot_affect_past_choice(self):
        source=history()
        before=f5.select_alpha(source,"2026-02-01T00:00:00Z","test")
        future=row(100,"2026-02-10T00:00:00Z","2026-02-09T00:00:00Z",50)
        other=row(101);other["competition"]="other"
        warmup=row(102);warmup["phase"]="inner_warmup"
        not_common=row(103);not_common["all_half_lives_common"]=False
        same_origin=row(104);same_origin["forecast_origin"]="2026-02-01T00:00:00Z"
        after=f5.select_alpha(source+[future,other,warmup,not_common,same_origin],"2026-02-01T00:00:00Z","test")
        self.assertEqual(before,after)

    def test_duplicate_match_never_adds_independent_weight(self):
        source=history()
        with self.assertRaises(ValueError):
            f5.select_alpha(source+[copy.deepcopy(source[0])],"2026-02-01T00:00:00Z","test")

    def test_fixed_ratio_score_total_loss_delta_identity(self):
        source=row(goals=5)
        baseline=f5.metrics(source,f5.complete_forecast(source,0))
        for alpha in f5.ALPHAS:
            m=f5.metrics(source,f5.complete_forecast(source,alpha))
            self.assertAlmostEqual(m["score_log_loss"]-baseline["score_log_loss"],
                                   m["total_log_loss"]-baseline["total_log_loss"],places=13)

    def test_same_window_mean_and_origin_binding(self):
        source=row()
        window={"origin":source["forecast_origin"],"end":"2026-01-07T00:00:00Z","phase":"outer",
                "training_data_sha256":"x","test_match_ids":[1],"last_result_assumed_available_at":"2025-12-30T00:00:00Z",
                "league_mean_rates":{"home":1.,"away":1.},
                "model_parameters":{"half_life_180":{"training_cutoff":source["forecast_origin"],"training_data_sha256":"x"}}}
        self.assertEqual(f5.validate_row(source,"test",window)["match_id"],1)
        for key,value in (("training_data_sha256","wrong"),("origin","2025-12-30T00:00:00Z"),("league_mean_rates",{"home":2.,"away":1.})):
            changed=copy.deepcopy(window);changed[key]=value
            with self.assertRaises(ValueError):
                f5.validate_row(source,"test",changed)

    def test_invalid_outcomes_and_timezone_rejected(self):
        for value in ("2026-01-01", "2026-01-01T01:00:00", 1):
            with self.assertRaises((ValueError,TypeError)):
                f5.timestamp(value)

    def test_duplicate_json_keys_and_nonfinite_constants_rejected(self):
        for value in ('{"a":1,"a":2}', '{"a":NaN}', '{"a":Infinity}', '{"a":-Infinity}', '{"a":1e999}'):
            with self.assertRaises(ValueError):
                f5.strict_json(value)

    def test_bad_saved_forecast_fields_rejected(self):
        for invalid in (True,float("nan"),float("inf"),-.1,1.1):
            bad=forecast();bad["probabilities"]["draw"]=invalid
            with self.assertRaises(ValueError):
                f5.validate_forecast(bad)
        bad=forecast();bad["expected_goals"]["home"]=True
        with self.assertRaises(ValueError):
            f5.validate_forecast(bad)
        bad=forecast();bad["events"]["unexpected"]=.1
        with self.assertRaises(ValueError):
            f5.validate_forecast(bad)

    def test_protocol_requires_all_locks_and_fixed_scope(self):
        protocol_path=Path(__file__).resolve().parents[1]/"docs/research/s2-f5-total-shrink-protocol-v1.json"
        # Protocol tests use the real frozen recipe only, never historical rows.
        protocol=f5.strict_json(protocol_path.read_bytes())
        f5.validate_protocol(protocol)
        for mode in ("lock","guard","bootstrap","competitions"):
            bad=copy.deepcopy(protocol)
            if mode=="lock":del bad["inputs"]["epl_protocol.json"]
            if mode=="guard":bad["development_screen"]["max_mean_over_2_5_brier_delta"]=False
            if mode=="bootstrap":bad["uncertainty"]["repetitions"]=2000.0
            if mode=="competitions":bad["competitions"].reverse()
            with self.assertRaises(ValueError):
                f5.validate_protocol(bad)

    def test_existing_output_is_not_overwritten(self):
        with self.assertRaises(FileExistsError):
            f5.run_experiment(Path("does-not-exist"),Path("does-not-exist"),Path(__file__).parent)


if __name__ == "__main__":
    unittest.main()
