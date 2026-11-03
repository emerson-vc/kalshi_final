"""NFL template families. Every family walks the FULL threshold ladder on both legs, extremes
included (4+/5+ pass TD, 3+ TD, 160+/190+ rec yds, 1H by 10.5+) — never main lines only.

All pairs are within one game. Leg order is (star/tail leg, outcome leg).
"""

from __future__ import annotations

from collections.abc import Iterator

from combo_edge.candidates import GameIndex, family, negated
from combo_edge.sports.base import Leg, SidedLeg
from combo_edge.sports.nfl.roster import is_rb, is_receiver

Pair = tuple[SidedLeg, SidedLeg]


def _yes(leg: Leg) -> SidedLeg:
    return SidedLeg(leg, "yes")


def _qbs(idx: GameIndex) -> list[Leg]:
    return idx.players_where(lambda p: p.position == "QB")


def _cross(a_legs: list[Leg], b_legs: list[Leg]) -> Iterator[Pair]:
    for a in a_legs:
        for b in b_legs:
            yield _yes(a), _yes(b)


@family("qb_ptd_x_team_total", "QB pass-TD ladder × own team total overs")
def qb_ptd_x_team_total(idx: GameIndex) -> Iterator[Pair]:
    for qb in _qbs(idx):
        yield from _cross(idx.ladder(qb.player_key or "", "pass_td"), idx.team_ladder(qb.team, "team_total"))


@family("qb_ptd_x_team_ml", "QB pass-TD ladder × own team ML")
def qb_ptd_x_team_ml(idx: GameIndex) -> Iterator[Pair]:
    for qb in _qbs(idx):
        yield from _cross(idx.ladder(qb.player_key or "", "pass_td"), idx.team_ml(qb.team))


@family("qb_pyds_x_rec_yds", "QB pass-yds ladder × own WR/TE rec-yds ladder")
def qb_pyds_x_rec_yds(idx: GameIndex) -> Iterator[Pair]:
    for qb in _qbs(idx):
        q_ladder = idx.ladder(qb.player_key or "", "pass_yds")
        for rec in idx.players_where(lambda p, t=qb.team: p.team == t and is_receiver(p.position)):
            yield from _cross(q_ladder, idx.ladder(rec.player_key or "", "rec_yds"))


@family("qb_ptd_x_rec_td", "QB pass-TD ladder × own WR/TE TD count")
def qb_ptd_x_rec_td(idx: GameIndex) -> Iterator[Pair]:
    for qb in _qbs(idx):
        q_ladder = idx.ladder(qb.player_key or "", "pass_td")
        for rec in idx.players_where(lambda p, t=qb.team: p.team == t and is_receiver(p.position)):
            yield from _cross(q_ladder, idx.ladder(rec.player_key or "", "anytime_td"))


@family("wr_td_x_team_total", "WR/TE anytime/2+/3+ TD × own team total overs")
def wr_td_x_team_total(idx: GameIndex) -> Iterator[Pair]:
    for wr in idx.players_where(lambda p: is_receiver(p.position)):
        yield from _cross(idx.ladder(wr.player_key or "", "anytime_td"), idx.team_ladder(wr.team, "team_total"))


@family("ml_x_h1_spread", "Team ML × same team's 1H spread ladder")
def ml_x_h1_spread(idx: GameIndex) -> Iterator[Pair]:
    for team in idx.teams():
        for ml in idx.team_ml(team):
            for sp in idx.team_ladder(team, "h1_spread"):
                yield _yes(sp), _yes(ml)


@family("qb_x_game_total", "QB pass-TD and pass-yds ladders × full-game total overs")
def qb_x_game_total(idx: GameIndex) -> Iterator[Pair]:
    totals = idx.by_kind.get("game_total", [])
    for qb in _qbs(idx):
        key = qb.player_key or ""
        yield from _cross(idx.ladder(key, "pass_td") + idx.ladder(key, "pass_yds"), totals)


@family("rb_x_ml_spread", "RB rush-yds and TD ladders × own team ML and spread ladder")
def rb_x_ml_spread(idx: GameIndex) -> Iterator[Pair]:
    for rb in idx.players_where(lambda p: is_rb(p.position)):
        key = rb.player_key or ""
        outcome_legs = idx.team_ml(rb.team) + idx.team_ladder(rb.team, "spread")
        yield from _cross(idx.ladder(key, "rush_yds") + idx.ladder(key, "anytime_td"), outcome_legs)


# "No" versions of the positively correlated families (e.g. No QB pass TD + No WR TD).
for _base in (
    "qb_ptd_x_team_total",
    "qb_ptd_x_team_ml",
    "qb_pyds_x_rec_yds",
    "qb_ptd_x_rec_td",
    "wr_td_x_team_total",
    "ml_x_h1_spread",
    "qb_x_game_total",
    "rb_x_ml_spread",
):
    negated(_base)
