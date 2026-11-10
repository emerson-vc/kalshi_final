"""``combo-edge`` command line. With no arguments it starts the dashboard and opens the browser.

    combo-edge                      dashboard at http://localhost:8000 (env from KALSHI_ENV, default demo)
    combo-edge scan [--dry-run] [--max-payout 40]   one headless scan (same run_scan() as the Run button)
    combo-edge doctor               check keys, connectivity, websocket auth, toolchain
    combo-edge limits               show your API tier and token buckets
    combo-edge upgrade-tier         one-time: request the Advanced API tier
    combo-edge kill                 kill switch: execution off, cancel this tool's open RFQs
    combo-edge recorder             24/7 RFQ-feed recorder (what the launchd agent runs)
    combo-edge requote-due          re-RFQ panel combos that are due (what the launchd agent runs)
    combo-edge fullscan-check       optional scheduler tick (off by default)
    combo-edge launchd {install,uninstall,status} {recorder,requote,fullscan}
    combo-edge nflverse-sync        download/refresh nflverse data
    combo-edge fit                  retrain the maker model and refit the fair model
    combo-edge exploration-report   write the Phase 1.5 report
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import platform
import sys
import threading
import webbrowser
from typing import Any

from combo_edge import __version__


def _setup_logging(verbose: bool = False) -> None:
    from logging.handlers import RotatingFileHandler

    from combo_edge.settings import data_dir

    logs = data_dir() / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr), RotatingFileHandler(logs / "combo_edge.log", maxBytes=5_000_000, backupCount=3)]
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", handlers=handlers)
    for noisy in ("httpx", "httpcore", "websockets", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _env(args: argparse.Namespace) -> str:
    from combo_edge.settings import load_env_settings

    return getattr(args, "env", None) or load_env_settings().kalshi_env


async def upgrade_tier(env: str, db: Any) -> tuple[bool, str]:
    """POST /account/api_usage_level/upgrade. Kalshi grants Advanced only if >= 1 of your last 100 orders came via the API."""
    from combo_edge.context import make_clients
    from combo_edge.kalshi.errors import Forbidden, KalshiError, NotConfigured

    clients = make_clients(env, db)
    try:
        if not clients.authenticated:
            return False, clients.credentials_error or f"no API credentials for {env}"
        before = (await clients.api.account_limits()).get("usage_tier")
        await clients.api.upgrade_usage_level()
        after = (await clients.api.account_limits()).get("usage_tier")
        return True, f"API tier: {before} -> {after}"
    except Forbidden:
        return False, ("Kalshi refused the upgrade (403): it grants Advanced only when at least 1 of your last 100 orders was placed "
                       "through the API. Quote accepts by this tool count as API orders, so this works after the first live execution.")
    except (KalshiError, NotConfigured) as exc:
        return False, f"upgrade failed: {exc}"
    finally:
        await clients.aclose()


def cmd_serve(args: argparse.Namespace) -> None:
    import uvicorn

    from combo_edge.context import default_db
    from combo_edge.settings import load_env_settings
    from combo_edge.web.app import create_app

    es = load_env_settings()
    env = _env(args)
    app = create_app(default_db(), env)
    host, port = es.combo_edge_host, args.port or es.combo_edge_port
    if host not in ("127.0.0.1", "localhost", "::1"):
        print(f"refusing to bind to {host}: the dashboard is localhost-only")
        host = "127.0.0.1"
    url = f"http://localhost:{port}"
    print(f"combo-edge {__version__} · env {env.upper()} · {url}")
    if not args.no_browser:
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host=host, port=port, log_level="warning")


def cmd_scan(args: argparse.Namespace) -> int:
    from combo_edge.context import default_db
    from combo_edge.hooks import default_hooks
    from combo_edge.ranking import filter_max_payout, parse_max_payout
    from combo_edge.scan import ScanProgress, ScanRequest, run_scan

    if args.max_payout is not None:
        try:
            parse_max_payout(args.max_payout)
        except ValueError as exc:
            print(exc)
            return 2
    prog = ScanProgress()
    req = ScanRequest(env=_env(args), dry_run=args.dry_run, rfq_budget=args.budget, max_new=args.max_new, stake=args.stake,
                      window_s=args.window, trigger="cli")
    res = asyncio.run(run_scan(req, db=default_db(), progress=prog, hooks=default_hooks()))
    print(f"run #{res.run_id}: {res.status}")
    s = res.summary
    if s and not s.get("dry_run"):
        fr = s.get("fill_rate")
        print(f"RFQs {s.get('rfqs_sent')} · quoted {s.get('quoted')} · fill rate {fr:.0%}" if fr is not None else f"RFQs {s.get('rfqs_sent')}")
    core = (s or {}).get("core") or []
    if core:
        verb = "planned" if s.get("dry_run") else "sent"
        print(f"core sweeps: {sum(r['selected'] for r in core)}/{sum(r['available'] for r in core)} rungs {verb}")
        for r in core:
            q = "" if s.get("dry_run") else f", {r['quoted']} quoted"
            print(f"  {r['game']}  {r['pair']}  ({r['label']}): {r['selected']}/{r['available']} {verb}{q}")
    rows = res.rows
    if args.max_payout is not None:
        rows = filter_max_payout(rows, parse_max_payout(args.max_payout))
        print(f"showing {len(rows)} of {len(res.rows)} rows at or under {parse_max_payout(args.max_payout):g}x")
    for r in rows[: args.top]:
        ev = f"{100 * r.ev:+.0f}%" if r.ev is not None else "  n/a"
        pay = f"{r.quoted_payout:.1f}x" if r.quoted_payout else "-"
        print(f"  {ev:>6}  {pay:>8}  ratio {r.ratio or 0:.2f}  [{r.pool}]  {r.legs}")
    for e in res.coverage.get("errors", []):
        print("COVERAGE ERROR:", e)
    return 2 if res.status == "coverage_error" else 0


def cmd_limits(args: argparse.Namespace) -> None:
    from combo_edge.context import default_db, make_clients

    async def go() -> None:
        c = make_clients(_env(args), default_db())
        try:
            if not c.authenticated:
                print(c.credentials_error or "no credentials")
                return
            lim = await c.api.account_limits()
            print("tier:", lim.get("usage_tier"))
            for b in ("read", "write"):
                print(f"  {b}: {lim.get(b)}")
            print("grants:", lim.get("grants"))
        finally:
            await c.aclose()

    asyncio.run(go())


def cmd_upgrade(args: argparse.Namespace) -> int:
    from combo_edge.context import default_db

    ok, msg = asyncio.run(upgrade_tier(_env(args), default_db()))
    print(msg)
    return 0 if ok else 1


def cmd_kill(args: argparse.Namespace) -> None:
    from combo_edge import execution
    from combo_edge.context import default_db, make_clients
    from combo_edge.probe import cleanup_our_open_rfqs

    db = default_db()
    execution.kill(db, "kill switch (CLI)")
    db.settings_set({"requote_enabled": False, "fullscan_enabled": False, "fullscan_early_week": False})

    async def go() -> int:
        c = make_clients(_env(args), db)
        try:
            return await cleanup_our_open_rfqs(c.api, db) if c.authenticated else 0
        finally:
            await c.aclose()

    print(f"kill switch ON · execution OFF · schedulers paused · cancelled {asyncio.run(go())} open RFQs")


def cmd_doctor(args: argparse.Namespace) -> int:
    from pathlib import Path

    from combo_edge.context import default_db, make_clients
    from combo_edge.settings import CredentialError, credentials_for, load_env_settings

    ok = True

    def line(good: bool | None, text: str) -> None:
        nonlocal ok
        mark = {True: "ok  ", False: "FAIL", None: "--  "}[good]
        if good is False:
            ok = False
        print(f"[{mark}] {text}")

    line(platform.machine() == "arm64", f"python {platform.python_version()} on {platform.machine()} ({sys.executable})")
    envfile = Path(os.environ.get("COMBO_EDGE_ENV_FILE") or ".env")
    if envfile.exists():
        names = []
        bad = 0
        for raw in envfile.read_text(errors="ignore").splitlines():
            s = raw.strip()
            if not s or s.startswith("#"):
                continue
            if "=" in s:
                names.append(s.split("=", 1)[0].strip())
            else:
                bad += 1
        line(bad == 0, f".env: {len(names)} variables ({', '.join(sorted(set(names))) or 'none'})" + (f"; {bad} line(s) are not NAME=value (see .env.example)" if bad else ""))
    else:
        line(None, ".env not found (copy .env.example)")
    es = load_env_settings()
    line(True, f"KALSHI_ENV={es.kalshi_env}")
    db = default_db()
    for env in ("demo", "prod"):
        try:
            cred = credentials_for(env, es)
            if cred is None:
                line(None, f"{env}: no credentials configured")
                continue
            from combo_edge.kalshi.auth import Signer

            s = Signer(cred.key_id, cred.private_key_pem)
            line(True, f"{env}: key {cred.masked_key_id} ({s.key_type})")
        except CredentialError as exc:
            line(False, f"{env}: {exc}")
            continue
        except Exception as exc:  # noqa: BLE001
            line(False, f"{env}: private key unreadable ({type(exc).__name__})")
            continue

        async def probe(env: str = env) -> None:
            c = make_clients(env, db)
            try:
                st = await c.api.exchange_status()
                line(True, f"{env}: REST reachable (exchange_active={st.get('exchange_active')})")
                lim = await c.api.account_limits()
                line(True, f"{env}: authenticated · tier {lim.get('usage_tier')} · write {lim.get('write')}")
                ws = c.websocket([{"channels": ["communications"], "user_filter": "self"}], "doctor")
                await ws.start()
                connected = await ws.wait_connected(10)
                line(connected, f"{env}: websocket {'authenticated and connected' if connected else 'failed: ' + str(ws.stats.get('last_error'))}")
                await ws.stop()
            except Exception as exc:  # noqa: BLE001
                line(False, f"{env}: {type(exc).__name__}: {exc}")
            finally:
                await c.aclose()

        asyncio.run(probe())
    from combo_edge import execution
    from combo_edge.quotes import current_side_mapping

    line(True, f"execution mode: {execution.get_mode(db)}" + (" (kill switch ON)" if execution.is_killed(db) else ""))
    for env in ("demo", "prod"):
        m = current_side_mapping(db, env)
        line(None if m is None else True, f"{env} side mapping: {m or 'not verified yet'}")
    try:
        import nflreadpy  # noqa: F401

        line(True, "nflreadpy importable")
    except Exception as exc:  # noqa: BLE001
        line(False, f"nflreadpy: {exc}")
    return 0 if ok else 1


def cmd_dispatch(args: argparse.Namespace) -> int:
    """Commands implemented in later-phase modules."""
    if args.cmd == "recorder":
        from combo_edge.recorder import run_recorder_forever

        asyncio.run(run_recorder_forever(_env(args)))
    elif args.cmd == "requote-due":
        from combo_edge.requote import requote_due_sync

        print(requote_due_sync(_env(args)))
    elif args.cmd == "fullscan-check":
        from combo_edge.scheduler.fullscan import fullscan_tick

        print(asyncio.run(fullscan_tick(_env(args))))
    elif args.cmd == "launchd":
        from combo_edge.scheduler import launchd

        if args.action == "status":
            print(launchd.status())
        else:
            launchd.set_agent(args.agent, args.action == "install")
            print(launchd.status())
    elif args.cmd == "nflverse-sync":
        from combo_edge.fair.history import sync_all

        print(sync_all())
    elif args.cmd == "fit":
        from combo_edge.learning.fit import fit_all

        print(fit_all(_env(args)))
    elif args.cmd == "exploration-report":
        from combo_edge.context import default_db
        from combo_edge.learning.report import write_exploration_report
        from combo_edge.scan import runtime_settings

        db = default_db()
        print(write_exploration_report(db, _env(args), runtime_settings(db)))
    return 0


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="combo-edge", description="Kalshi NFL combo edge scanner")
    p.add_argument("--env", choices=["demo", "prod", "mock"], help="override KALSHI_ENV")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd")
    s = sub.add_parser("serve")
    s.add_argument("--port", type=int)
    s.add_argument("--no-browser", action="store_true")
    sc = sub.add_parser("scan")
    sc.add_argument("--dry-run", action="store_true")
    sc.add_argument("--budget", type=int)
    sc.add_argument("--max-new", type=int)
    sc.add_argument("--stake", type=float)
    sc.add_argument("--window", type=float)
    sc.add_argument("--top", type=int, default=15)
    sc.add_argument("--max-payout", help="only show rows paying at most this much: 40, 40x or <40x")
    for name in ("doctor", "limits", "upgrade-tier", "kill", "recorder", "requote-due", "fullscan-check", "nflverse-sync", "fit", "exploration-report"):
        sub.add_parser(name)
    ld = sub.add_parser("launchd")
    ld.add_argument("action", choices=["install", "uninstall", "status"])
    ld.add_argument("agent", nargs="?", default="recorder", choices=["recorder", "requote", "fullscan"])
    args = p.parse_args(argv)
    os.environ.setdefault("PYTHONUNBUFFERED", "1")
    _setup_logging(args.verbose)
    if args.env:
        os.environ["KALSHI_ENV"] = args.env
    cmd = args.cmd or "serve"
    if cmd == "serve":
        if not hasattr(args, "port"):
            args.port, args.no_browser = None, False
        cmd_serve(args)
        return
    code = {"scan": cmd_scan, "limits": cmd_limits, "upgrade-tier": cmd_upgrade, "kill": cmd_kill, "doctor": cmd_doctor}.get(cmd, cmd_dispatch)(args)
    sys.exit(code or 0)


if __name__ == "__main__":
    main()
