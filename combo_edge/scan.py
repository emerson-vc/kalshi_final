"""``run_scan(ScanRequest)``: the one entry point used by the Run button, the CLI and schedulers.

Flow: limits -> cleanup -> websocket -> universe -> candidates -> prescore -> selection -> leg
snapshots -> (create-or-resolve + RFQ probe + execution decision, concurrently) -> rank -> coverage.
"""

from __future__ import annotations

import asyncio
import logging
import statistics
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import combo_edge.sports.nfl.families  # noqa: F401 - registers the template families
from combo_edge import timeutil
from combo_edge.candidates import FAMILIES, Candidate, GameIndex, parse_signature
from combo_edge.combos import ComboResolver
from combo_edge.context import EnvClients, log_limits, make_clients
from combo_edge.core import build_context as build_core_context
from combo_edge.core import coverage as core_coverage
from combo_edge.core import Sweep, load_core
from combo_edge.prescore import Prescorer, load_priors
from combo_edge.probe import ProbeEngine, ProbeRequest, ProbeResult, cleanup_our_open_rfqs, drain_pending_cancels
from combo_edge.quotes import QuoteRouter
from combo_edge.ranking import ResultRow, build_row, rank
from combo_edge.selection import Selection, select
from combo_edge.settings import RuntimeSettings, data_dir, load_env_settings
from combo_edge.snapshots import open_prices, refresh_legs, write_snapshots
from combo_edge.sports.nfl.implications import logical_class, structural_rule
from combo_edge.storage import Database, dumps
from combo_edge.storage import schema as S
from combo_edge.universe import PLAYER_PROP_KINDS, Universe, build_universe

log = logging.getLogger(__name__)


def runtime_settings(db: Database) -> RuntimeSettings:
    return RuntimeSettings().merged(db.settings_overrides())


@dataclass
class ScanRequest:
    env: str | None = None
    stake: float | None = None
    rfq_budget: int | None = None
    max_new: int | None = None
    window_s: float | None = None
    dry_run: bool = False
    trigger: str = "button"  # button | cli | scheduler
    families: list[str] | None = None


@dataclass
class ScanProgress:
    running: bool = False
    phase: str = "idle"
    run_id: int | None = None
    env: str | None = None
    started_at: float | None = None
    finished_at: float | None = None
    counters: dict[str, int] = field(default_factory=lambda: {
        "games": 0, "legs_found": 0, "candidates": 0, "selected": 0, "lookups": 0, "creations": 0, "rejections": 0,
        "rfqs_sent": 0, "quotes_received": 0, "probes_done": 0, "probes_total": 0, "executions": 0,
    })
    messages: list[str] = field(default_factory=list)
    error: str | None = None
    status: str | None = None

    def bump(self, key: str, n: int = 1) -> None:
        self.counters[key] = self.counters.get(key, 0) + n

    def note(self, text: str) -> None:
        self.messages.append(f"{timeutil.fmt_local(timeutil.now(), '%H:%M:%S')} {text}")
        self.messages = self.messages[-60:]
        log.info(text)

    def as_dict(self) -> dict[str, Any]:
        return {"running": self.running, "phase": self.phase, "run_id": self.run_id, "env": self.env, "started_at": self.started_at,
                "finished_at": self.finished_at, "counters": dict(self.counters), "messages": list(self.messages), "error": self.error,
                "status": self.status}


@dataclass
class ScanResult:
    run_id: int | None
    status: str
    rows: list[ResultRow]
    coverage: dict[str, Any]
    summary: dict[str, Any]


@dataclass
class ScanHooks:
    """Extension points filled in by later phases (exploration, fair model, maker model, execution)."""

    extra_candidates: Callable[[Universe, RuntimeSettings, Database, set[str]], list[Candidate]] | None = None
    fair_model_factory: Callable[[Universe], Any] | None = None
    maker_model_factory: Callable[[Database, str, Universe], Any] | None = None
    explore_picker_factory: Callable[[Database, str, RuntimeSettings], Callable[[list[Candidate], int, int], list[Candidate]]] | None = None
    execution_factory: Callable[..., Any] | None = None
    exploit_key_factory: Callable[[Database, str], Callable[[Candidate], float]] | None = None
    prepare: list[Callable[[Database, str], None]] = field(default_factory=list)
    after_probes: list[Callable[..., Awaitable[None] | None]] = field(default_factory=list)


