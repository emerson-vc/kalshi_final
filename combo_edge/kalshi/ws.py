"""Authenticated Kalshi websocket with reconnect, sequence-gap detection and outage tracking.

Connect to ``wss://…/trade-api/ws/v2`` with signed headers (the handshake signs ``GET /trade-api/ws/v2``),
then ``{"id": n, "cmd": "subscribe", "params": {"channels": [...], ...}}``. Probes use the outage log
(``was_down_between``) to decide when to fall back to REST for quotes.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import random
import time
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from combo_edge.kalshi.auth import Signer
from combo_edge.kalshi.errors import NotConfigured
from combo_edge.settings import WS_SIGN_PATH, WS_URLS

log = logging.getLogger(__name__)


class WSConn(Protocol):
    async def send(self, message: str) -> None: ...
    async def recv(self) -> str: ...
    async def close(self) -> None: ...


Connector = Callable[[str, dict[str, str]], Awaitable[WSConn]]
Handler = Callable[[dict[str, Any]], Any]


class _WebsocketsAdapter:
    def __init__(self, ws: Any):
        self._ws = ws

    async def send(self, message: str) -> None:
        await self._ws.send(message)

    async def recv(self) -> str:
        msg = await self._ws.recv()
        return msg if isinstance(msg, str) else msg.decode()

    async def close(self) -> None:
        await self._ws.close()


async def real_connector(url: str, headers: dict[str, str]) -> WSConn:
    from websockets.asyncio.client import connect

    ws = await connect(url, additional_headers=headers, ping_interval=20, ping_timeout=20, open_timeout=15, max_size=2**23)
    return _WebsocketsAdapter(ws)


class KalshiWS:
    def __init__(
        self,
        env: str,
        signer: Signer | None,
        subscriptions: list[dict[str, Any]],
        connector: Connector | None = None,
        url: str | None = None,
        name: str = "ws",
    ):
        self.env = env
        self.signer = signer
        self.subscriptions = subscriptions
        self.connector = connector or real_connector
        self.url = url or WS_URLS[env]
        self.name = name
        self.handlers: list[Handler] = []
        self.connected = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._conn: WSConn | None = None
        self._stop = False
        self._cmd_id = 0
        self._last_seq: dict[int, int] = {}
        self.outages: list[tuple[float, float | None]] = []
        self.stats: dict[str, Any] = {"connects": 0, "disconnects": 0, "messages": 0, "gaps": 0, "errors": 0, "last_msg_ts": None, "last_error": None}

    def add_handler(self, fn: Handler) -> None:
        self.handlers.append(fn)

    async def start(self) -> None:
        if self.signer is None and self.env != "mock":
            raise NotConfigured("the Kalshi websocket requires API credentials")
        if self._task is None:
            self._stop = False
            self.outages.append((time.time(), None))  # down until the first connect
            self._task = asyncio.create_task(self._run(), name=f"kalshi-ws-{self.name}")

    async def wait_connected(self, timeout: float = 15.0) -> bool:
        try:
            await asyncio.wait_for(self.connected.wait(), timeout)
            return True
        except TimeoutError:
            return False

    async def stop(self) -> None:
        self._stop = True
        if self._conn is not None:
            try:
                await self._conn.close()
            except Exception:  # noqa: BLE001
                pass
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None
        self.connected.clear()

    def was_down_between(self, t0: float, t1: float) -> bool:
        for start, end in self.outages:
            if start <= t1 and (end is None or end >= t0):
                return True
        return False

    def _mark_down(self) -> None:
        if self.outages and self.outages[-1][1] is None:
            return
        self.outages.append((time.time(), None))
        if len(self.outages) > 500:
            self.outages = self.outages[-500:]

    def _mark_up(self) -> None:
        if self.outages and self.outages[-1][1] is None:
            start, _ = self.outages[-1]
            self.outages[-1] = (start, time.time())

    async def _run(self) -> None:
        delay = 1.0
        while not self._stop:
            try:
                headers = self.signer.headers("GET", WS_SIGN_PATH) if self.signer else {}
                self._conn = await self.connector(self.url, headers)
                self._last_seq.clear()
                for params in self.subscriptions:
                    self._cmd_id += 1
                    await self._conn.send(json.dumps({"id": self._cmd_id, "cmd": "subscribe", "params": params}))
                self.stats["connects"] += 1
                self.connected.set()
                self._mark_up()
                delay = 1.0
                while not self._stop:
                    raw = await self._conn.recv()
                    await self._dispatch(raw)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - any drop leads to reconnect
                self.stats["errors"] += 1
                self.stats["last_error"] = f"{type(exc).__name__}: {exc}"[:300]
                if not self._stop:
                    log.warning("%s websocket dropped: %s", self.name, self.stats["last_error"])
            finally:
                if self.connected.is_set():
                    self.stats["disconnects"] += 1
                self.connected.clear()
                self._mark_down()
                if self._conn is not None:
                    try:
                        await self._conn.close()
                    except Exception:  # noqa: BLE001
                        pass
                    self._conn = None
            if self._stop:
                break
            await asyncio.sleep(delay * (0.75 + 0.5 * random.random()))
            delay = min(30.0, delay * 2)

    async def _dispatch(self, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except ValueError:
            return
        self.stats["messages"] += 1
        self.stats["last_msg_ts"] = time.time()
        sid, seq = msg.get("sid"), msg.get("seq")
        if isinstance(sid, int) and isinstance(seq, int):
            last = self._last_seq.get(sid)
            if last is not None and seq != last + 1:
                self.stats["gaps"] += 1
            self._last_seq[sid] = seq
        if msg.get("type") == "error":
            self.stats["last_error"] = str(msg.get("msg"))[:300]
        for fn in list(self.handlers):
            try:
                res = fn(msg)
                if inspect.isawaitable(res):
                    await res
            except Exception:  # noqa: BLE001 - a bad handler must not kill the socket
                log.exception("%s handler failed", self.name)
