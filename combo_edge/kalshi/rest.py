"""Async REST transport: allowlist check, signing, rate limiting, retries with backoff.

Retry policy per call:
- ``safe``   GET/DELETE: retry 429, 5xx and network errors with exponential backoff + jitter.
- ``429``    non-idempotent POSTs (create RFQ, create combo): retry only 429 (the request was refused).
- ``none``   never retried (quote accept: a retry loop could double-fill).
"""

from __future__ import annotations

import asyncio
import random
import re
from collections.abc import Callable
from typing import Any, Literal

import httpx

from combo_edge import safety
from combo_edge.kalshi.auth import Signer
from combo_edge.kalshi.errors import NotConfigured, from_response
from combo_edge.kalshi.limiter import Limiter
from combo_edge.settings import REST_BASES, REST_PATH_PREFIX

RetryPolicy = Literal["safe", "429", "none"]
_ID_SEG = re.compile(r"[A-Z0-9]|.{25,}")


def endpoint_label(method: str, path: str) -> str:
    parts = [":id" if (p and _ID_SEG.search(p)) else p for p in path.split("/")]
    return f"{method.upper()} " + "/".join(parts)


def _clean_params(params: dict[str, Any] | None) -> dict[str, Any] | None:
    if not params:
        return None
    out: dict[str, Any] = {}
    for k, v in params.items():
        if v is None:
            continue
        if isinstance(v, bool):
            out[k] = "true" if v else "false"
        elif isinstance(v, (list, tuple)):
            out[k] = ",".join(str(x) for x in v)
        else:
            out[k] = v
    return out


class RestClient:
    def __init__(
        self,
        env: str,
        signer: Signer | None,
        limiter: Limiter,
        transport: httpx.AsyncBaseTransport | None = None,
        base_url: str | None = None,
        on_stat: Callable[[str, int], None] | None = None,
        timeout: float = 20.0,
    ):
        self.env = env
        self.signer = signer
        self.limiter = limiter
        self.base = base_url or REST_BASES[env]
        self.on_stat = on_stat
        self._client = httpx.AsyncClient(
            transport=transport, timeout=timeout, headers={"User-Agent": "combo-edge/1.0", "Accept": "application/json"}
        )

    @property
    def authenticated(self) -> bool:
        return self.signer is not None

    async def aclose(self) -> None:
        await self._client.aclose()

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
        auth_required: bool = False,
        retry: RetryPolicy = "safe",
        capability: object | None = None,
        max_attempts: int = 5,
    ) -> Any:
        method = method.upper()
        safety.check(method, path, capability)  # raises ForbiddenEndpoint before any I/O
        if auth_required and self.signer is None:
            raise NotConfigured(f"{method} {path} needs API credentials for env '{self.env}'")
        url = self.base + path
        label = endpoint_label(method, path)
        attempt = 0
        while True:
            await self.limiter.acquire(method, path)
            headers = self.signer.headers(method, REST_PATH_PREFIX + path) if self.signer else {}
            try:
                resp = await self._client.request(method, url, params=_clean_params(params), json=json, headers=headers)
            except httpx.TransportError:
                if retry == "safe" and attempt + 1 < max_attempts:
                    attempt += 1
                    await asyncio.sleep(_backoff(attempt))
                    continue
                raise
            if self.on_stat:
                try:
                    self.on_stat(label, resp.status_code)
                except Exception:  # noqa: BLE001 - stats must never break a request
                    pass
            status = resp.status_code
            if status == 429:
                self.limiter.on_429(method, path, attempt)
                if retry in ("safe", "429") and attempt + 1 < max_attempts:
                    attempt += 1
                    await asyncio.sleep(_backoff(attempt))
                    continue
            elif status >= 500 and retry == "safe" and attempt + 1 < max_attempts:
                attempt += 1
                await asyncio.sleep(_backoff(attempt))
                continue
            body: Any = None
            if resp.content:
                try:
                    body = resp.json()
                except ValueError:
                    body = resp.text
            if status >= 400:
                raise from_response(status, body)
            return body


def _backoff(attempt: int) -> float:
    return min(10.0, 0.4 * (2 ** (attempt - 1))) * (0.7 + 0.6 * random.random())
