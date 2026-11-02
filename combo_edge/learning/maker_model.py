"""Maker model (learning loop 1): predicts the quoted ratio (quoted payout / product of leg payouts,
fee-inclusive) before probing, so the RFQ budget goes where predicted edge is highest.

Every quote is a label, so feedback is fast. Gradient-boosted trees (scikit-learn HistGradientBoosting,
in place of LightGBM by your toolchain choice) on log(ratio), with features:
  family, leg kinds, relation, tailness, sides, thresholds, fair/market probability of each leg, the
  game's team total / total / spread, hours to start, day of week, hour, leg spread / depth / volume /
  open interest / move since open, game volume, whether the combo already existed, RFQ feed demand,
  RFQ size; plus (full variant only) number of makers and best-vs-second quote dispersion.
The pre-probe variant (used for allocation) excludes the two post-quote features and uses the family's
historical maker count instead. Recent data weighs more (half-life ``maker_half_life_days``) because
makers adapt. Retrained on every Run; falls back to hierarchical means while data is thin.
"""

from __future__ import annotations

import logging
import math
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from combo_edge import timeutil
from combo_edge.candidates import Candidate
from combo_edge.learning.labels import edge_labels
from combo_edge.settings import data_dir
from combo_edge.storage import Database, dumps, loads
from combo_edge.storage import schema as S

log = logging.getLogger(__name__)
CAT = ["family_base", "kind_a", "kind_b", "relation", "tail_a", "tail_b", "sides", "dow"]
NUM_PRE = ["thr_a", "thr_b", "p_a", "p_b", "log_leg_product", "team_total_main", "game_total_main", "spread_main", "hours_to_start",
           "hour", "spread_a", "spread_b", "depth_a", "depth_b", "vol_a", "vol_b", "oi_a", "oi_b", "move_a", "move_b", "game_volume",
           "existed", "feed_demand", "target_cost", "family_makers"]
NUM_FULL = NUM_PRE + ["n_makers", "quote_dispersion"]
MIN_HGB = 150
MIN_ANY = 15


def family_base(fam: str | None) -> str:
    f = fam or "?"
    if f.startswith("explore:") or f.startswith("feed:"):
        return f.split(":", 1)[0]
    return f.removeprefix("no_")


def _split_l1(l1: str | None) -> tuple[str, str, str]:
    if not l1 or "|" not in l1:
        return ("?", "?", "?")
    pair, rel = l1.rsplit("|", 1)
    a, _, b = pair.partition("×")
    return (a or "?", b or "?", rel)


def _split_l2(l2: str | None) -> tuple[str, str, str]:
    if not l2 or "|" not in l2:
        return ("?", "?", "?")
    tails, sides = l2.rsplit("|", 1)
    a, _, b = tails.partition("/")
    return (a or "?", b or "?", sides)


def _leg_feats(snap: dict[str, Any] | None, suffix: str) -> dict[str, float]:
    s = snap or {}
    return {f"spread_{suffix}": s.get("spread"), f"depth_{suffix}": (s.get("yes_bid_size") or 0) + (s.get("yes_ask_size") or 0) if s else None,
            f"vol_{suffix}": s.get("volume"), f"oi_{suffix}": s.get("open_interest"), f"move_{suffix}": s.get("move_since_open")}


