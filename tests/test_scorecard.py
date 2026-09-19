import json
import random
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
    return RecordedRyoClient(FIXTURES).call("deep_analysis", {"symbol": "SOL", "include_perp": True}).get("market.price_usd")


def _ticker(mult=1.01):
    respx.get(TICKER).mock(return_value=Response(200, json={"code": "0", "data": [{"last": str(_ryo_price() * mult), "ts": "1789700000000"}]}))


class Down:
    name = "down"

    def __init__(self, status=401, code="UNAUTHENTICATED"):
        self.status, self.code, self.calls = status, code, 0

    def call(self, tool, args=None):
        self.calls += 1
        raise RyoError(self.status, self.code, "Invalid or expired credential.", f"trace-{self.status}")


def _ms(t):
    return int(t.timestamp() * 1000)


def _rows(start, n, step, high="100.5", low="99.5", close="100", hit=None):
    """OKX candle rows, newest first as OKX sends them. `hit` = {index: (high, low)} overrides."""
    out = []
    for i in range(n):
        h, lo = (hit or {}).get(i, (high, low))
        out.append([str(_ms(start + i * step)), "100", h, lo, close, "0", "0", "0", "1"])
    return out[::-1]


def _okx(hourly, minutes=lambda after_ms: []):
    """One OKX candle endpoint answering by bar; minute pages are chosen by the `after` param."""
    def answer(request):
        p = request.url.params
        rows = hourly if p["bar"] == "1H" else minutes(int(p["after"]))
        return Response(200, json={"code": "0", "data": rows})
    return respx.get(CANDLES).mock(side_effect=answer)


# -- lock -----------------------------------------------------------------------------------------

@respx.mock
def test_lock_stores_ryo_plan_okx_price_basis_and_momentum_gate():
    _ticker()
    led = Ledger(":memory:")
    [row] = sc.lock_all(RecordedRyoClient(FIXTURES), led, ["SOL"], http=httpx.Client(), sleep=lambda s: None)
    assert row["status"] == "locked" and row["basis_pct"] == 1.0 and row["plan"]["entry"] and row["envelope"]["tool"] == "deep_analysis"
    assert row["momentum_gate"] is True  # the recorded SOL answer's confluence gate named 'momentum' passed
    assert [i for i, _ in led.unsettled_locks()] == [row["id"]]
    assert sc.lock_all(RecordedRyoClient(FIXTURES), led, ["SOL"], http=httpx.Client(), sleep=lambda s: None) == []  # within 20 h


@respx.mock
def test_a_lock_12_hours_after_the_last_is_skipped_across_midnight_and_one_20_hours_later_is_not():
    _ticker()
    led = Ledger(":memory:")
    evening = datetime(2026, 9, 18, 21, tzinfo=timezone.utc)
    row = {"symbol": "SOL", "locked_at": evening.isoformat(), "status": "locked"}
    led.save_lock("old", "SOL", row["locked_at"], "locked", json.dumps(row))
    run = lambda now: sc.lock_all(RecordedRyoClient(FIXTURES), led, ["SOL"], http=httpx.Client(), sleep=lambda s: None, now=now)
    assert run(evening + timedelta(hours=12)) == []   # 09:00 next UTC day: same market, not a new sample
    assert [r["status"] for r in run(evening + timedelta(hours=20))] == ["locked"]


@respx.mock
def test_lock_refuses_to_settle_across_a_basis_gap_and_records_failures():
    _ticker(1.05)
    led = Ledger(":memory:")
    [gap] = sc.lock_all(RecordedRyoClient(FIXTURES), led, ["SOL"], http=httpx.Client(), sleep=lambda s: None)
    down, aborted = sc.lock_all(Down(), led, ["ETH"], http=httpx.Client(), sleep=lambda s: None)
    assert gap["status"] == "basis_mismatch" and down["status"] == "ryo_unavailable" and "UNAUTHENTICATED" in down["error"]
    assert down["trace_id"] == "trace-401" and down["status_code"] == 401  # what RYO support asks for
    assert aborted["status"] == "aborted" and aborted["skipped"] == []
    assert led.unsettled_locks() == [] and len(led.list_locks()) == 3  # all kept, none settles


