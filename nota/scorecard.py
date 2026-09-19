"""RYO Verdict Scorecard: does RYO's verdict change how often RYO's own trade plan works?

Every `deep_analysis` answer carries a verdict (`verdict.call`, `confluence.state`) and a bracket
(`trade_plan`: entry, a 1.5-ATR stop, targets at +1R and +2R). RYO never reports what happened to
those plans. Once a day this module *locks* the full envelope for a fixed universe of majors, and
later *settles* each plan on OKX's public candles: which came first, the stop or the +1R target,
within 24 h and 72 h.

Settlement is independent of RYO on purpose (RYO does not grade itself), which means two price
feeds. The bracket is therefore re-anchored: its distances from RYO's entry, in percent, are applied
to OKX's price at lock time. A lock whose RYO and OKX prices differ by more than BASIS_MAX_PCT is
recorded as `basis_mismatch` and never settled, because at that gap the two feeds may not even be
describing the same asset.

Every outcome, including every failure, is a stored row, so coverage is countable: nothing is
dropped silently.
"""

from __future__ import annotations

import hashlib
import json
import random
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import httpx

from nota.ledger import Ledger, now_iso
from nota.ryo_client import RyoError, RyoSource

# RYO symbol -> OKX spot instrument. Each pair was checked by hand on 2026-09-18 against OKX's
# ticker (listed, and priced where RYO prices it). TON is absent: OKX answers 51001 for TON-USDT.
UNIVERSE: dict[str, str] = {s: f"{s}-USDT" for s in (
    "BTC", "ETH", "SOL", "XRP", "BNB", "DOGE", "ADA", "TRX", "AVAX", "LINK", "DOT", "SUI", "LTC",
    "BCH", "NEAR", "APT", "UNI", "PEPE", "SHIB", "ARB", "OP", "INJ", "ATOM", "WIF", "AAVE")}
BASIS_MAX_PCT = 2.0
HORIZONS_H = (24, 72)
PACE_S = 12.0  # RYO's builder fan-out allowance is six deep calls a minute
OKX = "https://www.okx.com/api/v5"


class OkxUnavailable(Exception):
    pass


def _okx(http: httpx.Client, path: str, params: dict[str, Any]) -> list[Any]:
    try:
        resp = http.get(f"{OKX}{path}", params=params)
    except httpx.HTTPError as exc:
        raise OkxUnavailable(f"okx: network error {type(exc).__name__}") from exc
    if resp.status_code != 200:
        raise OkxUnavailable(f"okx: HTTP {resp.status_code}")
    body = resp.json()
    if body.get("code") != "0":
        raise OkxUnavailable(f"okx: code {body.get('code')} {body.get('msg', '')}".strip())
    return body.get("data") or []


def okx_price(inst: str, http: httpx.Client) -> tuple[float, str]:
    rows = _okx(http, "/market/ticker", {"instId": inst})
    if not rows:
        raise OkxUnavailable(f"okx: no ticker for {inst}")
    ts = datetime.fromtimestamp(int(rows[0]["ts"]) / 1000, timezone.utc).isoformat(timespec="seconds")
    return float(rows[0]["last"]), ts


def okx_hourly(inst: str, start_ms: int, end_ms: int, http: httpx.Client) -> list[dict[str, float]]:
    """Closed 1 h candles whose open time is in [start_ms, end_ms), oldest first."""
    rows = _okx(http, "/market/history-candles",
                {"instId": inst, "bar": "1H", "before": start_ms - 1, "after": end_ms, "limit": 100})
    out = [{"ts": int(r[0]), "open": float(r[1]), "high": float(r[2]), "low": float(r[3]), "close": float(r[4])}
           for r in rows if r[8] == "1"]
    return sorted(out, key=lambda c: c["ts"])


# -- lock --------------------------------------------------------------------------------------

def _row_id(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:12]


