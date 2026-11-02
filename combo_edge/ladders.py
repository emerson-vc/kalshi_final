"""Leg probabilities from whole threshold ladders.

A rung with a two-sided book gets its mid. Rungs with only an ask (often 1c tail rungs with no bid) are
NOT taken at the ask, which overstates their probability and made tail-on-tail candidates look
absurdly good. Instead each ladder (same player or team, same stat) is made monotone (isotonic), and
missing rungs are interpolated or extrapolated in log-odds from the quoted rungs, capped by the ask
and by the neighbouring rung. ``p_source`` records how each estimate was made, and the prescore widens
its uncertainty band for anything that isn't a two-sided mid.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Iterable

from combo_edge.sports.base import Leg

# Fallback log-odds slope per unit of threshold when a ladder has only one quoted rung.
DEFAULT_SLOPE = {
    "pass_td": -1.6, "anytime_td": -2.4, "receptions": -0.45, "pass_int": -1.4, "rec_yds": -0.035, "rush_yds": -0.04,
    "pass_yds": -0.025, "rush_rec_yds": -0.035, "team_total": -0.17, "h1_team_total": -0.3, "game_total": -0.12,
    "spread": -0.13, "h1_spread": -0.2,
}


def _logit(p: float) -> float:
    p = min(max(p, 1e-6), 1 - 1e-6)
    return math.log(p / (1 - p))


def _expit(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))


def _isotonic_decreasing(vals: list[float]) -> list[float]:
    """Pool-adjacent-violators for a non-increasing sequence."""
    blocks: list[list[float]] = []
    for v in vals:
        blocks.append([v, 1.0])
        while len(blocks) > 1 and blocks[-2][0] < blocks[-1][0]:
            v2, w2 = blocks.pop()
            v1, w1 = blocks.pop()
            blocks.append([(v1 * w1 + v2 * w2) / (w1 + w2), w1 + w2])
    out: list[float] = []
    for v, w in blocks:
        out += [v] * int(w)
    return out


def _two_sided_mid(leg: Leg) -> float | None:
    if leg.yes_bid is not None and leg.yes_ask is not None and 0 < leg.yes_bid < leg.yes_ask < 1:
        return (leg.yes_bid + leg.yes_ask) / 2.0
    return None


def ladder_key(leg: Leg) -> tuple[str, ...] | None:
    if leg.threshold is None:
        return None
    if leg.scope == "player":
        return ("p", leg.player_key or leg.ticker, leg.kind, "prot" if leg.protected else "", "precise" if leg.precise else "")
    if leg.scope == "team":
        return ("t", leg.game_code, leg.team or "", leg.kind)
    return ("g", leg.game_code, leg.kind, "precise" if leg.precise else "")


def estimate(legs: Iterable[Leg]) -> dict[str, int]:
    """Set ``p_est``/``p_source`` on every leg in place. Returns counts by source."""
    groups: dict[tuple[str, ...], list[Leg]] = defaultdict(list)
    counts: dict[str, int] = defaultdict(int)
    for leg in legs:
        key = ladder_key(leg)
        if key is None:
            m = _two_sided_mid(leg)
            if m is not None:
                leg.p_est, leg.p_source = m, "mid"
            elif leg.yes_ask is not None:
                leg.p_est, leg.p_source = leg.yes_ask * 0.6, "ask_only"
            else:
                leg.p_est, leg.p_source = None, "none"
            counts[leg.p_source] += 1
            continue
        groups[key].append(leg)
    for key, ladder in groups.items():
        ladder.sort(key=lambda l: l.threshold or 0.0)
        mids = [_two_sided_mid(l) for l in ladder]
        quoted = [i for i, m in enumerate(mids) if m is not None]
        if quoted:
            iso = _isotonic_decreasing([mids[i] for i in quoted])  # type: ignore[misc]
            for i, v in zip(quoted, iso, strict=True):
                ladder[i].p_est, ladder[i].p_source = min(max(v, 1e-4), 1 - 1e-4), "mid"
        kind = key[2] if key[0] == "p" else key[3] if key[0] == "t" else key[2]
        for i, leg in enumerate(ladder):
            if leg.p_source == "mid":
                continue
            lo = max((j for j in quoted if j < i), default=None)
            hi = min((j for j in quoted if j > i), default=None)
            x = leg.threshold or 0.0
            if lo is not None and hi is not None:
                x0, x1 = ladder[lo].threshold or 0.0, ladder[hi].threshold or 0.0
                y0, y1 = _logit(ladder[lo].p_est or 0.5), _logit(ladder[hi].p_est or 0.5)
                t = (x - x0) / (x1 - x0) if x1 != x0 else 0.5
                p, src = _expit(y0 + t * (y1 - y0)), "interp"
            elif lo is not None or hi is not None:
                anchors = [j for j in quoted if (j < i if hi is None else j > i)]
                a = anchors[-1] if hi is None else anchors[0]
                if len(anchors) >= 2:
                    b = anchors[-2] if hi is None else anchors[1]
                    xa, xb = ladder[a].threshold or 0.0, ladder[b].threshold or 0.0
                    slope = (_logit(ladder[a].p_est or 0.5) - _logit(ladder[b].p_est or 0.5)) / (xa - xb) if xa != xb else DEFAULT_SLOPE.get(kind, -0.2)
                    slope = min(slope, -1e-3)  # must decrease with threshold
                else:
                    slope = DEFAULT_SLOPE.get(kind, -0.2)
                p = _expit(_logit(ladder[a].p_est or 0.5) + slope * (x - (ladder[a].threshold or 0.0)))
                src = "extrap"
            else:
                p = (leg.yes_ask * 0.6) if leg.yes_ask is not None else None
                src = "ask_only" if p is not None else "none"
            if p is not None and leg.yes_ask is not None:
                p = min(p, leg.yes_ask)  # nobody sells YES below its probability for long
            leg.p_est, leg.p_source = p, src
        # Final monotone pass so every rung respects its neighbours.
        prev = 1.0
        for leg in ladder:
            if leg.p_est is not None:
                leg.p_est = min(leg.p_est, prev)
                prev = leg.p_est
        for leg in ladder:
            counts[leg.p_source or "none"] += 1
    return dict(counts)
