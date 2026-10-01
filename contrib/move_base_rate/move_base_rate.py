"""`move_base_rate` for RYO's skill registry. Standalone: stdlib + httpx, nothing else.

RYO gives ATR(14) and a verdict but no probability. Before acting on "SOL cautious, ATR 4.1%" a
reader wants the base rate: in this token's own recent history, on days with similar volatility,
how often did price reach `k` ATRs away within `h` days? Counted, not modelled, from about 400 OKX
daily candles (UTC days):

- `touch`: the high (up) or low (down) reached `k` x ATR from that day's close within `h` days.
- `close`: the close `h` days later was beyond `k` x ATR. With `k = 0` this is simply "higher after h
  days", the reference a forecaster of `p_up` has to beat.

Distances follow RYO's ATR when it is given: RYO's `atr_14_pct` runs a few percent under a Wilder ATR
from OKX's UTC candles, so `k` is converted into OKX-ATR units with today's ratio of the two, and that
ratio is printed. Only days in today's volatility tercile count. A holdout splits the history in time
(older three quarters against the newest quarter); the tercile cuts come from the older part only and
an older day counts only when its window closed before the split, so the fit never sees the holdout.

A source that fails is `unavailable` plus a warning naming the cause; `p` is then None (JSON null),
never 0. Same logic as Nota's nota/skills/base_rate.py, minus Nota's envelope models.
"""

from __future__ import annotations

import math
import re
import time
from datetime import datetime, timezone
from typing import Any

import httpx

NAME = "move_base_rate"
SCHEMA_VERSION = "nota-skill-1"
UA = "move_base_rate/1.0 (+https://ryobuild.com)"

# RYO's SkillDefinition / SkillArgSchema (docs/ryo-openapi-subset.json): name, description, args[], requires_guard, xp
SKILL_DEFINITION: dict[str, Any] = {
    "name": NAME,
    "description": "Empirical probability that a token moves k ATRs (up or down, touched or closed beyond) within h days, counted "
    "from ~400 days of OKX daily candles on days in the same ATR tercile as today, with a time-split holdout. Pass RYO's "
    "atr_14_pct to measure the distance in RYO's ATR; k=0 with event=close gives the plain 'higher after h days' rate.",
    "args": [
        {"name": "symbol", "type": "string", "required": True, "description": "Token symbol, e.g. SOL", "enum": None, "items": None},
        {"name": "k", "type": "number", "required": False, "description": "Distance in ATRs, 0 to 10 (default 1)", "enum": None, "items": None},
        {"name": "horizon_days", "type": "integer", "required": False, "description": "1 to 14 (default 3)", "enum": None, "items": None},
        {"name": "direction", "type": "string", "required": False, "description": "Default up", "enum": ["up", "down"], "items": None},
        {"name": "event", "type": "string", "required": False, "description": "Default touch", "enum": ["touch", "close"], "items": None},
        {"name": "atr_14_pct", "type": "number", "required": False, "enum": None, "items": None,
         "description": "RYO's ATR(14) as % of price (analyze_token or deep_analysis)"},
        {"name": "as_of", "type": "string", "required": False, "enum": None, "items": None,
         "description": "YYYY-MM-DD: use only candles closed before this day (scoring a past call)"},
    ],
    "requires_guard": False,  # read-only research: never touches a wallet or an order
    "xp": 0,
}

OKX = "https://www.okx.com/api/v5/market/history-candles"
PAGES = 4  # 100 daily candles each
MIN_DAYS = 60
PERIOD = 14
DRIFT_WARN = 0.10
MAX_K = 10.0
SYMBOL_RE = re.compile(r"[A-Z0-9]{1,15}")  # the symbol ends up in an exchange URL, so it is checked here
DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


class SourceUnavailable(Exception):
    """A network source failed. Reported, never papered over."""


def okx_daily(symbol: str, http: httpx.Client, pages: int = PAGES) -> list[dict[str, float]]:
    """Closed UTC-day candles, oldest first."""
    rows: list[list[str]] = []
    after = None
    for _ in range(pages):
        params = {"instId": f"{symbol}-USDT", "bar": "1Dutc", "limit": 100, **({"after": after} if after else {})}
        try:
            resp = http.get(OKX, params=params)
        except httpx.HTTPError as exc:
            raise SourceUnavailable(f"okx: network error {type(exc).__name__}") from exc
        try:
            body = resp.json() if resp.status_code == 200 else {}
        except ValueError:
            body = {}
        if not isinstance(body, dict) or body.get("code") != "0":
            msg = body.get("msg") if isinstance(body, dict) else None
            raise SourceUnavailable(f"okx: {msg or 'HTTP ' + str(resp.status_code)}")
        page = body.get("data") or []
        if not page:
            break
        rows += page
        after = page[-1][0]
    if not rows:
        raise SourceUnavailable(f"okx: no daily candles for {symbol}-USDT")
    try:
        out = {int(r[0]): {"ts": int(r[0]), "open": float(r[1]), "high": float(r[2]), "low": float(r[3]), "close": float(r[4])}
               for r in rows if r[8] == "1"}
    except (IndexError, TypeError, ValueError) as exc:
        raise SourceUnavailable(f"okx: malformed candle ({type(exc).__name__})") from exc
    return [out[k] for k in sorted(out)]


