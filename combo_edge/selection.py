"""Split a run's RFQ and creation budget across candidate pools.

Pools:
- ``core``     the proven patterns in ``config/core.yaml``, swept rung by rung (round-robin across players)
- ``free``     combos that already exist (in the index from the RFQ feed or earlier runs): cost no creations
- ``exploit``  highest expected EV
- ``explore``  stratified exploration / bandit picks (Phase 1.5 / 2.5 supply these)
- ``unpriced`` a small random slice of candidates with an unpriced leg (never filter on liquidity)

Core, free and exploit only send combos inside the payout band (default 8x-50x) whose family is not
explore-only; anything else can only go out through explore or unpriced.

Never create a combo for a candidate that isn't selected here (top N only).
"""

from __future__ import annotations

import math
import random
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from combo_edge.candidates import Candidate
from combo_edge.core import CoreContext, Sweep, core_match


@dataclass
class Selection:
    chosen: list[Candidate] = field(default_factory=list)
    by_pool: dict[str, int] = field(default_factory=dict)
    new_creations_planned: int = 0
    core_ctx: CoreContext | None = None  # lines/receiver ranks the core sweeps matched against


def _ev_key(c: Candidate) -> float:
    """Rank on EV at the fair model's lower bound, so noisy tail estimates can't crowd out solid ones."""
    v = c.expected_ev_lower if c.expected_ev_lower is not None else c.expected_ev
    return v if v is not None else -9.0


def _entity(leg: Any) -> str:
    return leg.player_key or f"{leg.team}:{leg.kind}"


def payout_of(c: Candidate) -> float | None:
    """The payout the band is judged on: expected payout, else the product of the leg payouts."""
    return c.expected_payout if c.expected_payout is not None else c.leg_payout_product


def _spread_rungs(group: list[Candidate], band: tuple[float, float] | None) -> list[Candidate]:
    """Order one pair's rungs from the middle of the payout band outward, preferring rungs that reuse
    each leg least (e.g. 3+/4+ x o21.5…o28.5 before any extreme). Nearest the middle breaks ties."""
    mid = math.sqrt(band[0] * band[1]) if band else 20.0

    def dist(c: Candidate) -> float:
        pay = payout_of(c)
        return abs(math.log(pay / mid)) if pay and pay > 0 else 99.0

    left = sorted(group, key=lambda c: (dist(c), -_ev_key(c)))
    uses: dict[str, int] = defaultdict(int)
    out: list[Candidate] = []
    while left:
        i = min(range(len(left)), key=lambda j: sum(uses[s.leg.ticker] for s in left[j].legs))
        c = left.pop(i)
        for s in c.legs:
            uses[s.leg.ticker] += 1
        out.append(c)
    return out


def round_robin_core(cands: list[Candidate], sweeps: list[Sweep], recently_quoted: set[str], ctx: CoreContext | None = None,
                     band: tuple[float, float] | None = None) -> dict[str, list[Candidate]]:
    """Per sweep, its candidates in sending order: one rung per (game, player/team pair) per round,
    middle-of-band rungs first within a pair; combos quoted recently go after everything not yet covered."""
    out: dict[str, list[Candidate]] = {sw.name: [] for sw in sweeps}
    for fresh in (True, False):
        groups: dict[tuple[str, ...], list[Candidate]] = defaultdict(list)
        for c in cands:
            sw = core_match(c, sweeps, ctx)
            if sw is not None and (c.signature not in recently_quoted) == fresh:
                if fresh:
                    c.notes.append(f"core:{sw.name}")
                groups[sw.group(c)].append(c)
        by_sweep: dict[str, list[list[Candidate]]] = defaultdict(list)
        for key, g in groups.items():
            by_sweep[key[1]].append(_spread_rungs(g, band))
        for name, queues in by_sweep.items():
            queues.sort(key=lambda q: _ev_key(q[0]), reverse=True)
            depth = 0
            while any(depth < len(q) for q in queues):
                out[name].extend(q[depth] for q in queues if depth < len(q))
                depth += 1
    return out