def lock_one(symbol: str, source: RyoSource, http: httpx.Client) -> dict[str, Any]:
    inst = UNIVERSE.get(symbol, f"{symbol}-USDT")
    row: dict[str, Any] = {"symbol": symbol, "inst": inst, "locked_at": now_iso(), "status": "locked", "error": None,
                           "ryo_as_of": None, "trace_id": None, "ryo_price": None, "okx_price": None, "okx_as_of": None,
                           "basis_pct": None, "verdict": None, "confluence_state": None, "confluence_score": None,
                           "plan": None, "envelope": None}
    try:
        env = source.call("deep_analysis", {"symbol": symbol, "include_perp": True})
    except RyoError as exc:
        row.update(status="ryo_unavailable", error=f"{exc.code}: {exc.message}")
        row["id"] = _row_id(symbol, row["locked_at"], row["status"])
        return row
    row["envelope"] = env.model_dump(mode="json")
    row.update(ryo_as_of=env.as_of, trace_id=env.trace_id, ryo_price=env.get("market.price_usd"),
               verdict=env.get("verdict.call"), confluence_state=env.get("confluence.state"),
               confluence_score=env.get("confluence.score"), plan=env.get("trade_plan"))
    # identity of a lock = what RYO said, not when we asked; the trace id changes on every call
    row["id"] = _row_id(symbol, json.dumps(env.model_dump(mode="json", exclude={"trace_id"}), sort_keys=True))
    plan = row["plan"] or {}
    if not (plan.get("entry") and plan.get("stop") and plan.get("targets")):
        row["status"] = "no_plan"
        return row
    try:
        row["okx_price"], row["okx_as_of"] = okx_price(inst, http)
    except OkxUnavailable as exc:
        row.update(status="okx_unavailable", error=str(exc))
        return row
    if row["ryo_price"]:
        row["basis_pct"] = round((row["okx_price"] / row["ryo_price"] - 1) * 100, 3)
        if abs(row["basis_pct"]) > BASIS_MAX_PCT:
            row["status"] = "basis_mismatch"
    return row


def lock_all(source: RyoSource, ledger: Ledger, symbols: list[str] | None = None, http: httpx.Client | None = None,
             sleep: Callable[[float], None] = time.sleep, pace_s: float = PACE_S) -> list[dict[str, Any]]:
    """One lock per symbol per UTC day: a re-run the same day skips what it already has, or the contrast
    would count that day's plans twice. A failed lock does not count as had, so a re-run retries it."""
    http = http or httpx.Client(timeout=20.0)
    today = now_iso()[:10]
    have = {json.loads(j)["symbol"] for _, j in ledger.list_locks() if json.loads(j)["locked_at"][:10] == today
            and json.loads(j)["status"] in ("locked", "basis_mismatch", "no_plan")}
    rows = []
    todo = [s.upper() for s in (symbols or list(UNIVERSE)) if s.upper() not in have]
    for i, s in enumerate(todo):
        if i:
            sleep(pace_s)
        row = lock_one(s, source, http)
        ledger.save_lock(row["id"], row["symbol"], row["locked_at"], row["status"], json.dumps(row))
        rows.append(row)
    return rows


# -- settle ------------------------------------------------------------------------------------

def bracket(row: dict[str, Any]) -> dict[str, Any]:
    """RYO's stop and first target as percent distances from RYO's entry, re-applied to OKX's price."""
    plan = row["plan"]
    entry, stop, target = float(plan["entry"]), float(plan["stop"]), float(plan["targets"][0])
    side = "long" if stop < entry else "short"
    return {"side": side, "entry": row["okx_price"],
            "stop": row["okx_price"] * stop / entry, "target": row["okx_price"] * target / entry}


def first_touch(b: dict[str, Any], candles: list[dict[str, float]]) -> tuple[str, int | None]:
    """'target' | 'stop' | 'ambiguous' (both inside one hourly candle) | 'neither'; plus the candle index."""
    for i, c in enumerate(candles):
        if b["side"] == "long":
            hit_t, hit_s = c["high"] >= b["target"], c["low"] <= b["stop"]
        else:
            hit_t, hit_s = c["low"] <= b["target"], c["high"] >= b["stop"]
        if hit_t and hit_s:
            # ponytail: an hourly candle cannot order the two; a 1 m tie-break would, add it if ambiguous
            # rows ever become more than a handful
            return "ambiguous", i
        if hit_t:
            return "target", i
        if hit_s:
            return "stop", i
    return "neither", None


