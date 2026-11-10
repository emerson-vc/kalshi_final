"""Probe: create an RFQ at your real stake, collect quotes for a window, decide, cancel.

Reliability instrumentation (this is what broke in the last build):
- The authenticated websocket is opened before any RFQ and stays open for the whole run.
- Quotes are matched to RFQs by id (``QuoteRouter``); early quotes are buffered.
- If the socket was down at any point during an RFQ's window, the quotes are re-read over REST.
  A random slice of RFQs is also cross-checked over REST to measure websocket completeness.
- Every RFQ logs time to first quote, number of makers, best and second-best bids, and its outcome:
  quoted | no_quotes_timeout | rejected_combo | conflict_409 | create_error | lease_busy | aborted.
- The execution hook runs while the RFQ is still open (quotes die when we cancel). Cancels retry, and
  any that still fail go to ``pending_cancels`` and are retried at the end of the run and on startup.
"""

from __future__ import annotations

import asyncio
import logging
import random
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from combo_edge import pricing, timeutil
from combo_edge.candidates import Candidate
from combo_edge.kalshi.api import KalshiAPI
from combo_edge.kalshi.errors import BadRequest, Conflict, KalshiError, NotFound
from combo_edge.kalshi.ws import KalshiWS
from combo_edge.quotes import INCONCLUSIVE, QuoteObs, QuoteRouter, RfqWatch, buy_yes_contracts, current_side_mapping, side_check
from combo_edge.scheduler.locks import LeaseManager
from combo_edge.storage import Database
from combo_edge.storage import schema as S

log = logging.getLogger(__name__)


@dataclass
class ProbeRequest:
    combo_ticker: str
    signature: str
    purpose: str  # probe | requote | recheck | shop | canary
    target_cost: float
    window_s: float
    candidate: Candidate | None = None
    hours_to_start: float | None = None
    offset_h: float | None = None
    feed_demand: int | None = None
    allow_execution: bool = True
    obscure_creator_id: bool = True  # False only for the Shop button's "under your ID" comparison round


@dataclass
class ProbeResult:
    request: ProbeRequest
    rfq_id: str | None = None
    outcome: str = "pending"
    reason: str = ""
    quotes: list[QuoteObs] = field(default_factory=list)
    n_makers: int = 0
    first_quote_ms: float | None = None
    best_yes_bid: float | None = None
    best_no_bid: float | None = None
    second_no_bid: float | None = None
    best_quote: QuoteObs | None = None
    taker_yes_price: float | None = None
    taker_yes_contracts: float | None = None
    combo_payout: float | None = None
    ws_ok: bool = True
    rest_used: bool = False
    created_at: float | None = None
    execution: dict[str, Any] | None = None

    @property
    def quoted(self) -> bool:
        return self.outcome == "quoted"


@dataclass
class ProbeContext:
    """What the execution hook sees while the RFQ is still open."""

    request: ProbeRequest
    result: ProbeResult
    watch: RfqWatch
    api: KalshiAPI
    side_mapping: str | None


ExecutionHook = Callable[[ProbeContext], Awaitable[dict[str, Any] | None]]


