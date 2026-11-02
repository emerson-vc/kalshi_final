"""Phase 1.5 designed experiment.

Testing every same-game 2-leg pair is impossible (hundreds of legs per game across ladders -> tens of
thousands of pairs vs 5,000 creations a week), so we sample:

- Candidates: for each game and each Level-1 cell (leg-type pair x relation), draw a few random pairs
  across ALL classified leg kinds, PROT legs included, PRECISE excluded. Level-2 attributes (tailness,
  sides) are randomized within the cell.
- Free combos: every 2-leg NFL same-game combo already in the index (mostly from the RFQ feed) whose legs
  are in today's universe becomes a candidate too. Pricing those costs no creation budget.
- Picker: cells below the ~30-quote target are filled least-sampled first (round robin). Once a cell is
  at target, budget shifts by Thompson sampling on the cell's mean edge (high mean or high uncertainty).
"""

from __future__ import annotations

import math
import random
from collections import defaultdict
from collections.abc import Callable

from combo_edge.candidates import Candidate, parse_signature
from combo_edge.learning.labels import edge_labels
from combo_edge.settings import RuntimeSettings
from combo_edge.sports.base import Leg, SidedLeg
from combo_edge.sports.nfl.implications import logical_class, structural_rule
from combo_edge.storage import Database
from combo_edge.strata import classify, leg_type, relation
from combo_edge.universe import Universe

PER_CELL_PER_GAME = 6


def _eligible(leg: Leg, include_prot: bool) -> bool:
    return leg.classified and not leg.precise and (include_prot or not leg.protected) and leg.outcome != "NONE"


def exploration_candidates(uni: Universe, st: RuntimeSettings, blocked: set[str], seed: int | None = None) -> list[Candidate]:
    rng = random.Random(seed)
    out: list[Candidate] = []
    for code, legs in uni.legs_by_game.items():
        pool = [l for l in legs if _eligible(l, st.prot_in_exploration)]
        by_type: dict[str, list[Leg]] = defaultdict(list)
        for l in pool:
            by_type[leg_type(SidedLeg(l, "yes"))].append(l)
        types = sorted(by_type)
        for i, ta in enumerate(types):
            for tb in types[i:]:
                cells: dict[str, list[Candidate]] = defaultdict(list)
                tries = 0
                while tries < PER_CELL_PER_GAME * 8 and sum(len(v) for v in cells.values()) < PER_CELL_PER_GAME * 3:
                    tries += 1
                    a = SidedLeg(rng.choice(by_type[ta]), rng.choice(("yes", "no")))
                    b = SidedLeg(rng.choice(by_type[tb]), rng.choice(("yes", "no")))
                    if structural_rule(a, b):
                        continue
                    lc = logical_class(a, b)
                    if lc and lc in blocked:
                        continue
                    rel = relation(a, b)
                    if len(cells[rel]) >= PER_CELL_PER_GAME:
                        continue
                    l1, _ = classify(a, b, None, None)
                    cand = Candidate((a, b), f"explore:{l1}", code)
                    if lc:
                        cand.notes.append(f"logical:{lc}")
                    cells[rel].append(cand)
                for v in cells.values():
                    out += v
    return out


def feed_candidates(db: Database, uni: Universe) -> list[Candidate]:
    """Combos other people already created (RFQ feed / earlier runs) whose legs are in today's universe."""
    out: list[Candidate] = []
    codes = list(uni.games)
    if not codes:
        return out
    ph = ",".join(f":g{i}" for i in range(len(codes)))
    rows = db.fetchall(f"SELECT ticker, signature FROM combos WHERE n_legs=2 AND source IN ('feed','rest','ours_resolved') AND game_code IN ({ph})",
                       {f"g{i}": c for i, c in enumerate(codes)})
    for r in rows:
        legs = []
        for t, side in parse_signature(r["signature"]):
            leg = uni.legs.get(t)
            if leg is None or leg.precise:
                legs = []
                break
            legs.append(SidedLeg(leg, side))
        if len(legs) == 2 and not structural_rule(legs[0], legs[1]):
            l1, _ = classify(legs[0], legs[1], None, None)
            c = Candidate(tuple(legs), f"feed:{l1}", legs[0].leg.game_code)
            c.combo_ticker, c.existed = r["ticker"], True
            out.append(c)
    return out


def cell_stats(db: Database, env: str) -> dict[str, tuple[int, float, float]]:
    """Per Level-1 stratum: (n quotes, mean edge, standard error), edges clipped to [-1, 3]."""
    groups: dict[str, list[float]] = defaultdict(list)
    for l in edge_labels(db, env):
        if l.get("stratum_l1"):
            groups[l["stratum_l1"]].append(max(-1.0, min(3.0, float(l["edge"]))))
    out = {}
    for k, v in groups.items():
        n = len(v)
        m = sum(v) / n
        sd = math.sqrt(sum((x - m) ** 2 for x in v) / (n - 1)) if n > 1 else 1.0
        out[k] = (n, m, max(sd, 0.05) / math.sqrt(n))
    return out


def make_picker(db: Database, env: str, st: RuntimeSettings, seed: int | None = None) -> Callable[[list[Candidate], int, int], list[Candidate]]:
    stats = cell_stats(db, env)
    rng = random.Random(seed)
    target = st.stratum_target_quotes

    def pick(cands: list[Candidate], n_rfq: int, n_create: int) -> list[Candidate]:
        by_cell: dict[str, list[Candidate]] = defaultdict(list)
        for c in cands:
            if c.stratum_l1:
                by_cell[c.stratum_l1].append(c)
        for v in by_cell.values():
            rng.shuffle(v)
            v.sort(key=lambda c: 1 if c.existed else 0)  # pop() takes from the end: free combos first within a cell
        under = sorted((k for k in by_cell if stats.get(k, (0, 0, 0))[0] < target), key=lambda k: stats.get(k, (0, 0, 0))[0])
        chosen: list[Candidate] = []
        creates = 0

        def take(c: Candidate) -> bool:
            nonlocal creates
            needs = not c.existed
            if needs and creates >= n_create:
                return False
            chosen.append(c)
            creates += int(needs)
            return True

        # 1) round-robin over under-sampled cells, least sampled first
        while len(chosen) < n_rfq and any(by_cell[k] for k in under):
            for k in under:
                if len(chosen) >= n_rfq:
                    break
                while by_cell[k]:
                    if take(by_cell[k].pop()):
                        break
        # After the exploration weeks, the explore slice favours pairs the simulator says are correlated
        # but no template covers (random correlated same-game pairs from the leg graph).
        if not st.exploration_mode:
            from combo_edge.learning.bandit import correlated

            for k in by_cell:
                by_cell[k].sort(key=lambda c: (0 if correlated(c) else 1, 0 if c.existed else 1), reverse=True)
        # 2) Thompson sampling over cells at target: draw a mean edge per cell from its posterior
        mature = [k for k in by_cell if k not in under and by_cell[k]]
        while len(chosen) < n_rfq and any(by_cell[k] for k in mature):
            draws = {k: rng.gauss(stats[k][1], stats[k][2]) for k in mature if by_cell[k]}
            if not draws:
                break
            best = max(draws, key=lambda k: draws[k])
            if not take(by_cell[best].pop()):
                by_cell[best].clear()
        return chosen

    return pick
