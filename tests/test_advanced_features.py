import copy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest

from football_model.advanced_features import (feature_values, build_feature_rows,
    make_players_snapshot, validate_players_snapshot, validate_evidence, FEATURE_GROUPS)
from football_model.advanced_collection import collect_run, _save
from football_model.data import DataError, RequestBudgetError
from football_model.fundamentals import make_snapshot, save_snapshot


def fixture(mid, day, *, status="FT", team=1):
    return {"match_id":mid,"kickoff":f"2026-01-{day:02d}T12:00:00Z",
        "home_id":team,"away_id":team+1,"status":status}


def api_fixture(f):
    return {"fixture":{"id":f["match_id"],"date":f["kickoff"],"status":{"short":f["status"]}},
        "teams":{"home":{"id":f["home_id"],"name":"A"},"away":{"id":f["away_id"],"name":"B"}},
        "league":{"id":39,"season":2025},"score":{"fulltime":{"home":1,"away":0}}}


def envelope(kind,f,*,observed=None,shift=0,xg=1.2,shots=10,minute=90,rating="7.0"):
    observed=observed or f["kickoff"].replace("12:00","18:00")
    rows=[]
    for tid in (f["home_id"],f["away_id"]):
        base=tid*100
        if kind=="statistics":
            data={"statistics":[{"type":"expected_goals","value":xg},
                {"type":"Total Shots","value":shots},{"type":"Shots on Goal","value":3}]}
        elif kind=="lineups":
            data={"startXI":[{"player":{"id":base+p+shift,"name":str(p)}} for p in range(1,12)],"substitutes":[]}
        else:
            data={"players":[{"player":{"id":base+p},"statistics":[{"games":{"minutes":minute,"rating":rating}}]} for p in range(1,12)]}
        rows.append({"team":{"id":tid},**data})
    return {"endpoint":"/fixtures/"+kind,"params":{"fixture":f["match_id"]},"response":rows,"fetched_at":observed}


class AdvancedFeaturesTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup); self.root=Path(self.tmp.name)
        self.history=[fixture(i,i*2) for i in range(1,5)]
        (self.root/"data").mkdir()
        (self.root/"data/x.json").write_text(json.dumps({"matches":self.history,"observed_at":"2026-01-09T00:00:00Z"}))
        self.target=fixture(9,12,status="NS")
        for f in self.history:
            for kind in ("statistics","lineups"):
                self.save(kind,f)
            snap=make_players_snapshot(envelope("players",f),f)
            _save(self.root,Path("advanced_features/snapshots/players")/str(f["match_id"]),snap)

    def save(self,kind,f,**kwargs):
        snap=make_snapshot(kind,envelope(kind,f,**kwargs),f)
        return save_snapshot(self.root/"fundamentals/snapshots",snap)

    def values(self,**kwargs):
        return feature_values(self.root,"x",self.target,"2026-01-12T11:00:00Z",**kwargs)

    def test_actual_zero_xg_distinct_from_missing(self):
        for path in (self.root/"fundamentals/snapshots/statistics").glob("*/*.json"): path.unlink()
        for f in self.history: self.save("statistics",f,xg=0)
        self.assertEqual(self.values()["home"]["xg_for_mean"],0)
        for path in (self.root/"fundamentals/snapshots/statistics").glob("*/*.json"): path.unlink()
        for f in self.history: self.save("statistics",f,xg=None)
        result=self.values()
        self.assertIsNone(result["home"]["xg_for_mean"])
        self.assertTrue(result["groups"]["opportunity_shots"])
        self.assertFalse(result["groups"]["opportunity_xg"])

    def test_target_stats_and_target_lineup_never_used_early(self):
        expected=self.values()["home"]
        target_ft={**self.target,"status":"FT"}
        self.save("statistics",target_ft,observed="2026-01-12T10:00:00Z",shots=999,xg=999)
        self.save("lineups",self.target,observed="2026-01-12T10:00:00Z",shift=20)
        result=self.values()
        self.assertEqual(result["home"],expected)
        self.assertIsNone(result["home"]["confirmed_xi_overlap"])
        self.assertFalse(any("/9/" in ref["path"] for ref in result["source_refs"]))

    def test_actual_future_observation_not_admitted(self):
        for path in (self.root/"fundamentals/snapshots/statistics").glob("*/*.json"): path.unlink()
        for f in self.history: self.save("statistics",f,observed="2026-02-01T00:00:00Z")
        self.assertIsNone(self.values()["home"]["shots_for_mean"])
        historic=self.values(mode="historical")
        self.assertEqual(historic["home"]["shots_for_mean"],10)
        self.assertFalse(historic["prospective_qualified"])

    def test_future_history_index_cannot_backdate_source_universe(self):
        path=self.root/"data/x.json";data=json.loads(path.read_text());data["observed_at"]="2026-01-13T00:00:00Z"
        path.write_text(json.dumps(data))
        with self.assertRaisesRegex(DataError,"history_not_observed"):
            self.values()

    def test_lineup_requires_actual_ns_snapshot_before_cutoff(self):
        self.save("lineups",{**self.target,"status":"FT"},observed="2026-01-12T10:30:00Z")
        self.assertIsNone(self.values(horizon="LINEUP")["home"]["confirmed_xi_overlap"])
        self.save("lineups",self.target,observed="2026-01-12T10:40:00Z")
        result=self.values(horizon="LINEUP")
        self.assertEqual(result["home"]["confirmed_xi_overlap"],1)
        self.assertEqual(result["home"]["confirmed_xi_prior_minutes_share"],1)
        self.assertEqual(result["home"]["confirmed_xi_prior_rating_delta"],0)

    def test_unseen_current_player_rating_is_missing_not_zero(self):
        self.save("lineups",self.target,observed="2026-01-12T10:40:00Z",shift=1)
        result=self.values(horizon="LINEUP")
        self.assertAlmostEqual(result["home"]["confirmed_xi_overlap"],10/11)
        self.assertIsNone(result["home"]["confirmed_xi_prior_rating_delta"])

    def test_player_rating_delta_uses_only_past_replacement_contribution(self):
        for path in (self.root/"advanced_features/snapshots/players").glob("*/*.json"):path.unlink()
        for f in self.history:
            env=envelope("players",f)
            for team in env["response"]:
                team["players"][-1]["statistics"][0]["games"]["minutes"]=45
                team["players"].append({"player":{"id":team["team"]["id"]*100+12},
                    "statistics":[{"games":{"minutes":45,"rating":"9.0"}}]})
            snap=make_players_snapshot(env,f)
            _save(self.root,Path("advanced_features/snapshots/players")/str(f["match_id"]),snap)
        current=envelope("lineups",self.target,observed="2026-01-12T10:40:00Z")
        for team in current["response"]:team["startXI"][-1]["player"]["id"]+=1
        save_snapshot(self.root/"fundamentals/snapshots",make_snapshot("lineups",current,self.target))
        result=self.values(horizon="LINEUP")
        self.assertAlmostEqual(result["home"]["confirmed_xi_prior_rating_delta"],2/11)
        self.assertTrue(result["groups"]["lineup_rating"])

    def test_impossible_player_minutes_quarantined(self):
        f=self.history[0];env=envelope("players",f)
        env["response"][0]["players"].append({"player":{"id":112},"statistics":[{"games":{"minutes":90}}]})
        with self.assertRaisesRegex(DataError,"team_total"):
            make_players_snapshot(env,f)

    def test_unknown_bench_minutes_dont_erase_observed_starters_rating_or_xi(self):
        for path in (self.root/"advanced_features/snapshots/players").glob("*/*.json"):path.unlink()
        for f in self.history:
            env=envelope("players",f)
            for team in env["response"]:
                team["players"].append({"player":{"id":team["team"]["id"]*100+12},"statistics":[{"games":{"minutes":None,"rating":None}}]})
            snap=make_players_snapshot(env,f)
            _save(self.root,Path("advanced_features/snapshots/players")/str(f["match_id"]),snap)
        self.save("lineups",self.target,observed="2026-01-12T10:40:00Z")
        result=self.values(horizon="LINEUP")
        self.assertTrue(result["groups"]["lineup_confirmed"])
        self.assertTrue(result["groups"]["lineup_rating"])
        self.assertFalse(result["groups"]["lineup_minutes"])
        self.assertIsNone(result["home"]["confirmed_xi_prior_minutes_share"])

    def test_minutes_null_not_interpreted_as_nonparticipation(self):
        f=self.history[0]; snap=make_players_snapshot(envelope("players",f,minute=None),f)
        self.assertFalse(snap["teams"]["1"]["complete_minutes"])
        self.assertIsNone(snap["teams"]["1"]["players"][0]["minutes"])
        altered=copy.deepcopy(snap); altered["teams"]["1"]["players"][0]["minutes"]=0
        with self.assertRaises(DataError):validate_players_snapshot(altered)

    def test_all_requested_rows_preserved_even_no_features(self):
        result=build_feature_rows(self.root,"x",self.history)
        self.assertEqual(len(result["rows"]),4)
        self.assertFalse(result["rows"][0]["groups"]["opportunity_shots"])
        self.assertTrue(result["coverage"]["denominator_preserved"])

    def test_source_closure_reconstructs_values(self):
        self.save("lineups",self.target,observed="2026-01-12T10:40:00Z")
        result=self.values(horizon="LINEUP")
        with tempfile.TemporaryDirectory() as dest:
            for ref in result["source_refs"]:
                target=Path(dest)/ref["path"];target.parent.mkdir(parents=True,exist_ok=True)
                target.write_bytes((self.root/ref["path"]).read_bytes())
            rebuilt=feature_values(dest,"x",self.target,result["cutoff_at"],horizon="LINEUP")
            self.assertEqual(rebuilt["home"],result["home"])
            self.assertEqual(rebuilt["away"],result["away"])
            self.assertEqual(rebuilt["source_refs"],result["source_refs"])


