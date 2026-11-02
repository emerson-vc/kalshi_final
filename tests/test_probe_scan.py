import asyncio

import pytest

from combo_edge.candidates import signature_of
from combo_edge.combos import ComboResolver, CreationLedger, RejectionBook
from combo_edge.context import default_db, make_clients
from combo_edge.probe import ProbeEngine, ProbeRequest, drain_pending_cancels
from combo_edge.quotes import QUOTE_SIDE, QuoteRouter, current_side_mapping
from combo_edge.scan import ScanProgress, ScanRequest, run_scan
from combo_edge.storage import schema as S

JAC_TD = {"market_ticker": "KXNFLPASSTDS-26NOV05JACBAL-JACTLAWRENCE16-3", "event_ticker": "KXNFLPASSTDS-26NOV05JACBAL", "side": "yes"}
JAC_TT = {"market_ticker": "KXNFLTEAMTOTAL-26NOV05JACBAL-JAC29", "event_ticker": "KXNFLTEAMTOTAL-26NOV05JACBAL", "side": "yes"}


async def _engine(mock_exchange, **kw):  # type: ignore[no-untyped-def]
    db = default_db()
    clients = make_clients("mock", db)
    router = QuoteRouter()
    ws = clients.websocket([{"channels": ["communications"], "user_filter": "self"}], "test")
    ws.add_handler(router.handle)
    await ws.start()
    assert await ws.wait_connected(3)
    run_id = db.insert(S.runs, {"kind": "test", "env": "mock", "status": "running"})
    return db, clients, ws, ProbeEngine(clients.api, ws, router, db, run_id=run_id, **kw)


def _ticker(mock_exchange, legs=(JAC_TD, JAC_TT)):  # type: ignore[no-untyped-def]
    for l in legs:
        assert l["market_ticker"] in mock_exchange.markets, l
    return mock_exchange.preexisting_combo(list(legs))


async def test_probe_collects_ws_quotes_and_cancels(mock_exchange) -> None:  # type: ignore[no-untyped-def]
    db, clients, ws, eng = await _engine(mock_exchange)
    t = _ticker(mock_exchange)
    res = await eng.probe(ProbeRequest(t, "sig", "probe", 5.0, 3.5))
    assert res.outcome == "quoted" and res.n_makers >= 1
    assert res.taker_yes_price == pytest.approx(1 - res.best_no_bid)
    assert res.first_quote_ms is not None and res.combo_payout and res.combo_payout > 1
    assert not mock_exchange.open_rfq_by_ticker
    rows = db.fetchall("SELECT * FROM quotes WHERE rfq_id=:r", {"r": res.rfq_id})
    assert len(rows) == len(res.quotes) and all(r["latency_ms"] is not None for r in rows)
    assert db.meta_get("side_check:mock", {}).get(QUOTE_SIDE, 0) >= 1
    await ws.stop()
    await clients.aclose()


async def test_ws_drop_falls_back_to_rest(mock_exchange) -> None:  # type: ignore[no-untyped-def]
    db, clients, ws, eng = await _engine(mock_exchange)
    mock_exchange.faults.drop_ws_after_quotes = 1
    res = await eng.probe(ProbeRequest(_ticker(mock_exchange), "sig", "probe", 5.0, 4.0))
    assert res.rest_used and not res.ws_ok
    assert res.outcome == "quoted"
    await ws.stop()
    await clients.aclose()


async def test_no_makers_is_a_timeout(mock_exchange) -> None:  # type: ignore[no-untyped-def]
    db, clients, ws, eng = await _engine(mock_exchange)
    mock_exchange.faults.no_makers = True
    res = await eng.probe(ProbeRequest(_ticker(mock_exchange), "sig", "probe", 5.0, 1.5))
    assert res.outcome == "no_quotes_timeout"
    await ws.stop()
    await clients.aclose()


async def test_409_from_foreign_rfq_is_skipped_not_deleted(mock_exchange) -> None:  # type: ignore[no-untyped-def]
    db, clients, ws, eng = await _engine(mock_exchange)
    t = _ticker(mock_exchange)
    mock_exchange.rfqs["foreign"] = {"id": "foreign", "market_ticker": t, "target": 5.0, "status": "open", "created_ts": 0}
    mock_exchange.open_rfq_by_ticker[t] = "foreign"
    res = await eng.probe(ProbeRequest(t, "sig", "probe", 5.0, 1.0))
    assert res.outcome == "conflict_409"
    assert mock_exchange.rfqs["foreign"]["status"] == "open"  # never touched your app's RFQ
    await ws.stop()
    await clients.aclose()