def test_a_dead_key_stops_after_one_call_and_a_failing_source_after_three():
    for src, calls, skipped in ((Down(401), 1, 4), (Down(503, "UPSTREAM"), 3, 2), (Down(0, "NO_KEY"), 1, 4)):
        led = Ledger(":memory:")
        rows = sc.lock_all(src, led, ["BTC", "ETH", "SOL", "XRP", "BNB"], http=httpx.Client(), sleep=lambda s: None)
        assert src.calls == calls and len(rows) == calls + 1
        [abort] = [json.loads(j) for _, j in led.list_locks() if json.loads(j)["status"] == "aborted"]
        assert len(abort["skipped"]) == skipped and abort["error"].startswith(src.code)


@respx.mock
def test_retried_failures_keep_one_row_and_a_success_removes_it():
    _ticker()

    class Flaky:
        name = "flaky"

        def __init__(self):
            self.n = 0

        def call(self, tool, args=None):
            self.n += 1
            if self.n <= 2:
                raise RyoError(503, "UPSTREAM", "busy")
            return RecordedRyoClient(FIXTURES).call(tool, args)

    led, src = Ledger(":memory:"), Flaky()
    for _ in range(3):
        sc.lock_all(src, led, ["SOL"], http=httpx.Client(), sleep=lambda s: None)
    s = sc.summary(led)
    assert s["coverage"] == {"locked": 1} and s["lock_days"] == 1


def test_lock_days_count_only_days_ryo_answered():
    led = Ledger(":memory:")
    for i, status in enumerate(("ryo_unavailable", "aborted", "okx_unavailable")):
        at = f"2026-09-1{i}T09:00:00+00:00"
        led.replace_lock(f"f{i}", "SOL", at, status, json.dumps({"symbol": "SOL", "locked_at": at, "status": status}))
    assert sc.summary(led)["lock_days"] == 0


def test_pacing_counts_from_the_start_of_the_last_call():
    t = [0.0]

    class Slow:
        name = "slow"

        def call(self, tool, args=None):
            t[0] += 15.0   # longer than the pace: nothing left to wait
            raise RyoError(404, "UNKNOWN_SYMBOL", "no")

    slept: list[float] = []
    sc.lock_all(Slow(), Ledger(":memory:"), ["BTC", "ETH"], http=httpx.Client(), sleep=slept.append, clock=lambda: t[0])
    assert slept == []
    t[0] = 0.0

    class Quick(Slow):
        def call(self, tool, args=None):
            t[0] += 2.0
            raise RyoError(404, "UNKNOWN_SYMBOL", "no")

    sc.lock_all(Quick(), Ledger(":memory:"), ["BTC", "ETH"], http=httpx.Client(), sleep=slept.append, clock=lambda: t[0])
    assert slept == [10.0]


def test_rows_are_handed_over_as_they_are_saved():
    seen = []
    sc.lock_all(Down(503, "UPSTREAM"), Ledger(":memory:"), ["BTC"], http=httpx.Client(), sleep=lambda s: None, on_row=seen.append)
    assert [r["status"] for r in seen] == ["ryo_unavailable"]


# -- settle ---------------------------------------------------------------------------------------

LONG = {"side": "long", "entry": 100.0, "stop": 94.0, "target": 106.0}


def c(high, low, close=100.0):
    return {"ts": 0, "open": 100.0, "high": high, "low": low, "close": close}


def test_first_touch_orders_stop_and_target_and_admits_what_one_candle_cannot_order():
    assert sc.first_touch(LONG, [c(101, 99), c(106.5, 99)]) == ("target", 1)
    assert sc.first_touch(LONG, [c(101, 93.9), c(107, 99)]) == ("stop", 0)
    assert sc.first_touch(LONG, [c(107, 93)]) == ("ambiguous", 0)
    assert sc.first_touch(LONG, [c(105, 95)]) == ("neither", None)
    short = {"side": "short", "entry": 100.0, "stop": 106.0, "target": 94.0}
    assert sc.first_touch(short, [c(101, 93.5)]) == ("target", 0)