def wilder_atr_pct(daily: list[dict[str, float]]) -> list[float | None]:
    """ATR(14)/close in percent for every day (None until 15 candles exist)."""
    out: list[float | None] = [None] * len(daily)
    value = None
    trs = []
    for i in range(1, len(daily)):
        p, c = daily[i - 1], daily[i]
        tr = max(c["high"] - c["low"], abs(c["high"] - p["close"]), abs(c["low"] - p["close"]))
        trs.append(tr)
        if len(trs) == PERIOD:
            value = sum(trs) / PERIOD
        elif len(trs) > PERIOD:
            value = (value * (PERIOD - 1) + tr) / PERIOD
        if value is not None:
            out[i] = value / c["close"] * 100
    return out


def hit(daily: list[dict[str, float]], i: int, dist_pct: float, h: int, direction: str, event: str) -> bool:
    base = daily[i]["close"]
    level = base * (1 + dist_pct / 100) if direction == "up" else base * (1 - dist_pct / 100)
    window = daily[i + 1: i + 1 + h]
    if event == "close":
        last = window[-1]["close"]
        return last > level if direction == "up" else last < level
    return any(c["high"] >= level for c in window) if direction == "up" else any(c["low"] <= level for c in window)


def _day(ts: float) -> str:
    return datetime.fromtimestamp(ts / 1000, timezone.utc).date().isoformat()


