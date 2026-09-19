"""Skill `technicals_crosscheck`: RSI(14) and ATR(14) computed independently from public daily candles.

RYO's `analyze_token` / `deep_analysis` report RSI(14) and ATR(14). This skill recomputes both with
Wilder's method from closed UTC-day exchange candles, so a reader can see whether RYO's indicators
agree with an independent calculation. Wilder smoothing remembers its seed, so the indicators run over
200 closed days (OKX first, Binance's public mirror if OKX fails) and today's unfinished candle is
never used. CoinGecko's 4-hour OHLC, aggregated to UTC days, is the last resort and says it has too
few days to converge. Nothing is smoothed over: too few candles means `null`, and the candle source
and count are printed with every number.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

import httpx

from nota.envelope import Envelope
from nota.skills.contract import SkillArg, SkillDefinition, SourceUnavailable, clean_symbol, make_envelope
from nota.skills.price_check import coingecko_id, get_json
from nota.skills.sources import UA

DEFINITION = SkillDefinition(
    name="technicals_crosscheck",
    description="Recompute RSI(14) and ATR(14) (Wilder smoothing) from 200 closed UTC-day candles (OKX, else Binance, else "
    "CoinGecko 4h OHLC aggregated to days) plus 1d/7d/30d performance, and report how far reference values (e.g. RYO's) "
    "deviate from the independent calculation.",
    args=[
        SkillArg(name="symbol", type="string", description="Token symbol, e.g. SOL"),
        SkillArg(name="reference_rsi_14", type="number", required=False, description="RSI(14) to compare against"),
        SkillArg(name="reference_atr_14", type="number", required=False, description="ATR(14) in USD to compare against"),
        SkillArg(name="days", type="integer", required=False,
                 description="lookback for performance, 7..90 (indicators always use 200 closed daily candles)"),
    ],
)

PERIOD = 14
CANDLES = 200
CONVERGED = 100  # below this many closes the Wilder seed still shows in RSI/ATR
DAY_MS = 86_400_000
H4_MS = 4 * 3_600_000
COINGECKO_ID = re.compile(r"[a-z0-9-]{1,80}")  # the id comes back from CoinGecko's search and goes into a URL path
RSI_WARN_POINTS = 10.0
ATR_WARN_PCT = 25.0
BINANCE = "https://data-api.binance.vision/api/v3/klines"


def binance_daily(symbol: str, http: httpx.Client, limit: int = CANDLES) -> list[dict[str, float]]:
    """Closed UTC-day candles from Binance's public data mirror, oldest first (USDT pair)."""
    rows = get_json(http, "binance", BINANCE, symbol=f"{symbol}USDT", interval="1d", limit=limit)
    if not isinstance(rows, list) or not rows:
        raise SourceUnavailable(f"binance: no daily candles for {symbol}USDT")
    now_ms = datetime.now(timezone.utc).timestamp() * 1000
    return [{"ts": int(r[0]), "open": float(r[1]), "high": float(r[2]), "low": float(r[3]), "close": float(r[4])}
            for r in rows if int(r[6]) <= now_ms]  # r[6] is close time: a later one is today's candle, still moving


def fetch_ohlc(symbol: str, days: int, http: httpx.Client | None = None) -> list[list[float]]:
    """CoinGecko `/coins/{id}/ohlc`: [[ms, open, high, low, close], ...]; 4h candles for 3-30 days, 4-day candles beyond,
    and the timestamp is each candle's close."""
    http = http or httpx.Client(timeout=20.0, headers={"User-Agent": UA})
    cid = coingecko_id(symbol, http)
    if not COINGECKO_ID.fullmatch(str(cid)):
        raise SourceUnavailable(f"coingecko: unexpected coin id {str(cid)[:80]!r}")
    rows = get_json(http, "coingecko ohlc", f"https://api.coingecko.com/api/v3/coins/{cid}/ohlc", vs_currency="usd", days=days)
    if not isinstance(rows, list) or not rows:
        raise SourceUnavailable("coingecko ohlc: empty response")
    return [[float(x) for x in r] for r in rows]


def to_daily(candles: list[list[float]]) -> list[dict[str, float]]:
    """Aggregate intraday candles into UTC-day candles (open first, high max, low min, close last). CoinGecko stamps a
    candle with its close, so the one stamped 00:00 belongs to the day before; `ts` is the UTC day's start."""
    days: dict[int, dict[str, float]] = {}
    for ts, o, h, lo, c in candles:
        key = int(ts - 1) // DAY_MS * DAY_MS
        d = days.get(key)
        if d is None:
            days[key] = {"ts": key, "open": o, "high": h, "low": lo, "close": c}
        else:
            d["high"], d["low"], d["close"] = max(d["high"], h), min(d["low"], lo), c
    return [days[k] for k in sorted(days)]


