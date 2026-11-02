"""Shop button and the optional full-scan scheduler, against the mock exchange only."""

from combo_edge.context import default_db
from combo_edge.hooks import default_hooks
from combo_edge.scan import ScanRequest, run_scan
from combo_edge.scheduler.fullscan import fullscan_tick
from combo_edge.shopping import shop_signature


async def test_shop_signature_reports_dispersion_and_never_buys(mock_exchange) -> None:  # type: ignore[no-untyped-def]
    db = default_db()
    db.settings_set({"shop_gap_s": 0.2, "shop_rounds": 3, "quote_window_s": 1.5})
    res = await run_scan(ScanRequest(env="mock", rfq_budget=3, window_s=1.5, families=["qb_ptd_x_team_total"]), db=db, hooks=default_hooks())
    sig = next(r.signature for r in res.rows if r.combo_ticker)
    executions_before = db.scalar("SELECT COUNT(*) FROM executions")
    out = await shop_signature(db, "mock", sig)
    assert out["rounds"] == 3 and out["quoted"] >= 1 and out["best_price"]
    assert len(out["per_round"]) == 3
    assert db.scalar("SELECT COUNT(*) FROM rfqs WHERE purpose='shop'") == 3
    assert db.scalar("SELECT COUNT(*) FROM executions") == executions_before  # the Shop button never buys
    assert mock_exchange.accepts == 0
    assert not mock_exchange.open_rfq_by_ticker


async def test_fullscan_is_off_by_default_and_runs_once_per_new_props(mock_exchange) -> None:  # type: ignore[no-untyped-def]
    db = default_db()
    assert (await fullscan_tick("mock"))["status"] == "disabled"
    db.settings_set({"fullscan_enabled": True, "rfq_budget": 3, "quote_window_s": 1.5})
    first = await fullscan_tick("mock")
    assert first["status"] == "done" and first["new_prop_events"] > 0
    assert (await fullscan_tick("mock"))["status"] == "nothing_new"  # same markets, no recommendation for today
