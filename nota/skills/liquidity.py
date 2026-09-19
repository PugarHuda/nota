"""Skill `liquidity_check`: is money arriving in crypto or leaving it?

None of RYO's six tools measures liquidity. Two public DefiLlama series do, keylessly:

- stablecoin supply: the total USD-pegged stablecoin float (`stablecoincharts/all`). Stablecoins are the
  cash crypto is bought with; a float that grows is fresh buying power, one that shrinks is redemptions.
- chain TVL: the value locked in DeFi on the token's own chain (`v2/historicalChainTvl/<Chain>`), for an
  L1 or L2 token whose demand is tied to that chain's activity.

Each is reported as its 7-day and 30-day change with the date of its last row, as its own availability
leg. A token with no chain of its own (BTC; DefiLlama's "Bitcoin" chain is mostly restaking deposits, not
demand for BTC) or not in CHAINS gets `tvl` unavailable with the reason, never a neighbouring chain.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from nota.envelope import Envelope
from nota.skills.contract import SkillArg, SkillDefinition, SourceUnavailable, clean_symbol, make_envelope
from nota.skills.price_check import get_json
from nota.skills.sources import UA

DEFINITION = SkillDefinition(
    name="liquidity_check",
    description="Macro liquidity from DefiLlama (no key): 7-day and 30-day change of the total USD stablecoin supply, and of "
    "DeFi TVL on the token's own chain (ETH, SOL, BNB, AVAX and other L1/L2 tokens). Each leg reports its own as_of and "
    "availability; a token without a chain of its own gets no TVL rather than a borrowed one.",
    args=[SkillArg(name="symbol", type="string", description="Token symbol, e.g. SOL")],
)

STABLES = "https://stablecoins.llama.fi/stablecoincharts/all"
CHAIN_TVL = "https://api.llama.fi/v2/historicalChainTvl/{chain}"
# token -> DefiLlama chain name, each checked against the live endpoint on 2026-09-19
CHAINS = {"ETH": "Ethereum", "SOL": "Solana", "BNB": "BSC", "AVAX": "Avalanche", "TRX": "Tron", "SUI": "Sui", "ADA": "Cardano",
          "NEAR": "Near", "APT": "Aptos", "ARB": "Arbitrum", "OP": "OP Mainnet", "INJ": "Injective", "ATOM": "CosmosHub",
          "DOT": "Polkadot", "TON": "TON", "POL": "Polygon", "SEI": "Sei", "HYPE": "Hyperliquid L1"}
NO_CHAIN = {"BTC": "BTC has no DeFi chain whose TVL tracks demand for it (DefiLlama's Bitcoin chain is mostly restaking deposits)"}
WINDOWS = (7, 30)


def _day(ts: Any) -> datetime:
    return datetime.fromtimestamp(int(ts), timezone.utc)


def changes(series: list[tuple[datetime, float]]) -> dict[str, Any]:
    """Percent change over each window, from the last row to the last row on or before that many days
    earlier. A window the series does not reach back to is null, not extrapolated."""
    series = sorted(series)
    if not series:
        raise SourceUnavailable("empty series")
    last_t, last_v = series[-1]
    out: dict[str, Any] = {"latest_usd": last_v, "as_of": last_t.isoformat(timespec="seconds")}
    for d in WINDOWS:
        then = [v for t, v in series if t <= last_t - timedelta(days=d)]
        out[f"change_{d}d_pct"] = round((last_v / then[-1] - 1) * 100, 3) if then and then[-1] else None
    return out


def stablecoin_supply(http: httpx.Client) -> dict[str, Any]:
    rows = get_json(http, "defillama stablecoins", STABLES)
    series = [(_day(r["date"]), float(r["totalCirculatingUSD"]["peggedUSD"])) for r in rows or []
              if isinstance(r, dict) and isinstance((r.get("totalCirculatingUSD") or {}).get("peggedUSD"), (int, float))]
    return changes(series)


def chain_tvl(chain: str, http: httpx.Client) -> dict[str, Any]:
    rows = get_json(http, "defillama tvl", CHAIN_TVL.format(chain=chain))
    series = [(_day(r["date"]), float(r["tvl"])) for r in rows or [] if isinstance(r, dict) and isinstance(r.get("tvl"), (int, float))]
    return {"chain": chain, **changes(series)}


def _signed(v: float | None) -> str:
    return "n/a" if v is None else f"{v:+.2f}%"


def liquidity_check(symbol: str, http: httpx.Client | None = None) -> Envelope:
    symbol = clean_symbol(symbol)
    http = http or httpx.Client(timeout=20.0, headers={"User-Agent": UA})
    availability: dict[str, str] = {}
    warnings: list[str] = []
    data: dict[str, Any] = {"symbol": symbol, "stablecoins": None, "tvl": None,
                            "sources": {"stablecoins": STABLES, "tvl": CHAIN_TVL.format(chain="<chain>")}}
    try:
        data["stablecoins"] = stablecoin_supply(http)
        availability["stablecoins"] = "available"
    except (SourceUnavailable, KeyError, TypeError, ValueError) as exc:
        availability["stablecoins"] = "unavailable"
        warnings.append(f"stablecoins: {exc}")
    chain = CHAINS.get(symbol)
    if chain is None:
        availability["tvl"] = "unavailable"
        warnings.append(NO_CHAIN.get(symbol, f"no DeFi chain of its own mapped for {symbol}; TVL not used"))
    else:
        try:
            data["tvl"] = chain_tvl(chain, http)
            availability["tvl"] = "available"
        except (SourceUnavailable, KeyError, TypeError, ValueError) as exc:
            availability["tvl"] = "unavailable"
            warnings.append(f"tvl {chain}: {exc}")
    points, parts = [], []
    if data["stablecoins"]:
        s = data["stablecoins"]
        parts.append(f"stablecoin supply {_signed(s['change_7d_pct'])} 7d / {_signed(s['change_30d_pct'])} 30d")
        points.append(f"stablecoin supply {s['latest_usd'] / 1e9:.1f} bn USD as of {s['as_of'][:10]}")
    if data["tvl"]:
        t = data["tvl"]
        parts.append(f"{t['chain']} TVL {_signed(t['change_7d_pct'])} 7d / {_signed(t['change_30d_pct'])} 30d")
        points.append(f"{t['chain']} TVL {t['latest_usd'] / 1e9:.2f} bn USD as of {t['as_of'][:10]}")
    headline = f"{symbol}: " + ("; ".join(parts) if parts else "no liquidity series available")
    # Primary is the stablecoin float, which every token has; TVL exists only for chain tokens, so a BTC
    # read with a good float is complete, and a chain token whose TVL failed is partial.
    primary = ["stablecoins"] + (["tvl"] if chain else [])
    return make_envelope("liquidity_check", {"symbol": symbol}, data, availability, warnings, headline, points, primary=primary)
