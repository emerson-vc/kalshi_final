"""Stratum classification for any same-game pair (Phase 1.5 design).

Level 1 (the allocation cell, where the ~30-quote target applies): unordered leg-type pair x relation.
Level 2 (randomized within a cell, estimated by regression): tailness of each leg x sides.
PROT legs count as their own leg type.
"""

from __future__ import annotations

from combo_edge.sports.base import SidedLeg

TAIL_MAIN = 0.35
TAIL_ALT = 0.12


def leg_type(s: SidedLeg) -> str:
    leg = s.leg
    if leg.scope == "player":
        pos = leg.position or "UNK"
        if pos in {"REC", "UNK"}:
            pos = "WRTE"
        t = f"{pos}:{leg.kind}"
    elif leg.scope == "team":
        t = leg.kind
    else:
        t = f"game:{leg.kind}"
    return t + ("[prot]" if leg.protected else "")


def relation(a: SidedLeg, b: SidedLeg) -> str:
    la, lb = a.leg, b.leg
    if la.player_key and la.player_key == lb.player_key:
        return "same_player"
    if la.scope == "game" or lb.scope == "game" or not la.team or not lb.team:
        return "game"
    return "same_team" if la.team == lb.team else "opponent"


def tailness(p: float | None) -> str:
    if p is None:
        return "unpriced"
    if p >= TAIL_MAIN:
        return "main"
    if p >= TAIL_ALT:
        return "alt"
    return "extreme"


def classify(a: SidedLeg, b: SidedLeg, pa: float | None, pb: float | None) -> tuple[str, str]:
    """Return (l1, l2). ``pa``/``pb`` are probabilities of the selected sides."""
    ta, tb = leg_type(a), leg_type(b)
    rel = relation(a, b)
    if (ta, a.side) > (tb, b.side):
        ta, tb, a, b, pa, pb = tb, ta, b, a, pb, pa
    l1 = f"{ta}×{tb}|{rel}"
    l2 = f"{tailness(pa)}/{tailness(pb)}|{a.side[0]}{b.side[0]}"
    return l1, l2
