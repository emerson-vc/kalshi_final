"""Stake sizing and exposure (Phase 3).

Stake = bankroll x kelly_fraction (1/4) x full-Kelly x uncertainty shrink, clipped by the per-combo,
per-game and per-player caps after counting what's already exposed. Example from the spec: a 20x combo
with +30% edge -> full Kelly ~ 1.58% -> 1/4 Kelly on $2,000 ~ $7.90.

The shrink uses the fair model's band: shrink = clip(EV_at_lower_bound / EV_at_fair, 0, 1). When the
lower bound is already break-even or worse, the stake goes to zero.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from combo_edge import pricing


def uncertainty_shrink(fair: float | None, fair_lower: float | None, payout: float | None) -> float:
    if fair is None or payout is None:
        return 0.0
    ev_mid = fair * payout - 1.0
    if ev_mid <= 0:
        return 0.0
    if fair_lower is None:
        return 0.5
    ev_low = fair_lower * payout - 1.0
    return max(0.0, min(1.0, ev_low / ev_mid))


def kelly_stake(fair: float | None, fair_lower: float | None, payout: float | None, bankroll: float, kelly_fraction: float, cap_combo: float) -> float:
    if fair is None or payout is None or payout <= 1.0:
        return 0.0
    f = pricing.kelly_fraction(fair, payout)
    stake = bankroll * kelly_fraction * f * uncertainty_shrink(fair, fair_lower, payout)
    return round(max(0.0, min(stake, cap_combo)), 2)


@dataclass
class Exposure:
    by_game: dict[str, float] = field(default_factory=lambda: defaultdict(float))
    by_player: dict[str, float] = field(default_factory=lambda: defaultdict(float))
    by_signature: dict[str, float] = field(default_factory=lambda: defaultdict(float))
    combos_by_player: dict[str, int] = field(default_factory=lambda: defaultdict(int))

    def add(self, signature: str, game: str | None, players: list[str], stake: float) -> None:
        self.by_signature[signature] += stake
        if game:
            self.by_game[game] += stake
        for p in players:
            self.by_player[p] += stake
            self.combos_by_player[p] += 1

    def room(self, game: str | None, players: list[str], cap_combo: float, cap_game: float, cap_player: float, signature: str | None = None) -> float:
        room = cap_combo - (self.by_signature.get(signature or "", 0.0) if signature else 0.0)
        if game:
            room = min(room, cap_game - self.by_game.get(game, 0.0))
        for p in players:
            room = min(room, cap_player - self.by_player.get(p, 0.0))
        return max(0.0, room)


def clip_to_caps(stake: float, exposure: Exposure, game: str | None, players: list[str], cap_combo: float, cap_game: float, cap_player: float, signature: str | None = None) -> float:
    return round(max(0.0, min(stake, exposure.room(game, players, cap_combo, cap_game, cap_player, signature))), 2)


def stacking_warnings(exposure: Exposure, threshold: int, names: dict[str, str] | None = None) -> list[str]:
    out = []
    for p, n in sorted(exposure.combos_by_player.items(), key=lambda kv: -kv[1]):
        if n >= threshold:
            out.append(f"{(names or {}).get(p, p)}: {n} combos stacked (${exposure.by_player.get(p, 0):.2f})")
    return out
