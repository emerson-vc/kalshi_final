"""Re-quote panel (Phase 1.5): time costs no creations, so a fixed panel of existing combos is re-RFQ'd
at set offsets before kickoff to measure how the edge moves toward game time.

- Membership (after each scan): the top exploit picks plus a stratified random sample of quoted probes,
  at most ``panel_max`` active.
- Slots: offsets 144/96/72/48/24/12/6/1 h before kickoff. Slots already in the past when a combo joins
  (props typically open only ~4 days out) are marked ``skipped_past``. A slot runs if the job fires within
  max(15 min, 10% of the offset) after it's due, else it's marked ``missed``.
- ``requote_due`` is what the launchd agent runs every 10 minutes. It ONLY RFQs existing tickers: it never
  creates combos and never places bets (no execution hook).

``recheck_positions`` (My bets) re-quotes every combo you hold the same way.
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections import defaultdict
from typing import Any

from combo_edge import timeutil
from combo_edge.context import EnvClients, make_clients
from combo_edge.probe import ProbeEngine, ProbeRequest
from combo_edge.quotes import QuoteRouter
from combo_edge.storage import Database
from combo_edge.storage import schema as S

log = logging.getLogger(__name__)


def tolerance_s(offset_h: float) -> float:
    return max(15 * 60.0, 0.10 * offset_h * 3600.0)


def add_to_panel(db: Database, env: str, rows: list[Any], kickoffs: dict[str, float | None], st: Any, seed: int | None = None) -> int:
    """rows: ranked ResultRow list from a scan."""
    active = int(db.scalar("SELECT COUNT(*) FROM requote_panel WHERE env=:e AND active=1", {"e": env}, 0))
    room = max(0, st.panel_max - active)
    if room == 0:
        return 0
    quoted = [r for r in rows if r.outcome == "quoted" and r.combo_ticker]
    picks = [(r, "top") for r in quoted[: st.panel_top_k]]
    rest = quoted[st.panel_top_k :]
    by_cell: dict[str, list[Any]] = defaultdict(list)
    for r in rest:
        by_cell[r.stratum_l1].append(r)
    rng = random.Random(seed)
    cells = list(by_cell)
    rng.shuffle(cells)
    k = 0
    while k < st.panel_random_per_run and any(by_cell.values()):
        for c in cells:
            if by_cell[c] and k < st.panel_random_per_run:
                picks.append((by_cell[c].pop(rng.randrange(len(by_cell[c]))), "stratified_sample"))
                k += 1
    now = timeutil.now()
    added = 0
    for r, reason in picks[:room]:
        if db.fetchone("SELECT 1 AS x FROM requote_panel WHERE combo_ticker=:t", {"t": r.combo_ticker}):
            continue
        ko = kickoffs.get(r.game_code)
        if not ko or ko <= now:
            continue
        db.insert(S.requote_panel, {"combo_ticker": r.combo_ticker, "env": env, "signature": r.signature, "game_code": r.game_code,
                                    "kickoff": ko, "added_at": now, "reason": reason, "stratum_l1": r.stratum_l1, "active": 1})
        for off in st.requote_offsets_h:
            due = ko - off * 3600
            status = "pending" if due >= now - tolerance_s(off) else "skipped_past"
            db.upsert(S.requote_slots, {"combo_ticker": r.combo_ticker, "offset_h": off, "due_at": due, "status": status}, ["combo_ticker", "offset_h"])
        added += 1
    return added


def due_slots(db: Database, env: str, now: float | None = None) -> tuple[list[dict[str, Any]], int]:
    from combo_edge.universe import signature_has_excluded_leg

    now = now or timeutil.now()
    for p in db.fetchall("SELECT combo_ticker, signature FROM requote_panel WHERE env=:e AND active=1", {"e": env}):
        if signature_has_excluded_leg(p["signature"] or ""):
            db.update(S.requote_panel, {"combo_ticker": p["combo_ticker"]}, {"active": 0, "reason": "excluded leg (win margin TIE)"})
    rows = db.fetchall(
        "SELECT s.combo_ticker, s.offset_h, s.due_at, p.signature, p.kickoff FROM requote_slots s JOIN requote_panel p ON p.combo_ticker = s.combo_ticker "
        "WHERE p.env=:e AND p.active=1 AND s.status='pending' AND s.due_at <= :n ORDER BY s.due_at",
        {"e": env, "n": now},
    )
    due, missed = [], 0
    for r in rows:
        if now - r["due_at"] > tolerance_s(r["offset_h"]):
            db.update(S.requote_slots, {"combo_ticker": r["combo_ticker"], "offset_h": r["offset_h"]}, {"status": "missed"})
            missed += 1
        else:
            due.append(r)
    # retire panel members whose game has started
    db.execute("UPDATE requote_panel SET active=0 WHERE env=:e AND kickoff <= :n", {"e": env, "n": now})
    return due, missed


async def _probe_tickers(clients: EnvClients, db: Database, run_id: int, items: list[dict[str, Any]], purpose: str, stake: float, window: float, max_open: int) -> list[Any]:
    router = QuoteRouter()
    ws = clients.websocket([{"channels": ["communications"], "user_filter": "self"}], name=purpose)
    ws.add_handler(router.handle)
    await ws.start()
    await ws.wait_connected(15)
    try:
        await refresh_combo_legs(clients, db, run_id, [i["signature"] for i in items if i.get("signature")])
        eng = ProbeEngine(clients.api, ws, router, db, run_id=run_id, max_open=max_open)
        reqs = [ProbeRequest(i["combo_ticker"], i.get("signature") or "", purpose, stake, window,
                             hours_to_start=timeutil.hours_until(i.get("kickoff")), offset_h=i.get("offset_h"), allow_execution=False)
                for i in items]
        return await eng.probe_many(reqs)
    finally:
        await ws.stop()


async def refresh_combo_legs(clients: EnvClients, db: Database, run_id: int, signatures: list[str]) -> None:
    """Snapshot the legs of re-quoted combos so every re-quote label has its own leg context."""
    from combo_edge.candidates import parse_signature
    from combo_edge.snapshots import _apply_market

    tickers = sorted({t for s in signatures for t, _ in parse_signature(s) if t})
    if not tickers:
        return
    try:
        markets = await clients.api.markets_by_tickers(tickers)
    except Exception as exc:  # noqa: BLE001
        log.warning("leg refresh failed: %s", exc)
        return
    from combo_edge.sports.base import Leg

    now = timeutil.now()
    rows = []
    for m in markets:
        leg = Leg(m["ticker"], m.get("event_ticker", ""), m.get("event_ticker", "").split("-")[0], "", "", "")
        _apply_market(leg, m)
        rows.append({"run_id": run_id, "ticker": leg.ticker, "ts": now, "yes_bid": leg.yes_bid, "yes_ask": leg.yes_ask, "no_bid": leg.no_bid,
                     "no_ask": leg.no_ask, "yes_bid_size": leg.yes_bid_size, "yes_ask_size": leg.yes_ask_size, "spread": leg.spread(),
                     "volume": leg.volume, "open_interest": leg.open_interest, "last_price": leg.last_price})
    db.insert_many(S.leg_snapshots, rows)


async def requote_due(env: str, db: Database | None = None) -> dict[str, Any]:
    from combo_edge import execution
    from combo_edge.context import default_db
    from combo_edge.scan import runtime_settings
    from combo_edge.scheduler.locks import ProcessLock
    from combo_edge.settings import data_dir

    db = db or default_db()
    st = runtime_settings(db)
    if not st.requote_enabled:
        return {"status": "disabled"}
    if execution.is_killed(db):
        return {"status": "kill_switch"}
    lock = ProcessLock(data_dir() / "locks" / f"requote-{env}.lock")
    if not lock.acquire():
        return {"status": "busy"}
    run_id = db.insert(S.runs, {"kind": "requote", "env": env, "status": "running", "started_at": timeutil.now()})
    summary: dict[str, Any] = {"probed": 0, "quoted": 0, "missed": 0}
    status = "done"
    try:
        due, missed = due_slots(db, env)
        summary["missed"] = missed
        if due:
            clients = make_clients(env, db, st.rate_utilization)
            try:
                if not clients.authenticated:
                    raise RuntimeError(clients.credentials_error or f"no credentials for {env}")
                await clients.configure_limits(db, run_id, "requote")
                results = await _probe_tickers(clients, db, run_id, due, "requote", st.stake, st.quote_window_s, min(st.max_open_rfqs, 20))
            finally:
                await clients.aclose()
            for slot, res in zip(due, results, strict=True):
                db.update(S.requote_slots, {"combo_ticker": slot["combo_ticker"], "offset_h": slot["offset_h"]},
                          {"status": "done" if res.rfq_id else "failed", "rfq_id": res.rfq_id, "done_at": timeutil.now()})
            summary["probed"] = sum(1 for r in results if r.rfq_id)
            summary["quoted"] = sum(1 for r in results if r.quoted)
    except Exception as exc:  # noqa: BLE001
        status = "failed"
        summary["error"] = str(exc)[:300]
        db.log_error("requote", str(exc))
    finally:
        lock.release()
        db.update(S.runs, {"id": run_id}, {"status": status, "finished_at": timeutil.now(), "summary": summary})
    return {"status": status, **summary}


def requote_due_sync(env: str) -> dict[str, Any]:
    return asyncio.run(requote_due(env))


async def recheck_positions(db: Database, env: str) -> dict[str, Any]:
    from combo_edge.positions import combo_positions, settle_executions, sync_portfolio
    from combo_edge.scan import runtime_settings

    st = runtime_settings(db)
    clients = make_clients(env, db, st.rate_utilization)
    run_id = db.insert(S.runs, {"kind": "recheck", "env": env, "status": "running", "started_at": timeutil.now()})
    try:
        if not clients.authenticated:
            raise RuntimeError(clients.credentials_error or f"no credentials for {env}")
        synced = await sync_portfolio(clients.api, db)
        positions = combo_positions(db, env)
        items = [{"combo_ticker": p["ticker"], "signature": p.get("signature") or ""} for p in positions]
        results = await _probe_tickers(clients, db, run_id, items, "recheck", st.stake, st.quote_window_s, 20) if items else []
        settled = await settle_executions(clients.api, db)
        out = {"synced": synced, "probed": len(results), "quoted": sum(1 for r in results if r.quoted), "settled": settled}
        db.update(S.runs, {"id": run_id}, {"status": "done", "finished_at": timeutil.now(), "summary": out})
        return out
    except Exception as exc:
        db.update(S.runs, {"id": run_id}, {"status": "failed", "finished_at": timeutil.now(), "error": str(exc)[:300]})
        raise
    finally:
        await clients.aclose()
