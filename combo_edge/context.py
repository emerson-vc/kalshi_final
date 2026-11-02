"""Per-environment client wiring: credentials -> signer, limiter, REST client, websocket connector.

``env == "mock"`` routes everything to the in-process fake exchange (no network at all).
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

import httpx

from combo_edge import timeutil
from combo_edge.kalshi.api import KalshiAPI
from combo_edge.kalshi.auth import Signer
from combo_edge.kalshi.limiter import Limiter
from combo_edge.kalshi.rest import RestClient
from combo_edge.kalshi.ws import Connector, KalshiWS
from combo_edge.settings import CredentialError, credentials_for, data_dir, load_env_settings
from combo_edge.storage import Database, open_db
from combo_edge.storage import schema as S

log = logging.getLogger(__name__)


class HttpStats:
    """Counts requests by endpoint and status in memory; flushed to ``http_stats`` per run."""

    def __init__(self, db: Database, env: str):
        self.db = db
        self.env = env
        self.counts: Counter[tuple[str, int]] = Counter()

    def __call__(self, label: str, status: int) -> None:
        self.counts[(label, status)] += 1

    def flush(self) -> None:
        if not self.counts:
            return
        day = timeutil.local_day(timeutil.now())
        for (label, status), n in self.counts.items():
            self.db.execute(
                "INSERT INTO http_stats(day, env, endpoint, status, n) VALUES(:d,:e,:p,:s,:n) "
                "ON CONFLICT(day, env, endpoint, status) DO UPDATE SET n = n + excluded.n",
                {"d": day, "e": self.env, "p": label, "s": status, "n": n},
            )
        self.counts.clear()


@dataclass
class EnvClients:
    env: str
    api: KalshiAPI
    signer: Signer | None
    limiter: Limiter
    ws_connector: Connector | None
    stats: HttpStats
    credentials_error: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def authenticated(self) -> bool:
        return self.signer is not None

    def websocket(self, subscriptions: list[dict[str, Any]], name: str) -> KalshiWS:
        return KalshiWS(self.env, self.signer, subscriptions, connector=self.ws_connector, name=name)

    async def configure_limits(self, db: Database, run_id: int | None = None, phase: str = "startup") -> dict[str, Any] | None:
        costs = None
        limits = None
        try:
            costs = await self.api.endpoint_costs()
        except Exception as exc:  # noqa: BLE001
            log.warning("endpoint_costs unavailable: %s", exc)
        if self.authenticated:
            try:
                limits = await self.api.account_limits()
            except Exception as exc:  # noqa: BLE001
                log.warning("account limits unavailable: %s", exc)
        self.limiter.configure(limits, costs)
        if limits is not None:
            log_limits(db, self.env, run_id, phase, limits)
        return limits

    async def aclose(self) -> None:
        self.stats.flush()
        await self.api.aclose()


def log_limits(db: Database, env: str, run_id: int | None, phase: str, limits: dict[str, Any]) -> None:
    db.insert(S.limits_log, {"ts": timeutil.now(), "env": env, "run_id": run_id, "phase": phase, "usage_tier": limits.get("usage_tier"), "body": limits})


def default_db() -> Database:
    return open_db(data_dir() / "combo_edge.sqlite")


def _mock_signer() -> Signer:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    pem = Ed25519PrivateKey.generate().private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    return Signer("mock-key", pem)


def make_clients(env: str, db: Database, utilization: float = 0.6) -> EnvClients:
    es = load_env_settings()
    signer: Signer | None = None
    cred_error: str | None = None
    transport: httpx.AsyncBaseTransport | None = None
    connector: Connector | None = None
    if env == "mock":
        from combo_edge.mock.exchange import get_mock

        mock = get_mock()
        signer = _mock_signer()
        transport = httpx.MockTransport(mock.handle_http)
        connector = mock.ws_connect
    else:
        try:
            cred = credentials_for(env, es)
            if cred is not None:
                signer = Signer(cred.key_id, cred.private_key_pem)
        except CredentialError as exc:
            cred_error = str(exc)
        except Exception as exc:  # noqa: BLE001 - never echo key material
            cred_error = f"could not load the {env} private key ({type(exc).__name__}); check the PEM file"
    limiter = Limiter(utilization=utilization, authenticated=signer is not None)
    stats = HttpStats(db, env)
    rest = RestClient(env, signer, limiter, transport=transport, on_stat=stats)
    return EnvClients(env, KalshiAPI(rest), signer, limiter, connector, stats, cred_error)