class ProbeEngine:
    def __init__(
        self,
        api: KalshiAPI,
        ws: KalshiWS | None,
        router: QuoteRouter,
        db: Database,
        *,
        run_id: int | None,
        max_open: int = 40,
        rest_crosscheck_frac: float = 0.05,
        execution_hook: ExecutionHook | None = None,
        fee_multiplier: float = 1.0,
        on_progress: Callable[[str, int], None] | None = None,
        abort_event: asyncio.Event | None = None,
    ):
        self.api = api
        self.ws = ws
        self.router = router
        self.db = db
        self.env = api.env
        self.run_id = run_id
        self.sem = asyncio.Semaphore(max_open)
        self.crosscheck = rest_crosscheck_frac
        self.execution_hook = execution_hook
        self.fee_mult = fee_multiplier
        self.on_progress = on_progress or (lambda _k, _n: None)
        self.abort = abort_event or asyncio.Event()
        self.holder = f"{self.env}:{run_id}:{uuid.uuid4().hex[:8]}"
        self.leases = LeaseManager(db, self.holder)
        self._locks: dict[str, asyncio.Lock] = {}
        self.side_counts: dict[str, int] = {}

    def side_mapping(self) -> str | None:
        return current_side_mapping(self.db, self.env)

    async def probe_many(self, reqs: list[ProbeRequest]) -> list[ProbeResult]:
        return list(await asyncio.gather(*(self.probe(r) for r in reqs)))

    async def probe(self, req: ProbeRequest) -> ProbeResult:
        res = ProbeResult(req)
        if self.abort.is_set():
            res.outcome, res.reason = "aborted", "kill switch or cancel"
            return res
        lock = self._locks.setdefault(req.combo_ticker, asyncio.Lock())
        async with self.sem, lock:
            if self.abort.is_set():
                res.outcome, res.reason = "aborted", "kill switch or cancel"
                return res
            if not self.leases.acquire(req.combo_ticker, ttl_s=req.window_s + 60):
                res.outcome, res.reason = "lease_busy", "another process holds an RFQ on this ticker"
                return res
            try:
                await self._run_rfq(req, res)
            finally:
                self.leases.release(req.combo_ticker)
        self._persist(res)
        return res

    async def _create(self, req: ProbeRequest, res: ProbeResult) -> str | None:
        for attempt in (1, 2):
            try:
                rfq_id = await self.api.create_rfq(req.combo_ticker, req.target_cost, req.obscure_creator_id)
                self.on_progress("rfqs_sent", 1)
                return rfq_id
            except Conflict:
                mine = await self._our_open_rfq_on(req.combo_ticker)
                if mine and attempt == 1:
                    await self._cancel(mine)
                    continue
                res.outcome, res.reason = "conflict_409", "an open RFQ on this ticker is not ours (maybe your app)"
                return None
            except BadRequest as exc:
                text = f"{exc.code or ''} {exc.message}".lower()
                res.outcome = "rejected_combo" if any(w in text for w in ("combination", "invalid", "conflict", "leg")) else "create_error"
                res.reason = f"{exc.code or ''} {exc.message}".strip()[:300]
                return None
            except Exception as exc:  # noqa: BLE001 - KalshiError, network errors, missing credentials
                res.outcome, res.reason = "create_error", f"{type(exc).__name__}: {exc}"[:300]
                return None
        return None

    async def _our_open_rfq_on(self, ticker: str) -> str | None:
        ours = {r["rfq_id"] for r in self.db.fetchall("SELECT rfq_id FROM rfqs WHERE env=:e AND combo_ticker=:t", {"e": self.env, "t": ticker})}
        try:
            for r in await self.api.my_rfqs(status="open"):
                if r.get("market_ticker") == ticker and r.get("id") in ours:
                    return str(r["id"])
        except KalshiError:
            return None
        return None

    async def _run_rfq(self, req: ProbeRequest, res: ProbeResult) -> None:
        rfq_id = await self._create(req, res)
        if rfq_id is None:
            return
        res.rfq_id = rfq_id
        res.created_at = timeutil.now()
        self._persist_open(res)
        watch = self.router.register(rfq_id, res.created_at)
        executed = False
        try:
            deadline = res.created_at + req.window_s
            while (left := deadline - timeutil.now()) > 0 and not self.abort.is_set():
                watch.changed.clear()
                try:
                    await asyncio.wait_for(watch.changed.wait(), timeout=left)
                except TimeoutError:
                    break
            t_end = timeutil.now()
            ws_down = self.ws is None or self.ws.was_down_between(res.created_at, t_end)
            res.ws_ok = not ws_down
            if ws_down or random.random() < self.crosscheck:
                await self._rest_quotes(rfq_id, watch, res)
            self._summarize(req, res, watch)
            if self.execution_hook is not None and req.allow_execution and res.quoted and not self.abort.is_set():
                try:
                    res.execution = await self.execution_hook(ProbeContext(req, res, watch, self.api, self.side_mapping()))
                except Exception as exc:  # noqa: BLE001 - execution failure must not leak an open RFQ
                    log.exception("execution hook failed")
                    res.execution = {"decision": "error", "reason": str(exc)[:300]}
                executed = bool(res.execution and res.execution.get("executed"))
        finally:
            self.router.unregister(rfq_id)
            if not executed:
                await self._cancel(rfq_id)

    async def _rest_quotes(self, rfq_id: str, watch: RfqWatch, res: ProbeResult) -> None:
        try:
            rows = await self.api.quotes_for_rfq(rfq_id)
        except KalshiError as exc:
            log.warning("REST quote fallback failed for %s: %s", rfq_id, exc)
            return
        res.rest_used = True
        now = timeutil.now()
        for row in rows:
            watch.add_rest(QuoteObs.from_rest(row, now))

    def _summarize(self, req: ProbeRequest, res: ProbeResult, watch: RfqWatch) -> None:
        latest = watch.latest_by_maker()
        res.quotes = list(watch.quotes)
        res.n_makers = len(latest)
        res.first_quote_ms = watch.first_quote_ms()
        self.on_progress("quotes_received", len(watch.quotes))
        if not latest:
            res.outcome = "no_quotes_timeout"
            res.reason = "no maker quoted within the window"
            return
        res.outcome = "quoted"
        yes_bids = sorted((q.yes_bid for q in latest.values() if q.yes_bid), reverse=True)
        no_quotes = sorted((q for q in latest.values() if q.no_bid), key=lambda q: q.no_bid or 0.0, reverse=True)
        res.best_yes_bid = yes_bids[0] if yes_bids else None
        if no_quotes:
            res.best_quote = no_quotes[0]
            res.best_no_bid = no_quotes[0].no_bid
            res.second_no_bid = no_quotes[1].no_bid if len(no_quotes) > 1 else None
            res.taker_yes_price = pricing.taker_yes_price(res.best_no_bid)
            mapping = self.side_mapping()
            res.taker_yes_contracts = buy_yes_contracts(res.best_quote, req.target_cost, mapping)
            res.combo_payout = pricing.combo_payout_from_contracts(req.target_cost, res.taker_yes_contracts)
        else:
            res.reason = "makers quoted only the NO side"
        for q in latest.values():
            verdict = side_check(q, req.target_cost, self.fee_mult)
            self.side_counts[verdict] = self.side_counts.get(verdict, 0) + 1
            if verdict != INCONCLUSIVE:
                _bump_side_counts(self.db, self.env, verdict)

    async def _cancel(self, rfq_id: str) -> None:
        last = ""
        for attempt in range(3):
            try:
                await self.api.delete_rfq(rfq_id)
                self.db.update(S.rfqs, {"rfq_id": rfq_id}, {"deleted_at": timeutil.now(), "status": "closed"})
                return
            except NotFound:
                self.db.update(S.rfqs, {"rfq_id": rfq_id}, {"status": "closed"})
                return
            except Exception as exc:  # noqa: BLE001 - a failed cancel must never escape
                last = f"{type(exc).__name__}: {exc}"
                await asyncio.sleep(0.5 * (attempt + 1))
        self.db.upsert(
            S.pending_cancels,
            {"rfq_id": rfq_id, "env": self.env, "first_failed_at": timeutil.now(), "attempts": 3, "last_error": last[:300]},
            ["rfq_id"],
            update=["attempts", "last_error"],
        )

    def _persist_open(self, res: ProbeResult) -> None:
        req = res.request
        self.db.upsert(
            S.rfqs,
            {"rfq_id": res.rfq_id, "env": self.env, "run_id": self.run_id, "candidate_id": req.candidate.candidate_id if req.candidate else None,
             "combo_ticker": req.combo_ticker, "signature": req.signature, "purpose": req.purpose, "target_cost": req.target_cost,
             "created_at": res.created_at, "window_s": req.window_s, "status": "open", "hours_to_start": req.hours_to_start,
             "offset_h": req.offset_h, "feed_demand_pair": req.feed_demand},
            ["rfq_id"],
        )

    def _persist(self, res: ProbeResult) -> None:
        req = res.request
        if res.rfq_id is None:
            return
        self.db.update(
            S.rfqs,
            {"rfq_id": res.rfq_id},
            {"outcome": res.outcome, "reason": res.reason, "first_quote_ms": res.first_quote_ms, "n_quotes": len(res.quotes),
             "n_makers": res.n_makers, "best_yes_bid": res.best_yes_bid, "best_no_bid": res.best_no_bid,
             "second_no_bid": res.second_no_bid, "taker_yes_price": res.taker_yes_price,
             "taker_yes_contracts": res.taker_yes_contracts, "combo_payout": res.combo_payout,
             "ws_ok": res.ws_ok, "rest_used": res.rest_used},
        )
        rows = [
            {"quote_id": q.quote_id, "rfq_id": res.rfq_id, "maker_id": q.maker_id, "yes_bid": q.yes_bid, "no_bid": q.no_bid,
             "yes_contracts_offered": q.yes_contracts, "no_contracts_offered": q.no_contracts, "created_ts": q.created_ts,
             "recv_ts": q.recv_ts, "latency_ms": (q.recv_ts - res.created_at) * 1000 if res.created_at else None, "source": q.source}
            for q in res.quotes
        ]
        self.db.insert_many(S.quotes, rows, ignore_conflicts=True)
        self.db.execute("UPDATE combos SET last_rfq_at=:t WHERE ticker=:k", {"t": timeutil.now(), "k": req.combo_ticker})


