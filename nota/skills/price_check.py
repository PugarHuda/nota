"""Skill `price_crosscheck`: independent spot prices next to RYO's read, never instead of it.

Five keyless public sources (CoinGecko, Coinbase, Kraken, Binance's public data mirror, DefiLlama
coins). The skill reports each source's own price and timestamp, the median and spread, and, when a
reference price is supplied, how far RYO's number sits from the exchanges. When the sources disagree by
more than SPREAD_WARN_PCT, the one farthest from the others is marked `outlier` and left out of the
median: a symbol search that lands on a different coin (CoinGecko's `AI` is not the exchanges' `AI`)
must not move the price. RYO's value is never overwritten; a large deviation becomes a warning the
council and the reader can see.
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from statistics import median
from typing import Any

import httpx

from nota.envelope import Envelope
from nota.skills.contract import SkillArg, SkillDefinition, SourceUnavailable, clean_symbol, make_envelope
from nota.skills.sources import UA

DEFINITION = SkillDefinition(
    name="price_crosscheck",
    description="Fetch independent USD spot prices for a symbol from CoinGecko, Coinbase, Kraken, Binance and DefiLlama (no "
    "keys), drop a source that disagrees with the rest as an outlier, report median and spread, and flag how far a "
    "reference price (e.g. RYO's) deviates from them.",
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
SPREAD_WARN_PCT = 2.0
LLAMA_MIN_CONFIDENCE = 0.9
RETRY_AFTER_CAP_S = 10.0
PRICE_LEGS = ("coingecko", "coinbase", "kraken", "binance", "defillama")


def get_json(http: httpx.Client, name: str, url: str, **params: Any) -> Any:
    """GET -> JSON or SourceUnavailable. CoinGecko's keyless tier answers 429 in bursts, so a CoinGecko
    429 is retried once after its Retry-After (capped); a demo key in COINGECKO_API_KEY raises the limit."""
    cg = url.startswith("https://api.coingecko.com/")
    headers = {"x-cg-demo-api-key": os.environ["COINGECKO_API_KEY"]} if cg and os.environ.get("COINGECKO_API_KEY") else None
    for attempt in (0, 1):
        try:
            resp = http.get(url, params=params or None, headers=headers)
        except httpx.HTTPError as exc:
            raise SourceUnavailable(f"{name}: network error {type(exc).__name__}") from exc
        if resp.status_code != 429 or attempt or not cg:
            break
        try:
            wait = float(resp.headers.get("retry-after", "1"))
        except ValueError:  # an HTTP-date Retry-After: wait the minimum rather than trust a remote clock
            wait = 1.0
        time.sleep(max(0.0, min(wait, RETRY_AFTER_CAP_S)))
    if resp.status_code != 200:
        raise SourceUnavailable(f"{name}: HTTP {resp.status_code}")
    try:
        return resp.json()
    except ValueError as exc:
        raise SourceUnavailable(f"{name}: non-JSON response") from exc


# symbol -> resolved id. ponytail: successes only and never evicted; a few hundred symbols at most per process.
_CG_IDS: dict[str, str] = {}


def coingecko_id(symbol: str, http: httpx.Client) -> str:
    """The CoinGecko id for a symbol. Search hits sharing the symbol are ranked by market-cap rank (unranked
    last): the first hit is often a small token with the same ticker, which priced `AI` 14x off the exchanges."""
    cid = COINGECKO_IDS.get(symbol) or _CG_IDS.get(symbol)
    if cid:
        return cid
    coins = [c for c in get_json(http, "coingecko", "https://api.coingecko.com/api/v3/search", query=symbol).get("coins", [])
             if str(c.get("symbol", "")).upper() == symbol]
    if not coins:
        raise SourceUnavailable(f"coingecko: no coin with symbol {symbol}")
    coins.sort(key=lambda c: (c.get("market_cap_rank") is None, c.get("market_cap_rank") or 0))
    _CG_IDS[symbol] = cid = str(coins[0]["id"])
    return cid


class ExchangePrices:
    def __init__(self, http: httpx.Client | None = None):
        self.http = http or httpx.Client(timeout=15.0, headers={"User-Agent": UA})

    def _get(self, name: str, url: str, **params: Any) -> Any:
        return get_json(self.http, name, url, **params)

    def coingecko(self, symbol: str) -> tuple[float, str | None]:
        cid = coingecko_id(symbol, self.http)
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

    def binance(self, symbol: str) -> tuple[float, str | None]:
        """Binance's public market-data mirror (answers where api.binance.com is geo-blocked). USDT pair."""
        d = self._get("binance", "https://data-api.binance.vision/api/v3/ticker/price", symbol=f"{symbol}USDT")
        if "price" not in d:
            raise SourceUnavailable("binance: no price in response")
        return float(d["price"]), None

    def defillama(self, symbol: str) -> tuple[float, str | None]:
        """DefiLlama coins API, keyed by the CoinGecko id; its own confidence score gates the price."""
        cid = coingecko_id(symbol, self.http)
        d = (self._get("defillama", f"https://coins.llama.fi/prices/current/coingecko:{cid}").get("coins") or {}).get(f"coingecko:{cid}") or {}
        if "price" not in d:
            raise SourceUnavailable(f"defillama: no price for coingecko:{cid}")
        if float(d.get("confidence") or 0) < LLAMA_MIN_CONFIDENCE:
            raise SourceUnavailable(f"defillama: confidence {d.get('confidence')} below {LLAMA_MIN_CONFIDENCE}")
        ts = d.get("timestamp")
        return float(d["price"]), datetime.fromtimestamp(int(ts), timezone.utc).isoformat(timespec="seconds") if ts else None

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
    symbol = clean_symbol(symbol)  # it goes into Coinbase's URL path, so nothing but letters and digits
    ex = exchanges or ExchangePrices()
    fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    availability: dict[str, str] = {}
    warnings: list[str] = []
    sources: list[dict[str, Any]] = []
    for name in PRICE_LEGS:
        try:
            price, as_of = getattr(ex, name)(symbol)
            sources.append({"name": name, "price_usd": price, "as_of": as_of, "status": "available"})
            availability[name] = "available"
            if name == "binance":
                warnings.append("binance: USDT used as USD proxy")
        except SourceUnavailable as exc:
            sources.append({"name": name, "price_usd": None, "as_of": None, "status": "unavailable", "error": str(exc)})
            availability[name] = "unavailable"
            warnings.append(str(exc))
    fng: dict[str, Any] | None = None
    try:
        value, label, as_of = ex.fear_greed()
        fng = {"value": value, "classification": label, "as_of": as_of, "source": "alternative.me", "reference_value": reference_fear_greed,
               "delta": (value - reference_fear_greed) if reference_fear_greed is not None else None}
        availability["fear_greed"] = "available"
        if fng["delta"] is not None and abs(fng["delta"]) >= FNG_WARN_DELTA:
            warnings.append(f"RYO fear/greed {reference_fear_greed:g} differs from alternative.me {value} by {fng['delta']:+g} points")
    except SourceUnavailable as exc:
        availability["fear_greed"] = "unavailable"
        warnings.append(str(exc))
    live = [s for s in sources if s["price_usd"] is not None]
    # DefiLlama is looked up by CoinGecko's id, so it is no second vote on *which* coin a symbol is: it is left out of
    # the vote and goes wherever CoinGecko goes. Otherwise a wrong id outvotes the exchanges two to one.
    voters = [s for s in live if s["name"] != "defillama"]

    def others(s: dict[str, Any]) -> float:
        return median(p["price_usd"] for p in voters if p is not s)

    prices = [s["price_usd"] for s in live]
    if len(voters) >= 3 and min(prices) > 0 and (max(prices) - min(prices)) / median(prices) * 100 > SPREAD_WARN_PCT:
        # the source farthest from the others is shown, not used; with two voters there is no majority to judge by
        odd = max(voters, key=lambda s: abs(s["price_usd"] / others(s) - 1))
        rest = others(odd)
        out = [odd] + ([s for s in live if s["name"] == "defillama"] if odd["name"] == "coingecko" else [])
        for s in out:
            warnings.append(f"{s['name']} {s['price_usd']:g} is {abs(s['price_usd'] / rest - 1) * 100:.1f}% from the other "
                            f"sources' median {rest:g}; excluded as an outlier")
            s["status"] = availability[s["name"]] = "outlier"
            live.remove(s)
        if odd["name"] == "coingecko":
            warnings.append(f"coingecko id {COINGECKO_IDS.get(symbol) or _CG_IDS.get(symbol)} may be a different asset")
        prices = [s["price_usd"] for s in live]
    med = median(prices) if prices else None
    spread = round((max(prices) - min(prices)) / med * 100, 3) if len(prices) >= 2 and med else None
    if spread is not None and spread > SPREAD_WARN_PCT:
        warnings.append(f"sources disagree: spread {spread}% between {min(prices):g} and {max(prices):g}")
    data: dict[str, Any] = {"symbol": symbol, "fetched_at": fetched_at, "sources": sources, "median_usd": med, "spread_pct": spread,
                            "coingecko_id": COINGECKO_IDS.get(symbol) or _CG_IDS.get(symbol),
                            "sources_ok": len(prices), "reference": None, "fear_greed": fng,
                            "thresholds": {"deviation_warn_pct": DEVIATION_WARN_PCT, "spread_warn_pct": SPREAD_WARN_PCT,
                                           "defillama_min_confidence": LLAMA_MIN_CONFIDENCE, "fear_greed_warn_delta": FNG_WARN_DELTA}}
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
                         key_points=[f"{s['name']}: {s['price_usd']}" + (" (outlier)" if s["status"] == "outlier" else "")
                                     for s in sources if s["price_usd"] is not None],
                         primary=list(PRICE_LEGS))
