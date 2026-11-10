"""FairModel: blended leg probabilities and simulator-based joint probabilities for any same-game pair.

For each game with legs:
1. Fit each team's scoring (mu_td, phi_fg) to its Kalshi team-total ladder (game total / spread as a
   fallback), with the scoring variance from nflverse history.
2. Build receivers / rushers / QB from nflverse usage (Kalshi players matched by team+jersey or name).
3. Simulate an ensemble (base + perturbed hyperparameters) so the lower bound reflects model
   uncertainty, not just Monte Carlo noise.
4. Each leg -> a simulated stat. Its target probability blends Kalshi and the raw model by liquidity.
   Events are defined in rank space (u >= 1 - p_target), so every leg's marginal equals its target and the
   simulator supplies the dependence. Joints for all legs of a game come from one matrix product.
   Exception: when a tail leg's target is more than 1.5x the simulator's own marginal, the rank event
   would pull in many simulated games where the stat did NOT happen, diluting (or inventing) dependence.
   Those pairs use P_sim(other | tail stat happened) x target instead.
5. History calibration (``learning/calibration.py``) shifts each family's YES/YES conditional by the
   log-odds gap to nflverse (``family_shift``) and widens its band by the residual (``family_u``).

Legs the simulator doesn't cover (first TD scorer, fantasy points, ...) return None, and prescore falls
back to the copula prior for those pairs.
"""

from __future__ import annotations

import logging
import re
import time
import zlib
from dataclasses import dataclass, field, replace
from typing import Any

import numpy as np

from combo_edge.candidates import Candidate
from combo_edge.fair.blend import blend, disagrees, kalshi_weight
from combo_edge.fair.history import ScoringHyper
from combo_edge.fair.player_models import UsageBook
from combo_edge.fair.simulator import GameSim, ReceiverSpec, RusherSpec, SimParams, TeamSpec, fit_team_scoring, rank_uniform
from combo_edge.sports.base import Leg
from combo_edge.universe import Universe

log = logging.getLogger(__name__)
_MARGIN = re.compile(r"(\d+)\s*to\s*(\d+)")
_PLUS = re.compile(r"(\d+)\s*\+")
PLAYER_KINDS = {"pass_td", "pass_yds", "pass_att", "pass_comp", "pass_int", "rec_yds", "receptions", "rush_yds", "rush_att", "anytime_td", "rush_rec_yds"}
ENSEMBLE = [
    {},
    {"sigma": 1.25, "alpha_targets": 0.5, "rho": +0.08},
    {"sigma": 0.80, "alpha_targets": 2.0, "rho": -0.06, "kappa_eff": 1.6},
]


def stat_key(leg: Leg) -> tuple[Any, ...] | None:
    k, t = leg.kind, leg.team
    if leg.scope == "player":
        if leg.position == "DST" and k == "anytime_td":
            return ("team", t, "dst_td")
        if k in PLAYER_KINDS and leg.player_key:
            return ("player", leg.player_key, k)
        return None
    table = {
        "team_total": ("team", t, "points"), "h1_team_total": ("team", t, "h1_points"),
        "spread": ("team", t, "margin"), "h1_spread": ("team", t, "h1_margin"), "q1_spread": ("team", t, "q1_margin"),
        "game_total": ("game", "total"), "h1_total": ("game", "h1_total"), "q1_total": ("game", "q1_total"),
        "tie": ("game", "tie"), "overtime": ("game", "tie"),
    }
    if k in table:
        return table[k]
    if k in ("ml", "h1_ml", "q1_ml"):
        if leg.outcome == "TIE":
            return ("game", {"ml": "tie", "h1_ml": "h1_tie", "q1_ml": "q1_tie"}[k])
        prefix = {"ml": "", "h1_ml": "h1_", "q1_ml": "q1_"}[k]
        return ("team", leg.outcome or t, f"{prefix}win")
    if k == "win_margin":
        o = (leg.outcome or "").lower()
        if o == "tie":
            return ("game", "tie")
        m = _MARGIN.search(o)
        if m and t:
            return ("margin_band", t, int(m.group(1)), int(m.group(2)))
        m = _PLUS.search(o)
        if m and t:
            return ("margin_band", t, int(m.group(1)), 999)
    return None