async def test_failed_cancels_queue_and_drain(mock_exchange) -> None:  # type: ignore[no-untyped-def]
    db, clients, ws, eng = await _engine(mock_exchange)
    mock_exchange.faults.fail_delete_n = 50
    res = await eng.probe(ProbeRequest(_ticker(mock_exchange), "sig", "probe", 5.0, 1.0))
    assert db.scalar("SELECT COUNT(*) FROM pending_cancels") == 1
    mock_exchange.faults.fail_delete_n = 0
    assert await drain_pending_cancels(clients.api, db) == 1
    assert mock_exchange.rfqs[res.rfq_id]["status"] == "closed"
    await ws.stop()
    await clients.aclose()


async def test_combo_resolver_index_new_resolved_and_rejected(mock_exchange) -> None:  # type: ignore[no-untyped-def]
    from combo_edge.candidates import Candidate
    from combo_edge.sports.base import Leg, SidedLeg

    db = default_db()
    clients = make_clients("mock", db)
    r = ComboResolver(clients.api, db, "mock", "KXMVECROSSCATEGORY-R", None, run_cap=10, weekly_cap=5000, reserve=300)

    def cand(*legs):  # type: ignore[no-untyped-def]
        sl = tuple(SidedLeg(Leg(l["market_ticker"], l["event_ticker"], l["event_ticker"].split("-")[0], "26NOV05JACBAL", "x", "player"), l["side"]) for l in legs)
        return Candidate(sl, "test", "26NOV05JACBAL")

    old = mock_exchange.preexisting_combo([JAC_TD, JAC_TT])  # someone else created it an hour ago
    res = await r.ensure(cand(JAC_TD, JAC_TT))
    assert res.status == "resolved_existing" and res.ticker == old
    assert (await r.ensure(cand(JAC_TD, JAC_TT))).status == "index_hit"
    new_leg = {"market_ticker": "KXNFLTEAMTOTAL-26NOV05JACBAL-JAC32", "event_ticker": "KXNFLTEAMTOTAL-26NOV05JACBAL", "side": "yes"}
    assert (await r.ensure(cand(JAC_TD, new_leg))).status == "created_new"
    bad = cand(JAC_TD, JAC_TT)
    mock_exchange.faults.reject_signatures.add(signature_of([("X", "yes")]))
    ml = {"market_ticker": "KXNFLGAME-26NOV05JACBAL-JAC", "event_ticker": "KXNFLGAME-26NOV05JACBAL", "side": "yes"}
    sp = {"market_ticker": "KXNFLSPREAD-26NOV05JACBAL-JAC2", "event_ticker": "KXNFLSPREAD-26NOV05JACBAL", "side": "yes"}
    rej = await r.ensure(cand(ml, sp), rule_class="spread_implies_ml")
    assert rej.status == "rejected"
    used = CreationLedger(db, "mock").used()
    assert used["conservative"] == 3 and used["likely_new"] == 1
    assert "spread_implies_ml" not in RejectionBook(db).blocked_classes()  # needs 2 rejections to learn
    assert bad.signature
    await clients.aclose()


async def test_full_scan_on_mock(mock_exchange) -> None:  # type: ignore[no-untyped-def]
    db = default_db()
    prog = ScanProgress()
    res = await run_scan(ScanRequest(env="mock", rfq_budget=25, window_s=3.0), db=db, progress=prog)
    assert res.status == "done", res.coverage
    assert res.summary["rfqs_sent"] == 25
    assert res.summary["fill_rate"] and res.summary["fill_rate"] > 0.7
    assert res.rows and res.rows == sorted(res.rows, key=lambda r: -(r.ev or -9))
    assert not mock_exchange.open_rfq_by_ticker  # every RFQ cancelled
    assert db.scalar("SELECT COUNT(*) FROM quotes") >= 25
    assert db.scalar("SELECT COUNT(*) FROM leg_snapshots") > 0
    assert prog.counters["probes_done"] == 25
    run = db.fetchone("SELECT status, coverage FROM runs WHERE id=:i", {"i": res.run_id})
    assert run["status"] == "done"
    assert current_side_mapping(db, "mock", 5) == QUOTE_SIDE


async def test_dry_run_creates_nothing(mock_exchange) -> None:  # type: ignore[no-untyped-def]
    db = default_db()
    res = await run_scan(ScanRequest(env="mock", rfq_budget=20, dry_run=True), db=db)
    assert res.status == "dry_run" and len(res.rows) == 20
    assert not mock_exchange.rfqs and db.scalar("SELECT COUNT(*) FROM creation_ledger") == 0


async def test_coverage_error_when_props_yield_no_candidates(mock_exchange) -> None:  # type: ignore[no-untyped-def]
    db = default_db()
    res = await run_scan(ScanRequest(env="mock", rfq_budget=5, dry_run=True, families=["ml_x_h1_spread"]), db=db)
    assert any("zero candidates" in e for e in res.coverage["errors"])  # JACBAL has props but no 1H spread
