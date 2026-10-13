import copy
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import unittest

from football_model.advanced_player_load import collect_run, load_sources, load_player_load_sources, feature_values, SCOPE
from football_model.data import DataError, RequestBudgetError
from football_model.fundamentals import make_snapshot, save_snapshot

NOW = "2026-01-15T00:00:00Z"
CONFIG = {"api_daily_limit":5000,"competitions":[{"key":"unl","mode":"national_baseline","name":"UEFA Nations League","country":"World"}]}


def raw(mid, day, status="FT"):
    teams=[]
    for tid, first in ((100, 101), (200, 201)):
        players=[{"player":{"id":first+i},"statistics":[{"games":{"minutes":90,"rating":None}}]} for i in range(11)]
        teams.append({"team":{"id":tid},"players":players})
    return {"fixture":{"id":mid,"date":f"2026-01-{day:02d}T12:00:00Z","status":{"short":status}},
        "teams":{"home":{"id":100,"name":"A national"},"away":{"id":200,"name":"B national"}},
        "league":{"id":5,"season":2026},"players":teams}


def identity(value):
    return {"match_id":value["fixture"]["id"],"kickoff":value["fixture"]["date"],
            "home_id":100,"away_id":200}


def save(path, data):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(data))


class Client:
    def __init__(self, rows, *, observation=NOW, failures=()):
        self.rows=rows;self.observation=observation;self.failures=set(failures)
        self.requests_made=0;self.reservation=None;self.calls=[]
    def status(self):return {}
    def budget_snapshot(self):return {"configured_daily_limit":5000,"daily_quota_status":"known","daily_remaining":4900}
    @contextmanager
    def reserve_requests(self,count):
        self.reservation=count
        yield
    def _cached_request(self, endpoint, params, **kwargs):
        self.requests_made+=1;self.calls.append((endpoint,params))
        return {"endpoint":endpoint,"params":params,"response":[{"league":{"id":5,"name":"UEFA Nations League"},
            "country":{"name":"World"},"seasons":[{"year":2026}]}],"fetched_at":self.observation}
    def fetch_fixtures(self,lid,year,**kwargs):
        self.requests_made+=1
        return {"endpoint":"/fixtures","params":{"league":lid,"season":year},"fetched_at":self.observation,
            "response":copy.deepcopy(self.rows),"paging":{"current":1,"total":1}}
    def fetch_fixture(self,mid,**kwargs):
        if mid in self.failures:raise RequestBudgetError("deferred")
        self.requests_made+=1
        return {"endpoint":"/fixtures","params":{"id":mid},"fetched_at":self.observation,
            "response":[copy.deepcopy(next(r for r in self.rows if r["fixture"]["id"]==mid))]}


class PlayerLoadTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.nationals=[raw(50,10),raw(51,12)]
        save(self.root/"data/unl.json",{"matches":[identity(r) for r in self.nationals],"requested_seasons":[2026],"observed_at":NOW})
        self.previous={"match_id":2,"home_id":1,"away_id":2,"kickoff":"2026-01-08T12:00:00Z","status":"FT",
            "home_name":"A club","away_name":"B club","home_goals":1,"away_goals":0}
        self.target={"match_id":3,"home_id":1,"away_id":2,"kickoff":"2026-01-16T12:00:00Z","status":"NS"}
        save(self.root/"data/club.json",{"matches":[self.previous],"observed_at":NOW})
        self.lineup(self.previous,"2026-01-08T18:00:00Z")
        self.client=Client(self.nationals)
    def lineup(self,fixture,observed,shift=0):
        teams=[]
        for tid in (1,2):
            teams.append({"team":{"id":tid},"startXI":[{"player":{"id":100*tid+i+shift,"name":"p"}} for i in range(1,12)],"substitutes":[]})
        snap=make_snapshot("lineups",{"endpoint":"/fixtures/lineups","params":{"fixture":fixture["match_id"]},
            "response":teams,"fetched_at":observed},fixture)
        save_snapshot(self.root/"fundamentals/snapshots",snap)
    def collect(self,client=None):
        return collect_run(CONFIG,self.root,client=client or self.client,
            clock=lambda:datetime.fromisoformat(NOW.replace("Z","+00:00")))
    def values(self,**kwargs):
        return feature_values(self.root,"club",self.target,"2026-01-16T02:00:00Z",**kwargs)
    def test_scoped_numeric_load_and_coverage_are_separate(self):
        code,report=self.collect();self.assertEqual(code,0)
        self.assertTrue(report["full_collection_completed"])
        self.assertEqual(self.client.reservation,250)
        value=self.values()
        self.assertEqual(value["scope"],SCOPE)
        self.assertFalse(value["scope_is_all_international_matches"])
        self.assertEqual(value["home"]["restricted_observed_national_minutes_per_matched_xi"],180)
        self.assertEqual(value["home"]["restricted_observed_national_xi_match_count"],11)
        self.assertEqual(value["home"]["restricted_observed_national_xi_match_fraction"],1)
        self.assertTrue(value["groups"]["restricted_observed_national_load"])
    def test_core_features_freeze_and_rebuild_national_source_closure(self):
        from football_model.advanced_features import feature_values as core_features
        self.collect()
        value=core_features(self.root,"club",self.target,"2026-01-16T02:00:00Z",mode="prospective")
        self.assertTrue(value["groups"]["restricted_observed_national_load"])
        self.assertEqual(value["home"]["restricted_observed_national_minutes_per_matched_xi"],180)
        with tempfile.TemporaryDirectory() as dest:
            root=Path(dest)
            for ref in value["source_refs"]:
                path=root/ref["path"];path.parent.mkdir(parents=True,exist_ok=True)
                shutil.copyfile(self.root/ref["path"],path)
                self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(),ref["sha256"])
            rebuilt=core_features(root,"club",self.target,value["cutoff_at"],mode="prospective")
            self.assertEqual(rebuilt,value)
    def test_core_optional_corruption_keeps_club_features_and_separates_diagnostics(self):
        from football_model.advanced_snapshots import _features, _refs_in
        self.collect()
        cutoff=datetime(2026,1,16,2,tzinfo=timezone.utc)
        before=_features(self.root,"club",self.target,cutoff,"EARLY",{})
        sources=load_sources(self.root)
        path=next(self.root/r["path"] for r in sources["source_refs"] if json.loads((self.root/r["path"]).read_text()).get("params")=={"id":50})
        path.write_text('{}')
        after=_features(self.root,"club",self.target,cutoff,"EARLY",{})
        self.assertFalse(after["groups"]["restricted_observed_national_load"])
        for side in ("home","away"):
            for name,value in before[side].items():
                if "restricted_observed_national" not in name:self.assertEqual(after[side][name],value)
        self.assertTrue(after["optional_diagnostic_refs"])
        self.assertIn({"observed_path":str(path.relative_to(self.root)),
            "observed_sha256":hashlib.sha256(b'{}').hexdigest()},after["optional_diagnostic_refs"])
        self.assertFalse(after["optional_diagnostic_bytes_frozen"])
        self.assertNotIn(str(path.relative_to(self.root)),[r["path"] for r in after["source_refs"]])
        self.assertEqual(after["home"]["rest_days_capped"],8)
        # Offset training bundles retain entire feature datasets. Rejected
        # diagnostics must remain outside generic recursive dependency closure.
        bundle={"feature_dataset":{"rows":[after]}}
        self.assertNotIn(str(path.relative_to(self.root)),[r["path"] for r in _refs_in(bundle)])
        self._assert_snapshot_rebuild(after,cutoff,bundle)
    def _assert_snapshot_rebuild(self,value,cutoff,bundle=None):
        from football_model.advanced_snapshots import _features,_feature_numeric,_refs_in,freeze_inputs,frozen_runtime
        refs=list(_refs_in(bundle)) if bundle else value["source_refs"]
        frozen=freeze_inputs(self.root,self.root/"freeze-test",refs,cutoff)
        with frozen_runtime(self.root,frozen,cutoff) as runtime:
            rebuilt=_features(runtime,"club",self.target,cutoff,"EARLY",{})
            self.assertEqual(_feature_numeric(rebuilt),_feature_numeric(value))
    def _assert_history_time_degrades_only_national(self,observed):
        from football_model.advanced_snapshots import _features
        path=self.root/"data/unl.json";history=json.loads(path.read_text())
        if observed is None:history.pop("observed_at")
        else:history["observed_at"]=observed
        save(path,history);self.collect()
        cutoff=datetime(2026,1,16,2,tzinfo=timezone.utc)
        value=_features(self.root,"club",self.target,cutoff,"EARLY",{})
        self.assertFalse(value["groups"]["restricted_observed_national_load"])
        self.assertEqual(value["home"]["rest_days_capped"],8)
        self.assertTrue(value["optional_diagnostic_refs"])
        self.assertFalse(any("player_load" in ref["path"] for ref in value["source_refs"]))
        self._assert_snapshot_rebuild(value,cutoff)
    def test_core_preloaded_future_national_history_is_locally_rejected(self):
        self._assert_history_time_degrades_only_national("2026-01-17T00:00:00Z")
    def test_core_preloaded_unknown_national_history_time_is_locally_rejected(self):
        self._assert_history_time_degrades_only_national(None)
    def test_core_preloaded_other_original_timestamp_is_locally_rejected(self):
        from football_model.advanced_snapshots import _features
        class LateMetadata(Client):
            def _cached_request(self,*args,**kwargs):
                value=super()._cached_request(*args,**kwargs)
                value["created_at"]="2026-01-17T00:00:00Z"
                return value
        self.collect(LateMetadata(self.nationals))
        cutoff=datetime(2026,1,16,2,tzinfo=timezone.utc)
        value=_features(self.root,"club",self.target,cutoff,"EARLY",{})
        self.assertFalse(value["groups"]["restricted_observed_national_load"])
        self.assertEqual(value["home"]["rest_days_capped"],8)
        self._assert_snapshot_rebuild(value,cutoff)
    def test_preloaded_source_hash_is_rechecked_before_numeric_admission(self):
        self.collect();sources=load_sources(self.root)
        path=next(self.root/r["path"] for r in sources["source_refs"] if json.loads((self.root/r["path"]).read_text()).get("params")=={"id":50})
        path.write_text('{}')
        value=self.values(sources=sources)
        self.assertFalse(value["groups"]["restricted_observed_national_load"])
        self.assertIn({"path":str(path.relative_to(self.root)),"sha256":hashlib.sha256(b'{}').hexdigest()},value["diagnostic_refs"])
    def test_unknown_xi_players_are_not_zero_minute_observations(self):
        self.lineup(self.previous,"2026-01-09T18:00:00Z",shift=10)
        self.collect();value=self.values()
        self.assertEqual(value["home"]["restricted_observed_national_minutes_per_matched_xi"],180)
        self.assertEqual(value["home"]["restricted_observed_national_xi_match_count"],1)
        self.assertAlmostEqual(value["home"]["restricted_observed_national_xi_match_fraction"],1/11)
        self.assertEqual(len(value["evidence"]["1"]["unobserved_xi_player_ids"]),10)
        self.assertIsNone(value["evidence"]["1"]["unobserved_player_load"])
    def test_no_matching_appearance_does_not_create_zero_feature(self):
        self.lineup(self.previous,"2026-01-09T18:00:00Z",shift=30)
        self.collect();value=self.values()
        self.assertTrue(all(v is None for v in value["home"].values()))
        self.assertEqual(value["evidence"]["1"]["status"],"no_observed_matching_appearance_not_zero_load")
    def test_unknown_matched_minutes_reject_branch_without_erasing_evidence(self):
        self.nationals[0]["players"][0]["players"][0]["statistics"][0]["games"]["minutes"]=None
        self.collect();value=self.values()
        self.assertIsNone(value["home"]["restricted_observed_national_minutes_per_matched_xi"])
        self.assertIn(101,value["evidence"]["1"]["unknown_minutes_player_ids"])
    def test_missing_detail_is_executable_pending_and_null_then_resume(self):
        code,report=self.collect(Client(self.nationals,failures={50}))
        self.assertEqual(code,0);self.assertEqual(report["execution_status"],"deferred")
        self.assertIn({"endpoint":"/fixtures","params":{"id":50},"reason":"actual_budget_or_runtime_deferred"},report["pending_requests"])
        value=self.values()
        self.assertEqual(value["missing_detail_fixture_ids"],[50])
        self.assertFalse(value["groups"]["restricted_observed_national_load"])
        self.collect();source=load_sources(self.root)
        # A real subsequent clock normally orders manifests. Supply selected
        # valid source here because the fake test clock is deliberately fixed.
        if len(source["snapshots"])!=2:
            old=[p for p in (self.root/"advanced_features/player_load/manifests").glob("*.json") if json.loads(p.read_text())["pending_requests"]]
            for p in old:p.unlink()
        self.assertTrue(self.values()["groups"]["restricted_observed_national_load"])
    def test_partial_schedule_page_cannot_claim_complete_denominator(self):
        class Partial(Client):
            def fetch_fixtures(self,*args,**kwargs):
                value=super().fetch_fixtures(*args,**kwargs);value["paging"]["total"]=2;return value
        code,report=self.collect(Partial(self.nationals))
        self.assertEqual(code,1);self.assertFalse(report["provider_schedule_queries_complete"])
        self.assertFalse(self.values()["groups"]["restricted_observed_national_load"])
    def test_later_downloads_historical_only_not_early_prospective(self):
        self.collect()
        cutoff="2026-01-14T02:00:00Z"
        historic=feature_values(self.root,"club",self.target,cutoff,mode="historical")
        self.assertTrue(historic["groups"]["restricted_observed_national_load"])
        self.assertFalse(historic["prospective_qualified"])
        # Club history too was downloaded later: prospective gate must reject.
        with self.assertRaisesRegex(DataError,"club_history_after_cutoff"):
            feature_values(self.root,"club",self.target,cutoff)
        self.assertFalse(load_sources(self.root,mode="prospective",cutoff_at=cutoff)["complete_schedule"])
    def test_lineup_horizon_uses_current_xi_early_cannot_see_it(self):
        self.collect();before=self.values()["home"]
        self.lineup(self.target,"2026-01-16T01:00:00Z",shift=10)
        self.assertEqual(self.values()["home"],before)
        value=self.values(horizon="LINEUP")
        self.assertEqual(value["home"]["restricted_observed_national_xi_match_count"],1)
        self.assertEqual(value["evidence"]["1"]["xi_basis"],"current_confirmed")
    def test_future_fixture_results_excluded_even_in_historical_mode(self):
        self.nationals.append(raw(52,17));self.collect()
        self.assertEqual(self.values(mode="historical")["window_fixture_ids"],[50,51])
    def test_et_minutes_are_real_load_without_changing_regulation_target(self):
        self.nationals[0]["fixture"]["status"]["short"]="AET"
        for team in self.nationals[0]["players"]:
            for p in team["players"]:p["statistics"][0]["games"]["minutes"]=120
        self.collect();value=self.values()
        self.assertEqual(value["home"]["restricted_observed_national_minutes_per_matched_xi"],210)
        self.assertTrue(value["minutes_include_extra_time_when_status_AET_or_PEN"])
    def test_exact_source_closure_reconstructs(self):
        self.collect();value=self.values()
        with tempfile.TemporaryDirectory() as dest:
            root=Path(dest)
            for ref in value["source_refs"]:
                path=root/ref["path"];path.parent.mkdir(parents=True,exist_ok=True)
                shutil.copyfile(self.root/ref["path"],path)
                self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(),ref["sha256"])
            rebuilt=feature_values(root,"club",self.target,value["cutoff_at"])
            self.assertEqual(rebuilt,value)
    def test_changed_detail_source_fails_hash_validation(self):
        self.collect();sources=load_sources(self.root)
        path=next(self.root/r["path"] for r in sources["source_refs"] if json.loads((self.root/r["path"]).read_text()).get("params")=={"id":50})
        value=json.loads(path.read_text());value["response"][0]["players"][0]["players"][0]["statistics"][0]["games"]["minutes"]=1
        path.write_text(json.dumps(value))
        with self.assertRaisesRegex(DataError,"source_hash_mismatch"):load_sources(self.root)
    def test_production_history_update_cannot_invalidate_frozen_sources(self):
        original=(self.root/"data/unl.json").read_bytes()
        _,report=self.collect()
        saved=self.root/report["history_ref"]["path"]
        self.assertEqual(saved.read_bytes(),original)
        self.assertNotEqual(report["history_ref"]["path"],"data/unl.json")
        (self.root/"data/unl.json").write_text('{"matches":[]}')
        self.assertTrue(load_sources(self.root)["complete_schedule"])
    def test_metadata_schema_failure_is_failure_not_successful_deferral(self):
        class InvalidMetadata(Client):
            def _cached_request(self,*args,**kwargs):
                env=super()._cached_request(*args,**kwargs)
                env["response"][0]["league"]["name"]="Unverified international competition"
                return env
        code,report=self.collect(InvalidMetadata(self.nationals))
        self.assertEqual(code,1)
        self.assertEqual(report["execution_status"],"failed")
        self.assertTrue(report["issues"])
    def test_missing_history_fixture_is_failed_census_not_empty_success(self):
        code,report=self.collect(Client(self.nationals[:1]))
        self.assertEqual(code,1)
        self.assertFalse(report["provider_schedule_queries_complete"])
        self.assertEqual(report["history_identity_mismatch_ids"],[51])
    def test_corrupt_optional_source_degrades_only_its_group_with_diagnostics(self):
        self.collect();source=load_sources(self.root)
        path=next(self.root/r["path"] for r in source["source_refs"] if json.loads((self.root/r["path"]).read_text()).get("params")=={"id":50})
        path.write_text('{}')
        degraded=load_player_load_sources(self.root,"club")
        self.assertFalse(degraded["complete_schedule"])
        self.assertEqual(degraded["source_refs"],[])
        self.assertTrue(any(r["sha256"]==hashlib.sha256(b'{}').hexdigest() for r in degraded["diagnostic_refs"]))
        value=self.values(sources=degraded)
        self.assertFalse(value["groups"]["restricted_observed_national_load"])
        self.assertFalse(value["prospective_qualified"])
        self.assertTrue(value["diagnostic_refs"])
    def test_duplicate_player_across_national_teams_rejected(self):
        self.nationals[0]["players"][1]["players"][0]["player"]["id"]=101
        code,report=self.collect()
        self.assertEqual(code,1)
        self.assertIn("player_load_player_on_both_national_teams",[p["reason"] for p in report["pending_requests"]])
        self.assertFalse(self.values()["groups"]["restricted_observed_national_load"])
    def test_recent_prematch_cache_is_refreshed_once_and_rejection_retained(self):
        class Stale(Client):
            def fetch_fixture(self,mid,**kwargs):
                env=super().fetch_fixture(mid,**kwargs)
                if not kwargs.get("force"):
                    env["from_cache"]=True
                    env["response"][0]["fixture"]["status"]["short"]="NS"
                    env["response"][0].pop("players")
                return env
        code,report=self.collect(Stale(self.nationals))
        self.assertEqual(code,0)
        self.assertTrue(report["full_collection_completed"])
        self.assertTrue(all(item["rejected_source_refs"] for item in report["details"]))
        value=self.values()
        self.assertTrue(value["groups"]["restricted_observed_national_load"])
        with tempfile.TemporaryDirectory() as dest:
            root=Path(dest)
            for ref in value["source_refs"]:
                path=root/ref["path"];path.parent.mkdir(parents=True,exist_ok=True)
                shutil.copyfile(self.root/ref["path"],path)
            self.assertEqual(feature_values(root,"club",self.target,value["cutoff_at"]),value)
    def test_preloaded_sources_from_after_cutoff_degrade_locally(self):
        self.collect();sources=load_sources(self.root)
        sources["manifest_observed_at"]="2026-01-17T00:00:00Z"
        value=self.values(sources=sources)
        self.assertFalse(value["groups"]["restricted_observed_national_load"])
        self.assertTrue(value["diagnostic_refs"])
    def test_capacity_deferral_before_collection_preserves_executable_operation(self):
        from football_model.advanced_capacity import CapacityGuard
        from football_model.state_storage import estimate_runtime
        estimate=estimate_runtime(self.root)
        guard=CapacityGuard(self.root,max_logical=estimate["logical_bytes"]+100000,
            max_physical=10000000,reserve_logical=100001,reserve_physical=0)
        code,report=collect_run(CONFIG,self.root,client=self.client,capacity_guard=guard,
            clock=lambda:datetime.fromisoformat(NOW.replace("Z","+00:00")))
        self.assertEqual(code,0)
        self.assertEqual(report["execution_status"],"deferred")
        self.assertEqual(report["api_requests"],0)
        self.assertEqual(report["pending_requests"][0]["operation"],"freeze_national_history")
        self.assertEqual(len(list((self.root/"advanced_features/player_load/manifests").glob("*.json"))),1)
    def test_unknown_national_identity_does_not_silently_expand_scope(self):
        config=copy.deepcopy(CONFIG);config["competitions"][0]["name"]="World Cup"
        with self.assertRaisesRegex(DataError,"requires_verified_nations_league"):
            collect_run(config,self.root,client=self.client)
    def test_observed_zero_is_not_an_observed_appearance(self):
        for row in self.nationals:
            for team in row["players"]:
                for p in team["players"]:p["statistics"][0]["games"]["minutes"]=0
        self.collect();self.assertIsNone(self.values()["home"]["restricted_observed_national_xi_match_count"])


if __name__=="__main__":unittest.main()
