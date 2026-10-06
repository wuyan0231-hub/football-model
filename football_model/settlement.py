"""Exact quarter-goal settlement and structural support, independent of prices."""
from __future__ import annotations

import math
from typing import Any
import numpy as np

STATES = ("win", "half_win", "push", "half_loss", "loss")
OUTCOMES = ("home_win", "draw", "away_win")


def quarter_line(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("line must be a finite number in quarter-goal units")
    if abs(value) > 100 or not math.isclose(value * 4, round(value * 4), abs_tol=1e-10):
        raise ValueError("line must be a multiple of 0.25 within [-100,100]")
    return round(value * 4)


def contract(value: dict) -> dict:
    if not isinstance(value, dict):
        raise ValueError("contract must be an object")
    if set(value) - {"market", "side", "line", "settlement_basis"}:
        raise ValueError("unknown contract fields; partial-match/start-score semantics are unsupported")
    if value.get("settlement_basis", "regulation_time_90_plus_stoppage") != "regulation_time_90_plus_stoppage":
        raise ValueError("only full regulation time plus stoppage is supported")
    market, side = value.get("market"), value.get("side")
    if market == "one_x_two":
        if side not in OUTCOMES:
            raise ValueError("invalid 1X2 side")
        if value.get("line") is not None:
            raise ValueError("1X2 has no handicap line")
        return {"market": market, "side": side}
    if market == "asian_handicap" and side not in ("home", "away"):
        raise ValueError("handicap side must be home or away")
    if market == "total_goals" and side not in ("over", "under"):
        raise ValueError("total side must be over or under")
    if market not in ("asian_handicap", "total_goals"):
        raise ValueError("unsupported market")
    line = quarter_line(value.get("line"))
    if market == "total_goals" and line < 0:
        raise ValueError("total line cannot be negative")
    return {"market": market, "side": side, "line": line / 4}


def settlement(home: int, away: int, value: dict) -> str:
    for score in (home, away):
        if isinstance(score, bool) or not isinstance(score, (int, np.integer)) or score < 0:
            raise ValueError("scores must be nonnegative integers")
    value = contract(value)
    if value["market"] == "one_x_two":
        outcome = "home_win" if home > away else ("draw" if home == away else "away_win")
        return "win" if outcome == value["side"] else "loss"
    q = quarter_line(value["line"])
    legs = [q] if q % 2 == 0 else [q - 1, q + 1]
    if value["market"] == "asian_handicap":
        margin = home - away if value["side"] == "home" else away - home
        signs = [int(np.sign(4 * margin + leg)) for leg in legs]
    else:
        signs = [int(np.sign(4 * (home + away) - leg)) for leg in legs]
        if value["side"] == "under":
            signs = [-sign for sign in signs]
    return {2: "win", 1: "half_win", 0: "push", -1: "half_loss", -2: "loss"}[
        sum(signs) * (2 if len(signs) == 1 else 1)]


def structural_states(value: dict) -> tuple[str, ...]:
    """Support comes from score algebra, never from tiny/truncated matrix cells."""
    value = contract(value)
    limit = int(abs(value.get("line", 0))) + 3
    if value["market"] == "total_goals":
        possible = {settlement(total, 0, value) for total in range(limit + 1)}
    else:
        possible = {settlement(max(margin, 0), max(-margin, 0), value)
                    for margin in range(-limit, limit + 1)}
    return tuple(state for state in STATES if state in possible)


def matrix_array(value: Any) -> np.ndarray:
    try:
        array = np.asarray(value, dtype=float)
    except (ValueError, TypeError) as exc:
        raise ValueError("invalid score matrix") from exc
    if (array.ndim != 2 or min(array.shape) < 1 or max(array.shape) > 401 or
            not np.all(np.isfinite(array)) or np.any(array < 0) or
            not math.isclose(float(array.sum()), 1, abs_tol=1e-8, rel_tol=0)):
        raise ValueError("matrix must be finite, nonnegative, two-dimensional and sum to one")
    return array


def probabilities(matrix: Any, value: dict) -> dict:
    array = matrix_array(matrix)
    value = contract(value)
    home, away = np.indices(array.shape)
    if value["market"] == "one_x_two":
        mask = {"home_win": home > away, "draw": home == away, "away_win": home < away}[value["side"]]
        codes = np.where(mask, 2, -2)
    else:
        q = quarter_line(value["line"])
        legs = [q] if q % 2 == 0 else [q - 1, q + 1]
        if value["market"] == "asian_handicap":
            margin = home - away if value["side"] == "home" else away - home
            codes = sum(np.sign(4 * margin + leg) for leg in legs)
        else:
            codes = sum(np.sign(4 * (home + away) - leg) for leg in legs)
            if value["side"] == "under":
                codes = -codes
        if len(legs) == 1:
            codes = codes * 2
    result = {state: float(array[codes == code].sum())
              for state, code in zip(STATES, (2, 1, 0, -1, -2))}
    support = structural_states(value)
    return {"probabilities": result, "possible_states": list(support),
            "structural_zero_states": [state for state in STATES if state not in support]}


def unit_return(state: str, decimal_odds: float, cost_per_unit: float = 0.0) -> float:
    if (isinstance(decimal_odds, bool) or not isinstance(decimal_odds, (int, float)) or
            not math.isfinite(decimal_odds) or decimal_odds <= 1):
        raise ValueError("decimal odds must exceed one")
    if (isinstance(cost_per_unit, bool) or not isinstance(cost_per_unit, (int, float)) or
            not math.isfinite(cost_per_unit) or not 0 <= cost_per_unit < 1):
        raise ValueError("cost_per_unit must be explicitly supplied in [0,1)")
    return {"win": decimal_odds - 1, "half_win": (decimal_odds - 1) / 2,
            "push": 0, "half_loss": -0.5, "loss": -1}[state] - cost_per_unit
