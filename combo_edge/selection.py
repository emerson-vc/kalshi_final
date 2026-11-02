"""Split a run's RFQ and creation budget across candidate pools.

Pools:
- ``free``     combos that already exist (in the index from the RFQ feed or earlier runs): cost no creations
- ``exploit``  highest expected EV
- ``explore``  stratified exploration / bandit picks (Phase 1.5 / 2.5 supply these)
- ``unpriced`` a small random slice of candidates with an unpriced leg (never filter on liquidity)

Never create a combo for a candidate that isn't selected here (top N only).
"""

from __future__ import annotations

import random
from collections.abc import Callable
from dataclasses import dataclass, field

from combo_edge.candidates import Candidate


@dataclass
class Selection:
    chosen: list[Candidate] = field(default_factory=list)
    by_pool: dict[str, int] = field(default_factory=dict)
    new_creations_planned: int = 0


def _ev_key(c: Candidate) -> float:
    """Rank on EV at the fair model's lower bound, so noisy tail estimates can't crowd out solid ones."""
    v = c.expected_ev_lower if c.expected_ev_lower is not None else c.expected_ev
    return v if v is not None else -9.0


def select(
    cands: list[Candidate],
    *,
    rfq_budget: int,
    creation_budget: int,
    known: set[str],
    shares: dict[str, float],
    explore_picker: Callable[[list[Candidate], int, int], list[Candidate]] | None = None,
    seed: int | None = None,
) -> Selection:
    rng = random.Random(seed)
    sel = Selection()
    taken: set[str] = set()
    creations = 0

    def take(c: Candidate, pool: str) -> bool:
        nonlocal creations
        if c.signature in taken or len(sel.chosen) >= rfq_budget:
            return False
        needs_create = c.signature not in known
        if needs_create and creations >= creation_budget:
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

    # Free combos first: pricing them costs no creation budget.
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

    for c in priced:
        if len(sel.chosen) >= rfq_budget:
            break
        take(c, "exploit")
    sel.new_creations_planned = creations
    return sel
