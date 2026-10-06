import copy
from contextlib import contextmanager
from pathlib import Path
import tempfile
import unittest

from football_model.data import APIError, DataError, RequestBudgetError
from football_model.fundamentals import (
    ENDPOINTS, build_features, collect_fixture_evidence, load_snapshots,
    make_snapshot, quantitative_scope, save_snapshot,
)


CUTOFF = "2026-10-06T11:00:00Z"
CURRENT = {"match_id": 100, "home_id": 1, "away_id": 2,
           "kickoff": "2026-10-06T12:00:00Z", "status": "NS"}
PRIOR = {"match_id": 90, "home_id": 2, "away_id": 1,
         "kickoff": "2026-10-02T12:00:00Z", "status": "FT"}


def envelope(kind, rows, observed=CUTOFF, fixture=CURRENT):
    return {"endpoint": ENDPOINTS[kind], "params": {"fixture": fixture["match_id"]},
            "response": rows, "fetched_at": observed, "errors": [],
            "paging": {"current": 1, "total": 1}, "from_cache": False}


def lineup(tid, players=range(11, 22)):
    return {"team": {"id": tid}, "formation": "4-3-3",
            "startXI": [{"player": {"id": p, "name": str(p)}} for p in players],
            "substitutes": []}


def statistics(tid, xg):
    return {"team": {"id": tid}, "statistics": [{"type": "expected_goals", "value": xg}]}


def snapshot(kind, rows, observed=CUTOFF, fixture=CURRENT, **kwargs):
    return make_snapshot(kind, envelope(kind, rows, observed, fixture), fixture, **kwargs)


