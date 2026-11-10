from types import SimpleNamespace

from combo_edge.id_requote import id_requote_request, pick_for_id_requote
from combo_edge.probe import ProbeRequest, ProbeResult


def _cand(sig: str, fair: float | None, legs: float | None):  # type: ignore[no-untyped-def]
    return SimpleNamespace(signature=sig, fair_joint=fair, leg_payout_product=legs)


def _res(sig: str, payout: float = 17.0, outcome: str = "quoted", execution=None, obscure: bool = True) -> ProbeResult:  # type: ignore[no-untyped-def]
    r = ProbeResult(ProbeRequest(f"T-{sig}", sig, "probe", 5.0, 5.0, hours_to_start=55.0, obscure_creator_id=obscure), outcome=outcome)
    r.combo_payout, r.execution = payout, execution
    return r


def test_picks_rank_by_fair_times_leg_product_not_the_anonymous_quote() -> None:
    by_sig = {
        "daniels": _cand("daniels", 0.0427, 49.7),   # potential +112% even though anonymous quote was 17x
        "meh": _cand("meh", 0.05, 22.0),             # +10%
        "nofair": _cand("nofair", None, 30.0),
        "bought": _cand("bought", 0.05, 60.0),
        "noquote": _cand("noquote", 0.05, 60.0),
    }
    results = [_res("meh", payout=21.0), _res("daniels", payout=17.4), _res("nofair"),
               _res("bought", execution={"decision": "paper_fill"}), _res("noquote", outcome="no_quotes_timeout")]
    picks = pick_for_id_requote(results, by_sig, 1)  # type: ignore[arg-type]
    assert [c.signature for _, c in picks] == ["daniels"]
    assert [c.signature for _, c in pick_for_id_requote(results, by_sig, 10)] == ["daniels", "meh"]  # type: ignore[arg-type]
    assert pick_for_id_requote(results, by_sig, 0) == []  # type: ignore[arg-type]


def test_id_requote_request_shows_your_id_and_keeps_stake() -> None:
    r0 = _res("daniels")
    req = id_requote_request(r0, _cand("daniels", 0.04, 49.7), 5.0, 5.0)  # type: ignore[arg-type]
    assert req.obscure_creator_id is False and req.purpose == "id_requote"
    assert req.combo_ticker == "T-daniels" and req.target_cost == 5.0 and req.hours_to_start == 55.0
