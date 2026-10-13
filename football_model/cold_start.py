"""Explicit league-centred prior for unseen teams; a separate research arm.

This never changes or relabels the production baseline. Zero attack/defence is
the fitted league-centred prior, not an estimate that an unseen team is average.
An unpaired cold-start output must not enter a paired improvement denominator.
"""
from __future__ import annotations

import copy
import hashlib
import json

from .model import ModelDataError, _integer, score_distribution


VERSION = "unseen_team_league_centred_prior_v1"


def score_cold_start(model, fixture):
    """Return a labelled fallback with known opponent coefficients preserved."""
    if not isinstance(model, dict) or not isinstance(fixture, dict):
        raise ModelDataError("cold-start input must be objects")
    ids = [_integer(fixture.get(s + "_id"), s + "_id", 1) for s in ("home", "away")]
    if ids[0] == ids[1]:
        raise ModelDataError("cold-start fixture has duplicate teams")
    original = json.dumps(model, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    clone = copy.deepcopy(model)
    unknown = []
    for side, tid in zip(("home", "away"), ids):
        if str(tid) not in clone.get("team_parameters", {}):
            name = fixture.get(side + "_name")
            if not isinstance(name, str) or not name.strip():
                raise ModelDataError("unseen team name and identity required")
            clone["team_parameters"][str(tid)] = {"name": name, "attack": 0.0,
                                                   "defence": 0.0, "training_matches": 0}
            unknown.append(tid)
    if not unknown:
        raise ModelDataError("cold-start branch only applies to unseen teams")
    forecast = score_distribution(clone, *ids, fixture["kickoff"], fixture.get("neutral", False))
    forecast.update(candidate_id="COLD_START", model_scope="unseen_team_prior_research_only",
                    model_version=VERSION, parent_model_sha256=hashlib.sha256(original).hexdigest(),
                    unknown_team_ids=unknown, unknown_team_coefficients="zero_league_centred_prior",
                    calibrated_uncertainty_available=False, production_promoted=False,
                    paired_with_production_baseline=False,
                    qualification="coverage_diagnostic_not_validated_team_strength")
    forecast.setdefault("warnings", []).append("未知球队使用联赛中心先验；并未学到该队真实强弱，不构成生产基线或精度改善证明。")
    return forecast
