"""Read-only queries behind each dashboard panel. Every function tolerates an empty database."""

from __future__ import annotations

import statistics
from collections import Counter, defaultdict
from typing import Any

from combo_edge import timeutil
from combo_edge.combos import CreationLedger
from combo_edge.execution import confirmation_phrase, get_mode, is_killed
from combo_edge.learning.labels import HOUR_BUCKETS, LIQ_BUCKETS, edge_labels, group_edges
from combo_edge.positions import book_summary, combo_positions, players_of
from combo_edge.quotes import current_side_mapping, side_counts
from combo_edge.settings import RuntimeSettings, live_forbidden
from combo_edge.sizing import Exposure, stacking_warnings
from combo_edge.storage import Database, loads


def header(db: Database, env: str, st: RuntimeSettings) -> dict[str, Any]:
    used = CreationLedger(db, env).used()
    return {
        "env": env,
        "mode": get_mode(db),
        "killed": is_killed(db),
        "trip_reason": db.risk_get("trip_reason"),
        "creations": used,
        "cap": st.weekly_creation_cap,
        "reserve": st.creation_reserve,
        "live_forbidden": live_forbidden(),
    }


def latest_run(db: Database, env: str, kind: str = "scan") -> dict[str, Any] | None:
    r = db.fetchone("SELECT * FROM runs WHERE env=:e AND kind=:k AND status != 'running' ORDER BY id DESC LIMIT 1", {"e": env, "k": kind})
    if not r:
        return None
    r["summary"] = loads(r.get("summary"), {}) or {}
    r["coverage"] = loads(r.get("coverage"), {}) or {}
    r["params"] = loads(r.get("params"), {}) or {}
    return r


def results(db: Database, env: str, family: str = "", team: str = "", max_hours: float | None = None, sort: str = "ev") -> dict[str, Any]:
    run = latest_run(db, env)
    rows = list((run or {}).get("summary", {}).get("rows", []) or [])
    families = sorted({r["family"] for r in rows})
    teams = sorted({t for r in rows for t in r.get("teams", [])})
    if family:
        rows = [r for r in rows if r["family"] == family]
    if team:
        rows = [r for r in rows if team in r.get("teams", [])]
    if max_hours is not None:
        rows = [r for r in rows if r.get("hours_to_start") is not None and r["hours_to_start"] <= max_hours]
    key = {"ev": lambda r: -(r.get("ev") if r.get("ev") is not None else -9),
           "ratio": lambda r: -(r.get("ratio") or 0),
           "payout": lambda r: -(r.get("quoted_payout") or 0),
           "hours": lambda r: (r.get("hours_to_start") if r.get("hours_to_start") is not None else 1e9),
           "makers": lambda r: -(r.get("n_makers") or 0)}.get(sort, lambda r: -(r.get("ev") or -9))
    rows.sort(key=key)
    return {"run": run, "rows": rows, "families": families, "teams": teams}


def run_summary(run: dict[str, Any] | None) -> dict[str, Any]:
    if not run:
        return {}
    s = run.get("summary", {})
    cov = run.get("coverage", {})
    return {
        "id": run["id"], "status": run["status"], "finished": timeutil.fmt_local(run.get("finished_at")),
        "rfqs": s.get("rfqs_sent"), "quoted": s.get("quoted"), "fill_rate": s.get("fill_rate"),
        "ttfq": s.get("ttfq_median_ms"), "makers": s.get("makers_mean"), "creations": s.get("creations_this_run"),
        "coverage_ok": cov.get("ok", True), "coverage_errors": cov.get("errors", []), "outcomes": s.get("outcomes", {}),
        "dry_run": bool(run.get("params", {}).get("dry_run")),
    }


