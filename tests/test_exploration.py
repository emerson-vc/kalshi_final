import plistlib
from collections import Counter

import pytest

from combo_edge import timeutil
from combo_edge.context import default_db, make_clients
from combo_edge.hooks import default_hooks
from combo_edge.learning.exploration import exploration_candidates, feed_candidates, make_picker
from combo_edge.learning.report import write_exploration_report
from combo_edge.recorder import Recorder, nfl_same_game
from combo_edge.requote import add_to_panel, due_slots, requote_due, tolerance_s
from combo_edge.scan import ScanRequest, run_scan, runtime_settings
from combo_edge.scheduler import launchd
from combo_edge.settings import RuntimeSettings
from combo_edge.storage import schema as S
from combo_edge.universe import build_universe


async def _universe():  # type: ignore[no-untyped-def]
    c = make_clients("mock", default_db())
    uni = await build_universe(c.api, "KXMVECROSSCATEGORY-R")
    await c.aclose()
    return uni


async def test_exploration_samples_many_cells_incl_prot_never_precise(mock_exchange) -> None:  # type: ignore[no-untyped-def]
    uni = await _universe()
    cands = exploration_candidates(uni, RuntimeSettings(), blocked=set(), seed=1)
    cells = Counter(c.family for c in cands)
    assert len(cells) > 150  # far beyond the template families
    assert any("[prot]" in f for f in cells)
    assert not any(s.leg.precise for c in cands for s in c.legs)
    assert any(s.side == "no" for c in cands for s in c.legs)
    rels = {f.rsplit("|", 1)[-1] for f in cells}
    assert {"same_team", "opponent", "game"} <= rels


async def test_feed_combos_become_free_candidates(mock_exchange) -> None:  # type: ignore[no-untyped-def]
    db = default_db()
    uni = await _universe()
    rec = Recorder(db, "mock")
    for _ in range(5):
        rec.handle({"type": "rfq_created", "msg": mock_exchange.emit_feed_rfq(2, "26NOV05JACBAL")})
    rec.flush()
    free = feed_candidates(db, uni)
    assert free and all(c.existed and c.combo_ticker for c in free)


def test_picker_fills_least_sampled_cells_first() -> None:
    from combo_edge.candidates import Candidate
    from combo_edge.sports.base import Leg, SidedLeg

    db = default_db()

    def cand(cell: str, i: int) -> Candidate:
        l1 = Leg(f"T{cell}{i}a", "E1", "S", "G", "x", "team")
        l2 = Leg(f"T{cell}{i}b", "E2", "S", "G", "y", "team")
        c = Candidate((SidedLeg(l1, "yes"), SidedLeg(l2, "yes")), f"explore:{cell}", "G")
        c.stratum_l1 = cell
        return c

    cands = [cand(cell, i) for cell in ("A", "B", "C") for i in range(10)]
    picked = make_picker(db, "mock", RuntimeSettings(), seed=3)(cands, 6, 6)
    assert Counter(c.stratum_l1 for c in picked) == {"A": 2, "B": 2, "C": 2}


def test_recorder_filters_and_counts(mock_exchange) -> None:  # type: ignore[no-untyped-def]
    db = default_db()
    rec = Recorder(db, "mock")
    two = mock_exchange.emit_feed_rfq(2, "26NOV05JACBAL")
    four = mock_exchange.emit_feed_rfq(4, "26NOV02CHISEA")
    multi = {"id": "m", "market_ticker": "X", "mve_selected_legs": [
        {"event_ticker": "KXNFLGAME-26NOV05JACBAL", "market_ticker": "KXNFLGAME-26NOV05JACBAL-JAC", "side": "yes"},
        {"event_ticker": "KXNFLGAME-26NOV08GBNE", "market_ticker": "KXNFLGAME-26NOV08GBNE-GB", "side": "yes"}]}
    nba = {"id": "n", "market_ticker": "Y", "mve_selected_legs": [
        {"event_ticker": "KXNBAGAME-26NOV05AB", "market_ticker": "a", "side": "yes"}, {"event_ticker": "KXNBAGAME-26NOV05AB", "market_ticker": "b", "side": "yes"}]}
    for m in (two, four, multi, nba):
        rec.handle({"type": "rfq_created", "msg": m})
    rec.handle({"type": "quote_created", "msg": {"quote_id": "q1", "rfq_id": "not-ours", "yes_bid_dollars": "0.01", "no_bid_dollars": "0.9"}})
    rec.flush()
    assert rec.stats["nfl_sg"] == 2
    assert db.scalar("SELECT COUNT(*) FROM feed_rfqs") == 1  # only the 2-leg combo gets a full row
    assert db.scalar("SELECT SUM(n) FROM feed_pair_counts") == 1 + 6  # C(2,2) + C(4,2)
    assert db.fetchone("SELECT source FROM combos WHERE ticker=:t", {"t": two["market_ticker"]})["source"] == "feed"
    assert db.scalar("SELECT COUNT(*) FROM quotes WHERE source='recorder'") == 0  # not our RFQ
    assert nfl_same_game(multi["mve_selected_legs"]) is None


