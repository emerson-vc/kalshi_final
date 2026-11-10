"""Core sweeps (``config/core.yaml``): the combo patterns that are swept every run.

A candidate matches a sweep when its two legs (either order) are YES legs of the configured kinds,
positions and threshold windows, on the same team. Matching is structural, so it does not depend on
which template family generated the candidate. Two rules need the game's lines, supplied by a
``CoreContext`` built from the universe: team totals within ``around_main`` points of the team's main
line, and only each team's ``top_receivers`` receivers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from combo_edge.candidates import Candidate
from combo_edge.sports.base import Leg, SidedLeg

CORE_PATH = Path(__file__).resolve().parent.parent / "config" / "core.yaml"
# Unknown/ambiguous receiver positions are treated like WR/TE, as the template families do.
_POSITION_ALIASES = {"WR": {"WR", "REC", "UNK"}, "TE": {"TE", "REC", "UNK"}}
_RECEIVER_POSITIONS = {"WR", "TE", "REC", "UNK"}


@dataclass
class CoreContext:
    """Per-game facts the sweeps filter on. Missing entries never block a match."""

    main_team_total: dict[tuple[str, str], float] = field(default_factory=dict)  # (game, team) -> main line
    receiver_rank: dict[tuple[str, str], list[str]] = field(default_factory=dict)  # (game, team) -> player keys, best first


@dataclass(frozen=True)
class LegSpec:
    kind: str
    positions: frozenset[str] = frozenset()
    min: float | None = None
    max: float | None = None
    around_main: float | None = None
    top_receivers: int | None = None

    def matches(self, s: SidedLeg, ctx: CoreContext | None = None) -> bool:
        leg = s.leg
        if s.side != "yes" or leg.kind != self.kind:
            return False
        if self.positions and (leg.position or "UNK") not in self.positions:
            return False
        if self.top_receivers and ctx is not None:
            ranked = ctx.receiver_rank.get((leg.game_code, leg.team or ""))
            if ranked is not None and leg.player_key not in ranked[: self.top_receivers]:
                return False
        if self.kind == "ml":
            return leg.outcome in (None, leg.team)
        t = leg.threshold
        if self.min is not None and (t is None or t < self.min - 1e-9):
            return False
        if self.max is not None and (t is None or t > self.max + 1e-9):
            return False
        if self.around_main is not None and ctx is not None:
            main = ctx.main_team_total.get((leg.game_code, leg.team or ""))
            if main is not None and (t is None or abs(t - main) > self.around_main + 1e-9):
                return False
        return True


@dataclass(frozen=True)
class Sweep:
    name: str
    label: str
    a: LegSpec
    b: LegSpec
    share: float = 0.0

    def matches(self, c: Candidate, ctx: CoreContext | None = None) -> bool:
        if len(c.legs) != 2:
            return False
        x, y = c.legs
        if not x.leg.team or x.leg.team != y.leg.team:
            return False
        return (self.a.matches(x, ctx) and self.b.matches(y, ctx)) or (self.a.matches(y, ctx) and self.b.matches(x, ctx))

    def oriented(self, c: Candidate) -> tuple[SidedLeg, SidedLeg]:
        x, y = c.legs
        return (x, y) if self.a.matches(x) and self.b.matches(y) else (y, x)

    def group(self, c: Candidate) -> tuple[str, ...]:
        """(game, sweep, A-side entity, B-side entity): the unit the round-robin spreads across."""
        a, b = self.oriented(c)
        return c.game_code, self.name, _entity(a.leg), _entity(b.leg)


def _entity(leg: Leg) -> str:
    return leg.player_key or f"{leg.team}:{leg.kind}"


def _num(v: Any) -> float | None:
    return None if v is None else float(v)


def _spec(raw: dict[str, Any]) -> LegSpec:
    pos: set[str] = set()
    for p in raw.get("positions") or []:
        pos |= _POSITION_ALIASES.get(str(p).upper(), {str(p).upper()})
    return LegSpec(kind=str(raw["kind"]), positions=frozenset(pos), min=_num(raw.get("min")), max=_num(raw.get("max")),
                   around_main=_num(raw.get("around_main")), top_receivers=None if raw.get("top_receivers") is None else int(raw["top_receivers"]))


def load_core(path: str | Path | None = None) -> list[Sweep]:
    p = Path(path) if path else CORE_PATH
    raw = yaml.safe_load(p.read_text()) if p.exists() else {}
    specs = list(((raw or {}).get("sweeps") or {}).items())
    equal = 1.0 / len(specs) if specs else 0.0
    return [Sweep(name, str(spec.get("label") or name), _spec(spec["a"]), _spec(spec["b"]), float(spec.get("share", equal)))
            for name, spec in specs]


def core_match(c: Candidate, sweeps: list[Sweep], ctx: CoreContext | None = None) -> Sweep | None:
    for s in sweeps:
        if s.matches(c, ctx):
            return s
    return None


def build_context(uni: Any, usage: Any = None) -> CoreContext:
    """Main team-total lines and receiver rankings for every game in the universe.

    Receivers rank by their anytime-TD (1+) price; a receiver without that market falls back to the
    nflverse target share (``usage``: a UsageBook, loaded lazily when needed)."""
    from combo_edge.gamelines import game_lines

    ctx = CoreContext()
    for code, legs in uni.legs_by_game.items():
        g = uni.games.get(code)
        if g is None:
            continue
        lines = game_lines(legs, (g.away_team, g.home_team))
        for team, main in (lines.get("team_total") or {}).items():
            if main is not None:
                ctx.main_team_total[(code, team)] = float(main)
        td_price: dict[str, float] = {}
        players: dict[str, Leg] = {}
        for leg in legs:
            if leg.scope != "player" or not leg.player_key or (leg.position or "UNK") not in _RECEIVER_POSITIONS:
                continue
            players.setdefault(leg.player_key, leg)
            if leg.kind == "anytime_td" and leg.threshold is not None and leg.threshold < 1.0 and not leg.protected:
                p = leg.p_est if leg.p_est is not None else leg.p_market()
                if p is not None:
                    td_price[leg.player_key] = float(p)
        by_team: dict[str, list[Leg]] = {}
        for leg in players.values():
            by_team.setdefault(leg.team or "", []).append(leg)
        for team, recs in by_team.items():
            if any(r.player_key not in td_price for r in recs) and usage is None:
                usage = _load_usage()
            ctx.receiver_rank[(code, team)] = [r.player_key for r in sorted(recs, key=lambda r: _rank_key(r, td_price, usage), reverse=True)
                                               if r.player_key]
    return ctx


def _rank_key(leg: Leg, td_price: dict[str, float], usage: Any) -> tuple[int, float]:
    if leg.player_key in td_price:
        return 1, td_price[leg.player_key]
    share = 0.0
    if usage is not None and usage is not False:
        try:
            pu = usage.player(leg.team, leg.player_name, leg.jersey, leg.position)
            share = float(pu.target_share) if pu is not None else 0.0
        except Exception:  # noqa: BLE001
            share = 0.0
    return 0, share


def _load_usage() -> Any:
    try:
        from combo_edge.fair.player_models import UsageBook

        return UsageBook.load()
    except Exception:  # noqa: BLE001
        return False  # tried and unavailable: rank without target shares


def _entity_label(leg: Leg) -> str:
    if leg.player_name:
        return leg.player_name
    return f"{leg.team} {leg.kind.replace('_', ' ')}"


def coverage(cands: list[Candidate], sweeps: list[Sweep], selected: set[str], quoted: set[str] | None = None,
             ctx: CoreContext | None = None) -> list[dict[str, Any]]:
    """Per (game, sweep, player/team pair): how many core rungs exist, were selected, and got a quote."""
    rows: dict[tuple[str, ...], dict[str, Any]] = {}
    for c in cands:
        sw = core_match(c, sweeps, ctx)
        if sw is None:
            continue
        a, b = sw.oriented(c)
        key = sw.group(c)
        r = rows.setdefault(key, {"game": c.game_code, "sweep": sw.name, "label": sw.label,
                                  "pair": f"{_entity_label(a.leg)} × {_entity_label(b.leg)}", "available": 0, "selected": 0, "quoted": 0})
        r["available"] += 1
        r["selected"] += int(c.signature in selected)
        r["quoted"] += int(quoted is not None and c.signature in quoted)
    return sorted(rows.values(), key=lambda r: (r["game"], r["sweep"], r["pair"]))
