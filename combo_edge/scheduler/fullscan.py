"""Optional full-scan scheduler (Phase 3). OFF by default; turn it on with the Health-tab toggles.

The launchd agent calls ``fullscan_tick`` every 30 minutes. A tick runs the same ``run_scan()`` as the
Run button when:
- full-scan mode: new player-prop markets have opened for an upcoming game since the last tick (the
  "heavy sweep when lines first open"), or the timing map recommends the current day and no scan has
  run for 12 hours;
- early-week mode: it is Tue-Thu between 9am and 9pm local, player props are open, and no early-week
  scan has run today (at most one a day; props open ~92 h before kickoff, when quotes look softest).
Execution inside that scan follows the execution mode (paper by default).
"""

from __future__ import annotations

import logging
from typing import Any

from combo_edge import timeutil

log = logging.getLogger(__name__)
PROP_SERIES = ("KXNFLPASSTDS", "KXNFLPASSYDS", "KXNFLRECYDS", "KXNFLTD", "KXNFLRSHYDS", "KXNFLREC")


async def prop_events(api: Any) -> list[str]:
    """Player-prop events open for upcoming games."""
    from combo_edge.universe import game_from_milestone

    out = []
    for ms in await api.milestones(type="football_game", minimum_start_date=timeutil.iso(timeutil.now())):
        g = game_from_milestone(ms)
        if g is None:
            continue
        out.extend(et for et in g.related_events if et.split("-")[0] in PROP_SERIES)
    return out


async def new_prop_events(api: Any, seen: set[str]) -> list[str]:
    return [et for et in await prop_events(api) if et not in seen]


def early_week_due(st: Any, now: float, last_day: str | None) -> bool:
    """Tue-Thu, inside the hour window, and not yet run today (local time)."""
    local = timeutil.local_dt(now)
    lo, hi = st.fullscan_early_hours
    return (st.fullscan_early_week and local.strftime("%a") in st.fullscan_early_days and lo <= local.hour < hi
            and last_day != local.date().isoformat())


async def fullscan_tick(env: str, now: float | None = None) -> dict[str, Any]:
    from combo_edge import execution
    from combo_edge.context import default_db, make_clients
    from combo_edge.hooks import default_hooks
    from combo_edge.scan import ScanRequest, run_scan, runtime_settings

    db = default_db()
    st = runtime_settings(db)
    now = now or timeutil.now()
    if not st.fullscan_enabled and not st.fullscan_early_week:
        return {"status": "disabled"}
    if execution.is_killed(db):
        return {"status": "kill_switch"}
    early_key = f"fullscan_early_last:{env}"
    early = early_week_due(st, now, db.meta_get(early_key))
    if not st.fullscan_enabled and not early:
        return {"status": "outside_early_week_window"}
    seen = set(db.meta_get(f"fullscan_seen:{env}", []) or [])
    clients = make_clients(env, db)
    try:
        props = await prop_events(clients.api)
    finally:
        await clients.aclose()
    fresh = [et for et in props if et not in seen]
    if early and props:
        db.meta_set(early_key, timeutil.local_dt(now).date().isoformat())
        db.meta_set(f"fullscan_seen:{env}", sorted(seen | set(fresh)))
        res = await run_scan(ScanRequest(env=env, trigger="scheduler:early_week"), db=db, hooks=default_hooks())
        return {"status": res.status, "run_id": res.run_id, "mode": "early_week", "prop_events": len(props)}
    if not st.fullscan_enabled:
        return {"status": "no_props_open"}
    last = db.scalar("SELECT MAX(started_at) FROM runs WHERE env=:e AND kind='scan'", {"e": env}, 0) or 0
    rec = db.meta_get(f"run_recommendation:{env}") or {}
    today = timeutil.local_dt(now).strftime("%a")
    recommended_now = today in (rec.get("headline") or "") and now - float(last) > 12 * 3600
    if not fresh and not recommended_now:
        return {"status": "nothing_new"}
    db.meta_set(f"fullscan_seen:{env}", sorted(seen | set(fresh)))
    res = await run_scan(ScanRequest(env=env, trigger="scheduler"), db=db, hooks=default_hooks())
    return {"status": res.status, "run_id": res.run_id, "new_prop_events": len(fresh)}
