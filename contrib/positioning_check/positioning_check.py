"""`positioning_check` for RYO's skill registry. Standalone: stdlib + httpx, nothing else.

Two jobs:
1. Gate RYO's own `deep_analysis.data.derivatives` block field by field, using only tests that need no
   definition of RYO's units (RYO documents no unit, venue or window for these fields):
   `not_token_specific` (same value on 2+ other symbols the same day), `conflicts_with_venue` (RYO's and
   OKX's coin-terms 24 h OI change have opposite signs, both >= 3%), `conflicts_with_ryo` (BTC funding
   exactly 0 while RYO's sentiment tool reports a value), `citable`, `unverified`, `absent` (null),
   `not_provided` (no block passed).
2. Report venue positioning: OKX premium over spot, 24 h OI change in coins, long/short account ratio
   and its 100-hour percentile; Hyperliquid premium as a second venue; Deribit DVOL implied 7-day move
   for BTC/ETH as a check on stop distance.

A source that fails is `unavailable` plus a warning naming the cause. A value that was not measured is
None (JSON null), never 0. `status` is derived from the four venue sections, never set by hand.
Same logic as Nota's nota/skills/positioning.py, minus Nota's ledger and RYO client: RYO's own derivatives
block, its peers and BTC funding come in as optional arguments (or the `fetch_reference` hook).
"""

from __future__ import annotations

import math
import re
import time
from datetime import datetime, timezone
from typing import Any, Callable

import httpx

NAME = "positioning_check"
SCHEMA_VERSION = "nota-skill-1"
UA = "positioning_check/1.0 (+https://ryobuild.com)"

# RYO's SkillDefinition / SkillArgSchema (docs/ryo-openapi-subset.json): name, description, args[], requires_guard, xp
SKILL_DEFINITION: dict[str, Any] = {
    "name": NAME,
    "description": "Decide, field by field, whether RYO's deep_analysis derivatives block can be cited as evidence about this token "
    "(identical values across other tokens, sign conflicts with OKX coin-terms open interest), and report OKX perp positioning: "
    "premium over spot on OKX and Hyperliquid, 24 h open-interest change in coins, long/short account ratio and its 100-hour "
    "percentile, and for BTC/ETH Deribit's DVOL implied 7-day move as a check on stop distance.",
    "args": [
        {"name": "symbol", "type": "string", "required": True, "description": "Token symbol, e.g. SOL", "enum": None, "items": None},
        {"name": "reference_derivatives", "type": "object", "required": False, "enum": None, "items": None,
         "description": "RYO deep_analysis data.derivatives for this symbol: {funding_rate_bps, open_interest_change_24h_pct, long_short_ratio}"},
        {"name": "peer_derivatives", "type": "array", "required": False, "enum": None, "items": {"type": "object"},
         "description": "Same-day RYO derivatives blocks for other symbols: [{symbol, funding_rate_bps, open_interest_change_24h_pct}], at most 25"},
        {"name": "ryo_btc_funding_bps", "type": "number", "required": False, "enum": None, "items": None,
         "description": "Latest BTC funding from RYO monitor_market_sentiment_shift (evidence.funding.latest_bps)"},
        {"name": "atr_stop_pct", "type": "number", "required": False, "enum": None, "items": None,
         "description": "Stop distance in % of price (e.g. RYO's 1.5 x atr_14_pct), checked against the DVOL implied 7-day move"},
    ],
    "requires_guard": False,  # read-only research: never touches a wallet or an order
    "xp": 0,
}

OKX = "https://www.okx.com/api/v5"
HL = "https://api.hyperliquid.xyz/info"
HL_NAMES = {"PEPE": "kPEPE", "SHIB": "kSHIB", "BONK": "kBONK", "FLOKI": "kFLOKI"}  # Hyperliquid lists these per 1000 tokens
DERIBIT = "https://www.deribit.com/api/v2/public/get_volatility_index_data"
DVOL_CURRENCIES = ("BTC", "ETH")  # the only DVOL indices Deribit publishes
SIGN_MIN_PCT = 3.0
AT_DEFAULT_BPS = 1.0  # |premium| under 1 bp: perp and spot agree, neither side is pressing
NOISE_SHARE = 0.5
FIELDS = ("funding_rate_bps", "open_interest_change_24h_pct", "long_short_ratio")
PRIMARY = ("okx_premium", "okx_open_interest", "okx_long_short", "hyperliquid_premium")
SYMBOL_RE = re.compile(r"[A-Z0-9]{1,15}")  # the symbol ends up in exchange URLs, so it is checked here
MAX_PEERS = 25


class SourceUnavailable(Exception):
    """A network source failed. Reported, never papered over."""


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


