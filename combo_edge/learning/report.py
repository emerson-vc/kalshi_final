"""End-of-exploration report (Phase 1.5): top strata, timing, liquidity, family keep / drop / add."""

from __future__ import annotations

from pathlib import Path

from combo_edge import timeutil
from combo_edge.candidates import FAMILIES
from combo_edge.learning.labels import HOUR_BUCKETS, LIQ_BUCKETS, edge_labels, group_edges
from combo_edge.settings import RuntimeSettings, data_dir
from combo_edge.storage import Database


def _fmt(r: dict) -> str:
    lo, hi = r["lo"], r["hi"]
    ci = f"[{100 * lo:+.1f}%, {100 * hi:+.1f}%]" if lo == lo else "[n<2]"
    return f"{100 * r['mean']:+.1f}% {ci} n={r['n']}"


def build_report(db: Database, env: str, st: RuntimeSettings) -> str:
    labels = edge_labels(db, env)
    lines = [f"# Exploration report ({env}, {timeutil.fmt_local(timeutil.now())})", "",
             f"{len(labels)} quote labels. Edge = fair x quoted payout - 1 (fees included), clipped to [-100%, +300%] for means.", ""]
    strata = [s for s in group_edges([l for l in labels if l.get("stratum_l1")], "stratum_l1") if s["n"] >= 5]
    strata.sort(key=lambda s: -s["mean"])
    lines += ["## Top 20 strata by mean edge (n >= 5)", ""] + [f"- {s['group']}: {_fmt(s)}" for s in strata[:20]] + [""]
    lines += ["## Edge vs hours to kickoff", ""] + [f"- {r['group']}: {_fmt(r)}" for r in group_edges(labels, "hours_bucket", [b[2] for b in HOUR_BUCKETS])] + [""]
    lines += ["## Edge vs leg liquidity (worst leg's spread)", ""] + [f"- {r['group']}: {_fmt(r)}" for r in group_edges(labels, "liq_bucket", [b[2] for b in LIQ_BUCKETS] + ["no book"])] + [""]
    fam = group_edges(labels, "family")
    keep = [r for r in fam if not r["group"].startswith(("explore:", "feed:")) and r["n"] >= 10 and r["lo"] == r["lo"] and r["lo"] > 0]
    drop = [r for r in fam if r["group"] in FAMILIES and r["n"] >= 10 and r["hi"] == r["hi"] and r["hi"] < 0]
    add = [s for s in strata if s["n"] >= st.stratum_target_quotes and s["lo"] == s["lo"] and s["lo"] > 0.05]
    lines += ["## Template families", "", "Keep (CI above zero):"] + [f"- {r['group']}: {_fmt(r)}" for r in keep] + [""]
    lines += ["Drop (CI below zero):"] + [f"- {r['group']}: {_fmt(r)}" for r in drop] + [""]
    lines += ["Add (strata with >= target quotes and CI above +5%; promotion makes these templates automatically):"]
    lines += [f"- {s['group']}: {_fmt(s)}" for s in add[:15]] + [""]
    lines += ["Next: the default budget switches to about 85% exploit / 15% explore."]
    return "\n".join(lines)


def write_exploration_report(db: Database, env: str, st: RuntimeSettings) -> str:
    text = build_report(db, env, st)
    d = data_dir() / "reports"
    d.mkdir(parents=True, exist_ok=True)
    path: Path = d / f"exploration_{env}_{timeutil.local_day(timeutil.now())}.md"
    path.write_text(text)
    db.meta_set(f"exploration_report:{env}", {"path": str(path), "text": text, "ts": timeutil.now()})
    return str(path)
