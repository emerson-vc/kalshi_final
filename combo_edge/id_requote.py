"""Re-quote a scan's best candidates under your ID.

Makers quote anonymous RFQs (``obscure_creator_id``) far worse than identified ones: on 2026-11-10 the same
combo, seconds apart, got 17.0x from 2 makers anonymously and 49.0x from 5 makers under your ID (your app's
price). Scans stay anonymous so makers don't see ~500 RFQs a run from you that you never take; only the top
``id_requote_top`` candidates get one more RFQ under your ID, and that quote replaces the anonymous one in the
results table and is the one the execution gates see.

Candidates are ranked by fair joint × leg product (the EV if makers quoted near the leg product, as your
manual finds show they often do), not by the anonymous quote, which is too degraded to rank on.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from combo_edge.candidates import Candidate
from combo_edge.probe import ProbeRequest, ProbeResult

DONE_DECISIONS = {"accept", "paper_fill"}


def potential_ev(c: Candidate) -> float | None:
    if not c.fair_joint or not c.leg_payout_product:
        return None
    return c.fair_joint * c.leg_payout_product - 1.0


def pick_for_id_requote(results: Iterable[ProbeResult], by_sig: dict[str, Candidate], n: int) -> list[tuple[ProbeResult, Candidate]]:
    """The top ``n`` anonymous probes to re-quote under your ID: quoted, not already bought or paper-filled."""
    if n <= 0:
        return []
    pool: list[tuple[float, ProbeResult, Candidate]] = []
    for r in results:
        c = by_sig.get(r.request.signature)
        if c is None or not r.quoted or not r.request.combo_ticker or not r.request.obscure_creator_id:
            continue
        ex: dict[str, Any] = r.execution or {}
        if ex.get("executed") or ex.get("decision") in DONE_DECISIONS:
            continue
        pev = potential_ev(c)
        if pev is None:
            continue
        pool.append((pev, r, c))
    pool.sort(key=lambda t: -t[0])
    return [(r, c) for _, r, c in pool[:n]]


def id_requote_request(anon: ProbeResult, c: Candidate, stake: float, window: float) -> ProbeRequest:
    """The one RFQ per pick that goes out under your ID. Same stake and window as the scan."""
    return ProbeRequest(anon.request.combo_ticker, c.signature, "id_requote", stake, window, candidate=c,
                        hours_to_start=anon.request.hours_to_start, offset_h=anon.request.offset_h, obscure_creator_id=False)
