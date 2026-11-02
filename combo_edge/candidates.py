"""Candidate combos, signatures and the template-family registry.

Add a family by decorating a generator with ``@family(name, description)``. The generator receives a
``GameIndex`` and yields leg pairs. ``negated(base)`` registers the "No + No" version of a family.
Promoted families (Phase 2.5) are registered at runtime from the ``promotions`` table.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from typing import Any

from combo_edge.sports.base import Leg, SidedLeg


def signature_of(legs: Iterable[SidedLeg] | Iterable[tuple[str, str]]) -> str:
    items = []
    for x in legs:
        if isinstance(x, SidedLeg):
            items.append(f"{x.leg.ticker}:{x.side}")
        else:
            items.append(f"{x[0]}:{x[1]}")
    return "|".join(sorted(items))


def parse_signature(sig: str) -> list[tuple[str, str]]:
    out = []
    for part in sig.split("|"):
        t, _, s = part.rpartition(":")
        out.append((t, s))
    return out


@dataclass
class Candidate:
    legs: tuple[SidedLeg, ...]
    family: str
    game_code: str
    signature: str = ""
    stratum_l1: str = ""
    stratum_l2: str = ""
    p_legs: tuple[float | None, ...] = ()
    fair_joint: float | None = None
    fair_lower: float | None = None
    fair_source: str = ""
    fair_version: str = ""
    p_b_given_a: float | None = None
    leg_payouts: tuple[float | None, ...] = ()
    leg_payout_product: float | None = None
    expected_ratio: float = 1.0
    expected_payout: float | None = None
    expected_ev: float | None = None
    expected_ev_lower: float | None = None
    unpriced: bool = False
    combo_ticker: str | None = None
    existed: bool | None = None
    pool: str = ""
    rank: int = 0
    candidate_id: int | None = None
    notes: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.signature:
            self.signature = signature_of(self.legs)

    def describe(self) -> str:
        return " + ".join(s.leg.describe(s.side) for s in self.legs)

    def players(self) -> list[str]:
        return sorted({s.leg.player_key for s in self.legs if s.leg.player_key})

    def player_names(self) -> list[str]:
        return sorted({s.leg.player_name for s in self.legs if s.leg.player_name})

    def legs_json(self) -> list[list[str]]:
        return [[s.leg.ticker, s.side] for s in self.legs]


class GameIndex:
    """Fast lookups over one game's template-eligible legs (regular, non-precise, non-protected)."""

    def __init__(self, legs: Iterable[Leg], include_protected: bool = False):
        self.legs = [l for l in legs if l.classified and not l.precise and (include_protected or not l.protected)]
        self.by_kind: dict[str, list[Leg]] = defaultdict(list)
        self.by_player_kind: dict[tuple[str, str], list[Leg]] = defaultdict(list)
        self.by_team_kind: dict[tuple[str | None, str], list[Leg]] = defaultdict(list)
        self.players: dict[str, Leg] = {}
        for leg in self.legs:
            self.by_kind[leg.kind].append(leg)
            self.by_team_kind[(leg.team, leg.kind)].append(leg)
            if leg.player_key:
                self.by_player_kind[(leg.player_key, leg.kind)].append(leg)
                self.players.setdefault(leg.player_key, leg)
        for lst in list(self.by_kind.values()) + list(self.by_player_kind.values()) + list(self.by_team_kind.values()):
            lst.sort(key=lambda l: (l.threshold if l.threshold is not None else -1.0))

    def teams(self) -> list[str]:
        return sorted({l.team for l in self.legs if l.team})

    def players_where(self, pred: Callable[[Leg], bool]) -> list[Leg]:
        return [p for p in self.players.values() if pred(p)]

    def ladder(self, player_key: str, kind: str) -> list[Leg]:
        return self.by_player_kind.get((player_key, kind), [])

    def team_ladder(self, team: str | None, kind: str) -> list[Leg]:
        return self.by_team_kind.get((team, kind), [])

    def team_ml(self, team: str | None) -> list[Leg]:
        return [l for l in self.by_team_kind.get((team, "ml"), []) if l.outcome == team]


Generator = Callable[[GameIndex], Iterator[tuple[SidedLeg, SidedLeg]]]


@dataclass(frozen=True)
class Family:
    name: str
    description: str
    generator: Generator
    prior_key: str
    negated_of: str | None = None
    exploit: bool = True
    definition: dict[str, Any] | None = None  # promoted families keep their stratum definition here


FAMILIES: dict[str, Family] = {}


def family(name: str, description: str, prior_key: str | None = None) -> Callable[[Generator], Generator]:
    def deco(fn: Generator) -> Generator:
        FAMILIES[name] = Family(name, description, fn, prior_key or name)
        return fn

    return deco


def negated(base: str) -> None:
    src = FAMILIES[base]

    def gen(idx: GameIndex) -> Iterator[tuple[SidedLeg, SidedLeg]]:
        for a, b in src.generator(idx):
            if a.leg.yes_only or b.leg.yes_only:
                continue
            yield SidedLeg(a.leg, "no"), SidedLeg(b.leg, "no")

    name = f"no_{base}"
    FAMILIES[name] = Family(name, f"No + No version of {base}", gen, src.prior_key, negated_of=base)


def register_runtime_family(fam: Family) -> None:
    FAMILIES[fam.name] = fam