def test_bracket_is_re_anchored_to_the_okx_price():
    row = {"okx_price": 200.0, "plan": {"entry": 100.0, "stop": 94.0, "targets": [106.0, 112.0]}}
    assert sc.bracket(row) == {"side": "long", "entry": 200.0, "stop": 188.0, "target": 212.0}


LOCKED = datetime(2026, 9, 18, 9, 20, tzinfo=timezone.utc)
START = datetime(2026, 9, 18, 10, tzinfo=timezone.utc)
ROW = {"id": "x", "symbol": "SOL", "inst": "SOL-USDT", "locked_at": LOCKED.isoformat(), "okx_price": 100.0, "status": "locked",
       "verdict": "constructive", "confluence_state": "MIXED", "plan": {"entry": 50.0, "stop": 47.0, "targets": [53.0]}}
MINUTE = timedelta(minutes=1)


@respx.mock
def test_settle_waits_for_the_horizon_then_settles_from_the_lock_minute():
    hourly = _rows(START, 24, timedelta(hours=1), hit={5: ("106.5", "99")})
    prefix = _rows(LOCKED, 40, MINUTE)
    route = _okx(hourly, lambda after: prefix if after == _ms(START) else [])
    assert sc.settle_one(ROW, 24, httpx.Client(), now=START + timedelta(hours=23)) is None
    out = sc.settle_one(ROW, 24, httpx.Client(), now=START + timedelta(hours=25))
    assert out["result"] == "target" and out["hours_to_touch"] == 6.67 and out["return_at_horizon_pct"] == 0.0
    assert out["minutes_before_first_hour"] == [40, 40] and out["resolved_by"] is None
    first = route.calls[0].request.url.params
    assert first["bar"] == "1H" and first["before"] == str(_ms(START) - 1)


@respx.mock
def test_a_touch_in_the_minutes_before_the_first_full_hour_is_seen():
    hourly = _rows(START, 24, timedelta(hours=1))
    prefix = _rows(LOCKED, 40, MINUTE, hit={7: ("100.5", "93.9")})  # stop at 09:27, before any hourly candle
    _okx(hourly, lambda after: prefix if after == _ms(START) else [])
    out = sc.settle_one(ROW, 24, httpx.Client(), now=START + timedelta(hours=25))
    assert out["result"] == "stop" and out["hours_to_touch"] == 0.13


@respx.mock
def test_an_hour_that_touched_both_levels_is_split_on_its_minutes():
    hourly = _rows(START, 24, timedelta(hours=1), hit={3: ("107", "93")})
    both_hour = START + timedelta(hours=3)
    inside = _rows(both_hour, 60, MINUTE, hit={10: ("100.5", "93.5"), 40: ("107", "99")})  # stop at :10, target at :40

    def minutes(after):
        return _rows(LOCKED, 40, MINUTE) if after == _ms(START) else inside if after == _ms(both_hour + timedelta(hours=1)) else []

    _okx(hourly, minutes)
    out = sc.settle_one(ROW, 24, httpx.Client(), now=START + timedelta(hours=25))
    assert out["result"] == "stop" and out["resolved_by"] == "1m" and out["hours_to_touch"] == 3.85


def test_minute_candles_are_paged_backwards_100_at_a_time():
    start = datetime(2026, 9, 18, 0, tzinfo=timezone.utc)
    rows = _rows(start, 150, MINUTE)[::-1]   # oldest first here
    asked = []

    def answer(request):
        after = int(request.url.params["after"])
        asked.append(after)
        page = [r for r in rows if int(r[0]) < after][-100:]
        return Response(200, json={"code": "0", "data": page[::-1]})

    with respx.mock:
        respx.get(CANDLES).mock(side_effect=answer)
        got = sc.okx_minutes("SOL-USDT", _ms(start), _ms(start + 150 * MINUTE), httpx.Client())
    assert len(got) == 150 and got[0]["ts"] == _ms(start) and len(asked) == 2


