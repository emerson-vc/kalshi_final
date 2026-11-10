"""Core sweeps (config/core.yaml), the payout band and explore-only families in selection."""

import pytest
from pydantic import ValidationError

from combo_edge import timeutil
from combo_edge.candidates import Candidate
from combo_edge.context import default_db
from combo_edge.core import CoreContext, build_context, core_match, load_core
from combo_edge.hooks import default_hooks
from combo_edge.scan import ScanRequest, explore_only_rule, recently_quoted, run_scan
from combo_edge.selection import select
from combo_edge.settings import RuntimeSettings
from combo_edge.sports.base import Leg, SidedLeg
from combo_edge.storage import schema as S

BAND = (8.0, 50.0)


def _qb(code: str, k: float, team: str = "SEA", game: str = "G1") -> Leg:
    return Leg(f"PTD-{team}{code}-{k}", "E1", "KXNFLPASSTDS", game, "pass_td", "player", team=team, player_code=code,
               player_name=code, position="QB", threshold=k)


def _tt(x: float, team: str = "SEA", game: str = "G1") -> Leg:
    return Leg(f"TT-{team}{x}", "E2", "KXNFLTEAMTOTAL", game, "team_total", "team", team=team, threshold=x)


def _wr(code: str, k: float, team: str = "SEA", kind: str = "anytime_td", pos: str = "WR") -> Leg:
    return Leg(f"{kind}-{team}{code}-{k}", "E3", "KXNFLTD", "G1", kind, "player", team=team, player_code=code, player_name=code,
               position=pos, threshold=k)


def _cand(a: Leg, b: Leg, ev: float = 0.1, payout: float | None = 20.0, family: str = "qb_ptd_x_team_total") -> Candidate:
    c = Candidate((SidedLeg(a, "yes"), SidedLeg(b, "yes")), family, a.game_code)
    c.expected_ev = c.expected_ev_lower = ev
    c.expected_payout = payout
    return c


CTX = CoreContext(main_team_total={("G1", "SEA"): 25.0, ("G1", "CHI"): 21.0})


def test_matching_follows_core_yaml() -> None:
    sweeps = load_core()
    assert core_match(_cand(_qb("DARNOLD", 3.5), _tt(24.5)), sweeps, CTX).name == "qb_ptd_x_team_total"
    assert core_match(_cand(_tt(28.5), _qb("DARNOLD", 2.5)), sweeps, CTX) is not None  # either leg order, 3+
    assert core_match(_cand(_qb("DARNOLD", 4.5), _tt(28.5)), sweeps, CTX) is None  # 5+ is explore-only
    assert core_match(_cand(_qb("DARNOLD", 1.5), _tt(24.5)), sweeps, CTX) is None  # 2+ is not a core rung
    assert core_match(_cand(_qb("DARNOLD", 3.5), _tt(18.5)), sweeps, CTX) is not None  # main 25 - 6.5
    assert core_match(_cand(_qb("DARNOLD", 3.5), _tt(32.5)), sweeps, CTX) is None  # main 25 + 7.5: outside +-7
    assert core_match(_cand(_qb("DARNOLD", 3.5), _tt(24.5, team="CHI")), sweeps, CTX) is None  # opponent's total
    no_side = Candidate((SidedLeg(_qb("DARNOLD", 3.5), "no"), SidedLeg(_tt(24.5), "yes")), "x", "G1")
    assert core_match(no_side, sweeps, CTX) is None


def test_receiver_sweeps_use_each_teams_top_two() -> None:
    sweeps = load_core()
    ctx = CoreContext(main_team_total=CTX.main_team_total, receiver_rank={("G1", "SEA"): ["SEA:JSN", "SEA:KUPP", "SEA:BARNER"]})
    assert core_match(_cand(_wr("KUPP", 1.5), _tt(24.5)), sweeps, ctx).name == "rec_td_x_team_total"
    assert core_match(_cand(_wr("BARNER", 1.5, pos="TE"), _tt(24.5)), sweeps, ctx) is None  # third receiver
    yds = _cand(_qb("DARNOLD", 299.5, ), _wr("JSN", 119.5, kind="rec_yds"))
    yds.legs[0].leg.kind = "pass_yds"
    assert core_match(yds, sweeps, ctx).name == "qb_pyds_x_rec_yds"


