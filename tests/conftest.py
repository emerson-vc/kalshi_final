"""Test harness. Every test runs with:
- all non-loopback network blocked (so nothing can ever reach demo/prod, including quote accepts),
- a fresh temp data dir (SQLite, caches),
- a fresh mock exchange,
- Kalshi credential variables blanked and the .env file redirected, so a real .env can never leak into tests.
"""

from __future__ import annotations

import gzip
import json
import socket
from pathlib import Path
from typing import Any

import pytest

FIXTURES = Path(__file__).parent / "fixtures" / "live"
_real_connect = socket.socket.connect
_real_create_connection = socket.create_connection


def _is_loopback(address: Any) -> bool:
    host = address[0] if isinstance(address, tuple) else address
    return isinstance(host, str) and (host in {"127.0.0.1", "::1", "localhost"} or host.startswith("/"))


@pytest.fixture(autouse=True)
def _block_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def guarded_connect(self: socket.socket, address: Any) -> Any:
        if not _is_loopback(address):
            raise RuntimeError(f"tests may not open network connections (tried {address!r})")
        return _real_connect(self, address)

    def guarded_create_connection(address: Any, *args: Any, **kwargs: Any) -> Any:
        if not _is_loopback(address):
            raise RuntimeError(f"tests may not open network connections (tried {address!r})")
        return _real_create_connection(address, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket, "create_connection", guarded_create_connection)


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("COMBO_EDGE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("COMBO_EDGE_ENV_FILE", str(tmp_path / "no.env"))  # never parse the real .env
    monkeypatch.setenv("KALSHI_ENV", "mock")
    monkeypatch.setenv("COMBO_EDGE_FORBID_LIVE", "1")
    for env in ("DEMO", "PROD"):
        for suffix in ("API_KEY_ID", "PRIVATE_KEY_PATH", "PRIVATE_KEY_PEM"):
            monkeypatch.setenv(f"KALSHI_{env}_{suffix}", "")
    monkeypatch.setenv("KALSHI_API_KEY_ID", "")
    monkeypatch.setenv("KALSHI_PRIVATE_KEY_PATH", "")
    return tmp_path


@pytest.fixture
def mock_exchange():  # type: ignore[no-untyped-def]
    from combo_edge.mock.exchange import reset_mock

    return reset_mock(seed=11)


@pytest.fixture
def db(_isolated_env: Path):  # type: ignore[no-untyped-def]
    from combo_edge.context import default_db

    return default_db()


def load_fixture(name: str) -> Any:
    with gzip.open(FIXTURES / f"{name}.json.gz", "rt") as f:
        return json.load(f)


@pytest.fixture
def live_events() -> dict[str, Any]:
    out: dict[str, Any] = {}
    for code in ("26NOV02CHISEA", "26NOV05JACBAL"):
        out.update(load_fixture(f"events_{code}"))
    return out


@pytest.fixture
def live_milestones() -> list[dict[str, Any]]:
    return load_fixture("milestones")["milestones"]