class FundamentalsEvidenceTests(unittest.TestCase):
    def test_time_is_observation_not_fixture_or_earlier_publication(self):
        s = snapshot("injuries", [], published_at="2026-10-05T00:00:00Z")
        self.assertEqual(s["known_at"], CUTOFF)
        self.assertEqual(s["published_at"], "2026-10-05T00:00:00Z")
        stale_cache = envelope("injuries", [], "2026-10-05T09:00:00Z")
        stale_cache["from_cache"] = True
        s = make_snapshot("injuries", stale_cache, CURRENT)
        self.assertEqual(s["observed_at"], "2026-10-05T09:00:00Z")
        context = make_snapshot("injuries", stale_cache, CURRENT, context_observed_at=CUTOFF)
        self.assertEqual(context["observed_at"], s["observed_at"])
        self.assertEqual(context["known_at"], CUTOFF)

    def test_future_backfill_never_enters_old_cutoff(self):
        future = snapshot("lineups", [lineup(1)], "2026-10-07T11:00:00Z")
        features = build_features(CURRENT, cutoff=CUTOFF, snapshots=[future])
        self.assertEqual(features["excluded_evidence"]["known_after_cutoff"], 1)
        self.assertIsNone(features["teams"]["1"]["lineups"]["snapshot_id"])
        self.assertFalse(features["fundamentals_used_in_probabilities"])

    def test_missing_null_zero_remain_distinct_and_nonnumeric_rejected(self):
        s = snapshot("statistics", [statistics(1, None), {"team": {"id": 2}, "statistics": []}], fixture=PRIOR)
        self.assertEqual(s["teams"]["1"]["missing_reason"], "provider_null")
        self.assertEqual(s["teams"]["2"]["missing_reason"], "field_absent")
        z = snapshot("statistics", [statistics(1, "0.00")], fixture=PRIOR)
        self.assertEqual(z["teams"]["1"]["xg"], 0.0)
        self.assertIsNone(z["teams"]["1"]["missing_reason"])
        for bad in (True, -1, "NaN", "Infinity", "none"):
            with self.subTest(bad=bad), self.assertRaises(DataError):
                snapshot("statistics", [statistics(1, bad)], fixture=PRIOR)

    def test_endpoint_identity_pagination_and_team_mismatch_rejected(self):
        for alter in (lambda e: e.update(endpoint="/fixtures"),
                      lambda e: e.update(params={"fixture": 99}),
                      lambda e: e.update(paging={"total": 2}),
                      lambda e: e.update(errors=["missing permission"]),
                      lambda e: e.update(response=[lineup(3)])):
            e = envelope("lineups", [lineup(1)])
            alter(e)
            with self.assertRaises(DataError):
                make_snapshot("lineups", e, CURRENT)

    def test_duplicate_or_incomplete_lineup(self):
        with self.assertRaises(DataError):
            snapshot("lineups", [lineup(1, [12] * 11)])
        s = snapshot("lineups", [lineup(1, range(1, 11))])
        self.assertFalse(s["teams"]["1"]["complete_starting_eleven"])
        self.assertEqual(s["teams"]["2"]["missing_reason"], "team_record_absent")

    def test_empty_injury_report_does_not_assert_full_strength(self):
        s = snapshot("injuries", [])
        self.assertEqual(s["teams"]["1"]["reported_count"], 0)
        self.assertFalse(s["teams"]["1"]["complete_roster_status"])
        self.assertIsNotNone(s["teams"]["1"]["missing_reason"])
        rows = [{"team": {"id": 1}, "fixture": {"id": 100},
                 "player": {"id": 11, "type": "Questionable", "reason": "Knock"}}]
        detail = snapshot("injuries", rows)["teams"]["1"]["records"][0]
        self.assertEqual(detail["provider_type"], "Questionable")
        self.assertIsNone(detail["effect_estimate"])
        doubled = snapshot("injuries", rows + rows)
        self.assertEqual(doubled["teams"]["1"]["reported_count"], 1)
        self.assertEqual(doubled["teams"]["1"]["duplicate_rows_removed"], 1)
        self.assertEqual(len(doubled["raw_response"]), 2)
        conflict = copy.deepcopy(rows[0])
        conflict["player"]["reason"] = "Knee Injury"
        with self.assertRaises(DataError):
            snapshot("injuries", rows + [conflict])

    def test_immutable_roundtrip_and_tamper_rejection(self):
        s = snapshot("injuries", [])
        with tempfile.TemporaryDirectory() as td:
            path = save_snapshot(td, s)
            self.assertEqual(save_snapshot(td, s), path)
            self.assertEqual(load_snapshots(td), [s])
            bad = copy.deepcopy(s)
            bad["known_at"] = "2020-01-01T00:00:00Z"
            with self.assertRaises(DataError):
                save_snapshot(td, bad)

    def test_xg_home_away_orientation_and_true_observation_rest(self):
        s = snapshot("statistics", [statistics(2, "0.70"), statistics(1, "1.90")],
                     "2026-10-03T10:00:00Z", PRIOR)
        f = build_features(CURRENT, cutoff=CUTOFF, snapshots=[s])
        self.assertAlmostEqual(f["teams"]["1"]["prior_xg"]["for_mean"], 1.9)
        self.assertAlmostEqual(f["teams"]["1"]["prior_xg"]["against_mean"], 0.7)
        self.assertEqual(f["teams"]["1"]["prior_xg"]["paired_sample_count"], 1)
        self.assertEqual(f["teams"]["1"]["rest_days"]["value"], 4)
        no_time = build_features(CURRENT, cutoff=CUTOFF, snapshots=[], fixture_history=[PRIOR])
        self.assertIsNone(no_time["teams"]["1"]["rest_days"]["value"])

    def test_current_match_stats_extra_time_and_post_cutoff_stats_are_excluded(self):
        own = snapshot("statistics", [statistics(1, 4), statistics(2, 1)], fixture=CURRENT)
        extra = snapshot("statistics", [statistics(1, 2), statistics(2, 1)], fixture={**PRIOR, "status": "AET"})
        future = snapshot("statistics", [statistics(1, 2), statistics(2, 1)], "2026-10-07T00:00:00Z", PRIOR)
        f = build_features(CURRENT, cutoff=CUTOFF, snapshots=[own, extra, future])
        self.assertIsNone(f["teams"]["1"]["prior_xg"]["for_mean"])
        self.assertEqual(f["teams"]["1"]["prior_xg"]["paired_sample_count"], 0)

    def test_recent_window_does_not_skip_missing_matches_to_find_old_xg(self):
        s = snapshot("statistics", [statistics(1, 1), statistics(2, 1)], fixture=PRIOR)
        recent = {**PRIOR, "match_id": 95, "kickoff": "2026-10-04T12:00:00Z", "observed_at": "2026-10-05T10:00:00Z"}
        f = build_features(CURRENT, cutoff=CUTOFF, snapshots=[s], fixture_history=[recent], recent_matches=1)
        self.assertEqual(f["teams"]["1"]["prior_xg"]["recent_finished_fixture_count"], 1)
        self.assertEqual(f["teams"]["1"]["prior_xg"]["coverage_fraction"], 0)
        self.assertIsNone(f["teams"]["1"]["prior_xg"]["for_mean"])
        self.assertEqual(f["teams"]["1"]["rest_days"]["value"], 2)

    def test_prematch_lineup_archive_joins_later_finished_history(self):
        current = snapshot("lineups", [lineup(1, range(12, 23))])
        old = snapshot("lineups", [lineup(1)], "2026-10-02T11:30:00Z", {**PRIOR, "status": "NS"})
        without_finished = build_features(CURRENT, cutoff=CUTOFF, snapshots=[current, old])
        self.assertIsNone(without_finished["teams"]["1"]["lineup_continuity"]["previous_starter_fraction"])
        history = [{**PRIOR, "observed_at": "2026-10-03T10:00:00Z"}]
        f = build_features(CURRENT, cutoff=CUTOFF, snapshots=[current, old], fixture_history=history)
        continuity = f["teams"]["1"]["lineup_continuity"]
        self.assertAlmostEqual(continuity["previous_starter_fraction"], 10 / 11)
        self.assertAlmostEqual(continuity["usual_starter_fraction"], 10 / 11)
        self.assertEqual(continuity["prior_lineup_count"], 1)
        history.append({**PRIOR, "match_id": 95, "kickoff": "2026-10-04T12:00:00Z",
                        "observed_at": "2026-10-05T10:00:00Z"})
        g = build_features(CURRENT, cutoff=CUTOFF, snapshots=[current, old], fixture_history=history)
        continuity = g["teams"]["1"]["lineup_continuity"]
        self.assertIsNone(continuity["previous_starter_fraction"])
        self.assertEqual(continuity["previous_match_id"], 95)
        self.assertEqual(continuity["latest_observed_lineup_match_id"], 90)

    def test_latest_known_revision_is_used_not_future_revision(self):
        old = snapshot("lineups", [lineup(1)], "2026-10-06T10:30:00Z")
        late = snapshot("lineups", [lineup(1, range(21, 32))], "2026-10-06T11:30:00Z")
        f = build_features(CURRENT, cutoff=CUTOFF, snapshots=[old, late])
        self.assertEqual(f["teams"]["1"]["lineups"]["snapshot_id"], old["snapshot_id"])
        conflict = snapshot("lineups", [lineup(1, range(21, 32))], "2026-10-06T10:30:00Z")
        with self.assertRaises(DataError):
            build_features(CURRENT, cutoff=CUTOFF, snapshots=[old, conflict])

    def test_cutoff_must_be_timezone_aware_and_prematch(self):
        for cutoff in ("2026-10-06T11:00:00", CURRENT["kickoff"]):
            with self.assertRaises(DataError):
                build_features(CURRENT, cutoff=cutoff, snapshots=[])


