"""Player positions (QB / RB / WR / TE / DST).

Primary source: nflverse weekly rosters matched on (team, jersey), then name. Fallback heuristics when
nflverse is unavailable: passing ladders imply QB; rushing-attempt ladders or a rushing ladder longer
than the receiving one imply RB; receivers without a roster match become ``REC`` (WR or TE), so no
receiver is ever dropped from the WR/TE families.
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from collections.abc import Iterable

from combo_edge.sports.base import Leg
from combo_edge.sports.nfl.teams import nflverse_code

log = logging.getLogger(__name__)
_PASS = {"pass_td", "pass_yds", "pass_att", "pass_comp", "pass_int"}
_NORM = re.compile(r"[^a-z]")

RosterIndex = dict[tuple[str, str], tuple[str, str]]  # (nflverse_team, jersey) -> (full_name, position)


def _norm(name: str | None) -> str:
    return _NORM.sub("", (name or "").lower())


def assign_positions(legs: Iterable[Leg], roster: RosterIndex | None = None) -> dict[str, int]:
    """Set ``position``/``position_source`` on every player leg in place. Returns counts by source."""
    by_player: dict[str, list[Leg]] = defaultdict(list)
    for leg in legs:
        if leg.scope == "player" and leg.outcome != "NONE":
            by_player[leg.player_key or leg.ticker].append(leg)
    counts: dict[str, int] = defaultdict(int)
    for group in by_player.values():
        kinds = {g.kind for g in group}
        pos, src = None, None
        sample = group[0]
        if any(g.position == "DST" for g in group):
            pos, src = "DST", "ticker"
        if pos is None and roster and sample.team and sample.jersey:
            hit = roster.get((nflverse_code(sample.team) or "", sample.jersey))
            if hit:
                full_name, rpos = hit
                if not sample.player_name or _norm(full_name)[:5] == _norm(sample.player_name)[:5] or rpos == "QB" and kinds & _PASS:
                    pos, src = _canonical(rpos), "nflverse"
        if pos is None:
            pos, src = _heuristic(group, kinds), "heuristic"
        for g in group:
            g.position, g.position_source = pos, src
        counts[src] += 1
    return dict(counts)


def _canonical(pos: str) -> str:
    pos = (pos or "").upper()
    if pos in {"FB", "HB"}:
        return "RB"
    return pos or "UNK"


def _heuristic(group: list[Leg], kinds: set[str]) -> str:
    if kinds & _PASS:
        return "QB"
    rush_max = max((g.threshold or 0) for g in group if g.kind == "rush_yds") if "rush_yds" in kinds else 0.0
    rec_max = max((g.threshold or 0) for g in group if g.kind == "rec_yds") if "rec_yds" in kinds else 0.0
    if "rush_att" in kinds or (rush_max and rush_max >= rec_max):
        return "RB"
    if kinds & {"rec_yds", "receptions", "anytime_td", "team_first_td", "first_td"}:
        return "REC"
    return "UNK"


def is_receiver(pos: str | None) -> bool:
    return pos in {"WR", "TE", "REC", "UNK"}


def is_wr(pos: str | None) -> bool:
    return pos in {"WR", "REC", "UNK"}


def is_rb(pos: str | None) -> bool:
    return pos == "RB"
