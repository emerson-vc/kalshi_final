from fastapi.testclient import TestClient

from combo_edge import execution
from combo_edge.context import default_db
from combo_edge.scan import ScanRequest, run_scan
from combo_edge.web.app import create_app


def _client(env: str = "mock") -> TestClient:
    return TestClient(create_app(default_db(), env))


def test_pages_render_empty(mock_exchange) -> None:  # type: ignore[no-untyped-def]
    c = _client()
    for path in ("/", "/bets", "/learning", "/health", "/partials/progress", "/partials/results"):
        r = c.get(path)
        assert r.status_code in (200, 286), path
    assert "Run scan" in c.get("/").text
    assert "PAPER" in c.get("/").text  # default execution mode


async def test_pages_render_after_mock_scan(mock_exchange) -> None:  # type: ignore[no-untyped-def]
    db = default_db()
    await run_scan(ScanRequest(env="mock", rfq_budget=12, window_s=2.5), db=db)
    c = _client()
    html = c.get("/").text
    assert "Ranked combos" in html and "Quote fill rate" in html and " + " not in html.split("Ranked combos")[0][-50:]
    assert c.get("/learning").status_code == 200
    h = c.get("/health").text
    assert "Coverage report" in h and "Quote side mapping" in h
    for name in ("hours", "liq", "family", "dow", "strata", "ttfq", "pnl", "clv"):
        assert c.get(f"/api/chart/{name}").status_code == 200
    r = c.get("/partials/results", params={"sort": "ratio"})
    assert r.status_code == 200


def test_live_mode_requires_phrase_and_is_forbidden_in_tests(mock_exchange) -> None:  # type: ignore[no-untyped-def]
    c = _client("demo")
    c.post("/execution/mode", data={"mode": "live", "confirmation": "GO LIVE DEMO"}, follow_redirects=False)
    assert execution.get_mode(default_db()) == "paper"  # COMBO_EDGE_FORBID_LIVE=1 in tests
    c.post("/execution/mode", data={"mode": "off"}, follow_redirects=False)
    assert execution.get_mode(default_db()) == "off"


def test_kill_switch(mock_exchange) -> None:  # type: ignore[no-untyped-def]
    c = _client()
    c.post("/kill", follow_redirects=False)
    db = default_db()
    assert execution.is_killed(db) and execution.get_mode(db) == "off"
    assert "Kill switch is ON" in c.get("/").text
    c.post("/kill/clear", follow_redirects=False)
    assert not execution.is_killed(db)


def test_settings_validation(mock_exchange) -> None:  # type: ignore[no-untyped-def]
    c = _client()
    c.post("/settings", data={"stake": "50"}, follow_redirects=False)  # above the $10 ceiling
    from combo_edge.scan import runtime_settings

    assert runtime_settings(default_db()).stake == 5.0
    c.post("/settings", data={"stake": "7.5", "back": "/"}, follow_redirects=False)
    assert runtime_settings(default_db()).stake == 7.5