class FakeClient:
    def __init__(self, failure=None):
        self.calls, self.reservation = [], 0
        self.failure = failure

    @contextmanager
    def reserve_requests(self, n):
        self.reservation = n
        yield
        self.reservation = 0

    def fetch_fundamentals(self, endpoint, fixture_id, **kwargs):
        self.calls.append((endpoint, fixture_id, kwargs, self.reservation))
        if self.failure:
            raise self.failure
        return {"endpoint": endpoint, "params": {"fixture": fixture_id},
                "response": [], "fetched_at": CUTOFF, "errors": []}


class FundamentalsCollectionTests(unittest.TestCase):
    COVERAGE = {"injuries": True, "fixtures": {"lineups": True, "statistics_fixtures": True}}

    def test_shared_adapter_and_reservation(self):
        client = FakeClient()
        result = collect_fixture_evidence(client, CURRENT, coverage=self.COVERAGE,
                                          observed_at=CUTOFF, reserved_requests=8)
        self.assertEqual(len(result["snapshots"]), 2)
        self.assertTrue(all(call[-1] == 8 for call in client.calls))
        self.assertEqual(client.calls[0][2]["max_age_seconds"], 4 * 3600)
        self.assertEqual(result["missing"], [])

    def test_coverage_budget_and_timing_are_distinct_missing_reasons(self):
        client = FakeClient()
        result = collect_fixture_evidence(client, CURRENT, coverage={"injuries": False}, observed_at=CUTOFF)
        self.assertEqual([m["reason"] for m in result["missing"]], ["coverage_false", "coverage_unknown"])
        self.assertEqual(client.calls, [])
        result = collect_fixture_evidence(FakeClient(RequestBudgetError("budget")), CURRENT,
            coverage=self.COVERAGE, observed_at=CUTOFF)
        self.assertTrue(all(m["reason"] == "budget_deferred" for m in result["missing"]))
        result = collect_fixture_evidence(client, CURRENT, coverage=self.COVERAGE,
            observed_at="2026-10-06T01:00:00Z", kinds=("lineups", "statistics"))
        self.assertEqual([m["reason"] for m in result["missing"]],
                         ["lineup_window_not_open", "not_completed_90_minute_fixture"])
        result = collect_fixture_evidence(client, PRIOR, coverage=self.COVERAGE, observed_at=CUTOFF)
        self.assertTrue(all(m["reason"] == "target_fixture_not_scheduled" for m in result["missing"]))
        self.assertEqual(result["snapshots"][0]["kind"], "lineups")


if __name__ == "__main__":
    unittest.main()
