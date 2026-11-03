"""Build the NFL universe: upcoming games, every combo-eligible leg, and per-event combo constraints.

Sources (verified live 2026-11-02):
- ``GET /milestones?type=football_game`` (NFL rows have ``details.league == "NFL"``) for games,
  kickoff (``start_date``), home/away team UUIDs and related events.
- ``GET /multivariate_event_collections`` for the combo collection (default ``KXMVECROSSCATEGORY-R``,
  the one Kalshi's own same-game NFL combos go through). Each ``associated_events`` entry carries
  ``size_max`` (GAME/SPREAD/TOTAL/FIRSTTD allow one market per combo) and ``is_yes_only``.
- ``GET /markets?series_ticker=…&status=open`` one series at a time (far fewer calls than per event).
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from combo_edge import timeutil
from combo_edge import ladders
from combo_edge.coverage import Coverage
from combo_edge.kalshi.api import KalshiAPI
from combo_edge.sports.base import Game, Leg
from combo_edge.sports.nfl import roster as roster_mod
from combo_edge.sports.nfl.parser import parse_market
from combo_edge.sports.nfl.teams import split_game_code

log = logging.getLogger(__name__)

PLAYER_PROP_KINDS = {
    "pass_td", "pass_yds", "pass_att", "pass_comp", "pass_int", "receptions", "rec_yds", "rush_yds",
    "rush_att", "rush_rec_yds", "anytime_td", "fantasy_pts", "long_rec", "long_rush",
}


@dataclass
class EventConstraint:
    event_ticker: str
    size_max: int | None = None
    size_min: int | None = None
    yes_only: bool = False


@dataclass
class Universe:
    env: str
    collection_ticker: str
    collection: dict[str, Any]
    games: dict[str, Game] = field(default_factory=dict)
    legs: dict[str, Leg] = field(default_factory=dict)
    legs_by_game: dict[str, list[Leg]] = field(default_factory=lambda: defaultdict(list))
    constraints: dict[str, EventConstraint] = field(default_factory=dict)
    coverage: Coverage = field(default_factory=Coverage)
    fetched_at: float = field(default_factory=timeutil.now)
    game_volume: dict[str, float] = field(default_factory=dict)

    def game_of(self, leg: Leg) -> Game | None:
        return self.games.get(leg.game_code)

    def hours_to_start(self, game_code: str, now_ts: float | None = None) -> float | None:
        g = self.games.get(game_code)
        return timeutil.hours_until(g.kickoff, now_ts) if g and g.kickoff else None


def game_from_milestone(ms: dict[str, Any]) -> Game | None:
    details = ms.get("details") or {}
    if details.get("league") != "NFL":
        return None
    main = str(details.get("main_game_event_ticker") or "")
    if "-" not in main:
        return None
    code = main.split("-", 1)[1]
    teams = split_game_code(code)
    if teams is None:
        return None
    away, home = teams
    uuids: dict[str, str] = {}
    if details.get("away_team_id"):
        uuids[details["away_team_id"]] = away
    if details.get("home_team_id"):
        uuids[details["home_team_id"]] = home
    season = (details.get("season") or {}) if isinstance(details.get("season"), dict) else {}
    return Game(
        game_id=str(ms.get("id")),
        game_code=code,
        title=str(ms.get("title") or code),
        kickoff=timeutil.parse_ts(ms.get("start_date")),
        away_team=away,
        home_team=home,
        team_uuids=uuids,
        related_events=list(ms.get("related_event_tickers") or []),
        season=season.get("year"),
        week=season.get("week"),
        status=details.get("status") or ms.get("status"),
    )


def pick_collection(collections: list[dict[str, Any]], preferred: str) -> tuple[dict[str, Any] | None, list[str]]:
    warnings: list[str] = []
    by_ticker = {c.get("collection_ticker"): c for c in collections}
    if preferred in by_ticker:
        return by_ticker[preferred], warnings
    nfl = [c for c in collections if any(str(e.get("ticker", "")).startswith("KXNFL") for e in c.get("associated_events") or [])]
    if nfl:
        warnings.append(f"collection {preferred} not open; falling back to {nfl[0].get('collection_ticker')}")
        return nfl[0], warnings
    return None, [f"no open collection contains NFL events (wanted {preferred})"]


def is_excluded_leg(leg: Leg) -> bool:
    """Legs never used in any combo: game win margin TIE."""
    return leg.kind == "win_margin" and leg.outcome == "TIE"


def signature_has_excluded_leg(signature: str) -> bool:
    """Same rule on a stored leg signature ("TICKER:side|..."), for combos saved before the rule existed."""
    return any(t.split(":")[0].startswith("KXNFLWINMARGIN-") and t.split(":")[0].endswith("-TIE") for t in signature.split("|"))


async def build_universe(
    api: KalshiAPI,
    collection_ticker: str,
    *,
    hours_ahead: float = 216.0,
    skip_starting_within_min: float = 10.0,
    roster: roster_mod.RosterIndex | None = None,
    include_started: bool = False,
    now_ts: float | None = None,
) -> Universe:
    now_ts = now_ts or timeutil.now()
    cov = Coverage()
    min_start = timeutil.iso(now_ts - 6 * 3600)
    milestones = await api.milestones(type="football_game", minimum_start_date=min_start)
    games: dict[str, Game] = {}
    for ms in milestones:
        g = game_from_milestone(ms)
        if g is None or g.kickoff is None:
            continue
        hrs = (g.kickoff - now_ts) / 3600
        if hrs > hours_ahead:
            continue
        if not include_started and hrs * 60 < skip_starting_within_min:
            cov.skipped["game_started_or_starting"] += 1
            continue
        games[g.game_code] = g

    collections = await api.collections(status="open")
    coll, warns = pick_collection(collections, collection_ticker)
    cov.warnings += warns
    uni = Universe(env=api.env, collection_ticker=(coll or {}).get("collection_ticker", collection_ticker), collection=coll or {}, games=games, coverage=cov)
    cov.collection = uni.collection_ticker
    if coll is None:
        cov.errors.append(warns[-1] if warns else "no collection")
        return uni

    eligible_series: set[str] = set()
    for e in coll.get("associated_events") or []:
        et = str(e.get("ticker") or "")
        parts = et.split("-")
        if len(parts) < 2 or not et.startswith("KXNFL") or parts[1] not in games:
            continue
        uni.constraints[et] = EventConstraint(et, e.get("size_max"), e.get("size_min"), bool(e.get("is_yes_only")))
        eligible_series.add(parts[0])
    for g in games.values():
        for et in g.related_events:
            if et not in uni.constraints:
                cov.not_eligible[g.game_code].append(et.split("-")[0])

    for series in sorted(eligible_series):
        try:
            markets = await api.markets(series_ticker=series, status="open")
        except Exception as exc:  # noqa: BLE001 - one bad series must not kill the run
            cov.errors.append(f"failed to fetch series {series}: {exc}")
            continue
        for m in markets:
            et = str(m.get("event_ticker") or "")
            if et not in uni.constraints:
                continue
            code = et.split("-")[1]
            game = games.get(code)
            if game is None:
                continue
            leg, reason = parse_market(m, game)
            c = uni.constraints[et]
            leg.event_size_max, leg.yes_only = c.size_max, c.yes_only
            uni.game_volume[code] = uni.game_volume.get(code, 0.0) + (leg.volume or 0.0)
            if reason is not None:
                cov.add_unparsed(leg.series, leg.ticker, reason)
                continue
            if is_excluded_leg(leg):
                cov.excluded["win_margin_tie"] += 1
                continue
            uni.legs[leg.ticker] = leg
            uni.legs_by_game[code].append(leg)
            cov.legs_by_game_kind[code][leg.kind + ("[prot]" if leg.protected else "[precise]" if leg.precise else "")] += 1

    cov.p_sources = ladders.estimate(uni.legs.values())
    counts = roster_mod.assign_positions(uni.legs.values(), roster)
    if roster is None:
        cov.warnings.append("nflverse rosters unavailable; positions inferred heuristically")
    log.info("universe: %d games, %d legs, positions %s", len(games), len(uni.legs), counts)
    for g in games.values():
        cov.games.append(
            {"game_code": g.game_code, "title": g.title, "kickoff": g.kickoff, "legs": len(uni.legs_by_game.get(g.game_code, []))}
        )
    return uni
