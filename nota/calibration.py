"""Calibration: resolve past decisions against the price at their seven-day horizon and score each agent.

Brier score = (p_up_7d - outcome)^2, outcome 1 if price rose. Lower is better; 0.25 is a coin
flip. Weights feed back into the judge prompt so agents that are often wrong lose influence.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from pydantic import BaseModel

from nota import paths
from nota.council import ROLES
from nota.evidence import SCORED_SOURCES, EvidencePack, Section, first_present
from nota.ledger import Ledger, now_iso
from nota.receipt import Receipt, ryo_direction
from nota.risk import PracticeTrade
from nota.ryo_client import RyoError, RyoSource
from nota.scorecard import UNIVERSE, OkxUnavailable, okx_hourly

HORIZON_DAYS = 7


class CannotResolve(Exception):
    pass


class Outcome(BaseModel):
    decision_id: str
    symbol: str
    resolved_at: str
    decided_as_of: str | None
    horizon_reached: bool
    price_then: float
    price_now: float
    price_now_source: str = "ryo"
    price_now_as_of: str | None = None
    return_pct: float
    went_up: bool
    brier: dict[str, float]
    trade_result_usd: float | None = None
    closed_reason: str | None = None  # 'stopped' | 'target' when a practice position was exited early
    # How often this token closed higher 7 days later, counted on candles closed before the decision day
    # (skill move_base_rate). The reference a p_up has to beat; None when it could not be counted.
    base_rate_p: float | None = None


def _price_then(receipt: Receipt, ledger: Ledger) -> float | None:
    if isinstance(receipt.trade, PracticeTrade):
        return receipt.trade.entry_price
    pack_json = ledger.get_pack(receipt.pack_hash)
    if pack_json is None:
        return None
    # The exchange median recorded beside RYO's evidence is the decision-time price when RYO answered
    # nothing (the 401 day), the same fallback `_price_now` takes for the other end of the horizon.
    # ponytail: then and now can come from different feeds when a key returns; the basis gap is a few
    # tenths of a percent, record which one priced `then` on the Outcome if that ever matters.
    _, price = first_present(EvidencePack.model_validate_json(pack_json), paths.PRICE_USD + ["price_check.data.median_usd"])
    return price


def _price_now(symbol: str, source: RyoSource | None) -> tuple[float | None, str | None, str]:
    """RYO's analyze_token first; when there is no source at all, or it fails or carries no price, the
    exchange median from `price_crosscheck`, labelled as such. Returns (price, as_of, source_label).

    `source=None` is the keyless path: scoring a decision needs no builder key, the same way verifying
    one needs no model key. The price it is scored against is then an independent one by construction."""
    if source is not None:
        try:
            env = source.call("analyze_token", {"symbol": symbol})
            probe = EvidencePack(symbol=symbol, created_at=now_iso(), source=source.name,
                                 sections={"analyze_token": Section(tool="analyze_token", status=env.status, envelope=env)})
            _, price = first_present(probe, [p for p in paths.PRICE_USD if p.startswith("analyze_token.")])
            if price is not None:
                return price, env.as_of, f"ryo:{source.name}"
        except RyoError:
            pass
    from nota.skills.price_check import price_crosscheck

    check = price_crosscheck(symbol)
    med = check.get("median_usd")
    if med is None:
        raise CannotResolve("no price from RYO analyze_token nor from any exchange")
    return float(med), check.as_of, f"exchange_median:{check.get('sources_ok')}_sources"


def _build_outcome(receipt: Receipt, ledger: Ledger, price_now: float, as_of_now: str | None, price_source: str,
                   closed_reason: str | None = None, resolved_at: str | None = None) -> Outcome:
    price_then = _price_then(receipt, ledger)
    if price_then is None:
        raise CannotResolve("price unavailable at decision time; refusing to score with a guess")
    went_up = price_now > price_then
    outcome_val = 1.0 if went_up else 0.0
    brier = {o.role: round((o.p_up_7d - outcome_val) ** 2, 4) for o in receipt.opinions}
    brier["judge"] = round((receipt.verdict.p_up_7d - outcome_val) ** 2, 4)
    decided_as_of = receipt.provenance.get("deep_analysis", {}).get("as_of") or receipt.created_at
    trade_result = None
    if isinstance(receipt.trade, PracticeTrade):
        sign = 1.0 if receipt.trade.side == "long" else -1.0
        trade_result = round(sign * (price_now - price_then) * receipt.trade.size_units, 2)
    out = Outcome(
        decision_id=receipt.id, symbol=receipt.symbol, resolved_at=resolved_at or now_iso(), decided_as_of=decided_as_of,
        horizon_reached=False, price_then=price_then, price_now=price_now, price_now_source=price_source, price_now_as_of=as_of_now,
        return_pct=round((price_now / price_then - 1.0) * 100.0, 4), went_up=went_up, brier=brier, trade_result_usd=trade_result,
        closed_reason=closed_reason,
    )
    out.horizon_reached = _at_horizon(out.model_dump(), receipt)
    ledger.save_outcome(receipt.id, out.model_dump_json())
    return out


def _ts(iso: str) -> datetime:
    return datetime.fromisoformat(iso.replace("Z", "+00:00"))


def _at_horizon(o: dict[str, Any], receipt: Receipt) -> bool:
    """The outcome answers "is the price higher seven days after the decision": not closed early at a
    stop or target, and resolved at least HORIZON_DAYS after it was decided. Computed from the stored
    times, so an outcome whose `horizon_reached` flag was written wrong still counts correctly."""
    if o.get("closed_reason") is not None:
        return False
    try:
        return _ts(o["resolved_at"]) - _ts(o.get("decided_as_of") or receipt.created_at) >= timedelta(days=HORIZON_DAYS)
    except (KeyError, TypeError, ValueError):
        return False


def _horizon_close(symbol: str, created_at: str, http: httpx.Client) -> tuple[float, str] | None:
    """Close of the OKX 1H candle that ends at the decision's seven-day horizon (floored to the hour):
    the last price the question is about, whenever the cycle gets round to asking."""
    close_at = (_ts(created_at) + timedelta(days=HORIZON_DAYS)).replace(minute=0, second=0, microsecond=0)
    open_ms = int((close_at - timedelta(hours=1)).timestamp() * 1000)
    try:
        candles = okx_hourly(UNIVERSE.get(symbol, f"{symbol}-USDT"), open_ms, open_ms + 3_600_000, http)
    except OkxUnavailable:
        return None
    return (candles[-1]["close"], close_at.isoformat()) if candles else None


def resolve(decision_id: str, ledger: Ledger, source: RyoSource | None, http: httpx.Client | None = None,
            now: datetime | None = None) -> Outcome:
    """Past the horizon, the price is the OKX hourly close at the horizon (`okx_1h_close_at_horizon`).
    When OKX cannot give it, the current price is used and labelled `late:<hours>h:<source>`, so how
    far off the horizon it was taken is on the record. Before the horizon (`resolve --early`) it is
    the current price."""
    raw = ledger.get_decision(decision_id)
    if raw is None:
        raise KeyError(f"no decision {decision_id}")
    receipt = Receipt.model_validate_json(raw)
    now = now or datetime.now(timezone.utc)
    horizon = _ts(receipt.created_at) + timedelta(days=HORIZON_DAYS)
    if now >= horizon:
        at = _horizon_close(receipt.symbol, receipt.created_at, http or httpx.Client(timeout=20.0))
        if at is not None:
            return _build_outcome(receipt, ledger, at[0], at[1], "okx_1h_close_at_horizon", resolved_at=now.isoformat(timespec="seconds"))
    price_now, as_of_now, price_source = _price_now(receipt.symbol, source)
    if now >= horizon:
        price_source = f"late:{int((now - horizon).total_seconds() // 3600)}h:{price_source}"
    if price_now is None:
        raise CannotResolve("price unavailable at resolution time; refusing to score with a guess")
    return _build_outcome(receipt, ledger, price_now, as_of_now, price_source, resolved_at=now.isoformat(timespec="seconds"))


def close_position(decision_id: str, ledger: Ledger, price: float, price_source: str, reason: str) -> Outcome:
    """Practice-trade exit: the latest independent price crossed the stop or the target, so the position is
    closed now at that price and scored immediately. `closed_reason` says which level was hit; the horizon
    flag stays honest (usually False)."""
    raw = ledger.get_decision(decision_id)
    if raw is None:
        raise KeyError(f"no decision {decision_id}")
    receipt = Receipt.model_validate_json(raw)
    if not isinstance(receipt.trade, PracticeTrade):
        raise CannotResolve("no practice position on this receipt")
    return _build_outcome(receipt, ledger, price, now_iso(), price_source, closed_reason=reason)


def due(ledger: Ledger, now: datetime | None = None) -> list[str]:
    """Unresolved decisions whose seven-day horizon has passed (measured from the receipt's own created_at).
    Scoring earlier would freeze a premature outcome, because `unresolved()` never returns a scored id again."""
    now = now or datetime.now(timezone.utc)
    out = []
    for id in ledger.unresolved():
        raw = ledger.get_decision(id)
        if raw is None:
            continue
        receipt = Receipt.model_validate_json(raw)
        if receipt.source not in SCORED_SOURCES:
            continue
        created = datetime.fromisoformat(receipt.created_at.replace("Z", "+00:00"))
        if (now - created).days >= HORIZON_DAYS:
            out.append(id)
    return out


def _scored_outcomes(ledger: Ledger) -> list[tuple[dict[str, Any], Receipt]]:
    """Outcomes whose receipt was decided on RYO evidence (live or recorded), with that receipt. A
    receipt built from any other source is kept and shown, but never counts towards a score."""
    out = []
    for raw in ledger.list_outcomes():
        o = json.loads(raw)
        stored = ledger.get_decision(o["decision_id"])
        if stored is not None and json.loads(stored).get("source") in SCORED_SOURCES:
            out.append((o, Receipt.model_validate_json(stored)))
    return out


def n_independent(stamps: list[tuple[str, str]]) -> int:
    """How many of these (symbol, decided at) calls watched separate weeks: per symbol, greedily, a call
    counts only when it came HORIZON_DAYS or more after the last one counted. Two SOL calls two minutes
    apart are one look at SOL's next week, not two samples of it."""
    last: dict[str, datetime] = {}
    n = 0
    for sym, t in sorted((sym, _ts(ts)) for sym, ts in stamps):
        if sym not in last or t - last[sym] >= timedelta(days=HORIZON_DAYS):
            last[sym] = t
            n += 1
    return n


def _stamps(pairs: list[tuple[dict[str, Any], Receipt]]) -> list[tuple[str, str]]:
    return [(r.symbol, r.created_at) for _, r in pairs]


def scored_independent(ledger: Ledger) -> int:
    return n_independent(_stamps(_scored_outcomes(ledger)))


def role_scores(ledger: Ledger) -> dict[str, dict[str, float]]:
    sums: dict[str, list[float]] = {}
    for o, _ in _scored_outcomes(ledger):
        for role, b in o["brier"].items():
            sums.setdefault(role, []).append(b)
    return {r: {"n": float(len(v)), "brier_mean": round(sum(v) / len(v), 4)} for r, v in sums.items()}


MEANINGFUL_N = 20


def base_rate_for(symbol: str, decided_day: str) -> float | None:
    from nota.skills.base_rate import move_base_rate

    env = move_base_rate(symbol, k=0, horizon_days=HORIZON_DAYS, direction="up", event="close", as_of=decided_day)
    return env.data.get("p")


def fill_base_rates(ledger: Ledger, rate=base_rate_for) -> list[tuple[str, float | None]]:
    """Attach `base_rate_p` to every outcome that lacks one. Idempotent; a failed count stays None and
    is tried again next time."""
    out = []
    for raw in ledger.list_outcomes():
        o = json.loads(raw)
        if o.get("base_rate_p") is not None:
            continue
        stored = ledger.get_decision(o["decision_id"])
        if stored is None:
            continue
        p = rate(o["symbol"], Receipt.model_validate_json(stored).created_at[:10])
        if p is not None:
            o["base_rate_p"] = p
            ledger.save_outcome(o["decision_id"], json.dumps(o))
        out.append((o["decision_id"], p))
    return out


def repair_outcomes(ledger: Ledger) -> list[str]:
    """Rewrite `decided_as_of` and `horizon_reached` on stored outcomes from their own times, for outcomes
    written before both were computed this way (one SOL outcome resolved eight days after its decision
    was stored with horizon_reached false). Idempotent; returns the ids it changed."""
    changed = []
    for raw in ledger.list_outcomes():
        o = json.loads(raw)
        stored = ledger.get_decision(o["decision_id"])
        if stored is None:
            continue
        rec = Receipt.model_validate_json(stored)
        fixed = {"decided_as_of": o.get("decided_as_of") or rec.provenance.get("deep_analysis", {}).get("as_of") or rec.created_at}
        fixed["horizon_reached"] = _at_horizon({**o, **fixed}, rec)
        if any(o.get(k) != v for k, v in fixed.items()):
            ledger.save_outcome(o["decision_id"], json.dumps({**o, **fixed}))
            changed.append(o["decision_id"])
    return changed


def _enough(n: int, n_ind: int) -> bool:
    return n >= MEANINGFUL_N and n_ind >= MEANINGFUL_N


def skill_vs_base(ledger: Ledger) -> dict[str, Any]:
    """Brier skill score of each role against the token's own base rate: 1 - Brier / Brier(base).
    Above 0 the agent knew something the calendar did not; at or below 0 it did not. Only outcomes
    that reached the horizon and carry a base rate count. `n` is the smallest role count, and
    `enough_to_read` needs both it and `n_independent` at MEANINGFUL_N."""
    per: dict[str, list[tuple[float, float]]] = {}
    used, excluded = [], 0
    for o, rec in _scored_outcomes(ledger):
        base = o.get("base_rate_p")
        if base is None:
            continue
        if not _at_horizon(o, rec):
            excluded += 1
            continue
        used.append((o, rec))
        ref = (base - (1.0 if o["went_up"] else 0.0)) ** 2
        for role, b in o["brier"].items():
            per.setdefault(role, []).append((b, ref))
    roles = {}
    for role, pairs in per.items():
        b, r = sum(x for x, _ in pairs) / len(pairs), sum(y for _, y in pairs) / len(pairs)
        roles[role] = {"n": len(pairs), "brier_mean": round(b, 4), "base_brier_mean": round(r, 4),
                       "skill": round(1 - b / r, 4) if r else None}
    n = min((v["n"] for v in roles.values()), default=0)
    n_ind = n_independent(_stamps(used))
    return {"roles": roles, "n": n, "n_independent": n_ind, "excluded_before_horizon": excluded,
            "enough_to_read": _enough(n, n_ind), "meaningful_at": MEANINGFUL_N}


def skill_vs_ryo(ledger: Ledger) -> dict[str, Any]:
    """Did Nota beat RYO's own call? Over scored outcomes that reached the horizon and whose receipt has
    both a council direction and a RYO direction (`agrees_with_ryo` is not None), how often each side's
    direction matched the move. `disagreed` counts the calls where the two pointed opposite ways, the
    only ones that can separate them."""
    council_hits = ryo_hits = n = disagreed = council_won = excluded = 0
    used = []
    for o, rec in _scored_outcomes(ledger):
        if rec.agrees_with_ryo is None:
            continue
        if not _at_horizon(o, rec):
            excluded += 1
            continue
        used.append((o, rec))
        up = "long" if o["went_up"] else "short"
        c_hit, r_hit = rec.verdict.action == up, ryo_direction(rec.ryo_view) == up
        n += 1
        council_hits += c_hit
        ryo_hits += r_hit
        if not rec.agrees_with_ryo:
            disagreed += 1
            council_won += c_hit
    rate = lambda k: round(k / n, 4) if n else None
    n_ind = n_independent(_stamps(used))
    return {"n": n, "n_independent": n_ind, "excluded_before_horizon": excluded,
            "council_hit_rate": rate(council_hits), "ryo_hit_rate": rate(ryo_hits), "agreed": n - disagreed,
            "disagreed": disagreed, "council_right_when_disagreeing": council_won if disagreed else None,
            "enough_to_read": _enough(n, n_ind), "meaningful_at": MEANINGFUL_N}


def source_scores(ledger: Ledger) -> dict[str, Any]:
    """Which sources actually help, rather than which agents are right.

    For every scored decision that reached its horizon, the judge's Brier score is filed under each
    evidence section twice over: once by whether that section answered (ok, available or partial - a
    partial answer is still an answer), once by whether it did not. A source that earns its place
    should show a lower mean Brier in the answered bucket than in the missing one, and `helps_by` is
    that difference.

    Two rules keep this from becoming a claim it cannot support. A bucket with nothing in it scores
    None, never 0. And the whole table carries `enough_to_read`, which is false until enough
    independent decisions have been scored to mean anything - the difference between two three-sample
    means is noise, and presenting it as a finding would be the same offence as turning a null into a zero.
    """
    buckets: dict[str, dict[str, list[float]]] = {}
    used, excluded = [], 0
    for o, rec in _scored_outcomes(ledger):
        brier = (o.get("brier") or {}).get("judge")
        if brier is None:
            continue
        if not _at_horizon(o, rec):
            excluded += 1
            continue
        used.append((o, rec))
        for section, status in rec.availability.items():
            b = buckets.setdefault(section, {"answered": [], "missing": []})
            b["answered" if status in ("ok", "available", "partial") else "missing"].append(brier)

    def mean(values: list[float]) -> float | None:
        return round(sum(values) / len(values), 4) if values else None

    sources = {}
    for section, b in sorted(buckets.items()):
        answered, missing = mean(b["answered"]), mean(b["missing"])
        sources[section] = {
            "answered": {"n": len(b["answered"]), "brier_mean": answered},
            "missing": {"n": len(b["missing"]), "brier_mean": missing},
            # lower Brier is better, so a positive number means the decision went better with it
            "helps_by": round(missing - answered, 4) if answered is not None and missing is not None else None,
        }
    n_ind = n_independent(_stamps(used))
    return {"scored_decisions": len(used), "n_independent": n_ind, "excluded_before_horizon": excluded,
            "enough_to_read": _enough(len(used), n_ind), "meaningful_at": MEANINGFUL_N, "sources": sources}


def reliability(ledger: Ledger, bins: int = 5) -> dict[str, Any]:
    """Reliability (calibration) table for the judge: outcomes bucketed by stated p_up_7d.
    A well-calibrated judge has hit_rate close to mean_p in every bucket. Empty buckets stay empty.

    Only outcomes that actually reached the seven-day horizon count. A position closed early at its
    stop, or scored with `resolve --early`, answers a different question than "is the price higher in
    seven days", and mixing it in would bias the table towards whatever moves fastest. Those outcomes
    still feed `role_scores`, which is the trading feedback loop rather than a calibration claim."""
    rows: list[tuple[float, float]] = []
    used, excluded = [], 0
    for o, rec in _scored_outcomes(ledger):
        if not _at_horizon(o, rec):
            excluded += 1
            continue
        used.append((o, rec))
        rows.append((rec.verdict.p_up_7d, 1.0 if o["went_up"] else 0.0))
    table = []
    for i in range(bins):
        lo, hi = i / bins, (i + 1) / bins
        hits = [(p, y) for p, y in rows if lo <= p < hi or (i == bins - 1 and p == 1.0)]
        table.append({"bin": f"{lo:.1f}-{hi:.1f}", "n": len(hits),
                      "mean_p": round(sum(p for p, _ in hits) / len(hits), 3) if hits else None,
                      "hit_rate": round(sum(y for _, y in hits) / len(hits), 3) if hits else None})
    brier = round(sum((p - y) ** 2 for p, y in rows) / len(rows), 4) if rows else None
    n_ind = n_independent(_stamps(used))
    return {"n": len(rows), "n_independent": n_ind, "judge_brier": brier, "bins": table, "excluded_before_horizon": excluded,
            "enough_to_read": _enough(len(rows), n_ind), "meaningful_at": MEANINGFUL_N}


def role_weights(scores: dict[str, dict[str, float]]) -> dict[str, float]:
    """1/(brier+0.05), normalised so the best role has weight 1.0, floor 0.2; then shrunk towards 1.0 by
    n/(n+MEANINGFUL_N), so five scored calls move a weight a fifth of the way and one call barely at
    all. Unscored roles get 1.0."""
    raw = {r: 1.0 / (scores[r]["brier_mean"] + 0.05) for r in ROLES if r in scores}
    if not raw:
        return {r: 1.0 for r in ROLES}
    top = max(raw.values())
    out = {}
    for r in ROLES:
        if r not in raw:
            out[r] = 1.0
            continue
        n = scores[r].get("n", 0)
        out[r] = round(1 + (max(raw[r] / top, 0.2) - 1) * n / (n + MEANINGFUL_N), 3)
    return out
