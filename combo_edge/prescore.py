"""Prescore: a prior fair joint and an expected quote for every candidate, before spending an RFQ.

fair_joint = P(A) * P(B | A). P(leg) is the blended fair when the Phase-2 model is loaded, else the
Kalshi mid (falling back to the ask). The joint comes from the fair model's simulator when it can
price the pair, else from the family's tail-dependent copula (``config/priors.yaml``).

Expected quote: makers price ~ the product of the (fee-inclusive) leg payouts, times an expected ratio
that the maker model predicts once it has data. Expected EV = fair_joint * expected_payout - 1.
"""

from __future__ import annotations

import logging
import math
from collections import defaultdict
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Protocol

import numpy as np
import yaml

from combo_edge import pricing
from combo_edge.candidates import FAMILIES, Candidate
from combo_edge.copulas import Copula, cap_conditional, fit_to_anchors
from combo_edge.sports.base import Leg
from combo_edge.strata import classify, relation

log = logging.getLogger(__name__)
DEFAULT_PRIORS_PATH = Path("config/priors.yaml")


class FairModelLike(Protocol):
    version: str

    def leg_prob(self, leg: Leg) -> tuple[float | None, float | None]:
        """(model_p, blended_p) for the YES side."""
        ...

    def joint_many(self, cands: list[Candidate]) -> list[tuple[float, float] | None]:
        """(fair_joint, fair_lower) per candidate, or None when the model can't price it."""
        ...


class MakerModelLike(Protocol):
    def predict_ratio(self, cands: list[Candidate]) -> list[float | None]: ...


@dataclass
class FamilyPrior:
    name: str
    copula: Copula
    anchored: bool
    uncertainty: float
    expected_ratio: float
    anchors: tuple[tuple[float, float, float], ...] = ()

    @property
    def max_cond(self) -> float | None:
        """Highest anchored conditional: the copula may not imply a stronger P(B|A) than any find showed."""
        return max(a[2] for a in self.anchors) if self.anchors else None

    def extrapolation(self, pa: float, pb: float) -> float:
        """Distance (in e-folds of probability) from the nearest anchor; 0 when unanchored."""
        if not self.anchors or pa <= 0 or pb <= 0:
            return 0.0
        best = 1e9
        for a_cond, a_target, _ in self.anchors:
            # anchors are symmetric for exchangeable copulas: try both leg orders
            d1 = abs(math.log(pa / a_cond)) + abs(math.log(pb / a_target))
            d2 = abs(math.log(pb / a_cond)) + abs(math.log(pa / a_target))
            best = min(best, d1, d2)
        return best


@dataclass
class Priors:
    defaults: dict[str, Any]
    families: dict[str, FamilyPrior] = field(default_factory=dict)
    source: str = ""

    @property
    def expected_ratio(self) -> float:
        return float(self.defaults.get("expected_ratio", 1.0))

    @property
    def max_expected_payout(self) -> float:
        return float(self.defaults.get("max_expected_payout", 200.0))

    @property
    def max_lift(self) -> float:
        return float(self.defaults.get("max_lift", 6.0))

    @property
    def payout_uncertainty(self) -> tuple[float, float]:
        """(extra band per doubling of payout, payout where it starts)."""
        return float(self.defaults.get("payout_uncertainty_per_doubling", 0.10)), float(self.defaults.get("payout_uncertainty_from", 20.0))

    @property
    def extrapolation_uncertainty(self) -> float:
        return float(self.defaults.get("uncertainty_per_efold_from_anchor", 0.12))

    @property
    def leg_uncertainty(self) -> float:
        return float(self.defaults.get("uncertainty_per_unquoted_leg", 0.20))

    def for_family(self, name: str) -> FamilyPrior | None:
        fam = FAMILIES.get(name)
        key = fam.prior_key if fam else name
        return self.families.get(key)

    def for_relation(self, rel: str) -> FamilyPrior:
        rho = float((self.defaults.get("relation_rho") or {}).get(rel, self.defaults.get("rho", 0.3)))
        return FamilyPrior(
            f"default:{rel}",
            Copula(self.defaults.get("copula", "t"), rho, float(self.defaults.get("df", 4))),
            False,
            float(self.defaults.get("uncertainty_unanchored", 0.3)) + 0.05,
            self.expected_ratio,
        )


