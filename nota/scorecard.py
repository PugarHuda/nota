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
from collections import Counter
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


def _candles(rows: list[list[str]], span_ms: int) -> list[dict[str, float]]:
    """Confirmed candles only (r[8] == '1'), oldest first; `end` is the candle's close time in ms."""
    out = [{"ts": int(r[0]), "open": float(r[1]), "high": float(r[2]), "low": float(r[3]), "close": float(r[4]),
            "end": int(r[0]) + span_ms} for r in rows if r[8] == "1"]
    return sorted(out, key=lambda c: c["ts"])


def okx_hourly(inst: str, start_ms: int, end_ms: int, http: httpx.Client) -> list[dict[str, float]]:
    """Closed 1 h candles whose open time is in [start_ms, end_ms), oldest first."""
    rows = _okx(http, "/market/history-candles",
                {"instId": inst, "bar": "1H", "before": start_ms - 1, "after": end_ms, "limit": 100})
    return _candles(rows, 3_600_000)


def okx_minutes(inst: str, start_ms: int, end_ms: int, http: httpx.Client) -> list[dict[str, float]]:
    """Closed 1 m candles whose open time is in [start_ms, end_ms), oldest first. OKX answers 100 a
    page, newest first, so each next page asks for what is older than the oldest open time seen."""
    rows: list[list[str]] = []
    after = end_ms
    while after > start_ms:
        page = _okx(http, "/market/history-candles",
                    {"instId": inst, "bar": "1m", "before": start_ms - 1, "after": after, "limit": 100})
        if not page:
            break
        rows += page
        oldest = min(int(r[0]) for r in page)
        if oldest >= after:  # a page that does not move backwards would loop forever
            break
        after = oldest
    return _candles(rows, 60_000)


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
        # the trace id and status are what RYO support needs to find this failure
        row.update(status="ryo_unavailable", error=f"{exc.code}: {exc.message}", trace_id=exc.trace_id, status_code=exc.status_code)
        row["id"] = _row_id(symbol, row["locked_at"], row["status"])
        return row
    row["envelope"] = env.model_dump(mode="json")
    row["momentum_gate"] = momentum_gate(row["envelope"])
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


def momentum_gate(envelope: dict[str, Any] | None) -> bool | None:
    """Whether RYO's own momentum gate (confluence.gates, name 'momentum') passed; None when absent."""
    conf = ((envelope or {}).get("data") or {}).get("confluence")
    gates = conf.get("gates") if isinstance(conf, dict) else None
    for g in gates if isinstance(gates, list) else []:
        if isinstance(g, dict) and g.get("name") == "momentum" and isinstance(g.get("passed"), bool):
            return g["passed"]
    return None


GOOD = ("locked", "basis_mismatch", "no_plan")   # RYO answered: a sample, even when it cannot be settled
RELOCK_H = 20          # two locks closer than this are one market, counted twice
ABORT_AFTER = 3        # consecutive RYO failures before the rest of the universe is not even asked


