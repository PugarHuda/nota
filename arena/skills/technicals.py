"""Skill `technicals_crosscheck`: RSI(14) and ATR(14) computed independently from public OHLC.

RYO's `analyze_token` / `deep_analysis` report RSI(14) and ATR(14). This skill recomputes both with
Wilder's method from CoinGecko's keyless OHLC (4-hour candles aggregated to UTC days), so a reader can
see whether RYO's indicators agree with an independent calculation. Nothing is smoothed over: too few
candles means `null`, and the candle count and window are printed with every number.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import httpx

from arena.envelope import Envelope
from arena.skills.contract import SkillArg, SkillDefinition, SourceUnavailable, make_envelope
from arena.skills.price_check import COINGECKO_IDS
from arena.skills.sources import UA

DEFINITION = SkillDefinition(
    name="technicals_crosscheck",
    description="Recompute RSI(14), ATR(14) and 1d/7d/30d performance from CoinGecko public OHLC (Wilder smoothing, daily "
    "candles) and report how far reference values (e.g. RYO's) deviate from the independent calculation.",
    args=[
        SkillArg(name="symbol", type="string", description="Token symbol, e.g. SOL"),
        SkillArg(name="reference_rsi_14", type="number", required=False, description="RSI(14) to compare against"),
        SkillArg(name="reference_atr_14", type="number", required=False, description="ATR(14) in USD to compare against"),
        SkillArg(name="days", type="integer", required=False, description="Look-back in days (default 30, max 90)"),
    ],
)

PERIOD = 14
RSI_WARN_POINTS = 10.0
ATR_WARN_PCT = 25.0


def coingecko_id(symbol: str, http: httpx.Client) -> str:
    cid = COINGECKO_IDS.get(symbol)
    if cid:
        return cid
    try:
        resp = http.get("https://api.coingecko.com/api/v3/search", params={"query": symbol})
    except httpx.HTTPError as exc:
        raise SourceUnavailable(f"coingecko: network error {type(exc).__name__}") from exc
    if resp.status_code != 200:
        raise SourceUnavailable(f"coingecko: HTTP {resp.status_code}")
    coins = [c for c in resp.json().get("coins", []) if str(c.get("symbol", "")).upper() == symbol]
    if not coins:
        raise SourceUnavailable(f"coingecko: no coin with symbol {symbol}")
    return coins[0]["id"]


def fetch_ohlc(symbol: str, days: int, http: httpx.Client | None = None) -> list[list[float]]:
    """CoinGecko `/coins/{id}/ohlc`: [[ms, open, high, low, close], ...]; 4h candles for 3-30 days, 4-day candles beyond."""
    http = http or httpx.Client(timeout=20.0, headers={"User-Agent": UA})
    cid = coingecko_id(symbol, http)
    try:
        resp = http.get(f"https://api.coingecko.com/api/v3/coins/{cid}/ohlc", params={"vs_currency": "usd", "days": days})
    except httpx.HTTPError as exc:
        raise SourceUnavailable(f"coingecko ohlc: network error {type(exc).__name__}") from exc
    if resp.status_code != 200:
        raise SourceUnavailable(f"coingecko ohlc: HTTP {resp.status_code}")
    rows = resp.json()
    if not isinstance(rows, list) or not rows:
        raise SourceUnavailable("coingecko ohlc: empty response")
    return [[float(x) for x in r] for r in rows]


def to_daily(candles: list[list[float]]) -> list[dict[str, float]]:
    """Aggregate intraday candles into UTC-day candles (open first, high max, low min, close last)."""
    days: dict[str, dict[str, float]] = {}
    for ts, o, h, lo, c in candles:
        key = datetime.fromtimestamp(ts / 1000, timezone.utc).strftime("%Y-%m-%d")
        d = days.get(key)
        if d is None:
            days[key] = {"day": key, "open": o, "high": h, "low": lo, "close": c, "ts": ts}
        else:
            d["high"], d["low"], d["close"], d["ts"] = max(d["high"], h), min(d["low"], lo), c, ts
    return [days[k] for k in sorted(days)]


def rsi(closes: list[float], period: int = PERIOD) -> float | None:
    """Wilder RSI: seed with simple averages over the first `period` changes, then smooth."""
    if len(closes) < period + 1:
        return None
    changes = [b - a for a, b in zip(closes, closes[1:])]
    gain = sum(max(c, 0.0) for c in changes[:period]) / period
    loss = sum(max(-c, 0.0) for c in changes[:period]) / period
    for c in changes[period:]:
        gain = (gain * (period - 1) + max(c, 0.0)) / period
        loss = (loss * (period - 1) + max(-c, 0.0)) / period
    if loss == 0:
        return 100.0
    return round(100.0 - 100.0 / (1.0 + gain / loss), 2)


def atr(daily: list[dict[str, float]], period: int = PERIOD) -> float | None:
    """Wilder ATR over true ranges (needs `period` + 1 candles for the first true range that uses a previous close)."""
    if len(daily) < period + 1:
        return None
    trs = []
    for prev, cur in zip(daily, daily[1:]):
        trs.append(max(cur["high"] - cur["low"], abs(cur["high"] - prev["close"]), abs(cur["low"] - prev["close"])))
    value = sum(trs[:period]) / period
    for tr in trs[period:]:
        value = (value * (period - 1) + tr) / period
    return round(value, 6)


def technicals_crosscheck(symbol: str, reference_rsi_14: float | None = None, reference_atr_14: float | None = None,
                          days: int = 30, http: httpx.Client | None = None) -> Envelope:
    symbol = symbol.upper()
    days = max(15, min(int(days), 90))
    availability: dict[str, str] = {}
    warnings: list[str] = []
    data: dict[str, Any] = {"symbol": symbol, "method": {"indicators": "wilder", "period": PERIOD, "candles": "coingecko_ohlc_4h_to_utc_daily"},
                            "days_requested": days, "daily_candles": None, "as_of": None, "close": None, "rsi_14": None, "atr_14": None,
                            "atr_pct": None, "performance_pct": {"1d": None, "7d": None, "30d": None}, "reference": None,
                            "thresholds": {"rsi_warn_points": RSI_WARN_POINTS, "atr_warn_pct": ATR_WARN_PCT}}
    try:
        daily = to_daily(fetch_ohlc(symbol, days, http))
        availability["ohlc"] = "ok"
    except SourceUnavailable as exc:
        availability["ohlc"] = "unavailable"
        warnings.append(str(exc))
        daily = []
    if daily:
        closes = [d["close"] for d in daily]
        last = daily[-1]
        data.update(daily_candles=len(daily), as_of=datetime.fromtimestamp(last["ts"] / 1000, timezone.utc).isoformat(timespec="seconds"),
                    close=last["close"], rsi_14=rsi(closes), atr_14=atr(daily))
        if data["atr_14"] is not None and last["close"]:
            data["atr_pct"] = round(data["atr_14"] / last["close"] * 100, 3)
        for label, back in (("1d", 1), ("7d", 7), ("30d", 30)):
            if len(closes) > back and closes[-1 - back]:
                data["performance_pct"][label] = round((closes[-1] / closes[-1 - back] - 1) * 100, 3)
        if len(daily) < PERIOD + 1:
            warnings.append(f"only {len(daily)} daily candles; RSI/ATR need {PERIOD + 1}, reported as null")
        if daily and (datetime.now(timezone.utc) - datetime.fromtimestamp(last["ts"] / 1000, timezone.utc)).days >= 2:
            warnings.append("last candle is more than two days old")
    if reference_rsi_14 is not None or reference_atr_14 is not None:
        ref: dict[str, Any] = {"rsi_14": reference_rsi_14, "atr_14": reference_atr_14, "rsi_diff_points": None, "atr_diff_pct": None}
        if reference_rsi_14 is not None and data["rsi_14"] is not None:
            ref["rsi_diff_points"] = round(reference_rsi_14 - data["rsi_14"], 2)
            if abs(ref["rsi_diff_points"]) >= RSI_WARN_POINTS:
                warnings.append(f"reference RSI(14) {reference_rsi_14:g} differs from independent {data['rsi_14']} by {ref['rsi_diff_points']:+g} points")
        if reference_atr_14 is not None and data["atr_14"]:
            ref["atr_diff_pct"] = round((reference_atr_14 / data["atr_14"] - 1) * 100, 2)
            if abs(ref["atr_diff_pct"]) >= ATR_WARN_PCT:
                warnings.append(f"reference ATR(14) {reference_atr_14:g} differs from independent {data['atr_14']:g} by {ref['atr_diff_pct']:+g}%")
        data["reference"] = ref
    if data["rsi_14"] is None:
        headline = f"{symbol}: independent technicals unavailable"
    else:
        headline = f"{symbol}: independent RSI(14) {data['rsi_14']}, ATR(14) {data['atr_14']:g} ({data['atr_pct']}%) over {data['daily_candles']} daily candles"
        if data["reference"] and data["reference"]["rsi_diff_points"] is not None:
            headline += f"; reference RSI {data['reference']['rsi_diff_points']:+g} pts"
    return make_envelope("technicals_crosscheck", {"symbol": symbol, "reference_rsi_14": reference_rsi_14, "reference_atr_14": reference_atr_14, "days": days},
                         data, availability, warnings, headline,
                         key_points=[f"{k}: {v}%" for k, v in data["performance_pct"].items() if v is not None], primary=["ohlc"])
