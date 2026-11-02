from collections import Counter

from combo_edge import ladders
from combo_edge.candidates import FAMILIES, GameIndex
from combo_edge.sports.nfl.parser import parse_market
from combo_edge.sports.nfl.series import lookup
from combo_edge.sports.nfl.teams import split_game_code
from combo_edge.universe import build_universe, game_from_milestone


def _game(milestones, code):  # type: ignore[no-untyped-def]
    for m in milestones:
        g = game_from_milestone(m)
        if g and g.game_code == code:
            return g
    raise AssertionError(code)


def _markets(events, prefix, code="26NOV05JACBAL"):  # type: ignore[no-untyped-def]
    body = events.get(f"{prefix}-{code}") or {}
    return body.get("markets") or (body.get("event") or {}).get("markets") or []


def test_split_game_codes() -> None:
    assert split_game_code("26NOV05JACBAL") == ("JAC", "BAL")
    assert split_game_code("26NOV08NYJKC") == ("NYJ", "KC")
    assert split_game_code("26NOV08CLENO") == ("CLE", "NO")
    assert split_game_code("26NOV08LARWAS") == ("LAR", "WAS")
    assert split_game_code("26NOV08GBNE") == ("GB", "NE")


def test_milestone_gives_kickoff_and_teams(live_milestones) -> None:  # type: ignore[no-untyped-def]
    g = _game(live_milestones, "26NOV05JACBAL")
    assert (g.away_team, g.home_team) == ("JAC", "BAL")
    assert g.kickoff is not None and len(g.team_uuids) == 2


def test_parse_every_recorded_market(live_events, live_milestones) -> None:  # type: ignore[no-untyped-def]
    games = {c: _game(live_milestones, c) for c in ("26NOV02CHISEA", "26NOV05JACBAL")}
    unclassified = Counter()
    n = 0
    for et, body in live_events.items():
        series = et.split("-")[0]
        if lookup(series) is None:
            continue  # not combo-eligible / not registered: universe never fetches these
        for m in body.get("markets") or (body.get("event") or {}).get("markets") or []:
            leg, reason = parse_market(m, games[et.split("-")[1]])
            n += 1
            if reason:
                unclassified[series] += 1
    assert n > 2500
    assert not unclassified, unclassified


def test_parse_examples(live_events, live_milestones) -> None:  # type: ignore[no-untyped-def]
    g = _game(live_milestones, "26NOV05JACBAL")
    legs = {m["ticker"]: parse_market(m, g)[0] for m in _markets(live_events, "KXNFLPASSTDS")}
    law3 = legs["KXNFLPASSTDS-26NOV05JACBAL-JACTLAWRENCE16-3"]
    assert (law3.kind, law3.team, law3.threshold, law3.line_label, law3.jersey) == ("pass_td", "JAC", 2.5, "3+", "16")
    assert law3.player_name == "Trevor Lawrence" and law3.opponent == "BAL"
    tt = {m["ticker"]: parse_market(m, g)[0] for m in _markets(live_events, "KXNFLTEAMTOTAL")}
    bal4 = tt["KXNFLTEAMTOTAL-26NOV05JACBAL-BAL4"]
    assert (bal4.kind, bal4.team, bal4.threshold) == ("team_total", "BAL", 3.5)
    ml = {m["ticker"]: parse_market(m, g)[0] for m in _markets(live_events, "KXNFLGAME")}
    assert ml["KXNFLGAME-26NOV05JACBAL-BAL"].outcome == "BAL"
    prot = parse_market(_markets(live_events, "KXNFLTDPROT")[0], g)[0]
    assert prot.protected and prot.kind == "anytime_td"
    g2 = _game(live_milestones, "26NOV02CHISEA")
    precise = parse_market(_markets(live_events, "KXNFLPASSYDSPRECISE", "26NOV02CHISEA")[0], g2)[0]
    assert precise.precise and precise.kind == "pass_yds"
    sp = parse_market(_markets(live_events, "KXNFL1HSPREAD", "26NOV02CHISEA")[0], g2)[0]
    assert sp.kind == "h1_spread" and sp.team in ("CHI", "SEA") and sp.threshold is not None


