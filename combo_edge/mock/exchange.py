"""In-process fake Kalshi exchange (REST + websocket). No network, no money.

Used by the test suite and by ``KALSHI_ENV=mock`` to try the whole dashboard offline. Market data is
real (recorded from prod on 2026-11-02) with times shifted so the games are upcoming.

Synthetic makers price combos roughly as the product of leg prices (the mistake we're hunting), plus
one correlation-aware maker. Accept semantics follow the docs-inferred mapping (``accepted_side`` is
the maker bid you take: buying YES = accepting the maker's NO quote) and the offered-contract fields
are named the same way, so the side self-check has something real to verify against.

Faults can be injected for tests via ``MockExchange.faults``.
"""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import math
import random
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import httpx

from combo_edge import pricing, timeutil

DATA = Path(__file__).resolve().parent / "data"
PREFIX = "/trade-api/v2"
_CLOSE = object()


FIXTURES = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "live"


def _load(name: str) -> Any:
    """Trimmed recorded markets in mock/data; falls back to the full recorded fixtures if one is missing."""
    path = DATA / f"{name}.json.gz"
    if not path.exists():
        path = FIXTURES / f"{name}.json.gz"
    with gzip.open(path, "rt") as f:
        return json.load(f)


def _grid(p: float) -> float:
    """Round to the combo price grid: $0.0001 below 1c and above 99c, $0.001 in between."""
    p = min(0.9999, max(0.0001, p))
    step = 0.0001 if (p < 0.01 or p > 0.99) else 0.001
    return round(round(p / step) * step, 4)


@dataclass
class Maker:
    name: str
    margin: float
    noise: float
    latency: tuple[float, float]
    decline: float
    corr_aware: float = 0.0  # 0 = prices as if independent; >0 boosts correlated same-team pairs


def default_makers() -> list[Maker]:
    return [
        Maker("maker-a", 0.06, 0.05, (0.08, 0.6), 0.10),
        Maker("maker-b", 0.09, 0.08, (0.2, 1.6), 0.25),
        Maker("maker-c", 0.12, 0.10, (0.5, 3.0), 0.45, corr_aware=0.6),
    ]


@dataclass
class Faults:
    drop_ws_after_quotes: int | None = None
    create_rfq_status: int | None = None
    reject_signatures: set[str] = field(default_factory=set)
    no_confirm: bool = False
    rest_quotes_hidden: bool = False
    fail_delete_n: int = 0
    rate_limit_every: int | None = None
    no_makers: bool = False
    wrong_accept_side_contracts: bool = False


class MockWSConn:
    def __init__(self, ex: "MockExchange"):
        self.ex = ex
        self.q: asyncio.Queue[Any] = asyncio.Queue()
        self.subs: dict[int, dict[str, Any]] = {}
        self.seq: dict[int, int] = {}
        self.closed = False
        self._next_sid = 1

    async def send(self, message: str) -> None:
        cmd = json.loads(message)
        if cmd.get("cmd") == "subscribe":
            params = cmd.get("params") or {}
            for ch in params.get("channels") or []:
                sid = self._next_sid
                self._next_sid += 1
                self.subs[sid] = {"channel": ch, **params}
                self.seq[sid] = 0
                self.q.put_nowait(json.dumps({"type": "subscribed", "id": cmd.get("id"), "msg": {"channel": ch, "sid": sid}}))

    async def recv(self) -> str:
        item = await self.q.get()
        if item is _CLOSE:
            self.closed = True
            raise ConnectionError("mock websocket closed")
        return item

    async def close(self) -> None:
        if not self.closed:
            self.closed = True
            self.q.put_nowait(_CLOSE)
        if self in self.ex.conns:
            self.ex.conns.remove(self)

    def drop(self) -> None:
        self.q.put_nowait(_CLOSE)
        if self in self.ex.conns:
            self.ex.conns.remove(self)

    def push(self, channel: str, mtype: str, msg: dict[str, Any], mine: bool) -> None:
        for sid, sub in self.subs.items():
            if sub["channel"] != channel:
                continue
            if channel == "communications" and mtype.startswith("rfq_") and sub.get("user_filter") == "self" and not mine:
                continue
            self.seq[sid] += 1
            self.q.put_nowait(json.dumps({"type": mtype, "sid": sid, "seq": self.seq[sid], "msg": msg}))