HOOKS = ScanHooks()


def _leg_price(yes_bid: float | None, yes_ask: float | None) -> float | None:
    if yes_bid is not None and yes_ask is not None:
        return (float(yes_bid) + float(yes_ask)) / 2.0
    return float(yes_ask) if yes_ask is not None else (float(yes_bid) if yes_bid is not None else None)


def recently_quoted(db: Database, env: str, st: RuntimeSettings, prices: dict[str, float] | None = None) -> set[str]:
    """Signatures quoted within ``core_requote_after_h``: they go to the back of the core queue, unless a
    leg's price has moved more than ``core_requote_move`` (3c) since that quote (then they're due again)."""
    last: dict[str, int | None] = {}
    for r in db.fetchall("SELECT signature, run_id FROM rfqs WHERE env=:e AND created_at >= :t AND outcome='quoted' "
                         "AND signature IS NOT NULL ORDER BY created_at", {"e": env, "t": timeutil.now() - st.core_requote_after_h * 3600}):
        last[r["signature"]] = r["run_id"]
    if not prices or not last:
        return set(last)
    runs = sorted({r for r in last.values() if r is not None})
    snap: dict[tuple[int, str], float] = {}
    if runs:
        ph = ",".join(f":r{i}" for i in range(len(runs)))
        for r in db.fetchall(f"SELECT run_id, ticker, yes_bid, yes_ask FROM leg_snapshots WHERE run_id IN ({ph})",
                             {f"r{i}": v for i, v in enumerate(runs)}):
            px = _leg_price(r["yes_bid"], r["yes_ask"])
            if px is not None:
                snap[(r["run_id"], r["ticker"])] = px
    out = set()
    for sig, run in last.items():
        moved = False
        for t, _side in parse_signature(sig):
            then, now = snap.get((run, t)) if run is not None else None, prices.get(t)
            if then is not None and now is not None and abs(now - then) > st.core_requote_move + 1e-9:
                moved = True
                break
        if not moved:
            out.add(sig)
    return out


def _qb_five_plus(c: Candidate) -> bool:
    """QB 5+ pass TD x team total: explore-only (core sweeps 3+/4+)."""
    return c.family == "qb_ptd_x_team_total" and any(s.side == "yes" and s.leg.kind == "pass_td" and (s.leg.threshold or 0) >= 4.5
                                                    for s in c.legs)


def explore_only_rule(db: Database, env: str, st: RuntimeSettings, cands: list[Candidate]) -> Callable[[Candidate], bool]:
    """Families only the explore pool may send: ``explore_only_families``, every no_* family and every
    exploration-strata pair (explore:*), until promoted or until the bandit shows a positive posterior over
    ``explore_only_release_quotes`` quotes. QB 5+ pass TD x team total is explore-only too."""
    from combo_edge.learning.bandit import released_families

    gated = set(st.explore_only_families) | {c.family for c in cands if c.family.startswith(("no_", "explore:"))}
    gated -= released_families(db, env, gated, st.explore_only_release_quotes)
    return lambda c: c.family in gated or _qb_five_plus(c)


def plan_selection(cands: list[Candidate], *, db: Database, env: str, st: RuntimeSettings, hooks: ScanHooks, rfq_budget: int,
                   creation_budget: int, known: set[str], recent: set[str] | None = None,
                   uni: Universe | None = None) -> tuple[Selection, list[Sweep]]:
    """The run's pool split over scored candidates (shared by run_scan and scripts/diagnose_selection.py)."""
    shares = {**st.budget_shares(), "unpriced": st.unpriced_share}
    sweeps = load_core()
    ctx = build_core_context(uni) if uni is not None else None
    picker = hooks.explore_picker_factory(db, env, st) if hooks.explore_picker_factory else None
    ekey = hooks.exploit_key_factory(db, env) if hooks.exploit_key_factory else None
    if recent is None:
        prices = {t: px for t, l in (uni.legs.items() if uni is not None else []) if (px := _leg_price(l.yes_bid, l.yes_ask)) is not None}
        recent = recently_quoted(db, env, st, prices)
    sel = select(cands, rfq_budget=rfq_budget, creation_budget=creation_budget, known=known, shares=shares, explore_picker=picker,
                 exploit_key=ekey, core=sweeps, core_ctx=ctx, recently_quoted=recent,
                 payout_band=(st.rank_payout_min, st.rank_payout_max), explore_only=explore_only_rule(db, env, st, cands))
    sel.core_ctx = ctx
    return sel, sweeps