def test_receivers_rank_by_td_price_then_target_share() -> None:
    class Game:
        away_team, home_team = "CHI", "SEA"

    class Uni:
        games = {"G1": Game()}
        legs_by_game = {"G1": []}

    legs = Uni.legs_by_game["G1"]
    for code, p in (("JSN", 0.45), ("KUPP", 0.30)):
        l = _wr(code, 0.5)
        l.yes_bid, l.yes_ask = p - 0.01, p + 0.01
        legs.append(l)
    legs.append(_wr("BARNER", 39.5, kind="rec_yds", pos="TE"))  # no TD market: falls back to target share

    class Usage:
        def player(self, team, name, jersey, pos):  # type: ignore[no-untyped-def]
            return type("PU", (), {"target_share": 0.18})()

    ctx = build_context(Uni(), usage=Usage())
    assert ctx.receiver_rank[("G1", "SEA")] == ["SEA:JSN", "SEA:KUPP", "SEA:BARNER"]


def test_core_budget_split_by_sweep_with_carry() -> None:
    sweeps = load_core()
    qb = [_cand(_qb(f"Q{i}", k, game=f"G{i}"), _tt(x, game=f"G{i}")) for i in range(30) for k in (2.5, 3.5) for x in (21.5, 24.5)]
    ml = [_cand(Leg(f"H1-{i}-{x}", "E", "S", f"G{i}", "h1_spread", "team", team="SEA", threshold=x),
                Leg(f"ML-{i}", "E", "S", f"G{i}", "ml", "team", team="SEA", outcome="SEA"), family="ml_x_h1_spread")
          for i in range(40) for x in (2.5, 6.5)]
    sel = select(qb + ml, rfq_budget=100, creation_budget=100, known=set(), shares={"core": 1.0, "unpriced": 0}, core=sweeps,
                 payout_band=BAND)
    by = {name: sum(1 for c in sel.chosen if f"core:{name}" in c.notes) for name in ("qb_ptd_x_team_total", "ml_x_h1_lead")}
    # QB TD x TT gets its 40%; the two receiver sweeps have nothing, so their 45% flows on to ML x 1H (15% + 45%)
    assert by == {"qb_ptd_x_team_total": 40, "ml_x_h1_lead": 60}
    few_qb = select(qb[:6] + ml, rfq_budget=100, creation_budget=100, known=set(), shares={"core": 1.0, "unpriced": 0}, core=sweeps,
                    payout_band=BAND)
    assert sum(1 for c in few_qb.chosen if c.legs[0].leg.kind == "pass_td") == 6  # unused QB share flows to the next sweep
    assert len(few_qb.chosen) == 86  # every ML x 1H rung after the QB rungs ran out


def test_rungs_go_middle_out_spread_and_rotate() -> None:
    sweeps = load_core()
    rungs = {(2.5, 21.5): 9.0, (2.5, 24.5): 12.0, (2.5, 28.5): 16.0, (3.5, 21.5): 24.0, (3.5, 24.5): 30.0, (3.5, 28.5): 45.0}
    sea = [_cand(_qb("DARNOLD", k), _tt(x), ev=2.0 if pay > 40 else 0.1, payout=pay) for (k, x), pay in rungs.items()]
    first = select(sea, rfq_budget=1, creation_budget=1, known=set(), shares={"core": 1.0, "unpriced": 0}, core=sweeps,
                   core_ctx=CTX, payout_band=BAND).chosen[0]
    assert first.expected_payout in (16.0, 24.0)  # nearest the band's middle (20x), not the +200%-EV 45x extreme
    three = select(sea, rfq_budget=2, creation_budget=2, known=set(), shares={"core": 1.0, "unpriced": 0}, core=sweeps,
                   core_ctx=CTX, payout_band=BAND).chosen
    assert len({c.legs[0].leg.ticker for c in three}) == 2 and len({c.legs[1].leg.ticker for c in three}) == 2  # spread both ladders
    recent = {c.signature for c in sea if c.expected_payout != 45.0}
    again = select(sea, rfq_budget=1, creation_budget=1, known=set(), shares={"core": 1.0, "unpriced": 0}, core=sweeps, core_ctx=CTX,
                   recently_quoted=recent, payout_band=BAND)
    assert again.chosen[0].expected_payout == 45.0  # the one rung not quoted recently goes first


