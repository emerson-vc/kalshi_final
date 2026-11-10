"""Quote observations, the websocket quote router and the side-mapping self-check.

Quotes are matched to RFQs by ``rfq_id``. A ``quote_created`` can arrive before our ``POST
/communications/rfqs`` returns, so unmatched quote events are buffered for a minute and drained when
the probe registers its RFQ id.

Side mapping. ``yes_bid``/``no_bid`` are the maker's bids, so a taker buys YES at 1 − no_bid. The REST
accept call takes ``accepted_side``, documented only as "the side of the quote". FIX says "BUY accepts
the maker's NO quote" and the batch docs speak of the bid "you take" on the accepted side, which
implies buying YES = ``accepted_side: "no"``. Every quote on a target-cost RFQ lets us test which
``*_contracts_offered_fp`` field holds the buy-YES contract count, and that decides the mapping.
"""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

from combo_edge import pricing, timeutil

QUOTE_SIDE = "quote_side"  # offered/accepted fields named by the maker's bid side -> buy YES = accept "no"
TAKER_SIDE = "taker_side"  # named by the taker's resulting side -> buy YES = accept "yes"
INCONCLUSIVE = "inconclusive"


@dataclass
class QuoteObs:
    quote_id: str
    rfq_id: str
    maker_id: str
    yes_bid: float | None
    no_bid: float | None
    yes_contracts: float | None
    no_contracts: float | None
    created_ts: float | None
    recv_ts: float
    source: str  # ws | rest | recorder
    target_cost: float | None = None
    status: str | None = None  # REST only: open | accepted | confirmed | executed | cancelled

    @property
    def live(self) -> bool:
        """False for a quote the exchange cancelled (a maker's replaced quote shows up this way over REST)."""
        return (self.status or "open") != "cancelled"

    @property
    def order_ts(self) -> float:
        """When the maker sent it. REST rows all share one local receive time, so order by created_ts."""
        return self.created_ts if self.created_ts is not None else self.recv_ts

    @classmethod
    def from_ws(cls, msg: dict[str, Any], recv_ts: float) -> "QuoteObs":
        return cls(
            quote_id=str(msg.get("quote_id") or ""),
            rfq_id=str(msg.get("rfq_id") or ""),
            maker_id=str(msg.get("quote_creator_id") or ""),
            yes_bid=pricing.parse_dollars(msg.get("yes_bid_dollars")),
            no_bid=pricing.parse_dollars(msg.get("no_bid_dollars")),
            yes_contracts=pricing.parse_dollars(msg.get("yes_contracts_offered_fp")),
            no_contracts=pricing.parse_dollars(msg.get("no_contracts_offered_fp")),
            created_ts=timeutil.parse_ts(msg.get("created_ts")),
            recv_ts=recv_ts,
            source="ws",
            target_cost=pricing.parse_dollars(msg.get("rfq_target_cost_dollars")),
        )

    @classmethod
    def from_rest(cls, q: dict[str, Any], recv_ts: float) -> "QuoteObs":
        return cls(
            quote_id=str(q.get("id") or ""),
            rfq_id=str(q.get("rfq_id") or ""),
            maker_id=str(q.get("creator_id") or ""),
            yes_bid=pricing.parse_dollars(q.get("yes_bid_dollars")),
            no_bid=pricing.parse_dollars(q.get("no_bid_dollars")),
            yes_contracts=pricing.parse_dollars(q.get("yes_contracts_fp")),
            no_contracts=pricing.parse_dollars(q.get("no_contracts_fp")),
            created_ts=timeutil.parse_ts(q.get("created_ts")),
            recv_ts=recv_ts,
            source="rest",
            target_cost=pricing.parse_dollars(q.get("rfq_target_cost_dollars")),
            status=str(q.get("status") or "") or None,
        )

    @property
    def taker_yes_price(self) -> float | None:
        return pricing.taker_yes_price(self.no_bid)


def _close(a: float | None, b: float) -> bool:
    return a is not None and abs(a - b) <= max(1.0, 0.02 * b)


def side_check(q: QuoteObs, target: float, multiplier: float = 1.0) -> str:
    """Which offered field holds the buy-YES contract count for a fee-inclusive target-cost RFQ."""
    if q.no_bid is None or q.no_bid <= 0 or q.yes_bid is None or q.yes_bid <= 0:
        return INCONCLUSIVE
    buy_yes = pricing.contracts_for_target(target, 1.0 - q.no_bid, multiplier)
    buy_no = pricing.contracts_for_target(target, 1.0 - q.yes_bid, multiplier)
    if abs(buy_yes - buy_no) <= max(1.0, 0.02 * max(buy_yes, buy_no)):
        return INCONCLUSIVE  # prices too close to tell the fields apart
    if _close(q.no_contracts, buy_yes) and _close(q.yes_contracts, buy_no):
        return QUOTE_SIDE
    if _close(q.yes_contracts, buy_yes) and _close(q.no_contracts, buy_no):
        return TAKER_SIDE
    return INCONCLUSIVE


