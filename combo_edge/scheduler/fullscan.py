"""Optional full-scan scheduler (Phase 3). OFF by default; turn it on with the Health-tab toggle.

The launchd agent calls ``fullscan_tick`` every 30 minutes. A tick runs the same ``run_scan()`` as the
Run button when either new player-prop markets have opened for an upcoming game since the last tick
(the "heavy sweep when lines first open"), or the timing map recommends the current day and no scan
has run for 12 hours. Execution inside that scan follows the execution mode (paper by default).
"""

from __future__ import annotations

import logging
from typing import Any

from combo_edge import timeutil

log = logging.getLogger(__name__)
PROP_SERIES = ("KXNFLPASSTDS", "KXNFLPASSYDS", "KXNFLRECYDS", "KXNFLTD", "KXNFLRSHYDS", "KXNFLREC")


async def new_prop_events(api: Any, seen: set[str]) -> list[str]:
    from combo_edge.universe import game_from_milestone

    out = []
    for ms in await api.milestones(type="football_game", minimum_start_date=timeutil.iso(timeutil.now())):
        g = game_from_milestone(ms)
        if g is None:
            continue
        for et in g.related_events:
            if et.split("-")[0] in PROP_SERIES and et not in seen:
                out.append(et)
    return out


async def fullscan_tick(env: str) -> dict[str, Any]:
    from combo_edge import execution
    from combo_edge.context import default_db, make_clients
    from combo_edge.hooks import default_hooks
    from combo_edge.scan import ScanRequest, run_scan, runtime_settings

    db = default_db()
    st = runtime_settings(db)
    if not st.fullscan_enabled:
        return {"status": "disabled"}
    if execution.is_killed(db):
        return {"status": "kill_switch"}
    seen = set(db.meta_get(f"fullscan_seen:{env}", []) or [])
    clients = make_clients(env, db)
    try:
        fresh = await new_prop_events(clients.api, seen)
    finally:
        await clients.aclose()
    last = db.scalar("SELECT MAX(started_at) FROM runs WHERE env=:e AND kind='scan'", {"e": env}, 0) or 0
    rec = db.meta_get(f"run_recommendation:{env}") or {}
    today = timeutil.local_dt(timeutil.now()).strftime("%a")
    recommended_now = today in (rec.get("headline") or "") and timeutil.now() - float(last) > 12 * 3600
    if not fresh and not recommended_now:
        return {"status": "nothing_new"}
    db.meta_set(f"fullscan_seen:{env}", sorted(seen | set(fresh)))
    res = await run_scan(ScanRequest(env=env, trigger="scheduler"), db=db, hooks=default_hooks())
    return {"status": res.status, "run_id": res.run_id, "new_prop_events": len(fresh)}
