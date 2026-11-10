"""Local dashboard: FastAPI + server-rendered Jinja2 + HTMX + Chart.js (all vendored, no CDN).

Bound to 127.0.0.1 only. Tabs: Scan, My bets, Learning, Health. Full scans run only on the Run button.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from combo_edge import execution, timeutil
from combo_edge.scan import ScanProgress, ScanRequest, run_scan, runtime_settings
from combo_edge.settings import RuntimeSettings, load_env_settings
from combo_edge.storage import Database
from combo_edge.web import data

log = logging.getLogger(__name__)
HERE = Path(__file__).resolve().parent
EDITABLE = {
    "stake": float, "rfq_budget": int, "max_new_creations_per_run": int, "quote_window_s": float, "bankroll": float,
    "cap_combo": float, "cap_game": float, "cap_player": float, "kelly_fraction": float, "ev_min": float,
    "daily_loss_limit": float, "weekly_loss_limit": float, "max_bets_per_day": int, "creation_reserve": int,
    "stack_warn": int, "max_open_rfqs": int, "stratum_min_quotes": int,
    "core_share": float, "exploit_share": float, "free_share": float, "explore_share": float,
    "rank_payout_min": float, "rank_payout_max": float,
}


class AppState:
    def __init__(self, db: Database, env: str):
        self.db = db
        self.env = env
        self.progress = ScanProgress()
        self.task: asyncio.Task[Any] | None = None
        self.abort = asyncio.Event()
        self.bg: dict[str, asyncio.Task[Any]] = {}
        self.flash: list[tuple[str, str]] = []
        self.bg_results: dict[str, Any] = {}

    def settings(self) -> RuntimeSettings:
        return runtime_settings(self.db)

    def note(self, level: str, text: str) -> None:
        self.flash.append((level, text))
        self.flash = self.flash[-6:]

    def busy(self) -> bool:
        return self.task is not None and not self.task.done()


def _fmt_pct(v: Any, digits: int = 1) -> str:
    if v is None or v != v:
        return "—"
    return f"{100 * v:+.{digits}f}%"


def _fmt_x(v: Any) -> str:
    return "—" if v is None else (f"{v:,.1f}x" if v < 100 else f"{v:,.0f}x")


def _fmt_money(v: Any) -> str:
    return "—" if v is None else f"${v:,.2f}"


def create_app(db: Database, env: str | None = None) -> FastAPI:
    env = env or load_env_settings().kalshi_env
    app = FastAPI(title="combo-edge", docs_url=None, redoc_url=None)
    state = AppState(db, env)
    app.state.ce = state
    templates = Jinja2Templates(directory=str(HERE / "templates"))
    templates.env.filters.update(pct=_fmt_pct, x=_fmt_x, money=_fmt_money, hours=timeutil.fmt_hours,
                                 when=lambda t: timeutil.fmt_local(t), when_short=lambda t: timeutil.fmt_local(t, "%m/%d %H:%M"))
    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")

    def ctx(request: Request, tab: str, **extra: Any) -> dict[str, Any]:
        st = state.settings()
        flash, state.flash = state.flash, []
        return {"request": request, "tab": tab, "h": data.header(db, state.env, st), "st": st, "progress": state.progress.as_dict(),
                "busy": state.busy(), "flash": flash, "envs": ["demo", "prod", "mock"], **extra}

    # ------------------------------------------------------------------ pages
    @app.get("/", response_class=HTMLResponse)
    async def scan_page(request: Request, family: str = "", team: str = "", max_hours: str = "", sort: str = "ev") -> HTMLResponse:
        mh = float(max_hours) if max_hours else None
        res = data.results(db, state.env, family, team, mh, sort)
        return templates.TemplateResponse(request, "scan.html", ctx(request, "scan", res=res, summary=data.run_summary(res["run"]), shop=db.meta_get("last_shop"),
                                                                    f={"family": family, "team": team, "max_hours": max_hours, "sort": sort}))

    @app.get("/bets", response_class=HTMLResponse)
    async def bets_page(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(request, "bets.html", ctx(request, "bets", b=data.bets(db, state.env, state.settings()),
                                                                    recheck=state.bg_results.get("recheck")))

    @app.get("/learning", response_class=HTMLResponse)
    async def learning_page(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(request, "learning.html", ctx(request, "learning", l=data.learning(db, state.env, state.settings())))

    @app.get("/health", response_class=HTMLResponse)
    async def health_page(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(request, "health.html", ctx(request, "health", hl=data.health(db, state.env, state.settings())))

    # ------------------------------------------------------------------ partials
    @app.get("/partials/progress", response_class=HTMLResponse)
    async def progress_partial(request: Request) -> HTMLResponse:
        busy = state.busy()
        resp = templates.TemplateResponse(request, "partials/progress.html", {"request": request, "progress": state.progress.as_dict(), "busy": busy, "final": not busy})
        if not busy:
            resp.status_code = 286  # htmx: stop polling; the final render refreshes the results once
        return resp

    @app.get("/partials/results", response_class=HTMLResponse)
    async def results_partial(request: Request, family: str = "", team: str = "", max_hours: str = "", sort: str = "ev") -> HTMLResponse:
        mh = float(max_hours) if max_hours else None
        res = data.results(db, state.env, family, team, mh, sort)
        return templates.TemplateResponse(request, "partials/results.html", {"request": request, "res": res, "summary": data.run_summary(res["run"]),
                                                                             "st": state.settings(), "h": data.header(db, state.env, state.settings())})

    @app.get("/api/chart/{name}")
    async def chart(name: str) -> JSONResponse:
        return JSONResponse(data.chart_payload(name, db, state.env, state.settings()))

    @app.get("/api/progress")
    async def api_progress() -> JSONResponse:
        return JSONResponse(state.progress.as_dict())

    # ------------------------------------------------------------------ actions
    @app.post("/scan/run")
    async def scan_run(stake: float = Form(None), rfq_budget: int = Form(None), quote_window_s: float = Form(None), dry_run: str = Form("")) -> RedirectResponse:
        if state.busy():
            state.note("warn", "A scan is already running.")
            return RedirectResponse("/", 303)
        if execution.is_killed(db):
            state.note("error", "Kill switch is on. Clear it in Health before scanning.")
            return RedirectResponse("/", 303)
        changes = {k: v for k, v in {"stake": stake, "rfq_budget": rfq_budget, "quote_window_s": quote_window_s}.items() if v is not None}
        if changes:
            state.settings().merged(changes)  # validates
            db.settings_set(changes)
        state.abort = asyncio.Event()
        req = ScanRequest(env=state.env, dry_run=bool(dry_run), trigger="button")

        async def go() -> None:
            from combo_edge.hooks import default_hooks

            try:
                await run_scan(req, db=db, progress=state.progress, abort=state.abort, hooks=default_hooks())
            except Exception as exc:  # noqa: BLE001
                state.note("error", f"Scan failed: {exc}")

        state.task = asyncio.create_task(go())
        return RedirectResponse("/", 303)

    @app.post("/scan/stop")
    async def scan_stop() -> RedirectResponse:
        state.abort.set()
        state.note("warn", "Stopping: open RFQs are being cancelled.")
        return RedirectResponse("/", 303)

    @app.post("/settings")
    async def save_settings(request: Request) -> RedirectResponse:
        form = await request.form()
        changes: dict[str, Any] = {}
        for k, cast in EDITABLE.items():
            v = form.get(k)
            if v not in (None, ""):
                changes[k] = cast(v)
        try:
            state.settings().merged(changes)
            db.settings_set(changes)
            state.note("ok", "Settings saved.")
        except Exception as exc:  # noqa: BLE001
            state.note("error", f"Invalid setting: {exc}")
        return RedirectResponse(str(form.get("back") or "/"), 303)

    @app.post("/env")
    async def switch_env(env: str = Form(...), confirm: str = Form("")) -> RedirectResponse:
        if state.busy():
            state.note("warn", "Wait for the running scan to finish before switching environments.")
        elif env == "prod" and confirm != "prod":
            state.note("error", "Type 'prod' to switch to production.")
        elif env in ("demo", "prod", "mock"):
            state.env = env
            state.note("ok", f"Environment: {env.upper()}")
        return RedirectResponse("/", 303)

    @app.post("/kill")
    async def kill_switch() -> RedirectResponse:
        execution.kill(db, "kill switch pressed in the dashboard")
        state.abort.set()
        db.settings_set({"requote_enabled": False, "fullscan_enabled": False, "fullscan_early_week": False})
        try:
            from combo_edge.context import make_clients
            from combo_edge.probe import cleanup_our_open_rfqs

            clients = make_clients(state.env, db)
            if clients.authenticated:
                n = await cleanup_our_open_rfqs(clients.api, db)
                state.note("error", f"KILL SWITCH ON: execution off, scan stopped, {n} open RFQs cancelled, schedulers paused.")
            await clients.aclose()
        except Exception as exc:  # noqa: BLE001
            state.note("error", f"Kill switch on; RFQ cleanup failed: {exc}")
        return RedirectResponse("/health", 303)

    @app.post("/kill/clear")
    async def kill_clear() -> RedirectResponse:
        execution.clear_kill(db)
        state.note("ok", "Kill switch cleared. Execution stays OFF until you choose a mode.")
        return RedirectResponse("/health", 303)

    @app.post("/execution/mode")
    async def exec_mode(mode: str = Form(...), confirmation: str = Form("")) -> RedirectResponse:
        try:
            execution.set_mode(db, mode, state.env, confirmation, state.settings())
            state.note("ok" if mode != "live" else "error", f"Execution mode: {mode.upper()}")
        except execution.ExecutionError as exc:
            state.note("error", str(exc))
        return RedirectResponse("/bets", 303)

    @app.post("/bets/recheck")
    async def bets_recheck() -> RedirectResponse:
        async def go() -> None:
            from combo_edge.requote import recheck_positions

            try:
                state.bg_results["recheck"] = await recheck_positions(db, state.env)
            except Exception as exc:  # noqa: BLE001
                state.note("error", f"Re-check failed: {exc}")

        state.bg["recheck"] = asyncio.create_task(go())
        state.note("ok", "Re-checking your combos (RFQ per position, cancelled after the window)…")
        return RedirectResponse("/bets", 303)

    @app.post("/shop")
    async def shop(signature: str = Form(...)) -> RedirectResponse:
        async def go() -> None:
            from combo_edge.shopping import shop_signature

            try:
                state.bg_results[f"shop:{signature}"] = await shop_signature(db, state.env, signature)
                db.meta_set("last_shop", state.bg_results[f"shop:{signature}"])
            except Exception as exc:  # noqa: BLE001
                state.note("error", f"Quote shopping failed: {exc}")

        state.bg[f"shop:{signature}"] = asyncio.create_task(go())
        state.note("ok", "Shopping that combo: 3 RFQs a few seconds apart. Results appear on this page.")
        return RedirectResponse("/", 303)

    @app.post("/toggle/{what}")
    async def toggle(what: str, on: str = Form("")) -> RedirectResponse:
        enable = on == "1"
        from combo_edge.scheduler import launchd

        try:
            if what == "requote":
                db.settings_set({"requote_enabled": enable})
                launchd.set_agent("requote", enable)
            elif what == "recorder":
                launchd.set_agent("recorder", enable)
            elif what in ("fullscan", "fullscan_early"):
                db.settings_set({"fullscan_enabled" if what == "fullscan" else "fullscan_early_week": enable})
                st = state.settings()
                # one launchd agent serves both modes: keep it while either is on
                launchd.set_agent("fullscan", st.fullscan_enabled or st.fullscan_early_week)
            state.note("ok", f"{what.replace('_', ' ')}: {'ON' if enable else 'OFF'}")
        except Exception as exc:  # noqa: BLE001
            state.note("error", f"{what} toggle failed: {exc}")
        return RedirectResponse("/health", 303)

    @app.post("/upgrade-tier")
    async def upgrade() -> RedirectResponse:
        from combo_edge.cli import upgrade_tier

        ok, msg = await upgrade_tier(state.env, db)
        state.note("ok" if ok else "error", msg)
        return RedirectResponse("/health", 303)

    @app.post("/exploration/finish")
    async def finish_exploration() -> RedirectResponse:
        from combo_edge.learning.report import write_exploration_report

        path = write_exploration_report(db, state.env, state.settings())
        db.settings_set({"exploration_mode": False})
        state.note("ok", f"Exploration report written to {path}.")
        return RedirectResponse("/learning", 303)

    return app
