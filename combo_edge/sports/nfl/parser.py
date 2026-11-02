"""Parse a Kalshi NFL market dict into a ``Leg``. Never raises: unparseable markets come back with
``classified=False`` and a reason, so the coverage report can list them with examples."""

from __future__ import annotations

import re
from typing import Any

from combo_edge import timeutil
from combo_edge.pricing import parse_dollars
from combo_edge.sports.base import Game, Leg
from combo_edge.sports.nfl.series import lookup

_TRAILING_DIGITS = re.compile(r"(\d+)$")


def _bid(v: Any) -> float | None:
    x = parse_dollars(v)
    return x if x is not None and x > 0 else None


def _ask(v: Any) -> float | None:
    x = parse_dollars(v)
    return x if x is not None and 0 < x < 1 else None


def _team_from_prefix(text: str, teams: list[str]) -> str | None:
    for t in sorted(teams, key=len, reverse=True):
        if text.startswith(t):
            return t
    return None


def parse_market(m: dict[str, Any], game: Game) -> tuple[Leg, str | None]:
    """Return (leg, reason). ``reason`` is None when the leg is fully classified."""
    ticker = str(m.get("ticker") or "")
    event_ticker = str(m.get("event_ticker") or "")
    series = event_ticker.split("-")[0] if event_ticker else ticker.split("-")[0]
    spec = lookup(series)
    cs = m.get("custom_strike") or {}
    team_uuid = cs.get("football_team")
    player_uuid = cs.get("football_player")
    teams = [game.away_team, game.home_team]
    team = game.team_uuids.get(team_uuid) if team_uuid else None
    floor = m.get("floor_strike")
    strike_type = m.get("strike_type")
    yes_sub = str(m.get("yes_sub_title") or "")

    leg = Leg(
        ticker=ticker,
        event_ticker=event_ticker,
        series=series,
        game_code=game.game_code,
        kind=spec.kind if spec else f"unknown:{series}",
        scope=spec.scope if spec else ("player" if player_uuid else "team" if team_uuid else "game"),
        title=str(m.get("title") or ""),
        protected=bool(spec and spec.protected),
        precise=bool(spec and spec.precise),
        yes_bid=_bid(m.get("yes_bid_dollars")),
        yes_ask=_ask(m.get("yes_ask_dollars")),
        no_bid=_bid(m.get("no_bid_dollars")),
        no_ask=_ask(m.get("no_ask_dollars")),
        yes_bid_size=parse_dollars(m.get("yes_bid_size_fp")),
        yes_ask_size=parse_dollars(m.get("yes_ask_size_fp")),
        last_price=parse_dollars(m.get("last_price_dollars")),
        volume=parse_dollars(m.get("volume_fp")),
        volume_24h=parse_dollars(m.get("volume_24h_fp")),
        open_interest=parse_dollars(m.get("open_interest_fp")),
        open_time=timeutil.parse_ts(m.get("open_time")),
        status=m.get("status"),
        result=m.get("result") or None,
        player_uuid=player_uuid,
        raw=None,
    )
    if spec is None:
        leg.classified = False
        return leg, f"unknown series {series}"

    rest = ticker[len(event_ticker) + 1 :] if ticker.startswith(event_ticker + "-") else ticker.split("-", 2)[-1]
    if spec.ladder:
        if strike_type != "greater" or floor is None:
            leg.classified = False
            return leg, f"ladder market without a 'greater' floor_strike (strike_type={strike_type})"
        leg.threshold = float(floor)

    if spec.scope == "player":
        part = rest.split("-")[0]
        if team is None:
            team = _team_from_prefix(part, teams)
        code = part[len(team) :] if team and part.startswith(team) else part
        leg.player_code = code or None
        jm = _TRAILING_DIGITS.search(code or "")
        leg.jersey = jm.group(1) if jm else None
        leg.player_name = yes_sub.split(":")[0].strip() if ":" in yes_sub else (yes_sub.strip() or None)
        if code and code.endswith("DST"):
            leg.position, leg.position_source = "DST", "ticker"
        elif code == "NONE" or (leg.player_name or "").lower().startswith("no touchdown"):
            leg.outcome = "NONE"
        if not spec.ladder:
            leg.outcome = leg.outcome or leg.player_name
        if not (player_uuid or code):
            leg.classified = False
            return leg, "player market without a player id"
    elif spec.scope == "team":
        if team is None:
            team = _team_from_prefix(rest, teams)
        special = None
        if rest.upper() in ("TIE", "NONE") or yes_sub.lower() in ("tie", "no touchdown") or str(cs.get("Winning Margin", "")).lower() == "tie":
            special = "NONE" if (rest.upper() == "NONE" or yes_sub.lower() == "no touchdown") else "TIE"
        if spec.kind == "win_margin":
            leg.outcome = special or str(cs.get("Winning Margin") or yes_sub)
        elif not spec.ladder:
            leg.outcome = special or team
        if special:
            team = None
        if team is None and leg.outcome not in ("TIE", "NONE"):
            leg.classified = False
            return leg, "team market whose team could not be resolved"
    leg.team = team
    leg.opponent = game.opponent(team)
    return leg, None
