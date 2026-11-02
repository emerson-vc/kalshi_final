"""24/7 RFQ-feed recorder (runs under launchd KeepAlive). Read-only: it never sends RFQs, never creates
combos, never trades.

Kalshi removed the lookup-history endpoints and the ``multivariate_lookup`` channel (Aug 6 2026). The
public ``rfq_created`` stream on the ``communications`` channel is what's left: each message carries the
combo ticker and ``mve_selected_legs``. For NFL same-game RFQs the recorder:
- stores full rows for combos with <= ``max_legs`` legs and adds them to the combo index (free to price),
- increments per-day leg and leg-pair demand counters for every NFL same-game RFQ (any size),
- backs up any ``quote_created`` for RFQs this tool created (a second capture path for probes).
Writes are batched; feed rows older than 60 days are pruned.
"""

from __future__ import annotations

import asyncio
import logging
import signal
from collections import Counter
from itertools import combinations
from typing import Any

from combo_edge import timeutil
from combo_edge.candidates import signature_of
from combo_edge.combos import ComboIndex
from combo_edge.quotes import QuoteObs
from combo_edge.storage import Database
from combo_edge.storage import schema as S

log = logging.getLogger(__name__)
RETENTION_DAYS = 60


def nfl_same_game(legs: list[dict[str, Any]]) -> str | None:
    codes = set()
    for l in legs:
        et = str(l.get("event_ticker") or "")
        if not et.startswith("KXNFL") or "-" not in et:
            return None
        codes.add(et.split("-")[1])
    return codes.pop() if len(codes) == 1 else None


class Recorder:
    def __init__(self, db: Database, env: str, max_legs: int = 2):
        self.db = db
        self.env = env
        self.max_legs = max_legs
        self.index = ComboIndex(db)
        self.pairs: Counter[tuple[str, str, str, str, str]] = Counter()
        self.leg_counts: Counter[tuple[str, str, str]] = Counter()
        self.rows: list[dict[str, Any]] = []
        self.quotes: list[dict[str, Any]] = []
        self.stats = {"messages": 0, "rfqs": 0, "nfl_sg": 0, "stored": 0, "quotes_backup": 0, "rows_today": 0, "day": timeutil.local_day(timeutil.now())}
        self.our_rfqs: set[str] = set()
        self._last_prune = 0.0

    def handle(self, message: dict[str, Any]) -> None:
        self.stats["messages"] += 1
        kind = message.get("type")
        msg = message.get("msg") or {}
        if kind == "rfq_created":
            self.stats["rfqs"] += 1
            self._rfq(msg)
        elif kind == "quote_created":
            rid = str(msg.get("rfq_id") or "")
            if rid:
                q = QuoteObs.from_ws(msg, timeutil.now())
                self.quotes.append({"quote_id": q.quote_id, "rfq_id": rid, "maker_id": q.maker_id, "yes_bid": q.yes_bid, "no_bid": q.no_bid,
                                    "yes_contracts_offered": q.yes_contracts, "no_contracts_offered": q.no_contracts,
                                    "created_ts": q.created_ts, "recv_ts": q.recv_ts, "source": "recorder"})

    def _rfq(self, msg: dict[str, Any]) -> None:
        legs = msg.get("mve_selected_legs") or []
        if len(legs) < 2:
            return
        code = nfl_same_game(legs)
        if code is None:
            return
        self.stats["nfl_sg"] += 1
        day = timeutil.local_day(timeutil.now())
        sided = sorted((str(l["market_ticker"]), str(l.get("side") or "yes")) for l in legs)
        for t, s in sided:
            self.leg_counts[(day, t, s)] += 1
        for (a, sa), (b, sb) in combinations(sided, 2):
            self.pairs[(day, a, sa, b, sb)] += 1
        if len(legs) <= self.max_legs:
            ticker = str(msg.get("market_ticker") or "")
            self.rows.append({"rfq_id": msg.get("id"), "env": self.env, "ts": timeutil.parse_ts(msg.get("created_ts")) or timeutil.now(),
                              "combo_ticker": ticker, "game_code": code, "signature": signature_of(sided), "legs": [list(x) for x in sided],
                              "n_legs": len(legs), "creator_hash": msg.get("creator_id"),
                              "contracts": _f(msg.get("contracts_fp")), "target_cost": _f(msg.get("target_cost_dollars"))})
            if ticker:
                self.index.add(ticker, sided, "feed", collection=msg.get("mve_collection_ticker"), game_code=code, event_ticker=msg.get("event_ticker"))

    def flush(self) -> None:
        if self.rows:
            n = self.db.insert_many(S.feed_rfqs, self.rows, ignore_conflicts=True)
            self.stats["stored"] += n
            today = timeutil.local_day(timeutil.now())
            if today != self.stats["day"]:
                self.stats["day"], self.stats["rows_today"] = today, 0
            self.stats["rows_today"] += n
            self.rows = []
        if self.pairs:
            for (day, a, sa, b, sb), n in self.pairs.items():
                self.db.execute("INSERT INTO feed_pair_counts(day, leg_a, side_a, leg_b, side_b, n) VALUES(:d,:a,:sa,:b,:sb,:n) "
                                "ON CONFLICT(day, leg_a, side_a, leg_b, side_b) DO UPDATE SET n = n + excluded.n",
                                {"d": day, "a": a, "sa": sa, "b": b, "sb": sb, "n": n})
            self.pairs.clear()
        if self.leg_counts:
            for (day, t, s), n in self.leg_counts.items():
                self.db.execute("INSERT INTO feed_leg_counts(day, leg, side, n) VALUES(:d,:t,:s,:n) "
                                "ON CONFLICT(day, leg, side) DO UPDATE SET n = n + excluded.n", {"d": day, "t": t, "s": s, "n": n})
            self.leg_counts.clear()
        if self.quotes:
            ours = {r["rfq_id"] for r in self.db.fetchall("SELECT rfq_id FROM rfqs WHERE env=:e AND created_at >= :t",
                                                            {"e": self.env, "t": timeutil.now() - 3600})}
            keep = [q for q in self.quotes if q["rfq_id"] in ours]
            self.db.insert_many(S.quotes, keep, ignore_conflicts=True)
            self.stats["quotes_backup"] += len(keep)
            self.quotes = []
        now = timeutil.now()
        if now - self._last_prune > 3600:
            self.db.execute("DELETE FROM feed_rfqs WHERE ts < :t", {"t": now - RETENTION_DAYS * 86400})
            self._last_prune = now


