"""Candidate-coverage report. Built during every run and stored with it; shown in the Health tab.

A game that has player props but yields zero candidates is a hard error (the run turns red).
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Coverage:
    games: list[dict[str, Any]] = field(default_factory=list)
    legs_by_game_kind: dict[str, Counter[str]] = field(default_factory=lambda: defaultdict(Counter))
    unparsed: dict[str, dict[str, Any]] = field(default_factory=dict)
    not_eligible: dict[str, list[str]] = field(default_factory=lambda: defaultdict(list))
    excluded: Counter[str] = field(default_factory=Counter)  # parsed but excluded by config (precise, prot, ...)
    candidates_by_family: Counter[str] = field(default_factory=Counter)
    candidates_by_game: Counter[str] = field(default_factory=Counter)
    combos: Counter[str] = field(default_factory=Counter)  # existed / created / rejected / budget_skip ...
    skipped: Counter[str] = field(default_factory=Counter)
    p_sources: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    collection: str | None = None

    def add_unparsed(self, series: str, ticker: str, reason: str, max_examples: int = 5) -> None:
        entry = self.unparsed.setdefault(series, {"count": 0, "examples": []})
        entry["count"] += 1
        if len(entry["examples"]) < max_examples:
            entry["examples"].append({"ticker": ticker, "reason": reason})

    def check_games_with_props(self, prop_kinds: set[str]) -> None:
        for g in self.games:
            code = g["game_code"]
            kinds = self.legs_by_game_kind.get(code, Counter())
            has_props = any(k in prop_kinds for k in kinds)
            g["has_props"] = has_props
            if has_props and self.candidates_by_game.get(code, 0) == 0:
                self.errors.append(f"{code}: game has player props but produced zero candidates")

    @property
    def ok(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict[str, Any]:
        return {
            "collection": self.collection,
            "games": self.games,
            "legs_by_game_kind": {g: dict(c) for g, c in self.legs_by_game_kind.items()},
            "unparsed": self.unparsed,
            "not_eligible": {g: sorted(set(v)) for g, v in self.not_eligible.items()},
            "excluded": dict(self.excluded),
            "candidates_by_family": dict(self.candidates_by_family),
            "candidates_by_game": dict(self.candidates_by_game),
            "combos": dict(self.combos),
            "skipped": dict(self.skipped),
            "p_sources": self.p_sources,
            "errors": self.errors,
            "warnings": self.warnings,
            "ok": self.ok,
        }

    def text(self) -> str:
        lines = [f"Coverage report (collection {self.collection})", f"  games found: {len(self.games)}"]
        for g in self.games:
            kinds = self.legs_by_game_kind.get(g["game_code"], Counter())
            lines.append(f"  {g['game_code']}: {sum(kinds.values())} legs " + ", ".join(f"{k}={v}" for k, v in sorted(kinds.items())))
        if self.unparsed:
            lines.append("  unparsed legs:")
            for s, e in sorted(self.unparsed.items()):
                ex = "; ".join(f"{x['ticker']} ({x['reason']})" for x in e["examples"][:2])
                lines.append(f"    {s}: {e['count']}  e.g. {ex}")
        lines.append("  candidates by family: " + ", ".join(f"{k}={v}" for k, v in sorted(self.candidates_by_family.items())))
        lines.append("  combos: " + ", ".join(f"{k}={v}" for k, v in sorted(self.combos.items())))
        if self.skipped:
            lines.append("  skipped: " + ", ".join(f"{k}={v}" for k, v in sorted(self.skipped.items())))
        for e in self.errors:
            lines.append(f"  ERROR: {e}")
        for w in self.warnings:
            lines.append(f"  warning: {w}")
        return "\n".join(lines)