def select(
    cands: list[Candidate],
    *,
    rfq_budget: int,
    creation_budget: int,
    known: set[str],
    shares: dict[str, float],
    explore_picker: Callable[[list[Candidate], int, int], list[Candidate]] | None = None,
    exploit_key: Callable[[Candidate], float] | None = None,
    core: list[Sweep] | None = None,
    core_ctx: CoreContext | None = None,
    recently_quoted: set[str] | None = None,
    payout_band: tuple[float, float] | None = None,
    explore_only: Callable[[Candidate], bool] | None = None,
    max_rungs_per_pair: int = 2,
    seed: int | None = None,
) -> Selection:
    rng = random.Random(seed)
    sel = Selection()
    taken: set[str] = set()
    creations = 0

    def take(c: Candidate, pool: str, creation_cap: int | None = None) -> bool:
        nonlocal creations
        if c.signature in taken or len(sel.chosen) >= rfq_budget:
            return False
        needs_create = c.signature not in known
        if needs_create and (creations >= creation_budget or (creation_cap is not None and creations >= creation_cap)):
            return False
        c.pool = pool
        c.existed = None if needs_create else True
        taken.add(c.signature)
        sel.chosen.append(c)
        sel.by_pool[pool] = sel.by_pool.get(pool, 0) + 1
        if needs_create:
            creations += 1
        return True

    def rankable(c: Candidate) -> bool:
        """Core, exploit and free only send combos inside the payout band and not explore-only."""
        if explore_only is not None and explore_only(c):
            return False
        if payout_band is None:
            return True
        pay = payout_of(c)
        return pay is not None and payout_band[0] <= pay <= payout_band[1]

    priced = sorted((c for c in cands if not c.unpriced and c.expected_ev is not None), key=_ev_key, reverse=True)
    unpriced = [c for c in cands if c.unpriced]

    # Core sweeps first: the proven patterns, each sweep with its share of the core budget (unused share
    # flows to the next sweep), round-robin across players, middle-of-band rungs first.
    if core:
        n_core = int(round(rfq_budget * shares.get("core", 0.0)))
        c_core = creations + int(round(creation_budget * shares.get("core", 0.0)))
        queues = round_robin_core(cands, core, recently_quoted or set(), core_ctx, payout_band)
        carry, used = 0, 0
        for sw in core:
            quota = min(int(round(n_core * sw.share)) + carry, n_core - used)
            n = 0
            for c in queues.get(sw.name, []):
                if n >= quota:
                    break
                if rankable(c) and take(c, "core", c_core):
                    n += 1
            used += n
            carry = quota - n
        for sw in core:  # share nobody could use: offer it to every sweep again, in order
            for c in queues.get(sw.name, []):
                if used >= n_core:
                    break
                if rankable(c) and take(c, "core", c_core):
                    used += 1

    # Free combos next: pricing them costs no creation budget.
    free_cap = int(round(rfq_budget * shares.get("free", 0.15))) if explore_picker else rfq_budget
    n = 0
    for c in priced:
        if c.signature in known and n < free_cap and rankable(c) and take(c, "free"):
            n += 1

    if explore_picker is not None:
        n_explore = int(round(rfq_budget * shares.get("explore", 0.0)))
        c_explore = int(round(creation_budget * shares.get("explore", 0.0)))
        for c in explore_picker([x for x in cands if x.signature not in taken], n_explore, c_explore):
            take(c, "explore")

    n_unpriced = int(round(rfq_budget * shares.get("unpriced", 0.05)))
    rng.shuffle(unpriced)
    k = 0
    for c in unpriced:
        if k >= n_unpriced:
            break
        if take(c, "unpriced"):
            k += 1

    # Exploit: best expected EV, at most a couple of rungs per (leg, partner player/team) so one extreme
    # pair's ladder can't eat the pool. Whatever the other pools left unused flows here.
    exploit_order = sorted(priced, key=exploit_key, reverse=True) if exploit_key else priced
    per_pair: dict[tuple[str, str], int] = defaultdict(int)
    for c in exploit_order:
        if len(sel.chosen) >= rfq_budget:
            break
        if not rankable(c):
            continue
        keys = [(s.leg.ticker, _entity(o.leg)) for s in c.legs for o in c.legs if o is not s]
        if any(per_pair[k] >= max_rungs_per_pair for k in keys):
            continue
        if take(c, "exploit"):
            for k in keys:
                per_pair[k] += 1
    for c in exploit_order:  # budget still left after the per-pair cap: fill it in EV order
        if len(sel.chosen) >= rfq_budget:
            break
        if rankable(c):
            take(c, "exploit")
    sel.new_creations_planned = creations
    return sel
