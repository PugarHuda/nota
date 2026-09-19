"""Skill `positioning_check`: is RYO's derivatives block about *this* token, and what do venues say?

`deep_analysis` returns `derivatives.{funding_rate_bps, open_interest_change_24h_pct, long_short_ratio}`
with `availability.derivatives = "available"`. On 2026-09-18 funding was 0.0 and the ratio null for
every token probed, and the 24 h OI change repeated exactly across unrelated tokens in one run
(-10.44 for ETH, SOL, WIF and ONDO). RYO's guide documents no unit, venue or window for these fields,
so this skill never calls a RYO number "wrong" against a guessed definition. It only uses tests that
need no definition:

- `not_token_specific`: the same value on two or more *other* symbols from the same day.
- `conflicts_with_venue`: RYO's OI change and OKX's coin-terms OI change over 24 h have opposite
  signs, both at least SIGN_MIN_PCT in size.
- `conflicts_with_ryo`: RYO's own market-wide tool reports BTC funding while `deep_analysis` BTC says
  exactly 0.0.
- `citable`: a venue agrees in sign. `unverified`: nothing to check it against. `absent`: null.
  `not_provided`: no RYO block was passed and none could be fetched.

Called without `reference_derivatives`, the skill asks RYO's `deep_analysis` itself when a RYO source
is configured, and takes the same-day cross-section of peers from the scorecard's locks in the ledger.

It also reports OKX's own positioning. The per-8 h funding rate is not read as a signal: OKX sets it
to the interest component (1 bp) unless the perp's premium moves it, so the premium is what says
whether futures trade above spot. Open interest is in coin terms, because a USD series moves with
price alone. A long/short account ratio is only meaningful against its own history, so it is given
as a percentile of the last 100 hours.

For BTC and ETH it reads Deribit's DVOL, the options market's 30-day implied volatility (annualised),
and turns it into the move a normal week spans, DVOL / sqrt(365) x sqrt(7). A stop closer than half
of that sits inside ordinary noise. Deribit publishes the index for BTC and ETH only, so for every
other token the section is `unavailable` and nothing is estimated in its place.
"""

from __future__ import annotations

import math
import time
from datetime import datetime, timezone
from typing import Any

import httpx

from nota.envelope import Envelope
from nota.ledger import Ledger
from nota.ryo_client import RyoError, RyoSource
from nota.skills.contract import SkillArg, SkillDefinition, SourceUnavailable, make_envelope
from nota.skills.sources import UA

DEFINITION = SkillDefinition(
    name="positioning_check",
    description="Decide, field by field, whether RYO's deep_analysis derivatives block can be cited as evidence about this token "
    "(identical values across other tokens, sign conflicts with OKX coin-terms open interest), and report OKX perp positioning: "
    "premium over spot on OKX and Hyperliquid, 24 h open-interest change in coins, long/short account ratio and its 100-hour "
    "percentile, and for BTC/ETH Deribit's DVOL implied 7-day move as a check on stop distance.",
    args=[
        SkillArg(name="symbol", type="string", description="Token symbol, e.g. SOL"),
        SkillArg(name="reference_derivatives", type="object", required=False,
                 description="RYO deep_analysis data.derivatives for this symbol; fetched from RYO when omitted and a RYO key is set"),
        SkillArg(name="peer_derivatives", type="array", required=False, items={"type": "object"},
                 description="Same-day RYO derivatives blocks for other symbols: [{symbol, funding_rate_bps, open_interest_change_24h_pct}]; "
                 "taken from the day's scorecard locks when omitted"),
        SkillArg(name="ryo_btc_funding_bps", type="number", required=False,
                 description="Latest BTC funding from RYO monitor_market_sentiment_shift (evidence.funding.latest_bps)"),
        SkillArg(name="atr_stop_pct", type="number", required=False,
                 description="Stop distance in % of price (e.g. RYO's 1.5 x atr_14_pct), checked against the DVOL implied 7-day move"),
    ],
)

OKX = "https://www.okx.com/api/v5"
SIGN_MIN_PCT = 3.0
AT_DEFAULT_BPS = 1.0  # |premium| under 1 bp: perp and spot agree, neither side is pressing
FIELDS = ("funding_rate_bps", "open_interest_change_24h_pct", "long_short_ratio")


