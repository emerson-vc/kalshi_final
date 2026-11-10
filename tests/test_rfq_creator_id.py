"""Scans hide your ID from makers; the Shop button sends one round under your ID to compare quotes."""

from typing import Any

from combo_edge.kalshi.api import KalshiAPI
from combo_edge.probe import ProbeRequest, ProbeResult
from combo_edge.shopping import summarize_rounds


class _Rest:
    env = "mock"

    def __init__(self) -> None:
        self.bodies: list[dict[str, Any]] = []

    async def request(self, method: str, path: str, json: dict[str, Any] | None = None, **_: Any) -> dict[str, Any]:
        self.bodies.append(json or {})
        return {"id": f"r{len(self.bodies)}"}


async def test_create_rfq_hides_id_by_default_and_can_show_it() -> None:
    rest = _Rest()
    api = KalshiAPI(rest)  # type: ignore[arg-type]
    await api.create_rfq("KXMVE-X", 5.0)
    await api.create_rfq("KXMVE-X", 5.0, obscure_creator_id=False)
    assert [b["obscure_creator_id"] for b in rest.bodies] == [True, False]
    assert all(b["rest_remainder"] is False for b in rest.bodies)


def _res(obscure: bool, price: float, payout: float) -> ProbeResult:
    r = ProbeResult(ProbeRequest("KXMVE-X", "sig", "shop", 5.0, 5.0, obscure_creator_id=obscure), outcome="quoted", n_makers=2)
    r.taker_yes_price, r.combo_payout = price, payout
    return r


def test_shop_summary_separates_anonymous_and_your_id_rounds() -> None:
    out = summarize_rounds([_res(True, 0.054, 17.4), _res(False, 0.0196, 47.0), _res(True, 0.050, 18.7)])
    assert [r["id_shown"] for r in out["per_round"]] == [False, True, False]
    assert out["best_payout_anonymous"] == 18.7
    assert out["best_payout_your_id"] == 47.0
    assert out["best_payout"] == 47.0