def _state(bps: float | None) -> str | None:
    return None if bps is None else "at_default" if abs(bps) < AT_DEFAULT_BPS else ("above_spot" if bps > 0 else "below_spot")


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
        out["premium_state"] = _state(out["premium_bps"])
        availability["okx_premium"] = "available"
    except (SourceUnavailable, KeyError, ValueError, TypeError) as exc:
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
    except (SourceUnavailable, IndexError, ValueError, TypeError) as exc:
        availability["okx_open_interest"] = "unavailable"
        warnings.append(f"okx open interest: {exc}")
    try:
        rows = _okx(http, "/rubik/stat/contracts/long-short-account-ratio-contract", {"instId": inst, "period": "1H", "limit": 100})
        ratios = [float(r[1]) for r in rows]
        now = ratios[0]
        out.update(long_short_ratio=round(now, 4), long_short_hours=len(ratios),
                   long_short_percentile_100h=round(sum(r <= now for r in ratios) / len(ratios) * 100, 1))
        availability["okx_long_short"] = "available"
    except (SourceUnavailable, IndexError, ValueError, TypeError) as exc:
        availability["okx_long_short"] = "unavailable"
        warnings.append(f"okx long/short: {exc}")
    return out, availability, warnings


def hl_premium(symbol: str, http: httpx.Client) -> float:
    """Hyperliquid's perp premium over its oracle, in bps: a second venue for which side of spot the perp trades."""
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


def deribit_dvol(symbol: str, http: httpx.Client) -> dict[str, Any]:
    """Latest daily DVOL close (annualised %, 30-day implied) and the 7-day move it implies."""
    now_ms = int(time.time() * 1000)
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


def premium_consensus(states: dict[str, str | None]) -> str:
    seen = [v for v in states.values() if v]
    if not seen:
        return "unavailable"
    if len(set(seen)) > 1:
        return "venues_disagree"
    return f"{seen[0]}_{len(seen)}_venues"


def premium_clause(consensus: str, okx: dict[str, Any], hl: dict[str, Any]) -> str | None:
    parts = " / ".join(f"{name} {v['premium_bps']:+g} bps" for name, v in (("OKX", okx), ("Hyperliquid", hl)) if v["premium_bps"] is not None)
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
    s = " ".join(en).strip()
    if oi is None:  # no OI clause to join onto: "and perps trade ..." -> "Perps trade ..."
        s = s.removeprefix("and ").removeprefix("but ")
    return {"en": s[0].upper() + s[1:] + ".", "ja": "".join(ja).strip("、") + "。"}


def _sign(x: float) -> int:
    return (x > 0) - (x < 0)


def gate(symbol: str, reference: dict[str, Any] | None, peers: list[dict[str, Any]], okx: dict[str, Any],
         ryo_btc_funding_bps: float | None) -> list[dict[str, Any]]:
    """One verdict per RYO field. `reference=None` means RYO was never asked, which is not RYO answering null."""
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


def status_from(availability: dict[str, str]) -> str:
    vals = [availability.get(k, "unavailable") for k in PRIMARY]
    if all(v == "available" for v in vals):
        return "ok"
    if all(v in ("unavailable", "error") for v in vals):
        return "unavailable"
    return "partial"