def generate_candidates(uni: Universe, families: list[str] | None, blocked: set[str]) -> list[Candidate]:
    cov = uni.coverage
    out: dict[str, Candidate] = {}
    for code, legs in uni.legs_by_game.items():
        idx = GameIndex(legs, include_protected=False)
        for fam in FAMILIES.values():
            if not fam.exploit or (families and fam.name not in families):
                continue
            for a, b in fam.generator(idx):
                rule = structural_rule(a, b)
                if rule:
                    cov.skipped[f"structural:{rule}"] += 1
                    continue
                lc = logical_class(a, b)
                if lc and lc in blocked:
                    cov.skipped[f"learned_reject:{lc}"] += 1
                    continue
                cand = Candidate((a, b), fam.name, code)
                if lc:
                    cand.notes.append(f"logical:{lc}")
                if cand.signature in out:
                    continue
                out[cand.signature] = cand
                cov.candidates_by_family[fam.name] += 1
                cov.candidates_by_game[code] += 1
    for leg in uni.legs.values():
        if leg.precise:
            cov.excluded["precise_leg"] += 1
        elif leg.protected:
            cov.excluded["protected_leg_(exploration_only)"] += 1
    return list(out.values())


def _rule_class(c: Candidate) -> str | None:
    for n in c.notes:
        if n.startswith("logical:"):
            return n.split(":", 1)[1]
    return None


def _persist_candidates(db: Database, run_id: int, cands: list[Candidate], chosen: set[str], top_unselected: int = 1000,
                        uni: Universe | None = None) -> None:
    from combo_edge.gamelines import game_lines

    lines: dict[str, dict] = {}
    if uni is not None:
        for code, g in uni.games.items():
            lines[code] = game_lines(uni.legs_by_game.get(code, []), (g.away_team, g.home_team))
    keep = [c for c in cands if c.signature in chosen]
    rest = sorted((c for c in cands if c.signature not in chosen and c.expected_ev is not None), key=lambda c: -(c.expected_ev or 0))[:top_unselected]
    rows = []
    for rank_i, c in enumerate(keep + rest):
        rows.append({"run_id": run_id, "signature": c.signature, "game_code": c.game_code, "family": c.family, "stratum_l1": c.stratum_l1,
                     "stratum_l2": c.stratum_l2, "legs": c.legs_json(), "p_a": c.p_legs[0] if c.p_legs else None,
                     "p_b": c.p_legs[1] if len(c.p_legs) > 1 else None, "p_b_given_a": c.p_b_given_a, "fair_joint": c.fair_joint,
                     "fair_lower": c.fair_lower, "fair_source": c.fair_source, "fair_version": c.fair_version,
                     "leg_payout_product": c.leg_payout_product, "expected_ratio": c.expected_ratio, "expected_payout": c.expected_payout,
                     "expected_ev": c.expected_ev, "unpriced": c.unpriced, "pool": c.pool if c.signature in chosen else "",
                     "rank": rank_i, "combo_ticker": c.combo_ticker, "existed": c.existed,
                     **_env_features(c, lines.get(c.game_code), uni)})
    for i in range(0, len(rows), 2000):
        db.insert_many(S.candidates, rows[i : i + 2000])
    ids = db.fetchall("SELECT id, signature FROM candidates WHERE run_id=:r", {"r": run_id})
    by_sig = {r["signature"]: r["id"] for r in ids}
    for c in keep:
        c.candidate_id = by_sig.get(c.signature)


def _env_features(c: Candidate, gl: dict | None, uni: Universe | None) -> dict[str, Any]:
    team = next((s.leg.team for s in c.legs if s.leg.team), None)
    out: dict[str, Any] = {"hours_to_start": uni.hours_to_start(c.game_code) if uni else None,
                           "predicted_ratio": c.expected_ratio if c.fair_source else None}
    if gl:
        out["game_total_main"] = gl.get("total")
        out["team_total_main"] = (gl.get("team_total") or {}).get(team) if team else None
        out["spread_main"] = (gl.get("spread") or {}).get(team) if team else None
    return out


