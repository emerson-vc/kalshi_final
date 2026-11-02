import random

import numpy as np

from combo_edge import timeutil
from combo_edge.candidates import FAMILIES, Candidate
from combo_edge.context import default_db
from combo_edge.hooks import default_hooks
from combo_edge.learning.bandit import Posteriors, correlated, rewards, thompson_key
from combo_edge.learning.calibration import history_calibration, outcome_calibration
from combo_edge.learning.promotion import check_promotions, load_promotions
from combo_edge.learning.timing import recommendation
from combo_edge.scan import ScanRequest, run_scan, runtime_settings
from combo_edge.selection import select
from combo_edge.settings import RuntimeSettings
from combo_edge.sports.base import Leg, SidedLeg
from combo_edge.storage import schema as S


def _label_rows(db, stratum: str, edges: list[float], games: int = 4, dow_hours: tuple[float, float] | None = None):  # type: ignore[no-untyped-def]
    run = db.insert(S.runs, {"kind": "scan", "env": "mock", "status": "done"})
    now = timeutil.now()
    for i, e in enumerate(edges):
        payout = 20.0
        fair = (1 + e) / payout
        cid = db.insert(S.candidates, {"run_id": run, "signature": f"{stratum}{i}", "family": f"explore:{stratum}", "stratum_l1": stratum,
                                       "stratum_l2": "alt/main|yy", "legs": [["A", "yes"], ["B", "yes"]], "fair_joint": fair,
                                       "leg_payout_product": 20.0, "game_code": f"G{i % games}"})
        created = now - (dow_hours[0] if dow_hours else 0) * 3600 if dow_hours else now - i * 600
        db.insert(S.rfqs, {"rfq_id": f"{stratum}r{i}", "env": "mock", "run_id": run, "candidate_id": cid, "combo_ticker": f"C{stratum}{i}",
                           "signature": f"{stratum}{i}", "purpose": "probe", "created_at": created, "outcome": "quoted",
                           "combo_payout": payout, "hours_to_start": dow_hours[1] if dow_hours else 30.0, "n_makers": 2})


def test_posteriors_and_thompson_prefer_paying_arms() -> None:
    post = Posteriors([("good", 0.4)] * 30 + [("bad", -0.2)] * 30)
    assert post.prob_above("good", 0.05) > 0.99 and post.prob_above("bad", 0.05) < 0.01

    def cand(arm: str, ev: float) -> Candidate:
        c = Candidate((SidedLeg(Leg(f"{arm}a{ev}", "E", "S", "G", "k", "team"), "yes"), SidedLeg(Leg(f"{arm}b{ev}", "F", "S", "G", "j", "team"), "yes")), "f", "G")
        c.stratum_l1, c.expected_ev_lower = arm, ev
        return c

    key = thompson_key(post, seed=1)
    good, bad = cand("good", 0.1), cand("bad", 0.12)
    assert key(good) > key(bad)  # the arm's realized reward outweighs a slightly better prior EV


def test_selection_uses_exploit_key() -> None:
    cands = []
    for i in range(10):
        c = Candidate((SidedLeg(Leg(f"a{i}", "E", "S", "G", "k", "team"), "yes"), SidedLeg(Leg(f"b{i}", "F", "S", "G", "j", "team"), "yes")), "f", "G")
        c.expected_ev = c.expected_ev_lower = 0.1 * i
        cands.append(c)
    sel = select(cands, rfq_budget=3, creation_budget=3, known=set(), shares={"unpriced": 0}, exploit_key=lambda c: -(c.expected_ev or 0))
    assert [c.signature for c in sel.chosen] == [cands[0].signature, cands[1].signature, cands[2].signature]


