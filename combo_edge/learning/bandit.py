"""Contextual bandit for the probe policy (Thompson sampling).

Arms are Level-1 strata (leg-type pair x relation); the context is each candidate's predicted edge from
the fair model + maker model. Each run:
- the exploit slice (most of the budget) ranks candidates by predicted EV at the lower bound, shifted
  by a Thompson draw from its arm's posterior of realized edge, so arms that keep paying get more;
- the explore slice (a fixed ~15% after exploration) goes to under-sampled strata, plus random same-game
  pairs from the leg graph that the simulator says are correlated but no template covers.

Reward = EV at the quote (model fair x quoted payout - 1) plus closing-line value from re-quotes; the EV
part counts half until the arm has 10 CLV observations. Never win/loss: on 10x-50x payouts it is far too
noisy at our volume.
"""

from __future__ import annotations

import math
import random
from collections import defaultdict
from collections.abc import Callable
from typing import Any

from combo_edge.candidates import Candidate
from combo_edge.learning.labels import edge_labels
from combo_edge.storage import Database

CLV_WEIGHT = 0.5
MIN_CLV_OBS = 10  # arms with fewer CLV observations...
UNPROVEN_EV_WEIGHT = 0.5  # ...get half their model-EV reward
PRIOR_N = 5.0
PRIOR_SD = 0.5


def rewards(db: Database, env: str, min_clv_obs: int = MIN_CLV_OBS) -> list[tuple[str, float]]:
    """Per probe quote: model EV at the quote + CLV. Until an arm has ``min_clv_obs`` CLV observations
    its model-EV part counts half: model EV alone is the quantity most likely to be wrong in the tails."""
    labels = [l for l in edge_labels(db, env) if l.get("stratum_l1") and l["purpose"] == "probe"]
    clv = closing_values(db, env)
    n_clv: dict[str, int] = defaultdict(int)
    for l in labels:
        if l["rfq_id"] in clv:
            n_clv[l["stratum_l1"]] += 1
    out = []
    for l in labels:
        r = max(-1.0, min(3.0, float(l["edge"])))
        if n_clv[l["stratum_l1"]] < min_clv_obs:
            r *= UNPROVEN_EV_WEIGHT
        c = clv.get(l["rfq_id"])
        if c is not None:
            r += CLV_WEIGHT * max(-1.0, min(1.0, c))
        out.append((l["stratum_l1"], r))
    return out


def closing_values(db: Database, env: str) -> dict[str, float]:
    """CLV per probe RFQ: probe payout / last pre-kickoff re-quote payout - 1 (positive = beat the close)."""
    rows = db.fetchall(
        "SELECT p.rfq_id, p.combo_payout AS pay, q.combo_payout AS close_pay FROM rfqs p JOIN rfqs q ON q.combo_ticker = p.combo_ticker "
        "WHERE p.env=:e AND p.purpose='probe' AND q.purpose='requote' AND q.created_at > p.created_at AND q.offset_h <= 6 "
        "AND p.combo_payout IS NOT NULL AND q.combo_payout IS NOT NULL ORDER BY q.created_at",
        {"e": env},
    )
    out: dict[str, float] = {}
    for r in rows:
        out[r["rfq_id"]] = float(r["pay"]) / float(r["close_pay"]) - 1.0
    return out


class Posteriors:
    def __init__(self, data: list[tuple[str, float]]):
        acc: dict[str, list[float]] = defaultdict(list)
        for arm, r in data:
            acc[arm].append(r)
        self.stats: dict[str, tuple[float, float, int]] = {}
        allr = [r for _, r in data]
        self.prior_mean = sum(allr) / len(allr) if allr else 0.0
        for arm, v in acc.items():
            n = len(v)
            m = (sum(v) + PRIOR_N * self.prior_mean) / (n + PRIOR_N)
            sd = math.sqrt(sum((x - sum(v) / n) ** 2 for x in v) / max(1, n - 1)) if n > 1 else PRIOR_SD
            self.stats[arm] = (m, max(sd, 0.05) / math.sqrt(n + PRIOR_N), n)

    def draw(self, arm: str, rng: random.Random) -> float:
        m, se, _ = self.stats.get(arm, (self.prior_mean, PRIOR_SD, 0))
        return rng.gauss(m, se)

    def prob_above(self, arm: str, threshold: float) -> float:
        m, se, _ = self.stats.get(arm, (self.prior_mean, PRIOR_SD, 0))
        return 0.5 * (1 + math.erf((m - threshold) / (se * math.sqrt(2))))


def thompson_key(post: Posteriors, seed: int | None = None, weight: float = 0.5) -> Callable[[Candidate], float]:
    rng = random.Random(seed)
    draws: dict[str, float] = {}

    def key(c: Candidate) -> float:
        base = c.expected_ev_lower if c.expected_ev_lower is not None else (c.expected_ev if c.expected_ev is not None else -9.0)
        arm = c.stratum_l1 or c.family
        if arm not in draws:
            draws[arm] = post.draw(arm, rng)
        return (1 - weight) * base + weight * draws[arm]

    return key


def correlated(c: Candidate, min_lift: float = 0.15) -> bool:
    """True if the simulator says the pair is meaningfully dependent (|lift - 1| > min_lift)."""
    if c.fair_joint is None or len(c.p_legs) != 2 or not all(c.p_legs):
        return False
    indep = (c.p_legs[0] or 0) * (c.p_legs[1] or 0)
    return indep > 0 and abs(c.fair_joint / indep - 1.0) > min_lift


def summary(post: Posteriors, top: int = 10) -> list[dict[str, Any]]:
    rows = [{"arm": a, "mean": m, "se": se, "n": n} for a, (m, se, n) in post.stats.items()]
    return sorted(rows, key=lambda r: -r["mean"])[:top]


def family_rewards(db: Database, env: str) -> list[tuple[str, float]]:
    """(family, reward) per probe quote, the same reward the strata arms use."""
    labels = edge_labels(db, env)
    clv = closing_values(db, env)
    out = []
    for l in labels:
        if not l.get("family") or l["purpose"] != "probe":
            continue
        r = max(-1.0, min(3.0, float(l["edge"])))
        c = clv.get(l["rfq_id"])
        if c is not None:
            r += CLV_WEIGHT * max(-1.0, min(1.0, c))
        out.append((l["family"], r))
    return out


def released_families(db: Database, env: str, gated: set[str], min_quotes: int = 30) -> set[str]:
    """Explore-only families that have earned exploit/core access: promoted, or a positive posterior mean
    over at least ``min_quotes`` probe quotes."""
    if not gated:
        return set()
    promoted = {r["family_key"] for r in db.fetchall("SELECT family_key FROM promotions WHERE active=1")}
    post = Posteriors(family_rewards(db, env))
    out = set()
    for f in gated:
        m, _se, n = post.stats.get(f, (0.0, PRIOR_SD, 0))
        if f in promoted or f"promoted:{f}" in promoted or (n >= min_quotes and m > 0):
            out.add(f)
    return out
