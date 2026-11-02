"""Execution module, exercised ONLY against the in-process mock exchange (network is blocked in tests)."""

import pytest

from combo_edge import execution
from combo_edge.context import default_db
from combo_edge.hooks import default_hooks
from combo_edge.scan import ScanRequest, run_scan


def _loose_gates(db, **extra) -> None:  # type: ignore[no-untyped-def]
    db.settings_set({"stratum_min_quotes": 0, "ev_min": -0.99, "stake_tolerance": 100.0, "fair_lower_pct": 0.0,
                     "exploration_mode": False, "max_bets_per_day": 50, "daily_loss_limit": 1000, "weekly_loss_limit": 5000,
                     "shop_gap_s": 0.2, "shop_rounds": 2, **extra})


def _force_live(db, monkeypatch, env: str = "mock") -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(execution, "live_forbidden", lambda: False)
    db.risk_set("execution_mode", "live")
    db.risk_set("live_confirmation", {"env": env, "ts": 0})
    db.meta_set(f"side_check:{env}", {"quote_side": 30})


def test_default_mode_is_paper_and_live_needs_everything(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    db = default_db()
    assert execution.get_mode(db) == "paper"
    with pytest.raises(execution.ExecutionError, match="disabled"):
        execution.set_mode(db, "live", "prod", "GO LIVE PROD")  # COMBO_EDGE_FORBID_LIVE=1 in tests
    monkeypatch.setattr(execution, "live_forbidden", lambda: False)
    with pytest.raises(execution.ExecutionError, match="real environment"):
        execution.set_mode(db, "live", "mock", "GO LIVE MOCK")
    with pytest.raises(execution.ExecutionError, match="type exactly"):
        execution.set_mode(db, "live", "prod", "go live")
    with pytest.raises(execution.ExecutionError, match="side mapping"):
        execution.set_mode(db, "live", "prod", "GO LIVE PROD")
    db.meta_set("side_check:prod", {"quote_side": 25})
    execution.set_mode(db, "live", "prod", "GO LIVE PROD")
    assert execution.get_mode(db) == "live"
    execution.kill(db)
    assert execution.get_mode(db) == "off" and execution.is_killed(db)
    with pytest.raises(execution.ExecutionError, match="kill switch"):
        execution.set_mode(db, "live", "prod", "GO LIVE PROD")


async def test_paper_mode_records_fills_and_trips_bet_limit(mock_exchange) -> None:  # type: ignore[no-untyped-def]
    db = default_db()
    _loose_gates(db, max_bets_per_day=3)
    await run_scan(ScanRequest(env="mock", rfq_budget=15, window_s=2.0, families=["qb_ptd_x_team_total"]), db=db, hooks=default_hooks())
    fills = db.fetchall("SELECT * FROM executions WHERE book='paper'")
    assert len(fills) == 3  # limit of 3 bets/day, then execution goes OFF
    assert all(f["status"] == "executed" and f["price"] and f["contracts"] > 0 for f in fills)
    assert execution.get_mode(db) == "off" and "limit" in (db.risk_get("trip_reason") or "")
    assert mock_exchange.accepts == 0  # paper never accepts
    assert not mock_exchange.open_rfq_by_ticker
    decisions = {d["decision"] for d in db.fetchall("SELECT decision FROM execution_decisions")}
    assert {"paper_fill", "reject"} <= decisions


async def test_paper_mode_respects_caps_and_duplicates(mock_exchange) -> None:  # type: ignore[no-untyped-def]
    db = default_db()
    _loose_gates(db, cap_game=12.0)
    await run_scan(ScanRequest(env="mock", rfq_budget=12, window_s=2.0, families=["qb_ptd_x_team_total"]), db=db, hooks=default_hooks())
    per_game = db.fetchall("SELECT game_code, SUM(stake) AS s FROM executions WHERE book='paper' GROUP BY game_code")
    assert per_game and all(r["s"] <= 12.0 + 1e-9 for r in per_game)
    sigs = [r["signature"] for r in db.fetchall("SELECT signature FROM executions")]
    assert len(sigs) == len(set(sigs))


async def test_live_canary_accept_verify_and_upgrade_offer(mock_exchange, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    db = default_db()
    _loose_gates(db, max_bets_per_day=1)
    _force_live(db, monkeypatch)
    await run_scan(ScanRequest(env="mock", rfq_budget=6, window_s=2.0, families=["qb_ptd_x_team_total"]), db=db, hooks=default_hooks())
    live = db.fetchall("SELECT * FROM executions WHERE book='live'")
    assert len(live) == 1, db.fetchall("SELECT decision, reason FROM execution_decisions")
    ex = live[0]
    assert ex["is_canary"] == 1 and ex["stake"] == pytest.approx(1.0)  # first live trade is a $1 canary
    assert ex["accepted_side"] == "no"  # buying YES = taking the maker's NO bid
    assert ex["status"] == "executed" and ex["verified"] == 1, ex["verify_detail"]
    assert db.risk_get("canary_passed:mock") is True
    assert db.risk_get("offer_upgrade") is True
    assert mock_exchange.accepts == 1 and mock_exchange.fills[0]["outcome_side"] == "yes"
    from combo_edge.cli import upgrade_tier

    ok, msg = await upgrade_tier("mock", db)
    assert ok and "advanced" in msg
    assert not mock_exchange.open_rfq_by_ticker


async def test_maker_no_confirm_is_logged_without_retry(mock_exchange, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    db = default_db()
    _loose_gates(db, max_bets_per_day=1)
    _force_live(db, monkeypatch)
    db.risk_set("canary_passed:mock", True)
    mock_exchange.faults.no_confirm = True
    await run_scan(ScanRequest(env="mock", rfq_budget=1, window_s=2.0, families=["qb_ptd_x_team_total"]), db=db, hooks=default_hooks())
    live = db.fetchall("SELECT status FROM executions WHERE book='live'")
    assert live and live[0]["status"] == "maker_no_confirm"
    assert mock_exchange.accepts == 0
    assert not mock_exchange.open_rfq_by_ticker


async def test_contract_mismatch_aborts_and_turns_execution_off(mock_exchange, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    db = default_db()
    _loose_gates(db)
    _force_live(db, monkeypatch)
    db.risk_set("canary_passed:mock", True)
    mock_exchange.faults.wrong_accept_side_contracts = True
    await run_scan(ScanRequest(env="mock", rfq_budget=3, window_s=2.0, families=["qb_ptd_x_team_total"]), db=db, hooks=default_hooks())
    statuses = [r["status"] for r in db.fetchall("SELECT status FROM executions WHERE book='live'")]
    assert statuses and statuses[0] == "aborted"
    assert execution.get_mode(db) == "off" and "side mapping" in (db.risk_get("trip_reason") or "")
    assert mock_exchange.accepts == 0  # RFQ deleted before the maker confirmed


async def test_kill_switch_blocks_execution(mock_exchange) -> None:  # type: ignore[no-untyped-def]
    db = default_db()
    _loose_gates(db)
    execution.kill(db)
    execution.clear_kill(db)
    db.risk_set("kill_switch", True)
    db.risk_set("execution_mode", "paper")
    await run_scan(ScanRequest(env="mock", rfq_budget=5, window_s=2.0, families=["qb_ptd_x_team_total"]), db=db, hooks=default_hooks())
    assert db.scalar("SELECT COUNT(*) FROM executions") == 0


async def test_strict_default_gates_block_unproven_strata(mock_exchange) -> None:  # type: ignore[no-untyped-def]
    db = default_db()
    await run_scan(ScanRequest(env="mock", rfq_budget=8, window_s=2.0, families=["qb_ptd_x_team_total"]), db=db, hooks=default_hooks())
    assert db.scalar("SELECT COUNT(*) FROM executions") == 0  # < 30 quotes per stratum: nothing executes, even on paper
    reasons = " ".join(r["reason"] for r in db.fetchall("SELECT reason FROM execution_decisions"))
    assert "stratum quotes" in reasons


async def test_paper_and_live_ledgers_settle_separately_with_clv(mock_exchange) -> None:  # type: ignore[no-untyped-def]
    from combo_edge import timeutil
    from combo_edge.positions import book_summary, settle_executions
    from combo_edge.storage import schema as S

    db = default_db()
    now = timeutil.now()
    db.insert(S.games, {"game_id": "g1", "game_code": "G1", "kickoff": now + 3600})
    rows = [("paper", "CA", "yes", 5.0, 100.0, 20.0), ("paper", "CB", "no", 4.0, 50.0, 12.5), ("live", "CC", "yes", 1.0, 30.0, 30.0)]
    for book, ticker, _res, stake, contracts, payout in rows:
        db.insert(S.executions, {"ts": now - 600, "book": book, "env": "mock", "combo_ticker": ticker, "signature": ticker, "game_code": "G1",
                                 "rfq_id": f"own-{ticker}", "price": stake / contracts, "contracts": contracts, "stake": stake,
                                 "payout": payout, "status": "executed"})
        db.insert(S.rfqs, {"rfq_id": f"own-{ticker}", "env": "mock", "combo_ticker": ticker, "purpose": "probe", "created_at": now - 610,
                           "outcome": "quoted", "taker_yes_price": stake / contracts, "combo_payout": payout})
    # a later re-quote of CA at a shorter payout (the price moved our way): CLV = 20 / 16 - 1 = +25%
    db.insert(S.rfqs, {"rfq_id": "rq-CA", "env": "mock", "combo_ticker": "CA", "purpose": "requote", "created_at": now - 60,
                       "outcome": "quoted", "taker_yes_price": 1 / 16, "combo_payout": 16.0})

    class StubAPI:
        env = "mock"

        async def markets_by_tickers(self, tickers):  # type: ignore[no-untyped-def]
            return [{"ticker": t, "result": res} for _b, t, res, *_ in rows if t in tickers]

    assert await settle_executions(StubAPI(), db) == 3  # type: ignore[arg-type]
    paper, live = book_summary(db, "mock", "paper"), book_summary(db, "mock", "live")
    assert paper["n"] == 2 and live["n"] == 1  # books never mix
    assert paper["pnl"] == pytest.approx((100 - 5) - 4) and live["pnl"] == pytest.approx(30 - 1)
    assert paper["roi"] == pytest.approx(91 / 9)
    assert paper["clv_mean"] == pytest.approx(0.25)  # only CA has a re-quote; the bet's own RFQ never counts as the close
    assert live["clv_mean"] is None
