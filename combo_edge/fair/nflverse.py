"""nflverse data (free) via ``nflreadpy``, cached as parquet under ``data/nflverse``.

Loaders refresh at most once per ``max_age_h``. Every loader degrades gracefully offline: it returns the
cached file if present, else None, and callers fall back (e.g. heuristic positions).
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

from combo_edge.settings import data_dir

log = logging.getLogger(__name__)


def cache_dir() -> Path:
    p = data_dir() / "nflverse"
    p.mkdir(parents=True, exist_ok=True)
    return p


def current_season() -> int:
    try:
        import nflreadpy

        return int(nflreadpy.get_current_season())
    except Exception:  # noqa: BLE001
        t = time.localtime()
        return t.tm_year if t.tm_mon >= 8 else t.tm_year - 1


def _load(name: str, seasons: list[int] | None, max_age_h: float, fetch: Any) -> Any:
    import polars as pl

    tag = "all" if seasons is None else "_".join(str(s) for s in sorted(seasons))
    path = cache_dir() / f"{name}_{tag}.parquet"
    fresh = path.exists() and (time.time() - path.stat().st_mtime) < max_age_h * 3600
    if fresh:
        return pl.read_parquet(path)
    try:
        df = fetch(seasons)
        df.write_parquet(path)
        return df
    except Exception as exc:  # noqa: BLE001 - offline or upstream change
        log.warning("nflverse %s download failed (%s); using cache if any", name, exc)
        return pl.read_parquet(path) if path.exists() else None


def rosters_weekly(seasons: list[int] | None = None, max_age_h: float = 24.0) -> Any:
    import nflreadpy

    return _load("rosters_weekly", seasons or [current_season()], max_age_h, lambda s: nflreadpy.load_rosters_weekly(s))


def player_stats(seasons: list[int], max_age_h: float = 24.0) -> Any:
    import nflreadpy

    return _load("player_stats_week", seasons, max_age_h, lambda s: nflreadpy.load_player_stats(s, summary_level="week"))


def schedules(seasons: list[int] | None = None, max_age_h: float = 24.0) -> Any:
    import nflreadpy

    return _load("schedules", seasons, max_age_h, lambda s: nflreadpy.load_schedules(s if s else True))


def pbp(seasons: list[int], max_age_h: float = 24.0 * 7) -> Any:
    import nflreadpy

    return _load("pbp", seasons, max_age_h, lambda s: nflreadpy.load_pbp(s))


def roster_index(max_age_h: float = 24.0) -> dict[tuple[str, str], tuple[str, str]] | None:
    """(nflverse team, jersey) -> (full_name, position), from the latest week of the current season."""
    try:
        df = rosters_weekly(max_age_h=max_age_h)
    except Exception as exc:  # noqa: BLE001
        log.warning("roster load failed: %s", exc)
        return None
    if df is None or df.height == 0:
        return None
    cols = set(df.columns)
    need = {"team", "jersey_number", "full_name", "position", "week"}
    if not need <= cols:
        log.warning("roster columns changed: %s", sorted(cols))
        return None
    latest = df.sort("week").group_by(["team", "jersey_number"]).last()
    out: dict[tuple[str, str], tuple[str, str]] = {}
    for row in latest.iter_rows(named=True):
        jn = row.get("jersey_number")
        if jn is None or row.get("team") is None:
            continue
        out[(str(row["team"]), str(int(jn)) if str(jn).replace(".0", "").isdigit() else str(jn))] = (
            str(row.get("full_name") or ""),
            str(row.get("position") or ""),
        )
    return out
