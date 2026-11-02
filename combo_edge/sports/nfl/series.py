"""Registry of Kalshi NFL series -> leg kind. Built from the live collection (2026-11-02).

``*PROT`` series ("Protected") settle at the pre-kickoff fair price if the player leaves after the first
half without hitting; ``*PRECISE`` series list every single yard/point. Both are parsed and reported.
By your choice, PROT legs only enter exploration strata, and PRECISE legs are never probed.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SeriesSpec:
    kind: str
    scope: str  # player | team | game
    ladder: bool = False  # threshold markets ("over X" / "k+")
    protected: bool = False
    precise: bool = False


_BASE: dict[str, SeriesSpec] = {
    # game lines
    "KXNFLGAME": SeriesSpec("ml", "team"),
    "KXNFLSPREAD": SeriesSpec("spread", "team", ladder=True),
    "KXNFLTOTAL": SeriesSpec("game_total", "game", ladder=True),
    "KXNFLTEAMTOTAL": SeriesSpec("team_total", "team", ladder=True),
    "KXNFLWINMARGIN": SeriesSpec("win_margin", "team"),
    "KXNFLTIE": SeriesSpec("tie", "game"),
    "KXNFLOT": SeriesSpec("overtime", "game"),
    # halves / quarters
    "KXNFL1H": SeriesSpec("h1_ml", "team"),
    "KXNFL1HSPREAD": SeriesSpec("h1_spread", "team", ladder=True),
    "KXNFL1HTOTAL": SeriesSpec("h1_total", "game", ladder=True),
    "KXNFL1HTEAMTOTAL": SeriesSpec("h1_team_total", "team", ladder=True),
    "KXNFL2H": SeriesSpec("h2_ml", "team"),
    "KXNFL2HSPREAD": SeriesSpec("h2_spread", "team", ladder=True),
    "KXNFL2HTOTAL": SeriesSpec("h2_total", "game", ladder=True),
    "KXNFL1Q": SeriesSpec("q1_ml", "team"),
    "KXNFL1QSPREAD": SeriesSpec("q1_spread", "team", ladder=True),
    "KXNFL1QTOTAL": SeriesSpec("q1_total", "game", ladder=True),
    "KXNFL2Q": SeriesSpec("q2_ml", "team"),
    "KXNFL2QSPREAD": SeriesSpec("q2_spread", "team", ladder=True),
    "KXNFL2QTOTAL": SeriesSpec("q2_total", "game", ladder=True),
    "KXNFL3Q": SeriesSpec("q3_ml", "team"),
    "KXNFL3QSPREAD": SeriesSpec("q3_spread", "team", ladder=True),
    "KXNFL3QTOTAL": SeriesSpec("q3_total", "game", ladder=True),
    "KXNFL4Q": SeriesSpec("q4_ml", "team"),
    "KXNFL4QSPREAD": SeriesSpec("q4_spread", "team", ladder=True),
    "KXNFL4QTOTAL": SeriesSpec("q4_total", "game", ladder=True),
    # touchdown scorers
    "KXNFLTD": SeriesSpec("anytime_td", "player", ladder=True),
    "KXNFLFIRSTTD": SeriesSpec("first_td", "player"),
    "KXNFLTEAMFIRSTTD": SeriesSpec("team_first_td", "player"),
    "KXNFLFIRSTTDTEAM": SeriesSpec("first_td_team", "team"),
    # player stat ladders
    "KXNFLPASSTDS": SeriesSpec("pass_td", "player", ladder=True),
    "KXNFLPASSYDS": SeriesSpec("pass_yds", "player", ladder=True),
    "KXNFLPASSATT": SeriesSpec("pass_att", "player", ladder=True),
    "KXNFLPASSCOMP": SeriesSpec("pass_comp", "player", ladder=True),
    "KXNFLPASSINT": SeriesSpec("pass_int", "player", ladder=True),
    "KXNFLREC": SeriesSpec("receptions", "player", ladder=True),
    "KXNFLRECYDS": SeriesSpec("rec_yds", "player", ladder=True),
    "KXNFLRSHYDS": SeriesSpec("rush_yds", "player", ladder=True),
    "KXNFLRSHATT": SeriesSpec("rush_att", "player", ladder=True),
    "KXNFLRRYDS": SeriesSpec("rush_rec_yds", "player", ladder=True),
    "KXNFLFFPTS": SeriesSpec("fantasy_pts", "player", ladder=True),
    "KXNFLLONGREC": SeriesSpec("long_rec", "player", ladder=True),
    "KXNFLLONGRSH": SeriesSpec("long_rush", "player", ladder=True),
}

PROTECTED_SUFFIX = "PROT"
PRECISE_SUFFIX = "PRECISE"


def lookup(series: str) -> SeriesSpec | None:
    spec = _BASE.get(series)
    if spec is not None:
        return spec
    if series.endswith(PROTECTED_SUFFIX):
        base = _BASE.get(series[: -len(PROTECTED_SUFFIX)])
        if base is not None:
            return SeriesSpec(base.kind, base.scope, base.ladder, protected=True)
    if series.endswith(PRECISE_SUFFIX):
        base = _BASE.get(series[: -len(PRECISE_SUFFIX)])
        if base is not None:
            return SeriesSpec(base.kind, base.scope, base.ladder, precise=True)
    return None


def known_series() -> list[str]:
    return sorted(_BASE)
