import math

import numpy as np
import pytest

from combo_edge import timeutil
from combo_edge.candidates import Candidate
from combo_edge.context import default_db, make_clients
from combo_edge.fair.blend import blend, disagrees, kalshi_weight
from combo_edge.fair.history import conditional_table, scoring_hyperparams
from combo_edge.fair.model import FairModel, stat_key
from combo_edge.fair.simulator import (
    GameSim,
    ReceiverSpec,
    RusherSpec,
    SimParams,
    TeamSpec,
    fit_team_scoring,
    points_pmf,
    points_survival,
    rank_uniform,
)
from combo_edge.sports.base import SidedLeg
from combo_edge.universe import build_universe


def test_points_pmf_is_a_distribution() -> None:
    for sg in (0.0, 0.12, 0.3):
        pmf = points_pmf(0.22, 0.15, sg)
        assert abs(pmf.sum() - 1) < 1e-9 and (pmf >= 0).all()
        mean = (np.arange(len(pmf)) * pmf).sum()
        assert 18 < mean < 28


def test_fit_reproduces_a_team_total_ladder() -> None:
    lad = [(13.5, 0.88, 1), (17.5, 0.79, 1), (20.5, 0.69, 1), (24.5, 0.5, 1), (28.5, 0.35, 1), (31.5, 0.22, 1), (35.5, 0.12, 1)]
    ptd, pfg, rmse = fit_team_scoring(lad, 0.12)
    assert rmse < 0.04
    surv = points_survival(ptd, pfg, 0.12, np.array([x for x, _, _ in lad]))
    assert np.all(np.diff(surv) <= 1e-12)


def _team(name: str, qb: bool = True) -> TeamSpec:
    t = TeamSpec(name, mu_td=0.24, phi_fg=0.17, qb_key=f"{name}:QB" if qb else None)
    t.receivers = [ReceiverSpec(f"{name}:WR1", 0.28, 0.65, 13, 0.06), ReceiverSpec(f"{name}:WR2", 0.18, 0.64, 12, 0.05),
                   ReceiverSpec(f"{name}:TE", 0.15, 0.7, 10, 0.06), ReceiverSpec("_other", 0.39, 0.66, 9, 0.04)]
    t.rushers = [RusherSpec(f"{name}:RB", 0.6, 4.4, 0.035), RusherSpec(f"{name}:QB", 0.1, 5, 0.04), RusherSpec("_o", 0.3, 4, 0.03)]
    return t


def test_simulator_structure_and_invariants() -> None:
    res = GameSim("G", _team("A"), _team("B"), SimParams(n=30000, sigma=0.12, rho=0.08)).run()
    s = res.stats
    # QB pass TDs are team pass TDs; every pass TD has exactly one receiver
    rec_tds = s[("player", "A:WR1", "rec_td")] + s[("player", "A:WR2", "rec_td")] + s[("player", "A:TE", "rec_td")]
    assert (rec_tds <= s[("player", "A:QB", "pass_td")]).all()
    pts, qb4 = s[("team", "A", "points")], s[("player", "A:QB", "pass_td")] >= 4
    # structural tail dependence: 4 TD passes are >= 24 points + XPs
    assert (pts[qb4] >= 24).all()
    assert (pts[qb4] > 28.5).mean() > 0.8
    # QB yards vs WR1 yards strongly positive; win vs margin consistent; h1 <= full
    assert np.corrcoef(s[("player", "A:QB", "pass_yds")], s[("player", "A:WR1", "rec_yds")])[0, 1] > 0.4
    assert ((s[("team", "A", "margin")] > 0) <= (s[("team", "A", "win")] > 0.5)).all()
    assert (s[("team", "A", "h1_points")] <= pts).all()
    # game script: RB rushing is higher when the team wins
    win = s[("team", "A", "win")] > 0.5
    assert s[("player", "A:RB", "rush_yds")][win].mean() > s[("player", "A:RB", "rush_yds")][~win].mean()


def test_rank_uniform_is_uniform_on_counts() -> None:
    x = np.random.default_rng(1).poisson(1.2, 50000)
    u = rank_uniform(x, np.random.default_rng(2))
    for q in (0.1, 0.5, 0.9):
        assert abs((u >= 1 - q).mean() - q) < 0.01
    assert np.all(np.diff(u[np.argsort(x, kind="stable")]) > -1)  # well-defined


def test_blend_weights() -> None:
    assert blend(0.3, 0.5, 0.9) == pytest.approx(0.32)
    assert blend(None, 0.4, 0.9) == 0.4
    assert disagrees(0.10, 0.25) and not disagrees(0.10, 0.13)


