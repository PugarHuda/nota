"""Skill `price_crosscheck`: independent spot prices next to RYO's read, never instead of it.

Three keyless public sources (CoinGecko, Coinbase, Kraken). The skill reports each source's own
price and timestamp, the median and spread, and, when a reference price is supplied, how far RYO's
number sits from the exchanges. RYO's value is never overwritten; a large deviation becomes a warning
the council and the reader can see.
"""

from __future__ import annotations

from datetime import datetime, timezone
from statistics import median
from typing import Any

import httpx

from nota.envelope import Envelope
from nota.skills.contract import SkillArg, SkillDefinition, SourceUnavailable, make_envelope
from nota.skills.sources import UA

DEFINITION = SkillDefinition(
    name="price_crosscheck",
    description="Fetch independent USD spot prices for a symbol from CoinGecko, Coinbase and Kraken (no keys), report "
    "median and spread, and flag how far a reference price (e.g. RYO's) deviates from the exchanges.",
    args=[
        SkillArg(name="symbol", type="string", description="Token symbol, e.g. SOL"),
        SkillArg(name="reference_price", type="number", required=False, description="Price to compare against, e.g. RYO deep_analysis price"),
        SkillArg(name="reference_path", type="string", required=False, description="Where the reference price came from"),
        SkillArg(name="reference_fear_greed", type="number", required=False, description="A Fear & Greed value to compare with alternative.me"),
    ],
)
FNG_WARN_DELTA = 10

COINGECKO_IDS = {"BTC": "bitcoin", "ETH": "ethereum", "SOL": "solana", "BNB": "binancecoin", "XRP": "ripple", "DOGE": "dogecoin",
                 "ADA": "cardano", "AVAX": "avalanche-2", "LINK": "chainlink", "POL": "polygon-ecosystem-token", "TON": "the-open-network",
                 "SUI": "sui", "DOT": "polkadot", "LTC": "litecoin", "TRX": "tron", "USDT": "tether", "USDC": "usd-coin"}
KRAKEN_ALIASES = {"BTC": "XBT", "DOGE": "XDG"}
DEVIATION_WARN_PCT = 2.0


class ExchangePrices:
    def __init__(self, http: httpx.Client | None = None):
        self.http = http or httpx.Client(timeout=15.0, headers={"User-Agent": UA})

    def _get(self, name: str, url: str, **params: Any) -> Any:
        try:
            resp = self.http.get(url, params=params or None)
        except httpx.HTTPError as exc:
            raise SourceUnavailable(f"{name}: network error {type(exc).__name__}") from exc
        if resp.status_code != 200:
            raise SourceUnavailable(f"{name}: HTTP {resp.status_code}")
        try:
            return resp.json()
        except ValueError as exc:
            raise SourceUnavailable(f"{name}: non-JSON response") from exc

    def coingecko(self, symbol: str) -> tuple[float, str | None]:
        cid = COINGECKO_IDS.get(symbol)
        if not cid:
            coins = [c for c in self._get("coingecko", "https://api.coingecko.com/api/v3/search", query=symbol).get("coins", [])
                     if str(c.get("symbol", "")).upper() == symbol]
            if not coins:
                raise SourceUnavailable(f"coingecko: no coin with symbol {symbol}")
            cid = coins[0]["id"]
        d = self._get("coingecko", "https://api.coingecko.com/api/v3/simple/price", ids=cid, vs_currencies="usd",
                      include_last_updated_at="true").get(cid) or {}
        if "usd" not in d:
            raise SourceUnavailable("coingecko: no usd price in response")
        ts = d.get("last_updated_at")
        as_of = datetime.fromtimestamp(int(ts), timezone.utc).isoformat(timespec="seconds") if ts else None
        return float(d["usd"]), as_of

    def coinbase(self, symbol: str) -> tuple[float, str | None]:
        d = self._get("coinbase", f"https://api.coinbase.com/v2/prices/{symbol}-USD/spot").get("data") or {}
        if "amount" not in d:
            raise SourceUnavailable("coinbase: no amount in response")
        return float(d["amount"]), None  # Coinbase spot carries no timestamp; fetched_at is reported instead

    def fear_greed(self) -> tuple[int, str, str | None]:
        """alternative.me Crypto Fear & Greed Index (no key). Returns (value, classification, as_of)."""
        rows = self._get("alternative.me", "https://api.alternative.me/fng/", limit=1).get("data") or []
        if not rows or "value" not in rows[0]:
            raise SourceUnavailable("alternative.me: no fear/greed value in response")
        ts = rows[0].get("timestamp")
        as_of = datetime.fromtimestamp(int(ts), timezone.utc).isoformat(timespec="seconds") if ts else None
        return int(rows[0]["value"]), str(rows[0].get("value_classification", "")), as_of

    def kraken(self, symbol: str) -> tuple[float, str | None]:
        pair = f"{KRAKEN_ALIASES.get(symbol, symbol)}USD"
        body = self._get("kraken", "https://api.kraken.com/0/public/Ticker", pair=pair)
        if body.get("error"):
            raise SourceUnavailable(f"kraken: {body['error'][0]}")
        result = body.get("result") or {}
        if not result:
            raise SourceUnavailable("kraken: empty result")
        last = next(iter(result.values())).get("c", [None])[0]
        if last is None:
            raise SourceUnavailable("kraken: no last-trade price")
        return float(last), None


