"""Core sweeps (``config/core.yaml``): the combo patterns that are swept in full every run.

A candidate matches a sweep when its two legs (either order) are YES legs of the configured kinds,
positions and threshold windows, on the same team. Matching is structural, so it does not depend on
which template family generated the candidate.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from combo_edge.candidates import Candidate
from combo_edge.sports.base import Leg, SidedLeg

CORE_PATH = Path(__file__).resolve().parent.parent / "config" / "core.yaml"
# Unknown/ambiguous receiver positions are treated like WR/TE, as the template families do.
_POSITION_ALIASES = {"WR": {"WR", "REC", "UNK"}, "TE": {"TE", "REC", "UNK"}}


@dataclass(frozen=True)
class LegSpec:
    kind: str
    positions: frozenset[str] = frozenset()
    min: float | None = None
    max: float | None = None

    def matches(self, s: SidedLeg) -> bool:
        leg = s.leg
        if s.side != "yes" or leg.kind != self.kind:
            return False
        if self.positions and (leg.position or "UNK") not in self.positions:
            return False
        if self.kind == "ml":
            return leg.outcome in (None, leg.team)
        t = leg.threshold
        if self.min is not None and (t is None or t < self.min - 1e-9):
            return False
        return not (self.max is not None and (t is None or t > self.max + 1e-9))


@dataclass(frozen=True)
class Sweep:
    name: str
    label: str
    a: LegSpec
    b: LegSpec

    def matches(self, c: Candidate) -> bool:
        if len(c.legs) != 2:
            return False
        x, y = c.legs
        if not x.leg.team or x.leg.team != y.leg.team:
            return False
        return (self.a.matches(x) and self.b.matches(y)) or (self.a.matches(y) and self.b.matches(x))

    def group(self, c: Candidate) -> tuple[str, ...]:
        """(game, sweep, A-side entity, B-side entity): the unit the round-robin spreads across."""
        x, y = c.legs
        a, b = (x, y) if self.a.matches(x) else (y, x)
        return c.game_code, self.name, _entity(a.leg), _entity(b.leg)


def _entity(leg: Leg) -> str:
    return leg.player_key or f"{leg.team}:{leg.kind}"


def _spec(raw: dict[str, Any]) -> LegSpec:
    pos: set[str] = set()
    for p in raw.get("positions") or []:
        pos |= _POSITION_ALIASES.get(str(p).upper(), {str(p).upper()})
    return LegSpec(kind=str(raw["kind"]), positions=frozenset(pos),
                   min=None if raw.get("min") is None else float(raw["min"]),
                   max=None if raw.get("max") is None else float(raw["max"]))


def load_core(path: str | Path | None = None) -> list[Sweep]:
    p = Path(path) if path else CORE_PATH
    raw = yaml.safe_load(p.read_text()) if p.exists() else {}
    return [Sweep(name, str(spec.get("label") or name), _spec(spec["a"]), _spec(spec["b"]))
            for name, spec in ((raw or {}).get("sweeps") or {}).items()]


def core_match(c: Candidate, sweeps: list[Sweep]) -> Sweep | None:
    for s in sweeps:
        if s.matches(c):
            return s
    return None


def _entity_label(leg: Leg) -> str:
    if leg.player_name:
        return leg.player_name
    return f"{leg.team} {leg.kind.replace('_', ' ')}"


def coverage(cands: list[Candidate], sweeps: list[Sweep], selected: set[str], quoted: set[str] | None = None) -> list[dict[str, Any]]:
    """Per (game, sweep, player/team pair): how many core rungs exist, were selected, and got a quote."""
    rows: dict[tuple[str, ...], dict[str, Any]] = {}
    for c in cands:
        sw = core_match(c, sweeps)
        if sw is None:
            continue
        x, y = c.legs
        a, b = (x, y) if sw.a.matches(x) else (y, x)
        key = sw.group(c)
        r = rows.setdefault(key, {"game": c.game_code, "sweep": sw.name, "label": sw.label,
                                  "pair": f"{_entity_label(a.leg)} × {_entity_label(b.leg)}", "available": 0, "selected": 0, "quoted": 0})
        r["available"] += 1
        r["selected"] += int(c.signature in selected)
        r["quoted"] += int(quoted is not None and c.signature in quoted)
    return sorted(rows.values(), key=lambda r: (r["game"], r["sweep"], r["pair"]))
