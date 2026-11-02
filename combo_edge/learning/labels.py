"""Edge labels, recomputed on demand from the raw quote log (never stored), so they improve whenever
the fair model does.

edge = fair_joint x combo_payout - 1   (== fair joint / implied quote probability - 1, fees included)

For re-quotes (no candidate of their own) the fair comes from the most recent candidate row with the
same signature, or from ``fair_override`` (Phase 2 re-pricing).
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Callable
from typing import Any

from combo_edge import timeutil
from combo_edge.storage import Database, loads

HOUR_BUCKETS = [(144, 1e9, "6d+"), (96, 144, "4-6d"), (72, 96, "3-4d"), (48, 72, "2-3d"), (24, 48, "1-2d"),
                (12, 24, "12-24h"), (6, 12, "6-12h"), (1, 6, "1-6h"), (-1e9, 1, "<1h")]
LIQ_BUCKETS = [(0.0, 0.02, "tight (<=2c)"), (0.02, 0.05, "2-5c"), (0.05, 0.10, "5-10c"), (0.10, 1.01, "wide (>10c)")]


def hours_bucket(h: float | None) -> str:
    if h is None:
        return "?"
    for lo, hi, name in HOUR_BUCKETS:
        if lo <= h < hi:
            return name
    return "?"


def liq_bucket(spread: float | None) -> str:
    if spread is None:
        return "no book"
    for lo, hi, name in LIQ_BUCKETS:
        if lo <= spread < hi:
            return name
    return "wide (>10c)"


def edge_labels(db: Database, env: str, since: float | None = None, fair_override: Callable[[str], float | None] | None = None) -> list[dict[str, Any]]:
    rows = db.fetchall(
        "SELECT r.rfq_id, r.run_id, r.signature, r.purpose, r.created_at, r.hours_to_start, r.combo_payout, r.n_makers, r.offset_h, "
        "r.best_no_bid, r.second_no_bid, r.target_cost, r.feed_demand_pair, c.family, c.stratum_l1, c.stratum_l2, c.fair_joint, c.fair_lower, "
        "c.legs, c.existed, c.game_code, c.leg_payout_product, c.p_a, c.p_b, c.team_total_main, c.game_total_main, c.spread_main "
        "FROM rfqs r LEFT JOIN candidates c ON r.candidate_id = c.id "
        "WHERE r.env = :e AND r.outcome = 'quoted' AND r.combo_payout IS NOT NULL AND r.created_at >= :t",
        {"e": env, "t": since or 0.0},
    )
    need = {r["signature"] for r in rows if r["fair_joint"] is None}
    latest: dict[str, dict[str, Any]] = {}
    if need:
        for c in db.fetchall(
            "SELECT signature, family, stratum_l1, stratum_l2, fair_joint, fair_lower, legs, existed, game_code, leg_payout_product, p_a, p_b, "
            "team_total_main, game_total_main, spread_main FROM candidates WHERE fair_joint IS NOT NULL ORDER BY id"
        ):
            if c["signature"] in need:
                latest[c["signature"]] = c
    spreads = _leg_spreads(db, {r["run_id"] for r in rows if r["run_id"] is not None})
    out = []
    for r in rows:
        base = dict(r)
        if base["fair_joint"] is None and r["signature"] in latest:
            for k, v in latest[r["signature"]].items():
                if base.get(k) is None:
                    base[k] = v
        fair = fair_override(r["signature"]) if fair_override else None
        fair = fair if fair is not None else base["fair_joint"]
        if fair is None:
            continue
        payout = float(r["combo_payout"])
        legs = loads(base.get("legs"), []) or []
        sp = [spreads.get((r["run_id"], t)) for t, _ in legs]
        sp = [s for s in sp if s is not None]
        created = float(r["created_at"] or 0)
        lt = timeutil.local_dt(created) if created else None
        ratio = (payout / base["leg_payout_product"]) if base.get("leg_payout_product") else None
        out.append({
            **base,
            "fair": fair,
            "edge": fair * payout - 1.0,
            "ratio": ratio,
            "log_ratio": math.log(ratio) if ratio and ratio > 0 else None,
            "hours_bucket": hours_bucket(r["hours_to_start"]),
            "worst_leg_spread": max(sp) if sp else None,
            "liq_bucket": liq_bucket(max(sp) if sp else None),
            "dow": lt.strftime("%a") if lt else "?",
            "hour": lt.hour if lt else None,
            "quote_spread": (r["best_no_bid"] - r["second_no_bid"]) if (r["best_no_bid"] and r["second_no_bid"]) else None,
        })
    return out


def _leg_spreads(db: Database, run_ids: set[int]) -> dict[tuple[int, str], float]:
    out: dict[tuple[int, str], float] = {}
    for rid in run_ids:
        for s in db.fetchall("SELECT ticker, spread FROM leg_snapshots WHERE run_id=:r", {"r": rid}):
            if s["spread"] is not None:
                out[(rid, s["ticker"])] = float(s["spread"])
    return out


def mean_ci(values: list[float], z: float = 1.96) -> tuple[float, float, float, int]:
    """Mean with a normal-approx 95% CI (bootstrap-free; fine for n >= ~10)."""
    n = len(values)
    if n == 0:
        return (math.nan, math.nan, math.nan, 0)
    m = sum(values) / n
    if n == 1:
        return (m, math.nan, math.nan, 1)
    var = sum((v - m) ** 2 for v in values) / (n - 1)
    half = z * math.sqrt(var / n)
    return (m, m - half, m + half, n)


def group_edges(labels: list[dict[str, Any]], key: str, order: list[str] | None = None, clip: float = 3.0) -> list[dict[str, Any]]:
    """Mean edge (clipped at +-clip to tame 50x outliers) with CI per group."""
    groups: dict[str, list[float]] = defaultdict(list)
    for l in labels:
        groups[str(l.get(key))].append(max(-1.0, min(clip, float(l["edge"]))))
    names = order if order else sorted(groups)
    out = []
    for name in names:
        if name not in groups:
            continue
        m, lo, hi, n = mean_ci(groups[name])
        out.append({"group": name, "mean": m, "lo": lo, "hi": hi, "n": n})
    return out