def _summary(results: list[ProbeResult], resolver: ComboResolver | None, limiter: Any) -> dict[str, Any]:
    sent = [r for r in results if r.rfq_id]
    quoted = [r for r in sent if r.outcome == "quoted"]
    ttfq = [r.first_quote_ms for r in quoted if r.first_quote_ms is not None]
    outcomes: dict[str, int] = {}
    for r in results:
        outcomes[r.outcome] = outcomes.get(r.outcome, 0) + 1
    return {
        "rfqs_sent": len(sent),
        "quoted": len(quoted),
        "fill_rate": (len(quoted) / len(sent)) if sent else None,
        "ttfq_median_ms": statistics.median(ttfq) if ttfq else None,
        "ttfq_p90_ms": (sorted(ttfq)[int(0.9 * (len(ttfq) - 1))] if ttfq else None),
        "makers_mean": (statistics.mean(r.n_makers for r in quoted) if quoted else None),
        "outcomes": outcomes,
        "ws_fallback_rfqs": sum(1 for r in sent if not r.ws_ok),
        "rest_used": sum(1 for r in sent if r.rest_used),
        "creations_this_run": resolver.created_this_run if resolver else 0,
        "creations_week": resolver.ledger.used() if resolver else None,
        "limiter": limiter.snapshot() if limiter else None,
    }


