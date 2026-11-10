"""Tail behaviour of the fair model and prescore: the edge must not keep growing as both legs get longer."""

import math
import statistics
from pathlib import Path

import numpy as np
import polars as pl

from combo_edge.candidates import Candidate
from combo_edge.context import default_db
from combo_edge.diagnostics import payout_of, plan_offline
from combo_edge.fair.model import FairModel, GameModel, conditional_shift, tail_joint
from combo_edge.fair.simulator import GameSim, ReceiverSpec, RusherSpec, TeamSpec, fit_team_scoring
from combo_edge.learning.calibration import _sim_params, history_calibration
from combo_edge.sports.base import Leg, SidedLeg

HISTORY = Path(__file__).parent / "fixtures" / "history" / "team_games.parquet"


def _pooled_qb4_over(params, implieds=np.linspace(22.5, 27.5, 6), share=0.62) -> float:  # type: ignore[no-untyped-def]
    """P(team pts > 28.5 | QB >= 4 pass TD) for a team implied at 22.5-27.5, pooled over the band."""
    hit = n = 0
    for imp in implieds:
        ptd, pfg, _ = fit_team_scoring([], params.sigma, mean_hint=float(imp))
        o_td, o_fg, _ = fit_team_scoring([], params.sigma, mean_hint=44.0 - float(imp))
        team = TeamSpec("T", ptd, pfg, pass_share=share, qb_key="QB", receivers=[ReceiverSpec("W", 1.0, 0.65, 11, 0.05)],
                        rushers=[RusherSpec("R", 1.0, 4.2, 0.03)])
        opp = TeamSpec("O", o_td, o_fg, qb_key="OQB", receivers=[ReceiverSpec("_o", 1.0, 0.65, 11, 0.05)], rushers=[RusherSpec("_o", 1.0, 4.2, 0.03)])
        s = GameSim("t", team, opp, params).run().stats
        m = s[("player", "QB", "pass_td")] >= 4
        hit += int((s[("team", "T", "points")][m] > 28.5).sum())
        n += int(m.sum())
    return hit / n


def test_qb_four_td_team_over_matches_history() -> None:
    """Real (nflverse 2018-25, implied 22.5-27.5): 0.93 with n=139. Was ~0.95-0.99 in the simulator."""
    tg = pl.read_parquet(HISTORY)
    params = _sim_params(tg, n=40000)
    raw = _pooled_qb4_over(params)
    assert 0.82 <= raw <= 0.93, raw  # the simulator alone (Beta pass share + TDs replacing FGs)
    shift = history_calibration(tg)["family_shift"]["qb_ptd_x_team_total"]
    calibrated = conditional_shift(raw, shift)
    assert 0.82 <= calibrated <= 0.93, (raw, shift, calibrated)  # what the fair model uses after calibration


def _two_leg_model(td: np.ndarray, pts: np.ndarray, p_td4: float) -> tuple[FairModel, Candidate]:
    """QB 4+ x team o28.5 on hand-made simulations; the team total's target equals its simulated marginal."""
    qb = Leg("PTD-4", "E", "KXNFLPASSTDS", "G", "pass_td", "player", team="SEA", player_code="QB", position="QB", threshold=3.5)
    tt = Leg("TT-28", "E", "KXNFLTEAMTOTAL", "G", "team_total", "team", team="SEA", threshold=28.5)
    fm = FairModel.__new__(FairModel)
    FairModel.__init__(fm, type("U", (), {"legs_by_game": {}, "games": {}, "legs": {}})())
    gm = GameModel("G", members=[{("player", qb.player_key, "pass_td"): td.astype(float), ("team", "SEA", "points"): pts.astype(float)}],
                   uniforms=[{}], n=len(td))
    fm.games["G"] = gm
    raw_td4 = float((td >= 4).mean())
    fm.leg_info[qb.ticker] = (raw_td4, p_td4, 0.9, False)
    p_over = float((pts > 28.5).mean())
    fm.leg_info[tt.ticker] = (p_over, p_over, 0.9, False)
    return fm, Candidate((SidedLeg(qb, "yes"), SidedLeg(tt, "yes")), "qb_ptd_x_team_total", "G")


def test_tail_target_far_above_simulator_uses_raw_conditional() -> None:
    rng = np.random.default_rng(0)
    n = 200_000
    td = rng.poisson(1.5, n)
    pts = 7 * td + rng.normal(10, 6, n)  # P(o28.5) ~ 0.3, near-certain on 4-TD days
    raw = float((td >= 4).mean())  # ~0.066
    target = 2.5 * raw  # Kalshi prices 4+ far above the simulator: rank space would pull in 3-TD games
    fm, cand = _two_leg_model(td, pts, target)
    fair = fm.joint_many([cand])[0][0]
    p_over = fm.leg_info["TT-28"][1]
    u_over = fm.games["G"].u(0, ("team", "SEA", "points")) >= 1.0 - p_over
    expected = tail_joint(u_over, td >= 4, target)
    assert math.isclose(fair, expected, rel_tol=1e-6)
    assert fair / target > 0.95  # P(o28.5 | 4 TDs) stays structural; rank space would dilute it toward 3-TD games
    fm2, cand2 = _two_leg_model(td, pts, 1.2 * raw)  # within 1.5x: ordinary rank-space joint
    u_td = fm2.games["G"].u(0, ("player", cand2.legs[0].leg.player_key, "pass_td")) >= 1 - 1.2 * raw
    assert math.isclose(fm2.joint_many([cand2])[0][0], float((u_td & u_over).mean()), rel_tol=1e-6)


def test_family_shift_moves_the_conditional_in_log_odds() -> None:
    rng = np.random.default_rng(1)
    td = rng.poisson(1.5, 100_000)
    pts = 7 * td + rng.normal(0, 4, 100_000)
    p4 = float((td >= 4).mean())
    fm, cand = _two_leg_model(td, pts, p4)
    base = fm.joint_many([cand])[0][0]
    fm.family_shift["qb_ptd_x_team_total"] = -0.5
    shifted = fm.joint_many([cand])[0][0]
    assert math.isclose(shifted / p4, conditional_shift(base / p4, -0.5), rel_tol=1e-6)
    assert shifted < base


async def test_ev_at_lower_bound_does_not_rise_with_payout(mock_exchange) -> None:  # type: ignore[no-untyped-def]
    plan = await plan_offline(default_db(), "mock", budget=500)
    buckets = [(8, 15), (15, 25), (25, 40), (40, 60), (60, 1e9)]
    med = []
    for lo, hi in buckets:
        low = [c.expected_ev_lower for c in plan.candidates
               if ":" not in c.family and not c.family.startswith("no_") and all(s.side == "yes" for s in c.legs)
               and c.expected_ev_lower is not None and lo <= (c.leg_payout_product or 0) * c.expected_ratio < hi]
        med.append(statistics.median(low) if low else None)
    vals = [m for m in med if m is not None]
    assert len(vals) >= 4
    assert vals != sorted(vals), med  # not monotonically rising
    assert vals[-1] < max(vals[:-1]), med  # the longest combos are not the best at the lower bound
    core_exploit = [c for c in plan.selection.chosen if c.pool in ("core", "exploit")]
    assert core_exploit and all(8.0 <= payout_of(c) <= 50.0 for c in core_exploit)