def test_payout_band_and_explore_only_families() -> None:
    long = _cand(_qb("DARNOLD", 3.5), _tt(38.5), ev=3.0, payout=120.0, family="qb_ptd_x_rec_td")
    short = _cand(_qb("DARNOLD", 2.5), _tt(10.5), ev=0.5, payout=4.0, family="qb_ptd_x_rec_td")
    mid = _cand(_qb("WILLIAMS", 3.5, team="CHI"), _tt(24.5, team="CHI"), ev=0.2, payout=25.0, family="qb_ptd_x_rec_td")
    rb = _cand(_qb("SWIFT", 3.5, team="CHI"), _tt(21.5, team="CHI"), ev=2.5, payout=30.0, family="rb_x_ml_spread")
    no = _cand(_qb("LAWRENCE", 3.5, team="JAC"), _tt(21.5, team="JAC"), ev=2.0, payout=20.0, family="no_qb_x_game_total")
    gated = {"rb_x_ml_spread", "no_qb_x_game_total"}
    sel = select([long, short, mid, rb, no], rfq_budget=5, creation_budget=5, known={long.signature, short.signature},
                 shares={"free": 1.0, "unpriced": 0}, payout_band=BAND, explore_only=lambda c: c.family in gated)
    assert [c.signature for c in sel.chosen] == [mid.signature]  # free and exploit: only in-band, non-gated combos

    picked = []

    def picker(cands, n, k):  # type: ignore[no-untyped-def]
        picked.extend(cands)
        return [c for c in cands if c.signature in (long.signature, rb.signature)]

    sel = select([long, short, mid, rb, no], rfq_budget=5, creation_budget=5, known=set(), shares={"explore": 0.4, "unpriced": 0},
                 explore_picker=picker, payout_band=BAND, explore_only=lambda c: c.family in gated)
    pools = {c.signature: c.pool for c in sel.chosen}
    assert pools[long.signature] == "explore" and pools[rb.signature] == "explore"  # outside the band / gated: explore only
    assert pools[mid.signature] == "exploit" and short.signature not in pools and no.signature not in pools


def test_explore_only_rule_releases_families_that_earn_it(mock_exchange) -> None:  # type: ignore[no-untyped-def]
    db = default_db()
    st = RuntimeSettings()
    cands = [_cand(_qb("A", 3.5), _tt(24.5), family=f) for f in ("rb_x_ml_spread", "no_qb_ptd_x_team_total", "explore:QB|game", "qb_pyds_x_rec_yds")]
    cands.append(_cand(_qb("A", 4.5), _tt(28.5)))  # QB 5+ x team total
    rule = explore_only_rule(db, "mock", st, cands)
    assert [rule(c) for c in cands] == [True, True, True, False, True]
    run = db.insert(S.runs, {"kind": "scan", "env": "mock", "status": "done"})
    for i in range(30):  # 30 quotes with a positive edge release rb_x_ml_spread
        cid = db.insert(S.candidates, {"run_id": run, "signature": f"s{i}", "family": "rb_x_ml_spread", "stratum_l1": "RB|team",
                                       "legs": [["A", "yes"], ["B", "yes"]], "fair_joint": 0.06, "game_code": f"G{i % 4}"})
        db.insert(S.rfqs, {"rfq_id": f"r{i}", "env": "mock", "run_id": run, "candidate_id": cid, "combo_ticker": f"C{i}", "signature": f"s{i}",
                           "purpose": "probe", "created_at": timeutil.now(), "outcome": "quoted", "combo_payout": 20.0})
    rule = explore_only_rule(db, "mock", st, cands)
    assert rule(cands[0]) is False and rule(cands[1]) is True


def test_core_combos_requeue_when_a_leg_moves(mock_exchange) -> None:  # type: ignore[no-untyped-def]
    db = default_db()
    st = RuntimeSettings()
    run = db.insert(S.runs, {"kind": "scan", "env": "mock", "status": "done"})
    for sig in ("A:yes|B:yes", "C:yes|D:yes"):
        db.insert(S.rfqs, {"rfq_id": sig, "env": "mock", "run_id": run, "signature": sig, "purpose": "probe", "created_at": timeutil.now(),
                           "outcome": "quoted", "combo_payout": 20.0})
    for t, bid, ask in (("A", 0.20, 0.22), ("B", 0.50, 0.52), ("C", 0.30, 0.32), ("D", 0.40, 0.42)):
        db.insert(S.leg_snapshots, {"run_id": run, "ticker": t, "yes_bid": bid, "yes_ask": ask})
    now = {"A": 0.22, "B": 0.51, "C": 0.36, "D": 0.41}  # C moved 5c since its quote
    assert recently_quoted(db, "mock", st, now) == {"A:yes|B:yes"}
    assert recently_quoted(db, "mock", st) == {"A:yes|B:yes", "C:yes|D:yes"}