def test_promotion_registers_family_and_reports(mock_exchange) -> None:  # type: ignore[no-untyped-def]
    db = default_db()
    rng = random.Random(0)
    _label_rows(db, "WRTE:receptions×team_total|same_team", [0.35 + rng.gauss(0, 0.1) for _ in range(40)])
    _label_rows(db, "RB:rush_att×game:game_total|game", [-0.1 + rng.gauss(0, 0.1) for _ in range(40)])
    promoted = check_promotions(db, "mock", RuntimeSettings())
    assert [p["family"] for p in promoted] == ["WRTE:receptions×team_total|same_team"]
    assert "promoted:WRTE:receptions×team_total|same_team" in FAMILIES
    assert load_promotions(db) == 1
    assert check_promotions(db, "mock", RuntimeSettings()) == []  # not promoted twice


def test_timing_recommendation() -> None:
    random.seed(5)
    labels = []
    for d, e in (("Tue", 0.30), ("Wed", 0.28), ("Sat", 0.02), ("Sun", -0.05)):
        labels += [{"edge": e + random.gauss(0, 0.05), "dow": d, "hours_bucket": "3-4d" if d in ("Tue", "Wed") else "<1h"} for _ in range(25)]
    rec = recommendation(labels)
    assert rec and "Tue–Wed" in rec["headline"] and "3-4d" in rec["headline"]


def test_history_calibration_reports_gaps() -> None:
    import polars as pl

    rng = np.random.default_rng(1)
    n = 6000
    imp = rng.uniform(18, 29, n)
    qb = rng.poisson(imp / 13)
    pts = np.round(imp + 6.5 * (qb - imp / 13) + rng.normal(0, 7, n))
    tg = pl.DataFrame({"pts": pts, "implied": imp, "qb_pass_td": qb, "top_rec_td": rng.poisson(0.5, n), "qb_pass_yds": rng.normal(230, 70, n),
                       "top_rec_yds": rng.normal(75, 30, n)})
    cal = history_calibration(tg)
    checks = {r["check"] for r in cal["rows"]}
    assert "qb_ptd_x_team_total|k3|x28.5" in checks
    assert all(0 <= u <= 0.30 for u in cal["family_u"].values())


def test_outcome_calibration(mock_exchange) -> None:  # type: ignore[no-untyped-def]
    db = default_db()
    _label_rows(db, "X|game", [0.1] * 10)
    db.upsert(S.legs, {"ticker": "A", "result": "yes"}, ["ticker"])
    db.upsert(S.legs, {"ticker": "B", "result": "no"}, ["ticker"])
    out = outcome_calibration(db, "mock")
    assert out["explore:X|game"]["n"] == 10 and out["explore:X|game"]["hit_rate"] == 0.0


def test_rewards_include_clv(mock_exchange) -> None:  # type: ignore[no-untyped-def]
    db = default_db()
    _label_rows(db, "S|game", [0.2])
    db.insert(S.rfqs, {"rfq_id": "rq", "env": "mock", "combo_ticker": "CS|game0", "purpose": "requote", "created_at": timeutil.now() + 10,
                       "offset_h": 1, "outcome": "quoted", "combo_payout": 10.0})
    r = dict(rewards(db, "mock"))
    assert r["S|game"] > 0.2  # payout 20x vs close 10x -> positive CLV adds to the reward


def test_correlated_flag() -> None:
    c = Candidate((SidedLeg(Leg("a", "E", "S", "G", "k", "team"), "yes"), SidedLeg(Leg("b", "F", "S", "G", "j", "team"), "yes")), "f", "G")
    c.p_legs, c.fair_joint = (0.2, 0.5), 0.15
    assert correlated(c)
    c.fair_joint = 0.101
    assert not correlated(c)


async def test_full_pipeline_with_learning_hooks(mock_exchange) -> None:  # type: ignore[no-untyped-def]
    db = default_db()
    res = await run_scan(ScanRequest(env="mock", rfq_budget=30, window_s=2.5), db=db, hooks=default_hooks())
    assert res.status == "done"
    res2 = await run_scan(ScanRequest(env="mock", rfq_budget=12, window_s=2.0), db=db, hooks=default_hooks())
    assert res2.status == "done"
    assert db.scalar("SELECT COUNT(*) FROM model_versions WHERE kind='maker'") >= 1  # retrained on the second run
    assert db.meta_get("calibration:mock") is not None
    assert runtime_settings(db).exploration_mode