def test_a_short_candle_series_is_retried_then_stored_as_unsettleable():
    led = Ledger(":memory:")
    led.save_lock("x", "SOL", ROW["locked_at"], "locked", json.dumps(ROW))
    hourly = _rows(START, 24, timedelta(hours=1))
    del hourly[10]  # one hour OKX never published
    with respx.mock:
        _okx(hourly, lambda after: _rows(LOCKED, 40, MINUTE) if after == _ms(START) else [])
        [early] = [r for r in sc.settle_all(led, httpx.Client(), now=START + timedelta(hours=26)) if r["horizon_h"] == 24]
        assert early["status"] == "incomplete_candles" and led.get_settlement("x", 24) is None  # retried next cycle
        [late] = [r for r in sc.settle_all(led, httpx.Client(), now=START + timedelta(hours=50)) if r["horizon_h"] == 24]
    assert late["status"] == "unsettleable" and late["candles"] == 23 and late["missing_hours"] == [(START + timedelta(hours=13)).isoformat()]
    s = sc.summary(led, 24, now=START + timedelta(hours=50))
    assert s["coverage"]["unsettleable"] == 1 and s["open"] == [] and s["settled"] == [] and s["target_first"] == [0, 0]


def test_an_open_plan_past_its_window_is_marked_overdue():
    led = Ledger(":memory:")
    led.save_lock("x", "SOL", ROW["locked_at"], "locked", json.dumps(ROW))
    assert sc.summary(led, 24, now=START + timedelta(hours=24, minutes=30))["open"][0]["overdue"] is False
    assert sc.summary(led, 24, now=START + timedelta(hours=26))["open"][0]["overdue"] is True


# -- read -----------------------------------------------------------------------------------------

def _s(day, state, result, ret=None, hour=9):
    return {"day": day, "locked_at": f"{day}T{hour:02d}:00:00+00:00", "confluence_state": state, "result": result,
            "return_at_horizon_pct": {"target": 2.0, "stop": -2.0}.get(result, 0.0) if ret is None else ret}


def _days(n, start=1):
    return [(datetime(2026, 10, 1) + timedelta(days=start - 1 + i)).date().isoformat() for i in range(n)]


def test_with_no_real_difference_distinguishable_stays_rare():
    """The old day bootstrap called three-day contrasts distinguishable about a third of the time under
    the null. Now three days never are, and at twelve days the permutation p-value keeps its level."""
    rng = random.Random(1)

    def null(days):
        rows = []
        for d in _days(days):
            rows += [_s(d, st, rng.choice(("target", "stop"))) for st in ("CONFIRMED", "MIXED") for _ in range(2)]
        return rows

    fp3 = sum(sc.contrast(null(3), "confluence_state", "CONFIRMED", "MIXED")["distinguishable"] for _ in range(200))
    assert fp3 / 200 < 0.15
    fp12 = sum(sc.contrast(null(12), "confluence_state", "CONFIRMED", "MIXED", rounds=199)["p_value"] < 0.10 for _ in range(100))
    assert fp12 / 100 < 0.2


def test_a_strong_effect_over_twelve_days_is_distinguishable():
    rows = []
    for d in _days(12):
        rows += [_s(d, "CONFIRMED", "target"), _s(d, "CONFIRMED", "target"), _s(d, "MIXED", "stop"), _s(d, "MIXED", "stop")]
    rows.append(_s("2026-11-30", "CONFIRMED", "stop"))  # a day with no MIXED plan cannot enter the contrast
    rows.append(_s(_days(1)[0], "MIXED", "neither"))      # undecided plans are not counted as losses
    out = sc.contrast(rows, "confluence_state", "CONFIRMED", "MIXED")
    assert out["days"] == 12 and out["a_rate"] == [24, 24] and out["b_rate"] == [0, 24] and out["diff_pct"] == 100.0
    assert out["p_value"] < 0.01 and out["distinguishable"] and out["ci90_pct"] == [100.0, 100.0]
    few = sc.contrast([r for r in rows if r["day"] in _days(4)], "confluence_state", "CONFIRMED", "MIXED")
    assert few["ci90_pct"] is None and not few["distinguishable"] and few["min_days"] == 10  # four days: no call