def event_threshold(leg: Leg, key: tuple[Any, ...]) -> float:
    if key[0] == "margin_band" or key[-1] in ("win", "h1_win", "q1_win", "tie", "h1_tie", "q1_tie"):
        return 0.5
    return float(leg.threshold if leg.threshold is not None else 0.5)


@dataclass
class GameModel:
    game_code: str
    members: list[dict[tuple[Any, ...], np.ndarray]] = field(default_factory=list)
    uniforms: list[dict[tuple[Any, ...], np.ndarray]] = field(default_factory=list)
    n: int = 0
    fit_rmse: dict[str, float] = field(default_factory=dict)

    def stat(self, m: int, key: tuple[Any, ...]) -> np.ndarray | None:
        if key[0] == "margin_band":
            base = self.members[m].get(("team", key[1], "margin"))
            if base is None:
                return None
            return ((base >= key[2]) & (base <= key[3])).astype(float)
        return self.members[m].get(key)

    def u(self, m: int, key: tuple[Any, ...]) -> np.ndarray | None:
        cache = self.uniforms[m]
        if key not in cache:
            x = self.stat(m, key)
            if x is None:
                return None
            rng = np.random.default_rng(zlib.crc32(repr((self.game_code, key, m)).encode()))  # stable across processes
            cache[key] = rank_uniform(x, rng)
        return cache[key]


