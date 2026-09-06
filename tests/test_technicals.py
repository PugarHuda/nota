import httpx
import pytest
import respx
from httpx import Response

from nota.skills.technicals import atr, rsi, technicals_crosscheck, to_daily


def test_rsi_matches_wilder_reference():
    # Classic textbook series (Wilder / StockCharts example); RSI(14) at the 15th close is 70.53
    closes = [44.3389, 44.0902, 44.1497, 43.6124, 44.3278, 44.8264, 45.0955, 45.4245, 45.8433, 46.0826, 45.8931, 46.0328, 45.6140, 46.2820, 46.2820]
    assert rsi(closes) == pytest.approx(70.53, abs=0.05)
    assert rsi(closes[:10]) is None
    assert rsi([1.0] * 16) == 100.0  # no losses at all


def test_atr_and_daily_aggregation():
    day = 86_400_000
    candles = []
    for i in range(20):  # four 4h candles per day, price drifting up with a 2-unit daily range
        base = 100 + i
        for j in range(4):
            candles.append([i * day + j * 4 * 3_600_000, base, base + 2, base - 0.5, base + 1])
    daily = to_daily(candles)
    assert len(daily) == 20 and daily[0]["high"] == 102 and daily[0]["low"] == 99.5 and daily[0]["close"] == 101
    assert atr(daily) == pytest.approx(2.5, abs=1e-6)  # TR = max(2.5, |102-100|... ) = 2.5 every day
    assert atr(daily[:10]) is None


@respx.mock
def test_skill_reports_reference_deviation_and_unavailable():
    day = 86_400_000
    rows = [[i * day + 1_700_000_000_000, 100 + i * 0.5, 101 + i * 0.5, 99 + i * 0.5, 100.4 + i * 0.5] for i in range(31)]
    respx.get("https://api.coingecko.com/api/v3/coins/solana/ohlc").mock(return_value=Response(200, json=rows))
    env = technicals_crosscheck("sol", reference_rsi_14=30.0, reference_atr_14=10.0, http=httpx.Client())
    assert env.status == "ok" and env.data["daily_candles"] == 31 and env.data["rsi_14"] == 100.0
    assert env.data["reference"]["rsi_diff_points"] == -70.0 and env.data["reference"]["atr_diff_pct"] > 100
    assert any("RSI(14)" in w for w in env.warnings) and any("ATR(14)" in w for w in env.warnings)
    assert env.data["performance_pct"]["7d"] == pytest.approx(3.5 / (100.4 + 23 * 0.5) * 100, abs=0.01)
    respx.get("https://api.coingecko.com/api/v3/coins/solana/ohlc").mock(return_value=Response(429))
    env = technicals_crosscheck("SOL", http=httpx.Client())
    assert env.status == "unavailable" and env.data["rsi_14"] is None and "HTTP 429" in env.warnings[0]
