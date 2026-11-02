"""Token-bucket rate limiter sized from GET /account/limits and GET /account/endpoint_costs.

Kalshi meters requests in token buckets (default 10 tokens per request). On Basic, Read refills at
200 tokens/s (3 s burst) and Write at 100 tokens/s with only a 1 s bucket. RFQ create/delete are writes.
We run at ``utilization`` (default 60%) of each bucket so we stay well under the cap, and back off
further after any 429.
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from typing import Any

# Basic-tier fallbacks used before /account/limits is read (or without credentials).
BASIC_DEFAULTS = {
    "read": (200.0, 600.0),
    "write": (100.0, 100.0),
    "balance": (10.0, 10.0),  # 1 call/s at 10 tokens
}
UNAUTH_READ = (40.0, 40.0)  # unauthenticated reads are IP-limited (~10 req/s); stay at ~4/s


class TokenBucket:
    def __init__(self, rate: float, capacity: float, name: str = ""):
        self.name = name
        self.rate = float(rate)
        self.capacity = max(float(capacity), 1.0)
        self.tokens = self.capacity
        self.updated = time.monotonic()
        self.penalty_until = 0.0
        self._lock = asyncio.Lock()
        self.waited_s = 0.0
        self.spent = 0.0

    def _refill(self, now: float) -> None:
        self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
        self.updated = now

    async def acquire(self, cost: float) -> None:
        cost = min(float(cost), self.capacity)
        async with self._lock:
            while True:
                now = time.monotonic()
                if now < self.penalty_until:
                    delay = self.penalty_until - now
                else:
                    self._refill(now)
                    if self.tokens >= cost:
                        self.tokens -= cost
                        self.spent += cost
                        return
                    delay = (cost - self.tokens) / self.rate
                self.waited_s += delay
                await asyncio.sleep(delay)

    def penalize(self, seconds: float) -> None:
        """After a 429: drain the bucket and pause it briefly."""
        now = time.monotonic()
        self.tokens = 0.0
        self.updated = now
        self.penalty_until = max(self.penalty_until, now + seconds)


@dataclass
class CostRule:
    method: str
    pattern: re.Pattern[str]
    cost: float


def _cost_pattern(path: str) -> re.Pattern[str]:
    """'/trade-api/v2/communications/rfqs/:rfq_id' -> regex matching the v2-relative path."""
    parts = []
    for seg in path.removeprefix("/trade-api/v2").split("/"):
        if seg.startswith(":"):
            parts.append("[^/]+")
        elif seg.startswith("*"):
            parts.append(".*")
        else:
            parts.append(re.escape(seg))
    return re.compile("^" + "/".join(parts) + "$")


@dataclass
class Limiter:
    utilization: float = 0.6
    authenticated: bool = True
    default_cost: float = 10.0
    rules: list[CostRule] = field(default_factory=list)
    buckets: dict[str, TokenBucket] = field(default_factory=dict)
    tier: str = "basic (assumed)"
    raw_limits: dict[str, Any] | None = None
    n_429: dict[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.buckets:
            self._build(BASIC_DEFAULTS)

    def _build(self, spec: dict[str, tuple[float, float]]) -> None:
        u = self.utilization
        self.buckets = {}
        for name, (rate, cap) in spec.items():
            if name == "read" and not self.authenticated:
                rate, cap = UNAUTH_READ
                self.buckets[name] = TokenBucket(rate, cap, name)
                continue
            # Keep a full request's worth of capacity so a single call never deadlocks.
            self.buckets[name] = TokenBucket(rate * u, max(cap * u, self.default_cost), name)

    def configure(self, limits: dict[str, Any] | None, costs: dict[str, Any] | None) -> None:
        spec = dict(BASIC_DEFAULTS)
        if limits:
            self.raw_limits = limits
            self.tier = str(limits.get("usage_tier") or "unknown")
            for name in ("read", "write"):
                b = limits.get(name) or {}
                rate = float(b.get("refill_rate") or spec[name][0])
                cap = float(b.get("bucket_capacity") or rate)
                spec[name] = (rate, cap)
        if costs:
            self.default_cost = float(costs.get("default_cost") or 10)
            self.rules = [
                CostRule(str(c["method"]).upper(), _cost_pattern(str(c["path"])), float(c["cost"]))
                for c in costs.get("endpoint_costs") or []
                if c.get("method") and c.get("path")
            ]
        self._build(spec)

    def cost(self, method: str, path: str) -> float:
        for rule in self.rules:
            if rule.method == method.upper() and rule.pattern.match(path):
                return rule.cost
        return self.default_cost

    @staticmethod
    def bucket_for(method: str, path: str) -> str:
        if path == "/portfolio/balance":
            return "balance"
        if method.upper() == "GET":
            return "read"
        # POST /multivariate_event_collections/{c} isn't documented as a Write; charge it to Read
        # (429 counts per endpoint in the Health tab will show if that's wrong).
        if method.upper() == "POST" and path.startswith("/multivariate_event_collections/"):
            return "read"
        return "write"

    async def acquire(self, method: str, path: str) -> None:
        name = self.bucket_for(method, path)
        await self.buckets[name].acquire(self.cost(method, path))

    def on_429(self, method: str, path: str, attempt: int) -> None:
        name = self.bucket_for(method, path)
        self.n_429[name] = self.n_429.get(name, 0) + 1
        self.buckets[name].penalize(min(8.0, 0.5 * (2**attempt)))

    def snapshot(self) -> dict[str, Any]:
        return {
            "tier": self.tier,
            "utilization": self.utilization,
            "authenticated": self.authenticated,
            "buckets": {
                n: {"rate": round(b.rate, 1), "capacity": round(b.capacity, 1), "waited_s": round(b.waited_s, 2), "spent": round(b.spent)}
                for n, b in self.buckets.items()
            },
            "n_429": dict(self.n_429),
        }
