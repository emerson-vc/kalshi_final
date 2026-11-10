"""Joint Monte Carlo game simulator: the Phase-2 fair model's dependence structure.

Per simulated game (vectorized over N sims):
  latent offense  Z_i = sqrt(rho)*G + sqrt(1-rho)*E_i       (shared pace G, team-specific E_i)
  drives          each of ~11 drives ends in a TD, a FG or nothing; the per-drive TD logit moves with Z_i,
                  and the FG rate on non-TD drives falls when a game's TDs run above expectation
  points          6*TD + XP(0.94 each) + 3*FG (+ rare safety); halves/quarters by binomial thinning
  TD types        pass ~ Binomial(TD, s) with s ~ Beta per simulated game (mean = team pass share,
                  concentration ~12: some games are pass-heavy, some run-heavy); D/ST share; the rest rushing
  passing game    attempts by game script (trailing teams pass more); targets ~ Multinomial over
                  Dirichlet-perturbed target shares; receptions ~ Binomial(catch rate); yards ~ Gamma
                  per reception x efficiency(Z); QB yards / completions are the receivers' sums
  TD scorers      pass TDs to receivers by targets x TD rate; rush TDs to rushers by carries x TD rate
  rushing         carries by game script (leaders run more); yards ~ Gamma per carry x efficiency

So a QB's 4th TD pass is literally team points, a WR's 3 TDs are literally his team's TDs, and RB rush
yards move with the lead. That is the correlation the makers appear to under-price. Team scoring is fitted
to the Kalshi team-total ladder (common random numbers). Player marginals are later mapped onto the
blended Kalshi/model targets in rank space, which keeps this dependence and matches the market.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
from scipy import stats as sstats
from scipy.optimize import minimize

XP_RATE = 0.94


@dataclass
class SimParams:
    n: int = 30000
    sigma: float = 0.30
    rho: float = 0.10
    dst_share: float = 0.04
    pass_share_conc: float = 12.0  # Beta concentration of the per-game pass-TD share (lower = more game-to-game spread)
    # TDs replace field goals: the FG rate on a game's non-TD drives scales by exp(-fg_td_tradeoff x (TDs -
    # expected TDs)), normalized to the same average FG rate. Leaves the TD count (and so every TD
    # marginal) untouched. nflverse 2018-25: 3.4 non-TD
    # points on 4+ pass-TD days vs 6.0 on average; 0 (the old independent FGs) gave 4.9.
    fg_td_tradeoff: float = 0.2
    h1_share_td: float = 0.48
    h1_share_fg: float = 0.47
    q1_share_of_h1: float = 0.44
    alpha_targets: float = 30.0
    alpha_carries: float = 25.0
    kappa_eff: float = 0.08
    kappa_score: float = 0.22
    script_pass: float = -0.10  # per 10 points of lead
    script_rush: float = 0.15
    seed: int = 7


@dataclass
class ReceiverSpec:
    key: str
    share: float
    catch_rate: float
    ypr: float
    td_rate: float


@dataclass
class RusherSpec:
    key: str
    share: float
    ypc: float
    td_rate: float


@dataclass
class TeamSpec:
    team: str
    mu_td: float = 0.22  # per-drive TD probability
    phi_fg: float = 0.15  # per-drive FG probability
    pass_share: float = 0.62
    pass_att: float = 34.0
    carries: float = 26.0
    int_rate: float = 0.024
    qb_key: str | None = None
    receivers: list[ReceiverSpec] = field(default_factory=list)
    rushers: list[RusherSpec] = field(default_factory=list)


_GH_T, _GH_W = np.polynomial.hermite.hermgauss(24)
_Z_NODES = np.sqrt(2.0) * _GH_T
_Z_W = _GH_W / math.sqrt(math.pi)
DRIVES = np.array([10, 11, 12, 13])
DRIVE_W = np.array([0.20, 0.35, 0.30, 0.15])


def _outcome_tables() -> dict[int, tuple[np.ndarray, ...]]:
    """Per drive count D: every (td, fg, xp) outcome with its points and the constant log-coefficients."""
    from scipy.special import gammaln

    out = {}
    for D in DRIVES:
        k, m, x = [], [], []
        for kk in range(D + 1):
            for mm in range(D - kk + 1):
                for xx in range(kk + 1):
                    k.append(kk), m.append(mm), x.append(xx)
        k_, m_, x_ = (np.array(v, dtype=float) for v in (k, m, x))
        lc = (gammaln(D + 1) - gammaln(k_ + 1) - gammaln(D - k_ + 1)
              + gammaln(D - k_ + 1) - gammaln(m_ + 1) - gammaln(D - k_ - m_ + 1)
              + gammaln(k_ + 1) - gammaln(x_ + 1) - gammaln(k_ - x_ + 1)
              + x_ * math.log(XP_RATE) + (k_ - x_) * math.log(1 - XP_RATE))
        out[int(D)] = (k_, m_, lc, (6 * k_ + 3 * m_ + x_).astype(int))
    return out


_OUTCOMES = _outcome_tables()
_PTS_LEN = 7 * int(DRIVES.max()) + 3 * int(DRIVES.max()) + 1


def points_pmf(p_td: float, p_fg: float, sigma: float, nodes: int = 24) -> np.ndarray:
    """Exact pmf of team points under the drive model, mixed over drive counts and the latent factor.

    Each of D drives ends in a TD (prob p_td, shifted on the logit scale by sigma*Z), a FG (p_fg) or
    nothing; TD + XP(0.94) + 3*FG. Binomial drive outcomes are underdispersed vs Poisson, matching how
    narrow real team-total ladders are, and make TDs and FGs trade off against each other.
    """
    from scipy.special import expit, logit

    zs, wz = (_Z_NODES, _Z_W) if (nodes >= 24 and sigma > 0) else ((np.array([0.0]), np.array([1.0])) if sigma == 0 else np.polynomial.hermite.hermgauss(nodes))
    if sigma > 0 and nodes < 24:
        zs, wz = np.sqrt(2.0) * zs, wz / math.sqrt(math.pi)
    ptd = expit(logit(np.clip(p_td, 1e-4, 0.9)) + sigma * np.asarray(zs))  # per node
    pfg = np.minimum(p_fg, 0.98 - ptd)
    q = pfg / np.maximum(1e-9, 1.0 - ptd)
    out = np.zeros(_PTS_LEN)
    for D, wd in zip(DRIVES, DRIVE_W):
        k, m, lc, pts = _OUTCOMES[int(D)]
        lp = (lc[None, :] + k[None, :] * np.log(ptd)[:, None] + (D - k)[None, :] * np.log1p(-ptd)[:, None]
              + m[None, :] * np.log(np.maximum(q, 1e-12))[:, None] + (D - k - m)[None, :] * np.log1p(-q)[:, None])
        w = (np.exp(lp) * np.asarray(wz)[:, None]).sum(axis=0) * wd
        out += np.bincount(pts, weights=w, minlength=_PTS_LEN)
    return out


def points_survival(mu: float, phi: float, sigma: float, xs: np.ndarray) -> np.ndarray:
    pmf = points_pmf(mu, phi, sigma)
    surv = 1.0 - np.cumsum(pmf)  # P(points > i)
    idx = np.clip(np.floor(xs).astype(int), 0, len(surv) - 1)
    return surv[idx]


def points_sd(mu: float, phi: float, sigma: float) -> float:
    pmf = points_pmf(mu, phi, sigma)
    x = np.arange(len(pmf))
    m = float((x * pmf).sum())
    return float(math.sqrt(max(((x - m) ** 2 * pmf).sum(), 0.0)))


def sigma_for_sd(target_sd: float, mean_pts: float = 22.0) -> float:
    """Latent mixing sigma whose points SD at a typical mean matches the historical residual SD."""
    ptd = mean_pts / (float((DRIVES * DRIVE_W).sum()) * 8.6)
    best, err = 0.15, 1e9
    for sg in np.linspace(0.0, 1.0, 26):
        e = abs(points_sd(ptd, 0.66 * ptd, float(sg)) - target_sd)
        if e < err:
            best, err = float(sg), e
    return best


def fit_team_scoring(ladder: list[tuple[float, float, float]], sigma: float, mean_hint: float | None = None, n: int = 0, seed: int = 0) -> tuple[float, float, float]:
    """Fit per-drive (p_td, p_fg) so P(points > x) matches the ladder [(x, p, weight)]. Returns (p_td, p_fg, rmse)."""
    from scipy.special import expit, logit

    xs = np.array([x for x, _, _ in ladder], dtype=float)
    ps = np.array([p for _, p, _ in ladder], dtype=float)
    ws = np.array([w for _, _, w in ladder], dtype=float)
    target_mean = mean_hint if mean_hint else _ladder_mean(xs, ps)
    mean_d = float((DRIVES * DRIVE_W).sum())

    def unpack(theta: np.ndarray) -> tuple[float, float]:
        return float(expit(theta[0])) * 0.8, float(expit(theta[1])) * 0.5

    def loss(theta: np.ndarray) -> float:
        ptd, pfg = unpack(theta)
        pmf = points_pmf(ptd, pfg, sigma)
        err = 0.0
        if len(xs):
            surv = 1.0 - np.cumsum(pmf)
            sim = surv[np.clip(np.floor(xs).astype(int), 0, len(surv) - 1)]
            err = float(np.sum(ws * (sim - ps) ** 2) / max(ws.sum(), 1e-9))
        ratio_pen = 0.002 * (math.log(max(pfg, 1e-4) / max(ptd, 1e-4)) - math.log(0.66)) ** 2
        mean_pen = 0.0
        if len(xs) < 3:
            mean_pen = ((float((np.arange(len(pmf)) * pmf).sum()) - target_mean) / 10.0) ** 2
        return err + ratio_pen + mean_pen

    ptd0 = min(0.6, max(0.05, target_mean / (mean_d * 8.6)))
    theta0 = np.array([logit(ptd0 / 0.8), logit(min(0.9, 0.66 * ptd0 / 0.5))])
    res = minimize(loss, theta0, method="Nelder-Mead", options={"xatol": 1e-4, "fatol": 1e-9, "maxiter": 250})
    ptd, pfg = unpack(res.x)
    return ptd, pfg, float(math.sqrt(max(res.fun, 0.0)))


def _ladder_mean(xs: np.ndarray, ps: np.ndarray) -> float:
    if len(xs) == 0:
        return 22.0
    order = np.argsort(xs)
    xs, ps = xs[order], ps[order]
    # E[X] = integral of the survival function; extend flat to 0 and taper beyond the last rung
    grid = np.arange(0, 70, 0.5)
    surv = np.interp(grid, xs, ps, left=min(1.0, ps[0] + (1 - ps[0]) * 0.5), right=ps[-1] * 0.3)
    return float(surv.sum() * 0.5)


def _pass_share_draws(rng: np.random.Generator, mean: float, conc: float, n: int) -> np.ndarray:
    m = float(np.clip(mean, 0.05, 0.95))
    if conc <= 0:
        return np.full(n, m)
    return rng.beta(m * conc, (1.0 - m) * conc, size=n)


@dataclass
class SimResult:
    n: int
    stats: dict[tuple[str, ...], np.ndarray]


class GameSim:
    def __init__(self, game_code: str, away: TeamSpec, home: TeamSpec, params: SimParams | None = None):
        self.game_code = game_code
        self.teams = (away, home)
        self.p = params or SimParams()

    def run(self) -> SimResult:
        p = self.p
        rng = np.random.default_rng(p.seed)
        n = p.n
        g = rng.standard_normal(n)
        z = [math.sqrt(p.rho) * g + math.sqrt(1 - p.rho) * rng.standard_normal(n) for _ in range(2)]
        st: dict[tuple[str, ...], np.ndarray] = {}
        td, fg, pts, pass_td, rush_td, dst_td, h1, q1 = [], [], [], [], [], [], [], []
        drives = rng.choice(DRIVES, size=n, p=DRIVE_W)
        extra = rng.random(n) < 0.5  # possessions alternate: one team may get one more drive
        from scipy.special import expit, logit

        for i, t in enumerate(self.teams):
            d_i = drives + (extra if i == 0 else 0)
            ptd = expit(logit(np.clip(t.mu_td, 1e-4, 0.9)) + p.sigma * z[i])
            pfg = np.clip(t.phi_fg, 0.0, 0.98 - ptd)
            tdi = rng.binomial(d_i, ptd)
            q = pfg / np.maximum(1e-9, 1.0 - ptd)
            if p.fg_td_tradeoff > 0:  # a 4-TD day's extra TDs came from drives that would have been FGs
                tilt = np.exp(-p.fg_td_tradeoff * (tdi - d_i * ptd))
                q = np.clip(q * tilt / tilt.mean(), 0.0, 0.95)  # same average FG rate, shifted toward low-TD games
            fgi = rng.binomial(d_i - tdi, q)
            # A 4-pass-TD day is often a pass-heavy game, not a 6-TD game: draw this game's pass share.
            share = _pass_share_draws(rng, t.pass_share, p.pass_share_conc, n)
            ptd = rng.binomial(tdi, share)
            dst = rng.binomial(tdi - ptd, np.minimum(0.5, p.dst_share / np.maximum(1e-6, 1 - share)))
            rtd = tdi - ptd - dst
            # nested scoring: Q1 inside 1H inside the game, XPs and safeties assigned consistently
            td_h1 = rng.binomial(tdi, p.h1_share_td)
            fg_h1 = rng.binomial(fgi, p.h1_share_fg)
            td_q1 = rng.binomial(td_h1, p.q1_share_of_h1)
            fg_q1 = rng.binomial(fg_h1, p.q1_share_of_h1)
            xp_q1 = rng.binomial(td_q1, XP_RATE)
            xp_h1 = xp_q1 + rng.binomial(td_h1 - td_q1, XP_RATE)
            xp = xp_h1 + rng.binomial(tdi - td_h1, XP_RATE)
            saf = rng.random(n) < 0.02
            saf_h1 = saf & (rng.random(n) < 0.5)
            ptsi = 6 * tdi + xp + 3 * fgi + 2 * saf
            h1i = 6 * td_h1 + xp_h1 + 3 * fg_h1 + 2 * saf_h1
            q1i = 6 * td_q1 + xp_q1 + 3 * fg_q1
            td.append(tdi), fg.append(fgi), pts.append(ptsi), pass_td.append(ptd), rush_td.append(rtd), dst_td.append(dst), h1.append(h1i), q1.append(q1i)
        coin = rng.random(n) < 0.5
        for i, t in enumerate(self.teams):
            j = 1 - i
            margin = pts[i] - pts[j]
            st[("team", t.team, "points")] = pts[i]
            st[("team", t.team, "margin")] = margin
            st[("team", t.team, "win")] = ((margin > 0) | ((margin == 0) & (coin if i == 0 else ~coin))).astype(float)
            st[("team", t.team, "h1_points")] = h1[i]
            st[("team", t.team, "h1_margin")] = h1[i] - h1[j]
            st[("team", t.team, "h1_win")] = (h1[i] > h1[j]).astype(float)
            st[("team", t.team, "q1_points")] = q1[i]
            st[("team", t.team, "q1_margin")] = q1[i] - q1[j]
            st[("team", t.team, "q1_win")] = (q1[i] > q1[j]).astype(float)
            st[("team", t.team, "dst_td")] = dst_td[i]
            st[("team", t.team, "pass_td")] = pass_td[i]
            self._players(t, i, pass_td[i], rush_td[i], margin, z[i], rng, st, scoring=td[i] + 0.4 * fg[i])
        st[("game", "total")] = pts[0] + pts[1]
        st[("game", "h1_total")] = h1[0] + h1[1]
        st[("game", "q1_total")] = q1[0] + q1[1]
        st[("game", "tie")] = (pts[0] == pts[1]).astype(float)
        st[("game", "h1_tie")] = (h1[0] == h1[1]).astype(float)
        st[("game", "q1_tie")] = (q1[0] == q1[1]).astype(float)
        return SimResult(n, st)

    def _players(self, t: TeamSpec, i: int, pass_td: np.ndarray, rush_td: np.ndarray, margin: np.ndarray, z: np.ndarray,
                 rng: np.random.Generator, st: dict[tuple[str, ...], np.ndarray], scoring: np.ndarray | None = None) -> None:
        p = self.p
        n = margin.shape[0]
        # Yardage efficiency follows the team's realized scoring (big passing days and TDs go together)
        # plus a smaller latent component.
        s = (scoring - scoring.mean()) / (scoring.std() + 1e-9) if scoring is not None else np.zeros(n)
        eff = np.exp(p.kappa_score * s - p.kappa_score ** 2 / 2) * np.exp(p.kappa_eff * z - p.kappa_eff ** 2 / 2)
        att = np.maximum(10, np.round(t.pass_att * np.exp(p.script_pass * margin / 10.0) * np.exp(rng.normal(0, 0.12, n)))).astype(np.int64)
        car = np.maximum(8, np.round(t.carries * np.exp(p.script_rush * margin / 10.0) * np.exp(rng.normal(0, 0.12, n)))).astype(np.int64)
        recs = list(t.receivers) or [ReceiverSpec("_other", 1.0, 0.65, 11.0, 0.05)]
        shares = np.array([max(r.share, 1e-4) for r in recs])
        shares = shares / shares.sum()
        dirich = rng.dirichlet(shares * p.alpha_targets, size=n)
        targets = rng.multinomial(att, dirich)
        cr = np.array([r.catch_rate for r in recs])
        receptions = rng.binomial(targets, cr[None, :])
        ypr = np.array([max(r.ypr, 3.0) for r in recs])
        k = 1.4
        yards = np.where(receptions > 0, rng.gamma(np.maximum(receptions, 1) * k, ypr[None, :] / k) * eff[:, None], 0.0)
        tdw = targets * np.array([max(r.td_rate, 1e-3) for r in recs])[None, :] + 1e-6
        rec_td = rng.multinomial(pass_td, tdw / tdw.sum(axis=1, keepdims=True))
        rush = list(t.rushers) or [RusherSpec("_rb", 1.0, 4.2, 0.03)]
        cs = np.array([max(r.share, 1e-4) for r in rush])
        cs = cs / cs.sum()
        carries = rng.multinomial(car, rng.dirichlet(cs * p.alpha_carries, size=n))
        ypc = np.array([max(r.ypc, 1.0) for r in rush])
        kr = 0.9
        ryds = np.where(carries > 0, rng.gamma(np.maximum(carries, 1) * kr, ypc[None, :] / kr) * eff[:, None] - 0.15 * carries, 0.0)
        rw = carries * np.array([max(r.td_rate, 1e-3) for r in rush])[None, :] + 1e-6
        rtd = rng.multinomial(rush_td, rw / rw.sum(axis=1, keepdims=True))
        if t.qb_key:
            q = t.qb_key
            st[("player", q, "pass_td")] = pass_td
            st[("player", q, "pass_yds")] = yards.sum(axis=1)
            st[("player", q, "pass_att")] = att.astype(float)
            st[("player", q, "pass_comp")] = receptions.sum(axis=1).astype(float)
            st[("player", q, "pass_int")] = rng.poisson(att * t.int_rate * np.exp(-0.2 * z)).astype(float)
        for j, r in enumerate(recs):
            if r.key.startswith("_"):
                continue
            st[("player", r.key, "rec_yds")] = yards[:, j]
            st[("player", r.key, "receptions")] = receptions[:, j].astype(float)
            st[("player", r.key, "rec_td")] = rec_td[:, j].astype(float)
        for j, r in enumerate(rush):
            if r.key.startswith("_"):
                continue
            st[("player", r.key, "rush_yds")] = ryds[:, j]
            st[("player", r.key, "rush_att")] = carries[:, j].astype(float)
            st[("player", r.key, "rush_td")] = rtd[:, j].astype(float)
        keys = {r.key for r in recs if not r.key.startswith("_")} | {r.key for r in rush if not r.key.startswith("_")}
        zero = np.zeros(n)
        for key in keys:
            rec_td_k = st.get(("player", key, "rec_td"), zero)
            rush_td_k = st.get(("player", key, "rush_td"), zero)
            st[("player", key, "anytime_td")] = rec_td_k + rush_td_k
            st[("player", key, "rush_rec_yds")] = st.get(("player", key, "rec_yds"), zero) + st.get(("player", key, "rush_yds"), zero)


def rank_uniform(x: np.ndarray, rng: np.random.Generator) -> np.ndarray:
    """Probability-integral transform with random tie-breaking (exact uniform even for counts)."""
    vals, inv, counts = np.unique(x, return_inverse=True, return_counts=True)
    n = x.shape[0]
    hi = np.cumsum(counts) / n
    lo = hi - counts / n
    v = rng.random(n)
    return lo[inv] + v * (hi[inv] - lo[inv])