def test_the_day_matched_difference_does_not_pool_plans_across_days():
    """Simpson: pooled, A is 2/11 and B 10/11; within every day they did exactly as well."""
    rows = ([_s("2026-10-01", "A", "target")] + [_s("2026-10-01", "B", "target")] * 9
            + [_s("2026-10-02", "A", "stop")] * 9 + [_s("2026-10-02", "B", "stop")]
            + [_s("2026-10-03", "A", "target"), _s("2026-10-03", "B", "target")])
    out = sc.contrast(rows, "confluence_state", "A", "B")
    assert out["a_rate"] == [2, 11] and out["b_rate"] == [10, 11] and out["diff_pct"] == 0.0


def test_overlapping_windows_form_one_cluster():
    rows = [_s(d, st, "target") for d in _days(3) for st in ("A", "B")]
    assert sc.contrast(rows, "confluence_state", "A", "B", horizon_h=72)["clusters"] == 1   # 24 h apart, 72 h windows
    assert sc.contrast(rows, "confluence_state", "A", "B", horizon_h=24)["clusters"] == 3
    half_day = [_s("2026-10-01", "A", "target", hour=21), _s("2026-10-02", "A", "target", hour=3)]
    assert len(set(sc.clusters(half_day, 24).values())) == 1   # 6 h apart: one market, two dates


def test_plans_that_touched_neither_level_still_give_a_return_contrast():
    rows = []
    for d in _days(12):
        rows += [_s(d, "CONFIRMED", "neither", 3.0), _s(d, "MIXED", "neither", -1.0)]
    out = sc.contrast(rows, "confluence_state", "CONFIRMED", "MIXED")
    assert out["days"] == 0 and out["diff_pct"] is None
    r = out["return"]
    assert r["days"] == 12 and r["diff_pct"] == 4.0 and r["a_mean_pct"] == 3.0 and r["distinguishable"]


def test_summary_contrasts_verdicts_and_the_momentum_gate_and_reports_ryo_lanes():
    env = RecordedRyoClient(FIXTURES).call("deep_analysis", {"symbol": "SOL", "include_perp": True}).model_dump(mode="json")
    led = Ledger(":memory:")
    for i, d in enumerate(_days(2)):
        for j, (verdict, gate, result) in enumerate((("constructive", True, "target"), ("cautious", False, "stop"))):
            row = {**ROW, "id": f"{i}{j}", "locked_at": f"{d}T09:00:00+00:00", "verdict": verdict, "envelope": env}
            if gate is False:
                row["momentum_gate"] = False        # stored at lock time; the other row derives it from its envelope
            led.save_lock(row["id"], "SOL", row["locked_at"], "locked", json.dumps(row))
            led.save_settlement(row["id"], 24, json.dumps({"status": "settled", "result": result, "hours_to_touch": 2,
                                                           "return_at_horizon_pct": 1.0 if result == "target" else -1.0}))
    s = sc.summary(led)
    keys = {(c["key"], c["a"], c["b"]) for c in s["contrasts"]}
    assert ("verdict", "cautious", "constructive") in keys and ("momentum_gate", True, False) in keys
    assert s["lanes"]["token_profile"] == {"partial": 4} and s["profile_latency_ms"] == [34290, 34290, 34290]
    assert s["missing_inputs"]["live holder coverage"] == 4


