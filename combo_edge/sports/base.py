"""Sport-agnostic data model and plugin protocol."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class Game:
    game_id: str  # Kalshi milestone id
    game_code: str  # e.g. 26NOV05JACBAL
    title: str
    kickoff: float | None
    away_team: str
    home_team: str
    team_uuids: dict[str, str] = field(default_factory=dict)  # uuid -> abbr
    related_events: list[str] = field(default_factory=list)
    season: int | None = None
    week: int | None = None
    status: str | None = None

    def opponent(self, team: str | None) -> str | None:
        if team == self.home_team:
            return self.away_team
        if team == self.away_team:
            return self.home_team
        return None


@dataclass
class Leg:
    """One Kalshi market usable as a combo leg (YES side semantics; NO is the complement)."""

    ticker: str
    event_ticker: str
    series: str
    game_code: str
    kind: str  # canonical stat kind, e.g. pass_td, team_total, ml
    scope: str  # player | team | game
    title: str = ""
    team: str | None = None
    opponent: str | None = None
    player_name: str | None = None
    player_uuid: str | None = None
    player_code: str | None = None
    jersey: str | None = None
    position: str | None = None
    position_source: str | None = None
    threshold: float | None = None  # "over X" line (floor_strike); YES = stat > X
    outcome: str | None = None  # for structured markets: team / player / TIE
    protected: bool = False
    precise: bool = False
    classified: bool = True
    yes_bid: float | None = None
    yes_ask: float | None = None
    no_bid: float | None = None
    no_ask: float | None = None
    yes_bid_size: float | None = None
    yes_ask_size: float | None = None
    last_price: float | None = None
    volume: float | None = None
    volume_24h: float | None = None
    open_interest: float | None = None
    open_time: float | None = None
    status: str | None = None
    result: str | None = None
    event_size_max: int | None = None
    yes_only: bool = False
    p_est: float | None = None  # ladder-consistent P(YES), see combo_edge.ladders
    p_source: str | None = None  # mid | interp | extrap | ask_only | none
    raw: dict[str, Any] | None = None

    @property
    def player_key(self) -> str | None:
        if self.scope != "player":
            return None
        return self.player_uuid or f"{self.team}:{self.player_code}"

    @property
    def line_label(self) -> str:
        if self.threshold is None:
            return self.outcome or ""
        if self.scope == "player":
            return f"{int(round(self.threshold + 0.5))}+"
        if self.kind.endswith("spread"):
            return f"by {self.threshold:g}+"
        return f"o{self.threshold:g}"

    def mid(self) -> float | None:
        if self.yes_bid is not None and self.yes_ask is not None and self.yes_bid > 0 and self.yes_ask < 1:
            return (self.yes_bid + self.yes_ask) / 2.0
        return None

    def p_market(self) -> float | None:
        """P(YES) from the book: the ladder-consistent estimate when available, else mid, else ask/bid."""
        if self.p_est is not None:
            return self.p_est
        m = self.mid()
        if m is not None:
            return m
        if self.yes_ask is not None and 0 < self.yes_ask < 1:
            return self.yes_ask
        if self.yes_bid is not None and 0 < self.yes_bid < 1:
            return self.yes_bid
        return None

    def ask(self, side: str) -> float | None:
        """Price a taker pays to buy ``side`` of this leg."""
        if side == "yes":
            v = self.yes_ask
            if v is None and self.no_bid:
                v = 1.0 - self.no_bid
        else:
            v = self.no_ask
            if v is None and self.yes_bid:
                v = 1.0 - self.yes_bid
        return v if v is not None and 0 < v < 1 else None

    def spread(self) -> float | None:
        if self.yes_bid is not None and self.yes_ask is not None and self.yes_ask > 0:
            return max(0.0, self.yes_ask - self.yes_bid)
        return None

    def describe(self, side: str = "yes") -> str:
        who = self.player_name or self.team or "Game"
        base = {
            "ml": f"{self.team} ML",
            "spread": f"{self.team} by {self.threshold:g}+" if self.threshold is not None else f"{self.team} spread",
            "team_total": f"{self.team} team o{self.threshold:g}" if self.threshold is not None else f"{self.team} team total",
            "game_total": f"Total o{self.threshold:g}" if self.threshold is not None else "Total",
            "h1_spread": f"{self.team} 1H by {self.threshold:g}+" if self.threshold is not None else f"{self.team} 1H",
            "h1_ml": f"{self.outcome} wins 1H",
            "h1_team_total": f"{self.team} 1H o{self.threshold:g}" if self.threshold is not None else f"{self.team} 1H total",
        }.get(self.kind)
        if base is None:
            stat = self.kind.replace("_", " ")
            base = f"{who} {self.line_label} {stat}".strip() if self.threshold is not None else f"{who} {stat} {self.outcome or ''}".strip()
        return base if side == "yes" else f"NO {base}"


@dataclass(frozen=True)
class SidedLeg:
    leg: Leg
    side: str  # yes | no

    @property
    def ticker(self) -> str:
        return self.leg.ticker

    def p_market(self) -> float | None:
        p = self.leg.p_market()
        if p is None:
            return None
        return p if self.side == "yes" else 1.0 - p

    def ask(self) -> float | None:
        return self.leg.ask(self.side)

    def selected_market(self) -> dict[str, str]:
        return {"market_ticker": self.leg.ticker, "event_ticker": self.leg.event_ticker, "side": self.side}


class SportPlugin(Protocol):
    name: str

    def is_game_milestone(self, milestone: dict[str, Any]) -> bool: ...

    def game_from_milestone(self, milestone: dict[str, Any]) -> Game | None: ...

    def parse_market(self, market: dict[str, Any], game: Game) -> Leg: ...
