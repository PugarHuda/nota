"""Skill `move_base_rate`: how often does a move this size happen anyway?

RYO gives ATR(14) and a verdict but no probability. Before acting on "SOL cautious, ATR 4.1%" a
reader wants the base rate: in this token's own recent history, on days with similar volatility,
how often did price reach `k` ATRs away within `h` days? This skill answers from about 400 days of
OKX daily candles (UTC days), counted, not modelled:

- `touch`: the high (up) or low (down) reached `k` x ATR from that day's close within `h` days.
- `close`: the close `h` days later was beyond `k` x ATR. With `k = 0` this is simply "higher after h
  days", the reference a forecaster of `p_up` has to beat.

Distances follow RYO's ATR when it is given. RYO's `atr_14_pct` runs a few percent under a Wilder
ATR computed from OKX's UTC candles (0.91-0.98 of it across the tokens checked on 2026-09-18), so the
request's `k` is converted into OKX-ATR units with today's ratio of the two, and that ratio is printed.

Only days in the same volatility tercile as today count. The result says how many days that is and
how many of them are independent (overlapping `h`-day windows share their future). A holdout splits
the history in time: the rate from the older three quarters against what happened in the newest
quarter, so a base rate that has drifted says so.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import httpx

from nota.envelope import Envelope
from nota.skills.contract import SkillArg, SkillDefinition, SourceUnavailable, make_envelope
from nota.skills.sources import UA
from nota.skills.technicals import PERIOD

DEFINITION = SkillDefinition(
    name="move_base_rate",
    description="Empirical probability that a token moves k ATRs (up or down, touched or closed beyond) within h days, counted "
    "from ~400 days of OKX daily candles on days in the same ATR tercile as today, with a time-split holdout. Pass RYO's "
    "atr_14_pct to measure the distance in RYO's ATR; k=0 with event=close gives the plain 'higher after h days' rate.",
    args=[
        SkillArg(name="symbol", type="string", description="Token symbol, e.g. SOL"),
        SkillArg(name="k", type="number", required=False, description="Distance in ATRs (default 1)"),
        SkillArg(name="horizon_days", type="integer", required=False, description="1 to 14 (default 3)"),
        SkillArg(name="direction", type="string", required=False, enum=["up", "down"], description="Default up"),
        SkillArg(name="event", type="string", required=False, enum=["touch", "close"], description="Default touch"),
        SkillArg(name="atr_14_pct", type="number", required=False, description="RYO's ATR(14) as % of price (analyze_token or deep_analysis)"),
        SkillArg(name="as_of", type="string", required=False, description="YYYY-MM-DD: use only candles closed before this day (scoring a past call)"),
    ],
)

OKX = "https://www.okx.com/api/v5/market/history-candles"
PAGES = 4  # 100 daily candles each
MIN_DAYS = 60


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
        body = resp.json() if resp.status_code == 200 else {}
        if body.get("code") != "0":
            raise SourceUnavailable(f"okx: {body.get('msg') or 'HTTP ' + str(resp.status_code)}")
        page = body.get("data") or []
        if not page:
            break
        rows += page
        after = page[-1][0]
    if not rows:
        raise SourceUnavailable(f"okx: no daily candles for {symbol}-USDT")
    out = {int(r[0]): {"ts": int(r[0]), "high": float(r[2]), "low": float(r[3]), "close": float(r[4])} for r in rows if r[8] == "1"}
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


def base_rate(daily: list[dict[str, float]], k: float, h: int, direction: str, event: str, k_scale: float = 1.0) -> dict[str, Any]:
    """Pure: everything the envelope reports, from candles alone."""
    atrs = wilder_atr_pct(daily)
    days = [i for i in range(len(daily) - h) if atrs[i] is not None]
    if len(days) < MIN_DAYS or atrs[-1] is None:
        return {"p": None, "reason": f"{len(days)} usable days, need {MIN_DAYS}"}
    ranked = sorted(atrs[i] for i in days)
    cut = [ranked[len(ranked) // 3], ranked[2 * len(ranked) // 3]]
    tercile = lambda a: 0 if a < cut[0] else (1 if a < cut[1] else 2)
    today = tercile(atrs[-1])
    same = [i for i in days if tercile(atrs[i]) == today]
    ke = k * k_scale

    def rate(ix: list[int]) -> tuple[int, int]:
        return sum(hit(daily, i, ke * atrs[i], h, direction, event) for i in ix), len(ix)

    hits, n = rate(same)
    split = days[int(len(days) * 0.75)]
    old, new = [i for i in same if i < split], [i for i in same if i >= split]
    (ho, no), (hn, nn) = rate(old), rate(new)
    return {"p": round(hits / n, 4) if n else None, "hits": hits, "n_days": n, "n_independent": n // max(h, 1),
            "tercile": ["low", "mid", "high"][today], "tercile_cuts_atr_pct": [round(x, 3) for x in cut],
            "atr_14_pct_okx_today": round(atrs[-1], 3), "k_in_okx_atr": round(ke, 4),
            "holdout": {"fit_p": round(ho / no, 4) if no else None, "fit_days": no,
                        "recent_p": round(hn / nn, 4) if nn else None, "recent_days": nn,
                        "recent_from": datetime.fromtimestamp(daily[split]["ts"] / 1000, timezone.utc).date().isoformat()},
            "history": [datetime.fromtimestamp(daily[0]["ts"] / 1000, timezone.utc).date().isoformat(),
                        datetime.fromtimestamp(daily[-1]["ts"] / 1000, timezone.utc).date().isoformat()]}


DRIFT_WARN = 0.10


def move_base_rate(symbol: str, k: float = 1.0, horizon_days: int = 3, direction: str = "up", event: str = "touch",
                   atr_14_pct: float | None = None, as_of: str | None = None, http: httpx.Client | None = None) -> Envelope:
    symbol = symbol.upper()
    h = max(1, min(int(horizon_days), 14))
    k = max(0.0, float(k))
    request = {"symbol": symbol, "k": k, "horizon_days": h, "direction": direction, "event": event, "atr_14_pct": atr_14_pct, "as_of": as_of}
    warnings: list[str] = []
    data: dict[str, Any] = {"symbol": symbol, "p": None, "event": event, "direction": direction, "k": k, "horizon_days": h,
                            "method": "okx_1Dutc_wilder_atr14_same_tercile_count", "atr_scale": None}
    try:
        daily = okx_daily(symbol, http or httpx.Client(timeout=20.0, headers={"User-Agent": UA}))
        if as_of:
            cutoff = datetime.fromisoformat(as_of).replace(tzinfo=timezone.utc).timestamp() * 1000
            daily = [d for d in daily if d["ts"] < cutoff]
        availability = {"okx_daily": "ok"}
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
    if k:
        what = f"{'reach' if event == 'touch' else 'close beyond'} {'+' if direction == 'up' else '-'}{k:g} ATR within {h}d"
    else:
        what = f"{'touch above' if event == 'touch' else 'close'} {'higher' if direction == 'up' else 'lower'} after {h}d"
    headline = (f"{symbol}: {data['p']:.0%} of {data['n_days']} {data['tercile']}-volatility days {what}"
                if data.get("p") is not None else f"{symbol}: base rate unavailable")
    return make_envelope("move_base_rate", request, data, availability, warnings, headline, primary=["okx_daily"])
