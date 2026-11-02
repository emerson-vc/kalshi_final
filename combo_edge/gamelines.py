"""Main lines (the 50% point) interpolated from Kalshi ladders: game total, team totals, spreads."""

from __future__ import annotations

from collections.abc import Iterable

from combo_edge.sports.base import Leg


def main_line(legs: Iterable[Leg]) -> float | None:
    rungs = sorted((l.threshold, l.p_est if l.p_est is not None else l.p_market()) for l in legs if l.threshold is not None)
    rungs = [(x, p) for x, p in rungs if p is not None]
    for (x0, p0), (x1, p1) in zip(rungs, rungs[1:]):
        if p0 >= 0.5 >= p1 and p0 != p1:
            return x0 + (p0 - 0.5) / (p0 - p1) * (x1 - x0)
    return None


def game_lines(legs: list[Leg], teams: tuple[str, str]) -> dict[str, object]:
    reg = [l for l in legs if not l.protected and not l.precise]
    total = main_line(l for l in reg if l.kind == "game_total")
    out: dict[str, object] = {"total": total, "team_total": {}, "spread": {}}
    for t in teams:
        tt = main_line(l for l in reg if l.kind == "team_total" and l.team == t)
        sp = main_line(l for l in reg if l.kind == "spread" and l.team == t)
        out["team_total"][t] = tt  # type: ignore[index]
        out["spread"][t] = sp  # type: ignore[index]
    a, h = teams
    for t, o in ((a, h), (h, a)):
        if out["spread"].get(t) is None and out["spread"].get(o) is not None:  # type: ignore[union-attr]
            out["spread"][t] = -out["spread"][o]  # type: ignore[index,operator]
        if out["team_total"].get(t) is None and total is not None:  # type: ignore[union-attr]
            s = out["spread"].get(t) or 0.0  # type: ignore[union-attr]
            out["team_total"][t] = (total + s) / 2.0  # type: ignore[index]
    return out