def test_the_scorecard_api_and_page_are_served_and_refuse_an_unknown_horizon(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    from nota import api

    monkeypatch.setenv("NOTA_DB", str(tmp_path / "s.db"))
    c = TestClient(api.app)
    s = c.get("/api/scorecard").json()
    assert s["horizon_h"] == 24 and s["open"] == [] and len(s["universe"]) == 25 and s["min_contrast_days"] == 10
    assert c.get("/api/scorecard?horizon=12").status_code == 422
    page = c.get("/scorecard")
    assert page.status_code == 200 and "/api/scorecard?horizon=" in page.text and "Does RYO's verdict change" in page.text


def test_the_scorecard_api_computes_once_per_ledger_state(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    from nota import api

    monkeypatch.setenv("NOTA_DB", str(tmp_path / "s.db"))
    calls = []
    real = sc.summary
    monkeypatch.setattr(sc, "summary", lambda led, h: calls.append(h) or real(led, h))
    c = TestClient(api.app)
    c.get("/api/scorecard")
    c.get("/api/scorecard")
    assert calls == [24]
    Ledger(str(tmp_path / "s.db")).save_lock("x", "SOL", ROW["locked_at"], "locked", json.dumps(ROW))
    assert len(c.get("/api/scorecard").json()["open"]) == 1 and calls == [24, 24]


def test_track_record_reads_the_settled_record_back_in_ryos_envelope():
    from nota.skills.track_record import verdict_track_record

    led = Ledger(":memory:")
    for i, (sym, verdict, state, result) in enumerate([("SOL", "constructive", "CONFIRMED", "target"), ("SOL", "cautious", "MIXED", "stop"),
                                                       ("ETH", "neutral", "MIXED", "target")]):
        row = {"id": f"l{i}", "symbol": sym, "inst": f"{sym}-USDT", "locked_at": f"2026-09-1{i}T09:00:00+00:00", "status": "locked",
               "verdict": verdict, "confluence_state": state, "confluence_score": 1, "okx_price": 100.0, "trace_id": f"t{i}",
               "plan": {"entry": 100.0, "stop": 94.0, "targets": [106.0], "atr_14_pct": 4.0}}
        led.save_lock(row["id"], sym, row["locked_at"], "locked", json.dumps(row))
        led.save_settlement(row["id"], 24, json.dumps({"status": "settled", "result": result, "hours_to_touch": 3, "return_at_horizon_pct": 1.0}))
    env = verdict_track_record("sol", ledger=led)
    assert env.data["target_first"] == {"hits": 1, "of_decided": 2} and env.data["verdict_against_plan"] == 1
    assert env.data["by_confluence_state"]["CONFIRMED"]["target"] == 1 and env.data["latest"]["trace_id"] == "t1"
    assert "1 of 2 decided RYO plans" in env.summary.headline
    empty = verdict_track_record("BTC", ledger=led)
    assert empty.status == "partial" and empty.data["target_first"]["of_decided"] == 0 and "none decided" in empty.summary.headline


def test_track_record_says_when_a_symbol_is_never_locked():
    from nota.skills.track_record import verdict_track_record

    env = verdict_track_record("FOO", ledger=Ledger(":memory:"))
    assert env.status == "unavailable" and "not in the scorecard universe" in " ".join(env.warnings)


def test_a_verdict_leaning_against_the_plan_is_flagged_on_either_side():
    assert sc.verdict_contradicts_side("short", "Constructive") and sc.verdict_contradicts_side("long", "cautious")
    assert not sc.verdict_contradicts_side("short", "cautious") and not sc.verdict_contradicts_side("long", "neutral")
    assert not sc.verdict_contradicts_side("short", None)


def test_track_record_counts_a_short_plan_under_a_constructive_verdict():
    from nota.skills.track_record import verdict_track_record

    led = Ledger(":memory:")
    row = {"id": "s1", "symbol": "SOL", "inst": "SOL-USDT", "locked_at": "2026-09-10T09:00:00+00:00", "status": "locked",
           "verdict": "constructive", "confluence_state": "MIXED", "confluence_score": 1, "okx_price": 100.0, "trace_id": "t",
           "plan": {"entry": 100.0, "stop": 106.0, "targets": [94.0], "atr_14_pct": 4.0}}
    led.save_lock("s1", "SOL", row["locked_at"], "locked", json.dumps(row))
    assert verdict_track_record("SOL", ledger=led).data["verdict_against_plan"] == 1
    assert sc.summary(led)["contradictions"] == 1


def test_list_locks_filters_by_utc_day():
    led = Ledger(":memory:")
    for i, at in enumerate(("2026-09-18T23:59:00+00:00", "2026-09-19T00:00:00+00:00", "2026-09-20T00:00:00+00:00")):
        led.save_lock(f"l{i}", "SOL", at, "locked", "{}")
    assert [i for i, _ in led.list_locks("2026-09-19")] == ["l1"] and len(led.list_locks()) == 3
