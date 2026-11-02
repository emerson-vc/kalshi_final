"""My bets: positions pulled from Kalshi (read-only), price paid from fills, re-checks, CLV, settlement.

CLV (closing-line value) for a YES combo bought at price p_paid: clv = p_now / p_paid - 1 using the
latest re-quote taker YES price (positive = the market moved your way). For settled bets the "close" is
the last re-quote before kickoff.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from typing import Any

from combo_edge import pricing, timeutil
from combo_edge.kalshi.api import KalshiAPI
from combo_edge.storage import Database, loads
from combo_edge.storage import schema as S

log = logging.getLogger(__name__)


def is_combo(ticker: str) -> bool:
    return ticker.startswith("KXMVE")


async def sync_portfolio(api: KalshiAPI, db: Database) -> dict[str, int]:
    env = api.env
    now = timeutil.now()
    positions = await api.positions()
    rows = []
    for p in positions:
        t = str(p.get("ticker") or "")
        rows.append({"ts": now, "env": env, "ticker": t, "position": pricing.parse_dollars(p.get("position_fp")),
                     "market_exposure": pricing.parse_dollars(p.get("market_exposure_dollars")),
                     "realized_pnl": pricing.parse_dollars(p.get("realized_pnl_dollars")), "fees_paid": pricing.parse_dollars(p.get("fees_paid_dollars")),
                     "raw": p})
    db.execute("DELETE FROM positions WHERE env=:e", {"e": env})
    db.insert_many(S.positions, rows)
    fills = await api.fills(min_ts=int(now - 120 * 86400))
    for f in fills:
        side = (f.get("outcome_side") or f.get("side") or "").lower()
        yes_px = pricing.parse_dollars(f.get("yes_price_dollars"))
        no_px = pricing.parse_dollars(f.get("no_price_dollars"))
        db.upsert(S.fills, {"fill_id": f.get("fill_id") or f.get("trade_id"), "env": env, "ts": timeutil.parse_ts(f.get("created_time")) or f.get("ts"),
                            "ticker": f.get("market_ticker") or f.get("ticker"), "outcome_side": side, "action": f.get("action"),
                            "count": pricing.parse_dollars(f.get("count_fp")), "price": yes_px if side == "yes" else no_px,
                            "order_id": f.get("order_id"), "is_taker": bool(f.get("is_taker")), "raw": f}, ["fill_id"])
    settlements = await api.settlements()
    for s in settlements:
        db.upsert(S.settlements, {"env": env, "ts": timeutil.parse_ts(s.get("settled_time")), "ticker": s.get("ticker"),
                                  "revenue": (float(s.get("revenue") or 0) / 100.0) if isinstance(s.get("revenue"), int) else pricing.parse_dollars(s.get("revenue")),
                                  "result": s.get("market_result"), "raw": s}, ["env", "ticker"])
    # Make sure every held combo is in the index with its legs (for exposure by game/player).
    known = {r["ticker"] for r in db.fetchall("SELECT ticker FROM combos")}
    missing = [r["ticker"] for r in rows if is_combo(r["ticker"]) and r["ticker"] not in known]
    if missing:
        for m in await api.markets_by_tickers(missing):
            legs = [(l["market_ticker"], l["side"]) for l in m.get("mve_selected_legs") or []]
            if legs:
                from combo_edge.combos import ComboIndex

                code = (legs[0][0].split("-") + ["", ""])[1]
                ComboIndex(db).add(m["ticker"], legs, "position", game_code=code, event_ticker=m.get("event_ticker"))
    return {"positions": len(rows), "fills": len(fills), "settlements": len(settlements)}


def price_paid(db: Database, env: str, ticker: str) -> tuple[float | None, float]:
    rows = db.fetchall("SELECT count, price FROM fills WHERE env=:e AND ticker=:t AND outcome_side='yes' AND (action IS NULL OR action='buy')",
                       {"e": env, "t": ticker})
    n = sum(float(r["count"] or 0) for r in rows)
    if n <= 0:
        return None, 0.0
    return sum(float(r["count"] or 0) * float(r["price"] or 0) for r in rows) / n, n


def latest_requote(db: Database, env: str, ticker: str, before: float | None = None) -> dict[str, Any] | None:
    sql = "SELECT taker_yes_price, combo_payout, created_at FROM rfqs WHERE env=:e AND combo_ticker=:t AND taker_yes_price IS NOT NULL"
    params: dict[str, Any] = {"e": env, "t": ticker}
    if before is not None:
        sql += " AND created_at < :b"
        params["b"] = before
    return db.fetchone(sql + " ORDER BY created_at DESC LIMIT 1", params)


def combo_positions(db: Database, env: str) -> list[dict[str, Any]]:
    out = []
    for p in db.fetchall("SELECT * FROM positions WHERE env=:e AND position IS NOT NULL AND position != 0", {"e": env}):
        t = p["ticker"]
        if not is_combo(t):
            continue
        combo = db.fetchone("SELECT legs, game_code, signature FROM combos WHERE ticker=:t", {"t": t}) or {}
        paid, n = price_paid(db, env, t)
        rq = latest_requote(db, env, t)
        now_px = rq["taker_yes_price"] if rq else None
        out.append({"ticker": t, "contracts": p["position"], "legs": loads(combo.get("legs"), []), "game_code": combo.get("game_code"),
                    "signature": combo.get("signature"), "price_paid": paid, "cost": (paid or 0) * abs(p["position"] or 0),
                    "latest_price": now_px, "latest_at": rq["created_at"] if rq else None,
                    "clv": (now_px / paid - 1.0) if (now_px and paid) else None, "exposure": p["market_exposure"]})
    return out


def players_of(db: Database, legs: list[list[str]]) -> list[str]:
    out = []
    for t, _side in legs:
        row = db.fetchone("SELECT player_uuid, team, player_code, scope FROM legs WHERE ticker=:t", {"t": t})
        if row and row["scope"] == "player":
            out.append(row["player_uuid"] or f"{row['team']}:{row['player_code']}")
    return out


async def settle_executions(api: KalshiAPI, db: Database) -> int:
    """Settle paper and live executions whose combo market has a result; also fill in CLV."""
    env = api.env
    pending = db.fetchall("SELECT * FROM executions WHERE env=:e AND status='executed'", {"e": env})
    if not pending:
        return 0
    markets = {m["ticker"]: m for m in await api.markets_by_tickers([r["combo_ticker"] for r in pending])}
    n = 0
    for r in pending:
        m = markets.get(r["combo_ticker"]) or {}
        result = (m.get("result") or "").lower()
        game = db.fetchone("SELECT kickoff FROM games WHERE game_code=:g", {"g": r["game_code"]})
        close = latest_requote(db, env, r["combo_ticker"], before=(game or {}).get("kickoff"))
        clv = None
        if close and close.get("combo_payout") and r["payout"]:
            clv = float(r["payout"]) / float(close["combo_payout"]) - 1.0
        if result in ("yes", "no"):
            win = result == "yes"
            pnl = (float(r["contracts"] or 0) * 1.0 - float(r["stake"] or 0)) if win else -float(r["stake"] or 0)
            db.update(S.executions, {"id": r["id"]}, {"status": "settled", "result": "win" if win else "loss", "pnl": pnl,
                                                     "settled_at": timeutil.now(), "clv": clv, "close_payout": (close or {}).get("combo_payout")})
            n += 1
        elif clv is not None:
            db.update(S.executions, {"id": r["id"]}, {"clv": clv, "close_payout": close["combo_payout"]})
    return n


def book_summary(db: Database, env: str, book: str) -> dict[str, Any]:
    rows = db.fetchall("SELECT * FROM executions WHERE env=:e AND book=:b ORDER BY ts", {"e": env, "b": book})
    settled = [r for r in rows if r["status"] == "settled"]
    open_ = [r for r in rows if r["status"] in ("executed", "pending")]
    pnl = sum(float(r["pnl"] or 0) for r in settled)
    staked = sum(float(r["stake"] or 0) for r in settled)
    clvs = [float(r["clv"]) for r in rows if r["clv"] is not None]
    cum, series = 0.0, []
    for r in settled:
        cum += float(r["pnl"] or 0)
        series.append({"t": r["settled_at"], "pnl": round(cum, 2), "clv": r["clv"]})
    by_game: dict[str, float] = defaultdict(float)
    for r in open_:
        by_game[r["game_code"] or "?"] += float(r["stake"] or 0)
    return {"book": book, "n": len(rows), "open": open_, "settled": settled, "pnl": pnl, "roi": (pnl / staked) if staked else None,
            "clv_mean": (sum(clvs) / len(clvs)) if clvs else None, "series": series, "open_by_game": dict(by_game)}
