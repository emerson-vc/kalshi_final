"""Blend our model with Kalshi: Kalshi wins on liquid legs, the model gets more weight on thin ones.
Big disagreements are flagged so you can check the leg by hand (e.g. in OddsJam)."""

from __future__ import annotations

from combo_edge.sports.base import Leg


def kalshi_weight(leg: Leg) -> float:
    src = leg.p_source or "mid"
    if leg.p_market() is None:
        return 0.0
    if src in ("interp", "extrap"):
        return 0.30
    if src == "ask_only":
        return 0.15
    spread = leg.spread()
    if spread is None:
        return 0.35
    w = 0.92 if spread <= 0.02 else 0.82 if spread <= 0.04 else 0.68 if spread <= 0.07 else 0.5 if spread <= 0.12 else 0.35
    depth = (leg.yes_bid_size or 0.0) + (leg.yes_ask_size or 0.0)
    if depth < 50:
        w -= 0.12
    return max(0.2, min(0.95, w))


def blend(p_kalshi: float | None, p_model: float | None, w: float) -> float | None:
    if p_model is None:
        return p_kalshi
    if p_kalshi is None:
        return p_model
    return w * p_kalshi + (1.0 - w) * p_model


def disagrees(p_kalshi: float | None, p_model: float | None) -> bool:
    if p_kalshi is None or p_model is None:
        return False
    return abs(p_model - p_kalshi) > max(0.07, 0.45 * min(p_kalshi, 1 - p_kalshi))
