import httpx
import respx
from httpx import Response

from nota.skills.base_rate import base_rate, hit, move_base_rate, wilder_atr_pct

DAY = 86_400_000


def candles(n=120, amp=0.02):
    """Close alternates 100 / 102; every high is 1% above and every low 1% below the day's close."""
    out = []
    for i in range(n):
        c = 100.0 * (1 + amp * (i % 2))
        out.append({"ts": 1_700_000_000_000 + i * DAY, "high": c * 1.01, "low": c * 0.99, "close": c})
    return out


def test_hit_touch_and_close_up_and_down():
    d = candles(5)  # closes 100, 102, 100, 102, 100
    assert hit(d, 0, 2.0, 1, "up", "close") is False       # 102 is not above 102
    assert hit(d, 0, 1.9, 1, "up", "close") is True
    assert hit(d, 0, 3.0, 1, "up", "touch") is True        # high 103.02 reaches 103
    assert hit(d, 1, 2.9, 2, "down", "touch") is True      # lows 99 and 100.98 against 99.04
    assert hit(d, 1, 3.1, 2, "down", "touch") is False


def test_base_rate_counts_same_tercile_days_and_splits_in_time():
    d = candles()
    out = base_rate(d, k=0.0, h=1, direction="up", event="close")
    # A 102 close divides the same true range by a bigger price, so those days sit in a lower ATR
    # tercile than the 100 days. Today (the last candle) closed at 102, only 102 days count, and from
    # 102 the next close is always 100: the conditioning is what makes this 0, not half.
    assert out["p"] == 0.0 and out["tercile"] == "low" and out["n_days"] >= 30 and out["n_independent"] == out["n_days"]
    # a fit day whose window reaches past the split is in neither half
    assert out["n_days"] - 1 <= out["holdout"]["fit_days"] + out["holdout"]["recent_days"] <= out["n_days"]
    assert base_rate(d[:40], 1, 3, "up", "touch")["p"] is None  # too little history is said, not guessed


def test_atr_is_none_until_fourteen_true_ranges_exist():
    atrs = wilder_atr_pct(candles(20))
    assert atrs[:14] == [None] * 14 and atrs[14] is not None


@respx.mock
def test_the_envelope_prints_rys_atr_ratio_and_reads_every_page():
    d = candles(250)
    rows = [[str(c["ts"]), "0", str(c["high"]), str(c["low"]), str(c["close"]), "0", "0", "0", "1"] for c in reversed(d)]
    route = respx.get("https://www.okx.com/api/v5/market/history-candles").mock(side_effect=[
        Response(200, json={"code": "0", "data": rows[:100]}), Response(200, json={"code": "0", "data": rows[100:200]}),
        Response(200, json={"code": "0", "data": rows[200:]}), Response(200, json={"code": "0", "data": []})])
    atr_okx = wilder_atr_pct(d)[-1]
    env = move_base_rate("sol", k=1, horizon_days=3, atr_14_pct=round(atr_okx * 0.9, 4), http=httpx.Client())
    assert env.status == "ok" and env.data["atr_scale"]["ryo_over_okx"] == 0.9 and env.data["k_in_okx_atr"] == 0.9
    assert route.calls[1].request.url.params["after"] == rows[99][0] and env.data["history"][0] < env.data["history"][1]
    assert "days touch +1 ATR above within 3d" in env.summary.headline


@respx.mock
def test_an_unlisted_symbol_is_unavailable_not_zero():
    respx.get("https://www.okx.com/api/v5/market/history-candles").mock(return_value=Response(200, json={"code": "51001", "msg": "Instrument ID doesn't exist.", "data": []}))
    env = move_base_rate("NOTACOIN", http=httpx.Client())
    assert env.status == "unavailable" and env.data["p"] is None and "doesn't exist" in env.warnings[0]


def test_the_holdout_fit_never_sees_the_newest_quarter():
    import random

    rng = random.Random(3)
    d = []
    for i in range(240):
        c = 100 * (1 + 0.05 * rng.random())
        r = 0.2 if i >= 220 else 0.03 * rng.random()        # the last 20 days are wild, so today is high-volatility either way
        d.append({"ts": 1_700_000_000_000 + i * DAY, "high": c * (1 + r), "low": c * (1 - r), "close": c})
    before = base_rate(d, 1.0, 3, "up", "touch")
    split = int((len(d) - 3 - 14) * 0.75) + 14            # first holdout day, as base_rate picks it
    # the holdout goes quiet: ranking cuts over every day would move them, and with them the fit
    calm = d[:split] + [{**c, "high": c["close"] * 1.001, "low": c["close"] * 0.999} for c in d[split:220]] + d[220:]
    after = base_rate(calm, 1.0, 3, "up", "touch")
    assert before["tercile"] == after["tercile"] == "high"
    assert after["holdout"]["recent_from"] == before["holdout"]["recent_from"]
    assert after["tercile_cuts_atr_pct"] == before["tercile_cuts_atr_pct"]
    assert after["holdout"]["fit_p"] == before["holdout"]["fit_p"] and after["holdout"]["fit_days"] == before["holdout"]["fit_days"]