def positioning_check(symbol: str, reference_derivatives: dict[str, Any] | None = None, peer_derivatives: list[dict[str, Any]] | None = None,
                      ryo_btc_funding_bps: float | None = None, atr_stop_pct: float | None = None, http: httpx.Client | None = None,
                      fetch_reference: Callable[[str], dict[str, Any] | None] | None = None) -> dict[str, Any]:
    """Returns RYO's public envelope. `fetch_reference(symbol)` is RYO's hook to supply its own deep_analysis
    derivatives block when the caller passed none; without it the gate reports `not_provided`."""
    symbol = symbol.upper()
    request = {"symbol": symbol, "reference_derivatives": reference_derivatives, "peer_derivatives": peer_derivatives,
               "ryo_btc_funding_bps": ryo_btc_funding_bps, "atr_stop_pct": atr_stop_pct}
    http = http or httpx.Client(timeout=20.0, headers={"User-Agent": UA})
    okx, availability, warnings = okx_positioning(symbol, http)
    if reference_derivatives is None and fetch_reference is not None:
        try:
            d = fetch_reference(symbol)
        except Exception as exc:  # RYO's own tool failing is a missing context section, not a crash
            d = None
            warnings.append(f"RYO deep_analysis failed: {type(exc).__name__}")
        availability["ryo_reference"] = "available" if isinstance(d, dict) else "unavailable"
        if isinstance(d, dict):
            reference_derivatives = d
        else:
            warnings.append(f"RYO deep_analysis {symbol} returned no derivatives block")
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
    warnings += [f"withheld {v['path']}: {v['verdict']} ({v['why']})" for v in withheld]
    data = {"symbol": symbol, "gate": verdicts, "withheld_paths": [v["path"] for v in withheld], "okx": okx, "hyperliquid": hl,
            "premium_consensus": consensus, "plain": plain_lines(symbol, okx, consensus),
            "peers_compared": sorted({str(p.get("symbol", "")).upper() for p in (peer_derivatives or [])} - {symbol}),
            "implied_vol": implied, "stop_check": stop_check,
            "thresholds": {"sign_min_pct": SIGN_MIN_PCT, "premium_at_default_bps": AT_DEFAULT_BPS, "stop_noise_share": NOISE_SHARE}}
    bits = []
    if okx["oi_change_24h_pct_coin"] is not None:
        bits.append(f"OKX OI {okx['oi_change_24h_pct_coin']:+g}% in coins over 24 h")
    clause = premium_clause(consensus, okx, hl)
    if clause:
        bits.append(clause)
    if okx["long_short_percentile_100h"] is not None:
        bits.append(f"long/short account ratio {okx['long_short_ratio']:g}, percentile {okx['long_short_percentile_100h']:g} of its last {okx['long_short_hours']} h")
    if implied:
        bits.append(f"DVOL {implied['dvol']:g} implies a {implied['implied_7d_move_pct']:g}% 7-day move")
    headline = f"{symbol}: {len(withheld)} of {len(FIELDS)} RYO derivatives fields withheld" + (f"; {'; '.join(bits)}" if bits else "; no venue data")
    status = status_from(availability)
    return {"schema_version": SCHEMA_VERSION, "tool": NAME, "status": status,
            "data_mode": "unknown" if status == "unavailable" else "live",  # nothing observed is not "live"
            "as_of": datetime.now(timezone.utc).isoformat(timespec="seconds"), "request": request, "data": data,
            "summary": {"headline": headline, "key_points": [f"{v['field']}: {v['verdict']}" for v in verdicts]},
            "availability": availability, "warnings": warnings}


def _finite(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def check_args(args: Any) -> dict[str, Any]:
    """The declared schema, enforced before anything reaches a URL. Raises ValueError (map it to 422)."""
    if not isinstance(args, dict):
        raise ValueError(f"{NAME}: arguments must be an object")
    unknown = set(args) - {a["name"] for a in SKILL_DEFINITION["args"]}
    if unknown:
        raise ValueError(f"{NAME}: unknown args {sorted(unknown)}")
    args = {k: v for k, v in args.items() if v is not None}  # null on an optional arg means "use the default"
    sym = args.get("symbol")
    if not isinstance(sym, str) or not SYMBOL_RE.fullmatch(sym.strip().upper()):
        raise ValueError(f"{NAME}: symbol must be 1-15 letters/digits, got {sym!r}")
    args["symbol"] = sym.strip().upper()
    if "reference_derivatives" in args and not isinstance(args["reference_derivatives"], dict):
        raise ValueError(f"{NAME}: arg reference_derivatives must be object")
    peers = args.get("peer_derivatives")
    if peers is not None and (not isinstance(peers, list) or len(peers) > MAX_PEERS or not all(isinstance(p, dict) for p in peers)):
        raise ValueError(f"{NAME}: arg peer_derivatives must be array of at most {MAX_PEERS} objects")
    for k in ("ryo_btc_funding_bps", "atr_stop_pct"):
        if k in args and not _finite(args[k]):
            raise ValueError(f"{NAME}: arg {k} must be number")
    return args


def invoke(args: dict[str, Any], http: httpx.Client | None = None,
           fetch_reference: Callable[[str], dict[str, Any] | None] | None = None) -> dict[str, Any]:
    """RYO's SkillCallResponse: {name, status: success|error, result: <envelope>, latency_ms, xp, guard_decision}.
    Bad args raise ValueError; a source failure is never raised, it is reported inside `result`."""
    started = time.monotonic()
    env = positioning_check(**check_args(args), http=http, fetch_reference=fetch_reference)
    # SkillCastStatus: pending | running | success | error. Only "every venue failed" is an error.
    return {"name": NAME, "status": "error" if env["status"] == "unavailable" else "success", "result": env,
            "latency_ms": int((time.monotonic() - started) * 1000), "xp": SKILL_DEFINITION["xp"], "guard_decision": None}


if __name__ == "__main__":
    import json
    import sys

    print(json.dumps(invoke(json.loads(sys.argv[1]) if len(sys.argv) > 1 else {"symbol": "BTC"}), indent=2))
