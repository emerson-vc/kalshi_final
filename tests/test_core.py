"""Core sweeps (config/core.yaml): matching, round-robin coverage, rotation, budget split."""

import pytest
from pydantic import ValidationError

from combo_edge.candidates import Candidate
from combo_edge.context import default_db
from combo_edge.core import core_match, load_core
from combo_edge.hooks import default_hooks
from combo_edge.scan import ScanRequest, run_scan
from combo_edge.selection import select
from combo_edge.settings import RuntimeSettings
from combo_edge.sports.base import Leg, SidedLeg


def _qb(code: str, k: float, team: str = "SEA", game: str = "G1") -> Leg:
    return Leg(f"PTD-{team}{code}-{k}", "E1", "KXNFLPASSTDS", game, "pass_td", "player", team=team, player_code=code,
               player_name=code, position="QB", threshold=k)


def _tt(x: float, team: str = "SEA", game: str = "G1") -> Leg:
    return Leg(f"TT-{team}{x}", "E2", "KXNFLTEAMTOTAL", game, "team_total", "team", team=team, threshold=x)


def _cand(a: Leg, b: Leg, ev: float = 0.1) -> Candidate:
    c = Candidate((SidedLeg(a, "yes"), SidedLeg(b, "yes")), "qb_ptd_x_team_total", a.game_code)
    c.expected_ev = c.expected_ev_lower = ev
    return c


def test_matching_follows_core_yaml() -> None:
    sweeps = load_core()
    assert core_match(_cand(_qb("DARNOLD", 3.5), _tt(24.5)), sweeps).name == "qb_ptd_x_team_total"
    assert core_match(_cand(_tt(28.5), _qb("DARNOLD", 2.5)), sweeps) is not None  # either leg order
    assert core_match(_cand(_qb("DARNOLD", 1.5), _tt(24.5)), sweeps) is None  # 2+ pass TD is not a core rung
    assert core_match(_cand(_qb("DARNOLD", 3.5), _tt(38.5)), sweeps) is None  # o38.5 is outside 17.5–31.5
    assert core_match(_cand(_qb("DARNOLD", 3.5), _tt(24.5, team="CHI")), sweeps) is None  # opponent's total
    no_side = Candidate((SidedLeg(_qb("DARNOLD", 3.5), "no"), SidedLeg(_tt(24.5), "yes")), "x", "G1")
    assert core_match(no_side, sweeps) is None


def test_round_robin_spreads_players_and_rungs_and_rotates() -> None:
    sweeps = load_core()
    sea = [_cand(_qb("DARNOLD", k), _tt(x), ev=1.0 + k + x / 100) for k in (2.5, 3.5, 4.5) for x in (17.5, 24.5, 28.5)]
    chi = [_cand(_qb("WILLIAMS", k, team="CHI"), _tt(x, team="CHI"), ev=0.1) for k in (2.5, 3.5) for x in (21.5, 24.5)]
    sel = select(sea + chi, rfq_budget=4, creation_budget=4, known=set(), shares={"core": 1.0, "unpriced": 0}, core=sweeps)
    teams = [c.legs[0].leg.team for c in sel.chosen]
    assert teams.count("SEA") == 2 and teams.count("CHI") == 2  # low-EV CHI still gets its turn
    first_three = select(sea, rfq_budget=3, creation_budget=3, known=set(), shares={"core": 1.0, "unpriced": 0}, core=sweeps).chosen
    assert len({c.legs[0].leg.ticker for c in first_three}) == 3 and len({c.legs[1].leg.ticker for c in first_three}) == 3
    recent = {c.signature for c in sea[:8]}
    again = select(sea, rfq_budget=1, creation_budget=1, known=set(), shares={"core": 1.0, "unpriced": 0}, core=sweeps, recently_quoted=recent)
    assert again.chosen[0].signature == sea[8].signature  # the one rung not quoted recently goes first


def test_exploit_caps_rungs_per_pair_then_fills() -> None:
    cands = [_cand(_qb("DARNOLD", 3.5), _tt(x), ev=x) for x in (31.5, 35.5, 38.5, 42.5)]
    cands.append(_cand(_qb("WILLIAMS", 3.5, team="CHI"), _tt(35.5, team="CHI"), ev=0.5))
    sel = select(cands, rfq_budget=3, creation_budget=3, known=set(), shares={"unpriced": 0})
    picked = [(c.legs[0].leg.team, c.legs[1].leg.threshold) for c in sel.chosen]
    assert picked == [("SEA", 42.5), ("SEA", 38.5), ("CHI", 35.5)]  # third Darnold rung waits behind the other pair
    full = select(cands, rfq_budget=5, creation_budget=5, known=set(), shares={"unpriced": 0})
    assert len(full.chosen) == 5  # leftover budget still fills in EV order


def test_shares_must_sum_to_one() -> None:
    st = RuntimeSettings()
    assert st.budget_shares() == {"core": 0.50, "exploit": 0.25, "free": 0.15, "explore": 0.10}
    with pytest.raises(ValidationError, match="add up to 100%"):
        st.merged({"core_share": 0.9})


async def test_dry_run_sweeps_every_core_rung(mock_exchange) -> None:  # type: ignore[no-untyped-def]
    db = default_db()
    res = await run_scan(ScanRequest(env="mock", dry_run=True, rfq_budget=1000), db=db, hooks=default_hooks())
    core = res.summary["core"]
    assert {r["sweep"] for r in core} == {"qb_ptd_x_team_total", "rec_td_x_team_total", "qb_pyds_x_rec_yds", "ml_x_h1_lead"}
    assert all(r["selected"] == r["available"] for r in core)  # the budget covers every rung
    core_legs = {r.legs for r in res.rows if r.pool == "core"}
    assert "Sam Darnold 4+ pass td + SEA team o24.5" in core_legs
    assert "Sam Darnold 4+ pass td + SEA team o28.5" in core_legs
    assert any(r["pair"].startswith("AJ Barner") for r in core if r["sweep"] == "rec_td_x_team_total")  # a TE