def lock_all(source: RyoSource, ledger: Ledger, symbols: list[str] | None = None, http: httpx.Client | None = None,
             sleep: Callable[[float], None] = time.sleep, pace_s: float = PACE_S, clock: Callable[[], float] = time.monotonic,
             on_row: Callable[[dict[str, Any]], None] | None = None, now: datetime | None = None) -> list[dict[str, Any]]:
    """One lock per symbol per RELOCK_H hours: a re-run skips what it already has, or the contrast would
    count one market twice. A failed lock does not count as had, so a re-run retries it, and it keeps a
    single row per symbol-day that a later success deletes.

    Circuit breaker: a 401/403 or a missing key on the first symbol, or ABORT_AFTER RYO failures in a
    row, stops the run with one `aborted` row naming the symbols not asked, instead of 25 identical
    failures paced 12 s apart."""
    http = http or httpx.Client(timeout=20.0)
    now = now or datetime.now(timezone.utc)
    last_good: dict[str, datetime] = {}
    for day in ((now - timedelta(days=1)).date().isoformat(), now.date().isoformat()):
        for _, j in ledger.list_locks(day):
            r = json.loads(j)
            if r["status"] in GOOD:
                t = datetime.fromisoformat(r["locked_at"])
                last_good[r["symbol"]] = max(t, last_good.get(r["symbol"], t))
    todo = [s.upper() for s in (symbols or list(UNIVERSE))
            if not (s.upper() in last_good and now - last_good[s.upper()] < timedelta(hours=RELOCK_H))]
    rows: list[dict[str, Any]] = []
    prev_start: float | None = None
    failures = 0

    def keep(row: dict[str, Any]) -> None:
        rows.append(row)
        if on_row:
            on_row(row)

    for i, s in enumerate(todo):
        wait = 0.0 if prev_start is None else pace_s - (clock() - prev_start)
        if wait > 0:  # pace by start time: a deep call that took 70 s has already used up the gap
            sleep(wait)
        prev_start = clock()
        row = lock_one(s, source, http)
        failed_id = _row_id(s, row["locked_at"][:10], "failed")
        if row["status"] in GOOD:
            ledger.save_lock(row["id"], row["symbol"], row["locked_at"], row["status"], json.dumps(row))
            ledger.delete_lock(failed_id)
        else:
            row["id"] = failed_id
            ledger.replace_lock(row["id"], row["symbol"], row["locked_at"], row["status"], json.dumps(row))
        keep(row)
        failures = failures + 1 if row["status"] == "ryo_unavailable" else 0
        auth = row.get("status_code") in (401, 403) or (row["error"] or "").startswith("NO_KEY")
        if (i == 0 and auth) or failures >= ABORT_AFTER:
            day = row["locked_at"][:10]
            abort = {"id": _row_id("*", day, "aborted"), "symbol": "*", "locked_at": now_iso(), "status": "aborted",
                     "error": row["error"], "trace_id": row.get("trace_id"), "status_code": row.get("status_code"),
                     "skipped": todo[i + 1:], "verdict": None, "confluence_state": None, "basis_pct": None}
            ledger.replace_lock(abort["id"], "*", abort["locked_at"], "aborted", json.dumps(abort))
            keep(abort)
            break
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
    """'target' | 'stop' | 'ambiguous' (both inside one candle) | 'neither'; plus the candle index."""
    for i, c in enumerate(candles):
        if b["side"] == "long":
            hit_t, hit_s = c["high"] >= b["target"], c["low"] <= b["stop"]
        else:
            hit_t, hit_s = c["low"] <= b["target"], c["high"] >= b["stop"]
        if hit_t and hit_s:
            return "ambiguous", i
        if hit_t:
            return "target", i
        if hit_s:
            return "stop", i
    return "neither", None


UNSETTLEABLE_AFTER = timedelta(hours=24)   # OKX backfills a late candle within minutes; a day later it never will


def _ms(t: datetime) -> int:
    return int(t.timestamp() * 1000)


