"""Combo tickers: local index, creation budget ledger, rejection book, create-or-resolve.

Kalshi removed the lookup endpoints on Aug 6 2026; ``POST /multivariate_event_collections/{c}`` now
creates *or resolves* a combo. Whether a resolve counts toward the 5,000/week creation cap isn't
documented and no endpoint reports usage, so the ledger counts EVERY POST that Kalshi processed
(conservative), and tracks "likely new" separately from the returned market's ``created_time``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from combo_edge import timeutil
from combo_edge.candidates import Candidate, signature_of
from combo_edge.kalshi.api import KalshiAPI
from combo_edge.kalshi.errors import BadRequest, KalshiError, RateLimited
from combo_edge.storage import Database, dumps
from combo_edge.storage import schema as S

log = logging.getLogger(__name__)
WEEK_S = 7 * 24 * 3600
LEARN_REJECT_THRESHOLD = 2


class ComboIndex:
    def __init__(self, db: Database):
        self.db = db

    def lookup(self, signature: str) -> str | None:
        row = self.db.fetchone("SELECT ticker FROM combos WHERE signature=:s", {"s": signature})
        return row["ticker"] if row else None

    def lookup_many(self, signatures: list[str]) -> dict[str, str]:
        out: dict[str, str] = {}
        for i in range(0, len(signatures), 500):
            chunk = signatures[i : i + 500]
            params = {f"s{j}": s for j, s in enumerate(chunk)}
            ph = ",".join(f":s{j}" for j in range(len(chunk)))
            for r in self.db.fetchall(f"SELECT signature, ticker FROM combos WHERE signature IN ({ph})", params):
                out[r["signature"]] = r["ticker"]
        return out

    def source_of(self, ticker: str) -> str | None:
        row = self.db.fetchone("SELECT source FROM combos WHERE ticker=:t", {"t": ticker})
        return row["source"] if row else None

    def add(
        self,
        ticker: str,
        legs: list[tuple[str, str]],
        source: str,
        *,
        collection: str | None = None,
        game_code: str | None = None,
        event_ticker: str | None = None,
        kalshi_created_time: float | None = None,
    ) -> None:
        sig = signature_of(legs)
        existing = self.db.fetchone("SELECT ticker, source FROM combos WHERE signature=:s", {"s": sig})
        if existing:
            return
        self.db.upsert(
            S.combos,
            {
                "ticker": ticker,
                "signature": sig,
                "collection": collection,
                "game_code": game_code,
                "event_ticker": event_ticker,
                "legs": [list(l) for l in legs],
                "n_legs": len(legs),
                "source": source,
                "first_seen": timeutil.now(),
                "kalshi_created_time": kalshi_created_time,
            },
            ["ticker"],
            update=[],
        )


class CreationLedger:
    def __init__(self, db: Database, env: str):
        self.db = db
        self.env = env

    def used(self, window_s: float = WEEK_S, now_ts: float | None = None) -> dict[str, int]:
        since = (now_ts or timeutil.now()) - window_s
        rows = self.db.fetchall(
            "SELECT result, COUNT(*) AS n FROM creation_ledger WHERE env=:e AND ts>=:t GROUP BY result",
            {"e": self.env, "t": since},
        )
        by = {r["result"]: int(r["n"]) for r in rows}
        conservative = sum(by.get(k, 0) for k in ("new", "resolved", "rejected", "error"))
        return {"conservative": conservative, "likely_new": by.get("new", 0), "resolved": by.get("resolved", 0), "rejected": by.get("rejected", 0), "errors": by.get("error", 0)}

    def remaining(self, weekly_cap: int, reserve: int) -> int:
        return max(0, weekly_cap - reserve - self.used()["conservative"])

    def record(self, run_id: int | None, signature: str, result: str, ticker: str | None, http_status: int | None, detail: Any = None) -> None:
        self.db.insert(
            S.creation_ledger,
            {"ts": timeutil.now(), "env": self.env, "run_id": run_id, "signature": signature, "result": result,
             "combo_ticker": ticker, "http_status": http_status, "detail": dumps(detail) if detail is not None else None},
        )


class RejectionBook:
    def __init__(self, db: Database):
        self.db = db
        self._cache: dict[str, int] | None = None

    def is_rejected(self, signature: str) -> bool:
        return self.db.fetchone("SELECT 1 AS x FROM combo_rejections WHERE signature=:s", {"s": signature}) is not None

    def blocked_classes(self) -> set[str]:
        rows = self.db.fetchall(
            "SELECT rule_class, COUNT(*) AS n FROM combo_rejections WHERE rule_class IS NOT NULL GROUP BY rule_class"
        )
        return {r["rule_class"] for r in rows if int(r["n"]) >= LEARN_REJECT_THRESHOLD}

    def record(self, signature: str, code: str | None, message: str, offending: Any, rule_class: str | None, run_id: int | None) -> None:
        self.db.upsert(
            S.combo_rejections,
            {"signature": signature, "reason_code": code, "message": message[:1000], "offending": offending,
             "rule_class": rule_class, "ts": timeutil.now(), "run_id": run_id},
            ["signature"],
        )


@dataclass
class EnsureResult:
    ticker: str | None
    status: str  # index_hit | created_new | resolved_existing | rejected | budget_exhausted | error
    detail: str = ""


class ComboResolver:
    """Get a combo ticker for a candidate, spending creation budget only on index misses."""

    def __init__(self, api: KalshiAPI, db: Database, env: str, collection: str, run_id: int | None, run_cap: int, weekly_cap: int, reserve: int):
        self.api = api
        self.db = db
        self.env = env
        self.collection = collection
        self.run_id = run_id
        self.index = ComboIndex(db)
        self.ledger = CreationLedger(db, env)
        self.rejections = RejectionBook(db)
        self.run_cap = run_cap
        self.weekly_left = self.ledger.remaining(weekly_cap, reserve)
        self.created_this_run = 0
        self.cap_hit = False

    def budget_left(self) -> int:
        return 0 if self.cap_hit else max(0, min(self.run_cap - self.created_this_run, self.weekly_left))

    async def ensure(self, cand: Candidate, rule_class: str | None = None) -> EnsureResult:
        known = self.index.lookup(cand.signature)
        if known:
            cand.combo_ticker = known
            cand.existed = self.index.source_of(known) not in ("ours_new",)
            return EnsureResult(known, "index_hit")
        if self.rejections.is_rejected(cand.signature):
            return EnsureResult(None, "rejected", "previously rejected by Kalshi")
        if self.budget_left() <= 0:
            return EnsureResult(None, "budget_exhausted")
        legs = [s.selected_market() for s in cand.legs]
        t_req = timeutil.now()
        self.created_this_run += 1
        self.weekly_left -= 1
        try:
            res = await self.api.create_combo(self.collection, legs)
        except RateLimited as exc:
            self.created_this_run -= 1
            self.weekly_left += 1
            return EnsureResult(None, "error", f"rate limited: {exc}")
        except BadRequest as exc:
            msg = f"{exc.code or ''} {exc.message}".strip()
            self.ledger.record(self.run_id, cand.signature, "rejected", None, exc.status, {"code": exc.code, "message": exc.message, "details": exc.details})
            if _looks_like_cap(exc):
                self.cap_hit = True
                return EnsureResult(None, "budget_exhausted", f"Kalshi refused creation: {msg}")
            self.rejections.record(cand.signature, exc.code, msg, exc.details, rule_class, self.run_id)
            return EnsureResult(None, "rejected", msg)
        except KalshiError as exc:
            self.ledger.record(self.run_id, cand.signature, "error", None, exc.status, {"message": exc.message})
            if exc.status == 403 or _looks_like_cap(exc):
                self.cap_hit = True
            return EnsureResult(None, "error", str(exc))
        ticker = str(res.get("market_ticker") or "")
        market = res.get("market") or {}
        created = timeutil.parse_ts(market.get("created_time"))
        existed = created is not None and created < t_req - 5.0
        status = "resolved_existing" if existed else "created_new"
        self.ledger.record(self.run_id, cand.signature, "resolved" if existed else "new", ticker, 200)
        self.index.add(
            ticker,
            [(s.leg.ticker, s.side) for s in cand.legs],
            "ours_resolved" if existed else "ours_new",
            collection=self.collection,
            game_code=cand.game_code,
            event_ticker=res.get("event_ticker"),
            kalshi_created_time=created,
        )
        cand.combo_ticker = ticker
        cand.existed = existed
        return EnsureResult(ticker, status)


def _looks_like_cap(exc: KalshiError) -> bool:
    text = f"{exc.code or ''} {exc.message or ''}".lower()
    return any(w in text for w in ("limit", "quota", "too many creations", "exceeded"))