def training_rows(db: Database, env: str) -> list[dict[str, Any]]:
    labels = edge_labels(db, env)
    if not labels:
        return []
    legs = {r["ticker"]: r for r in db.fetchall("SELECT ticker, kind, threshold FROM legs")}
    snaps: dict[tuple[int, str], dict[str, Any]] = {}
    for rid in {l["run_id"] for l in labels if l.get("run_id") is not None}:
        for s in db.fetchall("SELECT * FROM leg_snapshots WHERE run_id=:r", {"r": rid}):
            snaps[(rid, s["ticker"])] = s
    fam_makers = _family_makers(labels)
    rows = []
    for l in labels:
        if not l.get("ratio") or l["ratio"] <= 0:
            continue
        lg = loads(l.get("legs"), []) or []
        if len(lg) != 2:
            continue
        (ta, _sa), (tb, _sb) = lg
        ka, kb, rel = _split_l1(l.get("stratum_l1"))
        tl_a, tl_b, sides = _split_l2(l.get("stratum_l2"))
        sa, sb = snaps.get((l["run_id"], ta)), snaps.get((l["run_id"], tb))
        row = {"family_base": family_base(l.get("family")), "kind_a": ka, "kind_b": kb, "relation": rel, "tail_a": tl_a, "tail_b": tl_b,
               "sides": sides, "dow": l.get("dow"), "thr_a": (legs.get(ta) or {}).get("threshold"), "thr_b": (legs.get(tb) or {}).get("threshold"),
               "p_a": l.get("p_a"), "p_b": l.get("p_b"), "log_leg_product": math.log(l["leg_payout_product"]) if l.get("leg_payout_product") else None,
               "team_total_main": l.get("team_total_main"), "game_total_main": l.get("game_total_main"), "spread_main": l.get("spread_main"),
               "hours_to_start": l.get("hours_to_start"), "hour": l.get("hour"), "game_volume": (sa or {}).get("game_volume"),
               "existed": 1.0 if l.get("existed") else 0.0, "feed_demand": l.get("feed_demand_pair") or 0, "target_cost": l.get("target_cost"),
               "family_makers": fam_makers.get(family_base(l.get("family")), 1.5), "n_makers": l.get("n_makers"),
               "quote_dispersion": l.get("quote_spread"), "y": math.log(l["ratio"]), "ts": l.get("created_at") or 0.0,
               **_leg_feats(sa, "a"), **_leg_feats(sb, "b")}
        rows.append(row)
    return rows


def _family_makers(labels: list[dict[str, Any]]) -> dict[str, float]:
    acc: dict[str, list[float]] = defaultdict(list)
    for l in labels:
        acc[family_base(l.get("family"))].append(float(l.get("n_makers") or 0))
    return {k: sum(v) / len(v) for k, v in acc.items()}


@dataclass
class Encoder:
    maps: dict[str, dict[str, int]] = field(default_factory=dict)

    def fit(self, rows: list[dict[str, Any]]) -> "Encoder":
        for c in CAT:
            vals = sorted({str(r.get(c)) for r in rows})
            self.maps[c] = {v: i for i, v in enumerate(vals)}
        return self

    def matrix(self, rows: list[dict[str, Any]], num: list[str]) -> np.ndarray:
        X = np.full((len(rows), len(CAT) + len(num)), np.nan)
        for i, r in enumerate(rows):
            for j, c in enumerate(CAT):
                X[i, j] = self.maps[c].get(str(r.get(c)), np.nan)
            for j, c in enumerate(num):
                v = r.get(c)
                X[i, len(CAT) + j] = np.nan if v is None else float(v)
        return X


class HierarchicalMeans:
    """Shrunk mean log-ratio by Level-1 kind pair -> family -> global."""

    def __init__(self, rows: list[dict[str, Any]], k: float = 10.0):
        ys = [r["y"] for r in rows]
        self.g = sum(ys) / len(ys) if ys else 0.0
        self.k = k
        self.fam = self._means(rows, lambda r: r["family_base"], self.g)
        self.pair = {key: v for key, v in self._means(rows, lambda r: (r["family_base"], r["kind_a"], r["kind_b"]), None).items()}

    def _means(self, rows: list[dict[str, Any]], key: Any, prior: float | None) -> dict[Any, float]:
        acc: dict[Any, list[float]] = defaultdict(list)
        for r in rows:
            acc[key(r)].append(r["y"])
        out = {}
        for kk, v in acc.items():
            base = self.g if prior is None else prior
            out[kk] = (sum(v) + self.k * base) / (len(v) + self.k)
        return out

    def predict(self, rows: list[dict[str, Any]]) -> np.ndarray:
        out = []
        for r in rows:
            f = self.fam.get(r["family_base"], self.g)
            p = self.pair.get((r["family_base"], r["kind_a"], r["kind_b"]))
            out.append(p if p is not None else f)
        return np.array(out)