class MockExchange:
    def __init__(self, seed: int = 7, first_kickoff_in_h: float = 30.0, makers: list[Maker] | None = None):
        self.rng = random.Random(seed)
        self.makers = makers or default_makers()
        self.faults = Faults()
        self.comm_id = "mock-me"
        self.conns: list[MockWSConn] = []
        self.combos: dict[str, dict[str, Any]] = {}
        self.combo_by_ticker: dict[str, dict[str, Any]] = {}
        self.rfqs: dict[str, dict[str, Any]] = {}
        self.open_rfq_by_ticker: dict[str, str] = {}
        self.quotes: dict[str, dict[str, Any]] = {}
        self.fills: list[dict[str, Any]] = []
        self.positions: dict[str, float] = {}
        self.accepts = 0
        self.tier = "basic"
        self.n_requests = 0
        self.n_deletes_failed = 0
        self.quotes_sent = 0
        self.tasks: set[asyncio.Task[Any]] = set()
        self._feed_task: asyncio.Task[Any] | None = None
        self._load(first_kickoff_in_h)

    # ------------------------------------------------------------------ data
    def _load(self, first_kickoff_in_h: float) -> None:
        ms = _load("milestones")["milestones"]
        chisea = next(m for m in ms if m["details"]["main_game_event_ticker"].endswith("26NOV02CHISEA"))
        self.shift = time.time() + first_kickoff_in_h * 3600 - (timeutil.parse_ts(chisea["start_date"]) or 0)
        self.milestones = []
        for m in ms:
            m = dict(m)
            m["start_date"] = self._shift_iso(m["start_date"])
            self.milestones.append(m)
        self.collections = _load("collections")["multivariate_contracts"]
        self.events: dict[str, dict[str, Any]] = {}
        self.markets: dict[str, dict[str, Any]] = {}
        for code in ("26NOV02CHISEA", "26NOV05JACBAL"):
            for et, body in _load(f"events_{code}").items():
                ev = dict(body.get("event") or {})
                mk = body.get("markets") or ev.get("markets") or []
                self.events[et] = ev
                for m in mk:
                    m = dict(m)
                    for k in ("open_time", "close_time", "expected_expiration_time", "latest_expiration_time", "created_time", "occurrence_datetime"):
                        if m.get(k):
                            m[k] = self._shift_iso(m[k])
                    if m.get("status") == "active":
                        m["status"] = "active"
                    self.markets[m["ticker"]] = m
        self.series = _load("series")
        self.endpoint_costs = _load("endpoint_costs")

    def _shift_iso(self, s: str) -> str:
        ts = timeutil.parse_ts(s)
        return timeutil.iso(ts + self.shift) if ts is not None else s

    # ------------------------------------------------------------------ websocket
    async def ws_connect(self, url: str, headers: dict[str, str]) -> MockWSConn:
        if "KALSHI-ACCESS-SIGNATURE" not in headers:
            raise ConnectionError("401: websocket requires authentication")
        conn = MockWSConn(self)
        self.conns.append(conn)
        return conn

    def _broadcast(self, mtype: str, msg: dict[str, Any], mine: bool = True) -> None:
        for c in list(self.conns):
            c.push("communications", mtype, msg, mine)

    def drop_all_ws(self) -> None:
        for c in list(self.conns):
            c.drop()

    # ------------------------------------------------------------------ http
    async def handle_http(self, request: httpx.Request) -> httpx.Response:
        self.n_requests += 1
        if self.faults.rate_limit_every and self.n_requests % self.faults.rate_limit_every == 0:
            return httpx.Response(429, json={"error": "too many requests"})
        path = request.url.path.removeprefix(PREFIX)
        params = {k: v[-1] for k, v in parse_qs(request.url.query.decode() if isinstance(request.url.query, bytes) else str(request.url.query)).items()}
        body = json.loads(request.content) if request.content else {}
        m = request.method
        try:
            return await self._route(m, path, params, body, request)
        except _HTTPError as e:
            return httpx.Response(e.status, json={"error": {"code": e.code, "message": e.message, "details": e.details}})

    async def _route(self, m: str, path: str, p: dict[str, str], body: dict[str, Any], request: httpx.Request) -> httpx.Response:
        parts = [x for x in path.split("/") if x]
        authed = "KALSHI-ACCESS-SIGNATURE" in request.headers

        def need_auth() -> None:
            if not authed:
                raise _HTTPError(401, "unauthorized", "authentication required")

        if m == "GET" and path == "/exchange/status":
            return _ok({"exchange_active": True, "trading_active": True})
        if m == "GET" and path == "/milestones":
            rows = [x for x in self.milestones if not p.get("type") or x.get("type") == p["type"]]
            if p.get("minimum_start_date"):
                lo = timeutil.parse_ts(p["minimum_start_date"]) or 0
                rows = [x for x in rows if (timeutil.parse_ts(x["start_date"]) or 0) >= lo]
            return _ok(_page(rows, p, "milestones"))
        if m == "GET" and path == "/multivariate_event_collections":
            return _ok({"multivariate_contracts": self.collections, "cursor": ""})
        if m == "GET" and len(parts) == 2 and parts[0] == "multivariate_event_collections":
            c = next((c for c in self.collections if c["collection_ticker"] == parts[1]), None)
            if not c:
                raise _HTTPError(404, "not_found", "collection not found")
            return _ok({"multivariate_contract": c})
        if m == "POST" and len(parts) == 2 and parts[0] == "multivariate_event_collections":
            need_auth()
            return self._create_combo(parts[1], body)
        if m == "GET" and len(parts) == 2 and parts[0] == "events" and parts[1] != "multivariate":
            ev = self.events.get(parts[1])
            if not ev:
                raise _HTTPError(404, "not_found", "event not found")
            return _ok({"event": ev, "markets": [mk for mk in self.markets.values() if mk["event_ticker"] == parts[1]]})
        if m == "GET" and path == "/markets":
            return _ok(self._markets(p))
        if m == "GET" and path == "/markets/candlesticks":
            return _ok(self._candles(p))
        if m == "GET" and path == "/markets/trades":
            return _ok({"trades": [], "cursor": ""})
        if m == "GET" and len(parts) == 2 and parts[0] == "markets":
            mk = self.markets.get(parts[1]) or (self.combo_by_ticker.get(parts[1]) or {}).get("market")
            if not mk:
                raise _HTTPError(404, "not_found", "market not found")
            return _ok({"market": mk})
        if m == "GET" and len(parts) == 2 and parts[0] == "series":
            return _ok({"series": self.series.get(parts[1], {}).get("series", {"ticker": parts[1], "fee_type": "quadratic", "fee_multiplier": 1})})
        if m == "GET" and path == "/account/endpoint_costs":
            return _ok(self.endpoint_costs)
        if m == "GET" and path == "/account/limits":
            need_auth()
            cap = {"basic": (200, 600, 100, 100), "advanced": (300, 900, 300, 900)}[self.tier]
            return _ok({"usage_tier": self.tier, "read": {"refill_rate": cap[0], "bucket_capacity": cap[1]},
                        "write": {"refill_rate": cap[2], "bucket_capacity": cap[3]}, "grants": []})
        if m == "POST" and path == "/account/api_usage_level/upgrade":
            need_auth()
            if self.accepts == 0:
                raise _HTTPError(403, "forbidden", "No API-created order was found in the user's latest 100 Predictions orders")
            self.tier = "advanced"
            return httpx.Response(201)
        if m == "GET" and path == "/account/api_usage_level/volume_progress":
            need_auth()
            return _ok({"progress": []})
        if m == "GET" and path == "/communications/id":
            need_auth()
            return _ok({"communications_id": self.comm_id})
        if path == "/communications/rfqs" and m == "POST":
            need_auth()
            return await self._create_rfq(body)
        if path == "/communications/rfqs" and m == "GET":
            need_auth()
            rows = [r for r in self.rfqs.values() if (not p.get("status") or r["status"] == p["status"])]
            return _ok({"rfqs": [self._rfq_out(r) for r in rows], "cursor": ""})
        if len(parts) == 3 and parts[:2] == ["communications", "rfqs"]:
            need_auth()
            r = self.rfqs.get(parts[2])
            if not r:
                raise _HTTPError(404, "not_found", "rfq not found")
            if m == "GET":
                return _ok({"rfq": self._rfq_out(r)})
            if m == "DELETE":
                if self.n_deletes_failed < self.faults.fail_delete_n:
                    self.n_deletes_failed += 1
                    raise _HTTPError(500, "internal", "mock delete failure")
                self._close_rfq(r, "cancelled")
                return httpx.Response(204)
        if len(parts) == 5 and parts[:2] == ["communications", "rfqs"] and parts[3] == "quotes" and m == "GET":
            need_auth()
            q = self.quotes.get(parts[4])
            if not q or q["rfq_id"] != parts[2]:
                raise _HTTPError(404, "not_found", "quote not found")
            return _ok({"quote": self._quote_rest(q)})
        if len(parts) == 6 and parts[:2] == ["communications", "rfqs"] and parts[3] == "quotes" and parts[5] == "accept" and m == "PUT":
            need_auth()
            return self._accept(parts[2], parts[4], body)
        if m == "GET" and path == "/communications/quotes":
            need_auth()
            if self.faults.rest_quotes_hidden:
                return _ok({"quotes": [], "cursor": ""})
            # Kalshi: every status, best price on the RFQ's main side first (NO for target-cost RFQs)
            mine = sorted((q for q in self.quotes.values() if q["rfq_id"] == p.get("rfq_id")), key=lambda q: (-q["no_bid"], q["created_ts"]))
            rows = [self._quote_rest(q) for q in mine]
            return _ok({"quotes": rows, "cursor": ""})
        if m == "GET" and path == "/portfolio/positions":
            need_auth()
            rows = [{"ticker": t, "position_fp": f"{v:.2f}", "market_exposure_dollars": f"{abs(v) * 0.05:.4f}",
                     "realized_pnl_dollars": "0.0000", "fees_paid_dollars": "0.0000", "total_traded_dollars": "0.0000"}
                    for t, v in self.positions.items() if v]
            return _ok({"market_positions": rows, "event_positions": [], "cursor": ""})
        if m == "GET" and path == "/portfolio/fills":
            need_auth()
            rows = [f for f in self.fills if not p.get("ticker") or f["ticker"] == p["ticker"]]
            return _ok({"fills": rows, "cursor": ""})
        if m == "GET" and path == "/portfolio/settlements":
            need_auth()
            return _ok({"settlements": [], "cursor": ""})
        if m == "GET" and path == "/portfolio/balance":
            need_auth()
            return _ok({"balance": 200000, "balance_dollars": "2000.0000", "portfolio_value": 0})
        raise _HTTPError(404, "not_found", f"mock has no route {m} {path}")

    # ------------------------------------------------------------------ markets
    def _markets(self, p: dict[str, str]) -> dict[str, Any]:
        rows = list(self.markets.values())
        if p.get("series_ticker"):
            s = p["series_ticker"]
            rows = [r for r in rows if r["event_ticker"].split("-")[0] == s]
        if p.get("event_ticker"):
            rows = [r for r in rows if r["event_ticker"] == p["event_ticker"]]
        if p.get("tickers"):
            want = set(p["tickers"].split(","))
            rows = [r for r in rows if r["ticker"] in want] + [c["market"] for t, c in self.combo_by_ticker.items() if t in want]
        if p.get("status") == "open":
            rows = [r for r in rows if r.get("status") in ("active", "open")]
        return _page(rows, p, "markets")

    def _candles(self, p: dict[str, str]) -> dict[str, Any]:
        out = []
        for t in (p.get("market_tickers") or "").split(","):
            mk = self.markets.get(t)
            if not mk:
                continue
            yb = pricing.parse_dollars(mk.get("yes_bid_dollars")) or 0.0
            ya = pricing.parse_dollars(mk.get("yes_ask_dollars")) or 0.0
            drift = self.rng.uniform(-0.03, 0.03)
            out.append({"market_ticker": t, "candlesticks": [{"end_period_ts": int(time.time()),
                        "yes_bid": {"open_dollars": f"{max(0.0, yb - drift):.4f}"}, "yes_ask": {"open_dollars": f"{min(1.0, ya - drift):.4f}"},
                        "price": {"open_dollars": f"{(yb + ya) / 2:.4f}"}}]})
        return {"markets": out}

    def leg_p(self, ticker: str, side: str) -> float | None:
        mk = self.markets.get(ticker)
        if not mk:
            return None
        yb = pricing.parse_dollars(mk.get("yes_bid_dollars")) or 0.0
        ya = pricing.parse_dollars(mk.get("yes_ask_dollars")) or 0.0
        p = (yb + ya) / 2 if (yb > 0 and 0 < ya < 1) else (ya if 0 < ya < 1 else None)
        if p is None:
            return None
        return p if side == "yes" else 1 - p

    # ------------------------------------------------------------------ combos
    def _create_combo(self, collection: str, body: dict[str, Any]) -> httpx.Response:
        legs = body.get("selected_markets") or []
        if len(legs) < 2:
            raise _HTTPError(400, "invalid_parameters", "at least 2 markets required")
        sig = "|".join(sorted(f"{l['market_ticker']}:{l['side']}" for l in legs))
        if sig in self.faults.reject_signatures:
            raise _HTTPError(400, "invalid_parameters", "invalid_market_combination: legs force each other", [l["market_ticker"] for l in legs])
        events = [l["event_ticker"] for l in legs]
        coll = next((c for c in self.collections if c["collection_ticker"] == collection), None)
        if coll is None:
            raise _HTTPError(404, "not_found", "collection not found")
        size_max = {e["ticker"]: e.get("size_max") for e in coll["associated_events"]}
        for e in set(events):
            if events.count(e) > 1 and size_max.get(e) == 1:
                raise _HTTPError(400, "invalid_parameters", f"conflicting_leg_outcomes: {e}", [l["market_ticker"] for l in legs if l["event_ticker"] == e])
        for l in legs:
            if l["market_ticker"] not in self.markets:
                raise _HTTPError(400, "invalid_parameters", f"unknown market {l['market_ticker']}")
        kinds = {l["event_ticker"].split("-")[0] for l in legs}
        if {"KXNFLGAME", "KXNFLSPREAD"} <= kinds:
            teams = {l["market_ticker"].split("-")[-1].rstrip("0123456789") for l in legs}
            if len(teams) == 1:
                raise _HTTPError(400, "invalid_parameters", "invalid_market_combination: spread implies moneyline")
        if sig in self.combos:
            c = self.combos[sig]
            return _ok({"event_ticker": c["event_ticker"], "market_ticker": c["ticker"], "market": c["market"]})
        h = hashlib.sha1(sig.encode()).hexdigest().upper()
        ev = f"KXMVECROSSCATEGORY-S2026MOCK{h[:12]}"
        ticker = f"{ev}-{h[12:21]}"
        now_iso = timeutil.iso(time.time())
        market = {"ticker": ticker, "event_ticker": ev, "mve_collection_ticker": collection, "status": "active",
                  "created_time": now_iso, "mve_selected_legs": legs, "yes_bid_dollars": "0.0000", "yes_ask_dollars": "1.0000",
                  "price_level_structure": "center_deci_edge_centi_cent"}
        c = {"ticker": ticker, "event_ticker": ev, "legs": legs, "market": market, "signature": sig}
        self.combos[sig] = c
        self.combo_by_ticker[ticker] = c
        return _ok({"event_ticker": ev, "market_ticker": ticker, "market": market})

    def preexisting_combo(self, legs: list[dict[str, str]], age_s: float = 3600.0) -> str:
        """Test helper: register a combo 'someone else' created earlier."""
        sig = "|".join(sorted(f"{l['market_ticker']}:{l['side']}" for l in legs))
        resp = self._create_combo("KXMVECROSSCATEGORY-R", {"selected_markets": legs})
        c = self.combos[sig]
        c["market"]["created_time"] = timeutil.iso(time.time() - age_s)
        return json.loads(resp.content)["market_ticker"]

    # ------------------------------------------------------------------ RFQs and quotes
    async def _create_rfq(self, body: dict[str, Any]) -> httpx.Response:
        if self.faults.create_rfq_status:
            raise _HTTPError(self.faults.create_rfq_status, "mock_fault", "forced create failure")
        ticker = body.get("market_ticker")
        if body.get("rest_remainder") is not False:
            raise _HTTPError(400, "invalid_parameters", "mock requires rest_remainder=false")
        if ticker not in self.combo_by_ticker:
            raise _HTTPError(400, "invalid_parameters", "unknown combo ticker")
        if ticker in self.open_rfq_by_ticker:
            raise _HTTPError(409, "conflict", "An open RFQ already exists on this market ticker")
        if sum(1 for r in self.rfqs.values() if r["status"] == "open") >= 100:
            raise _HTTPError(400, "invalid_parameters", "maximum of 100 open RFQs")
        rid = str(uuid.uuid4())
        target = float(body.get("target_cost_dollars") or 0)
        r = {"id": rid, "market_ticker": ticker, "target": target, "status": "open", "created_ts": time.time()}
        self.rfqs[rid] = r
        self.open_rfq_by_ticker[ticker] = rid
        self._broadcast("rfq_created", {"id": rid, "creator_id": self.comm_id, "market_ticker": ticker, "target_cost_dollars": f"{target:.2f}",
                                         "created_ts": timeutil.iso(r["created_ts"]), "mve_collection_ticker": "KXMVECROSSCATEGORY-R",
                                         "mve_selected_legs": self.combo_by_ticker[ticker]["legs"]})
        if not self.faults.no_makers:
            for mk in self.makers:
                self._spawn(self._maker_quote(rid, mk))
        return httpx.Response(201, json={"id": rid})

    def _spawn(self, coro: Any) -> None:
        t = asyncio.get_event_loop().create_task(coro)
        self.tasks.add(t)
        t.add_done_callback(self.tasks.discard)

    def _leg_ask(self, ticker: str, side: str) -> float | None:
        mk = self.markets.get(ticker)
        if not mk:
            return None
        v = pricing.parse_dollars(mk.get("yes_ask_dollars" if side == "yes" else "no_ask_dollars"))
        return pricing.fee_inclusive_price(v) if v is not None and 0 < v < 1 else None

    def _fair_yes(self, legs: list[dict[str, str]], mk: Maker) -> float:
        # Naive makers: price the combo off the product of fee-inclusive leg asks (your observation).
        ps = [self._leg_ask(l["market_ticker"], l["side"]) or self.leg_p(l["market_ticker"], l["side"]) for l in legs]
        ps = [p if p is not None else 0.35 for p in ps]
        q = pricing.product(ps)
        if mk.corr_aware and len(legs) == 2:
            same_side = legs[0]["side"] == legs[1]["side"]
            q *= 1.0 + (mk.corr_aware if same_side else -mk.corr_aware / 2) * (1 - max(ps))
        return q

    async def _maker_quote(self, rid: str, mk: Maker) -> None:
        if self.rng.random() < mk.decline:
            return
        await asyncio.sleep(self.rng.uniform(*mk.latency))
        r = self.rfqs.get(rid)
        if not r or r["status"] != "open":
            return
        legs = self.combo_by_ticker[r["market_ticker"]]["legs"]
        fair = self._fair_yes(legs, mk) * math.exp(self.rng.gauss(0, mk.noise))
        ask_yes = _grid(min(0.985, max(0.002, fair * (1 + mk.margin))))
        bid_yes = _grid(max(0.0001, fair * (1 - mk.margin)))
        if bid_yes >= ask_yes:
            bid_yes = _grid(ask_yes * 0.9)
        no_bid = round(1 - ask_yes, 4)
        yes_bid = bid_yes
        target = r["target"]
        qid = str(uuid.uuid4())
        q = {"id": qid, "rfq_id": rid, "maker": mk.name, "market_ticker": r["market_ticker"], "yes_bid": yes_bid, "no_bid": no_bid,
             # contracts named by the maker-bid side taken: taking the NO bid buys YES at 1 - no_bid
             "no_contracts": pricing.contracts_for_target(target, 1 - no_bid), "yes_contracts": pricing.contracts_for_target(target, 1 - yes_bid),
             "status": "open", "created_ts": time.time(), "target": target}
        for prev in self.quotes.values():  # like Kalshi: a maker's new quote cancels its previous one
            if prev["rfq_id"] == rid and prev["maker"] == mk.name and prev["status"] == "open":
                prev["status"] = "cancelled"
        self.quotes[qid] = q
        self.quotes_sent += 1
        self._broadcast("quote_created", self._quote_ws(q))
        if self.faults.drop_ws_after_quotes is not None and self.quotes_sent >= self.faults.drop_ws_after_quotes:
            self.faults.drop_ws_after_quotes = None
            self.drop_all_ws()

    def _quote_ws(self, q: dict[str, Any]) -> dict[str, Any]:
        return {"quote_id": q["id"], "rfq_id": q["rfq_id"], "quote_creator_id": q["maker"], "rfq_creator_id": self.comm_id,
                "market_ticker": q["market_ticker"], "yes_bid_dollars": f"{q['yes_bid']:.4f}", "no_bid_dollars": f"{q['no_bid']:.4f}",
                "yes_contracts_offered_fp": f"{q['yes_contracts']:.2f}", "no_contracts_offered_fp": f"{q['no_contracts']:.2f}",
                "rfq_target_cost_dollars": f"{q['target']:.2f}", "created_ts": timeutil.iso(q["created_ts"])}

    def _quote_rest(self, q: dict[str, Any]) -> dict[str, Any]:
        return {"id": q["id"], "rfq_id": q["rfq_id"], "creator_id": q["maker"], "market_ticker": q["market_ticker"],
                "yes_bid_dollars": f"{q['yes_bid']:.4f}", "no_bid_dollars": f"{q['no_bid']:.4f}", "status": q["status"],
                "yes_contracts_fp": f"{q['yes_contracts']:.2f}", "no_contracts_fp": f"{q['no_contracts']:.2f}",
                "created_ts": timeutil.iso(q["created_ts"]), "rfq_target_cost_dollars": f"{q['target']:.2f}"}

    def _rfq_out(self, r: dict[str, Any]) -> dict[str, Any]:
        return {"id": r["id"], "creator_id": self.comm_id, "market_ticker": r["market_ticker"], "status": r["status"],
                "target_cost_dollars": f"{r['target']:.2f}", "created_ts": timeutil.iso(r["created_ts"]), "contracts_fp": "0.00"}

    def _close_rfq(self, r: dict[str, Any], reason: str) -> None:
        if r["status"] != "open":
            return
        r["status"] = "closed"
        self.open_rfq_by_ticker.pop(r["market_ticker"], None)
        for q in self.quotes.values():
            if q["rfq_id"] == r["id"] and q["status"] in ("open", "accepted"):  # deleting before the maker confirms kills the accept
                q["status"] = "cancelled"
        self._broadcast("rfq_deleted", {"id": r["id"], "creator_id": self.comm_id, "market_ticker": r["market_ticker"], "deleted_ts": timeutil.iso(time.time())})

    def _accept(self, rid: str, qid: str, body: dict[str, Any]) -> httpx.Response:
        r = self.rfqs.get(rid)
        q = self.quotes.get(qid)
        if not r or not q or q["rfq_id"] != rid:
            raise _HTTPError(404, "NOT_FOUND", "quote not found")
        if r["status"] != "open" or q["status"] != "open":
            raise _HTTPError(400, "RFQ_CLOSED", "RFQ was deleted, expired, or already executed")
        side = body.get("accepted_side")
        if side not in ("yes", "no"):
            raise _HTTPError(400, "invalid_parameters", "accepted_side must be yes or no")
        q["status"] = "accepted"
        contracts = q["no_contracts"] if side == "no" else q["yes_contracts"]
        if self.faults.wrong_accept_side_contracts:
            contracts = q["yes_contracts"] if side == "no" else q["no_contracts"]
        self._broadcast("quote_accepted", {**self._quote_ws(q), "accepted_side": side, "contracts_accepted_fp": f"{contracts:.2f}"})
        self._spawn(self._confirm_and_execute(r, q, side, contracts))
        return httpx.Response(204)

    async def _confirm_and_execute(self, r: dict[str, Any], q: dict[str, Any], side: str, contracts: float) -> None:
        if self.faults.no_confirm:
            await asyncio.sleep(3.0)
            q["status"] = "cancelled"
            return
        await asyncio.sleep(0.3)
        if q["status"] != "accepted":
            return
        q["status"] = "confirmed"
        await asyncio.sleep(1.0)
        q["status"] = "executed"
        oid = str(uuid.uuid4())
        outcome = "yes" if side == "no" else "no"
        price = (1 - q["no_bid"]) if side == "no" else (1 - q["yes_bid"])
        self.fills.append({"fill_id": str(uuid.uuid4()), "order_id": oid, "ticker": r["market_ticker"], "market_ticker": r["market_ticker"],
                           "outcome_side": outcome, "side": outcome, "action": "buy", "count_fp": f"{contracts:.2f}",
                           "yes_price_dollars": f"{price if outcome == 'yes' else 1 - price:.4f}", "no_price_dollars": f"{1 - price if outcome == 'yes' else price:.4f}",
                           "is_taker": True, "created_time": timeutil.iso(time.time()), "ts": int(time.time())})
        self.positions[r["market_ticker"]] = self.positions.get(r["market_ticker"], 0.0) + (contracts if outcome == "yes" else -contracts)
        self.accepts += 1
        self._broadcast("quote_executed", {"quote_id": q["id"], "rfq_id": r["id"], "quote_creator_id": q["maker"], "rfq_creator_id": self.comm_id,
                                           "order_id": oid, "client_order_id": oid, "market_ticker": r["market_ticker"],
                                           "executed_ts": timeutil.iso(time.time())})
        r["status"] = "closed"
        self.open_rfq_by_ticker.pop(r["market_ticker"], None)

    # ------------------------------------------------------------------ public RFQ feed (other users)
    def emit_feed_rfq(self, n_legs: int = 2, game_code: str = "26NOV05JACBAL") -> dict[str, Any]:
        pool = [m for m in self.markets.values() if m["event_ticker"].split("-")[1] == game_code and "PRECISE" not in m["event_ticker"]]
        legs = []
        events: set[str] = set()
        while len(legs) < n_legs and pool:
            mk = self.rng.choice(pool)
            if mk["event_ticker"] in events:
                continue
            events.add(mk["event_ticker"])
            legs.append({"market_ticker": mk["ticker"], "event_ticker": mk["event_ticker"], "side": "yes"})
        sig = "|".join(sorted(f"{l['market_ticker']}:{l['side']}" for l in legs))
        if sig not in self.combos:
            self._create_combo("KXMVECROSSCATEGORY-R", {"selected_markets": legs})
        c = self.combos[sig]
        msg = {"id": str(uuid.uuid4()), "creator_id": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
               "market_ticker": c["ticker"], "event_ticker": c["event_ticker"], "target_cost_dollars": "10.00",
               "created_ts": timeutil.iso(time.time()), "mve_collection_ticker": "KXMVECROSSCATEGORY-R", "mve_selected_legs": legs}
        self._broadcast("rfq_created", msg, mine=False)
        return msg

    def start_feed(self, rate_hz: float = 2.0) -> None:
        async def run() -> None:
            while True:
                await asyncio.sleep(1.0 / rate_hz)
                self.emit_feed_rfq(self.rng.choice([2, 2, 3, 4]), self.rng.choice(["26NOV02CHISEA", "26NOV05JACBAL"]))

        if self._feed_task is None:
            self._feed_task = asyncio.get_event_loop().create_task(run())


class _HTTPError(Exception):
    def __init__(self, status: int, code: str, message: str, details: Any = None):
        super().__init__(message)
        self.status, self.code, self.message, self.details = status, code, message, details


def _ok(obj: Any) -> httpx.Response:
    return httpx.Response(200, json=obj)


def _page(rows: list[dict[str, Any]], p: dict[str, str], key: str) -> dict[str, Any]:
    limit = int(p.get("limit") or 100)
    start = int(p.get("cursor") or 0)
    chunk = rows[start : start + limit]
    nxt = start + limit
    return {key: chunk, "cursor": str(nxt) if nxt < len(rows) else ""}


_MOCK: MockExchange | None = None


def get_mock() -> MockExchange:
    global _MOCK
    if _MOCK is None:
        _MOCK = MockExchange()
    return _MOCK


def reset_mock(**kwargs: Any) -> MockExchange:
    global _MOCK
    _MOCK = MockExchange(**kwargs)
    return _MOCK
