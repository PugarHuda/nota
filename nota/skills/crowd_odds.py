"""Skill `crowd_odds`: what do people betting real money say the chance of a rise is?

A council's `p_up_7d` has to beat something harder than the calendar (move_base_rate): the market's own
implied probability. Polymarket lists, per day, a ladder of "Will <coin> be above $K on <date>?" markets,
and Kalshi lists the same ladder per week (series KXBTCD, KXETHD, ...). Each strike's Yes price is the
crowd's P(price > K at expiry). Reading the ladder at today's spot gives P(higher than now at expiry):
the same question `p_up_7d` answers, priced by others.

Method, stated so it can be checked:
- the ladder whose expiry is within ±1 day of now + horizon (nearest first) is used;
- a strike counts only when its book is two-sided and at most MAX_SPREAD wide, so an empty book's 0.5
  placeholder is never read as a probability;
- the Yes prices are made non-increasing in the strike (pool-adjacent-violators), because a higher bar
  cannot be likelier to clear, then linearly interpolated at spot; a spot outside the ladder is clamped
  to the end strike and flagged `extrapolated`;
- Polymarket first, Kalshi when Polymarket has no usable ladder. No usable ladder at all gives
  `market_p: null` and `unavailable`, never 0.5.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from nota.envelope import Envelope
from nota.skills.contract import SkillArg, SkillDefinition, SourceUnavailable, clean_symbol, make_envelope
from nota.skills.price_check import get_json
from nota.skills.sources import UA

DEFINITION = SkillDefinition(
    name="crowd_odds",
    description="Prediction-market implied probability that a token trades higher than now at a horizon: the Polymarket "
    "'above $K on <date>' ladder (Kalshi's KX<coin>D ladder as fallback) expiring within a day of now + horizon, liquid "
    "strikes only, made monotone and interpolated at spot. BTC, ETH, SOL, XRP (and DOGE on Kalshi); null when no market.",
    args=[
        SkillArg(name="symbol", type="string", description="Token symbol, e.g. BTC"),
        SkillArg(name="horizon_days", type="integer", required=False, description="1 to 14 (default 7)"),
        SkillArg(name="spot", type="number", required=False, description="Price to read the ladder at (default: Binance spot, "
                 "which is what Polymarket's crypto ladders resolve on)"),
    ],
)

GAMMA = "https://gamma-api.polymarket.com/events"
KALSHI = "https://api.elections.kalshi.com/trade-api/v2/markets"
BINANCE = "https://data-api.binance.vision/api/v3/ticker/price"
POLYMARKET_NAMES = {"BTC": "bitcoin", "ETH": "ethereum", "SOL": "solana", "XRP": "xrp"}
KALSHI_SERIES = {"BTC": "KXBTCD", "ETH": "KXETHD", "SOL": "KXSOLD", "XRP": "KXXRPD", "DOGE": "KXDOGED"}
MAX_SPREAD = 0.10
WINDOW_DAYS = 1
MONTHS = ("january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december")


def _num(v: Any) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f else None


def monotone(prices: list[float]) -> list[float]:
    """Nearest non-increasing sequence in least squares (pool adjacent violators), strikes ascending."""
    blocks: list[list[float]] = []  # [mean, weight]
    for p in prices:
        blocks.append([p, 1.0])
        while len(blocks) > 1 and blocks[-2][0] < blocks[-1][0]:
            m2, w2 = blocks.pop()
            m1, w1 = blocks.pop()
            blocks.append([(m1 * w1 + m2 * w2) / (w1 + w2), w1 + w2])
    return [m for m, w in blocks for _ in range(int(w))]


def p_above(strikes: list[float], prices: list[float], spot: float) -> tuple[float, bool]:
    """P(price > spot) read off a ladder (strikes ascending, prices already monotone). -> (p, extrapolated)."""
    if spot <= strikes[0]:
        return prices[0], spot < strikes[0]
    if spot >= strikes[-1]:
        return prices[-1], spot > strikes[-1]
    for i in range(1, len(strikes)):
        if spot <= strikes[i]:
            k0, k1 = strikes[i - 1], strikes[i]
            return prices[i - 1] + (prices[i] - prices[i - 1]) * (spot - k0) / (k1 - k0), False
    raise AssertionError("unreachable")


def _read(ladder: list[tuple[float, float]], spot: float) -> dict[str, Any] | None:
    ladder = sorted(ladder)
    if len(ladder) < 2:
        return None
    strikes = [k for k, _ in ladder]
    prices = monotone([p for _, p in ladder])
    p, extrapolated = p_above(strikes, prices, spot)
    return {"market_p": round(p, 4), "strikes": strikes, "prices": [round(x, 4) for x in prices],
            "raw_prices": [p for _, p in ladder], "extrapolated": extrapolated}


def _targets(now: datetime, horizon_days: int) -> list[datetime]:
    t = now + timedelta(days=horizon_days)
    return [t + timedelta(days=d) for d in (0, -1, 1)]  # nearest first


def _gap(expiry: str, target: datetime) -> float:
    """Seconds between an ISO expiry and the target time; unparseable is infinitely far."""
    try:
        return abs((datetime.fromisoformat(expiry.replace("Z", "+00:00")) - target).total_seconds())
    except ValueError:
        return float("inf")


def polymarket_ladder(event: dict[str, Any]) -> list[tuple[float, float]]:
    """(strike, Yes price) for each liquid strike of an 'above ___ on <date>' event."""
    out = []
    for m in event.get("markets") or []:
        if m.get("closed") or not m.get("active", True):
            continue
        strike = _num(str(m.get("groupItemTitle", "")).replace(",", "").replace("$", ""))
        try:
            outcomes, prices = json.loads(m.get("outcomes") or "[]"), json.loads(m.get("outcomePrices") or "[]")
        except (TypeError, ValueError):
            continue
        bid, ask = _num(m.get("bestBid")), _num(m.get("bestAsk"))
        if strike is None or "Yes" not in outcomes or len(prices) != len(outcomes) or bid is None or ask is None or ask - bid > MAX_SPREAD:
            continue
        yes = _num(prices[outcomes.index("Yes")])
        if yes is not None:
            out.append((strike, yes))
    return out


def polymarket(symbol: str, spot: float, targets: list[datetime], http: httpx.Client) -> dict[str, Any] | None:
    name = POLYMARKET_NAMES.get(symbol)
    if not name:
        return None
    for t in targets:
        slug = f"{name}-above-on-{MONTHS[t.month - 1]}-{t.day}-{t.year}"
        events = get_json(http, "polymarket", GAMMA, slug=slug)
        if not isinstance(events, list) or not events or _gap(str(events[0].get("endDate")), targets[0]) > WINDOW_DAYS * 86400:
            continue
        read = _read(polymarket_ladder(events[0]), spot)
        if read:
            return {**read, "source": "polymarket", "event_slug": slug, "expiry": events[0].get("endDate")}
    return None


def kalshi_ladder(markets: list[dict[str, Any]]) -> dict[str, list[tuple[float, float]]]:
    """close_time -> (strike, Yes mid) for 'greater' markets with a two-sided book at most MAX_SPREAD wide."""
    out: dict[str, list[tuple[float, float]]] = {}
    for m in markets:
        bid, ask, strike = _num(m.get("yes_bid_dollars")), _num(m.get("yes_ask_dollars")), _num(m.get("floor_strike"))
        if m.get("strike_type") != "greater" or strike is None or not bid or ask is None or ask - bid > MAX_SPREAD:
            continue
        out.setdefault(str(m.get("close_time")), []).append((strike, (bid + ask) / 2))
    return out


def kalshi(symbol: str, spot: float, targets: list[datetime], http: httpx.Client) -> dict[str, Any] | None:
    series = KALSHI_SERIES.get(symbol)
    if not series:
        return None
    lo, hi = min(targets), max(targets)
    body = get_json(http, "kalshi", KALSHI, series_ticker=series, status="open", limit=1000,
                    min_close_ts=int(lo.timestamp()), max_close_ts=int(hi.timestamp()))
    ladders = kalshi_ladder((body or {}).get("markets") or [])
    for close in sorted(ladders, key=lambda c: _gap(c, targets[0])):
        if _gap(close, targets[0]) > WINDOW_DAYS * 86400:
            continue
        read = _read(ladders[close], spot)
        if read:
            ticker = next((m.get("event_ticker") for m in body["markets"] if str(m.get("close_time")) == close), None)
            return {**read, "source": "kalshi", "ticker": ticker, "expiry": close}
    return None


def binance_spot(symbol: str, http: httpx.Client) -> float:
    d = get_json(http, "binance", BINANCE, symbol=f"{symbol}USDT")
    p = _num((d or {}).get("price"))
    if p is None:
        raise SourceUnavailable("binance: no price in response")
    return p


def crowd_odds(symbol: str, horizon_days: int = 7, spot: float | None = None, http: httpx.Client | None = None,
               now: datetime | None = None) -> Envelope:
    symbol = clean_symbol(symbol)
    if not 1 <= horizon_days <= 14:
        raise ValueError("crowd_odds: horizon_days must be 1 to 14")
    if spot is not None and spot <= 0:
        raise ValueError("crowd_odds: spot must be positive")
    http = http or httpx.Client(timeout=20.0, headers={"User-Agent": UA})
    now = now or datetime.now(timezone.utc)
    request = {"symbol": symbol, "horizon_days": horizon_days, "spot": spot}
    availability: dict[str, str] = {}
    warnings: list[str] = []
    data: dict[str, Any] = {"symbol": symbol, "market_p": None, "source": None, "horizon_days": horizon_days,
                            "spot": spot, "spot_source": "request" if spot is not None else None,
                            "as_of": now.isoformat(timespec="seconds"), "max_spread": MAX_SPREAD, "window_days": WINDOW_DAYS}
    if spot is None:
        try:
            data["spot"], data["spot_source"] = binance_spot(symbol, http), "binance"
        except SourceUnavailable as exc:
            warnings.append(f"spot: {exc}")
    targets = _targets(now, horizon_days)
    read = None
    if data["spot"] is not None:
        for name, fn in (("polymarket", polymarket), ("kalshi", kalshi)):
            if read:
                break
            try:
                read = fn(symbol, data["spot"], targets, http)
                availability[name] = "available" if read else "unavailable"
                if not read and (symbol in POLYMARKET_NAMES if name == "polymarket" else symbol in KALSHI_SERIES):
                    warnings.append(f"{name}: no ladder with two or more liquid strikes expiring within {WINDOW_DAYS} day of {targets[0]:%Y-%m-%d}")
            except SourceUnavailable as exc:
                availability[name] = "unavailable"
                warnings.append(str(exc))
    if read:
        data.update(read)
        if read["extrapolated"]:
            warnings.append(f"spot {data['spot']:g} is outside the ladder {read['strikes'][0]:g}-{read['strikes'][-1]:g}; clamped to the end strike")
        headline = (f"{symbol}: {read['source']} prices P(above {data['spot']:g} at {str(read['expiry'])[:16]}) at {read['market_p']:.2f} "
                    f"from {len(read['strikes'])} liquid strikes")
    else:
        availability.setdefault("market", "unavailable")
        headline = f"{symbol}: no prediction-market ladder to read" + ("" if symbol in KALSHI_SERIES else " (none is listed for this token)")
    primary = [k for k in ("polymarket", "kalshi", "market") if k in availability]
    # One answering venue is the whole answer; the other being unused or empty is not a degradation.
    if read:
        primary = [read["source"]]
    return make_envelope("crowd_odds", request, data, availability, warnings, headline,
                         [f"{k:g}: {p:.3f}" for k, p in zip(data.get("strikes") or [], data.get("prices") or [])], primary=primary)
