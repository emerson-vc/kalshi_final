"""Pluggable outside fair prices (a sportsbook or OddsJam feed later). OFF by default: no paid services.

To add one, implement ``OddsProvider.leg_prob`` and return it from ``get_provider``; the fair model
blends it in like a third opinion. Ask before adding anything paid.
"""

from __future__ import annotations

from typing import Protocol

from combo_edge.sports.base import Leg


class OddsProvider(Protocol):
    name: str

    def leg_prob(self, leg: Leg) -> float | None:
        """No-vig P(YES) for this leg, or None if the provider doesn't cover it."""
        ...


class NullProvider:
    name = "none"

    def leg_prob(self, leg: Leg) -> float | None:
        return None


def get_provider() -> OddsProvider:
    return NullProvider()