def settle_one(row: dict[str, Any], horizon_h: int, http: httpx.Client, now: datetime | None = None) -> dict[str, Any] | None:
    """None while the horizon has not passed yet. The path is OKX's 1 m candles from the lock to the first
    full hour, then `horizon_h` hourly candles. When one hour touched both levels, that hour's 1 m candles
    decide which came first (`resolved_by: '1m'`); both inside one minute stays 'ambiguous'.

    A window with an hourly candle missing is retried until UNSETTLEABLE_AFTER past its end, then stored
    as `unsettleable` with the missing hours, so it leaves the open list instead of waiting forever."""
    now = now or datetime.now(timezone.utc)
    locked = datetime.fromisoformat(row["locked_at"])
    start = (locked + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
    end = start + timedelta(hours=horizon_h)
    if now < end + timedelta(minutes=5):
        return None
    b = bracket(row)
    window = [start.isoformat(), end.isoformat()]
    first_minute = locked.replace(second=0, microsecond=0) + (timedelta(minutes=1) if locked.second or locked.microsecond else timedelta())
    try:
        candles = okx_hourly(row["inst"], _ms(start), _ms(end), http)
        if len(candles) < horizon_h:
            have = {c["ts"] for c in candles}
            missing = [(start + timedelta(hours=i)).isoformat() for i in range(horizon_h) if _ms(start + timedelta(hours=i)) not in have]
            status = "unsettleable" if now > end + UNSETTLEABLE_AFTER else "incomplete_candles"
            return {"status": status, **({"result": "unsettleable"} if status == "unsettleable" else {}),
                    "candles": len(candles), "missing_hours": missing, "horizon_h": horizon_h, "window": window}
        minutes = okx_minutes(row["inst"], _ms(first_minute), _ms(start), http)
        path = minutes + candles
        result, idx = first_touch(b, path)
        touched = None if idx is None else path[idx]
        resolved_by = None
        if result == "ambiguous" and idx is not None and idx >= len(minutes):  # an hour, not a minute: look inside it
            inner = okx_minutes(row["inst"], int(touched["ts"]), int(touched["end"]), http)
            r2, j = first_touch(b, inner)
            if r2 in ("target", "stop"):
                result, touched, resolved_by = r2, inner[j], "1m"
    except OkxUnavailable as exc:
        return {"status": "okx_unavailable", "error": str(exc), "horizon_h": horizon_h}
    last = candles[-1]["close"]
    ret = (last / b["entry"] - 1) * 100 * (1 if b["side"] == "long" else -1)
    return {"status": "settled", "result": result, "resolved_by": resolved_by,
            "hours_to_touch": None if touched is None else round((touched["end"] - _ms(locked)) / 3_600_000, 2),
            "horizon_h": horizon_h, "bracket": {k: round(v, 10) if isinstance(v, float) else v for k, v in b.items()},
            "close_at_horizon": last, "return_at_horizon_pct": round(ret, 3), "candles": len(candles),
            # a missing minute before the first hour could hide a touch: said, not assumed away
            "minutes_before_first_hour": [len(minutes), int((start - first_minute).total_seconds() // 60)],
            "window": window}


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
            if res["status"] in ("settled", "unsettleable"):  # transient failures are retried on the next cycle
                ledger.save_settlement(lock_id, h, json.dumps(res))
            out.append({"lock_id": lock_id, "symbol": row["symbol"], **res})
    return out


def peer_derivatives(ledger: Ledger, day: str) -> list[dict[str, Any]]:
    """RYO's derivatives blocks from the day's locks: the cross-section `positioning_check` needs to
    tell a token-specific number from one that is the same for every token."""
    out = []
    for _, j in ledger.list_locks(day):
        row = json.loads(j)
        d = ((row.get("envelope") or {}).get("data") or {}).get("derivatives")
        if isinstance(d, dict):
            out.append({"symbol": row["symbol"], **{k: d.get(k) for k in ("funding_rate_bps", "open_interest_change_24h_pct")}})
    return out


# -- read ---------------------------------------------------------------------------------------

BOOTSTRAP_N = 2000
MIN_CONTRAST_DAYS = 10    # below this many shared days nothing is called distinguishable, whatever p says
ALPHA = 0.10
# Besag & Clifford (1991): a sequential Monte Carlo p-value may stop once this many permuted statistics
# reach the observed one and stays valid; a plainly null contrast then costs ~40 rounds, not BOOTSTRAP_N.
STOP_AT_HITS = 20


def _rate(rows: list[dict[str, Any]]) -> tuple[int, int]:
    decided = [r for r in rows if r["result"] in ("target", "stop")]
    return sum(r["result"] == "target" for r in decided), len(decided)


def _locked_at(r: dict[str, Any]) -> datetime:
    return datetime.fromisoformat(r.get("locked_at") or f"{r['day']}T00:00:00+00:00")


def clusters(rows: list[dict[str, Any]], horizon_h: int) -> dict[str, int]:
    """Lock day -> cluster. Consecutive lock days whose windows overlap by more than half the horizon
    (median lock times less than horizon/2 apart) watch mostly the same price path, so they form one
    cluster: at 72 h daily locks chain into one, at 24 h days 12 h or more apart stay separate."""
    by_day: dict[str, list[datetime]] = {}
    for r in rows:
        by_day.setdefault(r["day"], []).append(_locked_at(r))
    out: dict[str, int] = {}
    cid, prev = -1, None
    for day in sorted(by_day):
        ts = sorted(by_day[day])
        med = ts[len(ts) // 2]
        if prev is None or med - prev >= timedelta(hours=horizon_h / 2):
            cid += 1
        out[day], prev = cid, med
    return out


def _mh(cells: list[list[float]]) -> float | None:
    """Mantel-Haenszel weighted difference of group means over day strata: each day's a-minus-b
    difference, weighted na*nb/(na+nb). Pooling plans across days instead lets a day with many plans of
    one kind and a bad market flip the sign (Simpson's paradox)."""
    num = den = 0.0
    for sa, na, sb, nb in cells:
        if na and nb:
            w = na * nb / (na + nb)
            num += w * (sa / na - sb / nb)
            den += w
    return num / den if den else None


def _stratified(rows: list[dict[str, Any]], key: str, a: Any, value: Callable[[dict[str, Any]], float],
                cl: dict[str, int], seed: int, rounds: int) -> dict[str, Any]:
    """MH difference; a permutation p-value with labels shuffled only within a cluster, so each cluster
    keeps its market; and, from MIN_CONTRAST_DAYS shared days on, a 90% interval from a bootstrap over
    clusters, never over plans (plans on one day move together)."""
    days = sorted({r["day"] for r in rows})
    di = {d: i for i, d in enumerate(days)}
    items = [(di[r["day"]], value(r)) for r in rows]
    labels = [r[key] == a for r in rows]

    def cells_of(lab: list[bool]) -> list[list[float]]:
        cells = [[0.0, 0, 0.0, 0] for _ in days]
        for (d, v), la in zip(items, lab):
            c = cells[d]
            if la:
                c[0] += v
                c[1] += 1
            else:
                c[2] += v
                c[3] += 1
        return cells

    obs = cells_of(labels)
    point = _mh(obs)
    out: dict[str, Any] = {"days": len(days), "clusters": len({cl[d] for d in days}), "point": point, "p_value": None, "ci90": None}
    if point is None:
        return out
    rng = random.Random(seed)  # fixed seed: the page shows the same numbers every time it is read
    groups: dict[int, list[int]] = {}
    for i, r in enumerate(rows):
        groups.setdefault(cl[r["day"]], []).append(i)
    hits = n = 0
    while n < rounds and hits < STOP_AT_HITS:
        perm = labels[:]
        for idx in groups.values():
            shuffled = [perm[i] for i in idx]
            rng.shuffle(shuffled)
            for i, la in zip(idx, shuffled):
                perm[i] = la
        s = _mh(cells_of(perm))
        n += 1
        hits += s is not None and abs(s) >= abs(point) - 1e-12
    out["p_value"] = round(hits / n if hits >= STOP_AT_HITS else (hits + 1) / (n + 1), 4)
    if len(days) >= MIN_CONTRAST_DAYS:
        per_cluster: dict[int, list[list[float]]] = {}
        for d, cells in zip(days, obs):
            per_cluster.setdefault(cl[d], []).append(cells)
        keys = list(per_cluster)
        draws = sorted(x for x in (_mh([c for k in (rng.choice(keys) for _ in keys) for c in per_cluster[k]])
                                   for _ in range(BOOTSTRAP_N)) if x is not None)
        out["ci90"] = [draws[int(0.05 * len(draws))], draws[int(0.95 * len(draws)) - 1]]
    return out


def contrast(settled: list[dict[str, Any]], key: str, a: Any, b: Any, horizon_h: int = 24, seed: int = 7,
             rounds: int = BOOTSTRAP_N) -> dict[str, Any]:
    """Group `a` against group `b` on two measures, each over the days on which both groups had a plan:

    - target-first rate among decided plans (target or stop), as a Mantel-Haenszel risk difference;
    - mean return at the horizon over every settled plan, 'neither' included, weighted the same way.

    `distinguishable` needs MIN_CONTRAST_DAYS shared days and p < ALPHA, so with no real difference it
    comes out true at most ALPHA of the time (simulated in tests/test_scorecard.py)."""
    cl = clusters(settled, horizon_h)
    mine = [r for r in settled if r.get(key) is not None and r.get(key) in (a, b)]

    def shared(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        seen: dict[str, set[bool]] = {}
        for r in rows:
            seen.setdefault(r["day"], set()).add(r[key] == a)
        return [r for r in rows if len(seen[r["day"]]) == 2]

    decided = shared([r for r in mine if r["result"] in ("target", "stop")])
    moved = shared([r for r in mine if isinstance(r.get("return_at_horizon_pct"), (int, float))])
    rate = _stratified(decided, key, a, lambda r: 1.0 if r["result"] == "target" else 0.0, cl, seed, rounds)
    ret = _stratified(moved, key, a, lambda r: float(r["return_at_horizon_pct"]), cl, seed, rounds)
    ta, na = _rate([r for r in decided if r[key] == a])
    tb, nb = _rate([r for r in decided if r[key] != a])

    def mean(rows: list[dict[str, Any]]) -> float | None:
        return round(sum(r["return_at_horizon_pct"] for r in rows) / len(rows), 3) if rows else None

    def distinguishable(x: dict[str, Any]) -> bool:
        return x["days"] >= MIN_CONTRAST_DAYS and x["p_value"] is not None and x["p_value"] < ALPHA

    return {"key": key, "a": a, "b": b, "days": rate["days"], "clusters": rate["clusters"], "min_days": MIN_CONTRAST_DAYS,
            "a_rate": [ta, na], "b_rate": [tb, nb], "p_value": rate["p_value"],
            "diff_pct": None if rate["point"] is None else round(rate["point"] * 100, 1),
            "ci90_pct": None if rate["ci90"] is None else [round(x * 100, 1) for x in rate["ci90"]],
            "distinguishable": distinguishable(rate),
            "return": {"days": ret["days"], "clusters": ret["clusters"], "p_value": ret["p_value"],
                       "a_mean_pct": mean([r for r in moved if r[key] == a]), "b_mean_pct": mean([r for r in moved if r[key] != a]),
                       "diff_pct": None if ret["point"] is None else round(ret["point"], 3),
                       "ci90_pct": None if ret["ci90"] is None else [round(x, 3) for x in ret["ci90"]],
                       "distinguishable": distinguishable(ret)}}


BEARISH = {"cautious", "bearish", "avoid", "negative"}
BULLISH = {"constructive", "bullish", "positive", "accumulate"}


def verdict_contradicts_side(side: str, verdict: str | None) -> bool:
    """A long bracket under a bearish verdict, or a short one under a bullish verdict: RYO's words and
    RYO's plan point opposite ways."""
    v = (verdict or "").lower()
    return (side == "long" and v in BEARISH) or (side == "short" and v in BULLISH)


def ryo_lanes(locks: list[dict[str, Any]]) -> dict[str, Any]:
    """How reliably each lane of RYO's deep_analysis answered across the locks: RYO's own availability
    word counted per lane, the token-profile lane's latency, and the inputs RYO said it was missing."""
    lanes: dict[str, Counter[str]] = {}
    latency: list[float] = []
    missing: Counter[str] = Counter()
    for r in locks:
        env = r.get("envelope")
        if not isinstance(env, dict):
            continue
        for lane, st in (env.get("availability") or {}).items():
            lanes.setdefault(lane, Counter())[str(st)] += 1
        tp = (env.get("data") or {}).get("token_profile")
        if not isinstance(tp, dict):
            continue
        ms = (tp.get("execution") or {}).get("elapsed_ms")
        if isinstance(ms, (int, float)) and not isinstance(ms, bool):
            latency.append(ms)
        inner = (tp.get("data") or {}).get("data") if isinstance(tp.get("data"), dict) else None
        missing.update(str(x) for x in ((inner if isinstance(inner, dict) else {}).get("missing_or_stale_inputs") or []))
    latency.sort()
    return {"lanes": {k: dict(v) for k, v in sorted(lanes.items())},
            "profile_latency_ms": [latency[0], latency[len(latency) // 2], latency[-1]] if latency else None,
            "missing_inputs": dict(missing.most_common())}


OVERDUE_AFTER = timedelta(hours=1)   # the hourly settle cycle records a closed window within this


def summary(ledger: Ledger, horizon_h: int = 24, now: datetime | None = None, stats: bool = True) -> dict[str, Any]:
    """The scorecard. `stats=False` skips the contrasts, the only costly part, for callers that read rows."""
    now = now or datetime.now(timezone.utc)
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
        settles_at = start + timedelta(hours=horizon_h)
        gate = r["momentum_gate"] if "momentum_gate" in r else momentum_gate(r.get("envelope"))  # rows locked before it was stored
        base = {"id": r["id"], "symbol": r["symbol"], "day": r["locked_at"][:10], "locked_at": r["locked_at"],
                "settles_at": settles_at.isoformat(), "trace_id": r.get("trace_id"),
                "confluence_score": r.get("confluence_score"), "atr_14_pct": (r.get("plan") or {}).get("atr_14_pct"),
                "verdict": r["verdict"], "confluence_state": r["confluence_state"], "momentum_gate": gate, "side": side,
                "verdict_contradicts_side": verdict_contradicts_side(side, r["verdict"])}
        if s is None:
            open_plans.append({**base, "overdue": now > settles_at + OVERDUE_AFTER})
            continue
        s = json.loads(s)
        if s["result"] == "unsettleable":  # an OKX gap: counted, never rated, never left open
            coverage["unsettleable"] = coverage.get("unsettleable", 0) + 1
            continue
        coverage[f"settled_{s['result']}"] = coverage.get(f"settled_{s['result']}", 0) + 1
        settled.append({**base, **{k: s.get(k) for k in ("result", "hours_to_touch", "return_at_horizon_pct", "resolved_by")}})
    contrasts = []
    if stats:
        for key in ("confluence_state", "verdict"):
            vals = sorted({r[key] for r in settled if r[key]})
            contrasts += [contrast(settled, key, a, b, horizon_h) for i, a in enumerate(vals) for b in vals[i + 1:]]
        if {True, False} <= {r["momentum_gate"] for r in settled}:
            contrasts.append(contrast(settled, "momentum_gate", True, False, horizon_h))
        contrasts = [c for c in contrasts if c["days"] or c["return"]["days"]]
    t, n = _rate(settled)
    return {"horizon_h": horizon_h, "universe": list(UNIVERSE),
            "lock_days": len({r["locked_at"][:10] for r in locks if r["status"] in GOOD}),
            "coverage": coverage, "target_first": [t, n], "contrasts": contrasts, "min_contrast_days": MIN_CONTRAST_DAYS,
            "open": sorted(open_plans, key=lambda r: r["settles_at"]),
            "settled": sorted(settled, key=lambda r: r["locked_at"], reverse=True),
            "contradictions": sum(r["verdict_contradicts_side"] for r in open_plans + settled),
            **ryo_lanes(locks)}
