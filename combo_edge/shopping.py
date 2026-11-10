"""Quote shopping (Phase 3).

- ``shop_signature``: the Shop button. Re-RFQ a combo 2-3 times a few seconds apart and report the best
  quote and the spread between rounds and makers. Never buys.
- ``shop_for_execution``: the execution module's re-RFQ for fixable gate misses (stale quote, stake
  mismatch, near-miss EV, canary). Each round's RFQ runs with the execution hook while it is open, so
  every gate is re-checked on the fresh quote; it stops at the first round that executes or paper-fills.
- ``fade_report``: are quotes on our RFQs getting worse versus how similar combos trade publicly? Quotes
  on other people's RFQs are private, so the benchmark is the public trade tape of the same combo
  markets around the time we quoted them.
"""

from __future__ import annotations

import asyncio
import logging
import statistics
from typing import Any

from combo_edge import timeutil
from combo_edge.context import EnvClients, make_clients
from combo_edge.pricing import parse_dollars
from combo_edge.probe import ExecutionHook, ProbeEngine, ProbeRequest, ProbeResult
from combo_edge.quotes import QuoteRouter
from combo_edge.storage import Database
from combo_edge.storage import schema as S

log = logging.getLogger(__name__)


async def _rounds(clients: EnvClients, db: Database, run_id: int | None, ticker: str, signature: str, stake: float, rounds: int, gap_s: float,
                  window: float, purpose: str, hook: ExecutionHook | None = None, candidate: Any = None, ws: Any = None, router: Any = None,
                  show_id_rounds: frozenset[int] = frozenset()) -> list[ProbeResult]:
    own_ws = ws is None
    if own_ws:
        router = QuoteRouter()
        ws = clients.websocket([{"channels": ["communications"], "user_filter": "self"}], name=purpose)
        ws.add_handler(router.handle)
        await ws.start()
        await ws.wait_connected(15)
    results: list[ProbeResult] = []
    try:
        eng = ProbeEngine(clients.api, ws, router, db, run_id=run_id, max_open=1, execution_hook=hook)
        for i in range(rounds):
            r = await eng.probe(ProbeRequest(ticker, signature, purpose, stake, window, candidate=candidate, allow_execution=hook is not None,
                                             obscure_creator_id=i not in show_id_rounds))
            results.append(r)
            ex = r.execution or {}
            if ex.get("executed") or ex.get("decision") in ("paper_fill", "accept", "abort", "maker_no_confirm"):
                break
            if ex.get("decision") == "reject" and not ex.get("fixable"):
                break  # another round can't fix it (limits, duplicate, kill switch, gates on the fair model)
            if i + 1 < rounds:
                await asyncio.sleep(gap_s)
    finally:
        if own_ws:
            await ws.stop()
    return results


def summarize_rounds(results: list[ProbeResult]) -> dict[str, Any]:
    quoted = [r for r in results if r.taker_yes_price]
    prices = [r.taker_yes_price for r in quoted]
    makers = [q.taker_yes_price for r in quoted for q in r.quotes if q.taker_yes_price]
    best = min(prices) if prices else None
    best_r = next((r for r in quoted if r.taker_yes_price == best), None)
    return {"rounds": len(results), "quoted": len(quoted), "best_price": best, "best_payout": best_r.combo_payout if best_r else None,
            "spread_between_rounds": (max(prices) - min(prices)) if len(prices) > 1 else None,
            "spread_between_makers": (max(makers) - min(makers)) if len(makers) > 1 else None,
            "per_round": [{"price": r.taker_yes_price, "payout": r.combo_payout, "makers": r.n_makers, "outcome": r.outcome,
                           "id_shown": not r.request.obscure_creator_id} for r in results],
            "best_payout_anonymous": _best_payout([r for r in quoted if r.request.obscure_creator_id]),
            "best_payout_your_id": _best_payout([r for r in quoted if not r.request.obscure_creator_id]),
            "ts": timeutil.now()}


