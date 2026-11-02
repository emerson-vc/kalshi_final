"""Player usage priors from nflverse weekly stats (recency-weighted, shrunk toward position means).

Per receiver: target share, catch rate, yards per reception, TD per target. Per rusher: carry share,
yards per carry, TD per carry. Per QB: attempts, yards per attempt, INT rate. Per team: pass attempts,
carries, and the share of offensive TDs that are passing TDs. Kalshi players are matched by
(team, jersey) through rosters, then by name; anyone unmatched (rookies, new signings) gets the
position mean, and the simulator's marginals are mapped to the Kalshi ladders anyway.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from combo_edge.fair import nflverse as nv
from combo_edge.sports.nfl.teams import nflverse_code

log = logging.getLogger(__name__)
DECAY = 0.85  # weight multiplier per team-game of age
_NORM = re.compile(r"[^a-z]")

POS_MEANS = {
    "WR": {"target_share": 0.16, "catch_rate": 0.64, "ypr": 12.5, "td_per_target": 0.055},
    "TE": {"target_share": 0.12, "catch_rate": 0.70, "ypr": 10.5, "td_per_target": 0.06},
    "RB": {"target_share": 0.08, "catch_rate": 0.78, "ypr": 7.5, "td_per_target": 0.03, "carry_share": 0.45, "ypc": 4.3, "td_per_carry": 0.032},
    "QB": {"carry_share": 0.10, "ypc": 5.0, "td_per_carry": 0.04, "ypa": 6.9, "int_rate": 0.024, "comp_rate": 0.65},
}


def _norm(s: str | None) -> str:
    return _NORM.sub("", (s or "").lower())


@dataclass
class PlayerUsage:
    name: str
    position: str
    gsis_id: str | None = None
    target_share: float = 0.0
    catch_rate: float = 0.65
    ypr: float = 11.0
    td_per_target: float = 0.05
    carry_share: float = 0.0
    ypc: float = 4.2
    td_per_carry: float = 0.03
    games: float = 0.0
    matched: bool = False


@dataclass
class TeamUsage:
    team: str
    pass_att: float = 34.0
    carries: float = 26.0
    ypa: float = 6.9
    comp_rate: float = 0.65
    int_rate: float = 0.024
    pass_td_share: float = 0.62
    players: dict[str, PlayerUsage] = field(default_factory=dict)  # by gsis_id or name


class UsageBook:
    """Built once per scan from cached nflverse data."""

    def __init__(self, stats: Any | None, rosters: Any | None):
        self.stats = stats
        self.rosters = rosters
        self._team_cache: dict[str, TeamUsage] = {}

    @classmethod
    def load(cls) -> "UsageBook":
        cur = nv.current_season()
        try:
            stats = nv.player_stats([cur - 1, cur])
        except Exception as exc:  # noqa: BLE001
            log.warning("player stats unavailable: %s", exc)
            stats = None
        try:
            rosters = nv.rosters_weekly([cur])
        except Exception as exc:  # noqa: BLE001
            log.warning("rosters unavailable: %s", exc)
            rosters = None
        return cls(stats, rosters)

    def team(self, kalshi_team: str) -> TeamUsage:
        if kalshi_team in self._team_cache:
            return self._team_cache[kalshi_team]
        tu = TeamUsage(kalshi_team)
        code = nflverse_code(kalshi_team)
        if self.stats is not None:
            try:
                tu = self._build(kalshi_team, code or kalshi_team)
            except Exception as exc:  # noqa: BLE001 - upstream schema drift must not kill the scan
                log.warning("usage build failed for %s: %s", kalshi_team, exc)
        self._team_cache[kalshi_team] = tu
        return tu

    def _build(self, kalshi_team: str, code: str) -> TeamUsage:
        import polars as pl

        df = self.stats.filter((pl.col("team") == code) & (pl.col("season_type") == "REG") if "season_type" in self.stats.columns else (pl.col("team") == code))
        tu = TeamUsage(kalshi_team)
        if df.height == 0:
            return tu
        games = df.select(["season", "week"]).unique().sort(["season", "week"], descending=True)
        order = {(r["season"], r["week"]): i for i, r in enumerate(games.iter_rows(named=True))}
        df = df.with_columns(pl.struct(["season", "week"]).map_elements(lambda s: DECAY ** order.get((s["season"], s["week"]), 99), return_dtype=pl.Float64).alias("w"))
        n_games = sum(DECAY ** i for i in range(len(order)))
        team = df.group_by(["season", "week"]).agg(
            (pl.col("attempts").sum()).alias("att"), (pl.col("carries").sum()).alias("car"), (pl.col("passing_yards").sum()).alias("pyds"),
            (pl.col("completions").sum()).alias("comp"), (pl.col("passing_interceptions").sum()).alias("ints"),
            (pl.col("passing_tds").sum()).alias("ptd"), (pl.col("rushing_tds").sum()).alias("rtd"), pl.col("w").first())
        w = team["w"].to_numpy()
        sw = w.sum() or 1.0

        def wavg(col: str) -> float:
            return float((team[col].to_numpy() * w).sum() / sw)

        att, car = wavg("att"), wavg("car")
        tu.pass_att, tu.carries = max(att, 20.0), max(car, 15.0)
        tu.ypa = (wavg("pyds") / att) if att else 6.9
        tu.comp_rate = (wavg("comp") / att) if att else 0.65
        tu.int_rate = (wavg("ints") + 0.5) / (att + 20) if att else 0.024
        ptd, rtd = wavg("ptd"), wavg("rtd")
        tu.pass_td_share = (ptd * n_games + 0.62 * 8) / ((ptd + rtd) * n_games + 8)
        per = df.group_by(["player_id", "player_display_name", "position"]).agg(
            (pl.col("targets") * pl.col("w")).sum().alias("tg"), (pl.col("receptions") * pl.col("w")).sum().alias("rec"),
            (pl.col("receiving_yards") * pl.col("w")).sum().alias("ryd"), (pl.col("receiving_tds") * pl.col("w")).sum().alias("rtd"),
            (pl.col("carries") * pl.col("w")).sum().alias("car"), (pl.col("rushing_yards") * pl.col("w")).sum().alias("ruy"),
            (pl.col("rushing_tds") * pl.col("w")).sum().alias("rutd"), pl.col("w").sum().alias("g"))
        team_tg = float(per["tg"].sum()) or 1.0
        team_car = float(per["car"].sum()) or 1.0
        for r in per.iter_rows(named=True):
            pos = str(r["position"] or "WR")
            m = POS_MEANS.get(pos, POS_MEANS["WR"])
            tg, rec = float(r["tg"] or 0), float(r["rec"] or 0)
            car_ = float(r["car"] or 0)
            pu = PlayerUsage(str(r["player_display_name"] or ""), pos, r["player_id"], matched=True, games=float(r["g"] or 0))
            pu.target_share = tg / team_tg
            pu.catch_rate = (rec + 6 * m.get("catch_rate", 0.65)) / (tg + 6)
            pu.ypr = (float(r["ryd"] or 0) + 5 * m.get("ypr", 11.0)) / (rec + 5)
            pu.td_per_target = (float(r["rtd"] or 0) + 10 * m.get("td_per_target", 0.05)) / (tg + 10)
            pu.carry_share = car_ / team_car
            pu.ypc = (float(r["ruy"] or 0) + 10 * m.get("ypc", 4.2)) / (car_ + 10)
            pu.td_per_carry = (float(r["rutd"] or 0) + 15 * m.get("td_per_carry", 0.03)) / (car_ + 15)
            tu.players[str(r["player_id"])] = pu
            tu.players.setdefault(_norm(pu.name), pu)
        return tu

    def player(self, kalshi_team: str, name: str | None, jersey: str | None, position: str | None) -> PlayerUsage:
        tu = self.team(kalshi_team)
        gsis = self._gsis(kalshi_team, jersey, name)
        if gsis and gsis in tu.players:
            return tu.players[gsis]
        key = _norm(name)
        if key in tu.players:
            return tu.players[key]
        for k, pu in tu.players.items():  # suffixes / initials ("Luther Burden III", "AJ" vs "A.J.")
            if key and len(key) > 5 and (k.startswith(key[:6]) or key.startswith(k[:6])) and len(k) > 5:
                return pu
        pos = position if position in POS_MEANS else ("WR" if position in ("REC", "UNK", None) else position or "WR")
        m = POS_MEANS.get(pos, POS_MEANS["WR"])
        return PlayerUsage(name or "?", pos or "WR", None, target_share=m.get("target_share", 0.06), catch_rate=m.get("catch_rate", 0.65),
                           ypr=m.get("ypr", 11.0), td_per_target=m.get("td_per_target", 0.05), carry_share=0.15 if pos == "RB" else 0.0,
                           ypc=m.get("ypc", 4.2), td_per_carry=m.get("td_per_carry", 0.03), matched=False)

    def _gsis(self, kalshi_team: str, jersey: str | None, name: str | None) -> str | None:
        if self.rosters is None or not jersey:
            return None
        import polars as pl

        code = nflverse_code(kalshi_team)
        try:
            rows = self.rosters.filter((pl.col("team") == code) & (pl.col("jersey_number").cast(pl.Utf8).str.replace(r"\.0$", "") == str(jersey)))
            if rows.height == 0:
                return None
            row = rows.sort("week").tail(1).row(0, named=True)
            return row.get("gsis_id")
        except Exception:  # noqa: BLE001
            return None
