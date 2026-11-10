"""Timing (early-week scans), the bandit's learning signal, the Scan tab's max-payout filter, repo hygiene."""

from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from combo_edge import timeutil
from combo_edge.context import default_db
from combo_edge.learning.bandit import rewards
from combo_edge.ranking import filter_max_payout, parse_max_payout, row_payout
from combo_edge.scan import ScanRequest, run_scan, runtime_settings
from combo_edge.scheduler.fullscan import early_week_due, fullscan_tick
from combo_edge.storage import schema as S
from combo_edge.web.app import create_app

ROOT = Path(__file__).resolve().parents[1]


def _local_ts(y: int, m: int, d: int, h: int) -> float:
    return datetime(y, m, d, h).timestamp()  # naive = local time, like timeutil.local_dt


def test_early_week_window() -> None:
    from combo_edge.settings import RuntimeSettings

    st = RuntimeSettings(fullscan_early_week=True)
    wed_10 = _local_ts(2026, 11, 4, 10)  # a Wednesday
    assert early_week_due(st, wed_10, None)
    assert not early_week_due(st, wed_10, "2026-11-04")  # once a day
    assert early_week_due(st, wed_10, "2026-11-03")
    assert not early_week_due(st, _local_ts(2026, 11, 4, 8), None)  # before 9am
    assert not early_week_due(st, _local_ts(2026, 11, 4, 21), None)  # 9pm is outside
    assert not early_week_due(st, _local_ts(2026, 11, 7, 12), None)  # Saturday
    assert not early_week_due(RuntimeSettings(), wed_10, None)  # off by default


async def test_early_week_tick_scans_once_a_day(mock_exchange) -> None:  # type: ignore[no-untyped-def]
    db = default_db()
    assert (await fullscan_tick("mock"))["status"] == "disabled"
    db.settings_set({"fullscan_early_week": True, "rfq_budget": 2, "quote_window_s": 1.0})
    sat = _local_ts(2026, 11, 7, 12)
    assert (await fullscan_tick("mock", now=sat))["status"] == "outside_early_week_window"
    tue = _local_ts(2026, 11, 3, 10)
    first = await fullscan_tick("mock", now=tue)
    assert first["status"] == "done" and first["mode"] == "early_week" and first["prop_events"] > 0
    assert (await fullscan_tick("mock", now=tue + 3600))["status"] == "outside_early_week_window"  # already ran today
    assert db.scalar("SELECT COUNT(*) FROM runs WHERE kind='scan'") == 1


def _probe_labels(db, arm: str, edges: list[float], clv: list[float | None]) -> None:  # type: ignore[no-untyped-def]
    run = db.insert(S.runs, {"kind": "scan", "env": "mock", "status": "done"})
    now = timeutil.now()
    for i, (e, c) in enumerate(zip(edges, clv)):
        cid = db.insert(S.candidates, {"run_id": run, "signature": f"{arm}{i}", "family": "f", "stratum_l1": arm,
                                       "legs": [["A", "yes"], ["B", "yes"]], "fair_joint": (1 + e) / 20.0, "game_code": "G"})
        db.insert(S.rfqs, {"rfq_id": f"{arm}p{i}", "env": "mock", "run_id": run, "candidate_id": cid, "combo_ticker": f"{arm}C{i}",
                           "signature": f"{arm}{i}", "purpose": "probe", "created_at": now - 3600, "outcome": "quoted", "combo_payout": 20.0})
        if c is not None:  # a late re-quote: CLV = 20 / close - 1
            db.insert(S.rfqs, {"rfq_id": f"{arm}q{i}", "env": "mock", "combo_ticker": f"{arm}C{i}", "purpose": "requote", "offset_h": 1,
                               "created_at": now, "outcome": "quoted", "combo_payout": 20.0 / (1 + c)})


def test_bandit_halves_model_ev_until_ten_clv_observations(mock_exchange) -> None:  # type: ignore[no-untyped-def]
    db = default_db()
    _probe_labels(db, "thin", [0.4] * 5, [None] * 5)
    _probe_labels(db, "proven", [0.4] * 12, [0.0] * 12)
    by_arm: dict[str, list[float]] = {}
    for arm, r in rewards(db, "mock"):
        by_arm.setdefault(arm, []).append(r)
    assert all(r == pytest.approx(0.2) for r in by_arm["thin"])  # 0.4 model EV x 0.5
    assert all(r == pytest.approx(0.4) for r in by_arm["proven"])  # full weight once CLV backs it


