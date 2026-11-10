"""Where does a run's RFQ budget go? Offline diagnostic on the mock exchange (the repo's recorded
CHI@SEA and JAC@BAL markets), using the same candidate generation, prescore and pool split as a real
run (``scan.plan_selection``). Creates nothing and sends nothing.

    uv run python scripts/diagnose_selection.py [--budget 500]

Prints:
1. count and median predicted EV (and EV at the lower bound) by payout bucket: every candidate, then
   the YES/YES template-family pairs
2. each pool's mix by payout bucket and by family (core sweeps by sweep)
3. the top 15 candidates by the ranking key (EV at the lower bound)
"""

from __future__ import annotations

import argparse
import asyncio
import os
import statistics
import sys
import tempfile
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _isolate() -> None:
    """Temp data dir (no real DB touched), mock env, live forbidden, the real .env never read."""
    tmp = Path(tempfile.mkdtemp(prefix="combo-edge-diag-"))
    os.environ.update({"COMBO_EDGE_DATA_DIR": str(tmp / "data"), "KALSHI_ENV": "mock", "COMBO_EDGE_FORBID_LIVE": "1",
                       "COMBO_EDGE_ENV_FILE": str(tmp / "none.env")})
    (tmp / "data").mkdir(parents=True)
    cache = ROOT / "data" / "nflverse"
    if cache.is_dir():  # reuse the nflverse cache so positions and usage match a real run
        (tmp / "data" / "nflverse").symlink_to(cache, target_is_directory=True)
    sys.path.insert(0, str(ROOT))


def family_of(c) -> str:  # type: ignore[no-untyped-def]
    core = next((n.split(":", 1)[1] for n in c.notes if n.startswith("core:")), None)
    if c.pool == "core" and core:
        return f"core:{core}"
    return c.family.split(":", 1)[0] + ":*" if ":" in c.family else c.family


def pct(v: float | None) -> str:
    return "   n/a" if v is None else f"{100 * v:+6.0f}%"


async def main(budget: int) -> None:
    _isolate()
    from combo_edge.candidates import Candidate
    from combo_edge.context import default_db
    from combo_edge.diagnostics import BUCKETS, bucket, payout_of, plan_offline
    from combo_edge.mock.exchange import reset_mock
    from combo_edge.selection import _ev_key

    reset_mock(seed=11)
    plan = await plan_offline(default_db(), "mock", budget)
    uni, cands, sel, st = plan.universe, plan.candidates, plan.selection, plan.settings

    names = [b[2] for b in BUCKETS] + ["none"]
    print(f"universe: {len(uni.games)} games, {len(uni.legs)} legs; {len(cands)} candidates; budget {budget}; "
          f"split {st.budget_shares()}")

    print("\n1) All candidates by expected payout")
    print(f"   {'bucket':<8} {'count':>7} {'median EV':>10} {'median EV@lower':>16}")
    by_b: dict[str, list[Candidate]] = defaultdict(list)
    for c in cands:
        by_b[bucket(payout_of(c))].append(c)
    for b in names:
        cs = by_b.get(b, [])
        evs = [c.expected_ev for c in cs if c.expected_ev is not None]
        lows = [c.expected_ev_lower for c in cs if c.expected_ev_lower is not None]
        print(f"   {b:<8} {len(cs):>7} {pct(statistics.median(evs) if evs else None):>10} {pct(statistics.median(lows) if lows else None):>16}")

    print("\n   Template families, YES on both legs (the star-tail x team-outcome patterns)")
    by_b = defaultdict(list)
    for c in cands:
        if ":" not in c.family and not c.family.startswith("no_") and all(x.side == "yes" for x in c.legs):
            by_b[bucket(payout_of(c))].append(c)
    for b in names:
        cs = by_b.get(b, [])
        evs = [c.expected_ev for c in cs if c.expected_ev is not None]
        lows = [c.expected_ev_lower for c in cs if c.expected_ev_lower is not None]
        print(f"   {b:<8} {len(cs):>7} {pct(statistics.median(evs) if evs else None):>10} {pct(statistics.median(lows) if lows else None):>16}")

    print("\n2) Selected RFQs by pool")
    pools: dict[str, list[Candidate]] = defaultdict(list)
    for c in sel.chosen:
        pools[c.pool].append(c)
    print(f"   {'pool':<9} {'n':>4}  " + " ".join(f"{b:>7}" for b in names))
    for pool in ("core", "exploit", "free", "explore", "unpriced"):
        cs = pools.get(pool, [])
        cnt = Counter(bucket(payout_of(c)) for c in cs)
        print(f"   {pool:<9} {len(cs):>4}  " + " ".join(f"{cnt.get(b, 0):>7}" for b in names))
    ce = pools.get("core", []) + pools.get("exploit", [])
    in_band = sum(1 for c in ce if (payout_of(c) or 0) >= 8 and (payout_of(c) or 0) <= 50)
    print(f"   core+exploit inside 8x-50x: {in_band}/{len(ce)}" + (f" ({100 * in_band / len(ce):.0f}%)" if ce else ""))
    for pool in ("core", "exploit", "free", "explore", "unpriced"):
        cs = pools.get(pool, [])
        if not cs:
            continue
        fams = Counter(family_of(c) for c in cs).most_common(10)
        print(f"   {pool} by family: " + ", ".join(f"{f} {n}" for f, n in fams))

    print("\n3) Top 15 candidates by ranking key (EV at the lower bound)")
    top = sorted((c for c in cands if c.expected_ev is not None), key=_ev_key, reverse=True)[:15]
    for c in top:
        pay = payout_of(c)
        print(f"   {pct(c.expected_ev)} (low {pct(c.expected_ev_lower).strip()})  {pay or 0:>6.1f}x  [{c.pool or '-':<8}] {c.family:<24} {c.describe()}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--budget", type=int, default=500)
    asyncio.run(main(ap.parse_args().budget))
