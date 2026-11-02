"""Wires every phase's components into ``run_scan`` (exploration, fair model, maker model, execution,
re-quote panel, promotions). One place, so the Run button, the CLI and schedulers all get the same
pipeline."""

from __future__ import annotations

import logging
from typing import Any

from combo_edge import timeutil
from combo_edge.scan import ScanHooks
from combo_edge.storage import Database
from combo_edge.storage import schema as S

log = logging.getLogger(__name__)


def persist_games(*, db: Database, universe: Any, **_: Any) -> None:
    for g in universe.games.values():
        db.upsert(S.games, {"game_id": g.game_id, "game_code": g.game_code, "title": g.title, "home_team": g.home_team,
                            "away_team": g.away_team, "kickoff": g.kickoff, "season": g.season, "week": g.week,
                            "related_events": g.related_events, "updated_at": timeutil.now()}, ["game_id"])


def default_hooks() -> ScanHooks:
    hooks = ScanHooks()
    hooks.after_probes.append(persist_games)
    _extend(hooks)
    return hooks


def _extend(hooks: ScanHooks) -> None:
    """Later phases register here (kept in one function so the wiring is easy to audit)."""
    for name in ("exploration", "fair", "maker", "execution", "panel", "promotion"):
        fn = _REGISTRY.get(name)
        if fn is not None:
            try:
                fn(hooks)
            except Exception:  # noqa: BLE001 - a broken optional component must not block scanning
                log.exception("hook %s failed to register", name)


_REGISTRY: dict[str, Any] = {}


def register(name: str):  # type: ignore[no-untyped-def]
    def deco(fn):  # type: ignore[no-untyped-def]
        _REGISTRY[name] = fn
        return fn

    return deco
