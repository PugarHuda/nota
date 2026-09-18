from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import respx
from httpx import Response

from nota import scorecard as sc
from nota.ledger import Ledger
from nota.ryo_client import RecordedRyoClient, RyoError

FIXTURES = Path(__file__).parent / "fixtures"
TICKER = "https://www.okx.com/api/v5/market/ticker"
CANDLES = "https://www.okx.com/api/v5/market/history-candles"


def _ryo_price():
    return RecordedRyoClient(FIXTURES, name="fixture").call("deep_analysis", {"symbol": "SOL", "include_perp": True}).get("market.price_usd")


class Down:
    name = "down"

    def call(self, tool, args=None):
        raise RyoError(401, "UNAUTHENTICATED", "Invalid or expired credential.")


@respx.mock
def test_lock_stores_ryo_plan_okx_price_and_basis():
    respx.get(TICKER).mock(return_value=Response(200, json={"code": "0", "data": [{"last": str(_ryo_price() * 1.01), "ts": "1789700000000"}]}))
    led = Ledger(":memory:")
    [row] = sc.lock_all(RecordedRyoClient(FIXTURES, name="fixture"), led, ["SOL"], http=httpx.Client(), sleep=lambda s: None)
    assert row["status"] == "locked" and row["basis_pct"] == 1.0 and row["plan"]["entry"] and row["envelope"]["tool"] == "deep_analysis"
    assert [i for i, _ in led.unsettled_locks()] == [row["id"]]
    assert sc.lock_all(RecordedRyoClient(FIXTURES, name="fixture"), led, ["SOL"], http=httpx.Client(), sleep=lambda s: None) == []  # once a day


@respx.mock
def test_lock_refuses_to_settle_across_a_basis_gap_and_records_failures():
    respx.get(TICKER).mock(return_value=Response(200, json={"code": "0", "data": [{"last": str(_ryo_price() * 1.05), "ts": "1789700000000"}]}))
    led = Ledger(":memory:")
    [gap] = sc.lock_all(RecordedRyoClient(FIXTURES, name="fixture"), led, ["SOL"], http=httpx.Client(), sleep=lambda s: None)
    [down] = sc.lock_all(Down(), led, ["ETH"], http=httpx.Client(), sleep=lambda s: None)
    assert gap["status"] == "basis_mismatch" and down["status"] == "ryo_unavailable" and "UNAUTHENTICATED" in down["error"]
    assert led.unsettled_locks() == [] and len(led.list_locks()) == 2  # both kept, neither settles


LONG = {"side": "long", "entry": 100.0, "stop": 94.0, "target": 106.0}


def c(high, low, close=100.0):
    return {"ts": 0, "open": 100.0, "high": high, "low": low, "close": close}


def test_first_touch_orders_stop_and_target_and_admits_what_an_hour_cannot_order():
    assert sc.first_touch(LONG, [c(101, 99), c(106.5, 99)]) == ("target", 1)
    assert sc.first_touch(LONG, [c(101, 93.9), c(107, 99)]) == ("stop", 0)
    assert sc.first_touch(LONG, [c(107, 93)]) == ("ambiguous", 0)
    assert sc.first_touch(LONG, [c(105, 95)]) == ("neither", None)
    short = {"side": "short", "entry": 100.0, "stop": 106.0, "target": 94.0}
    assert sc.first_touch(short, [c(101, 93.5)]) == ("target", 0)


def test_bracket_is_re_anchored_to_the_okx_price():
    row = {"okx_price": 200.0, "plan": {"entry": 100.0, "stop": 94.0, "targets": [106.0, 112.0]}}
    assert sc.bracket(row) == {"side": "long", "entry": 200.0, "stop": 188.0, "target": 212.0}


@respx.mock
def test_settle_waits_for_the_horizon_then_settles_on_closed_candles():
    locked = datetime(2026, 9, 18, 9, 20, tzinfo=timezone.utc)
    row = {"id": "x", "symbol": "SOL", "inst": "SOL-USDT", "locked_at": locked.isoformat(), "okx_price": 100.0,
           "plan": {"entry": 50.0, "stop": 47.0, "targets": [53.0]}}
    start = datetime(2026, 9, 18, 10, tzinfo=timezone.utc)
    rows = [[str(int((start + timedelta(hours=i)).timestamp() * 1000)), "100", "103" if i != 5 else "106.5", "99", "101", "0", "0", "0", "1"]
            for i in range(24)]
    route = respx.get(CANDLES).mock(return_value=Response(200, json={"code": "0", "data": rows[::-1]}))
    assert sc.settle_one(row, 24, httpx.Client(), now=start + timedelta(hours=23)) is None
    out = sc.settle_one(row, 24, httpx.Client(), now=start + timedelta(hours=25))
    assert out["result"] == "target" and out["hours_to_touch"] == 6 and out["return_at_horizon_pct"] == 1.0
    assert route.calls.last.request.url.params["before"] == str(int(start.timestamp() * 1000) - 1)


def test_settle_reports_a_short_candle_series_instead_of_settling_on_it():
    row = {"inst": "SOL-USDT", "locked_at": "2026-09-01T00:10:00+00:00", "okx_price": 100.0, "plan": {"entry": 1, "stop": 0.9, "targets": [1.1]}}
    with respx.mock:
        respx.get(CANDLES).mock(return_value=Response(200, json={"code": "0", "data": []}))
        assert sc.settle_one(row, 24, httpx.Client())["status"] == "incomplete_candles"


def _s(day, state, result):
    return {"day": day, "confluence_state": state, "result": result}


def test_contrast_compares_within_a_day_and_bootstraps_over_days_not_plans():
    rows = []
    for d in range(6):
        day = f"2026-09-{10 + d:02d}"
        rows += [_s(day, "CONFIRMED", "target"), _s(day, "CONFIRMED", "target"), _s(day, "MIXED", "stop"), _s(day, "MIXED", "target")]
    rows.append(_s("2026-09-30", "CONFIRMED", "stop"))  # a day with no MIXED plan cannot enter the contrast
    rows.append(_s("2026-09-11", "MIXED", "neither"))    # undecided plans are not counted as losses
    out = sc.contrast(rows, "confluence_state", "CONFIRMED", "MIXED")
    assert out["days"] == 6 and out["a_rate"] == [12, 12] and out["b_rate"] == [6, 12] and out["diff_pct"] == 50.0
    assert out["ci90_pct"] == [50.0, 50.0] and out["distinguishable"]
    assert sc.contrast(rows[:8], "confluence_state", "CONFIRMED", "MIXED")["ci90_pct"] is None  # two days: no interval


def test_the_scorecard_api_and_page_are_served_and_refuse_an_unknown_horizon(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    from nota import api

    monkeypatch.setenv("NOTA_DB", str(tmp_path / "s.db"))
    c = TestClient(api.app)
    s = c.get("/api/scorecard").json()
    assert s["horizon_h"] == 24 and s["open"] == [] and len(s["universe"]) == 25
    assert c.get("/api/scorecard?horizon=12").status_code == 422
    page = c.get("/scorecard")
    assert page.status_code == 200 and "/api/scorecard?horizon=" in page.text and "Does RYO's verdict change" in page.text