def _okx(http: httpx.Client, path: str, params: dict[str, Any]) -> list[Any]:
    try:
        resp = http.get(f"{OKX}{path}", params=params)
    except httpx.HTTPError as exc:
        raise SourceUnavailable(f"okx: network error {type(exc).__name__}") from exc
    if resp.status_code != 200:
        raise SourceUnavailable(f"okx: HTTP {resp.status_code}")
    body = resp.json()
    if body.get("code") != "0":
        raise SourceUnavailable(f"okx: {body.get('msg') or 'code ' + str(body.get('code'))}")
    if not body.get("data"):
        raise SourceUnavailable("okx: empty response")
    return body["data"]


def okx_positioning(symbol: str, http: httpx.Client) -> tuple[dict[str, Any], dict[str, str], list[str]]:
    inst = f"{symbol}-USDT-SWAP"
    out: dict[str, Any] = {"venue": "okx", "inst": inst, "premium_bps": None, "funding_rate_bps_8h": None, "interest_bps_8h": None,
                           "premium_state": None, "oi_change_24h_pct_coin": None, "long_short_ratio": None,
                           "long_short_percentile_100h": None, "long_short_hours": None}
    availability: dict[str, str] = {}
    warnings: list[str] = []
    try:
        f = _okx(http, "/public/funding-rate", {"instId": inst})[0]
        out.update(premium_bps=round(float(f["premium"]) * 1e4, 3), funding_rate_bps_8h=round(float(f["fundingRate"]) * 1e4, 3),
                   interest_bps_8h=round(float(f["interestRate"]) * 1e4, 3))
        p = out["premium_bps"]
        out["premium_state"] = "at_default" if abs(p) < AT_DEFAULT_BPS else ("above_spot" if p > 0 else "below_spot")
        availability["okx_premium"] = "available"
    except (SourceUnavailable, KeyError, ValueError) as exc:
        availability["okx_premium"] = "unavailable"
        warnings.append(f"okx premium: {exc}")
    try:
        rows = _okx(http, "/rubik/stat/contracts/open-interest-history", {"instId": inst, "period": "1H", "limit": 25})
        coins = [float(r[2]) for r in rows]  # newest first: [ts, oi, oiCcy, oiUsd]
        if len(coins) >= 25 and coins[24]:
            out["oi_change_24h_pct_coin"] = round((coins[0] / coins[24] - 1) * 100, 2)
            availability["okx_open_interest"] = "available"
        else:
            availability["okx_open_interest"] = "partial"
            warnings.append(f"okx open interest: {len(coins)} hourly points, need 25 for a 24 h change")
    except (SourceUnavailable, IndexError, ValueError) as exc:
        availability["okx_open_interest"] = "unavailable"
        warnings.append(f"okx open interest: {exc}")
    try:
        rows = _okx(http, "/rubik/stat/contracts/long-short-account-ratio-contract", {"instId": inst, "period": "1H", "limit": 100})
        ratios = [float(r[1]) for r in rows]
        now = ratios[0]
        out.update(long_short_ratio=round(now, 4), long_short_hours=len(ratios),
                   long_short_percentile_100h=round(sum(r <= now for r in ratios) / len(ratios) * 100, 1))
        availability["okx_long_short"] = "available"
    except (SourceUnavailable, IndexError, ValueError) as exc:
        availability["okx_long_short"] = "unavailable"
        warnings.append(f"okx long/short: {exc}")
    return out, availability, warnings


HL = "https://api.hyperliquid.xyz/info"
HL_NAMES = {"PEPE": "kPEPE", "SHIB": "kSHIB", "BONK": "kBONK", "FLOKI": "kFLOKI"}  # Hyperliquid lists these per 1000 tokens


def hl_premium(symbol: str, http: httpx.Client) -> float:
    """Hyperliquid's perp premium over its oracle, in bps. A second venue for the one field two venues
    can be compared on: which side of spot the perp trades."""
    try:
        resp = http.post(HL, json={"type": "metaAndAssetCtxs"})
    except httpx.HTTPError as exc:
        raise SourceUnavailable(f"hyperliquid: network error {type(exc).__name__}") from exc
    if resp.status_code != 200:
        raise SourceUnavailable(f"hyperliquid: HTTP {resp.status_code}")
    meta, ctxs = resp.json()
    names = [a["name"] for a in meta["universe"]]
    name = HL_NAMES.get(symbol, symbol)
    if name not in names:
        raise SourceUnavailable(f"hyperliquid: no perp for {symbol}")
    p = ctxs[names.index(name)].get("premium")
    if p is None:
        raise SourceUnavailable(f"hyperliquid: {name} has no premium (inactive market)")
    return round(float(p) * 1e4, 3)