async def run_scan(
    req: ScanRequest,
    *,
    db: Database,
    progress: ScanProgress | None = None,
    abort: asyncio.Event | None = None,
    hooks: ScanHooks | None = None,
) -> ScanResult:
    hooks = hooks or HOOKS
    progress = progress or ScanProgress()
    abort = abort or asyncio.Event()
    es = load_env_settings()
    env = req.env or es.kalshi_env
    st = runtime_settings(db)
    stake = req.stake or st.stake
    budget = req.rfq_budget or st.rfq_budget
    window = req.window_s or st.quote_window_s
    max_new = st.max_new_creations_per_run if req.max_new is None else req.max_new
    from combo_edge.scheduler.locks import ProcessLock

    lock = ProcessLock(data_dir() / "locks" / f"scan-{env}.lock")
    if not lock.acquire():
        raise RuntimeError("another scan is already running for this environment")
    progress.__init__()  # reset counters in place for the dashboard
    progress.running, progress.env, progress.started_at, progress.phase = True, env, timeutil.now(), "starting"
    run_id = db.insert(S.runs, {"kind": "scan", "env": env, "status": "running", "started_at": progress.started_at,
                                 "params": {"stake": stake, "rfq_budget": budget, "window_s": window, "max_new": max_new,
                                            "dry_run": req.dry_run, "trigger": req.trigger, "families": req.families}})
    progress.run_id = run_id
    clients: EnvClients | None = None
    ws = None
    results: list[ProbeResult] = []
    rows: list[ResultRow] = []
    uni: Universe | None = None
    resolver: ComboResolver | None = None
    status = "done"
    summary: dict[str, Any] = {}
    try:
        clients = make_clients(env, db, st.rate_utilization)
        if clients.credentials_error:
            progress.note(f"credentials: {clients.credentials_error}")
        if not clients.authenticated and not req.dry_run:
            raise RuntimeError(f"no usable API credentials for '{env}' (see .env.example); use a dry run to scan without RFQs")
        progress.phase = "limits"
        limits = await clients.configure_limits(db, run_id, "before")
        progress.note(f"rate tier: {clients.limiter.tier}" + ("" if limits else " (limits unavailable; Basic assumed)"))

        if clients.authenticated and not req.dry_run:
            progress.phase = "cleanup"
            n1 = await drain_pending_cancels(clients.api, db)
            n2 = await cleanup_our_open_rfqs(clients.api, db)
            if n1 or n2:
                progress.note(f"cleanup: cancelled {n1 + n2} leftover RFQs created by this tool")
            progress.phase = "websocket"
            router = QuoteRouter()
            ws = clients.websocket([{"channels": ["communications"], "user_filter": "self"}], name="scan")
            ws.add_handler(router.handle)
            await ws.start()
            if not await ws.wait_connected(15):
                progress.note("websocket not connected yet; quotes will fall back to REST")
        else:
            router = QuoteRouter()

        progress.phase = "universe"
        roster = None
        try:
            from combo_edge.fair.nflverse import roster_index

            roster = await asyncio.wait_for(asyncio.to_thread(roster_index), timeout=60)
        except Exception as exc:  # noqa: BLE001
            progress.note(f"nflverse rosters unavailable ({type(exc).__name__}); using heuristic positions")
        uni = await build_universe(clients.api, st.collection_ticker, hours_ahead=st.hours_ahead_max,
                                   skip_starting_within_min=st.skip_starting_within_min, roster=roster)
        progress.counters["games"] = len(uni.games)
        progress.counters["legs_found"] = len(uni.legs)
        progress.note(f"universe: {len(uni.games)} games, {len(uni.legs)} combo-eligible legs via {uni.collection_ticker}")

        progress.phase = "candidates"
        resolver = ComboResolver(clients.api, db, env, uni.collection_ticker, run_id, max_new, st.weekly_creation_cap, st.creation_reserve)
        blocked = resolver.rejections.blocked_classes()
        for prep in hooks.prepare:
            prep(db, env)
        cands = generate_candidates(uni, req.families, blocked)
        if hooks.extra_candidates:
            extra = hooks.extra_candidates(uni, st, db, blocked)
            seen = {c.signature for c in cands}
            for c in extra:
                if c.signature not in seen:
                    seen.add(c.signature)
                    cands.append(c)
                    uni.coverage.candidates_by_family[c.family.split(":", 1)[0]] += 1
                    uni.coverage.candidates_by_game[c.game_code] += 1
        progress.counters["candidates"] = len(cands)
        progress.note(f"{len(cands)} candidates from {len(set(c.family for c in cands))} families")

        progress.phase = "prescore"
        fair_model = await asyncio.to_thread(hooks.fair_model_factory, uni) if hooks.fair_model_factory else None
        if fair_model is not None:
            progress.note(f"fair model: {sum(1 for g in fair_model.games.values() if g)} games simulated, "
                          f"{len(fair_model.flags)} legs flagged vs Kalshi")
            db.meta_set(f"fair_flags:{env}", {"run_id": run_id, "ts": timeutil.now(), "flags": fair_model.flags[:300]})
        maker_model = await asyncio.to_thread(hooks.maker_model_factory, db, env, uni) if hooks.maker_model_factory else None
        if maker_model is not None:
            progress.note(f"maker model: {maker_model.kind} on {maker_model.n_train} quotes")
        scorer = Prescorer(load_priors(), fair_model, maker_model)
        await asyncio.to_thread(scorer.score, cands)
        if len(cands) > st.max_candidates:
            cands = sorted(cands, key=lambda c: -(c.expected_ev or -9))[: st.max_candidates]
            uni.coverage.warnings.append(f"candidate cap {st.max_candidates} applied")

        progress.phase = "select"
        known = set(resolver.index.lookup_many([c.signature for c in cands]))
        # A dry run creates nothing, so it plans against the full run budget.
        creation_budget = budget if req.dry_run else resolver.budget_left()
        sel, sweeps = plan_selection(cands, db=db, env=env, st=st, hooks=hooks, rfq_budget=budget,
                                     creation_budget=creation_budget, known=known, uni=uni)
        chosen_sigs = {c.signature for c in sel.chosen}
        progress.counters["selected"] = len(sel.chosen)
        progress.note(f"selected {len(sel.chosen)} ({', '.join(f'{k}={v}' for k, v in sel.by_pool.items())}); "
                      f"creation budget {creation_budget} (week left {resolver.weekly_left})")
        _persist_candidates(db, run_id, cands, {c.signature for c in sel.chosen}, uni=uni)
        uni.coverage.check_games_with_props(PLAYER_PROP_KINDS)

        if req.dry_run:
            rows = [_dry_row(c, st) for c in sel.chosen]
            status = "dry_run"
            summary = {"dry_run": True, "core": core_coverage(cands, sweeps, chosen_sigs, ctx=sel.core_ctx)}
            return ScanResult(run_id, status, rank(rows), uni.coverage.to_dict(), summary)

        progress.phase = "snapshots"
        legs = {s.leg.ticker: s.leg for c in sel.chosen for s in c.legs}
        await refresh_legs(clients.api, legs.values())
        opens = await open_prices(clients.api, db, legs.values())
        mp = {t: (scorer.model_p(l), scorer.leg_p(l)) for t, l in legs.items()}
        write_snapshots(db, run_id, uni, legs.values(), opens, mp)

        progress.phase = "probing"
        progress.counters["probes_total"] = len(sel.chosen)
        execution = hooks.execution_factory(db=db, env=env, settings=st, run_id=run_id, universe=uni, progress=progress) if hooks.execution_factory else None
        engine = ProbeEngine(clients.api, ws, router, db, run_id=run_id, max_open=st.max_open_rfqs,
                             rest_crosscheck_frac=st.rest_crosscheck_frac,
                             execution_hook=execution.on_probe if execution else None,
                             on_progress=lambda k, n: progress.bump(k, n), abort_event=abort)
        demand = _feed_demand(db, sel.chosen)

        async def one(c: Candidate) -> ProbeResult | None:
            if abort.is_set():
                return None
            er = await resolver.ensure(c, _rule_class(c))
            if er.status == "index_hit":
                progress.bump("lookups")
                uni.coverage.combos["existed_index"] += 1
            elif er.status == "resolved_existing":
                progress.bump("creations")
                uni.coverage.combos["existed_resolved"] += 1
            elif er.status == "created_new":
                progress.bump("creations")
                uni.coverage.combos["created"] += 1
            else:
                uni.coverage.combos[er.status] += 1
                if er.status == "rejected":
                    progress.bump("rejections")
                return None
            if c.candidate_id:
                db.update(S.candidates, {"id": c.candidate_id}, {"combo_ticker": c.combo_ticker, "existed": c.existed})
            r = await engine.probe(ProbeRequest(er.ticker or "", c.signature, "probe", stake, window, candidate=c,
                                                hours_to_start=uni.hours_to_start(c.game_code), feed_demand=demand.get(c.signature)))
            progress.bump("probes_done")
            return r

        got = await asyncio.gather(*(one(c) for c in sel.chosen))
        results = [r for r in got if r is not None]
        by_sig = {c.signature: c for c in sel.chosen}
        ev_ticker = {r["ticker"]: r["event_ticker"] for r in db.fetchall("SELECT ticker, event_ticker FROM combos")}
        for r in results:
            c = by_sig.get(r.request.signature)
            if c is not None and r.rfq_id:
                rows.append(build_row(r, c, st, ev_ticker.get(r.request.combo_ticker)))
        id_results: list[ProbeResult] = []
        if st.id_requote_top > 0 and not abort.is_set():
            from combo_edge.id_requote import id_requote_request, pick_for_id_requote

            picks = pick_for_id_requote(results, by_sig, st.id_requote_top)
            progress.phase = "id_requote"

            async def again(r0: ProbeResult, c: Candidate) -> ProbeResult | None:
                if abort.is_set():
                    return None
                r = await engine.probe(id_requote_request(r0, c, stake, window))
                progress.bump("id_requotes")
                return r

            id_results = [r for r in await asyncio.gather(*(again(r0, c) for r0, c in picks)) if r is not None]
            anon_pay = {r0.request.signature: r0.combo_payout for r0, _ in picks}
            replaced = {r.request.signature for r in id_results if r.rfq_id and r.quoted}
            rows = [row for row in rows if row.signature not in replaced]
            for r in id_results:
                c = by_sig.get(r.request.signature)
                if c is not None and r.rfq_id and r.quoted:
                    row = build_row(r, c, st, ev_ticker.get(r.request.combo_ticker))
                    row.id_quote, row.anon_payout = True, anon_pay.get(c.signature)
                    rows.append(row)
            better = sum(1 for r in id_results if r.combo_payout and anon_pay.get(r.request.signature) and r.combo_payout > anon_pay[r.request.signature] * 1.05)
            progress.note(f"your-ID re-quotes: {len(replaced)}/{len(picks)} quoted, {better} better than anonymous")
        rows = rank(rows)
        for hook in hooks.after_probes:
            res = hook(db=db, env=env, settings=st, run_id=run_id, universe=uni, results=results, rows=rows,
                       candidates=sel.chosen, clients=clients, ws=ws, router=router, progress=progress, execution=execution)
            if asyncio.iscoroutine(res):
                await res
        if abort.is_set():
            status = "aborted"
        elif not uni.coverage.ok:
            status = "coverage_error"
        summary = _summary(results, resolver, clients.limiter)
        summary["id_requotes"] = {"sent": sum(1 for r in id_results if r.rfq_id), "quoted": sum(1 for r in id_results if r.quoted)}
        summary["core"] = core_coverage(cands, sweeps, chosen_sigs, {r.request.signature for r in results if r.quoted}, ctx=sel.core_ctx)
        progress.note(f"done: {summary['quoted']}/{summary['rfqs_sent']} RFQs quoted"
                      + (f", fill rate {summary['fill_rate']:.0%}" if summary.get("fill_rate") is not None else ""))
    except Exception as exc:
        status = "failed"
        progress.error = f"{type(exc).__name__}: {exc}"
        db.log_error("scan", progress.error)
        log.exception("scan failed")
        raise
    finally:
        try:
            if ws is not None:
                await ws.stop()
            if clients is not None and clients.authenticated and env != "mock":
                try:
                    after = await clients.api.account_limits()
                    log_limits(db, env, run_id, "after", after)
                except Exception:  # noqa: BLE001
                    pass
            if clients is not None and clients.authenticated and not req.dry_run:
                try:
                    await drain_pending_cancels(clients.api, db)
                except Exception:  # noqa: BLE001
                    pass
            if clients is not None:
                await clients.aclose()
        finally:
            lock.release()
            progress.running, progress.phase, progress.finished_at, progress.status = False, "idle", timeutil.now(), status
            cov = uni.coverage.to_dict() if uni else {}
            db.update(S.runs, {"id": run_id}, {"status": status, "finished_at": timeutil.now(), "coverage": cov,
                                               "summary": {**summary, "rows": [r.to_dict() for r in rows[:500]]}, "error": progress.error})
    return ScanResult(run_id, status, rows, uni.coverage.to_dict() if uni else {}, summary)