async def test_panel_slots_and_requote_due_never_creates(mock_exchange) -> None:  # type: ignore[no-untyped-def]
    db = default_db()
    res = await run_scan(ScanRequest(env="mock", rfq_budget=15, window_s=2.0), db=db, hooks=default_hooks())
    assert res.status == "done"
    pools = Counter(r.pool for r in res.rows)
    assert pools.get("explore", 0) > 0
    n_panel = db.scalar("SELECT COUNT(*) FROM requote_panel")
    assert n_panel > 0
    statuses = Counter(r["status"] for r in db.fetchall("SELECT status FROM requote_slots"))
    assert statuses["pending"] > 0 and statuses["skipped_past"] > 0  # kickoff ~30h out: 6d/4d/3d/2d already past
    # make the 24h slot due now
    db.execute("UPDATE requote_slots SET due_at=:n WHERE offset_h=24", {"n": timeutil.now() - 60})
    db.settings_set({"requote_enabled": True})
    creations_before = db.scalar("SELECT COUNT(*) FROM creation_ledger")
    decisions_before = db.scalar("SELECT COUNT(*) FROM execution_decisions")  # the scan's own (paper) decisions
    out = await requote_due("mock", db)
    assert out["status"] == "done" and out["probed"] == n_panel
    assert db.scalar("SELECT COUNT(*) FROM creation_ledger") == creations_before  # never creates combos
    assert db.scalar("SELECT COUNT(*) FROM execution_decisions") == decisions_before  # never bets
    assert not mock_exchange.open_rfq_by_ticker
    done = db.scalar("SELECT COUNT(*) FROM requote_slots WHERE status='done' AND offset_h=24")
    assert done == n_panel


def test_missed_slots_are_marked() -> None:
    db = default_db()
    now = timeutil.now()
    db.insert(S.requote_panel, {"combo_ticker": "C1", "env": "mock", "signature": "s", "game_code": "G", "kickoff": now + 7200, "active": 1})
    db.upsert(S.requote_slots, {"combo_ticker": "C1", "offset_h": 6, "due_at": now - tolerance_s(6) - 5, "status": "pending"}, ["combo_ticker", "offset_h"])
    due, missed = due_slots(db, "mock", now)
    assert not due and missed == 1


async def test_requote_disabled_by_default(mock_exchange) -> None:  # type: ignore[no-untyped-def]
    assert (await requote_due("mock", default_db()))["status"] == "disabled"


def test_launchd_plists_and_guard() -> None:
    rec = plistlib.loads(launchd.render("recorder"))
    assert rec["KeepAlive"] is True and rec["ProgramArguments"][-1] == "recorder"
    rq = plistlib.loads(launchd.render("requote"))
    assert rq["StartInterval"] == 600 and rq["ProgramArguments"][-1] == "requote-due"
    with pytest.raises(RuntimeError):
        launchd.install("recorder")  # never touches the real launchd from tests


async def test_exploration_report(mock_exchange) -> None:  # type: ignore[no-untyped-def]
    db = default_db()
    await run_scan(ScanRequest(env="mock", rfq_budget=10, window_s=2.0), db=db, hooks=default_hooks())
    path = write_exploration_report(db, "mock", runtime_settings(db))
    text = open(path).read()
    assert "Top 20 strata" in text and "Edge vs hours to kickoff" in text and "Template families" in text