def _state(bps: float | None) -> str | None:
    return None if bps is None else "at_default" if abs(bps) < AT_DEFAULT_BPS else ("above_spot" if bps > 0 else "below_spot")


def premium_consensus(states: dict[str, str | None]) -> str:
    seen = {v for v in states.values() if v}
    if not seen:
        return "unavailable"
    if len(seen) > 1:
        return "venues_disagree"
    return next(iter(seen)) + (f"_{len([v for v in states.values() if v])}_venues")


def plain_lines(symbol: str, okx: dict[str, Any], consensus: str) -> dict[str, str]:
    """One sentence a beginner can act on, in English and Japanese, built only from the numbers above."""
    oi = okx.get("oi_change_24h_pct_coin")
    en, ja = [], []
    if oi is not None:
        en.append(f"{symbol} open interest {'rose' if oi > 0 else 'fell'} {abs(oi):g}% in a day (OKX, in coins)")
        ja.append(f"{symbol}の建玉は1日で{abs(oi):g}%{'増加' if oi > 0 else '減少'}（OKX、枚数ベース）")
    side = consensus.split("_")[0] if consensus not in ("unavailable", "venues_disagree") else consensus
    en.append({"above": "and perps trade above spot on every venue checked, so the long side is the more eager one",
               "below": "and perps trade below spot on every venue checked, so the short side is the more eager one",
               "at": "and perps trade at spot, so neither side is crowding in",
               "venues_disagree": "but the venues disagree on whether perps trade above or below spot, so no side is called eager",
               "unavailable": "and no venue reported a premium"}[side])
    ja.append({"above": "、確認した全取引所で先物が現物より高く、ロング側が積極的です",
               "below": "、確認した全取引所で先物が現物より安く、ショート側が積極的です",
               "at": "、先物は現物とほぼ同じで、どちらの側にも偏りはありません",
               "venues_disagree": "、ただし先物が現物より高いか安いかで取引所の見方が分かれるため、どちらの側とも判断しません",
               "unavailable": "、プレミアムを報告した取引所はありません"}[side])
    return {"en": " ".join(en).strip() + ".", "ja": "".join(ja).strip("、") + "。"}


def _sign(x: float) -> int:
    return (x > 0) - (x < 0)


def gate(symbol: str, reference: dict[str, Any] | None, peers: list[dict[str, Any]], okx: dict[str, Any],
         ryo_btc_funding_bps: float | None) -> list[dict[str, Any]]:
    """One verdict per RYO field. Pure: every input is already in hand. `reference=None` means RYO was
    never asked, which is not the same as RYO answering null."""
    if reference is None:
        return [{"field": f, "path": f"deep_analysis.data.derivatives.{f}", "ryo_value": None, "verdict": "not_provided",
                 "why": "no reference_derivatives passed"} for f in FIELDS]
    others = [p for p in peers if str(p.get("symbol", "")).upper() != symbol]
    out = []
    for field in FIELDS:
        v = reference.get(field)
        row: dict[str, Any] = {"field": field, "path": f"deep_analysis.data.derivatives.{field}", "ryo_value": v, "verdict": "unverified", "why": ""}
        same = sorted({str(p["symbol"]).upper() for p in others if p.get(field) is not None and p.get(field) == v}) if v is not None else []
        if v is None:
            row.update(verdict="absent", why="RYO returned null")
        elif len(same) >= 2:
            row.update(verdict="not_token_specific", why=f"the same value {v:g} on {', '.join(same)} the same day", same_as=same)
        elif field == "funding_rate_bps" and symbol == "BTC" and v == 0 and ryo_btc_funding_bps:
            row.update(verdict="conflicts_with_ryo", why=f"RYO's market-wide sentiment tool reports BTC funding {ryo_btc_funding_bps:g} bps, deep_analysis says 0")
        elif field == "open_interest_change_24h_pct" and okx.get("oi_change_24h_pct_coin") is not None:
            o = okx["oi_change_24h_pct_coin"]
            if _sign(o) == _sign(v):
                row.update(verdict="citable", why=f"OKX coin-terms 24 h OI change {o:+g}% has the same sign")
            elif abs(o) >= SIGN_MIN_PCT and abs(v) >= SIGN_MIN_PCT:
                row.update(verdict="conflicts_with_venue", why=f"OKX coin-terms 24 h OI change is {o:+g}%, RYO says {v:+g}%")
            else:
                row.update(why=f"signs differ but one side is under {SIGN_MIN_PCT:g}% (OKX {o:+g}%)")
        else:
            row["why"] = "no definition-free check applies (RYO documents no unit, venue or window)"
        out.append(row)
    return out


