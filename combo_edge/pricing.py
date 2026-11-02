"""Fees, payouts, ratios and EV.

Conventions (confirmed with you): payouts are **fee-inclusive taker payouts**, the way the Kalshi app
shows them. A contract bought at price P costs P plus the taker fee 0.07·P·(1−P) per contract, so
payout = 1 / (P + 0.07·P·(1−P)). Your fixture numbers reproduce exactly this way.

Quote side convention: ``yes_bid``/``no_bid`` are the maker's bids, so a taker buys YES at 1 − no_bid.
"""

from __future__ import annotations

import math
from collections.abc import Iterable

TAKER_RATE = 0.07


def taker_fee_raw(contracts: float, price: float, multiplier: float = 1.0) -> float:
    return TAKER_RATE * multiplier * contracts * price * (1.0 - price)


def taker_fee(contracts: float, price: float, multiplier: float = 1.0) -> float:
    """Per-order fee rounded up to the cent (conservative; Kalshi rounds to 1e-6 then to balance precision)."""
    raw = taker_fee_raw(contracts, price, multiplier)
    return math.ceil(raw * 100 - 1e-9) / 100.0


def fee_inclusive_price(price: float, multiplier: float = 1.0) -> float:
    return price + TAKER_RATE * multiplier * price * (1.0 - price)


def payout_fee_inclusive(price: float | None, multiplier: float = 1.0) -> float | None:
    """Decimal payout (total return per $1 staked) for a $1 contract bought at ``price``, fees included."""
    if price is None or price <= 0.0 or price >= 1.0:
        return None
    return 1.0 / fee_inclusive_price(price, multiplier)


def price_from_payout(payout: float, multiplier: float = 1.0) -> float:
    """Invert ``payout_fee_inclusive``: solve P + r·P·(1−P) = 1/payout for P in (0, 1)."""
    r = TAKER_RATE * multiplier
    c = 1.0 / payout
    if r == 0:
        return c
    disc = (1 + r) ** 2 - 4 * r * c
    return ((1 + r) - math.sqrt(disc)) / (2 * r)


def contracts_for_target(target: float, price: float, multiplier: float = 1.0, step: float = 0.01) -> float:
    """Max contracts (0.01 steps) whose principal plus per-order fee (rounded up to the cent) fits ``target``.

    This mirrors Kalshi's default fee-inclusive ``target_cost_dollars`` sizing.
    """
    if price <= 0 or price >= 1 or target <= 0:
        return 0.0
    c = math.floor(target / fee_inclusive_price(price, multiplier) / step) * step
    while c > 0 and c * price + taker_fee(c, price, multiplier) > target + 1e-9:
        c = round(c - step, 2)
    while (c + step) * price + taker_fee(c + step, price, multiplier) <= target + 1e-9:
        c = round(c + step, 2)
    return round(max(c, 0.0), 2)


def taker_yes_price(no_bid: float | None) -> float | None:
    return None if no_bid is None or no_bid <= 0 else round(1.0 - no_bid, 6)


def taker_no_price(yes_bid: float | None) -> float | None:
    return None if yes_bid is None or yes_bid <= 0 else round(1.0 - yes_bid, 6)


def combo_payout_from_contracts(target: float, contracts: float | None) -> float | None:
    if not contracts or target <= 0:
        return None
    return contracts / target


def product(values: Iterable[float]) -> float:
    out = 1.0
    for v in values:
        out *= v
    return out


def ratio(combo_payout: float | None, leg_payouts: Iterable[float | None]) -> float | None:
    legs = list(leg_payouts)
    if combo_payout is None or not legs or any(p is None for p in legs):
        return None
    return combo_payout / product(p for p in legs if p is not None)


def ev(fair_prob: float | None, payout: float | None) -> float | None:
    """EV per $1 staked (fees already inside the fee-inclusive payout). Also the 'edge' label."""
    if fair_prob is None or payout is None:
        return None
    return fair_prob * payout - 1.0


def kelly_fraction(fair_prob: float, payout: float) -> float:
    """Full-Kelly fraction of bankroll for a bet returning ``payout`` per $1 with win prob ``fair_prob``."""
    if payout <= 1.0:
        return 0.0
    return max(0.0, (fair_prob * payout - 1.0) / (payout - 1.0))


def parse_dollars(value: object) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