def base_rate(daily: list[dict[str, float]], k: float, h: int, direction: str, event: str, k_scale: float = 1.0) -> dict[str, Any]:
    """Pure: everything the envelope reports, from candles alone."""
    atrs = wilder_atr_pct(daily)
    days = [i for i in range(len(daily) - h) if atrs[i] is not None]
    if len(days) < MIN_DAYS or atrs[-1] is None:
        return {"p": None, "reason": f"{len(days)} usable days, need {MIN_DAYS}"}
    split = days[int(len(days) * 0.75)]
    ranked = sorted(atrs[i] for i in days if i + h < split)  # tercile cuts from fit days only
    cut = [ranked[len(ranked) // 3], ranked[2 * len(ranked) // 3]]

    def tercile(a: float) -> int:
        return 0 if a < cut[0] else (1 if a < cut[1] else 2)

    today = tercile(atrs[-1])
    same = [i for i in days if tercile(atrs[i]) == today]
    ke = k * k_scale

    def rate(ix: list[int]) -> tuple[int, int]:
        return sum(hit(daily, i, ke * atrs[i], h, direction, event) for i in ix), len(ix)

    hits, n = rate(same)
    old, new = [i for i in same if i + h < split], [i for i in same if i >= split]
    (ho, no), (hn, nn) = rate(old), rate(new)
    return {"p": round(hits / n, 4) if n else None, "hits": hits, "n_days": n, "n_independent": n // max(h, 1),
            "tercile": ["low", "mid", "high"][today], "tercile_cuts_atr_pct": [round(x, 3) for x in cut],
            "atr_14_pct_okx_today": round(atrs[-1], 3), "k_in_okx_atr": round(ke, 4),
            "holdout": {"fit_p": round(ho / no, 4) if no else None, "fit_days": no,
                        "recent_p": round(hn / nn, 4) if nn else None, "recent_days": nn, "recent_from": _day(daily[split]["ts"])},
            "history": [_day(daily[0]["ts"]), _day(daily[-1]["ts"])]}


def move_base_rate(symbol: str, k: float = 1.0, horizon_days: int = 3, direction: str = "up", event: str = "touch",
                   atr_14_pct: float | None = None, as_of: str | None = None, http: httpx.Client | None = None) -> dict[str, Any]:
    """The envelope (RYO's public response shape). Arguments are assumed checked (see `check_args`)."""
    h = max(1, min(int(horizon_days), 14))
    request = {"symbol": symbol, "k": k, "horizon_days": h, "direction": direction, "event": event, "atr_14_pct": atr_14_pct, "as_of": as_of}
    warnings: list[str] = []
    data: dict[str, Any] = {"symbol": symbol, "p": None, "event": event, "direction": direction, "k": k, "horizon_days": h,
                            "method": "okx_1Dutc_wilder_atr14_same_tercile_count", "atr_scale": None}
    try:
        daily = okx_daily(symbol, http or httpx.Client(timeout=20.0, headers={"User-Agent": UA}))
        if as_of:
            cutoff = datetime.fromisoformat(as_of).replace(tzinfo=timezone.utc).timestamp() * 1000
            daily = [d for d in daily if d["ts"] < cutoff]
        availability = {"okx_daily": "available"}
    except SourceUnavailable as exc:
        daily, availability = [], {"okx_daily": "unavailable"}
        warnings.append(str(exc))
    if daily:
        okx_now = wilder_atr_pct(daily)[-1]
        scale = 1.0
        if atr_14_pct and okx_now:
            scale = atr_14_pct / okx_now
            data["atr_scale"] = {"ryo_atr_14_pct": atr_14_pct, "okx_atr_14_pct": round(okx_now, 3), "ryo_over_okx": round(scale, 4)}
            if not 0.5 <= scale <= 2.0:
                warnings.append(f"RYO ATR is {scale:.2f}x OKX's: the two may not measure the same asset or window")
        data.update(base_rate(daily, k, h, direction, event, scale))
        if data.get("p") is None:
            availability["okx_daily"] = "partial"
            warnings.append(data.pop("reason", "not enough history"))
        else:
            hd = data["holdout"]
            if hd["fit_p"] is not None and hd["recent_p"] is not None and abs(hd["fit_p"] - hd["recent_p"]) > DRIFT_WARN:
                warnings.append(f"base rate drifted: {hd['fit_p']:.0%} before {hd['recent_from']}, {hd['recent_p']:.0%} since")
    elif availability["okx_daily"] == "available":  # candles exist, but none closed before as_of
        availability["okx_daily"] = "partial"
        warnings.append(f"no daily candles closed before {as_of}")
    if k:
        dist = f"{'+' if direction == 'up' else '-'}{k:g} ATR {'above' if direction == 'up' else 'below'}"
        what = f"touch {dist} within {h}d" if event == "touch" else f"close {dist} after {h}d"
    else:
        what = f"close {'higher' if direction == 'up' else 'lower'} after {h}d"
    headline = (f"{symbol}: {data['p']:.0%} of {data['n_days']} {data['tercile']}-volatility days {what}"
                if data.get("p") is not None else f"{symbol}: base rate unavailable")
    status = {"available": "ok", "partial": "partial"}.get(availability["okx_daily"], "unavailable")
    return {"schema_version": SCHEMA_VERSION, "tool": NAME, "status": status,
            "data_mode": "unknown" if status == "unavailable" else "live",  # nothing observed is not "live"
            "as_of": datetime.now(timezone.utc).isoformat(timespec="seconds"), "request": request, "data": data,
            "summary": {"headline": headline, "key_points": []}, "availability": availability, "warnings": warnings}


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
    if "k" in args and not (_finite(args["k"]) and 0 <= args["k"] <= MAX_K):
        raise ValueError(f"{NAME}: arg k must be a number from 0 to {MAX_K:g}")
    h = args.get("horizon_days")
    if h is not None and not (isinstance(h, int) and not isinstance(h, bool) and 1 <= h <= 14):
        raise ValueError(f"{NAME}: arg horizon_days must be an integer from 1 to 14")
    for name in ("direction", "event"):
        allowed = next(a["enum"] for a in SKILL_DEFINITION["args"] if a["name"] == name)
        if name in args and args[name] not in allowed:
            raise ValueError(f"{NAME}: arg {name} must be one of {allowed}")
    if "atr_14_pct" in args and not (_finite(args["atr_14_pct"]) and args["atr_14_pct"] > 0):
        raise ValueError(f"{NAME}: arg atr_14_pct must be a positive number")
    if "as_of" in args:
        try:
            ok = isinstance(args["as_of"], str) and DATE_RE.fullmatch(args["as_of"]) and datetime.fromisoformat(args["as_of"])
        except ValueError:
            ok = False
        if not ok:
            raise ValueError(f"{NAME}: arg as_of must be a YYYY-MM-DD date")
    # the day's own close already sits 0 ATR away, so every day "touches": a 99% that says nothing
    if args.get("k", 1.0) == 0 and args.get("event", "touch") == "touch":
        raise ValueError(f"{NAME}: k must be above 0 for event=touch; use event=close for k=0")
    return args


def invoke(args: dict[str, Any], http: httpx.Client | None = None) -> dict[str, Any]:
    """RYO's SkillCallResponse: {name, status: success|error, result: <envelope>, latency_ms, xp, guard_decision}.
    Bad args raise ValueError; a source failure is never raised, it is reported inside `result`."""
    started = time.monotonic()
    env = move_base_rate(**check_args(args), http=http)
    # SkillCastStatus: pending | running | success | error. Only "OKX gave nothing" is an error.
    return {"name": NAME, "status": "error" if env["status"] == "unavailable" else "success", "result": env,
            "latency_ms": int((time.monotonic() - started) * 1000), "xp": SKILL_DEFINITION["xp"], "guard_decision": None}


if __name__ == "__main__":
    import json
    import sys

    print(json.dumps(invoke(json.loads(sys.argv[1]) if len(sys.argv) > 1 else {"symbol": "SOL"}), indent=2))
