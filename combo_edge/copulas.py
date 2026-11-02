"""Tail-dependent copulas for the Phase-1 prescore fallback.

No Gaussian copula anywhere: it has zero tail dependence, which is exactly the makers' apparent mistake,
so it would hide the edge. Default is a Student-t copula with df≈4 (symmetric tail dependence, so it
also covers "No + No" pairs). Gumbel and a 180°-rotated Clayton put the dependence on the joint-YES tail.

Joint-YES probability: S(pa, pb) = P(A=yes, B=yes) where P(A)=pa, P(B)=pb.
The t version is exact up to quadrature error: given T1 = x, T2 is Student-t with df+1 degrees of
freedom, location rho·x and scale sqrt((1−rho²)(df+x²)/(df+1)). Integrating over u = F(x) on
[1−pa, 1] with Gauss–Legendre gives a smooth bounded integrand, so deep tails stay accurate
(max relative error ~3e-4 vs SciPy, including two 0.2% legs).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from functools import lru_cache
from typing import Literal

import numpy as np
from scipy import special, stats
from scipy.optimize import minimize_scalar

Kind = Literal["t", "gumbel", "clayton"]
_N_NODES = 48
_CHUNK = 20000


@lru_cache(maxsize=1)
def _legendre() -> tuple[np.ndarray, np.ndarray]:
    return np.polynomial.legendre.leggauss(_N_NODES)


def t_joint_yes(pa: np.ndarray, pb: np.ndarray, rho: float, df: float = 4.0) -> np.ndarray:
    pa = np.clip(np.asarray(pa, dtype=float).reshape(-1), 1e-12, 1 - 1e-12)
    pb = np.clip(np.asarray(pb, dtype=float).reshape(-1), 1e-12, 1 - 1e-12)
    rho = float(np.clip(rho, -0.97, 0.97))
    t, v = _legendre()
    out = np.empty(pa.shape, dtype=float)
    for i in range(0, pa.size, _CHUNK):
        a_p, b_p = pa[i : i + _CHUNK], pb[i : i + _CHUNK]
        b = stats.t.isf(b_p, df)
        x = stats.t.isf(a_p[:, None] * (1.0 - t[None, :]) / 2.0, df)  # T1 quantiles across [1-pa, 1]
        z = (b[:, None] - rho * x) / np.sqrt((1.0 - rho * rho) * (df + x * x) / (df + 1.0))
        surv = special.stdtr(df + 1.0, -z)  # P(T2 > b | T1 = x)
        out[i : i + _CHUNK] = a_p / 2.0 * (surv * v[None, :]).sum(axis=1)
    return np.clip(out, np.maximum(pa + pb - 1.0, 0.0), np.minimum(pa, pb))


def gumbel_joint_yes(pa: np.ndarray, pb: np.ndarray, theta: float) -> np.ndarray:
    pa = np.clip(np.asarray(pa, dtype=float), 1e-12, 1 - 1e-12)
    pb = np.clip(np.asarray(pb, dtype=float), 1e-12, 1 - 1e-12)
    theta = max(1.0, float(theta))
    u, v = 1.0 - pa, 1.0 - pb
    c = np.exp(-(((-np.log(u)) ** theta + (-np.log(v)) ** theta) ** (1.0 / theta)))
    return np.clip(pa + pb - 1.0 + c, np.maximum(pa + pb - 1, 0), np.minimum(pa, pb))


def clayton_joint_yes(pa: np.ndarray, pb: np.ndarray, theta: float) -> np.ndarray:
    """180°-rotated Clayton: its lower-tail dependence becomes joint-YES tail dependence."""
    pa = np.clip(np.asarray(pa, dtype=float), 1e-12, 1 - 1e-12)
    pb = np.clip(np.asarray(pb, dtype=float), 1e-12, 1 - 1e-12)
    theta = max(1e-6, float(theta))
    c = (pa ** (-theta) + pb ** (-theta) - 1.0) ** (-1.0 / theta)
    return np.clip(c, np.maximum(pa + pb - 1, 0), np.minimum(pa, pb))


def upper_tail_dependence(kind: Kind, theta: float, df: float = 4.0) -> float:
    if kind == "t":
        rho = float(np.clip(theta, -0.95, 0.95))
        return float(2.0 * stats.t.cdf(-math.sqrt((df + 1.0) * (1.0 - rho) / (1.0 + rho)), df + 1.0))
    if kind == "gumbel":
        return 2.0 - 2.0 ** (1.0 / max(theta, 1.0))
    return 2.0 ** (-1.0 / max(theta, 1e-6))


_BOUNDS: dict[str, tuple[float, float]] = {"t": (-0.9, 0.95), "gumbel": (1.0, 15.0), "clayton": (0.01, 25.0)}


@dataclass(frozen=True)
class Copula:
    kind: Kind = "t"
    theta: float = 0.5  # rho for t, theta for gumbel/clayton
    df: float = 4.0

    def joint_yes(self, pa: np.ndarray | float, pb: np.ndarray | float) -> np.ndarray:
        if self.kind == "t":
            return t_joint_yes(np.atleast_1d(pa), np.atleast_1d(pb), self.theta, self.df)
        if self.kind == "gumbel":
            return gumbel_joint_yes(np.atleast_1d(pa), np.atleast_1d(pb), self.theta)
        return clayton_joint_yes(np.atleast_1d(pa), np.atleast_1d(pb), self.theta)

    def joint(self, pa: np.ndarray, pb: np.ndarray, side_a: np.ndarray, side_b: np.ndarray) -> np.ndarray:
        """Joint probability for each (side_a, side_b) in {True=yes, False=no}, given YES probabilities."""
        pa = np.atleast_1d(np.asarray(pa, dtype=float))
        pb = np.atleast_1d(np.asarray(pb, dtype=float))
        sa = np.atleast_1d(np.asarray(side_a, dtype=bool))
        sb = np.atleast_1d(np.asarray(side_b, dtype=bool))
        s = self.joint_yes(pa, pb)
        return np.where(sa & sb, s, np.where(sa & ~sb, pa - s, np.where(~sa & sb, pb - s, 1.0 - pa - pb + s)))

    def tail_dependence(self) -> float:
        return upper_tail_dependence(self.kind, self.theta, self.df)


def fit_to_anchors(anchors: list[tuple[float, float, float]], kind: Kind = "t", df: float = 4.0) -> Copula:
    """Find the parameter whose implied P(B|A)=S(pa,pb)/pa best matches each anchor (pa, pb, cond)."""
    if not anchors:
        raise ValueError("no anchors")
    pa = np.array([a[0] for a in anchors])
    pb = np.array([a[1] for a in anchors])
    target = np.array([a[2] for a in anchors])

    def loss(theta: float) -> float:
        cond = Copula(kind, theta, df).joint_yes(pa, pb) / pa
        return float(np.sum((cond - target) ** 2))

    lo, hi = _BOUNDS[kind]
    res = minimize_scalar(loss, bounds=(lo, hi), method="bounded", options={"xatol": 1e-4})
    return Copula(kind, float(res.x), df)
