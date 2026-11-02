"""Typed Kalshi REST calls used by combo-edge (docs verified 2026-11-02).

Deliberately absent: any order endpoint, and accepting or confirming quotes. The single accept call lives in
``combo_edge.execution`` behind a capability token (see ``combo_edge.safety``).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Any

from combo_edge.kalshi.rest import RestClient


def _dollars(x: float) -> str:
    return f"{x:.2f}"


class KalshiAPI:
    def __init__(self, rest: RestClient):
        self.rest = rest

    @property
    def env(self) -> str:
        return self.rest.env

    @property
    def authenticated(self) -> bool:
        return self.rest.authenticated

    async def aclose(self) -> None:
        await self.rest.aclose()

    async def _get(self, path: str, params: dict[str, Any] | None = None, *, auth: bool = False) -> Any:
        return await self.rest.request("GET", path, params=params, auth_required=auth)

    async def paginate(
        self, path: str, key: str, params: dict[str, Any] | None = None, *, auth: bool = False, max_pages: int = 200
    ) -> AsyncIterator[dict[str, Any]]:
        p = dict(params or {})
        for _ in range(max_pages):
            body = await self._get(path, p, auth=auth) or {}
            for item in body.get(key) or []:
                yield item
            cursor = body.get("cursor")
            if not cursor or not body.get(key):
                return
            p["cursor"] = cursor

    # ------------------------------------------------------------------ exchange / reference
    async def exchange_status(self) -> dict[str, Any]:
        return await self._get("/exchange/status") or {}

    async def milestones(self, **params: Any) -> list[dict[str, Any]]:
        return [m async for m in self.paginate("/milestones", "milestones", {"limit": 200, **params})]

    async def collections(self, status: str | None = "open", **params: Any) -> list[dict[str, Any]]:
        return [c async for c in self.paginate("/multivariate_event_collections", "multivariate_contracts", {"status": status, "limit": 200, **params})]

    async def collection(self, ticker: str) -> dict[str, Any]:
        body = await self._get(f"/multivariate_event_collections/{ticker}") or {}
        return body.get("multivariate_contract") or body

    async def event(self, ticker: str, nested: bool = True) -> dict[str, Any]:
        body = await self._get(f"/events/{ticker}", {"with_nested_markets": nested}) or {}
        event = body.get("event") or {}
        if nested and not event.get("markets") and body.get("markets"):
            event["markets"] = body["markets"]
        return event

    async def markets(self, **params: Any) -> list[dict[str, Any]]:
        return [m async for m in self.paginate("/markets", "markets", {"limit": 1000, **params})]

    async def markets_by_tickers(self, tickers: Sequence[str]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        uniq = list(dict.fromkeys(tickers))
        for i in range(0, len(uniq), 100):
            out += await self.markets(tickers=",".join(uniq[i : i + 100]))
        return out

    async def market(self, ticker: str) -> dict[str, Any]:
        body = await self._get(f"/markets/{ticker}") or {}
        return body.get("market") or {}

    async def series(self, ticker: str) -> dict[str, Any]:
        body = await self._get(f"/series/{ticker}") or {}
        return body.get("series") or {}

    async def candlesticks_batch(self, tickers: Sequence[str], start_ts: int, end_ts: int, period_min: int = 1440) -> dict[str, list[dict[str, Any]]]:
        out: dict[str, list[dict[str, Any]]] = {}
        uniq = list(dict.fromkeys(tickers))
        for i in range(0, len(uniq), 100):
            body = await self._get(
                "/markets/candlesticks",
                {"market_tickers": ",".join(uniq[i : i + 100]), "start_ts": start_ts, "end_ts": end_ts, "period_interval": period_min},
            ) or {}
            for entry in body.get("markets") or []:
                out[entry.get("market_ticker", "")] = entry.get("candlesticks") or []
        return out

    async def trades(self, ticker: str, **params: Any) -> list[dict[str, Any]]:
        return [t async for t in self.paginate("/markets/trades", "trades", {"ticker": ticker, "limit": 1000, **params}, max_pages=5)]

    # ------------------------------------------------------------------ combos
    async def create_combo(self, collection: str, selected_markets: list[dict[str, str]]) -> dict[str, Any]:
        """Create or resolve a combo market. Counts toward the 5,000/week creation cap (conservatively)."""
        body = {"selected_markets": selected_markets, "with_market_payload": True}
        return await self.rest.request(
            "POST", f"/multivariate_event_collections/{collection}", json=body, auth_required=True, retry="429"
        ) or {}

    async def multivariate_events(self, **params: Any) -> list[dict[str, Any]]:
        return [e async for e in self.paginate("/events/multivariate", "events", {"limit": 200, **params}, max_pages=20)]

    # ------------------------------------------------------------------ RFQs (price discovery)
    async def create_rfq(self, market_ticker: str, target_cost: float) -> str:
        """RFQ sized by fee-inclusive target cost. Never rests a remainder, never replaces existing RFQs."""
        body = {
            "market_ticker": market_ticker,
            "target_cost_dollars": _dollars(target_cost),
            "rest_remainder": False,
            "obscure_creator_id": True,
        }
        assert body["rest_remainder"] is False  # never place resting orders on the book
        res = await self.rest.request("POST", "/communications/rfqs", json=body, auth_required=True, retry="429")
        return str((res or {})["id"])

    async def delete_rfq(self, rfq_id: str) -> None:
        await self.rest.request("DELETE", f"/communications/rfqs/{rfq_id}", auth_required=True, retry="safe")

    async def get_rfq(self, rfq_id: str) -> dict[str, Any]:
        body = await self._get(f"/communications/rfqs/{rfq_id}", auth=True) or {}
        return body.get("rfq") or {}

    async def my_rfqs(self, status: str | None = "open") -> list[dict[str, Any]]:
        return [r async for r in self.paginate("/communications/rfqs", "rfqs", {"user_filter": "self", "status": status, "limit": 100}, auth=True)]

    async def quotes_for_rfq(self, rfq_id: str) -> list[dict[str, Any]]:
        return [
            q
            async for q in self.paginate(
                "/communications/quotes", "quotes", {"rfq_id": rfq_id, "rfq_user_filter": "self", "limit": 500}, auth=True, max_pages=5
            )
        ]

    async def rfq_quote(self, rfq_id: str, quote_id: str) -> dict[str, Any]:
        body = await self._get(f"/communications/rfqs/{rfq_id}/quotes/{quote_id}", auth=True) or {}
        return body.get("quote") or {}

    async def communications_id(self) -> str | None:
        body = await self._get("/communications/id", auth=True) or {}
        return body.get("communications_id")

    # ------------------------------------------------------------------ account
    async def account_limits(self) -> dict[str, Any]:
        return await self._get("/account/limits", auth=True) or {}

    async def endpoint_costs(self) -> dict[str, Any]:
        return await self._get("/account/endpoint_costs") or {}

    async def volume_progress(self) -> dict[str, Any]:
        return await self._get("/account/api_usage_level/volume_progress", auth=True) or {}

    async def upgrade_usage_level(self) -> None:
        await self.rest.request("POST", "/account/api_usage_level/upgrade", auth_required=True, retry="429")

    # ------------------------------------------------------------------ portfolio (read-only)
    async def positions(self, settlement_status: str = "unsettled") -> list[dict[str, Any]]:
        return [
            p
            async for p in self.paginate(
                "/portfolio/positions", "market_positions", {"settlement_status": settlement_status, "limit": 1000}, auth=True
            )
        ]

    async def fills(self, **params: Any) -> list[dict[str, Any]]:
        return [f async for f in self.paginate("/portfolio/fills", "fills", {"limit": 200, **params}, auth=True)]

    async def settlements(self, **params: Any) -> list[dict[str, Any]]:
        return [s async for s in self.paginate("/portfolio/settlements", "settlements", {"limit": 200, **params}, auth=True)]

    async def balance(self) -> dict[str, Any]:
        return await self._get("/portfolio/balance", auth=True) or {}