class FairModel:
    def __init__(self, universe: Universe, usage: UsageBook | None = None, hyper: ScoringHyper | None = None, params: SimParams | None = None):
        self.version = "sim-v1"
        self.uni = universe
        self.usage = usage
        self.hyper = hyper or ScoringHyper()
        self.base = params or SimParams()
        if hyper is not None:
            from combo_edge.fair.simulator import sigma_for_sd

            self.base = replace(self.base, rho=float(np.clip(hyper.resid_corr, 0.0, 0.35)), sigma=max(0.08, sigma_for_sd(hyper.resid_sd)))
        self.games: dict[str, GameModel | None] = {}
        self.leg_info: dict[str, tuple[float | None, float | None, float, bool]] = {}  # model_p, blended, w, flagged
        self.flags: list[dict[str, Any]] = []
        self.timings: dict[str, float] = {}
        self.u_struct = 0.15
        self._flag_by_stat: dict[tuple[Any, ...], tuple[float, dict[str, Any]]] = {}
        self.family_u: dict[str, float] = {}  # extra band per family from truth-model calibration (Phase 2.5)
        self.family_shift: dict[str, float] = {}  # log-odds shift of P(B|A) per family toward nflverse history
        self.tail_remap_ratio = 1.5

    # ------------------------------------------------------------------ build
    def _team_spec(self, team: str, legs: list[Leg], sigma: float) -> TeamSpec:
        ladder = [(l.threshold, l.p_est, kalshi_weight(l)) for l in legs if l.kind == "team_total" and l.team == team and not l.protected
                  and l.threshold is not None and l.p_est is not None]
        hint = None
        if len(ladder) < 2:
            hint = self._mean_from_lines(team, legs)
        mu, phi, rmse = fit_team_scoring([(float(x), float(p), max(w, 0.05)) for x, p, w in ladder], sigma, mean_hint=hint)
        spec = TeamSpec(team, mu_td=mu, phi_fg=phi)
        spec._fit_rmse = rmse  # type: ignore[attr-defined]
        if self.usage is not None:
            tu = self.usage.team(team)
            spec.pass_share = float(np.clip(tu.pass_td_share, 0.4, 0.8))
            spec.pass_att, spec.carries, spec.int_rate = tu.pass_att, tu.carries, tu.int_rate
        players: dict[str, Leg] = {}
        for l in legs:
            if l.scope == "player" and l.team == team and l.player_key and l.position != "DST":
                players.setdefault(l.player_key, l)
        qbs = [p for p in players.values() if p.position == "QB"]
        qb = max(qbs, key=lambda q: sum(1 for l in legs if l.player_key == q.player_key), default=None)
        spec.qb_key = qb.player_key if qb else None
        recv_share = 0.0
        for key, l in players.items():
            pu = self.usage.player(team, l.player_name, l.jersey, l.position) if self.usage else None
            kinds = {x.kind for x in legs if x.player_key == key}
            if pu is None:
                continue
            if kinds & {"rec_yds", "receptions", "anytime_td", "rush_rec_yds"} and l.position != "QB" and pu.target_share > 0.005:
                spec.receivers.append(ReceiverSpec(key, pu.target_share, pu.catch_rate, pu.ypr, pu.td_per_target))
                recv_share += pu.target_share
            if kinds & {"rush_yds", "rush_att", "anytime_td", "rush_rec_yds"} and (pu.carry_share > 0.01 or l.position in ("RB", "QB")):
                spec.rushers.append(RusherSpec(key, max(pu.carry_share, 0.03), pu.ypc, pu.td_per_carry))
        if spec.receivers:
            spec.receivers.append(ReceiverSpec("_other", max(0.08, 1.0 - recv_share), 0.65, 10.5, 0.05))
        rush_share = sum(r.share for r in spec.rushers)
        if spec.rushers:
            spec.rushers.append(RusherSpec("_other_rb", max(0.05, 1.0 - rush_share), 4.0, 0.03))
        return spec

    def _mean_from_lines(self, team: str, legs: list[Leg]) -> float:
        def main(kind: str, t: str | None = None) -> float | None:
            rungs = sorted((l.threshold, l.p_est) for l in legs if l.kind == kind and (t is None or l.team == t) and l.threshold is not None and l.p_est is not None)
            for (x0, p0), (x1, p1) in zip(rungs, rungs[1:]):
                if p0 >= 0.5 >= p1 and p0 != p1:
                    return x0 + (p0 - 0.5) / (p0 - p1) * (x1 - x0)
            return None

        total = main("game_total")
        spread = main("spread", team)
        opp_spread = None
        if spread is None:
            others = [l for l in legs if l.kind == "spread" and l.team and l.team != team]
            if others:
                opp_spread = main("spread", others[0].team)
        if total is None:
            return 22.0
        s = spread if spread is not None else (-opp_spread if opp_spread is not None else 0.0)
        return (total + s) / 2.0

    def prepare(self) -> None:
        t0 = time.time()
        for code, legs in self.uni.legs_by_game.items():
            game = self.uni.games.get(code)
            if game is None or not legs:
                continue
            try:
                self.games[code] = self._build_game(code, game.away_team, game.home_team, legs)
            except Exception:  # noqa: BLE001 - a game the model can't build falls back to copula priors
                log.exception("fair model failed for %s", code)
                self.games[code] = None
        self.timings["prepare_s"] = time.time() - t0
        for leg in self.uni.legs.values():
            self._leg(leg)

    def _build_game(self, code: str, away: str, home: str, legs: list[Leg]) -> GameModel | None:
        if not any(l.kind == "team_total" for l in legs) and not any(l.kind in ("game_total", "spread") for l in legs):
            return None
        gm = GameModel(code)
        for i, delta in enumerate(ENSEMBLE):
            p = replace(self.base, seed=self.base.seed + 101 * i)
            for k, v in delta.items():
                cur = getattr(p, k)
                p = replace(p, **{k: (cur * v if k in ("sigma", "alpha_targets", "kappa_eff") else cur + v)})
            p = replace(p, rho=float(np.clip(p.rho, 0.0, 0.5)))
            a, h = self._team_spec(away, legs, p.sigma), self._team_spec(home, legs, p.sigma)
            if i == 0:
                gm.fit_rmse = {away: getattr(a, "_fit_rmse", 0.0), home: getattr(h, "_fit_rmse", 0.0)}
            res = GameSim(code, a, h, p).run()
            gm.members.append(res.stats)
            gm.uniforms.append({})
            gm.n = res.n
        return gm

    # ------------------------------------------------------------------ queries
    def _leg(self, leg: Leg) -> tuple[float | None, float | None, float, bool]:
        if leg.ticker in self.leg_info:
            return self.leg_info[leg.ticker]
        gm = self.games.get(leg.game_code)
        p_k = leg.p_market()
        model_p = None
        key = stat_key(leg)
        if gm is not None and key is not None:
            x = gm.stat(0, key)
            if x is not None:
                model_p = float((x > event_threshold(leg, key)).mean())
        w = kalshi_weight(leg)
        p_b = blend(p_k, model_p, w)
        if p_b is not None:
            p_b = float(np.clip(p_b, 1e-4, 1 - 1e-4))
        flag = leg.scope == "player" and (leg.p_source == "mid") and disagrees(p_k, model_p)
        if flag:
            key2 = (leg.player_key, leg.kind, leg.protected)
            gap = abs((model_p or 0) - (p_k or 0))
            prev = self._flag_by_stat.get(key2)
            if prev is None or gap > prev[0]:
                entry = {"ticker": leg.ticker, "leg": leg.describe(), "kalshi": p_k, "model": model_p, "weight_kalshi": w}
                if prev is not None:
                    self.flags.remove(prev[1])
                self.flags.append(entry)
                self._flag_by_stat[key2] = (gap, entry)
        out = (model_p, p_b, w, flag)
        self.leg_info[leg.ticker] = out
        return out

    def leg_prob(self, leg: Leg) -> tuple[float | None, float | None]:
        model_p, blended, _w, _f = self._leg(leg)
        return model_p, blended

    def joint_many(self, cands: list[Candidate]) -> list[tuple[float, float] | None]:
        out: list[tuple[float, float] | None] = [None] * len(cands)
        by_game: dict[str, list[int]] = {}
        for i, c in enumerate(cands):
            if len(c.legs) == 2 and self.games.get(c.game_code) is not None:
                by_game.setdefault(c.game_code, []).append(i)
        t0 = time.time()
        for code, idxs in by_game.items():
            gm = self.games[code]
            assert gm is not None
            legs: dict[str, Leg] = {}
            for i in idxs:
                for s in cands[i].legs:
                    legs[s.leg.ticker] = s.leg
            usable = []
            for t, leg in legs.items():
                key = stat_key(leg)
                _, p_b, _, _ = self._leg(leg)
                if key is not None and p_b is not None and gm.stat(0, key) is not None:
                    usable.append((t, key, p_b))
            if not usable:
                continue
            pos = {t: j for j, (t, _, _) in enumerate(usable)}
            # tail legs whose target is far above the simulator's own marginal: condition on the raw stat
            inflated = [j for j, (t, key, p_b) in enumerate(usable)
                        if (mp := self._leg(legs[t])[0]) is not None and 0 < mp and p_b > self.tail_remap_ratio * mp]
            ipos = {j: r for r, j in enumerate(inflated)}
            joints, tails = [], []
            for m in range(len(gm.members)):
                ev = np.zeros((len(usable), gm.n), dtype=np.float32)
                for j, (_, key, p_b) in enumerate(usable):
                    u = gm.u(m, key)
                    if u is not None:
                        ev[j] = u >= 1.0 - p_b
                joints.append((ev @ ev.T) / gm.n)
                if inflated:
                    raw = np.zeros((len(inflated), gm.n), dtype=np.float32)
                    for r, j in enumerate(inflated):
                        t, key, _ = usable[j]
                        x = gm.stat(m, key)
                        if x is not None:
                            raw[r] = x > event_threshold(legs[t], key)
                    hits = raw.sum(axis=1, keepdims=True)
                    tails.append(np.where(hits > 0, (raw @ ev.T) / np.maximum(hits, 1.0), 0.0))  # P_sim(other | tail stat)
            n = gm.n
            shift_of = self.family_shift
            for i in idxs:
                (a, b) = cands[i].legs
                if a.leg.ticker not in pos or b.leg.ticker not in pos:
                    continue
                ja, jb = pos[a.leg.ticker], pos[b.leg.ticker]
                pa, pb = usable[ja][2], usable[jb][2]
                tail = min((j for j in (ja, jb) if j in ipos), key=lambda j: usable[j][2], default=None)
                other = jb if tail == ja else ja
                shift = shift_of.get(cands[i].family, 0.0) if a.side == "yes" and b.side == "yes" else 0.0
                vals = []
                for mi, S in enumerate(joints):
                    s = min(float(S[ja, jb]), pa, pb)  # rank events can overshoot min(pa, pb) by MC noise
                    if tail is not None:
                        s = min(usable[tail][2] * float(tails[mi][ipos[tail], other]), pa, pb)
                    if shift:
                        s = min(pa * conditional_shift(s / pa, shift), pb)
                    if a.side == "yes" and b.side == "yes":
                        v = s
                    elif a.side == "yes":
                        v = pa - s
                    elif b.side == "yes":
                        v = pb - s
                    else:
                        v = 1.0 - pa - pb + s
                    vals.append(max(v, 0.0))
                fair = vals[0]
                se = (max(fair * (1 - fair), 1e-9) / n) ** 0.5
                # Ensemble spread + MC error, floored by a structural band (the model can be wrong in ways
                # the ensemble doesn't span). Wider when a leg's probability leans on the model.
                w_min = min(self._leg(a.leg)[2], self._leg(b.leg)[2])
                u = self.u_struct + (0.10 if w_min < 0.5 else 0.0) + self.family_u.get(cands[i].family, 0.0)
                lower = max(0.0, min(min(vals) - 2 * se, fair * (1.0 - u)))
                out[i] = (fair, lower)
        self.timings["joint_s"] = self.timings.get("joint_s", 0.0) + time.time() - t0
        return out