class MakerModel:
    def __init__(self, kind: str, estimator: Any, encoder: Encoder | None, num: list[str], n_train: int, fam_makers: dict[str, float]):
        self.kind = kind
        self.estimator = estimator
        self.encoder = encoder
        self.num = num
        self.n_train = n_train
        self.fam_makers = fam_makers
        self.universe: Any = None
        self.feed: dict[str, int] = {}

    def predict_log_ratio(self, rows: list[dict[str, Any]]) -> np.ndarray:
        if self.kind == "hgb":
            return self.estimator.predict(self.encoder.matrix(rows, self.num))
        return self.estimator.predict(rows)

    def predict_ratio(self, cands: list[Candidate]) -> list[float | None]:
        if not cands:
            return []
        rows = [candidate_row(c, self.universe, self.fam_makers, self.feed) for c in cands]
        ok = [i for i, r in enumerate(rows) if r is not None]
        out: list[float | None] = [None] * len(cands)
        if not ok:
            return out
        pred = self.predict_log_ratio([rows[i] for i in ok])  # type: ignore[misc]
        for i, v in zip(ok, pred):
            out[i] = float(np.exp(np.clip(v, -3.0, 2.0)))
        return out


def candidate_row(c: Candidate, uni: Any, fam_makers: dict[str, float], feed: dict[str, int]) -> dict[str, Any] | None:
    if len(c.legs) != 2 or not c.stratum_l1:
        return None
    from combo_edge.gamelines import game_lines

    ka, kb, rel = _split_l1(c.stratum_l1)
    tl_a, tl_b, sides = _split_l2(c.stratum_l2)
    a, b = c.legs[0].leg, c.legs[1].leg
    lines: dict[str, Any] = {}
    team = a.team or b.team
    if uni is not None and c.game_code in uni.games:
        g = uni.games[c.game_code]
        cache = getattr(uni, "_lines_cache", None)
        if cache is None:
            cache = {}
            uni._lines_cache = cache  # type: ignore[attr-defined]
        if c.game_code not in cache:
            cache[c.game_code] = game_lines(uni.legs_by_game.get(c.game_code, []), (g.away_team, g.home_team))
        lines = cache[c.game_code]
    lt = timeutil.local_dt(timeutil.now())

    def lf(leg: Any, suffix: str) -> dict[str, Any]:
        return {f"spread_{suffix}": leg.spread(), f"depth_{suffix}": (leg.yes_bid_size or 0) + (leg.yes_ask_size or 0),
                f"vol_{suffix}": leg.volume, f"oi_{suffix}": leg.open_interest, f"move_{suffix}": None}

    return {"family_base": family_base(c.family), "kind_a": ka, "kind_b": kb, "relation": rel, "tail_a": tl_a, "tail_b": tl_b, "sides": sides,
            "dow": lt.strftime("%a"), "thr_a": a.threshold, "thr_b": b.threshold, "p_a": c.p_legs[0] if c.p_legs else None,
            "p_b": c.p_legs[1] if len(c.p_legs) > 1 else None,
            "log_leg_product": math.log(c.leg_payout_product) if c.leg_payout_product else None,
            "team_total_main": (lines.get("team_total") or {}).get(team) if team else None, "game_total_main": lines.get("total"),
            "spread_main": (lines.get("spread") or {}).get(team) if team else None,
            "hours_to_start": uni.hours_to_start(c.game_code) if uni is not None else None, "hour": lt.hour,
            "game_volume": uni.game_volume.get(c.game_code) if uni is not None else None, "existed": 1.0 if c.existed else 0.0,
            "feed_demand": feed.get(c.signature, 0), "target_cost": None, "family_makers": fam_makers.get(family_base(c.family), 1.5),
            **lf(a, "a"), **lf(b, "b")}