@lru_cache(maxsize=64)
def _fit_cached(kind: str, df: float, anchors: tuple[tuple[float, float, float], ...]) -> Copula:
    return fit_to_anchors(list(anchors), kind, df)  # type: ignore[arg-type]


def load_priors(path: str | Path | None = None) -> Priors:
    p = Path(path) if path else DEFAULT_PRIORS_PATH
    if not p.exists():
        p = Path(__file__).resolve().parent.parent / "config" / "priors.yaml"
    raw = yaml.safe_load(p.read_text()) if p.exists() else {}
    defaults = dict(raw.get("defaults") or {})
    pri = Priors(defaults=defaults, source=str(p))
    kind_default = str(defaults.get("copula", "t"))
    df_default = float(defaults.get("df", 4))
    for name, spec in (raw.get("families") or {}).items():
        spec = spec or {}
        kind = str(spec.get("copula", kind_default))
        df = float(spec.get("df", df_default))
        anchors = tuple(
            (float(a["p_cond"]), float(a["p_target"]), float(a["cond"])) for a in (spec.get("anchors") or [])
        )
        if anchors:
            cop = _fit_cached(kind, df, anchors)
            anchored = True
        else:
            theta = float(spec.get("rho", spec.get("theta", defaults.get("rho", 0.35))))
            cop = Copula(kind, theta, df)  # type: ignore[arg-type]
            anchored = False
        unc = float(defaults.get("uncertainty_anchored" if anchored else "uncertainty_unanchored", 0.2))
        pri.families[name] = FamilyPrior(name, cop, anchored, float(spec.get("uncertainty", unc)),
                                         float(spec.get("expected_ratio", pri.expected_ratio)), anchors)
    return pri