def conditional_shift(cond: float, shift: float) -> float:
    """Move a conditional probability by ``shift`` in log-odds."""
    c = min(max(cond, 1e-6), 1 - 1e-6)
    return float(1.0 / (1.0 + np.exp(-(np.log(c / (1 - c)) + shift))))


def tail_joint(other_event: np.ndarray, tail_raw: np.ndarray, tail_target: float) -> float:
    """P(tail and other) = P_sim(other | the tail stat actually happened in the simulation) x tail target.

    Used instead of the rank-space event when the tail's target is far above the simulator's marginal."""
    hit = np.asarray(tail_raw, dtype=bool)
    if not hit.any():
        return 0.0
    return float(tail_target * np.asarray(other_event, dtype=float)[hit].mean())


def build_fair_model(universe: Universe) -> FairModel | None:
    """Factory used by the scan hooks. Never raises: returns None if nflverse data is unavailable."""
    from combo_edge.fair.history import scoring_hyperparams, team_games

    try:
        usage = UsageBook.load()
    except Exception as exc:  # noqa: BLE001
        log.warning("usage priors unavailable (%s); simulator uses position means", exc)
        usage = None
    hyper = None
    try:
        hyper = scoring_hyperparams(team_games())
    except Exception as exc:  # noqa: BLE001
        log.warning("history unavailable (%s); using default scoring hyperparameters", exc)
    fm = FairModel(universe, usage, hyper)
    fm.prepare()
    return fm
