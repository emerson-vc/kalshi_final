"""Time helpers. All storage uses UTC epoch seconds (float)."""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone


def now() -> float:
    return time.time()


def parse_ts(value: str | float | int | None) -> float | None:
    """Parse an ISO-8601 string (Kalshi style, 'Z' or offset) or a number into epoch seconds."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    # Kalshi sometimes sends 7+ fractional digits; Python accepts at most 6.
    if "." in text:
        head, _, tail = text.partition(".")
        digits = ""
        rest = ""
        for i, ch in enumerate(tail):
            if ch.isdigit():
                digits += ch
            else:
                rest = tail[i:]
                break
        text = f"{head}.{digits[:6]}{rest}"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def hours_until(target_ts: float | None, now_ts: float | None = None) -> float | None:
    if target_ts is None:
        return None
    return (target_ts - (now_ts if now_ts is not None else now())) / 3600.0


def local_dt(ts: float) -> datetime:
    return datetime.fromtimestamp(ts).astimezone()


def local_day(ts: float) -> str:
    """Calendar day in the machine's local time zone (used for daily limits)."""
    return local_dt(ts).strftime("%Y-%m-%d")


def start_of_local_day(ts: float) -> float:
    d = local_dt(ts)
    return d.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def fmt_local(ts: float | None, fmt: str = "%a %b %d %I:%M %p") -> str:
    if ts is None:
        return "—"
    return local_dt(ts).strftime(fmt)


def fmt_hours(h: float | None) -> str:
    if h is None:
        return "—"
    if h < 0:
        return "started"
    if h < 1:
        return f"{h * 60:.0f}m"
    if h < 48:
        return f"{h:.1f}h"
    return f"{h / 24:.1f}d"


def days_ago(days: float, now_ts: float | None = None) -> float:
    return (now_ts if now_ts is not None else now()) - timedelta(days=days).total_seconds()
