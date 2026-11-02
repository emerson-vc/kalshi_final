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


# ---------------------------------------------------------------- Phase 2: fair model + maker model
def fair_model(uni: Universe):  # type: ignore[no-untyped-def]
    from combo_edge.context import default_db
    from combo_edge.fair.model import build_fair_model

    fm = build_fair_model(uni)
    if fm is not None:
        cal = default_db().meta_get(f"calibration:{uni.env}") or {}
        for fam, u in ((cal.get("history") or {}).get("family_u") or {}).items():
            fm.family_u[fam] = fm.family_u[f"no_{fam}"] = float(u)  # wider band where history disagrees
    return fm


def maker_model(db: Database, env: str, uni: Universe):  # type: ignore[no-untyped-def]
    from combo_edge.learning.maker_model import train
    from combo_edge.scan import runtime_settings

    m = train(db, env, runtime_settings(db).maker_half_life_days)
    if m is not None:
        m.universe = uni
    return m


# ---------------------------------------------------------------- Phase 2.5: bandit, promotion, timing, calibration
def load_promoted(db: Database, env: str) -> None:
    from combo_edge.learning.promotion import load_promotions

    load_promotions(db)


def thompson(db: Database, env: str):  # type: ignore[no-untyped-def]
    from combo_edge.learning.bandit import Posteriors, rewards, thompson_key

    return thompson_key(Posteriors(rewards(db, env)))


def learn_after_run(*, db: Database, env: str, settings: RuntimeSettings, progress: Any, **_: Any) -> None:
    from combo_edge.learning.calibration import run_calibration
    from combo_edge.learning.promotion import check_promotions
    from combo_edge.learning.timing import update_recommendation

    for p in check_promotions(db, env, settings):
        progress.note(f"PROMOTED new template family: {p['family']} (n={p['n']}, {p['games']} games, mean edge {100 * p['mean']:+.0f}%)")
    update_recommendation(db, env)
    last = (db.meta_get(f"calibration:{env}") or {}).get("ts") or 0
    if timeutil.now() - float(last) > 24 * 3600:
        try:
            run_calibration(db, env)
        except Exception:  # noqa: BLE001
            log.exception("calibration failed")


# ---------------------------------------------------------------- Phase 3: execution (paper by default), shopping
def execution_engine(*, db: Database, env: str, settings: RuntimeSettings, run_id: int, universe: Universe, progress: Any):  # type: ignore[no-untyped-def]
    from combo_edge.execution import ExecutionEngine, get_mode

    if get_mode(db) == "off":
        return None
    return ExecutionEngine(db, env, settings, run_id, universe, progress)


async def execution_sweep(*, db: Database, env: str, settings: RuntimeSettings, run_id: int, clients: Any, ws: Any, router: Any,
                          execution: Any, progress: Any, **_: Any) -> None:
    from combo_edge.positions import settle_executions
    from combo_edge.shopping import fade_report, make_execution_shopper

    if execution is not None and execution.reshop:
        shop = make_execution_shopper(clients, db, run_id, execution, ws, router, settings)
        n = await execution.end_of_run_sweep(shop)
        if n:
            progress.note(f"execution: re-shopped {n} candidates with fresh quotes")
    try:
        if db.scalar("SELECT COUNT(*) FROM executions WHERE env=:e AND status='executed'", {"e": env}, 0):
            await settle_executions(clients.api, db)
        fade = await fade_report(clients, db, env)
        if fade and fade.get("fading"):
            progress.note(f"warning: quotes on our RFQs look {100 * (fade['late'] - fade['early']):.0f}% worse than public trades lately (possible fading)")
    except Exception:  # noqa: BLE001
        log.exception("post-run settlement/fade check failed")


def default_hooks() -> ScanHooks:
    hooks = ScanHooks()
    hooks.after_probes.append(persist_games)
    hooks.extra_candidates = exploration_and_feed
    hooks.explore_picker_factory = explore_picker
    hooks.fair_model_factory = fair_model
    hooks.maker_model_factory = maker_model
    hooks.after_probes.append(update_panel)
    hooks.prepare.append(load_promoted)
    hooks.exploit_key_factory = thompson
    hooks.after_probes.append(learn_after_run)
    hooks.execution_factory = execution_engine
    hooks.after_probes.append(execution_sweep)
    return hooks