def _best_payout(results: list[ProbeResult]) -> float | None:
    pays = [r.combo_payout for r in results if r.combo_payout]
    return max(pays) if pays else None


async def shop_signature(db: Database, env: str, signature: str) -> dict[str, Any]:
    from combo_edge.scan import runtime_settings

    st = runtime_settings(db)
    row = db.fetchone("SELECT ticker FROM combos WHERE signature=:s", {"s": signature})
    if not row:
        raise RuntimeError("combo not found in the index")
    clients = make_clients(env, db, st.rate_utilization)
    run_id = db.insert(S.runs, {"kind": "shop", "env": env, "status": "running", "started_at": timeutil.now()})
    try:
        if not clients.authenticated:
            raise RuntimeError(clients.credentials_error or f"no credentials for {env}")
        # One round goes out under your ID (the 2nd, so it sits between two anonymous rounds) to test whether
        # makers quote the scanner's anonymous RFQs worse than your app. Never buys.
        rounds = max(2, st.shop_rounds)
        res = await _rounds(clients, db, run_id, row["ticker"], signature, st.stake, rounds, st.shop_gap_s, st.quote_window_s, "shop",
                            show_id_rounds=frozenset({1}))
        out = {"signature": signature, "ticker": row["ticker"], **summarize_rounds(res)}
        db.update(S.runs, {"id": run_id}, {"status": "done", "finished_at": timeutil.now(), "summary": out})
        return out
    finally:
        await clients.aclose()


def make_execution_shopper(clients: EnvClients, db: Database, run_id: int | None, engine: Any, ws: Any, router: Any, st: Any):  # type: ignore[no-untyped-def]
    async def shop(cand: Any, stake: float) -> list[ProbeResult]:
        if not cand.combo_ticker:
            return []
        # decide while every quote that arrived in the window is still within the quote-age gate
        window = max(1.0, min(st.quote_window_s, st.max_quote_age_s - 0.5))
        return await _rounds(clients, db, run_id, cand.combo_ticker, cand.signature, stake, st.shop_rounds, st.shop_gap_s,
                             window, "shop", hook=engine.on_probe, candidate=cand, ws=ws, router=router)

    return shop


async def fade_report(clients: EnvClients, db: Database, env: str, max_combos: int = 15) -> dict[str, Any] | None:
    """Our quoted taker price vs public trades on the same combo within 6 h (positive = we're quoted worse)."""
    rows = db.fetchall("SELECT combo_ticker, taker_yes_price, created_at FROM rfqs WHERE env=:e AND taker_yes_price IS NOT NULL "
                       "AND created_at >= :t ORDER BY created_at DESC LIMIT 200", {"e": env, "t": timeutil.now() - 7 * 86400})
    seen: set[str] = set()
    diffs: list[tuple[float, float]] = []
    for r in rows:
        t = r["combo_ticker"]
        if t in seen or len(seen) >= max_combos:
            continue
        seen.add(t)
        try:
            trades = await clients.api.trades(t, min_ts=int(r["created_at"] - 6 * 3600), max_ts=int(r["created_at"] + 6 * 3600))
        except Exception:  # noqa: BLE001
            continue
        px = [parse_dollars(x.get("yes_price_dollars")) for x in trades if (x.get("taker_outcome_side") or x.get("taker_side")) == "yes"]
        px = [p for p in px if p]
        if px:
            diffs.append((r["created_at"], float(r["taker_yes_price"]) / statistics.median(px) - 1.0))
    if not diffs:
        return None
    diffs.sort()
    half = len(diffs) // 2
    early = [d for _, d in diffs[:half]] or [0.0]
    late = [d for _, d in diffs[half:]]
    out = {"n": len(diffs), "mean": statistics.mean(d for _, d in diffs), "early": statistics.mean(early), "late": statistics.mean(late),
           "fading": statistics.mean(late) - statistics.mean(early) > 0.05, "ts": timeutil.now()}
    db.meta_set(f"fade:{env}", out)
    return out