def closed_daily(symbol: str, http: httpx.Client, warnings: list[str]) -> tuple[list[dict[str, float]], str, str]:
    """(candles, source, method): closed UTC-day candles, oldest first, from the first source that answers."""
    from nota.skills.base_rate import okx_daily  # imported here: base_rate takes PERIOD from this module

    try:
        return okx_daily(symbol, http, pages=3), "okx", "okx_1Dutc"  # 300 rows: 200 closed ones after today is dropped
    except SourceUnavailable as exc:
        warnings.append(str(exc))
    try:
        return binance_daily(symbol, http, CANDLES + 1), "binance", "binance_1d"
    except SourceUnavailable as exc:
        warnings.append(str(exc))
    rows = fetch_ohlc(symbol, 30, http)  # 30 days is the longest window CoinGecko still serves as 4h candles
    kept = rows[:1] + [b for a, b in zip(rows, rows[1:]) if b[0] - a[0] <= H4_MS]
    if len(kept) < len(rows):
        warnings.append(f"coingecko: skipped {len(rows) - len(kept)} candles spaced wider than 4h")
    warnings.append(f"coingecko 4h candles aggregated; fewer than {CONVERGED} candles, RSI/ATR not fully converged")
    return to_daily(kept), "coingecko", "coingecko_ohlc_4h_to_utc_daily"


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
    return float(f"{value:.6g}")  # significant digits: six decimals rounded a PEPE-sized ATR to 0


def technicals_crosscheck(symbol: str, reference_rsi_14: float | None = None, reference_atr_14: float | None = None,
                          days: int = 30, http: httpx.Client | None = None) -> Envelope:
    symbol = clean_symbol(symbol)
    warnings: list[str] = []
    asked = int(days)
    days = max(7, min(asked, 90))
    if days != asked:
        warnings.append(f"days {asked} clamped to {days} (7..90)")
    availability: dict[str, str] = {}
    labels = list(dict.fromkeys(["1d", "7d", "30d", f"{days}d"]))
    data: dict[str, Any] = {"symbol": symbol, "method": {"indicators": "wilder", "period": PERIOD, "candles": None},
                            "candle_source": None, "warmup_candles": None, "days_requested": days, "daily_candles": None,
                            "as_of": None, "close": None, "rsi_14": None, "atr_14": None, "atr_pct": None,
                            "performance_pct": {k: None for k in labels}, "reference": None,
                            "thresholds": {"rsi_warn_points": RSI_WARN_POINTS, "atr_warn_pct": ATR_WARN_PCT}}
    try:
        daily, source, method = closed_daily(symbol, http or httpx.Client(timeout=20.0, headers={"User-Agent": UA}), warnings)
        today = int(datetime.now(timezone.utc).timestamp() * 1000) // DAY_MS * DAY_MS
        daily = [d for d in daily if d["ts"] < today][-CANDLES:]  # today's candle is still moving
        availability["ohlc"] = "available"
        data["method"]["candles"], data["candle_source"] = method, source
    except SourceUnavailable as exc:
        availability["ohlc"] = "unavailable"
        warnings.append(str(exc))
        daily = []
    if daily:
        closes = [d["close"] for d in daily]
        last = daily[-1]
        data.update(daily_candles=len(daily), warmup_candles=len(closes),
                    as_of=datetime.fromtimestamp((last["ts"] + DAY_MS) / 1000, timezone.utc).isoformat(timespec="seconds"),
                    close=last["close"], rsi_14=rsi(closes), atr_14=atr(daily))
        if data["atr_14"] is not None and last["close"]:
            data["atr_pct"] = round(data["atr_14"] / last["close"] * 100, 3)
        for label in labels:
            back = int(label[:-1])
            if len(closes) > back and closes[-1 - back]:
                data["performance_pct"][label] = round((closes[-1] / closes[-1 - back] - 1) * 100, 3)
        if len(daily) < PERIOD + 1:
            warnings.append(f"only {len(daily)} daily candles; RSI/ATR need {PERIOD + 1}, reported as null")
        elif len(daily) < CONVERGED and data["candle_source"] != "coingecko":
            warnings.append(f"only {len(daily)} closed daily candles; fewer than {CONVERGED}, RSI/ATR not fully converged")
        if (today - last["ts"]) // DAY_MS > 2:
            warnings.append("last closed candle is more than two days old")
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
        headline = (f"{symbol}: independent RSI(14) {data['rsi_14']}, ATR(14) {data['atr_14']:g} ({data['atr_pct']}%) over "
                    f"{data['daily_candles']} closed daily candles ({data['candle_source']})")
        if data["reference"] and data["reference"]["rsi_diff_points"] is not None:
            headline += f"; reference RSI {data['reference']['rsi_diff_points']:+g} pts"
    return make_envelope("technicals_crosscheck", {"symbol": symbol, "reference_rsi_14": reference_rsi_14, "reference_atr_14": reference_atr_14, "days": days},
                         data, availability, warnings, headline,
                         key_points=[f"{k}: {v}%" for k, v in data["performance_pct"].items() if v is not None], primary=["ohlc"])
