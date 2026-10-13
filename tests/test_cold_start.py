import copy
import unittest
from football_model.cold_start import score_cold_start
from football_model.model import MODEL_VERSION, ModelDataError, UnsupportedTeamError, score_distribution


class ColdStartTests(unittest.TestCase):
    def setUp(self):
        self.model = dict(model_version=MODEL_VERSION, fit_status="converged", graph_connected=True,
            training_cutoff="2026-01-01T00:00:00Z", training_data_sha256="a"*64,
            intercept=0.1, home_advantage=0.2, team_parameters={"1":dict(name="Known",attack=.3,defence=.2,training_matches=50)},
            warnings=[],low_sample_team_threshold=5)
        self.fixture = dict(match_id=8,home_id=1,away_id=2,home_name="Known",away_name="New",kickoff="2026-01-02T00:00:00Z")
    def test_does_not_change_baseline_or_invent_strength(self):
        original=copy.deepcopy(self.model)
        p=score_cold_start(self.model,self.fixture)
        self.assertEqual(original,self.model)
        self.assertEqual(p["unknown_team_ids"],[2])
        self.assertEqual(p["team_training_matches"]["away"],0)
        self.assertFalse(p["paired_with_production_baseline"])
        self.assertAlmostEqual(sum(map(sum,p["score_matrix"])),1)
        with self.assertRaises(UnsupportedTeamError):
            score_distribution(self.model,1,2,self.fixture["kickoff"])
    def test_identity_and_time_still_required(self):
        for changes in ({"home_id":2},{"away_name":None},{"kickoff":"2025-01-01T00:00:00Z"},{"neutral":"false"}):
            with self.assertRaises(ModelDataError):
                score_cold_start(self.model,{**self.fixture,**changes})
    def test_neutral_does_not_keep_home_advantage(self):
        p=score_cold_start(self.model,self.fixture)
        n=score_cold_start(self.model,{**self.fixture,"neutral":True})
        self.assertGreater(p["expected_goals"]["home"],n["expected_goals"]["home"])
        self.assertEqual(p["expected_goals"]["away"],n["expected_goals"]["away"])