DERIBIT = "https://www.deribit.com/api/v2/public/get_volatility_index_data"
DVOL_CURRENCIES = ("BTC", "ETH")  # the only DVOL indices Deribit publishes
NOISE_SHARE = 0.5


def deribit_dvol(symbol: str, http: httpx.Client, now_ms: int | None = None) -> dict[str, Any]:
    """Latest daily DVOL close (annualised %, 30-day implied) and the 7-day move it implies."""
    now_ms = now_ms or int(time.time() * 1000)
    try:
        resp = http.get(DERIBIT, params={"currency": symbol, "resolution": "1D",
                                         "start_timestamp": now_ms - 3 * 86_400_000, "end_timestamp": now_ms})
    except httpx.HTTPError as exc:
        raise SourceUnavailable(f"deribit: network error {type(exc).__name__}") from exc
    if resp.status_code != 200:
        raise SourceUnavailable(f"deribit: HTTP {resp.status_code}")
    rows = (resp.json().get("result") or {}).get("data") or []
    if not rows:
        raise SourceUnavailable(f"deribit: no DVOL candles for {symbol} in the last 3 days")
    last = max(rows, key=lambda r: r[0])  # [timestamp, open, high, low, close]
    dvol = float(last[4])
    return {"index": f"{symbol} DVOL", "dvol": round(dvol, 2),
            "implied_7d_move_pct": round(dvol / math.sqrt(365) * math.sqrt(7), 2),
            "as_of": datetime.fromtimestamp(last[0] / 1000, timezone.utc).isoformat(timespec="seconds")}


def premium_clause(consensus: str, okx: dict[str, Any], hl: dict[str, Any]) -> str | None:
    """The headline's premium words, from both venues: one venue's number never speaks for the market."""
    parts = " / ".join(f"{name} {v['premium_bps']:+g} bps" for name, v in (("OKX", okx), ("Hyperliquid", hl))
                       if v["premium_bps"] is not None)
    if consensus == "unavailable":
        return None
    if consensus == "venues_disagree":
        return f"{parts}: venues disagree"
    side, n, _ = consensus.rsplit("_", 2)
    if n == "1":
        parts += " only"  # the other venue did not answer; one venue is not the market
    return {"at_default": f"perp at spot ({parts}): neither side pressing",
            "above_spot": f"perp above spot ({parts}): more demand to be long",
            "below_spot": f"perp below spot ({parts}): more demand to be short"}[side]


