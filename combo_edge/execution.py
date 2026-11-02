"""Execution module: the ONLY code path that may accept a quote. Modes: off / paper / live. Default: paper.

When it runs: ``probe.py`` calls ``ExecutionEngine.on_probe`` at the end of each quote window while the
RFQ is still open (quotes die when the RFQ is cancelled). A quote that passes every gate is accepted
(live) or recorded as a paper fill. Fixable misses (stake mismatch, stale quote, near-miss EV, canary
required) are re-RFQ'd through ``shopping.py`` in an end-of-run sweep, best EV first, with every gate
re-checked on the fresh quote. No retry loops.

Gates (all must pass; every decision is logged with per-gate results):
 1. EV after fees >= ev_min (default +15%) on the blended fair.
 2. Stratum has >= stratum_min_quotes logged quotes AND EV is still > 0 at the fair model's lower bound.
 3. Quote is live: the maker's latest, <= max_quote_age_s old, and REST status ``open``.
 4. Stake = 1/4-Kelly x uncertainty shrink, clipped to $10/combo, $40/game, $30/player; the RFQ size
    must match it.
 5. No duplicate of an open position in the same book.
 6. Daily loss, weekly loss and bets/day limits (open stakes count as lost until settled). A trip sets
    the mode to off until you reset it.
 7. Kill switch off.
 8. Live only: side mapping verified, typed confirmation recorded, $1 canary passed (until then, live
    places only the canary).

Side mapping: buying YES appears to be ``accepted_side: "no"`` (you take the maker's NO bid). That is
inferred from the docs, so it's confirmed by the per-quote sizing self-check, then by the canary's fill,
and every live accept is verified against ``quote_accepted.contracts_accepted_fp`` (abort on mismatch).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

from combo_edge import pricing, safety, timeutil
from combo_edge.candidates import Candidate
from combo_edge.quotes import accepted_side_for_buy_yes, buy_yes_contracts, current_side_mapping
from combo_edge.settings import RuntimeSettings, live_forbidden
from combo_edge.sizing import Exposure, clip_to_caps, kelly_stake
from combo_edge.storage import Database, loads
from combo_edge.storage import schema as S

log = logging.getLogger(__name__)

_ACCEPT_ROUTE = r"^/communications/rfqs/[^/]+/quotes/[^/]+/accept$"


class _ExecutionCapability:
    """Opaque token. Only this module holds an instance; the REST layer checks identity."""

    __slots__ = ()


_CAPABILITY = _ExecutionCapability()
safety.register_privileged("PUT", _ACCEPT_ROUTE, _CAPABILITY, owner_module=__name__)

MODES = ("off", "paper", "live")
DEFAULT_MODE = "paper"


class ExecutionError(Exception):
    pass


def confirmation_phrase(env: str) -> str:
    return f"GO LIVE {env.upper()}"


def get_mode(db: Database) -> str:
    mode = db.risk_get("execution_mode", DEFAULT_MODE)
    return mode if mode in MODES else DEFAULT_MODE


def is_killed(db: Database) -> bool:
    from combo_edge.settings import data_dir

    return bool(db.risk_get("kill_switch", False)) or (data_dir() / "KILL").exists()


def kill(db: Database, reason: str = "kill switch") -> None:
    db.risk_set("kill_switch", True)
    db.risk_set("execution_mode", "off")
    db.risk_set("trip_reason", reason)
    db.risk_set("live_confirmation", None)


def clear_kill(db: Database) -> None:
    from combo_edge.settings import data_dir

    db.risk_set("kill_switch", False)
    k = data_dir() / "KILL"
    if k.exists():
        k.unlink()


def set_mode(db: Database, mode: str, env: str, confirmation: str | None = None, settings: RuntimeSettings | None = None) -> None:
    """Switch execution mode. Live needs the typed phrase, a verified side mapping and no live-forbid flag."""
    if mode not in MODES:
        raise ExecutionError(f"unknown mode {mode}")
    if mode == "live":
        if live_forbidden():
            raise ExecutionError("live execution is disabled on this machine (COMBO_EDGE_FORBID_LIVE)")
        if env not in ("demo", "prod"):
            raise ExecutionError("live execution needs a real environment (demo or prod)")
        if (confirmation or "").strip() != confirmation_phrase(env):
            raise ExecutionError(f"type exactly: {confirmation_phrase(env)}")
        if is_killed(db):
            raise ExecutionError("clear the kill switch first")
        min_q = settings.side_check_min_quotes if settings else 20
        if current_side_mapping(db, env, min_q) is None:
            raise ExecutionError(f"side mapping not verified yet on {env}: needs {min_q}+ consistent quotes from real makers")
        db.risk_set("live_confirmation", {"env": env, "ts": timeutil.now()})
    else:
        db.risk_set("live_confirmation", None)
    db.risk_set("execution_mode", mode)
    db.risk_set("trip_reason", None)


@dataclass
class Gate:
    name: str
    ok: bool
    detail: str = ""
    fixable: str | None = None  # reason a re-RFQ could fix it


@dataclass
class Decision:
    gates: list[Gate] = field(default_factory=list)
    stake: float = 0.0
    payout: float | None = None
    ev: float | None = None
    ev_lower: float | None = None
    contracts: float | None = None

    @property
    def ok(self) -> bool:
        return all(g.ok for g in self.gates)

    @property
    def fixable(self) -> str | None:
        bad = [g for g in self.gates if not g.ok]
        if bad and all(g.fixable for g in bad):
            return bad[0].fixable
        return None

    def reason(self) -> str:
        bad = [f"{g.name}: {g.detail}" for g in self.gates if not g.ok]
        return "; ".join(bad) if bad else "all gates passed"

    def gates_json(self) -> list[dict[str, Any]]:
        return [{"gate": g.name, "ok": g.ok, "detail": g.detail, "fixable": g.fixable} for g in self.gates]


class ExecutionEngine:
    def __init__(self, db: Database, env: str, settings: RuntimeSettings, run_id: int | None, universe: Any = None, progress: Any = None):
        self.db = db
        self.env = env
        self.st = settings
        self.run_id = run_id
        self.universe = universe
        self.progress = progress
        self.arbiter = asyncio.Lock()
        self.reshop: list[tuple[Candidate, str, float]] = []
        self._stratum_counts: dict[str, int] = {}
        self.exposure = {"paper": self._load_exposure("paper"), "live": self._load_exposure("live")}

    # ------------------------------------------------------------------ state
    def _load_exposure(self, book: str) -> Exposure:
        exp = Exposure()
        rows = self.db.fetchall(
            "SELECT signature, game_code, players, stake FROM executions WHERE book=:b AND env=:e AND status IN ('executed','pending')",
            {"b": book, "e": self.env},
        )
        for r in rows:
            exp.add(r["signature"], r["game_code"], loads(r["players"], []) or [], float(r["stake"] or 0))
        return exp

    def stratum_quotes(self, stratum: str) -> int:
        if stratum not in self._stratum_counts:
            self._stratum_counts[stratum] = int(self.db.scalar(
                "SELECT COUNT(*) FROM quotes q JOIN rfqs r ON q.rfq_id = r.rfq_id JOIN candidates c ON r.candidate_id = c.id "
                "WHERE c.stratum_l1 = :s AND r.env = :e", {"s": stratum, "e": self.env}, 0))
        return self._stratum_counts[stratum]

    def worst_case_loss(self, book: str, since: float) -> float:
        rows = self.db.fetchall(
            "SELECT status, stake, pnl FROM executions WHERE book=:b AND env=:e AND ts>=:t AND status IN ('executed','pending','settled')",
            {"b": book, "e": self.env, "t": since},
        )
        loss = 0.0
        for r in rows:
            if r["status"] == "settled":
                loss -= float(r["pnl"] or 0.0)
            else:
                loss += float(r["stake"] or 0.0)
        return loss

    def bets_today(self, book: str) -> int:
        return int(self.db.scalar(
            "SELECT COUNT(*) FROM executions WHERE book=:b AND env=:e AND ts>=:t AND status IN ('executed','pending','settled')",
            {"b": book, "e": self.env, "t": timeutil.start_of_local_day(timeutil.now())}, 0))

    def _trip(self, reason: str) -> None:
        self.db.risk_set("execution_mode", "off")
        self.db.risk_set("trip_reason", reason)
        self.db.risk_set("live_confirmation", None)
        if self.progress is not None:
            self.progress.note(f"EXECUTION OFF: {reason}")
        log.warning("execution tripped: %s", reason)

    def canary_passed(self) -> bool:
        return bool(self.db.risk_get(f"canary_passed:{self.env}", False))

    # ------------------------------------------------------------------ gates
    def evaluate(self, cand: Candidate, quote: Any, target: float, mode: str, mapping: str | None, quote_ok: bool | None = None) -> Decision:
        d = Decision()
        st = self.st
        book = "live" if mode == "live" else "paper"
        contracts = buy_yes_contracts(quote, target, mapping) if quote is not None else 0.0
        payout = pricing.combo_payout_from_contracts(target, contracts) if contracts else None
        fair, fair_lo = cand.fair_joint, cand.fair_lower
        ev = pricing.ev(fair, payout)
        ev_lo = pricing.ev(fair_lo, payout)
        d.payout, d.ev, d.ev_lower, d.contracts = payout, ev, ev_lo, contracts
        near = ev is not None and ev >= st.ev_min - st.reshop_near_miss
        d.gates.append(Gate("ev_after_fees", ev is not None and ev >= st.ev_min,
                            f"EV {ev:+.1%} vs min {st.ev_min:+.0%}" if ev is not None else "no payout/fair",
                            "near_miss" if (near and (ev or 0) < st.ev_min) else None))
        n_q = self.stratum_quotes(cand.stratum_l1)
        d.gates.append(Gate("stratum_confidence", n_q >= st.stratum_min_quotes and ev_lo is not None and ev_lo > 0,
                            f"{n_q}/{st.stratum_min_quotes} stratum quotes; EV at lower bound {ev_lo:+.1%}" if ev_lo is not None else f"{n_q} quotes; no lower bound"))
        age = (timeutil.now() - quote.recv_ts) if quote is not None else None
        fresh = age is not None and age <= st.max_quote_age_s and quote_ok is not False
        d.gates.append(Gate("quote_live", fresh, f"age {age:.1f}s" if age is not None else "no quote", None if fresh else "stale"))
        players = cand.players()
        exp = self.exposure[book]
        stake = kelly_stake(fair, fair_lo, payout, st.bankroll, st.kelly_fraction, st.cap_combo)
        stake = clip_to_caps(stake, exp, cand.game_code, players, st.cap_combo, st.cap_game, st.cap_player, cand.signature)
        if mode == "live" and not self.canary_passed():
            stake = min(stake, st.canary_stake) if stake >= st.min_stake else 0.0
        d.stake = stake
        stake_ok = stake >= st.min_stake and abs(stake - target) <= st.stake_tolerance
        d.gates.append(Gate("stake_kelly_caps", stake_ok, f"stake ${stake:.2f} vs RFQ ${target:.2f}",
                            "stake_mismatch" if stake >= st.min_stake and not stake_ok else None))
        dup = exp.by_signature.get(cand.signature, 0.0) > 0
        d.gates.append(Gate("no_duplicate", not dup, "already holding this combo" if dup else ""))
        now = timeutil.now()
        day_loss = self.worst_case_loss(book, timeutil.start_of_local_day(now))
        week_loss = self.worst_case_loss(book, now - 7 * 86400)
        n_today = self.bets_today(book)
        limits_ok = day_loss < st.daily_loss_limit and week_loss < st.weekly_loss_limit and n_today < st.max_bets_per_day
        d.gates.append(Gate("loss_limits", limits_ok, f"day ${day_loss:.2f}/{st.daily_loss_limit:.0f}, week ${week_loss:.2f}/{st.weekly_loss_limit:.0f}, bets {n_today}/{st.max_bets_per_day}"))
        d.gates.append(Gate("kill_switch", not is_killed(self.db), "kill switch on" if is_killed(self.db) else ""))
        if mode == "live":
            conf = self.db.risk_get("live_confirmation")
            ok = mapping is not None and bool(conf) and (conf or {}).get("env") == self.env and not live_forbidden()
            d.gates.append(Gate("live_preconditions", ok, "side mapping verified + typed confirmation" if ok else "side mapping or confirmation missing"))
        return d

    def _limits_would_trip(self, d: Decision) -> str | None:
        g = next((g for g in d.gates if g.name == "loss_limits"), None)
        return g.detail if g is not None and not g.ok else None

    # ------------------------------------------------------------------ probe hook
    async def on_probe(self, ctx: Any) -> dict[str, Any] | None:
        cand: Candidate | None = ctx.request.candidate
        mode = get_mode(self.db)
        if cand is None or mode == "off" or ctx.result.best_quote is None:
            return None
        async with self.arbiter:
            mode = get_mode(self.db)
            if mode == "off":
                return None
            q = ctx.result.best_quote
            d = self.evaluate(cand, q, ctx.request.target_cost, mode, ctx.side_mapping)
            if d.ok:
                # last-moment liveness check over REST (2 tokens), only for quotes that pass everything else
                try:
                    rq = await ctx.api.rfq_quote(ctx.result.rfq_id, q.quote_id)
                    quote_ok = (rq.get("status") or "open") == "open"
                except Exception:  # noqa: BLE001
                    quote_ok = True  # REST may not show open quotes (documented); websocket age gate still applies
                d = self.evaluate(cand, q, ctx.request.target_cost, mode, ctx.side_mapping, quote_ok=quote_ok)
            if not d.ok:
                trip = self._limits_would_trip(d)
                if trip:
                    self._trip(f"loss/bet limit reached ({trip})")
                fix = d.fixable  # e.g. live before the canary: the $1 stake mismatches the probe size -> re-RFQ at $1
                if fix:
                    self.reshop.append((cand, fix, d.ev or 0.0))
                    self._log(cand, ctx, d, mode, "shop", f"deferred to end-of-run re-RFQ ({fix}): {d.reason()}")
                else:
                    self._log(cand, ctx, d, mode, "reject", d.reason())
                return {"decision": "shop" if fix else "reject", "executed": False, "reason": d.reason()}
            if mode == "paper":
                self._paper_fill(cand, ctx, d)
                return {"decision": "paper_fill", "executed": False, "stake": d.stake, "ev": d.ev}
            return await self._accept_live(cand, ctx, d)

    # ------------------------------------------------------------------ actions
    def _log(self, cand: Candidate, ctx: Any, d: Decision, mode: str, decision: str, reason: str) -> int | None:
        q = ctx.result.best_quote
        return self.db.insert(S.execution_decisions, {
            "ts": timeutil.now(), "run_id": self.run_id, "env": self.env, "mode": mode, "candidate_id": cand.candidate_id,
            "signature": cand.signature, "combo_ticker": ctx.request.combo_ticker, "rfq_id": ctx.result.rfq_id,
            "quote_id": q.quote_id if q else None, "decision": decision, "reason": reason[:1000], "gates": d.gates_json(),
            "stake": d.stake, "price": ctx.result.taker_yes_price, "contracts": d.contracts, "payout": d.payout, "ev": d.ev,
            "ev_lower": d.ev_lower, "fair": cand.fair_joint})

    def _exec_row(self, cand: Candidate, ctx: Any, d: Decision, book: str, decision_id: int | None, status: str, **extra: Any) -> int | None:
        price = ctx.result.taker_yes_price
        contracts = d.contracts or 0.0
        return self.db.insert(S.executions, {
            "ts": timeutil.now(), "book": book, "env": self.env, "decision_id": decision_id, "run_id": self.run_id,
            "combo_ticker": ctx.request.combo_ticker, "signature": cand.signature, "game_code": cand.game_code,
            "players": cand.players(), "legs": cand.legs_json(), "rfq_id": ctx.result.rfq_id,
            "quote_id": ctx.result.best_quote.quote_id if ctx.result.best_quote else None, "price": price, "contracts": contracts,
            "stake": ctx.request.target_cost, "fee": pricing.taker_fee(contracts, price) if price else None, "payout": d.payout,
            "fair": cand.fair_joint, "ev": d.ev, "status": status, **extra})

    def _paper_fill(self, cand: Candidate, ctx: Any, d: Decision) -> None:
        did = self._log(cand, ctx, d, "paper", "paper_fill", "all gates passed (paper)")
        self._exec_row(cand, ctx, d, "paper", did, "executed", verified=1, verify_detail="paper")
        self.exposure["paper"].add(cand.signature, cand.game_code, cand.players(), ctx.request.target_cost)
        if self.progress is not None:
            self.progress.bump("executions")

    async def _accept_live(self, cand: Candidate, ctx: Any, d: Decision) -> dict[str, Any]:
        mapping = ctx.side_mapping
        side = accepted_side_for_buy_yes(mapping)
        q = ctx.result.best_quote
        if side is None or q is None:
            self._log(cand, ctx, d, "live", "reject", "side mapping unknown")
            return {"decision": "reject", "executed": False, "reason": "side mapping unknown"}
        if is_killed(self.db) or get_mode(self.db) != "live":
            return {"decision": "reject", "executed": False, "reason": "mode changed"}
        expected = buy_yes_contracts(q, ctx.request.target_cost, mapping)
        is_canary = not self.canary_passed()
        did = self._log(cand, ctx, d, "live", "canary" if is_canary else "accept", f"accepting {side} side of quote {q.quote_id}")
        exec_id = self._exec_row(cand, ctx, d, "live", did, "pending", accepted_side=side, is_canary=int(is_canary))
        self.exposure["live"].add(cand.signature, cand.game_code, cand.players(), ctx.request.target_cost)
        path = f"/communications/rfqs/{ctx.result.rfq_id}/quotes/{q.quote_id}/accept"
        try:
            await ctx.api.rest.request("PUT", path, json={"accepted_side": side}, auth_required=True, retry="none", capability=_CAPABILITY)
        except Exception as exc:  # noqa: BLE001
            self.db.update(S.executions, {"id": exec_id}, {"status": "failed", "verify_detail": str(exc)[:300]})
            return {"decision": "accept_failed", "executed": False, "reason": str(exc)[:200]}
        watch = ctx.watch
        deadline = timeutil.now() + 5.0
        executed: dict[str, Any] | None = None
        checked_accept = False
        while timeutil.now() < deadline:
            if watch.accepted and not checked_accept:
                checked_accept = True
                got = pricing.parse_dollars(watch.accepted[-1].get("contracts_accepted_fp"))
                if got is not None and abs(got - expected) > max(1.0, 0.02 * expected):
                    try:
                        await ctx.api.delete_rfq(ctx.result.rfq_id)  # abort before the maker confirms
                    except Exception:  # noqa: BLE001
                        pass
                    self.db.update(S.executions, {"id": exec_id}, {"status": "aborted", "verify_detail": f"contracts {got} != expected YES {expected}"})
                    self._trip(f"accept aborted: accepted contracts {got} did not match the YES count {expected}; side mapping suspect")
                    return {"decision": "abort", "executed": False, "reason": "contract count mismatch"}
            if watch.executed:
                executed = watch.executed[-1]
                break
            watch.changed.clear()
            try:
                await asyncio.wait_for(watch.changed.wait(), timeout=max(0.05, deadline - timeutil.now()))
            except TimeoutError:
                pass
        if executed is None:
            self.db.update(S.executions, {"id": exec_id}, {"status": "maker_no_confirm"})
            self.db.update(S.execution_decisions, {"id": did}, {"reason": "maker_no_confirm: maker did not confirm within the 3 s window"})
            return {"decision": "maker_no_confirm", "executed": False}
        order_id = executed.get("order_id")
        self.db.update(S.executions, {"id": exec_id}, {"status": "executed", "order_id": order_id})
        verified, detail = await self._verify_fill(ctx, order_id, expected)
        self.db.update(S.executions, {"id": exec_id}, {"verified": int(verified), "verify_detail": detail})
        if self.progress is not None:
            self.progress.bump("executions")
        self.db.risk_set("offer_upgrade", True)
        if is_canary:
            if verified:
                self.db.risk_set(f"canary_passed:{self.env}", True)
            else:
                self._trip(f"canary fill did not verify ({detail}); live stopped")
        elif not verified:
            self._trip(f"live fill did not verify ({detail})")
        return {"decision": "accept", "executed": True, "order_id": order_id, "verified": verified}

    async def _verify_fill(self, ctx: Any, order_id: str | None, expected: float) -> tuple[bool, str]:
        for _ in range(4):
            try:
                fills = await ctx.api.fills(ticker=ctx.request.combo_ticker)
            except Exception as exc:  # noqa: BLE001
                return False, f"fills unavailable: {exc}"[:200]
            mine = [f for f in fills if not order_id or f.get("order_id") == order_id]
            if mine:
                side = (mine[0].get("outcome_side") or mine[0].get("side") or "").lower()
                count = sum(pricing.parse_dollars(f.get("count_fp")) or 0.0 for f in mine)
                ok = side == "yes" and abs(count - expected) <= max(1.0, 0.02 * expected)
                return ok, f"outcome_side={side} count={count:.2f} expected={expected:.2f}"
            await asyncio.sleep(1.0)
        return False, "no fill found for order"

    # ------------------------------------------------------------------ end-of-run sweep
    async def end_of_run_sweep(self, shop: Any) -> int:
        """Re-RFQ deferred candidates (best EV first) once each, re-checking every gate on the fresh quote."""
        done = 0
        seen: set[str] = set()
        for cand, fix, _ev in sorted(self.reshop, key=lambda x: -x[2]):
            if cand.signature in seen or get_mode(self.db) == "off" or is_killed(self.db):
                continue
            seen.add(cand.signature)
            mode = get_mode(self.db)
            stake = kelly_stake(cand.fair_joint, cand.fair_lower, cand.expected_payout, self.st.bankroll, self.st.kelly_fraction, self.st.cap_combo)
            if mode == "live" and not self.canary_passed():
                stake = self.st.canary_stake
            stake = clip_to_caps(stake, self.exposure["live" if mode == "live" else "paper"], cand.game_code, cand.players(),
                                 self.st.cap_combo, self.st.cap_game, self.st.cap_player, cand.signature)
            if stake < self.st.min_stake:
                continue
            await shop(cand, round(stake, 2))
            done += 1
        self.reshop.clear()
        return done


def current_mapping(db: Database, env: str, settings: RuntimeSettings | None = None) -> str | None:
    return current_side_mapping(db, env, settings.side_check_min_quotes if settings else 20)