def health(db: Database, env: str, st: RuntimeSettings) -> dict[str, Any]:
    used = CreationLedger(db, env).used()
    limits = db.fetchone("SELECT * FROM limits_log WHERE env=:e ORDER BY id DESC LIMIT 1", {"e": env})
    since = timeutil.now() - 7 * 86400
    rfqs = db.fetchall("SELECT outcome, first_quote_ms, n_makers, ws_ok, rest_used FROM rfqs WHERE env=:e AND created_at >= :t AND purpose='probe'",
                       {"e": env, "t": since})
    sent = len(rfqs)
    quoted = [r for r in rfqs if r["outcome"] == "quoted"]
    ttfq = [float(r["first_quote_ms"]) for r in quoted if r["first_quote_ms"] is not None]
    hist_edges = [0, 250, 500, 1000, 2000, 3000, 5000, 1e9]
    hist_labels = ["<250ms", "250-500", "0.5-1s", "1-2s", "2-3s", "3-5s", "5s+"]
    hist = [0] * (len(hist_edges) - 1)
    for v in ttfq:
        for i in range(len(hist)):
            if hist_edges[i] <= v < hist_edges[i + 1]:
                hist[i] += 1
                break
    http = db.fetchall("SELECT endpoint, status, SUM(n) AS n FROM http_stats WHERE env=:e AND day >= :d GROUP BY endpoint, status ORDER BY n DESC",
                       {"e": env, "d": timeutil.local_day(since)})
    errors = db.fetchall("SELECT ts, \"where\", message FROM errors ORDER BY id DESC LIMIT 15")
    requote_runs = db.fetchall("SELECT id, status, started_at, finished_at, summary FROM runs WHERE env=:e AND kind='requote' ORDER BY id DESC LIMIT 12", {"e": env})
    for r in requote_runs:
        r["summary"] = loads(r["summary"], {}) or {}
    run = latest_run(db, env)
    counts = side_counts(db, env)
    return {
        "creations": used, "cap": st.weekly_creation_cap, "reserve": st.creation_reserve,
        "limits": {"tier": (limits or {}).get("usage_tier"), "body": loads((limits or {}).get("body"), {}), "ts": (limits or {}).get("ts")},
        "fill": {"sent": sent, "quoted": len(quoted), "rate": (len(quoted) / sent) if sent else None,
                 "ttfq_median": statistics.median(ttfq) if ttfq else None, "outcomes": dict(Counter(r["outcome"] for r in rfqs)),
                 "ws_fallbacks": sum(1 for r in rfqs if not r["ws_ok"]), "hist_labels": hist_labels, "hist": hist},
        "http": http[:25], "n429": sum(int(h["n"]) for h in http if h["status"] == 429),
        "coverage": (run or {}).get("coverage", {}), "coverage_run": (run or {}).get("id"),
        "errors": errors, "requote_runs": requote_runs,
        "requote_enabled": st.requote_enabled, "fullscan_enabled": st.fullscan_enabled,
        "recorder": _recorder_status(db, env),
        "side": {"counts": counts, "mapping": current_side_mapping(db, env, st.side_check_min_quotes), "need": st.side_check_min_quotes},
        "pending_cancels": db.fetchall("SELECT * FROM pending_cancels WHERE env=:e", {"e": env}),
        "offer_upgrade": bool(db.risk_get("offer_upgrade", False)),
    }


def bets(db: Database, env: str, st: RuntimeSettings) -> dict[str, Any]:
    positions = combo_positions(db, env)
    live_exp = Exposure()
    names: dict[str, str] = {}
    for p in positions:
        players = players_of(db, p["legs"])
        live_exp.add(p["signature"] or p["ticker"], p["game_code"], players, float(p["cost"] or 0))
    for row in db.fetchall("SELECT player_uuid, player_name FROM legs WHERE player_uuid IS NOT NULL"):
        names[row["player_uuid"]] = row["player_name"]
    paper = book_summary(db, env, "paper")
    live = book_summary(db, env, "live")
    paper_exp = Exposure()
    for r in paper["open"]:
        paper_exp.add(r["signature"], r["game_code"], loads(r["players"], []) or [], float(r["stake"] or 0))

    def exposure_rows(exp: Exposure) -> dict[str, Any]:
        return {
            "games": [{"name": g, "amt": v, "cap": st.cap_game, "warn": v >= 0.8 * st.cap_game} for g, v in sorted(exp.by_game.items(), key=lambda x: -x[1])],
            "players": [{"name": names.get(p, p), "amt": v, "cap": st.cap_player, "n": exp.combos_by_player.get(p, 0),
                         "warn": v >= 0.8 * st.cap_player or exp.combos_by_player.get(p, 0) >= st.stack_warn}
                        for p, v in sorted(exp.by_player.items(), key=lambda x: -x[1])],
            "stacking": stacking_warnings(exp, st.stack_warn, names),
        }

    decisions = db.fetchall("SELECT ts, mode, decision, reason, ev, stake, combo_ticker FROM execution_decisions WHERE env=:e ORDER BY id DESC LIMIT 40", {"e": env})
    return {"positions": positions, "live_exposure": exposure_rows(live_exp), "paper_exposure": exposure_rows(paper_exp),
            "paper": paper, "live": live, "decisions": decisions, "mode": get_mode(db), "phrase": confirmation_phrase(env),
            "canary_passed": bool(db.risk_get(f"canary_passed:{env}", False))}