def settle_one(row: dict[str, Any], horizon_h: int, http: httpx.Client, now: datetime | None = None) -> dict[str, Any] | None:
    """None while the horizon has not passed yet. Candles start at the first full hour after the lock,
    so up to 59 minutes after the lock are not observed (said here rather than guessed at)."""
    now = now or datetime.now(timezone.utc)
    locked = datetime.fromisoformat(row["locked_at"])
    start = (locked + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
    end = start + timedelta(hours=horizon_h)
    if now < end + timedelta(minutes=5):
        return None
    b = bracket(row)
    try:
        candles = okx_hourly(row["inst"], int(start.timestamp() * 1000), int(end.timestamp() * 1000), http)
    except OkxUnavailable as exc:
        return {"status": "okx_unavailable", "error": str(exc), "horizon_h": horizon_h}
    if len(candles) < horizon_h:
        return {"status": "incomplete_candles", "candles": len(candles), "horizon_h": horizon_h}
    result, idx = first_touch(b, candles)
    last = candles[-1]["close"]
    ret = (last / b["entry"] - 1) * 100 * (1 if b["side"] == "long" else -1)
    return {"status": "settled", "result": result, "hours_to_touch": None if idx is None else idx + 1,
            "horizon_h": horizon_h, "bracket": {k: round(v, 10) if isinstance(v, float) else v for k, v in b.items()},
            "close_at_horizon": last, "return_at_horizon_pct": round(ret, 3), "candles": len(candles),
            "window": [start.isoformat(), end.isoformat()]}


def settle_all(ledger: Ledger, http: httpx.Client | None = None, now: datetime | None = None) -> list[dict[str, Any]]:
    http = http or httpx.Client(timeout=20.0)
    out = []
    for lock_id, row_json in ledger.unsettled_locks():
        row = json.loads(row_json)
        for h in HORIZONS_H:
            if ledger.get_settlement(lock_id, h) is not None:
                continue
            res = settle_one(row, h, http, now)
            if res is None:
                continue
            if res["status"] == "settled":  # transient failures are retried on the next cycle
                ledger.save_settlement(lock_id, h, json.dumps(res))
            out.append({"lock_id": lock_id, "symbol": row["symbol"], **res})
    return out


def peer_derivatives(ledger: Ledger, day: str) -> list[dict[str, Any]]:
    """RYO's derivatives blocks from the day's locks: the cross-section `positioning_check` needs to
    tell a token-specific number from one that is the same for every token."""
    out = []
    for _, j in ledger.list_locks():
        row = json.loads(j)
        d = (row.get("envelope") or {}).get("data", {}).get("derivatives")
        if row["locked_at"][:10] == day and isinstance(d, dict):
            out.append({"symbol": row["symbol"], **{k: d.get(k) for k in ("funding_rate_bps", "open_interest_change_24h_pct")}})
    return out


# -- read ---------------------------------------------------------------------------------------

BOOTSTRAP_N = 2000


def _rate(rows: list[dict[str, Any]]) -> tuple[int, int]:
    decided = [r for r in rows if r["result"] in ("target", "stop")]
    return sum(r["result"] == "target" for r in decided), len(decided)


def contrast(settled: list[dict[str, Any]], key: str, a: str, b: str, seed: int = 7) -> dict[str, Any]:
    """Target-first rate of group `a` minus group `b`, comparing only days on which both groups had a
    decided plan (so the market's mood that day cancels out). The interval is a bootstrap over lock
    days, not over plans: plans on one day move together and are not independent samples."""
    days: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for r in settled:
        if r.get(key) in (a, b) and r["result"] in ("target", "stop"):
            days.setdefault(r["day"], {a: [], b: []})[r[key]].append(r)
    both = sorted(d for d, g in days.items() if g[a] and g[b])

    def diff(sample: list[str]) -> float | None:
        ta = sum(_rate(days[d][a])[0] for d in sample); na = sum(_rate(days[d][a])[1] for d in sample)
        tb = sum(_rate(days[d][b])[0] for d in sample); nb = sum(_rate(days[d][b])[1] for d in sample)
        return None if not na or not nb else ta / na - tb / nb

    point = diff(both)
    ci = None
    if len(both) >= 3:
        rng = random.Random(seed)  # fixed seed: the page shows the same interval every time it is read
        draws = sorted(x for x in (diff([rng.choice(both) for _ in both]) for _ in range(BOOTSTRAP_N)) if x is not None)
        ci = [round(draws[int(0.05 * len(draws))] * 100, 1), round(draws[int(0.95 * len(draws)) - 1] * 100, 1)]
    ta, na = _rate([r for d in both for r in days[d][a]])
    tb, nb = _rate([r for d in both for r in days[d][b]])
    return {"key": key, "a": a, "b": b, "days": len(both), "a_rate": [ta, na], "b_rate": [tb, nb],
            "diff_pct": None if point is None else round(point * 100, 1), "ci90_pct": ci,
            "distinguishable": bool(ci and (ci[0] > 0 or ci[1] < 0))}


BEARISH = {"cautious", "bearish", "avoid", "negative"}
BULLISH = {"constructive", "bullish", "positive", "accumulate"}


def verdict_contradicts_side(side: str, verdict: str | None) -> bool:
    """A long bracket under a bearish verdict, or a short one under a bullish verdict: RYO's words and
    RYO's plan point opposite ways."""
    v = (verdict or "").lower()
    return (side == "long" and v in BEARISH) or (side == "short" and v in BULLISH)


def summary(ledger: Ledger, horizon_h: int = 24) -> dict[str, Any]:
    locks = [json.loads(j) for _, j in ledger.list_locks()]
    coverage: dict[str, int] = {}
    for r in locks:
        coverage[r["status"]] = coverage.get(r["status"], 0) + 1
    settled = []
    open_plans = []
    for r in locks:
        if r["status"] != "locked":
            continue
        s = ledger.get_settlement(r["id"], horizon_h)
        side = bracket(r)["side"]
        start = (datetime.fromisoformat(r["locked_at"]) + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
        base = {"id": r["id"], "symbol": r["symbol"], "day": r["locked_at"][:10], "locked_at": r["locked_at"],
                "settles_at": (start + timedelta(hours=horizon_h)).isoformat(), "trace_id": r.get("trace_id"),
                "confluence_score": r.get("confluence_score"), "atr_14_pct": (r.get("plan") or {}).get("atr_14_pct"),
                "verdict": r["verdict"], "confluence_state": r["confluence_state"], "side": side,
                "verdict_contradicts_side": verdict_contradicts_side(side, r["verdict"])}
        if s is None:
            open_plans.append(base)
        else:
            s = json.loads(s)
            coverage[f"settled_{s['result']}"] = coverage.get(f"settled_{s['result']}", 0) + 1
            settled.append({**base, **{k: s[k] for k in ("result", "hours_to_touch", "return_at_horizon_pct")}})
    states = sorted({r["confluence_state"] for r in settled if r["confluence_state"]})
    contrasts = [contrast(settled, "confluence_state", a, b) for i, a in enumerate(states) for b in states[i + 1:]]
    t, n = _rate(settled)
    return {"horizon_h": horizon_h, "universe": list(UNIVERSE), "lock_days": len({r["locked_at"][:10] for r in locks}),
            "coverage": coverage, "target_first": [t, n], "contrasts": contrasts,
            "open": sorted(open_plans, key=lambda r: r["settles_at"]), "settled": sorted(settled, key=lambda r: r["locked_at"], reverse=True),
            "contradictions": sum(r["verdict_contradicts_side"] for r in open_plans + settled)}