async def test_fair_model_on_recorded_markets(mock_exchange) -> None:  # type: ignore[no-untyped-def]
    c = make_clients("mock", default_db())
    uni = await build_universe(c.api, "KXMVECROSSCATEGORY-R")
    await c.aclose()
    fm = FairModel(uni, usage=None, hyper=None)  # no network in tests: position-mean usage, default hyperparameters
    fm.prepare()
    assert fm.games["26NOV02CHISEA"] is not None
    legs = list(uni.legs.values())
    # every modelled leg's blended probability stays close to a liquid Kalshi mid
    tt = [l for l in legs if l.kind == "team_total" and l.team == "SEA" and l.p_source == "mid"]
    for l in tt:
        assert abs(fm.leg_prob(l)[1] - l.p_market()) < 0.06
    def near(pred, x):  # type: ignore[no-untyped-def]
        cs = [l for l in legs if pred(l) and l.threshold is not None and not l.protected]
        return min(cs, key=lambda l: abs(l.threshold - x))
    qb4 = near(lambda l: l.kind == "pass_td" and l.player_name == "Sam Darnold", 3.5)
    over = near(lambda l: l.kind == "team_total" and l.team == "SEA", 28.5)
    under_ml = next(l for l in legs if l.kind == "ml" and l.outcome == "SEA")
    cands = [Candidate((SidedLeg(qb4, "yes"), SidedLeg(over, "yes")), "t", qb4.game_code),
             Candidate((SidedLeg(qb4, "no"), SidedLeg(over, "no")), "t", qb4.game_code),
             Candidate((SidedLeg(qb4, "yes"), SidedLeg(under_ml, "yes")), "t", qb4.game_code)]
    out = fm.joint_many(cands)
    pa, pb = fm.leg_prob(qb4)[1], fm.leg_prob(over)[1]
    fair, lower = out[0]
    assert fair / pa > 0.85  # P(team over 28.5 | QB 4+ pass TD) is high: the makers' mistake
    assert fair <= min(pa, pb) + 1e-9 and 0 < lower < fair
    nn = out[1][0]
    assert abs(nn - (1 - pa - pb + fair)) < 1e-6
    assert out[2][0] / pa > 0.6
    assert stat_key(qb4) == ("player", qb4.player_key, "pass_td")


def test_history_tools_on_synthetic_data() -> None:
    import polars as pl

    rng = np.random.default_rng(0)
    n = 4000
    imp = rng.uniform(17, 30, n)
    qb = rng.poisson(imp / 14)
    pts = np.round(imp + 7 * (qb - imp / 14) + rng.normal(0, 7, n))
    tg = pl.DataFrame({"game_id": np.repeat(np.arange(n // 2), 2).astype(str), "pts": pts, "implied": imp, "qb_pass_td": qb,
                       "team_pass_td": qb, "team_rush_td": rng.poisson(0.9, n)})
    h = scoring_hyperparams(tg)
    assert 5 < h.resid_sd < 15 and 0.4 < h.pass_td_share < 0.8
    tab = conditional_table(tg)
    row = next(r for r in tab if r["k"] == 3 and r["x"] == 28.5 and r["implied"] == 26.5)
    assert row["p_model"] > 0.5  # conditioning on 3+ pass TDs lifts P(team > 28.5)


def test_maker_model_learns_family_ratios(mock_exchange) -> None:  # type: ignore[no-untyped-def]
    from combo_edge.learning.maker_model import train
    from combo_edge.storage import schema as S

    db = default_db()
    now = timeutil.now()
    rng = np.random.default_rng(4)
    run = db.insert(S.runs, {"kind": "scan", "env": "mock", "status": "done"})
    for i in range(60):
        fam = "qb_ptd_x_team_total" if i % 2 else "wr_td_x_team_total"
        true_ratio = 0.7 if fam.startswith("qb") else 1.1
        cid = db.insert(S.candidates, {"run_id": run, "signature": f"s{i}", "family": fam, "stratum_l1": "QB:pass_td×team_total|same_team",
                                       "stratum_l2": "alt/main|yy", "legs": [["A", "yes"], ["B", "yes"]], "fair_joint": 0.1, "leg_payout_product": 20.0,
                                       "p_a": 0.2, "p_b": 0.5})
        db.insert(S.rfqs, {"rfq_id": f"r{i}", "env": "mock", "run_id": run, "candidate_id": cid, "combo_ticker": f"C{i}", "signature": f"s{i}",
                           "purpose": "probe", "created_at": now - i * 3600, "outcome": "quoted", "n_makers": 2,
                           "combo_payout": 20.0 * true_ratio * math.exp(rng.normal(0, 0.05))})
    m = train(db, "mock")
    assert m is not None and m.kind == "means"
    rows = [{"family_base": "qb_ptd_x_team_total", "kind_a": "QB:pass_td", "kind_b": "team_total"},
            {"family_base": "wr_td_x_team_total", "kind_a": "QB:pass_td", "kind_b": "team_total"}]
    r_qb, r_wr = np.exp(m.predict_log_ratio(rows))
    assert r_qb < 0.85 and r_wr > 0.95
    assert db.meta_get("maker_drift:mock")["summary"]