def learning(db: Database, env: str, st: RuntimeSettings) -> dict[str, Any]:
    labels = edge_labels(db, env)
    probes = [l for l in labels if l["purpose"] == "probe"]
    per_stratum = Counter(l["stratum_l1"] for l in probes if l.get("stratum_l1"))
    target = st.stratum_target_quotes
    strata = group_edges([l for l in labels if l.get("stratum_l1")], "stratum_l1")
    strata.sort(key=lambda s: -(s["mean"] if s["n"] >= 5 else -9))
    by_hours = group_edges(labels, "hours_bucket", [b[2] for b in HOUR_BUCKETS])
    by_liq = group_edges(labels, "liq_bucket", [b[2] for b in LIQ_BUCKETS] + ["no book"])
    by_family = group_edges(labels, "family")
    by_dow = group_edges(labels, "dow", ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"])
    models = db.fetchall("SELECT kind, created_at, n_train, metrics FROM model_versions ORDER BY id DESC LIMIT 20")
    for m in models:
        m["metrics"] = loads(m["metrics"], {}) or {}
    promotions = db.fetchall("SELECT ts, family_key, evidence, active FROM promotions ORDER BY id DESC LIMIT 20")
    for p in promotions:
        p["evidence"] = loads(p["evidence"], {}) or {}
    recommendation = db.meta_get(f"run_recommendation:{env}")
    report = db.meta_get(f"exploration_report:{env}")
    under = sorted(((s, n) for s, n in per_stratum.items() if n < target), key=lambda x: x[1])
    reached = sum(1 for n in per_stratum.values() if n >= target)
    return {"n_labels": len(labels), "per_stratum": per_stratum.most_common(), "under": under[:40], "reached": reached,
            "n_strata": len(per_stratum), "target": target, "top_strata": [s for s in strata if s["n"] >= 2][:20],
            "by_hours": by_hours, "by_liq": by_liq, "by_family": by_family, "by_dow": by_dow, "models": models,
            "promotions": promotions, "recommendation": recommendation, "report": report, "exploration_mode": st.exploration_mode,
            "calibration": db.meta_get(f"calibration:{env}"), "drift": db.meta_get(f"maker_drift:{env}")}


def chart_payload(name: str, db: Database, env: str, st: RuntimeSettings) -> dict[str, Any]:
    if name == "pnl":
        out = {}
        for book in ("paper", "live"):
            s = book_summary(db, env, book)["series"]
            out[book] = [{"x": timeutil.fmt_local(p["t"], "%m/%d"), "y": p["pnl"]} for p in s]
        return out
    if name == "clv":
        out = {}
        for book in ("paper", "live"):
            rows = db.fetchall("SELECT ts, clv FROM executions WHERE env=:e AND book=:b AND clv IS NOT NULL ORDER BY ts", {"e": env, "b": book})
            out[book] = [{"x": timeutil.fmt_local(r["ts"], "%m/%d"), "y": round(100 * float(r["clv"]), 2)} for r in rows]
        return out
    lr = learning(db, env, st)
    if name in ("hours", "liq", "family", "dow"):
        rows = lr[{"hours": "by_hours", "liq": "by_liq", "family": "by_family", "dow": "by_dow"}[name]]
        return {"labels": [r["group"] for r in rows], "mean": [_pct(r["mean"]) for r in rows], "lo": [_pct(r["lo"]) for r in rows],
                "hi": [_pct(r["hi"]) for r in rows], "n": [r["n"] for r in rows]}
    if name == "strata":
        rows = lr["per_stratum"][:30]
        return {"labels": [r[0] for r in rows], "n": [r[1] for r in rows], "target": lr["target"]}
    if name == "ttfq":
        h = health(db, env, st)["fill"]
        return {"labels": h["hist_labels"], "n": h["hist"]}
    return {}


def _pct(v: float) -> float | None:
    return None if v != v else round(100 * v, 2)  # NaN -> None


def _recorder_status(db: Database, env: str) -> dict[str, Any]:
    rec = dict(db.meta_get(f"recorder_status:{env}", {}) or {})
    try:
        from combo_edge.scheduler.launchd import is_installed

        rec["installed"] = is_installed("recorder")
    except Exception:  # noqa: BLE001
        rec["installed"] = False
    fresh = rec.get("updated") and timeutil.now() - float(rec["updated"]) < 60
    rec["connected"] = bool(rec.get("connected") and fresh)
    return rec