class Prescorer:
    def __init__(self, priors: Priors, fair_model: FairModelLike | None = None, maker_model: MakerModelLike | None = None):
        self.priors = priors
        self.fair_model = fair_model
        self.maker_model = maker_model
        self._leg_p: dict[str, tuple[float | None, float | None]] = {}

    def leg_p(self, leg: Leg) -> float | None:
        """Best available P(YES) for a leg: blended fair if a model is loaded, else the market."""
        if leg.ticker not in self._leg_p:
            model_p, blended = (None, None)
            if self.fair_model is not None:
                try:
                    model_p, blended = self.fair_model.leg_prob(leg)
                except Exception:  # noqa: BLE001
                    model_p, blended = None, None
            self._leg_p[leg.ticker] = (model_p, blended if blended is not None else leg.p_market())
        return self._leg_p[leg.ticker][1]

    def model_p(self, leg: Leg) -> float | None:
        self.leg_p(leg)
        return self._leg_p[leg.ticker][0]

    def score(self, cands: list[Candidate]) -> None:
        if not cands:
            return
        for c in cands:
            ps = []
            for s in c.legs:
                p = self.leg_p(s.leg)
                ps.append(None if p is None else (p if s.side == "yes" else 1.0 - p))
            c.p_legs = tuple(ps)
            c.unpriced = any(p is None for p in ps)
            pays = []
            for s in c.legs:
                ask = s.ask()
                pays.append(pricing.payout_fee_inclusive(ask))
            c.leg_payouts = tuple(pays)
            c.leg_payout_product = None if any(p is None for p in pays) else pricing.product(p for p in pays if p is not None)
            if len(c.legs) >= 2:
                c.stratum_l1, c.stratum_l2 = classify(c.legs[0], c.legs[1], ps[0], ps[1])

        model_out: list[tuple[float, float] | None] = [None] * len(cands)
        if self.fair_model is not None:
            try:
                model_out = self.fair_model.joint_many(cands)
            except Exception:  # noqa: BLE001
                log.exception("fair model failed; using copula priors")
        groups: dict[str, list[int]] = defaultdict(list)
        for i, c in enumerate(cands):
            if model_out[i] is not None:
                c.fair_joint, c.fair_lower = model_out[i]
                c.fair_source = "simulator"
                c.fair_version = getattr(self.fair_model, "version", "")
                continue
            if c.unpriced or len(c.legs) != 2:
                if not c.unpriced and len(c.legs) > 2:
                    c.fair_joint = pricing.product(p for p in c.p_legs if p is not None)
                    c.fair_lower = c.fair_joint * 0.7
                    c.fair_source = "independence"
                continue
            prior = self.priors.for_family(c.family)
            key = c.family if prior else f"default:{relation(c.legs[0], c.legs[1])}"
            groups[key].append(i)
        for key, idxs in groups.items():
            prior = self.priors.for_family(key) if not key.startswith("default:") else self.priors.for_relation(key.split(":", 1)[1])
            assert prior is not None
            # Copula args are YES probabilities; side handling happens in Copula.joint.
            pa = np.array([self.leg_p(cands[i].legs[0].leg) for i in idxs], dtype=float)
            pb = np.array([self.leg_p(cands[i].legs[1].leg) for i in idxs], dtype=float)
            sa = np.array([cands[i].legs[0].side == "yes" for i in idxs])
            sb = np.array([cands[i].legs[1].side == "yes" for i in idxs])
            joint = prior.copula.joint(pa, pb, sa, sb)
            if prior.max_cond is not None:
                joint = np.where(sa & sb, cap_conditional(joint, pa, pb, prior.max_cond), joint)
            for j, i in enumerate(idxs):
                c = cands[i]
                c.fair_joint = float(max(joint[j], 0.0))
                n_soft = sum(1 for s in c.legs if (s.leg.p_source or "mid") != "mid")
                far = prior.extrapolation(float(pa[j]), float(pb[j])) * self.priors.extrapolation_uncertainty
                u = min(0.9, prior.uncertainty + self.priors.leg_uncertainty * n_soft + far)
                c.fair_lower = c.fair_joint * (1.0 - u)
                c.fair_source = f"copula:{prior.name}" + ("" if prior.anchored else "(unanchored)")
                c.fair_version = f"priors:{prior.copula.kind}:{prior.copula.theta:.3f}"
                pa_side = c.p_legs[0]
                c.p_b_given_a = (c.fair_joint / pa_side) if pa_side else None

        ratios: list[float | None] = [None] * len(cands)
        if self.maker_model is not None:
            try:
                ratios = self.maker_model.predict_ratio(cands)
            except Exception:  # noqa: BLE001
                log.exception("maker model failed; using expected_ratio prior")
        for i, c in enumerate(cands):
            prior = self.priors.for_family(c.family)
            c.expected_ratio = ratios[i] if ratios[i] is not None else (prior.expected_ratio if prior else self.priors.expected_ratio)
            if c.leg_payout_product is not None:
                c.expected_payout = c.expected_ratio * c.leg_payout_product
            elif c.fair_joint:
                # A leg without an ask: assume makers price off our fair (worst case: no edge).
                c.expected_payout = pricing.payout_fee_inclusive(min(0.99, c.fair_joint))
            raw_payout = c.expected_payout
            if c.expected_payout is not None:
                # Prior only: makers rarely sell penny-on-penny combos at the product of 1c asks.
                c.expected_payout = min(c.expected_payout, self.priors.max_expected_payout)
            self._tail_guards(c, raw_payout)
            c.expected_ev = pricing.ev(c.fair_joint, c.expected_payout)
            c.expected_ev_lower = pricing.ev(c.fair_lower, c.expected_payout)

    def _tail_guards(self, c: Candidate, payout: float | None) -> None:
        """Keep the model's edge from growing without bound as both legs get longer.

        1. Lift cap: fair_joint <= max_lift * P(A) * P(B) (side probabilities), any fair source.
        2. Payout band: fair_lower drops by an extra 10% of fair per doubling of payout above 20x, so EV at
           the lower bound (the ranking key) penalizes deep tails where the dependence is least known.
        """
        if c.fair_joint is None:
            return
        if c.p_legs and all(p is not None for p in c.p_legs):
            cap = self.priors.max_lift * pricing.product(p for p in c.p_legs if p is not None)
            if c.fair_joint > cap > 0:
                scale = cap / c.fair_joint
                c.fair_joint = cap
                c.fair_lower = None if c.fair_lower is None else c.fair_lower * scale
                c.notes.append("lift_capped")
        per_doubling, start = self.priors.payout_uncertainty
        if c.fair_lower is not None and payout is not None and payout > start:
            c.fair_lower = max(0.0, c.fair_lower - per_doubling * math.log2(payout / start) * c.fair_joint)
        if c.p_legs and c.p_legs[0]:
            c.p_b_given_a = c.fair_joint / c.p_legs[0]
