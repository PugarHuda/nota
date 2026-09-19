import json
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

from nota.skills.technicals import DAY_MS, H4_MS, atr, binance_daily, rsi, technicals_crosscheck, to_daily

MARKET = Path(__file__).parent / "fixtures" / "market"
# Real Binance BTCUSDT 1d klines (limit 200), captured 2026-09-19; the last row was the day in progress.
BINANCE = json.loads((MARKET / "binance_btc_1d.json").read_text())
# The same series converted into OKX's history-candles row shape (not OKX data; see its _note).
OKX = json.loads((MARKET / "okx_btc_1d_converted_from_binance.json").read_text())["data"]


def _today() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000) // DAY_MS * DAY_MS


def _shift() -> int:
    """Move the captured series so its last (in-progress) row is today, whatever day the suite runs."""
    return _today() - int(BINANCE[-1][0])


def _binance_rows() -> list[list]:
    s = _shift()
    return [[r[0] + s, *r[1:6], r[6] + s, *r[7:]] for r in BINANCE]


def _okx_rows() -> list[list]:
    s = _shift()
    return [[str(int(r[0]) + s), *r[1:]] for r in OKX]


def _client(okx=True, binance=True, coingecko=None, calls=None) -> httpx.Client:
    def handler(req: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(req.url.host)
        if req.url.host == "www.okx.com":
            if not okx:
                return httpx.Response(200, json={"code": "51001", "msg": "Instrument ID doesn't exist.", "data": []})
            after = req.url.params.get("after")
            rows = [r for r in _okx_rows() if after is None or int(r[0]) < int(after)][:100]
            return httpx.Response(200, json={"code": "0", "msg": "", "data": rows})
        if req.url.host == "data-api.binance.vision":
            if not binance:
                return httpx.Response(451, json={"code": 0, "msg": "restricted location"})
            return httpx.Response(200, json=_binance_rows()[-int(req.url.params["limit"]):])
        if req.url.host == "api.coingecko.com" and coingecko is not None:
            return httpx.Response(200, json=coingecko)
        return httpx.Response(503)
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_rsi_matches_wilder_reference():
    # Classic textbook series (Wilder / StockCharts example); RSI(14) at the 15th close is 70.53
    closes = [44.3389, 44.0902, 44.1497, 43.6124, 44.3278, 44.8264, 45.0955, 45.4245, 45.8433, 46.0826, 45.8931, 46.0328, 45.6140, 46.2820, 46.2820]
    assert rsi(closes) == pytest.approx(70.53, abs=0.05)
    assert rsi(closes[:10]) is None
    assert rsi([1.0] * 16) == 100.0  # no losses at all


def test_rsi_and_atr_converge_on_real_daily_candles():
    """Wilder smoothing forgets its seed geometrically: 100 real closes already agree with 199 to 0.1 point,
    while the old 31-candle window did not."""
    daily = binance_daily("BTC", _client(), 201)
    closes = [d["close"] for d in daily]
    assert len(closes) == 199
    assert rsi(closes[-100:]) == pytest.approx(rsi(closes), abs=0.1)
    assert atr(daily[-100:]) == pytest.approx(atr(daily), rel=0.01)


def test_binance_drops_the_day_in_progress():
    daily = binance_daily("BTC", _client(), 201)
    assert daily[-1]["ts"] == _today() - DAY_MS and set(daily[-1]) == {"ts", "open", "high", "low", "close"}
    assert daily[-1]["close"] == float(BINANCE[-2][4])


def test_okx_first_with_200_closed_candles():
    calls: list[str] = []
    env = technicals_crosscheck("btc", http=_client(calls=calls))
    assert env.status == "ok" and env.availability == {"ohlc": "available"}
    assert env.data["candle_source"] == "okx" and env.data["method"]["candles"] == "okx_1Dutc"
    assert env.data["warmup_candles"] == 199 and "binance" not in " ".join(calls)  # 200 rows minus today's
    assert env.data["as_of"] == datetime.fromtimestamp(_today() / 1000, timezone.utc).isoformat(timespec="seconds")
    assert env.data["close"] == float(BINANCE[-2][4]) and not env.warnings


def test_okx_failure_falls_back_to_binance_and_says_so():
    env = technicals_crosscheck("BTC", reference_rsi_14=30.0, reference_atr_14=10.0, http=_client(okx=False))
    assert env.status == "ok" and env.data["candle_source"] == "binance" and env.data["method"]["candles"] == "binance_1d"
    assert any(w.startswith("okx:") for w in env.warnings)
    closes = [float(r[4]) for r in BINANCE[:-1]]
    assert env.data["rsi_14"] == rsi(closes) and env.data["reference"]["rsi_diff_points"] == round(30.0 - rsi(closes), 2)
    assert any("RSI(14)" in w for w in env.warnings) and any("ATR(14)" in w for w in env.warnings)
    assert env.data["performance_pct"]["7d"] == round((closes[-1] / closes[-8] - 1) * 100, 3)


def test_coingecko_only_path_aggregates_4h_and_warns():
    start = _today() - 30 * DAY_MS
    # 4h candles stamped with their close, like CoinGecko's; the last few belong to today and must go
    rows = [[start + (i + 1) * H4_MS, 100 + i * 0.1, 101 + i * 0.1, 99 + i * 0.1, 100.5 + i * 0.1] for i in range(30 * 6 + 3)]
    env = technicals_crosscheck("SOL", http=_client(okx=False, binance=False, coingecko=rows))
    assert env.data["candle_source"] == "coingecko" and env.data["method"]["candles"] == "coingecko_ohlc_4h_to_utc_daily"
    assert env.data["daily_candles"] == 30 and env.status == "ok"
    assert "coingecko 4h candles aggregated; fewer than 100 candles, RSI/ATR not fully converged" in env.warnings
    assert env.data["close"] == pytest.approx(100.5 + (30 * 6 - 1) * 0.1)  # the candle closing at 00:00 today ends yesterday


def test_every_source_down_is_unavailable():
    env = technicals_crosscheck("SOL", http=_client(okx=False, binance=False))
    assert env.status == "unavailable" and env.data["rsi_14"] is None and env.availability == {"ohlc": "unavailable"}
    assert any("HTTP 503" in w for w in env.warnings)


def test_days_is_the_performance_lookback_and_is_clamped():
    env = technicals_crosscheck("BTC", days=365, http=_client())
    assert env.request["days"] == 90 and "days 365 clamped to 90 (7..90)" in env.warnings
    assert env.data["warmup_candles"] == 199 and env.data["performance_pct"]["90d"] is not None


def test_atr_and_daily_aggregation():
    candles = []
    for i in range(20):  # four 4h candles per day (stamped at their close), price drifting up with a 2-unit daily range
        base = 100 + i
        for j in range(4):
            candles.append([i * DAY_MS + (j + 1) * H4_MS, base, base + 2, base - 0.5, base + 1])
    daily = to_daily(candles)
    assert len(daily) == 20 and daily[0]["high"] == 102 and daily[0]["low"] == 99.5 and daily[0]["close"] == 101
    assert daily[1]["ts"] == DAY_MS
    assert atr(daily) == pytest.approx(2.5, abs=1e-6)  # TR = max(2.5, |102-100|... ) = 2.5 every day
    assert atr(daily[:10]) is None


def test_atr_keeps_significant_digits_for_a_sub_cent_token():
    daily = [{"high": 4.0e-6, "low": 3.8e-6, "close": 3.9e-6} for _ in range(15)]
    assert atr(daily) == 2.0e-7  # six decimals used to round this to 0.0
