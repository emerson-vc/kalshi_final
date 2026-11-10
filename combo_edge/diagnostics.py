"""Offline selection pipeline for diagnostics and tests: universe -> candidates -> prescore -> pool split,
exactly as ``run_scan`` does it, but with no combo creation, no RFQs and no database writes beyond the
caller's own (temporary) DB. Used by ``scripts/diagnose_selection.py`` and the selection tests.
"""

from __future__ import annotations

import random
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterator

from combo_edge.candidates import Candidate
from combo_edge.selection import Selection
from combo_edge.settings import RuntimeSettings
from combo_edge.storage import Database
from combo_edge.universe import Universe

BUCKETS = [(0, 8, "<8x"), (8, 15, "8-15x"), (15, 25, "15-25x"), (25, 40, "25-40x"), (40, 50, "40-50x"), (50, 100, "50-100x"),
           (100, float("inf"), "100x+")]


@dataclass
class Plan:
    universe: Universe
    candidates: list[Candidate]
    selection: Selection
    settings: RuntimeSettings
    fair_model: Any


def payout_of(c: Candidate) -> float | None:
    return c.expected_payout if c.expected_payout is not None else c.leg_payout_product


def bucket(x: float | None) -> str:
    if x is None:
        return "none"
    return next(name for lo, hi, name in BUCKETS if lo <= x < hi)


class _Seeded(random.Random):
    def __init__(self, seed: Any = None) -> None:
        super().__init__(0 if seed is None else seed)


@contextmanager
def _deterministic() -> Iterator[None]:
    """Unseeded Random() instances (exploration sampling, Thompson draws) get seed 0, so runs compare."""
    orig = random.Random
    random.Random = _Seeded  # type: ignore[misc]
    try:
        yield
    finally:
        random.Random = orig  # type: ignore[misc]


async def plan_offline(db: Database, env: str = "mock", budget: int = 500, use_roster: bool = True, calibrate: bool = True) -> Plan:
    """``calibrate``: apply the nflverse history calibration (family shifts and bands) as a production run
    does after its daily calibration; skipped when no history is cached."""
    with _deterministic():
        if calibrate:
            _calibrate(db, env)
        return await _plan(db, env, budget, use_roster)


def _calibrate(db: Database, env: str) -> None:
    from combo_edge import timeutil
    from combo_edge.fair.history import team_games
    from combo_edge.learning.calibration import history_calibration

    try:
        tg = team_games()
    except Exception:  # noqa: BLE001
        tg = None
    if tg is not None:
        db.meta_set(f"calibration:{env}", {"history": history_calibration(tg), "outcomes": {}, "ts": timeutil.now()})


async def _plan(db: Database, env: str, budget: int, use_roster: bool) -> Plan:
    from combo_edge.context import make_clients
    from combo_edge.hooks import default_hooks
    from combo_edge.prescore import Prescorer, load_priors
    from combo_edge.scan import generate_candidates, plan_selection, runtime_settings
    from combo_edge.universe import build_universe

    st = runtime_settings(db)
    hooks = default_hooks()
    roster = None
    if use_roster:
        try:
            from combo_edge.fair.nflverse import roster_index

            roster = roster_index()
        except Exception:  # noqa: BLE001
            roster = None
    clients = make_clients(env, db)
    try:
        uni = await build_universe(clients.api, st.collection_ticker, hours_ahead=st.hours_ahead_max,
                                   skip_starting_within_min=st.skip_starting_within_min, roster=roster)
    finally:
        await clients.aclose()
    for prep in hooks.prepare:
        prep(db, env)
    cands = generate_candidates(uni, None, set())
    seen = {c.signature for c in cands}
    for c in hooks.extra_candidates(uni, st, db, set()) if hooks.extra_candidates else []:
        if c.signature not in seen:
            seen.add(c.signature)
            cands.append(c)
    fm = hooks.fair_model_factory(uni) if hooks.fair_model_factory else None
    mm = hooks.maker_model_factory(db, env, uni) if hooks.maker_model_factory else None
    Prescorer(load_priors(), fm, mm).score(cands)
    sel, _ = plan_selection(cands, db=db, env=env, st=st, hooks=hooks, rfq_budget=budget, creation_budget=budget, known=set(),
                            recent=set(), uni=uni)
    return Plan(uni, cands, sel, st, fm)