def positioning_check(symbol: str, reference_derivatives: dict[str, Any] | None = None, peer_derivatives: list[dict[str, Any]] | None = None,
                      ryo_btc_funding_bps: float | None = None, atr_stop_pct: float | None = None,
                      http: httpx.Client | None = None, ryo: RyoSource | None = None, ledger: Ledger | None = None) -> Envelope:
    from nota.evidence import ryo_args
    from nota.scorecard import peer_derivatives as ledger_peers

    symbol = symbol.upper()
    request = {"symbol": symbol, "reference_derivatives": reference_derivatives, "peer_derivatives": peer_derivatives,
               "ryo_btc_funding_bps": ryo_btc_funding_bps, "atr_stop_pct": atr_stop_pct}
    http = http or httpx.Client(timeout=20.0, headers={"User-Agent": UA})
    okx, availability, warnings = okx_positioning(symbol, http)
    if reference_derivatives is None and ryo is not None:
        try:
            d = ryo.call("deep_analysis", ryo_args(symbol)["deep_analysis"]).get("derivatives")
            reference_derivatives = d if isinstance(d, dict) else {}
            availability["ryo_reference"] = "available" if isinstance(d, dict) else "unavailable"
            if not isinstance(d, dict):
                warnings.append(f"RYO deep_analysis {symbol} returned no derivatives block")
        except RyoError as exc:
            availability["ryo_reference"] = "unavailable"
            warnings.append(f"RYO deep_analysis failed: {exc.code}: {exc.message}")
    if peer_derivatives is None and reference_derivatives is not None and ledger is not None:
        try:
            peer_derivatives = ledger_peers(ledger, datetime.now(timezone.utc).date().isoformat())
            availability["ledger_peers"] = "available" if peer_derivatives else "partial"
            if not peer_derivatives:
                warnings.append("no scorecard locks from today in the ledger: nothing to compare RYO's values across tokens")
        except Exception as exc:  # an old snapshot without the scorecard tables
            availability["ledger_peers"] = "unavailable"
            warnings.append(f"ledger peers: {type(exc).__name__}")
    implied = None
    if symbol in DVOL_CURRENCIES:
        try:
            implied = deribit_dvol(symbol, http)
            availability["deribit_dvol"] = "available"
        except (SourceUnavailable, ValueError, TypeError, IndexError) as exc:
            availability["deribit_dvol"] = "unavailable"
            warnings.append(f"deribit dvol: {exc}")
    else:
        availability["deribit_dvol"] = "unavailable"
        warnings.append(f"no DVOL index for {symbol} (Deribit publishes BTC and ETH only)")
    stop_check = None
    if implied and atr_stop_pct is not None:
        floor = round(NOISE_SHARE * implied["implied_7d_move_pct"], 2)
        stop_check = {"atr_stop_pct": atr_stop_pct, "half_implied_7d_move_pct": floor, "inside_noise": atr_stop_pct < floor}
        if stop_check["inside_noise"]:
            warnings.append(f"stop inside normal 7-day noise: {atr_stop_pct:g}% against an implied 7-day move of "
                            f"{implied['implied_7d_move_pct']:g}% (DVOL {implied['dvol']:g})")
    hl: dict[str, Any] = {"venue": "hyperliquid", "premium_bps": None, "premium_state": None}
    try:
        hl["premium_bps"] = hl_premium(symbol, http)
        hl["premium_state"] = _state(hl["premium_bps"])
        availability["hyperliquid_premium"] = "available"
    except (SourceUnavailable, KeyError, ValueError, TypeError) as exc:
        availability["hyperliquid_premium"] = "unavailable"
        warnings.append(f"hyperliquid premium: {exc}")
    consensus = premium_consensus({"okx": okx["premium_state"], "hyperliquid": hl["premium_state"]})
    verdicts = gate(symbol, reference_derivatives, peer_derivatives or [], okx, ryo_btc_funding_bps)
    withheld = [v for v in verdicts if v["verdict"] in ("not_token_specific", "conflicts_with_venue", "conflicts_with_ryo")]
    for v in withheld:
        warnings.append(f"withheld {v['path']}: {v['verdict']} ({v['why']})")
    data = {"symbol": symbol, "gate": verdicts, "withheld_paths": [v["path"] for v in withheld], "okx": okx, "hyperliquid": hl,
            # premium is the one field two venues can be compared on; OI change and long/short come from OKX alone
            "premium_consensus": consensus, "plain": plain_lines(symbol, okx, consensus),
            "peers_compared": sorted({str(p.get("symbol", "")).upper() for p in (peer_derivatives or [])} - {symbol}),
            "implied_vol": implied, "stop_check": stop_check,
            "thresholds": {"sign_min_pct": SIGN_MIN_PCT, "premium_at_default_bps": AT_DEFAULT_BPS, "stop_noise_share": NOISE_SHARE}}
    bits = []
    if okx["oi_change_24h_pct_coin"] is not None:
        bits.append(f"OKX OI {okx['oi_change_24h_pct_coin']:+g}% in coins over 24 h")
    # A premium this small does not move who pays: OKX funding stays at the 1 bp interest component.
    # It says which side is more eager, and only when the venues agree on it.
    clause = premium_clause(consensus, okx, hl)
    if clause:
        bits.append(clause)
    if okx["long_short_percentile_100h"] is not None:
        bits.append(f"long/short account ratio {okx['long_short_ratio']:g}, percentile {okx['long_short_percentile_100h']:g} of its last {okx['long_short_hours']} h")
    if implied:
        bits.append(f"DVOL {implied['dvol']:g} implies a {implied['implied_7d_move_pct']:g}% 7-day move")
    headline = f"{symbol}: {len(withheld)} of {len(FIELDS)} RYO derivatives fields withheld" + (f"; {'; '.join(bits)}" if bits else "; no venue data")
    # the venues are what this skill measures; RYO's block, the ledger peers and DVOL are optional context
    primary = [k for k in availability if k.startswith(("okx_", "hyperliquid_"))]
    return make_envelope("positioning_check", request, data, availability, warnings, headline,
                         key_points=[f"{v['field']}: {v['verdict']}" for v in verdicts], primary=primary)
