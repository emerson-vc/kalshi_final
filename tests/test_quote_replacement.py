"""A maker's replaced quote must never set the quoted payout.

Kalshi's GET /communications/quotes returns every quote on the RFQ, cancelled ones included, best NO price
first. The probe re-reads quotes over REST whenever the websocket blinked and on a random 5% cross-check.
Every REST row got the same local receive time, so "latest per maker" used to pick the LAST row in API order,
which is that maker's worst (stale, cancelled) price. That showed a 49x combo as 17.4x.
"""

from combo_edge import pricing
from combo_edge.quotes import QuoteObs, RfqWatch

T0 = 1_800_000_000.0


def _rest(qid: str, maker: str, no_bid: float, created: float, status: str) -> dict:
    return {"id": qid, "rfq_id": "r1", "creator_id": maker, "yes_bid_dollars": "0.0050", "no_bid_dollars": f"{no_bid:.4f}",
            "status": status, "created_ts": created, "rfq_target_cost_dollars": "5.00"}


def _api_order(rows: list[dict]) -> list[dict]:
    return sorted(rows, key=lambda r: (-float(r["no_bid_dollars"]), r["created_ts"]))


def test_rest_rows_use_each_makers_current_quote_not_the_cancelled_one() -> None:
    rows = _api_order([
        _rest("a1", "makerA", 0.946, T0 + 0.4, "cancelled"),  # A's opening quote, then improved
        _rest("a2", "makerA", 0.980, T0 + 1.2, "open"),
        _rest("b1", "makerB", 0.719, T0 + 0.6, "cancelled"),  # B's lowball, then improved
        _rest("b2", "makerB", 0.975, T0 + 1.5, "open"),
    ])
    w = RfqWatch("r1", T0)
    for row in rows:
        w.add_rest(QuoteObs.from_rest(row, recv_ts=T0 + 3.0))  # same receive time for every REST row
    latest = w.latest_by_maker()
    assert {m: q.no_bid for m, q in latest.items()} == {"makerA": 0.980, "makerB": 0.975}
    best = max(q.no_bid for q in latest.values())
    payout = pricing.payout_fee_inclusive(pricing.taker_yes_price(best))
    assert payout is not None and payout > 45  # the phone's ~49x, not 17.4x


def test_rest_cancel_status_updates_a_quote_seen_on_the_websocket() -> None:
    w = RfqWatch("r1", T0)
    ws_msg = {"quote_id": "a1", "rfq_id": "r1", "quote_creator_id": "makerA", "yes_bid_dollars": "0.0050",
              "no_bid_dollars": "0.9800", "created_ts": T0 + 0.4}
    w.add(QuoteObs.from_ws(ws_msg, recv_ts=T0 + 0.5))
    assert not w.add_rest(QuoteObs.from_rest(_rest("a1", "makerA", 0.98, T0 + 0.4, "cancelled"), recv_ts=T0 + 3.0))
    assert len(w.quotes) == 1
    assert w.latest_by_maker() == {}  # the maker withdrew; nothing to take


def test_ws_replacement_still_uses_the_newest_quote() -> None:
    w = RfqWatch("r1", T0)
    for qid, nb, t in (("a1", 0.946, 0.4), ("a2", 0.980, 1.2)):
        w.add(QuoteObs.from_ws({"quote_id": qid, "rfq_id": "r1", "quote_creator_id": "makerA", "yes_bid_dollars": "0.005",
                                "no_bid_dollars": str(nb), "created_ts": T0 + t}, recv_ts=T0 + t + 0.05))
    assert w.latest_by_maker()["makerA"].no_bid == 0.980