def train(db: Database, env: str, half_life_days: float = 7.0, now_ts: float | None = None) -> MakerModel | None:
    rows = training_rows(db, env)
    if len(rows) < MIN_ANY:
        return None
    now_ts = now_ts or timeutil.now()
    rows.sort(key=lambda r: r["ts"])
    fam_makers = _family_makers([{"family": r["family_base"], "n_makers": r.get("n_makers")} for r in rows])
    w = np.array([0.5 ** (max(0.0, now_ts - r["ts"]) / 86400.0 / half_life_days) for r in rows])
    y = np.array([r["y"] for r in rows])
    cut = int(len(rows) * 0.8)
    metrics: dict[str, Any] = {"n": len(rows)}
    if len(rows) >= MIN_HGB:
        from sklearn.ensemble import HistGradientBoostingRegressor

        def fit(rws: list[dict[str, Any]], ww: np.ndarray, yy: np.ndarray) -> tuple[Any, Encoder]:
            enc = Encoder().fit(rws)
            est = HistGradientBoostingRegressor(max_iter=300, learning_rate=0.05, max_leaf_nodes=31, min_samples_leaf=20,
                                                l2_regularization=1.0, categorical_features=list(range(len(CAT))), random_state=0)
            est.fit(enc.matrix(rws, NUM_PRE), yy, sample_weight=ww)
            return est, enc

        est, enc = fit(rows[:cut], w[:cut], y[:cut])
        pred = est.predict(enc.matrix(rows[cut:], NUM_PRE))
        base = HierarchicalMeans(rows[:cut]).predict(rows[cut:])
        metrics.update(mae=float(np.mean(np.abs(pred - y[cut:]))), mae_baseline=float(np.mean(np.abs(base - y[cut:]))),
                       bias_recent=float(np.mean(y[cut:] - pred)))
        est, enc = fit(rows, w, y)
        model = MakerModel("hgb", est, enc, NUM_PRE, len(rows), fam_makers)
    else:
        hm = HierarchicalMeans(rows[:cut] if cut >= MIN_ANY else rows)
        if cut < len(rows) and cut >= MIN_ANY:
            pred = hm.predict(rows[cut:])
            metrics.update(mae=float(np.mean(np.abs(pred - y[cut:]))), bias_recent=float(np.mean(y[cut:] - pred)))
        model = MakerModel("means", HierarchicalMeans(rows), None, NUM_PRE, len(rows), fam_makers)
    metrics["mean_ratio"] = float(np.exp(np.mean(y)))
    _record(db, env, model, metrics)
    return model


def _record(db: Database, env: str, model: MakerModel, metrics: dict[str, Any]) -> None:
    path = None
    try:
        import joblib

        d = data_dir() / "models"
        d.mkdir(parents=True, exist_ok=True)
        path = str(d / f"maker_{env}_{int(time.time())}.joblib")
        joblib.dump(model, path)
    except Exception as exc:  # noqa: BLE001
        log.warning("could not save maker model: %s", exc)
    prev = db.fetchone("SELECT metrics FROM model_versions WHERE kind='maker' ORDER BY id DESC LIMIT 1")
    db.insert(S.model_versions, {"kind": "maker", "created_at": timeutil.now(), "n_train": model.n_train, "metrics": metrics,
                                 "params": {"estimator": model.kind, "features": CAT + model.num}, "path": path})
    prev_m = loads((prev or {}).get("metrics"), {}) or {}
    bias = metrics.get("bias_recent")
    summary = "not enough data" if bias is None else (
        f"recent quotes {'richer' if bias > 0 else 'stingier'} than the model by {100 * (math.exp(abs(bias)) - 1):.1f}% in ratio"
        + (f" (was {100 * (math.exp(abs(prev_m['bias_recent'])) - 1):.1f}%)" if prev_m.get("bias_recent") is not None else ""))
    db.meta_set(f"maker_drift:{env}", {"bias": bias, "mae": metrics.get("mae"), "mae_baseline": metrics.get("mae_baseline"), "summary": summary,
                                        "ts": timeutil.now()})
