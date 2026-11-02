"""HTTP allowlist. Every Kalshi REST call passes ``check()`` before any network I/O.

Hard rules enforced here and by ``tests/test_safety.py``:
- Only read endpoints, combo create/resolve, RFQ create/delete and the tier upgrade are allowed.
- The single order-type call (accepting an RFQ quote) is NOT listed in this file. ``execution.py``
  registers it at import time together with a private capability token, and the REST client only sends
  it when that exact token is presented. No other module can construct a valid capability.
- Resting orders, order groups, block trades, RFQ batches, quote creation and quote confirmation are
  never allowed.
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass


class ForbiddenEndpoint(Exception):
    """Raised before any I/O when code tries to call an endpoint outside the allowlist."""


@dataclass(frozen=True)
class Route:
    method: str
    pattern: re.Pattern[str]


def _r(method: str, rx: str) -> Route:
    return Route(method, re.compile(rx))


SEG = r"[^/]+"

ALLOWED: tuple[Route, ...] = (
    _r("GET", r"^/exchange/status$"),
    _r("GET", r"^/milestones$"),
    _r("GET", rf"^/milestones/{SEG}$"),
    _r("GET", r"^/events$"),
    _r("GET", r"^/events/multivariate$"),
    _r("GET", rf"^/events/{SEG}$"),
    _r("GET", r"^/markets$"),
    _r("GET", r"^/markets/candlesticks$"),
    _r("GET", r"^/markets/trades$"),
    _r("GET", rf"^/markets/{SEG}$"),
    _r("GET", r"^/series$"),
    _r("GET", rf"^/series/{SEG}$"),
    _r("GET", r"^/multivariate_event_collections$"),
    _r("GET", rf"^/multivariate_event_collections/{SEG}$"),
    _r("GET", r"^/communications/id$"),
    _r("GET", r"^/communications/rfqs$"),
    _r("GET", rf"^/communications/rfqs/{SEG}$"),
    _r("GET", rf"^/communications/rfqs/{SEG}/quotes/{SEG}$"),
    _r("GET", r"^/communications/quotes$"),
    _r("GET", r"^/account/limits$"),
    _r("GET", r"^/account/endpoint_costs$"),
    _r("GET", r"^/account/api_usage_level/volume_progress$"),
    _r("GET", r"^/portfolio/positions$"),
    _r("GET", r"^/portfolio/fills$"),
    _r("GET", r"^/portfolio/settlements$"),
    _r("GET", r"^/portfolio/balance$"),
    _r("POST", rf"^/multivariate_event_collections/{SEG}$"),
    _r("POST", r"^/communications/rfqs$"),
    _r("POST", r"^/account/api_usage_level/upgrade$"),
    _r("DELETE", rf"^/communications/rfqs/{SEG}$"),
)

_privileged: dict[tuple[str, str], tuple[Route, object]] = {}
_lock = threading.Lock()


def register_privileged(method: str, pattern: str, token: object, owner_module: str) -> None:
    """Register a privileged route. Only ``combo_edge.execution`` may do this, exactly once."""
    if owner_module != "combo_edge.execution":
        raise ForbiddenEndpoint(f"{owner_module} may not register privileged routes")
    key = (method.upper(), pattern)
    with _lock:
        existing = _privileged.get(key)
        if existing is not None and existing[1] is not token:
            raise ForbiddenEndpoint("privileged route already registered with a different capability")
        _privileged[key] = (_r(method.upper(), pattern), token)


def check(method: str, path: str, capability: object | None = None) -> str:
    """Validate ``method path`` (path relative to /trade-api/v2, no query). Returns 'public' or 'privileged'."""
    method = method.upper()
    if "?" in path:
        raise ForbiddenEndpoint("pass query parameters separately, not in the path")
    for route in ALLOWED:
        if route.method == method and route.pattern.match(path):
            if capability is not None:
                raise ForbiddenEndpoint("capabilities are only valid on privileged routes")
            return "public"
    with _lock:
        entries = list(_privileged.values())
    for route, token in entries:
        if route.method == method and route.pattern.match(path):
            if capability is None or capability is not token:
                raise ForbiddenEndpoint(f"{method} {path} requires the execution capability")
            return "privileged"
    raise ForbiddenEndpoint(f"{method} {path} is not on the allowlist")
