"""Pre-filter pairs that Kalshi must reject (structural) and label pairs that might be rejected
(logical implication), so rejections can be learned per rule class instead of retried."""

from __future__ import annotations

from combo_edge.sports.base import SidedLeg


def structural_rule(a: SidedLeg, b: SidedLeg) -> str | None:
    """Pairs that can never be valid combos. Always skipped before any POST."""
    la, lb = a.leg, b.leg
    if la.ticker == lb.ticker:
        return "duplicate_leg"
    if la.event_ticker == lb.event_ticker and (la.event_size_max == 1 or lb.event_size_max == 1):
        return "same_event_single_market"
    if (a.side == "no" and la.yes_only) or (b.side == "no" and lb.yes_only):
        return "yes_only_event"
    if la.player_key and la.player_key == lb.player_key and la.kind == lb.kind:
        return "same_player_ladder"
    return None


def logical_class(a: SidedLeg, b: SidedLeg) -> str | None:
    """Pairs where one leg logically forces the other. Kalshi may reject these; we learn which.

    Returns a rule class label, or None.
    """
    la, lb = a.leg, b.leg
    pair = {la.kind, lb.kind}
    if pair == {"ml", "spread"} and la.team == lb.team:
        return "spread_implies_ml"
    if pair == {"team_total", "game_total"}:
        tt = la if la.kind == "team_total" else lb
        gt = lb if tt is la else la
        if tt.threshold is not None and gt.threshold is not None and tt.threshold >= gt.threshold:
            return "team_total_implies_game_total"
    if pair == {"pass_td", "team_total"}:
        q = la if la.kind == "pass_td" else lb
        t = lb if q is la else la
        if q.team == t.team and q.threshold is not None and t.threshold is not None:
            k = int(round(q.threshold + 0.5))
            if 6 * k > t.threshold:
                return "td_points_imply_team_total"
    if pair == {"h1_spread", "h1_ml"} and la.team == lb.team:
        return "h1_spread_implies_h1_ml"
    return None