def test_shares_and_band_settings_validate() -> None:
    st = RuntimeSettings()
    assert st.budget_shares() == {"core": 0.50, "exploit": 0.25, "free": 0.15, "explore": 0.10}
    assert (st.rank_payout_min, st.rank_payout_max) == (8.0, 50.0)
    assert st.requote_enabled is True and st.fullscan_early_week is False
    with pytest.raises(ValidationError, match="add up to 100%"):
        st.merged({"core_share": 0.9})
    with pytest.raises(ValidationError, match="min must be below max"):
        st.merged({"rank_payout_min": 60})


async def test_dry_run_core_lands_in_band(mock_exchange) -> None:  # type: ignore[no-untyped-def]
    db = default_db()
    res = await run_scan(ScanRequest(env="mock", dry_run=True, rfq_budget=500), db=db, hooks=default_hooks())
    core_rows = [r for r in res.rows if r.pool == "core"]
    assert core_rows and all(8.0 <= r.expected_payout <= 50.0 for r in core_rows)
    exploit = [r for r in res.rows if r.pool == "exploit"]
    assert all(8.0 <= r.expected_payout <= 50.0 for r in exploit)
    assert not [r for r in exploit if r.family.startswith(("no_", "explore:")) or r.family in ("rb_x_ml_spread", "qb_x_game_total")]
    legs = {r.legs for r in core_rows}
    assert "Sam Darnold 4+ pass td + SEA team o24.5" in legs and "Sam Darnold 4+ pass td + SEA team o28.5" in legs
    assert not [l for l in legs if "5+ pass td" in l]
    assert {r["sweep"] for r in res.summary["core"]} == {"qb_ptd_x_team_total", "rec_td_x_team_total", "qb_pyds_x_rec_yds", "ml_x_h1_lead"}


async def test_win_margin_tie_is_never_a_leg(mock_exchange) -> None:  # type: ignore[no-untyped-def]
    import copy

    from combo_edge.requote import due_slots

    base = next(m for t, m in mock_exchange.markets.items() if t.startswith("KXNFLWINMARGIN-26NOV02CHISEA-"))
    tie = copy.deepcopy(base)
    tie.update(ticker="KXNFLWINMARGIN-26NOV02CHISEA-TIE", yes_sub_title="Tie", custom_strike={"Winning Margin": "Tie"})
    mock_exchange.markets[tie["ticker"]] = tie
    db = default_db()
    res = await run_scan(ScanRequest(env="mock", dry_run=True, rfq_budget=1000), db=db, hooks=default_hooks())
    assert res.coverage["excluded"].get("win_margin_tie") == 1
    assert db.scalar("SELECT COUNT(*) FROM candidates WHERE legs LIKE '%WINMARGIN-26NOV02CHISEA-TIE%'") == 0
    # a TIE combo saved on the re-quote panel before the rule existed is retired, not re-quoted
    sig = "KXNFLRSHYDS-26NOV02CHISEA-SEAZCHARBONNET26-90:no|KXNFLWINMARGIN-26NOV02CHISEA-TIE:yes"
    now = timeutil.now()
    db.insert(S.requote_panel, {"combo_ticker": "KXMVETIE", "env": "mock", "signature": sig, "game_code": "26NOV02CHISEA",
                                "kickoff": now + 86400, "added_at": now, "reason": "top_pick", "active": 1})
    db.insert(S.requote_slots, {"combo_ticker": "KXMVETIE", "offset_h": 24, "due_at": now - 60, "status": "pending"})
    due, _ = due_slots(db, "mock", now)
    assert not [d for d in due if d["combo_ticker"] == "KXMVETIE"]
    assert db.scalar("SELECT active FROM requote_panel WHERE combo_ticker='KXMVETIE'") == 0
