import asyncio
import time

import httpx
import pytest

from combo_edge.kalshi.errors import BadRequest, Conflict
from combo_edge.kalshi.limiter import Limiter, TokenBucket
from combo_edge.kalshi.rest import RestClient, endpoint_label
from combo_edge.safety import ForbiddenEndpoint


async def test_token_bucket_paces_requests() -> None:
    b = TokenBucket(rate=100.0, capacity=10.0)
    t0 = time.monotonic()
    for _ in range(6):
        await b.acquire(10)  # 1 free (capacity) + 5 refills of 0.1 s
    assert 0.4 <= time.monotonic() - t0 <= 0.8


def test_configure_from_account_limits_and_costs() -> None:
    lim = Limiter(utilization=0.5)
    lim.configure(
        {"usage_tier": "advanced", "read": {"refill_rate": 300, "bucket_capacity": 900}, "write": {"refill_rate": 300, "bucket_capacity": 900}},
        {"default_cost": 10, "endpoint_costs": [{"method": "GET", "path": "/trade-api/v2/communications/rfqs/:rfq_id/quotes/:quote_id", "cost": 2}]},
    )
    assert lim.tier == "advanced"
    assert lim.buckets["write"].rate == 150
    assert lim.cost("GET", "/communications/rfqs/abc/quotes/def") == 2
    assert lim.cost("POST", "/communications/rfqs") == 10
    assert Limiter.bucket_for("DELETE", "/communications/rfqs/x") == "write"
    assert Limiter.bucket_for("GET", "/portfolio/balance") == "balance"


def test_basic_write_bucket_has_no_burst() -> None:
    lim = Limiter(utilization=0.6)
    lim.configure({"usage_tier": "basic", "read": {"refill_rate": 200, "bucket_capacity": 600}, "write": {"refill_rate": 100, "bucket_capacity": 100}}, None)
    assert lim.buckets["write"].capacity == 60  # 1 s of budget at 60%


def _client(handler) -> RestClient:  # type: ignore[no-untyped-def]
    return RestClient("mock", None, Limiter(authenticated=True), transport=httpx.MockTransport(handler))


async def test_retries_429_then_succeeds() -> None:
    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(429, json={"error": "too many requests"}) if calls["n"] < 3 else httpx.Response(200, json={"ok": 1})

    c = _client(handler)
    assert await c.request("GET", "/markets") == {"ok": 1}
    assert calls["n"] == 3


async def test_post_rfq_not_retried_on_5xx() -> None:
    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(500, json={"error": {"code": "internal", "message": "x"}})

    c = _client(handler)
    with pytest.raises(Exception):
        await c.request("POST", "/communications/rfqs", json={}, retry="429")
    assert calls["n"] == 1


async def test_error_mapping() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "POST":
            return httpx.Response(409, json={"error": {"code": "conflict", "message": "An open RFQ already exists"}})
        return httpx.Response(400, json={"code": "invalid_parameters", "message": "bad"})

    c = _client(handler)
    with pytest.raises(Conflict):
        await c.request("POST", "/communications/rfqs", json={}, retry="429")
    with pytest.raises(BadRequest) as ei:
        await c.request("GET", "/markets")
    assert ei.value.code == "invalid_parameters"


async def test_forbidden_endpoint_blocked_before_io() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        raise AssertionError("must not be called")

    c = _client(handler)
    with pytest.raises(ForbiddenEndpoint):
        await c.request("POST", "/portfolio/orders", json={})


def test_endpoint_label_hides_ids() -> None:
    assert endpoint_label("GET", "/events/KXNFLGAME-26NOV05JACBAL") == "GET /events/:id"
    assert endpoint_label("DELETE", "/communications/rfqs/0b2f-uuid") == "DELETE /communications/rfqs/:id"


async def test_websocket_reconnects_and_tracks_outage(mock_exchange) -> None:  # type: ignore[no-untyped-def]
    from combo_edge.context import _mock_signer
    from combo_edge.kalshi.ws import KalshiWS

    ws = KalshiWS("mock", _mock_signer(), [{"channels": ["communications"]}], connector=mock_exchange.ws_connect)
    await ws.start()
    assert await ws.wait_connected(2)
    t_drop = time.time()
    mock_exchange.drop_all_ws()
    await asyncio.sleep(0.1)
    assert await ws.wait_connected(5)
    assert ws.stats["connects"] >= 2
    assert ws.was_down_between(t_drop - 0.01, time.time())
    await ws.stop()
