"""Leg snapshots at probe time: bid, ask, spread, top-of-book depth, volume, open interest,
move since the market opened, and the game's total volume. Logged from day one for the maker model."""

from __future__ import annotations

import logging
from collections.abc import Iterable

from combo_edge import timeutil
from combo_edge.kalshi.api import KalshiAPI
from combo_edge.pricing import parse_dollars
from combo_edge.sports.base import Leg
from combo_edge.storage import Database
from combo_edge.storage import schema as S
from combo_edge.universe import Universe

log = logging.getLogger(__name__)


def _apply_market(leg: Leg, m: dict) -> None:
    def bid(v: object) -> float | None:
        x = parse_dollars(v)
        return x if x is not None and x > 0 else None

    def ask(v: object) -> float | None:
        x = parse_dollars(v)
        return x if x is not None and 0 < x < 1 else None

    leg.yes_bid, leg.yes_ask = bid(m.get("yes_bid_dollars")), ask(m.get("yes_ask_dollars"))
    leg.no_bid, leg.no_ask = bid(m.get("no_bid_dollars")), ask(m.get("no_ask_dollars"))
    leg.yes_bid_size, leg.yes_ask_size = parse_dollars(m.get("yes_bid_size_fp")), parse_dollars(m.get("yes_ask_size_fp"))
    leg.volume, leg.open_interest = parse_dollars(m.get("volume_fp")), parse_dollars(m.get("open_interest_fp"))
    leg.last_price = parse_dollars(m.get("last_price_dollars"))


async def refresh_legs(api: KalshiAPI, legs: Iterable[Leg]) -> int:
    by_ticker = {l.ticker: l for l in legs}
    if not by_ticker:
        return 0
    try:
        markets = await api.markets_by_tickers(list(by_ticker))
    except Exception as exc:  # noqa: BLE001
        log.warning("leg refresh failed: %s", exc)
        return 0
    for m in markets:
        leg = by_ticker.get(m.get("ticker", ""))
        if leg is not None:
            _apply_market(leg, m)
    return len(markets)


async def open_prices(api: KalshiAPI, db: Database, legs: Iterable[Leg]) -> dict[str, float]:
    """Mid at market open, from daily candlesticks (cached in leg_snapshots.open_price)."""
    out: dict[str, float] = {}
    need: list[Leg] = []
    for leg in legs:
        row = db.fetchone("SELECT open_price FROM leg_snapshots WHERE ticker=:t AND open_price IS NOT NULL LIMIT 1", {"t": leg.ticker})
        if row:
            out[leg.ticker] = float(row["open_price"])
        elif leg.open_time:
            need.append(leg)
    if need:
        start = int(min(l.open_time or 0 for l in need))
        try:
            candles = await api.candlesticks_batch([l.ticker for l in need], start, int(timeutil.now()), 1440)
        except Exception as exc:  # noqa: BLE001
            log.warning("candlesticks unavailable: %s", exc)
            candles = {}
        for t, cs in candles.items():
            for c in cs:
                yb = parse_dollars((c.get("yes_bid") or {}).get("open_dollars"))
                ya = parse_dollars((c.get("yes_ask") or {}).get("open_dollars"))
                px = parse_dollars((c.get("price") or {}).get("open_dollars"))
                if yb and ya and 0 < yb < ya < 1:
                    out[t] = (yb + ya) / 2
                    break
                if px:
                    out[t] = px
                    break
    return out


def write_snapshots(db: Database, run_id: int | None, uni: Universe, legs: Iterable[Leg], opens: dict[str, float], model_p: dict[str, tuple[float | None, float | None]] | None = None) -> int:
    now = timeutil.now()
    rows = []
    for leg in legs:
        mid = leg.p_market()
        op = opens.get(leg.ticker)
        mp, bp = (model_p or {}).get(leg.ticker, (None, None))
        rows.append(
            {"run_id": run_id, "ticker": leg.ticker, "ts": now, "hours_to_start": uni.hours_to_start(leg.game_code, now),
             "yes_bid": leg.yes_bid, "yes_ask": leg.yes_ask, "no_bid": leg.no_bid, "no_ask": leg.no_ask,
             "yes_bid_size": leg.yes_bid_size, "yes_ask_size": leg.yes_ask_size, "spread": leg.spread(), "volume": leg.volume,
             "open_interest": leg.open_interest, "last_price": leg.last_price, "open_price": op,
             "move_since_open": (mid - op) if (mid is not None and op is not None) else None,
             "game_volume": uni.game_volume.get(leg.game_code), "model_p": mp, "blend_p": bp}
        )
        db.upsert(
            S.legs,
            {"ticker": leg.ticker, "game_code": leg.game_code, "event_ticker": leg.event_ticker, "series": leg.series, "kind": leg.kind,
             "scope": leg.scope, "team": leg.team, "opponent": leg.opponent, "player_name": leg.player_name, "player_uuid": leg.player_uuid,
             "player_code": leg.player_code, "jersey": leg.jersey, "position": leg.position, "threshold": leg.threshold,
             "outcome": leg.outcome, "line_label": leg.line_label, "protected": leg.protected, "precise": leg.precise, "title": leg.title,
             "open_time": leg.open_time, "first_seen": now, "last_seen": now},
            ["ticker"],
            update=["last_seen", "position"],
        )
    db.insert_many(S.leg_snapshots, rows)
    return len(rows)
