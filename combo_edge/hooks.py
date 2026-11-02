"""Wires every phase's components into ``run_scan``: exploration (1.5), fair model (2), maker model
(2.5), execution (3), re-quote panel (1.5) and promotions (2.5). One place, so the Run button, the CLI
and schedulers all run the same pipeline."""

from __future__ import annotations

import logging
from typing import Any

from combo_edge import timeutil
from combo_edge.candidates import Candidate
from combo_edge.scan import ScanHooks
from combo_edge.settings import RuntimeSettings
from combo_edge.storage import Database
from combo_edge.storage import schema as S
from combo_edge.universe import Universe

log = logging.getLogger(__name__)


def persist_games(*, db: Database, universe: Any, **_: Any) -> None:
    for g in universe.games.values():
        db.upsert(S.games, {"game_id": g.game_id, "game_code": g.game_code, "title": g.title, "home_team": g.home_team,
                            "away_team": g.away_team, "kickoff": g.kickoff, "season": g.season, "week": g.week,
                            "related_events": g.related_events, "updated_at": timeutil.now()}, ["game_id"])


# ---------------------------------------------------------------- Phase 1.5: exploration + panel
def exploration_and_feed(uni: Universe, st: RuntimeSettings, db: Database, blocked: set[str]) -> list[Candidate]:
    from combo_edge.learning.exploration import exploration_candidates, feed_candidates

    out = feed_candidates(db, uni)
    if st.explore_split()[0] > 0:
        out += exploration_candidates(uni, st, blocked)
    return out


def explore_picker(db: Database, env: str, st: RuntimeSettings):  # type: ignore[no-untyped-def]
    from combo_edge.learning.exploration import make_picker

    return make_picker(db, env, st)


def update_panel(*, db: Database, env: str, settings: RuntimeSettings, universe: Universe, rows: list[Any], **_: Any) -> None:
    from combo_edge.requote import add_to_panel

    kickoffs = {g.game_code: g.kickoff for g in universe.games.values()}
    n = add_to_panel(db, env, rows, kickoffs, settings)
    if n:
        log.info("re-quote panel: added %d combos", n)


def default_hooks() -> ScanHooks:
    hooks = ScanHooks()
    hooks.after_probes.append(persist_games)
    hooks.extra_candidates = exploration_and_feed
    hooks.explore_picker_factory = explore_picker
    hooks.after_probes.append(update_panel)
    return hooks