def _f(v: Any) -> float | None:
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


async def run_recorder(env: str, db: Database, stop: asyncio.Event, flush_s: float = 5.0) -> Recorder:
    from combo_edge.context import make_clients

    clients = make_clients(env, db)
    if not clients.authenticated:
        raise RuntimeError(clients.credentials_error or f"no API credentials for {env}; the recorder needs an authenticated websocket")
    rec = Recorder(db, env)
    ws = clients.websocket([{"channels": ["communications"]}], name="recorder")
    ws.add_handler(rec.handle)
    await ws.start()
    try:
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=flush_s)
            except TimeoutError:
                pass
            rec.flush()
            db.meta_set(f"recorder_status:{env}", {**rec.stats, "connected": ws.connected.is_set(), "last_msg_ts": ws.stats.get("last_msg_ts"),
                                                    "ws": {k: ws.stats[k] for k in ("connects", "disconnects", "gaps", "errors")},
                                                    "updated": timeutil.now(), "installed": _installed()})
    finally:
        rec.flush()
        await ws.stop()
        await clients.aclose()
        db.meta_set(f"recorder_status:{env}", {**rec.stats, "connected": False, "updated": timeutil.now(), "installed": _installed()})
    return rec


def _installed() -> bool:
    try:
        from combo_edge.scheduler.launchd import is_installed

        return is_installed("recorder")
    except Exception:  # noqa: BLE001
        return False


async def run_recorder_forever(env: str) -> None:
    from combo_edge.context import default_db

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    log.info("recorder starting on %s", env)
    await run_recorder(env, default_db(), stop)
