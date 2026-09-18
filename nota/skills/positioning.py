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

It also reports OKX's own positioning. The per-8 h funding rate is not read as a signal: OKX sets it
to the interest component (1 bp) unless the perp's premium moves it, so the premium is what says
whether futures trade above spot. Open interest is in coin terms, because a USD series moves with
price alone. A long/short account ratio is only meaningful against its own history, so it is given
as a percentile of the last 100 hours.
"""

from __future__ import annotations

from typing import Any

import httpx

from nota.envelope import Envelope
from nota.skills.contract import SkillArg, SkillDefinition, SourceUnavailable, make_envelope
from nota.skills.sources import UA

DEFINITION = SkillDefinition(
    name="positioning_check",
    description="Decide, field by field, whether RYO's deep_analysis derivatives block can be cited as evidence about this token "
    "(identical values across other tokens, sign conflicts with OKX coin-terms open interest), and report OKX perp positioning: "
    "premium over spot, 24 h open-interest change in coins, long/short account ratio and its 100-hour percentile.",
    args=[
        SkillArg(name="symbol", type="string", description="Token symbol, e.g. SOL"),
        SkillArg(name="reference_derivatives", type="object", required=False,
                 description="RYO deep_analysis data.derivatives for this symbol"),
        SkillArg(name="peer_derivatives", type="array", required=False, items={"type": "object"},
                 description="Same-day RYO derivatives blocks for other symbols: [{symbol, funding_rate_bps, open_interest_change_24h_pct}]"),
        SkillArg(name="ryo_btc_funding_bps", type="number", required=False,
                 description="Latest BTC funding from RYO monitor_market_sentiment_shift (evidence.funding.latest_bps)"),
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
        availability["okx_premium"] = "ok"
    except (SourceUnavailable, KeyError, ValueError) as exc:
        availability["okx_premium"] = "unavailable"
        warnings.append(f"okx premium: {exc}")
    try:
        rows = _okx(http, "/rubik/stat/contracts/open-interest-history", {"instId": inst, "period": "1H", "limit": 25})
        coins = [float(r[2]) for r in rows]  # newest first: [ts, oi, oiCcy, oiUsd]
        if len(coins) >= 25 and coins[24]:
            out["oi_change_24h_pct_coin"] = round((coins[0] / coins[24] - 1) * 100, 2)
            availability["okx_open_interest"] = "ok"
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
        availability["okx_long_short"] = "ok"
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
    """One verdict per RYO field. Pure: every input is already in hand."""
    reference = reference or {}
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


def positioning_check(symbol: str, reference_derivatives: dict[str, Any] | None = None, peer_derivatives: list[dict[str, Any]] | None = None,
                      ryo_btc_funding_bps: float | None = None, http: httpx.Client | None = None) -> Envelope:
    symbol = symbol.upper()
    http = http or httpx.Client(timeout=20.0, headers={"User-Agent": UA})
    okx, availability, warnings = okx_positioning(symbol, http)
    hl: dict[str, Any] = {"venue": "hyperliquid", "premium_bps": None, "premium_state": None}
    try:
        hl["premium_bps"] = hl_premium(symbol, http)
        hl["premium_state"] = _state(hl["premium_bps"])
        availability["hyperliquid_premium"] = "ok"
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
            "thresholds": {"sign_min_pct": SIGN_MIN_PCT, "premium_at_default_bps": AT_DEFAULT_BPS}}
    bits = []
    if okx["oi_change_24h_pct_coin"] is not None:
        bits.append(f"OKX OI {okx['oi_change_24h_pct_coin']:+g}% in coins over 24 h")
    if okx["premium_state"]:
        # A premium this small does not move who pays: OKX funding stays at the 1 bp interest component.
        # It says which side is more eager, which is all it is reported as.
        bits.append({"at_default": "perp at spot (neither side pressing)", "above_spot": f"perp {okx['premium_bps']:+g} bps above spot (more demand to be long)",
                     "below_spot": f"perp {okx['premium_bps']:+g} bps below spot (more demand to be short)"}[okx["premium_state"]])
    if okx["long_short_percentile_100h"] is not None:
        bits.append(f"long/short account ratio {okx['long_short_ratio']:g}, percentile {okx['long_short_percentile_100h']:g} of its last {okx['long_short_hours']} h")
    headline = f"{symbol}: {len(withheld)} of {len(FIELDS)} RYO derivatives fields withheld" + (f"; {'; '.join(bits)}" if bits else "; no venue data")
    return make_envelope("positioning_check", {"symbol": symbol, "reference_derivatives": reference_derivatives,
                                               "peer_derivatives": peer_derivatives, "ryo_btc_funding_bps": ryo_btc_funding_bps},
                         data, availability, warnings, headline, key_points=[f"{v['field']}: {v['verdict']}" for v in verdicts],
                         primary=list(availability))
