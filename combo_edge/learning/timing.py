"""The timing and liquidity map: where edge concentrates by hours-to-start bucket, leg liquidity, day of
week and time of day. Drives the "when should I press Run" recommendation (and later the scheduler)."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from combo_edge import timeutil
from combo_edge.learning.labels import HOUR_BUCKETS, edge_labels, mean_ci
from combo_edge.storage import Database

DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
MIN_N = 20


def recommendation(labels: list[dict[str, Any]]) -> dict[str, Any] | None:
    by_day: dict[str, list[float]] = defaultdict(list)
    by_hours: dict[str, list[float]] = defaultdict(list)
    for l in labels:
        e = max(-1.0, min(3.0, float(l["edge"])))
        by_day[l.get("dow") or "?"].append(e)
        by_hours[l.get("hours_bucket") or "?"].append(e)
    days = [(d, mean_ci(v)) for d, v in by_day.items() if len(v) >= MIN_N and d in DAYS]
    hrs = [(h, mean_ci(v)) for h, v in by_hours.items() if len(v) >= MIN_N]
    if len(days) < 2 and len(hrs) < 2:
        return None
    parts, detail = [], []
    if len(days) >= 2:
        days.sort(key=lambda x: -x[1][0])
        top = days[0][1][0]
        tol = max(0.03, 0.25 * abs(top))
        best = sorted([d for d, v in days if v[0] >= top - tol or (v[2] == v[2] and v[2] >= top)][:3], key=DAYS.index)
        avg = sum(v[0] for d, v in days if d in best) / len(best)
        span = best[0] if len(best) == 1 else f"{best[0]}–{best[-1]}"
        parts.append(f"edge peaks {span}, avg {100 * avg:+.0f}%")
        detail.append(", ".join(f"{d} {100 * v[0]:+.0f}% (n={v[3]})" for d, v in sorted(days, key=lambda x: DAYS.index(x[0]))))
    if len(hrs) >= 2:
        order = [b[2] for b in HOUR_BUCKETS]
        hrs.sort(key=lambda x: -x[1][0])
        h, (m, lo, hi, n) = hrs[0]
        parts.append(f"best {h} before kickoff ({100 * m:+.0f}%)")
        detail.append(", ".join(f"{b} {100 * v[0]:+.0f}%" for b, v in sorted(hrs, key=lambda x: order.index(x[0]) if x[0] in order else 99)))
    return {"headline": "; ".join(parts), "detail": " · ".join(detail), "ts": timeutil.now(), "n": len(labels)}


def update_recommendation(db: Database, env: str) -> dict[str, Any] | None:
    rec = recommendation(edge_labels(db, env))
    if rec is not None:
        db.meta_set(f"run_recommendation:{env}", rec)
    return rec