def _bump_side_counts(db: Database, env: str, verdict: str) -> None:
    key = f"side_check:{env}"
    counts = db.meta_get(key, {}) or {}
    counts[verdict] = int(counts.get(verdict, 0)) + 1
    db.meta_set(key, counts)


async def drain_pending_cancels(api: KalshiAPI, db: Database) -> int:
    done = 0
    for row in db.fetchall("SELECT rfq_id, attempts FROM pending_cancels WHERE env=:e", {"e": api.env}):
        try:
            await api.delete_rfq(row["rfq_id"])
        except NotFound:
            pass
        except KalshiError as exc:
            db.update(S.pending_cancels, {"rfq_id": row["rfq_id"]}, {"attempts": int(row["attempts"] or 0) + 1, "last_error": str(exc)[:300]})
            continue
        db.execute("DELETE FROM pending_cancels WHERE rfq_id=:r", {"r": row["rfq_id"]})
        db.update(S.rfqs, {"rfq_id": row["rfq_id"]}, {"status": "closed", "deleted_at": timeutil.now()})
        done += 1
    return done


async def cleanup_our_open_rfqs(api: KalshiAPI, db: Database) -> int:
    """Cancel open RFQs that THIS TOOL created. Never touches RFQs you opened in the Kalshi app."""
    try:
        open_rfqs = await api.my_rfqs(status="open")
    except KalshiError as exc:
        log.warning("could not list open RFQs: %s", exc)
        return 0
    ours = {r["rfq_id"] for r in db.fetchall("SELECT rfq_id FROM rfqs WHERE env=:e", {"e": api.env})}
    n = 0
    for r in open_rfqs:
        rid = str(r.get("id") or "")
        if rid in ours:
            try:
                await api.delete_rfq(rid)
                n += 1
            except NotFound:
                pass
            except KalshiError as exc:
                log.warning("cleanup cancel failed for %s: %s", rid, exc)
            db.update(S.rfqs, {"rfq_id": rid}, {"status": "closed", "deleted_at": timeutil.now()})
    return n
