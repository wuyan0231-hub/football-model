"""Current competition scope, including rejection of retired configurations.

Historical evidence is read with its fixed source; a stale config cannot enable
a competition removed from current collection, fitting, or prediction.
"""
from __future__ import annotations

import re


def _identity(value):
    return re.sub(r"[\W_]+", "", str(value).casefold())


_RETIRED = frozenset(("unl", "uefanationsleague", "nationsleague",
    "europeannationsleague", "欧国联", "欧洲国家联赛", "欧洲国家联赛赛事"))


def is_retired(value):
    return _identity(value) in _RETIRED


def require_league(league):
    if is_retired(league):
        raise ValueError("competition_removed_by_user")
    return league


def require_competition(competition, provider_id=None):
    if (any(is_retired(competition.get(field, "")) for field in ("key", "name", "label"))
            or competition.get("mode") in ("national_baseline", "nations_league_baseline")
            or str(competition.get("provider_id", "")) == "5" or str(provider_id) == "5"):
        raise ValueError("competition_removed_by_user")
    return competition


def select_competitions(config, league="all", *, models_only=False):
    require_league(league)
    selected = []
    for competition in config.get("competitions", []):
        if league != "all" and competition.get("key") != league:
            continue
        try:
            require_competition(competition)
        except ValueError:
            if league != "all":
                raise
            continue
        if not models_only or competition.get("mode") != "audit_only":
            selected.append(competition)
    return selected