def price_crosscheck(symbol: str, reference_price: float | None = None, reference_path: str | None = None,
                     reference_fear_greed: float | None = None, exchanges: ExchangePrices | None = None) -> Envelope:
    symbol = symbol.upper()
    ex = exchanges or ExchangePrices()
    fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    availability: dict[str, str] = {}
    warnings: list[str] = []
    sources: list[dict[str, Any]] = []
    for name, fn in (("coingecko", ex.coingecko), ("coinbase", ex.coinbase), ("kraken", ex.kraken)):
        try:
            price, as_of = fn(symbol)
            sources.append({"name": name, "price_usd": price, "as_of": as_of, "status": "ok"})
            availability[name] = "ok"
        except SourceUnavailable as exc:
            sources.append({"name": name, "price_usd": None, "as_of": None, "status": "unavailable", "error": str(exc)})
            availability[name] = "unavailable"
            warnings.append(str(exc))
    fng: dict[str, Any] | None = None
    try:
        value, label, as_of = ex.fear_greed()
        fng = {"value": value, "classification": label, "as_of": as_of, "source": "alternative.me", "reference_value": reference_fear_greed,
               "delta": (value - reference_fear_greed) if reference_fear_greed is not None else None}
        availability["fear_greed"] = "ok"
        if fng["delta"] is not None and abs(fng["delta"]) >= FNG_WARN_DELTA:
            warnings.append(f"RYO fear/greed {reference_fear_greed:g} differs from alternative.me {value} by {fng['delta']:+g} points")
    except SourceUnavailable as exc:
        availability["fear_greed"] = "unavailable"
        warnings.append(str(exc))
    prices = [s["price_usd"] for s in sources if s["price_usd"] is not None]
    med = median(prices) if prices else None
    spread = round((max(prices) - min(prices)) / med * 100, 3) if len(prices) >= 2 and med else None
    data: dict[str, Any] = {"symbol": symbol, "fetched_at": fetched_at, "sources": sources, "median_usd": med, "spread_pct": spread,
                            "sources_ok": len(prices), "reference": None, "fear_greed": fng,
                            "thresholds": {"deviation_warn_pct": DEVIATION_WARN_PCT, "fear_greed_warn_delta": FNG_WARN_DELTA}}
    if reference_price is not None:
        dev = round((reference_price / med - 1) * 100, 3) if med else None
        data["reference"] = {"price_usd": reference_price, "path": reference_path, "deviation_pct": dev}
        if dev is not None and abs(dev) >= DEVIATION_WARN_PCT:
            warnings.append(f"reference price {reference_price:g} deviates {dev:+.2f}% from the exchange median {med:g}")
    if med is None:
        headline = f"{symbol}: no independent price available"
    else:
        headline = f"{symbol}: exchange median {med:g} USD from {len(prices)} source(s)" + (f", spread {spread}%" if spread is not None else "")
        if data["reference"] and data["reference"]["deviation_pct"] is not None:
            headline += f"; reference {data['reference']['deviation_pct']:+.2f}%"
    if fng:
        headline += f"; fear/greed {fng['value']} ({fng['classification']})"
    return make_envelope("price_crosscheck", {"symbol": symbol, "reference_price": reference_price, "reference_path": reference_path,
                                              "reference_fear_greed": reference_fear_greed},
                         data, availability, warnings, headline,
                         key_points=[f"{s['name']}: {s['price_usd']}" for s in sources if s["price_usd"] is not None],
                         primary=["coingecko", "coinbase", "kraken"])
