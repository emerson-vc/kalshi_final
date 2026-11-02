"""SQLite access: WAL mode, short transactions, small helper API.

Several processes share the file (dashboard, 24/7 recorder, launchd re-quote job), so every write is a
short transaction and connections use a generous busy timeout.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from sqlalchemy import Table, create_engine, event, text
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Engine

from combo_edge import timeutil
from combo_edge.storage import schema


def dumps(obj: Any) -> str:
    return json.dumps(obj, separators=(",", ":"), default=str)


def loads(value: str | None, default: Any = None) -> Any:
    if value is None or value == "":
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


class Database:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.engine: Engine = create_engine(
            f"sqlite:///{self.path}",
            connect_args={"check_same_thread": False, "timeout": 30},
            future=True,
        )
        event.listen(self.engine, "connect", self._on_connect)
        self._lock = threading.RLock()

    @staticmethod
    def _on_connect(dbapi_conn: Any, _record: Any) -> None:
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.execute("PRAGMA busy_timeout=30000")
        cur.close()

    def init_schema(self) -> None:
        schema.metadata.create_all(self.engine)
        if self.meta_get("schema_version") is None:
            self.meta_set("schema_version", schema.SCHEMA_VERSION)

    # ---------------------------------------------------------------- core helpers
    def execute(self, sql: str | Any, params: Mapping[str, Any] | Sequence[Mapping[str, Any]] | None = None) -> Any:
        stmt = text(sql) if isinstance(sql, str) else sql
        with self.engine.begin() as conn:
            return conn.execute(stmt, params or {})

    def fetchall(self, sql: str, params: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
        with self.engine.connect() as conn:
            res = conn.execute(text(sql), params or {})
            return [dict(r._mapping) for r in res]

    def fetchone(self, sql: str, params: Mapping[str, Any] | None = None) -> dict[str, Any] | None:
        rows = self.fetchall(sql, params)
        return rows[0] if rows else None

    def scalar(self, sql: str, params: Mapping[str, Any] | None = None, default: Any = None) -> Any:
        with self.engine.connect() as conn:
            v = conn.execute(text(sql), params or {}).scalar()
        return default if v is None else v

    def insert(self, table: Table, row: Mapping[str, Any]) -> int | None:
        with self.engine.begin() as conn:
            res = conn.execute(table.insert().values(**_clean(table, row)))
            pk = res.inserted_primary_key
            return pk[0] if pk else None

    def insert_many(self, table: Table, rows: Iterable[Mapping[str, Any]], ignore_conflicts: bool = False) -> int:
        batch = [_clean(table, r) for r in rows]
        if not batch:
            return 0
        stmt = sqlite_insert(table)
        if ignore_conflicts:
            stmt = stmt.on_conflict_do_nothing()
        with self.engine.begin() as conn:
            conn.execute(stmt, batch)
        return len(batch)

    def upsert(self, table: Table, row: Mapping[str, Any], keys: Sequence[str], update: Sequence[str] | None = None) -> None:
        clean = _clean(table, row)
        stmt = sqlite_insert(table).values(**clean)
        cols = update if update is not None else [c for c in clean if c not in keys]
        if cols:
            stmt = stmt.on_conflict_do_update(index_elements=list(keys), set_={c: stmt.excluded[c] for c in cols})
        else:
            stmt = stmt.on_conflict_do_nothing(index_elements=list(keys))
        with self.engine.begin() as conn:
            conn.execute(stmt)

    def upsert_many(self, table: Table, rows: Sequence[Mapping[str, Any]], keys: Sequence[str], update: Sequence[str]) -> None:
        if not rows:
            return
        batch = [_clean(table, r) for r in rows]
        stmt = sqlite_insert(table)
        stmt = stmt.on_conflict_do_update(index_elements=list(keys), set_={c: stmt.excluded[c] for c in update})
        with self.engine.begin() as conn:
            conn.execute(stmt, batch)

    def update(self, table: Table, where: Mapping[str, Any], values: Mapping[str, Any]) -> int:
        stmt = table.update()
        for k, v in where.items():
            stmt = stmt.where(table.c[k] == v)
        with self.engine.begin() as conn:
            return conn.execute(stmt.values(**_clean(table, values))).rowcount

    # ---------------------------------------------------------------- key/value tables
    def meta_get(self, key: str, default: Any = None) -> Any:
        row = self.fetchone("SELECT value FROM meta WHERE key=:k", {"k": key})
        return loads(row["value"], default) if row else default

    def meta_set(self, key: str, value: Any) -> None:
        self.upsert(schema.meta, {"key": key, "value": dumps(value), "updated_at": timeutil.now()}, ["key"])

    def risk_get(self, key: str, default: Any = None) -> Any:
        row = self.fetchone("SELECT value FROM risk_state WHERE key=:k", {"k": key})
        return loads(row["value"], default) if row else default

    def risk_set(self, key: str, value: Any) -> None:
        self.upsert(schema.risk_state, {"key": key, "value": dumps(value), "updated_at": timeutil.now()}, ["key"])

    def settings_overrides(self) -> dict[str, Any]:
        return {r["key"]: loads(r["value"]) for r in self.fetchall("SELECT key, value FROM settings")}

    def settings_set(self, changes: Mapping[str, Any]) -> None:
        now = timeutil.now()
        for k, v in changes.items():
            self.upsert(schema.settings, {"key": k, "value": dumps(v), "updated_at": now}, ["key"])

    def log_error(self, where: str, message: str, detail: Any = None) -> None:
        try:
            self.insert(
                schema.errors,
                {"ts": timeutil.now(), "where": where, "message": message[:2000], "detail": dumps(detail)[:8000] if detail is not None else None},
            )
        except Exception:  # noqa: BLE001 - error logging must never raise
            pass


def _clean(table: Table, row: Mapping[str, Any]) -> dict[str, Any]:
    cols = table.c
    out: dict[str, Any] = {}
    for k, v in row.items():
        if k not in cols:
            continue
        if isinstance(v, (dict, list, tuple)) and not isinstance(v, (bytes, bytearray)):
            v = dumps(v)
        elif isinstance(v, bool):
            v = int(v)
        out[k] = v
    return out


_db_cache: dict[str, Database] = {}
_db_lock = threading.Lock()


def open_db(path: str | Path) -> Database:
    """Process-wide cached Database per file path, with schema initialized."""
    key = str(Path(path).resolve())
    with _db_lock:
        db = _db_cache.get(key)
        if db is None:
            db = Database(path)
            db.init_schema()
            _db_cache[key] = db
        return db