def test_unknown_series_is_reported_not_crashed(live_milestones) -> None:  # type: ignore[no-untyped-def]
    g = _game(live_milestones, "26NOV05JACBAL")
    leg, reason = parse_market({"ticker": "KXNFLNEWTHING-26NOV05JACBAL-X", "event_ticker": "KXNFLNEWTHING-26NOV05JACBAL"}, g)
    assert reason and not leg.classified


async def test_universe_from_mock(mock_exchange) -> None:  # type: ignore[no-untyped-def]
    from combo_edge.context import default_db, make_clients

    clients = make_clients("mock", default_db())
    uni = await build_universe(clients.api, "KXMVECROSSCATEGORY-R")
    await clients.aclose()
    assert {"26NOV02CHISEA", "26NOV05JACBAL"} <= set(uni.games)
    assert len(uni.legs_by_game["26NOV05JACBAL"]) > 300
    game_ev = next(c for e, c in uni.constraints.items() if e.startswith("KXNFLGAME-"))
    assert game_ev.size_max == 1
    assert not uni.coverage.unparsed
    qbs = {l.player_name for l in uni.legs.values() if l.position == "QB"}
    assert {"Trevor Lawrence", "Sam Darnold"} <= qbs
    # ladders are monotone after estimation
    by_ladder: dict = {}
    for l in uni.legs.values():
        k = ladders.ladder_key(l)
        if k and l.p_est is not None:
            by_ladder.setdefault(k, []).append((l.threshold, l.p_est))
    for rungs in by_ladder.values():
        ps = [p for _, p in sorted(rungs)]
        assert all(a >= b - 1e-9 for a, b in zip(ps, ps[1:]))


async def test_templates_walk_full_ladders_and_skip_prot_precise(mock_exchange) -> None:  # type: ignore[no-untyped-def]
    import combo_edge.sports.nfl.families  # noqa: F401
    from combo_edge.context import default_db, make_clients

    clients = make_clients("mock", default_db())
    uni = await build_universe(clients.api, "KXMVECROSSCATEGORY-R")
    await clients.aclose()
    idx = GameIndex(uni.legs_by_game["26NOV02CHISEA"])
    pairs = list(FAMILIES["qb_ptd_x_team_total"].generator(idx))
    darnold = [(a.leg, b.leg) for a, b in pairs if a.leg.player_name == "Sam Darnold"]
    rungs = sorted({a.threshold for a, _ in darnold})
    assert rungs[-1] >= 3.5  # 4+ / 5+ pass TD extremes included
    tts = sorted({b.threshold for _, b in darnold})
    assert len(tts) >= 8  # every team-total rung, not just the main line
    for fam in FAMILIES.values():
        for a, b in fam.generator(idx):
            assert not (a.leg.protected or b.leg.protected or a.leg.precise or b.leg.precise)
            break
    no_pairs = list(FAMILIES["no_qb_ptd_x_team_total"].generator(idx))
    assert no_pairs and all(a.side == "no" and b.side == "no" for a, b in no_pairs)


def test_same_ladder_pairs_are_structural() -> None:
    from combo_edge.sports.base import Leg, SidedLeg
    from combo_edge.sports.nfl.implications import structural_rule

    a = Leg("T1", "E1", "KXNFL1HTEAMTOTAL", "G", "h1_team_total", "team", team="CHI", threshold=20.5)
    b = Leg("T2", "E1", "KXNFL1HTEAMTOTAL", "G", "h1_team_total", "team", team="CHI", threshold=23.5)
    c = Leg("T3", "E1", "KXNFL1HTEAMTOTAL", "G", "h1_team_total", "team", team="SEA", threshold=20.5)
    assert structural_rule(SidedLeg(a, "yes"), SidedLeg(b, "yes")) == "same_ladder"
    assert structural_rule(SidedLeg(a, "yes"), SidedLeg(c, "yes")) is None