class FakeClient:
    def __init__(self,fixtures,budget=100):self.fixtures={f["match_id"]:f for f in fixtures};self.requests_made=0;self.budget=budget;self.order=[]
    def status(self):return {}
    def budget_snapshot(self):return {"configured_daily_limit":5000,"daily_remaining":self.budget}
    def resolve_league(self,c,force=False):
        return {"league":{"id":39},"seasons":[{"year":2025,"coverage":{"fixtures":{"lineups":True,"statistics_fixtures":True,"statistics_players":True}}}],"fetched_at":"2026-02-01T00:00:00Z"}
    def fetch_fixture(self,mid,**kwargs):
        return {"endpoint":"/fixtures","params":{"id":mid},"response":[api_fixture(self.fixtures[mid])],"fetched_at":"2026-02-01T00:00:00Z"}
    def _cached_request(self,endpoint,params,**kwargs):
        if self.requests_made>=self.budget:raise RequestBudgetError("budget")
        self.requests_made+=1;self.order.append(params["fixture"])
        return envelope(endpoint.rsplit("/",1)[-1],self.fixtures[params["fixture"]],observed="2026-02-01T00:00:00Z")


class AdvancedCollectionTests(unittest.TestCase):
    def test_resume_keeps_denominator_and_rotates_leagues(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/"data").mkdir()
            fixtures=[fixture(1,2),fixture(2,4),fixture(3,6,team=3),fixture(4,8,team=3)]
            for key,rows in (("a",fixtures[:2]),("b",fixtures[2:])):
                (root/"data"/(key+".json")).write_text(json.dumps({"matches":rows}))
            config={"competitions":[{"key":"a"},{"key":"b"}]}
            clock=lambda:datetime(2026,2,2,tzinfo=timezone.utc)
            client=FakeClient(fixtures,budget=3)
            code,report=collect_run(config,root,client=client,clock=clock)
            self.assertEqual(code,0);self.assertEqual(len(report["matches"]),4)
            self.assertEqual(report["pending_requests"],9)
            self.assertEqual(report["deferred_requests"],9)
            self.assertEqual(report["engineering_failure_count"],0)
            self.assertEqual(report["execution_status"],"deferred")
            self.assertFalse(report["full_collection_completed"])
            self.assertTrue(all(l["denominator_preserved"] for l in report["leagues"]))
            next_client=FakeClient(fixtures)
            code,finished=collect_run(config,root,client=next_client,clock=clock)
            self.assertEqual(code,0);self.assertEqual(next_client.requests_made,9)
            self.assertEqual(finished["pending_requests"],0)
            self.assertTrue(finished["full_collection_completed"])
            self.assertEqual(finished["execution_status"],"complete")
            self.assertEqual(json.loads((root/"advanced_features/collection/by_selection/all/latest.json").read_text()),finished)
            value=next((root/"advanced_features/evidence/statistics").glob("*/*.json"))
            raw=json.loads(value.read_text());raw["snapshot"]["fixture"]["status"]="NS"
            with self.assertRaises(DataError):validate_evidence(raw,root)

    def test_missing_league_history_never_reports_zero_coverage(self):
        with tempfile.TemporaryDirectory() as root:
            code,report=collect_run({"competitions":[{"key":"absent"}]},root)
            self.assertEqual(code,1)
            self.assertEqual(report["engineering_failure_count"],1)
            self.assertEqual(report["execution_status"],"failed")
            self.assertEqual(report["status"],"partial")
            self.assertIsNone(report["leagues"][0]["requested_matches"])

    def test_mixed_schema_failure_and_budget_deferral_remains_failure(self):
        class BrokenClient(FakeClient):
            def _cached_request(self,endpoint,params,**kwargs):
                result=super()._cached_request(endpoint,params,**kwargs)
                result["params"]={"fixture":999999}
                return result
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/"data").mkdir()
            rows=[fixture(1,2),fixture(2,4)]
            (root/"data/a.json").write_text(json.dumps({"matches":rows}))
            code,report=collect_run({"competitions":[{"key":"a"}]},root,league="a",
                client=BrokenClient(rows,budget=1),clock=lambda:datetime(2026,2,2,tzinfo=timezone.utc))
            self.assertEqual(code,1)
            self.assertEqual(report["failed_requests"],1)
            self.assertEqual(report["deferred_requests"],5)
            self.assertEqual(report["engineering_failure_count"],1)
            self.assertEqual(report["execution_status"],"failed")
            self.assertTrue(report["matches"][0]["kinds"]["statistics"]["diagnostic_ref"])
            self.assertEqual(json.loads((root/"advanced_features/collection/by_selection/a/latest.json").read_text()),report)

    def test_account_initialization_failure_is_not_healthy_quota_deferral(self):
        class BrokenStatus(FakeClient):
            def status(self):raise DataError("invalid_account_response")
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/"data").mkdir();rows=[fixture(1,2)]
            (root/"data/a.json").write_text(json.dumps({"matches":rows}))
            code,report=collect_run({"competitions":[{"key":"a"}]},root,client=BrokenStatus(rows),
                clock=lambda:datetime(2026,2,2,tzinfo=timezone.utc))
            self.assertEqual(code,1)
            self.assertEqual(report["engineering_failure_count"],1)
            self.assertEqual(report["execution_status"],"failed")
            self.assertEqual(report["deferred_requests"],3)

    def test_unknown_actual_daily_balance_cannot_be_reported_normal_deferral(self):
        class UnknownQuota(FakeClient):
            def budget_snapshot(self):return {"configured_daily_limit":5000,"daily_quota_status":"unknown","daily_remaining":None}
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/"data").mkdir();rows=[fixture(1,2)]
            (root/"data/a.json").write_text(json.dumps({"matches":rows}))
            client=UnknownQuota(rows)
            code,report=collect_run({"competitions":[{"key":"a"}]},root,client=client,
                clock=lambda:datetime(2026,2,2,tzinfo=timezone.utc))
            self.assertEqual(code,1);self.assertEqual(client.requests_made,0)
            self.assertEqual(report["engineering_failure_count"],1)


if __name__=="__main__":unittest.main()