def buy_yes_contracts(q: QuoteObs, target: float, mapping: str | None) -> float:
    """Contracts we'd get buying YES: from the quote if the mapping is known, else computed."""
    if mapping == QUOTE_SIDE and q.no_contracts:
        return q.no_contracts
    if mapping == TAKER_SIDE and q.yes_contracts:
        return q.yes_contracts
    price = q.taker_yes_price
    return pricing.contracts_for_target(target, price) if price else 0.0


def accepted_side_for_buy_yes(mapping: str | None) -> str | None:
    return {"quote_side": "no", "taker_side": "yes"}.get(mapping or "")


def side_counts(db: Any, env: str) -> dict[str, int]:
    return {k: int(v) for k, v in (db.meta_get(f"side_check:{env}", {}) or {}).items()}


def current_side_mapping(db: Any, env: str, min_quotes: int = 20) -> str | None:
    """The verified mapping, or None. Verified = one verdict has >= min_quotes and the other <= 5%."""
    counts = side_counts(db, env)
    qs, ts = counts.get(QUOTE_SIDE, 0), counts.get(TAKER_SIDE, 0)
    total = qs + ts
    if total == 0:
        return None
    if qs >= min_quotes and ts <= 0.05 * total:
        return QUOTE_SIDE
    if ts >= min_quotes and qs <= 0.05 * total:
        return TAKER_SIDE
    return None


@dataclass
class RfqWatch:
    rfq_id: str
    created_ts: float
    quotes: list[QuoteObs] = field(default_factory=list)
    accepted: list[dict[str, Any]] = field(default_factory=list)
    executed: list[dict[str, Any]] = field(default_factory=list)
    deleted: bool = False
    changed: asyncio.Event = field(default_factory=asyncio.Event)

    def add(self, q: QuoteObs) -> None:
        self.quotes.append(q)
        self.changed.set()

    def add_rest(self, q: QuoteObs) -> bool:
        """Merge a REST row. Updates the status of a quote already seen on the websocket; True if new."""
        for seen in self.quotes:
            if seen.quote_id and seen.quote_id == q.quote_id:
                if q.status:
                    seen.status = q.status
                if seen.created_ts is None:
                    seen.created_ts = q.created_ts
                return False
        self.add(q)
        return True

    def latest_by_maker(self) -> dict[str, QuoteObs]:
        """Each maker's current quote. A maker's new quote replaces its previous one, and Kalshi marks the old
        one cancelled. Order by when the maker sent it, never by when we received it: GET /communications/quotes
        returns every quote best price first, cancelled ones included, all with the same receive time."""
        out: dict[str, QuoteObs] = {}
        for q in sorted(self.quotes, key=lambda x: (x.order_ts, x.recv_ts)):
            out[q.maker_id or q.quote_id] = q
        return {k: q for k, q in out.items() if q.live}

    def first_quote_ms(self) -> float | None:
        if not self.quotes:
            return None
        return max(0.0, (min(q.recv_ts for q in self.quotes) - self.created_ts) * 1000.0)


class QuoteRouter:
    """Routes communications-channel messages to the RfqWatch for our RFQs."""

    def __init__(self, buffer_ttl_s: float = 60.0):
        self.watches: dict[str, RfqWatch] = {}
        self._buffer: OrderedDict[str, list[tuple[str, dict[str, Any], float]]] = OrderedDict()
        self.ttl = buffer_ttl_s
        self.stats = {"quotes": 0, "buffered": 0, "unmatched": 0}

    def register(self, rfq_id: str, created_ts: float) -> RfqWatch:
        w = RfqWatch(rfq_id, created_ts)
        self.watches[rfq_id] = w
        for kind, msg, ts in self._buffer.pop(rfq_id, []):
            self._apply(w, kind, msg, ts)
        return w

    def unregister(self, rfq_id: str) -> None:
        self.watches.pop(rfq_id, None)

    def handle(self, message: dict[str, Any]) -> None:
        kind = message.get("type")
        if kind not in {"quote_created", "quote_accepted", "quote_executed", "rfq_deleted"}:
            return
        msg = message.get("msg") or {}
        rfq_id = str(msg.get("rfq_id") or (msg.get("id") if kind == "rfq_deleted" else "") or "")
        if not rfq_id:
            return
        now = time.time()
        w = self.watches.get(rfq_id)
        if w is None:
            self._buffer.setdefault(rfq_id, []).append((kind, msg, now))
            self.stats["buffered"] += 1
            self._expire(now)
            return
        self._apply(w, kind, msg, now)

    def _apply(self, w: RfqWatch, kind: str, msg: dict[str, Any], ts: float) -> None:
        if kind == "quote_created":
            w.add(QuoteObs.from_ws(msg, ts))
            self.stats["quotes"] += 1
        elif kind == "quote_accepted":
            w.accepted.append({**msg, "_recv_ts": ts})
            w.changed.set()
        elif kind == "quote_executed":
            w.executed.append({**msg, "_recv_ts": ts})
            w.changed.set()
        elif kind == "rfq_deleted":
            w.deleted = True
            w.changed.set()

    def _expire(self, now: float) -> None:
        while self._buffer:
            rfq_id, items = next(iter(self._buffer.items()))
            if items and now - items[-1][2] < self.ttl:
                break
            self._buffer.popitem(last=False)
            self.stats["unmatched"] += len(items)
