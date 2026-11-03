"""Split a run's RFQ and creation budget across candidate pools.

Pools:
- ``core``     the proven patterns in ``config/core.yaml``, swept rung by rung (round-robin across players)
- ``free``     combos that already exist (in the index from the RFQ feed or earlier runs): cost no creations
- ``exploit``  highest expected EV
- ``explore``  stratified exploration / bandit picks (Phase 1.5 / 2.5 supply these)
- ``unpriced`` a small random slice of candidates with an unpriced leg (never filter on liquidity)

Never create a combo for a candidate that isn't selected here (top N only).
"""

from __future__ import annotations

import random
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from combo_edge.candidates import Candidate
from combo_edge.core import Sweep, core_match


@dataclass
class Selection:
    chosen: list[Candidate] = field(default_factory=list)
    by_pool: dict[str, int] = field(default_factory=dict)
    new_creations_planned: int = 0


def _ev_key(c: Candidate) -> float:
    """Rank on EV at the fair model's lower bound, so noisy tail estimates can't crowd out solid ones."""
    v = c.expected_ev_lower if c.expected_ev_lower is not None else c.expected_ev
    return v if v is not None else -9.0


def _entity(leg: Any) -> str:
    return leg.player_key or f"{leg.team}:{leg.kind}"


def _spread_rungs(group: list[Candidate]) -> list[Candidate]:
    """Order one pair's rungs so each pick uses the least-used rung on each leg (best EV breaks ties).
    The model's EV peaks at the extreme rungs; this walks the whole ladder (e.g. 3+/4+/5+ × o17.5…o31.5)
    instead of sending only the extremes first."""
    left = sorted(group, key=_ev_key, reverse=True)
    uses: dict[str, int] = defaultdict(int)
    out: list[Candidate] = []
    while left:
        i = min(range(len(left)), key=lambda j: sum(uses[s.leg.ticker] for s in left[j].legs))
        c = left.pop(i)
        for s in c.legs:
            uses[s.leg.ticker] += 1
        out.append(c)
    return out


def round_robin_core(cands: list[Candidate], sweeps: list[Sweep], recently_quoted: set[str]) -> list[Candidate]:
    """Core candidates in sweep order: one rung per (game, sweep, player/team pair) per round, best EV
    first within a group; combos quoted recently (last runs) go after everything not yet covered."""
    order: list[Candidate] = []
    for fresh in (True, False):
        groups: dict[tuple[str, ...], list[Candidate]] = defaultdict(list)
        for c in cands:
            sw = core_match(c, sweeps)
            if sw is not None and (c.signature not in recently_quoted) == fresh:
                c.notes.append(f"core:{sw.name}")
                groups[sw.group(c)].append(c)
        queues = sorted((_spread_rungs(g) for g in groups.values()), key=lambda g: _ev_key(g[0]), reverse=True)
        depth = 0
        while any(depth < len(q) for q in queues):
            order.extend(q[depth] for q in queues if depth < len(q))
            depth += 1
    return order


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
    recently_quoted: set[str] | None = None,
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

    priced = sorted((c for c in cands if not c.unpriced and c.expected_ev is not None), key=_ev_key, reverse=True)
    unpriced = [c for c in cands if c.unpriced]

    # Core sweeps first: the proven patterns, every rung in config/core.yaml, spread across players.
    if core:
        n_core = int(round(rfq_budget * shares.get("core", 0.0)))
        c_core = creations + int(round(creation_budget * shares.get("core", 0.0)))
        n = 0
        for c in round_robin_core(cands, core, recently_quoted or set()):
            if n >= n_core:
                break
            if take(c, "core", c_core):
                n += 1

    # Free combos next: pricing them costs no creation budget.
    free_cap = int(round(rfq_budget * shares.get("free", 0.15))) if explore_picker else rfq_budget
    n = 0
    for c in priced:
        if c.signature in known and n < free_cap and take(c, "free"):
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
        keys = [(s.leg.ticker, _entity(o.leg)) for s in c.legs for o in c.legs if o is not s]
        if any(per_pair[k] >= max_rungs_per_pair for k in keys):
            continue
        if take(c, "exploit"):
            for k in keys:
                per_pair[k] += 1
    for c in exploit_order:  # budget still left after the per-pair cap: fill it in EV order
        if len(sel.chosen) >= rfq_budget:
            break
        take(c, "exploit")
    sel.new_creations_planned = creations
    return sel