def _dry_row(c: Candidate, st: RuntimeSettings) -> ResultRow:
    from combo_edge.sizing import kelly_stake

    return ResultRow(signature=c.signature, combo_ticker="", legs=c.describe(), family=c.family, game_code=c.game_code,
                     teams=sorted({s.leg.team for s in c.legs if s.leg.team}), players=c.player_names(), hours_to_start=None,
                     quoted_payout=c.expected_payout, leg_payout_product=c.leg_payout_product, ratio=c.expected_ratio,
                     fair_joint=c.fair_joint, fair_lower=c.fair_lower, fair_payout=(1 / c.fair_joint) if c.fair_joint else None,
                     ev=c.expected_ev, ev_lower=c.expected_ev_lower, fair_source=c.fair_source,
                     suggested_stake=kelly_stake(c.fair_joint, c.fair_lower, c.expected_payout, st.bankroll, st.kelly_fraction, st.cap_combo),
                     n_makers=0, taker_yes_price=None, best_no_bid=None, second_no_bid=None, quote_spread=None, existed=c.existed,
                     pool=c.pool, stratum_l1=c.stratum_l1, rfq_id=None, outcome="dry_run", link=None, unpriced=c.unpriced,
                     expected_payout=c.expected_payout)


def _feed_demand(db: Database, cands: list[Candidate]) -> dict[str, int]:
    """How often the public RFQ feed showed each exact pair in the last 7 days."""
    since = timeutil.local_day(timeutil.now() - 7 * 86400)
    out: dict[str, int] = {}
    for c in cands:
        if len(c.legs) != 2:
            continue
        (a, sa), (b, sb) = sorted([(s.leg.ticker, s.side) for s in c.legs])
        n = db.scalar("SELECT SUM(n) FROM feed_pair_counts WHERE day>=:d AND leg_a=:a AND side_a=:sa AND leg_b=:b AND side_b=:sb",
                      {"d": since, "a": a, "sa": sa, "b": b, "sb": sb}, 0)
        out[c.signature] = int(n or 0)
    return out


def run_scan_sync(req: ScanRequest, db: Database) -> ScanResult:
    return asyncio.run(run_scan(req, db=db))


def dumps_rows(rows: list[ResultRow]) -> str:
    return dumps([r.to_dict() for r in rows])
