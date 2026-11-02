"""Turn probe results into ranked rows (sorted by EV%).

Per combo: quoted payout (fee-inclusive taker YES), product of fee-inclusive leg payouts, ratio,
prior/blended fair payout, EV% = fair x payout - 1, suggested stake, hours to kickoff, makers, link.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from combo_edge import pricing
from combo_edge.candidates import Candidate
from combo_edge.probe import ProbeResult
from combo_edge.settings import RuntimeSettings
from combo_edge.sizing import kelly_stake


@dataclass
class ResultRow:
    signature: str
    combo_ticker: str
    legs: str
    family: str
    game_code: str
    teams: list[str]
    players: list[str]
    hours_to_start: float | None
    quoted_payout: float | None
    leg_payout_product: float | None
    ratio: float | None
    fair_joint: float | None
    fair_lower: float | None
    fair_payout: float | None
    ev: float | None
    ev_lower: float | None
    fair_source: str
    suggested_stake: float
    n_makers: int
    taker_yes_price: float | None
    best_no_bid: float | None
    second_no_bid: float | None
    quote_spread: float | None
    existed: bool | None
    pool: str
    stratum_l1: str
    rfq_id: str | None
    outcome: str
    link: str | None
    unpriced: bool = False
    execution: dict[str, Any] | None = None
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def market_link(template: str, combo_ticker: str | None, event_ticker: str | None) -> str | None:
    if not combo_ticker:
        return None
    series = (event_ticker or combo_ticker).split("-")[0]
    event = event_ticker or combo_ticker.rsplit("-", 1)[0]
    return template.format(series=series, event=event, series_lower=series.lower(), event_lower=event.lower(), ticker=combo_ticker)


def build_row(res: ProbeResult, cand: Candidate, settings: RuntimeSettings, event_ticker: str | None = None) -> ResultRow:
    payout = res.combo_payout
    ratio = pricing.ratio(payout, cand.leg_payouts) if payout else None
    ev = pricing.ev(cand.fair_joint, payout)
    ev_low = pricing.ev(cand.fair_lower, payout)
    stake = kelly_stake(cand.fair_joint, cand.fair_lower, payout, settings.bankroll, settings.kelly_fraction, settings.cap_combo)
    spread = None
    if res.best_no_bid is not None and res.second_no_bid is not None:
        spread = res.best_no_bid - res.second_no_bid
    return ResultRow(
        signature=cand.signature,
        combo_ticker=res.request.combo_ticker,
        legs=cand.describe(),
        family=cand.family,
        game_code=cand.game_code,
        teams=sorted({s.leg.team for s in cand.legs if s.leg.team}),
        players=cand.player_names(),
        hours_to_start=res.request.hours_to_start,
        quoted_payout=payout,
        leg_payout_product=cand.leg_payout_product,
        ratio=ratio,
        fair_joint=cand.fair_joint,
        fair_lower=cand.fair_lower,
        fair_payout=(1.0 / cand.fair_joint) if cand.fair_joint else None,
        ev=ev,
        ev_lower=ev_low,
        fair_source=cand.fair_source,
        suggested_stake=stake,
        n_makers=res.n_makers,
        taker_yes_price=res.taker_yes_price,
        best_no_bid=res.best_no_bid,
        second_no_bid=res.second_no_bid,
        quote_spread=spread,
        existed=cand.existed,
        pool=cand.pool,
        stratum_l1=cand.stratum_l1,
        rfq_id=res.rfq_id,
        outcome=res.outcome,
        link=market_link(settings.market_link_template, res.request.combo_ticker, event_ticker),
        unpriced=cand.unpriced,
        execution=res.execution,
    )


def rank(rows: list[ResultRow]) -> list[ResultRow]:
    return sorted(rows, key=lambda r: (r.ev is None, -(r.ev or 0.0)))
