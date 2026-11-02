"""Historical NFL tables from nflverse (free) and what we learn from them.

- ``team_games``: one row per team-game: points, the closing-line implied team total
  ((total_line +/- spread_line)/2; nflverse ``spread_line`` > 0 means the home team is favored),
  team passing/rushing TDs, the starting QB's passing TDs.
- ``scoring_hyperparams``: residual SD of team points around the implied total and the correlation of
  the two teams' residuals, used to set the simulator's latent scoring variance.
- ``conditional_table``: P(team pts > X | QB pass TD >= k), conditioned on the implied team total, by
  logistic regression (smooth in the tails where raw frequencies are thin). Reported per family and used
  to check the simulator (the truth-model calibration in Phase 2.5).
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from typing import Any

import numpy as np

from combo_edge.fair import nflverse as nv

log = logging.getLogger(__name__)
HIST_SEASONS = list(range(2018, 2026))


def team_games(seasons: list[int] | None = None) -> Any:
    import polars as pl

    seasons = seasons or HIST_SEASONS
    sc = nv.schedules(None)
    if sc is None:
        return None
    sc = sc.filter(pl.col("season").is_in(seasons) & pl.col("home_score").is_not_null() & pl.col("spread_line").is_not_null() & pl.col("total_line").is_not_null())
    home = sc.select(pl.col("game_id"), pl.col("season"), pl.col("week"), pl.col("home_team").alias("team"), pl.col("away_team").alias("opp"),
                     pl.col("home_score").alias("pts"), pl.col("away_score").alias("opp_pts"),
                     ((pl.col("total_line") + pl.col("spread_line")) / 2).alias("implied"),
                     ((pl.col("total_line") - pl.col("spread_line")) / 2).alias("opp_implied"), pl.lit(1).alias("home"))
    away = sc.select(pl.col("game_id"), pl.col("season"), pl.col("week"), pl.col("away_team").alias("team"), pl.col("home_team").alias("opp"),
                     pl.col("away_score").alias("pts"), pl.col("home_score").alias("opp_pts"),
                     ((pl.col("total_line") - pl.col("spread_line")) / 2).alias("implied"),
                     ((pl.col("total_line") + pl.col("spread_line")) / 2).alias("opp_implied"), pl.lit(0).alias("home"))
    tg = pl.concat([home, away])
    ps = nv.player_stats(seasons)
    if ps is not None:
        agg = ps.group_by(["season", "week", "team"]).agg(
            pl.col("passing_tds").sum().alias("team_pass_td"),
            pl.col("rushing_tds").sum().alias("team_rush_td"),
            pl.col("passing_tds").max().alias("qb_pass_td"),
            pl.col("receiving_tds").max().alias("top_rec_td"),
            pl.col("passing_yards").max().alias("qb_pass_yds"),
            pl.col("receiving_yards").max().alias("top_rec_yds"),
        )
        tg = tg.join(agg, on=["season", "week", "team"], how="left")
    return tg


@dataclass
class ScoringHyper:
    resid_sd: float = 10.0
    resid_corr: float = 0.08
    pass_td_share: float = 0.62
    n_games: int = 0


def scoring_hyperparams(tg: Any) -> ScoringHyper:
    if tg is None or tg.height == 0:
        return ScoringHyper()
    pts = tg["pts"].to_numpy().astype(float)
    imp = tg["implied"].to_numpy().astype(float)
    resid = pts - imp
    import polars as pl

    piv = tg.with_columns((pl.col("pts") - pl.col("implied")).alias("r")).group_by("game_id").agg(pl.col("r"))
    pairs = np.array([r for r in piv["r"].to_list() if len(r) == 2], dtype=float)
    corr = float(np.corrcoef(pairs[:, 0], pairs[:, 1])[0, 1]) if len(pairs) > 10 else 0.08
    share = 0.62
    if "team_pass_td" in tg.columns:
        p = tg["team_pass_td"].fill_null(0).to_numpy().astype(float)
        r = tg["team_rush_td"].fill_null(0).to_numpy().astype(float)
        if (p + r).sum() > 0:
            share = float(p.sum() / (p + r).sum())
    return ScoringHyper(resid_sd=float(np.std(resid)), resid_corr=corr, pass_td_share=share, n_games=int(len(pairs)))


def conditional_table(tg: Any, ks: tuple[int, ...] = (2, 3, 4), xs: tuple[float, ...] = (20.5, 24.5, 28.5, 31.5, 34.5),
                      implieds: tuple[float, ...] = (17.5, 20.5, 23.5, 26.5, 29.5)) -> list[dict[str, Any]]:
    """P(team pts > X | QB pass TD >= k, implied) via logistic regression on (implied, k-indicator)."""
    from sklearn.linear_model import LogisticRegression

    if tg is None or "qb_pass_td" not in tg.columns:
        return []
    df = tg.drop_nulls(["qb_pass_td", "pts", "implied"])
    imp = df["implied"].to_numpy().astype(float)
    qb = df["qb_pass_td"].to_numpy().astype(float)
    pts = df["pts"].to_numpy().astype(float)
    out = []
    for k in ks:
        mask = qb >= k
        if mask.sum() < 30:
            continue
        for x in xs:
            y = (pts[mask] > x).astype(int)
            if y.min() == y.max():
                continue
            model = LogisticRegression().fit(imp[mask].reshape(-1, 1), y)
            for it in implieds:
                p = float(model.predict_proba(np.array([[it]]))[0, 1])
                near = mask & (np.abs(imp - it) <= 1.5)
                emp = float((pts[near] > x).mean()) if near.sum() >= 10 else None
                out.append({"k": k, "x": x, "implied": it, "p_model": p, "p_empirical_near": emp, "n_cond": int(mask.sum()), "n_near": int(near.sum())})
    return out


def sync_all() -> dict[str, Any]:
    """Download / refresh the nflverse tables the fair model uses."""
    cur = nv.current_season()
    out: dict[str, Any] = {}
    for name, fn in (("rosters_weekly", lambda: nv.rosters_weekly([cur], max_age_h=0)),
                     ("player_stats", lambda: nv.player_stats([cur - 1, cur], max_age_h=0)),
                     ("schedules", lambda: nv.schedules(None, max_age_h=0))):
        df = fn()
        out[name] = None if df is None else df.height
    tg = team_games()
    out["team_games"] = None if tg is None else tg.height
    out["hyper"] = asdict(scoring_hyperparams(tg))
    return out