@pytest.mark.parametrize("text,value", [("40", 40.0), ("40x", 40.0), ("<40x", 40.0), ("100", 100.0), ("<100x", 100.0),
                                        (" < 60 X ", 60.0), ("<=25x", 25.0), ("", None), ("   ", None), (None, None)])
def test_parse_max_payout(text, value) -> None:  # type: ignore[no-untyped-def]
    assert parse_max_payout(text) == value


@pytest.mark.parametrize("bad", ["abc", "40y", ">40", "-5", "0x", "4 0"])
def test_parse_max_payout_rejects_garbage(bad) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(ValueError):
        parse_max_payout(bad)


def test_filter_uses_quote_then_expected_payout() -> None:
    rows = [{"legs": "a", "quoted_payout": 30.0, "expected_payout": 90.0},  # quoted wins
            {"legs": "b", "quoted_payout": 55.0, "expected_payout": 20.0},
            {"legs": "c", "quoted_payout": None, "expected_payout": 45.0},  # no quote yet: expected
            {"legs": "d", "quoted_payout": None, "expected_payout": None}]  # nothing to judge: shown
    assert row_payout(rows[2]) == 45.0
    assert [r["legs"] for r in filter_max_payout(rows, 40.0)] == ["a", "d"]
    assert [r["legs"] for r in filter_max_payout(rows, 50.0)] == ["a", "c", "d"]
    assert len(filter_max_payout(rows, None)) == 4


async def test_scan_tab_renders_payout_filter(mock_exchange) -> None:  # type: ignore[no-untyped-def]
    await run_scan(ScanRequest(env="mock", rfq_budget=6, window_s=1.5), db=default_db())
    html = TestClient(create_app(default_db(), "mock")).get("/partials/results", params={"family": ""}).text
    assert 'id="max-payout"' in html and "Max payout" in html
    for b in ("&lt;25x", "&lt;40x", "&lt;60x", "&lt;100x"):
        assert f'data-max-payout="{b}"' in html
    assert 'data-max-payout=""' in html  # "All"
    assert 'data-payout="' in html and 'id="payout-count"' in html
    js = (ROOT / "combo_edge" / "web" / "static" / "app.js").read_text()
    assert "localStorage" in js and "htmx:afterSwap" in js and "showing ${shown} of" in js


def test_cli_max_payout_filters_rows(mock_exchange, capsys) -> None:  # type: ignore[no-untyped-def]
    from combo_edge.cli import main

    with pytest.raises(SystemExit) as exc:
        main(["--env", "mock", "scan", "--dry-run", "--budget", "60", "--top", "60", "--max-payout", "<25x"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "showing " in out and "at or under 25x" in out
    pays = [float(line.split()[1].rstrip("x")) for line in out.splitlines() if line.startswith("  ") and line.split()[1].endswith("x")]
    assert pays and max(pays) <= 25.0
    with pytest.raises(SystemExit) as bad:
        main(["--env", "mock", "scan", "--dry-run", "--max-payout", "lots"])
    assert bad.value.code == 2


def test_requote_on_by_default_and_kill_switch_stops_early_week(mock_exchange) -> None:  # type: ignore[no-untyped-def]
    db = default_db()
    assert runtime_settings(db).requote_enabled
    db.settings_set({"fullscan_early_week": True})
    TestClient(create_app(db, "mock")).post("/kill", follow_redirects=False)
    st = runtime_settings(db)
    assert not st.fullscan_early_week and not st.requote_enabled and not st.fullscan_enabled


def test_mock_data_is_tracked_and_falls_back_to_fixtures(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    lines = (ROOT / ".gitignore").read_text().splitlines()
    assert "/data/" in lines and "data/" not in lines  # only the top-level runtime folder, not combo_edge/mock/data
    from combo_edge.mock import exchange

    monkeypatch.setattr(exchange, "DATA", tmp_path / "missing")
    mock = exchange.reset_mock(seed=3)  # loads the recorded fixtures instead
    assert any(t.startswith("KXNFLPASSTDS-26NOV02CHISEA") for t in mock.markets)
