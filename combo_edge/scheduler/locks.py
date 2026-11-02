"""Cross-process coordination.

- ``LeaseManager``: per-ticker leases in SQLite so the scan, the launchd re-quote job and the dashboard
  never hold two open RFQs on one market ticker (Kalshi allows only one; a duplicate returns 409).
- ``ProcessLock``: an exclusive flock file, e.g. one scan at a time.
"""

from __future__ import annotations

import fcntl
import os
from pathlib import Path

from sqlalchemy import text

from combo_edge import timeutil
from combo_edge.storage import Database


class LeaseManager:
    def __init__(self, db: Database, holder: str):
        self.db = db
        self.holder = holder

    def acquire(self, ticker: str, ttl_s: float = 90.0) -> bool:
        now = timeutil.now()
        with self.db.engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO rfq_leases(ticker, holder, expires_at) VALUES(:t, :h, :e) "
                    "ON CONFLICT(ticker) DO UPDATE SET holder=excluded.holder, expires_at=excluded.expires_at "
                    "WHERE rfq_leases.expires_at < :now OR rfq_leases.holder = excluded.holder"
                ),
                {"t": ticker, "h": self.holder, "e": now + ttl_s, "now": now},
            )
            row = conn.execute(text("SELECT holder FROM rfq_leases WHERE ticker=:t"), {"t": ticker}).first()
        return bool(row and row[0] == self.holder)

    def release(self, ticker: str) -> None:
        self.db.execute("DELETE FROM rfq_leases WHERE ticker=:t AND holder=:h", {"t": ticker, "h": self.holder})


class ProcessLock:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fd: int | None = None

    def acquire(self) -> bool:
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            return False
        os.ftruncate(fd, 0)
        os.write(fd, str(os.getpid()).encode())
        self._fd = fd
        return True

    def release(self) -> None:
        if self._fd is not None:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            finally:
                os.close(self._fd)
                self._fd = None

    def __enter__(self) -> "ProcessLock":
        if not self.acquire():
            raise RuntimeError(f"another process holds {self.path.name}")
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()
