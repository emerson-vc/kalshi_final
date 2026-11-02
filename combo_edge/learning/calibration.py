"""Learning loop 2, the truth model: is the fair joint probability calibrated?

Two checks, reported per family:
1. History (now): the simulator's structural conditionals vs nflverse reality at the same implied team
   total, e.g. P(team pts > 28.5 | QB pass TD >= 4) or P(top WR >= 100 yds | QB >= 300 yds). The gap per
   family widens that family's uncertainty band (fair_lower), so execution stays conservative where the
   model is off.
2. Settled outcomes (as they accrue): Brier score and reliability bins of fair_joint vs whether probed
   combos actually hit. Win/loss is never used as a training reward; this only measures calibration.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from typing import Any

import numpy as np

from combo_edge import timeutil
from combo_edge.storage import Database, loads

log = logging.getLogger(__name__)


def _sim_conditionals(implied: float, n: int = 20000, seed: int = 0) -> dict[str, float]:
    from combo_edge.fair.simulator import GameSim, ReceiverSpec, RusherSpec, SimParams, TeamSpec, fit_team_scoring

    ptd, pfg, _ = fit_team_scoring([], 0.12, mean_hint=implied)
    opp_ptd, opp_pfg, _ = fit_team_scoring([], 0.12, mean_hint=44.0 - implied)
    team = TeamSpec("T", ptd, pfg, qb_key="QB",
                    receivers=[ReceiverSpec("WR1", 0.27, 0.65, 13.0, 0.06), ReceiverSpec("WR2", 0.19, 0.64, 12.0, 0.05),
                               ReceiverSpec("TE", 0.15, 0.7, 10.0, 0.06), ReceiverSpec("_o", 0.39, 0.66, 9.0, 0.04)],
                    rushers=[RusherSpec("RB", 0.62, 4.3, 0.035), RusherSpec("_o", 0.38, 4.2, 0.03)])
    opp = TeamSpec("O", opp_ptd, opp_pfg, qb_key="OQB", receivers=[ReceiverSpec("_o", 1.0, 0.65, 11, 0.05)], rushers=[RusherSpec("_o", 1.0, 4.2, 0.03)])
    s = GameSim("cal", team, opp, SimParams(n=n, sigma=0.12, rho=0.05, seed=seed)).run().stats
    pts = s[("team", "T", "points")]
    qbtd = s[("player", "QB", "pass_td")]
    qby = s[("player", "QB", "pass_yds")]
    wr_y = np.maximum(s[("player", "WR1", "rec_yds")], s[("player", "WR2", "rec_yds")])
    wr_td = np.maximum(s[("player", "WR1", "rec_td")], s[("player", "WR2", "rec_td")])
    out: dict[str, float] = {}
    for k in (3, 4):
        m = qbtd >= k
        if m.sum() > 50:
            out[f"qb_ptd_x_team_total|k{k}|x28.5"] = float((pts[m] > 28.5).mean())
    m = wr_td >= 2
    if m.sum() > 50:
        out["wr_td_x_team_total|k2|x28.5"] = float((pts[m] > 28.5).mean())
    m = qby >= 300
    if m.sum() > 50:
        out["qb_pyds_x_rec_yds|y300|w100"] = float((wr_y[m] >= 100).mean())
    return out


def _hist_conditionals(tg: Any, implied: float, band: float = 2.0) -> dict[str, tuple[float, int]]:
    import polars as pl

    df = tg.filter((pl.col("implied") - implied).abs() <= band)
    out: dict[str, tuple[float, int]] = {}
    if df.height == 0:
        return out
    pts = df["pts"].to_numpy().astype(float)
    if "qb_pass_td" in df.columns:
        qb = df["qb_pass_td"].fill_null(0).to_numpy()
        for k in (3, 4):
            m = qb >= k
            if m.sum() >= 15:
                out[f"qb_ptd_x_team_total|k{k}|x28.5"] = (float((pts[m] > 28.5).mean()), int(m.sum()))
        rt = df["top_rec_td"].fill_null(0).to_numpy()
        m = rt >= 2
        if m.sum() >= 15:
            out["wr_td_x_team_total|k2|x28.5"] = (float((pts[m] > 28.5).mean()), int(m.sum()))
        qy = df["qb_pass_yds"].fill_null(0).to_numpy()
        wy = df["top_rec_yds"].fill_null(0).to_numpy()
        m = qy >= 300
        if m.sum() >= 15:
            out["qb_pyds_x_rec_yds|y300|w100"] = (float((wy[m] >= 100).mean()), int(m.sum()))
    return out


def history_calibration(tg: Any) -> dict[str, Any]:
    rows = []
    for implied in (20.5, 23.5, 26.5):
        sim = _sim_conditionals(implied)
        hist = _hist_conditionals(tg, implied) if tg is not None else {}
        for key, p_sim in sim.items():
            if key in hist:
                p_h, n = hist[key]
                rows.append({"check": key, "implied": implied, "model": p_sim, "history": p_h, "n": n, "gap": p_sim - p_h})
    by_family: dict[str, list[float]] = defaultdict(list)
    for r in rows:
        by_family[r["check"].split("|")[0]].append(abs(r["gap"]) / max(r["history"], 0.05))
    family_u = {f: float(min(0.30, np.mean(v))) for f, v in by_family.items()}
    return {"rows": rows, "family_u": family_u}


def outcome_calibration(db: Database, env: str) -> dict[str, Any]:
    """Brier + reliability of fair_joint on probed combos whose legs have settled."""
    results = {r["ticker"]: (r["result"] or "").lower() for r in db.fetchall("SELECT ticker, result FROM legs WHERE result IS NOT NULL")}
    rows = db.fetchall("SELECT c.family, c.fair_joint, c.legs FROM candidates c JOIN rfqs r ON r.candidate_id = c.id "
                       "WHERE r.env = :e AND c.fair_joint IS NOT NULL", {"e": env})
    by_fam: dict[str, list[tuple[float, int]]] = defaultdict(list)
    for r in rows:
        legs = loads(r["legs"], []) or []
        outs = [results.get(t) for t, _ in legs]
        if any(o not in ("yes", "no") for o in outs):
            continue
        hit = all(o == side for o, (_, side) in zip(outs, legs))
        by_fam[r["family"]].append((float(r["fair_joint"]), int(hit)))
    out = {}
    for fam, pairs in by_fam.items():
        p = np.array([a for a, _ in pairs])
        y = np.array([b for _, b in pairs])
        out[fam] = {"n": len(pairs), "brier": float(np.mean((p - y) ** 2)), "mean_fair": float(p.mean()), "hit_rate": float(y.mean())}
    return out


def run_calibration(db: Database, env: str) -> dict[str, Any]:
    from combo_edge.fair.history import team_games

    try:
        tg = team_games()
    except Exception as exc:  # noqa: BLE001
        log.warning("history unavailable for calibration: %s", exc)
        tg = None
    hist = history_calibration(tg) if tg is not None else {"rows": [], "family_u": {}}
    outc = outcome_calibration(db, env)
    lines = ["Simulator vs nflverse history (same implied team total):"]
    for r in hist["rows"]:
        lines.append(f"  {r['check']} @implied {r['implied']}: model {r['model']:.2f} vs history {r['history']:.2f} (n={r['n']})")
    lines.append("Settled probed combos (fair vs hit rate):" if outc else "Settled probed combos: none yet")
    for fam, d in sorted(outc.items()):
        lines.append(f"  {fam}: n={d['n']} mean fair {d['mean_fair']:.3f} vs hit {d['hit_rate']:.3f} (Brier {d['brier']:.4f})")
    res = {"history": hist, "outcomes": outc, "text": "\n".join(lines), "ts": timeutil.now()}
    db.meta_set(f"calibration:{env}", res)
    return res
